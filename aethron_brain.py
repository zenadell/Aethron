#!/usr/bin/env python3
"""Aethron Brain — ONE model setting for the whole product.

The owner's rule, and now the architecture: whatever API key you put in
settings powers EVERYTHING — copy fill, plan polish, design matching,
self-heal, the migration agent AND the coding agent. Not one key for
the template side and another for code.

    aethron_config.json  {"ai": {provider, api_key, model, base_url}}
                     │
        ┌────────────┼─────────────┬──────────────────┐
     studio AI    aethron_agent   coding agent     anything later
    (fill/plan/    (migration      (Claude Code
     match/heal)     loop)          CLI)
                                       │
                          not Anthropic-wire? -> aethron_bridge
                          translates on the way through, so a
                          DeepSeek/Gemini/OpenAI/Ollama key drives
                          the coding agent too.

Providers are a dict entry, never a code path. Adding one is a line.
"""
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "aethron_config.json"

# wire: how the provider expects to be talked to.
#   openai        -> /chat/completions   (the majority)
#   anthropic     -> /v1/messages
#   anthropic-cli -> no key at all: the Claude Code CLI's own login
PROVIDERS = {
    "deepseek": {"label": "DeepSeek", "wire": "openai",
                 "base": "https://api.deepseek.com/v1",
                 "model": "deepseek-v4-pro",
                 "keys": "platform.deepseek.com"},
    "anthropic": {"label": "Anthropic (API key)", "wire": "anthropic",
                  "base": "https://api.anthropic.com",
                  "model": "claude-sonnet-5",
                  "keys": "console.anthropic.com"},
    "claude-cli": {"label": "Claude Code login (no key)",
                   "wire": "anthropic-cli", "base": "", "model": "",
                   "keys": "run `claude` once to sign in",
                   "code_only": True},
    "gemini": {"label": "Google Gemini", "wire": "openai",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.5-flash", "keys": "aistudio.google.com"},
    "openai": {"label": "OpenAI", "wire": "openai",
               "base": "https://api.openai.com/v1", "model": "gpt-5",
               "keys": "platform.openai.com"},
    "openrouter": {"label": "OpenRouter (any model)", "wire": "openai",
                   "base": "https://openrouter.ai/api/v1", "model": "",
                   "keys": "openrouter.ai/keys"},
    "groq": {"label": "Groq", "wire": "openai",
             "base": "https://api.groq.com/openai/v1", "model": "",
             "keys": "console.groq.com"},
    "ollama": {"label": "Ollama (local, free)", "wire": "openai",
               "base": "http://127.0.0.1:11434/v1", "model": "qwen3-coder",
               "keys": "no key needed — install Ollama", "local": True},
    "fcc": {"label": "Free Claude Code proxy", "wire": "anthropic",
            "base": "http://127.0.0.1:8082", "model": "",
            "keys": "token (default: freecc)", "code_only": True},
    "custom": {"label": "Custom endpoint", "wire": "openai", "base": "",
               "model": "", "keys": "your own gateway"},
}

DEFAULT = {"provider": "deepseek", "api_key": "", "model": "",
           "base_url": "", "wire": "",
           # Spend guards. An agent that loops is an agent that spends,
           # so these are ON by default and deliberately low: a single
           # run that needs more should say so out loud.
           # A token ceiling cannot be set once for every model: 2M
           # tokens is a dollar on one and fifteen cents on another, and
           # this one stopped a working agent 36 tool calls in, twice, for
           # 21 cents of real spend. Money is the cap that means
           # something; tokens and requests are backstops.
           "budget_requests": 600, "budget_tokens": 20_000_000,
           "budget_usd": 1.0}


# ────────────────────────── settings ─────────────────────────────────

def load() -> dict:
    """The one place settings live. Env wins (CI, power users), then
    the config file, then the defaults."""
    cfg = dict(DEFAULT)
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8"))
                   .get("ai") or {})
    except Exception:
        pass
    env = os.environ
    for key, var in (("provider", "AETHRON_AI_PROVIDER"),
                     ("api_key", "AETHRON_AI_KEY"),
                     ("model", "AETHRON_AI_MODEL"),
                     ("base_url", "AETHRON_AI_BASE_URL")):
        if env.get(var):
            cfg[key] = env[var]
    return cfg


def save(patch: dict) -> dict:
    try:
        whole = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        whole = {}
    ai = {**(whole.get("ai") or {}), **{k: v for k, v in patch.items()
                                        if k in DEFAULT}}
    whole["ai"] = ai
    CONFIG.write_text(json.dumps(whole, indent=1), encoding="utf-8")
    return ai


def resolve(cfg: dict = None) -> dict:
    """Settings -> everything a caller needs, with an honest `ready`."""
    cfg = {**load(), **(cfg or {})}
    p = PROVIDERS.get(cfg.get("provider") or "", PROVIDERS["custom"])
    base = (cfg.get("base_url") or p["base"]).rstrip("/")
    model = cfg.get("model") or p["model"]
    key = cfg.get("api_key") or ""
    wire = cfg.get("wire") or p["wire"]
    why = ""
    if wire == "anthropic-cli":
        ready = True
        why = "uses the Claude Code CLI's own login"
    elif not base:
        ready, why = False, "set a base URL for this provider"
    elif not key and not p.get("local") and wire != "anthropic-cli":
        ready, why = False, f"add an API key ({p.get('keys', '')})"
    elif not model and wire != "anthropic-cli":
        ready, why = False, "choose a model"
    else:
        ready = True
    return {"provider": cfg.get("provider", ""), "label": p["label"],
            "wire": wire, "base": base, "key": key, "model": model,
            "ready": ready, "why": why, "local": bool(p.get("local")),
            "code_only": bool(p.get("code_only"))}


# ─────────────────── one prompt -> one answer ────────────────────────

def text_call(prompt: str, cfg: dict = None, timeout=300,
              max_tokens=16000) -> str:
    """The simple call the template side makes (fill, plan, match).
    Same settings, same provider, whichever wire it speaks."""
    r = resolve(cfg)
    if not r["ready"]:
        raise ValueError(r["why"] or "AI settings incomplete")
    if r["wire"] == "anthropic":
        url = r["base"] + "/v1/messages"
        body = {"model": r["model"], "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": r["key"], "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        pick = lambda d: d["content"][0]["text"]
    else:
        url = r["base"] + "/chat/completions"
        body = {"model": r["model"],
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"Authorization": "Bearer " + r["key"],
                   "Content-Type": "application/json"}
        pick = lambda d: d["choices"][0]["message"]["content"]
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return pick(json.loads(resp.read()))


# ─────────────────── an Anthropic endpoint, always ───────────────────
# The coding agent (Claude Code CLI) speaks only Anthropic. Rather than
# forcing a second account, we give it one: either the provider already
# speaks that wire, or the bridge translates. Same key, either way.

_BRIDGE = {"srv": None, "url": "", "sig": ""}
_LOCK = threading.Lock()


def bridge_config(cfg: dict = None):
    import aethron_bridge
    r = resolve(cfg)
    if not r["ready"]:
        raise ValueError(r["why"] or "AI settings incomplete")
    return aethron_bridge.BridgeConfig(r["base"], r["key"], r["model"])


def anthropic_endpoint(cfg: dict = None) -> dict:
    """-> {base_url, token, mode, model, why}

    mode: 'cli-login' (no endpoint needed) | 'direct' (provider already
    speaks Anthropic) | 'bridge' (we translate). One bridge per config,
    reused across sessions."""
    r = resolve(cfg)
    if r["wire"] == "anthropic-cli":
        return {"base_url": "", "token": "", "mode": "cli-login",
                "model": r["model"], "why": "the CLI's own login"}
    if not r["ready"]:
        return {"base_url": "", "token": "", "mode": "unconfigured",
                "model": "", "why": r["why"]}
    if r["wire"] == "anthropic":
        return {"base_url": r["base"], "token": r["key"], "mode": "direct",
                "model": r["model"],
                "why": f"{r['label']} speaks the Anthropic API directly"}
    import aethron_bridge
    sig = f"{r['base']}|{r['key'][:8]}|{r['model']}"
    with _LOCK:
        if _BRIDGE["sig"] != sig:
            if _BRIDGE["srv"]:
                _BRIDGE["srv"].shutdown()
            cfg_all = {**load(), **(cfg or {})}
            # pass the DOLLAR cap too — it was defined here and never
            # sent, so the bridge fell back to counting tokens alone
            aethron_bridge.set_limits(
                requests=cfg_all.get("budget_requests"),
                tokens=cfg_all.get("budget_tokens"),
                usd=cfg_all.get("budget_usd"))
            aethron_bridge.reset_usage()
            srv, url = aethron_bridge.start(
                aethron_bridge.BridgeConfig(r["base"], r["key"], r["model"]))
            _BRIDGE.update(srv=srv, url=url, sig=sig)
    return {"base_url": _BRIDGE["url"], "token": "aethron", "mode": "bridge",
            "model": r["model"],
            "why": f"{r['label']} via Aethron's translator "
                   f"(one key for design and code)"}


def spend() -> dict:
    """What this process has spent through the bridge so far."""
    try:
        import aethron_bridge
        return aethron_bridge.usage_report()
    except Exception:
        return {}


def is_paid() -> bool:
    """True when the configured provider charges money. Local models and
    an unconfigured setup are free — everything else is the owner's
    balance."""
    r = resolve()
    return bool(r["ready"] and r["key"] and not r["local"])


def require_live(live: bool, what="this run"):
    """Refuse to spend the owner's balance unless asked to, by name."""
    if not is_paid() or live:
        return
    r = resolve()
    raise SystemExit(
        f"REFUSED: {what} would spend real money ({r['label']} · "
        f"{r['model']}).\n"
        f"  Aethron does not touch a paid key unless you ask for it: "
        f"re-run with --live.\n"
        f"  Caps for a live run: {load().get('budget_requests')} requests / "
        f"{load().get('budget_tokens'):,} tokens (change them in the AI "
        f"settings).")


def shutdown_bridge():
    with _LOCK:
        if _BRIDGE["srv"]:
            _BRIDGE["srv"].shutdown()
            _BRIDGE.update(srv=None, url="", sig="")


def status() -> dict:
    r = resolve()
    return {"settings": {**load(), "api_key":
                         ("set" if load().get("api_key") else "")},
            "resolved": {k: v for k, v in r.items() if k != "key"},
            "providers": {k: {"label": v["label"], "wire": v["wire"],
                              "base": v["base"], "model": v["model"],
                              "keys": v.get("keys", ""),
                              "local": bool(v.get("local")),
                              "code_only": bool(v.get("code_only"))}
                          for k, v in PROVIDERS.items()}}


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        ok = True
        r = resolve({"provider": "deepseek", "api_key": "k", "model": "m"})
        ok &= r["wire"] == "openai" and r["ready"]
        r2 = resolve({"provider": "deepseek", "api_key": "", "model": "m"})
        ok &= not r2["ready"] and "API key" in r2["why"]
        r3 = resolve({"provider": "ollama", "api_key": "", "model": "x"})
        ok &= r3["ready"]        # local models need no key
        e = anthropic_endpoint({"provider": "anthropic", "api_key": "k",
                                "model": "claude-sonnet-5"})
        ok &= e["mode"] == "direct"
        e2 = anthropic_endpoint({"provider": "claude-cli"})
        ok &= e2["mode"] == "cli-login"
        e3 = anthropic_endpoint({"provider": "deepseek", "api_key": "k",
                                 "model": "deepseek-v4-pro"})
        ok &= e3["mode"] == "bridge" and e3["base_url"].startswith("http")
        shutdown_bridge()
        print("brain selftest:", "ok" if ok else "FAILED")
        sys.exit(0 if ok else 1)
    print(json.dumps(status(), indent=1))
