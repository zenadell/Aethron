"""What in a screenshot is the thing to build.

A screenshot is rarely only the design. A Dribbble shot floats the app
window on a mountain photo; a screen grab keeps the desktop around the
window; a person circles the part they mean with a red pen. Aethron used
to rebuild all of it — the mountain became the page's background, and a
pen stroke would have become a curved div.

    frame(png) -> (png, info)

  * PEN MARKS. Thin, saturated, hand-drawn strokes (a circle, an arrow,
    a scribble) are found and painted out from what surrounds them, so
    nothing downstream ever sees them. A UI's own accent colour is kept:
    a stroke must be long, thin, sparse in its own box and not a straight
    rule before it counts as a mark.
  * THE SUBJECT. When the design is a card set on a backdrop (a photo, a
    flat colour, a desktop), its four edges are long straight steps that a
    photo never has. The largest box drawn by such edges is cut out, its
    rounded corners filled with its own edge colour, and the rest thrown
    away. It is cut only when that is safe: there is a margin on three
    sides, the box is at least a fifth of the picture, and nothing outside
    it is UI — OCR finds no text there, or (with no OCR) the margin is
    plainly empty. A full-page website with a card in the middle keeps
    everything.

Never raises: anything it cannot decide, it leaves as it was.
Stdlib only.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aethron_figma_grade as G      # noqa: E402

SMALL = 640          # detection runs on a picture this wide


def _hsv(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    s = 0 if mx == 0 else (mx - mn) / mx
    if mx == mn:
        h = 0.0
    elif mx == r:
        h = (60 * ((g - b) / (mx - mn)) + 360) % 360
    elif mx == g:
        h = 60 * ((b - r) / (mx - mn)) + 120
    else:
        h = 60 * ((r - g) / (mx - mn)) + 240
    return h, s, mx


class Px:
    def __init__(self, w, h, rgba):
        self.w, self.h, self.a = w, h, rgba

    def get(self, x, y):
        i = (y * self.w + x) * 4
        return self.a[i], self.a[i + 1], self.a[i + 2]

    def put(self, x, y, c):
        i = (y * self.w + x) * 4
        self.a[i], self.a[i + 1], self.a[i + 2], self.a[i + 3] = c[0], c[1], c[2], 255

    def small(self):
        f = max(1, math.ceil(self.w / SMALL))
        sw, sh = self.w // f, self.h // f
        rows = []
        for y in range(sh):
            yy = y * f + f // 2
            rows.append([self.get(x * f + f // 2, yy) for x in range(sw)])
        return f, sw, sh, rows

    def crop(self, x0, y0, x1, y1):
        out = bytearray((x1 - x0) * (y1 - y0) * 4)
        w = x1 - x0
        for y in range(y0, y1):
            s = (y * self.w + x0) * 4
            out[(y - y0) * w * 4:(y - y0 + 1) * w * 4] = self.a[s:s + w * 4]
        return Px(w, y1 - y0, out)


def _lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


# ─────────────────────────── pen marks ────────────────────────────────

def _is_ink(c):
    h, s, v = _hsv(*c)
    return s >= 0.62 and v >= 120


def marks(px: Px):
    """Components of saturated colour that look drawn on, not designed."""
    f, sw, sh, rows = px.small()
    ink = [[_is_ink(rows[y][x]) for x in range(sw)] for y in range(sh)]
    seen = [[False] * sw for _ in range(sh)]
    out = []
    for y0 in range(sh):
        for x0 in range(sw):
            if not ink[y0][x0] or seen[y0][x0]:
                continue
            stack, comp = [(x0, y0)], []
            seen[y0][x0] = True
            while stack:
                x, y = stack.pop()
                comp.append((x, y))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < sw and 0 <= ny < sh and ink[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
            n = len(comp)
            xs, ys = [p[0] for p in comp], [p[1] for p in comp]
            bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
            bw, bh = bx1 - bx0 + 1, by1 - by0 + 1
            if max(bw, bh) < 0.06 * sw or min(bw, bh) < 3 or n < 12:
                continue                      # a letter, an icon, a dot
            if n / (bw * bh) > 0.25:
                continue                      # a filled shape: a button
            edge = sum(1 for (x, y) in comp
                       if not all(0 <= x + a < sw and 0 <= y + b < sh and ink[y + b][x + a]
                                  for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1))))
            width = 2 * n / max(1, edge)      # stroke width, in small pixels
            if width * f > 18 or width > 0.25 * min(bw, bh):
                continue                      # thick, not a pen line
            onbox = sum(1 for (x, y) in comp
                        if x - bx0 <= 1 or bx1 - x <= 1 or y - by0 <= 1 or by1 - y <= 1)
            if onbox / n > 0.7:
                continue                      # a rectangle outline: a designed border
            hues = [_hsv(*rows[y][x])[0] for (x, y) in comp]
            hx = sum(math.cos(math.radians(h)) for h in hues)
            hy = sum(math.sin(math.radians(h)) for h in hues)
            out.append({"box": [bx0 * f, by0 * f, (bx1 + 1) * f, (by1 + 1) * f],
                        "seeds": [(x * f + f // 2, y * f + f // 2) for (x, y) in comp],
                        "hue": math.degrees(math.atan2(hy, hx)) % 360,
                        "pixels": n * f * f})
    return out


def paint_out(px: Px, mk):
    """Paint each mark out from what surrounds it, at full resolution."""
    removed = 0
    for m in mk:
        x0, y0, x1, y1 = m["box"]
        pad = 6
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(px.w, x1 + pad), min(px.h, y1 + pad)
        # only the stroke itself: flood from where it was seen, through its
        # own colour — an orange label beside a red arrow is not the arrow
        # (each seed carries its own colour: a red circle and a green arrow
        # that touch are two strokes, not one)
        def inked(q, h0):
            h, s, v = _hsv(*px.get(*q))
            return s >= 0.42 and v >= 90 and abs((h - h0 + 180) % 360 - 180) <= 16
        hole, stack = set(), []
        for q in m.get("seeds", []):
            if x0 <= q[0] < x1 and y0 <= q[1] < y1 and q not in hole:
                h, s, v = _hsv(*px.get(*q))
                if s >= 0.42 and v >= 90:
                    hole.add(q)
                    stack.append((q, h))
        while stack:
            (x, y), h0 = stack.pop()
            for a in (-1, 0, 1):
                for b in (-1, 0, 1):
                    q = (x + a, y + b)
                    if q not in hole and x0 <= q[0] < x1 and y0 <= q[1] < y1 and inked(q, h0):
                        hole.add(q)
                        stack.append((q, h0))
        grown = set(hole)                     # the antialiased fringe too
        for (x, y) in hole:
            for a in (-1, 0, 1):
                for b in (-1, 0, 1):
                    if x0 <= x + a < x1 and y0 <= y + b < y1:
                        grown.add((x + a, y + b))
        removed += len(grown)
        todo = grown
        for _ in range(60):                   # onion peel, outside in
            if not todo:
                break
            nxt, fills = set(), []
            for (x, y) in todo:
                acc, k = [0, 0, 0], 0
                for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                    q = (x + a, y + b)
                    if 0 <= q[0] < px.w and 0 <= q[1] < px.h and q not in todo:
                        c = px.get(*q)
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]
                        k += 1
                if k >= 2:
                    fills.append(((x, y), tuple(v // k for v in acc)))
                else:
                    nxt.add((x, y))
            for p, c in fills:
                px.put(p[0], p[1], c)
            if len(nxt) == len(todo):
                break
            todo = nxt
    return removed


# ─────────────────────────── the subject ──────────────────────────────

def _runs(vals, need):
    """Contiguous True runs (gaps of up to 2 bridged) at least `need` long."""
    out, start, gap = [], None, 0
    for i, v in enumerate(vals + [False, False, False]):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > 2:
                end = i - gap
                if end - start + 1 >= need:
                    out.append((start, end))
                start, gap = None, 0
    return out


def subject(px: Px):
    """The largest box drawn by four long straight edges, or None."""
    f, sw, sh, rows = px.small()
    L = [[_lum(c) for c in r] for r in rows]
    T = 16
    H, V = [], []
    for y in range(1, sh):
        for a, b in _runs([abs(L[y][x] - L[y - 1][x]) >= T for x in range(sw)], int(0.3 * sw)):
            H.append((y, a, b))
    for x in range(1, sw):
        for a, b in _runs([abs(L[y][x] - L[y][x - 1]) >= T for y in range(sh)], int(0.3 * sh)):
            V.append((x, a, b))
    tol = max(3, int(0.04 * sw))
    best = None
    for (yt, at, bt) in H:
        for (yb, ab, bb) in H:
            if yb - yt < 0.25 * sh or abs(at - ab) > tol or abs(bt - bb) > tol:
                continue
            x0, x1 = min(at, ab), max(bt, bb)
            span = yb - yt

            def side(xc, outward):
                hits = [x for (x, a, b) in V
                        if abs(x - xc) <= tol and min(b, yb) - max(a, yt) >= 0.5 * span]
                # a rounded card's top edge stops short of its sides: the
                # side is the vertical line itself, the outermost one
                return (min(hits) if outward < 0 else max(hits)) if hits else None
            sx0, sx1 = side(x0, -1), side(x1, 1)
            if sx0 is None or sx1 is None:
                continue
            x0, x1 = sx0, sx1 - 1
            area = (x1 - x0) * span
            if best is None or area > best[0]:
                best = (area, x0, yt, x1, yb)
    if not best:
        return None
    _, x0, y0, x1, y1 = best
    if (x1 - x0) * (y1 - y0) < 0.2 * sw * sh:
        return None
    m = 0.02
    margins = [x0 > m * sw, y0 > m * sh, x1 < (1 - m) * sw, y1 < (1 - m) * sh]
    if sum(margins) < 3:
        return None
    return [x0 * f, y0 * f, (x1 + 1) * f, (y1 + 1) * f], f


def _refine(px: Px, box, f):
    """Put each edge on the strongest step at full resolution."""
    x0, y0, x1, y1 = box
    mid = lambda a, b: range(a + (b - a) // 4, b - (b - a) // 4, max(1, (b - a) // 200))

    def best_row(yc, inside):
        cand = [y for y in range(max(1, yc - 2 * f), min(px.h, yc + 2 * f))]
        sc = lambda y: sum(abs(_lum(px.get(x, y)) - _lum(px.get(x, y - 1))) for x in mid(x0, x1))
        y = max(cand, key=sc)
        return y if inside > 0 else y          # first row of the subject / first row past it

    def best_col(xc):
        cand = [x for x in range(max(1, xc - 2 * f), min(px.w, xc + 2 * f))]
        sc = lambda x: sum(abs(_lum(px.get(x, y)) - _lum(px.get(x - 1, y))) for y in mid(y0, y1))
        return max(cand, key=sc)
    return [best_col(x0), best_row(y0, 1), best_col(x1), best_row(y1, -1)]


def _margin_is_empty(px: Px, box):
    """No OCR: the margin may be dropped only when it is plainly empty."""
    f, sw, sh, rows = px.small()
    x0, y0, x1, y1 = (v // f for v in box)
    L = [[_lum(c) for c in r] for r in rows]
    edges = total = 0
    for y in range(1, sh):
        for x in range(1, sw):
            if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
                continue
            total += 1
            if abs(L[y][x] - L[y][x - 1]) + abs(L[y][x] - L[y - 1][x]) >= 24:
                edges += 1
    return total == 0 or edges / total < 0.02


def _square_corners(px: Px):
    """The cut card's rounded corners still show the backdrop: fill them
    with the card's own edge colour."""
    w, h = px.w, px.h
    for cx, cy, sx, sy in ((0, 0, 1, 1), (w - 1, 0, -1, 1), (0, h - 1, 1, -1), (w - 1, h - 1, -1, -1)):
        ref = px.get(cx + sx * min(w // 2, 60), cy)          # the top/bottom edge, away from the corner
        refl = _lum(ref)
        d = 0
        while d < min(w, h) // 6 and abs(_lum(px.get(cx + sx * d, cy + sy * d)) - refl) > 18:
            d += 1
        r = int(d / (1 - 1 / math.sqrt(2))) + 2 if d else 0
        r = min(r, min(w, h) // 4)
        for y in range(r):
            for x in range(r):
                if (r - x) ** 2 + (r - y) ** 2 > r * r:
                    px.put(cx + sx * x, cy + sy * y, px.get(cx + sx * r, cy + sy * y)
                           if abs(_lum(px.get(cx + sx * r, cy + sy * y)) - refl) < 40 else ref)


def frame(png, out_dir, ocr_lines=None):
    """(png_to_build_from, info). ocr_lines: [{text,x,y,w,h}] in the
    picture's pixels, or None when there is no OCR."""
    info = {"cropped": None, "marks": 0}
    try:
        w, h, rgba = G.read_png(Path(png))
        px = Px(w, h, bytearray(rgba))
    except Exception as e:
        info["why"] = f"not read ({e})"
        return Path(png), info
    changed = False
    try:
        mk = marks(px)
        if mk:
            paint_out(px, mk)
            info["marks"] = len(mk)
            changed = True
    except Exception as e:
        info["marks_note"] = f"pen marks not checked ({e})"
    try:
        got = subject(px)
        if got:
            box, f = got
            box = _refine(px, box, f)
            x0, y0, x1, y1 = box
            ok, why = True, ""
            if callable(ocr_lines):          # read only when a subject was found
                try:
                    ocr_lines = ocr_lines()
                except Exception:
                    ocr_lines = None
            if ocr_lines is not None:
                chars_in = chars_out = 0
                for ln in ocr_lines:
                    cx = ln.get("x", 0) + ln.get("w", 0) / 2
                    cy = ln.get("y", 0) + ln.get("h", 0) / 2
                    n = len(str(ln.get("text", "")).strip())
                    if x0 <= cx <= x1 and y0 <= cy <= y1:
                        chars_in += n
                    else:
                        chars_out += n
                if chars_out > 0.05 * max(1, chars_in + chars_out):
                    ok, why = False, "there is text outside it, so it is part of the page"
            elif not _margin_is_empty(px, box):
                ok, why = False, "the margin is not empty and there is no OCR to read it"
            if ok:
                px = px.crop(x0, y0, x1, y1)
                _square_corners(px)
                info["cropped"] = [x0, y0, x1, y1]
                info["from"] = [w, h]
                changed = True
            else:
                info["kept_whole"] = why
    except Exception as e:
        info["subject_note"] = f"the subject was not looked for ({e})"
    if not changed:
        return Path(png), info
    out = Path(out_dir) / "framed.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    G.write_png(out, px.w, px.h, px.a)
    return out, info


def describe(info):
    """One line for the log and the model."""
    bits = []
    if info.get("cropped"):
        x0, y0, x1, y1 = info["cropped"]
        bits.append(f"the design is the {x1 - x0}x{y1 - y0} card in the screenshot; "
                    "the backdrop around it was left out")
    if info.get("marks"):
        bits.append(f"{info['marks']} pen mark(s) drawn over the screenshot were painted out")
    return "; ".join(bits)


if __name__ == "__main__":
    src = sys.argv[1]
    p, i = frame(src, Path(sys.argv[2]) if len(sys.argv) > 2 else Path(src).parent)
    print(p, i, describe(i))
