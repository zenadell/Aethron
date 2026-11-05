#!/usr/bin/env python3
"""Ember on the desktop: a second, transparent window over the whole screen.

The companion leaves the Aethron window and lives on the Mac's desktop: it
sits in the notch, perches on the top edge of other windows, naps on the
Dock, and keeps reacting to what Aethron is really doing.

How it stays out of the way:
  * the window ignores the mouse everywhere except over Ember itself (and
    its reply box), decided 20 times a second from where the pointer is;
  * it is left out of screen sharing and screenshots (sharing type none),
    so nobody watching a shared screen sees it;
  * it hides while any app is full screen (a film, a presentation);
  * it reads WHERE other windows are (their frames, from the window
    server), never what they show and never their titles.

macOS only. Everything that touches AppKit is guarded: on another system,
or when a class is missing, `available()` is False and nothing happens.
The pure parts (coordinates, which windows count, full-screen detection)
are plain functions so they are tested anywhere:

    python3 aethron_desktop.py --selftest
"""
import json
import os
import sys
import threading
import time

TITLE = "Ember"
MIN_W, MIN_H = 220, 140          # smaller than this is a palette, not a window


def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import AppKit  # noqa: F401
        import Quartz  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- pure parts
def to_page(x, y, screen):
    """A point in the window server's space (origin bottom-left of the main
    screen) as page coordinates (origin top-left of the screen we cover)."""
    sx, sy, sw, sh = screen
    return x - sx, (sy + sh) - y


def cg_to_page(b, screen):
    """A CGWindow bounds dict (origin TOP-left of the main display) as a
    page rectangle on the screen we cover."""
    sx, sy, sw, sh = screen
    # CG uses top-left origin of the primary display; our screen is the main
    # one, whose AppKit origin is (0,0), so only the x/y offsets apply.
    return {"x": float(b.get("X", 0)) - sx, "y": float(b.get("Y", 0)),
            "w": float(b.get("Width", 0)), "h": float(b.get("Height", 0))}


def pick_windows(infos, own_pid, screen, menubar=24):
    """Other apps' ordinary windows worth perching on, frontmost first.

    Only layer 0 (normal windows), not ours, not tiny, visible on this
    screen, with a top edge below the menu bar so Ember can sit on it."""
    sx, sy, sw, sh = screen
    out = []
    for w in infos:
        try:
            if int(w.get("kCGWindowLayer", 1)) != 0:
                continue
            if int(w.get("kCGWindowOwnerPID", -1)) == own_pid:
                continue
            if float(w.get("kCGWindowAlpha", 1)) < .3:
                continue
            r = cg_to_page(w.get("kCGWindowBounds") or {}, screen)
        except Exception:
            continue
        if r["w"] < MIN_W or r["h"] < MIN_H:
            continue
        if r["x"] + r["w"] < 40 or r["x"] > sw - 40 or r["y"] > sh - 80:
            continue
        if r["y"] < menubar + 30:          # no room above its top edge
            r = dict(r, perch=False)
        else:
            r = dict(r, perch=True)
        out.append(r)
    return out[:12]


def is_fullscreen(infos, own_pid, screen):
    """True when another app's window covers the whole screen (a film, a
    presentation, a full-screen app). Ember hides then."""
    sx, sy, sw, sh = screen
    for w in infos:
        try:
            if int(w.get("kCGWindowOwnerPID", -1)) == own_pid:
                continue
            if int(w.get("kCGWindowLayer", 1)) != 0:
                continue
            r = cg_to_page(w.get("kCGWindowBounds") or {}, screen)
        except Exception:
            continue
        if r["x"] <= 0 and r["y"] <= 0 and r["w"] >= sw - 1 and r["h"] >= sh - 1:
            return True
    return False


def dock_rect(frame, visible):
    """Where the Dock is, from the difference between the screen's frame and
    the part apps may use. Both are AppKit rects (x, y, w, h), bottom-left.
    Returns a page rect, or None when the Dock is hidden."""
    fx, fy, fw, fh = frame
    vx, vy, vw, vh = visible
    if vy - fy > 4:                                    # bottom
        return {"x": fw * .3, "y": fh - (vy - fy), "w": fw * .4, "h": vy - fy, "side": "bottom"}
    if vx - fx > 4:                                    # left
        return {"x": 0, "y": fh * .3, "w": vx - fx, "h": fh * .4, "side": "left"}
    if (fx + fw) - (vx + vw) > 4:                      # right
        d = (fx + fw) - (vx + vw)
        return {"x": fw - d, "y": fh * .3, "w": d, "h": fh * .4, "side": "right"}
    return None


def notch_rect(screen_w, menubar, left_area=None, right_area=None):
    """The notch (or, on a Mac without one, the middle of the menu bar):
    page rect. `left_area`/`right_area` are the auxiliary top areas'
    widths when the screen reports them."""
    if left_area and right_area:
        x0, x1 = left_area, screen_w - right_area
        return {"x": x0, "y": 0, "w": max(0.0, x1 - x0), "h": menubar, "notch": True}
    return {"x": screen_w / 2 - 90, "y": 0, "w": 180, "h": menubar, "notch": False}


# ---------------------------------------------------------------- the window
def _is_admin() -> bool:
    try:
        import aethron_control
        return bool((aethron_control.me() or {}).get("is_admin"))
    except Exception:
        return False


def _primary():
    """The screen with the menu bar. The window server's window list counts
    from ITS top-left, so covering this screen makes both agree."""
    from AppKit import NSScreen
    sc = NSScreen.screens()
    return sc[0] if sc else NSScreen.mainScreen()


class Desktop:
    """Owns the transparent window and the 20 Hz pump that feeds it."""

    def __init__(self, studio, base_url, log=print):
        self.studio, self.base, self.log = studio, base_url.rstrip("/"), log
        self.win = None          # pywebview window
        self.ns = None           # its NSWindow
        self.on = False
        self.ignoring = True
        self.thread = None
        self.helper = None

    # ---- on / off -----------------------------------------------------
    def set(self, on: bool):
        on = bool(on)
        if on == self.on:
            return {"on": self.on}
        self.on = on
        if on:
            self._open()
        else:
            self._close()
        return {"on": self.on}

    def _open(self):
        import inspect
        import webview
        f = _primary().frame()
        kw = dict(x=0, y=0, width=int(f.size.width), height=int(f.size.height),
                  frameless=True, transparent=True, on_top=True, easy_drag=False,
                  resizable=False, focus=True, shadow=False)
        ok = set(inspect.signature(webview.create_window).parameters)
        self.win = webview.create_window(TITLE, self.base + "/ember-desk",
                                         **{k: v for k, v in kw.items() if k in ok})
        self.win.events.loaded += self._tune
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._pump, daemon=True)
            self.thread.start()
        self.log("ember desktop: opening")

    def _close(self):
        w, self.win, self.ns = self.win, None, None
        try:
            if w:
                w.destroy()
        except Exception as e:
            self.log(f"ember desktop: close {e}")
        self.log("ember desktop: closed")

    # ---- AppKit ---------------------------------------------------------
    def _main(self, fn):
        """Run fn on the main thread (AppKit refuses window changes from any
        other once it has started)."""
        try:
            import objc
            from Foundation import NSObject
            if self.helper is None:
                class _H(NSObject):
                    def run_(self, box):
                        try:
                            box()
                        except Exception as e:  # pragma: no cover - mac only
                            print("ember desktop:", e)
                self.helper = _H.alloc().init()
            self.helper.performSelectorOnMainThread_withObject_waitUntilDone_("run:", fn, False)
        except Exception as e:
            self.log(f"ember desktop: main thread {e}")

    def _tune(self, *_):
        def go():
            from AppKit import (NSApp, NSColor, NSScreen,
                                NSWindowCollectionBehaviorCanJoinAllSpaces,
                                NSWindowCollectionBehaviorStationary,
                                NSWindowCollectionBehaviorFullScreenAuxiliary,
                                NSWindowCollectionBehaviorIgnoresCycle)
            ns = None
            try:
                ns = self.win.native.window() if hasattr(self.win.native, "window") else self.win.native
            except Exception:
                ns = None
            if ns is None or not hasattr(ns, "setIgnoresMouseEvents_"):
                for w in NSApp.windows():
                    if str(w.title()) == TITLE:
                        ns = w
            if ns is None:
                self.log("ember desktop: window not found"); return
            ns.setFrame_display_(_primary().frame(), True)
            ns.setLevel_(25)                       # status-window level: over the menu bar
            ns.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                      | NSWindowCollectionBehaviorStationary
                                      | NSWindowCollectionBehaviorFullScreenAuxiliary
                                      | NSWindowCollectionBehaviorIgnoresCycle)
            ns.setOpaque_(False)
            ns.setBackgroundColor_(NSColor.clearColor())
            ns.setHasShadow_(False)
            try:
                ns.setSharingType_(0)              # NSWindowSharingNone: not in screen shares
            except Exception:
                pass
            ns.setIgnoresMouseEvents_(True)
            self.ns, self.ignoring = ns, True
            self.log("ember desktop: ready")
        self._main(go)

    def _mouse(self, ignore: bool):
        if ignore == self.ignoring or self.ns is None:
            return
        self.ignoring = ignore
        ns = self.ns
        self._main(lambda: ns.setIgnoresMouseEvents_(ignore))

    def _key(self):
        """Let the reply box take typing: bring the window forward as key."""
        ns = self.ns
        if ns is None:
            return

        def go():
            from AppKit import NSApp
            NSApp.activateIgnoringOtherApps_(True)
            ns.makeKeyAndOrderFront_(None)
        self._main(go)

    # ---- the pump -------------------------------------------------------
    def _screen(self):
        s = _primary()
        f, v = s.frame(), s.visibleFrame()
        frame = (f.origin.x, f.origin.y, f.size.width, f.size.height)
        vis = (v.origin.x, v.origin.y, v.size.width, v.size.height)
        menubar = (f.origin.y + f.size.height) - (v.origin.y + v.size.height)
        la = ra = None
        try:
            la = s.auxiliaryTopLeftArea().size.width
            ra = s.auxiliaryTopRightArea().size.width
        except Exception:
            pass
        return s, frame, vis, max(0.0, menubar), la, ra

    def _pump(self):
        import Quartz
        from AppKit import NSEvent
        pid = os.getpid()
        last_world, world, want_key = 0.0, {}, False
        while self.on:
            time.sleep(0.05)
            if self.win is None or self.ns is None:
                continue
            try:
                _, frame, vis, menubar, la, ra = self._screen()
                m = NSEvent.mouseLocation()
                mx, my = to_page(m.x, m.y, frame)
                now = time.time()
                if now - last_world > 1.0:
                    last_world = now
                    infos = Quartz.CGWindowListCopyWindowInfo(
                        Quartz.kCGWindowListOptionOnScreenOnly
                        | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
                    infos = [dict(w) for w in infos]
                    world = {"windows": pick_windows(infos, pid, frame, menubar),
                             "fullscreen": is_fullscreen(infos, pid, frame),
                             "dock": dock_rect(frame, vis),
                             "notch": notch_rect(frame[2], menubar or 24, la, ra),
                             "menubar": menubar,
                             "admin": _is_admin()}
                sigs = []
                q = getattr(self.studio, "EMBER_SIGNALS", None)
                while q:
                    sigs.append(q.pop(0))
                payload = dict(world, mouse=[mx, my], signals=sigs)
                ans = self.win.evaluate_js(f"window.ember&&ember.desk({json.dumps(payload)})")
                hit = bool(ans and ans.get("hit")) if isinstance(ans, dict) else bool(ans)
                self._mouse(not hit)
                k = bool(isinstance(ans, dict) and ans.get("key"))
                if k and not want_key:
                    self._key()
                want_key = k
            except Exception as e:
                self.log(f"ember desktop: pump {type(e).__name__}: {e}")
                time.sleep(1)


def install(studio, base_url, log=print, home=None):
    """Called by desktop.py on a Mac. Gives the studio a switch for the
    desktop companion and re-opens it if it was on when the app last quit."""
    if not available():
        log("ember desktop: not available here")
        return None
    d = Desktop(studio, base_url, log)
    path = os.path.join(str(home or "."), "ember_desktop.json")

    def switch(on=None):
        if on is None:
            return {"available": True, "on": d.on}
        out = d.set(on)
        try:
            with open(path, "w") as fh:
                json.dump({"on": bool(on)}, fh)
        except Exception:
            pass
        return dict(out, available=True)
    studio.EMBER_DESKTOP = switch
    try:
        with open(path) as fh:
            if json.load(fh).get("on"):
                threading.Timer(3.0, lambda: d.set(True)).start()
    except Exception:
        pass
    log("ember desktop: switch ready")
    return d


# ---------------------------------------------------------------- selftest
def _selftest() -> int:
    ok = fails = 0

    def check(name, cond):
        nonlocal ok, fails
        if cond:
            ok += 1; print(f"  ok   {name}")
        else:
            fails += 1; print(f"  FAIL {name}")

    screen = (0.0, 0.0, 1512.0, 982.0)
    check("a point is flipped to the page's top-left origin", to_page(100, 900, screen) == (100, 82))
    infos = [
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 5, "kCGWindowAlpha": 1, "kCGWindowBounds": {"X": 200, "Y": 120, "Width": 900, "Height": 600}},
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 99, "kCGWindowAlpha": 1, "kCGWindowBounds": {"X": 10, "Y": 100, "Width": 800, "Height": 500}},
        {"kCGWindowLayer": 25, "kCGWindowOwnerPID": 7, "kCGWindowAlpha": 1, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1512, "Height": 24}},
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 8, "kCGWindowAlpha": 1, "kCGWindowBounds": {"X": 30, "Y": 30, "Width": 120, "Height": 80}},
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 9, "kCGWindowAlpha": 1, "kCGWindowBounds": {"X": 0, "Y": 28, "Width": 700, "Height": 400}},
    ]
    ws = pick_windows(infos, 99, screen, 24)
    check("other apps' normal windows are kept, ours and the menu bar are not",
          [w["x"] for w in ws] == [200, 0])
    check("a tiny palette is not a window", all(w["w"] >= MIN_W for w in ws))
    check("a window with no room above it is not perched on", ws[0]["perch"] and not ws[1]["perch"])
    check("nothing full screen", not is_fullscreen(infos, 99, screen))
    full = infos + [{"kCGWindowLayer": 0, "kCGWindowOwnerPID": 4, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1512, "Height": 982}}]
    check("a window covering the screen is full screen", is_fullscreen(full, 99, screen))
    check("our own full-screen window does not count", not is_fullscreen(
        [{"kCGWindowLayer": 0, "kCGWindowOwnerPID": 99, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1512, "Height": 982}}], 99, screen))
    d = dock_rect((0, 0, 1512, 982), (0, 70, 1512, 880))
    check("a bottom Dock is found from the usable area", d and d["side"] == "bottom" and abs(d["y"] - 912) < 1)
    check("a hidden Dock is none", dock_rect((0, 0, 1512, 982), (0, 0, 1512, 958)) is None)
    check("a left Dock is found", (dock_rect((0, 0, 1512, 982), (64, 0, 1448, 958)) or {}).get("side") == "left")
    n = notch_rect(1512, 37, 650, 650)
    check("the notch sits between the two top areas", n["notch"] and abs(n["x"] - 650) < 1 and abs(n["w"] - 212) < 1)
    check("without a notch, the middle of the menu bar", not notch_rect(1440, 24)["notch"])
    check("off a Mac it is simply unavailable", sys.platform == "darwin" or not available())
    print(f"\n{ok} passed, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
