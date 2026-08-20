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
        _write(dest / "app/layout.tsx",
               "import type { Metadata } from 'next';\n"
               "import './globals.css';\n\n"
               "export const metadata: Metadata = { title: '"
               + name + "' };\n\n"
               "export default function RootLayout("
               "{ children }: { children: React.ReactNode }) {\n"
               "  return (<html lang=\"en\"><body>{children}</body></html>);\n"
               "}\n")
        _write(dest / "app/globals.css",
               "*,*::before,*::after{box-sizing:border-box}\n"
               "body{margin:0}\n")
        _write(dest / "app/page.tsx",
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
        _write(dest / "src/pages/index.astro",
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
    _write(dest / "index.html",
           "<!doctype html><html lang=\"en\"><head><meta charset=\"UTF-8\" />"
           "<meta name=\"viewport\" content=\"width=device-width,"
           "initial-scale=1\" /><title>" + name + "</title></head>"
           "<body><div id=\"root\"></div>"
           "<script type=\"module\" src=\"/src/main.tsx\"></script>"
           "</body></html>\n")
    _write(dest / "src/main.tsx",
           "import { StrictMode } from 'react';\n"
           "import { createRoot } from 'react-dom/client';\n"
           "import App from './App';\n"
           "createRoot(document.getElementById('root')!).render("
           "<StrictMode><App /></StrictMode>);\n")
    _write(dest / "src/App.tsx",
           "export default function App() {\n"
           "  return <main>replace me with the ported home page</main>;\n"
           "}\n")
    return {"out": "dist", "build": ["run", "build"]}


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


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


# ─────────────────────────── the loop ────────────────────────────────

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
           cfg=None, home=None, install=True):
    project = Path(project).resolve()
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
    prompt = PORT_PROMPT.format(
        framework=framework, public=public, structure=STRUCTURE[framework],
        pages="\n".join(f"- {s['page']}: outline {s['outline']} "
                        f"({s['outline_chars']} chars) · full DOM {s['file']} "
                        f"· {len(s['headings'])} headings, "
                        f"{s['chars']} chars of text"
                        for s in src))
    feedback = ""
    history = []
    for rnd in range(1, rounds + 1):
        say("stage", f"agent round {rnd}/{rounds}")
        t0 = time.time()
        session_cfg = {"disallowed_tools": ["Bash"],
                       "aethron_tools": False,
                       "permission_mode": "acceptEdits",
                       **(cfg or {})}
        res = code.run_once(dest, prompt + feedback, cfg=session_cfg,
                            home=home, timeout=3600,
                            on_event=lambda e: _stream(e, say))
        history.append({"round": rnd, "tools": res.get("tools", []),
                        "cost": res.get("cost_usd", 0),
                        "seconds": round(time.time() - t0)})
        say("stage", "building the port…")
        ok, log = npm(meta["build"], dest)
        if not ok:
            say("error", f"build failed (round {rnd})")
            feedback = ("\n\nYOUR LAST ATTEMPT DID NOT BUILD. Fix these "
                        "errors:\n" + log[-3000:])
            continue
        out_dir = dest / meta["out"]
        say("stage", f"grading against the original ({out_dir.name}/)…")
        passed, verdict = referee(project, out_dir, pages[0])
        say("referee", verdict)
        if passed:
            say("done", f"PORT ACCEPTED after round {rnd}")
            return {"ok": True, "stage": f"round-{rnd}", "verdict": verdict,
                    "dir": str(dest), "out": str(out_dir),
                    "history": history}
        feedback = ("\n\nYOUR LAST ATTEMPT BUILT BUT DID NOT MATCH THE "
                    "ORIGINAL:\n" + verdict + "\nFix exactly that: add the "
                    "missing content, keep the wording identical.")
    say("stuck", "the port does not match the original yet — not accepted")
    return {"ok": False, "stage": "not-accepted", "verdict": feedback[-1500:],
            "dir": str(dest), "history": history}


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
                 install="--no-install" not in argv)
    print()
    print("ACCEPTED:" if res.get("ok") else "NOT ACCEPTED:",
          res.get("verdict", res.get("stage")))
    if res.get("history"):
        print("rounds:", json.dumps(res["history"]))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
