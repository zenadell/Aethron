#!/usr/bin/env python3
"""Can Aethron survive a crash with nobody watching?

WHY THIS EXISTS

Every crash in this pipeline's history was fixed by a person reading a
traceback. That is the one thing a shipped product cannot rely on: after
launch a user feeds it a template nobody has seen, something raises, and
there is no engineer attached.

So the claim under test is narrow and checkable: when a pipeline step
CRASHES, does the crash reach the agent with enough evidence to act on,
and is the result judged by the machine rather than by the model?

The expensive half — whether a given model actually fixes it — needs a
real model and real money. The cheap half is everything else, and it is
the half that was broken: the crash never reached the agent at all. That
part runs here in seconds, with no key and no network.

    python3 tests/selfheal_battery.py           (plumbing only, free)
    python3 tests/selfheal_battery.py --live    (drive the real model)
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))


# ── the input that broke a real conversion ───────────────────────────
# avenlo's own head script explains a z-index problem and the explanation
# contains the word <body>. split_document matched it and took the page
# body from the middle of the JavaScript. Kept verbatim, because a
# regression here is invisible until a whole conversion dies on it.
COMMENT_BODY_PAGE = (
    '<html lang="en"><head><script>\n'
    '(function () {\n'
    '    // a backdrop parked on <body> paints above the modal,\n'
    '    // swallowing every click meant for the player controls.\n'
    '    window.scrollTo({ top: 0, behavior: "smooth" })\n'
    '})()\n'
    '</script></head>\n'
    '<body class="real"><main><h1>Hello</h1></main></body></html>'
)


def test_body_extraction():
    """The defect that needed a person, as a unit test."""
    import aethron_convert as ac
    head, body, attrs, lang = ac.split_document(COMMENT_BODY_PAGE)
    check("body is taken from the real <body>, not a comment",
          body.strip() == "<main><h1>Hello</h1></main>",
          f"got {body.strip()[:60]!r}")
    check("body attributes survive", attrs == 'class="real"', repr(attrs))
    check("the script is NOT dragged into the body",
          "window.scrollTo" not in body)
    # and the escaper must not touch code it now correctly excludes
    out = ac.astro_markup(COMMENT_BODY_PAGE)
    check("brace escaper leaves script bodies alone",
          "window.scrollTo({ top: 0" in out,
          "braces inside <script> were entity-escaped")


def test_crash_evidence():
    """A traceback names a file and a line; the evidence must carry both."""
    import aethron_fixer as fx
    fake = (
        "Traceback (most recent call last):\n"
        f'  File "{ROOT / "aethron_convert.py"}", line 20, in split_document\n'
        "    body = re.search(...)\n"
        "ValueError: boom\n")
    ev = fx.crash_evidence(
        [sys.executable, "aethron_convert.py", "projects/x"],
        str(ROOT), 1, fake, root=ROOT)
    check("evidence carries the exact command", "aethron_convert.py" in ev)
    check("evidence carries the exit code", "EXIT CODE: 1" in ev)
    check("evidence carries the traceback", "ValueError: boom" in ev)
    check("evidence quotes OUR source around the failing line",
          "aethron_convert.py around line 20" in ev)
    check("the failing line is marked", ">>" in ev)
    # a traceback in someone else's code must say so rather than bluff
    other = ('Traceback (most recent call last):\n'
             '  File "/usr/lib/python3/json/decoder.py", line 9, in raw_decode\n'
             'ValueError: nope\n')
    ev2 = fx.crash_evidence(["x"], "/tmp", 2, other, root=ROOT)
    check("a crash outside our code is reported as such",
          "came from a tool we invoke" in ev2)

    # WHEN THE TOOL IS SOMEONE ELSE'S, THE ARTIFACT IS THE EVIDENCE.
    # Astro's traceback is entirely node_modules, so the frame scan finds
    # nothing of ours; the agent then read the pipeline blind and spent
    # two million input tokens without a fix. The error names the file we
    # GENERATED and the line — quoting it shows the defect directly.
    gen = ROOT / "tests" / "_selfheal_tmp.astro"
    gen.write_text("---\n// generated\n---\n" + "x\n" * 20 +
                   "window.scrollTo(&#123; top: 0 &#125;)\n")
    try:
        astro_err = (f"[ERROR] Unexpected \"}}\"\n  Location:\n    {gen}:24:9\n"
                     "  Stack trace:\n    at compileAstro "
                     "(file:///x/node_modules/astro/dist/compile.js:62:11)\n")
        ev3 = fx.crash_evidence(["convert"], str(ROOT), 2, astro_err, root=ROOT)
        check("a foreign tool's crash quotes the file WE generated",
              "THE GENERATED FILE IT REJECTED" in ev3)
        check("the offending generated line is shown",
              "window.scrollTo(&#123;" in ev3)
        check("node_modules frames are not quoted as our artifact",
              "compile.js" not in ev3.split("THE GENERATED")[-1])
    finally:
        gen.unlink(missing_ok=True)


def test_crash_reaches_the_agent():
    """The gap that made every crash a person's job.

    A step that exits non-zero must be handed over, not summarised into
    four lines and abandoned. Driven with a stub agent so the plumbing is
    tested without a model: what matters here is that agent_fix is CALLED
    AT ALL, with crash-shaped evidence, and that the command is retried.
    """
    import aethron_auto as auto
    import aethron_fixer as fx

    seen = {}
    calls = {"n": 0}

    def stub(project, build, check_name, evidence, cfg=None, timeout=1800,
             task=None):
        calls["n"] += 1
        seen.update(build=build, check=check_name, evidence=evidence,
                    task=task)
        return {"ok": True, "error": None, "text": "stub", "tools": ["Edit"],
                "cost_usd": 0.0}

    real_fix, real_git, real_corpus = fx.agent_fix, auto.git, auto.corpus_clear
    fx.agent_fix = stub
    auto.git = lambda *a: type("R", (), {"stdout": " M forge.py\n"})()
    auto.corpus_clear = lambda: (True, "")
    try:
        script = ("import sys;"
                  "sys.stderr.write('Traceback (most recent call last):\\n"
                  '  File \\"%s\\", line 3, in <module>\\n'
                  "ValueError: synthetic\\n' % r'" + str(ROOT / "forge.py") +
                  "');sys.exit(1)")
        ok, _ = auto.step_with_fix(
            "synthetic", [sys.executable, "-c", script], ROOT,
            cwd=ROOT, timeout=120, attempts=2, allow_fix=True)
        check("a crashing step is NOT reported as success", ok is False)
        check("the crash reached the agent", calls["n"] == 1,
              f"agent_fix called {calls['n']} times")
        check("it was sent the crash task, not the check task",
              seen.get("task") is fx.CRASH_TASK)
        check("the evidence handed over is crash-shaped",
              "EXIT CODE: 1" in (seen.get("evidence") or "")
              and "synthetic" in (seen.get("evidence") or ""))
    finally:
        fx.agent_fix, auto.git, auto.corpus_clear = real_fix, real_git, real_corpus


def test_refusals():
    """The rules that make an autonomous fixer safe to leave alone."""
    import aethron_auto as auto
    import aethron_fixer as fx

    reverted = {"n": 0}

    def stub(*a, **k):
        return {"ok": True, "error": None, "text": "", "tools": [],
                "cost_usd": 0.0}

    def fake_git(*args):
        if args[:1] == ("checkout",):
            reverted["n"] += 1
        return type("R", (), {"stdout": " M aethron_bridge.py\n"})()

    real_fix, real_git = fx.agent_fix, auto.git
    fx.agent_fix, auto.git = stub, fake_git
    try:
        script = "import sys;sys.stderr.write('boom\\n');sys.exit(1)"
        auto.step_with_fix("synthetic", [sys.executable, "-c", script], ROOT,
                           cwd=ROOT, timeout=120, attempts=2, allow_fix=True)
        check("editing the spend limits is refused and reverted",
              reverted["n"] >= 1)
    finally:
        fx.agent_fix, auto.git = real_fix, real_git

    check("the referee is not editable by the agent",
          "aethron_auto.py" in auto.GUARDED)
    check("the corpus gate is not editable by the agent",
          "aethron_corpus.py" in auto.GUARDED)
    check("the pipeline IS editable", "forge.py" in fx.EDITABLE
          and "aethron_convert.py" in fx.EDITABLE)


def test_no_fix_mode_stays_honest():
    """--no-fix must report the crash, never quietly paper over it."""
    import aethron_auto as auto
    script = "import sys;sys.stderr.write('boom\\n');sys.exit(3)"
    ok, _ = auto.step_with_fix("synthetic", [sys.executable, "-c", script],
                               ROOT, cwd=ROOT, timeout=120, attempts=2,
                               allow_fix=False)
    check("with fixing disabled a crash is still a crash", ok is False)


def main():
    print("SELF-HEAL BATTERY — can a crash be handled with nobody watching?\n")
    test_body_extraction()
    test_crash_evidence()
    test_crash_reaches_the_agent()
    test_refusals()
    test_no_fix_mode_stays_honest()
    print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"     FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
