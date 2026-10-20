#!/usr/bin/env python3
"""Every line of text where the screenshot has it, in the face and size it has.

temple, 2026-10-20: "significantly invest in its ability to get elements'
placement and position/style, to make sure it gets it exactly as it
appears, so we know to only worry about the images … what matters is the
system and technology running it, not the model."

A model reading a screenshot guesses type: the paragraph came back set in
a monospace face, the headline 20% too small, the lines broken in other
places. None of that needs guessing. It is measured here, and then fixed
here, with no model at all:

  1. IDENTIFY THE TYPE (identify). Every line OCR read in the screenshot
     is set again in a lab page — one render — in ~30 web fonts across
     serif, sans, mono and handwriting, upright and italic, regular and
     semibold. Each sample's ink is compared with the screenshot's ink for
     that same text: shape (overlap once scaled to one height) and width
     for its height. The closest is the face; the size follows exactly,
     because the same words in the same face scale linearly: size =
     24px x screenshot ink height / lab ink height. The model is told all
     of it before it writes a line.
  2. MEASURE THE PAGE AS BUILT (measure_page): the render, read by the
     same OCR as the screenshot (same instrument both sides), and the DOM,
     so every line read is owned by an element with a data-ae-id.
  3. FIT (fit): face, then size, then letter-spacing and line breaks, then
     colour, then position — each pass rendered and scored, and KEPT ONLY
     IF IT MEASURES BETTER. The fitted rules live in one <style
     data-ae-fit> block a developer can read: face, colour and tracking at
     every width; sizes and nudges only at the design's own width, so the
     model's responsive layout still owns phones.

THE SCORE (placement): the share of the screenshot's lines that appear in
the built page within a few pixels of their place and within 12% of their
size, matched by their words. It is reported alongside the colour-and-
structure likeness, because a page can be the right colours everywhere
and still have every line in the wrong place.

`python3 aethron_fit.py <screenshot> <page.html>` fits a page in place;
`--fonts <screenshot>` prints the type; `--selftest` runs a known case.
"""
from __future__ import annotations

import json
import os
import math
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LAB_PX = 24                 # the lab sets every sample at this size
SIZE_TOL = 0.12             # a line is the right size within 12%
FIT_MIN = 0.8               # sizes and nudges apply from 80% of the design width

# family -> (class, weights available, has italic)
FONTS = {
    "Cormorant Garamond": ("serif", (400, 600), True),
    "Playfair Display": ("serif", (400, 600), True),
    "EB Garamond": ("serif", (400, 600), True),
    "Fraunces": ("serif", (400, 600), True),
    "Instrument Serif": ("serif", (400,), True),
    "DM Serif Display": ("serif", (400,), True),
    "Lora": ("serif", (400, 600), True),
    "Libre Baskerville": ("serif", (400, 700), True),
    "Newsreader": ("serif", (400, 600), True),
    "Source Serif 4": ("serif", (400, 600), True),
    "Inter": ("sans", (400, 600), True),
    "DM Sans": ("sans", (400, 600), True),
    "Manrope": ("sans", (400, 600), False),
    "Work Sans": ("sans", (400, 600), True),
    "Poppins": ("sans", (400, 600), True),
    "Space Grotesk": ("sans", (400, 600), False),
    "Outfit": ("sans", (400, 600), False),
    "Plus Jakarta Sans": ("sans", (400, 600), True),
    "IBM Plex Sans": ("sans", (400, 600), True),
    "Archivo": ("sans", (400, 600), True),
    "JetBrains Mono": ("mono", (400, 600), True),
    "IBM Plex Mono": ("mono", (400, 600), True),
    "Space Mono": ("mono", (400, 700), True),
    "DM Mono": ("mono", (400, 500), True),
    "Caveat": ("hand", (400, 600), False),
    "Kalam": ("hand", (400, 700), False),
    "Homemade Apple": ("hand", (400,), False),
    "Dancing Script": ("hand", (400, 600), False),
    "Shadows Into Light": ("hand", (400,), False),
    "Patrick Hand": ("hand", (400,), False),
}
FALLBACK = {"serif": "Georgia, serif", "sans": "system-ui, sans-serif",
            "mono": "ui-monospace, monospace", "hand": "cursive"}


def say(*a):
    print(*a, flush=True)


def font_link(families):
    """One <link> per family, asking only for the styles it really has —
    one wrong axis and Google refuses the whole request."""
    out = []
    for f in families:
        if f not in FONTS:
            continue
        _, ws, ital = FONTS[f]
        spec = ";".join([f"0,{w}" for w in ws] + ([f"1,{w}" for w in ws] if ital else []))
        q = f.replace(" ", "+") + f":ital,wght@{spec}"
        out.append(f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
                   f'family={q}&display=swap">')
    return "\n".join(out)


# ─────────────────────────── ink ───────────────────────────────────────

def _luma(p):
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def ink(img, x, y, w, h, pad=3, step=1):
    """The ink of one line of text inside a box: its tight box, a mask,
    and its colour. The ground is the box's own border; ink is whatever
    stands well away from it, on the side the text is."""
    x0, y0 = max(0, int(x) - pad), max(0, int(y) - pad)
    x1, y1 = min(img.w, int(x + w) + pad), min(img.h, int(y + h) + pad)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    W = img.w
    px = lambda xx, yy: img.rgb[(yy * W + xx) * 3:(yy * W + xx) * 3 + 3]
    border = []
    for xx in range(x0, x1, step):
        border += [_luma(px(xx, y0)), _luma(px(xx, y1 - 1))]
    for yy in range(y0, y1, step):
        border += [_luma(px(x0, yy)), _luma(px(x1 - 1, yy))]
    border.sort()
    ground = border[len(border) // 2]
    lums = [[_luma(px(xx, yy)) for xx in range(x0, x1, step)] for yy in range(y0, y1, step)]
    flat = sorted(v for r in lums for v in r)
    lo, hi = flat[len(flat) // 50], flat[-1 - len(flat) // 50]
    dark = (ground - lo) >= (hi - ground)          # dark ink on a light ground?
    span = (ground - lo) if dark else (hi - ground)
    if span < 18:
        return None
    thr = max(16, 0.42 * span)
    mask = [[(ground - v > thr) if dark else (v - ground > thr) for v in r] for r in lums]
    ys = [i for i, r in enumerate(mask) if any(r)]
    cols_any = [any(c) for c in zip(*mask)]
    xs = [j for j, v in enumerate(cols_any) if v]
    if not ys or not xs:
        return None
    by0, by1, bx0, bx1 = ys[0], ys[-1] + 1, xs[0], xs[-1] + 1
    # An underline is not a glyph: a nearly full row at the bottom, below
    # an empty one, makes a 6px caps link measure as tall as a paragraph.
    full = lambda i: sum(mask[i][bx0:bx1]) >= 0.85 * (bx1 - bx0)
    b = by1 - 1
    if b - by0 >= 3 and full(b):
        while b > by0 and full(b):
            b -= 1
        if not any(mask[b][bx0:bx1]):
            while b > by0 and not any(mask[b][bx0:bx1]):
                b -= 1
            by1 = b + 1
            xs = [j for j, v in enumerate(any(c) for c in zip(*mask[by0:by1])) if v]
            bx0, bx1 = xs[0], xs[-1] + 1
    m = [r[bx0:bx1] for r in mask[by0:by1]]
    # the ink's own colour: the most extreme third of the ink pixels
    cols = []
    for i in range(by0, by1):
        for j in range(bx0, bx1):
            if mask[i][j]:
                cols.append((lums[i][j], px(x0 + j * step, y0 + i * step)))
    cols.sort(key=lambda c: c[0], reverse=not dark)
    core = cols[:max(1, len(cols) // 3)]
    colour = tuple(sum(c[1][k] for c in core) / len(core) for k in range(3))
    return {"x": x0 + bx0 * step, "y": y0 + by0 * step, "w": (bx1 - bx0) * step,
            "h": (by1 - by0) * step, "mask": m, "colour": colour, "dark": dark}


def _grid(mask, gw, gh):
    """Coverage of a boolean mask on a gw x gh grid, 0..1 per cell."""
    h, w = len(mask), len(mask[0])
    out = []
    for gy in range(gh):
        ya, yb = gy * h // gh, max(gy * h // gh + 1, (gy + 1) * h // gh)
        for gx in range(gw):
            xa, xb = gx * w // gw, max(gx * w // gw + 1, (gx + 1) * w // gw)
            n = s = 0
            for yy in range(ya, yb):
                row = mask[yy]
                for xx in range(xa, xb):
                    s += row[xx]
                    n += 1
            out.append(s / n)
    return out


def shape_score(a, b):
    """How alike two inks are: overlap once scaled to one box, times how
    close their width-for-height is. 0..1."""
    ar_a, ar_b = a["w"] / a["h"], b["w"] / b["h"]
    gh = max(12, min(36, a["h"]))       # finer for big type: weight is in the strokes
    gw = max(4, min(320, round(gh * ar_a)))
    ga, gb = _grid(a["mask"], gw, gh), _grid(b["mask"], gw, gh)
    mn = sum(min(p, q) for p, q in zip(ga, gb))
    mx = sum(max(p, q) for p, q in zip(ga, gb)) or 1
    # how much of its own box the ink fills: the weight of the strokes
    da, db = sum(ga) / len(ga), sum(gb) / len(gb)
    dens = math.exp(-2.0 * abs(math.log(max(da, 1e-3) / max(db, 1e-3))))
    return (mn / mx) * math.exp(-2.5 * abs(math.log(ar_a / ar_b))) * dens


# ─────────────────────────── the lab ───────────────────────────────────

def _esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def lab_page(texts, variants, only=None, sizes=None):
    """One page setting every text in every variant (or only the variant
    indices only[text_i] allows), each at a known spot and at its own size
    (sizes[text_i], default LAB_PX): thin strokes antialias differently at
    8px and at 56px, so a sample is compared at the size it was read at.
    -> (html, cells[(text_i, variant_i, x, y, w, h, px)], W, H)."""
    cells, rows = [], []
    W = 0
    y = 8
    for ti, t in enumerate(texts):
        px = max(8, min(120, round(sizes[ti]))) if sizes else LAB_PX
        row_h = int(px * 1.7) + 4
        est = int(len(t) * px * 0.75) + 40
        W = max(W, est)
        for vi, (f, wt, st, ls) in enumerate(variants):
            if only is not None and vi not in only[ti]:
                continue
            cells.append((ti, vi, 10, y, est, row_h, px))
            rows.append(f'<div style="position:absolute;left:10px;top:{y}px;'
                        f"font-family:'{f}',{FALLBACK[FONTS[f][0]]};font-size:{px}px;"
                        f'font-weight:{wt};font-style:{st};letter-spacing:{ls}em;white-space:nowrap;'
                        f'line-height:{row_h}px">{_esc(t)}</div>')
            y += row_h
    H = y + 8
    W = min(W + 20, 4000)
    fams = sorted({variants[c[1]][0] for c in cells})
    html = ("<!doctype html><html><head><meta charset=utf-8>" + font_link(fams)
            + "<style>html,body{margin:0;background:#fff;color:#000}"
              "body{position:relative;width:%dpx;height:%dpx}</style></head><body>" % (W, H)
            + "".join(rows) + "</body></html>")
    return html, cells, W, H


def _shoot(html, W, H, work, name):
    import aethron_figma_grade as G
    import aethron_shot2code as S
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    p = work / f"{name}.html"
    p.write_text(html)
    out = G.shoot(p, W, H, work / f"{name}.png")
    return S.Img.read(out) if out else None


TRACKS = (0.0, 0.08, 0.16)          # em; tried only on all-caps lines


def _caps(t):
    letters = [c for c in t if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def _variants(classes=None, families=None, tracks=(0.0,)):
    out = []
    for f, (cls, ws, ital) in FONTS.items():
        if classes and cls not in classes:
            continue
        if families and f not in families:
            continue
        for wt in ws:
            for st in (("normal", "italic") if ital else ("normal",)):
                for ls in tracks:
                    out.append((f, wt, st, ls))
    return out


def identify(ref, lines, work, log=say, max_roles=10, floor=0.15):
    """The face, weight, style, tracking, size and colour of the screenshot's type.

    ref: Img at CSS pixels; lines: OCR lines in the same pixels.
    -> [{text, x, y, w, h (ink box), family, cls, weight, style, tracking
         (em), size, colour, score, role}]. A role whose best match scores
    under `floor` is left out: no reading beats a wrong one.
    """
    work = Path(work)
    read = []
    for ln in sorted(lines, key=lambda l: -l["h"]):
        t = (ln.get("text") or "").strip()
        if float(ln.get("confidence", 1)) < 0.5 or len(t) < 2:
            continue
        k = ink(ref, ln["x"], ln["y"], ln["w"], ln["h"])
        if not k or k["h"] < 6:
            continue
        read.append((t, k, ln))
    if not read:
        return []
    # ROLES, not lines: five nav links, or two lines of one paragraph, are
    # one face at one size. Lines join a role when their ink is the same
    # height and colour and they share their case (an underlined caps link
    # measures as tall as a paragraph line).
    roles = []
    for t, k, ln in read:
        for r in roles:
            h0, c0 = r["members"][0][1]["h"], r["members"][0][1]["colour"]
            if (abs(k["h"] - h0) <= 0.15 * h0 and math.dist(k["colour"], c0) < 60
                    and _caps(t) == r["caps"]):
                r["members"].append((t, k, ln))
                break
        else:
            roles.append({"caps": _caps(t), "members": [(t, k, ln)]})
    roles = roles[:max_roles]
    # The lab sets up to three lines of each role; their scores add up.
    todo, owner = [], []
    for ri, r in enumerate(roles):
        ms = sorted(r["members"], key=lambda m: -min(len(m[0]), 90))[:3]
        for t, k, _ in ms:
            todo.append((t[:90], k, None))
            owner.append(ri)
    texts = [t for t, _, _ in todo]
    t0 = time.time()

    def run(variants, allowed, name, sizes=None):
        only = [{vi for vi, v in enumerate(variants) if allowed(roles[owner[ti]], v)}
                for ti in range(len(todo))]
        html, cells, W, H = lab_page(texts, variants, only=only, sizes=sizes)
        img = _shoot(html, W, H, work, name)
        if img is None:
            return None
        sc = _score_cells(img, cells, todo, variants)
        per = [dict() for _ in roles]
        for ti in range(len(todo)):
            for vi, v in sc[ti].items():
                per[owner[ti]][vi] = per[owner[ti]].get(vi, 0.0) + v
        for ri in range(len(roles)):
            n = sum(1 for o in owner if o == ri) or 1
            per[ri] = {vi: v / n for vi, v in per[ri].items()}
        return per, sc

    # Stage 1: one or two families per class, every style — which KIND.
    reps = ("Lora", "Cormorant Garamond", "Inter", "Poppins", "JetBrains Mono",
            "Caveat", "Homemade Apple")
    v1 = _variants(families=reps, tracks=TRACKS)
    got = run(v1, lambda r, v: r["caps"] or v[3] == 0.0, "lab1")
    if got is None:
        log("the font lab could not render — type was not identified")
        return []
    s1, sc1 = got
    # Each text's size, from its role's stage-1 winner: stage 2 sets it at
    # that size, where its strokes antialias the way the screenshot's did.
    est = []
    for ti in range(len(todo)):
        ri = owner[ti]
        best = max(s1[ri].items(), key=lambda kv: kv[1])[0] if s1[ri] else None
        k1 = sc1.inks[ti].get(best) if best is not None else None
        est.append(k1["px"] * todo[ti][1]["h"] / k1["h"] if k1 else LAB_PX)
    # Stage 2: every family of the class that won each role, in its style.
    v2 = _variants(tracks=TRACKS)
    win = []
    for ri in range(len(roles)):
        if not s1[ri]:
            win.append(None)
            continue
        f1, _, st1, ls1 = v1[max(s1[ri].items(), key=lambda kv: kv[1])[0]]
        win.append((FONTS[f1][0], st1, ls1))
    allowed = lambda r, v: (win[roles.index(r)] is not None
                            and FONTS[v[0]][0] == win[roles.index(r)][0]
                            and v[2] == win[roles.index(r)][1]
                            and (v[3] == win[roles.index(r)][2]))
    got = run(v2, allowed, "lab2", sizes=est)
    if got is None:
        return []
    s2, sc2 = got
    out = []
    for ri, r in enumerate(roles):
        if not s2[ri]:
            continue
        vi, score = max(s2[ri].items(), key=lambda kv: kv[1])
        if score < floor:
            continue
        f, wt, st, ls = v2[vi]
        sizes = []
        for ti in range(len(todo)):
            if owner[ti] == ri and vi in sc2.inks[ti]:
                k2 = sc2.inks[ti][vi]
                sizes.append(k2["px"] * todo[ti][1]["h"] / k2["h"])
        size = sorted(sizes)[len(sizes) // 2] if sizes else None
        for mt, mk, _ in r["members"]:
            out.append({"text": mt, "x": mk["x"], "y": mk["y"], "w": mk["w"], "h": mk["h"],
                        "family": f, "cls": FONTS[f][0], "weight": wt, "style": st,
                        "tracking": ls, "size": round(size, 1) if size else None,
                        "colour": "#%02X%02X%02X" % tuple(int(round(c)) for c in mk["colour"]),
                        "score": round(score, 3), "role": ri + 1})
    log(f"type identified for {len(out)} line(s) in {len(roles)} role(s) "
        f"in {time.time() - t0:.0f}s")
    return out


class _Scores(list):
    inks = None


def _score_cells(img, cells, todo, variants):
    sc = _Scores({} for _ in todo)
    sc.inks = [dict() for _ in todo]
    for ti, vi, x, y, w, h, px in cells:
        k = ink(img, x, y, min(w, img.w - x), h, pad=0, step=1 if px < 30 else 2)
        if not k:
            continue
        k["px"] = px
        sc.inks[ti][vi] = k
        sc[ti][vi] = shape_score(todo[ti][1], k)
    return sc


# ─────────────────────────── the built page ────────────────────────────

FIT_JS = r"""
<script id="__ae_eye">
(function () {
  function shown(el) {
    for (var u = el; u && u.nodeType === 1; u = u.parentElement) {
      var c = getComputedStyle(u);
      if (c.display === 'none' || c.visibility === 'hidden') return false;
    }
    return true;
  }
  function run() {
    var out = [];
    var all = document.querySelectorAll('[data-ae-id]');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!shown(el)) continue;
      var txt = '', rects = [];
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
        txt = el.value || el.getAttribute('placeholder') || '';
        var b = el.getBoundingClientRect();
        if (txt) rects.push([b.left, b.top, b.width, b.height]);
      } else {
        for (var j = 0; j < el.childNodes.length; j++) {
          var n = el.childNodes[j];
          if (n.nodeType !== 3 || !n.nodeValue.trim()) continue;
          txt += n.nodeValue;
          var r = document.createRange();
          r.selectNodeContents(n);
          var rs = r.getClientRects();
          for (var k = 0; k < rs.length; k++)
            if (rs[k].width > 0.5 && rs[k].height > 0.5)
              rects.push([rs[k].left, rs[k].top, rs[k].width, rs[k].height]);
        }
      }
      txt = txt.replace(/\s+/g, ' ').trim();
      if (!txt || !rects.length) continue;
      var anc = [];
      for (var u = el.parentElement; u; u = u.parentElement)
        if (u.hasAttribute && u.hasAttribute('data-ae-id')) anc.push(u.getAttribute('data-ae-id'));
      var cs = getComputedStyle(el);
      out.push({id: el.getAttribute('data-ae-id'), tag: el.tagName.toLowerCase(), text: txt,
                rects: rects, anc: anc, fs: parseFloat(cs.fontSize), ff: cs.fontFamily,
                fw: cs.fontWeight, fst: cs.fontStyle, lh: cs.lineHeight,
                ls: cs.letterSpacing, color: cs.color, align: cs.textAlign,
                tt: cs.textTransform});
    }
    // Elements that draw a box of their own (a pill, a card, a button):
    // what sits inside one moves with it, never out of it.
    var boxes = [];
    for (var i2 = 0; i2 < all.length; i2++) {
      var e2 = all[i2], c2 = getComputedStyle(e2);
      if (!shown(e2) || e2 === document.body) continue;
      var bg = c2.backgroundColor, paints = (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent')
        || c2.backgroundImage !== 'none' || c2.boxShadow !== 'none'
        || parseFloat(c2.borderTopWidth) + parseFloat(c2.borderBottomWidth)
           + parseFloat(c2.borderLeftWidth) + parseFloat(c2.borderRightWidth) > 0;
      if (!paints) continue;
      var b2 = e2.getBoundingClientRect();
      boxes.push({id: e2.getAttribute('data-ae-id'), w: b2.width, h: b2.height});
    }
    document.documentElement.setAttribute('data-ae-eye',
      JSON.stringify({vw: innerWidth, items: out, boxes: boxes}));
  }
  var started = false;
  function once() {
    if (started) return; started = true;
    var go = function () { setTimeout(run, 250); };
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(go); else go();
  }
  if (document.readyState === 'complete') once(); else addEventListener('load', once);
  setTimeout(once, 1500);
})();
</script>
"""


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _sim(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = [a[i:i + 2] for i in range(len(a) - 1)] or [a]
    gb = [b[i:i + 2] for i in range(len(b) - 1)] or [b]
    pool = list(gb)
    hit = 0
    for g in ga:
        if g in pool:
            pool.remove(g)
            hit += 1
    return 2 * hit / (len(ga) + len(gb))


def ocr_lines(png_path, scale=1):
    """OCR lines in CSS px with their measured ink, or None."""
    import aethron_vision as V
    got = V.ocr(str(png_path))
    if got is None:
        return None
    return [{**ln, **{k: round(ln[k] / scale) for k in ("x", "y", "w", "h")}}
            for ln in got if float(ln.get("confidence", 1)) >= 0.5]


def measure_page(page, W, H, work, ocr=ocr_lines):
    """The page as built: its render, its lines (same OCR as the screenshot,
    with ink measured the same way), and which element owns each line."""
    import aethron_eye as EYE
    import aethron_shot2code as S
    import aethron_figma_grade as G
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    png = G.shoot(Path(page), W, H, work / "fit.png")
    if not png:
        return None
    img = S.Img.read(png)
    dom = EYE.read_page(page, W, H, probe=FIT_JS) or {}
    items = dom.get("items") or []
    lines = ocr(png) or []
    out = []
    for ln in lines:
        k = ink(img, ln["x"], ln["y"], ln["w"], ln["h"])
        box = {"x": k["x"], "y": k["y"], "w": k["w"], "h": k["h"]} if k else \
            {k2: ln[k2] for k2 in ("x", "y", "w", "h")}
        cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        owner, area = None, 1e18
        for it in items:
            for (rx, ry, rw, rh) in it["rects"]:
                if rx - 4 <= cx <= rx + rw + 4 and ry - 4 <= cy <= ry + rh + 4:
                    if rw * rh < area and _sim(it["text"], ln["text"]) > 0.15:
                        owner, area = it["id"], rw * rh
        out.append({"text": ln["text"], **box, "owner": owner,
                    "colour": k["colour"] if k else None})
    return {"img": img, "png": str(png), "lines": out, "items": items,
            "boxes": dom.get("boxes") or []}


def pair(ref_lines, built_lines, floor=0.55):
    """Match the screenshot's lines to the page's by their words."""
    cand = []
    for i, a in enumerate(ref_lines):
        for j, b in enumerate(built_lines):
            s = _sim(a["text"], b["text"])
            if s >= floor:
                d = abs(a["x"] - b["x"]) + abs(a["y"] - b["y"])
                cand.append((-s, d, i, j))
    cand.sort()
    ui, uj, out = set(), set(), []
    for _, _, i, j in cand:
        if i in ui or j in uj:
            continue
        ui.add(i)
        uj.add(j)
        out.append((ref_lines[i], built_lines[j]))
    return out


def _dx(a, b):
    """Horizontal error, read the way the line is set: by its left edge, its
    centre or its right edge, whichever the two lines share."""
    return min((b["x"] - a["x"], b["x"] + b["w"] / 2 - a["x"] - a["w"] / 2,
                b["x"] + b["w"] - a["x"] - a["w"]), key=abs)


def placement(ref_lines, built_lines, W):
    """How many of the screenshot's lines sit in the right place at the
    right size. -> {right, total, score (0..1, continuous), lines[...]}"""
    tol = max(4.0, 0.006 * W)
    pairs = pair(ref_lines, built_lines)
    got = {id(a): b for a, b in pairs}
    rows, err, right = [], 0.0, 0
    for a in ref_lines:
        b = got.get(id(a))
        if b is None:
            rows.append({"text": a["text"], "missing": True})
            err += 1
            continue
        dx, dy = _dx(a, b), b["y"] - a["y"]
        hr = b["h"] / max(1, a["h"])
        ok = abs(dx) <= tol and abs(dy) <= tol and abs(math.log(hr)) <= math.log(1 + SIZE_TOL)
        right += ok
        e = 0.6 * min(1, (abs(dx) + abs(dy)) / (10 * tol)) \
            + 0.4 * min(1, abs(math.log(hr)) / math.log(1.6))
        err += e
        rows.append({"text": a["text"], "dx": round(dx), "dy": round(dy),
                     "size": round(hr, 2), "ok": ok, "owner": b.get("owner")})
    n = max(1, len(ref_lines))
    return {"right": right, "total": len(ref_lines), "score": round(1 - err / n, 4),
            "lines": rows}


# ─────────────────────────── fitting ───────────────────────────────────

def _rules_css(rules, W, links=""):
    glob, scoped = [], []
    for eid, props in sorted(rules.items()):
        g = {k: v for k, v in props.items() if k in
             ("font-family", "font-weight", "font-style", "color", "letter-spacing")}
        s = {k: v for k, v in props.items() if k not in g}
        if g:
            glob.append(f'[data-ae-id="{eid}"]{{' + ";".join(
                f"{k}:{v} !important" for k, v in g.items()) + "}")
        if s:
            scoped.append(f'  [data-ae-id="{eid}"]{{' + ";".join(
                f"{k}:{v} !important" for k, v in s.items()) + "}")
    css = ["/* Fitted to the screenshot by Aethron: faces, sizes, tracking and"
           f" positions were measured, not guessed. Sizes and nudges apply at the"
           f" design's width ({W}px) and above {int(W * FIT_MIN)}px. */"] + glob
    if scoped:
        css += [f"@media (min-width: {int(W * FIT_MIN)}px) {{"] + scoped + ["}"]
    return (links + "\n" if links else "") + '<style data-ae-fit>\n' + "\n".join(css) + "\n</style>"


def apply_rules(html, rules, W, families=()):
    html = re.sub(r"(?s)<!--ae-fit-->.*?<!--/ae-fit-->\n?", "", html)
    if not rules:
        return html
    block = "<!--ae-fit-->" + _rules_css(rules, W, font_link(sorted(families))) + "<!--/ae-fit-->\n"
    if "</head>" in html:
        return html.replace("</head>", block + "</head>", 1)
    return block + html


def _merge(rules, eid, props):
    r = {k: dict(v) for k, v in rules.items()}
    r.setdefault(eid, {}).update(props)
    return r


def fit(html, ref, ref_lines, W, H, work, fonts=None, log=say, likeness_fn=None,
        ocr=ocr_lines, rounds=2):
    """Fit a page's type and placement to the screenshot. -> (html, report).

    Every pass is rendered and scored; a pass is kept only when the
    placement score rises and the page's look does not fall.
    """
    import aethron_shot2code as S
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    html = S.stamp_ids(html)
    page = work / "fit.html"
    fonts = fonts or []
    n_eval = [0]

    def evaluate(h):
        n_eval[0] += 1
        page.write_text(h)
        m = measure_page(page, W, H, work / f"m{n_eval[0]}", ocr=ocr)
        if m is None:
            return None
        pl = placement(ref_lines, m["lines"], W)
        lk = likeness_fn(m["img"]) if likeness_fn else None
        return {"m": m, "pl": pl, "look": lk}

    base = evaluate(html)
    if base is None:
        return html, {"verdict": "SKIPPED", "why": "the page could not be rendered"}
    report = {"before": {"placement": base["pl"]["score"], "right": base["pl"]["right"],
                         "total": base["pl"]["total"], "look": base["look"]},
              "passes": []}
    best, rules, fams = base, {}, set()
    kept = [(base, {}, set())]

    def better(c):
        if c is None:
            return False
        gain = c["pl"]["score"] - best["pl"]["score"]
        if gain <= 0.002:
            return False
        if best["look"] is not None and c["look"] is not None:
            # A right face moves everything below it, so the look can dip
            # until the positions follow; a big placement gain may cost a
            # little look here. The page handed back is checked at the end.
            if c["look"] < best["look"] - (0.05 if gain >= 0.15 else 0.01):
                return False
        return True

    def attempt(name, new_rules, new_fams):
        nonlocal best, rules, fams
        if new_rules == rules:
            return False
        c = evaluate(apply_rules(html, new_rules, W, new_fams))
        ok = better(c)
        report["passes"].append({"pass": name, "kept": ok,
                                 "placement": c["pl"]["score"] if c else None,
                                 "look": c["look"] if c else None})
        log(f"fit · {name}: placement {best['pl']['score']:.1%} -> "
            f"{(c['pl']['score'] if c else 0):.1%} — {'kept' if ok else 'not kept'}")
        if c and os.environ.get("AE_FIT_DEBUG"):
            for ln in c["pl"]["lines"]:
                if not ln.get("ok"):
                    log("     ", ln)
        if ok:
            best, rules, fams = c, new_rules, set(new_fams)
            kept.append((best, rules, set(fams)))
        return ok

    def owners():
        """element id -> [(ref line, built line)] on the current best page."""
        o = {}
        for a, b in pair(ref_lines, best["m"]["lines"]):
            if b.get("owner"):
                o.setdefault(b["owner"], []).append((a, b))
        return o

    items = lambda: {it["id"]: it for it in best["m"]["items"]}

    lab_sized = set()       # sizes the lab measured as font-sizes; ratios never override them

    # 1. FACE: the identified face, size and tracking of each element's
    # lines. The lab measured the size as a font-size, so it is set as one.
    if fonts:
        r, fa = dict(rules), set(fams)
        by_text = {_norm(f["text"]): f for f in fonts}
        its = items()
        for eid, ps in owners().items():
            votes, sizes, tracks = {}, [], []
            for a, _ in ps:
                f = by_text.get(_norm(a["text"]))
                if f and f["score"] >= 0.2:
                    key = (f["family"], f["weight"], f["style"], f["cls"])
                    votes[key] = votes.get(key, 0) + f["score"]
                    if f.get("size"):
                        sizes.append(f["size"])
                    tracks.append(f.get("tracking") or 0.0)
            if not votes:
                continue
            fam, wt, st, cls = max(votes.items(), key=lambda kv: kv[1])[0]
            props = {"font-family": f"'{fam}', {FALLBACK[cls]}",
                     "font-weight": str(wt), "font-style": st}
            it = its.get(eid)
            if sizes and it and it.get("fs"):
                fs = sorted(sizes)[len(sizes) // 2]
                if 4 <= fs <= 200:
                    props["font-size"] = f"{fs:.1f}px"
                    lab_sized.add(eid)
                    lh = it.get("lh") or ""
                    if lh.endswith("px"):
                        props["line-height"] = f"{float(lh[:-2]) * fs / it['fs']:.1f}px"
            tr = sorted(tracks)[len(tracks) // 2] if tracks else 0.0
            if tr:
                props["letter-spacing"] = f"{tr:.2f}em"
            r = _merge(r, eid, props)
            fa.add(fam)
        attempt("faces", r, fa)

    # 2. SIZE, then 3. TRACKING AND LINE BREAKS, then 4. COLOUR, then
    # 5. POSITION — twice, since each settles the next.
    for rnd in range(rounds):
        r = dict(rules)
        its = items()
        for eid, ps in owners().items():
            it = its.get(eid)
            if not it or (eid in lab_sized and eid in rules):
                continue
            ratios = sorted(a["h"] / max(1, b["h"]) for a, b in ps)
            q = ratios[len(ratios) // 2]
            if abs(q - 1) > 0.04 and 0.4 < q < 2.5:
                fs = it["fs"] * q
                props = {"font-size": f"{fs:.1f}px"}
                lh = it.get("lh") or ""
                if lh.endswith("px"):
                    props["line-height"] = f"{float(lh[:-2]) * q:.1f}px"
                r = _merge(r, eid, props)
        attempt(f"sizes (round {rnd + 1})", r, fams)

        r = dict(rules)
        its = items()
        for eid, ps in owners().items():
            it = its.get(eid)
            if not it:
                continue
            refs = [a for a, _ in ps]
            nbuilt = len(it["rects"])
            if len(ps) == 1 and nbuilt == 1:
                a, b = ps[0]
                chars = max(1, len(it["text"]) - 1)
                cur = it.get("ls") or "normal"
                cur = float(cur[:-2]) if cur.endswith("px") else 0.0
                d = (a["w"] - b["w"]) / chars
                if abs(d) > 0.08 and abs(a["w"] - b["w"]) > 3:
                    r = _merge(r, eid, {"letter-spacing": f"{cur + d:.2f}px"})
            elif nbuilt != len(refs) and refs:
                widest = max(a["w"] for a in refs)
                built_w = max(rw for _, _, rw, _ in it["rects"])
                if nbuilt > len(refs):
                    tw = max(widest, built_w) * 1.06
                else:
                    tw = widest * 1.04
                r = _merge(r, eid, {"max-width": f"{tw:.0f}px"})
                if it.get("align") == "center":
                    r[eid]["margin-left"] = "auto"
                    r[eid]["margin-right"] = "auto"
        attempt(f"tracking and line breaks (round {rnd + 1})", r, fams)

        r = dict(rules)
        its = items()
        for eid, ps in owners().items():
            it = its.get(eid)
            cols = [a.get("colour_rgb") for a, _ in ps if a.get("colour_rgb")]
            if not it or not cols:
                continue
            want = tuple(sum(c[k] for c in cols) / len(cols) for k in range(3))
            m = re.match(r"rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)", it["color"] or "")
            if not m:
                continue
            have = tuple(float(v) for v in m.groups())
            got = [b.get("colour") for _, b in ps if b.get("colour")]
            seen = tuple(sum(c[k] for c in got) / len(got) for k in range(3)) if got else have
            if math.dist(want, seen) > 28:
                fix = tuple(max(0, min(255, h + (w - s))) for h, w, s in zip(have, want, seen))
                r = _merge(r, eid, {"color": "#%02X%02X%02X" % tuple(int(round(v)) for v in fix)})
        attempt(f"colours (round {rnd + 1})", r, fams)

        r = dict(rules)
        its = items()
        # Text inside a drawn box (a pill, a button, a card) moves WITH the
        # box: nudging the words alone would carry them out of it.
        small = {b["id"] for b in best["m"].get("boxes", [])
                 if b["w"] * b["h"] < 0.3 * W * H}
        moves, anc_of = {}, {}
        for eid, ps in owners().items():
            if eid not in its:
                continue
            chain = its[eid]["anc"]
            unit, rest = eid, chain
            for i, a in enumerate(chain):
                if a in small:
                    unit, rest = a, chain[i + 1:]
                    break
            if eid in small:
                unit, rest = eid, chain
            anc_of[unit] = rest
            moves.setdefault(unit, []).extend(ps)
        need = {}
        for unit, ps in moves.items():
            dxs = sorted(_dx(b, a) for a, b in ps)
            dys = sorted(a["y"] - b["y"] for a, b in ps)
            need[unit] = (dxs[len(dxs) // 2], dys[len(dys) // 2])
        shift = {}
        for eid in sorted(need, key=lambda e: len(anc_of[e])):
            px_, py_ = need[eid]
            for a in anc_of[eid]:
                if a in shift:
                    px_ -= shift[a][0]
                    py_ -= shift[a][1]
            prev = rules.get(eid, {}).get("translate", "0px 0px").split()
            px_ += float(prev[0][:-2])
            py_ += float(prev[1][:-2]) if len(prev) > 1 else 0.0
            if (abs(px_) > 1.5 or abs(py_) > 1.5) and abs(px_) <= 160 and abs(py_) <= 160:
                shift[eid] = (px_, py_)
                r = _merge(r, eid, {"translate": f"{px_:.1f}px {py_:.1f}px"})
        attempt(f"positions (round {rnd + 1})", r, fams)

    # Never hand back a page that looks worse than the one given.
    if base["look"] is not None:
        for st in reversed(kept):
            if st[0]["look"] is None or st[0]["look"] >= base["look"] - 0.005:
                best, rules, fams = st
                break
    report["after"] = {"placement": best["pl"]["score"], "right": best["pl"]["right"],
                       "total": best["pl"]["total"], "look": best["look"]}
    report["lines"] = best["pl"]["lines"]
    report["renders"] = n_eval[0]
    report["render_png"] = best["m"]["png"]
    return apply_rules(html, rules, W, fams), report


def ref_lines_for(ref, lines):
    """The screenshot's lines with their ink measured (box and colour)."""
    out = []
    for ln in lines:
        if float(ln.get("confidence", 1)) < 0.5 or len((ln.get("text") or "").strip()) < 2:
            continue
        k = ink(ref, ln["x"], ln["y"], ln["w"], ln["h"])
        if not k:
            continue
        out.append({"text": ln["text"].strip(), "x": k["x"], "y": k["y"], "w": k["w"],
                    "h": k["h"], "colour_rgb": k["colour"]})
    return out


# ─────────────────────────── selftest ──────────────────────────────────

def _selftest():
    """A known page, rendered as the "screenshot"; the same page set wrong —
    wrong faces, sizes and offsets — must be measured and fitted back."""
    import aethron_shot2code as S
    import aethron_figma_grade as G
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (f" — {extra}" if extra else ""))

    if not G.find_browser():
        print("VERDICT: SKIPPED — no browser to render with")
        return 0
    import aethron_vision as V
    if V.ocr(str(ROOT / "tests/pages/dispatch.png")) is None:
        print("VERDICT: SKIPPED — no text recognition on this machine (" +
              (V.OCR_WHY[0] or "?") + ")")
        return 0
    work = Path(tempfile.mkdtemp(prefix="ae-fit-test-"))
    W, H = 900, 520
    truth = ("<!doctype html><html><head><meta charset=utf-8>"
             + font_link(["Playfair Display", "DM Sans"]) +
             "<style>body{margin:0;background:#F3EFE6;color:#1D2A22;text-align:center}"
             "h1{font-family:'Playfair Display',serif;font-weight:400;font-size:56px;"
             "line-height:1.1;margin:70px 0 18px}"
             "p{font-family:'DM Sans',sans-serif;font-size:17px;line-height:1.6;"
             "max-width:460px;margin:0 auto;color:#3E4A42}"
             "a{display:inline-block;margin-top:26px;font-family:'DM Sans';font-size:15px;"
             "color:#1D2A22}</style></head><body><main>"
             "<h1>Letters worth keeping</h1>"
             "<p>A short note every Sunday about the craft of making useful things, "
             "read by people who like it calm.</p><a href=#>Read the archive</a>"
             "</main></body></html>")
    draft = (truth.replace("font-family:'Playfair Display',serif", "font-family:monospace")
             .replace("font-size:56px", "font-size:40px")
             .replace("font-family:'DM Sans',sans-serif;font-size:17px",
                      "font-family:monospace;font-size:14px")
             .replace("max-width:460px", "max-width:620px")
             .replace("margin:70px 0 18px", "margin:40px 0 30px"))
    tp = work / "truth.html"
    tp.write_text(truth)
    png = G.shoot(tp, W, H, work / "truth.png")
    ref = S.Img.read(png)
    lines = ocr_lines(png)
    check("the screenshot's lines are read", lines and len(lines) >= 3, str(lines)[:200])
    fonts = identify(ref, lines, work / "lab", log=lambda *a: None)
    h1 = next((f for f in fonts if "Letters" in f["text"]), None)
    check("the headline's face is a serif", h1 and h1["cls"] == "serif", str(h1))
    check("the headline's size is measured within 8%",
          h1 and abs(h1["size"] - 56) / 56 < 0.08, str(h1 and h1["size"]))
    body = next((f for f in fonts if "Sunday" in f["text"] or "note" in f["text"]), None)
    check("the paragraph's face is a sans", body and body["cls"] == "sans", str(body))
    rl = ref_lines_for(ref, lines)
    look = lambda img: S.likeness(ref, img)["likeness"]
    out, rep = fit(draft, ref, rl, W, H, work / "fit", fonts=fonts,
                   log=(say if os.environ.get("AE_FIT_DEBUG") else (lambda *a: None)),
                   likeness_fn=look)
    if os.environ.get("AE_FIT_DEBUG"):
        for ln in rep.get("lines", []):
            say("  ", ln)
    b, a = rep["before"], rep["after"]
    check("the wrong draft is measured as wrong", b["right"] < b["total"], str(b))
    check("fitting raises placement", a["placement"] > b["placement"] + 0.15,
          f"{b['placement']:.0%} -> {a['placement']:.0%}")
    check("most lines end in the right place at the right size",
          a["right"] >= max(2, int(0.75 * a["total"])), f"{a['right']}/{a['total']}")
    check("the page does not look worse", a["look"] >= b["look"] - 0.005,
          f"{b['look']} -> {a['look']}")
    check("the fit is one readable block, faces global, sizes scoped",
          out.count("<style data-ae-fit>") == 1 and "@media (min-width:" in out
          and "fonts.googleapis.com" in out)
    again = apply_rules(out, {}, W)
    check("re-fitting replaces the block instead of stacking it",
          "data-ae-fit" not in again)
    print("VERDICT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    import aethron_shot2code as S
    if len(argv) >= 2 and argv[0] == "--fonts":
        src = S._as_png(argv[1])
        img = S.Img.read(src)
        W, H, scale = S.css_size(img.w, img.h)
        if scale != 1:
            gw, gh, cells = img.grid(W, H)
            img = S.Img(gw, gh, bytes(int(v) for c in cells for v in c))
        lines = ocr_lines(src, scale) or []
        for f in identify(img, lines, tempfile.mkdtemp(prefix="ae-fonts-")):
            print(json.dumps(f))
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = S._as_png(argv[0])
    img = S.Img.read(src)
    W, H, scale = S.css_size(img.w, img.h)
    if scale != 1:
        gw, gh, cells = img.grid(W, H)
        img = S.Img(gw, gh, bytes(int(v) for c in cells for v in c))
    lines = ocr_lines(src, scale)
    if lines is None:
        print("SKIPPED — no text recognition here")
        return 0
    work = Path(tempfile.mkdtemp(prefix="ae-fit-"))
    fonts = identify(img, lines, work / "lab")
    page = Path(argv[1])
    out, rep = fit(page.read_text(), img, ref_lines_for(img, lines), W, H, work, fonts=fonts,
                   likeness_fn=lambda r: S.likeness(img, r)["likeness"])
    page.write_text(out)
    print(json.dumps({k: rep[k] for k in ("before", "after", "passes", "renders")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
