#!/usr/bin/env python3
"""First, polite look at GPRIS (gpris.go.ke), the government project reporting portal.

Does NOT download project data. It checks that the site answers from a GitHub runner, reads robots.txt and the
sitemap (rules we must follow), and scans the site's own public JavaScript for the address of the data service
the project pages call (the pages say "Loading projects..." because they fetch their data after loading).
Output: reports/gpris_probe.json and the job summary.
"""
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests

BASE = "https://gpris.go.ke"
HEADERS = {"User-Agent": "PesaWatchKenya-gpris-probe/0.1 (non-commercial public-finance transparency project)"}
PAGES = ["/en", "/en/browse", "/en/about", "/en/medium-term-plan", "/robots.txt", "/sitemap.xml"]
MAX_JS_FILES, MAX_JS_BYTES = 6, 6_000_000
URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+")
PATH_RE = re.compile(r"[\"'`](/(?:api|graphql|v\d)/[A-Za-z0-9_\-./?=&{}$]*)[\"'`]")
SCRIPT_RE = re.compile(r"""(?:src|href)=["']([^"']+\.(?:js|mjs)(?:\?[^"']*)?)["']""", re.I)


def get(url, **kw):
    t = time.monotonic()
    try:
        r = requests.get(url, headers=HEADERS, timeout=(15, 60), **kw)
        return r, round(time.monotonic() - t, 2), None
    except requests.RequestException as e:
        return None, round(time.monotonic() - t, 2), f"{type(e).__name__}: {str(e)[:150]}"


def interesting(urls):
    keep = []
    for u in sorted(set(urls)):
        low = u.lower()
        if any(k in low for k in ("api", "gpris", "graphql", "supabase", "firebase", "/v1", "/v2", "projects")):
            if not any(low.endswith(x) for x in (".png", ".jpg", ".svg", ".css", ".woff", ".woff2", ".mp4")):
                keep.append(u.rstrip(".,;)"))
    return keep


def main():
    os.makedirs("reports", exist_ok=True)
    out = {"pages": {}, "scripts": [], "candidate_urls": [], "candidate_paths": []}
    lines = ["## GPRIS probe", ""]
    home_html = ""
    for path in PAGES:
        r, secs, err = get(BASE + path)
        rec = {"seconds": secs, "error": err}
        if r is not None:
            rec.update(status=r.status_code, content_type=r.headers.get("content-type", ""), bytes=len(r.content))
            if path in ("/robots.txt", "/sitemap.xml"):
                rec["text"] = r.text[:3000]
            if path == "/en":
                home_html = r.text
        out["pages"][path] = rec
        lines.append(f"- {path}: {rec.get('status', 'FAIL')} {secs}s {rec.get('content_type', '')} {rec.get('bytes', '')} {err or ''}")
    lines += ["", "### robots.txt", "```", (out["pages"].get("/robots.txt", {}).get("text") or "(none)")[:1500], "```"]

    scripts = [urljoin(BASE + "/en", s) for s in SCRIPT_RE.findall(home_html)]
    scripts = list(dict.fromkeys(scripts))[:MAX_JS_FILES]
    urls, paths = [], []
    for s in scripts:
        r, secs, err = get(s, stream=True)
        size = 0
        text = ""
        if r is not None and r.status_code == 200:
            chunks = []
            for ch in r.iter_content(65536):
                size += len(ch)
                if size > MAX_JS_BYTES:
                    break
                chunks.append(ch)
            text = b"".join(chunks).decode("utf-8", errors="ignore")
        out["scripts"].append({"url": s, "bytes": size, "error": err})
        urls += URL_RE.findall(text)
        paths += PATH_RE.findall(text)
    out["candidate_urls"] = interesting(urls)[:60]
    out["candidate_paths"] = sorted(set(paths))[:60]
    lines += ["", f"### JavaScript scanned: {len(scripts)} files",
              "### Candidate data-service addresses found in the site's own scripts:"]
    lines += [f"- {u}" for u in out["candidate_urls"][:40]] + [f"- (path) {p}" for p in out["candidate_paths"][:40]]
    if not out["candidate_urls"] and not out["candidate_paths"]:
        lines.append("- none found in the scripts that were scanned (the data service may be called from a file not linked on the home page)")
    with open("reports/gpris_probe.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    text = "\n".join(lines)
    print(text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
