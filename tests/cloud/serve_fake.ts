// The real edge function on a local port, over the stand-in Supabase, with
// a few people, copies, spend and errors seeded — for the console's browser
// test (tests/admin_console_battery.py) and for trying the console by hand:
//   bun tests/cloud/serve_fake.ts 8787  ->  serves the function AND the console
// deno-lint-ignore-file no-explicit-any
import { Fake, PROV, SB } from "./fake_supabase.ts";

const G: any = globalThis;
const port = Number(process.argv[2] || 8787);
const fake = new Fake();
G.__AE_FETCH = fake.route;
G.__AE_ENV = { SUPABASE_URL: SB, SUPABASE_SERVICE_ROLE_KEY: "service-key", SUPABASE_ANON_KEY: "anon-key",
  ADMIN_EMAILS: "owner@jomiez.com", ADMIN_MASTER_KEY: "master-key-0123456789abcdefghij" };
const { handle } = await import("../../supabase/functions/aethron/app.ts");

const now = Date.now();
const iso = (msAgo: number) => new Date(now - msAgo).toISOString();
const people = [["ada@example.com", "NG", "Lagos", 20_000], ["bo@example.com", "GH", "Accra", 3 * 3600_000],
  ["cy@example.com", "US", "California", 40_000], ["dee@example.com", "GB", "England", 9 * 86400_000]];
people.forEach(([email, country, region, seen], i) => {
  const u = fake.addUser(String(email), { created_at: iso((i + 1) * 86400_000) });
  fake.tables.devices.push({ machine: "m-seed" + i, user_id: u.id, email, first_seen: iso(30 * 86400_000), last_seen: iso(Number(seen)),
    version: i === 3 ? "1.1.40" : "1.1.60", os: "Darwin 24.1", arch: "arm64", country, region, ip_hash: "h" + i,
    activity: i === 0 ? { job: "build", project: "agero", agent: true, agent_in: "agero", jobs: 1, projects: 4 } : { projects: 2 } });
  for (let k = 0; k < 5; k++) {
    fake.tables.usage_ledger.push({ id: fake.tables.usage_ledger.length + 1, at: iso(k * 86400_000 + i * 1000), user_id: u.id,
      provider: "gemini", model: "gemini-3.6-flash", purpose: k % 2 ? "agent" : "fill", input_tokens: 20000, output_tokens: 3000,
      cached_tokens: 0, cost_usd: 0.0262, status: "ok", ms: 4000, estimated: false });
  }
  fake.tables.events.push({ id: i + 1, user_id: u.id, event: "project_created", props: { platform: "framer" }, created_at: iso(i * 3600_000) });
});
fake.tables.failure_reports.push({ id: 1, received_at: iso(3600_000), machine: "m-seed0", fingerprint: "a1b2c3d4", kind: "job",
  step: "build", category: "ChunkLoadError: Loading chunk 42 failed", app_version: "1.1.60", source_url: "https://agero.framer.website",
  record: { lines: ["FAIL /works: chunk 42 404"], log: "$ forge build\n… 404 /assets/chunks/42.mjs", trace: "" } });
fake.tables.secrets.push({ name: "base_gemini", value: PROV });
// the admin AI: one read, then an answer
let step = 0;
fake.provider = (b: any) => {
  if (b.tools && !b.messages.some((m: any) => m.role === "tool") && step++ % 2 === 0) {
    return fake.reply("", { prompt_tokens: 50, completion_tokens: 10 },
      [{ id: "c" + step, type: "function", function: { name: "overview", arguments: "{}" } }]);
  }
  const t = b.messages.filter((m: any) => m.role === "tool").pop();
  const total = t ? (JSON.parse(t.content).users || {}).total : "?";
  return fake.reply(`There are ${total} people on Aethron.`);
};

const consoleHtml = (await Bun.file(new URL("../../admin_ui/console.html", import.meta.url)).text())
  .replace("/*__AE_CONFIG__*/{}", JSON.stringify({ fn: `http://127.0.0.1:${port}/aethron`, supabase: SB, anon: "anon-key" }));

Bun.serve({
  port, hostname: "127.0.0.1",
  async fetch(req) {
    const u = new URL(req.url);
    if (u.pathname === "/" || u.pathname === "/admin/") {
      return new Response(consoleHtml, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }
    if (u.pathname === "/__state") {
      return Response.json({ control: fake.tables.control[0], audit: fake.tables.audit_log, devices: fake.tables.devices,
        usage: fake.tables.usage_ledger, events: fake.tables.events, sent: fake.sent.filter((x) => x.url.startsWith(PROV))
          .map((x) => ({ url: x.url, auth: x.headers.authorization, body: x.body })) });
    }
    if (u.pathname === "/__user") {
      const email = u.searchParams.get("email") || "someone@example.com";
      const old = fake.users.find((x) => x.email === email);
      // a sign-in AFTER now, only when asked: an admin's sign-out refuses tokens issued before it
      const at = u.searchParams.get("fresh") ? Date.now() + 2000 : Date.now();
      if (old) return Response.json({ id: old.id, token: fake.tokenFor(old.id, at) });
      return Response.json(fake.addUser(email));
    }
    const r = await handle(req);
    await Promise.all(G.__AE_PENDING || []);
    G.__AE_PENDING = [];
    return r;
  },
});
console.log("ready " + port);
