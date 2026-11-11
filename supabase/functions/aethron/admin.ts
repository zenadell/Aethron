// Everything the admin can do, as ONE list of tools. The web console, the
// admin AI, admin API keys and the admin MCP server all call these same
// tools, so no surface can do something the others cannot — and every
// change goes through `need()` (permission) and `audit()` (the record).

import { emberSettings } from "./ember.ts";
import { auth, db, Row } from "./db.ts";
import { PROVIDERS } from "./prices.ts";
import { drawIllustration, end as endAnnouncement, list as listAnnouncements, publish as publishAnnouncement } from "./announce.ts";
import {
  Admin, audit, cached, control, forget, keyList, need, ownerEmails, publicControl, Scope, secret, setControl, setSecret,
  spentToday,
} from "./state.ts";
import { clip, daysAgoIso, dayStartIso, fingerprint, HttpError, nowIso, nowMs, num, randomToken, sha256 } from "./util.ts";

export type Tool = {
  name: string;
  scope: Scope;
  description: string;
  params: Row; // JSON-schema "properties"
  required?: string[];
  run: (args: Row, a: Admin) => Promise<unknown>;
};

const ONLINE_MS = 720_000;   // a copy checks in fully every 10 minutes
const S = (description: string, extra: Row = {}) => ({ type: "string", description, ...extra });
const N = (description: string) => ({ type: "number", description });
const B = (description: string) => ({ type: "boolean", description });

// ── reading ─────────────────────────────────────────────────────────

export async function users(fresh = false): Promise<Row[]> {
  if (fresh) forget("users");
  return cached("users", 30_000, () => auth.allUsers());
}

export async function findUser(ref: string): Promise<Row> {
  const r = String(ref || "").trim();
  if (!r) throw new HttpError(400, "name a user by email or id");
  const pick = (all: Row[]) =>
    all.find((x) => x.id === r) || all.find((x) => String(x.email || "").toLowerCase() === r.toLowerCase());
  let all = await users();
  let u = pick(all);
  if (!u) {
    all = await users(true); // someone who signed up since the list was read
    u = pick(all);
  }
  if (u) return u;
  const near = all.filter((x) => String(x.email || "").toLowerCase().includes(r.toLowerCase()));
  if (near.length === 1) return near[0];
  throw new HttpError(404, near.length ? `"${r}" matches ${near.length} users: ${near.slice(0, 5).map((x) => x.email).join(", ")}`
    : `no user "${r}"`);
}

function isOnline(d: Row): boolean {
  return nowMs() - new Date(d.last_seen || 0).getTime() < ONLINE_MS;
}

async function devices(days = 90): Promise<Row[]> {
  return db.select("devices", { last_seen: "gte." + daysAgoIso(days), order: "last_seen.desc", limit: 20000 });
}

async function ledger(days: number, uid = ""): Promise<Row[]> {
  return db.select("usage_ledger", {
    at: "gte." + daysAgoIso(days), ...(uid ? { user_id: "eq." + uid } : {}), order: "at.desc", limit: 50000,
  });
}

function sum(rows: Row[], f = "cost_usd"): number {
  return Math.round(rows.reduce((s, r) => s + num(r[f]), 0) * 10000) / 10000;
}

function groupCount<T>(rows: T[], key: (r: T) => string): Array<{ key: string; count: number }> {
  const m = new Map<string, number>();
  for (const r of rows) {
    const k = key(r) || "unknown";
    m.set(k, (m.get(k) || 0) + 1);
  }
  return [...m.entries()].map(([key, count]) => ({ key, count })).sort((a, b) => b.count - a.count);
}

async function errorGroups(days: number, limit = 50): Promise<Row[]> {
  const rows = await db.select("failure_reports", {
    received_at: "gte." + daysAgoIso(days), order: "received_at.desc", limit: 5000,
    select: "id,received_at,machine,fingerprint,kind,step,category,app_version,source_url",
  });
  const g = new Map<string, Row>();
  for (const r of rows) {
    const x = g.get(r.fingerprint) || {
      fingerprint: r.fingerprint, kind: r.kind, step: r.step, category: r.category, count: 0,
      machines: new Set<string>(), versions: new Set<string>(), first: r.received_at, last: r.received_at,
      source_urls: new Set<string>(),
    };
    x.count++;
    x.machines.add(r.machine);
    if (r.app_version) x.versions.add(r.app_version);
    if (r.source_url) x.source_urls.add(r.source_url);
    if (r.received_at < x.first) x.first = r.received_at;
    if (r.received_at > x.last) x.last = r.received_at;
    g.set(r.fingerprint, x);
  }
  return [...g.values()].sort((a, b) => b.count - a.count).slice(0, limit).map((x) => ({
    ...x, machines: x.machines.size, versions: [...x.versions].slice(0, 6), source_urls: [...x.source_urls].slice(0, 4),
  }));
}

async function overview(): Promise<Row> {
  const [all, devs, c, led30, errs, runs] = await Promise.all([
    users(), devices(30), control(), ledger(30),
    db.count("failure_reports", { received_at: "gte." + daysAgoIso(1) }),
    db.count("agent_runs", { received_at: "gte." + daysAgoIso(1) }),
  ]);
  const t = nowMs();
  const newer = (d: number) => all.filter((u) => t - new Date(u.created_at).getTime() < d * 86400_000).length;
  const online = devs.filter(isOnline);
  const active24 = new Set(devs.filter((d) => t - new Date(d.last_seen).getTime() < 86400_000).map((d) => d.user_id || d.machine));
  const today = led30.filter((r) => r.at >= dayStartIso());
  const led7 = led30.filter((r) => r.at >= daysAgoIso(7));
  return {
    users: { total: all.length, new_24h: newer(1), new_7d: newer(7), new_30d: newer(30) },
    online_now: online.length,
    online_users: new Set(online.map((d) => d.user_id).filter(Boolean)).size,
    active_24h: active24.size,
    countries: groupCount(devs.filter((d) => t - new Date(d.last_seen).getTime() < 7 * 86400_000), (d) => d.country).slice(0, 12),
    versions: groupCount(online.length ? online : devs.slice(0, 200), (d) => d.version).slice(0, 8),
    spend: { today: sum(today), d7: sum(led7), d30: sum(led30), requests_today: today.length,
      errors_today: today.filter((r) => r.status !== "ok").length },
    errors_24h: errs,
    top_errors: (await errorGroups(1, 5)),
    agent_runs_24h: runs,
    control: publicControl(c),
    managed: { provider: c.managed_provider, model: c.managed_model, default_daily_usd: c.default_daily_usd,
      global_daily_usd: c.global_daily_usd },
    trend: trend14(all, led30, t),
    at: nowIso(),
  };
}

/** The last 14 days, oldest first: sign-ups and AI spend per day (UTC). */
function trend14(all: Row[], led: Row[], t: number): Row {
  const days = [...Array(14)].map((_, i) => new Date(t - (13 - i) * 86400_000).toISOString().slice(0, 10));
  const by = (rows: Row[], f: string) => {
    const m = new Map<string, Row[]>();
    for (const r of rows) {
      const k = String(r[f] || "").slice(0, 10);
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(r);
    }
    return m;
  };
  const sp = by(led, "at"), su = by(all, "created_at");
  return { days, spend: days.map((d) => sum(sp.get(d) || [])), signups: days.map((d) => (su.get(d) || []).length),
    requests: days.map((d) => (sp.get(d) || []).length) };
}

async function listUsers(args: Row): Promise<Row> {
  const q = String(args.q || "").toLowerCase();
  const limit = Math.min(500, Math.max(1, num(args.limit, 100)));
  const offset = Math.max(0, num(args.offset, 0));
  const [all, profiles, ucs, devs, led] = await Promise.all([
    users(), db.select("profiles", { select: "id,plan", limit: 20000 }),
    db.select("user_controls", { limit: 20000 }), devices(365), ledger(30),
  ]);
  const plan = new Map(profiles.map((p) => [p.id, p.plan]));
  const uc = new Map(ucs.map((x) => [x.user_id, x]));
  const dev = new Map<string, Row>();
  for (const d of devs) if (d.user_id && !dev.has(d.user_id)) dev.set(d.user_id, d);
  const spend = new Map<string, number>();
  for (const r of led) if (r.user_id) spend.set(r.user_id, (spend.get(r.user_id) || 0) + num(r.cost_usd));
  let rows = all.map((u) => {
    const d = dev.get(u.id) || {};
    const c = uc.get(u.id) || {};
    return {
      id: u.id, email: u.email, created_at: u.created_at, last_sign_in_at: u.last_sign_in_at,
      providers: (u.app_metadata && u.app_metadata.providers) || [],
      plan: plan.get(u.id) || "free", blocked: !!c.blocked, daily_usd: c.daily_usd ?? null, ai_mode: c.ai_mode || null,
      last_seen: d.last_seen || null, online: d.last_seen ? isOnline(d) : false, country: d.country || "",
      region: d.region || "", version: d.version || "", os: d.os || "", activity: d.activity || {},
      spend_30d: Math.round((spend.get(u.id) || 0) * 10000) / 10000,
    };
  });
  if (q) rows = rows.filter((r) => [r.email, r.id, r.country, r.plan].some((v) => String(v || "").toLowerCase().includes(q)));
  const sort = String(args.sort || "last_seen");
  rows.sort((a, b) => String((b as Row)[sort] ?? "").localeCompare(String((a as Row)[sort] ?? ""), undefined, { numeric: true }));
  return { total: rows.length, users: rows.slice(offset, offset + limit) };
}

async function getUser(args: Row): Promise<Row> {
  const u = await findUser(args.user);
  const [profile, uc, note, devs, led, events] = await Promise.all([
    db.one("profiles", { id: "eq." + u.id }), db.one("user_controls", { user_id: "eq." + u.id }),
    db.one("user_notes", { user_id: "eq." + u.id }),
    db.select("devices", { user_id: "eq." + u.id, order: "last_seen.desc", limit: 50 }),
    ledger(90, u.id), db.select("events", { user_id: "eq." + u.id, order: "created_at.desc", limit: 200 }),
  ]);
  const machines = devs.map((d) => d.machine);
  const inList = machines.length ? "in.(" + machines.map((m) => `"${m}"`).join(",") + ")" : "";
  const [fails, runs] = inList ? await Promise.all([
    db.select("failure_reports", { machine: inList, order: "received_at.desc", limit: 50,
      select: "id,received_at,machine,fingerprint,kind,step,category,app_version,source_url" }),
    db.select("agent_runs", { machine: inList, order: "received_at.desc", limit: 50,
      select: "id,received_at,machine,caller,run,tool,project,status,ms" }),
  ]) : [[], []];
  const t = nowMs();
  return {
    account: { id: u.id, email: u.email, created_at: u.created_at, last_sign_in_at: u.last_sign_in_at,
      confirmed_at: u.email_confirmed_at || u.confirmed_at || null, providers: (u.app_metadata || {}).providers || [] },
    plan: (profile && profile.plan) || "free",
    controls: uc || {},
    note: (note && note.note) || "",
    online: devs.some(isOnline),
    devices: devs.map((d) => ({ ...d, token_hash: undefined, ip_hash: undefined, online: isOnline(d) })),
    spend: {
      today: sum(led.filter((r) => r.at >= dayStartIso())),
      d7: sum(led.filter((r) => t - new Date(r.at).getTime() < 7 * 86400_000)),
      d30: sum(led.filter((r) => t - new Date(r.at).getTime() < 30 * 86400_000)),
      d90: sum(led), requests_90d: led.length,
      by_model: groupCount(led, (r) => r.model),
    },
    usage: led.slice(0, 60),
    events,
    failures: fails,
    agent_runs: runs,
  };
}

async function usageReport(args: Row): Promise<Row> {
  const days = Math.min(365, Math.max(1, num(args.days, 30)));
  const uid = args.user ? (await findUser(args.user)).id : "";
  const rows = await ledger(days, uid);
  const all = await users();
  const email = new Map(all.map((u) => [u.id, u.email]));
  const by = (k: (r: Row) => string) => {
    const m = new Map<string, Row>();
    for (const r of rows) {
      const key = k(r) || "unknown";
      const x = m.get(key) || { key, requests: 0, cost_usd: 0, input_tokens: 0, output_tokens: 0, errors: 0 };
      x.requests++;
      x.cost_usd += num(r.cost_usd);
      x.input_tokens += num(r.input_tokens);
      x.output_tokens += num(r.output_tokens);
      if (r.status !== "ok") x.errors++;
      m.set(key, x);
    }
    return [...m.values()].map((x): Row => ({ ...x, cost_usd: Math.round(x.cost_usd * 10000) / 10000 }))
      .sort((a, b) => b.cost_usd - a.cost_usd);
  };
  return {
    days, total_usd: sum(rows), requests: rows.length,
    by_user: by((r) => email.get(r.user_id) || r.user_id).slice(0, 200),
    by_model: by((r) => r.model),
    by_day: by((r) => String(r.at).slice(0, 10)).sort((a, b) => String(a.key).localeCompare(String(b.key))),
    by_purpose: by((r) => r.purpose),
  };
}

async function listModels(): Promise<Row> {
  const c = await control();
  const sec = await db.select("secrets", { select: "name,value,updated_at,updated_by", limit: 500 });
  const providers = Object.entries(PROVIDERS).map(([id, p]) => {
    const k = sec.find((s) => s.name === "key_" + id);
    const b = sec.find((s) => s.name === "base_" + id);
    return {
      id, label: p.label, base: (b && b.value) || p.base, default_model: p.model,
      keys: k ? keyList(k.value).map(fingerprint) : [], updated_at: k ? k.updated_at : null,
    };
  });
  const ai = (n: string) => (sec.find((s) => s.name === n) || {}).value || "";
  return {
    managed: { mode: c.ai_mode, provider: c.managed_provider, model: c.managed_model },
    providers,
    admin_ai: { provider: ai("admin_ai_provider"), model: ai("admin_ai_model"),
      key: fingerprint(ai("admin_ai_key")) || (ai("admin_ai_provider") ? "(uses the provider key)" : "") },
    prices: c.prices,
  };
}

// ── exports ─────────────────────────────────────────────────────────

function csv(rows: Row[]): string {
  if (!rows.length) return "";
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const cell = (v: unknown) => {
    const s = v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [cols.join(","), ...rows.map((r) => cols.map((c) => cell(r[c])).join(","))].join("\n");
}

function mdTable(rows: Row[], cols: string[]): string {
  if (!rows.length) return "_none_\n";
  const esc = (v: unknown) => String(v ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ").slice(0, 160);
  return `| ${cols.join(" | ")} |\n|${cols.map(() => "---").join("|")}|\n` +
    rows.map((r) => `| ${cols.map((c) => esc(typeof r[c] === "object" ? JSON.stringify(r[c]) : r[c])).join(" | ")} |`).join("\n") + "\n";
}

async function exportReport(args: Row): Promise<{ filename: string; type: string; body: string }> {
  const kind = String(args.kind || "all");
  const fmt = String(args.format || "md");
  const days = Math.min(365, Math.max(1, num(args.days, 7)));
  const stamp = nowIso().slice(0, 16).replace(/[:T]/g, "-");
  const parts: Row = {};
  if (kind === "all" || kind === "overview") parts.overview = await overview();
  if (kind === "all" || kind === "errors") {
    parts.errors = await errorGroups(days, 100);
    parts.error_samples = await db.select("failure_reports", { received_at: "gte." + daysAgoIso(days),
      order: "received_at.desc", limit: kind === "errors" ? 200 : 40 });
  }
  if (kind === "all" || kind === "usage") parts.usage = await usageReport({ days });
  if (kind === "all" || kind === "users") parts.users = (await listUsers({ limit: 500 })).users;
  if (kind === "all" || kind === "audit") parts.audit = await db.select("audit_log", { order: "at.desc", limit: 500 });
  if (kind === "events") parts.events = await db.select("events", { created_at: "gte." + daysAgoIso(days), order: "created_at.desc", limit: 5000 });
  if (kind === "runs") parts.agent_runs = await db.select("agent_runs", { received_at: "gte." + daysAgoIso(days), order: "received_at.desc", limit: 2000 });
  if (!Object.keys(parts).length) throw new HttpError(400, "kind is all, overview, errors, usage, users, audit, events or runs");
  const name = `aethron-${kind}-${stamp}`;
  if (fmt === "json") return { filename: name + ".json", type: "application/json", body: JSON.stringify(parts, null, 1) };
  if (fmt === "csv") {
    const key = Object.keys(parts).find((k) => Array.isArray(parts[k])) || "";
    const rows = key ? parts[key] : kind === "usage" ? parts.usage.by_user : [parts.overview];
    return { filename: name + ".csv", type: "text/csv", body: csv(rows as Row[]) };
  }
  let md = `# Aethron ${kind} report\n\nGenerated ${nowIso()} · last ${days} day(s)\n\n`;
  if (parts.overview) {
    const o = parts.overview;
    md += `## Overview\n\n- Users: ${o.users.total} (new: ${o.users.new_24h} today, ${o.users.new_7d} this week)\n` +
      `- Online now: ${o.online_now} copies, ${o.online_users} people · active in 24h: ${o.active_24h}\n` +
      `- AI spend: $${o.spend.today} today, $${o.spend.d7} 7d, $${o.spend.d30} 30d (${o.spend.requests_today} requests today)\n` +
      `- Errors in 24h: ${o.errors_24h} · agent runs in 24h: ${o.agent_runs_24h}\n` +
      `- Switches: paused=${o.control.paused}, AI mode=${o.control.ai_mode}, model=${o.managed.model || "-"}\n\n` +
      `### Countries (7 days)\n\n${mdTable(o.countries, ["key", "count"])}\n`;
  }
  if (parts.errors) {
    md += `## Errors, grouped (${days}d)\n\n${mdTable(parts.errors, ["fingerprint", "count", "machines", "kind", "step", "category", "last", "versions"])}\n`;
    md += `## Recent error reports\n\n`;
    for (const r of parts.error_samples.slice(0, 40)) {
      const rec = r.record || {};
      md += `### ${r.kind || "?"} · ${r.step || ""} · ${r.received_at}\n\n- fingerprint \`${r.fingerprint}\` · app ${r.app_version || "?"} · ${r.source_url || ""}\n` +
        `- ${clip(r.category, 300)}\n\n` +
        (rec.lines ? "```\n" + clip((rec.lines || []).join("\n"), 1500) + "\n```\n" : "") +
        (rec.log ? "<details><summary>log</summary>\n\n```\n" + clip(rec.log, 2500) + "\n```\n</details>\n" : "") +
        (rec.trace ? "```\n" + clip(rec.trace, 1500) + "\n```\n" : "") + "\n";
    }
  }
  if (parts.usage) {
    md += `## AI usage (${days}d): $${parts.usage.total_usd}, ${parts.usage.requests} requests\n\n` +
      `### By user\n\n${mdTable(parts.usage.by_user.slice(0, 100), ["key", "requests", "cost_usd", "input_tokens", "output_tokens", "errors"])}\n` +
      `### By model\n\n${mdTable(parts.usage.by_model, ["key", "requests", "cost_usd"])}\n`;
  }
  if (parts.users) {
    md += `## Users\n\n${mdTable(parts.users, ["email", "created_at", "last_seen", "country", "plan", "version", "spend_30d", "blocked"])}\n`;
  }
  if (parts.audit) md += `## Admin actions\n\n${mdTable(parts.audit, ["at", "actor", "via", "action", "target"])}\n`;
  if (parts.events) md += `## Events\n\n${mdTable(parts.events, ["created_at", "user_id", "event", "props"])}\n`;
  if (parts.agent_runs) md += `## Agent runs\n\n${mdTable(parts.agent_runs, ["received_at", "caller", "tool", "project", "status", "ms"])}\n`;
  return { filename: name + ".md", type: "text/markdown", body: md };
}

// ── changing a person ───────────────────────────────────────────────

async function userControl(a: Admin, ref: string, patch: Row, action: string): Promise<Row> {
  const u = await findUser(ref);
  const [r] = await db.upsert("user_controls", { user_id: u.id, ...patch, updated_at: nowIso() }, "user_id");
  // every copy watches ONE timestamp (control.updated_at) between full
  // check-ins; a change to one person used to wait up to 10 minutes
  await db.upsert("control", { id: 1, updated_at: nowIso() }, "id");
  forget("");
  await audit(a, action, u.email || u.id, patch);
  return { user: u.email, controls: r };
}

// ── the tools ───────────────────────────────────────────────────────

export const TOOLS: Tool[] = [
  { name: "overview", scope: "read", params: {},
    description: "Everything at a glance: users, who is online, countries, AI spend, errors, agent runs, the switches.",
    run: () => overview() },
  { name: "list_users", scope: "read",
    params: { q: S("search email, id, country or plan"), limit: N("rows (max 500)"), offset: N("skip"), sort: S("last_seen, created_at, spend_30d, email") },
    description: "Every account with plan, country, version, last seen, online, 30-day AI spend and blocks.",
    run: (a) => listUsers(a) },
  { name: "get_user", scope: "read", params: { user: S("email or id") }, required: ["user"],
    description: "One person in full: account, plan, devices (country, version, what Aethron is doing), spend, usage, events, errors, agent runs.",
    run: (a) => getUser(a) },
  { name: "online_users", scope: "read", params: {},
    description: "Copies of Aethron checked in within the last 2.5 minutes, with what each is doing right now.",
    run: async () => (await devices(1)).filter(isOnline).map((d) => ({ ...d, token_hash: undefined, ip_hash: undefined })) },
  { name: "countries", scope: "read", params: { days: N("active within this many days (default 30)") },
    description: "Where people use Aethron from: copies and people per country and region.",
    run: async (a) => {
      const devs = await devices(Math.max(1, num(a.days, 30)));
      return { by_country: groupCount(devs, (d) => d.country), by_region: groupCount(devs, (d) => d.country + " · " + (d.region || "?")).slice(0, 60),
        people_by_country: groupCount([...new Map(devs.filter((d) => d.user_id).map((d) => [d.user_id, d])).values()], (d) => d.country) };
    } },
  { name: "get_control", scope: "read", params: {}, description: "The switches every copy of Aethron obeys right now.",
    run: () => control(true) },
  { name: "usage_report", scope: "read", params: { days: N("window (default 30)"), user: S("only this person (email or id)") },
    description: "Managed-AI spend and requests by user, model, day and purpose.", run: (a) => usageReport(a) },
  { name: "errors", scope: "read", params: { days: N("window (default 7)"), limit: N("groups (default 50)") },
    description: "Failure reports from every copy, grouped by fingerprint (one fault = one group), most frequent first.",
    run: (a) => errorGroups(Math.max(1, num(a.days, 7)), num(a.limit, 50)) },
  { name: "error_detail", scope: "read", params: { fingerprint: S("the group's fingerprint") }, required: ["fingerprint"],
    description: "The reports behind one error group, with their failing lines, log tail and trace.",
    run: (a) => db.select("failure_reports", { fingerprint: "eq." + a.fingerprint, order: "received_at.desc", limit: 30 }) },
  { name: "events", scope: "read", params: { user: S("email or id"), event: S("event name"), days: N("window (default 7)"), limit: N("rows (default 200)") },
    description: "The activity stream (logins, builds, migrations, agent turns...).",
    run: async (a) => db.select("events", {
      ...(a.user ? { user_id: "eq." + (await findUser(a.user)).id } : {}), ...(a.event ? { event: "eq." + a.event } : {}),
      created_at: "gte." + daysAgoIso(Math.max(1, num(a.days, 7))), order: "created_at.desc", limit: Math.min(5000, num(a.limit, 200)),
    }) },
  { name: "agent_runs", scope: "read", params: { days: N("window (default 3)"), caller: S("agent label"), limit: N("rows") },
    description: "What agents driving Aethron over MCP did, call by call.",
    run: (a) => db.select("agent_runs", { received_at: "gte." + daysAgoIso(Math.max(1, num(a.days, 3))),
      ...(a.caller ? { caller: "eq." + a.caller } : {}), order: "received_at.desc", limit: Math.min(2000, num(a.limit, 200)),
      select: "id,received_at,machine,caller,run,n,tool,project,status,ms" }) },
  { name: "audit_log", scope: "read", params: { limit: N("rows (default 200)"), actor: S("only this admin") },
    description: "Every admin action: who (person, API key or the admin AI), what, when.",
    run: (a) => db.select("audit_log", { ...(a.actor ? { actor: "eq." + a.actor } : {}), order: "at.desc", limit: Math.min(2000, num(a.limit, 200)) }) },
  { name: "list_models", scope: "read", params: {},
    description: "Providers the managed AI can use, which have keys (fingerprints only), the model in use, the admin AI's setup.",
    run: () => listModels() },
  { name: "export_report", scope: "read",
    params: { kind: S("all, overview, errors, usage, users, audit, events or runs"), format: S("md, json or csv"), days: N("window (default 7)") },
    description: "A downloadable report (Markdown by default) — errors with their logs, usage, users, admin actions.",
    run: async (a) => exportReport(a) },
  { name: "list_api_keys", scope: "read", params: {}, description: "Admin API keys (never the key itself).",
    run: () => db.select("admin_keys", { select: "id,label,prefix,scopes,created_at,created_by,last_used,revoked_at", order: "created_at.desc" }) },
  { name: "list_admins", scope: "read", params: {}, description: "Who can sign in to the admin, and their roles.",
    run: async () => [...ownerEmails().map((e) => ({ email: e, role: "owner", source: "ADMIN_EMAILS" })),
      ...(await db.select("admins", { order: "added_at.asc" })).map((r) => ({ ...r, source: "admin" }))] },

  // ── switches ──
  { name: "pause_all", scope: "write", params: { message: S("what people see while paused") },
    description: "KILL SWITCH. Stops every copy of Aethron: no messages, no agent turns, no MCP tools, no AI answers.",
    run: async (a, ad) => {
      const c = await setControl({ paused: true, pause_message: a.message || "Aethron is paused by its team for a moment." }, ad.actor);
      await audit(ad, "pause_all", "", { message: c.pause_message });
      return publicControl(c);
    } },
  { name: "resume_all", scope: "write", params: {}, description: "Lift the kill switch.",
    run: async (_a, ad) => {
      const c = await setControl({ paused: false }, ad.actor);
      await audit(ad, "resume_all");
      return publicControl(c);
    } },
  { name: "set_banner", scope: "write", params: { text: S("message shown at the top of every copy ('' removes it)"), level: S("info, warn or critical") },
    description: "A one-line strip at the top of every copy, for ongoing notices (\"maintenance at 18:00 UTC\"). For news people should see once, with a picture, use publish_announcement.",
    run: async (a, ad) => {
      const c = await setControl({ banner: a.text || "", banner_level: a.level || "info" }, ad.actor);
      await audit(ad, "set_banner", "", { text: a.text, level: a.level });
      return publicControl(c);
    } },
  { name: "preview_illustration", scope: "write",
    params: { title: S("the announcement's title"), body: S("its words"), level: S("info, warn or critical"),
      brief: S("optional art direction, e.g. 'a rocket leaving a launch pad'") },
    description: "Draw (without publishing) the animated illustration for an announcement, in Aethron's house style. Returns the SVG.",
    run: (a) => drawIllustration(a) },
  { name: "publish_announcement", scope: "write",
    params: { title: S("short headline, ≤ 7 words"), body: S("one or two plain sentences"), level: S("info, warn or critical"),
      cta_label: S("optional button label"), cta_url: S("optional https:// address the button opens"),
      art: S("'auto' (default: the admin AI draws it), 'none', or an SVG from preview_illustration"),
      brief: S("optional art direction"), banner: B("also show it as the one-line strip at the top") },
    required: ["title"],
    description: "Show every copy of Aethron a card, once: title, words, optional button, and an animated illustration drawn for it.",
    run: (a, ad) => publishAnnouncement(a, ad) },
  { name: "end_announcement", scope: "write", params: {}, description: "Stop showing the current announcement.",
    run: (_a, ad) => endAnnouncement(ad) },
  { name: "list_announcements", scope: "read", params: { limit: N("rows (default 30)") },
    description: "Past and current announcements (which one is live).",
    run: (a) => listAnnouncements(Number(a.limit) || 30) },
  { name: "set_feature", scope: "write",
    params: { feature: S("agent, mcp, migrate, screenshot, publish or signup"), on: B("true to allow, false to switch off") },
    required: ["feature", "on"],
    description: "Switch one part of Aethron on or off for everyone (MCP: outside coding agents).",
    run: async (a, ad) => {
      const c = await setControl({ features: { [a.feature]: !!a.on } }, ad.actor);
      await audit(ad, "set_feature", a.feature, { on: !!a.on });
      return c.features;
    } },
  { name: "set_ai_mode", scope: "write",
    params: { mode: S("byok (people's own keys), managed (Aethron's key) or off"), provider: S("managed provider id"), model: S("managed model id") },
    required: ["mode"],
    description: "Who powers Aethron's AI. 'managed' runs every copy on Aethron's own key and model; people's own keys are no longer used.",
    run: async (a, ad) => {
      const patch: Row = { ai_mode: a.mode };
      if (a.provider) {
        if (!PROVIDERS[a.provider]) throw new HttpError(400, `unknown provider "${a.provider}" (known: ${Object.keys(PROVIDERS).join(", ")})`);
        patch.managed_provider = a.provider;
      }
      if (a.model) patch.managed_model = a.model;
      const c = await setControl(patch, ad.actor);
      await audit(ad, "set_ai_mode", a.mode, patch);
      return { ai_mode: c.ai_mode, provider: c.managed_provider, model: c.managed_model };
    } },
  { name: "set_limits", scope: "write",
    params: { default_daily_usd: N("each person's daily AI allowance (0 = unlimited)"), global_daily_usd: N("everyone together, per day (0 = unlimited)") },
    description: "Managed-AI spending limits.",
    run: async (a, ad) => {
      const patch: Row = {};
      if (a.default_daily_usd !== undefined) patch.default_daily_usd = a.default_daily_usd;
      if (a.global_daily_usd !== undefined) patch.global_daily_usd = a.global_daily_usd;
      const c = await setControl(patch, ad.actor);
      await audit(ad, "set_limits", "", patch);
      return { default_daily_usd: c.default_daily_usd, global_daily_usd: c.global_daily_usd };
    } },
  { name: "set_min_version", scope: "write", params: { version: S("e.g. 1.1.60 ('' = none)") }, required: ["version"],
    description: "Copies older than this must update before they can work.",
    run: async (a, ad) => {
      const c = await setControl({ min_version: a.version }, ad.actor);
      await audit(ad, "set_min_version", a.version);
      return { min_version: c.min_version };
    } },
  { name: "set_control", scope: "write", params: { patch: { type: "object", description: "any control fields" } }, required: ["patch"],
    description: "Change several switches at once (paused, pause_message, banner, banner_level, ai_mode, managed_provider, managed_model, features, min_version, default_daily_usd, global_daily_usd, prices).",
    run: async (a, ad) => {
      const c = await setControl(a.patch || {}, ad.actor);
      await audit(ad, "set_control", "", a.patch || {});
      return c;
    } },

  // ── people ──
  { name: "block_user", scope: "write", params: { user: S("email or id"), reason: S("shown to them") }, required: ["user"],
    description: "Put one person on hold: their copy stops working and Aethron's AI refuses them.",
    run: (a, ad) => userControl(ad, a.user, { blocked: true, block_reason: String(a.reason || "") }, "block_user") },
  { name: "unblock_user", scope: "write", params: { user: S("email or id") }, required: ["user"],
    description: "Lift a hold.", run: (a, ad) => userControl(ad, a.user, { blocked: false, block_reason: "" }, "unblock_user") },
  { name: "set_user_limit", scope: "write", params: { user: S("email or id"), daily_usd: N("their daily AI allowance; omit to use the default; 0 = unlimited") },
    required: ["user"], description: "One person's managed-AI allowance.",
    run: (a, ad) => userControl(ad, a.user, { daily_usd: a.daily_usd === undefined || a.daily_usd === null ? null : num(a.daily_usd) }, "set_user_limit") },
  { name: "set_user_ai_mode", scope: "write", params: { user: S("email or id"), mode: S("byok, managed, off — or '' to follow everyone") },
    required: ["user"], description: "Who powers the AI for this one person.",
    run: (a, ad) => userControl(ad, a.user, { ai_mode: a.mode || null }, "set_user_ai_mode") },
  { name: "notify_user", scope: "write", params: { user: S("email or id"), notice: S("message shown in their app ('' clears)") },
    required: ["user"], description: "Show one person a message in their app.",
    run: (a, ad) => userControl(ad, a.user, { notice: String(a.notice || "").slice(0, 600) }, "notify_user") },
  { name: "sign_out_user", scope: "write", params: { user: S("email or id") }, required: ["user"],
    description: "Sign one person out of every copy; they must sign in again.",
    run: async (a, ad) => {
      const u = await findUser(a.user);
      await db.update("devices", { user_id: "eq." + u.id }, { revoked_at: nowIso() });
      const r = await userControl(ad, u.id, { signout_after: nowIso() }, "sign_out_user");
      return r;
    } },
  { name: "set_user_plan", scope: "write", params: { user: S("email or id"), plan: S("free, pro or studio") }, required: ["user", "plan"],
    description: "Change a person's plan.",
    run: async (a, ad) => {
      if (!["free", "pro", "studio"].includes(a.plan)) throw new HttpError(400, "plan is free, pro or studio");
      const u = await findUser(a.user);
      await db.upsert("profiles", { id: u.id, plan: a.plan, updated_at: nowIso() }, "id");
      await audit(ad, "set_user_plan", u.email, { plan: a.plan });
      return { user: u.email, plan: a.plan };
    } },
  { name: "note_user", scope: "write", params: { user: S("email or id"), note: S("private note for admins") }, required: ["user"],
    description: "A private admin note on a person (they never see it).",
    run: async (a, ad) => {
      const u = await findUser(a.user);
      await db.upsert("user_notes", { user_id: u.id, note: String(a.note || ""), updated_at: nowIso(), updated_by: ad.actor }, "user_id");
      await audit(ad, "note_user", u.email);
      return { user: u.email, note: a.note };
    } },
  { name: "revoke_device", scope: "write", params: { machine: S("the copy's machine id") }, required: ["machine"],
    description: "Cut off one installed copy (it must sign in again).",
    run: async (a, ad) => {
      const r = await db.update("devices", { machine: "eq." + a.machine }, { revoked_at: nowIso() });
      forget("dev:");
      await audit(ad, "revoke_device", a.machine);
      return { revoked: r.length };
    } },

  // ── owner only ──
  { name: "set_provider_key", scope: "owner",
    params: { provider: S("gemini, openai, deepseek, anthropic, openrouter, groq, xai, mistral, custom"),
      keys: S("one or more keys, one per line ('' removes)"), base_url: S("only to override the provider's address") },
    required: ["provider"],
    description: "Store Aethron's own key for a provider. The key is never shown again — only its fingerprint.",
    run: async (a, ad) => {
      if (!PROVIDERS[a.provider]) throw new HttpError(400, `unknown provider "${a.provider}"`);
      if (a.keys !== undefined) await setSecret("key_" + a.provider, String(a.keys || "").trim(), ad.actor);
      if (a.base_url !== undefined) await setSecret("base_" + a.provider, String(a.base_url || "").trim(), ad.actor);
      await audit(ad, "set_provider_key", a.provider, { keys: keyList(String(a.keys || "")).map(fingerprint), base_url: a.base_url });
      return { provider: a.provider, keys: keyList(await secret("key_" + a.provider)).map(fingerprint) };
    } },
  { name: "set_ember_mind", scope: "owner",
    params: { on: { type: "boolean", description: "switch Ember's mind on or off for every copy" },
      provider: S("provider its model runs on (default groq)"), model: S("a small fast model (default llama-3.1-8b-instant)"),
      per_hour: N("most thoughts per person per hour (default 40)") },
    description: "Ember's mind: the small model that decides what the companion does and lets it invent new behaviours. Separate from the project AI; it uses the provider key set with set_provider_key. Off = built-in behaviour.",
    run: async (a, ad) => {
      if (a.provider !== undefined) {
        if (a.provider && !PROVIDERS[a.provider]) throw new HttpError(400, `unknown provider "${a.provider}"`);
        await setSecret("ember_provider", String(a.provider || ""), ad.actor);
      }
      if (a.model !== undefined) await setSecret("ember_model", String(a.model || ""), ad.actor);
      if (a.per_hour !== undefined) await setSecret("ember_per_hour", String(Math.max(1, Math.min(600, num(a.per_hour) || 40))), ad.actor);
      if (a.on !== undefined) await setSecret("ember_on", a.on ? "1" : "0", ad.actor);
      await audit(ad, "set_ember_mind", "", { on: a.on, provider: a.provider, model: a.model, per_hour: a.per_hour });
      const s = await emberSettings();
      return { ...s, keys: keyList(await secret("key_" + s.provider)).map(fingerprint) };
    } },
  { name: "ember_mind", scope: "read", params: {},
    description: "Whether Ember's mind is on, its model, and how often it thought today.",
    run: async () => {
      const s = await emberSettings();
      const rows = await db.select("usage_ledger", { select: "status,cost_usd", purpose: "eq.ember", at: "gte." + dayStartIso(), limit: 50000 });
      return { ...s, has_key: keyList(await secret("key_" + s.provider)).length > 0, thoughts_today: rows.length,
        failed_today: rows.filter((r) => r.status !== "ok").length, cost_today: rows.reduce((t, r) => t + num(r.cost_usd), 0) };
    } },
  { name: "set_admin_ai", scope: "owner",
    params: { provider: S("provider the admin AI uses"), model: S("its model"), key: S("its own key (optional; '' = use the provider key)") },
    description: "Set up the admin's own AI (separate from the AI that powers Aethron for users).",
    run: async (a, ad) => {
      if (a.provider !== undefined) {
        if (a.provider && !PROVIDERS[a.provider]) throw new HttpError(400, `unknown provider "${a.provider}"`);
        await setSecret("admin_ai_provider", a.provider, ad.actor);
      }
      if (a.model !== undefined) await setSecret("admin_ai_model", a.model, ad.actor);
      if (a.key !== undefined) await setSecret("admin_ai_key", a.key, ad.actor);
      await audit(ad, "set_admin_ai", a.provider || "", { model: a.model, key: a.key ? fingerprint(a.key) : undefined });
      return (await listModels()).admin_ai;
    } },
  { name: "create_api_key", scope: "owner", params: { label: S("who it is for"), scopes: { type: "array", items: { type: "string" }, description: "read, write, owner" } },
    description: "A key a coding agent can use against the admin API and admin MCP. Shown ONCE.",
    run: async (a, ad) => {
      const scopes = (Array.isArray(a.scopes) && a.scopes.length ? a.scopes : ["read"]).map(String);
      for (const s of scopes) if (!["read", "write", "owner"].includes(s)) throw new HttpError(400, "scopes are read, write, owner");
      for (const s of scopes) need(ad, s as Scope);
      const key = randomToken("ak_", 30);
      await db.insert("admin_keys", { label: String(a.label || "").slice(0, 80), prefix: key.slice(0, 10), hash: await sha256(key),
        scopes, created_by: ad.actor });
      await audit(ad, "create_api_key", a.label || "", { scopes, prefix: key.slice(0, 10) });
      return { key, prefix: key.slice(0, 10), scopes, note: "Store it now: it is not shown again." };
    } },
  { name: "revoke_api_key", scope: "owner", params: { id: S("the key's id or prefix") }, required: ["id"],
    description: "Revoke an admin API key.",
    run: async (a, ad) => {
      const q = String(a.id).startsWith("ak_") ? { prefix: "eq." + String(a.id).slice(0, 10) } : { id: "eq." + a.id };
      const r = await db.update("admin_keys", q, { revoked_at: nowIso() });
      await audit(ad, "revoke_api_key", a.id);
      return { revoked: r.length };
    } },
  { name: "add_admin", scope: "owner", params: { email: S("their sign-in email"), role: S("admin or viewer") }, required: ["email"],
    description: "Let someone into the admin.",
    run: async (a, ad) => {
      const role = a.role || "admin";
      if (!["owner", "admin", "viewer"].includes(role)) throw new HttpError(400, "role is owner, admin or viewer");
      await db.upsert("admins", { email: String(a.email).toLowerCase(), role, added_by: ad.actor }, "email");
      await audit(ad, "add_admin", a.email, { role });
      return { email: a.email, role };
    } },
  { name: "remove_admin", scope: "owner", params: { email: S("their email") }, required: ["email"],
    description: "Take someone out of the admin.",
    run: async (a, ad) => {
      const r = await db.remove("admins", { email: "eq." + String(a.email).toLowerCase() });
      await audit(ad, "remove_admin", a.email);
      return { removed: r.length };
    } },
  { name: "delete_user", scope: "owner", params: { user: S("email or id"), confirm: S("type their email to confirm") },
    required: ["user", "confirm"], description: "Delete an account for good (with its devices, controls and events).",
    run: async (a, ad) => {
      const u = await findUser(a.user);
      if (String(a.confirm || "").toLowerCase() !== String(u.email || "").toLowerCase()) {
        throw new HttpError(400, `to delete ${u.email}, pass confirm="${u.email}"`);
      }
      await auth.deleteUser(u.id);
      forget("users");
      await audit(ad, "delete_user", u.email, { id: u.id });
      return { deleted: u.email };
    } },
];

export function tool(name: string): Tool {
  const t = TOOLS.find((x) => x.name === name);
  if (!t) throw new HttpError(404, `no admin tool "${name}"`);
  return t;
}

export async function runTool(name: string, args: Row, a: Admin): Promise<unknown> {
  const t = tool(name);
  need(a, t.scope);
  for (const r of t.required || []) {
    // "" is a real answer for some ("no minimum version"); a tool that needs
    // a non-empty value says so itself
    if (args[r] === undefined || args[r] === null) throw new HttpError(400, `${name} needs "${r}"`);
  }
  return t.run(args || {}, a);
}

export function visibleTools(a: Admin): Tool[] {
  return TOOLS.filter((t) => a.scopes.includes(t.scope));
}

export { spentToday };
