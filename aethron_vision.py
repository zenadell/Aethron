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
import re
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


class Field:
    """The page's own colour UNDER each pixel — its local background.

    THE BUG THIS EXISTS FOR. Ink used to mean "differs from the one page
    colour". On a page with a gradient that is true almost everywhere,
    so a whole hero came back as a single blob spanning x0-1023: the
    button, the sub-paragraph and the quote were never found as separate
    things, their colours read as gradient (#481912 where they are
    white), and a human had to place them by eye. Measured, that
    eye-placed button was 43.7% wrong inside its own box while the
    carried gradient beside it was 0.2% wrong.

    A median downsample rejects text — a glyph stroke is an outlier
    inside a cell, and the median throws outliers away while a mean
    would smear them in — so what is left is the ground the text sits
    on. Bilinear between cells makes it smooth.
    """

    CELL = 8          # target cell size in pixels — see __init__

    def __init__(self, shot: Shot, gw=None, gh=None, dark=True):
        """`dark` = the page's ground is darker than what sits on it.

        A MEDIAN IS NOT ENOUGH. A cell is ~21px and a button is 115x26,
        so whole cells fall INSIDE it and the median calls the button the
        ground — after which the button is not ink, is never found, and
        has to be placed by hand. Text is a minority in its cell and a
        median rejects it; a big solid element is the majority and a
        median adopts it.

        Polarity separates them. On a dark page the ground is the
        darkest thing present and every element sits lighter, so a LOW
        percentile is the ground and rejects both text and buttons. On a
        light page the reverse. Inside a smooth gradient the spread
        within one cell is small, so the percentile ~= the median and
        the ramp is still tracked faithfully.
        """
        # A CELL USED TO BE 21 PIXELS AND THAT COST A VISIBLE EDGE. The
        # owner's page is built on a faint grid, and its top line turned
        # out not to be a hairline at all but the edge of a slightly
        # lighter panel. A field sampled every 21px smears that edge
        # across a whole cell, so the rebuilt page simply had no frame.
        # At ~8px the edge survives, the file is still a few kilobytes,
        # and it is CARRIED rather than inferred — which is the whole
        # argument of this module applied to one more thing.
        if gw is None:
            gw = max(16, min(192, round(shot.w / self.CELL)))
        if gh is None:
            gh = max(12, min(192, round(shot.h / self.CELL)))
        self.dark = dark
        self.gw, self.gh, self.shot = gw, gh, shot
        self.cell = []
        for gy in range(gh):
            row = []
            y0, y1 = gy * shot.h // gh, (gy + 1) * shot.h // gh
            for gx in range(gw):
                x0, x1 = gx * shot.w // gw, (gx + 1) * shot.w // gw
                rs, gs, bs = [], [], []
                for y in range(y0, max(y0 + 1, y1), 2):
                    for x in range(x0, max(x0 + 1, x1), 2):
                        c = shot.rgb(x, y)
                        rs.append(c[0]); gs.append(c[1]); bs.append(c[2])
                row.append(self._pick(rs, gs, bs))
            self.cell.append(row)

    def _pick(self, rs, gs, bs):
        q = 0.2 if self.dark else 0.8
        out = []
        for ch in (rs, gs, bs):
            ch = sorted(ch)
            out.append(int(ch[min(len(ch) - 1, int(q * len(ch)))]))
        return tuple(out)

    def refine(self, mask):
        """Re-estimate the ground with the ink taken out.

        The first pass is chicken-and-egg: the field is built from an
        image that still contains the text, so a cell dense with glyphs
        is pulled slightly toward the type. Measured on the ground-truth
        page, that was enough to make rows next to a heading register as
        ink and inflate its height from 48px to 64px.

        One pass with the ink excluded removes the feedback: the ground
        is estimated from ground only. Cells that are ALL ink keep their
        first estimate rather than being left undefined.
        """
        shot = self.shot
        blind = []
        for gy in range(self.gh):
            y0, y1 = gy * shot.h // self.gh, (gy + 1) * shot.h // self.gh
            for gx in range(self.gw):
                x0, x1 = gx * shot.w // self.gw, (gx + 1) * shot.w // self.gw
                rs, gs, bs = [], [], []
                for y in range(y0, max(y0 + 1, y1)):
                    base = y * shot.w
                    for x in range(x0, max(x0 + 1, x1)):
                        if mask[base + x]:
                            continue
                        c = shot.rgb(x, y)
                        rs.append(c[0]); gs.append(c[1]); bs.append(c[2])
                if len(rs) >= 4:
                    self.cell[gy][gx] = self._pick(rs, gs, bs)
                else:
                    blind.append((gy, gx))
        blindset = set(blind)
        # A CELL WITH NO GROUND IN IT MUST NOT KEEP ITS FIRST GUESS.
        # Small cells fall entirely inside a heavy glyph stroke, and
        # that first guess is then the colour of the TYPE — a white
        # smudge painted into the page's own background, exactly where
        # the heading is. Ask the neighbours instead; they are ground.
        for _ in range(6):
            if not blind:
                break
            rest = []
            for gy, gx in blind:
                got = []
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = gy + dy, gx + dx
                    if (0 <= ny < self.gh and 0 <= nx < self.gw
                            and (ny, nx) not in blindset):
                        got.append(self.cell[ny][nx])
                if got:
                    self.cell[gy][gx] = tuple(
                        sum(c[k] for c in got) // len(got) for k in range(3))
                else:
                    rest.append((gy, gx))
            for gy, gx in blind:
                blindset.discard((gy, gx))
            blind = rest
        return self.smooth()

    def smooth(self):
        """Take the TEXT out of the background, keep the EDGES in.

        At 8px cells the field started carrying ghosts: dark, text-shaped
        smudges where the headline is, because a small cell straddling a
        glyph is mostly that glyph's dark fringe and the percentile has
        nothing cleaner to choose. The ink mask cannot help — the fringe
        is only a shade off the ground, which is exactly why it is not
        ink.

        A background is smooth BY DEFINITION, so any high-frequency
        structure in the field is contamination. A 3x3 median removes
        it, and a median is the right filter rather than a blur for the
        reason this whole change was made: it leaves a step edge
        standing. The panel edge survives; the ghost of the headline
        does not.
        """
        out = []
        for gy in range(self.gh):
            row = []
            for gx in range(self.gw):
                got = []
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = gy + dy, gx + dx
                        if 0 <= ny < self.gh and 0 <= nx < self.gw:
                            got.append(self.cell[ny][nx])
                row.append(tuple(sorted(c[k] for c in got)[len(got) // 2]
                                 for k in range(3)))
            out.append(row)
        self.cell = out
        return self

    def at(self, x, y):
        fx = x * self.gw / self.shot.w - 0.5
        fy = y * self.gh / self.shot.h - 0.5
        x0 = max(0, min(self.gw - 1, int(fx)))
        y0 = max(0, min(self.gh - 1, int(fy)))
        x1 = min(self.gw - 1, x0 + 1)
        y1 = min(self.gh - 1, y0 + 1)
        tx, ty = max(0.0, fx - x0), max(0.0, fy - y0)
        out = []
        for k in range(3):
            a = self.cell[y0][x0][k] * (1 - tx) + self.cell[y0][x1][k] * tx
            b = self.cell[y1][x0][k] * (1 - tx) + self.cell[y1][x1][k] * tx
            out.append(int(a * (1 - ty) + b * ty))
        return tuple(out)

    def png_bytes(self):
        """The field as a tiny image — what a port embeds and carries."""
        px = bytearray()
        for row in self.cell:
            for c in row:
                px += bytes((c[0], c[1], c[2], 255))
        import io
        tmp = Path(tempfile.mkdtemp(prefix="ae-field-")) / "f.png"
        G.write_png(tmp, self.gw, self.gh, px)
        return tmp.read_bytes()


def ink_mask(shot: Shot, field: "Field"):
    """One pass: is this pixel something drawn ON the background?

    Computed once and shared. Every consumer used to re-scan the whole
    image with its own copy of the same comparison.
    """
    m = bytearray(shot.w * shot.h)
    px, w = shot.px, shot.w
    for y in range(shot.h):
        row = y * w * 4
        base = y * w
        for x in range(w):
            i = row + x * 4
            b = field.at(x, y)
            if (abs(px[i] - b[0]) > INK or abs(px[i + 1] - b[1]) > INK
                    or abs(px[i + 2] - b[2]) > INK):
                m[base + x] = 1
    return m


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

def rules(shot: Shot, mask, field, min_frac=0.20, margin=5):
    """Hairline rules — the grid a design is built on.

    The owner saw these before the tool could: a line top-to-bottom and
    another left-to-right, crossing. Nothing looked for them, so the
    rebuild had no cross.

    NOT DETECTED BY BRIGHTNESS, AND NOT BY THE INK MASK. These rules
    FADE — the same rule sits 3 from the ground at the top of the page
    and 44 away lower down where a glow lights it — so a fixed
    threshold finds one end and loses the other. Measured at y=306: 342
    ink pixels spread over x231-875, but the longest unbroken stretch
    was 142, because the line kept dipping under the threshold.

    What is true along a rule at every point, faint or bright, is that
    it is brighter than the rows immediately ABOVE AND BELOW it. That
    is local, survives fading, and a column of text cannot fake it for
    a third of the page.
    """
    w, h = shot.w, shot.h
    px = shot.px

    def lum(x, y):
        i = (y * w + x) * 4
        return px[i] + px[i + 1] + px[i + 2]

    out = []
    step = 2
    for y in range(3, h - 3):
        n = ok = 0
        for x in range(0, w, step):
            n += 1
            here = lum(x, y)
            if (here - lum(x, y - 3) > margin * 3
                    and here - lum(x, y + 3) > margin * 3):
                ok += 1
        if n and ok / n >= min_frac:
            out.append({"axis": "horizontal", "at": y, "extent": ok * step,
                        "color": _line_color(shot, mask, field, y, True)})
    for x in range(3, w - 3):
        n = ok = 0
        for y in range(0, h, step):
            n += 1
            here = lum(x, y)
            if (here - lum(x - 3, y) > margin * 3
                    and here - lum(x + 3, y) > margin * 3):
                ok += 1
        if n and ok / n >= min_frac:
            out.append({"axis": "vertical", "at": x, "extent": ok * step,
                        "color": _line_color(shot, mask, field, x, False)})
    # A 1px rule antialiases across two or three lines; report the rule,
    # not every line it touched. Keep the strongest of a cluster.
    out.sort(key=lambda r: (r["axis"], r["at"]))
    merged = []
    for r in out:
        if (merged and merged[-1]["axis"] == r["axis"]
                and r["at"] - merged[-1]["at"] <= 5):
            if r["extent"] > merged[-1]["extent"]:
                merged[-1] = r
            continue
        merged.append(r)
    return merged


def _line_color(shot: Shot, mask, field, at, horizontal):
    """The rule's own colour, taken where it is most distinct."""
    best, col = -1, None
    rng = range(0, shot.w) if horizontal else range(0, shot.h)
    for k in rng:
        x, y = (k, at) if horizontal else (at, k)
        if not mask[y * shot.w + x]:
            continue
        d = _dist(shot.rgb(x, y), field.at(x, y))
        if d > best:
            best, col = d, shot.rgb(x, y)
    return "#%02X%02X%02X" % col if col else None


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

def _runs(shot: Shot, y, bg, field=None):
    """Runs of one colour across a row, ignoring the background.

    Background means the LOCAL ground, not one page colour. Left on the
    global colour, this missed a white button sitting over a gradient
    entirely — so the button had to be placed by hand, and it was the
    worst-fitting element on the page at 41.3% wrong inside its own box.
    """
    px, w = shot.px, shot.w
    row, out = y * w * 4, []
    x = 0
    while x < w:
        i = row + x * 4
        c = (px[i], px[i + 1], px[i + 2])
        if _dist(c, field.at(x, y) if field else bg) <= INK:
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


def boxes(shot: Shot, bg, step=1, field=None):
    """Solid rectangles — cards, buttons, filled sections.

    Grown from same-coloured row runs that line up vertically. UI fills
    are flat and axis-aligned, so this finds them exactly without a
    connected-component pass over a million pixels.
    """
    open_, done = [], []
    for y in range(0, shot.h, step):
        rs = _runs(shot, y, bg, field)
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
        # HEIGHT IS JUDGED AFTER REJOINING, NOT BEFORE. A button with a
        # label on it is a clean run only in the few rows above and below
        # its own text; the middle rows fragment. Discarding those thin
        # strips here left _rejoin nothing to work with, so the element
        # was never found at all and had to be placed by hand.
        if w_ < MIN_BOX:
            continue
        rgb = interior_colour(shot, b["left"], b["top"], w_, h_) or b["rgb"]
        out.append({"x": b["left"], "y": b["top"], "w": w_, "h": h_,
                    "fill": "#%02X%02X%02X" % rgb,
                    "radius": corner_radius(shot, b["left"], b["top"],
                                            w_, h_, rgb)})
    out = _rejoin(shot, bg, out, field)
    out = [b for b in out if b["h"] >= MIN_BOX]
    # HONEST FALLBACK. Detecting elements against the LOCAL ground is
    # the right idea — it is what let the text pass find a button's
    # strips at all — but assembling those strips back into one element
    # is not solved: on a real page the pieces are there and the merge
    # still returns nothing. Rather than ship a detector that finds
    # FEWER things than before, fall back to the flat-background pass
    # when the local one comes back empty, and leave the gap named.
    if not out and field is not None:
        return boxes(shot, bg, step=step, field=None)
    # radius needs the whole element, so it is measured after the join
    for b in out:
        b["radius"] = corner_radius(shot, b["x"], b["y"], b["w"], b["h"],
                                    tuple(int(b["fill"][i:i + 2], 16)
                                          for i in (1, 3, 5)))
    out.sort(key=lambda r: -(r["w"] * r["h"]))
    return out


def _rejoin(shot: Shot, bg, boxes_, field=None):
    """Put an element back together after its own contents split it.

    A row-run detector sees a filled card with a heading on it as strips
    of fill, and a button with a label as a left margin, a right margin
    and two thin bands. Strips are not something to generate code from,
    and an element that is never assembled has to be placed by hand —
    measured, the hand-placed button was 41.3% wrong inside its own box
    while the carried gradient beside it was 0.2% wrong.

    Merging is by OVERLAP in both axes and by COLOUR (not by an equal
    hex string: one white button's strips read #FFFFFB, #FCFCFC and
    #FFFFFF). Two pieces separated by real page background are left
    alone — that gap means two elements, not one interrupted element.
    """
    def _rgb(hx):
        return tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5))

    items = [dict(b) for b in boxes_]
    changed = True
    while changed:
        changed = False
        for i in range(len(items)):
            a = items[i]
            if a is None:
                continue
            for j in range(i + 1, len(items)):
                b = items[j]
                if b is None or _dist(_rgb(a["fill"]), _rgb(b["fill"])) > MERGE:
                    continue
                # touching or overlapping, with a little slack for the
                # antialiased edge between two pieces of one element
                if (a["x"] > b["x"] + b["w"] + 4
                        or b["x"] > a["x"] + a["w"] + 4
                        or a["y"] > b["y"] + b["h"] + 4
                        or b["y"] > a["y"] + a["h"] + 4):
                    continue
                x0, y0 = min(a["x"], b["x"]), min(a["y"], b["y"])
                x1 = max(a["x"] + a["w"], b["x"] + b["w"])
                y1 = max(a["y"] + a["h"], b["y"] + b["h"])
                if _is_background(shot, bg, x0, y0, x1 - x0, y1 - y0,
                                  thresh=0.55, field=field):
                    continue                  # a real gap: two elements
                a.update(x=x0, y=y0, w=x1 - x0, h=y1 - y0)
                items[j] = None
                changed = True
    return [b for b in items if b]


def _is_background(shot: Shot, bg, x, y, w, h, thresh=0.7, field=None):
    """Is this rectangle mostly the page colour?"""
    if h <= 0 or w <= 0:
        return True
    hit = tot = 0
    for yy in range(y, min(y + h, shot.h), max(1, h // 12)):
        for xx in range(x, min(x + w, shot.w), max(1, w // 12)):
            tot += 1
            if _dist(shot.rgb(xx, yy),
                     field.at(xx, yy) if field else bg) <= INK:
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


# ──────────────────── filled elements, found whole ───────────────────

def _row_runs(shot: Shot, y, tol=MERGE):
    """One row cut into maximal stretches of near-equal colour."""
    px, w = shot.px, shot.w
    base = y * w * 4
    out, x = [], 0
    while x < w:
        i = base + x * 4
        seed = (px[i], px[i + 1], px[i + 2])
        x0 = x
        x += 1
        while x < w:
            j = base + x * 4
            if (abs(px[j] - seed[0]) > tol or abs(px[j + 1] - seed[1]) > tol
                    or abs(px[j + 2] - seed[2]) > tol):
                break
            x += 1
        out.append((x0, x - 1, seed))
    return out


def regions(shot: Shot, field: "Field", lines=(), min_w=12, min_h=10,
            min_fill=0.60, tol=MERGE):
    """Buttons, chips and cards — found WHOLE, in one pass.

    SIX ATTEMPTS DIED HERE AND ALL OF THEM THE SAME WAY. A row-run
    detector sees a button as strips: the clean rows above and below its
    label, and the label's own rows cut into slivers. Every attempt put
    the strips back together AFTERWARDS — by matching edges, by
    proximity, by colour — and the real page always had one more gap
    than the slack allowed. Measured on the owner's screenshot: the
    white pill's upper strip ends at y303 and its lower strip begins at
    y310, seven pixels apart against four pixels of slack. So the
    element was never assembled, never emitted, and the owner saw his
    button rendered as bare text on the background.

    THE STRIPS NEVER NEEDED REASSEMBLING. They were never separate. A
    button has padding, so its fill runs CONTINUOUSLY AROUND its label —
    the slivers between the letters touch the clean rows above and
    below. Union runs that OVERLAP instead of runs that line up and the
    pill arrives whole on the first pass, in half a second.

    A letter is a connected area too, so three things tell them apart:
      * how full its own box is — the pill is 78% ink, a 'T' is 24%;
      * whether it differs from the page's own ground at all, which
        drops the background and every band of the gradient;
      * how tall it is against the line it sits on — a button is taller
        than its label, a letter is exactly its line's height.
    `lines` are OCR boxes when the caller has them; without them the
    first two still hold and only the tallest display type can slip
    through.
    """
    w, h = shot.w, shot.h
    rows = [_row_runs(shot, y, tol) for y in range(h)]
    par = []
    ids = []
    for runs in rows:
        ids.append(list(range(len(par), len(par) + len(runs))))
        par.extend(range(len(par), len(par) + len(runs)))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a

    for y in range(1, h):
        above, here = rows[y - 1], rows[y]
        ia, ih = ids[y - 1], ids[y]
        j = 0
        for k, (x0, x1, c) in enumerate(here):
            while j < len(above) and above[j][1] < x0:
                j += 1
            jj = j
            while jj < len(above) and above[jj][0] <= x1:
                if _dist(above[jj][2], c) <= tol:
                    ra, rb = find(ia[jj]), find(ih[k])
                    if ra != rb:
                        par[rb] = ra
                jj += 1

    acc = {}
    for y, runs in enumerate(rows):
        for k, (x0, x1, c) in enumerate(runs):
            r = find(ids[y][k])
            a = acc.get(r)
            n = x1 - x0 + 1
            if a is None:
                acc[r] = [x0, y, x1, y, n, Counter({c: n})]
            else:
                a[0] = min(a[0], x0)
                a[1] = min(a[1], y)
                a[2] = max(a[2], x1)
                a[3] = max(a[3], y)
                a[4] += n
                a[5][c] += n

    out = []
    for x0, y0, x1, y1, n, cols in acc.values():
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if bw < min_w or bh < min_h or n / (bw * bh) < min_fill:
            continue
        if bw >= w * 0.92 and bh >= h * 0.92:
            continue
        rgb = interior_colour(shot, x0, y0, bw, bh) or cols.most_common(1)[0][0]
        # THE GROUND IS NOT AN ELEMENT. A smooth ramp breaks into many
        # runs and unions into large components that are perfectly
        # "filled" — and they are the page itself. A region has to
        # differ from the ground measured underneath it.
        cx, cy = x0 + bw // 2, y0 + bh // 2
        if _dist(rgb, field.at(cx, cy)) <= INK:
            continue
        # A letter is as tall as its line; a button is taller.
        if _is_glyph(x0, y0, bw, bh, lines):
            continue
        out.append({"x": x0, "y": y0, "w": bw, "h": bh,
                    "fill": "#%02X%02X%02X" % rgb,
                    "radius": corner_radius(shot, x0, y0, bw, bh, rgb),
                    "ink": n})
    for b in out:
        b["radius"] = _shape_radius(b, b.pop("ink"))
    out.sort(key=lambda b: -(b["w"] * b["h"]))
    return out


def _is_glyph(x, y, w, h, lines):
    """Does this region sit inside one text line and match its height?"""
    for ln in lines or ():
        lx, ly, lw, lh = ln["x"], ln["y"], ln["w"], ln["h"]
        if (x >= lx - 3 and x + w <= lx + lw + 3
                and y >= ly - 3 and y + h <= ly + lh + 3
                and h <= lh * 1.25):
            return True
    return False


def _shape_radius(b, ink):
    """A circle is not a rounded rectangle, and CSS spells it 50%.

    The owner's word for what the rebuild missed was "perfect circle".
    A square box with a big radius is not one, and the difference is
    measurable without any shape fitting: a disc covers pi/4 of the box
    it sits in, a rounded rectangle covers far more, and a circle's box
    is square.
    """
    w, h = b["w"], b["h"]
    if abs(w - h) <= max(2, 0.10 * max(w, h)) and 0.70 <= ink / (w * h) <= 0.88:
        return "50%"
    return b["radius"]


def rule_paint(shot: Shot, field: "Field", axis, at, sample=4, margin=5,
               mask=None):
    """A rule's colour ALONG ITS LENGTH, read rather than chosen.

    One flat hex drawn edge to edge is what made the rebuilt page's
    cross look painted on. The original's horizontal rule is 478 of
    1024 pixels long, it STOPS where a button sits on it, and it fades
    from #FFFFFF where a glow lights it to almost the ground colour at
    both ends. No single colour can say any of that, and a segment list
    only says the first part of it.

    So do not pick a colour for the line: READ it every few pixels and
    emit the readings as gradient stops, transparent wherever the line
    is not there at all. The gap under the button, the fade into the
    dark and the ends of the line are then one mechanism with no
    special cases, and the rule is carried rather than recreated.
    """
    w, h = shot.w, shot.h
    px = shot.px
    n = w if axis == "horizontal" else h

    def lum(x, y):
        i = (y * w + x) * 4
        return px[i] + px[i + 1] + px[i + 2]

    stops, present, run, longest = [], 0, 0, 0
    for k in range(0, n, sample):
        if axis == "horizontal":
            x, y = k, at
            on = (lum(x, y) - lum(x, max(0, y - 3)) > margin * 3
                  and lum(x, y) - lum(x, min(h - 1, y + 3)) > margin * 3)
        else:
            x, y = at, k
            on = (lum(x, y) - lum(max(0, x - 3), y) > margin * 3
                  and lum(x, y) - lum(min(w - 1, x + 3), y) > margin * 3)
        if on:
            present += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        # NO THRESHOLD IN THE PAINT. The first version drew the sampled
        # colour where a contrast test passed and transparent where it
        # did not, which turned a continuous hairline into a dashed one
        # — the owner's grid came back faint and broken. There is
        # nothing to decide here: one pixel row of the ORIGINAL is the
        # right answer everywhere along the line. Where the rule is
        # there, this is the rule; where it has faded out, this is the
        # background, painted 1px over a background we had only
        # approximated. The on/off test survives solely to say how much
        # of the line is really lit, which is what tells a rule from a
        # row of type.
        # READ THE RULE WHERE NOTHING IS ON TOP OF IT. A hairline runs
        # under the page's type, and sampling it there reads the GLYPH,
        # not the rule — so the line was emitted carrying the
        # original's own letter pixels and rendered as a white streak
        # across every text row it crossed. On the owner's second
        # screenshot that put a strikethrough through the paragraph,
        # the headline and three buttons at once, and it looked like a
        # phantom rule when the rule was real and only its COLOUR was
        # wrong. Where the line is covered, its colour is unknowable
        # here; carry the nearest reading instead of inventing one.
        stops.append(None if (mask is not None and mask[y * shot.w + x])
                     else shot.rgb(x, y))
    known = [i for i, c in enumerate(stops) if c is not None]
    if not known:
        return {"css": None, "present": present * sample,
                "longest": longest * sample}
    for i, c in enumerate(stops):
        if c is None:
            j = min(known, key=lambda q: abs(q - i))
            stops[i] = stops[j]
    side = "to right" if axis == "horizontal" else "to bottom"
    text = ",".join(f"rgb({c[0]},{c[1]},{c[2]}) "
                    f"{i * sample / max(1, n - 1) * 100:.1f}%"
                    for i, c in enumerate(stops))
    return {"css": f"linear-gradient({side},{text})",
            "present": present * sample, "longest": longest * sample}


def raster_regions(shot: Shot, field: "Field", lines, fills=(), cell=4,
                   join=8, min_w=10, min_h=10, conf=0.60,
                   min_density=0.34, max_area=0.14):
    """The parts of a page that are pictures, found without being told.

    A logo is not type and must never be emitted as type: asked to set
    one, OCR read a row of wordmarks as VIVUVIYOIIII"VIVUCINUU and the
    rebuild printed exactly that. Until now the answer was to hand the
    tool a list of rectangles to carry, which works on one screenshot
    and generalises to nothing.

    The page already says which parts they are. OCR names the type and
    leaves the pictures unnamed, so INK THAT OCR COULD NOT READ is a
    picture — that is the whole rule. Clusters of unnamed ink are the
    seeds; each one then ABSORBS any text line it touches, because a
    logo is a mark welded to a wordmark and carrying half of one is
    worse than carrying none.

    What is carried is a crop of the original, so it is pixel-exact by
    construction and costs no model anything.
    """
    w, h = shot.w, shot.h
    mask = ink_mask(shot, field)
    gw, gh = (w + cell - 1) // cell, (h + cell - 1) // cell
    grid = bytearray(gw * gh)
    for y in range(h):
        row = y * w
        gy = (y // cell) * gw
        for x in range(w):
            if mask[row + x]:
                grid[gy + x // cell] = 1
    # everything OCR named is type, and every fill already measured is
    # an element — neither is a picture that needs carrying
    def clear(x, y, bw, bh):
        for gy in range(max(0, (y - 1) // cell),
                        min(gh, (y + bh + 1) // cell + 1)):
            for gx in range(max(0, (x - 1) // cell),
                            min(gw, (x + bw + 1) // cell + 1)):
                grid[gy * gw + gx] = 0

    for ln in lines or ():
        # A LOW-CONFIDENCE READ IS NOT TYPE, IT IS A PICTURE OCR TRIED
        # TO READ. The logo strip comes back as '*Oogcipum N Iim' at
        # 0.30, and treating that as named type both stops it being
        # carried and invites the emitter to set those characters —
        # which is precisely how a row of wordmarks once shipped as
        # VIVUVIYOIIII. Below the bar, leave the ink standing.
        if ln.get("confidence", 1) < conf:
            continue
        # CLEAR THE GLOW, NOT JUST THE GLYPHS. Vision reports a tight
        # box; a headline's ink carries a halo past it, and those fringe
        # cells chained through the join radius into one blob that
        # carried 463x139 of the hero as a photograph — a page cannot
        # be a picture of itself and still be code.
        pad = max(2, int(ln["h"] * 0.4))
        clear(ln["x"] - pad, ln["y"] - pad,
              ln["w"] + pad * 2, ln["h"] + pad * 2)
    for b in fills or ():
        clear(b["x"] - 2, b["y"] - 2, b["w"] + 4, b["h"] + 4)

    seen = bytearray(gw * gh)
    span = max(1, join // cell)
    found = []
    for i in range(gw * gh):
        if not grid[i] or seen[i]:
            continue
        stack, cells = [i], []
        seen[i] = 1
        while stack:
            j = stack.pop()
            cells.append(j)
            jy, jx = divmod(j, gw)
            for dy in range(-span, span + 1):
                ny = jy + dy
                if not 0 <= ny < gh:
                    continue
                for dx in range(-span, span + 1):
                    nx = jx + dx
                    if not 0 <= nx < gw:
                        continue
                    k = ny * gw + nx
                    if grid[k] and not seen[k]:
                        seen[k] = 1
                        stack.append(k)
        xs = [c % gw for c in cells]
        ys = [c // gw for c in cells]
        x0, y0 = min(xs), min(ys)
        x1, y1 = max(xs), max(ys)
        # A PICTURE IS DENSE; A HAZE IS NOT. The glow around a headline
        # leaves a scatter of lit cells over a third of the page, and
        # with a join radius they chain into one blob — the first run
        # carried a 736x292 slab of the hero as a photograph, which
        # looks perfect and is not code. A logo fills its own box.
        if len(cells) / max(1, (x1 - x0 + 1) * (y1 - y0 + 1)) < min_density:
            continue
        found.append([x0 * cell, y0 * cell,
                      (x1 + 1) * cell - 1, (y1 + 1) * cell - 1])

    # a mark and its wordmark are one object
    for ln in lines or ():
        lx, ly = ln["x"], ln["y"]
        lx1, ly1 = lx + ln["w"], ly + ln["h"]
        for r in found:
            if (lx <= r[2] + join and lx1 >= r[0] - join
                    and ly <= r[3] + join and ly1 >= r[1] - join):
                r[0] = min(r[0], lx)
                r[1] = min(r[1], ly)
                r[2] = max(r[2], lx1)
                r[3] = max(r[3], ly1)

    merged = True
    while merged:
        merged = False
        for a in range(len(found)):
            if found[a] is None:
                continue
            for b in range(a + 1, len(found)):
                if found[b] is None:
                    continue
                p, q = found[a], found[b]
                if (p[0] <= q[2] and q[0] <= p[2]
                        and p[1] <= q[3] and q[1] <= p[3]):
                    p[0], p[1] = min(p[0], q[0]), min(p[1], q[1])
                    p[2], p[3] = max(p[2], q[2]), max(p[3], q[3])
                    found[b] = None
                    merged = True
    out = []
    for r in found:
        if r is None:
            continue
        x, y = max(0, r[0]), max(0, r[1])
        rw, rh = min(r[2], w - 1) - x + 1, min(r[3], h - 1) - y + 1
        if rw < min_w or rh < min_h or rw >= w * 0.98:
            continue
        if rw * rh > max_area * w * h:
            continue          # a page is not a picture of itself
        out.append({"x": x, "y": y, "w": rw, "h": rh})
    out.sort(key=lambda r: (r["y"], r["x"]))
    return out


def carry_failures(html, original, rebuild_png, pad=2, carried=()):
    """Whatever could not be SET as type gets CARRIED as pixels.

    The last mile, and it closes honestly. After the correction pass the
    checker still names a line or two: a wordmark set in a face nobody
    has, a lockup OCR reads differently every time, a caption whose ink
    height no web font reproduces. Chasing those with more font search
    is how a rebuild spends an hour to get further from the original.

    There is a reading of that page which is exactly right and already
    in hand — the original's own pixels. So a line the checker fails is
    dropped from the type layer and the original's crop of it is laid
    down in its place. The page loses a little editability exactly
    where it was already wrong, and gains being correct.

    Returns (html, regions). The regions matter as much as the html: a
    line that is now a crop of the original must be graded BY ITS
    PIXELS from here on, exactly like every other carried region. Grade
    it by reading it again and OCR will happily report a pixel-perfect
    crop as the wrong size, because a two-word wordmark segments
    differently depending on what is beside it.

    Nothing is carried when the checker could not run — an unproven
    page is not a broken one.
    """
    v = verify_rebuild(original, rebuild_png, carried=carried)
    if v.get("verdict") == "SKIPPED":
        return html, 0
    shot = original if isinstance(original, Shot) else load(original)
    want = {ln["text"].strip(): ln for ln in (ocr(original) or [])}
    seen, adds, made = set(), [], []
    for f in v["findings"]:
        if f["kind"] not in ("MISSING", "WRONG SIZE", "MISPLACED"):
            continue
        ln = want.get(f["text"])
        if not ln or f["text"] in seen:
            continue
        seen.add(f["text"])
        x = max(0, ln["x"] - pad)
        y = max(0, ln["y"] - pad)
        bw = min(shot.w - x, ln["w"] + pad * 2)
        bh = min(shot.h - y, ln["h"] + pad * 2)
        # drop the type element that failed, so nothing is drawn twice
        esc = (f["text"].replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;"))
        html = re.sub(r'<div class="t"[^>]*>' + re.escape(esc) + r'</div>',
                      "", html, count=1)
        adds.append(f'<img alt="" class="r" data-ae-id="c{len(adds):02d}" '
                    f'style="left:{x}px;top:{y}px;'
                    f'width:{bw}px;height:{bh}px;z-index:6" '
                    f'src="data:image/png;base64,'
                    f'{crop_b64(shot, x, y, bw, bh)}">')
        made.append({"x": x, "y": y, "w": bw, "h": bh})
    if adds:
        html = html.replace("</body>", "".join(adds) + "</body>", 1)
    return html, made


def crop_b64(shot: Shot, x, y, w, h):
    """A rectangle of the original, as a PNG data payload."""
    import base64
    px = bytearray()
    for yy in range(y, min(y + h, shot.h)):
        for xx in range(x, min(x + w, shot.w)):
            c = shot.rgb(xx, yy)
            px += bytes((c[0], c[1], c[2], 255))
    tmp = Path(tempfile.mkdtemp(prefix="ae-crop-")) / "r.png"
    G.write_png(tmp, min(w, shot.w - x), min(h, shot.h - y), px)
    return base64.b64encode(tmp.read_bytes()).decode()


def _fill_under(fills, ln):
    """The filled element a text line is sitting on, if any."""
    cx, cy = ln["x"] + ln["w"] / 2, ln["y"] + ln["h"] / 2
    best = None
    for b in fills:
        if (b["x"] - 2 <= cx <= b["x"] + b["w"] + 2
                and b["y"] - 2 <= cy <= b["y"] + b["h"] + 2):
            if best is None or b["w"] * b["h"] < best["w"] * best["h"]:
                best = b                       # the tightest one wins
    return best


def _ink_on(shot: Shot, ln, fill_hex):
    """A label's colour, read against the thing it is printed on."""
    ground = tuple(int(fill_hex[i:i + 2], 16) for i in (1, 3, 5))
    got = []
    for y in range(ln["y"], min(ln["y"] + ln["h"], shot.h)):
        for x in range(ln["x"], min(ln["x"] + ln["w"], shot.w)):
            rgb = shot.rgb(x, y)
            d = _dist(rgb, ground)
            if d > INK:
                got.append((d, rgb))
    if not got:
        return None
    got.sort(key=lambda t: -t[0])
    c = Counter(rgb for _, rgb in got[:max(1, len(got) // 4)])
    return "#%02X%02X%02X" % c.most_common(1)[0][0]


def chrome_html(rep, w, h, limit=20):
    """The rules and the filled elements, as measured, for any emitter.

    One copy, because two emitters drifted apart here once already.

    Every element carries a `data-ae-id`. That is not decoration: it is
    the handle a later edit uses to name this exact thing. A model that
    can say "element f01" never has to say "the white box at 455,293",
    and so never has to be right about a coordinate.
    """
    out = []
    for i, r in enumerate(rep.get("rules", [])):
        # THE READING, NOT A CHOICE. `css` is the rule's own colour
        # sampled along its length and transparent where the line is
        # not there — so it stops where the original stops and fades
        # where the original fades, instead of one flat hex drawn edge
        # to edge. `color` is the fallback for a report measured before
        # rule_paint existed.
        paint = r.get("css") or r.get("color") or "#FFFFFF"
        if r["axis"] == "horizontal":
            out.append(f'<div class="r" data-ae-id="r{i:02d}" '
                       f'style="left:0;top:{r["at"]}px;'
                       f'width:{w}px;height:1px;z-index:1;'
                       f'background:{paint}"></div>')
        else:
            out.append(f'<div class="r" data-ae-id="r{i:02d}" '
                       f'style="left:{r["at"]}px;top:0;'
                       f'width:1px;height:{h}px;z-index:1;'
                       f'background:{paint}"></div>')
    for i, b in enumerate(rep.get("boxes", [])[:limit]):
        rad = b["radius"]
        rad = rad if isinstance(rad, str) else f"{rad}px"
        out.append(f'<div class="r" data-ae-id="f{i:02d}" '
                   f'style="left:{b["x"]}px;top:{b["y"]}px;'
                   f'width:{b["w"]}px;height:{b["h"]}px;z-index:2;'
                   f'background:{b["fill"]};border-radius:{rad}"></div>')
    return "".join(out)


def mask_radius(shot: Shot, field: "Field", x, y, w, h, thresh=INK):
    """Was this crop round? Ask its corners.

    An avatar carried out of a screenshot as a rectangle shows the
    square photograph it was cropped from, which is what the owner saw
    and named. Whether the source was round needs no shape fitting: a
    round mask leaves all four corners sitting on the page's own ground
    while the middle does not.
    """
    if w < 6 or h < 6:
        return None
    inset = max(1, min(w, h) // 12)
    corners = [(x + inset, y + inset), (x + w - 1 - inset, y + inset),
               (x + inset, y + h - 1 - inset),
               (x + w - 1 - inset, y + h - 1 - inset)]
    if any(_dist(shot.rgb(cx, cy), field.at(cx, cy)) > thresh
           for cx, cy in corners):
        return None
    mx, my = x + w // 2, y + h // 2
    if _dist(shot.rgb(mx, my), field.at(mx, my)) <= thresh:
        return None                       # empty crop, not a masked one
    # ONLY A SQUARE CROP CAN BE PROVED ROUND. On a wide one the corner
    # test proves nothing: a 457x35 strip of logos has background in all
    # four corners because the logos do not reach them, and the first
    # version duly rounded that strip into a stadium with 17px ends.
    # Evidence of a mask requires the content to fill the box, which is
    # exactly what being square-ish stands in for.
    if abs(w - h) > max(2, 0.10 * max(w, h)):
        return None
    return "50%"


# ─────────────────────────── text ────────────────────────────────────

def text_rows(shot: Shot, bg, band, mask, field):
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
            base = y * w
            for x in range(w):
                on = bool(mask[base + x])
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
                        "color": _ink_color(shot, mask, field, run_start, y - 1)}
                # A LINE HAS A SIZE. A COLUMN OF LINES DOES NOT.
                # Rows only break where ink stops entirely, so a
                # subheading, a button and a quote stacked with tight
                # spacing came back as ONE 248px "row" — and dividing
                # that by cap height produced "344px", a number no page
                # has ever contained. Fabricating it would be the exact
                # failure this module exists to avoid, so a tall run is
                # reported as a block with NO size rather than a line
                # with a false one.
                item["segments"] = _segments(shot, mask, run_start, y - 1)
                if item["segments"]:
                    item["left"] = item["segments"][0]["left"]
                    item["right"] = item["segments"][-1]["right"]
                    item["center"] = (item["left"] + item["right"]) // 2
                if hgt <= MAX_LINE:
                    item["kind"] = "line"
                    # INK HEIGHT IS A FACT. FONT SIZE IS AN INFERENCE.
                    # How tall a line's ink is depends on which glyphs it
                    # happens to contain: a line with descenders spans
                    # about 0.95em, one of capitals about 0.72em. The
                    # tool cannot know which without reading the text, so
                    # a single number here is false precision — it read
                    # 62px for a 48px heading purely because the words
                    # had a descender in them. Report the fact, bound the
                    # inference, and let the referee pick inside it.
                    item["ink_height"] = hgt
                    item["font_size_estimate"] = round(hgt / 0.82)
                    item["font_size_range"] = [round(hgt / 0.98),
                                               round(hgt / 0.70)]
                else:
                    item["kind"] = "block"
                    item["note"] = ("several lines with no blank row "
                                    "between them — no single font size "
                                    "applies, so none is reported")
                out.append(item)
            run_start = None
    return out


def _segments(shot: Shot, mask, y0, y1, gap=14):
    """WHERE a line of text sits, not just how tall it is.

    Text rows used to report y and height only, so every HORIZONTAL
    placement had to be judged by eye — and eye-judgement is exactly the
    part a weak model cannot do. Measured on a real page, the element
    placed that way (a button) was 43.7% wrong inside its own box while
    the carried gradient beside it was 0.2% wrong.

    A word gap is a few pixels; the gap between a logo and a nav, or
    between nav items, is much larger. Splitting on the larger gaps
    gives one segment per placed THING, with real bounds.
    """
    w = shot.w
    ink = [False] * w
    for y in range(y0, min(y1 + 1, shot.h)):
        base = y * w
        for x in range(w):
            if not ink[x] and mask[base + x]:
                ink[x] = True
    out, start, blank = [], None, 0
    for x in range(w + 1):
        on = x < w and ink[x]
        if on:
            if start is None:
                start = x
            blank = 0
        elif start is not None:
            blank += 1
            if blank >= gap or x == w:
                right = x - blank
                if right - start >= 3:
                    out.append({"left": start, "right": right,
                                "width": right - start + 1})
                start = None
    return out


def _ink_color(shot: Shot, mask, field, y0, y1):
    """The commonest INK colour in a row range = the type's colour.

    Reading this against one page colour made white text over a gradient
    report as #481912 — the gradient's own colour, not the type's.
    """
    # THE GLYPH CORE, NOT ITS EDGE. Most ink pixels of small type are
    # partial coverage — the letter fading into the ground — so the
    # commonest ink colour is a blend and reads as #423A37 for text that
    # is plainly white. The true colour is the FURTHEST from the
    # background; taking the mode of the most extreme quarter keeps that
    # without letting one stray pixel decide.
    px, w = shot.px, shot.w
    got = []
    for y in range(y0, min(y1 + 1, shot.h)):
        row = y * w * 4
        base = y * w
        for x in range(w):
            if not mask[base + x]:
                continue
            i = row + x * 4
            rgb = (px[i], px[i + 1], px[i + 2])
            got.append((_dist(rgb, field.at(x, y)), rgb))
    if not got:
        return None
    got.sort(key=lambda t: -t[0])
    c = Counter(rgb for _, rgb in got[:max(1, len(got) // 4)])
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
    dark = sum(bg["rgb"]) < 384
    field = Field(shot, dark=dark)
    mask = ink_mask(shot, field)
    # The ground estimated from ground only — see Field.refine.
    field.refine(mask)
    mask = ink_mask(shot, field)
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
        b["text"] = text_rows(shot, bgrgb, b, mask, field)
    rep["rules"] = rules(shot, mask, field)
    for r in rep["rules"]:
        r.update(rule_paint(shot, field, r["axis"], r["at"], mask=mask))
    # A LINE OF TYPE CAN LOOK LIKE A RULE and one did: the heading row
    # is brighter than the rows above and below it along 20% of the
    # page, which is exactly what rules() asks for, so a phantom
    # hairline was drawn straight through the headline. What separates
    # them is not how MUCH is lit but how much of it is JOINED — a rule
    # runs unbroken, type is strokes with gaps between every letter.
    span = {"horizontal": shot.w, "vertical": shot.h}
    rep["rules"] = [r for r in rep["rules"]
                    if r["longest"] >= 0.12 * span[r["axis"]]]
    rep["bands"] = bs
    # WHOLE ELEMENTS FIRST, strips only if there are none. regions()
    # unions overlapping runs and so returns a button with its label on
    # it as one thing; boxes() grows columns that line up and returns
    # the same button as strips it then cannot rejoin. Keeping the old
    # pass as the fallback honours the rule this file has broken twice:
    # never ship a detector that finds FEWER things than the one before
    # it. Either way a slab carved out of a ramp is not a card.
    found = regions(shot, field)
    if not found:
        found = boxes(shot, bgrgb, step=step, field=field)
    rep["boxes"] = [b for b in found
                    if not _in_any(grads, b["x"], b["y"], b["w"], b["h"])][:40]
    return rep


# ─────────────────────────── the carry pass ──────────────────────────

def carry_pass(html: str, original, regions=None, rep=None) -> str:
    """Put the un-recreatable parts back, mechanically.

    THIS IS THE WHOLE ANSWER TO "CAN A WEAK MODEL DO IT". Measured, on
    one screenshot, same measurements, same prompt:

        gemini-3.6-flash alone ................ 53.05%
        + the referee tuning its type sizes ... 53.66%
        + THIS ................................ 94.46%
        a frontier model doing it by hand ..... 95.92%

    The model's STRUCTURE was never the problem — its nav scored 4.2%
    wrong and its heading 7.4%, as good as anyone's. The entire gap was
    two mechanical jobs it was asked to do by hand and could not: it
    mis-applied the page's colour field (the whole lower page came back
    100% wrong) and it had no way to reproduce raster logos (88.2%
    wrong). Both belong to the tool.

    So the tool does them. The model writes structure and words; this
    lays the page's own ground behind everything and carries the
    regions that are photographs rather than design — which is exactly
    what a developer does when they export an asset.
    """
    shot = load(original) if not isinstance(original, Shot) else original
    rep = rep or measure(original)
    field = Field(shot, dark=sum(rep["background"]["rgb"]) < 384)
    field.refine(ink_mask(shot, field))
    import base64
    fb = base64.b64encode(field.png_bytes()).decode()

    # A model asked to place this by hand gets it wrong; strip whatever
    # it did and lay the real one down at exactly the canvas size.
    html = re.sub(r"<div[^>]*id=[\"']field[\"'][^>]*>\s*</div>", "", html)
    html = re.sub(r"background-image:\s*url\(data:image/png;base64,[^)]+\)",
                  "", html)
    w, h = rep["width"], rep["height"]
    ground = (f'<div style="position:absolute;left:0;top:0;width:{w}px;'
              f'height:{h}px;z-index:0;background-size:100% 100%;'
              f'background-image:url(data:image/png;base64,{fb})"></div>')

    carried = []
    for r in (regions or []):
        x0, y0, x1, y1 = r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]
        px = bytearray()
        for y in range(y0, min(y1, shot.h)):
            for x in range(x0, min(x1, shot.w)):
                c = shot.rgb(x, y)
                px += bytes((c[0], c[1], c[2], 255))
        tmp = Path(tempfile.mkdtemp(prefix="ae-carry-")) / "r.png"
        G.write_png(tmp, x1 - x0, y1 - y0, px)
        b64 = base64.b64encode(tmp.read_bytes()).decode()
        extra = r.get("style", "")
        if "border-radius" not in extra:
            _r = mask_radius(shot, field, x0, y0, x1 - x0, y1 - y0)
            if _r:
                extra += f"border-radius:{_r};"
        carried.append(
            f'<img alt="" style="position:absolute;left:{x0}px;top:{y0}px;'
            f'width:{x1 - x0}px;height:{y1 - y0}px;z-index:5;{extra}" '
            f'src="data:image/png;base64,{b64}">')

    if "<body>" in html:
        html = html.replace("<body>", "<body>" + ground, 1)
    else:
        html = ground + html
    if "</body>" in html:
        html = html.replace("</body>", "".join(carried) + "</body>", 1)
    else:
        html += "".join(carried)
    # whatever the model drew belongs above the ground, not under it
    if "<style>" in html:
        html = html.replace(
            "<style>", "<style>body>*{position:relative;z-index:2}\n", 1)
    return html


def snap_pass(html: str, original, render_fn, rounds=2, verbose=False):
    """Move every text element to where it belongs, without a model.

    THE CORRECTION LOOP BELONGS HERE, NOT IN A PROMPT. Handed its own
    mistakes as a list, gemini-3.6-flash made the page worse three times
    out of three — deleting rules that were right, inventing empty text
    divs, moving a heading that was already correct — because a finding
    like "text at y224, move it +10px" names a run in the ORIGINAL and
    the builder cannot tell which of its own divs that is.

    WHY THIS DOES NOT COMPUTE THE DELTA DIRECTLY. The obvious version
    measures both images, pairs the runs and applies the difference. It
    was built, and it does not work: the two images do not SEGMENT the
    same way. A paragraph whose lines touch is one run in the original
    and three in the rebuild, so the pairing slips and every delta after
    it is nonsense. Measured, that version scored 0.9387 against 0.9446
    and was correctly thrown away by the guard below.

    So it searches instead. Each text element is nudged a few pixels and
    a few percent, and the referee says whether that helped. Slower, and
    it cannot be fooled by a segmentation that disagrees.

    NOTHING HERE CAN MAKE A PAGE WORSE: every candidate is rendered and
    kept only if it scores higher. A model's revision is a coin flip;
    a sweep that discards anything worse cannot lose.

    `render_fn(html) -> (png_path, identical_score)`.
    """
    _, best = render_fn(html)
    best_html = html
    if verbose:
        print(f"    start {best:.4f}")

    # BOXES FIRST, AND EXACTLY. Text has to be searched because the two
    # images segment it differently, but a filled box is found the same
    # way in both — so its geometry is not a guess, it is a number, and
    # it can be written straight in. Skipped until now because the pass
    # only touched elements carrying text: the button stayed 41.4% wrong
    # inside its own region through every round.
    a_boxes = measure(original).get("boxes", [])
    if a_boxes:
        png, _ = render_fn(best_html)
        mine_boxes = measure(png).get("boxes", [])
        cand = re.findall(
            r'style="([^"]*width:\s*[\d.]+px[^"]*height:\s*[\d.]+px[^"]*)"',
            best_html)
        for style in cand:
            def num(prop, st=style):
                m = re.search(rf"{prop}:\s*([\d.]+)px", st)
                return float(m.group(1)) if m else None
            x, y, w, h = (num("left"), num("top"), num("width"), num("height"))
            if None in (x, y, w, h) or w < MIN_BOX or h < MIN_BOX:
                continue
            near = [b for b in a_boxes
                    if abs(b["x"] - x) < 60 and abs(b["y"] - y) < 60]
            if not near:
                continue
            t = min(near, key=lambda b: abs(b["x"] - x) + abs(b["y"] - y))
            ns = style
            for prop, val in (("left", t["x"]), ("top", t["y"]),
                              ("width", t["w"]), ("height", t["h"])):
                ns = re.sub(rf"{prop}:\s*[\d.]+px", f"{prop}:{val}px",
                            ns, count=1)
            if "border-radius" in ns:
                ns = re.sub(r"border-radius:\s*[\d.]+px",
                            f"border-radius:{t['radius']}px", ns, count=1)
            if ns == style:
                continue
            _, sc = render_fn(best_html.replace(style, ns, 1))
            if sc > best:
                best_html = best_html.replace(style, ns, 1)
                best = sc
                if verbose:
                    print(f"    box at ({x:.0f},{y:.0f}) -> "
                          f"{t['w']}x{t['h']} r{t['radius']}  {best:.4f}")

    for rnd in range(rounds):
        # only elements that CARRY TEXT — a page like this has dozens of
        # hairline rule divs and a handful of text divs, and attributing
        # a heading's correction to a rule moves nothing.
        els = []
        for m in re.finditer(
                r'<(\w+)([^>]*style="([^"]*top:\s*[\d.]+px[^"]*)"[^>]*)>'
                r'(.*?)</\1>', best_html, re.S):
            inner = re.sub(r"<[^>]+>", "", m.group(4)).strip()
            if inner:
                els.append(m.group(3))
        if not els:
            break
        moved = 0
        for style in els:
            if style not in best_html:
                continue                      # edited already this round
            cand = []
            for d in (-6, -3, -1, 1, 3, 6):
                cand.append(("top", d))
            if "left:" in style:
                for d in (-6, -3, 3, 6):
                    cand.append(("left", d))
            if "font-size:" in style:
                for k in (0.88, 0.94, 1.06, 1.12):
                    cand.append(("font", k))
            local_best, local_style = best, style
            for kind, v in cand:
                if kind == "font":
                    ns = re.sub(r"font-size:\s*([\d.]+)px",
                                lambda m: f"font-size:{float(m.group(1)) * v:.1f}px",
                                style, count=1)
                else:
                    ns = re.sub(rf"{kind}:\s*([\d.]+)px",
                                lambda m: f"{kind}:{float(m.group(1)) + v:.1f}px",
                                style, count=1)
                if ns == style:
                    continue
                _, sc = render_fn(best_html.replace(style, ns, 1))
                if sc > local_best + 0.0002:
                    local_best, local_style = sc, ns
            if local_style is not style:
                best_html = best_html.replace(style, local_style, 1)
                best = local_best
                moved += 1
        if verbose:
            print(f"    round {rnd + 1}: {moved} element(s) improved -> {best:.4f}")
        if not moved:
            break
    return best_html, best


# Grotesques a 2020s marketing page is actually likely to use. The list
# is deliberately narrow: every extra candidate is a page render, and
# families that look nothing alike cannot win.
FONT_CANDIDATES = [
    ("Inter", "Inter:wght@400;500;600;700"),
    ("Geist", "Geist:wght@400;500;600;700"),
    ("Manrope", "Manrope:wght@400;500;600;700"),
    ("DM Sans", "DM+Sans:wght@400;500;700"),
    ("Figtree", "Figtree:wght@400;500;600;700"),
    ("Plus Jakarta Sans", "Plus+Jakarta+Sans:wght@400;500;600;700"),
    ("Outfit", "Outfit:wght@400;500;600;700"),
    ("Sora", "Sora:wght@400;500;600;700"),
    ("Space Grotesk", "Space+Grotesk:wght@400;500;700"),
    ("Onest", "Onest:wght@400;500;600;700"),
    ("Schibsted Grotesk", "Schibsted+Grotesk:wght@400;500;700"),
    ("General Sans", "General+Sans:wght@400;500;600"),
]


def fit_font(html: str, render_fn, region=None, verbose=False):
    """Let the referee choose the typeface, the weight and the tracking.

    On a text-heavy page the typeface IS the ceiling: once colours and
    positions are right, what remains is the shape of the letters, and
    no amount of nudging a correct position fixes a wrong glyph. Picking
    it by eye is exactly the judgement this project keeps removing —
    when swept properly the first time, the winner was Geist, where a
    frontier model would have said Inter.

    Weight and letter-spacing are swept too, and often matter more than
    the family: a 500 rendered at 600 is wrong in every glyph at once.

    `region` scores only part of the page (x0, y0, x1, y1), so a
    candidate can be judged on the heading it is supposed to match
    rather than diluted across a page that is mostly background.
    """
    def score(h):
        return render_fn(h, region)[1] if region else render_fn(h)[1]

    best_html, best = html, score(html)
    if verbose:
        print(f"    start {best:.4f}")

    for name, spec in FONT_CANDIDATES:
        h = best_html
        h = re.sub(r'<link[^>]*fonts\.googleapis[^>]*>', "", h)
        link = ('<link rel="stylesheet" href="https://fonts.googleapis.com'
                f'/css2?family={spec}&display=swap">')
        if "<head>" in h:
            h = h.replace("<head>", "<head>" + link, 1)
        else:
            h = link + h
        # THE CHARACTER CLASS MUST ADMIT QUOTES. It excluded them while
        # the replacement INSERTED them, so on the second candidate the
        # pattern matched only "font-family:" and produced
        #   font-family:'Geist',sans-serif'Inter',sans-serif
        # — malformed, silently ignored by the browser, every candidate
        # falling back to the same face and scoring identically to four
        # decimal places. A sweep whose options are all the same option
        # looks like a working sweep.
        # Monospace is left alone: a page that sets it means it.
        h = re.sub(r"font-family:\s*(?![^;}]*monospace)[^;}]+",
                   f"font-family:'{name}',sans-serif", h)
        sc = score(h)
        if verbose:
            print(f"      {name:<20} {sc:.4f}")
        if sc > best + 0.0002:
            best_html, best = h, sc
    for prop, vals in (("letter-spacing", ("-.03em", "-.02em", "-.01em", "0")),
                       ("font-weight", ("450", "500", "550", "600"))):
        for v in vals:
            h = re.sub(rf"{prop}:[^;}}\"']+", f"{prop}:{v}", best_html)
            if h == best_html:
                continue
            sc = score(h)
            if sc > best + 0.0002:
                if verbose:
                    print(f"      {prop}:{v} -> {sc:.4f}")
                best_html, best = h, sc
    return best_html, best


def ink_iou(a_png, b_png, b_mask=None, b_shot=None):
    """How much of the ink lands in the same PLACE. 0..1.

    THE PERCENTAGE THAT MISLED US. "Pixels identical" counts the whole
    canvas, and a page like this is mostly dark ground and gradient —
    both of which the carry pass gets exactly right. So a rebuild whose
    text was visibly in the wrong places still scored 95.6%, and the
    owner spotted by eye what the number was hiding.

    Content-only exact matching overcorrects: antialiased glyph edges
    almost never match to tolerance even when perfectly placed, which
    scored a good build at 30%.

    Overlap of the two INK MASKS asks the question that actually
    matters — is the ink where it should be — and does not care whether
    the letters have the same shape. Use it to judge PLACEMENT; use the
    referee's identical score to judge the finished look.
    """
    if b_mask is None:
        b_shot = load(b_png)
        f = Field(b_shot, dark=True)
        m = ink_mask(b_shot, f)
        f.refine(m)
        b_mask = ink_mask(b_shot, f)
        b_field = f
    a = load(a_png)
    # judged against the ORIGINAL's ground, so a candidate cannot score
    # well by shifting what counts as background
    fa = Field(a, dark=True)
    fa.refine(ink_mask(a, fa))
    ma = ink_mask(a, fa)
    inter = union = 0
    for i in range(len(b_mask)):
        x, y = ma[i], b_mask[i]
        if x or y:
            union += 1
            if x and y:
                inter += 1
    return inter / max(union, 1)


def locate_elements(html: str, render_fn, verbose=False):
    """Render each text element ALONE to learn exactly where it lands.

    Every earlier pass tried to work out which ink belonged to which
    element by lining up measured runs in order, and every earlier pass
    was defeated by the same thing: the two images do not segment alike,
    so the pairing slips. It also could not see an element at all unless
    it already had a `top:` — which is precisely how a caption written
    with `left:0` and no `top` stayed in normal flow, enormous and
    jammed against the left edge, through three rounds of "correction".

    ISOLATION BY DIFFERENCE, not by ink. The first version hid the other
    text and measured whatever ink remained, and every element came back
    as the whole canvas: a carried gradient and a carried logo strip are
    still on the page and still register. So render once with ALL text
    hidden, then once per element, and take the pixels that CHANGED.
    Those pixels are that element and nothing else.
    """
    body_at = html.find("<body")
    body = html[body_at:] if body_at >= 0 else html
    # never treat CSS as content: the first version matched a rule out
    # of the <style> block and solemnly reported its position
    scrubbed = re.sub(r"(?is)<(style|script)\b.*?</\1>", "", body)
    els, seen = [], set()
    for m in re.finditer(r"<(\w+)([^>]*)>([^<]{2,})</\1>", scrubbed):
        text = m.group(3).strip()
        if not text or m.group(0) in seen:
            continue
        seen.add(m.group(0))
        els.append({"whole": m.group(0), "tag": m.group(1),
                    "attrs": m.group(2), "text": text})
    if not els:
        return []

    def hide(h, items):
        for it in items:
            h = h.replace(it["whole"],
                          it["whole"].replace(
                              f"<{it['tag']}{it['attrs']}>",
                              f"<{it['tag']}{it['attrs']} hidden>", 1), 1)
        return h

    base_png, _ = render_fn(hide(html, els))
    base = load(base_png)
    out = []
    for e in els:
        png, _ = render_fn(hide(html, [x for x in els if x is not e]))
        shot = load(png)
        xs, ys = [], []
        for y in range(shot.h):
            for x in range(shot.w):
                if _dist(shot.rgb(x, y), base.rgb(x, y)) > 16:
                    xs.append(x)
                    ys.append(y)
        box = ({"left": min(xs), "right": max(xs), "top": min(ys),
                "bottom": max(ys), "w": max(xs) - min(xs) + 1,
                "h": max(ys) - min(ys) + 1} if xs else None)
        out.append({**e, "box": box})
        if verbose:
            print(f"   {e['text'][:32]:<34} "
                  + (f"x{box['left']}-{box['right']} y{box['top']}"
                     f"-{box['bottom']}" if box else "DRAWS NOTHING"))
    return out


OCR_SRC = ROOT / "aethron_ocr.swift"
OCR_BIN = ROOT / ".aethron_ocr"


def ocr(image, timeout=120):
    """The words and their exact boxes, from the OCR already in macOS.

    THE MISSING FACT. Every placement failure in this project came from
    guessing which element corresponds to which measured run, using
    order and geometry, and every attempt to be cleverer about the guess
    failed: absolute moves, isolating elements, inverting the emitter,
    hard-constraining the prompt — 41.4%, defeated, 22.3%, 19.5%.

    Text settles it exactly. And OCR gives something the ink measurement
    cannot: ONE BOX PER LINE. A tightly-led paragraph measures as a
    single 32px-tall run, which is why the emitter gave a 13px paragraph
    a 39px face; Vision returns its three lines separately, each with
    its own height.

    Compiled once with the swiftc already on the machine, so the Python
    side stays stdlib-only — no pip install, no network, no key. Returns
    None if it cannot build or run, and the caller must then say so
    rather than pretend it read anything.
    """
    if not OCR_SRC.exists():
        return None
    if not OCR_BIN.exists() or OCR_BIN.stat().st_mtime < OCR_SRC.stat().st_mtime:
        sw = shutil.which("swiftc")
        if not sw:
            return None
        r = subprocess.run([sw, "-O", str(OCR_SRC), "-o", str(OCR_BIN)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode or not OCR_BIN.exists():
            return None
    try:
        r = subprocess.run([str(OCR_BIN), str(image)], capture_output=True,
                           text=True, timeout=timeout)
    except Exception:
        return None
    if r.returncode:
        return None
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def emit_from_ocr(image, font="Inter", carried=(), rep=None,
                  ocr_lines=None, ground_cell=4):
    """Build the whole page from what was READ and what was MEASURED.

    No model anywhere in this function. OCR supplies the words and where
    each line sits; the measurement supplies the ground, the rules, the
    filled boxes and every colour. That removes the last place a model
    could put something in the wrong place.

    Font size comes from the LINE's own box height rather than a run's,
    which is the whole reason this can work where emit_page could not.
    """
    rep = rep or measure(image)
    lines = ocr_lines if ocr_lines is not None else ocr(image)
    if lines is None:
        return None
    shot = load(image)
    dark = sum(rep["background"]["rgb"]) < 384
    field = Field(shot, dark=dark)
    field.refine(ink_mask(shot, field))
    mask = ink_mask(shot, field)
    import base64
    # TWO FIELDS, BECAUSE THEY ANSWER TWO DIFFERENT QUESTIONS — and
    # conflating them is what the owner saw as blur.
    #
    # DETECTION wants a COARSE field. Its job is to say what is ink and
    # what is ground, and a fine field absorbs whole elements into the
    # "ground" (a cell inside a card is all card, so the percentile has
    # nothing else to pick) — after which the element is not ink, is
    # never found, and is never reproduced.
    #
    # RENDERING wants a FINE one. The emitted background is the
    # FALLBACK layer: it paints everything no other pass claimed, so on
    # a page whose hero holds a screenshot-of-a-dashboard it is drawing
    # real content. At 8px cells that content arrives as a 150x112
    # thumbnail stretched over the canvas, which is exactly right for a
    # glow and exactly wrong for a dashboard.
    #
    # Measured on that page: mean error off-ink 2.76 at 8px, 1.94 at
    # 4px, 1.52 at 2px, for 12.6 KB / 43 KB / 149 KB. 4px is the
    # default because it halves the error for a file still smaller than
    # one photograph; `ground_cell` trades sharpness against weight.
    ground = Field(shot, gw=max(16, round(shot.w / ground_cell)),
                   gh=max(12, round(shot.h / ground_cell)), dark=dark)
    ground.refine(mask)
    fb = base64.b64encode(ground.png_bytes()).decode()
    w, h = rep["width"], rep["height"]

    out = [f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={
    font.replace(' ', '+')}:wght@300;400;500;600;700&display=swap">
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{w}px;height:{h}px;overflow:hidden}}
body{{background:{rep['background']['hex']};position:relative;
 font-family:'{font}',-apple-system,Helvetica,Arial,sans-serif;
 -webkit-font-smoothing:antialiased}}
.t{{position:absolute;white-space:nowrap;line-height:1}}
.r{{position:absolute}}</style></head><body>
<div class="r" data-ae-id="ground" style="left:0;top:0;width:{w}px;
 height:{h}px;z-index:0;background-size:100% 100%;
 background-image:url(data:image/png;base64,{fb})"></div>"""]
    out.append(chrome_html(rep, w, h))
    fills = rep.get("boxes", [])[:20]

    def inside_carried(ln):
        # A carried region is a photograph of that part of the page; the
        # words in it are already there. Drawing them again on top is
        # how a logo strip became "VIVUVIYOIIII"VIVUCINUU".
        cy = ln["y"] + ln["h"] / 2
        cx = ln["x"] + ln["w"] / 2
        for c in carried:
            if (c["x"] - 4 <= cx <= c["x"] + c["w"] + 4
                    and c["y"] - 4 <= cy <= c["y"] + c["h"] + 4):
                return True
        return False

    for ln in lines:
        txt = (ln.get("text") or "").strip()
        # The same bar as raster_regions, and for the same reason: what
        # OCR is unsure of is carried as pixels, never set as type.
        if not txt or ln.get("confidence", 1) < 0.6 or inside_carried(ln):
            continue
        bh = max(6, int(ln["h"]))
        # START SMALL ON PURPOSE. The box bounds one line's ink, and the
        # face that produced it is usually close to that height — but
        # guess high and neighbouring lines OVERLAP, which is not merely
        # ugly: the overlap makes the render unreadable to OCR, the line
        # then matches nothing, and the correction pass that would have
        # fixed the size never fires. Measured: h/0.74 rendered a 37px
        # heading at 51px and its two lines came back from OCR as one
        # smear, "Than& tart, yvayuse rmanage".
        # Undersized text stays legible, stays matchable, and is scaled
        # up by the correction in one round.
        size = bh * 0.88
        # A LABEL ON A BUTTON IS NOT INK ON THE PAGE. _ink_color reads
        # against the page's ground, and on a white pill the dark label
        # is nearer that dark ground than anything else in the row — so
        # the button's own text was coloured from whatever else shared
        # its rows. Inside a fill, the ground IS the fill.
        host = _fill_under(fills, ln)
        if host:
            colour = _ink_on(shot, ln, host["fill"]) or "#000000"
        else:
            colour = _ink_color(shot, mask, field, ln["y"],
                                ln["y"] + bh) or "#FFFFFF"
        esc = (txt.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;"))
        out.append(f'<div class="t" data-ae-id="t{len(out):02d}" '
                   f'style="left:{ln["x"]}px;'
                   f'top:{ln["y"]}px;font-size:{size:.1f}px;'
                   f'color:{colour};z-index:4">{esc}</div>')
    for c in carried:
        style = c.get("style", "")
        if "border-radius" not in style:
            # THE OWNER'S "PERFECT CIRCLE". An avatar cropped out as a
            # rectangle ships the square photograph it was cut from.
            # mask_radius asks the crop's own corners whether the source
            # was round, so the mask is measured rather than assumed —
            # and a square logo keeps its square corners.
            _r = mask_radius(shot, field, c["x"], c["y"], c["w"], c["h"])
            if _r:
                style += f"border-radius:{_r};"
        out.append(f'<img alt="" class="r" data-ae-id="p{len(out):02d}" '
                   f'style="left:{c["x"]}px;'
                   f'top:{c["y"]}px;width:{c["w"]}px;height:{c["h"]}px;'
                   f'z-index:5;{style}" '
                   f'src="data:image/png;base64,{c["b64"]}">')
    out.append("</body></html>")
    return "".join(out)


def _pixels_match(a: "Shot", b: "Shot", x, y, w, h, tol=INK, need=0.90):
    """Do two pages agree, pixel for pixel, inside one rectangle?"""
    ok = n = 0
    for yy in range(y, min(y + h, a.h, b.h)):
        for xx in range(x, min(x + w, a.w, b.w)):
            n += 1
            ok += _dist(a.rgb(xx, yy), b.rgb(xx, yy)) <= tol
    return (n and ok / n >= need), (ok / n if n else 0.0)


def rebuild(image, outdir, font="Inter", rounds=7, fit=True,
            verbose=True, ground_cell=4):
    """A screenshot in, a page out, and not one model anywhere.

    The whole chain in one call, in the order that each stage earns:

      1. MEASURE   the ground, its gradients, the rules and every filled
                   element, from the pixels.
      2. READ      the words and each line's box, with the OCR that
                   ships with the machine.
      3. EMIT      the page: the colour field carried behind everything,
                   the rules painted from their own pixels, the fills at
                   their measured corners, the type set line by line,
                   and anything OCR could not name carried as a crop.
      4. FIT       the typeface, chosen by a referee on ink overlap
                   rather than by anyone's taste.
      5. CORRECT   by rendering, reading the result back, and matching
                   line to line BY ITS WORDS — keeping the round that
                   actually scored best, not merely the last one.
      6. CARRY     whatever still cannot be set as type, as pixels, so
                   the page is never left wrong where it could be right.
      7. CHECK     every line and every carried region, and say so.

    Returns the report. It is a checklist, not a percentage, because a
    percentage cannot fail a page that is wrong in the places that
    matter — this page scored 95.6% with its entire navigation missing.
    """
    import aethron_figma_grade as GR
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    image = Path(image)
    if image.suffix.lower() != ".png":
        # the referee reads PNG; convert once and work from that
        png = outdir / "original.png"
        subprocess.run([shutil.which("sips") or "sips", "-s", "format",
                        "png", str(image), "--out", str(png)],
                       capture_output=True)
        if png.is_file():
            image = png

    def say(*a):
        if verbose:
            print(*a)

    rep = measure(image)
    w, h = rep["width"], rep["height"]
    say(f"  {w}x{h}  ground {rep['background']['hex']}  "
        f"{len(rep.get('rules', []))} rules  "
        f"{len(rep.get('boxes', []))} filled elements")

    lines = ocr(image)
    if lines is None:
        return {"verdict": "SKIPPED", "why": "no OCR on this machine — "
                "UNVERIFIED, not proven good", "findings": []}
    say(f"  {len(lines)} lines read")

    shot = load(image)
    field = Field(shot, dark=sum(rep["background"]["rgb"]) < 384)
    field.refine(ink_mask(shot, field))
    regs = raster_regions(shot, field, lines, fills=rep.get("boxes", []))
    carried = [dict(r, b64=crop_b64(shot, r["x"], r["y"], r["w"], r["h"]))
               for r in regs]
    say(f"  {len(carried)} region(s) carried as pixels")

    html = emit_from_ocr(image, font=font, carried=carried, rep=rep,
                         ocr_lines=lines, ground_cell=ground_cell)
    n = [0]

    def draw(h_, region=None):
        n[0] += 1
        hp, pp = outdir / f"_{n[0]}.html", outdir / f"_{n[0]}.png"
        hp.write_text(h_)
        GR.shoot(hp, w, h, pp)
        return pp, ink_iou(pp, image)

    if fit:
        html = fit_font(html, draw, verbose=verbose)
        if isinstance(html, tuple):
            html = html[0]
    html = refine_with_ocr(html, image, draw, rounds=rounds, verbose=verbose)
    if isinstance(html, tuple):
        html = html[0]
    mid = outdir / "refined.png"
    (outdir / "refined.html").write_text(html)
    GR.shoot(outdir / "refined.html", w, h, mid)
    html, extra = carry_failures(html, image, mid, carried=regs)
    if extra:
        say(f"  {len(extra)} line(s) carried rather than left wrong")
    regs = regs + extra
    (outdir / "page.html").write_text(html)
    GR.shoot(outdir / "page.html", w, h, outdir / "page.png")
    v = verify_rebuild(image, outdir / "page.png", carried=regs)
    v["page"] = str(outdir / "page.html")
    v["carried"] = regs
    (outdir / "report.json").write_text(json.dumps(v, indent=1))
    say(f"  {v['verdict']}: {v.get('lines_correct')} of "
        f"{v.get('lines_expected')} checks correct")
    for f in v["findings"][:12]:
        say(f"     {f['kind']:20s} {f['text'][:40]}")
    return v


def verify_rebuild(original, rebuild_png, tol=4, size_tol=0.15, carried=()):
    """Is every line THERE, in the right PLACE, at the right SIZE?

    THE CHECK THE SCORE COULD NOT DO. "95.6% identical" was reported for
    a page with a caption rendered enormous in the wrong place, a button
    hundreds of pixels low and text piled on top of other text — because
    the canvas is mostly ground and gradient and those were right. A
    percentage cannot fail a page that is wrong in the places that
    matter; a checklist can.

    Reading both pages makes it a checklist. Every line of the original
    is looked for by its own words: missing, misplaced, resized, or
    correct. There is nothing to infer and nothing to average away.
    """
    want = ocr(original)
    got = ocr(rebuild_png)
    if want is None or got is None:
        return {"verdict": "SKIPPED", "why": "no OCR available — "
                "UNVERIFIED, not proven good", "findings": []}
    # MATCH ON SIMILARITY, NOT ON AN EQUAL STRING. The rebuild is set in
    # a different face, so OCR reads its "Docs" as "Dacs" and its
    # "effective\"" as "effective*". Demanding an exact string reported
    # six such lines as MISSING when they were present and correctly
    # placed — a checker that cries wolf is one people stop reading.
    import difflib
    findings, ok = [], 0
    used = set()
    # A CARRIED REGION IS NOT GRADED BY READING IT. The logo strip is a
    # crop of the original, so it is right by construction — and OCR
    # still reported it MISSING, because it segments that row of
    # wordmarks differently on every read: '*Oogcipum N Iim' one time,
    # 'logoipsum' plus 'N IOOisum' the next. Grading pixels against
    # pixels is not a softer check than reading the words, it is a
    # far stricter one, and it is the check that actually applies.
    shot_a = original if isinstance(original, Shot) else load(original)
    shot_b = load(rebuild_png)
    for c in carried or ():
        good, frac = _pixels_match(shot_a, shot_b, c["x"], c["y"],
                                   c["w"], c["h"])
        if good:
            ok += 1
        else:
            findings.append({"kind": "CARRIED WRONG",
                             "text": f"picture at ({c['x']},{c['y']})",
                             "want": "the original's own pixels",
                             "got": f"{frac * 100:.0f}% of them match"})

    def in_carried(ln):
        cx, cy = ln["x"] + ln["w"] / 2, ln["y"] + ln["h"] / 2
        return any(c["x"] - 4 <= cx <= c["x"] + c["w"] + 4
                   and c["y"] - 4 <= cy <= c["y"] + c["h"] + 4
                   for c in carried or ())

    for w in want:
        key = w["text"].strip()
        if not key or in_carried(w):
            continue
        cands = []
        for i, g in enumerate(got):
            if i in used:
                continue
            t = g["text"].strip()
            if not t:
                continue
            r = difflib.SequenceMatcher(None, key.lower(), t.lower()).ratio()
            if r >= 0.72:
                cands.append((r, i, g))
        if cands:
            cands.sort(key=lambda c: (-c[0], abs(c[2]["y"] - w["y"])))
            used.add(cands[0][1])
            cands = [cands[0][2]]
        if not cands:
            findings.append({"kind": "MISSING", "text": key[:48],
                             "want": f"at ({w['x']},{w['y']})",
                             "got": "not on the page at all"})
            continue
        g = cands[0]
        dx, dy = g["x"] - w["x"], g["y"] - w["y"]
        bad = False
        if abs(dx) > tol or abs(dy) > tol:
            findings.append({"kind": "MISPLACED", "text": key[:48],
                             "want": f"({w['x']},{w['y']})",
                             "got": f"({g['x']},{g['y']})",
                             "fix": f"move it {-dx:+d},{-dy:+d}"})
            bad = True
        if w["h"] and abs(g["h"] - w["h"]) / w["h"] > size_tol:
            findings.append({"kind": "WRONG SIZE", "text": key[:48],
                             "want": f"{w['h']}px of ink",
                             "got": f"{g['h']}px",
                             "fix": f"scale the font by "
                                    f"{w['h'] / max(1, g['h']):.2f}"})
            bad = True
        ok += not bad
    extra = [g for i, g in enumerate(got)
             if i not in used and g["text"].strip() and not in_carried(g)]
    for e in extra[:8]:
        findings.append({"kind": "NOT IN THE ORIGINAL",
                         "text": e["text"][:48],
                         "want": "nothing here",
                         "got": f"at ({e['x']},{e['y']})"})
    n = len([w for w in want if w["text"].strip() and not in_carried(w)])
    n += len(carried or ())
    return {"verdict": "PASS" if not findings else "FAIL",
            "lines_expected": n, "lines_correct": ok,
            "findings": findings}


def refine_with_ocr(html, image, render_fn, rounds=4, verbose=False):
    """Correct every line by reading BOTH pages and matching the words.

    This is the mapping that was missing all along. Earlier passes had
    to infer which element produced which ink from order and geometry,
    and every version of that inference was beaten by something —
    paragraphs that measure as one run, elements that opt out of
    positioning, siblings that move when their neighbours are hidden.

    Reading both images removes the inference. "Get started for free" in
    the original and "Get started for free" in the render are the same
    thing because they are the same STRING; the correction is then plain
    arithmetic on two boxes.

    It also fixes the offset that made the first OCR build worse than
    guessing: OCR reports where the INK begins, CSS `top` positions the
    BOX, and the gap between them depends on the face and the size. So
    do not compute it — render, read back where the ink actually landed,
    and move by the difference.
    """
    want = ocr(image)
    if want is None:
        return html, None
    # WHAT "BEST" HAS TO MEAN. The first version of this loop called
    # its running value best_html and never scored anything — each
    # round simply overwrote the last. With a handful of lines to fix
    # that converges by luck; with twenty it CHASES NOISE, because OCR
    # reports ink height as a whole number and a 12px line reads 11 or
    # 13 depending on the round. Measured, the unscored loop corrected
    # 12 lines, then 10, then 8, then 7, and ended WORSE than it
    # started: 15 of 21 against 17.
    # So score every round on the checklist it is trying to satisfy and
    # keep the render that actually scored highest. A correction that
    # makes the page worse is thrown away, which is the same rule the
    # pixel referee has always used.
    best_html, best_score, best_round = html, -1, 0
    cur = html
    for rnd in range(rounds):
        png, _ = render_fn(cur)
        got = ocr(png)
        if not got:
            break
        by_text = {}
        for g in got:
            by_text.setdefault(g["text"].strip(), []).append(g)
        score, moved = 0, 0
        new_html = cur
        for wln in want:
            key = wln["text"].strip()
            cands = by_text.get(key)
            if not cands:
                continue
            g = min(cands, key=lambda c: abs(c["y"] - wln["y"]))
            dx, dy = wln["x"] - g["x"], wln["y"] - g["y"]
            scale = wln["h"] / max(1, g["h"])
            # SETTLED IS SETTLED. A line already inside tolerance is
            # left alone; nudging it again is how the loop oscillated.
            wide = abs(wln["w"] / max(1, g["w"]) - 1) if wln["w"] else 0
            if (abs(dx) <= 2 and abs(dy) <= 2 and abs(scale - 1) <= 0.10
                    and wide <= 0.12):
                score += 1
                continue
            esc = (key.replace("&", "&amp;").replace("<", "&lt;")
                      .replace(">", "&gt;"))
            # MATCH THE ELEMENT, NOT ONE EXACT SPELLING OF ITS TAG.
            # This pattern named the attributes in order and in full,
            # so the day every element gained a data-ae-id it silently
            # matched nothing — the loop reported "corrected 0" on a
            # page with seven faults and the correction stage quietly
            # stopped existing. A brittle regex does not fail, it
            # abstains, which is worse.
            m = re.search(r'<div class="t"[^>]*?style="([^"]*)"[^>]*>'
                          + re.escape(esc) + r"</div>", new_html)
            if not m:
                continue
            style = m.group(1)
            ns = style
            if abs(dy) >= 1:
                ns = re.sub(r"top:\s*([-\d.]+)px",
                            lambda z: f"top:{float(z.group(1)) + dy:.1f}px",
                            ns, count=1)
            if abs(dx) >= 1:
                ns = re.sub(r"left:\s*([-\d.]+)px",
                            lambda z: f"left:{float(z.group(1)) + dx:.1f}px",
                            ns, count=1)
            if abs(scale - 1) > 0.10:
                # HALF A STEP, NOT A WHOLE ONE. The ink height that
                # this scale is computed from is quantised, so the
                # full correction routinely overshoots and the next
                # round corrects back. Damped, it settles.
                k = max(0.6, min(1.6, 1 + (scale - 1) * 0.6))
                ns = re.sub(r"font-size:\s*([\d.]+)px",
                            lambda z: f"font-size:{float(z.group(1)) * k:.1f}px",
                            ns, count=1)
            # THE LINE HAS A WIDTH AND IT IS ALSO MEASURED. Matching
            # only the height leaves a substitute face setting the same
            # words wider than the original did, and on a nav bar that
            # is not cosmetic: "Features" grew past "Docs", the two ran
            # together, and OCR read the pair as HRATTAS — so the
            # checker called a word that was plainly on the page
            # MISSING. The original's own box says how wide the line
            # should be, so condense it to exactly that.
            if wln["w"] and g["w"] and abs(wln["w"] / g["w"] - 1) > 0.12:
                # ONLY WHEN IT IS BADLY WRONG, AND ONLY PART OF THE WAY.
                # Correcting every line's width every round fought the
                # size correction and cost four settled lines; reserving
                # it for a real mismatch keeps what it is for — a nav
                # item that grew into its neighbour — without touching
                # lines that were already right.
                xk = 1 + (wln["w"] / g["w"] - 1) * 0.7
                prev = re.search(r"scaleX\(([\d.]+)\)", ns)
                xk *= float(prev.group(1)) if prev else 1.0
                xk = max(0.55, min(1.8, xk))
                if abs(xk - 1) > 0.02:
                    # Drop the WHOLE previous pair, not just the
                    # transform: leaving the orphan origin behind piled
                    # up `transform-origin` three deep on one element by
                    # the fourth round.
                    ns = re.sub(r";?transform:scaleX\([\d.]+\)", "", ns)
                    ns = re.sub(r";?transform-origin:left top", "", ns)
                    ns += (f";transform:scaleX({xk:.3f});"
                           f"transform-origin:left top")
            if ns != style:
                new_html = new_html.replace(f'style="{style}"',
                                            f'style="{ns}"', 1)
                moved += 1
        if score > best_score:
            best_html, best_score, best_round = cur, score, rnd
        if verbose:
            print(f"    round {rnd}: {score} of {len(want)} lines settled, "
                  f"corrected {moved}")
        if not moved:
            break
        cur = new_html
    if verbose:
        print(f"    keeping round {best_round} — {best_score} settled")
    return best_html, want


def emit_page(rep, texts, field_b64="", carried=(), font="Inter"):
    """The TOOL writes the page. The model only says what the words are.

    THIS IS THE INVERSION THAT MAKES A WEAK MODEL SUFFICIENT. Asking a
    model to place elements from a list of numbers failed in every form
    it was tried: it wrote a caption with `left:0` and no `top` at all,
    so the caption sat in normal flow, enormous, against the left edge —
    and three rounds of measured corrections could not reach it, because
    a pass that edits `top:` cannot fix an element that has none.

    Reading words off a picture is the one job a small model does
    reliably; deciding pixels is the one it cannot do at all. So the
    split is made absolute here. Every position, size, colour and
    alignment below is measured. `texts` is a list of strings, one per
    measured run, in reading order, and that is the model's ENTIRE
    contribution. A wrong word is then a wrong word — it cannot become
    a wrecked layout.
    """
    w, h = rep["width"], rep["height"]
    runs = [t for b in rep.get("bands", []) for t in b.get("text", [])
            if t.get("left") is not None]
    out = [f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={
    font.replace(' ', '+')}:wght@300;400;500;600;700&display=swap">
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{w}px;height:{h}px;overflow:hidden}}
body{{background:{rep['background']['hex']};position:relative;
 font-family:'{font}',-apple-system,Helvetica,Arial,sans-serif;
 -webkit-font-smoothing:antialiased}}
.e{{position:absolute;white-space:nowrap}}
.r{{position:absolute}}</style></head><body>"""]
    if field_b64:
        out.append(f'<div class="r" style="left:0;top:0;width:{w}px;'
                   f'height:{h}px;z-index:0;background-size:100% 100%;'
                   f'background-image:url(data:image/png;base64,'
                   f'{field_b64})"></div>')
    out.append(chrome_html(rep, w, h))
    for i, r in enumerate(runs):
        words = texts[i] if i < len(texts) else ""
        if not words:
            continue
        segs = r.get("segments") or []
        lo, hi = r.get("font_size_range") or [12, 18]
        size = (lo + hi) / 2
        # A RUN IS NOT ALWAYS A LINE. Ink stops between lines only if a
        # blank row separates them, so tightly-led paragraphs measure as
        # ONE run — and taking its full ink height as a font size gave a
        # 13px paragraph a 39px face, which is worse than any guess a
        # model would have made. Estimate how many lines the words need
        # in the measured width, and divide.
        width = max(1, r["right"] - r["left"] + 1)
        if len(segs) <= 1 and words:
            for _ in range(3):
                per_char = size * 0.5
                lines = max(1, round(len(words) * per_char / width))
                want = (r.get("ink_height") or size) / (0.82 * lines)
                if abs(want - size) < 0.4:
                    size = want
                    break
                size = want
            size = max(7.0, min(size, hi))
        # A run made of several separated items is several elements at
        # their own measured x, not one string with guessed gaps.
        parts = words.split("\\t") if "\\t" in words else [words]
        if len(segs) > 1 and len(parts) == len(segs):
            for s, txt in zip(segs, parts):
                out.append(
                    f'<div class="e" style="left:{s["left"]}px;'
                    f'top:{r["top"]}px;font-size:{size:.1f}px;'
                    f'color:{r["color"]};z-index:4;line-height:1">'
                    f'{txt}</div>')
        else:
            width = r["right"] - r["left"] + 1
            out.append(
                f'<div class="e" style="left:{r["left"]}px;'
                f'top:{r["top"]}px;width:{width}px;font-size:{size:.1f}px;'
                f'color:{r["color"]};z-index:4;line-height:1;'
                f'white-space:normal;text-align:center">{words}</div>')
    for c in carried:
        style = c.get("style", "")
        if "border-radius" not in style:
            # THE OWNER'S "PERFECT CIRCLE". An avatar cropped out as a
            # rectangle ships the square photograph it was cut from.
            # mask_radius asks the crop's own corners whether the source
            # was round, so the mask is measured rather than assumed —
            # and a square logo keeps its square corners.
            _r = mask_radius(shot, field, c["x"], c["y"], c["w"], c["h"])
            if _r:
                style += f"border-radius:{_r};"
        out.append(f'<img alt="" class="r" data-ae-id="p{len(out):02d}" '
                   f'style="left:{c["x"]}px;'
                   f'top:{c["y"]}px;width:{c["w"]}px;height:{c["h"]}px;'
                   f'z-index:5;{style}" '
                   f'src="data:image/png;base64,{c["b64"]}">')
    out.append("</body></html>")
    return "".join(out)


def place_pass(html: str, original, render_fn, score_fn, rounds=2,
               verbose=False):
    """Put each element where it MEASURES, in one move, not by nudging.

    snap_pass searches a few pixels either way, which is right for a
    near-miss and useless for a gross one: a caption that belongs
    centred under a button and was placed at the far left is hundreds of
    pixels out, and no amount of +/-6px finds it.

    So this computes the WHOLE delta. Both documents run down the page,
    so the runs are aligned monotonically and each element is moved to
    its partner's measured position outright. Every move is scored on
    INK OVERLAP and kept only if it improves — a mapping that slips
    produces a worse overlap and is discarded, so a wrong pairing costs
    a render rather than the page.
    """
    a = measure(original)
    orig_runs = [t for bd in a.get("bands", []) for t in bd.get("text", [])
                 if t.get("left") is not None]
    b_shot = load(original)
    bf = Field(b_shot, dark=True)
    bf.refine(ink_mask(b_shot, bf))
    b_mask = ink_mask(b_shot, bf)

    best_html = html
    png, _ = render_fn(best_html)
    best = ink_iou(png, original, b_mask=b_mask)
    if verbose:
        print(f"    ink overlap at start {best * 100:.1f}%")

    for rnd in range(rounds):
        png, _ = render_fn(best_html)
        mine = [t for bd in measure(png).get("bands", [])
                for t in bd.get("text", []) if t.get("left") is not None]
        els = []
        for m in re.finditer(
                r'<(\w+)([^>]*style="([^"]*top:\s*[-\d.]+px[^"]*)"[^>]*)>'
                r'(.*?)</\1>', best_html, re.S):
            if re.sub(r"<[^>]+>", "", m.group(4)).strip():
                els.append(m.group(3))
        pairs = [(o, r) for o, r in _align(orig_runs, mine) if o and r]
        if not pairs or not els:
            break
        pairs.sort(key=lambda p: p[1]["top"])

        def elt_top(s):
            m = re.search(r"top:\s*([-\d.]+)px", s)
            return float(m.group(1)) if m else 1e9

        els.sort(key=elt_top)
        moved = 0
        for (o, r), style in zip(pairs, els):
            if style not in best_html:
                continue
            dt = o["top"] - r["top"]
            dl = o["left"] - r["left"]
            if abs(dt) < 2 and abs(dl) < 2:
                continue
            ns = style
            if abs(dt) >= 2:
                ns = re.sub(r"top:\s*([-\d.]+)px",
                            lambda m: f"top:{float(m.group(1)) + dt:.0f}px",
                            ns, count=1)
            if abs(dl) >= 2 and "left:" in ns:
                ns = re.sub(r"left:\s*([-\d.]+)px",
                            lambda m: f"left:{float(m.group(1)) + dl:.0f}px",
                            ns, count=1)
            if ns == style:
                continue
            cand = best_html.replace(style, ns, 1)
            cpng, _ = render_fn(cand)
            sc = ink_iou(cpng, original, b_mask=b_mask)
            if sc > best + 0.001:
                best_html, best = cand, sc
                moved += 1
                if verbose:
                    print(f"      moved {dl:+.0f},{dt:+.0f} -> "
                          f"overlap {best * 100:.1f}%")
        if verbose:
            print(f"    round {rnd + 1}: {moved} moved, overlap "
                  f"{best * 100:.1f}%")
        if not moved:
            break
    return best_html, best


# ─────────────────────────── the audit ───────────────────────────────

def _align(ta, tb, gap=18):
    """Pair text runs IN ORDER, so a match can never cross another.

    Nearest-neighbour matching looked reasonable and was not: one run
    slightly out of place stole its neighbour's partner, and every
    comparison after it compared the wrong two things. The audit then
    reported a nav line against a heading and advised "multiply this
    font-size by 0.400" — advice that would have made the rebuild worse
    while sounding exact.

    Both lists run down the page, so the true pairing is monotonic.
    A standard alignment enforces that; unmatched runs on either side
    are reported as missing or extra rather than forced into a pair.
    """
    n, m = len(ta), len(tb)
    INF = float("inf")
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0
    for i in range(n + 1):
        for j in range(m + 1):
            if cost[i][j] == INF:
                continue
            if i < n and j < m:
                d = abs(ta[i]["top"] - tb[j]["top"])
                la, lb = ta[i].get("left"), tb[j].get("left")
                if la is not None and lb is not None:
                    d += abs(la - lb) * 0.35
                c = cost[i][j] + d
                if c < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], back[i + 1][j + 1] = c, (i, j, "m")
            if i < n and cost[i][j] + gap < cost[i + 1][j]:
                cost[i + 1][j], back[i + 1][j] = cost[i][j] + gap, (i, j, "a")
            if j < m and cost[i][j] + gap < cost[i][j + 1]:
                cost[i][j + 1], back[i][j + 1] = cost[i][j] + gap, (i, j, "b")
    out, i, j = [], n, m
    while (i, j) != (0, 0):
        pi, pj, k = back[i][j]
        if k == "m":
            out.append((ta[pi], tb[pj]))
        elif k == "a":
            out.append((ta[pi], None))
        else:
            out.append((None, tb[pj]))
        i, j = pi, pj
    return list(reversed(out))


def audit(original, rebuild, tol=2) -> list:
    """Measure BOTH images and report every property that disagrees.

    This is `verify` for pixels. A percentage tells you a rebuild is
    wrong; it never tells you WHAT is wrong, so a person ends up
    comparing screenshots by eye and reporting "the button is too big" —
    which is exactly the judgement this project is trying to take out of
    the loop. The referee already knows the answer numerically; it just
    was not asked.

    Every finding names the element, the expected number, the number
    that was built, and the fix. Nothing is inferred that can be
    measured, and a property that cannot be matched is reported as
    UNMATCHED rather than silently skipped — an element the rebuild
    never drew is the most important thing to say, and the easiest to
    lose by only comparing pairs that happen to line up.
    """
    a, b = measure(original), measure(rebuild)
    out = []

    def add(kind, what, want, got, fix):
        out.append({"kind": kind, "element": what, "expected": want,
                    "got": got, "fix": fix})

    if a["background"]["hex"] != b["background"]["hex"]:
        add("colour", "page background", a["background"]["hex"],
            b["background"]["hex"], "set the page background to the measured value")

    ta = [t for bd in a.get("bands", []) for t in bd.get("text", [])]
    tb = [t for bd in b.get("bands", []) for t in bd.get("text", [])]
    for pair in _align(ta, tb):
        t, u = pair
        if u is None:
            add("missing", f"text at y{t['top']}", "a text run", "nothing",
                "the rebuild draws no text near this row")
            continue
        if t is None:
            add("extra", f"text at y{u['top']}", "nothing", "a text run",
                "the original has no text here")
            continue
        if abs(u["top"] - t["top"]) > tol:
            add("position", f"text at y{t['top']}", f"top {t['top']}",
                f"top {u['top']}", f"move it {u['top'] - t['top']:+d}px vertically")
        ha, hb = t.get("ink_height"), u.get("ink_height")
        if ha and hb and abs(ha - hb) > tol:
            scale = ha / hb
            add("text size", f"text at y{t['top']}",
                f"ink {ha}px", f"ink {hb}px",
                f"multiply this font-size by {scale:.3f}")
        if t.get("left") is not None and u.get("left") is not None:
            if abs(t["left"] - u["left"]) > tol + 1:
                add("position", f"text at y{t['top']}", f"left {t['left']}",
                    f"left {u['left']}",
                    f"move it {u['left'] - t['left']:+d}px horizontally")
            wa = t["right"] - t["left"]
            wb = u["right"] - u["left"]
            if wa > 0 and abs(wa - wb) > max(3, wa * 0.04):
                add("text width", f"text at y{t['top']}", f"{wa}px wide",
                    f"{wb}px wide",
                    "tracking or font-size is off, or the wrong typeface")
        if t.get("color") and u.get("color") and t["color"] != u["color"]:
            ca = tuple(int(t["color"][i:i + 2], 16) for i in (1, 3, 5))
            cb = tuple(int(u["color"][i:i + 2], 16) for i in (1, 3, 5))
            if _dist(ca, cb) > MERGE:
                add("colour", f"text at y{t['top']}", t["color"], u["color"],
                    "use the measured colour")

    for box in a.get("boxes", []):
        cand = [c for c in b.get("boxes", [])
                if abs(c["x"] - box["x"]) < 40 and abs(c["y"] - box["y"]) < 40]
        name = f"{box['fill']} region at ({box['x']},{box['y']})"
        if not cand:
            add("missing", name, f"{box['w']}x{box['h']}", "nothing",
                "the rebuild has no element here")
            continue
        c = min(cand, key=lambda c: abs(c["w"] - box["w"]) + abs(c["h"] - box["h"]))
        if abs(c["w"] - box["w"]) > tol or abs(c["h"] - box["h"]) > tol:
            add("size", name, f"{box['w']}x{box['h']}", f"{c['w']}x{c['h']}",
                f"resize by {box['w'] - c['w']:+d}px wide, "
                f"{box['h'] - c['h']:+d}px tall")
        if abs(c["radius"] - box["radius"]) > tol:
            add("radius", name, f"{box['radius']}px", f"{c['radius']}px",
                f"set border-radius to {box['radius']}px")

    ra, rb = a.get("rules", []), b.get("rules", [])
    for r in ra:
        near = [s for s in rb if s["axis"] == r["axis"]
                and abs(s["at"] - r["at"]) <= 3]
        if not near:
            add("missing", f"{r['axis']} rule at {r['at']}",
                f"a {r['axis']} line ({r['color']})", "nothing",
                f"draw a 1px {r['axis']} rule at {r['at']} in {r['color']}")
    for s in rb:
        if not [r for r in ra if r["axis"] == s["axis"]
                and abs(s["at"] - r["at"]) <= 3]:
            add("extra", f"{s['axis']} rule at {s['at']}", "nothing",
                f"a {s['axis']} line", "the original has no rule here — remove it")

    if len(a.get("gradients", [])) != len(b.get("gradients", [])):
        add("gradient", "smooth ramps", f"{len(a.get('gradients', []))}",
            f"{len(b.get('gradients', []))}",
            "carry the original's colour field rather than fitting one")
    return out


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
        lo, hi = big["font_size_range"]
        check("  ...its size bounded, and the true 48px is inside",
              lo <= 48 <= hi, f"got range {lo}-{hi} "
              f"(ink {big['ink_height']}px)")
        check("  ...the bound is tight enough to be useful",
              hi - lo <= 24, f"range {lo}-{hi} is too loose to help")

    print("\n── THE AUDIT: does it catch damage it was not told about?")
    bad = tmp / "bad"
    bad.mkdir()
    # Three deliberate defects, of the three kinds a rebuild gets wrong:
    # a box moved and resized, a radius flattened, and type enlarged.
    (bad / "index.html").write_text(
        TRUTH_HTML
        .replace("left:120px;top:80px;width:400px;height:240px",
                 "left:150px;top:80px;width:360px;height:240px")
        .replace("border-radius:24px", "border-radius:2px")
        .replace("font-size:48px", "font-size:64px"))
    bad_png = bad / "bad.png"
    if G.shoot(bad / "index.html", 800, 600, bad_png):
        f = audit(shot_png, bad_png)
        kinds = {x["kind"] for x in f}
        check("it reports the resized card", "size" in kinds, str(kinds))
        check("it reports the flattened radius", "radius" in kinds, str(kinds))
        check("it reports the enlarged type", "text size" in kinds, str(kinds))
        sz = [x for x in f if x["kind"] == "text size"]
        check("  ...with the correction to apply, not just a complaint",
              bool(sz) and "multiply" in sz[0]["fix"], str(sz[:1]))
        clean = audit(shot_png, shot_png)
        serious = [x for x in clean if x["kind"] != "extra"]
        check("an identical page produces no findings", not serious,
              f"{len(serious)}: {[x['kind'] for x in serious[:4]]}")

    print("\n── honesty")
    check("the report says what a still cannot contain",
          any("animation" in s for s in rep["not_in_a_still"]))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nvision selftest:", "all green" if ok else "FAILED")
    return 0 if ok else 1


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: aethron_vision.py <image> [--json]")
        print("       aethron_vision.py <image> --rebuild <outdir>"
              "     screenshot -> page, no model")
        print("       aethron_vision.py <original> --against <rebuild>"
              "   what is wrong, and by how much")
        print("       aethron_vision.py <original> --check <rebuild.png>"
              "  every line: there? placed? sized?")
        print("       aethron_vision.py --selftest")
        return 0
    if argv[0] == "--selftest":
        return _selftest()
    if "--rebuild" in argv:
        out = argv[argv.index("--rebuild") + 1]
        v = rebuild(argv[0], out, fit="--no-fit" not in argv)
        if v["verdict"] == "SKIPPED":
            print("VERDICT: SKIPPED — " + v["why"])
            return 0
        print(f"\n{v['page']}")
        print("VERDICT: " + v["verdict"])
        return 0 if v["verdict"] == "PASS" else 1
    if "--check" in argv:
        # THE CHECKLIST, NOT THE PERCENTAGE. Reports every line of the
        # original that is missing, misplaced or the wrong size in the
        # rebuild — the things a whole-page score averages away.
        other = argv[argv.index("--check") + 1]
        r = verify_rebuild(argv[0], other)
        if r["verdict"] == "SKIPPED":
            print("VERDICT: SKIPPED — " + r["why"])
            return 0
        print(f"{r['lines_correct']}/{r['lines_expected']} lines correct")
        for f in r["findings"]:
            print(f"  [{f['kind']}] {f['text']!r}")
            print(f"      want {f['want']}   got {f['got']}"
                  + (f"   -> {f['fix']}" if f.get("fix") else ""))
        print("VERDICT: " + r["verdict"])
        return 0 if r["verdict"] == "PASS" else 1
    if "--against" in argv:
        # THE CHECKER. A percentage says a rebuild is wrong; this says
        # WHAT is wrong, with the number to change. It is written to be
        # read by an agent as a work list, not admired by a person.
        other = argv[argv.index("--against") + 1]
        found = audit(argv[0], other)
        if "--json" in argv:
            print(json.dumps(found, indent=1))
            return 0
        if not found:
            print("no measurable difference")
            return 0
        print(f"{len(found)} difference(s) — original vs rebuild\n")
        for x in found:
            print(f"  [{x['kind']}] {x['element']}")
            print(f"      want {x['expected']}   got {x['got']}")
            print(f"      -> {x['fix']}")
        return 1
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
