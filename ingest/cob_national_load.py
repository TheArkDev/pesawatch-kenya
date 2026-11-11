#!/usr/bin/env python3
"""Load national government votes (FY2025/26): revised budget, exchequer issues and expenditure per vote.

Source: the sector analyses in the Controller of Budget's national report (ten sector tables, about 85 votes), read
by cob_national_votes.py. For each vote and for development and recurrent separately we store:
  revised_budget (revised gross estimate), exchequer_release (exchequer issues), reported_expenditure
in whole shillings (the report gives KSh billions to 2 decimals). Sector = classification 401-410.
Every table is checked against its own Total row and sector summary; a figure whose column does not add up, or
whose row fails its own percentage check, is stored as 'unreviewed' with a note saying why. Nothing is guessed.
Gates (abort, no writes): all ten sector tables found, each with a Total row or sector summary to check against.
"""
import datetime
import os
import re
import sys
import time

import cob_national_votes as nv
from cob_county_load import chunks, _summary
from cob_national_profile import get_pdf
from ocds_load import D1, lit

MODE = "load"          # "dry-run" or "load"
FY_START = 2025
BN = 1_000_000_000
DOC = {"external_id": "16454", "title": "National Government Budget Implementation Review Report FY 2025/26 (annual)",
       "url": nv.__dict__.get("URL", "https://cob.go.ke/download/national-government-budget-implementation-review-report-fy-2025-2026/?wpdmdl=16454"),
       "doc_kind": "national_birr", "period": "annual", "listed_size_text": "13.19 MB",
       "original_filename": "NGBIRR Report August 2026.pdf"}
SECTORS = {93: (401, "Agriculture, Rural and Urban Development"), 112: (402, "Education"),
           130: (403, "Energy, Infrastructure and ICT"), 160: (404, "Environment Protection, Water and Natural Resources"),
           180: (405, "General Economic and Commercial Affairs"), 208: (406, "Governance, Justice, Law and Order"),
           243: (407, "Health"), 254: (408, "National Security"),
           261: (409, "Public Administration and International Relations"),
           323: (410, "Social Protection, Culture and Recreation")}
# (column index in the 12-value row, spend type, measure, label)
COLUMNS = [(0, "development", "revised_budget", "revised gross estimate"), (2, "development", "exchequer_release", "exchequer issues"),
           (3, "development", "reported_expenditure", "expenditure"), (6, "recurrent", "revised_budget", "revised gross estimate"),
           (8, "recurrent", "exchequer_release", "exchequer issues"), (9, "recurrent", "reported_expenditure", "expenditure")]
MONEY_COLS = (0, 1, 2, 3, 6, 7, 8, 9)
DECIMAL_COMMA = re.compile(r"^\d{1,3},\d{1,2}$")


def entity_kind(name):
    if name.startswith("State Department"):
        return "state_department"
    if name.startswith("Ministry of"):
        return "ministry"
    return "agency"


def column_report(t):
    """Per money column: (sum of the votes, the reported sector figure, % difference)."""
    votes = [r for r in t["rows"] if r["name"].strip().lower() != "total"]
    total = next((r["values"] for r in t["rows"] if r["name"].strip().lower() == "total"), None)
    summ = t.get("summary", {})
    out = {}
    for j in MONEY_COLS:
        ref = None
        if total is not None and j < len(total):
            ref = nv.to_float(total[j])
        if ref is None:
            half, k = ("development", j) if j < 6 else ("recurrent", j - 6)
            if half in summ and k < len(summ[half]):
                ref = nv.to_float(summ[half][k])
        if ref is None or abs(ref) < 1e-9:
            continue
        s = sum((nv.to_float(r["values"][j]) or 0.0) for r in votes)
        out[j] = (round(s, 2), ref, round(100 * abs(s - ref) / abs(ref), 3))
    return out


def prepare(tables):
    by_page = {t["pages"][0]: t for t in tables}
    msgs, ok = [], True
    missing = [p for p in SECTORS if p not in by_page]
    if missing:
        return None, [f"sector tables not found at pages {missing}"], False
    sectors = []
    for page, (sid, sname) in SECTORS.items():
        t = by_page[page]
        rep = column_report(t)
        if not rep:
            ok = False
            msgs.append(f"{sname}: nothing to check against (no Total row or sector summary)")
            continue
        off = {j: v for j, v in rep.items() if v[2] > 0.5}
        votes = [r for r in t["rows"] if r["name"].strip().lower() != "total"]
        msgs.append(f"{sname}: {len(votes)} votes, worst difference {max(v[2] for v in rep.values())}%"
                    + (f", columns off: { {j: v[2] for j, v in off.items()} }" if off else ""))
        sectors.append({"id": sid, "name": sname, "table": t, "votes": votes, "off": off})
    return sectors, msgs, ok


def build_facts(sectors):
    facts, skipped = [], []
    for sec in sectors:
        for r in sec["votes"]:
            if len(r["values"]) != 12:
                skipped.append((sec["name"], r["name"], len(r["values"])))
                continue
            rc = nv.row_check(r["values"])
            row_msg = None if rc == "ok" else rc
            comma = any(DECIMAL_COMMA.match(v) for v in r["values"])
            for col, spend, measure, label in COLUMNS:
                val = nv.to_float(r["values"][col])
                if val is None:
                    continue
                notes = []
                if row_msg:
                    notes.append(f"Printed percentages do not match the amounts for this vote ({row_msg}).")
                if col in sec["off"] and sec["id"] == 408:
                    notes.append("The Ministry of Defence is not included (the report gives no comparable recurrent figures "
                                 "for it), so the sector total in the report is larger than the votes shown here.")
                elif col in sec["off"]:
                    s, ref, pct = sec["off"][col]
                    notes.append(f"The report's sector total for {spend} {label} ({ref:,.2f} bn) differs from the sum of the "
                                 f"votes ({s:,.2f} bn) by {pct}%.")
                if DECIMAL_COMMA.match(r["values"][col]):
                    notes.append(f"Printed as '{r['values'][col]}' in the report; read as {val}.")
                facts.append({"vote": r["name"].strip(), "sector_id": sec["id"], "spend_type": spend, "measure": measure,
                              "amount_kes": int(round(val * BN)), "page_ref": f"{sec['name']} sector analysis, PDF page {r['page']}",
                              "review_status": "unreviewed" if notes else "checked", "note": " ".join(notes) or None})
            if comma:
                pass
    return facts, skipped


def statements(facts, doc_id, entity_of):
    st = [f"INSERT OR IGNORE INTO classifications (id, scheme, code, name) VALUES ({sid},'sector','sector{sid}',{lit(name)});"
          for sid, name in SECTORS.values()]
    for f in facts:
        st.append("INSERT OR IGNORE INTO fiscal_facts (entity_id, fy_start, as_of_period, classification_id, spend_type, measure, "
                  "amount_kes, source_document_id, page_ref, extraction_method, review_status, note) VALUES ("
                  + ",".join(lit(x) for x in (entity_of[f["vote"]], FY_START, "annual", f["sector_id"], f["spend_type"],
                                              f["measure"], f["amount_kes"], doc_id, f["page_ref"], "pdf_table",
                                              f["review_status"], f["note"])) + ");")
    return st


def main():
    os.makedirs("reports", exist_ok=True)
    path, sha = get_pdf()
    tables, _ = nv.extract_tables(path)
    sectors, msgs, ok = prepare(tables)
    lines = [f"## National votes load ({MODE})", ""] + [f"- {m}" for m in msgs]
    if not ok or sectors is None:
        lines.append("\nGATES FAILED. Nothing was written.")
        print("\n".join(lines)); _summary(lines); sys.exit(1)
    facts, skipped = build_facts(sectors)
    for sec in sectors:
        for x in sec["table"].get("skipped", []):
            nm = x["name"].strip()
            if x["page"] in sec["table"]["pages"] and 7 <= len(x["values"]) <= 11 and nm.lower() not in nv.SUMMARY_NAMES \
                    and not nm.lower().startswith("total"):
                skipped.append((sec["name"], nm[:50], len(x["values"])))
    names = sorted({f["vote"] for f in facts})
    tot = {}
    for f in facts:
        tot[(f["spend_type"], f["measure"])] = tot.get((f["spend_type"], f["measure"]), 0) + f["amount_kes"]
    lines += ["", f"- {len(names)} votes, {len(facts)} figures ({sum(1 for f in facts if f['review_status'] != 'checked')} unreviewed)"]
    lines += [f"- total {k[0]} {k[1]}: {v:,} KES" for k, v in sorted(tot.items())]
    lines += [f"- not loaded (not 12 comparable values): {skipped}"]
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
    written = 0
    ent_sql = [f"INSERT OR IGNORE INTO entities (kind, name) VALUES ({lit(entity_kind(n))},{lit(n)});" for n in names]
    for part in chunks(ent_sql):
        written += db.run("\n".join(part))[1]
    entity_of = {r["name"]: r["id"] for r in db.run("SELECT id, kind, name FROM entities WHERE kind IN "
                                                    "('ministry','state_department','agency')")[0]}
    for part in chunks(statements(facts, doc_id, entity_of)):
        written += db.run("\n".join(part))[1]
        time.sleep(0.3)
    db.run("INSERT INTO d1_write_budget (day, rows_written) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET rows_written = rows_written + excluded.rows_written",
           [now[:10], written + 2])
    chk = db.run("SELECT spend_type, measure, COUNT(*) AS n, SUM(amount_kes) AS total FROM fiscal_facts WHERE source_document_id=? "
                 "AND classification_id BETWEEN 401 AND 410 GROUP BY spend_type, measure", [doc_id])[0]
    lines += ["", f"- rows written this run: {written}", "- now in D1:"] + \
             [f"  - {r['spend_type']} {r['measure']}: {r['n']} rows, {r['total']:,} KES" for r in chk]
    print("\n".join(lines)); _summary(lines)


if __name__ == "__main__":
    main()
