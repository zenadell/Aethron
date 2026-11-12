#!/usr/bin/env python3
"""The things Ember does of its own accord: a hoop, an idea, music, a dance,
coffee, shades, a notebook, a laptop, a kite and other toys, the cold, the
rain, fishing, a snack, a garden, a sparkler, the SIU, a celebration, a flirt.

A real studio on a throwaway home, Ember in a real browser, its clock run
fast. Every one of them runs to its end without a word, without a repair and
leaves nothing in its hands; the props really appear; what it did is recorded
as it happened (a fish caught or lost is counted, a note is kept and survives
a reload); what it feels follows the REAL weather it is given and nothing
else (no weather: it never shivers, and its mind is told it cannot feel it);
hunger, tiredness, cold and a win change what it wants to do; its mind can
choose any of them by name, and a help-desk line its mind writes is never
said. No key, no network.
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
SP = Path(tempfile.mkdtemp(prefix="things-")); HOME = SP / "home"; (HOME / "projects").mkdir(parents=True)
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true", AETHRON_REPORTS="off", AETHRON_EMBER_WEATHER="off")
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
    ctx = b.new_context(viewport={"width": 760, "height": 520})
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

PROPS = {"hoop": "hoop", "headset": "headset", "coffee": "cup", "note": "note", "laptop": "laptop", "kite": "kite",
         "yoyo": "yoyo", "balloon": "balloon", "bubbles": "wand", "umbrella": "umbrella", "fish": "rod",
         "snack": "snack", "sparkler": "sparkler", "idea": "bulb", "aura": "aura"}
def run(pg, k, opts=None, secs=70):
    """Run one thing on a fast clock; what was in its hands, whether it spoke, how it ended."""
    ok = pg.evaluate("([k,o])=>ember.thing(k,o)", [k, opts or {}])
    seen, said, garden = set(), "", False
    for _ in range(int(secs * 20 / 40)):
        pg.evaluate("ember.frames(50,40)")
        st = pg.evaluate("({p:ember.prop(),b:ember.bubble(),a:ember.busy(),g:ember.garden()})")
        if st["p"]: seen.add(st["p"])
        said = said or st["b"]; garden = garden or st["g"]
        if not st["a"]: break
    return {"ok": ok, "seen": seen, "said": said, "garden": garden, "ended": not st["a"], "left": st["p"]}
try:
    for _ in range(60):
        try: urllib.request.urlopen(BASE + "/", timeout=2); break
        except Exception: time.sleep(.5)
    sys.path.insert(0, str(ROOT)); import aethron_ember as E
    with sync_playwright() as p:
        b = launch(p)
        mind = []
        ctx, pg = window(b, mind)
        pg.evaluate("ember.still(9000)"); pg.evaluate("ember.lifeSet({trust:.9,bond:.6})"); pg.mouse.move(520, 200)
        names = pg.evaluate("ember.things()")
        check("the window does every thing the server names, and no other", sorted(names) == sorted(E.THINGS), str(set(names) ^ set(E.THINGS)))
        check("with no weather it feels none, and its mind is told so", "unknown" in str(pg.evaluate("ember.day().weather")))
        bad = []; props = []; silent = []; clean = []
        for k in names:
            r = run(pg, k)
            if not (r["ok"] and r["ended"]): bad.append(k)
            if k in PROPS and PROPS[k] not in r["seen"]: props.append((k, sorted(r["seen"])))
            if r["said"]: silent.append((k, r["said"]))
            if r["left"]: clean.append((k, r["left"]))
        check("every thing runs to its end", not bad, str(bad))
        check("what it holds really appears", not props, str(props))
        check("none of them says a word", not silent, str(silent))
        check("nothing is left in its hands afterwards", not clean, str(clean))
        check("no repair was needed through any of it", pg.evaluate("ember.health().bad.length") == 0, str(pg.evaluate("ember.health().bad")))
        did = [d["what"] for d in pg.evaluate("ember.did()")]
        check("what it did is recorded as it happened", len(did) >= 5 and all(isinstance(x, str) and x for x in did), str(did))
        r = run(pg, "garden")
        check("a garden grows where it stands", r["garden"])
        # fishing: the catch is counted either way, and written down
        l0 = pg.evaluate("ember.life()"); f0 = (l0.get("fish") or 0) + (l0.get("misses") or 0)
        for _ in range(3): run(pg, "fish")
        l1 = pg.evaluate("ember.life()")
        check("every cast is counted as a catch or a miss", (l1["fish"] + l1["misses"]) - f0 == 3, str((l1["fish"], l1["misses"])))
        last = pg.evaluate("ember.did()")[-1]["what"]
        check("how the fishing went is what it remembers", "fish" in last, last)
        # the notebook: its own true line, or the line its mind gave it
        n0 = len(pg.evaluate("ember.notes()"))
        run(pg, "note"); run(pg, "note", {"text": "they came back. good."})
        notes = [n["text"] for n in pg.evaluate("ember.notes()")]
        check("a note is written, and its mind's own words are kept as given", len(notes) == n0 + 2 and notes[-1] == "they came back. good." and len(notes[-2]) > 3, str(notes[-3:]))
        pg.wait_for_timeout(600)
        disk = json.loads(LIFE_FILE.read_text()) if LIFE_FILE.exists() else {}
        check("the notebook and the catch are kept in the app's folder", [n["text"] for n in disk.get("notes", [])][-1:] == ["they came back. good."] and disk.get("fish") == l1["fish"])
        # the weather is the real one, or nothing
        pg.evaluate("ember.weather(null)"); pg.evaluate("ember.scarf(false)")
        check("no weather: it has no wish to shiver", pg.evaluate("ember.urges().shiver") == 0)
        pg.evaluate("ember.weather({temp:3,feels:0,wind:30})")
        check("cold outside: it wants to shiver", pg.evaluate("ember.urges().shiver") > 1)
        r = run(pg, "shiver")
        check("it shivers, then wraps up in its scarf", r["ok"] and pg.evaluate("ember.scarf()"))
        check("with the scarf on the cold no longer bothers it", pg.evaluate("ember.urges().shiver") == 0)
        check("its mind is told the real weather and how it feels", "cold" in str(pg.evaluate("ember.day().weather.you_feel")) and "3°" in pg.evaluate("ember.day().weather.outside"))
        pg.evaluate("ember.weather({temp:34,feels:37,wind:2})")
        run(pg, "coffee"); c = pg.evaluate("ember.did()")[-1]["what"]
        check("hot outside: the coffee is iced", "iced" in c, c)
        run(pg, "kite"); kt = pg.evaluate("ember.did()")[-1]["what"]
        check("no wind: the kite does not fly, and it knows why", "no wind" in kt, kt)
        pg.evaluate("ember.weather({temp:18,feels:18,wind:24})")
        run(pg, "coffee"); run(pg, "kite"); d2 = [d["what"] for d in pg.evaluate("ember.did()")][-2:]
        check("mild and windy: hot coffee, and the kite flies", "a hot coffee" in d2[0] and "flew a kite" in d2[1], str(d2))
        # needs change what it wants
        pg.evaluate("ember.frames(50,4400)")
        u0 = pg.evaluate("ember.urges()")
        pg.evaluate("ember.win()"); u1 = pg.evaluate("ember.urges()")
        check("something went well: it wants to celebrate", u0["party"] == 0 and u1["party"] > 1, str((u0["party"], u1["party"])))
        # its mind chooses one by name; a help-desk line is never said
        plan = E.mind_parse(json.dumps({"thought": "t", "plan": [{"say": "How can I help you today?"}, {"do": "wheelie"}, {"do": "note", "text": "x marks the spot"}]}), names)["plan"]
        check("the mind's plan keeps the things and drops the help-desk line", plan == [{"do": "hoop"}, {"do": "note", "text": "x marks the spot"}], str(plan))
        check("asked in words, it does the thing", E.wish_local("do the siuuu")["plan"] == [{"do": "siu"}] and E.thing_wish("make the hero button celebrate") == "")
        for txt in ("What are you working on?", "How can I help?", "Let me know if you need anything."):
            if E.deai(txt): check("a help-desk line is taken out", False, txt); break
        else: check("help-desk lines are taken out, its own are kept", E.deai("caught two fish. you were not watching.") == "caught two fish. you were not watching.")
        asks = {"can you go fishing": "fish", "do you know how to fish?": "fish", "do a wheelie": "hoop", "have a coffee": "coffee", "put some music on": "headset", "do the siuuu": "siu"}
        miss = {q: E.thing_plan(q) for q, n in asks.items() if E.thing_plan(q) != [{"do": n}]}
        check("asked in ordinary words, each thing is recognised", not miss, str(miss))
        check("a request about the site, or a 'don't', is not taken for one", not E.thing_plan("add music to the hero section") and not E.thing_plan("don't go fishing"))
        r = E.act_parse('{"kind":"send","prompt":"Go fishing","say":"sending that to Aethron"}', "can you go fishing")
        check("a spoken wish the model sent to Aethron's agent is done by Ember instead", r["kind"] == "answer" and r["plan"] == [{"do": "fish"}] and "sending" not in r["say"], str(r))
        r = E.parse('{"say":"My notebook stays with me.","face":"smug"}', "show me your notebook")
        check("asked for its notebook, it shows it, whatever the model said", r["plan"] == [{"show": "notebook"}] and "stays with me" not in r["say"], str(r))
        check("asked what it can do, it shows several", len(E.thing_plan("what can you do")) == 4)
        check("a change to their site is still sent on", E.act_parse('{"kind":"send","prompt":"Change the hero headline to Hi","say":"sending"}', "change the hero headline to hi")["kind"] == "send")
        # in the window: a typed wish runs with no model, the notebook opens, the menu runs any thing
        pg.evaluate("ember.frames(50,60)"); pg.evaluate("ember.wish('can you go fishing')")
        pg.evaluate("ember.frames(50,60)")
        check("typed in the window with no model, the wish still runs", pg.evaluate("ember.prop()") == "rod", pg.evaluate("ember.prop()"))
        pg.evaluate("ember.wish('show me your notebook')"); pg.wait_for_timeout(250)
        check("the notebook opens when asked", pg.evaluate("!document.getElementById('ember-menu').hidden&&/notebook/i.test(document.getElementById('ember-menu').textContent)"))
        pg.evaluate("ember.showThings()"); pg.wait_for_timeout(100)
        n_btn = pg.evaluate("document.querySelectorAll('#ember-menu [data-thing]').length")
        pg.evaluate("document.querySelector('#ember-menu [data-thing=hoop]').click()"); pg.evaluate("ember.frames(50,40)")
        check("the menu lists every thing and runs the one pressed", n_btn == len(names) and pg.evaluate("ember.prop()") == "hoop", str(n_btn))
        check("every voice of Ember knows who made it", all("Jomiez" in t and "Tim" in t for t in (E.rules("sassy"), E.act_rules("sweet"), E.mind_rules("sassy", []))))
        ctx.close()
        # a new window: the notebook is still there
        ctx, pg = window(b); pg.evaluate("ember.still(900)")
        check("after a reload the notebook is still its own", [n["text"] for n in pg.evaluate("ember.notes()")][-1:] == ["they came back. good."])
        check("no script errors", not errs, str(errs[:3]))
        ctx.close(); b.close()
finally:
    srv.terminate()
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
