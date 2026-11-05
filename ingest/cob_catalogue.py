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


def classify_doc(title, section):
    """What kind of document is this? (The 'Other Downloads' pane also holds BIRR reports.)"""
    t = title.lower()
    if section == "national":
        return "national_birr"
    if section == "county":
        return "county_birr"
    if "county governments budget implementation" in t:
        return "county_birr"
    if "national government budget implementation" in t:
        return "national_birr"
    if re.search(r"annual report|financial statements|strategic plan|audit report|auditor-general|"
                 r"end of term|sub-sector|stakeholders|m&e", t):
        return "ocob_corporate"
    return "agency_report"


def parse_title(title, doc_kind):
    t = title.lower()
    out = {"fy_start": None, "fy_end": None, "period": None, "fy_suspect": False}
    if doc_kind not in ("national_birr", "county_birr"):
        return out
    # FY like 2025/26, 2025/2026, 2014-2015, FY2020/2021, or 201718 (no separator)
    m = re.search(r"(20\d{2})\s*[/\-\u2013_]?\s*(?:20)?(\d{2})\b", title)
    if m:
        out["fy_start"] = int(m.group(1))
        out["fy_end"] = 2000 + int(m.group(2))
        out["fy_suspect"] = out["fy_end"] != out["fy_start"] + 1
    # Period (cumulative months covered). Old reports say "Third Quarter" = 9 months.
    if "nine months" in t or "third quarter" in t or "quarter three" in t:
        out["period"] = "9M"
    elif "six months" in t or "half year" in t or "half-year" in t or "first half" in t \
            or "second quarter" in t or "quarter two" in t:
        out["period"] = "6M"
    elif "three months" in t or "first quarter" in t or "quarter one" in t:
        out["period"] = "3M"
    elif "fourth quarter" in t or "quarter four" in t:
        out["period"] = "12M"
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
                doc_kind = classify_doc(it["title"], kind)
                rec = dict(it, section=kind, doc_kind=doc_kind, page_found=page, scraped_at=now,
                           listing_size_zero=size_is_zero(it["listing_size_text"]))
                rec.update(parse_title(it["title"], doc_kind))
                rec["needs_review"] = (
                    "no financial year in title" if doc_kind in ("national_birr", "county_birr") and rec["fy_start"] is None
                    else "FY looks odd" if rec["fy_suspect"]
                    else "listed size is 0 (file may be missing)" if rec["listing_size_zero"]
                    else "")
                rec.pop("pane", None)
                catalogue[it["wpdm_id"]] = rec
            print(f"  {len(items)} items on page, {new} new", flush=True)
            if new == 0:
                break
            time.sleep(PAUSE)

    records = sorted(catalogue.values(), key=lambda r: (r["doc_kind"], -(r["fy_start"] or 0), r["wpdm_id"]))
    with open("reports/cob_catalogue.json", "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    fields = ["wpdm_id", "section", "doc_kind", "title", "fy_start", "fy_end", "period", "needs_review",
              "listing_size_text", "listing_size_zero", "listing_downloads", "download_url", "page_found", "scraped_at"]
    with open("reports/cob_catalogue.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k) for k in fields})

    # Summary
    kinds = ["national_birr", "county_birr", "ocob_corporate", "agency_report"]
    lines = ["## COB catalogue v2", "", f"Total records: {len(records)}", "",
             "| Document kind | Reports | Needs review |", "|---|---|---|"]
    for k in kinds:
        rs = [r for r in records if r["doc_kind"] == k]
        lines.append(f"| {k} | {len(rs)} | {sum(1 for r in rs if r['needs_review'])} |")
    lines += ["", "### Same kind + FY + period listed more than once (likely re-uploads)", ""]
    seen = {}
    for r in records:
        if r["doc_kind"] in ("national_birr", "county_birr") and r["fy_start"]:
            seen.setdefault((r["doc_kind"], r["fy_start"], r["period"]), []).append(r["wpdm_id"])
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    lines += [f"- {k[0]} FY{k[1]} {k[2]}: ids {v}" for k, v in dups.items()] or ["- none"]
    lines += ["", "### Needs review", ""]
    lines += [f"- {r['wpdm_id']} ({r['doc_kind']}): {r['needs_review']} - {r['title'][:70]}"
              for r in records if r["needs_review"]] or ["- none"]
    lines += ["", "### Latest national and county reports", "", "| Kind | FY | Period | Id | Size |", "|---|---|---|---|---|"]
    for k in ("national_birr", "county_birr"):
        for r in [x for x in records if x["doc_kind"] == k][:6]:
            lines.append(f"| {k} | {r['fy_start']}/{r['fy_end']} | {r['period']} | {r['wpdm_id']} | {r['listing_size_text']} |")
    table = "\n".join(lines)
    print("\n" + table)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(table + "\n")


if __name__ == "__main__":
    main()
