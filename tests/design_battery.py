#!/usr/bin/env python3
"""Does the design referee fire on each defect — and stay quiet otherwise?

A CHECK THAT NEVER FIRES IS A CHECK THAT CANNOT SEE. The contrast rule
in particular is easy to ship dead: an element's own background is
almost always transparent, so a rule that reads it finds nothing on
every page and passes everything forever, looking exactly like a clean
bill of health.

So every rule here is aimed at a page built to break THAT RULE AND NO
OTHER, and every rule is also aimed at a clean page and must stay
silent. Both halves, or the suite is decoration.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_design as D           # noqa: E402
import aethron_eye as EYE            # noqa: E402

OK = FAIL = SKIP = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"   ({detail})" if detail else ""))


def skip(name, why):
    global SKIP
    SKIP += 1
    print(f"  SKIP {name}   ({why})")


# A page built to a real system: an 8px spacing grid, a five-step type
# scale, AA contrast throughout, aligned to one column, 48px targets.
CLEAN = """<!doctype html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#FFFFFF;color:#1A1A1F;
 font-family:Helvetica,Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:32px}
h1{font-size:40px;line-height:1.2;margin-bottom:16px}
h2{font-size:24px;margin-bottom:8px}
p{font-size:16px;color:#3C3C46;margin-bottom:24px}
small{font-size:13px;color:#4A4A55}
a.btn{display:inline-block;font-size:16px;background:#1A3FCC;color:#fff;
 text-decoration:none;border-radius:8px;padding:16px 24px}
section{margin-bottom:32px}
</style></head><body><div class="wrap">
<h1>Interfaces that hold their system</h1>
<p>Every size on this page comes from one scale and every gap is a
multiple of eight.</p>
<section><h2>Measured</h2><p>Numbers rather than opinions.</p></section>
<section><h2>Checked</h2><p>Narrow and wide, every time.</p></section>
<a class="btn" href="#">Start free</a>
<section><small>Small print that still meets AA.</small></section>
</div></body></html>"""


def variant(**kw):
    return CLEAN


def main():
    import tempfile
    import aethron_figma_grade as GR
    tmp = Path(tempfile.mkdtemp(prefix="ae-design-battery-"))

    print("\n── the arithmetic, before anything is built on it")
    # Published WCAG values: black on white is 21:1, and #767676 is the
    # canonical "exactly AA on white" grey.
    check("black on white is 21:1",
          abs(D.contrast((0, 0, 0), (255, 255, 255)) - 21.0) < 0.05,
          f"{D.contrast((0, 0, 0), (255, 255, 255)):.2f}")
    check("white on white is 1:1",
          abs(D.contrast((255, 255, 255), (255, 255, 255)) - 1.0) < 1e-6)
    g = D.contrast((0x76, 0x76, 0x76), (255, 255, 255))
    check("#767676 on white is the AA borderline", 4.5 <= g < 4.6,
          f"{g:.2f}")

    print("\n── a spacing system must beat chance")
    # THE BUG THIS REPRODUCES: with a +/-1 tolerance, three of every
    # four integers sit within 1 of a multiple of 4, so the first
    # version declared "base 4" for gaps of 2,3,6,9,15,21,44,105 — a
    # page with no spacing system at all. And at +/-1 on a 6px base,
    # chance alone is 50%: two of four RANDOM gap sets came back as
    # "base 6". A detector that invents a system where there is none is
    # worse than one that finds nothing.
    check("a real 8px grid is found",
          D.step_scale([8, 16, 24, 32, 48, 64, 16, 8]) in (4, 8))
    check("a real 6px grid is found",
          D.step_scale([6, 12, 18, 24, 36, 48, 12, 6]) == 6)
    check("a page with no system reports none",
          D.step_scale([2, 3, 6, 9, 15, 21, 44, 105]) is None)
    import random
    random.seed(11)
    fp = sum(1 for _ in range(40)
             if D.step_scale(sorted(random.randint(3, 120)
                                    for _ in range(8))) is not None)
    check("random gaps rarely look like a system", fp <= 6, f"{fp}/40")

    if not GR.find_browser():
        skip("everything that needs a render", "no browser")
        print(f"\ndesign battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
        return 0 if FAIL == 0 else 1

    print("\n── the clean page stays quiet")
    f = tmp / "clean.html"
    f.write_text(CLEAN)
    r = D.look(f, widths=(1280,), verbose=False)
    serious = [x for x in r["findings"]
               if x["kind"] in ("LOW CONTRAST", "NO TYPE SCALE")]
    check("no contrast or scale complaints on a well-built page",
          not serious,
          "; ".join(f'{x["kind"]}:{x.get("got")}' for x in serious[:3]))

    print("\n── each rule fires on a page built to break IT")
    attacks = {
        "text at 2.3:1 on its own ground": (
            CLEAN.replace("color:#3C3C46", "color:#B9B9C4"),
            "LOW CONTRAST", (1280,)),
        # THE TEST'S OWN PREMISE WAS WRONG FIRST. This block used to
        # claim eleven sizes while rendering seven, because two of its
        # stylesheet edits were overridden by the inline styles it also
        # added — so the rule correctly did not fire and the failure
        # looked like a code fault. Measure the page you are attacking
        # with before believing what it is attacking.
        "ten arbitrary font sizes": (
            CLEAN.replace("h1{font-size:40px", "h1{font-size:41px")
                 .replace("p{font-size:16px", "p{font-size:17px")
                 .replace("small{font-size:13px", "small{font-size:11.5px")
                 .replace("a.btn{display:inline-block;font-size:16px",
                          "a.btn{display:inline-block;font-size:15px")
                 .replace("<p>Every size",
                          '<p style="font-size:19px">Every size')
                 .replace("<h2>Measured",
                          '<h2 style="font-size:21px">Measured')
                 .replace("<h2>Checked",
                          '<h2 style="font-size:27px">Checked')
                 .replace("<p>Numbers rather than opinions.</p>",
                          '<p style="font-size:22.5px">Numbers rather '
                          'than opinions.</p>')
                 .replace("<p>Narrow and wide, every time.</p>",
                          '<p style="font-size:29px">Narrow and wide, '
                          'every time.</p>')
                 .replace("<h1>", '<h1><span style="font-size:35px">A'
                                  '</span>', 1),
            "NO TYPE SCALE", (1280,)),
        "a 28px tap target on a phone": (
            CLEAN.replace("padding:16px 24px", "padding:5px 8px"),
            "TAP TARGET", (390,)),
        "an edge that misses by 3px": (
            CLEAN.replace("<section><h2>Checked",
                          '<section style="margin-left:3px"><h2>Checked'),
            "NOT ALIGNED", (1280,)),
    }
    for name, (html, want, widths) in attacks.items():
        p = tmp / (re.sub(r"\W+", "_", name) + ".html")
        p.write_text(html)
        rr = D.look(p, widths=widths, verbose=False)
        kinds = {x["kind"] for x in rr["findings"]}
        check(f"caught: {name}", want in kinds,
              f"got {sorted(kinds) or 'nothing'}")

    print("\n── and the contrast rule is not silently dead")
    # THE FAILURE THIS GUARDS. An element's own background is almost
    # always transparent, so a contrast rule that reads it finds nothing
    # on every page — and a rule that never fires looks exactly like a
    # page that never fails. It has to be proved to fire, on a page
    # where the low-contrast text sits on an ANCESTOR's background.
    p = tmp / "nested.html"
    p.write_text(CLEAN.replace(
        "<section><h2>Measured</h2><p>Numbers rather than opinions.</p>"
        "</section>",
        '<section style="background:#2A2A31"><h2 style="color:#3A3A44">'
        'Measured</h2><p style="color:#33333C">Numbers rather than '
        'opinions.</p></section>'))
    rr = D.look(p, widths=(1280,), verbose=False)
    lows = [x for x in rr["findings"] if x["kind"] == "LOW CONTRAST"]
    check("low contrast against an ANCESTOR's background is caught",
          bool(lows), f"{len(rr['findings'])} finding(s), none contrast")
    check("and it reports the measured ratio, not a verdict",
          bool(lows) and ":1" in str(lows[0].get("got")),
          str(lows[0].get("got")) if lows else "")

    print("\n── a system can be read off a page that already has one")
    f = tmp / "clean2.html"
    f.write_text(CLEAN)
    got = EYE.read_page(f, 1280)
    if not got:
        skip("system inference", "no reading")
    else:
        sysm = D.infer(got["items"])
        # the page is built on 40/24/16/13 — the scale must recover the
        # sizes that carry the text, not every size that appears once
        check("the type scale it recovers is small and real",
              3 <= len(sysm["type_scale"]) <= 7,
              str(sysm["type_scale"]))
        check("the spacing base is recovered",
              D.step_scale(sysm["spacing"]) in (4, 8),
              str(sysm["spacing"]))

    print("\n── a check that cannot run says so")
    rr = D.look(tmp / "nope.html", widths=(1280,), verbose=False)
    check("an unrenderable page is SKIPPED, never PASS",
          rr["verdict"] == "SKIPPED", rr["verdict"])

    print(f"\ndesign battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
