#!/usr/bin/env python3
"""Load the national headline series (overall budget performance, CFS, public debt, compensation, pending bills).

Reads the report with cob_national_headlines.extract(); aborts, writing nothing, unless every table passes its own
arithmetic identities. Money is stored in whole shillings (the report prints KSh billions), percentages as printed.
Pending bills: only the ministries/departments/agencies rows and the state-corporation sub-totals are loaded; the
state-corporation line items wait until their wrapped labels are read reliably.
"""
import datetime
import os
import sys

import cob_national_headlines as H
from cob_county_load import chunks, _summary
from cob_national_profile import get_pdf
from ocds_load import D1, lit
from pypdf import PdfReader

MODE = "load"          # "dry-run" or "load"
BN = 1_000_000_000
DOC = {"external_id": "16454", "sha_from_pdf": True}
SECTOR_NAMES = {"ARUD": "Agriculture, Rural and Urban Development", "EIICT": "Energy, Infrastructure and ICT",
                "GECA": "General Economic and Commercial Affairs", "Health": "Health", "Education": "Education",
                "GJLO": "Governance, Justice, Law and Order", "PAIR": "Public Administration and International Relations",
                "National Security*": "National Security", "SPCR": "Social Protection, Culture and Recreation",
                "EPWNR": "Environment Protection, Water and Natural Resources"}


def b(v):
    return None if v is None else int(round(v * BN))


def build(res):
    rows = []

    def add(section, item, fy, period, measure, page, amount=None, pct=None, note=None):
        if amount is None and pct is None:
            return
        rows.append({"section": section, "item": item, "fy_start": fy, "period": period, "measure": measure,
                     "amount_kes": amount, "pct": pct, "page_ref": f"Table, PDF page {page}", "note": note})

    # 3.1 overall budget performance (this year: cols 0-5, last year: cols 6-11)
    for label, v in zip(res["3.1"]["labels"], res["3.1"]["rows"]):
        for fy, period, o in ((2025, "FY2025/26", 0), (2024, "FY2024/25", 6)):
            for k, m in enumerate(("revised_gross", "revised_net", "exchequer", "expenditure")):
                add("budget_performance", label, fy, period, m, 57, amount=b(v[o + k]))
            add("budget_performance", label, fy, period, "exchequer_pct_of_net", 57, pct=v[o + 4])
            add("budget_performance", label, fy, period, "expenditure_pct_of_gross", 57, pct=v[o + 5])
    # 3.2 Consolidated Fund Services
    for label, v in zip(res["3.2"]["labels"], res["3.2"]["rows"]):
        for k, m in enumerate(("revised_net", "exchequer", "expenditure")):
            add("cfs", label, 2025, "FY2025/26", m, 59, amount=b(v[k]))
        add("cfs", label, 2025, "FY2025/26", "expenditure_pct", 59, pct=v[4])
        for k, m in enumerate(("revised_net", "exchequer", "expenditure")):
            add("cfs", label, 2024, "FY2024/25", m, 59, amount=b(v[5 + k]))
    # 3.3 public debt stock
    for label, e in zip(res["3.3"]["external_labels"], res["3.3"]["external"]):
        add("debt_stock", "External: " + label, 2024, "as_of_2025-06-30", "stock", 59, amount=b(e["as_of_2025"]))
        add("debt_stock", "External: " + label, 2025, "as_of_2026-06-30", "stock", 59, amount=b(e["as_of_2026"]), pct=e["pct_change"],
            note="pct is the change over the year; composition share is in the report")
    for label, d in zip(res["3.3"]["domestic_labels"], res["3.3"]["domestic"]):
        add("debt_stock", "Domestic: " + label, 2024, "as_of_2025-06-30", "stock", 60, amount=b(d["as_of_2025"]))
        add("debt_stock", "Domestic: " + label, 2025, "as_of_2026-06-30", "stock", 60, amount=b(d["as_of_2026"]), pct=d["pct_change"])
    t = res["3.3"]["total"]
    add("debt_stock", "Total public debt", 2024, "as_of_2025-06-30", "stock", 60, amount=b(t[0]))
    add("debt_stock", "Total public debt", 2025, "as_of_2026-06-30", "stock", 60, amount=b(t[1]), pct=t[2])
    # 3.4 external debt by currency (already in shillings)
    for r in res["3.4"]["rows"]:
        add("debt_currency", r["currency"], 2025, "as_of_2026-06-30", "stock", 61, amount=int(round(r["amount_kes"])),
            pct=float(r["pct"]) if r["pct"].replace(".", "").isdigit() else None)
    # 3.5 debt service
    for label in ("External", "Domestic", "Total"):
        v = res["3.5"]["rows"][label]
        add("debt_service", label, 2025, "FY2025/26", "revised_principal", 62, amount=b(v[0]))
        add("debt_service", label, 2025, "FY2025/26", "revised_interest_and_other", 62, amount=b(v[1]))
        add("debt_service", label, 2025, "FY2025/26", "revised_total", 62, amount=b(v[2]))
        if label == "Total":
            add("debt_service", label, 2025, "FY2025/26", "exchequer_issues", 62, amount=b(v[3]))
            add("debt_service", label, 2025, "FY2025/26", "expenditure_principal", 62, amount=b(v[4]))
            add("debt_service", label, 2025, "FY2025/26", "expenditure_interest_and_other", 62, amount=b(v[5]))
            add("debt_service", label, 2025, "FY2025/26", "expenditure_total", 62, amount=b(v[6]))
        else:
            add("debt_service", label, 2025, "FY2025/26", "expenditure_principal", 62, amount=b(v[3]))
            add("debt_service", label, 2025, "FY2025/26", "expenditure_interest_and_other", 62, amount=b(v[4]))
            add("debt_service", label, 2025, "FY2025/26", "expenditure_total", 62, amount=b(v[5]))
    # 3.12 compensation by sector
    for r in res["3.12"]["rows"]:
        name = SECTOR_NAMES.get(r["sector"], r["sector"])
        note = "Excludes the National Intelligence Service and the Kenya Defence Forces." if r["sector"].startswith("National") else None
        add("compensation_by_sector", name, 2025, "FY2025/26", "revised_gross", 85, amount=b(r["revised_gross"]), note=note)
        add("compensation_by_sector", name, 2025, "FY2025/26", "expenditure", 85, amount=b(r["expenditure"]), pct=r["pct"], note=note)
    tot = res["3.12"]["total"]
    add("compensation_by_sector", "Total", 2025, "FY2025/26", "revised_gross", 85, amount=b(tot["revised_gross"]))
    add("compensation_by_sector", "Total", 2025, "FY2025/26", "expenditure", 85, amount=b(tot["expenditure"]), pct=tot["pct"])
    # 3.14 pending bills (parts with reliable labels)
    by = {r["label"]: r for r in res["3.14"]["rows"]}
    picks = [("MDAs: Recurrent", by.get("Recurrent")), ("MDAs: Development", by.get("Development")), ("MDAs: Total", by.get("Sub-Total*")),
             ("State corporations: Recurrent", by.get("Sub-total (Recurrent)")),
             ("State corporations: Development", next((r for r in res["3.14"]["rows"] if r["label"].lower().endswith("opment)")), None)),
             ("State corporations: Total", next((r for k, r in by.items() if k.startswith("Sub-Total SCs")), None))]
    for item, r in picks:
        if r:
            add("pending_bills", item, 2024, "as_of_2025-06-30", "amount", 88, amount=r["fy_2025_kes"])
            add("pending_bills", item, 2025, "as_of_2026-06-30", "amount", 88, amount=r["fy_2026_kes"], pct=float(r["change_pct"]),
                note="pct is the change over the year as printed (a fall is printed in brackets or with a sign in the report)")
    return rows


def main():
    os.makedirs("reports", exist_ok=True)
    path, sha = get_pdf()
    reader = PdfReader(path)
    res = H.extract(lambda p: H.get_lines(reader, p))
    bad = {k: v["check"] for k, v in res.items() if not k.startswith("_") and not any(
        x is True for x in [v["check"].get("identities_ok"), v["check"].get("sum_matches_total"), v["check"].get("mda_subtotal_ok")])}
    lines = [f"## National finances load ({MODE})", ""]
    if bad:
        lines.append(f"GATES FAILED for tables {sorted(bad)}: {bad}. Nothing was written.")
        print("\n".join(lines)); _summary(lines); sys.exit(1)
    rows = build(res)
    by_section = {}
    for r in rows:
        by_section[r["section"]] = by_section.get(r["section"], 0) + 1
    lines += [f"- {len(rows)} rows: {by_section}"]
    if MODE != "load":
        lines.append('\nDry run only. Set MODE = "load" to write.')
        print("\n".join(lines)); _summary(lines); return
    if not (os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID")):
        sys.exit("Load mode needs the Cloudflare secrets.")
    db = D1()
    doc = db.run("SELECT id FROM source_documents WHERE source_id='cob' AND external_id='16454' AND sha256=?", [sha])[0]
    if not doc:
        sys.exit("The national report is not registered yet: run cob_national_load.py first.")
    doc_id = doc[0]["id"]
    st = []
    for r in rows:
        st.append("INSERT OR IGNORE INTO national_series (section, item, fy_start, period, measure, amount_kes, pct, source_document_id, page_ref, "
                  "review_status, note) VALUES (" + ",".join(lit(x) for x in (r["section"], r["item"], r["fy_start"], r["period"], r["measure"],
                  r["amount_kes"], r["pct"], doc_id, r["page_ref"], "checked", r["note"])) + ");")
    written = 0
    for part in chunks(st):
        written += db.run("\n".join(part))[1]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    db.run("INSERT INTO d1_write_budget (day, rows_written) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET rows_written = rows_written + excluded.rows_written", [now, written + 2])
    n = db.run("SELECT section, COUNT(*) AS n FROM national_series GROUP BY section")[0]
    lines += [f"- rows written: {written}"] + [f"  - {r['section']}: {r['n']}" for r in n]
    print("\n".join(lines)); _summary(lines)


if __name__ == "__main__":
    main()
