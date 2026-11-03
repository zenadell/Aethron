"""A provider that goes quiet is retried, and the run carries on.

The owner's run on gpt-4.1-mini stopped after 92 steps with "The provider
did not respond after 2½ minutes" — and it was not the first time. The
cause was ours: when an answer did not come, the agent engine asked again
on its own, but between two tries the bridge held no request, so the
screen measured silence from the last thing the agent said (by the second
try, 3 minutes ago) and stopped a run that was in the middle of
recovering. And an answer that stalled half-way was FINISHED, with
"[bridge: upstream stream failed]" as its text, which ended the turn.

  A  the real engine through the real bridge into a provider that hangs
     twice: the engine asks again, the bridge says "retrying" between
     tries, and the turn finishes with the real answer
  B  the same provider stalling half-way through an answer: the half is
     dropped, the engine asks again, the turn finishes with the full one
  C  the screen, in a browser: it waits through "retrying"; when the
     whole connection is silent it reconnects on the same conversation
     (twice), and only then stops and says what it tried
     (SKIPPED without playwright or a browser, never PASS)
"""
import json, os, pathlib, shutil, socket, subprocess, sys, tempfile, \
    threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import aethron_bridge as B
import aethron_code as C

ok = True


def check(n, c):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n)


B.UPSTREAM_TIMEOUT = 3.0
PLAN = {"hang": 0, "mid": False}
SEEN = []


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(
            int(self.headers.get("Content-Length") or 0)) or b"{}")
        SEEN.append(body)
        if not body.get("stream"):          # the engine's side calls
            out = json.dumps({"choices": [{"index": 0, "message": {
                "role": "assistant", "content": "t"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        if PLAN["hang"] > 0:
            PLAN["hang"] -= 1
            if PLAN["mid"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(("data: " + json.dumps({"choices": [{
                    "index": 0, "delta": {"content": "HALF-ANSWER "}}]})
                    + "\n\n").encode())
                self.wfile.flush()
            time.sleep(B.UPSTREAM_TIMEOUT + 2)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for c in ({"choices": [{"index": 0, "delta": {"content": "FULL ANSWER"}}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 2}}):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def turn(hang, mid):
    PLAN.update(hang=hang, mid=mid)
    del SEEN[:]
    up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    bsrv, burl = B.start(B.BridgeConfig(
        f"http://127.0.0.1:{up.server_address[1]}", "sk-fake", "gpt-4.1-mini"))
    states, stop = [], []

    def watch():
        while not stop:
            states.append(B.status())
            time.sleep(0.2)
    threading.Thread(target=watch, daemon=True).start()
    ws = pathlib.Path(tempfile.mkdtemp(prefix="ae-stall-ws-"))
    s = C.CodeSession(ws, {"provider": "custom", "base_url": burl,
                           "token": "aethron", "mode": "bridge",
                           "permission_policy": "automatic",
                           "aethron_tools": False, "isolate": True})
    text, err, done = [], "", False
    try:
        s.start()
        s.send("say something")
        for ev in s.events(timeout=120, idle=100):
            if ev["type"] == "text":
                text.append(ev["text"])
            elif ev["type"] == "done":
                done, err = True, (ev["text"] if ev.get("error") else "")
                break
    finally:
        stop.append(1)
        s.close()
        bsrv.shutdown()
        up.shutdown()
    return "".join(text), err, done, states


if shutil.which("claude"):
    said, err, done, states = turn(2, False)
    streams = [b for b in SEEN if b.get("stream")]
    check("A: a provider that does not answer is asked again (3 tries)",
          len(streams) >= 3)
    check("A: between tries the bridge says it is retrying, with the count",
          any(st["state"] == "retrying" and st["inflight"] == 0
              and "so far" in st["detail"] for st in states))
    check("A: the turn finishes with the real answer",
          done and not err and "FULL ANSWER" in said)
    check("A: an answer resets the failure count", B.STATUS["fails"] == 0)
    said, err, done, states = turn(1, True)
    check("B: an answer that stalls half-way is not handed over as finished",
          "HALF-ANSWER" not in said and "upstream stream failed" not in said)
    check("B: the engine asks again and gets the full answer",
          done and not err and "FULL ANSWER" in said
          and len([b for b in SEEN if b.get("stream")]) >= 2)
else:
    print("SKIP the agent engine is not installed — A and B UNVERIFIED")

# ── C: the screen ─────────────────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None
if not sync_playwright:
    print("SKIP playwright is not installed — C UNVERIFIED")
else:
    SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-stall-"))
    HOME = SP / "home"
    (HOME / "projects").mkdir(parents=True)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
    env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true")
    for k in list(env):
        if k.startswith("AETHRON_SUPABASE"):
            env.pop(k)
    srv = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                           stdout=open(SP / "studio.log", "w"),
                           stderr=subprocess.STDOUT)
    # The api is replaced inside the page: the screen's decisions are what
    # is under test, not a provider. BR is what the bridge reports.
    MOCK = """
    window.__calls=[]; window.__BR={inflight:0,state:'idle',for_s:0,detail:''};
    window.__keys=1;
    window.api=async (path,body)=>{
      __calls.push([path.split('?')[0],body||null]);
      if(path.startsWith('/api/code/events'))
        return {events:[],n:CODE.since,running:true,bridge:__BR};
      if(path==='/api/code/start')return {key:'k'+(++__keys)};
      return {};
    };
    0;
    """
    SETUP = """
    CODE.key='k1';CODE.since=0;CODE.recoveries=0;CODE.slowNote='';
    CODE.events=[{type:'you',text:'finish the migration'},
      {type:'tool',id:'t1',name:'mcp__aethron__probe',input:{project:'x'}},
      {type:'tool_result',id:'t1',ok:true,text:'CLEAN'}];
    clearInterval(CODE.poll);CODE.poll=777;
    """
    try:
        time.sleep(4)
        with sync_playwright() as p:
            b = None
            for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
                try:
                    b = p.chromium.launch(**kw)
                    break
                except Exception:
                    pass
            if not b:
                print("SKIP no browser playwright can start — C UNVERIFIED")
            else:
                pg = b.new_page()
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.add_init_script("try{localStorage.forge_tour='done'}catch(e){}")
                pg.goto(f"http://127.0.0.1:{PORT}/")
                pg.wait_for_function("typeof pollCode==='function'", timeout=15000)
                pg.evaluate(MOCK)
                pg.evaluate(SETUP)

                def poll(br, quiet_ms):
                    pg.evaluate(f"""async()=>{{window.__BR={json.dumps(br)};
                        CODE.lastEv=Date.now()-{quiet_ms};
                        await pollCode(); await new Promise(r=>setTimeout(r,50));}}""")

                def calls(name):
                    return [c for c in pg.evaluate("__calls") if c[0] == name]

                # 3 minutes since the agent last spoke, but the bridge is
                # between two tries — exactly the owner's run
                poll({"inflight": 0, "state": "retrying", "for_s": 2,
                      "detail": "The provider did not answer (2 tries so far). "
                                "Aethron is asking again."}, 200000)
                check("C: a run between two tries is NOT stopped",
                      not calls("/api/code/stop")
                      and pg.evaluate("CODE.key") == "k1")
                check("C: and the screen says it is asking again",
                      "asking again" in pg.evaluate("CODE.slowNote"))
                poll({"inflight": 1, "state": "asking", "for_s": 40,
                      "detail": "Asking gpt-4.1-mini (key 1 of 1)."}, 400000)
                check("C: nor while a try is in flight",
                      not calls("/api/code/stop"))
                # now truly silent: nothing from the agent or the bridge
                poll({"inflight": 0, "state": "idle", "for_s": 200}, 200000)
                starts = calls("/api/code/start")
                sends = calls("/api/code/send")
                check("C: a silent connection is reconnected, not ended",
                      calls("/api/code/stop") and starts
                      and pg.evaluate("CODE.key") == "k2")
                check("C: on the same conversation (not a fresh one)",
                      starts and starts[0][1].get("fresh") is False)
                check("C: and the agent is told to carry on from where it was",
                      sends and "Continue the work now" in sends[-1][1].get("text", "")
                      and sends[-1][1].get("text", "").startswith("<aethron-resume>"))
                check("C: the person is told it reconnected",
                      any(e.get("type") == "recover"
                          for e in pg.evaluate("CODE.events")))
                poll({"inflight": 0, "state": "idle", "for_s": 200}, 200000)
                check("C: a second silence reconnects once more",
                      pg.evaluate("CODE.key") == "k3"
                      and len(calls("/api/code/start")) == 2)
                poll({"inflight": 0, "state": "idle", "for_s": 200}, 200000)
                evs = pg.evaluate("CODE.events")
                last = [e for e in evs if e.get("type") == "error"]
                check("C: after 2 reconnects the run stops, and says what it tried",
                      pg.evaluate("CODE.key") == ""
                      and len(calls("/api/code/start")) == 2
                      and last and "reconnected 2 times" in last[-1]["text"])
                check("C: no script errors", not errs)
                b.close()
    finally:
        srv.terminate()

print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
