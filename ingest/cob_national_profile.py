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
DUMP_PAGES = [69, 70, 79, 80, 85, 88, 93, 96, 97, 262]   # MDA tables 3.10/3.11, compensation 3.12, payables 3.14, sector examples
LINES_PER_DUMP = 90
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


NUM = r"(?:\(?-?\d[\d,]*(?:\.\d+)?\)?%?|[-\u2013\u2014])"
_NUMS = NUM + r"(?:\s+" + NUM + r"){5,}"
# A name must be separated from the numbers by whitespace, so "Allo-" (name wrapped with a hyphen) is not
# mistaken for a dash value. Lines that are all numbers are also rows (name printed on other lines).
NUM_ROW = re.compile(r"^(?:(?P<name>.*?)\s+)?(?P<nums>" + _NUMS + r")\s*$")
LINE_TOL = 2.5      # points: fragments with y within this belong to one printed line
MAX_DY = 30.0       # points: how far a name fragment may sit from the numbers line it belongs to


def lines_with_positions(page):
    """Text lines with their y position, built from positioned text fragments."""
    frags = []

    def visitor(text, cm, tm, font_dict, font_size):
        t = (text or "").replace("\n", " ").strip()
        if not t:
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        frags.append((y, x, t))

    page.extract_text(visitor_text=visitor)
    frags.sort(key=lambda f: (-f[0], f[1]))
    lines = []
    for y, x, t in frags:
        if lines and abs(lines[-1]["y"] - y) <= LINE_TOL:
            lines[-1]["parts"].append((x, t))
        else:
            lines.append({"y": y, "parts": [(x, t)]})
    for ln in lines:
        ln["parts"].sort()
        ln["text"] = re.sub(r"\s+", " ", " ".join(t for _, t in ln["parts"])).strip()
    return lines


def join_name(parts):
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        out = out[:-1] + part if out.endswith("-") else (out + " " + part if out else part)
    return out


def vote_rows(page):
    """Rows with >= 6 numbers; wrapped names rebuilt by attaching each text-only line to the nearest numbers line."""
    lines = lines_with_positions(page)
    rows, texts = [], []
    for ln in lines:
        m = NUM_ROW.match(ln["text"])
        if m:
            name = (m.group("name") or "").strip()
            values = m.group("nums").split()
            if name and re.fullmatch(NUM + r"(?:\s+" + NUM + r")*", name):   # line is all numbers, no name text
                values, name = name.split() + values, ""
            rows.append({"y": ln["y"], "name_part": name, "values": values, "frags": []})
        elif ln["text"]:
            texts.append(ln)
    for t in texts:
        if not rows:
            break
        nearest = min(rows, key=lambda r: abs(r["y"] - t["y"]))
        if abs(nearest["y"] - t["y"]) <= MAX_DY:
            nearest["frags"].append((t["y"], t["text"]))
    out = []
    for r in rows:
        above = [txt for y, txt in sorted(r["frags"], key=lambda f: -f[0]) if y > r["y"]]
        below = [txt for y, txt in sorted(r["frags"], key=lambda f: -f[0]) if y <= r["y"]]
        out.append({"name": join_name(above + [r["name_part"]] + below), "values": r["values"],
                    "n_values": len(r["values"]), "fragments_above": above, "name_on_numbers_line": r["name_part"],
                    "fragments_below": below})
    return out


def profile(path):
    reader = PdfReader(path)
    titles, dumps, row_reads = [], {}, {}
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
            try:
                rr = vote_rows(page)
                if len(rr) >= 3:
                    row_reads[str(i)] = rr
            except Exception as e:  # noqa: BLE001
                row_reads[str(i)] = [{"error": f"{type(e).__name__}: {str(e)[:150]}"}]
    return {"pages": len(reader.pages), "table_titles": titles, "page_dumps": dumps, "row_reads": row_reads}


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
