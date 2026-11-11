// PesaWatch Kenya API (read-only). Reads ONLY the small pre-aggregated sum_* tables,
// never the raw tenders/awards tables, to stay inside D1's free read budget.
//
// GET /health
// GET /years                       financial years available (+ coverage) and the default year
// GET /overview?fy=2025            national procurement overview + breakdowns + provenance
// GET /counties?fy=2025            the 47 counties with procurement totals
// GET /counties/:code?fy=2025      one county (code 1-47) and its buyers
// GET /suppliers/top?fy=&limit=20  suppliers by awarded value (name-matched, see notes)
// GET /budget/counties?fy=2025     county approved budget, exchequer issues, expenditure, absorption
// GET /national?fy=2025            national government votes by sector: revised budget, exchequer issues, expenditure

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

const BUDGET_NOTES = [
  "Source: Office of the Controller of Budget, county budget implementation review report (annual), as published. Figures are reported by counties and are not audited.",
  "Approved budget is the county's approved estimates. Exchequer issues are funds released to the county by the National Treasury. Expenditure is as reported by the county. These are different measures and must not be read as one another.",
  "Absorption rate is reported expenditure divided by the approved budget. A low rate can reflect delays or late releases; it is not by itself evidence of wrongdoing.",
  "Pending bills are amounts a county owed suppliers, staff and statutory bodies at year end but had not paid, as reported by counties. They include unpaid salaries and statutory deductions.",
  "The source reports in KSh millions, so amounts are rounded to that level. Each figure carries the report table and PDF page it came from.",
];

const NATIONAL_NOTES = [
  "Source: Office of the Controller of Budget, National Government Budget Implementation Review Report (annual), sector analyses. Figures are as reported by ministries, departments and agencies and are not audited.",
  "Revised budget is the revised gross estimate after supplementary budgets, including appropriations in aid. Exchequer issues are funds released by the National Treasury. Expenditure is as reported. These are different measures and must not be read as one another.",
  "The source reports in KSh billions to two decimals, so amounts are rounded to that level. Each figure carries its sector analysis and PDF page.",
  "The Ministry of Defence is not included: the report gives no comparable recurrent figures for it. Some figures are marked as not passing the automatic arithmetic checks and carry a note saying why.",
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

async function provenance(db: D1Database, sourceId = "ppip_ocds", docKind: string | null = null) {
  const { results } = await db
    .prepare(
      "SELECT title, url, retrieved_at, substr(sha256,1,12) AS sha256_prefix FROM source_documents " +
        "WHERE source_id = ? AND status = 'available' AND (? IS NULL OR doc_kind = ?) ORDER BY retrieved_at DESC LIMIT 5"
    )
    .bind(sourceId, docKind, docKind)
    .all();
  return results;
}

type BudgetRow = {
  county_code: number; name: string; approved_budget: number | null; exchequer_release: number | null;
  reported_expenditure: number | null; dev_approved: number | null; dev_expenditure: number | null;
  pending_bills: number | null;
};

type Half = { revised: number | null; exchequer: number | null; expenditure: number | null };
const emptyHalf = (): Half => ({ revised: null, exchequer: null, expenditure: null });
type NationalFact = {
  entity_id: number; name: string; kind: string; sector_id: number; spend_type: string; measure: string;
  amount_kes: number; review_status: string; note: string | null; page_ref: string | null;
};
type NationalVote = {
  entity_id: number; name: string; kind: string; sector_id: number; development: Half; recurrent: Half;
  unreviewed: boolean; notes: string[]; pages: string[];
};

const pct = (num: number | null, den: number | null): number | null =>
  num !== null && den ? Math.round((1000 * num) / den) / 10 : null;

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

      if (path === "/years") {
        const { results } = await db
          .prepare("SELECT fy_start, tenders, first_release_date, last_release_date FROM sum_fy_overview ORDER BY fy_start DESC")
          .all<{ fy_start: number; tenders: number; first_release_date: string | null; last_release_date: string | null }>();
        const years = results.map((r) => ({
          fy: r.fy_start,
          label: `${r.fy_start}/${String(r.fy_start + 1).slice(2)}`,
          tenders: r.tenders,
          coverage: coverage(r.fy_start, r.first_release_date, r.last_release_date),
        }));
        return json({ default_fy: await resolveFy(db, url), years });
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

      if (path === "/budget/counties") {
        let fy: number | null;
        const q = url.searchParams.get("fy");
        if (q !== null) {
          const n = Number(q);
          fy = Number.isInteger(n) && n >= 2010 && n <= 2035 ? n : null;
        } else {
          const row = await db.prepare("SELECT MAX(fy_start) AS fy FROM dash_entity_fy WHERE as_of_period = 'annual'").first<{ fy: number | null }>();
          fy = row?.fy ?? null;
        }
        if (fy === null) return json({ error: "no budget data" }, 404);
        const { results } = await db
          .prepare(
            "SELECT e.county_code, e.name, d.approved_budget, d.exchequer_release, d.reported_expenditure, d.dev_approved, d.dev_expenditure, d.pending_bills " +
              "FROM dash_entity_fy d JOIN entities e ON e.id = d.entity_id " +
              "WHERE d.fy_start = ? AND d.as_of_period = 'annual' AND e.kind = 'county' ORDER BY e.county_code"
          )
          .bind(fy)
          .all<BudgetRow>();
        if (results.length === 0) return json({ error: `no budget data for fy ${fy}` }, 404);
        const { results: flags } = await db
          .prepare(
            "SELECT DISTINCT e.county_code, e.name, f.note FROM fiscal_facts f JOIN entities e ON e.id = f.entity_id " +
              "WHERE f.fy_start = ? AND f.note IS NOT NULL ORDER BY e.county_code"
          )
          .bind(fy)
          .all();
        const sum = (k: keyof BudgetRow) => results.reduce((a, r) => a + ((r[k] as number | null) ?? 0), 0);
        const totals = {
          approved_budget: sum("approved_budget"), exchequer_release: sum("exchequer_release"),
          reported_expenditure: sum("reported_expenditure"), dev_approved: sum("dev_approved"), dev_expenditure: sum("dev_expenditure"),
          pending_bills: sum("pending_bills"),
        };
        return json({
          fy, label: `${fy}/${String(fy + 1).slice(2)}`,
          counties: results.map((r) => ({
            ...r, absorption_pct: pct(r.reported_expenditure, r.approved_budget), dev_absorption_pct: pct(r.dev_expenditure, r.dev_approved),
            pending_pct_of_budget: pct(r.pending_bills, r.approved_budget),
          })),
          totals: { ...totals, absorption_pct: pct(totals.reported_expenditure, totals.approved_budget), dev_absorption_pct: pct(totals.dev_expenditure, totals.dev_approved), pending_pct_of_budget: pct(totals.pending_bills, totals.approved_budget) },
          data_flags: flags, notes: BUDGET_NOTES, source: await provenance(db, "cob", "county_birr"),
        });
      }

      if (path === "/national") {
        let fy: number | null;
        const q = url.searchParams.get("fy");
        if (q !== null) {
          const n = Number(q);
          fy = Number.isInteger(n) && n >= 2010 && n <= 2035 ? n : null;
        } else {
          const row = await db.prepare("SELECT MAX(fy_start) AS fy FROM fiscal_facts WHERE classification_id BETWEEN 401 AND 410").first<{ fy: number | null }>();
          fy = row?.fy ?? null;
        }
        if (fy === null) return json({ error: "no national data" }, 404);
        const { results: sectorRows } = await db
          .prepare("SELECT id, name FROM classifications WHERE id BETWEEN 401 AND 410 ORDER BY id")
          .all<{ id: number; name: string }>();
        const { results: facts } = await db
          .prepare(
            "SELECT e.id AS entity_id, e.name, e.kind, f.classification_id AS sector_id, f.spend_type, f.measure, f.amount_kes, " +
              "f.review_status, f.note, f.page_ref FROM fiscal_facts f JOIN entities e ON e.id = f.entity_id " +
              "WHERE f.fy_start = ? AND f.as_of_period = 'annual' AND f.classification_id BETWEEN 401 AND 410 " +
              "ORDER BY f.classification_id, e.name"
          )
          .bind(fy)
          .all<NationalFact>();
        if (facts.length === 0) return json({ error: `no national data for fy ${fy}` }, 404);
        const votes = new Map<string, NationalVote>();
        for (const f of facts) {
          const key = `${f.sector_id}:${f.entity_id}`;
          let v = votes.get(key);
          if (!v) {
            v = { entity_id: f.entity_id, name: f.name, kind: f.kind, sector_id: f.sector_id, development: emptyHalf(), recurrent: emptyHalf(),
                  unreviewed: false, notes: [], pages: [] };
            votes.set(key, v);
          }
          const half = f.spend_type === "development" ? v.development : f.spend_type === "recurrent" ? v.recurrent : null;
          const field = f.measure === "revised_budget" ? "revised" : f.measure === "exchequer_release" ? "exchequer" : f.measure === "reported_expenditure" ? "expenditure" : null;
          if (half && field) half[field] = f.amount_kes;
          if (f.review_status !== "checked") v.unreviewed = true;
          if (f.note && !v.notes.includes(f.note)) v.notes.push(f.note);
          if (f.page_ref && !v.pages.includes(f.page_ref)) v.pages.push(f.page_ref);
        }
        const addHalf = (a: Half, b: Half): Half => ({
          revised: (a.revised ?? 0) + (b.revised ?? 0), exchequer: (a.exchequer ?? 0) + (b.exchequer ?? 0),
          expenditure: (a.expenditure ?? 0) + (b.expenditure ?? 0),
        });
        const national = { development: emptyHalf(), recurrent: emptyHalf() };
        const sectors = sectorRows.map((sec) => {
          const vs = [...votes.values()].filter((v) => v.sector_id === sec.id);
          const totals = { development: emptyHalf(), recurrent: emptyHalf() };
          for (const v of vs) {
            totals.development = addHalf(totals.development, v.development);
            totals.recurrent = addHalf(totals.recurrent, v.recurrent);
          }
          national.development = addHalf(national.development, totals.development);
          national.recurrent = addHalf(national.recurrent, totals.recurrent);
          return { id: sec.id, name: sec.name, totals, votes: vs };
        });
        return json({
          fy, label: `${fy}/${String(fy + 1).slice(2)}`, national_totals: national, sectors,
          notes: NATIONAL_NOTES, source: await provenance(db, "cob", "national_birr"),
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
        const { results: budgetFacts } = await db
          .prepare(
            "SELECT measure, spend_type, classification_id, amount_kes, page_ref, review_status, note FROM fiscal_facts " +
              "WHERE entity_id = ? AND fy_start = ? AND as_of_period = 'annual' ORDER BY measure, spend_type, classification_id"
          )
          .bind(ent.id, fy)
          .all();
        return json({
          fy, county: ent, buyers, notes: NOTES, source: await provenance(db),
          budget_facts: budgetFacts, budget_notes: BUDGET_NOTES,
          budget_source: budgetFacts.length ? await provenance(db, "cob", "county_birr") : [],
        });
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
