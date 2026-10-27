// Who is calling, and what the switches say.

import { auth, db, Row } from "./db.ts";
import { dayStartIso, env, HttpError, nowIso, nowMs, num, sha256 } from "./util.ts";

// ── a short memory per edge isolate ─────────────────────────────────
const CACHE = new Map<string, { at: number; v: unknown }>();
export async function cached<T>(key: string, ttlMs: number, f: () => Promise<T>): Promise<T> {
  const hit = CACHE.get(key);
  if (hit && nowMs() - hit.at < ttlMs) return hit.v as T;
  const v = await f();
  CACHE.set(key, { at: nowMs(), v });
  return v;
}
export function forget(prefix = ""): void {
  for (const k of [...CACHE.keys()]) if (k.startsWith(prefix)) CACHE.delete(k);
}

// ── the switches ────────────────────────────────────────────────────
export const DEFAULT_FEATURES = { agent: true, mcp: true, migrate: true, screenshot: true, publish: true, signup: true };

export async function control(fresh = false): Promise<Row> {
  if (fresh) forget("control");
  return cached("control", 8000, async () => {
    const c = normalise((await db.one("control", { id: "eq.1" })) || { id: 1 });
    // the live announcement travels with the switches (its drawing does not:
    // that is fetched once by id, so the minute-by-minute check stays small)
    if (c.announcement_id) {
      const a = await db.one("announcements", { id: "eq." + c.announcement_id,
        select: "id,title,body,level,art_source,cta_label,cta_url,created_at" }).catch(() => null);
      c.announcement = a ? { id: a.id, title: a.title, body: a.body, level: a.level, has_art: a.art_source !== "none",
        cta_label: a.cta_label || "", cta_url: a.cta_url || "", published_at: a.created_at } : null;
    }
    return c;
  });
}

function normalise(c: Row): Row {
  return {
    paused: !!c.paused,
    pause_message: c.pause_message || "",
    banner: c.banner || "",
    banner_level: c.banner_level || "info",
    ai_mode: c.ai_mode || "byok",
    managed_provider: c.managed_provider || "gemini",
    managed_model: c.managed_model || "",
    features: { ...DEFAULT_FEATURES, ...(c.features || {}) },
    min_version: c.min_version || "",
    default_daily_usd: num(c.default_daily_usd, 0.5),
    global_daily_usd: num(c.global_daily_usd, 10),
    prices: c.prices || {},
    announcement_id: c.announcement_id || "",
    announcement: null as Row | null,
    updated_at: c.updated_at || "",
    updated_by: c.updated_by || "",
  };
}

/** What every copy of Aethron may read without signing in. */
export function publicControl(c: Row): Row {
  return {
    paused: c.paused, pause_message: c.pause_message, banner: c.banner, banner_level: c.banner_level,
    ai_mode: c.ai_mode, managed_model: c.managed_model, features: c.features, min_version: c.min_version,
    announcement: c.announcement || null, updated_at: c.updated_at,
  };
}

const CONTROL_FIELDS: Record<string, (v: unknown) => unknown> = {
  paused: (v) => !!v,
  pause_message: (v) => String(v ?? "").slice(0, 600),
  banner: (v) => String(v ?? "").slice(0, 600),
  banner_level: (v) => {
    const s = String(v);
    if (!["info", "warn", "critical"].includes(s)) throw new HttpError(400, "banner_level is info, warn or critical");
    return s;
  },
  ai_mode: (v) => {
    const s = String(v);
    if (!["byok", "managed", "off"].includes(s)) throw new HttpError(400, "ai_mode is byok, managed or off");
    return s;
  },
  managed_provider: (v) => String(v ?? "").slice(0, 40),
  managed_model: (v) => String(v ?? "").slice(0, 120),
  features: (v) => {
    if (!v || typeof v !== "object") throw new HttpError(400, "features is an object of on/off switches");
    const out: Row = {};
    for (const [k, x] of Object.entries(v as Row)) {
      if (!(k in DEFAULT_FEATURES)) throw new HttpError(400, `unknown feature "${k}" (known: ${Object.keys(DEFAULT_FEATURES).join(", ")})`);
      out[k] = !!x;
    }
    return out;
  },
  min_version: (v) => {
    const s = String(v ?? "").trim();
    if (s && !/^\d+(\.\d+){0,3}$/.test(s)) throw new HttpError(400, "min_version looks like 1.1.40");
    return s;
  },
  default_daily_usd: (v) => {
    const n = Number(v);
    if (!Number.isFinite(n) || n < 0) throw new HttpError(400, "default_daily_usd is 0 or more (0 = unlimited)");
    return n;
  },
  global_daily_usd: (v) => {
    const n = Number(v);
    if (!Number.isFinite(n) || n < 0) throw new HttpError(400, "global_daily_usd is 0 or more (0 = unlimited)");
    return n;
  },
  announcement_id: (v) => {
    const s = String(v ?? "").trim();
    if (s && !/^an_[A-Za-z0-9_-]{6,40}$/.test(s)) throw new HttpError(400, "announcement_id is an an_… id ('' ends it)");
    return s;
  },
  prices: (v) => {
    if (!v || typeof v !== "object") throw new HttpError(400, "prices maps a model to [in, out] dollars per 1M tokens");
    return v;
  },
};

export async function setControl(patch: Row, actor: string): Promise<Row> {
  const clean: Row = {};
  for (const [k, v] of Object.entries(patch || {})) {
    const f = CONTROL_FIELDS[k];
    if (!f) throw new HttpError(400, `unknown control "${k}" (known: ${Object.keys(CONTROL_FIELDS).join(", ")})`);
    clean[k] = f(v);
  }
  if (!Object.keys(clean).length) throw new HttpError(400, "nothing to change");
  if (clean.features) clean.features = { ...(await control(true)).features, ...clean.features };
  clean.updated_at = nowIso();
  clean.updated_by = actor;
  await db.upsert("control", { id: 1, ...clean }, "id");
  return control(true);
}

// ── provider keys and admin settings ────────────────────────────────
export async function secret(name: string): Promise<string> {
  const r = await cached("secret:" + name, 15000, () => db.one("secrets", { name: "eq." + name }));
  return (r && String(r.value || "")) || "";
}

export async function setSecret(name: string, value: string, actor: string): Promise<void> {
  if (!/^[a-z0-9_.-]{2,80}$/i.test(name)) throw new HttpError(400, "a secret's name is letters, digits, _ . -");
  if (value) await db.upsert("secrets", { name, value, updated_at: nowIso(), updated_by: actor }, "name");
  else await db.remove("secrets", { name: "eq." + name });
  forget("secret:" + name);
}

/** Several keys may sit in one secret (one per line or comma). */
export function keyList(v: string): string[] {
  return String(v || "").split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
}

// ── a person's own switches ─────────────────────────────────────────
export async function userControls(uid: string): Promise<Row> {
  if (!uid) return {};
  return (await db.one("user_controls", { user_id: "eq." + uid })) || {};
}

export async function spentToday(uid: string): Promise<number> {
  const rows = await db.select("usage_ledger", {
    select: "cost_usd", user_id: "eq." + uid, at: "gte." + dayStartIso(), limit: 20000,
  });
  return rows.reduce((s, r) => s + num(r.cost_usd), 0);
}

export async function spentTodayAll(): Promise<number> {
  const rows = await db.select("usage_ledger", { select: "cost_usd", at: "gte." + dayStartIso(), limit: 50000 });
  return rows.reduce((s, r) => s + num(r.cost_usd), 0);
}

/** What applies to this person: the global switches with their overrides. */
export async function effective(uid: string): Promise<Row> {
  const c = await control();
  const uc = await userControls(uid);
  const limit = uc.daily_usd === null || uc.daily_usd === undefined ? c.default_daily_usd : num(uc.daily_usd);
  return {
    control: c,
    blocked: !!uc.blocked,
    block_reason: uc.block_reason || "",
    notice: uc.notice || "",
    signout_after: uc.signout_after || null,
    ai_mode: uc.ai_mode || c.ai_mode,
    daily_usd: limit,
  };
}

// ── who is calling ──────────────────────────────────────────────────
export type Caller = { kind: "device" | "user"; user_id: string; email: string; machine: string; iat: number };

/** When an access token was issued (GoTrue has already checked it). */
function issuedAt(jwt: string): number {
  try {
    const p = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return Number(JSON.parse(atob(p + "=".repeat((4 - (p.length % 4)) % 4))).iat || 0) * 1000;
  } catch (_) {
    return 0;
  }
}

/** A person signed out by an admin after this token was issued. */
export function signedOutAfter(who: Caller, signoutAfter: string | null | undefined): boolean {
  if (!signoutAfter || who.kind !== "user") return false;
  return who.iat > 0 && who.iat < new Date(signoutAfter).getTime();
}

export function bearer(req: Request): string {
  const h = req.headers.get("authorization") || "";
  return h.toLowerCase().startsWith("bearer ") ? h.slice(7).trim() : "";
}

/** A person: a device token from an installed copy, or their access token. */
export async function caller(req: Request): Promise<Caller | null> {
  const tok = bearer(req);
  if (!tok) return null;
  if (tok.startsWith("adt_")) {
    const h = await sha256(tok);
    const d = await cached("dev:" + h, 30000, () => db.one("devices", { token_hash: "eq." + h }));
    if (!d || d.revoked_at) return null;
    return { kind: "device", user_id: d.user_id || "", email: d.email || "", machine: d.machine, iat: 0 };
  }
  const anon = env("SUPABASE_ANON_KEY");
  if (anon && tok === anon) return null;
  const u = await cached("jwt:" + (await sha256(tok)), 60000, () => auth.userFromToken(tok));
  if (!u || !u.id) return null;
  return { kind: "user", user_id: u.id, email: u.email || "", machine: "", iat: issuedAt(tok) };
}

// ── admins ──────────────────────────────────────────────────────────
export type Scope = "read" | "write" | "owner";
export type Admin = { actor: string; email: string; role: string; scopes: Scope[]; via: string };

const ROLE_SCOPES: Record<string, Scope[]> = {
  owner: ["read", "write", "owner"], admin: ["read", "write"], viewer: ["read"],
};

function sameSecret(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let x = 0;
  for (let i = 0; i < a.length; i++) x |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return x === 0;
}

export function ownerEmails(): string[] {
  return env("ADMIN_EMAILS").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
}

export async function adminRole(email: string): Promise<string> {
  const e = String(email || "").toLowerCase();
  if (!e) return "";
  if (ownerEmails().includes(e)) return "owner";
  const r = await db.one("admins", { email: "eq." + e });
  return r ? r.role : "";
}

export async function admin(req: Request): Promise<Admin | null> {
  const tok = bearer(req);
  if (!tok) return null;
  const master = env("ADMIN_MASTER_KEY");
  if (master && master.length >= 24 && sameSecret(tok, master)) {
    return { actor: "master key", email: "", role: "owner", scopes: ROLE_SCOPES.owner, via: "key" };
  }
  if (tok.startsWith("ak_")) {
    const h = await sha256(tok);
    const k = await db.one("admin_keys", { hash: "eq." + h });
    if (!k || k.revoked_at) return null;
    db.update("admin_keys", { id: "eq." + k.id }, { last_used: nowIso() }).catch(() => {});
    return { actor: "key:" + (k.label || k.prefix), email: k.created_by || "", role: "key",
      scopes: (k.scopes || ["read"]) as Scope[], via: "key" };
  }
  if (tok.startsWith("adt_")) return null;
  const u = await cached("jwt:" + (await sha256(tok)), 60000, () => auth.userFromToken(tok));
  if (!u || !u.email) return null;
  const role = await adminRole(u.email);
  if (!role) return null;
  return { actor: u.email, email: u.email, role, scopes: ROLE_SCOPES[role] || [], via: "ui" };
}

export function need(a: Admin, scope: Scope): void {
  if (!a.scopes.includes(scope)) {
    throw new HttpError(403, `this needs the "${scope}" permission; ${a.actor} has ${a.scopes.join(", ") || "none"}`);
  }
}

export async function audit(a: Admin | { actor: string; via: string }, action: string, target = "",
  details: Row = {}): Promise<void> {
  try {
    await db.insert("audit_log", { actor: a.actor, via: a.via, action, target: String(target).slice(0, 200), details });
  } catch (_) { /* the action happened; a failed audit write must not undo it */ }
}
