#!/usr/bin/env python3
"""What in a screenshot is the thing to build (aethron_frame).

Synthetic pictures, so it runs anywhere with no OCR:
  * a dark rounded card on a flat backdrop is cut out exactly, corners
    filled with its own colour
  * the same card on a photo-like backdrop is cut only when OCR says the
    text is all inside it — with no OCR, or with text outside, the whole
    screenshot is kept (a full-page site with a card in it keeps its page)
  * a red pen circle and a green arrow are painted out; the design's own
    orange button, orange label and white text are not touched
  * the real fixtures (a full-page site, a newsletter) are left exactly as
    they are
  * build() records the framing and designs at the card's size
"""
import math
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aethron_figma_grade as G  # noqa: E402
import aethron_frame as F  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, flush=True)


TMP = Path(tempfile.mkdtemp(prefix="ae-frame-"))
W, H = 1200, 840
CARD = (130, 110, 1070, 730)          # x0, y0, x1, y1
R = 28


def canvas(bg):
    a = bytearray(W * H * 4)
    for y in range(H):
        for x in range(W):
            c = bg(x, y)
            i = (y * W + x) * 4
            a[i:i + 4] = bytes((c[0], c[1], c[2], 255))
    return a


def put(a, x, y, c):
    if 0 <= x < W and 0 <= y < H:
        i = (y * W + x) * 4
        a[i:i + 3] = bytes(c)


def rect(a, x0, y0, x1, y1, c, r=0):
    for y in range(y0, y1):
        for x in range(x0, x1):
            dx = max(x0 + r - x, 0, x - (x1 - 1 - r))
            dy = max(y0 + r - y, 0, y - (y1 - 1 - r))
            if dx * dx + dy * dy <= r * r:
                put(a, x, y, c)


def stroke(a, pts, c, wdt):
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        n = int(max(abs(xb - xa), abs(yb - ya))) + 1
        for k in range(n):
            x, y = xa + (xb - xa) * k / n, ya + (yb - ya) * k / n
            for u in range(-wdt // 2, wdt // 2 + 1):
                for v in range(-wdt // 2, wdt // 2 + 1):
                    put(a, int(x + u), int(y + v), c)


def design(a):
    """The card: panels, white text-ish bars, an orange button and label."""
    rect(a, *CARD, (28, 28, 28), R)
    rect(a, 150, 130, 330, 710, (36, 36, 36), 16)
    rect(a, 350, 130, 820, 710, (32, 32, 32), 16)
    rect(a, 840, 130, 1050, 710, (36, 36, 36), 16)
    for i, y in enumerate((170, 230, 280, 330)):
        rect(a, 175, y, 300 - i * 15, y + 10, (235, 235, 235))
    rect(a, 380, 600, 720, 640, (240, 240, 240))            # the big headline
    rect(a, 750, 600, 800, 650, (255, 79, 23), 25)           # orange round button
    for x in range(860, 930, 14):                            # orange "FUTURE" label
        rect(a, x, 680, x + 9, 690, (255, 79, 23))


def flat(x, y):
    return (196, 205, 214)


random.seed(7)
NOISE = [[random.randint(0, 255) for _ in range(40)] for _ in range(30)]


def photo(x, y):
    """Smooth value noise plus fine grain: no long straight edges."""
    gx, gy = x / W * 39, y / H * 29
    i, j = int(gx), int(gy)
    fx, fy = gx - i, gy - j
    v = (NOISE[j][i] * (1 - fx) * (1 - fy) + NOISE[j][i + 1] * fx * (1 - fy)
         + NOISE[j + 1][i] * (1 - fx) * fy + NOISE[j + 1][i + 1] * fx * fy)
    g = (x * 7 + y * 13) % 11 * 3
    return (int(40 + v * .5 + g), int(60 + v * .45 + g), int(70 + v * .4 + g))


def save(a, name):
    p = TMP / name
    G.write_png(p, W, H, a)
    return p


def near(a, b, t=3):
    return all(abs(u - v) <= t for u, v in zip(a, b))


# 1. flat backdrop
a = canvas(flat)
design(a)
p1 = save(a, "flat.png")
out, info = F.frame(p1, TMP / "o1")
check("a card on a flat backdrop is cut out", info.get("cropped") is not None)
check("exactly at its edges (within 3px)", info.get("cropped") and near(info["cropped"], CARD))
if info.get("cropped"):
    w, h, px = G.read_png(out)
    corner = tuple(px[0:3])
    check("and its rounded corners take its own colour, not the backdrop",
          max(abs(c - 28) for c in corner) < 20)

# 2. photo backdrop
a = canvas(photo)
design(a)
p2 = save(a, "photo.png")
inside = [{"text": "Ask your data anything", "x": 380, "y": 600, "w": 340, "h": 40}]
_, info = F.frame(p2, TMP / "o2", ocr_lines=inside)
check("a card on a photo is cut out when the text is all inside it",
      info.get("cropped") and near(info["cropped"], CARD))
_, info = F.frame(p2, TMP / "o3", ocr_lines=inside + [
    {"text": "Subscribe to the newsletter", "x": 20, "y": 790, "w": 320, "h": 16}])
check("but kept whole when there is text outside it (it is part of a page)",
      info.get("cropped") is None and "outside" in info.get("kept_whole", ""))
_, info = F.frame(p2, TMP / "o4")
check("and kept whole with no OCR to prove the photo holds no text",
      info.get("cropped") is None)
called = []
F.frame(p2, TMP / "o5", ocr_lines=lambda: called.append(1) or inside)
check("OCR is read only when a subject was found", called == [1])
a = canvas(flat)
for i in range(0, H, 60):
    rect(a, 40, i + 10, 400, i + 22, (40, 40, 40))
p6 = save(a, "page.png")
called = []
_, info = F.frame(p6, TMP / "o6", ocr_lines=lambda: called.append(1) or [])
check("a page with no card is left alone, OCR never asked", info.get("cropped") is None and not called)

# 3. pen marks
a = canvas(flat)
design(a)
clean = bytes(a)
circle = [(560 + 120 * math.cos(t / 40 * 2 * math.pi) + 5 * math.sin(t * 1.7),
           380 + 70 * math.sin(t / 40 * 2 * math.pi) + 4 * math.cos(t * 1.3)) for t in range(42)]
stroke(a, circle, (235, 30, 40), 5)
stroke(a, [(400, 300), (480, 380), (455, 378), (480, 380), (477, 355)], (40, 200, 60), 4)
p7 = save(a, "marked.png")
out, info = F.frame(p7, TMP / "o7")
check("the pen marks are found", (info.get("marks") or 0) >= 1)
w, h, px = G.read_png(out)
x0, y0 = info["cropped"][:2] if info.get("cropped") else (0, 0)


def at(buf, bw, x, y):
    i = (y * bw + x) * 4
    return tuple(buf[i:i + 3])


left = sum(1 for y in range(h) for x in range(w)
           if near(at(px, w, x, y), (235, 30, 40), 40) or near(at(px, w, x, y), (40, 200, 60), 40))
check(f"and painted out (red or green pixels left: {left})", left < 40)
check("the orange button is kept", near(at(px, w, 775 - x0, 625 - y0), (255, 79, 23), 6))
check("the orange label is kept", near(at(px, w, 864 - x0, 685 - y0), (255, 79, 23), 6))
check("the white headline is kept", near(at(px, w, 700 - x0, 620 - y0), (240, 240, 240), 6))
moved = sum(1 for y in range(h) for x in range(w)
            if max(abs(u - v) for u, v in zip(at(px, w, x, y), at(clean, W, x + x0, y + y0))) > 40)
check(f"nothing else changed (pixels off by >40: {moved})", moved < 0.003 * w * h)

# 4. real fixtures stay as they are
for name in ("dispatch.png", "recognito.png"):
    src = ROOT / "tests" / "pages" / name
    out, info = F.frame(src, TMP / ("f-" + name), ocr_lines=lambda: [])
    check(f"{name}: left exactly as it was", Path(out) == src and not info.get("marks")
          and not info.get("cropped"))

# 5. build() records it and designs at the card's size
try:
    import aethron_shot2code as S

    def writer(prompt, images):
        writer.prompt = prompt
        return "<!doctype html><html><head><title>x</title></head><body><main><h1>Hi</h1></main></body></html>"
    rep = S.build(p1, TMP / "b", writer=writer, rounds=1, minutes=4, log=lambda *a: None)
    fr = rep.get("framing") or {}
    check("build() records the framing", fr.get("cropped") is not None)
    check("and designs at the card's size, not the screenshot's",
          rep.get("canvas") == [CARD[2] - CARD[0], CARD[3] - CARD[1]]
          or (fr.get("cropped") and rep.get("canvas") == [fr["cropped"][2] - fr["cropped"][0],
                                                         fr["cropped"][3] - fr["cropped"][1]]))
    check("and tells the writer to build only that design",
          "backdrop" in getattr(writer, "prompt", ""))
except Exception as e:
    check(f"build() runs ({e})", False)

print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
