#!/usr/bin/env python3
"""Profile the PPIP OCDS bulk files before we write the loader.

We have only seen a few top-level keys of one record. This script downloads yearly OCDS
files from the Open Contracting registry and reports what is REALLY inside:
which fields exist and how often, value ranges and currencies, status/method values,
supplier identifiers, date ranges, duplicate ocids, plus two sample records.
Nothing is written to D1. Outputs: reports/ocds_profile.json and the job summary.
"""
import collections
import gzip
import io
import json
import os
import time

import requests

URL = "https://data.open-contracting.org/en/publication/147/download?name={year}.jsonl.gz"
YEARS = [2026, 2019]          # newest + an older year to spot schema drift
HEADERS = {"User-Agent": "PesaWatchKenya-ocds-profile/0.1 (public-finance transparency project)"}
MAX_SAMPLE_CHARS = 4000


def key_paths(obj, prefix=""):
    """All field paths in a record; list items share the path 'name[]'."""
    paths = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            paths.add(p)
            paths |= key_paths(v, p)
    elif isinstance(obj, list):
        for it in obj:
            paths |= key_paths(it, prefix + "[]")
    return paths


def pct(sorted_vals, q):
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def money_stats(values):
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return {"count": 0}
    return {"count": len(vals), "min": vals[0], "median": pct(vals, 0.5), "p99": pct(vals, 0.99),
            "max": vals[-1], "zero_or_negative": sum(1 for v in vals if v <= 0)}


def top(counter, n=12):
    return counter.most_common(n)


def profile(records):
    n = len(records)
    presence = collections.Counter()
    ocids = collections.Counter()
    tender_vals, award_vals = [], []
    enums = {k: collections.Counter() for k in (
        "tag", "initiationType", "tender.status", "tender.procurementMethod",
        "tender.procurementMethodDetails", "tender.mainProcurementCategory",
        "award.status", "party.roles", "supplier.id.scheme", "currency")}
    n_awards = n_tenderers = n_suppliers = 0
    dates = []
    with_awards = None
    without_awards = None

    for r in records:
        presence.update(key_paths(r))
        ocids[r.get("ocid")] += 1
        for t in (r.get("tag") or []):
            enums["tag"][t] += 1
        enums["initiationType"][r.get("initiationType")] += 1
        if r.get("date"):
            dates.append(r["date"])
        t = r.get("tender") or {}
        enums["tender.status"][t.get("status")] += 1
        enums["tender.procurementMethod"][t.get("procurementMethod")] += 1
        enums["tender.procurementMethodDetails"][t.get("procurementMethodDetails")] += 1
        enums["tender.mainProcurementCategory"][t.get("mainProcurementCategory")] += 1
        v = t.get("value") or {}
        if v:
            tender_vals.append(v.get("amount"))
            enums["currency"][v.get("currency")] += 1
        n_tenderers += len(t.get("tenderers") or [])
        awards = r.get("awards") or []
        n_awards += len(awards)
        for a in awards:
            enums["award.status"][a.get("status")] += 1
            av = a.get("value") or {}
            if av:
                award_vals.append(av.get("amount"))
                enums["currency"][av.get("currency")] += 1
            for s in (a.get("suppliers") or []):
                n_suppliers += 1
                sid = s.get("id")
                enums["supplier.id.scheme"][(s.get("identifier") or {}).get("scheme") or ("has_id" if sid else "none")] += 1
        for p in (r.get("parties") or []):
            enums["party.roles"][",".join(sorted(p.get("roles") or []))] += 1
        if awards and with_awards is None:
            with_awards = r
        if not awards and without_awards is None:
            without_awards = r

    dup = sum(1 for c in ocids.values() if c > 1)
    return {
        "records": n,
        "unique_ocids": len(ocids),
        "ocids_with_multiple_releases": dup,
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "awards_total": n_awards,
        "tenderers_total": n_tenderers,
        "award_suppliers_total": n_suppliers,
        "tender_value": money_stats(tender_vals),
        "award_value": money_stats(award_vals),
        "enums": {k: top(v) for k, v in enums.items()},
        "field_presence_pct": {p: round(100 * c / n, 1) for p, c in sorted(presence.items())},
        "sample_with_awards": (json.dumps(with_awards, ensure_ascii=False)[:MAX_SAMPLE_CHARS]
                               if with_awards else None),
        "sample_without_awards": (json.dumps(without_awards, ensure_ascii=False)[:MAX_SAMPLE_CHARS]
                                  if without_awards else None),
    }


def load_year(year):
    url = URL.format(year=year)
    start = time.monotonic()
    r = requests.get(url, headers=HEADERS, timeout=(15, 300))
    r.raise_for_status()
    records = []
    bad = 0
    with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
        for line in gz:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return records, {"url": url, "compressed_bytes": len(r.content), "bad_lines": bad,
                     "seconds": round(time.monotonic() - start, 1)}


def main():
    os.makedirs("reports", exist_ok=True)
    out = {}
    summary = ["## OCDS profile", ""]
    for year in YEARS:
        print(f"Year {year} ...", flush=True)
        try:
            records, meta = load_year(year)
        except Exception as e:  # noqa: BLE001
            out[year] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            summary.append(f"### {year}: FAILED - {out[year]['error']}")
            continue
        prof = profile(records)
        prof["download"] = meta
        out[year] = prof
        summary += [f"### {year}",
                    f"- records {prof['records']}, unique ocids {prof['unique_ocids']}, "
                    f"dates {prof['date_min']} to {prof['date_max']}, file {meta['compressed_bytes']} bytes",
                    f"- awards {prof['awards_total']}, tenderers {prof['tenderers_total']}, "
                    f"award suppliers {prof['award_suppliers_total']}",
                    f"- tender value: {prof['tender_value']}",
                    f"- award value: {prof['award_value']}"]
        for k in ("tender.status", "tender.procurementMethod", "tender.mainProcurementCategory",
                  "award.status", "supplier.id.scheme", "currency", "tag"):
            summary.append(f"- {k}: {prof['enums'][k][:8]}")
        summary.append("")
        pres = prof["field_presence_pct"]
        summary.append("Fields present in <100% of records (top level and tender/awards/buyer):")
        for p, v in pres.items():
            if (p.split(".")[0] in ("tender", "awards", "buyer") or "." not in p) and v < 100:
                summary.append(f"  - {p}: {v}%")
        summary.append("")
        print(f"  {prof['records']} records", flush=True)

    with open("reports/ocds_profile.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    text = "\n".join(summary)
    print("\n" + text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
