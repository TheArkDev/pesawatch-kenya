#!/usr/bin/env python3
"""Load county own-source revenue (FY2025/26) from Table 2.1 of the Controller of Budget county report.

Table 2.1 columns (KSh millions): annual target for ordinary own-source revenue (OSR), facility improvement
financing (FIF) and their total; actual collection in the same three splits; performance percentages.
Stored as measure revenue_target / revenue_actual, classification 111 = ordinary OSR, 112 = FIF, 0 = total.
Gates (abort with no writes): 47 counties + Total row; money columns sum to the Total row; A+B=C and D+E=F per
county; published performance percentages recompute from the amounts.
"""
import datetime
import os
import re
import sys
import time

import cob_county_tables as ct
from cob_county_load import DOC, MN, FY_START, chunks, _summary
from ocds_load import D1, lit

MODE = "load"          # "dry-run" or "load"
CLS_ORDINARY, CLS_FIF = 111, 112


def pick(tables):
    for t in tables:
        if re.match(r"Table\s+2\.1\b", t.get("title_guess") or "") and t["ncols"] == 9:
            return t
    return None


def check(t):
    if not t:
        return False, ["Table 2.1 not found"]
    v = t["validation"]
    if v["counties_found"] != 47 or not v["has_total_row"] or v["duplicate_codes"]:
        return False, [f"Table 2.1 shape wrong: counties={v['counties_found']} total={v['has_total_row']} irregular={len(v.get('irregular_rows', []))}"]
    msgs, ok = [], True
    worst = max(c.get("diff_pct", 0) for c in v["column_sums"] if c["col"] in range(6))
    msgs.append(f"Table 2.1: 47 counties, worst money-column sum difference {worst}%")
    if worst > 0.1:
        ok = False
        msgs.append("  FAIL: column sums differ from the Total row")
    bad = 0
    for r in t["rows"]:
        a, b, c, d, e, f, p1, p2, p3 = r["values"]
        if None in (a, b, c, d, e, f):
            bad += 1
            continue
        if abs(a + b - c) > 0.05 or abs(d + e - f) > 0.05:
            bad += 1
        elif c and p3 is not None and abs(100 * f / c - p3) > 0.06:
            bad += 1
    msgs.append(f"row-arithmetic / percentage failures: {bad}")
    if bad > 2:
        ok = False
        msgs.append("  FAIL: too many row failures")
    return ok, msgs


def build(t):
    facts = []
    for r in t["rows"]:
        v = r["values"]
        a, b, c, d, e, f, p1, p2, p3 = v
        row_ok = None not in (a, b, c, d, e, f) and abs(a + b - c) <= 0.05 and abs(d + e - f) <= 0.05 \
            and (not c or p3 is None or abs(100 * f / c - p3) <= 0.06)
        status = "checked" if row_ok else "unreviewed"
        ref = f"Table 2.1, PDF page {r['page']}"
        for measure, vals in (("revenue_target", (a, b, c)), ("revenue_actual", (d, e, f))):
            for cls, val in zip((CLS_ORDINARY, CLS_FIF, 0), vals):
                if val is None:
                    continue
                facts.append({"code": r["county_code"], "measure": measure, "classification_id": cls,
                              "amount_kes": int(round(val * MN)), "page_ref": ref, "review_status": status})
    return facts


def statements(facts, doc_id, entity_of):
    st = [f"INSERT OR IGNORE INTO classifications (id, scheme, code, name) VALUES ({CLS_ORDINARY},'revenue','ordinary_osr','Ordinary own-source revenue');",
          f"INSERT OR IGNORE INTO classifications (id, scheme, code, name) VALUES ({CLS_FIF},'revenue','fif','Facility improvement financing (FIF)');"]
    for f in facts:
        st.append("INSERT OR IGNORE INTO fiscal_facts (entity_id, fy_start, as_of_period, classification_id, spend_type, measure, "
                  "amount_kes, source_document_id, page_ref, extraction_method, review_status, note) VALUES ("
                  + ",".join(lit(x) for x in (entity_of[f["code"]], FY_START, "annual", f["classification_id"], "all", f["measure"],
                                              f["amount_kes"], doc_id, f["page_ref"], "pdf_table", f["review_status"], None)) + ");")
    return st


def main():
    os.makedirs("reports", exist_ok=True)
    path, sha = ct.get_pdf()
    tables, _ = ct.analyse(path)
    t = pick(tables)
    ok, msgs = check(t)
    lines = [f"## COB county revenue load ({MODE})", ""] + [f"- {m}" for m in msgs]
    if not ok:
        lines.append("\nGATES FAILED. Nothing was written.")
        print("\n".join(lines)); _summary(lines); sys.exit(1)
    facts = build(t)
    tot = {m: sum(f["amount_kes"] for f in facts if f["measure"] == m and f["classification_id"] == 0)
           for m in ("revenue_target", "revenue_actual")}
    lines += [f"- facts to write: {len(facts)} ({sum(1 for f in facts if f['review_status'] != 'checked')} not passing checks)",
              f"- totals: target {tot['revenue_target']:,} KES, actual {tot['revenue_actual']:,} KES"]
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
    for part in chunks(statements(facts, doc_id, entity_of)):
        written += db.run("\n".join(part))[1]
        time.sleep(0.3)
    db.run("INSERT INTO d1_write_budget (day, rows_written) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET rows_written = rows_written + excluded.rows_written",
           [now[:10], written + 2])
    rows = db.run("SELECT measure, COUNT(*) AS n, SUM(amount_kes) AS total FROM fiscal_facts WHERE source_document_id=? AND classification_id=0 "
                  "AND measure IN ('revenue_target','revenue_actual') GROUP BY measure", [doc_id])[0]
    lines += ["", f"- rows written this run: {written}"] + [f"  - {r['measure']}: {r['n']} rows, {r['total']:,} KES" for r in rows]
    print("\n".join(lines)); _summary(lines)


if __name__ == "__main__":
    main()
