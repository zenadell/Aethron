// Ember's mind. The companion in every copy asks here, every minute or two,
// what to do next. It runs on its OWN model and key, chosen in the admin and
// separate from the AI that works on projects, so it never eats that
// allowance and a person's own key is never used for it. Small and fast by
// design: a free model is enough. Off unless the admin switches it on; off,
// every copy simply keeps Ember's built-in behaviour.

import { db } from "./db.ts";
import { costOf, PROVIDERS } from "./prices.ts";
import { caller, effective, secret, signedOutAfter } from "./state.ts";
import { upstream } from "./gateway.ts";
import { fail, http, json, later, nowMs } from "./util.ts";

export const EMBER_DEFAULT = { provider: "groq", model: "llama-3.1-8b-instant", per_hour: 40 };
const MAX_CHARS = 16000;

export async function emberSettings() {
  const on = (await secret("ember_on")) === "1";
  const provider = (await secret("ember_provider")) || EMBER_DEFAULT.provider;
  const model = (await secret("ember_model")) ||
    (provider === EMBER_DEFAULT.provider ? EMBER_DEFAULT.model : (PROVIDERS[provider] || PROVIDERS.custom).model);
  const per_hour = Number(await secret("ember_per_hour")) || EMBER_DEFAULT.per_hour;
  return { on, provider, model, per_hour };
}

async function usedThisHour(uid: string): Promise<number> {
  const since = new Date(Date.now() - 3600_000).toISOString();
  const rows = await db.select("usage_ledger", {
    select: "id", user_id: "eq." + uid, purpose: "eq.ember", at: "gte." + since, limit: 500,
  });
  return rows.length;
}

export async function emberMind(req: Request): Promise<Response> {
  const who = await caller(req);
  if (!who || !who.user_id) return fail(401, "Sign in to Aethron so Ember can think.");
  const e = await effective(who.user_id);
  // deno-lint-ignore no-explicit-any
  if (signedOutAfter(who as any, e.signout_after)) return fail(401, "You were signed out of Aethron. Sign in again.");
  if (e.control && e.control.paused) return fail(403, "Aethron is paused by its team.", { off: true });
  if (e.blocked) return fail(403, "Your Aethron account is on hold.", { off: true });
  const s = await emberSettings();
  if (!s.on) return fail(503, "Ember's mind is not switched on.", { off: true });
  if (await usedThisHour(who.user_id) >= s.per_hour) {
    return fail(429, `Ember has thought ${s.per_hour} times this hour; it carries on by itself for now.`, { off: true });
  }
  const { base, keys } = await upstream(s.provider);
  if (!base || !keys.length) return fail(503, `Ember's mind has no ${s.provider} key yet.`, { off: true });

  // deno-lint-ignore no-explicit-any
  let b: any;
  try {
    b = await req.json();
  } catch (_) {
    return fail(400, "the request is not JSON");
  }
  const msgs = (Array.isArray(b.messages) ? b.messages : [])
    .filter((m: { role?: string; content?: unknown }) => m && ["system", "user"].includes(String(m.role)) && typeof m.content === "string")
    .slice(0, 4);
  if (!msgs.length) return fail(400, "nothing for Ember to think about");
  let left = MAX_CHARS;
  for (const m of msgs) {
    m.content = m.content.slice(0, Math.max(0, left));
    left -= m.content.length;
  }
  const body: Record<string, unknown> = {
    model: s.model, messages: msgs, max_tokens: Math.min(1000, Number(b.max_tokens) || 900),
    temperature: 0.9, response_format: { type: "json_object" },
  };
  const t0 = nowMs();
  let r: Response | null = null;
  let err = "";
  for (const key of keys) {
    for (let i = 0; i < 2; i++) {
      r = await http(base + "/chat/completions", {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + key },
        body: JSON.stringify(body),
      });
      if (r.ok) break;
      err = await r.text().catch(() => "");
      if (r.status === 400 && /response_format|json_object/i.test(err) && body.response_format) {
        delete body.response_format;
        continue;
      }
      break;
    }
    if (r && r.ok) break;
    if (!r || ![401, 402, 403, 429].includes(r.status)) break;
  }
  const record = (inp: number, out: number, status: string) =>
    later(db.insert("usage_ledger", {
      user_id: who.user_id, machine: who.machine || "", provider: s.provider, model: s.model, purpose: "ember",
      input_tokens: inp, output_tokens: out, cached_tokens: 0,
      cost_usd: status === "ok" ? Math.round(costOf(s.model, inp, out) * 1e6) / 1e6 : 0,
      status, ms: nowMs() - t0, estimated: false,
    }));
  if (!r || !r.ok) {
    record(0, 0, "error");
    return fail(502, `Ember's mind (${s.provider}) answered ${r ? r.status : "nothing"}: ${err.slice(0, 200)}`);
  }
  const j = await r.json();
  const content = String(j?.choices?.[0]?.message?.content || "");
  record(Number(j?.usage?.prompt_tokens) || 0, Number(j?.usage?.completion_tokens) || 0, "ok");
  if (!content) return fail(502, "Ember's mind gave an empty answer.");
  return json({ content, model: s.model });
}
