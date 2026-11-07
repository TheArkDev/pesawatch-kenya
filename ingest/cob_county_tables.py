#!/usr/bin/env python3
"""Find and parse every county-by-county table in a Controller of Budget county report.

From the profile we know Chapter 2 holds tables with one row per county (Tana River ... West Pokot,
then a Total row), and the PDFs have a text layer. This script:
  - downloads the report once (cached in ./pdf_cache between workflow runs),
  - reads the first PAGE_LIMIT pages in 'layout' mode,
  - finds county rows (a county name followed by numbers), handles names wrapped over two lines,
  - splits them into tables (new table when the column count changes or a Total row ends one),
  - validates: 47 counties present? column sums match the Total row?
Nothing is written to D1 yet. Output: reports/cob_county_tables.json and the job summary.
"""
import hashlib
import json
import os
import re
import time

import requests
from pypdf import PdfReader

from ocds_load import ALNUM, COUNTY_KEY, COUNTIES

HEADERS = {"User-Agent": "PesaWatchKenya-cob-tables/0.1 (public-finance transparency project)"}
DOC_LABEL = "county_annual_2025_26"
DOC_URL = ("https://cob.go.ke/download/county-governments-budget-implementation-review-report-for-the-"
           "financial-year-2025-26/?wpdmdl=16482")
PAGE_LIMIT = 150        # chapter 2 summary tables sit near the front
CACHE_DIR = "pdf_cache"

NUMTOK = r"(?:-?\d[\d,]*(?:\.\d+)?|[-\u2013\u2014])"
# Footnote markers such as * or a dagger can follow a county name (e.g. "Murang\u2019a*"); the first run lost
# those rows (14 counties missing from the payables tables), so allow and ignore them.
MARK = r"[*\u2020\u2021\u00a7\u00b9\u00b2\u00b3]*"
ROW_RE = re.compile(r"^\s*(?P<name>[A-Za-z][A-Za-z'\u2019\-\./ ]*?)\s*" + MARK + r"\s+(?P<nums>" + NUMTOK + r"(?:\s+" + NUMTOK + r")*)\s*$")
FRAG_RE = re.compile(r"^\s*[A-Za-z][A-Za-z'\u2019\-\./ ]{0,30}\s*$")
TITLE_RE = re.compile(r"Table\s+\d+(?:\.\d+)*\s*[:.]?\s*[^\n]{3,140}")


def get_pdf():
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, DOC_LABEL + ".pdf")
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        print(f"Using cached {path}", flush=True)
    else:
        print("Downloading report (about 51 MB, several minutes)...", flush=True)
        with requests.get(DOC_URL, headers=HEADERS, timeout=(15, 600), stream=True) as r:
            r.raise_for_status()
            with open(path + ".part", "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    f.write(chunk)
        os.replace(path + ".part", path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return path, h.hexdigest()


def page_text(page):
    try:
        return page.extract_text(extraction_mode="layout") or ""
    except TypeError:
        return page.extract_text() or ""


def to_numbers(nums_str):
    out = []
    for tok in re.findall(NUMTOK, nums_str):
        out.append(None if tok in "-\u2013\u2014" else float(tok.replace(",", "")))
    return out


def resolve_county(name, frag):
    """County code (1-47) for a row name, trying the wrapped-name join if needed."""
    for cand in (name, (frag or "") + name if (frag or "").endswith("-") else (frag or "") + " " + name):
        code = COUNTY_KEY.get(ALNUM(cand))
        if code:
            return code
    return None


def parse_pages(pages_text):
    """Yield events in reading order: ('title', page, text) / ('county', page, code, name, nums) / ('total', page, nums)."""
    events = []
    for pno, text in pages_text:
        frag = None
        for line in text.splitlines():
            if not line.strip():
                continue
            t = TITLE_RE.search(line)
            if t:
                events.append(("title", pno, re.sub(r"\s{2,}", " ", t.group(0)).strip()))
            m = ROW_RE.match(line)
            if m:
                name = re.sub(r"\s+", " ", m.group("name")).strip()
                nums = to_numbers(m.group("nums"))
                if name.lower() in ("total", "grand total", "totals") and len(nums) >= 2:
                    events.append(("total", pno, nums))
                else:
                    code = resolve_county(name, frag)
                    if code and len(nums) >= 2:
                        events.append(("county", pno, code, name, nums))
                frag = None
            elif FRAG_RE.match(line) and len(line.strip()) <= 30:
                frag = line.strip()
            else:
                frag = None
                # A line that starts like a table row but did not parse (e.g. columns run together):
                # keep it so we can see it instead of silently losing a county or the Total row.
                head = re.match(r"^\s*([A-Za-z][A-Za-z'\u2019\-\./ ]*?)\s*" + MARK + r"\s*\d", line)
                if head and re.search(r"\d", line):
                    nm = head.group(1).strip()
                    if nm.lower() in ("total", "grand total", "totals") or COUNTY_KEY.get(ALNUM(nm)):
                        events.append(("unparsed", pno, re.sub(r"\s+", " ", line.strip())[:200]))
    return events


def build_tables(events):
    """Group county rows into tables.

    A row with a different number of values than the table (a blank cell instead of a dash) used to close
    the table and silently drop everything after it. Now, if the NEXT county row is back to the table's
    column count, the odd row is kept aside as 'irregular_rows' and the table continues.
    """
    tables, cur, last_title = [], None, None
    county_idx = [i for i, e in enumerate(events) if e[0] == "county"]
    next_county = {a: events[b] for a, b in zip(county_idx, county_idx[1:])}
    for i, ev in enumerate(events):
        kind = ev[0]
        if kind == "title":
            last_title = ev[2]
            continue
        if kind == "unparsed":
            if cur is not None:
                cur.setdefault("unparsed_lines", []).append({"page": ev[1], "line": ev[2]})
            continue
        if kind == "county":
            _, pno, code, name, nums = ev
            if cur and cur["total"] is None and len(nums) != cur["ncols"]:
                nxt = next_county.get(i)
                if nxt and len(nxt[4]) == cur["ncols"] and nxt[1] - pno <= 1:
                    cur.setdefault("irregular_rows", []).append(
                        {"county_code": code, "name_in_pdf": name, "page": pno, "values": nums})
                    continue
            if cur and (len(nums) != cur["ncols"] or pno - cur["pages"][-1] > 1 or cur["total"] is not None):
                tables.append(cur)
                cur = None
            if cur is None:
                cur = {"title_guess": last_title, "pages": [pno], "ncols": len(nums), "rows": [], "total": None}
            if pno not in cur["pages"]:
                cur["pages"].append(pno)
            cur["rows"].append({"county_code": code, "name_in_pdf": name, "page": pno, "values": nums})
        elif kind == "total" and cur and cur["total"] is None and len(ev[2]) == cur["ncols"]:
            cur["total"] = {"page": ev[1], "values": ev[2]}
            if ev[1] not in cur["pages"]:
                cur["pages"].append(ev[1])
    if cur:
        tables.append(cur)
    return tables


def validate(table):
    codes = [r["county_code"] for r in table["rows"]]
    seen = set(codes)
    missing = [COUNTIES[c - 1] for c in range(1, 48) if c not in seen]
    dupes = sorted({c for c in codes if codes.count(c) > 1})
    sums = []
    if table["total"]:
        for j in range(table["ncols"]):
            tot = table["total"]["values"][j]
            col = [r["values"][j] for r in table["rows"] if r["values"][j] is not None]
            if tot is None or not col:
                sums.append({"col": j, "status": "n/a"})
            elif abs(tot) <= 100:
                sums.append({"col": j, "status": "percent-like, skipped"})
            else:
                s = sum(col)
                sums.append({"col": j, "sum": round(s, 2), "total": tot,
                             "diff_pct": round(100 * abs(s - tot) / abs(tot), 3)})
    return {"counties_found": len(seen), "rows": len(codes), "missing_counties": missing,
            "unparsed_lines": table.get("unparsed_lines", []),
            "irregular_rows": table.get("irregular_rows", []),
            "duplicate_codes": dupes, "has_total_row": table["total"] is not None, "column_sums": sums}


def analyse(pdf_path):
    reader = PdfReader(pdf_path)
    n = min(PAGE_LIMIT, len(reader.pages))
    print(f"Reading {n} of {len(reader.pages)} pages...", flush=True)
    pages_text = [(i + 1, page_text(reader.pages[i])) for i in range(n)]
    tables = build_tables(parse_pages(pages_text))
    out = []
    for t in tables:
        if len(t["rows"]) < 10:
            continue  # ignore small incidental matches
        t["validation"] = validate(t)
        first = t["pages"][0]
        head = [ln.strip()[:150] for ln in dict(pages_text)[first].splitlines() if ln.strip()][:14]
        t["page_head_lines"] = head
        out.append(t)
    pt = dict(pages_text)
    for t in out:
        v = t["validation"]
        if 35 <= v["counties_found"] < 47:
            ctx = {}
            for name in v["missing_counties"]:
                key = ALNUM(name)[:5]
                hits = []
                for pg in t["pages"]:
                    for ln in pt[pg].splitlines():
                        if ALNUM(ln).startswith(key):
                            hits.append({"page": pg, "line": re.sub(r"\s+", " ", ln.strip())[:170]})
                ctx[name] = hits[:3]
            v["missing_county_lines"] = ctx
    return out, len(reader.pages)


def main():
    os.makedirs("reports", exist_ok=True)
    t0 = time.monotonic()
    path, sha = get_pdf()
    tables, total_pages = analyse(path)
    res = {"document": DOC_LABEL, "sha256": sha, "total_pages": total_pages, "page_limit": PAGE_LIMIT,
           "seconds": round(time.monotonic() - t0, 1), "tables": tables}
    with open("reports/cob_county_tables.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    lines = ["## COB county tables", "",
             f"- {len(tables)} county-by-county tables found in the first {PAGE_LIMIT} pages (of {total_pages}); sha256 {sha[:12]}", ""]
    for i, t in enumerate(tables, 1):
        v = t["validation"]
        worst = max((c.get("diff_pct", 0) for c in v["column_sums"]), default=0)
        lines.append(f"### Table {i}: pages {t['pages']}, {t['ncols']} numeric columns")
        lines.append(f"- title guess: {t['title_guess']}")
        lines.append(f"- counties {v['counties_found']}/47, rows {v['rows']}, total row: {v['has_total_row']}, "
                     f"missing: {v['missing_counties'] or 'none'}, duplicates: {v['duplicate_codes'] or 'none'}")
        if v.get("irregular_rows"):
            lines.append(f"- IRREGULAR rows kept aside (different number of values): "
                         f"{[(r['name_in_pdf'], r['values']) for r in v['irregular_rows']][:6]}")
        if v.get("missing_county_lines"):
            shown = {k: [h["line"] for h in hits][:1] for k, hits in list(v["missing_county_lines"].items())[:5]}
            lines.append(f"- lines starting with a missing county (first 5): {shown}")
        if v["unparsed_lines"]:
            lines.append(f"- UNPARSED row-like lines: {v['unparsed_lines'][:4]}")
        lines.append(f"- worst column-sum difference vs Total row: {worst}%")
        lines.append(f"- first row: {t['rows'][0]['name_in_pdf']} {t['rows'][0]['values']}")
        lines.append("")
    text = "\n".join(lines)
    print("\n" + text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
