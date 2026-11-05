// Ember's mind. The companion in every copy asks here, every minute or two,
// what to do next. It runs on its OWN model and key, chosen in the admin and
// separate from the AI that works on projects, so it never eats that
// allowance and a person's own key is never used for it. Small and fast by
// design: a free model is enough. Off unless the admin switches it on; off,
// every copy simply keeps Ember's built-in behaviour.

import { db } from "./db.ts";
import { costOf, PROVIDERS } from "./prices.ts";
import { caller, effective, keyList, secret, setSecret, signedOutAfter } from "./state.ts";
import { upstream } from "./gateway.ts";
import { fail, http, json, later, nowMs } from "./util.ts";

export const EMBER_DEFAULT = { provider: "groq", model: "llama-3.1-8b-instant", per_hour: 40 }; // a retired model is replaced by what the provider lists
const MAX_CHARS = 16000;
const UA = "Aethron/1.1 (+https://aethron.jomiez.com)";

function extractJson(raw: string): string {
  const s = String(raw || "").replace(/<think>[\s\S]*?<\/think>/gi, "").trim();
  const start = s.indexOf("{");
  const end = s.lastIndexOf("}");
  if (start >= 0 && end > start) {
    const cand = s.slice(start, end + 1);
    try {
      JSON.parse(cand);
      return cand;
    } catch (_) { /* fall through */ }
    for (let i = start; i >= 0 && i < end; i = s.indexOf("{", i + 1)) {
      let depth = 0, inStr = false, esc = false;
      for (let j = i; j <= end; j++) {
        const ch = s[j];
        if (inStr) {
          if (esc) esc = false;
          else if (ch === "\\") esc = true;
          else if (ch === '"') inStr = false;
        } else if (ch === '"') inStr = true;
        else if (ch === "{") depth++;
        else if (ch === "}") {
          depth--;
          if (depth === 0) {
            const sub = s.slice(i, j + 1);
            try {
              JSON.parse(sub);
              return sub;
            } catch (_) { /* try next */ }
            break;
          }
        }
      }
    }
  }
  return "";
}

export async function emberSettings() {
  const provider = (await secret("ember_provider")) || EMBER_DEFAULT.provider;
  // A key for its provider is enough: once the admin adds one, the mind is on
  // unless it was switched off on purpose.
  const flag = await secret("ember_on");
  const on = flag === "1" || (!flag && keyList(await secret("key_" + provider)).length > 0);
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
  // A provider retires models; when ours is gone, take the smallest fast one
  // it lists now and remember it, instead of failing every thought.
  const pickModel = async (key: string): Promise<string> => {
    try {
      const m = await http(base + "/models", { headers: { Authorization: "Bearer " + key, "User-Agent": UA } });
      if (!m.ok) return "";
      const ids: string[] = ((await m.json())?.data || []).map((x: { id: string }) => String(x.id))
        .filter((id: string) => !/whisper|tts|guard|embed|vision|audio|speech|moderat|compound|orpheus/i.test(id));
      const score = (id: string) =>
        (/instant|8b|mini|small|lite|nano|fast|flash|27b|20b/i.test(id) ? 0 : 1) * 10 +
        (/gpt-oss/i.test(id) ? 3 : 0) +
        (/70b|120b|large|pro/i.test(id) ? 5 : 0);
      ids.sort((x, y) => score(x) - score(y) || x.length - y.length);
      return ids[0] || "";
    } catch (_) {
      return "";
    }
  };
  let r: Response | null = null;
  let err = "";
  let content = "";
  let usage = { prompt_tokens: 0, completion_tokens: 0 };
  for (const key of keys) {
    for (let i = 0; i < 3; i++) {
      r = await http(base + "/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + key, "User-Agent": UA },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        const j = await r.json().catch(() => ({}));
        usage = {
          prompt_tokens: Number(j?.usage?.prompt_tokens) || 0,
          completion_tokens: Number(j?.usage?.completion_tokens) || 0,
        };
        const raw = String(j?.choices?.[0]?.message?.content || "");
        content = extractJson(raw) || raw.trim();
        if (content) break;
        err = "empty answer";
        if (body.response_format) {
          delete body.response_format;
          continue;
        }
        break;
      }
      err = await r.text().catch(() => "");
      if (r.status === 400 && /response_format|json_object|json_validate_failed|failed_generation/i.test(err) && body.response_format) {
        delete body.response_format;
        if (/gpt-oss/i.test(String(body.model))) {
          const next = await pickModel(key);
          if (next && next !== body.model) {
            body.model = next;
            s.model = next;
            later(setSecret("ember_model", next, "ember"));
          }
        }
        continue;
      }
      if ((r.status === 404 || r.status === 400) && /model_not_found|does not exist|decommission|not supported/i.test(err)) {
        const next = await pickModel(key);
        if (next && next !== body.model) {
          body.model = next;
          s.model = next;
          later(setSecret("ember_model", next, "ember"));
          continue;
        }
      }
      break;
    }
    if (r && r.ok && content) break;
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
    const why = `Ember's mind (${s.provider}) answered ${r ? r.status : "nothing"}: ${err.slice(0, 300)}`;
    later(setSecret("ember_last_error", new Date().toISOString() + " " + why, "ember"));
    return fail(502, why);
  }
  record(usage.prompt_tokens, usage.completion_tokens, "ok");
  if (!content) return fail(502, "Ember's mind gave an empty answer.");
  if (await secret("ember_last_error")) later(setSecret("ember_last_error", "", "ember"));
  return json({ content, model: s.model });
}
