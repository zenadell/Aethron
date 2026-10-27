// The admin & control edge function, end to end against a stand-in Supabase.
// Run: bun test tests/cloud
// deno-lint-ignore-file no-explicit-any
import { beforeEach, describe, expect, test } from "bun:test";
import { Fake, PROV, SB } from "./fake_supabase.ts";

const G: any = globalThis;
let fake: Fake;
let handle: (r: Request) => Promise<Response>;
let forget: (p?: string) => void;

const BASE = "https://fn.test/functions/v1/aethron";
const MASTER = "master-key-0123456789abcdefghij";
const OUR_KEY = "AIzaOURKEY-1234567890";

async function call(method: string, path: string, opts: { token?: string; body?: any; headers?: Record<string, string> } = {}) {
  const h: Record<string, string> = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (opts.token) h.Authorization = "Bearer " + opts.token;
  const r = await handle(new Request(BASE + path, { method, headers: h,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body) }));
  const t = await r.text();
  let j: any = null;
  try { j = JSON.parse(t); } catch (_) { j = t; }
  await Promise.all(G.__AE_PENDING || []);
  G.__AE_PENDING = [];
  return { status: r.status, body: j, text: t, headers: r.headers };
}

const tool = (name: string, args: any = {}, token = MASTER) => call("POST", "/admin/call", { token, body: { tool: name, args } });
const ping = (body: any, token?: string, headers: Record<string, string> = {}) =>
  call("POST", "/ping", { token, body: { machine: "machine-abc-001", version: "1.1.60", os: "macOS 15", arch: "arm64", ...body },
    headers: { "x-forwarded-for": "102.89.1.7", ...headers } });

beforeEach(async () => {
  fake = new Fake();
  G.__AE_FETCH = fake.route;
  G.__AE_ENV = { SUPABASE_URL: SB, SUPABASE_SERVICE_ROLE_KEY: "service-key", SUPABASE_ANON_KEY: "anon-key",
    ADMIN_EMAILS: "owner@jomiez.com", ADMIN_MASTER_KEY: MASTER };
  G.__AE_PENDING = [];
  const app = await import("../../supabase/functions/aethron/app.ts");
  const st = await import("../../supabase/functions/aethron/state.ts");
  handle = app.handle;
  forget = st.forget;
  forget("");
  fake.tables.secrets.push({ name: "base_gemini", value: PROV });
});

async function managed(model = "gemini-3.6-flash", keys = OUR_KEY) {
  await tool("set_provider_key", { provider: "gemini", keys, base_url: PROV });
  await tool("set_ai_mode", { mode: "managed", provider: "gemini", model });
  forget("");
}

async function deviceFor(email: string) {
  const u = fake.addUser(email);
  const r = await ping({}, u.token);
  return { ...u, device: r.body.device_token as string };
}

describe("public switches and the heartbeat", () => {
  test("anyone can read the switches, and they carry nothing secret", async () => {
    const r = await call("GET", "/control");
    expect(r.status).toBe(200);
    expect(r.body.paused).toBe(false);
    expect(r.body).not.toHaveProperty("prices");
    expect(JSON.stringify(r.body)).not.toContain("key");
  });

  test("a signed-out copy is placed by country, and its address is never stored", async () => {
    const r = await ping({});
    expect(r.status).toBe(200);
    const d = fake.tables.devices[0];
    expect(d.country).toBe("NG");
    expect(d.region).toBe("Lagos");
    expect(JSON.stringify(fake.tables.devices)).not.toContain("102.89.1.7");
    expect(d.ip_hash.length).toBeGreaterThan(10);
    expect(r.body.device_token).toBeUndefined();
  });

  test("the country comes from a header when the platform gives one, with no lookup", async () => {
    await ping({}, undefined, { "cf-ipcountry": "GH" });
    expect(fake.tables.devices[0].country).toBe("GH");
    expect(fake.geoCalls).toBe(0);
  });

  test("a signed-in copy gets a device token; the same address is not looked up twice", async () => {
    const u = fake.addUser("ada@example.com");
    const r1 = await ping({ activity: { view: "project", job: "build", project: "acme" } }, u.token);
    expect(r1.body.device_token).toMatch(/^adt_/);
    expect(r1.body.me.email).toBe("ada@example.com");
    const r2 = await ping({}, r1.body.device_token);
    expect(r2.status).toBe(200);
    expect(r2.body.device_token).toBeUndefined();
    expect(r2.body.me.user_id).toBe(u.id);
    expect(fake.geoCalls).toBe(1);
    expect(fake.tables.devices[0].token_hash).not.toContain("adt_");
  });

  test("an old version is told it must update", async () => {
    await tool("set_min_version", { version: "1.1.70" });
    forget("");
    const u = fake.addUser("old@example.com");
    const r = await ping({ version: "1.1.60" }, u.token);
    expect(r.body.me.update_required).toBe(true);
    expect(r.body.control.min_version).toBe("1.1.70");
  });
});

describe("activity events", () => {
  test("a copy sends events on its device token; a stranger cannot", async () => {
    const d = await deviceFor("ada@example.com");
    const r = await call("POST", "/event", { token: d.device, body: { events: [{ event: "build", props: { ok: true } }] } });
    expect(r.status).toBe(200);
    expect(fake.tables.events[0].user_id).toBe(d.id);
    expect(fake.tables.events[0].props.machine).toBe("machine-abc-001");
    expect((await call("POST", "/event", { body: { events: [{ event: "x" }] } })).status).toBe(401);
  });
});

describe("who may run the admin", () => {
  test("a normal account is refused; the allow-listed owner and the master key get in", async () => {
    const u = fake.addUser("someone@example.com");
    expect((await call("GET", "/admin/me", { token: u.token })).status).toBe(401);
    expect((await call("GET", "/admin/me")).status).toBe(401);
    const o = fake.addUser("owner@jomiez.com");
    const me = await call("GET", "/admin/me", { token: o.token });
    expect(me.body.role).toBe("owner");
    expect((await call("GET", "/admin/me", { token: MASTER })).body.role).toBe("owner");
  });

  test("a device token is never an admin credential", async () => {
    const o = fake.addUser("owner@jomiez.com");
    const r = await ping({}, o.token);
    expect((await call("GET", "/admin/me", { token: r.body.device_token })).status).toBe(401);
  });

  test("a read-only API key reads and cannot change anything; a revoked key cannot even read", async () => {
    const k = (await tool("create_api_key", { label: "claude", scopes: ["read"] })).body.result.key;
    expect(k).toMatch(/^ak_/);
    expect((await tool("overview", {}, k)).status).toBe(200);
    const w = await tool("pause_all", {}, k);
    expect(w.status).toBe(403);
    expect(w.body.error.message).toContain("write");
    const listed = (await tool("list_api_keys")).body.result;
    expect(JSON.stringify(listed)).not.toContain(k);
    expect(listed[0]).not.toHaveProperty("hash");
    await tool("revoke_api_key", { id: listed[0].id });
    expect((await tool("overview", {}, k)).status).toBe(401);
  });

  test("a viewer admin reads but cannot write", async () => {
    await tool("add_admin", { email: "viewer@jomiez.com", role: "viewer" });
    const v = fake.addUser("viewer@jomiez.com");
    expect((await tool("list_users", {}, v.token)).status).toBe(200);
    expect((await tool("block_user", { user: "x" }, v.token)).status).toBe(403);
  });
});

describe("the kill switch and the switches", () => {
  test("pause_all stops the managed AI and every copy hears it on its next check-in", async () => {
    await managed();
    const d = await deviceFor("ada@example.com");
    await tool("pause_all", { message: "Back in 10 minutes" });
    forget("");
    const g = await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [{ role: "user", content: "hi" }] } });
    expect(g.status).toBe(403);
    expect(g.body.error.message).toBe("Back in 10 minutes");
    expect(fake.sent.some((s) => s.url.startsWith(PROV))).toBe(false);
    const p = await ping({}, d.device);
    expect(p.body.control.paused).toBe(true);
    await tool("resume_all");
    forget("");
    expect((await call("GET", "/control")).body.paused).toBe(false);
  });

  test("features switch off one part at a time, and unknown ones are refused", async () => {
    await tool("set_feature", { feature: "mcp", on: false });
    forget("");
    const c = (await call("GET", "/control")).body;
    expect(c.features.mcp).toBe(false);
    expect(c.features.agent).toBe(true);
    expect((await tool("set_feature", { feature: "teleport", on: false })).status).toBe(400);
  });

  test("a banner reaches every copy", async () => {
    await tool("set_banner", { text: "Beta maintenance at 18:00 UTC", level: "warn" });
    forget("");
    const c = (await call("GET", "/control")).body;
    expect(c.banner).toBe("Beta maintenance at 18:00 UTC");
    expect(c.banner_level).toBe("warn");
  });
});

describe("the managed AI", () => {
  test("off by default: a person keeps using their own key", async () => {
    const d = await deviceFor("ada@example.com");
    const g = await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [] } });
    expect(g.status).toBe(403);
    expect(g.body.error.message).toContain("own key");
  });

  test("managed: the admin's key and model are used, the person's request is metered", async () => {
    await managed("gemini-3.6-flash");
    const d = await deviceFor("ada@example.com");
    let seen: any = null, key = "";
    fake.provider = (b, k) => { seen = b; key = k; return fake.reply("hi there"); };
    const g = await call("POST", "/v1/chat/completions", { token: d.device,
      body: { model: "whatever-the-app-asked", messages: [{ role: "user", content: "hi" }] },
      headers: { "x-aethron-purpose": "agent" } });
    expect(g.status).toBe(200);
    expect(g.body.choices[0].message.content).toBe("hi there");
    expect(key).toBe(OUR_KEY);
    expect(seen.model).toBe("gemini-3.6-flash");
    const row = fake.tables.usage_ledger[0];
    expect(row.user_id).toBe(d.id);
    expect(row.input_tokens).toBe(1000);
    expect(row.output_tokens).toBe(200);
    expect(row.cost_usd).toBeCloseTo((1000 * 0.75 + 200 * 3.75) / 1e6, 8);
    expect(row.estimated).toBe(false);
  });

  test("a streamed answer passes through untouched and its usage is still recorded", async () => {
    await managed();
    const d = await deviceFor("ada@example.com");
    let asked: any = null;
    fake.provider = (b) => { asked = b; return fake.streamReply("streamed words here"); };
    const g = await call("POST", "/v1/chat/completions", { token: d.device, body: { stream: true, messages: [] } });
    expect(g.status).toBe(200);
    expect(g.text).toContain("data: [DONE]");
    expect(asked.stream_options).toEqual({ include_usage: true });
    expect(fake.tables.usage_ledger[0].input_tokens).toBe(500);
  });

  test("an answer with no usage is estimated, and says so", async () => {
    await managed();
    const d = await deviceFor("ada@example.com");
    fake.provider = () => fake.reply("no usage here", null);
    await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [{ role: "user", content: "x".repeat(400) }] } });
    const row = fake.tables.usage_ledger[0];
    expect(row.estimated).toBe(true);
    expect(row.input_tokens).toBeGreaterThan(90);
  });

  test("a busy key gives way to the next one", async () => {
    await managed("gemini-3.6-flash", "key-one-busy\nkey-two-good");
    const d = await deviceFor("ada@example.com");
    const used: string[] = [];
    fake.provider = (_b, k) => { used.push(k); return k === "key-one-busy" ? new Response("busy", { status: 429 }) : fake.reply("ok"); };
    const g = await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [] } });
    expect(g.status).toBe(200);
    expect(used).toEqual(["key-one-busy", "key-two-good"]);
  });

  test("a person's daily allowance and the global limit are enforced before the provider is asked", async () => {
    await managed();
    const d = await deviceFor("ada@example.com");
    await tool("set_user_limit", { user: "ada@example.com", daily_usd: 0.001 });
    forget("");
    fake.provider = () => fake.reply("big", { prompt_tokens: 100000, completion_tokens: 10000 });
    expect((await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [] } })).status).toBe(200);
    const asked = fake.sent.filter((s) => s.url.startsWith(PROV)).length;
    const r = await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [] } });
    expect(r.status).toBe(403);
    expect(r.body.error.message).toContain("allowance");
    expect(fake.sent.filter((s) => s.url.startsWith(PROV)).length).toBe(asked);
    await tool("set_user_limit", { user: "ada@example.com", daily_usd: 0 });
    await tool("set_limits", { global_daily_usd: 0.0001 });
    forget("");
    const g = await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [] } });
    expect(g.status).toBe(403);
    expect(g.body.error.message).toContain("limit for today");
  });

  test("a refusal is never a 429 (the agent engine retries those in silence)", async () => {
    await managed();
    const d = await deviceFor("ada@example.com");
    await tool("block_user", { user: "ada@example.com", reason: "testing" });
    forget("");
    const r = await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [] } });
    expect(r.status).toBe(403);
    expect(r.body.error.message).toContain("on hold: testing");
    expect((await ping({}, d.device)).body.me.blocked).toBe(true);
  });

  test("the admin's key never reaches a person, a log or a listing", async () => {
    await managed();
    const d = await deviceFor("ada@example.com");
    const outs = [
      await ping({}, d.device), await call("GET", "/control"), await tool("list_models"), await tool("audit_log"),
      await tool("overview"), await tool("get_user", { user: "ada@example.com" }),
      await call("GET", "/admin/export?kind=all&format=json", { token: MASTER }),
    ];
    for (const o of outs) expect(o.text).not.toContain(OUR_KEY);
    expect((await tool("list_models")).body.result.providers.find((p: any) => p.id === "gemini").keys[0]).toBe("AIza…7890");
  });
});

describe("people", () => {
  test("sign_out_user cuts off every copy, and an access token from before cannot mint a new one", async () => {
    const d = await deviceFor("ada@example.com");
    await tool("sign_out_user", { user: "ada@example.com" });
    forget("");
    const p = await ping({}, d.device);
    expect(p.status).toBe(401);
    expect(p.body.signed_out).toBe(true);
    const oldJwt = fake.tokenFor(d.id, Date.now() - 60_000);
    expect((await ping({}, oldJwt)).status).toBe(401);
    const fresh = fake.tokenFor(d.id, Date.now() + 60_000);
    const again = await ping({}, fresh);
    expect(again.status).toBe(200);
    expect(again.body.device_token).toMatch(/^adt_/);
  });

  test("the users list and one person's page carry country, version, activity and spend", async () => {
    await managed();
    const d = await deviceFor("ada@example.com");
    await ping({ activity: { job: "migrate", project: "acme", agent: true } }, d.device);
    await call("POST", "/v1/chat/completions", { token: d.device, body: { messages: [] } });
    fake.tables.events.push({ id: 1, user_id: d.id, event: "build", props: { ok: true }, created_at: new Date().toISOString() });
    fake.tables.failure_reports.push({ id: 1, received_at: new Date().toISOString(), machine: "machine-abc-001",
      fingerprint: "fp1", kind: "job", step: "build", category: "TypeError", app_version: "1.1.60", record: { log: "boom" } });
    const list = (await tool("list_users", { q: "ada" })).body.result;
    expect(list.total).toBe(1);
    expect(list.users[0].country).toBe("NG");
    expect(list.users[0].online).toBe(true);
    expect(list.users[0].spend_30d).toBeGreaterThan(0);
    const one = (await tool("get_user", { user: "ada@example.com" })).body.result;
    expect(one.devices[0].activity.job).toBe("migrate");
    expect(one.devices[0]).not.toHaveProperty("token_hash");
    expect(one.failures.length).toBe(1);
    expect(one.events.length).toBe(1);
    expect(one.spend.requests_90d).toBe(1);
    expect((await tool("online_users")).body.result.length).toBe(1);
  });

  test("deleting an account needs its email typed back", async () => {
    fake.addUser("gone@example.com");
    const r = await tool("delete_user", { user: "gone@example.com", confirm: "yes" });
    expect(r.status).toBe(400);
    expect((await tool("delete_user", { user: "gone@example.com", confirm: "gone@example.com" })).status).toBe(200);
    expect(fake.users.find((u) => u.email === "gone@example.com")).toBeUndefined();
  });

  test("every change is in the audit log, with who did it and how", async () => {
    fake.addUser("ada@example.com");
    await tool("block_user", { user: "ada@example.com" });
    const log = (await tool("audit_log")).body.result;
    expect(log.some((r: any) => r.action === "block_user" && r.actor === "master key" && r.target === "ada@example.com")).toBe(true);
  });
});

describe("errors and exports", () => {
  test("failure reports group by fingerprint and the Markdown export carries their logs", async () => {
    const t = new Date().toISOString();
    for (let i = 0; i < 3; i++) {
      fake.tables.failure_reports.push({ id: i + 1, received_at: t, machine: "m" + i, fingerprint: "abc123", kind: "job",
        step: "build", category: "ChunkLoadError", app_version: "1.1.55", source_url: "https://x.framer.website",
        record: { lines: ["FAIL chunk"], log: "the log tail here" } });
    }
    const g = (await tool("errors", { days: 1 })).body.result;
    expect(g[0].count).toBe(3);
    expect(g[0].machines).toBe(3);
    const md = await call("GET", "/admin/export?kind=errors&format=md&days=1", { token: MASTER });
    expect(md.headers.get("content-disposition")).toContain(".md");
    expect(md.text).toContain("ChunkLoadError");
    expect(md.text).toContain("the log tail here");
    const csv = await call("GET", "/admin/export?kind=errors&format=csv&days=1", { token: MASTER });
    expect(csv.text.split("\n")[0]).toContain("fingerprint");
  });
});

describe("the admin AI and the admin MCP", () => {
  test("the admin AI reads through the tools and answers with real figures", async () => {
    await tool("set_provider_key", { provider: "gemini", keys: OUR_KEY, base_url: PROV });
    await tool("set_admin_ai", { provider: "gemini", model: "gemini-3.6-flash", key: "admin-ai-own-key-123" });
    forget("");
    fake.addUser("ada@example.com");
    let step = 0;
    const keys: string[] = [];
    fake.provider = (b, k) => {
      keys.push(k);
      step++;
      if (step === 1) {
        expect(b.tools.some((t: any) => t.function.name === "pause_all")).toBe(true);
        return fake.reply("", { prompt_tokens: 10, completion_tokens: 5 },
          [{ id: "c1", type: "function", function: { name: "overview", arguments: "{}" } }]);
      }
      const toolMsg = b.messages.find((m: any) => m.role === "tool");
      expect(toolMsg.content).toContain('"total":1');
      return fake.reply("You have 1 user.");
    };
    const r = await call("POST", "/admin/ai", { token: MASTER, body: { message: "how many users?" } });
    expect(r.status).toBe(200);
    expect(r.body.reply).toBe("You have 1 user.");
    expect(r.body.actions[0].tool).toBe("overview");
    expect(keys.every((k) => k === "admin-ai-own-key-123")).toBe(true);
    const thread = (await call("GET", "/admin/ai/thread?id=" + r.body.thread, { token: MASTER })).body;
    expect(thread.map((m: any) => m.role)).toEqual(["user", "assistant", "tool", "assistant"]);
  });

  test("the admin AI acts with the asker's permissions, and its changes are audited as the AI", async () => {
    await tool("set_admin_ai", { provider: "gemini", model: "gemini-3.6-flash", key: "admin-ai-own-key-123" });
    forget("");
    const k = (await tool("create_api_key", { label: "reader", scopes: ["read"] })).body.result.key;
    let step = 0;
    fake.provider = (b) => {
      step++;
      if (step === 1) {
        expect(b.tools.some((t: any) => t.function.name === "pause_all")).toBe(false);
        return fake.reply("", null, [{ id: "c1", type: "function", function: { name: "pause_all", arguments: "{}" } }]);
      }
      return fake.reply("I could not pause it.");
    };
    const r = await call("POST", "/admin/ai", { token: k, body: { message: "pause everything" } });
    expect(r.body.actions[0].ok).toBe(false);
    forget("");
    expect((await call("GET", "/control")).body.paused).toBe(false);

    step = 0;
    fake.provider = () => (++step === 1
      ? fake.reply("", null, [{ id: "c2", type: "function", function: { name: "set_banner", arguments: '{"text":"hi all"}' } }])
      : fake.reply("Banner set."));
    await call("POST", "/admin/ai", { token: MASTER, body: { message: "set a banner saying hi all" } });
    const log = (await tool("audit_log")).body.result;
    expect(log.find((x: any) => x.action === "set_banner").via).toBe("ai");
  });

  test("a coding agent connects over MCP with an API key, lists tools for its scope and calls them", async () => {
    const k = (await tool("create_api_key", { label: "codex", scopes: ["read", "write"] })).body.result.key;
    const init = await call("POST", "/mcp", { token: k, body: { jsonrpc: "2.0", id: 1, method: "initialize", params: {} } });
    expect(init.body.result.serverInfo.name).toBe("aethron-admin");
    expect(init.headers.get("mcp-session-id")).toBeTruthy();
    const list = await call("POST", "/mcp", { token: k, body: { jsonrpc: "2.0", id: 2, method: "tools/list" } });
    const names = list.body.result.tools.map((t: any) => t.name);
    expect(names).toContain("pause_all");
    expect(names).toContain("ask_admin_ai");
    expect(names).not.toContain("set_provider_key");
    const ov = await call("POST", "/mcp", { token: k, body: { jsonrpc: "2.0", id: 3, method: "tools/call",
      params: { name: "overview", arguments: {} } } });
    expect(ov.body.result.structuredContent.users.total).toBe(0);
    const owner = await call("POST", "/mcp", { token: k, body: { jsonrpc: "2.0", id: 4, method: "tools/call",
      params: { name: "set_provider_key", arguments: { provider: "gemini", keys: "x" } } } });
    expect(owner.body.result.isError).toBe(true);
    const note = await call("POST", "/mcp", { token: k, body: { jsonrpc: "2.0", method: "notifications/initialized" } });
    expect(note.status).toBe(202);
    expect((await call("POST", "/mcp", { body: { jsonrpc: "2.0", id: 5, method: "tools/list" } })).status).toBe(401);
  });
});

describe("announcements and their illustrations", () => {
  const GOOD = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 206"><style>.a{animation:p 2s infinite}@keyframes p{50%{opacity:.4}}</style>
<rect width="400" height="206" fill="#0f0e10"/><circle class="a" cx="200" cy="103" r="30" fill="#d97757"/></svg>`;

  test("a drawing is refused for scripts, handlers, outside addresses and foreign elements — with every reason", async () => {
    const art = await import("../../supabase/functions/aethron/art.ts");
    expect(art.checkSvg("Here you go:\n```svg\n" + GOOD + "\n```")).toContain("<circle");
    for (const [bad, why] of [
      [GOOD.replace("<rect", '<script>alert(1)</script><rect'), "<script> is not allowed"],
      [GOOD.replace("<rect", '<rect onload="x()"'), "event handlers"],
      [GOOD.replace("<rect", '<image href="https://evil.test/x.png"/><rect'), "links outside itself"],
      [GOOD.replace("<rect", '<foreignObject></foreignObject><rect'), "<foreignObject> is not allowed"],
      [GOOD.replace(".a{", ".a{background:url(https://evil.test/t.png);"), "outside address"],
      [GOOD.replace("0 0 400 206", "0 0 100 100"), "viewBox"],
    ] as const) {
      let msg = "";
      try { art.checkSvg(bad); } catch (e) { msg = (e as Error).message; }
      expect(msg).toContain(why);
    }
    for (const t of Object.values(art.TEMPLATES)) expect(art.checkSvg(t)).toContain("@keyframes");
  });

  test("with no admin AI, publishing still ships a house illustration, and every copy sees the card", async () => {
    const r = await tool("publish_announcement", { title: "Scheduled maintenance tonight", body: "Aethron pauses at 22:00 UTC for ten minutes." });
    expect(r.status).toBe(200);
    expect(r.body.result.art).toBe("template");
    forget("");
    const c = (await call("GET", "/control")).body;
    expect(c.announcement.title).toBe("Scheduled maintenance tonight");
    expect(c.announcement.has_art).toBe(true);
    expect(JSON.stringify(c)).not.toContain("<svg");           // the minute-by-minute check stays small
    const art = await call("GET", "/announcement/art?id=" + c.announcement.id);
    expect(art.status).toBe(200);
    expect(art.headers.get("content-type")).toContain("image/svg+xml");
    expect(art.headers.get("content-security-policy")).toContain("default-src 'none'");
    expect(art.text).toContain("ring");                          // the maintenance drawing
    const u = fake.addUser("ann@example.com");
    expect((await ping({}, u.token)).body.control.announcement.id).toBe(c.announcement.id);
  });

  test("the admin AI draws it; a refused drawing is sent back with the reasons, and the fixed one ships", async () => {
    await tool("set_provider_key", { provider: "gemini", keys: OUR_KEY, base_url: PROV });
    await tool("set_admin_ai", { provider: "gemini", model: "gemini-3.6-flash" });
    forget("");
    const asks: any[] = [];
    fake.provider = (b) => {
      asks.push(b);
      return fake.reply(asks.length === 1 ? GOOD.replace("<rect", "<script>x()</script><rect") : GOOD);
    };
    const r = await tool("publish_announcement", { title: "Screenshots now build whole sites", body: "Drop a screenshot.",
      cta_label: "See how", cta_url: "https://aethron.jomiez.com/docs" });
    expect(r.body.result.art).toBe("ai");
    expect(asks.length).toBe(2);
    expect(asks[0].messages[0].content).toContain("illustrator for Aethron");
    expect(asks[1].messages.at(-1).content).toContain("<script> is not allowed");
    forget("");
    const c = (await call("GET", "/control")).body.announcement;
    expect(c.cta_url).toBe("https://aethron.jomiez.com/docs");
    expect((await call("GET", "/announcement/art?id=" + c.id)).text).not.toContain("<script");
    expect(fake.tables.usage_ledger.some((x: any) => x.purpose === "admin_art")).toBe(true);
  });

  test("two refused drawings fall back to the house style; a bad button address is refused; ending clears it", async () => {
    await tool("set_provider_key", { provider: "gemini", keys: OUR_KEY, base_url: PROV });
    await tool("set_admin_ai", { provider: "gemini", model: "gemini-3.6-flash" });
    forget("");
    fake.provider = () => fake.reply('<svg viewBox="0 0 400 206"><image href="https://x.test/a.png"/></svg>');
    const r = await tool("publish_announcement", { title: "Heads up", body: "Prices change next month.", level: "warn" });
    expect(r.body.result.art).toBe("template");
    expect(r.body.result.note).toContain("refused twice");
    const bad = await tool("publish_announcement", { title: "x", cta_url: "javascript:alert(1)", art: "none" });
    expect(bad.status).toBe(400);
    const prev = await tool("preview_illustration", { title: "New: Publish", body: "One click to the web." });
    expect(prev.body.result.svg).toContain("<svg");
    await tool("end_announcement");
    forget("");
    expect((await call("GET", "/control")).body.announcement).toBeNull();
    const list = (await tool("list_announcements")).body.result;
    expect(list.length).toBe(1);
    expect(fake.tables.audit_log.some((x: any) => x.action === "end_announcement")).toBe(true);
  });
});

describe("a drawing must stand still", () => {
  test("hidden-until-animated is refused; gradient stops at 0 are fine; every house drawing passes", async () => {
    const art = await import("../../supabase/functions/aethron/art.ts");
    const base = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 206"><defs><linearGradient id="ae-g"><stop offset="0" stop-opacity="0"/></linearGradient></defs><style>X</style><rect class="c" width="10" height="10"/></svg>`;
    let msg = "";
    try { art.checkSvg(base.replace("X", ".c{opacity:0;animation:in 1s forwards}@keyframes in{to{opacity:1}}")); } catch (e) { msg = (e as Error).message; }
    expect(msg).toContain("with motion off it never appears");
    expect(art.checkSvg(base.replace("X", ".c{animation:in 1s both}@keyframes in{from{opacity:0}to{opacity:1}}"))).toContain("<rect");
    for (const t of Object.values(art.TEMPLATES)) expect(art.checkSvg(t)).toContain("<svg");
  });
});
