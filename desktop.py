#!/usr/bin/env python3
"""Aethron desktop launcher — the entry point of the packaged app.

Normal launch:  starts the Studio server on a free local port and opens
                the user's browser at it. User data (projects/, library/)
                lives in the OS data directory, never inside the bundle.

`--forge` mode: the SAME binary doubles as the engine. The Studio runs
                every mechanical step as a subprocess; in a frozen app
                there is no `python3`, so it invokes itself:
                    Aethron --forge build …  ->  forge.py's dispatcher
                This keeps crash isolation and parallel previews without
                shipping a Python interpreter.

Build:  bash build_desktop.sh   (see docs/DESKTOP.md)
"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _log(msg):
    """A windowed bundle (console=False) has NO stdout — every print in
    this file has been going nowhere, which is why a fallback could
    happen silently and look like a mystery. Write to a file instead."""
    try:
        import datetime
        d = data_home()
        with open(d / "aethron.log", "a") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S} {msg}\n")
    except Exception:
        pass


def data_home() -> Path:
    """Writable per-user data dir: the bundle itself is read-only."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME",
                                   Path.home() / ".local" / "share"))
    d = base / "Aethron"
    d.mkdir(parents=True, exist_ok=True)
    return d


def free_port(preferred=8899) -> int:
    # Try a small BOUNDED range first, not a random port: Google/Supabase
    # OAuth needs the loopback callback (http://127.0.0.1:<port>/auth/
    # callback) to be in the redirect allow-list, so the port must be
    # predictable. Register 8899-8909 (or a wildcard) once. Random 0 is
    # the last resort (login won't work on it, but local editing will).
    for port in list(range(preferred, preferred + 11)) + [0]:
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    return preferred


def run_forge_mode(args):
    """`Aethron --forge <cmd> …` — dispatch into the engine in-process.
    cwd is already the project dir (the Studio sets it), exactly like
    `python3 forge.py <cmd>` in development."""
    import forge
    if not args or args[0] not in forge.COMMANDS:
        print(f"unknown forge command {args[:1]!r}; "
              f"known: {sorted(forge.COMMANDS)}")
        sys.exit(1)
    forge.COMMANDS[args[0]](args[1:])
    sys.exit(0)


def run_mcp_mode():
    """`Aethron --mcp` — BE the MCP server, over stdio.

    THE BUG THIS EXISTS FOR. The coding agent is handed Aethron's own
    tools through an --mcp-config that spawns `sys.executable
    <path>/forge_mcp.py`. In development that path is a real file. In the
    packaged app forge_mcp.py is inside the compiled archive, not on
    disk, so `FORGE_MCP.exists()` was False, no --mcp-config was passed,
    and the migration agent shipped to users with ZERO mcp__aethron__
    tools — while its own system prompt told it to call create_project,
    fetch, inventory and build.

    It could not, so it did the only thing left: fetched the page with
    curl and offered to rewrite the site by hand. That is the approach
    this project measured and rejected (75% of the text, a design that
    merely resembles the original). The model was not off-thesis; it was
    unequipped.

    Worse, --strict-mcp-config is only passed alongside --mcp-config, so
    the same gap let the user's personal MCP servers into a session that
    is supposed to be reproducible.

    Same trick as --forge: one binary, two roles.
    """
    import forge_mcp
    forge_mcp.main()
    sys.exit(0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--forge":
        run_forge_mode(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp":
        run_mcp_mode()

    home = Path(os.environ.get("AETHRON_HOME") or data_home())
    os.environ["AETHRON_HOME"] = str(home)

    import studio                      # honors AETHRON_HOME

    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    studio.PROJECTS.mkdir(parents=True, exist_ok=True)

    # bind FIRST, then present the UI — never a "connection refused"
    # window, even on a slow cold start
    server = studio.ThreadingHTTPServer(("127.0.0.1", port), studio.Handler)
    print(f"Aethron → {url}")
    print(f"your data: {home}")

    # Prefer a REAL native window (pywebview → a WKWebView on macOS): no
    # browser chrome, no visible 127.0.0.1, it reads as an app. Google
    # sign-in still opens the SYSTEM browser (Google blocks OAuth inside
    # embedded webviews) and loops back via polling — handled in studio.
    try:
        import webview                       # pywebview
    except ImportError as e:
        _log(f"pywebview NOT BUNDLED: {e}")
        webbrowser.open(url)                 # fallback: system browser
        return server.serve_forever()

    threading.Thread(target=server.serve_forever, daemon=True).start()

    def raise_window():
        """Bring Aethron to the front — called when Google sign-in
        completes in the system browser, so the user lands back in the
        app instead of on a stray browser tab."""
        try:                                  # native, no shell-out
            from AppKit import NSApp, NSApplication
            NSApplication.sharedApplication()
            NSApp.activateIgnoringOtherApps_(True)
            return
        except Exception:
            pass
        try:                                  # fallback: ask the OS
            import subprocess
            subprocess.run(
                ["osascript", "-e",
                 'tell application "Aethron" to activate'],
                capture_output=True, timeout=5)
        except Exception:
            pass

    studio.FOCUS_APP = raise_window

    def install_liquid_glass():
        """Put the REAL system material in the window, behind the page.

        Aethron's interface is HTML in a WKWebView, so SwiftUI's
        `.glassEffect` can never apply to a <div> — but this is a native
        app, so the inverse works: NSGlassEffectView goes into the window
        as the bottom-most view and the page (launched with ?glass=1)
        stops painting its own ground. What shows through is the actual
        macOS 26+ Liquid Glass material, drawn by the system, not a CSS
        impression of it.

        Silent and optional by design: on an older macOS the class does
        not exist and the app looks exactly as it did before.
        """
        try:
            import objc
            from Foundation import NSObject
            from AppKit import (NSApp, NSColor, NSViewWidthSizable,
                                NSViewHeightSizable, NSWindowBelow)
            try:
                GLASS = objc.lookUpClass('NSGlassEffectView')
            except Exception:
                _log("liquid glass: not on this macOS")
                return

            class _Install(NSObject):
                # AppKit refuses layout changes from a background thread
                # once the main thread has touched it, and by now it has.
                def go_(self, _):
                    wins = [w for w in NSApp.windows() if w.isVisible()]
                    if not wins:
                        _log("liquid glass: no window yet"); return
                    win = wins[-1]
                    root = win.contentView()
                    g = GLASS.alloc().initWithFrame_(root.bounds())
                    g.setStyle_(0)                       # Regular
                    g.setCornerRadius_(0)
                    try: g.setEffectIsInteractive_(True)
                    except Exception: pass
                    g.setAutoresizingMask_(
                        NSViewWidthSizable | NSViewHeightSizable)
                    root.addSubview_positioned_relativeTo_(
                        g, NSWindowBelow, None)
                    win.setOpaque_(False)
                    win.setBackgroundColor_(NSColor.clearColor())
                    _log("liquid glass: installed")

            def later():
                time.sleep(2.5)          # after the first paint
                _Install.alloc().init()\
                    .performSelectorOnMainThread_withObject_waitUntilDone_(
                        'go:', None, False)
            threading.Thread(target=later, daemon=True).start()
        except Exception as e:
            _log(f"liquid glass: skipped ({type(e).__name__}: {e})")

    try:
        install_liquid_glass()
        webview.create_window("Aethron", url + "?glass=1",
                              width=1280, height=840,
                              min_size=(940, 620), transparent=True)
        # THE REASON LOGIN NEVER STUCK. pywebview defaults
        # private_mode=True, and on macOS that branch CLEARS every
        # website data type from epoch on each launch (read the cocoa
        # backend: `if _state['private_mode']` → removeDataOfTypes from
        # 1970). So the server could remember a session perfectly — it
        # did, the file had one stored — and the window still arrived
        # with no cookie and showed sign-in.
        #
        # private_mode=False skips that wipe and uses
        # WKWebsiteDataStore.defaultDataStore(), which persists to
        # ~/Library/WebKit/<bundle-id>/WebsiteData.
        #
        # storage_path is IGNORED by the macOS backend (zero references
        # in it) — it is passed for Windows/GTK, where it is honoured.
        # Do not read the absence of files under it as a failure on a
        # Mac; nothing writes there.
        store = home / "webview"
        store.mkdir(parents=True, exist_ok=True)
        import inspect
        _log(f"pywebview {getattr(webview, '__version__', '?')} "
             f"start params={list(inspect.signature(webview.start).parameters)}")
        webview.start(private_mode=False, storage_path=str(store))
        _log("webview.start returned (window closed)")
    except Exception as e:
        _log(f"NATIVE WINDOW FAILED: {type(e).__name__}: {e}")
        print(f"(native window unavailable: {e}; using browser)")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)             # server runs in the daemon thread
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
