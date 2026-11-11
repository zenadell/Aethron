#!/usr/bin/env python3
"""Ember's life with one person: bond, trust, nature, memory, and what it
starts by itself.

A real studio on a throwaway home, Ember in a real browser. Its nature is
drawn once and kept (across a reload, and across a window that lost its own
storage, because the app's folder holds it); a return after days is noticed;
petting builds the bond and throwing costs trust, after which it keeps away;
what happened is written into a shared history its mind is given; every
self-started behaviour runs without a word and without a repair; a spark it
holds out can be taken; it dreams asleep and wakes when the pointer comes;
it is sleepy at night. No key, no network.
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
SP = Path(tempfile.mkdtemp(prefix="life-")); HOME = SP / "home"; (HOME / "projects").mkdir(parents=True)
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true", AETHRON_REPORTS="off")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE") or k == "AETHRON_EMBER_SAVAGE": env.pop(k)
srv = subprocess.Popen([sys.executable, "tests/dev_studio.py", str(PORT)], cwd=ROOT, env=env,
                       stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{PORT}"
LIFE_FILE = HOME / "ember_life.json"
def launch(p):
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try: return p.chromium.launch(**kw)
        except Exception: pass
    print("VERDICT: SKIPPED — no browser playwright can start"); srv.terminate(); sys.exit(0)
errs = []
def window(b, mind=None):
    ctx = b.new_context(viewport={"width": 1400, "height": 900})
    ctx.route("**/api/update/check", lambda rt: rt.fulfill(json={"available": False}))
    if mind is not None:
        def answer(rt):
            mind.append(json.loads(rt.request.post_data or "{}"))
            rt.fulfill(status=200, content_type="application/json", body=json.dumps({"off": True, "retry": 600, "why": "test"}))
        ctx.route("**/api/ember/mind", answer)
    ctx.add_init_script("try{localStorage.forge_tour='done';localStorage.ember_welcomed='1'}catch(e){}")
    pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/?ember=1"); pg.wait_for_timeout(3500)
    return ctx, pg
try:
    for _ in range(60):
        try: urllib.request.urlopen(BASE + "/", timeout=2); break
        except Exception: time.sleep(.5)
    with sync_playwright() as p:
        b = launch(p)
        ctx, pg = window(b)
        pg.evaluate("ember.still(900)")
        l = pg.evaluate("ember.life()")
        check("a first meeting: one visit, a new bond", l["ready"] and l["met"] > 0 and l["visits"] == 1 and l["stage"] == "new", str({k: l[k] for k in ("ready", "met", "visits", "stage")}))
        tr = l["traits"]
        check("it is born with a nature of its own", isinstance(tr, dict) and set(tr) == {"bold", "clingy", "playful", "lazy", "curious", "vain"}
              and len({round(v, 2) for v in tr.values()}) > 3, str(tr))
        for _ in range(20):
            if LIFE_FILE.exists(): break
            pg.wait_for_timeout(250)
        on_disk = json.loads(LIFE_FILE.read_text()) if LIFE_FILE.exists() else {}
        check("its life is kept in the app's own folder", on_disk.get("traits") == tr and on_disk.get("visits") == 1)
        pg.reload(); pg.wait_for_timeout(3500); pg.evaluate("ember.still(900)")
        l2 = pg.evaluate("ember.life()")
        check("a reload a moment later is the same visit, the same nature", l2["traits"] == tr and l2["visits"] == 1, str(l2["visits"]))
        # bond and trust are earned and lost
        b0 = l2["bond"]
        pg.evaluate("for(let i=0;i<4;i++)ember.event('petted',{what:'they petted Ember with the pointer'})")
        l3 = pg.evaluate("ember.life()")
        check("petting builds the bond and makes that place its favourite", l3["bond"] > b0 + .02 and l3["pets"] == 4 and l3["fav"] is not None, str((b0, l3["bond"], l3["fav"])))
        st = pg.evaluate("ember.state()"); cx = st['x'] + 60
        pg.evaluate(f"ember.cursor({cx},{st['y']})")
        u0 = pg.evaluate("ember.life().urges")
        pg.evaluate("for(let i=0;i<3;i++)ember.event('thrown',{what:'they threw Ember hard across the screen'})")
        l4 = pg.evaluate("ember.life()")
        check("being thrown hard costs trust", l4["trust"] < .4 and l4["throws"] == 3, str(l4["trust"]))
        check("and then it keeps its distance instead of coming near", l4["urges"]["wary"] > 1 and l4["urges"]["near"] == 0 and l4["urges"]["tag"] == 0, str({k: l4["urges"][k] for k in ("wary", "near", "tag")}))
        check("before that it was not wary", u0["wary"] == 0)
        pg.evaluate("ember.impulse('wary')"); x0 = pg.evaluate("ember.state().x"); pg.wait_for_timeout(2500)
        check("wary, it backs away from the pointer", abs(pg.evaluate("ember.state().x") - cx) > abs(x0 - cx) + 40, f"{x0} -> {pg.evaluate('ember.state().x')} from pointer at {cx}")
        pg.evaluate("ember.signal('published',{})"); pg.wait_for_timeout(300)
        l5 = pg.evaluate("ember.life()")
        check("what happens to them goes into a shared history", l5["wins"] == 1 and any("went live" in j["what"] for j in l5["journal"])
              and any("threw" in j["what"] for j in l5["journal"]), str(l5["journal"]))
        pg.wait_for_timeout(7000)
        # everything it starts by itself: no words, no repairs
        pg.evaluate("ember.lifeSet({trust:.9,bond:.8})"); pg.evaluate("ember.still(900)")
        ran, said, bad = {}, set(), []
        for k in ("near", "company", "peek", "showoff", "groom", "miss", "yawn", "favorite", "nuzzle", "tag", "gift"):
            stt = pg.evaluate("ember.state()")
            pg.evaluate(f"ember.cursor({max(80, stt['x'] - 260)},{max(120, stt['y'] - 120)})")
            ok = pg.evaluate(f"ember.impulse('{k}')"); pg.wait_for_timeout(1300)
            s2 = pg.evaluate("ember.state()"); ran[k] = bool(ok) and s2["act"] == "ns_" + k
            said.add(s2["bubble"] or ""); bad += pg.evaluate("ember.health().bad")
        check("every self-started behaviour runs", all(ran.values()), str({k: v for k, v in ran.items() if not v}))
        check("none of them says a word", said <= {""}, str(said))
        check("and none needed a repair", not bad, str(bad[:3]))
        # a spark held out can be taken
        stt = pg.evaluate("ember.state()"); pg.evaluate(f"ember.cursor({stt['x'] - 200},{stt['y']})")
        pg.evaluate("ember.impulse('gift')")
        at = None
        for _ in range(40):
            pg.wait_for_timeout(200); at = pg.evaluate("ember.giftAt()")
            if at: break
        check("it holds out a spark", bool(at))
        pg.wait_for_timeout(600); at = pg.evaluate("ember.giftAt()") or at
        g0 = pg.evaluate("ember.life().gifts"); pg.evaluate(f"ember.cursor({at[0]},{at[1]})"); pg.wait_for_timeout(500)
        l6 = pg.evaluate("ember.life()")
        check("taking it is felt and remembered", l6["gifts"] == g0 + 1 and not l6["gift"] and any("spark" in j["what"] for j in l6["journal"]), str(l6["gifts"]))
        # tag moves it about, with a wordless wish over its head
        stt = pg.evaluate("ember.state()"); pg.evaluate(f"ember.cursor({stt['x'] - 150},{stt['y']})"); pg.evaluate("ember.impulse('tag')")
        w = pg.evaluate("ember.life().want"); xs = []
        for _ in range(24):
            pg.wait_for_timeout(300); s3 = pg.evaluate("ember.state()"); xs.append((s3["x"], s3["y"]))
        span = max(x for x, _ in xs) - min(x for x, _ in xs) + max(y for _, y in xs) - min(y for _, y in xs)
        check("a game of tag sends it darting about", span > 200, f"moved {span:.0f}px")
        check("its wish is drawn, not written", w == "play" and not pg.evaluate("ember.state().bubble"), str(w))
        # asleep it dreams; the pointer coming near wakes it
        pg.evaluate("ember.cursor(40,40)"); pg.evaluate("ember.impulse('doze')"); pg.wait_for_timeout(3200)
        check("asleep, it dreams", pg.evaluate("ember.life().dreaming") is True)
        stt = pg.evaluate("ember.state()"); pg.evaluate(f"ember.cursor({stt['x'] + 30},{stt['y']})"); pg.wait_for_timeout(400)
        check("the pointer coming near wakes it", pg.evaluate("ember.state().act") == "ns_wake" and pg.evaluate("ember.life().dreaming") is False, pg.evaluate("ember.state().act"))
        # the hour
        pg.evaluate("ember.hurry();ember.lifeSet({hour:2})"); n = pg.evaluate("ember.life()")
        pg.evaluate("ember.lifeSet({hour:14})"); d = pg.evaluate("ember.life()")
        check("at night it is sleepy; by day it is not", n["urges"]["yawn"] > .5 and n["view"]["night"] is True and d["urges"]["yawn"] == 0 and d["view"]["night"] is False,
              str((n["urges"]["yawn"], d["urges"]["yawn"])))
        pg.evaluate("ember.lifeSet({hour:null})")
        # Settings says what they are to each other
        pg.evaluate("openSettings()"); pg.wait_for_timeout(900)
        line = pg.evaluate("(document.getElementById('ember-life-line')||{}).innerText||''")
        check("Settings says how long they have known each other", "has known you for 1 day" in line and "nature" in line, line[:160])
        pg.evaluate("ember.lifeSet({})"); pg.wait_for_timeout(600)
        ctx.close(); time.sleep(1.5)     # its last save on the way out lands first
        # days later, in a window that lost its own storage
        saved = json.loads(LIFE_FILE.read_text())
        saved.update(last=int(time.time() * 1000) - 3 * 86400 * 1000, bond=.5, trust=.9, lastDay="Thu Jan 01 2026")
        LIFE_FILE.write_text(json.dumps(saved))
        asked = []
        ctx2, pg2 = window(b, mind=asked)
        pg2.wait_for_timeout(1500)
        r = pg2.evaluate("ember.life()")
        check("in a window with empty storage it is still the same Ember", r["traits"] == tr and r["pets"] == 4, str(r["traits"]))
        check("coming back days later is a new visit and a new day", r["visits"] == saved["visits"] + 1 and r["days"] == saved["days"] + 1 and r["view"]["back_after"] == "3 days",
              str((r["visits"], r["days"], r["view"]["back_after"])))
        mm = pg2.evaluate("ember.mind()"); nv = pg2.evaluate("ember.nervous()")
        check("it notices: its mind hears they are back, and its body shows it", any("back after 3 days" in e for e in mm["ev"]) and "reunion" in nv["kinds"], str((mm["ev"], nv["kinds"])))
        pg2.evaluate("ember.think('test')"); pg2.wait_for_timeout(1500)
        lf = ((asked[-1] if asked else {}).get("state") or {}).get("life") or {}
        check("its mind is given the life they share", lf.get("bond") == "friend" and lf.get("back_after") == "3 days" and lf.get("your_nature")
              and any("went live" in h for h in lf.get("shared_history", [])) and lf.get("known_days", 0) >= 1, str(lf)[:300])
        ctx2.close()
        check("no page errors", not errs, "; ".join(errs[:3]))
        b.close()
finally:
    srv.terminate()
print(f"\n{OK} passed, {FAIL} failed")
print("VERDICT:", "PASS" if not FAIL else "FAIL")
sys.exit(1 if FAIL else 0)
