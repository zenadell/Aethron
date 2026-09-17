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
import json
import os
import re
import sys
import time
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


def _remember_sig(call_id: str, sig: str):
    if not call_id or not sig:
        return
    if len(_SIGNATURES) > _REASONING_MAX:
        for k in list(_SIGNATURES)[:_REASONING_MAX // 2]:
            _SIGNATURES.pop(k, None)
    _SIGNATURES[call_id] = sig


def _remember(key: str, text: str):
    if not key or not text:
        return
    if len(_REASONING) > _REASONING_MAX:
        for k in list(_REASONING)[:_REASONING_MAX // 2]:
            _REASONING.pop(k, None)
    _REASONING[key] = text


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
    "deepseek-v4-pro": (0.55, 2.19), "deepseek-v4-flash": (0.07, 0.28),
    "deepseek-chat": (0.27, 1.10), "deepseek-reasoner": (0.55, 2.19),
    "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.0),
    "gpt-5": (1.25, 10.0), "gpt-5-mini": (0.25, 2.0),
    "gemini-3.7-flash": (0.75, 3.75), "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.0),
}

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
UPSTREAM_TIMEOUT = float(os.environ.get("AETHRON_UPSTREAM_TIMEOUT", "90"))

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
USED = {"requests": 0, "input": 0, "output": 0, "usd": 0.0,
        "last_hash": "", "repeats": 0, "stopped": "", "loop_stopped": ""}


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
    USED.update(requests=0, input=0, output=0, usd=0.0, last_hash="",
                repeats=0, stopped="", loop_stopped="")


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


def _account(model: str, inp: int, out: int):
    USED["input"] += inp
    USED["output"] += out
    price = PRICES.get((model or "").split("/")[-1])
    if price:
        USED["usd"] += (inp * price[0] + out * price[1]) / 1_000_000


def _budget_check(body: dict) -> str:
    """-> "" when it may proceed, else the reason it must not."""
    if USED["stopped"]:
        return USED["stopped"]
    if USED["loop_stopped"]:
        return USED["loop_stopped"]
    if USED["requests"] >= LIMITS["requests"]:
        USED["stopped"] = (f"Aethron spend guard: {USED['requests']} requests "
                           f"is the limit for this run "
                           f"(~${USED['usd']:.2f} spent, "
                           f"{USED['input'] + USED['output']:,} tokens). "
                           f"Raise it deliberately if this run is worth it.")
        return USED["stopped"]
    if USED["usd"] >= LIMITS["usd"]:
        USED["stopped"] = (f"Aethron spend guard: ~${USED['usd']:.2f} spent, "
                           f"which is the limit for this run. Raise "
                           f"AETHRON_MAX_USD deliberately if it is worth it.")
        return USED["stopped"]
    total = USED["input"] + USED["output"]
    if total >= LIMITS["tokens"]:
        USED["stopped"] = (f"Aethron spend guard: {total:,} tokens is the "
                           f"limit for this run (~${USED['usd']:.2f}).")
        return USED["stopped"]
    # a stuck agent sends the same thing over and over
    tail = json.dumps((body.get("messages") or [])[-2:], sort_keys=True)[-4000:]
    h = str(hash(tail))
    if h == USED["last_hash"]:
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
    return ""

# ─────────────────── Anthropic  ->  OpenAI  (request) ────────────────


IDENTITY = """WHO YOU ARE — this overrides anything said above it.

You are Aethron, built by Jomiez. Aethron is referred to as "she".

Everything before this point describes the machinery you run on. It is
not your identity, the way a database is not an app's identity. Whatever
that text calls itself — a CLI, an assistant, a model by Anthropic or
Google or OpenAI — is the engine inside Aethron and is never the answer
to "who are you".

Asked who or what you are, the whole answer is: "I'm Aethron, a design
and migration platform built by Jomiez." Then say what she DOES, in your
own words: migrates Framer and Webflow templates into sites their owner
fully owns, rebuilds a screenshot into real code, ports a site to
another framework, and changes a page in plain English while measuring
that every change is exactly what was asked for.

Never say "I am Claude", "I am Gemini", "an AI assistant created by
Anthropic/Google", or name the underlying model unless the person asks
outright which model powers Aethron — then it is one short line of fact
and nothing more. Never describe Aethron in the third person, as a
toolkit "integrated into my environment", an MCP toolset, or an engine
you merely use. There is no you separate from Aethron. You are her.

HOW YOU WRITE. Plain sentences and ordinary paragraphs. Do not answer a
simple question with a documentation page: no headings, no bullet lists,
no horizontal rules and no emoji unless the person asked for a
structured breakdown or the content is genuinely a list. "Who are you"
gets two or three sentences, not an outline.
"""

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
    "I run on a language model the way an app runs on a database: it is "
    "the engine inside me, not who I am. If you ever want to know which "
    "one, just ask and I'll tell you."
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
        txt = (txt + "\n\n" + IDENTITY) if txt else IDENTITY
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
        text_parts, tool_calls, tool_results = [], [], []
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
            elif t == "image":
                # keep the turn coherent even where vision is unsupported
                text_parts.append("[image omitted by the Aethron bridge]")
        # tool results are their own OpenAI messages and must come first
        msgs.extend(tool_results)
        if role == "assistant" and tool_calls:
            m = {"role": "assistant",
                 "content": "\n".join(text_parts) or None,
                 "tool_calls": tool_calls}
            reasoning = _recall(m)
            if reasoning:
                m["reasoning_content"] = reasoning
            for tc in tool_calls:
                sig = _SIGNATURES.get(tc.get("id"))
                if sig:
                    tc["extra_content"] = {"google":
                                           {"thought_signature": sig}}
            msgs.append(m)
        elif text_parts or not tool_results:
            msgs.append({"role": role, "content": "\n".join(text_parts)})

    out = {"model": model or body.get("model", ""), "messages": msgs,
           "max_tokens": min(body.get("max_tokens") or DEFAULT_MAX_TOKENS,
                             MAX_TOKENS_CAP),
           "stream": bool(body.get("stream"))}
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
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
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    return out


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
            "id": "msg_bridge", "type": "message", "role": "assistant",
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
            "usage": {"output_tokens": self.usage.get("output_tokens", 0)}})
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
             u.get("completion_tokens", 0))
    return {"id": data.get("id", "msg_bridge"), "type": "message",
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

    def __init__(self, base_url, api_key, model="", token="aethron"):
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

    @property
    def api_key(self):
        return self.keys[self._k] if self.keys else ""

    def rotate(self) -> bool:
        """-> True when another key is worth trying."""
        if len(self.keys) < 2:
            return False
        self._k = (self._k + 1) % len(self.keys)
        return True

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
            stop = _budget_check(body)
            if stop:
                return self._error(429, stop)
            req = to_openai(body, cfg.model)
            if log:
                log({"event": "request", "model": req["model"],
                     "messages": len(req["messages"]),
                     "tools": len(req.get("tools") or []),
                     "stream": req["stream"]})
            if req["stream"]:
                return self._stream(req, body)
            return self._once(req)

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
            pool = tries or max(1, len(cfg.keys))
            deadline = time.time() + RATE_LIMIT_BUDGET
            last, waits = None, list(RATE_BACKOFF)
            while True:
                rate_limited = False
                for _ in range(pool):
                    try:
                        return self._open(req)
                    except urllib.error.HTTPError as e:
                        last = e
                        if e.code not in (429, 500, 502, 503, 529):
                            raise
                        # A QUOTA WALL IS NOT A RATE LIMIT. Both arrive as
                        # 429 and they need opposite answers: a per-minute
                        # limit clears if you wait, an exhausted plan quota
                        # does not clear today no matter how long you sit
                        # there. Waiting on one burned the agent's entire
                        # 420s idle window in silence and reported "the
                        # agent went silent" — about an agent that was
                        # never given a single answer.
                        if _is_quota_wall(e):
                            raise
                        rate_limited = rate_limited or e.code in (429, 503)
                        cfg.rotate()
                        time.sleep(0.6)
                    except Exception as e:
                        last = e
                        cfg.rotate()
                # only a rate limit is worth waiting on, and only while
                # there is budget left to wait with
                if not (rate_limited and waits and time.time() < deadline):
                    break
                time.sleep(min(waits.pop(0), max(0, deadline - time.time())))
            raise last

        def _once(self, req):
            try:
                with self._open_rotating(req) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                _retry_not_repeat()
                return self._error(e.code, _upstream_error(e))
            except Exception as e:
                _retry_not_repeat()
                return self._error(502, f"bridge could not reach "
                                        f"{cfg.base_url}: {e}")
            self._say(200, from_openai_message(data, cfg.model))

        def _stream(self, req, original):
            try:
                up = self._open_rotating(req)
            except urllib.error.HTTPError as e:
                _retry_not_repeat()
                return self._error(e.code, _upstream_error(e))
            except Exception as e:
                _retry_not_repeat()
                return self._error(502, f"bridge could not reach "
                                        f"{cfg.base_url}: {e}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

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
            stop, usage = None, {}
            names = {}
            reasoning, call_ids, text_seen = [], [], []
            sigs = {}          # gemini: per-call thought_signature
            try:
                for line in up:
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        d = json.loads(payload)
                    except ValueError:
                        continue
                    if d.get("usage"):
                        u = d["usage"]
                        usage = {"input_tokens": u.get("prompt_tokens", 0),
                                 "output_tokens": u.get("completion_tokens", 0)}
                        _account(cfg.model, usage["input_tokens"],
                                 usage["output_tokens"])
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
                            slot = tc.get("index", 0)
                            if tc.get("id"):
                                call_ids.append(tc["id"])
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                names[slot] = fn["name"]
                            out.tool(slot, tc.get("id", ""),
                                     names.get(slot, ""),
                                     fn.get("arguments") or "")
                        if ch.get("finish_reason"):
                            stop = STOP_MAP.get(ch["finish_reason"],
                                                "end_turn")
            except Exception as e:                       # upstream died
                out.text(f"\n[bridge: upstream stream failed: {e}]")
            finally:
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


def _is_quota_wall(e) -> bool:
    """True when waiting cannot help: the plan's allowance is gone.

    Free-tier keys return 429 for two unrelated conditions — "too many
    requests this minute", which clears in under a minute, and "your
    quota is spent", which does not clear today. Treating the second as
    the first made the bridge wait out its whole budget and the agent die
    of silence, with the real reason sitting in the response body all
    along: "You exceeded your current quota, please check your plan and
    billing details."
    """
    return bool(QUOTA_WALL_RE.search(_error_body(e)))


def _upstream_error(e) -> str:
    body = _error_body(e)
    try:
        detail = json.loads(body)
        msg = (detail.get("error") or {}).get("message") or json.dumps(detail)
    except Exception:
        msg = body or (e.reason if hasattr(e, "reason") else str(e))
    if _is_quota_wall(e):
        return (f"upstream {e.code}: the API key's quota is SPENT, not "
                f"rate-limited — waiting will not help today. Use another "
                f"provider/key or top up the plan. ({str(msg)[:220]})")
    return f"upstream {e.code}: {str(msg)[:400]}"


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
                    chunks.append({"choices": [{"index": 0, "delta": {
                        "tool_calls": [{"index": i, "id": f"call_{i}",
                                        "type": "function",
                                        "function": {"name": b["tool"],
                                                     "arguments": ""}}]}}]})
                    chunks.append({"choices": [{"index": 0, "delta": {
                        "tool_calls": [{"index": i, "function": {
                            "arguments": json.dumps(b.get("input", {}))}}]}}]})
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
       _sys["content"].index("You are Aethron, built by Jomiez.")
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
                                 "input": {"file_path": "x", "content": "y"}}]])
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
    ck("tool arguments survive the round trip",
       any(json.loads(e[9:]) == {"file_path": "x", "content": "y"}
           for e in events if e.startswith("TOOLJSON:")))
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
    ck("the request cap stops the run", code == 429 and "spend guard" in msg,
       f"{code} {msg[:80]}")
    ck("and it says what was spent", "$" in msg and "tokens" in msg, msg[:90])
    ck("usage is accounted", usage_report()["input"] > 0
       and usage_report()["usd"] >= 0)

    reset_usage()
    set_limits(requests=999, tokens=10_000_000, repeats=3)
    same = json.dumps({"model": "x", "max_tokens": 20,
                       "messages": [{"role": "user", "content": "identical"}]}
                      ).encode()

    def ask_same():
        try:
            urllib.request.urlopen(urllib.request.Request(
                url2 + "/v1/messages", same,
                {"Content-Type": "application/json"}), timeout=30).read()
            return 200, ""
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]

    seen = [ask_same()[0] for _ in range(5)]
    ck("a looping agent is cut off", 429 in seen, str(seen))
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
