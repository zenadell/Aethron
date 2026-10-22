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
import re
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _home() -> Path:
    """Where a person's settings live — NEVER inside the app.

    From source that is the repo, as it always was. Inside the packaged
    app ROOT is the app bundle itself, and CONFIG = ROOT / … put the
    owner's API keys INSIDE Aethron.app: every update and every reinstall
    replaced the bundle and silently wiped them (and writing into a signed
    bundle breaks its signature). That is the "is my key even saved?"
    confusion, and it wiped the keys once more the day it was found. The
    frozen app keeps them in AETHRON_HOME, which desktop.py sets before
    any role starts; the bundle's own copy is read only for defaults."""
    # an explicitly chosen data folder wins from source too — the studio's
    # projects already followed it while settings silently used the repo,
    # so one process read two different homes
    h = os.environ.get("AETHRON_HOME")
    if h:
        return Path(h)
    if not getattr(sys, "frozen", False):
        return ROOT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Aethron"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "Aethron"
    return Path(os.environ.get("XDG_DATA_HOME",
                               Path.home() / ".local" / "share")) / "Aethron"


HOME = _home()
CONFIG = HOME / "aethron_config.json"
BUNDLED = ROOT / "aethron_config.json"    # read-only defaults in the app
# Which keys are spent, and when that was last true. Kept OUT of the
# config so a shared or committed config never carries key material, and
# kept on disk because every forge subprocess is a fresh process — an
# in-memory ring would rediscover each exhausted key by burning a request
# on it, once per batch.
KEYSTATE = HOME / "aethron_keys.json"

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
           "budget_usd": 1.0,
           # THE SESSION LIMIT: how much may be spent in one usage window
           # (like a subscription's 5-hour window). 0 = unlimited. The unit
           # is dollars or requests — requests, because a free key costs
           # $0 and a dollar limit would never stop it.
           "session_limit": 5.0, "session_unit": "usd", "session_hours": 5.0,
           # OTHER PROVIDERS ONLY WHEN ASKED. Some jobs only Gemini or
           # OpenAI can do — making a picture, meaning-based brand search.
           # These used to take any saved Gemini/OpenAI key whatever
           # provider was selected, so a person "using DeepSeek" watched
           # their Gemini credit fall. Now they use the SELECTED provider
           # when it can, and another saved provider only when switched
           # on here; every request is listed in Settings either way.
           "helpers": {"pictures": False, "brand_search": False}}


# ────────────────────────── settings ─────────────────────────────────

# SAVED PROVIDERS. One record used to hold everything, so the key ring
# typed in for Gemini stayed in force after a switch to DeepSeek and was
# sent to DeepSeek — "it keeps using Gemini in the background". Each
# provider is now its own PROFILE carrying its own keys, model and
# address; exactly one is active, and switching changes which one, never
# what is stored in the others. Spend caps stay global: money is the
# person's, whichever provider spends it.
PROFILE_FIELDS = ("provider", "model", "base_url", "wire",
                  "api_key", "api_keys")


def _raw_ai() -> dict:
    """The stored "ai" record, app defaults first, then the person's own."""
    ai = {}
    for f in ([BUNDLED, CONFIG] if BUNDLED != CONFIG else [CONFIG]):
        try:
            ai.update(json.loads(f.read_text(encoding="utf-8"))
                      .get("ai") or {})
        except Exception:
            pass
    return ai


def _profiles(ai: dict) -> tuple:
    """-> (profiles, active id). A record from before profiles becomes ONE
    profile named after its provider, so nothing that worked stops."""
    profs = {k: dict(v) for k, v in (ai.get("profiles") or {}).items()
             if isinstance(v, dict)}
    if not profs and (ai.get("provider") or ai.get("api_key")
                      or ai.get("api_keys")):
        pid = ai.get("provider") or "default"
        profs[pid] = {f: ai[f] for f in PROFILE_FIELDS if f in ai}
    active = ai.get("active") or ""
    if active not in profs:
        active = next(iter(profs), "")
    return profs, active


def load() -> dict:
    """The one place settings live. Env wins (CI, power users), then the
    ACTIVE PROFILE, then the global settings, then the defaults."""
    cfg = dict(DEFAULT)
    ai = _raw_ai()
    profs, active = _profiles(ai)
    cfg.update({k: v for k, v in ai.items()
                if k not in ("profiles", "active") + PROFILE_FIELDS})
    if active:
        prof = profs[active]
        for f in PROFILE_FIELDS:                # a profile is the WHOLE
            cfg[f] = prof.get(f, DEFAULT.get(f, ""))   # provider record
    cfg["active"] = active
    env = os.environ
    for key, var in (("provider", "AETHRON_AI_PROVIDER"),
                     ("api_key", "AETHRON_AI_KEY"),
                     ("model", "AETHRON_AI_MODEL"),
                     ("base_url", "AETHRON_AI_BASE_URL")):
        if env.get(var):
            cfg[key] = env[var]
    return cfg


def _write(ai: dict) -> dict:
    try:
        whole = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        whole = {}
    whole["ai"] = ai
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(whole, indent=1), encoding="utf-8")
    return ai


def _stored() -> dict:
    """The person's own record with profiles made explicit (migrated)."""
    try:
        ai = dict(json.loads(CONFIG.read_text(encoding="utf-8")).get("ai")
                  or {})
    except Exception:
        ai = {}
    profs, active = _profiles({**_raw_ai(), **ai})
    for f in PROFILE_FIELDS:                    # they live in profiles now
        ai.pop(f, None)
    ai["profiles"], ai["active"] = profs, active
    return ai


def save(patch: dict) -> dict:
    """Save settings. Provider fields go to a PROFILE: the active one if it
    is the same provider (or `profile` names one), otherwise that
    provider's own saved profile — created if new — which also becomes
    active. Saving never overwrites a different provider's keys."""
    ai = _stored()
    profs = ai["profiles"]
    glob = {k: v for k, v in patch.items()
            if k in DEFAULT and k not in PROFILE_FIELDS}
    ai.update(glob)
    prof_patch = {k: v for k, v in patch.items() if k in PROFILE_FIELDS}
    if prof_patch:
        pid = str(patch.get("profile") or "")
        want = prof_patch.get("provider")
        if not pid:
            cur = profs.get(ai["active"], {})
            if not want or cur.get("provider") == want:
                pid = ai["active"]
            else:
                pid = next((k for k, v in profs.items()
                            if v.get("provider") == want), "")
        if not pid:
            pid = _new_id(profs, want or "custom")
        prof = profs.setdefault(pid, {})
        if want and prof.get("provider") not in (None, "", want):
            prof.clear()            # a profile id reused for a new provider
        prof.update(prof_patch)
        if patch.get("label"):
            prof["label"] = str(patch["label"])[:60]
        ai["active"] = pid
    res = _write(ai)
    try:
        import aethron_bridge
        cfg_all = load()
        aethron_bridge.set_limits(
            requests=cfg_all.get("budget_requests"),
            tokens=cfg_all.get("budget_tokens"),
            usd=cfg_all.get("budget_usd"))
    except Exception:
        pass
    return res


def _new_id(profs: dict, base: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "provider"
    pid, n = base, 2
    while pid in profs:
        pid, n = f"{base}-{n}", n + 1
    return pid


def profiles() -> list:
    """Every saved provider, for the switcher. Never carries a key."""
    ai = _stored()
    out = []
    for pid, p in ai["profiles"].items():
        meta = PROVIDERS.get(p.get("provider") or "", PROVIDERS["custom"])
        free = [k for k in (p.get("api_keys") or []) if str(k).strip()]
        r = resolve({**{f: p.get(f, DEFAULT.get(f, "")) for f in
                        PROFILE_FIELDS}})
        out.append({"id": pid, "label": p.get("label") or meta["label"],
                    "provider": p.get("provider", ""),
                    "model": p.get("model") or meta["model"],
                    "keys": len(free) + (1 if p.get("api_key") else 0),
                    "free_keys": len(free), "paid_key": bool(p.get("api_key")),
                    "active": pid == ai["active"],
                    "ready": r["ready"], "why": r["why"]})
    return out


def switch(pid: str) -> dict:
    """Make a saved provider the active one. One click, nothing else
    changes; the bridge started for the old one is stopped so no later
    session can reach it."""
    ai = _stored()
    if pid not in ai["profiles"]:
        raise ValueError(f"no saved provider called {pid!r}")
    ai["active"] = pid
    _write(ai)
    shutdown_bridge()
    return resolve()


def delete_profile(pid: str) -> dict:
    ai = _stored()
    if pid not in ai["profiles"]:
        raise ValueError(f"no saved provider called {pid!r}")
    del ai["profiles"][pid]
    if ai["active"] == pid:
        ai["active"] = next(iter(ai["profiles"]), "")
        shutdown_bridge()
    return _write(ai)


def profile_keys(provider: str) -> list:
    """Every key saved under ANY profile of this provider, free first.
    For capabilities only one provider has — Gemini's embeddings feed
    brand memory — which must keep working whichever provider is
    active for writing."""
    keys = []
    for p in _stored()["profiles"].values():
        if p.get("provider") == provider:
            for k in list(p.get("api_keys") or []) + [p.get("api_key")]:
                k = str(k or "").strip()
                if k and k not in keys:
                    keys.append(k)
    return keys


HELPER_PROVIDERS = {"pictures": ("openai", "gemini"),
                    "brand_search": ("gemini",)}


def helper_keys(kind: str, provider: str) -> list:
    """Keys a side job (`pictures`, `brand_search`) may use with `provider`.

    The SELECTED provider's own keys always; another saved provider's only
    when the person switched that job on. Never a key behind their back."""
    if provider not in HELPER_PROVIDERS.get(kind, ()):
        return []
    if resolve().get("provider") == provider:
        return profile_keys(provider)
    if (load().get("helpers") or {}).get(kind):
        return profile_keys(provider)
    return []


def helper_blocked(kind: str) -> list:
    """Saved providers that COULD do `kind` but are not allowed to — so a
    skipped job can say exactly what would turn it on."""
    out = []
    for prov in HELPER_PROVIDERS.get(kind, ()):
        if profile_keys(prov) and not helper_keys(kind, prov):
            out.append(PROVIDERS.get(prov, {}).get("label", prov))
    return out


def _fp(key: str) -> str:
    """A key's fingerprint. The state file records these, never the key."""
    import hashlib
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _today() -> str:
    """The day as the PROVIDER counts it, not as this machine does.

    Google's free allowance resets at midnight Pacific. Using the local
    date meant a key spent at 00:30 in Lagos (Google's day still running)
    was marked for the whole Lagos day and stayed benched straight through
    Google's reset at 08:00 — a full day of free requests thrown away.
    """
    import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=8)).date().isoformat()


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


def spent_for(model: str = "") -> set:
    """Fingerprints spent today FOR THIS MODEL. Google counts each model's
    free allowance separately — a key that has used gemini-3.8-flash's 20
    still has gemini-3.6-flash's and the embedding model's — so a key is
    benched per model. A bare fingerprint (older records) benches it for
    every model."""
    out = set()
    for f in _keystate()["exhausted"]:
        fp, _, m = f.partition(":")
        if not m or m == model:
            out.add(fp)
    return out


def mark_exhausted(key: str, model: str = "") -> None:
    st = _keystate()
    f = _fp(key) + (f":{model}" if model else "")
    if f not in st["exhausted"]:
        st["exhausted"].append(f)
        try:
            KEYSTATE.write_text(json.dumps(st, indent=1), encoding="utf-8")
        except Exception:
            pass


WALLET = HOME / "aethron_spend.json"   # real money, counted down across runs
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
    # UNLIMITED IS A CHOICE, NOT A MISSING VALUE. `or DEFAULT_WALLET` above
    # read a saved 0 as "unset", so the ceiling could never be switched off.
    unlimited = bool(st.get("unlimited")) and not os.environ.get("AETHRON_WALLET_USD")
    return {"limit_usd": limit, "spent_usd": spent,
            "left_usd": float("inf") if unlimited else max(0.0, limit - spent),
            "unlimited": unlimited,
            "calls": int(st.get("calls") or 0), "since": st.get("since") or _today()}


def wallet_spend(usd: float) -> dict:
    """Record money the provider actually charged. Called AFTER a paid reply, never before —
    an estimate must gate the call, but only the bill may reduce what is left."""
    w = wallet()
    st = {"limit_usd": w["limit_usd"], "spent_usd": w["spent_usd"] + max(0.0, float(usd)),
          "calls": w["calls"] + 1, "since": w["since"], "unlimited": w["unlimited"]}
    try:
        WALLET.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass
    session_add(usd, 1)          # the same bill counts toward the usage window
    return st


def wallet_set(limit_usd: float, keep_spent: bool = False) -> dict:
    """Set the ceiling. By default the count starts again from zero."""
    w = wallet()
    limit_usd = float(limit_usd)
    st = {"limit_usd": round(limit_usd, 4) if limit_usd > 0 else w["limit_usd"],
          "unlimited": limit_usd <= 0,
          "spent_usd": w["spent_usd"] if keep_spent else 0.0,
          "calls": w["calls"] if keep_spent else 0, "since": _today()}
    try:
        WALLET.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass
    return st


# ─────────────────────────── run limits ──────────────────────────────
# Three limits, each the person's to set or switch off:
#   run      the spend guard — what ONE request may cost (budget_usd)
#   session  what one usage WINDOW may cost; the window starts with the
#            first request after the last one ended and resets after
#            session_hours, like a subscription's session limit
#   wallet   a ceiling across everything, until it is reset
# 0 means unlimited for each. None of them can lift a provider's own
# limit: when the provider's credit is gone, it says no, and Aethron says
# so in the provider's words.
SESSION = HOME / "aethron_session.json"
_SESSION_LOCK = threading.Lock()


def _num(v, default=0.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if v == v and v >= 0 else default


def limits() -> dict:
    """The three limits as the person set them (0 = unlimited)."""
    cfg = load()
    w = wallet()
    unit = cfg.get("session_unit") if cfg.get("session_unit") in (
        "usd", "requests") else "usd"
    return {"run_usd": _num(cfg.get("budget_usd"), 1.0),
            "session_limit": _num(cfg.get("session_limit"), 5.0),
            "session_unit": unit,
            "session_hours": max(0.25, _num(cfg.get("session_hours"), 5.0)),
            "wallet_usd": 0.0 if w.get("unlimited") else w["limit_usd"]}


def set_limits(run_usd=None, session_limit=None, session_unit=None,
               session_hours=None, wallet_usd=None) -> dict:
    """Save any of the limits (0 = unlimited) and apply them at once to a
    running agent — a limit that waits for a restart is not a limit."""
    patch = {}
    if run_usd is not None:
        patch["budget_usd"] = _num(run_usd, 1.0)
    if session_limit is not None:
        patch["session_limit"] = _num(session_limit, 5.0)
    if session_unit in ("usd", "requests"):
        patch["session_unit"] = session_unit
    if session_hours is not None:
        patch["session_hours"] = max(0.25, min(24 * 7, _num(session_hours, 5.0)))
    if patch:
        ai = _stored()
        ai.update(patch)
        _write(ai)
    if wallet_usd is not None:
        wallet_set(_num(wallet_usd, DEFAULT_WALLET), keep_spent=True)
    lim = limits()
    br = sys.modules.get("aethron_bridge")
    if br is not None:
        br.set_limits(usd=lim["run_usd"])
    return lim


def _session_read() -> dict:
    try:
        return json.loads(SESSION.read_text(encoding="utf-8"))
    except Exception:
        return {}


def session(now: float = None) -> dict:
    """Where this usage window stands. A window that has ended counts as
    empty: the next request opens a new one."""
    import time as _t
    now = now or _t.time()
    lim = limits()
    hours = lim["session_hours"]
    st = _session_read()
    start = float(st.get("start") or 0)
    live = bool(start) and now < start + hours * 3600
    used_usd = float(st.get("usd") or 0) if live else 0.0
    used_req = int(st.get("requests") or 0) if live else 0
    limit, unit = lim["session_limit"], lim["session_unit"]
    used = used_usd if unit == "usd" else used_req
    return {"limit": limit, "unit": unit, "hours": hours,
            "unlimited": not limit, "started": start if live else 0,
            "resets_at": start + hours * 3600 if live else 0,
            "used_usd": used_usd, "used_requests": used_req,
            "left": None if not limit else max(0.0, limit - used),
            "reached": bool(limit) and used >= limit}


def session_add(usd: float = 0.0, requests: int = 0, now: float = None) -> dict:
    """Count spend into the current window, opening one if none is live."""
    import time as _t
    now = now or _t.time()
    with _SESSION_LOCK:
        hours = limits()["session_hours"]
        st = _session_read()
        start = float(st.get("start") or 0)
        if not start or now >= start + hours * 3600:
            st = {"start": now, "usd": 0.0, "requests": 0}
        st["usd"] = float(st.get("usd") or 0) + max(0.0, float(usd or 0))
        st["requests"] = int(st.get("requests") or 0) + int(requests or 0)
        try:
            SESSION.parent.mkdir(parents=True, exist_ok=True)
            SESSION.write_text(json.dumps(st), encoding="utf-8")
        except OSError:
            pass
    return session(now)


def _clock(t: float) -> str:
    import time as _t
    return _t.strftime("%H:%M", _t.localtime(t))


def session_check(now: float = None) -> str:
    """"" when a request may start, else the sentence that says why not."""
    import time as _t
    now = now or _t.time()
    s = session(now)
    if not s["reached"]:
        return ""
    left = max(0, int(s["resets_at"] - now))
    h, m = divmod(left // 60, 60)
    used = (f"${s['used_usd']:.2f} of ${s['limit']:.2f}" if s["unit"] == "usd"
            else f"{s['used_requests']} of {int(s['limit'])} requests")
    return (f"Aethron session limit: {used} used in this "
            f"{s['hours']:g}-hour window. It resets at {_clock(s['resets_at'])} "
            f"(in {h}h {m:02d}m). Raise it or set it to Unlimited in "
            f"Settings → Limits to keep going now.")


def session_reset() -> dict:
    with _SESSION_LOCK:
        try:
            SESSION.unlink()
        except OSError:
            pass
    return session()


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
    model = str({**load(), **(cfg or {})}.get("model") or "")
    spent = spent_for(model)
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

    model = resolve(cfg)["model"]
    spent = spent_for(model)
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
                mark_exhausted(key, model)
                frees.remove(key)
                print(f"  free key {_fp(key)} is out of quota for today — "
                      f"{len(frees)} free key(s) left", flush=True)
            elif verdict == "busy":
                # It accepted the connection and did not answer. Park it
                # so the next batch does not pay this same timeout again.
                mark_cold(key)
                frees.remove(key)

    # THE PAID KEY IS ALWAYS ASKED. Benching it "for today" made one
    # billing refusal (a project budget, a top-up still landing) stick
    # until midnight Pacific, so a key with credit answered "no API key
    # could serve this request" all day. A paid key costs one failed
    # request to re-ask; its refusal is reported, never remembered.
    if paid:
        if free_ring(cfg):
            print("  no free key answered in time — using the PAID key",
                  flush=True)
        out, verdict = attempt(paid, timeout)
        if verdict == "ok":
            return out
        last = out
    if isinstance(last, urllib.error.HTTPError):
        # SAY WHAT THE PROVIDER SAID. A bare "HTTP Error 400: Bad Request"
        # hides the one sentence that tells the person what to fix.
        raise ValueError(f"{resolve(cfg)['label']}: "
                         f"{_br._upstream_error(last)}") from last
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
        d = json.loads(resp.read())
    try:                         # where it really went, for the settings screen
        import aethron_usage
        import aethron_bridge as _br
        u = d.get("usage") or {}
        i = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        o = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        aethron_usage.record(r["base"], r["model"], "text", i, o,
                             _br.cost_of(r["model"], i, o, _br.cached_tokens(u)),
                             free=r["key"] in free_ring())
    except Exception:
        pass
    return pick(d)


# ─────────────────── an Anthropic endpoint, always ───────────────────
# The coding agent (Claude Code CLI) speaks only Anthropic. Rather than
# forcing a second account, we give it one: either the provider already
# speaks that wire, or the bridge translates. Same key, either way.

_BRIDGE = {"srv": None, "url": "", "sig": ""}
_LOCK = threading.Lock()


def _bridge_pool(cfg: dict, r: dict) -> dict:
    """The WHOLE ring for the coding agent, free keys first, paid last.

    MEASURED 2026-09-23: when the free keys moved into `api_keys`, the
    bridge kept receiving `resolve()["key"]` — ONE key. Its pool logic
    (rotate on busy, wait out a rate limit) was intact and had one key to
    work with. The first free key's daily allowance ran out mid-migration
    and the agent stalled while three keys with allowance sat unused.

    The bridge gets every key and asks the brain which are spent today, so
    a key benched in one session is benched in all of them, and comes back
    when the provider's day turns. A key pinned by the caller stays pinned.
    """
    pin = str((cfg or {}).get("_pin_key") or (cfg or {}).get("api_key")
              or "").strip()
    keys = [pin] if pin else (ring(cfg) or [r["key"]])
    free = free_ring(cfg)
    # Only FREE keys are benched across sessions (see text_call): a paid
    # key's billing refusal must not outlive the person fixing it.
    return {"api_key": keys, "free": free,
            "is_spent": lambda k: k in free and _fp(k) in spent_for(r["model"]),
            "on_spent": lambda k: k in free and mark_exhausted(k, r["model"])}


def bridge_config(cfg: dict = None):
    import aethron_bridge
    r = resolve(cfg)
    if not r["ready"]:
        raise ValueError(r["why"] or "AI settings incomplete")
    p = _bridge_pool(cfg, r)
    return aethron_bridge.BridgeConfig(r["base"], p.pop("api_key"),
                                       r["model"], **p)


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
    pool = _bridge_pool(cfg, r)
    # the signature names the whole ring, never the key that happens to be
    # live: a key running out must not restart the bridge under a session
    # that is still talking to it (a dead socket, no error, no timeout)
    sig = f"{r['base']}|{','.join(_fp(k) for k in pool['api_key'])}|{r['model']}"
    with _LOCK:
        cfg_all = {**load(), **(cfg or {})}
        aethron_bridge.set_limits(
            requests=cfg_all.get("budget_requests"),
            tokens=cfg_all.get("budget_tokens"),
            usd=cfg_all.get("budget_usd"))
        if _BRIDGE["sig"] != sig:
            if _BRIDGE["srv"]:
                _BRIDGE["srv"].shutdown()
            aethron_bridge.reset_usage()
            # what Gemini/DeepSeek demand back on every later turn must
            # outlive this process, or a resumed conversation is refused
            aethron_bridge.MEMORY_FILE = str(HOME / "bridge_memory.json")
            aethron_bridge.load_memory()
            srv, url = aethron_bridge.start(
                aethron_bridge.BridgeConfig(r["base"], pool.pop("api_key"),
                                            r["model"], **pool))
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
    return {"profiles": profiles(),
            "settings": {**load(), "api_key":
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
