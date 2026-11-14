#!/usr/bin/env python3
"""Mirror GPRIS public project data into D1 (two requests per run).

Fetches /api/projects/stats and /api/projects (the same calls the public pages make), 5 seconds apart. GPRIS lists
about a dozen projects today; if a response ever looks big (more than MAX_PROJECTS) the run stops, because at that
size we should use an official export instead of retrieving the list ourselves.
Privacy: no approver names or contacts are stored; free text is shortened and stripped of emails and phone numbers.
Quality flags are OUR checks (never GPRIS's): disbursed_exceeds_allocated, no_budget, no_county, no_update_date.
Only projects GPRIS marks approved and not voided are stored.
"""
import datetime
import hashlib
import json
import os
import re
import sys
import time

import requests

from ocds_load import ALNUM, COUNTY_KEY, D1, lit

MODE = "load"            # "dry-run" (fetch and report, no database) or "load"
BASE = "https://gpris.go.ke"
HEADERS = {"User-Agent": "PesaWatchKenya-gpris/0.1 (non-commercial public-finance transparency project)", "Accept": "application/json"}
MAX_PROJECTS = 500
MAX_BYTES = 5_000_000
PAUSE = 5
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?:\+254|\b0)[17]\d{8}\b")


def clean_text(s, n=600):
    if not isinstance(s, str):
        return None
    t = PHONE.sub("[phone]", EMAIL.sub("[email]", s)).strip()
    return (t[:n] + "...") if len(t) > n else (t or None)


def as_int(v):
    return int(round(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def fetch(path):
    r = requests.get(BASE + path, headers=HEADERS, timeout=(15, 60), stream=True)
    raw = b""
    for chunk in r.iter_content(65536):
        raw += chunk
        if len(raw) > MAX_BYTES:
            sys.exit(f"{path}: response larger than {MAX_BYTES} bytes; stop and request an official export instead.")
    r.raise_for_status()
    return raw, json.loads(raw.decode("utf-8"))


def transform(p):
    loc, bud, tl, pr = (p.get(k) or {} for k in ("location", "budget", "timeline", "progress"))
    geo = loc.get("geocoordinates") or {}
    county = (loc.get("county") or "").strip()
    code = COUNTY_KEY.get(ALNUM(county)) if county else None
    alloc, disb = as_int(bud.get("allocated_amount_kes")), as_int(bud.get("disbursed_amount_kes"))
    flags = []
    if alloc is None or alloc <= 0:
        flags.append("no_budget")
    elif disb is not None and disb > alloc:
        flags.append("disbursed_exceeds_allocated")
    if not county:
        flags.append("no_county")
    if not (tl.get("last_updated") or "").strip():
        flags.append("no_update_date")
    ver = sorted({(d.get("verification_status") or "").strip() for d in (p.get("data_sources") or []) if d.get("verification_status")})
    lat, lng = geo.get("lat"), geo.get("lng")
    return {
        "project_id": str(p.get("project_id")), "name": clean_text(p.get("name"), 300) or "(unnamed)",
        "description": clean_text(p.get("description")), "sector": clean_text(p.get("sector"), 200),
        "ministry": clean_text(p.get("ministry"), 200), "state_department": clean_text(p.get("state_department"), 200),
        "implementing_agency": clean_text(p.get("implementing_agency"), 200) or None, "programme_name": clean_text(p.get("programme_name"), 200),
        "project_level": clean_text(p.get("project_level"), 40), "county_name": county or None, "county_code": code,
        "constituency": clean_text(loc.get("constituency"), 100), "ward": clean_text(loc.get("ward"), 100),
        "lat": float(lat) if isinstance(lat, (int, float)) else None, "lng": float(lng) if isinstance(lng, (int, float)) else None,
        "allocated_kes": alloc, "disbursed_kes": disb, "funding_source": clean_text(bud.get("source"), 100),
        "start_date": clean_text(tl.get("start_date"), 40), "expected_completion": clean_text(tl.get("expected_completion_date"), 40),
        "last_updated": clean_text(tl.get("last_updated"), 40), "progress_pct": as_int(pr.get("percentage_complete")),
        "status": clean_text(pr.get("status"), 60), "latest_update": clean_text(pr.get("latest_update_summary"), 400),
        "gpris_verification": ", ".join(ver) or None, "quality_flags": ",".join(flags) or None,
    }


def keep(p):
    pub = p.get("is_public") or {}
    return not p.get("voided") and pub.get("approved", False) and p.get("project_id") is not None


def main():
    stats_raw, stats = fetch("/api/projects/stats")
    time.sleep(PAUSE)
    list_raw, listing = fetch("/api/projects")
    data = listing.get("data")
    if not isinstance(data, list) or listing.get("msg") != "Success":
        sys.exit("Unexpected response shape from /api/projects; nothing written.")
    if len(data) > MAX_PROJECTS:
        sys.exit(f"{len(data)} projects returned (> {MAX_PROJECTS}); stop and request an official export instead.")
    kept = [transform(p) for p in data if keep(p)]
    sd = stats.get("data") or {}
    lines = [f"## GPRIS load ({MODE})", "", f"- projects returned {len(data)}, stored {len(kept)} (approved, not voided); "
             f"GPRIS's own count {sd.get('totalProjects')}",
             f"- counties covered {sd.get('countiesCovered')} of {sd.get('totalCounties')}; allocated {sd.get('totalAllocated')}; disbursed {sd.get('totalDisbursed')}"]
    flagged = [(k["name"][:40], k["quality_flags"]) for k in kept if k["quality_flags"]]
    lines.append(f"- projects with our quality flags: {flagged}")
    sha = hashlib.sha256(list_raw + stats_raw).hexdigest()
    if MODE != "load":
        lines.append('\nDry run only. Set MODE = "load" to write.')
        print("\n".join(lines))
        return
    if not (os.environ.get("CLOUDFLARE_API_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID")):
        sys.exit("Load mode needs the Cloudflare secrets.")
    db = D1()
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp, day = now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d")
    last = db.run("SELECT sha256 FROM source_documents WHERE source_id='gpris' AND external_id='api/projects' ORDER BY id DESC LIMIT 1")[0]
    if last and last[0]["sha256"] == sha:
        lines.append("- GPRIS data unchanged since the last snapshot; nothing written.")
        print("\n".join(lines))
        return
    db.run("INSERT OR IGNORE INTO source_documents (source_id, external_id, title, doc_kind, url, bytes, sha256, status, retrieved_at) "
           "VALUES ('gpris','api/projects','GPRIS public project list (snapshot)','gpris_snapshot',?,?,?, 'available',?)",
           [BASE + "/api/projects", len(list_raw) + len(stats_raw), sha, stamp])
    doc_id = db.run("SELECT id FROM source_documents WHERE source_id='gpris' AND external_id='api/projects' AND sha256=?", [sha])[0][0]["id"]
    st = []
    cols = ["project_id", "name", "description", "sector", "ministry", "state_department", "implementing_agency", "programme_name",
            "project_level", "county_name", "county_code", "constituency", "ward", "lat", "lng", "allocated_kes", "disbursed_kes",
            "funding_source", "start_date", "expected_completion", "last_updated", "progress_pct", "status", "latest_update",
            "gpris_verification", "quality_flags"]
    for k in kept:
        vals = ",".join(lit(k[c]) if not isinstance(k[c], float) else repr(k[c]) for c in cols)
        st.append(f"INSERT INTO gpris_projects ({','.join(cols)}, first_seen_at, last_seen_at, source_document_id) VALUES "
                  f"({vals},{lit(stamp)},{lit(stamp)},{doc_id}) ON CONFLICT(project_id) DO UPDATE SET "
                  + ",".join(f"{c}=excluded.{c}" for c in cols[1:]) + f",last_seen_at={lit(stamp)},source_document_id={doc_id};")
        st.append(f"INSERT OR REPLACE INTO gpris_project_history (project_id, observed_on, progress_pct, allocated_kes, disbursed_kes, status) "
                  f"VALUES ({lit(k['project_id'])},{lit(day)},{lit(k['progress_pct'])},{lit(k['allocated_kes'])},{lit(k['disbursed_kes'])},{lit(k['status'])});")
    st.append("INSERT OR REPLACE INTO gpris_stats (observed_on, total_projects, total_allocated_kes, total_disbursed_kes, counties_covered, "
              "total_counties, avg_progress, disbursement_rate, source_document_id) VALUES "
              f"({lit(day)},{lit(as_int(sd.get('totalProjects')))},{lit(as_int(sd.get('totalAllocated')))},{lit(as_int(sd.get('totalDisbursed')))},"
              f"{lit(as_int(sd.get('countiesCovered')))},{lit(as_int(sd.get('totalCounties')))},{lit(as_int(sd.get('avgProgress')))},"
              f"{lit(as_int(sd.get('disbursementRate')))},{doc_id});")
    written = db.run("\n".join(st))[1]
    db.run("INSERT INTO d1_write_budget (day, rows_written) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET rows_written = rows_written + excluded.rows_written", [day, written + 2])
    n = db.run("SELECT COUNT(*) AS n FROM gpris_projects")[0][0]["n"]
    lines += [f"- rows written: {written}; projects now in D1: {n}"]
    print("\n".join(lines))
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
