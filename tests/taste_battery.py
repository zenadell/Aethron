#!/usr/bin/env python3
"""Do the taste rules refuse generic design — and ONLY generic design?

Taste guides for coding agents (DESIGN.md rules, anti-slop filters,
Taste Skill) put their bans in a prompt and let the model grade itself.
Aethron refuses on the rendered page instead. That is only an advantage
if the refusal is PRECISE: a taste rule that fires on a legitimate design
is worse than none, because in clone mode it would "improve" a template
away from what the user asked to copy, and in brief mode it would spend
paid repair rounds chasing a phantom.

So most of this file is the precision half: an orange-into-black glow is
not the purple-to-blue signature, a headline sized down on a phone is not
a flat hierarchy, a gradient button is not a gradient hero, and nothing
taste-related may fire while cloning.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_design as D           # noqa: E402

OK = FAIL = SKIP = 0
TASTE = {"FLAT HIERARCHY", "AI GRADIENT", "EM DASH"}


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


def item(tag, text, fs, y=0, w=600, h=None, bgi=None, sel=None):
    """One element in exactly the shape the eye's probe reports."""
    return {"sel": sel or f"{tag}.t{y}", "tag": tag, "text": text,
            "x": 40, "y": y, "w": w, "h": h or int(fs * 1.3), "fs": fs,
            "fw": "400", "color": "rgb(20, 20, 24)", "bg": None,
            "effbg": "rgb(255, 255, 255)", "radius": "0px", "boxish": None,
            "bgi": bgi, "img": None, "broke": False}


LONG = "A paragraph long enough to count as body copy on this page."


def kinds(findings):
    return {f["kind"] for f in findings}


def main():
    print("\n── flat hierarchy")
    flat = [item("h1", "Simple pricing for everyone", 18, y=0),
            item("p", LONG, 14, y=40),
            item("h2", "Starter plan", 18, y=120),
            item("p", LONG, 14, y=160), item("p", LONG, 14, y=200)]
    strong = [item("h1", "Simple pricing for everyone", 48, y=0),
              item("p", LONG, 16, y=80),
              item("h2", "Starter plan", 24, y=160),
              item("p", LONG, 16, y=200), item("p", LONG, 16, y=240)]
    # THE PAGE GEMINI ACTUALLY SHIPPED: headline 18px, card titles 18px,
    # body 14px — every measured rule passed, and it looked timid.
    check("a headline no bigger than its section titles is refused",
          "FLAT HIERARCHY" in kinds(D.audit(flat, taste=True)))
    check("a headline that genuinely leads is not",
          "FLAT HIERARCHY" not in kinds(D.audit(strong, taste=True)),
          str(kinds(D.audit(strong, taste=True))))
    phone = [item("h1", "Simple pricing for everyone", 26, y=0),
             item("p", LONG, 16, y=60), item("h2", "Starter plan", 20, y=140),
             item("p", LONG, 16, y=180)]
    check("a headline sized down on a phone is not refused",
          "FLAT HIERARCHY" not in kinds(D.audit(phone, mobile=True,
                                                taste=True)))
    f = [x for x in D.audit(flat, taste=True) if x["kind"] == "FLAT HIERARCHY"]
    check("and the repair names a real size to use",
          bool(f) and "at least" in f[0]["fix"] and f[0].get("selector"),
          f[0]["fix"] if f else "no finding")

    print("\n── the generic gradient, and only that gradient")
    base = [item("h1", "Ship faster", 56, y=0), item("p", LONG, 16, y=90)]
    purple = base + [item("section", "", 16, y=0, w=1200, h=600,
        bgi="linear-gradient(135deg, rgb(102, 126, 234) 0%, "
            "rgb(118, 75, 162) 100%)", sel="section.hero")]
    check("purple-into-blue across a hero is refused",
          "AI GRADIENT" in kinds(D.audit(purple, taste=True)))
    # THE OWNER'S OWN FIRST SCREENSHOT: orange blending into black. That
    # is a design, and a rule that cannot tell it from the signature is
    # exactly the kind that gets switched off.
    orange = base + [item("section", "", 16, y=0, w=1200, h=600,
        bgi="radial-gradient(circle, rgb(225, 100, 42) 0%, "
            "rgb(30, 18, 17) 100%)", sel="section.hero")]
    check("an orange-into-black glow is NOT refused",
          "AI GRADIENT" not in kinds(D.audit(orange, taste=True)))
    blue = base + [item("section", "", 16, y=0, w=1200, h=600,
        bgi="linear-gradient(90deg, rgb(14, 165, 233), rgb(37, 99, 235))",
        sel="section.hero")]
    check("a blue-only wash is NOT refused",
          "AI GRADIENT" not in kinds(D.audit(blue, taste=True)))
    button = base + [item("a", "Get started", 16, y=160, w=200, h=48,
        bgi="linear-gradient(135deg, rgb(102, 126, 234), "
            "rgb(118, 75, 162))", sel="a.cta")]
    check("the same gradient on a small button is NOT refused",
          "AI GRADIENT" not in kinds(D.audit(button, taste=True)))
    hexy = base + [item("section", "", 16, y=0, w=1200, h=600,
        bgi="linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
        sel="section.hero")]
    check("hex colour stops are read as well as rgb()",
          "AI GRADIENT" in kinds(D.audit(hexy, taste=True)))

    print("\n── em dashes")
    dash = base + [item("p", "Built for teams — and for you.", 16, y=200)]
    fs = D.audit(dash, taste=True)
    check("an em dash in the copy is reported", "EM DASH" in kinds(fs))

    print("\n── cloning turns every taste rule off")
    # A TEMPLATE THE USER WANTS COPIED may have a flat hierarchy, a
    # purple gradient and em dashes. Copying it is the job.
    slop = flat + purple[2:] + dash[2:]
    check("with taste on, the slop page trips all three rules",
          TASTE <= kinds(D.audit(slop, taste=True)),
          str(kinds(D.audit(slop, taste=True)) & TASTE))
    check("with taste off, not one taste rule fires",
          not (kinds(D.audit(slop, taste=False)) & TASTE),
          str(kinds(D.audit(slop, taste=False)) & TASTE))

    import aethron_figma_grade as GR
    if not GR.find_browser():
        skip("the rendered checks", "no browser")
        print(f"\ntaste battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
        return 0 if FAIL == 0 else 1

    import aethron_build as B
    print("\n── on a rendered page, the mode decides")
    tmp = Path(tempfile.mkdtemp(prefix="ae-taste-"))

    def page(h1px):
        d = tmp / f"h{h1px}"
        d.mkdir()
        (d / "index.html").write_text(f"""<!doctype html><html lang="en">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kestrel</title><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#FFFFFF;color:#16161C;font-family:Helvetica,Arial,sans-serif}}
.wrap{{max-width:960px;margin:0 auto;padding:48px 24px}}
h1{{font-size:{h1px}px;line-height:1.1;margin-bottom:24px}}
h2{{font-size:24px;margin:40px 0 16px}}
p{{font-size:16px;color:#3A3A46;margin-bottom:16px;max-width:560px}}
</style></head><body><div class="wrap">
<h1>Kestrel keeps your books</h1>
<p>{LONG} Kestrel reconciles every account overnight.</p>
<h2>How it works</h2>
<p>{LONG} Nothing to install and nothing to learn.</p>
</div></body></html>""")
        return d

    brief = "Build a landing page for Kestrel"
    flat_page = page(24)
    r = B.check(flat_page, brief, widths=(390, 1280), verbose=False)
    check("BRIEF mode refuses a rendered page with a flat hierarchy",
          r["verdict"] == "REFUSED"
          and "FLAT HIERARCHY" in kinds(r["blocking"]),
          f"{r['verdict']}: {r['why']}")
    r = B.check(flat_page, brief, reference=flat_page / "index.html",
                widths=(390, 1280), verbose=False)
    check("CLONE mode accepts that same page as a faithful copy",
          r["verdict"] == "ACCEPTED" and not (kinds(r["findings"]) & TASTE),
          f"{r['verdict']}: {r['why']}")
    r = B.check(page(52), brief, widths=(390, 1280), verbose=False)
    check("and a page whose headline leads is accepted in BRIEF mode",
          r["verdict"] == "ACCEPTED", f"{r['verdict']}: {r['why']}")

    print(f"\ntaste battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
