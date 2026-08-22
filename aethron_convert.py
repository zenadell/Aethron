#!/usr/bin/env python3
"""Aethron Convert — the pixel-perfect port. No model, no runtime, no
dependency on the platform the template came from.

WHY THIS EXISTS, AND WHY IT IS NOT THE AI PORT: asking a model to
rewrite a template produces an approximation — measured on a real
Framer template: 75% of the copy and a design that merely resembles
the original. This does the opposite of rewriting. It CARRIES what
already exists, exactly as editor mode does, and that is why it can be
pixel-perfect.

WHAT THE EVIDENCE SAID (agero, a real Framer template):

    every <script> stripped  ->  6,296 chars of text, 152 images,
                                 26/26 headings, 99% identical

The design is entirely in the CSS Framer emits. The runtime contributes
nothing to layout, type, colour or spacing. It only does one visible
thing:

    49 elements sit at inline `opacity: 0`, waiting to be animated in.

Both ends of those animations are already in the DOM — the start is
written in the style attribute, the end is the CSS default. So the
entrance is recovered mechanically, not generated: strip the parked
initial state, hand it to a ~20-line IntersectionObserver that ships as
readable source in the user's own repo. No Framer runtime is carried,
and nothing is a black box.

    python3 aethron_convert.py <project> --framework astro|next|vite
"""
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORGE = ROOT / "forge.py"
FRAMEWORKS = ("astro", "next", "vite")

# The entrance runtime. Twenty lines the owner can read, delete or
# rewrite — the point of the whole exercise.
MOTION_TAG = '\n<script src="/aethron-motion.js" defer></script>\n'

MOTION_JS = """// Aethron entrance animations — recovered from the original
// template's own start states. Each [data-ae] element carries the style
// it began at; we hold it there, then release it when it scrolls into
// view. With JavaScript disabled the content is simply visible, which
// is strictly better than the original behaved.
(function () {
  var els = document.querySelectorAll('[data-ae]');
  if (!els.length) return;
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) return;
  els.forEach(function (el) { el.setAttribute('style',
    (el.getAttribute('style') || '') + ';' + el.dataset.ae); });
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      var el = e.target;
      el.style.transition = el.dataset.aeDur || 'opacity .6s ease, transform .6s cubic-bezier(.44,0,.56,1)';
      el.style.opacity = '';
      el.style.transform = '';
      io.unobserve(el);
    });
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.01 });
  els.forEach(function (el) { io.observe(el); });
})();
"""

# An animation's PARKED START, never the design.
#
# THE RULE THAT MATTERS: a transform alone is NOT an entrance. Framer
# uses `translate(-50%,-50%)` to centre things and `scale()` for design
# effects — strip those and the layout moves, which is the one thing
# this converter exists to prevent. An entrance is only recognised when
# the element is also HIDDEN (opacity 0), because that is what an
# element waiting to animate in looks like. Caught by counting: a first
# cut reported 197 "entrances" on a page that has 49 hidden elements.
OPACITY = re.compile(r"(?i)(?:^|;)\s*opacity\s*:\s*([0-9]*\.?[0-9]+)\s*(?=;|$)")
HIDDEN_MAX = 0.05        # above this it is a design choice, not a parked state


def is_hidden(style: str) -> bool:
    """Parked mid-entrance, or deliberately faded?

    Framer parks elements at opacity 0 AND at 0.001 — and it also uses
    0.06 and 0.18 as real design values. A regex that pattern-matches
    digits cannot tell 0.001 from 0.18 (both are "0.something"), so the
    value is parsed and compared. Forcing a deliberately faded element
    to full opacity is exactly the kind of silent wrongness a content
    score would never catch."""
    m = OPACITY.search(style)
    try:
        return bool(m) and float(m.group(1)) <= HIDDEN_MAX
    except ValueError:
        return False
START_STATE = re.compile(
    r"(?i)(?:^|;)\s*(opacity\s*:\s*0(?:\.\d+)?|"
    r"transform\s*:\s*[^;]*(?:translate|scale|rotate|perspective)[^;]*)\s*(?=;|$)")


def strip_scripts(dom: str) -> tuple:
    """Remove every script. The template's runtime does not come with
    us — that is the entire promise of this converter."""
    n = len(re.findall(r"(?i)<script\b", dom))
    dom = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", "", dom)
    dom = re.sub(r"(?is)<script\b[^>]*/?>", "", dom)
    return dom, n


def recover_entrances(dom: str) -> tuple:
    """Move parked start states out of the inline style and onto
    data-ae, so the element renders at its FINAL appearance and the
    entrance can be replayed by our own observer."""
    count = [0]

    def fix(m):
        tag, style = m.group(0), m.group(1)
        # only a hidden element is mid-entrance; everything else keeps
        # its inline style exactly as the template wrote it
        if not is_hidden(style) and "data-framer-appear-id" not in tag:
            return tag
        starts = [s.strip() for s in START_STATE.findall(style)]
        if not starts:
            return tag
        rest = START_STATE.sub("", style).strip(" ;")
        count[0] += 1
        keep = f' style="{rest}"' if rest else ""
        tag = re.sub(r'\s*style="[^"]*"', keep, tag, count=1)
        # the attribute belongs INSIDE the tag; appending after the
        # closing bracket turns it into page text
        attr = ' data-ae="' + ";".join(starts).replace('"', "&quot;") + '"'
        close = "/>" if tag.rstrip().endswith("/>") else ">"
        return tag.rstrip()[:-len(close)].rstrip() + attr + close

    dom = re.sub(r'(?i)<[a-z][^>]*\sstyle="([^"]*)"[^>]*>', fix, dom)
    return dom, count[0]


# Links that point back at the platform the template came from. The
# rendered DOM carries them (editor mode only hides them with CSS), and
# a site that is "completely yours" cannot ship a marketplace link to
# the shop it was bought from.
PLATFORM_LINKS = re.compile(
    r"(?i)https?://(?:www\.)?(?:framer\.com|framer\.website|webflow\.com|"
    r"buy\.polar\.sh|[a-z0-9-]+\.lemonsqueezy\.com|gumroad\.com)[^\"\']*")


def strip_platform(dom: str) -> tuple:
    """Dead-end every link back to the platform, and drop the preloads
    for a runtime that no longer exists. 30 modulepreload tags were
    making the browser fetch 5.5MB of chunks that nothing runs."""
    n_pre = len(re.findall(
        r'(?is)<link[^>]*rel="(?:modulepreload|prefetch)"[^>]*>', dom))
    dom = re.sub(r'(?is)<link[^>]*rel="(?:modulepreload|prefetch)"[^>]*>',
                 "", dom)
    n_link = len(set(PLATFORM_LINKS.findall(dom)))
    dom = PLATFORM_LINKS.sub("#", dom)
    return dom, n_pre, n_link


def split_document(dom: str) -> tuple:
    head = re.search(r"(?is)<head\b[^>]*>(.*?)</head\s*>", dom)
    body = re.search(r"(?is)<body\b[^>]*>(.*?)</body\s*>", dom)
    battrs = re.search(r"(?is)<body\b([^>]*)>", dom)
    lang = re.search(r'(?is)<html\b[^>]*\blang="([^"]*)"', dom)
    return (head.group(1) if head else "",
            body.group(1) if body else dom,
            (battrs.group(1) if battrs else "").strip(),
            lang.group(1) if lang else "en")


def route_of(page: str) -> str:
    return "index" if page == "index.html" else page[:-5] if \
        page.endswith(".html") else page


# ─────────────────────────── emitters ────────────────────────────────

def emit_astro(dest: Path, pages: dict, name: str):
    """Astro is a superset of HTML, and `set:html` writes markup out
    verbatim — so the built page is byte-for-byte what we carried in."""
    _w(dest / "package.json", json.dumps({
        "name": name, "private": True, "version": "0.1.0", "type": "module",
        "scripts": {"dev": "astro dev", "build": "astro build",
                    "preview": "astro preview"},
        "dependencies": {"astro": "5.14.1"}}, indent=2))
    _w(dest / "astro.config.mjs",
       "import { defineConfig } from 'astro/config';\n"
       "// `file` format keeps /about.html style output, matching the\n"
       "// original site's URLs exactly.\n"
       "export default defineConfig({ build: { format: 'file' },\n"
       "  devToolbar: { enabled: false } });\n")
    _w(dest / "tsconfig.json",
       json.dumps({"extends": "astro/tsconfigs/strict"}, indent=2))
    for page, doc in pages.items():
        r = route_of(page)
        _w(dest / f"src/html/{r}.head.html", doc["head"])
        # the entrance runtime has to be REFERENCED, not merely shipped:
        # writing it to public/ and forgetting the tag left 181 recovered
        # entrances sitting inert in the build
        _w(dest / f"src/html/{r}.body.html", doc["body"] + MOTION_TAG)
        _w(dest / f"src/pages/{r}.astro", f"""---
// Carried verbatim from the original build. `?raw` hands Vite the exact
// bytes, so nothing is re-escaped or re-formatted on the way through.
import head from '../html/{r}.head.html?raw';
import body from '../html/{r}.body.html?raw';
---
<html lang="{doc['lang']}">
  <head set:html={{head}} />
  <body{(' ' + doc['battrs']) if doc['battrs'] else ''} set:html={{body}} />
</html>
""")
    _w(dest / "public/aethron-motion.js", MOTION_JS)


def emit_next(dest: Path, pages: dict, name: str):
    _w(dest / "package.json", json.dumps({
        "name": name, "private": True, "version": "0.1.0",
        "scripts": {"dev": "next dev", "build": "next build",
                    "start": "next start"},
        "dependencies": {"next": "15.5.4", "react": "19.1.1",
                         "react-dom": "19.1.1"},
        "devDependencies": {"typescript": "5.9.2",
                            "@types/react": "19.1.9",
                            "@types/react-dom": "19.1.7",
                            "@types/node": "22.15.3"}}, indent=2))
    _w(dest / "next.config.mjs",
       "/** @type {import('next').NextConfig} */\n"
       "export default { output: 'export', images: { unoptimized: true },\n"
       "  trailingSlash: true };\n")
    _w(dest / "tsconfig.json", json.dumps({
        "compilerOptions": {"target": "ES2022",
                            "lib": ["dom", "dom.iterable", "esnext"],
                            "allowJs": True, "skipLibCheck": True,
                            "strict": True, "noEmit": True,
                            "esModuleInterop": True, "module": "esnext",
                            "moduleResolution": "bundler",
                            "resolveJsonModule": True,
                            "isolatedModules": True, "jsx": "preserve",
                            "incremental": True,
                            "plugins": [{"name": "next"}]},
        "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx",
                    ".next/types/**/*.ts"],
        "exclude": ["node_modules"]}, indent=2))
    first = next(iter(pages.values()))
    _w(dest / "app/layout.tsx", """import Head from './head-tags';

export default function RootLayout(
  { children }: { children: React.ReactNode }) {
  return (
    <html lang="%s">
      <head><Head /></head>
      <body>
        {children}
        <script src="/aethron-motion.js" defer />
      </body>
    </html>
  );
}
""" % first["lang"])
    # <head> content is carried as real React elements: React refuses
    # dangerouslySetInnerHTML on <head>, and hoisting <style>/<link>
    # keeps Next's own head management working.
    _w(dest / "app/head-tags.tsx",
       "// Styles and links carried from the original document head.\n"
       "import raw from './head-data';\n\n"
       "export default function Head() {\n"
       "  return <>{raw.map((t, i) => t.style\n"
       "    ? <style key={i} dangerouslySetInnerHTML={{ __html: t.style }} />\n"
       "    : <link key={i} {...t.attrs} />)}</>;\n"
       "}\n")
    for page, doc in pages.items():
        r = route_of(page)
        _w(dest / (f"app/page.tsx" if r == "index" else f"app/{r}/page.tsx"),
           f"""import html from './body';

// Carried verbatim from the original build — the markup is not
// re-generated, so what renders is what the template always rendered.
export default function Page() {{
  return <div dangerouslySetInnerHTML={{{{ __html: html }}}} />;
}}
""")
        body_dir = dest / ("app" if r == "index" else f"app/{r}")
        _w(body_dir / "body.ts",
           "const html = " + json.dumps(doc["body"])
           + ";\nexport default html;\n")
    _w(dest / "app/head-data.ts",
       "const tags: any[] = " + json.dumps(_head_tags(first["head"]))
       + ";\nexport default tags;\n")
    _w(dest / "public/aethron-motion.js", MOTION_JS)


def _head_tags(head: str) -> list:
    """<style> and <link> out of the original head, as data React can
    render as real elements."""
    out = []
    for m in re.finditer(r"(?is)<style\b[^>]*>(.*?)</style\s*>", head):
        out.append({"style": m.group(1)})
    for m in re.finditer(r"(?is)<link\b([^>]*)>", head):
        attrs = dict(re.findall(r'([a-zA-Z-]+)="([^"]*)"', m.group(1)))
        ren = {"class": "className", "crossorigin": "crossOrigin",
               "referrerpolicy": "referrerPolicy"}
        out.append({"attrs": {ren.get(k, k): v for k, v in attrs.items()}})
    return out


def emit_vite(dest: Path, pages: dict, name: str):
    _w(dest / "package.json", json.dumps({
        "name": name, "private": True, "version": "0.1.0", "type": "module",
        "scripts": {"dev": "vite", "build": "vite build",
                    "preview": "vite preview"},
        "devDependencies": {"vite": "7.1.5"}}, indent=2))
    _w(dest / "vite.config.ts",
       "import { defineConfig } from 'vite';\n"
       "export default defineConfig({ appType: 'mpa' });\n")
    for page, doc in pages.items():
        r = route_of(page)
        doc = {**doc, "body": doc["body"] + MOTION_TAG}
        _w(dest / f"{r}.html",
           f"<!doctype html>\n<html lang=\"{doc['lang']}\">\n<head>"
           f"{doc['head']}</head>\n<body{(' ' + doc['battrs']) if doc['battrs'] else ''}>"
           f"{doc['body']}\n<script src=\"/aethron-motion.js\" defer></script>"
           f"</body>\n</html>\n")
    _w(dest / "public/aethron-motion.js", MOTION_JS)


EMITTERS = {"astro": emit_astro, "next": emit_next, "vite": emit_vite}
OUTDIR = {"astro": "dist", "next": "out", "vite": "dist"}


def _w(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ─────────────────────────── the pipeline ────────────────────────────

def convert(project, framework="astro", pages=None, on_event=None,
            install=True, build=True):
    project = Path(project).resolve()
    if framework not in FRAMEWORKS:
        raise SystemExit(f"--framework must be one of {FRAMEWORKS}")
    site = project / "site"
    if not site.is_dir():
        raise SystemExit("no site/ — migrate and build the project first")
    say = on_event or (lambda k, t: print(f"── {t}"))
    sys.path.insert(0, str(ROOT))
    import forge
    from http.server import ThreadingHTTPServer

    cfg = json.loads((project / "forge.json").read_text(encoding="utf-8"))
    want = pages or [p for p in cfg.get("pages", []) if (site / p).exists()]
    browser = forge._find_browser()
    if not browser:
        raise SystemExit("conversion needs a headless browser to read the "
                         "original as it truly renders")

    dest = project / f"convert-{framework}"
    if dest.exists():
        # keep node_modules — reinstalling a toolchain on every run is
        # minutes of nothing, and on a locked-down machine it can fail
        for child in dest.iterdir():
            if child.name in ("node_modules", "package-lock.json"):
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()

    H = forge._site_handler(site, cfg.get("platform", "static"), quiet=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    docs, total_ae = {}, 0
    try:
        for page in want:
            say("stage", f"reading {page} as the browser renders it…")
            got = forge._render_page(browser,
                                     f"http://127.0.0.1:{port}/{page}",
                                     budget_ms=9000)
            if not got["dom"]:
                say("error", f"{page} did not render — skipped")
                continue
            dom, n_scripts = strip_scripts(got["dom"])
            dom, n_pre, n_link = strip_platform(dom)
            dom, n_ae = recover_entrances(dom)
            total_ae += n_ae
            head, body, battrs, lang = split_document(dom)
            docs[page] = {"head": head, "body": body, "battrs": battrs,
                          "lang": lang}
            say("page", f"{page}: {n_scripts} script(s) + {n_pre} preload(s) "
                        f"dropped, {n_link} platform link(s) cut, "
                        f"{n_ae} entrance(s) recovered, "
                        f"{len(forge._visible_text(dom))} chars carried")
    finally:
        srv.shutdown()
    if not docs:
        raise SystemExit("nothing rendered — cannot convert")

    say("stage", f"emitting {framework}")
    EMITTERS[framework](dest, docs, cfg.get("name", "site"))
    # chunks/ and cms/ are the ORIGINAL RUNTIME's files. The content
    # they used to render is already baked into the carried HTML, so
    # copying them ships 5.5MB of dead weight from a platform this port
    # no longer depends on.
    shutil.copytree(site / "assets", dest / "public/assets",
                    ignore=shutil.ignore_patterns("chunks", "cms", "*.mjs",
                                                  "*.framercms"))
    n_assets = sum(1 for _ in (dest / "public/assets").rglob("*") if _.is_file())
    say("stage", f"{n_assets} local asset(s) copied — no CDN, no platform")

    result = {"ok": True, "dir": str(dest), "pages": list(docs),
              "entrances": total_ae, "assets": n_assets}
    if not build:
        return result
    say("stage", "npm install…")
    r = subprocess.run(["npm", "install", "--no-audit", "--no-fund"],
                       cwd=dest, capture_output=True, text=True, timeout=1800)
    if r.returncode:
        say("stage", "retrying install without postinstall scripts…")
        r = subprocess.run(["npm", "install", "--ignore-scripts",
                            "--no-audit", "--no-fund"], cwd=dest,
                           capture_output=True, text=True, timeout=1800)
    if r.returncode and install:
        return {**result, "ok": False, "stage": "install",
                "log": (r.stdout + r.stderr)[-1500:]}
    say("stage", "building…")
    r = subprocess.run(["npm", "run", "build"], cwd=dest,
                       capture_output=True, text=True, timeout=1800)
    if r.returncode:
        return {**result, "ok": False, "stage": "build",
                "log": (r.stdout + r.stderr)[-2500:]}
    out = dest / OUTDIR[framework]
    say("stage", f"grading {out.name}/ against the original…")
    home_page = "index.html" if "index.html" in docs else want[0]
    g = subprocess.run([sys.executable, str(FORGE), "probe",
                        f"--page={home_page}", f"--against={out}"],
                       cwd=project, capture_output=True, text=True,
                       timeout=1800)
    verdict = [l for l in (g.stdout + g.stderr).splitlines()
               if l.startswith(("PASS ", "FAIL ", "       missing"))]
    say("referee", "\n".join(verdict) or (g.stdout + g.stderr)[-800:])
    return {**result, "ok": g.returncode == 0, "stage": "graded",
            "out": str(out), "verdict": "\n".join(verdict)}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    fw = "astro"
    if "--framework" in argv:
        fw = argv[argv.index("--framework") + 1]
    pages = None
    if "--pages" in argv:
        pages = argv[argv.index("--pages") + 1].split(",")
    res = convert(Path(argv[0]).expanduser(), fw, pages,
                  build="--no-build" not in argv)
    print()
    print(("PIXEL-PERFECT PORT READY: " if res.get("ok")
           else "NOT ACCEPTED: ") + res.get("dir", ""))
    if res.get("verdict"):
        print(res["verdict"])
    if res.get("log"):
        print(res["log"])
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
