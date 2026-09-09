#!/usr/bin/env python3
"""ADVERSARIAL: what does a BROKEN site have to look like to get a
CLEAN probe?

WHY THIS EXISTS
---------------
tests/probe_battery.py proves the probe catches the damage it was
written to catch: blank pages, 404s, module errors. That is the
author's imagination again, and the same method that beat the auditor
eleven times to one applies here — ask the opposite question.

The probe is the last check before a site is handed to a user. verify
reads files; the probe is the only thing that claims to know what a
BROWSER does. If it can be fooled, every green above it is decoration.

Each scenario is a site that a human would call obviously broken. The
suite asserts the probe REFUSES it. Whatever passes is printed as a
hole, in the probe's own words, because a check that cannot see a
catastrophe should say so out loud rather than be quietly excluded
from a count.

    python3 tests/probe_adversary.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CAUGHT, HOLES, SKIPPED = [], [], []

# Holes that are NAMED and accepted, with the reason. Anything that
# gets through and is NOT in here fails the suite — that is the
# regression contract. A hole you have decided to live with is a
# decision; a hole you did not notice is a lie waiting to happen.
KNOWN_HOLES = {
    "white text on a white background":
        "contrast is a different discipline, and the obvious rule is "
        "wrong: white text over a hero image or a dark section is "
        "ordinary good design, so a colour check would fail real "
        "templates constantly. A check that cries wolf gets ignored, "
        "and then it protects nothing at all.",
    "every image on the page is gone":
        "the plain probe has no baseline — a page with no images is not "
        "damaged, it is a page with no images. Knowing that a PORT lost "
        "the original's images needs the original to compare against, "
        "which is exactly what `probe --against` does and what it "
        "already refuses ports over.",
    "the page swallows its own errors":
        "window.onerror returning true is legitimate — every error "
        "reporter in production does it. The probe watches the console "
        "from outside the page and a page can always close that window "
        "from inside. Named rather than papered over: the request log "
        "and the paint measurement are the signals that do not depend "
        "on the page's cooperation.",
}

# Enough real text that no length rule can fire for the wrong reason:
# every scenario below must be judged on the damage it does, not on
# being too small to bother with.
BODY = ("Aethron ports templates without recreating them. " * 40)

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Adversary</title><style>{css}</style></head>
<body>{attr}
<h1>Own your template</h1>
<p>{body}</p>
{extra}
</body></html>"""


def make_site(d: Path, css="", attr="", extra="", pages=("index.html",)):
    """A minimal but REAL forge project the probe will accept."""
    site = d / "site"
    site.mkdir(parents=True)
    for p in pages:
        (site / p).write_text(PAGE.format(css=css, attr=attr, extra=extra,
                                          body=BODY))
    (d / "forge.json").write_text(json.dumps(
        {"name": d.name, "platform": "static", "pages": list(pages)}))
    return d


def run_probe(d: Path):
    """(clean?, output). CLEAN means the probe handed this site over."""
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, str(ROOT / "forge.py"), "probe"],
        cwd=str(d), capture_output=True, text=True, timeout=300, env=env)
    txt = out.stdout + out.stderr
    clean = out.returncode == 0 and "VERDICT: CLEAN" in txt
    return clean, txt


def attack(name, why_it_is_broken, **site_kw):
    """Build a broken site; the probe SHOULD refuse it."""
    d = Path(tempfile.mkdtemp(prefix="probe-adv-"))
    try:
        make_site(d, **site_kw)
        clean, txt = run_probe(d)
        if clean:
            HOLES.append((name, why_it_is_broken, _verdict_line(txt)))
            print(f"  HOLE   {name}")
            print(f"         probe said: {_verdict_line(txt)}")
        else:
            CAUGHT.append(name)
            print(f"  caught {name}   ({_fail_line(txt)})")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _verdict_line(txt):
    for ln in txt.splitlines():
        if ln.startswith("VERDICT"):
            return ln.strip()
    return "(no verdict line)"


def _fail_line(txt):
    for ln in txt.splitlines():
        if ln.startswith("FAIL"):
            return ln.strip()[:90]
    return _verdict_line(txt)[:90]


def main():
    import forge
    if not forge._find_browser():
        print("SKIPPED — no headless browser. A check that cannot run "
              "reports SKIPPED, never PASS.")
        return 0

    print("── attacking the TEXT measurement: it reads HTML, not pixels")
    attack("the whole page is transparent",
           "body{opacity:0} renders a blank white screen to every human "
           "being, while _visible_text — a regex over the DOM string — "
           "counts every character as if it were on screen.",
           css="body{opacity:0}")
    attack("the content is display:none",
           "the page shows a bare background. The text is in the DOM, so "
           "a string-length measurement sees a full page.",
           css="h1,p{display:none}")
    attack("white text on a white background",
           "legible to no one. Colour is never read, so the probe cannot "
           "tell painted text from invisible text.",
           css="body{background:#fff}h1,p{color:#fff}")
    attack("content pushed off screen",
           "position:absolute;left:-99999px is the oldest way to ship a "
           "blank page with a full DOM.",
           css="h1,p{position:absolute;left:-99999px}")
    attack("a hidden block masks a blank page",
           "the visible page is empty; the character count comes "
           "entirely from a hidden div. This is the exact shape of a "
           "hydration wipe that leaves its SSR text behind.",
           css="h1,p{display:none}",
           extra=f'<div hidden>{BODY}</div>')

    print("\n── attacking the IMAGE count: reported, never judged")
    attack("every image on the page is gone",
           "the probe prints an image count and never compares it to "
           "anything. Kept as a NAMED hole rather than a fixed one: "
           "without a baseline the plain probe cannot distinguish a "
           "page that lost its images from a page that never had any.",
           extra="")

    print("\n── attacking the CONSOLE and REQUEST logs")
    attack("the page swallows its own errors",
           "window.onerror returns true, so the runtime failure never "
           "reaches the console and the probe's only window into JS "
           "health is closed from inside the page.",
           extra="<script>window.onerror=function(){return true};"
                 "setTimeout(function(){nope.boom()},10);</script>")
    attack("a dead CDN asset the probe never asked for",
           "the request log records what OUR server served. An asset "
           "still pointing at a third-party host is fetched by the "
           "browser, not by us, so a port that does not own its assets "
           "can fail every one of them invisibly.",
           extra='<img src="https://cdn.invalid.example/logo.png">')

    print("\n── control: does it still catch what it claims to?")
    d = Path(tempfile.mkdtemp(prefix="probe-ctl-"))
    try:
        make_site(d)
        (d / "site" / "index.html").write_text(
            "<!DOCTYPE html><html><body><script>"
            "document.body.innerHTML=''</script></body></html>")
        clean, txt = run_probe(d)
        if clean:
            HOLES.append(("CONTROL: a genuinely blank page",
                          "if this passes, the suite itself is not "
                          "measuring anything.", _verdict_line(txt)))
            print("  HOLE   CONTROL blank page passed — the suite is blind")
        else:
            CAUGHT.append("CONTROL blank page")
            print(f"  caught CONTROL blank page   ({_fail_line(txt)})")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"attacks CAUGHT      : {len(CAUGHT)}")
    print(f"attacks THAT PASSED : {len(HOLES)}")
    unexpected = [h for h in HOLES if h[0] not in KNOWN_HOLES]
    if HOLES:
        print("\nHOLES — sites the probe hands over as healthy:\n")
        for n, why, verdict in HOLES:
            known = "  (KNOWN, accepted)" if n in KNOWN_HOLES else ""
            print(f"  * {n}{known}\n    {why}\n    {verdict}\n")
    print("=" * 70)
    if unexpected:
        print(f"probe adversary: FAILED — {len(unexpected)} unnamed hole(s)")
        return 1
    print(f"probe adversary: {len(CAUGHT)} caught, "
          f"{len(HOLES)} named hole(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
