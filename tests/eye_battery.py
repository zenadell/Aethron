#!/usr/bin/env python3
"""Can the eye actually SEE — and can it be fooled?

This instrument is about to be handed to other people's coding agents
through MCP, so the adversarial half matters more than the happy path.
A referee that cries wolf gets switched off; a referee that waves
everything through is worse than none, because it launders a broken
page as a verified one.

Every check here is built from a page whose faults are KNOWN because
this file put them there.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_eye as E             # noqa: E402

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


PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0E0E12;color:#fff;font-family:Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px}
nav{display:flex;gap:28px;align-items:baseline;margin-bottom:60px}
/* the y coordinates in this row DELIBERATELY disagree by a few px —
   that is what scrambled a (y,x) sort and made one element report as
   MISSING and EXTRA at the same time */
nav .brand{font-size:20px;font-weight:700}
nav a{font-size:12px;color:#B9B9C2;text-decoration:none}
nav a.tall{font-size:15px}
nav .cta{margin-left:auto;font-size:13px;background:#fff;color:#111;
 border:0;border-radius:6px;padding:8px 14px}
h1{font-size:44px;line-height:1.1;margin-bottom:18px}
p.lede{font-size:15px;color:#A8A8B4;max-width:520px}
.row{display:flex;gap:16px;margin-top:40px}
.card{flex:1;background:#17171F;border-radius:12px;padding:20px}
.card h3{font-size:18px;margin-bottom:6px}
.card p{font-size:13px;color:#9A9AA6}
</style></head><body><div class="wrap">
<nav><span class="brand">Northwind</span>
<a href="#">Features</a><a href="#" class="tall">Pricing</a>
<a href="#">Docs</a><button class="cta">Start free</button></nav>
<h1>Ship interfaces that match the design</h1>
<p class="lede">Every agent writes the code. Almost none of them look at
what they drew. Northwind measures the result and tells you what to
change.</p>
<div class="row">
<div class="card"><h3>Measured</h3><p>Numbers, not opinions.</p></div>
<div class="card"><h3>Checked</h3><p>Four widths, every time.</p></div>
<div class="card"><h3>Monotone</h3><p>It cannot get worse.</p></div>
</div></div></body></html>"""


def main():
    import tempfile
    import aethron_figma_grade as GR
    tmp = Path(tempfile.mkdtemp(prefix="ae-eye-battery-"))

    print("\n── the arithmetic is right before anything is built on it")
    # CIEDE2000 against published reference behaviour: identical is 0,
    # a just-noticeable difference is small, black-vs-white is large.
    check("identical colours are zero", E.delta_e((90, 40, 40),
                                                  (90, 40, 40)) < 1e-6)
    big = E.delta_e((0, 0, 0), (255, 255, 255))
    check("black vs white is a large distance", big > 95, f"{big:.1f}")
    # THE TEST PREMISE WAS CHECKED BEFORE IT WAS BELIEVED. The first
    # pair chosen here (dark grey vs mid green) came back 3.94 vs 4.10
    # and failed — not because the code was wrong but because those two
    # pairs genuinely are about as far apart as each other. Measured
    # across candidates, black->#101010 is 2.73 while
    # #78C878->#88D888 is 4.10 at the SAME RGB distance, which is the
    # property worth asserting: perceptual distance is not a function
    # of RGB distance.
    d_dark = E.delta_e((0, 0, 0), (16, 16, 16))
    d_mid = E.delta_e((120, 200, 120), (136, 216, 136))
    check("perceptual, not RGB (same RGB gap, different distance)",
          d_mid - d_dark > 1.0, f"{d_dark:.2f} vs {d_mid:.2f}")
    check("text similarity tolerates an OCR slip",
          E._sim("Docs", "Dacs") > 0.3
          and E._sim("Docs", "Pricing") < 0.3)

    print("\n── reading order survives a noisy row")
    # THE BUG THIS REPRODUCES: a nav sits at y = 22, 24, 24, 26, 26, and
    # sorting by the raw (y, x) makes the reference and the built page
    # different permutations of the same row. Monotone alignment then
    # has to drop matches, and the one Log-in button on the page came
    # back reported as MISSING and as EXTRA simultaneously.
    noisy = [{"text": "E", "x": 400, "y": 26, "h": 10},
             {"text": "A", "x": 60, "y": 22, "h": 14},
             {"text": "D", "x": 300, "y": 24, "h": 12},
             {"text": "B", "x": 120, "y": 25, "h": 10},
             {"text": "C", "x": 210, "y": 23, "h": 11},
             {"text": "Z", "x": 60, "y": 200, "h": 12}]
    order = "".join(e["text"] for e in E._reading_order(noisy))
    check("a row reads left to right despite 4px of y noise",
          order == "ABCDEZ", order)

    if not GR.find_browser():
        skip("everything that needs a render", "no browser")
        print(f"\neye battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
        return 0 if FAIL == 0 else 1

    ref_html = tmp / "reference.html"
    ref_html.write_text(PAGE)
    ref_png = tmp / "reference.png"
    GR.shoot(ref_html, 1280, 900, ref_png)
    if not (ref_png.is_file() and ref_png.stat().st_size):
        skip("every rendered check", "the reference did not render")
        print(f"\neye battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
        return 0 if FAIL == 0 else 1

    print("\n── it does not cry wolf")
    same = tmp / "same.html"
    same.write_text(PAGE)
    r = E.look(same, ref_html, widths=(1280,), verbose=False)
    # A REFEREE THAT FIRES ON EVERYTHING IS ONE PEOPLE SWITCH OFF. The
    # probe learned this once already and it had to be learned again for
    # the audit; a page compared with itself must come back clean.
    check("a page compared with itself PASSES",
          r["verdict"] == "PASS", f"{r['verdict']}: {r['why']}")
    check("and reports no repairs", not r.get("findings"),
          f"{len(r.get('findings', []))} finding(s)")

    print("\n── it sees what a human would call obviously wrong")
    attacks = {
        "a heading three times too big":
            (PAGE.replace("h1{font-size:44px", "h1{font-size:130px"),
             ("WRONG SIZE", "MISPLACED")),
        "body copy in the wrong colour":
            (PAGE.replace("color:#A8A8B4", "color:#D4463C"),
             ("WRONG COLOUR",)),
        "a whole section deleted":
            (re.sub(r'<div class="row">.*?</div>\s*</div>',
                    "</div>", PAGE, flags=re.S), ("MISSING",)),
        "a dead image":
            (PAGE.replace("<h1>",
                          '<img src="does-not-exist.png" '
                          'width="200" height="80"><h1>'),
             ("BROKEN IMAGE",)),
    }
    for name, (html, want_kinds) in attacks.items():
        f = tmp / (re.sub(r"\W+", "_", name) + ".html")
        f.write_text(html)
        rr = E.look(f, ref_html, widths=(1280,), verbose=False)
        kinds = {x["kind"] for x in rr.get("findings", [])}
        check(f"caught: {name}",
              rr["verdict"] == "FAIL" and bool(kinds & set(want_kinds)),
              f"{rr['verdict']}, kinds={sorted(kinds)}")

    print("\n── every finding is something a builder can act on")
    f = tmp / "colour.html"
    f.write_text(PAGE.replace("color:#A8A8B4", "color:#D4463C"))
    rr = E.look(f, ref_html, widths=(1280,), verbose=False)
    acts = [x for x in rr.get("findings", [])
            if x["kind"] in ("WRONG COLOUR", "WRONG SIZE", "MISPLACED")]
    check("findings carry a real CSS selector",
          bool(acts) and all(x.get("selector") for x in acts),
          f"{sum(1 for x in acts if not x.get('selector'))} without one")
    check("findings carry the words of the element",
          bool(acts) and all(x.get("text") for x in acts))
    check("findings say what to change, not just what is wrong",
          bool(acts) and all(x.get("fix") for x in acts))
    # the selector must actually RESOLVE in the built page — a selector
    # that matches nothing is a finding the builder cannot act on
    probe = E.read_page(f, 1280)
    if probe:
        sels = {i["sel"] for i in probe["items"]}
        check("the selector names an element that exists",
              all(x["selector"] in sels for x in acts),
              f"{[x['selector'] for x in acts if x['selector'] not in sels][:1]}")
    else:
        skip("selector resolution", "no reading")

    print("\n── one width is not a check")
    narrow = PAGE.replace(".row{display:flex", ".row{display:flex;"
                          "min-width:1400px")
    f = tmp / "narrow.html"
    f.write_text(narrow)
    rr = E.look(f, ref_html, widths=(390, 1280), verbose=False)
    spilled = sum(x.get("spills", 0) for x in rr["rounds"])
    # THE INDUSTRY'S OWN NAMED FAILURE: "agents test UI work at one
    # screen width, so everything narrower ships unchecked, resulting in
    # primary buttons ending up off the edge of phone screens." At 1280
    # this page is fine; the fault exists only narrow.
    check("content that spills on a phone is caught",
          spilled > 0 and rr["verdict"] == "FAIL", f"{spilled} spill(s)")

    print("\n── a phone is 390px, not Chrome's 500px floor")
    # CHROME HEADLESS CLAMPS ITS WINDOW TO 500px ON macOS — measured:
    # asked 390 -> innerWidth 500. So every "390px" check here was laid
    # out at 500, and the attack below, which fits 500 and breaks at
    # 390, was invisible to the whole referee stack. It was also found
    # the embarrassing way round: a live build was screenshotted at
    # "390", the image cropped a 500px layout and looked broken, and the
    # page turned out to be fine — two instruments lying in opposite
    # directions about the same page.
    squeeze = PAGE.replace(
        ".wrap{max-width:900px;margin:0 auto;padding:24px}",
        ".wrap{max-width:900px;min-width:460px;margin:0 auto;padding:24px}")
    assert squeeze != PAGE, "the attack did not apply"
    f = tmp / "fits500_breaks390.html"
    f.write_text(squeeze)
    r390 = E.read_page(f, 390)
    r500 = E.read_page(f, 500)
    if r390 is None or r500 is None:
        skip("the true-phone-width checks", "no reading")
    else:
        def spills(r):
            return sum(1 for it in r["items"]
                       if it["x"] + it["w"] > r["vw"] + 2)
        check("a 390px reading is laid out at 390px, not 500px",
              r390["vw"] == 390 and r390.get("true_width"),
              f"vw={r390['vw']}")
        check("the attack is precise: it genuinely fits at 500px",
              spills(r500) == 0, f"{spills(r500)} spill(s) at 500")
        check("and a page that breaks only below 500px IS caught at 390",
              spills(r390) > 0, f"{spills(r390)} spill(s) at 390")
        check("the narrow read leaves nothing behind in the project",
              not f.with_suffix(".eye.html").exists()
              and sorted(x.name for x in tmp.iterdir()
                         if "__ae_frame__" in x.name) == [])

    print("\n── the loop cannot make the page worse")
    f = tmp / "loop.html"
    f.write_text(PAGE.replace("p.lede{font-size:15px", "p.lede{font-size:9px"))
    before = E.look(f, ref_html, widths=(1280,), verbose=False)
    rr = E.refine(f, ref_html, rounds=3, widths=(1280,), verbose=False)
    check("refine never returns a score below where it started",
          rr["score"] >= before["score"] - 1e-9,
          f"{before['score']:.3f} -> {rr['score']:.3f}")
    check("the trajectory is recorded so the claim is checkable",
          len(rr["trail"]) >= 1, str(rr["trail"]))
    after = E.look(f, ref_html, widths=(1280,), verbose=False)
    check("and the file left on disk is the BEST one, not the last tried",
          abs(after["score"] - rr["score"]) < 1e-6,
          f"on disk {after['score']:.3f} vs reported {rr['score']:.3f}")

    print("\n── a check that cannot run says so")
    rr = E.look(tmp / "does-not-exist.html", ref_html, widths=(1280,),
                verbose=False)
    # THE PROJECT'S OLDEST INVARIANT. A Framer check "passing" on a
    # Next.js site once shipped a blank page as healthy.
    check("an unrenderable page is SKIPPED, never PASS",
          rr["verdict"] == "SKIPPED", rr["verdict"])

    print(f"\neye battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
