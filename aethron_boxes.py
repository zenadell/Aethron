#!/usr/bin/env python3
"""THE BOX TREE — what a screenshot is actually MADE OF.

The gate for screenshot-to-website, and the thing every previous
attempt tripped over. A page is a nest of rectangles: a panel holding
cards holding buttons. Recover that nest and everything downstream
falls out of it — the fills become CSS, the nesting becomes structure,
the leaves become buttons and inputs, and nothing has to be carried as
a picture. Fail to recover it and the tool has no choice but to paste
the screenshot behind the text and call it a page, which is exactly
what it was doing.

WHY COLOUR CANNOT FIND THEM, settled with numbers rather than opinion.
The old detector asked "does this region differ from the ground beneath
it?" and the ground is estimated per cell, so every cell inside a
200x70 card is entirely card and the estimate adopts the card's own
colour. The card matches the ground, is rejected, and is never seen.
Asking the neighbours instead breaks that circle — and then admits the
page's own background panel, and no interior test separates the two,
because REAL BUTTONS ARE GRADIENTS. Measured on one page:

    want IN   white pill 0.711   orange card 0.307   Home pill 0.154
    want OUT  hero panel 0.411   whole canvas 0.462

Fully overlapping. There is no threshold there to find.

SO KEY ON THE BOUNDARY. A button has a crisp closed edge whether it is
flat, gradient, glassy or shadowed; a glow has no edge anywhere. That
is what a person's eye uses and it is cheap to measure: find the rows
and columns carrying long runs of step-change, pair them into
rectangles, and accept a rectangle only when its own perimeter is
really there.

The panel is not rejected for being big. It is emitted AS a panel, with
its gradient read from its own interior — which is what dissolves the
problem the colour tests kept hitting.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_vision as V            # noqa: E402


# ───────────────────────────── edges ─────────────────────────────────

def edge_maps(shot, step=1, th=16):
    """Where the image steps, per column and per row.

    `th` is a step in summed RGB — about five per channel. SWEPT, not
    chosen, against ten elements picked off a real page by hand:

        th=48   22 rectangles   3 of 10 found
        th=30   38 rectangles   6 of 10
        th=22   55 rectangles   7 of 10
        th=16   74 rectangles   8 of 10

    A high bar is not "conservative" here, it is blind: the nav pills
    and the hero buttons sit ON a bright glow, so their edges are a few
    shades, and a detector that wants a hard step never sees the very
    controls a person most wants to click. Extra rectangles are cheap —
    nesting and dedupe sort them out — and a missing one is not
    recoverable downstream at all.

    SCAN EVERY LINE. The first version stepped by two and then measured
    each edge's support over every row, so no edge could ever score
    above 0.5 against a 0.55 bar and the detector found NOTHING at all.
    An instrument that samples half as often as it scores is not
    measuring what it reports.
    """
    w, h, px = shot.w, shot.h, shot.px

    def lum(x, y):
        i = (y * w + x) * 4
        return px[i] + px[i + 1] + px[i + 2]

    vert = [bytearray(h) for _ in range(w)]     # vert[x][y] = 1
    horiz = [bytearray(w) for _ in range(h)]    # horiz[y][x] = 1
    for y in range(2, h - 2, step):
        for x in range(2, w - 2):
            if abs(lum(x + 2, y) - lum(x - 2, y)) > th:
                vert[x][y] = 1
    for x in range(2, w - 2, step):
        for y in range(2, h - 2):
            if abs(lum(x, y + 2) - lum(x, y - 2)) > th:
                horiz[y][x] = 1
    return vert, horiz


def runs_of(bits, lo, hi, gap=6, min_len=24):
    """Contiguous stretches of edge, tolerating small interruptions.

    A card's top edge is broken wherever a label or an icon crosses it;
    demanding an unbroken run finds nothing on a real page.
    """
    out, start, miss = [], None, 0
    for i in range(lo, hi):
        if bits[i]:
            if start is None:
                start = i
            miss = 0
        elif start is not None:
            miss += 1
            if miss > gap:
                if i - miss - start >= min_len:
                    out.append((start, i - miss))
                start, miss = None, 0
    if start is not None and hi - miss - start >= min_len:
        out.append((start, hi - miss))
    return out


def support(bits, a, b, step=1):
    """What share of a-to-b actually carries an edge?"""
    n = ok = 0
    for i in range(a, b, step):
        n += 1
        ok += bits[i]
    return ok / n if n else 0.0


# ─────────────────────────── rectangles ──────────────────────────────

def rectangles(shot, vert=None, horiz=None, min_w=28, min_h=16,
               side=0.55, slack=6, step=1, th=16):
    """Every rectangle whose own four edges are really present.

    Built from the TOP EDGE down rather than by trying every pair of
    lines: for each horizontal run, look for a run below it with the
    same x-extent, then verify the two sides. That is a few hundred
    candidates on a real page instead of the tens of millions a
    pair-of-every-line search would produce.
    """
    if vert is None or horiz is None:
        vert, horiz = edge_maps(shot, step=step, th=th)
    w, h = shot.w, shot.h

    tops = []
    for y in range(2, h - 2):
        for a, b in runs_of(horiz[y], 2, w - 2, min_len=min_w):
            tops.append((y, a, b))

    seen, out = set(), []
    for i, (y1, a1, b1) in enumerate(tops):
        for y2, a2, b2 in tops[i + 1:]:
            if y2 - y1 < min_h:
                continue
            if y2 - y1 > h * 0.98:
                break
            if abs(a1 - a2) > slack or abs(b1 - b2) > slack:
                continue
            x1, x2 = min(a1, a2), max(b1, b2)
            if x2 - x1 < min_w:
                continue
            left = support(vert[x1], y1, y2)
            right = support(vert[min(w - 1, x2)], y1, y2)
            if left < side or right < side:
                continue
            top = support(horiz[y1], x1, x2)
            bot = support(horiz[y2], x1, x2)
            if top < side or bot < side:
                continue
            key = (x1 // 4, y1 // 4, x2 // 4, y2 // 4)
            if key in seen:
                continue
            seen.add(key)
            out.append({"x": x1, "y": y1, "w": x2 - x1 + 1,
                        "h": y2 - y1 + 1,
                        "edge": round(min(left, right, top, bot), 2)})
    out.sort(key=lambda b: -(b["w"] * b["h"]))
    return dedupe(out, slack)


def dedupe(boxes, tol=5):
    """One edge is one rectangle.

    An antialiased border is two or three pixels thick, so the same
    card is found at y=38 and again at y=40. Left alone they NEST
    inside each other and the tree comes out six deep for a page that
    is two — structure invented out of blur. Rectangles agreeing on all
    four sides within a few pixels are the same rectangle; keep the one
    whose perimeter is best supported.
    """
    kept = []
    for b in boxes:
        twin = None
        for k in kept:
            if (abs(k["x"] - b["x"]) <= tol and abs(k["y"] - b["y"]) <= tol
                    and abs(k["w"] - b["w"]) <= tol * 2
                    and abs(k["h"] - b["h"]) <= tol * 2):
                twin = k
                break
        if twin is None:
            kept.append(b)
        elif b["edge"] > twin["edge"]:
            kept[kept.index(twin)] = b
    return kept


def nest(boxes, pad=3):
    """Put the rectangles into the tree they actually form.

    Containment is the whole relationship: a card inside a panel is a
    child of it, and that nesting IS the page's structure. Emitted as a
    tree, the output stops being a pile of absolutely-placed divs.
    """
    items = [dict(b, children=[]) for b in boxes]
    items.sort(key=lambda b: -(b["w"] * b["h"]))

    def inside(c, p):
        return (c["x"] >= p["x"] - pad and c["y"] >= p["y"] - pad
                and c["x"] + c["w"] <= p["x"] + p["w"] + pad
                and c["y"] + c["h"] <= p["y"] + p["h"] + pad
                and c["w"] * c["h"] < p["w"] * p["h"])

    roots = []
    for c in items:
        host = None
        for p in items:
            if p is c or not inside(c, p):
                continue
            if host is None or p["w"] * p["h"] < host["w"] * host["h"]:
                host = p
        (host["children"] if host else roots).append(c)
    return roots


# ────────────────────────── each box's own fill ──────────────────────

def fill_css(shot, b, samples=7):
    """Read a box's background AS CSS, from its own interior.

    THIS IS WHAT RETIRES THE CARRIED PLATE. The old pipeline had two
    categories — "an element", painted as one flat colour, and
    "everything else", photographed and stretched. A real page has no
    such split: a card is a gradient, a pill is a gradient, the hero
    panel is a gradient with a glow on it. Given the box, the gradient
    is simply readable, and a gradient that is read is CSS rather than
    a picture.

    Flat first, then a linear ramp along whichever axis actually
    varies. Returns (css, quality) where quality is how well the fit
    reproduces the interior — the caller emits nothing it cannot
    reproduce, rather than painting a wrong colour confidently.
    """
    x, y, w, h = b["x"], b["y"], b["w"], b["h"]
    ix, iy = max(1, w // 8), max(1, h // 8)
    x0, x1 = x + ix, max(x + ix + 1, x + w - ix)
    y0, y1 = y + iy, max(y + iy + 1, y + h - iy)

    def mean(ax0, ay0, ax1, ay1):
        r = g = bl = n = 0
        for yy in range(ay0, min(ay1, shot.h), max(1, (ay1 - ay0) // 6)):
            for xx in range(ax0, min(ax1, shot.w),
                            max(1, (ax1 - ax0) // 6)):
                c = shot.rgb(xx, yy)
                r += c[0]; g += c[1]; bl += c[2]; n += 1
        return (r // n, g // n, bl // n) if n else (0, 0, 0)

    cols = [mean(x0 + (x1 - x0) * i // samples, y0,
                 x0 + (x1 - x0) * (i + 1) // samples, y1)
            for i in range(samples)]
    rows = [mean(x0, y0 + (y1 - y0) * i // samples,
                 x1, y0 + (y1 - y0) * (i + 1) // samples)
            for i in range(samples)]
    spread = lambda v: max(max(abs(a[k] - c[k]) for k in range(3))
                           for a in v for c in v)
    sx, sy = spread(cols), spread(rows)
    flat = mean(x0, y0, x1, y1)

    if max(sx, sy) <= V.MERGE:
        css = "#%02X%02X%02X" % flat
        stops = [flat] * samples
    elif sx >= sy:
        stops, css = cols, _ramp("to right", cols)
    else:
        stops, css = rows, _ramp("to bottom", rows)

    # quality: how far the fit is from the real interior, worst case
    want = cols if (sx >= sy and max(sx, sy) > V.MERGE) else (
        rows if max(sx, sy) > V.MERGE else [flat] * samples)
    err = max(max(abs(a[k] - c[k]) for k in range(3))
              for a, c in zip(stops, want)) if stops else 255
    return css, max(0.0, 1.0 - err / 96.0)


def _ramp(side, stops):
    return (f"linear-gradient({side},"
            + ",".join(f"rgb({c[0]},{c[1]},{c[2]}) "
                       f"{i / (len(stops) - 1) * 100:.0f}%"
                       for i, c in enumerate(stops)) + ")")


def radius_of(shot, b, cap=64):
    """The corner radius, measured from the BOUNDARY.

    `corner_radius` looks for where the box's own FILL reaches its edge,
    which needs a flat fill — so on a gradient pill it finds nothing and
    reports 0, and every rounded control on the owner's page came back
    square. The boundary does not care about the fill: walk down the
    left edge and find where the element starts differing from what
    surrounds it. The inset shrinks to zero exactly at the radius.
    """
    x, y, w, h = b["x"], b["y"], b["w"], b["h"]
    outside = _around(shot, x, y, w, h)
    limit = min(cap, w // 2, h // 2)
    if limit < 2:
        return 0
    last = 0
    for dy in range(limit):
        yy = y + dy
        if yy >= shot.h:
            break
        inset = None
        for dx in range(limit + 2):
            xx = x + dx
            if xx >= shot.w:
                break
            if V._dist(shot.rgb(xx, yy), outside) > V.INK:
                inset = dx
                break
        if inset is None:
            continue                  # a row still entirely outside
        if inset >= 2:
            last = dy + 1
        elif last:
            break                     # the corner has closed
    return last


def _around(shot, x, y, w, h, pad=4):
    """The commonest colour in a ring just outside the box."""
    from collections import Counter
    c = Counter()
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(shot.w - 1, x + w + pad), min(shot.h - 1, y + h + pad)
    for xx in range(x0, x1 + 1, max(1, (x1 - x0) // 40)):
        for yy in (y0, y1):
            c[shot.rgb(xx, yy)] += 1
    for yy in range(y0, y1 + 1, max(1, (y1 - y0) // 40)):
        for xx in (x0, x1):
            c[shot.rgb(xx, yy)] += 1
    return c.most_common(1)[0][0] if c else (0, 0, 0)


def styled(shot, boxes, min_quality=0.55):
    """Boxes with their CSS, dropping any the fit cannot reproduce.

    MEASURED WARNING — DO NOT OVERLAY THESE ON THE CARRIED PLATE. Doing
    exactly that dropped the owner's page from 0.97266 to 0.88103
    identical. Two reasons, and both are about integration rather than
    detection:

      * at th=16 the recovered set includes the page's own faint GRID
        CELLS, whose interiors really are smooth gradients and so fit
        perfectly — they are true rectangles and false elements;
      * a seven-stop linear fit is COARSER than the plate it is painted
        over, so even a correct box degrades the area it covers.

    The sweep that chose th=16 measured RECALL and never precision,
    which is how 74 rectangles looked like a win. The box tree is for
    REPLACING the plate, not for drawing on top of it: once a box owns
    its own area there is nothing underneath to degrade, and a grid
    cell that is drawn in place of the background it was cut from costs
    nothing. Until the plate is gone, these are measurements, not
    output.
    """
    out = []
    for b in boxes:
        css, q = fill_css(shot, b)
        if q < min_quality:
            continue
        out.append(dict(b, css=css, quality=round(q, 2),
                        radius=radius_of(shot, b)))
    return out


def depth_of(roots):
    def d(n):
        return 1 + max([d(c) for c in n["children"]] or [0])
    return max([d(r) for r in roots] or [0])


def flatten(roots):
    out = []

    def walk(n, lvl):
        out.append((lvl, n))
        for c in n["children"]:
            walk(c, lvl + 1)
    for r in roots:
        walk(r, 0)
    return out


def _selftest():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fail += 1
            print(f"  FAIL {name}   {detail}")

    # A page whose answer we know: a panel, two cards inside it, and a
    # GRADIENT button — the case no interior test can classify.
    W, H = 600, 400
    px = bytearray()
    for y in range(H):
        for x in range(W):
            c = (20, 20, 22)                          # page
            if 40 <= x < 560 and 40 <= y < 360:
                c = (48, 48, 52)                      # panel
            if 70 <= x < 270 and 80 <= y < 200:
                c = (90, 90, 96)                      # card A
            if 300 <= x < 500 and 80 <= y < 200:
                c = (90, 90, 96)                      # card B
            if 70 <= x < 200 and 250 <= y < 290:
                t = (x - 70) / 130                    # a GRADIENT button
                c = (int(250 - 60 * t), int(90 + 40 * t), 40)
            px += bytes((c[0], c[1], c[2], 255))
    shot = V.Shot(W, H, px)

    print("── rectangles, from their edges")
    rects = rectangles(shot)
    def find(x, y, w, h, tol=6):
        return next((b for b in rects
                     if abs(b["x"] - x) <= tol and abs(b["y"] - y) <= tol
                     and abs(b["w"] - w) <= tol * 2
                     and abs(b["h"] - h) <= tol * 2), None)
    check("the panel is found", find(40, 40, 520, 320) is not None,
          str([(b["x"], b["y"], b["w"], b["h"]) for b in rects[:6]]))
    check("card A is found", find(70, 80, 200, 120) is not None)
    check("card B is found", find(300, 80, 200, 120) is not None)
    check("THE GRADIENT BUTTON IS FOUND — the case colour cannot judge",
          find(70, 250, 130, 40) is not None,
          str([(b["x"], b["y"], b["w"], b["h"]) for b in rects]))
    check("the flat background is not a rectangle",
          not any(b["w"] > W * 0.95 and b["h"] > H * 0.95 for b in rects))

    print("\n── and they nest into the structure they really have")
    roots = nest(rects)
    check("one root", len(roots) == 1, str(len(roots)))
    if roots:
        kids = roots[0]["children"]
        check("  ...the panel holds the three elements", len(kids) == 3,
              str([(k["x"], k["y"]) for k in kids]))
    check("the tree is two deep", depth_of(roots) == 2, str(depth_of(roots)))
    check("every rectangle is in the tree",
          len(flatten(roots)) == len(rects))

    print("\n── runs tolerate a label crossing an edge")
    bits = bytearray([1] * 40 + [0] * 4 + [1] * 40 + [0] * 30)
    check("a small gap does not split a run",
          runs_of(bits, 0, 114) == [(0, 83)], str(runs_of(bits, 0, 114)))
    check("a large one does",
          len(runs_of(bytearray([1] * 40 + [0] * 30 + [1] * 40), 0, 110)) == 2)

    print(f"\nbox selftest: {ok} ok, {fail} failed")
    return 1 if fail else 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: aethron_boxes.py <screenshot> [--json]")
        print("       aethron_boxes.py --selftest")
        return 0
    if argv[0] == "--selftest":
        return _selftest()
    shot = V.load(argv[0])
    rects = rectangles(shot)
    roots = nest(rects)
    if "--json" in argv:
        print(json.dumps(roots, indent=1))
        return 0
    print(f"{len(rects)} rectangle(s), {depth_of(roots)} deep\n")
    for lvl, b in flatten(roots):
        print(f"{'  ' * lvl}({b['x']:4d},{b['y']:4d}) {b['w']:4d}x{b['h']:3d}"
              f"   edge {b['edge']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
