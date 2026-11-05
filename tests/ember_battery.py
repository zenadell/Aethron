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
        pg.add_init_script("try{localStorage.forge_tour='done';localStorage.ember_welcomed='1'}catch(e){}")
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
        # ---- voice and hands: listen on a double-tap, then type and press
        v = b.new_page(viewport={"width": 1400, "height": 900})
        v.on("pageerror", lambda e: errs.append("voice: " + str(e)))
        v.add_init_script("try{localStorage.forge_tour='done';localStorage.ember_welcomed='1'}catch(e){}")
        v.goto(BASE + "/?ember=1"); v.wait_for_timeout(3500)
        v.evaluate("window.consoleSend=()=>{window.__sent=document.getElementById('npurl').value}")
        st = v.evaluate("ember.state()")
        v.mouse.dblclick(st["x"], st["y"]); v.wait_for_timeout(700)
        vs = v.evaluate("ember.voice().state")
        check("a double-tap on Ember starts listening", vs in ("asking", "listening", "idle") and v.evaluate("ember.state().mood") in ("listen", ""), vs)
        v.keyboard.press("Escape"); v.wait_for_timeout(500)
        check("Escape stops listening", v.evaluate("ember.voice().state") == "idle")
        v.wait_for_timeout(3500)
        v.evaluate("void ember.hear('um can you make the hero headline say hello world')")
        mid = ""; seen = []
        for _ in range(90):
            v.wait_for_timeout(100)
            mid = v.evaluate("document.getElementById('npurl').value")
            seen.append(len(mid))
            if 5 < len(mid) < 30: break
        check("what was said is typed into the chat box where you can watch",
              "Can you make the hero headline say hello world.".startswith(mid) and 0 < len(mid) < 47, repr(mid) + " seen " + str(seen[:12]))
        for _ in range(200):
            v.wait_for_timeout(100)
            if v.evaluate("window.__sent"): break
        check("then Ember presses Send with the whole request",
              v.evaluate("window.__sent") == "Can you make the hero headline say hello world.", repr(v.evaluate("window.__sent")))
        check("filler words are left out", "um" not in (v.evaluate("window.__sent") or "x"))
        pr = v.evaluate("ember.voice().press") or {}
        check("and it flew there to press it", pr and ((pr["x"]-pr["cx"])**2+(pr["y"]-pr["cy"])**2) ** .5 < 90, str(pr))
        v.wait_for_timeout(1500)
        # publish: Ember presses the app's own Publish button, then the sheet's
        v.evaluate("""(()=>{window.__pub=0;const r=document.createElement('button');r.id='pubrail';r.textContent='Publish';
          r.style.cssText='position:fixed;left:300px;top:20px;width:90px;height:30px;z-index:5';
          r.onclick=()=>{const o=document.createElement('div');o.id='pubov';o.style.cssText='position:fixed;left:500px;top:300px;z-index:5';
            o.innerHTML='<button onclick="pubGo()" style="width:120px;height:36px">Publish</button>';document.body.appendChild(o)};
          document.body.appendChild(r);window.pubGo=()=>{window.__pub++}})()""")
        v.evaluate("void ember.hear('publish it please')")
        for _ in range(200):
            v.wait_for_timeout(100)
            if v.evaluate("window.__pub"): break
        check("asked to publish, Ember presses Publish and then the sheet's Publish", v.evaluate("window.__pub") == 1)
        # ---- Ember presses the app's own controls instead of typing a request
        v.wait_for_timeout(2500)
        v.evaluate("""(()=>{window.__opened=0;try{S.projects.push({name:'chaka',platform:'framer'})}catch(e){}
          const d=document.createElement('div');d.className='pitem';d.style.cssText='position:fixed;left:30px;top:420px;width:220px;height:50px;z-index:6';
          d.innerHTML='<div class="pname">chaka</div>';d.onclick=()=>{window.__opened++};document.body.appendChild(d)})()""")
        v.evaluate("void ember.hear(\"can you open chaka's project\")")
        for _ in range(200):
            v.wait_for_timeout(100)
            if v.evaluate("window.__opened"): break
        check("'open chaka's project' opens the project, it is not typed", v.evaluate("window.__opened") == 1
              and "chaka" not in (v.evaluate("document.getElementById('npurl').value") or ""), str(v.evaluate("document.getElementById('npurl').value")))
        pr = v.evaluate("ember.voice().press") or {}
        check("and Ember went to the project to press it", pr and ((pr["x"]-pr["cx"])**2+(pr["y"]-pr["cy"])**2) ** .5 < 120, str(pr))
        v.wait_for_timeout(1500)
        v.evaluate("void ember.hear('go to settings')")
        for _ in range(120):
            v.wait_for_timeout(100)
            if v.evaluate("typeof S!=='undefined'&&S.view==='settings'"): break
        check("'go to settings' opens Settings", v.evaluate("typeof S!=='undefined'&&S.view==='settings'"))
        v.evaluate("goHome()"); v.wait_for_timeout(1800)
        # ---- typing happens where the letters appear
        v.evaluate("window.consoleSend=()=>{window.__sent=document.getElementById('npurl').value}; window.__sent=''")
        v.evaluate("void ember.hear('please make the hero headline say we build sites that people love to use every day')")
        near = None
        for _ in range(120):
            v.wait_for_timeout(100)
            n = v.evaluate("document.getElementById('npurl').value.length")
            if 15 < n < 60:
                near = v.evaluate("""(()=>{const t=document.getElementById('npurl').getBoundingClientRect(),s=ember.state();return {sx:s.x,sy:s.y,l:t.left,r:t.right,t:t.top}})()""")
                break
        check("while typing, Ember sits over the text box, not off at its end",
              bool(near) and near["l"] - 20 <= near["sx"] <= near["r"] + 20 and near["t"] - 140 <= near["sy"] <= near["t"] + 40, str(near))
        for _ in range(200):
            v.wait_for_timeout(100)
            if v.evaluate("window.__sent"): break
        v.wait_for_timeout(1500)
        # ---- what it says stays long enough to read
        v.evaluate("ember.perform({kind:'answer',say:'This is a longer line Ember says, long enough that a person needs a moment to read it all.'})")
        v.wait_for_timeout(5000)
        b_ = v.evaluate("ember.state().bubble") or ""
        check("a long line is still on screen after 5 seconds", "longer line" in b_, repr(b_))
        # ---- the reply box is styled and sits by Ember
        v.evaluate("ember.talk()"); v.wait_for_timeout(1800)
        tb = v.evaluate("""(()=>{const t=document.getElementById('ember-talk'),r=t.getBoundingClientRect(),s=ember.state(),cs=getComputedStyle(t);
          return {pos:cs.position,x:r.left+r.width/2,y:r.top,sx:s.x,sy:s.y,h:r.height,w:r.width,mic:!!t.querySelector('#ember-mic'),hidden:t.hidden}})()""")
        check("the reply box is styled (fixed, one pill) and has a mic", tb["pos"] == "fixed" and tb["h"] < 60 and tb["w"] > 200 and tb["mic"], str(tb))
        check("and it opens next to Ember, not at the top of the window",
              abs(tb["x"] - tb["sx"]) < 200 and abs(tb["y"] - tb["sy"]) < 200, str(tb))
        v.keyboard.press("Escape"); v.wait_for_timeout(600)
        r = v.evaluate("fetch('/api/ember/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:'rebuild it',relay:1})}).then(r=>r.json())")
        check("a spoken button needs no model", r.get("kind") == "action" and r.get("action") == "build", str(r))
        box = v.evaluate("fetch('/api/ember/inbox').then(r=>r.json())")
        check("a request heard on the desktop waits for the app window to type it", box.get("items") and box["items"][0].get("action") == "build", str(box))
        check("and is handed over once", v.evaluate("fetch('/api/ember/inbox').then(r=>r.json())").get("items") == [])
        vv = v.evaluate("fetch('/api/voice').then(r=>r.json())")
        check("off a Mac the microphone says so instead of pretending", vv.get("available") is (sys.platform == "darwin"), str(vv))
        v.wait_for_timeout(2500)
        v.evaluate("window.__sent=''; ember.signal('broke',{what:'about.html',gaps:['the browser check found 1 broken page (about.html)']})")
        v.wait_for_timeout(9000)
        st = v.evaluate("ember.state()")
        v.mouse.click(st["x"], st["y"], button="right"); v.wait_for_timeout(300)
        items = v.evaluate("[...document.querySelectorAll('#ember-menu button')].map(b=>b.textContent)")
        check("after a failure its menu offers to tell Aethron the fix", "Tell Aethron to fix this" in items, str(items))
        if "Tell Aethron to fix this" in items:
            v.evaluate("[...document.querySelectorAll('#ember-menu button')].find(b=>b.textContent==='Tell Aethron to fix this').click()")
            for _ in range(200):
                v.wait_for_timeout(100)
                if v.evaluate("window.__sent"): break
            sent = v.evaluate("window.__sent") or ""
            check("and the request names exactly what the check found", "about.html" in sent and sent.startswith("Fix what the last check found"), repr(sent))
        # ---- a reply is never waited on forever
        v.evaluate("""(()=>{window.__api0=window.api;window.api=(p,b)=>p==='/api/ember/say'?new Promise(()=>{}):window.__api0(p,b)})()""")
        v.evaluate("ember.talk()"); v.wait_for_timeout(2600)
        v.fill("#ember-in", "how is your day going"); v.press("#ember-in", "Enter")
        v.wait_for_timeout(26000)
        b_ = v.evaluate("ember.state().bubble") or ""
        check("a reply that never comes is answered anyway, not left loading", bool(b_.strip()), repr(b_))
        v.evaluate("void (window.api=window.__api0)"); v.keyboard.press("Escape"); v.wait_for_timeout(500)
        # ---- left alone, Ember goes about its own errands
        v.evaluate("ember.hurry()")
        for _ in range(90):
            v.wait_for_timeout(500)
            if sum((v.evaluate("ember.state().seen") or {}).values()) >= 2: break
        seen = v.evaluate("ember.state().seen") or {}
        check("left alone, Ember goes off and does errands of its own", sum(seen.values()) >= 2, str(seen))
        v.close()
        # ---- a first launch: Ember welcomes the person and shows them around
        w = b.new_page(viewport={"width": 1400, "height": 900})
        w.on("pageerror", lambda e: errs.append("welcome: " + str(e)))
        w.add_init_script("try{if(!sessionStorage.cleared){sessionStorage.cleared=1;localStorage.removeItem('ember_welcomed')}}catch(e){}")
        w.goto(BASE + "/?ember=1"); w.wait_for_timeout(4500)
        t = w.evaluate("ember.tour()")
        check("on a first launch Ember starts the welcome tour", t.get("on") is True, str(t))
        check("the old written tour does not run on top of it", w.evaluate("!document.querySelector('#tour.on')"))
        bar = w.evaluate("(()=>{const b=document.getElementById('ember-tour');return b&&!b.hidden?b.innerText:''})()")
        check("the tour shows its step and a Skip and Next", "Skip" in bar and ("Next" in bar or "Done" in bar), repr(bar))
        seen_said = set()
        for _ in range(12):
            if not w.evaluate("ember.tour().on"): break
            seen_said.add(w.evaluate("ember.state().bubble") or "")
            w.wait_for_timeout(1800)
            w.evaluate("(()=>{const n=document.querySelector('#ember-tour .nx');n&&n.click()})()")
            w.wait_for_timeout(2600)
        said = " ".join(seen_said)
        check("it introduces itself", "Ember" in said, said[:200])
        check("and explains the chat box and the projects", "Aethron" in said and ("project" in said.lower()), said[:300])
        check("the tour ends and is remembered", not w.evaluate("ember.tour().on") and w.evaluate("localStorage.ember_welcomed") == "1")
        w.reload(); w.wait_for_timeout(4000)
        check("it does not run again on the next launch", not w.evaluate("ember.tour().on"))
        w.evaluate("void ember.welcome(1)"); w.wait_for_timeout(3000)
        check("it can be replayed on request", w.evaluate("ember.tour().on") is True)
        w.evaluate("document.querySelector('#ember-tour .sk').click()"); w.wait_for_timeout(1500)
        check("Skip ends it at once", not w.evaluate("ember.tour().on"))
        w.close()
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
