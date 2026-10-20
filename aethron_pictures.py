#!/usr/bin/env python3
"""Pictures in a screenshot stay pictures: found, kept out of the code, filled.

temple, 2026-10-20, on a rebuild that drew a watercolour of a woman as CSS
circles and blurred waves: "it should understand where images or
animations are placed, so it wouldn't try to build the image in code …
it can either create an image for it, or pull one from the net, one that
matches or relates to the image available."

A developer handed that screenshot would never draw the illustration. They
would leave a slot for it and put a picture there. So:

  1. FIND the pictures. A designed surface is ground, gradients, flat
     boxes and type; every 8px cell of those is explained by at most two
     colours mixing (ink into its ground, one stop into the next). A photo
     or an illustration is not: its cells hold colours no straight line
     between two colours reaches. That residual, with the measured text
     cleared away, is the whole detector. No model, no guess.
  2. TELL the writer: each slot's exact box, what sits on top of it, and
     that it must be an <img> (or a background) pointing at slot-N — never
     shapes. The writer describes what the picture shows in its alt text.
  3. CHECK the render: every slot must be covered by an element showing
     slot-N. One that is not was drawn in code, or left out.
  4. FILL each slot, in this order: MAKE one with a saved provider that can
     make pictures (within the run's money cap); FIND one on the web
     (Openverse, then Wikimedia Commons — openly licensed), ranked by how
     close its colours are to the region's; or paint an honest PLACEHOLDER
     (an SVG wash of the region's own colours) that says what belongs there.

A slot image is never the screenshot: it is generated, downloaded or drawn,
its provenance is written to pictures.json beside the page, and a file is
only accepted as a slot picture when its bytes match that record.

`python3 aethron_pictures.py <image>` prints the slots; `--selftest` offline.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CELL = 8            # CSS px per cell
RICH = 3.0          # mean distance (0-255 RGB) from the best two-colour mix
DENSITY = 0.30      # share of rich cells around a cell for it to be picture
MIN_SHARE = 0.02    # a slot is at least 2% of the canvas …
MIN_SIDE = 48       # … and at least 48 CSS px on its short side
MANIFEST = "pictures.json"
UA = "Aethron/1.0 (image-to-code; https://github.com/zenadell/Aethron)"


# ─────────────────────────── finding them ─────────────────────────────

def residuals(img, cell=CELL):
    """Per cell: how far its colours sit from the best two-colour mix.

    Ink antialiased into its ground, and one gradient stop easing into
    the next, are both a straight line in RGB between two colours — the
    cell's darkest and lightest. A photograph or a painted illustration
    is not: skin, hair and water put colours well off that line.
    """
    W, H, rgb = img.w, img.h, img.rgb
    gw, gh = max(1, W // cell), max(1, H // cell)
    step = 2 if cell >= 6 else 1
    out = [0.0] * (gw * gh)
    for gy in range(gh):
        for gx in range(gw):
            px, lum = [], []
            for y in range(gy * cell, min(H, gy * cell + cell), step):
                o = y * W * 3
                for x in range(gx * cell, min(W, gx * cell + cell), step):
                    i = o + x * 3
                    r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
                    px.append((r, g, b))
                    lum.append(0.299 * r + 0.587 * g + 0.114 * b)
            if len(px) < 4:
                continue
            order = sorted(range(len(px)), key=lum.__getitem__)
            k = len(px) // 20
            lo, hi = px[order[k]], px[order[-1 - k]]
            d = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
            dd = d[0] * d[0] + d[1] * d[1] + d[2] * d[2] or 1
            s = 0.0
            for q in px:
                a = (q[0] - lo[0], q[1] - lo[1], q[2] - lo[2])
                t = max(0.0, min(1.0, (a[0] * d[0] + a[1] * d[1] + a[2] * d[2]) / dd))
                s += math.sqrt((a[0] - t * d[0]) ** 2 + (a[1] - t * d[1]) ** 2
                               + (a[2] - t * d[2]) ** 2)
            out[gy * gw + gx] = s / len(px)
    return gw, gh, out


def _hex(c):
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(v)))) for v in c[:3])


def region_colours(img, x, y, w, h, n=3):
    """The region's colours on an n x n grid, top-left first, as hex."""
    out = []
    for gy in range(n):
        for gx in range(n):
            x0, x1 = x + gx * w // n, x + (gx + 1) * w // n
            y0, y1 = y + gy * h // n, y + (gy + 1) * h // n
            r = g = b = c = 0
            sy = max(1, (y1 - y0) // 12)
            sx = max(1, (x1 - x0) // 12)
            for yy in range(max(0, y0), min(img.h, y1), sy):
                for xx in range(max(0, x0), min(img.w, x1), sx):
                    i = (yy * img.w + xx) * 3
                    r += img.rgb[i]; g += img.rgb[i + 1]; b += img.rgb[i + 2]; c += 1
            out.append(_hex((r / c, g / c, b / c)) if c else "#808080")
    return out


def detect(img, lines=(), rich=RICH, density=DENSITY, min_share=MIN_SHARE):
    """The picture slots in a screenshot (CSS-pixel image), largest first.

    lines: measured text, {x,y,w,h} in the same pixels. Text is cleared
    first — a headline over a photograph is still type, and type over a
    flat ground must never seed a picture.
    """
    W, H = img.w, img.h
    gw, gh, res = residuals(img)
    hot = [1 if r > rich else 0 for r in res]
    for ln in lines or ():
        if float(ln.get("confidence", 1)) < 0.5:
            continue
        pad = max(2, int(ln.get("h", 0) * 0.3))
        for gy in range(max(0, (ln["y"] - pad) // CELL),
                        min(gh, (ln["y"] + ln["h"] + pad) // CELL + 1)):
            for gx in range(max(0, (ln["x"] - pad) // CELL),
                            min(gw, (ln["x"] + ln["w"] + pad) // CELL + 1)):
                hot[gy * gw + gx] = 0
    # A cell is picture when enough of its neighbourhood is: a painted
    # region is speckled with flat-ish cells (a cheek, still water), and a
    # stray rich cell on a flat page (an icon, a stroke) is not a picture.
    R = 2
    integ = [[0] * (gw + 1) for _ in range(gh + 1)]
    for y in range(gh):
        row = 0
        for x in range(gw):
            row += hot[y * gw + x]
            integ[y + 1][x + 1] = integ[y][x + 1] + row
    pic = bytearray(gw * gh)
    for y in range(gh):
        y0, y1 = max(0, y - R), min(gh, y + R + 1)
        for x in range(gw):
            x0, x1 = max(0, x - R), min(gw, x + R + 1)
            n = (y1 - y0) * (x1 - x0)
            s = integ[y1][x1] - integ[y0][x1] - integ[y1][x0] + integ[y0][x0]
            if s >= density * n:
                pic[y * gw + x] = 1
    # Join parts of one picture (hair either side of a face) across a few
    # cells of gap; measure each joined part by its own picture cells.
    J = 3
    joined = bytearray(gw * gh)
    for y in range(gh):
        for x in range(gw):
            if pic[y * gw + x]:
                for yy in range(max(0, y - J), min(gh, y + J + 1)):
                    for xx in range(max(0, x - J), min(gw, x + J + 1)):
                        joined[yy * gw + xx] = 1
    seen = bytearray(gw * gh)
    slots = []
    for start in range(gw * gh):
        if not joined[start] or seen[start]:
            continue
        stack, cells = [start], []
        seen[start] = 1
        while stack:
            c = stack.pop()
            cells.append(c)
            cx, cy = c % gw, c // gw
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if 0 <= nx < gw and 0 <= ny < gh:
                    k = ny * gw + nx
                    if joined[k] and not seen[k]:
                        seen[k] = 1
                        stack.append(k)
        # The box is where the picture's OWN colours are, not the
        # neighbourhood that voted for it (that reaches R cells further).
        own = [c for c in cells if pic[c] and hot[c]]
        if not own:
            continue
        xs = [c % gw for c in own]
        ys = [c // gw for c in own]
        x, y = min(xs) * CELL, min(ys) * CELL
        w = min(W, (max(xs) + 1) * CELL) - x
        h = min(H, (max(ys) + 1) * CELL) - y
        if w * h < min_share * W * H or min(w, h) < MIN_SIDE:
            continue
        fill = len(own) / max(1, ((max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)))
        if fill < 0.12:
            continue
        slots.append({"x": x, "y": y, "w": w, "h": h, "fill": round(fill, 2)})
    slots.sort(key=lambda s: -s["w"] * s["h"])
    out = []
    for s in slots:
        # A picture that spans most of the page is the page's BACKGROUND:
        # the text in the screenshot sits on top of it, and a developer
        # would set it behind the whole section, edge to edge.
        if s["w"] >= 0.6 * W and s["w"] * s["h"] >= 0.25 * W * H:
            s.update(kind="background", x=0, y=0, w=W, h=H)
        else:
            s["kind"] = "picture"
        if any(_overlap(s, o) > 0.5 for o in out):
            continue
        out.append(s)
    for i, s in enumerate(out, 1):
        s["id"] = i
        s["file"] = f"slot-{i}"
        s["colours"] = region_colours(img, s["x"], s["y"], s["w"], s["h"])
        s["on_top"] = [ln.get("text") for ln in (lines or ())
                       if float(ln.get("confidence", 1)) >= 0.5
                       and _overlap(ln, s) > 0.5][:12]
    return out


def _overlap(a, b):
    """Share of a's area inside b."""
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ix * iy / max(1, a["w"] * a["h"])


def brief(slots, W, H):
    """What the writer is told about the pictures: exact, and binding."""
    if not slots:
        return ""
    out = ["PICTURES — these regions of the screenshot are photographs or "
           "illustrations. Do NOT draw them with CSS, SVG, gradients or shapes. "
           "Each one is a real picture file Aethron puts in place; your page "
           "only places it:"]
    for s in slots:
        where = f"x {s['x']}-{s['x'] + s['w']}, y {s['y']}-{s['y'] + s['h']} ({s['w']}x{s['h']} px)"
        if s["kind"] == "background":
            out.append(
                f"- slot {s['id']}: the BACKGROUND picture of the page, {where}. "
                f"Set it on the section it fills as background-image:url({s['file']}.jpg) "
                f"(background-size:cover; background-position:center bottom) and add "
                f'data-ae-slot="{s["id"]}" data-ae-alt="<what the picture shows>" to that '
                f"element. Everything listed on top stays real text over it: "
                + "; ".join(repr(t) for t in s["on_top"][:8]))
        else:
            out.append(
                f'- slot {s["id"]}: {where}. Use <img src="{s["file"]}.jpg" '
                f'data-ae-slot="{s["id"]}" alt="<what the picture shows, specific>" '
                f'width="{s["w"]}" height="{s["h"]}" style="object-fit:cover"> placed at '
                "exactly that box.")
    out.append("Describe each picture precisely in its alt / data-ae-alt (subject, style, "
               "colours, composition) — that description is used to make or find it.")
    return "\n".join(out)


# ─────────────────────────── checking them ────────────────────────────

SLOTS_JS = r"""
<script id="__ae_eye">
(function () {
  function run() {
    var out = [];
    var all = document.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i], cs = getComputedStyle(el), src = '';
      if (el.tagName === 'IMG') src = el.currentSrc || el.getAttribute('src') || '';
      else if (cs.backgroundImage && cs.backgroundImage.indexOf('url(') >= 0) src = cs.backgroundImage;
      else if (el.tagName === 'VIDEO') src = el.getAttribute('poster') || el.currentSrc || '';
      if (!src) continue;
      var r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      out.push({src: src.slice(0, 400), slot: el.getAttribute('data-ae-slot'),
                tag: el.tagName.toLowerCase(),
                x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height,
                hidden: cs.display === 'none' || cs.visibility === 'hidden'
                        || parseFloat(cs.opacity) < 0.05});
    }
    document.documentElement.setAttribute('data-ae-eye',
      JSON.stringify({vw: innerWidth, pictures: out}));
  }
  var started = false;
  function once() { if (started) return; started = true; setTimeout(run, 300); }
  if (document.readyState === 'complete') once(); else addEventListener('load', once);
  setTimeout(once, 900);
})();
</script>
"""


def placed(page, W, H):
    """[{src, slot, x, y, w, h}] every picture the rendered page shows, or None."""
    try:
        import aethron_eye as EYE
    except Exception:
        return None
    r = EYE.read_page(page, W, H, probe=SLOTS_JS)
    if not r or "pictures" not in r:
        return None
    return r["pictures"]


def honoured(page, slots, W, H):
    """Findings: every slot must be shown, by a picture pointing at slot-N."""
    if not slots:
        return [], True
    pics = placed(page, W, H)
    if pics is None:
        return [], False
    found = []
    for s in slots:
        if s.get("drawn_ok"):
            continue        # drawn in code and measured as alike as a picture would be
        best = 0.0
        for p in pics:
            if p.get("hidden"):
                continue
            if s["file"] not in p["src"] and str(p.get("slot") or "") != str(s["id"]):
                continue
            ix = max(0, min(p["x"] + p["w"], s["x"] + s["w"]) - max(p["x"], s["x"]))
            iy = max(0, min(p["y"] + p["h"], s["y"] + s["h"]) - max(p["y"], s["y"]))
            best = max(best, ix * iy / max(1, s["w"] * s["h"]))
        if best < 0.6:
            what = "the background picture" if s["kind"] == "background" else "the picture"
            found.append(
                f"{what} at x {s['x']}, y {s['y']} ({s['w']}x{s['h']}) is "
                + ("not covered by its slot" if best else "drawn in code or left out")
                + f" — it must be a picture pointing at {s['file']}.jpg "
                f"(data-ae-slot=\"{s['id']}\"), covering that box "
                f"({best:.0%} covered now)")
    return found, True


def descriptions(html, slots):
    """slot id -> what the writer said the picture shows."""
    out = {}
    for s in slots:
        sid = str(s["id"])
        for m in re.finditer(r"<[a-z][^>]*data-ae-slot\s*=\s*['\"]" + sid + r"['\"][^>]*>",
                             html, re.I):
            tag = m.group(0)
            for attr in ("data-ae-alt", "alt", "aria-label", "title"):
                a = re.search(attr + r"""\s*=\s*(['"])(.*?)\1""", tag, re.I | re.S)
                if a and a.group(2).strip():
                    out[s["id"]] = re.sub(r"\s+", " ", a.group(2)).strip()[:400]
                    break
            if s["id"] in out:
                break
        if s["id"] not in out:
            m = re.search(re.escape(s["file"]) + r"\.[a-z]+['\"]?[^>]*alt\s*=\s*(['\"])(.*?)\1",
                          html, re.I | re.S)
            if m:
                out[s["id"]] = m.group(2).strip()[:400]
    return out


# ─────────────────────────── filling them ─────────────────────────────

def _decode(data: bytes):
    """Image bytes -> (w, h, rgb) for colour ranking, or None."""
    try:
        import aethron_figma_grade as G
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            tmp = Path(tempfile.mkdtemp(prefix="ae-pic-")) / "x.png"
            tmp.write_bytes(data)
            w, h, a = G.read_png(tmp)
            return w, h, bytes(v for i in range(w * h) for v in a[i * 4:i * 4 + 3])
    except Exception:
        pass
    try:                                     # optional: Pillow where installed
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((96, 96))
        return im.width, im.height, im.tobytes()
    except Exception:
        pass
    sips = shutil.which("sips")              # macOS converts anything
    if sips:
        try:
            d = Path(tempfile.mkdtemp(prefix="ae-pic-"))
            (d / "in").write_bytes(data)
            subprocess.run([sips, "-s", "format", "png", "-Z", "96", str(d / "in"),
                            "--out", str(d / "o.png")], capture_output=True, timeout=30)
            if (d / "o.png").is_file():
                return _decode((d / "o.png").read_bytes())
        except Exception:
            pass
    return None


def _grid_of(w, h, rgb, aspect, n=3):
    """Mean colours on an n x n grid of the centre crop with `aspect`."""
    if w / h > aspect:
        cw, ch = int(h * aspect), h
    else:
        cw, ch = w, int(w / aspect)
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    out = []
    for gy in range(n):
        for gx in range(n):
            r = g = b = c = 0
            for y in range(y0 + gy * ch // n, y0 + (gy + 1) * ch // n):
                for x in range(x0 + gx * cw // n, x0 + (gx + 1) * cw // n):
                    i = (y * w + x) * 3
                    r += rgb[i]; g += rgb[i + 1]; b += rgb[i + 2]; c += 1
            out.append((r / c, g / c, b / c) if c else (128, 128, 128))
    return out


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def colour_distance(slot, data):
    """How far a candidate picture's colours are from the region's, 0..441."""
    d = _decode(data)
    if not d:
        return None
    w, h, rgb = d
    got = _grid_of(w, h, rgb, slot["w"] / max(1, slot["h"]))
    want = [_hex_rgb(c) for c in slot["colours"]]
    return sum(math.dist(a, b) for a, b in zip(got, want)) / len(want)


def _get(url, timeout=20, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _words(desc, limit=6):
    stop = {"a", "an", "the", "of", "with", "and", "in", "on", "at", "to", "for", "is",
            "her", "his", "its", "their", "from", "by", "into", "over", "under", "that",
            "this", "which", "who", "style", "picture", "image", "photo", "background"}
    w = [x for x in re.findall(r"[a-zA-Z]{3,}", desc.lower()) if x not in stop]
    seen = []
    for x in w:
        if x not in seen:
            seen.append(x)
    return seen[:limit]


def search_web(desc, limit=8, get=_get):
    """Openly licensed pictures matching a description: [{url, thumb, credit, source}]."""
    words = _words(desc)
    if not words:
        return []
    found = []
    for q in (" ".join(words[:4]), " ".join(words[:2])):
        try:
            data = json.loads(get("https://api.openverse.org/v1/images/?page_size=%d&q=%s"
                                  % (limit, urllib.parse.quote(q)), accept="application/json"))
            for r in data.get("results", []):
                if r.get("url"):
                    found.append({"url": r["url"], "thumb": r.get("thumbnail") or r["url"],
                                  "credit": f"{r.get('title') or 'picture'} by "
                                            f"{r.get('creator') or 'unknown'} "
                                            f"({(r.get('license') or '').upper()} "
                                            f"{r.get('license_version') or ''}".strip() + ")",
                                  "source": r.get("foreign_landing_url") or r["url"]})
        except Exception:
            pass
        if found:
            break
    if not found:
        try:
            data = json.loads(get(
                "https://commons.wikimedia.org/w/api.php?action=query&format=json"
                "&generator=search&gsrnamespace=6&gsrlimit=%d&gsrsearch=%s"
                "&prop=imageinfo&iiprop=url&iiurlwidth=320"
                % (limit, urllib.parse.quote(" ".join(words[:4]))),
                accept="application/json"))
            for p in (data.get("query", {}).get("pages", {}) or {}).values():
                ii = (p.get("imageinfo") or [{}])[0]
                if ii.get("url", "").lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    found.append({"url": ii["url"], "thumb": ii.get("thumburl") or ii["url"],
                                  "credit": f"{p.get('title', 'picture')} (Wikimedia Commons)",
                                  "source": ii.get("descriptionurl") or ii["url"]})
        except Exception:
            pass
    return found[:limit]


def _image_providers():
    """Saved keys that can make pictures: [(provider, key)], cheapest first."""
    try:
        import aethron_brain as BR
    except Exception:
        return []
    # ONLY THE SELECTED PROVIDER, unless the person allowed another. This
    # took any saved OpenAI or Gemini key whatever was selected: a person
    # using DeepSeek paid Google ~$0.04 a picture and could not see why.
    out = []
    for prov in ("openai", "gemini"):
        try:
            for k in BR.helper_keys("pictures", prov):
                out.append((prov, k))
        except Exception:
            pass
    return out


def _blocked_note():
    try:
        import aethron_brain as BR
        names = BR.helper_blocked("pictures")
    except Exception:
        return ""
    if not names:
        return ""
    return (f"pictures are not made with your saved {' or '.join(names)} key "
            f"because another provider is selected — Settings → Model can allow it")


# What one picture costs, worst case, by provider — checked before asking.
MAKE_COST = {"openai": 0.02, "gemini": 0.04}
MAX_FIND_DIST = 70      # mean RGB distance over the 3x3 colours; past it the picture is someone else's


def make_picture(slot, desc, provider, key, post=None, timeout=120):
    """One picture made by a provider. -> bytes or raises."""
    shape = ("landscape" if slot["w"] > slot["h"] * 1.15 else
             "portrait" if slot["h"] > slot["w"] * 1.15 else "square")
    prompt = (f"{desc}. A {shape} picture for a website, "
              f"its colours close to {', '.join(slot['colours'][:5])}. "
              + ("Leave calm, simple space where text will sit on top: "
                 + "; ".join(slot["on_top"][:4]) + ". " if slot.get("on_top") else "")
              + "No text, no letters, no logos, no frame.")
    post = post or _post_json
    if provider == "openai":
        size = {"landscape": "1536x1024", "portrait": "1024x1536"}.get(shape, "1024x1024")
        r = post("https://api.openai.com/v1/images/generations",
                 {"model": "gpt-image-1-mini", "prompt": prompt, "size": size,
                  "quality": "medium", "n": 1},
                 {"Authorization": f"Bearer {key}"}, timeout)
        return base64.b64decode(r["data"][0]["b64_json"])
    if provider == "gemini":
        r = post("https://generativelanguage.googleapis.com/v1beta/models/"
                 "gemini-2.5-flash-image:generateContent",
                 {"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseModalities": ["IMAGE"]}},
                 {"x-goog-api-key": key}, timeout)
        for c in r.get("candidates", []):
            for p in (c.get("content") or {}).get("parts", []):
                d = p.get("inlineData") or p.get("inline_data")
                if d and d.get("data"):
                    return base64.b64decode(d["data"])
        raise RuntimeError("the reply carried no picture")
    raise RuntimeError(f"{provider} cannot make pictures")


def _post_json(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def placeholder_png(slot, side=200):
    """A small smooth wash of the region's nine colours, as a PNG.

    Used while the page is still being written, so each round's render has
    the picture's tones where the picture goes. At most `side` px on its
    long side — below icon size, never a cut of anything.
    """
    import zlib
    w, h = slot["w"], slot["h"]
    k = side / max(w, h)
    pw, ph = max(2, round(w * k)), max(2, round(h * k))
    cols = [_hex_rgb(c) for c in (slot.get("colours") or ["#808080"] * 9)]
    while len(cols) < 9:
        cols.append(cols[-1])
    raw = bytearray()
    for y in range(ph):
        raw.append(0)
        fy = min(1.999, max(0.0, (y + 0.5) / ph * 3 - 0.5))
        y0, ty = int(fy), fy - int(fy)
        for x in range(pw):
            fx = min(1.999, max(0.0, (x + 0.5) / pw * 3 - 0.5))
            x0, tx = int(fx), fx - int(fx)
            c00, c10 = cols[y0 * 3 + x0], cols[y0 * 3 + x0 + 1]
            c01, c11 = cols[(y0 + 1) * 3 + x0], cols[(y0 + 1) * 3 + x0 + 1]
            for ch in range(3):
                top = c00[ch] * (1 - tx) + c10[ch] * tx
                bot = c01[ch] * (1 - tx) + c11[ch] * tx
                raw.append(int(top * (1 - ty) + bot * ty))

    def chunk(t, d):
        c = t + d
        return len(d).to_bytes(4, "big") + c + zlib.crc32(c).to_bytes(4, "big")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", pw.to_bytes(4, "big") + ph.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00") + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def stage(slots, folder):
    """Put a stand-in for every slot where a draft page will look for it
    (slot-N.jpg beside the page) so a draft's render shows the picture's
    tones. Browsers read an image by its content, not its name."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for s in slots or ():
        data = placeholder_png(s)
        for ext in ("jpg", "png", "webp"):
            (folder / f"{s['file']}.{ext}").write_bytes(data)


def _ext(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.lstrip()[:4] in (b"<svg", b"<?xm"):
        return "svg"
    return "jpg"


def fill(html, slots, outdir, mode="auto", budget_usd=0.10, log=print,
         search=search_web, get=_get, maker=make_picture, providers=None):
    """Write a picture for every slot beside the page; rewrite the page's
    references to the file that was actually written. -> (html, records).

    mode: "auto" (make if a saved provider can, else find, else
    placeholder), "make", "find", "placeholder".
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    desc = descriptions(html, slots)
    blocked = ""
    if providers is None:
        providers = _image_providers()
        blocked = _blocked_note() if not providers else ""
    spent = 0.0
    records = []
    for s in slots:
        if s.get("drawn_ok"):
            continue
        described = bool(desc.get(s["id"]))
        d = desc.get(s["id"]) or ("an illustration in the colours "
                                  + ", ".join(s["colours"][:4]))
        data, how, credit = None, None, ""
        if not described:
            # A search or a picture model given only colours returns
            # something unrelated; an honest wash is better than a wrong
            # picture.
            log(f"picture {s['id']}: the page does not say what it shows — "
                "a stand-in is used until it does")
        if described and mode in ("auto", "make") and blocked:
            log(f"picture {s['id']}: {blocked}")
        if described and mode in ("auto", "make"):
            for prov, key in providers:
                cost = MAKE_COST.get(prov, 0.05)
                if spent + cost > budget_usd:
                    log(f"picture {s['id']}: making one would pass the "
                        f"${budget_usd:.2f} picture budget — not asked")
                    break
                try:
                    data = maker(s, d, prov, key)
                    spent += cost
                    _use(prov, cost)
                    how, credit = "made", f"made by {prov} for this page"
                    _wallet(cost)
                    break
                except Exception as e:
                    log(f"picture {s['id']}: {prov} could not make it ({str(e)[:120]})")
        if data is None and described and mode in ("auto", "find"):
            best = None
            for c in search(d) or []:
                try:
                    th = get(c["thumb"], timeout=15)
                except Exception:
                    continue
                dist = colour_distance(s, th)
                score = dist if dist is not None else 999
                if best is None or score < best[0]:
                    best = (score, c)
            if best and best[0] > MAX_FIND_DIST:
                log(f"picture {s['id']}: the closest picture found is too far from the "
                    f"region's colours ({best[0]:.0f}) — not used")
                best = None
            if best:
                try:
                    data = get(best[1]["url"], timeout=40)
                    how, credit = "found", best[1]["credit"] + " — " + best[1]["source"]
                except Exception as e:
                    log(f"picture {s['id']}: the best match did not download ({e})")
        if data is None:
            data = placeholder_png(s)
            how, credit = "placeholder", "drawn by Aethron from the region's colours"
        ext = _ext(data)
        name = f"{s['file']}.{ext}"
        (outdir / name).write_bytes(data)
        # The writer was told slot-N.jpg; point every reference at the file
        # that exists, whatever its real type.
        html = re.sub(re.escape(s["file"]) + r"\.(?:jpe?g|png|webp|svg|gif|avif)",
                      name, html)
        records.append({"slot": s["id"], "file": name, "how": how, "credit": credit,
                        "description": d, "sha1": hashlib.sha1(data).hexdigest(),
                        "box": [s["x"], s["y"], s["w"], s["h"]], "kind": s["kind"]})
        log(f"picture {s['id']} ({s['w']}x{s['h']}): {how} — {credit[:100]}")
    (outdir / MANIFEST).write_text(json.dumps({"pictures": records,
                                               "usd": round(spent, 4)}, indent=1))
    return html, records


def _use(prov, usd):
    try:
        import aethron_usage
        url = {"openai": "https://api.openai.com/v1/images",
               "gemini": "https://generativelanguage.googleapis.com/v1beta"}[prov]
        model = {"openai": "gpt-image-1-mini",
                 "gemini": "gemini-2.5-flash-image"}[prov]
        aethron_usage.record(url, model, "picture", usd=usd)
    except Exception:
        pass


def _wallet(usd):
    try:
        import aethron_brain as BR
        BR.wallet_spend(usd)
    except Exception:
        pass


def accepted(page_dir):
    """sha1 -> record, for pictures Aethron itself put in this page's slots."""
    p = Path(page_dir) / MANIFEST
    try:
        return {r["sha1"]: r for r in json.loads(p.read_text())["pictures"]}
    except Exception:
        return {}


# ─────────────────────── putting them in place ────────────────────────

ELEMENTS_JS = r"""
<script id="__ae_eye">
(function () {
  var CONTROLS = 'a,button,input,select,textarea,img,video,picture,canvas,iframe,label';
  function run() {
    var out = [];
    var all = document.querySelectorAll('[data-ae-id]');
    for (var i = 0; i < all.length; i++) {
      var el = all[i], cs = getComputedStyle(el), r = el.getBoundingClientRect();
      var bgc = cs.backgroundColor, painted = bgc && bgc !== 'rgba(0, 0, 0, 0)' && bgc !== 'transparent';
      var anc = [], p = el.parentElement;
      while (p) { var a = p.getAttribute && p.getAttribute('data-ae-id'); if (a) anc.push(a); p = p.parentElement; }
      out.push({id: el.getAttribute('data-ae-id'), tag: el.tagName.toLowerCase(),
        x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height,
        text: (el.innerText || '').trim().length,
        controls: el.matches(CONTROLS) ? 1 : el.querySelectorAll(CONTROLS).length,
        draws: !!(painted || (cs.backgroundImage && cs.backgroundImage !== 'none')
                  || (cs.boxShadow && cs.boxShadow !== 'none')
                  || (cs.filter && cs.filter !== 'none') || el.tagName === 'svg'
                  || el.querySelector('svg')),
        position: cs.position, anc: anc});
    }
    var d = document.documentElement;
    d.setAttribute('data-ae-eye', JSON.stringify({vw: innerWidth,
      doc_h: Math.max(d.scrollHeight, document.body.scrollHeight), elements: out}));
  }
  var started = false;
  function once() { if (started) return; started = true; setTimeout(run, 300); }
  if (document.readyState === 'complete') once(); else addEventListener('load', once);
  setTimeout(once, 900);
})();
</script>
"""


def _elements(html, W, H, work):
    import aethron_eye as EYE
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    p = work / "slots-probe.html"
    p.write_text(html)
    r = EYE.read_page(p, W, H, probe=ELEMENTS_JS)
    return r if r and "elements" in r else None


def _remove_by_id(html, eid):
    """Delete the element carrying data-ae-id=eid, balanced; unchanged if
    it cannot be located exactly."""
    m = re.search(r'<([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*\bdata-ae-id="' + re.escape(eid) + r'"[^>]*>',
                  html)
    if not m:
        return html
    tag = m.group(1).lower()
    if tag in ("img", "input", "br", "hr", "meta", "link", "source", "wbr") \
            or m.group(0).endswith("/>"):
        return html[:m.start()] + html[m.end():]
    depth, pos = 1, m.end()
    pat = re.compile(r"<" + tag + r"\b[^>]*>|</" + tag + r"\s*>", re.I)
    while depth:
        m2 = pat.search(html, pos)
        if not m2:
            return html
        if not m2.group(0).startswith("</") and m2.group(0).endswith("/>"):
            pos = m2.end()
            continue
        depth += -1 if m2.group(0).startswith("</") else 1
        pos = m2.end()
    return html[:m.start()] + html[pos:]


def _sub(img, x, y, w, h):
    import aethron_shot2code as S
    x, y = max(0, int(x)), max(0, int(y))
    w, h = max(1, min(int(w), img.w - x)), max(1, min(int(h), img.h - y))
    out = bytearray(w * h * 3)
    for yy in range(h):
        o = ((y + yy) * img.w + x) * 3
        out[yy * w * 3:(yy + 1) * w * 3] = img.rgb[o:o + w * 3]
    return S.Img(w, h, out)


def region_likeness(ref, page_img, s):
    """How alike the page already is inside one slot, 0..1."""
    import aethron_shot2code as S
    a = _sub(ref, s["x"], s["y"], s["w"], s["h"])
    b = _sub(page_img, s["x"], s["y"], s["w"], s["h"])
    if (a.w, a.h) != (b.w, b.h):
        return 0.0
    return S.likeness(a, b)["likeness"]


GOOD_DRAWING = 0.75     # a slot drawn in code this well is left as it is


def enforce(html, slots, W, H, work, ref=None, page_img=None, log=print):
    """Put every picture slot in place on the page, whatever the writer did.

    Code that DRAWS a picture's region (shapes, gradients, blurred circles
    with no words, controls or pictures in them) is taken out, and the
    slot's picture is set on the element the region belongs to — the
    section it fills as a background, or an <img> at the slot's box.
    A slot the page already shows, or already draws well enough
    (GOOD_DRAWING alike to the screenshot in that region), is left alone.
    -> (html, notes)."""
    import aethron_shot2code as S
    notes = []
    if not slots:
        return html, notes
    html = S.stamp_ids(html)
    r = _elements(html, W, H, work)
    if r is None:
        return html, ["the page could not be rendered — pictures were not put in place"]
    els = {e["id"]: e for e in r["elements"]}
    shown = {}
    for p in (placed(Path(work) / "slots-probe.html", W, H) or []):
        for s in slots:
            if s["file"] in p["src"] or str(p.get("slot") or "") == str(s["id"]):
                shown[s["id"]] = True
    css, removed = [], set()
    for s in slots:
        if shown.get(s["id"]):
            notes.append(f"picture {s['id']} is already on the page")
            continue
        if ref is not None and page_img is not None:
            lk = region_likeness(ref, page_img, s)
            if lk >= GOOD_DRAWING:
                s["drawn_ok"] = round(lk, 3)
                notes.append(f"picture {s['id']} is drawn in code {lk:.0%} like the "
                             "screenshot — left as it is")
                continue
        area = s["w"] * s["h"]
        inside = lambda e: _overlap({"x": e["x"], "y": e["y"], "w": max(1, e["w"]),
                                     "h": max(1, e["h"])}, s)
        covers = lambda e: _overlap(s, {"x": e["x"], "y": e["y"], "w": max(1, e["w"]),
                                        "h": max(1, e["h"])})
        url = f"{s['file']}.jpg"
        host = None
        if s["kind"] == "background":
            cands = sorted((e for e in els.values() if e["text"] or e["controls"]),
                           key=lambda e: e["w"] * e["h"])
            full = [e for e in cands if covers(e) >= 0.97]
            if full:
                host = full[0]
            elif r.get("doc_h", H) > 1.2 * H:
                part = [e for e in cands if covers(e) >= 0.85]
                host = part[0] if part else None
            sel = f'[data-ae-id="{host["id"]}"]' if host else "body"
            css.append(f"{sel}{{background-image:url({url}) !important;"
                       "background-size:cover !important;background-position:center bottom !important;"
                       "background-repeat:no-repeat !important}")
        else:
            box = [e for e in els.values() if not e["text"] and not e["controls"]
                   and min(covers(e), inside(e)) >= 0.6]
            if box:
                host = min(box, key=lambda e: abs(e["w"] * e["h"] - area))
                css.append(f'[data-ae-id="{host["id"]}"]{{background-image:url({url}) !important;'
                           "background-size:cover !important;background-position:center !important;"
                           "background-repeat:no-repeat !important}")
            else:
                wrap = [e for e in els.values() if covers(e) >= 0.98]
                wrap = min(wrap, key=lambda e: e["w"] * e["h"]) if wrap else None
                ox, oy, ow = (wrap["x"], wrap["y"], wrap["w"]) if wrap else (0, 0, W)
                img = (f'<img src="{url}" data-ae-slot="{s["id"]}" alt="" '
                       f'style="position:absolute;left:{(s["x"] - ox) / max(1, ow):.2%};'
                       f'top:{s["y"] - oy:.0f}px;width:{s["w"] / max(1, ow):.2%};'
                       f'height:{s["h"]}px;object-fit:cover;z-index:-1;pointer-events:none">')
                if wrap:
                    m = re.search(r'<[^>]*\bdata-ae-id="' + wrap["id"] + r'"[^>]*>', html)
                    html = html[:m.end()] + img + html[m.end():]
                    if wrap["position"] == "static":
                        css.append(f'[data-ae-id="{wrap["id"]}"]{{position:relative;isolation:isolate}}')
                else:
                    html = re.sub(r"(<body\b[^>]*>)", lambda m_: m_.group(1) + img, html, count=1)
                    css.append("body{position:relative;isolation:isolate}")
        keep = set([host["id"]] + host["anc"]) if host else set()
        drawn = []
        for e in sorted(els.values(), key=lambda e: -e["w"] * e["h"]):
            if e["id"] in keep or e["text"] or e["controls"] or not e["draws"]:
                continue
            if host and e["id"] in host["anc"]:
                continue
            if e["w"] * e["h"] < 0.03 * area or inside(e) < 0.7:
                continue
            if any(a in removed for a in e["anc"]):
                continue
            # an element that holds the host is part of the page, not a drawing
            if host and host["id"] in [x["id"] for x in els.values() if e["id"] in x["anc"]]:
                continue
            removed.add(e["id"])
            drawn.append(e)
        for e in drawn:
            html = _remove_by_id(html, e["id"])
        where = ("the page's background" if s["kind"] == "background" else
                 f"x {s['x']}, y {s['y']} ({s['w']}x{s['h']})")
        notes.append(f"picture {s['id']} set as {where}"
                     + (f"; {len(drawn)} shape(s) that drew it in code taken out" if drawn else ""))
    if css:
        block = "<style data-ae-slots>\n" + "\n".join(css) + "\n</style>"
        html = re.sub(r"(?s)<style data-ae-slots>.*?</style>\n?", "", html)
        html = html.replace("</head>", block + "\n</head>", 1) if "</head>" in html \
            else block + html
    return html, notes


# ─────────────────────────── selftest ─────────────────────────────────

def _selftest():
    import aethron_shot2code as S
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (f" — {extra}" if extra else ""))

    # A flat page with a heading, and the same page with a painted square.
    W, H = 320, 240
    rgb = bytearray(bytes((236, 232, 220)) * (W * H))
    for y in range(30, 50):                       # a line of "type"
        for x in range(40, 200):
            if (x // 3) % 2:
                i = (y * W + x) * 3
                rgb[i:i + 3] = bytes((30, 40, 30))
    flat = S.Img(W, H, bytes(rgb))
    check("a flat page with type has no picture", detect(flat) == [],
          str(detect(flat)))
    import random
    rnd = random.Random(4)
    for y in range(110, 220):                     # a "painting": colours off any line
        for x in range(150, 300):
            i = (y * W + x) * 3
            rgb[i:i + 3] = bytes((rnd.randrange(80, 230), rnd.randrange(40, 160),
                                  rnd.randrange(20, 140)))
    art = S.Img(W, H, bytes(rgb))
    sl = detect(art)
    check("a painted region is found", len(sl) == 1, str(sl))
    if sl:
        s = sl[0]
        check("its box is the painting's (within a cell)",
              abs(s["x"] - 150) <= 16 and abs(s["y"] - 110) <= 16
              and abs(s["w"] - 150) <= 24 and abs(s["h"] - 110) <= 24, str(s))
    # A gradient is design, not a picture.
    g = bytearray(W * H * 3)
    for y in range(H):
        for x in range(W):
            i = (y * W + x) * 3
            g[i:i + 3] = bytes((int(255 * x / W), 80, int(255 * y / H)))
    check("a gradient is not a picture", detect(S.Img(W, H, bytes(g))) == [])
    # The prompt names the slot and forbids drawing it.
    b = brief(sl, W, H)
    check("the brief names the slot file and forbids drawing it",
          "slot-1.jpg" in b and "Do NOT draw" in b)
    html = ('<main><img src="slot-1.jpg" data-ae-slot="1" alt="a watercolour of a fox '
            'in tall grass"></main>')
    check("the writer's description is read back",
          descriptions(html, sl).get(1, "").startswith("a watercolour of a fox"))
    # Filling: made, then found, then placeholder — all offline.
    d = Path(tempfile.mkdtemp(prefix="ae-pic-test-"))
    png = S._png_bytes(64, 48, (200, 120, 60))
    out, rec = fill(html, sl, d, providers=[("openai", "k")], log=lambda *a: None,
                    maker=lambda s, desc, p, k: png)
    check("a saved provider that can make pictures is used first",
          rec[0]["how"] == "made" and (d / "slot-1.png").read_bytes() == png
          and "slot-1.png" in out, str(rec))
    check("the made picture is recorded by its bytes",
          hashlib.sha1(png).hexdigest() in accepted(d))
    warm = S._png_bytes(64, 48, (190, 110, 70))
    cold = S._png_bytes(64, 48, (30, 60, 200))
    hits = [{"url": "u-cold", "thumb": "t-cold", "credit": "cold", "source": "s"},
            {"url": "u-warm", "thumb": "t-warm", "credit": "warm", "source": "s"}]
    files = {"t-cold": cold, "t-warm": warm, "u-cold": cold, "u-warm": warm}
    d2 = Path(tempfile.mkdtemp(prefix="ae-pic-test-"))
    sl2 = [dict(sl[0], colours=[S._hex(c) if False else "#BE6E46"] * 9)]
    out2, rec2 = fill(html, sl2, d2, providers=[], log=lambda *a: None,
                      search=lambda q: hits, get=lambda u, timeout=0: files[u])
    check("found pictures are ranked by the region's colours",
          rec2[0]["how"] == "found" and rec2[0]["credit"].startswith("warm"), str(rec2))
    d3 = Path(tempfile.mkdtemp(prefix="ae-pic-test-"))
    out3, rec3 = fill(html, sl, d3, providers=[], log=lambda *a: None,
                      search=lambda q: [])
    check("nothing made or found -> an honest placeholder, referenced by the page",
          rec3[0]["how"] == "placeholder" and "slot-1.png" in out3
          and (d3 / "slot-1.png").read_bytes()[:4] == b"\x89PNG")
    spent = []
    out4, rec4 = fill(html, sl, Path(tempfile.mkdtemp()), providers=[("gemini", "k")],
                      budget_usd=0.01, log=spent.append, search=lambda q: [],
                      maker=lambda *a: png)
    check("making a picture is refused when it would pass the budget",
          rec4[0]["how"] == "placeholder" and any("budget" in x for x in spent))
    print("VERDICT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if not argv:
        print(__doc__)
        return 2
    import aethron_shot2code as S
    import aethron_vision as V
    src = S._as_png(argv[0])
    img = S.Img.read(src)
    W, H, scale = S.css_size(img.w, img.h)
    if scale != 1:
        gw, gh, cells = img.grid(W, H)
        img = S.Img(gw, gh, bytes(int(v) for c in cells for v in c))
    lines = [{**ln, **{k: round(ln[k] / scale) for k in ("x", "y", "w", "h")}}
             for ln in (V.ocr(str(src)) or [])]
    print(json.dumps(detect(img, lines), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
