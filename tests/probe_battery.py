#!/usr/bin/env python3
"""Regression battery for `forge.py probe` — runtime verification and
the framework-port referee.

Every scenario is a REAL failure the probe exists to catch, injected
into a real built project and then repaired.

    python3 tests/probe_battery.py [project-dir]

Default project: projects/acme-demo (Framer, static-host hardened,
known good). Lives in the repo on purpose: the previous two copies of
this file were written to a scratch directory and lost.
"""
import json
import re
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORGE = ROOT / "forge.py"
PROJ = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 \
    else ROOT / "projects/acme-demo"

results = []


def probe(*args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run([sys.executable, str(FORGE), "probe", *args],
                       cwd=PROJ, capture_output=True, text=True, env=e,
                       timeout=900)
    return r.returncode, r.stdout + r.stderr


def check(name, cond, detail=""):
    results.append((name, cond))
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   {detail}" if not cond and detail else ""))


def scenario(title):
    print(f"\n── {title}")


def main():
    if not (PROJ / "site").is_dir():
        print(f"SKIPPED — {PROJ} has no site/ (build it first)")
        return 0

    scenario("healthy build probes CLEAN")
    rc, out = probe("--page=index.html")
    check("exit 0", rc == 0, f"rc={rc}")
    check("verdict CLEAN at runtime", "CLEAN at runtime" in out)
    check("renders text", "PASS renders" in out)
    check("all requests served", "runtime request(s) served" in out)
    rep = json.loads((PROJ / "site/.forge-probe.json").read_text())
    page = rep["pages"][0]
    check("report has rendered_text", page["rendered_text"] > 500)
    check("report records requests", page["requests"] > 5)
    check("report carries no raw page text",
          not any(k.startswith("_") for k in page))

    scenario("known export artifacts stay NOTEs, never FAILs")
    check("React #418 is not fatal",
          "console error(s) — code or assets failed" not in out)

    scenario("corrupt chunk: the file scan passes, the runtime fails")
    target = None
    index = (PROJ / "site/index.html").read_text(errors="ignore")
    for c in sorted((PROJ / "site/assets/chunks").glob("*.mjs")) \
            if (PROJ / "site/assets/chunks").is_dir() else []:
        if c.name in index:
            target = c
            break
    if not target:
        print("  (no chunk referenced by index.html — not applicable)")
    else:
        backup = target.read_bytes()
        try:
            # A CHUNK THAT DOES NOT PARSE IS NOW CAUGHT ON DISK. verify
            # gained a syntax check after a rebrand rewrote a bare object
            # key (`Annual:` -> `Complete Sets:`) and shipped a chunk that
            # could not parse: every file was present, the brand was gone,
            # verify said CLEAN and the homepage rendered 126 characters.
            # This scenario used to assert that CLEAN — the old limitation
            # written down as an expectation.
            target.write_bytes(backup + b"\nconst boom = (((;\n")
            v = subprocess.run([sys.executable, str(FORGE), "verify"],
                               cwd=PROJ, capture_output=True, text=True)
            check("verify now catches a chunk that cannot parse",
                  "does not parse" in v.stdout or "SKIPPED chunk syntax"
                  in v.stdout, v.stdout.strip().splitlines()[-1][:80])
            rc, out = probe("--page=index.html")
            check("probe exits nonzero", rc != 0, f"rc={rc}")
            check("parse failure reported as fatal",
                  "code or assets failed to load" in out)
            check("wiped page detected",
                  "content DISAPPEARS after JS" in out or
                  "renders BLANK" in out)
        finally:
            target.write_bytes(backup)

        # THE CASE ONLY A BROWSER CAN SEE. The syntax check above closes
        # one hole, and closing it must not be mistaken for making probe
        # optional: code that parses perfectly and then throws is
        # invisible to every file-level check there is.
        scenario("valid syntax, throws at runtime: only the browser knows")
        try:
            target.write_bytes(backup + b'\nthrow new Error("boom");\n')
            v = subprocess.run([sys.executable, str(FORGE), "verify"],
                               cwd=PROJ, capture_output=True, text=True)
            check("verify says CLEAN — it parses, so files look fine",
                  "does not parse" not in v.stdout)
            rc, out = probe("--page=index.html")
            check("probe still exits nonzero", rc != 0, f"rc={rc}")
            check("the runtime failure is reported",
                  "code or assets failed to load" in out or
                  "content DISAPPEARS after JS" in out or
                  "renders BLANK" in out)
        finally:
            target.write_bytes(backup)

    scenario("an asset the CODE asks for goes missing")
    rep = json.loads((PROJ / "site/.forge-probe.json").read_text())
    victim = None
    for path in rep["pages"][0].get("requested", []):
        f = PROJ / "site" / path.split("?")[0].lstrip("/")
        if f.is_file() and f.suffix.lower() in (".js", ".mjs", ".css",
                                                ".png", ".jpg", ".svg",
                                                ".woff2"):
            victim = f
            break
    if not victim:
        print("  (nothing suitable was requested — not applicable)")
    else:
        hidden = victim.with_suffix(victim.suffix + ".hidden")
        try:
            victim.rename(hidden)
            rc, out = probe("--page=index.html")
            check("failed request reported with its status", "404" in out)
            check("points at capture", "forge.py capture" in out)
            check("nonzero exit", rc != 0)
        finally:
            hidden.rename(victim)

    scenario("no browser must SKIP, never PASS")
    rc, out = probe("--page=index.html", env={"AETHRON_BROWSER": "none"})
    check("verdict SKIPPED", "VERDICT: SKIPPED" in out)
    check("says UNVERIFIED", "UNVERIFIED" in out)
    check("never claims clean", "CLEAN at runtime" not in out)
    check("exit 0 (an absent check is not a failure)", rc == 0, f"rc={rc}")

    scenario("the framework-port referee (--baseline / --against)")
    rc, out = probe("--page=index.html", "--baseline")
    check("baseline written",
          (PROJ / ".forge-baseline.json").is_file() and rc == 0)
    base = json.loads((PROJ / ".forge-baseline.json").read_text())
    fp = list(base["pages"].values())[0]
    check("baseline is a READER-level fingerprint",
          fp["words"] > 50 and len(fp["headings"]) > 0)
    rc, out = probe("--page=index.html", "--against=site")
    # NOT an exact 100%. Measured: a self-comparison of this page
    # scores 99% on most runs and 100% occasionally, with UNCHANGED
    # code — the page animates (counters, marquees), so two renders of
    # the same build legitimately differ by a word or two. Asserting
    # "100%" made this test pass or fail on animation timing, which is
    # a test that is not measuring what it claims. The real invariant
    # is that a build compared against ITSELF must clear the referee's
    # own bar comfortably.
    m = re.search(r"text (\d+)% identical", out)
    pct = int(m.group(1)) if m else -1
    check("a build compared against itself scores at least 98%",
          pct >= 98, f"got {pct}% — out: {out[:120]}")
    # THE GATE IS TESTED, NOT THE FIXTURE'S MOOD.
    #
    # This used to assert that comparing acme-demo with itself was
    # REFUSED as un-owned, on the belief that the fixture had never
    # been localized. It has: its served pages contain zero platform
    # asset references (the 32 that grep finds live only in
    # .forge-report.json, a metadata file nothing fetches). So the gate
    # was correctly refusing nothing, and the test failed the product
    # for being right.
    #
    # Concluding "nothing to refuse, therefore fine" would be the other
    # error — a check that passes because it CANNOT fail. So plant a
    # real CDN dependency and prove the gate catches it.
    idx = PROJ / "site" / "index.html"
    original = idx.read_bytes()
    try:
        idx.write_bytes(original.replace(
            b"</body>",
            b'<img src="https://framerusercontent.com/images/planted.png">'
            b"</body>", 1))
        rc_o, out_o = probe("--page=index.html", "--against=site")
        check("a planted CDN dependency is REFUSED as not owned",
              rc_o == 1 and "NOT OWNED" in out_o,
              f"rc={rc_o} — the ownership gate did not fire")
        check("and it names the host the page still depends on",
              "framerusercontent.com" in out_o, out_o[-200:])
    finally:
        idx.write_bytes(original)
    rc, out = probe("--page=index.html", "--against=site")
    check("a genuinely owned project is NOT refused",
          rc == 0 and "NOT OWNED" not in out, f"rc={rc}")
    check("the original's own runtime health is not counted as drift",
          "do not count against the comparison" in out
          or "runtime problem(s) of" not in out)

    naive = Path(tempfile.mkdtemp(prefix="naive-port-"))
    (naive / "index.html").write_text(
        "<!doctype html><html><body><h1>"
        + (fp["headings"][0] if fp["headings"] else "Home")
        + "</h1><p>only a fraction of the copy</p></body></html>",
        encoding="utf-8")
    rc, out = probe("--page=index.html", f"--against={naive}")
    check("a lossy port FAILS", rc != 0 and "FAIL index.html" in out)
    check("it says how much text survived", "% identical" in out)
    check("it names the missing headings",
          "missing heading" in out or len(fp["headings"]) <= 1)

    scenario("project restored")
    rc, out = probe("--page=index.html")
    check("CLEAN again", rc == 0 and "CLEAN at runtime" in out)

    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} green")
    if bad:
        print("FAILED: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
