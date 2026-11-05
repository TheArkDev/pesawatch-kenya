#!/usr/bin/env python3
"""Connectivity + download check for PesaWatch Kenya data sources (v2).

Run from a GitHub Actions runner. It tells us:
  1. which portals respond, how fast, and WHY any fail (error classification);
  2. whether real files can be downloaded (PPIP OCDS bulk file, sample PDFs
     discovered on the Controller of Budget and Auditor-General sites).

Writes sources_report.json and a Markdown table (also appended to the GitHub
job summary when GITHUB_STEP_SUMMARY is set). TLS verification is never
disabled: if a certificate is wrong we want to know, not bypass it.
"""
import gzip
import io
import json
import os
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = "PesaWatchKenya-source-check/0.2 (public-finance transparency project)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
RETRIES = 2
MAX_PAGE_BYTES = 64 * 1024

FAST = (10, 45)   # (connect, read) seconds
SLOW = (15, 90)   # Bajeti Yetu answered in ~20s last time

PAGES = [
    ("PPIP OCDS page", "https://tenders.go.ke/ocds", FAST),
    ("PPIP home (no www)", "https://tenders.go.ke/", FAST),
    ("OCP registry (PPIP dataset)", "https://data.open-contracting.org/en/publication/147", FAST),
    ("Bajeti Yetu home", "https://bajetiyetu.treasury.go.ke/", SLOW),
    ("Bajeti Yetu national", "https://bajetiyetu.treasury.go.ke/site/national?type=nat", SLOW),
    ("Bajeti Yetu county (ctid=31)", "https://bajetiyetu.treasury.go.ke/site/county-analysis?ctid=31", SLOW),
    ("Controller of Budget publications", "https://cob.go.ke/publications/", FAST),
    ("Auditor-General supreme auditor", "https://www.oagkenya.go.ke/supreme-auditor", FAST),
    ("Treasury QEBR page", "https://www.treasury.go.ke/node/424", FAST),
    ("Open Budget Kenya (no www)", "https://openbudget.or.ke/", FAST),
    ("GPRIS https /en", "https://gpris.go.ke/en", FAST),
    ("GPRIS https root", "https://gpris.go.ke/", FAST),
    ("GPRIS http root", "http://gpris.go.ke/", FAST),
    ("GPRIS https www", "https://www.gpris.go.ke/", FAST),
    ("Parliament library", "https://libraryir.parliament.go.ke/", FAST),
]

OCDS_SAMPLE = "https://data.open-contracting.org/en/publication/147/download?name=2026.jsonl.gz"

PDF_DISCOVERY = [
    ("Controller of Budget", "https://cob.go.ke/publications/"),
    ("Auditor-General", "https://www.oagkenya.go.ke/supreme-auditor"),
    ("Treasury QEBR", "https://www.treasury.go.ke/node/424"),
]


def classify(exc):
    msg = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in msg and "Hostname mismatch" in msg:
        return "TLS hostname mismatch (site certificate misconfigured)"
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        return "TLS certificate error"
    if "Connection refused" in msg:
        return "Connection refused (server rejects us: IP/geo block or service down)"
    if "NameResolutionError" in msg or "Name or service not known" in msg or "name resolution" in msg:
        return "DNS failure"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "Connect timeout (firewall silently dropping?)"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "Read timeout (server too slow)"
    if "Connection reset" in msg or "RemoteDisconnected" in msg or "Connection aborted" in msg:
        return "Connection reset by server"
    return f"{type(exc).__name__}"


def dns_info(url):
    host = urlparse(url).hostname
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
        return f"resolves to {', '.join(ips)}"
    except Exception as e:  # noqa: BLE001
        return f"DNS lookup failed ({type(e).__name__})"


def probe_page(name, url, timeout):
    r = {"kind": "page", "name": name, "url": url}
    last = None
    for attempt in range(1, RETRIES + 2):
        start = time.monotonic()
        try:
            with requests.get(url, headers=HEADERS, timeout=timeout, stream=True, allow_redirects=True) as resp:
                body = b""
                for chunk in resp.iter_content(8192):
                    body += chunk
                    if len(body) >= MAX_PAGE_BYTES:
                        break
                text = body.decode("utf-8", errors="ignore").lower()
                r.update(
                    status=resp.status_code,
                    seconds=round(time.monotonic() - start, 2),
                    final_url=resp.url,
                    content_type=resp.headers.get("content-type", ""),
                    bytes_read=len(body),
                    looks_blocked=any(s in text for s in ("access denied", "captcha", "request blocked")),
                    looks_js_shell=(len(body) < 4000 and "<script" in text),
                    attempts=attempt,
                    detail="",
                )
                return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 * attempt)
    r.update(status=None, seconds=None, attempts=RETRIES + 1, detail=f"{classify(last)}; {dns_info(url)}")
    return r


def page_verdict(r):
    if r["status"] is None:
        return "FAIL"
    if r["status"] >= 400:
        return f"HTTP {r['status']}"
    if r.get("looks_blocked"):
        return "SUSPECT (block page?)"
    if r.get("looks_js_shell"):
        return "OK, JS shell"
    return "OK"


def test_ocds_file():
    r = {"kind": "file", "name": "PPIP OCDS 2026 bulk file", "url": OCDS_SAMPLE}
    start = time.monotonic()
    try:
        resp = requests.get(OCDS_SAMPLE, headers=HEADERS, timeout=(15, 180), stream=True)
        data = b""
        for chunk in resp.iter_content(65536):
            data += chunk
            if len(data) > 20 * 1024 * 1024:
                break
        r.update(status=resp.status_code, seconds=round(time.monotonic() - start, 2), bytes_read=len(data))
        if resp.status_code != 200:
            r["verdict"], r["detail"] = f"HTTP {resp.status_code}", ""
        elif data[:2] != b"\x1f\x8b":
            r["verdict"], r["detail"] = "NOT GZIP", "response is not a gzip file"
        else:
            line = gzip.GzipFile(fileobj=io.BytesIO(data)).readline()
            obj = json.loads(line)
            r["verdict"] = "OK"
            r["detail"] = f"gzip ok; first record ocid={obj.get('ocid')}; keys={sorted(obj.keys())[:8]}"
    except Exception as e:  # noqa: BLE001
        r.update(status=None, seconds=None, verdict="FAIL", detail=f"{classify(e)}: {str(e)[:160]}")
    return r


def test_pdf_discovery(name, page_url):
    out = []
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=FAST)
        links = re.findall(r'href=["\']([^"\']+?\.pdf)["\']', resp.text, flags=re.I)
        links = list(dict.fromkeys(urljoin(page_url, l) for l in links))
    except requests.RequestException as e:
        return [{"kind": "pdf", "name": f"{name}: find PDF links", "url": page_url, "status": None,
                 "seconds": None, "verdict": "FAIL", "detail": classify(e)}]
    if not links:
        return [{"kind": "pdf", "name": f"{name}: find PDF links", "url": page_url, "status": resp.status_code,
                 "seconds": None, "verdict": "NO PDF LINKS", "detail": "page has no direct .pdf links (JS-loaded?)"}]
    for link in links[:2]:
        r = {"kind": "pdf", "name": f"{name}: sample PDF", "url": link}
        start = time.monotonic()
        try:
            h = dict(HEADERS, Range="bytes=0-1023")
            resp = requests.get(link, headers=h, timeout=(15, 90), stream=True)
            head = next(resp.iter_content(1024), b"")
            r.update(status=resp.status_code, seconds=round(time.monotonic() - start, 2),
                     bytes_read=len(head))
            ok = head.startswith(b"%PDF")
            total = resp.headers.get("content-range", resp.headers.get("content-length", "?"))
            r["verdict"] = "OK" if ok else "NOT A PDF"
            r["detail"] = f"size info: {total}; range requests {'supported' if resp.status_code == 206 else 'ignored'}"
        except requests.RequestException as e:
            r.update(status=None, seconds=None, verdict="FAIL", detail=classify(e))
        out.append(r)
    return out


def main():
    results = []
    for name, url, timeout in PAGES:
        print(f"Page: {name}", flush=True)
        r = probe_page(name, url, timeout)
        r["verdict"] = page_verdict(r)
        results.append(r)
        print(f"  -> {r['verdict']} {r.get('status')} {r.get('seconds')}s {r.get('detail', '')}", flush=True)

    print("File: PPIP OCDS bulk sample", flush=True)
    results.append(test_ocds_file())
    print(f"  -> {results[-1]['verdict']} {results[-1].get('detail', '')}", flush=True)

    for name, page in PDF_DISCOVERY:
        print(f"PDF discovery: {name}", flush=True)
        for r in test_pdf_discovery(name, page):
            results.append(r)
            print(f"  -> {r['verdict']} {r.get('detail', '')}", flush=True)

    with open("sources_report.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    lines = ["| Check | Verdict | Status | Seconds | Detail |", "|---|---|---|---|---|"]
    for r in results:
        detail = (r.get("detail") or "").replace("|", "/")[:140]
        lines.append(f"| {r['name']} | {r['verdict']} | {r.get('status')} | {r.get('seconds')} | {detail} |")
    table = "\n".join(lines)
    print("\n" + table)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("## PesaWatch source check v2\n\n" + table + "\n")


if __name__ == "__main__":
    main()
