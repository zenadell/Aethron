// Small shared pieces: environment, JSON replies, hashing, time.
// Everything here is a web standard (fetch, Request, crypto.subtle), so the
// same code runs in Supabase's Deno edge runtime and in Bun for the tests.

// deno-lint-ignore no-explicit-any
const G: any = globalThis;

/** An environment value. Tests set globalThis.__AE_ENV. */
export function env(name: string, fallback = ""): string {
  const over = G.__AE_ENV;
  if (over && name in over) return String(over[name] ?? "");
  try {
    if (G.Deno?.env) return G.Deno.env.get(name) ?? fallback;
  } catch (_) { /* no permission */ }
  try {
    if (G.process?.env) return G.process.env[name] ?? fallback;
  } catch (_) { /* none */ }
  return fallback;
}

/** fetch, swappable by the tests (they route it to fakes). */
export function http(input: string, init?: RequestInit): Promise<Response> {
  const f = G.__AE_FETCH || fetch;
  return f(input, init);
}

/** Work that should finish after the reply is sent (ledger writes). */
export function later(p: Promise<unknown>): void {
  const swallow = p.catch(() => {});
  try {
    if (G.EdgeRuntime?.waitUntil) return G.EdgeRuntime.waitUntil(swallow);
  } catch (_) { /* not in the edge runtime */ }
  (G.__AE_PENDING ||= []).push(swallow);
}

export const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-aethron-purpose, x-aethron-machine, mcp-session-id",
  "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
  "Access-Control-Expose-Headers": "mcp-session-id, content-disposition",
};

export function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS, ...extra },
  });
}

export function text(body: string, status = 200, type = "text/plain", extra: Record<string, string> = {}): Response {
  return new Response(body, { status, headers: { "Content-Type": type + "; charset=utf-8", ...CORS, ...extra } });
}

export function fail(status: number, message: string, extra: Record<string, unknown> = {}): Response {
  return json({ error: { message, ...extra } }, status);
}

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function sha256(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function randomToken(prefix: string, bytes = 24): string {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  const b64 = btoa(String.fromCharCode(...a)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return prefix + b64;
}

export function nowIso(): string {
  return new Date(nowMs()).toISOString();
}

/** The clock, swappable by the tests. */
export function nowMs(): number {
  return G.__AE_NOW ? G.__AE_NOW() : Date.now();
}

/** Midnight UTC today, as ISO — the day a daily allowance counts. */
export function dayStartIso(): string {
  const d = new Date(nowMs());
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())).toISOString();
}

export function daysAgoIso(days: number): string {
  return new Date(nowMs() - days * 86400_000).toISOString();
}

/** A key shown to a person: never the key, only enough to tell keys apart. */
export function fingerprint(key: string): string {
  const k = String(key || "");
  if (k.length <= 8) return k ? "••••" : "";
  return k.slice(0, 4) + "…" + k.slice(-4);
}

export function clip(s: unknown, n = 400): string {
  const t = typeof s === "string" ? s : JSON.stringify(s ?? "");
  return t.length > n ? t.slice(0, n) + "…" : t;
}

export function num(v: unknown, d = 0): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

/** "1.1.40" vs "1.1.9" compared as numbers, part by part. */
export function versionLess(a: string, b: string): boolean {
  const pa = String(a || "").split(".").map((x) => parseInt(x, 10) || 0);
  const pb = String(b || "").split(".").map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x < y;
  }
  return false;
}
