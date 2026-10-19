"""A conversation survives a restart and an update — at Home and in the
project it built.

temple, after an update: "why does it clear the previous chat each time it
restarts or updates itself … making Aethron lose all track of things it
was doing before". Two faults, both measured here on the real studio and
the real agent engine with a fake model (no key, no credit):

  1. Home was never put back on screen ("the front door stays a front
     door"), and the first message after a restart then STARTED FRESH and
     dropped the record — so a migration begun at Home was forgotten on
     every restart.
  2. A project created from Home had no conversation of its own: opening
     it after a restart showed nothing, and "continue" there began from
     nothing, though everything the agent did was on disk.

  A  Home: say a word, restart the studio, Home shows the conversation
     and the agent, asked again, is given the word
  B  the project the Home agent worked on shows the same conversation
     after the restart, and continuing there gives the agent the word
  C  the control: a project that never had a conversation starts empty,
     and a fresh agent there is given none of it
  D  in a browser after the restart: Home and the project both draw the
     conversation (SKIPPED without playwright or a browser, never PASS)
"""
import json, os, pathlib, shutil, socket, subprocess, sys, tempfile, \
    threading, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import aethron_code as C

ok = True


def check(n, c):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n)


if not shutil.which("claude"):
    print("VERDICT: SKIPPED — the agent engine is not installed")
    sys.exit(0)

SEEN = []


def model(body):
    """Asked anything at Home the first time, it uses an Aethron tool on
    the project (that is what links the conversation to it); otherwise it
    just answers."""
    msgs = json.dumps(body.get("messages", []))
    SEEN.append(msgs)
    if "REMEMBER-ME" in msgs and "tool_result" not in msgs \
            and "What word" not in msgs:
        return [{"tool": "mcp__aethron__get_plan",
                 "input": {"project": PROJ}}]
    return [{"text": "noted"}]


up, UP = C.mock_provider(script=model)
threading.Thread(target=up.serve_forever, daemon=True).start()

SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-conv-"))
HOME = SP / "home"
(HOME / "projects").mkdir(parents=True)
work = SP / "work"
work.mkdir()
subprocess.run([sys.executable, str(ROOT / "forge.py"), "init",
                str(ROOT / "tests/pages/site.html")], cwd=work,
               capture_output=True, timeout=300)
src = next(work.iterdir())
PROJ = src.name
shutil.copytree(src, HOME / "projects" / PROJ)
shutil.move(str(src), HOME / "projects" / "untouched")
cfg = json.loads((HOME / "projects/untouched/forge.json").read_text())
cfg["name"] = "untouched"
(HOME / "projects/untouched/forge.json").write_text(json.dumps(cfg))

s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
BASE = f"http://127.0.0.1:{PORT}"
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE"):
        env.pop(k)
CFG = {"provider": "custom", "base_url": UP, "token": "mock",
       "model": "mock-1", "permission_policy": "automatic"}


def studio():
    p = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                         stdout=open(SP / "studio.log", "a"),
                         stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + "/api/projects", timeout=2)
            return p
        except Exception:
            time.sleep(.25)
    return p


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or b"{}")


def turn(where, text, fresh=False):
    r = api("/api/code/start", {**where, "fresh": fresh, "cfg": CFG})
    api("/api/code/send", {"key": r["key"], "text": text})
    deadline = time.time() + 90
    while time.time() < deadline:
        e = api("/api/code/events?key=" + urllib.request.quote(r["key"])
                + "&since=0")
        if any(x["type"] == "done" for x in e["events"]):
            return r, e["events"]
        time.sleep(.3)
    return r, []


def said(evs, word):
    return any(word in (e.get("text") or "") for e in evs)


srv = studio()
try:
    r1, evs = turn({"console": True}, "REMEMBER-ME: the word is PELICAN")
    check("the Home agent worked on the project with an Aethron tool",
          any(e["type"] == "tool_result" and e.get("ok") for e in evs))
finally:
    srv.terminate(); srv.wait(10)          # the app quits for its update

srv = studio()
try:
    h = api("/api/code/history?console=1")
    check("A: after the restart Home has its conversation back",
          not h.get("live") and said(h["events"], "PELICAN"))
    hp = api("/api/code/history?project=" + PROJ)
    check("B: the project the Home agent worked on has it too",
          said(hp["events"], "PELICAN"))
    hu = api("/api/code/history?project=untouched")
    check("C: a project nobody worked on stays empty", not hu["events"])

    del SEEN[:]
    r, _ = turn({"project": PROJ}, "What word did I give you?")
    check("B: continuing in the project, the agent is given the word",
          r.get("resumed") and any("PELICAN" in m for m in SEEN))
    del SEEN[:]
    r, _ = turn({"project": "untouched"}, "What word did I give you?",
                fresh=True)
    check("C: a fresh conversation is given none of it (the control)",
          SEEN and not any("PELICAN" in m for m in SEEN))
    del SEEN[:]
    r, _ = turn({"console": True}, "What word did I give you?")
    check("A: continuing at Home, the agent is given the word",
          r.get("resumed") and any("PELICAN" in m for m in SEEN))

    # ── D: what the person sees ──────────────────────────────────────
    srv.terminate(); srv.wait(10)          # and once more, cold
    srv = studio()
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        sync_playwright = None
    if not sync_playwright:
        print("SKIP playwright is not installed — D UNVERIFIED")
    else:
        with sync_playwright() as p:
            b = None
            for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
                try:
                    b = p.chromium.launch(**kw)
                    break
                except Exception:
                    pass
            if not b:
                print("SKIP no browser playwright can start — D UNVERIFIED")
            else:
                pg = b.new_page()
                pg.add_init_script(
                    "try{localStorage.forge_tour='done'}catch(e){}")
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.goto(BASE + "/")
                pg.wait_for_timeout(2500)
                home = pg.eval_on_selector(
                    "#chatlog", "e=>e.hidden?'':e.innerText") or ""
                check("D: Home draws the conversation after the restart",
                      "PELICAN" in home)
                pg.evaluate(f"select({json.dumps(PROJ)})")
                pg.wait_for_timeout(2500)
                pj = pg.eval_on_selector(
                    "#chatlog", "e=>e.hidden?'':e.innerText") or ""
                check("D: the project draws it too", "PELICAN" in pj)
                check("D: no page errors", not errs)
                # while a run of it is going, a screen re-attaching to it
                # shows the conversation before that run as well
                api("/api/code/start", {"project": PROJ, "cfg": CFG})
                pg.evaluate("select('untouched')")
                pg.wait_for_timeout(1500)
                pg.evaluate(f"select({json.dumps(PROJ)})")
                pg.wait_for_timeout(2500)
                pj = pg.eval_on_selector(
                    "#chatlog", "e=>e.hidden?'':e.innerText") or ""
                check("D: re-attached to a running session, the earlier "
                      "conversation is still shown", "PELICAN" in pj)
                b.close()
finally:
    srv.terminate(); srv.wait(10)
    up.shutdown()
    shutil.rmtree(SP, ignore_errors=True)

print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
