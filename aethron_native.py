#!/usr/bin/env python3
"""What the Mac itself is holding: images on the clipboard, or in the last drag.

WHY THIS EXISTS. The page reads pasted and dropped images through the web
platform (clipboardData / dataTransfer.files), and inside the app that is
not enough. In the app's web view:

  * Cmd+V with no text field focused never reaches the page at all, so a
    screenshot on the clipboard could not be pasted onto the chat;
  * a screenshot dragged straight from the floating thumbnail, or a photo
    dragged out of Photos, is a FILE PROMISE or raw image data — the page
    sees a drop with no files in it;
  * a HEIC or TIFF arrives as a file the page cannot turn into PNG.

The Mac's own pasteboards hold all of it. So when the page comes back
empty-handed it asks here, and this reads the general pasteboard (paste)
or the drag pasteboard (drop) natively and hands back PNG/JPEG bytes.

`read(which)` is safe to call from any thread: AppKit is touched on the
main thread only. Off a Mac, or without pyobjc, it reports that plainly
and the page carries on with what the browser gave it.

`python3 aethron_native.py --selftest` uses a private pasteboard, so it
never touches what the person has copied.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif",
             ".tif", ".tiff", ".bmp"}
MAX_BYTES = 24 * 1024 * 1024
# What the web platform can show and the upload endpoint stores as is.
KEEP = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif"}


def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import AppKit  # noqa: F401
        return True
    except Exception:
        return False


def _kind(blob: bytes):
    for k, v in KEEP.items():
        if blob.startswith(k):
            return v
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


def _to_png(blob: bytes):
    """Anything AppKit can open (HEIC, TIFF, BMP…), as PNG bytes."""
    import AppKit
    from Foundation import NSData
    data = NSData.dataWithBytes_length_(blob, len(blob))
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(data)
    if rep is None:
        img = AppKit.NSImage.alloc().initWithData_(data)
        if img is None:
            return None
        tiff = img.TIFFRepresentation()
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(tiff) if tiff else None
        if rep is None:
            return None
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    return bytes(png) if png else None


def _usable(blob: bytes):
    if not blob or len(blob) > MAX_BYTES:
        return None
    return blob if _kind(blob) else _to_png(blob)


def _read_board(pb) -> dict:
    """Images on one pasteboard. Must run on the main thread."""
    import AppKit
    out = {"images": [], "others": [], "count": int(pb.changeCount())}
    # 1. files — a copied or dragged file from Finder, or a saved screenshot
    opts = {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
    urls = pb.readObjectsForClasses_options_([AppKit.NSURL], opts) or []
    for u in urls:
        try:
            p = Path(str(u.path()))
        except Exception:
            continue
        if p.suffix.lower() in IMAGE_EXT and p.is_file():
            try:
                blob = _usable(p.read_bytes())
            except OSError:
                blob = None
            if blob:
                out["images"].append({"name": p.name, "data": blob})
                continue
        out["others"].append(p.name)
    if out["images"] or out["others"]:
        return out
    # 2. image data — a screenshot copied with Ctrl, an image copied from
    # a web page or Preview, or the floating screenshot thumbnail dragged
    for t in ("public.png", "public.jpeg", "public.tiff", "public.heic",
              "com.compuserve.gif", "com.microsoft.bmp"):
        d = pb.dataForType_(t)
        if d is not None and d.length():
            blob = _usable(bytes(d))
            if blob:
                out["images"].append({"name": "pasted image", "data": blob})
                return out
    # 3. anything else NSImage can read off it (PDF, EPS…)
    if AppKit.NSImage.canInitWithPasteboard_(pb):
        img = AppKit.NSImage.alloc().initWithPasteboard_(pb)
        tiff = img.TIFFRepresentation() if img is not None else None
        blob = _to_png(bytes(tiff)) if tiff else None
        if blob:
            out["images"].append({"name": "pasted image", "data": blob})
    return out


def _board(which):
    import AppKit
    if which == "drag":
        return AppKit.NSPasteboard.pasteboardWithName_(
            getattr(AppKit, "NSPasteboardNameDrag", "Apple CFPasteboard drag"))
    if which == "general":
        return AppKit.NSPasteboard.generalPasteboard()
    return AppKit.NSPasteboard.pasteboardWithName_(which)


def read(which="general", timeout=5.0) -> dict:
    """{"images": [{"name", "data"}], "others": [names], "count"} or
    {"native": False, "why": …}. Thread-safe: AppKit runs on the main thread."""
    if not available():
        return {"native": False, "why": "not running in the Mac app"}
    import Foundation
    if Foundation.NSThread.isMainThread():
        try:
            return _read_board(_board(which))
        except Exception as e:
            return {"native": False, "why": f"{type(e).__name__}: {e}"}
    box, done = {}, threading.Event()

    def job():
        try:
            box["r"] = _read_board(_board(which))
        except Exception as e:
            box["r"] = {"native": False, "why": f"{type(e).__name__}: {e}"}
        done.set()
    Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(job)
    if not done.wait(timeout):
        return {"native": False, "why": "the window did not answer"}
    return box["r"]


def change_count(which="general"):
    """The pasteboard's change count (cheap; any thread is fine for this)."""
    if not available():
        return None
    try:
        return int(_board(which).changeCount())
    except Exception:
        return None


# ─────────────────────────── selftest ────────────────────────────────

def _selftest():
    if not available():
        print("VERDICT: SKIPPED — needs macOS with pyobjc")
        return 0
    import AppKit
    from Foundation import NSData, NSURL
    import tempfile
    import zlib
    ok = True

    def check(n, c):
        nonlocal ok
        ok &= bool(c)
        print(("PASS " if c else "FAIL ") + n)

    def png(w=8, h=6, rgb=(200, 80, 40)):
        raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

        def chunk(t, d):
            c = t + d
            return len(d).to_bytes(4, "big") + c + zlib.crc32(c).to_bytes(4, "big")
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big")
                + b"\x08\x02\x00\x00\x00") + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    # A PRIVATE pasteboard: the person's clipboard is never touched.
    pb = AppKit.NSPasteboard.pasteboardWithUniqueName()
    name = str(pb.name())
    try:
        pb.clearContents()
        blob = png()
        pb.setData_forType_(NSData.dataWithBytes_length_(blob, len(blob)), "public.png")
        r = read(name)
        check("PNG data on the board comes back as the same PNG",
              r.get("images") and r["images"][0]["data"] == blob)

        pb.clearContents()
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(NSData.dataWithBytes_length_(blob, len(blob)))
        tiff = rep.TIFFRepresentation()
        pb.setData_forType_(tiff, "public.tiff")
        r = read(name)
        check("TIFF data (a copied screenshot) comes back as PNG",
              r.get("images") and r["images"][0]["data"].startswith(b"\x89PNG"))

        d = Path(tempfile.mkdtemp(prefix="ae-native-"))
        f = d / "Screenshot 2026.png"
        f.write_bytes(png(10, 10))
        t = d / "notes.txt"
        t.write_text("hi")
        pb.clearContents()
        pb.writeObjects_([NSURL.fileURLWithPath_(str(f)), NSURL.fileURLWithPath_(str(t))])
        r = read(name)
        check("a copied image file is read from disk",
              r.get("images") and r["images"][0]["name"] == f.name
              and r["images"][0]["data"] == f.read_bytes())
        check("a file that is not an image is named, not taken",
              r.get("others") == ["notes.txt"])

        pb.clearContents()
        pb.setString_forType_("just words", AppKit.NSPasteboardTypeString)
        r = read(name)
        check("text on the board is not an image", not r.get("images"))

        box = {}
        th = threading.Thread(target=lambda: box.update(r=read(name, timeout=1.0)))
        th.start()
        # off the main thread the read is queued on the main queue; spin it
        end = __import__("time").time() + 3
        while th.is_alive() and __import__("time").time() < end:
            AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
                AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05))
        th.join(1)
        check("a read from another thread is answered on the main thread",
              "images" in (box.get("r") or {}))
    finally:
        pb.releaseGlobally()
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
