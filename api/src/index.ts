// PesaWatch Kenya API (read-only). Reads ONLY the small pre-aggregated sum_* tables,
// never the raw tenders/awards tables, to stay inside D1's free read budget.
//
// GET /health
// GET /overview?fy=2025            national procurement overview + breakdowns + provenance
// GET /counties?fy=2025            the 47 counties with procurement totals
// GET /counties/:code?fy=2025      one county (code 1-47) and its buyers
// GET /suppliers/top?fy=&limit=20  suppliers by awarded value (name-matched, see notes)

export interface Env {
  DB: D1Database;
}

const NOTES = [
  "Source: PPIP procurement data (OCDS) as published; figures are not independently verified.",
  "Tender values are not published in this source. Amounts are contract AWARD values only.",
  "Awards are recorded for only some tenders. 'No award recorded' does not mean a tender was not awarded.",
  "Award values above KSh 10 billion are flagged as probable data errors and excluded from totals.",
  "Suppliers are matched by normalised company name only; names may be merged or split incorrectly.",
  "Financial year runs July to June (fy=2025 means 2025/26).",
];

const CORS = { "Access-Control-Allow-Origin": "*" };

function json(data: unknown, status = 200, maxAge = 3600): Response {
  return Response.json(data, {
    status,
    headers: { ...CORS, "Cache-Control": status === 200 ? `public, max-age=${maxAge}` : "no-store" },
  });
}

function currentFy(now = new Date()): number {
  // Financial year starts 1 July: Jul-Dec 2026 -> FY 2026, Jan-Jun 2026 -> FY 2025.
  return now.getUTCMonth() >= 6 ? now.getUTCFullYear() : now.getUTCFullYear() - 1;
}

async function resolveFy(db: D1Database, url: URL): Promise<number | null> {
  const q = url.searchParams.get("fy");
  if (q !== null) {
    const fy = Number(q);
    return Number.isInteger(fy) && fy >= 2010 && fy <= 2035 ? fy : null;
  }
  // Default: the latest financial year that has already ended; otherwise the latest we have.
  const done = await db
    .prepare("SELECT MAX(fy_start) AS fy FROM sum_fy_overview WHERE fy_start < ?")
    .bind(currentFy())
    .first<{ fy: number | null }>();
  if (done?.fy != null) return done.fy;
  const any = await db.prepare("SELECT MAX(fy_start) AS fy FROM sum_fy_overview").first<{ fy: number | null }>();
  return any?.fy ?? null;
}

function coverage(fy: number, first: string | null, last: string | null) {
  const inProgress = fy >= currentFy();
  const gapAtStart = !first || first > `${fy}-08-15`;
  const gapAtEnd = !inProgress && (!last || last < `${fy + 1}-05-15`);
  const warnings: string[] = [];
  if (inProgress) warnings.push("This financial year is still in progress: figures are partial.");
  if (gapAtStart) warnings.push(`No tenders loaded for the start of this financial year (earliest loaded: ${first ?? "none"}). Earlier data may not be loaded yet.`);
  if (gapAtEnd) warnings.push(`No tenders loaded for the end of this financial year (latest loaded: ${last ?? "none"}).`);
  return { first_release_date: first, last_release_date: last, in_progress: inProgress, complete: warnings.length === 0, warnings };
}

async function provenance(db: D1Database) {
  const { results } = await db
    .prepare(
      "SELECT title, url, retrieved_at, substr(sha256,1,12) AS sha256_prefix FROM source_documents " +
        "WHERE source_id = 'ppip_ocds' AND status = 'available' ORDER BY retrieved_at DESC LIMIT 5"
    )
    .all();
  return results;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "GET") return json({ error: "method not allowed" }, 405);
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const db = env.DB;

    try {
      if (path === "/health") {
        const row = await db.prepare("SELECT COUNT(*) AS counties FROM entities WHERE kind = 'county'").first();
        return json({ ok: true, ...row }, 200, 60);
      }

      if (path === "/overview") {
        const fy = await resolveFy(db, url);
        if (fy === null) return json({ error: "no data or invalid fy" }, 404);
        const overview = await db.prepare("SELECT * FROM sum_fy_overview WHERE fy_start = ?").bind(fy).first();
        if (!overview) return json({ error: `no data for fy ${fy}` }, 404);
        const { results: breakdown } = await db
          .prepare("SELECT dim, value, tenders, tenders_with_award, awarded_value_kes FROM sum_fy_breakdown WHERE fy_start = ? ORDER BY dim, tenders DESC")
          .bind(fy)
          .all();
        const o = overview as Record<string, number>;
        const o2 = overview as unknown as { first_release_date: string | null; last_release_date: string | null };
        return json({
          fy, label: `${fy}/${String(fy + 1).slice(2)}`, coverage: coverage(fy, o2.first_release_date, o2.last_release_date), overview,
          share_of_tenders_with_award_recorded: o.tenders ? Math.round((1000 * o.tenders_with_award) / o.tenders) / 10 : null,
          breakdown, notes: NOTES, source: await provenance(db),
        });
      }

      if (path === "/counties") {
        const fy = await resolveFy(db, url);
        if (fy === null) return json({ error: "no data or invalid fy" }, 404);
        const { results } = await db
          .prepare(
            "SELECT e.county_code, e.name, COALESCE(s.tenders,0) AS tenders, COALESCE(s.tenders_with_award,0) AS tenders_with_award, " +
              "COALESCE(s.awarded_value_kes,0) AS awarded_value_kes FROM entities e LEFT JOIN (" +
              "SELECT buyer_entity_id, SUM(tenders) AS tenders, SUM(tenders_with_award) AS tenders_with_award, " +
              "SUM(awarded_value_kes) AS awarded_value_kes FROM sum_buyer_fy WHERE fy_start = ? AND buyer_entity_id IS NOT NULL " +
              "GROUP BY buyer_entity_id) s ON s.buyer_entity_id = e.id WHERE e.kind = 'county' ORDER BY e.county_code"
          )
          .bind(fy)
          .all();
        return json({ fy, counties: results, notes: NOTES, source: await provenance(db) });
      }

      const county = path.match(/^\/counties\/(\d{1,2})$/);
      if (county) {
        const code = Number(county[1]);
        if (code < 1 || code > 47) return json({ error: "county code must be 1-47" }, 404);
        const fy = await resolveFy(db, url);
        if (fy === null) return json({ error: "no data or invalid fy" }, 404);
        const ent = await db.prepare("SELECT id, county_code, name FROM entities WHERE kind = 'county' AND county_code = ?").bind(code).first<{ id: number }>();
        if (!ent) return json({ error: "county not found" }, 404);
        const { results: buyers } = await db
          .prepare(
            "SELECT buyer_ppip_id, buyer_name, tenders, tenders_with_award, direct_tenders, awarded_value_kes FROM sum_buyer_fy " +
              "WHERE fy_start = ? AND buyer_entity_id = ? ORDER BY tenders DESC LIMIT 25"
          )
          .bind(fy, ent.id)
          .all();
        return json({ fy, county: ent, buyers, notes: NOTES, source: await provenance(db) });
      }

      if (path === "/suppliers/top") {
        const fy = await resolveFy(db, url);
        if (fy === null) return json({ error: "no data or invalid fy" }, 404);
        const limit = Math.min(50, Math.max(1, Number(url.searchParams.get("limit") ?? 20) || 20));
        const { results } = await db
          .prepare("SELECT name, award_rows, awarded_value_kes, buyers FROM sum_supplier_fy WHERE fy_start = ? ORDER BY awarded_value_kes DESC LIMIT ?")
          .bind(fy, limit)
          .all();
        return json({ fy, suppliers: results, notes: NOTES, source: await provenance(db) });
      }

      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: "internal error" }, 500);
    }
  },
};
