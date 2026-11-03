#!/usr/bin/env python3
"""What the Aethron team says, as a person sees it in the app — in a real
browser, against the real edge function (tests/cloud/serve_fake.ts) and the
real studio, with a throwaway AETHRON_HOME.

  paused   a chip in the top bar (not a pill over the title), a calm card
           above the chat box, and a message sent while paused comes back
           as the team's words — never "check your model settings"
  resumed  all of it goes away by itself
  banner   a chip with the text; its card opens and can be hidden
  news     an announcement opens once as a card with its illustration
           (an SVG picture served by the studio, no script), "Got it"
           closes it, and it does not come back after a reload

Needs bun and a browser; without either it reports SKIPPED, never PASS.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def skip(why):
    print("VERDICT: SKIPPED — " + why)
    sys.exit(0)


bun = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")
if not Path(bun).exists():
    skip("no bun to run the edge function")
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    skip("no playwright")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


HOME = Path(tempfile.mkdtemp(prefix="ae-teamui-"))
port = free_port()
BASE = f"http://127.0.0.1:{port}"
FN = BASE + "/aethron"
os.environ.update(AETHRON_HOME=str(HOME), AETHRON_CONTROL_URL=FN, AETHRON_REPORTS="off", AETHRON_RUNLOG="off")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
srv = subprocess.Popen([bun, str(ROOT / "tests/cloud/serve_fake.ts"), str(port)], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
MASTER = "master-key-0123456789abcdefghij"


def admin(tool, **args):
    r = urllib.request.Request(FN + "/admin/call", data=json.dumps({"tool": tool, "args": args}).encode(),
                               headers={"Authorization": "Bearer " + MASTER, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["result"]


try:
    if "ready" not in srv.stdout.readline():
        skip("the function did not start")
    import aethron_control as C
    import studio

    me = json.loads(urllib.request.urlopen(BASE + "/__user?email=una@example.com", timeout=5).read())
    C.ping(me["token"])
    sport = free_port()
    httpd = studio.ThreadingHTTPServer(("127.0.0.1", sport), studio.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app = f"http://127.0.0.1:{sport}/"

    with sync_playwright() as p:
        exe = os.environ.get("AE_CHROMIUM") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
        try:
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as e:
            skip(f"no browser ({e})")
        ctx = b.new_context(viewport={"width": 1400, "height": 860})
        ctx.add_init_script("try{localStorage.forge_tour='1'}catch(e){}")
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))

        # ── paused ──
        msg = "Aethron is paused for a moment while we fix something. Back soon."
        admin("pause_all", message=msg)
        C.ping()
        pg.goto(app)
        pg.wait_for_selector("#teamchip .tc", timeout=15000)
        chip = pg.locator("#teamchip .tc")
        check("paused: a chip in the top bar says so", "Paused by the team" in chip.inner_text())
        check("and it lives in the header, not floating over the title",
              pg.evaluate("!!document.querySelector('header #teamchip') && !document.querySelector('#teambar')"))
        pg.wait_for_selector("#teamhold", timeout=10000)
        hold = pg.inner_text("#teamhold")
        check("a calm card above the chat box carries the team's words", "Aethron is paused" in hold and msg in hold)
        check("and the chat box shows it is held", pg.evaluate("document.querySelector('.composer').classList.contains('held')"))
        chip.click()
        pg.wait_for_selector(".tcpop", timeout=5000)
        check("the chip opens the full message", msg in pg.inner_text(".tcpop"))
        pg.mouse.click(700, 500)
        pg.fill("#npurl", "hi")
        pg.press("#npurl", "Enter")
        pg.wait_for_selector(".turn.teamnote", timeout=15000)
        log = pg.inner_text("#chatlog")
        check("a message sent while paused comes back in the team's words", msg in log and "Not sent" in log)
        check("and never as 'check the model settings'", "model settings" not in log)
        vis = pg.evaluate("""(()=>{const h=document.querySelector('#teamhold');if(!h)return 'missing';
          const r=h.getBoundingClientRect();return JSON.stringify({w:r.width,h:r.height,top:r.top,n:document.querySelectorAll('#teamhold').length,
          parent:h.parentNode.className,shown:getComputedStyle(h).display})})()""")
        print("DEBUG hold", vis)
        check("the paused card is on screen", '"w":0' not in vis and vis != "missing")
        pg.screenshot(path=str(HOME / "paused.png"))

        # ── resumed ──
        admin("resume_all")
        C.ping()
        pg.evaluate("teamCheck(true)")
        pg.wait_for_function("!document.querySelector('#teamhold') && document.querySelector('#teamchip').hidden", timeout=10000)
        check("resuming clears the chip, the card and the held chat box",
              not pg.evaluate("document.querySelector('.composer').classList.contains('held')"))

        # ── banner ──
        admin("set_banner", text="Maintenance tonight at 22:00 UTC — about ten minutes.", level="warn")
        C.ping()
        pg.evaluate("teamCheck(true)")
        pg.wait_for_selector("#teamchip .tc.warn", timeout=10000)
        check("a banner shows as a chip with its words", "Maintenance tonight" in pg.inner_text("#teamchip"))
        pg.click("#teamchip .tc")
        pg.click(".tcpop >> text=Hide")
        pg.wait_for_function("document.querySelector('#teamchip').hidden", timeout=5000)
        check("and it can be hidden", True)
        admin("set_banner", text="")

        # ── too old: the whole app is behind one update sheet ──
        admin("set_min_version", version="9.9.9")
        C.ping()
        pg.goto(app)
        pg.wait_for_selector("#updgate .ugc", timeout=15000)
        check("too old: the update sheet is up before anything is sent",
              "Update Aethron to continue" in pg.inner_text("#updgate"))
        check("and the rest of the app cannot be used",
              pg.evaluate("[...document.body.children].filter(x=>x.id!=='updgate').every(x=>x.inert)"))
        pg.evaluate("UPD={available:true,latest:'9.9.9',version:'1.0.8'};UG=null;updGate()")
        check("with one button that updates, and both versions",
              pg.inner_text("#ugbtn") == "Update now" and "1.0.8" in pg.inner_text("#updgate") and "9.9.9" in pg.inner_text("#updgate"))
        pg.wait_for_timeout(1200)
        pg.screenshot(path=str(Path(os.environ.get("AE_SHOTS") or HOME) / "update-gate.png"))
        admin("set_min_version", version="")
        C.ping()
        pg.evaluate("teamCheck(true)")
        pg.wait_for_function("!document.querySelector('#updgate')", timeout=10000)
        check("and it goes away, the app usable again, once the version is fine",
              pg.evaluate("![...document.body.children].some(x=>x.inert)"))

        # ── an announcement, once ──
        admin("publish_announcement", title="Screenshots now build whole sites",
              body="Drop a screenshot into Aethron and get a real, responsive website back.")
        C.ping()
        pg.evaluate("teamCheck(true)")
        pg.wait_for_selector("#annov .anncard", timeout=10000)
        card = pg.inner_text("#annov")
        check("an announcement opens as a card with its words", "Screenshots now build whole sites" in card
              and "From the Aethron team" in card)
        pg.wait_for_function("(()=>{const i=document.querySelector('#annov img');return i&&i.complete&&i.naturalWidth>0})()",
                             timeout=10000)
        check("with its illustration, drawn and loaded", True)
        r = urllib.request.urlopen(app + "api/announcement/art?id=" + C.control()["announcement"]["id"], timeout=5)
        check("served by the studio as an SVG picture that cannot run code",
              "image/svg+xml" in r.headers.get("Content-Type", "") and "sandbox" in r.headers.get("Content-Security-Policy", "")
              and b"<script" not in r.read().lower())
        pg.wait_for_timeout(1600)
        pg.screenshot(path=str(HOME / "announce.png"))
        pg.click("#annov >> text=Got it")
        pg.wait_for_function("!document.querySelector('#annov')", timeout=5000)
        pg.reload()
        pg.wait_for_timeout(2500)
        check("seen once, it does not come back after a reload", pg.locator("#annov").count() == 0)
        check("no page errors", not errors or print(errors))
        if os.environ.get("AE_KEEP_SHOTS"):
            for f in ("paused.png", "announce.png"):
                shutil.copy(HOME / f, os.environ["AE_KEEP_SHOTS"])
        b.close()
    httpd.shutdown()
finally:
    srv.terminate()
    shutil.rmtree(HOME, ignore_errors=True)

print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
