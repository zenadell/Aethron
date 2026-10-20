#!/usr/bin/env python3
"""aethron_usage — which provider every model request REALLY went to.

The owner chose DeepSeek, watched its balance stay put and their Gemini
credit fall from $0.70 to $0.53, and asked the only reasonable question:
is Aethron saying one model and using another? Nothing could answer it.
The agent's requests were counted inside the bridge process and thrown
away with it; pictures for image-to-code were made with a saved Gemini or
OpenAI key whatever was selected, and said so only in a log nobody reads.

So every request any part of Aethron sends to a model — the agent, a
one-off text call, a page writer, a picture, a brand-search embedding —
appends one line here: when, which provider (read from the ADDRESS it was
sent to, never from a label), which model, why, tokens and estimated cost.
The settings screen shows it, so "what spent my Gemini credit" has an
answer on the screen, not a guess.

Never a key, a prompt or an answer: only who, what for, and how much.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

_LOCK = threading.Lock()
KEEP = 3000                      # lines kept; older ones are trimmed

# Read from the address, so a mislabelled profile cannot hide where a
# request went.
HOSTS = (("deepseek.com", "deepseek", "DeepSeek"),
         ("generativelanguage.googleapis.com", "gemini", "Gemini"),
         ("openai.com", "openai", "OpenAI"),
         ("anthropic.com", "anthropic", "Anthropic"),
         ("openrouter.ai", "openrouter", "OpenRouter"),
         ("groq.com", "groq", "Groq"),
         ("127.0.0.1:11434", "ollama", "Ollama"),
         ("localhost:11434", "ollama", "Ollama"))

PURPOSES = {"agent": "the assistant (chat and tools)",
            "text": "a one-off text request",
            "writer": "writing or changing a page",
            "picture": "making a picture for image-to-code",
            "embedding": "brand search (meaning-based)"}


def provider_of(url: str) -> tuple:
    """-> (id, label) for the address a request was sent to."""
    u = (url or "").lower()
    for host, pid, label in HOSTS:
        if host in u:
            return pid, label
    try:
        h = urlparse(url).netloc or url
    except Exception:
        h = url
    return "custom", h or "unknown"


def _file() -> Path:
    h = os.environ.get("AETHRON_HOME")
    if h:
        base = Path(h)
    else:
        try:
            import aethron_brain as B
            base = B.HOME
        except Exception:
            base = Path(__file__).resolve().parent
    return base / "model_use.jsonl"


def record(url: str, model: str, purpose: str, inp: int = 0, out: int = 0,
           usd: float = 0.0, free: bool = False, ok: bool = True,
           note: str = "") -> None:
    """Append one request. Never raises: accounting must not break work."""
    if os.environ.get("AETHRON_USAGE") == "off":
        return
    pid, label = provider_of(url)
    row = {"t": round(time.time(), 1), "provider": pid, "label": label,
           "model": str(model or "")[:80], "purpose": purpose,
           "in": int(inp or 0), "out": int(out or 0),
           "usd": 0.0 if free else round(float(usd or 0.0), 6),
           "free": bool(free), "ok": bool(ok)}
    if note:
        row["note"] = str(note)[:160]
    try:
        f = _file()
        with _LOCK:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            if f.stat().st_size > 900_000:          # ~3000 lines
                lines = f.read_text(encoding="utf-8").splitlines()[-KEEP:]
                f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def rows(since_s: float = 7 * 86400) -> list:
    try:
        lines = _file().read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    cut = time.time() - since_s
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("t", 0) >= cut:
            out.append(r)
    return out


def summary(since_s: float = 7 * 86400, recent: int = 30) -> dict:
    """Per provider and per purpose, plus the latest requests."""
    rs = rows(since_s)
    by = {}
    for r in rs:
        k = (r["provider"], r["label"])
        p = by.setdefault(k, {"provider": r["provider"], "label": r["label"],
                              "requests": 0, "failed": 0, "in": 0, "out": 0,
                              "usd": 0.0, "models": [], "purposes": {}})
        p["requests"] += 1
        p["failed"] += 0 if r.get("ok", True) else 1
        p["in"] += r.get("in", 0)
        p["out"] += r.get("out", 0)
        p["usd"] += r.get("usd", 0.0)
        if r.get("model") and r["model"] not in p["models"]:
            p["models"].append(r["model"])
        pu = p["purposes"].setdefault(r["purpose"], {"requests": 0, "usd": 0.0})
        pu["requests"] += 1
        pu["usd"] += r.get("usd", 0.0)
    provs = sorted(by.values(), key=lambda p: -p["requests"])
    for p in provs:
        p["usd"] = round(p["usd"], 4)
        for pu in p["purposes"].values():
            pu["usd"] = round(pu["usd"], 4)
    return {"days": round(since_s / 86400, 1), "providers": provs,
            "recent": list(reversed(rs[-recent:])), "purposes": PURPOSES}


def _selftest() -> int:
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as d:
        os.environ["AETHRON_HOME"] = d

        def ck(name, cond):
            nonlocal ok
            ok &= bool(cond)
            print(("PASS " if cond else "FAIL ") + name)

        ck("an address names its provider",
           provider_of("https://api.deepseek.com/v1")[0] == "deepseek"
           and provider_of("https://generativelanguage.googleapis.com/v1beta/"
                           "openai")[0] == "gemini"
           and provider_of("https://api.openai.com/v1/images")[0] == "openai")
        record("https://api.deepseek.com/v1", "deepseek-flash", "agent",
               1000, 50, 0.0002)
        record("https://generativelanguage.googleapis.com/v1beta/models/x",
               "gemini-2.5-flash-image", "picture", usd=0.04)
        record("https://generativelanguage.googleapis.com/v1beta", "e",
               "embedding", 10, 0, 0.0, free=True)
        s = summary()
        labels = [p["label"] for p in s["providers"]]
        ck("both providers are listed", "DeepSeek" in labels and "Gemini" in labels)
        g = next(p for p in s["providers"] if p["label"] == "Gemini")
        ck("Gemini's spend is the picture, by purpose",
           g["purposes"]["picture"]["usd"] == 0.04 and g["requests"] == 2)
        ck("a free request costs nothing", g["purposes"]["embedding"]["usd"] == 0)
        fields = {k for ln in _file().read_text().splitlines()
                  for k in json.loads(ln)}
        ck("only who, what for and how much is written",
           fields <= {"t", "provider", "label", "model", "purpose", "in",
                      "out", "usd", "free", "ok", "note"})
        os.environ["AETHRON_USAGE"] = "off"
        record("https://api.openai.com/v1", "m", "text")
        ck("can be switched off", "openai" not in _file().read_text())
        os.environ.pop("AETHRON_USAGE", None)
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(json.dumps(summary(), indent=1))
