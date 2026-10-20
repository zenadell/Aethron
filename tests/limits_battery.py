"""Limits the person sets, a clock on every run, and where the time went.

temple: the $1 spend guard stopped long runs too early; they wanted to
choose the amount or switch it off, a session limit that resets after a
window (like a subscription's 5-hour limit) that can also be switched
off, a clock showing how long a run has been going, and to know why a run
took so long with no error on screen.

  A  the limits round-trip through settings; 0 is Unlimited for each
  B  the session window counts dollars or requests, refuses once reached
     (naming when it resets), and opens a new window when its time is up
  C  the bridge: the session limit refuses before the provider is asked,
     an Unlimited spend guard lifts the request cap with it, a set one
     still stops, and the bridge times the model and the waits
  D  a turn reports where its time went: tools as a union, not a sum
  E  the real studio and engine with a fake slow model: the Limits card
     saves, the clock runs while a run goes, the run ends with "Took …",
     a reached session limit is said in the chat, and a provider out of
     credit is said plainly (SKIPPED without the engine or a browser)
"""
import json, os, pathlib, shutil, socket, subprocess, sys, tempfile, \
    threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-limits-"))
HOME = SP / "home"
(HOME / "projects").mkdir(parents=True)
os.environ["AETHRON_HOME"] = str(HOME)
for k in [k for k in os.environ if k.startswith(("AETHRON_SUPABASE",
                                                 "AETHRON_WALLET"))]:
    os.environ.pop(k)
import aethron_brain as BR
import aethron_bridge as B

ok = True


def check(n, c, extra=""):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n + (f"   [{extra}]" if extra and not c else ""))


# ── A: settings ───────────────────────────────────────────────────────
L = BR.limits()
check("A: defaults — $1 spend guard, $5 per 5-hour window",
      L["run_usd"] == 1.0 and L["session_limit"] == 5.0
      and L["session_hours"] == 5.0 and L["session_unit"] == "usd", str(L))
L = BR.set_limits(run_usd=0, session_limit=0, wallet_usd=0)
check("A: all three can be set to Unlimited (0)",
      L["run_usd"] == 0 and L["session_limit"] == 0 and L["wallet_usd"] == 0, str(L))
check("A: an Unlimited ceiling never runs out",
      BR.wallet()["unlimited"] and BR.wallet()["left_usd"] == float("inf"))
check("A: an Unlimited spend guard reaches a running bridge at once",
      B.LIMITS["usd"] == float("inf"))
L = BR.set_limits(run_usd=3.5, session_limit=20, session_unit="requests",
                  session_hours=2, wallet_usd=10)
check("A: chosen amounts are kept",
      L == {"run_usd": 3.5, "session_limit": 20.0, "session_unit": "requests",
            "session_hours": 2.0, "wallet_usd": 10.0}, str(L))
check("A: the bridge takes the chosen amount", B.LIMITS["usd"] == 3.5)
cfg = json.loads((HOME / "aethron_config.json").read_text())
check("A: limits live in the person's own settings (survive updates)",
      cfg["ai"].get("budget_usd") == 3.5)

# ── B: the window ─────────────────────────────────────────────────────
BR.session_reset()
BR.set_limits(session_limit=1.0, session_unit="usd", session_hours=5)
t0 = 1_000_000.0
BR.session_add(0.4, 1, now=t0)
check("B: under the limit, nothing is refused", BR.session_check(now=t0 + 60) == "")
BR.session_add(0.7, 1, now=t0 + 120)
why = BR.session_check(now=t0 + 600)
check("B: reached, it refuses and says when it resets",
      "session limit" in why and "$1.10 of $1.00" in why and "resets at" in why
      and "4h 50m" in why, why)
check("B: and where to change it", "Settings → Limits" in why)
check("B: when the window ends, the next request opens a new one",
      BR.session_check(now=t0 + 5 * 3600 + 1) == ""
      and BR.session(now=t0 + 5 * 3600 + 1)["used_usd"] == 0)
BR.session_add(0.2, 1, now=t0 + 5 * 3600 + 5)
s = BR.session(now=t0 + 5 * 3600 + 10)
check("B: the new window starts with that request",
      abs(s["used_usd"] - 0.2) < 1e-9 and s["started"] == t0 + 5 * 3600 + 5, str(s))
BR.set_limits(session_limit=3, session_unit="requests")
BR.session_reset()
for i in range(3):
    BR.session_add(0.0, 1)
check("B: a request limit counts free requests too",
      "3 of 3 requests" in BR.session_check())
BR.set_limits(session_limit=0)
check("B: Unlimited never refuses", BR.session_check() == "")
BR.wallet_set(0)
BR.session_reset()
BR.set_limits(session_limit=1.0, session_unit="usd")
BR.wallet_spend(0.25)
check("B: paid side jobs (pictures, page writers) count toward the window",
      abs(BR.session()["used_usd"] - 0.25) < 1e-9)

# ── C: the bridge ─────────────────────────────────────────────────────
MODE = {"think": 0.0, "code": 200}


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(
            int(self.headers.get("Content-Length") or 0)) or b"{}")
        UP_SEEN.append(1)
        if MODE["code"] != 200:
            out = json.dumps({"error": {"message": "Insufficient Balance",
                                        "type": "unknown_error"}}).encode()
            self.send_response(MODE["code"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        time.sleep(MODE["think"])
        if not body.get("stream"):
            out = json.dumps({"choices": [{"index": 0, "message": {
                "role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for c in ({"choices": [{"index": 0, "delta": {"content": "DONE-WORK"}}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


UP_SEEN = []
up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
threading.Thread(target=up.serve_forever, daemon=True).start()
UP = f"http://127.0.0.1:{up.server_address[1]}"
bsrv, burl = B.start(B.BridgeConfig(UP, "sk-fake", "deepseek-flash"))


def ask(i):
    req = urllib.request.Request(burl + "/v1/messages", json.dumps(
        {"model": "x", "max_tokens": 20,
         "messages": [{"role": "user", "content": f"q{i}"}]}).encode(),
        {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


BR.session_reset()
BR.set_limits(session_limit=2, session_unit="requests", run_usd=0)
B.reset_usage()
codes = [ask(i)[0] for i in range(2)]
n0 = len(UP_SEEN)
code, text = ask(3)
check("C: the session limit refuses the request after the limit",
      codes == [200, 200] and code == 400 and "session limit" in text, f"{codes} {code} {text[:160]}")
check("C: ...before the provider is asked (nothing spent)", len(UP_SEEN) == n0)
BR.session_reset()
check("C: a new window lets the next request through", ask(4)[0] == 200)

BR.set_limits(session_limit=0, run_usd=0)
B.set_limits(requests=1)
B.reset_usage()
codes = [ask(i)[0] for i in range(4)]
check("C: an Unlimited spend guard lifts the request cap with it",
      codes == [200] * 4, str(codes))
BR.set_limits(run_usd=1)
B.reset_usage()
codes = [ask(i)[0] for i in range(3)]
check("C: a set spend guard still stops at its caps",
      codes[0] == 200 and codes[-1] == 400, str(codes))
B.set_limits(requests=600)

B.reset_usage()
MODE["think"] = 1.2
ask(9)
MODE["think"] = 0.0
t = B.timing()
check("C: the bridge times the wait for the model",
      t["answers"] == 1 and 1.1 <= t["model_s"] <= 5 and t["slowest_s"] >= 1.1, str(t))

# ── D: where a turn's time went ───────────────────────────────────────
import aethron_code as C
fake = C.CodeSession.__new__(C.CodeSession)
now = time.time()
fake.turn_t0, fake.turn_from = now - 100, 0
fake._events = [
    {"type": "tool", "id": "a", "name": "mcp__aethron__build", "t": now - 90},
    {"type": "tool", "id": "b", "name": "mcp__aethron__probe", "t": now - 85},
    {"type": "tool_result", "id": "a", "t": now - 60},
    {"type": "tool_result", "id": "b", "t": now - 50},
    {"type": "tool", "id": "c", "name": "Read", "t": now - 30},
    {"type": "tool_result", "id": "c", "t": now - 25}]
tt = fake.turn_timing()
check("D: parallel steps are counted once (a union, not a sum)",
      abs(tt["tools_s"] - 45) < 0.5 and tt["tool_calls"] == 3, str(tt))
check("D: the longest step is named",
      tt["longest_tool"] == "mcp__aethron__probe" and abs(tt["longest_tool_s"] - 35) < .5)
check("D: and the total is the whole turn", 99 <= tt["total_s"] <= 102)

# ── E: the real studio, engine and browser ────────────────────────────
src = (ROOT / "studio.py").read_text()
check("E: the run clock and the timing line are on the page",
      "function runClock" in src and "function timingText" in src
      and 'id="limitscard"' in src)


def skip(why):
    print("SKIP " + why + " — E UNVERIFIED")


try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None
if not shutil.which("claude"):
    skip("the agent engine is not installed")
elif not sync_playwright:
    skip("playwright is not installed")
else:
    bsrv.shutdown()
    (HOME / "aethron_config.json").write_text(json.dumps({"ai": {
        "provider": "custom", "base_url": UP, "api_key": "sk-fake",
        "model": "deepseek-flash", "budget_usd": 1.0,
        "session_limit": 0, "session_unit": "usd", "session_hours": 5}}))
    BR.session_reset()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
    BASE = f"http://127.0.0.1:{PORT}"
    env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true")
    srv = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                           stdout=open(SP / "studio.log", "w"),
                           stderr=subprocess.STDOUT)

    def api(path, body=None):
        req = urllib.request.Request(
            BASE + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}")

    try:
        for _ in range(80):
            try:
                api("/api/limits")
                break
            except Exception:
                time.sleep(.25)
        r = api("/api/limits")
        check("E: /api/limits reports the limits and the window",
              r["run_usd"] == 1.0 and r["session_limit"] == 0
              and "session" in r and "wallet" in r, str(r)[:200])

        def launch(p):
            for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
                try:
                    return p.chromium.launch(**kw)
                except Exception:
                    pass
            return None

        with sync_playwright() as p:
            b = launch(p)
            if not b:
                skip("no browser playwright can start")
            else:
                pg = b.new_page(viewport={"width": 1400, "height": 900})
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.add_init_script("try{localStorage.forge_tour='done'}catch(e){}")
                pg.goto(BASE + "/")
                pg.wait_for_timeout(2500)
                pg.evaluate("openSettings()")
                pg.wait_for_selector("#limitsbox .limrow", timeout=10000)
                check("E: Settings has the Limits card with Unlimited switches",
                      pg.locator("#lrunu").count() == 1
                      and pg.locator("#lsesu").count() == 1
                      and pg.locator("#lwalu").count() == 1)
                pg.check("#lrunu")
                pg.uncheck("#lsesu") if pg.is_checked("#lsesu") else None
                pg.fill("#lses", "7")
                pg.select_option("#lsesunit", "requests")
                pg.fill("#lhrs", "3")
                pg.click("text=Save limits")
                pg.wait_for_timeout(800)
                r = api("/api/limits")
                check("E: saving from the card stores Unlimited and the window",
                      r["run_usd"] == 0 and r["session_limit"] == 7
                      and r["session_unit"] == "requests" and r["session_hours"] == 3,
                      str({k: r[k] for k in ("run_usd", "session_limit",
                                             "session_unit", "session_hours")}))
                # a slow run from Home: the clock must tick
                MODE["think"] = 4.0
                pg.evaluate("newProject()")
                pg.wait_for_timeout(1500)
                pg.fill("#npurl", "do the work")
                pg.keyboard.press("Enter")
                pg.wait_for_selector(".nowline .runclock", timeout=60000)
                pg.wait_for_function("(document.querySelector('.nowline .runclock')||{}).textContent", timeout=30000)
                a = pg.inner_text(".nowline .runclock")
                pg.wait_for_timeout(2200)
                bb = pg.inner_text(".nowline .runclock") if pg.locator(".nowline .runclock").count() else ""
                check("E: the clock shows and ticks while the run goes",
                      a and bb and a != bb, f"{a!r} -> {bb!r}")
                pg.wait_for_selector(".turn.sys.timing", timeout=120000)
                tl = pg.inner_text(".turn.sys.timing")
                check("E: the run ends saying how long it took and where",
                      tl.startswith("Took") and "waiting for the model" in tl, tl)
                check("E: the clock stops with the run",
                      pg.locator(".nowline").count() == 0)
                MODE["think"] = 0.0
                # session limit reached
                # the run above already used this window's requests; a
                # limit of 1 is therefore reached before the next message
                api("/api/limits", {"session_limit": 1, "session_unit": "requests"})
                api("/api/limits", {})
                pg.fill("#npurl", "and more")
                pg.keyboard.press("Enter")
                try:
                    pg.wait_for_function("document.querySelector('#chatlog').innerText.includes('session limit')",
                                         timeout=120000)
                except Exception:
                    pass
                txt = pg.inner_text("#chatlog")
                check("E: a reached session limit is said in the chat, with when it resets",
                      "session limit" in txt and "resets at" in txt, txt[-200:] + " || " + json.dumps(api("/api/limits")))
                # provider out of credit
                api("/api/limits", {"session_limit": 0})
                MODE["code"] = 402
                n_before = pg.locator(".turn.sys.bad").count()
                pg.wait_for_timeout(1000)
                pg.fill("#npurl", "once more")
                pg.keyboard.press("Enter")
                try:
                    pg.wait_for_function("document.querySelector('#chatlog').innerText.includes('out of credit')",
                                         timeout=180000)
                    got = True
                except Exception:
                    got = False
                check("E: a provider out of credit is said plainly, in its own words",
                      got and "Insufficient Balance" in pg.content(),
                      pg.inner_text("#chatlog")[-300:])
                MODE["code"] = 200
                check("E: no page errors", not errs, "; ".join(errs[:3]))
                b.close()
    finally:
        srv.terminate()
        srv.wait(10)

up.shutdown()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
