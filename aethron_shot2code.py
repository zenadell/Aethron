#!/usr/bin/env python3
"""A screenshot becomes a real website: HTML, CSS and SVG, never a picture of itself.

WHY THIS EXISTS. 1.1.27 handed the owner a "rebuild" that was the screenshot
pasted in as the page's background, with two lines of real text laid on top.
The old path (aethron_vision.rebuild) carried the original's pixels on
purpose — a colour field of the whole image as the ground, and a crop of
anything text recognition could not read — so it scored 96% against the
screenshot while its nav, buttons, badges and orb were all one photograph.
A website that is a photograph of a website is not image-to-code.

THE SHAPE, and every step can be checked:

  1. MEASURE what can be measured from the image (canvas size, ground
     colour, palette, gradients, and — on a Mac — every line of text with
     its box). Numbers a model would otherwise guess.
  2. A MODEL WRITES the page from the picture plus those numbers: real
     <header>/<nav>/<a>/<button>/<h1>, gradients and shapes drawn in CSS or
     inline SVG, a layout that reflows.
  3. AETHRON CHECKS IT, and the model's opinion of its own page counts for
     nothing:
       - REUSE: the screenshot, or any crop of it bigger than an icon, used
         as an image or a background fails the page outright;
       - LIKENESS: the page is rendered at the screenshot's size and
         compared with it (colour and structure), region by region;
       - PHONES: rendered at 390 and 768 px wide, nothing may hang off the
         edge.
  4. REFINE: the model is shown the screenshot beside its own render, with
     the regions that differ most and every failed check, and writes the
     page again. The best page that passes REUSE is kept — never the last
     one tried — so a worse revision cannot replace a better one.

Stdlib only. `python3 aethron_shot2code.py <image> <outdir>` or
`--grade <image> <page.html>`; `--selftest` runs offline.
"""
from __future__ import annotations

import base64
import hashlib
import html as _html
import json
import math
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_figma_grade as G     # noqa: E402

# The largest picture a page may carry, in CSS pixels on either side. An
# icon or an avatar fits; a hero, a card or a background does not.
ICON = 128
# Below this the page does not look like the screenshot. MEASURED on the
# owner's Recognito screenshot (tests/pages/recognito.png): a black page
# scores 3%, the right text on black 11%, a flat orange-into-black gradient
# 32%, a hand-written CSS rebuild a person would call close 51%, and the
# 1.1.27 page that WAS the screenshot ~95%. Real CSS can never match a
# photographic screenshot pixel for pixel, so the bar sits between "the
# right colours somewhere" and "the right design".
LIKENESS_FLOOR = 0.45
GOOD_ENOUGH = 0.70
PHONE, TABLET = 390, 768

RULES = """\
You are a senior front-end developer. Build the website in the screenshot the way
you would ship it to a client — a real, working, responsive site, not a picture
of one. The page is checked against every rule below; a page that breaks one is
refused and sent back.

LOOK — the same design as the screenshot
1. ONE self-contained HTML file: inline <style>, inline <script> if needed.
   Google Fonts may be linked. Nothing else external.
2. At the screenshot's width the page must look like the screenshot: same
   layout, colours, type sizes, spacing and effects. Use the measured numbers
   below instead of guessing. Copy every word exactly.
3. Draw the INTERFACE with CSS or inline <svg>: backgrounds, gradients, glows
   (box-shadow, filter: blur), glass (backdrop-filter), pills, borders, grids,
   orbs, rings, icons. NEVER use the screenshot as an image: no <img>, url() or
   data: URI may show it or any part of it bigger than a 128px icon.
   PICTURES are different: a photograph, painting, illustration or video still
   is NOT drawn in code. Where the PICTURES list below names a region, place
   the picture file it names (slot-N.jpg) and describe it precisely in its alt;
   Aethron makes or finds the real picture. Shapes, gradients or blurred
   circles imitating a picture are taken out.

BUILT — what a human developer adds without being asked
4. Semantic structure: <header> with <nav> (links are <a href>), exactly one
   <main>, <section>s each led by a heading, exactly one <h1>, <footer> when the
   design has one, headings in order. Buttons are <button>, links are <a>.
5. Responsive by design, not by accident: a centred container (max-width),
   flex/grid layouts, fluid type with clamp(), and @media breakpoints at about
   900px and 600px where columns stack to one, spacing tightens and nothing
   scrolls sideways at 390px.
6. A real phone menu: when the nav has 4 or more links, below ~768px hide them
   behind a <button aria-expanded="false" aria-controls="…" aria-label="Open menu">
   that opens a panel showing every link (a few lines of JS toggling a class and
   aria-expanded; Escape and choosing a link close it).
7. Design tokens: colours, radii, spacing (a 4/8px scale) and fonts as custom
   properties in :root, used throughout. Body line-height ~1.5; long text no
   wider than ~70ch.
8. Interaction: every link and button has :hover, :active and :focus-visible
   states (a visible outline — never outline:none alone), cursor:pointer, and
   transitions of 150-250ms. Wrap motion in @media (prefers-reduced-motion).
9. Accessible: every field has a <label> (visually hidden is fine), icon-only
   buttons have aria-label, decorative SVG has aria-hidden="true", body text
   reaches 4.5:1 contrast (3:1 for large text) — if the screenshot's muted text
   is fainter, lift it just enough, keeping its hue.
10. Head: <html lang>, charset, viewport, a <title> and meta description written
    from the page's own words, Open Graph title/description, theme-color.
11. Links go somewhere: nav links point at the page's sections (#features),
    email at mailto:, the rest at sensible paths. Forms use the right input
    types (email, tel), autocomplete and required.
12. Nothing a person would spot as generated: no lorem ipsum, no placeholder
    copy, no sections that are not in the screenshot, clean indented code with
    short comments only where they help.
13. Reply with the complete HTML in one ```html block and nothing else.
"""


def say(*a):
    print(*a, flush=True)


# ─────────────────────────── pixels ──────────────────────────────────

class Img:
    """RGB pixels, composited over white where there was alpha."""

    def __init__(self, w, h, rgb):
        self.w, self.h, self.rgb = w, h, rgb

    @classmethod
    def read(cls, path):
        w, h, a = G.read_png(Path(path))
        rgb = bytearray(w * h * 3)
        for i in range(w * h):
            al = a[i * 4 + 3]
            for c in range(3):
                v = a[i * 4 + c]
                rgb[i * 3 + c] = v if al == 255 else (v * al + 255 * (255 - al)) // 255
        return cls(w, h, rgb)

    def crop(self, w, h):
        w, h = min(w, self.w), min(h, self.h)
        out = bytearray(w * h * 3)
        for y in range(h):
            out[y * w * 3:(y + 1) * w * 3] = \
                self.rgb[y * self.w * 3:y * self.w * 3 + w * 3]
        return Img(w, h, out)

    def grid(self, gw, gh=None):
        """Area-averaged down to gw x gh: each cell the mean of its block."""
        gh = gh or max(1, round(self.h * gw / self.w))
        out = []
        sx = max(1, self.w // (gw * 6))      # sample, a block need not be read whole
        for gy in range(gh):
            y0, y1 = gy * self.h // gh, max(gy * self.h // gh + 1, (gy + 1) * self.h // gh)
            for gx in range(gw):
                x0, x1 = gx * self.w // gw, max(gx * self.w // gw + 1, (gx + 1) * self.w // gw)
                r = g = b = n = 0
                for y in range(y0, y1, sx):
                    row = y * self.w * 3
                    for x in range(x0, x1, sx):
                        i = row + x * 3
                        r += self.rgb[i]; g += self.rgb[i + 1]; b += self.rgb[i + 2]
                        n += 1
                out.append((r / n, g / n, b / n))
        return gw, gh, out

    def png(self, path, max_w=1280):
        """Write a (downscaled) PNG — what a model is shown."""
        if self.w > max_w:
            gw, gh, cells = self.grid(max_w)
        else:
            gw, gh = self.w, self.h
            cells = [tuple(self.rgb[i:i + 3]) for i in range(0, len(self.rgb), 3)]
        rgba = bytearray(gw * gh * 4)
        for i, (r, g, b) in enumerate(cells):
            rgba[i * 4:i * 4 + 4] = bytes((int(r), int(g), int(b), 255))
        G.write_png(Path(path), gw, gh, rgba)
        return Path(path)


def css_size(w, h):
    """A Mac screenshot is taken at 2x: a 2880px-wide image is a 1440px page."""
    s = 2 if w >= 2200 else 1
    return round(w / s), round(h / s), s


def _luma(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _hex(c):
    return "#%02X%02X%02X" % tuple(int(round(v)) for v in c[:3])


def likeness(ref: Img, page: Img):
    """How much the render looks like the screenshot, 0..1, and where not.

    Two readings, because each alone is fooled: COLOUR on a coarse grid
    (is the right colour in the right place — a black page against an
    orange hero scores near 0) and STRUCTURE as SSIM on luminance (are the
    edges, text and shapes where they are — a flat average of the right
    colours scores near 0). Neither asks for pixel equality: a real page
    re-drawn in CSS can never match a JPEG-ish screenshot pixel for pixel,
    and it should not be marked as if it had to.
    """
    page = page.crop(ref.w, ref.h) if (page.w, page.h) != (ref.w, ref.h) else page
    gw = 48
    _, gh, a = ref.grid(gw)
    _, _, b = page.grid(gw, gh)
    diffs = [sum(abs(x - y) for x, y in zip(p, q)) / 3 for p, q in zip(a, b)]
    colour = max(0.0, 1 - (sum(diffs) / len(diffs)) / 60)
    sw = 160
    _, sh, la = ref.grid(sw)
    _, _, lb = page.grid(sw, sh)
    la = [_luma(c) for c in la]
    lb = [_luma(c) for c in lb]
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    vals = []
    win = 8
    for y0 in range(0, sh - win + 1, win // 2):
        for x0 in range(0, sw - win + 1, win // 2):
            xs = [la[(y0 + j) * sw + x0 + i] for j in range(win) for i in range(win)]
            ys = [lb[(y0 + j) * sw + x0 + i] for j in range(win) for i in range(win)]
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            vx = sum((v - mx) ** 2 for v in xs) / n
            vy = sum((v - my) ** 2 for v in ys) / n
            cxy = sum((p - mx) * (q - my) for p, q in zip(xs, ys)) / n
            vals.append(((2 * mx * my + C1) * (2 * cxy + C2))
                        / ((mx * mx + my * my + C1) * (vx + vy + C2)))
    structure = max(0.0, sum(vals) / max(1, len(vals)))
    # WHERE: a 4 x 3 grid of the page, worst first, with the colour each
    # region should be and the colour it is. The model can act on a named
    # region and two hex values; it cannot act on a single number.
    regions = []
    cols, rows = 4, 3
    names_y = ["top", "middle", "bottom"]
    names_x = ["left", "centre-left", "centre-right", "right"]
    for ry in range(rows):
        for rx in range(cols):
            cells = [(gy * gw + gx) for gy in range(ry * gh // rows, (ry + 1) * gh // rows)
                     for gx in range(rx * gw // cols, (rx + 1) * gw // cols)]
            if not cells:
                continue
            d = sum(diffs[c] for c in cells) / len(cells)
            ma = [sum(a[c][k] for c in cells) / len(cells) for k in range(3)]
            mb = [sum(b[c][k] for c in cells) / len(cells) for k in range(3)]
            regions.append({"region": f"{names_y[ry]} {names_x[rx]}",
                            "x": rx * ref.w // cols, "y": ry * ref.h // rows,
                            "w": ref.w // cols, "h": ref.h // rows,
                            "should_be": _hex(ma), "is": _hex(mb),
                            "difference": round(d, 1)})
    regions.sort(key=lambda r: -r["difference"])
    score = round(0.55 * colour + 0.45 * structure, 4)
    return {"likeness": score, "colour": round(colour, 4),
            "structure": round(structure, 4), "regions": regions}


# ─────────────────────────── reuse ───────────────────────────────────

def _raster_size(data: bytes):
    """(w, h) of a PNG, JPEG, GIF or WebP from its header, else None."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8X":
                return (1 + int.from_bytes(data[24:27], "little"),
                        1 + int.from_bytes(data[27:30], "little"))
            if data[12:16] == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
            if data[12:16] == b"VP8L":
                b = data[21:25]
                v = int.from_bytes(b, "little")
                return 1 + (v & 0x3FFF), 1 + ((v >> 14) & 0x3FFF)
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except Exception:
        return None
    return None


RASTER_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".heic", ".tif", ".tiff")
_REF = re.compile(r"""(?:url\(\s*['"]?|(?:src|href|poster|xlink:href)\s*=\s*['"])"""
                  r"""([^'")\s>]+)""", re.I)
_SRCSET = re.compile(r"""srcset\s*=\s*['"]([^'"]+)['"]""", re.I)
_DATA = re.compile(r"data:image/([a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)", re.I)


def reuse(html: str, page_dir, image) -> list:
    """Every way the page shows the screenshot, as findings. Empty = clean.

    Read from the SOURCE, so nothing can hide behind a render: every
    data: picture is decoded and measured, every local file a page points
    at is opened. The screenshot itself, by name or by its bytes, fails at
    any size; any other picture fails above icon size — a page has no
    honest way to come by a large picture except by cutting it out of the
    screenshot. A picture on another server is allowed and noted.
    """
    found = []
    image = Path(image) if image else None
    shot = image.read_bytes() if image and image.is_file() else b""
    sha = hashlib.sha1(shot).hexdigest() if shot else ""
    sw = sh = 0
    if shot:
        sz = _raster_size(shot)
        if sz:
            sw, sh = sz

    try:
        import aethron_pictures as PIC
        allowed = PIC.accepted(page_dir) if page_dir else {}
    except Exception:
        allowed = {}

    def judge(data, label):
        if hashlib.sha1(data).hexdigest() in allowed:
            return        # a picture Aethron made or found for one of the page's slots
        if sha and hashlib.sha1(data).hexdigest() == sha:
            found.append(f"{label} IS the screenshot — the page may not show it")
            return
        sz = _raster_size(data)
        if not sz:
            return
        w, h = sz
        if sw and abs(w / max(1, h) - sw / max(1, sh)) < 0.02 and w >= sw * 0.25:
            found.append(f"{label} is a {w}x{h} picture with the screenshot's own shape — "
                         "a scaled copy of the screenshot")
        elif w > ICON * 2 or h > ICON * 2:
            # A 2x icon is 256px in the file; anything bigger is a slab of the design.
            found.append(f"{label} is a {w}x{h} picture — bigger than an icon; draw it "
                         "with CSS or SVG instead of carrying pixels")

    for m in _DATA.finditer(html):
        try:
            data = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
        except Exception:
            continue
        judge(data, f"an embedded {m.group(1)} picture")
    refs = [m.group(1) for m in _REF.finditer(html)]
    for m in _SRCSET.finditer(html):
        refs += [p.strip().split()[0] for p in m.group(1).split(",") if p.strip()]
    base = Path(page_dir) if page_dir else None
    for ref in refs:
        if ref.startswith(("data:", "#", "javascript:", "mailto:", "tel:")):
            continue
        from urllib.parse import unquote
        clean = unquote(ref.split("?")[0].split("#")[0])
        if image and Path(clean).name == image.name:
            found.append(f"the page points at the screenshot itself ({ref})")
            continue
        if ref.startswith(("http://", "https://", "//")):
            continue
        if not clean.lower().endswith(RASTER_EXT) or base is None:
            continue
        p = (base / clean).resolve() if not clean.startswith("/") else Path(clean)
        if p.is_file():
            judge(p.read_bytes(), f"the file {clean}")
    return found


def external_pictures(html):
    return sorted({m.group(1) for m in _REF.finditer(html)
                   if m.group(1).startswith(("http://", "https://"))
                   and m.group(1).split("?")[0].lower().endswith(RASTER_EXT)})


# ─────────────────────────── the page ────────────────────────────────

def extract_html(text: str):
    """The page out of a model's reply, or None."""
    m = re.search(r"```(?:html)?\s*\n(.*?)```", text, re.S | re.I)
    body = m.group(1) if m else text
    i = body.lower().find("<!doctype")
    if i < 0:
        i = body.lower().find("<html")
    j = body.lower().rfind("</html>")
    if i < 0 or j < 0:
        return None
    return body[i:j + 7]


def visible_text(html):
    h = re.sub(r"(?is)<(script|style|svg)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(h)).strip()


_STAMP_TAGS = ("header", "nav", "a", "button", "h1", "h2", "h3", "h4", "h5", "h6",
               "p", "span", "div", "section", "main", "footer", "ul", "li", "img",
               "svg", "form", "input", "label", "article", "aside", "figure", "strong", "em")


def stamp_ids(html: str) -> str:
    """Give every element in the body a data-ae-id, so a person can point at
    it in the preview and remove or change it. An attribute changes nothing
    on screen. Inside <svg>, <script> and <style> nothing is stamped."""
    lo = html.lower()
    b0 = lo.find("<body")
    if b0 < 0:
        return html
    taken = set(re.findall(r'data-ae-id="([^"]+)"', html))
    n = [0]
    out, i = [html[:b0]], b0
    skip = None
    tag_re = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>")
    for m in tag_re.finditer(html, b0):
        out.append(html[i:m.start()])
        i = m.end()
        close, tag, rest = m.group(1), m.group(2).lower(), m.group(3)
        tok = m.group(0)
        if skip:
            if close and tag == skip:
                skip = None
            out.append(tok)
            continue
        if not close and tag in ("svg", "script", "style", "template"):
            if tag != "svg" or "/>" not in tok[-2:]:
                skip = tag
        if (not close and tag in _STAMP_TAGS and tag != "body"
                and "data-ae-id" not in rest):
            while True:
                n[0] += 1
                eid = f"e{n[0]:03d}"
                if eid not in taken:
                    break
            tok = f"<{m.group(2)} data-ae-id=\"{eid}\"{rest}>"
        out.append(tok)
    out.append(html[i:])
    return "".join(out)


# ─────────────────────────── measuring ───────────────────────────────

def facts(image, W, H, scale):
    """The numbers a model would otherwise guess, in CSS pixels."""
    out = {"canvas": f"{W} x {H} CSS px (the screenshot is {W * scale} x {H * scale}"
           + (", taken at 2x" if scale == 2 else "") + ")"}
    try:
        import aethron_vision as V
        rep = V.measure(str(image), deep=False)
        out["background"] = rep["background"].get("hex")
        out["palette"] = [f"{p['hex']} {p.get('share', 0) * 100:.1f}%"
                          if isinstance(p.get("share"), float) else p.get("hex")
                          for p in rep.get("palette", [])[:10]]
        grads = []
        for g in rep.get("gradients", [])[:6]:
            box = {k: round(v / scale) for k, v in (g.get("box") or {}).items()}
            grads.append({**box, "from": g.get("from"), "to": g.get("to"),
                          "note": (g.get("note") or g.get("axis") or "")[:120]})
        if grads:
            out["gradients"] = grads
    except BaseException as e:            # measuring is help, never a gate
        out["measure_note"] = f"colours were not measured ({e})"
    try:
        import aethron_vision as V
        lines = V.ocr(str(image)) or []
        txt = []
        for ln in lines:
            if float(ln.get("conf", 1)) < 0.5:
                continue
            txt.append({"text": ln.get("text"),
                        "x": round(ln.get("x", 0) / scale), "y": round(ln.get("y", 0) / scale),
                        "w": round(ln.get("w", 0) / scale),
                        "cap_height": round(ln.get("h", 0) / scale)})
        if txt:
            out["text"] = txt
        else:
            out["text_note"] = ("text was not measured here (" +
                                (V.OCR_WHY[0] if getattr(V, "OCR_WHY", None) else "no OCR")
                                + ") — read it off the picture")
    except BaseException as e:
        out["text_note"] = f"text was not measured ({e})"
    return out


def render(page: Path, W, H, out: Path):
    """The page at the screenshot's size, as pixels, or None."""
    shot = G.shoot(page, W, H, out)
    return Img.read(shot) if shot else None


def phones(page: Path):
    """[(width, scroll_width)] where the page hangs off the edge."""
    try:
        import aethron_eye as EYE
    except Exception:
        return None, []
    bad, ran = [], False
    for w in (PHONE, TABLET):
        r = EYE.read_page(page, w, 900)
        if not r:
            continue
        ran = True
        if r.get("scroll", 0) > r.get("vw", w) + 2:
            bad.append((w, r["scroll"]))
    return ran, bad



# ─────────────────── built like a website, not a picture ───────────────
# temple, 2026-10-19: "it shouldn't treat this as just another image to
# code that transfers a picture into code. Treat it as a website,
# automatically adding responsiveness and all the things websites need.
# It shouldn't mimic AI. It should mimic a human."
#
# So a page is held to what a senior front-end developer ships without
# being asked: a phone layout with a real menu, breakpoints, landmarks,
# one h1, every control named, readable contrast, hover and keyboard
# focus states, reduced motion, design tokens, and the head a search
# engine and a share card read. What can be added without changing how
# the page LOOKS is added by Aethron itself (finish_site); what needs
# design judgement is a finding the model must fix.

CRAFT_JS = r"""
<script id="__ae_eye">
(function () {
  function shown(el) {
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    for (var u = el; u && u.nodeType === 1; u = u.parentElement) {
      var c = getComputedStyle(u);
      if (c.display === 'none' || c.visibility === 'hidden'
          || parseFloat(c.opacity) < 0.05) return false;
    }
    return r.right > 0 && r.left < innerWidth;
  }
  function own(el) {
    var s = '';
    for (var i = 0; i < el.childNodes.length; i++)
      if (el.childNodes[i].nodeType === 3) s += el.childNodes[i].nodeValue;
    return s.replace(/\s+/g, ' ').trim();
  }
  function rgba(s) {
    var m = (s || '').match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    var p = m[1].split(/[ ,\/]+/).filter(Boolean).map(parseFloat);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  }
  function over(top, under) {
    var a = top[3];
    return [top[0] * a + under[0] * (1 - a), top[1] * a + under[1] * (1 - a),
            top[2] * a + under[2] * (1 - a), 1];
  }
  function lum(c) {
    var v = c.slice(0, 3).map(function (x) {
      x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
  }
  function ground(el) {
    // the colour behind this text, blended up through translucent layers;
    // null when a gradient or picture is behind it (then it is not judged)
    var layers = [];
    for (var u = el; u && u.nodeType === 1; u = u.parentElement) {
      var c = getComputedStyle(u);
      if (c.backgroundImage && c.backgroundImage !== 'none') return null;
      var b = rgba(c.backgroundColor);
      if (b && b[3] > 0) { layers.push(b); if (b[3] >= 0.99) break; }
    }
    var g = [255, 255, 255, 1];
    for (var i = layers.length - 1; i >= 0; i--) g = over(layers[i], g);
    return g;
  }
  function name(el) {
    var t = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    if (t) return t;
    var by = el.getAttribute('aria-labelledby');
    if (by && document.getElementById(by)) return document.getElementById(by).textContent.trim();
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) {
      if (el.labels && el.labels.length) return el.labels[0].textContent.trim();
      if (el.type === 'submit' || el.type === 'button') return (el.value || '').trim();
      return '';
    }
    var s = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (s) return s;
    var img = el.querySelector('img[alt]:not([alt=""]), svg title');
    return img ? (img.getAttribute('alt') || img.textContent || '').trim() : '';
  }
  function navLinks() {
    var nav = document.querySelector('header nav') || document.querySelector('nav');
    if (!nav) return null;
    var all = [].slice.call(nav.querySelectorAll('a'));
    var vis = all.filter(shown);
    var tops = [];
    vis.forEach(function (a) {
      var t = Math.round(a.getBoundingClientRect().top);
      if (!tops.some(function (y) { return Math.abs(y - t) < 10; })) tops.push(t);
    });
    return {total: all.length, shown: vis.length, rows: tops.length,
            texts: vis.map(function (a) { return a.textContent.replace(/\s+/g, ' ').trim(); }),
            over: vis.filter(function (a) { return a.getBoundingClientRect().right > innerWidth + 1; }).length};
  }
  function toggle() {
    var c = [].slice.call(document.querySelectorAll('button, [role=button]'));
    for (var i = 0; i < c.length; i++) {
      var b = c[i];
      if (!shown(b)) continue;
      var lab = (b.getAttribute('aria-label') || '') + ' ' + b.textContent + ' ' + (b.className || '');
      if (b.hasAttribute('aria-expanded') || b.hasAttribute('aria-controls') || /menu|navigation|nav-toggle|burger/i.test(lab))
        return b;
    }
    return null;
  }
  function visibleLinkTexts() {
    return [].slice.call(document.querySelectorAll('a')).filter(shown)
      .map(function (a) { return a.textContent.replace(/\s+/g, ' ').trim(); });
  }
  function measure() {
    var d = document.documentElement, out = {vw: innerWidth, scroll: Math.round(d.scrollWidth),
      height: Math.round(d.scrollHeight)};
    out.nav = navLinks();
    var unnamed = [];
    [].slice.call(document.querySelectorAll('a[href], button, input:not([type=hidden]), textarea, select'))
      .forEach(function (el) {
        if (!shown(el)) return;
        if (!name(el)) unnamed.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
          + (el.className && typeof el.className === 'string' ? '.' + el.className.split(/\s+/)[0] : ''));
      });
    out.unnamed = unnamed.slice(0, 8);
    out.unnamed_n = unnamed.length;
    var low = [], sizes = {};
    [].slice.call(document.body.querySelectorAll('*')).forEach(function (el) {
      var t = own(el);
      if (!t || t.length < 2 || !shown(el)) return;
      var cs = getComputedStyle(el), fg = rgba(cs.color), g = ground(el);
      var fs = parseFloat(cs.fontSize);
      sizes[Math.round(fs)] = (sizes[Math.round(fs)] || 0) + t.length;
      if (!fg || !g) return;
      var f = over(fg, g), a = lum(f), b = lum(g);
      var ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      var big = fs >= 24 || (fs >= 18.6 && parseInt(cs.fontWeight, 10) >= 700);
      if (ratio < (big ? 3 : 4.5))
        low.push({text: t.slice(0, 50), ratio: Math.round(ratio * 100) / 100, size: Math.round(fs), big: big});
    });
    low.sort(function (a, b) { return a.ratio - b.ratio; });
    out.low = low.slice(0, 8);
    out.sizes = Object.keys(sizes).length;
    out.main = document.querySelectorAll('main, [role=main]').length;
    out.h1 = [].slice.call(document.querySelectorAll('h1')).filter(shown).length;
    out.links = visibleLinkTexts();
    return out;
  }
  function done(m) { document.documentElement.setAttribute('data-ae-eye', JSON.stringify(m)); }
  function run() {
    var m;
    try { m = measure(); } catch (e) { done({error: String(e)}); return; }
    var t = toggle();
    if (!t) { done(m); return; }
    m.toggle = {label: name(t), expanded: t.getAttribute('aria-expanded'),
                controls: t.getAttribute('aria-controls')};
    if (innerWidth >= 700) { done(m); return; }
    try { t.click(); } catch (e) {}
    setTimeout(function () {
      m.toggle.expanded_after = t.getAttribute('aria-expanded');
      m.toggle.links_after = visibleLinkTexts();
      done(m);
    }, 500);
  }
  var started = false;
  function once() { if (started) return; started = true; setTimeout(run, 350); }
  if (document.readyState === 'complete') once(); else addEventListener('load', once);
  setTimeout(once, 900);
})();
</script>
"""

HEAD_TAGS = (
    ("doctype", r"<!doctype\s+html", "<!doctype html>"),
    ("lang", r"<html[^>]*\slang\s*=\s*['\"]?[a-z]", "<html lang>"),
    ("charset", r"<meta[^>]+charset", "<meta charset>"),
    ("viewport", r"<meta[^>]+name\s*=\s*['\"]viewport['\"][^>]*width\s*=\s*device-width"
                 r"|<meta[^>]+width\s*=\s*device-width[^>]*name\s*=\s*['\"]viewport", "viewport meta"),
    ("title", r"<title>\s*[^<\s][^<]*</title>", "<title>"),
    ("description", r"<meta[^>]+name\s*=\s*['\"]description['\"]", "meta description"),
    ("og", r"<meta[^>]+property\s*=\s*['\"]og:title['\"]", "Open Graph tags"),
)


def _css(html):
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I)) + "\n" + \
        " ".join(re.findall(r"""\sstyle\s*=\s*["']([^"']*)["']""", html, re.I))


def static_craft(html):
    """What a reader of the SOURCE can tell: (findings, notes, checks).

    checks is [(label, ok)] — the list a person sees under the page."""
    find, notes, checks = [], [], []
    low = html.lower()
    css = _css(html)
    missing = [lab for _, rx, lab in HEAD_TAGS if not re.search(rx, html, re.I)]
    checks.append(("head: title, description, viewport, language, share card", not missing))
    if missing:
        find.append("the <head> is missing " + ", ".join(missing) + " — every real site has them")
    media = re.findall(r"@media[^{]*\((?:max|min)-width\s*:\s*([\d.]+)(px|em|rem)", css, re.I)
    checks.append((f"responsive breakpoints ({len(media)} @media rule(s))", bool(media)))
    if not media:
        find.append("the page has no breakpoints (@media (max-width: …)) — a phone gets the "
                    "desktop layout squeezed; add at least ~900px and ~600px breakpoints")
    hover = re.search(r":hover", css)
    focus = re.search(r":focus-visible|:focus\b", css)
    checks.append(("hover and keyboard-focus states", bool(hover and focus)))
    if not hover:
        find.append("links and buttons have no :hover state")
    if not focus:
        find.append("links and buttons have no :focus-visible style — keyboard users cannot "
                    "see where they are")
    if re.search(r"outline\s*:\s*(none|0)\b", css) and not focus:
        find.append("focus outlines are removed (outline: none) with nothing in their place")
    moving = re.search(r"@keyframes|animation\s*:|transition\s*:", css, re.I)
    rm = re.search(r"prefers-reduced-motion", css, re.I)
    if moving:
        checks.append(("respects reduced motion", bool(rm)))
        if not rm:
            find.append("the page animates but ignores prefers-reduced-motion")
    tokens = len(set(re.findall(r"--[\w-]+\s*:", "".join(re.findall(r":root\s*{([^}]*)}", css)))))
    checks.append((f"design tokens in :root ({tokens} custom properties)", tokens >= 3))
    if tokens < 3:
        find.append("no design tokens — put the palette, radii and spacing in :root custom "
                    "properties (--bg, --text, --accent, --radius, --space-*) and use them")
    imgs = re.findall(r"<img\b[^>]*>", html, re.I)
    noalt = [i for i in imgs if not re.search(r"\salt\s*=", i, re.I)]
    if noalt:
        find.append(f"{len(noalt)} <img> without alt text")
    svgs = re.findall(r"<svg\b[^>]*>", html, re.I)
    loose = [s for s in svgs if not re.search(r"aria-hidden|role\s*=|aria-label", s, re.I)]
    if len(loose) > 0:
        notes.append(f"{len(loose)} inline <svg> without aria-hidden or a label")
    hrefs = re.findall(r"<a\b[^>]*>", html, re.I)
    dead = [a for a in hrefs if not re.search(r"\shref\s*=\s*['\"][^'\"]", a, re.I)]
    if dead:
        find.append(f"{len(dead)} <a> without a destination — every link needs an href "
                    "(#section anchors, mailto:, or a page)")
    hashes = [a for a in hrefs if re.search(r"\shref\s*=\s*['\"]#['\"]", a, re.I)]
    if hrefs and len(hashes) > len(hrefs) / 2:
        notes.append(f"{len(hashes)} of {len(hrefs)} links go nowhere (href=\"#\") — point them "
                     "at the page's sections")
    if re.search(r"lorem ipsum|your (text|title|heading) here", low):
        find.append("placeholder copy (lorem ipsum / 'your text here') is on the page")
    return find, notes, checks


def craft_read(page: Path):
    """The page read at phone, tablet and desktop width: {width: reading}."""
    try:
        import aethron_eye as EYE
    except Exception:
        return {}
    got = {}
    for w in (PHONE, TABLET, 1280):
        r = EYE.read_page(page, w, 900, probe=CRAFT_JS)
        if r and "error" not in r:
            got[w] = r
    return got


def live_craft(reads):
    """What only a browser can tell: (findings, notes, checks)."""
    find, notes, checks = [], [], []
    if not reads:
        return find, ["the page could not be opened in a browser to check how it works"], checks
    for w in (PHONE, TABLET):
        r = reads.get(w)
        if r and r.get("scroll", 0) > r.get("vw", w) + 2:
            find.append(f"at {w}px wide the page is {r['scroll']}px wide — it hangs off "
                        "the edge and scrolls sideways")
    checks.append(("fits phones and tablets (390 and 768px, no sideways scroll)",
                   not any(r.get("scroll", 0) > r.get("vw", w) + 2
                           for w, r in reads.items() if w in (PHONE, TABLET))))
    desk, phone = reads.get(1280), reads.get(PHONE)
    if desk and phone and desk.get("nav") and desk["nav"].get("shown", 0) >= 4:
        want = [t for t in desk["nav"]["texts"] if t]
        pn = phone.get("nav") or {}
        t = phone.get("toggle") or {}
        after = set(t.get("links_after") or [])
        crowded = pn.get("shown", 0) >= 4 and (pn.get("rows", 1) > 1 or pn.get("over", 0))
        hidden = pn.get("shown", 0) < len(want)
        menu_ok = bool(t) and t.get("expanded_after") == "true" and all(x in after for x in want)
        ok = not crowded and (not hidden or menu_ok)
        checks.append(("phone menu: the nav collapses behind a working menu button", ok))
        if crowded:
            find.append(f"on a phone the {pn.get('shown')} nav links are squeezed into "
                        f"{pn.get('rows')} row(s) — collapse them behind a menu button")
        elif hidden and not t:
            find.append("on a phone the nav links disappear and nothing opens them — add a menu "
                        "button (<button aria-expanded aria-controls aria-label=\"Open menu\">) "
                        "that shows them")
        elif hidden and not menu_ok:
            why = ("it does not set aria-expanded=\"true\"" if t.get("expanded_after") != "true"
                   else "the links it should show stay hidden: "
                   + ", ".join(x for x in want if x not in after)[:120])
            find.append(f"the phone menu button does not work — {why}")
    ref = desk or phone or {}
    if ref:
        checks.append(("one <main> and one <h1>", ref.get("main") == 1 and ref.get("h1") == 1))
        if ref.get("main") != 1:
            find.append(f"the page has {ref.get('main', 0)} <main> landmark(s) — wrap the content "
                        "in exactly one <main>")
        if ref.get("h1") != 1:
            find.append(f"the page shows {ref.get('h1', 0)} <h1> — a page has exactly one")
        # every width counts: a phone-only menu button is hidden on a desktop
        names = []
        for rd in reads.values():
            for x in (rd or {}).get("unnamed") or []:
                if x not in names:
                    names.append(x)
        un = max([(rd or {}).get("unnamed_n", 0) for rd in reads.values()] + [len(names)])
        checks.append(("every link, button and field has a name", not un))
        if un:
            find.append(f"{un} control(s) have no accessible name (icon-only buttons need "
                        f"aria-label, fields need a <label>): " + ", ".join(names[:4]))
        low = ref.get("low") or []
        bad = [x for x in low if x["ratio"] < 3]
        checks.append(("text contrast readable (WCAG)", not bad))
        if bad:
            find.append("text too faint to read: " + "; ".join(
                f"\"{x['text']}\" {x['ratio']}:1 at {x['size']}px" for x in bad[:4])
                + " — lift it to at least 4.5:1 (3:1 for large text), keeping the hue")
        weak = [x for x in low if x["ratio"] >= 3]
        if weak:
            notes.append(f"{len(weak)} text block(s) under WCAG AA 4.5:1 (e.g. \"{weak[0]['text']}\" "
                         f"{weak[0]['ratio']}:1)")
        if ref.get("sizes", 0) > 9:
            notes.append(f"{ref['sizes']} different font sizes — a type scale usually has 5-7")
    return find, notes, checks


def _slug(t):
    return re.sub(r"[^a-z0-9]+", "-", _html.unescape(t).lower()).strip("-")[:40]


def _text(fragment):
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def finish_site(html, accent=None):
    """What a careful developer adds without changing how the page LOOKS.

    Head metadata, the language, alt text on pictures without any, names
    for fields that only have a placeholder, keyboard focus and hover
    states where there are none, a skip link, reduced motion, and nav
    links that point at the page's own sections. Idempotent: a page that
    already has a thing keeps its own."""
    h = html
    if not re.search(r"<!doctype", h, re.I):
        h = "<!doctype html>\n" + h
    if re.search(r"<html\b(?![^>]*\slang\s*=)", h, re.I):
        h = re.sub(r"<html\b", '<html lang="en"', h, count=1, flags=re.I)
    if not re.search(r"<head\b", h, re.I):
        h = re.sub(r"(<html\b[^>]*>)", r"\1<head></head>", h, count=1, flags=re.I)
    body = h[h.lower().find("<body"):] if "<body" in h.lower() else h
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", body, re.S | re.I)
    title = _text(m.group(1)) if m else ""
    p = next((t for t in (_text(x) for x in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I))
              if len(t) > 40), "")
    desc = (p[:152].rsplit(" ", 1)[0] + "…") if len(p) > 155 else p
    add = []
    if not re.search(r"<meta[^>]+charset", h, re.I):
        add.append('<meta charset="utf-8">')
    if not re.search(r"<meta[^>]+name\s*=\s*['\"]viewport", h, re.I):
        add.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    if not re.search(r"<title>\s*[^<\s]", h, re.I):
        h = re.sub(r"<title>\s*</title>", "", h, flags=re.I)
        if title:
            add.append(f"<title>{_html.escape(title)}</title>")
    if desc and not re.search(r"<meta[^>]+name\s*=\s*['\"]description", h, re.I):
        add.append(f'<meta name="description" content="{_html.escape(desc)}">')
    t_now = re.search(r"<title>(.*?)</title>", h, re.S | re.I)
    t_txt = _text(t_now.group(1)) if t_now else title
    if t_txt and not re.search(r"property\s*=\s*['\"]og:title", h, re.I):
        add.append(f'<meta property="og:title" content="{_html.escape(t_txt)}">')
        add.append('<meta property="og:type" content="website">')
        if desc:
            add.append(f'<meta property="og:description" content="{_html.escape(desc)}">')
        add.append('<meta name="twitter:card" content="summary_large_image">')
    if accent and not re.search(r"name\s*=\s*['\"]theme-color", h, re.I):
        add.append(f'<meta name="theme-color" content="{accent}">')
    if t_txt and not re.search(r"rel\s*=\s*['\"](?:shortcut )?icon", h, re.I):
        ch = _html.escape(t_txt.strip()[:1].upper() or "•")
        col = (accent or "#111").replace("#", "%23")
        add.append('<link rel="icon" href="data:image/svg+xml,'
                   "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
                   f"%3Crect width='64' height='64' rx='14' fill='{col}'/%3E"
                   "%3Ctext x='32' y='44' font-size='36' text-anchor='middle' "
                   f"font-family='system-ui,sans-serif' font-weight='700' fill='white'%3E{ch}"
                   '%3C/text%3E%3C/svg%3E">')
    first = [x for x in add if x.startswith(("<meta charset", '<meta name="viewport'))]
    rest = [x for x in add if x not in first]
    if first:   # the charset belongs at the very top of <head>
        h = re.sub(r"(<head\b[^>]*>)", lambda m_: m_.group(1) + "\n" + "\n".join(first), h,
                   count=1, flags=re.I)
    if rest:
        h = re.sub(r"</head>", lambda _m: "\n".join(rest) + "\n</head>", h, count=1, flags=re.I)
    # pictures without alt are decorative until someone says otherwise
    h = re.sub(r"<img\b(?![^>]*\salt\s*=)", '<img alt=""', h, flags=re.I)
    # a field with only a placeholder gets that placeholder as its name
    def name_field(m_):
        tag = m_.group(0)
        if re.search(r"aria-label|aria-labelledby|type\s*=\s*['\"](hidden|submit|button)", tag, re.I):
            return tag
        fid = re.search(r"\sid\s*=\s*['\"]([^'\"]+)", tag)
        if fid and re.search(rf"<label[^>]+for\s*=\s*['\"]{re.escape(fid.group(1))}['\"]", h, re.I):
            return tag
        ph = re.search(r"placeholder\s*=\s*['\"]([^'\"]+)", tag)
        return tag[:-1].rstrip("/") + f' aria-label="{ph.group(1)}">' if ph else tag
    h = re.sub(r"<(?:input|textarea)\b[^>]*>", name_field, h, flags=re.I)
    # nav links that go nowhere point at the section they name
    secs = {}
    for m_ in re.finditer(r"<section\b([^>]*)>(.*?)</section>", h, re.S | re.I):
        hd = re.search(r"<h[2-3]\b[^>]*>(.*?)</h[2-3]>", m_.group(2), re.S | re.I)
        sid = re.search(r"\sid\s*=\s*['\"]([^'\"]+)", m_.group(1))
        if hd:
            secs[_slug(_text(hd.group(1)))] = sid.group(1) if sid else None
    if secs:
        def give_id(m_):
            attrs, inner = m_.group(1), m_.group(2)
            if re.search(r"\sid\s*=", attrs):
                return m_.group(0)
            hd = re.search(r"<h[2-3]\b[^>]*>(.*?)</h[2-3]>", inner, re.S | re.I)
            if not hd:
                return m_.group(0)
            s = _slug(_text(hd.group(1)))
            if not s or secs.get(s):
                return m_.group(0)
            secs[s] = s
            return f'<section id="{s}"{attrs}>{inner}</section>'
        h = re.sub(r"<section\b([^>]*)>(.*?)</section>", give_id, h, flags=re.S | re.I)

        def point(m_):
            tag, inner = m_.group(1), m_.group(2)
            if re.search(r"\shref\s*=\s*['\"](?!#['\"])[^'\"]", tag, re.I):
                return m_.group(0)
            s = _slug(_text(inner))
            hit = next((v for k, v in secs.items() if v and s and (k == s or k.startswith(s)
                                                                    or s.startswith(k))), None)
            if not hit:
                return m_.group(0)
            tag2 = re.sub(r"\shref\s*=\s*['\"][^'\"]*['\"]", "", tag)
            return f'<a href="#{hit}"{tag2[2:]}{inner}</a>'
        h = re.sub(r"(<a\b[^>]*>)(.*?)</a>", point, h, flags=re.S | re.I)
    css = _css(h)
    extra = []
    if not re.search(r":focus-visible", css):
        extra.append(":where(a,button,input,textarea,select,summary,[tabindex]):focus-visible"
                     "{outline:2px solid currentColor;outline-offset:3px;border-radius:4px}")
    if not re.search(r":hover", css):
        extra.append(":where(a,button){transition:opacity .18s ease,filter .18s ease}"
                     ":where(a,button):hover{filter:brightness(1.12)}")
    if re.search(r"<section\b[^>]*\sid=", h, re.I) and "scroll-margin-top" not in css:
        extra.append("section[id]{scroll-margin-top:80px}"
                     "html{scroll-behavior:smooth}")
    has_main = re.search(r"<main\b", h, re.I)
    if has_main and "ae-skip" not in h:
        if not re.search(r"<main\b[^>]*\sid\s*=", h, re.I):
            h = re.sub(r"<main\b", '<main id="main"', h, count=1, flags=re.I)
        mid = re.search(r"<main\b[^>]*\sid\s*=\s*['\"]([^'\"]+)", h, re.I).group(1)
        h = re.sub(r"(<body\b[^>]*>)", rf'\1<a class="ae-skip" href="#{mid}">Skip to content</a>',
                   h, count=1, flags=re.I)
        extra.append(".ae-skip{position:absolute;left:12px;top:-60px;z-index:9999;padding:10px 14px;"
                     "border-radius:8px;background:#111;color:#fff;font:600 14px/1 system-ui,"
                     "sans-serif;text-decoration:none}.ae-skip:focus{top:12px}")
    css2 = css + "".join(extra)
    if re.search(r"@keyframes|animation\s*:|transition\s*:|scroll-behavior", css2, re.I) \
            and not re.search(r"prefers-reduced-motion", css2, re.I):
        extra.append("@media (prefers-reduced-motion:reduce){*,*::before,*::after{"
                     "animation-duration:.01ms!important;animation-iteration-count:1!important;"
                     "transition-duration:.01ms!important;scroll-behavior:auto!important}}")
    if extra:
        block = "<style data-ae-finish>\n" + "\n".join(extra) + "\n</style>"
        if re.search(r"</head>", h, re.I):
            h = re.sub(r"</head>", lambda _m: block + "\n</head>", h, count=1, flags=re.I)
        else:
            h = block + h
    return h


def grade(image, page, ref: Img = None, work=None, slots=None):
    """Check a page against its screenshot. Never decides on the model's word.

    slots: the screenshot's picture regions (aethron_pictures.detect); each
    must be shown as a picture, not drawn in code."""
    page = Path(page)
    html = page.read_text(errors="replace")
    if ref is None:
        ref = Img.read(_as_png(image))
    W, H, scale = css_size(ref.w, ref.h)
    if scale != 1:
        gw, gh, cells = ref.grid(W, H)
        rgb = bytearray()
        for c in cells:
            rgb += bytes(int(v) for v in c)
        ref = Img(gw, gh, rgb)
    work = Path(work or tempfile.mkdtemp(prefix="ae-s2c-"))
    work.mkdir(parents=True, exist_ok=True)     # a browser cannot write into nothing
    rep = {"page": str(page), "canvas": [W, H], "findings": [], "notes": []}
    rep["reused"] = reuse(html, page.parent, image)
    rep["findings"] += rep["reused"]
    ext = external_pictures(html)
    if ext:
        rep["notes"].append(f"{len(ext)} picture(s) load from other servers: "
                            + ", ".join(ext[:3]))
    tags = {t: len(re.findall(rf"<{t}[\s>]", html, re.I)) for t in
            ("nav", "a", "button", "h1", "header", "section", "img", "svg")}
    rep["elements"] = tags
    if not (tags["a"] or tags["button"]):
        rep["findings"].append("there is no real link or button on the page — "
                               "nav items and buttons must be <a> and <button>")
    sf, sn, sc = static_craft(html)
    rep["findings"] += sf
    rep["notes"] += sn
    rep["craft"] = [{"check": c, "ok": bool(o)} for c, o in sc]
    if rep["reused"]:
        # Refused from its source; there is nothing to learn from rendering
        # a picture of the screenshot, and a page carrying a multi-megabyte
        # copy of it can take longer to render than the check is worth.
        rep.update(likeness=0.0, colour=0.0, structure=0.0, regions=[],
                   verdict="FAIL", phones_checked=False)
        return rep
    png = render(page, W, H, work / "render.png")
    if png is None:
        rep["verdict"] = "SKIPPED"
        rep["why"] = "no browser could render the page — UNVERIFIED, not good"
        return rep
    rep["render_png"] = str(work / "render.png")
    rep.update(likeness(ref, png))
    if rep["likeness"] < LIKENESS_FLOOR:
        rep["findings"].append(
            f"the page does not look enough like the screenshot "
            f"({rep['likeness']:.0%}, needs {LIKENESS_FLOOR:.0%})")
    reads = craft_read(page)
    rep["phones_checked"] = bool(reads.get(PHONE) and reads.get(TABLET))
    lf, ln, lc = live_craft(reads)
    rep["findings"] += lf
    rep["notes"] += ln
    rep["craft"] += [{"check": c, "ok": bool(o)} for c, o in lc]
    if not rep["phones_checked"]:
        rep["notes"].append("phone widths could not be checked")
    if slots:
        import aethron_pictures as PIC
        pf, ran = PIC.honoured(page, slots, W, H)
        if ran:
            rep["findings"] += pf
            rep["craft"].append({"check": f"{len(slots)} picture(s) placed as pictures",
                                 "ok": not pf})
        else:
            rep["notes"].append("where the pictures sit could not be checked")
    rep["verdict"] = "PASS" if not rep["findings"] else "FAIL"
    return rep


def _as_png(image):
    p = Path(image)
    if p.suffix.lower() == ".png":
        return p
    import aethron_vision as V           # converts with sips where it can
    s = V.load(p)
    tmp = Path(tempfile.mkdtemp(prefix="ae-s2c-in-")) / "in.png"
    G.write_png(tmp, s.w, s.h, s.px)
    return tmp


# ─────────────────────────── the loop ────────────────────────────────

def _measured(f):
    """The measured facts for a prompt: the picture brief as plain lines,
    everything else as JSON."""
    g = {k: v for k, v in f.items() if not k.startswith("_")}
    out = ("MEASURED FROM THE SCREENSHOT (trust these over your eye):\n"
           + json.dumps(g, indent=1))
    if f.get("_pictures"):
        out = f["_pictures"] + "\n\n" + out
    return out


def _first_prompt(f):
    return ("Rebuild the website in this screenshot as a real, working web page.\n\n"
            + RULES + "\n" + _measured(f))


def _repair_prompt(f, rep, html, rnd):
    worst = [r for r in rep.get("regions", []) if r["difference"] > 12][:5]
    lines = [f"Round {rnd}. The first picture is the screenshot; the second is how your "
             f"page renders at the same size. It looks {rep.get('likeness', 0):.0%} like the "
             f"screenshot (colour {rep.get('colour', 0):.0%}, structure "
             f"{rep.get('structure', 0):.0%})."]
    if rep.get("findings"):
        lines.append("Checks that FAILED (fix every one):")
        lines += [f"- {x}" for x in rep["findings"]]
    if worst:
        lines.append("Regions that differ most (should be -> is):")
        lines += [f"- {r['region']} (x {r['x']}-{r['x'] + r['w']}, y {r['y']}-{r['y'] + r['h']}): "
                  f"{r['should_be']} -> {r['is']}" for r in worst]
    lines.append("Compare the two pictures closely: layout, spacing, sizes, colours, glows "
                 "and every effect. Fix what differs and keep what already matches.")
    lines.append(RULES)
    lines.append(_measured(f))
    lines.append("YOUR CURRENT PAGE:\n```html\n" + html[:60000] + "\n```")
    lines.append("Reply with the complete corrected HTML in one ```html block.")
    return "\n".join(lines)


def model_writer(budget_usd=0.40, ledger=None, max_tokens=14000):
    """The active saved provider, through the capped key ring, with pictures."""
    import aethron_build as B
    ledger = {} if ledger is None else ledger

    def write(prompt, images):
        return B.gemini_text(prompt, ledger, budget_usd, max_tokens=max_tokens,
                             temperature=0.2, timeout=300, images=images)
    write.ledger = ledger
    return write


def _read_design(src, ref, W, H, scale, work, f, log):
    """What a model would otherwise guess, read off the screenshot: its
    lines of text, the regions that are PICTURES (photographs, paintings,
    illustrations — not drawn in code), and its type (face, weight, size,
    tracking, colour per role). Each is help, never a gate."""
    lines, slots, fonts = [], [], []
    try:
        import aethron_fit as FIT
        lines = FIT.ocr_lines(src, scale) or []
    except Exception as e:
        log(f"text was not located ({e})")
    try:
        import aethron_pictures as PIC
        slots = PIC.detect(ref, lines)
        if slots:
            f["_pictures"] = PIC.brief(slots, W, H)
            PIC.stage(slots, work)
            log(f"{len(slots)} picture region(s) found — they will be pictures, "
                "not drawings")
    except Exception as e:
        log(f"pictures were not looked for ({e})")
    if lines:
        try:
            import aethron_fit as FIT
            log("reading the screenshot's type (face, weight, size, tracking)")
            fonts = FIT.identify(ref, lines, work / "lab", log=log)
            seen, roles = set(), []
            for t in fonts:
                if t["role"] in seen:
                    continue
                seen.add(t["role"])
                roles.append({"sample": t["text"][:60], "font": t["family"],
                              "weight": t["weight"], "style": t["style"],
                              "size_px": t["size"], "letter_spacing_em": t["tracking"],
                              "colour": t["colour"]})
            if roles:
                f["type"] = roles
        except Exception as e:
            log(f"type was not identified ({e})")
    return lines, slots, fonts


def _polish(src, ref, scale, W, H, rep, html, lines, slots, fonts, out, work, report, log):
    """What Aethron does to the best page itself, with no model: put every
    picture in its slot (taking out code that drew it), fill the slots with
    real pictures, and fit type and placement to the screenshot. The result
    is graded again; it is kept unless it is worse."""
    import aethron_pictures as PIC
    import aethron_fit as FIT
    page_img = Img.read(rep["render_png"]) if rep.get("render_png") else None
    notes = []
    try:
        html2, n = PIC.enforce(html, slots, W, H, work / "slots", ref=ref, page_img=page_img,
                               log=log)
        notes += n
        html2, pics = PIC.fill(html2, slots, out, log=log)
        if pics:
            report["pictures"] = pics
        fitdir = work / "fit"
        fitdir.mkdir(parents=True, exist_ok=True)
        for r in pics or []:
            (fitdir / r["file"]).write_bytes((out / r["file"]).read_bytes())
        if lines:
            html2, frep = FIT.fit(html2, ref, FIT.ref_lines_for(ref, lines), W, H, fitdir,
                                  fonts=fonts, log=log,
                                  likeness_fn=lambda img: likeness(ref, img)["likeness"])
            if frep.get("before"):
                report["placement"] = {"before": frep["before"], "after": frep["after"],
                                       "lines": frep.get("lines", [])}
    except Exception as e:
        log(f"the finishing pass stopped ({e}) — the page is handed over as written")
        report["polish"] = notes + [f"stopped: {e}"]
        return rep, html
    site = out / "site.html"
    site.write_text(stamp_ids(html2))
    rep2 = grade(src, site, ref=ref if scale == 1 else None, work=work / "final", slots=slots)
    report["polish"] = notes
    if rep2.get("verdict") == "SKIPPED":
        return rep, html
    worse = (rep2["likeness"] < rep.get("likeness", 0) - 0.02
             and len(rep2["findings"]) >= len(rep.get("findings") or []))
    if worse:
        log(f"the finished page looked worse ({rep2['likeness']:.0%} against "
            f"{rep['likeness']:.0%}) — keeping the page as written")
        report["polish"].append("the finishing pass was not kept: it looked worse")
        return rep, html
    log(f"finished page: looks {rep2['likeness']:.0%} like the screenshot "
        f"(was {rep['likeness']:.0%}), {len(rep2['findings'])} check(s) failed")
    return rep2, html2


def build(image, outdir, writer=None, rounds=3, minutes=15, log=say, budget_usd=0.40):
    """Screenshot -> outdir/site.html, real HTML and CSS, checked.

    Returns a report: verdict PASS / FAIL / SKIPPED / NOT ASKED, the
    likeness of the page kept, every round, and every check that failed.
    """
    t0 = time.time()
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".work"
    work.mkdir(exist_ok=True)
    src = _as_png(image)
    full = Img.read(src)
    W, H, scale = css_size(full.w, full.h)
    if scale != 1:
        gw, gh, cells = full.grid(W, H)
        rgb = bytearray()
        for c in cells:
            rgb += bytes(int(v) for v in c)
        ref = Img(gw, gh, rgb)
    else:
        ref = full
    ref_png = ref.png(work / "reference.png")
    log(f"measuring the screenshot ({W}x{H} CSS px)")
    f = facts(src, W, H, scale)
    lines, slots, fonts = _read_design(src, ref, W, H, scale, work, f, log)
    if writer is None:
        writer = model_writer(budget_usd)
    report = {"image": str(image), "canvas": [W, H], "rounds": [], "verdict": "FAIL"}
    best = None
    html = None
    prev = None
    for rnd in range(1, rounds + 1):
        if time.time() - t0 > minutes * 60:
            report["stopped"] = f"stopped at the {minutes}-minute limit"
            break
        if rnd == 1:
            prompt, images = _first_prompt(f), [("image/png", ref_png.read_bytes())]
            log("writing the page from the screenshot")
        else:
            prompt = _repair_prompt(f, prev, html, rnd)
            images = [("image/png", ref_png.read_bytes())]
            if prev.get("render_png"):
                images.append(("image/png", Img.read(prev["render_png"])
                               .png(work / f"render{rnd - 1}.small.png").read_bytes()))
            log(f"round {rnd}: comparing with the screenshot and correcting")
        try:
            reply = writer(prompt, images)
        except Exception as e:
            why = str(e)
            if best is None:
                report.update(verdict="NOT ASKED",
                              why=f"the model could not be reached: {why}")
                return _finish(report, out, None, ledger=getattr(writer, "ledger", None))
            report["stopped"] = f"round {rnd} was not answered: {why}"
            break
        cand = extract_html(reply or "")
        if not cand:
            report["rounds"].append({"round": rnd, "error": "the reply held no HTML page"})
            if html is None:
                continue
            log(f"round {rnd}: the reply held no page — keeping the last one")
            continue
        # What a careful developer adds without changing the look (head,
        # focus states, alt, skip link, reduced motion, section anchors)
        # is Aethron's job, not a round of the model's.
        html = finish_site(cand, accent=f.get("background"))
        page = work / f"round{rnd}.html"
        page.write_text(html)
        rep = grade(src, page, ref=ref if scale == 1 else None, work=work / f"r{rnd}",
                    slots=slots)
        if rep.get("verdict") == "SKIPPED":
            report.update(verdict="SKIPPED", why=rep.get("why"))
            best = (rep, html)
            break
        row = {"round": rnd, "likeness": rep["likeness"], "verdict": rep["verdict"],
               "findings": rep["findings"]}
        report["rounds"].append(row)
        log(f"round {rnd}: looks {rep['likeness']:.0%} like the screenshot, "
            f"{len(rep['findings'])} check(s) failed")
        prev = rep
        # THE BEST THAT DOES NOT REUSE THE SCREENSHOT, never the last one tried.
        if not rep["reused"] and (best is None or best[0]["reused"]
                                  or _rank(rep) > _rank(best[0])):
            best = (rep, html)
        elif best is None:
            best = (rep, html)
        if rep["verdict"] == "PASS" and rep["likeness"] >= GOOD_ENOUGH:
            break
    if best is None:
        report.update(verdict="FAIL", why="no round produced a page")
        return _finish(report, out, None, ledger=getattr(writer, "ledger", None))
    rep, html = best
    if report["verdict"] != "SKIPPED":
        rep, html = _polish(src, ref, scale, W, H, rep, html, lines, slots, fonts,
                            out, work, report, log)
        report["verdict"] = rep["verdict"]
    for k in ("likeness", "colour", "structure", "findings", "notes", "elements",
              "phones_checked", "reused", "craft"):
        if k in rep:
            report[k] = rep[k]
    report["seconds"] = round(time.time() - t0)
    return _finish(report, out, html, ledger=getattr(writer, "ledger", None),
                   render_png=rep.get("render_png"))


def _rank(rep):
    """Passing first; then a page that looks like the screenshot; then the
    one with fewer things still wrong; then the closer likeness."""
    lk = rep.get("likeness", 0)
    return (rep.get("verdict") == "PASS", lk >= LIKENESS_FLOOR,
            -len(rep.get("findings") or []), lk)


def _finish(report, out, html, ledger=None, render_png=None):
    if ledger:
        report["calls"] = ledger.get("calls", 0)
        report["usd"] = round(ledger.get("usd", 0.0), 4)
    if html:
        if report.get("reused"):
            # A page that shows the screenshot is not handed over at all.
            report["page"] = None
            report["why"] = "every round reused the screenshot as a picture"
        else:
            site = out / "site.html"
            site.write_text(stamp_ids(html))
            report["page"] = str(site)
            if render_png and Path(render_png).is_file():
                (out / "render.png").write_bytes(Path(render_png).read_bytes())
    (out / "report.json").write_text(json.dumps(report, indent=1))
    return report


# ─────────────────────────── selftest ────────────────────────────────

def _png_bytes(w, h, rgb=(200, 80, 40)):
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(t, d):
        c = t + d
        return len(d).to_bytes(4, "big") + c + zlib.crc32(c).to_bytes(4, "big")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00") + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _selftest():
    ok = True

    def check(n, c):
        nonlocal ok
        ok &= bool(c)
        print(("PASS " if c else "FAIL ") + n)

    tmp = Path(tempfile.mkdtemp(prefix="ae-s2c-st-"))
    shot = tmp / "Screenshot 1.png"
    shot.write_bytes(_png_bytes(1440, 900))
    big = base64.b64encode(_png_bytes(1440, 900, (1, 2, 3))).decode()
    icon = base64.b64encode(_png_bytes(48, 48)).decode()
    slab = base64.b64encode(_png_bytes(600, 300)).decode()
    check("a page showing the screenshot's own bytes is refused",
          reuse(f'<img src="data:image/png;base64,{base64.b64encode(shot.read_bytes()).decode()}">',
                tmp, shot))
    check("a scaled copy of the screenshot as a background is refused",
          reuse(f'<div style="background:url(data:image/png;base64,{big})">', tmp, shot))
    check("a 600x300 slab cut out of it is refused",
          reuse(f"<style>.h{{background-image:url('data:image/png;base64,{slab}')}}</style>", tmp, shot))
    check("pointing at the screenshot file by name is refused",
          reuse('<img src="Screenshot%201.png"><img src="./Screenshot 1.png">', tmp, shot))
    (tmp / "hero.png").write_bytes(_png_bytes(900, 500))
    check("a large local picture is refused", reuse('<img src="hero.png">', tmp, shot))
    check("a small icon is allowed", not reuse(f'<img src="data:image/png;base64,{icon}">', tmp, shot))
    check("CSS and SVG are clean",
          not reuse("<style>body{background:radial-gradient(#f80,#000)}</style><svg><circle r=9/></svg>",
                    tmp, shot))
    check("a picture on another server is allowed, and noted",
          not reuse('<img src="https://images.example.com/a.jpg">', tmp, shot)
          and external_pictures('<img src="https://images.example.com/a.jpg">'))
    check("the page is taken out of a fenced reply",
          extract_html("Here:\n```html\n<!doctype html><html><body>x</body></html>\n```") ==
          "<!doctype html><html><body>x</body></html>")
    s = stamp_ids('<html><head><style>a{}</style></head><body><nav><a>x</a></nav>'
                  '<svg><path d="M0"/><g></g></svg><button>b</button></body></html>')
    check("ids are stamped on elements and not inside svg",
          s.count("data-ae-id") == 4 and "<path d" in s and "<g>" in s)
    a = Img(4, 4, bytearray([200, 100, 50] * 16))
    check("an identical picture is fully alike", likeness(a, a)["likeness"] > 0.99)
    b = Img(4, 4, bytearray([0, 0, 0] * 16))
    check("a black page is not alike an orange one", likeness(a, b)["colour"] < 0.1)
    check("css size halves a 2x screenshot", css_size(2880, 1800)[:2] == (1440, 900))
    bare = ("<!doctype html><html><head><style>.a{color:#fff}</style></head><body>"
            "<nav><a href='#'>Pricing plans</a></nav><main><h1>Ship faster</h1>"
            "<p>A calm place to build the products your customers come back to every day.</p>"
            "<section><h2>Pricing plans</h2></section><input placeholder='Your email'>"
            "<img src='https://x.example/a.png'></main></body></html>")
    f0, _, c0 = static_craft(bare)
    check("a bare page is told what a shipped site has (head, breakpoints, hover, focus)",
          any("<head>" in x for x in f0) and any("@media" in x for x in f0)
          and not all(ok_ for _, ok_ in c0))
    fin = finish_site(bare, accent="#101010")
    check("finish_site adds the head a person would write, from the page's own words",
          "<title>Ship faster</title>" in fin and 'name="description"' in fin
          and "A calm place" in fin and 'property="og:title"' in fin
          and 'lang="en"' in fin and 'name="viewport"' in fin)
    check("and keyboard focus, a hover state, a skip link and reduced motion",
          ":focus-visible" in fin and ":hover" in fin and "Skip to content" in fin
          and "prefers-reduced-motion" in fin)
    check("and names what had no name: alt text, a field's label from its placeholder",
          'alt=""' in fin and 'aria-label="Your email"' in fin)
    check("and points a nav link at the section it names",
          'href="#pricing-plans"' in fin and 'id="pricing-plans"' in fin)
    check("finish_site run twice changes nothing more",
          finish_site(fin, accent="#101010") == fin)
    f1, _, _ = static_craft(fin)
    check("what finish_site adds is no longer asked for",
          not any(k in " ".join(f1) for k in ("<head>", ":hover", ":focus-visible")))
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--grade" in argv:
        i = argv.index("--grade")
        rep = grade(argv[i + 1], argv[i + 2])
        rep.pop("regions", None)
        print(json.dumps(rep, indent=1))
        return 0 if rep.get("verdict") == "PASS" else 1
    if len(argv) >= 2:
        rep = build(argv[0], argv[1])
        print(json.dumps({k: v for k, v in rep.items() if k != "rounds"}, indent=1))
        return 0 if rep.get("verdict") == "PASS" else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
