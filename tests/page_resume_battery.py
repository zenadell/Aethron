"""One screenshot request is one page, continued — never restarted.

The owner's sidebar filled with <hash>, <hash>-2, … for ONE request: every
call to screenshot_to_page made a new folder and ran the whole build again,
and the tool told the agent to call it again until PASS. This drives the
real MCP tool with a fake model and counts what a person pays for:

  - the same screenshot (any file name) continues the SAME page folder
  - a second call grades the page first and repairs it; it never sends
    the from-nothing prompt again
  - a page that already passes costs nothing to ask about again
  - one request is held to REQUEST_CALLS model calls in total, then DONE
  - a provider that failed leaves a page that the next call continues
  - a busy page is not built twice at once
  - new=true, and only that, makes a second page
  - pages made before this record are found and continued

Needs a browser (grading renders the page); without one it is SKIPPED.
"""
import json, os, pathlib, shutil, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SP = pathlib.Path(tempfile.mkdtemp(prefix="ae-resume-"))
os.environ["AETHRON_HOME"] = str(SP / "home")
os.environ["AETHRON_REPORTS"] = "off"
os.environ["AETHRON_RUNLOG"] = "off"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import forge_mcp as M
import aethron_shot2code as S
import aethron_brain as BR
import aethron_build as B
import aethron_figma_grade as G

ok = True


def check(n, c):
    global ok
    ok &= bool(c)
    print(("PASS " if c else "FAIL ") + n, flush=True)


if not G.find_browser():
    print("VERDICT: SKIPPED — no browser to grade pages with")
    sys.exit(0)

REF = ROOT / "tests" / "pages" / "recognito.png"
CRAFTED = (ROOT / "tests" / "pages" / "recognito-crafted.html").read_text()
BLACK = ("<!doctype html><html><body style='margin:0;background:#000'>"
         "<a href=#>x</a></body></html>")
BR.resolve = lambda *a, **k: {"ready": True, "wire": "openai", "provider": "fake"}

calls = []
MODE = {"reply": BLACK, "dead": False}


def fake_writer(budget_usd=0.40, ledger=None, max_tokens=0):
    ledger = {} if ledger is None else ledger

    def write(prompt, images):
        if MODE["dead"]:
            raise B.BudgetExhausted("the provider did not answer")
        calls.append(prompt)
        ledger["calls"] = ledger.get("calls", 0) + 1
        ledger["usd"] = ledger.get("usd", 0.0) + 0.01
        return "```html\n" + MODE["reply"] + "\n```"
    write.ledger = ledger
    return write


S.model_writer = fake_writer
PAGES = SP / "home" / "pages"
up = SP / "home" / "uploads"
up.mkdir(parents=True)
shot = up / "68fdf834b20ecf0d.png"
shutil.copy(REF, shot)
copy = up / "pasted again.png"           # same picture, another file
shutil.copy(REF, copy)


def folders():
    return sorted(p.name for p in PAGES.iterdir() if p.is_dir()) if PAGES.is_dir() else []


def first_prompts():
    return sum(1 for p in calls if not p.startswith("Round"))


# 1. a page that keeps failing: one request, then the next calls continue it
M.REQUEST_CALLS = 4
r1 = M.t_screenshot_to_page({"image": str(shot), "rounds": 2})
check("the first call makes one page", folders() == ["68fdf834b20ecf0d"])
check("the first call writes it from the screenshot once", first_prompts() == 1)
n1 = len(calls)
r2 = M.t_screenshot_to_page({"image": str(copy), "rounds": 2})
check("the same screenshot under another name continues the SAME page",
      folders() == ["68fdf834b20ecf0d"])
check("the second call says it continued instead of starting again",
      "continued the page already built" in r2)
check("the second call only repairs: no from-nothing prompt is sent again",
      first_prompts() == 1 and len(calls) > n1
      and all(p.startswith("Round") for p in calls[n1:]))
check("one request is held to its call budget across calls", len(calls) <= M.REQUEST_CALLS)
n2 = len(calls)
r3 = M.t_screenshot_to_page({"image": str(shot)})
check("once the budget is spent the tool answers DONE with the best page",
      r3.startswith("DONE") and "aethron-page: 68fdf834b20ecf0d" in r3)
check("and spends nothing more", len(calls) == n2)
st = json.loads((PAGES / "68fdf834b20ecf0d" / "request.json").read_text())
check("the request record counts every call made for it", st["calls"] == n2)

# 2. a later request on the same page gets a fresh budget, still the same page
st["last"] = time.time() - M.REQUEST_GAP_S - 5
(PAGES / "68fdf834b20ecf0d" / "request.json").write_text(json.dumps(st))
MODE["reply"] = CRAFTED
M.t_screenshot_to_page({"image": str(shot), "rounds": 1})
check("a later request continues the same page with a fresh budget",
      len(calls) == n2 + 1 and folders() == ["68fdf834b20ecf0d"])
site = PAGES / "68fdf834b20ecf0d" / "site.html"
check("the repair that did better is the page kept", "Apply for Beta" in site.read_text())

# 3. a page that already passes costs nothing to ask about again
rep = json.loads((PAGES / "68fdf834b20ecf0d" / "report.json").read_text())
n3 = len(calls)
r4 = M.t_screenshot_to_page({"image": str(shot)})
if rep.get("verdict") == "PASS":
    check("asking again about a passing page makes no model call",
          len(calls) == n3 and "PASS" in r4)
else:
    check("asking again about the page continues it (it does not pass yet)",
          folders() == ["68fdf834b20ecf0d"])

# 4. busy: a second call while the first is still building waits
(PAGES / "68fdf834b20ecf0d" / ".building").write_text("1")
n4 = len(calls)
r5 = M.t_screenshot_to_page({"image": str(shot)})
check("a page still being built is not built twice", r5.startswith("BUSY") and len(calls) == n4)
(PAGES / "68fdf834b20ecf0d" / ".building").unlink()

# 5. new=true makes a second page, and only then
M.t_screenshot_to_page({"image": str(shot), "new": True, "rounds": 1})
check("new=true, and only that, makes a second page",
      folders() == ["68fdf834b20ecf0d", "68fdf834b20ecf0d-2"])

# 6. a provider that fails: the next call continues the same folder
other = up / "dispatch.png"
shutil.copy(ROOT / "tests" / "pages" / "dispatch.png", other)
MODE["dead"] = True
r6 = M.t_screenshot_to_page({"image": str(other)})
check("a provider that did not answer is NOT ASKED", r6.startswith("NOT ASKED"))
before = folders()
MODE["dead"] = False
MODE["reply"] = BLACK
M.t_screenshot_to_page({"image": str(other), "rounds": 1})
check("the next call after a failure uses the same page folder", folders() == before)

# 7. a page from before this record (no source.json) is found by its report
legacy = PAGES / "476bd1c2accd0538"
legacy.mkdir()
(legacy / "site.html").write_text(CRAFTED)
(legacy / "report.json").write_text(json.dumps({"image": str(REF), "verdict": "FAIL"}))
for d in PAGES.iterdir():
    if d != legacy:
        os.utime(d, (1, 1))
        try:
            (d / "source.json").unlink()
            (d / "report.json").unlink()
        except OSError:
            pass
r7 = M.t_screenshot_to_page({"image": str(shot), "rounds": 1})
check("a page made before this record is found and continued",
      "aethron-page: 476bd1c2accd0538" in r7 and "continued" in r7)

# 8. the tool tells the agent to call once
desc = next(t[1] for t in M.TOOLS if t[0] == "screenshot_to_page")
check("the tool no longer asks to be called again until PASS",
      "ONCE per screenshot" in desc)

shutil.rmtree(SP, ignore_errors=True)
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
