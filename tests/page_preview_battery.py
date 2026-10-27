"""A page rebuilt from a screenshot is SHOWN, never handed over as a path.

The owner ran a rebuild on 1.1.26 and got back
/Users/…/Library/Application Support/Aethron/rebuilds/<hash>/page.html —
a file in a folder macOS hides. A rebuild now lands in the pages folder
the studio reads, and opens in the same sheet as a template's preview.

  A  screenshot_to_page writes PAGES/<name>/site.html and names the page
     for the studio; when measuring cannot run it tells the agent to
     write that exact file; a change keeps the version it replaces; the
     agent may write a page without a permission card
  B  the studio in a real browser: the conversation shows the page as a
     card, the sidebar lists it, the preview opens with point-and-edit,
     Remove hides the element and Undo brings it back, Show code shows
     the source, and a change asked in words names the file exactly
     (SKIPPED without playwright or a browser, never PASS)
"""
import json, os, pathlib, shutil, socket, subprocess, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-pages-"))
HOME = SP / "home"
(HOME / "projects").mkdir(parents=True)
os.environ["AETHRON_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import aethron_code as C
import forge_mcp as FM

ok = True


def check(n, c):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n)


PAGES = HOME / "pages"
FIX = (ROOT / "tests" / "pages" / "site.html").read_text()
SHOT = SP / "Screenshot 2026-10-18 at 18.01.png"
SHOT.write_bytes(b"\x89PNG not really")

# ── A: the tool ──────────────────────────────────────────────────────
import aethron_shot2code as S2C
import aethron_brain as BR
_orig, _res = S2C.build, BR.resolve


def fake_build(image, outdir, **k):
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "site.html").write_text(FIX)
    return {"verdict": "PASS", "likeness": 0.71, "rounds": [{}], "calls": 1,
            "usd": 0.0, "page": str(out / "site.html"), "findings": [],
            "elements": {"a": 7, "button": 2}}


S2C.build = fake_build
BR.resolve = lambda *a, **k: {"ready": True, "wire": "openai"}
try:
    r1 = FM.t_screenshot_to_page({"image": str(SHOT)})
    r2 = FM.t_screenshot_to_page({"image": str(SHOT)})
    r2n = FM.t_screenshot_to_page({"image": str(SHOT), "new": True})
    BR.resolve = lambda *a, **k: {"ready": False, "why": "test"}
    r3 = FM.t_screenshot_to_page({"image": str(SHOT)})
finally:
    S2C.build, BR.resolve = _orig, _res
name = "screenshot-2026-10-18-at-18-01"
check("a rebuild lands where the studio looks, as site.html",
      (PAGES / name / "site.html").read_text() == FIX)
check("and the result names the page for the studio, not a path to go find",
      f"aethron-page: {name}" in r1 and "Do not give them the path" in r1)
check("a second rebuild of the same screenshot continues its page, not a new one",
      f"aethron-page: {name}\n" in r2 and f"{name}-2" not in r2)
check("a separate version only when asked for (new=true)",
      f"aethron-page: {name}-2" in r2n and (PAGES / f"{name}-2" / "site.html").is_file())
check("when measuring cannot run, the agent is told the exact file to continue",
      r3.startswith("SKIPPED") and str(PAGES / f"{name}-2" / "site.html") in r3
      and "CONTINUE" in r3 and "Do not ask" in r3)
check("the agent may write that file without a permission card",
      C.permission_risk("Write", {"file_path": str(PAGES / "x" / "site.html")},
                        str(HOME / "workspaces" / "__console__")) == "")
check("but a write anywhere else outside the workspace still asks",
      C.permission_risk("Write", {"file_path": str(SP / "elsewhere.html")},
                        str(HOME / "workspaces" / "__console__")) != "")
FM.page_snapshot(PAGES / name / "site.html")
check("a change keeps the version it replaces, for Undo",
      len(list((PAGES / name / ".history").glob("*.html"))) == 1)
shutil.rmtree(PAGES / name / ".history")
shutil.copytree(ROOT / "tests" / "pages" / "assets", PAGES / name / "assets")

# ── B: the studio in a browser ───────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None
if not sync_playwright:
    print("SKIP playwright is not installed — B UNVERIFIED")
else:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
    env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true")
    for k in list(env):
        if k.startswith("AETHRON_SUPABASE"):
            env.pop(k)
    srv = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                           stdout=open(SP / "studio.log", "w"),
                           stderr=subprocess.STDOUT)
    try:
        time.sleep(4)
        with sync_playwright() as p:
            b = None
            for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
                try:
                    b = p.chromium.launch(**kw); break
                except Exception:
                    pass
            if not b:
                print("SKIP no browser playwright can start — B UNVERIFIED")
            else:
                pg = b.new_page(viewport={"width": 1400, "height": 900})
                errs, sent = [], []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.add_init_script("try{localStorage.forge_tour='done';"
                                   "localStorage.forge_news='done';"
                                   "localStorage.forge_dev='desktop'}catch(e){}")
                pg.route("**/api/code/start", lambda r: r.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"key": "k1"})))

                def on_send(r):
                    sent.append(json.loads(r.request.post_data or "{}"))
                    r.fulfill(status=200, content_type="application/json",
                              body='{"ok":true}')
                pg.route("**/api/code/send", on_send)
                pg.goto(f"http://127.0.0.1:{PORT}/")
                pg.wait_for_selector("#npurl", timeout=15000)
                pg.wait_for_timeout(800)
                check("the sidebar lists the page",
                      pg.locator(f".pitem .pname:text-is('{name}')").count() == 1)
                pg.evaluate("""n=>{CODE.events=[
                  {type:'you',text:'Rebuild this screenshot as html/css/js'},
                  {type:'tool',id:'t1',name:'mcp__aethron__screenshot_to_page',input:{image:'/x.png'}},
                  {type:'tool_result',id:'t1',ok:true,text:'PASS: 9 of 9 checks correct\\npage: /x/site.html\\naethron-page: '+n+'\\n'},
                  {type:'text',text:'Done.'}]; renderChat();}""", name)
                pg.wait_for_selector(".pagecard", timeout=5000)
                check("the conversation shows the page itself, with its doors",
                      pg.locator(".pagecard button").count() == 5
                      and pg.locator(".pagecard iframe").get_attribute("src")
                      == f"/design/{name}/site.html")
                pg.click(".pagecard button:has-text('Point and edit')")
                pg.wait_for_selector("#pvwframe", timeout=5000)
                pg.wait_for_timeout(1500)
                check("the preview opens on the page with the point-and-edit picker",
                      "pick=1" in pg.locator("#pvwframe").get_attribute("src")
                      and pg.frame_locator("#pvwframe")
                        .locator("script[data-forge-editor]").count() == 1)
                check("with Undo, Open in browser, Show code and Open in Finder beside it",
                      all(pg.locator(f".pvw-bar button[title='{t}']").count() == 1
                          for t in ("Show code", "Open in Finder", "Open in your browser"))
                      and pg.locator("#pgundo").count() == 1)
                fr = pg.frame_locator("#pvwframe")
                fr.locator("[data-ae-id='t05']").click(force=True)
                pg.wait_for_selector("#selbox", timeout=5000)
                check("pointing at the headline offers Remove",
                      pg.locator("#selbox button:has-text('Remove')").count() == 1)
                pg.click("#selbox button:has-text('Remove')")
                pg.wait_for_timeout(2000)
                site = (PAGES / name / "site.html").read_text()
                gone = fr.locator("[data-ae-id='t05']").evaluate(
                    "e=>getComputedStyle(e).display==='none'||getComputedStyle(e).visibility==='hidden'")
                check("Remove hides it on the page, in the file itself",
                      site != FIX and gone)
                check("and Undo is offered",
                      pg.locator("#pgundo").is_enabled())
                pg.click("#pgundo")
                pg.wait_for_timeout(2000)
                check("Undo brings it back exactly",
                      (PAGES / name / "site.html").read_text() == FIX
                      and fr.locator("[data-ae-id='t05']").is_visible())
                pg.click(".pvw-bar button[title='Show code']")
                pg.wait_for_selector(".codeview", timeout=5000)
                check("Show code shows the page's source",
                      "Build Apps People Love" in pg.inner_text(".codeview"))
                pg.click(".pvw-bar button:has-text('Preview')")
                pg.wait_for_selector("#pvwframe", timeout=5000)
                pg.wait_for_timeout(1200)
                pg.click(".pvw-seg button:has-text('Select')")
                pg.wait_for_timeout(300)
                fr.locator("[data-ae-id='s01']").click(force=True)
                pg.wait_for_selector("#selta", timeout=5000)
                pg.fill("#selta", "make it green")
                pg.click("#selbox .send")
                pg.wait_for_timeout(2500)
                said = sent[-1]["text"] if sent else ""
                check("a change in words names the page's file exactly",
                      f'On the page "{name}"' in said
                      and str(PAGES / name / "site.html") in said
                      and "make it green" in said)
                check("and the version it replaces was kept first",
                      len(list((PAGES / name / ".history").glob("*.html"))) >= 1)
                check("no page errors", not errs or print(errs))
                b.close()
    finally:
        srv.terminate()
shutil.rmtree(SP, ignore_errors=True)
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
