#!/usr/bin/env python3
"""Load county budget, exchequer and expenditure figures (FY2025/26) from the Controller of Budget report.

Source tables (county annual report, Chapter 2):
  Table 2.3  approved estimates and EXCHEQUER ISSUES (cash released to each county)
  Table 2.4  expenditure by economic class (compensation, operations & maintenance, development)
  Table 2.5  approved budget, actual expenditure and absorption rate
Everything is stored as published, in whole shillings, with the PDF page on every figure.

Safety gates (abort, no writes, if any fails): all three tables present with 47 counties and a Total row;
columns sum to the Total row; row arithmetic holds; Tables 2.3/2.4/2.5 agree where they overlap.
MODE "dry-run" runs the gates and prints totals without touching D1.
"""
import datetime
import json
import os
import re
import sys
import time

import cob_county_tables as ct
from ocds_load import D1, lit

MODE = "load"          # "dry-run" or "load"
FY_START = 2025
MN = 1_000_000         # source tables are in KSh millions
DOC = {"external_id": "16482", "title": "County Governments Budget Implementation Review Report FY 2025/26 (annual)",
       "url": ct.DOC_URL, "doc_kind": "county_birr", "period": "annual", "listed_size_text": "51.08 MB",
       "original_filename": "CGBIRR FY 2025_26 August 2026 Final 5 (1).pdf"}
CLASS_COMP, CLASS_OM = 101, 102


def pick_tables(tables):
    found = {}
    for t in tables:
        m = re.match(r"Table\s+(\d+\.\d+)", t.get("title_guess") or "")
        if m and m.group(1) in ("2.3", "2.4", "2.5") and m.group(1) not in found:
            found[m.group(1)] = t
    return found


def check(found):
    """Return (ok, messages). Pure function so it can be tested without a PDF."""
    msgs, ok = [], True
    for key, ncols, money_cols in (("2.3", 9, range(6)), ("2.4", 5, range(5)), ("2.5", 9, range(6))):
        t = found.get(key)
        if not t:
            return False, [f"Table {key} not found"]
        v = t["validation"]
        if t["ncols"] != ncols or v["counties_found"] != 47 or not v["has_total_row"] or v["duplicate_codes"]:
            return False, [f"Table {key} shape wrong: cols={t['ncols']} counties={v['counties_found']} total={v['has_total_row']}"]
        worst = max(c.get("diff_pct", 0) for c in v["column_sums"] if c["col"] in money_cols)
        msgs.append(f"Table {key}: 47 counties, worst column-sum difference {worst}%")
        if worst > 0.1:
            ok = False
            msgs.append(f"  FAIL: column sums differ from Total row by {worst}%")
    r23 = {r["county_code"]: r["values"] for r in found["2.3"]["rows"]}
    r24 = {r["county_code"]: r["values"] for r in found["2.4"]["rows"]}
    r25 = {r["county_code"]: r["values"] for r in found["2.5"]["rows"]}
    id_fail = sum(1 for v in r25.values() if abs(v[0] + v[1] - v[2]) > 0.05 or abs(v[3] + v[4] - v[5]) > 0.05)
    cross = sum(1 for k in range(1, 48) if abs(r23[k][2] - r25[k][2]) > 0.05 or abs(r24[k][2] - r25[k][3]) > 0.1
                or abs(r24[k][3] - r25[k][4]) > 0.1)
    msgs.append(f"Table 2.5 row-arithmetic failures: {id_fail}; cross-table disagreements: {cross}")
    if id_fail > 2 or cross > 2:
        ok = False
        msgs.append("  FAIL: too many arithmetic or cross-table failures")
    return ok, msgs


def build_facts(found):
    r23 = {r["county_code"]: r for r in found["2.3"]["rows"]}
    r24 = {r["county_code"]: r for r in found["2.4"]["rows"]}
    r25 = {r["county_code"]: r for r in found["2.5"]["rows"]}
    facts, dash = [], {}
    for code in range(1, 48):
        v3, v4, v5 = r23[code]["values"], r24[code]["values"], r25[code]["values"]
        p3, p4, p5 = r23[code]["page"], r24[code]["page"], r25[code]["page"]
        ok5 = abs(v5[0] + v5[1] - v5[2]) <= 0.05 and abs(v5[3] + v5[4] - v5[5]) <= 0.05
        cross_ok = abs(v3[2] - v5[2]) <= 0.05 and abs(v4[2] - v5[3]) <= 0.1 and abs(v4[3] - v5[4]) <= 0.1
        status5 = "checked" if ok5 and cross_ok else "unreviewed"

        def add(measure, spend, value, ref, status, note=None, cls=0):
            facts.append({"code": code, "measure": measure, "spend_type": spend, "classification_id": cls,
                          "amount_kes": int(round(value * MN)), "page_ref": ref, "review_status": status, "note": note})

        for spend, i in (("recurrent", 0), ("development", 1), ("all", 2)):
            add("approved_budget", spend, v5[i], f"Table 2.5, PDF page {p5}", status5)
        for spend, i in (("recurrent", 3), ("development", 4), ("all", 5)):
            add("reported_expenditure", spend, v5[i], f"Table 2.5, PDF page {p5}", status5)
        same = v3[3:6] == v5[3:6]
        note = ("Exchequer issues equal reported expenditure to the last digit in the source tables; "
                "worth verifying against the report.") if same else None
        for spend, i in (("recurrent", 3), ("development", 4), ("all", 5)):
            add("exchequer_release", spend, v3[i], f"Table 2.3, PDF page {p3}", "checked" if cross_ok else "unreviewed", note)
        comp_ok = abs(v4[0] + v4[1] - v4[2]) <= 0.05
        cnote = None if comp_ok else (f"Published components sum to {round(v4[0] + v4[1], 2)} Mn but the published "
                                      f"recurrent total is {v4[2]} Mn.")
        st4 = "checked" if comp_ok else "unreviewed"
        add("reported_expenditure", "recurrent", v4[0], f"Table 2.4, PDF page {p4}", st4, cnote, CLASS_COMP)
        add("reported_expenditure", "recurrent", v4[1], f"Table 2.4, PDF page {p4}", st4, cnote, CLASS_OM)
        dash[code] = {"approved": v5[2], "exchequer": v3[5], "expenditure": v5[5], "dev_approved": v5[1],
                      "dev_expenditure": v5[4], "page": p5}
    return facts, dash


def statements(facts, dash, doc_id, entity_of, now):
    st = [f"INSERT OR IGNORE INTO classifications (id, scheme, code, name) VALUES ({CLASS_COMP},'economic','compensation','Compensation of employees');",
          f"INSERT OR IGNORE INTO classifications (id, scheme, code, name) VALUES ({CLASS_OM},'economic','operations_maintenance','Operations and maintenance');"]
    for f in facts:
        st.append("INSERT OR IGNORE INTO fiscal_facts (entity_id, fy_start, as_of_period, classification_id, spend_type, measure, "
                  "amount_kes, source_document_id, page_ref, extraction_method, review_status, note) VALUES ("
                  + ",".join(lit(x) for x in (entity_of[f["code"]], FY_START, "annual", f["classification_id"], f["spend_type"],
                                              f["measure"], f["amount_kes"], doc_id, f["page_ref"], "pdf_table",
                                              f["review_status"], f["note"])) + ");")
    for code, d in dash.items():
        st.append("INSERT OR REPLACE INTO dash_entity_fy (entity_id, fy_start, as_of_period, approved_budget, revised_budget, "
                  "exchequer_release, reported_expenditure, dev_approved, dev_expenditure, pending_bills, source_document_id, updated_at) VALUES ("
                  + ",".join(lit(x) for x in (entity_of[code], FY_START, "annual", int(round(d["approved"] * MN)), None,
                                              int(round(d["exchequer"] * MN)), int(round(d["expenditure"] * MN)),
                                              int(round(d["dev_approved"] * MN)), int(round(d["dev_expenditure"] * MN)),
                                              None, doc_id, now)) + ");")
    return st


def chunks(stmts, limit=70_000):
    cur, size = [], 0
    for s in stmts:
        if cur and size + len(s) > limit:
            yield cur
            cur, size = [], 0
        cur.append(s)
        size += len(s)
    if cur:
        yield cur


def main():
    os.makedirs("reports", exist_ok=True)
    path, sha = ct.get_pdf()
    tables, _ = ct.analyse(path)
    found = pick_tables(tables)
    ok, msgs = check(found)
    lines = [f"## COB county budget load ({MODE})", ""] + [f"- {m}" for m in msgs]
    if not ok:
        lines.append("\nGATES FAILED. Nothing was written.")
        print("\n".join(lines))
        _summary(lines)
        sys.exit(1)
    facts, dash = build_facts(found)
    tot = {m: sum(f["amount_kes"] for f in facts if f["measure"] == m and f["spend_type"] == "all" and f["classification_id"] == 0)
           for m in ("approved_budget", "exchequer_release", "reported_expenditure")}
    flagged = sorted({f["code"] for f in facts if f["note"]})
    lines += ["", f"- facts to write: {len(facts)} ({sum(1 for f in facts if f['review_status'] != 'checked')} not passing automatic checks)",
              f"- totals across 47 counties: approved {tot['approved_budget']:,}, exchequer {tot['exchequer_release']:,}, "
              f"expenditure {tot['reported_expenditure']:,} KES",
              f"- counties with a data note: {flagged}"]
    if MODE != "load":
        lines.append("\nDry run only. Set MODE = \"load\" to write.")
        print("\n".join(lines))
        _summary(lines)
        return
    if not (os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID")):
        sys.exit("Load mode needs the Cloudflare secrets.")
    db = D1()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    size = os.path.getsize(path)
    db.run("INSERT OR IGNORE INTO source_documents (source_id, external_id, title, doc_kind, fy_start, period, url, original_filename, "
           "bytes, sha256, status, listed_size_text, retrieved_at) VALUES ('cob', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?, ?)",
           [DOC["external_id"], DOC["title"], DOC["doc_kind"], FY_START, DOC["period"], DOC["url"], DOC["original_filename"],
            size, sha, DOC["listed_size_text"], now])
    doc_id = db.run("SELECT id FROM source_documents WHERE source_id='cob' AND external_id=? AND sha256=?",
                    [DOC["external_id"], sha])[0][0]["id"]
    entity_of = {r["county_code"]: r["id"] for r in db.run("SELECT id, county_code FROM entities WHERE kind='county'")[0]}
    written = 0
    for part in chunks(statements(facts, dash, doc_id, entity_of, now)):
        written += db.run("\n".join(part))[1]
        time.sleep(0.3)
    today = now[:10]
    db.run("INSERT INTO d1_write_budget (day, rows_written) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET rows_written = rows_written + excluded.rows_written",
           [today, written + 2])
    check_rows = db.run("SELECT measure, spend_type, COUNT(*) AS n, SUM(amount_kes) AS total FROM fiscal_facts "
                        "WHERE source_document_id=? AND classification_id=0 AND spend_type='all' GROUP BY measure", [doc_id])[0]
    lines += ["", f"- source document id {doc_id}; rows written this run: {written}", "- totals now in D1 (spend_type=all):"]
    lines += [f"  - {r['measure']}: {r['n']} rows, {r['total']:,} KES" for r in check_rows]
    print("\n".join(lines))
    _summary(lines)


def _summary(lines):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
