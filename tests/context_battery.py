"""A run that grew past the model's limit and kept going.

temple's deepseek-flash run worked for 2h 32m and $8.56, then stopped on
"This model's maximum context length is 1048576 tokens. However, you
requested 1058475 tokens". The conversation was never trimmed:

  1. the bridge never told the engine how big a request was. The prompt
     size arrives at the END of a provider's stream; the bridge put it
     nowhere the engine reads, so the engine believed every request was
     0 tokens and never compacted. Every request carried the whole run
     (about $0.30 and minutes each, near the end).
  2. the engine also assumed a 1M window for its own model, so even with
     the size known it would have compacted only at the very wall.
  3. once over the wall, a provider's refusal ended the turn, and every
     later message sent the same oversized conversation to the same
     refusal.

  A  every provider's wording of "too long" is recognised
  B  the oldest long parts are cut to fit; no message is dropped, the
     newest are left whole, and an impossible fit is refused
  C  the prompt size reaches the engine at the end of a stream
  D  a refusal for size is asked again, trimmed; if it still cannot fit
     the engine is told in its own words
  E  the engine trims at a working window, whatever it inherited
  F  the real engine: it compacts once the size is known, and a refusal
     for size is survived
"""
import json, os, pathlib, shutil, sys, tempfile, threading, urllib.error, \
    urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["AETHRON_HOME"] = tempfile.mkdtemp(prefix="ae-ctx-home-")
import aethron_bridge as B
import aethron_code as C

ok = True


def check(n, c, extra=""):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n + (f"   [{extra}]" if extra and not c else ""))


# ── A ────────────────────────────────────────────────────────────────
DS = ("This model's maximum context length is 1048576 tokens. However, you "
      "requested 1058475 tokens (1042475 in the messages, 16000 in the "
      "completion). Please reduce the length of the messages or completion.")
check("A: DeepSeek's wording, numbers the right way round",
      B.too_long(DS) == (1058475, 1048576), str(B.too_long(DS)))
check("A: Gemini's wording",
      B.too_long("The input token count (1200000) exceeds the maximum number "
                 "of tokens allowed (1048576).") == (1200000, 1048576))
check("A: a wording with no numbers still counts",
      B.too_long('{"code":"context_length_exceeded"}', {"messages": []}))
check("A: other refusals are not taken for size",
      B.too_long("Invalid API key") is None
      and B.too_long("rate limit reached") is None)

# ── B ────────────────────────────────────────────────────────────────
msgs = [{"role": "system", "content": "rules " * 500}]
for i in range(6):
    msgs.append({"role": "user", "content": [{"type": "text",
                 "text": f"part {i} " + "lorem " * 8000}]})
    msgs.append({"role": "assistant", "content": None, "tool_calls": [{
        "id": f"c{i}", "type": "function",
        "function": {"name": "Read", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                 "content": "file " * 9000})
msgs.append({"role": "user", "content": "the newest request " * 50})
req = {"model": "m", "max_tokens": 16000, "messages": msgs}
whole = len(json.dumps(req))
asked = whole // 4
small = B.trim_to_fit(req, asked, asked // 3)
check("B: an oversized request is cut to fit",
      small and len(json.dumps(small)) // 4 < (asked // 3 - 16000),
      str(small and len(json.dumps(small))))
check("B: no message is dropped (every tool call keeps its result)",
      small and len(small["messages"]) == len(msgs)
      and [m.get("tool_call_id") for m in small["messages"]]
      == [m.get("tool_call_id") for m in msgs])
check("B: the newest message is left whole",
      small and small["messages"][-1] == msgs[-1])
check("B: the system rules are left whole",
      small and small["messages"][0] == msgs[0])
check("B: a cut part says it was cut",
      small and "cut by Aethron" in json.dumps(small["messages"][1]))
check("B: an impossible fit is refused, not faked",
      B.trim_to_fit(req, asked, 17000) is None)
check("B: the original request is left as it was",
      len(json.dumps(req)) == whole)


# ── C, D: the raw bridge against a fake provider ─────────────────────
MODE = {"limit": 10 ** 9}
SEEN = []


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(
            int(self.headers.get("Content-Length") or 0)) or b"{}")
        n = len(json.dumps(body))
        mt = int(body.get("max_tokens") or body.get("max_completion_tokens")
                 or 0)
        if n > MODE["limit"]:
            SEEN.append(("refused", len(body["messages"]), n))
            return self._send(400, {"error": {
                "type": "invalid_request_error", "message":
                f"This model's maximum context length is "
                f"{MODE['limit'] // 4 + mt} tokens. However, you requested "
                f"{n // 4 + mt} tokens ({n // 4} in the messages, {mt} in "
                f"the completion)."}})
        SEEN.append(("ok", len(body["messages"]), n))
        pt = MODE.get("pt") or n // 4
        if not body.get("stream"):
            return self._send(200, {"choices": [{"index": 0, "message": {
                "role": "assistant", "content": "SUMMARY of what came before."},
                "finish_reason": "stop"}],
                "usage": {"prompt_tokens": pt, "completion_tokens": 5}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for c in ({"choices": [{"index": 0, "delta": {"content": "ANSWER"}}]},
                  {"choices": [{"index": 0, "delta": {},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": pt, "completion_tokens": 10}}):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
threading.Thread(target=up.serve_forever, daemon=True).start()
UP = f"http://127.0.0.1:{up.server_address[1]}"
bsrv, burl = B.start(B.BridgeConfig(UP, "sk-fake", "deepseek-flash"))


def ask(body, stream=True):
    r = urllib.request.Request(burl + "/v1/messages", json.dumps(dict(
        body, stream=stream)).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


B.reset_usage()
MODE.update(limit=10 ** 9, pt=987654)
code, raw = ask({"model": "x", "max_tokens": 100,
                 "messages": [{"role": "user", "content": "hi"}]})
delta = [json.loads(l[5:]) for l in raw.splitlines()
         if l.startswith("data:") and '"message_delta"' in l]
check("C: the stream's end tells the engine the prompt size",
      code == 200 and delta and delta[0]["usage"].get("input_tokens") == 987654,
      str(delta)[:200])

big = {"model": "x", "max_tokens": 1000, "messages": []}
for i in range(5):
    big["messages"] += [
        {"role": "user", "content": f"part {i} " + "lorem " * 6000},
        {"role": "assistant", "content": "noted " * 3000}]
big["messages"].append({"role": "user", "content": "now the real question"})
size = len(json.dumps(B.to_openai(big, "deepseek-flash")))
B.reset_usage()
MODE.update(limit=size // 2, pt=None)
SEEN.clear()
code, raw = ask(big)
check("D: a refusal for size is asked again, trimmed, and answered",
      code == 200 and "ANSWER" in raw
      and [s[0] for s in SEEN] == ["refused", "ok"]
      and SEEN[1][2] < MODE["limit"], f"{code} {SEEN}")
check("D: the trimmed retry keeps every message",
      len(SEEN) == 2 and SEEN[0][1] == SEEN[1][1])
MODE.update(limit=2000)
SEEN.clear()
code, raw = ask(big)
check("D: when it cannot fit, the engine is told in its own words",
      code == 400 and "prompt is too long:" in raw
      and "tokens >" in raw and "maximum" in raw, f"{code} {raw[:200]}")
check("D: and the provider was not asked over and over",
      len(SEEN) == 1, str(SEEN))

# ── E ────────────────────────────────────────────────────────────────
os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "1000000"
os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "95"
env = C.CodeSession(pathlib.Path(tempfile.mkdtemp()), {
    "provider": "custom", "base_url": burl, "token": "aethron",
    "mode": "bridge"})._env()
check("E: the engine trims at a working window, not an inherited one",
      env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == str(C.COMPACT_WINDOW)
      and C.COMPACT_WINDOW <= 300_000, env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
check("E: and no inherited trim percentage moves it",
      "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in env)
del os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
del os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]

# ── F: the real engine ───────────────────────────────────────────────
if not shutil.which("claude"):
    print("SKIP the agent engine is not installed — F UNVERIFIED")
else:
    def session():
        s = C.CodeSession(pathlib.Path(tempfile.mkdtemp(prefix="ae-ctx-ws-")),
                          {"provider": "custom", "base_url": burl,
                           "token": "aethron", "mode": "bridge",
                           "permission_policy": "automatic",
                           "aethron_tools": False, "isolate": True})
        return s.start()

    def turn(s, text):
        s.send(text)
        for ev in s.events(timeout=180, idle=180):
            if ev["type"] == "done":
                return ev
        return None

    # F1: the size is known, so the engine compacts before the wall
    MODE.update(limit=10 ** 9, pt=int(C.COMPACT_WINDOW * 0.9))
    SEEN.clear()
    B.reset_usage()
    s = session()
    try:
        dones = [turn(s, w) for w in ("hello", "again", "third")]
    finally:
        s.close()
    most = max(x[1] for x in SEEN) if SEEN else 0
    check("F: every turn answered",
          all(d and not d.get("error") for d in dones),
          str([d and d.get("text") for d in dones]))
    check("F: the engine compacted once the prompt size was near its window",
          most < 9, f"largest request had {most} messages: {SEEN}")

    # F2: the provider refuses for size; the run goes on
    MODE.update(limit=230_000, pt=1000)
    SEEN.clear()
    B.reset_usage()
    s = session()
    try:
        dones = [turn(s, f"part {i}: " + "lorem ipsum dolor " * 2200)
                 for i in range(4)]
    finally:
        s.close()
    check("F: a provider refusal for size was met",
          any(x[0] == "refused" for x in SEEN), str(SEEN))
    check("F: and every turn still answered",
          all(d and not d.get("error") for d in dones),
          str([d and (d.get("text") or "")[:80] for d in dones]))

bsrv.shutdown()
up.shutdown()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
