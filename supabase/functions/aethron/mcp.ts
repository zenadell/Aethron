// The admin as an MCP server, so a coding agent (Claude Code, Codex,
// Cursor...) can read the logs, run the admin tools and talk to the admin
// AI. Streamable HTTP, answered with JSON. Auth: an admin API key (ak_...)
// or the master key, sent as a Bearer token — exactly like the REST API.

import { Row } from "./db.ts";
import { runTool, visibleTools } from "./admin.ts";
import { ask } from "./ai.ts";
import { Admin } from "./state.ts";
import { clip, json, randomToken, text } from "./util.ts";

const PROTOCOL = "2025-06-18";

function list(a: Admin): Row[] {
  const tools = visibleTools(a).map((t) => ({
    name: t.name,
    description: `${t.description} (needs ${t.scope})`,
    inputSchema: { type: "object", properties: t.params, required: t.required || [] },
  }));
  tools.push({
    name: "ask_admin_ai",
    description: "Ask the admin AI (it reads the logs and runs these tools itself). Pass thread to continue a conversation.",
    inputSchema: { type: "object", properties: { message: { type: "string" }, thread: { type: "string" } }, required: ["message"] },
  });
  return tools;
}

export async function mcp(req: Request, a: Admin): Promise<Response> {
  if (req.method === "GET") return text("This MCP server answers POST requests (JSON-RPC).", 405);
  // deno-lint-ignore no-explicit-any
  let msg: any;
  try {
    msg = await req.json();
  } catch (_) {
    return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } }, 400);
  }
  if (Array.isArray(msg)) {
    const out = [];
    for (const m of msg) {
      const r = await one(m, a);
      if (r) out.push(r);
    }
    return out.length ? json(out) : new Response(null, { status: 202 });
  }
  const r = await one(msg, a);
  if (!r) return new Response(null, { status: 202 });
  const extra: Record<string, string> = msg.method === "initialize" ? { "Mcp-Session-Id": randomToken("s_", 12) } : {};
  return json(r, 200, extra);
}

async function one(m: Row, a: Admin): Promise<Row | null> {
  const id = m.id;
  if (id === undefined || id === null) return null; // a notification
  const ok = (result: unknown) => ({ jsonrpc: "2.0", id, result });
  switch (m.method) {
    case "initialize":
      return ok({ protocolVersion: (m.params && m.params.protocolVersion) || PROTOCOL,
        capabilities: { tools: {} }, serverInfo: { name: "aethron-admin", version: "1.0.0" },
        instructions: "Aethron's admin. Read with overview, list_users, errors, usage_report; act with the write tools (you have: " +
          a.scopes.join(", ") + "). Every action is audited." });
    case "ping":
      return ok({});
    case "tools/list":
      return ok({ tools: list(a) });
    case "tools/call": {
      const name = m.params && m.params.name;
      const args = (m.params && m.params.arguments) || {};
      try {
        const result = name === "ask_admin_ai" ? await ask(args.message, args.thread, a)
          : await runTool(name, args, { ...a, via: "mcp" });
        const body = name === "export_report" ? (result as Row).body : JSON.stringify(result, null, 1);
        return ok({ content: [{ type: "text", text: clip(body, 200000) }],
          structuredContent: typeof result === "object" && !Array.isArray(result) ? result : { result } });
      } catch (e) {
        return ok({ content: [{ type: "text", text: "ERROR: " + (e as Error).message }], isError: true });
      }
    }
    default:
      return { jsonrpc: "2.0", id, error: { code: -32601, message: `unknown method ${m.method}` } };
  }
}
