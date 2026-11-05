#!/usr/bin/env python3
"""Probe the Controller of Budget download mechanism (WordPress Download Manager).

Goals:
  1. Confirm the two known download URLs really deliver PDFs, and see where they redirect.
  2. Look in the raw HTML of the publications listing pages for report IDs (wpdmdl=NNNN)
     so a scraper can enumerate every report.

Each test downloads only the first KB, but the plugin may still count it as a download,
so we keep the number of requests small and polite.
Outputs: reports/cob_probe.json, reports/cob_publications_page1.html, job summary.
"""
import json
import os
import re
import time

import requests

HEADERS = {"User-Agent": "PesaWatchKenya-cob-probe/0.1 (public-finance transparency project)"}

KNOWN = [
    ("National govt FY2025/26 report",
     "https://cob.go.ke/download/national-government-budget-implementation-review-report-fy-2025-2026/?wpdmdl=16454"),
    ("County govts FY2025/26 report",
     "https://cob.go.ke/download/county-governments-budget-implementation-review-report-for-the-financial-year-2025-26/?wpdmdl=16482"),
]

LISTING_PAGES = [
    "https://cob.go.ke/publications/",
    "https://cob.go.ke/publications/?&cp_national-government=2",
    "https://cob.go.ke/publications/?&cp_national-government=3",
    "https://cob.go.ke/publications/?&cp_consolidated-county-review=2",
    "https://cob.go.ke/publications/?&cp_consolidated-county-review=3",
]

PATTERNS = {
    "wpdmdl_ids": r"wpdmdl=(\d+)",
    "download_urls": r"https?://cob\.go\.ke/download/[^\"'\s<>]+",
    "data_downloadurl": r"data-downloadurl=[\"']([^\"']+)[\"']",
    "data_package_ids": r"data-(?:package|id|pid)=[\"'](\d{3,6})[\"']",
}


def test_known(name, url):
    r = {"name": name, "url": url}
    start = time.monotonic()
    try:
        resp = requests.get(url, headers=dict(HEADERS, Range="bytes=0-1023"),
                            timeout=(15, 90), stream=True, allow_redirects=True)
        head = next(resp.iter_content(1024), b"")
        r.update(
            status=resp.status_code,
            seconds=round(time.monotonic() - start, 2),
            final_url=resp.url,
            redirects=[h.headers.get("location", "") for h in resp.history],
            content_type=resp.headers.get("content-type", ""),
            content_disposition=resp.headers.get("content-disposition", ""),
            content_length=resp.headers.get("content-length", ""),
            content_range=resp.headers.get("content-range", ""),
            is_pdf=head.startswith(b"%PDF"),
            first_bytes=head[:12].hex(),
        )
        resp.close()
    except requests.RequestException as e:
        r.update(status=None, error=f"{type(e).__name__}: {str(e)[:200]}")
    return r


def scan_listing(url, save_html_as=None):
    r = {"url": url}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=(15, 60))
        r["status"] = resp.status_code
        r["html_bytes"] = len(resp.content)
        for key, pat in PATTERNS.items():
            found = sorted(set(re.findall(pat, resp.text)))
            r[key] = found[:60]
            r[key + "_count"] = len(found)
        if save_html_as:
            os.makedirs("reports", exist_ok=True)
            with open(save_html_as, "w", encoding="utf-8") as f:
                f.write(resp.text)
    except requests.RequestException as e:
        r.update(status=None, error=f"{type(e).__name__}: {str(e)[:200]}")
    return r


def main():
    os.makedirs("reports", exist_ok=True)
    out = {"known_downloads": [], "listing_pages": []}

    for name, url in KNOWN:
        print(f"Testing download: {name}", flush=True)
        res = test_known(name, url)
        out["known_downloads"].append(res)
        print(f"  status={res.get('status')} pdf={res.get('is_pdf')} final={res.get('final_url')}", flush=True)
        time.sleep(3)

    for i, url in enumerate(LISTING_PAGES):
        print(f"Scanning listing: {url}", flush=True)
        res = scan_listing(url, "reports/cob_publications_page1.html" if i == 0 else None)
        out["listing_pages"].append(res)
        print(f"  status={res.get('status')} wpdmdl ids={res.get('wpdmdl_ids_count')} "
              f"download urls={res.get('download_urls_count')}", flush=True)
        time.sleep(3)

    with open("reports/cob_probe.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    lines = ["## COB probe", "", "| Known download | Status | PDF? | Final URL |", "|---|---|---|---|"]
    for d in out["known_downloads"]:
        lines.append(f"| {d['name']} | {d.get('status')} | {d.get('is_pdf')} | {(d.get('final_url') or d.get('error') or '')[:110]} |")
    lines += ["", "| Listing page | Status | wpdmdl IDs | /download/ URLs | data-downloadurl |", "|---|---|---|---|---|"]
    for p in out["listing_pages"]:
        lines.append(f"| {p['url'].replace('https://cob.go.ke', '')} | {p.get('status')} | "
                     f"{p.get('wpdmdl_ids_count', '')} | {p.get('download_urls_count', '')} | {p.get('data_downloadurl_count', '')} |")
    table = "\n".join(lines)
    print("\n" + table)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(table + "\n")


if __name__ == "__main__":
    main()
