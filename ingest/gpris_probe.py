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
FETCH_RE = re.compile(r"fetch\(\s*([^,)]{3,140})")
MAX_JS_FILES, MAX_JS_BYTES = 60, 3_000_000
NOISE = ("nextjs.org", "cloudinary", "google", "facebook", "twitter", "linkedin", "w3.org", "reactjs.org", "github.com",
         "mui.com", "emotion", "mozilla.org", "youtube", "gstatic", "schema.org", "unpkg", "jsdelivr", "cloudflare.com/ajax")
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
        low = u.lower().rstrip(".,;)")
        if any(n in low for n in NOISE):
            continue
        if any(low.split("?")[0].endswith(x) for x in (".png", ".jpg", ".jpeg", ".svg", ".css", ".woff", ".woff2", ".mp4", ".ico", ".webp", ".js")):
            continue
        keep.append(low)
    return keep


def visible_text(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


def main():
    os.makedirs("reports", exist_ok=True)
    out = {"pages": {}, "scripts": [], "candidate_urls": [], "candidate_paths": []}
    lines = ["## GPRIS probe", ""]
    html_by_path = {}
    for path in PAGES:
        r, secs, err = get(BASE + path)
        rec = {"seconds": secs, "error": err}
        if r is not None:
            rec.update(status=r.status_code, content_type=r.headers.get("content-type", ""), bytes=len(r.content))
            html_by_path[path] = r.text
            if "NEXT_HTTP_ERROR_FALLBACK;404" in r.text:
                rec["soft_404"] = True        # a 'not found' page served with status 200: the file does not exist
            elif path in ("/robots.txt", "/sitemap.xml"):
                rec["text"] = r.text[:3000]
        out["pages"][path] = rec
        lines.append(f"- {path}: {rec.get('status', 'FAIL')} {secs}s {rec.get('bytes', '')} bytes"
                     f"{' (not found: soft 404)' if rec.get('soft_404') else ''} {err or ''}")
    mtp = visible_text(html_by_path.get("/en/medium-term-plan", ""))
    out["medium_term_plan_text"] = mtp[:2500]
    lines += ["", "### Medium Term Plan page, visible text (first 1200 chars)", mtp[:1200] or "(empty)"]

    scripts = []
    for pth in ("/en", "/en/browse"):
        scripts += [urljoin(BASE + pth, x) for x in SCRIPT_RE.findall(html_by_path.get(pth, ""))]
    scripts = list(dict.fromkeys(scripts))[:MAX_JS_FILES]
    urls, paths, fetches = [], [], []
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
        fetches += [f.strip() for f in FETCH_RE.findall(text)]
    out["candidate_urls"] = interesting(urls)[:60]
    out["candidate_paths"] = sorted(set(paths))[:60]
    out["fetch_calls"] = sorted(set(fetches))[:80]
    lines += ["", f"### JavaScript scanned: {len(scripts)} files",
              "### Candidate data-service addresses found in the site's own scripts:"]
    lines += [f"- {u}" for u in out["candidate_urls"][:40]] + [f"- (path) {p}" for p in out["candidate_paths"][:40]]
    lines += ["", "### fetch(...) calls found in the scripts:"] + [f"- {f[:120]}" for f in out["fetch_calls"][:50]]
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
