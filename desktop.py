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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--forge":
        run_forge_mode(sys.argv[2:])

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
    except ImportError:
        webbrowser.open(url)                 # fallback: system browser
        return server.serve_forever()

    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        webview.create_window("Aethron", url, width=1280, height=840,
                              min_size=(940, 620))
        webview.start()                      # blocks until the window closes
    except Exception as e:
        print(f"(native window unavailable: {e}; using browser)")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)             # server runs in the daemon thread
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
