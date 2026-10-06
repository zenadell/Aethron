#!/usr/bin/env python3
"""Saved providers: add several, switch with one click, and what runs is
what was switched to.

    python3 tests/provider_battery.py

The owner, 2026-10-06: after switching away from Gemini, Aethron "keeps
using Gemini in the background" and fails. Two causes, both measured:
settings were ONE record, so the Gemini key ring stayed in force and was
sent to the new provider; and the change/edit/spec writer posted to
Google's address with a Gemini model name written into the code.

Offline: settings live in a throwaway file, the network is faked and
every request is recorded — address, key, model.
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
import aethron_brain as brain  # noqa: E402
import aethron_build as B      # noqa: E402

checks = []


def ck(name, ok, detail=""):
    checks.append((name, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))


tmp = Path(tempfile.mkdtemp(prefix="ae-prov-"))
brain.CONFIG = tmp / "aethron_config.json"
brain.BUNDLED = tmp / "bundled-none.json"
brain.KEYSTATE = tmp / "keys.json"
brain.WALLET = tmp / "spend.json"
brain.wallet_set(5.0)

# the owner's settings as they are today: one flat Gemini record
brain.CONFIG.write_text(json.dumps({"ai": {
    "provider": "gemini", "model": "gemini-3.8-flash",
    "api_keys": ["g-free-1", "g-free-2"], "api_key": "g-paid",
    "budget_usd": 2.0}}))

r = brain.resolve()
ck("an existing single-provider setup still works untouched",
   r["provider"] == "gemini" and r["key"] == "g-free-1", r)
P = brain.profiles()
ck("…and appears as one saved provider",
   len(P) == 1 and P[0]["provider"] == "gemini" and P[0]["keys"] == 3, P)

# add DeepSeek the way the panel does: pick it, paste a key, save
brain.save({"provider": "deepseek", "model": "deepseek-v4-pro",
            "api_key": "d-key", "base_url": ""})
r = brain.resolve()
ck("adding a provider makes it the one in use",
   r["provider"] == "deepseek" and r["base"].startswith("https://api.deepseek.com"), r)
ck("THE BUG: the Gemini keys are NOT handed to DeepSeek",
   brain.ring() == ["d-key"] and r["key"] == "d-key", brain.ring())
ck("…and Gemini's own keys are still saved, untouched",
   brain.profile_keys("gemini") == ["g-free-1", "g-free-2", "g-paid"],
   brain.profile_keys("gemini"))
ck("global settings (spend caps) survive the move to profiles",
   brain.load().get("budget_usd") == 2.0, brain.load().get("budget_usd"))

ids = {p["provider"]: p["id"] for p in brain.profiles()}
brain.switch(ids["gemini"])
ck("one switch back to Gemini brings back its keys and model",
   brain.resolve()["provider"] == "gemini"
   and brain.ring() == ["g-free-1", "g-free-2", "g-paid"]
   and brain.resolve()["model"] == "gemini-3.8-flash", brain.resolve())
brain.switch(ids["deepseek"])
ck("…and one switch forward again", brain.resolve()["key"] == "d-key")

# saving the active provider again with no key keeps its key
brain.save({"provider": "deepseek", "model": "deepseek-v4-flash"})
ck("saving a provider without retyping its key keeps the key",
   brain.resolve()["key"] == "d-key"
   and brain.resolve()["model"] == "deepseek-v4-flash", brain.resolve())

listed = json.dumps(brain.profiles()) + json.dumps(brain.status())
ck("the switcher and status never carry a key",
   not any(k in listed for k in ("g-free-1", "g-free-2", "g-paid", "d-key")),
   "a key leaked into profiles()/status()")

# ── what actually gets called ───────────────────────────────────────────
seen = []


class Reply:
    def __init__(self, d):
        self.d = json.dumps(d).encode()

    def read(self):
        return self.d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake(req, timeout=None):
    body = json.loads(req.data or b"{}")
    seen.append({"url": req.full_url, "model": body.get("model"),
                 "auth": req.get_header("Authorization")
                 or req.get_header("X-api-key") or ""})
    if req.full_url.endswith("/v1/messages"):
        return Reply({"content": [{"type": "text", "text": "ok"}],
                      "stop_reason": "end_turn",
                      "usage": {"input_tokens": 10, "output_tokens": 5}})
    return Reply({"choices": [{"finish_reason": "stop",
                               "message": {"content": "ok"}}],
                  "usage": {"prompt_tokens": 10, "total_tokens": 15}})


real = urllib.request.urlopen
urllib.request.urlopen = fake
try:
    B.gemini_text("hi", {}, budget_usd=1.0)
    ck("the change/edit writer calls the ACTIVE provider, not Google",
       seen and "api.deepseek.com" in seen[-1]["url"]
       and seen[-1]["model"] == "deepseek-v4-flash"
       and seen[-1]["auth"] == "Bearer d-key", seen[-1:])

    brain.switch(ids["gemini"])
    B.gemini_text("hi", {}, budget_usd=1.0)
    ck("…and Google again after switching back",
       "generativelanguage.googleapis.com" in seen[-1]["url"]
       and seen[-1]["auth"] == "Bearer g-free-1", seen[-1:])

    brain.save({"provider": "anthropic", "model": "claude-sonnet-5",
                "api_key": "a-key"})
    B.gemini_text("hi", {}, budget_usd=1.0)
    ck("an Anthropic-wire provider is spoken to in its own format",
       seen[-1]["url"].endswith("/v1/messages") and seen[-1]["auth"] == "a-key",
       seen[-1:])

    d = Path(tempfile.mkdtemp())
    w = B.gemini_writer(budget_usd=1.0, verbose=False)
    try:
        w("make a page", d, 0)
    except RuntimeError:
        pass        # "ok" is not a page; what matters is where it went
    ck("the build loop's page writer follows the switch too",
       seen[-1]["url"].endswith("/v1/messages"), seen[-1:])

    os.environ["AETHRON_EMBED_KEYS"] = ""
    os.environ.pop("AETHRON_EMBED_KEYS")
    import aethron_rag as R
    ck("brand memory keeps its Gemini keys while another provider is active",
       R._keys() == ["g-free-1", "g-free-2", "g-paid"], R._keys())
finally:
    urllib.request.urlopen = real

brain.delete_profile(brain.load()["active"])
ck("removing the provider in use falls back to another saved one",
   brain.resolve()["provider"] in ("gemini", "deepseek")
   and len(brain.profiles()) == 2, brain.profiles())

bad = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
sys.exit(1 if bad else 0)
