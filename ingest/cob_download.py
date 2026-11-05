#!/usr/bin/env python3
"""Download Controller of Budget reports once, verify them, and record a manifest.

Flow:
  1. Run cob_catalogue.py (same folder) to get the current list of reports.
  2. Select which reports to fetch (see SETTINGS below).
  3. Download each one politely (one at a time, with pauses and retries) to a temp file.
  4. Verify: starts with %PDF, ends with %%EOF, size matches Content-Length when given.
  5. Record SHA-256, exact size and filename in reports/cob_downloads_manifest.json.
  6. Delete the temp file. (PDF storage in Cloudflare R2 comes later; for now we only prove
     that every file downloads cleanly and capture its fingerprint.)

NOTE: the website counts each download publicly, so keep MAX_FILES modest and do not
re-download unchanged files once we store checksums in D1.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import requests

# ---------------- SETTINGS (edit these) ----------------
MIN_FY = 2025        # only reports for financial years starting >= this (2025 = FY2025/26)
MAX_FILES = 4        # first test: 4 files. Raise later (e.g. MIN_FY=2022, MAX_FILES=40).
KINDS = ("national_birr", "county_birr")
PAUSE_SECONDS = 5    # polite gap between downloads
RETRIES = 3
# -------------------------------------------------------

HEADERS = {"User-Agent": "PesaWatchKenya-cob-download/0.1 (public-finance transparency project)"}
PERIOD_RANK = {"annual": 5, "12M": 4, "9M": 3, "6M": 2, "3M": 1}
HERE = os.path.dirname(os.path.abspath(__file__))


def load_catalogue():
    subprocess.run([sys.executable, os.path.join(HERE, "cob_catalogue.py")], check=True)
    with open("reports/cob_catalogue.json", encoding="utf-8") as f:
        return json.load(f)


def select(records):
    chosen = [r for r in records
              if r.get("doc_kind") in KINDS and (r.get("fy_start") or 0) >= MIN_FY]
    skipped = [r for r in chosen if r.get("listing_size_zero")]
    chosen = [r for r in chosen if not r.get("listing_size_zero")]
    chosen.sort(key=lambda r: (-(r["fy_start"]), -PERIOD_RANK.get(r["period"], 0), r["doc_kind"]))
    return chosen[:MAX_FILES], skipped


def download_one(rec, tmpdir):
    out = {"wpdm_id": rec["wpdm_id"], "title": rec["title"], "doc_kind": rec["doc_kind"],
           "fy_start": rec["fy_start"], "period": rec["period"], "url": rec["download_url"],
           "listed_size": rec["listing_size_text"]}
    path = os.path.join(tmpdir, f"{rec['wpdm_id']}.pdf")
    last = None
    for attempt in range(1, RETRIES + 1):
        start = time.monotonic()
        try:
            sha = hashlib.sha256()
            size = 0
            with requests.get(rec["download_url"], headers=HEADERS, timeout=(15, 300), stream=True) as r:
                out["status"] = r.status_code
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                cd = r.headers.get("content-disposition", "")
                m = re.search(r'filename="?([^";]+)"?', cd)
                out["filename"] = m.group(1) if m else ""
                declared = r.headers.get("content-length")
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        f.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)
            with open(path, "rb") as f:
                head = f.read(5)
                f.seek(max(0, size - 2048))
                tail = f.read()
            out.update(
                bytes=size,
                sha256=sha.hexdigest(),
                pdf_header_ok=head.startswith(b"%PDF"),
                eof_marker_ok=b"%%EOF" in tail,
                content_length_matches=(declared is None or int(declared) == size),
                seconds=round(time.monotonic() - start, 1),
                attempts=attempt,
                downloaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                error=None,
            )
            out["verdict"] = "OK" if (out["pdf_header_ok"] and out["eof_marker_ok"]
                                      and out["content_length_matches"]) else "CORRUPT?"
            os.remove(path)
            return out
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:150]}"
            time.sleep(10 * attempt)
    out.update(verdict="FAIL", error=last, attempts=RETRIES)
    return out


def main():
    os.makedirs("reports", exist_ok=True)
    records = load_catalogue()
    chosen, skipped = select(records)
    print(f"\nSelected {len(chosen)} reports (skipped {len(skipped)} listed as 0 KB)", flush=True)

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, rec in enumerate(chosen, 1):
            print(f"[{i}/{len(chosen)}] {rec['doc_kind']} FY{rec['fy_start']} {rec['period']} id={rec['wpdm_id']}", flush=True)
            res = download_one(rec, tmp)
            results.append(res)
            print(f"   -> {res['verdict']} bytes={res.get('bytes')} {res.get('error') or ''}", flush=True)
            time.sleep(PAUSE_SECONDS)

    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "settings": {"MIN_FY": MIN_FY, "MAX_FILES": MAX_FILES, "KINDS": KINDS},
                "skipped_zero_size": [{"wpdm_id": r["wpdm_id"], "title": r["title"]} for r in skipped],
                "downloads": results}
    with open("reports/cob_downloads_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    lines = ["## COB downloads", "", "| Kind | FY | Period | Id | Verdict | Size (MB) | Listed | Seconds |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        mb = round(r["bytes"] / 1048576, 2) if r.get("bytes") else ""
        lines.append(f"| {r['doc_kind']} | {r['fy_start']} | {r['period']} | {r['wpdm_id']} | "
                     f"{r['verdict']} | {mb} | {r['listed_size']} | {r.get('seconds', '')} |")
    table = "\n".join(lines)
    print("\n" + table)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(table + "\n")


if __name__ == "__main__":
    main()
