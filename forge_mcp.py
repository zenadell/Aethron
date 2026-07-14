#!/usr/bin/env python3
"""Aethron MCP server — drive template migrations from ANY AI
agent (Claude Code, Antigravity, Cursor, anything MCP-speaking).

Zero dependencies: stdio JSON-RPC 2.0, newline-delimited.

Register (Claude Code):   claude mcp add forge -- python3 forge_mcp.py
or via .mcp.json:         {"mcpServers": {"template-forge":
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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORGE = ROOT / "forge.py"
PROJECTS = ROOT / "projects"
LIBRARY = ROOT / "library"   # design cards: fingerprints, never files
PREVIEWS = {}

sys.path.insert(0, str(ROOT))
from forge import image_slot_styles  # noqa: E402


# ───────────────────────── shared plumbing ───────────────────────────

def pdir(name: str) -> Path:
    d = (PROJECTS / str(name)).resolve()
    if not d.is_relative_to(PROJECTS) or not (d / "forge.json").exists():
        raise ValueError(f"unknown project {name!r} — try list_projects")
    return d


def run_forge(project: str, *args, timeout=900):
    r = subprocess.run([sys.executable, str(FORGE), *args],
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


def guard_write(entry: dict, new: str):
    """The rules that keep migrations unbreakable. Returns error or None."""
    if "`" in new or "${" in new:
        return "backtick/${ forbidden (strings land inside JS template literals)"
    mb = entry.get("max_bytes")
    if mb and len(new.encode()) > mb:
        return (f"over CMS byte budget: {len(new.encode())} > {mb} bytes "
                "(em-dash/curly quotes are 3 bytes) — shorten it")
    return None


# ───────────────────────── tool handlers ─────────────────────────────

def t_list_projects(a):
    PROJECTS.mkdir(exist_ok=True)
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
    PROJECTS.mkdir(exist_ok=True)
    url = str(a.get("url") or "").strip()
    if url:
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http(s)://")
        r = subprocess.run([sys.executable, str(FORGE), "init", url,
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
            r = subprocess.run([sys.executable, str(FORGE), "init", td,
                                "--name", name], cwd=PROJECTS,
                               capture_output=True, text=True)
    else:
        r = subprocess.run([sys.executable, str(FORGE), "init", str(src),
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
    if a.get("only_unfilled"):
        entries = [e for e in entries if not e.get("new")]
    flt = str(a.get("filter", "")).lower()
    if flt:
        entries = [e for e in entries if flt in e["old"].lower()]
    off, lim = int(a.get("offset", 0)), min(int(a.get("limit", 60)), 200)
    page = entries[off:off + lim]
    slim = [{"old": e["old"], "new": e.get("new", ""),
             **({"max_bytes": e["max_bytes"]} if e.get("max_bytes") else {})}
            for e in page]
    return (f"{section}: showing {off}-{off + len(page)} of {len(entries)} "
            f"(only_unfilled={bool(a.get('only_unfilled'))})\n"
            "max_bytes = UTF-8 byte budget the 'new' value MUST fit\n"
            + json.dumps(slim, indent=1, ensure_ascii=False))


def t_set_content_bulk(a):
    d = pdir(str(a["project"]))
    cm = load_cm(d)
    snapshot(d)
    idx = {sec: {e["old"]: e for e in cm.get(sec, [])}
           for sec in ("strings", "images", "links")}
    applied, rejected = 0, []
    for item in a["entries"]:
        sec = item.get("section", "strings")
        tgt = idx.get(sec, {}).get(item.get("old", ""))
        if tgt is None:
            rejected.append(f"not found in {sec}: {item.get('old','')[:50]!r}")
            continue
        err = guard_write(tgt, item.get("new", ""))
        if err:
            rejected.append(f"{item.get('old','')[:40]!r}: {err}")
            continue
        tgt["new"] = item.get("new", "")
        applied += 1
    save_cm(d, cm)
    out = f"applied {applied}, rejected {len(rejected)}"
    if rejected:
        out += "\nREJECTED (fix and resend just these):\n- " + "\n- ".join(rejected[:20])
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
    return out


def t_set_content(a):
    return t_set_content_bulk({"project": a["project"], "build": a.get("build", True),
                               "entries": [{"section": a.get("section", "strings"),
                                            "old": a["old"], "new": a["new"]}]})


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
    p = subprocess.Popen([sys.executable, str(FORGE), "serve", str(port)],
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
    LIBRARY.mkdir(exist_ok=True)
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
        r = subprocess.run([sys.executable, str(FORGE), "init", td,
                            "--name", name], cwd=PROJECTS,
                           capture_output=True, text=True)
    if r.returncode:
        raise ValueError((r.stdout + r.stderr).strip())
    return (r.stdout.strip()
            + "\nNext: fetch -> inventory -> set_plan -> fill.")


TOOLS = [
    ("list_projects", "List all migration projects with platform, fill "
     "progress and build state.", S({}, []), t_list_projects),
    ("create_project", "Start a migration from a LIVE template URL "
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
    ("fetch", "Download+localize the template runtime (Framer: JS chunks, "
     "CMS binaries, icons — takes a minute; Webflow: instant no-op).",
     S(P, ["project"]), t_pipeline("fetch")),
    ("inventory", "Extract every editable string/image/link into the copy "
     "map. Auto-sets the old brand as a forbidden word for verify.",
     S(P, ["project"]), t_pipeline("inventory")),
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
     "Returns per-entry rejections to fix and resend.",
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
    ("verify", "Machine checks: forbidden-word leftovers, missing chunks, "
     "telemetry refs, CMS size locks, local images. Run before shipping; "
     "FAILED output tells you exactly what to fix.",
     S(P, ["project"]), t_pipeline("verify")),
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
    ("undo", "Revert the last content/plan/config change (snapshots are "
     "taken before every write) and rebuild.", S(P, ["project"]), t_undo),
    ("delete_project", "Delete a project entirely (needs confirm=true).",
     S({**P, "confirm": {"type": "boolean"}}, ["project"]),
     t_delete_project),
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


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "template-forge", "version": "1.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"tools": TOOL_DEFS}})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                if name not in HANDLERS:
                    raise ValueError(f"unknown tool {name!r}")
                text = HANDLERS[name](args)
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}]}})
            except Exception as e:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"ERROR: {e}"}],
                    "isError": True}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "resources/list":      # politeness for clients
            send({"jsonrpc": "2.0", "id": mid, "result": {"resources": []}})
        elif method == "resources/templates/list":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"resourceTemplates": []}})
        elif method == "prompts/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}})
        elif mid is not None:   # unknown request (not a notification)
            send({"jsonrpc": "2.0", "id": mid, "error":
                  {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
