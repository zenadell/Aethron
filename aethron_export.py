#!/usr/bin/env python3
"""Aethron Export — take a migrated template to the framework the owner
actually wants to build in, and REFUSE to hand over a port that isn't
the same site.

The honest architecture, after trying to imagine a transpiler: there is
no mechanical HTML→JSX rewrite that survives a real Framer or Webflow
template. The markup is machine-generated, the runtime re-renders half
of it, and the interesting parts (a rotator, a marquee, a hover
variant) exist only as behaviour. So the port is an AGENT task — and
the thing that makes an agent trustworthy here is that it is graded by
a machine:

    site/  ──rendered DOM──> agent writes the app ──> npm build
                                     ▲                    │
                                     └── compiler errors ─┤
                                                          ▼
                                         forge probe --against=<out>
                                     (same text? same headings? same
                                      images? — judged in a browser)

Aethron compiles and judges; the agent only writes files. Same rule as
everywhere else: the agent proposes, deterministic checks dispose.

    python3 aethron_export.py <project> --framework next [--rounds 3]
"""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORGE = ROOT / "forge.py"
FRAMEWORKS = ("next", "astro", "vite")


# ────────────────────────── scaffolds ────────────────────────────────
# Deterministic boilerplate, so agent rounds are spent on the SITE and
# not on remembering what a tsconfig looks like.

def scaffold(kind: str, dest: Path, name: str):
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "next":
        _write(dest / "package.json", json.dumps({
            "name": name, "private": True, "version": "0.1.0",
            "scripts": {"dev": "next dev", "build": "next build",
                        "start": "next start"},
            "dependencies": {"next": "15.5.4", "react": "19.1.1",
                             "react-dom": "19.1.1"},
            "devDependencies": {"typescript": "5.9.2",
                                "@types/react": "19.1.9",
                                "@types/react-dom": "19.1.7",
                                "@types/node": "22.15.3"}}, indent=2))
        _write(dest / "next.config.mjs",
               "/** @type {import('next').NextConfig} */\n"
               "const nextConfig = {\n"
               "  output: 'export',           // a real static build\n"
               "  images: { unoptimized: true },\n"
               "  trailingSlash: true,\n"
               "};\nexport default nextConfig;\n")
        _write(dest / "tsconfig.json", json.dumps({
            "compilerOptions": {
                "target": "ES2022", "lib": ["dom", "dom.iterable", "esnext"],
                "allowJs": True, "skipLibCheck": True, "strict": True,
                "noEmit": True, "esModuleInterop": True, "module": "esnext",
                "moduleResolution": "bundler", "resolveJsonModule": True,
                "isolatedModules": True, "jsx": "preserve",
                "incremental": True,
                "plugins": [{"name": "next"}],
                "paths": {"@/*": ["./*"]}},
            "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx",
                        ".next/types/**/*.ts"],
            "exclude": ["node_modules"]}, indent=2))
        _seed(dest / "app/layout.tsx",
               "import type { Metadata } from 'next';\n"
               "import './globals.css';\n\n"
               "export const metadata: Metadata = { title: '"
               + name + "' };\n\n"
               "export default function RootLayout("
               "{ children }: { children: React.ReactNode }) {\n"
               "  return (<html lang=\"en\"><body>{children}</body></html>);\n"
               "}\n")
        _seed(dest / "app/globals.css",
               "*,*::before,*::after{box-sizing:border-box}\n"
               "body{margin:0}\n")
        _seed(dest / "app/page.tsx",
               "export default function Home() {\n"
               "  return <main>replace me with the ported home page</main>;\n"
               "}\n")
        return {"out": "out", "build": ["run", "build"]}
    if kind == "astro":
        _write(dest / "package.json", json.dumps({
            "name": name, "private": True, "version": "0.1.0",
            "type": "module",
            "scripts": {"dev": "astro dev", "build": "astro build"},
            "dependencies": {"astro": "5.14.1"},
            "devDependencies": {"typescript": "5.9.2"}}, indent=2))
        _write(dest / "astro.config.mjs",
               "import { defineConfig } from 'astro/config';\n"
               "export default defineConfig({ build: { format: 'file' } });\n")
        _write(dest / "tsconfig.json",
               json.dumps({"extends": "astro/tsconfigs/strict"}, indent=2))
        _seed(dest / "src/pages/index.astro",
               "---\n---\n<html lang=\"en\"><head><meta charset=\"utf-8\" />"
               "<title>" + name + "</title></head>\n"
               "<body><main>replace me with the ported home page</main>"
               "</body></html>\n")
        return {"out": "dist", "build": ["run", "build"]}
    # vite + react + ts
    _write(dest / "package.json", json.dumps({
        "name": name, "private": True, "version": "0.1.0", "type": "module",
        "scripts": {"dev": "vite", "build": "tsc -b && vite build",
                    "preview": "vite preview"},
        "dependencies": {"react": "19.1.1", "react-dom": "19.1.1"},
        "devDependencies": {"vite": "7.1.5", "typescript": "5.9.2",
                            "@vitejs/plugin-react": "5.0.2",
                            "@types/react": "19.1.9",
                            "@types/react-dom": "19.1.7"}}, indent=2))
    _write(dest / "vite.config.ts",
           "import { defineConfig } from 'vite';\n"
           "import react from '@vitejs/plugin-react';\n"
           "export default defineConfig({ plugins: [react()], base: './' });\n")
    _write(dest / "tsconfig.json", json.dumps({
        "compilerOptions": {"target": "ES2022", "useDefineForClassFields": True,
                            "lib": ["ES2022", "DOM", "DOM.Iterable"],
                            "module": "ESNext", "skipLibCheck": True,
                            "moduleResolution": "bundler", "jsx": "react-jsx",
                            "strict": True, "noEmit": True,
                            "isolatedModules": True},
        "include": ["src"]}, indent=2))
    _seed(dest / "index.html",
           "<!doctype html><html lang=\"en\"><head><meta charset=\"UTF-8\" />"
           "<meta name=\"viewport\" content=\"width=device-width,"
           "initial-scale=1\" /><title>" + name + "</title></head>"
           "<body><div id=\"root\"></div>"
           "<script type=\"module\" src=\"/src/main.tsx\"></script>"
           "</body></html>\n")
    _seed(dest / "src/main.tsx",
           "import { StrictMode } from 'react';\n"
           "import { createRoot } from 'react-dom/client';\n"
           "import App from './App';\n"
           "createRoot(document.getElementById('root')!).render("
           "<StrictMode><App /></StrictMode>);\n")
    _seed(dest / "src/App.tsx",
           "export default function App() {\n"
           "  return <main>replace me with the ported home page</main>;\n"
           "}\n")
    return {"out": "dist", "build": ["run", "build"]}


def _write(p: Path, text: str, overwrite=True):
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and not overwrite:
        return
    p.write_text(text, encoding="utf-8")


def _seed(p: Path, text: str):
    """Scaffold a starting file — but NEVER over an existing one. Re-running
    the exporter used to overwrite the page the agent had already written,
    silently throwing away a round's work."""
    _write(p, text, overwrite=False)


# ─────────────────── the source of truth for the port ────────────────

# A 669 KB rendered DOM is not agent input — Read truncates it and the
# context is gone. What a port actually needs is the page's CONTENT in
# document order: headings, copy, images, links, buttons. So we distil
# one, and leave the full DOM beside it for targeted lookups.

class _Outline:
    """Markdown-ish transcript of a rendered page, in document order."""

    def __init__(self):
        from html.parser import HTMLParser

        class P(HTMLParser):
            def __init__(inner):
                super().__init__(convert_charrefs=True)
                inner.out = []
                inner.skip = 0
                inner.heading = 0
                inner.buf = []

            def flush(inner, prefix=""):
                text = re.sub(r"\s+", " ", "".join(inner.buf)).strip()
                inner.buf = []
                if text:
                    inner.out.append(prefix + text)

            def handle_starttag(inner, tag, attrs):
                a = dict(attrs)
                if tag in ("script", "style", "noscript", "svg"):
                    inner.skip += 1
                    return
                if tag in ("h1", "h2", "h3", "h4"):
                    inner.flush()
                    inner.heading = int(tag[1])
                elif tag in ("p", "div", "section", "li", "footer", "header",
                             "nav", "br", "tr"):
                    inner.flush()
                elif tag == "img":
                    inner.flush()
                    src = a.get("src", "")
                    if src and not src.startswith("data:"):
                        inner.out.append(f"![{a.get('alt', '')}]({src})")
                elif tag == "a":
                    inner.href = a.get("href", "")

            def handle_endtag(inner, tag):
                if tag in ("script", "style", "noscript", "svg"):
                    inner.skip = max(0, inner.skip - 1)
                    return
                if tag in ("h1", "h2", "h3", "h4"):
                    inner.flush("#" * inner.heading + " ")
                    inner.heading = 0
                elif tag in ("p", "div", "section", "li", "footer", "header",
                             "nav", "a", "button"):
                    inner.flush()

            def handle_data(inner, data):
                if not inner.skip:
                    inner.buf.append(data)

        self.parser = P()

    def render(self, dom: str) -> str:
        self.parser.feed(dom)
        self.parser.flush()
        lines, seen = [], set()
        for l in self.parser.out:
            if l in seen and not l.startswith("#"):
                continue          # nav/footer repeats add nothing
            seen.add(l)
            lines.append(l)
        return "\n".join(lines)


def outline_of(dom: str) -> str:
    return _Outline().render(dom)


def capture_source(project: Path, dest: Path, pages, budget=9000) -> list:
    """The RENDERED DOM of each page — what the reader actually sees,
    after the template's own runtime has done its work. Porting from
    this instead of from the export's markup is the whole trick: no
    hydration, no framework internals, just the finished page."""
    sys.path.insert(0, str(ROOT))
    import forge
    from http.server import ThreadingHTTPServer
    import threading
    site = project / "site"
    browser = forge._find_browser()
    if not browser:
        raise SystemExit("export needs a headless browser to read the "
                         "original (set AETHRON_BROWSER)")
    H = forge._site_handler(site, forge.read_cfg(project).get("platform",
                                                              "static"),
                            quiet=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    out = []
    try:
        for page in pages:
            got = forge._render_page(browser, f"http://127.0.0.1:{port}/{page}",
                                     budget_ms=budget)
            dom = got["dom"]
            if not dom:
                continue
            f = dest / "source" / (page.replace("/", "-") + ".rendered.html")
            _write(f, dom)
            o = dest / "source" / (page.replace("/", "-") + ".outline.md")
            _write(o, outline_of(dom))
            out.append({"page": page, "file": str(f.relative_to(dest)),
                        "outline": str(o.relative_to(dest)),
                        "outline_chars": len(o.read_text(encoding="utf-8")),
                        "chars": len(forge._visible_text(dom)),
                        "headings": forge._headings(dom)})
    finally:
        srv.shutdown()
    return out


def copy_assets(project: Path, dest: Path, public: str = "public"):
    src = project / "site" / "assets"
    if not src.is_dir():
        return 0
    target = dest / public / "assets"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src, target)
    return sum(1 for _ in target.rglob("*") if _.is_file())


PORT_PROMPT = """Port this site to {framework} (TypeScript).

WHAT YOU ARE PORTING FROM
`source/*.outline.md` is the page's CONTENT in document order —
headings, copy, images, links. READ THAT FIRST: it is the complete
list of what must appear in your port.
`source/*.rendered.html` beside it is the finished DOM (large — hundreds
of KB). Use grep/targeted reads on it when you need styling or structure
for a specific section, never a full read.
Every asset referenced under /assets/ is already in `{public}/assets/`,
so those paths work unchanged.

WHAT "DONE" MEANS — you are graded by a machine, not by your own opinion
- every heading in the original must exist in your port, with the same
  text (they are checked by exact text)
- the rendered text must be at least 90% identical, word for word: do
  not summarise, do not rewrite copy, do not drop sections
- images must still be there (same count, same files)
- `npm run build` must succeed with no TypeScript errors

HOW TO WORK
1. read a source file (they are large — read it in pieces if needed)
2. build the page out of components in {structure}
3. carry the CSS across: extract what the original page uses (inline
   styles, the <style> blocks in the rendered DOM) into real CSS files.
   Layout and typography matter — a wall of unstyled text fails.
4. keep going until every section of the original exists in the port

RULES
- TypeScript, no `any` unless unavoidable
- no placeholder or lorem text, ever — copy the real words
- you have NO shell. Aethron installs, builds and grades between rounds
  and hands you the errors — you only read and write files.
- DO NOT STOP AFTER READING. A turn that reads files and writes nothing
  is a wasted round: finish this turn with the page files actually
  written to disk, even if a later round has to refine them.

PAGES TO PORT
{pages}
"""

STRUCTURE = {"next": "app/ (App Router: app/page.tsx and components/)",
             "astro": "src/pages/ and src/components/",
             "vite": "src/ (App.tsx and src/components/)"}



# ─────────────────── bounded work, not one giant turn ────────────────
# The first live run asked for a whole page in one turn: the model spent
# seventeen minutes re-reading and grepping a 669KB DOM and produced one
# file. Batching fixes both halves of that — each turn has a small,
# checkable target, and Aethron (not the model) assembles the page, so
# no turn has to hold the whole thing in its head.

def split_outline(outline: str, per_batch=8) -> list:
    """Cut the page outline into batches at heading boundaries."""
    lines = outline.splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("#")] or [0]
    groups, cur = [], []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        cur.append("\n".join(lines[i:end]))
        if len(cur) >= per_batch:
            groups.append("\n".join(cur))
            cur = []
    if cur:
        groups.append("\n".join(cur))
    head = "\n".join(lines[:starts[0]])
    if head.strip() and groups:
        groups[0] = head + "\n" + groups[0]
    return groups


SECTION_PROMPT = """Write ONE file: `{path}`

It renders this slice of the page, in this order, with this exact copy:

{slice}

RULES
- {lang}
- the copy above is the real text: use it verbatim, never summarise or
  invent
- images: use the src exactly as given
- self-contained styling (inline style objects or a co-located CSS
  module); the design should resemble a modern marketing site — real
  spacing, hierarchy and colour, not a bare list
- export {export_line}
- write the file and stop. Do not read other files, do not run anything,
  do not explain.
"""

LANGS = {"next": "TypeScript React (a client-free server component)",
         "vite": "TypeScript React",
         "astro": "an Astro component (.astro)"}
EXPORTS = {"next": "it as the default export",
           "vite": "it as the default export",
           "astro": "nothing — .astro files need no export statement"}


def assemble(kind: str, dest: Path, count: int, title: str):
    """Aethron writes the page that imports the sections, in order. The
    model never has to hold the whole page — and cannot break the
    assembly by editing one section."""
    names = [f"Section{n:02d}" for n in range(1, count + 1)]
    if kind == "astro":
        imports = "\n".join(
            f"import {n} from '../components/{n}.astro';" for n in names)
        body = "\n".join(f"  <{n} />" for n in names)
        _write(dest / "src/pages/index.astro",
               f"---\n{imports}\n---\n<html lang=\"en\"><head>"
               f"<meta charset=\"utf-8\" />"
               f"<title>{title}</title></head>\n<body>\n{body}\n"
               f"</body></html>\n")
        return
    imports = "\n".join(
        f"import {n} from '../components/{n}';" for n in names)
    body = "\n".join(f"      <{n} />" for n in names)
    target = "app/page.tsx" if kind == "next" else "src/App.tsx"
    if kind == "vite":
        imports = imports.replace("../components/", "./components/")
    _write(dest / target,
           f"{imports}\n\nexport default function Page() {{\n"
           f"  return (\n    <main>\n{body}\n    </main>\n  );\n}}\n")

# ─────────────────────────── the loop ────────────────────────────────

def _stopped() -> bool:
    """Has the spend guard cut this run off?"""
    try:
        import aethron_brain as brain
        return bool(brain.spend().get("stopped"))
    except Exception:
        return False


def npm(args, cwd, timeout=1200):
    r = subprocess.run(["npm", *args], cwd=cwd, capture_output=True,
                       text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr)[-6000:]


def referee(project: Path, out_dir: Path, page="index.html"):
    """forge probe --against: does the port RENDER the same site?"""
    r = subprocess.run([sys.executable, str(FORGE), "probe",
                        f"--page={page}", f"--against={out_dir}"],
                       cwd=project, capture_output=True, text=True,
                       timeout=1800)
    out = r.stdout + r.stderr
    verdict = [l for l in out.splitlines()
               if l.startswith(("PASS ", "FAIL ", "       missing"))
               and ("identical" in l or "missing" in l or "rendered" in l)]
    return r.returncode == 0, "\n".join(verdict) or out[-1500:]


def export(project, framework="next", rounds=3, pages=None, on_event=None,
           cfg=None, home=None, install=True, live=False):
    project = Path(project).resolve()
    if cfg is None:                       # a test harness passes its own
        import aethron_brain as brain
        brain.require_live(live, f"porting {project.name} to {framework}")
    if framework not in FRAMEWORKS:
        raise SystemExit(f"--framework must be one of {FRAMEWORKS}")
    if not (project / "site").is_dir():
        raise SystemExit("no site/ — migrate and build the project first")
    say = on_event or (lambda k, t: print(f"── {t}"))
    cfgj = json.loads((project / "forge.json").read_text(encoding="utf-8"))
    pages = pages or ["index.html"]
    dest = project / f"export-{framework}"
    say("stage", f"scaffolding {framework} in {dest.name}/")
    meta = scaffold(framework, dest, cfgj.get("name", "site"))
    public = "public"
    n = copy_assets(project, dest, public)
    say("stage", f"{n} asset(s) copied into {public}/assets")
    say("stage", "capturing the original's rendered DOM…")
    src = capture_source(project, dest, pages)
    for s in src:
        say("source", f"{s['page']}: {s['chars']} chars, "
                      f"{len(s['headings'])} headings -> {s['file']}")
    if install:
        say("stage", "npm install (once)…")
        ok, log = npm(["install", "--no-audit", "--no-fund"], dest)
        if not ok:
            say("error", "npm install failed:\n" + log[-1500:])
            return {"ok": False, "stage": "install", "log": log}

    import aethron_code as code
    outline_file = dest / src[0]["outline"]
    batches = split_outline(outline_file.read_text(encoding="utf-8"))
    ext = ".astro" if framework == "astro" else ".tsx"
    say("stage", f"{len(batches)} section batch(es) to write")

    # ── 1. one bounded turn per batch. The agent may ONLY write: no
    #      reading, no grepping, no shell — that is what turned a
    #      seventeen-minute turn into a one-minute one.
    history, written = [], 0
    for i, slice_text in enumerate(batches, 1):
        name = f"Section{i:02d}"
        rel = (f"src/components/{name}{ext}" if framework != "next"
               else f"components/{name}{ext}")
        say("stage", f"section {i}/{len(batches)} -> {rel}")
        t0 = time.time()
        res = code.run_once(dest, SECTION_PROMPT.format(
            path=rel, slice=slice_text[:6000], lang=LANGS[framework],
            export_line=EXPORTS[framework]),
            cfg={"allowed_tools": ["Write"], "disallowed_tools": ["Bash"],
                 "aethron_tools": False, "permission_mode": "acceptEdits",
                 **(cfg or {})},
            home=home, timeout=900,
            on_event=lambda e: _stream(e, say))
        ok_file = (dest / rel).exists()
        written += bool(ok_file)
        history.append({"section": i, "file": rel, "written": ok_file,
                        "cost": res.get("cost_usd", 0),
                        "seconds": round(time.time() - t0)})
        say("section", f"{name}: {'written' if ok_file else 'MISSING'} "
                       f"({round(time.time() - t0)}s, "
                       f"${res.get('cost_usd', 0):.3f})")
        if _stopped():
            say("error", "spend guard stopped the run")
            break

    if not written:
        return {"ok": False, "stage": "no-sections", "dir": str(dest),
                "history": history,
                "verdict": "the agent wrote no section files"}

    # ── 2. Aethron assembles the page. The model never holds it whole.
    assemble(framework, dest, written, cfgj.get("name", "site"))
    say("stage", f"assembled {written} section(s) into the page")

    # ── 3. build, with at most one repair turn — a port that cannot
    #      compile after one fix is a report, not another spend.
    for attempt in (1, 2):
        ok, log = npm(meta["build"], dest)
        if ok:
            break
        say("error", f"build failed (attempt {attempt})")
        if attempt == 2 or _stopped():
            return {"ok": False, "stage": "build-failed", "dir": str(dest),
                    "history": history, "verdict": log[-1200:]}
        code.run_once(dest, "The build failed. Fix ONLY the files named in "
                            "these errors, then stop:\n\n" + log[-3000:],
                      cfg={"disallowed_tools": ["Bash"],
                           "aethron_tools": False,
                           "permission_mode": "acceptEdits", **(cfg or {})},
                      home=home, timeout=900,
                      on_event=lambda e: _stream(e, say))

    out_dir = dest / meta["out"]
    say("stage", f"grading against the original ({out_dir.name}/)…")
    passed, verdict = referee(project, out_dir, pages[0])
    say("referee", verdict)
    if passed:
        say("done", "PORT ACCEPTED")
        return {"ok": True, "stage": "graded", "verdict": verdict,
                "dir": str(dest), "out": str(out_dir), "history": history}

    # ── 4. one gap-filling turn against the referee's own complaint
    if rounds > 1 and not _stopped():
        say("stage", "one repair round against the referee's findings")
        code.run_once(dest, "Your port builds but does not match the "
                            "original:\n\n" + verdict +
                            "\n\nThe missing content is in "
                            f"`{src[0]['outline']}`. Add it to the existing "
                            "section components (or add one more section "
                            "file and it will be picked up). Write files "
                            "only, then stop.",
                      cfg={"disallowed_tools": ["Bash"],
                           "aethron_tools": False,
                           "permission_mode": "acceptEdits", **(cfg or {})},
                      home=home, timeout=900,
                      on_event=lambda e: _stream(e, say))
        extra = sorted((dest / ("components" if framework == "next"
                                else "src/components")).glob(f"Section*{ext}"))
        if len(extra) > written:
            assemble(framework, dest, len(extra), cfgj.get("name", "site"))
            written = len(extra)
        ok, log = npm(meta["build"], dest)
        if ok:
            passed, verdict = referee(project, out_dir, pages[0])
            say("referee", verdict)
            if passed:
                say("done", "PORT ACCEPTED after the repair round")
                return {"ok": True, "stage": "repaired", "verdict": verdict,
                        "dir": str(dest), "out": str(out_dir),
                        "history": history}

    say("stuck", "the port does not match the original yet — not accepted")
    return {"ok": False, "stage": "not-accepted", "verdict": feedback[-1500:],
            "dir": str(dest), "history": history}


def _spend_line():
    try:
        import aethron_brain as brain
        u = brain.spend()
        if u.get("requests"):
            return (f"spent this run: {u['requests']} requests, "
                    f"{u['input'] + u['output']:,} tokens, "
                    f"~${u['usd']:.2f}" + (f" — STOPPED: {u['stopped']}"
                                           if u.get("stopped") else ""))
    except Exception:
        pass
    return ""


def _stream(e, say):
    if e["type"] == "tool":
        say("tool", f"→ {e['name']} {json.dumps(e.get('input', {}))[:110]}")
    elif e["type"] == "text" and e.get("text"):
        say("say", e["text"][:300])
    elif e["type"] == "done" and e.get("error"):
        say("error", e.get("text", "")[:300])


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    project = Path(argv[0]).expanduser()
    fw = "next"
    rounds = 3
    if "--framework" in argv:
        fw = argv[argv.index("--framework") + 1]
    if "--rounds" in argv:
        rounds = int(argv[argv.index("--rounds") + 1])
    pages = None
    if "--pages" in argv:
        pages = argv[argv.index("--pages") + 1].split(",")
    res = export(project, fw, rounds, pages,
                 install="--no-install" not in argv,
                 live="--live" in argv)
    print()
    line = _spend_line()
    if line:
        print(line)
    print("ACCEPTED:" if res.get("ok") else "NOT ACCEPTED:",
          res.get("verdict", res.get("stage")))
    if res.get("history"):
        print("rounds:", json.dumps(res["history"]))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
