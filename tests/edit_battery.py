#!/usr/bin/env python3
"""CAN A MODEL EDIT THIS PAGE WITHOUT BREAKING IT?

The architecture the owner asked for splits the job: measurement owns
geometry, the model owns intent. aethron_edit's own selftest proves the
allow-list refuses a bad edit. That is the easy half — it only shows
that each rule fires on the example its author imagined.

This asks the harder question, on a real rendered page: when an edit is
ALLOWED and still damages the page, does anything notice? A model is
entitled to say "make this text bigger"; the page then reflows, a
neighbouring line is covered, and nothing in the edit itself is wrong.
The only thing that can catch it is looking at the result.

So every check here renders. No assertion is made about a page that was
not drawn.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aethron_edit as E            # noqa: E402
import aethron_vision as V          # noqa: E402
import aethron_figma_grade as GR    # noqa: E402

OK = FAIL = 0
TMP = Path(tempfile.mkdtemp(prefix="ae-edit-"))
W, H = 700, 460

PAGE = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{W}px;height:{H}px;overflow:hidden}}
body{{background:#0B0B0B;position:relative;font-family:Helvetica,Arial}}
.t{{position:absolute;white-space:nowrap;line-height:1}}
.r{{position:absolute}}</style></head><body>
<div class="r" data-ae-id="ground" style="left:0;top:0;width:{W}px;
 height:{H}px;z-index:0;background:#0B0B0B"></div>
<div class="r" data-ae-id="r00" style="left:0;top:120px;width:{W}px;
 height:1px;z-index:1;background:#333333"></div>
<div class="r" data-ae-id="f00" style="left:60px;top:200px;width:180px;
 height:44px;z-index:2;background:#FFFFFF;border-radius:6px"></div>
<div class="t" data-ae-id="t00" style="left:60px;top:40px;font-size:30.0px;
 color:#FFFFFF;z-index:4">Wezzi is the way</div>
<div class="t" data-ae-id="t01" style="left:60px;top:90px;font-size:14.0px;
 color:#AAAAAA;z-index:4">Wezzi tracks your users</div>
<div class="t" data-ae-id="t02" style="left:80px;top:214px;font-size:16.0px;
 color:#000000;z-index:4">Get started</div>
<div class="t" data-ae-id="t03" style="left:60px;top:330px;font-size:13.0px;
 color:#888888;z-index:4">Trusted by teams everywhere</div>
</body></html>'''


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}   {detail}")


def shoot(html, tag):
    p = TMP / f"{tag}.html"
    p.write_text(html)
    png = TMP / f"{tag}.png"
    GR.shoot(p, W, H, png)
    return png


def boxes_of(html, ids):
    idx = {e["id"]: e for e in E.manifest(html)["elements"]}
    out = []
    for i in ids:
        b = idx[i]["box"]
        # A text element declares no width; take a generous span so the
        # guard is testing collateral damage, not the edit itself.
        out.append([b[0] or 0, b[1] or 0, b[2] or 460, b[3] or 60])
    return out


def main():
    if not GR.find_browser():
        print("VERDICT: SKIPPED — no browser, so nothing was rendered "
              "and nothing is proven")
        return 0

    before = shoot(PAGE, "before")

    print("── an ordinary rebrand lands, and disturbs nothing else")
    html, applied, refused = E.apply(PAGE, [
        {"id": "t00", "set": {"text": "Jomiez is the way"}},
        {"id": "t01", "set": {"text": "Jomiez tracks your users"}},
        {"id": "f00", "set": {"background": "#B9FF66"}},
    ])
    check("three edits applied, none refused",
          len(applied) == 3 and not refused, str(refused))
    after = shoot(html, "rebrand")
    ok, damage = E.touched_only(before, after, boxes_of(html, ["t00", "t01",
                                                              "f00", "t02"]))
    check("the guard agrees only the named things changed", ok, str(damage))
    check("the old name is gone from the page", "Wezzi" not in html)

    print("\n── the page really did change (a guard that passes a no-op "
          "is worthless)")
    same = GR.compare(after, before)["identical"]
    check("before and after are not the same image", same < 0.999,
          f"identical={same:.4f}")

    print("\n── AND NOW THE ONE THAT MATTERS: an ALLOWED edit that "
          "damages the page")
    # Every property here is legal. font_size is in range, the id is
    # real, the kind admits it. Only the RESULT is wrong: this line
    # grows until it covers its neighbours.
    huge, ap2, rf2 = E.apply(PAGE, [{"id": "t01", "set": {"font_size": 90}}])
    check("the allow-list lets it through, as it should",
          len(ap2) == 1 and not rf2, str(rf2))
    after2 = shoot(huge, "huge")
    ok2, damage2 = E.touched_only(before, after2,
                                  boxes_of(PAGE, ["t01"]))
    check("...and the guard catches the damage anyway", not ok2,
          "the enlarged line covered its neighbours and nothing noticed")
    if damage2:
        check("  ...naming where the page changed without permission",
              "box" in damage2 and damage2["pixels"] > 0, str(damage2))

    print("\n── a second kind of collateral: the wrong element edited")
    # A model asked to restyle the button reaches for the rule instead —
    # a legal edit on a real element, and the wrong one. The report will
    # say the button changed; the page says the rule did.
    hid, ap3, _ = E.apply(PAGE, [{"id": "r00", "set": {"hidden": True}}])
    check("hiding a rule is an allowed edit", len(ap3) == 1)
    after3 = shoot(hid, "wrongel")
    ok3, dmg3 = E.touched_only(before, after3, boxes_of(PAGE, ["f00"]))
    check("...and the guard catches that it was not the named one",
          not ok3, str(dmg3))

    print("\n── an edit that changes nothing visible is still honest")
    noop, ap4, _ = E.apply(PAGE, [{"id": "t03", "set": {"color": "#888888"}}])
    after4 = shoot(noop, "noop")
    ok4, _ = E.touched_only(before, after4, boxes_of(PAGE, ["t03"]))
    check("setting a colour to what it already was passes the guard", ok4)

    print(f"\nedit battery: {OK} ok, {FAIL} failed")
    print("VERDICT: " + ("PASS" if not FAIL else "FAIL"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
