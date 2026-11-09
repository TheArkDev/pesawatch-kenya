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
PRE_MIN_NUMBER_LINES = 1            # a candidate page has at least this many lines with 10+ numbers
MIN_VALUES = 8                      # numbers needed on a line to count as a data row
ROW_TOL = 3.5                       # points: numbers within this vertical distance share a row
LINE_TOL = 2.5                      # points: name words within this share a name line
MAX_NAME_DY = 3.2                   # name lines farther than this many line-spacings from a row are ignored
DANGLING = re.compile(r"(?:^|\s)(?:State|The|of|for|and|the|&|on)$")   # a name block cannot end on these words
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


def assign_by_blocks(rows, rest, line_h):
    """Rows of a table have padding, so name lines form blocks separated by a bigger gap than the gap between two
    lines of the same name. If every block holds exactly one numbers row, each block's lines belong to that row.
    Returns False (leaving rows untouched) when the blocks are not clean, so the caller can use the fallback."""
    items = [(r["yc"], None, r) for r in rows] + [(l["yc"], l, None) for l in rest]
    # lines that sit on a numbers line are already attached; use the rows themselves as anchors
    items.sort(key=lambda it: it[0])
    blocks, cur = [], [items[0]]
    for prev, it in zip(items, items[1:]):
        if it[0] - prev[0] > 1.3 * line_h:
            blocks.append(cur)
            cur = []
        cur.append(it)
    blocks.append(cur)
    plan = []
    for b in blocks:
        owners = [r for _, l, r in b if r is not None]
        texts = [l for _, l, r in b if l is not None]
        if len(owners) == 1:
            plan.append((owners[0], texts))
        elif len(owners) == 0:
            continue                      # heading or footer text far from any numbers row
        else:
            return False                  # two rows in one block: padding too small to tell
    # Geometry alone can be wrong: reject block names that read badly, so the constrained search runs instead.
    drafts = []
    for owner, texts in plan:
        lines_ = [t for _, t in sorted(owner["frags"] + [(l["yc"], l["text"]) for l in texts])]
        drafts.append((owner, texts, lines_))
    for k, (owner, texts, lines_) in enumerate(drafts):
        if not lines_:
            continue
        last_row = k == len(drafts) - 1
        if not last_row and (lines_[-1].rstrip().endswith("-") or DANGLING.search(lines_[-1].strip())):
            return False
        if lines_[0][:1].islower():
            return False
    for owner, texts, _lines in drafts:
        owner["frags"] += [(l["yc"], l["text"]) for l in texts]
    return True


def split_names(rows, lines, line_h=10.0):
    """Attach name lines to rows. Names are vertically centred on their numbers line, so for each gap between two
    rows the lines split into 'tail of the row above' and 'head of the row below', with |head - tail| <= 1 for
    every row. Lines above the first row or below the last row that sit close to it belong to it (they are the
    head/tail of its name); farther ones are header or footer text and are dropped."""
    n = len(rows)
    gaps = [[] for _ in range(n + 1)]
    for ln in lines:
        gaps[sum(1 for r in rows if r["yc"] < ln["yc"])].append(ln)
    for g in gaps:
        g.sort(key=lambda ln: ln["yc"])
    reach = MAX_NAME_DY * line_h
    gaps[0] = [l for l in gaps[0] if rows[0]["yc"] - l["yc"] <= reach]
    gaps[n] = [l for l in gaps[n] if l["yc"] - rows[-1]["yc"] <= reach]
    INF = float("inf")

    def solve(strict):
        memo = {}

        def best(j, h):
            key = (j, h)
            if key in memo:
                return memo[key]
            head = gaps[j][len(gaps[j]) - h:] if h else []
            head_cost = sum(abs(l["yc"] - rows[j]["yc"]) for l in head)
            out = (INF, None)
            for t in (h - 1, h, h + 1):
                if t < 0 or t > len(gaps[j + 1]):
                    continue
                if j == n - 1 and strict and t != len(gaps[n]):
                    continue
                tail = gaps[j + 1][:t]
                cost = head_cost + sum(abs(l["yc"] - rows[j]["yc"]) for l in tail)
                # A line ending in a hyphen continues on the next line, so the two must belong to the same row.
                last_text = tail[-1]["text"] if tail else rows[j].get("own_text", "")
                more_follows = (t < len(gaps[j + 1])) or j < n - 1
                if more_follows and (last_text.rstrip().endswith("-") or DANGLING.search(last_text.strip())):
                    cost += 1000.0
                # a block cannot begin with a lowercase continuation ("on Administrative Justice", "partment")
                nxt_first = gaps[j + 1][t]["text"] if t < len(gaps[j + 1]) else (
                    rows[j + 1].get("own_text", "") if j < n - 1 else "")
                if nxt_first and nxt_first[:1].islower():
                    cost += 1000.0
                if j == n - 1:
                    cand = (cost, [(h, t)])
                else:
                    sub = best(j + 1, len(gaps[j + 1]) - t)
                    if sub[0] == INF:
                        continue
                    cand = (cost + sub[0], [(h, t)] + sub[1])
                if cand[0] < out[0]:
                    out = cand
            memo[key] = out
            return out

        starts = [len(gaps[0])] if strict else range(0, len(gaps[0]) + 1)
        choice = (INF, None)
        for h0 in starts:
            c = best(0, h0)
            if c[0] < choice[0]:
                choice = c
        return choice

    choice = solve(True)
    strict_ok = choice[1] is not None
    if not strict_ok:
        choice = solve(False)
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


def row_check(values):
    """Vote rows (12 columns, KSh billions to 2 decimals, whole-number percentages): the printed percentage must be
    possible given amounts that were rounded to the nearest 0.005 and a percentage rounded to the nearest 0.5."""
    if len(values) != 12:
        return "not 12 values"
    v = [to_float(x) for x in values]
    bad = []
    e = 0.005
    for base, label in ((0, "development"), (6, "recurrent")):
        gross, net, exch, exp, p_exch, p_exp = v[base:base + 6]
        for num, den, pr, what in ((exp, gross, p_exp, "expenditure"), (exch, net, p_exch, "exchequer")):
            if num is None or den is None or pr is None or den <= 0:
                continue
            lo = 100 * max(num - e, 0) / (den + e) - 0.5
            hi = 100 * (num + e) / max(den - e, 1e-9) + 0.5
            if not (lo <= pr <= hi):
                bad.append(f"{label} {what}% {round(100 * num / den, 1)} vs printed {pr}")
    return "ok" if not bad else "; ".join(bad)


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
    bold, rest, bold_lines = [], [], []
    spacing = sorted(b["yc"] - a["yc"] for a, b in zip(lines, lines[1:]) if 4 < b["yc"] - a["yc"] < 20)
    line_h = spacing[len(spacing) // 2] if spacing else 10.0
    for ln in lines:                      # name text printed on the numbers line itself (bold 'Total' included)
        near = min(rows, key=lambda r: abs(r["yc"] - ln["yc"]))
        if abs(near["yc"] - ln["yc"]) <= LINE_TOL + 1.0:
            near["frags"].append((ln["yc"], ln["text"]))
            near["own_text"] = (near.get("own_text", "") + " " + ln["text"]).strip()
        elif ln["bold"]:
            bold_lines.append(ln)         # bold text away from the numbers is a heading ... unless it is 'Total'
            bold.append(ln["text"])
        elif not re.match(r"(?i)^(source|note)\b", ln["text"]):
            rest.append(ln)
    for ln in bold_lines:                 # a bold 'Total' sitting a few points off its numbers line
        if re.fullmatch(r"(?i)total", ln["text"].strip()):
            near = min(rows, key=lambda r: abs(r["yc"] - ln["yc"]))
            if not near.get("own_text") and abs(near["yc"] - ln["yc"]) <= 1.6 * line_h:
                near["frags"].append((ln["yc"], ln["text"]))
                near["own_text"] = ln["text"]
    ys = sorted([r["yc"] for r in rows] + [l["yc"] for l in rest])
    deltas = [round(b - a, 1) for a, b in zip(ys, ys[1:])][:30]     # real row padding, for tuning
    ok = assign_by_blocks(rows, rest, line_h)
    method = "gaps"
    if not ok:
        ok = split_names(rows, rest, line_h)
        method = "constrained-nearest"
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
                    "name_uncertain": uncertain and method != "gaps", "row_check": None if re.fullmatch(r"(?i)total", join_name(frags).strip()) else row_check(r["values"])})
    if diag is not None:
        diag.update({"first_numeric_column_cut_x": round(cut, 1), "name_column_right_edge": round(name_right, 1),
                     "rows": len(out), "constraint_split_ok": ok, "name_assignment": method,
                     "bold_lines_ignored": bold[:6], "line_height": round(line_h, 1), "first_y_gaps": deltas})
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
    from cob_national_profile import get_pdf     # uses the cache if present, otherwise downloads (about 15 MB)
    path, _sha = get_pdf()
    cands = list(range(PAGE_FROM, min(PAGE_TO, len(PdfReader(path).pages)) + 1))   # every page; pdfplumber skips empty ones fast
    print(f"{len(cands)} candidate pages between {PAGE_FROM} and {PAGE_TO}: {cands[:40]}", flush=True)
    pages_rows, diags = [], {}
    with pdfplumber.open(path) as pdf:
        for n, pg in enumerate(cands):
            d = {}
            try:
                rows, _ = page_rows(pdf.pages[pg - 1], d)
            except Exception as e:  # noqa: BLE001
                rows, d = [], {"error": f"{type(e).__name__}: {str(e)[:150]}"}
            pages_rows.append((pg, rows))
            if rows and len(diags) < 3:
                diags[str(pg)] = d
    tables = assemble(pages_rows)
    import csv
    with open("reports/vote_names_review.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table", "page", "name_as_read", "name_lines", "uncertain", "values_count", "row_check"])
        for i, t in enumerate(tables, 1):
            for r in t["rows"]:
                w.writerow([i, r["page"], r["name"], " | ".join(r["name_lines"]), r.get("name_uncertain"), len(r["values"]), r.get("row_check")])
    res = {"candidate_pages": cands, "diagnostics": diags, "tables": tables}
    with open("reports/cob_national_votes.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    lines = ["## National vote tables (prototype)", "", f"- {len(cands)} candidate pages, {len(tables)} tables assembled", ""]
    for i, t in enumerate(tables, 1):
        c = t["check"]
        lines.append(f"- Table {i}: pages {t['pages']}, {len(t['rows'])} rows, check: {c}")
        bad_rows = [(r['name'][:30], r['row_check']) for r in t['rows'] if r.get('row_check') not in (None, 'ok')]
        if bad_rows:
            lines.append(f"    rows failing their own percentage check: {bad_rows[:6]}")
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
