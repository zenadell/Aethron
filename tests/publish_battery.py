"""Publish to Netlify, through the real studio, in a real browser, against
a fake Netlify that keeps the real API's rules (aethron_publish.fake_netlify).

A project built from tests/pages/site.html is published from the Publish
dialog: browser sign-in, an address, the build + upload + live steps, then
republish, versions/restore, a custom domain, rename and unpublish. The
token must never reach the page. SKIPPED without playwright or a browser.
"""
import os, sys, json, time, socket, shutil, subprocess, tempfile, pathlib, urllib.request
ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("VERDICT: SKIPPED — playwright is not installed"); sys.exit(0)
import aethron_publish as P

SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-pub-")); HOME = SP / "home"
(HOME / "projects").mkdir(parents=True)
work = SP / "work"; work.mkdir()
subprocess.run([sys.executable, str(ROOT / "forge.py"), "init",
                str(ROOT / "tests/pages/site.html")], cwd=work,
               capture_output=True, timeout=300)
proj = next(work.iterdir()); shutil.move(str(proj), HOME / "projects" / proj.name)
proj = HOME / "projects" / proj.name
NAME = proj.name
for step in ("inventory", "build"):
    subprocess.run([sys.executable, str(ROOT / "forge.py"), step], cwd=proj,
                   capture_output=True, timeout=300)

fsrv, st, API = P.fake_netlify()
s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()


def launch(p):
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try:
            return p.chromium.launch(**kw)
        except Exception:
            pass
    print("VERDICT: SKIPPED — no browser playwright can start"); sys.exit(0)


env = dict(os.environ, AETHRON_HOME=str(HOME), BROWSER="true",
           AETHRON_NETLIFY_API=API, AETHRON_NETLIFY_CLIENT_ID="cid-test",
           AETHRON_REPORTS="off")
for k in list(env):
    if k.startswith("AETHRON_SUPABASE"):
        env.pop(k)
from aethron_news import NEWS as _NEWS
(HOME / ".news-seen.json").write_text(json.dumps([n["id"] for n in _NEWS]))
srv = subprocess.Popen([sys.executable, "studio.py", str(PORT)], env=env,
                       stdout=open(SP / "studio.log", "w"), stderr=subprocess.STDOUT)
ok = True


def check(n, c):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n)


def post(body):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/publish",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


try:
    time.sleep(4)
    seen = []
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and "favicon" not in str(m.location)
              and errs.append(m.text))
        pg.on("response", lambda r: "/api/publish" in r.url and seen.append(r.text()))
        pg.add_init_script("try{localStorage.forge_tour='done'}catch(e){}")
        pg.goto(f"http://127.0.0.1:{PORT}/")
        pg.wait_for_timeout(2500)
        pg.evaluate(f"select({json.dumps(NAME)})")
        pg.wait_for_selector("#pubrail", timeout=15000)
        check("a built project shows Publish beside Build", "Publish" in pg.inner_text("#pubrail"))
        pg.click("#pubrail")
        pg.wait_for_selector("#pubov.on .pubcard", timeout=5000)
        pg.wait_for_selector("text=Connect Netlify", timeout=5000)
        check("not connected: the dialog offers Netlify sign-in",
              pg.locator("text=Use a token instead").count() == 1)

        # browser sign-in: Netlify's page would open; approve the ticket ourselves
        pg.click("text=Connect Netlify")
        pg.wait_for_selector("text=Waiting for your approval", timeout=5000)
        check("it waits for approval on netlify.com", True)
        tid = next(iter(st["tickets"]))
        st["tickets"][tid]["authorized"] = True
        pg.wait_for_selector("#pubname", timeout=10000)
        check("once approved, the account is connected without a reload",
              "you@example.com" in pg.inner_text(".pubcard"))

        pg.fill("#pubname", "My Bakery")
        pg.click(".pubcard button.primary:has-text('Publish')")
        pg.wait_for_selector(".pubstep.now", timeout=5000)
        check("publishing shows its steps", pg.locator(".pubstep").count() == 3)
        pg.wait_for_selector("text=Your site is live", timeout=120000)
        url = pg.inner_text(".publive .url")
        check("the live address is the one asked for", "my-bakery.netlify.app" in url)
        rec = P.records if False else json.loads((HOME / "published.json").read_text())
        r = rec[f"project:{NAME}"]
        dep = st["deploys"][r["deploy_id"]]
        check("the site went up as a real deploy, ready", dep["state"] == "ready")
        check("index.html was uploaded, with the built content",
              b"<" in st["blobs"][dep["files"]["/index.html"]])
        check("our own runner and reports were not uploaded",
              not any(k.endswith(("serve.py", "README.txt", ".forge-report.json"))
                      for k in dep["files"]))
        check("Open site and Copy link are offered",
              pg.locator("text=Open site").count() == 1 and pg.locator("text=Copy link").count() == 1)
        idx = st["blobs"][dep["files"]["/index.html"]]
        check("the published page carries the Built with Aethron badge",
              idx.count(b"<script data-aethron-badge>") == 1 and P.SITE_URL.encode() in idx)
        check("the dialog shows the badge switch, on",
              pg.locator("#pubbadge").is_checked())
        # the badge on the real published page, in a browser, beside Netlify's own
        bp = b.new_page(viewport={"width": 1400, "height": 900})
        nf = (b'<iframe id="nl-badge-frame" title="Powered by Netlify" style="position:fixed;'
              b'right:0;bottom:0;width:216px;height:92px;border:0"></iframe></body>')
        for name_, html_, vw in (("alone", idx, 1400),
                                 ("beside Netlify's", idx.replace(b"</body>", nf), 1400),
                                 ("beside Netlify's at 500px", idx.replace(b"</body>", nf), 500),
                                 ("beside Netlify's on a 390px phone", idx.replace(b"</body>", nf), 390)):
            f_ = SP / "badge.html"; f_.write_bytes(html_)
            if vw < 500:   # headless Chrome will not lay out below 500px: use a true viewport
                (SP / "frame.html").write_text(
                    f'<iframe src="badge.html" style="width:{vw}px;height:860px;border:0"></iframe>')
                bp.set_viewport_size({"width": 600, "height": 900})
                bp.goto((SP / "frame.html").as_uri()); bp.wait_for_timeout(1800)
                fr = bp.frames[1]
            else:
                bp.set_viewport_size({"width": vw, "height": 900})
                bp.goto(f_.as_uri()); bp.wait_for_timeout(1800)
                fr = bp.main_frame
            box = fr.evaluate("""()=>{const h=document.querySelector('div[data-aethron-badge]');
              const n=document.querySelector('#nl-badge-frame');if(!h)return null;
              const r=h.getBoundingClientRect(),q=n?n.getBoundingClientRect():null;
              const over=q?!(r.right<=q.left||r.left>=q.right||r.bottom<=q.top||r.top>=q.bottom):false;
              return {w:r.width,over,inside:r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight,
                      right:innerWidth-r.right}}""")
            ok_ = bool(box) and box["w"] > 150 and box["inside"] and not box["over"]
            if ok_ and name_ == "alone":
                ok_ = box["right"] <= 20      # bottom-right, like Framer's
            check(f"the badge shows {name_} without overlapping it", ok_ or print(name_, box))
        bp.close()
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(2500)
        check("closed, the rail says the site is Live", "Live" in pg.inner_text("#pubrail"))
        pg.click("#pubrail")
        pg.wait_for_selector("text=Your site is live", timeout=8000)

        # republish: nothing changed, so nothing is uploaded again
        puts = st["puts"]
        pg.click("text=Publish changes")
        pg.wait_for_selector("text=Your site is live", timeout=120000)
        pg.wait_for_timeout(500)
        check("republishing an unchanged site uploads nothing new", st["puts"] == puts)

        pg.click("summary:has-text('Versions')")
        pg.wait_for_selector(".pubver", timeout=8000)
        check("both versions are listed, one live",
              pg.locator(".pubver").count() == 2 and pg.locator(".pubver .live").count() == 1)
        pg.on("dialog", lambda d: d.accept())
        pg.click(".pubver button:has-text('Restore')")
        pg.wait_for_timeout(1500)
        site_id = r["site_id"]
        first = [d for d in st["deploys"].values() if d["site_id"] == site_id]
        first = min(first, key=lambda d: d["n"])["id"]
        check("Restore makes the earlier version live", st["sites"][site_id]["live"] == first)

        pg.click("summary:has-text('Custom domain')")
        pg.fill("#pubdom", "www.mybakery.com")
        pg.click("text=Connect domain")
        pg.wait_for_selector(".pubdns", timeout=8000)
        cells = pg.inner_text(".pubdns")
        check("the domain asks for one CNAME to the site",
              "CNAME" in cells and "my-bakery.netlify.app" in cells)
        check("HTTPS is not claimed before DNS exists",
              pg.locator(".pubpill.ok").count() == 0)
        check("Netlify got the domain", st["sites"][site_id]["custom_domain"] == "www.mybakery.com")
        pg.click(".pubrow button:has-text('Remove')")
        pg.wait_for_timeout(1000)
        check("removing the domain puts the netlify.app address back",
              "my-bakery.netlify.app" in pg.inner_text(".publive .url"))

        pg.click("summary:has-text('Address')")
        pg.fill("#pubren", "sweet-bakery")
        pg.click("text=Change address")
        pg.wait_for_timeout(1000)
        check("changing the address renames the site",
              "sweet-bakery.netlify.app" in pg.inner_text(".publive .url")
              and st["sites"][site_id]["name"] == "sweet-bakery")

        pg.uncheck("#pubbadge")
        pg.click("text=Publish changes")
        pg.wait_for_selector("text=Your site is live", timeout=120000)
        pg.wait_for_timeout(500)
        r2 = json.loads((HOME / "published.json").read_text())[f"project:{NAME}"]
        idx2 = st["blobs"][st["deploys"][r2["deploy_id"]]["files"]["/index.html"]]
        check("switching the badge off publishes the pages without it (beta)",
              b"aethron-badge" not in idx2 and r2["badge"] is False)
        pg.click("summary:has-text('Unpublish')")
        pg.click("text=Unpublish site")
        pg.wait_for_selector("#pubname", timeout=8000)
        check("unpublish deletes the site and returns to the address step",
              site_id not in st["sites"])
        check("the token never reached the page",
              not any(st["token"] in t for t in seen))
        check("no console errors", not errs or print(errs))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(500)
        check("Escape closes the dialog", pg.locator("#pubov").count() == 0)
        b.close()

    # the server refuses what it must
    code, body = post({"action": "publish", "kind": "project", "name": "../../etc"})
    check("a path outside the projects is refused", code == 404)
    code, body = post({"action": "status", "kind": "page", "name": "../x"})
    check("a crafted page name is refused", code == 404)
    code, body = post({"action": "disconnect"})
    code, body = post({"action": "publish", "kind": "project", "name": NAME})
    check("publishing without an account is refused with the reason",
          code == 400 and "Connect" in body.get("error", ""))
    code, body = post({"action": "connect", "token": "tok_" + "z" * 24})
    check("a token Netlify rejects is refused", code == 400)

    # a page rebuilt from a screenshot publishes as index.html + what it uses
    pgdir = HOME / "pages" / "demo-page"; pgdir.mkdir(parents=True)
    (pgdir / "site.html").write_text('<h1>Demo</h1><img src="logo.svg">')
    (pgdir / "logo.svg").write_text("<svg/>")
    (pgdir / "source.png").write_bytes(b"not shipped")
    post({"action": "connect", "token": st["token"]})
    code, body = post({"action": "publish", "kind": "page", "name": "demo-page"})
    job = body.get("job")
    for _ in range(60):
        time.sleep(0.5)
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/job?id={job}") as rr:
            j = json.loads(rr.read())
        if j.get("done"):
            break
    rec = json.loads((HOME / "published.json").read_text()).get("page:demo-page", {})
    files = st["deploys"].get(rec.get("deploy_id"), {}).get("files", {})
    check("a rebuilt page publishes as index.html with its logo, not its screenshot",
          j.get("ok") and set(files) == {"/index.html", "/logo.svg"})

    # the MCP tool, as an agent would call it
    os.environ["AETHRON_HOME"] = str(HOME)
    P.API = API
    P.disconnect()
    import forge_mcp
    r = forge_mcp.t_publish_site({"project": NAME})
    check("MCP publish without an account says so instead of publishing",
          r.startswith("NOT PUBLISHED") and r.data["published"] is False)
    P.connect_token(st["token"])
    r = forge_mcp.t_publish_site({"project": NAME, "site_name": "agent-made"})
    check("MCP publish builds, publishes and returns the live address",
          r.data.get("published") and r.data["url"] == "https://agent-made.netlify.app"
          or print(r))
finally:
    srv.terminate()
    fsrv.shutdown()
    shutil.rmtree(SP, ignore_errors=True)
print("\nVERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
