// Aethron hosting: every published site at https://<name>.aethron.site.
//
// One Worker, one R2 bucket (binding SITES). Nothing in it is a server we
// run: Cloudflare serves the files from its edge.
//
//   sites/<name>.json            {owner, email, live, created, updated}
//   deploys/<name>/<id>.json     {files: {path: sha1}, created, state}
//   blobs/<sha1>                 file bytes, shared between deploys and sites
//
// THE API lives on the bare domain, https://aethron.site/__api/v1/...
//   GET    /me                          who you are (Aethron device token)
//   GET    /sites                       your sites
//   POST   /sites            {name}     claim a name
//   POST   /sites/:n/deploys {files}    -> {id, required: [sha1...]}
//   PUT    /blobs/:sha1      (bytes)    upload one file; its hash is checked
//   POST   /sites/:n/deploys/:id/finish  every file present -> live
//   GET    /sites/:n/deploys            versions, newest first
//   POST   /sites/:n/rollback {id}      put an older version live
//   POST   /sites/:n/rename  {name}     a new address
//   DELETE /sites/:n                    unpublish
//
// Who may publish is decided by Aethron's own server (AUTH_URL/whoami): the
// same account, the same holds and pauses as the rest of Aethron. The Worker
// keeps no keys of its own.

const NAME_RE = /^[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$/;
const SHA_RE = /^[0-9a-f]{40}$/;
const RESERVED = new Set(["www", "api", "app", "admin", "mail", "email", "smtp", "ftp", "ns", "ns1",
  "ns2", "dashboard", "docs", "help", "support", "status", "blog", "aethron", "jomiez", "cdn",
  "static", "assets", "login", "signin", "signup", "account", "billing", "secure", "root"]);
const MAX_FILE = 25 * 1024 * 1024;
const MAX_FILES = 5000;
const TYPES = {
  html: "text/html; charset=utf-8", htm: "text/html; charset=utf-8", css: "text/css; charset=utf-8",
  js: "text/javascript; charset=utf-8", mjs: "text/javascript; charset=utf-8",
  json: "application/json", map: "application/json", txt: "text/plain; charset=utf-8",
  xml: "application/xml", svg: "image/svg+xml", png: "image/png", jpg: "image/jpeg",
  jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", avif: "image/avif", ico: "image/x-icon",
  woff: "font/woff", woff2: "font/woff2", ttf: "font/ttf", otf: "font/otf", mp4: "video/mp4",
  webm: "video/webm", mp3: "audio/mpeg", wav: "audio/wav", pdf: "application/pdf",
  webmanifest: "application/manifest+json", wasm: "application/wasm",
};
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...CORS } });
}
const fail = (status, error) => json({ error }, status);

async function getJSON(env, key) {
  const o = await env.SITES.get(key);
  return o ? await o.json() : null;
}
const putJSON = (env, key, v) => env.SITES.put(key, JSON.stringify(v), { httpMetadata: { contentType: "application/json" } });

async function sha1Hex(buf) {
  const d = await crypto.subtle.digest("SHA-1", buf);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function cleanPath(p) {
  const s = String(p || "").replace(/^\/+/, "");
  if (!s || s.length > 512 || s.split("/").some((x) => x === ".." || x === "." || x === "")) return null;
  if (/[\u0000-\u001f\\]/.test(s)) return null;
  return s;
}

// Cloudflare lets one request touch storage at most 1000 times, and a site
// can have thousands of files, so nothing here looks files up one by one.
// Each upload leaves an empty marker up/<account>/<sha1>; what an account
// has uploaded is then read by listing, 1000 names per call.
async function uploadedBy(env, uid) {
  const have = new Set();
  let cursor;
  for (let page = 0; page < 200; page++) {
    const l = await env.SITES.list({ prefix: `up/${uid}/`, limit: 1000, cursor });
    for (const o of l.objects) have.add(o.key.slice(o.key.lastIndexOf("/") + 1));
    if (!l.truncated) break;
    cursor = l.cursor;
  }
  return have;
}

const WHO = new Map();   // token -> {at, who}: a short cache, the server stays the authority
async function whoami(req, env) {
  const h = req.headers.get("authorization") || "";
  const tok = h.toLowerCase().startsWith("bearer ") ? h.slice(7).trim() : "";
  if (!tok) return { error: "Sign in to Aethron to publish.", status: 401 };
  const hit = WHO.get(tok);
  if (hit && Date.now() - hit.at < 60_000) return hit.who;
  let r;
  try {
    r = await fetch(env.AUTH_URL.replace(/\/+$/, "") + "/whoami", { headers: { authorization: "Bearer " + tok } });
  } catch (e) {
    return { error: "Aethron's account server could not be reached. Try again in a moment.", status: 503 };
  }
  const body = await r.json().catch(() => ({}));
  let who;
  const said = typeof body.error === "string" ? body.error : (body.error && body.error.message) || "";
  if (r.status === 401 || r.status === 403) who = { error: said || "Sign in to Aethron to publish.", status: 401 };
  else if (!r.ok || !body.user_id) who = { error: "Aethron's account server answered " + r.status, status: 503 };
  else if (!body.publish) who = { error: body.why || "Publishing is switched off for this account.", status: 403 };
  else who = { user_id: body.user_id, email: body.email || "" };
  if (!who.error || who.status !== 503) WHO.set(tok, { at: Date.now(), who });
  if (WHO.size > 5000) WHO.clear();
  return who;
}

async function ownSite(env, name, who) {
  if (!NAME_RE.test(name)) return [null, fail(404, "no such site")];
  const s = await getJSON(env, `sites/${name}.json`);
  if (!s) return [null, fail(404, "no such site")];
  if (s.owner !== who.user_id) return [null, fail(403, "this site belongs to another account")];
  return [s, null];
}

function siteUrl(env, name) {
  return `https://${name}.${env.ROOT_DOMAIN}`;
}

async function api(req, env, path) {
  if (path === "/v1/health") return json({ ok: true, root: env.ROOT_DOMAIN });
  const who = await whoami(req, env);
  if (who.error) return fail(who.status, who.error);
  const m = req.method;
  let g;
  if (path === "/v1/me") return json({ user_id: who.user_id, email: who.email });

  if (path === "/v1/sites" && m === "GET") {
    const out = [];
    let cursor;
    do {
      const l = await env.SITES.list({ prefix: "sites/", cursor, limit: 1000 });
      for (const o of l.objects) {
        const s = await getJSON(env, o.key);
        if (s && s.owner === who.user_id) {
          const name = o.key.slice(6, -5);
          out.push({ name, url: siteUrl(env, name), live: s.live || null, updated: s.updated });
        }
      }
      cursor = l.truncated ? l.cursor : undefined;
    } while (cursor);
    return json({ sites: out });
  }

  if (path === "/v1/sites" && m === "POST") {
    const b = await req.json().catch(() => ({}));
    const name = String(b.name || "").toLowerCase();
    if (!NAME_RE.test(name)) return fail(400, "an address is 1-42 letters, digits or dashes, not starting or ending with a dash");
    if (RESERVED.has(name)) return fail(409, `"${name}" is reserved`);
    const prev = await getJSON(env, `sites/${name}.json`);
    if (prev && prev.owner !== who.user_id) return fail(409, `"${name}.${env.ROOT_DOMAIN}" is taken`);
    if (!prev) {
      const now = new Date().toISOString();
      await putJSON(env, `sites/${name}.json`, { owner: who.user_id, email: who.email, live: null, created: now, updated: now });
    }
    return json({ name, url: siteUrl(env, name), created: !prev }, prev ? 200 : 201);
  }

  if ((g = path.match(/^\/v1\/blobs\/([0-9a-f]{40})$/)) && m === "PUT") {
    const len = Number(req.headers.get("content-length") || 0);
    if (len > MAX_FILE) return fail(413, "a single file can be at most 25 MB");
    const buf = await req.arrayBuffer();
    if (buf.byteLength > MAX_FILE) return fail(413, "a single file can be at most 25 MB");
    if ((await sha1Hex(buf)) !== g[1]) return fail(400, "the file does not match its hash");
    if (!(await env.SITES.head(`blobs/${g[1]}`))) await env.SITES.put(`blobs/${g[1]}`, buf);
    await env.SITES.put(`up/${who.user_id}/${g[1]}`, "");
    return json({ ok: true });
  }

  if ((g = path.match(/^\/v1\/sites\/([^/]+)(\/.*)?$/))) {
    const name = g[1], rest = g[2] || "";
    const [site, err] = await ownSite(env, name, who);
    if (err) return err;

    if (rest === "/deploys" && m === "POST") {
      const b = await req.json().catch(() => ({}));
      const files = b.files && typeof b.files === "object" ? b.files : null;
      if (!files) return fail(400, "send {files: {path: sha1}}");
      const clean = {};
      for (const [p, h] of Object.entries(files)) {
        const cp = cleanPath(p);
        if (!cp || !SHA_RE.test(String(h))) return fail(400, `bad file entry: ${String(p).slice(0, 80)}`);
        clean[cp] = h;
      }
      const n = Object.keys(clean).length;
      if (!n) return fail(400, "a site needs at least one file");
      if (n > MAX_FILES) return fail(413, `a site can have at most ${MAX_FILES} files`);
      const have = await uploadedBy(env, who.user_id);
      if (site.live) {
        const prev = await getJSON(env, `deploys/${name}/${site.live}.json`);
        for (const h of Object.values((prev && prev.files) || {})) have.add(h);
      }
      const required = [...new Set(Object.values(clean))].filter((h) => !have.has(h));
      const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      await putJSON(env, `deploys/${name}/${id}.json`, { files: clean, required, created: new Date().toISOString(), state: "uploading" });
      return json({ id, required });
    }

    if ((g = rest.match(/^\/deploys\/([a-z0-9]+)\/finish$/)) && m === "POST") {
      const d = await getJSON(env, `deploys/${name}/${g[1]}.json`);
      if (!d) return fail(404, "no such version");
      // blobs are content-addressed and never removed, so only the ones that
      // were missing when the version was made need checking again
      const have = await uploadedBy(env, who.user_id);
      const missing = (d.required || []).filter((h) => !have.has(h));
      if (missing.length) return fail(409, `${missing.length} file(s) were not uploaded`);
      d.state = "ready";
      await putJSON(env, `deploys/${name}/${g[1]}.json`, d);
      Object.assign(site, { live: g[1], updated: new Date().toISOString() });
      await putJSON(env, `sites/${name}.json`, site);
      return json({ id: g[1], url: siteUrl(env, name), state: "ready" });
    }

    if (rest === "/deploys" && m === "GET") {
      const l = await env.SITES.list({ prefix: `deploys/${name}/`, limit: 1000 });
      const out = [];
      for (const o of l.objects) {
        const d = await getJSON(env, o.key);
        if (d && d.state === "ready") {
          const id = o.key.split("/").pop().slice(0, -5);
          out.push({ id, created: d.created, live: id === site.live, files: Object.keys(d.files).length });
        }
      }
      out.sort((a, b) => (a.created < b.created ? 1 : -1));
      return json({ deploys: out.slice(0, 50) });
    }

    if (rest === "/rollback" && m === "POST") {
      const b = await req.json().catch(() => ({}));
      const d = await getJSON(env, `deploys/${name}/${String(b.id || "")}.json`);
      if (!d || d.state !== "ready") return fail(404, "no such version");
      Object.assign(site, { live: String(b.id), updated: new Date().toISOString() });
      await putJSON(env, `sites/${name}.json`, site);
      return json({ id: site.live, url: siteUrl(env, name) });
    }

    if (rest === "/rename" && m === "POST") {
      const b = await req.json().catch(() => ({}));
      const to = String(b.name || "").toLowerCase();
      if (!NAME_RE.test(to)) return fail(400, "an address is 1-42 letters, digits or dashes");
      if (RESERVED.has(to)) return fail(409, `"${to}" is reserved`);
      if (await env.SITES.head(`sites/${to}.json`)) return fail(409, `"${to}.${env.ROOT_DOMAIN}" is taken`);
      const l = await env.SITES.list({ prefix: `deploys/${name}/`, limit: 1000 });
      for (const o of l.objects) {
        const d = await env.SITES.get(o.key);
        await env.SITES.put(`deploys/${to}/` + o.key.split("/").pop(), await d.arrayBuffer());
        await env.SITES.delete(o.key);
      }
      await putJSON(env, `sites/${to}.json`, { ...site, updated: new Date().toISOString() });
      await env.SITES.delete(`sites/${name}.json`);
      return json({ name: to, url: siteUrl(env, to) });
    }

    if (rest === "" && m === "DELETE") {
      const l = await env.SITES.list({ prefix: `deploys/${name}/`, limit: 1000 });
      for (const o of l.objects) await env.SITES.delete(o.key);
      await env.SITES.delete(`sites/${name}.json`);
      return json({ deleted: name });
    }
  }
  return fail(404, "no such route");
}

function typeOf(path) {
  const ext = path.includes(".") ? path.split(".").pop().toLowerCase() : "";
  return TYPES[ext] || "application/octet-stream";
}

const PAGE = (title, body, status) => new Response(
  `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
  `<title>${title}</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f0e0d;color:#efe8df;` +
  `font:15px/1.5 -apple-system,system-ui,sans-serif}main{max-width:420px;padding:24px;text-align:center}h1{font-size:22px;margin:0 0 8px}` +
  `p{color:#a59c92;margin:0 0 18px}a{color:#d97757}</style></head><body><main><h1>${title}</h1><p>${body}</p>` +
  `<a href="https://aethron.jomiez.com/?ref=hosting">Built with Aethron</a></main></body></html>`,
  { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });

const DEPS = new Map();
async function serve(req, env, name, path) {
  const site = await getJSON(env, `sites/${name}.json`);
  if (!site || !site.live) return PAGE("Nothing here yet", `${name}.${env.ROOT_DOMAIN} has not been published.`, 404);
  // a version never changes once made, so its file list is kept in memory
  const ck = `${name}/${site.live}`;
  let dep = DEPS.get(ck);
  if (!dep) {
    dep = await getJSON(env, `deploys/${name}/${site.live}.json`);
    if (!dep) return PAGE("Nothing here yet", "This site is being published. Try again in a moment.", 404);
    if (DEPS.size > 200) DEPS.clear();
    DEPS.set(ck, dep);
  }
  const files = dep.files;
  let p = decodeURIComponent(path).replace(/^\/+/, "");
  let status = 200;
  let key = files[p] && p;
  if (!key && (p === "" || p.endsWith("/"))) key = files[p + "index.html"] && p + "index.html";
  if (!key && p && !p.endsWith("/") && files[p + "/index.html"]) {
    return Response.redirect(new URL("/" + p + "/", req.url).toString(), 301);
  }
  if (!key) key = files[p + ".html"] && p + ".html";
  if (!key && files["404.html"]) { key = "404.html"; status = 404; }
  if (!key) return PAGE("Page not found", "There is no page at this address.", 404);
  const sha = files[key];
  const etag = `"${sha}"`;
  const html = key.endsWith(".html") || key.endsWith(".htm");
  const headers = {
    "Content-Type": typeOf(key), ETag: etag,
    // a page is checked each time (a new version shows at once); its files
    // are named by content in the store, so the browser may keep them
    "Cache-Control": html ? "public, max-age=0, must-revalidate" : "public, max-age=300, must-revalidate",
    "X-Content-Type-Options": "nosniff",
  };
  if (req.headers.get("if-none-match") === etag && status === 200) return new Response(null, { status: 304, headers });
  const obj = await env.SITES.get(`blobs/${sha}`);
  if (!obj) return PAGE("Page not found", "This file is missing from the site.", 404);
  return new Response(req.method === "HEAD" ? null : obj.body, { status, headers });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const host = url.hostname.toLowerCase();
    const root = env.ROOT_DOMAIN;
    try {
      if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
      if ((host === root || host === "www." + root) && url.pathname.startsWith("/__api/")) {
        return await api(req, env, url.pathname.slice("/__api".length).replace(/\/+$/, ""));
      }
      if (host === root || host === "www." + root) {
        return Response.redirect(env.HOME_URL || "https://aethron.jomiez.com/", 302);
      }
      if (!host.endsWith("." + root)) return PAGE("Not found", "This address is not an Aethron site.", 404);
      const name = host.slice(0, -(root.length + 1));
      if (!NAME_RE.test(name)) return PAGE("Not found", "This address is not an Aethron site.", 404);
      if (req.method !== "GET" && req.method !== "HEAD") return new Response("Method not allowed", { status: 405 });
      return await serve(req, env, name, url.pathname);
    } catch (e) {
      return fail(500, "Aethron hosting hit an error: " + (e && e.message ? e.message : e));
    }
  },
};
