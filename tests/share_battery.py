"""The link preview shows the migrated site (aethron_share).

A real export is built, the share image is taken in a real browser, and:
  * the picture exists, is 1200x630 and is not one flat colour;
  * every page carries ONE og:image / twitter:image pointing at it, and
    the template's own preview tags are gone;
  * nothing changed -> it is not taken again; a picture the person chose
    is never replaced;
  * publishing turns the address into a full one on the site.
No browser -> SKIPPED, never PASS.
"""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS = FAIL = 0


def ck(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))


def main():
    import forge
    import aethron_share as SH
    print("A. a full address at publish (no browser needed)")
    h = b'<head><meta property="og:image" content="/assets/share.png"><meta name="twitter:image" content="/assets/share.png"></head>'
    out = SH.absolute(h, "https://jarin.aethron.site/")
    ck("og:image becomes a full address", b'content="https://jarin.aethron.site/assets/share.png"' in out, out)
    ck("so does twitter:image", out.count(b"https://jarin.aethron.site/assets/share.png") == 2)
    ck("an address that is already full is left alone",
       SH.absolute(b'<meta property="og:image" content="https://x.com/a.png">', "https://y") ==
       b'<meta property="og:image" content="https://x.com/a.png">')
    t = forge._set_share_image('<head><meta property="og:image" content="https://framerusercontent.com/x.png">'
                               '<meta property="og:image:width" content="2400"></head>',
                               {"share_image": "/assets/share.png"})
    ck("the template's preview tags are replaced, not added to",
       "framerusercontent" not in t and t.count('property="og:image"') == 1
       and 'content="2400"' not in t, t)
    ck("no share image set -> the page is untouched",
       forge._set_share_image("<head></head>", {}) == "<head></head>")

    if not forge._find_browser():
        print("SKIPPED — no browser on this machine; the picture was NOT checked")
        print(f"\nshare battery: {PASS} passed, {FAIL} failed (browser part SKIPPED)")
        return 1 if FAIL else 0
    print("B. a real build, a real browser")
    tmp = Path(tempfile.mkdtemp(prefix="aeshare-"))
    os.environ.update(AETHRON_HOME=str(tmp / "home"), AETHRON_REPORTS="off", AETHRON_RUNLOG="off")
    (tmp / "home" / "projects").mkdir(parents=True)
    src = tmp / "export"; src.mkdir()
    shutil.copy(ROOT / "tests/pages/site.html", src / "index.html")
    if (ROOT / "tests/pages/assets").is_dir():
        shutil.copytree(ROOT / "tests/pages/assets", src / "assets")
    fg = [sys.executable, str(ROOT / "forge.py")]
    subprocess.run(fg + ["init", str(src / "index.html"), "demo"], cwd=tmp / "home/projects",
                   capture_output=True, text=True, timeout=300)
    d = next((p for p in (tmp / "home/projects").iterdir() if (p / "forge.json").exists()), None)
    ck("the project was made", d is not None)
    if not d:
        return 1
    for c in ("inventory", "build"):
        subprocess.run(fg + [c], cwd=d, capture_output=True, text=True, timeout=600)
    builds = []

    def rebuild():
        builds.append(1)
        subprocess.run(fg + ["build"], cwd=d, capture_output=True, text=True, timeout=600)
    what = SH.ensure(d, log=lambda t: None, rebuild=rebuild)
    png = d / SH.FILE
    ck("the preview was taken", png.exists(), what)
    if png.exists():
        from aethron_figma_grade import read_png
        w, hh, _ = read_png(png)
        ck("at 1200x630", (w, hh) == (1200, 630), f"{w}x{hh}")
        ck("and it is not a blank page", not SH._blank(png.read_bytes()))
    page = (d / "site/index.html").read_text(errors="ignore")
    ck("the built page points its link preview at it",
       re.search(r'property="og:image" content="/assets/share\.png"', page) is not None)
    ck("the picture ships with the site", (d / "site/assets/share.png").exists())
    ck("nothing changed -> not taken again",
       "up to date" in SH.ensure(d, log=lambda t: None, rebuild=rebuild) and len(builds) == 1)
    cfg = forge.read_cfg(d)
    cfg.update(share_image="/assets/mine.png", share_auto=False)
    forge.write_cfg(d, cfg)
    ck("a picture the person chose is kept", "kept" in SH.ensure(d, rebuild=rebuild, force=True))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nshare battery: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
