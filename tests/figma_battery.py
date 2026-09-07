#!/usr/bin/env python3
"""Does the Figma referee actually catch a broken conversion?

A referee that passes everything is worse than none: it converts a
silent failure into a confident claim. So this breaks pages on purpose
and asserts the grader REFUSES them.

No network, no Figma token, no API. The "design" is a page we render
ourselves and then damage in specific ways, which is the only way to
know the grader responds to the thing it claims to measure.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_figma as F            # noqa: E402
import aethron_figma_grade as G      # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          + (f"   {detail}" if not cond and detail else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  SKIP {name} — {why}")


PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0}}
body{{width:400px;height:300px;position:relative;background:#fff;
  font-family:Helvetica,Arial,sans-serif}}
.n{{position:absolute}}
</style></head><body>
<div class="n" style="left:20px;top:20px;width:360px;height:80px;
  background-color:{c1}"></div>
<div class="n" style="left:{tx}px;top:130px;width:300px;height:40px;
  font-size:28px;color:#111">Positivus</div>
<div class="n" style="left:20px;top:200px;width:120px;height:60px;
  background-color:#191A23;border-radius:14px"></div>
{extra}
</body></html>"""


def write(d: Path, **kw):
    kw.setdefault("c1", "#B9FF66")
    kw.setdefault("tx", 20)
    kw.setdefault("extra", "")
    (d / "index.html").write_text(PAGE.format(**kw))
    return d / "index.html"


def main():
    print("── PNG codec (no dependency, so it must be right)")
    tmp = Path(tempfile.mkdtemp(prefix="figma-bat-"))
    px = bytearray()
    for v in [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255),
              (9, 30, 70, 255), (200, 200, 200, 255), (0, 0, 0, 255)]:
        px += bytes(v)
    G.write_png(tmp / "rt.png", 3, 2, px)
    w, h, back = G.read_png(tmp / "rt.png")
    check("a written PNG reads back byte-identical",
          (w, h) == (3, 2) and bytes(back) == bytes(px))

    print("\n── url parsing")
    k, n = F.parse_url("https://www.figma.com/design/AbC123/My-Page?node-id=403-333")
    check("file key extracted", k == "AbC123", k)
    check("node id converted 403-333 -> 403:333", n == "403:333", str(n))
    k2, n2 = F.parse_url("https://figma.com/file/XyZ9/Old-Style")
    check("legacy /file/ urls work", k2 == "XyZ9" and n2 is None)
    try:
        F.parse_url("https://example.com/nope")
        check("a non-figma url is refused", False, "it was accepted")
    except SystemExit:
        check("a non-figma url is refused", True)

    print("\n── colour + effect translation")
    c = F._rgba({"r": 1, "g": 0, "b": 0, "a": 1})
    check("solid red", c.replace(".0", "") == "rgba(255,0,0,1)", c)
    sh = F._effects_css({"effects": [{"type": "DROP_SHADOW", "radius": 4,
                                      "offset": {"x": 2, "y": 3},
                                      "color": {"r": 0, "g": 0, "b": 0, "a": .5}}]})
    check("drop shadow becomes box-shadow",
          sh and sh[0].startswith("box-shadow:2px 3px 4px"), str(sh))
    hidden = F._effects_css({"effects": [{"type": "DROP_SHADOW", "radius": 4,
                                          "visible": False,
                                          "offset": {"x": 1, "y": 1},
                                          "color": {"r": 0, "g": 0, "b": 0}}]})
    check("an invisible effect is not emitted", hidden == [], str(hidden))
    r = F._radius_css({"rectangleCornerRadii": [1, 2, 3, 4]})
    check("per-corner radii kept distinct",
          r == ["border-radius:1px 2px 3px 4px"], str(r))

    print("\n── icon collapsing (small + graphical + no text)")
    icon = {"type": "GROUP", "absoluteBoundingBox": {"width": 24, "height": 24},
            "children": [{"type": "VECTOR"}]}
    check("a small vector group is exported as one SVG", F._is_icon(icon))
    withtext = {"type": "GROUP",
                "absoluteBoundingBox": {"width": 24, "height": 24},
                "children": [{"type": "VECTOR"}, {"type": "TEXT"}]}
    check("a group containing TEXT is NEVER flattened to an image",
          not F._is_icon(withtext))
    huge = {"type": "GROUP",
            "absoluteBoundingBox": {"width": 1400, "height": 900},
            "children": [{"type": "VECTOR"}]}
    check("a page-sized group is not flattened", not F._is_icon(huge))

    print("\n── THE REFEREE: does it refuse damage?")
    if not G.find_browser():
        skip("referee scenarios", "no headless browser — UNVERIFIED, "
             "not proven good")
    else:
        truth_dir = tmp / "truth"
        truth_dir.mkdir()
        write(truth_dir)
        truth = truth_dir / "truth.png"
        if not G.shoot(truth_dir / "index.html", 400, 300, truth):
            skip("referee scenarios", "browser produced no screenshot")
        else:
            same = tmp / "same"
            same.mkdir()
            write(same)
            r = G.grade(same, truth, verbose=False)
            check("an identical page passes",
                  r.get("ok") is True and r["identical"] > 0.999,
                  f"identical={r.get('identical')}")

            moved = tmp / "moved"
            moved.mkdir()
            write(moved, tx=64)
            r = G.grade(moved, truth, verbose=False)
            check("text shifted 44px is REFUSED",
                  r.get("ok") is False, f"identical={r.get('identical')}")
            check("  ...and it registers as structural, not antialiasing",
                  r["structural"] > 0.001, f"structural={r['structural']}")

            recol = tmp / "recol"
            recol.mkdir()
            write(recol, c1="#B9FF00")     # subtly wrong brand green
            r = G.grade(recol, truth, verbose=False)
            check("a wrong brand colour is REFUSED",
                  r.get("ok") is False, f"identical={r.get('identical')}")

            missing = tmp / "missing"
            missing.mkdir()
            (missing / "index.html").write_text(
                PAGE.format(c1="#B9FF66", tx=20, extra="")
                .replace('background-color:#191A23;border-radius:14px', ''))
            r = G.grade(missing, truth, verbose=False)
            check("a missing element is REFUSED",
                  r.get("ok") is False, f"identical={r.get('identical')}")

            r = G.grade(same, truth, verbose=False)
            check("the diff map is written for a human to look at",
                  (same / ".grade-diff.png").is_file())

    print("\n── honesty rule")
    nob = tmp / "nob"
    nob.mkdir()
    write(nob)
    real = G.find_browser
    G.find_browser = lambda: None
    try:
        r = G.grade(nob, tmp / "truth" / "truth.png", verbose=False)
        check("no browser reports SKIPPED, never PASS",
              r.get("ok") is None, str(r))
    finally:
        G.find_browser = real

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 60)
    print(f"{len(PASS)} ok, {len(FAIL)} failed, {len(SKIP)} skipped")
    if FAIL:
        for f in FAIL:
            print("   FAILED:", f)
    print("figma battery:", "all green" if not FAIL else "FAILED")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
