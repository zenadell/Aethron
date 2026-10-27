// The admin's own AI. Separate from the AI that powers Aethron for users:
// its own provider, model and (optionally) key. It works the admin through
// the SAME tools a person clicks, with the permissions of whoever asked,
// and every change it makes is in the audit log under "via: ai".

import { db, Row } from "./db.ts";
import { costOf, openaiReasoning, PROVIDERS } from "./prices.ts";
import { runTool, visibleTools } from "./admin.ts";
import { upstream } from "./gateway.ts";
import { Admin, control, keyList, secret } from "./state.ts";
import { clip, http, HttpError, nowIso, nowMs, randomToken } from "./util.ts";

const MAX_STEPS = 12;

function system(a: Admin): string {
  return `You are the Aethron admin assistant. Aethron is a desktop app (with an MCP server for outside coding agents) that migrates and builds websites; you run its admin for ${a.actor} (role: ${a.role}). Now: ${nowIso()}.

You act ONLY through the tools. Rules:
- Read before you change: look at the numbers with a read tool first, and quote them exactly.
- Change things only when the admin asked for that change, in so many words. Never pause Aethron, delete a user, change keys, switch the AI mode or sign people out on your own idea — propose it and let them confirm.
- After a change, say exactly what you changed and what it does for users.
- Never print an API key or secret, even if a tool output seems to contain one.
- When asked for a file or a report, use export_report and give the admin its content.
- If a tool fails, say what failed and why; do not pretend it worked.
- Announcements: when the admin asks you to announce something (or to set a banner people should notice), write a short, warm, professional title (≤ 7 words) and one or two plain sentences, then call publish_announcement — it draws a matching animated illustration in the house style by itself. Use preview_illustration first only if they want to see it before it goes out. Never put internal details, keys or blame in an announcement.
Be brief and concrete. Use short lists and the real figures.`;
}

export async function adminAiSettings(): Promise<{ provider: string; model: string; base: string; key: string }> {
  const c = await control();
  const provider = (await secret("admin_ai_provider")) || c.managed_provider || "gemini";
  const p = PROVIDERS[provider] || PROVIDERS.custom;
  const model = (await secret("admin_ai_model")) || p.model;
  const { base, keys } = await upstream(provider);
  const key = (await secret("admin_ai_key")) || keys[0] || "";
  if (!key) {
    throw new HttpError(400, `The admin AI has no key. In "AI & models", give it a provider and key (or add a ${provider} key).`);
  }
  if (!base || !model) throw new HttpError(400, "The admin AI needs a provider and model (AI & models).");
  return { provider, model, base, key };
}

function schema(a: Admin) {
  return visibleTools(a).map((t) => ({
    type: "function",
    function: {
      name: t.name, description: `${t.description} [needs ${t.scope}]`,
      parameters: { type: "object", properties: t.params, required: t.required || [] },
    },
  }));
}

async function history(thread: string): Promise<Row[]> {
  const rows = await db.select("admin_ai_messages", { thread: "eq." + thread, order: "id.desc", limit: 40 });
  const msgs = rows.reverse().map((r) => r.content);
  // a tool answer must follow the call that asked for it; drop a cut-off head
  while (msgs.length && msgs[0].role === "tool") msgs.shift();
  return msgs;
}

async function save(thread: string, actor: string, m: Row): Promise<void> {
  await db.insert("admin_ai_messages", { thread, actor, role: m.role, content: m });
}

export async function ask(message: string, threadIn: string, a: Admin): Promise<Row> {
  const msg = String(message || "").trim();
  if (!msg) throw new HttpError(400, "ask the admin AI something");
  const thread = /^[A-Za-z0-9_-]{6,64}$/.test(threadIn || "") ? threadIn : randomToken("t_", 9);
  const st = await adminAiSettings();
  const msgs: Row[] = [{ role: "system", content: system(a) }, ...(await history(thread))];
  const user = { role: "user", content: msg };
  msgs.push(user);
  await save(thread, a.actor, user);
  const actions: Row[] = [];
  let spent = 0;
  const ai: Admin = { ...a, via: "ai", actor: a.actor + " (via admin AI)" };

  for (let step = 0; step < MAX_STEPS; step++) {
    const body: Row = { model: st.model, messages: msgs, tools: schema(a), tool_choice: "auto" };
    if (!openaiReasoning(st.model)) body.temperature = 0.2;
    const t0 = nowMs();
    const r = await http(st.base + "/chat/completions", {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + st.key },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text().catch(() => "");
      throw new HttpError(502, `the admin AI (${st.provider} ${st.model}) answered ${r.status}: ${clip(t, 300)}`);
    }
    const j = await r.json();
    const u = j.usage || {};
    const cost = costOf(st.model, Number(u.prompt_tokens || 0), Number(u.completion_tokens || 0));
    spent += cost;
    db.insert("usage_ledger", { provider: st.provider, model: st.model, purpose: "admin_ai",
      input_tokens: Number(u.prompt_tokens || 0), output_tokens: Number(u.completion_tokens || 0),
      cost_usd: Math.round(cost * 1e6) / 1e6, ms: nowMs() - t0, estimated: !j.usage }).catch(() => {});
    const m = (j.choices && j.choices[0] && j.choices[0].message) || { role: "assistant", content: "" };
    const out: Row = { role: "assistant", content: m.content || "" };
    if (m.tool_calls && m.tool_calls.length) out.tool_calls = m.tool_calls;
    msgs.push(out);
    await save(thread, a.actor, out);
    if (!out.tool_calls) {
      return { thread, reply: out.content, actions, steps: step + 1, cost_usd: Math.round(spent * 1e6) / 1e6 };
    }
    for (const call of out.tool_calls) {
      const name = call.function && call.function.name;
      let args: Row = {};
      try {
        args = JSON.parse(call.function.arguments || "{}");
      } catch (_) { /* leave empty; the tool will say what is missing */ }
      let result: unknown, ok = true;
      try {
        result = await runTool(name, args, ai);
      } catch (e) {
        ok = false;
        result = { error: (e as Error).message };
      }
      const shown = name === "export_report" && ok ? { ...(result as Row), body: clip((result as Row).body, 14000) } : result;
      actions.push({ tool: name, args: redact(args), ok, summary: clip(shown, 300) });
      const tm = { role: "tool", tool_call_id: call.id, content: clip(shown, 14000) };
      msgs.push(tm);
      await save(thread, a.actor, tm);
    }
  }
  return { thread, reply: "I stopped after " + MAX_STEPS + " steps. Here is what I did so far.", actions, steps: MAX_STEPS,
    cost_usd: Math.round(spent * 1e6) / 1e6 };
}

/** Never write a key into the action list the admin sees. */
function redact(args: Row): Row {
  const out: Row = {};
  for (const [k, v] of Object.entries(args || {})) {
    out[k] = /key|secret|token|password/i.test(k) && v ? "[hidden: " + keyList(String(v)).length + " value(s)]" : v;
  }
  return out;
}

export async function threads(): Promise<Row[]> {
  const rows = await db.select("admin_ai_messages", { select: "thread,at,actor,role,content", role: "eq.user",
    order: "id.desc", limit: 300 });
  const seen = new Map<string, Row>();
  for (const r of rows) {
    if (!seen.has(r.thread)) seen.set(r.thread, { thread: r.thread, at: r.at, actor: r.actor, first: clip(r.content.content, 120) });
  }
  return [...seen.values()];
}

export async function thread(id: string): Promise<Row[]> {
  return (await db.select("admin_ai_messages", { thread: "eq." + id, order: "id.asc", limit: 400 })).map((r) => ({
    at: r.at, actor: r.actor, ...r.content,
  }));
}
