#!/usr/bin/env python3
"""Ember's flame: its kinds, its colours, how it changes.

Starts a studio on a throwaway home and drives Ember in a real browser:
every kind of flame draws (and is told apart from the others ON SCREEN, by
the pixels, not by a setting), a change of kind flows through the fire
instead of cutting, the mood flame follows Ember's face, a spoken wish
("turn blue") is kept across a reload, the mind can try a flame on without
taking away the person's choice, the right-click menu and the Settings
swatches change it, another window follows, and with motion off the flame
holds still. No key, no network.
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request
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
SP = Path(tempfile.mkdtemp(prefix="flame-")); HOME = SP / "home"; (HOME / "projects").mkdir(parents=True)
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true", AETHRON_REPORTS="off")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE") or k == "AETHRON_EMBER_SAVAGE": env.pop(k)
srv = subprocess.Popen([sys.executable, "tests/dev_studio.py", str(PORT)], cwd=ROOT, env=env,
                       stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"
KINDS = ["classic", "blue", "arcane", "witch", "soul", "gold", "rose", "white", "inferno", "coal", "shadow", "prism"]
# the mean colour of the flame's own pixels, read off the canvas
MEAN = """(()=>{const c=document.getElementById('ember-cv'),x=c.getContext('2d'),s=ember.state(),d=Math.min(2,devicePixelRatio||1),r=s.r;
  const x0=Math.round((s.x-r*.95)*d),y0=Math.round((s.y-r*2)*d),w=Math.round(r*1.9*d),h=Math.round(r*2.9*d);
  const im=x.getImageData(x0,y0,w,h).data;let n=0,R=0,G=0,B=0;
  for(let i=0;i<im.length;i+=4)if(im[i+3]>200){n++;R+=im[i];G+=im[i+1];B+=im[i+2]}
  return n?[R/n,G/n,B/n,n]:[0,0,0,0]})()"""
def launch(p):
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try: return p.chromium.launch(**kw)
        except Exception: pass
    print("VERDICT: SKIPPED — no browser playwright can start"); srv.terminate(); sys.exit(0)
def dist(a, b): return sum((a[i] - b[i]) ** 2 for i in range(3)) ** .5
try:
    for _ in range(60):
        try: urllib.request.urlopen(BASE + "/", timeout=2); break
        except Exception: time.sleep(.5)
    with sync_playwright() as p:
        b = launch(p)
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        ctx.route("**/api/update/check", lambda rt: rt.fulfill(json={"available": False}))
        ctx.add_init_script("try{localStorage.forge_tour='done';localStorage.ember_welcomed='1'}catch(e){}")
        pg = ctx.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/?ember=1"); pg.wait_for_timeout(3500)
        pg.evaluate("ember.still(900)")
        check("it starts as the classic orange flame", pg.evaluate("ember.fire().kind") == "classic")
        m0 = pg.evaluate(MEAN)
        check("and is drawn warm: red over blue", m0[3] > 200 and m0[0] > m0[2] + 60, str(m0))
        kinds = pg.evaluate("ember.fireKinds().map(k=>k.id)")
        check("it offers twelve kinds of flame and the mood flame", kinds == KINDS + ["mood"], str(kinds))
        # every kind draws, and each looks like itself
        means, bad = {}, []
        for k in KINDS:
            pg.evaluate(f"ember.set({{fire:'{k}'}})"); pg.wait_for_timeout(1700)
            means[k] = pg.evaluate(MEAN)
            bad += pg.evaluate("ember.health().bad")   # real frames only: ember.frames() would run Ember's clock ahead
        check("every kind is drawn", all(m[3] > 200 for m in means.values()), str({k: m[3] for k, m in means.items()}))
        check("no kind needed a repair while it burned", not bad, str(bad[:4]))
        check("blue is blue on screen", means["blue"][2] > means["blue"][0] + 40, str(means["blue"]))
        check("witchfire is green on screen", means["witch"][1] > means["witch"][0] + 30 and means["witch"][1] > means["witch"][2], str(means["witch"]))
        check("arcane is violet on screen", means["arcane"][2] > means["arcane"][1] + 40 and means["arcane"][0] > means["arcane"][1], str(means["arcane"]))
        check("shadow is dark, coal is darker than classic",
              sum(means["shadow"][:3]) < sum(means["classic"][:3]) - 150 and sum(means["coal"][:3]) < sum(means["classic"][:3]), str((means["shadow"], means["coal"])))
        fixed = [k for k in KINDS if k != "prism"]
        close = [(a, c, round(dist(means[a], means[c]))) for i, a in enumerate(fixed) for c in fixed[i + 1:] if dist(means[a], means[c]) < 22]
        check("the eleven fixed kinds are told apart by their pixels", not close, str(close))
        pg.evaluate("ember.set({fire:'prism'})"); pg.wait_for_timeout(1500)
        p1 = pg.evaluate("ember.fire().stops[2]"); pg.wait_for_timeout(3000); p2 = pg.evaluate("ember.fire().stops[2]")
        check("prism keeps changing colour", dist(p1, p2) > 40, f"{p1} -> {p2}")
        # a change flows through the fire
        pg.evaluate("ember.set({fire:'classic'})"); pg.wait_for_timeout(2200)
        a = pg.evaluate("ember.fire().stops[2]")
        pg.evaluate("ember.set({fire:'blue'})"); pg.wait_for_timeout(250)
        mid = pg.evaluate("ember.fire()")
        tgt = mid["target"][2]
        check("a change blends: a moment later the flame is between the two",
              dist(mid["stops"][2], a) > 15 and dist(mid["stops"][2], tgt) > 15, f"{a} {mid['stops'][2]} {tgt}")
        check("and flares as it changes", mid["flare"] > .3, str(mid["flare"]))
        pg.wait_for_timeout(2500)
        check("then arrives", dist(pg.evaluate("ember.fire().stops[2]"), tgt) < 6)
        # the mood flame follows the face
        pg.evaluate("ember.set({fire:'mood'})"); pg.wait_for_timeout(5000)
        pg.evaluate("ember.play([{t:7,face:'glare'}])"); pg.wait_for_timeout(3200)
        k1 = pg.evaluate("ember.fire().kind")
        pg.wait_for_timeout(2500)
        pg.evaluate("ember.play([{t:9,face:'sad'}])"); pg.wait_for_timeout(6000)
        k2 = pg.evaluate("ember.fire().kind")
        check("the mood flame burns red when Ember is cross and blue when it is down", (k1, k2) == ("inferno", "blue"), f"{k1} {k2}")
        # a spoken wish is kept; a site request is not taken
        pg.evaluate("ember.set({fire:'classic'})")
        pg.evaluate("ember.hear('turn blue')"); pg.wait_for_timeout(4500)
        st = pg.evaluate("[ember.fire().kind, ember.get().fire, JSON.parse(localStorage.ember_cfg||'{}').fire]")
        check("\"turn blue\" turns it blue and keeps it", st == ["blue", "blue", "blue"], str(st))
        pg.evaluate("ember.hear('can you be a soul fire for 5 minutes')"); pg.wait_for_timeout(4500)
        st = pg.evaluate("[ember.fire().kind, ember.fire().over, ember.get().fire]")
        check("\"for 5 minutes\" is only for a while: the choice underneath stays", st == ["soul", "soul", "blue"], str(st))
        pg.evaluate("ember.set({fire:'blue'})")
        pg.reload(); pg.wait_for_timeout(3500); pg.evaluate("ember.still(900)")
        check("the flame is kept across a restart", pg.evaluate("ember.fire().kind") == "blue")
        # the mind may try a flame on; the person's choice stays theirs
        plans = [{"thought": "feeling mysterious", "mood": "mischief", "plan": [{"fire": "arcane", "for": 5}, {"say": "spooky."}], "new": [], "remember": ""}]
        pg.route("**/api/ember/mind", lambda rt: rt.fulfill(status=200, content_type="application/json", body=json.dumps(plans[0])))
        pg.evaluate("ember.think('test')"); pg.wait_for_timeout(3500)
        st = pg.evaluate("[ember.fire().kind, ember.fire().over, ember.get().fire]")
        check("the mind can try a flame on for a while", st == ["arcane", "arcane", "blue"], str(st))
        pg.unroute("**/api/ember/mind")
        # the right-click menu
        pg.evaluate("ember.set({fire:'blue'})"); pg.wait_for_timeout(300)
        stt = pg.evaluate("ember.state()")
        pg.mouse.click(stt["x"], stt["y"] - 4, button="right"); pg.wait_for_timeout(400)
        items = pg.evaluate("[...document.querySelectorAll('#ember-menu button')].map(b=>b.textContent)")
        check("the right-click menu offers to change the flame", "Change my flame" in items, str(items))
        pg.evaluate("[...document.querySelectorAll('#ember-menu button')].find(b=>b.textContent==='Change my flame').click()")
        pg.wait_for_timeout(600)
        st = pg.evaluate("[ember.fire().kind, ember.get().fire]")
        check("and changes it, for good", st[0] != "blue" and st[0] == st[1] and st[0] in KINDS, str(st))
        # Settings
        pg.evaluate("openSettings()"); pg.wait_for_timeout(1000)
        sw = pg.evaluate("[...document.querySelectorAll('#embercard .firesw')].map(b=>b.getAttribute('aria-label'))")
        check("Settings shows a swatch for each flame", len(sw) == 13 and "Shadow" in sw and "Mood" in sw, str(sw))
        pg.evaluate("document.querySelector('#embercard .firesw[aria-label=\"Shadow\"]').click()"); pg.wait_for_timeout(500)
        check("a swatch changes the flame and shows it chosen",
              pg.evaluate("ember.get().fire") == "shadow"
              and pg.evaluate("document.querySelector('#embercard .firesw[aria-label=\"Shadow\"]').getAttribute('aria-pressed')") == "true")
        # another window (the desktop Ember) follows a change made here
        other = ctx.new_page(); other.on("pageerror", lambda e: errs.append("other: " + str(e)))
        other.goto(BASE + "/ember-desk"); other.wait_for_timeout(2500)
        pg.evaluate("ember.set({fire:'witch'})"); other.wait_for_timeout(2000)
        check("another window follows the change", other.evaluate("ember.fire().kind") == "witch", other.evaluate("ember.fire().kind"))
        other.close()
        # the fire answers the work
        pg.evaluate("ember.signal('working',{tool:'build'})"); pg.wait_for_timeout(1800)
        check("it burns higher while Aethron works", pg.evaluate("ember.fire().stoke") > .5, str(pg.evaluate("ember.fire().stoke")))
        pg.evaluate("ember.signal('pass',{project:'p'})"); pg.wait_for_timeout(200)
        check("it flares when a check passes", pg.evaluate("ember.fire().flare") > .5)
        pg.wait_for_timeout(3000)
        pg.evaluate("ember.signal('broke',{what:'about.html'})"); pg.wait_for_timeout(200)
        check("and sputters when something breaks", pg.evaluate("ember.fire().sput") > .5)
        # a pointer swept past bends the flame like a draft, then it settles
        pg.evaluate("ember.still(900)"); pg.wait_for_timeout(1500)
        st = pg.evaluate("ember.state()"); y = st["y"] - st["r"] * 1.2
        pg.mouse.move(st["x"] - 160, y); pg.wait_for_timeout(120)
        drafts = []
        for i in range(1, 9):
            pg.mouse.move(st["x"] - 160 + i * 40, y); pg.wait_for_timeout(16)
            drafts.append(pg.evaluate("ember.fire().draft"))
        check("a pointer swept past bends the flame the way it went", max(drafts) > .08, str([round(d, 2) for d in drafts]))
        pg.mouse.move(5, 5); pg.wait_for_timeout(2500)
        check("and the flame settles back", abs(pg.evaluate("ember.fire().draft")) < .04, str(pg.evaluate("ember.fire().draft")))
        # a rogue value never breaks it
        pg.evaluate("ember.set({fire:'lava-lamp'})"); pg.wait_for_timeout(600)
        check("an unknown kind falls back to the classic flame", pg.evaluate("ember.fire().kind") == "classic")
        # with motion off the flame holds still
        rp = b.new_context(viewport={"width": 1000, "height": 700}, reduced_motion="reduce")
        rp.route("**/api/update/check", lambda rt: rt.fulfill(json={"available": False}))
        rp.add_init_script("try{localStorage.forge_tour='done';localStorage.ember_welcomed='1';localStorage.ember_cfg=JSON.stringify({fire:'inferno'})}catch(e){}")
        q = rp.new_page(); q.on("pageerror", lambda e: errs.append("reduced: " + str(e)))
        q.goto(BASE + "/?ember=1"); q.wait_for_timeout(3500); q.evaluate("ember.still(900)"); q.wait_for_timeout(1500)
        grab = """(()=>{const c=document.getElementById('ember-cv'),x=c.getContext('2d'),s=ember.state(),d=Math.min(2,devicePixelRatio||1);
          return Array.from(x.getImageData(Math.round((s.x-s.r*1.5)*d),Math.round((s.y-s.r*2.6)*d),Math.round(s.r*3*d),Math.round(s.r*2.05*d)).data)})()"""   # the fire above the face: eyes still look around
        for _ in range(6):   # a speech bubble coming or going over the flame is not the flame moving
            b1 = q.evaluate("ember.state().bubble"); g1 = q.evaluate(grab); q.wait_for_timeout(700); g2 = q.evaluate(grab)
            if not b1 and not q.evaluate("ember.state().bubble"): break
            q.wait_for_timeout(2500)
        diff = sum(1 for i in range(0, min(len(g1), len(g2)), 4) if abs(g1[i] - g2[i]) + abs(g1[i + 3] - g2[i + 3]) > 24)
        check("with motion off the flame holds still", diff < len(g1) // 4 * .01, f"{diff} of {len(g1)//4} pixels moved")
        check("and still burns as the kind it was set to", q.evaluate("ember.fire().kind") == "inferno")
        rp.close()
        check("no page errors", not errs, "; ".join(errs[:3]))
        b.close()
finally:
    srv.terminate()
print(f"\n{OK} passed, {FAIL} failed")
print("VERDICT:", "PASS" if not FAIL else "FAIL")
sys.exit(1 if FAIL else 0)
