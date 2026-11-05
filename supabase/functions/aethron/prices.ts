// Providers the managed AI can run on, and what their tokens cost.
// Every one is reached through its OpenAI-compatible endpoint, so the
// gateway and the admin AI speak one wire. Prices are $ per 1M tokens and
// mirror aethron_bridge.PRICES; control.prices overrides any of them.

export const PROVIDERS: Record<string, { label: string; base: string; model: string }> = {
  gemini: { label: "Google Gemini", base: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-3.6-flash" },
  openai: { label: "OpenAI", base: "https://api.openai.com/v1", model: "gpt-4.1-mini" },
  deepseek: { label: "DeepSeek", base: "https://api.deepseek.com/v1", model: "deepseek-flash" },
  anthropic: { label: "Anthropic", base: "https://api.anthropic.com/v1", model: "claude-sonnet-5" },
  openrouter: { label: "OpenRouter", base: "https://openrouter.ai/api/v1", model: "" },
  groq: { label: "Groq", base: "https://api.groq.com/openai/v1", model: "" },
  xai: { label: "xAI", base: "https://api.x.ai/v1", model: "" },
  mistral: { label: "Mistral", base: "https://api.mistral.ai/v1", model: "" },
  custom: { label: "Custom (OpenAI-compatible)", base: "", model: "" },
};

export const PRICES: Record<string, [number, number]> = {
  "deepseek-flash": [0.30, 1.20], "deepseek-v4-flash": [0.30, 1.20],
  "deepseek-v4-pro": [1.32, 3.96], "deepseek-chat": [0.27, 1.10], "deepseek-reasoner": [0.55, 2.19],
  "gemini-2.5-flash": [0.30, 2.50], "gemini-2.5-pro": [1.25, 10.0],
  "gemini-3.8-flash": [0.75, 3.75], "gemini-3.7-flash": [0.75, 3.75], "gemini-3.6-flash": [0.75, 3.75],
  "gemini-3.5-flash-lite": [0.30, 2.50], "gemini-3.1-flash-lite": [0.25, 1.50],
  "gemini-3.1-pro-preview": [2.00, 12.0],
  "gpt-4o-mini": [0.15, 0.60], "gpt-4o": [2.50, 10.0], "gpt-4.1": [2.00, 8.00],
  "gpt-4.1-mini": [0.40, 1.60], "gpt-4.1-nano": [0.10, 0.40],
  "gpt-5": [1.25, 10.0], "gpt-5-mini": [0.25, 2.0], "gpt-5-nano": [0.05, 0.40],
  "o4-mini": [1.10, 4.40], "o3": [2.00, 8.00], "o3-mini": [1.10, 4.40],
  "gpt-6": [1.25, 10.0], "gpt-6-mini": [0.25, 2.0], "gpt-6-nano": [0.05, 0.40],
  "llama-3.1-8b-instant": [0.05, 0.08], "llama-3.3-70b-versatile": [0.59, 0.79],
  "gpt-oss-20b": [0.075, 0.30], "gpt-oss-120b": [0.15, 0.60],
  "claude-sonnet-5": [3.00, 15.0], "claude-haiku-5": [1.00, 5.00], "claude-opus-5": [15.0, 75.0],
};

export const CACHED: Record<string, number> = {
  "deepseek-flash": 0.006, "deepseek-v4-flash": 0.006, "deepseek-v4-pro": 0.044,
  "deepseek-chat": 0.07, "deepseek-reasoner": 0.14,
  "gpt-4o-mini": 0.075, "gpt-4o": 1.25, "gpt-4.1": 0.50, "gpt-4.1-mini": 0.10,
  "gpt-4.1-nano": 0.025, "gpt-5": 0.125, "gpt-5-mini": 0.025, "gpt-5-nano": 0.005,
};

const DEAREST: [number, number] = Object.values(PRICES).reduce((a, b) => (a[0] + a[1] >= b[0] + b[1] ? a : b));

function bare(model: string): string {
  return String(model || "").split("/").pop()!.trim().replace(/-\d{4}-\d{2}-\d{2}$/, "");
}

/** (in, out) $/1M. An unknown model costs the dearest known rate, so a
 *  limit is never switched off by a name nobody listed. */
export function priceOf(model: string, overrides: Record<string, unknown> = {}): [number, number] {
  const m = bare(model);
  const o = overrides && (overrides[m] || overrides[model]);
  if (Array.isArray(o) && o.length >= 2) return [Number(o[0]) || 0, Number(o[1]) || 0];
  return PRICES[m] || DEAREST;
}

export function costOf(model: string, inp: number, out: number, cached = 0,
  overrides: Record<string, unknown> = {}): number {
  const [pin, pout] = priceOf(model, overrides);
  const pc = CACHED[bare(model)] ?? pin;
  const c = Math.min(Math.max(0, cached | 0), Math.max(0, inp | 0));
  return ((inp - c) * pin + c * pc + out * pout) / 1_000_000;
}

/** OpenAI reasoning models refuse max_tokens and any temperature. */
export function openaiReasoning(model: string): boolean {
  const m = String(model || "").trim();
  return !m.includes("/") && /^(gpt-([5-9]|\d{2,})\b|o\d\b)/i.test(m);
}
