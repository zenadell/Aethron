// The database, through Supabase's own REST layer (PostgREST) with the
// service role. Narrow on purpose: the queries this function issues are
// the only ones it can issue, which keeps the tests' stand-in honest.

import { env, http, HttpError } from "./util.ts";

// deno-lint-ignore no-explicit-any
export type Row = Record<string, any>;
export type Query = Record<string, string | number | boolean | undefined>;

function base(): string {
  const u = env("SUPABASE_URL").replace(/\/+$/, "");
  if (!u) throw new HttpError(500, "SUPABASE_URL is not set");
  return u;
}

function service(): string {
  const k = env("SUPABASE_SERVICE_ROLE_KEY");
  if (!k) throw new HttpError(500, "SUPABASE_SERVICE_ROLE_KEY is not set");
  return k;
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const k = service();
  return { apikey: k, Authorization: "Bearer " + k, "Content-Type": "application/json", ...extra };
}

function qs(q: Query = {}): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== "") p.append(k, String(v));
  const s = p.toString();
  return s ? "?" + s : "";
}

async function call(method: string, path: string, body?: unknown, extra: Record<string, string> = {}): Promise<Response> {
  const r = await http(base() + path, {
    method,
    headers: headers(extra),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => "");
    throw new HttpError(r.status >= 500 ? 502 : r.status, `database ${method} ${path.split("?")[0]}: ${r.status} ${t.slice(0, 300)}`);
  }
  return r;
}

async function rows(r: Response): Promise<Row[]> {
  const t = await r.text();
  if (!t) return [];
  const v = JSON.parse(t);
  return Array.isArray(v) ? v : [v];
}

export const db = {
  async select(table: string, q: Query = {}): Promise<Row[]> {
    return rows(await call("GET", `/rest/v1/${table}${qs({ select: "*", ...q })}`));
  },

  async one(table: string, q: Query = {}): Promise<Row | null> {
    const r = await db.select(table, { ...q, limit: 1 });
    return r[0] || null;
  },

  async count(table: string, q: Query = {}): Promise<number> {
    const r = await call("GET", `/rest/v1/${table}${qs({ select: "*", ...q, limit: 1 })}`, undefined,
      { Prefer: "count=exact" });
    const cr = r.headers.get("content-range") || "";
    await r.text();
    const total = cr.split("/")[1];
    return total && total !== "*" ? parseInt(total, 10) : 0;
  },

  async insert(table: string, data: Row | Row[]): Promise<Row[]> {
    return rows(await call("POST", `/rest/v1/${table}`, data, { Prefer: "return=representation" }));
  },

  async upsert(table: string, data: Row | Row[], onConflict: string): Promise<Row[]> {
    return rows(await call("POST", `/rest/v1/${table}${qs({ on_conflict: onConflict })}`, data,
      { Prefer: "return=representation,resolution=merge-duplicates" }));
  },

  async update(table: string, q: Query, patch: Row): Promise<Row[]> {
    return rows(await call("PATCH", `/rest/v1/${table}${qs(q)}`, patch, { Prefer: "return=representation" }));
  },

  async remove(table: string, q: Query): Promise<Row[]> {
    if (!Object.keys(q).length) throw new HttpError(400, "refusing to delete without a filter");
    return rows(await call("DELETE", `/rest/v1/${table}${qs(q)}`, undefined, { Prefer: "return=representation" }));
  },
};

// ── accounts (GoTrue) ───────────────────────────────────────────────

export const auth = {
  /** The account behind a person's access token, or null. */
  async userFromToken(token: string): Promise<Row | null> {
    const r = await http(base() + "/auth/v1/user", {
      headers: { apikey: env("SUPABASE_ANON_KEY") || service(), Authorization: "Bearer " + token },
    });
    if (!r.ok) {
      await r.text().catch(() => "");
      return null;
    }
    return await r.json();
  },

  async listUsers(page = 1, perPage = 200): Promise<Row[]> {
    const r = await call("GET", `/auth/v1/admin/users${qs({ page, per_page: perPage })}`);
    const v = await r.json();
    return (v && v.users) || [];
  },

  async allUsers(cap = 5000): Promise<Row[]> {
    const out: Row[] = [];
    for (let page = 1; out.length < cap; page++) {
      const batch = await auth.listUsers(page, 200);
      out.push(...batch);
      if (batch.length < 200) break;
    }
    return out;
  },

  async getUser(id: string): Promise<Row | null> {
    try {
      const r = await call("GET", `/auth/v1/admin/users/${encodeURIComponent(id)}`);
      return await r.json();
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) return null;
      throw e;
    }
  },

  async deleteUser(id: string): Promise<void> {
    await (await call("DELETE", `/auth/v1/admin/users/${encodeURIComponent(id)}`)).text();
  },
};
