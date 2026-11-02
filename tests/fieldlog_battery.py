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


def check(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
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

print(f"\n{ok}/{ok + bad} field-log checks passed")
sys.exit(1 if bad else 0)
