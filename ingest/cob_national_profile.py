#!/usr/bin/env python3
"""Map the National Government report (FY2025/26) before writing an extractor.

Prints, for the whole report: every 'Table N.N: title' with its PDF page (so we can pick the tables that
matter: budget by vote/MDA, exchequer issues, expenditure, pending bills), and the raw layout text of a few
pages (DUMP_PAGES) so I can see how wrapped vote names and the 12-number rows are laid out.
Nothing is written to D1. Output: reports/cob_national_profile.json and the job summary.
"""
import hashlib
import json
import os
import re

import requests
from pypdf import PdfReader

HEADERS = {"User-Agent": "PesaWatchKenya-cob-national/0.1 (public-finance transparency project)"}
URL = ("https://cob.go.ke/download/national-government-budget-implementation-review-report-fy-2025-2026/"
       "?wpdmdl=16454")
CACHE = os.path.join("pdf_cache", "national_annual_2025_26.pdf")
DUMP_PAGES = [57, 58, 59, 60, 61, 62, 262, 263]   # chapter 3 summary tables + one sector table
LINES_PER_DUMP = 80
TITLE_RE = re.compile(r"^\s*(Table\s+\d+\.\d+\s*[:.]?\s*.{3,150})$")


def get_pdf():
    os.makedirs("pdf_cache", exist_ok=True)
    if os.path.exists(CACHE) and os.path.getsize(CACHE) > 500_000:
        print("Using cached national report", flush=True)
    else:
        print("Downloading national report (about 15 MB)...", flush=True)
        with requests.get(URL, headers=HEADERS, timeout=(15, 600), stream=True) as r:
            r.raise_for_status()
            with open(CACHE + ".part", "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    f.write(chunk)
        os.replace(CACHE + ".part", CACHE)
    h = hashlib.sha256()
    with open(CACHE, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return CACHE, h.hexdigest()


def page_text(page):
    try:
        return page.extract_text(extraction_mode="layout") or ""
    except TypeError:
        return page.extract_text() or ""


def profile(path):
    reader = PdfReader(path)
    titles, dumps = [], {}
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page_text(page)
        except Exception:  # noqa: BLE001
            continue
        for ln in text.splitlines():
            m = TITLE_RE.match(ln)
            if m:
                titles.append({"page": i, "title": re.sub(r"\s{2,}", " ", m.group(1)).strip()[:150]})
        if i in DUMP_PAGES:
            keep = []
            for ln in text.splitlines():
                if ln.strip():
                    keep.append(re.sub(r"[ \t]{4,}", "   ", ln).rstrip()[:200])
                if len(keep) >= LINES_PER_DUMP:
                    break
            dumps[str(i)] = keep
    return {"pages": len(reader.pages), "table_titles": titles, "page_dumps": dumps}


def main():
    os.makedirs("reports", exist_ok=True)
    path, sha = get_pdf()
    res = profile(path)
    res["sha256"] = sha
    with open("reports/cob_national_profile.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    lines = ["## National report profile", "", f"- {res['pages']} pages, {len(res['table_titles'])} table titles found, sha256 {sha[:12]}", ""]
    seen = set()
    for t in res["table_titles"]:
        key = (t["title"][:60])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- p{t['page']}: {t['title']}")
        if len(seen) >= 70:
            lines.append("- ... (full list in the JSON)")
            break
    text = "\n".join(lines)
    print(text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
