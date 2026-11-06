#!/usr/bin/env python3
"""Rebuild the procurement summary tables in D1 (atomic: delete + insert in one batch).

Run after every ocds_load.py load. Needs migration 0004 applied first.
Reads the raw tables (a few tens of thousands of rows) and writes a few thousand summary rows.
Joint awards: awards has one row per (award, supplier); in 2026 each award has at most one
supplier so values are not double counted. Re-check this when loading older years.
"""
import datetime
import json
import os
import sys

# Award value counted in totals unless flagged extreme.
VALUE_OK = ("SUM(CASE WHEN COALESCE(quality_flags,'') NOT LIKE '%value_extreme%' "
            "THEN COALESCE(value_kes,0) ELSE 0 END)")
AWARD_AGG = (f"(SELECT ocid, COUNT(*) AS n, {VALUE_OK} AS value_ok, "
             "SUM(CASE WHEN quality_flags IS NOT NULL THEN 1 ELSE 0 END) AS flagged FROM awards GROUP BY ocid)")


def summary_statements(now):
    n = "'" + now + "'"
    return [
        "DELETE FROM sum_buyer_fy;",
        "DELETE FROM sum_supplier_fy;",
        "DELETE FROM sum_fy_overview;",
        "DELETE FROM sum_fy_breakdown;",
        f"""INSERT INTO sum_buyer_fy (buyer_ppip_id, fy_start, buyer_name, buyer_entity_id, tenders, tenders_with_award,
  direct_tenders, award_rows, awarded_value_kes, flagged_awards, updated_at)
SELECT t.buyer_ppip_id, t.fy_start, MAX(t.buyer_name), MAX(t.buyer_entity_id), COUNT(*),
  SUM(CASE WHEN t.award_count > 0 THEN 1 ELSE 0 END),
  SUM(CASE WHEN t.procurement_method = 'direct' THEN 1 ELSE 0 END),
  COALESCE(SUM(aw.n),0), COALESCE(SUM(aw.value_ok),0), COALESCE(SUM(aw.flagged),0), {n}
FROM tenders t LEFT JOIN {AWARD_AGG} aw ON aw.ocid = t.ocid
WHERE t.fy_start IS NOT NULL AND t.buyer_ppip_id IS NOT NULL
GROUP BY t.buyer_ppip_id, t.fy_start;""",
        f"""INSERT INTO sum_supplier_fy (supplier_key, fy_start, name, award_rows, awarded_value_kes, buyers, updated_at)
SELECT a.supplier_key, t.fy_start, MAX(s.name), COUNT(*),
  SUM(CASE WHEN COALESCE(a.quality_flags,'') NOT LIKE '%value_extreme%' THEN COALESCE(a.value_kes,0) ELSE 0 END),
  COUNT(DISTINCT t.buyer_ppip_id), {n}
FROM awards a JOIN tenders t ON t.ocid = a.ocid LEFT JOIN suppliers s ON s.supplier_key = a.supplier_key
WHERE a.supplier_key <> '' AND t.fy_start IS NOT NULL
GROUP BY a.supplier_key, t.fy_start;""",
        f"""INSERT INTO sum_fy_overview (fy_start, tenders, tenders_with_award, award_rows, awarded_value_kes,
  zero_value_awards, extreme_value_awards, direct_tenders, county_buyer_tenders, first_release_date,
  last_release_date, updated_at)
SELECT b.fy_start, SUM(b.tenders), SUM(b.tenders_with_award), SUM(b.award_rows), SUM(b.awarded_value_kes),
  COALESCE((SELECT COUNT(*) FROM awards a JOIN tenders t ON t.ocid=a.ocid WHERE t.fy_start=b.fy_start
            AND COALESCE(a.value_kes,0) <= 0),0),
  COALESCE((SELECT COUNT(*) FROM awards a JOIN tenders t ON t.ocid=a.ocid WHERE t.fy_start=b.fy_start
            AND COALESCE(a.quality_flags,'') LIKE '%value_extreme%'),0),
  SUM(b.direct_tenders),
  SUM(CASE WHEN b.buyer_entity_id IS NOT NULL THEN b.tenders ELSE 0 END),
  (SELECT MIN(release_date) FROM tenders t WHERE t.fy_start=b.fy_start),
  (SELECT MAX(release_date) FROM tenders t WHERE t.fy_start=b.fy_start), {n}
FROM sum_buyer_fy b GROUP BY b.fy_start;""",
        f"""INSERT INTO sum_fy_breakdown (fy_start, dim, value, tenders, tenders_with_award, awarded_value_kes)
SELECT t.fy_start, 'method', COALESCE(t.procurement_method,'unknown'), COUNT(*),
  SUM(CASE WHEN t.award_count > 0 THEN 1 ELSE 0 END), COALESCE(SUM(aw.value_ok),0)
FROM tenders t LEFT JOIN {AWARD_AGG} aw ON aw.ocid = t.ocid WHERE t.fy_start IS NOT NULL
GROUP BY t.fy_start, COALESCE(t.procurement_method,'unknown');""",
        f"""INSERT INTO sum_fy_breakdown (fy_start, dim, value, tenders, tenders_with_award, awarded_value_kes)
SELECT t.fy_start, 'category', COALESCE(t.main_category,'unknown'), COUNT(*),
  SUM(CASE WHEN t.award_count > 0 THEN 1 ELSE 0 END), COALESCE(SUM(aw.value_ok),0)
FROM tenders t LEFT JOIN {AWARD_AGG} aw ON aw.ocid = t.ocid WHERE t.fy_start IS NOT NULL
GROUP BY t.fy_start, COALESCE(t.main_category,'unknown');""",
    ]


def main():
    from ocds_load import D1  # same folder; reuses the REST helper
    if not (os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID")):
        sys.exit("Cloudflare secrets missing from the environment.")
    db = D1()
    have = {r["name"] for r in db.run("SELECT name FROM sqlite_master WHERE type='table'")[0]}
    if "sum_buyer_fy" not in have:
        sys.exit("Summary tables missing. Apply migration 0004: npx wrangler d1 migrations apply pesawatch --remote")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, written = db.run("\n".join(summary_statements(now)))
    today = now[:10]
    db.run("INSERT INTO d1_write_budget (day, rows_written) VALUES (?, ?) "
           "ON CONFLICT(day) DO UPDATE SET rows_written = rows_written + excluded.rows_written", [today, written + 2])
    used = db.run("SELECT rows_written FROM d1_write_budget WHERE day=?", [today])[0][0]["rows_written"]

    lines = ["## Summary rebuild", "", f"- rows written this run: {written} (today total {used}/100000 free limit)"]
    for t in ("sum_buyer_fy", "sum_supplier_fy", "sum_fy_overview", "sum_fy_breakdown"):
        lines.append(f"- {t}: {db.run(f'SELECT COUNT(*) AS n FROM {t}')[0][0]['n']} rows")
    lines += ["", "### Overview", "```", json.dumps(db.run("SELECT * FROM sum_fy_overview ORDER BY fy_start")[0], indent=1), "```",
              "", "### Top 5 counties by tenders (latest FY)", "```",
              json.dumps(db.run("SELECT e.name, SUM(b.tenders) AS tenders, SUM(b.tenders_with_award) AS with_award, "
                                "SUM(b.awarded_value_kes) AS value_kes FROM sum_buyer_fy b JOIN entities e ON e.id=b.buyer_entity_id "
                                "WHERE b.fy_start=(SELECT MAX(fy_start) FROM sum_fy_overview) GROUP BY e.id ORDER BY tenders DESC LIMIT 5")[0], indent=1),
              "```"]
    text = "\n".join(lines)
    print(text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
