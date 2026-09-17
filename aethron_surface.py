#!/usr/bin/env python3
"""Surfaces found from what is INSIDE them, not from their edges.

Both box detectors in this project look for a boundary. On a glass card
there is not a boundary to find: its left side dissolves into a blue glow,
its fill is itself a gradient, and a soft shadow blurs its bottom. On the
owner's third template the solid-fill detector found 0 boxes and the
edge-based one produced 12 candidates — every one a letter of the
headline, bar the logo. The card never reached a gate.

What a surface always has is an INSIDE. The words on a card sit on the
card, so the pixels just beside those words belong to it. Flood outward
from there across colours that change only gently, step over the glyphs
themselves, and stop wherever the colour steps. A card's interior fills
exactly to its outline. The open page around a headline does not stop —
it runs on through a smooth gradient across most of the canvas — and is
rejected as not a surface.

Seeded by OCR, measured by the pixels. Nothing here is guessed from where
a card "should" be.
"""
import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _passable(lines, step, pad=2):
    cells = set()
    for ln in lines:
        x0, y0 = ln["x"] - pad, ln["y"] - pad
        x1, y1 = ln["x"] + ln["w"] + pad, ln["y"] + ln["h"] + pad
        for gy in range(int(y0) // step, int(y1) // step + 1):
            for gx in range(int(x0) // step, int(x1) // step + 1):
                cells.add((gx, gy))
    return cells


def flood(shot, seed, lines, step=3, tol=16, border=10, cap=0.45):
    """Every grid cell reachable from `seed` without crossing a step.

    Returns (cells, reason). A flood that grows past `cap` of the canvas
    is abandoned: that is open page, not a surface, and finishing it would
    only cost time.
    """
    gw, gh = shot.w // step, shot.h // step
    limit = int(cap * gw * gh)
    text = _passable(lines, step)
    sx, sy = seed[0] // step, seed[1] // step
    if not (0 <= sx < gw and 0 <= sy < gh) or (sx, sy) in text:
        return set(), "seed off canvas or on a glyph"
    lo, hi = border // step, None

    def colour(gx, gy):
        return shot.rgb(min(shot.w - 1, gx * step), min(shot.h - 1, gy * step))

    seen = {(sx, sy)}
    ref = {(sx, sy): colour(sx, sy)}
    q = deque([(sx, sy)])
    while q:
        cx, cy = q.popleft()
        here = ref[(cx, cy)]
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if (nx, ny) in seen:
                continue
            if nx < lo or ny < lo or nx >= gw - lo or ny >= gh - lo:
                continue
            if (nx, ny) in text:
                # STEP OVER THE WORDS: a glyph is a sharp step by nature,
                # and letting it stop the flood would carve every card
                # into the gaps between its lines. The colour carried
                # across is the colour the flood arrived with.
                seen.add((nx, ny))
                ref[(nx, ny)] = here
                q.append((nx, ny))
                continue
            c = colour(nx, ny)
            if (abs(c[0] - here[0]) + abs(c[1] - here[1])
                    + abs(c[2] - here[2])) > tol:
                continue
            seen.add((nx, ny))
            ref[(nx, ny)] = c
            q.append((nx, ny))
            if len(seen) > limit:
                return seen, "open page"
    return seen, "closed"


def _bbox(cells, step):
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    x0, y0 = min(xs) * step, min(ys) * step
    return {"x": x0, "y": y0, "w": (max(xs) + 1) * step - x0,
            "h": (max(ys) + 1) * step - y0}


def _iou(a, b):
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0.0


def _patch(shot, x, y, r=3):
    px = [shot.rgb(min(shot.w - 1, max(0, x + dx)), min(shot.h - 1, max(0, y + dy)))
          for dx in range(-r, r + 1, 2) for dy in range(-r, r + 1, 2)]
    return "#%02X%02X%02X" % tuple(sorted(p[i] for p in px)[len(px) // 2]
                                   for i in range(3))


def surfaces(shot, lines, step=3, tol=16, min_fill=0.72):
    """The card-like surfaces the page's words sit on, largest first."""
    found, dead = [], []
    for ln in lines:
        seeds = [(ln["x"] - 7, ln["y"] + ln["h"] // 2),
                 (ln["x"] + ln["w"] + 7, ln["y"] + ln["h"] // 2),
                 (ln["x"] + ln["w"] // 2, ln["y"] - 7),
                 (ln["x"] + ln["w"] // 2, ln["y"] + ln["h"] + 7)]
        for sd in seeds:
            if any(d[0] <= sd[0] // step <= d[2] and d[1] <= sd[1] // step <= d[3]
                   and (sd[0] // step, sd[1] // step) in d[4] for d in dead):
                continue
            if any(f["x"] <= sd[0] < f["x"] + f["w"]
                   and f["y"] <= sd[1] < f["y"] + f["h"]
                   and (sd[0] // step, sd[1] // step) in f["_cells"]
                   for f in found):
                continue
            cells, why = flood(shot, sd, lines, step, tol)
            if not cells:
                continue
            if why == "open page":
                xs = [c[0] for c in cells]
                ys = [c[1] for c in cells]
                dead.append((min(xs), min(ys), max(xs), max(ys), cells))
                continue
            box = _bbox(cells, step)
            area = (box["w"] // step) * (box["h"] // step)
            fill = len(cells) / max(1, area)
            if box["w"] < 24 or box["h"] < 16 or fill < min_fill:
                continue
            inside = [l["text"] for l in lines
                      if box["x"] <= l["x"] + l["w"] / 2 <= box["x"] + box["w"]
                      and box["y"] <= l["y"] + l["h"] / 2 <= box["y"] + box["h"]]
            if any(_iou(box, f) > 0.8 for f in found):
                continue
            box.update({"fill_ratio": round(fill, 3), "lines": inside,
                        "_cells": cells})
            found.append(box)
    out = []
    try:
        import aethron_boxes as BX
    except Exception:
        BX = None
    for f in sorted(found, key=lambda f: -f["w"] * f["h"]):
        cells = f.pop("_cells")
        inset = 12
        f["colours"] = {
            "top_left": _patch(shot, f["x"] + inset, f["y"] + inset),
            "top_right": _patch(shot, f["x"] + f["w"] - inset, f["y"] + inset),
            "bottom_left": _patch(shot, f["x"] + inset, f["y"] + f["h"] - inset),
            "bottom_right": _patch(shot, f["x"] + f["w"] - inset,
                                   f["y"] + f["h"] - inset),
        }
        if BX is not None:
            try:
                f["radius"] = BX.radius_of(shot, {k: f[k] for k in "xywh"})
            except Exception:
                f["radius"] = None
        out.append(f)
    return out


def main(argv):
    if not argv:
        print(__doc__.split("\n\n")[0])
        print("usage: aethron_surface.py <image> [ocr.json]")
        return 0
    import aethron_vision as V
    shot = V.load(argv[0])
    if len(argv) > 1:
        lines = json.loads(Path(argv[1]).read_text())
    else:
        lines = V.ocr(argv[0]) or []
    for s in surfaces(shot, lines):
        print(json.dumps(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
