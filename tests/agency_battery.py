#!/usr/bin/env python3
"""Ember does things for you, inside the real studio window.

A throwaway home, a fake update feed serving a real (tiny) bundle, and
migration jobs written to disk the way the job runner writes them. Checks,
in a browser:
  * an update appears -> Ember downloads it by itself (safe mode), the chip
    shows "Restart", and Ember offers the restart; "Later" holds it back;
  * "Restart now" is pressed by Ember, visibly: the chip, then its button;
  * in "Only suggest" mode nothing downloads until the person says yes;
  * a migration that needs an answer is offered; yes opens the project and
    puts the cursor in the answer box;
  * in "Everything it can" mode a stopped migration is retried by itself,
    once;
  * the desktop hand-over and the world summary;
  * the setting is in Settings, and no page errors.
No key, no network beyond 127.0.0.1.
"""
import http.server, json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  ok   {name}")
    else: FAIL += 1; print(f"  FAIL {name} {extra}")
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("VERDICT: SKIPPED — playwright is not installed"); sys.exit(0)
if sys.platform == "darwin" or os.name == "nt":
    # the fake bundle is the Linux shape; a hand-made .app cannot be signed
    print("VERDICT: SKIPPED — the fake update bundle is built for Linux"); sys.exit(0)

SP = Path(tempfile.mkdtemp(prefix="agency-")); HOME = SP / "home"; (HOME / "projects").mkdir(parents=True)
# a tiny bundle of this system's shape, served by a fake feed
src = SP / "bundle" / "Aethron"; (src / "_internal").mkdir(parents=True); (src / "Aethron").write_text("NEW")
zp = SP / "app.zip"
with zipfile.ZipFile(zp, "w") as z:
    for f in src.parent.rglob("*"): z.write(f, str(f.relative_to(src.parent)))
FEED = {"version": "0.0.1"}            # not newer: no update until a test says so
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = zp.read_bytes() if self.path == "/app.zip" else json.dumps(FEED).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
fs = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=fs.serve_forever, daemon=True).start()
FBASE = f"http://127.0.0.1:{fs.server_address[1]}"

sys.path.insert(0, str(ROOT)); import aethron_news
aethron_news.mark_seen(HOME, [n["id"] for n in aethron_news.NEWS])   # no What's new card over the test
for name in ("demo", "other"):
    d = HOME / "projects" / name; d.mkdir()
    (d / "forge.json").write_text(json.dumps({"name": name, "platform": "webflow"}))
def job(project, **st):
    j = HOME / "jobs" / project; j.mkdir(parents=True, exist_ok=True)
    base = {"project": project, "status": "running", "step": "fill", "steps": {}, "created": time.time(),
            "heartbeat": time.time(), "pid": 999999}
    base.update(st); (j / "state.json").write_text(json.dumps(base))

s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true", AETHRON_REPORTS="off",
           AETHRON_UPDATE_FEED=FBASE + "/feed.json")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE"): env.pop(k)
srv = subprocess.Popen([sys.executable, "tests/dev_studio.py", str(PORT)], cwd=ROOT, env=env,
                       stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"
def api(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
def launch(p):
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try: return p.chromium.launch(**kw)
        except Exception: pass
    print("VERDICT: SKIPPED — no browser playwright can start"); srv.terminate(); sys.exit(0)
def wait(pg, js, secs):
    for _ in range(int(secs * 4)):
        try:
            if pg.evaluate(js): return True
        except Exception: pass
        pg.wait_for_timeout(250)
    return False
INIT = "try{localStorage.forge_tour='done';localStorage.ember_welcomed='1';%s}catch(e){}"
try:
    for _ in range(60):
        try: urllib.request.urlopen(BASE + "/", timeout=2); break
        except Exception: time.sleep(.5)
    with sync_playwright() as p:
        b = launch(p); errs = []
        def page(cfg="", watch=None):
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errs.append(str(e)))
            if watch is not None: pg.on("request", lambda r: watch.append(r.url))
            pg.add_init_script(INIT % cfg); pg.goto(BASE + "/?ember=1"); pg.wait_for_timeout(2500)
            pg.evaluate("ember.still(900)"); return pg

        print("── an update: Ember downloads it, then offers the restart")
        FEED.update(version="9.9.9", url=FBASE + "/app.zip")
        pg = page()
        check("Ember starts the download by itself", wait(pg, "ember.agency().log.some(x=>/started downloading/.test(x))", 15),
              str(pg.evaluate("ember.agency()")))
        check("and says so", wait(pg, "/downloading it for you/.test(ember.state().bubble)", 4), pg.evaluate("ember.state().bubble"))
        check("the download lands, checked, waiting for a restart", wait(pg, "UPD&&UPD.staged==='9.9.9'", 30)
              and json.loads((HOME / "updates" / "staged.json").read_text()).get("version") == "9.9.9")
        check("nothing was installed", (src / "Aethron").read_text() == "NEW" and not (ROOT / "Aethron.old").exists())
        check("the chip now says Restart", wait(pg, "/Restart/.test(document.getElementById('updchip').innerText)", 5),
              pg.evaluate("document.getElementById('updchip').innerText"))
        check("Ember offers the restart", wait(pg, "(ember.agency().offer||{}).id==='restart:9.9.9'", 12), str(pg.evaluate("ember.agency()")))
        card = pg.evaluate("(()=>{const o=document.getElementById('ember-offer');return o&&!o.hidden?o.innerText:''})()")
        check("on a card with the version and a Restart now button", "9.9.9" in card and "Restart now" in card, card)
        pg.screenshot(path=str(SP / "offer.png"))
        pg.click("#ember-offer .no"); pg.wait_for_timeout(600)
        check("Later puts it away", pg.evaluate("!ember.agency().offer&&document.getElementById('ember-offer').hidden"))
        pg.evaluate("ember.agTick()"); pg.wait_for_timeout(5000); pg.evaluate("ember.agTick()"); pg.wait_for_timeout(500)
        check("and it does not come straight back", not pg.evaluate("ember.agency().offer"))
        pg.close()

        print("── yes: Ember presses the chip and its button")
        pg = page()
        pg.route("**/api/update/apply", lambda rt: rt.fulfill(json={"job": "fakejob"}))
        pg.route("**/api/job?id=fakejob", lambda rt: rt.fulfill(json={"done": True, "ok": True, "log": "installed 9.9.9 — restarting"}))
        check("a fresh window offers the restart straight away (the copy is already here)",
              wait(pg, "(ember.agency().offer||{}).id==='restart:9.9.9'", 15), str(pg.evaluate("ember.agency()")))
        check("without downloading again", not pg.evaluate("ember.agency().log.some(x=>/started downloading/.test(x))"))
        pg.evaluate("window.__pressed=[];document.addEventListener('click',e=>{const t=e.target.closest('button');if(t)__pressed.push(t.className+'|'+t.innerText)},true)")
        pg.click("#ember-offer .yes")
        check("it restarts", wait(pg, "/Updated to 9\\.9\\.9/.test(document.body.innerText)", 15), pg.evaluate("document.body.innerText.slice(0,200)"))
        pressed = pg.evaluate("window.__pressed||[]")
        check("by pressing the update chip and its Restart now button",
              any(x.split("|")[0].split()[:1] == ["uc"] for x in pressed) and any("Restart now" in x for x in pressed), str(pressed))
        pg.close()

        print("── Only suggest: nothing downloads until they say yes")
        import shutil; shutil.rmtree(HOME / "updates", ignore_errors=True)
        seen = []
        pg = page("localStorage.ember_cfg=JSON.stringify({agency:'ask'})", watch=seen)
        check("it offers the download instead", wait(pg, "(ember.agency().offer||{}).id==='download:9.9.9'", 15), str(pg.evaluate("ember.agency()")))
        check("and has downloaded nothing", not any("/api/update/prefetch" in u for u in seen))
        pg.click("#ember-offer .yes")
        check("yes starts the download", wait(pg, "UPD&&UPD.staged==='9.9.9'", 30))
        pg.close()
        FEED.clear(); FEED.update(version="0.0.1")

        print("── a migration that needs an answer")
        job("demo", status="running")
        pg = page()
        pg.evaluate("ember.agTick()"); pg.wait_for_timeout(1500)
        check("first sight of a running job is not news", not pg.evaluate("ember.agency().offer"))
        job("demo", status="waiting", step="plan", question="What is the brand called?", ask_kind="question")
        pg.evaluate("ember.agTick()")
        check("Ember offers to take them to the question", wait(pg, "/What is the brand called/.test((ember.agency().offer||{}).text||'')", 10),
              str(pg.evaluate("ember.agency()")))
        pg.click("#ember-offer .yes")
        check("yes opens the project", wait(pg, "S.cur==='demo'", 12), pg.evaluate("S.cur"))
        check("and puts the cursor in the answer box", wait(pg, "document.activeElement&&document.activeElement.id==='jobans-demo'", 12),
              pg.evaluate("document.activeElement&&document.activeElement.id"))
        pg.close()

        print("── Everything it can: a stopped migration is retried, once")
        job("other", status="running")
        starts = []
        pg = page("localStorage.ember_cfg=JSON.stringify({agency:'all'})")
        pg.route("**/api/job/start", lambda rt: (starts.append(rt.request.post_data), rt.fulfill(json={"project": "other", "status": "running", "steps": []})))
        pg.evaluate("ember.agTick()"); pg.wait_for_timeout(1200)
        job("other", status="failed", error="the provider timed out")
        pg.evaluate("ember.agTick()")
        check("it presses Try again by itself", wait(pg, "ember.agency().log.some(x=>/Try again on other/.test(x))", 15),
              str(pg.evaluate("ember.agency().log")))
        check("which starts the job again", len(starts) == 1, str(starts))
        job("other", status="failed", error="the provider timed out again", heartbeat=time.time() + 1)
        pg.evaluate("ember.agTick()"); pg.wait_for_timeout(3000)
        check("a second failure is offered, not retried again", len(starts) == 1
              and wait(pg, "/^stop:other/.test((ember.agency().offer||{}).id||'')||ember.agency().queue.some(i=>/^stop:other/.test(i))", 8),
              str(pg.evaluate("ember.agency()")))
        pg.close()

        print("── out of credit: Ember takes them to Settings")
        job("demo", status="done", finished=time.time(), result={"ok": True})   # nothing left waiting from above
        pg = page()
        pg.evaluate("ember.fromEvents([{type:'done',error:true,text:'The AI provider refused: Insufficient Balance (402)'}])")
        check("it offers to open Settings", wait(pg, "(ember.agency().offer||{}).id==='credit'", 12), str(pg.evaluate("ember.agency()")))
        pg.click("#ember-offer .yes")
        check("and yes opens them", wait(pg, "S.view==='settings'", 10), pg.evaluate("S.view"))
        pg.evaluate("ember.fromEvents([{type:'done',error:true,text:'connection reset by peer'}])"); pg.wait_for_timeout(6500); pg.evaluate("ember.agTick()")
        check("an ordinary error is not called a credit problem", not pg.evaluate("ember.agency().offer") and "credit" not in pg.evaluate("ember.agency().queue"))
        check("and a vague error is not offered as a fix", not any(i.startswith("fix:") for i in pg.evaluate("ember.agency().queue")))
        pg.evaluate("ember.signal('broke',{what:'about.html',gaps:['the browser check found 1 broken page (about.html)']})")
        check("a page the browser check found broken is offered as a fix", wait(pg, "/about\\.html broke/.test((ember.agency().offer||{}).text||'')", 12), str(pg.evaluate("ember.agency()")))
        pg.close()

        print("── an offer that no longer applies goes away")
        job("other", status="running")
        pg = page()
        pg.evaluate("ember.agTick()"); pg.wait_for_timeout(1200)
        job("other", status="waiting", question="Which logo should it use?", ask_kind="question")
        pg.evaluate("ember.agTick()")
        shown_ = wait(pg, "/Which logo/.test((ember.agency().offer||{}).text||'')", 12)
        job("other", status="running", heartbeat=time.time() + 2)      # answered somewhere else
        pg.evaluate("ember.agTick()")
        check("a question answered elsewhere takes its card away", shown_ and wait(pg, "!ember.agency().offer&&document.getElementById('ember-offer').hidden", 8),
              str(pg.evaluate("ember.agency()")))
        pg.close()

        print("── the desktop hand-over and the world")
        st, w = api("/api/ember/world")
        check("the world lists every project's migration", st == 200 and {j["project"] for j in w.get("jobs", [])} >= {"demo", "other"}, str(w)[:300])
        check("and the update waiting for a restart", w.get("update", {}).get("staged") == "9.9.9", str(w.get("update")))
        st, _ = api("/api/ember/relay", {"id": "answer", "project": "demo"})
        st2, inbox = api("/api/ember/inbox")
        check("a yes on the desktop is handed to the app window", st == 200 and any(i.get("kind") == "agency" and i.get("project") == "demo" for i in inbox.get("items", [])), str(inbox))
        st, _ = api("/api/ember/relay", {"id": "rm -rf", "project": "demo"})
        check("only known actions are handed over", st >= 400)

        print("── while Aethron is not in front: one notification, and the card waits")
        job("other", status="running", heartbeat=time.time())
        sent = []
        pg = page()
        pg.route("**/api/notify", lambda r: (sent.append(json.loads(r.request.post_data or "{}")),
                                             r.fulfill(status=200, content_type="application/json", body='{"ok":true,"how":"test"}')))
        pg.evaluate("ember.agTick()"); pg.wait_for_timeout(1200)
        pg.evaluate("document.hasFocus=()=>false")             # another app is in front
        job("other", status="waiting", question="Which colour should the buttons be?", ask_kind="question", heartbeat=time.time() + 3)
        pg.evaluate("ember.agTick(1)")
        for _ in range(12):                                     # offers wait out their few seconds first
            if any("Which colour" in (x.get("body") or "") for x in sent): break
            pg.wait_for_timeout(1000); pg.evaluate("ember.agTick(1)")
        check("an offer made while they are elsewhere goes out as a notification",
              any("Which colour" in (x.get("body") or "") for x in sent), str(sent))
        for _ in range(4): pg.evaluate("ember.agTick(1)"); pg.wait_for_timeout(400)
        check("once, not on every tick", sum("Which colour" in (x.get("body") or "") for x in sent) == 1, str(sent))
        check("with words the notification can show", sent and len(sent[0].get("body", "")) <= 400 and sent[0].get("key", "").startswith("ask:other"), str(sent[:1]))
        pg.evaluate("document.hasFocus=()=>true")             # they clicked it: Aethron is in front again
        check("and the offer is waiting when they come back", wait(pg, "/Which colour/.test((ember.agency().offer||{}).text||'')", 12), str(pg.evaluate("ember.agency()")))
        pg.close()

        sent.clear()
        job("other", status="running", heartbeat=time.time())
        pg = page("localStorage.ember_cfg=JSON.stringify({notify:'off'});")
        pg.route("**/api/notify", lambda r: (sent.append(1), r.fulfill(status=200, content_type="application/json", body='{"ok":true}')))
        pg.evaluate("ember.agTick()"); pg.wait_for_timeout(1200)
        pg.evaluate("document.hasFocus=()=>false")
        job("other", status="waiting", question="Which font?", ask_kind="question", heartbeat=time.time() + 3)
        for _ in range(6): pg.evaluate("ember.agTick(1)"); pg.wait_for_timeout(1200)
        made = pg.evaluate("/Which font/.test((ember.agency().offer||{}).text||'')||ember.agency().queue.some(i=>/^ask:other/.test(i))")
        check("switched off, nothing is sent (the offer itself was still made)", made and not sent, f"{made} {sent}")
        pg.evaluate("document.hasFocus=()=>true")
        pg.close()

        sent.clear()
        job("other", status="running", heartbeat=time.time())
        pg = page()
        pg.route("**/api/notify", lambda r: (sent.append(1), r.fulfill(status=200, content_type="application/json", body='{"ok":true}')))
        pg.evaluate("ember.agTick()"); pg.wait_for_timeout(1200)
        job("other", status="waiting", question="Which footer?", ask_kind="question", heartbeat=time.time() + 3)
        for _ in range(6): pg.evaluate("ember.agTick(1)"); pg.wait_for_timeout(1200)
        check("in front, Ember shows the card and sends nothing", not sent and wait(pg, "/Which footer/.test((ember.agency().offer||{}).text||'')", 8), str(sent))
        pg.close()
        job("other", status="done", finished=time.time(), result={"ok": True})

        st, r = api("/api/notify", {"title": "Aethron", "body": "a\x00test" + "x" * 500, "key": "battery"})
        check("the studio's notify answers honestly (sent, or why not)", st == 200 and (r.get("ok") or r.get("why")), str(r))

        print("── the setting")
        pg = page()
        pg.evaluate("openSettings()"); pg.wait_for_timeout(900)
        card = pg.evaluate("(document.getElementById('embercard')||{}).innerText||''")
        check("Settings says what Ember may do", "What Ember may do" in card and "Only suggest" in card and "Everything it can" in card, card[:300])
        check("and whether to tell you when you are elsewhere", "Tell me when" in card, card[:400])
        pg.evaluate("emberSet('agency','all')")
        check("and it sticks", pg.evaluate("ember.agency().level") == "all" and "all" in pg.evaluate("localStorage.ember_cfg"))
        pg.close()
        check("no page errors", not errs, "; ".join(errs[:3]))
        b.close()
finally:
    srv.terminate(); fs.shutdown()
print(f"\n{OK} passed, {FAIL} failed")
print("VERDICT:", "PASS" if not FAIL else "FAIL")
sys.exit(1 if FAIL else 0)
