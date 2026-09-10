#!/usr/bin/env python3
"""Read a screenshot as NUMBERS, before any model is allowed to look.

WHY THIS EXISTS
---------------
Screenshot-to-code is bad for a measured reason, not a vague one. A 2026
benchmark perturbed one card's width or one text's font-size so it broke
the repeated pattern, then asked multimodal models to recover the value:

    card width   21.17% correct
    font size     7.89% correct

They call it PATTERN COMPLETION BIAS. The model is not reading the
pixels; it is completing a pattern it learned in training, so anything
slightly unusual comes back as the usual thing — confidently. A stronger
model is still guessing.

But a PNG is not a picture. It is an array of exact integers, and almost
everything a design system cares about is sitting in there as a number
nobody bothers to read:

    colour            exact, not "a lime green"
    section bounds    exact y, from where ink starts and stops
    card geometry     exact x/y/w/h of a solid fill
    corner radius     measured from the curve at the corner
    text size         measured from the height of the ink
    borders           measured from the ring around a fill

So this module measures. It contains NO model and never asks for a
value it can compute. That is the same division this project already
lives by — the model decides meaning, the tool decides physics — and it
is why template migration lands at 100% while eyeballing lands at 8%.

WHAT IT DOES NOT DO, and cannot:
    A single frame carries NO animation, no hover state, no other
    breakpoint, and nothing scrolled out of shot. That is not a hard
    problem, it is an absent one. Ask for a recording, or for the URL.

    python3 aethron_vision.py <image>            the measurements
    python3 aethron_vision.py --selftest         prove it can see
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aethron_figma_grade as G   # noqa: E402  (the stdlib PNG codec)

# Two colours within this per-channel distance are the same colour with
# antialiasing between them, not two design decisions.
MERGE = 12
# A pixel this far from the background counts as ink.
INK = 24
# Smaller than this is noise, not a component of the design.
MIN_RUN = 8
MIN_BOX = 12
# Taller than this is not one line of type, whatever the ink says.
MAX_LINE = 120


class Shot:
    """An image as measurable numbers."""

    def __init__(self, w, h, rgba):
        self.w, self.h, self.px = w, h, rgba

    def rgb(self, x, y):
        i = (y * self.w + x) * 4
        return self.px[i], self.px[i + 1], self.px[i + 2]

    def hex(self, x, y):
        return "#%02X%02X%02X" % self.rgb(x, y)


def load(path) -> Shot:
    """Any image the machine can convert -> a Shot.

    PNG is read directly by our own decoder. Anything else is converted
    first, because a JPEG screenshot is the common case and refusing it
    would make the whole feature academic. macOS ships `sips`; elsewhere
    we say so plainly instead of half-working.
    """
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"no such image: {path}")
    if path.suffix.lower() != ".png":
        sips = shutil.which("sips")
        if not sips:
            raise SystemExit(
                f"{path.suffix} needs converting to PNG first and no "
                f"converter was found (macOS ships `sips`). Save the "
                f"screenshot as PNG and try again.")
        tmp = Path(tempfile.mkdtemp(prefix="ae-vision-")) / "in.png"
        r = subprocess.run([sips, "-s", "format", "png", str(path),
                            "--out", str(tmp)],
                           capture_output=True, text=True)
        if r.returncode or not tmp.is_file():
            raise SystemExit(f"could not convert {path}: {r.stderr[:200]}")
        path = tmp
    w, h, rgba = G.read_png(path)
    return Shot(w, h, rgba)


# ─────────────────────────── colour ──────────────────────────────────

def _dist(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def palette(shot: Shot, top=12, step=1, skip=None):
    """The design's real colours, most-used first.

    Antialiasing invents thousands of intermediate colours that no
    designer chose. They are always LOW-count and always sit between two
    real ones, so counting exactly and then folding each rare colour
    into the nearest frequent one recovers the palette instead of a
    smear. Folding runs most-frequent-first, so a real colour is never
    absorbed into a fringe.
    """
    c = Counter()
    px, w = shot.px, shot.w
    skip = skip or []
    for y in range(0, shot.h, step):
        row = y * w * 4
        # A ramp is one design decision, not ten thousand colours. Its
        # pixels are reported as a gradient and must not also compete
        # for the palette — unfiltered, a single hero glow took eight of
        # the top ten slots and pushed the real tokens out entirely.
        if any(r["box"]["y"] <= y < r["box"]["y"] + r["box"]["h"]
               for r in skip):
            continue
        for x in range(0, w, step):
            i = row + x * 4
            if px[i + 3] < 128:            # transparent pixels are not colour
                continue
            c[(px[i], px[i + 1], px[i + 2])] += 1

    kept = []                              # [(rgb, count)], strongest first
    for rgb, n in c.most_common():
        for j, (krgb, kn) in enumerate(kept):
            if _dist(rgb, krgb) <= MERGE:
                kept[j] = (krgb, kn + n)
                break
        else:
            kept.append((rgb, n))
        if len(kept) > top * 8:            # enough to rank; stop counting
            break
    kept.sort(key=lambda t: -t[1])
    total = sum(n for _, n in kept) or 1
    return [{"hex": "#%02X%02X%02X" % rgb, "rgb": list(rgb),
             "share": round(n / total, 4)} for rgb, n in kept[:top]]


SMOOTH = 14      # per-step colour change that still reads as one ramp
RAMP = 40        # total change before a run is a gradient, not noise


def gradients(shot: Shot, step=4):
    """Find smooth ramps, because a flat-colour palette cannot hold one.

    MEASURED, on a real hero with an orange bloom: eight of the top ten
    "colours" were samples of the gradient and all eight detected
    regions were slabs of it. The white heading and the button did not
    appear at all. A ramp is thousands of almost-colours, so ranking by
    area buries the handful of real tokens underneath it.

    So a ramp is identified as ONE object and taken out of the flat
    palette. Runs are found on row means (and column means) where each
    step changes a little, consistently, and the run as a whole changes
    a lot.

    HONESTY: only an axis-aligned linear ramp is fitted. If the rows
    inside a run are not themselves uniform, the ramp is radial or
    multi-axis, and this says so rather than reporting stops that would
    be confidently wrong.
    """
    w, h = shot.w, shot.h
    xs = list(range(0, w, max(1, w // 48)))

    def row_mean(y):
        r = g = b = 0
        for x in xs:
            c = shot.rgb(x, y)
            r += c[0]; g += c[1]; b += c[2]
        n = len(xs)
        return (r // n, g // n, b // n)

    ys = list(range(0, h, step))
    means = [row_mean(y) for y in ys]
    out, i = [], 0
    while i < len(means) - 1:
        j = i
        # `0 < delta` used to end the run here, which broke a ramp at
        # every plateau — a long glow came back as two short pieces and
        # the rest of it leaked through as "solid regions". A flat
        # stretch inside a ramp is still the ramp; only a JUMP ends it.
        while (j < len(means) - 1
               and _dist(means[j], means[j + 1]) <= SMOOTH):
            j += 1
        if j > i and _dist(means[i], means[j]) >= RAMP:
            top, bot = ys[i], ys[j]
            # is each row inside the run flat across its width?
            spread = 0
            for y in (ys[i], ys[(i + j) // 2], ys[j]):
                row = [shot.rgb(x, y) for x in xs]
                spread = max(spread, max(_dist(row[0], c) for c in row))
            g = {"box": {"x": 0, "y": top, "w": w, "h": bot - top + 1},
                 "from": "#%02X%02X%02X" % means[i],
                 "to": "#%02X%02X%02X" % means[j]}
            if spread <= SMOOTH * 2:
                g["axis"] = "vertical"
                g["css"] = (f"linear-gradient(180deg, {g['from']} 0%, "
                            f"{g['to']} 100%)")
            else:
                g["axis"] = "not linear"
                g["note"] = ("colour also changes across each row — this "
                             "is radial or multi-axis and is NOT fitted; "
                             "the two colours are its ends, not stops")
            out.append(g)
            i = j
        else:
            i = j + 1 if j > i else i + 1
    # Stitch pieces separated by a short interruption — a logo strip or
    # a line of text crossing a glow splits the run without ending the
    # gradient underneath it.
    merged = []
    for g in out:
        if merged:
            prev = merged[-1]["box"]
            if g["box"]["y"] - (prev["y"] + prev["h"]) <= 56:
                prev["h"] = g["box"]["y"] + g["box"]["h"] - prev["y"]
                merged[-1]["to"] = g["to"]
                if merged[-1]["axis"] == "vertical" and g["axis"] != "vertical":
                    merged[-1]["axis"] = g["axis"]
                    merged[-1].pop("css", None)
                    merged[-1]["note"] = g.get("note", "")
                continue
        merged.append(g)
    return merged


def _in_any(regions, x, y, w=1, h=1):
    cx, cy = x + w // 2, y + h // 2
    for r in regions:
        b = r["box"] if "box" in r else r
        if (b["x"] <= cx < b["x"] + b["w"]
                and b["y"] <= cy < b["y"] + b["h"]):
            return True
    return False


def background(shot: Shot):
    """The page colour, taken from the BORDER, not the whole image.

    The most common colour overall is whatever fills the most area — on
    a dark hero that is the hero, not the page. The outer ring of a
    full-page screenshot is almost always the page itself.
    """
    c = Counter()
    for x in range(0, shot.w, 2):
        c[shot.rgb(x, 0)] += 1
        c[shot.rgb(x, shot.h - 1)] += 1
    for y in range(0, shot.h, 2):
        c[shot.rgb(0, y)] += 1
        c[shot.rgb(shot.w - 1, y)] += 1
    rgb = c.most_common(1)[0][0]
    return {"hex": "#%02X%02X%02X" % rgb, "rgb": list(rgb)}


# ─────────────────────────── layout ──────────────────────────────────

def _ink_rows(shot: Shot, bg):
    """Per row: how many pixels differ from the background."""
    out = []
    px, w = shot.px, shot.w
    for y in range(shot.h):
        row, n = y * w * 4, 0
        for x in range(w):
            i = row + x * 4
            if (abs(px[i] - bg[0]) > INK or abs(px[i + 1] - bg[1]) > INK
                    or abs(px[i + 2] - bg[2]) > INK):
                n += 1
        out.append(n)
    return out


def bands(shot: Shot, bg, gap=6):
    """Horizontal bands of content, with the empty space between them.

    This is what a section IS, measured: a run of rows carrying ink,
    bounded by rows carrying none. The gaps are as much a design
    decision as the bands — they are the vertical rhythm — so they are
    reported rather than discarded.
    """
    rows = _ink_rows(shot, bg)
    out, start, blank = [], None, 0
    for y, n in enumerate(rows):
        if n > 0:
            if start is None:
                start = y
            blank = 0
        else:
            if start is not None:
                blank += 1
                if blank >= gap:
                    out.append((start, y - blank))
                    start = None
    if start is not None:
        out.append((start, shot.h - 1))
    bands_ = [{"top": a, "bottom": b, "height": b - a + 1,
               "ink_max": max(rows[a:b + 1] or [0])}
              for a, b in out if b - a + 1 >= 2]
    for i, b in enumerate(bands_):
        b["gap_above"] = (b["top"] - bands_[i - 1]["bottom"] - 1
                          if i else b["top"])
    return bands_


def columns(shot: Shot, bg, top, bottom, gap=8):
    """Vertical runs of content inside a band — the column grid."""
    px, w = shot.px, shot.w
    ink = [0] * w
    for y in range(top, bottom + 1):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            if (abs(px[i] - bg[0]) > INK or abs(px[i + 1] - bg[1]) > INK
                    or abs(px[i + 2] - bg[2]) > INK):
                ink[x] += 1
    out, start, blank = [], None, 0
    for x, n in enumerate(ink):
        if n > 0:
            if start is None:
                start = x
            blank = 0
        else:
            if start is not None:
                blank += 1
                if blank >= gap:
                    out.append({"left": start, "right": x - blank,
                                "width": x - blank - start + 1})
                    start = None
    if start is not None:
        out.append({"left": start, "right": w - 1, "width": w - start})
    return [c for c in out if c["width"] >= MIN_RUN]


# ─────────────────────────── solid regions ───────────────────────────

def _runs(shot: Shot, y, bg):
    """Runs of one colour across a row, ignoring the background."""
    px, w = shot.px, shot.w
    row, out = y * w * 4, []
    x = 0
    while x < w:
        i = row + x * 4
        c = (px[i], px[i + 1], px[i + 2])
        if _dist(c, bg) <= INK:
            x += 1
            continue
        x0 = x
        while x < w:
            i = row + x * 4
            if _dist((px[i], px[i + 1], px[i + 2]), c) > MERGE:
                break
            x += 1
        if x - x0 >= MIN_RUN:
            out.append((x0, x - 1, c))
    return out


def boxes(shot: Shot, bg, step=1):
    """Solid rectangles — cards, buttons, filled sections.

    Grown from same-coloured row runs that line up vertically. UI fills
    are flat and axis-aligned, so this finds them exactly without a
    connected-component pass over a million pixels.
    """
    open_, done = [], []
    for y in range(0, shot.h, step):
        rs = _runs(shot, y, bg)
        used = [False] * len(rs)
        for b in open_[:]:
            for k, (x0, x1, c) in enumerate(rs):
                if used[k] or _dist(c, b["rgb"]) > MERGE:
                    continue
                # the same column of fill, allowing a rounded corner to
                # narrow the run at top and bottom
                if abs(x0 - b["left"]) <= 24 and abs(x1 - b["right"]) <= 24:
                    b["left"] = min(b["left"], x0)
                    b["right"] = max(b["right"], x1)
                    b["bottom"] = y
                    b["rows"] += 1
                    used[k] = True
                    break
            else:
                open_.remove(b)
                done.append(b)
        for k, (x0, x1, c) in enumerate(rs):
            if not used[k]:
                open_.append({"left": x0, "right": x1, "top": y,
                              "bottom": y, "rgb": c, "rows": 1})
    done.extend(open_)
    out = []
    for b in done:
        w_, h_ = b["right"] - b["left"] + 1, b["bottom"] - b["top"] + 1
        if w_ < MIN_BOX or h_ < MIN_BOX:
            continue
        rgb = interior_colour(shot, b["left"], b["top"], w_, h_) or b["rgb"]
        out.append({"x": b["left"], "y": b["top"], "w": w_, "h": h_,
                    "fill": "#%02X%02X%02X" % rgb,
                    "radius": corner_radius(shot, b["left"], b["top"],
                                            w_, h_, rgb)})
    out = _rejoin(shot, bg, out)
    out.sort(key=lambda r: -(r["w"] * r["h"]))
    return out


def _rejoin(shot: Shot, bg, boxes_):
    """Put a card back together after its own contents split it.

    A row-run detector sees a filled card with a heading and a paragraph
    on it as THREE strips of fill — the bands between the text. Measured
    on a real template: one card came back as y=193 h36, y=279 h18,
    y=314 h111. Strips are not what anyone wants to generate code from.

    The join is decided by measurement, not by a spacing guess: two
    strips of the same fill and the same left/right edges are one card
    if the space BETWEEN them is not page background — because that
    space is the card's own content. If the gap is background, they are
    genuinely two cards and stay apart.
    """
    boxes_ = sorted(boxes_, key=lambda b: (b["fill"], b["x"], b["y"]))
    out, used = [], [False] * len(boxes_)
    for i, a in enumerate(boxes_):
        if used[i]:
            continue
        cur = dict(a)
        for j in range(i + 1, len(boxes_)):
            b = boxes_[j]
            if used[j] or b["fill"] != cur["fill"]:
                continue
            if abs(b["x"] - cur["x"]) > 6 or abs(
                    (b["x"] + b["w"]) - (cur["x"] + cur["w"])) > 6:
                continue
            gap_top = cur["y"] + cur["h"]
            gap_bot = b["y"]
            if gap_bot < gap_top - 2 or gap_bot - gap_top > 400:
                continue
            if _is_background(shot, bg, cur["x"], gap_top,
                              cur["w"], gap_bot - gap_top):
                continue                     # a real gap: two cards
            cur["h"] = (b["y"] + b["h"]) - cur["y"]
            used[j] = True
        used[i] = True
        out.append(cur)
    return out


def _is_background(shot: Shot, bg, x, y, w, h, thresh=0.7):
    """Is this rectangle mostly the page colour?"""
    if h <= 0 or w <= 0:
        return True
    hit = tot = 0
    for yy in range(y, min(y + h, shot.h), max(1, h // 12)):
        for xx in range(x, min(x + w, shot.w), max(1, w // 12)):
            tot += 1
            if _dist(shot.rgb(xx, yy), bg) <= INK:
                hit += 1
    return tot and hit / tot >= thresh


def interior_colour(shot: Shot, x, y, w, h):
    """A region's real fill: the commonest colour INSIDE it.

    A box is seeded from the first row that matches, and on a rounded
    card that row is the corner — every pixel of it antialiased against
    the page. Reading the fill there returned #BCFF6D for a card the
    designer set to #B9FF66: a blend, off by a hair in every channel and
    wrong in a way that would propagate into the generated CSS.

    Antialiasing lives at edges. The middle of a fill is the fill, so
    inset past the border and take the mode.
    """
    ix = max(1, w // 6)
    iy = max(1, h // 6)
    c = Counter()
    for yy in range(y + iy, min(y + h - iy, shot.h), max(1, h // 40)):
        for xx in range(x + ix, min(x + w - ix, shot.w), max(1, w // 40)):
            c[shot.rgb(xx, yy)] += 1
    return c.most_common(1)[0][0] if c else None


def corner_radius(shot: Shot, x, y, w, h, rgb, cap=64):
    """Measure the corner, do not guess it.

    On a square corner the fill reaches the box edge on the first row.
    On a rounded one it is inset, and the inset shrinks to zero exactly
    at the radius — so the radius is the first row whose fill starts at
    the box edge.
    """
    limit = min(cap, w // 2, h // 2)
    if limit < 2:
        return 0
    for dy in range(limit):
        yy = y + dy
        if yy >= shot.h:
            break
        for dx in range(limit + 1):
            xx = x + dx
            if xx >= shot.w:
                break
            if _dist(shot.rgb(xx, yy), rgb) <= MERGE:
                if dx == 0:
                    return dy
                break
    return 0


# ─────────────────────────── text ────────────────────────────────────

def text_rows(shot: Shot, bg, band):
    """Rows of small, broken ink — the signature of type, not of fills.

    Reported with the measured height of the ink and its colour. Height
    is a MEASUREMENT; the font-size it implies is an inference and is
    labelled as one, because cap height varies by typeface and this
    module does not pretend otherwise.
    """
    px, w = shot.px, shot.w
    out = []
    run_start = None
    for y in range(band["top"], band["bottom"] + 2):
        segs, ink = 0, 0
        prev = False
        if y <= band["bottom"]:
            row = y * w * 4
            for x in range(w):
                i = row + x * 4
                on = (abs(px[i] - bg[0]) > INK or abs(px[i + 1] - bg[1]) > INK
                      or abs(px[i + 2] - bg[2]) > INK)
                if on:
                    ink += 1
                    if not prev:
                        segs += 1
                prev = on
        texty = segs >= 3 and 0 < ink < w * 0.7
        if texty and run_start is None:
            run_start = y
        elif not texty and run_start is not None:
            hgt = y - run_start
            if hgt >= 5:
                item = {"top": run_start, "height": hgt,
                        "color": _ink_color(shot, bg, run_start, y - 1)}
                # A LINE HAS A SIZE. A COLUMN OF LINES DOES NOT.
                # Rows only break where ink stops entirely, so a
                # subheading, a button and a quote stacked with tight
                # spacing came back as ONE 248px "row" — and dividing
                # that by cap height produced "344px", a number no page
                # has ever contained. Fabricating it would be the exact
                # failure this module exists to avoid, so a tall run is
                # reported as a block with NO size rather than a line
                # with a false one.
                if hgt <= MAX_LINE:
                    item["kind"] = "line"
                    item["font_size_estimate"] = round(hgt / 0.72)
                else:
                    item["kind"] = "block"
                    item["note"] = ("several lines with no blank row "
                                    "between them — no single font size "
                                    "applies, so none is reported")
                out.append(item)
            run_start = None
    return out


def _ink_color(shot: Shot, bg, y0, y1):
    """The commonest non-background colour in a row range = the type."""
    c = Counter()
    px, w = shot.px, shot.w
    for y in range(y0, min(y1 + 1, shot.h)):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            rgb = (px[i], px[i + 1], px[i + 2])
            if _dist(rgb, bg) > INK * 2:      # skip antialiased edges
                c[rgb] += 1
    if not c:
        return None
    return "#%02X%02X%02X" % c.most_common(1)[0][0]


# ─────────────────────────── the report ──────────────────────────────

def measure(path, deep=True) -> dict:
    """Everything measurable, with nothing inferred that could be read."""
    shot = load(path)
    bg = background(shot)
    bgrgb = tuple(bg["rgb"])
    step = 1 if shot.w * shot.h <= 1_600_000 else 2
    grads = gradients(shot)
    rep = {
        "image": str(path), "width": shot.w, "height": shot.h,
        "background": bg,
        "gradients": grads,
        "palette": palette(shot, step=step, skip=grads),
        "measured": ["background", "palette", "bands", "boxes", "radius"],
        "not_in_a_still": [
            "animation, easing and duration — one frame carries none",
            "hover, focus and other states",
            "other breakpoints",
            "anything scrolled out of shot",
        ],
    }
    if not deep:
        return rep
    bs = bands(shot, bgrgb)
    for b in bs[:40]:
        b["columns"] = columns(shot, bgrgb, b["top"], b["bottom"])
        b["text"] = text_rows(shot, bgrgb, b)
    rep["bands"] = bs
    # A slab carved out of a ramp is not a card. Dropped here rather
    # than inside boxes(), so the detector stays one simple idea.
    rep["boxes"] = [b for b in boxes(shot, bgrgb, step=step)
                    if not _in_any(grads, b["x"], b["y"], b["w"], b["h"])][:40]
    return rep


# ─────────────────────────── selftest ────────────────────────────────

TRUTH_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:800px;height:600px;background:#FFFFFF;position:relative;
 font-family:Helvetica,Arial,sans-serif}
#card{position:absolute;left:120px;top:80px;width:400px;height:240px;
 background:#B9FF66;border-radius:24px}
#pill{position:absolute;left:120px;top:380px;width:200px;height:60px;
 background:#191A23;border-radius:0}
#head{position:absolute;left:120px;top:480px;width:560px;height:60px;
 font-size:48px;line-height:60px;color:#2A5CE0}
</style></head><body>
<div id="card"></div><div id="pill"></div>
<div id="head">Measured not guessed</div>
</body></html>"""

# What the page IS. The instrument has to find these without being told.
TRUTH = {"bg": "#FFFFFF", "card": (120, 80, 400, 240, "#B9FF66", 24),
         "pill": (120, 380, 200, 60, "#191A23", 0), "text": "#2A5CE0"}


def _selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"   {detail}" if not cond and detail else ""))

    print("── the codec")
    tmp = Path(tempfile.mkdtemp(prefix="ae-vision-test-"))
    px = bytearray()
    for v in [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255),
              (18, 52, 86, 255), (200, 200, 200, 255), (0, 0, 0, 255)]:
        px += bytes(v)
    G.write_png(tmp / "rt.png", 3, 2, px)
    s = load(tmp / "rt.png")
    check("a known image reads back exactly",
          (s.w, s.h) == (3, 2) and s.hex(0, 0) == "#FF0000"
          and s.hex(0, 1) == "#123456", s.hex(0, 1))

    print("\n── colour merging")
    check("antialiasing folds into the colour it came from",
          _dist((185, 255, 102), (183, 252, 100)) <= MERGE)
    check("two real design colours stay apart",
          _dist((185, 255, 102), (25, 26, 35)) > MERGE)

    print("\n── AGAINST GROUND TRUTH (a page whose values we set)")
    if not G.find_browser():
        print("  SKIP — no headless browser. UNVERIFIED, not proven good.")
        print("\nvision selftest: SKIPPED (measurement unproven)")
        return 0
    d = tmp / "truth"
    d.mkdir()
    (d / "index.html").write_text(TRUTH_HTML)
    shot_png = d / "truth.png"
    if not G.shoot(d / "index.html", 800, 600, shot_png):
        print("  SKIP — the browser produced no screenshot.")
        return 0

    rep = measure(shot_png)
    check("the page colour is read exactly",
          rep["background"]["hex"] == TRUTH["bg"],
          rep["background"]["hex"])
    hexes = [p["hex"] for p in rep["palette"]]
    for want in ("#B9FF66", "#191A23"):
        check(f"{want} is in the palette", want in hexes, str(hexes[:6]))

    def find(x, y, w, h):
        for b in rep["boxes"]:
            if (abs(b["x"] - x) <= 3 and abs(b["y"] - y) <= 3
                    and abs(b["w"] - w) <= 4 and abs(b["h"] - h) <= 4):
                return b
        return None

    cx, cy, cw, ch, cfill, crad = TRUTH["card"]
    card = find(cx, cy, cw, ch)
    check("the rounded card is found at its real position", card is not None,
          f"boxes={[(b['x'], b['y'], b['w'], b['h']) for b in rep['boxes'][:5]]}")
    if card:
        check("  ...with its exact fill", card["fill"] == cfill, card["fill"])
        check("  ...and its corner radius measured, not guessed",
              abs(card["radius"] - crad) <= 4, f"got {card['radius']} want {crad}")

    px_, py, pw, ph, pfill, prad = TRUTH["pill"]
    pill = find(px_, py, pw, ph)
    check("a square-cornered box is found too", pill is not None)
    if pill:
        check("  ...and reports radius 0, not a phantom curve",
              pill["radius"] <= 2, f"got {pill['radius']}")

    texts = [t for b in rep.get("bands", []) for t in b.get("text", [])]
    check("the text row is located", bool(texts), str(texts[:2]))
    if texts:
        big = max(texts, key=lambda t: t["height"])
        check("  ...its colour read from the pixels",
              big["color"] == TRUTH["text"], str(big["color"]))
        check("  ...its size estimated within 20% of 48px",
              abs(big["font_size_estimate"] - 48) <= 10,
              f"got {big['font_size_estimate']}")

    print("\n── honesty")
    check("the report says what a still cannot contain",
          any("animation" in s for s in rep["not_in_a_still"]))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nvision selftest:", "all green" if ok else "FAILED")
    return 0 if ok else 1


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: aethron_vision.py <image.png|jpg> [--json]")
        print("       aethron_vision.py --selftest")
        return 0
    if argv[0] == "--selftest":
        return _selftest()
    rep = measure(argv[0])
    if "--json" in argv:
        print(json.dumps(rep, indent=1))
        return 0
    print(f"{rep['width']}x{rep['height']}  background "
          f"{rep['background']['hex']}")
    print("\npalette (measured, most-used first)")
    for p in rep["palette"][:10]:
        print(f"   {p['hex']}   {p['share'] * 100:5.1f}%")
    if rep.get("gradients"):
        print(f"\n{len(rep['gradients'])} gradient(s)")
        for g in rep["gradients"]:
            b = g["box"]
            print(f"   y {b['y']}-{b['y'] + b['h'] - 1}  {g['from']} -> "
                  f"{g['to']}  [{g['axis']}]")
            if g.get("css"):
                print(f"      {g['css']}")
            if g.get("note"):
                print(f"      NOT FITTED: {g['note']}")
    print(f"\n{len(rep.get('bands', []))} band(s) of content")
    for b in rep.get("bands", [])[:8]:
        print(f"   y {b['top']:>4}-{b['bottom']:<4} h{b['height']:<4} "
              f"gap above {b['gap_above']:<4} "
              f"{len(b.get('columns', []))} column(s), "
              f"{len(b.get('text', []))} text row(s)")
    print(f"\n{len(rep.get('boxes', []))} solid region(s)")
    for r in rep.get("boxes", [])[:8]:
        print(f"   {r['fill']}  {r['w']:>4}x{r['h']:<4} at "
              f"({r['x']},{r['y']})  radius {r['radius']}")
    print("\nnot in a still image:")
    for s in rep["not_in_a_still"]:
        print("   -", s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
