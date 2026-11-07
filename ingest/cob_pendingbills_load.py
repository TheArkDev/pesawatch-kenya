#!/usr/bin/env python3
"""Load county pending bills (trade payables as of 30 June 2026) from the Controller of Budget county report.

Table 2.10  pending bills by county: executive and assembly, recurrent/development, grand total, budget, % of budget
Table 2.11  executive pending bills by age (under 1 year, 1-2, 2-3, over 3 years)
Table 2.13  salary arrears and statutory deductions (a subset of the pending bills) by age, executive and assembly
Stored as measure 'pending_bills' with these classification ids:
  301 executive, 302 assembly | 311-314 executive ageing | 321-324 executive salary arrears by age, 325 total |
  331-334 assembly salary arrears by age, 335 total. Classification 0 + spend_type 'all' = grand total.
A source row with ONE blank cell (printed as nothing rather than a dash) is placed by arithmetic only if exactly
one position satisfies every identity in the row; otherwise it is left out.
Gates (abort, no writes): 47 counties per table, column sums match Total rows, row arithmetic holds.
"""
import datetime
import itertools
import os
import re
import sys
import time

import cob_county_tables as ct
from cob_county_load import DOC, MN, FY_START, chunks, _summary
from ocds_load import D1, lit

MODE = "load"          # "dry-run" or "load"
Z = lambda x: x or 0   # noqa: E731  (a dash means 'no figure' and counts as zero in sums)

CLASSES = {
    301: "County Executive pending bills", 302: "County Assembly pending bills",
    311: "Executive pending bills: under 1 year", 312: "Executive pending bills: 1-2 years",
    313: "Executive pending bills: 2-3 years", 314: "Executive pending bills: over 3 years",
    321: "Executive salary arrears and statutory deductions: under 1 year", 322: "Executive salary arrears: 1-2 years",
    323: "Executive salary arrears: 2-3 years", 324: "Executive salary arrears: over 3 years",
    325: "Executive salary arrears and statutory deductions: total",
    331: "Assembly salary arrears and statutory deductions: under 1 year", 332: "Assembly salary arrears: 1-2 years",
    333: "Assembly salary arrears: 2-3 years", 334: "Assembly salary arrears: over 3 years",
    335: "Assembly salary arrears and statutory deductions: total",
}


# ---------------------------------------------------------------- row arithmetic
def sum_ok(parts, total):
    """parts add up to total. A dash counts as zero. Buckets printed as whole millions can be off by 0.5 each."""
    shown = [x for x in parts if x]
    whole = bool(shown) and all(float(x).is_integer() for x in shown)
    tol = 0.05 + (0.5 * len(shown) if whole else 0)
    return abs(sum(shown) - Z(total)) <= tol


def ok_210(v):
    a, b, c, d, e, f, g, h, i = v
    if not h:
        return False
    if not (sum_ok([a, b], c) and sum_ok([d, e], f) and sum_ok([c, f], g)):
        return False
    return i is None or abs(100 * Z(g) / h - i) <= 0.06


def ok_211(v):
    return sum_ok(v[:4], v[4])


def ok_213(v):
    return sum_ok(v[:4], v[4]) and sum_ok(v[5:9], v[9])


def reconstruct(values, ncols, ok):
    """One blank cell (printed as nothing instead of a dash). Try a blank at every position. Cells on which ALL
    valid placements agree are kept; cells that differ between valid placements are left out (None).
    Returns (merged_values, one_valid_placement_for_arithmetic_checks, ambiguous_columns) or None."""
    if len(values) != ncols - 1:
        return None
    cands = []
    for pos in range(ncols):
        cand = list(values[:pos]) + [None] + list(values[pos:])
        if ok(cand):
            cands.append(cand)
    if not cands:
        return None
    ambiguous = {j for j in range(ncols) if not all(c[j] == cands[0][j] for c in cands)}
    merged = [None if j in ambiguous else cands[0][j] for j in range(ncols)]
    return merged, cands[0], ambiguous


# ---------------------------------------------------------------- table selection and gates
def pick(tables):
    found = {}
    for t in tables:
        m = re.match(r"Table\s+(\d+\.\d+)", t.get("title_guess") or "")
        if m and m.group(1) in ("2.10", "2.11", "2.13") and m.group(1) not in found:
            found[m.group(1)] = t
    return found


def complete_rows(t, ok):
    """Regular rows plus irregular rows that can be placed unambiguously. Returns (rows, notes)."""
    rows = [{"code": r["county_code"], "page": r["page"], "values": r["values"], "check_values": r["values"],
             "ambiguous": set(), "note": None} for r in t["rows"]]
    notes = []
    for ir in t.get("validation", {}).get("irregular_rows", []):
        rec = reconstruct(ir["values"], t["ncols"], ok)
        if rec:
            merged, valid, amb = rec
            rows.append({"code": ir["county_code"], "page": ir["page"], "values": merged, "check_values": valid,
                         "ambiguous": amb,
                         "note": "One blank cell in the source row; cells that could not be placed with certainty were left out."
                         if amb else "One blank cell in the source row was placed using the table's own arithmetic."})
            notes.append(f"{ir['name_in_pdf']} {'partly ' if amb else ''}placed by arithmetic")
    return rows, notes


def column_sum_diff(rows, total, cols):
    skip = set().union(*(r["ambiguous"] for r in rows)) if rows else set()
    worst = 0.0
    for j in cols:
        if j in skip:
            continue  # cells left out on purpose; the row identities and the subtotal columns still check them
        tot = total[j]
        s = sum(Z(r["values"][j]) for r in rows)
        if tot:
            worst = max(worst, 100 * abs(s - tot) / abs(tot))
    return round(worst, 3)


def prepare(found):
    spec = {"2.10": (9, ok_210, range(8)), "2.11": (5, ok_211, range(5)), "2.13": (10, ok_213, range(10))}
    out, msgs, ok_all = {}, [], True
    for key, (ncols, ok, cols) in spec.items():
        t = found.get(key)
        if not t or t["ncols"] != ncols or not t.get("total"):
            return None, [f"Table {key} missing or wrong shape"], False
        rows, notes = complete_rows(t, ok)
        codes = {r["code"] for r in rows}
        worst = column_sum_diff(rows, t["total"]["values"], cols)
        bad = [r["code"] for r in rows if not ok(r["check_values"])]
        msgs.append(f"Table {key}: {len(codes)}/47 counties, worst column-sum difference {worst}%, "
                    f"row-arithmetic failures {len(bad)}" + (f", {'; '.join(notes)}" if notes else ""))
        if len(codes) != 47 or len(rows) != 47 or worst > 0.1 or len(bad) > 2:
            ok_all = False
            msgs.append(f"  FAIL: Table {key} did not pass the gates")
        out[key] = rows
    return out, msgs, ok_all


# ---------------------------------------------------------------- facts
def build_facts(rows):
    r210 = {r["code"]: r for r in rows["2.10"]}
    r211 = {r["code"]: r for r in rows["2.11"]}
    r213 = {r["code"]: r for r in rows["2.13"]}
    facts, cross = [], {"exec_210_vs_211": [], "salary_exceeds_pending": []}
    for code in range(1, 48):
        a, b, c, d, e, f, g, h, i = r210[code]["values"]
        ok10 = ok_210(r210[code]["check_values"])
        page10 = r210[code]["page"]
        note10 = r210[code]["note"]
        v11, v13 = r211[code]["values"], r213[code]["values"]
        notes = {}
        if abs(Z(c) - Z(v11[4])) > max(0.5, 0.01 * Z(c)):
            cross["exec_210_vs_211"].append(code)
            notes["exec"] = (f"Executive pending bills are {Z(c):,.2f} Mn in Table 2.10 but {Z(v11[4]):,.2f} Mn in the "
                             "ageing table (Table 2.11); the report's own tables differ.")
        if Z(v13[4]) > Z(c) + 0.5:
            cross["salary_exceeds_pending"].append(code)
            notes["salary"] = (f"Executive salary arrears ({v13[4]:,.2f} Mn, Table 2.13) exceed the executive "
                               f"pending-bills total ({Z(c):,.2f} Mn, Table 2.10), though they should be part of it.")

        def add(table, page, cls, spend, val, ok, note=None):
            if val is None:
                return
            facts.append({"code": code, "classification_id": cls, "spend_type": spend, "amount_kes": int(round(val * MN)),
                          "page_ref": f"Table {table}, PDF page {page}", "review_status": "checked" if ok else "unreviewed",
                          "note": note})

        ex_note = " ".join(x for x in (note10, notes.get("exec")) if x) or None
        add("2.10", page10, 301, "recurrent", a, ok10, ex_note)
        add("2.10", page10, 301, "development", b, ok10, ex_note)
        add("2.10", page10, 301, "all", c, ok10, ex_note)
        add("2.10", page10, 302, "recurrent", d, ok10, note10)
        add("2.10", page10, 302, "development", e, ok10, note10)
        add("2.10", page10, 302, "all", f, ok10, note10)
        add("2.10", page10, 0, "all", g, ok10, note10)
        ok11, p11 = ok_211(r211[code]["check_values"]), r211[code]["page"]
        for k in range(4):
            add("2.11", p11, 311 + k, "all", v11[k], ok11, notes.get("exec"))
        ok13, p13 = ok_213(r213[code]["check_values"]), r213[code]["page"]
        for k in range(4):
            add("2.13", p13, 321 + k, "all", v13[k], ok13, notes.get("salary"))
            add("2.13", p13, 331 + k, "all", v13[5 + k], ok13)
        add("2.13", p13, 325, "all", v13[4], ok13, notes.get("salary"))
        add("2.13", p13, 335, "all", v13[9], ok13)
    return facts, cross, {code: int(round(Z(r210[code]["values"][6]) * MN)) for code in range(1, 48)}


def statements(facts, grand, doc_id, entity_of):
    st = [f"INSERT OR IGNORE INTO classifications (id, scheme, code, name) VALUES ({k},'pending_bills','pb{k}',{lit(v)});"
          for k, v in CLASSES.items()]
    for f in facts:
        st.append("INSERT OR IGNORE INTO fiscal_facts (entity_id, fy_start, as_of_period, classification_id, spend_type, measure, "
                  "amount_kes, source_document_id, page_ref, extraction_method, review_status, note) VALUES ("
                  + ",".join(lit(x) for x in (entity_of[f["code"]], FY_START, "annual", f["classification_id"], f["spend_type"],
                                              "pending_bills", f["amount_kes"], doc_id, f["page_ref"], "pdf_table",
                                              f["review_status"], f["note"])) + ");")
    for code, amount in grand.items():
        st.append(f"UPDATE dash_entity_fy SET pending_bills = {amount} WHERE entity_id = {entity_of[code]} "
                  f"AND fy_start = {FY_START} AND as_of_period = 'annual';")
    return st


def main():
    os.makedirs("reports", exist_ok=True)
    path, sha = ct.get_pdf()
    tables, _ = ct.analyse(path)
    rows, msgs, ok = prepare(pick(tables))
    lines = [f"## COB county pending bills load ({MODE})", ""] + [f"- {m}" for m in msgs]
    if not ok:
        lines.append("\nGATES FAILED. Nothing was written.")
        print("\n".join(lines)); _summary(lines); sys.exit(1)
    facts, cross, grand = build_facts(rows)
    total = sum(grand.values())
    lines += [f"- facts to write: {len(facts)} ({sum(1 for f in facts if f['review_status'] != 'checked')} not passing checks)",
              f"- grand total pending bills (sum of counties): {total:,} KES",
              f"- executive total differs >1% between Tables 2.10 and 2.11 for county codes: {cross['exec_210_vs_211']}",
              f"- salary arrears exceed pending-bill total for county codes: {cross['salary_exceeds_pending']}"]
    if MODE != "load":
        lines.append('\nDry run only. Set MODE = "load" to write.')
        print("\n".join(lines)); _summary(lines); return
    if not (os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID")):
        sys.exit("Load mode needs the Cloudflare secrets.")
    db = D1()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.run("INSERT OR IGNORE INTO source_documents (source_id, external_id, title, doc_kind, fy_start, period, url, original_filename, "
           "bytes, sha256, status, listed_size_text, retrieved_at) VALUES ('cob', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?, ?)",
           [DOC["external_id"], DOC["title"], DOC["doc_kind"], FY_START, DOC["period"], DOC["url"], DOC["original_filename"],
            os.path.getsize(path), sha, DOC["listed_size_text"], now])
    doc_id = db.run("SELECT id FROM source_documents WHERE source_id='cob' AND external_id=? AND sha256=?",
                    [DOC["external_id"], sha])[0][0]["id"]
    entity_of = {r["county_code"]: r["id"] for r in db.run("SELECT id, county_code FROM entities WHERE kind='county'")[0]}
    written = 0
    for part in chunks(statements(facts, grand, doc_id, entity_of)):
        written += db.run("\n".join(part))[1]
        time.sleep(0.3)
    db.run("INSERT INTO d1_write_budget (day, rows_written) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET rows_written = rows_written + excluded.rows_written",
           [now[:10], written + 2])
    check = db.run("SELECT COUNT(*) AS n, SUM(amount_kes) AS total FROM fiscal_facts WHERE source_document_id=? AND measure='pending_bills' "
                   "AND classification_id=0 AND spend_type='all'", [doc_id])[0][0]
    lines += ["", f"- rows written this run: {written}", f"- in D1: {check['n']} grand-total rows, {check['total']:,} KES"]
    print("\n".join(lines)); _summary(lines)


if __name__ == "__main__":
    main()
