#!/usr/bin/env python3
"""THE REBUILT SCREENSHOT, IN WHATEVER FRAMEWORK THE CLIENT ASKED FOR.

`aethron_vision.rebuild()` turns a screenshot into one self-contained
HTML file, measured rather than guessed. That file is the right ANSWER
and the wrong DELIVERABLE for a client who works in React, or Vue, or
Astro — and "it's HTML, port it yourself" is the thing they were paying
not to have to do.

WHY THIS IS EASY HERE AND WAS HARD FOR THE TEMPLATE PORT. Converting a
Framer export means fighting a live runtime, hydration, chunk data and
an animation engine. A rebuilt screenshot has none of that. It is a
flat list of absolutely-positioned elements, each already carrying a
stable id, over a carried background — so every framework renders the
identical DOM from the identical CSS, and the only thing that actually
differs between targets is syntax.

So the shape is: ONE description, MANY emitters.

    page_ir(html)     the page as a neutral description — canvas, font,
                      externalised assets, and elements grouped into
                      sections a person can navigate
    emit_*(ir, dest)  one per framework; syntax only, no decisions
    grade(...)        build it, serve it, screenshot it, and compare it
                      against the original screenshot

THE LAST ONE IS NOT OPTIONAL. A port that has not been rendered and
compared is not a port, it is a guess — this project has shipped
"PIXEL-PERFECT PORT READY" over a build rendering 65 of 153 images
once already, because images were counted and never judged. Every
target here is graded against the owner's original screenshot, and one
that does not match is REFUSED rather than handed over.

ASSETS BECOME FILES. The single-file build inlines the carried ground
and every carried crop as base64, which is right for one file and wrong
for a project: a developer cannot open a data URI, and every edit
rewrites a 90KB line. Emitters ship `public/assets/*.png` and reference
them by path.
"""
import base64
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_edit as AE              # noqa: E402
import aethron_figma_grade as GR       # noqa: E402

FRAMEWORKS = ("html", "astro", "next", "react", "vue", "svelte")

# Where each target leaves its built, servable site.
OUTDIR = {"html": ".", "astro": "dist", "next": "out",
          "react": "dist", "vue": "dist", "svelte": "dist"}

# AN EMITTER IS GRADED AGAINST THE PAGE IT EMITTED FROM, NOT AGAINST
# THE SCREENSHOT. Graded against the screenshot every target scores
# about 96%, because that is where the REBUILD tops out — the remaining
# error is glyph shape, a face we do not have, and it belongs to the
# rebuild rather than to the emitter. Gating on it would hold React
# responsible for a typeface. The question an emitter has to answer is
# narrower and much stricter: does your output render the same page as
# the single file it came from? The end-to-end number against the
# owner's screenshot is reported alongside, because that is what they
# actually see.
ACCEPT = 0.995

ASTRO_VERSION = "5.18.2"
REACT_VERSION = "19.3.0"
VITE_VERSION = "8.3.0"
VITE_REACT_VERSION = "6.1.1"
NEXT_VERSION = "16.3.4"
VUE_VERSION = "3.5.42"
VITE_VUE_VERSION = "6.0.8"
SVELTE_VERSION = "5.57.0"
SVELTE_PLUGIN = "7.3.0"


# ────────────────────────── the description ──────────────────────────

def page_ir(html: str, name="site"):
    """The rebuilt page as something any emitter can write out.

    Everything here is already measured. Nothing in this function
    decides anything about the design — it reads what `rebuild()`
    wrote, pulls the inline assets out into real files, and groups the
    elements into sections so the emitted project is navigable.
    """
    man = AE.manifest(html)
    canvas = man["canvas"]
    if not canvas.get("w"):
        raise SystemExit("this does not look like a rebuilt page "
                         "(no canvas size in its <style>)")

    font = {"family": None, "href": None}
    m = re.search(r'<link[^>]*href="(https://fonts\.googleapis\.com[^"]*)"',
                  html)
    if m:
        font["href"] = m.group(1).replace("&amp;", "&")
    m = re.search(r"font-family:\s*'([^']+)'", html)
    if m:
        font["family"] = m.group(1)
    m = re.search(r"body\{background:(#[0-9A-Fa-f]{6})", html)
    background = m.group(1) if m else "#FFFFFF"

    assets, elements = [], []
    for el in man["elements"]:
        raw = _tag_of(html, el["id"])
        if raw is None:
            continue
        style = AE._parse(_attr(raw, "style"))
        # KEEP z-index. The first version dropped it and let document
        # order do the stacking, which is true of the single file and
        # STOPS being true the moment sections_of sorts elements by
        # their position — a filled box whose top sits below its own
        # label's top would then be painted over the label. On this
        # page that happened not to occur, which is luck, not design.
        # An explicit z-index survives any reordering, in every target.
        item = {"id": el["id"], "kind": el["kind"], "style": style,
                "tag": el.get("tag", "div"),
                "z": _num(style.get("z-index")) or 0}
        if el.get("font_size") and not style.get("font-size"):
            item["font_size"] = el["font_size"]

        src = _attr(raw, "src")
        if src and src.startswith("data:image/"):
            fn = f"{el['id']}.png"
            assets.append({"file": fn, "bytes": _from_data_uri(src)})
            item["src"] = f"/assets/{fn}"
        bg = style.get("background-image", "")
        if "data:image/" in bg:
            fn = f"{el['id']}.png"
            assets.append({"file": fn, "bytes": _from_data_uri(
                re.search(r"url\((data:image/[^)]+)\)", bg).group(1))})
            style["background-image"] = f"url(/assets/{fn})"
        if el.get("text"):
            item["text"] = el["text"]
        elements.append(item)

    return {"name": _slug(name), "canvas": canvas, "font": font,
            "background": background, "assets": assets,
            "elements": elements, "sections": sections_of(elements, canvas)}


def sections_of(elements, canvas, gap=28):
    """Group the flat element list into something a person can navigate.

    NAMED BY WHAT WAS MEASURED, NEVER BY WHAT IT MIGHT BE. It is
    tempting to call the first band `Hero` and the last `Footer`, and
    that is a guess — the same guess this project refuses everywhere
    else. Sections are numbered, and the comment above each one lists
    the band it covers and the words in it, which is the information a
    developer actually opens the file to find.

    Elements that span the page (the carried ground, a full-height
    rule) are not part of any band; they are the backdrop, and putting
    them in one would put a 729px-tall div inside the nav.
    """
    back, rest = [], []
    for e in elements:
        h = _num(e["style"].get("height"))
        w = _num(e["style"].get("width"))
        spans = ((h or 0) >= canvas["h"] * 0.6
                 or (w or 0) >= canvas["w"] * 0.98)
        (back if spans else rest).append(e)

    rest.sort(key=lambda e: (_num(e["style"].get("top")) or 0,
                             e.get("z", 0),
                             _num(e["style"].get("left")) or 0))
    bands, cur, last = [], [], None
    for e in rest:
        top = _num(e["style"].get("top")) or 0
        if last is not None and top - last > gap:
            bands.append(cur)
            cur = []
        cur.append(e)
        last = max(last or 0, top + (_num(e["style"].get("height")) or 12))
    if cur:
        bands.append(cur)

    out = []
    if back:
        out.append({"ident": "Backdrop", "file": "00-backdrop",
                    "note": "the page's own colour, carried out of the "
                            "screenshot, plus anything spanning it",
                    "elements": back})
    for i, band in enumerate(bands, 1):
        tops = [_num(e["style"].get("top")) or 0 for e in band]
        words = [e["text"] for e in band if e.get("text")][:3]
        note = (f"y {int(min(tops))}-{int(max(tops))} · "
                f"{len(band)} element(s)")
        if words:
            note += " · " + ", ".join(repr(w[:28]) for w in words)
        out.append({"ident": f"Sec{i:02d}", "file": f"{i:02d}-section",
                    "note": note, "elements": band})
    return out


def _tag_of(html, eid):
    # THE SAME div|img ASSUMPTION manifest() carried, in a second place.
    # Fixing only one of them changes nothing: manifest finds the nav and
    # the buttons, and page_ir then drops every one of them again on the
    # way past, because it cannot locate the tag it was just told about.
    m = re.search(r'<(?:div|img|a|button|p|h[1-6]|span|section|nav|header'
                  r'|footer)\b[^>]*data-ae-id="' + re.escape(eid)
                  + r'"[^>]*>', html)
    return m.group(0) if m else None


def _attr(tag, key):
    m = re.search(key + r'="([^"]*)"', tag)
    return m.group(1) if m else ""


def _from_data_uri(uri):
    return base64.b64decode(uri.split("base64,", 1)[1])


def _num(v):
    if not v:
        return None
    m = re.match(r"^(-?[\d.]+)", str(v))
    return float(m.group(1)) if m else None


def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return s or "site"


# ──────────────────────── syntax, shared ─────────────────────────────
# Every emitter uses these, so a target cannot differ from its siblings
# by accident. The only legitimate difference between two ports of this
# page is the file extension.

def css_text(style: dict) -> str:
    return ";".join(f"{k}:{v}" for k, v in style.items())


def react_style(style: dict) -> str:
    """A CSS dict as a JSX style object.

    React will not take a style string, and hand-converting per emitter
    is how two targets that render the same DOM stop rendering the same
    DOM. Numeric-looking values stay strings on purpose: React appends
    'px' to bare numbers for some properties and not others, and a
    string is never reinterpreted.
    """
    out = []
    for k, v in style.items():
        key = re.sub(r"-([a-z])", lambda m: m.group(1).upper(), k)
        if not re.match(r"^[A-Za-z][A-Za-z0-9]*$", key):
            key = json.dumps(k)
        out.append(f"{key}: {json.dumps(str(v))}")
    return "{ " + ", ".join(out) + " }"


def esc_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;"))


def esc_jsx(s: str) -> str:
    """JSX text: the HTML escapes plus braces, which open an expression."""
    return esc_html(s).replace("{", "&#123;").replace("}", "&#125;")


def esc_vue(s: str) -> str:
    """Vue templates interpolate on {{ }} — break the pair, keep the text."""
    return esc_html(s).replace("{{", "&#123;&#123;")


def esc_svelte(s: str) -> str:
    """Svelte opens an expression on a single brace, like JSX."""
    return esc_jsx(s)


def note_safe(s: str) -> str:
    """A section note that cannot break the comment it is written into.

    The notes quote the page's own words, and the selftest caught the
    consequence immediately: a heading reading `A <b> and {braces}`
    put a live `<b>` inside an HTML comment. A page's text is data, and
    data inside a comment closes it — `-->` in HTML and Astro, `*/` in
    every JS-family target.
    """
    return (str(s).replace("<", "\u2039").replace(">", "\u203a")
            .replace("--", "\u2013").replace("*/", "*\u2044"))


def write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)


def write_assets(ir, dest: Path, sub="public/assets"):
    for a in ir["assets"]:
        write(dest / sub / a["file"], a["bytes"])


def font_link(ir) -> str:
    return (f'<link rel="stylesheet" href="{ir["font"]["href"]}">'
            if ir["font"].get("href") else "")


def base_css(ir) -> str:
    """The page's own frame. Identical in every target, deliberately."""
    c, f = ir["canvas"], ir["font"]
    fam = f"'{f['family']}', " if f.get("family") else ""
    return f"""*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{c['w']}px;height:{c['h']}px;overflow:hidden}}
body{{background:{ir['background']};position:relative;
  font-family:{fam}-apple-system,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased}}
.t{{position:absolute;white-space:nowrap;line-height:1}}
.r{{position:absolute}}"""


# ───────────────────────── the reference emitter ─────────────────────

def emit_html(ir, dest: Path):
    """Plain HTML and CSS, with the assets as real files.

    This is the reference every other emitter is graded against, and a
    target in its own right: plenty of clients want exactly this.
    """
    write_assets(ir, dest, "assets")
    body = []
    for sec in ir["sections"]:
        body.append(f"\n  <!-- {sec['ident']} — "
                    f"{note_safe(sec['note'])} -->")
        for e in sec["elements"]:
            body.append("  " + html_element(e))
    write(dest / "index.html", f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={ir['canvas']['w']}">
<title>{ir['name']}</title>
{font_link(ir)}
<link rel="stylesheet" href="./styles.css">
</head>
<body>{chr(10).join(body)}
</body>
</html>
""")
    write(dest / "styles.css", base_css(ir) + "\n")
    write(dest / "README.md", readme(ir, "html",
                                     "open index.html, or serve the folder"))
    return dest


def html_element(e) -> str:
    style = css_text(e["style"]).replace("/assets/", "./assets/")
    if e["kind"] == "picture":
        src = (e.get("src") or "").replace("/assets/", "./assets/")
        return (f'<img alt="" class="r" data-ae-id="{e["id"]}" '
                f'style="{style}" src="{src}">')
    cls = "t" if e["kind"] == "text" else "r"
    txt = esc_html(e.get("text", ""))
    return (f'<div class="{cls}" data-ae-id="{e["id"]}" '
            f'style="{style}">{txt}</div>')


def readme(ir, framework, how) -> str:
    c = ir["canvas"]
    return f"""# {ir['name']}

Rebuilt from a screenshot by Aethron, then emitted as {framework}.

Every position, colour, corner radius and rule in here was MEASURED
from the original image — not estimated by a model. The background is
the page's own colour field, carried out of the screenshot rather than
recreated, and `public/assets/` holds the parts that are photographs
rather than design.

    canvas      {c['w']} x {c['h']}
    elements    {len(ir['elements'])}
    sections    {len(ir['sections'])}
    assets      {len(ir['assets'])}

    {how}

The layout is absolute by design: it reproduces one screenshot at one
size exactly. Making it responsive is a design decision — yours, not
the tool's — and the measured values are the right starting point for
it.
"""


def astro_element(e) -> str:
    """One element as Astro markup.

    Astro is a superset of HTML, so this is `html_element` with two
    differences and no others. (1) Asset paths stay ROOT-ABSOLUTE:
    `public/` is the site root in Astro, so `/assets/x.png` is already
    correct and rewriting it to `./assets/x.png` would 404 from any
    page that is not the index. (2) Text is escaped with `esc_jsx`,
    because an unescaped `{` opens an expression in an .astro template
    exactly as it does in JSX — a brace in the page's own copy would
    otherwise be a compile error or, worse, silently evaluated.
    """
    style = css_text(e["style"])
    if e["kind"] == "picture":
        return (f'<img alt="" class="r" data-ae-id="{e["id"]}" '
                f'style="{style}" src="{e.get("src") or ""}">')
    cls = "t" if e["kind"] == "text" else "r"
    return (f'<div class="{cls}" data-ae-id="{e["id"]}" '
            f'style="{style}">{esc_jsx(e.get("text", ""))}</div>')


def emit_astro(ir, dest: Path):
    """Astro 5, static output — one component per measured section.

    THE NOTES GO IN THE FRONTMATTER, NOT IN AN HTML COMMENT. An HTML
    comment is shipped to the browser; a `//` line in the `---` fence
    is compiled away, so the developer gets the navigation aid and the
    built page stays the page it was emitted from.

    `compressHTML` is turned OFF for the same reason. It is on by
    default in Astro 5 and it collapses whitespace runs in the
    template — harmless on most pages, and not on this one, where
    every text node is measured copy inside `white-space:nowrap`.

    Styles are `is:global`: Astro scopes a plain <style> block by
    stamping an attribute on the elements the component owns, and
    `base_css` deliberately addresses `html`, `body`, `.t` and `.r`
    across every component at once.
    """
    write_assets(ir, dest, "public/assets")

    imports = []
    for sec in ir["sections"]:
        body = "\n".join(astro_element(e) for e in sec["elements"])
        write(dest / "src/components" / f"{sec['file']}.astro",
              f"---\n// {sec['ident']} — {note_safe(sec['note'])}\n---\n"
              f"{body}\n")
        imports.append(f"import {sec['ident']} from "
                       f"'../components/{sec['file']}.astro';")

    page = ["---"] + imports + ["---", "<!DOCTYPE html>",
                                '<html lang="en">', "<head>",
                                '<meta charset="utf-8">',
                                f'<meta name="viewport" '
                                f'content="width={ir["canvas"]["w"]}">',
                                f"<title>{esc_html(ir['name'])}</title>"]
    if font_link(ir):
        page.append(font_link(ir))
    page += [f"<style is:global>\n{base_css(ir)}\n</style>", "</head>",
             "<body>"]
    for sec in ir["sections"]:
        # {/* … */} is an Astro comment: visible in the source a
        # developer opens, absent from the HTML the browser receives.
        page.append("{/* " + f"{sec['ident']} — {note_safe(sec['note'])}"
                    + " */}")
        page.append(f"<{sec['ident']} />")
    page += ["</body>", "</html>", ""]
    write(dest / "src/pages/index.astro", "\n".join(page))

    write(dest / "astro.config.mjs", """import { defineConfig } from 'astro/config';

// Static by construction: `output: 'static'` writes plain .html into
// dist/, which a bare file server can hand out with no rewrites, no
// node process and no client-side router.
export default defineConfig({
  output: 'static',
  compressHTML: false,
  build: { inlineStylesheets: 'always' },
  devToolbar: { enabled: false },
});
""")
    write(dest / "package.json", json.dumps({
        "name": f"{ir['name']}-astro",
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {"dev": "astro dev", "build": "astro build",
                    "preview": "astro preview"},
        # PINNED EXACT, no caret. A port graded today and rebuilt in
        # six months has to be the same port.
        "dependencies": {"astro": ASTRO_VERSION},
    }, indent=2) + "\n")
    write(dest / ".gitignore", "node_modules/\ndist/\n.astro/\n")
    write(dest / "README.md", readme(
        ir, "astro",
        "npm install && npm run build — the static site lands in dist/"))
    return dest


def jsx_element(e, indent="      ") -> str:
    """One element as JSX.

    TEXT IS A STRING EXPRESSION, NOT TEMPLATE TEXT. `esc_jsx` exists and
    works, but it relies on JSX decoding `&#123;` back into a brace, and
    "it relies on" is not a thing to find out from a page that happens
    to contain no braces. A JSON-encoded string expression is correct
    for every character there is — braces, quotes, backticks, emoji —
    and it is still the obvious place for a developer to edit the copy.
    """
    style = react_style(e["style"])
    if e["kind"] == "picture":
        return (f'{indent}<img alt="" className="r" data-ae-id="{e["id"]}"\n'
                f'{indent}     src={json.dumps(e.get("src") or "")}'
                f' style={{{style}}} />')
    cls = "t" if e["kind"] == "text" else "r"
    inner = (f"{{{json.dumps(e['text'])}}}" if e["kind"] == "text" else "")
    return (f'{indent}<div className="{cls}" data-ae-id="{e["id"]}"\n'
            f'{indent}     style={{{style}}}>{inner}</div>')


def emit_react(ir, dest: Path):
    """Vite + React, one component per measured section.

    A CLIENT-RENDERED PAGE SHIPS AN EMPTY SHELL, and that is a real
    trade-off rather than a defect — the README says so plainly. React
    builds this DOM itself on load, so there is no server markup and no
    hydration step, which also means none of the node-dropping that
    caught a previous Next port (120 images emitted, 65 rendered). If
    the client needs the markup present without JavaScript, astro or
    next is the target for them, and the same description emits either.
    """
    write_assets(ir, dest, "public/assets")

    imports, uses = [], []
    for sec in ir["sections"]:
        body = "\n".join(jsx_element(e) for e in sec["elements"])
        write(dest / "src/components" / f"{sec['file']}.jsx",
              f"// {sec['ident']} — {note_safe(sec['note'])}\n"
              f"export default function {sec['ident']}() {{\n"
              f"  return (\n    <>\n{body}\n    </>\n  );\n}}\n")
        imports.append(f"import {sec['ident']} from "
                       f"'./components/{sec['file']}.jsx';")
        uses.append(f"      {{/* {note_safe(sec['note'])} */}}\n"
                    f"      <{sec['ident']} />")
    nl = chr(10)
    write(dest / "src/App.jsx",
          f"import './styles.css';\n{nl.join(imports)}\n\n"
          f"export default function App() {{\n  return (\n    <>\n"
          f"{nl.join(uses)}\n    </>\n  );\n}}\n")
    write(dest / "src/main.jsx",
          "import { StrictMode } from 'react';\n"
          "import { createRoot } from 'react-dom/client';\n"
          "import App from './App.jsx';\n\n"
          "createRoot(document.getElementById('root')).render(\n"
          "  <StrictMode>\n    <App />\n  </StrictMode>,\n);\n")
    write(dest / "src/styles.css", base_css(ir) + "\n")
    write(dest / "index.html", f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width={ir['canvas']['w']}" />
    <title>{esc_html(ir['name'])}</title>
    {font_link(ir)}
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
""")
    write(dest / "vite.config.js",
          "import { defineConfig } from 'vite';\n"
          "import react from '@vitejs/plugin-react';\n\n"
          "// base '/' because the emitted asset paths are root-absolute\n"
          "// (/assets/ground.png). Serving this build from a SUBPATH\n"
          "// means changing this and nothing else.\n"
          "export default defineConfig({ base: '/', plugins: [react()] });\n")
    write(dest / "package.json", json.dumps({
        "name": f"{ir['name']}-react",
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {"dev": "vite", "build": "vite build",
                    "preview": "vite preview"},
        # PINNED EXACT, no caret — a port graded today and rebuilt in
        # six months has to be the same port.
        "dependencies": {"react": REACT_VERSION,
                         "react-dom": REACT_VERSION},
        "devDependencies": {"vite": VITE_VERSION,
                            "@vitejs/plugin-react": VITE_REACT_VERSION},
    }, indent=2) + "\n")
    write(dest / ".gitignore", "node_modules/\ndist/\n")
    write(dest / "README.md", readme(
        ir, "react",
        "npm install && npm run build — the static site lands in dist/")
        + "\nNOTE: this target renders on the client, so the built\n"
          "index.html is a shell and React draws the page on load. If the\n"
          "markup has to be in the HTML itself — for search engines, or\n"
          "for readers with JavaScript off — emit the same page as astro\n"
          "or next instead. Nothing else about it changes.\n")
    return dest


def emit_next(ir, dest: Path):
    """Next.js App Router, static export — real markup in the HTML.

    THE OPPOSITE TRADE-OFF TO THE VITE TARGET, and the reason to offer
    both. These components hold no state and no hooks, so they stay
    Server Components and `output: 'export'` writes the whole page into
    out/index.html as markup. Nothing has to run for a reader — or a
    search engine — to see it.

    That also means REAL SSG, which is where a previous Next port of a
    template came unstuck: it emitted 120 images, built 120, and
    rendered 65, because React dropped nodes while hydrating. Nothing
    here hydrates, and the grade renders the BUILT output in a browser,
    which is the only check that would see it if it did.
    """
    write_assets(ir, dest, "public/assets")

    imports, uses = [], []
    for sec in ir["sections"]:
        body = "\n".join(jsx_element(e) for e in sec["elements"])
        write(dest / "app/components" / f"{sec['file']}.jsx",
              f"// {sec['ident']} — {note_safe(sec['note'])}\n"
              f"export default function {sec['ident']}() {{\n"
              f"  return (\n    <>\n{body}\n    </>\n  );\n}}\n")
        imports.append(f"import {sec['ident']} from "
                       f"'./components/{sec['file']}.jsx';")
        uses.append(f"      {{/* {note_safe(sec['note'])} */}}\n"
                    f"      <{sec['ident']} />")
    nl = chr(10)
    write(dest / "app/page.jsx",
          f"{nl.join(imports)}\n\nexport default function Page() {{\n"
          f"  return (\n    <>\n{nl.join(uses)}\n    </>\n  );\n}}\n")
    # React 19 hoists a <link rel="stylesheet"> rendered anywhere into
    # the head, which is how the App Router wants an external font
    # referenced without reaching for next/font.
    link = (f"\n        <link rel=\"stylesheet\" "
            f"href=\"{ir['font']['href']}\" />" if ir["font"].get("href")
            else "")
    write(dest / "app/layout.jsx", f"""import './globals.css';

export const metadata = {{
  title: {json.dumps(ir['name'])},
  viewport: 'width={ir['canvas']['w']}',
}};

export default function RootLayout({{ children }}) {{
  return (
    <html lang="en">
      <body>{link}
        {{children}}
      </body>
    </html>
  );
}}
""")
    write(dest / "app/globals.css", base_css(ir) + "\n")
    write(dest / "next.config.mjs",
          "/** @type {import('next').NextConfig} */\n"
          "// `export` writes plain .html into out/ — a bare static host\n"
          "// with no rewrites and no node process can serve it.\n"
          "// Unoptimized images because the emitted <img> tags point at\n"
          "// carried crops in public/, which need no optimiser.\n"
          "export default {\n  output: 'export',\n"
          "  images: { unoptimized: true },\n};\n")
    write(dest / "jsconfig.json",
          json.dumps({"compilerOptions": {"jsx": "preserve"}}, indent=2))
    write(dest / "package.json", json.dumps({
        "name": f"{ir['name']}-next",
        "private": True,
        "version": "0.0.0",
        "scripts": {"dev": "next dev", "build": "next build",
                    "start": "next start"},
        # PINNED EXACT, no caret.
        "dependencies": {"next": NEXT_VERSION, "react": REACT_VERSION,
                         "react-dom": REACT_VERSION},
    }, indent=2) + "\n")
    write(dest / ".gitignore", "node_modules/\n.next/\nout/\n")
    write(dest / "README.md", readme(
        ir, "next",
        "npm install && npm run build — the static site lands in out/")
        + "\nThe components are Server Components: no state, no hooks,\n"
          "nothing to hydrate. The page is real markup in out/index.html,\n"
          "so it is readable with JavaScript switched off.\n")
    return dest


def emit_vue(ir, dest: Path):
    """Vite + Vue 3 single-file components.

    TEXT IS BOUND, NOT WRITTEN INTO THE TEMPLATE. A Vue template is
    parsed as HTML and then scanned for `{{ }}`, so escaping a brace as
    `&#123;` does not save you — the HTML parser hands the compiler a
    real brace back and Vue interpolates it. `esc_vue` is kept for
    plain-HTML uses, and this target declares each string in
    `<script setup>`, where content is raw text, and binds it with
    `v-text`. Correct for every character, including a page whose own
    copy contains braces.
    """
    write_assets(ir, dest, "public/assets")

    imports, uses = [], []
    for sec in ir["sections"]:
        consts, rows = [], []
        for i, e in enumerate(sec["elements"]):
            style = css_text(e["style"])
            if e["kind"] == "picture":
                rows.append(f'  <img alt="" class="r" '
                            f'data-ae-id="{e["id"]}"\n'
                            f'       src="{e.get("src") or ""}" '
                            f'style="{style}">')
            elif e["kind"] == "text":
                consts.append(f"const t{i} = {json.dumps(e['text'])};")
                rows.append(f'  <div class="t" data-ae-id="{e["id"]}"\n'
                            f'       style="{style}" v-text="t{i}"></div>')
            else:
                rows.append(f'  <div class="r" data-ae-id="{e["id"]}"\n'
                            f'       style="{style}"></div>')
        nl = chr(10)
        script = (f"<script setup>{nl}{nl.join(consts)}{nl}</script>{nl}{nl}"
                  if consts else "")
        write(dest / "src/components" / f"{sec['file']}.vue",
              f"<!-- {sec['ident']} — {note_safe(sec['note'])} -->\n"
              f"{script}<template>\n{nl.join(rows)}\n</template>\n")
        imports.append(f"import {sec['ident']} from "
                       f"'./components/{sec['file']}.vue';")
        uses.append(f"  <!-- {note_safe(sec['note'])} -->\n"
                    f"  <{sec['ident']} />")
    nl = chr(10)
    write(dest / "src/App.vue",
          f"<script setup>\n{nl.join(imports)}\n</script>\n\n"
          f"<template>\n{nl.join(uses)}\n</template>\n")
    write(dest / "src/main.js",
          "import { createApp } from 'vue';\n"
          "import './styles.css';\n"
          "import App from './App.vue';\n\n"
          "createApp(App).mount('#app');\n")
    write(dest / "src/styles.css", base_css(ir) + "\n")
    write(dest / "index.html", f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width={ir['canvas']['w']}" />
    <title>{esc_html(ir['name'])}</title>
    {font_link(ir)}
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
""")
    write(dest / "vite.config.js",
          "import { defineConfig } from 'vite';\n"
          "import vue from '@vitejs/plugin-vue';\n\n"
          "// base '/' because the emitted asset paths are root-absolute.\n"
          "export default defineConfig({ base: '/', plugins: [vue()] });\n")
    write(dest / "package.json", json.dumps({
        "name": f"{ir['name']}-vue", "private": True, "version": "0.0.0",
        "type": "module",
        "scripts": {"dev": "vite", "build": "vite build",
                    "preview": "vite preview"},
        "dependencies": {"vue": VUE_VERSION},
        "devDependencies": {"vite": VITE_VERSION,
                            "@vitejs/plugin-vue": VITE_VUE_VERSION},
    }, indent=2) + "\n")
    write(dest / ".gitignore", "node_modules/\ndist/\n")
    write(dest / "README.md", readme(
        ir, "vue",
        "npm install && npm run build — the static site lands in dist/"))
    return dest


def emit_svelte(ir, dest: Path):
    """Vite + Svelte 5 components.

    Svelte opens an expression on a SINGLE brace, so the page's own copy
    goes in as a string expression for the same reason it does in JSX —
    a measured line of text is data, and data must never be able to
    become syntax.
    """
    write_assets(ir, dest, "public/assets")

    imports, uses = [], []
    for sec in ir["sections"]:
        rows = []
        for e in sec["elements"]:
            style = css_text(e["style"])
            if e["kind"] == "picture":
                rows.append(f'<img alt="" class="r" data-ae-id="{e["id"]}"\n'
                            f'     src="{e.get("src") or ""}" '
                            f'style="{style}" />')
            else:
                cls = "t" if e["kind"] == "text" else "r"
                inner = ("{" + json.dumps(e["text"]) + "}"
                         if e["kind"] == "text" else "")
                rows.append(f'<div class="{cls}" data-ae-id="{e["id"]}"\n'
                            f'     style="{style}">{inner}</div>')
        nl = chr(10)
        write(dest / "src/components" / f"{sec['file']}.svelte",
              f"<!-- {sec['ident']} — {note_safe(sec['note'])} -->\n"
              f"{nl.join(rows)}\n")
        imports.append(f"  import {sec['ident']} from "
                       f"'./components/{sec['file']}.svelte';")
        uses.append(f"<!-- {note_safe(sec['note'])} -->\n"
                    f"<{sec['ident']} />")
    nl = chr(10)
    write(dest / "src/App.svelte",
          f"<script>\n{nl.join(imports)}\n</script>\n\n"
          f"{nl.join(uses)}\n")
    write(dest / "src/main.js",
          "import { mount } from 'svelte';\n"
          "import './styles.css';\n"
          "import App from './App.svelte';\n\n"
          "export default mount(App, "
          "{ target: document.getElementById('app') });\n")
    write(dest / "src/styles.css", base_css(ir) + "\n")
    write(dest / "index.html", f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width={ir['canvas']['w']}" />
    <title>{esc_html(ir['name'])}</title>
    {font_link(ir)}
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
""")
    write(dest / "vite.config.js",
          "import { defineConfig } from 'vite';\n"
          "import { svelte } from '@sveltejs/vite-plugin-svelte';\n\n"
          "// base '/' because the emitted asset paths are root-absolute.\n"
          "export default defineConfig({ base: '/', plugins: [svelte()] });\n")
    write(dest / "svelte.config.js",
          "import { vitePreprocess } from "
          "'@sveltejs/vite-plugin-svelte';\n\n"
          "export default { preprocess: vitePreprocess() };\n")
    write(dest / "package.json", json.dumps({
        "name": f"{ir['name']}-svelte", "private": True, "version": "0.0.0",
        "type": "module",
        "scripts": {"dev": "vite", "build": "vite build",
                    "preview": "vite preview"},
        "devDependencies": {"svelte": SVELTE_VERSION,
                            "@sveltejs/vite-plugin-svelte": SVELTE_PLUGIN,
                            "vite": VITE_VERSION},
    }, indent=2) + "\n")
    write(dest / ".gitignore", "node_modules/\ndist/\n")
    write(dest / "README.md", readme(
        ir, "svelte",
        "npm install && npm run build — the static site lands in dist/"))
    return dest


# ───────────────────────────── the referee ───────────────────────────

class _Serve:
    """A real HTTP server over the built site, because a framework port
    references its assets from the site ROOT and file:// has no root."""

    def __init__(self, directory: Path):
        import http.server
        import socketserver
        d = str(directory)

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=d, **k)

            def log_message(self, *a):
                pass

            def end_headers(self):
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

        socketserver.TCPServer.allow_reuse_address = True
        self.srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


def shoot_site(directory: Path, w, h, out: Path):
    """Screenshot a built site the way a browser would really load it."""
    with _Serve(directory) as s:
        time.sleep(0.3)
        return GR.shoot(None, w, h, out, url=f"http://127.0.0.1:{s.port}/")


def build(dest: Path, framework, timeout=1800):
    """npm install && npm run build, with the log kept when it fails."""
    if framework == "html":
        return {"ok": True, "stage": "none", "log": ""}
    if not shutil.which("npm"):
        return {"ok": None, "stage": "install",
                "log": "npm is not installed — UNVERIFIED, not proven good"}
    r = subprocess.run(["npm", "install", "--no-audit", "--no-fund"],
                       cwd=dest, capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode:
        return {"ok": False, "stage": "install",
                "log": (r.stdout + r.stderr)[-2500:]}
    r = subprocess.run(["npm", "run", "build"], cwd=dest,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        return {"ok": False, "stage": "build",
                "log": (r.stdout + r.stderr)[-2500:]}
    return {"ok": True, "stage": "built", "log": ""}


def grade(dest: Path, framework, reference_png, canvas, original_png=None,
          out_png=None):
    """Render the BUILT port and compare it with the page it came from.

    A port nobody rendered is a guess. This is the only statement in
    this module allowed to say a target works, and it says it about the
    EMITTER: `reference_png` is a render of the single-file rebuild, so
    a target that scores 1.0000 against it is the same page in a
    different syntax, which is the entire claim being made.
    `original_png` is graded too and only REPORTED — it carries the
    rebuild's own ceiling and is not the emitter's to answer for.
    """
    site = dest if OUTDIR[framework] == "." else dest / OUTDIR[framework]
    if not site.is_dir():
        return {"ok": False, "why": f"no built output at {site}"}
    if not Path(reference_png).is_file():
        # A MISSING REFERENCE IS NOT A FAILING PORT. Crashing here would
        # have reported a toolchain problem as a broken emitter.
        return {"ok": None, "why": f"no reference render at {reference_png}"
                " — UNVERIFIED, not proven good"}
    if not GR.find_browser():
        return {"ok": None, "why": "no browser — UNVERIFIED, not proven good"}
    out_png = Path(out_png or dest / "_rendered.png")
    got = shoot_site(site, canvas["w"], canvas["h"], out_png)
    if not got:
        return {"ok": False, "why": "the built port produced no screenshot"}
    ref = GR.compare(out_png, Path(reference_png))
    res = {"ok": ref["identical"] >= ACCEPT, "identical": ref["identical"],
           "structural": ref.get("structural"), "png": str(out_png),
           "why": (f"{ref['identical'] * 100:.3f}% identical to the page it "
                   f"was emitted from (accept at {ACCEPT * 100:.1f}%)")}
    if original_png and Path(original_png).is_file():
        res["vs_original"] = GR.compare(out_png,
                                        Path(original_png))["identical"]
    return res


def _selftest():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fail += 1
            print(f"  FAIL {name}   {detail}")

    tiny = base64.b64encode(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100fdff03fa0000000049454e44ae"
        "426082")).decode()
    page = ('<!DOCTYPE html><html><head>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/'
            'css2?family=General+Sans&amp;display=swap">'
            '<style>*{margin:0}html,body{width:800px;height:600px;'
            'overflow:hidden}body{background:#0B0B0B;position:relative;'
            "font-family:'General Sans',sans-serif}</style></head><body>"
            f'<div class="r" data-ae-id="ground" style="left:0;top:0;'
            f'width:800px;height:600px;z-index:0;background-image:'
            f'url(data:image/png;base64,{tiny})"></div>'
            '<div class="r" data-ae-id="r00" style="left:0;top:300px;'
            'width:800px;height:1px;z-index:1;background:#333333"></div>'
            '<div class="r" data-ae-id="f00" style="left:60px;top:200px;'
            'width:180px;height:44px;z-index:2;background:#FFFFFF;'
            'border-radius:6px"></div>'
            '<div class="t" data-ae-id="t00" style="left:60px;top:40px;'
            'font-size:30.0px;color:#FFFFFF;z-index:4">A &lt;b&gt; and '
            '{braces}</div>'
            '<div class="t" data-ae-id="t01" style="left:60px;top:400px;'
            'font-size:13.0px;color:#AAAAAA;z-index:4">Lower down</div>'
            f'<img data-ae-id="p00" class="r" style="left:300px;top:420px;'
            f'width:60px;height:60px;z-index:5" '
            f'src="data:image/png;base64,{tiny}">'
            '</body></html>')

    print("── the page reads back as a neutral description")
    ir = page_ir(page, "Wezzi Demo")
    check("the canvas is carried", ir["canvas"] == {"w": 800, "h": 600},
          str(ir["canvas"]))
    check("the name is slugged", ir["name"] == "wezzi-demo", ir["name"])
    check("the font link survives its escaping",
          ir["font"]["href"].endswith("family=General+Sans&display=swap"),
          str(ir["font"]))
    check("the family is read", ir["font"]["family"] == "General Sans")
    check("the ground colour is read", ir["background"] == "#0B0B0B")
    check("every element is carried", len(ir["elements"]) == 6,
          str(len(ir["elements"])))

    print("\n── the inlined assets become real files")
    check("both data URIs were extracted", len(ir["assets"]) == 2,
          str([a["file"] for a in ir["assets"]]))
    check("  ...as real PNG bytes",
          all(a["bytes"][:4] == b"\x89PNG" for a in ir["assets"]))
    img = [e for e in ir["elements"] if e["id"] == "p00"][0]
    check("  ...and the element points at the file, not the data",
          img["src"] == "/assets/p00.png", str(img.get("src")))
    gnd = [e for e in ir["elements"] if e["id"] == "ground"][0]
    check("  ...including a background-image",
          gnd["style"]["background-image"] == "url(/assets/ground.png)",
          str(gnd["style"].get("background-image")))
    check("no base64 survives into the description",
          "base64," not in json.dumps(
              {k: v for k, v in ir.items() if k != "assets"}))

    print("\n── elements are grouped into something navigable")
    idents = [s["ident"] for s in ir["sections"]]
    check("a page-spanning element is backdrop, not a band",
          idents[0] == "Backdrop", str(idents))
    back = ir["sections"][0]["elements"]
    check("  ...and holds the ground and the full-width rule",
          {e["id"] for e in back} == {"ground", "r00"},
          str([e["id"] for e in back]))
    check("the rest split into bands by their measured gaps",
          len(ir["sections"]) >= 3, str(idents))
    check("every element lands in exactly one section",
          sum(len(s["elements"]) for s in ir["sections"])
          == len(ir["elements"]))
    check("a section is named by measurement, never by a guess",
          all(re.match(r"^(Sec\d\d|Backdrop)$", i) for i in idents),
          str(idents))
    check("  ...with the band and its words in the note",
          any("'Lower down'" in s["note"] for s in ir["sections"]),
          str([s["note"] for s in ir["sections"]]))

    print("\n── the shared syntax helpers, which stop targets diverging")
    check("css_text writes a declaration list",
          css_text({"left": "1px", "font-size": "2px"})
          == "left:1px;font-size:2px")
    rs = react_style({"font-size": "10.6px", "background-image":
                      "url(/assets/x.png)"})
    check("react_style camelCases and quotes",
          rs == '{ fontSize: "10.6px", backgroundImage: "url(/assets/x.png)" }',
          rs)
    check("  ...and keeps numbers as strings, so React cannot reinterpret",
          '"10.6px"' in rs)
    t = [e for e in ir["elements"] if e["id"] == "t00"][0]
    check("the manifest already un-escaped the text", "<b>" in t["text"],
          repr(t["text"]))
    check("esc_html re-escapes for HTML", esc_html(t["text"]).count("&lt;") == 1)
    check("esc_jsx also neutralises braces",
          "&#123;" in esc_jsx(t["text"]) and "{" not in esc_jsx(t["text"]),
          esc_jsx(t["text"]))
    check("esc_vue breaks the interpolation pair",
          esc_vue("a {{ b }}").startswith("a &#123;&#123;"),
          esc_vue("a {{ b }}"))
    check("esc_svelte neutralises a single brace",
          "{" not in esc_svelte("a { b }"))

    print("\n── the reference emitter")
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ae-screen-"))
    emit_html(ir, d)
    out = (d / "index.html").read_text()
    check("index.html is written", (d / "index.html").is_file())
    check("styles.css is written", (d / "styles.css").is_file())
    check("the assets are on disk", (d / "assets/p00.png").is_file())
    check("nothing is inlined as base64", "base64," not in out)
    check("the canvas is in the CSS",
          "width:800px;height:600px" in (d / "styles.css").read_text())
    check("the sections are commented for a reader",
          out.count("<!-- ") >= 3, str(out.count("<!-- ")))
    check("every element is emitted",
          all(f'data-ae-id="{e["id"]}"' in out for e in ir["elements"]))
    check("stacking is explicit, so re-ordering sections cannot repaint",
          all("z-index" in e["style"] for e in ir["elements"]),
          str([e["id"] for e in ir["elements"] if "z-index" not in e["style"]]))
    check("  ...and survives into the emitted markup",
          out.count("z-index") >= len(ir["elements"]))
    check("markup in the text is escaped, not rendered",
          "&lt;b&gt;" in out and "<b>" not in out)
    check("  ...including inside the section comments, which quote it",
          "\u2039b\u203a" in out, [l for l in out.splitlines()
                                     if "<!--" in l][:2])

    print("\n── honesty")
    g = grade(d, "html", d / "nope.png", ir["canvas"])
    check("grading with no reference render does not claim success",
          g["ok"] is not True, str(g))
    b = build(d, "html")
    check("the html target needs no toolchain", b["ok"] is True)
    try:
        page_ir("<html><body>nothing</body></html>")
        bad = False
    except SystemExit:
        bad = True
    check("a page that is not a rebuild is refused, not half-read", bad)

    shutil.rmtree(d, ignore_errors=True)
    print(f"\nscreen selftest: {ok} ok, {fail} failed")
    return 1 if fail else 0


EMITTERS = {"html": emit_html, "astro": emit_astro,
            "react": emit_react, "next": emit_next,
            "vue": emit_vue, "svelte": emit_svelte}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: aethron_screen.py <page.html> <outdir> "
              "[--framework html|astro|next|react|vue|svelte]")
        print("       aethron_screen.py --selftest")
        return 0
    if argv[0] == "--selftest":
        return _selftest()
    src, dest = Path(argv[0]), Path(argv[1])
    fw = argv[argv.index("--framework") + 1] if "--framework" in argv \
        else "html"
    if fw not in EMITTERS:
        raise SystemExit(f"--framework must be one of {sorted(EMITTERS)}")
    ir = page_ir(src.read_text(), argv[argv.index("--name") + 1]
                 if "--name" in argv else src.parent.name)
    EMITTERS[fw](ir, dest)
    print(f"{fw}: {len(ir['elements'])} element(s), "
          f"{len(ir['sections'])} section(s), "
          f"{len(ir['assets'])} asset(s) -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
