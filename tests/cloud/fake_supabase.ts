// A stand-in for the parts of Supabase the edge function calls: PostgREST
// (the subset of filters it issues), GoTrue (token -> user, admin users),
// plus a fake model provider and a fake geo lookup. Every request the
// function makes goes through `route`, so a test can also see WHAT was
// sent — which is how "the admin's key went to the provider, the user's
// never did" is proven rather than assumed.

// deno-lint-ignore-file no-explicit-any
type Row = Record<string, any>;

export const SB = "https://sb.test";
export const PROV = "https://prov.test/v1";

export class Fake {
  tables: Record<string, Row[]> = {};
  ids: Record<string, number> = {};
  tokens: Record<string, Row> = {}; // access token -> user
  users: Row[] = [];
  sent: Array<{ url: string; method: string; headers: Record<string, string>; body: string }> = [];
  geoCalls = 0;
  provider: (body: Row, key: string) => Response | Promise<Response> = () => this.reply("hello");

  constructor() {
    for (const t of ["control", "user_controls", "user_notes", "devices", "usage_ledger", "secrets", "admins",
      "admin_keys", "audit_log", "admin_ai_messages", "profiles", "events", "agent_runs", "failure_reports",
      "announcements"]) {
      this.tables[t] = [];
    }
    this.tables.control.push({ id: 1, paused: false, pause_message: "", banner: "", banner_level: "info", ai_mode: "byok",
      managed_provider: "gemini", managed_model: "", features: { agent: true, mcp: true, migrate: true, screenshot: true,
        publish: true, signup: true }, min_version: "", default_daily_usd: 0.5, global_daily_usd: 10, prices: {} });
  }

  addUser(email: string, opts: Row = {}): { id: string; token: string } {
    const id = crypto.randomUUID();
    const iat = Math.floor((opts.iatMs ?? Date.now()) / 1000);
    const token = "h." + btoa(JSON.stringify({ sub: id, iat })).replace(/=+$/, "") + ".s" + Math.random().toString(36).slice(2);
    const u = { id, email, created_at: opts.created_at || new Date().toISOString(), last_sign_in_at: new Date().toISOString(),
      app_metadata: { providers: ["email"] } };
    this.users.push(u);
    this.tokens[token] = u;
    this.tables.profiles.push({ id, plan: "free" });
    return { id, token };
  }

  tokenFor(id: string, iatMs = Date.now()): string {
    const u = this.users.find((x) => x.id === id)!;
    const token = "h." + btoa(JSON.stringify({ sub: id, iat: Math.floor(iatMs / 1000) })).replace(/=+$/, "") + ".s" +
      Math.random().toString(36).slice(2);
    this.tokens[token] = u;
    return token;
  }

  reply(content: string, usage: Row | null = { prompt_tokens: 1000, completion_tokens: 200 }, tool_calls?: Row[]): Response {
    const message: Row = { role: "assistant", content };
    if (tool_calls) message.tool_calls = tool_calls;
    return new Response(JSON.stringify({ id: "x", object: "chat.completion", choices: [{ index: 0, message,
      finish_reason: tool_calls ? "tool_calls" : "stop" }], ...(usage ? { usage } : {}) }),
      { headers: { "Content-Type": "application/json" } });
  }

  streamReply(text: string, usage: Row | null = { prompt_tokens: 500, completion_tokens: 100 }): Response {
    const enc = new TextEncoder();
    const parts = [...text.match(/.{1,5}/g) || []].map((t) => `data: ${JSON.stringify({ choices: [{ index: 0, delta: { content: t } }] })}\n\n`);
    if (usage) parts.push(`data: ${JSON.stringify({ choices: [], usage })}\n\n`);
    parts.push("data: [DONE]\n\n");
    return new Response(new ReadableStream({
      start(c) {
        for (const p of parts) c.enqueue(enc.encode(p));
        c.close();
      },
    }), { headers: { "Content-Type": "text/event-stream" } });
  }

  // ── the router the function's fetch is pointed at ──
  route = async (input: string | URL | Request, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    const method = (init.method || "GET").toUpperCase();
    const headers: Record<string, string> = {};
    new Headers(init.headers || {}).forEach((v, k) => (headers[k] = v));
    const body = typeof init.body === "string" ? init.body : "";
    this.sent.push({ url: url.href, method, headers, body });
    if (url.href.startsWith(PROV)) {
      const key = (headers.authorization || "").replace(/^Bearer /i, "");
      return await this.provider(body ? JSON.parse(body) : {}, key);
    }
    if (url.host === "ipapi.co" || url.host === "ip-api.com") {
      this.geoCalls++;
      return Response.json({ country_code: "NG", region: "Lagos", countryCode: "NG", regionName: "Lagos" });
    }
    if (!url.href.startsWith(SB)) return new Response("unknown host " + url.host, { status: 599 });
    if (url.pathname === "/auth/v1/user") {
      const t = (headers.authorization || "").replace(/^Bearer /i, "");
      const u = this.tokens[t];
      return u ? Response.json(u) : Response.json({ msg: "invalid token" }, { status: 401 });
    }
    if (url.pathname.startsWith("/auth/v1/admin/users")) return this.adminUsers(url, method, headers);
    if (url.pathname.startsWith("/rest/v1/")) return this.rest(url, method, headers, body);
    return new Response("not found", { status: 404 });
  };

  private service(headers: Record<string, string>): boolean {
    return (headers.authorization || "") === "Bearer service-key" && headers.apikey === "service-key";
  }

  private adminUsers(url: URL, method: string, headers: Record<string, string>): Response {
    if (!this.service(headers)) return Response.json({ msg: "forbidden" }, { status: 403 });
    const id = url.pathname.split("/")[5];
    if (!id) {
      const page = Number(url.searchParams.get("page") || 1), per = Number(url.searchParams.get("per_page") || 50);
      return Response.json({ users: this.users.slice((page - 1) * per, page * per) });
    }
    const u = this.users.find((x) => x.id === id);
    if (!u) return Response.json({ msg: "not found" }, { status: 404 });
    if (method === "DELETE") {
      this.users = this.users.filter((x) => x.id !== id);
      for (const t of ["devices", "user_controls", "user_notes", "events", "profiles"]) {
        this.tables[t] = this.tables[t].filter((r) => r.user_id !== id && r.id !== id);
      }
      return Response.json({});
    }
    return Response.json(u);
  }

  private match(r: Row, key: string, cond: string): boolean {
    const dot = cond.indexOf(".");
    const op = cond.slice(0, dot), val = cond.slice(dot + 1);
    const v = r[key];
    const cmp = (x: any) => (typeof v === "number" ? Number(x) : x);
    switch (op) {
      case "eq": return String(v) === val;
      case "neq": return String(v) !== val;
      case "gte": return v !== undefined && v !== null && v >= cmp(val);
      case "lte": return v !== undefined && v !== null && v <= cmp(val);
      case "gt": return v !== undefined && v !== null && v > cmp(val);
      case "lt": return v !== undefined && v !== null && v < cmp(val);
      case "is": return val === "null" ? v === null || v === undefined : String(v) === val;
      case "in": {
        const items = val.replace(/^\(|\)$/g, "").split(",").map((s) => s.replace(/^"|"$/g, ""));
        return items.includes(String(v));
      }
      default: throw new Error("fake PostgREST: unsupported operator " + op);
    }
  }

  private rest(url: URL, method: string, headers: Record<string, string>, body: string): Response {
    if (!this.service(headers)) return Response.json({ message: "permission denied" }, { status: 401 });
    const table = url.pathname.slice("/rest/v1/".length);
    if (!(table in this.tables)) return Response.json({ message: `relation "${table}" does not exist` }, { status: 404 });
    const rows = this.tables[table];
    const special = new Set(["select", "order", "limit", "offset", "on_conflict"]);
    const filters: Array<[string, string]> = [];
    url.searchParams.forEach((v, k) => { if (!special.has(k)) filters.push([k, v]); });
    const pick = (r: Row) => filters.every(([k, v]) => this.match(r, k, v));
    const prefer = headers.prefer || "";
    const now = new Date().toISOString();
    const defaults = (r: Row): Row => {
      const d: Row = { ...r };
      if (["usage_ledger", "audit_log", "admin_ai_messages", "events", "agent_runs", "failure_reports"].includes(table) && d.id === undefined) {
        d.id = (this.ids[table] = (this.ids[table] || 0) + 1);
      }
      if (table === "admin_keys" && !d.id) d.id = crypto.randomUUID();
      for (const c of { usage_ledger: ["at"], audit_log: ["at"], admin_ai_messages: ["at"], events: ["created_at"],
        failure_reports: ["received_at"], agent_runs: ["received_at"], devices: ["first_seen", "last_seen"],
        admin_keys: ["created_at"], admins: ["added_at"] }[table] || []) if (d[c] === undefined) d[c] = now;
      if (table === "usage_ledger") {
        d.status ??= "ok"; d.cost_usd ??= 0; d.input_tokens ??= 0; d.output_tokens ??= 0; d.cached_tokens ??= 0;
      }
      return d;
    };
    if (method === "GET") {
      let out = rows.filter(pick);
      const order = url.searchParams.get("order");
      if (order) {
        const [col, dir] = order.split(".");
        out = [...out].sort((a, b) => (a[col] > b[col] ? 1 : a[col] < b[col] ? -1 : 0) * (dir === "desc" ? -1 : 1));
      }
      const total = out.length;
      const off = Number(url.searchParams.get("offset") || 0);
      const lim = url.searchParams.get("limit");
      out = out.slice(off, lim ? off + Number(lim) : undefined);
      const sel = url.searchParams.get("select") || "*";
      if (sel !== "*") {
        const cols = sel.split(",");
        out = out.map((r) => Object.fromEntries(cols.map((c) => [c, r[c]])));
      }
      const h: Record<string, string> = { "Content-Type": "application/json" };
      if (prefer.includes("count=exact")) h["Content-Range"] = `0-${Math.max(0, out.length - 1)}/${total}`;
      return new Response(JSON.stringify(out), { headers: h });
    }
    if (method === "POST") {
      const data = JSON.parse(body || "[]");
      const list: Row[] = Array.isArray(data) ? data : [data];
      const conflict = url.searchParams.get("on_conflict");
      const done: Row[] = [];
      for (const item of list) {
        const ex = conflict ? rows.find((r) => String(r[conflict]) === String(item[conflict])) : null;
        if (ex && prefer.includes("merge-duplicates")) {
          Object.assign(ex, item);
          done.push(ex);
        } else {
          if (table === "admin_keys" && rows.some((r) => r.hash === item.hash)) {
            return Response.json({ message: "duplicate key" }, { status: 409 });
          }
          const d = defaults(item);
          rows.push(d);
          done.push(d);
        }
      }
      return Response.json(done);
    }
    if (method === "PATCH") {
      const patch = JSON.parse(body || "{}");
      const hit = rows.filter(pick);
      for (const r of hit) Object.assign(r, patch);
      return Response.json(hit);
    }
    if (method === "DELETE") {
      const hit = rows.filter(pick);
      this.tables[table] = rows.filter((r) => !pick(r));
      return Response.json(hit);
    }
    return new Response("method", { status: 405 });
  }
}
