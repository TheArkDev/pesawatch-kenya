// Minimal PesaWatch API: proves the D1 binding works.
// GET /health    -> checks the database answers
// GET /counties  -> the 47 seeded counties

export interface Env {
  DB: D1Database;
}

const json = (data: unknown, status = 200) =>
  Response.json(data, {
    status,
    headers: { "Cache-Control": "public, max-age=300" }, // cheap reads: let Cloudflare cache
  });

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const { pathname } = new URL(request.url);

    if (pathname === "/health") {
      const row = await env.DB.prepare("SELECT COUNT(*) AS counties FROM entities WHERE kind = 'county'").first();
      return json({ ok: true, ...row });
    }

    if (pathname === "/counties") {
      const { results } = await env.DB
        .prepare("SELECT county_code, name FROM entities WHERE kind = 'county' ORDER BY county_code")
        .all();
      return json(results);
    }

    return json({ error: "not found" }, 404);
  },
};
