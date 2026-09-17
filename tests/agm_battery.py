#!/usr/bin/env python3
"""Does the Automatic General Measure see what nobody wrote a check for?

The owner, 2026-09-15: Aethron "only measures for something you specifically added to measure".
This battery makes changes that slip past every purpose-built check Aethron had — a property no
check lists, a behaviour with no pixels at all, a hover effect quietly lost, a motion nobody asked
for — and asks the AGM, which was given no rule about any of them, to find each one and refuse it.
The honest change beside them must come out fully explained, or the measure is just noise.

No key, no network: the page is built here, and there is no reviewer, so anything the request's
own words cannot explain is refused as UNVERIFIED rather than waved through.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import aethron_agm as A               # noqa: E402
import aethron_change as C            # noqa: E402
import aethron_figma_grade as GR      # noqa: E402
import interact_battery as IB         # noqa: E402
import spec_battery as SB             # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}", flush=True)
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"   ({detail})" if detail else ""), flush=True)


def measure(html, work, plan, request, related, acted, claims=()):
    after, errs = C.apply_plan(html, plan)
    assert not errs, errs
    seen = A.observe(html, after, work, IB.W, IB.H)
    notes, problems = A.explain(seen or [], request, related=related, acted=acted, claims=claims)
    return seen, notes, problems


def patch(find, replace):
    return {"find": find, "replace": replace, "count": 1}


# THE OWNER'S FIRST REQUEST, on the fixture: the card 10% shorter, and every button on it riding up with
# its bottom edge. Measured 2026-09-15: the AGM refused 36 of the 38 differences of exactly this change
# on the real page — the buttons' move, the far edge of the card, the screen the card left behind.
SHORTER = "Can you reduce this chat box height a little bit?"
SHORTER_PLAN = {"patches": [
    patch('data-ae-id="s00" style="left:150px;top:190px;width:600px;height:240px;',
          'data-ae-id="s00" style="left:150px;top:190px;width:600px;height:216px;'),
    patch('data-ae-id="s01" style="left:590px;top:370px;', 'data-ae-id="s01" style="left:590px;top:346px;'),
    patch('data-ae-id="s02" style="left:180px;top:373px;', 'data-ae-id="s02" style="left:180px;top:349px;'),
    patch('data-ae-id="s03" style="left:290px;top:373px;', 'data-ae-id="s03" style="left:290px;top:349px;'),
    patch('data-ae-id="s04" style="left:400px;top:373px;', 'data-ae-id="s04" style="left:400px;top:349px;')],
    "touches": ["s00", "s01", "s02", "s03", "s04"]}
SHORTER_CLAIMS = [{"kind": "size", "id": "s00", "dimension": "height", "change": "-10%"}] + [
    {"kind": "position", "id": i, "axis": "y", "change": "-24px"} for i in ("s01", "s02", "s03", "s04")]


def main():
    if not GR.find_browser():
        print("VERDICT: SKIPPED — no browser to record the pages with; nothing here is proven")
        return 0
    work = Path(tempfile.mkdtemp(prefix="ae-agm-battery-"))
    html = IB.page()
    counter = SB.GOOD_CODE

    print("── the honest change is recorded, and every difference in it is explained")
    seen, notes, problems = measure(html, work, counter, SB.ASK, {"c01"}, {"t01"})
    check("the character counter produces differences, and the AGM records them", seen and len(seen) >= 2, str(seen))
    check("  ...every one is explained by the request and the tests", not problems, "\n".join(problems))
    check("  ...including what typing into the chat box now does",
          any("type #t01 now also" in n for n in notes), "\n".join(notes))

    print("\n── changes no check was ever written for are found and refused")
    upper = dict(counter, css='[data-ae-id="t00"]{text-transform:uppercase}', touches=["c01", "t00"])
    seen, notes, problems = measure(html, work, upper, SB.ASK, {"c01"}, {"t01"})
    check("the headline switched to capitals — a typography change nobody asked for — is refused",
          any("t00" in p and "typography" in p for p in problems), "\n".join(problems))
    cursor = dict(counter, css='[data-ae-id="s01"]{cursor:wait!important}')
    seen, notes, problems = measure(html, work, cursor, SB.ASK, {"c01"}, {"t01"})
    check("Generate's cursor turned to 'wait' — no pixel changes at all — is refused",
          any("s01" in p and "cursor" in p for p in problems), "\n".join(problems))
    dull = dict(counter, css='button.sf[data-ae-id="s02"]:hover{filter:none!important}')
    seen, notes, problems = measure(html, work, dull, SB.ASK, {"c01"}, {"t01"})
    check("the Android chip no longer brightening on hover — a lost reaction — is refused",
          any("hover #s02 no longer" in p for p in problems), "\n".join(problems))
    wobble = dict(counter, css='@keyframes aeWobble{to{transform:rotate(4deg)}}'
                               '[data-ae-id="t00"]{animation:aeWobble 1.5s ease-in-out infinite alternate}')
    seen, notes, problems = measure(html, work, wobble, SB.ASK, {"c01"}, {"t01"})
    check("a wobble on the headline — motion nobody asked for — is refused",
          any("t00" in p and "motion" in p.lower() for p in problems), "\n".join(problems))

    print("\n── a change and everything it must cause are one change")
    riders = {"s00", "s01", "s02", "s03", "s04"}
    seen, notes, problems = measure(html, work, SHORTER_PLAN, SHORTER, riders, set(), SHORTER_CLAIMS)
    check("a shorter chat box with its buttons riding up produces differences", seen and len(seen) >= 5, str(seen))
    check("  ...and every one is explained: the resize, the buttons' move, the screen they left", not problems,
          "\n".join(problems))
    darker = dict(SHORTER_PLAN, patches=SHORTER_PLAN["patches"] + [
        patch("border-radius:40px;background:rgba(240,239,240,0.8)", "border-radius:40px;background:rgba(170,169,170,0.8)")])
    seen, notes, problems = measure(html, work, darker, SHORTER, riders, set(), SHORTER_CLAIMS)
    check("the same resize that also darkens the card is refused for the colour", any("s00" in p and "colour" in p
                                                                                     for p in problems), "\n".join(problems))
    headline = dict(SHORTER_PLAN, patches=SHORTER_PLAN["patches"] + [
        patch('data-ae-id="t00" style="left:80px;top:40px;', 'data-ae-id="t00" style="left:80px;top:24px;')])
    seen, notes, problems = measure(html, work, headline, SHORTER, riders, set(), SHORTER_CLAIMS)
    check("the same resize that also moves the headline is refused for the headline",
          any("t00" in p and "position" in p for p in problems), "\n".join(problems))
    seen, notes, problems = measure(html, work, SHORTER_PLAN, SHORTER, riders, set(), SHORTER_CLAIMS[:1])
    check("the buttons' move with no claim that measured it is refused", any("s01" in p for p in problems),
          "\n".join(problems))
    print(f"\nagm battery: {OK} ok, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
