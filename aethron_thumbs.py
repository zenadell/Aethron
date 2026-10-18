#!/usr/bin/env python3
"""Pictures of a project's site: its card on Home and the Before / After slider.

    shot(project_dir, "site", "index.html", forge_cmd)      -> Path | None
    shot(project_dir, "original", "index.html", forge_cmd)  -> Path | None

"site" is what the project builds today. "original" is the same pages built
from the TEMPLATE's content: a copy of the project with every fill, style
and removal taken out, built by the same engine. Both are served with the
same handler `forge serve` uses (Framer's ?range= and .js@ protocols
included) and shot at 1440x900 by headless Chrome. Without those protocols
a Framer page renders blank, and so would the picture.

Pictures are cached in <project>/.thumbs/. A "site" picture is redone
when the page it shows was rebuilt after it; an "original" never changes,
because the template does not.

Shooting is slow (a few seconds; the first "original" also needs a build),
so the studio asks for it in the background with request(), and the page
polls state(). One Chrome at a time: a Home screen with twenty projects
must not start twenty browsers on someone's laptop.
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

W, H = 1440, 900
WHICH = ("site", "original")

_LOCK = threading.Lock()          # guards _STATE
_SHOOT = threading.Semaphore(1)   # one browser at a time
_STATE = {}                       # (dir, which, page) -> "pending" | "failed: why"


def _safe(page: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", page or "index.html")[:80]


def path(d: Path, which: str, page: str = "index.html") -> Path:
    return Path(d) / ".thumbs" / f"{which}-{_safe(page)}.png"


def _page_file(site: Path, page: str) -> Path:
    page = page or "index.html"
    f = site / page
    return f if f.is_file() else site / "index.html"


def fresh(d: Path, which: str, page: str = "index.html") -> bool:
    p = path(d, which, page)
    if not (p.is_file() and p.stat().st_size):
        return False
    if which == "original":
        return True
    src = _page_file(Path(d) / "site", page)
    return src.is_file() and p.stat().st_mtime >= src.stat().st_mtime


def state(d: Path, which: str, page: str = "index.html") -> dict:
    """{"state": "ready"|"pending"|"none"|"failed", "v": mtime, "why": …}"""
    d = Path(d)
    if which == "site" and not (d / "site" / "index.html").is_file():
        return {"state": "none", "why": "not built yet"}
    with _LOCK:
        s = _STATE.get((str(d), which, page))
    if s == "pending":
        return {"state": "pending"}
    if fresh(d, which, page):
        return {"state": "ready", "v": int(path(d, which, page).stat().st_mtime)}
    if s and s.startswith("failed"):
        return {"state": "failed", "why": s[len("failed: "):]}
    return {"state": "none"}


def request(d: Path, which: str, page: str, forge_cmd) -> dict:
    """Start making the picture in the background unless it is fresh or
    already on its way. Returns state() as it stands now."""
    d = Path(d)
    st = state(d, which, page)
    if st["state"] in ("ready", "pending") or (
            which == "site" and st.get("why") == "not built yet"):
        return st
    key = (str(d), which, page)
    with _LOCK:
        if _STATE.get(key) == "pending":
            return {"state": "pending"}
        _STATE[key] = "pending"

    def run():
        try:
            out = shot(d, which, page, forge_cmd)
            res = None if out else "failed: no picture (is a browser installed?)"
        except Exception as e:                       # noqa: BLE001
            res = f"failed: {e}"
        with _LOCK:
            if res:
                _STATE[key] = res
            else:
                _STATE.pop(key, None)
    threading.Thread(target=run, daemon=True).start()
    return {"state": "pending"}


def _serve(site: Path, platform: str):
    import forge
    handler = forge._site_handler(site, platform, quiet=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _platform(d: Path) -> str:
    try:
        return json.loads((Path(d) / "forge.json").read_text())["platform"]
    except (OSError, ValueError, KeyError):
        return "static"


def original_site(d: Path, forge_cmd, work: Path) -> Path:
    """Build the project's pages from the template's own content into
    `work`. The template's pristine files are hard-linked, not copied."""
    d = Path(d)
    twin = work / d.name
    keep = {"pristine", "copy_map.json", "forge.json", "animations.json"}

    def share(src, dst):
        # only the template's own files are shared; anything a build may
        # write to (forge.json, the copy map) is a real copy
        if "pristine" in Path(src).relative_to(d).parts \
                and not Path(src).name.startswith(".forge-"):
            try:
                return os.link(src, dst)
            except OSError:
                pass
        return shutil.copy2(src, dst)
    shutil.copytree(d, twin, copy_function=share,
                    ignore=lambda w, n: [] if Path(w) != d
                    else [x for x in n if x not in keep])
    cm_f = twin / "copy_map.json"
    if cm_f.is_file():
        cm = json.loads(cm_f.read_text(encoding="utf-8"))
        for sec, v in list(cm.items()):
            if isinstance(v, list):
                for e in v:
                    if isinstance(e, dict) and "new" in e:
                        e["new"] = ""
            if sec in ("styles", "remove"):
                cm[sec] = []
        cm_f.write_text(json.dumps(cm, indent=1), encoding="utf-8")
    r = subprocess.run(list(forge_cmd) + ["build"], cwd=twin,
                       capture_output=True, text=True, timeout=600)
    site = twin / "site"
    if not (site / "index.html").is_file():
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        raise RuntimeError("the template would not build: " + " / ".join(tail))
    return site


def shot(d: Path, which: str, page: str, forge_cmd) -> Path | None:
    import aethron_figma_grade as G
    if which not in WHICH:
        raise ValueError(which)
    d = Path(d)
    b = G.find_browser()
    if not b:
        return None
    with _SHOOT:
        work = Path(tempfile.mkdtemp(prefix="ae-thumb-"))
        srv = None
        try:
            site = d / "site" if which == "site" \
                else original_site(d, forge_cmd, work)
            srv = _serve(site, _platform(d))
            rel = _page_file(site, page).relative_to(site).as_posix()
            url = f"http://127.0.0.1:{srv.server_address[1]}/" \
                + ("" if rel == "index.html" else rel)
            tmp = work / "shot.png"
            if not G.shoot(Path("unused.html"), W, H, tmp, browser=b, url=url):
                return None
            out = path(d, which, page)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), out)
            return out
        finally:
            if srv:
                srv.shutdown()
            shutil.rmtree(work, ignore_errors=True)


def _selftest():
    """Offline checks that need no browser."""
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        ok, fail = ok + bool(cond), fail + (not cond)
        print(("  ok   " if cond else "  FAIL ") + name)
    t = Path(tempfile.mkdtemp(prefix="ae-thumbs-test-"))
    try:
        check("a page name cannot leave the folder",
              path(t, "site", "../../etc/passwd").parent == t / ".thumbs")
        check("an unbuilt project has no site picture to make",
              state(t, "site")["state"] == "none")
        (t / "site").mkdir()
        (t / "site" / "index.html").write_text("<p>x</p>")
        check("a built project with no picture yet says none",
              state(t, "site")["state"] == "none")
        p = path(t, "site")
        p.parent.mkdir()
        p.write_bytes(b"png")
        check("a picture newer than its page is ready",
              state(t, "site")["state"] == "ready")
        time.sleep(0.02)
        os.utime(t / "site" / "index.html")
        old = time.time() - 60
        os.utime(p, (old, old))
        check("a rebuild makes the picture stale",
              state(t, "site")["state"] == "none")
        check("an unknown page falls back to the home page",
              _page_file(t / "site", "nope.html").name == "index.html")
        q = path(t, "original")
        q.write_bytes(b"png")
        os.utime(q, (old, old))
        check("the original never goes stale", fresh(t, "original"))
    finally:
        shutil.rmtree(t, ignore_errors=True)
    print(f"thumbs selftest: {ok} ok, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    d, which = Path(sys.argv[1]), sys.argv[2]
    page = sys.argv[3] if len(sys.argv) > 3 else "index.html"
    print(shot(d, which, page, [sys.executable, str(ROOT / "forge.py")]))
