#!/usr/bin/env python3
"""Build a catalogue of every report on the Controller of Budget publications page.

Does NOT download any PDF. It reads the listing pages (3 sections, each paginated with
?&cp_<section>=N), and records for each report: WPDM id, title, section, financial year,
period (3M/6M/9M/annual), listed size, listed download count and the download URL.

Outputs: reports/cob_catalogue.json, reports/cob_catalogue.csv, job summary table.
"""
import csv
import html
import json
import os
import re
import time
from datetime import datetime, timezone

import requests

BASE = "https://cob.go.ke/publications/"
HEADERS = {"User-Agent": "PesaWatchKenya-cob-catalogue/0.1 (public-finance transparency project)"}
SECTIONS = [
    ("National Government Budget Implementation Review Reports", "national", "cp_national-government"),
    ("Consolidated County Budget Implementation Review Reports", "county", "cp_consolidated-county-review"),
    ("Other Downloads", "other", "cp_downloads"),
]
MAX_PAGES = 12
PAUSE = 3  # seconds between requests: be polite

PANE_RE = re.compile(r"\[pane title=&#8221;(.*?)&#8221;\]")
ITEM_RE = re.compile(
    r'<strong class="ptitle">(.*?)</strong>.*?'
    r"location\.href='([^']*?wpdmdl=(\d+))[^']*'.*?"
    r'<span class="pull-right">([\d,]+) downloads</span>\s*([\d.,]+ [KMG]B)',
    re.S,
)


def parse_listing(page_html):
    """Return list of dicts for every report block in one listing page."""
    panes = [(m.start(), html.unescape(m.group(1)).strip()) for m in PANE_RE.finditer(page_html)]
    items = []
    for m in ITEM_RE.finditer(page_html):
        pane = next((p for pos, p in reversed(panes) if pos < m.start()), None)
        items.append({
            "pane": pane,
            "title": html.unescape(m.group(1)).strip(),
            "download_url": m.group(2),
            "wpdm_id": int(m.group(3)),
            "listing_downloads": int(m.group(4).replace(",", "")),
            "listing_size_text": m.group(5),
        })
    return items


def parse_title(title, kind):
    t = title.lower()
    out = {"fy_start": None, "fy_end": None, "period": None}
    if kind in ("national", "county"):
        m = re.search(r"(20\d{2})\s*[/\-\u2013_]\s*(?:20)?(\d{2})\b", title)
        if m:
            out["fy_start"] = int(m.group(1))
            out["fy_end"] = 2000 + int(m.group(2))
        if "nine months" in t:
            out["period"] = "9M"
        elif "six months" in t or "first half" in t or "half of" in t:
            out["period"] = "6M"
        elif "three months" in t or "first quarter" in t or "quarter" in t:
            out["period"] = "3M"
        else:
            out["period"] = "annual"
    return out


def size_is_zero(text):
    return text.strip().startswith("0.00")


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=(15, 90))
    r.raise_for_status()
    return r.text


def main():
    os.makedirs("reports", exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    catalogue = {}  # wpdm_id -> record

    for pane_title, kind, param in SECTIONS:
        for page in range(1, MAX_PAGES + 1):
            url = BASE if page == 1 else f"{BASE}?&{param}={page}"
            print(f"[{kind}] page {page}: {url}", flush=True)
            try:
                items = [i for i in parse_listing(fetch(url)) if i["pane"] == pane_title]
            except requests.RequestException as e:
                print(f"  fetch failed: {e}", flush=True)
                break
            new = 0
            for it in items:
                if it["wpdm_id"] in catalogue:
                    continue
                new += 1
                rec = dict(it, section=kind, page_found=page, scraped_at=now,
                           listing_size_zero=size_is_zero(it["listing_size_text"]))
                rec.update(parse_title(it["title"], kind))
                rec.pop("pane", None)
                catalogue[it["wpdm_id"]] = rec
            print(f"  {len(items)} items on page, {new} new", flush=True)
            if new == 0:
                break
            time.sleep(PAUSE)

    records = sorted(catalogue.values(), key=lambda r: (r["section"], -(r["fy_start"] or 0), r["wpdm_id"]))
    with open("reports/cob_catalogue.json", "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    fields = ["wpdm_id", "section", "title", "fy_start", "fy_end", "period", "listing_size_text",
              "listing_size_zero", "listing_downloads", "download_url", "page_found", "scraped_at"]
    with open("reports/cob_catalogue.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k) for k in fields})

    # Summary
    lines = ["## COB catalogue", "", "| Section | Reports | Zero-size files | No FY parsed |", "|---|---|---|---|"]
    for _, kind, _ in SECTIONS:
        rs = [r for r in records if r["section"] == kind]
        zero = sum(r["listing_size_zero"] for r in rs)
        nofy = sum(1 for r in rs if kind != "other" and r["fy_start"] is None)
        lines.append(f"| {kind} | {len(rs)} | {zero} | {nofy} |")
    lines += ["", "### Same section + FY + period listed more than once", ""]
    seen = {}
    for r in records:
        if r["section"] in ("national", "county") and r["fy_start"]:
            seen.setdefault((r["section"], r["fy_start"], r["period"]), []).append(r["wpdm_id"])
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    lines += [f"- {k[0]} FY{k[1]} {k[2]}: ids {v}" for k, v in dups.items()] or ["- none"]
    lines += ["", "### Latest national and county reports", "", "| Section | FY | Period | Id | Size |", "|---|---|---|---|---|"]
    for kind in ("national", "county"):
        for r in [x for x in records if x["section"] == kind][:6]:
            lines.append(f"| {kind} | {r['fy_start']}/{r['fy_end']} | {r['period']} | {r['wpdm_id']} | {r['listing_size_text']} |")
    table = "\n".join(lines)
    print("\n" + table)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(table + "\n")


if __name__ == "__main__":
    main()
