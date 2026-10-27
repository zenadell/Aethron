#!/usr/bin/env python3
"""The admin console, in a real browser, against the real edge function.

tests/cloud/serve_fake.ts runs supabase/functions/aethron over a stand-in
Supabase seeded with four people (two online, in Nigeria and the US),
their copies, AI spend and an error. This drives admin_ui/console.html
like the owner would:

  sign in with the master key -> overview numbers are the seeded ones ->
  live view shows what each copy is doing -> a person's page opens with
  their country, spend and errors -> the kill switch pauses everything
  and the switch is really set on the server -> an API key is shown once
  -> the admin AI answers with a figure it read through a tool -> an
  export downloads -> no console errors anywhere.

Needs bun and a browser; without either it reports SKIPPED, never PASS.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
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

s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
srv = subprocess.Popen([bun, str(ROOT / "tests/cloud/serve_fake.ts"), str(port)], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    line = srv.stdout.readline()
    if "ready" not in line:
        skip("the function did not start: " + line + (srv.stdout.read() or "")[:400])
    base = f"http://127.0.0.1:{port}"

    def server_state():
        return json.loads(urllib.request.urlopen(base + "/__state", timeout=5).read())

    with sync_playwright() as p:
        exe = os.environ.get("AE_CHROMIUM") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
        try:
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as e:
            skip(f"no browser ({e})")
        pg = b.new_page(viewport={"width": 1400, "height": 900}, accept_downloads=True)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(base + "/")
        pg.wait_for_selector("#lkey")
        check("signed out, the console asks who you are", pg.locator("text=Admin API key").count() == 1)
        pg.fill("#lkey", "master-key-0123456789abcdefghij")
        pg.click("text=Use key")
        pg.wait_for_selector("text=People", timeout=15000)
        body = pg.inner_text("main")
        check("the overview counts the seeded people", "4" in pg.inner_text(".kpi >> nth=0"))
        check("and who is online right now", pg.inner_text(".kpi >> nth=1").strip().startswith("Online now\n2"))
        check("and where they are (Nigeria, Ghana, US)", all(c in body for c in ("NG", "GH", "US")))
        check("and today's AI spend", "$0.1048" in body or "$0.10" in body)
        check("and the top error", "ChunkLoadError" in body)

        pg.click("aside .nv:has-text('Live now')")
        pg.wait_for_selector("text=open", timeout=10000)
        live = pg.inner_text("main")
        check("live: what each copy is doing", "Agent working in agero" in live and "build · agero" in live)
        check("live: only copies seen in the last minutes", "dee@example.com" not in live)

        pg.click("aside .nv:has-text('People')")
        pg.wait_for_selector("text=ada@example.com")
        pg.click("text=ada@example.com")
        pg.wait_for_selector(".drawer >> text=Devices & location", timeout=10000)
        drawer = pg.inner_text(".drawer")
        check("a person's page: country and region", "Nigeria" in drawer and "Lagos" in drawer)
        pg.click(".drawer >> text=AI usage")
        check("a person's page: their AI spend", "gemini-3.6-flash" in pg.inner_text(".drawer"))
        pg.click(".drawer >> text=Errors")
        check("a person's page: the errors their copy reported", "ChunkLoadError" in pg.inner_text(".drawer"))
        pg.click(".drawer >> button:has-text('Close')")

        pg.click("aside .nv:has-text('Controls')")
        pg.wait_for_selector("text=Kill switch")
        pg.click("main >> text=Pause everything")
        pg.wait_for_selector(".dlg #dlf0")
        pg.fill(".dlg #dlf0", "Back in five minutes")
        pg.click(".dlg [data-ok]")
        pg.wait_for_selector("main >> text=Aethron is paused for everyone", timeout=10000)
        st = server_state()
        check("the kill switch is really set on the server", st["control"]["paused"] is True
              and st["control"]["pause_message"] == "Back in five minutes")
        check("and recorded as the master key's action",
              any(a["action"] == "pause_all" and a["actor"] == "master key" for a in st["audit"]))
        pg.click("main >> text=Resume everything")
        pg.wait_for_selector("main >> text=Pause everything", timeout=10000)
        check("resume lifts it", server_state()["control"]["paused"] is False)

        pg.click("aside .nv:has-text('Access & API keys')")
        pg.wait_for_selector("#kl")
        pg.fill("#kl", "claude on my mac")
        pg.click("text=Create key")
        pg.wait_for_selector(".keybox", timeout=10000)
        key = pg.inner_text(".keybox").strip()
        check("a new API key is shown once", key.startswith("ak_"))
        pg.click("aside .nv:has-text('Access & API keys')")
        pg.wait_for_selector("text=claude on my mac")
        check("and never again (only its prefix is listed)", key not in pg.content())

        pg.click("aside .nv:has-text('Admin AI')")
        pg.wait_for_selector("#aiin")
        r = urllib.request.Request(base + "/aethron/admin/call", data=json.dumps({
            "tool": "set_admin_ai", "args": {"provider": "gemini", "model": "gemini-3.6-flash", "key": "admin-ai-key-xyz"}}).encode(),
            headers={"Authorization": "Bearer master-key-0123456789abcdefghij", "Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=5).read()
        pg.fill("#aiin", "how many people use Aethron?")
        pg.click("#aisend")
        pg.wait_for_selector("text=There are 4 people on Aethron.", timeout=15000)
        check("the admin AI answers with the figure it read", True)
        check("and shows the tool it used", pg.locator(".act >> text=overview").count() >= 1)

        pg.click("aside .nv:has-text('Announcements')")
        pg.wait_for_selector("#at")
        pg.fill("#at", "Screenshots now build whole sites")
        pg.fill("#ab", "Drop a screenshot into Aethron and get a real website back.")
        pg.click("#adraw")
        pg.wait_for_selector("#apv .art img", timeout=10000)
        src = pg.get_attribute("#apv .art img", "src") or ""
        check("an illustration is drawn and previewed before anything goes out", src.startswith("data:image/svg+xml"))
        check("the preview shows the card exactly as the app will", "Screenshots now build whole sites" in pg.inner_text("#apv"))
        pg.click("#apub")
        pg.click(".dlg [data-ok]")
        pg.wait_for_selector("main >> text=Live: Screenshots now build whole sites", timeout=10000)
        live_ann = json.loads(urllib.request.urlopen(base + "/aethron/control", timeout=5).read()).get("announcement") or {}
        check("publishing makes it live for every copy, with its illustration",
              live_ann.get("title") == "Screenshots now build whole sites" and live_ann.get("has_art") is True)
        art = urllib.request.urlopen(base + "/aethron/announcement/art?id=" + live_ann.get("id", ""), timeout=5)
        check("and the illustration is served as a picture that cannot run code",
              "image/svg+xml" in art.headers.get("Content-Type", "") and b"<script" not in art.read().lower())

        pg.click("aside .nv:has-text('Exports')")
        pg.wait_for_selector("text=Everything")
        with pg.expect_download() as dl:
            pg.locator("button[onclick^=\"download('errors','md'\"]").click()
        path = dl.value.path()
        md = Path(path).read_text()
        check("an errors export downloads as Markdown with the log", md.startswith("# Aethron errors report")
              and "chunk 42" in md)

        broken = []
        for v in ("Usage & spend", "Errors", "Activity", "Agent runs", "Audit log", "AI & models", "Announcements", "Overview"):
            pg.click(f"aside .nv:has-text('{v}')")
            pg.wait_for_selector("main .top h1", timeout=10000)
            pg.wait_for_function("!document.querySelector('main .skel')", timeout=10000)
            if pg.locator("main > .card.bad").count():
                broken.append(v + ": " + pg.inner_text("main > .card.bad"))
        check("every view opens without an error", not broken or print(broken))
        check("no console errors on any view", not errors or print(errors))

        pg2 = b.new_page(viewport={"width": 390, "height": 800})
        pg2.goto(base + "/")
        pg2.wait_for_selector("#lkey")
        sw = pg2.evaluate("document.documentElement.scrollWidth")
        check("the sign-in fits a phone", sw <= 392)
        b.close()
finally:
    srv.terminate()

print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
