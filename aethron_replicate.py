#!/usr/bin/env python3
"""Replicate a screenshot as a working web page. Every value is measured; none is typed in.

    python3 aethron_replicate.py <screenshot> <outdir> [--fast]

WHY THIS FILE EXISTS. On 2026-09-14 a rebuild of the owner's "Build Apps People
Love" screenshot came out at 91.6% and looked close, and the owner asked the only
question that matters: did Aethron do that? It had not. Aethron's instruments had
measured, and the operator had DRIVEN — typing the wave points, the chip boxes, the
card's box, the button's gradient and the logo's rings into a one-off script from
numbers the instruments printed. A test of whether the product can do something, run
by hand, answers nothing. Every one of those hand steps is here as code. Nothing in
this file names a screenshot, a word on one, or a coordinate on one; the constants are
tolerances, and the report says so.

WHAT IT BUILDS, IN ORDER
  lines       the words and their boxes (on-device OCR)
  surfaces    cards, chips and buttons, found from the words sitting on them; each
              edge placed at its strongest colour step; the corner radius fitted to
              the corner's own profile
  background  a base colour and radial gradients fitted as real CSS
  marks       non-text ink the background does not explain; concentric rings become
              SVG circles, anything else is carried as an image and REPORTED as such
  strokes     thin curved lines, traced column by column and through their crossings
  rules       faint straight lines, with the fade measured along their length
  fills       each surface as flat, a linear gradient, or glass over what is behind
              it — whichever the pixels fit; borders and top highlights measured
  type        the typeface chosen by ink overlap, candidates rendered side by side;
              every line placed by reading both pages; widths and colours matched
  shadows     swept against the pixels around each card
  semantics   links, a heading, a text box, buttons and a form — then the check that
              turning the drawing into controls moved no pixels
  grade       whole page, background, a checklist by words, a difference map
"""
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aethron_vision as V          # noqa: E402
import aethron_gradient as G        # noqa: E402
import aethron_surface as SF        # noqa: E402
import aethron_figma_grade as GR    # noqa: E402

FAMILIES = ["Inter", "Geist", "Manrope", "DM Sans", "Figtree", "Plus Jakarta Sans",
            "Outfit", "Sora", "Onest", "Schibsted Grotesk", "Urbanist", "Lexend",
            "Poppins", "Albert Sans", "Instrument Sans", "Hanken Grotesk",
            "Red Hat Display", "Be Vietnam Pro", "Rethink Sans", "Wix Madefor Display",
            "Space Grotesk", "Montserrat", "Roboto", "Work Sans"]
TOL = 12


# ───────────────────────────── small things ─────────────────────────────

def say(*a):
    print(*a, flush=True)


def lum(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def dist(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def med(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0


def med_rgb(px):
    return tuple(med([p[i] for p in px]) for i in range(3)) if px else (0, 0, 0)


def hexc(c):
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(v)))) for v in c[:3])


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def iou(a, b):
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0.0


def contains(o, i, slack=4):
    return (i["x"] >= o["x"] - slack and i["y"] >= o["y"] - slack
            and i["x"] + i["w"] <= o["x"] + o["w"] + slack
            and i["y"] + i["h"] <= o["y"] + o["h"] + slack)


def centre_in(l, b, slack=2):
    cx, cy = l["x"] + l["w"] / 2, l["y"] + l["h"] / 2
    return (b["x"] - slack <= cx <= b["x"] + b["w"] + slack
            and b["y"] - slack <= cy <= b["y"] + b["h"] + slack)


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "item"


class Img:
    def __init__(self, path):
        self.path = Path(path)
        self.shot = V.load(self.path)
        self.w, self.h = self.shot.w, self.shot.h

    def px(self, x, y):
        return self.shot.rgb(min(self.w - 1, max(0, int(x))),
                             min(self.h - 1, max(0, int(y))))


class Blocks:
    def __init__(self, boxes):
        self.boxes = [tuple(int(round(v)) for v in b) for b in boxes]

    def hit(self, x, y):
        for bx, by, bw, bh in self.boxes:
            if bx <= x < bx + bw and by <= y < by + bh:
                return True
        return False

    def across(self, u, vertical=True):
        if vertical:
            return [(by, by + bh) for bx, by, bw, bh in self.boxes if bx <= u < bx + bw]
        return [(bx, bx + bw) for bx, by, bw, bh in self.boxes if by <= u < by + bh]


class Renderer:
    def __init__(self, out, w, h):
        self.out, self.w, self.h, self.n = out, w, h, 0

    def page(self, html, name, w=None, h=None):
        p = self.out / f"_w_{name}.html"
        p.write_text(html)
        png = p.with_suffix(".png")
        GR.shoot(p, w or self.w, h or self.h, png)
        self.n += 1
        return png

    def clean(self):
        for f in self.out.glob("_w_*"):
            f.unlink()


def region_mae(img, png, rects, step=2):
    b = V.load(png)
    tot = n = 0
    for (x, y, w, h) in rects:
        for yy in range(max(0, int(y)), min(img.h, int(y + h)), step):
            for xx in range(max(0, int(x)), min(img.w, int(x + w)), step):
                tot += dist(img.shot.rgb(xx, yy), b.rgb(xx, yy))
                n += 3
    return tot / max(1, n)


def predict(model, pts):
    cells = [(x, y, 0, 0, 0) for x, y in pts]
    S = G._Cells(cells)
    al = [G._fast_alphas(L, S) for L in model["layers"]]
    return list(zip(*G.composite(model, cells, al)))


# ───────────────────────────── lines and ink ─────────────────────────────

def read_lines(path):
    got = V.ocr(str(path))
    if got is None:
        return None
    out = []
    for l in got:
        t = l["text"].strip()
        if t and l.get("confidence", 1.0) >= 0.5 and l["w"] > 2 and l["h"] > 4:
            out.append({"text": t, "x": int(l["x"]), "y": int(l["y"]),
                        "w": int(l["w"]), "h": int(l["h"])})
    return out


def ring_colour(img, l, pad=4):
    x, y, w, h = l["x"], l["y"], l["w"], l["h"]
    pts = ([(xx, y - pad) for xx in range(x - pad, x + w + pad, 2)]
           + [(xx, y + h + pad) for xx in range(x - pad, x + w + pad, 2)]
           + [(x - pad, yy) for yy in range(y, y + h, 2)]
           + [(x + w + pad, yy) for yy in range(y, y + h, 2)])
    return med_rgb([img.px(*p) for p in pts])


def ink_colour(img, l, ring):
    """The 2% extreme on the side of the ground the type is on. Thin type is mostly
    antialiasing, so a median would report the blend, not the ink."""
    px = sorted((img.px(xx, yy) for yy in range(l["y"], l["y"] + l["h"])
                 for xx in range(l["x"], l["x"] + l["w"])), key=lum)
    if not px:
        return ring, True
    k = max(2, len(px) // 50)
    dark, light = med_rgb(px[:k]), med_rgb(px[-k:])
    if lum(light) - lum(ring) >= lum(ring) - lum(dark):
        return light, True
    return dark, False


# ───────────────────────────── surfaces ─────────────────────────────

def refine_box(img, s):
    x0, y0, w, h = s["x"], s["y"], s["w"], s["h"]
    x1, y1 = x0 + w, y0 + h
    # THE SEARCH MUST NOT REACH THE NEIGHBOUR. Chips sit ten pixels apart; a nine-pixel
    # reach found the next chip's edge, which is a stronger step than a chip's own.
    reach = max(3, min(9, int(min(w, h) * 0.12)))

    def edge(axis, pos, lo, hi, sign):
        found = []
        span = max(1, int((hi - lo) / 14))
        for t in range(int(lo), int(hi) + 1, span):
            best, bp = -1, pos
            for p in range(int(pos) - reach, int(pos) + reach + 1):
                if axis == "x":
                    d = dist(img.px(p, t), img.px(p - 2 * sign, t))
                else:
                    d = dist(img.px(t, p), img.px(t, p - 2 * sign))
                if d > best:
                    best, bp = d, p
            found.append((bp, best))
        return med([f[0] for f in found]), med([f[1] for f in found])

    left, sl = edge("x", x0, y0 + h * .3, y1 - h * .3, +1)
    right, sr = edge("x", x1 - 1, y0 + h * .3, y1 - h * .3, -1)
    top, st = edge("y", y0, x0 + w * .3, x1 - w * .3, +1)
    bottom, sb = edge("y", y1 - 1, x0 + w * .3, x1 - w * .3, -1)
    right, bottom = right + 1, bottom + 1
    # A SURFACE HAS AN OUTLINE. A flood can close on noise in a smooth gradient;
    # what it cannot fake is a real step in colour on three sides or more.
    if sum(v >= 12 for v in (sl, sr, st, sb)) < 3 or right - left < 24 or bottom - top < 16:
        return None
    bw, bh = right - left, bottom - top
    ci = img.px(left + bw / 2, top + 3)
    co = img.px(left - 3, top + bh / 2)
    r = 0
    if dist(ci, co) >= 12:
        R = int(min(bw, bh, 200) / 2)
        prof = []
        for dy in range(0, R):
            yy = top + dy
            xe = next((xx for xx in range(left - 1, left + R + 2)
                       if dist(img.px(xx, yy), ci) < dist(img.px(xx, yy), co)), None)
            if xe is not None:
                prof.append((dy + 0.5, xe - left))

        def err(rr):
            return sum((((rr - math.sqrt(max(0.0, rr * rr - (rr - dy) ** 2))) if dy < rr else 0.0)
                        - off) ** 2 for dy, off in prof)
        if prof:
            r = min(range(0, R + 1), key=err)
    # A CHIP IS A PILL. A corner fitted to within a few pixels of half the height is the
    # designer's "fully round"; a pixel short of it reads as a flattened end.
    if r >= 0.7 * min(bw, bh) / 2:
        r = int(round(min(bw, bh) / 2))
    return {"x": int(left), "y": int(top), "w": int(bw), "h": int(bh), "r": int(r),
            "edge_strength": [sl, sr, st, sb]}


def find_surfaces(img, lines):
    cands = []
    for tol, step in ((16, 3), (8, 2), (5, 2)):
        try:
            cands += SF.surfaces(img.shot, lines, step=step, tol=tol)
        except Exception as e:                      # one pass failing is not the build
            say(f"  surface pass at tolerance {tol} failed: {e}")
    # THE FULLEST READING OF EACH SURFACE WINS. At a loose tolerance a chip floods
    # into the card it sits on; at a tight one a gradient button stops half way.
    chosen = []
    for c in sorted(cands, key=lambda s: -s["w"] * s["h"]):
        key = tuple(sorted(c["lines"]))
        if any(iou(c, k) > 0.5 and tuple(sorted(k["lines"])) == key for k in chosen):
            continue
        if any(iou(c, k) > 0.85 for k in chosen):
            continue
        chosen.append(c)
    out = []
    for c in chosen:
        b = refine_box(img, c)
        if b is None:
            continue
        if b["x"] < 16 or b["y"] < 16 or b["x"] + b["w"] > img.w - 16 or b["y"] + b["h"] > img.h - 16:
            continue                                # the screenshot's own frame
        if any(iou(b, o) > 0.8 for o in out):
            continue
        out.append(b)
    for i, s in enumerate(out):
        s["id"] = i
    for s in out:
        par = [p for p in out if p is not s and contains(p, s)
               and p["w"] * p["h"] > s["w"] * s["h"] * 1.5]
        s["parent"] = min(par, key=lambda p: p["w"] * p["h"])["id"] if par else None
    for s in out:
        d, p = 0, s["parent"]
        while p is not None:
            d, p = d + 1, out[p]["parent"]
        s["depth"] = d
    return out


# ───────────────────────────── background ─────────────────────────────

def fit_background(img, lines, surfaces, out, fast):
    ex = [(l["x"] - 7, l["y"] - 7, l["w"] + 14, l["h"] + 14) for l in lines]
    for s in surfaces:
        if s["parent"] is not None:
            continue
        if s["w"] * s["h"] >= 20000:                # a card casts a shadow, and it falls DOWN
            ex.append((s["x"] - 50, s["y"] - 30, s["w"] + 100, s["h"] + 150))
        else:
            ex.append((s["x"] - 12, s["y"] - 12, s["w"] + 24, s["h"] + 24))
    # keyed on the image itself: a surface refined by a pixel moves the mask by a pixel,
    # which is no reason to spend five minutes fitting the same sky again
    key = hashlib.sha1(img.path.read_bytes() + json.dumps([fast, "two-starts"]).encode()).hexdigest()[:16]
    cache = out / "background.json"
    if cache.is_file():
        d = json.loads(cache.read_text())
        if d.get("key") == key:
            say("  background: reusing the fit already made for this exact screenshot")
            return d
    # TWO STARTS, ONE JUDGE. The fit settles where its first layers lead it: on the same
    # screenshot one run reached rms 2.70 and another 3.37, and the worse one painted a wrong
    # band straight through the hero. Two different starting grids, scored on one common
    # sample (each fit's own error is over its own grid), and the better one is kept.
    fits = []
    for k, step in (((5, 20),) if fast else ((7, 16), (8, 13))):
        r = G.fit(img.shot, exclude=ex, k=k, step=step, verbose=True, budget_s=120 if fast else 300)
        if r.get("verdict") == "FITTED":
            fits.append(r)
    if not fits:
        return None
    common = G.sample(img.shot, ex, step=11)
    scored = [(math.sqrt(G.error(r["model"], common) / 3), r) for r in fits]
    say("  background starts scored on one sample: " + ", ".join(f"rms {e:.2f}" for e, _r in scored))
    e, r = min(scored, key=lambda t: t[0])
    d = {"key": key, "model": r["model"], "exclude": ex, "rms": e}
    cache.write_text(json.dumps(d))
    return d


# ───────────────────────────── marks ─────────────────────────────

def find_marks(img, model, lines, surfaces, out):
    step = 2
    B = Blocks([(l["x"] - 4, l["y"] - 4, l["w"] + 8, l["h"] + 8) for l in lines]
               + [(s["x"] - 8, s["y"] - 8, s["w"] + 16, s["h"] + 48) for s in surfaces])
    gw, gh = img.w // step, img.h // step
    pts = [(gx * step, gy * step) for gy in range(gh) for gx in range(gw)]
    pred = predict(model, pts)
    hot = bytearray(gw * gh)
    for i, (x, y) in enumerate(pts):
        if x < 24 or y < 24 or x > img.w - 24 or y > img.h - 24:
            continue
        if dist(img.shot.rgb(x, y), pred[i]) > 60 and not B.hit(x, y):
            hot[i] = 1
    seen = bytearray(gw * gh)
    marks = []
    # a previous run's carried marks are not this run's; stale files would ship unreferenced
    for old in (out / "assets").glob("mark*.png") if (out / "assets").is_dir() else ():
        old.unlink()
    for i in range(gw * gh):
        if not hot[i] or seen[i]:
            continue
        stack, comp = [i], []
        seen[i] = 1
        while stack:
            j = stack.pop()
            comp.append(j)
            jx, jy = j % gw, j // gw
            for nx, ny in ((jx + 1, jy), (jx - 1, jy), (jx, jy + 1), (jx, jy - 1)):
                if 0 <= nx < gw and 0 <= ny < gh:
                    k = ny * gw + nx
                    if hot[k] and not seen[k]:
                        seen[k] = 1
                        stack.append(k)
        xs = [j % gw for j in comp]
        ys = [j // gw for j in comp]
        bx, by = min(xs) * step, min(ys) * step
        bw, bh = (max(xs) - min(xs) + 1) * step, (max(ys) - min(ys) + 1) * step
        if len(comp) < 8 or max(bw, bh) > 180 or min(bw, bh) < 10:
            continue
        # A MARK STANDS OUT FROM ITS GROUND. Where the fitted sky is merely a little off, the
        # residual is a faint blob, and "fitting rings" to it only paints the sky over again
        # under a false name. A logo on its ground differs by hundreds of levels, not tens.
        contrast = med([dist(img.shot.rgb(j % gw * step, j // gw * step), pred[j]) for j in comp])
        if contrast < 120:
            continue
        if max(bw, bh) / max(1, min(bw, bh)) > 3:
            continue                                # a stroke, not a mark
        box = (bx, by, bw, bh)
        rings = fit_rings(img, model, box)
        if rings:
            marks.append({"box": box, "kind": "rings", "rings": rings,
                          "measured_from": "ink the background does not explain, "
                          "fitted as concentric rings"})
        else:
            name = f"assets/mark{len(marks)}.png"
            (out / "assets").mkdir(exist_ok=True)
            rgba = bytearray()
            for yy in range(by - 3, by + bh + 3):
                for xx in range(bx - 3, bx + bw + 3):
                    rgba += bytes(img.px(xx, yy)) + b"\xff"
            GR.write_png(out / name, bw + 6, bh + 6, bytes(rgba))
            marks.append({"box": (bx - 3, by - 3, bw + 6, bh + 6), "kind": "raster", "src": name,
                          "measured_from": "ink no shape model explains — CARRIED AS AN IMAGE"})
    return marks


def fit_rings(img, model, box, explain=0.85, diag=None):
    """Concentric rings fitted to the mark by DRAWING them and comparing.

    A radial histogram finds where rings might be, and nothing more: on the owner's logo a
    thin grey inner ring sits on the shoulder of a bright outer one, the histogram shows one
    lump, and reading widths off it drew one fat ring where there were two thin ones. So the
    peaks only seed the rings. Each ring is drawn with antialiased coverage, its colour is
    SOLVED exactly for that geometry, and radius, width and centre are moved while the
    drawing gets closer to the pixels. The rings are accepted only if they explain most of
    what the background does not; anything else is carried as an image and said so.
    """
    x0, y0, w, h = box
    pts = [(x + 0.5, y + 0.5) for y in range(y0 - 4, y0 + h + 4) for x in range(x0 - 4, x0 + w + 4)]
    under = predict(model, [(int(x), int(y)) for x, y in pts])
    obs = [img.px(int(x), int(y)) for x, y in pts]
    res = [dist(o, u) for o, u in zip(obs, under)]
    hot = [(p, r) for p, r in zip(pts, res) if r > 45]
    if len(hot) < 20:
        return None
    tw = sum(r for _p, r in hot)
    cx = sum(p[0] * r for p, r in hot) / tw
    cy = sum(p[1] * r for p, r in hot) / tw
    base = sum((u[k] - o[k]) ** 2 for u, o in zip(under, obs) for k in range(3))
    if base <= 0:
        return None
    hb = {}
    for (x, y), r in hot:
        bi = int(math.hypot(x - cx, y - cy) * 2)
        hb[bi] = hb.get(bi, 0) + r
    n = max(hb) + 2
    sm = [sum(hb.get(bi + d, 0) for d in (-1, 0, 1)) / 3 for bi in range(n)]
    floor = med([v for v in sm if v > 0])
    lift = [max(0.0, v - floor) for v in sm]
    top = max(lift) or 1.0
    seeds = []
    for bi in sorted(range(1, n - 1), key=lambda k: -lift[k]):
        if lift[bi] < 0.15 * top or len(seeds) >= 4:
            break
        if all(abs(bi - q) >= 4 for q in seeds):
            seeds.append(bi)
    if not seeds:
        return None

    def solve(rings, ccx, ccy):
        covs = [[max(0.0, min(1.0, rg["w"] / 2 + 0.5 - abs(math.hypot(x - ccx, y - ccy) - rg["r"])))
                 for x, y in pts] for rg in rings]
        cols = []
        for cov in covs:
            num, den = [0.0, 0.0, 0.0], 0.0
            for j, c in enumerate(cov):
                if c:
                    for k in range(3):
                        num[k] += c * (obs[j][k] - under[j][k] * (1 - c))
                    den += c * c
            cols.append(tuple(max(0.0, min(255.0, v / den)) for v in num) if den else (128.0, 128.0, 128.0))
        err = 0.0
        for j in range(len(pts)):
            m = under[j]
            for cov, col in zip(covs, cols):
                c = cov[j]
                if c:
                    m = tuple(m[k] * (1 - c) + col[k] * c for k in range(3))
            err += sum((m[k] - obs[j][k]) ** 2 for k in range(3))
        return err, cols
    def descend(rings, ccx, ccy):
        err, cols = solve(rings, ccx, ccy)
        step = 1.0
        for _sweep in range(10):
            moved = False
            for key in ["cx", "cy"] + [(i, f) for i in range(len(rings)) for f in ("r", "w")]:
                for sgn in (1, -1):
                    t_cx, t_cy, t_r = ccx, ccy, [dict(rg) for rg in rings]
                    if key == "cx":
                        t_cx += sgn * step * 0.5
                    elif key == "cy":
                        t_cy += sgn * step * 0.5
                    else:
                        i, f = key
                        t_r[i][f] = max(0.6 if f == "w" else 1.0, t_r[i][f] + sgn * step)
                        # a ring is a line bent round; a band as wide as its radius is a disc
                        t_r[i]["w"] = min(t_r[i]["w"], max(1.5, 0.45 * t_r[i]["r"]))
                    e, c = solve(t_r, t_cx, t_cy)
                    if e < err - 1e-6:
                        err, cols, ccx, ccy, rings = e, c, t_cx, t_cy, t_r
                        moved = True
                        break
            if not moved:
                step /= 2
                if step < 0.12:
                    break
        return err, cols, rings, ccx, ccy

    def drawn(rings, cols, ccx, ccy, j):
        x, y = pts[j]
        m = under[j]
        for rg, col in zip(rings, cols):
            c = max(0.0, min(1.0, rg["w"] / 2 + 0.5 - abs(math.hypot(x - ccx, y - ccy) - rg["r"])))
            if c:
                m = tuple(m[k] * (1 - c) + col[k] * c for k in range(3))
        return m
    # ONE RING AT A TIME, EACH WHERE THE DRAWING IS STILL MOST WRONG. A weak ring beside a
    # strong one never shows as its own peak; it shows as the error left once the strong
    # one is drawn.
    err, cols, rings, cx, cy = descend([{"r": seeds[0] / 2, "w": 2.0}], cx, cy)
    for _add in range(3):
        eb = {}
        for j, (x, y) in enumerate(pts):
            m = drawn(rings, cols, cx, cy, j)
            bi = int(math.hypot(x - cx, y - cy) * 2)
            eb[bi] = eb.get(bi, 0) + sum((m[k] - obs[j][k]) ** 2 for k in range(3))
        free = [bi for bi in eb if bi >= 2 and all(abs(bi / 2 - rg["r"]) > 2 for rg in rings)]
        if not free:
            break
        bi = max(free, key=eb.get)
        e2, c2, r2, cx2, cy2 = descend(rings + [{"r": bi / 2, "w": 1.5}], cx, cy)
        if e2 >= err * 0.95:
            break
        err, cols, rings, cx, cy = e2, c2, r2, cx2, cy2
    # A RING THAT EXPLAINS ALMOST NOTHING IS NOT DRAWN
    changed = True
    while changed and len(rings) > 1:
        changed = False
        for i in range(len(rings)):
            e, c = solve(rings[:i] + rings[i + 1:], cx, cy)
            if e <= err * 1.03:
                rings, err, cols, changed = rings[:i] + rings[i + 1:], e, c, True
                break
    explained = 1 - err / base
    if explained < explain:
        if diag is not None:
            diag.update({"explained": round(explained, 3), "rings": [(round(rg["r"], 1), round(rg["w"], 1)) for rg in rings]})
        return None
    left = []
    for j in range(len(pts)):
        if res[j] <= 45:
            continue
        m = drawn(rings, cols, cx, cy, j)
        left.append(sum(abs(m[k] - obs[j][k]) for k in range(3)) / 3)
    if diag is not None:
        diag.update({"explained": round(explained, 3), "left": round(sum(left) / max(1, len(left)), 1),
                     "rings": [(round(rg["r"], 1), round(rg["w"], 1), hexc(c)) for rg, c in zip(rings, cols)]})
    # A PHOTOGRAPH ALSO "EXPLAINS" AS RINGS: its average colour does most of the work. What
    # rings leave behind on a drawn mark is antialiasing; on a photograph it is the picture.
    if left and sum(left) / len(left) > 14:
        return None
    return {"cx": round(cx - 0.0, 2), "cy": round(cy - 0.0, 2), "fit": round(explained, 3),
            "rings": [{"r": round(rg["r"], 2), "width": round(rg["w"], 2), "colour": hexc(col)}
                      for rg, col in zip(rings, cols)]}


# ───────────────────────────── strokes ─────────────────────────────

def _extrap(P, x):
    P = P[-8:]
    if len(P) == 1:
        return P[0][1]
    n = len(P)
    mx = sum(p[0] for p in P) / n
    my = sum(p[1] for p in P) / n
    sxx = sum((p[0] - mx) ** 2 for p in P)
    if sxx == 0:
        return my
    return my + sum((p[0] - mx) * (p[1] - my) for p in P) / sxx * (x - mx)


def find_strokes(img, lines, surfaces, marks):
    boxes = ([(l["x"] - 6, l["y"] - 6, l["w"] + 12, l["h"] + 12) for l in lines]
             + [(s["x"] - 6, s["y"] - 6, s["w"] + 12, s["h"] + 12) for s in surfaces]
             + [(m["box"][0] - 6, m["box"][1] - 6, m["box"][2] + 12, m["box"][3] + 12) for m in marks])
    B = Blocks(boxes)
    step = 4
    cols = {}
    for x in range(12, img.w - 12, step):
        blk = B.across(x - 1) + B.across(x) + B.across(x + 1)
        prof = [(lum(img.px(x - 1, y)) + lum(img.px(x, y)) + lum(img.px(x + 1, y))) / 3
                for y in range(img.h)]
        pts = []
        for y in range(14, img.h - 14):
            if any(a - 4 <= y < b + 4 for a, b in blk):
                continue
            c = prof[y]
            if c <= prof[y - 1] and c <= prof[y + 1]:
                d = min(prof[y - 4], prof[y + 4]) - c
                if d >= 3:
                    pts.append((y, d, -1))
            elif c >= prof[y - 1] and c >= prof[y + 1]:
                d = c - max(prof[y - 4], prof[y + 4])
                if d >= 3:
                    pts.append((y, d, 1))
        merged = []
        for p in pts:
            if merged and p[0] - merged[-1][0] <= 2 and p[2] == merged[-1][2]:
                if p[1] > merged[-1][1]:
                    merged[-1] = p
            else:
                merged.append(p)
        # A LINE LEAVES AN ECHO OF THE OPPOSITE KIND BESIDE IT. The strongest reading within
        # six pixels is the line; the weaker one of the other kind is its shadow.
        cols[x] = [p for p in merged
                   if not any(q[2] != p[2] and abs(q[0] - p[0]) <= 6 and q[1] > p[1] for q in merged)]
    curves = []
    for kind in (-1, 1):
        segs = _trace(cols, kind)
        segs = _link([piece for s in segs for piece in _split_kinks(s)])
        # A STRAIGHT LEVEL TRACE IS A RULE, NOT A CURVE — the rule pass draws it.
        segs = [s for s in segs if s[-1][0] - s[0][0] >= 200 and len(s) >= 30
                and sum(p[2] for p in s) / len(s) >= 3.5
                and max(p[1] for p in s) - min(p[1] for p in s) > 6]
        segs.sort(key=lambda s: -len(s))
        kept = []
        for s in segs:
            if any(_shared(s, k) for k in kept):
                continue
            kept.append(s)
        for s in kept:
            paint = _stroke_paint(img, s, kind)
            if paint:
                curves.append({"kind": "dark" if kind < 0 else "light", "points": s,
                               "path": _smooth(s), **paint,
                               "measured_from": "a thin line traced column by column, "
                               "colour and fade fitted from how far it departs from its ground"})
    return curves


def _trace(cols, kind, max_miss=6, win=4.0):
    active, finished = [], []
    for x in sorted(cols):
        pts = [p for p in cols[x] if p[2] == kind]
        preds = [_extrap(t["pts"], x) for t in active]
        pairs = sorted((abs(p[0] + 0.5 - pr), ti, pi) for ti, pr in enumerate(preds)
                       for pi, p in enumerate(pts) if abs(p[0] + 0.5 - pr) <= win)
        took, owner = set(), {}
        for _c, ti, pi in pairs:
            if ti in took:
                continue
            # AT A CROSSING TWO LINES SHARE A PIXEL, and both must be allowed it —
            # but only when both were headed there anyway.
            if pi in owner and abs(preds[ti] - preds[owner[pi]]) > 3.0:
                continue
            took.add(ti)
            owner.setdefault(pi, ti)
            active[ti]["pts"].append((x, pts[pi][0] + 0.5, pts[pi][1]))
            active[ti]["miss"] = 0
        keep = []
        for ti, t in enumerate(active):
            if ti not in took:
                t["miss"] += 1
            (finished if t["miss"] > max_miss else keep).append(t)
        active = keep
        for pi, p in enumerate(pts):
            if pi not in owner:
                active.append({"pts": [(x, p[0] + 0.5, p[1])], "miss": 0})
    return [t["pts"] for t in finished + active if len(t["pts"]) >= 8]


def _slope(P):
    n = len(P)
    if n < 2:
        return 0.0
    mx = sum(p[0] for p in P) / n
    my = sum(p[1] for p in P) / n
    sxx = sum((p[0] - mx) ** 2 for p in P)
    return sum((p[0] - mx) * (p[1] - my) for p in P) / sxx if sxx else 0.0


def _split_kinks(s, win=10, limit=0.35):
    """A drawn curve does not turn a corner. Where the slope before a point and the slope
    after it disagree sharply, the trace jumped from one line to another at a crossing."""
    cut, i = [0], win
    while i < len(s) - win:
        if abs(_slope(s[i - win:i]) - _slope(s[i:i + win])) > limit:
            cut.append(i)
            i += win
        else:
            i += 1
    cut.append(len(s))
    return [s[a:b] for a, b in zip(cut, cut[1:]) if b - a >= 6]


def _link(segs, max_gap=260):
    segs = [list(s) for s in segs]
    while True:
        best = None
        for i, a in enumerate(segs):
            for j, b in enumerate(segs):
                if i == j:
                    continue
                gap = b[0][0] - a[-1][0]
                if gap <= 4 or gap > max_gap or len(a) < 6 or len(b) < 6:
                    continue
                if abs(_slope(a[-10:]) - _slope(b[:10])) > 0.15 + 0.004 * gap:
                    continue
                mid = (a[-1][0] + b[0][0]) / 2
                ya = _extrap(a, mid)
                yb = _extrap(list(reversed(b[:8])), mid)
                err = abs(ya - yb)
                if err <= 0.06 * gap + 4 and (best is None or err < best[0]):
                    best = (err, i, j)
        if not best:
            return segs
        _e, i, j = best
        segs[i] = segs[i] + segs[j]
        del segs[j]


def _shared(s, k):
    ks = {p[0]: p[1] for p in k}
    over = [p for p in s if p[0] in ks]
    if len(over) < 10:
        return False
    return sum(abs(p[1] - ks[p[0]]) <= 1.5 for p in over) >= 0.6 * len(over)


def _smooth(pts, span=80.0, reject=2.5):
    X = [p[0] for p in pts]
    Y = {p[0]: p[1] for p in pts}
    keep = {x: True for x in X}

    def at(x0):
        S0 = S1 = S2 = S3 = S4 = T0 = T1 = T2 = 0.0
        n = 0
        for x in X:
            if not keep[x]:
                continue
            u = (x - x0) / span
            if abs(u) >= 1:
                continue
            w = (1 - abs(u) ** 3) ** 3
            y = Y[x]
            n += 1
            S0 += w; S1 += w * u; S2 += w * u * u; S3 += w * u ** 3; S4 += w * u ** 4
            T0 += w * y; T1 += w * u * y; T2 += w * u * u * y
        if n < 5:
            return None
        sol = G._solve([[S0, S1, S2], [S1, S2, S3], [S2, S3, S4]], [T0, T1, T2])
        return sol[0] if sol else T0 / S0
    for _ in range(2):
        for x in X:
            f = at(x)
            if f is not None and abs(Y[x] - f) > reject:
                keep[x] = False
    samples = [(x0, f) for x0 in range(X[0], X[-1] + 1, 24) for f in [at(x0)] if f is not None]
    if len(samples) < 2:
        samples = [(p[0], p[1]) for p in pts[::6]]
    return samples


def _stroke_paint(img, pts, kind):
    rows = []
    for x, y, _d in pts:
        yi = int(y)
        obs = img.px(x, yi)
        up, dn = img.px(x, yi - 4), img.px(x, yi + 4)
        rows.append((x, obs, tuple((up[i] + dn[i]) / 2 for i in range(3))))
    N = len(rows)
    sb = [sum(r[2][c] for r in rows) for c in range(3)]
    sd = [sum(r[1][c] - r[2][c] for r in rows) for c in range(3)]
    sbb = sum(r[2][c] ** 2 for r in rows for c in range(3))
    sbd = sum(r[2][c] * (r[1][c] - r[2][c]) for r in rows for c in range(3))
    A = [[N, 0, 0, -sb[0]], [0, N, 0, -sb[1]], [0, 0, N, -sb[2]], [-sb[0], -sb[1], -sb[2], sbb]]
    sol = G._solve(A, [sd[0], sd[1], sd[2], -sbd])
    if not sol or sol[3] <= 0.02:
        colour = (0, 0, 0) if kind < 0 else (255, 255, 255)
    else:
        colour = tuple(max(0.0, min(255.0, sol[c] / sol[3])) for c in range(3))
    stops = []
    for b0 in range(0, img.w, 176):
        part = [r for r in rows if b0 <= r[0] < b0 + 176]
        if not part:
            continue
        num = sum((r[1][c] - r[2][c]) * (colour[c] - r[2][c]) for r in part for c in range(3))
        den = sum((colour[c] - r[2][c]) ** 2 for r in part for c in range(3))
        a = max(0.0, min(1.0, num / den)) if den else 0.0
        stops.append((round((b0 + 88) / img.w, 3), round(a, 3)))
    if not stops or max(a for _o, a in stops) < 0.03:
        return None
    return {"colour": hexc(colour), "stops": stops, "width": 1.5}


# ───────────────────────────── faint straight lines ─────────────────────────────

def find_rules(img, lines, surfaces, marks, strokes, band=24):
    B = Blocks([(l["x"] - 6, l["y"] - 6, l["w"] + 12, l["h"] + 12) for l in lines]
               + [(s["x"] - 4, s["y"] - 4, s["w"] + 8, s["h"] + 8) for s in surfaces]
               + [(m["box"][0] - 4, m["box"][1] - 4, m["box"][2] + 8, m["box"][3] + 8) for m in marks])
    near = {}
    for c in strokes:
        for x, y, _d in c["points"]:
            for dx in range(-8, 9, 4):
                near.setdefault((x + dx) // 4, []).append(y)

    def stroke_near(u, v, vertical):
        x, y = (u, v) if vertical else (v, u)
        return any(abs(y - yy) <= 6 for yy in near.get(x // 4, ()))

    found = []
    for vertical in (True, False):
        U, Vn = (img.w, img.h) if vertical else (img.h, img.w)
        nb, need = Vn // band, (5 if vertical else 10)

        def P(u, v):
            return img.px(u, v) if vertical else img.px(v, u)
        prof = {}
        for u in range(3, U - 3):
            blk = B.across(u - 3, vertical) + B.across(u, vertical) + B.across(u + 3, vertical)
            vals = []
            for b in range(nb):
                vs = [v for v in range(b * band + 1, (b + 1) * band, 3)
                      if not any(a <= v < c for a, c in blk) and not stroke_near(u, v, vertical)]
                if len(vs) < 4:
                    vals.append(None)
                    continue
                n = len(vs)
                c = sum(lum(P(u, v)) for v in vs) / n
                lft = sum(lum(P(u - 3, v)) for v in vs) / n
                rgt = sum(lum(P(u + 3, v)) for v in vs) / n
                vals.append((c - (lft + rgt) / 2, c))
            prof[u] = vals
        pool = []
        for sign in (1, -1):
            for u, vals in prof.items():
                if u < 16 or u > U - 16:
                    continue
                best, run, weak, blind = [], [], 0, 0
                for b, val in enumerate(vals):
                    if val is None:
                        blind += 1
                        if blind > 12:
                            best = max(best, run, key=len)
                            run, weak, blind = [], 0, 0
                        continue
                    if sign * val[0] >= 1.2:
                        run.append((b, val))
                        weak = blind = 0
                    else:
                        weak += 1
                        if weak > 1:
                            best = max(best, run, key=len)
                            run, weak, blind = [], 0, 0
                best = max(best, run, key=len)
                if len(best) >= need:
                    total = sum(sign * v[0] for _b, v in best)
                    if vertical or total / len(best) >= 2.0:
                        pool.append((total, u, sign, best))
        pool.sort(key=lambda q: -q[0])
        taken = []
        for _total, u, sign, run in pool:
            # A BRIGHT LINE LEAVES A DARK ECHO THREE PIXELS EITHER SIDE, and the echo measures
            # as a line of its own. The strongest reading within six pixels is the line.
            if any(abs(u - t) <= 8 for t in taken):
                continue
            taken.append(u)
            start, end = run[0][0] * band, (run[-1][0] + 1) * band
            stops = []
            for b, (rv, c) in run:
                ground = c - rv
                a = abs(rv) / max(1.0, (255 - ground) if sign > 0 else ground)
                stops.append((round(((b + 0.5) * band - start) / (end - start), 3), round(min(1.0, a), 3)))
            found.append({"vertical": vertical, "at": u, "start": start, "len": end - start,
                          "light": sign > 0, "stops": stops,
                          "measured_from": "a faint line brighter or darker than both its "
                          "neighbours, band by band, with its fade measured"})
    return found


# ───────────────────────────── surface fills ─────────────────────────────

def _interior(img, s, lines, surfaces):
    x, y, w, h, r = s["x"], s["y"], s["w"], s["h"], s["r"]
    inset = max(5, int(r * 0.3))
    kids = [k for k in surfaces if k["parent"] == s["id"]]
    blocks = ([(l["x"] - 4, l["y"] - 4, l["w"] + 8, l["h"] + 8) for l in lines]
              + [(k["x"] - 5, k["y"] - 5, k["w"] + 10, k["h"] + 10) for k in kids])
    step = max(3, int(min(w, h) / 30))
    pts = []
    for yy in range(y + inset, y + h - inset, step):
        for xx in range(x + inset, x + w - inset, step):
            if r > inset:
                cx = min(max(xx, x + r), x + w - r)
                cy = min(max(yy, y + r), y + h - r)
                if (xx - cx) ** 2 + (yy - cy) ** 2 > (r - inset) ** 2:
                    continue
            if any(bx <= xx < bx + bw and by <= yy < by + bh for bx, by, bw, bh in blocks):
                continue
            pts.append((xx, yy, img.px(xx, yy)))
    return pts


def fill_at(fills, model, surfaces, s, pts):
    f = fills[s["id"]]
    if f["kind"] == "flat":
        return [f["c"]] * len(pts)
    if f["kind"] == "linear":
        out = []
        for p in pts:
            t = _t(s, f["angle"], p[0], p[1])
            out.append(tuple(f["c0"][i] + t * (f["c1"][i] - f["c0"][i]) for i in range(3)))
        return out
    under = behind(fills, model, surfaces, s, pts)
    return [tuple(f["u"][i] + f["v"] * b[i] for i in range(3)) for b in under]


def behind(fills, model, surfaces, s, pts):
    if s["parent"] is not None:
        return fill_at(fills, model, surfaces, surfaces[s["parent"]], pts)
    acc = [[0.0, 0.0, 0.0] for _ in pts]
    offs = [(dx, dy) for dx in (-20, 0, 20) for dy in (-20, 0, 20)]
    for dx, dy in offs:
        for i, c in enumerate(predict(model, [(p[0] + dx, p[1] + dy) for p in pts])):
            for k in range(3):
                acc[i][k] += c[k] / len(offs)
    return [tuple(a) for a in acc]


def _t(s, ang, x, y):
    rad = math.radians(ang)
    sx, sy = math.sin(rad), -math.cos(rad)
    L = abs(s["w"] * sx) + abs(s["h"] * sy)
    return max(0.0, min(1.0, ((x - (s["x"] + s["w"] / 2)) * sx + (y - (s["y"] + s["h"] / 2)) * sy) / L + 0.5))


def surface_fill(img, model, s, lines, surfaces, fills):
    pts = _interior(img, s, lines, surfaces)
    if len(pts) < 8:
        c = img.px(s["x"] + s["w"] / 2, s["y"] + 3)
        return {"kind": "flat", "c": c, "rms": None}

    def rms(pred):
        return math.sqrt(sum((pred[i][c] - pts[i][2][c]) ** 2 for i in range(len(pts)) for c in range(3))
                         / (3 * len(pts)))
    cands = []
    c = med_rgb([p[2] for p in pts])
    cands.append((rms([c] * len(pts)), {"kind": "flat", "c": c}))
    for ang in (0, 45, 90, 135):
        ts = [_t(s, ang, p[0], p[1]) for p in pts]
        n = len(ts)
        st, stt = sum(ts), sum(t * t for t in ts)
        den = n * stt - st * st
        if abs(den) < 1e-9:
            continue
        c0, c1 = [], []
        for ch in range(3):
            sy = sum(p[2][ch] for p in pts)
            sty = sum(t * p[2][ch] for t, p in zip(ts, pts))
            b = (n * sty - st * sy) / den
            a = (sy - b * st) / n
            c0.append(a)
            c1.append(a + b)
        f = {"kind": "linear", "angle": ang, "c0": tuple(c0), "c1": tuple(c1)}
        pred = [tuple(c0[i] + t * (c1[i] - c0[i]) for i in range(3)) for t in ts]
        cands.append((rms(pred), f))
    under = behind(fills, model, surfaces, s, pts)
    N = len(pts)
    SB = [sum(b[ch] for b in under) for ch in range(3)]
    SP = [sum(p[2][ch] for p in pts) for ch in range(3)]
    SBB = sum(b[ch] ** 2 for b in under for ch in range(3))
    SBP = sum(under[i][ch] * pts[i][2][ch] for i in range(N) for ch in range(3))
    sol = G._solve([[N, 0, 0, SB[0]], [0, N, 0, SB[1]], [0, 0, N, SB[2]], [SB[0], SB[1], SB[2], SBB]],
                   [SP[0], SP[1], SP[2], SBP])
    if sol:
        u, v = sol[:3], sol[3]
        alpha = 1 - v
        if 0.05 <= alpha <= 0.98:
            tint = tuple(max(0.0, min(255.0, u[i] / alpha)) for i in range(3))
            pred = [tuple(u[i] + v * b[i] for i in range(3)) for b in under]
            cands.append((rms(pred), {"kind": "glass", "u": tuple(u), "v": v,
                                      "alpha": alpha, "tint": tint}))
    cands.sort(key=lambda c: c[0])
    best_rms = cands[0][0]
    # PREFER WHAT THE DESIGN IS WHEN THE PIXELS CANNOT TELL. Glass over what is behind
    # stays right when the layout moves; a painted gradient does not.
    for kind, margin in (("glass", 1.5), ("flat", 0.8)):
        for r_, f in cands:
            if f["kind"] == kind and r_ <= best_rms + margin:
                f["rms"] = round(r_, 2)
                return f
    cands[0][1]["rms"] = round(best_rms, 2)
    return cands[0][1]


def edge_light(img, s):
    x, y, w, h, r = s["x"], s["y"], s["w"], s["h"], s["r"]
    xs = list(range(x + r + 4, x + w - r - 4, max(1, (w - 2 * r - 8) // 20))) or list(range(x + w // 3, x + 2 * w // 3, 3))
    ys = list(range(y + r + 4, y + h - r - 4, max(1, (h - 2 * r - 8) // 20))) or list(range(y + h // 3, y + 2 * h // 3, 3))

    def side(pairs):
        edge = [(lum(img.px(*a)) + lum(img.px(*a2))) / 2 for a, a2, _b, _o in pairs]
        return (med([e - lum(img.px(*q[2])) for e, q in zip(edge, pairs)]),
                med([e - lum(img.px(*q[3])) for e, q in zip(edge, pairs)]))
    top, top_o = side([((xx, y), (xx, y + 1), (xx, y + 6), (xx, y - 3)) for xx in xs])
    bottom, bottom_o = side([((xx, y + h - 1), (xx, y + h - 2), (xx, y + h - 7), (xx, y + h + 2)) for xx in xs])
    left, left_o = side([((x, yy), (x + 1, yy), (x + 6, yy), (x - 3, yy)) for yy in ys])
    right, right_o = side([((x + w - 1, yy), (x + w - 2, yy), (x + w - 7, yy), (x + w + 2, yy)) for yy in ys])
    inner = lum(img.px(x + w / 2, y + 6))
    out = {}
    # AN EDGE IS A BORDER ONLY WHEN IT IS BRIGHTER THAN BOTH SIDES OF IT. Antialiasing
    # against a dark page darkens every edge of a light card, and against a light page
    # brightens it past the inside; neither is a border.
    if top >= 8 and top_o >= 4 and bottom <= 3 and max(left, right) <= top / 2:
        out["highlight"] = round(min(0.9, top / max(1.0, 255 - inner)), 3)
    elif min(top, bottom, left, right) >= 5 and min(top_o, bottom_o, left_o, right_o) >= 4:
        out["border"] = ("light", round(min(0.8, med([top, bottom, left, right]) / max(1.0, 255 - inner)), 3))
    return out


# ───────────────────────────── type ─────────────────────────────

def sweep(R, img, line, cands, per_page=8):
    """Candidates set side by side, a few per page, each sized and placed to the original's
    ink, then scored by ink overlap.

    A FACE THAT DID NOT LOAD DRAWS WHAT THE FALLBACK DRAWS. Twenty-four families on one page
    were still downloading when the page was shot, every one came back as the fallback, and
    the step reported that no typeface could be measured. So pages hold a few families, and
    on the scoring render each candidate is set twice — as itself and as the fallback at the
    same size — and one indistinguishable from its twin is dropped, never scored."""
    ring = ring_colour(img, line)
    ink, _light = ink_colour(img, line, ring)
    thr = max(28.0, 0.45 * abs(lum(ink) - lum(ring)))
    gl = lum(ring)
    h = line["h"]

    def mask(pxf, x0, x1, y0, y1):
        return {(xx, yy) for yy in range(int(y0), int(y1))
                for xx in range(int(max(0, x0)), int(min(img.w, x1)))
                if abs(lum(pxf(xx, yy)) - gl) > thr}
    T = mask(img.px, line["x"] - 3, line["x"] + line["w"] + 3, line["y"] - 3, line["y"] + h + 3)
    if len(T) < 20:
        return []
    tx0, tx1, ty0 = min(q[0] for q in T), max(q[0] for q in T), min(q[1] for q in T)
    Trel = {(xx, yy - ty0) for xx, yy in T}
    band = int(h * 2.4 + 40)
    st = [{"fam": f, "wt": wt, "ls": ls, "fs": h * 0.9, "left": float(line["x"]), "top": 14.0, "dead": False}
          for f, wt, ls in cands]
    for it in range(3):
        per = 2 if it == 2 else 1
        for p0 in range(0, len(st), per_page):
            chunk = st[p0:p0 + per_page]
            rows = []
            for j, c in enumerate(chunk):
                for twin in range(per):
                    fam = "Aethron Missing Face" if twin else c["fam"]
                    rows.append(f'<div class="t" style="left:{c["left"]:.1f}px;top:{(j * per + twin) * band + c["top"]:.1f}px;'
                                f'font-size:{c["fs"]:.2f}px;font-family:\'{fam}\',monospace;font-weight:{c["wt"]};'
                                f'letter-spacing:{c["ls"]}">{esc(line["text"])}</div>')
            links = "".join(f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family='
                            f'{f.replace(" ", "+")}:wght@400;500;600;700&display=swap">'
                            for f in sorted({c["fam"] for c in chunk}))
            html = (f'<!doctype html><html><head><meta charset="utf-8">{links}<style>html,body{{margin:0;'
                    f'background:{hexc(ring)}}}.t{{position:absolute;white-space:nowrap;line-height:1.2;'
                    f'color:{hexc(ink)}}}</style></head><body>{"".join(rows)}</body></html>')
            got = V.load(R.page(html, "fonts", img.w, band * per * len(chunk)))

            def gpx(xx, yy, got=got):
                return got.rgb(min(got.w - 1, max(0, xx)), min(got.h - 1, max(0, yy)))
            for j, c in enumerate(chunk):
                if c["dead"]:
                    continue
                y0 = j * per * band
                M = mask(gpx, c["left"] - 40, c["left"] + line["w"] * 2 + 40, y0, y0 + band)
                if not M:
                    c["dead"] = True
                    continue
                rx0, rx1, ry0 = min(q[0] for q in M), max(q[0] for q in M), min(q[1] for q in M)
                if it < 2:
                    k = max(0.5, min(2.0, (tx1 - tx0) / max(1, rx1 - rx0)))
                    c["fs"] = max(0.5 * h, min(1.8 * h, c["fs"] * k))
                    c["left"] += tx0 - rx0
                    c["top"] += 14 - (ry0 - y0)
                    continue
                Rrel = {(xx - rx0, yy - ry0) for xx, yy in M}
                Wm = mask(gpx, c["left"] - 40, c["left"] + line["w"] * 2 + 40, y0 + band, y0 + 2 * band)
                if Wm:
                    wx0, wy0 = min(q[0] for q in Wm), min(q[1] for q in Wm)
                    Wrel = {(xx - wx0, yy - wy0) for xx, yy in Wm}
                    if len(Rrel & Wrel) / max(1, len(Rrel | Wrel)) >= 0.9:
                        c["dead"] = True
                        continue
                Rabs = {(xx + rx0, yy) for xx, yy in Rrel}
                c["iou"] = len(Trel & Rabs) / max(1, len(Trel | Rabs))
    out = [{"iou": round(c["iou"], 4), "fam": c["fam"], "wt": c["wt"], "ls": c["ls"], "fs": c["fs"],
            "left": c["left"], "top": ty0 - 14 + c["top"]} for c in st if not c["dead"] and "iou" in c]
    if not out and per_page > 2:
        return sweep(R, img, line, cands, per_page=2)
    return sorted(out, key=lambda r: -r["iou"])


def choose_fonts(R, img, lines):
    hs = sorted(l["h"] for l in lines)
    head = max(lines, key=lambda l: l["h"])
    if head["h"] < 1.4 * hs[len(hs) // 2]:
        head = None
    body = max((l for l in lines if l is not head), key=lambda l: len(l["text"]), default=head)
    target = head or body
    s1 = sweep(R, img, target, [(f, 500 if head else 400, "0") for f in FAMILIES])
    if not s1:
        return None
    ranked = [r["fam"] for r in s1]
    say("  typeface, round 1: " + ", ".join("%s %.3f" % (r["fam"], r["iou"]) for r in s1[:5]))
    s2 = sweep(R, img, target, [(f, wt, "0") for f in ranked[:4] for wt in (400, 500, 600)])
    best = max(s1[:1] + s2, key=lambda r: r["iou"])
    s3 = sweep(R, img, target, [(best["fam"], best["wt"], ls) for ls in ("-0.04em", "-0.03em", "-0.02em", "-0.01em", "0.01em")])
    best = max([best] + s3, key=lambda r: r["iou"])
    say(f"  typeface: {best['fam']} {best['wt']} {best['ls']} — ink overlap {best['iou']:.3f}"
        f" (runner-up family {next((r['fam'] + ' ' + format(r['iou'], '.3f') for r in s1 if r['fam'] != best['fam']), '-')})")
    choice = {"head_line": target if head else None, "head": best, "body_line": body, "ranking": s1[:10]}
    if head and body is not None:
        b = sweep(R, img, body, [(f, wt, "0") for f in list(dict.fromkeys([best["fam"]] + ranked[:5])) for wt in (400, 500)])
        same = [r for r in b if r["fam"] == best["fam"]]
        top = b[0] if b else None
        pick = top if (top and (not same or top["iou"] > same[0]["iou"] + 0.03)) else (same[0] if same else top)
        choice["body"] = pick or dict(best, wt=400)
    else:
        choice["body"] = best
    return choice


def initial_text(img, lines, fonts):
    items = []
    hb, bb = fonts["head"], fonts["body"]
    body_line = fonts["body_line"]
    ratio = bb["fs"] / max(1, body_line["h"])
    for l in lines:
        ring = ring_colour(img, l)
        ink, light = ink_colour(img, l, ring)
        if fonts["head_line"] is l:
            fs, left, top, fam, wt, ls = hb["fs"], hb["left"], hb["top"], hb["fam"], hb["wt"], hb["ls"]
        elif l is body_line:
            fs, left, top, fam, wt, ls = bb["fs"], bb["left"], bb["top"], bb["fam"], bb["wt"], "0"
        else:
            # START SMALL. Oversized lines run into each other, OCR then reads the pile as one
            # string, and a line that cannot be read cannot be corrected.
            fs = l["h"] * min(ratio, 0.9)
            left, top, fam, wt, ls = l["x"], l["y"] - fs * 0.16, bb["fam"], bb["wt"], "0"
        items.append({"line": l, "ring": ring, "ink": ink, "light": light, "fam": fam,
                      "style": f"left:{left:.1f}px;top:{top:.1f}px;font-size:{fs:.2f}px;color:{hexc(ink)};"
                               f"font-weight:{wt};letter-spacing:{ls}"
                               + (f";font-family:'{fam}',sans-serif" if fam != bb["fam"] else "")})
    return items


def text_block(items):
    return "\n".join(f'<div class="t" style="{it["style"]}">{esc(it["line"]["text"])}</div>' for it in items)


def style_of(html, text):
    m = re.search(r'<div class="t" style="([^"]*)">' + re.escape(esc(text)) + r"</div>", html)
    return m


# ───────────────────────────── the page ─────────────────────────────

def _fill_css(s, f, top_level):
    if f["kind"] == "flat":
        return f"background:{hexc(f['c'])}"
    if f["kind"] == "linear":
        return f"background:linear-gradient({f['angle']}deg,{hexc(f['c0'])} 0%,{hexc(f['c1'])} 100%)"
    t = f["tint"]
    css = f"background:rgba({int(round(t[0]))},{int(round(t[1]))},{int(round(t[2]))},{f['alpha']:.3f})"
    if top_level:
        css += ";backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)"
    return css


def surface_css(spec, s):
    f = spec["fills"][s["id"]]
    e = spec["edges"][s["id"]]
    parts = [f"left:{s['x']}px", f"top:{s['y']}px", f"width:{s['w']}px", f"height:{s['h']}px",
             f"border-radius:{s['r']}px", _fill_css(s, f, s["parent"] is None)]
    if "border" in e:
        tone, a = e["border"]
        parts.append(f"border:1px solid rgba({'255,255,255' if tone == 'light' else '0,0,0'},{a})")
    shadows = []
    if "highlight" in e:
        shadows.append(f"inset 0 1px 0 rgba(255,255,255,{e['highlight']})")
    sh = spec["shadows"].get(s["id"])
    if sh and sh["a"] > 0.005:
        c = sh["c"]
        shadows.append(f"0 {sh['oy']:.0f}px {sh['blur']:.0f}px rgba({int(c[0])},{int(c[1])},{int(c[2])},{sh['a']:.3f})")
    if shadows:
        parts.append("box-shadow:" + ",".join(shadows))
    return ";".join(parts)


def _decor(spec):
    W, H = spec["W"], spec["H"]
    out = []
    for ri, r in enumerate(spec["rules"]):
        tone = "255,255,255" if r["light"] else "0,0,0"
        stops = ",".join(f"rgba({tone},{a}) {o * 100:.1f}%" for o, a in r["stops"])
        stops = f"rgba({tone},0) 0%,{stops},rgba({tone},0) 100%"
        if r["vertical"]:
            out.append(f'<div class="rl" data-ae-id="r{ri:02d}" style="left:{r["at"]}px;top:{r["start"]}px;width:1px;height:{r["len"]}px;'
                       f'background:linear-gradient(to bottom,{stops})"></div>')
        else:
            out.append(f'<div class="rl" data-ae-id="r{ri:02d}" style="left:{r["start"]}px;top:{r["at"]}px;height:1px;width:{r["len"]}px;'
                       f'background:linear-gradient(to right,{stops})"></div>')
    if spec["strokes"]:
        defs, paths = [], []
        for i, c in enumerate(spec["strokes"]):
            defs.append(f'<linearGradient id="st{i}" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="{W}" y2="0">'
                        + "".join(f'<stop offset="{o}" stop-color="{c["colour"]}" stop-opacity="{a}"/>' for o, a in c["stops"])
                        + "</linearGradient>")
            paths.append(f'<path d="{_path(c["path"], W)}" fill="none" stroke="url(#st{i})" stroke-width="{c["width"]}"/>')
        out.append(f'<svg class="strokes" data-ae-id="k00" width="{W}" height="{H}" viewBox="0 0 {W} {H}" aria-hidden="true">'
                   f'<defs>{"".join(defs)}</defs>{"".join(paths)}</svg>')
    return out


def _path(samples, W):
    P = list(samples)
    if len(P) >= 2:
        (xa, ya), (xb, yb) = P[0], P[min(2, len(P) - 1)]
        (xc, yc), (xd, yd) = P[max(0, len(P) - 3)], P[-1]
        if xa > 24:
            P.insert(0, (xa - 24, ya - (yb - ya) / max(1, xb - xa) * 24))
        if xd < W - 24:
            P.append((xd + 24, yd + (yd - yc) / max(1, xd - xc) * 24))
    d = [f"M{P[0][0]:.1f},{P[0][1]:.1f}"]
    for i in range(len(P) - 1):
        p0 = P[i - 1] if i else P[i]
        p1, p2 = P[i], P[i + 1]
        p3 = P[i + 2] if i + 2 < len(P) else P[i + 1]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def _mark_html(m, logo=False, mid="m00"):
    x, y, w, h = m["box"]
    if m["kind"] == "rings":
        ring = m["rings"]
        pad = 6
        vx, vy = x - pad, y - pad
        circles = "".join(f'<circle cx="{ring["cx"] - vx:.2f}" cy="{ring["cy"] - vy:.2f}" r="{c["r"]}" fill="none" '
                          f'stroke="{c["colour"]}" stroke-width="{c["width"]}"/>' for c in ring["rings"])
        svg = (f'<svg width="{w + 2 * pad}" height="{h + 2 * pad}" viewBox="0 0 {w + 2 * pad} {h + 2 * pad}" '
               f'aria-hidden="true" style="display:block">{circles}</svg>')
        pos = f"left:{vx}px;top:{vy}px;width:{w + 2 * pad}px;height:{h + 2 * pad}px"
    else:
        svg = f'<img src="{m["src"]}" alt="" width="{w}" height="{h}" style="display:block">'
        pos = f"left:{x}px;top:{y}px;width:{w}px;height:{h}px"
    if logo:
        return f'<a class="mk logo" data-ae-id="{mid}" href="#" aria-label="Home" style="{pos}">{svg}</a>'
    return f'<span class="mk" data-ae-id="{mid}" style="{pos}">{svg}</span>'


def emit(spec, text_html, semantic=False):
    W, H = spec["W"], spec["H"]
    fams = list(dict.fromkeys([spec["fonts"]["body"]["fam"], spec["fonts"]["head"]["fam"]]))
    links = "".join(f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family='
                    f'{f.replace(" ", "+")}:wght@400;500;600;700&display=swap">' for f in fams)
    base = spec["background"]["model"]["base"]
    css = [f"html,body{{margin:0;padding:0;background:{hexc(base)}}}",
           f".page{{position:relative;width:{W}px;height:{H}px;overflow:hidden;transform-origin:0 0;"
           f"font-family:'{spec['fonts']['body']['fam']}',sans-serif;"
           f"background:{G.css(spec['background']['model'], W, H, units='%')}}}",
           ".t{position:absolute;white-space:nowrap;line-height:1.2;margin:0}",
           ".sf{position:absolute;box-sizing:border-box}", ".rl{position:absolute}",
           ".mk{position:absolute;display:block}", ".strokes{position:absolute;left:0;top:0;pointer-events:none}"]
    body = _decor(spec)
    R = roles(spec, text_html) if semantic else None
    for i, m in enumerate(spec["marks"]):
        body.append(_mark_html(m, logo=bool(R and i in R["logo_marks"]), mid=f"m{i:02d}"))
    if not semantic:
        for s in sorted(spec["surfaces"], key=lambda s: s["depth"]):
            body.append(f'<div class="sf" data-ae-id="s{s["id"]:02d}" style="{surface_css(spec, s)}"></div>')
        body.append("<!--ae:text-->\n" + text_html + "\n<!--ae:/text-->")
    else:
        body += _semantic_body(spec, text_html, R)
        css += [".link{text-decoration:none}.link:hover{filter:brightness(1.35)}",
                "button.sf{padding:0;margin:0;font:inherit;color:inherit;cursor:pointer;-webkit-appearance:none;appearance:none}",
                "button.sf:not([style*='border:']){border:0}",
                "button.sf:hover{filter:brightness(1.12)}button.sf[aria-pressed=true]{filter:brightness(1.18)}",
                ".prompt{background:transparent;border:0;outline:none;resize:none;padding:0;white-space:pre-wrap;font-family:inherit;line-height:1.2}",
                ".prompt::placeholder{color:var(--ph);opacity:1}",
                ".link:focus-visible,button.sf:focus-visible,.logo:focus-visible{outline:2px solid #8DB6FF;outline-offset:3px}",
                ".link,button.sf{transition:filter .15s}@media (prefers-reduced-motion:reduce){*{transition:none!important}}"]
    script = ""
    if semantic:
        script = ("<script>(function(){var p=document.querySelector('.page'),W=%d,H=%d;"
                  "function f(){var k=Math.min(1,window.innerWidth/W);p.style.transform=k<1?'scale('+k+')':'';"
                  "document.body.style.height=k<1?(H*k)+'px':'';}f();window.addEventListener('resize',f);"
                  "document.querySelectorAll('button[aria-pressed]').forEach(function(b){b.addEventListener('click',function(){"
                  "b.setAttribute('aria-pressed',b.getAttribute('aria-pressed')!=='true');});});"
                  "document.querySelectorAll('form').forEach(function(fm){fm.addEventListener('submit',function(e){"
                  "e.preventDefault();var t=fm.querySelector('textarea');if(t)t.focus();});});})();</script>") % (W, H)
    title = esc(spec["title"])
    # EVERY ELEMENT CARRIES AN ID the edit tool can name. Without them the guarded edit
    # seam found nothing on this page, and "make the button green" had no path but a hand.
    page_open = '<main class="page" data-ae-id="bg">' if semantic else '<div class="page">'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>{links}'
            f'<style>{chr(10).join(css)}</style></head><body>{page_open}\n'
            + "\n".join(body) + f'\n</{"main" if semantic else "div"}>{script}</body></html>')


def text_of(html):
    a = html.index("<!--ae:text-->") + len("<!--ae:text-->")
    return html[a:html.index("<!--ae:/text-->")].strip()


def roles(spec, text_html):
    lines, surfs, W, H = spec["lines"], spec["surfaces"], spec["W"], spec["H"]
    owner = {}
    for i, l in enumerate(lines):
        c = [s for s in surfs if centre_in(l, s)]
        if c:
            owner[i] = max(c, key=lambda s: s["depth"])["id"]
    buttons = {}
    for s in surfs:
        own = [i for i, sid in owner.items() if sid == s["id"]]
        kids = [k for k in surfs if k["parent"] == s["id"]]
        if len(own) == 1 and not kids and s["h"] <= 90 and s["w"] <= 420 and len(lines[own[0]]["text"].split()) <= 4:
            buttons[s["id"]] = own[0]
    placeholders = {}
    for s in surfs:
        if s["id"] in buttons or s["w"] * s["h"] < 20000:
            continue
        own = [i for i, sid in owner.items() if sid == s["id"] and i not in buttons.values()]
        if len(own) != 1:
            continue
        l, it = lines[own[0]], spec["items"][own[0]]
        if l["x"] - s["x"] <= 90 and l["y"] - s["y"] <= 90 and abs(lum(it["ink"]) - lum(it["ring"])) < 110:
            placeholders[s["id"]] = own[0]
    head = spec["head_index"]
    top = [i for i, l in enumerate(lines) if i not in owner and i != head
           and l["y"] + l["h"] / 2 < 0.14 * H and len(l["text"].split()) <= 3]
    top.sort(key=lambda i: lines[i]["x"])
    nav = []
    if len(top) >= 3:
        gaps = [lines[b]["x"] - (lines[a]["x"] + lines[a]["w"]) for a, b in zip(top, top[1:])]
        mg = med(gaps)
        cluster, clusters = [top[0]], []
        for g, i in zip(gaps, top[1:]):
            if g > 3 * max(10, mg):
                clusters.append(cluster)
                cluster = [i]
            else:
                cluster.append(i)
        clusters.append(cluster)
        nav = max(clusters, key=len)
        if len(nav) < 2:
            nav = []
    logo_marks = [i for i, m in enumerate(spec["marks"])
                  if m["box"][1] < 0.14 * H and m["box"][0] < 0.3 * W]
    return {"owner": owner, "buttons": buttons, "placeholders": placeholders, "links": top,
            "nav": nav, "head": head, "logo_marks": logo_marks[:1]}


def _rel(style, dx, dy):
    s = re.sub(r"left:\s*([-\d.]+)px", lambda z: f"left:{float(z.group(1)) - dx:.1f}px", style, count=1)
    return re.sub(r"top:\s*([-\d.]+)px", lambda z: f"top:{float(z.group(1)) - dy:.1f}px", s, count=1)


def _semantic_body(spec, text_html, R):
    lines, surfs = spec["lines"], spec["surfaces"]
    used = set()
    out = []
    forms = {}
    for sid in R["placeholders"]:
        forms[sid] = [k["id"] for k in surfs if k["parent"] == sid]
    in_form = {k for ks in forms.values() for k in ks} | set(forms)
    for s in sorted(surfs, key=lambda s: s["depth"]):
        if s["id"] in in_form and s["id"] not in forms:
            continue
        chunk = []
        group = [s] + ([surfs[k] for k in forms[s["id"]]] if s["id"] in forms else [])
        submit = None
        if s["id"] in forms:
            kids = [k for k in forms[s["id"]] if k in R["buttons"]]
            if kids:
                parent_c = spec["fills"][s["id"]]
                pc = parent_c.get("c") or parent_c.get("tint") or parent_c.get("c0")

                def colour_of(k):
                    f = spec["fills"][k]
                    c = f.get("c") or f.get("tint") or tuple((f["c0"][i] + f["c1"][i]) / 2 for i in range(3))
                    return c
                submit = max(kids, key=lambda k: dist(colour_of(k), pc))
        for g in group:
            css = surface_css(spec, g)
            if g["id"] in R["buttons"]:
                i = R["buttons"][g["id"]]
                m = style_of(text_html, lines[i]["text"])
                if not m:
                    chunk.append(f'<div class="sf" data-ae-id="s{g["id"]:02d}" style="{css}"></div>')
                    continue
                border = 1 if "border:" in css else 0
                inner = _rel(m.group(1), g["x"] + border, g["y"] + border)
                kind = ('type="submit"' if g["id"] == submit else
                        'type="button" aria-pressed="false"' if g["parent"] is not None else 'type="button"')
                chunk.append(f'<button {kind} class="sf" data-ae-id="s{g["id"]:02d}" style="{css}"><span class="t" style="{inner}">'
                             f'{esc(lines[i]["text"])}</span></button>')
                used.add(i)
            else:
                chunk.append(f'<div class="sf" data-ae-id="s{g["id"]:02d}" style="{css}"></div>')
            if g["id"] in R["placeholders"]:
                i = R["placeholders"][g["id"]]
                m = style_of(text_html, lines[i]["text"])
                if m:
                    st = m.group(1)
                    ph = re.search(r"color:(#[0-9A-Fa-f]{6})", st).group(1)
                    lx = float(re.search(r"left:\s*([-\d.]+)px", st).group(1))
                    ly = float(re.search(r"top:\s*([-\d.]+)px", st).group(1))
                    kids_y = [surfs[k]["y"] for k in forms.get(g["id"], [])]
                    bottom = (min(kids_y) - 12) if kids_y else g["y"] + g["h"] * 0.6
                    width = g["x"] + g["w"] - (lx - g["x"]) - lx
                    st2 = re.sub(r"color:#[0-9A-Fa-f]{6};?", "", st)
                    chunk.append(f'<textarea class="t prompt" data-ae-id="t{i:02d}" name="prompt" aria-label="{esc(lines[i]["text"])}" '
                                 f'placeholder="{esc(lines[i]["text"])}" style="{st2};color:{hexc(spec["items"][i]["ink"])};'
                                 f'width:{max(40, width):.0f}px;height:{max(24, bottom - ly):.0f}px;--ph:{ph}"></textarea>')
                    used.add(i)
        if s["id"] in forms:
            out.append('<form class="composer" aria-label="Composer">\n' + "\n".join(chunk) + "\n</form>")
        else:
            out += chunk
    nav_html, rest = [], []
    for i, l in enumerate(lines):
        if i in used:
            continue
        m = style_of(text_html, l["text"])
        if not m:
            continue
        st = m.group(1)
        if i == R["head"]:
            rest.append(f'<h1 class="t" data-ae-id="t{i:02d}" style="{st}">{esc(l["text"])}</h1>')
        elif i in R["links"]:
            a = f'<a class="t link" data-ae-id="t{i:02d}" href="#{slug(l["text"])}" style="{st}">{esc(l["text"])}</a>'
            (nav_html if i in R["nav"] else rest).append(a)
        else:
            rest.append(f'<p class="t" data-ae-id="t{i:02d}" style="{st}">{esc(l["text"])}</p>')
    if nav_html:
        out.append('<nav aria-label="Primary">' + "".join(nav_html) + "</nav>")
    return out + rest


# ───────────────────────────── the stages that render ─────────────────────────────

def refine_lines(R, img, spec, html, rounds=6):
    """Read both pages and move every line by the difference, matching lines by SIMILAR
    words near the same place. An exact-string match abstains whenever a substitute face
    reads back one letter differently, and a line that is never matched is never moved."""
    import difflib
    lines = spec["lines"]
    best_html, best_ok, cur = html, -1, html
    for rnd in range(rounds):
        got = V.ocr(str(R.page(cur, "refine"))) or []
        pairs = []
        for i, l in enumerate(lines):
            for j, g in enumerate(got):
                r = difflib.SequenceMatcher(None, l["text"].lower(), g["text"].strip().lower()).ratio()
                d = abs(g["y"] - l["y"]) + abs(g["x"] - l["x"]) * 0.25
                if r >= 0.6 and d <= 120:
                    pairs.append((r - d / 400, i, j))
        pairs.sort(reverse=True)
        match, used = {}, set()
        for _sc, i, j in pairs:
            if i in match or j in used:
                continue
            match[i] = got[j]
            used.add(j)
        ok = moved = 0
        new = cur
        for i, l in enumerate(lines):
            g, m = match.get(i), style_of(new, l["text"])
            if not g or not m:
                continue
            dx, dy, scale = l["x"] - g["x"], l["y"] - g["y"], l["h"] / max(1, g["h"])
            if abs(dx) <= 2 and abs(dy) <= 2 and abs(scale - 1) <= 0.10:
                ok += 1
                continue
            ns = m.group(1)
            if abs(scale - 1) > 0.10:
                k = max(0.6, min(1.6, 1 + (scale - 1) * 0.6))
                ns = re.sub(r"font-size:\s*([\d.]+)px", lambda z: f"font-size:{float(z.group(1)) * k:.2f}px", ns, count=1)
            ns = re.sub(r"left:\s*([-\d.]+)px", lambda z: f"left:{float(z.group(1)) + dx:.1f}px", ns, count=1)
            ns = re.sub(r"top:\s*([-\d.]+)px", lambda z: f"top:{float(z.group(1)) + dy:.1f}px", ns, count=1)
            new = new.replace(m.group(0), f'<div class="t" style="{ns}">{esc(l["text"])}</div>', 1)
            moved += 1
        say(f"    refine round {rnd + 1}: {ok} of {len(lines)} settled, {len(match)} matched, {moved} moved")
        if ok > best_ok:
            best_html, best_ok = cur, ok
        if not moved:
            break
        cur = new
    return best_html


def fit_type(R, img, spec, html, report):
    lines = spec["lines"]

    def render_fn(h, region=None):
        png = R.page(h, "refine")
        return png, G.compare(img.shot, V.load(png), step=3, tol=TOL, border=10)["within_tol"]
    html = refine_lines(R, img, spec, html)
    # ONE SIZE PER GROUP OF LINES THAT BELONG TOGETHER, then each line's INK width fitted by
    # spacing, which keeps letters their own shape. Guarded by the CHECKLIST as well as the
    # pixels: a pixel score once accepted a pass that cost two lines of the subtitle.
    body = [i for i in range(len(lines)) if i != spec["head_index"]]
    rings = {i: lum(spec["items"][i]["ring"]) for i in body}

    def cy(l):
        return l["y"] + l["h"] / 2

    def xov(a, b):
        return min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]) > 0
    parent = {i: i for i in body}

    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k
    for n, a in enumerate(body):
        for b in body[n + 1:]:
            la, lb = lines[a], lines[b]
            row = (abs(cy(la) - cy(lb)) <= 6 and abs(rings[a] - rings[b]) < 60
                   and abs(la["h"] - lb["h"]) <= 4)
            para = (xov(la, lb) and abs(la["h"] - lb["h"]) <= 3
                    and 0 <= lb["y"] - (la["y"] + la["h"]) <= max(la["h"], lb["h"]) * 0.6)
            if row or para:
                parent[find(a)] = find(b)
    groups = {}
    for i in body:
        groups.setdefault(find(i), []).append(i)
    trects = [(l["x"] - 2, l["y"] - 2, l["w"] + 4, l["h"] + 4) for l in lines]
    png0 = R.page(html, "widths0")
    e0 = region_mae(img, png0, trects)
    c0 = V.verify_rebuild(str(img.path), png0).get("lines_correct") or 0
    cand = html
    for members in groups.values():
        if len(members) < 2:
            continue
        sizes = []
        for i in members:
            m = style_of(cand, lines[i]["text"])
            if m:
                sizes.append(float(re.search(r"font-size:([\d.]+)px", m.group(1)).group(1)))
        if not sizes:
            continue
        size = med(sizes)
        for i in members:
            m = style_of(cand, lines[i]["text"])
            if m:
                ns = re.sub(r"font-size:[\d.]+px", f"font-size:{size:.2f}px", m.group(1))
                cand = cand.replace(m.group(0), f'<div class="t" style="{ns}">{esc(lines[i]["text"])}</div>', 1)
    for i in body:
        m = style_of(cand, lines[i]["text"])
        if m:
            ns = re.sub(r";?transform:scaleX\([\d.]+\)", "", m.group(1))
            ns = re.sub(r";?transform-origin:left top", "", ns)
            cand = cand.replace(m.group(0), f'<div class="t" style="{ns}">{esc(lines[i]["text"])}</div>', 1)

    def ink_box(shot_px, x0, x1, y0, y1, gl, thr=40):
        xs, ys = [], []
        for yy in range(max(0, int(y0)), min(img.h, int(y1))):
            for xx in range(max(0, int(x0)), min(img.w, int(x1))):
                if abs(lum(shot_px(xx, yy)) - gl) > thr:
                    xs.append(xx)
                    ys.append(yy)
        return (min(xs), max(xs), min(ys), max(ys)) if xs else None
    tink = {i: ink_box(img.px, lines[i]["x"] - 3, lines[i]["x"] + lines[i]["w"] + 3,
                       lines[i]["y"] - 2, lines[i]["y"] + lines[i]["h"] + 2, rings[i]) for i in body}

    def window(i):
        l = lines[i]
        above = [lines[q] for q in body if q != i and xov(lines[q], l) and lines[q]["y"] + lines[q]["h"] <= l["y"] + 2]
        below = [lines[q] for q in body if q != i and xov(lines[q], l) and lines[q]["y"] >= l["y"] + l["h"] - 2]
        y0 = max([l["y"] - 8] + [(q["y"] + q["h"] + l["y"]) / 2 for q in above])
        y1 = min([l["y"] + l["h"] + 8] + [(q["y"] + l["y"] + l["h"]) / 2 for q in below])
        # THE WINDOW STOPS HALF WAY TO THE NEXT WORD, AND AT THE EDGE OF WHAT IT SITS ON.
        # Twenty-five pixels either side took in the neighbouring nav link and a button's
        # own edge, measured those as this line's ink, and set letter-spacing to match.
        row = [q for q in lines if q is not l and abs(cy(q) - cy(l)) <= max(q["h"], l["h"]) / 2]
        x0 = max([l["x"] - 25] + [(q["x"] + q["w"] + l["x"]) / 2 for q in row if q["x"] + q["w"] <= l["x"] + 2])
        x1 = min([l["x"] + l["w"] + 25] + [(q["x"] + l["x"] + l["w"]) / 2 for q in row if q["x"] >= l["x"] + l["w"] - 2])
        home = [sf for sf in spec["surfaces"] if centre_in(l, sf)]
        if home:
            sf = max(home, key=lambda z: z["depth"])
            x0, x1 = max(x0, sf["x"] + 3), min(x1, sf["x"] + sf["w"] - 3)
            y0, y1 = max(y0, sf["y"] + 3), min(y1, sf["y"] + sf["h"] - 3)
        return x0, x1, y0, y1
    for rnd in range(3):
        got = V.load(R.page(cand, f"widths{rnd + 1}"))

        def gpx(xx, yy):
            return got.rgb(min(got.w - 1, max(0, xx)), min(got.h - 1, max(0, yy)))
        for i in body:
            t = tink[i]
            r = ink_box(gpx, *window(i), rings[i])
            m = style_of(cand, lines[i]["text"])
            if not t or not r or not m:
                continue
            st = m.group(1)
            dw, dx, dy = (t[1] - t[0]) - (r[1] - r[0]), t[0] - r[0], t[2] - r[2]
            if abs(dw) <= 1 and abs(dx) <= 1 and abs(dy) <= 1:
                continue
            lsm = re.search(r"letter-spacing:([-\d.]+)px", st)
            ls = float(lsm.group(1)) if lsm else 0.0
            ns = re.sub(r";?letter-spacing:[^;]*", "", st) + \
                f";letter-spacing:{ls + dw / max(1, len(lines[i]['text']) - 1) * 0.9:.2f}px"
            ns = re.sub(r"left:\s*([-\d.]+)px", lambda z: f"left:{float(z.group(1)) + dx:.1f}px", ns, count=1)
            ns = re.sub(r"top:\s*([-\d.]+)px", lambda z: f"top:{float(z.group(1)) + dy:.1f}px", ns, count=1)
            cand = cand.replace(m.group(0), f'<div class="t" style="{ns}">{esc(lines[i]["text"])}</div>', 1)
    png1 = R.page(cand, "widths_after")
    e1 = region_mae(img, png1, trects)
    c1 = V.verify_rebuild(str(img.path), png1).get("lines_correct") or 0
    kept = e1 < e0 and c1 >= c0
    say(f"  line groups + ink widths: text error {e0:.2f} -> {e1:.2f}, checklist {c0} -> {c1}"
        + ("  kept" if kept else "  discarded"))
    report["stages"]["widths"] = {"error": [round(e0, 2), round(e1, 2)], "checklist": [c0, c1], "kept": kept}
    if kept:
        html = cand
    # INK COLOUR, LIKE FOR LIKE: the same 2% extreme on both pages.
    for rnd in range(2):
        png = R.page(html, "colour_a")
        got = V.load(png)
        ea = region_mae(img, png, trects)
        new = html
        for i, l in enumerate(lines):
            if i == spec["head_index"]:
                continue
            it = spec["items"][i]
            gimg = type("I", (), {})()
            gimg.px = lambda xx, yy: got.rgb(min(got.w - 1, max(0, int(xx))), min(got.h - 1, max(0, int(yy))))
            t, _ = ink_colour(img, l, it["ring"])
            r, _ = ink_colour(gimg, l, it["ring"])
            m = re.search(r'(<div class="t" style="[^"]*?color:)(#[0-9A-Fa-f]{6})([^"]*">'
                          + re.escape(esc(l["text"])) + r"</div>)", new)
            if not m:
                continue
            c = [int(m.group(2)[k:k + 2], 16) for k in (1, 3, 5)]
            nc = [max(0, min(255, round(c[k] + (t[k] - r[k]) * 0.8))) for k in range(3)]
            new = new.replace(m.group(0), m.group(1) + hexc(nc) + m.group(3))
        eb = region_mae(img, R.page(new, "colour_b"), trects)
        say(f"  ink colour round {rnd + 1}: text error {ea:.2f} -> {eb:.2f}" + ("  kept" if eb < ea else "  discarded"))
        if eb < ea:
            html = new
        else:
            break
    return html


def fit_shadows(R, img, spec, html):
    tb = text_of(html)
    for s in spec["surfaces"]:
        if s["parent"] is not None or s["w"] * s["h"] < 20000:
            continue
        x, y, w, h = s["x"], s["y"], s["w"], s["h"]
        rects = [(x, y + h + 2, w, 100), (x - 44, y + h * 0.2, 42, h * 0.8), (x + w + 2, y + h * 0.2, 42, h * 0.8)]
        # the shadow's colour, from how the pixels just below the card depart from the ground
        pts = [(xx, yy) for yy in range(y + h + 4, y + h + 30, 3) for xx in range(x + 10, x + w - 10, 12)]
        under = predict(spec["background"]["model"], pts)
        obs = [img.px(*p) for p in pts]
        N = len(pts)
        SB = [sum(b[c] for b in under) for c in range(3)]
        SP = [sum(o[c] for o in obs) for c in range(3)]
        SBB = sum(b[c] ** 2 for b in under for c in range(3))
        SBP = sum(under[i][c] * obs[i][c] for i in range(N) for c in range(3))
        sol = G._solve([[N, 0, 0, SB[0]], [0, N, 0, SB[1]], [0, 0, N, SB[2]], [SB[0], SB[1], SB[2], SBB]],
                       [SP[0], SP[1], SP[2], SBP]) if N else None
        a0 = 1 - sol[3] if sol else 0.3
        colour = tuple(max(0, min(255, sol[c] / a0)) for c in range(3)) if sol and a0 > 0.03 else (10, 20, 50)
        cur = {"oy": round(h * 0.08), "blur": round(h * 0.2), "a": max(0.15, min(0.6, a0 * 1.5)), "c": colour}
        spec["shadows"][s["id"]] = dict(cur)
        best = region_mae(img, R.page(emit(spec, tb), "shadow"), rects)
        start = best
        steps = {"a": 0.15, "blur": 20.0, "oy": 10.0}
        for _r in range(3):
            for k in ("a", "blur", "oy"):
                for sgn in (1, -1):
                    trial = dict(cur)
                    trial[k] = max(0.0, trial[k] + sgn * steps[k])
                    spec["shadows"][s["id"]] = trial
                    e = region_mae(img, R.page(emit(spec, tb), "shadow"), rects)
                    if e < best - 0.02:
                        best, cur = e, trial
                        break
                    spec["shadows"][s["id"]] = cur
            steps = {k: v / 2 for k, v in steps.items()}
        spec["shadows"][s["id"]] = cur
        say(f"  shadow of the {w}x{h} surface: 0 {cur['oy']:.0f}px {cur['blur']:.0f}px alpha {cur['a']:.2f} "
            f"— error around it {start:.2f} -> {best:.2f}")
    return emit(spec, tb)


# ───────────────────────────── grade and viewer ─────────────────────────────

def grade(img, spec, png, pre_png, report, out):
    fb = V.load(png)
    whole = G.compare(img.shot, fb, step=3, tol=TOL, border=10)
    bgc = G.compare(img.shot, fb, exclude=spec["background"]["exclude"], step=3, tol=TOL, border=12)
    same = G.compare(V.load(pre_png), fb, step=2, tol=TOL, border=0)
    chk = V.verify_rebuild(str(img.path), png)
    W, H = img.w, img.h

    def region(rects):
        tot = hit = 0
        s = 0.0
        for (x, y, w, h) in rects:
            for yy in range(max(0, int(y)), min(H, int(y + h)), 2):
                for xx in range(max(0, int(x)), min(W, int(x + w)), 2):
                    p, q = img.shot.rgb(xx, yy), fb.rgb(xx, yy)
                    d = max(abs(p[0] - q[0]), abs(p[1] - q[1]), abs(p[2] - q[2]))
                    tot += 1
                    hit += d <= TOL
                    s += dist(p, q) / 3
        return (round(hit / max(1, tot) * 100, 1), round(s / max(1, tot), 2)) if tot else None
    regions = []
    for i, l in enumerate(spec["lines"]):
        regions.append((("headline" if i == spec["head_index"] else "text") + f": {l['text'][:28]}",
                        region([(l["x"] - 3, l["y"] - 3, l["w"] + 6, l["h"] + 6)])))
    for s in spec["surfaces"]:
        regions.append((f"surface {s['w']}x{s['h']} ({spec['fills'][s['id']]['kind']})",
                        region([(s["x"], s["y"], s["w"], s["h"])])))
    for m in spec["marks"]:
        regions.append((f"mark ({m['kind']})", region([m["box"]])))
    rgba = bytearray()
    for y in range(H):
        for x in range(W):
            p, q = img.shot.rgb(x, y), fb.rgb(x, y)
            d = max(abs(p[0] - q[0]), abs(p[1] - q[1]), abs(p[2] - q[2]))
            g = lum(p) * 0.3
            if d > TOL:
                k = min(1.0, d / 60.0)
                rgba += bytes((int(g + (255 - g) * k), int(g + (110 - g) * k), int(g * (1 - k)), 255))
            else:
                rgba += bytes((int(g), int(g), int(g), 255))
    GR.write_png(out / "diff.png", W, H, bytes(rgba))
    report["grade"] = {"whole_within12": round(whole["within_tol"] * 100, 2), "whole_mean": round(whole["mean_abs"], 2),
                       "background_within12": round(bgc["within_tol"] * 100, 2),
                       "semantic_pixels_unchanged": round(same["within_tol"] * 100, 2),
                       "checklist": {"verdict": chk.get("verdict"), "correct": chk.get("lines_correct"),
                                     "expected": chk.get("lines_expected"), "findings": chk.get("findings", [])},
                       "regions": [{"name": n, "within12": r[0], "mean": r[1]} for n, r in regions if r]}


VIEWER = r"""<title>__TITLE__</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root{--ground:#F2F3F6;--panel:#FFFFFF;--ink:#14161C;--muted:#5B6170;--line:#DCDFE6;--accent:#3563D8;--accent-soft:#E4EBFB;--good:#1B7F4E;--warn:#A8661A;--bar:#E7E9EE}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--ground:#0B0C10;--panel:#14161C;--ink:#E8EAF0;--muted:#9499A8;--line:#262A33;--accent:#7FA4FF;--accent-soft:#1B2438;--good:#4DC38B;--warn:#E2A548;--bar:#22252D}}
:root[data-theme="dark"]{--ground:#0B0C10;--panel:#14161C;--ink:#E8EAF0;--muted:#9499A8;--line:#262A33;--accent:#7FA4FF;--accent-soft:#1B2438;--good:#4DC38B;--warn:#E2A548;--bar:#22252D}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);font-family:"Schibsted Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;font-size:15px;line-height:1.55}
.wrap{max-width:1180px;margin:0 auto;padding:28px 24px 64px}
.mono{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
header.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px 24px;margin-bottom:18px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 6px}
h1{font-size:29px;line-height:1.15;font-weight:600;letter-spacing:-.02em;margin:0;text-wrap:balance}
.lede{margin:6px 0 0;color:var(--muted);max-width:64ch}
.modes{display:inline-flex;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
.modes button{font:inherit;font-size:14px;font-weight:500;color:var(--muted);background:transparent;border:0;border-radius:7px;padding:7px 12px;cursor:pointer}
.modes button[aria-pressed="true"]{background:var(--accent-soft);color:var(--accent)}
.modes button:focus-visible,.split input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.stage{position:relative;width:100%;background:#000;border-radius:12px;overflow:hidden;border:1px solid var(--line)}
.stage.actual{overflow:auto;max-height:80vh}
.canvas{position:absolute;left:0;top:0;width:__W__px;height:__H__px;transform-origin:0 0}
.stage.actual .canvas{position:relative}
.canvas>*{position:absolute;left:0;top:0;width:__W__px;height:__H__px;border:0;display:block}
#overlay{pointer-events:none}
#divider{position:absolute;top:0;bottom:0;width:2px;background:#fff;box-shadow:0 0 0 1px rgba(0,0,0,.35);pointer-events:none;left:50%}
.below{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px 20px;margin:10px 2px 0;color:var(--muted);font-size:13.5px}
.split{display:flex;align-items:center;gap:10px}.split input{width:min(320px,52vw);accent-color:var(--accent)}
.swatch{width:12px;height:12px;border-radius:3px;background:#FF6E00;display:inline-block;margin-right:6px;vertical-align:-1px}
.figures{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:26px 0 12px}
.fig{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.fig .n{font-size:27px;font-weight:600;line-height:1.1}.fig .l{color:var(--muted);font-size:13.5px;margin-top:4px}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:12px}
section.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 18px 8px;margin-bottom:12px}
h2{font-size:16px;font-weight:600;margin:0 0 4px}.sub{color:var(--muted);font-size:13.5px;margin:0 0 12px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td{padding:6px 0;border-top:1px solid var(--line);vertical-align:middle}
td.name{width:44%;padding-right:10px;overflow-wrap:anywhere}td.num{width:14%;text-align:right;padding-left:8px;white-space:nowrap}
.track{height:8px;background:var(--bar);border-radius:4px;overflow:hidden}.track i{display:block;height:100%;border-radius:4px;background:var(--accent)}
.track i.good{background:var(--good)}.track i.warn{background:var(--warn)}
.row{display:grid;grid-template-columns:110px minmax(0,1fr);gap:4px 14px;padding:9px 0;border-top:1px solid var(--line);font-size:14px}
.row dt{font-weight:600}.row dd{margin:0}.row dd .mono{font-size:12.5px;color:var(--muted);overflow-wrap:anywhere}
ul.off{margin:0;padding:0;list-style:none}ul.off li{position:relative;padding:9px 0 9px 20px;border-top:1px solid var(--line);font-size:14px}
ul.off li::before{content:"";position:absolute;left:2px;top:17px;width:8px;height:8px;border-radius:2px;background:var(--warn)}
@media (max-width:880px){.figures{grid-template-columns:repeat(2,minmax(0,1fr))}.cols{grid-template-columns:1fr}h1{font-size:24px}}
</style>
<div class="wrap">
  <header class="top">
    <div>
      <p class="eyebrow">Aethron · replicate · no screenshot-specific values entered</p>
      <h1>__HEADING__</h1>
      <p class="lede">This page was produced by Aethron's replicate command, run untouched on one screenshot. Every colour, size and position came from measuring that screenshot; nothing was typed in for it.</p>
    </div>
    <div class="modes" role="group" aria-label="View">
      <button type="button" data-mode="live" aria-pressed="true">Live code</button>
      <button type="button" data-mode="original" aria-pressed="false">Original</button>
      <button type="button" data-mode="compare" aria-pressed="false">Compare</button>
      <button type="button" data-mode="diff" aria-pressed="false">Difference</button>
      <button type="button" data-mode="actual" aria-pressed="false">Actual size</button>
    </div>
  </header>
  <div class="stage" id="stage"><div class="canvas" id="canvas">
    <iframe id="live" src="site.html" title="The replicated page, as live code" scrolling="no"></iframe>
    <img id="original" src="original.png" alt="The original screenshot" hidden>
    <img id="overlay" src="original.png" alt="" hidden>
    <img id="diffmap" src="diff.png" alt="Difference map" hidden>
  </div><div id="divider" hidden></div></div>
  <div class="below">
    <span id="hint"></span>
    <label class="split" id="splitbox" hidden><span>Original</span><input id="split" type="range" min="0" max="100" value="50" aria-label="Divider position"><span>Code</span></label>
  </div>
  <div class="figures">__FIGURES__</div>
  <div class="cols">
    <section class="card"><h2>By element</h2><p class="sub">Share of pixels within 12 of 255 colour levels, and mean difference.</p><div class="scroll"><table class="mono" id="regions"></table></div></section>
    <section class="card"><h2>What was found, and from what</h2><p class="sub">Each element and the measurement it came from.</p><dl>__FOUND__</dl></section>
  </div>
  <section class="card"><h2>Still off</h2><ul class="off">__OFF__</ul></section>
</div>
<script>
(function(){
  var REGIONS=__REGIONS__;
  var t=document.getElementById("regions");
  REGIONS.forEach(function(r){var tr=document.createElement("tr");var cls=r.within12>=94?"good":(r.within12<85?"warn":"");
    tr.innerHTML='<td class="name"></td><td><div class="track"><i class="'+cls+'" style="width:'+r.within12+'%"></i></div></td><td class="num">'+r.within12.toFixed(1)+'%</td><td class="num">'+r.mean.toFixed(2)+'</td>';
    tr.querySelector(".name").textContent=r.name;t.appendChild(tr);});
  var W=__W__,H=__H__,stage=document.getElementById("stage"),canvas=document.getElementById("canvas"),actual=false;
  function fit(){if(actual){canvas.style.transform="";stage.style.height="";return;}var k=stage.clientWidth/W;canvas.style.transform="scale("+k+")";stage.style.height=(H*k)+"px";}
  fit();try{new ResizeObserver(fit).observe(stage);}catch(e){window.addEventListener("resize",fit);}
  var live=document.getElementById("live"),orig=document.getElementById("original"),ov=document.getElementById("overlay"),diff=document.getElementById("diffmap"),
      div=document.getElementById("divider"),split=document.getElementById("split"),box=document.getElementById("splitbox"),hint=document.getElementById("hint");
  var HINTS={live:"Live code: type in the text box, click the chips and links.",original:"The screenshot it was built from.",
    compare:"Original left of the line, live code right of it.",diff:"Orange: differs by more than 12 of 255 levels. Grey: within tolerance.",actual:"The live code at its real size. Scroll inside the frame."};
  function setSplit(){var v=split.value;ov.style.clipPath="inset(0 "+(100-v)+"% 0 0)";div.style.left=v+"%";}
  function mode(m){document.querySelectorAll(".modes button").forEach(function(b){b.setAttribute("aria-pressed",String(b.dataset.mode===m));});
    actual=m==="actual";stage.classList.toggle("actual",actual);
    live.hidden=!(m==="live"||m==="compare"||m==="actual");orig.hidden=m!=="original";ov.hidden=m!=="compare";div.hidden=m!=="compare";diff.hidden=m!=="diff";
    box.hidden=m!=="compare";hint.textContent=HINTS[m];if(m==="compare")setSplit();fit();}
  document.querySelectorAll(".modes button").forEach(function(b){b.addEventListener("click",function(){mode(b.dataset.mode);});});
  split.addEventListener("input",setSplit);mode("live");
})();
</script>
"""


def write_viewer(out, img, spec, report):
    g = report["grade"]
    chk = g["checklist"]
    figs = [(f"{g['background_within12']:.1f}%", "of background pixels within 12 levels, as real CSS gradients"),
            (f"{g['whole_within12']:.1f}%", "of the whole page within 12 levels"),
            (f"{chk.get('correct')} / {chk.get('expected')}", "lines of text in the right place and size, by their words"),
            ("0", "values entered by hand for this screenshot")]
    figures = "".join(f'<div class="fig"><div class="n mono">{esc(n)}</div><div class="l">{esc(l)}</div></div>' for n, l in figs)
    found = []
    for s in spec["surfaces"]:
        f = spec["fills"][s["id"]]
        e = spec["edges"][s["id"]]
        detail = f"{s['w']}×{s['h']} at {s['x']},{s['y']} · radius {s['r']} · {f['kind']}"
        if f["kind"] == "glass":
            detail += f" white {f['alpha'] * 100:.0f}%"
        if "highlight" in e:
            detail += " · top highlight"
        if "border" in e:
            detail += " · 1px border"
        found.append(("Surface", "Found from the words sitting on it; edges at the strongest colour step; "
                      "radius fitted to the corner; fill chosen by fit.", detail))
    for m in spec["marks"]:
        if m["kind"] == "rings":
            found.append(("Mark", "Ink the background does not explain, fitted as concentric rings.",
                          " · ".join(f"r {c['r']} {c['colour']}" for c in m["rings"]["rings"])))
        else:
            found.append(("Mark", "Ink no shape model explains — carried as an image.", f"{m['box'][2]}×{m['box'][3]}"))
    for c in spec["strokes"]:
        found.append(("Stroke", "A thin line traced column by column, colour and fade fitted.",
                      f"{c['kind']} · {len(c['points'])} points · {c['colour']}"))
    if spec["rules"]:
        found.append(("Faint lines", "Brighter or darker than both neighbours, band by band.",
                      " · ".join(f"{'x' if r['vertical'] else 'y'} {r['at']}" for r in spec["rules"])))
    hd = spec["fonts"]["head"]
    found.append(("Type", f"{len(FAMILIES)} families rendered side by side, scored by ink overlap; each line placed by reading both pages.",
                  f"{hd['fam']} {hd['wt']} {hd['ls']} · overlap {hd['iou']:.3f}"))
    found.append(("Background", "Base colour and radial gradients fitted to the pixels with the foreground masked.",
                  f"{len(spec['background']['model']['layers'])} layers"))
    found.append(("Website", "Links, heading, text box, buttons and form inferred from what the elements are.",
                  f"{g['semantic_pixels_unchanged']:.2f}% of pixels unchanged by the conversion"))
    found_html = "".join(f'<div class="row"><dt>{esc(a)}</dt><dd>{esc(b)} <span class="mono">{esc(c)}</span></dd></div>'
                         for a, b, c in found)
    off = []
    for fnd in chk.get("findings", [])[:6]:
        off.append(f"{fnd['kind'].title()}: “{fnd['text']}” — wanted {fnd.get('want')}, got {fnd.get('got')}.")
    worst = sorted(g["regions"], key=lambda r: r["within12"])[:3]
    for r in worst:
        if r["within12"] < 85:
            off.append(f"{r['name']} is {r['within12']:.1f}% within tolerance.")
    for m in spec["marks"]:
        if m["kind"] == "raster":
            off.append("A mark could not be described as shapes and is carried as an image.")
    off.append(f"Fixed {img.w}×{img.h} canvas, the screenshot's size; it scales down but does not reflow.")
    off_html = "".join(f"<li>{esc(o)}</li>" for o in off)
    heading = spec["lines"][spec["head_index"]]["text"] if spec["head_index"] is not None else img.path.stem
    html = (VIEWER.replace("__TITLE__", esc(f"{heading} replica")).replace("__HEADING__", esc(f"{heading}, replicated"))
            .replace("__W__", str(img.w)).replace("__H__", str(img.h)).replace("__FIGURES__", figures)
            .replace("__FOUND__", found_html).replace("__OFF__", off_html)
            .replace("__REGIONS__", json.dumps(g["regions"])))
    (out / "compare.html").write_text(html)


# ───────────────────────────── the whole run ─────────────────────────────

def replicate(image, out, fast=False):
    t0 = time.time()
    image, out = Path(image), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if not GR.find_browser():
        say("VERDICT: SKIPPED — no browser to render with; UNVERIFIED, not proven good")
        # THE REASON TRAVELS WITH THE VERDICT. A caller that only gets "SKIPPED" has to guess,
        # and the studio showed exactly that to a user with nothing to act on.
        return {"verdict": "SKIPPED", "why": "no browser to render with — install Chrome "
                                             "or Chromium; nothing was proven either way"}
    img = Img(image)
    R = Renderer(out, img.w, img.h)
    report = {"image": str(image), "size": [img.w, img.h], "hand_entered_values": 0, "stages": {}}
    say(f"── {image.name}: {img.w}x{img.h}")
    lines = read_lines(image)
    if not lines:
        say("VERDICT: SKIPPED — no text could be read (OCR unavailable or empty); UNVERIFIED")
        return {"verdict": "SKIPPED", "why": "no text could be read from the image — macOS "
                                             "text recognition is unavailable (aethron_ocr.swift "
                                             "missing or swiftc absent), or the picture has no "
                                             "words in it"}
    say(f"  lines: {len(lines)}")
    surfaces = find_surfaces(img, lines)
    say(f"  surfaces: {len(surfaces)} — " + ", ".join(f"{s['w']}x{s['h']} r{s['r']}" for s in surfaces))
    bg = fit_background(img, lines, surfaces, out, fast)
    if bg is None:
        say("VERDICT: FAILED — the background could not be fitted")
        return {"verdict": "FAILED"}
    marks = find_marks(img, bg["model"], lines, surfaces, out)
    say(f"  marks: {len(marks)} — " + ", ".join(m["kind"] for m in marks))
    strokes = find_strokes(img, lines, surfaces, marks)
    say(f"  strokes: {len(strokes)} — " + ", ".join(f"{c['kind']} {len(c['points'])} pts" for c in strokes))
    rules = find_rules(img, lines, surfaces, marks, strokes)
    say(f"  faint lines: {len(rules)} — " + ", ".join(f"{'x' if r['vertical'] else 'y'}{r['at']}" for r in rules))
    fills, edges = {}, {}
    for s in sorted(surfaces, key=lambda s: s["depth"]):
        fills[s["id"]] = surface_fill(img, bg["model"], s, lines, surfaces, fills)
        edges[s["id"]] = edge_light(img, s)
        say(f"  surface {s['w']}x{s['h']}: {fills[s['id']]['kind']} (rms {fills[s['id']].get('rms')}) {edges[s['id']]}")
    fonts = choose_fonts(R, img, lines)
    if fonts is None:
        say("VERDICT: FAILED — no typeface could be measured")
        return {"verdict": "FAILED"}
    items = initial_text(img, lines, fonts)
    head_index = next((i for i, l in enumerate(lines) if l is fonts["head_line"]), None)
    spec = {"W": img.w, "H": img.h, "title": lines[head_index]["text"] if head_index is not None else image.stem,
            "lines": lines, "items": items, "surfaces": surfaces, "background": bg, "marks": marks,
            "strokes": strokes, "rules": rules, "fills": fills, "edges": edges, "fonts": fonts,
            "shadows": {}, "head_index": head_index}
    html = emit(spec, text_block(items))
    first = G.compare(img.shot, V.load(R.page(html, "first")), step=3, tol=TOL, border=10)
    report["stages"]["first_render"] = round(first["within_tol"] * 100, 2)
    say(f"  first render: {first['within_tol'] * 100:.2f}% within 12")
    html = fit_type(R, img, spec, html, report)
    html = fit_shadows(R, img, spec, html)
    pre = R.page(html, "pre_semantic")
    site = emit(spec, text_of(html), semantic=True)
    (out / "site.html").write_text(site)
    png = R.page(site, "final")
    grade(img, spec, png, pre, report, out)
    (out / "original.png").write_bytes(image.read_bytes()) if image.suffix.lower() == ".png" else \
        GR.write_png(out / "original.png", img.w, img.h,
                     bytes(b for y in range(img.h) for x in range(img.w) for b in (*img.shot.rgb(x, y), 255)))
    report["found"] = {"lines": len(lines), "surfaces": [{k: s[k] for k in ("x", "y", "w", "h", "r")} | {"fill": fills[s["id"]]["kind"]} for s in surfaces],
                       "marks": [m["kind"] for m in marks], "strokes": len(strokes), "rules": len(rules),
                       "typeface": {k: fonts["head"][k] for k in ("fam", "wt", "ls", "iou")}}
    report["renders"] = R.n
    report["seconds"] = round(time.time() - t0)
    write_viewer(out, img, spec, report)
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    R.clean()
    g = report["grade"]
    say(f"\nVERDICT: BUILT — whole page {g['whole_within12']}% within 12 · background {g['background_within12']}% · "
        f"checklist {g['checklist']['correct']}/{g['checklist']['expected']} · {R.n} renders · {report['seconds']}s")
    say(f"  site: {out / 'site.html'}\n  viewer: {out / 'compare.html'}")
    report["verdict"] = "BUILT"
    return report


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    image = Path(argv[0])
    out = Path(argv[1]) if len(argv) > 1 and not argv[1].startswith("--") else image.parent / f"{image.stem}-replica"
    rep = replicate(image, out, fast="--fast" in argv)
    return 0 if rep.get("verdict") == "BUILT" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
