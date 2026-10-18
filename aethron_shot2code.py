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
RULES — the page is checked against every one, and a page that breaks one is refused:
1. Write ONE self-contained HTML file: inline <style>, optional inline <script>.
   You may link Google Fonts. Nothing else external.
2. Build it from REAL elements: <header>, <nav> with <a> links, <button>s,
   <h1>/<h2>/<p>, <section>s, <form>/<input> where the design has them.
   Copy every word of text exactly as it appears in the screenshot.
3. Draw everything visual with CSS or inline <svg>: backgrounds, gradients
   (linear-, radial-, conic-), glows (box-shadow, filter: blur), glass
   (backdrop-filter), pills, borders, lines, grids, orbs, rings and icons.
4. NEVER use the screenshot as an image. No <img>, CSS url() or data: URI may
   show the screenshot or any part of it larger than a 128px icon. You do not
   have the screenshot as a file anyway — draw it.
5. Make it RESPONSIVE: a centred container with a max-width, flex/grid
   layout, sizes in %, rem, clamp() or vw. At 390px wide nothing may hang
   off the right edge (no horizontal scroll); nav links may collapse.
6. At the screenshot's width the page must look like the screenshot: same
   layout, same colours, same type sizes, same effects. Use the measured
   numbers below instead of guessing.
7. Reply with the complete HTML in one ```html block and nothing else.
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

    def judge(data, label):
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


def grade(image, page, ref: Img = None, work=None):
    """Check a page against its screenshot. Never decides on the model's word."""
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
    ran, bad = phones(page)
    rep["phones_checked"] = bool(ran)
    for w, sw in bad:
        rep["findings"].append(f"at {w}px wide the page is {sw}px wide — it hangs off "
                               "the edge and scrolls sideways")
    if not ran:
        rep["notes"].append("phone widths could not be checked")
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

def _first_prompt(f):
    return ("Rebuild the website in this screenshot as a real, working web page.\n\n"
            + RULES + "\nMEASURED FROM THE SCREENSHOT (trust these over your eye):\n"
            + json.dumps(f, indent=1))


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
    lines.append("MEASURED FROM THE SCREENSHOT:\n" + json.dumps(f, indent=1))
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
        html = cand
        page = work / f"round{rnd}.html"
        page.write_text(html)
        rep = grade(src, page, ref=ref if scale == 1 else None, work=work / f"r{rnd}")
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
                                  or (rep["verdict"] == "PASS", rep["likeness"])
                                  > (best[0]["verdict"] == "PASS", best[0]["likeness"])):
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
        report["verdict"] = rep["verdict"]
    for k in ("likeness", "colour", "structure", "findings", "notes", "elements",
              "phones_checked", "reused"):
        if k in rep:
            report[k] = rep[k]
    report["seconds"] = round(time.time() - t0)
    return _finish(report, out, html, ledger=getattr(writer, "ledger", None),
                   render_png=rep.get("render_png"))


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
