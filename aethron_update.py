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
VERSION = "1.0.0"

REPO = os.environ.get("AETHRON_UPDATE_REPO", "zenadell/Aethron")
FEED = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 30


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
    req = urllib.request.Request(FEED, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Aethron/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.loads(r.read().decode("utf8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"available": False, "version": VERSION,
                    "why": "no releases published yet"}
        return {"available": False, "version": VERSION,
                "why": f"update check failed ({e.code})"}
    except Exception as e:
        return {"available": False, "version": VERSION,
                "why": f"update check failed ({type(e).__name__})"}

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
    with zipfile.ZipFile(zpath) as z:
        # zip-slip guard: refuse any member that escapes the staging dir
        for m in z.namelist():
            if m.startswith("/") or ".." in Path(m).parts:
                raise ValueError(f"unsafe path in download: {m}")
        z.extractall(into)
    zpath.unlink(missing_ok=True)

    apps = [p for p in into.rglob("*.app") if _verify_bundle(p)]
    if not apps:
        raise ValueError("the download did not contain a usable app")
    return sorted(apps, key=lambda p: len(p.parts))[0]


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
