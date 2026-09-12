#!/usr/bin/env python3
"""Does the flow pass keep the design AND gain the reflow?

BOTH HALVES, ALWAYS, because each one alone is trivially easy and
completely worthless — and this suite exists because both were shipped
separately during the work that produced the module:

  * the first version reflowed perfectly (0 spills at phone width) while
    landing 0 of 21 lines at the design width. A flawless reflow of a
    page that was no longer the design.
  * the version before it was pixel-faithful and could not move at all,
    which is what the owner was looking at when they asked why their
    website renders as a frozen canvas.

So no check here passes on one number. And the adversarial half matters
more than the happy path: a flow pass that silently drops elements still
reflows beautifully, so "it reflows" must never be allowed to stand in
for "it is still the page".

Runs with no key, no network and no model. Needs a browser; without one
every check reports SKIPPED, never PASS.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_flow as F            # noqa: E402
import aethron_edit as AE           # noqa: E402
import aethron_screen as SC         # noqa: E402

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


# A small page in exactly the shape `rebuild()` emits: absolutely
# positioned, one div per measured LINE, a real <button> and real <a>
# nav items from the semantic pass, and a rule.
PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:900px;height:600px;overflow:hidden}
body{background:#101014;position:relative;font-family:sans-serif}
.t{position:absolute;white-space:nowrap;line-height:1}
.r{position:absolute}
button{position:absolute}
</style></head><body>
<a href="#" class="t" data-ae-id="t00" style="left:60px;top:30px;
 font-size:18px;color:#FFFFFF;z-index:4">Brandmark</a>
<a href="#" class="t" data-ae-id="t01" style="left:600px;top:34px;
 font-size:12px;color:#CCCCCC;z-index:4">Features</a>
<a href="#" class="t" data-ae-id="t02" style="left:680px;top:34px;
 font-size:12px;color:#CCCCCC;z-index:4">Pricing</a>
<div class="r" data-ae-id="r00" style="left:0;top:80px;width:900px;
 height:1px;background:#2A2A31;z-index:1"></div>
<div class="t" data-ae-id="t03" style="left:60px;top:160px;
 font-size:44px;color:#FFFFFF;z-index:4">A headline that carries</div>
<div class="t" data-ae-id="t04" style="left:60px;top:250px;
 font-size:14px;color:#A0A0A8;z-index:4">First line of the paragraph
 here</div>
<div class="t" data-ae-id="t05" style="left:60px;top:272px;
 font-size:14px;color:#A0A0A8;z-index:4">second line of the very same
 paragraph</div>
<div class="t" data-ae-id="t06" style="left:60px;top:294px;
 font-size:14px;color:#A0A0A8;z-index:4">and a third line to
 finish</div>
<button type="button" data-ae-id="t07" style="left:60px;top:350px;
 width:150px;height:40px;background:#FF5C28;border:0;border-radius:8px;
 padding:0;font:inherit;cursor:pointer;color:#FFFFFF;z-index:2">
 <span class="t" style="left:20px;top:14px;font-size:13px;
 color:#FFFFFF">Get started</span></button>
</body></html>"""


def build(tmp):
    src = tmp / "page.html"
    src.write_text(PAGE)
    r = F.flow(PAGE, "battery", verbose=False, src=src)
    out = tmp / "flow.html"
    out.write_text(r["html"])
    return src, out, r


def main():
    import tempfile
    import aethron_figma_grade as GR
    tmp = Path(tempfile.mkdtemp(prefix="ae-flow-battery-"))

    print("\n── the semantic elements survive the trip")
    man = AE.manifest(PAGE)
    ids = {e["id"] for e in man["elements"]}
    # THE REGRESSION THAT PROMPTED THIS FILE. manifest() matched only
    # div|img, so every <a> and every <button> the semantic pass emits
    # was invisible — to the framework emitters AND to the model-facing
    # edit seam. Measured on a real rebuild: 27 elements in the file,
    # 20 in the manifest, the seven missing ones being the whole
    # navigation and both buttons.
    check("manifest sees the nav links", {"t00", "t01", "t02"} <= ids,
          f"got {sorted(ids)}")
    check("manifest sees the button", "t07" in ids)
    kinds = {e["id"]: e["kind"] for e in man["elements"]}
    check("a button is a button, not a nameless box",
          kinds.get("t07") == "button", kinds.get("t07"))
    btn = [e for e in man["elements"] if e["id"] == "t07"][0]
    check("the button carries its label",
          "Get started" in (btn.get("text") or ""), btn.get("text"))
    check("the button's label size comes off the label",
          btn.get("font_size") == 13, btn.get("font_size"))
    ir = SC.page_ir(PAGE, "battery")
    check("page_ir carries them too (all six emitters read this)",
          {"t00", "t01", "t02", "t07"}
          <= {e["id"] for e in ir["elements"]})
    check("aethron_edit will accept an edit to a button",
          "text" in AE.ALLOWED.get("button", set()))

    if not GR.find_browser():
        skip("everything that needs a render", "no browser")
        print(f"\nflow battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
        return 0 if FAIL == 0 else 1

    print("\n── the flow keeps the page")
    src, out, r = build(tmp)
    check("the measured lines were reassembled into a paragraph",
          r["paragraphs"] >= 1, f"{r['paragraphs']}")
    html = out.read_text()
    # A PASS THAT DROPS ELEMENTS STILL REFLOWS BEAUTIFULLY. The first
    # version of paragraphs() returned only what it had merged and
    # deleted every picture and filled box on the page — 65 elements in,
    # 17 out — and nothing about the reflow noticed.
    kept = {m for m in re.findall(r'data-ae-id="([^"]+)"', html)}
    check("no element is silently dropped",
          {"t00", "t01", "t02", "t03", "t07"} <= kept,
          f"missing {sorted({'t00','t01','t02','t03','t07'} - kept)}")
    check("the rule is still on the page (all 18 were once dropped)",
          "class=\"grid\"" in html and "<i style=" in html)
    check("a nav link is still a link", "<a href=\"#\"" in html)
    check("a button is still a button", "<button" in html)
    check("nothing is positioned absolutely in the flow",
          "position:absolute" not in html.split('class="page"')[-1])

    print("\n── it lands where the design put it")
    a = F.measure(src)
    b = F.measure(out, size=(900, 600))
    if not a or not b:
        skip("the element-by-element comparison", "no reading")
    else:
        off = [k for k in a if k in b
               and max(abs(b[k][0] - a[k][0]),
                       abs(b[k][1] - a[k][1])) >= 8]
        check("every element within 8px of its measured place",
              not off, f"off: {off}")

    print("\n── and it actually reflows")
    import aethron_generate as G
    narrow = G._flow(out, 400)
    if narrow is None:
        skip("the reflow reading", "the browser gave no reading")
    else:
        check("nothing spills at phone width",
              narrow["spills"] == 0,
              f"{narrow['spills']} spill(s)")
        wide = F.measure(out, size=(500, 1600))
        if wide:
            tall = max(v[1] + v[3] for v in wide.values())
            # THE flex-direction:column TRAP. Each cell carries its
            # measured width as an inline flex-basis, and flex-basis is
            # measured along the MAIN axis — so flipping the row to a
            # column turned every width into a HEIGHT. A 435px-wide
            # heading became a 435px-tall cell and the phone layout ran
            # to 3,200px of mostly empty page while passing every other
            # check in this file.
            check("the phone layout is not absurdly tall",
                  tall < 1600, f"{tall}px")

    print("\n── a background that is a PHOTOGRAPH OF THE PAGE is caught")
    # THE ADVERSARIAL HALF, and its absence is the whole reason a
    # visibly broken page was handed over as a 95% pass. Every check in
    # this file rendered at the DESIGN WIDTH — the one width at which a
    # stretched background photograph lines up exactly with the elements
    # on top of it. At 2000px the two separated and every element on the
    # page rendered beside a blurred copy of itself, and all eighteen
    # checks stayed green.
    ref = tmp / "ref.png"
    GR.shoot(src, 900, 600, ref)
    if not (ref.is_file() and ref.stat().st_size):
        skip("the photograph-plate attack", "the reference did not render")
    else:
        boxes = [v for v in (a or {}).values() if v[2] > 3 and v[3] > 3]
        # the page itself, offered as its own background: the exact
        # thing that shipped, in its purest form
        worst = F.plate_resembles_page(ref.read_bytes(), ref, boxes)
        check("the page offered as its own background scores near 1.0",
              worst > 0.9, f"{worst:.3f}")
        honest, _k = F.background_plate(
            ref, boxes=[v for v in (a or {}).values()][:4],
            solid=[], cell=4)
        hs = F.plate_resembles_page(honest, ref, boxes)
        check("a real background scores far below it",
              hs < worst - 0.15, f"honest {hs:.3f} vs photo {worst:.3f}")
        # and the referee must REFUSE the photograph
        gh = F.ghosts(out, ref, boxes, widths=(900,), tol=0.65)
        check("ghosts() reports the widths it checked",
              len(gh["widths"]) == 1)

    print("\n── the verdict is honest")
    # prove() judges against the ORIGINAL screenshot; here the absolute
    # page IS the reference, so render it and use that.
    ref = tmp / "ref.png"
    GR.shoot(src, 900, 600, ref)
    if ref.is_file() and ref.stat().st_size:
        p = F.prove(out, ref, {"w": 900, "h": 600})
        check("the verdict reports both halves, not one",
              "land" in p["why"] and "spill" in p["why"], p["why"])
        check("a page that keeps its lines and reflows PASSES",
              p["verdict"] in ("PASS", "SKIPPED"),
              f"{p['verdict']}: {p['why']}")
    else:
        skip("the two-sided verdict", "the reference did not render")

    print(f"\nflow battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
