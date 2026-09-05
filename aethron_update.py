#!/usr/bin/env python3
"""Aethron self-update — the app replaces itself, in place.

WHY THIS EXISTS

The owner shipped a new build and ended up with TWO Aethron.app copies
on their Mac: macOS opened the old one, so they had to find it, delete
it by hand, and move the new one into /Applications before they could
use it. That is not a packaging detail, it is the product failing at
the first thing a desktop app must do — keep itself current.

THE RULE THAT PREVENTS COPIES: update the bundle WHERE IT ALREADY IS.
The running app resolves its own location from sys.executable and
replaces that exact bundle. Nothing is ever written to a second place,
so a second copy can never appear.

SAFETY, in order:
  1. download to a temp dir, never near the live app
  2. verify the download is a real bundle before anything is touched
  3. move the CURRENT app aside (kept, not deleted)
  4. move the new one into its place
  5. if any step fails, put the original back and report honestly
  6. only after the new app is in place is the backup removed

A failed update leaves the user with a working app. That is the whole
design constraint.

    python3 aethron_update.py --check      what is available
    python3 aethron_update.py --selftest   prove the mechanism, offline
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Bump this when cutting a release; the tag on GitHub must match
# (with or without a leading "v").
VERSION = "1.0.5"

REPO = os.environ.get("AETHRON_UPDATE_REPO", "zenadell/Aethron")
GITHUB_FEED = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 30


def feed_url() -> str:
    """Where to ask about updates.

    A PRIVATE REPO CANNOT SERVE ITS OWN UPDATES. The app checks
    anonymously, and api.github.com answers 404 for a private
    repository — so a published release is invisible and the owner is
    back to installing by hand, which is the whole thing this exists to
    stop. Any plain-JSON URL works instead:

        {"version": "1.0.1",
         "url": "https://…/Aethron-mac.zip",
         "notes": "what changed"}

    Supabase storage serves that from a public bucket with no key, so
    the source stays private and the updates do not.
    """
    env = os.environ.get("AETHRON_UPDATE_FEED", "").strip()
    if env:
        return env
    try:
        cfg = json.loads((Path(__file__).resolve().parent
                          / "aethron_config.json").read_text())
        if cfg.get("update_feed"):
            return str(cfg["update_feed"]).strip()
        base = str(cfg.get("supabase_url") or "").rstrip("/")
        if base:
            return (base + "/storage/v1/object/public/releases/"
                    + f"{platform_key()}.json")
    except Exception:
        pass
    return GITHUB_FEED


# ─────────────────────────── version compare ─────────────────────────

def parse_version(v):
    """'v1.2.3' -> (1,2,3). Unparseable parts sort as 0, never crash."""
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(candidate, current=VERSION) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# ─────────────────────────── where we live ───────────────────────────

def app_bundle() -> Path | None:
    """The .app that is RUNNING, or None in dev.

    sys.executable inside a bundle is
    …/Aethron.app/Contents/MacOS/Aethron — walk up to the .app. In dev
    this returns None and every caller reports "not packaged" rather
    than pretending it can update a git checkout.
    """
    if not getattr(sys, "frozen", False):
        return None
    p = Path(sys.executable).resolve()
    for parent in [p] + list(p.parents):
        if parent.suffix == ".app":
            return parent
    return None


def platform_key() -> str:
    if sys.platform == "darwin":
        return "mac"
    if os.name == "nt":
        return "win"
    return "linux"


# ─────────────────────────── the feed ────────────────────────────────

def check(timeout=TIMEOUT) -> dict:
    """-> {available, version, url, notes, why}. Never raises."""
    url = feed_url()
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": f"Aethron/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.loads(r.read().decode("utf8", "replace"))
    except urllib.error.HTTPError as e:
        # Supabase answers 400 (not 404) for a bucket or object that
        # does not exist yet. Before the first publish that is the
        # NORMAL state, and "update check failed (400)" in the UI reads
        # like a broken app instead of an empty shelf.
        body = ""
        try:
            body = e.read().decode("utf8", "replace")[:300]
        except Exception:
            pass
        if e.code == 404 or "not found" in body.lower():
            return {"available": False, "version": VERSION,
                    "why": "no releases published yet"}
        return {"available": False, "version": VERSION,
                "why": f"update check failed ({e.code})"}
    except Exception as e:
        return {"available": False, "version": VERSION,
                "why": f"update check failed ({type(e).__name__})"}

    # a hosted feed is {version, url, notes}; a GitHub release is
    # {tag_name, assets[]}. Accept either, so moving the feed later
    # needs no new app build.
    if "version" in rel and "assets" not in rel:
        tag = str(rel.get("version") or "")
        if not is_newer(tag):
            return {"available": False, "version": VERSION, "latest": tag,
                    "why": "you are on the latest version"}
        if not rel.get("url"):
            return {"available": False, "version": VERSION, "latest": tag,
                    "why": f"{tag} has no download url in the feed"}
        return {"available": True, "version": VERSION, "latest": tag,
                "url": rel["url"], "notes": (rel.get("notes") or "")[:2000]}

    tag = rel.get("tag_name") or rel.get("name") or ""
    if not is_newer(tag):
        return {"available": False, "version": VERSION,
                "latest": tag, "why": "you are on the latest version"}

    key = platform_key()
    url = ""
    for a in rel.get("assets") or []:
        name = (a.get("name") or "").lower()
        if name.endswith(".zip") and key in name:
            url = a.get("browser_download_url") or ""
            break
    if not url:
        return {"available": False, "version": VERSION, "latest": tag,
                "why": f"{tag} has no {key} download attached"}
    return {"available": True, "version": VERSION, "latest": tag,
            "url": url, "notes": (rel.get("body") or "").strip()[:2000]}


# ─────────────────────────── download + verify ───────────────────────

def _verify_bundle(path: Path) -> bool:
    """A real .app, not a zip of something else."""
    return (path.suffix == ".app"
            and (path / "Contents" / "MacOS").is_dir()
            and any((path / "Contents" / "MacOS").iterdir()))


def _extract(zpath: Path, into: Path):
    """Unpack a .app — WITH ITS SYMLINKS.

    THIS IS THE BUG THAT BRICKED THE FIRST REAL UPDATE, and nothing
    short of launching the result would have shown it. The build ships
    81 symlinks (Python.framework/Versions/Current and friends);
    zipfile.extractall writes every one of them out as a REGULAR FILE.
    The bundle still looks perfect — right size, right layout, launches
    nothing. macOS answers "Launchd job spawn failed", because the
    framework layout is gone and the code signature no longer matches.

    ditto is the only extractor on macOS that preserves symlinks,
    permissions and extended attributes, which is exactly why the build
    script uses `ditto -c -k` to create the archive in the first place.
    Use the matching tool to open it.
    """
    with zipfile.ZipFile(zpath) as z:
        # zip-slip guard runs on the LISTING, before anything is
        # written, so it protects whichever extractor runs below.
        for m in z.namelist():
            if m.startswith("/") or ".." in Path(m).parts:
                raise ValueError(f"unsafe path in download: {m}")
    if sys.platform == "darwin" and shutil.which("ditto"):
        r = subprocess.run(["ditto", "-x", "-k", str(zpath), str(into)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise ValueError(f"could not unpack the download: "
                             f"{(r.stderr or '').strip()[:200]}")
        return
    with zipfile.ZipFile(zpath) as z:
        z.extractall(into)


def _runnable(app: Path) -> bool:
    """Would macOS actually launch this? Checked BEFORE we swap.

    A bundle whose signature does not match its contents is refused by
    launchd with an error the user cannot act on, so the honest place
    to find out is here — while the working app is still in place.
    Unsigned builds (our beta) are fine; INVALID ones are not.
    """
    if sys.platform != "darwin" or not shutil.which("codesign"):
        return True
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict",
                        str(app)], capture_output=True, text=True)
    if r.returncode == 0:
        return True
    err = (r.stderr or "") + (r.stdout or "")
    if "not signed at all" in err:
        # ad-hoc re-sign and re-check: recoverable, and never silently
        # assumed — if it still fails we refuse the update.
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-",
                        str(app)], capture_output=True)
        return subprocess.run(["codesign", "--verify", "--deep", "--strict",
                               str(app)], capture_output=True).returncode == 0
    return False


def stage(url, into: Path, timeout=180, progress=None) -> Path:
    """Download + unzip into `into`. -> the extracted .app. Raises on
    anything that would leave us with something unusable."""
    into.mkdir(parents=True, exist_ok=True)
    zpath = into / "download.zip"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"Aethron/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r, \
            open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(got / total)
    _extract(zpath, into)
    zpath.unlink(missing_ok=True)

    apps = [p for p in into.rglob("*.app") if _verify_bundle(p)]
    if not apps:
        raise ValueError("the download did not contain a usable app")
    app = sorted(apps, key=lambda p: len(p.parts))[0]
    if not _runnable(app):
        raise ValueError("the downloaded app is not launchable "
                         "(damaged signature) — keeping the current one")
    return app


# ─────────────────────────── the swap ────────────────────────────────

def apply(new_app: Path, current: Path | None = None) -> Path:
    """Replace the running bundle IN PLACE. -> the updated path.

    The old bundle is moved aside first and only removed once the new
    one is in position, so an interrupted update still leaves a working
    app where the user expects it.
    """
    current = current or app_bundle()
    if not current:
        raise RuntimeError("not running as a packaged app — nothing to "
                           "update (use build_desktop.sh in dev)")
    if not _verify_bundle(new_app):
        raise ValueError("refusing to install: not a valid app bundle")

    backup = current.with_name(current.name + ".old")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    os.rename(current, backup)             # same volume, atomic
    try:
        shutil.move(str(new_app), str(current))
    except Exception:
        os.rename(backup, current)         # put it back, untouched
        raise
    if not _verify_bundle(current):
        shutil.rmtree(current, ignore_errors=True)
        os.rename(backup, current)
        raise ValueError("installed bundle failed verification — "
                         "the original has been restored")
    shutil.rmtree(backup, ignore_errors=True)
    if sys.platform == "darwin":
        # clear the quarantine bit so the replaced app opens without a
        # second Gatekeeper prompt (unsigned builds only)
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(current)],
                       capture_output=True)
    return current


def relaunch(app: Path):
    """Start the updated app and let this process exit."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-n", str(app)], start_new_session=True)
    elif os.name == "nt":
        subprocess.Popen([str(app)], close_fds=True)
    else:
        subprocess.Popen([str(app)], start_new_session=True)


def update(progress=None) -> dict:
    """check -> stage -> apply. -> {ok, version, path} | {ok:False, why}"""
    info = check()
    if not info.get("available"):
        return {"ok": False, "why": info.get("why", "no update available")}
    cur = app_bundle()
    if not cur:
        return {"ok": False, "why": "not running as a packaged app"}
    tmp = Path(tempfile.mkdtemp(prefix="aethron-update-"))
    try:
        newapp = stage(info["url"], tmp, progress=progress)
        installed = apply(newapp, cur)
        return {"ok": True, "version": info["latest"],
                "path": str(installed)}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── selftest ────────────────────────────────

def _selftest() -> int:
    ok = True

    def check_(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"   {detail}" if not cond and detail else ""))

    print("── version comparison")
    check_("v1.0.1 is newer than 1.0.0", is_newer("v1.0.1", "1.0.0"))
    check_("1.0.0 is not newer than itself", not is_newer("1.0.0", "1.0.0"))
    check_("1.10.0 beats 1.9.0 (not string order)",
           is_newer("1.10.0", "1.9.0"))
    check_("2.0 beats 1.9.9", is_newer("2.0", "1.9.9"))
    check_("garbage never counts as newer", not is_newer("", "1.0.0"))

    print("── bundle verification")
    tmp = Path(tempfile.mkdtemp(prefix="aethron-selftest-"))
    good = tmp / "Aethron.app"
    (good / "Contents" / "MacOS").mkdir(parents=True)
    (good / "Contents" / "MacOS" / "Aethron").write_text("#!/bin/sh\n")
    check_("a real bundle verifies", _verify_bundle(good))
    empty = tmp / "Empty.app"
    (empty / "Contents" / "MacOS").mkdir(parents=True)
    check_("an empty bundle is refused", not _verify_bundle(empty))
    check_("a plain folder is refused", not _verify_bundle(tmp / "nope"))

    print("── the in-place swap")
    live = tmp / "installed" / "Aethron.app"
    (live / "Contents" / "MacOS").mkdir(parents=True)
    (live / "Contents" / "MacOS" / "Aethron").write_text("OLD")
    incoming = tmp / "staged" / "Aethron.app"
    (incoming / "Contents" / "MacOS").mkdir(parents=True)
    (incoming / "Contents" / "MacOS" / "Aethron").write_text("NEW")
    out = apply(incoming, live)
    check_("updated in place — same path", out == live, str(out))
    check_("contents replaced",
           (live / "Contents" / "MacOS" / "Aethron").read_text() == "NEW")
    check_("no second copy left behind",
           len(list((tmp / "installed").iterdir())) == 1)
    check_("backup removed after success",
           not live.with_name(live.name + ".old").exists())

    print("── a bad download never replaces a working app")
    (live / "Contents" / "MacOS" / "Aethron").write_text("STILL WORKING")
    junk = tmp / "junk" / "Aethron.app"
    junk.mkdir(parents=True)                       # no Contents/MacOS
    try:
        apply(junk, live)
        check_("refuses an invalid bundle", False, "it installed junk")
    except Exception:
        check_("refuses an invalid bundle", True)
    check_("original untouched after refusal",
           (live / "Contents" / "MacOS" / "Aethron").read_text()
           == "STILL WORKING")

    print("── symlinks survive the download (the bricked-update bug)")
    src = tmp / "src" / "Aethron.app"
    (src / "Contents" / "MacOS").mkdir(parents=True)
    (src / "Contents" / "MacOS" / "Aethron").write_text("#!/bin/sh\n")
    fw = src / "Contents" / "Frameworks" / "Python.framework" / "Versions"
    (fw / "3.13").mkdir(parents=True)
    (fw / "3.13" / "Python").write_text("lib")
    os.symlink("3.13", fw / "Current")          # <- what zipfile destroys
    zp2 = tmp / "app.zip"
    if sys.platform == "darwin":
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(src), str(zp2)], check=True, capture_output=True)
    else:
        with zipfile.ZipFile(zp2, "w") as z:
            for p in src.rglob("*"):
                z.write(p, p.relative_to(src.parent))
    out = tmp / "unpacked"
    out.mkdir()
    _extract(zp2, out)
    got = out / "Aethron.app"
    link = got / "Contents/Frameworks/Python.framework/Versions/Current"
    check_("the bundle extracts", _verify_bundle(got), str(got))
    check_("a symlink is still a symlink, not a copied file",
           link.is_symlink(),
           "extracted as a regular file — this is what made the updated "
           "app refuse to launch")
    check_("it still points where it did", link.is_symlink()
           and os.readlink(link) == "3.13")

    # …and a bundle macOS would refuse to spawn must never reach the
    # swap. This hand-made app cannot be signed, so it stands in for a
    # damaged download.
    if sys.platform == "darwin":
        try:
            stage("file://" + str(zp2), tmp / "refused")
            check_("an unlaunchable download is refused", False,
                   "it was accepted")
        except Exception as e:
            check_("an unlaunchable download is refused",
                   "not launchable" in str(e), str(e)[:80])

    print("── zip-slip")
    zp = tmp / "evil.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("../escaped.txt", "nope")
    try:
        stage("file://" + str(zp), tmp / "slip")
        check_("escaping paths refused", False, "extracted outside")
    except Exception as e:
        check_("escaping paths refused", "unsafe path" in str(e)
               or isinstance(e, (ValueError, urllib.error.URLError)))

    print("── the hosted feed (a PRIVATE repo cannot serve its own)")
    import http.server
    import threading
    feed = {"version": "9.9.9", "url": "https://example.invalid/A.zip",
            "notes": "hosted"}
    body = {"/mac.json": json.dumps(feed).encode(),
            "/same.json": json.dumps(dict(feed, version=VERSION)).encode(),
            "/nourl.json": json.dumps({"version": "9.9.9"}).encode(),
            "/gh.json": json.dumps({
                "tag_name": "v9.9.9",
                "assets": [{"name": "Aethron-mac.zip",
                            "browser_download_url": "https://x/A.zip"}]}).encode()}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = body.get(self.path)
            self.send_response(200 if b else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b or b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    old_env = os.environ.get("AETHRON_UPDATE_FEED")
    try:
        os.environ["AETHRON_UPDATE_FEED"] = base + "/mac.json"
        check_("feed_url honours the env override",
               feed_url() == base + "/mac.json", feed_url())
        r = check(timeout=5)
        check_("a hosted {version,url} feed offers the update",
               r.get("available") and r.get("url") == feed["url"], str(r))
        os.environ["AETHRON_UPDATE_FEED"] = base + "/same.json"
        check_("same version offers nothing",
               not check(timeout=5).get("available"))
        os.environ["AETHRON_UPDATE_FEED"] = base + "/nourl.json"
        r = check(timeout=5)
        check_("a feed with no download url is refused, with a reason",
               not r.get("available") and "download url" in r.get("why", ""),
               str(r))
        os.environ["AETHRON_UPDATE_FEED"] = base + "/gh.json"
        r = check(timeout=5)
        check_("a GitHub release still works through the same path",
               r.get("available") and r.get("url") == "https://x/A.zip",
               str(r))
        os.environ["AETHRON_UPDATE_FEED"] = base + "/missing.json"
        r = check(timeout=5)
        check_("nothing published yet is not an error",
               not r.get("available") and r.get("why"), str(r))
    finally:
        srv.shutdown()
        if old_env is None:
            os.environ.pop("AETHRON_UPDATE_FEED", None)
        else:
            os.environ["AETHRON_UPDATE_FEED"] = old_env

    print("── dev mode is honest")
    check_("no bundle in dev", app_bundle() is None)
    r = update()
    check_("update() refuses in dev with a reason",
           not r["ok"] and r["why"], str(r))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nupdate selftest:", "ok" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--check" in sys.argv:
        print(json.dumps(check(), indent=1))
        sys.exit(0)
    print(json.dumps(update(progress=lambda f:
                     print(f"  {f*100:.0f}%", end="\r")), indent=1))
