#!/usr/bin/env python3
"""Look at the SHAPE of GPRIS project data with three small requests (no scraping).

The probe found the addresses the public pages themselves call: /api/projects, /api/projects/featured,
/api/projects/stats. This makes exactly three requests, about 5 seconds apart, each capped at 400 KB, to learn the
field names and types, whether a limit parameter is honoured, and what rate-limit or caching headers say.
Emails and phone numbers are redacted and long text is shortened in what is saved: we only need the structure.
Output: reports/gpris_sample.json and the job summary.
"""
import json
import os
import re
import time

import requests

BASE = "https://gpris.go.ke"
HEADERS = {"User-Agent": "PesaWatchKenya-gpris-sample/0.1 (non-commercial public-finance transparency project)",
           "Accept": "application/json"}
REQUESTS = [("stats", "/api/projects/stats"), ("featured", "/api/projects/featured"),
            ("list_limit5", "/api/projects?limit=5&page=1")]
CAP_BYTES = 400_000
PAUSE_SECONDS = 5
KEEP_HEADERS = ("content-type", "content-length", "cache-control", "etag", "last-modified", "retry-after", "server",
                "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "x-total-count", "age")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")


def redact(obj, depth=0):
    """Shorten strings, hide emails/phones, keep at most 3 items of any list (the shape is what matters)."""
    if isinstance(obj, str):
        t = PHONE.sub("[phone]", EMAIL.sub("[email]", obj))
        return t[:120] + ("..." if len(t) > 120 else "")
    if isinstance(obj, dict):
        return {k: redact(v, depth + 1) for k, v in list(obj.items())[:60]}
    if isinstance(obj, list):
        return [redact(v, depth + 1) for v in obj[:3]]
    return obj


def shape(obj, depth=0):
    """Field names with value types, to three levels."""
    if isinstance(obj, dict):
        return {k: (shape(v, depth + 1) if depth < 3 else type(v).__name__) for k, v in obj.items()}
    if isinstance(obj, list):
        return {"list_of": shape(obj[0], depth + 1) if obj else "empty", "length": len(obj)}
    return type(obj).__name__


def fetch_capped(path):
    t = time.monotonic()
    out = {"path": path}
    try:
        with requests.get(BASE + path, headers=HEADERS, timeout=(15, 60), stream=True) as r:
            out["status"] = r.status_code
            out["headers"] = {k: v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS}
            body, size = [], 0
            for chunk in r.iter_content(65536):
                size += len(chunk)
                body.append(chunk)
                if size >= CAP_BYTES:
                    out["truncated_at_cap"] = True
                    break
            raw = b"".join(body)
            out["bytes_read"] = len(raw)
            out["seconds"] = round(time.monotonic() - t, 2)
            try:
                data = json.loads(raw.decode("utf-8"))
                out["json_ok"] = True
                out["shape"] = shape(data)
                out["sample"] = redact(data)
                if isinstance(data, list):
                    out["item_count"] = len(data)
                elif isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, list):
                            out.setdefault("list_lengths", {})[k] = len(v)
            except ValueError:
                out["json_ok"] = False
                out["text_head"] = PHONE.sub("[phone]", EMAIL.sub("[email]", raw[:600].decode("utf-8", "ignore")))
    except requests.RequestException as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    return out


def main():
    os.makedirs("reports", exist_ok=True)
    results, lines = {}, ["## GPRIS sample (3 requests)", ""]
    for i, (label, path) in enumerate(REQUESTS):
        if i:
            time.sleep(PAUSE_SECONDS)
        print(f"Requesting {path} ...", flush=True)
        res = fetch_capped(path)
        results[label] = res
        lines.append(f"- {path}: status {res.get('status')} bytes {res.get('bytes_read')} json={res.get('json_ok')} "
                     f"items={res.get('item_count', res.get('list_lengths'))} truncated={res.get('truncated_at_cap', False)} "
                     f"{res.get('error', '')}")
        lines.append(f"    headers: {res.get('headers')}")
    with open("reports/gpris_sample.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1, ensure_ascii=False)
    lines += ["", "### Field names (first request that returned JSON with fields)"]
    for label, res in results.items():
        if res.get("shape"):
            lines.append(f"**{label}**: `{json.dumps(res['shape'])[:1500]}`")
    text = "\n".join(lines)
    print(text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
