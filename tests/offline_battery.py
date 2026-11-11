"""What the owner hit on 1.1.130/131 (2026-11-11):

  * offline (or a provider whose address cannot be reached) the chat said
    "Thinking… still working" forever: the bridge answered 502, the engine
    retried quietly, and the screen read "retrying" as alive;
  * while it waited, every poll rebuilt the whole conversation (the note's
    seconds count changed), so the entire chat blinked;
  * typed to Ember with no model, "how are you" was sent to Aethron's agent
    as a request; and Ember's line could claim work was done before it was;
  * switching the AI (the team's own vs people's keys) did not reach a
    session that was already open.

Real bridge, real studio, real browser, fake providers. No network, no key.
The browser half is SKIPPED without playwright — never PASS."""
import json, os, sys, time, socket, subprocess, tempfile, pathlib, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
for k in list(os.environ):
    if k.lower() in ("https_proxy", "http_proxy", "all_proxy"):
        os.environ.pop(k)
ok = True
def check(n, c, extra=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c or not extra else "   " + str(extra)[:300]), flush=True)

DEAD = "https://no-such-host.invalid/v1"

# 1. the bridge: a request that cannot be made ends with the truth
import aethron_bridge as B
srv, url = B.start(B.BridgeConfig(DEAD, "sk-x", "gpt-4.1-mini"))
body = json.dumps({"model": "x", "max_tokens": 10, "stream": True,
                   "messages": [{"role": "user", "content": "hi"}]}).encode()
codes = []
for i in range(4):
    try:
        urllib.request.urlopen(urllib.request.Request(url + "/v1/messages", body, {"content-type": "application/json"}), timeout=20)
        codes.append((200, ""))
    except urllib.error.HTTPError as e:
        codes.append((e.code, e.read().decode()))
check("an unreachable provider is tried a few times first", [c for c, _ in codes[:2]] == [502, 502], codes)
check("then every request gets a plain 'no connection', which the engine does not retry",
      all(c == 400 and "No connection" in t for c, t in codes[2:]), codes[2:])
check("and the bridge says offline, not retrying", B.status()["state"] == "offline", B.status())
check("a slow answer is not mistaken for no connection", not B.unreachable(TimeoutError("timed out")))
srv.shutdown()

# 2. Ember with no model: talk is answered, work is sent, nothing claims to be done
import aethron_ember as AE
r = AE.act_fallback("how are you doing today", [], "<urlopen error [Errno 8] nodename nor servname provided>")
check("small talk to Ember is answered, not sent to Aethron's agent", r["kind"] == "answer" and "internet" in r["say"], r)
r = AE.act_fallback("change the hero headline to Hello", [], "urlopen error")
check("a request for work is still sent", r["kind"] == "send" and "headline" in r["prompt"], r)
check("a line claiming the job is done is replaced before it is said",
      AE.honest_say("answer", "Done! I published your site.") != "Done! I published your site."
      and AE.honest_say("send", "I've fixed the header.") == "sending that to Aethron.")
check("an ordinary line is left alone", AE.honest_say("answer", "nice try. no.") == "nice try. no.")

# 3. the studio in a browser
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("VERDICT: SKIPPED — playwright is not installed (browser checks)"); sys.exit(0 if ok else 1)

HITS = {"A": 0, "B": 0}
def upstream(tag):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0); b = json.loads(self.rfile.read(n) or b"{}")
            HITS[tag] += 1
            txt = f"answer from {tag}"
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for c in [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": txt}}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}]:
                self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
    u = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=u.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{u.server_port}/v1"
UA, UB = upstream("A"), upstream("B")

SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-offline-")); HOME = SP / "home"; (HOME / "projects").mkdir(parents=True)
CFG = HOME / "aethron_config.json"
def use(base):
    CFG.write_text(json.dumps({"ai": {"provider": "openai", "model": "gpt-4.1-mini", "base_url": base,
                                      "api_key": "sk-test-123456789012345678901234"}}))
use(DEAD)
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true", AETHRON_REPORTS="off", NO_PROXY="*")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE"): env.pop(k)
st = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                      stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
def launch(p):
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try: return p.chromium.launch(**kw)
        except Exception: pass
    print("VERDICT: SKIPPED — no browser playwright can start"); st.terminate(); sys.exit(0 if ok else 1)
try:
    time.sleep(4)
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1500, "height": 900})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.add_init_script("try{localStorage.forge_tour='done';localStorage.ember_welcomed='1'}catch(e){}")
        pg.goto(f"http://127.0.0.1:{PORT}/"); pg.wait_for_selector("#npurl", timeout=20000); pg.wait_for_timeout(1500)
        pg.evaluate("()=>{const o=document.getElementById('newsov');o&&o.remove()}")
        pg.fill("#npurl", "say hello"); pg.press("#npurl", "Enter")
        pg.wait_for_function("()=>document.querySelector('#chatlog .turn.you')", timeout=20000)
        pg.evaluate("()=>{window.__you=document.querySelector('#chatlog .turn.you');window.__swaps=0;"
                    "new MutationObserver(m=>{for(const r of m)for(const n of r.removedNodes)if(n===window.__you)window.__swaps++})"
                    ".observe(document.getElementById('chatlog'),{childList:true})}")
        t0 = time.time(); ended = False
        while time.time() - t0 < 90:
            pg.wait_for_timeout(1000)
            if pg.evaluate("()=>!CODE.key&&CODE.events.some(e=>/No connection/.test(e.text||''))"):
                ended = True; break
        took = round(time.time() - t0)
        check("offline, the run ends by itself and says there is no connection", ended, f"{took}s")
        check("within half a minute, not after minutes of spinning", took <= 30, f"{took}s")
        check("the conversation was updated, never rebuilt, while it waited (no blinking)",
              pg.evaluate("()=>window.__swaps===0&&document.body.contains(window.__you)"))
        # the AI changes under an open conversation: the next message uses the new one
        use(UA)
        pg.fill("#npurl", "hello again"); pg.press("#npurl", "Enter")
        pg.wait_for_function("()=>CODE.events.some(e=>e.type==='done'&&/answer from A/.test(e.text||''))", timeout=60000)
        check("after the provider comes back, the next message is answered", HITS["A"] > 0)
        use(UB)
        pg.fill("#npurl", "and once more"); pg.press("#npurl", "Enter")
        try:
            pg.wait_for_function("()=>CODE.events.some(e=>e.type==='done'&&/answer from B/.test(e.text||''))", timeout=60000)
        except Exception:
            pass
        check("a change of AI reaches a conversation that was already open", HITS["B"] > 0, HITS)
        check("no script errors", not errs, errs[:3])
finally:
    st.terminate()
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
