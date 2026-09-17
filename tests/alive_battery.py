#!/usr/bin/env python3
"""Does the living background actually live, and does it rest on the design?

A moving background is only a feature if both halves are MEASURED: the page at rest
still matches what was fitted, and the page in motion really moves. A string check
cannot tell either; a render can. So frames are rendered and compared, one of them
tampered with on purpose to prove the proof notices, and the guarded edits Aethron's
own pages now take are checked for collateral damage on screen.
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_edit as E              # noqa: E402
import aethron_figma_grade as GR      # noqa: E402
import aethron_gradient as G          # noqa: E402

OK = FAIL = SKIP = 0
W, H = 900, 560
MODEL = {"base": (2, 2, 4), "layers": [
    {"cx": 120, "cy": 520, "rx": 520, "ry": 330, "c": (48, 78, 214), "a0": 1.0, "a1": 0.6, "s": 0.4},
    {"cx": 760, "cy": 420, "rx": 460, "ry": 360, "c": (90, 150, 225), "a0": 0.95, "a1": 0.5,
     "s": 0.35, "ease": "smooth"},
    {"cx": 450, "cy": 600, "rx": 380, "ry": 170, "c": (240, 246, 252), "a0": 1.0, "a1": 0.7, "s": 0.3}]}


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


def page():
    return ('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0}'
            f'.page{{position:relative;width:{W}px;height:{H}px;overflow:hidden;'
            f'background:{G.css(MODEL, W, H, units="%")}}}'
            '.t{position:absolute;white-space:nowrap;line-height:1.2;margin:0;'
            'font-family:Helvetica,Arial,sans-serif}'
            '.sf{position:absolute;box-sizing:border-box}'
            'button.sf{padding:0;margin:0;border:0;font:inherit;color:inherit}'
            '</style></head><body><main class="page" data-ae-id="bg">'
            '<h1 class="t" data-ae-id="t00" style="left:80px;top:60px;font-size:44.00px;color:#FFFFFF">'
            'Build Apps</h1>'
            '<button type="button" class="sf" data-ae-id="s00" style="left:600px;top:400px;width:180px;'
            'height:52px;border-radius:26px;background:#101014"><span class="t" style="left:48px;top:14px;'
            'font-size:18.00px;color:#EDEDED">Generate</span></button>'
            '</main></body></html>')


def shoot(html, tmp, name):
    f = tmp / f"{name}.html"
    f.write_text(html)
    png = tmp / f"{name}.png"
    GR.shoot(f, W, H, png)
    return png


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ae-alive-"))
    base = page()
    print("\n── the proof refuses to grade what it cannot see")
    r = E.prove_alive(base, tmp, W, H)
    check("a page that is not animated is SKIPPED, not passed", r["verdict"] == "SKIPPED", str(r))

    if not GR.find_browser():
        skip("every rendered check", "no browser")
        print(f"\nalive battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
        return 0 if FAIL == 0 else 1

    print("\n── the background moves, and its first frame is the design")
    live, ap, rf = E.apply(base, [{"id": "bg", "animate": {"style": "drift+breathe", "period": 16,
                                                           "strength": 0.6}}])
    check("the animation is applied", len(ap) == 1 and not rf, str(rf))
    r = E.prove_alive(live, tmp, W, H)
    print(f"       measured: {r}")
    check("frame 0 is the fitted background, pixel for pixel", r.get("frame0_matches_still", 0) >= 99.9,
          str(r))
    check("half a cycle in, the background has visibly moved", r.get("half_cycle_mean_change", 0) >= 0.5,
          str(r))
    check("the proof's verdict is PASS", r["verdict"] == "PASS", str(r))

    print("\n── ...and the proof notices a loop that does not start on the design")
    tampered = re.sub(r"(0\.0%\{--ae0w:[\d.]+%;--ae0h:[\d.]+%;--ae0x:)([-\d.]+)",
                      lambda m: m.group(1) + f"{float(m.group(2)) + 25:.2f}", live, count=1)
    check("the tamper really changed the first keyframe", tampered != live)
    r = E.prove_alive(tampered, tmp, W, H)
    check("a first frame that is not the fitted background FAILS", r["verdict"] == "FAIL"
          and "first frame" in r.get("why", ""), str(r))

    print("\n── strength means something you can measure")
    gentle, _, _ = E.apply(base, [{"id": "bg", "animate": {"style": "drift", "period": 16, "strength": 0.1}}])
    strong, _, _ = E.apply(base, [{"id": "bg", "animate": {"style": "drift", "period": 16, "strength": 1.0}}])
    rg, rs = E.prove_alive(gentle, tmp, W, H), E.prove_alive(strong, tmp, W, H)
    check("strength 1 moves the sky more than strength 0.1",
          rs.get("half_cycle_mean_change", 0) > rg.get("half_cycle_mean_change", 0),
          f"0.1 -> {rg.get('half_cycle_mean_change')}, 1.0 -> {rs.get('half_cycle_mean_change')}")

    print("\n── visibility asked for by name is reached by measuring")
    for word in ("visible", "strong"):
        got, tried = E.tune_alive(base, "drift+breathe", 10, word, tmp, W, H)
        goal = E.ALIVE_TARGETS[word]
        measured = dict(tried).get(got)
        check(f"'{word}' returns a strength it actually rendered", measured is not None, str(tried))
        check(f"  ...landing near {goal:g} levels, or at full strength if the sky cannot move that much",
              measured is not None and (abs(measured - goal) <= 0.15 * goal or got >= 1.0), str(tried))

    print("\n── an edit on Aethron's own markup changes only what it names, on screen")
    before = shoot(base, tmp, "before")
    recolour, ap, rf = E.apply(base, [{"id": "s00", "set": {"background": "#16A34A", "text": "Create"}}])
    check("the button edit is written", len(ap) == 1 and not rf, str(rf))
    after = shoot(recolour, tmp, "after")
    ok, damage = E.touched_only(before, after, [(600, 400, 180, 52)])
    check("recolouring and relabelling the button touched nothing else", ok, str(damage))
    longer, _, _ = E.apply(base, [{"id": "t00", "set": {"font_size": 120}}])
    wrecked = shoot(longer, tmp, "wrecked")
    ok, damage = E.touched_only(before, wrecked, [(600, 400, 180, 52)])
    check("an allowed edit that spills onto the page IS noticed", not ok, str(damage))

    print("\n── words in: the on-screen damage check has the final say")
    good = '{"edits": [{"id": "s00", "set": {"background": "#16A34A", "text": "Create"}}], "note": "green"}'
    r = E.ask(base, "make the button green and say Create", tmp, call=lambda pr: good)
    check("a clean edit asked for in words is APPLIED and verified on screen",
          r["verdict"] == "APPLIED" and r["checks"].get("collateral", {}).get("ok") is True, str(r.get("checks")))
    # THE PREMISE IS MEASURED, NOT ASSUMED. Twice this test "grew the headline over the
    # button" without its ink ever reaching the button, and twice the check rightly passed.
    over = base.replace("left:600px;top:400px;width:180px", "left:120px;top:190px;width:180px")
    hide_both = '<style>[data-ae-id="s00"],[data-ae-id="t00"]{visibility:hidden}</style></head>'
    hide_btn = '<style>[data-ae-id="s00"]{visibility:hidden}</style></head>'
    ground = shoot(over.replace("</head>", hide_both, 1), tmp, "premise_ground")
    alone = shoot(over.replace("</head>", hide_btn, 1).replace("font-size:44.00px", "font-size:360.00px"),
                  tmp, "premise_alone")
    import aethron_vision as V
    g, al = V.load(ground), V.load(alone)
    reach = sum(1 for y in range(190, 242, 2) for x in range(120, 300, 2) if V._dist(g.rgb(x, y), al.rgb(x, y)) > 16)
    check("premise: at 360px the headline's ink really reaches the button's box", reach >= 50, f"{reach} samples")
    grow = '{"edits": [{"id": "t00", "set": {"font_size": 360}}], "note": "huge headline"}'
    r = E.ask(over, "make the headline huge", tmp, call=lambda pr: grow)
    check("an allowed edit that grows into another element is REFUSED, page untouched",
          r["verdict"] == "REFUSED" and r["html"] == over, str(r.get("checks")))
    green = '{"edits": [{"id": "s00", "set": {"background": "#22C55E"}}], "note": "green"}'
    r = E.ask(base, "make the button green", tmp, call=lambda pr: green)
    check("a fill that would leave its label unreadable gets a legible label, measured",
          r["verdict"] == "APPLIED" and any("contrast" in n for n in r.get("tuning", [])), str(r.get("tuning")))
    empty_grow = '{"edits": [{"id": "t00", "set": {"text": "Build Apps People Really Love"}}], "note": "longer"}'
    r = E.ask(base, "make the headline longer", tmp, call=lambda pr: empty_grow)
    check("a longer headline that only covers empty background is APPLIED",
          r["verdict"] == "APPLIED", str(r.get("checks")))

    print("\n── moving and resizing, judged on screen")
    ok_move = '{"edits": [{"id": "s00", "move": {"dx": -100, "dy": 0}}], "note": "a little left"}'
    r = E.ask(base, "move the button a little left", tmp, call=lambda pr: ok_move)
    check("a move into empty space is APPLIED", r["verdict"] == "APPLIED", str(r.get("checks")))
    onto = '{"edits": [{"id": "s00", "move": {"dx": -480, "dy": -340}}], "note": "next to the headline"}'
    r = E.ask(base, "put the button by the headline", tmp, call=lambda pr: onto)
    check("a move onto the headline is REFUSED, page untouched", r["verdict"] == "REFUSED" and r["html"] == base,
          str(r.get("checks")))
    off = '{"edits": [{"id": "s00", "move": {"dx": 200, "dy": 0}}], "note": "further right"}'
    r = E.ask(base, "move the button right", tmp, call=lambda pr: off)
    check("a move that pushes the button off the page is REFUSED", r["verdict"] == "REFUSED"
          and "edge" in r.get("why", ""), str(r.get("why")))
    grow_btn = '{"edits": [{"id": "s00", "resize": {"scale": 1.3}}], "note": "bigger button"}'
    r = E.ask(base, "make the button bigger", tmp, call=lambda pr: grow_btn)
    check("a button made 30% bigger in open space is APPLIED", r["verdict"] == "APPLIED", str(r.get("checks")))

    print("\n── growing the page by copying, judged on screen, with one guided retry")
    chips = base.replace(
        '</main>',
        '<button type="button" class="sf" data-ae-id="c01" style="left:80px;top:300px;width:100px;height:40px;'
        'border-radius:20px;background:#2A2F3A"><span class="t" style="left:20px;top:10px;font-size:14.00px;'
        'color:#EDEDED">Android</span></button>'
        '<button type="button" class="sf" data-ae-id="c02" style="left:190px;top:300px;width:100px;height:40px;'
        'border-radius:20px;background:#2A2F3A"><span class="t" style="left:35px;top:10px;font-size:14.00px;'
        'color:#EDEDED">IOS</span></button></main>')
    add_chip = '{"edits": [{"id": "c02", "clone": {"text": "Linux", "place": "after"}}], "note": "third chip"}'
    r = E.ask(chips, "add a Linux chip", tmp, call=lambda pr: add_chip)
    created = (r["applied"] or [{}])[0].get("created", [])
    check("a copied chip in open space is APPLIED", r["verdict"] == "APPLIED" and created == ["c02-c1"],
          str((r["verdict"], r.get("checks"))))
    check("  ...placed one measured gap (10px) after its sibling",
          'data-ae-id="c02-c1" style="left:300.0px;top:300.0px' in r["html"])
    crash = '{"edits": [{"id": "c02", "clone": {"text": "Linux", "dx": 410, "dy": 100}}], "note": "onto the button"}'
    better = '{"edits": [{"id": "c02", "clone": {"text": "Linux", "place": "after"}}], "note": "in the row instead"}'
    prompts, replies = [], iter([crash, better])
    r = E.ask(chips, "add a Linux chip", tmp, call=lambda pr: (prompts.append(pr), next(replies))[1])
    check("a copy that would land on the Generate button is refused, and the retry that fixes it is APPLIED",
          r["verdict"] == "APPLIED" and len(r["attempts"]) == 2 and r["attempts"][0]["verdict"] == "REFUSED",
          str(r.get("attempts")))
    check("  ...and the retry named the element it would have covered",
          len(prompts) == 2 and "s00" in prompts[1] and "Generate" in prompts[1], prompts[1][-600:] if len(prompts) > 1 else "")

    print(f"\nalive battery: {OK} ok, {FAIL} failed, {SKIP} skipped")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
