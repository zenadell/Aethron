#!/usr/bin/env python3
"""THE DESIGN REFEREE — for when there is no picture to copy.

`aethron_eye` can only grade against something a browser has already
drawn: a screenshot, a URL, a Figma frame. That covers "rebuild this"
and covers nothing else. Ask any of these tools "build me a dashboard"
and there is no target at all — which is exactly why Lovable, v0 and
Bolt generate code and then hope, and why the thing they hand back so
often looks like software rather than like design.

A TARGET DOES NOT HAVE TO BE AN IMAGE. It can be a SPECIFICATION, and a
specification made of numbers is checkable the same way a screenshot is:

    type scale      every size on the page comes from one small set
    spacing scale   every gap is a step, not a number someone typed
    palette         every colour is a token, within a perceptual
                    distance of one the design actually declares
    contrast        WCAG AA, measured against what is REALLY behind the
                    text rather than against an assumption about it
    alignment       things line up, because edges that nearly agree are
                    the loudest defect in any interface
    rhythm          the gaps between sections are consistent
    target size     a thing you tap is big enough to tap

None of that is taste. All of it is arithmetic, and all of it is the
difference between a page that matches a mock and a page a designer
would sign. It is also precisely what generated interfaces get wrong:
twenty-three different font sizes, a 13px gap beside a 16px one, text at
2.9:1 on its own background, and a button 28px tall on a phone.

WHY THIS IS THE HALF THAT WAS MISSING. Measuring a rebuild against a
screenshot can only ever reach the screenshot. A design system is the
thing you can hold NEW work to — a section nobody drew, a page that did
not exist, the second screen of a flow. That is the difference between
copying a design and having one.

TWO MODES, and the second matters more than it looks:

    declared   the spec is given (brand tokens, a chosen scale) and the
               page is held to it
    inferred   the spec is READ OFF a page that is already good — a
               reference site, or the design's own screenshot — so
               "match this design system" needs no one to write it down

The inferred mode is what makes this usable: nobody types out a type
scale, and a design system recovered by measurement is a design system
that is actually in use rather than one that was aspired to.
"""
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aethron_eye as EYE            # noqa: E402

# A size is "on the scale" if it is within this fraction of a step.
SIZE_SNAP = 0.04
# A gap is on the spacing scale if it is within this many px of a step.
SPACE_SNAP = 2
# A colour is "a token" within this CIEDE2000 distance of one.
COLOR_SNAP = 4.0
# WCAG AA: 4.5 for body text, 3.0 for large text (>=24px, or >=19px bold)
AA_BODY, AA_LARGE = 4.5, 3.0
# Apple HIG 44pt, Material 48dp. 44 is the number both agree is enough.
TAP_MIN = 44
# A type scale is 5-7 steps. The finding message has always said so;
# the threshold used to say 8, which is the kind of quiet disagreement
# between a rule and its own statement that lets a page through while
# the report claims it is being held to a standard.
MAX_STEPS = 7


def _lum(rgb):
    def f(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    """WCAG contrast ratio. 1.0 is invisible, 21.0 is black on white."""
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _text_items(items):
    return [i for i in items
            if (i.get("text") or "").strip() and i.get("fs")]


# ───────────────────────────── reading a system ──────────────────────

def infer(items, keep=0.9):
    """The design system a page is ACTUALLY using, read off the page.

    Not the one its CSS declares — the one that reaches the screen. A
    codebase can hold a beautiful token file and still render 23
    distinct font sizes, because tokens are a promise and computed
    styles are what happened.

    Sizes are kept by how much TEXT is set in them, not by how many
    elements use them: one h1 is a real step in the scale and forty
    identical nav links are one decision. A size used once, for a few
    characters, is a mistake — and a scale that admits every mistake is
    not a scale.
    """
    txt = _text_items(items)
    weight = Counter()
    for i in txt:
        weight[round(i["fs"], 1)] += max(1, len(i["text"]))
    sizes, run = [], 0
    total = sum(weight.values()) or 1
    for size, w in weight.most_common():
        sizes.append(size)
        run += w
        if run / total >= keep:
            break
    colors = Counter()
    for i in txt:
        c = EYE._rgb(i.get("color"))
        if c:
            colors[c] += max(1, len(i["text"]))
    grounds = Counter()
    for i in items:
        c = EYE._rgb(i.get("effbg") or i.get("bg"))
        if c:
            grounds[c] += max(1, i.get("w", 0) * i.get("h", 0))
    gaps = Counter()
    for a, bx in zip(sorted(items, key=lambda e: e["y"]),
                     sorted(items, key=lambda e: e["y"])[1:]):
        g = bx["y"] - (a["y"] + a["h"])
        if 0 < g < 200:
            gaps[int(round(g))] += 1
    return {
        "type_scale": sorted(sizes),
        "text_colors": [c for c, _ in colors.most_common(6)],
        "grounds": [c for c, _ in grounds.most_common(4)],
        "spacing": sorted(g for g, n in gaps.most_common(8)),
    }


def step_scale(spacing):
    """The base step a spacing system is built on, if it has one.

    Real systems are multiples of 4 or 8. Given the gaps a page actually
    uses, the base is the largest of 8, 6, 4 that most of them divide
    into — and if none does, the page has no spacing system, which is a
    finding rather than a reason to invent one.
    """
    if len(spacing) < 4:
        return None
    # IT MUST BEAT CHANCE, OR IT IS NOT A SYSTEM. With a +/-1 tolerance,
    # three of every four integers are within 1 of a multiple of 4 — so
    # the first version of this function declared "space_base: 4" for a
    # page whose gaps were 2, 3, 6, 9, 15, 21, 44, 105, which is a page
    # with no spacing system at all. A test whose options all score the
    # same is not a test; this one now measures LIFT over the hit rate
    # the base would get on random numbers.
    # AND THE TOLERANCE HAS TO SHRINK WITH THE BASE. At +/-1 on a 6px
    # base, chance alone is 50% and the lift test waved through half of
    # a set of genuinely random gaps. Measured: 2 of 4 random sets came
    # back "base 6". A detector that invents a design system where there
    # is none is worse than one that finds nothing, because the audit
    # then holds a page to a grid it never had.
    best, best_lift = None, 0.0
    for base, tol in ((8, 1), (6, 0), (4, 0)):
        hit = sum(1 for g in spacing
                  if abs(g - round(g / base) * base) <= tol)
        rate = hit / len(spacing)
        chance = min(1.0, (2 * tol + 1) / base)
        lift = rate - chance
        if rate >= 0.7 and lift > best_lift:
            best, best_lift = base, lift
    return best if best_lift >= 0.25 else None


# ───────────────────────────── the audit ─────────────────────────────

def audit(items, spec=None, mobile=False):
    """Every way this page departs from a design system, as repairs.

    Same finding shape as `aethron_eye`, so both flow into the same
    loop and the same report: what is wrong, which selector, what to
    change.
    """
    out = []
    txt = _text_items(items)
    spec = spec or {}
    scale = spec.get("type_scale")
    if not scale:
        scale = infer(items)["type_scale"]
    scale = sorted(set(scale))

    # ── type scale ────────────────────────────────────────────────
    # A PAGE WITH NO TYPE SCALE IS THE COMMONEST GENERATED-DESIGN TELL.
    # Every size is chosen locally and nothing is chosen twice, so the
    # page has no hierarchy, only variety.
    off = Counter()
    for i in txt:
        fs = i["fs"]
        near = min(scale, key=lambda s: abs(s - fs)) if scale else fs
        if scale and abs(near - fs) > max(0.6, SIZE_SNAP * fs):
            off[i["sel"]] = (fs, near)
    for sel, (fs, near) in list(off.items())[:20]:
        out.append({
            "kind": "OFF SCALE", "text": "", "selector": sel,
            "tag": "type", "want": near, "got": fs,
            "fix": f"font-size {fs}px is not on the page's type scale "
                   f"{scale} — use {near}px",
        })
    # COUNT WHAT THE PAGE USES, NOT WHAT WAS INFERRED FROM IT. `infer`
    # deliberately keeps the scale compact — it stops once the sizes it
    # has cover 90% of the text — so testing ITS length can never fire:
    # a page setting eleven arbitrary sizes still infers a short scale
    # and reports the rest one at a time. The signal is how many
    # distinct sizes actually reach the screen carrying real text.
    used = sorted({round(i["fs"], 1) for i in txt
                   if len((i.get("text") or "").strip()) >= 4})
    if len(used) > MAX_STEPS:
        out.append({
            "kind": "NO TYPE SCALE", "text": "", "selector": None,
            "tag": "type", "want": "5-7 steps", "got": len(used),
            "fix": f"the page sets type at {len(used)} distinct sizes "
                   f"({used}); a scale is {MAX_STEPS} steps at most — "
                   f"collapse the "
                   f"near-duplicates onto {scale}",
        })

    # ── spacing ───────────────────────────────────────────────────
    base = spec.get("space_base") or step_scale(infer(items)["spacing"])
    if base:
        rows = EYE._reading_order(items)
        seen = set()
        for a, bx in zip(rows, rows[1:]):
            g = bx["y"] - (a["y"] + a["h"])
            if not (0 < g < 200):
                continue
            if abs(g - round(g / base) * base) > SPACE_SNAP:
                key = bx.get("sel")
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "kind": "OFF GRID", "text": (bx.get("text") or "")[:40],
                    "selector": key, "tag": "space",
                    "want": int(round(g / base) * base), "got": int(g),
                    "fix": f"the gap above this is {int(g)}px; the page's "
                           f"spacing step is {base}px — use "
                           f"{int(round(g / base) * base)}px",
                })
                if len(seen) >= 12:
                    break

    # ── contrast ──────────────────────────────────────────────────
    # MEASURED AGAINST WHAT IS REALLY BEHIND THE TEXT. An element's own
    # background is usually transparent, so a check that reads it finds
    # nothing and passes everything.
    for i in txt:
        fg = EYE._rgb(i.get("color"))
        bg = EYE._rgb(i.get("effbg"))
        if not fg or not bg:
            continue
        ratio = contrast(fg, bg)
        large = i["fs"] >= 24 or (i["fs"] >= 19
                                  and str(i.get("fw", "")) in
                                  ("bold", "700", "800", "900"))
        need = AA_LARGE if large else AA_BODY
        if ratio < need:
            out.append({
                "kind": "LOW CONTRAST",
                "text": (i.get("text") or "")[:40],
                "selector": i["sel"], "tag": "color",
                "want": f"{need}:1", "got": f"{ratio:.2f}:1",
                "fix": f"text #%02X%02X%02X on #%02X%02X%02X is "
                       f"{ratio:.2f}:1, below WCAG AA ({need}:1) — "
                       f"darken the text or lighten the ground"
                       % (fg + bg),
            })

    # ── alignment ─────────────────────────────────────────────────
    # EDGES THAT NEARLY AGREE ARE THE LOUDEST DEFECT IN AN INTERFACE.
    # A 2px misalignment reads as broken in a way a 40px offset never
    # does, because the eye is looking for the line.
    lefts = Counter(i["x"] for i in items if i.get("w", 0) > 24)
    strong = {x for x, n in lefts.items() if n >= 3}
    for i in items:
        if i.get("w", 0) <= 24 or i["x"] in strong:
            continue
        near = [x for x in strong if 0 < abs(x - i["x"]) <= 4]
        if near:
            out.append({
                "kind": "NOT ALIGNED", "text": (i.get("text") or "")[:40],
                "selector": i["sel"], "tag": "layout",
                "want": near[0], "got": i["x"],
                "fix": f"this sits at x={i['x']} while {lefts[near[0]]} "
                       f"other elements align at x={near[0]} — a {abs(near[0] - i['x'])}px "
                       f"miss reads as broken; align it",
            })

    # ── tap targets ───────────────────────────────────────────────
    if mobile:
        for i in items:
            if i.get("tag") not in ("button", "a"):
                continue
            if not (i.get("text") or "").strip():
                continue
            if min(i.get("w", 0), i.get("h", 0)) < TAP_MIN:
                out.append({
                    "kind": "TAP TARGET",
                    "text": (i.get("text") or "")[:40],
                    "selector": i["sel"], "tag": "a11y",
                    "want": f"{TAP_MIN}x{TAP_MIN}",
                    "got": f"{i.get('w')}x{i.get('h')}",
                    "fix": f"{i.get('w')}x{i.get('h')}px is below the "
                           f"{TAP_MIN}px both Apple and Google call the "
                           f"minimum — add padding",
                })
    return out


def look(page, widths=(390, 1280), spec=None, verbose=True):
    """Hold a built page to a design system and report the repairs.

    Judged narrow AND wide, because tap targets and line lengths only
    fail on a phone, and alignment only fails where there is room to be
    misaligned.
    """
    def say(*a):
        if verbose:
            print(*a)

    rounds, all_f = [], []
    for w in widths:
        r = EYE.read_page(page, w)
        if r is None:
            say(f"  {w:>5}px  UNMEASURED")
            rounds.append({"width": w, "skipped": True})
            continue
        f = audit(r["items"], spec, mobile=(w <= 480))
        rounds.append({"width": w, "findings": f,
                       "system": infer(r["items"])})
        all_f.extend(f)
        kinds = Counter(x["kind"] for x in f)
        say(f"  {w:>5}px  {len(f)} finding(s)  "
            + (", ".join(f"{k.lower()} {n}" for k, n in kinds.most_common())
               or "clean"))
    graded = [r for r in rounds if "findings" in r]
    if not graded:
        return {"verdict": "SKIPPED", "why": "the page did not render",
                "findings": [], "rounds": rounds}
    # SEVERITY IS NOT COUNT. One unreadable heading matters more than a
    # dozen gaps that are 2px off a grid nobody will ever measure.
    hard = [f for f in all_f
            if f["kind"] in ("LOW CONTRAST", "TAP TARGET", "NO TYPE SCALE")]
    verdict = "PASS" if not hard and len(all_f) <= 6 else "FAIL"
    return {"verdict": verdict, "findings": all_f, "rounds": rounds,
            "system": graded[0]["system"],
            "why": (f"{len(all_f)} design finding(s), "
                    f"{len(hard)} of them serious")}


def main(argv):
    if not argv or {"-h", "--help"} & set(argv):
        print(__doc__.split("\n\n")[0])
        print("\nusage: aethron_design.py <page-or-url> [--spec s.json]")
        print("       --system   just print the design system it uses")
        return 0
    spec = None
    if "--spec" in argv:
        spec = json.loads(Path(argv[argv.index("--spec") + 1]).read_text())
    if "--system" in argv:
        r = EYE.read_page(argv[0], 1280)
        if r is None:
            print("SKIPPED — the page did not render")
            return 1
        sysm = infer(r["items"])
        print(json.dumps({
            "type_scale": sysm["type_scale"],
            "space_base": step_scale(sysm["spacing"]),
            "spacing_seen": sysm["spacing"],
            "text_colors": ["#%02X%02X%02X" % c
                            for c in sysm["text_colors"]],
            "grounds": ["#%02X%02X%02X" % c for c in sysm["grounds"]],
        }, indent=1))
        return 0
    r = look(argv[0], spec=spec)
    print()
    print(EYE.brief(r, limit=30))
    return 0 if r["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
