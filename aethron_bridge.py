#!/usr/bin/env python3
"""Aethron Bridge — one API key drives everything.

THE PROBLEM IT SOLVES: Aethron's template side already speaks to any
model (DeepSeek, Gemini, OpenAI, Groq, OpenRouter, Ollama, Anthropic).
The coding side drives the Claude Code CLI, and that CLI speaks exactly
one dialect: the Anthropic Messages API. So without a translator the
owner would need TWO configurations — a DeepSeek key for copy and an
Anthropic account for code. That is not the product.

This is the translator, in one stdlib file:

    claude CLI ──Anthropic /v1/messages──> BRIDGE ──OpenAI /chat/completions──> DeepSeek
                                             │                                  Gemini
                <────── Anthropic SSE ───────┘                                  OpenAI
                                                                                Groq …

Set ONE key in Aethron's settings and it powers copy fill, plan
polish, design matching, self-heal AND the coding agent. Nothing is
vendored and no external proxy is required — this is ~500 lines we own,
zero dependencies, and it is what makes "any API key" true rather than
aspirational.

Run it standalone:
    python3 aethron_bridge.py --port 8790          (reads Aethron settings)
    python3 aethron_bridge.py --selftest           (no key needed)
"""
import hashlib
import json
import os
import re
import sys
import time
import uuid
import urllib.parse
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_MAX_TOKENS = 8192
# A CEILING, not a default. The Claude CLI asks for 64000 output tokens
# on every turn; providers RESERVE that against the account balance
# before generating a word, so a free-tier key is refused with
# "402: requires more credits, or fewer max_tokens" for an answer that
# would have cost a cent. The reservation is the problem, not the usage.
MAX_TOKENS_CAP = int(os.environ.get("AETHRON_MAX_TOKENS_CAP", "16000"))

# REASONING MODELS (DeepSeek v4-pro, and others that follow it) return a
# `reasoning_content` field and then REQUIRE it back on the next request:
# "The `reasoning_content` in the thinking mode must be passed back to
# the API." The Anthropic wire has no such field, and the CLI therefore
# cannot echo it — so the bridge remembers it. Keyed by the tool-call id
# the same assistant turn produced (unique, and it survives the round
# trip through the CLI untouched); falls back to a hash of the text.
_REASONING = {}
_REASONING_MAX = 200

# GEMINI 3.x does the same thing with a different name and a stricter
# rule: every functionCall it emits carries a `thought_signature`, and
# sending that call back WITHOUT the signature is a hard 400 — "Function
# call is missing a thought_signature in functionCall parts. This is
# required for tools to work correctly." So the agent died on its second
# tool call, every time, on every key. Same shape as the DeepSeek fix
# above: remember it against the call id, re-attach on the way out.
_SIGNATURES = {}


def _sig_of(tc: dict):
    """Where Gemini actually puts it: tool_calls[].extra_content.google
    .thought_signature — not a top-level field, which is why the first
    reading of the error found nothing to carry."""
    extra = tc.get("extra_content") or {}
    google = extra.get("google") or {}
    return (google.get("thought_signature")
            or tc.get("thought_signature")
            or (tc.get("function") or {}).get("thought_signature"))


# A CONVERSATION OUTLIVES THE PROCESS THAT STARTED IT. Both memories above
# lived only in RAM, so the moment the app restarted, a resumed
# conversation carried tool calls Gemini would refuse ("missing a
# thought_signature") and DeepSeek would refuse ("reasoning_content must be
# passed back"). The brain points this at a file in AETHRON_HOME; without
# it the bridge behaves exactly as before.
MEMORY_FILE = None
# Google's documented value for a function call whose signature is gone
# (a conversation begun before signatures were kept, or pruned from the
# cap). Measured 2026-09-23 on gemini-3.8-flash: accepted, answered 200.
GEMINI_NO_SIGNATURE = "skip_thought_signature_validator"


def load_memory():
    if not MEMORY_FILE:
        return
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return
    for k, v in (d.get("sig") or {}).items():
        _SIGNATURES.setdefault(k, v)
    for k, v in (d.get("reasoning") or {}).items():
        _REASONING.setdefault(k, v)


def _save_memory():
    if not MEMORY_FILE:
        return
    try:
        tmp = MEMORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sig": _SIGNATURES, "reasoning": _REASONING}, f)
        os.replace(tmp, MEMORY_FILE)
    except Exception:
        pass


def _remember_sig(call_id: str, sig: str):
    if not call_id or not sig:
        return
    if len(_SIGNATURES) > _REASONING_MAX:
        for k in list(_SIGNATURES)[:_REASONING_MAX // 2]:
            _SIGNATURES.pop(k, None)
    _SIGNATURES[call_id] = sig
    _save_memory()


def _remember(key: str, text: str):
    if not key or not text:
        return
    if len(_REASONING) > _REASONING_MAX:
        for k in list(_REASONING)[:_REASONING_MAX // 2]:
            _REASONING.pop(k, None)
    _REASONING[key] = text
    _save_memory()


def _recall(msg: dict):
    for tc in msg.get("tool_calls") or []:
        if tc.get("id") in _REASONING:
            return _REASONING[tc["id"]]
    body = msg.get("content")
    if isinstance(body, str):
        return _REASONING.get("txt:" + str(hash(body.strip()))[:24])
    return None



# ─────────────────────── THE SPEND GUARD ─────────────────────────────
# Every request to every provider passes through this file, so this is
# the one place that can make runaway spend impossible. It exists
# because it did not: an agent loop plus CLI retries burned real money
# in an afternoon while a hung run looked like it was "still working".
#
# Three independent stops, all cheap:
#   1. a request cap      — agents loop; loops are requests
#   2. a token cap        — one huge context can cost more than many
#                           small ones
#   3. a repeat detector  — the same request three times in a row is a
#                           stuck agent, not progress
# Exceeding any of them returns a plain Anthropic error, which the CLI
# surfaces and the session ends. Nothing silently keeps spending.

# USD per 1M tokens (input, output).
#
# CALIBRATED AGAINST A REAL BILL, 2026-09-03: a day of runs this table
# scored at ~$9.25 cost $5.80 on the account. So the estimate reads about
# 1.6x HIGH for gemini-3.7-flash.
#
# That direction is the safe one — the guard stops before the money is
# actually gone — but it must be stated, because a $1.00 cap really
# permits about $0.63 of spend, and anyone sizing a budget from these
# numbers is sizing it small. The figures below are deliberately left
# unchanged: an estimate known to be conservative is more useful than a
# guess re-tuned from one day's data.
PRICES = {
    # DeepSeek, peak rates per 1M tokens (checked on
    # api-docs.deepseek.com/quick_start/pricing 2026-10-20; off-peak is
    # half). "deepseek-flash" was MISSING, so it priced at the dearest
    # rate in this table ($2/$12) — the owner saw "$0.19 this session"
    # while DeepSeek billed a fraction of a cent. The legacy flash names
    # are served by the same model at the same price.
    "deepseek-flash": (0.30, 1.20), "deepseek-v4-flash": (0.30, 1.20),
    "deepseek-v4-flash-vision-exp": (0.30, 1.20),
    "deepseek-v4-pro": (1.32, 3.96),
    "deepseek-chat": (0.27, 1.10), "deepseek-reasoner": (0.55, 2.19),
    "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.0),
    "gpt-5": (1.25, 10.0), "gpt-5-mini": (0.25, 2.0),
    # checked on ai.google.dev/gemini-api/docs/pricing 2026-09-22:
    # 0.75/3.75 through 2026-12-31, then 1.50/7.50 — all three flashes
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75), "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.0),
    # OpenAI, list prices per 1M tokens (checked 2026-10-18). Before these
    # were here, gpt-4o-mini counted at the DEAREST rate above — ~20x its
    # real $0.15/$0.60 — so the $5 run cap and the $1 wallet tripped after
    # a few cents of real spend and the model "suddenly stopped working".
    "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.0),
    "gpt-4.1": (2.00, 8.00), "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40), "gpt-5-nano": (0.05, 0.40),
    "o4-mini": (1.10, 4.40), "o3": (2.00, 8.00), "o3-mini": (1.10, 4.40),
    "gpt-6": (1.25, 10.0), "gpt-6-luna": (1.25, 10.0),
    "gpt-6-mini": (0.25, 2.0), "gpt-6-nano": (0.05, 0.40),
}

# WHAT A REPEATED PROMPT COSTS. An agent sends its whole conversation on
# every turn, and providers that cache it bill the repeated part at a
# small fraction of the input price. DeepSeek reports it as
# prompt_cache_hit_tokens, OpenAI as prompt_tokens_details.cached_tokens.
# $/1M for those tokens; a model not listed gets no discount (high, never
# low).
CACHED_PRICES = {
    "deepseek-flash": 0.006, "deepseek-v4-flash": 0.006,
    "deepseek-v4-flash-vision-exp": 0.006, "deepseek-v4-pro": 0.044,
    "deepseek-chat": 0.07, "deepseek-reasoner": 0.14,
    "gpt-4o-mini": 0.075, "gpt-4o": 1.25, "gpt-4.1": 0.50,
    "gpt-4.1-mini": 0.10, "gpt-4.1-nano": 0.025, "gpt-5": 0.125,
    "gpt-5-mini": 0.025, "gpt-5-nano": 0.005,
    "gpt-6": 0.125, "gpt-6-luna": 0.125, "gpt-6-mini": 0.025,
    "gpt-6-nano": 0.005,
}


def cached_tokens(u: dict) -> int:
    """How many of a reply's prompt tokens the provider served from its
    cache, in either wire's spelling. 0 when it does not say."""
    u = u or {}
    n = u.get("prompt_cache_hit_tokens")
    if n is None:
        n = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if n is None:
        n = u.get("cache_read_input_tokens")
    try:
        return max(0, int(n or 0))
    except (TypeError, ValueError):
        return 0


def cost_of(model: str, inp: int, out: int, cached: int = 0) -> float:
    """Dollars for one reply: cached prompt tokens at the cache rate, the
    rest of the prompt at the input rate, the answer at the output rate."""
    pin, pout = price_of(model)
    m = (model or "").split("/")[-1].strip()
    pc = CACHED_PRICES.get(m, CACHED_PRICES.get(_SNAPSHOT.sub("", m), pin))
    cached = min(max(0, int(cached or 0)), max(0, int(inp or 0)))
    return ((inp - cached) * pin + cached * pc + out * pout) / 1_000_000


UNKNOWN_PRICE = max(PRICES.values(), key=lambda p: p[0] + p[1])

_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def price_of(model: str) -> tuple:
    """(in, out) $/1M for a model id. A provider prefix ("openai/gpt-4o")
    and a dated snapshot ("gpt-4o-mini-2024-07-18") price as the model they
    name; anything still unknown is priced at the dearest known rate, so a
    cap is never switched off by a name nobody listed."""
    m = (model or "").split("/")[-1].strip()
    return PRICES.get(m) or PRICES.get(_SNAPSHOT.sub("", m)) or UNKNOWN_PRICE


_OPENAI_REASONING = re.compile(r"^(gpt-([5-9]|\d{2,})\b|o\d\b)", re.I)


def openai_reasoning(model: str) -> bool:
    """True for OpenAI's reasoning models (gpt-5*, gpt-6*, o1/o3/o4*). They
    refuse `max_tokens` (it must be `max_completion_tokens`) and any
    temperature but the default, while gpt-4o/4.1 refuse `reasoning_effort`.
    Only a BARE id counts: "openai/gpt-5" goes through OpenRouter, which
    takes max_tokens."""
    m = (model or "").strip()
    return "/" not in m and bool(_OPENAI_REASONING.match(m))


_OPENAI_NO_TOOL_REASONING = re.compile(r"^(gpt-([6-9]|\d{2,})\b)", re.I)


def openai_tools_need_no_reasoning(model: str) -> bool:
    """gpt-6* (bare ids) accept function tools on /chat/completions only
    with reasoning_effort "none". gpt-5 does not accept "none", so it is
    left alone; any other model that asks gets it via adapt_params."""
    m = (model or "").strip()
    return "/" not in m and bool(_OPENAI_NO_TOOL_REASONING.match(m))


def adapt_params(req: dict, body: str) -> dict | None:
    """When a model refuses a parameter we sent (`max_tokens`,
    `max_completion_tokens`, `temperature`, `reasoning_effort`,
    `stream_options`), return a copy adjusted to what the error asked for,
    or None when nothing in the error is a parameter mismatch we can fix."""
    b = (body or "").lower()
    out = dict(req)
    changed = False
    if "max_completion_tokens" in b and "max_tokens" in out:
        out["max_completion_tokens"] = out.pop("max_tokens")
        out.pop("temperature", None)
        changed = True
    elif ("'max_completion_tokens'" in b or '"max_completion_tokens"' in b) \
            and "max_completion_tokens" in out:
        out["max_tokens"] = out.pop("max_completion_tokens")
        changed = True
    if "temperature" in b and "temperature" in out and (
            "unsupported" in b or "does not support" in b
            or "only the default" in b or "unrecognized" in b):
        out.pop("temperature", None)
        changed = True
    if "reasoning_effort to 'none'" in b or 'reasoning_effort to "none"' in b:
        # tools + reasoning on /chat/completions (gpt-6*): the model asks
        # for reasoning_effort "none" rather than for it to be dropped
        if out.get("reasoning_effort") != "none":
            out["reasoning_effort"] = "none"
            changed = True
    elif "reasoning_effort" in b and "reasoning_effort" in out:
        out.pop("reasoning_effort", None)
        changed = True
    if "stream_options" in b and "stream_options" in out:
        out.pop("stream_options", None)
        changed = True
    return out if changed else None

# MONEY IS THE LIMIT THAT MEANS SOMETHING.
#
# A token cap cannot be set once for every model: the same 2M tokens is
# $1.12 on one and $0.15 on another, and the guard stopped a working
# agent 40 tool calls into a real investigation for fifteen cents of
# actual spend. Cost is the thing being guarded, so cost is the thing to
# count. The token and request caps stay as backstops for a runaway that
# is somehow cheap, and every one is raisable for a run worth it.
LIMITS = {"requests": int(os.environ.get("AETHRON_MAX_REQUESTS", "600")),
          "tokens": int(os.environ.get("AETHRON_MAX_TOKENS_RUN", "20000000")),
          "usd": float(os.environ.get("AETHRON_MAX_USD", "5.0")),
          "repeats": 3}
# A HANGING KEY MUST LOSE ITS TURN BEFORE THE AGENT LOSES ITS SESSION.
#
# Rotation already treats any upstream exception as "try the next key", and
# a socket timeout is an exception — so this was believed handled. It was
# not, because of an ordering nobody checked: the upstream read waited 600s
# while the agent's idle guard stops a silent session at 420s. The session
# always died 180s BEFORE rotation could fire, so one bad key wasted the
# whole run and three good ones sat unused.
#
# Measured: of four Gemini keys, three answer in ~1s and one never answers
# at all. A key that ERRORS rotates fine; a key that HANGS looked exactly
# like a model with nothing to say ("the agent went silent for 420s"),
# which is why it read as a capability failure for two runs.
#
# The whole sweep must therefore fit inside the idle budget:
#     4 keys x 90s = 360s  <  420s idle guard
# Raise this only together with the idle limit in aethron_code, never alone.
PING_EVERY = 10.0          # seconds of downstream silence before a ping
UPSTREAM_TIMEOUT = float(os.environ.get("AETHRON_UPSTREAM_TIMEOUT", "90"))
# answers the engine asks for again by itself — a failure, not a refusal
RETRIED_CODES = (408, 500, 502, 503, 504, 529)

# How long to keep waiting out a rate limit, and how long to pause between
# passes over the key pool. A free-tier per-minute window needs more than
# one 60s wait to clear reliably; the total stays well under the agent's
# 420s idle limit so a wait never reads as a wedged session.
RATE_BACKOFF = (5, 15, 30, 60)
RATE_LIMIT_BUDGET = float(os.environ.get("AETHRON_RATE_BUDGET", "180"))

# TWO LATCHES, NOT ONE. Money, requests and tokens are spent by the RUN and
# must stay latched across sessions — that is the whole point of a cap. A
# repeat loop is a property of ONE stuck conversation, and the cure for it is
# a new conversation. Sharing a latch made the cure impossible: the first
# session to loop poisoned every session after it, which arrived pre-rejected
# and reported "ok=False tools=0 cost=$0.0000" — indistinguishable from a
# model that read the task and declined it. Measured on mondragon: attempt 2
# never made a single request.
USED = {"requests": 0, "input": 0, "output": 0, "usd": 0.0, "list_usd": 0.0,
        "last_hash": "", "repeats": 0, "stopped": "", "loop_stopped": ""}

# WHAT THE BRIDGE IS DOING, FOR THE SCREEN. A rate-limit wait, a busy
# provider and a model thinking all look the same from outside: nothing
# arrives. The bridge is the one place that knows which it is, so it says
# so. The studio shows this instead of a generic "waiting", and stops a run
# only when neither the agent nor the bridge has moved for its whole limit
# — before, the screen gave up at 2½ minutes while the bridge was still
# inside its own 3-minute retry budget, and called a busy provider a dead
# one.
STATUS = {"inflight": 0, "state": "idle", "detail": "", "since": 0.0,
          "fails": 0}
# A STALL IS RETRIED, NOT A VERDICT. When an answer does not come, the
# engine asks again on its own (it retries a failed or cut-off request with
# back-off). Between two tries the bridge has nothing in flight, and the
# screen used to read that gap as "nobody is doing anything" — measured
# from the last thing the agent said, which by then was 3 minutes ago — and
# stopped a run that was in the middle of recovering. Consecutive failures
# are counted here, and while there are any the bridge says "retrying"
# instead of "idle", so the gap reads as what it is.
_ST_LOCK = threading.Lock()
_TL = threading.local()        # per request: was it served by a free key?


def _status(state: str, detail: str = "") -> None:
    STATUS.update(state=state, detail=detail, since=time.time())


# WHERE THE TIME WENT. A long run with no error has three possible
# owners: the model taking long to answer, tools taking long to run, or
# Aethron waiting out limits and retries. The bridge times the first and
# the third; the session times the tools. Reset with each person's message.
TIMING = {"t0": 0.0, "model_s": 0.0, "answers": 0, "slowest_s": 0.0,
          "waited_s": 0.0, "failures": 0, "_busy_since": 0.0}


def _timing_reset():
    TIMING.update(t0=time.time(), model_s=0.0, answers=0, slowest_s=0.0,
                  waited_s=0.0, failures=0, _busy_since=0.0)


def timing() -> dict:
    t = {k: v for k, v in TIMING.items() if not k.startswith("_")}
    if TIMING["_busy_since"]:
        t["model_s"] += time.time() - TIMING["_busy_since"]
    t["model_s"] = round(t["model_s"], 1)
    t["waited_s"] = round(t["waited_s"], 1)
    t["slowest_s"] = round(t["slowest_s"], 1)
    return t


def _inflight(delta: int) -> None:
    with _ST_LOCK:
        before = STATUS["inflight"]
        STATUS["inflight"] = max(0, STATUS["inflight"] + delta)
        now = time.time()
        if not before and STATUS["inflight"]:
            TIMING["_busy_since"] = now
        elif before and not STATUS["inflight"] and TIMING["_busy_since"]:
            TIMING["model_s"] += now - TIMING["_busy_since"]
            TIMING["_busy_since"] = 0.0
        if not STATUS["inflight"]:
            n = STATUS["fails"]
            if n:
                _status("retrying", f"The provider did not answer "
                        f"({n} tr{'y' if n == 1 else 'ies'} so far). "
                        f"Aethron is asking again.")
            else:
                _status("idle")


def _failed() -> None:
    with _ST_LOCK:
        STATUS["fails"] += 1
        TIMING["failures"] += 1


def _answered() -> None:
    with _ST_LOCK:
        STATUS["fails"] = 0


def status() -> dict:
    s = dict(STATUS)
    s["for_s"] = round(time.time() - s["since"], 1) if s["since"] else 0.0
    return s


def set_limits(requests=None, tokens=None, repeats=None, usd=None):
    for k, v in (("requests", requests), ("tokens", tokens),
                 ("repeats", repeats)):
        if v:
            LIMITS[k] = int(v)
    # the dollar cap is a float and was silently dropped by the int loop
    # above — the parameter existed, the caller passed it, and the guard
    # went on counting tokens
    #
    # A FREE-TIER KEY HAS NO DOLLARS TO CAP, and `if usd:` made 0 mean
    # "unset" rather than "no limit", so there was no way to say so. The
    # cost is an ESTIMATE from the PRICES table; on a free-tier key the
    # real figure is always $0.00, and the guard stopped a working agent
    # 32 tool calls in, reporting "~$4.02 spent" that never existed.
    # Nothing about that run was expensive — it was free, and cut short.
    #
    # 0 or negative now means no dollar limit. That is safe because money
    # was never the only stop: the request cap, the token cap, the repeat
    # detector and the idle limit all still apply, and on a free tier the
    # binding constraint is requests-per-minute anyway — which is what
    # the key pool exists to spread.
    if usd is not None:
        try:
            _u = float(usd)
        except (TypeError, ValueError):
            _u = 0.0
        LIMITS["usd"] = _u if _u > 0 else float("inf")


def reset_usage():
    _timing_reset()
    USED.update(requests=0, input=0, output=0, usd=0.0, list_usd=0.0,
                last_hash="", repeats=0, stopped="", loop_stopped="")


def _retry_not_repeat():
    """An upstream failure means the next identical request is a RETRY.

    The repeat detector exists to catch an agent asking the same thing
    over and over because it is stuck. A client re-sending a request that
    never got an answer is the opposite: correct behaviour, and the only
    way to survive a transient error.

    Free-tier keys make this the common case, not the rare one. They are
    rate-limited per minute, so 429s are expected — that is exactly why
    the pool has four keys — and when every key is briefly exhausted the
    CLI retries. Measured: the very first call of a session went out three
    times, the detector latched, and the session died having made ZERO
    tool calls, reporting "the agent is looping" about a model that had
    not yet been given the chance to say anything.

    Forgetting the hash after a failure costs nothing: a genuinely stuck
    agent repeats requests that SUCCEED, and those still latch.
    """
    USED["last_hash"], USED["repeats"] = "", 0


def reset_loop_guard():
    """Start a fresh conversation with a fresh loop detector.

    Deliberately does NOT touch requests/tokens/usd: a run's budget must
    survive session boundaries or the cap means nothing. Call this when a
    new agent session begins, never per request.
    """
    USED.update(last_hash="", repeats=0, loop_stopped="")


def usage_report() -> dict:
    return {**USED, "limits": dict(LIMITS)}


def _account(model: str, inp: int, out: int, cached: int = 0):
    USED["input"] += inp
    USED["output"] += out
    # AN UNKNOWN MODEL IS PRICED HIGH, NEVER AT ZERO. A model missing from
    # PRICES used to add $0 per call, so choosing a newer model than this
    # table knew about switched the money cap off without a word — which
    # is exactly what switching the owner to gemini-3.8-flash would have
    # done. Counting an unknown model at the dearest known rate means the
    # cap trips early rather than never.
    cost = cost_of(model, inp, out, cached)
    # A FREE KEY CHARGES NOTHING. Its list price is kept apart so a report
    # can say what the work would have cost, and so the money cap is never
    # tripped by money that was never spent.
    if getattr(_TL, "free", False):
        USED["list_usd"] = USED.get("list_usd", 0.0) + cost
    else:
        USED["usd"] += cost
        _session_count(cost)
    # WHERE IT REALLY WENT. The address this handler sent to, not a label:
    # the owner could not tell which provider their agent was spending.
    base = getattr(_TL, "base", "")
    if base:
        try:
            import aethron_usage
            aethron_usage.record(base, model, "agent", inp, out, cost,
                                 free=getattr(_TL, "free", False))
        except Exception:
            pass


def _budget_check(body: dict) -> str:
    """-> "" when it may proceed, else the reason it must not."""
    if USED["stopped"]:
        return USED["stopped"]
    if USED["loop_stopped"]:
        return USED["loop_stopped"]
    # THE PERSON'S USAGE WINDOW, across every run (Settings → Limits).
    why = _session_why()
    if why:
        return why          # not latched: the window resets on its own
    # UNLIMITED MEANS UNLIMITED. With the spend guard switched off the
    # request and token caps — backstops FOR the money cap — go with it;
    # only the loop detector stays, because a stuck agent makes no
    # progress at any price.
    unlimited = LIMITS["usd"] == float("inf")
    if not unlimited and USED["requests"] >= LIMITS["requests"]:
        USED["stopped"] = (f"Aethron spend guard: {USED['requests']} requests "
                           f"is the limit for this run "
                           f"(${USED['usd']:.2f} spent, "
                           f"{USED['input'] + USED['output']:,} tokens). "
                           f"Set the spend guard to Unlimited in Settings → "
                           f"Limits to lift it.")
        return USED["stopped"]
    if USED["usd"] >= LIMITS["usd"]:
        USED["stopped"] = (f"Aethron spend guard: ${USED['usd']:.2f} spent "
                           f"on this request, which is the ${LIMITS['usd']:.2f} "
                           f"limit per run. Raise it or set it to Unlimited "
                           f"in Settings → Limits, then send “continue”.")
        return USED["stopped"]
    total = USED["input"] + USED["output"]
    if not unlimited and total >= LIMITS["tokens"]:
        USED["stopped"] = (f"Aethron spend guard: {total:,} tokens is the "
                           f"limit for this run (${USED['usd']:.2f}). Set the "
                           f"spend guard to Unlimited in Settings → Limits "
                           f"to lift it.")
        return USED["stopped"]
    # a stuck agent sends the same thing over and over. Only the agent's
    # own turns stream: the engine's side calls (a "current state" summary
    # it asks for while working, sent twice in a row with the same words)
    # are not the agent repeating itself, and counting them tripped this
    # guard on a healthy run.
    non_sys = [m for m in (body.get("messages") or [])
               if isinstance(m, dict) and m.get("role") != "system"]
    h = hashlib.sha256(
        json.dumps(non_sys, sort_keys=True).encode("utf-8", "replace")
    ).hexdigest()
    if not body.get("stream") or unlimited:
        pass                              # side call or unlimited: never trip repeat guard
    elif h == USED["last_hash"]:
        USED["repeats"] += 1
        if USED["repeats"] >= LIMITS["repeats"]:
            # SAY WHAT REPEATED. "The agent is looping" is a verdict about
            # the model, and this guard cannot support it on its own: the
            # CLI also makes auxiliary calls (titles, summaries) carrying a
            # short message list of their own, and three of those in a row
            # look identical to a detector hashing the last two messages.
            # Blaming the model for the harness's own traffic is exactly
            # the mistake the healer battery made with a turn counter.
            # Record the shape of what repeated so the reader can tell a
            # stuck agent from ordinary chatter.
            msgs = body.get("messages") or []
            last = msgs[-1] if msgs else {}
            content = last.get("content")
            if isinstance(content, list):
                kinds = [c.get("type", "?") for c in content
                         if isinstance(c, dict)]
                names = [c.get("name", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "tool_use"]
                shape = f"{'+'.join(kinds) or 'empty'}"
                if names:
                    shape += f" ({', '.join(n for n in names if n)})"
                preview = ""
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        preview = str(c.get("text", ""))[:70]
                        break
            else:
                shape = "text"
                preview = str(content or "")[:70]
            USED["loop_stopped"] = (
                f"Aethron spend guard: the same request {USED['repeats']} "
                f"times in a row. Stopped. Repeated payload: "
                f"{len(msgs)} message(s), last is {last.get('role','?')}/"
                f"{shape}"
                + (f" — {preview!r}" if preview else "")
                + ". A long conversation here means the AGENT is stuck; a "
                  "short one means it was the CLI's own auxiliary traffic.")
            return USED["loop_stopped"]
    else:
        USED["last_hash"], USED["repeats"] = h, 0
    USED["requests"] += 1
    _session_count(0.0, 1)
    return ""


def _control_why() -> str:
    """The Aethron team's kill switch and holds, checked on EVERY request,
    so an agent that is already running stops at its next model call."""
    try:
        import aethron_control
        return aethron_control.why("agent")
    except Exception:
        return ""


def _session_why() -> str:
    # only where the app's settings are loaded: a bare bridge (a test, a
    # script) has no person's window to keep
    b = sys.modules.get("aethron_brain")
    try:
        return b.session_check() if b else ""
    except Exception:
        return ""


def _session_count(usd: float, requests: int = 0) -> None:
    b = sys.modules.get("aethron_brain")
    try:
        if b:
            b.session_add(usd, requests)
    except Exception:
        pass

# ─────────────────── Anthropic  ->  OpenAI  (request) ────────────────


IDENTITY = """WHO YOU ARE. This overrides anything said above it.

You are Aethron, made by Jomiez. You speak as yourself, in the first
person: "I migrate templates", "I can rebuild that screenshot", never
"Aethron is..." or "Aethron does..." as though Aethron were someone else.

Everything before this point describes the machinery you run on. It is
not who you are, and you never mention it. Do not name any AI company,
model, assistant, CLI, bridge, MCP server, toolkit or "underlying model",
and do not say you "run on", are "powered by" or are "built on" anything.
There is no you separate from Aethron.

Asked who or what you are: "I'm Aethron, a design and migration platform
made by Jomiez." Then say what you do, in your own words: I migrate
Framer and Webflow templates into sites their owner fully owns, rebuild
a screenshot into real code, port a site to another framework, and
change a page in plain English while measuring that every change is
exactly what was asked for.

Asked which model or AI you use: say you use whichever AI provider they
chose in Settings, and that they can switch it there. Do not name it.

Asked your version or what is new: answer from the facts below. Never
offer to look through files or package.json to find out.

HOW YOU WRITE. Plain sentences and ordinary paragraphs. Do not answer a
simple question with a documentation page: no headings, no bullet lists,
no horizontal rules and no emoji unless the person asked for a
structured breakdown or the content is genuinely a list. "Who are you"
gets two or three sentences, not an outline.
"""

def _facts() -> str:
    """Version and what's new, read from the installed build."""
    try:
        import aethron_news
        return aethron_news.self_facts()
    except Exception:
        return ""


def _text_of(content) -> str:
    """Anthropic content is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def _normalize_tool_input(raw):
    """Return a canonical JSON object for one streamed tool call.

    Most OpenAI-compatible providers stream function arguments as one JSON
    object, in pieces.  Gemini's compatibility endpoint can also send an
    initial empty object and then the real object (``{}{...}``).  Passing
    those fragments straight through makes Claude Code concatenate them and
    reject the call before Aethron's MCP server ever sees it.

    An empty-object prefix is transport noise, not a request with two
    meanings, so it is safe to discard.  Any other concatenated or malformed
    JSON stays a hard failure: guessing tool input is worse than doing
    nothing.
    """
    text = str(raw or "").strip()
    if not text:
        return "{}", ""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        decoder, values, rest = json.JSONDecoder(), [], text
        try:
            while rest:
                value, end = decoder.raw_decode(rest)
                values.append(value)
                rest = rest[end:].lstrip()
        except (TypeError, ValueError):
            return "", "the provider streamed invalid tool JSON"
        if (not values or not isinstance(values[-1], dict)
                or any(value != {} for value in values[:-1])):
            return "", "the provider streamed conflicting tool JSON"
        value = values[-1]
    if not isinstance(value, dict):
        return "", "tool input must be a JSON object"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")), ""


# WHAT SHE HAS ALREADY SAID. Gemini's OpenAI-compatible endpoint accepts
# a system message and demonstrably will not let it override "who are
# you" — three rounds of stronger, later, more explicit system text all
# came back "I am Gemini 3.6 Flash, created by Google". So the identity
# stops being an instruction and becomes CONVERSATION: the first
# exchange of every request is Aethron already having answered, in her
# own voice. A model argues with a system prompt; it does not contradict
# its own last turn.
SEED_Q = "Before we start - who are you?"
SEED_A = (
    "I'm Aethron, a design and migration platform built by Jomiez.\n\n"
    "I migrate Framer and Webflow templates into sites you fully own, "
    "rebuild a screenshot into real code, port a site to another "
    "framework, and change a page from plain English - measuring every "
    "change to prove it is exactly what you asked for.\n\n"
    "I use whichever AI provider you choose in Settings, and you can "
    "switch it there at any time."
)


def to_openai(body: dict, model: str = "") -> dict:
    """Anthropic Messages request -> OpenAI chat/completions request.

    Handles the parts that actually matter for an agent: system prompt,
    multi-block messages, tool definitions, tool calls and tool results.
    Unknown fields are dropped rather than forwarded — a provider that
    rejects an unknown key would break the whole session."""
    msgs = []
    # WHO IT IS BELONGS AT THE SEAM EVERY REQUEST CROSSES. Putting the
    # identity only in the CLI's --append-system-prompt left three ways
    # to lose it: a session started before the app was updated keeps the
    # prompt it was spawned with, a caller that sets `append_system`
    # replaces it, and the internal runtime never went through the CLI
    # at all. The owner asked Aethron who it was and Gemini answered as
    # Gemini. Every model request — any runtime, any provider, any
    # session age — passes through here, so this is the one place the
    # answer cannot be missed. Prepended, so anything the caller sends
    # still wins on everything else.
    system = body.get("system")
    txt = _text_of(system) if system else ""
    # APPENDED, NOT PREPENDED — and that was the whole bug. The CLI sends
    # its own "you are <vendor>'s official CLI" block as the system
    # prompt, so an identity placed BEFORE it is simply overruled by the
    # later, more specific one: the owner asked twice more and got "an
    # AI assistant created by Anthropic" and then "Aethron is a platform
    # integrated into my environment". Last instruction wins, so ours
    # goes last and says explicitly that it overrides what precedes it.
    if IDENTITY.strip() not in txt:
        who = IDENTITY + "\n" + _facts()
        txt = (txt + "\n\n" + who) if txt else who
    msgs.append({"role": "system", "content": txt})

    # THE ROOT. She has already introduced herself, before anything the
    # caller sends — see SEED_A above for why this is conversation and
    # not another instruction.
    if not any(isinstance(x.get("content"), str) and x["content"] == SEED_A
               for x in (body.get("messages") or [])):
        msgs.append({"role": "user", "content": SEED_Q})
        msgs.append({"role": "assistant", "content": SEED_A})

    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        text_parts, tool_calls, tool_results, pictures = [], [], [], []
        for b in content or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                text_parts.append(b.get("text", ""))
            elif t == "tool_use":
                tool_calls.append({
                    "id": b.get("id", ""), "type": "function",
                    "function": {"name": b.get("name", ""),
                                 "arguments": json.dumps(b.get("input", {}))}})
            elif t == "tool_result":
                tool_results.append({
                    "role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                    "content": _tool_result_text(b.get("content"))})
                # A tool message carries text only, so a picture a tool
                # returned (Read on a .png) follows it as the model's input.
                pictures += [_image_part(x) for x in
                             (b.get("content") if isinstance(
                                 b.get("content"), list) else [])
                             if isinstance(x, dict) and x.get("type") == "image"]
            elif t == "image":
                # THE PICTURE IS THE MESSAGE. This used to be replaced by
                # "[image omitted]", so an attached screenshot never
                # reached any model behind the bridge.
                pictures.append(_image_part(b))
        pictures = [x for x in pictures if x]
        # tool results are their own OpenAI messages and must come first
        msgs.extend(tool_results)
        if role == "assistant" and tool_calls:
            m = {"role": "assistant",
                 "content": "\n".join(text_parts) or None,
                 "tool_calls": tool_calls}
            reasoning = _recall(m)
            if reasoning:
                m["reasoning_content"] = reasoning
            gemini = "gemini" in (model or body.get("model", "")).lower()
            for tc in tool_calls:
                sig = _SIGNATURES.get(tc.get("id")) or (
                    GEMINI_NO_SIGNATURE if gemini else None)
                if sig:
                    tc["extra_content"] = {"google":
                                           {"thought_signature": sig}}
            msgs.append(m)
        elif pictures and role == "user":
            msgs.append({"role": "user", "content": (
                [{"type": "text", "text": "\n".join(text_parts)}]
                if text_parts else []) + pictures})
            pictures = []
        elif text_parts or not tool_results:
            msgs.append({"role": role, "content": "\n".join(text_parts)})
        if pictures:
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": "The image the tool returned:"}]
                + pictures})

    out = {"model": model or body.get("model", ""), "messages": msgs,
           "max_tokens": min(body.get("max_tokens") or DEFAULT_MAX_TOKENS,
                             MAX_TOKENS_CAP),
           "stream": bool(body.get("stream"))}
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if openai_reasoning(out["model"]):
        out["max_completion_tokens"] = out.pop("max_tokens")
        out.pop("temperature", None)
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    tools = []
    for t in body.get("tools") or []:
        if not t.get("name"):
            continue          # server-side tool defs (web_search etc.)
        tools.append({"type": "function", "function": {
            "name": t["name"], "description": t.get("description", "")[:1024],
            "parameters": t.get("input_schema") or {"type": "object",
                                                    "properties": {}}}})
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice") or {}
        kind = tc.get("type")
        if kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": tc["name"]}}
    if tools and openai_tools_need_no_reasoning(out["model"]):
        # gpt-6* refuse function tools with any reasoning on
        # /chat/completions: "set reasoning_effort to 'none'"
        out["reasoning_effort"] = "none"
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    return out


def _image_part(b: dict):
    """An Anthropic image block as an OpenAI image_url part (data URL)."""
    src = b.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return {"type": "image_url", "image_url": {"url": (
            f"data:{src.get('media_type') or 'image/png'};base64,"
            f"{src['data']}")}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


NO_SIGHT = ("[An image was attached, but this model cannot see images. "
            "Its saved path is in the message; mcp__aethron__"
            "screenshot_to_page rebuilds it as a real page.]")


def strip_images(req: dict) -> dict:
    """The same request with every picture replaced by a note — for a
    model that refuses image input."""
    out = dict(req)
    msgs = []
    for m in req.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            txt = [x.get("text", "") for x in c if x.get("type") == "text"]
            if any(x.get("type") == "image_url" for x in c):
                txt.append(NO_SIGHT)
            m = dict(m, content="\n".join(t for t in txt if t))
        msgs.append(m)
    out["messages"] = msgs
    return out


def has_images(req: dict) -> bool:
    return any(isinstance(m.get("content"), list) and any(
        x.get("type") == "image_url" for x in m["content"])
        for m in req.get("messages", []))


# How providers word "this model takes no images".
NO_VISION_RE = re.compile(
    r"(?i)image|vision|multimodal|multi-modal|image_url|content type|"
    r"unknown variant|expected `?text|does not support")


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict))
    return json.dumps(content) if content is not None else ""


STOP_MAP = {"stop": "end_turn", "length": "max_tokens",
            "tool_calls": "tool_use", "function_call": "tool_use",
            "content_filter": "end_turn"}


# ─────────────────── OpenAI  ->  Anthropic  (response) ───────────────

class AnthropicStream:
    """Builds the Anthropic SSE event sequence from OpenAI stream deltas.

    Claude Code is strict about this shape: message_start, then one
    content_block_start/delta*/stop per block, then message_delta with
    the stop reason, then message_stop. Tool calls arrive as
    input_json_delta chunks — assembling them wrongly is the difference
    between a working agent and a silent one."""

    def __init__(self, model: str, emit):
        self.model = model
        self.emit = emit
        self.msg_id = f"msg_bridge_{uuid.uuid4().hex[:16]}"
        self.started = False
        self.index = -1
        self.open_kind = None      # 'text' | 'tool'
        self.tool_slots = {}       # openai index -> our block index
        self.stop = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def _ev(self, name, data):
        self.emit(name, data)

    def start(self):
        if self.started:
            return
        self.started = True
        self._ev("message_start", {"type": "message_start", "message": {
            "id": self.msg_id, "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": self.usage}})

    def _close_block(self):
        if self.open_kind is not None:
            self._ev("content_block_stop", {"type": "content_block_stop",
                                            "index": self.index})
            self.open_kind = None

    def text(self, chunk: str):
        if not chunk:
            return
        self.start()
        if self.open_kind != "text":
            self._close_block()
            self.index += 1
            self.open_kind = "text"
            self._ev("content_block_start", {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "text", "text": ""}})
        self._ev("content_block_delta", {
            "type": "content_block_delta", "index": self.index,
            "delta": {"type": "text_delta", "text": chunk}})

    def tool(self, slot: int, call_id: str, name: str, args_chunk: str):
        self.start()
        if slot not in self.tool_slots:
            self._close_block()
            self.index += 1
            self.tool_slots[slot] = self.index
            self.open_kind = "tool"
            self._ev("content_block_start", {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "tool_use",
                                  "id": call_id or f"toolu_{self.index}",
                                  "name": name or "tool", "input": {}}})
        if args_chunk:
            self._ev("content_block_delta", {
                "type": "content_block_delta",
                "index": self.tool_slots[slot],
                "delta": {"type": "input_json_delta",
                          "partial_json": args_chunk}})

    def finish(self, stop_reason=None, usage=None):
        self.start()
        self._close_block()
        if usage:
            self.usage.update(usage)
        self._ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason or self.stop,
                      "stop_sequence": None},
            "usage": {k: v for k, v in self.usage.items()
                      if k in ("input_tokens", "output_tokens",
                               "cache_read_input_tokens")}})
        self._ev("message_stop", {"type": "message_stop"})


def from_openai_message(data: dict, model: str) -> dict:
    """Non-streaming OpenAI response -> Anthropic message object."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    if msg.get("reasoning_content"):
        for tc in msg.get("tool_calls") or []:
            _remember(tc.get("id", ""), msg["reasoning_content"])
    for tc in msg.get("tool_calls") or []:
        sig = _sig_of(tc)
        if sig:
            _remember_sig(tc.get("id", ""), sig)
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        content.append({"type": "tool_use", "id": tc.get("id", ""),
                        "name": fn.get("name", ""), "input": args})
    u = data.get("usage") or {}
    _account(model or data.get("model", ""), u.get("prompt_tokens", 0),
             u.get("completion_tokens", 0), cached_tokens(u))
    raw_id = data.get("id") or ""
    msg_id = (raw_id if raw_id and raw_id not in ("msg_bridge", "cmpl")
              else f"msg_bridge_{uuid.uuid4().hex[:16]}")
    return {"id": msg_id, "type": "message",
            "role": "assistant", "model": model or data.get("model", ""),
            "content": content or [{"type": "text", "text": ""}],
            "stop_reason": STOP_MAP.get(choice.get("finish_reason"),
                                        "end_turn"),
            "stop_sequence": None,
            "usage": {"input_tokens": u.get("prompt_tokens", 0),
                      "output_tokens": u.get("completion_tokens", 0)}}


# ─────────────────────────── the server ──────────────────────────────

class BridgeConfig:
    """Where upstream lives. One place, so the studio, the CLI and the
    desktop app cannot drift apart."""

    def __init__(self, base_url, api_key, model="", token="aethron",
                 free=None, is_spent=None, on_spent=None):
        self.base_url = (base_url or "").rstrip("/")
        # A POOL, not a key. Providers refuse for reasons that have
        # nothing to do with the request — two of four Gemini keys
        # answered 503 "high demand" on the same prompt in the same
        # second — and a run should not die because one key was unlucky.
        # A plain string still works; it is a pool of one.
        if isinstance(api_key, (list, tuple)):
            self.keys = [k for k in api_key if k]
        else:
            self.keys = [k for k in str(api_key or "").split(",") if k.strip()]
        self.keys = [k.strip() for k in self.keys]
        self._k = 0
        self.model = model or ""
        self.token = token          # what the CLI must present to US
        # WHICH KEYS COST NOTHING, AND WHICH ARE DONE FOR THE DAY. The
        # brain owns the daily record (it survives restarts and turns over
        # when the provider's day does); without it the bridge keeps its
        # own in memory. A free key is accounted at $0.
        self.free = {str(k).strip() for k in (free or []) if k}
        self._is_spent = is_spent
        self._on_spent = on_spent
        self._spent_here = set()
        if self.keys and self.spent(self.api_key):
            self.rotate()

    @property
    def api_key(self):
        return self.keys[self._k] if self.keys else ""

    def spent(self, key) -> bool:
        if key in self._spent_here:
            return True
        try:
            return bool(self._is_spent and self._is_spent(key))
        except Exception:
            return False

    def live(self) -> list:
        return [k for k in self.keys if not self.spent(k)]

    def spend(self, key) -> None:
        """A key whose allowance is gone for today is never asked again."""
        self._spent_here.add(key)
        if self._on_spent:
            try:
                self._on_spent(key)
            except Exception:
                pass
        self.rotate()

    def position(self) -> int:
        return self._k + 1

    def rotate(self) -> bool:
        """-> True when another usable key is now current.

        A paid key is last in the pool and is reached only once every free
        key is spent for the day: a free key that is merely BUSY or pacing
        is worth coming back to, and rotating onto the paid key for a blip
        would spend money on a problem that clears in seconds."""
        n = len(self.keys)
        live = self.live()
        free_live = [k for k in live if k in self.free]
        pool = free_live or live
        for step in range(1, n + 1):
            j = (self._k + step) % n
            if self.keys[j] in pool:
                moved = j != self._k
                self._k = j
                return moved
        return False

    @property
    def endpoint(self):
        base = self.base_url
        if not base.endswith("/v1") and "/v1" not in base:
            base += "/v1"
        return base + "/chat/completions"


def _handler(cfg: BridgeConfig, log=None):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _say(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, msg):
            # Anthropic error shape: the CLI renders `message` verbatim,
            # so upstream failures reach the user instead of "unknown".
            self._say(code, {"type": "error",
                             "error": {"type": "api_error", "message": msg}})

        def _open_fit(self, req):
            """Ask; and when the conversation is too big for the model, ask
            once more with its oldest long parts cut down to fit."""
            try:
                return self._open_rotating(req)
            except urllib.error.HTTPError as e:
                over = too_long(_error_body(e), req) \
                    if e.code in (400, 413) else None
                small = trim_to_fit(req, *over) if over else None
                if not small:
                    raise
                _status("trimming", "The conversation is too big for the "
                                    "model; cutting its oldest long parts "
                                    "down and asking again.")
                req.clear(); req.update(small)
                return self._open_rotating(req)

        def _upstream_fail(self, e, req):
            over = too_long(_error_body(e), req) if e.code in (400, 413) \
                else None
            if over:
                # A CONVERSATION TOO BIG FOR THE MODEL IS TRIMMED, NOT
                # RETRIED. Each provider words it its own way ("maximum
                # context length is…", "input token count … exceeds…"); the
                # engine compacts and carries on only on its own wording,
                # and otherwise stopped the turn. The screen then
                # reconnected the same oversized conversation to the same
                # refusal.
                _status("trimming", "The conversation is too big for the "
                                    "model; trimming it and carrying on.")
                return self._say(400, {"type": "error", "error": {
                    "type": "invalid_request_error", "message":
                    f"prompt is too long: {over[0]} tokens > "
                    f"{over[1]} maximum"}})
            return self._error(e.code, _upstream_error(e))

        @property
        def route(self):
            # the CLI calls /v1/messages?beta=true — matching on the raw
            # path 404s it, and the CLI reports that as "the model may
            # not exist", which sends you hunting in entirely the wrong
            # place. Always compare the path WITHOUT the query.
            return urllib.parse.urlparse(self.path).path.rstrip("/")

        def do_GET(self):
            if self.route.startswith("/health"):
                return self._say(200, {"ok": True, "upstream": cfg.base_url,
                                       "model": cfg.model,
                                       "usage": usage_report()})
            self._error(404, "not found")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if self.route.endswith("count_tokens"):
                # rough but honest: 4 chars ≈ 1 token. Only used for UI
                # meters; never for correctness.
                return self._say(200, {"input_tokens": max(1, len(raw) // 4)})
            if not self.route.endswith("/v1/messages"):
                return self._error(404, f"unsupported path {self.route}")
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._error(400, "bad JSON")
            stop = _control_why() or _budget_check(body)
            if stop:
                # A GUARD'S NO MUST BE HEARD. It was a 429, and the agent
                # engine treats 429 as "busy, ask again" — measured: it
                # retried in silence for over eight minutes, the screen
                # called the model quiet, reconnected to the same tripped
                # guard and went quiet again ("try 2 of 2"). A refusal the
                # engine does not retry ends the turn with this sentence.
                _status("stopped", stop)
                return self._say(400, {"type": "error", "error": {
                    "type": "invalid_request_error", "message": stop}})
            req = to_openai(body, cfg.model)
            if log:
                log({"event": "request", "model": req["model"],
                     "messages": len(req["messages"]),
                     "tools": len(req.get("tools") or []),
                     "stream": req["stream"]})
            _TL.free = False
            _TL.base = cfg.base_url
            _inflight(+1)
            t_req = time.time()
            try:
                if req["stream"]:
                    return self._stream(req, body)
                return self._once(req)
            finally:
                took = time.time() - t_req
                TIMING["answers"] += 1
                TIMING["slowest_s"] = max(TIMING["slowest_s"], took)
                _inflight(-1)

        # -- upstream ------------------------------------------------
        def _open(self, req):
            headers = {"Content-Type": "application/json"}
            if cfg.api_key:
                headers["Authorization"] = "Bearer " + cfg.api_key
            r = urllib.request.Request(cfg.endpoint,
                                       json.dumps(req).encode(), headers)
            return urllib.request.urlopen(r, timeout=UPSTREAM_TIMEOUT)

        def _open_rotating(self, req, tries=None):
            """Try the pool, then WAIT and try the pool again.

            429 and 503 are the provider's problem, not the prompt's, so
            another key is a real answer to them. But rotation alone only
            answers "this key is exhausted" — it cannot answer "ALL of
            them are", which is the ordinary case on a free tier, where
            the limit is per MINUTE and the pool exists to widen a window
            that still closes.

            One pass with 0.6s pauses spent about 2.4 seconds before
            giving up on a limit that clears in 60. Measured: an agent got
            two real tool calls in and died on "upstream 429" with every
            key merely resting.

            So a rate limit now gets waited out, in passes, with a hard
            deadline well inside the agent's idle limit (420s) — long
            enough to outlast a per-minute window, short enough that a
            genuinely dead upstream still fails fast. Errors that are NOT
            rate limits keep the old single-pass behaviour: retrying those
            just wastes the same time twice.
            """
            import urllib.error
            deadline = time.time() + RATE_LIMIT_BUDGET
            last, waits = None, list(RATE_BACKOFF)
            model = req.get("model", "")
            while True:
                rate_limited, hint = False, 0
                pool = tries or max(1, len(cfg.live()))
                for _ in range(pool):
                    if not cfg.live():
                        break
                    key, n = cfg.api_key, cfg.position()
                    _status("asking", f"Asking {model} (key {n} of "
                                      f"{len(cfg.keys)}).")
                    try:
                        up = self._open(req)
                        _TL.free = key in cfg.free
                        return up
                    except urllib.error.HTTPError as e:
                        last = e
                        # A MODEL WITHOUT EYES. Refused for the picture, the
                        # same request is asked again with a note in its
                        # place, so the turn goes on and the agent can
                        # still rebuild the image by measuring it.
                        if e.code in (400, 404, 415, 422) and has_images(req) \
                                and NO_VISION_RE.search(_error_body(e)):
                            _status("asking", f"{model} cannot see images; "
                                              f"asking without them.")
                            blind = strip_images(req)
                            req.clear(); req.update(blind)
                            try:
                                up = self._open(req)
                                _TL.free = key in cfg.free
                                return up
                            except urllib.error.HTTPError as e2:
                                last = e = e2
                        if e.code in (400, 422):
                            fixed = adapt_params(req, _error_body(e))
                            if fixed:
                                req.clear(); req.update(fixed)
                                try:
                                    up = self._open(req)
                                    _TL.free = key in cfg.free
                                    return up
                                except urllib.error.HTTPError as e2:
                                    last = e = e2
                                    fixed2 = adapt_params(req, _error_body(e2)) \
                                        if e2.code in (400, 422) else None
                                    if fixed2:
                                        req.clear(); req.update(fixed2)
                                        try:
                                            up = self._open(req)
                                            _TL.free = key in cfg.free
                                            return up
                                        except urllib.error.HTTPError as e3:
                                            last = e = e3
                        if e.code not in (429, 500, 502, 503, 529):
                            raise
                        # A QUOTA WALL IS NOT A RATE LIMIT — and neither is
                        # it the end of the POOL. Google words a free key's
                        # spent DAY and its busy MINUTE identically; only
                        # the quota id tells them apart. A spent key is
                        # benched for the provider's day and the next key
                        # asked at once; a pacing key is rotated off and
                        # waited out. Raising on the first spent key killed
                        # the agent while three other keys had allowance.
                        if e.code == 429:
                            kind, secs = _quota_kind(e)
                            if kind == "spent":
                                cfg.spend(key)
                                left = len(cfg.live())
                                if not left:
                                    raise
                                _status("spent", f"Key {n} has used today's "
                                                 f"free allowance; {left} "
                                                 f"key(s) left.")
                                continue
                            hint = max(hint, secs)
                            _status("pacing", f"Key {n} hit the provider's "
                                              f"per-minute limit.")
                        else:
                            _status("busy", f"The provider is busy "
                                            f"({e.code}) on key {n}.")
                        rate_limited = True
                        cfg.rotate()
                        time.sleep(0.6)
                    except Exception as e:
                        last = e
                        _status("busy", f"Key {n} did not answer "
                                        f"({type(e).__name__}).")
                        cfg.rotate()
                # only a rate limit is worth waiting on, and only while
                # there is budget left to wait with
                if not (rate_limited and waits and time.time() < deadline):
                    break
                pause = min(max(waits.pop(0), hint),
                            max(0, deadline - time.time()))
                _status("waiting", f"Every key is busy or pacing; trying "
                                   f"again in {int(pause)}s.")
                time.sleep(pause)
                TIMING["waited_s"] += pause
            if last is None:
                raise NoKeysLeft()
            raise last

        def _once(self, req):
            try:
                with self._open_fit(req) as r:
                    _status("answering", "The model is working on its answer.")
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                _retry_not_repeat()
                if e.code in RETRIED_CODES:
                    _failed()
                return self._upstream_fail(e, req)
            except NoKeysLeft as e:
                _retry_not_repeat()
                return self._error(429, f"upstream 429: {e}")
            except Exception as e:
                _retry_not_repeat()
                _failed()
                return self._error(502, f"bridge could not reach "
                                        f"{cfg.base_url}: {e}")
            _answered()
            self._say(200, from_openai_message(data, cfg.model))

        def _stream(self, req, original):
            try:
                up = self._open_fit(req)
            except urllib.error.HTTPError as e:
                _retry_not_repeat()
                if e.code in RETRIED_CODES:
                    _failed()
                return self._upstream_fail(e, req)
            except NoKeysLeft as e:
                _retry_not_repeat()
                return self._error(429, f"upstream 429: {e}")
            except Exception as e:
                _retry_not_repeat()
                _failed()
                return self._error(502, f"bridge could not reach "
                                        f"{cfg.base_url}: {e}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            _status("answering", "The model is working on its answer.")

            gone = []

            def emit(name, data):
                """A disconnected client is routine — the CLI hangs up when
                it aborts a turn. Writing on regardless dumps a stack per
                event and kills the handler thread mid-accounting."""
                if gone:
                    return
                try:
                    self.wfile.write(f"event: {name}\ndata: "
                                     f"{json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    gone.append(True)

            out = AnthropicStream(cfg.model or req.get("model", ""), emit)
            # A THINKING MODEL SAYS NOTHING FOR MINUTES. DeepSeek's flash
            # thinks by default, the reasoning is not forwarded and tool
            # arguments are held until they are whole, so the engine could
            # receive no byte at all until the end of a long answer. Open
            # the message at once and send the protocol's own ping while
            # the provider is working, so nothing downstream mistakes a
            # working model for a dead connection.
            out.start()
            beat = [time.time()]

            def alive():
                if time.time() - beat[0] >= PING_EVERY:
                    beat[0] = time.time()
                    emit("ping", {"type": "ping"})
            stop, usage = None, {}
            # Do not forward tool-argument fragments as they arrive. Some
            # OpenAI-compatible endpoints emit a redundant ``{}`` before the
            # actual argument object; Claude Code then sees ``{}{...}`` and
            # refuses the call before the MCP server runs. Buffering also lets
            # us associate chunks that omit their index with the right call.
            pending_tools, tool_order, call_slots = {}, [], {}
            reasoning, call_ids, text_seen = [], [], []
            sigs = {}          # gemini: per-call thought_signature

            def remember_tool(tc):
                fn = tc.get("function") or {}
                call_id = str(tc.get("id") or "")
                index = tc.get("index")
                # AN ID OUTRANKS AN INDEX. Gemini's compatibility endpoint
                # streams PARALLEL calls that all say index 0, each with its
                # own id and name. Keyed by index they collapsed into one
                # call: list_projects `{}` and WebFetch `{"url":..}` became
                # list_projects `{}{"url":..}`, which the CLI refused, the
                # model retried, and the owner watched fourteen identical
                # failures. A new id at an index already holding a
                # different id is a NEW call; only id-less continuation
                # chunks fall back to the index.
                idx_slot = ("index", str(index)) if index is not None else None
                held = pending_tools.get(idx_slot, {}).get("id") if idx_slot else ""
                if call_id and call_id in call_slots:
                    slot = call_slots[call_id]
                elif call_id and held and held != call_id:
                    slot = ("id", call_id)
                elif idx_slot is not None:
                    slot = idx_slot
                elif call_id:
                    slot = ("id", call_id)
                elif fn.get("name"):
                    slot = ("anonymous", len(tool_order))
                elif tool_order:
                    # A few compatibility layers omit index/id on later
                    # argument chunks. They can only belong to the call that
                    # was just opened; anything more ambiguous is rejected
                    # below as invalid JSON rather than guessed.
                    slot = tool_order[-1]
                else:
                    slot = ("anonymous", 0)
                if slot not in pending_tools:
                    pending_tools[slot] = {"id": "", "name": "",
                                           "arguments": []}
                    tool_order.append(slot)
                call = pending_tools[slot]
                if call_id:
                    call["id"] = call_id
                    call_slots[call_id] = slot
                if fn.get("name"):
                    call["name"] = str(fn["name"])
                if "arguments" in fn and fn.get("arguments") is not None:
                    args = fn["arguments"]
                    if isinstance(args, (dict, list)):
                        args = json.dumps(args, ensure_ascii=False)
                    elif not isinstance(args, str):
                        args = str(args)
                    call["arguments"].append(args)

            try:
                for line in up:
                    line = line.decode("utf-8", "replace").strip()
                    if line.startswith(":"):
                        # A QUEUED REQUEST IS NOT A DEAD ONE. DeepSeek, under
                        # load, holds a stream open with ": keep-alive"
                        # comments before the first word. Say so, so the
                        # screen waits instead of calling the model quiet.
                        _status("queued", "The provider has queued the "
                                          "request and is keeping it open.")
                        alive()
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        d = json.loads(payload)
                    except ValueError:
                        continue
                    # every chunk is proof of life; the screen measures
                    # silence from here, not from the start of the request
                    _status("writing", "The model is writing its answer.")
                    alive()
                    if d.get("usage"):
                        u = d["usage"]
                        usage = {"input_tokens": u.get("prompt_tokens", 0),
                                 "output_tokens": u.get("completion_tokens", 0)}
                        _account(cfg.model, usage["input_tokens"],
                                 usage["output_tokens"], cached_tokens(u))
                    for ch in d.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                        for tc in (delta.get("tool_calls") or []):
                            sg = _sig_of(tc)
                            if sg and tc.get("id"):
                                sigs[tc["id"]] = sg
                        if delta.get("content"):
                            text_seen.append(delta["content"])
                            out.text(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            if tc.get("id"):
                                call_ids.append(tc["id"])
                            remember_tool(tc)
                        if ch.get("finish_reason"):
                            stop = STOP_MAP.get(ch["finish_reason"],
                                                "end_turn")
            except Exception as e:                       # upstream died
                # A STREAM THAT STALLS MID-ANSWER IS CUT, NOT FINISHED.
                # Finishing it handed the engine half an answer plus
                # "[bridge: upstream stream failed]" as if the model had
                # meant it: the turn ended there and the whole run with
                # it. A response that simply stops before message_stop is
                # what the engine retries — measured: it asks again at
                # once and carries on with the full answer. So the
                # connection is closed with nothing more written; the
                # half-built tool calls are dropped, never run.
                _retry_not_repeat()
                _failed()
                _status("busy", f"The answer stopped arriving "
                                f"({type(e).__name__}); asking again.")
                gone.append(True)
                try:
                    up.close()
                except Exception:
                    pass
                self.close_connection = True
                return
            delivered, bad_tools = 0, []
            for slot in tool_order:
                call = pending_tools[slot]
                args, error = _normalize_tool_input(
                    "".join(call["arguments"]))
                if not call["name"]:
                    error = error or "the provider omitted a tool name"
                if error:
                    bad_tools.append(error)
                    continue
                out.tool(delivered, call["id"], call["name"], args)
                delivered += 1
            if bad_tools and not delivered:
                # A malformed call must be terminal. Giving it to the
                # CLI produces an error result that small models often
                # retry forever, which looks like an access prompt while
                # nothing has actually run.
                out.text("Aethron stopped before running a malformed "
                         "model request. Nothing was changed; please "
                         "send the request again.")
                stop = "end_turn"
            for cid, sg in (sigs or {}).items():
                _remember_sig(cid, sg)
            blob = "".join(reasoning)
            if blob:
                for cid in call_ids:
                    _remember(cid, blob)
                if not call_ids:
                    _remember("txt:" + str(hash("".join(text_seen)
                                               .strip()))[:24], blob)
            try:
                out.finish(stop, usage)
            except Exception:
                pass
            _answered()
            up.close()

    return H


def _error_body(e) -> str:
    """Read an HTTPError body ONCE and remember it.

    e.read() is a stream: the second reader gets an empty string. The
    quota check and the message formatter both want it, and whichever ran
    second used to silently report nothing.
    """
    if not hasattr(e, "_aethron_body"):
        try:
            e._aethron_body = e.read().decode("utf8", "replace")
        except Exception:
            e._aethron_body = ""
    return e._aethron_body


# A plan quota that is spent says so in words, not in the status code.
QUOTA_WALL_RE = re.compile(
    r"(?i)exceeded your current quota|billing details|quota_?exceeded"
    r"|insufficient[_ ]quota|out of credit|exceeded your monthly")


class NoKeysLeft(Exception):
    """Every key in the pool has used the provider's allowance for today."""

    def __str__(self):
        return ("every API key has used today's allowance for this model. "
                "Free allowances reset at midnight Pacific; switch to another "
                "model in Settings (each model has its own allowance) or add "
                "a key.")


def _quota_kind(e) -> tuple:
    """-> ('spent', 0) when the key is done for the provider's day, or
    ('pace', seconds) when it clears soon. Google sends the SAME words for
    both ("You exceeded your current quota"); only the quota id differs —
    measured 2026-09-23: GenerateRequestsPerDayPerProjectPerModel-FreeTier,
    quotaValue 20, alongside a misleading "Please retry in 17s"."""
    body = _error_body(e)
    if "PerMinute" in body:
        m = re.search(r'"retryDelay":\s*"(\d+)', body)
        return "pace", min(60, (int(m.group(1)) + 1) if m else 20)
    if "PerDay" in body or QUOTA_WALL_RE.search(body):
        return "spent", 0
    return "pace", 15


def _is_quota_wall(e) -> bool:
    """True when waiting cannot help: the plan's allowance is gone.

    Free-tier keys return 429 for two unrelated conditions — "too many
    requests this minute", which clears in under a minute, and "your
    quota is spent", which does not clear today. Treating the second as
    the first made the bridge wait out its whole budget and the agent die
    of silence, with the real reason sitting in the response body all
    along: "You exceeded your current quota, please check your plan and
    billing details."

    A PER-MINUTE limit is never a wall, whatever the words say: Google
    uses the same sentence for it, and calling it "spent for today" sent
    people to top up a plan that only needed a minute's rest.
    """
    body = _error_body(e)
    return bool(QUOTA_WALL_RE.search(body)) and "PerMinute" not in body


# How providers word "the conversation is longer than this model takes".
TOO_LONG_RES = (
    # DeepSeek, OpenAI and most OpenAI-compatible servers
    (re.compile(r"(?is)maximum context length is (\d+) tokens.*?"
                r"(?:requested|resulted in|have) (\d+)"), "limit_first"),
    # Gemini
    (re.compile(r"(?is)input token count \(?(\d+)\)? exceeds the maximum"
                r"[^0-9]*(\d+)"), "actual_first"),
)
TOO_LONG_WORDS = re.compile(
    r"(?i)context_length_exceeded|context length|context window|"
    r"too many tokens|prompt is too long|input is too long")


def too_long(body: str, req: dict = None):
    """(tokens asked, tokens allowed) when a refusal says the conversation
    is too big for the model, else None."""
    b = body or ""
    for rx, order in TOO_LONG_RES:
        m = rx.search(b)
        if m:
            a, c = int(m.group(1)), int(m.group(2))
            return (c, a) if order == "limit_first" else (a, c)
    if TOO_LONG_WORDS.search(b):
        n = len(json.dumps(req or {})) // 4 + 1
        return (n, max(1, int(n * 0.9)))
    return None


TRIM_KEEP_LAST = 6       # the latest messages are cut only as a last resort
TRIM_MIN = 1500          # a part shorter than this is left whole
TRIM_HEAD = 600          # what a cut part keeps of its start


def trim_to_fit(req: dict, asked: int, allowed: int):
    """The same request with its oldest long parts cut down until it fits
    the model, or None when it cannot be made to fit.

    Only CONTENT is shortened — no message is dropped — so every tool call
    still has its result and the provider accepts the order. The newest
    messages are left whole: they are what the model is working on."""
    msgs = [dict(m) for m in req.get("messages") or []]
    # the whole request counts (tool definitions are tokens too), so the
    # rate is measured on all of it and only the messages are shortened
    fixed = len(json.dumps(dict(req, messages=[])))
    size = fixed + len(json.dumps(msgs))
    if not msgs or asked <= 0:
        return None
    per_char = asked / max(1, size)
    reserve = int(req.get("max_tokens") or req.get("max_completion_tokens")
                  or 0)
    target = int((allowed - reserve) * 0.8 / per_char)
    if target <= 0:
        return None

    def cut(text: str) -> str:
        return (text[:TRIM_HEAD] + f"\n[… {len(text) - TRIM_HEAD} characters"
                f" of earlier content cut by Aethron to fit the model]")

    # oldest first; only when that is not enough, newer parts too — but
    # never the last two, the request the model is answering
    order = list(range(max(0, len(msgs) - TRIM_KEEP_LAST)))
    order += [i for i in range(max(0, len(msgs) - 2)) if i not in order]
    for i in order:
        if size <= target:
            break
        m = msgs[i]
        if m.get("role") == "system":
            continue
        c = m.get("content")
        if isinstance(c, str) and len(c) > TRIM_MIN:
            m["content"] = cut(c)
        elif isinstance(c, list):
            parts = []
            for x in c:
                if x.get("type") == "text" and len(x.get("text", "")) > TRIM_MIN:
                    x = dict(x, text=cut(x["text"]))
                elif x.get("type") == "image_url":
                    x = {"type": "text", "text": "[an earlier picture, cut "
                                                 "by Aethron to fit the model]"}
                parts.append(x)
            m["content"] = parts
        size = fixed + len(json.dumps(msgs))
    if size > target:
        return None
    return dict(req, messages=msgs)


def _upstream_error(e) -> str:
    body = _error_body(e)
    try:
        detail = json.loads(body)
        msg = (detail.get("error") or {}).get("message") or json.dumps(detail)
    except Exception:
        msg = body or (e.reason if hasattr(e, "reason") else str(e))
    hint = _hint(e.code, body)
    if _is_quota_wall(e):
        return (f"upstream {e.code}: the API key's quota is SPENT, not "
                f"rate-limited — waiting will not help. {hint or 'Use another provider/key or top up the plan.'}"
                f" ({str(msg)[:220]})")
    return f"upstream {e.code}: {str(msg)[:400]}" + (f" — {hint}" if hint else "")


def _hint(code, body: str) -> str:
    """What the PERSON should check, for the errors whose provider message
    does not say it. OpenAI answers `insufficient_quota` both when the
    balance is empty AND when a project's monthly budget is reached with
    credit still on the account — the second is the one people miss."""
    b = body or ""
    if "insufficient_quota" in b:
        return ("OpenAI refuses this key for billing: check the balance at "
                "platform.openai.com/settings/organization/billing, that credit "
                "has not EXPIRED (prepaid credit expires after a year), and the "
                "key's project budget under Projects → Limits.")
    if code == 401:
        return ("the key was refused — it is wrong, revoked or deleted; "
                "paste a fresh one in Settings → Model and key.")
    if code == 404 and "model" in b.lower():
        return "this key cannot use that model; pick another in Settings → Model and key."
    if code == 400 and ("Unsupported parameter" in b or "Unrecognized request argument" in b
                        or "not supported with this model" in b):
        return ("Aethron sent a setting this model does not accept — "
                "please report it with the model name.")
    return ""


def start(cfg: BridgeConfig, port=0, log=None):
    """-> (server, url). Serve it in a thread; stop with server.shutdown()."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), _handler(cfg, log))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ─────────────────────────── selftest ────────────────────────────────

def _mock_openai(script):
    """A minimal OpenAI-compatible upstream: [{'text':…}|{'tool':name,
    'input':{…}}] per turn, chosen by whether tool results came back."""
    turns = list(script)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            seen = sum(1 for m in body.get("messages", [])
                       if m.get("role") == "tool")
            blocks = turns[min(seen, len(turns) - 1)]
            if not body.get("tools"):
                blocks = [{"text": "ok"}]
            chunks = []
            for i, b in enumerate(blocks):
                if "tool" in b:
                    pieces = b.get("argument_chunks")
                    if pieces is None:
                        pieces = [json.dumps(b.get("input", {}))]
                    elif isinstance(pieces, str):
                        pieces = [pieces]
                    chunks.append({"choices": [{"index": 0, "delta": {
                        "tool_calls": [{"index": i, "id": f"call_{i}",
                                        "type": "function",
                                        "function": {"name": b["tool"],
                                                     "arguments": ""}}]}}]})
                    for piece in pieces:
                        chunks.append({"choices": [{"index": 0, "delta": {
                            "tool_calls": [{"index": i, "function": {
                                "arguments": str(piece)}}]}}]})
                else:
                    for piece in _split(b.get("text", "")):
                        chunks.append({"choices": [{"index": 0,
                                                    "delta": {"content": piece}}]})
            fin = "tool_calls" if any("tool" in b for b in blocks) else "stop"
            if not body.get("stream"):
                msg = {"role": "assistant",
                       "content": "".join(b.get("text", "") for b in blocks),
                       "tool_calls": [
                           {"id": f"call_{i}", "type": "function",
                            "function": {"name": b["tool"],
                                         "arguments": json.dumps(
                                             b.get("input", {}))}}
                           for i, b in enumerate(blocks) if "tool" in b]}
                out = json.dumps({"id": "cmpl", "choices": [
                    {"index": 0, "message": msg, "finish_reason": fin}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                return self.wfile.write(out)
            chunks.append({"choices": [{"index": 0, "delta": {},
                                        "finish_reason": fin}],
                           "usage": {"prompt_tokens": 10,
                                     "completion_tokens": 5}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _split(text, n=12):
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def selftest() -> int:
    checks = []

    def ck(name, ok, detail=""):
        checks.append((name, ok))
        print(("  ok   " if ok else "  FAIL ") + name
              + (f"   {detail}" if not ok and detail else ""))

    # 1. request translation (the part that silently breaks agents)
    req = to_openai({
        "system": [{"type": "text", "text": "be brief"}],
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "a.txt"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "contents"}]}]}],
        "tools": [{"name": "Read", "description": "read a file",
                   "input_schema": {"type": "object",
                                    "properties": {"file_path":
                                                   {"type": "string"}}}}],
        "max_tokens": 100, "stream": True}, model="deepseek-chat")
    # The caller's system prompt is CARRIED, not replaced — Aethron's
    # identity is prepended to it at this seam (see to_openai), so exact
    # equality is the wrong assertion now. Both halves are checked.
    _sys = req["messages"][0]
    ck("system prompt becomes an OpenAI system message",
       _sys["role"] == "system" and "be brief" in _sys["content"])
    ck("Aethron's identity has the LAST word in every request",
       _sys["content"].index("You are Aethron, made by Jomiez.")
       > _sys["content"].index("be brief"))
    # skip the seeded introduction — the first assistant turn of every
    # request is now Aethron saying who she is (see SEED_A)
    asst = next(m for m in req["messages"]
                if m["role"] == "assistant" and m.get("content") != SEED_A)
    ck("she introduces herself before anything the caller sent",
       req["messages"][2]["content"] == SEED_A)
    ck("tool_use becomes an OpenAI tool_call",
       asst["tool_calls"][0]["function"]["name"] == "Read"
       and json.loads(asst["tool_calls"][0]["function"]["arguments"])
       == {"file_path": "a.txt"})
    tool_msg = next(m for m in req["messages"] if m["role"] == "tool")
    ck("tool_result becomes an OpenAI tool message",
       tool_msg["tool_call_id"] == "t1" and tool_msg["content"] == "contents")
    ck("tools convert to function schemas",
       req["tools"][0]["function"]["name"] == "Read")
    ck("model override wins", req["model"] == "deepseek-chat")

    # 2. response translation, end to end over HTTP
    up, up_url = _mock_openai([[{"text": "hello there"},
                                {"tool": "Write",
                                 "input": {"file_path": "x", "content": "y"},
                                 # Gemini's compatibility stream has emitted
                                 # this exact redundant prefix in the wild.
                                 "argument_chunks": [
                                     "{}", '{"file_path":"x","content":"y"}']} ]])
    srv, url = start(BridgeConfig(up_url, "k", "deepseek-chat"))
    body = json.dumps({"model": "claude-x", "max_tokens": 50, "stream": True,
                       "messages": [{"role": "user", "content": "go"}],
                       "tools": [{"name": "Write", "input_schema":
                                  {"type": "object"}}]}).encode()
    r = urllib.request.urlopen(urllib.request.Request(
        url + "/v1/messages", body, {"Content-Type": "application/json"}),
        timeout=30)
    events = []
    for line in r:
        s = line.decode().strip()
        if s.startswith("event:"):
            events.append(s.split(":", 1)[1].strip())
        elif s.startswith("data:") and "input_json_delta" in s:
            events.append("TOOLJSON:" + json.loads(s[5:])["delta"]
                          ["partial_json"])
    ck("emits message_start first", events and events[0] == "message_start")
    ck("streams a text block", "content_block_start" in events
       and "content_block_delta" in events)
    ck("streams tool input as input_json_delta",
       any(e.startswith("TOOLJSON:") for e in events))
    tool_json = [e[9:] for e in events if e.startswith("TOOLJSON:")]
    ck("repairs a duplicated empty tool-input prefix",
       len(tool_json) == 1 and json.loads(tool_json[0])
       == {"file_path": "x", "content": "y"})
    ck("ends with message_stop", events[-1] == "message_stop")

    # 3. non-streaming path
    body = json.dumps({"model": "claude-x", "max_tokens": 50,
                       "messages": [{"role": "user", "content": "go"}]}).encode()
    r = urllib.request.urlopen(urllib.request.Request(
        url + "/v1/messages", body, {"Content-Type": "application/json"}),
        timeout=30)
    msg = json.loads(r.read())
    ck("non-streaming returns an Anthropic message",
       msg["type"] == "message" and msg["role"] == "assistant")

    # 4. upstream failures reach the caller as readable errors
    bad_srv, bad_url = start(BridgeConfig("http://127.0.0.1:9", "k", "m"))
    try:
        urllib.request.urlopen(urllib.request.Request(
            bad_url + "/v1/messages", body,
            {"Content-Type": "application/json"}), timeout=30)
        ck("unreachable upstream reports an error", False)
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read())
        ck("unreachable upstream reports an error",
           e.code == 502 and "could not reach" in
           detail["error"]["message"])
    bad_srv.shutdown()
    srv.shutdown()
    up.shutdown()

    # 5. THE SPEND GUARD — the part that protects a real API key
    up2, up2_url = _mock_openai([[{"text": "hello"}]])
    srv2, url2 = start(BridgeConfig(up2_url, "k", "deepseek-v4-pro"))
    reset_usage()
    set_limits(requests=3, tokens=10_000_000, repeats=99)

    def ask(n):
        b = json.dumps({"model": "x", "max_tokens": 20,
                        "messages": [{"role": "user",
                                      "content": f"request {n}"}]}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                url2 + "/v1/messages", b,
                {"Content-Type": "application/json"}), timeout=30).read()
            return 200, ""
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]

    codes = [ask(i)[0] for i in range(3)]
    ck("requests under the cap go through", codes == [200, 200, 200], str(codes))
    code, msg = ask(99)
    # a 400, never a 429: the engine retries a 429 in silence for minutes
    ck("the request cap stops the run, with a refusal the engine does not retry",
       code == 400 and "spend guard" in msg,
       f"{code} {msg[:80]}")
    ck("and it says what was spent", "$" in msg and "tokens" in msg, msg[:90])
    ck("usage is accounted", usage_report()["input"] > 0
       and usage_report()["usd"] >= 0)

    reset_usage()
    set_limits(requests=999, tokens=10_000_000, repeats=3)
    # the agent's own turns stream; a side call repeated is not a loop
    same = json.dumps({"model": "x", "max_tokens": 20, "stream": True,
                       "messages": [{"role": "user", "content": "identical"}]}
                      ).encode()
    side = json.dumps({"model": "x", "max_tokens": 20,
                       "messages": [{"role": "user", "content": "state?"}]}
                      ).encode()
    side_codes = []
    for _ in range(5):
        try:
            urllib.request.urlopen(urllib.request.Request(
                url2 + "/v1/messages", side,
                {"Content-Type": "application/json"}), timeout=30).read()
            side_codes.append(200)
        except urllib.error.HTTPError as e:
            side_codes.append(e.code)
    ck("the engine's repeated side calls are not taken for a loop",
       side_codes == [200] * 5, str(side_codes))

    def ask_same():
        try:
            urllib.request.urlopen(urllib.request.Request(
                url2 + "/v1/messages", same,
                {"Content-Type": "application/json"}), timeout=30).read()
            return 200, ""
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]

    seen = [ask_same()[0] for _ in range(5)]
    ck("a looping agent is cut off", 400 in seen, str(seen))
    reset_usage()
    set_limits(requests=200, tokens=2_000_000, repeats=3)
    srv2.shutdown()
    up2.shutdown()

    # A KEY THAT HANGS MUST LOSE ITS TURN, NOT THE RUN.
    # Rotation handles a key that ERRORS. A key that never answers is the
    # dangerous one: it blocked the upstream read for longer than the
    # agent's idle guard allows, so the session was killed before another
    # key was ever tried, and the log said "the agent went silent" — which
    # reads as a model with nothing to say, not as one bad credential.
    global UPSTREAM_TIMEOUT
    _saved_to, UPSTREAM_TIMEOUT = UPSTREAM_TIMEOUT, 3.0
    HANGS, GOOD = "key-that-hangs", "key-that-works"
    tried = []

    class _Up3(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            key = (self.headers.get("Authorization") or "").replace(
                "Bearer ", "")
            tried.append(key)
            if key == HANGS:
                time.sleep(30)            # answers nothing, ever
                return
            out = json.dumps(
                {"id": "x", "choices": [{"message": {
                    "role": "assistant", "content": "ok"},
                    "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    up3 = ThreadingHTTPServer(("127.0.0.1", 0), _Up3)
    threading.Thread(target=up3.serve_forever, daemon=True).start()
    cfg3 = BridgeConfig(f"http://127.0.0.1:{up3.server_address[1]}",
                        [HANGS, GOOD], model="m")
    srv3, url3 = start(cfg3)
    reset_usage()
    t0 = time.time()
    answered = ""
    try:
        rq = urllib.request.Request(
            url3 + "/v1/messages",
            data=json.dumps({"model": "claude", "max_tokens": 16,
                             "messages": [{"role": "user",
                                           "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json",
                     "x-api-key": "aethron"})
        with urllib.request.urlopen(rq, timeout=60) as rs:
            answered = "".join(c.get("text", "") for c in
                               json.loads(rs.read()).get("content", []))
    except Exception as e:
        answered = f"<{type(e).__name__}>"
    took = time.time() - t0
    ck("a hanging key rotates instead of killing the session",
       answered == "ok" and took < 20 and tried[:2] == [HANGS, GOOD],
       f"{answered!r} in {took:.1f}s, tried={tried}")
    UPSTREAM_TIMEOUT = _saved_to
    reset_usage()
    srv3.shutdown()
    up3.shutdown()

    # ── PARALLEL CALLS THAT ALL SAY "index 0" ───────────────────────
    # The exact shape that broke the owner's session: Gemini streamed
    # list_projects and WebFetch as two calls, both at index 0, each with
    # its own id. Keyed by index they fused into one refused call and the
    # model retried it fourteen times. They must arrive as TWO tool_use
    # blocks, each with its own name and its own arguments.
    class _Up4(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "call_a", "type": "function",
                    "function": {"name": "mcp__aethron__list_projects",
                                 "arguments": "{}"}}]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "call_b", "type": "function",
                    "function": {"name": "WebFetch", "arguments":
                        json.dumps({"url": "https://www.jomiez.com/",
                                    "prompt": "Extract key facts"})}}]}}]},
                {"choices": [{"index": 0, "delta": {},
                              "finish_reason": "tool_calls"}]},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    up4 = ThreadingHTTPServer(("127.0.0.1", 0), _Up4)
    threading.Thread(target=up4.serve_forever, daemon=True).start()
    srv4, url4 = start(BridgeConfig(
        f"http://127.0.0.1:{up4.server_address[1]}", "k", model="m"))
    blocks, current = [], {}
    try:
        rq = urllib.request.Request(
            url4 + "/v1/messages",
            data=json.dumps({"model": "claude", "max_tokens": 64,
                             "stream": True, "messages": [
                                 {"role": "user", "content": "go"}]}).encode(),
            headers={"Content-Type": "application/json",
                     "x-api-key": "aethron"})
        with urllib.request.urlopen(rq, timeout=30) as rs:
            for raw in rs.read().decode().splitlines():
                if not raw.startswith("data: "):
                    continue
                ev = json.loads(raw[6:])
                if ev.get("type") == "content_block_start" and \
                        ev["content_block"].get("type") == "tool_use":
                    current = {"name": ev["content_block"]["name"], "json": ""}
                    blocks.append(current)
                elif ev.get("type") == "content_block_delta" and \
                        ev["delta"].get("type") == "input_json_delta":
                    current["json"] += ev["delta"]["partial_json"]
    except Exception as e:
        blocks = [{"name": f"<{type(e).__name__}: {e}>", "json": ""}]
    names = [b["name"] for b in blocks]
    args = [json.loads(b["json"] or "{}") for b in blocks
            if not b["name"].startswith("<")]
    ck("parallel calls at one index stay two calls",
       names == ["mcp__aethron__list_projects", "WebFetch"]
       and args[0] == {} and args[1].get("url") == "https://www.jomiez.com/",
       f"{names} {args}")
    srv4.shutdown()
    up4.shutdown()

    # ── A SPENT KEY IS BENCHED, NOT FATAL — the owner's stalled fill ──
    # Measured 2026-09-23 on Google's free tier: the refusal for a spent
    # DAY and a busy MINUTE use the same sentence; only the quota id
    # differs, and the per-day one still says "Please retry in 17s". The
    # agent's bridge was handed ONE key and raised on the first spent day
    # while three other keys had allowance.
    def _g429(quota, delay):
        return json.dumps([{"error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "message": "You exceeded your current quota, please check your "
                       "plan and billing details. limit: 20, model: m\n"
                       f"Please retry in {delay}s.",
            "details": [{"@type": "type.googleapis.com/google.rpc."
                                  "QuotaFailure",
                         "violations": [{"quotaId": quota,
                                         "quotaValue": "20"}]},
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                         "retryDelay": f"{delay}s"}]}}]).encode()
    DAY = _g429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", 17)
    MINUTE = _g429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", 1)
    tried5 = []

    class _Up5(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            key = (self.headers.get("Authorization") or "")[7:]
            tried5.append(key)
            if key.startswith(("spent", "pacing")):
                body = DAY if key.startswith("spent") else MINUTE
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            out = json.dumps({"id": "x", "choices": [{"message": {
                "role": "assistant", "content": "ok:" + key},
                "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000,
                          "completion_tokens": 1000}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    up5 = ThreadingHTTPServer(("127.0.0.1", 0), _Up5)
    threading.Thread(target=up5.serve_forever, daemon=True).start()
    base5 = f"http://127.0.0.1:{up5.server_address[1]}"

    def ask5(url):
        rq = urllib.request.Request(
            url + "/v1/messages", data=json.dumps(
                {"model": "claude", "max_tokens": 16, "messages": [
                    {"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json",
                     "x-api-key": "aethron"})
        try:
            with urllib.request.urlopen(rq, timeout=60) as rs:
                return 200, "".join(c.get("text", "") for c in
                                    json.loads(rs.read()).get("content", []))
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    benched = []
    cfg5 = BridgeConfig(base5, ["spent-1", "good-free", "paid"], model="m",
                        free=["spent-1", "good-free"],
                        on_spent=benched.append)
    srv5, url5 = start(cfg5)
    reset_usage()
    t0 = time.time()
    code, said = ask5(url5)
    ck("a key spent for the day hands over to the next key at once",
       code == 200 and said == "ok:good-free" and time.time() - t0 < 5
       and tried5 == ["spent-1", "good-free"], f"{code} {said!r} {tried5}")
    ck("the spent key is recorded for the day", benched == ["spent-1"],
       str(benched))
    ck("the paid key is not touched while a free key answers",
       "paid" not in tried5, str(tried5))
    ck("a free key's answer costs $0 (its list price kept apart)",
       USED["usd"] == 0 and USED["list_usd"] > 0,
       f"usd={USED['usd']} list={USED['list_usd']}")
    tried5.clear()
    code, said = ask5(url5)
    ck("a benched key is not asked again", tried5 == ["good-free"],
       str(tried5))
    srv5.shutdown()

    tried5.clear()
    benched.clear()
    srv5, url5 = start(BridgeConfig(base5, ["pacing-1", "good-2"], model="m",
                                    free=["pacing-1", "good-2"],
                                    on_spent=benched.append))
    code, said = ask5(url5)
    ck("a per-minute limit moves to the next key without benching it",
       code == 200 and said == "ok:good-2" and not benched,
       f"{code} {said!r} benched={benched}")
    srv5.shutdown()

    tried5.clear()
    reset_usage()      # the repeat detector would rightly stop a 4th copy
    srv5, url5 = start(BridgeConfig(base5, ["spent-a", "spent-b"], model="m",
                                    free=["spent-a", "spent-b"]))
    t0 = time.time()
    code, said = ask5(url5)
    ck("when every key is spent the answer is quick and says so",
       code == 429 and "SPENT" in said and time.time() - t0 < 10,
       f"{code} in {time.time() - t0:.1f}s: {said[:120]}")
    tried5.clear()
    code, said = ask5(url5)
    ck("and the next request is refused without asking anyone",
       code == 429 and "today's allowance" in said and not tried5,
       f"{code} tried={tried5}: {said[:120]}")
    srv5.shutdown()
    up5.shutdown()
    st = status()
    ck("the bridge reports itself idle once nothing is in flight",
       st["inflight"] == 0 and st["state"] == "idle", str(st))
    reset_usage()

    # ── WHAT A PROVIDER DEMANDS BACK OUTLIVES THE PROCESS ─────────────
    # A resumed conversation replays old tool calls. Gemini refuses one
    # without its thought_signature, and the signatures lived in RAM.
    global MEMORY_FILE
    import tempfile as _tf
    saved_mem, saved_sigs = MEMORY_FILE, dict(_SIGNATURES)
    MEMORY_FILE = os.path.join(_tf.mkdtemp(), "bridge_memory.json")
    _remember_sig("call_keep", "SIG-123")
    _SIGNATURES.clear()                      # the app restarts
    load_memory()
    ck("a signature survives a restart", _SIGNATURES.get("call_keep") == "SIG-123")
    replay = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_keep", "name": "t", "input": {}},
            {"type": "tool_use", "id": "call_lost", "name": "t", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_keep", "content": "a"},
            {"type": "tool_result", "tool_use_id": "call_lost", "content": "b"}]}]}
    sent = {tc["id"]: ((tc.get("extra_content") or {}).get("google") or {})
            .get("thought_signature")
            for m in to_openai(replay, "gemini-3.8-flash")["messages"]
            for tc in (m.get("tool_calls") or [])}
    ck("a resumed Gemini call carries its own signature, or Google's "
       "documented stand-in when it is gone",
       sent.get("call_keep") == "SIG-123"
       and sent.get("call_lost") == GEMINI_NO_SIGNATURE, str(sent))
    other = [tc for m in to_openai(replay, "deepseek-chat")["messages"]
             for tc in (m.get("tool_calls") or [])
             if tc["id"] == "call_lost"]
    ck("no stand-in is invented for a provider that is not Gemini",
       other and "extra_content" not in other[0], str(other))
    MEMORY_FILE = saved_mem
    _SIGNATURES.clear()
    _SIGNATURES.update(saved_sigs)

    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
    return 1 if bad else 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    try:
        import aethron_brain as brain
        cfg = brain.bridge_config()
    except Exception as e:
        print(f"no AI settings ({e}) — set them in the studio or "
              f"aethron_config.json")
        return 1
    port = 0
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    srv, url = start(cfg, port)
    print(f"Aethron bridge: {url}/v1/messages -> {cfg.base_url} "
          f"({cfg.model or 'provider default'})")
    print("point Claude Code at it:")
    print(f"  ANTHROPIC_BASE_URL={url} ANTHROPIC_AUTH_TOKEN={cfg.token} claude")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
