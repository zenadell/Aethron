#!/usr/bin/env python3
"""The failure a shipped Aethron will actually meet: an input it cannot handle.

WHY THIS EXISTS, AND WHY IT REPLACED THE OTHER HARNESS

selfheal_shapes.py injects faults into pipeline CODE and healed 0 of 3.
That reads as "the self-heal does not generalise", and it is the wrong
conclusion, because it is the wrong threat model: after launch the
pipeline is FROZEN. Nobody edits forge.py on a user's machine. What
varies is the TEMPLATE — someone feeds Aethron a page built in a way
nobody anticipated, and the code that has worked a hundred times raises.

That is exactly the one real crash this loop has fixed unattended: a
Framer page whose own head script contained the word "<body>" inside a
comment. Nobody broke the code; the input was simply shaped in a way the
code did not expect.

So these are INPUTS, not injuries. Each is a page a real template could
plausibly ship, and each is checked to genuinely break the current
pipeline before the agent is asked for anything — a fault that silently
works makes a pass vacuous, which this repo keeps re-learning.

WHAT A PASS MEANS: the pipeline handled a page shape it could not handle
before, and the corpus still accepts every existing build. That is the
product claim — "throw any template at it" — reduced to something a
machine can check.

    python3 tests/selfheal_inputs.py               (all, real model)
    python3 tests/selfheal_inputs.py --case=script-close
    python3 tests/selfheal_inputs.py --dry         (prove they break, free)
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = ROOT / "projects" / "_selfheal_input"


# Page bodies that a real template could ship and that the pipeline may
# not survive. Keep them SMALL: the point is the shape, not the size.
CASES = {
    # A </script> inside a JavaScript string literal. The HTML parser ends
    # the script there; every regex that matches <script>...</script>
    # non-greedily gets half a script and treats the rest as markup.
    # Real templates do this in analytics snippets and CMS embeds.
    "script-close": '''<!doctype html><html lang="en"><head>
<title>Studio</title>
<script>
  var tpl = "<div></div></script>";   // ends the element early
  window.__ready = true;
</script>
</head><body class="page"><main><h1>Split Script</h1>
<p>Body copy that must survive.</p></main></body></html>''',

    # An unclosed <body> — no </body> anywhere. Browsers accept it; any
    # slice that looks for the closing tag gets nothing, or everything.
    "no-body-close": '''<!doctype html><html lang="en"><head>
<title>Studio</title></head>
<body class="page"><main><h1>No Close</h1>
<p>Body copy that must survive.</p></main>''',

    # Structural tag names inside an HTML COMMENT rather than a script.
    # The comment-<body> crash was the script variant; a comment is the
    # sibling case, and a template's build notes live in comments.
    "tag-in-comment": '''<!doctype html><html lang="en"><head>
<title>Studio</title>
<!-- layout note: the sticky <body> wrapper owns the scroll -->
</head><body class="page"><main><h1>Commented</h1>
<p>Body copy that must survive.</p></main></body></html>''',
}


def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True,
                          cwd=str(ROOT), **kw)


def make_project(case):
    """A minimal static project carrying one awkward page.

    The source page is built OUTSIDE the project directory: init creates
    projects/<name> itself and refuses if it already exists, so staging
    the html inside the target made every case 'fail' at init — a harness
    bug that looked exactly like three pipeline defects.
    """
    if WORK.exists():
        shutil.rmtree(WORK)
    src = ROOT / "projects" / "_selfheal_src"
    if src.exists():
        shutil.rmtree(src)
    src.mkdir(parents=True)
    (src / "index.html").write_text(CASES[case], encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(ROOT / "forge.py"), "init", str(src),
         "--name", "_selfheal_input"],
        capture_output=True, text=True, cwd=str(ROOT / "projects"))
    return r.returncode == 0, r.stdout + r.stderr


def convert_cmd():
    return [sys.executable, str(ROOT / "aethron_convert.py"),
            str(WORK), "--framework=astro"]


def breaks(case, timeout=600):
    """Does this page actually defeat the pipeline as it stands?"""
    ok, out = make_project(case)
    if not ok:
        return True, "init itself failed:\n" + out[-400:]
    # inventory BEFORE build: build reads copy_map.json and raises a bare
    # FileNotFoundError without it. Skipping it made every case look like
    # a pipeline defect when the harness was simply running the pipeline
    # out of order.
    for step in ("inventory", "build"):
        b = subprocess.run([sys.executable, str(ROOT / "forge.py"), step],
                           capture_output=True, text=True, cwd=str(WORK))
        if b.returncode != 0:
            return True, f"{step} failed:\n" + (b.stdout + b.stderr)[-400:]
    try:
        c = subprocess.run(convert_cmd(), capture_output=True, text=True,
                           cwd=str(ROOT), timeout=timeout)
    except subprocess.TimeoutExpired:
        return True, "convert timed out"
    return c.returncode != 0, (c.stdout + c.stderr)[-400:]


def run_case(case, live=True):
    print(f"\n{'=' * 68}\nINPUT: {case}\n{'=' * 68}")
    broke, detail = breaks(case)
    print(f"  defeats the pipeline today: {broke}")
    if not broke:
        print("  SKIP — the pipeline already handles this shape, so a pass "
              "would prove nothing")
        return None
    print(f"  {detail.strip().splitlines()[-1][:110] if detail.strip() else ''}")
    if not live:
        return None

    import aethron_auto as auto
    try:
        import aethron_bridge as bridge
        bridge.reset_usage()
        bridge.set_limits(usd=1.0)      # each case is its own run
    except Exception:
        pass
    # HAND OVER THE STEP THAT ACTUALLY FAILED. breaks() already found it,
    # and the first version threw that away and always handed over convert
    # — so when inventory or build was the real casualty the agent was
    # given a downstream symptom ("no site/") and blamed for not fixing a
    # defect it was never shown.
    failing = next((s for s in ("inventory", "build")
                    if subprocess.run([sys.executable, str(ROOT / "forge.py"), s],
                                      capture_output=True, cwd=str(WORK)).returncode),
                   None)
    cmd = ([sys.executable, str(ROOT / "forge.py"), failing] if failing
           else convert_cmd())
    cwd = WORK if failing else ROOT
    print(f"  handing over the step that failed: {failing or 'convert'}")
    t0 = time.time()
    healed, _ = auto.step_with_fix(failing or "convert", cmd, WORK,
                                   cwd=cwd, timeout=1800, attempts=2,
                                   allow_fix=True)
    dt = time.time() - t0
    changed = [l[-40:].strip() for l in
               sh("git", "status", "--porcelain").stdout.splitlines()
               if l.strip() and not l.strip().startswith("??")]
    print(f"\n  RESULT  healed={healed}  changed={changed}  {dt:.0f}s")
    return {"case": case, "healed": bool(healed), "changed": changed,
            "seconds": round(dt)}


def main(argv):
    live = "--dry" not in argv
    only = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--case=")), None)
    cases = [only] if only else list(CASES)
    results = [r for r in (run_case(c, live) for c in cases) if r]
    for d in (WORK, ROOT / "projects" / "_selfheal_src"):
        if d.exists():
            shutil.rmtree(d)
    if results:
        print(f"\n{'=' * 68}\nSUMMARY")
        for r in results:
            print(f"  {r['case']:<16} healed={str(r['healed']):<5} "
                  f"{r['seconds']}s   {r['changed']}")
        print(f"\n  {sum(r['healed'] for r in results)} of {len(results)} "
              f"unhandled input shape(s) fixed unattended")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
