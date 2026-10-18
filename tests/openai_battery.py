#!/usr/bin/env python3
"""OpenAI keeps working: gpt-4o-mini, the gpt-5 family, and a key whose
billing was refused once.

    python3 tests/openai_battery.py

The owner, 2026-10-18: GPT-4o mini "suddenly stopped working" with credit
still on the account. The model is live at OpenAI; the causes were ours:
  1. since 2026-10-06 every change/edit call sent `reasoning_effort`, which
     gpt-4o-mini refuses with a 400;
  2. gpt-4o-mini was missing from the price table, so it counted at the
     dearest rate (~20x) and the spend caps tripped on cents;
  3. one billing refusal benched the PAID key until midnight Pacific;
  4. a refusal reached the person as "HTTP Error 400: Bad Request".
A fake OpenAI enforces the rules the real one does.

Offline: settings in a throwaway folder, the network faked.
"""
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for v in ("AETHRON_AI_PROVIDER", "AETHRON_AI_KEY", "AETHRON_AI_MODEL",
          "AETHRON_AI_BASE_URL", "AETHRON_WALLET_USD"):
    os.environ.pop(v, None)
import aethron_brain as brain    # noqa: E402
import aethron_build as B        # noqa: E402
import aethron_bridge as BR      # noqa: E402

checks = []


def ck(name, ok, detail=""):
    checks.append((name, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))


tmp = Path(tempfile.mkdtemp(prefix="ae-openai-"))
brain.CONFIG = tmp / "aethron_config.json"
brain.BUNDLED = tmp / "bundled-none.json"
brain.KEYSTATE = tmp / "keys.json"
brain.WALLET = tmp / "spend.json"
brain.wallet_set(5.0)
brain.save({"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"})

seen = []
MODE = {"billing": False}


def err(code, typ, msg):
    body = json.dumps({"error": {"message": msg, "type": typ,
                                 "code": typ}}).encode()
    return urllib.error.HTTPError("https://api.openai.com/v1/chat/completions",
                                  code, "refused", {}, io.BytesIO(body))


class Reply:
    def __init__(self, d):
        self.d = json.dumps(d).encode()

    def read(self):
        return self.d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_openai(req, timeout=None):
    """Refuses exactly what api.openai.com refuses."""
    b = json.loads(req.data or b"{}")
    seen.append(b)
    m = b.get("model", "")
    reasoning = m.startswith(("gpt-5", "o1", "o3", "o4"))
    if MODE["billing"]:
        raise err(429, "insufficient_quota",
                  "You exceeded your current quota, please check your plan "
                  "and billing details.")
    if "reasoning_effort" in b and not reasoning:
        raise err(400, "invalid_request_error",
                  "Unrecognized request argument supplied: reasoning_effort")
    if reasoning and "max_tokens" in b:
        raise err(400, "invalid_request_error",
                  "Unsupported parameter: 'max_tokens' is not supported "
                  "with this model. Use 'max_completion_tokens' instead.")
    if reasoning and b.get("temperature") not in (None, 1):
        raise err(400, "invalid_request_error",
                  "Unsupported value: 'temperature' does not support 0.3 "
                  "with this model. Only the default (1) value is supported.")
    return Reply({"choices": [{"finish_reason": "stop",
                               "message": {"content": "ok"}}],
                  "usage": {"prompt_tokens": 10, "total_tokens": 15}})


real = urllib.request.urlopen
urllib.request.urlopen = fake_openai
try:
    out = B.gemini_text("hi", {}, budget_usd=1.0)
    ck("a change/edit call to gpt-4o-mini is accepted",
       "reasoning_effort" not in seen[-1] and seen[-1]["model"] == "gpt-4o-mini",
       seen[-1:])

    ck("the template side (fill/plan) answers on gpt-4o-mini",
       brain.text_call("hi") == "ok")

    brain.save({"provider": "openai", "model": "gpt-5", "api_key": "sk-test"})
    try:
        B.gemini_text("hi", {}, budget_usd=1.0)
        ok = True
    except Exception as e:
        ok = False
    ck("gpt-5 gets max_completion_tokens and no temperature",
       ok and "max_completion_tokens" in seen[-1] and "max_tokens" not in seen[-1]
       and "temperature" not in seen[-1], seen[-1:])
    brain.save({"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"})

    MODE["billing"] = True
    try:
        brain.text_call("hi")
        msg = ""
    except ValueError as e:
        msg = str(e)
    ck("a billing refusal says what to check, not 'Bad Request'",
       "billing" in msg and "Projects" in msg and "OpenAI" in msg, msg)

    MODE["billing"] = False
    try:
        ans = brain.text_call("hi")
    except Exception as e:
        ans = repr(e)
    ck("once billing is fixed the same key works at once (not benched all day)",
       ans == "ok", ans)
    p = brain._bridge_pool({}, brain.resolve())
    ck("the coding agent never benches a paid key across sessions",
       not p["is_spent"]("sk-test"), p["free"])
finally:
    urllib.request.urlopen = real

# the coding agent's translator
b = BR.to_openai({"model": "x", "max_tokens": 9000, "temperature": 0.7,
                  "messages": [{"role": "user", "content": "hi"}]}, "gpt-5")
ck("the translator sends gpt-5 max_completion_tokens, no temperature",
   "max_completion_tokens" in b and "max_tokens" not in b
   and "temperature" not in b, b)
b = BR.to_openai({"model": "x", "max_tokens": 9000, "temperature": 0.7,
                  "messages": [{"role": "user", "content": "hi"}]}, "gpt-4o-mini")
ck("…and leaves gpt-4o-mini's max_tokens and temperature alone",
   b.get("max_tokens") == 9000 and b.get("temperature") == 0.7, b)
b = BR.to_openai({"model": "x", "max_tokens": 9000,
                  "messages": [{"role": "user", "content": "hi"}]}, "openai/gpt-5")
ck("…and OpenRouter ids keep max_tokens", "max_tokens" in b, b)

# money
ck("gpt-4o-mini is priced at its real rate, not the dearest",
   BR.price_of("gpt-4o-mini") == (0.15, 0.60))
ck("a dated snapshot and a provider prefix price as their model",
   BR.price_of("gpt-4o-mini-2024-07-18") == (0.15, 0.60)
   and BR.price_of("openai/gpt-4o-mini") == (0.15, 0.60))
ck("an unknown model still counts at the dearest rate",
   BR.price_of("gpt-99") == BR.UNKNOWN_PRICE)
ck("a 401 tells the person to paste a fresh key",
   "fresh" in BR._upstream_error(err(401, "invalid_api_key", "Incorrect API key")))

bad = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
sys.exit(1 if bad else 0)
