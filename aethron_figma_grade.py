#!/usr/bin/env python3
"""The referee for a Figma conversion: OUR page vs FIGMA'S OWN render.

WHY THIS EXISTS AT ALL
----------------------
Every design-to-code tool claims fidelity. Almost none measure it, and
the ones that do usually ask a model whether the result "looks right" —
which is asking the same kind of system that produced the output to
mark its own work.

Figma will render the design itself, with the renderer the designer was
looking at, via GET /v1/images/:key. So the acceptance test is not an
opinion: render our HTML in a real browser at the designed width, put
the two images side by side, and count the pixels that differ.

A conversion that does not match is REFUSED. That refusal is the
feature — it is the same rule the framework port already follows, and
the reason a port that is not the same site is never handed over.

WHAT "DIFFERENT" MEANS, HONESTLY
--------------------------------
Exact equality is the wrong test and would fail everything. Figma and a
browser are different rasterisers: at any text edge, and at the edge of
any curve, they disagree by a shade or two of antialiasing. That is
physics, not a defect, and no tool on any platform escapes it.

So a pixel counts as MATCHING when every channel is within TOL (default
16/255 — invisible to a person, roughly one antialiasing step) and as
DIFFERENT otherwise. Two numbers are reported:

    identical   pixels within tolerance          -> the headline
    structural  pixels differing by MORE than 64 -> the honest one

The second is what matters. A wrong colour, a missing icon or a
mispositioned block moves it; antialiasing cannot. A build can be 96%
"identical" and still be broken; if `structural` is near zero, the page
is genuinely the design.

NO DEPENDENCIES. PNG is zlib-compressed scanlines with a one-byte
filter per row — about eighty lines to decode, and Aethron already
does harder binary work than this on Framer's CMS blobs. Pulling in
Pillow to compare two images would cost more than it is worth.

    python3 aethron_figma_grade.py <dir> --truth truth.png [--tol 16]
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

TOL = 16          # per-channel: within this, a person sees no difference
STRUCTURAL = 64   # beyond this, it is not antialiasing — it is wrong
PASS_AT = 0.98    # identical fraction required
STRUCT_MAX = 0.01  # structural fraction allowed


# ─────────────────────────── PNG, from scratch ───────────────────────

def read_png(path: Path):
    """-> (width, height, rgba bytearray). Handles the colour types a
    browser and Figma actually emit: 8-bit RGB, RGBA and grey."""
    raw = Path(path).read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")
    pos, idat, w = 8, [], None
    h = bits = ctype = None
    while pos < len(raw):
        ln = int.from_bytes(raw[pos:pos + 4], "big")
        typ = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w = int.from_bytes(data[0:4], "big")
            h = int.from_bytes(data[4:8], "big")
            bits, ctype = data[8], data[9]
            if data[12] != 0:
                raise ValueError("interlaced PNG not supported")
        elif typ == b"IDAT":
            idat.append(data)
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if bits != 8:
        raise ValueError(f"{path}: {bits}-bit PNG not supported")
    nch = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if nch is None:
        raise ValueError(f"{path}: colour type {ctype} not supported")

    data = zlib.decompress(b"".join(idat))
    stride = w * nch
    out = bytearray(w * h * 4)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = data[p]; p += 1
        line = bytearray(data[p:p + stride]); p += stride
        # PNG filters: each row is stored as a delta against its
        # neighbours. Undo it, or every row after the first is noise.
        if f == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        elif f != 0:
            raise ValueError(f"unknown PNG filter {f}")
        o = y * w * 4
        if nch == 4:
            out[o:o + w * 4] = line
        elif nch == 3:
            for x in range(w):
                s, d = x * 3, o + x * 4
                out[d] = line[s]; out[d+1] = line[s+1]
                out[d+2] = line[s+2]; out[d+3] = 255
        elif nch == 1:
            for x in range(w):
                v = line[x]; d = o + x * 4
                out[d] = out[d+1] = out[d+2] = v; out[d+3] = 255
        else:                                   # grey + alpha
            for x in range(w):
                v = line[x*2]; d = o + x * 4
                out[d] = out[d+1] = out[d+2] = v; out[d+3] = line[x*2+1]
        prev = line
    return w, h, out


def write_png(path: Path, w, h, rgba):
    """Write the diff map so a human can SEE where it failed. A number
    tells you that something is wrong; this tells you what."""
    def chunk(typ, data):
        return (len(data).to_bytes(4, "big") + typ + data
                + zlib.crc32(typ + data).to_bytes(4, "big"))
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        rows += rgba[y * w * 4:(y + 1) * w * 4]
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big")
                   + bytes([8, 6, 0, 0, 0]))
           + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
           + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


# ─────────────────────────── render ours ─────────────────────────────

def find_browser():
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              shutil.which("google-chrome"), shutil.which("chromium")):
        if c and Path(c).exists():
            return c
    return None


def shoot(html: Path, w, h, out: Path, browser=None, wait_max=90, url=None):
    """Screenshot our page at the designed size.

    `url` shoots a running server instead of a local file. A framework
    build references its assets from the SITE ROOT, and file:// has no
    root — graded from disk, every such port renders unstyled and gets
    condemned for a defect that exists only in the way it was opened.
    """
    b = browser or find_browser()
    if not b:
        return None
    prof = tempfile.mkdtemp(prefix="aethron-shot-")
    cmd = [b, "--headless", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=1",
           "--default-background-color=00000000",
           f"--user-data-dir={prof}",
           f"--window-size={w},{h}",
           f"--screenshot={out}",
           # Fonts must be loaded before the shutter, or we grade a
           # page mid-swap and blame the converter for it.
           "--virtual-time-budget=8000",
           url or html.resolve().as_uri()]
    # CHROME DOES NOT RELIABLY EXIT after taking its screenshot — this
    # project already documented the same behaviour for --dump-dom, and
    # it cost the full 300s timeout on the first run here. The file is
    # written long before the process ends, so WATCH THE FILE, not the
    # process: wait for it to appear and stop growing, then kill.
    if out.exists():
        out.unlink()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    deadline, last, stable = time.time() + wait_max, -1, 0
    try:
        while time.time() < deadline:
            if proc.poll() is not None and out.exists():
                break
            if out.exists():
                sz = out.stat().st_size
                if sz > 0 and sz == last:
                    stable += 1
                    if stable >= 3:        # ~0.6s unchanged = finished
                        break
                else:
                    stable = 0
                last = sz
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
    shutil.rmtree(prof, ignore_errors=True)
    return out if out.exists() and out.stat().st_size > 0 else None


# ─────────────────────────── compare ─────────────────────────────────

def compare(ours: Path, truth: Path, diff_out: Path = None, tol=TOL):
    w1, h1, a = read_png(ours)
    w2, h2, b = read_png(truth)
    w, h = min(w1, w2), min(h1, h2)
    if (w1, h1) != (w2, h2):
        note = (f"size differs: ours {w1}x{h1}, figma {w2}x{h2} — "
                f"comparing the shared {w}x{h} region")
    else:
        note = ""

    same = struct = 0
    total = w * h
    diff = bytearray(w * h * 4) if diff_out else None
    rows = []
    for y in range(h):
        rowbad = 0
        oa, ob = y * w1 * 4, y * w2 * 4
        for x in range(w):
            i, j = oa + x * 4, ob + x * 4
            dr = abs(a[i] - b[j])
            dg = abs(a[i+1] - b[j+1])
            db = abs(a[i+2] - b[j+2])
            m = dr if dr > dg else dg
            if db > m:
                m = db
            if m <= tol:
                same += 1
                if diff is not None:
                    d = (y * w + x) * 4
                    v = 255 - (a[i] + a[i+1] + a[i+2]) // 12
                    diff[d] = diff[d+1] = diff[d+2] = v
                    diff[d+3] = 255
            else:
                rowbad += 1
                if m > STRUCTURAL:
                    struct += 1
                    if diff is not None:
                        d = (y * w + x) * 4
                        diff[d] = 255; diff[d+1] = 0; diff[d+2] = 0
                        diff[d+3] = 255
                elif diff is not None:
                    d = (y * w + x) * 4
                    diff[d] = 255; diff[d+1] = 170; diff[d+2] = 0
                    diff[d+3] = 255
        rows.append(rowbad)
    if diff is not None:
        write_png(diff_out, w, h, diff)

    # WHERE it fails matters more than how much. A band of bad rows is
    # one broken section; scatter is text antialiasing.
    worst = sorted(range(len(rows)), key=lambda i: -rows[i])[:6]
    bands = []
    for y in sorted(worst):
        bands.append({"y": y, "bad_px": rows[y],
                      "pct": round(100 * rows[y] / max(1, w), 1)})
    return {"width": w, "height": h, "pixels": total,
            "identical": round(same / total, 6),
            "structural": round(struct / total, 6),
            "note": note, "worst_rows": bands}


def grade(page_dir, truth: Path, tol=TOL, verbose=True):
    d = Path(page_dir)
    html = d / "index.html"
    if not html.is_file():
        raise SystemExit(f"no index.html in {d}")
    say = print if verbose else (lambda *a, **k: None)

    tw, th, _ = read_png(truth)
    shot = d / ".grade-ours.png"
    if not find_browser():
        # THE RULE THAT HAS HELD SINCE THE VACUOUS-PASS INCIDENT.
        say("VERDICT: SKIPPED — no headless browser, so the port is "
            "UNVERIFIED (not proven good)")
        return {"ok": None, "why": "no browser"}
    say(f"rendering our page at {tw}x{th}…")
    if not shoot(html, tw, th, shot):
        say("VERDICT: SKIPPED — the browser produced no screenshot")
        return {"ok": None, "why": "screenshot failed"}

    say("comparing against Figma's own render…")
    r = compare(shot, truth, d / ".grade-diff.png", tol)
    ok = r["identical"] >= PASS_AT and r["structural"] <= STRUCT_MAX
    r["ok"] = ok
    say("")
    if r["note"]:
        say("  " + r["note"])
    say(f"  identical  : {r['identical']*100:.2f}%   "
        f"(within {tol}/255 per channel)")
    say(f"  structural : {r['structural']*100:.3f}%  "
        f"(differ by more than {STRUCTURAL} — real errors, not antialiasing)")
    if r["worst_rows"]:
        say("  worst rows :")
        for b in r["worst_rows"]:
            say(f"     y={b['y']:>5}  {b['pct']:>5.1f}% of the row differs")
    say(f"  diff map   : {d/'.grade-diff.png'}  "
        "(red = real difference, orange = borderline)")
    say("")
    say("VERDICT: PIXEL-PERFECT" if ok else
        "VERDICT: REFUSED — this is not the same design")
    return r


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    d = Path(argv[0])
    truth = Path(argv[argv.index("--truth") + 1]) if "--truth" in argv \
        else d / "figma_truth.png"
    tol = int(argv[argv.index("--tol") + 1]) if "--tol" in argv else TOL
    r = grade(d, truth, tol)
    if r.get("ok") is None:
        return 3
    return 0 if r["ok"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
