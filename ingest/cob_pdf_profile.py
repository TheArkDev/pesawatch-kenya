#!/usr/bin/env python3
"""Profile Controller of Budget PDFs so we can write a reliable table extractor.

Downloads the FY2025/26 annual reports (national + county), then reports for each:
page count, how many pages have no text layer (scanned), the PDF outline (table of contents),
and the pages most likely to hold budget-performance tables, with their first lines of text.
Nothing is written to D1. Outputs reports/cob_pdf_profile.json and the job summary.
"""
import hashlib
import json
import os
import re
import tempfile
import time

import requests
from pypdf import PdfReader

HEADERS = {"User-Agent": "PesaWatchKenya-cob-profile/0.1 (public-finance transparency project)"}
DOCS = [
    ("county_annual_2025_26",
     "https://cob.go.ke/download/county-governments-budget-implementation-review-report-for-the-financial-year-2025-26/?wpdmdl=16482"),
    ("national_annual_2025_26",
     "https://cob.go.ke/download/national-government-budget-implementation-review-report-fy-2025-2026/?wpdmdl=16454"),
]
KEYWORDS = ["exchequer", "absorption", "approved budget", "annual budget", "supplementary", "pending bills",
            "development expenditure", "recurrent expenditure", "own source revenue", "budget performance",
            "revised budget", "actual expenditure", "total expenditure", "county executive"]
TOP_PAGES = 12
LINES_PER_PAGE = 45


def download(url, path):
    sha = hashlib.sha256()
    size = 0
    start = time.monotonic()
    with requests.get(url, headers=HEADERS, timeout=(15, 600), stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)
                sha.update(chunk)
                size += len(chunk)
    return {"bytes": size, "sha256": sha.hexdigest(), "seconds": round(time.monotonic() - start, 1)}


def page_text(page):
    try:
        return page.extract_text(extraction_mode="layout") or ""
    except TypeError:
        return page.extract_text() or ""


def flatten_outline(reader, items, level=0, out=None):
    out = [] if out is None else out
    for it in items:
        if isinstance(it, list):
            flatten_outline(reader, it, level + 1, out)
        else:
            try:
                pg = reader.get_destination_page_number(it) + 1
            except Exception:  # noqa: BLE001
                pg = None
            out.append({"level": level, "title": str(getattr(it, "title", ""))[:120], "page": pg})
    return out


def clean_lines(text, n):
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t]{6,}", "    ", ln).rstrip()
        if ln.strip():
            lines.append(ln[:170])
        if len(lines) >= n:
            break
    return lines


def profile_pdf(path):
    reader = PdfReader(path)
    n = len(reader.pages)
    scanned = 0
    scored = []
    for i, page in enumerate(reader.pages):
        try:
            text = page_text(page)
        except Exception:  # noqa: BLE001
            text = ""
        if len(text.strip()) < 40:
            scanned += 1
            continue
        low = text.lower()
        hits = [k for k in KEYWORDS if k in low]
        numbers = len(re.findall(r"\d[\d,]{3,}", text))
        score = len(hits) * 10 + min(numbers, 200) / 10
        if hits:
            scored.append((score, i + 1, hits, numbers, text))
    scored.sort(reverse=True)
    candidates = [{"page": pg, "score": round(sc, 1), "keywords": hits, "numeric_tokens": nums,
                   "first_lines": clean_lines(text, LINES_PER_PAGE)}
                  for sc, pg, hits, nums, text in scored[:TOP_PAGES]]
    try:
        outline = flatten_outline(reader, reader.outline)[:120]
    except Exception:  # noqa: BLE001
        outline = []
    return {"pages": n, "pages_without_text": scanned, "outline_entries": len(outline),
            "outline": outline, "candidate_pages": candidates}


def main():
    os.makedirs("reports", exist_ok=True)
    out, lines = {}, ["## COB PDF profile", ""]
    with tempfile.TemporaryDirectory() as tmp:
        for label, url in DOCS:
            print(f"{label}: downloading ...", flush=True)
            path = os.path.join(tmp, label + ".pdf")
            try:
                meta = download(url, path)
                print(f"  {meta}", flush=True)
                prof = profile_pdf(path)
                prof["download"] = meta
            except Exception as e:  # noqa: BLE001
                prof = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            out[label] = prof
            lines.append(f"### {label}")
            if "error" in prof:
                lines.append(f"- FAILED: {prof['error']}")
                continue
            lines.append(f"- {prof['pages']} pages, {prof['pages_without_text']} without a text layer, "
                         f"{prof['outline_entries']} outline entries, {prof['download']['bytes']} bytes")
            lines.append("- candidate pages: " + ", ".join(
                f"p{c['page']} (score {c['score']})" for c in prof["candidate_pages"]))
            lines.append("- outline (first 25): " + "; ".join(
                f"{'  ' * o['level']}{o['title']} [p{o['page']}]" for o in prof["outline"][:25]))
            lines.append("")
    with open("reports/cob_pdf_profile.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    text = "\n".join(lines)
    print("\n" + text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
