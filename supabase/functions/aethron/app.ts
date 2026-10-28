// One edge function, every route. Deployed as `aethron`:
//   https://<project>.supabase.co/functions/v1/aethron/<route>
//
// PUBLIC        GET  /control            the switches (no sign-in needed)
//               GET  /announcement/art?id=  an announcement's illustration (SVG)
// A COPY        POST /ping               heartbeat; returns the switches for this person
//               POST /event              activity events, on the device token
// A PERSON      POST /v1/chat/completions  the managed AI (Aethron's key)
//               GET  /v1/models
//               GET  /whoami             who this token is, and whether they may
//                                        publish (read by the hosting Worker)
// AN ADMIN      GET  /admin/me           who you are, what you may do
//               GET  /admin/tools        every admin tool and its arguments
//               POST /admin/call         {tool, args} — run any admin tool
//               GET  /admin/export       ?kind=&format=&days= — a download
//               POST /admin/ai           {message, thread} — the admin AI
//               GET  /admin/ai/threads, /admin/ai/thread?id=
//               POST /mcp                the admin as an MCP server
//
// Admins sign in with their Aethron account (allow-listed), or present an
// admin API key (ak_...) or the master key.

import { Row } from "./db.ts";
import { runTool, TOOLS, visibleTools } from "./admin.ts";
import { ask, thread, threads } from "./ai.ts";
import { artOf } from "./announce.ts";
import { chat, models } from "./gateway.ts";
import { mcp } from "./mcp.ts";
import { events, ping } from "./presence.ts";
import { admin, caller, control, effective, publicControl, signedOutAfter } from "./state.ts";
import { CORS, fail, HttpError, json, text } from "./util.ts";

function route(req: Request): string {
  const p = new URL(req.url).pathname;
  // /functions/v1/aethron/admin/me  ->  /admin/me   (and /aethron/admin/me in tests)
  const i = p.indexOf("/aethron");
  const r = i >= 0 ? p.slice(i + "/aethron".length) : p;
  return (r || "/").replace(/\/+$/, "") || "/";
}

export async function handle(req: Request): Promise<Response> {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  const r = route(req);
  try {
    if (r === "/" || r === "/health") return json({ ok: true, service: "aethron" });
    if (r === "/control" && req.method === "GET") return json(publicControl(await control()));
    if (r === "/announcement/art" && req.method === "GET") {
      // an illustration by id: the same bytes forever, and an image that can
      // never run code (served as SVG with a policy that forbids scripts)
      const svg = await artOf(new URL(req.url).searchParams.get("id") || "");
      return text(svg, 200, "image/svg+xml; charset=utf-8", {
        "Cache-Control": "public, max-age=31536000, immutable",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        "X-Content-Type-Options": "nosniff",
      });
    }
    if (r === "/ping" && req.method === "POST") return await ping(req);
    if (r === "/event" && req.method === "POST") return await events(req);
    if (r === "/v1/chat/completions" && req.method === "POST") return await chat(req);
    if (r === "/v1/models") return await models(req);
    if (r === "/whoami" && req.method === "GET") {
      // The hosting Worker asks this before it takes an upload: the same
      // person, the same holds and pauses as everywhere else.
      const who = await caller(req);
      if (!who || !who.user_id) return fail(401, "Sign in to Aethron to publish.");
      const e = await effective(who.user_id);
      let why = "";
      // deno-lint-ignore no-explicit-any
      if (signedOutAfter(who as any, e.signout_after)) why = "You were signed out of Aethron. Sign in again.";
      else if (e.control && e.control.paused) why = e.control.pause_message || "Aethron is paused by its team for a moment.";
      else if (e.blocked) why = e.block_reason ? `Your Aethron account is on hold: ${e.block_reason}` : "Your Aethron account is on hold.";
      return json({ user_id: who.user_id, email: who.email, publish: !why, why });
    }

    if (r.startsWith("/admin") || r === "/mcp") {
      const a = await admin(req);
      if (!a) return fail(401, "Not an Aethron admin. Sign in with an allowed account, or use an admin API key.");
      if (r === "/mcp") return await mcp(req, a);
      if (r === "/admin/me") return json({ actor: a.actor, email: a.email, role: a.role, scopes: a.scopes, via: a.via });
      if (r === "/admin/tools") {
        return json(visibleTools(a).map((t) => ({ name: t.name, scope: t.scope, description: t.description,
          params: t.params, required: t.required || [] })).concat([]));
      }
      if (r === "/admin/call" && req.method === "POST") {
        const b: Row = await req.json().catch(() => ({}));
        if (!b.tool) return fail(400, "name a tool: {tool, args}");
        return json({ ok: true, result: await runTool(String(b.tool), b.args || {}, a) });
      }
      if (r === "/admin/export") {
        const u = new URL(req.url);
        const out = await runTool("export_report", {
          kind: u.searchParams.get("kind") || "all", format: u.searchParams.get("format") || "md",
          days: Number(u.searchParams.get("days") || 7),
        }, a) as { filename: string; type: string; body: string };
        return text(out.body, 200, out.type, { "Content-Disposition": `attachment; filename="${out.filename}"` });
      }
      if (r === "/admin/ai" && req.method === "POST") {
        const b: Row = await req.json().catch(() => ({}));
        return json(await ask(String(b.message || ""), String(b.thread || ""), a));
      }
      if (r === "/admin/ai/threads") return json(await threads());
      if (r === "/admin/ai/thread") return json(await thread(new URL(req.url).searchParams.get("id") || ""));
      return fail(404, `no admin route ${r} (tools: ${TOOLS.length})`);
    }
    return fail(404, `no route ${r}`);
  } catch (e) {
    if (e instanceof HttpError) return fail(e.status, e.message);
    return fail(500, "the admin function failed: " + (e as Error).message);
  }
}
