// The hosting Worker, run in Bun with an in-memory R2 and a fake account
// server — the real worker.js, nothing re-implemented.
//   bun tests/hosting/fake.ts <port>   prints "ready" when listening
// Tokens: "good-<user>" publish; "held" is on hold; anything else is refused.
import worker from "../../cloud/hosting/worker.js";

class Obj {
  constructor(public data: Uint8Array) {}
  get body() { return new Response(this.data).body; }
  async json() { return JSON.parse(new TextDecoder().decode(this.data)); }
  async arrayBuffer() { return this.data.buffer.slice(this.data.byteOffset, this.data.byteOffset + this.data.byteLength); }
}
export class FakeR2 {
  m = new Map<string, Uint8Array>();
  async get(k: string) { const v = this.m.get(k); return v ? new Obj(v) : null; }
  // a real R2 lookup takes tens of milliseconds; asked one by one, hundreds add up
  async head(k: string) { await new Promise((r) => setTimeout(r, 25)); return this.m.has(k) ? {} : null; }
  // like R2 now and then: one write in 40 fails once with "internal error"
  puts = 0;
  async put(k: string, v: ArrayBuffer | string | Uint8Array) {
    if (++this.puts % 40 === 0) throw new Error("put: We encountered an internal error. Please try again. (10001)");
    this.m.set(k, typeof v === "string" ? new TextEncoder().encode(v) : new Uint8Array(v as ArrayBuffer));
  }
  async delete(k: string) { this.m.delete(k); }
  async list(o: { prefix: string; limit?: number; cursor?: string }) {
    const all = [...this.m.keys()].filter((k) => k.startsWith(o.prefix)).sort();
    const from = Number(o.cursor || 0), lim = Math.min(o.limit || 1000, 1000);
    const page = all.slice(from, from + lim);
    const more = from + lim < all.length;
    return { objects: page.map((key) => ({ key })), truncated: more, cursor: more ? String(from + lim) : undefined };
  }
}
// Cloudflare's limit: one request may touch storage at most 1000 times
function counted(r2: FakeR2) {
  let n = 0;
  const tick = () => { if (++n > 1000) throw new Error("Too many API requests by single Worker invocation."); };
  return {
    get: (k: string) => (tick(), r2.get(k)), head: (k: string) => (tick(), r2.head(k)),
    put: (k: string, v: any) => (tick(), r2.put(k, v)), delete: (k: string) => (tick(), r2.delete(k)),
    list: (o: any) => (tick(), r2.list(o)),
  };
}

const port = Number(process.argv[2] || 8787);
const r2 = new FakeR2();
const env = { SITES: r2, ROOT_DOMAIN: "aethron.site", AUTH_URL: `http://127.0.0.1:${port}/__auth`, HOME_URL: "https://aethron.jomiez.com/" };
Bun.serve({
  port, hostname: "127.0.0.1",
  async fetch(req) {
    const u = new URL(req.url);
    if (u.pathname === "/__auth/whoami") {
      const t = (req.headers.get("authorization") || "").slice(7);
      if (t.startsWith("good-")) return Response.json({ user_id: t.slice(5), email: t.slice(5) + "@x.com", publish: true, why: "" });
      if (t === "held") return Response.json({ user_id: "h", email: "h@x.com", publish: false, why: "Your Aethron account is on hold." });
      return Response.json({ error: "Sign in to Aethron to publish." }, { status: 401 });
    }
    // like Cloudflare's browser check on aethron.site: Python's default client name is refused (1010)
    if ((req.headers.get("user-agent") || "").startsWith("Python-urllib")) return new Response("error code: 1010", { status: 403 });
    if (u.pathname === "/__stats") return Response.json({ keys: [...r2.m.keys()] });
    // the Host the Worker sees comes from X-Host (a test cannot resolve *.aethron.site)
    const host = req.headers.get("x-host") || "aethron.site";
    const url = new URL(u.pathname + u.search, "https://" + host);
    const fwd = new Request(url, { method: req.method, headers: req.headers, body: ["GET", "HEAD"].includes(req.method) ? undefined : await req.arrayBuffer() });
    return worker.fetch(fwd, { ...env, SITES: counted(r2) });
  },
});
console.log("ready");
