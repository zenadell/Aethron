#!/usr/bin/env python3
"""Pictures stay pictures; type and placement are fitted by measurement.

The owner's example (tests/pages/dispatch.png): the page Aethron wrote drew
the watercolour portrait as CSS circles and blurred waves, set the type in
the wrong faces, and placed it by eye. This battery runs the real
screenshot-to-code build on that screenshot, with a writer that hands back
tests/pages/dispatch-draft.html — a STAND-IN rebuilt from the owner's
screenshot of their output, not their real HTML — and asks:

  * the drawn shapes are taken out and the region becomes a picture slot,
    recorded with the sha1 of the file on disk;
  * text placement rises from the draft's and lands most lines exactly;
  * the whole page does not look worse for it;
  * a slot nobody described is never searched for or made (colours alone
    find unrelated pictures — the first version put a medical book page
    behind the headline);
  * a found picture far from the region's colours is refused.

Needs OCR and a browser; without either it reports SKIPPED, never PASS.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_pictures as P      # noqa: E402
import aethron_shot2code as S     # noqa: E402
import aethron_vision as V        # noqa: E402

SHOT = ROOT / "tests/pages/dispatch.png"
DRAFT = ROOT / "tests/pages/dispatch-draft.html"

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f" — {extra}" if extra else ""))


def skipped(why):
    print(f"VERDICT: SKIPPED — {why}")
    sys.exit(0)


# --- can it run at all -------------------------------------------------------
if not (SHOT.exists() and DRAFT.exists()):
    skipped("the dispatch fixtures are missing from tests/pages")
try:
    probe = V.ocr(str(SHOT))
except Exception as e:                      # noqa: BLE001
    probe = None
    print("note: OCR raised", e)
if not probe:
    skipped("no OCR on this machine — " + (V.OCR_WHY[0] or "unavailable"))

# --- 1. a slot nobody described is never searched for ------------------------
calls = []


def no_net(*a, **k):
    calls.append(a)
    raise RuntimeError("the battery forbids the network here")


slot = {"id": 1, "file": "slot-1", "x": 0, "y": 0, "w": 200, "h": 120,
        "kind": "picture", "colours": ["#CCD4BD"] * 9}
with tempfile.TemporaryDirectory() as d:
    html, recs = P.fill("<html><body><div></div></body></html>", [slot], d,
                        search=no_net, get=no_net, providers=[("x", "k")],
                        maker=no_net, log=lambda *a: None)
    check("a slot nobody described is never searched for or made",
          not calls and recs and recs[0]["how"] == "placeholder",
          f"calls={len(calls)} how={recs and recs[0]['how']}")
    data = (Path(d) / recs[0]["file"]).read_bytes()
    check("its stand-in is a real PNG, recorded by sha1",
          data[:8] == b"\x89PNG\r\n\x1a\n" and hashlib.sha1(data).hexdigest() == recs[0]["sha1"])


# --- 2. a found picture far from the region's colours is refused -------------
def png(rgb, w=60, h=40):
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


far = png((200, 20, 200))          # magenta against a sage region
near = png((204, 212, 189))        # the region's own colour
for label, pic, want in (("far", far, "placeholder"), ("near", near, "found")):
    def search(desc, pic=pic):
        return [{"thumb": "t", "url": "u", "credit": "someone", "source": "somewhere"}]

    def get(url, timeout=0, pic=pic):
        return pic
    page = '<html><body><img src="slot-1.jpg" alt="a watercolour portrait"></body></html>'
    with tempfile.TemporaryDirectory() as d:
        _, recs = P.fill(page, [slot], d, mode="find", search=search, get=get,
                         providers=[], log=lambda *a: None)
    check(f"a found picture {'far from' if label == 'far' else 'matching'} the region's "
          f"colours is {'refused' if want == 'placeholder' else 'taken'}",
          recs and recs[0]["how"] == want, recs and recs[0]["how"])


# --- 3. the owner's example, end to end --------------------------------------
draft = DRAFT.read_text()
looked = {"images": 0}


def writer(prompt, images=None):
    looked["images"] = len(images or [])
    return "```html\n" + draft + "\n```"


writer.ledger = {}
P_get, P_search = P._get, P.search_web
P._get = no_net
P.search_web = lambda *a, **k: no_net()
calls.clear()
out = Path(tempfile.mkdtemp(prefix="ae-dispatch-"))
t = time.time()
try:
    rep = S.build(str(SHOT), str(out), writer=writer, rounds=1)
finally:
    P._get, P.search_web = P_get, P_search
secs = round(time.time() - t)
if rep.get("verdict") == "SKIPPED":
    skipped("the build could not run — " + str(rep.get("why") or rep.get("notes")))

site = (out / "site.html").read_text()
pl = rep.get("placement") or {}
before, after = pl.get("before") or {}, pl.get("after") or {}
first = (rep.get("rounds") or [{}])[0].get("likeness") or 0
print(f"note: {secs}s; draft likeness {first:.3f} -> {rep.get('likeness', 0):.3f}; "
      f"placement {before.get('placement')} ({before.get('right')}/{before.get('total')}) -> "
      f"{after.get('placement')} ({after.get('right')}/{after.get('total')})")

check("the picture region is not drawn in code: the three shapes are gone",
      not re.search(r'class="(?:water|hair|face)"', site))
pics = rep.get("pictures") or []
check("the region is a picture slot, recorded", len(pics) >= 1, str(pics)[:200])
if pics:
    f = out / pics[0]["file"]
    check("the recorded picture is on disk with the sha1 it was recorded with",
          f.exists() and hashlib.sha1(f.read_bytes()).hexdigest() == pics[0]["sha1"])
    check("the page uses that picture", pics[0]["file"] in site)
    manifest = json.loads((out / P.MANIFEST).read_text())
    check("pictures.json lists it", any(p["sha1"] == pics[0]["sha1"] for p in manifest["pictures"]))
check("no network was used for a picture the page does not describe", not calls,
      f"{len(calls)} call(s)")
check("the screenshot itself is not in the page", not rep.get("reused"), rep.get("reused"))
check("text placement rises from the draft's by at least 15 points",
      (after.get("placement") or 0) >= (before.get("placement") or 0) + 0.15,
      f"{before.get('placement')} -> {after.get('placement')}")
check("most lines land exactly where the screenshot has them",
      (after.get("right") or 0) >= 0.7 * (after.get("total") or 99),
      f"{after.get('right')} of {after.get('total')}")
check("the page does not look worse for it",
      rep.get("likeness", 0) >= first - 0.02, f"{first:.3f} -> {rep.get('likeness', 0):.3f}")
check("the headline is set in a serif read off the pixels",
      re.search(r"font-family:'[^']+',[^;]*serif !important", site) is not None)
check("the writer was shown the screenshot", looked["images"] >= 1)
wide = rep.get("wide") or {}
check("in a window half again as wide it looks as alike (the page scales with the picture)",
      wide.get("likeness", 0) >= rep.get("likeness", 0) - 0.03,
      f"{wide.get('likeness')} at {wide.get('width')}px against {rep.get('likeness')}")
m = re.search(r'data-ae-id="(e\d+)"[^>]*>\s*One email', site)
check("the paragraph is set upright, as the screenshot has it",
      m and not re.search(r'\[data-ae-id="%s"\]\{[^}]*font-style:italic' % m.group(1), site),
      m and m.group(1))

print("VERDICT: " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
