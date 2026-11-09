#!/usr/bin/env python3
"""Talking with Ember live, in the real studio window, against a pretend
Gemini Live (aethron_live.FakeLive). Chromium's fake microphone stands in
for the person. Checks:
  * double-tap -> one live conversation; the window's microphone streams
    16 kHz audio to the service, and the key never reaches the window;
  * what the person says shows as they say it; Ember's voice is queued
    and played as its pieces arrive, its mouth moves, and the body does
    what the tool said (a wave, on the right arm);
  * talking over Ember stops its voice at once;
  * "open demo" opens the project, pressed on screen;
  * "that's all" ends it, on both sides;
  * no key or Live voice off -> the old way, not a broken one;
  * no page errors.
No key, no network beyond 127.0.0.1.
"""
import json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  ok   {name}")
    else: FAIL += 1; print(f"  FAIL {name} {extra}")
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("VERDICT: SKIPPED — playwright is not installed"); sys.exit(0)

import aethron_live, aethron_news
fake = aethron_live.FakeLive()
SCRIPT = {"turn": 0}
def answer(fk, conn, m):
    ri = m.get("realtimeInput") or {}
    if not ri.get("audio") or SCRIPT["turn"]:
        return
    SCRIPT["turn"] = 1                      # the first thing the microphone sends starts the first turn
    fk.send(conn, {"serverContent": {"inputTranscription": {"text": "hey ember "}}})
    time.sleep(.3)
    fk.send(conn, {"serverContent": {"inputTranscription": {"text": "wave at me"}}})
    fk.send(conn, {"toolCall": {"functionCalls": [{"id": "w1", "name": "ember_behave", "args": {"reply_json": json.dumps({
        "kind": "answer", "say": "hi there",
        "plan": [{"steps": [{"t": 2.5, "arms": {"right": {"shoulder": 150, "elbow": 30}}, "face": "happy", "look": "viewer"}]}]})}}]}})
    for _ in range(12):
        fk.send(conn, {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000",
                                                                                  "data": aethron_live.FakeLive.tone(.2, level=.5)}}]}}})
        time.sleep(.05)
    fk.send(conn, {"serverContent": {"outputTranscription": {"text": "hi there, nice to see you"}}})
    fk.send(conn, {"serverContent": {"turnComplete": True}})
fake.on_message = answer

SP = Path(tempfile.mkdtemp(prefix="live-")); HOME = SP / "home"; (HOME / "projects").mkdir(parents=True)
aethron_news.mark_seen(HOME, [n["id"] for n in aethron_news.NEWS])
d = HOME / "projects" / "demo"; d.mkdir()
(d / "forge.json").write_text(json.dumps({"name": "demo", "platform": "webflow"}))
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true", AETHRON_REPORTS="off", AETHRON_USAGE="off",
           AETHRON_LIVE_URL=fake.url, GEMINI_API_KEY="test-key-123")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE"): env.pop(k)
srv = subprocess.Popen([sys.executable, "tests/dev_studio.py", str(PORT)], cwd=ROOT, env=env,
                       stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"
def api(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r: return json.loads(r.read() or b"{}")
def wait(pg, js, secs):
    for _ in range(int(secs * 5)):
        try:
            if pg.evaluate(js): return True
        except Exception: pass
        pg.wait_for_timeout(200)
    return False
INIT = "try{localStorage.forge_tour='done';localStorage.ember_welcomed='1';%s}catch(e){}"
ARGS = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"]
def launch(p):
    for kw in ({"args": ARGS}, {"args": ARGS, "executable_path": "/opt/pw-browsers/chromium"}):
        try: return p.chromium.launch(**kw)
        except Exception: pass
    print("VERDICT: SKIPPED — no browser playwright can start"); srv.terminate(); sys.exit(0)
try:
    for _ in range(60):
        try: urllib.request.urlopen(BASE + "/", timeout=2); break
        except Exception: time.sleep(.5)
    st = api("/api/ember/live")
    check("the studio says a live conversation is possible here", st.get("ok") and not st.get("native_mic"), str(st))
    with sync_playwright() as p:
        b = launch(p); errs = []; bodies = []
        ctx = b.new_context(viewport={"width": 1400, "height": 900}, permissions=["microphone"])
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        def keep(resp):
            if "/api/ember/live" in resp.url:
                try: bodies.append(resp.text())
                except Exception: pass
        pg.on("response", keep)
        NET = []
        T0 = time.time()
        pg.on("request", lambda r: NET.append((round(time.time() - T0, 2), "->", r.url.split("/api/")[-1][:60])) if "/api/ember/live" in r.url else None)
        pg.on("requestfinished", lambda r: NET.append((round(time.time() - T0, 2), "<-", r.url.split("/api/")[-1][:60])) if "/api/ember/live" in r.url else None)
        pg.on("requestfailed", lambda r: NET.append((round(time.time() - T0, 2), "XX", r.url.split("/api/")[-1][:60])) if "/api/ember/live" in r.url else None)
        pg.add_init_script(INIT % ""); pg.goto(BASE + "/?ember=1"); pg.wait_for_timeout(2500)
        pg.evaluate("ember.still(900)")
        wait(pg, "Array.isArray(S.projects)&&S.projects.some(p=>p.name==='demo')", 10)

        print("── double-tap: one live conversation")
        pg.evaluate("ember.converse()")
        check("it goes live, with this window's microphone", wait(pg, "ember.live().on&&ember.live().mode==='page'", 10), str(pg.evaluate("ember.live()")))
        check("the sound is switched on inside the tap", wait(pg, "ember.live().ctx==='running'", 5), pg.evaluate("ember.live().ctx"))
        got_audio = lambda: [m for m in fake.got if (m.get("realtimeInput") or {}).get("audio")]
        for _ in range(50):
            if got_audio(): break
            time.sleep(.2)
        a = got_audio()
        check("the microphone streams to the service as 16 kHz PCM", a and a[0]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000", str(len(a)))
        check("in pieces of about 100 ms", a and abs(len(__import__("base64").b64decode(a[0]["realtimeInput"]["audio"]["data"])) - 3200) <= 4)
        su = fake.setups[-1] if fake.setups else {}
        check("Ember's rules and what the window shows went into the setup", "STATE" in json.dumps(su) and "LIVE SPOKEN CONVERSATION" in json.dumps(su))

        print("── a turn: heard as they speak, answered as it is made")
        check("what they said shows while they say it", wait(pg, "/wave at me/.test(ember.state().bubble)", 8), pg.evaluate("ember.state().bubble"))
        check("Ember's voice is queued as its pieces arrive", wait(pg, "ember.live().chunks>=12", 8), str(pg.evaluate("ember.live()")))
        playing = wait(pg, "ember.live().playing", 4)
        check("and played straight away", playing, str(pg.evaluate("ember.live()")))
        check("its mouth and body talk while it does", wait(pg, "ember.state().talking", 3))
        check("the body does what the tool said: the right arm goes up", wait(pg, "ember.state().arms[1].sh>110", 6),
              str(pg.evaluate("ember.state().arms")))
        check("the caption is what Ember actually says", wait(pg, "/nice to see you/.test(ember.state().bubble)", 6), pg.evaluate("ember.state().bubble"))
        check("the pieces finish playing", wait(pg, "ember.live().played>=12&&!ember.live().playing", 8), str(pg.evaluate("ember.live()")))
        check("and it listens again by itself", wait(pg, "ember.voice().state==='listening'", 5), str(pg.evaluate("ember.voice()")))

        print("── talking over Ember")
        conn = fake.last()
        for _ in range(25):
            fake.send(conn, {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000",
                                                                                       "data": aethron_live.FakeLive.tone(.2, level=.4)}}]}}})
        check("a long answer is playing", wait(pg, "ember.live().playing&&ember.live().queued>5", 6), str(pg.evaluate("ember.live()")))
        t0 = time.time()
        fake.send(conn, {"serverContent": {"interrupted": True}})
        stopped = wait(pg, "!ember.live().playing&&ember.live().queued===0", 3)
        check("an interruption stops the voice at once", stopped and time.time() - t0 < 1.5, f"{time.time()-t0:.2f}s {pg.evaluate('ember.live()')}")
        check("and Ember listens", pg.evaluate("ember.voice().state") == "listening")

        print("── a request: Ember presses it on screen")
        fake.send(conn, {"serverContent": {"inputTranscription": {"text": "open the demo project"}}})
        fake.send(conn, {"toolCall": {"functionCalls": [{"id": "o1", "name": "ember_behave", "args": {"reply_json": json.dumps(
            {"kind": "action", "action": "open_project", "target": "demo", "say": "opening it"})}}]}})
        fake.send(conn, {"serverContent": {"turnComplete": True}})
        check("it opens the project", wait(pg, "S.cur==='demo'", 15), pg.evaluate("S.cur"))
        check("and answers the tool so Ember goes on", any(m.get("toolResponse", {}).get("functionResponses", [{}])[0].get("id") == "o1" for m in fake.got))
        check("the conversation is still going", pg.evaluate("ember.live().on"))

        print("── that's all")
        fake.send(conn, {"serverContent": {"inputTranscription": {"text": "that's all"}}})
        fake.send(conn, {"serverContent": {"turnComplete": True}})
        check("it ends when they say so", wait(pg, "!ember.live().on&&ember.live().ended==='bye'", 6), str(pg.evaluate("ember.live()")))
        for _ in range(15):
            if api("/api/ember/live").get("live") is None: break
            time.sleep(.2)
        check("on the server too", api("/api/ember/live").get("live") is None, str(api("/api/ember/live")))
        check("the microphone is let go", pg.evaluate("ember.live().on") is False)
        check("the key never reached the window", not any("test-key-123" in x for x in bodies) and "test-key-123" not in pg.content())

        print("── a new talk right after the last one is not ended by the old one")
        SCRIPT["turn"] = 1
        pg.evaluate("ember.converse()")
        live_again = wait(pg, "ember.live().on", 10)
        if not live_again:
            print("   (debug)", pg.evaluate("[ember.voice(),ember.voiceLog(),ember.state().act,ember.state().mood]"))
            for n in [x for x in NET if "audio" not in x[2]][-25:]: print("   (net)", n)
        old_id = (api("/api/ember/live").get("live") or {}).get("id")
        # end it and start the next 50 ms later, while the old poll is still waiting on the server
        pg.evaluate("ember.converse();setTimeout(()=>ember.converse(),50)")
        check("the second talk starts", wait(pg, "ember.live().on", 10), str(pg.evaluate("[ember.live(),ember.voice(),ember.voiceLog()]")))
        pg.wait_for_timeout(3000)
        new = api("/api/ember/live").get("live") or {}
        check("and is still going after the old one's last answer came back", pg.evaluate("ember.live().on") and new.get("live")
              and new.get("id") != old_id, f"{pg.evaluate('ember.live()')} {new} old={old_id}")
        with urllib.request.urlopen(f"{BASE}/api/ember/live/events?id={old_id}&wait=0", timeout=10) as r:
            stale = json.loads(r.read())
        check("a poll for the old talk is answered as over, not with the new one's events", stale.get("closed") and not stale.get("events"), str(stale)[:200])
        req = urllib.request.Request(BASE + "/api/ember/live/stop", data=json.dumps({"id": old_id}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        check("and stopping the old one does not stop the new one", (api("/api/ember/live").get("live") or {}).get("live") is True)

        print("── Esc ends a live conversation")
        pg.keyboard.press("Escape")
        check("Esc ends it", live_again and wait(pg, "!ember.live().on&&ember.live().ended==='esc'", 5), str(pg.evaluate("ember.live()")))

        print("── a double-tap while Ember's hands are busy is not dropped")
        busy = pg.evaluate("ember.perform({kind:'action',action:'settings',say:''});ember.voice().driving")
        pg.evaluate("ember.converse()")
        started = wait(pg, "ember.live().on", 10)
        check("Ember was in the middle of pressing Settings", busy, str(pg.evaluate("ember.voiceLog()")))
        check("and the talk starts anyway", started, str(pg.evaluate("[ember.live(),ember.voiceLog()]")))
        pg.keyboard.press("Escape"); wait(pg, "!ember.live().on", 5)
        pg.close()

        print("── Live voice off, or no key: the old way, not a broken one")
        pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.add_init_script(INIT % "localStorage.ember_cfg=JSON.stringify({liveMode:'groq'});")
        pg.goto(BASE + "/?ember=1"); pg.wait_for_timeout(2500); pg.evaluate("ember.still(900)")
        n0 = len(fake.setups)
        pg.evaluate("ember.converse()"); pg.wait_for_timeout(2500)
        check("with Live voice off nothing goes live", not pg.evaluate("ember.live().on") and len(fake.setups) == n0)
        pg.keyboard.press("Escape"); pg.close()
        pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route("**/api/ember/live", lambda r: r.fulfill(status=200, content_type="application/json",
                                                         body='{"ok":false,"why":"talking live needs a Gemini key"}'))
        pg.add_init_script(INIT % ""); pg.goto(BASE + "/?ember=1"); pg.wait_for_timeout(2500); pg.evaluate("ember.still(900)")
        pg.evaluate("ember.converse()"); pg.wait_for_timeout(2500)
        v = pg.evaluate("ember.voice()")
        check("with no Gemini key it falls back to listening the old way", not pg.evaluate("ember.live().on") and len(fake.setups) == n0
              and v["state"] in ("asking", "listening", "idle", "thinking"), str(v))
        pg.keyboard.press("Escape"); pg.close()
        check("no page errors", not errs, "; ".join(errs[:3]))
        b.close()
finally:
    srv.terminate(); fake.close()
print(f"\n{OK} passed, {FAIL} failed")
print("VERDICT:", "PASS" if not FAIL else "FAIL")
sys.exit(1 if FAIL else 0)
