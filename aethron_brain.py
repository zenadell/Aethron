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
# Which keys are spent, and when that was last true. Kept OUT of the
# config so a shared or committed config never carries key material, and
# kept on disk because every forge subprocess is a fresh process — an
# in-memory ring would rediscover each exhausted key by burning a request
# on it, once per batch.
KEYSTATE = ROOT / "aethron_keys.json"

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
           # A RING OF KEYS, CHEAPEST FIRST.
           #
           # Free-tier keys cost nothing and run out daily; a paid key
           # costs money and does not. Holding exactly one key forced the
           # choice up front and spent real credit on work a free key
           # would have done. The ring is tried IN ORDER, so put the free
           # keys first and the paid one last: the paid key is only
           # reached when every free allowance is genuinely gone.
           #
           # `api_key` still works and is treated as a one-key ring, so
           # nothing that already worked stops working.
           "api_keys": [],
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


def _fp(key: str) -> str:
    """A key's fingerprint. The state file records these, never the key."""
    import hashlib
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _keystate() -> dict:
    """Exhausted fingerprints, reset when the day turns.

    Free-tier allowances are DAILY. Latching a key as dead forever would
    throw away tomorrow's free requests and quietly move every future run
    onto the paid key — the exact cost the ring exists to avoid.
    """
    try:
        st = json.loads(KEYSTATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    if st.get("date") != _today():
        st = {"date": _today(), "exhausted": []}
    st.setdefault("exhausted", [])
    return st


# A HANGING KEY COSTS TIME, WHICH IS THE OTHER BUDGET.
#
# Measured on this account: free-tier Gemini stopped returning 503 and
# started ACCEPTING the connection and never answering — 3 of 4 free keys
# hung, one served in 14.7s, the paid key in 1.8s. With a 300s per-attempt
# timeout and a 4-key sweep, one batch of a rebrand sat blocked for 18
# minutes inside its FIRST sweep, and the worst case was ~100 minutes per
# batch across ~24 batches. The rotation was right; the clock was not.
ATTEMPT_TIMEOUT = float(os.environ.get("AETHRON_ATTEMPT_TIMEOUT", "45"))
FREE_BUDGET = float(os.environ.get("AETHRON_FREE_BUDGET", "120"))
COLD_SECONDS = float(os.environ.get("AETHRON_COLD_SECONDS", "600"))


def mark_cold(key: str) -> None:
    """This key did not answer. Skip it for a while.

    Distinct from exhausted: the allowance may be fine, the endpoint just
    is not serving. Without this, every one of a rebrand's ~24 batches
    re-discovers the same three dead keys and pays the timeout for each,
    turning a 3-minute job into an hour of waiting.
    """
    import time as _t
    st = _keystate()
    st.setdefault("cold", {})[_fp(key)] = _t.time()
    try:
        KEYSTATE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except Exception:
        pass


def _is_cold(key: str) -> bool:
    import time as _t
    return (_t.time() - (_keystate().get("cold", {}).get(_fp(key), 0))
            ) < COLD_SECONDS


def mark_exhausted(key: str) -> None:
    st = _keystate()
    f = _fp(key)
    if f not in st["exhausted"]:
        st["exhausted"].append(f)
        try:
            KEYSTATE.write_text(json.dumps(st, indent=1), encoding="utf-8")
        except Exception:
            pass


WALLET = ROOT / "aethron_spend.json"   # real money, counted down across runs
DEFAULT_WALLET = 1.00                  # what an unconfigured Aethron may spend, ever


def wallet(cfg: dict = None) -> dict:
    """What Aethron is still allowed to spend, in dollars.

    A per-run budget caps ONE run. It cannot stop twenty runs from emptying a card, which is
    how prepaid credit actually disappears: never in one call, always in a hundred reasonable
    ones. The ceiling lives here instead — counted down by REAL usage, written to disk, so it
    survives restarts and is enforced by the code rather than remembered by whoever is driving.
    """
    cfg = {**load(), **(cfg or {})}
    try:
        st = json.loads(WALLET.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    limit = (os.environ.get("AETHRON_WALLET_USD") or cfg.get("wallet_usd")
             or st.get("limit_usd") or DEFAULT_WALLET)
    # NOT ROUNDED. A rounded balance fed back into the next addition drifts away from the bill,
    # and money is the one number in this project that may not be approximated. Round to print.
    limit, spent = float(limit), float(st.get("spent_usd") or 0.0)
    return {"limit_usd": limit, "spent_usd": spent, "left_usd": max(0.0, limit - spent),
            "calls": int(st.get("calls") or 0), "since": st.get("since") or _today()}


def wallet_spend(usd: float) -> dict:
    """Record money the provider actually charged. Called AFTER a paid reply, never before —
    an estimate must gate the call, but only the bill may reduce what is left."""
    w = wallet()
    st = {"limit_usd": w["limit_usd"], "spent_usd": w["spent_usd"] + max(0.0, float(usd)),
          "calls": w["calls"] + 1, "since": w["since"]}
    try:
        WALLET.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass
    return st


def wallet_set(limit_usd: float, keep_spent: bool = False) -> dict:
    """Set the ceiling. By default the count starts again from zero."""
    w = wallet()
    st = {"limit_usd": round(float(limit_usd), 4),
          "spent_usd": w["spent_usd"] if keep_spent else 0.0,
          "calls": w["calls"] if keep_spent else 0, "since": _today()}
    try:
        WALLET.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass
    return st


def free_ring(cfg: dict = None) -> list:
    """The free keys, in order. `api_keys` is exactly this list."""
    cfg = {**load(), **(cfg or {})}
    return [str(k).strip() for k in (cfg.get("api_keys") or [])
            if str(k).strip()]


def paid_key(cfg: dict = None) -> str:
    """The last resort. `api_key` keeps its old meaning — the one key —
    which is why a config that predates the ring still works untouched."""
    cfg = {**load(), **(cfg or {})}
    return str(cfg.get("api_key") or "").strip()


def ring(cfg: dict = None) -> list:
    """Every key we may use, cheapest first."""
    keys = free_ring(cfg)
    p = paid_key(cfg)
    if p and p not in keys:
        keys.append(p)
    return keys


def live_keys(cfg: dict = None) -> list:
    """The ring minus whatever is spent today."""
    spent = set(_keystate()["exhausted"])
    return [k for k in ring(cfg) if _fp(k) not in spent]


def resolve(cfg: dict = None) -> dict:
    """Settings -> everything a caller needs, with an honest `ready`."""
    # PINNING GETS ITS OWN FIELD, and deliberately not `api_key`.
    # The paid key IS the config's `api_key`, so treating that field as
    # "the caller pinned a key" answers yes for any caller that passes
    # whole settings through — studio does — and pins the PAID key on
    # every call. The ring would exist and never once be used. Caught by
    # the battery, which passes a config-shaped cfg exactly as studio
    # does. Only `_pin_key` pins, and only text_call sets it.
    #
    # Note it reads the ARGUMENT, never the merged settings: the paid key
    # lives in the stored `api_key`, so merging first would make every
    # call look pinned and the ring would never be used.
    pin = str((cfg or {}).get("_pin_key")
              or (cfg or {}).get("api_key") or "").strip()
    cfg = {**load(), **(cfg or {})}
    if pin:
        cfg["api_key"] = pin
    else:
        avail, allk = live_keys(cfg), ring(cfg)
        # ALL SPENT IS NOT THE SAME AS NO KEY. Falling back to the last
        # key makes the failure the provider's real "quota exceeded"
        # message instead of a misleading "add an API key".
        cfg["api_key"] = avail[0] if avail else (allk[-1] if allk else "")
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

    Walks the key ring: a key whose free allowance is spent is marked and
    the next key takes over, so a run drains the free keys before it ever
    touches the paid one. A 429 that is merely "too many this minute" is
    waited out on the SAME key — rotating on that would burn the whole
    ring in seconds and land on the paid key for no reason.
    """
    import time
    import urllib.error
    import aethron_bridge as _br

    spent = set(_keystate()["exhausted"])
    frees = [k for k in free_ring(cfg)
             if _fp(k) not in spent and not _is_cold(k)]
    paid = paid_key(cfg)
    last = None
    free_deadline = time.time() + FREE_BUDGET

    def attempt(key, cap):
        """-> (answer, verdict). verdict: 'ok' | 'spent' | 'busy' | raise."""
        try:
            return _text_once(resolve({**(cfg or {}), "_pin_key": key}),
                              prompt, cap, max_tokens), "ok"
        except urllib.error.HTTPError as e:
            if e.code == 429 and _br._is_quota_wall(e):
                return e, "spent"
            # 429-as-pace and 5xx are both "not now", not "not ever".
            # FREE CAPACITY IS INTERMITTENT — measured on this account,
            # the same key answered, then 503'd, then answered again
            # within a minute. Treating a 503 as a dead key would walk
            # the whole free ring in one second and spend real credit on
            # a blip.
            if e.code == 429 or 500 <= e.code < 600:
                return e, "busy"
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return e, "busy"

    # SWEEP THE FREE RING UNDER A CLOCK. Free capacity is worth waiting a
    # little for and never worth waiting indefinitely for, so the whole
    # free phase shares one budget: when it runs out, the paid key takes
    # the request rather than the caller taking another twenty minutes.
    for delay in (0,) + _br.RATE_BACKOFF:
        if not frees or time.time() >= free_deadline:
            break
        if delay:
            time.sleep(min(delay, max(0, free_deadline - time.time())))
        for key in list(frees):
            cap = min(ATTEMPT_TIMEOUT, timeout,
                      max(1, free_deadline - time.time()))
            if time.time() >= free_deadline:
                break
            out, verdict = attempt(key, cap)
            if verdict == "ok":
                return out
            last = out
            if verdict == "spent":
                mark_exhausted(key)
                frees.remove(key)
                print(f"  free key {_fp(key)} is out of quota for today — "
                      f"{len(frees)} free key(s) left", flush=True)
            elif verdict == "busy":
                # It accepted the connection and did not answer. Park it
                # so the next batch does not pay this same timeout again.
                mark_cold(key)
                frees.remove(key)

    if paid and _fp(paid) not in _keystate()["exhausted"]:
        if free_ring(cfg):
            print("  no free key answered in time — using the PAID key",
                  flush=True)
        out, verdict = attempt(paid, timeout)
        if verdict == "ok":
            return out
        if verdict == "spent":
            mark_exhausted(paid)
        last = out
    if isinstance(last, BaseException):
        raise last
    raise ValueError("no API key could serve this request")


def _text_once(r, prompt, timeout, max_tokens):
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
    allk, live = ring(), live_keys()
    return {"settings": {**load(), "api_key":
                         ("set" if load().get("api_key") else ""),
                         "api_keys": f"{len(allk)} key(s) in the ring"},
            "keyring": {"total": len(allk), "live_today": len(live),
                        "spent_today": len(allk) - len(live),
                        "in_use": _fp(r["key"]) if r.get("key") else "",
                        "resets": "daily (free-tier allowances)"},
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
        # "no key" now has to be stated, not assumed: resolve falls back
        # to the configured ring, so a machine WITH keys would otherwise
        # make this assertion fail for the right reason.
        r2 = resolve({"provider": "deepseek", "api_key": "",
                      "api_keys": [], "model": "m"})
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
