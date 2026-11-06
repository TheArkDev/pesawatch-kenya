#!/usr/bin/env python3
"""PPIP OCDS loader: dry-run first, then a budget-limited, resumable load into D1.

MODE "dry-run": downloads the yearly file(s), transforms every record exactly as the loader
would, and prints statistics (no database access, no secrets needed). It also checks two open
questions about the data: (a) do 'supplier' parties include losing bidders? (b) are supplier ids
per-company or per-bid?
MODE "load": writes tenders, awards and suppliers to D1 through the REST API, in batches,
stopping at DAILY_WRITE_BUDGET rows written per UTC day and resuming next run from a cursor.

Privacy: contact emails, phone numbers and street addresses in the source are NOT stored.
"""
import collections
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata

import requests

# ---------------- SETTINGS (edit these) ----------------
MODE = "load"               # "dry-run" or "load"
YEARS = [2026]              # which yearly OCDS files to process
LIMIT_RECORDS = 300         # 0 = all; 300 = small first real load (set to 0 for the full year)
DAILY_WRITE_BUDGET = 80_000 # D1 free allows 100,000 rows written per UTC day; keep headroom
MAX_REQUEST_CHARS = 70_000  # SQL text per API request
MAX_STATEMENTS = 150
SCRIPT_VERSION = "ocds_load v2"
# -------------------------------------------------------

URL = "https://data.open-contracting.org/en/publication/147/download?name={year}.jsonl.gz"
HEADERS = {"User-Agent": "PesaWatchKenya-ocds-load/0.1 (public-finance transparency project)"}
API = "https://api.cloudflare.com/client/v4"
EXTREME_KES = 10_000_000_000  # flag, never delete

COUNTIES = ["Mombasa", "Kwale", "Kilifi", "Tana River", "Lamu", "Taita-Taveta", "Garissa", "Wajir", "Mandera",
            "Marsabit", "Isiolo", "Meru", "Tharaka-Nithi", "Embu", "Kitui", "Machakos", "Makueni", "Nyandarua",
            "Nyeri", "Kirinyaga", "Murang'a", "Kiambu", "Turkana", "West Pokot", "Samburu", "Trans Nzoia",
            "Uasin Gishu", "Elgeyo-Marakwet", "Nandi", "Baringo", "Laikipia", "Nakuru", "Narok", "Kajiado",
            "Kericho", "Bomet", "Kakamega", "Vihiga", "Bungoma", "Busia", "Siaya", "Kisumu", "Homa Bay",
            "Migori", "Kisii", "Nyamira", "Nairobi City"]
ALNUM = lambda s: re.sub(r"[^a-z0-9]", "", s.casefold())  # noqa: E731
COUNTY_KEY = {ALNUM(n): i + 1 for i, n in enumerate(COUNTIES)}
COUNTY_KEY["nairobi"] = 47


# ---------------------------------------------------------------- transform
def norm_name(name):
    s = unicodedata.normalize("NFKD", name or "").casefold().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    while True:
        new = re.sub(r"\s+(limited|ltd)$", "", s)
        if new == s:
            return s
        s = new


def match_county(buyer_name):
    n = (buyer_name or "").strip()
    if "county government" not in n.casefold():
        return None
    core = re.sub(r"(?i)\bcounty government( of)?\b", " ", n)
    return COUNTY_KEY.get(ALNUM(core))


def d10(v):
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def fy_of(date_str):
    if not date_str:
        return None
    y, m = int(date_str[:4]), int(date_str[5:7])
    return y if m >= 7 else y - 1


def clip(s, n=500):
    return s[:n] if isinstance(s, str) else s


def transform(r):
    buyer = r.get("buyer") or {}
    t = r.get("tender") or {}
    tp = t.get("tenderPeriod") or {}
    rel, start, end = d10(r.get("date")), d10(tp.get("startDate")), d10(tp.get("endDate"))
    flags = []
    # Dry run showed 73% of 2026 tenders have a start date AFTER the end date (placeholder dates),
    # so tender_start is treated as unreliable and left empty in that case. No per-row flag.
    if start and end and start > end:
        start = None
    if not t.get("mainProcurementCategory"):
        flags.append("no_category")
    parties = r.get("parties") or []
    supplier_party_ids = {p.get("id") for p in parties if "supplier" in (p.get("roles") or [])}
    contracts = {c.get("awardID"): c for c in (r.get("contracts") or [])}

    awards, suppliers = [], {}
    for a in (r.get("awards") or []):
        c = contracts.get(a.get("id")) or {}
        period = c.get("period") or a.get("contractPeriod") or {}
        amount = (a.get("value") or {}).get("amount")
        value = int(round(amount)) if isinstance(amount, (int, float)) else None
        for s in (a.get("suppliers") or [None]):
            name = ((s or {}).get("name") or "").strip()
            key = norm_name(name)
            af = []
            if value is not None and value <= 0:
                af.append("value_zero")
            if value is not None and value > EXTREME_KES:
                af.append("value_extreme")
            if not key:
                af.append("no_supplier")
            if key and key not in suppliers:
                suppliers[key] = name
            awards.append({"ocid": r["ocid"], "award_id": str(a.get("id")), "supplier_key": key,
                           "supplier_ppip_id": str((s or {}).get("id") or "") or None, "value_kes": value,
                           "contract_start": d10(period.get("startDate")), "contract_end": d10(period.get("endDate")),
                           "date_signed": d10(c.get("dateSigned")), "quality_flags": ",".join(af) or None})
    try:
        buyer_id = int(buyer.get("id"))
    except (TypeError, ValueError):
        buyer_id = None
    tender = {"ocid": r["ocid"], "buyer_ppip_id": buyer_id, "buyer_name": (buyer.get("name") or "").strip() or None,
              "county_code": match_county(buyer.get("name")), "title": clip(t.get("title")),
              "procurement_method": t.get("procurementMethod"), "main_category": t.get("mainProcurementCategory"),
              "fy_start": fy_of(rel or start), "release_date": rel, "tender_start": start, "tender_end": end,
              "award_count": len(r.get("awards") or []), "bidder_count": len(supplier_party_ids) or None,
              "quality_flags": ",".join(flags) or None}
    return tender, awards, suppliers, supplier_party_ids


# ---------------------------------------------------------------- SQL building
def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("\x00", "").replace("'", "''") + "'"


def insert(table, row):
    cols = ",".join(row)
    vals = ",".join(lit(v) for v in row.values())
    return f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({vals});"


def record_statements(tender, awards, suppliers, doc_id, county_entity):
    stmts = [insert("suppliers", {"supplier_key": k, "name": clip(n, 300), "first_seen_ocid": tender["ocid"]})
             for k, n in suppliers.items()]
    t = {k: v for k, v in tender.items() if k != "county_code"}
    t["buyer_entity_id"] = county_entity.get(tender["county_code"])
    t["source_document_id"] = doc_id
    order = ["ocid", "buyer_ppip_id", "buyer_name", "buyer_entity_id", "title", "procurement_method", "main_category",
             "fy_start", "release_date", "tender_start", "tender_end", "award_count", "bidder_count",
             "source_document_id", "quality_flags"]
    stmts.append(insert("tenders", {k: t[k] for k in order}))
    stmts += [insert("awards", a) for a in awards]
    est_writes = len(suppliers) + 1 + 2 * len(awards)
    return stmts, est_writes


# ---------------------------------------------------------------- D1 REST helpers
def d1(method, path, **kw):
    r = requests.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"},
                         timeout=(10, 120), **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"success": False, "errors": [{"message": r.text[:200]}]}
    if r.status_code >= 400 or not body.get("success"):
        errs = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in body.get("errors", []))
        raise RuntimeError(f"HTTP {r.status_code} {path.split('?')[0]} -> {errs}")
    return body


class D1:
    def __init__(self):
        acct = os.environ["CLOUDFLARE_ACCOUNT_ID"].strip()
        dbs = d1("GET", f"/accounts/{acct}/d1/database?name=pesawatch")["result"]
        self.path = f"/accounts/{acct}/d1/database/{[d for d in dbs if d['name'] == 'pesawatch'][0]['uuid']}/query"

    def run(self, sql, params=None):
        body = {"sql": sql}
        if params:
            body["params"] = params
        res = d1("POST", self.path, json=body)["result"]
        rows_written = sum((x.get("meta") or {}).get("rows_written", 0) for x in res)
        return res[0].get("results", []) if res else [], rows_written


# ---------------------------------------------------------------- main
def load_year(year):
    r = requests.get(URL.format(year=year), headers=HEADERS, timeout=(15, 300))
    r.raise_for_status()
    sha = hashlib.sha256(r.content).hexdigest()
    records = []
    with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
        for line in gz:
            if line.strip():
                records.append(json.loads(line))
    return records, sha, len(r.content)


def dry_run(year, records):
    c = collections.Counter()
    id_tenders = collections.defaultdict(set)
    key_ids = collections.defaultdict(set)
    raw_names, keys = set(), set()
    bidder_dist = collections.Counter()
    unmatched_counties = collections.Counter()
    est = 0
    for r in records:
        tender, awards, suppliers, sp_ids = transform(r)
        _, w = record_statements(tender, awards, suppliers, 1, {})
        est += w
        c["tenders"] += 1
        c["awards_rows"] += len(awards)
        c["county_buyer_matched"] += tender["county_code"] is not None
        if "county" in (tender["buyer_name"] or "").casefold() and tender["county_code"] is None:
            unmatched_counties[tender["buyer_name"]] += 1
        c["tender_start_null"] += tender["tender_start"] is None
        for f in (tender["quality_flags"] or "").split(","):
            if f:
                c["flag_" + f] += 1
        for a in awards:
            for f in (a["quality_flags"] or "").split(","):
                if f:
                    c["award_flag_" + f] += 1
            if a["supplier_ppip_id"]:
                id_tenders[a["supplier_ppip_id"]].add(a["ocid"])
                key_ids[a["supplier_key"]].add(a["supplier_ppip_id"])
        for k, n in suppliers.items():
            keys.add(k)
            raw_names.add(n)
        if awards:
            c["awarded_tenders"] += 1
            bidder_dist[min(len(sp_ids), 6)] += 1
            award_ids = {a["supplier_ppip_id"] for a in awards if a["supplier_ppip_id"]}
            c["awarded_supplier_in_parties"] += bool(award_ids) and award_ids <= sp_ids
        elif sp_ids:
            c["unawarded_with_supplier_parties"] += 1
    multi_id = sum(1 for s in id_tenders.values() if len(s) > 1)
    keys_multi_ids = sum(1 for s in key_ids.values() if len(s) > 1)
    return {
        "year": year, "records": len(records), "counters": dict(c), "estimated_rows_written": est,
        "unique_supplier_keys": len(keys), "unique_raw_supplier_names": len(raw_names),
        "supplier_ids_seen": len(id_tenders), "supplier_ids_used_in_more_than_one_tender": multi_id,
        "supplier_keys_with_more_than_one_id": keys_multi_ids, "supplier_keys_total_with_ids": len(key_ids),
        "supplier_party_count_on_awarded_tenders (6 = 6+)": dict(sorted(bidder_dist.items())),
        "top_unmatched_county_like_buyers": unmatched_counties.most_common(10),
    }


def load_mode(year, records, sha, nbytes):
    db = D1()
    cols = {row["name"] for row in db.run("PRAGMA table_info(tenders)")[0]}
    if "bidder_count" not in cols:
        sys.exit("The tenders table is missing or is the old layout. Apply migration 0003 first: "
                 "npx wrangler d1 migrations apply pesawatch --remote")
    county_entity = {code: eid for eid, code in
                     [(row["id"], row["county_code"]) for row in db.run(
                         "SELECT id, county_code FROM entities WHERE kind='county'")[0]]}
    ext = f"{year}.jsonl.gz"
    db.run("INSERT OR IGNORE INTO source_documents (source_id, external_id, title, doc_kind, url, bytes, sha256, "
           "status, retrieved_at) VALUES ('ppip_ocds', ?, ?, 'ocds_bulk', ?, ?, ?, 'available', ?)",
           [ext, f"PPIP OCDS {year}", URL.format(year=year), nbytes, sha,
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")])
    doc_id = db.run("SELECT id FROM source_documents WHERE source_id='ppip_ocds' AND external_id=? AND sha256=?",
                    [ext, sha])[0][0]["id"]
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    used = (db.run("SELECT rows_written FROM d1_write_budget WHERE day=?", [today])[0] or [{"rows_written": 0}])[0]["rows_written"]
    job = f"ocds_{year}"
    cur = db.run("SELECT cursor FROM ingest_state WHERE job_name=?", [job])[0]
    start_idx = 0
    if cur and cur[0]["cursor"] and cur[0]["cursor"].startswith(sha + ":"):
        start_idx = int(cur[0]["cursor"].split(":")[1])
    end_idx = min(len(records), LIMIT_RECORDS) if LIMIT_RECORDS else len(records)
    print(f"{year}: doc id {doc_id}, resume at {start_idx}/{end_idx}, writes used today {used}/{DAILY_WRITE_BUDGET}", flush=True)

    batch, chars, est, idx = [], 0, 0, start_idx
    written_total = 0

    def flush(next_idx, state):
        nonlocal batch, chars, est, used, written_total
        if not batch:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        sql = "\n".join(batch) + "\n" + (
            f"INSERT OR REPLACE INTO ingest_state (job_name, cursor, last_run_at, last_status) VALUES "
            f"({lit(job)},{lit(f'{sha}:{next_idx}')},{lit(now)},{lit(state)});")
        _, w = db.run(sql)
        db.run("INSERT INTO d1_write_budget (day, rows_written) VALUES (?, ?) "
               "ON CONFLICT(day) DO UPDATE SET rows_written = rows_written + excluded.rows_written", [today, w + 2])
        used += w + 2
        written_total += w
        print(f"  cursor {next_idx}/{end_idx}  rows written this request {w}  today {used}", flush=True)
        batch, chars, est = [], 0, 0
        time.sleep(0.3)

    stopped_for_budget = False
    for idx in range(start_idx, end_idx):
        tender, awards, suppliers, _ = transform(records[idx])
        stmts, w = record_statements(tender, awards, suppliers, doc_id, county_entity)
        size = sum(len(s) for s in stmts)
        if batch and (chars + size > MAX_REQUEST_CHARS or len(batch) + len(stmts) > MAX_STATEMENTS):
            flush(idx, "partial")
        if used + est + w > DAILY_WRITE_BUDGET:
            stopped_for_budget = True
            flush(idx, "paused: daily write budget reached")
            break
        batch += stmts
        chars += size
        est += w
    else:
        flush(end_idx, "complete" if end_idx == len(records) else "partial (limit)")
    totals = db.run("SELECT (SELECT COUNT(*) FROM tenders) AS tenders, (SELECT COUNT(*) FROM awards) AS awards, "
                    "(SELECT COUNT(*) FROM suppliers) AS suppliers, "
                    "(SELECT COUNT(*) FROM tenders WHERE buyer_entity_id IS NOT NULL) AS county_tenders")[0][0]
    return {"year": year, "rows_written_this_run": written_total, "written_today": used,
            "stopped_for_budget": stopped_for_budget, "table_totals_in_d1": totals}


def main():
    os.makedirs("reports", exist_ok=True)
    out = {"script_version": SCRIPT_VERSION, "mode": MODE, "years": {}}
    lines = [f"## OCDS {MODE} ({SCRIPT_VERSION})", ""]
    if MODE == "load" and not (os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID")):
        sys.exit("Load mode needs the Cloudflare secrets in the environment.")
    for year in YEARS:
        records, sha, nbytes = load_year(year)
        if MODE == "dry-run":
            res = dry_run(year, records)
            out["years"][year] = res
            lines.append(f"### {year}")
            for k, v in res.items():
                lines.append(f"- {k}: {v}")
        else:
            res = load_mode(year, records, sha, nbytes)
            out["years"][year] = res
            lines.append(f"### {year}: {res}")
    with open("reports/ocds_load_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    text = "\n".join(lines)
    print("\n" + text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
