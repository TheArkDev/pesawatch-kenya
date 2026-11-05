#!/usr/bin/env python3
"""Connectivity check for PesaWatch Kenya data sources.

Run from a GitHub Actions runner to learn which official portals respond,
how fast, and whether they return real content (not a block page).
Writes sources_report.json and a Markdown table (also appended to the
GitHub job summary when GITHUB_STEP_SUMMARY is set).
"""
import json
import os
import time

import requests

USER_AGENT = "PesaWatchKenya-source-check/0.1 (public-finance transparency project)"
TIMEOUT = (10, 30)  # connect, read (seconds)
RETRIES = 2
MAX_BYTES = 64 * 1024  # read only the first 64 KB; this is a probe, not a scrape

SOURCES = [
    ("PPIP OCDS page", "https://tenders.go.ke/ocds"),
    ("PPIP home", "https://www.tenders.go.ke/"),
    ("OCP registry (PPIP dataset)", "https://data.open-contracting.org/en/publication/147"),
    ("Bajeti Yetu home", "https://bajetiyetu.treasury.go.ke/"),
    ("Bajeti Yetu FAQ", "https://bajetiyetu.treasury.go.ke/site/faq"),
    ("Bajeti Yetu national", "https://bajetiyetu.treasury.go.ke/site/national?type=nat"),
    ("Bajeti Yetu county (ctid=31)", "https://bajetiyetu.treasury.go.ke/site/county-analysis?ctid=31"),
    ("Controller of Budget home", "https://cob.go.ke/"),
    ("Controller of Budget publications", "https://cob.go.ke/publications/"),
    ("Auditor-General home", "https://www.oagkenya.go.ke/"),
    ("Auditor-General supreme auditor", "https://www.oagkenya.go.ke/supreme-auditor"),
    ("GPRIS", "https://gpris.go.ke/en"),
    ("Treasury QEBR page", "https://www.treasury.go.ke/node/424"),
    ("Open Budget Kenya", "https://www.openbudget.or.ke/"),
    ("Parliament library (backup copies)", "https://libraryir.parliament.go.ke/"),
]


def probe(name, url):
    result = {"name": name, "url": url}
    last_error = None
    for attempt in range(1, RETRIES + 2):
        start = time.monotonic()
        try:
            with requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                timeout=TIMEOUT,
                stream=True,
                allow_redirects=True,
            ) as r:
                body = b""
                for chunk in r.iter_content(chunk_size=8192):
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                elapsed = time.monotonic() - start
                text = body.decode("utf-8", errors="ignore").lower()
                result.update(
                    status=r.status_code,
                    seconds=round(elapsed, 2),
                    final_url=r.url,
                    content_type=r.headers.get("content-type", ""),
                    content_length_header=r.headers.get("content-length", ""),
                    bytes_read=len(body),
                    server=r.headers.get("server", ""),
                    # Rough signals of a block page or a JS-only shell
                    looks_blocked=any(
                        s in text
                        for s in ("access denied", "forbidden", "captcha", "cloudflare ray id", "request blocked")
                    ),
                    looks_js_shell=(len(body) < 4000 and "<script" in text),
                    attempts=attempt,
                    error=None,
                )
                return result
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            time.sleep(2 * attempt)
    result.update(status=None, seconds=None, error=last_error, attempts=RETRIES + 1)
    return result


def verdict(r):
    if r.get("error"):
        return "FAIL (no response)"
    if r["status"] and r["status"] >= 400:
        return f"BLOCKED/ERROR ({r['status']})"
    if r.get("looks_blocked"):
        return "SUSPECT (block page?)"
    if r.get("looks_js_shell"):
        return "OK but JS shell (needs network-tab discovery)"
    return "OK"


def main():
    results = []
    for name, url in SOURCES:
        print(f"Checking {name} ...", flush=True)
        r = probe(name, url)
        r["verdict"] = verdict(r)
        results.append(r)
        print(f"  -> {r['verdict']}  status={r.get('status')}  {r.get('seconds')}s", flush=True)

    with open("sources_report.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    lines = [
        "| Source | Verdict | Status | Seconds | Type | Bytes read |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['name']} | {r['verdict']} | {r.get('status')} | {r.get('seconds')} "
            f"| {(r.get('content_type') or '').split(';')[0]} | {r.get('bytes_read', '')} |"
        )
    table = "\n".join(lines)
    print("\n" + table)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("## PesaWatch source connectivity check\n\n" + table + "\n")


if __name__ == "__main__":
    main()
