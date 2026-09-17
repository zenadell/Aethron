#!/usr/bin/env python3
"""Regenerate studio.py's METAL_JS from metal-fx's own source.

    python3 vendor/build_metal.py            # writes vendor/metal.js
    python3 vendor/build_metal.py --install  # ...and splices it into studio.py

WHY THIS EXISTS RATHER THAN A COPY OF THE EFFECT. metal-fx ships a React
component and Aethron has no React — but their own index.ts calls the
engine primitives a "power-user surface ... for consumers building
non-React integrations", so the supported path is to drive the engine
directly. This script strips the TypeScript (node's own
stripTypeScriptTypes — no npm, no bundler), drops the import/export
syntax, and concatenates the modules in dependency order. Nothing about
the effect is reimplemented.

Sources, fetched on demand into vendor/.cache:
  metal-fx v2.0.0  MIT, (c) 2026 Jakub Antalik
                   https://github.com/Jakubantalik/metal-fx
  @paper-design/shaders 0.0.80  Apache-2.0, Paper Shaders, (c) 2026 Paper
                   https://shaders.paper.design
                   — ONE symbol, liquidMetalFragmentShader, which is what
                   metal-fx imports and its own vite build inlines.
See vendor/NOTICE-metal-fx.md.

TWO THINGS THE FLAT FORM NEEDS THAT MODULES GIVE FOR FREE:
  * scope. Every module's private top-level names land in ONE scope, so
    duplicates are an early SyntaxError (and, unwrapped, would collide
    with the page's own names). dedupe() renames the private ones and
    the mount wraps the result in a closure.
  * an honest gate. `node --check x.js` DETECTS module syntax and
    re-checks as ESM, which accepts `export` and hides duplicate
    declarations — both of which a <script> tag refuses. The check runs
    against a .cjs copy, which is what a classic script actually is.
"""
import io, json, os, re, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
METAL_TAG = "v2.0.0"
PAPER_VER = "0.0.80"


def fetch():
    """Get both upstreams into vendor/.cache (git + npm, nothing else)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    repo = CACHE / "metal-fx"
    if not (repo / "src").exists():
        subprocess.run(["git", "clone", "--depth", "1", "--branch", METAL_TAG,
                        "https://github.com/Jakubantalik/metal-fx.git", str(repo)],
                       check=True)
    glsl = CACHE / "liquid-metal.glsl"
    if not glsl.exists():
        pkg = CACHE / "paper"
        pkg.mkdir(exist_ok=True)
        subprocess.run(["npm", "pack", f"@paper-design/shaders@{PAPER_VER}"],
                       cwd=pkg, check=True)
        tgz = next(pkg.glob("paper-design-shaders-*.tgz"))
        subprocess.run(["tar", "xzf", tgz.name], cwd=pkg, check=True)
        out = subprocess.run(
            ["node", "-e",
             "import(process.argv[1]).then(m=>"
             "process.stdout.write(m.liquidMetalFragmentShader))",
             str(pkg / "package/dist/index.js")],
            capture_output=True, text=True, check=True)
        glsl.write_text(out.stdout, encoding="utf-8")
    return str(repo / "src") + "/", glsl


SRC, GLSL_PATH = None, None

ORDER = [
    'engine/bend/config.ts', 'engine/color.ts', 'engine/perfConfig.ts',
    'engine/presets.ts', 'engine/shaders.ts',
    'engine/renderer/core.ts', 'engine/renderer/outline.ts',
    'engine/renderer/sampling.ts', 'engine/renderer/loop.ts',
    'engine/cursor/light.ts',
    'engine/glow/config.ts', 'engine/glow/bake.ts', 'engine/glow/geometry.ts',
    'engine/tween.ts', 'engine/glow/glow.ts',
    'engine/reflection/constants.ts', 'engine/reflection/geometry.ts',
    'engine/reflection/observers.ts', 'engine/reflection/paint.ts',
    'engine/reflection/reflectionScheduler.ts',
    'engine/rim.ts', 'engine/textMask.ts',
    'styles.ts',
]

def strip_types(path):
    js = subprocess.run(
        ['node', '-e',
         "const{stripTypeScriptTypes}=require('node:module');"
         "const fs=require('fs');"
         "process.stdout.write(stripTypeScriptTypes("
         "fs.readFileSync(process.argv[1],'utf8'),{mode:'strip'}))",
         path], capture_output=True, text=True)
    if js.returncode:
        sys.exit(f'{path}: {js.stderr}')
    return js.stdout

# `import {...} from '...'` / `export {...} from '...'` — possibly multiline.
IMP = re.compile(r"^(?:import|export)\s*(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)?\s*"
                 r"(?:from\s*)?['\"][^'\"]+['\"];?\s*$", re.M | re.S)
IMP_ML = re.compile(r"^(?:import|export)\s*\{[^}]*?\}\s*from\s*['\"][^'\"]+['\"];?",
                    re.M | re.S)

# `export { A, B };` with NO module path — a re-export of local names.
BARE = re.compile(r"^export\s*\{[^}]*\}\s*;?\s*$", re.M)

def flatten(src):
    src = IMP_ML.sub('', src)
    src = IMP.sub('', src)
    src = BARE.sub('', src)
    # a bare `export` keyword in front of a declaration
    src = re.sub(r'^export\s+(?=(const|let|var|function|class|async)\b)',
                 '', src, flags=re.M)
    return src

DECL = re.compile(
    r"^(?P<exp>export\s+)?(?:async\s+)?"
    r"(?:const|let|var|function|class)\s+(?P<name>[A-Za-z_$][\w$]*)", re.M)

def declared(src):
    """Top-level names this module declares, and whether it exports them."""
    out = {}
    for m in DECL.finditer(src):
        # only column 0 — nested declarations are already scoped
        line = src[src.rfind('\n', 0, m.start()) + 1:m.start()]
        if line.strip():
            continue
        out[m.group('name')] = bool(m.group('exp'))
    return out


def dedupe(mods):
    """ESM gives every module its own scope; a flat concatenation does not.
    Two modules here both declare `_pt`, which is a private helper in each —
    legal as modules, an early SyntaxError as one script. Rename the private
    ones per module. A name two modules both EXPORT is a real ambiguity and
    is refused rather than silently renamed."""
    seen = {}
    for rel, src in mods:
        for name, exported in declared(src).items():
            seen.setdefault(name, []).append((rel, exported))
    ren = {}
    for name, uses in seen.items():
        if len(uses) < 2:
            continue
        exporters = [r for r, e in uses if e]
        if len(exporters) > 1:
            sys.exit(f'collision on exported name {name!r}: {exporters}')
        keep = exporters[0] if exporters else uses[0][0]
        for i, (rel, _) in enumerate(uses):
            if rel != keep:
                ren.setdefault(rel, {})[name] = f'{name}__m{i}'
    return ren


SRC, GLSL_PATH = fetch()

out = ["/* metal-fx v2.0.0 — MIT © 2026 Jakub Antalik "
       "(github.com/Jakubantalik/metal-fx).\n"
       "   Flattened from their own ES modules by build_metal.py; the engine\n"
       "   code below is theirs, unchanged apart from module syntax. */"]
# metal-fx imports ONE symbol from @paper-design/shaders (Apache-2.0) —
# the liquid-metal fragment shader — and its own file says why: so the
# material stays byte-identical to Paper's. Their vite build inlines it;
# we resolve the same string from the published package and hand it over
# as a plain constant. The NOTICE Apache-2.0 asks for ships beside it.
GLSL = io.open(GLSL_PATH, encoding='utf-8').read()
out.append('\n/* ── @paper-design/shaders liquid-metal (Apache-2.0)\n'
           '   Paper Shaders, Copyright 2026 Paper — https://shaders.paper.design\n'
           '   The one symbol metal-fx imports, resolved from the published\n'
           '   package exactly as their own build inlines it. */')
out.append('const liquidMetalFragmentShader = ' + json.dumps(GLSL) + ';')

mods = [(rel, flatten(strip_types(SRC + rel))) for rel in ORDER]
ren = dedupe(mods)
for rel, src in mods:
    for old, new in (ren.get(rel) or {}).items():
        src = re.sub(rf'\b{re.escape(old)}\b', new, src)
        print(f'  renamed {old} -> {new} in {rel}')
    out.append(f"\n/* ── {rel} ─────────────────────────────── */")
    out.append(src)
body = '\n'.join(out)
print('engine chars', len(body))
# THE GATE HAS TO SEE WHAT THE BROWSER SEES. `node --check x.js` detects
# module syntax and silently accepts `export` — which is exactly the
# token that killed the first build in a <script> tag. Check it as .cjs,
# where ESM syntax is the error it will be in the page.
ENGINE = HERE / 'metal-engine.js'
io.open(ENGINE, 'w', encoding='utf-8').write(body)
shutil.copyfile(ENGINE, HERE / '.gate.cjs')
r = subprocess.run(['node', '--check', str(HERE / '.gate.cjs')],
                   capture_output=True, text=True)
os.remove(HERE / '.gate.cjs')
print(r.stderr[:1500] or 'node --check (classic script) OK')
assert r.returncode == 0, 'engine does not parse as a plain script'

# ── the shipped file ─────────────────────────────────────────────────
# ONE CLOSURE, TWO NAMES OUT. A classic <script> shares the global scope
# with the page's own script, and ~20 flattened modules put every one of
# their private top-level names into it. Measured: `cache` collided with
# Aethron's own and took the whole interface down with an uncaught
# SyntaxError. 'use strict' is not a choice either — these were ES
# modules, which are always strict.
MOUNT = io.open(HERE / 'metal-mount.js', encoding='utf-8').read()
PAYLOAD = (
    "/* THE WHOLE ENGINE LIVES IN ITS OWN SCOPE — see build_metal.py. */\n"
    "(function(){\n'use strict';\n" + body + "\n" + MOUNT +
    "\nwindow.metalWrap = metalWrap;\nwindow.metalReady = true;\n})();\n")

assert '</script' not in PAYLOAD.lower(), \
    'a literal </script> would end the block it is inlined in'
io.open(HERE / 'metal.js', 'w', encoding='utf-8').write(PAYLOAD)

io.open(HERE / '.gate2.cjs', 'w', encoding='utf-8').write(PAYLOAD)
r = subprocess.run(['node', '--check', str(HERE / '.gate2.cjs')],
                   capture_output=True, text=True)
os.remove(HERE / '.gate2.cjs')
print(r.stderr[:1500] or 'metal.js parses as a plain script')
assert r.returncode == 0
print('metal.js', len(PAYLOAD), 'chars ->', HERE / 'metal.js')

if '--install' in sys.argv:
    # Splice it into studio.py's METAL_JS. Embedded rather than read from
    # disk at runtime: a data file is one more thing that can be missing
    # from the packaged app, and this project has found the bundle stale
    # three times.
    for bad in ('"""', "'''"):
        assert bad not in PAYLOAD, f'{bad!r} would end the python string'
    assert not PAYLOAD.rstrip('\n').endswith('\\'), 'trailing backslash'
    sp = HERE.parent / 'studio.py'
    s = io.open(sp, encoding='utf-8').read()
    i = s.index('METAL_JS = r"""')
    j = s.index('"""\n', i + 15)
    io.open(sp, 'w', encoding='utf-8').write(
        s[:i] + 'METAL_JS = r"""' + PAYLOAD + '"""\n' + s[j + 4:])
    print('spliced into studio.py')
