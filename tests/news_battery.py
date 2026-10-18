"""What's new card: shown once after an update, and every button goes
where it says. Real studio, real browser, throwaway AETHRON_HOME with a
project built from tests/pages/site.html. SKIPPED without playwright or
a browser — never PASS.
"""
import os, sys, json, time, socket, shutil, subprocess, tempfile, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("VERDICT: SKIPPED — playwright is not installed"); sys.exit(0)
SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-news-")); HOME = SP / "home"
(HOME / "projects").mkdir(parents=True)
work = SP / "work"; work.mkdir()
subprocess.run([sys.executable, str(ROOT / "forge.py"), "init",
                str(ROOT / "tests/pages/site.html")], cwd=work,
               capture_output=True, timeout=300)
proj = next(work.iterdir()); shutil.move(str(proj), HOME / "projects" / proj.name)
proj = HOME / "projects" / proj.name
for step in ("inventory", "build"):
    subprocess.run([sys.executable, str(ROOT / "forge.py"), step], cwd=proj,
                   capture_output=True, timeout=300)
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
def launch(p):
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try: return p.chromium.launch(**kw)
        except Exception: pass
    print("VERDICT: SKIPPED — no browser playwright can start"); sys.exit(0)
for f in (".news-seen.json",): (HOME / f).unlink(missing_ok=True)
(HOME / ".last-update.json").write_text(json.dumps({"version": "1.1.13", "from": "1.1.12"}))
env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE"): env.pop(k)
srv = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                       stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
ok = True
def check(n, c):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n)
try:
    time.sleep(4)
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and "favicon" not in str(m.location) and errs.append(m.text+" @ "+str(m.location)))
        pg.add_init_script("try{localStorage.forge_tour='done'}catch(e){}")
        pg.goto("http://127.0.0.1:%d/" % PORT + "")
        pg.wait_for_selector("#newsov.on .newscard", timeout=12000)
        pg.wait_for_timeout(2600)
        None
        k = pg.inner_text(".nkick")
        check("card says it updated to 1.1.13", "Updated to 1.1.13" in k)
        check("three dots for three entries", pg.locator(".ndots i").count() == 3)
        check("the Updated chip does not show beside it", pg.locator("#updchip .uc.done").count() == 0)
        pg.click("text=Next"); pg.wait_for_timeout(2200); None
        check("Next shows the second entry", "A way back" in pg.inner_text("#ntitle"))
        pg.keyboard.press("ArrowRight"); pg.wait_for_timeout(2400); None
        check("arrow key shows the third", "agent did" in pg.inner_text("#ntitle"))
        pg.click("#ngo"); pg.wait_for_timeout(800)
        check("the card closes", pg.locator("#newsov").count() == 0)
        check("Try it opens Agent runs", "Agent runs" in pg.inner_text("body"))
        check("a ring lands on the Agent runs row", pg.locator(".aespot").count() == 1)
        None
        seen = json.loads((HOME / ".news-seen.json").read_text())
        check("all three are remembered as seen", len(seen) == 3)
        pg.reload(); pg.wait_for_timeout(5500)
        check("it does not come back after a reload", pg.locator("#newsov").count() == 0)
        # the other two actions
        (HOME / ".news-seen.json").unlink()
        pg.reload(); pg.wait_for_selector("#newsov.on", timeout=12000)
        pg.click("#ngo"); pg.wait_for_timeout(1500)
        check("preview Try it opens the built project", pg.locator("#prail").count() == 1)
        check("...with a ring on the rail", pg.locator(".aespot").count() == 1)
        None
        (HOME / ".news-seen.json").unlink()
        pg.reload(); pg.wait_for_selector("#newsov.on", timeout=12000)
        pg.click("text=Next"); pg.click("#ngo"); pg.wait_for_timeout(1200)
        check("back Try it shows the back arrow, ringed",
              pg.locator("#backbtn:not([hidden])").count() == 1 and pg.locator(".aespot").count() == 1)
        None
        (HOME / ".news-seen.json").unlink()
        pg.reload(); pg.wait_for_selector("#newsov.on", timeout=12000)
        pg.keyboard.press("Escape"); pg.wait_for_timeout(500)
        check("Esc closes it", pg.locator("#newsov").count() == 0)
        pr = b.new_page(viewport={"width": 900, "height": 700}, reduced_motion="reduce")
        pr.add_init_script("try{localStorage.forge_tour='done'}catch(e){}")
        (HOME / ".news-seen.json").unlink()
        pr.goto("http://127.0.0.1:%d/" % PORT + ""); pr.wait_for_selector("#newsov.on", timeout=12000)
        pr.wait_for_timeout(600); None
        check("with motion off the picture is still drawn",
              pr.evaluate("getComputedStyle(document.querySelector('.a-rail')).opacity") == "1")
        check("no console errors", not errs); errs and print(errs)
        b.close()
finally:
    srv.terminate()
    shutil.rmtree(SP, ignore_errors=True)
print("ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
