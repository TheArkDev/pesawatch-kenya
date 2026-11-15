#!/usr/bin/env python3
"""National government headline tables (FY2025/26) from the Controller of Budget report, validated by arithmetic.

Tables (PDF pages in brackets, from the earlier profile):
  3.1  overall budget performance: recurrent (MDAs, CFS), development, total; this year and last year   [57]
  3.2  Consolidated Fund Services: public debt, pensions, salaries                                       [59]
  3.3  public debt stock by lender, 30 June 2025 and 2026                                               [59-60]
  3.4  external debt by currency                                                                         [60-61]
  3.5  public debt service: principal and interest, external and domestic                                [62]
  3.12 compensation to employees by sector                                                               [85]
  3.14 national trade payables (pending bills)                                                           [88-89]
Rows are named by their fixed order in the report (wrapped labels make text parsing of names unreliable) and every
table is checked with its own identities: sub-totals add up, totals add up, percentages recompute.
Nothing is written to D1 here. Output: reports/cob_national_headlines.json and the job summary.
"""
import json
import os
import re

from pypdf import PdfReader

TOK = r"Less than 1|\(?-?\d[\d,]*(?:\.\d+)?\)?%?|-"
TAIL = re.compile(r"^(?:(?P<label>.*?)\s+)?(?P<nums>(?:(?:" + TOK + r")(?:\s+|$))+)$")
PAGES = {"3.1": 57, "3.2": 59, "3.3": (59, 60), "3.4": (60, 61), "3.5": 62, "3.12": 85, "3.14": (88, 89)}


def num(tok):
    t = tok.strip()
    if t == "-" or t.lower().startswith("less than"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()%").replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def split_row(line, min_tokens):
    """Return (label, [tokens]) when the line ends in at least min_tokens number-like tokens."""
    m = TAIL.match(line.strip())
    if not m:
        return None
    toks = re.findall(TOK, m.group("nums"))
    if len(toks) < min_tokens:
        return None
    return (m.group("label") or "").strip(), toks


def region(lines, start_pat, end_pat="Source:"):
    out, on = [], False
    for ln in lines:
        if not on and re.search(start_pat, ln):
            on = True
            continue
        if on and ln.strip().startswith(end_pat):
            break
        if on:
            out.append(ln)
    return out


def rows_in_order(lines, min_tokens, labels):
    rows = []
    for ln in lines:
        r = split_row(ln, min_tokens)
        if r:
            rows.append(r)
    return rows


def z(x):
    return x or 0.0            # a dash (None) counts as zero in sums


def close(a, b, tol=0.06):
    return abs(z(a) - z(b)) <= tol


def t31(lines):
    rows = rows_in_order(region(lines, r"Table 3\.1:"), 12, None)
    labels = ["Recurrent", "Recurrent: MDAs", "Recurrent: CFS", "Development", "Total"]
    out = {"labels": labels, "rows": [[num(t) for t in r[1][:12]] for r in rows]}
    chk = []
    if len(rows) == 5:
        v = out["rows"]
        for j in (0, 1, 2, 3, 6, 7, 8, 9):
            chk.append(close(v[0][j], z(v[1][j]) + z(v[2][j]), 0.15) and close(v[4][j], z(v[0][j]) + z(v[3][j]), 0.15))
    out["check"] = {"rows_found": len(rows), "identities_ok": bool(chk) and all(chk), "detail": chk}
    return out


def t32(lines):
    rows = rows_in_order(region(lines, r"Table 3\.2:"), 9, None)
    labels = ["Public Debt", "Pensions and Gratuities", "Salaries, Allowances and Miscellaneous Services", "Total"]
    v = [[num(t) for t in r[1][:9]] for r in rows]
    ok = len(v) == 4 and all(close(sum(z(v[i][j]) for i in range(3)), v[3][j], 0.06) for j in (0, 1, 2, 5, 6, 7))
    return {"labels": labels, "rows": v, "check": {"rows_found": len(rows), "identities_ok": ok}}


def t33(lines):
    """Public debt by lender. External rows end with (as of June 2026, % change, composition); domestic rows are
    (as of 2025, net change, as of 2026, % change, composition). External rows can print fewer dashes than the
    header has columns, so only the first and last three values are relied on."""
    ext_labels = ["Bilateral", "Multilateral", "Commercial Banks", "International Sovereign Bond", "Guaranteed Debt",
                  "Suppliers Credit", "Sub-Total External Debt"]
    dom_labels = ["Treasury Bills", "Treasury bonds", "Pre-1997 Government Overdraft", "Central Bank of Kenya Overdraft",
                  "IMF SDR Funds On-Lent to Government", "Sub-Total Domestic Debt"]
    ext, dom, total = [], [], None
    for ln in lines:
        if len(ext) < 7:
            r = split_row(ln, 7)
            if r:
                t = r[1]
                ext.append({"as_of_2025": num(t[0]), "as_of_2026": num(t[-3]), "pct_change": num(t[-2]), "composition": t[-1],
                            "flows": [num(x) for x in t[1:-3]] if len(t) == 8 else None})
                continue
        elif len(dom) < 6:
            r = split_row(ln, 5)
            if r:
                t = r[1]
                dom.append({"as_of_2025": num(t[0]), "net_change": num(t[1]), "as_of_2026": num(t[2]), "pct_change": num(t[3]), "composition": t[4]})
                continue
        elif total is None and re.match(r"^\s*Total\b", ln):
            r = split_row(ln, 4)
            if r:
                total = [num(x) for x in r[1][:4]]
    ok = False
    if len(ext) == 7 and len(dom) == 6 and total:
        e, d = ext, dom
        ok = (close(sum(z(e[i]["as_of_2025"]) for i in range(6)), e[6]["as_of_2025"]) and close(sum(z(e[i]["as_of_2026"]) for i in range(6)), e[6]["as_of_2026"])
              and close(sum(z(d[i]["as_of_2025"]) for i in range(5)), d[5]["as_of_2025"], 0.1) and close(sum(z(d[i]["as_of_2026"]) for i in range(5)), d[5]["as_of_2026"], 0.1)
              and close(z(e[6]["as_of_2025"]) + z(d[5]["as_of_2025"]), total[0], 0.1) and close(z(e[6]["as_of_2026"]) + z(d[5]["as_of_2026"]), total[1], 0.1))
    return {"external_labels": ext_labels, "external": ext, "domestic_labels": dom_labels, "domestic": dom, "total": total,
            "unit": "KSh billion",
            "check": {"external_rows": len(ext), "domestic_rows": len(dom), "has_total": total is not None, "identities_ok": ok}}


def t34(lines):
    rows, total = [], None
    for ln in lines:
        m = re.match(r"^\s*(\d{1,2})\.\s+(.+?)\s+([\d,]+(?:\s?\.\d+)?)\s+(Less than 1|\d+)\s*$", ln)
        if m:
            rows.append({"n": int(m.group(1)), "currency": re.sub(r"\s+", " ", m.group(2)).strip(),
                         "amount_kes": float(m.group(3).replace(",", "").replace(" ", "")), "pct": m.group(4)})
            continue
        m = re.match(r"^\s*Total\s+([\d,]+)\s+100\s*$", ln)
        if m:
            total = float(m.group(1).replace(",", ""))
    s = sum(r["amount_kes"] for r in rows)
    return {"rows": rows, "total_kes": total, "check": {"rows_found": len(rows), "sum_matches_total": total is not None and abs(s - total) <= 2}}


def t35(lines):
    reg = region(lines, r"Table 3\.5:")
    out = {}
    for ln in reg:
        m = re.match(r"^\s*(External|Domestic|Total)\b\s+(.*)$", ln)
        if m:
            r = split_row(ln, 7)
            if r:
                out[m.group(1)] = [num(t) for t in r[1]]
    ok = False
    if len(out) == 3:
        e, d, t = out["External"], out["Domestic"], out["Total"]
        ok = (close(z(e[3]) + z(e[4]), e[5]) and close(z(d[3]) + z(d[4]), d[5]) and close(z(e[5]) + z(d[5]), t[6], 0.06)
              and close(z(e[3]) + z(d[3]), t[4], 0.1) and close(z(e[4]) + z(d[4]), t[5], 0.1))
    return {"rows": out, "note": "columns: revised principal, revised interest/other, revised total, then (Total row only) exchequer issues, "
            "expenditure principal, interest/other, total, exchequer %, expenditure %. External/Domestic rows have no exchequer figure printed.",
            "check": {"rows_found": len(out), "identities_ok": ok}}


def t312(lines):
    rows, total = [], None
    for ln in lines:
        m = re.match(r"^\s*([A-Za-z*][A-Za-z* ,&-]*?)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+(\d+(?:\.\d+)?)\s*$", ln)
        if m:
            row = {"sector": m.group(1).strip(), "revised_gross": float(m.group(2).replace(",", "")),
                   "expenditure": float(m.group(3).replace(",", "")), "pct": float(m.group(4))}
            if row["sector"].lower() == "total":
                total = row
            else:
                rows.append(row)
    ok = False
    if rows and total:
        ok = (abs(sum(r["revised_gross"] for r in rows) - total["revised_gross"]) <= 0.1
              and abs(sum(r["expenditure"] for r in rows) - total["expenditure"]) <= 0.1
              and all(abs(100 * r["expenditure"] / r["revised_gross"] - r["pct"]) <= 0.6 for r in rows))
    return {"rows": rows, "total": total, "check": {"sectors_found": len(rows), "identities_ok": ok}}


def t314(lines):
    rows = []
    prev_text = ""
    for ln in lines:
        m = re.match(r"^\s*(.*?)\s+(\d[\d,]{8,})\s+(\d[\d,]{8,})\s+(\d+)\s+\(?(-?\d+)\)?\s*$", ln)
        if m:
            label = m.group(1).strip() or prev_text
            rows.append({"label": label, "fy_2026_kes": int(m.group(2).replace(",", "")), "fy_2025_kes": int(m.group(3).replace(",", "")),
                         "share_pct": int(m.group(4)), "change_pct": int(m.group(5))})
            prev_text = ""
        elif ln.strip() and not re.search(r"\d{2,}", ln):
            prev_text = ln.strip()
    by = {r["label"]: r for r in rows}
    ok = None
    rec, dev, sub = by.get("Recurrent"), by.get("Development"), next((r for k, r in by.items() if k.startswith("Sub-Total")), None)
    if rec and dev and sub:
        ok = abs(rec["fy_2026_kes"] + dev["fy_2026_kes"] - sub["fy_2026_kes"]) <= 2 and abs(rec["fy_2025_kes"] + dev["fy_2025_kes"] - sub["fy_2025_kes"]) <= 2
    return {"rows": rows, "check": {"rows_found": len(rows), "mda_subtotal_ok": ok}}


def get_lines(reader, p):
    try:
        txt = reader.pages[p - 1].extract_text(extraction_mode="layout") or ""
    except TypeError:
        txt = reader.pages[p - 1].extract_text() or ""
    return [ln for ln in txt.splitlines() if ln.strip()]


def extract(lines_for_page):
    L = lines_for_page
    res = {
        "3.1": t31(L(57)),
        "3.2": t32(L(59)),
        "3.3": t33(region(L(59), r"Table 3\.3:", "\x00") + region(L(60), r"FY 2025/26", "Source:")),
        "3.4": t34(L(60) + L(61)),
        "3.5": t35(L(62)),
        "3.12": t312(L(85)),
        "3.14": t314(L(88) + L(89)),
    }
    return res


def main():
    from cob_national_profile import get_pdf
    os.makedirs("reports", exist_ok=True)
    path, sha = get_pdf()
    reader = PdfReader(path)
    res = extract(lambda p: get_lines(reader, p))
    res["_pdf_sha256"] = sha
    with open("reports/cob_national_headlines.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    lines = ["## National headline tables", ""]
    for k, v in res.items():
        if k.startswith("_"):
            continue
        lines.append(f"- Table {k}: {v['check']}")
    text = "\n".join(lines)
    print(text)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
