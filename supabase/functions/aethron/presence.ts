// Every installed copy checks in about once a minute. That one call is how
// the admin knows who is online, where they are (country and region —
// never the address, which is hashed and thrown away) and what Aethron is
// doing for them; and how the copy learns the switches it must obey.

import { db, Row } from "./db.ts";
import { caller, control, effective, publicControl, spentToday, adminRole, bearer, signedOutAfter } from "./state.ts";
import { clip, env, fail, http, json, nowIso, randomToken, sha256, versionLess } from "./util.ts";

const COUNTRY_HEADERS = ["cf-ipcountry", "x-country", "x-country-code", "x-vercel-ip-country"];
const REGION_HEADERS = ["cf-region", "x-region", "x-vercel-ip-country-region"];

export function clientIp(req: Request): string {
  const xf = req.headers.get("x-forwarded-for") || "";
  return (xf.split(",")[0] || req.headers.get("cf-connecting-ip") || req.headers.get("x-real-ip") || "").trim();
}

/** (country, region) for an address. Headers first, then one lookup. */
export async function geo(req: Request, ip: string): Promise<{ country: string; region: string }> {
  for (const h of COUNTRY_HEADERS) {
    const c = (req.headers.get(h) || "").trim().toUpperCase();
    if (/^[A-Z]{2}$/.test(c) && c !== "XX") {
      let region = "";
      for (const r of REGION_HEADERS) region = region || (req.headers.get(r) || "").trim();
      return { country: c, region: region.slice(0, 80) };
    }
  }
  if (!ip || /^(127\.|10\.|192\.168\.|::1|fc|fd)/i.test(ip)) return { country: "", region: "" };
  const lookups = [
    async () => {
      const r = await http(`https://ipapi.co/${encodeURIComponent(ip)}/json/`, { signal: AbortSignal.timeout(2500) });
      const v = await r.json();
      return { country: String(v.country_code || v.country || ""), region: String(v.region || "") };
    },
    async () => {
      const r = await http(`http://ip-api.com/json/${encodeURIComponent(ip)}?fields=status,countryCode,regionName`,
        { signal: AbortSignal.timeout(2500) });
      const v = await r.json();
      return { country: String(v.countryCode || ""), region: String(v.regionName || "") };
    },
  ];
  for (const f of lookups) {
    try {
      const g = await f();
      if (/^[A-Z]{2}$/i.test(g.country)) return { country: g.country.toUpperCase(), region: g.region.slice(0, 80) };
    } catch (_) { /* try the next one */ }
  }
  return { country: "", region: "" };
}

function cleanActivity(a: unknown): Row {
  if (!a || typeof a !== "object") return {};
  const out: Row = {};
  for (const [k, v] of Object.entries(a as Row).slice(0, 24)) {
    if (!/^[a-z_]{1,32}$/i.test(k)) continue;
    if (typeof v === "number" || typeof v === "boolean") out[k] = v;
    else if (v !== null && v !== undefined) out[k] = clip(typeof v === "string" ? v : JSON.stringify(v), 200);
  }
  return out;
}

export async function ping(req: Request): Promise<Response> {
  let body: Row = {};
  try {
    body = await req.json();
  } catch (_) { /* an empty ping is still a ping */ }
  const machine = String(body.machine || "").slice(0, 64);
  if (!/^[A-Za-z0-9_-]{6,64}$/.test(machine)) return fail(400, "machine is the app's random install id");
  const tok = bearer(req);
  const who = await caller(req);
  if (tok.startsWith("adt_") && !who) {
    // revoked by an admin ("sign this person out"), or a stale token
    return json({ signed_out: true, control: publicControl(await control()) }, 401);
  }

  const ip = clientIp(req);
  const salt = env("IP_SALT") || (await sha256(env("SUPABASE_SERVICE_ROLE_KEY"))).slice(0, 16);
  const ipHash = ip ? (await sha256(salt + ip)).slice(0, 32) : "";
  const prev = await db.one("devices", { machine: "eq." + machine });
  const row: Row = {
    machine,
    last_seen: nowIso(),
    version: String(body.version || "").slice(0, 40),
    os: String(body.os || "").slice(0, 60),
    arch: String(body.arch || "").slice(0, 20),
    tz: String(body.tz || "").slice(0, 60),
    activity: cleanActivity(body.activity),
  };
  if (!prev || prev.ip_hash !== ipHash || !prev.country) {
    const g = await geo(req, ip);
    row.ip_hash = ipHash;
    row.country = g.country || (prev && prev.country) || "";
    row.region = g.region || (prev && prev.region) || "";
  }
  let issued = "";
  if (who) {
    if (who.user_id && signedOutAfter(who, (await effective(who.user_id)).signout_after)) {
      return json({ signed_out: true, control: publicControl(await control()) }, 401);
    }
    row.user_id = who.user_id;
    row.email = who.email;
    // a copy that signs in as SOMEONE ELSE does not keep the old person's token
    const changedUser = prev && prev.user_id && prev.user_id !== who.user_id;
    if (who.kind === "user" && (!prev || !prev.token_hash || prev.revoked_at || changedUser || body.rotate)) {
      issued = randomToken("adt_");
      row.token_hash = await sha256(issued);
      row.revoked_at = null;
    }
  }
  if (!prev) row.first_seen = nowIso();
  await db.upsert("devices", row, "machine");

  const c = await control();
  const out: Row = { control: publicControl(c), at: row.last_seen };
  if (issued) out.device_token = issued;
  if (who && who.user_id) {
    const e = await effective(who.user_id);
    out.me = {
      user_id: who.user_id,
      email: who.email,
      blocked: e.blocked,
      block_reason: e.block_reason,
      notice: e.notice,
      ai_mode: e.ai_mode,
      daily_usd: e.daily_usd,
      spent_today: Math.round((await spentToday(who.user_id)) * 10000) / 10000,
      is_admin: !!(await adminRole(who.email)),
      update_required: !!(c.min_version && row.version && versionLess(row.version, c.min_version)),
    };
  }
  return json(out);
}

/** Activity events from a copy, on its device token (which, unlike an
 *  access token, does not expire after an hour). */
export async function events(req: Request): Promise<Response> {
  const who = await caller(req);
  if (!who || !who.user_id) return fail(401, "sign in to send events");
  let body: Row = {};
  try {
    body = await req.json();
  } catch (_) { /* nothing */ }
  const list = Array.isArray(body.events) ? body.events.slice(0, 50) : [];
  const rows = list.filter((e: Row) => e && typeof e.event === "string").map((e: Row) => {
    const props = e.props && typeof e.props === "object" ? e.props : {};
    const raw = JSON.stringify(props);
    return {
      user_id: who.user_id,
      event: String(e.event).slice(0, 60),
      props: raw.length > 4000 ? { truncated: true } : { ...props, machine: who.machine || undefined },
      ...(typeof e.ts === "number" && Math.abs(e.ts * 1000 - Date.now()) < 7 * 86400_000
        ? { created_at: new Date(e.ts * 1000).toISOString() } : {}),
    };
  });
  if (rows.length) await db.insert("events", rows);
  return json({ ok: true, saved: rows.length });
}
