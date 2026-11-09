#!/usr/bin/env python3
"""What the uploaded failure reports and agent runs (2026-11-02) showed,
held as checks. Each scenario is a fault that stopped a real migration:

  * a brand guessed from the title's first word ("Webdesign") that verify
    then demanded be removed from the site's own code — never passable;
  * a dropped Chrome connection answered "Broken pipe" on every call;
  * a line spelled a little differently ("?width=" or quotes) refused;
  * a missing argument answered "ERROR: 'project'";
  * a file the ORIGINAL site lacks, spelled %20 in one place and a space
    in the other, failed verify;
  * a hang-up (tab reloaded) reported as a crash, most of what came in.
"""
import json, os, socket, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="fieldlog-"))
os.environ.update(AETHRON_HOME=str(TMP), AETHRON_REPORTS="off",
                  AETHRON_RUNLOG="off")
import forge, forge_mcp as M, aethron_reports as R  # noqa: E402

ok = bad = 0


def check(name, cond, extra=""):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or not extra else f"   {extra}"))
    ok += bool(cond)
    bad += not cond


def call(n, a):
    r = M.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": n, "arguments": a}})
    return r["result"]["content"][0]["text"]


# 1. the brand is what the site calls itself
mk = '<meta property="og:site_name" content="MK Vision Studio">'
check("brand from host/og:site_name, not the title's first word",
      forge._brand_of("Webdesign Beverwijk | Websites op maat | MK Vision "
                      "Studio", mk, {"source_url": "https://mkvision.studio"})
      == "MK Vision")
check("a brand that opens the title still wins",
      forge._brand_of("Nexmind® - AI agency", "",
                      {"source_url": "https://nexmind.framer.website"})
      == "Nexmind")
check("a title of just 'Home' takes the host",
      forge._brand_of("Home | Plumbing", "",
                      {"source_url": "https://plumfix.framer.website"})
      == "Plumfix")

# 2. a project saved with the old wrong guess is corrected; an edited one kept
proj = TMP / "p"
(proj / "pristine").mkdir(parents=True)
(proj / "pristine" / "index.html").write_text(
    "<html><head><title>Webdesign Beverwijk | MK Vision Studio</title>"
    + mk + "</head><body>x</body></html>")
cfg = {"pages": ["index.html"], "source_url": "https://mkvision.studio",
       "forbidden_words": ["Webdesign"]}
(proj / "forge.json").write_text(json.dumps(cfg))
forge._heal_brand_word(proj, cfg)
check("a saved wrong guess is corrected",
      json.loads((proj / "forge.json").read_text())["forbidden_words"]
      == ["MK Vision"])
cfg2 = {**cfg, "forbidden_words": ["Webdesign", "Other"]}
forge._heal_brand_word(proj, cfg2)
check("a list the person edited is left alone",
      cfg2["forbidden_words"] == ["Webdesign", "Other"])

# 3. lines spelled a little differently
idx = {"strings": {"Expert work.": {"old": "Expert work."},
                   "Contact us": {"old": "Contact us"}},
       "images": {"https://x.com/a.svg?width=129": {
           "old": "https://x.com/a.svg?width=129"}}, "links": {}}
check("quotes around a line still find it",
      M._find_entry(idx, "strings", '"Expert work."')[1] is not None)
check("another ?width= still finds the image",
      M._find_entry(idx, "images", "https://x.com/a.svg")[1] is not None)
check("the right line under another section is found",
      M._find_entry(idx, "strings", "https://x.com/a.svg?width=5")[0]
      == "images")
check("a line that does not exist is still refused",
      M._find_entry(idx, "strings", "water-damage")[1] is None)
check("a refusal names the closest line", M._nearest(idx, "Contact") ==
      "Contact us")

# 4. a missing argument is named
check("a missing argument says which",
      "missing argument 'project'" in call("set_content_bulk",
                                           {"entries": []}))

# 5. the page tools take the name Aethron hands out
(TMP / "pages" / "demo").mkdir(parents=True)
(TMP / "pages" / "demo" / "site.html").write_text("<p>x</p>")
check("aethron-page: <name> resolves to its site.html",
      M.page_path("aethron-page: demo") == TMP / "pages" / "demo" /
      "site.html")
check("a placeholder image path is answered with where uploads are",
      "aethron-attached" in call("measure_screenshot",
                                 {"image": "<uploaded screenshot path>"}))

# 6. a hang-up is not a crash
try:
    raise BrokenPipeError(32, "Broken pipe")
except BrokenPipeError:
    check("a broken pipe is not reported", R.report_exception("crash",
                                                               "/edit/x/")
          == {})

# 7. Chrome's connection drops; the next open starts a fresh browser
page = TMP / "pg.html"
page.write_text("<h1>Hello</h1>")
try:
    first = call("browser_open", {"url": page.as_uri()})
    have = "Hello" in first
except Exception as e:
    have, first = False, str(e)
if not have:
    print("SKIPPED browser recovery — no browser here: " + first[:120])
else:
    b = M._BROWSER
    b.ws.sock.shutdown(socket.SHUT_RDWR)        # Chrome alive, socket gone
    time.sleep(1)
    check("a dropped connection is noticed", b.alive() is False)
    again = call("browser_open", {"url": page.as_uri()})
    check("the next open recovers instead of 'Broken pipe'",
          "Hello" in again and "Broken pipe" not in again)
    check("it is a new browser", M._BROWSER is not b)
    M._BROWSER.close()

# 8. the screen turns the engine's diagnostic into words and carries on
src = (ROOT / "studio.py").read_text()
check("the screen explains a turn cut after a step",
      "t.startsWith('[ede_diagnostic]')" in src)
check("and continues it on the same conversation",
      "String(e.text||'').startsWith('[ede_diagnostic]')" in src
      and "reconnectRun" in src)

# 9. an intro video is media that can be swapped, under every spelling
import subprocess, http.server, threading, functools
src = TMP / "vsite"; src.mkdir(); M.PROJECTS.mkdir(parents=True, exist_ok=True)
(src / "index.html").write_text('<html><head><title>Acme</title></head><body><h1>Hello there</h1>'
    '<video muted><source src="assets/media/intro.mp4" type="video/mp4"></video></body></html>')
(src / "about.html").write_text('<html><head><title>About Acme</title></head><body><h1>About us</h1>'
    '<video muted poster="../assets/media/poster.jpg"><source src="../assets/media/intro.mp4"></video></body></html>')
subprocess.run([sys.executable, str(ROOT / "forge.py"), "init", str(src), "--name", "vproj"],
               cwd=M.PROJECTS, capture_output=True, timeout=120)
vp = M.PROJECTS / "vproj"
subprocess.run([sys.executable, str(ROOT / "forge.py"), "inventory"], cwd=vp,
               capture_output=True, timeout=120)
imgs = [e["old"] for e in json.loads((vp / "copy_map.json").read_text())["images"]]
check("inventory lists the intro video", "assets/media/intro.mp4" in imgs)
check("and its other spelling", "../assets/media/intro.mp4" in imgs)
check("and the poster frame", "../assets/media/poster.jpg" in imgs)
out = call("set_content_bulk", {"project": "vproj", "build": False, "entries": [
    {"section": "images", "old": "assets/media/intro.mp4", "new": "/assets/mine.mp4"}]})
cm = json.loads((vp / "copy_map.json").read_text())
news = {e["old"]: e["new"] for e in cm["images"]}
check("filling one spelling fills its twin",
      news.get("../assets/media/intro.mp4") == "/assets/mine.mp4")
check("and nothing else", news.get("../assets/media/poster.jpg") == "")
vd = TMP / "vserve"; vd.mkdir()
(vd / "a.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(vd))
H.log_message = lambda *a: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
check("a video address is accepted as media",
      M.image_problem(f"http://127.0.0.1:{srv.server_port}/a.mp4") == "")
srv.shutdown()

# A VARIABLE IS NOT A LEFTOVER (bloop-ai: verify failed on `bloopLabel`)
code = 'const bloopLabel=1;x={bloopProp:2};y="bloopKey";'
check("a brand used only as code names is not a leftover",
      not forge._brand_in_copy(code, "Bloop"))
check("the brand in readable text still is",
      forge._brand_in_copy(code + 'z=`Powered by BloopAI`', "Bloop"))

# AN AGENT STARTED FROM A CODE FOLDER STILL FINDS THE APP'S PROJECTS
# (30 reports: "unknown project 'onward-express'" while the app had it)
app = Path(tempfile.mkdtemp()) / "Aethron"
(app / "projects" / "onward-express").mkdir(parents=True)
(app / "projects" / "onward-express" / "forge.json").write_text(
    json.dumps({"name": "onward-express", "platform": "framer", "pages": ["index.html"]}))
keep = (M.HOME, M.PROJECTS, M.LIBRARY, M.PAGES, M.EXPLICIT_HOME, M._app_home,
        os.environ.get("AETHRON_HOME"))
M._app_home = lambda: app
M.EXPLICIT_HOME = True
try:
    M.pdir("onward-express"); told = ""
except ValueError as e:
    told = str(e)
check("with a data folder set on purpose, the error says where the project is",
      "app's data" in told and str(app) in told, told)
check("and list_projects names the app's projects", "onward-express" in M.t_list_projects({}))
M.EXPLICIT_HOME = False
try:
    got = M.pdir("onward-express")
except ValueError as e:
    got = e
check("taken by default, the server follows the project to the app's data",
      isinstance(got, Path) and got == (app / "projects" / "onward-express").resolve()
      and M.HOME == app.resolve(), str(got))
cwd0 = os.getcwd(); os.chdir(app / "projects" / "onward-express")
try:
    dot = M.pdir(".")
except ValueError as e:
    dot = e
os.chdir(cwd0)
check("'.' inside a project's folder is that project", isinstance(dot, Path)
      and dot.name == "onward-express", str(dot))
M.HOME, M.PROJECTS, M.LIBRARY, M.PAGES, M.EXPLICIT_HOME, M._app_home = keep[:6]
os.environ["AETHRON_HOME"] = keep[6]

# A RUN ID IS ENOUGH (agents guessed the caller: "no runs recorded for 'forge'")
import aethron_runlog as RL
rd = RL.runs_dir() / "claude-code"; rd.mkdir(parents=True, exist_ok=True)
(rd / "20261107-000000-abc123.jsonl").write_text(json.dumps({"caller": "claude-code"}) + "\n")
try:
    r = RL.get_run("forge", "20261107-000000-abc123"); found = r["run"]
except ValueError as e:
    found = str(e)
check("a run named by its id is found under whichever caller holds it",
      found == "20261107-000000-abc123", found)
try:
    RL.get_run("forge"); said = ""
except ValueError as e:
    said = str(e)
check("and an unknown caller is told which callers have runs", "claude-code" in said, said)

print(f"\n{ok}/{ok + bad} field-log checks passed")
sys.exit(1 if bad else 0)
