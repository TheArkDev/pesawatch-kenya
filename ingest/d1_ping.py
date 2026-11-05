#!/usr/bin/env python3
"""Check that GitHub Actions can reach your D1 database with the stored secrets.

Needs env vars CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID (set from GitHub secrets).
Steps: find the database 'pesawatch' by name, read a few counts, do one tiny write+delete
in ingest_state, and report rows_written so we can see how D1 counts writes.
The token is never printed.
"""
import os
import sys
import time

import requests

DB_NAME = "pesawatch"
API = "https://api.cloudflare.com/client/v4"


def api(method, path, **kw):
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    r = requests.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"},
                         timeout=(10, 60), **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"success": False, "errors": [{"message": r.text[:200]}]}
    if r.status_code >= 400 or not body.get("success"):
        errs = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in body.get("errors", []))
        raise RuntimeError(f"HTTP {r.status_code} {method} {path.split('?')[0]} -> {errs}")
    return body


def main():
    missing = [k for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {missing}. Is the workflow passing the secrets? "
                 "Did you name the GitHub secrets exactly?")
    acct = os.environ["CLOUDFLARE_ACCOUNT_ID"].strip()
    lines = ["## D1 ping", ""]

    dbs = api("GET", f"/accounts/{acct}/d1/database?name={DB_NAME}")["result"]
    match = [d for d in dbs if d.get("name") == DB_NAME]
    if not match:
        sys.exit(f"No D1 database named '{DB_NAME}' found. Names seen: {[d.get('name') for d in dbs]}")
    db_id = match[0]["uuid"]
    lines.append(f"- Found database '{DB_NAME}' (id {db_id})")

    def q(sql, params=None):
        body = api("POST", f"/accounts/{acct}/d1/database/{db_id}/query",
                   json={"sql": sql, "params": params or []})
        res = body["result"][0]
        return res.get("results", []), res.get("meta", {})

    counts = {}
    for label, sql in [
        ("counties", "SELECT COUNT(*) AS n FROM entities WHERE kind='county'"),
        ("financial_years", "SELECT COUNT(*) AS n FROM financial_years"),
        ("sources", "SELECT COUNT(*) AS n FROM sources"),
        ("tenders", "SELECT COUNT(*) AS n FROM tenders"),
    ]:
        rows, meta = q(sql)
        counts[label] = rows[0]["n"]
        lines.append(f"- {label}: {rows[0]['n']} (rows read {meta.get('rows_read')})")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _, m1 = q("INSERT OR REPLACE INTO ingest_state (job_name, cursor, last_run_at, last_status) VALUES (?,?,?,?)",
              ["d1_ping", "test", now, "ok"])
    _, m2 = q("DELETE FROM ingest_state WHERE job_name = ?", ["d1_ping"])
    lines.append(f"- Write test OK: insert rows_written={m1.get('rows_written')}, delete rows_written={m2.get('rows_written')}")

    ok = counts["counties"] == 47
    lines.append("")
    lines.append("RESULT: " + ("database reachable, migrations applied, writes work." if ok
                               else "reachable, but county count is not 47 - migrations may be missing."))
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}")
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"## D1 ping\n\nFAILED: {e}\n")
        sys.exit(1)
