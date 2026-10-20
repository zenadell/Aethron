#!/usr/bin/env python3
"""The selected model is the one that is called — and every request says
where it went.

temple selected DeepSeek, saw no DeepSeek spend and watched Gemini credit
fall from $0.70 to $0.53. Two side jobs took a saved Gemini/OpenAI key
whatever was selected: pictures for image-to-code (~$0.04 each on Gemini)
and brand-search embeddings. This battery saves BOTH providers with fake
keys, selects DeepSeek, intercepts every outgoing request, and drives each
path a model is reached by: the agent's bridge, a text call, the page
writer, picture making and brand search. No network, no key, no browser.
"""
import io
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

HOME = tempfile.mkdtemp()
os.environ["AETHRON_HOME"] = HOME
for v in ("AETHRON_AI_PROVIDER", "AETHRON_AI_KEY", "AETHRON_AI_MODEL",
          "AETHRON_AI_BASE_URL", "AETHRON_EMBED_KEYS", "AETHRON_USAGE"):
    os.environ.pop(v, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_brain as B          # noqa: E402

# a fresh home: nothing of this machine's settings leaks in
B.HOME = Path(HOME)
B.CONFIG = B.HOME / "aethron_config.json"
B.KEYSTATE = B.HOME / "aethron_keys.json"
B.BUNDLED = B.CONFIG

SENT = []
_real = urllib.request.urlopen


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, *a, **k):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if url.startswith("http://127.0.0.1"):          # our own bridge
        return _real(req, *a, **k)
    SENT.append(url)
    if "batchEmbedContents" in url:
        n = len(json.loads(req.data)["requests"])
        return _Resp(json.dumps({"embeddings": [{"values": [1.0, 0.0]}] * n}).encode())
    if "/images/" in url or ":generateContent" in url:
        raise AssertionError("a picture was asked for")
    return _Resp(json.dumps({
        "id": "x", "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                  "total_tokens": 12}}).encode())


urllib.request.urlopen = fake_urlopen

RESULTS = []


def ck(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


def google(urls):
    return [u for u in urls if "googleapis.com" in u]


# Gemini saved first (as the owner did), then DeepSeek saved and selected.
B.save({"provider": "gemini", "model": "gemini-3.8-flash",
        "api_key": "AIza-paid-gemini", "api_keys": ["AIza-free-1"]})
B.save({"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-openai"})
B.save({"provider": "deepseek", "model": "deepseek-flash", "api_key": "sk-deepseek"})
r = B.resolve()
ck("DeepSeek is the selected provider",
   r["provider"] == "deepseek" and "deepseek.com" in r["base"] and r["key"] == "sk-deepseek")

# 1. the agent: the bridge it is given points at DeepSeek with DeepSeek's key
e = B.anthropic_endpoint()
cfgb = B.bridge_config()
ck("the agent's bridge sends to DeepSeek with DeepSeek's key only",
   e["mode"] == "bridge" and "deepseek.com" in cfgb.base_url
   and cfgb.keys == ["sk-deepseek"], f"{cfgb.base_url} {cfgb.keys}")
SENT.clear()
req = urllib.request.Request(
    e["base_url"] + "/v1/messages",
    json.dumps({"model": "claude-x", "max_tokens": 50, "stream": False,
                "messages": [{"role": "user", "content": "hi"}]}).encode(),
    {"Content-Type": "application/json", "x-api-key": "aethron",
     "anthropic-version": "2023-06-01"})
with _real(req, timeout=30) as resp:
    resp.read()
ck("an agent request reaches DeepSeek and nothing else",
   SENT and all("api.deepseek.com" in u for u in SENT), SENT)

# 2. a one-off text call and 3. the page writer
SENT.clear()
B.text_call("hello")
import aethron_build as BUILD   # noqa: E402
BUILD.gemini_text("hello", {}, 0.5)
ck("text calls and the page writer reach DeepSeek only",
   len(SENT) == 2 and all("api.deepseek.com" in u for u in SENT), SENT)

# 4. pictures and 5. brand search, with nothing allowed
import aethron_pictures as PIC   # noqa: E402
import aethron_rag as RAG        # noqa: E402
ck("pictures do not take the saved Gemini or OpenAI key", PIC._image_providers() == [],
   PIC._image_providers())
ck("...and say why, and how to allow it",
   "Gemini" in PIC._blocked_note() and "Settings" in PIC._blocked_note())
SENT.clear()
try:
    RAG.embed(["a sentence"])
    ck("brand search does not take the saved Gemini key", False, "embedded")
except RAG.Unavailable as ex:
    ck("brand search does not take the saved Gemini key",
       not google(SENT) and "Settings" in str(ex), str(ex))

# switched on: the side jobs may use the other provider, and say so
B.save({"helpers": {"pictures": True, "brand_search": True}})
ck("still DeepSeek after allowing side jobs", B.resolve()["provider"] == "deepseek")
provs = [p for p, _ in PIC._image_providers()]
ck("allowed: pictures may use OpenAI then Gemini", provs[:1] == ["openai"] and "gemini" in provs, provs)
SENT.clear()
RAG.embed(["a sentence"])
ck("allowed: brand search uses Gemini", google(SENT), SENT)

# Gemini selected: its own side jobs need no permission
B.save({"helpers": {"pictures": False, "brand_search": False}})
B.switch("gemini")
ck("Gemini selected: its own key makes pictures without a switch",
   {p for p, _ in PIC._image_providers()} == {"gemini"},
   (B.resolve()["provider"], PIC._image_providers()))
B.shutdown_bridge()

# 6. the record: every request named by the address it went to
import aethron_usage as U   # noqa: E402
s = U.summary()
by = {p["label"]: p for p in s["providers"]}
ck("the record lists DeepSeek for the agent, text and writer",
   "DeepSeek" in by and {"agent", "text", "writer"} <= set(by["DeepSeek"]["purposes"]),
   by.get("DeepSeek"))
ck("the record lists Gemini only for the brand search that was allowed",
   "Gemini" in by and set(by["Gemini"]["purposes"]) == {"embedding"}, by.get("Gemini"))
raw = (Path(HOME) / "model_use.jsonl").read_text()
ck("no key is ever written to the record",
   not any(k in raw for k in ("sk-deepseek", "AIza", "sk-openai")))

n = sum(RESULTS)
print(f"\n{n}/{len(RESULTS)} " + ("ALL PASS" if n == len(RESULTS) else "SOME FAILED"))
sys.exit(0 if n == len(RESULTS) else 1)
