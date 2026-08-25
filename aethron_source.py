#!/usr/bin/env python3
"""Recover a Framer template's ORIGINAL SOURCE, then map its animations.

Every chunk a Framer site ships ends with

    //# sourceMappingURL=<name>.mjs.map

and the CDN serves those maps with `sourcesContent` populated — the
authored source of every component in the template, unminified, with
real identifier names:

    import{motion,useScroll,useTransform}from"framer-motion";
    const {scrollYProgress}=useScroll({target:ref,
                                       offset:["start 0.75","start 0.15"]});

That single fact retires a long run of guesswork. Until now the only
way to learn what an element did was to WATCH it — capture the rendered
DOM, diff transforms over time, infer a curve. Inference cannot see an
animation that changes colour, cannot read a spring's stiffness, and
cannot tell a design value from an entrance pose. Reading the source
can do all three, exactly, for free, with no browser and no model.

What is and is not recoverable:

  RECOVERABLE   the template author's own components — the 130-odd
                modules that make up the site the owner bought, both
                Framer-generated page/section code and hand-written
                code components (Ticker, SlideShow, counters, text
                effects). This is the part that carries the design.

  NOT SERVED    Framer's vendored runtime bundles (react, framer,
                rolldown-runtime) answer 403 for their maps. No loss:
                the animation engine underneath is framer-motion, a
                public npm package, and React is React.

  STILL OURS    the `framer` package those components import from is
                proprietary and must be SHIMMED, not shipped. The
                surface is finite and this module reports it, so the
                shim can be written against measured reality instead
                of a guess. Most of it is inert in a production port —
                property controls only exist for the Framer canvas.

Honesty rule (the vacuous-pass lesson): a template whose maps are not
served reports UNAVAILABLE and recovers nothing. It never reports an
empty map as a clean one.

    python3 aethron_source.py [project_dir]          recover + map
    python3 aethron_source.py --map-only [project]   re-map what is local
"""
import concurrent.futures as futures
import json
import re
import sys
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

# The site bundle root, e.g. https://framerusercontent.com/sites/<id>
SITE_BASE_RE = re.compile(r'https://framerusercontent\.com/sites/[A-Za-z0-9_-]+')

# Framer's own packages. Their maps are 403 and we would not ship them
# anyway — the port depends on framer-motion from npm, not on Framer.
VENDOR_STEMS = ("chunk-", "react", "framer-motion", "motion.", "framer.",
                "rolldown-runtime", "node_modules")

# What each primitive MEANS, so the map reads like an inventory and not
# like a grep. Order matters: the first match labels the animation.
PRIMITIVES = [
    ("useScroll",        "scroll-linked"),
    ("scrollYProgress",  "scroll-linked"),
    ("useSpring",        "spring"),
    ("useInView",        "plays when it enters the viewport"),
    ("whileInView",      "plays when it enters the viewport"),
    ("whileHover",       "hover"),
    ("whileTap",         "tap"),
    ("AnimatePresence",  "enter/exit"),
    ("useTime",          "continuous"),
    ("useVelocity",      "velocity-driven"),
    ("stagger",          "staggered"),
    ("useTransform",     "mapped value"),
    ("useMotionValue",   "motion value"),
    ("variants:",        "variants"),
    ("transition:",      "transition"),
    ("animate(",         "imperative"),
]


def log(msg):
    print(f"  {msg}")


# ───────────────────────── recovery ──────────────────────────────────

def _site_base(root: Path) -> str:
    """The CDN prefix the chunks were served from, read from any page."""
    for page in sorted((root / "pristine").glob("*.html")):
        m = SITE_BASE_RE.search(page.read_text(errors="replace"))
        if m:
            return m.group(0)
    return ""


def _is_vendor(origin_name: str, text: str) -> bool:
    if origin_name.startswith(VENDOR_STEMS):
        return True
    # Framer's own bundles re-export straight out of node_modules paths;
    # an authored component never does.
    return "node_modules/" in text[:4000]


def recover(root: Path, quiet: bool = False) -> dict:
    """Fetch every chunk's source map and expand it into pristine/sources/.

    Returns the index dict (also written to sources/index.json)."""
    chunks = sorted((root / "pristine" / "chunks").glob("*.mjs"))
    if not chunks:
        return {"available": False, "reason": "no chunks — not a Framer export"}
    base = _site_base(root)
    if not base:
        return {"available": False,
                "reason": "no framerusercontent site base in the pages"}

    out = root / "pristine" / "sources"
    out.mkdir(parents=True, exist_ok=True)

    def grab(path: Path):
        try:
            req = urllib.request.Request(f"{base}/{path.name}.map", headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return path.name, r.read()
        except Exception as exc:                      # 403 for vendored
            return path.name, exc

    served, denied = 0, []
    modules = {}
    with futures.ThreadPoolExecutor(8) as pool:
        for name, body in pool.map(grab, chunks):
            if not isinstance(body, (bytes, bytearray)):
                denied.append(name)
                continue
            served += 1
            try:
                smap = json.loads(body)
            except Exception:
                denied.append(name)
                continue
            sources = smap.get("sources") or []
            contents = smap.get("sourcesContent") or []
            for src, text in zip(sources, contents):
                if not text:
                    continue
                origin = src.rsplit("/", 1)[-1]
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", origin)
                # Two chunks can carry the same module; keep one copy.
                dest = out / safe
                if not dest.exists():
                    dest.write_text(text)
                modules[safe] = {
                    "origin": origin,
                    "url": src,
                    "chunk": name,
                    "bytes": len(text),
                    "vendor": _is_vendor(origin, text),
                }

    authored = {k: v for k, v in modules.items() if not v["vendor"]}
    index = {
        "available": bool(authored),
        "base": base,
        "chunks": len(chunks),
        "maps_served": served,
        "maps_denied": len(denied),
        "denied": sorted(denied),
        "modules": modules,
        "authored": len(authored),
        "vendored": len(modules) - len(authored),
    }
    (out / "index.json").write_text(json.dumps(index, indent=2))
    if not quiet:
        if not authored:
            log("source maps UNAVAILABLE — nothing recovered "
                f"({len(denied)} of {len(chunks)} chunks denied)")
        else:
            log(f"source maps: {served}/{len(chunks)} chunks served")
            log(f"recovered {len(authored)} authored module(s), "
                f"{index['vendored']} vendored (not shipped)")
    return index


# ───────────────────── animation extraction ──────────────────────────

def _balanced(text: str, start: int) -> str:
    """The object literal beginning at text[start] == '{', braces matched.

    Framer's authored output is emitted on very long lines, so a regex
    for `transition:{...}` stops at the first '}' and truncates nested
    configs. Counting is the only thing that reads them whole."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        elif c in "\"'`":                       # skip strings
            quote, i = c, i + 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        i += 1
    return text[start:start + 400]


def _line_col(text: str, pos: int):
    line = text.count("\n", 0, pos) + 1
    col = pos - (text.rfind("\n", 0, pos) + 1)
    return line, col


def _configs(text: str) -> list:
    """The literal numbers behind each animation, with where they live.

    The owner asked for the LINES. Framer emits authored modules as one
    very long line, so a line number alone would say '1' for everything
    — the column and the excerpt are what actually locate the code."""
    found = []

    def add(kind, pos, code):
        line, col = _line_col(text, pos)
        found.append({"kind": kind, "line": line, "col": col,
                      "code": code[:400]})

    for m in re.finditer(r"transition:\s*\{", text):
        add("transition", m.start(), _balanced(text, m.end() - 1))
    for m in re.finditer(r"useScroll\s*\(\s*\{", text):
        add("useScroll", m.start(), _balanced(text, m.end() - 1))
    for m in re.finditer(r"variants:\s*\{", text):
        add("variants", m.start(), _balanced(text, m.end() - 1))
    for m in re.finditer(r"useTransform\s*\(", text):
        add("useTransform", m.start(), text[m.start():m.start() + 220])
    for m in re.finditer(r"\bspeed\s*:\s*(-?[\d.]+)", text):
        add("speed", m.start(), m.group(0))
    return found


# A Framer element class is a whole token: framer-1g1hmix, framer-DIoSH.
# Both guards are load-bearing. Without the lookbehind this also matches
# inside the CSS custom properties (--framer-link-hover-text-color), and
# without the lookahead it truncates every hyphenated name to its first
# word — which is how "framer-link" came to "match" 8,957 elements.
FRAMER_CLASS_RE = re.compile(r'(?<![-\w])framer-[A-Za-z0-9]{4,}(?![\w-])')


def _class_tokens(text: str) -> set:
    """Every framer-* class a module mentions, however it is assembled.

    Generated components rarely write className="framer-X" — they build
    it with cx(scopingClassNames, …) and template literals, so matching
    the literal attribute finds a handful of modules and misses every
    one that actually animates. Collecting the tokens themselves finds
    the per-element classes (framer-1g1hmix), which map at element
    granularity rather than component granularity."""
    return set(FRAMER_CLASS_RE.findall(text))


# Modules import each other by absolute CDN url ending in the filename:
#   import Slideshow from "https://…/modules/…/SlideShow.js"
IMPORT_RE = re.compile(r'from\s*"https://framerusercontent\.com/modules/'
                       r'[^"]*/([A-Za-z0-9._-]+\.js)"')


def _imports_of(text: str) -> set:
    return set(IMPORT_RE.findall(text))


def build_map(root: Path, quiet: bool = False) -> dict:
    """Write animations.json: every animation, its code, and its elements."""
    out = root / "pristine" / "sources"
    idx_file = out / "index.json"
    if not idx_file.exists():
        return {"available": False, "reason": "run recovery first"}
    index = json.loads(idx_file.read_text())
    if not index.get("available"):
        return index

    pages = {p.name: p.read_text(errors="replace")
             for p in sorted((root / "pristine").glob("*.html"))}

    # Read every authored module once: the import graph and the class of
    # each module are both needed before any single one can be resolved.
    texts, tokens_of, importers = {}, {}, {}
    for name, meta in sorted(index["modules"].items()):
        if meta["vendor"] or not (out / name).exists():
            continue
        texts[name] = (out / name).read_text(errors="replace")
        tokens_of[name] = _class_tokens(texts[name])

    # framer-appear / framer-text / framer-image and friends are runtime
    # classes shared by everything; a class that shows up across many
    # unrelated modules identifies the framework, not an element. Let
    # the corpus say which are which instead of hard-coding a stoplist.
    freq = {}
    for toks in tokens_of.values():
        for t in toks:
            freq[t] = freq.get(t, 0) + 1
    generic = {t for t, n in freq.items() if n > max(3, len(texts) * 0.25)}
    classes_of = {n: sorted(t for t in toks if t not in generic)
                  for n, toks in tokens_of.items()}

    for name, text in texts.items():
        for target in _imports_of(text):
            importers.setdefault(target, set()).add(name)

    def sites_for(name, seen=None):
        """Where this module's markup actually lands in the pages.

        A hand-written code component (Ticker, SlideShow) renders no
        class of its own — it is mounted by a generated component that
        does. Walking up the import graph is what connects 'this file
        holds the marquee' to 'these elements are the marquee'."""
        seen = seen or set()
        if name in seen:
            return {}
        seen.add(name)
        own = classes_of.get(name) or []
        if own:
            hits = {}
            for c in own:
                # Same whole-token rule on the page side, or a class
                # would be credited with every longer class it prefixes.
                pat = re.compile(re.escape(c) + r"(?![\w-])")
                per = {pg: len(pat.findall(html))
                       for pg, html in pages.items()}
                per = {pg: n for pg, n in per.items() if n}
                if per:
                    hits[c] = per
            return hits
        found = {}
        for parent in sorted(importers.get(name, ())):
            found.update(sites_for(parent, seen))
        return found

    components, framer_api = [], {}
    for name in sorted(texts):
        meta = index["modules"][name]
        text = texts[name]

        for m in re.finditer(r'import\s*\{([^}]*)\}\s*from\s*"framer"', text):
            for sym in m.group(1).split(","):
                sym = sym.strip().split(" as ")[0].strip()
                if sym:
                    framer_api[sym] = framer_api.get(sym, 0) + 1

        kinds = [label for key, label in PRIMITIVES if key in text]
        if not kinds:
            continue
        classes = classes_of.get(name) or []
        elements = {c: h for c, h in sites_for(name).items() if h}
        components.append({
            # A component whose markup we cannot find is reported as
            # unlocated, never as having no animations. The code is
            # real; only its place on the page is unknown.
            "located": bool(elements),
            "mounted_by": sorted(importers.get(name, ())) if not classes
                          else [],
            "module": name,
            "origin": meta["origin"],
            "chunk": meta["chunk"],
            "bytes": meta["bytes"],
            "behaviours": sorted(set(kinds)),
            "classes": classes,
            "elements": elements,
            "code": _configs(text),
            "imports_framer": sorted({
                s.strip().split(" as ")[0].strip()
                for m in re.finditer(
                    r'import\s*\{([^}]*)\}\s*from\s*"framer"', text)
                for s in m.group(1).split(",") if s.strip()}),
            "imports_motion": "framer-motion" in text,
        })

    components.sort(key=lambda c: (-len(c["behaviours"]), -c["bytes"]))
    doc = {
        "available": True,
        "source": "sourcemaps",
        "components": components,
        "totals": {
            "modules_with_motion": len(components),
            "animation_sites": sum(len(c["code"]) for c in components),
            "uses_framer_motion": sum(1 for c in components
                                      if c["imports_motion"]),
        },
        # The shim surface. A port replaces every one of these; knowing
        # the real list is what makes that job finite.
        "framer_package_api": dict(sorted(framer_api.items(),
                                          key=lambda kv: -kv[1])),
    }
    (root / "animations.json").write_text(json.dumps(doc, indent=2))
    if not quiet:
        t = doc["totals"]
        log(f"{t['modules_with_motion']} component(s) carry motion, "
            f"{t['animation_sites']} animation site(s) located")
        log(f"framer package surface to shim: "
            f"{len(doc['framer_package_api'])} export(s)")
        log("wrote animations.json")
    return doc


def summarise(doc: dict, limit: int = 14) -> str:
    if not doc.get("available"):
        return f"UNAVAILABLE — {doc.get('reason', 'no source maps served')}"
    lines = []
    for c in doc["components"][:limit]:
        if c["elements"]:
            n = sum(sum(v.values()) for v in c["elements"].values())
            where = f"  [{n} element(s)]"
        else:
            where = "  [not located]"
        lines.append(f"  {c['origin'][:36]:36} {', '.join(c['behaviours'])}"
                     f"{where}")
    more = len(doc["components"]) - limit
    if more > 0:
        lines.append(f"  … and {more} more")
    return "\n".join(lines)


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    root = Path(args[0]).resolve() if args else Path.cwd()
    if not (root / "forge.json").exists():
        print(f"ERROR: not a project directory: {root}")
        return 1
    if "--map-only" not in argv:
        index = recover(root)
        if not index.get("available"):
            print(f"  UNAVAILABLE: {index.get('reason', 'no maps served')}")
            return 1
    doc = build_map(root)
    print(summarise(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
