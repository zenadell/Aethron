#!/usr/bin/env python3
"""Ember, the companion, inside the real studio window.

Starts a studio on a throwaway home and drives it in a browser:
the companion loads, stays out of automated browsers unless asked,
reacts to real signals, takes a press on itself without letting it reach
the page (and nothing else), refuses Savage for someone who is not an
admin, answers in a conversation even with no provider configured, and its
settings card renders. No key, no network.
"""
import os, socket, subprocess, sys, tempfile, time, json, urllib.request
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
SP = Path(tempfile.mkdtemp(prefix="ember-")); HOME = SP / "home"; (HOME / "projects").mkdir(parents=True)
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true", AETHRON_REPORTS="off")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE") or k == "AETHRON_EMBER_SAVAGE": env.pop(k)
srv = subprocess.Popen([sys.executable, "studio.py", str(PORT)], cwd=ROOT, env=env,
                       stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"
def launch(p):
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try: return p.chromium.launch(**kw)
        except Exception: pass
    print("VERDICT: SKIPPED — no browser playwright can start"); srv.terminate(); sys.exit(0)
try:
    for _ in range(60):
        try: urllib.request.urlopen(BASE + "/", timeout=2); break
        except Exception: time.sleep(.5)
    html = urllib.request.urlopen(BASE + "/").read().decode()
    import re
    m = re.search(r'/ember\.js\?v=\w+', html)
    check("the page asks for the companion by a versioned address", bool(m))
    js = urllib.request.urlopen(BASE + m.group(0)).read().decode() if m else ""
    check("the companion script is served", "window.ember=" in js)
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.add_init_script("try{localStorage.forge_tour='done'}catch(e){}")
        pg.goto(BASE + "/"); pg.wait_for_timeout(2500)
        check("an automated browser gets no companion", pg.evaluate("!!(window.ember&&ember.get().running)") is False
              and pg.evaluate("!document.getElementById('ember-cv')"))
        pg.goto(BASE + "/?ember=1"); pg.wait_for_timeout(3500)
        check("asked for, it runs", pg.evaluate("window.ember&&ember.get().running") is True)
        st = pg.evaluate("ember.state()")
        check("it sits inside the window", 0 < st["x"] < 1400 and 0 < st["y"] < 900, str(st))
        drawn = pg.evaluate("""(()=>{const c=document.getElementById('ember-cv'),x=c.getContext('2d');const s=ember.state(),d=Math.min(2,devicePixelRatio||1);
          const im=x.getImageData(Math.round((s.x-s.r)*d),Math.round((s.y-s.r)*d),Math.round(s.r*2*d),Math.round(s.r*2*d)).data;let n=0;for(let i=3;i<im.length;i+=4)if(im[i]>200)n++;return n})()""")
        check("it is drawn", drawn > 50, f"opaque pixels {drawn}")
        pg.evaluate("ember.signal('job',{project:'p',job:{status:'running',progress:{done:10,total:100},steps:[]}})"); pg.wait_for_timeout(600)
        check("a running job puts it to work", pg.evaluate("ember.state().mood") == "job")
        pg.evaluate("ember.signal('job',{project:'p',job:{status:'done',result:{ok:true,total:100,filled:100}}})"); pg.wait_for_timeout(1200)
        check("a finished, checked job is celebrated", pg.evaluate("ember.state().mood") == ""
              and pg.evaluate("ember.state().face") in ("joy", "smug", "surprised", "happy"), str(pg.evaluate("ember.state()")))
        pg.wait_for_timeout(4000)
        pg.screenshot(path=os.environ.get("EMBER_SHOT",str(SP/"home.png")))
        pg.evaluate("ember.signal('broke',{what:'about.html'})"); pg.wait_for_timeout(1500)
        b_ = pg.evaluate("ember.state().bubble")
        check("a real failure is named", "about.html" in b_, b_)
        pg.wait_for_timeout(6500)
        # a press on Ember never reaches the page, a press elsewhere does
        pg.evaluate("""(()=>{const s=ember.state();window.__hits=0;const d=document.createElement('button');d.id='under';
          d.style.cssText=`position:fixed;left:${s.x-30}px;top:${s.y-30}px;width:60px;height:60px;z-index:10`;d.onclick=()=>window.__hits++;document.body.appendChild(d);
          const e=document.createElement('button');e.id='away';e.style.cssText='position:fixed;left:10px;top:860px;width:30px;height:30px;z-index:10';e.onclick=()=>window.__hits+=100;document.body.appendChild(e)})()""")
        pg.wait_for_timeout(100)
        st = pg.evaluate("ember.state()")
        pg.mouse.click(st["x"], st["y"]); pg.wait_for_timeout(500)
        check("a click on Ember does not reach the button under it", pg.evaluate("window.__hits") == 0)
        check("and Ember reacts to it", pg.evaluate("ember.state().face") in ("surprised", "sideeye", "tfman", "joy", "glare", "shocked"),
              str(pg.evaluate("ember.state()")))
        pg.mouse.click(25, 875); pg.wait_for_timeout(200)
        check("a click anywhere else reaches the page", pg.evaluate("window.__hits") == 100)
        pg.evaluate("ember.set({att:'savage'})")
        check("Savage is refused for someone who is not an admin", pg.evaluate("ember.get().att") == "sassy")
        r = pg.evaluate("fetch('/api/ember/say',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({turns:[{role:'user',content:'hi'}],state:'',attitude:'sassy'})}).then(r=>r.status)")
        check("with no provider set up, the reply endpoint says no instead of hanging", r in (400, 401, 403, 500), str(r))
        pg.wait_for_timeout(2500)
        pg.evaluate("ember.talk()"); pg.wait_for_timeout(2600)
        check("talking opens its reply box", pg.evaluate("!document.getElementById('ember-talk').hidden"))
        pg.fill("#ember-in", "hi, my name is tim"); pg.press("#ember-in", "Enter"); pg.wait_for_timeout(2500)
        b_ = pg.evaluate("ember.state().bubble")
        check("it answers even with no provider", "Tim" in b_, b_)
        check("and remembers the name", pg.evaluate("ember.memory().name") == "Tim")
        pg.press("#ember-in", "Escape"); pg.wait_for_timeout(300)
        pg.evaluate("openSettings()"); pg.wait_for_timeout(900)
        card = pg.evaluate("(document.getElementById('embercard')||{}).innerText||''")
        check("Settings has a companion card", "companion" in card.lower() and "sassy" in card.lower(), card[:200])
        check("and it offers no Savage to someone who is not an admin", "Savage" not in card)
        pg.screenshot(path=str(SP/"settings.png"))
        pg.evaluate("emberSet('on',false)"); pg.wait_for_timeout(300)
        check("turning it off stops it", pg.evaluate("ember.get().running") is False)
        # the app hands its signals to the desktop Ember instead of drawing its own
        pg.evaluate("ember.set({on:true,desktop:true})"); pg.wait_for_timeout(300)
        check("with Ember on the desktop, the window draws none", pg.evaluate("ember.get().running") is False)
        with pg.expect_request(lambda r: "/api/ember/signal" in r.url and r.method == "POST", timeout=5000) as rq:
            pg.evaluate("ember.signal('pass',{project:'p'})")
        check("and sends its signals on", json.loads(rq.value.post_data or "{}").get("name") == "pass")
        pg.evaluate("ember.set({desktop:false})")
        # the desktop page itself
        d = b.new_page(viewport={"width": 1440, "height": 900})
        d.on("pageerror", lambda e: errs.append("desk: " + str(e)))
        d.goto(BASE + "/ember-desk"); d.wait_for_timeout(2500)
        check("the desktop page runs Ember", d.evaluate("ember.get().running && ember.get().desk") is True)
        bg = d.evaluate("getComputedStyle(document.body).backgroundColor")
        check("and is see-through", bg in ("rgba(0, 0, 0, 0)", "transparent"), bg)
        world = {"windows": [{"x": 200, "y": 160, "w": 900, "h": 600, "perch": True}],
                 "notch": {"x": 640, "y": 0, "w": 160, "h": 32, "notch": True},
                 "dock": {"x": 400, "y": 830, "w": 640, "h": 70, "side": "bottom"}, "menubar": 32, "fullscreen": False}
        far = d.evaluate("p=>ember.desk(p)", dict(world, mouse=[5, 5]))
        check("the pointer far away passes through to the desktop", far.get("hit") is False, str(far)+str(d.evaluate("ember.state()")))
        st = d.evaluate("ember.state()")
        near = d.evaluate("p=>ember.desk(p)", dict(world, mouse=[st["x"], st["y"]]))
        check("the pointer on Ember is Ember's", near.get("hit") is True)
        d.evaluate("p=>ember.desk(p)", dict(world, mouse=[5, 5], signals=[{"name": "broke", "data": {"what": "about.html"}}]))
        d.wait_for_timeout(1500)
        check("signals from the app reach the desktop Ember", "about.html" in d.evaluate("ember.state().bubble"))
        d.evaluate("p=>ember.desk(p)", dict(world, mouse=[5, 5], fullscreen=True)); d.wait_for_timeout(1200)
        check("it hides while something is full screen", d.evaluate("ember.state().alpha") < 0.1)
        d.evaluate("p=>ember.desk(p)", dict(world, mouse=[5, 5], fullscreen=False)); d.wait_for_timeout(1200)
        check("and comes back after", d.evaluate("ember.state().alpha") > 0.9)
        d.close()
        check("no page errors", not errs, "; ".join(errs[:3]))
        b.close()
finally:
    srv.terminate()
print(f"\n{OK} passed, {FAIL} failed")
print("VERDICT:", "PASS" if not FAIL else "FAIL")
sys.exit(1 if FAIL else 0)
