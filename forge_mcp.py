#!/usr/bin/env python3
"""Aethron MCP server — drive template migrations from ANY AI
agent (Cursor, Antigravity, any AI IDE — anything MCP-speaking).

Zero dependencies: stdio JSON-RPC 2.0, newline-delimited.

Register via your MCP client's CLI, or drop this in .mcp.json:
    {"mcpServers": {"aethron":
      {"command": "python3", "args": ["forge_mcp.py"]}}}

The agent IS the copy model: read entries with get_content, write them
with set_content_bulk — every write passes the same guardrails as the
studio (CMS byte budgets, forbidden characters) and is snapshotted for
undo. The typical migration:

  create_project -> fetch -> inventory -> set_plan ->
  get_content(only_unfilled) -> set_content_bulk -> build -> verify
"""
import json
import re
import shutil
import subprocess
import sys
import secrets
import time
import urllib.parse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# THE ENGINE, FROM SOURCE OR FROM INSIDE THE APP. From source,
# sys.executable is Python and `python forge.py init …` runs the engine.
# Inside the packaged app sys.executable is AETHRON ITSELF, and
# `Aethron forge.py init …` does not run the engine — it opens a SECOND
# COPY OF THE APP, which then sits there until whatever called it gives
# up. That was every migration tool the agent had: create, fetch,
# inventory, build, verify, preview. The app answers the engine only as
# `Aethron --forge <cmd>` (desktop.run_forge_mode), exactly as
# studio.forge_argv already does. Measured: `init` timing out at 300s
# while a second Aethron announced itself running in the background.
_FORGE_CMD = ([sys.executable, "--forge"] if getattr(sys, "frozen", False)
              else [sys.executable, str(ROOT / "forge.py")])

FORGE = ROOT / "forge.py"
# Where the USER's data lives. The desktop app (and any agent session it
# starts) sets AETHRON_HOME because the bundle itself is read-only — so
# the MCP server must follow it, or an agent inside the app sees an
# empty project list while the studio shows a dozen.
# .resolve() matters: macOS hands out /var/folders/... which resolves to
# /private/var/folders/..., and a user's home can be a symlink too. The
# path guard below compares a RESOLVED project dir against this root, so
# an unresolved root rejects every project with "unknown project".
HOME = Path(os.environ.get("AETHRON_HOME", ROOT)).resolve()
PROJECTS = HOME / "projects"
LIBRARY = HOME / "library"   # design cards: fingerprints, never files
PAGES = HOME / "pages"       # screenshots rebuilt into pages; the studio shows these
PREVIEWS = {}


def page_slot(stem):
    """A fresh folder under PAGES for a page named after its screenshot.

    A rebuild used to land in rebuilds/<hash>/page.html — a path in a
    hidden Library folder that the person was handed as the answer and
    could not find. Pages live where the studio looks, so the result
    opens in the preview instead."""
    base = re.sub(r"[^a-z0-9-]+", "-", str(stem).lower()).strip("-")[:40] or "page"
    name, n = base, 2
    while (PAGES / name).exists():
        name, n = f"{base}-{n}", n + 1
    (PAGES / name).mkdir(parents=True)
    return PAGES / name


def page_snapshot(page):
    """Keep the version a change is about to replace, for Undo in the studio."""
    page = Path(page).resolve()
    try:
        page.relative_to(PAGES.resolve())
    except ValueError:
        return
    if page.name != "site.html" or not page.is_file():
        return
    hist = page.parent / ".history"
    hist.mkdir(exist_ok=True)
    import time as _t
    (hist / f"{_t.time():.6f}.html").write_bytes(page.read_bytes())
    for old in sorted(hist.glob("*.html"))[:-30]:
        old.unlink()

sys.path.insert(0, str(ROOT))
from forge import (image_slot_styles, _locate_element, word_fragments,  # noqa: E402
                   hide_selector_audit)


# ───────────────────────── shared plumbing ───────────────────────────

class Result(str):
    """A tool's answer: readable text for a model, plus `data` — the same
    facts as JSON for a program (sent as MCP structuredContent), so no
    caller has to parse prose."""
    def __new__(cls, text, data):
        o = str.__new__(cls, text)
        o.data = data
        return o


def pdir(name: str) -> Path:
    d = (PROJECTS / str(name)).resolve()
    if not d.is_relative_to(PROJECTS) or not (d / "forge.json").exists():
        raise ValueError(f"unknown project {name!r} — try list_projects")
    return d


def run_forge(project: str, *args, timeout=900):
    r = subprocess.run([*_FORGE_CMD, *args],
                       cwd=pdir(project), capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def load_cm(d: Path) -> dict:
    f = d / "copy_map.json"
    if not f.exists():
        raise ValueError("no copy_map.json — run inventory first")
    return json.loads(f.read_text(encoding="utf-8"))


def save_cm(d: Path, cm: dict):
    (d / "copy_map.json").write_text(
        json.dumps(cm, indent=1, ensure_ascii=False), encoding="utf-8")


def snapshot(d: Path):
    """Same .history format as the studio — undo interoperates."""
    hist = d / ".history"
    hist.mkdir(exist_ok=True)
    snap = {}
    for f in ("copy_map.json", "forge.json", "project_plan.md"):
        p = d / f
        if p.exists():
            snap[f] = p.read_text(encoding="utf-8")
    (hist / f"{time.time():.6f}.json").write_text(
        json.dumps(snap), encoding="utf-8")
    for old in sorted(hist.glob("*.json"))[:-30]:
        old.unlink()


def keep_undone(d: Path):
    """UNDO THROWS NOTHING AWAY. An agent that could not explain a broken
    page undid seven changes in a row and 94 finished lines were gone,
    recoverable only from the conversation transcript. The state an undo
    replaces is kept in .history/undone/ (last 30), so any run of undos
    can be walked back."""
    und = d / ".history" / "undone"
    und.mkdir(parents=True, exist_ok=True)
    snap = {f: (d / f).read_text(encoding="utf-8")
            for f in ("copy_map.json", "forge.json", "project_plan.md")
            if (d / f).exists()}
    (und / f"{time.time():.6f}.json").write_text(json.dumps(snap),
                                                 encoding="utf-8")
    for old in sorted(und.glob("*.json"))[:-30]:
        old.unlink()


def guard_write(entry: dict, new: str):
    """The rules that keep migrations unbreakable. Returns error or None."""
    if "`" in new or "${" in new:
        return "backtick/${ forbidden (strings land inside JS template literals)"
    mb = entry.get("max_bytes")
    if mb and len(new.encode()) > mb:
        return (f"over CMS byte budget: {len(new.encode())} > {mb} bytes "
                "(em-dash/curly quotes are 3 bytes) — shorten it")
    return None


# AN IMAGE ADDRESS IS CHECKED BEFORE IT GOES IN. A dead one raises no error
# anywhere: the build copies it, the page still has its text, and the
# picture is simply gone (the Progrex hero: a lead photo address that did
# not load left a blank hero and white headline on white). Asked once, with
# the answer cached for the call. Unreachable is not dead — that is a note.
def image_problem(url: str, _seen={}):
    """'' when the address serves an image, else the reason it does not.
    None when it could not be asked (no network) — not a refusal."""
    if not re.match(r"https?://", url or ""):
        return ""
    if url in _seen:
        return _seen[url]
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url.replace("&amp;", "&"), headers={
        "User-Agent": "Mozilla/5.0 (Aethron image check)",
        "Range": "bytes=0-2047", "Accept": "image/*,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].lower()
            head = r.read(16)
        magic = (head[:4] in (b"\x89PNG", b"GIF8", b"RIFF")
                 or head[:3] == b"\xff\xd8\xff" or b"ftyp" in head[:12])
        ok = ctype.startswith("image/") or (
            ctype in ("", "application/octet-stream", "binary/octet-stream")
            and magic)
        why = "" if ok else (f"the address answers with {ctype or 'no type'}, "
                             "not an image")
    except urllib.error.HTTPError as e:
        why = f"the address answers {e.code} — it does not load"
    except Exception:
        why = None
    _seen[url] = why
    return why


# ───────────────────────── tool handlers ─────────────────────────────

def t_list_projects(a):
    PROJECTS.mkdir(parents=True, exist_ok=True)
    out = []
    for d in sorted(PROJECTS.iterdir()):
        if not (d / "forge.json").exists():
            continue
        cfg = json.loads((d / "forge.json").read_text())
        filled = total = 0
        if (d / "copy_map.json").exists():
            cm = json.loads((d / "copy_map.json").read_text(encoding="utf-8"))
            for sec in ("strings", "images", "links"):
                for e in cm.get(sec, []):
                    total += 1
                    filled += bool(e.get("new"))
        out.append(f"- {cfg['name']} [{cfg['platform']}] "
                   f"filled {filled}/{total}, "
                   f"built={'yes' if (d / 'site').exists() else 'no'}, "
                   f"pages={len(cfg.get('pages', []))}")
    return "\n".join(out) or "no projects yet — create_project to start"


def t_create_project(a):
    name = re.sub(r"[^\w-]+", "-", str(a["name"]).strip().lower()).strip("-")
    PROJECTS.mkdir(parents=True, exist_ok=True)
    url = str(a.get("url") or "").strip()
    if url:
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http(s)://")
        r = subprocess.run([*_FORGE_CMD, "init", url,
                            "--name", name], cwd=PROJECTS,
                           capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise ValueError((r.stdout + r.stderr).strip())
        return (r.stdout.strip() + "\nNext: fetch, then inventory.")
    src = Path(str(a.get("source_path") or "")).expanduser().resolve()
    if not src.exists():
        raise ValueError(f"source_path not found: {src} "
                         "(pass url for a live site instead)")
    if src.suffix.lower() == ".zip":
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            zipfile.ZipFile(src).extractall(td)
            r = subprocess.run([*_FORGE_CMD, "init", td,
                                "--name", name], cwd=PROJECTS,
                               capture_output=True, text=True)
    else:
        r = subprocess.run([*_FORGE_CMD, "init", str(src),
                            "--name", name], cwd=PROJECTS,
                           capture_output=True, text=True)
    if r.returncode:
        raise ValueError((r.stdout + r.stderr).strip())
    return (r.stdout.strip() + "\nNext: fetch (framer downloads the "
            "runtime; webflow is instant), then inventory.")


def t_pipeline(cmd):
    def h(a):
        ok, log = run_forge(str(a["project"]), cmd)
        return ("OK" if ok else "FAILED") + "\n" + log
    return h


def t_probe(a):
    argv = ["probe"]
    if a.get("page"):
        argv.append("--page=" + str(a["page"]))
    if a.get("all"):
        argv.append("--all")
    ok, log = run_forge(str(a["project"]), *argv)
    return ("OK" if ok else "PROBLEMS") + "\n" + log


def t_look(a):
    """The one tool that makes a coding agent SIGHTED.

    Every agent in this class writes front-end code and never looks at
    what it drew; the industry's workaround is to hand a human a
    screenshot. This renders what was built, measures it against the
    target, and returns REPAIRS — each naming the words of the element,
    a real CSS selector in the built page, and the change to make.
    """
    import json as _j
    sys.path.insert(0, str(ROOT))
    try:
        import aethron_eye as EYE
    except Exception as e:                       # pragma: no cover
        return f"FAILED the eye is unavailable ({e})"
    widths = a.get("widths") or list(EYE.WIDTHS)
    try:
        if a.get("repair"):
            r = EYE.refine(a["built"], a["reference"],
                           rounds=int(a.get("rounds", 4)),
                           widths=tuple(widths), verbose=False)
            return (f"{r['verdict']} — {r.get('why', '')}\n"
                    f"trajectory: "
                    + " -> ".join(f"{t * 100:.1f}%" for t in r["trail"])
                    + "\n\n" + EYE.brief(r))
        r = EYE.look(a["built"], a["reference"], widths=tuple(widths),
                     verbose=False)
        if a.get("design"):
            r = EYE.with_design(r, a["built"], widths)
    except Exception as e:
        return f"FAILED {type(e).__name__}: {e}"
    return EYE.brief(r, limit=int(a.get("limit", 30)))


def t_build_check(a):
    """The contract. Refuses a build that does not meet what was asked.

    The one tool here that says NO. Everything else reports; this
    decides — and it decides on the brief, the target, the design system
    and the reflow together, so a build cannot pass by being good at one
    of them.
    """
    sys.path.insert(0, str(ROOT))
    try:
        import aethron_build as BLD
        import aethron_eye as EYE
    except Exception as e:                       # pragma: no cover
        return f"FAILED the build contract is unavailable ({e})"
    try:
        r = BLD.check(a["project"], a.get("brief", ""),
                      a.get("reference"),
                      widths=tuple(a.get("widths") or
                                   (390, 768, 1280, 1920)),
                      verbose=False)
    except Exception as e:
        return f"FAILED {type(e).__name__}: {e}"
    head = f"{r['verdict']} — {r.get('why', '')}"
    if r["verdict"] == "ACCEPTED":
        return head + "\n\nEvery check passed. This is safe to hand over."
    return head + "\n\n" + EYE.brief(r, limit=30)


def t_design_review(a):
    """Hold a page to a design system, or read the one it already has."""
    import json as _j
    sys.path.insert(0, str(ROOT))
    try:
        import aethron_design as D
        import aethron_eye as EYE
    except Exception as e:                       # pragma: no cover
        return f"FAILED the design referee is unavailable ({e})"
    try:
        if a.get("system"):
            r = EYE.read_page(a["page"], 1280)
            if r is None:
                return "SKIPPED the page did not render"
            sysm = D.infer(r["items"])
            return _j.dumps({
                "type_scale": sysm["type_scale"],
                "space_base": D.step_scale(sysm["spacing"]),
                "text_colors": ["#%02X%02X%02X" % c
                                for c in sysm["text_colors"]],
                "grounds": ["#%02X%02X%02X" % c
                            for c in sysm["grounds"]]}, indent=1)
        r = D.look(a["page"],
                   widths=tuple(a.get("widths") or (390, 1280)),
                   verbose=False)
    except Exception as e:
        return f"FAILED {type(e).__name__}: {e}"
    return EYE.brief(r, limit=40)


def t_measure_screenshot(a):
    """Hand the agent numbers, so it never has to guess at pixels."""
    import json as _j
    sys.path.insert(0, str(ROOT))
    try:
        import aethron_vision
    except Exception as e:                       # pragma: no cover
        return f"FAILED the measurement pass is unavailable ({e})"
    try:
        if a.get("against"):
            # THE WORK LIST. A percentage says a rebuild is wrong; this
            # says which element, by how much, and what to change. It is
            # the same shape as verify -> heal, for pixels.
            found = aethron_vision.audit(str(a["image"]), str(a["against"]))
            return (_j.dumps(found, indent=1)[:60000] if found
                    else "NO MEASURABLE DIFFERENCE")
        rep = aethron_vision.measure(str(a["image"]))
    except SystemExit as e:
        return f"FAILED {e}"
    except Exception as e:
        return f"FAILED {type(e).__name__}: {e}"
    return _j.dumps(rep, indent=1)[:60000]


def _craft_lines(rep):
    """The website checklist, as the person would tick it."""
    c = rep.get("craft") or []
    if not c:
        return []
    return (["built like a website (%d of %d):" % (sum(1 for x in c if x["ok"]), len(c))]
            + [("  ✓ " if x["ok"] else "  ✗ ") + x["check"] for x in c])


def _tool_note(tool: str, detail: str = ""):
    """Record a live heartbeat so the studio knows a long MCP tool is
    actively making progress and what step it is on."""
    try:
        f = HOME / ".tool_status.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"t": round(time.time(), 1),
                                 "tool": str(tool or ""),
                                 "detail": str(detail or "")[:200]}),
                     encoding="utf-8")
    except Exception:
        pass


def t_screenshot_to_page(a):
    """A screenshot becomes a REAL page: HTML, CSS and SVG, checked.

    1.1.27 handed the owner the screenshot itself pasted in as the page's
    background, with two lines of text on top — 96% "identical" and not
    image-to-code at all. Now Aethron's own model loop writes real
    elements, and the checks refuse any page that shows the screenshot,
    compare a render with it, and test phone widths. When no provider can
    run that loop, the agent writes the page itself under the same rules
    and checks it with grade_page.
    """
    sys.path.insert(0, str(ROOT))
    import aethron_shot2code as S2C
    asked = a.get("outdir")
    out = Path(asked) if asked else page_slot(Path(str(a["image"])).stem)
    site = out / "site.html"
    name_line = ("" if asked else
                 f"aethron-page: {out.name}\nThe page is open in Aethron's preview, "
                 "where the person can look at it, point at anything to change or "
                 "remove it, and see the code. Do not give them the path as the "
                 "answer.\n")
    yourself = (
        "Build the page from the image yourself NOW, as ONE self-contained "
        f"HTML file written to exactly {site}, following these rules:\n"
        + S2C.RULES +
        f"\nThen call grade_page with image={a['image']} and page={site}, fix "
        "every finding it names, and call it again until it says PASS (at most "
        "three rounds). Do not ask the person to confirm first.\n")
    try:
        import aethron_brain as _B
        r = _B.resolve()
    except Exception as e:                       # pragma: no cover
        r = {"ready": False, "why": str(e)}
    if not r.get("ready") or r.get("wire") == "anthropic-cli":
        return ("SKIPPED Aethron's rebuild loop has no provider it can call "
                f"({r.get('why') or 'this provider has no API'}).\n" + yourself
                + name_line)
    import contextlib

    def _s2c_log(*x):
        msg = " ".join(str(v) for v in x)
        print(msg, file=sys.stderr, flush=True)
        _tool_note("screenshot_to_page", msg)

    try:
        # stdout is the MCP channel on stdio; nothing the build prints may land in it.
        with contextlib.redirect_stdout(sys.stderr):
            rep = S2C.build(str(a["image"]), str(out),
                            rounds=int(a.get("rounds", 3)), minutes=12,
                            budget_usd=float(a.get("budget_usd", 0.40)),
                            log=_s2c_log)
    except SystemExit as e:
        return f"FAILED {e}"
    except Exception as e:
        return f"FAILED {type(e).__name__}: {e}"
    if rep.get("verdict") == "NOT ASKED" or not rep.get("page"):
        return (f"NOT ASKED {rep.get('why', '')}\n" + yourself + name_line)
    lines = [f"{rep['verdict']}: the page is real HTML and CSS and looks "
             f"{rep.get('likeness', 0):.0%} like the screenshot "
             f"({len(rep.get('rounds', []))} round(s), "
             f"{rep.get('calls', 0)} model call(s), ${rep.get('usd', 0):.4f})",
             f"page: {site}"]
    el = rep.get("elements") or {}
    lines.append("real elements: " + ", ".join(f"{v} {k}" for k, v in el.items() if v))
    pl = rep.get("placement") or {}
    if pl.get("after"):
        b, f = pl.get("before") or {}, pl["after"]
        lines.append(f"text placement: {b.get('placement', 0):.0%} -> {f['placement']:.0%} "
                     f"({f.get('right', 0)} of {f.get('total', 0)} lines exactly where the "
                     "screenshot has them; type face, size and spacing measured from its pixels)")
    for p in rep.get("pictures") or []:
        lines.append(f"picture {p['slot']} ({p['box'][2]}x{p['box'][3]}, {p['kind']}): "
                     f"{p['how']} — {p['file']}"
                     + ("" if p["how"] != "placeholder" else
                        " (a stand-in in the region's colours; describe the picture in the "
                        "<img alt> and run again to find or make a real one)"))
    lines += [f"note: {n}" for n in rep.get("polish") or []]
    lines += _craft_lines(rep)
    if rep.get("findings"):
        lines.append("still failing:")
        lines += [f"- {x}" for x in rep["findings"]]
        lines.append("You can fix these by editing the file and checking it "
                     "with grade_page.")
    for n in rep.get("notes") or []:
        lines.append(f"note: {n}")
    return "\n".join(lines) + "\n" + name_line


def t_grade_page(a):
    """Check a page built from a screenshot — Aethron decides, not the model."""
    sys.path.insert(0, str(ROOT))
    import aethron_shot2code as S2C
    page = Path(str(a["page"]))
    if not page.suffix and (PAGES / page.name / "site.html").is_file():
        page = PAGES / page.name / "site.html"
    if not page.is_file():
        return f"FAILED no page at {page}"
    finished = ""
    try:
        # what any developer adds without being asked (head, focus and hover
        # states, skip link, alt text, field names) is added here, for pages
        # the agent wrote itself too; the look does not move
        src = page.read_text(encoding="utf-8", errors="replace")
        fin = S2C.finish_site(src)
        try:
            # the composition scales as one above the screenshot's width
            head = Path(str(a["image"])).read_bytes()[:24]
            iw, ih = (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) \
                if head[:8] == b"\x89PNG\r\n\x1a\n" else (0, 0)
        except Exception:
            iw = ih = 0
        if iw:
            fin = S2C.fluid(fin, S2C.css_size(iw, ih)[0])
        if fin != src:
            page.write_text(fin, encoding="utf-8")
            finished = ("note: Aethron added the head tags, focus and hover states, "
                        "skip link and missing names itself, and made it scale with the window")
    except Exception:
        pass
    try:
        rep = S2C.grade(str(a["image"]), page)
    except SystemExit as e:
        return f"FAILED {e}"
    except Exception as e:
        return f"FAILED {type(e).__name__}: {e}"
    if rep.get("verdict") == "SKIPPED":
        return f"SKIPPED {rep.get('why')}"
    out = [f"{rep['verdict']}: looks {rep['likeness']:.0%} like the screenshot "
           f"(colour {rep['colour']:.0%}, structure {rep['structure']:.0%}; "
           f"needs {S2C.LIKENESS_FLOOR:.0%})"]
    out += [f"- FAIL {x}" for x in rep["findings"]]
    out += _craft_lines(rep)
    if finished:
        out.append(finished)
    worst = [r for r in rep.get("regions", []) if r["difference"] > 12][:5]
    if worst:
        out.append("regions that differ most (should be -> is):")
        out += [f"- {r['region']} (x {r['x']}-{r['x'] + r['w']}, y {r['y']}-"
                f"{r['y'] + r['h']}): {r['should_be']} -> {r['is']}" for r in worst]
    out += [f"note: {n}" for n in rep.get("notes") or []]
    return "\n".join(out)


def t_edit_page(a):
    """Change what a rebuilt page says and how it looks — never where.

    You may set text, colour, background, font size/weight/family,
    letter-spacing, corner radius, opacity and hidden. You may NOT set
    left, top, width, height, position, transform or z-index: those were
    measured from the original's pixels and are not yours. An edit that
    names one of them is refused with that reason, so read the refusal
    rather than trying a synonym.
    """
    import json as _j
    sys.path.insert(0, str(ROOT))
    import aethron_edit as AE
    page = Path(str(a["page"]))
    if not page.is_file():
        return f"FAILED no such page: {page}"
    html = page.read_text()
    if a.get("list") or not a.get("edits"):
        return _j.dumps(AE.manifest(html), indent=1)[:60000]
    edits = a["edits"]
    notes = []
    for e in edits if isinstance(edits, list) else []:
        an = e.get("animate") if isinstance(e, dict) else None
        if isinstance(an, dict) and an.get("strength") in AE.ALIVE_TARGETS:
            canvas = AE.manifest(html)["canvas"]
            if canvas.get("w"):
                word = an["strength"]
                an["strength"], tried = AE.tune_alive(html, an.get("style", "drift"), an.get("period", 18),
                                                      word, page.parent, canvas["w"], canvas["h"])
                notes.append(f"MOTION TUNED to '{word}' by measuring: {tried} => strength {an['strength']}")
    out, applied, refused = AE.apply(html, edits)
    dest = Path(a.get("out") or page)
    if applied:
        page_snapshot(dest)
    dest.write_text(out)
    lines = [f"{len(applied)} applied, {len(refused)} refused -> {dest}"]
    lines += [f"REFUSED  {w}" for w in refused]
    lines += notes
    if any("animate" in e for e in applied):
        canvas = AE.manifest(out)["canvas"]
        if canvas.get("w"):
            proof = AE.prove_alive(out, dest.parent, canvas["w"], canvas["h"])
            lines.append("MOTION " + _j.dumps(proof))
    return "\n".join(lines)


def t_adopt_page(a):
    """Stamp measurable ids onto any page, and prove the stamping changed nothing."""
    sys.path.insert(0, str(ROOT))
    import aethron_adopt as AD
    page = Path(str(a["page"]))
    if not page.is_file():
        return f"FAILED no such page: {page}"
    rep = AD.adopt(page, int(a.get("width") or 1414), int(a.get("height") or 900),
                   int(a.get("cap") or AD.MAX_ELEMENTS),
                   write=not a.get("dry_run"), log=lambda *x: None)
    out = []
    AD.report(rep, out.append)
    return "\n".join(out)


def t_change_page(a):
    """Any change, in the user's words — written as code, kept only if MEASURED true."""
    import json as _j
    import shutil as _sh
    import tempfile as _tf
    sys.path.insert(0, str(ROOT))
    import aethron_spec as CH
    page = Path(str(a["page"]))
    if not page.is_file():
        return f"FAILED no such page: {page}"
    budget = float(a.get("budget_usd") or 0.05)

    # ANY PAGE, NOT ONLY THE ONES AETHRON BUILT. The measure reads [data-ae-id]; a page
    # carrying none is unread, not unchangeable. Stamped first, and only if the stamping is
    # PROVEN invisible and proven to have landed on the right elements.
    import aethron_adopt as AD
    import aethron_change as AC
    note = ""
    page = page.resolve()
    if AD.needs_adopting(page.read_text()):
        rep = AD.adopt(page, write=True, log=lambda *x: None)
        if not rep.get("verdict", "").startswith(("ADOPTED", "ALREADY")):
            return (f"NOT ASKED — this page could not be made measurable "
                    f"({rep.get('why')}), so nothing was asked of a model and the page "
                    f"is untouched.")
        note = (f"adopted first: {rep.get('stamped', 0)} element(s) named, "
                f"{rep['proof']['pixels_changed']} pixel(s) changed by the stamping\n")
    # Relative assets resolve against the page's real home, and the folder is SERVED so
    # root-absolute paths mean what they mean to a visitor.
    AC.set_asset_base(page.parent)
    AC.serve_assets(page.parent)

    work = Path(_tf.mkdtemp(prefix="ae-change-"))
    r = CH.build(page.read_text(), str(a["request"]), work, budget_usd=budget)
    _sh.rmtree(work, ignore_errors=True)
    dest = Path(a.get("out") or page)
    if r["verdict"] == "APPLIED":
        page_snapshot(dest)
        dest.write_text(r["html"])
    dest.with_name(dest.stem + ".change.json").write_text(
        _j.dumps({k: v for k, v in r.items() if k != "html"}, indent=1, default=str))
    return note + CH.report(r) + (f"\n-> {dest}" if r["verdict"] == "APPLIED"
                                  else "\npage left unchanged")


def t_convert(a):
    """Port a built migration into a framework project the owner owns."""
    argv = ["convert", "--framework", str(a.get("framework") or "astro")]
    if a.get("pages"):
        argv += ["--pages", ",".join(a["pages"])]
    if a.get("no_build"):
        argv.append("--no-build")
    # A port takes minutes: it renders every page in a real browser,
    # then installs and builds a node project. The default tool timeout
    # would kill it mid-flight and report a failure that never happened.
    ok, log = run_forge(str(a["project"]), *argv, timeout=3600)
    return ("PORT ACCEPTED" if ok else "PORT REFUSED") + "\n" + log


def t_figma_import(a):
    """A Figma design becomes a page the owner owns."""
    argv = ["figma", str(a["url"])]
    if a.get("node"):
        argv += ["--node", str(a["node"])]
    argv += ["--out", str(pdir(str(a["project"])) / "figma")]
    if a.get("grade"):
        argv.append("--grade")
    ok, log = run_forge(str(a["project"]), *argv, timeout=3600)
    return ("IMPORTED" if ok else "PROBLEM") + "\n" + log


def t_get_plan(a):
    f = pdir(str(a["project"])) / "project_plan.md"
    return f.read_text(encoding="utf-8") if f.exists() else "(no plan yet)"


def t_set_plan(a):
    d = pdir(str(a["project"]))
    snapshot(d)
    (d / "project_plan.md").write_text(str(a["plan"]), encoding="utf-8")
    return "plan saved"


def t_get_content(a):
    d = pdir(str(a["project"]))
    cm = load_cm(d)
    section = a.get("section", "strings")
    if section not in ("strings", "images", "links"):
        raise ValueError("section must be strings|images|links")
    entries = cm.get(section, [])
    # a word of an animated heading is not a line: it is written through
    # the heading's whole line (a word changed alone changes it site-wide)
    frag = word_fragments(d, cm) if section == "strings" else {}
    hidden = sum(1 for e in entries if e["old"] in frag)
    entries = [e for e in entries if e["old"] not in frag]
    # A PREVIEW lists only the lines on its chosen pages: tokens are what
    # a lead costs, and the rest of the template is never shown.
    pv_note = ""
    pv = json.loads((d / "forge.json").read_text(encoding="utf-8")).get("preview")
    if pv:
        import aethron_preview as _P
        keep = _P.page_lines(d)
        entries = [e for e in entries if e["old"] in keep]
        pv_note = (f"preview — only the lines on: {', '.join(pv['pages'])}\n")
    if a.get("only_unfilled"):
        entries = [e for e in entries if not e.get("new")]
    flt = [f.strip() for f in str(a.get("filter", "")).lower().split("|")
           if f.strip()]                       # "Contact|Visit" = either
    if flt:
        entries = [e for e in entries if any(f in e["old"].lower() for f in flt)]
    off, lim = int(a.get("offset", 0)), min(int(a.get("limit", 60)), 200)
    page = entries[off:off + lim]
    slim = [{"old": e["old"], "new": e.get("new", ""),
             **({"max_bytes": e["max_bytes"]} if e.get("max_bytes") else {})}
            for e in page]
    # GROUNDING. When the brand's own material has been read (learn_brand),
    # each line worth writing arrives with the passages that answer it —
    # the template's words say what the slot is ABOUT, so they are the
    # question. A slot nothing in the material covers gets no note, rather
    # than an unrelated one the writer would turn into an invented fact.
    head = ""
    if section == "strings" and a.get("brand_notes", True):
        try:
            import aethron_rag
            todo = [e["old"] for e in slim if not e["new"]]
            r = aethron_rag.notes_for(d, todo)
            for e in slim:
                if e["old"] in r["notes"]:
                    e["brand_notes"] = r["notes"][e["old"]]
            got = sum(1 for e in slim if "brand_notes" in e)
            if r["mode"] == "none":
                head = ("no brand material read yet — call learn_brand with "
                        "the brand's site so lines arrive with its real "
                        "facts\n")
            elif got or r["mode"]:
                head = (f"brand_notes = passages from the brand's own "
                        f"material ({r['mode']} search) for {got} line(s). "
                        f"match 'strong' = it answers what the line is about; "
                        f"'loose' = same topic, NOT proof of any fact. Use "
                        f"only facts a note literally states; never invent "
                        f"what the notes do not give"
                        + (f" — note: {r['why']}" if r["why"] else "") + "\n")
        except Exception as ex:                     # grounding is a help,
            head = f"brand notes unavailable: {ex}\n"  # never a blocker
    head = pv_note + head
    if hidden:
        head += (f"{hidden} single words of animated headings are not listed: "
                 "each heading is its own line here — write the whole line\n")
    return Result(f"{section}: showing {off}-{off + len(page)} of {len(entries)} "
                  f"(only_unfilled={bool(a.get('only_unfilled'))})\n"
                  "max_bytes = UTF-8 byte budget the 'new' value MUST fit\n"
                  + head
                  + json.dumps(slim, indent=1, ensure_ascii=False),
                  {"section": section, "offset": off, "total": len(entries),
                   "entries": slim})


def t_learn_brand(a):
    import aethron_rag
    d = pdir(str(a["project"]))
    srcs = a.get("sources") or []
    if isinstance(srcs, str):
        srcs = [srcs]
    if not srcs:
        raise ValueError("give sources: the brand's website and/or files")
    notes = []
    r = aethron_rag.learn(d, srcs, max_pages=int(a.get("max_pages", 25)),
                          log=notes.append)
    pages = sum(x["pages"] for x in r["read"])
    out = (f"read {pages} page(s)/file(s) into the brand pack: "
           f"{r['passages']} passages ({r['new']} new, {r['reused']} unchanged)."
           + (" Meaning-based + keyword search ready."
              if r["meaning_search"] else
              f" KEYWORD SEARCH ONLY — {r['why']}"))
    if notes:
        out += "\n" + "\n".join(notes[:10])
    return out + ("\nget_content now attaches brand_notes to each line; "
                  "search_brand answers anything else.")


def t_search_brand(a):
    import aethron_rag
    d = pdir(str(a["project"]))
    r = aethron_rag.search(d, str(a["query"]), k=min(int(a.get("k", 4)), 8))
    hits = r["results"][0]
    if not hits:
        return ("nothing in the brand material answers that"
                + (f" ({r['why']})" if r["why"] else "")
                + " — do not invent it; ask the person or keep it generic")
    return (f"{r['mode']} search" + (f" ({r['why']})" if r["why"] else "")
            + ". 'strong' answers the question; 'loose' is only on the topic"
            " — if no passage literally states the fact, do not invent it.\n"
            + json.dumps([{"text": h["text"], "from": h["src"],
                           "section": h["heading"],
                           "match": aethron_rag._strength(h, r["mode"])}
                          for h in hits], indent=1, ensure_ascii=False))


def t_set_content_bulk(a):
    d = pdir(str(a["project"]))
    cm = load_cm(d)
    snapshot(d)
    idx = {sec: {e["old"]: e for e in cm.get(sec, [])}
           for sec in ("strings", "images", "links")}
    applied, rejected, refused, unchecked = 0, [], [], []
    frag = word_fragments(d, cm)
    for item in a["entries"]:
        sec = item.get("section", "strings")
        tgt = idx.get(sec, {}).get(item.get("old", ""))
        if tgt is None:
            rejected.append(f"not found in {sec}: {item.get('old','')[:50]!r}")
            refused.append({"old": item.get("old", ""), "reason": f"not found in {sec}"})
            continue
        old, new = item.get("old", ""), item.get("new", "")
        if sec == "strings" and old in frag and new not in ("", old):
            line = sorted(frag[old], key=len)[0]
            rejected.append(f"{old!r} is one word of the animated heading "
                            f"{line[:70]!r} — write that whole line instead "
                            "(a word changed on its own changes it everywhere "
                            "on the site)")
            refused.append({"old": old, "reason": "one word of the animated "
                            f"heading {line!r} — write that whole line"})
            continue
        err = guard_write(tgt, item.get("new", ""))
        if not err and sec == "images" and new not in ("", old):
            why = image_problem(new)
            if why:
                err = f"image not used: {why} ({new[:80]})"
            elif why is None:
                unchecked.append(new)
        if err:
            rejected.append(f"{item.get('old','')[:40]!r}: {err}")
            refused.append({"old": item.get("old", ""), "reason": err})
            continue
        tgt["new"] = item.get("new", "")
        applied += 1
    save_cm(d, cm)
    out = f"applied {applied}, rejected {len(rejected)}"
    if rejected:
        out += "\nREJECTED (fix and resend just these):\n- " + "\n- ".join(rejected[:20])
    if unchecked:
        out += (f"\nNOTE {len(unchecked)} image address(es) could not be "
                "checked (no answer) — export_preview's browser check will "
                "say whether they load")
    if a.get("build", True) and applied:
        ok, log = run_forge(str(a["project"]), "build")
        out += "\nbuild: " + ("OK" if ok else "FAILED\n" + log)
        rep_f = d / "site" / ".forge-report.json"
        if ok and rep_f.exists():
            rep = json.loads(rep_f.read_text(encoding="utf-8"))
            sent = {i.get("old", "") for i in a["entries"]}
            moot = set(rep.get("__moot__", []))
            zero = [o for o in sent if rep.get(o) == 0 and o not in moot]
            risk = [o for o in sent if o in set(rep.get("__at_risk__", []))]
            if zero or risk:
                out += ("\n⚠ BROKEN FILLS (zero-effect or hydration would "
                        "revert them). Run the heal tool, then build:\n- "
                        + "\n- ".join(z[:60] for z in (zero + risk)[:15]))
    return Result(out, {"applied": applied, "rejected": refused})


def t_set_content(a):
    return t_set_content_bulk({"project": a["project"], "build": a.get("build", True),
                               "entries": [{"section": a.get("section", "strings"),
                                            "old": a["old"], "new": a["new"]}]})


# ─────────────────── personalised previews (per lead) ──────────────────

def _slug(v) -> str:
    return re.sub(r"[^\w-]+", "-", str(v).strip().lower()).strip("-")


def t_make_preview(a):
    import aethron_preview as P
    name = _slug(a["name"])
    if not name:
        raise ValueError("give the preview a name (e.g. the lead's slug)")
    tpl = pdir(str(a["template"]))
    base = str(a.get("base") or f"/preview/{name}")
    r = P.make_preview(tpl, PROJECTS / name, a.get("pages") or ["home"],
                       base, str(a.get("notice") or ""), a.get("ribbon"))
    return Result(f"preview project {r['project']!r} made from {tpl.name!r} — pages: "
            f"{', '.join(r['pages'])}; it will live at {r['base']}.\n"
            f"{r['lines']} of the template's {r['of']} lines are on those "
            f"pages; get_content lists only those (text by default).\n"
            "Next: get_content (only_unfilled) -> set_content_bulk "
            "(build=false) -> optional add_block -> export_preview.",
                  {"project": r["project"], "pages": r["pages"],
                   "base": r["base"], "lines": r["lines"], "of": r["of"]})


def t_preview_ribbon(a):
    import aethron_preview as P
    d = pdir(str(a["project"]))
    snapshot(d)
    r = P.set_ribbon(d, a.get("ribbon"))
    return ((f"ribbon set: {r['text']!r} — {r['get_label']} -> {r['get_url']}, "
             f"{r['decline_label']} -> {r['decline_url'] or 'closes the ribbon'}")
            if r else "ribbon removed") + ". Run export_preview to publish."


def t_preview_pages(a):
    import aethron_preview as P
    d = pdir(str(a["project"]))
    cfg = json.loads((d / "forge.json").read_text(encoding="utf-8"))
    have = P.available_pages(cfg)
    if "preview" not in cfg or not a.get("pages"):
        now = [P._route(p) or "home" for p in
               (cfg.get("preview") or {}).get("pages", [])]
        return Result(f"pages this template has: {', '.join(have)}\n"
                      + (f"this preview builds: {', '.join(now)}" if now else
                         "not a preview — make_preview(template=…, pages=[…])"),
                      {"pages": have, "building": now})
    snapshot(d)
    r = P.set_pages(d, a["pages"])
    names = [P._route(p) or "home" for p in r["pages"]]
    return Result(f"this preview now builds: {', '.join(names)} "
                  f"({r['lines']} of {r['of']} lines; fills already written are "
                  "kept)." + (f"\nextras not shown on these pages: "
                             f"{', '.join(r['parked'])}" if r["parked"] else "")
                  + "\nRun export_preview to publish the change.",
                  {"pages": have, "building": names, "parked": r["parked"]})


def t_add_block(a):
    import aethron_preview as P
    d = pdir(str(a["project"]))
    snapshot(d)
    b = P.add_block(d, str(a["kind"]), str(a["anchor"]),
                    str(a.get("position") or "after"), a.get("data") or {},
                    str(a.get("page") or ""), bool(a.get("replace")))
    return (f"added {b['kind']} block {b['id']} {b['position']} the section "
            f"holding {b['anchor']!r} on {', '.join(b['pages'])}. It is "
            "placed when the preview is exported.")


def t_export_preview(a):
    import aethron_preview as P
    d = pdir(str(a["project"]))
    out = Path(str(a.get("out") or (HOME / "previews" / d.name))).expanduser()
    r = P.export_preview(d, out)
    # what the template shows on these pages, so a picture that vanished
    # without an error is caught too (the browser decides, not the agent)
    try:
        before = P.template_pictures(d, r["base"])
    except Exception as e:
        before, note = None, f"  NOTE pictures were not compared with the " \
                             f"template ({str(e)[:120]}) — UNVERIFIED"
    else:
        note = ""
    c = P.check_preview(out, r["base"], pristine=d / "pristine",
                        baseline=before)
    lines = [f"exported {len(r['pages'])} page(s) to {r['out']}",
             f"serve that folder at {r['base']} — the address itself answers "
             f"with {r['home']}; other pages at {r['base']}/<page>.html. "
             "Files must be served as they are (.mjs as text/javascript).",
             f"browser check at {r['base']}: {c['verdict']}"
             + (f" — {c['why']}" if c.get("why") else "")]
    if r.get("unmoved"):
        lines.append(f"  NOTE {r['unmoved']} image address(es) in the CMS "
                     "data had no room for the preview address and still "
                     "point at the site root")
    for page, v in c.get("pages", {}).items():
        lines.append(f"  {'ok  ' if v['ok'] else 'FAIL'} {page}: "
                     f"{v['chars']} chars"
                     + (f"; failed: {', '.join(v['failed_requests'])}"
                        if v["failed_requests"] else "")
                     + (f"; console: {v['console_errors'][0]}"
                        if v["console_errors"] else "")
                     + "".join(f"; IMAGE DID NOT LOAD ({im['w']}x{im['h']}"
                               f"{', the biggest on the page' if im is max(v['dead_images'], key=lambda x: x['w'] * x['h']) else ''}"
                               f"): {im['src'][:100]}"
                               for im in v.get("dead_images", []))
                     + "".join(f"; PICTURE MISSING — the template shows "
                               f"a {im['w']}x{im['h']} "
                               f"{'background image' if im['kind'] == 'bg' else 'picture'}"
                               f" {im['y']}px down the page that this "
                               f"preview does not: {im['src'][:100]}"
                               for im in v.get("missing_images", []))
                     + ("" if v["layer"] else "; preview layer MISSING"))
    if note:
        lines.append(note)
    return Result("\n".join(lines),
                  {"verdict": c["verdict"], "why": c.get("why", ""),
                   "folder": r["out"], "base": r["base"], "home": r["home"],
                   "pages": c.get("pages", {})})


def t_add_style(a):
    d = pdir(str(a["project"]))
    sel = str(a["selector"]).strip()
    if not sel or re.search(r"[{}<]", sel):
        raise ValueError("bad selector (braces/< forbidden; '>' is fine)")
    clean = {p: str(v).strip() for p, v in dict(a["css"]).items()
             if re.fullmatch(r"[a-zA-Z-]+", p) and str(v).strip()
             and not re.search(r"[{}<>;\\]|expression", str(v), re.I)}
    if not clean:
        raise ValueError("no valid css properties survived sanitization")
    cm = load_cm(d)
    snapshot(d)
    for s in cm.setdefault("styles", []):
        if s["selector"] == sel:
            s["css"].update(clean)
            break
    else:
        cm["styles"].append({"selector": sel, "css": clean,
                             "label": str(a.get("label", "via-mcp"))})
    save_cm(d, cm)
    ok, log = run_forge(str(a["project"]), "build")
    return f"style saved for {sel}\nbuild: " + ("OK" if ok else "FAILED\n" + log)


def t_remove_element(a):
    d = pdir(str(a["project"]))
    cfg = json.loads((d / "forge.json").read_text())
    contains = str(a.get("contains") or "").strip()
    if not contains:
        raise ValueError("pass `contains`: a snippet of the TEXT inside "
                         "the element you want gone (see get_content)")
    ancestor = int(a.get("ancestor", 0) or 0)
    occurrence = int(a.get("occurrence", 0) or 0)
    pages = cfg["pages"]
    only = a.get("page")
    scan = [only] if only else pages
    target = found_page = None
    for pg in scan:
        if pg not in pages:
            continue
        html = (d / "pristine" / pg).read_text(encoding="utf-8", errors="ignore")
        desc = _locate_element(html, contains, ancestor, occurrence)
        if desc:
            target, found_page = desc, pg
            break
    if not target:
        raise ValueError(f"no element found containing {contains!r} — try a "
                         "shorter/exact snippet, or name the `page`")
    if target["index"] < 0 or target["tag"] in ("html", "head", "body"):
        raise ValueError(f"{contains!r} only resolves to a page-level/"
                         "un-targetable element — use a more specific "
                         "snippet or a `page`")
    preview = (target["tag"]
               + ("#" + target["id"] if target["id"] else "")
               + ("." + ".".join(target["classes"]) if target["classes"] else "")
               + f"  [{found_page}]  text: {target['label']!r}")
    if a.get("preview_only"):
        return ("WOULD REMOVE: " + preview
                + "\n(call again without preview_only to delete; raise "
                "`ancestor` to grab a bigger wrapper like a whole card)")
    snapshot(d)
    if cfg.get("platform") == "framer":
        if target["id"]:
            sel = "#" + target["id"]
        elif target["classes"]:
            sel = target["tag"] + "".join("." + c for c in target["classes"])
        else:
            raise ValueError("no id/classes to target this element safely "
                             "on Framer — raise `ancestor` to a wrapper")
        page_texts = [(d / "pristine" / p).read_text(encoding="utf-8",
                      errors="ignore") for p in pages]
        audit = hide_selector_audit(sel, page_texts)
        if audit["risky"]:
            raise ValueError(f"refusing {sel}: {audit['why']}. Raise "
                             "`ancestor` to target a more specific wrapper.")
        hs = cfg.setdefault("hide_selectors", [])
        if sel not in hs:
            hs.append(sel)
        (d / "forge.json").write_text(json.dumps(cfg, indent=2))
        mode = f"hidden via {sel} (Framer: baked CSS, hydration-safe)"
    else:
        cm = load_cm(d)
        entry = {"tag": target["tag"], "id": target["id"],
                 "classes": target["classes"], "index": target["index"],
                 "label": target["label"]}
        if target.get("href"):         # class-less link, targeted by href
            entry["href"] = target["href"]
        if not a.get("all_pages"):     # shared nav/footer -> every page
            entry["page"] = found_page
        cm.setdefault("remove", []).append(entry)
        save_cm(d, cm)
        mode = ("physically deleted (Webflow, EVERY page)"
                if a.get("all_pages")
                else "physically deleted (Webflow, page-scoped)")
    if a.get("build", True):
        ok, log = run_forge(str(a["project"]), "build")
        b = "\nbuild: " + ("OK" if ok else "FAILED\n" + log)
    else:
        b = ""
    return f"REMOVED [{mode}]: {preview}{b}"


def t_remove_page(a):
    d = pdir(str(a["project"]))
    cfg = json.loads((d / "forge.json").read_text())
    page = str(a.get("page") or "").strip()
    pages = cfg["pages"]
    routes = cfg.get("routes", {})
    fname = page if page in pages else None
    if not fname:
        for r, f in routes.items():
            if r == page.strip("/") or f == page:
                fname = f
                break
    if not fname or fname not in pages:
        raise ValueError(f"unknown page {page!r}. pages: {pages}")
    if fname == "index.html":
        raise ValueError("can't remove the home page")
    snapshot(d)
    cfg["pages"] = [p for p in pages if p != fname]
    cfg["routes"] = {r: f for r, f in routes.items() if f != fname}
    (d / "forge.json").write_text(json.dumps(cfg, indent=2))
    ok, log = run_forge(str(a["project"]), "build")
    return (f"removed page '{fname}' — links to it now 404 cleanly. Remove "
            "its nav/footer link too with remove_element (contains the link "
            "text).\nbuild: " + ("OK" if ok else "FAILED\n" + log))


def t_replace_image_slots(a):
    d = pdir(str(a["project"]))
    news = [str(u) for u in a["new_urls"] if str(u).strip()]
    if not news:
        raise ValueError("new_urls is empty")
    entries = image_slot_styles(d, str(a["old_url"]), news)
    if not entries:
        raise ValueError("that url wasn't found in any <img>/background "
                         "slot — check it with get_content section=images")
    cm = load_cm(d)
    snapshot(d)
    styles = cm.setdefault("styles", [])
    for e in entries:
        for s in styles:
            if s["selector"] == e["selector"]:
                s["css"].update(e["css"])
                break
        else:
            styles.append(e)
    save_cm(d, cm)
    ok, log = run_forge(str(a["project"]), "build")
    plan = "\n".join(f"  {e['label']} -> {e['selector'][:70]}…"
                     for e in entries[:12])
    return (f"assigned {len(news)} image(s) across {len(entries)} slot(s) "
            f"via per-slot CSS overrides (hydration-proof):\n{plan}\n"
            "build: " + ("OK" if ok else "FAILED\n" + log))


def t_heal(a):
    d = pdir(str(a["project"]))
    snapshot(d)
    ok, log = run_forge(str(a["project"]), "heal")
    return ("OK" if ok else "FAILED") + "\n" + log


def t_undo(a):
    d = pdir(str(a["project"]))
    hist = d / ".history"
    snaps = sorted(hist.glob("*.json")) if hist.exists() else []
    if not snaps:
        raise ValueError("nothing to undo")
    snap = json.loads(snaps[-1].read_text(encoding="utf-8"))
    keep_undone(d)
    for fn, content in snap.items():
        if fn in ("copy_map.json", "forge.json", "project_plan.md"):
            (d / fn).write_text(content, encoding="utf-8")
    snaps[-1].unlink()
    ok, _ = run_forge(str(a["project"]), "build")
    return f"reverted last change ({len(snaps) - 1} more undo(s) available); " \
           f"rebuild {'OK' if ok else 'FAILED'}"


def t_generate_logo(a):
    d = pdir(str(a["project"]))
    args = ["logo", str(a["text"])]
    if a.get("font"):
        args += ["--font", str(a["font"])]
    if a.get("color"):
        args += ["--color", str(a["color"])]
    ok, log = run_forge(str(a["project"]), *args)
    hint = ("\nNEXT: find the old logo image entry (get_content section="
            "images; the logo is usually an .svg/.png whose alt/name "
            "mentions the brand or 'logo') and set_content its 'new' to "
            "the /assets/... path printed above, then build."
            if ok else
            "\nIt listed the template's fonts — call again with font set "
            "to a distinguishing substring (e.g. 'Inter 700').")
    return ("OK" if ok else "CHOOSE A FONT") + "\n" + log + hint


def t_serve_preview(a):
    import socket
    name = str(a["project"])
    d = pdir(name)
    if not (d / "site").exists():
        raise ValueError("run build first")
    if name in PREVIEWS and PREVIEWS[name][1].poll() is None:
        return f"already serving at http://127.0.0.1:{PREVIEWS[name][0]}/"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    p = subprocess.Popen([*_FORGE_CMD, "serve", str(port)],
                         cwd=d, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    PREVIEWS[name] = (port, p)
    time.sleep(0.4)
    return (f"serving at http://127.0.0.1:{port}/ — a real forge serve "
            "(CMS ?range=, .js@ MIME, SPA fallback), what you see is "
            "what ships")


def t_delete_project(a):
    d = pdir(str(a["project"]))
    if not a.get("confirm"):
        raise ValueError("pass confirm=true to delete — this removes the "
                         "pristine original, copy map and built site")
    if str(a["project"]) in PREVIEWS:
        PREVIEWS.pop(str(a["project"]))[1].terminate()
    shutil.rmtree(d)
    return "deleted"


# ───────────────────────── tool schemas ──────────────────────────────

def S(props, req):
    return {"type": "object", "properties": props, "required": req}

P = {"project": {"type": "string", "description": "project name (see list_projects)"}}

def t_list_runs(a):
    import aethron_runlog as RL
    cs = RL.callers()
    if a.get("caller"):
        cs = [c for c in cs if c["caller"] == RL.slug(a["caller"])]
    if not cs:
        return Result("no runs recorded yet — every MCP call is recorded "
                      f"under {RL.runs_dir()}", {"callers": []})
    lines = []
    for c in cs:
        lines.append(c["caller"])
        for r in c["runs"][:int(a.get("limit") or 10)]:
            lines.append(f"  {r['run']}  {r['calls']} call(s), "
                         f"{r['problems']} problem(s), {r['findings']} "
                         f"finding(s)  {', '.join(r['projects'])}")
    return Result("\n".join(lines) + "\nget_run(caller, run) reads one.",
                  {"callers": cs})


def t_get_run(a):
    import aethron_runlog as RL
    r = RL.get_run(str(a["caller"]), str(a.get("run") or ""))
    return Result(RL.timeline(r), r)


def t_list_library(a):
    cards = []
    if LIBRARY.is_dir():
        for f in sorted(LIBRARY.glob("*.json")):
            try:
                c = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            c["id"] = f.stem
            cards.append(c)
    if not cards:
        return ("library is empty — save_to_library captures a project's "
                "design fingerprint as a card")
    return json.dumps(cards, indent=1, ensure_ascii=False)


def t_save_to_library(a):
    d = pdir(str(a["project"]))
    ok, log = run_forge(str(a["project"]), "card")
    if not ok:
        raise ValueError(log)
    card = json.loads((d / "design_card.json").read_text(encoding="utf-8"))
    card["project"] = d.name
    card["saved"] = time.strftime("%Y-%m-%d")
    LIBRARY.mkdir(parents=True, exist_ok=True)
    (LIBRARY / f"{d.name}.json").write_text(
        json.dumps(card, indent=1, ensure_ascii=False), encoding="utf-8")
    return f"saved design card '{d.name}' — {log}"


def t_create_from_library(a):
    lid = re.sub(r"[^\w-]+", "", str(a["id"]))
    f = LIBRARY / f"{lid}.json"
    if not f.exists():
        raise ValueError("unknown library entry — list_library first")
    card = json.loads(f.read_text(encoding="utf-8"))
    # license-clean by construction: re-import from the card's live
    # source URL, or from the owner's own local project — a card alone
    # can never rebuild a template
    if card.get("source_url"):
        return t_create_project({"name": a["name"],
                                 "url": card["source_url"]})
    src = PROJECTS / card.get("project", "_")
    if not (src / "forge.json").exists():
        raise ValueError("card has no source URL and the original project "
                         "is gone — re-import the template you own, then "
                         "save_to_library again")
    import tempfile
    pcfg = json.loads((src / "forge.json").read_text(encoding="utf-8"))
    name = re.sub(r"[^\w-]+", "-", str(a["name"]).strip().lower()).strip("-")
    if not name:
        raise ValueError("give the new project a name")
    if (PROJECTS / name).exists():
        raise ValueError(f"project '{name}' already exists")
    with tempfile.TemporaryDirectory() as td:
        for p in pcfg.get("pages", []):
            pg = src / "pristine" / p
            if pg.is_file():
                shutil.copy(pg, Path(td) / p)
        r = subprocess.run([*_FORGE_CMD, "init", td,
                            "--name", name], cwd=PROJECTS,
                           capture_output=True, text=True)
    if r.returncode:
        raise ValueError((r.stdout + r.stderr).strip())
    return (r.stdout.strip()
            + "\nNext: fetch -> inventory -> set_plan -> fill.")


TOOLS = [
    ("list_projects", "List all migration projects with platform, fill "
     "progress and build state.", S({}, []), t_list_projects),
    ("create_project", "Start a migration from a LIVE URL — a Framer or "
     "Webflow template, or ANY regular website "
     "(scrapes the home page + same-host subpages automatically) or a "
     "local export (.html file, folder of separately-saved pages, or "
     "zip). Detects the platform (framer/webflow), finds the home page, "
     "wires inter-page links. Typical flow after this: fetch -> "
     "inventory -> set_plan -> get_content -> set_content_bulk -> "
     "build -> verify.",
     S({"name": {"type": "string"},
        "url": {"type": "string", "description": "live site URL, e.g. "
                "https://sometemplate.framer.website/ (preferred — no "
                "manual export needed)"},
        "source_path": {"type": "string", "description": "OR absolute "
                        "path to a local export (.html/.zip/folder)"}},
       ["name"]), t_create_project),
    ("fetch", "Download everything the site needs to run on its own. "
     "Framer: JS chunks, CMS binaries, icons (a minute). ANY OTHER SITE "
     "taken from a live URL (Webflow, WordPress, hand-built, Next.js…): its "
     "own stylesheets, scripts and images, then a browser pass that learns "
     "what the pages ask for at runtime — on-demand code and the data its "
     "API returns, kept as editable data (a few minutes). It never copies "
     "private endpoints and names what needs the original server.",
     S(P, ["project"]), t_pipeline("fetch")),
    ("inventory", "Extract every editable string/image/link into the copy "
     "map. Auto-sets the old brand as a forbidden word for verify.",
     S(P, ["project"]), t_pipeline("inventory")),
    ("figma_import",
     "Import a FIGMA DESIGN as a page — a source, like a live Framer or "
     "Webflow URL. NOT a screenshot-to-code guess: Figma's REST API "
     "returns exact geometry, fills, effects and typography, so the "
     "layout is copied, never inferred, and no vision model is "
     "involved. Vectors and images are exported by Figma itself. Pass "
     "grade=true to compare the result against Figma's OWN render, "
     "pixel by pixel, and refuse it if it does not match. Needs a Figma "
     "token (dev) or a connected Figma account (shipped).",
     S({"project": {"type": "string"},
        "url": {"type": "string", "description": "figma.com/design/... URL"},
        "node": {"type": "string",
                 "description": "optional frame id; default the largest frame"},
        "grade": {"type": "boolean",
                  "description": "pixel-compare against Figma's render"}},
       ["project", "url"]), t_figma_import),
    ("convert_framework",
     "Port a BUILT migration into a framework project (astro | next | "
     "vite) that runs with no dependency on Framer or Webflow. 'next' "
     "emits React. The port carries the original's own rendered DOM, "
     "CSS and motion rather than recreating them, then a referee "
     "renders BOTH builds and compares what a reader actually sees — a "
     "port that is not the same site is REFUSED, and that refusal is "
     "the point. Needs a headless browser, plus node/npm to build and "
     "grade (without npm it emits the project and says so instead of "
     "claiming success). Run build + verify first; takes minutes.",
     S({"project": {"type": "string"},
        "framework": {"type": "string", "enum": ["astro", "next", "vite"],
                      "description": "next = React"},
        "pages": {"type": "array", "items": {"type": "string"},
                  "description": "optional subset, default every page"},
        "no_build": {"type": "boolean",
                     "description": "emit only; skips the grade"}},
       ["project"]), t_convert),
    ("get_plan", "Read the project's migration plan (brand, tone, "
     "contacts).", S(P, ["project"]), t_get_plan),
    ("set_plan", "Save the migration plan. Include: brand name, domain, "
     "one-line description, tone, email, socials, what to keep.",
     S({**P, "plan": {"type": "string"}}, ["project", "plan"]), t_set_plan),
    ("get_content", "Read copy-map entries. YOU are the copy model: fill "
     "'new' values per the plan and send them back via set_content_bulk. "
     "Entries with max_bytes are CMS-bound — the new value's UTF-8 byte "
     "length MUST fit (em-dash/curly quotes = 3 bytes). Page through "
     "with offset/limit; only_unfilled=true for remaining work. "
     "NEVER edit the project's files directly (especially pristine/ or "
     "site/) — React hydration reverts hand-edits and verify will flag "
     "the tampering; these tools are the ONLY safe write path. Note: if "
     "one image URL fills several slots (e.g. a repeated dummy logo), "
     "replacing it changes ALL those slots — the design reuses one "
     "asset; per-slot splits are not possible.",
     S({**P, "section": {"type": "string", "enum": ["strings", "images", "links"]},
        "only_unfilled": {"type": "boolean"},
        "filter": {"type": "string", "description": "substring filter on old text"},
        "offset": {"type": "integer"}, "limit": {"type": "integer"}},
       ["project"]), t_get_content),
    ("set_content", "Guarded write of ONE entry's 'new' value (byte "
     "budgets + forbidden chars enforced), then rebuild. Never use "
     "backticks or ${ in values. Empty new = keep original.",
     S({**P, "section": {"type": "string", "enum": ["strings", "images", "links"]},
        "old": {"type": "string", "description": "the entry's exact 'old' value"},
        "new": {"type": "string"}, "build": {"type": "boolean"}},
       ["project", "old", "new"]), t_set_content),
    ("set_content_bulk", "Guarded write of MANY entries at once (the "
     "efficient way to fill a migration), one snapshot, one rebuild. "
     "Returns per-entry rejections to fix and resend. Send AT MOST 40 "
     "entries per call and write each batch as soon as you have read it "
     "— read 40 with get_content, fill them, send them, read the next 40. "
     "One huge answer can take minutes to write and is lost whole if the "
     "provider drops it; a batch that lands is saved.",
     S({**P, "entries": {"type": "array", "items": {"type": "object",
        "properties": {"section": {"type": "string"}, "old": {"type": "string"},
                       "new": {"type": "string"}}, "required": ["old", "new"]}},
        "build": {"type": "boolean"}}, ["project", "entries"]),
     t_set_content_bulk),
    ("add_style", "Bake a CSS override into every page's head "
     "(!important — beats the runtime's inline styles). Use for colors, "
     "fonts, hiding, freezing animation (animation/transition/transform: "
     "none). Selector: any CSS selector; '>' allowed.",
     S({**P, "selector": {"type": "string"},
        "css": {"type": "object", "description": "{property: value}"},
        "label": {"type": "string"}}, ["project", "selector", "css"]),
     t_add_style),
    ("build", "Regenerate site/ from pristine + copy map (all layers: "
     "HTML, JS chunks, CMS binaries — byte-locked, hydration-safe).",
     S(P, ["project"]), t_pipeline("build")),
    ("learn_brand", "Read the BRAND's own material — its live website "
     "(same-site pages, up to max_pages) and/or .md/.txt/.html files — "
     "into this project's brand pack, so every line you write comes with "
     "the brand's real facts. Call it once, early, whenever the person "
     "gives a brand URL or documents. Re-running refreshes it cheaply.",
     S({**P, "sources": {"type": "array", "items": {"type": "string"},
                         "description": "brand URLs and/or file paths"},
        "max_pages": {"type": "integer"}}, ["project", "sources"]),
     t_learn_brand),
    ("search_brand", "Ask the brand pack a question (services, clients, "
     "case studies, contact details, policies, tone). Returns passages "
     "with where they came from — or says nothing covers it, in which "
     "case do not invent the fact.",
     S({**P, "query": {"type": "string"}, "k": {"type": "integer"}},
       ["project", "query"]), t_search_brand),
    ("verify", "Machine checks: forbidden-word leftovers, missing chunks, "
     "telemetry refs, CMS size locks, local images. Run before shipping; "
     "FAILED output tells you exactly what to fix.",
     S(P, ["project"]), t_pipeline("verify")),
    ("probe", "RUNTIME check — the one thing file scans cannot do: "
     "loads every built page in a headless browser and reports what "
     "actually happens. Catches pages that ship every asset and still "
     "render blank, content wiped by hydration, requests the CODE makes "
     "that 404 (not just the ones in the markup), and console errors. "
     "Run it after verify, and always when the owner says the site "
     "looks broken but the checks pass. If no browser is installed it "
     "reports SKIPPED — that means UNVERIFIED, not fine.",
     S({**P, "page": {"type": "string", "description": "one page, e.g. "
        "index.html (default: home + 5 more)"},
        "all": {"type": "boolean", "description": "probe every page"}},
       ["project"]), t_probe),
    ("look", "LOOK AT WHAT YOU BUILT. Renders your page and measures it "
     "against the target, then returns REPAIRS — each one naming the "
     "words of the element, a real CSS selector in YOUR page, and the "
     "change to make. Call it after every UI change; it is the only way "
     "to know whether the code you just wrote produced the design you "
     "were asked for. It judges at FOUR WIDTHS by default (390/768/"
     "1280/1920), because agent-written UI overwhelmingly breaks "
     "narrow and testing one width is how a primary button ends up off "
     "the edge of a phone. The verdict is a CHECKLIST, not a "
     "percentage: a page that has lost its whole navigation can still "
     "score 95% by area, so elements are counted present, placed and "
     "sized — or not. With repair:true it also FIXES what is pure "
     "arithmetic (type scale, colour), trying one change at a time and "
     "keeping only what measures better, so it cannot make your page "
     "worse.",
     {"type": "object", "properties": {
        "built": {"type": "string", "description":
                  "what you built: a URL (your dev server), an .html "
                  "file, or a project directory"},
        "reference": {"type": "string", "description":
                      "the target: a URL or a screenshot"},
        "widths": {"type": "array", "items": {"type": "integer"},
                   "description": "viewport widths to judge at "
                                  "(default 390,768,1280,1920)"},
        "repair": {"type": "boolean", "description":
                   "also apply the repairs that are arithmetic, keeping "
                   "only changes that measure better"},
        "rounds": {"type": "integer", "description":
                   "max repair rounds (default 4)"},
        "design": {"type": "boolean", "description":
                   "also hold the page to a design system — type "
                   "scale, spacing grid, WCAG AA contrast, alignment "
                   "and tap targets"}},
      "required": ["built", "reference"]}, t_look),
    ("build_check", "THE CONTRACT — ask whether what you built is "
     "actually finished, and be told NO if it is not. Checks four "
     "things together, so a build cannot pass by being good at one: "
     "(1) THE BRIEF — the words and the COUNTS the user asked for, so "
     "'three tiers' is verified as three repeated structures on the "
     "rendered page; (2) the TARGET, if a screenshot or URL was given; "
     "(3) the DESIGN SYSTEM — contrast, type scale, spacing, tap "
     "targets; (4) REFLOW at 390/768/1280/1920. Returns ACCEPTED or "
     "REFUSED with the blocking faults, each naming a selector in your "
     "own page. Run it before you tell the user you are done — it is "
     "the difference between handing over work and handing over a "
     "guess.",
     {"type": "object", "properties": {
        "project": {"type": "string", "description":
                    "your project directory, or an .html file"},
        "brief": {"type": "string", "description":
                  "what the user asked for, in their words — quoted "
                  "copy and counts in it become hard requirements"},
        "reference": {"type": "string", "description":
                      "OPTIONAL target to match: a URL or screenshot"},
        "widths": {"type": "array", "items": {"type": "integer"},
                   "description": "viewport widths to judge at"}},
      "required": ["project"]}, t_build_check),
    ("design_review", "REVIEW A PAGE AS A DESIGNER WOULD, with no "
     "reference needed. Answers the question the rest of this toolkit "
     "cannot: not 'does it match the mock' but 'is this good'. Measures "
     "the type scale the page actually renders (not the tokens it "
     "declares), the spacing grid, WCAG AA contrast against what is "
     "REALLY behind each piece of text, edges that miss alignment by a "
     "few pixels, and tap targets below the 44px Apple and Google both "
     "call the minimum. Use it on anything you generated from a "
     "description rather than from a design. With system:true it "
     "instead REPORTS the design system a page is using, which is how "
     "you make new work match an existing site without anyone writing "
     "the tokens down.",
     {"type": "object", "properties": {
        "page": {"type": "string", "description":
                 "a URL, an .html file, or a project directory"},
        "widths": {"type": "array", "items": {"type": "integer"},
                   "description": "viewport widths (default 390,1280)"},
        "system": {"type": "boolean", "description":
                   "report the design system in use instead of auditing"}},
      "required": ["page"]}, t_design_review),
    ("measure_screenshot", "MEASURE a screenshot before writing any code "
     "from it. Returns the exact background and palette, gradients, "
     "content bands and their gaps, the column grid, solid regions with "
     "fill and measured corner radius, and text rows with colour and "
     "height. USE THIS INSTEAD OF READING VALUES OFF THE IMAGE: asked "
     "for a font size that breaks the surrounding pattern, vision "
     "models are right 7.89% of the time, and a confident wrong number "
     "is worse than none. Decide what things MEAN; take every number "
     "from here. A still contains no animation, no hover state and no "
     "other breakpoint — the report says so, and for motion you need a "
     "screen recording or the live URL.",
     {"type": "object", "properties": {
        "image": {"type": "string", "description":
                  "path to a PNG/JPG screenshot (a pasted image is "
                  "saved under uploads/ and its path given to you)"},
        "against": {"type": "string", "description":
                    "OPTIONAL screenshot of your rebuild. With this, the "
                    "tool measures BOTH and returns every property that "
                    "disagrees — element, expected number, built number, "
                    "and the change to make. Screenshot your rebuild and "
                    "pass it here after every attempt; that loop is what "
                    "takes a rebuild from roughly right to identical."}},
      "required": ["image"]}, t_measure_screenshot),
    ("screenshot_to_page", "SCREENSHOT -> a REAL web page: Aethron writes it "
     "as HTML, CSS and SVG (real nav links, buttons, headings; gradients, "
     "glows and shapes drawn in code), renders it beside the screenshot, "
     "and corrects it over up to three rounds. A page that shows the "
     "screenshot itself or a crop of it bigger than an icon is refused, and "
     "phone widths are checked. Use this whenever the person attaches an "
     "image and asks for a page. If it answers SKIPPED or NOT ASKED, build "
     "the page yourself under the rules it gives and check it with "
     "grade_page — do not ask first.",
     {"type": "object", "properties": {
        "image": {"type": "string", "description":
                  "path to a PNG/JPG screenshot"},
        "outdir": {"type": "string", "description":
                   "OPTIONAL where to write the page. Leave it out: by "
                   "default the page opens in Aethron's preview."},
        "rounds": {"type": "integer", "description":
                   "OPTIONAL build + correction rounds (default 3)"}},
      "required": ["image"]}, t_screenshot_to_page),
    ("grade_page", "CHECK a page built from a screenshot: refuses it if it "
     "shows the screenshot (or a crop bigger than an icon) as a picture, "
     "renders it at the screenshot's size and says how much it looks like "
     "it and which regions differ (should-be and is colours), checks it "
     "has real links/buttons, and that nothing hangs off the edge at 390 "
     "and 768px. Fix what it names and call it again until PASS.",
     {"type": "object", "properties": {
        "image": {"type": "string", "description": "the screenshot"},
        "page": {"type": "string", "description":
                 "path to the page's HTML file, or the page's name"}},
      "required": ["image", "page"]}, t_grade_page),
    ("edit_page", "Change what a rebuilt page SAYS and how it LOOKS. "
     "Call with no `edits` to get the element list (ids, kinds, words, "
     "colours) — then name elements by id. Allowed: text, color, "
     "background, font_size, font_weight, font_family, letter_spacing, "
     "border_radius, opacity, hidden (a button takes both its label's "
     "and its box's; a text box's text is its placeholder). The page's "
     "ground also takes {\"id\": \"bg\", \"animate\": {\"style\": "
     "\"drift|breathe|drift+breathe|off\", \"period\": 4-120, "
     "\"strength\": 0.05-1 or \"subtle|visible|strong\" (reached by "
     "rendering and measuring)}} to make its fitted background move — the "
     "result is rendered to prove frame 0 is still the design and that it "
     "moves. REFUSED: left, top, width, "
     "height, position, transform, z-index — those were measured from "
     "the original's pixels and a model reading a size off an image is "
     "right about 8% of the time. For ANY other change (a size, a "
     "position, a new behaviour, a recoloured moving background) use "
     "change_page, which measures the result.",
     {"type": "object", "properties": {
        "page": {"type": "string", "description": "path to page.html"},
        "edits": {"type": "array", "description":
                  "[{\"id\": \"t04\", \"set\": {\"text\": \"Start free\"}}]",
                  "items": {"type": "object"}},
        "out": {"type": "string", "description":
                "OPTIONAL write here instead of in place"}},
      "required": ["page"]}, t_edit_page),
    ("adopt_page", "Make a page Aethron did NOT build measurable, so every other tool "
     "here can read and change it. The measure reads data-ae-id; a migration, a user's "
     "own site or any hand-written page carries none, so it was unreadable rather than "
     "hard to change. This stamps stable ids into the source and REFUSES unless both are "
     "proven: the picture did not change (every pixel compared) and every id landed on "
     "the element it was picked for — shuffling ids onto the wrong elements changes zero "
     "pixels, so the pixel proof alone cannot see it. change_page does this on its own; "
     "call this directly to inspect what a page offers before changing it.",
     {"type": "object", "properties": {
        "page": {"type": "string", "description": "path to the .html file"},
        "width": {"type": "number", "description": "OPTIONAL viewport width (default 1414)"},
        "height": {"type": "number", "description": "OPTIONAL viewport height (default 900)"},
        "cap": {"type": "number", "description":
                "OPTIONAL most elements to name (default 400); the measure records every "
                "computed style of each, so this bounds the cost"},
        "dry_run": {"type": "boolean", "description":
                    "OPTIONAL measure and report without writing the file"}},
      "required": ["page"]}, t_adopt_page),
    ("change_page", "ANY change to a rebuilt page, in the user's own words — "
     "size, position, words, colours, new or removed elements, scripts such "
     "as a typing animation, a recoloured moving background, pop-ups and "
     "dropdowns on hover or click, colours that change on hover. Anything "
     "that reacts is USED like a person uses it — hovered, the pointer "
     "carried onto what opened, clicked, Escape, a click outside — and the "
     "trigger must keep doing what it did. The model "
     "writes code AND claims what will be true; Aethron renders the result "
     "and MEASURES every claim (pixels, boxes, colours, words at two moments "
     "in time), holds the claims to the request (reduce means a measured "
     "decrease, 'a little' is bounded, a named colour must be measured), and "
     "requires everything not named to measure exactly as before. Failures "
     "go back to the model with the measured numbers; if it still fails the "
     "page is left unchanged and the reasons are returned.",
     {"type": "object", "properties": {
        "page": {"type": "string", "description": "path to page.html"},
        "request": {"type": "string", "description":
                    "what the user asked for, in their words"},
        "budget_usd": {"type": "number", "description":
                       "OPTIONAL spend cap for this request (default 0.15)"},
        "out": {"type": "string", "description":
                "OPTIONAL write here instead of in place"}},
      "required": ["page", "request"]}, t_change_page),
    ("generate_backend", "Generate backend/app.py (content API + site "
     "server; copy_map is the database) + AGENT_GUIDE.md for handoff.",
     S(P, ["project"]), t_pipeline("backend")),
    ("localize_assets", "FULL OWNERSHIP: download every remote CDN "
     "asset (Webflow css/js/images, Framer images, fonts) into the "
     "project under brand-free hashed names; the next build serves "
     "them locally — zero dependency on the template platform's CDN. "
     "Run build after this.", S(P, ["project"]), t_pipeline("localize")),
    ("replace_image_slots", "THE FIX for shared assets: when ONE image "
     "url fills MANY slots (dummy logo tickers, repeated cards), "
     "set_content would change them all identically. This assigns "
     "DIFFERENT images per slot via per-element CSS overrides "
     "(content:url on each slot's DOM path — hydration-proof, baked "
     "into the shipped code). new_urls are distributed round-robin "
     "across every slot the old url fills (upload files first via the "
     "backend /api/media or drop them in assets/, or use "
     "generate_logo's output paths).",
     S({**P, "old_url": {"type": "string", "description": "the shared "
        "asset url (from get_content section=images)"},
        "new_urls": {"type": "array", "items": {"type": "string"},
                     "description": "replacement urls/paths, assigned "
                     "round-robin, e.g. [\"/assets/a.svg\", \"/assets/b.svg\"]"}},
       ["project", "old_url", "new_urls"]), t_replace_image_slots),
    ("generate_logo", "Brand logos are IMAGES — text replacement never "
     "touches them (verify's NOTE reminds you). This renders the new "
     "brand name as an SVG wordmark in the TEMPLATE'S OWN font into "
     "assets/. Call once without font to list available fonts, then "
     "again with a font substring. Needs pip3 install fonttools brotli.",
     S({**P, "text": {"type": "string", "description": "the brand name"},
        "font": {"type": "string", "description": "font substring, e.g. "
                 "'Inter 700' (omit first call to see the list)"},
        "color": {"type": "string", "description": "hex color, e.g. #ffffff"}},
       ["project", "text"]), t_generate_logo),
    ("serve_preview", "Serve the built site locally with the exact "
     "protocols production needs; returns the URL.",
     S(P, ["project"]), t_serve_preview),
    ("remove_element", "Remove an element from the shipped code — the "
     "agent-facing version of the visual editor's Remove (closes the MCP's "
     "biggest parity gap). Give `contains`: a snippet of the TEXT inside "
     "the element (from get_content), and optionally climb to a wrapper "
     "with `ancestor` (0 = the tightest element holding the text, 1 = its "
     "parent, 2 = grandparent … — use this to grab a whole CARD/SECTION, "
     "not just the words). WEBFLOW/static: the element is physically "
     "deleted, scoped to the page it's on. FRAMER: a permanent, blast-"
     "radius-guarded hide rule is baked in (React re-creates deleted DOM); "
     "over-broad selectors are refused — raise `ancestor` for a safer "
     "target. Call with preview_only=true first to see exactly what would "
     "go. Snapshotted (undo covers it).",
     S({**P, "contains": {"type": "string", "description": "text inside "
        "the element to remove"},
        "ancestor": {"type": "integer", "description": "climb N levels "
                     "from the text to a wrapper (whole card/section)"},
        "occurrence": {"type": "integer", "description": "if the text "
                       "appears in several spots (e.g. a nav AND a footer "
                       "link), which one: 0=first, 1=second, …"},
        "page": {"type": "string", "description": "restrict to one page file"},
        "all_pages": {"type": "boolean", "description": "for SHARED chrome "
                      "(nav/footer links) — remove it from every page, not "
                      "just the one it was found on"},
        "preview_only": {"type": "boolean", "description": "just report "
                         "what would be removed, don't delete"}},
       ["project", "contains"]), t_remove_element),
    ("remove_page", "Remove a whole page from the site (e.g. a Careers "
     "page you don't need yet). Takes the page file (careers.html) or its "
     "route (careers). Links to it then 404 cleanly; remove its nav link "
     "with remove_element. Can't remove the home page. Snapshotted.",
     S({**P, "page": {"type": "string"}}, ["project", "page"]),
     t_remove_page),
    ("heal", "SELF-HEAL broken edits. Run whenever build's report shows "
     "zero-effect entries or __at_risk__ ones (replaced in pages but "
     "the chunks still spell the old text = hydration reverts it). "
     "Deterministic, never a guess. TEXT: flex upgrade (whitespace-"
     "tolerant matching), source-casing adoption, nearest-source-string "
     "adoption (>=85% similar, CMS byte budgets enforced). IMAGES: finds "
     "every real source URL sharing the asset id (all srcset size/format "
     "variants, any filename encoding) and points them at your new image "
     "— fixes mangled Webflow picks and partial srcset swaps, drops junk "
     "entries. Whatever it can't fix safely comes back as STUCK with the "
     "exact reason — fix the entry via set_content, never hand-edit. "
     "Snapshotted (undo covers it). Run build afterwards.",
     S(P, ["project"]), t_heal),
    ("make_preview", "PERSONALISED PREVIEW FOR ONE LEAD, cheaply. Copies a "
     "template project that was already prepared ONCE (create_project, "
     "fetch, inventory) into a new project for this lead, keeping only the "
     "chosen pages, to be served at `base` (default /preview/<name>). "
     "Takes about a second and costs no tokens; get_content then lists only "
     "the chosen pages' lines. YOU choose the pages — any the template "
     "has, named like a person would (home, about, services, work, "
     "pricing…) or [\"all\"]; call preview_pages on the template first to "
     "see them, and again on the preview to add or drop pages later. "
     "Pages not built: a click on a link to one shows a preview-mode "
     "notice (your words via `notice`).",
     S({"template": {"type": "string", "description": "the prepared "
                     "template project to copy"},
        "name": {"type": "string", "description": "the preview's name, "
                 "e.g. the lead's slug: live2living-furniture-d2cad9"},
        "pages": {"type": "array", "items": {"type": "string"},
                  "description": "pages to include, e.g. [\"home\", "
                  "\"about\", \"contact\"]; default home only"},
        "base": {"type": "string", "description": "the address it will be "
                 "served at; default /preview/<name>"},
        "ribbon": {"type": "object", "description": "optional bar across the bottom "
                   "of every page: {text, get_url, decline_url?, get_label?, "
                   "decline_label?}. Buttons default to 'Get this website' / "
                   "'Not interested'; without decline_url that button just "
                   "closes the ribbon. Addresses must be https:// or /path."},
        "notice": {"type": "string", "description": "what a click on a page "
                   "that is not included says"}},
       ["template", "name"]), t_make_preview),
    ("preview_ribbon", "Set, change or remove (omit `ribbon`) the bar "
     "across the bottom of a preview's pages — your text plus 'Get this "
     "website' and 'Not interested' buttons pointing where you say. "
     "export_preview again to publish.",
     S({**P, "ribbon": {"type": "object", "description": "optional bar across the bottom "
                   "of every page: {text, get_url, decline_url?, get_label?, "
                   "decline_label?}. Buttons default to 'Get this website' / "
                   "'Not interested'; without decline_url that button just "
                   "closes the ribbon. Addresses must be https:// or /path."}}, ["project"]), t_preview_ribbon),
    ("preview_pages", "Which pages a preview builds — your choice, any "
     "time. On a template project: lists every page it has. On a preview: "
     "with no `pages`, lists what it builds and what it could; with "
     "`pages` (names or [\"all\"]), REPLACES the set. Fills already "
     "written are kept; export_preview again to publish.",
     S({**P, "pages": {"type": "array", "items": {"type": "string"}}},
       ["project"]), t_preview_pages),
    ("add_block", "Add something the template does not have to a preview: "
     "a map (data.query = address or place name), reviews (data.items = "
     "[{author, text, stars}] — the REAL reviews you found; nothing is "
     "invented here; optional rating/count/source) or your own section "
     "(data.html, no scripts). It is placed before/after the section "
     "holding `anchor` (words that appear on the page) and takes that "
     "section's fonts and colours. `page` limits it to one page.",
     S({**P, "kind": {"type": "string", "enum": ["map", "reviews", "html"]},
        "anchor": {"type": "string"},
        "position": {"type": "string", "enum": ["before", "after"]},
        "page": {"type": "string"},
        "replace": {"type": "boolean", "description": "swap out an "
                    "existing extra of the same kind on that page; without "
                    "it a second one is refused"},
        "data": {"type": "object"}},
       ["project", "kind", "anchor"]), t_add_block),
    ("export_preview", "Build a preview and write its chosen pages, ready to "
     "be served at its base address, then open every page AT THAT ADDRESS "
     "in a headless browser and report what failed — including any picture "
     "the template shows on those pages that the preview no longer does "
     "(PICTURE MISSING: something hid or removed it; restore it, or swap it "
     "for another picture instead). Returns the folder to publish (default <Aethron data>/previews/<name>) and the check.",
     S({**P, "out": {"type": "string", "description": "folder to write "
                     "to; default <Aethron data>/previews/<name>"}},
       ["project"]), t_export_preview),
    ("undo", "Revert the LAST content/plan/config change and rebuild. Only "
     "when the person asks for it. Each call discards one finished change, "
     "so NEVER undo repeatedly to find what broke a page: a fill cannot "
     "break a page unless the engine is wrong — report the probe's FAIL "
     "lines to the person, verbatim, and stop.", S(P, ["project"]), t_undo),
    ("delete_project", "Delete a project entirely (needs confirm=true).",
     S({**P, "confirm": {"type": "boolean"}}, ["project"]),
     t_delete_project),
    ("list_runs", "RUN RECORDS: every agent that drives Aethron over MCP is "
     "recorded — which tool, with what, what Aethron answered, how long, "
     "and what failed or went missing. Lists callers and their runs, "
     "newest first, with problem counts. Secrets are never recorded.",
     S({"caller": {"type": "string", "description": "only this caller"},
        "limit": {"type": "integer", "description": "runs per caller "
                  "(default 10)"}}, []), t_list_runs),
    ("get_run", "One run as a timeline: each call with its status (ok / "
     "partial / FAIL / ERR), time taken and findings (refused fills, "
     "pages that FAIL, images that did not load). structuredContent "
     "carries every call in full.",
     S({"caller": {"type": "string"},
        "run": {"type": "string", "description": "run id from list_runs, "
                "or 0 = newest (default), 1 = the one before…"}},
       ["caller"]), t_get_run),
    ("list_library", "Read the DESIGN LIBRARY: one card per saved "
     "template — palette, fonts, section structure, motion features "
     "(hover variants, split text, rotators, marquee), scale, source "
     "URL. YOU are the matcher: when the owner describes a new project, "
     "read the cards and rank the best-fitting designs yourself, then "
     "create_from_library. Cards never contain template files.",
     S({}, []), t_list_library),
    ("save_to_library", "Extract a project's design fingerprint "
     "(forge card) and save it as a library card. Do this after a "
     "successful migration so the design is findable for future "
     "projects.", S(P, ["project"]), t_save_to_library),
    ("create_from_library", "Start a NEW project from a library card: "
     "re-imports from the card's source URL (or the owner's local "
     "project when it was an upload) — license-clean, the card alone "
     "can't rebuild a template. Then: fetch -> inventory -> set_plan "
     "-> fill.", S({"id": {"type": "string"}, "name": {"type": "string"}},
                   ["id", "name"]), t_create_from_library),
]

TOOL_DEFS = [{"name": n, "description": d, "inputSchema": s}
             for n, d, s, _ in TOOLS]
HANDLERS = {n: h for n, d, s, h in TOOLS}


# ───────────────────────── JSON-RPC over stdio ───────────────────────

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


# The MCP is part of Studio. Only the stdio server an outside coding agent
# starts is checked (the HTTP server is run by us, behind its own tokens).
# The answer is kept for a few minutes so a migration's many calls do not
# each ask the account.
GATED = False
_ACCESS = {"at": 0.0, "v": (True, "")}


def _access():
    if time.time() - _ACCESS["at"] > 300:
        import aethron_access
        _ACCESS["v"] = aethron_access.mcp_access()
        _ACCESS["at"] = time.time()
    return _ACCESS["v"]


def dispatch(msg, run=None):
    """One JSON-RPC message in, its reply out (None for a notification).
    The same answers whether the message came over stdio or HTTP. `run`
    is the caller's run record (aethron_runlog.Session): every tool call
    is written to it, with what was asked and what Aethron answered."""
    mid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize" and run is not None:
        run.hello(params)
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion",
                                                 "2024-11-05"),
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "aethron", "version": "1.0"}})
    if method == "tools/list":
        return ok({"tools": TOOL_DEFS})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        t0 = time.time()
        if GATED:
            allowed, why = _access()
            if not allowed:
                return ok({"content": [{"type": "text", "text": why}],
                           "isError": True})
        try:
            if name not in HANDLERS:
                raise ValueError(f"unknown tool {name!r}")
            text = HANDLERS[name](args)
            res = {"content": [{"type": "text", "text": str(text)}]}
            if isinstance(text, Result):
                res["structuredContent"] = text.data
            if run is not None and name not in ("list_runs", "get_run"):
                run.call(name, args, str(text), False,
                         (time.time() - t0) * 1000,
                         getattr(text, "data", None))
            return ok(res)
        except Exception as e:
            if run is not None:
                run.call(name, args, f"ERROR: {e}", True,
                         (time.time() - t0) * 1000)
            return ok({"content": [{"type": "text", "text": f"ERROR: {e}"}],
                       "isError": True})
    if method == "ping":
        return ok({})
    if method == "resources/list":      # politeness for clients
        return ok({"resources": []})
    if method == "resources/templates/list":
        return ok({"resourceTemplates": []})
    if method == "prompts/list":
        return ok({"prompts": []})
    if mid is not None:                 # unknown request (not a notification)
        return {"jsonrpc": "2.0", "id": mid, "error":
                {"code": -32601, "message": f"method not found: {method}"}}
    return None


def main():
    global GATED
    GATED = True
    import aethron_runlog as RL
    run = RL.Session("stdio")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        reply = dispatch(msg, run)
        if reply is not None:
            send(reply)


# ─────────────────────── the same tools over HTTP ────────────────────
# For an agent that is NOT on this computer (a lead agent on Render,
# driven from a phone). MCP "Streamable HTTP": every message is a POST to
# /mcp and is answered with plain JSON. Nothing is reachable without the
# token: these tools create, change and delete projects, and fetch any
# address they are given. /preview/<name>/… serves finished previews
# (public by nature — they are meant to be shown to leads) and nothing
# else; /health answers for the host's health check.

TOKEN_ENV = "AETHRON_MCP_TOKEN"
# One token per caller, so a run record can say WHO called without
# trusting what an agent says it is: AETHRON_MCP_TOKENS="keeper=<secret>,
# other=<secret>". AETHRON_MCP_TOKEN alone is the caller "default".
TOKENS_ENV = "AETHRON_MCP_TOKENS"


def http_tokens() -> dict:
    """{secret: label}. Every secret 24+ characters, or the server will
    not start."""
    out = {}
    one = os.environ.get(TOKEN_ENV, "").strip()
    if one:
        out[one] = "default"
    for part in os.environ.get(TOKENS_ENV, "").split(","):
        if "=" in part:
            label, sec = part.split("=", 1)
            if sec.strip():
                out[sec.strip()] = re.sub(r"[^\w.-]+", "-",
                                          label.strip()) or "caller"
    return out


def serve_http(port: int, host: str = "0.0.0.0"):
    import hmac
    import http.server
    import mimetypes
    import threading
    import aethron_runlog as RL
    tokens = http_tokens()
    if not tokens or any(len(t) < 24 for t in tokens):
        sys.exit(f"refusing to start: set {TOKEN_ENV} (or {TOKENS_ENV}="
                 "label=secret,…) to long random secrets (24+ characters "
                 "each) — anyone holding one controls this Aethron")
    mimetypes.add_type("text/javascript", ".mjs")
    previews = (HOME / "previews").resolve()
    runs, runs_lock = {}, threading.Lock()   # session id -> run record

    def run_for(label, sid):
        with runs_lock:
            if len(runs) > 500:              # forget the oldest
                for k in list(runs)[:100]:
                    runs.pop(k, None)
            key = (label, sid)
            if key not in runs:
                runs[key] = RL.Session("http", caller=label, sid=sid)
            return runs[key]

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):     # never log the token
            sys.stderr.write("%s %s\n" % (self.command,
                                          self.path.split("?")[0]))

        def _send(self, code, body=b"", ctype="application/json",
                  extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authed(self):
            """The caller's label, or None."""
            got = self.headers.get("Authorization", "")
            hit = None
            for sec, label in tokens.items():   # compare all: constant time
                if hmac.compare_digest(got.encode(),
                                       f"Bearer {sec}".encode()):
                    hit = label
            return hit

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") != "/mcp":
                return self._send(404, b'{"error":"not found"}')
            label = self._authed()
            if not label:
                return self._send(401, b'{"error":"bad or missing token"}',
                                  extra={"WWW-Authenticate": "Bearer"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"null")
            except ValueError:
                return self._send(400, json.dumps({
                    "jsonrpc": "2.0", "id": None, "error": {
                        "code": -32700, "message": "parse error"}}).encode())
            msgs = body if isinstance(body, list) else [body]
            sid = re.sub(r"[^\w-]", "", self.headers.get(
                "Mcp-Session-Id", ""))[:64]
            fresh = any(isinstance(m, dict) and m.get("method") ==
                        "initialize" for m in msgs)
            if fresh or not sid:
                # a new connection gets its own run; a client that never
                # sends the id back still gets one run per initialize
                sid = secrets.token_hex(8) if fresh else "no-session"
            run = run_for(label, sid)
            replies = [r for r in (dispatch(m, run) for m in msgs
                                   if isinstance(m, dict)) if r is not None]
            extra = {"Mcp-Session-Id": sid} if fresh else None
            if not replies:
                return self._send(202, extra=extra)
            out = replies if isinstance(body, list) else replies[0]
            self._send(200, json.dumps(out).encode(), extra=extra)

        def do_GET(self):
            path = urllib.parse.unquote(self.path.split("?")[0].split("#")[0])
            if path == "/health":
                return self._send(200, b'{"ok":true}')
            if path.rstrip("/") == "/mcp":     # no server-initiated stream
                return self._send(405, b"", extra={"Allow": "POST"})
            m = re.fullmatch(r"/preview/([\w-]+)(/.*)?", path)
            if not m:
                return self._send(404, b"not found", "text/plain")
            root = (previews / m.group(1)).resolve()
            rel = (m.group(2) or "/").lstrip("/") or "index.html"
            f = (root / rel).resolve()
            if f.is_dir():
                f = f / "index.html"
            if root.parent != previews or not str(f).startswith(
                    str(root) + os.sep) or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or \
                "application/octet-stream"
            self._send(200, f.read_bytes(), ctype,
                       {"Cache-Control": "no-store"})
        do_HEAD = do_GET

    srv = http.server.ThreadingHTTPServer((host, port), H)
    print(f"Aethron MCP over HTTP on {host}:{port} — POST /mcp with "
          f"'Authorization: Bearer <{TOKEN_ENV}>'; previews at "
          f"/preview/<name>; data in {HOME}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if "--http" in sys.argv:
        k = sys.argv.index("--http")
        nxt = sys.argv[k + 1] if len(sys.argv) > k + 1 else ""
        serve_http(int(nxt if nxt.isdigit() else
                       os.environ.get("PORT", "8765")))
    else:
        main()
