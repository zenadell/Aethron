#!/usr/bin/env python3
"""
ADOPT BATTERY — can Aethron make a page it did not build measurable, and can that claim
be fooled?

The honest half is easy: stamp a page, prove the picture did not change. The half that
matters is the adversarial one, and it exists because of a specific measured fact recorded
while building this: SHUFFLING EVERY ID ONTO THE WRONG ELEMENT CHANGES ZERO PIXELS. A pixel
referee — the instrument this project reaches for first — is structurally blind to identity.
So "the page looks the same" can never be the proof that the ids landed right, and this
battery fails if the identity check ever stops being able to tell the difference.

Runs a real browser. With none, every browser check reports SKIPPED, never PASS.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_adopt as AD       # noqa: E402
import aethron_change as C       # noqa: E402
import aethron_edit as AE        # noqa: E402
import aethron_figma_grade as GR  # noqa: E402

W, H = 900, 600

# A page with NO ids and a RELATIVE stylesheet — the two things every real page has and
# every page Aethron built for itself does not.
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="style.css"></head>
<body>
<header class="bar"><a href="/" class="logo">Northwind</a>
<nav><a href="/about">About Us</a><a href="/pricing">Pricing</a><a href="/jobs">Careers</a></nav></header>
<main><h1>Ship it on Friday</h1><p class="lede">The plan that fits the week you actually have.</p>
<section class="cards">
<div class="card"><h2>Starter</h2><p>For one person.</p><button>Choose Starter</button></div>
<div class="card"><h2>Team</h2><p>For a small crew.</p><button>Choose Team</button></div>
<div class="card"><h2>Studio</h2><p>For the whole floor.</p><button>Choose Studio</button></div>
</section>
<table><tr><td>Seats</td><td>Unlimited</td></tr></table>
</main></body></html>"""

CSS = """body{margin:0;background:#0d1b2a;color:#e8eef5;font:16px/1.5 system-ui,sans-serif}
.bar{display:flex;gap:24px;align-items:center;padding:18px 28px;background:#132b45}
.bar a{color:#cfe3ff;text-decoration:none;margin-right:14px}
h1{font-size:40px;margin:32px 28px 8px}
.lede{margin:0 28px 28px;color:#9fb6cf}
.cards{display:flex;gap:18px;padding:0 28px 40px}
.card{background:#1b3556;border-radius:14px;padding:22px;flex:1;box-shadow:0 8px 24px #0006}
.card h2{margin:0 0 8px;font-size:22px}
button{margin-top:14px;background:#e0a458;color:#10203a;border:0;border-radius:9px;
padding:10px 16px;font-size:15px;cursor:pointer}
td{padding:8px 28px;color:#9fb6cf}"""


def main():
    ok = fail = skip = 0

    def check(name, cond, note=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fail += 1
            print(f"  FAIL {name}" + (f" — {note}" if note else ""))

    def skipped(name, why):
        nonlocal skip
        skip += 1
        print(f"  SKIP {name} — {why}")

    print("adopt battery")

    if not GR.find_browser():
        print("\nVERDICT: SKIPPED — no browser, so nothing here could be measured. "
              "UNVERIFIED (not proven good)")
        return 0

    work = Path(tempfile.mkdtemp(prefix="ae-adopt-bat-"))
    try:
        site = work / "site"
        site.mkdir()
        page = site / "index.html"
        page.write_text(PAGE)
        (site / "style.css").write_text(CSS)
        original = PAGE

        # ---- a page nobody stamped ----------------------------------------------------
        check("a page Aethron did not build is seen to need adopting", AD.needs_adopting(PAGE))
        check("and its canvas cannot be read before it is adopted",
              AE.manifest(PAGE)["canvas"]["w"] is None)

        rep = AD.adopt(page, W, H, write=True, log=lambda *a: None)
        check("it is adopted", rep.get("verdict", "").startswith("ADOPTED"), str(rep.get("why")))
        if not rep.get("verdict", "").startswith("ADOPTED"):
            print(f"\nVERDICT: FAIL — {rep.get('why')}")
            return 1

        stamped = page.read_text()
        check("the picture did not change, every pixel compared",
              rep["proof"]["verdict"] == "PROVEN" and rep["proof"]["pixels_changed"] == 0,
              str(rep["proof"]))
        check("and every id landed on the element it was picked for",
              rep["landed"]["verdict"].startswith("PROVEN")
              and rep["landed"]["on_the_wrong_element"] == 0, str(rep["landed"]))
        check("nothing was left unplaced in the source",
              rep["could_not_be_placed_in_the_source"] == 0)

        import re
        ids = re.findall(r'data-ae-id="([^"]+)"', stamped)
        check("every id is unique", len(set(ids)) == len(ids) and len(ids) > 10, str(len(ids)))
        check("the ground is named", "bg" in ids)
        check("the words on the page are reachable",
              any(n["why"] == "words" for n in rep["named"]))
        check("so are the controls", any(n["why"] == "control" for n in rep["named"]))
        check("and the cards it paints", any(n["why"] == "surface" for n in rep["named"]))

        cv = AE.manifest(stamped)["canvas"]
        check("the canvas the browser measured travels with the page",
              cv["w"] == W and cv["h"] >= H, str(cv))
        check("the element list a model is given is no longer empty",
              len(AE.manifest(stamped)["elements"]) > 5)

        # A <td> only exists inside a <tbody> the source never spelled. If the mapping were
        # arithmetic rather than aligned, everything after <table> would be stamped one out.
        check("an element the browser invented did not shift the mapping",
              "<td data-ae-id=" in stamped)

        again = AD.adopt(page, W, H, write=True, log=lambda *a: None)
        check("adopting an adopted page changes nothing",
              again["verdict"] == "ALREADY ADOPTED" and page.read_text() == stamped,
              str(again.get("verdict")))

        # ---- THE ATTACK: every id on the wrong element, and not one pixel to show for it ----
        rot = {a: b for a, b in zip(ids, ids[3:] + ids[:3]) if a != "bg" and b != "bg"}
        shuffled = re.sub(r'data-ae-id="([^"]+)"',
                          lambda m: 'data-ae-id="%s"' % rot.get(m.group(1), m.group(1)), stamped)
        check("the attack really did move the ids", shuffled != stamped)

        named = [n for n in rep["named"] if n["id"] != "bg"]
        landed = AD.confirm(shuffled, page, W, H, rep["named"])
        check("ids shuffled onto the wrong elements are REFUSED",
              landed["verdict"] == "REFUSED", str(landed.get("why")))
        check("and it names how many, not just that something is wrong",
              landed["on_the_wrong_element"] > len(named) // 3,
              f"{landed.get('on_the_wrong_element')} of {len(named)}")

        blind = AD.prove(stamped, shuffled, page, W, H)
        check("THE POINT: the same shuffle changes zero pixels, so the pixel proof passes it",
              blind["verdict"] == "PROVEN" and blind["pixels_changed"] == 0, str(blind))

        # ---- THE ASSET TRAP: a real page rendered away from its own folder ----------------
        elsewhere = work / "elsewhere"
        elsewhere.mkdir()
        was = C.set_asset_base(None)
        C.stop_serving()          # a served folder would resolve the assets on its own
        C._SHOTS.clear()
        lost = C._shot(stamped, elsewhere, W, H, "nobase")
        C.set_asset_base(site)
        kept = C._shot(stamped, elsewhere, W, H, "withbase")
        C.set_asset_base(was)
        if lost is None or kept is None:
            skipped("a page measured away from its assets", "the page could not be rendered")
        else:
            check("without a base, a real page is measured UNSTYLED — the silent wrong answer",
                  lost.px != kept.px)
            truth = C._shot(stamped, site, W, H, "athome")
            C.set_asset_base(None)
            check("with the base, it is the same picture as at home",
                  truth is not None and kept.px == truth.px)
            # A page whose assets are ROOT-absolute cannot be saved by a base at all — only
            # by serving its folder, which is what a visitor's browser does.
            rooted = stamped.replace('href="style.css"', 'href="/style.css"')
            C.set_asset_base(site)
            by_base = C._shot(rooted, elsewhere, W, H, "rootbase")
            C.set_asset_base(None)
            C.serve_assets(site)
            by_server = C._shot(rooted, elsewhere, W, H, "rootserved")
            C.stop_serving()
            check("a root-absolute stylesheet is NOT saved by a base",
                  by_base is not None and truth is not None and by_base.px != truth.px)
            check("  ...and IS saved by serving the page's own folder",
                  by_server is not None and by_server.px == truth.px)

        # ---- the cap is a real constraint and says what it dropped ------------------------
        dom, why = AD._render(page, W, H)
        if dom is None:
            skipped("the cap", why)
        else:
            _, small = AD.stamp(original, dom, cap=3, canvas=AD.canvas_of(dom, W, H))
            check("a cap keeps the page measurable rather than enormous",
                  small["stamped"] == 4 and small["over_the_cap"] > 0, str(small["stamped"]))
            check("and what it dropped is reported, not lost",
                  small["over_the_cap"] + small["stamped"] - 1 == small["worth_naming"])

        # ---- honesty: no browser is SKIPPED, never ADOPTED --------------------------------
        real, GR.find_browser = GR.find_browser, lambda: None
        try:
            blindrep = AD.adopt(page, W, H, write=False, log=lambda *a: None)
        finally:
            GR.find_browser = real
        check("with no browser it reports SKIPPED, never a pass",
              blindrep["verdict"] == "SKIPPED", str(blindrep.get("verdict")))

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\nadopt battery: {ok} ok, {fail} failed, {skip} skipped")
    print(f"VERDICT: {'PASS' if not fail else 'FAIL'} — {ok} of {ok + fail} checks")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
