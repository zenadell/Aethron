// The managed AI. When the admin turns it on, every installed copy sends
// its model requests here instead of to a key the person brought. The
// admin's key never leaves this function; each request is checked against
// the switches, priced, and written to the person's ledger.
//
// It speaks the OpenAI chat-completions wire, which Aethron's bridge
// already translates for the coding agent and every provider offers.
// Refusals are 403, never 429: the agent engine treats 429 as "busy, ask
// again" and would retry a deliberate no in silence.

import { db } from "./db.ts";
import { costOf, openaiReasoning, PROVIDERS } from "./prices.ts";
import { caller, control, effective, keyList, secret, signedOutAfter, spentToday, spentTodayAll } from "./state.ts";
import { CORS, fail, http, json, later, nowMs } from "./util.ts";

type Usage = { input: number; output: number; cached: number; estimated: boolean };

function readUsage(u: Record<string, unknown> | undefined | null): Usage | null {
  if (!u) return null;
  // deno-lint-ignore no-explicit-any
  const x = u as any;
  const input = Number(x.prompt_tokens ?? x.input_tokens ?? 0) || 0;
  const output = Number(x.completion_tokens ?? x.output_tokens ?? 0) || 0;
  const cached = Number(x.prompt_cache_hit_tokens ?? x.prompt_tokens_details?.cached_tokens ??
    x.cache_read_input_tokens ?? 0) || 0;
  if (!input && !output) return null;
  return { input, output, cached, estimated: false };
}

export async function upstream(provider: string): Promise<{ base: string; keys: string[] }> {
  const p = PROVIDERS[provider] || PROVIDERS.custom;
  const base = ((await secret("base_" + provider)) || p.base).replace(/\/+$/, "");
  return { base, keys: keyList(await secret("key_" + provider)) };
}

export async function refusal(uid: string, who: { kind: string; iat: number } | null): Promise<string> {
  const e = await effective(uid);
  const c = e.control;
  // deno-lint-ignore no-explicit-any
  if (who && signedOutAfter(who as any, e.signout_after)) return "You were signed out of Aethron. Sign in again.";
  if (c.paused) return c.pause_message || "Aethron is paused by its team for a moment. Please try again later.";
  if (e.blocked) return e.block_reason ? `Your Aethron account is on hold: ${e.block_reason}` : "Your Aethron account is on hold.";
  if (e.ai_mode !== "managed") {
    return e.ai_mode === "off" ? "Aethron's AI is switched off right now."
      : "Aethron's own AI is not switched on for your account — add your own key in Settings.";
  }
  if (e.daily_usd > 0) {
    const spent = await spentToday(uid);
    if (spent >= e.daily_usd) {
      return `You have used today's AI allowance ($${spent.toFixed(2)} of $${e.daily_usd.toFixed(2)}). It resets at 00:00 UTC.`;
    }
  }
  if (c.global_daily_usd > 0 && (await spentTodayAll()) >= c.global_daily_usd) {
    return "Aethron's AI has reached its limit for today. It resets at 00:00 UTC.";
  }
  return "";
}

export async function chat(req: Request): Promise<Response> {
  const who = await caller(req);
  if (!who || !who.user_id) return fail(401, "Sign in to Aethron to use its AI.");
  const why = await refusal(who.user_id, who);
  if (why) return fail(403, why, { type: "aethron_refused" });

  const c = await control();
  const provider = c.managed_provider || "gemini";
  const model = c.managed_model || (PROVIDERS[provider] || PROVIDERS.custom).model;
  const { base, keys } = await upstream(provider);
  if (!base) return fail(503, `Aethron's AI has no address for provider "${provider}" yet.`);
  if (!keys.length) return fail(503, `Aethron's AI has no ${provider} key yet — the team is setting it up.`);
  if (!model) return fail(503, "Aethron's AI has no model chosen yet.");

  // deno-lint-ignore no-explicit-any
  let body: any;
  try {
    body = await req.json();
  } catch (_) {
    return fail(400, "the request is not JSON");
  }
  body.model = model;
  if (openaiReasoning(model)) {
    if (body.max_tokens && !body.max_completion_tokens) body.max_completion_tokens = body.max_tokens;
    delete body.max_tokens;
    delete body.temperature;
  }
  const stream = !!body.stream;
  if (stream) body.stream_options = { include_usage: true };
  const purpose = (req.headers.get("x-aethron-purpose") || "agent").slice(0, 40);
  const machine = (req.headers.get("x-aethron-machine") || who.machine || "").slice(0, 64);
  const t0 = nowMs();
  const inputChars = JSON.stringify(body.messages || []).length;

  const record = (u: Usage | null, status: string, outChars = 0) => {
    const use: Usage = u || {
      input: Math.ceil(inputChars / 4), output: Math.ceil(outChars / 4), cached: 0, estimated: true,
    };
    const cost = status === "ok" ? costOf(model, use.input, use.output, use.cached, c.prices) : 0;
    later(db.insert("usage_ledger", {
      user_id: who.user_id, machine, provider, model, purpose,
      input_tokens: status === "ok" ? use.input : 0, output_tokens: status === "ok" ? use.output : 0,
      cached_tokens: use.cached, cost_usd: Math.round(cost * 1e6) / 1e6, status, ms: nowMs() - t0,
      estimated: use.estimated,
    }));
  };

  let r: Response | null = null;
  let lastErr = "";
  for (const key of keys) {
    for (let attempt = 0; attempt < 2; attempt++) {
      r = await http(base + "/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + key },
        body: JSON.stringify(body),
      });
      if (r.ok) break;
      lastErr = await r.text().catch(() => "");
      // some OpenAI-compatible endpoints refuse stream_options; ask once without
      if (r.status === 400 && /stream_options/i.test(lastErr) && body.stream_options) {
        delete body.stream_options;
        continue;
      }
      break;
    }
    if (r && r.ok) break;
    // a busy or refused KEY: try the next one; anything else is the request's fault
    if (!r || ![401, 402, 403, 429].includes(r.status)) break;
  }
  if (!r || !r.ok) {
    record(null, "error");
    const status = r ? (r.status >= 500 ? 502 : r.status === 429 ? 503 : r.status) : 502;
    let msg = lastErr.slice(0, 400);
    try {
      const j = JSON.parse(lastErr);
      msg = (Array.isArray(j) ? j[0] : j)?.error?.message || msg;
    } catch (_) { /* not JSON */ }
    return fail(status, `Aethron's AI (${provider}) answered ${r ? r.status : "nothing"}: ${msg}`);
  }

  if (!stream) {
    const j = await r.json();
    const outChars = JSON.stringify(j.choices || []).length;
    record(readUsage(j.usage), "ok", outChars);
    return json(j);
  }

  // stream through, untouched, while reading the usage it ends with
  let usage: Usage | null = null;
  let outChars = 0;
  let carry = "";
  const dec = new TextDecoder();
  const ts = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, ctl) {
      ctl.enqueue(chunk);
      carry += dec.decode(chunk, { stream: true });
      const lines = carry.split("\n");
      carry = lines.pop() || "";
      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith("data:")) continue;
        const d = t.slice(5).trim();
        if (!d || d === "[DONE]") continue;
        try {
          const j = JSON.parse(d);
          const u = readUsage(j.usage);
          if (u) usage = u;
          for (const ch of j.choices || []) {
            const delta = ch.delta || {};
            outChars += String(delta.content || "").length + JSON.stringify(delta.tool_calls || "").length;
          }
        } catch (_) { /* a partial or foreign line */ }
      }
    },
    flush() {
      record(usage, "ok", outChars);
    },
  });
  return new Response(r.body!.pipeThrough(ts), {
    status: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", ...CORS },
  });
}

export async function models(req: Request): Promise<Response> {
  const who = await caller(req);
  if (!who) return fail(401, "Sign in to Aethron to use its AI.");
  const c = await control();
  const model = c.managed_model || (PROVIDERS[c.managed_provider] || PROVIDERS.custom).model;
  return json({ object: "list", data: model ? [{ id: model, object: "model", owned_by: "aethron" }] : [] });
}
