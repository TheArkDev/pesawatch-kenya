#!/usr/bin/env python3
"""Prototype: read the national government's vote-by-vote tables (FY2025/26) using word positions.

Ministry/department names wrap over 2-3 lines with the numbers on the middle line, so text order alone is
unreliable. This reads each word with its position (pdfplumber), finds rows of numbers, then attaches every
name line to the nearest numbers row (names are vertically centred on their numbers). Tables run over several
pages and end with a 'Total' row, so each table is checked by summing its rows against that Total row.
Nothing is written to D1. Output: reports/cob_national_votes.json and the job summary.
"""
import json
import os
import re

import pdfplumber
from pypdf import PdfReader

CACHE = os.path.join("pdf_cache", "national_annual_2025_26.pdf")
PAGE_FROM, PAGE_TO = 90, 340        # chapter 4 sector analyses; widen if tables are missing
PRE_MIN_NUMBER_LINES = 3            # a candidate page has at least this many lines with 10+ numbers
MIN_VALUES = 8                      # numbers needed on a line to count as a data row
ROW_TOL = 3.5                       # points: numbers within this vertical distance share a row
LINE_TOL = 2.5                      # points: name words within this share a name line
MAX_NAME_DY = 3.2                   # name lines farther than this many line-spacings from a row are ignored
NUMW = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?%?|[-\u2013\u2014]")
NUM_TOKEN = re.compile(r"(?<!\S)(?:\(?-?\d[\d,]*(?:\.\d+)?\)?|-)(?!\S)")


def prefilter(path):
    reader = PdfReader(path)
    cands = []
    for i in range(PAGE_FROM, min(PAGE_TO, len(reader.pages)) + 1):
        try:
            text = reader.pages[i - 1].extract_text(extraction_mode="layout") or ""
        except TypeError:
            text = reader.pages[i - 1].extract_text() or ""
        n = sum(1 for ln in text.splitlines() if len(NUM_TOKEN.findall(ln)) >= 10)
        if n >= PRE_MIN_NUMBER_LINES:
            cands.append(i)
    return cands


def to_float(tok):
    t = tok.strip()
    if t in ("-", "\u2013", "\u2014", ""):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()%").replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def join_name(parts):
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        out = out[:-1] + part if out.endswith("-") else (out + " " + part if out else part)
    return out


def split_names(rows, lines):
    """Attach name lines to rows. Names are vertically centred on their numbers line, so for each gap between two
    rows the lines split into 'tail of the row above' and 'head of the row below', with |head - tail| <= 1 for
    every row. Pick the split pattern with the least total vertical distance (ties resolved by geometry)."""
    n = len(rows)
    gaps = [[] for _ in range(n + 1)]
    for ln in lines:
        gaps[sum(1 for r in rows if r["yc"] < ln["yc"])].append(ln)
    for g in gaps:
        g.sort(key=lambda ln: ln["yc"])
    INF = float("inf")
    memo = {}

    def best(j, h):
        """Min cost for rows j..n-1 when row j has h head lines taken from the bottom of gap j."""
        key = (j, h)
        if key in memo:
            return memo[key]
        head = gaps[j][len(gaps[j]) - h:] if h else []
        head_cost = sum(abs(l["yc"] - rows[j]["yc"]) for l in head)
        out = (INF, None)
        for t in (h - 1, h, h + 1):
            if t < 0 or t > len(gaps[j + 1]):
                continue
            tail = gaps[j + 1][:t]
            cost = head_cost + sum(abs(l["yc"] - rows[j]["yc"]) for l in tail)
            # A line ending in a hyphen continues on the next line, so the two must belong to the same row.
            last_text = tail[-1]["text"] if tail else rows[j].get("own_text", "")
            more_follows = (t < len(gaps[j + 1])) or j < n - 1
            if more_follows and last_text.rstrip().endswith("-"):
                cost += 1000.0
            if j == n - 1:
                cand = (cost, [(h, t)])
            else:
                nxt_h = len(gaps[j + 1]) - t
                sub = best(j + 1, nxt_h)
                if sub[0] == INF:
                    continue
                cand = (cost + sub[0], [(h, t)] + sub[1])
            if cand[0] < out[0]:
                out = cand
        memo[key] = out
        return out

    choice = (INF, None)
    for h0 in range(0, len(gaps[0]) + 1):       # extra lines above the first head are header text
        c = best(0, h0)
        if c[0] < choice[0]:
            choice = c
    if choice[1] is None:                       # constraints impossible: fall back to nearest row
        for ln in lines:
            near = min(rows, key=lambda r: abs(r["yc"] - ln["yc"]))
            near["frags"].append((ln["yc"], ln["text"]))
        return False
    for j, (h, t) in enumerate(choice[1]):
        head = gaps[j][len(gaps[j]) - h:] if h else []
        rows[j]["frags"] += [(l["yc"], l["text"]) for l in head]
        rows[j]["frags"] += [(l["yc"], l["text"]) for l in gaps[j + 1][:t]]
    return True


def page_rows(page, diag=None):
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False,
                               extra_attrs=["fontname", "size"])
    for w in words:
        w["yc"] = (w["top"] + w["bottom"]) / 2.0
    nums = sorted((w for w in words if NUMW.fullmatch(w["text"])), key=lambda w: w["yc"])
    clusters = []
    for w in nums:
        if clusters and abs(clusters[-1][-1]["yc"] - w["yc"]) <= ROW_TOL:
            clusters[-1].append(w)
        else:
            clusters.append([w])
    clusters = [c for c in clusters if len(c) >= MIN_VALUES]
    if not clusters:
        return [], None
    # First numeric column: use right edges (numbers are right-aligned, so wide numbers start further left).
    first_right = sorted(min(w["x1"] for w in c) for c in clusters)
    cut = first_right[len(first_right) // 2] - 6
    rows, used, left_edges = [], set(), []
    for c in clusters:
        vals = sorted((w for w in c if w["x1"] >= cut), key=lambda w: w["x0"])
        merged = []
        for w in vals:      # "377.5 0" printed with a hair-space: merge digits that touch
            if merged and w["x0"] - merged[-1]["x1"] < 2.0 and re.fullmatch(r"\d", w["text"]):
                merged[-1] = dict(merged[-1], text=merged[-1]["text"] + w["text"], x1=w["x1"])
            else:
                merged.append(w)
        used |= {id(w) for w in vals}
        left_edges.append(min(w["x0"] for w in vals))
        rows.append({"yc": sum(w["yc"] for w in merged) / len(merged), "values": [m["text"] for m in merged],
                     "frags": []})
    name_right = min(left_edges) - 1
    name_words = [w for w in words if id(w) not in used and w["x1"] <= name_right and not re.fullmatch(r"\d+", w["text"])]
    name_words.sort(key=lambda w: (w["yc"], w["x0"]))
    lines = []
    for w in name_words:
        if lines and abs(lines[-1]["yc"] - w["yc"]) <= LINE_TOL:
            lines[-1]["words"].append(w)
        else:
            lines.append({"yc": w["yc"], "words": [w]})
    for ln in lines:
        ln["text"] = " ".join(w["text"] for w in sorted(ln["words"], key=lambda w: w["x0"]))
        ln["bold"] = any("bold" in (w.get("fontname") or "").lower() for w in ln["words"])
    bold = [ln["text"] for ln in lines if ln["bold"]]
    lines = [ln for ln in lines if not ln["bold"] and not re.match(r"(?i)^(source|note)\b", ln["text"])]
    rest = []
    for ln in lines:                      # name text printed on the numbers line itself
        near = min(rows, key=lambda r: abs(r["yc"] - ln["yc"]))
        if abs(near["yc"] - ln["yc"]) <= LINE_TOL + 1.0:
            near["frags"].append((ln["yc"], ln["text"]))
            near["own_text"] = (near.get("own_text", "") + " " + ln["text"]).strip()
        else:
            rest.append(ln)
    ok = split_names(rows, rest)
    out = []
    for j, r in enumerate(rows):
        frags = [t for _, t in sorted(r["frags"])]       # top to bottom (pdfplumber y grows downward)
        # A name line almost equally close to the row above or below could belong to either: flag for review.
        uncertain = False
        for y, _t in r["frags"]:
            own = abs(y - r["yc"])
            others = [abs(y - rows[k]["yc"]) for k in (j - 1, j + 1) if 0 <= k < len(rows)]
            if own > LINE_TOL + 1.0 and others and abs(own - min(others)) < 1.5:
                uncertain = True
        out.append({"name": join_name(frags), "name_lines": frags, "values": r["values"], "yc": round(r["yc"], 1),
                    "name_uncertain": uncertain})
    if diag is not None:
        diag.update({"first_numeric_column_cut_x": round(cut, 1), "name_column_right_edge": round(name_right, 1),
                     "rows": len(out), "constraint_split_ok": ok, "bold_lines_ignored": bold[:6]})
    return out, cut


def assemble(pages_rows):
    """Join rows across pages; each table ends at a row named 'Total'. Check column sums against it."""
    tables, cur = [], {"rows": [], "pages": []}
    for pg, rows in pages_rows:
        for r in rows:
            r = dict(r, page=pg)
            cur["rows"].append(r)
            if pg not in cur["pages"]:
                cur["pages"].append(pg)
            if re.fullmatch(r"(?i)total", r["name"].strip()):
                tables.append(cur)
                cur = {"rows": [], "pages": []}
    if cur["rows"]:
        cur["open"] = True
        tables.append(cur)
    for t in tables:
        if t.get("open") or len(t["rows"]) < 2:
            t["check"] = {"status": "no Total row found"}
            continue
        body, total = t["rows"][:-1], t["rows"][-1]
        ncols = len(total["values"])
        diffs = []
        for j in range(ncols):
            tv = to_float(total["values"][j])
            col = [to_float(r["values"][j]) for r in body if len(r["values"]) == ncols]
            if tv is None or not col or abs(tv) <= 1e-9:
                continue
            if ncols == 12 and j % 6 in (4, 5):
                continue   # percentage columns of the 12-column vote tables
            s = sum(x or 0 for x in col)
            diffs.append((j, round(100 * abs(s - tv) / abs(tv), 3)))
        bad_len = sum(1 for r in body if len(r["values"]) != ncols)
        t["check"] = {"ncols": ncols, "rows": len(body), "rows_with_other_column_count": bad_len,
                      "worst_sum_diff_pct": max((d for _, d in diffs), default=0.0),
                      "columns_off_by_over_0.5pct": [j for j, d in diffs if d > 0.5]}
    return tables


def main():
    os.makedirs("reports", exist_ok=True)
    if not os.path.exists(CACHE):
        raise SystemExit("National PDF not cached: run cob_national_profile.py first (it downloads it).")
    cands = prefilter(CACHE)
    print(f"{len(cands)} candidate pages between {PAGE_FROM} and {PAGE_TO}: {cands[:40]}", flush=True)
    pages_rows, diags = [], {}
    with pdfplumber.open(CACHE) as pdf:
        for n, pg in enumerate(cands):
            d = {}
            try:
                rows, _ = page_rows(pdf.pages[pg - 1], d)
            except Exception as e:  # noqa: BLE001
                rows, d = [], {"error": f"{type(e).__name__}: {str(e)[:150]}"}
            pages_rows.append((pg, rows))
            if n < 3:
                diags[str(pg)] = d
    tables = assemble(pages_rows)
    res = {"candidate_pages": cands, "diagnostics": diags, "tables": tables}
    with open("reports/cob_national_votes.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    lines = ["## National vote tables (prototype)", "", f"- {len(cands)} candidate pages, {len(tables)} tables assembled", ""]
    for i, t in enumerate(tables, 1):
        c = t["check"]
        lines.append(f"- Table {i}: pages {t['pages']}, {len(t['rows'])} rows, check: {c}")
        lines.append(f"    first names: {[r['name'][:40] for r in t['rows'][:4]]}; "
                     f"names flagged uncertain: {sum(1 for r in t['rows'] if r.get('name_uncertain'))}")
    text = "\n".join(lines)
    print(text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
