#!/usr/bin/env python3
"""Why are ~73% of 2026 PPIP tenders flagged 'period_inverted' (tenderPeriod.startDate > endDate)?

Read-only: downloads the yearly OCDS file, looks only at date fields, writes a summary. No database,
no secrets. Reuses load_year() and d10() from ocds_load.py (same folder).

It tests three explanations:
  H1  startDate is a placeholder (e.g. 30 June / 1 July, the Kenyan financial-year boundary)
  H2  startDate and endDate are swapped in the source (end sits near the release date, start is later)
  H3  the inversion is concentrated in particular buyers, months or procurement methods
"""
import collections
import datetime
import json
import os
import sys

from ocds_load import d10, load_year

YEAR = 2026
SCRIPT_VERSION = "ocds_dates_diag v1"
EXAMPLES = 8


def to_date(s):
    try:
        return datetime.date.fromisoformat(s) if s else None
    except ValueError:
        return None


def bucket(days):
    if days is None:
        return "n/a"
    d = abs(days)
    for limit, label in [(0, "0"), (7, "1-7"), (30, "8-30"), (90, "31-90"), (180, "91-180"), (365, "181-365")]:
        if d <= limit:
            return label
    return ">365"


def pct(n, d):
    return f"{100 * n / d:.1f}%" if d else "n/a"


def analyse(records):
    total = len(records)
    both = inverted = 0
    inv_gap, ok_gap = collections.Counter(), collections.Counter()
    inv_start, inv_end, inv_start_md = collections.Counter(), collections.Counter(), collections.Counter()
    rel_vs = collections.Counter()          # how the release date relates to start/end, inverted records only
    by_month = collections.defaultdict(lambda: [0, 0])   # release month -> [inverted, total]
    by_method = collections.defaultdict(lambda: [0, 0])
    by_buyer = collections.defaultdict(lambda: [0, 0])
    duration_checked = duration_matches_end_minus_start = duration_matches_start_minus_end = 0
    period_keys = collections.Counter()
    tender_date_keys = collections.Counter()
    examples = []

    for r in records:
        t = r.get("tender") or {}
        tp = t.get("tenderPeriod") or {}
        for k in tp:
            period_keys[k] += 1
        for k, v in t.items():
            if isinstance(v, dict) and any("Date" in kk for kk in v):
                tender_date_keys[k] += 1
        rel, start, end = to_date(d10(r.get("date"))), to_date(d10(tp.get("startDate"))), to_date(d10(tp.get("endDate")))
        month = rel.strftime("%Y-%m") if rel else "no-date"
        method = t.get("procurementMethod") or "none"
        buyer = ((r.get("buyer") or {}).get("name") or "none").strip()
        is_inv = bool(start and end and start > end)
        for table, key in ((by_month, month), (by_method, method), (by_buyer, buyer)):
            table[key][1] += 1
            table[key][0] += is_inv
        if not (start and end):
            continue
        both += 1
        dur = tp.get("durationInDays")
        if isinstance(dur, (int, float)):
            duration_checked += 1
            duration_matches_end_minus_start += (end - start).days == dur
            duration_matches_start_minus_end += (start - end).days == dur
        if not is_inv:
            ok_gap[bucket((end - start).days)] += 1
            continue
        inverted += 1
        inv_gap[bucket((start - end).days)] += 1
        inv_start[start.isoformat()] += 1
        inv_end[end.isoformat()] += 1
        inv_start_md[start.strftime("%m-%d")] += 1
        if rel:
            rel_vs["end within 7d of release"] += abs((end - rel).days) <= 7
            rel_vs["start within 7d of release"] += abs((start - rel).days) <= 7
            rel_vs["release between end and start"] += end <= rel <= start
            rel_vs["release before both"] += rel < end
            rel_vs["release after both"] += rel > start
        if len(examples) < EXAMPLES:
            examples.append({"ocid": r.get("ocid"), "release_date": r.get("date"), "tenderPeriod": tp,
                             "method": method, "buyer": buyer[:60]})

    fy_boundary = inv_start_md["06-30"] + inv_start_md["07-01"]
    top_buyers = sorted(((k, v) for k, v in by_buyer.items() if v[1] >= 100), key=lambda kv: -kv[1][1])[:10]
    return {
        "records": total, "with_both_dates": both, "inverted": inverted, "inverted_share": pct(inverted, both),
        "tenderPeriod_keys_seen": dict(period_keys),
        "other_tender_blocks_with_dates": dict(tender_date_keys),
        "durationInDays": {"checked": duration_checked,
                           "equals_end_minus_start": duration_matches_end_minus_start,
                           "equals_start_minus_end": duration_matches_start_minus_end},
        "H1_start_on_FY_boundary_30Jun_or_1Jul": f"{fy_boundary} of {inverted} inverted ({pct(fy_boundary, inverted)})",
        "inverted_top_start_dates": inv_start.most_common(8),
        "inverted_top_end_dates": inv_end.most_common(8),
        "inverted_top_start_month_day": inv_start_md.most_common(8),
        "H2_release_date_vs_period (inverted only)": dict(rel_vs),
        "gap_days_inverted (start minus end)": dict(inv_gap),
        "gap_days_not_inverted (end minus start)": dict(ok_gap),
        "H3_inverted_rate_by_release_month": {k: f"{v[0]}/{v[1]} ({pct(*v)})" for k, v in sorted(by_month.items())},
        "H3_inverted_rate_by_method": {k: f"{v[0]}/{v[1]} ({pct(*v)})" for k, v in by_method.items()},
        "H3_inverted_rate_top_buyers": [(k, f"{v[0]}/{v[1]} ({pct(*v)})") for k, v in top_buyers],
        "examples": examples,
    }


def main():
    records, _, nbytes = load_year(YEAR)
    res = analyse(records)
    res["script_version"] = SCRIPT_VERSION
    res["file_bytes"] = nbytes
    os.makedirs("reports", exist_ok=True)
    with open("reports/ocds_dates_diag.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, default=str)
    lines = [f"## OCDS date diagnostic {YEAR} ({SCRIPT_VERSION})", ""]
    for k, v in res.items():
        if k == "examples":
            continue
        lines.append(f"- **{k}**: {v}")
    lines += ["", "### Raw examples (inverted tenders)"]
    lines += [f"- `{e['ocid']}` release {e['release_date']} period {e['tenderPeriod']} method {e['method']} buyer {e['buyer']}"
              for e in res["examples"]]
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
