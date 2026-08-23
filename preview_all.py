#!/usr/bin/env python3
"""Serve every original and every converted port side by side.

The only honest way to judge a port is to look at it next to the
original, so this finds all of them and puts each on its own port:

    python3 preview_all.py                 everything it can find
    python3 preview_all.py agero test-2    just these projects

    ORIGINAL  projects/<name>/site
    astro     projects/<name>/convert-astro/dist
    next      projects/<name>/convert-next/out
    vite      projects/<name>/convert-vite/dist

Lives in the repo on purpose: the previous copy was written to a scratch
directory and vanished with the session, which is now the third time
that has cost real work.
"""
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import forge  # noqa: E402

PROJECTS = ROOT / "projects"
TARGETS = [("ORIGINAL", "site", ""),
           ("astro", "convert-astro/dist", "static"),
           ("next / React", "convert-next/out", "static"),
           ("vite", "convert-vite/dist", "static")]


def platform_of(project: Path) -> str:
    try:
        return json.loads((project / "forge.json").read_text(
            encoding="utf-8")).get("platform", "static")
    except Exception:
        return "static"


def main(argv):
    wanted = [a for a in argv if not a.startswith("-")]
    if not PROJECTS.is_dir():
        print("no projects/ directory here")
        return 1
    port, served = 8801, []
    for project in sorted(p for p in PROJECTS.iterdir() if p.is_dir()):
        if wanted and project.name not in wanted:
            continue
        found = [(label, project / rel, plat)
                 for label, rel, plat in TARGETS if (project / rel).is_dir()]
        if len(found) < 2:
            continue            # nothing to compare against
        print(f"\n{project.name}")
        for label, path, plat in found:
            handler = forge._site_handler(
                path, plat or platform_of(project), quiet=True)
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
            except OSError as e:
                print(f"  port {port} unavailable ({e}) — skipped")
                port += 1
                continue
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"  http://127.0.0.1:{port}/   {label}")
            served.append(port)
            port += 1
        port += 10 - (port % 10)          # a clean gap between projects

    if not served:
        print("nothing to serve — convert a project first")
        return 1
    print(f"\n{len(served)} site(s) up. Ctrl-C, or: pkill -f preview_all")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
