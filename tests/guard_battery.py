"""DeepSeek "went quiet" and "$0.19 that DeepSeek never billed".

temple ran deepseek-flash. The screen kept saying "The model went quiet, so
Aethron reconnected" (try 2 of 2), and the footer read "$0.19 this
session" while DeepSeek's balance did not move. Three causes, all ours:

  1. the bridge's spend guard refused with a 429, and the agent engine
     treats 429 as "busy, ask again" — measured: silent retries for over
     eight minutes. The guard was only reset when the provider changed,
     so once it tripped, every later message and every reconnect was
     refused for the rest of the app's life.
  2. the footer showed the ENGINE's own cost, which prices every token as
     the engine's own model, not DeepSeek's.
  3. deepseek-flash was missing from the price table, so the bridge
     priced it at the dearest rate it knew; and cached prompt tokens
     (most of an agent's prompt) were billed at the full input rate.

  A  a tripped guard ends the turn at once, with its reason — not silence
  B  the next message is a new run: the guard is reset and it answers
  C  the cost a turn reports is the provider's price on the provider's
     own token counts, cached tokens at the cache rate
  D  repeated side calls are not taken for a loop
  E  a long silent think: the stream opens at once and pings while the
     provider works (raw bridge, no engine needed)
"""
import json, os, pathlib, shutil, sys, tempfile, threading, time, \
    urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import aethron_bridge as B

ok = True


def check(n, c, extra=""):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n + (f"   [{extra}]" if extra and not c else ""))


# ── pricing, no network ───────────────────────────────────────────────
check("deepseek-flash has its own price, not the dearest fallback",
      B.price_of("deepseek-flash") != B.UNKNOWN_PRICE)
want = (100_000 * 0.30 + 900_000 * 0.006 + 1000 * 1.20) / 1e6
got = B.cost_of("deepseek-flash", 1_000_000, 1000,
                B.cached_tokens({"prompt_cache_hit_tokens": 900_000}))
check("cached prompt tokens are priced at the cache rate",
      abs(got - want) < 1e-9, f"{got} vs {want}")
check("OpenAI's spelling of cached tokens is read too",
      B.cached_tokens({"prompt_tokens_details": {"cached_tokens": 7}}) == 7)
check("a model with no cache price gets no discount",
      abs(B.cost_of("gemini-3.8-flash", 1000, 0, 1000)
          - B.cost_of("gemini-3.8-flash", 1000, 0, 0)) < 1e-12)

THINK = {"s": 0.0}


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(
            int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not body.get("stream"):
            out = json.dumps({"choices": [{"index": 0, "message": {
                "role": "assistant", "content": "t"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        t0 = time.time()
        while time.time() - t0 < THINK["s"]:
            self.wfile.write(("data: " + json.dumps({"choices": [{"index": 0,
                "delta": {"reasoning_content": "hmm "}}]}) + "\n\n").encode())
            self.wfile.flush()
            time.sleep(0.3)
        for c in ({"choices": [{"index": 0, "delta": {"content": "FULL ANSWER"}}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   # under the engine's trim window, or it compacts first
                   "usage": {"prompt_tokens": 100_000, "completion_tokens": 1000,
                             "prompt_cache_hit_tokens": 90_000}}):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
threading.Thread(target=up.serve_forever, daemon=True).start()
UP = f"http://127.0.0.1:{up.server_address[1]}"
bsrv, burl = B.start(B.BridgeConfig(UP, "sk-fake", "deepseek-flash"))

# ── E: the raw stream, no engine ──────────────────────────────────────
B.reset_usage()
B.PING_EVERY, THINK["s"] = 1.0, 4.0
t0 = time.time()
r = urllib.request.urlopen(urllib.request.Request(
    burl + "/v1/messages", json.dumps({"model": "x", "max_tokens": 50,
        "stream": True, "messages": [{"role": "user", "content": "go"}]}
    ).encode(), {"Content-Type": "application/json"}), timeout=30)
first_at, events = None, []
for line in r:
    line = line.decode().strip()
    if line.startswith("event:"):
        if first_at is None:
            first_at = time.time() - t0
        events.append(line[6:].strip())
check("E: the message opens at once, before the model has said a word",
      events and events[0] == "message_start" and first_at < 2.0,
      f"{events[:2]} after {first_at}")
check("E: the bridge pings while the model thinks in silence",
      events.count("ping") >= 2, str(events))
check("E: the answer still arrives whole",
      events[-1] == "message_stop" and "content_block_delta" in events)
B.PING_EVERY, THINK["s"] = 10.0, 0.0

# ── A-D: the real engine ──────────────────────────────────────────────
if not shutil.which("claude"):
    print("SKIP the agent engine is not installed — A-D UNVERIFIED")
else:
    import aethron_code as C
    ws = pathlib.Path(tempfile.mkdtemp(prefix="ae-guard-ws-"))
    s = C.CodeSession(ws, {"provider": "custom", "base_url": burl,
                           "token": "aethron", "mode": "bridge",
                           "permission_policy": "automatic",
                           "aethron_tools": False, "isolate": True})

    def turn(text, limit=150):
        t0, said, done = time.time(), [], None
        s.send(text)
        for ev in s.events(timeout=limit, idle=limit):
            if ev["type"] == "text":
                said.append(ev["text"])
            elif ev["type"] == "done":
                done = ev
                break
        return done, "".join(said), time.time() - t0

    try:
        s.start()
        # D first, on a clean run: a healthy turn is not refused
        B.reset_usage()
        done, said, _ = turn("say something")
        check("D: a healthy turn with the engine's side calls is not refused",
              done and not done.get("error") and "FULL ANSWER" in said,
              str(done)[:200])
        cost = (done or {}).get("cost_usd", -1)
        check("C: the turn reports the provider's own price, not the engine's",
              done and abs(cost - B.USED["usd"]) < 1e-9 and 0 < cost < 0.2,
              f"{cost} vs bridge {B.USED['usd']}")
        # A: trip the guard as a long day would
        B.USED["stopped"] = "Aethron spend guard: TRIPPED FOR THE TEST."
        done, said, took = turn("and again")
        check("A: a tripped guard ends the turn quickly, not in silence",
              done is not None and took < 60, f"{took:.0f}s {str(done)[:160]}")
        check("A: and the person is told why",
              done and "spend guard" in (done.get("text") or "") + said,
              str(done)[:200])
        # B: the next message is a new run (studio resets it per message)
        B.reset_usage()
        done, said, _ = turn("one more")
        check("B: the next message is a new run and is answered",
              done and not done.get("error") and "FULL ANSWER" in said,
              str(done)[:200])
    finally:
        s.close()

src = (ROOT / "studio.py").read_text()
check("B: the screen's send starts a new run in the bridge",
      "br.reset_usage()" in src.split('"/api/code/send"', 1)[1][:3000])
bsrv.shutdown()
up.shutdown()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
