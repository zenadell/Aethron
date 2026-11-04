#!/usr/bin/env python3
"""The link preview is the brand's.

A shared link shows the page's og:image. A migrated template still points
at the TEMPLATE's own picture: its marketplace preview, or its logo. So
after a build Aethron opens the migrated home page in a real browser
(real time, so the entrance animations have played), takes it at
1200x630 (the size link previews use) and ships that as the preview.

A picture the person chose (cfg["share_image"] without "share_auto") is
never replaced. The picture is made again only when the copy changed.
Publishing turns it into a full address (link previews need one).
"""
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
W, H = 1200, 630
FILE = "assets/share.png"


def _fingerprint(d: Path) -> str:
    h = hashlib.sha1()
    for n in ("copy_map.json", "project_plan.md"):
        p = d / n
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _blank(png: bytes) -> bool:
    """A page photographed before anything appeared is one flat colour."""
    import tempfile
    sys.path.insert(0, str(ROOT))
    from aethron_figma_grade import read_png
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
    try:
        w, h, px = read_png(Path(f.name))
    finally:
        Path(f.name).unlink(missing_ok=True)
    vals = []
    step = max(1, (w * h) // 4000)
    for i in range(0, w * h, step):
        r, g, b = px[i * 4], px[i * 4 + 1], px[i * 4 + 2]
        vals.append((r * 299 + g * 587 + b * 114) // 1000)
    if not vals:
        return True
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return var < 20


def shoot(site: Path, platform: str, wait=3.0) -> bytes | None:
    """The built home page, 1200x630, in a real browser in real time."""
    sys.path.insert(0, str(ROOT))
    import aethron_browser as AB
    if not (site / "index.html").exists():
        return None
    base = AB.serve_site(site, platform)
    br = AB.Browser(headful=False, width=W, height=H)
    try:
        br.start()
        br.page("Emulation.setDeviceMetricsOverride",
                {"width": W, "height": H, "deviceScaleFactor": 1,
                 "mobile": False})
        br.open(base)
        for extra in (wait, 3.0):           # a slow hero gets one more look
            time.sleep(extra)
            png = br.screenshot()
            if png and not _blank(png):
                return png
        return None
    finally:
        try:
            br.close()
        except Exception:
            pass


def ensure(d: Path, log=print, rebuild=None, force=False) -> str:
    """Make (or keep) the share image for project `d`. -> what happened.
    `rebuild()` runs a build so the pages carry the new meta tags."""
    sys.path.insert(0, str(ROOT))
    import forge
    d = Path(d)
    cfg = forge.read_cfg(d)
    if cfg.get("share_image") and not cfg.get("share_auto"):
        return "kept the preview picture you chose"
    fp = _fingerprint(d)
    if not force and cfg.get("share_auto") and cfg.get("share_from") == fp \
            and (d / FILE).exists():
        return "link preview is up to date"
    try:
        png = shoot(d / "site", cfg.get("platform", "framer"))
    except Exception as e:
        return f"could not take the link preview ({type(e).__name__})"
    if not png:
        return "could not take the link preview (the page stayed blank)"
    (d / "assets").mkdir(exist_ok=True)
    (d / FILE).write_bytes(png)
    cfg = forge.read_cfg(d)
    cfg.update(share_image="/" + FILE, share_auto=True, share_from=fp)
    forge.write_cfg(d, cfg)
    log("link preview: took the migrated home page at 1200x630")
    if rebuild:
        rebuild()
    return "made the link preview from the migrated home page"


def absolute(html: bytes, origin: str) -> bytes:
    """og:image / twitter:image that start with "/" get the site's address:
    link previews ignore a relative picture."""
    import re
    origin = origin.rstrip("/")
    if not origin:
        return html
    pat = re.compile(rb'(<meta\b[^>]*(?:property|name)=["\'](?:og:image|'
                     rb'twitter:image)["\'][^>]*content=["\'])(/[^"\']*)',
                     re.I)
    return pat.sub(lambda m: m.group(1) + origin.encode() + m.group(2), html)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--selftest":
        print(ensure(Path(sys.argv[1]), force="--force" in sys.argv))
