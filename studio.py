#!/usr/bin/env python3
"""Aethron Studio — a local AI IDE for template migrations.

One command:  python3 studio.py [port]   (default 8899, binds 127.0.0.1)

Wraps forge.py (which stays the single source of truth for every
dangerous mechanical step) with the full workflow in a browser:

  1. drop in a Framer/Webflow export (.html or .zip)  -> init
  2. one-click fetch / inventory / build / verify      (forge.py subprocesses)
  3. write your PROJECT PLAN (brand, voice, links, images)
  4. "Fill with AI" — any model: Anthropic, DeepSeek, Gemini, OpenAI,
     Ollama (OpenAI-compatible), or fully manual copy/paste — the model
     only ever fills copy_map.json, exactly like the PLAYBOOK says
  5. visual editors for strings (live CMS byte budgets), images
     (upload straight into assets/), links, and the wordmark generator
  6. live preview (a real `forge.py serve` per project, so both Framer
     protocols are exact) and a ship-ready zip of site/

Zero dependencies, same as forge.py. Projects live in ./projects/.
The studio never edits site/ or pristine/ itself — every change flows
through copy_map.json + build, keeping all forge invariants intact.
"""
import atexit
import base64
import os
import io
import json
import mimetypes
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORGE = ROOT / "forge.py"

# Desktop build support:
# - FROZEN (PyInstaller): sys.executable is the app binary, not python.
#   The app doubles as the engine — `Aethron --forge <cmd> …` dispatches
#   into forge (see desktop.py) — so subprocess calls stay subprocess
#   calls (crash isolation, parallel previews) with no python needed.
# - AETHRON_HOME: user data (projects/, library/) lives in a writable
#   data dir (the bundle is read-only); set by desktop.py, defaults to
#   the repo dir for normal dev use.
FROZEN = bool(getattr(sys, "frozen", False))
# resolved for the same reason forge_mcp resolves it: the project-dir
# guard compares resolved paths, and a symlinked home (macOS /var ->
# /private/var) would make every project look like it is outside.
HOME = Path(os.environ.get("AETHRON_HOME", ROOT)).resolve()
PROJECTS = HOME / "projects"
LIBRARY = HOME / "library"   # design cards: fingerprints, never files
PAGES = HOME / "pages"       # a screenshot, measured into a page, changed in words


def design_pages():
    """Every page a screenshot became, newest first."""
    out = []
    if not PAGES.exists():
        return out
    for p in sorted(PAGES.glob("*/site.html"), key=lambda f: -f.stat().st_mtime):
        out.append({"name": p.parent.name, "when": int(p.stat().st_mtime),
                    "bytes": p.stat().st_size})
    return out
WORKSPACES = HOME / "workspaces"   # code workspaces (not migrations)

# The coding layer is optional at import time: a broken/absent
# aethron_code must never take the studio down — the IDE just reports
# why it is unavailable.
try:
    import aethron_brain as brain
except Exception:                                      # pragma: no cover
    brain = None
try:
    import aethron_code as codelayer
except Exception as _e:                                # pragma: no cover
    codelayer = None
    CODE_IMPORT_ERROR = str(_e)
else:
    CODE_IMPORT_ERROR = ""
CODE_SESSIONS = {}    # workspace path -> CodeSession
# The rectangle the native shell should back with real Liquid Glass,
# or {} for none. desktop.py reads this on the main thread; nothing
# in this file touches AppKit.
GLASS_RECT = {}
SKIP_DIRS = {".git", "node_modules", ".history", "__pycache__", ".venv",
             "dist", "build", ".next", ".DS_Store"}


def fs_tree(root: Path, cap=4000) -> list:
    """Flat, sorted file list — the IDE builds the tree client-side.
    Heavy generated dirs are skipped so a Next.js workspace doesn't
    ship 30k node_modules entries to the browser."""
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.is_dir():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append({"path": str(rel), "size": size,
                    "locked": is_locked(root, p)})
        if len(out) >= cap:
            break
    return out


def safe_path(root: Path, rel: str) -> Path:
    f = (root / (rel or "")).resolve()
    if not f.is_relative_to(root.resolve()) or not f.is_file():
        raise FileNotFoundError("no such file in this workspace")
    return f


def is_locked(root: Path, f: Path) -> bool:
    """site/ and pristine/ in a template project are read-only by law."""
    if not (root / "forge.json").exists():
        return False
    try:
        first = f.resolve().relative_to(root.resolve()).parts[0]
    except Exception:
        return False
    return first in LOCKED_DIRS

# Files the IDE refuses to write, per the forge invariants: generated
# output and the sealed original. Editing them looks like it works and
# then silently reverts on hydration (or fails the pristine seal).
LOCKED_DIRS = ("site", "pristine")


def forge_argv(*args):
    """argv prefix that runs the forge engine, dev or frozen."""
    if FROZEN:
        return [sys.executable, "--forge", *map(str, args)]
    return [sys.executable, str(FORGE), *map(str, args)]

sys.path.insert(0, str(ROOT))
from forge import _flex_pat, hide_selector_audit  # shared matchers  # noqa: E402
import secrets  # noqa: E402
import aethron_cloud as cloud  # noqa: E402
try:
    import aethron_update as updater
except Exception:
    updater = None

# server-side login sessions (cloud mode only): opaque cookie -> user.
# The Supabase access token stays here, never in the browser.
#
# PERSISTED, because an app that forgets you every time you close it is
# not behaving like an app. Two things were wrong and BOTH had to change:
# this dict lived only in memory, and the cookie carried no Max-Age, so
# it was a session cookie that died with the window regardless. Now: a
# 0600 file beside the projects, a 30-day sliding window refreshed on
# use, and expiry enforced on load. Logging out still clears it.
SESSIONS = {}
SESS_FILE = HOME / ".sessions.json"
SESSION_TTL = 30 * 86400          # 30 days since last use
_SESS_SAVED = 0.0                 # last flush, so use does not hit disk


def _restore_sessions_once():
    """Load persisted logins when this MODULE loads, not from __main__.

    The desktop app never executes studio.py's __main__ — desktop.py
    imports this module and constructs the server itself. So the startup
    hook lived in dead code, SESSIONS stayed empty, and every launch
    asked for a login while a perfectly good session sat in
    .sessions.json. The trace said it plainly:
        cookie_sent=True matches_session=False sessions=0
    The cookie was always coming back; nobody had loaded the other half.
    """
    try:
        _sessions_load()
    except Exception:
        pass


def _clog(msg):
    """Cookie-chain tracing. Windowed bundles have no stdout, and three
    rounds were lost to reasoning about this instead of reading it."""
    try:
        import datetime
        with open(HOME / "aethron.log", "a") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S} COOKIE {msg}\n")
    except Exception:
        pass


def _sessions_load():
    """Restore sessions, dropping anything past its sliding window."""
    try:
        raw = json.loads(SESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    now, kept = time.time(), 0
    for tok, rec in (raw or {}).items():
        if not isinstance(rec, dict) or "user" not in rec:
            continue
        if now - float(rec.get("seen") or 0) > SESSION_TTL:
            continue
        SESSIONS[tok] = rec["user"]
        SESSIONS[tok]["_seen"] = float(rec.get("seen") or now)
        kept += 1
    if kept:
        print(f"restored {kept} login session(s)")


def _sessions_save():
    """Write atomically, owner-readable only — these hold access tokens."""
    global _SESS_SAVED
    try:
        SESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            t: {"user": {k: v for k, v in u.items() if k != "_seen"},
                "seen": u.get("_seen") or time.time()}
            for t, u in SESSIONS.items()}), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(SESS_FILE)
        _SESS_SAVED = time.time()
    except Exception:
        pass


def _session_touch(user):
    """Slide the window. Disk is touched at most hourly, not per request."""
    user["_seen"] = time.time()
    if time.time() - _SESS_SAVED > 3600:
        _sessions_save()

# in-flight direct-Google logins: state -> {verifier, cookie|None, error|None}.
# The OAuth completes in the SYSTEM browser (different cookie jar than a
# native app window), so the callback stores the minted session cookie
# here and the app window collects it by polling.
PENDING = {}

JOBS = {}       # job id -> {"done": bool, "ok": bool|None, "log": str}
PREVIEWS = {}   # project -> (port, Popen)
RUN_CMDS = {"fetch", "inventory", "build", "verify", "probe",
            "localize"}

# Framework port. Separate from RUN_CMDS because it carries an argument
# and runs for MINUTES (it renders every page in a real browser, then
# installs and builds a node project), so it needs its own longer leash.
CONVERT_FRAMEWORKS = ("astro", "next", "vite",
                      "react", "nextjs", "next.js")


# Set by desktop.py: brings the app window to the front. Google sign-in
# finishes in the SYSTEM browser, so without this the user is left staring
# at a browser tab while the app quietly logs in behind it.
FOCUS_APP = None


def focus_app():
    if not FOCUS_APP:
        return
    try:
        threading.Thread(target=FOCUS_APP, daemon=True).start()
    except Exception:
        pass


def mint_session(auth_result):
    """auth_result (login/signup/session_from_google) -> (cookie, err).
    Resolves the user, enforces billing entitlement, and registers a
    server-side session. err is (message, code) on failure."""
    user = cloud.user_of(auth_result)
    if not user.get("id"):
        return None, ("could not resolve user", 401)
    user["plan"] = cloud.plan_of(user["token"], user["id"])
    if not cloud.entitled(user["plan"]):
        cloud.track("login_blocked", token=user["token"], step="billing")
        return None, ("Your free beta access has ended — upgrade to Pro "
                      "to keep using Aethron.", 402)
    tok = secrets.token_urlsafe(24)
    user["_seen"] = time.time()
    SESSIONS[tok] = user
    _sessions_save()
    return tok, None


_restore_sessions_once()

# ───────────────────────── forge subprocess plumbing ─────────────────

def start_job(argv, cwd) -> str:
    jid = f"{time.time():.6f}"
    JOBS[jid] = {"done": False, "ok": None, "log": "$ " + " ".join(argv[1:]) + "\n"}

    def run():
        try:
            p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                JOBS[jid]["log"] += line
            p.wait()
            JOBS[jid].update(done=True, ok=p.returncode == 0)
        except Exception as e:
            JOBS[jid]["log"] += f"studio error: {e}\n"
            JOBS[jid].update(done=True, ok=False)

    threading.Thread(target=run, daemon=True).start()
    return jid


def start_fn_job(fn, header="") -> str:
    """Same job contract as start_job, for work that runs in-process and
    streams its own events (the agentic healer)."""
    jid = f"{time.time():.6f}"
    JOBS[jid] = {"done": False, "ok": None, "log": header}

    def append(text):
        JOBS[jid]["log"] += text.rstrip() + "\n"

    def run():
        try:
            ok = fn(append)
            JOBS[jid].update(done=True, ok=bool(ok))
        except Exception as e:
            append(f"studio error: {e}")
            JOBS[jid].update(done=True, ok=False)

    threading.Thread(target=run, daemon=True).start()
    return jid


def snapshot(d: Path):
    """Before every mutation: snapshot the owner-editable state so any
    mistake is one Undo away. Keeps the last 30."""
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


def undo_count(d: Path) -> int:
    hist = d / ".history"
    return len(list(hist.glob("*.json"))) if hist.exists() else 0


def project_info(d: Path) -> dict:
    cfg = json.loads((d / "forge.json").read_text())
    chunks = d / "pristine" / "chunks"
    filled = total = 0
    cm_f = d / "copy_map.json"
    if cm_f.exists():
        cm = json.loads(cm_f.read_text(encoding="utf-8"))
        for sec in ("strings", "images", "links"):
            for e in cm.get(sec, []):
                total += 1
                filled += bool(e.get("new"))
    return {
        "name": cfg["name"], "platform": cfg["platform"],
        "public_base": cfg.get("public_base", "/assets"),
        "fetched": cfg["platform"] != "framer"
                   or (chunks.exists() and any(chunks.glob("*.mjs"))),
        "inventoried": cm_f.exists(),
        "built": (d / "site").exists(),
        "has_plan": (d / "project_plan.md").exists(),
        "filled": filled, "total": total,
        "undo": undo_count(d),
        "pages": cfg.get("pages", []),
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def preview_start(name: str) -> int:
    if name in PREVIEWS and PREVIEWS[name][1].poll() is None:
        return PREVIEWS[name][0]
    port = free_port()
    p = subprocess.Popen(forge_argv("serve", str(port)),
                         cwd=PROJECTS / name,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PREVIEWS[name] = (port, p)
    time.sleep(0.4)
    if p.poll() is not None:
        raise RuntimeError("preview server exited — run build first")
    return port


@atexit.register
def _kill_previews():
    for _, p in PREVIEWS.values():
        if p.poll() is None:
            p.terminate()
    for s in list(CODE_SESSIONS.values()):
        try:
            s.close()
        except Exception:
            pass


# ───────────────────────── AI copy-fill ──────────────────────────────
# The model's only job, verbatim from PLAYBOOK Part 2 Step 4. Batched so
# small models (DeepSeek flash, 7B local) never blow their output limit.

# REBRAND MEANS REBRAND. The previous rules made only brand tokens
# mandatory and said "leave new as '' for anything that should keep the
# original text" — so a model did the literal minimum: it swapped the
# company name and left a finance product's every sentence in place. The
# owner saw a site with their logo on someone else's business and was
# right to call it a relabel.
#
# The default is now inverted. Every string is rewritten unless it is
# genuinely generic, and the model is told what "generic" means so the
# exception cannot swallow the rule.
PROMPT_RULES = """Rules:
1. REWRITE EVERYTHING. This is a rebrand, not a find-and-replace. Every
   heading, paragraph, feature name, testimonial, blog title, changelog
   line, FAQ, dashboard label and micro-copy must describe the OWNER'S
   business. A visitor reading the finished site must not be able to tell
   what the template originally sold.
2. The ONLY entries you may leave as "" are ones that are already true of
   any business: navigation words (Home, Blog, Contact), UI verbs (Submit,
   Close, Next), country, city and browser names, dates, statistics and
   numbers, and legal boilerplate that names no industry. If a string
   mentions the original industry, its products, its jargon or its example
   data, it MUST be rewritten. When in doubt, rewrite it.
2b. SHORT STRINGS ARE NOT EXEMPT. Most of what a visitor reads is short —
   pricing tier names, feature labels, section eyebrows, card titles, job
   titles, chart legends. A two-word label naming the template's industry
   ("Predict your income", "Smart Expense Sorting", "AI-Powered Finance")
   is exactly as wrong as a paragraph and must be rewritten into the
   owner's equivalent. Never answer "" merely because a string is short or
   because you cannot see where it appears; write the label this owner's
   site would use in that slot.
2c. NEVER answer "" for a string containing the template's own brand name.
   That name must not survive anywhere, in any form — including possessive
   forms, tier names ("Brand Pro"), page titles, email addresses and URL
   slugs. In a slug or an address use one lowercase word, not the full
   name with spaces.
3. Map the original's domain onto the owner's, concept for concept, and
   keep it consistent across the whole site. Do not translate word by word:
   "invoice" -> "migration" produces sentences that parse and mean nothing.
   Read what the sentence is FOR, then write that sentence for this owner.
4. If max_bytes is set, the UTF-8 byte length of "new" must be <= it.
   Em-dashes and curly quotes are 3 bytes each. When unsure, write shorter.
   A shorter true sentence beats a longer one that is rejected. When
   max_bytes cannot hold the full brand name, use the SHORT BRAND given in
   the plan — never invent an abbreviation or an acronym of your own.
5. Never use backticks or ${ in any "new" value.
6. Keep the same shape as the original (a 3-word button stays ~3 words; a
   one-line subtitle stays one line). Shape is layout; words are yours.
7. Links: retarget emails, phone numbers and socials per the plan; leave
   internal anchors (#...) alone unless the plan says otherwise.
8. Images: leave "new" empty unless the plan supplies a replacement URL.
Return the complete JSON you were given, nothing else."""


AI_EDIT_PROMPT = """You are editing ONE element of a live website on the \
owner's instruction. Reply with ONLY a JSON object, nothing else:
{"text": "<replacement text>" or null,
 "css": {"property": "value", ...} or null,
 "explain": "<one short line describing what you did>"}

Rules:
- "text" replaces the element's text. %(budget)s Never use backticks \
or ${ in text. Keep roughly the same length/shape unless asked.
- "css" is applied to the element as !important overrides. Plain \
values only (colors as hex/rgb, sizes with units). Choose colors that \
harmonize with the page palette given below.
- Change ONLY what the instruction asks. If the instruction is about \
color/spacing/size, set css and leave text null. If about wording, \
set text and leave css null.

Owner instruction: %(instruction)s

Element context:
%(context)s

Project plan (brand voice/details):
%(plan)s
"""

PLAN_POLISH_PROMPT = """The owner of a website-template migration tool \
wrote a rough, incomplete migration plan. Rewrite it into the exact \
format below. Rules: never drop or contradict anything the owner wrote; \
infer sensible specifics for gaps (domain = brand.com, email = \
hello@<domain>, social handles from the brand name) — those are \
placeholders the owner can edit; if the BRAND NAME itself is missing, \
write <FILL: brand name> so the owner sees it. Return ONLY the plan.

Format:
Brand: <name>
Domain: <domain>
What it is: <one or two sentences>
Tone: <short guidance>
Replace every mention of the template's old brand (any casing, with or
without the (R) symbol) with <write the ACTUAL brand name here, not a
placeholder>. Keep all numbers, stats and pricing the same.
Email: <email>
Socials: <links>
Keep internal anchors (#...) unchanged.
<each extra owner instruction, restated clearly on its own line>

Owner's rough plan:
"""

MATCH_PROMPT = """You match a site owner's project idea against a \
library of saved template DESIGN CARDS (fingerprints: palette, fonts, \
section structure, motion features, scale — extracted from real \
templates). Rank the best-fitting cards for this project. Judge by: \
purpose/industry fit (title, description, sections), tone implied by \
the palette and fonts, structure (pages, sections), and motion \
features the plan calls for. Return ONLY a JSON array of the top 3 \
(fewer if the library is small), best first:
[{"id": "<card id>", "score": <0-100>, "reason": "<one plain sentence \
the owner will read>"}]

OWNER'S PROJECT:
%(plan)s

LIBRARY CARDS:
%(cards)s
"""


def build_prompt(plan: str, batch: dict) -> str:
    return (f"You are rebranding a website template. PROJECT PLAN from "
            f"the owner:\n\n{plan.strip()}\n\nFill the \"new\" field of "
            f"each entry in this JSON per the plan. {PROMPT_RULES}\n\n"
            + json.dumps(batch, indent=1, ensure_ascii=False))


def make_batches(cm: dict, size=40):
    flat = [(sec, e) for sec in ("strings", "images", "links")
            for e in cm.get(sec, []) if not e.get("new")]
    batches = []
    for i in range(0, len(flat), size):
        b = {}
        for sec, e in flat[i:i + size]:
            b.setdefault(sec, []).append(e)
        batches.append(b)
    return batches


def call_model(st: dict, prompt: str) -> str:
    """ONE KEY FOR EVERYTHING: whatever provider is configured in the AI
    settings answers here too. Anything the caller passes explicitly
    still wins, so an ad-hoc run can use a different model."""
    if brain is not None:
        override = {k: v for k, v in (st or {}).items()
                    if k in ("provider", "api_key", "model", "base_url") and v}
        try:
            return brain.text_call(prompt, override or None)
        except ValueError:
            raise
        except Exception:
            if not (st or {}).get("api_key"):
                raise
    if st["provider"] == "anthropic":
        url = (st.get("base_url") or "https://api.anthropic.com").rstrip("/") \
            + "/v1/messages"
        body = {"model": st["model"], "max_tokens": 16000,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": st["api_key"],
                   "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        pick = lambda r: r["content"][0]["text"]
    else:  # every other provider speaks OpenAI chat/completions
        url = st["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": st["model"],
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"Authorization": "Bearer " + st.get("api_key", ""),
                   "Content-Type": "application/json"}
        pick = lambda r: r["choices"][0]["message"]["content"]
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        return pick(json.loads(r.read()))


def extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("no JSON object in model output")
    return json.loads(text[s:e + 1])


def merge_fill(proj: Path, filled: dict):
    """Apply model output to copy_map.json with the forge guardrails.
    Rejected entries are reported, never silently applied."""
    cm_f = proj / "copy_map.json"
    cm = json.loads(cm_f.read_text(encoding="utf-8"))
    applied, errors = 0, []
    for sec in ("strings", "images", "links"):
        index = {e["old"]: e for e in cm.get(sec, [])}
        for e in filled.get(sec, []):
            tgt = index.get(e.get("old", ""))
            new = e.get("new") or ""
            if not tgt or not new or new == tgt.get("new"):
                continue
            if "`" in new or "${" in new:
                errors.append(f"forbidden chars in: {new[:50]!r}")
                continue
            mb = tgt.get("max_bytes")
            if mb and len(new.encode()) > mb:
                errors.append(f"over budget ({len(new.encode())}>{mb}B): "
                              f"{tgt['old'][:40]!r} -> {new[:40]!r}")
                continue
            tgt["new"] = new
            applied += 1
    cm_f.write_text(json.dumps(cm, indent=1, ensure_ascii=False),
                    encoding="utf-8")
    return applied, errors


def start_ai_job(name: str, st: dict) -> str:
    jid = f"{time.time():.6f}"
    JOBS[jid] = {"done": False, "ok": None, "log": ""}

    def log(s):
        JOBS[jid]["log"] += s + "\n"

    def run():
        try:
            proj = PROJECTS / name
            plan_f = proj / "project_plan.md"
            if not plan_f.exists() or not plan_f.read_text().strip():
                raise RuntimeError("write the project plan first")
            plan = plan_f.read_text(encoding="utf-8")
            cm = json.loads((proj / "copy_map.json").read_text(encoding="utf-8"))
            batches = make_batches(cm)
            if not batches:
                raise RuntimeError("nothing left to fill")
            log(f"{st['provider']} / {st['model']}: {len(batches)} batch(es)")
            total, errors, failed = 0, [], 0
            for i, batch in enumerate(batches):
                n = sum(len(v) for v in batch.values())
                log(f"batch {i + 1}/{len(batches)} ({n} entries)…")
                out = None
                for attempt in range(3):  # transient API failures must
                    try:                  # never kill the whole fill
                        out = call_model(st, build_prompt(plan, batch))
                        break
                    except Exception as e:
                        log(f"  attempt {attempt + 1}/3 failed: {e}")
                        time.sleep(5 * (attempt + 1))
                if out is None:
                    failed += 1
                    log("  batch skipped — rerun Fill to resume "
                        "(only unfilled entries are re-sent)")
                    continue
                try:
                    filled = extract_json(out)
                except Exception as e:
                    log(f"  could not parse model output ({e}) — batch skipped")
                    failed += 1
                    continue
                a, errs = merge_fill(proj, filled)
                total += a
                errors += errs
                for er in errs:
                    log("  REJECTED " + er)
                log(f"  applied {a}")
            log(f"done: {total} fields filled, {len(errors)} rejected by "
                f"guardrails, {failed} batch(es) failed."
                + (" Rerun Fill to resume the failed batches."
                   if failed else " Review in the Strings tab, then Build."))
            JOBS[jid].update(done=True, ok=failed == 0)
        except Exception as e:
            log(f"ERROR: {e}")
            JOBS[jid].update(done=True, ok=False)

    threading.Thread(target=run, daemon=True).start()
    return jid


# ───────────────────────── edit mode (visual picker) ─────────────────
# /edit/<project>/ serves the BUILT site from the studio's own origin
# with a picker overlay injected: hover highlights, click reports the
# element to the studio, which maps it to its copy_map entry. Changes
# still flow through copy_map + build — the invariant holds; this is
# just a pointing device.

OVERLAY_JS = """<script data-forge-editor>(function(){
var PICKING=true,HOVERMODE='freeze';  // freeze | sticky | live
window.__forgeState=function(){return {picking:PICKING,hover:HOVERMODE}};
window.addEventListener('message',function(e){
  /* THE RING IS DRAWN OUTSIDE THIS DOCUMENT, so only this side knows
     where the element actually is after a resize. Without this the ring
     keeps the geometry it had when you hovered — the owner watched it
     stay small after the window grew, and had to re-hover to fix it. */
  if(e.data&&e.data.forge==='remeasure'){
    var q=cur&&cur.getBoundingClientRect?cur.getBoundingClientRect():null;
    parent.postMessage({forge:'hover',rect:q?{t:q.top,b:q.bottom,
      l:q.left,r:q.right}:null},'*');
    return;
  }
  if(e.data&&e.data.forge==='mode'){
    PICKING=!!e.data.picking;
    if(e.data.hover){
      if(e.data.hover!==HOVERMODE){entered=[];stickyLast=null}
      HOVERMODE=e.data.hover;
    }
  }
});
/* Hover-variant cards flip their content on mouseenter — the text you
   want to edit vanishes as you approach it. In picking mode we control
   hover delivery to the page:
   - freeze: block ALL enter/leave — cards stay in rest state
   - sticky: allow enter, block leave — hovering PINS the hover state
   (React synthesizes enter/leave from bubbling over/out, so stopping
   those at document-capture starves its delegated listeners.) */
/* enter/leave don't bubble but DO capture — stopping them at document
   capture starves even listeners attached directly on elements */
['mouseover','mouseout','pointerover','pointerout',
 'mouseenter','mouseleave','pointerenter','pointerleave']
.forEach(function(t){
  document.addEventListener(t,function(e){
    if(!e.isTrusted)return;   // our own synthetic hover must pass
    if(!PICKING||HOVERMODE==='live')return;
    var leaving=/out|leave/.test(t);
    if(HOVERMODE==='freeze'||leaving)e.stopPropagation();
  },true);
});
/* STICKY doesn't just let real hover through — runtime/mount quirks
   can starve it — it DRIVES the flip: dispatch synthetic enter/over
   for everything under the cursor (framer-motion and React don't
   check isTrusted) and never send the matching leaves. Sweep a
   hover-variant card and it flips AND STAYS flipped, so the hover
   side is clickable/editable. Reset = toggle back to freeze
   (reloads) or rebuild. */
var entered=[],stickyLast=null;
function stickyEnter(x,y){
  var deep=document.elementFromPoint(x,y);
  if(!deep||deep===stickyLast)return;stickyLast=deep;
  entered=entered.filter(function(el){return el.isConnected});
  var chain=[],n=deep;
  while(n&&n.nodeType===1){chain.unshift(n);n=n.parentElement}
  chain.forEach(function(el){
    if(entered.indexOf(el)>=0)return;entered.push(el);
    [['pointerenter',PointerEvent],['mouseenter',MouseEvent]]
    .forEach(function(te){el.dispatchEvent(new te[1](te[0],
      {bubbles:false,cancelable:true,clientX:x,clientY:y,
       pointerId:1,pointerType:'mouse',isPrimary:true,view:window}))});
  });
  [['pointerover',PointerEvent],['mouseover',MouseEvent],
   ['pointermove',PointerEvent],['mousemove',MouseEvent]]
  .forEach(function(te){deep.dispatchEvent(new te[1](te[0],
    {bubbles:true,cancelable:true,clientX:x,clientY:y,
     pointerId:1,pointerType:'mouse',isPrimary:true,view:window}))});
}
window.__forgeHover=stickyEnter;
var st=document.createElement('style');
/* THE RING IS DRAWN BY THE PARENT NOW, so this in-page outline was a
   SECOND indicator sitting under the first — the bulky dashed box the
   owner could see beneath the new one. Kept as a no-op class so every
   existing add/remove call still works, and so the legacy preview tab
   (which has no parent ring) still shows something. */
st.textContent='.__forge-hl{outline:1px solid rgba(217,119,87,.35) !important;'+
 'outline-offset:2px;cursor:crosshair !important}'+
 '.__forge-target{outline:3px solid #34d399 !important;'+
 'outline-offset:2px}'+
 /* Framer marks image/decoration layers pointer-events:none — they are
    invisible to hit-testing, i.e. unclickable. Edit mode intercepts
    every click anyway, so make EVERYTHING pickable. */
 '*{pointer-events:auto !important}';
document.head.appendChild(st);
var cur=null;
function imgUrl(el){
  if(el.tagName==='IMG')return (el.currentSrc||el.src||'').split('?')[0];
  var bg=getComputedStyle(el).backgroundImage;
  var m=bg&&bg!=='none'?bg.match(/url\\((["']?)([^"')]+)\\1/):null;
  return m?m[2].split('?')[0]:null;
}
function findPick(x,y){
  // walk the full stack under the cursor: text/imgs behind transparent
  // overlay divs, CSS background-image elements — everything counts.
  // Several images stacked (hero collages)? report them ALL and let
  // the studio ask which one.
  var list=document.elementsFromPoint(x,y),i,el,imgs=[],seen={},firstEl=null;
  function overlayHuge(n){ // full-bleed decoration layers must not
    var r=n.getBoundingClientRect();  // steal picks from buttons/text
    return r.width*r.height>window.innerWidth*window.innerHeight*0.6;
  }
  for(i=0;i<list.length;i++){
    el=list[i];
    if(el===document.body)break;
    var u=imgUrl(el);
    if(u&&!seen[u]){seen[u]=1;
      imgs.push({src:u,huge:overlayHuge(el)});
      if(!firstEl)firstEl=el;}
    if(el.tagName!=='IMG'&&el.childElementCount===0
       &&(el.textContent||'').trim().length>=1){
      if(imgs.some(function(m){return !m.huge}))break; // real image above
      // Framer splits text into per-char/word spans. Collect this leaf
      // AND its ancestors' text (leaf-first) so the studio can resolve
      // to the smallest COMPLETE sentence/heading in the copy map.
      var texts=[],node=el,sn={},kk=0;
      while(node&&node!==document.body&&kk<9){
        var tt=(node.textContent||'').replace(/\\s+/g,' ').trim();
        if(tt&&!sn[tt]){sn[tt]=1;texts.push(tt);}
        node=node.parentElement;kk++;
      }
      return{kind:'string',el:el,texts:texts,text:texts[0]||''};
    }
  }
  if(imgs.length){
    imgs.sort(function(a,b){return (a.huge?1:0)-(b.huge?1:0)});
    return{kind:'images',el:firstEl,
           srcs:imgs.slice(0,6).map(function(m){return m.src})};
  }
  // nothing text/image under the cursor: pick the CONTAINER itself
  // (buttons, sections, cards — the things with backgrounds). Prefer
  // a reasonably-sized one; page-spanning wrappers only as last resort
  var fallback=null;
  for(i=0;i<list.length;i++){
    el=list[i];
    if(el===document.body||el===document.documentElement)break;
    var r=el.getBoundingClientRect();
    if(r.width>4&&r.height>4){
      if(!overlayHuge(el))return{kind:'container',el:el};
      if(!fallback)fallback=el;
    }
  }
  if(fallback)return{kind:'container',el:fallback,huge:true};
  return null;
}
document.addEventListener('mousemove',function(e){
  if(!e.isTrusted)return; // synthetic moves (incl. our own) don't re-pick
  if(!PICKING){if(cur){cur.classList.remove('__forge-hl');cur=null}return}
  if(HOVERMODE==='sticky')stickyEnter(e.clientX,e.clientY);
  var p=findPick(e.clientX,e.clientY);
  var el=p?p.el:null;
  if(cur&&cur!==el)cur.classList.remove('__forge-hl');
  if(el)el.classList.add('__forge-hl');
  /* ONE RING, MORPHING. A dashed outline stamped on each element in turn
     snaps from thing to thing; the parent draws a single ring instead
     and springs it between them, which is the same idea as Apple's
     glassEffectID morph and reads as one object moving rather than a
     border being switched on and off. The rect goes out on every move
     because only the parent can animate across the iframe boundary. */
  if(el!==cur||!el){
    var q=el?el.getBoundingClientRect():null;
    parent.postMessage({forge:'hover',rect:q?{t:q.top,b:q.bottom,
      l:q.left,r:q.right}:null},'*');
  }
  cur=el;
},true);
function cssPath(el){
  var parts=[];
  while(el&&el.nodeType===1&&el.tagName!=='BODY'){
    if(el.id){parts.unshift('#'+CSS.escape(el.id));break}
    var ix=1,sib=el;
    while((sib=sib.previousElementSibling))ix++;
    parts.unshift(el.tagName.toLowerCase()+':nth-child('+ix+')');
    el=el.parentElement;
  }
  if(!parts.length||parts[0][0]!=='#')parts.unshift('body');
  return parts.join(' > ');
}
function elInfo(el){
  var cls=[].slice.call(el.classList).filter(function(c){
    return c!=='__forge-hl'&&!/^w--/.test(c)});
  var sig={tag:el.tagName.toLowerCase(),id:el.id||null,classes:cls,
    frname:el.getAttribute('data-framer-name')||null,
    label:(el.textContent||el.getAttribute('alt')||'').trim().slice(0,60),
    path:cssPath(el),
    index:0};
  try{
    var sel=sig.id?'#'+CSS.escape(sig.id)
      :sig.tag+cls.map(function(c){return '.'+CSS.escape(c)}).join('');
    sig.index=Math.max(0,[].indexOf.call(document.querySelectorAll(sel),el));
  }catch(e){}
  return sig;
}
function elInfoFull(el){
  var s=elInfo(el),p=el.parentElement,n=0;
  s.ancestors=[];
  while(p&&p.tagName!=='BODY'&&n<6){s.ancestors.push(elInfo(p));
    p=p.parentElement;n++;}
  return s;
}
document.addEventListener('click',function(e){
  if(!PICKING)return;            // browse mode: clicks navigate normally
  if(e.altKey||e.metaKey){
    // ⌥/⌘-click should NAVIGATE to another page — but the browser's
    // native ⌥-click DOWNLOADS the link and ⌘-click opens a new tab.
    // Intercept and navigate the edit-mount iframe in place instead.
    var a=e.target.closest&&e.target.closest('a[href]');
    if(a){var h=a.getAttribute('href')||'';
      if(h&&!/^(#|mailto:|tel:|javascript:)/.test(h)){
        e.preventDefault();e.stopPropagation();
        try{location.href=a.href;}catch(_){}
      }}
    return;
  }
  var p=findPick(e.clientX,e.clientY);
  if(!p)return;
  var _r=p.el&&p.el.getBoundingClientRect?p.el.getBoundingClientRect():null;
  var _rect=_r?{t:_r.top,b:_r.bottom,l:_r.left,r:_r.right}:null;
  e.preventDefault();e.stopPropagation();
  if(p.kind==='images')
    parent.postMessage({forge:'pick',kind:'images',srcs:p.srcs,x:e.clientX,y:e.clientY,rect:_rect,
      el:elInfoFull(p.el)},'*');
  else if(p.kind==='container')
    parent.postMessage({forge:'pick',kind:'container',huge:!!p.huge,x:e.clientX,y:e.clientY,rect:_rect,
      el:elInfoFull(p.el)},'*');
  else
    parent.postMessage({forge:'pick',kind:'string',text:p.text,x:e.clientX,y:e.clientY,rect:_rect,
      texts:p.texts,el:elInfoFull(p.el)},'*');
},true);
})();</script>"""


# ───────────────────────── HTTP handler ──────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        # HTTP Basic auth, only when STUDIO_PASSWORD is set (hosted
        # instances). User is always 'aethron'.
        pw = os.environ.get("STUDIO_PASSWORD")
        if not pw:
            return True
        want = "Basic " + base64.b64encode(
            f"aethron:{pw}".encode()).decode()
        if self.headers.get("Authorization", "") == want:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="Aethron Studio"')
        self.end_headers()
        return False

    def log_message(self, *a):
        pass

    def _session(self):
        # returns the logged-in user dict, or None. Cloud mode only.
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"aethron_sess=([A-Za-z0-9_-]+)", cookie)
        u = SESSIONS.get(m.group(1)) if m else None
        if u:
            _session_touch(u)
        return u

    def _trace_cookie(self, path):
        raw = self.headers.get("Cookie") or ""
        has = "aethron_sess=" in raw
        m = re.search(r"aethron_sess=([A-Za-z0-9_-]+)", raw)
        known = bool(m and SESSIONS.get(m.group(1)))
        _clog(f"GET {path[:20]:20} cookie_sent={has} known_session={known} "
              f"sessions_in_memory={len(SESSIONS)}")

    def _gate(self, u):
        if u.path in ("/", "/index.html"):
            raw = self.headers.get("Cookie") or ""
            m = re.search(r"aethron_sess=([A-Za-z0-9_-]+)", raw)
            _clog(f"PAGE {u.path} cookie_sent={'aethron_sess=' in raw} "
                  f"matches_session={bool(m and SESSIONS.get(m.group(1)))} "
                  f"sessions={len(SESSIONS)}")
        # When cloud auth is ON, everything except the login page and the
        # auth endpoints requires a session. Dormant otherwise (local /
        # desktop-offline use never sees a login screen).
        if not cloud.ENABLED:
            return True
        if u.path in ("/login", "/api/auth/login", "/api/auth/signup",
                      "/api/auth/google", "/api/auth/google/start",
                      "/api/auth/google/poll", "/auth/callback",
                      "/api/auth/session",
                      # An error on the LOGIN page is still an error we
                      # need to see; gating the report would hide
                      # exactly the failures nobody can work around.
                      "/api/clienterror"):
            return True
        s = self._session()
        if s:
            # billing enforcement re-check: if the switch is flipped on,
            # a session on a free plan is locked out immediately, not
            # just at next login.
            if cloud.entitled(s.get("plan", "free")):
                return True
            if u.path == "/" or not u.path.startswith("/api"):
                self._trace_cookie(u.path)
                body = LOGIN_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.fail("upgrade required", 402)
            return False
        if u.path == "/" or not u.path.startswith("/api"):
            body = LOGIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.fail("login required", 401)
        return False

    def ai_settings(self, body):
        """Per-call overrides on top of the saved settings. Raises with
        the brain's own reason when nothing usable is configured."""
        st = {k: body.get(k, "") for k in
              ("provider", "base_url", "api_key", "model")}
        if brain:
            r = brain.resolve({k: v for k, v in st.items() if v} or None)
            if not r["ready"]:
                raise ValueError(r["why"] + " — open the Plan & AI tab "
                                 "(or the Code view) and save your key")
            return st
        if not st["model"]:
            raise ValueError("set a model + API key in the Plan & AI tab")
        return st

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, msg, code=400):
        self.send_json({"error": str(msg)}, code)

    def project_dir(self, q) -> Path:
        name = q.get("project", [""])[0] or q.get("name", [""])[0]
        d = (PROJECTS / name).resolve()
        if not name or not d.is_relative_to(PROJECTS) \
                or not (d / "forge.json").exists():
            raise FileNotFoundError("unknown project")
        return d

    # ---- the code workspace (IDE + coding agent) --------------------
    def ws_dir(self, src) -> Path:
        """A workspace is an Aethron project or a folder under
        workspaces/ — never an arbitrary path off the user's disk."""
        get = (lambda k: (src.get(k, [""])[0] if isinstance(src.get(k), list)
                          else src.get(k, "")) or "")
        project, ws = get("project"), get("workspace")
        if project:
            return self.project_dir({"project": [project]})
        WORKSPACES.mkdir(parents=True, exist_ok=True)
        d = (WORKSPACES / ws).resolve()
        if not ws or not d.is_relative_to(WORKSPACES) or not d.is_dir():
            raise FileNotFoundError("unknown workspace")
        return d

    # ------------------------------------------------------------ GET
    def do_GET(self):
        if not self._authed():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._gate(u):
            return
        try:
            if u.path == "/login":
                body = LOGIN_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if u.path == "/api/auth/me":
                s = self._session()
                return self.send_json({"email": s["email"]} if s else {}, 200)
            if u.path == "/api/design/list":
                return self.send_json({"pages": design_pages()})
            if u.path == "/api/wallet":
                import aethron_brain as _brain
                return self.send_json(_brain.wallet())
            if u.path.startswith("/design/"):
                # The page itself, for the preview frame. Served from HOME/pages,
                # never from anywhere a crafted name could reach.
                rel = urllib.parse.unquote(u.path[len("/design/"):])
                p = (PAGES / rel).resolve()
                if not str(p).startswith(str(PAGES.resolve())) or not p.is_file():
                    return self.fail("no such page", 404)
                body = p.read_bytes()
                kind = {".html": "text/html; charset=utf-8", ".png": "image/png",
                        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".webp": "image/webp", ".svg": "image/svg+xml",
                        ".json": "application/json", ".css": "text/css",
                        ".js": "text/javascript"}
                self.send_response(200)
                self.send_header("Content-Type",
                                 kind.get(p.suffix.lower(), "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(body)
            if u.path == "/api/auth/google/start":
                # DIRECT flow: our own Google client → the consent screen
                # reads "Aethron", and it works from a native app window
                # (opens the system browser, we loop back and poll).
                if not cloud.GOOGLE_DIRECT:
                    return self.send_json(
                        {"error": "direct google not configured"}, 400)
                port = self.server.server_address[1]
                cb = f"http://127.0.0.1:{port}/auth/callback"
                verifier, challenge = cloud.pkce_pair()
                state = secrets.token_urlsafe(18)
                PENDING[state] = {"verifier": verifier, "cb": cb,
                                  "cookie": None, "error": None,
                                  "ts": time.time()}
                url = cloud.google_authorize_url(cb, state, challenge)
                try:
                    import webbrowser
                    webbrowser.open(url)
                except Exception:
                    pass
                return self.send_json({"state": state, "url": url})
            if u.path == "/api/auth/google/poll":
                state = q.get("state", [""])[0]
                p = PENDING.get(state)
                if not p:
                    return self.send_json({"status": "unknown"}, 404)
                if p.get("error"):
                    PENDING.pop(state, None)
                    return self.send_json(
                        {"status": "error", "error": p["error"]}, 200)
                if p.get("cookie"):
                    cookie = PENDING.pop(state)["cookie"]
                    out = json.dumps({"status": "ok"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    _clog(f"SET via google/poll Max-Age=2592000 tok={cookie[:8]}…")
                    self.send_header("Set-Cookie", f"aethron_sess={cookie}; "
                                     "Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    return self.wfile.write(out)
                return self.send_json({"status": "pending"})
            if u.path == "/api/auth/google":
                port = self.server.server_address[1]
                cb = f"http://127.0.0.1:{port}/auth/callback"
                url = cloud.oauth_url("google", cb)
                if not url:      # dry/dormant: simulate a successful login
                    user = cloud.user_from_token("")
                    user["plan"] = cloud.plan_of(user["token"], user["id"])
                    if not cloud.entitled(user["plan"]):
                        self.send_response(302)
                        self.send_header("Location", "/login?locked=1")
                        self.end_headers()
                        return
                    tok = secrets.token_urlsafe(24)
                    SESSIONS[tok] = user
                    cloud.track("login", token=user["token"], source="google")
                    self.send_response(302)
                    _clog("SET via /auth/callback (system browser jar)")
                    self.send_header("Set-Cookie", f"aethron_sess={tok}; "
                                     "Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                self.send_response(302)
                self.send_header("Location", url)
                self.end_headers()
                return
            if u.path == "/auth/callback":
                code = q.get("code", [""])[0]
                state = q.get("state", [""])[0]
                if code and state in PENDING:      # DIRECT-flow callback
                    p = PENDING[state]
                    try:
                        idt = cloud.google_exchange_code(
                            code, p["cb"], p["verifier"])
                        cookie, err = mint_session(cloud.session_from_google(idt))
                        if err:
                            p["error"] = err[0]
                        else:
                            p["cookie"] = cookie
                            cloud.track("login", source="google")
                            focus_app()      # pull the app back to the front
                    except Exception as e:
                        p["error"] = str(e)
                    body = CALLBACK_DONE_HTML.encode()
                else:                              # Supabase fragment flow
                    body = CALLBACK_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if u.path.startswith("/edit/"):
                return self.serve_edit(u, q)
            if u.path == "/metal.js":
                # A vendored engine never changes between renders, so it
                # is cached hard. Everything else in this server sends
                # no-store, and that is right for anything Aethron makes;
                # this is not that.
                body = METAL_JS.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if u.path == "/":
                body = INDEX_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/update/just-updated":
                # DID THE APP JUST REPLACE ITSELF? The swap happens in
                # the OLD process; the new one boots with no memory of
                # it, so a successful self-update looks exactly like an
                # ordinary launch and the user is left wondering whether
                # anything happened. The updater leaves a note; this
                # reads it ONCE and deletes it, so the banner shows on
                # the first launch after an update and never again.
                #
                # TWO SIGNALS, because the first one CANNOT WORK on the
                # update that introduces it. The marker is written by
                # the updater — and the updater that runs is the OLD
                # version's, which knows nothing about it. Relying on it
                # alone would mean the banner never appears on the one
                # update where it was asked for, and only from the next
                # one onward. So there is also a version stamp written
                # on every launch, and the transition itself is evidence:
                # a HOME that has clearly been used before but carries no
                # stamp is a HOME whose app was just replaced by one that
                # keeps stamps. A genuinely fresh install has no history
                # to mistake, and stamps itself silently on first run.
                mark = HOME / ".last-update.json"
                stampf = HOME / ".last-run.json"
                ver = getattr(updater, "VERSION", "") if updater else ""
                out, prev, had_stamp = {"updated": False}, "", False
                try:
                    if stampf.is_file():
                        had_stamp = True
                        prev = str(json.loads(stampf.read_text(
                            encoding="utf-8")).get("version") or "")
                except Exception:
                    pass
                try:
                    if mark.is_file():                    # exact, when present
                        d = json.loads(mark.read_text(encoding="utf-8"))
                        out = {"updated": True,
                               "version": str(d.get("version") or ver),
                               "from": str(d.get("from") or prev)}
                        mark.unlink()
                    elif had_stamp and prev and prev != ver:
                        out = {"updated": True, "version": ver, "from": prev}
                    elif not had_stamp and any(
                            (HOME / n).exists() for n in
                            ("projects", "library", "aethron_config.json")):
                        # used before, never stamped => just replaced
                        out = {"updated": True, "version": ver, "from": ""}
                except Exception:
                    pass
                try:
                    HOME.mkdir(parents=True, exist_ok=True)
                    stampf.write_text(json.dumps({"version": ver}),
                                      encoding="utf-8")
                except Exception:
                    pass
                self.send_json(out)
            elif u.path == "/api/update/check":
                # Never blocks the UI on a network call it cannot
                # control: a failed check reports why and the app
                # carries on unchanged.
                self.send_json(updater.check(timeout=8) if updater else
                               {"available": False,
                                "why": "updater unavailable"})
            elif u.path == "/api/projects":
                PROJECTS.mkdir(parents=True, exist_ok=True)
                out = []
                for d in sorted(PROJECTS.iterdir()):
                    if (d / "forge.json").exists():
                        out.append(project_info(d))
                self.send_json(out)
            elif u.path == "/api/copymap":
                d = self.project_dir(q)
                f = d / "copy_map.json"
                if not f.exists():
                    return self.fail("run inventory first", 404)
                self.send_json(json.loads(f.read_text(encoding="utf-8")))
            elif u.path == "/api/plan":
                d = self.project_dir(q)
                f = d / "project_plan.md"
                self.send_json({"plan": f.read_text(encoding="utf-8")
                                if f.exists() else ""})
            elif u.path == "/api/config":
                d = self.project_dir(q)
                cfg = json.loads((d / "forge.json").read_text())
                self.send_json({"forbidden_words": cfg.get("forbidden_words", []),
                                "hide_selectors": cfg.get("hide_selectors", []),
                                "reduce_motion": cfg.get("reduce_motion", False)})
            elif u.path == "/api/report":
                d = self.project_dir(q)
                f = d / "site" / ".forge-report.json"
                self.send_json(json.loads(f.read_text(encoding="utf-8"))
                               if f.exists() else {})
            elif u.path == "/api/job":
                self.send_json(JOBS.get(q.get("id", [""])[0])
                               or {"error": "no such job"})
            elif u.path == "/api/ai/settings":
                if not brain:
                    return self.send_json({"available": False})
                st = brain.status()
                st["available"] = True
                self.send_json(st)
            # ---- code layer (IDE + coding agent) --------------------
            elif u.path == "/api/code/status":
                if not codelayer:
                    return self.send_json({"available": False,
                                           "why": CODE_IMPORT_ERROR})
                st = codelayer.status(HOME)
                st["available"] = True
                st["workspaces"] = sorted(
                    p.name for p in WORKSPACES.iterdir() if p.is_dir()) \
                    if WORKSPACES.is_dir() else []
                key = q.get("key", [""])[0]
                s = CODE_SESSIONS.get(key)
                st["session"] = {"running": bool(s and s.alive),
                                 "busy": bool(s and s.busy),
                                 "cost_usd": s.cost_usd if s else 0}
                self.send_json(st)
            elif u.path == "/api/code/events":
                s = CODE_SESSIONS.get(q.get("key", [""])[0])
                if not s:
                    return self.send_json({"events": [], "n": 0,
                                           "running": False})
                since = int(q.get("since", ["0"])[0] or 0)
                evs, n = s.drain(since)
                self.send_json({"events": evs, "n": n,
                                "running": s.alive, "busy": s.busy,
                                "cost_usd": s.cost_usd})
            elif u.path == "/api/fs/tree":
                d = self.ws_dir(q)
                self.send_json({"root": d.name, "files": fs_tree(d)})
            elif u.path == "/api/fs/read":
                d = self.ws_dir(q)
                rel = q.get("path", [""])[0]
                f = safe_path(d, rel)
                if f.stat().st_size > 1_500_000:
                    return self.send_json({"path": rel, "too_big": True,
                                           "size": f.stat().st_size})
                raw = f.read_bytes()
                if b"\0" in raw[:4000]:
                    return self.send_json({"path": rel, "binary": True,
                                           "size": len(raw)})
                self.send_json({"path": rel,
                                "text": raw.decode("utf-8", "replace"),
                                "locked": is_locked(d, f),
                                "size": len(raw)})
            elif u.path == "/api/ai/prompt":
                d = self.project_dir(q)
                plan = (d / "project_plan.md").read_text(encoding="utf-8") \
                    if (d / "project_plan.md").exists() else "(no plan yet)"
                cm = json.loads((d / "copy_map.json").read_text(encoding="utf-8"))
                batch = {}
                for sec in ("strings", "images", "links"):
                    left = [e for e in cm.get(sec, []) if not e.get("new")]
                    if left:
                        batch[sec] = left
                self.send_json({"prompt": build_prompt(plan, batch)})
            elif u.path == "/api/library":
                cards = []
                for f in sorted(LIBRARY.glob("*.json")) \
                        if LIBRARY.is_dir() else []:
                    try:
                        c = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    c["id"] = f.stem
                    cards.append(c)
                self.send_json(cards)
            elif u.path == "/api/download":
                d = self.project_dir(q)
                site = d / "site"
                if not site.exists():
                    return self.fail("run build first", 404)
                full = q.get("full", ["0"])[0] == "1"
                buf = io.BytesIO()
                if full:
                    # dev/AI handoff: the WHOLE self-contained project —
                    # rebuildable, extendable, with the backend + guide
                    subprocess.run(forge_argv("backend"),
                                   cwd=d, capture_output=True)
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        z.write(FORGE, "forge.py")
                        for f in d.rglob("*"):
                            if f.is_file() and ".history" not in f.parts:
                                z.write(f, f.relative_to(d))
                    fname = f"{d.name}-project.zip"
                else:
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        for f in site.rglob("*"):
                            if f.is_file():
                                z.write(f, f.relative_to(site))
                    fname = f"{d.name}-site.zip"
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.fail("not found", 404)
        except FileNotFoundError as e:
            self.fail(e, 404)
        except Exception as e:
            self.fail(e, 500)

    def serve_edit(self, u, q):
        parts = u.path.split("/", 3)          # '', 'edit', name, rest
        name = parts[2] if len(parts) > 2 else ""
        rest = parts[3] if len(parts) > 3 else ""
        d = self.project_dir({"name": [name]})
        site = (d / "site").resolve()
        f = (site / (rest or "index.html")).resolve()
        if f.is_dir():
            f = f / "index.html"
        if not f.is_relative_to(site):
            return self.send_error(404)
        if not f.is_file():
            # platform-aware: Framer is a SPA (extensionless deep links
            # are client routes -> index.html); Webflow is multi-page, so
            # an unmatched path is a real page miss -> 404.html, NEVER the
            # home page (that's the "selecting a page just shows home" bug).
            platform = json.loads((d / "forge.json").read_text()) \
                .get("platform", "static")
            extless = "." not in Path(rest).name
            if platform == "framer" and extless:
                f = site / "index.html"
            elif (site / "404.html").is_file():
                f = site / "404.html"
            else:
                return self.send_error(404)
        data = f.read_bytes()
        if f.name.endswith(".framercms") and "range" in q:
            pieces = []
            for part in q["range"][0].split(","):
                m = re.fullmatch(r"(\d+)-(\d+)?", part.strip())
                if not m:
                    return self.send_error(400)
                s = int(m.group(1))
                e = int(m.group(2)) + 1 if m.group(2) else len(data)
                pieces.append(data[s:e])
            data, ctype = b"".join(pieces), "application/octet-stream"
        elif ".js@" in f.name or f.name.endswith((".js", ".mjs")) \
                or f.suffix.lower() in (".html", ".htm", ".css"):
            is_js = ".js@" in f.name or f.name.endswith((".js", ".mjs"))
            ctype = "text/javascript" if is_js else (
                "text/css" if f.suffix.lower() == ".css"
                else "text/html; charset=utf-8")
            # every absolute {pub}/… reference — HTML attrs, CSS url(),
            # chunk template literals AND plain JS strings (hydration
            # re-renders img srcs from chunk data) — must resolve under
            # this /edit/<name>/ mount
            pub = json.loads((d / "forge.json").read_text()) \
                .get("public_base", "/assets").rstrip("/").encode()
            mount = b"/edit/" + name.encode() + pub + b"/"
            for pre in (b'"', b"'", b"`", b"(", b" ", b","):
                data = data.replace(pre + pub + b"/", pre + mount)
            if is_js:
                data = data.replace(
                    b"${location.origin}" + pub + b"/",
                    b"${location.origin}/edit/" + name.encode() + pub + b"/")
            elif f.suffix.lower() != ".css":
                data = data.replace(b"</body>", OVERLAY_JS.encode() + b"</body>")
        else:
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # edits must show
        self.end_headers()
        self.wfile.write(data)

    # ----------------------------------------------------------- POST
    def do_POST(self):
        if not self._authed():
            return
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.fail("bad JSON body")
        try:
            if u.path in ("/api/auth/login", "/api/auth/signup"):
                return self.api_auth(u.path, body)
            if u.path == "/api/image":
                # PASTE, DON'T FILE. A screenshot lives on the clipboard
                # for about four seconds; telling someone to save it,
                # find it, and type its path is how a feature goes
                # unused. The browser hands us the bytes — take them.
                #
                # The NAME is ours, never the client's: it is the sha1 of
                # the content, so a crafted filename cannot escape the
                # directory and the same image pasted twice is stored
                # once.
                raw = body.get("data") or ""
                if "," in raw[:64]:            # data:image/png;base64,…
                    head, raw = raw.split(",", 1)
                else:
                    head = ""
                try:
                    blob = base64.b64decode(raw, validate=True)
                except Exception:
                    return self.fail("that paste was not an image")
                if not blob:
                    return self.fail("that paste was empty")
                if len(blob) > 24 * 1024 * 1024:
                    return self.fail(f"image is {len(blob) // 1048576}MB — "
                                     f"24MB is the limit")
                kind = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg",
                        b"GIF8": "gif", b"RIFF": "webp"}
                ext = next((v for k, v in kind.items()
                            if blob.startswith(k)), "")
                if not ext:
                    return self.fail("that file is not a PNG, JPG, GIF or "
                                     "WEBP")
                import hashlib
                name = hashlib.sha1(blob).hexdigest()[:16] + "." + ext
                d = HOME / "uploads"
                d.mkdir(parents=True, exist_ok=True)
                p = d / name
                if not p.exists():
                    p.write_bytes(blob)
                return self.send_json({"path": str(p), "name": name,
                                       "bytes": len(blob)})
            if u.path == "/api/clienterror":
                # THE WINDOW HAS NO CONSOLE. A packaged desktop app
                # gives the user no devtools and gives us no stderr, so
                # a JavaScript error was previously invisible — it
                # showed up only as a page that looked wrong. That is
                # exactly how "the interface fell apart" kept arriving
                # with no cause attached. Now it is written down.
                #
                # Handled BEFORE the login gate on purpose: an error on
                # the login screen is the one nobody can work around.
                try:
                    _clog(f"JS  {str(body.get('msg',''))[:400]} | "
                          f"project={body.get('project','')} | "
                          f"{str(body.get('stack') or '')[:400]}")
                except Exception:
                    pass
                return self.send_json({"ok": True})
            if u.path == "/api/auth/session":
                # OAuth callback landed with an access token in the URL
                # fragment; the callback page POSTs it here so we can
                # build a server-side session (token never persists in
                # the browser beyond this hop).
                try:
                    user = cloud.user_from_token(body.get("access_token", ""))
                except Exception as e:
                    return self.fail(str(e), 401)
                if not user.get("id"):
                    return self.fail("could not resolve user", 401)
                user["plan"] = cloud.plan_of(user["token"], user["id"])
                if not cloud.entitled(user["plan"]):
                    cloud.track("login_blocked", token=user["token"],
                                step="billing")
                    return self.fail("Your free beta access has ended — "
                                     "upgrade to Pro to keep using Aethron.",
                                     402)
                tok = secrets.token_urlsafe(24)
                SESSIONS[tok] = user
                cloud.track("login", token=user["token"], source="google")
                out = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"aethron_sess={tok}; "
                                 "Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                return self.wfile.write(out)
            if u.path == "/api/update/apply":
                if not updater:
                    return self.fail("updater unavailable")

                def run_update(append):
                    # THE UPDATE IS A JOB, NOT A REQUEST. Measured on the
                    # owner's own connection: 100 KB/s, so a 43 MB build
                    # takes ~6 minutes. Held open as a single HTTP call
                    # that is indistinguishable from a hang — which is
                    # exactly what it looked like the first time. Now it
                    # streams a percentage like every other run does.
                    seen = [-1]

                    def progress(frac):
                        pct = int(frac * 100)
                        if pct != seen[0]:
                            seen[0] = pct
                            append(f"downloading {pct}%")

                    append("checking for a newer build")
                    r = updater.update(progress=progress, note=append)
                    if not r.get("ok"):
                        append(f"update failed: {r.get('why', 'unknown')}")
                        append("your current app is untouched")
                        return False
                    append(f"installed {r['version']} — restarting")
                    # relaunch the REPLACED bundle, then let this process
                    # die so the new one owns the port. The delay lets
                    # this job's final poll reach the UI first.
                    threading.Timer(1.8, lambda: (
                        updater.relaunch(Path(r["path"])),
                        os._exit(0))).start()
                    return True

                return self.send_json({"job": start_fn_job(run_update, "")})
            if u.path == "/api/auth/logout":
                s = self.headers.get("Cookie", "")
                m = re.search(r"aethron_sess=([A-Za-z0-9_-]+)", s)
                if m:
                    SESSIONS.pop(m.group(1), None)
                    _sessions_save()
                # AND CLEAR THE COOKIE. Popping the server session alone
                # left the browser holding a token forever now that both
                # sides persist — logout has to expire it too.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "aethron_sess=; Path=/; "
                                 "HttpOnly; SameSite=Lax; Max-Age=0")
                body_out = json.dumps({"ok": True}).encode()
                self.send_header("Content-Length", str(len(body_out)))
                self.end_headers()
                self.wfile.write(body_out)
                return
            if not self._gate(u):
                return
            if u.path == "/api/projects":
                self.api_create(body)
            elif u.path == "/api/projects/delete":
                d = self.project_dir({"name": [body.get("name", "")]})
                if body.get("name") in PREVIEWS:
                    PREVIEWS.pop(body["name"])[1].terminate()
                shutil.rmtree(d)
                self.send_json({"ok": True})
            elif u.path == "/api/run":
                d = self.project_dir({"name": [body.get("project", "")]})
                cmd = body.get("cmd", "")
                if cmd == "logo":
                    argv = forge_argv("logo",
                            body.get("text") or "Brand")
                    for flag in ("font", "color", "tracking"):
                        if body.get(flag):
                            argv += ["--" + flag, str(body[flag])]
                elif cmd == "convert":
                    fw = str(body.get("framework") or "astro")
                    if fw not in CONVERT_FRAMEWORKS:
                        return self.fail(f"framework must be one of "
                                         f"{', '.join(CONVERT_FRAMEWORKS)}")
                    argv = forge_argv("convert", str(d), "--framework", fw)
                elif cmd in RUN_CMDS:
                    argv = forge_argv(cmd)
                else:
                    return self.fail("command not allowed")
                self._track("run_step", step=cmd)
                self.send_json({"job": start_job(argv, d)})
            elif u.path == "/api/glass":
                # THE PAGE CANNOT REFRACT, SO IT ASKS THE WINDOW TO.
                # Measured: `backdrop-filter: url(#svg)` is ignored by
                # this engine, so a magnifying, mirroring edge is not
                # available to a <div> at all. It IS available to an
                # NSGlassEffectView, which already sits in this window —
                # so the page posts the rectangle it wants backed, in CSS
                # pixels with a top-left origin, and the native shell
                # moves real glass there.
                GLASS_RECT.clear()
                GLASS_RECT.update(body or {})
                self.send_json({"ok": True})
            elif u.path == "/api/ai/settings":
                if not brain:
                    return self.fail("AI settings unavailable")
                saved = brain.save(body.get("ai") or {})
                # THE BUG THIS FIXES, AND IT COST DAYS:
                # a session's endpoint is baked into its child process
                # environment at spawn (ANTHROPIC_BASE_URL), so a running
                # CLI points at ONE bridge for its whole life. Saving
                # settings shuts that bridge down — correctly, the next
                # session must not reuse it — but any session already
                # open then POSTs into a dead socket. No response, no
                # error, no timeout the user can see: the turn simply
                # hangs on "Thinking" forever.
                # A session is bound to the key it started with, so the
                # honest thing is to end it. The next prompt opens a
                # fresh one on the new endpoint.
                ended = 0
                for _k, _s in list(CODE_SESSIONS.items()):
                    try:
                        _s.close()
                        ended += 1
                    except Exception:
                        pass
                    CODE_SESSIONS.pop(_k, None)
                brain.shutdown_bridge()   # only now is it safe to stop
                self.send_json({"ok": True, "ended": ended,
                                "ai": {**saved, "api_key":
                                ("set" if saved.get("api_key") else "")}})
            # ---- code layer -----------------------------------------
            elif u.path == "/api/code/start":
                if not codelayer:
                    return self.fail("the coding layer is unavailable: "
                                     + CODE_IMPORT_ERROR)
                cfg = dict(body.get("cfg") or {})
                if body.get("console"):
                    # THE FRONT DOOR. Before any project exists there is
                    # no workspace to root a session in — but the
                    # mcp__aethron__* tools act on HOME/projects wherever
                    # the agent is sitting, so a dedicated console folder
                    # is enough to let it create the project it is about
                    # to migrate.
                    d = WORKSPACES / "__console__"
                    d.mkdir(parents=True, exist_ok=True)
                    cfg.setdefault("append_system", codelayer.CONSOLE_RULES)
                else:
                    d = self.ws_dir(body)
                key = str(d)
                old = CODE_SESSIONS.pop(key, None)
                if old:
                    old.close()
                try:
                    s = codelayer.CodeSession(
                        d, cfg=cfg, home=HOME).start()
                except Exception as e:
                    return self.fail(str(e))
                CODE_SESSIONS[key] = s
                self._track("code_session", runtime=s.cfg.get("runtime", ""),
                            provider=s.cfg.get("provider", ""))
                self.send_json({"key": key, "id": s.id,
                                "workspace": d.name})
            elif u.path == "/api/code/send":
                s = CODE_SESSIONS.get(body.get("key", ""))
                if not s or not s.alive:
                    return self.fail("no running session — start one first")
                text = (body.get("text") or "").strip()
                if not text:
                    return self.fail("empty message")
                try:
                    s.send(text)
                except Exception as e:
                    return self.fail(str(e))
                self.send_json({"ok": True})
            elif u.path == "/api/code/stop":
                s = CODE_SESSIONS.pop(body.get("key", ""), None)
                if s:
                    s.interrupt()
                self.send_json({"ok": True})
            elif u.path == "/api/code/config":
                # persisted where every face reads it (CLI, studio, app)
                f = ROOT / "aethron_config.json"
                try:
                    cur = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    cur = {}
                cur["code"] = {**(cur.get("code") or {}),
                               **(body.get("code") or {})}
                f.write_text(json.dumps(cur, indent=1), encoding="utf-8")
                self.send_json({"ok": True, "code": cur["code"]})
            elif u.path == "/api/workspaces":
                name = re.sub(r"[^A-Za-z0-9 _-]", "",
                              body.get("name", "")).strip()
                if not name:
                    return self.fail("name required")
                d = WORKSPACES / name
                if d.exists():
                    return self.fail("a workspace with that name exists")
                d.mkdir(parents=True)
                (d / "README.md").write_text(
                    f"# {name}\n\nBuilt with Aethron.\n", encoding="utf-8")
                self.send_json({"ok": True, "workspace": name})
            elif u.path == "/api/fs/write":
                d = self.ws_dir(body)
                rel = body.get("path", "")
                f = (d / rel).resolve()
                if not f.is_relative_to(d.resolve()) or not rel:
                    return self.fail("path outside the workspace")
                if is_locked(d, f):
                    return self.fail(
                        f"{rel} is generated/sealed output — Aethron never "
                        "hand-edits site/ or pristine/ (hydration reverts "
                        "it and the seal fails). Change it in the Strings/"
                        "Images tab or copy_map.json, then rebuild.")
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(body.get("text", ""), encoding="utf-8")
                self.send_json({"ok": True, "size": f.stat().st_size})
            elif u.path == "/api/library/save":
                d = self.project_dir({"name": [body.get("project", "")]})
                r = subprocess.run(forge_argv("card"),
                                   cwd=d, capture_output=True, text=True)
                if r.returncode:
                    return self.fail((r.stdout + r.stderr).strip()
                                     or "card extraction failed")
                card = json.loads((d / "design_card.json")
                                  .read_text(encoding="utf-8"))
                card["project"] = d.name
                card["saved"] = time.strftime("%Y-%m-%d")
                LIBRARY.mkdir(exist_ok=True)
                (LIBRARY / f"{d.name}.json").write_text(
                    json.dumps(card, indent=1, ensure_ascii=False),
                    encoding="utf-8")
                self.send_json({"ok": True, "id": d.name, "card": card})
            elif u.path == "/api/library/delete":
                lid = re.sub(r"[^\w-]+", "", body.get("id", ""))
                f = LIBRARY / f"{lid}.json"
                if not lid or not f.exists():
                    return self.fail("unknown library entry")
                f.unlink()
                self.send_json({"ok": True})
            elif u.path == "/api/library/start":
                lid = re.sub(r"[^\w-]+", "", body.get("id", ""))
                f = LIBRARY / f"{lid}.json"
                if not f.exists():
                    return self.fail("unknown library entry")
                card = json.loads(f.read_text(encoding="utf-8"))
                # licence-clean by construction: we re-import from the
                # card's live source URL, or from the owner's own local
                # project — a card alone can't rebuild a template
                if card.get("source_url"):
                    return self.api_create({"name": body.get("name", ""),
                                            "url": card["source_url"]})
                src = PROJECTS / card.get("project", "_")
                if not (src / "forge.json").exists():
                    return self.fail(
                        "this card has no source URL and the original "
                        "project is gone — re-import the template you "
                        "own, then save it to the library again")
                pcfg = json.loads((src / "forge.json")
                                  .read_text(encoding="utf-8"))
                with tempfile.TemporaryDirectory() as td:
                    for p in pcfg.get("pages", []):
                        pg = src / "pristine" / p
                        if pg.is_file():
                            shutil.copy(pg, Path(td) / p)
                    if not list(Path(td).iterdir()):
                        return self.fail("original project has no pages")
                    raw = body.get("name", "")
                    name = re.sub(r"[^\w-]+", "-",
                                  raw.strip().lower()).strip("-")
                    if not name:
                        return self.fail("give the new project a name")
                    if (PROJECTS / name).exists():
                        return self.fail(f"project '{name}' already exists")
                    r = subprocess.run(
                        forge_argv("init", td,
                         "--name", name),
                        cwd=PROJECTS, capture_output=True, text=True)
                if r.returncode:
                    return self.fail((r.stdout + r.stderr).strip()
                                     or "init failed")
                self.send_json({"name": name, "log": r.stdout})
            elif u.path == "/api/ai/match":
                plan = (body.get("plan") or "").strip()
                if not plan:
                    return self.fail("describe the project first — even "
                                     "a few rough words work")
                st = self.ai_settings(body)
                cards = []
                for f in sorted(LIBRARY.glob("*.json")) \
                        if LIBRARY.is_dir() else []:
                    try:
                        c = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    cards.append({"id": f.stem, **{
                        k: c.get(k) for k in
                        ("title", "description", "platform", "palette",
                         "fonts", "sections", "features", "counts")}})
                if not cards:
                    return self.fail("library is empty — open a project "
                                     "and hit Save design first")
                out = call_model(st, MATCH_PROMPT % {
                    "plan": plan[:2000],
                    "cards": json.dumps(cards, ensure_ascii=False)[:12000]})
                m = re.search(r"\[.*\]", out, re.S)
                try:
                    ranked = json.loads(m.group(0)) if m else []
                except Exception:
                    ranked = []
                ids = {c["id"] for c in cards}
                ranked = [r for r in ranked if isinstance(r, dict)
                          and r.get("id") in ids][:3]
                if not ranked:
                    return self.fail("the model returned no usable "
                                     "ranking — try again or rephrase")
                self.send_json({"matches": ranked})
            elif u.path == "/api/copymap":
                d = self.project_dir({"name": [body.get("project", "")]})
                cm = body.get("copymap")
                if not isinstance(cm, dict) or "strings" not in cm:
                    return self.fail("malformed copy map")
                snapshot(d)
                (d / "copy_map.json").write_text(
                    json.dumps(cm, indent=1, ensure_ascii=False),
                    encoding="utf-8")
                self.send_json({"ok": True})
            elif u.path == "/api/plan":
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                (d / "project_plan.md").write_text(
                    body.get("plan", ""), encoding="utf-8")
                self.send_json({"ok": True})
            elif u.path == "/api/config":
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                cfg = json.loads((d / "forge.json").read_text())
                for k in ("forbidden_words", "hide_selectors"):
                    if k in body:
                        cfg[k] = [s.strip() for s in body[k]
                                  if isinstance(s, str) and s.strip()]
                if "reduce_motion" in body:
                    cfg["reduce_motion"] = bool(body["reduce_motion"])
                (d / "forge.json").write_text(json.dumps(cfg, indent=2))
                self.send_json({"ok": True})
            elif u.path == "/api/style":
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                sel = (body.get("selector") or "").strip()
                # '>' is the CSS child combinator — only block what can
                # escape a declaration block or open a tag
                if not sel or re.search(r"[{}<]", sel):
                    return self.fail("bad selector")
                clean = {p: v.strip() for p, v in (body.get("css") or {}).items()
                         if re.fullmatch(r"[a-zA-Z-]+", p)
                         and isinstance(v, str) and v.strip()
                         and not re.search(r"[{}<>;\\]|expression", v, re.I)}
                if not clean:
                    return self.fail("no valid css properties")
                cm_f = d / "copy_map.json"
                cm = json.loads(cm_f.read_text(encoding="utf-8"))
                for s in cm.setdefault("styles", []):
                    if s["selector"] == sel:
                        s["css"].update(clean)
                        break
                else:
                    cm["styles"].append({"selector": sel, "css": clean,
                                         "label": body.get("label", "")})
                cm_f.write_text(json.dumps(cm, indent=1, ensure_ascii=False),
                                encoding="utf-8")
                self.send_json({"ok": True, "selector": sel})
            elif u.path == "/api/assets":
                d = self.project_dir({"name": [body.get("project", "")]})
                fname = re.sub(r"[^\w.-]+", "-",
                               Path(body.get("filename", "")).name)
                if not fname.strip("-."):
                    return self.fail("no filename")
                (d / "assets").mkdir(exist_ok=True)
                (d / "assets" / fname).write_bytes(
                    base64.b64decode(body["data_b64"]))
                pub = json.loads((d / "forge.json").read_text()) \
                    .get("public_base", "/assets").rstrip("/")
                self.send_json({"url": f"{pub}/{fname}"})
            elif u.path == "/api/ai/plan":
                # rough owner notes in -> structured migration plan out
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                raw = (body.get("plan") or "").strip()
                if not raw:
                    return self.fail("write a few rough words first — "
                                     "brand name at minimum")
                st = self.ai_settings(body)
                out = call_model(st, PLAN_POLISH_PROMPT + raw)
                out = re.sub(r"^```\w*\n?|```$", "", out.strip(), flags=re.M)
                (d / "project_plan.md").write_text(out, encoding="utf-8")
                self.send_json({"plan": out})
            elif u.path == "/api/ai/edit":
                # natural-language element editing: instruction + element
                # context in, guarded text/css changes out
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                st = self.ai_settings(body)
                ctx = body.get("context") or {}
                instruction = (body.get("instruction") or "").strip()
                if not instruction:
                    return self.fail("say what you want changed")
                plan_f = d / "project_plan.md"
                plan = plan_f.read_text(encoding="utf-8") \
                    if plan_f.exists() else "(no plan)"
                mb = ctx.get("max_bytes")
                budget = (f"HARD LIMIT: text must be <= {mb} UTF-8 bytes "
                          "(em-dash/curly quotes = 3 bytes)." if mb else "")
                prompt = AI_EDIT_PROMPT % {
                    "budget": budget, "instruction": instruction,
                    "context": json.dumps(ctx, indent=1)[:4000],
                    "plan": plan[:1500]}
                act = extract_json(call_model(st, prompt))
                did, errs = [], []
                new_text = act.get("text")
                if new_text and ctx.get("old"):
                    if "`" in new_text or "${" in new_text:
                        errs.append("AI text contained backtick/${ — rejected")
                    elif mb and len(new_text.encode()) > mb:
                        errs.append(f"AI text over budget "
                                    f"({len(new_text.encode())}>{mb}B) — rejected")
                    else:
                        cm_f = d / "copy_map.json"
                        cm = json.loads(cm_f.read_text(encoding="utf-8"))
                        for e in cm.get("strings", []):
                            if e["old"] == ctx["old"]:
                                e["new"] = new_text
                                cm_f.write_text(json.dumps(
                                    cm, indent=1, ensure_ascii=False),
                                    encoding="utf-8")
                                did.append(f'text -> "{new_text[:60]}"')
                                break
                        else:
                            errs.append("entry not found for text change")
                css = act.get("css")
                if css and ctx.get("selector"):
                    clean = {p: str(v).strip() for p, v in dict(css).items()
                             if re.fullmatch(r"[a-zA-Z-]+", p)
                             and str(v).strip()
                             and not re.search(r"[{}<>;\\]|expression",
                                               str(v), re.I)}
                    if clean:
                        cm_f = d / "copy_map.json"
                        cm = json.loads(cm_f.read_text(encoding="utf-8"))
                        for s in cm.setdefault("styles", []):
                            if s["selector"] == ctx["selector"]:
                                s["css"].update(clean)
                                break
                        else:
                            cm["styles"].append({
                                "selector": ctx["selector"], "css": clean,
                                "label": "ai: " + instruction[:40]})
                        cm_f.write_text(json.dumps(cm, indent=1,
                                        ensure_ascii=False), encoding="utf-8")
                        did.append("css -> " + "; ".join(
                            f"{p}:{v}" for p, v in clean.items()))
                if not did:
                    return self.fail("no applicable change ("
                                     + "; ".join(errs) if errs else
                                     "AI returned nothing usable — "
                                     + str(act.get("explain", ""))[:100] + ")")
                self.send_json({"did": did, "errors": errs,
                                "explain": act.get("explain", "")})
            elif u.path == "/api/ai/fill":
                name = body.get("project", "")
                d = self.project_dir({"name": [name]})
                snapshot(d)
                st = self.ai_settings(body)
                self.send_json({"job": start_ai_job(name, st)})
            elif u.path == "/api/ai/merge":
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                filled = extract_json(body.get("text", ""))
                applied, errors = merge_fill(d, filled)
                self.send_json({"applied": applied, "errors": errors})
            elif u.path == "/api/wallet":
                # THE CEILING IS THE OWNER'S TO SET. It counts down by real charges and survives
                # restarts, so it is the one number that decides whether a card can be emptied.
                import aethron_brain as _brain
                try:
                    limit = float(body.get("limit_usd"))
                except (TypeError, ValueError):
                    return self.fail("give the ceiling in dollars, e.g. 2.50")
                if not 0 <= limit <= 1000:
                    return self.fail("a ceiling between $0 and $1000, please")
                _brain.wallet_set(limit, keep_spent=bool(body.get("keep_spent")))
                return self.send_json(_brain.wallet())
            elif u.path == "/api/design/new":
                # A SCREENSHOT BECOMES A PAGE. Every number in it is measured off
                # the image — a model reading a size off a picture is right about
                # 8% of the time, so no model is asked for one.
                img = Path(str(body.get("image") or ""))
                if not img.is_file():
                    return self.fail("paste or choose an image first")
                name = re.sub(r"[^a-z0-9-]+", "-",
                              str(body.get("name") or img.stem).lower()).strip("-") or "page"
                out = PAGES / name
                fast = bool(body.get("fast"))

                def job(append):
                    import aethron_replicate as R
                    import contextlib
                    append(f"measuring {img.name} …")

                    class Stream(io.TextIOBase):
                        """The rebuild says what it is doing; without this it said it to a
                        console no user has, and the view showed one line for fifteen minutes.
                        A silent box is indistinguishable from a hang."""
                        buf = ""

                        def write(self, s):
                            self.buf += s
                            while "\n" in self.buf:
                                line, self.buf = self.buf.split("\n", 1)
                                if line.strip():
                                    append(line.rstrip())
                            return len(s)
                    with contextlib.redirect_stdout(Stream()):
                        rep = R.replicate(img, out, fast=fast)
                    for k in ("verdict", "why", "identical", "background", "checklist",
                              "hand_entered_values"):
                        if rep.get(k) is not None:
                            append(f"  {k}: {rep[k]}")
                    if rep.get("verdict") != "BUILT":
                        append("\nNOT BUILT — the page was not handed over.")
                    return rep.get("verdict") == "BUILT"
                return self.send_json({"job": start_fn_job(
                    job, f"building a page from {img.name}\n"), "name": name})
            elif u.path == "/api/design/change":
                # ANY CHANGE, IN THE USER'S OWN WORDS. Tests are written first, the
                # code must pass them, and every difference on the page must be
                # explained by the request — or the page is left exactly as it was.
                name = str(body.get("name") or "")
                page = PAGES / name / "site.html"
                if not page.is_file():
                    return self.fail("no such page")
                ask = str(body.get("request") or "").strip()
                if not ask:
                    return self.fail("say what you want changed")
                budget = float(body.get("budget") or 0)

                def job(append):
                    import aethron_adopt as AD
                    import aethron_change as AC
                    import aethron_spec as SP
                    work = Path(tempfile.mkdtemp(prefix="ae-studio-change-"))
                    append(f"“{ask}”\n")
                    # ANY PAGE, NOT ONLY THE ONES AETHRON BUILT. A page carrying no
                    # measurable elements is unread, not unchangeable — stamp it first,
                    # and only if the stamping is proven invisible and proven to have
                    # landed on the elements it was picked for.
                    if AD.needs_adopting(page.read_text(encoding="utf-8")):
                        append("this page has no measurable elements yet — reading it "
                               "in a browser and naming them…\n")
                        rep = AD.adopt(page, write=True, log=lambda *a: append(
                            " ".join(str(x) for x in a) + "\n"))
                        out = []
                        AD.report(rep, out.append)
                        append("\n".join(out) + "\n")
                        if not rep.get("verdict", "").startswith(("ADOPTED", "ALREADY")):
                            append("\nNOT ASKED — the page could not be made measurable, "
                                   "so nothing was asked of a model and it is untouched.")
                            return False
                    AC.set_asset_base(page.parent)
                    AC.serve_assets(page.parent)
                    try:
                        res = SP.build(page.read_text(encoding="utf-8"), ask, work,
                                       budget_usd=budget)
                    finally:
                        # The studio outlives the job. A folder left served would keep a
                        # local port open on the user's project and silently decide how
                        # every later render resolves its assets.
                        AC.set_asset_base(None)
                        AC.stop_serving()
                    append(SP.report(res))
                    if res.get("verdict") == "APPLIED":
                        hist = PAGES / name / ".history"
                        hist.mkdir(exist_ok=True)
                        (hist / f"{time.time():.6f}.html").write_text(
                            page.read_text(encoding="utf-8"), encoding="utf-8")
                        page.write_text(res["html"], encoding="utf-8")
                        append("\nthe page was changed; the version before it is in "
                               ".history")
                    else:
                        append("\nthe page was left exactly as it was.")
                    return res.get("verdict") == "APPLIED"
                return self.send_json({"job": start_fn_job(job, "")})
            elif u.path == "/api/heal":
                # deterministic self-heal for broken fills (zero-effect
                # or hydration-revert): flex upgrade / source-casing /
                # nearest-source adoption — never a model, never a guess
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                if body.get("agent"):
                    # AGENTIC heal: the ladder first (free, certain), and
                    # only what it cannot fix goes to the model — which
                    # still may not declare success; verify + probe do.
                    import aethron_healer
                    rounds = int(body.get("rounds") or 2)

                    def job(append):
                        res = aethron_healer.heal(
                            d, rounds=rounds, home=HOME,
                            on_event=lambda k, ev: append(
                                {"tool": "   ", "say": "   ",
                                 "error": "!! "}.get(k, "── ") + ev["text"]))
                        if res.get("stage") == "unproven":
                            append("\nNOT PROVEN — this is not a repair "
                                   "failure:")
                            append(res.get("why", ""))
                        elif not res.get("ok"):
                            append("\nWHAT IS STILL BROKEN:")
                            append(aethron_healer.evidence_text(
                                res.get("evidence") or {})[:2000])
                            append("\n(Undo reverts everything the agent "
                                   "did.)")
                        # unproven is not success; the caller must not
                        # read "nothing more to fix" as "verified good".
                        return bool(res.get("ok"))

                    return self.send_json({"job": start_fn_job(
                        job, "$ agentic self-heal\n")})
                cmd = forge_argv("heal")
                idx0 = None
                if body.get("old"):
                    cmd += ["--entry", body["old"]]
                    try:
                        cmb = json.loads((d / "copy_map.json").read_text())
                        idx0 = next(i for i, s in enumerate(cmb["strings"])
                                    if s["old"] == body["old"])
                    except Exception:
                        idx0 = None
                r = subprocess.run(cmd, cwd=d, capture_output=True, text=True)
                out = (r.stdout + r.stderr).strip()
                new_old = body.get("old")
                if idx0 is not None:   # adoption may have changed the key
                    try:
                        cma = json.loads((d / "copy_map.json").read_text())
                        new_old = cma["strings"][idx0]["old"]
                    except Exception:
                        pass
                self._track("heal", ok=r.returncode == 0,
                            healed=out.count("HEALED:"),
                            stuck=out.count("STUCK:"))
                self.send_json({"ok": r.returncode == 0,
                                "healed": out.count("HEALED:"),
                                "stuck": out.count("STUCK:"),
                                "new_old": new_old,
                                "log": out[-4000:]})
            elif u.path == "/api/undo":
                d = self.project_dir({"name": [body.get("project", "")]})
                hist = d / ".history"
                snaps = sorted(hist.glob("*.json")) if hist.exists() else []
                if not snaps:
                    return self.fail("nothing to undo", 404)
                snap = json.loads(snaps[-1].read_text(encoding="utf-8"))
                for fn, content in snap.items():
                    if fn in ("copy_map.json", "forge.json",
                              "project_plan.md"):
                        (d / fn).write_text(content, encoding="utf-8")
                snaps[-1].unlink()
                self.send_json({"ok": True, "remaining": len(snaps) - 1})
            elif u.path == "/api/preview":
                self.project_dir({"name": [body.get("project", "")]})
                self.send_json({"port": preview_start(body["project"])})
            elif u.path == "/api/entry/resolve":
                # map a picked element back to its copy_map entry;
                # create one if inventory missed it (budget computed
                # against the CMS blobs exactly like inventory does)
                d = self.project_dir({"name": [body.get("project", "")]})
                cm_f = d / "copy_map.json"
                cm = json.loads(cm_f.read_text(encoding="utf-8"))
                norm = lambda s: re.sub(r"\s+", " ", s or "").strip()
                out, created, found = None, False, None
                if body.get("kind") == "string":
                    section = "strings"
                    # candidates: leaf first, then ancestors (Framer
                    # splits text into spans) — pick the SMALLEST that
                    # maps to a real copy-map entry / source string.
                    raw = [norm(t) for t in (body.get("texts")
                           or [body.get("text", "")]) if norm(t)]
                    # DOM concatenates adjacent text blocks with NO space
                    # ("automatically.It gives…"); split at those glue
                    # points so each real paragraph is matchable
                    cands = []
                    for c in raw:
                        if c not in cands:
                            cands.append(c)
                        for pc in re.split(r"(?<=[.!?])(?=[A-Z])", c):
                            pc = pc.strip()
                            if pc and pc not in cands:
                                cands.append(pc)
                    text = cands[0] if cands else ""
                    idx = {norm(e["old"]): e for e in cm["strings"]}
                    idxn = {norm(e["new"]): e for e in cm["strings"]
                            if e.get("new")}
                    for c in cands:                 # smallest-first match
                        if c in idx:
                            out = idx[c]
                            break
                        if c in idxn:
                            out = idxn[c]
                            break
                    if out is not None and not out.get("flex") \
                            and " " in (out.get("old") or ""):
                        # inventory normalizes whitespace when it harvests
                        # (double spaces, hard wraps) — if the stored form
                        # no longer appears byte-for-byte in the source,
                        # exact replacement misses the CHUNKS and hydration
                        # reverts the edit. Upgrade to flexible matching.
                        cfgx = json.loads((d / "forge.json").read_text())
                        exact = any(
                            out["old"] in (d / "pristine" / pg).read_text(
                                encoding="utf-8", errors="ignore")
                            for pg in cfgx["pages"]) or any(
                            out["old"] in c2.read_text(
                                encoding="utf-8", errors="ignore")
                            for c2 in (d / "pristine" / "chunks").glob("*.mjs"))
                        if not exact:
                            out["flex"] = True
                            created = True      # forces the copy-map save
                    if out is not None and \
                            "picked-in-editor" in str(out.get("where", "")):
                        # re-edit of a picked entry: recompute where it
                        # lives so the split-text guard can fire again
                        cms = d / "pristine" / "cms"
                        blobs = [p.read_bytes() for p in
                                 cms.glob("*.framercms")] if cms.exists() else []
                        cfg = json.loads((d / "forge.json").read_text())
                        pat = re.compile(_flex_pat(out["old"]))
                        found = {
                            "html": any(pat.search(
                                (d / "pristine" / pg).read_text(
                                    encoding="utf-8", errors="ignore"))
                                for pg in cfg["pages"]),
                            "chunks": any(pat.search(
                                c2.read_text(encoding="utf-8", errors="ignore"))
                                for c2 in (d / "pristine" / "chunks").glob("*.mjs")),
                            "cms": any(re.search(pat.pattern.encode(), bl)
                                       for bl in blobs),
                        }
                    rotator = None
                    if out is None and cands:
                        cms = d / "pristine" / "cms"
                        blobs = [p.read_bytes() for p in
                                 cms.glob("*.framercms")] if cms.exists() else []
                        cfg = json.loads((d / "forge.json").read_text())
                        pages = [(d / "pristine" / pg).read_text(
                                 encoding="utf-8", errors="ignore")
                                 for pg in cfg["pages"]]
                        chunks = [c.read_text(encoding="utf-8", errors="ignore")
                                  for c in (d / "pristine" / "chunks").glob("*.mjs")]

                        def locate(t):
                            pat = re.compile(_flex_pat(t))
                            return {"html": any(pat.search(p) for p in pages),
                                    "chunks": any(pat.search(c) for c in chunks),
                                    "cms": any(re.search(pat.pattern.encode(), bl)
                                               for bl in blobs)}

                        def meaningful(t):
                            # skip per-char / single-word animation
                            # fragments; a real editable unit has spaces
                            # or is a substantial word
                            return (len(t) >= 4 and any(ch.isalpha() for ch in t)
                                    and (" " in t or len(t) >= 8))
                        # smallest MEANINGFUL candidate present in source
                        # (chunks hold the full string for split text)
                        chosen = None
                        for c in cands:
                            if not meaningful(c):
                                continue
                            f = locate(c)
                            if f["html"] or f["chunks"] or f["cms"]:
                                chosen, found = c, f
                                break
                        if chosen is None:          # nothing found — take
                            mc = [c for c in cands if meaningful(c)]  # the
                            chosen = (mc or cands)[-1]   # biggest text unit
                            found = locate(chosen)
                        text = chosen
                        b = chosen.encode()
                        out = {"old": chosen, "new": "",
                               "max_bytes": len(b) if any(b in bl for bl in blobs)
                               else None,
                               "scope": "all", "flex": True,
                               "where": ["picked-in-editor"]}
                        cm["strings"].append(out)
                        created = True

                        # ROTATING/TYPEWRITER text: the picked phrase is
                        # one of several cycled by the runtime (stored as
                        # text:`…` items in the chunk). Surface the WHOLE
                        # cycle so the owner edits every phrase at once
                        # instead of chasing the animation.
                        if found and found.get("chunks"):
                            for ctext in chunks:
                                m2 = re.search(re.escape(chosen), ctext)
                                if not m2:
                                    continue
                                hits = [(mm.start(), mm.group(1)) for mm in
                                        re.finditer(r"text:[`\"]([^`\"]{1,90})"
                                                    r"[`\"]", ctext)]
                                # contiguous group (gaps < 400 chars)
                                # containing the picked phrase
                                group, cur = [], []
                                for k, (pos2, ph) in enumerate(hits):
                                    if cur and pos2 - cur[-1][0] > 400:
                                        if any(p2[1] == chosen and
                                               abs(p2[0] - m2.start()) < 200
                                               for p2 in cur):
                                            group = cur
                                            break
                                        cur = []
                                    cur.append((pos2, ph))
                                if not group and cur and any(
                                        p2[1] == chosen and
                                        abs(p2[0] - m2.start()) < 200
                                        for p2 in cur):
                                    group = cur
                                phrases = []
                                for _, ph in group:
                                    if ph not in phrases:
                                        phrases.append(ph)
                                if len(phrases) > 1 and chosen in phrases:
                                    idx2 = {e["old"]: e for e in cm["strings"]}
                                    rotator = []
                                    for ph in phrases:
                                        ent = idx2.get(ph)
                                        if ent is None:
                                            ent = {"old": ph, "new": "",
                                                   "scope": "all", "flex": True,
                                                   "where": ["picked-in-editor",
                                                             "rotator"]}
                                            cm["strings"].append(ent)
                                        rotator.append({"old": ph,
                                                        "new": ent.get("new", "")})
                                break
                else:
                    section = "images"
                    urlbase = lambda s: (s or "").split("#")[0].split("?")[0] \
                        .replace(" ", "").strip()
                    src = urllib.parse.unquote(body.get("src") or "")
                    # strip local origins + the edit mount; padding spaces
                    # (%20) never belong in a path
                    src = re.sub(r"^https?://(?:127\.0\.0\.1|localhost)"
                                 r"(?::\d+)?", "", src)
                    src = re.sub(rf"^/edit/{re.escape(d.name)}", "", src)
                    src = urlbase(src)
                    for e in cm["images"]:
                        if urlbase(e["old"]) == src \
                                or urlbase(e.get("new")) == src:
                            out = e
                            break
                    if out is None and src:
                        out = {"old": src, "new": ""}
                        cm["images"].append(out)
                        created = True
                if out is None:
                    return self.fail("could not identify that element", 404)
                if created:
                    cm_f.write_text(json.dumps(cm, indent=1, ensure_ascii=False),
                                    encoding="utf-8")
                self.send_json({"section": section, "created": created,
                                "found": found,
                                "rotator": rotator if body.get("kind") ==
                                "string" else None, **out})
            elif u.path == "/api/entry/remove":
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                el = body.get("el") or {}
                cfg = json.loads((d / "forge.json").read_text())
                if cfg["platform"] == "framer":
                    # React re-creates deleted DOM (hydration breaks) —
                    # bake a permanent hide rule into every build instead.
                    # BLAST-RADIUS GUARD (the "Variant 1" incident): a
                    # generic Framer default name hides unrelated
                    # components site-wide — scope by unique classes or
                    # a unique ancestor, or refuse; never hide blindly.
                    pages_txt = [(d / "pristine" / pg).read_text(
                        encoding="utf-8", errors="ignore")
                        for pg in cfg["pages"]]

                    def audit(s):
                        return hide_selector_audit(s, pages_txt)

                    def class_sel(info):
                        cls = [c for c in (info.get("classes") or [])
                               if re.match(r"framer-[\w-]+$", c)]
                        return (info.get("tag", "div")
                                + "".join("." + c for c in cls)) \
                            if cls else None

                    sel = None
                    if el.get("id"):
                        sel = "#" + el["id"]
                    elif el.get("frname"):
                        cand = f'[data-framer-name="{el["frname"]}"]'
                        if not audit(cand)["risky"]:
                            sel = cand
                        else:
                            cs = class_sel(el)
                            if cs and audit(cs)["count"] == 1:
                                sel = cs
                            else:   # anchor on a unique ancestor
                                for anc in (el.get("ancestors") or []):
                                    asel = class_sel(anc)
                                    if asel and audit(asel)["count"] == 1:
                                        sel = f"{asel} {cand}"
                                        break
                            if sel is None:
                                return self.fail(
                                    f'"{el["frname"]}" is a generic Framer '
                                    "name used all over the site — hiding "
                                    "by it would remove unrelated elements "
                                    "too. Climb the breadcrumb chips to a "
                                    "more specific parent and remove that.")
                    elif el.get("classes"):
                        sel = el["tag"] + "".join("." + c
                                                  for c in el["classes"])
                    else:
                        return self.fail("no id/name/classes to target "
                                         "this element safely")
                    hs = cfg.get("hide_selectors", [])
                    if sel not in hs:
                        hs.append(sel)
                        cfg["hide_selectors"] = hs
                        (d / "forge.json").write_text(json.dumps(cfg, indent=2))
                    return self.send_json({"mode": "hidden", "sel": sel})
                if not el.get("id") and not el.get("classes"):
                    return self.fail("element has no id/classes — can't "
                                     "target it safely")
                cm_f = d / "copy_map.json"
                cm = json.loads(cm_f.read_text(encoding="utf-8"))
                cm.setdefault("remove", []).append(
                    {k: el.get(k) for k in
                     ("tag", "id", "classes", "index", "label")})
                cm_f.write_text(json.dumps(cm, indent=1, ensure_ascii=False),
                                encoding="utf-8")
                self.send_json({"mode": "deleted"})
            elif u.path == "/api/entry/set":
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                cm_f = d / "copy_map.json"
                cm = json.loads(cm_f.read_text(encoding="utf-8"))
                new = body.get("new", "")
                if "`" in new or "${" in new:
                    return self.fail("backticks and ${ are forbidden "
                                     "(strings land inside JS literals)")
                for e in cm.get(body.get("section", ""), []):
                    if e["old"] == body.get("old"):
                        mb = e.get("max_bytes")
                        if mb and len(new.encode()) > mb:
                            return self.fail(f"over CMS budget: "
                                             f"{len(new.encode())} > {mb} bytes"
                                             " — shorten the text")
                        e["new"] = new
                        cm_f.write_text(json.dumps(cm, indent=1,
                                        ensure_ascii=False), encoding="utf-8")
                        return self.send_json({"ok": True})
                self.fail("entry not found", 404)
            else:
                self.fail("not found", 404)
        except FileNotFoundError as e:
            self.fail(e, 404)
        except ValueError as e:
            self.fail(e, 400)      # e.g. AI settings not configured yet
        except Exception as e:
            self.fail(e, 500)

    def api_auth(self, path, body):
        email = (body.get("email") or "").strip()
        pw = body.get("password") or ""
        if not email or not pw:
            return self.fail("email and password required")
        try:
            res = (cloud.signup if path.endswith("signup") else cloud.login)(
                email, pw)
        except Exception as e:
            return self.fail(str(e), 401)
        user = cloud.user_of(res)
        if not user.get("id"):
            # signup with email-confirmation on: no session yet
            return self.send_json({"ok": True, "confirm": True})
        user["plan"] = cloud.plan_of(user["token"], user["id"])
        if not cloud.entitled(user["plan"]):
            cloud.track("login_blocked", token=user["token"], step="billing")
            return self.fail("Your free beta access has ended — upgrade "
                             "to Pro to keep using Aethron.", 402)
        tok = secrets.token_urlsafe(24)
        # THE EMAIL LOGIN PATH — the one people actually use — minted a
        # session but sent a cookie with NO Max-Age, i.e. a session
        # cookie the window discards on quit. That is why a stored
        # session sat on disk next to a login prompt: the server
        # remembered, the browser was never told to. It also wrote the
        # session only to memory, so it did not survive a restart either.
        user["_seen"] = time.time()
        SESSIONS[tok] = user
        _sessions_save()
        cloud.track("login", token=user["token"],
                    source=path.rsplit("/", 1)[-1])
        body_out = json.dumps({"ok": True, "email": user["email"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        _ck = f"aethron_sess={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={30*86400}"
        _clog(f"SET on login -> {_ck[:34]}… Max-Age={30*86400}")
        self.send_header("Set-Cookie", _ck)
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _track(self, event, **props):
        # telemetry with the current session's token (cloud mode); a
        # no-op locally. Never raises into the request.
        try:
            s = self._session()
            cloud.track(event, token=(s or {}).get("token", ""), **props)
        except Exception:
            pass

    def api_create(self, body):
        raw = body.get("name", "")
        name = re.sub(r"[^\w-]+", "-", raw.strip().lower()).strip("-")
        if not name:
            return self.fail("give the project a name")
        if (PROJECTS / name).exists():
            return self.fail(f"project '{name}' already exists")
        PROJECTS.mkdir(parents=True, exist_ok=True)
        # a LIVE URL: forge scrapes the home page + same-host routes
        url = (body.get("url") or "").strip()
        if url:
            if not url.startswith(("http://", "https://")):
                return self.fail("url must start with http(s)://")
            r = subprocess.run(
                forge_argv("init", url, "--name", name),
                cwd=PROJECTS, capture_output=True, text=True)
            if r.returncode:
                return self.fail((r.stdout + r.stderr).strip() or "init failed")
            self._track_created(name, "url")
            return self.send_json({"name": name, "log": r.stdout})
        # single file, a zip, or MANY loose files (scattered per-page
        # saves) — init normalizes whatever lands in the temp dir
        files = body.get("files") or [{
            "filename": body.get("filename", "export.html"),
            "data_b64": body.get("data_b64", "")}]
        with tempfile.TemporaryDirectory() as td:
            for fd in files:
                fn = Path(fd.get("filename", "file")).name
                data = base64.b64decode(fd.get("data_b64", ""))
                if not data:
                    return self.fail(f"empty upload: {fn}")
                (Path(td) / fn).write_bytes(data)
            entries = list(Path(td).iterdir())
            if len(entries) == 1 and entries[0].suffix.lower() == ".zip":
                ex = Path(td) / "unzipped"
                zipfile.ZipFile(entries[0]).extractall(ex)
                target = ex
            elif len(entries) == 1:
                target = entries[0]
            else:
                target = Path(td)
            r = subprocess.run(
                forge_argv("init", str(target),
                 "--name", name),
                cwd=PROJECTS, capture_output=True, text=True)
        if r.returncode:
            return self.fail((r.stdout + r.stderr).strip() or "init failed")
        self._track_created(name, "upload")
        self.send_json({"name": name, "log": r.stdout})

    def _track_created(self, name, source):
        try:
            cfg = json.loads((PROJECTS / name / "forge.json").read_text())
            self._track("project_created", platform=cfg.get("platform"),
                        pages=len(cfg.get("pages", [])), source=source)
        except Exception:
            pass


# ───────────────────────── the app (single page) ─────────────────────

# The Aethron mark (white on transparent), inlined so every surface —
# login, splash, app shell — shows the real logo with no extra request
# and no external file. Regenerate with tools/make_brand.py if it changes.
MARK_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAKAAAACgCAQAAAAhxq+mAAAMeElEQVR42u2da3BU5RnH"
    "f/vuybKEFELAJICINF6iXOxIUMHLOPVSO2qtVG2pitZqoYMtWK2a0Rmt2o5j9YOtTj+o"
    "Y6f2otOOOjrjII5a+SDeBUQuogYvISRAjAom2T17nn7YZbPZ7Gb3ZM+ze3a7784w7AXO"
    "Oc/5P+//fZ7n/z4nIFRHIcNUTVA1YNWAVQNWDVgdVQNWDVg14P/lsIp8vGZWMIBNgHEE"
    "CWITxQABBJsYgoPBAgLYQACQxHeBlNvuAEI/DiFqAYcQQcDmYfoq14ABnuZE5WMczTVF"
    "vaSihnKH8QkDHt40J20acgCbZr6uVAQuTTqo1higlnN5vDIRaPExM3FUicvGYjPHJbBZ"
    "YSy8gJlElI9oYTOXwypzGXNdUY7n4LCsEl24gT04RZhzHQx9NBGpNARekFi96V9RhHra"
    "Ks2FDTcXjfMNcH2lufCxvE+EUJGuysHQTHclIfD64i0sABtYXkkIrOPLos63DoYeZhaD"
    "SIpzUZdgsCnm4ixCIwsrBYFBOtQjkEwRybP8oDIMuJA3ikggqSacxu5KcOGbS5K4dYCl"
    "lYDAevZgSmJAQzcziZY7As/DKlZYNYJImji+3F3Y0A5Fn/+Gru3GcnfheWwqAYGkZmYa"
    "+aKcEbi6qBFIJiZeUs4InEAfpoTFUwfDNuZo3kTdSzsTC7uEtWdDhFbml6sLB7jDF6X7"
    "1eXqwofTgV300v1IN7Zp0iu2a+JjZbJyS0mJJMSPyhGB49hNfZFTCNmIZAvztG6l3uWd"
    "Sr16ETO/K7Q5liPLzYUD3OYb7ZeDw4pyc+HZfOwDAhkyYYQpfFNOCFyO4wMCGSKSMGeX"
    "EwLH00vYBwSSGtK9yYlIuSDwdMK+IBBSFDMLdYjEqBDIH0qgfc2dn15VLi48i50+IpAh"
    "Aw7QyIFyQODVviKQodVgLWeUAwJr2E2DjwgklUhe42Svb633l3kaDb4ikFQiOcl7IvH+"
    "Qm/w7e4TB8d7Bb/XLtxMl+8IZLiGf7K3EYnXWLkooY3Cl7uybEIs8jMCg2ynxYcEkkok"
    "L3OGlxGJtwacy3slLGLma0JPpZfeYqUdvw8HuNKvLjyZXh+7b6pi5lDv5mkvL3cJlEQF"
    "414xc7IfEWj4kNm+R2B8FnzBu+ygdwacw2afE0iqCWfyud9ceDXlMhzgUr8hcBI9WGXS"
    "QsBT6aXxjEBCJVXBuCeShX5CYJAdZUEgqbPgWr7nHwO28WaZEEiqG3ui4TcVnsIabTPY"
    "T/yCwAb2+TaFNRoCOziSmB8Q+ENfqLDcE8lsLzT8xoNU+R2+K2LiQoBXchcuPwJJTfE3"
    "0VtqBK6iXIeNxUWlRuAE9hJSZWC91aWD4SNaC0ttFXpyZxNWroEYNImkhTmlPL0A7aoE"
    "4gC9qgxfsPSyMBf+Nh8prgAdDMfjsEH1GDCZr0qFwCsVV4A28ALvsokOLLWjRDCFKWYK"
    "QWCYPdSqzVIRQrTxNrCUfzJA2J/NykxBOvw6tRSWQ4iPeBeAZ7DV1pnxZmXzSuPCtyhy"
    "pA3cl8DFAR7FKJarnEL6HI3dhafTqbxGm5KMEo5hi2K0U9BmMFNQHGmr4c/wYkqQtZ0d"
    "hBTpKsQFxUbgOHqoUyWQ03mFVNXrQ2oYdDB8wLFjS22ZMUcgE1UJpI/1wz57SnHBbohw"
    "FHOL6cIBZQJxuC+NNPbxonL7qBXFdGHdncA2Fi18nPbpd3lRmUimsL9YCLxKNQKxWEfH"
    "iM/X0aVIJBFCnFYsFx7PKsVuqDZwTwYJpM39irxvAe0pzeZVXfj7PKfIiLCfZvozfHcI"
    "PcoTxxgUM2aMKhijuAJ8LKP5YC/rsNQw6DCmrpfuEdhItyIOIoSYy/tZvl3Eq6qrwT6a"
    "GdRG4I8VCcQhxAa2ZP3+LXrViCTePvkMbRcOcpPikjYbgRwcUe7CUXNiA9zglkjcuvBx"
    "bFBdjUWYOuqOyul0KhPJbHZqIrBdkUAiGP6aY0PqLtYoE8mlmgjU7UYZIUQr28m1mfEV"
    "VSJxKb00LglE8+6H2MyOnL9bT6dajSQuvWzTcmF9ArkzD8NE+bNiWsEAt2q5sK4KJv9w"
    "fgafKxPJDHZpIPDXaOpUDI/mmQ3ZxSvKRLJSA4ET+UI1hLM4hm3k21blZVUi2c/UfCOS"
    "/A1yoeLME6/Obs/79+vZrzaVGCLUsdhrF67hHkUCcYC7XOziHeRBHLVCp6sHuuTrwpoE"
    "4gAODa4ep3cU25UVM83s8RKBq5RTWH9z+TTCHbyqSCQ2hku8ROCkRI1Wr4j5HTbitjvD"
    "v9UUMw6GLmblE5HkZ5LzMWq9YBxCfJY1A5h9PMdXqqmtafk9RtXk9RvdCMTh9jE44zf8"
    "XTkiudErF9ZVpthYTKcLv7W4GK7OKQiBKxRFtjYWT4/JfPA+2wgpEgmc6wUC69iHpZrC"
    "WsRrY/zXV/GIakTyCS25FDO5DXg+zyiyHXTSkpHtavgWTUzBppcu9mdcZsc3WWitDmys"
    "PFYHMvrLyLsiEhWd0S8iK0ccMyDHyWPSPeyXb8kVMjHD+T0gIoNKZzcoIv/IYZ+cBjxK"
    "0XwiMYnJ5LQjHiEvJb8dlH4ZTBooJiukJu3XLcrnF5VJhRnwTxJTu8NREXkqDXs/T9z7"
    "qMTSLmVQYiLynjSnechWkbTfeovBqwox4ATpVzu5+OmdNMx8DySMN5rLD0rrsHNcpujE"
    "URHZKmbsBrxY8eRiIvK5WClHu11EDuS8pJhEpSnlX9VLTPEmR0Vk3mg2MqPKKH+rrMO/"
    "O2UVdwq3MUAtuVRUDhZrCSY/6eMJZenldWNdxhzJB8obuaayj4Obdrqoy7NkGiHEtTyY"
    "fD+fjaoRCUzN/mQwU7KNXIY1SfPBZS6ePWJhcy8Tku83K2v4zaidtrJ6d618oTi7DIrI"
    "qcljWbLT1bEGRWRpyrmuVF4rbMxOJNnv+bnUK2/kejX5voVZro5lGP7g0f8o6iUsbOZz"
    "hFsXjqewNAnkgZQos9Vl73MDnJoy6/XwnGqh08le6DRZH6iyQJFADPAEqfuO3SHI4FDP"
    "xJR56DbF+doCrma8OwNeo9gPP95Lt2tYxtEt2h0YluDYQKdiftqmlrPcGHAcKzGqNa/b"
    "Ga77H+tEMPT3O5Wllzdlll6arDuBNWsgvWkbuTpcO6AFaUrCZ5WJZDGz8zfgzcobue5P"
    "ywC+7XLCcIC9aVr+XTypGJE4wLJ8IxF9GW1647kWPnR1xHgh4MK0T0/gdWUN//SRGzAy"
    "4ewK5Y1ca0Z0Ev+UHleiSQf414hP36FDWcO/OB8XrmGVsgrmdyPS81EecbGNy8Fg83yG"
    "m/NHxU7WhsxtrkYEJ6eoprBi0j0iq4wgjS6OOigid2QMrJpU89NREZmeO5S7VlkFc2/G"
    "ElIPvyTEQN7Vst9n/K5bPSL5aS4ETh6RTPc6iXpIlrA8IGsTOefR/4cDIjI/awrkFOUU"
    "8K50/0nH2sXKmqdns4rGhAvYRJiBUWjABmpZzqasv3hdeTPYNBaMhkBLPlUu0SwatYQw"
    "Xp5J/DKWYQYaFJGYnJWjDNYusZw4LmQWXCuB7DWRE5QdYM+wGkhmR14iXydONl7SjL/i"
    "Bn1Kpuaq08oM5UKnDK8LDnfhXylHIH/JOT0IT9LEcrZhESaUfBnW0MYS9uY8TidrVach"
    "uCxbJDKZHsV40t2OcMM0FtBGI/1sZSPbXXQW0t1Rb+hkdspKIgWO1ylXWN8YPnuovcKy"
    "r2jFiBS81XC9j3T4hYwBHlSWXv4mkwufxHplueKkQjpFuhqH8YlyQbbx4HLMpOSgdWsg"
    "jxbNfPAZrysTyYXpCGygW3kn8JxReiF4P87m+eJILw+a7DwstTyGjcXbee+D82a8RA9G"
    "sUYy6+CuYpP4c7WqDt9wRZEfV2CzTNGJYUjDry6j7BeRu4uyfEl/PaYaV0WlYWgZc6ta"
    "DnqAMOu4hVKMX9BDSGlisrH42UESmciXKt1QHSKE2cyJ3j7RFzdPO36HaQwodPuPRySH"
    "YxvgTM+7ojnYRDCEeYi2kpkPdtPKGsIYBrA99TFDhBkcDwEJ8F8We4zAOB1t4bJEJ+hS"
    "jgCX8DB1yYSG8dCJH+fygMzlPc9Puo8neYg3C39ikUejlnO4nHMUdrs0BaSVawkmnj/u"
    "IMSIEMAQwBAigE0QhxiSdIEgYLAwWAgBIEIMsInyJTv5kK0ldFtG6fpwNIfSzCGECRNF"
    "CBIFanCowcEQw8EQIUKAmsS7foLUEQMsYgSJESNImAZqGaA9IFSHPx93UjVgdVQNWDVg"
    "1YBVA1ZH1YBVA1YNWMHjfzCNJtWxlhfBAAAAAElFTkSuQmCC")
MARK = "data:image/png;base64," + MARK_B64

# ══ METAL ═══════════════════════════════════════════════════════════
# metal-fx v2.0.0 — MIT, © 2026 Jakub Antalik
#   https://github.com/Jakubantalik/metal-fx
# carrying, inside it, the liquid-metal fragment shader from
# @paper-design/shaders — Apache-2.0, "Paper Shaders, Copyright 2026
# Paper, https://shaders.paper.design" — which is the one symbol
# metal-fx imports and its own build inlines.
#
# Their React component is not used; their index.ts exposes the engine
# primitives precisely "for consumers building non-React integrations",
# and scratchpad/metal/build_metal.py flattens their ES modules into
# this one script. Nothing about the effect is reimplemented.
#
# It is served at /metal.js rather than inlined into the page: 222KB
# has no business being re-sent with every render, and the browser
# caches a file with its own URL. It is EMBEDDED here rather than read
# from disk because a data file is one more thing that can be missing
# from the bundle, and this project has already found the shipped app
# stale three times.
METAL_JS = r"""/* THE WHOLE ENGINE LIVES IN ITS OWN SCOPE — see build_metal.py. */
(function(){
'use strict';
/* metal-fx v2.0.0 — MIT © 2026 Jakub Antalik (github.com/Jakubantalik/metal-fx).
   Flattened from their own ES modules by build_metal.py; the engine
   code below is theirs, unchanged apart from module syntax. */

/* ── @paper-design/shaders liquid-metal (Apache-2.0)
   Paper Shaders, Copyright 2026 Paper — https://shaders.paper.design
   The one symbol metal-fx imports, resolved from the published
   package exactly as their own build inlines it. */
const liquidMetalFragmentShader = "#version 300 es\nprecision mediump float;\n\nuniform sampler2D u_image;\nuniform float u_imageAspectRatio;\n\nuniform vec2 u_resolution;\nuniform float u_time;\n\nuniform vec4 u_colorBack;\nuniform vec4 u_colorTint;\n\nuniform float u_softness;\nuniform float u_repetition;\nuniform float u_shiftRed;\nuniform float u_shiftBlue;\nuniform float u_distortion;\nuniform float u_contour;\nuniform float u_angle;\n\nuniform float u_shape;\nuniform bool u_isImage;\n\nin vec2 v_objectUV;\nin vec2 v_responsiveUV;\nin vec2 v_responsiveBoxGivenSize;\nin vec2 v_imageUV;\n\nout vec4 fragColor;\n\n\n#define TWO_PI 6.28318530718\n#define PI 3.14159265358979323846\n\n\nvec2 rotate(vec2 uv, float th) {\n  return mat2(cos(th), sin(th), -sin(th), cos(th)) * uv;\n}\n\n\nvec3 permute(vec3 x) { return mod(((x * 34.0) + 1.0) * x, 289.0); }\nfloat snoise(vec2 v) {\n  const vec4 C = vec4(0.211324865405187, 0.366025403784439,\n    -0.577350269189626, 0.024390243902439);\n  vec2 i = floor(v + dot(v, C.yy));\n  vec2 x0 = v - i + dot(i, C.xx);\n  vec2 i1;\n  i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);\n  vec4 x12 = x0.xyxy + C.xxzz;\n  x12.xy -= i1;\n  i = mod(i, 289.0);\n  vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0))\n    + i.x + vec3(0.0, i1.x, 1.0));\n  vec3 m = max(0.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy),\n      dot(x12.zw, x12.zw)), 0.0);\n  m = m * m;\n  m = m * m;\n  vec3 x = 2.0 * fract(p * C.www) - 1.0;\n  vec3 h = abs(x) - 0.5;\n  vec3 ox = floor(x + 0.5);\n  vec3 a0 = x - ox;\n  m *= 1.79284291400159 - 0.85373472095314 * (a0 * a0 + h * h);\n  vec3 g;\n  g.x = a0.x * x0.x + h.x * x0.y;\n  g.yz = a0.yz * x12.xz + h.yz * x12.yw;\n  return 130.0 * dot(m, g);\n}\n\n\nfloat getColorChanges(float c1, float c2, float stripe_p, vec3 w, float blur, float bump, float tint) {\n\n  float ch = mix(c2, c1, smoothstep(.0, 2. * blur, stripe_p));\n\n  float border = w[0];\n  ch = mix(ch, c2, smoothstep(border, border + 2. * blur, stripe_p));\n\n  if (u_isImage == true) {\n    bump = smoothstep(.2, .8, bump);\n  }\n  border = w[0] + .4 * (1. - bump) * w[1];\n  ch = mix(ch, c1, smoothstep(border, border + 2. * blur, stripe_p));\n\n  border = w[0] + .5 * (1. - bump) * w[1];\n  ch = mix(ch, c2, smoothstep(border, border + 2. * blur, stripe_p));\n\n  border = w[0] + w[1];\n  ch = mix(ch, c1, smoothstep(border, border + 2. * blur, stripe_p));\n\n  float gradient_t = (stripe_p - w[0] - w[1]) / w[2];\n  float gradient = mix(c1, c2, smoothstep(0., 1., gradient_t));\n  ch = mix(ch, gradient, smoothstep(border, border + .5 * blur, stripe_p));\n\n  // Tint color is applied with color burn blending\n  ch = mix(ch, 1. - min(1., (1. - ch) / max(tint, 0.0001)), u_colorTint.a);\n  return ch;\n}\n\nfloat getImgFrame(vec2 uv, float th) {\n  float frame = 1.;\n  frame *= smoothstep(0., th, uv.y);\n  frame *= 1.0 - smoothstep(1. - th, 1., uv.y);\n  frame *= smoothstep(0., th, uv.x);\n  frame *= 1.0 - smoothstep(1. - th, 1., uv.x);\n  return frame;\n}\n\nfloat blurEdge3x3(sampler2D tex, vec2 uv, vec2 dudx, vec2 dudy, float radius, float centerSample) {\n  vec2 texel = 1.0 / vec2(textureSize(tex, 0));\n  vec2 r = radius * texel;\n\n  float w1 = 1.0, w2 = 2.0, w4 = 4.0;\n  float norm = 16.0;\n  float sum = w4 * centerSample;\n\n  sum += w2 * textureGrad(tex, uv + vec2(0.0, -r.y), dudx, dudy).r;\n  sum += w2 * textureGrad(tex, uv + vec2(0.0, r.y), dudx, dudy).r;\n  sum += w2 * textureGrad(tex, uv + vec2(-r.x, 0.0), dudx, dudy).r;\n  sum += w2 * textureGrad(tex, uv + vec2(r.x, 0.0), dudx, dudy).r;\n\n  sum += w1 * textureGrad(tex, uv + vec2(-r.x, -r.y), dudx, dudy).r;\n  sum += w1 * textureGrad(tex, uv + vec2(r.x, -r.y), dudx, dudy).r;\n  sum += w1 * textureGrad(tex, uv + vec2(-r.x, r.y), dudx, dudy).r;\n  sum += w1 * textureGrad(tex, uv + vec2(r.x, r.y), dudx, dudy).r;\n\n  return sum / norm;\n}\n\nfloat lst(float edge0, float edge1, float x) {\n  return clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0);\n}\n\nvoid main() {\n\n  const float firstFrameOffset = 2.8;\n  float t = .3 * (u_time + firstFrameOffset);\n\n  vec2 uv = v_imageUV;\n  vec2 dudx = dFdx(v_imageUV);\n  vec2 dudy = dFdy(v_imageUV);\n  vec4 img = textureGrad(u_image, uv, dudx, dudy);\n\n  if (u_isImage == false) {\n    uv = v_objectUV + .5;\n    uv.y = 1. - uv.y;\n  }\n\n  float cycleWidth = u_repetition;\n  float edge = 0.;\n  float contOffset = 1.;\n\n  vec2 rotatedUV = uv - vec2(.5);\n  float angle = (-u_angle + 70.) * PI / 180.;\n  float cosA = cos(angle);\n  float sinA = sin(angle);\n  rotatedUV = vec2(\n  rotatedUV.x * cosA - rotatedUV.y * sinA,\n  rotatedUV.x * sinA + rotatedUV.y * cosA\n  ) + vec2(.5);\n\n  if (u_isImage == true) {\n    float edgeRaw = img.r;\n    edge = blurEdge3x3(u_image, uv, dudx, dudy, 6., edgeRaw);\n    edge = pow(edge, 1.6);\n    edge *= mix(0.0, 1.0, smoothstep(0.0, 0.4, u_contour));\n  } else {\n    if (u_shape < 1.) {\n      // full-fill on canvas\n      vec2 borderUV = v_responsiveUV + .5;\n      float ratio = v_responsiveBoxGivenSize.x / v_responsiveBoxGivenSize.y;\n      vec2 mask = min(borderUV, 1. - borderUV);\n      vec2 pixel_thickness = min(250. / v_responsiveBoxGivenSize, vec2(.5));\n      float maskX = smoothstep(0.0, pixel_thickness.x, mask.x);\n      float maskY = smoothstep(0.0, pixel_thickness.y, mask.y);\n      maskX = pow(maskX, .25);\n      maskY = pow(maskY, .25);\n      edge = clamp(1. - maskX * maskY, 0., 1.);\n\n      uv = v_responsiveUV;\n      if (ratio > 1.) {\n        uv.y /= ratio;\n      } else {\n        uv.x *= ratio;\n      }\n      uv += .5;\n      uv.y = 1. - uv.y;\n\n      cycleWidth *= 2.;\n      contOffset = 1.5;\n\n    } else if (u_shape < 2.) {\n      // circle\n      vec2 shapeUV = uv - .5;\n      shapeUV *= .67;\n      edge = pow(clamp(3. * length(shapeUV), 0., 1.), 18.);\n    } else if (u_shape < 3.) {\n      // daisy\n      vec2 shapeUV = uv - .5;\n      shapeUV *= 1.68;\n\n      float r = length(shapeUV) * 2.;\n      float a = atan(shapeUV.y, shapeUV.x) + .2;\n      r *= (1. + .05 * sin(3. * a + 2. * t));\n      float f = abs(cos(a * 3.));\n      edge = smoothstep(f, f + .7, r);\n      edge *= edge;\n\n      uv *= .8;\n      cycleWidth *= 1.6;\n\n    } else if (u_shape < 4.) {\n      // diamond\n      vec2 shapeUV = uv - .5;\n      shapeUV = rotate(shapeUV, .25 * PI);\n      shapeUV *= 1.42;\n      shapeUV += .5;\n      vec2 mask = min(shapeUV, 1. - shapeUV);\n      vec2 pixel_thickness = vec2(.15);\n      float maskX = smoothstep(0.0, pixel_thickness.x, mask.x);\n      float maskY = smoothstep(0.0, pixel_thickness.y, mask.y);\n      maskX = pow(maskX, .25);\n      maskY = pow(maskY, .25);\n      edge = clamp(1. - maskX * maskY, 0., 1.);\n    } else if (u_shape < 5.) {\n      // metaballs\n      vec2 shapeUV = uv - .5;\n      shapeUV *= 1.3;\n      edge = 0.;\n      for (int i = 0; i < 5; i++) {\n        float fi = float(i);\n        float speed = 1.5 + 2./3. * sin(fi * 12.345);\n        float angle = -fi * 1.5;\n        vec2 dir1 = vec2(cos(angle), sin(angle));\n        vec2 dir2 = vec2(cos(angle + 1.57), sin(angle + 1.));\n        vec2 traj = .4 * (dir1 * sin(t * speed + fi * 1.23) + dir2 * cos(t * (speed * 0.7) + fi * 2.17));\n        float d = length(shapeUV + traj);\n        edge += pow(1.0 - clamp(d, 0.0, 1.0), 4.0);\n      }\n      edge = 1. - smoothstep(.65, .9, edge);\n      edge = pow(edge, 4.);\n    }\n\n    edge = mix(smoothstep(.9 - 2. * fwidth(edge), .9, edge), edge, smoothstep(0.0, 0.4, u_contour));\n\n  }\n\n  float opacity = 0.;\n  if (u_isImage == true) {\n    opacity = img.g;\n    float frame = getImgFrame(v_imageUV, 0.);\n    opacity *= frame;\n  } else {\n    opacity = 1. - smoothstep(.9 - 2. * fwidth(edge), .9, edge);\n    if (u_shape < 2.) {\n      edge = 1.2 * edge;\n    } else if (u_shape < 5.) {\n      edge = 1.8 * pow(edge, 1.5);\n    }\n  }\n\n  float diagBLtoTR = rotatedUV.x - rotatedUV.y;\n  float diagTLtoBR = rotatedUV.x + rotatedUV.y;\n\n  vec3 color = vec3(0.);\n  vec3 color1 = vec3(.98, 0.98, 1.);\n  vec3 color2 = vec3(.1, .1, .1 + .1 * smoothstep(.7, 1.3, diagTLtoBR));\n\n  vec2 grad_uv = uv - .5;\n\n  float dist = length(grad_uv + vec2(0., .2 * diagBLtoTR));\n  grad_uv = rotate(grad_uv, (.25 - .2 * diagBLtoTR) * PI);\n  float direction = grad_uv.x;\n\n  float bump = pow(1.8 * dist, 1.2);\n  bump = 1. - bump;\n  bump *= pow(uv.y, .3);\n\n\n  float thin_strip_1_ratio = .12 / cycleWidth * (1. - .4 * bump);\n  float thin_strip_2_ratio = .07 / cycleWidth * (1. + .4 * bump);\n  float wide_strip_ratio = (1. - thin_strip_1_ratio - thin_strip_2_ratio);\n\n  float thin_strip_1_width = cycleWidth * thin_strip_1_ratio;\n  float thin_strip_2_width = cycleWidth * thin_strip_2_ratio;\n\n  float noise = snoise(uv - t);\n\n  edge += (1. - edge) * u_distortion * noise;\n\n  direction += diagBLtoTR;\n  float contour = 0.;\n  direction -= 2. * noise * diagBLtoTR * (smoothstep(0., 1., edge) * (1.0 - smoothstep(0., 1., edge)));\n  direction *= mix(1., 1. - edge, smoothstep(.5, 1., u_contour));\n  direction -= 1.7 * edge * smoothstep(.5, 1., u_contour);\n  direction += .2 * pow(u_contour, 4.) * (1.0 - smoothstep(0., 1., edge));\n\n  bump *= clamp(pow(uv.y, .1), .3, 1.);\n  direction *= (.1 + (1.1 - edge) * bump);\n\n  direction *= (.4 + .6 * (1.0 - smoothstep(.5, 1., edge)));\n  direction += .18 * (smoothstep(.1, .2, uv.y) * (1.0 - smoothstep(.2, .4, uv.y)));\n  direction += .03 * (smoothstep(.1, .2, 1. - uv.y) * (1.0 - smoothstep(.2, .4, 1. - uv.y)));\n\n  direction *= (.5 + .5 * pow(uv.y, 2.));\n  direction *= cycleWidth;\n  direction -= t;\n\n\n  float colorDispersion = (1. - bump);\n  colorDispersion = clamp(colorDispersion, 0., 1.);\n  float dispersionRed = colorDispersion;\n  dispersionRed += .03 * bump * noise;\n  dispersionRed += 5. * (smoothstep(-.1, .2, uv.y) * (1.0 - smoothstep(.1, .5, uv.y))) * (smoothstep(.4, .6, bump) * (1.0 - smoothstep(.4, 1., bump)));\n  dispersionRed -= diagBLtoTR;\n\n  float dispersionBlue = colorDispersion;\n  dispersionBlue *= 1.3;\n  dispersionBlue += (smoothstep(0., .4, uv.y) * (1.0 - smoothstep(.1, .8, uv.y))) * (smoothstep(.4, .6, bump) * (1.0 - smoothstep(.4, .8, bump)));\n  dispersionBlue -= .2 * edge;\n\n  dispersionRed *= (u_shiftRed / 20.);\n  dispersionBlue *= (u_shiftBlue / 20.);\n\n  float blur = 0.;\n  float rExtraBlur = 0.;\n  float gExtraBlur = 0.;\n  if (u_isImage == true) {\n    float softness = 0.05 * u_softness;\n    blur = softness + .5 * smoothstep(1., 10., u_repetition) * smoothstep(.0, 1., edge);\n    float smallCanvasT = 1.0 - smoothstep(100., 500., min(u_resolution.x, u_resolution.y));\n    blur += smallCanvasT * smoothstep(.0, 1., edge);\n    rExtraBlur = softness * (0.05 + .1 * (u_shiftRed / 20.) * bump);\n    gExtraBlur = softness * 0.05 / max(0.001, abs(1. - diagBLtoTR));\n  } else {\n    blur = u_softness / 15. + .3 * contour;\n  }\n\n  vec3 w = vec3(thin_strip_1_width, thin_strip_2_width, wide_strip_ratio);\n  w[1] -= .02 * smoothstep(.0, 1., edge + bump);\n  float stripe_r = fract(direction + dispersionRed);\n  float r = getColorChanges(color1.r, color2.r, stripe_r, w, blur + fwidth(stripe_r) + rExtraBlur, bump, u_colorTint.r);\n  float stripe_g = fract(direction);\n  float g = getColorChanges(color1.g, color2.g, stripe_g, w, blur + fwidth(stripe_g) + gExtraBlur, bump, u_colorTint.g);\n  float stripe_b = fract(direction - dispersionBlue);\n  float b = getColorChanges(color1.b, color2.b, stripe_b, w, blur + fwidth(stripe_b), bump, u_colorTint.b);\n\n  color = vec3(r, g, b);\n  color *= opacity;\n\n  vec3 bgColor = u_colorBack.rgb * u_colorBack.a;\n  color = color + bgColor * (1. - opacity);\n  opacity = opacity + u_colorBack.a * (1. - opacity);\n\n  \n  color += 1. / 256. * (fract(sin(dot(.014 * gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453123) - .5);\n\n\n  fragColor = vec4(color, opacity);\n}\n";

/* ── engine/bend/config.ts ─────────────────────────────── */
/**
 * Live config for the cursor "bend" effect — a local liquid dent.
 *
 * The cursor carries a blob of displacement that rides the ring: moving
 * toward the button's centre dents the edge inward, moving away drags it
 * outward, and on release the bulge springs back. Implemented as an SVG
 * `feDisplacementMap` over the whole MetalFx root (button, ring, glow), with
 * the vector field regenerated each frame into a small canvas.
 *
 * Mutable singleton, read every frame by `useBend`.
 */
                             
                   
                                                                           
                                                    
                          
                                                                     
                   
                                                                          
                                                                          
                   
                                                                     
                    
                                                                            
                                                                            
                                                                              
                   
                                                                            
                
                                                           
               
                                                                             
                     
                                                                  
                  
                                                                         
                                        
               
                                                                            
                                                                       
                    
                                                                           
                                                                                 
                   
                                                                     
                
                                                                           
                                                                           
                                                                            
                                                                           
                 
                                                                           
                                                                        
                                                                               
                      
                                                                   
                          
                                                                          
                        
                                                             
                    
                                                          
                  
               
                                                                        
                                                                          
                                                              
                 
                                                                            
                                                                             
                 
                                                                            
                                                                             
                                                   
                 
 

const BEND_DEFAULTS                       = Object.freeze({
  enabled: true,
  applyTo: 'ring',
  strength: 0.74,
  fadeInMs: 200,
  fadeOutMs: 350,
  smoothMs: 140,
  reach: 36,
  blob: 13,
  liquidBlob: 10,
  maxDisp: 9,
  gain: 0.6,
  pressGain: 0.55,
  pullGain: 0.49,
  press: 5,
  liquid: 7.5,
  liquidReach: 8,
  liquidStiffness: 53,
  liquidDamping: 9,
  stiffness: 260,
  damping: 13,
  mass: 1,
  follow: 0.32,
  mapRes: 2,
  smooth: 0.25,
});

const BEND             = { ...BEND_DEFAULTS };

function setBendConfig(patch                     )       {
  Object.assign(BEND, patch);
}

function resetBendConfig()       {
  Object.assign(BEND, BEND_DEFAULTS);
}


/* ── engine/color.ts ─────────────────────────────── */
/** Converts `#rrggbb` (or `#rgb`) to a normalized `[r, g, b]` triple (0–1). */
function hexToRgb(hex        )                           {
  let h = hex.replace('#', '');
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  return [parseInt(h.slice(0, 2), 16) / 255, parseInt(h.slice(2, 4), 16) / 255, parseInt(h.slice(4, 6), 16) / 255];
}

/**
 * Converts `#rgb` / `#rgba` / `#rrggbb` / `#rrggbbaa` to a normalized
 * `[r, g, b, a]` quad (0–1). Alpha defaults to 1 when the hex omits it.
 *
 * Paper's shader takes colors as `vec4`, and for `u_colorTint` the alpha is a
 * blend *amount* (how much colour-burn to apply), not an opacity — so the
 * 8-digit form is the normal way to write a tint here, not an edge case.
 */
function hexToRgba(hex        )                                   {
  let h = hex.replace('#', '');
  if (h.length === 3 || h.length === 4) h = h.split('').map((c) => c + c).join('');
  const a = h.length >= 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1;
  return [
    parseInt(h.slice(0, 2), 16) / 255,
    parseInt(h.slice(2, 4), 16) / 255,
    parseInt(h.slice(4, 6), 16) / 255,
    a,
  ];
}

function rgbToHsv(r        , g        , b        )                           {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  const s = max === 0 ? 0 : d / max;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d + 6) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h /= 6;
  }
  return [h, s, max];
}

function hsvToRgb(h        , s        , v        )                           {
  const i = Math.floor(h * 6), f = h * 6 - i;
  const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  let r = 0, g = 0, b = 0;
  switch (i % 6) {
    case 0: r = v; g = t; b = p; break; case 1: r = q; g = v; b = p; break;
    case 2: r = p; g = v; b = t; break; case 3: r = p; g = q; b = v; break;
    case 4: r = t; g = p; b = v; break; case 5: r = v; g = p; b = q; break;
  }
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}


/* ── engine/perfConfig.ts ─────────────────────────────── */
// ─── Frame rates ──────────────────────────────────────────────────────────

// Main render loop: shader + canvas compositing. 66ms ≈ 15fps.
const FRAME_INTERVAL_MS = 66;

// Reflection repaint throttle. Matches main loop; CSS blur hides stepping.
const REFLECTION_INTERVAL_MS = 66;

// ─── Glow ─────────────────────────────────────────────────────────────────

// gl.readPixels interval for luminance sampling. GPU→CPU sync is expensive.
const GLOW_READBACK_INTERVAL_MS = 1500;

// Only run the glow callback every Nth rendered frame. Each update triggers
// Chrome to re-rasterize + re-blur 6 SVG paths through 2 masks.
const GLOW_SKIP_FRAMES = 1;

// Points sampled around the perimeter to find the brightest hotspot.
const PERIM_SAMPLES = 16;

// SVG path segment counts. Lower = shorter d-strings = less SVG parse work.
// Blur filters smooth out any polygon faceting.
const HALO_SEGMENTS = 16;
const EXTRA_SEGMENTS = 8;

// ─── GL canvas ────────────────────────────────────────────────────────────

// Base pixel size of the offscreen GL canvas (before DPR scaling).
// The plasma is inherently blurry — higher values waste fragment work.
const CANONICAL_GL_SIZE = 96;

// Cap devicePixelRatio for the GL canvas. 3x retina is wasted on soft plasma.
const GL_DPR_CAP = 2;


/* ── engine/presets.ts ─────────────────────────────── */
/**
 * Bundled preset configurations for the metal effect.
 *
 * These sit on top of Paper Shaders' `liquidMetal`, so the parameter set is
 * Paper's, not the old plasma engine's. Baseline values come from Paper's own
 * `fullScreenPreset` ("Backdrop") — the `shape: 'none'` variant, which fills
 * the frame with the material instead of masking it to a circle/daisy/diamond.
 * That's the mode we want: metal-fx carves the ring itself on the 2D canvas
 * (`punchInnerHole`), so the shader should hand us a full sheet of metal.
 *
 * A note on color, because it is the big behavioural change from the plasma
 * engine: Paper hardcodes the stripe endpoints inside the shader to
 * near-white (.98,.98,1.) and near-black (.1,.1,.1). There is no palette to
 * feed. All three presets therefore render the *same* silver material and
 * differ only in `colorTint`, which the shader applies as a colour-burn pass
 * weighted by the tint's alpha. `chromatic` is consequently an approximation
 * — the old 5-stop rainbow is not reproducible here.
 *
 * `colorBack` is composited *under* the material at its own alpha. Keep it
 * fully transparent for ring use, otherwise the punched-out centre fills in.
 *
 * `speed` is applied JS-side to `u_time` before upload (cheaper than a
 * uniform, and it matches how Paper's own mount drives time).
 */

                                                         
                                           

/** Paper's `LiquidMetalShapes`. Only `none` fills the frame. */
const SHAPE_NONE = 0;
const SHAPE_CIRCLE = 1;
const SHAPE_DAISY = 2;
const SHAPE_DIAMOND = 3;
const SHAPE_METABALLS = 4;

/** Paper's `ShaderFitOptions`. */
const FIT_NONE = 0;
const FIT_CONTAIN = 1;
const FIT_COVER = 2;

                             
                                                                               
                    
                                                                              
                                                     
                    
                                                                     
                
                                
                     
                                                      
                   
                                      
                   
                                      
                    
                                                         
                     
                                                                
                  
                                                     
                
                                                                                
                
                                
                
                                              
                   
                                       
                  
                  
                                                              
                  
                  
                                                                   
                     
                      
                                            
              
                                                                            
                                                                             
                                                                         
                        
 

                         
                   
                                         
 

/** Paper `fullScreenPreset` values, minus the opaque `#AAAAAC` backdrop. */
const BASE                                                  = {
  colorBack: '#00000000',
  speed: 1,
  repetition: 1.5,
  softness: 0.05,
  shiftRed: 0.3,
  shiftBlue: 0.3,
  distortion: 0.1,
  contour: 0.4,
  angle: 90,
  shape: SHAPE_NONE,
  scale: 1,
  rotation: 0,
  offsetX: 0,
  offsetY: 0,
  originX: 0.5,
  originY: 0.5,
  worldWidth: 0,
  worldHeight: 0,
  fit: FIT_CONTAIN,
};

const CHROMATIC         = {
  name: 'chromatic',
  modes: {
    // Cool blue burn with the dispersion pushed well past Paper's default —
    // the R/B channel split is the only knob that produces colour separation
    // in this shader, so it carries what the 5-stop palette used to do.
    dark: { ...BASE, colorTint: '#88ccff2e', shiftRed: 0.75, shiftBlue: 0.75, repetition: 2, softness: 0.09, shaderOpacity: 1 },
    light: { ...BASE, colorTint: '#66b0ff99', shiftRed: 0.6, shiftBlue: 0.6, shaderOpacity: 1 },
  },
};

const SILVER         = {
  name: 'silver',
  modes: {
    // White tint at low amount = Paper's material essentially untouched.
    dark: { ...BASE, colorTint: '#ffffff66', shaderOpacity: 0.88 },
    light: { ...BASE, colorTint: '#ffffff40', shaderOpacity: 1 },
  },
};

const GOLD         = {
  name: 'gold',
  modes: {
    dark: { ...BASE, colorTint: '#ffcc55cc', speed: 0.85, shaderOpacity: 0.92 },
    light: { ...BASE, colorTint: '#f7d488aa', shaderOpacity: 1 },
  },
};

const PRESETS                             = {
  chromatic: CHROMATIC,
  silver: SILVER,
  gold: GOLD,
};




/* ── engine/shaders.ts ─────────────────────────────── */
/**
 * Vertex + fragment shader for the metal-fx effect.
 *
 * Source-of-truth: Paper Shaders' `liquidMetal`, consumed unmodified from
 * `@paper-design/shaders` so the material stays byte-identical to what the
 * Paper editor previews and updates come in via npm.
 *
 * Paper's shaders are GLSL ES 3.00 / WebGL2 (`#version 300 es`, `out vec4
 * fragColor`), which is why the shared renderer asks for a `webgl2` context.
 * The fragment stage reads varyings (`v_objectUV`, `v_responsiveUV`,
 * `v_responsiveBoxGivenSize`, `v_imageUV`) produced by Paper's own vertex
 * shader, so the pair has to travel together — a bare full-screen-quad vertex
 * stage will link but render nothing.
 *
 * The vertex source is vendored below rather than imported: `@paper-design/
 * shaders` exposes it only through `ShaderMount`, which owns its own canvas
 * and RAF loop and would bypass this library's shared-renderer architecture
 * (one GL context feeding every instance). Copied verbatim from
 * paper-design/shaders `packages/shaders/src/vertex-shader.ts` @ 0.0.80,
 * Apache-2.0 — see NOTICE.
 *
 * IMPORTANT: `#version` must be the first characters of the source string.
 * Neither template literal below may start with a newline.
 *
 * Fragment uniforms (Paper's liquidMetal):
 *   u_resolution      vec2  — destination pixel buffer (DPR-scaled)
 *   u_time            float — seconds since boot, JS-side multiplied by speed
 *   u_pixelRatio      float — device pixel ratio the buffer was sized at
 *   u_colorBack       vec4  — backdrop RGBA, composited under the material
 *   u_colorTint       vec4  — tint RGBA, applied as color-burn (a = amount)
 *   u_repetition      float — stripe density (1..10)
 *   u_softness        float — stripe transition blur (0..1)
 *   u_shiftRed        float — R-channel dispersion (-1..1)
 *   u_shiftBlue       float — B-channel dispersion (-1..1)
 *   u_distortion      float — simplex-noise warp over the stripes (0..1)
 *   u_contour         float — edge-following strength (0..1)
 *   u_angle           float — pattern drift direction, degrees (0..360)
 *   u_shape           float — 0 none / 1 circle / 2 daisy / 3 diamond / 4 metaballs
 *   u_isImage         bool  — image-mask mode; always false here
 *   u_image           sampler2D — unused at u_isImage=false, 1×1 dummy bound
 *
 * Sizing uniforms consumed by the vertex stage: u_originX, u_originY,
 * u_worldWidth, u_worldHeight, u_fit, u_scale, u_rotation, u_offsetX,
 * u_offsetY, u_imageAspectRatio.
 *
 * Note the material itself is fixed: Paper hardcodes the stripe endpoints to
 * near-white and near-black, so all color comes from u_colorTint (burn) and
 * u_colorBack. There is no multi-stop palette to drive.
 */


/** Paper's sizing vertex stage. Vendored verbatim — see file header. */
const VERT_SHADER_SRC = /* glsl */ `#version 300 es
precision mediump float;

layout(location = 0) in vec4 a_position;

uniform vec2 u_resolution;
uniform float u_pixelRatio;
uniform float u_imageAspectRatio;
uniform float u_originX;
uniform float u_originY;
uniform float u_worldWidth;
uniform float u_worldHeight;
uniform float u_fit;
uniform float u_scale;
uniform float u_rotation;
uniform float u_offsetX;
uniform float u_offsetY;

out vec2 v_objectUV;
out vec2 v_objectBoxSize;
out vec2 v_responsiveUV;
out vec2 v_responsiveBoxGivenSize;
out vec2 v_patternUV;
out vec2 v_patternBoxSize;
out vec2 v_imageUV;

vec3 getBoxSize(float boxRatio, vec2 givenBoxSize) {
  vec2 box = vec2(0.);
  // fit = none
  box.x = boxRatio * min(givenBoxSize.x / boxRatio, givenBoxSize.y);
  float noFitBoxWidth = box.x;
  if (u_fit == 1.) { // fit = contain
    box.x = boxRatio * min(u_resolution.x / boxRatio, u_resolution.y);
  } else if (u_fit == 2.) { // fit = cover
    box.x = boxRatio * max(u_resolution.x / boxRatio, u_resolution.y);
  }
  box.y = box.x / boxRatio;
  return vec3(box, noFitBoxWidth);
}

void main() {
  gl_Position = a_position;

  vec2 uv = gl_Position.xy * .5;
  vec2 boxOrigin = vec2(.5 - u_originX, u_originY - .5);
  vec2 givenBoxSize = vec2(u_worldWidth, u_worldHeight);
  givenBoxSize = max(givenBoxSize, vec2(1.)) * u_pixelRatio;
  float r = u_rotation * 3.14159265358979323846 / 180.;
  mat2 graphicRotation = mat2(cos(r), sin(r), -sin(r), cos(r));
  vec2 graphicOffset = vec2(-u_offsetX, u_offsetY);


  // ===================================================

  float fixedRatio = 1.;
  vec2 fixedRatioBoxGivenSize = vec2(
  (u_worldWidth == 0.) ? u_resolution.x : givenBoxSize.x,
  (u_worldHeight == 0.) ? u_resolution.y : givenBoxSize.y
  );

  v_objectBoxSize = getBoxSize(fixedRatio, fixedRatioBoxGivenSize).xy;
  vec2 objectWorldScale = u_resolution.xy / v_objectBoxSize;

  v_objectUV = uv;
  v_objectUV *= objectWorldScale;
  v_objectUV += boxOrigin * (objectWorldScale - 1.);
  v_objectUV += graphicOffset;
  v_objectUV /= u_scale;
  v_objectUV = graphicRotation * v_objectUV;

  // ===================================================

  v_responsiveBoxGivenSize = vec2(
  (u_worldWidth == 0.) ? u_resolution.x : givenBoxSize.x,
  (u_worldHeight == 0.) ? u_resolution.y : givenBoxSize.y
  );
  float responsiveRatio = v_responsiveBoxGivenSize.x / v_responsiveBoxGivenSize.y;
  vec2 responsiveBoxSize = getBoxSize(responsiveRatio, v_responsiveBoxGivenSize).xy;
  vec2 responsiveBoxScale = u_resolution.xy / responsiveBoxSize;

  v_responsiveUV = uv;
  v_responsiveUV *= responsiveBoxScale;
  v_responsiveUV += boxOrigin * (responsiveBoxScale - 1.);
  v_responsiveUV += graphicOffset;
  v_responsiveUV /= u_scale;
  v_responsiveUV.x *= responsiveRatio;
  v_responsiveUV = graphicRotation * v_responsiveUV;
  v_responsiveUV.x /= responsiveRatio;

  // ===================================================

  float patternBoxRatio = givenBoxSize.x / givenBoxSize.y;
  vec2 patternBoxGivenSize = vec2(
  (u_worldWidth == 0.) ? u_resolution.x : givenBoxSize.x,
  (u_worldHeight == 0.) ? u_resolution.y : givenBoxSize.y
  );
  patternBoxRatio = patternBoxGivenSize.x / patternBoxGivenSize.y;

  vec3 boxSizeData = getBoxSize(patternBoxRatio, patternBoxGivenSize);
  v_patternBoxSize = boxSizeData.xy;
  float patternBoxNoFitBoxWidth = boxSizeData.z;
  vec2 patternBoxScale = u_resolution.xy / v_patternBoxSize;

  v_patternUV = uv;
  v_patternUV += graphicOffset / patternBoxScale;
  v_patternUV += boxOrigin;
  v_patternUV -= boxOrigin / patternBoxScale;
  v_patternUV *= u_resolution.xy;
  v_patternUV /= u_pixelRatio;
  if (u_fit > 0.) {
    v_patternUV *= (patternBoxNoFitBoxWidth / v_patternBoxSize.x);
  }
  v_patternUV /= u_scale;
  v_patternUV = graphicRotation * v_patternUV;
  v_patternUV += boxOrigin / patternBoxScale;
  v_patternUV -= boxOrigin;
  // x100 is a default multiplier between vertex and fragmant shaders
  // we use it to avoid UV presision issues
  v_patternUV *= .01;

  // ===================================================

  vec2 imageBoxSize;
  if (u_fit == 1.) { // contain
    imageBoxSize.x = min(u_resolution.x / u_imageAspectRatio, u_resolution.y) * u_imageAspectRatio;
  } else if (u_fit == 2.) { // cover
    imageBoxSize.x = max(u_resolution.x / u_imageAspectRatio, u_resolution.y) * u_imageAspectRatio;
  } else {
    imageBoxSize.x = min(10.0, 10.0 / u_imageAspectRatio * u_imageAspectRatio);
  }
  imageBoxSize.y = imageBoxSize.x / u_imageAspectRatio;
  vec2 imageBoxScale = u_resolution.xy / imageBoxSize;

  v_imageUV = uv;
  v_imageUV *= imageBoxScale;
  v_imageUV += boxOrigin * (imageBoxScale - 1.);
  v_imageUV += graphicOffset;
  v_imageUV /= u_scale;
  v_imageUV.x *= u_imageAspectRatio;
  v_imageUV = graphicRotation * v_imageUV;
  v_imageUV.x /= u_imageAspectRatio;

  v_imageUV += .5;
  v_imageUV.y = 1. - v_imageUV.y;
}`;

/** Paper's liquidMetal fragment stage, unmodified. */
const FRAG_SHADER_SRC         = liquidMetalFragmentShader;

/** Compile a single shader stage. Throws with the GL info log on failure. */
function compileShader(
  gl                        ,
  type        ,
  source        
)              {
  const shader = gl.createShader(type);
  if (!shader) throw new Error('metal-fx: gl.createShader returned null');
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`metal-fx: shader compile failed: ${info ?? '(no info log)'}`);
  }
  return shader;
}

/** Link a vertex + fragment shader pair into a complete program. */
function linkProgram(
  gl                        ,
  vert             ,
  frag             
)               {
  const program = gl.createProgram();
  if (!program) throw new Error('metal-fx: gl.createProgram returned null');
  gl.attachShader(program, vert);
  gl.attachShader(program, frag);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(program);
    gl.deleteProgram(program);
    throw new Error(`metal-fx: program link failed: ${info ?? '(no info log)'}`);
  }
  return program;
}


/* ── engine/renderer/core.ts ─────────────────────────────── */
/**
 * Shared WebGL renderer — one offscreen GL canvas drives all MetalFx instances.
 *
 * Architecture:
 *   1. A single offscreen GL canvas renders the plasma shader.
 *   2. Each instance owns a visible 2D canvas that receives a cropped/scaled
 *      copy of the GL output with an inner "hole punch" mask (ring effect).
 *   3. Glow sampling reads from a shared pixel buffer (gl.readPixels) that is
 *      refreshed at most every 200ms to avoid GPU pipeline flushes on every frame.
 *   4. The animation loop is capped at ~30fps — the blur + slow plasma motion
 *      makes higher rates imperceptible.
 */



const CANONICAL_PILL_W = 140;
const CANONICAL_PILL_H = 40;
const PILL_SHADER_SCALE = 1.6;
const CIRCLE_SHADER_SCALE = 1.3;

                                                              

/**
 * Vector deformation hook. Maps a point in the instance's CSS-px box (origin
 * top-left, before any overscan) to its displaced position, writing into
 * `out`. When an instance carries one, the ring mask is built from displaced
 * rounded-rect outlines instead of `roundRect`, so stretch stays anti-aliased
 * at any magnitude — unlike a pixel displacement filter.
 */
                                                                                     

/**
 * Extra layers drawn into the canvas while deforming — things that live on
 * CSS boxes (root background, `::after` rim, `.metal-fx-inner` hairline) and
 * therefore can't follow a vector deformation on their own.
 */
                               
                                                                    
                       
                                                                             
                                                               
                                                               
                                                                       
                                                                    
 

/**
 * Custom alpha mask. Paints opaque shapes in *device* px onto a context whose
 * origin is the instance's box top-left; the engine keeps the shader only
 * where the mask painted (`destination-in`). Replaces the ring punch — use it
 * for metal-filled text or glyphs.
 */
                                                                                                

                                  
                            
                                
                   
                    
                       
                          
                    
                      
                     
                                                                             
                                                                    
                   
                   
                                                                            
                                                                            
                  
                  
                                                                         
                                                                             
                                                               
                      
              
                                                                       
                                                                            
                                                                              
                
                            
                                                                             
                                                            
                           
                                                                        
                                                          
                           
                                                                              
                      
                                                                           
                                                                        
                                                                        
                                      
                   
                                                        
                          
                                    
                                                                          
                                                          
                   
                                                                            
                                                                             
                                                                             
                                                          
                                                                           
                                                                            
                                                    
                    
 

                                 
                                                
                             
                        
                      
                                                        
                                                                 
                                    
                     
                       
                       
                        
                                  
                  
                   
                            
                
              
                                  
                     
                               
                  
                   
                         
                      
                      
 

let SHARED                        = null;

let _supported                 = null;
/**
 * Whether this browser can run the engine (WebGL2). Cached after the first
 * call. Consumers get this for free through `<MetalFx>`, which renders its
 * children plain when unsupported instead of throwing.
 */
function isMetalFxSupported()          {
  if (_supported !== null) return _supported;
  if (typeof document === 'undefined') return (_supported = false);
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2')                                 ;
    _supported = !!gl;
    gl?.getExtension('WEBGL_lose_context')?.loseContext();
  } catch {
    _supported = false;
  }
  return _supported;
}

// Called by ensureSharedRenderer on first init and by the contextrestored
// listener to rebuild GL state after the browser reclaims the context.
let _onContextRestored                      = null;
function setContextRestoredCallback(cb                     )       {
  _onContextRestored = cb;
}

const UNIFORM_NAMES = [
  // Fragment stage (Paper liquidMetal)
  'u_resolution', 'u_time', 'u_pixelRatio',
  'u_colorBack', 'u_colorTint',
  'u_repetition', 'u_softness', 'u_shiftRed', 'u_shiftBlue',
  'u_distortion', 'u_contour', 'u_angle', 'u_shape', 'u_isImage', 'u_image',
  // Vertex stage (Paper sizing)
  'u_originX', 'u_originY', 'u_worldWidth', 'u_worldHeight',
  'u_fit', 'u_scale', 'u_rotation', 'u_offsetX', 'u_offsetY',
  'u_imageAspectRatio',
];

/**
 * Paper's shader writes premultiplied color (`color *= opacity` before the
 * backdrop composite), so the blend func has to be ONE / 1-SRC_ALPHA. Pairing
 * premultiplied output with the classic SRC_ALPHA factor double-darkens every
 * partially-transparent pixel — which on a 1px ring is the whole thing.
 */
function buildGLPipeline(gl                        )   
                        
                      
                                                        
                                    
  {
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

  const vert = compileShader(gl, gl.VERTEX_SHADER, VERT_SHADER_SRC);
  const frag = compileShader(gl, gl.FRAGMENT_SHADER, FRAG_SHADER_SRC);
  const program = linkProgram(gl, vert, frag);
  // biome-ignore lint/correctness/useHookAtTopLevel: WebGL method, not a React hook
  gl.useProgram(program);

  const buffer = gl.createBuffer();
  if (!buffer) throw new Error('metal-fx: gl.createBuffer returned null');
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
  const posLoc = gl.getAttribLocation(program, 'a_position');
  gl.enableVertexAttribArray(posLoc);
  // Paper declares `a_position` as vec4; feeding 2 floats leaves z=0, w=1,
  // which is exactly the full-screen quad the shader expects.
  gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

  const uniforms                                              = {};
  for (const n of UNIFORM_NAMES) uniforms[n] = gl.getUniformLocation(program, n);

  // `u_image` is dead at u_isImage=false, but an unbound sampler2D is
  // undefined behaviour and renders black on some drivers. Bind 1×1 opaque
  // black to texture unit 0 and leave it there for the life of the program.
  const dummyTexture = gl.createTexture();
  if (dummyTexture) {
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, dummyTexture);
    gl.texImage2D(
      gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
      new Uint8Array([0, 0, 0, 255])
    );
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    if (uniforms.u_image) gl.uniform1i(uniforms.u_image, 0);
  }

  return { program, buffer, uniforms, dummyTexture };
}

function ensureSharedRenderer()                 {
  if (SHARED) return SHARED;

  const dpr = Math.min(GL_DPR_CAP, typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1);
  const size = Math.round(CANONICAL_GL_SIZE * dpr);
  const useOffscreen = typeof OffscreenCanvas !== 'undefined';

  let glCanvas                                     ;
  let gl                               ;

  // WebGL2 is required, not preferred: Paper's shaders are `#version 300 es`
  // and use textureSize/fwidth/textureGrad. There is no WebGL1 fallback path.
  if (useOffscreen) {
    glCanvas = new OffscreenCanvas(size, size);
    gl = glCanvas.getContext('webgl2', {
      alpha: true, premultipliedAlpha: true, antialias: false,
    })                                 ;
  } else {
    const htmlCanvas = document.createElement('canvas');
    htmlCanvas.width = size;
    htmlCanvas.height = size;
    gl = htmlCanvas.getContext('webgl2', {
      alpha: true, premultipliedAlpha: true, antialias: false, preserveDrawingBuffer: true,
    })                                 ;
    glCanvas = htmlCanvas;
  }
  if (!gl) throw new Error('metal-fx: WebGL2 not supported');

  const { program, buffer, uniforms, dummyTexture } = buildGLPipeline(gl);

  const onContextLost = (e       ) => { e.preventDefault(); if (SHARED) SHARED.contextLost = true; };
  const onContextRestored = () => {
    if (!SHARED) return;
    const rebuilt = buildGLPipeline(SHARED.gl);
    SHARED.program = rebuilt.program;
    SHARED.buffer = rebuilt.buffer;
    SHARED.uniforms = rebuilt.uniforms;
    SHARED.dummyTexture = rebuilt.dummyTexture;
    SHARED.presetDirty = true;
    SHARED.contextLost = false;
    _onContextRestored?.();
  };
  glCanvas.addEventListener('webglcontextlost', onContextLost                 , false);
  glCanvas.addEventListener('webglcontextrestored', onContextRestored                 , false);

  SHARED = {
    glCanvas, gl, program, buffer, uniforms, dummyTexture,
    preset: PRESETS.chromatic.modes.dark, presetDirty: true,
    contextLost: false, useOffscreen, frameBitmap: null,
    startMs: performance.now(), pausedMs: 0, pausedAtMs: null,
    rafId: 0, dpr, instances: new Set(), frameCount: 0,
    glowQueue: [], glowIdx: 0, glowSkip: 0,
    glowPixels: new Uint8Array(size * size * 4),
    glowPixelsW: size, glowPixelsH: size,
  };
  return SHARED;
}

function teardownSharedRenderer()       {
  if (!SHARED) return;
  const { gl, program, buffer, frameBitmap, dummyTexture } = SHARED;
  try {
    frameBitmap?.close();
    gl.deleteBuffer(buffer);
    gl.deleteProgram(program);
    if (dummyTexture) gl.deleteTexture(dummyTexture);
    gl.getExtension('WEBGL_lose_context')?.loseContext();
  } catch { /* swallow */ }
  SHARED = null;
}


/* ── engine/renderer/outline.ts ─────────────────────────────── */
/**
 * Rounded-rect outline sampling shared by the canvas ring mask and the glow's
 * SVG mask, so both see the *same* deformed shape.
 *
 * Points go into a reusable flat `Float32Array` (x0,y0,x1,y1,…) — a bend
 * traces 6–8 outlines per frame at display rate, and allocating ~150 point
 * objects per outline was measurable GC churn.
 */
                                       

                                                           

const ARC_N = 14;
const EDGE_STEP = 1.5;
const _o = { x: 0, y: 0 };

function createOutlineBuf(capacity = 512)             {
  return { xy: new Float32Array(capacity * 2), n: 0 };
}

/**
 * Sample a rounded rect clockwise from the end of the top-left corner. Edges
 * every ~1.5 CSS px, 14 points per corner arc — dense enough that a gaussian
 * dent a few px wide stays smooth. `deform` (optional) displaces each point.
 */
function roundRectOutline(
  x        , y        , w        , h        , r        ,
  deform                 ,
  buf             = createOutlineBuf()
)             {
  r = Math.max(0, Math.min(r, Math.min(w, h) / 2));
  // Upper bound on point count: 4 arcs + 4 edges.
  const need = 4 * (ARC_N + 1) + Math.ceil((2 * (w + h)) / EDGE_STEP) + 8;
  if (buf.xy.length < need * 2) buf.xy = new Float32Array(need * 2);
  const xy = buf.xy;
  let n = 0;
  const push = (px        , py        ) => {
    if (deform) { deform(px, py, _o); xy[n * 2] = _o.x; xy[n * 2 + 1] = _o.y; }
    else { xy[n * 2] = px; xy[n * 2 + 1] = py; }
    n++;
  };
  const edge = (x0        , y0        , x1        , y1        ) => {
    const len = Math.hypot(x1 - x0, y1 - y0);
    const k = Math.max(1, Math.ceil(len / EDGE_STEP));
    for (let i = 0; i < k; i++) { const t = i / k; push(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t); }
  };
  const arc = (cx        , cy        , a0        , a1        ) => {
    for (let i = 0; i <= ARC_N; i++) {
      const a = a0 + (a1 - a0) * (i / ARC_N);
      push(cx + r * Math.cos(a), cy + r * Math.sin(a));
    }
  };
  edge(x + r, y, x + w - r, y);
  arc(x + w - r, y + r, -Math.PI / 2, 0);
  edge(x + w, y + r, x + w, y + h - r);
  arc(x + w - r, y + h - r, 0, Math.PI / 2);
  edge(x + w - r, y + h, x + r, y + h);
  arc(x + r, y + h - r, Math.PI / 2, Math.PI);
  edge(x, y + h - r, x, y + r);
  arc(x + r, y + r, Math.PI, 1.5 * Math.PI);
  buf.n = n;
  return buf;
}

/** SVG path data for an outline (closed). */
function outlinePathD(buf            )         {
  const { xy, n } = buf;
  if (n === 0) return '';
  let d = `M${xy[0].toFixed(2)} ${xy[1].toFixed(2)}`;
  for (let i = 1; i < n; i++) d += `L${xy[i * 2].toFixed(2)} ${xy[i * 2 + 1].toFixed(2)}`;
  return d + 'Z';
}


/* ── engine/renderer/sampling.ts ─────────────────────────────── */
/**
 * Pixel readback and luminance/colour sampling from the shared GL canvas.
 *
 * All glow luminance/color sampling reads from a shared pixel buffer
 * (SHARED.glowPixels). The buffer is refreshed via gl.readPixels at most
 * every GLOW_READBACK_INTERVAL_MS to avoid the expensive GPU→CPU pipeline
 * flush on every frame. The plasma shader evolves slowly so 200ms-stale
 * data is visually indistinguishable.
 */


let _lastReadbackMs = 0;

function ensureGlowPixels()       {
  if (!SHARED) return;
  const now = performance.now();
  if (now - _lastReadbackMs < GLOW_READBACK_INTERVAL_MS) return;
  _lastReadbackMs = now;
  const { gl, glCanvas } = SHARED;
  const cw = glCanvas.width, ch = glCanvas.height;
  if (SHARED.glowPixelsW !== cw || SHARED.glowPixelsH !== ch) {
    SHARED.glowPixelsW = cw;
    SHARED.glowPixelsH = ch;
    SHARED.glowPixels = new Uint8Array(cw * ch * 4);
  }
  gl.readPixels(0, 0, cw, ch, gl.RGBA, gl.UNSIGNED_BYTE, SHARED.glowPixels);
}

/**
 * Map per-instance CSS-px glow coordinates to the shared GL pixel buffer.
 *
 * The GL canvas is shared across all instances. Each instance "sees" a
 * different crop of it (computed identically to copyShaderToInstance).
 * This function reverses that mapping: given a CSS-px coordinate on the
 * instance, it returns the (bx, by) index into SHARED.glowPixels.
 *
 * readPixels stores rows bottom-up (GL convention) so Y is flipped.
 */
const _map = { bx: 0, by: 0 };

function mapToGlowBuf(inst                 , cssPxX        , cssPxY        )              {
  if (!SHARED) { _map.bx = 0; _map.by = 0; return _map; }
  const { glCanvas } = SHARED;
  const cw = glCanvas.width, ch = glCanvas.height;
  const dpr = inst.dpr;
  const dw = inst.cssWidth * dpr, dh = inst.cssHeight * dpr;
  const bdW = CANONICAL_PILL_W * dpr, bdH = CANONICAL_PILL_H * dpr;
  let srcW = (dw * (cw / bdW)) / inst.shaderScale;
  let srcH = (dh * (ch / bdH)) / inst.shaderScale;
  if (srcW > cw) srcW = cw;
  if (srcH > ch) srcH = ch;
  const sx = (cw - srcW) / 2;
  const sy = (ch - srcH) / 2;
  const glX = sx + (cssPxX / inst.cssWidth) * srcW;
  const glY = sy + (cssPxY / inst.cssHeight) * srcH;
  _map.bx = Math.round(glX);
  _map.by = Math.round(ch - 1 - glY);
  return _map;
}

const _sr = { r: 0, g: 0, b: 0, lum: 0, count: 0 };

function sampleRegion(
  buf            , W        , H        ,
  bx        , by        , radius        
)             {
  const r = Math.max(1, radius | 0);
  const x0 = Math.max(0, bx - r), x1 = Math.min(W, bx + r + 1);
  const y0 = Math.max(0, by - r), y1 = Math.min(H, by + r + 1);
  _sr.r = 0; _sr.g = 0; _sr.b = 0; _sr.lum = 0; _sr.count = 0;
  for (let py = y0; py < y1; py++) {
    const row = py * W;
    for (let px = x0; px < x1; px++) {
      const i = (row + px) * 4;
      _sr.r += buf[i]; _sr.g += buf[i + 1]; _sr.b += buf[i + 2];
      _sr.lum += (0.2126 * buf[i] + 0.7152 * buf[i + 1] + 0.0722 * buf[i + 2]) / 255;
      _sr.count++;
    }
  }
  return _sr;
}

const _rgb            = { r: 255, g: 255, b: 255 };

function sampleShaderLumAt(inst                 , cssPxX        , cssPxY        , radius        )         {
  if (!SHARED) return 0;
  ensureGlowPixels();
  const m = mapToGlowBuf(inst, cssPxX, cssPxY);
  const s = sampleRegion(SHARED.glowPixels, SHARED.glowPixelsW, SHARED.glowPixelsH, m.bx, m.by, radius);
  return s.count > 0 ? s.lum / s.count : 0;
}

function sampleShaderRGBAt(inst                 , cssPxX        , cssPxY        , radius        )            {
  if (!SHARED) { _rgb.r = 255; _rgb.g = 255; _rgb.b = 255; return _rgb; }
  ensureGlowPixels();
  const m = mapToGlowBuf(inst, cssPxX, cssPxY);
  const s = sampleRegion(SHARED.glowPixels, SHARED.glowPixelsW, SHARED.glowPixelsH, m.bx, m.by, radius);
  if (s.count === 0) { _rgb.r = 255; _rgb.g = 255; _rgb.b = 255; return _rgb; }
  _rgb.r = s.r / s.count; _rgb.g = s.g / s.count; _rgb.b = s.b / s.count;
  return _rgb;
}

function sampleShaderRGBChromatic(inst                 , cssPxX        , cssPxY        , radius        )            {
  if (!SHARED) { _rgb.r = 255; _rgb.g = 255; _rgb.b = 255; return _rgb; }
  ensureGlowPixels();
  const m = mapToGlowBuf(inst, cssPxX, cssPxY);
  const { glowPixels: buf, glowPixelsW: W, glowPixelsH: H } = SHARED;
  const r = Math.max(1, radius | 0);
  const x0 = Math.max(0, m.bx - r), x1 = Math.min(W, m.bx + r + 1);
  const y0 = Math.max(0, m.by - r), y1 = Math.min(H, m.by + r + 1);
  let bestScore = -1;
  _rgb.r = 255; _rgb.g = 255; _rgb.b = 255;
  for (let py = y0; py < y1; py++) {
    const row = py * W;
    for (let px = x0; px < x1; px++) {
      const i = (row + px) * 4;
      const rr = buf[i], gg = buf[i + 1], bb = buf[i + 2];
      const maxC = Math.max(rr, gg, bb), minC = Math.min(rr, gg, bb);
      const sat = maxC > 0 ? (maxC - minC) / maxC : 0;
      const score = sat * (0.35 + 0.65 * (maxC / 255));
      if (score > bestScore) { bestScore = score; _rgb.r = rr; _rgb.g = gg; _rgb.b = bb; }
    }
  }
  return _rgb;
}

const _pk = { r: 255, g: 255, b: 255, lum: 0 };

/** Brightest pixel in the window (not the mean) — for "is the ring shining
 *  here" questions, where a dark stripe next to a bright one should still
 *  read as lit. */
function sampleShaderPeakAt(inst                 , cssPxX        , cssPxY        , radius        )             {
  _pk.r = 255; _pk.g = 255; _pk.b = 255; _pk.lum = 0;
  if (!SHARED) return _pk;
  ensureGlowPixels();
  const m = mapToGlowBuf(inst, cssPxX, cssPxY);
  const { glowPixels: buf, glowPixelsW: W, glowPixelsH: H } = SHARED;
  const r = Math.max(1, radius | 0);
  const x0 = Math.max(0, m.bx - r), x1 = Math.min(W, m.bx + r + 1);
  const y0 = Math.max(0, m.by - r), y1 = Math.min(H, m.by + r + 1);
  for (let py = y0; py < y1; py++) {
    const row = py * W;
    for (let px = x0; px < x1; px++) {
      const i = (row + px) * 4;
      const lum = (0.2126 * buf[i] + 0.7152 * buf[i + 1] + 0.0722 * buf[i + 2]) / 255;
      if (lum > _pk.lum) { _pk.lum = lum; _pk.r = buf[i]; _pk.g = buf[i + 1]; _pk.b = buf[i + 2]; }
    }
  }
  return _pk;
}


/* ── engine/renderer/loop.ts ─────────────────────────────── */
/** Animation loop, per-frame compositing, and instance lifecycle. */







// Restart the animation loop when the browser restores the GL context.
setContextRestoredCallback(() => {
  if (SHARED && SHARED.instances.size > 0 && SHARED.pausedAtMs === null) {
    startSharedLoop();
  }
});

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (!SHARED || SHARED.pausedAtMs !== null || SHARED.contextLost) return;
    if (document.hidden) {
      stopSharedLoop();
    } else if (SHARED.instances.size > 0) {
      startSharedLoop();
    }
  });
}

// ─── Instance lifecycle ───────────────────────────────────────────────────

                                 
                                
                   
                    
                       
                          
                       
                     
                      
                    
                   
                 
                            
                           
                           
                       
 

function createInstance(opts                       )                  {
  const renderer = ensureSharedRenderer();
  const ctx = opts.hostCanvas.getContext('2d', { alpha: true });
  if (!ctx) throw new Error('metal-fx: canvas 2D context unavailable');

  const scale = opts.scale ?? 1;
  const inst                  = {
    canvas: opts.hostCanvas, ctx,
    cssWidth: opts.cssWidth, cssHeight: opts.cssHeight,
    cornerRadius: opts.cornerRadius,
    kind: opts.kind,
    ringCssPx: opts.ringCssPx ?? (opts.kind === 'circle' ? 2 : 1) * scale,
    shaderScale: opts.shaderScale ?? (opts.kind === 'circle' ? CIRCLE_SHADER_SCALE : PILL_SHADER_SCALE) * scale,
    opacityMul: opts.opacityMul ?? 1,
    glowGain: opts.glowGain ?? 1,
    visible: true,
    paused: opts.paused ?? false,
    everCopied: false,
    dpr: typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1,
    scale,
    onAfterFrame: opts.onAfterFrame,
    onComposite: opts.onComposite,
    onFirstCopy: opts.onFirstCopy,
    mask: opts.mask ?? null,
    deform: null,
    deformLayers: null,
    overscan: 0,
    cursorLight: null,
    glowFast: false,
    rawCanvas: null,
    wantRaw: false,
  };
  resizeInstanceCanvas(inst);
  renderer.instances.add(inst);
  if (renderer.rafId === 0 && renderer.pausedAtMs === null) startSharedLoop();
  return inst;
}

function destroyInstance(inst                 )       {
  if (!SHARED) return;
  SHARED.instances.delete(inst);
  const qi = SHARED.glowQueue.indexOf(inst);
  if (qi !== -1) SHARED.glowQueue.splice(qi, 1);
  if (SHARED.instances.size === 0) { stopSharedLoop(); teardownSharedRenderer(); }
}

function registerGlowInstance(inst                 )       {
  if (!SHARED) return;
  if (!SHARED.glowQueue.includes(inst)) SHARED.glowQueue.push(inst);
}

function unregisterGlowInstance(inst                 )       {
  if (!SHARED) return;
  const i = SHARED.glowQueue.indexOf(inst);
  if (i !== -1) SHARED.glowQueue.splice(i, 1);
}

function updateInstance(
  inst                 ,
  patch                                                                                                                                                                            
)       {
  let dirty = false;
  if (patch.mask !== undefined) inst.mask = patch.mask;
  if (patch.cssWidth !== undefined && patch.cssWidth !== inst.cssWidth) { inst.cssWidth = patch.cssWidth; dirty = true; }
  if (patch.cssHeight !== undefined && patch.cssHeight !== inst.cssHeight) { inst.cssHeight = patch.cssHeight; dirty = true; }
  if (patch.cornerRadius !== undefined) inst.cornerRadius = patch.cornerRadius;
  if (patch.scale !== undefined) inst.scale = patch.scale;
  if (patch.kind !== undefined && patch.kind !== inst.kind) {
    inst.kind = patch.kind;
    if (patch.shaderScale === undefined) inst.shaderScale = (patch.kind === 'circle' ? CIRCLE_SHADER_SCALE : PILL_SHADER_SCALE) * inst.scale;
    if (patch.ringCssPx === undefined) inst.ringCssPx = (patch.kind === 'circle' ? 2 : 1) * inst.scale;
  }
  if (patch.shaderScale !== undefined) inst.shaderScale = patch.shaderScale;
  if (patch.ringCssPx !== undefined) inst.ringCssPx = patch.ringCssPx;
  if (patch.opacityMul !== undefined) inst.opacityMul = patch.opacityMul;
  if (patch.glowGain !== undefined) inst.glowGain = patch.glowGain;
  if (patch.paused !== undefined && patch.paused !== inst.paused) {
    inst.paused = patch.paused;
    // Unpausing should kick the loop if it had idled because every visible
    // instance was paused.
    if (!patch.paused && SHARED && SHARED.rafId === 0 && SHARED.pausedAtMs === null && !SHARED.contextLost) {
      startSharedLoop();
    }
  }
  if (dirty) resizeInstanceCanvas(inst);
}

function setInstanceVisible(inst                 , visible         )       {
  inst.visible = visible;
  if (visible && SHARED && SHARED.rafId === 0 && SHARED.pausedAtMs === null && !SHARED.contextLost) {
    startSharedLoop();
  }
}

/**
 * Attach (or clear) a vector deformation to the instance that owns `canvas`.
 * `overscan` grows the canvas by that many CSS px on every side so outward
 * bulges aren't clipped. Redraws immediately so a paused instance updates.
 */
function setInstanceDeform(
  canvas                   ,
  deform                 ,
  layers                      = null,
  overscan = 0
)          {
  const inst = findInstance(canvas);
  if (!inst) return false;
  inst.deform = deform;
  inst.deformLayers = deform ? layers : null;
  const o = deform ? Math.max(0, Math.round(overscan)) : 0;
  if (o !== inst.overscan) { inst.overscan = o; resizeInstanceCanvas(inst); }
  copyShaderToInstance(inst);
  return true;
}

/** Re-composite one instance now — for callers driving `deform` per frame at
 *  a higher rate than the shared 15 fps loop. */
function redrawInstance(canvas                   )       {
  const inst = findInstance(canvas);
  if (inst) copyShaderToInstance(inst);
}

function findInstance(canvas                   )                         {
  if (!SHARED) return null;
  for (const inst of SHARED.instances) if (inst.canvas === canvas) return inst;
  return null;
}

/** Set by `setSharedPresetMode`. While non-null it wins over the named
 *  presets, so a live tuning surface isn't fighting every `<MetalFx preset>`
 *  effect that re-runs on a theme toggle. */
let presetOverride                    = null;

function setSharedPreset(name            , theme             )       {
  const s = ensureSharedRenderer();
  s.preset = presetOverride ?? PRESETS[name].modes[theme];
  s.presetDirty = true;
}

/**
 * Push raw Paper liquidMetal parameters into the shared renderer, bypassing
 * the named presets. Pass `null` to hand control back to `preset` / `theme`.
 *
 * This exists for the playground: every instance shares one GL program, so
 * tuning is necessarily global rather than per-instance.
 */
function setSharedPresetMode(mode                   )       {
  const s = ensureSharedRenderer();
  presetOverride = mode;
  if (mode) {
    s.preset = mode;
    s.presetDirty = true;
  }
}

/** The preset the shared renderer is currently drawing with, or null before
 *  any instance has mounted. Read-only snapshot — mutate via the setters. */
function getSharedPreset()                    {
  return SHARED ? { ...SHARED.preset } : null;
}

function pauseShared()       {
  if (!SHARED || SHARED.pausedAtMs !== null) return;
  SHARED.pausedAtMs = performance.now();
  stopSharedLoop();
}

function resumeShared()       {
  if (!SHARED || SHARED.pausedAtMs === null) return;
  SHARED.pausedMs += performance.now() - SHARED.pausedAtMs;
  SHARED.pausedAtMs = null;
  if (SHARED.instances.size > 0) startSharedLoop();
}

function getSharedFrameCount()         {
  return SHARED?.frameCount ?? 0;
}

// ─── Glow callback ────────────────────────────────────────────────────────

/** Returns true when the glow wants per-frame ticks (mid-fade). */
                                                                                    
let _glowCallback                      = null;

function setGlowCallback(cb                     )       {
  _glowCallback = cb;
}

/** Run one glow update for an instance now — for drivers (cursor light) that
 *  need the hotspot to move at pointer rate rather than the shared 15 fps. */
function tickInstanceGlow(inst                 , nowMs        )       {
  if (!_glowCallback || !SHARED || !inst.visible || inst.paused) return;
  if (!SHARED.glowQueue.includes(inst)) return;
  inst.glowFast = !!_glowCallback(inst, nowMs);
}

// ─── Internal rendering ───────────────────────────────────────────────────

function resizeInstanceCanvas(inst                 )       {
  inst.dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
  const o = inst.overscan;
  const w = Math.max(1, Math.round((inst.cssWidth + 2 * o) * inst.dpr));
  const h = Math.max(1, Math.round((inst.cssHeight + 2 * o) * inst.dpr));
  if (inst.canvas.width !== w) inst.canvas.width = w;
  if (inst.canvas.height !== h) inst.canvas.height = h;
  // Overscan: grow the element past its box and drop the CSS radius clip so
  // displaced geometry outside the rounded box is visible.
  const st = inst.canvas.style;
  if (o > 0) {
    st.left = `${-o}px`; st.top = `${-o}px`;
    st.width = `calc(100% + ${2 * o}px)`; st.height = `calc(100% + ${2 * o}px)`;
    st.borderRadius = '0';
  } else if (st.left !== '') {
    st.left = ''; st.top = ''; st.width = '100%'; st.height = '100%'; st.borderRadius = '';
  }
}

function punchInnerHole(inst                 )       {
  const { ctx, dpr, canvas } = inst;
  const stroke = inst.ringCssPx * dpr;
  const w = canvas.width, h = canvas.height;
  const innerR = Math.max(0, (inst.cornerRadius - inst.ringCssPx) * dpr);
  ctx.save();
  ctx.globalCompositeOperation = 'destination-out';
  ctx.fillStyle = '#000';
  ctx.beginPath();
  ctx.roundRect(stroke, stroke, w - 2 * stroke, h - 2 * stroke, innerR);
  ctx.fill();
  ctx.restore();
}

const _outline             = createOutlineBuf();

/** Trace a (possibly deformed) rounded rect into the ctx, in device px. */
function traceDeformedRoundRect(
  ctx                          ,
  x        , y        , w        , h        , r        ,
  deform          , dpr        
)       {
  const { xy, n } = roundRectOutline(x, y, w, h, r, deform, _outline);
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    if (i === 0) ctx.moveTo(xy[0] * dpr, xy[1] * dpr);
    else ctx.lineTo(xy[i * 2] * dpr, xy[i * 2 + 1] * dpr);
  }
  ctx.closePath();
}

function copyShaderToInstance(inst                 )       {
  if (!SHARED) return;
  const src                    = SHARED.frameBitmap ?? SHARED.glCanvas;
  const dpr = inst.dpr;
  const dw = inst.canvas.width, dh = inst.canvas.height;
  if (dw < 1 || dh < 1) return;
  // Box = the element's own CSS box in device px; the canvas may be larger
  // by `overscan` on every side while deforming.
  const bw = Math.max(1, Math.round(inst.cssWidth * dpr));
  const bh = Math.max(1, Math.round(inst.cssHeight * dpr));
  const od = inst.overscan * dpr;

  const cw = SHARED.glCanvas.width, ch = SHARED.glCanvas.height;
  const bdW = CANONICAL_PILL_W * dpr, bdH = CANONICAL_PILL_H * dpr;
  let srcW = (bw * (cw / bdW)) / inst.shaderScale;
  let srcH = (bh * (ch / bdH)) / inst.shaderScale;
  if (srcW > cw) srcW = cw;
  if (srcH > ch) srcH = ch;
  const sx = Math.max(0, (cw - srcW) / 2);
  const sy = Math.max(0, (ch - srcH) / 2);

  // Paper's shader has no `u_shaderOpacity` equivalent, so the preset-level
  // opacity rides along with the per-instance `strength` multiplier here
  // instead of being applied on the GPU.
  const alpha = inst.opacityMul * SHARED.preset.shaderOpacity;
  const ctx = inst.ctx;

  ctx.clearRect(0, 0, dw, dh);

  const deform = inst.deform;
  if (inst.mask) {
    // ── Custom mask (metal text / glyph) ─────────────────────────────
    if (alpha < 1) ctx.globalAlpha = alpha;
    ctx.drawImage(src, sx, sy, srcW, srcH, 0, 0, dw, dh);
    if (alpha < 1) ctx.globalAlpha = 1;
    if (inst.wantRaw) {
      // Keep the sheet before it is cut to the glyphs, for reflections.
      let rc = inst.rawCanvas;
      if (!rc) { rc = document.createElement('canvas'); inst.rawCanvas = rc; }
      if (rc.width !== dw || rc.height !== dh) { rc.width = dw; rc.height = dh; }
      const rg = rc.getContext('2d');
      if (rg) { rg.clearRect(0, 0, dw, dh); rg.drawImage(inst.canvas, 0, 0); }
    }
    ctx.save();
    ctx.globalCompositeOperation = 'destination-in';
    ctx.fillStyle = '#000';
    inst.mask(ctx, dw, dh, dpr);
    ctx.restore();
    ctx.globalCompositeOperation = 'source-over';
  } else if (!deform) {
    if (alpha < 1) ctx.globalAlpha = alpha;
    ctx.drawImage(src, sx, sy, srcW, srcH, 0, 0, dw, dh);
    if (alpha < 1) ctx.globalAlpha = 1;
    punchInnerHole(inst);
  } else {
    // ── Vector-deformed ring ──────────────────────────────────────────
    // The shader texture is laid down over the (overscanned) canvas, then
    // masked to a displaced outer outline minus a displaced inner outline.
    // Because the mask is geometry, stretched regions stay crisp.
    const W = inst.cssWidth, H = inst.cssHeight;
    const R = inst.cornerRadius, ring = inst.ringCssPx;
    const layers = inst.deformLayers;
    ctx.save();
    ctx.translate(od, od);

    // Texture: over the box plus the overscan margin, so an outward bulge
    // still has shader pixels under it. The source crop grows by the same
    // ratio so the mapping *inside the box* is identical to the rigid path —
    // otherwise the pattern jumps scale the moment a bend starts or ends.
    // If the enlarged crop would exceed the GL buffer, shrink the *destination*
    // instead of the crop's scale — a scale change is a visible texture jump.
    const scX = bw / srcW, scY = bh / srcH;          // dest px per source px
    const esW = Math.min(cw, srcW * (bw + 2 * od) / bw);
    const esH = Math.min(ch, srcH * (bh + 2 * od) / bh);
    const esx = Math.max(0, (cw - esW) / 2);
    const esy = Math.max(0, (ch - esH) / 2);
    const dW = esW * scX, dH = esH * scY;
    if (alpha < 1) ctx.globalAlpha = alpha;
    ctx.drawImage(src, esx, esy, esW, esH, bw / 2 - dW / 2, bh / 2 - dH / 2, dW, dH);
    if (alpha < 1) ctx.globalAlpha = 1;

    ctx.globalCompositeOperation = 'destination-in';
    traceDeformedRoundRect(ctx, 0, 0, W, H, R, deform, dpr);
    ctx.fillStyle = '#000';
    ctx.fill();

    ctx.globalCompositeOperation = 'destination-out';
    traceDeformedRoundRect(ctx, ring, ring, W - 2 * ring, H - 2 * ring, Math.max(0, R - ring), deform, dpr);
    ctx.fill();

    if (layers?.hairline) {
      const hl = layers.hairline;
      ctx.globalCompositeOperation = 'destination-over';
      traceDeformedRoundRect(ctx, hl.inset, hl.inset, W - 2 * hl.inset, H - 2 * hl.inset, Math.max(0, R - hl.inset), deform, dpr);
      ctx.lineWidth = hl.width * dpr;
      ctx.strokeStyle = hl.color;
      ctx.stroke();
    }
    if (layers?.fill) {
      ctx.globalCompositeOperation = 'destination-over';
      traceDeformedRoundRect(ctx, 0, 0, W, H, R, deform, dpr);
      ctx.fillStyle = layers.fill;
      ctx.fill();
    }
    if (layers?.rim) {
      const rim = layers.rim;
      ctx.globalCompositeOperation = 'source-over';
      // Inset band of `width` starting `inset` in from the outline: stroke
      // its centre line, then clip to the outline so nothing spills out.
      ctx.save();
      traceDeformedRoundRect(ctx, 0, 0, W, H, R, deform, dpr);
      ctx.clip();
      const c = rim.inset + rim.width / 2;
      traceDeformedRoundRect(ctx, c, c, W - 2 * c, H - 2 * c, Math.max(0, R - c), deform, dpr);
      ctx.lineWidth = rim.width * dpr;
      ctx.strokeStyle = rim.color;
      ctx.stroke();
      ctx.restore();
    }
    ctx.restore();
    ctx.globalCompositeOperation = 'source-over';
  }

  inst.onComposite?.();
  if (inst.onFirstCopy) { const cb = inst.onFirstCopy; inst.onFirstCopy = undefined; cb(); }
  inst.onAfterFrame?.();
}

function uploadPresetUniforms()       {
  if (!SHARED) return;
  const { gl, uniforms, preset, glCanvas, dpr } = SHARED;

  // Shared by both stages.
  if (uniforms.u_resolution) gl.uniform2f(uniforms.u_resolution, glCanvas.width, glCanvas.height);
  // Paper's vertex stage divides the world box by this; leaving it at the
  // default 0 collapses the box and the shader renders nothing.
  if (uniforms.u_pixelRatio) gl.uniform1f(uniforms.u_pixelRatio, dpr);

  // Fragment — material.
  if (uniforms.u_colorBack) gl.uniform4fv(uniforms.u_colorBack, hexToRgba(preset.colorBack));
  if (uniforms.u_colorTint) gl.uniform4fv(uniforms.u_colorTint, hexToRgba(preset.colorTint));
  if (uniforms.u_repetition) gl.uniform1f(uniforms.u_repetition, preset.repetition);
  if (uniforms.u_softness) gl.uniform1f(uniforms.u_softness, preset.softness);
  if (uniforms.u_shiftRed) gl.uniform1f(uniforms.u_shiftRed, preset.shiftRed);
  if (uniforms.u_shiftBlue) gl.uniform1f(uniforms.u_shiftBlue, preset.shiftBlue);
  if (uniforms.u_distortion) gl.uniform1f(uniforms.u_distortion, preset.distortion);
  if (uniforms.u_contour) gl.uniform1f(uniforms.u_contour, preset.contour);
  if (uniforms.u_angle) gl.uniform1f(uniforms.u_angle, preset.angle);
  if (uniforms.u_shape) gl.uniform1f(uniforms.u_shape, preset.shape);
  // Procedural only — metal-fx never feeds a logo through the effect.
  if (uniforms.u_isImage) gl.uniform1i(uniforms.u_isImage, 0);
  if (uniforms.u_imageAspectRatio) gl.uniform1f(uniforms.u_imageAspectRatio, 1);

  // Vertex — sizing.
  if (uniforms.u_originX) gl.uniform1f(uniforms.u_originX, preset.originX);
  if (uniforms.u_originY) gl.uniform1f(uniforms.u_originY, preset.originY);
  if (uniforms.u_worldWidth) gl.uniform1f(uniforms.u_worldWidth, preset.worldWidth);
  if (uniforms.u_worldHeight) gl.uniform1f(uniforms.u_worldHeight, preset.worldHeight);
  if (uniforms.u_fit) gl.uniform1f(uniforms.u_fit, preset.fit);
  if (uniforms.u_scale) gl.uniform1f(uniforms.u_scale, preset.scale);
  if (uniforms.u_rotation) gl.uniform1f(uniforms.u_rotation, preset.rotation);
  if (uniforms.u_offsetX) gl.uniform1f(uniforms.u_offsetX, preset.offsetX);
  if (uniforms.u_offsetY) gl.uniform1f(uniforms.u_offsetY, preset.offsetY);

  SHARED.presetDirty = false;
}

function renderSharedFrame(now        )       {
  if (!SHARED) return;
  const { gl, uniforms, preset, glCanvas } = SHARED;
  const t = ((now - SHARED.startMs - SHARED.pausedMs) / 1000) * preset.speed;

  gl.viewport(0, 0, glCanvas.width, glCanvas.height);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT);

  if (SHARED.presetDirty) uploadPresetUniforms();
  if (uniforms.u_time) gl.uniform1f(uniforms.u_time, t);

  gl.drawArrays(gl.TRIANGLES, 0, 6);
  SHARED.frameCount++;
}

let lastFrameMs = 0;

function tick(now        )       {
  if (!SHARED) return;
  if (SHARED.contextLost) { SHARED.rafId = 0; return; }

  // Loop stays alive while at least one visible instance still has work to do
  // — i.e. it's either unpaused (needs a fresh copy each frame) or paused but
  // hasn't yet painted its first frame (initial-mount-paused case).
  let anyWork = false;
  for (const inst of SHARED.instances) {
    if (inst.visible && (!inst.paused || !inst.everCopied)) { anyWork = true; break; }
  }
  if (!anyWork) { SHARED.rafId = 0; return; }

  SHARED.rafId = requestAnimationFrame(tick);
  if (now - lastFrameMs < FRAME_INTERVAL_MS) {
    // Between shader frames, keep fading glows moving at display rate.
    if (_glowCallback) {
      for (const inst of SHARED.glowQueue) {
        if (inst.glowFast && inst.visible && !inst.paused) inst.glowFast = !!_glowCallback(inst, now);
      }
    }
    return;
  }
  lastFrameMs = now;

  renderSharedFrame(now);

  if (SHARED.useOffscreen) {
    if (SHARED.glowQueue.length > 0) ensureGlowPixels();
    SHARED.frameBitmap?.close();
    SHARED.frameBitmap = (SHARED.glCanvas                   ).transferToImageBitmap();
  }

  for (const inst of SHARED.instances) {
    if (!inst.visible) continue;
    if (inst.paused && inst.everCopied) continue;
    copyShaderToInstance(inst);
    inst.everCopied = true;
  }

  // Every visible glow instance, every Nth tick. Round-robin (one instance
  // per tick) made each halo's update rate depend on how many rings were on
  // the page — five rings meant ~330 ms between updates, so a 300 ms fade
  // landed in a single step and read as a flash. updateGlow costs ~0.05 ms.
  if (_glowCallback && SHARED.glowQueue.length > 0 && ++SHARED.glowSkip % GLOW_SKIP_FRAMES === 0) {
    for (const inst of SHARED.glowQueue) {
      // Skip paused instances so their halo also freezes (otherwise the
      // catch-light would keep travelling on a frozen ring).
      if (inst.visible && !inst.paused) inst.glowFast = !!_glowCallback(inst, now);
    }
  }
}

function startSharedLoop()       {
  if (!SHARED || SHARED.rafId !== 0) return;
  SHARED.rafId = requestAnimationFrame(tick);
}

function stopSharedLoop()       {
  if (!SHARED) return;
  if (SHARED.rafId !== 0) cancelAnimationFrame(SHARED.rafId);
  SHARED.rafId = 0;
}


/* ── engine/cursor/light.ts ─────────────────────────────── */
/**
 * Cursor light — the pointer and the ring lighting each other.
 *
 * Three effects, all keyed off the pointer's distance to the nearest ring
 * (either side of the edge, within `reach`):
 *
 *   • cursor — the ring lights the cursor. A page cannot paint on the OS
 *     cursor, so while the pointer is near a ring and the element under it
 *     shows the plain arrow, that element gets `cursor: none` and a sprite
 *     of the pointer is drawn at the same spot (same trick as cursorjoy).
 *     The sprite must be the platform's real pointer or the swap shows —
 *     consumers supply it via `setCursorSprite`; without one this effect is
 *     off. Over buttons/text (hand, I-beam) the OS cursor stays.
 *
 *     Lighting treats the ring as the light source and the pointer as a
 *     small glossy object: a diffuse rim on the face that looks at the ring,
 *     coloured by the ring there and falling off with the inverse square of
 *     the distance, plus a specular term — the ring's own canvas mirrored
 *     across that face and compressed in depth, like on a convex surface.
 *   • spill — a soft glint on the page under the pointer, tinted with the
 *     ring's colour at the outline point nearest the cursor.
 *   • catch — the ring's catch-light faces the pointer (see `updateGlow`'s
 *     cursor mode), so the pointer acts as a light source.
 *
 * Runs only on `(pointer: fine)` devices. Tracking starts when the first
 * instance attaches and stops with the last; the per-frame loop runs only
 * while the pointer is within reach of some ring (plus the fade-out).
 *
 * Fail-safes for the cursor swap (the only part that can hurt someone):
 *   • off under `prefers-reduced-motion`, `forced-colors`, coarse/no-hover
 *     pointers, and pen/touch input;
 *   • off while the page is zoomed (DPR differs from when the sprite was
 *     registered, or pinch-zoomed) — the sprite would scale, the OS cursor
 *     wouldn't;
 *   • the OS cursor is hidden by an inline style on one element only, never
 *     a stylesheet, restored on every leave/blur/hide/keydown, when that
 *     element is detached, and on any exception (which also disables the
 *     effect for the session);
 *   • a frame-time watchdog disables it if it ever becomes expensive;
 *   • it never runs where the pointer is anything but the plain arrow, so
 *     hand, I-beam, resize and custom cursors are untouched.
 * Not detectable: Accessibility › Pointer size/colour on macOS. A user with
 * an enlarged pointer sees it swap to the stock one — ship the sprite only
 * where that trade-off is acceptable, and give them a way to turn it off.
 */





                                    
                   
                                                                                        
                
                                                                         
                 

                                                                  
                  
                                                                          
                                                                            
                                                     
                         
                                                                           
                         
                                                                              
                        
                                                                        
                                                      
                        
                                                                             
                                                                         
                      
                                                                                  
                     
                                                                         
                                                                          
                      
                                       
                     
                                                                         
                                                                   
                     

                                             
                 
                              
                      
                                                          
                        
                                                                           
                      
                                                                                 
                       
                                                   
                          
                                                                                 
                      
                                                                    
                    

                                              
                      
                                                                          
                      
                                                             
                    
 

const CURSOR_LIGHT_DEFAULTS                              = Object.freeze({
  enabled: true,
  reach: 56,
  fadeMs: 200,
  cursor: true,
  cursorDistance: 186,
  cursorStrength: 3.35,
  cursorDiffuse: 1.4,
  cursorFalloff: 37,
  cursorDepth: 0.4,
  cursorEdge: 0,
  cursorReach: 11.5,
  cursorBlur: 0.5,
  cursorZoom: 3,
  spill: false,
  spillRadius: 48,
  spillStrength: 0.55,
  spillOffset: 0.35,
  spillLumGain: 0.7,
  spillSaturation: 1.3,
  spillInside: 0.5,
  spillBlur: 0,
  catchLight: false,
  catchFollow: 0.25,
  catchGain: 1,
});

/** Live values. Read every frame; write via `setCursorLightConfig`. */
const CURSOR_LIGHT                    = { ...CURSOR_LIGHT_DEFAULTS };

function setCursorLightConfig(patch                            )       {
  Object.assign(CURSOR_LIGHT, patch);
  if (!CURSOR_LIGHT.spill && spillEl) hideSpill();
  if (!CURSOR_LIGHT.cursor) hideCursor();
  kick();
}

function resetCursorLightConfig()       {
  setCursorLightConfig({ ...CURSOR_LIGHT_DEFAULTS });
}

/** A raster of the platform's real pointer. `width`/`height` in CSS px,
 *  `hotX`/`hotY` the click point. `centerX`/`centerY` optionally override
 *  the body centre (default: alpha centroid). */
                               
              
                
                 
               
               
                   
                   
 

let sprite                      = null;
let spriteImg                          = null;
/** DPR when the sprite was registered — a change means browser zoom or a
 *  different display, where the sprite no longer matches the OS cursor. */
let spriteDpr = 0;
/** Off-switch flipped by the fail-safes. An exception disables for the
 *  session; the frame-time watchdog only pauses for a while (`disabledUntil`). */
let cursorDisabled = false;
let disabledUntil = 0;
let slowFrames = 0;
/** Opaque body pixels (sprite-local CSS px, centred on `bodyC`), the alpha
 *  centroid, and a hard mask of the body — built once per sprite. The
 *  silhouette extent in any direction comes from `bodyPts` per frame. */
let bodyPts                      = null;
const bodyC = { x: 0, y: 0 };
let bodyMask                           = null;
/** Hard body alpha at the sprite canvas's device resolution, cached by dpr. */
let bodyAlpha                           = null;
let bodyAlphaDpr = 0;

function bodyAlphaFor(img                  , sp              , dpr        , w        , h        )                           {
  if (bodyAlpha && bodyAlphaDpr === dpr && bodyAlpha.length === w * h) return bodyAlpha;
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const g = c.getContext('2d', { willReadFrequently: true });
  if (!g) return null;
  g.scale(dpr, dpr);
  g.drawImage(img, 0, 0, sp.width, sp.height);
  const d = g.getImageData(0, 0, w, h).data;
  const a = new Uint8ClampedArray(w * h);
  for (let i = 0, j = 3; i < a.length; i++, j += 4) a[i] = d[j] >= 128 ? 255 : 0;
  bodyAlpha = a; bodyAlphaDpr = dpr;
  return a;
}

/**
 * Multiply the canvas alpha by a per-pixel factor, in pixel data. Used in
 * place of `destination-in`, which WebKit intermittently misapplies on
 * accelerated canvases (a frame of the unclipped image — a white flash).
 */
function alphaPass(g                          , w        , h        , factor                                             )       {
  const img = g.getImageData(0, 0, w, h);
  const px = img.data;
  for (let y = 0, i = 0, j = 3; y < h; y++) {
    for (let x = 0; x < w; x++, i++, j += 4) {
      const a = px[j];
      if (a === 0) continue;
      const f = factor(x, y, i);
      px[j] = f >= 1 ? a : f <= 0 ? 0 : a * f;
    }
  }
  g.putImageData(img, 0, 0);
}

function analyseSprite(img                  , sp              )       {
  const S = 2;
  const c = document.createElement('canvas');
  c.width = Math.ceil(sp.width * S); c.height = Math.ceil(sp.height * S);
  const g = c.getContext('2d', { willReadFrequently: true });
  if (!g) return;
  g.drawImage(img, 0, 0, c.width, c.height);
  const d = g.getImageData(0, 0, c.width, c.height).data;
  const pts           = [];
  let sx = 0, sy = 0, n = 0;
  for (let y = 0; y < c.height; y++) {
    for (let x = 0; x < c.width; x++) {
      const a = d[(y * c.width + x) * 4 + 3];
      if (a < 128) continue;
      const px = (x + 0.5) / S, py = (y + 0.5) / S;
      pts.push(px, py); sx += px; sy += py; n++;
    }
  }
  if (n === 0) return;
  bodyC.x = sp.centerX ?? sx / n;
  bodyC.y = sp.centerY ?? sy / n;
  const arr = new Float32Array(pts.length);
  for (let i = 0; i < pts.length; i += 2) { arr[i] = pts[i] - bodyC.x; arr[i + 1] = pts[i + 1] - bodyC.y; }
  bodyPts = arr;
  // Hard body mask (drops the sprite's soft shadow) for clipping the light.
  const m = document.createElement('canvas');
  m.width = c.width; m.height = c.height;
  const mg = m.getContext('2d');
  if (mg) {
    const id = mg.createImageData(c.width, c.height);
    for (let i = 3; i < d.length; i += 4) if (d[i] >= 128) { id.data[i - 3] = 255; id.data[i - 2] = 255; id.data[i - 1] = 255; id.data[i] = 255; }
    mg.putImageData(id, 0, 0);
  }
  bodyMask = m;
}

/** Farthest the body extends from its centre along direction (ux, uy). */
function bodyExtent(ux        , uy        )         {
  if (!bodyPts) return 0;
  let best = -Infinity;
  for (let i = 0; i < bodyPts.length; i += 2) {
    const dot = bodyPts[i] * ux + bodyPts[i + 1] * uy;
    if (dot > best) best = dot;
  }
  return best === -Infinity ? 0 : best;
}

/** Supply the pointer raster (or null to turn the cursor effect off). Must
 *  match the OS pointer pixel-for-pixel, or the swap is visible. */
function setCursorSprite(next                     )       {
  sprite = next;
  spriteImg = null; bodyPts = null; bodyMask = null; bodyAlpha = null;
  cursorDisabled = false; disabledUntil = 0; slowFrames = 0;
  spriteDpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
  hideCursor();
  if (!next || typeof Image === 'undefined') return;
  const img = new Image();
  img.decoding = 'async';
  img.onload = () => { if (sprite === next) { analyseSprite(img, next); spriteImg = img; kick(); } };
  img.src = next.src;
}

// ─── Tracking ─────────────────────────────────────────────────────────────

let attached = 0;
let tracking = false;
let raf = 0;
let last = 0;
let px = Number.NaN, py = Number.NaN; // live pointer, NaN when gone
let lpx = 0, lpy = 0;                 // last known, for the fade-out
let uS = 0;                           // smoothed proximity weight (reach)
let uCS = 0;                          // smoothed proximity weight (cursorReach)
let near                         = null;
const _near = { d: 0, nx: 0, ny: 0, k: 1, left: 0, top: 0 };
const _pt = { x: 0, y: 0 };
const _sc = { r: 255, g: 255, b: 255 }; // smoothed spill tint

let spillEl                        = null;
let spillBg = '';
let spillSize = -1;
let spillBlur = -1;
let spillShown = false;

/** Reference-counted: MetalFx calls attach on mount, detach on unmount. */
function attachCursorLight()       {
  attached++;
  ensureTracking();
}

function detachCursorLight()       {
  attached = Math.max(0, attached - 1);
  if (attached === 0) stopTracking();
}

const mq = (q        )          => typeof window.matchMedia === 'function' && window.matchMedia(q).matches;

/** Whether replacing the OS cursor is acceptable right now. Re-checked
 *  every frame; all of these can change while the page is open. */
function cursorSwapAllowed()          {
  if (cursorDisabled || performance.now() < disabledUntil || !sprite || !spriteImg) return false;
  if (mq('(prefers-reduced-motion: reduce)') || mq('(forced-colors: active)')) return false;
  if (!mq('(pointer: fine)') || !mq('(hover: hover)')) return false;
  if ((window.devicePixelRatio || 1) !== spriteDpr) return false;
  const vv = window.visualViewport;
  if (vv && Math.abs(vv.scale - 1) > 0.001) return false;
  return true;
}

function ensureTracking()       {
  if (tracking || attached === 0 || typeof document === 'undefined') return;
  if (!mq('(pointer: fine)')) return;
  tracking = true;
  document.addEventListener('pointermove', onMove, { passive: true });
  document.addEventListener('pointerleave', onLeave);
  document.addEventListener('pointercancel', onLeave);
  document.addEventListener('keydown', onKey, { passive: true });
  document.addEventListener('visibilitychange', onLeave);
  window.addEventListener('blur', onLeave);
}

function stopTracking()       {
  if (!tracking) return;
  tracking = false;
  document.removeEventListener('pointermove', onMove);
  document.removeEventListener('pointerleave', onLeave);
  document.removeEventListener('pointercancel', onLeave);
  document.removeEventListener('keydown', onKey);
  document.removeEventListener('visibilitychange', onLeave);
  window.removeEventListener('blur', onLeave);
  if (raf !== 0) { cancelAnimationFrame(raf); raf = 0; }
  if (near) { near.cursorLight = null; near = null; }
  uS = 0; uCS = 0;
  if (spillEl) { spillEl.remove(); spillEl = null; spillBg = ''; spillSize = -1; spillBlur = -1; spillShown = false; }
  hideCursor();
  if (curEl) { curEl.remove(); curEl = null; }
}

let pointerIsMouse = true;
/** Set on keydown, cleared by the next pointer move — keeps the sprite off
 *  while someone types even though the loop keeps running nearby. */
let typing = false;

function onMove(e              )       {
  pointerIsMouse = e.pointerType === 'mouse' || e.pointerType === '';
  typing = false;
  px = lpx = e.clientX;
  py = lpy = e.clientY;
  // While the sprite is up, hide the OS cursor and move the sprite *inside*
  // the event, not in the next frame. WebKit re-evaluates the cursor only on
  // mouse moves: a hide applied one frame late, after the pointer crossed
  // into a new element and stopped, leaves the real arrow showing until the
  // next move. Moving the sprite here also trims a frame of lag.
  if (curShown && curEl) {
    if (pointerIsMouse && claimCursor(px, py)) {
      const sp = sprite;
      if (sp) curEl.style.transform = `translate3d(${(px - sp.hotX).toFixed(2)}px,${(py - sp.hotY).toFixed(2)}px,0)`;
    } else {
      hideCursor();
    }
  }
  kick();
}

/** macOS hides the pointer while typing; match it, and never leave a fake
 *  arrow sitting over a field someone is keyboard-navigating. */
function onKey()       {
  typing = true;
  hideCursor();
}

function onLeave()       {
  px = py = Number.NaN;
  kick();
}

function kick()       {
  if (!tracking || raf !== 0) return;
  last = performance.now();
  raf = requestAnimationFrame(step);
}

// ─── Geometry ─────────────────────────────────────────────────────────────

/**
 * Signed distance from a box-local point to the ring's outer outline
 * (positive outside), writing the nearest outline point to `out`.
 */
function nearestOutlinePoint(
  lx        , ly        , W        , H        , R        , kind                   , out                          
)         {
  const rr = kind === 'circle' ? Math.min(W, H) / 2 : Math.max(0, Math.min(R, Math.min(W, H) / 2));
  const cx = W / 2, cy = H / 2;
  const hx = Math.max(0, W / 2 - rr), hy = Math.max(0, H / 2 - rr);
  const qx = Math.max(-hx, Math.min(hx, lx - cx));
  const qy = Math.max(-hy, Math.min(hy, ly - cy));
  const dx = lx - cx - qx, dy = ly - cy - qy;
  const len = Math.hypot(dx, dy);
  if (len > 1e-6) {
    out.x = cx + qx + (dx / len) * rr;
    out.y = cy + qy + (dy / len) * rr;
    return len - rr;
  }
  // Inside the straight-edged core: nearest side.
  const dl = lx, dr = W - lx, dt = ly, db = H - ly;
  const m = Math.min(dl, dr, dt, db);
  if (m === dl) { out.x = 0; out.y = ly; }
  else if (m === dr) { out.x = W; out.y = ly; }
  else if (m === dt) { out.x = lx; out.y = 0; }
  else { out.x = lx; out.y = H; }
  return -m;
}

// ─── Pointer sprite ───────────────────────────────────────────────────────

let curEl                        = null;
let curCanvas                           = null;
let curCtx                                  = null;
let refCanvas                           = null;
let refCtx                                  = null;
let refCtxReadable = false;
let curDpr = 0, curW = 0, curH = 0;
let curShown = false;
/** Root-level hide (stable while the sprite is up — no churn as the pointer
 *  crosses elements, which is what WebKit shows as flicker), plus one
 *  element-level hide for elements that set their own `cursor: default`. */
let rootHidden = false;
let rootPrev = '';
let hiddenEl                     = null;
let hiddenPrev = '';

const TEXTY = /^(INPUT|TEXTAREA|SELECT)$/;
/** Per-element verdicts (true = plain arrow, claimable) so a pointermove
 *  doesn't force style resolution on every event. Cleared on release. */
let verdicts = new WeakMap                      ();
let lastClaimMs = 0;
let lastTarget                     = null;
/** Elements where `cursor: auto` means something other than the arrow. */
function isTextTarget(el             )          {
  let n                     = el;
  while (n && n !== document.body) {
    if (TEXTY.test(n.tagName) || n.isContentEditable) return true;
    n = n.parentElement;
  }
  return false;
}

function ensureCursor()          {
  if (curEl) return true;
  const el = document.createElement('div');
  el.className = 'metal-fx-cursor';
  el.setAttribute('aria-hidden', 'true');
  el.style.cssText = 'position:fixed;left:0;top:0;pointer-events:none;z-index:2147483001;will-change:transform;display:none';
  const c = document.createElement('canvas');
  c.style.display = 'block';
  el.appendChild(c);
  document.body.appendChild(el);
  const ctx = c.getContext('2d');
  const rc = document.createElement('canvas');
  const rctx = rc.getContext('2d');
  if (!ctx || !rctx) { el.remove(); return false; }
  curEl = el; curCanvas = c; curCtx = ctx; refCanvas = rc; refCtx = rctx;
  return true;
}

/** Hide the OS cursor on the element under the pointer, if it shows the
 *  plain arrow there. Returns whether the sprite may show. */
function claimCursor(x        , y        , force = false)          {
  // Hit-testing forces layout; while the bend is animating that layout is
  // dirty on every frame, so cap this at ~one per frame unless forced.
  const now = performance.now();
  if (!force && rootHidden && now - lastClaimMs < 12) return true;
  lastClaimMs = now;
  const target = document.elementFromPoint(x, y)                      ;
  if (!target) { releaseCursor(); return false; }
  if (target === lastTarget && rootHidden) return true;
  lastTarget = target;
  let ok = verdicts.get(target);
  if (ok === undefined) {
    ok = !isTextTarget(target);
    if (ok) { const cur = getComputedStyle(target).cursor; ok = cur === 'auto' || cur === 'default' || cur === 'none'; }
    verdicts.set(target, ok);
  }
  if (!ok) { releaseCursor(); return false; }
  if (!rootHidden) {
    const root = document.documentElement;
    rootPrev = root.style.cursor;
    root.style.cursor = 'none';
    rootHidden = true;
  }
  // Elements that set `cursor: default` / `auto` themselves don't inherit the
  // root's `none`; hide on them directly (rare — chips, drag handles).
  if (target !== hiddenEl) {
    if (hiddenEl) { hiddenEl.style.cursor = hiddenPrev; hiddenEl = null; hiddenPrev = ''; }
    if (getComputedStyle(target).cursor !== 'none') {
      hiddenEl = target; hiddenPrev = target.style.cursor;
      target.style.cursor = 'none';
    }
  }
  return true;
}

function releaseCursor()       {
  if (hiddenEl) {
    if (hiddenEl.isConnected) hiddenEl.style.cursor = hiddenPrev;
    hiddenEl = null; hiddenPrev = '';
  }
  if (rootHidden) {
    document.documentElement.style.cursor = rootPrev;
    rootHidden = false; rootPrev = '';
  }
  lastTarget = null;
  verdicts = new WeakMap();
}

function hideCursor()       {
  releaseCursor();
  if (curEl && curShown) { curEl.style.display = 'none'; curShown = false; }
}

function drawCursor(inst                 , cfg                   , env        )       {
  if (!curCtx || !refCtx || !curCanvas || !refCanvas || !curEl || !sprite || !spriteImg) return;
  const sp = sprite;
  const dpr = Math.min(3, window.devicePixelRatio || 1);
  if (dpr !== curDpr || sp.width !== curW || sp.height !== curH) {
    curDpr = dpr; curW = sp.width; curH = sp.height;
    curCanvas.width = refCanvas.width = Math.ceil(sp.width * dpr);
    curCanvas.height = refCanvas.height = Math.ceil(sp.height * dpr);
    curCanvas.style.width = `${sp.width}px`;
    curCanvas.style.height = `${sp.height}px`;
  }
  if (!refCtxReadable) { refCtx = refCanvas.getContext('2d', { willReadFrequently: true }); refCtxReadable = true; if (!refCtx) return; }
  const ctx = curCtx, rctx = refCtx;
  const W = sp.width, H = sp.height;

  // The pointer itself.
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, curCanvas.width, curCanvas.height);
  ctx.scale(dpr, dpr);
  ctx.drawImage(spriteImg, 0, 0, W, H);

  // Light from the ring. Direction from the body centre to the outline
  // point nearest it; the mirror plane sits on the body's silhouette in that
  // direction, so only the face that looks at the ring catches light —
  // cursor left of the button lights the pointer's right side, and so on.
  const rx = _near.left + _near.nx * _near.k, ry = _near.top + _near.ny * _near.k;
  const ccx = lpx - sp.hotX + bodyC.x, ccy = lpy - sp.hotY + bodyC.y;
  const dx = rx - ccx, dy = ry - ccy;
  const dist = Math.hypot(dx, dy);
  const ux0 = dist > 0.01 ? dx / dist : 1, uy0 = dist > 0.01 ? dy / dist : 0;
  const e = bodyExtent(ux0, uy0) + cfg.cursorEdge;
  const dEdge = Math.max(0, dist - e);
  const f0 = Math.max(1, cfg.cursorFalloff);
  const falloff = 1 / (1 + (dEdge / f0) * (dEdge / f0));
  // Sample the ring's colour a little inside its outer edge — the edge pixel
  // itself is anti-aliased toward transparent and reads dark.
  const bcx = inst.cssWidth / 2, bcy = inst.cssHeight / 2;
  const inx = bcx - _near.nx, iny = bcy - _near.ny, inl = Math.hypot(inx, iny) || 1;
  const ins = inst.ringCssPx * 0.5 + 1;
  const sxp = _near.nx + (inx / inl) * ins, syp = _near.ny + (iny / inl) * ins;
  const pk = sampleShaderPeakAt(inst, sxp, syp, 4);
  const lum = pk.lum;
  const pr = pk.r, pg = pk.g, pb = pk.b;
  const spec = cfg.cursorStrength * falloff * env;
  const diff = cfg.cursorDiffuse * falloff * (0.5 + 0.5 * Math.min(1, lum / 0.5)) * env;
  if (dist > 0.01 && spec + diff > 0.005) {
    const ux = dx / dist, uy = dy / dist, a = Math.atan2(uy, ux);
    const sdepth = Math.max(0.1, Math.min(1, cfg.cursorDepth));
    // Mirror plane: on the face of the pointer that looks at the ring.
    const mx = bodyC.x + e * ux, my = bodyC.y + e * uy;
    const fl = Math.max(1, cfg.cursorReach);

    rctx.setTransform(1, 0, 0, 1, 0, 0);
    rctx.clearRect(0, 0, refCanvas.width, refCanvas.height);
    rctx.scale(dpr, dpr);

    // Specular: the ring seen in the pointer's surface.
    if (spec > 0.005) {
      rctx.save();
      rctx.filter = cfg.cursorBlur > 0 ? `blur(${cfg.cursorBlur}px)` : 'none';
      const zoom = Math.max(1, cfg.cursorZoom);
      rctx.translate(mx, my);
      rctx.rotate(a);
      rctx.scale(-1, 1);                                // mirror across the plane
      rctx.translate((dist - e) * sdepth, 0);           // ring point, depth-compressed, behind the plane
      rctx.rotate(-a);
      rctx.scale(zoom, zoom);                           // magnify around the ring point
      const o = inst.overscan, k = _near.k;
      const passes = Math.max(1, Math.ceil(spec));
      rctx.globalAlpha = Math.min(1, spec / passes);
      rctx.globalCompositeOperation = 'lighter';
      for (let i = 0; i < passes; i++) {
        rctx.drawImage(inst.canvas, -(_near.nx + o) * k, -(_near.ny + o) * k, (inst.cssWidth + 2 * o) * k, (inst.cssHeight + 2 * o) * k);
      }
      rctx.restore();
      // Fade into the body away from the mirror plane.
      const inv = 1 / dpr, fl1 = 1 / fl;
      alphaPass(rctx, refCanvas.width, refCanvas.height, (x, y) => {
        const behind = -(((x + 0.5) * inv - mx) * ux + ((y + 0.5) * inv - my) * uy);
        return behind <= 0 ? 1 : 1 - behind * fl1;
      });
    }

    // Diffuse: the lit rim, in the ring's colour at that point.
    if (diff > 0.005) {
      const peak = Math.max(pr, pg, pb) || 1;
      const cr = Math.round((pr * 255) / peak), cg = Math.round((pg * 255) / peak), cb = Math.round((pb * 255) / peak);
      const dl = fl * 1.2;
      const g = rctx.createLinearGradient(mx + 0.5 * ux, my + 0.5 * uy, mx - dl * ux, my - dl * uy);
      const a0 = Math.min(1, diff);
      g.addColorStop(0, `rgba(${cr},${cg},${cb},${a0.toFixed(3)})`);
      g.addColorStop(0.45, `rgba(${cr},${cg},${cb},${(a0 * 0.4).toFixed(3)})`);
      g.addColorStop(1, `rgba(${cr},${cg},${cb},0)`);
      rctx.globalCompositeOperation = 'lighter';
      rctx.fillStyle = g;
      rctx.fillRect(0, 0, W, H);
      rctx.globalCompositeOperation = 'source-over';
    }

    // Keep the light on the pointer's body only (not its soft shadow).
    const body = bodyAlphaFor(spriteImg, sp, dpr, refCanvas.width, refCanvas.height);
    if (body) alphaPass(rctx, refCanvas.width, refCanvas.height, (_x, _y, i) => body[i] === 0 ? 0 : 1);

    ctx.globalCompositeOperation = 'lighter';
    ctx.drawImage(refCanvas, 0, 0, W, H);
    ctx.globalCompositeOperation = 'source-over';
  }

  curEl.style.transform = `translate3d(${(lpx - sp.hotX).toFixed(2)}px,${(lpy - sp.hotY).toFixed(2)}px,0)`;
  if (!curShown) { curEl.style.display = ''; curShown = true; }
}

// ─── Frame ────────────────────────────────────────────────────────────────

function ensureSpill()                 {
  if (spillEl) return spillEl;
  const el = document.createElement('div');
  el.className = 'metal-fx-cursor-spill';
  el.setAttribute('aria-hidden', 'true');
  el.style.cssText =
    'position:fixed;left:0;top:0;pointer-events:none;z-index:2147483000;border-radius:50%;' +
    'mix-blend-mode:plus-lighter;will-change:transform,opacity;opacity:0;display:none';
  document.body.appendChild(el);
  spillEl = el;
  return el;
}

function hideSpill()       {
  if (!spillEl || !spillShown) return;
  spillEl.style.display = 'none';
  spillEl.style.opacity = '0';
  spillShown = false;
}

function step(now        )       {
  raf = 0;
  if (!tracking) return;
  // Own work only — `now` is the frame timestamp and includes whatever else
  // ran in this frame (React renders, other rAF callbacks), which is not
  // ours to be blamed for.
  const t0 = performance.now();
  try {
    stepInner(now);
  } catch (err) {
    // Whatever broke, the user must get their cursor back.
    cursorDisabled = true;
    hideCursor();
    hideSpill();
    if (near) { near.cursorLight = null; near = null; }
    if (typeof console !== 'undefined') console.warn('metal-fx: cursor light disabled after error', err);
    return;
  }
  // Watchdog: this should cost well under a millisecond. If it keeps not
  // doing so — huge pages, a pathological elementFromPoint — pause the
  // cursor swap for a few seconds and try again.
  const took = performance.now() - t0;
  if (took > 6) {
    if (++slowFrames >= 20) { slowFrames = 0; disabledUntil = performance.now() + 5000; hideCursor(); }
  } else if (slowFrames > 0) slowFrames--;
}

function stepInner(now        )       {
  const cfg = CURSOR_LIGHT;
  const dt = Math.min(0.05, Math.max(0.001, (now - last) / 1000));
  last = now;

  // Nearest ring within reach.
  let best                         = null;
  let u = 0, uC = 0;
  if (cfg.enabled && SHARED && !Number.isNaN(px)) {
    let bestAbs = Number.POSITIVE_INFINITY;
    const reachA = Math.max(1, cfg.reach);
    const reachC = cfg.cursor && cursorSwapAllowed() ? Math.max(1, cfg.cursorDistance) : 0;
    const reach = Math.max(reachA, reachC);
    for (const inst of SHARED.instances) {
      if (!inst.visible || inst.paused || !inst.canvas.isConnected) continue;
      const r = inst.canvas.getBoundingClientRect();
      if (r.width <= 0) continue;
      const o = inst.overscan;
      const k = r.width / (inst.cssWidth + 2 * o);
      const left = r.left + o * k, top = r.top + o * k;
      const rk = reach * k;
      if (px < left - rk || px > left + inst.cssWidth * k + rk || py < top - rk || py > top + inst.cssHeight * k + rk) continue;
      const lx = (px - left) / k, ly = (py - top) / k;
      const d = nearestOutlinePoint(lx, ly, inst.cssWidth, inst.cssHeight, inst.cornerRadius, inst.kind, _pt);
      const ad = Math.abs(d);
      if (ad <= reach && ad < bestAbs) {
        bestAbs = ad; best = inst;
        _near.d = d; _near.nx = _pt.x; _near.ny = _pt.y; _near.k = k; _near.left = left; _near.top = top;
      }
    }
    if (best) {
      if (bestAbs <= reachA) { const t = 1 - bestAbs / reachA; u = t * t * (3 - 2 * t); }
      if (bestAbs <= reachC) uC = Math.min(1, (1 - bestAbs / reachC) * 3);
    }
  }

  // Envelope on the proximity weight — no pops on enter/leave/jump.
  const a = 1 - Math.exp(-(dt * 1000) / (Math.max(1, cfg.fadeMs) / 3));
  uS += (u - uS) * a;
  uCS += (uC - uCS) * a;

  if (best && best !== near) {
    if (near) { near.cursorLight = null; tickInstanceGlow(near, now); }
    near = best;
  }
  if (!best && uS < 0.002 && uCS < 0.002) {
    uS = 0; uCS = 0;
    if (near) { near.cursorLight = null; tickInstanceGlow(near, now); near = null; }
    hideSpill();
    hideCursor();
    return;
  }
  if (!near) return;

  // C — hand the glow a light source and tick it at pointer rate.
  if (cfg.catchLight) {
    const cl = near.cursorLight ?? (near.cursorLight = { x: 0, y: 0, w: 0 });
    cl.x = _near.nx; cl.y = _near.ny; cl.w = uS;
  } else if (near.cursorLight) {
    near.cursorLight = null;
  }
  tickInstanceGlow(near, now);

  // A — the ring on the cursor.
  if (cfg.cursor && uCS > 0.002 && pointerIsMouse && !typing && !Number.isNaN(px) && cursorSwapAllowed() && ensureCursor() && claimCursor(px, py)) {
    drawCursor(near, cfg, uCS);
  } else {
    hideCursor();
  }

  // B — the glint under the pointer.
  if (cfg.spill) {
    const el = ensureSpill();
    const rgb = sampleShaderRGBAt(near, _near.nx, _near.ny, 2);
    const lum = sampleShaderLumAt(near, _near.nx, _near.ny, 3);
    const peak = Math.max(rgb.r, rgb.g, rgb.b) || 1;
    const hsv = rgbToHsv((rgb.r * 255) / peak, (rgb.g * 255) / peak, (rgb.b * 255) / peak);
    const [cr, cg, cb] = hsvToRgb(hsv[0], Math.min(1, hsv[1] * cfg.spillSaturation), 1);
    _sc.r += (cr - _sc.r) * 0.15; _sc.g += (cg - _sc.g) * 0.15; _sc.b += (cb - _sc.b) * 0.15;
    // Quantise so the gradient string (and its repaint) only changes on a
    // visible step.
    const qr = Math.round(_sc.r / 6) * 6, qg = Math.round(_sc.g / 6) * 6, qb = Math.round(_sc.b / 6) * 6;
    const bg = `radial-gradient(closest-side, rgba(${qr},${qg},${qb},1) 0%, rgba(${qr},${qg},${qb},0.35) 45%, rgba(${qr},${qg},${qb},0) 100%)`;
    if (bg !== spillBg) { spillBg = bg; el.style.background = bg; }

    const R = Math.max(1, cfg.spillRadius * _near.k);
    if (R !== spillSize) { spillSize = R; el.style.width = `${(2 * R).toFixed(1)}px`; el.style.height = `${(2 * R).toFixed(1)}px`; }
    if (cfg.spillBlur !== spillBlur) { spillBlur = cfg.spillBlur; el.style.filter = cfg.spillBlur > 0 ? `blur(${cfg.spillBlur}px)` : ''; }

    const rx = _near.left + _near.nx * _near.k, ry = _near.top + _near.ny * _near.k;
    const sx = lpx + (rx - lpx) * cfg.spillOffset, sy = lpy + (ry - lpy) * cfg.spillOffset;
    el.style.transform = `translate3d(${(sx - R).toFixed(2)}px,${(sy - R).toFixed(2)}px,0)`;

    const lf = Math.min(1, Math.max(0, lum / 0.3));
    const lumMix = 1 - cfg.spillLumGain + cfg.spillLumGain * lf;
    const insideMul = _near.d < 0 ? cfg.spillInside : 1;
    const op = Math.max(0, Math.min(1, cfg.spillStrength * uS * lumMix * insideMul));
    if (!spillShown) { el.style.display = ''; spillShown = true; }
    el.style.opacity = op.toFixed(3);
  } else {
    hideSpill();
  }

  raf = requestAnimationFrame(step);
}


/* ── engine/glow/config.ts ─────────────────────────────── */
/**
 * Live-tunable glow parameters.
 *
 * Every number the halo + catch-light overlay used to hard-code lives here as
 * a mutable singleton so a tuning surface can drive it at runtime. Two classes:
 *
 *   • runtime — read every frame inside `updateGlow`. Changing one takes
 *     effect on the next frame with no DOM work.
 *   • markup — baked into the SVG (`buildSvgMarkup`) at inject time: stroke
 *     widths, blur radii, per-layer opacities, blob lengths. Changing one
 *     requires the SVG to be rebuilt, which `setGlowConfig` signals through
 *     `subscribeGlowConfig` so MetalFx can re-inject.
 *
 * `GLOW_DEFAULTS` are the values that shipped before this file existed, so
 * `resetGlowConfig()` is an exact restore.
 */

                             
                                                                           
                                               
                    
                                                                              
                         
                                                     
                 
                                                
                 
                                                                        
                
                                                                   
                       
                                                                              
                      
                                                 
                     
                                                                      
                   
                                                         
                
                                                         
                
                                                                        
                     
                                                                         
                                                                                 
                      
                                                                            
                                                                          
                              
                    

                                                                           
                                                                    
                      
                                                                           
                       
                       
                       
                       
                       
                     
                     
                     
                     
                   
                   
                   
                   
                           
                          
                         
                        
                                                                     
                     
                       
 

const GLOW_MARKUP_KEYS                                = new Set                  ([
  'haloHalfLen', 'extraHalfLen',
  'haloStrokeXl', 'haloStrokeLg', 'haloStrokeMd', 'haloStrokeSm',
  'haloBlurXl', 'haloBlurLg', 'haloBlurMd', 'haloBlurSm',
  'haloOpXl', 'haloOpLg', 'haloOpMd', 'haloOpSm',
  'extraStrokeOuter', 'extraStrokeCore', 'extraBlurOuter', 'extraBlurCore',
  'extraFadeR', 'extraOpOuter',
]);

// The `/ 3` values were `EXTRA_SCALE = 1 / 3` applied to the original
// constants (4.0, 2.0, 2.0, 1.35, 13.0, 9.13952). Stored pre-multiplied so a
// slider moves the number the SVG actually receives.
const GLOW_DEFAULTS                       = Object.freeze({
  haloOpMul: 2.0,
  extraIntensity: 3.51,
  peakOp: 0.85,
  baseOp: 0.34,
  inset: 1.5,
  extraOutward: 1.0,
  wanderRange: 15,
  wanderLerp: 0.0075,
  fadeRate: 0.00875,
  lumLo: 0.08,
  lumHi: 0.32,
  minDwellMs: 1500,
  relocFadeMs: 300,
  pointGain: 2.5,

  haloHalfLen: 7.8,
  extraHalfLen: 9.13952 / 3,
  haloStrokeXl: 26.4,
  haloStrokeLg: 15.6,
  haloStrokeMd: 7.2,
  haloStrokeSm: 3.0,
  haloBlurXl: 8.4,
  haloBlurLg: 4.8,
  haloBlurMd: 2.1,
  haloBlurSm: 0.9,
  haloOpXl: 0.385,
  haloOpLg: 0.595,
  haloOpMd: 0.70,
  haloOpSm: 0.70,
  extraStrokeOuter: 4.0 / 3,
  extraStrokeCore: 2.0 / 3,
  extraBlurOuter: 2.0 / 3,
  extraBlurCore: 1.35 / 3,
  extraFadeR: 13.0 / 3,
  extraOpOuter: 0.85,
});

/** Live values. Read directly by the glow engine; write via `setGlowConfig`. */
const GLOW             = { ...GLOW_DEFAULTS };

                                                 
const listeners = new Set          ();

/**
 * Merge a partial config. Notifies subscribers, flagging whether any markup
 * key changed so they can decide between "next frame picks it up" and
 * "rebuild the SVG".
 */
function setGlowConfig(patch                     )       {
  let markupChanged = false;
  for (const k of Object.keys(patch)                           ) {
    const v = patch[k];
    if (v === undefined || GLOW[k] === v) continue;
    GLOW[k] = v;
    if (GLOW_MARKUP_KEYS.has(k)) markupChanged = true;
  }
  for (const fn of listeners) fn(markupChanged);
}

function resetGlowConfig()       {
  setGlowConfig({ ...GLOW_DEFAULTS });
}

function subscribeGlowConfig(fn          )             {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}


/* ── engine/glow/bake.ts ─────────────────────────────── */
/**
 * Pre-rendered glow sprites.
 *
 * The halo used to be four blurred SVG strokes re-rasterised through
 * `feGaussianBlur` on every move. Here the same strokes are rendered once,
 * in white, into small alpha bitmaps; the per-frame work is then a couple of
 * `drawImage` calls. Blur is a 3-pass box blur on the alpha channel (a close
 * gaussian approximation) so it doesn't depend on `ctx.filter` support.
 *
 * Sprites are cached by everything that shapes them — half-length, scale,
 * device pixel ratio and the GLOW markup values — and shared by every
 * instance with the same key.
 */


                         
                            
                                                                    
                           
                                   
            
            
                                                                                
             
             
 

const cache = new Map                ();

// ─── Blur ─────────────────────────────────────────────────────────────────

/** Box sizes for `n` passes approximating a gaussian of `sigma` (Kutskir). */
function boxesForGauss(sigma        , n        )           {
  const wIdeal = Math.sqrt((12 * sigma * sigma) / n + 1);
  let wl = Math.floor(wIdeal);
  if (wl % 2 === 0) wl--;
  const wu = wl + 2;
  const mIdeal = (12 * sigma * sigma - n * wl * wl - 4 * n * wl - 3 * n) / (-4 * wl - 4);
  const m = Math.round(mIdeal);
  const sizes           = [];
  for (let i = 0; i < n; i++) sizes.push(i < m ? wl : wu);
  return sizes;
}

function boxBlurH(src              , dst              , w        , h        , r        )       {
  const iarr = 1 / (r + r + 1);
  for (let y = 0; y < h; y++) {
    const row = y * w;
    let acc = 0;
    for (let x = -r; x <= r; x++) acc += src[row + Math.min(w - 1, Math.max(0, x))];
    for (let x = 0; x < w; x++) {
      dst[row + x] = acc * iarr;
      const out = row + Math.max(0, x - r), inn = row + Math.min(w - 1, x + r + 1);
      acc += src[inn] - src[out];
    }
  }
}

function boxBlurV(src              , dst              , w        , h        , r        )       {
  const iarr = 1 / (r + r + 1);
  for (let x = 0; x < w; x++) {
    let acc = 0;
    for (let y = -r; y <= r; y++) acc += src[Math.min(h - 1, Math.max(0, y)) * w + x];
    for (let y = 0; y < h; y++) {
      dst[y * w + x] = acc * iarr;
      const out = Math.max(0, y - r) * w + x, inn = Math.min(h - 1, y + r + 1) * w + x;
      acc += src[inn] - src[out];
    }
  }
}

function gaussBlur(a              , w        , h        , sigma        )               {
  if (sigma <= 0.05) return a;
  const tmp = new Float32Array(a.length);
  let cur = a;
  for (const box of boxesForGauss(sigma, 3)) {
    const r = (box - 1) / 2;
    boxBlurH(cur, tmp, w, h, r);
    boxBlurV(tmp, cur, w, h, r);
  }
  return cur;
}

// ─── Rasterising ──────────────────────────────────────────────────────────

                                                                 

/** Alpha of a horizontal round-capped line of `halfLen` at the given width,
 *  rendered through the 2D canvas so the AA matches what SVG produced. */
function strokeAlpha(halfLen        , strokeW        , w        , h        , dpr        , ax        , ay        )               {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const g = c.getContext('2d', { willReadFrequently: true });
  const out = new Float32Array(w * h);
  if (!g) return out;
  g.scale(dpr, dpr);
  g.strokeStyle = '#fff';
  g.lineCap = 'round';
  g.lineJoin = 'round';
  g.lineWidth = strokeW;
  g.beginPath();
  g.moveTo(ax - halfLen, ay);
  g.lineTo(ax + halfLen, ay);
  g.stroke();
  const d = g.getImageData(0, 0, w, h).data;
  for (let i = 0, j = 3; i < out.length; i++, j += 4) out[i] = d[j] / 255;
  return out;
}

function compose(layers         , halfLen        , s        , dpr        , fade        )         {
  let padMax = 0;
  for (const l of layers) padMax = Math.max(padMax, (l.stroke / 2 + 3 * l.blur) * s);
  const pad = Math.ceil(padMax) + 1;
  const cw = 2 * halfLen + 2 * pad, ch = 2 * pad;
  const w = Math.ceil(cw * dpr), h = Math.ceil(ch * dpr);
  const acc = new Float32Array(w * h);
  for (const l of layers) {
    let a = strokeAlpha(halfLen, l.stroke * s, w, h, dpr, pad, pad);
    a = gaussBlur(a, w, h, l.blur * s * dpr);
    const op = l.opacity;
    // White over white: only alpha composes.
    for (let i = 0; i < acc.length; i++) { const la = a[i] * op; acc[i] = acc[i] + la * (1 - acc[i]); }
  }
  if (fade > 0) {
    // SVG luminance mask: white to 0.30, #404040 (0.25) at 0.65, black at 1.
    const cx = pad * dpr, cy = pad * dpr, R = fade * s * dpr;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const t = Math.hypot(x + 0.5 - cx, y + 0.5 - cy) / R;
      let m        ;
      if (t <= 0.3) m = 1;
      else if (t <= 0.65) m = 1 - ((t - 0.3) / 0.35) * 0.75;
      else if (t < 1) m = 0.25 * (1 - (t - 0.65) / 0.35);
      else m = 0;
      acc[y * w + x] *= m;
    }
  }
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const g = c.getContext('2d');
  const alpha = new Uint8ClampedArray(w * h);
  for (let i = 0; i < acc.length; i++) alpha[i] = Math.round(Math.min(1, acc[i]) * 255);
  if (g) {
    const img = g.createImageData(w, h);
    const d = img.data;
    for (let i = 0, j = 0; i < acc.length; i++, j += 4) {
      d[j] = 255; d[j + 1] = 255; d[j + 2] = 255; d[j + 3] = alpha[i];
    }
    g.putImageData(img, 0, 0);
  }
  return { canvas: c, alpha, w: cw, h: ch, ax: pad, ay: pad };
}

function markupKey()         {
  return [
    GLOW.haloStrokeXl, GLOW.haloStrokeLg, GLOW.haloStrokeMd, GLOW.haloStrokeSm,
    GLOW.haloBlurXl, GLOW.haloBlurLg, GLOW.haloBlurMd, GLOW.haloBlurSm,
    GLOW.haloOpXl, GLOW.haloOpLg, GLOW.haloOpMd, GLOW.haloOpSm,
    GLOW.extraStrokeOuter, GLOW.extraStrokeCore, GLOW.extraBlurOuter, GLOW.extraBlurCore,
    GLOW.extraFadeR, GLOW.extraOpOuter,
  ].join(',');
}

/** The wide halo: four blurred strokes stacked, at `halfLen` half-length. */
function bakeHalo(halfLen        , s        , dpr        )         {
  const key = `h|${halfLen.toFixed(2)}|${s}|${dpr}|${markupKey()}`;
  let sp = cache.get(key);
  if (!sp) {
    sp = compose([
      { stroke: GLOW.haloStrokeXl, blur: GLOW.haloBlurXl, opacity: GLOW.haloOpXl },
      { stroke: GLOW.haloStrokeLg, blur: GLOW.haloBlurLg, opacity: GLOW.haloOpLg },
      { stroke: GLOW.haloStrokeMd, blur: GLOW.haloBlurMd, opacity: GLOW.haloOpMd },
      { stroke: GLOW.haloStrokeSm, blur: GLOW.haloBlurSm, opacity: GLOW.haloOpSm },
    ], halfLen, s, dpr, 0);
    cache.set(key, sp);
  }
  return sp;
}

/** The catch-light: two tight strokes with a radial fade at the ends. */
function bakeExtra(halfLen        , s        , dpr        )         {
  const key = `e|${halfLen.toFixed(2)}|${s}|${dpr}|${markupKey()}`;
  let sp = cache.get(key);
  if (!sp) {
    sp = compose([
      { stroke: GLOW.extraStrokeOuter, blur: GLOW.extraBlurOuter, opacity: GLOW.extraOpOuter },
      { stroke: GLOW.extraStrokeCore, blur: GLOW.extraBlurCore, opacity: 1 },
    ], halfLen, s, dpr, GLOW.extraFadeR);
    cache.set(key, sp);
  }
  return sp;
}

/** A tinted copy of a white sprite. `holder` caches by tint so the re-tint
 *  only happens when the colour actually changes. Pixel-data based on
 *  purpose: no `source-in` compositing, which WebKit intermittently gets
 *  wrong on accelerated canvases (a solid rectangle instead of the shape). */
                                                                                                                     

function tintSprite(src        , r        , g        , b        , holder        )                    {
  const key = (r << 16) | (g << 8) | b;
  if (holder.canvas && holder.tint === key && holder.src === src) return holder.canvas;
  let c = holder.canvas;
  let img = holder.img;
  if (!c || !img || holder.src !== src) {
    c = document.createElement('canvas');
    c.width = src.canvas.width; c.height = src.canvas.height;
    img = c.getContext('2d')?.createImageData(c.width, c.height) ?? null;
  }
  const ctx = c.getContext('2d');
  if (ctx && img) {
    const d = img.data, a = src.alpha;
    for (let i = 0, j = 0; i < a.length; i++, j += 4) { d[j] = r; d[j + 1] = g; d[j + 2] = b; d[j + 3] = a[i]; }
    ctx.putImageData(img, 0, 0);
  }
  holder.canvas = c; holder.img = img; holder.tint = key; holder.src = src;
  return c;
}


/* ── engine/glow/geometry.ts ─────────────────────────────── */
/**
 * Pure geometry + SVG markup for the glow overlay.
 *
 * Perimeter math (rounded-rect / circle arc-length sampling), blob path
 * generation, SVG filter/mask construction, and HSV colour helpers.
 * No state — every function is a pure transform.
 */






function rrPerim(w        , h        , r        )         {
  const rr = Math.max(0, Math.min(r, Math.min(w, h) / 2));
  return 2 * Math.max(0, w - 2 * rr) + 2 * Math.max(0, h - 2 * rr) + 2 * Math.PI * rr;
}

function shapePerim(w        , h        , r        , kind                   )         {
  if (kind === 'circle') return 2 * Math.PI * Math.max(0, Math.min(r, Math.min(w, h) / 2));
  return rrPerim(w, h, r);
}

function sampleAtArc(s        , w        , h        , r        , inset        , outward        , kind                   , out     )     {
  const o = out || { x: 0, y: 0 };
  const rr = Math.max(0, Math.min(r, Math.min(w, h) / 2));
  if (kind === 'circle') {
    const perim = 2 * Math.PI * rr;
    if (perim <= 0.0001) { o.x = w * 0.5; o.y = h * 0.5; return o; }
    s = ((s % perim) + perim) % perim;
    const theta = -Math.PI / 2 + (s / perim) * Math.PI * 2;
    const rad = Math.max(0, rr - inset + outward);
    o.x = w * 0.5 + rad * Math.cos(theta);
    o.y = h * 0.5 + rad * Math.sin(theta);
    return o;
  }
  const topLen = Math.max(0, w - 2 * rr), sideLen = Math.max(0, h - 2 * rr);
  const arcLen = (Math.PI * rr) / 2;
  const perim = 2 * (topLen + sideLen) + 4 * arcLen;
  s = ((s % perim) + perim) % perim;
  const rad = Math.max(0, rr - inset + outward);
  let d = s;
  if (d < topLen) { o.x = rr + d; o.y = inset - outward; return o; }
  d -= topLen;
  if (d < arcLen) {
    const theta = -Math.PI / 2 + (arcLen > 0 ? d / arcLen : 0) * (Math.PI / 2);
    o.x = (w - rr) + rad * Math.cos(theta); o.y = rr + rad * Math.sin(theta); return o;
  }
  d -= arcLen;
  if (d < sideLen) { o.x = w - inset + outward; o.y = rr + d; return o; }
  d -= sideLen;
  if (d < arcLen) {
    const theta = (arcLen > 0 ? d / arcLen : 0) * (Math.PI / 2);
    o.x = (w - rr) + rad * Math.cos(theta); o.y = (h - rr) + rad * Math.sin(theta); return o;
  }
  d -= arcLen;
  if (d < topLen) { o.x = w - rr - d; o.y = h - inset + outward; return o; }
  d -= topLen;
  if (d < arcLen) {
    const theta = Math.PI / 2 + (arcLen > 0 ? d / arcLen : 0) * (Math.PI / 2);
    o.x = rr + rad * Math.cos(theta); o.y = (h - rr) + rad * Math.sin(theta); return o;
  }
  d -= arcLen;
  if (d < sideLen) { o.x = inset - outward; o.y = h - rr - d; return o; }
  d -= sideLen;
  const theta = Math.PI + (arcLen > 0 ? d / arcLen : 0) * (Math.PI / 2);
  o.x = rr + rad * Math.cos(theta); o.y = rr + rad * Math.sin(theta);
  return o;
}

/**
 * Inverse of `sampleAtArc` at inset 0: the arc-length position on the outline
 * nearest a box-local point. Points off the outline project onto it.
 */
function arcAtPoint(x        , y        , w        , h        , r        , kind                   )         {
  const rr = Math.max(0, Math.min(r, Math.min(w, h) / 2));
  if (kind === 'circle') {
    const perim = 2 * Math.PI * rr;
    if (perim <= 0.0001) return 0;
    const theta = Math.atan2(y - h / 2, x - w / 2);
    const s = ((theta + Math.PI / 2) / (2 * Math.PI)) * perim;
    return ((s % perim) + perim) % perim;
  }
  const topLen = Math.max(0, w - 2 * rr), sideLen = Math.max(0, h - 2 * rr);
  const arcLen = (Math.PI * rr) / 2, Q = Math.PI / 2;
  const s1 = topLen, s2 = s1 + arcLen, s3 = s2 + sideLen, s4 = s3 + arcLen, s5 = s4 + topLen, s6 = s5 + arcLen, s7 = s6 + sideLen;
  const inX = x >= rr && x <= w - rr, inY = y >= rr && y <= h - rr;
  if (inX && inY) {
    const dl = x, dr = w - x, dt = y, db = h - y, m = Math.min(dl, dr, dt, db);
    if (m === dt) return x - rr;
    if (m === dr) return s2 + (y - rr);
    if (m === db) return s4 + (w - rr - x);
    return s6 + (h - rr - y);
  }
  if (inX) return y < h / 2 ? x - rr : s4 + (w - rr - x);
  if (inY) return x > w / 2 ? s2 + (y - rr) : s6 + (h - rr - y);
  if (x > w / 2 && y < h / 2) { const t = Math.atan2(y - rr, x - (w - rr)); return s1 + ((t + Q) / Q) * arcLen; }
  if (x > w / 2) { const t = Math.atan2(y - (h - rr), x - (w - rr)); return s3 + (t / Q) * arcLen; }
  if (y > h / 2) { const t = Math.atan2(y - (h - rr), x - rr); return s5 + ((t - Q) / Q) * arcLen; }
  const t = Math.atan2(y - rr, x - rr);
  return s7 + ((t + Math.PI) / Q) * arcLen;
}

function buildStaticBlobPath(halfLen        , segments        )         {
  const step = (halfLen * 2) / segments;
  let d = '';
  for (let i = 0; i <= segments; i++) {
    const x = -halfLen + i * step;
    d += (i === 0 ? 'M ' : 'L ') + x.toFixed(3) + ' 0 ';
  }
  return d;
}

const _ta     = { x: 0, y: 0 };
const _tb     = { x: 0, y: 0 };

function tangentAngleAtArc(s        , w        , h        , r        , inset        , kind                   )         {
  const eps = 0.1;
  sampleAtArc(s - eps, w, h, r, inset, 0, kind, _ta);
  sampleAtArc(s + eps, w, h, r, inset, 0, kind, _tb);
  return Math.atan2(_tb.y - _ta.y, _tb.x - _ta.x);
}

function smoothstep(a        , b        , x        )         {
  if (a === b) return x < a ? 0 : 1;
  const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

function buildPerimTable(opts             )                {
  if (opts.samplePoints && opts.samplePoints.length > 0) {
    // Point mode: `arc` is just the index — relocation logic works on
    // indices, and positioning bypasses arc math entirely.
    return opts.samplePoints.map((p, i) => ({ x: p.x, y: p.y, arc: i }));
  }
  const perim = shapePerim(opts.width, opts.height, opts.cornerRadius, opts.kind);
  const insetS = GLOW.inset * (opts.scale ?? 1);
  const table                = [];
  for (let i = 0; i < PERIM_SAMPLES; i++) {
    const arc = (i / PERIM_SAMPLES) * perim;
    const pt = sampleAtArc(arc, opts.width, opts.height, opts.cornerRadius, insetS, 0, opts.kind);
    table.push({ x: pt.x, y: pt.y, arc });
  }
  return table;
}

function buildSvgMarkup(opts             , p        )         {
  const { width: W, height: H, cornerRadius: R } = opts;
  const s = opts.scale ?? 1;
  const ringInset = opts.kind === 'circle' ? 2 : 1;
  const innerR = Math.max(0, R - ringInset);
  // Filter region grows with scale so blurred strokes don't get clipped at
  // bigger sizes. The 200/540/440 baseline matches the canonical 1× pill.
  const fX = (-200 * s).toFixed(0), fY = fX;
  const fW = (540 * s).toFixed(0), fH = (440 * s).toFixed(0);
  const fRect = `x="${fX}" y="${fY}" width="${fW}" height="${fH}"`;
  const fr = `${fRect} filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB"`;
  // Stroke widths and blur stdDeviations are absolute SVG units; multiply
  // by `s` so they remain proportional when viewBox grows with the host.
  const sw = (n        ) => (n * s).toFixed(3);
  const sd = (n        ) => (n * s).toFixed(3);
  return [
    '<defs>',
    `<filter id="${p}_bXl" ${fr}><feGaussianBlur stdDeviation="${sd(GLOW.haloBlurXl)}"/></filter>`,
    `<filter id="${p}_bLg" ${fr}><feGaussianBlur stdDeviation="${sd(GLOW.haloBlurLg)}"/></filter>`,
    `<filter id="${p}_bMd" ${fr}><feGaussianBlur stdDeviation="${sd(GLOW.haloBlurMd)}"/></filter>`,
    `<filter id="${p}_bSm" ${fr}><feGaussianBlur stdDeviation="${sd(GLOW.haloBlurSm)}"/></filter>`,
    `<filter id="${p}_ebO" ${fr}><feGaussianBlur stdDeviation="${sd(GLOW.extraBlurOuter)}"/></filter>`,
    `<filter id="${p}_ebC" ${fr}><feGaussianBlur stdDeviation="${sd(GLOW.extraBlurCore)}"/></filter>`,
    `<radialGradient id="${p}_fg" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="white"/><stop offset="0.30" stop-color="white"/><stop offset="0.65" stop-color="#404040"/><stop offset="1" stop-color="black"/></radialGradient>`,
    `<mask id="${p}_fm" maskUnits="userSpaceOnUse" ${fRect}><rect ${fRect} fill="black"/><circle id="${p}_fc" cx="0" cy="0" r="${(GLOW.extraFadeR * s).toFixed(3)}" fill="url(#${p}_fg)"/></mask>`,
    opts.maskDataUrl
      // Point mode clips hard to the glyphs (black surround); the halo's
      // blurred energy outside the strokes is discarded, so `pointGain`
      // compensates inside them.
      ? `<mask id="${p}_rm" maskUnits="userSpaceOnUse" ${fRect}><rect ${fRect} fill="black"/><image href="${opts.maskDataUrl}" x="0" y="0" width="${W}" height="${H}" preserveAspectRatio="none"/></mask>`
      : `<mask id="${p}_rm" maskUnits="userSpaceOnUse" ${fRect}><rect ${fRect} fill="#808080"/><path id="${p}_rmO" d="${outlinePathD(roundRectOutline(0, 0, W, H, R, null))}" fill="white"/><path id="${p}_rmI" d="${outlinePathD(roundRectOutline(ringInset, ringInset, W - ringInset * 2, H - ringInset * 2, innerR, null))}" fill="black"/></mask>`,
    '</defs>',
    // Safari clips mask to the masked element's bbox; our horizontal strokes
    // have zero height, so the mask becomes a sliver. These spacer rects
    // inflate the bbox to the full filter region.
    `<g id="${p}_h" mask="url(#${p}_rm)" opacity="0">`,
    `<rect ${fRect} fill="none" pointer-events="none"/>`,
    `<g id="${p}_hI" stroke="white">`,
    `<path id="${p}_pXl" stroke-width="${sw(GLOW.haloStrokeXl)}" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity="${GLOW.haloOpXl}" filter="url(#${p}_bXl)"/>`,
    `<path id="${p}_pLg" stroke-width="${sw(GLOW.haloStrokeLg)}" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity="${GLOW.haloOpLg}" filter="url(#${p}_bLg)"/>`,
    `<path id="${p}_pMd" stroke-width="${sw(GLOW.haloStrokeMd)}" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity="${GLOW.haloOpMd}" filter="url(#${p}_bMd)"/>`,
    `<path id="${p}_pSm" stroke-width="${sw(GLOW.haloStrokeSm)}" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity="${GLOW.haloOpSm}" filter="url(#${p}_bSm)"/>`,
    '</g></g>',
    `<g id="${p}_e" mask="url(#${p}_rm)" opacity="0">`,
    `<rect ${fRect} fill="none" pointer-events="none"/>`,
    `<g mask="url(#${p}_fm)">`,
    `<g id="${p}_eI" stroke="white">`,
    `<path id="${p}_eO" stroke-width="${sw(GLOW.extraStrokeOuter)}" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity="${GLOW.extraOpOuter}" filter="url(#${p}_ebO)"/>`,
    `<path id="${p}_eC" stroke-width="${sw(GLOW.extraStrokeCore)}" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity="1.0" filter="url(#${p}_ebC)"/>`,
    '</g></g></g>',
  ].join('');
}


/* ── engine/tween.ts ─────────────────────────────── */
                                           

const ease = {
  linear: (t        ) => t,
  smoothstep: (t        ) => t * t * (3 - 2 * t),
}         ;

                        
               
             
              
               
                  
              
                
 

function tween(from        , to        , dur        , e         = ease.linear)        {
  return { from, to, dur, ease: e, startMs: -1, val: from, done: false };
}

function tweenStart(tw       , nowMs        )       {
  tw.startMs = nowMs;
  tw.val = tw.from;
  tw.done = false;
}

function tweenTick(tw       , nowMs        )         {
  if (tw.done || tw.startMs < 0) return tw.val;
  const t = Math.min(1, (nowMs - tw.startMs) / tw.dur);
  tw.val = tw.from + (tw.to - tw.from) * tw.ease(t);
  if (t >= 1) tw.done = true;
  return tw.val;
}


/* ── engine/glow/glow.ts ─────────────────────────────── */
/**
 * Glow overlay — a luminance-driven halo that tracks the brightest point on
 * the shader's perimeter, plus a tight catch-light.
 *
 * How it works:
 *   1. Samples luminance at N points around the component's perimeter.
 *   2. A state machine tracks which perimeter point is brightest, with dwell
 *      timers and fade-out / fade-in when relocating to a new hotspot.
 *   3. Pre-baked sprites (see `bake.ts`) are drawn onto a small per-instance
 *      canvas at the hotspot, tinted to the shader's colour there, and
 *      clipped to the ring band (or the glyph mask in point mode).
 *
 * The canvas replaces the earlier SVG: four `feGaussianBlur` strokes over a
 * 540×440 filter region re-rasterised on every move — the single biggest
 * idle cost of the effect. Now a move is a few `drawImage` calls inside
 * `clip()` paths, and nothing is drawn at all when the hotspot hasn't moved
 * more than a quarter pixel.
 *
 * Deliberately no `destination-in` / `source-in` compositing on the hot
 * path: WebKit intermittently applies those wrong on accelerated canvases
 * (one frame of the halo unclipped and untinted — a white flash). Tinting is
 * pixel data, clipping is paths; the glyph mask (point mode) multiplies
 * alpha in pixel data too.
 */
                                                                   





                                                 




// ─── Constants ────────────────────────────────────────────────────────────

const RELOCATE_DELTA = 0.05;
/** Wander retarget period. Was 120 ticks at the 15 fps shader rate. */
const WANDER_RETARGET_MS = 120 * (1000 / 15);
/** Per-tick rates in GLOW are defined at the 15 fps shader rate; ticks now
 *  come at display rate mid-fade, so they're rescaled by elapsed time. */
const RATE_TICK_MS = 1000 / 15;
const TINT_HOLD_MS = 2000, TINT_FADE_MS = 400;
const LT_SAT_BOOST = 2.625, LT_VAL_MULT = 1.008, LT_MIN_VAL = 0.31;
const REF_W = 140, REF_H = 40, REF_R = 20;
/** Longest a single tick may advance the fade envelope, ms (~2 frames). */
const ENV_MAX_STEP_MS = 34;
/** Redraw thresholds — below these a frame is skipped entirely. */
const POS_EPS = 0.25, ANG_EPS = 0.01, OP_EPS = 0.004;
/** Point mode: the glyph clip keeps a soft skirt around the letters, like the
 *  ring band's 50 % surround — otherwise on small type the halo's blur is
 *  thrown away and only a hairline glint survives inside the strokes. */
const GLYPH_SKIRT = 0.5, GLYPH_SKIRT_SIGMA = 3.5;

// ─── Types ────────────────────────────────────────────────────────────────

                                              

                              
                                                                            
                                                                        
                                                                        
                                                                             
                                                                      
                                                          
                       
                      
                            
                                
                                                                          
                                                                          
                                           
                              
                          
                                                                                   
                                      
                     
                                                                            
                 
              
               
                
                   
                    
                                                                             
                                                                        
                                                                         
                                                                                  
                          
                                                                               
                                                               
                                                                            
                
                       
                                                                             
                     
                                                              
                                                                            
                                                                          
                                                                                 
                                                                                     
                                                                          
                                                                          
                                                                     
                                                                                      
                                                               
                                                                                             
                                             
                                                                                             
                                                        
                                                                           
                                                                     
               
 

const _pt__m1     = { x: 0, y: 0 };

// ─── Public API ───────────────────────────────────────────────────────────

function injectGlow(container             , opts             )              {
  const { width: W, height: H } = opts;
  const s = opts.scale ?? 1;
  const dpr = Math.min(3, typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1);

  const ratio = shapePerim(W, H, opts.cornerRadius, opts.kind) / rrPerim(REF_W, REF_H, REF_R);
  const haloHL = Math.max(1, GLOW.haloHalfLen * ratio);
  const extraHL = Math.max(0.6, GLOW.extraHalfLen * ratio);
  const halo = bakeHalo(haloHL, s, dpr);
  const extra = bakeExtra(extraHL, s, dpr);

  // Enough room for the halo's full blur skirt plus the outward catch-light.
  const margin = Math.ceil(Math.max(halo.ay, extra.ay) + GLOW.extraOutward * ratio * s + 2);

  const wrap = document.createElement('div');
  wrap.className = 'metal-fx-glow-svg';
  wrap.setAttribute('aria-hidden', 'true');
  const env = document.createElement('div');
  env.className = 'metal-fx-glow-env';
  env.style.cssText = 'position:absolute;inset:0;pointer-events:none;opacity:0';
  const canvas = document.createElement('canvas');
  canvas.className = 'metal-fx-glow-canvas';
  const cw = W + 2 * margin, ch = H + 2 * margin;
  canvas.width = Math.ceil(cw * dpr); canvas.height = Math.ceil(ch * dpr);
  canvas.style.cssText = `position:absolute;left:${-margin}px;top:${-margin}px;width:${cw}px;height:${ch}px;pointer-events:none`;
  env.appendChild(canvas);
  wrap.appendChild(env);
  container.appendChild(wrap);
  const ctx = canvas.getContext('2d', { willReadFrequently: !!opts.maskDataUrl });
  if (!ctx) throw new Error('metal-fx: glow canvas 2D context unavailable');

  const h              = {
    wrap, env, canvas, ctx, surroundPath: null, bandPath: null, maskAlpha: null, maskReady: false, margin, dpr,
    halo, extra,
    haloTint: { canvas: null, img: null, tint: -1, src: null }, extraTint: { canvas: null, img: null, tint: -1, src: null },
    mO: createOutlineBuf(), mI: createOutlineBuf(), maskSum: Number.NaN, maskDeformed: false, deform: null,
    width: W, height: H, cornerRadius: opts.cornerRadius, kind: opts.kind,
    scale: s,
    perim: buildPerimTable(opts),
    pointMode: !!(opts.samplePoints && opts.samplePoints.length > 0),
    currentIdx: 0, appearedAt: 0, glowOpacity: 0,
    relocTween: null, relocNextIdx: -1, relocMul: 0, envClock: 0,
    cursorMode: false, cursorArc: 0, cursorTargetArc: 0, lastTickMs: 0,
    wanderS: 0, wanderTargetS: 0, wanderFrames: 0,
    tintFrom: { r: 255, g: 255, b: 255 }, tintTarget: { r: 255, g: 255, b: 255 }, tintTween: null, tintHoldUntil: 0,
    dX: Number.NaN, dY: Number.NaN, dAng: Number.NaN, dEX: Number.NaN, dEY: Number.NaN, dHOp: Number.NaN, dEOp: Number.NaN,
    dHaloTint: '', dExtraTint: '', dirty: true,
    dEnv: -1,
  };

  if (opts.maskDataUrl) {
    const img = new Image();
    img.onload = () => {
      // Rasterise the glyph mask once at canvas resolution and keep its alpha.
      const c = document.createElement('canvas');
      c.width = canvas.width; c.height = canvas.height;
      const g = c.getContext('2d', { willReadFrequently: true });
      if (!g) return;
      g.scale(dpr, dpr);
      g.drawImage(img, margin, margin, W, H);
      const d = g.getImageData(0, 0, c.width, c.height).data;
      const n = c.width * c.height;
      const glyph = new Float32Array(n);
      for (let i = 0, j = 3; i < n; i++, j += 4) glyph[i] = d[j] / 255;
      const skirt = gaussBlur(Float32Array.from(glyph), c.width, c.height, GLYPH_SKIRT_SIGMA * dpr);
      let peak = 0;
      for (let i = 0; i < n; i++) if (skirt[i] > peak) peak = skirt[i];
      const k = peak > 0 ? GLYPH_SKIRT / peak : 0;
      const a = new Uint8ClampedArray(n);
      for (let i = 0; i < n; i++) a[i] = Math.round(Math.max(glyph[i], skirt[i] * k) * 255);
      h.maskAlpha = a; h.maskReady = true; h.dirty = true;
    };
    img.src = opts.maskDataUrl;
  } else {
    renderMask(h, null);
  }
  return h;
}

// ─── Mask ─────────────────────────────────────────────────────────────────

/**
 * The band clip, as two evenodd paths. Matches the old SVG mask: 50 % outside
 * the ring, 100 % inside the band, 0 in the hole — the halo's blur skirt
 * still spills softly onto the page.
 */
function renderMask(h             , deform                 )       {
  if (h.pointMode) return;
  const { margin: m, width: W, height: H, cornerRadius: R } = h;
  const ringInset = h.kind === 'circle' ? 2 : 1;
  roundRectOutline(0, 0, W, H, R, deform, h.mO);
  roundRectOutline(ringInset, ringInset, W - 2 * ringInset, H - 2 * ringInset, Math.max(0, R - ringInset), deform, h.mI);
  const outer = new Path2D();
  tracePath(outer, h.mO, m);
  const band = new Path2D();
  tracePath(band, h.mO, m);
  tracePath(band, h.mI, m);
  const surround = new Path2D();
  surround.rect(0, 0, W + 2 * m, H + 2 * m);
  surround.addPath(outer);
  h.surroundPath = surround;
  h.bandPath = band;
  h.maskReady = true;
}

function tracePath(p        , buf            , off        )       {
  const xy = buf.xy;
  for (let i = 0; i < buf.n; i++) {
    const x = xy[i * 2] + off, y = xy[i * 2 + 1] + off;
    if (i === 0) p.moveTo(x, y); else p.lineTo(x, y);
  }
  p.closePath();
}

function outlineSum(deform                 , h             )         {
  if (!deform) return 0;
  // Cheap checksum of the deformed outline: every 4th point.
  roundRectOutline(0, 0, h.width, h.height, h.cornerRadius, deform, h.mO);
  let sum = 0;
  const xy = h.mO.xy;
  for (let i = 0; i < h.mO.n; i += 4) sum += xy[i * 2] * 1.37 + xy[i * 2 + 1];
  return sum;
}

/**
 * Keep the glow's band mask (and hotspot) on the deformed outline. Call after
 * every composite while `deform` is set; pass null once to restore the rigid
 * mask. Cheap: a checksum of ~40 points, and a small path fill when changed.
 */
function updateGlowMask(h             , deform                 )       {
  h.deform = deform;
  if (h.pointMode) return;
  if (deform) {
    const sum = outlineSum(deform, h);
    if (sum !== h.maskSum) { h.maskSum = sum; renderMask(h, deform); h.maskDeformed = true; h.dirty = true; }
  } else if (h.maskDeformed) {
    h.maskSum = Number.NaN;
    renderMask(h, null);
    h.maskDeformed = false;
    h.dirty = true;
  }
}

// ─── Per-frame update ─────────────────────────────────────────────────────

/**
 * One glow tick. Returns true while an envelope is animating (relocation
 * fade, tint crossfade, cursor tracking) — the loop then calls again every
 * animation frame so the fade is smooth instead of stepping at 15 fps.
 */
function updateGlow(h             , inst                 , nowMs        , strengthMul        , theme                   = 'dark')          {
  const { width: W, height: H, cornerRadius: R, perim } = h;
  if (perim.length === 0) return false;

  const halfWin = 2;

  let maxLum = -1, maxIdx = h.currentIdx, curLum = 0;
  for (let i = 0; i < perim.length; i++) {
    const pt = perim[i];
    const lum = sampleShaderLumAt(inst, pt.x, pt.y, halfWin);
    if (lum > maxLum) { maxLum = lum; maxIdx = i; }
    if (i === h.currentIdx) curLum = lum;
  }

  const dwellActive = h.appearedAt > 0 && nowMs - h.appearedAt < GLOW.minDwellMs;
  const targetOp = GLOW.baseOp + (GLOW.peakOp - GLOW.baseOp) * smoothstep(GLOW.lumLo, GLOW.lumHi, curLum);
  const rivalDominates = !dwellActive && maxLum - curLum > RELOCATE_DELTA;

  // Cursor as light source: while the pointer is within reach the hotspot
  // faces it (nearest outline point) and its brightness follows proximity.
  // Ring mode only — glyph masks have no continuous outline to slide along.
  const cl = inst.cursorLight;
  const cursorOn = CURSOR_LIGHT.enabled && CURSOR_LIGHT.catchLight && !h.pointMode && !!cl && cl.w > 0.02;
  const perimLen = shapePerim(W, H, R, h.kind);
  if (cursorOn) h.cursorTargetArc = arcAtPoint(cl .x, cl .y, W, H, R, h.kind);
  const cursorOp = cursorOn ? Math.min(1, GLOW.peakOp * CURSOR_LIGHT.catchGain * cl .w) : 0;
  const dtMs = h.lastTickMs > 0 ? Math.min(200, Math.max(0.5, nowMs - h.lastTickMs)) : RATE_TICK_MS;
  h.lastTickMs = nowMs;
  h.envClock += Math.min(dtMs, ENV_MAX_STEP_MS);
  const rate = (perTick        ) => 1 - Math.pow(1 - perTick, dtMs / RATE_TICK_MS);

  // Relocation rules: a hotspot holds for at least `minDwellMs`; moving is
  // always disappear-in-place (relocFadeMs) then appear at the new point
  // (relocFadeMs). The halo never slides along the ring — except in cursor
  // mode, where the light source itself is moving.
  const fadeMs = Math.max(1, GLOW.relocFadeMs);
  const CURSOR_ENTER = -2, CURSOR_EXIT = -3;
  const fadeIn = () => {
    h.appearedAt = nowMs;
    h.wanderS = 0; h.wanderTargetS = 0; h.wanderFrames = 0;
    h.relocTween = tween(0, 1, fadeMs, ease.smoothstep);
    tweenStart(h.relocTween, h.envClock);
  };
  const fadeOut = (next        ) => {
    h.relocNextIdx = next;
    h.relocTween = tween(1, 0, fadeMs, ease.smoothstep);
    tweenStart(h.relocTween, h.envClock);
  };
  if (h.relocTween?.done && h.relocTween.to === 0) {
    // Faded out at the old spot: switch, then fade in at the new one.
    let next = h.relocNextIdx;
    if (next === CURSOR_ENTER && !cursorOn) next = CURSOR_EXIT;
    if (next === CURSOR_EXIT) {
      // Back to the luminance hunt — re-appears below as a first appearance.
      h.cursorMode = false; h.appearedAt = 0; h.relocTween = null;
    } else if (next === CURSOR_ENTER) {
      h.cursorMode = true; h.cursorArc = h.cursorTargetArc; h.glowOpacity = cursorOp;
      fadeIn();
    } else {
      h.currentIdx = next;
      const np = perim[h.currentIdx];
      const nl = sampleShaderLumAt(inst, np.x, np.y, halfWin);
      h.glowOpacity = GLOW.baseOp + (GLOW.peakOp - GLOW.baseOp) * smoothstep(GLOW.lumLo, GLOW.lumHi, nl);
      fadeIn();
    }
  }
  if (!h.relocTween || h.relocTween.done) {
    if (h.appearedAt === 0) {
      // First appearance: face the cursor if it's there, else the brightest point.
      if (cursorOn) { h.cursorMode = true; h.cursorArc = h.cursorTargetArc; h.glowOpacity = cursorOp; }
      else { h.cursorMode = false; h.currentIdx = maxIdx; h.glowOpacity = targetOp; }
      fadeIn();
    } else if (cursorOn !== h.cursorMode) {
      fadeOut(cursorOn ? CURSOR_ENTER : CURSOR_EXIT);
    } else if (!h.cursorMode && rivalDominates) {
      fadeOut(maxIdx);
    }
  }
  if (h.cursorMode) {
    // Proximity-weighted, no lag; holds the last value while fading out.
    if (cursorOn) h.glowOpacity = cursorOp;
    const follow = Math.max(0.01, Math.min(1, CURSOR_LIGHT.catchFollow));
    const fa = 1 - Math.pow(1 - follow, dtMs / (1000 / 60));
    let diff = h.cursorTargetArc - h.cursorArc;
    diff = ((((diff % perimLen) + perimLen * 1.5) % perimLen) - perimLen / 2);
    h.cursorArc += diff * fa;
  } else {
    // Luminance tracking runs continuously; the envelope handles appear/disappear.
    h.glowOpacity += (targetOp - h.glowOpacity) * rate(GLOW.fadeRate);
  }
  h.glowOpacity = Math.max(0, Math.min(1, h.glowOpacity));
  h.relocMul = h.relocTween ? tweenTick(h.relocTween, h.envClock) : 1;

  const ratio = shapePerim(W, H, R, h.kind) / rrPerim(REF_W, REF_H, REF_R);
  const wanderRange = GLOW.wanderRange * ratio;
  h.wanderFrames += dtMs;
  if (h.wanderFrames >= WANDER_RETARGET_MS) { h.wanderTargetS = (Math.random() * 2 - 1) * wanderRange; h.wanderFrames = 0; }
  h.wanderS += (h.wanderTargetS - h.wanderS) * rate(GLOW.wanderLerp);

  let blobX        , blobY        , tangent        , exX        , exY        ;
  if (h.pointMode) {
    // Custom-mask instance: hotspot sits on a sampled glyph point, halo runs
    // horizontally (reads as a glint across the letterforms), wander slides
    // it along x only.
    const p = perim[h.currentIdx];
    blobX = p.x + h.wanderS; blobY = p.y; tangent = 0;
    exX = blobX; exY = blobY;
  } else {
    const blobArc = h.cursorMode ? h.cursorArc : perim[h.currentIdx].arc + h.wanderS;
    // GLOW.inset / GLOW.extraOutward are absolute units; multiply by the
    // master scale so the catch-light sits at the right perpendicular
    // distance when the host element is rendered at non-1× layout.
    const insetS = GLOW.inset * h.scale;
    sampleAtArc(blobArc, W, H, R, insetS, 0, h.kind, _pt__m1);
    blobX = _pt__m1.x; blobY = _pt__m1.y;
    tangent = tangentAngleAtArc(blobArc, W, H, R, insetS, h.kind);
    const extraOut = GLOW.extraOutward * ratio * h.scale;
    sampleAtArc(blobArc, W, H, R, insetS, extraOut, h.kind, _pt__m1);
    exX = _pt__m1.x; exY = _pt__m1.y;
  }
  if (h.deform) {
    h.deform(blobX, blobY, _pt__m1); blobX = _pt__m1.x; blobY = _pt__m1.y;
    h.deform(exX, exY, _pt__m1); exX = _pt__m1.x; exY = _pt__m1.y;
  }

  const light = theme === 'light';
  const samp = light
    ? sampleShaderRGBChromatic(inst, blobX, blobY, halfWin)
    : sampleShaderRGBAt(inst, blobX, blobY, halfWin);

  if (!h.tintTween) {
    h.tintFrom = { ...samp }; h.tintTarget = { ...samp };
    h.tintTween = tween(0, 1, TINT_FADE_MS);
    tweenStart(h.tintTween, nowMs);
    h.tintHoldUntil = light ? 0 : nowMs + TINT_HOLD_MS;
  } else if (h.tintTween.done) {
    if (light) {
      h.tintFrom = {
        r: h.tintFrom.r + (h.tintTarget.r - h.tintFrom.r) * h.tintTween.val,
        g: h.tintFrom.g + (h.tintTarget.g - h.tintFrom.g) * h.tintTween.val,
        b: h.tintFrom.b + (h.tintTarget.b - h.tintFrom.b) * h.tintTween.val,
      };
      h.tintTarget = { ...samp };
      h.tintTween = tween(0, 1, TINT_FADE_MS);
      tweenStart(h.tintTween, nowMs);
    } else if (nowMs >= h.tintHoldUntil) {
      h.tintFrom = { ...h.tintTarget };
      h.tintTarget = { ...samp };
      h.tintTween = tween(0, 1, TINT_FADE_MS);
      tweenStart(h.tintTween, nowMs);
      h.tintHoldUntil = nowMs + TINT_HOLD_MS;
    }
  }
  tweenTick(h.tintTween , nowMs);
  const ft = h.tintTween .val;

  let tR        , tG        , tB        ;
  if (light) {
    tR = Math.round(h.tintFrom.r + (h.tintTarget.r - h.tintFrom.r) * ft);
    tG = Math.round(h.tintFrom.g + (h.tintTarget.g - h.tintFrom.g) * ft);
    tB = Math.round(h.tintFrom.b + (h.tintTarget.b - h.tintFrom.b) * ft);
  } else {
    const hR = h.tintFrom.r + (h.tintTarget.r - h.tintFrom.r) * ft;
    const hG = h.tintFrom.g + (h.tintTarget.g - h.tintFrom.g) * ft;
    const hB = h.tintFrom.b + (h.tintTarget.b - h.tintFrom.b) * ft;
    const peak = Math.max(hR, hG, hB) || 1;
    tR = Math.round(255 * (hR / peak)); tG = Math.round(255 * (hG / peak)); tB = Math.round(255 * (hB / peak));
  }
  const haloTint = `rgb(${tR},${tG},${tB})`;
  let extraTint = '#ffffff';
  if (light) {
    const hsv = rgbToHsv(tR, tG, tB);
    const [er, eg, eb] = hsvToRgb(hsv[0], Math.min(1, hsv[1] * LT_SAT_BOOST), Math.max(LT_MIN_VAL, hsv[2] * LT_VAL_MULT));
    extraTint = `rgb(${er},${eg},${eb})`;
  }

  const m = Math.max(0, Math.min(1, strengthMul)) * (h.pointMode ? GLOW.pointGain : 1);
  const haloOp = Math.min(1, h.glowOpacity * GLOW.haloOpMul * m);
  const extraOp = Math.min(1, h.glowOpacity * GLOW.extraIntensity * m);

  // The appear/disappear envelope is element opacity: a compositor-only
  // change, so it can run every animation frame for free. Only movement,
  // tint and luminance changes redraw the canvas.
  if (Math.abs(h.relocMul - h.dEnv) > 0.002) {
    const arrived = h.relocMul >= 0.998 && h.dEnv < 0.998;
    h.dEnv = h.relocMul;
    h.env.style.opacity = h.relocMul.toFixed(3);
    // Fresh content once fully visible — a nudge for engines that only
    // re-upload a canvas when something in it changes.
    if (arrived) h.dirty = true;
  }

  const animating = !!(h.relocTween && !h.relocTween.done) || h.cursorMode;

  // Skip the draw when nothing visible changed.
  const moved = !(Math.abs(blobX - h.dX) < POS_EPS && Math.abs(blobY - h.dY) < POS_EPS &&
                  Math.abs(tangent - h.dAng) < ANG_EPS &&
                  Math.abs(exX - h.dEX) < POS_EPS && Math.abs(exY - h.dEY) < POS_EPS);
  const faded = !(Math.abs(haloOp - h.dHOp) < OP_EPS && Math.abs(extraOp - h.dEOp) < OP_EPS);
  const tinted = haloTint !== h.dHaloTint || extraTint !== h.dExtraTint;
  if (!(h.dirty || moved || faded || tinted)) return animating;
  h.dX = blobX; h.dY = blobY; h.dAng = tangent; h.dEX = exX; h.dEY = exY;
  h.dHOp = haloOp; h.dEOp = extraOp; h.dHaloTint = haloTint; h.dExtraTint = extraTint;
  h.dirty = false;
  draw(h, blobX, blobY, tangent, exX, exY, haloOp, extraOp, haloTint, extraTint);
  return animating;
}

// ─── Drawing ──────────────────────────────────────────────────────────────

function draw(
  h             ,
  bx        , by        , ang        , ex        , ey        ,
  haloOp        , extraOp        , haloTint        , extraTint        
)       {
  const { ctx: g, canvas: c, dpr, margin: m } = h;
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.globalCompositeOperation = 'source-over';
  g.globalAlpha = 1;
  g.clearRect(0, 0, c.width, c.height);
  if ((haloOp <= 0.002 && extraOp <= 0.002) || !h.maskReady) return;

  const haloImg = haloOp > 0.002 ? tintSprite(h.halo, ...parseRgb(haloTint), h.haloTint) : null;
  const extraImg = extraOp > 0.002
    ? (extraTint === '#ffffff' ? h.extra.canvas : tintSprite(h.extra, ...parseRgb(extraTint), h.extraTint))
    : null;

  const sprites = (mul        ) => {
    if (haloImg) {
      g.save();
      g.translate(bx + m, by + m);
      g.rotate(ang);
      g.globalAlpha = haloOp * mul;
      g.drawImage(haloImg, -h.halo.ax, -h.halo.ay, h.halo.w, h.halo.h);
      g.restore();
    }
    if (extraImg) {
      g.save();
      g.translate(ex + m, ey + m);
      g.rotate(ang);
      g.globalAlpha = extraOp * mul;
      g.drawImage(extraImg, -h.extra.ax, -h.extra.ay, h.extra.w, h.extra.h);
      g.restore();
    }
  };

  if (!h.pointMode && h.surroundPath && h.bandPath) {
    // Two disjoint clip regions: outside the ring at half strength, the band
    // at full. Plain source-over inside each, so nothing for WebKit to get
    // wrong.
    g.save(); g.scale(dpr, dpr); g.clip(h.surroundPath, 'evenodd'); sprites(0.5); g.restore();
    g.save(); g.scale(dpr, dpr); g.clip(h.bandPath, 'evenodd'); sprites(1); g.restore();
    return;
  }

  // Point mode: draw, then multiply alpha by the glyph mask in pixel data.
  g.save(); g.scale(dpr, dpr); sprites(1); g.restore();
  const a = h.maskAlpha;
  if (!a) return;
  const img = g.getImageData(0, 0, c.width, c.height);
  const d = img.data;
  for (let i = 0, j = 3; i < a.length; i++, j += 4) {
    const ma = a[i];
    if (ma === 255) continue;
    if (ma === 0) { d[j] = 0; continue; }
    d[j] = (d[j] * ma + 127) / 255;
  }
  g.putImageData(img, 0, 0);
}

const _rgb__m1                           = [255, 255, 255];
function parseRgb(css        )                           {
  if (css[0] === '#') {
    _rgb__m1[0] = parseInt(css.slice(1, 3), 16); _rgb__m1[1] = parseInt(css.slice(3, 5), 16); _rgb__m1[2] = parseInt(css.slice(5, 7), 16);
    return _rgb__m1;
  }
  // "rgb(r,g,b)"
  let i = 4, n = 0, k = 0;
  while (i < css.length && k < 3) {
    const ch = css.charCodeAt(i++);
    if (ch >= 48 && ch <= 57) n = n * 10 + (ch - 48);
    else if (ch === 44 || ch === 41) { _rgb__m1[k++] = n; n = 0; }
  }
  return _rgb__m1;
}

/**
 * Carry the visible state of a glow across a rebuild (a real resize), so the
 * halo keeps its hotspot, brightness and fade instead of restarting from
 * invisible. Only state — never geometry, sprites or masks.
 */
function carryGlowState(prev             , next             )       {
  if (prev.pointMode !== next.pointMode) return;
  next.currentIdx = Math.min(prev.currentIdx, Math.max(0, next.perim.length - 1));
  next.appearedAt = prev.appearedAt;
  next.glowOpacity = prev.glowOpacity;
  next.relocTween = prev.relocTween;
  next.relocNextIdx = prev.relocNextIdx;
  next.relocMul = prev.relocMul;
  next.envClock = prev.envClock;
  next.cursorMode = prev.cursorMode;
  next.cursorArc = prev.cursorArc;
  next.cursorTargetArc = prev.cursorTargetArc;
  next.lastTickMs = prev.lastTickMs;
  next.wanderS = prev.wanderS; next.wanderTargetS = prev.wanderTargetS; next.wanderFrames = prev.wanderFrames;
  next.tintFrom = prev.tintFrom; next.tintTarget = prev.tintTarget;
  next.tintTween = prev.tintTween; next.tintHoldUntil = prev.tintHoldUntil;
  next.dEnv = prev.relocMul;
  next.env.style.opacity = prev.relocMul.toFixed(3);
}

function resizeGlow(handles             , container             , opts             )              {
  for (const el of Array.from(container.querySelectorAll('.metal-fx-glow-svg'))) {
    if (el.parentNode === container) container.removeChild(el);
  }
  void handles;
  return injectGlow(container, opts);
}


/* ── engine/reflection/constants.ts ─────────────────────────────── */
/**
 * Canonical constants and types for proximity reflections.
 *
 * Verbatim from `Image loader/index.html` L5851-5915. Shared across
 * the observer, geometry, and paint modules.
 */
                                                        

const RANGE_PX = 12;
const ATTACH_RANGE_PX = 32;
const OVERLAP_MIN_PX = 1;
const BASE_ALPHA = 0.55;
const BOOST_ALPHA = 1.0;
const GRAD_NEAR = 1.0;
const GRAD_MID = 0.85;
const GRAD_FAR = 0.0;
const INTENSITY_MULT = 1.3;
const MAX_ALPHA_STACK = 3.6;
const GLOBAL_ATTENUATION = 0.7;
const STROKE_CSS_PX = 1;
const STROKE_EXTRA_ALPHA = 0.52;
const BORDER_HILITE_PX = 1.0;
const BORDER_HILITE_ALPHA = 0.044;
const REF_DRAW_CSS_W = 235;
const FILL_EXTRA_ALPHA = 2.535;
const FILL_OPACITY_MUL = 0.7;
const FILL_CIRCLE_ATTENUATION = 0.5;

const REFLECTION_BLOCKED_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'OPTION']);

                                   
                  
                          
                        
                                                                        
                   
                       
                            
                                
                                  
                                      
                       
                        
                             
                                   
                            
                                        
                                            
                                                         
                        
 


/* ── engine/reflection/geometry.ts ─────────────────────────────── */
/**
 * Canvas 2D drawing primitives for proximity reflections.
 *
 * All the low-level compositing passes — rounded-rect paths, ring clips,
 * fill/stroke multi-pass alpha stacking, mirror-flip drawImage, and the
 * border-highlight gradient stroke.
 */


// ─── Layout helpers ───────────────────────────────────────────────────────

function shortestRectDistance(a         , b         )         {
  const dx = Math.max(a.left - b.right, b.left - a.right, 0);
  const dy = Math.max(a.top - b.bottom, b.top - a.bottom, 0);
  return Math.sqrt(dx * dx + dy * dy);
}

function isHorizontalNeighbour(anchorRect         , targetRect         , overlapMin        , attachRange        )          {
  const verticalOverlap =
    Math.min(anchorRect.bottom, targetRect.bottom) -
    Math.max(anchorRect.top, targetRect.top);
  if (verticalOverlap < overlapMin) return false;
  const horizontalGap = Math.max(
    anchorRect.left - targetRect.right,
    targetRect.left - anchorRect.right,
    0
  );
  if (horizontalGap > attachRange) return false;
  return true;
}

function isVerticalNeighbour(anchorRect         , targetRect         , overlapMin        , attachRange        )          {
  const horizontalOverlap =
    Math.min(anchorRect.right, targetRect.right) -
    Math.max(anchorRect.left, targetRect.left);
  if (horizontalOverlap < overlapMin) return false;
  const verticalGap = Math.max(
    anchorRect.top - targetRect.bottom,
    targetRect.top - anchorRect.bottom,
    0
  );
  return verticalGap <= attachRange;
}

// ─── Path helpers ─────────────────────────────────────────────────────────

function roundRectPath(
  ctx                          ,
  x        ,
  y        ,
  w        ,
  h        ,
  r        
)       {
  const rr = Math.max(0, Math.min(r, w * 0.5, h * 0.5));
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const native = (ctx       ).roundRect;
  if (typeof native === 'function') {
    native.call(ctx, x, y, w, h, rr);
    return;
  }
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  ctx.lineTo(x + rr, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - rr);
  ctx.lineTo(x, y + rr);
  ctx.quadraticCurveTo(x, y, x + rr, y);
}

// ─── Draw source (mirror flip) ───────────────────────────────────────────

                          
            
            
            
            
                 
                 
                                                                            
                                                                            
              
              
 

function drawSource(
  ctx                          ,
  src                   ,
  sw        ,
  sh        ,
  dst         
)       {
  if (!dst.flipX && !dst.flipY) {
    ctx.drawImage(src, dst.sx ?? 0, dst.sy ?? 0, sw, sh, dst.x, dst.y, dst.w, dst.h);
    return;
  }
  ctx.save();
  if (dst.flipX) {
    ctx.translate(dst.x + dst.w, 0);
    ctx.scale(-1, 1);
  }
  if (dst.flipY) {
    ctx.translate(0, dst.y + dst.h);
    ctx.scale(1, -1);
  }
  ctx.drawImage(
    src,
    dst.sx ?? 0,
    dst.sy ?? 0,
    sw,
    sh,
    dst.flipX ? 0 : dst.x,
    dst.flipY ? 0 : dst.y,
    dst.w,
    dst.h
  );
  ctx.restore();
}

// ─── Clip + compositing passes ────────────────────────────────────────────

                          
            
            
            
            
            
 

const FILL_BLUR_CSS_PX = 4;

function fillRingClip(
  ctx                          ,
  x        , y        , w        , h        ,
  radiusDevPx        , bandDevPx        
)       {
  if (w <= 2 * bandDevPx || h <= 2 * bandDevPx) {
    ctx.beginPath();
    roundRectPath(ctx, x, y, w, h, radiusDevPx);
    ctx.clip();
    return;
  }
  ctx.beginPath();
  roundRectPath(ctx, x, y, w, h, radiusDevPx);
  roundRectPath(ctx, x + bandDevPx, y + bandDevPx, w - 2 * bandDevPx, h - 2 * bandDevPx, Math.max(0, radiusDevPx - bandDevPx));
  ctx.clip('evenodd');
}

function maskedFillPasses(
  ctx                          ,
  src                   ,
  sw        , sh        ,
  tw        , th        ,
  totalAlpha        ,
  grad                ,
  dst         ,
  fillBox         ,
  dpr        ,
  /** Override for the edge band the fill is clipped to, device px. Glyph
   *  targets pass the whole box — letters have no "rim" to hug. */
  bandDevPxOverride         
)       {
  const fillBandDevPx = bandDevPxOverride ?? Math.max(1, Math.round((RANGE_PX + FILL_BLUR_CSS_PX * 3) * dpr));
  let remaining = Math.max(0, totalAlpha);
  let firstChunk = true;
  for (let i = 0; i < 3 && remaining > 1e-4; i++) {
    const a = Math.min(1, remaining);
    ctx.save();
    fillRingClip(ctx, fillBox.x, fillBox.y, fillBox.w, fillBox.h, fillBox.r, fillBandDevPx);
    ctx.globalCompositeOperation = firstChunk ? 'source-over' : 'lighter';
    firstChunk = false;
    ctx.globalAlpha = a;
    drawSource(ctx, src, sw, sh, dst);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'destination-in';
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, tw, th);
    ctx.restore();
    remaining -= a;
  }
}

function insideStrokeEvenOddClip(
  ctx                          ,
  x        , y        , w        , h        ,
  radiusDevPx        , strokeDevPx        
)       {
  const r = strokeDevPx | 0;
  if (r < 1 || w <= 2 * r || h <= 2 * r) {
    ctx.beginPath();
    roundRectPath(ctx, x, y, w, h, radiusDevPx);
    ctx.clip();
    return;
  }
  ctx.beginPath();
  roundRectPath(ctx, x, y, w, h, radiusDevPx);
  roundRectPath(ctx, x + r, y + r, w - 2 * r, h - 2 * r, Math.max(0, radiusDevPx - r));
  ctx.clip('evenodd');
}

function maskedStrokePasses(
  ctx                          ,
  src                   ,
  sw        , sh        ,
  tw        , th        ,
  strokeBox         ,
  intensity        ,
  strokeBandPx        ,
  grad                ,
  strokeExtraAlpha        ,
  dst         
)       {
  let remaining = intensity * strokeExtraAlpha;
  let firstChunk = true;
  for (let i = 0; i < 3 && remaining > 1e-4; i++) {
    const a = Math.min(1, remaining);
    ctx.save();
    insideStrokeEvenOddClip(ctx, strokeBox.x, strokeBox.y, strokeBox.w, strokeBox.h, strokeBox.r, strokeBandPx);
    ctx.globalCompositeOperation = firstChunk ? 'source-over' : 'lighter';
    firstChunk = false;
    ctx.globalAlpha = a;
    drawSource(ctx, src, sw, sh, dst);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'destination-in';
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, tw, th);
    ctx.restore();
    remaining -= a;
  }
}

function drawBorderHighlight(
  ctx                          ,
  strokeBox         ,
  strokeDevPx        ,
  g0x        , g0y        ,
  g1x        , g1y        ,
  alpha        
)       {
  const grad = ctx.createLinearGradient(g0x, g0y, g1x, g1y);
  grad.addColorStop(0, `rgba(255,255,255,${alpha.toFixed(3)})`);
  grad.addColorStop(0.5, `rgba(255,255,255,${(alpha * 0.45).toFixed(3)})`);
  grad.addColorStop(1, 'rgba(255,255,255,0)');

  ctx.save();
  insideStrokeEvenOddClip(ctx, strokeBox.x, strokeBox.y, strokeBox.w, strokeBox.h, strokeBox.r, strokeDevPx);
  ctx.globalCompositeOperation = 'lighter';
  ctx.lineWidth = strokeDevPx * 2;
  ctx.strokeStyle = grad;
  ctx.beginPath();
  roundRectPath(ctx, strokeBox.x, strokeBox.y, strokeBox.w, strokeBox.h, strokeBox.r);
  ctx.stroke();
  ctx.restore();
}


/* ── engine/reflection/observers.ts ─────────────────────────────── */
/**
 * Style observation for reflection targets.
 *
 * Reads corner radii and hairline specs from computed styles, and
 * attaches ResizeObserver / MutationObserver so values stay fresh
 * without per-frame getComputedStyle calls.
 */
                                                    

function readCornerRadius(el             )         {
  const cs = getComputedStyle(el);
  const radii = [
    parseFloat(cs.borderTopLeftRadius) || 0,
    parseFloat(cs.borderTopRightRadius) || 0,
    parseFloat(cs.borderBottomRightRadius) || 0,
    parseFloat(cs.borderBottomLeftRadius) || 0,
  ].filter((v) => v > 0);
  return radii.length ? Math.min.apply(null, radii) : 0;
}

/**
 * Read the visible "hairline" geometry of the host so the 1-px stroke
 * reflection sits exactly on the host's existing ring.
 *
 * Returns the visible thickness (`width`) and the OUTWARD extent past the
 * padding-box edge (`outerCssPx`) — the wrap is overscanned by `outerCssPx`
 * on every side so its outer rim lines up with the host's visible silhouette.
 *
 * Source contributions:
 *   - CSS `border-*-width` (max across the 4 sides)
 *   - smallest `inset` `box-shadow` with spread > 0
 *   - smallest outset `box-shadow` with spread > 0
 */
function readHairlineSpec(el             )                                        {
  const cs = getComputedStyle(el);
  const borderMax = Math.max(
    parseFloat(cs.borderTopWidth) || 0,
    parseFloat(cs.borderRightWidth) || 0,
    parseFloat(cs.borderBottomWidth) || 0,
    parseFloat(cs.borderLeftWidth) || 0
  );

  let smallestInsetSpread = 0;
  let smallestOutsetSpread = 0;
  const shadow = cs.boxShadow;
  if (shadow && shadow !== 'none') {
    const safe = shadow.replace(/rgba?\([^)]*\)/g, (m) => m.replace(/,/g, '\u0000'));
    const parts = safe.split(/,\s*/);
    let inset = Infinity;
    let outset = Infinity;
    for (const part of parts) {
      const nums = part.match(/-?\d+(?:\.\d+)?px/g);
      if (!nums || nums.length < 4) continue;
      const spread = parseFloat(nums[3]);
      if (!(spread > 0)) continue;
      if (/\binset\b/.test(part)) {
        if (spread < inset) inset = spread;
      } else if (spread < outset) {
        outset = spread;
      }
    }
    if (Number.isFinite(inset)) smallestInsetSpread = inset;
    if (Number.isFinite(outset)) smallestOutsetSpread = outset;
  }

  const outerCssPx = Math.max(borderMax, smallestOutsetSpread);
  const width =
    Math.max(borderMax, smallestInsetSpread, smallestOutsetSpread) || 1;

  return { width, outerCssPx };
}

function refreshTargetStyles(t                  )       {
  t.cornerRadius = readCornerRadius(t.el);
  const spec = readHairlineSpec(t.el);
  t.hairlineWidth = spec.width;
  t.hairlineOuterCssPx = spec.outerCssPx;
}

function attachObservers(t                  )       {
  if (typeof ResizeObserver !== 'undefined') {
    t.resizeObserver = new ResizeObserver(() => refreshTargetStyles(t));
    t.resizeObserver.observe(t.el);
  }
  if (typeof MutationObserver !== 'undefined') {
    t.mutationObserver = new MutationObserver(() => refreshTargetStyles(t));
    t.mutationObserver.observe(t.el, {
      attributes: true,
      attributeFilter: ['style', 'class'],
    });
  }
}

function detachObservers(t                  )       {
  t.resizeObserver?.disconnect();
  t.resizeObserver = null;
  t.mutationObserver?.disconnect();
  t.mutationObserver = null;
}


/* ── engine/reflection/paint.ts ─────────────────────────────── */
/** Proximity reflection — public API and per-frame paint loop. */
                                                        




                                                    

const targets                        = new Set();

// ─── Cursor occluder ──────────────────────────────────────────────────────
// The reflection is light leaving the anchor and landing on the target. A
// pointer sitting in the gap between them blocks some of that light, so we
// cut a soft shadow band out of the painted reflection at the pointer's
// position across the layout axis. The band widens with distance from the
// target (penumbra from an extended source) and is only applied while the
// pointer is actually inside the gap.

                                           
                   
                                                                              
                 
                                                                              
                   
                                                                           
                   
                                                                         
                                                                
                  
                                                                       
                   
                                                                   
                   
                                                      
                    
 

const REFLECTION_OCCLUDER_DEFAULTS                                     = Object.freeze({
  enabled: true,
  radius: 20,
  strength: 1,
  penumbra: 0.55,
  falloff: 0.21,
  edgeFade: 0.7,
  softness: 0.24,
  repaintMs: 36,
});

const REFLECTION_OCCLUDER                           = { ...REFLECTION_OCCLUDER_DEFAULTS };

function setReflectionOccluderConfig(patch                                   )       {
  Object.assign(REFLECTION_OCCLUDER, patch);
  scheduleOccluderRepaint();
}

function resetReflectionOccluderConfig()       {
  setReflectionOccluderConfig({ ...REFLECTION_OCCLUDER_DEFAULTS });
}

let occluder                                  = null;
let occluderRaf = 0;
let occluderLastMs = 0;
let pointerTracked = false;

function scheduleOccluderRepaint()       {
  if (occluderRaf !== 0 || typeof requestAnimationFrame === 'undefined') return;
  occluderRaf = requestAnimationFrame((now) => {
    occluderRaf = 0;
    // 30 fps is plenty for a shadow that follows a hand.
    if (now - occluderLastMs < REFLECTION_OCCLUDER.repaintMs) { scheduleOccluderRepaint(); return; }
    occluderLastMs = now;
    paintReflections();
  });
}

// Repaint only while the pointer can actually cast a shadow — inside the
// region spanning some anchor and its target (expanded by the occluder
// radius) — plus one more repaint on the way out to clear it. Without this
// every mouse move anywhere on the page re-rasterised every reflection.
let occluderWasNear = false;
function pointerNearAnyGap(x        , y        )          {
  const r = REFLECTION_OCCLUDER.radius;
  for (const t of targets) {
    const a = t.anchorEl.getBoundingClientRect();
    const b = t.el.getBoundingClientRect();
    const l = Math.min(a.left, b.left) - r, rt = Math.max(a.right, b.right) + r;
    const tp = Math.min(a.top, b.top) - r, bt = Math.max(a.bottom, b.bottom) + r;
    if (x >= l && x <= rt && y >= tp && y <= bt) return true;
  }
  return false;
}
function onOccluderMove(e              )       {
  occluder = { x: e.clientX, y: e.clientY };
  if (!REFLECTION_OCCLUDER.enabled) return;
  const near = pointerNearAnyGap(e.clientX, e.clientY);
  if (near || occluderWasNear) scheduleOccluderRepaint();
  occluderWasNear = near;
}
function onOccluderLeave()       {
  occluder = null;
  if (occluderWasNear) scheduleOccluderRepaint();
  occluderWasNear = false;
}

function ensurePointerTracking(on         )       {
  if (typeof document === 'undefined' || on === pointerTracked) return;
  pointerTracked = on;
  if (on) {
    document.addEventListener('pointermove', onOccluderMove, { passive: true });
    document.addEventListener('pointerleave', onOccluderLeave);
    window.addEventListener('blur', onOccluderLeave);
  } else {
    document.removeEventListener('pointermove', onOccluderMove);
    document.removeEventListener('pointerleave', onOccluderLeave);
    window.removeEventListener('blur', onOccluderLeave);
    occluder = null;
  }
}

/**
 * Cut the pointer's shadow out of a freshly painted reflection.
 * `horiz` — layout axis; light travels along x when true.
 */
function applyOccluderShadow(
  ctx                          ,
  strokeCtx                          ,
  aRect         ,
  tRect         ,
  horiz         ,
  tw        ,
  th        ,
  overscanCssPx        ,
  dpr        
)       {
  if (!occluder) return;
  const cfg = REFLECTION_OCCLUDER;
  if (!cfg.enabled || cfg.strength <= 0) return;
  const r = cfg.radius;

  // Gap along the layout axis between the two facing edges, and the overlap
  // band across it. Pointer must be inside (expanded by r) for any effect.
  let gapStart        , gapEnd        , along        , across        , bandLo        , bandHi        ;
  if (horiz) {
    const anchorRight = aRect.left >= tRect.right;
    gapStart = anchorRight ? tRect.right : aRect.right;   // target-side edge
    gapEnd = anchorRight ? aRect.left : tRect.left;       // anchor-side edge
    along = occluder.x; across = occluder.y;
    bandLo = Math.max(aRect.top, tRect.top); bandHi = Math.min(aRect.bottom, tRect.bottom);
  } else {
    const anchorBelow = aRect.top >= tRect.bottom;
    gapStart = anchorBelow ? tRect.bottom : aRect.bottom;
    gapEnd = anchorBelow ? aRect.top : tRect.top;
    along = occluder.y; across = occluder.x;
    bandLo = Math.max(aRect.left, tRect.left); bandHi = Math.min(aRect.right, tRect.right);
  }
  const lo = Math.min(gapStart, gapEnd), hi = Math.max(gapStart, gapEnd);
  const gapW = Math.max(1, hi - lo);
  if (along < lo - r || along > hi + r) return;
  if (across < bandLo - r || across > bandHi + r) return;

  // 0 at the target's edge, 1 at the anchor's edge.
  const t = Math.max(0, Math.min(1, Math.abs(along - gapStart) / gapW));
  // Fade at the gap's ends so entering/leaving doesn't pop.
  const fadePx = Math.max(0.5, r * cfg.edgeFade);
  const endFade = Math.min(1, Math.min(along - (lo - r), (hi + r) - along) / fadePx);
  const depth = cfg.strength * (1 - cfg.falloff * t) * endFade;
  if (depth <= 0.001) return;

  const halfBand = r * dpr * (1 + cfg.penumbra * t);
  // Position across the target, in the target canvas' device space.
  const c = horiz
    ? (across - tRect.top + overscanCssPx) * dpr
    : (across - tRect.left + overscanCssPx) * dpr;

  for (const c2d of [ctx, strokeCtx]) {
    c2d.save();
    c2d.setTransform(1, 0, 0, 1, 0, 0);
    c2d.globalCompositeOperation = 'destination-out';
    const g = horiz
      ? c2d.createLinearGradient(0, c - halfBand, 0, c + halfBand)
      : c2d.createLinearGradient(c - halfBand, 0, c + halfBand, 0);
    // softness 1 → triangle; 0 → flat plateau across the whole band.
    const core = Math.max(0, Math.min(0.5, (1 - cfg.softness) * 0.5));
    const d = `rgba(0,0,0,${depth.toFixed(3)})`;
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(0.5 - core, d);
    g.addColorStop(0.5 + core, d);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    c2d.fillStyle = g;
    if (horiz) c2d.fillRect(0, c - halfBand, tw, halfBand * 2);
    else c2d.fillRect(c - halfBand, 0, halfBand * 2, th);
    c2d.restore();
  }
}

// Scratch pair for multi-edge (contained) targets. Each masked pass ends with
// a `destination-in` gradient over the whole ring clip, which would erase the
// previous edge's ink — so every edge after the first paints here and is
// composited back with `lighter`.
let scratchFill                           = null;
let scratchStroke                           = null;
let scratchFillCtx                                  = null;
let scratchStrokeCtx                                  = null;
function ensureScratch(w        , h        )          {
  if (!scratchFill) {
    scratchFill = document.createElement('canvas');
    scratchStroke = document.createElement('canvas');
    scratchFillCtx = scratchFill.getContext('2d', { alpha: true });
    scratchStrokeCtx = scratchStroke.getContext('2d', { alpha: true });
  }
  if (!scratchFillCtx || !scratchStrokeCtx || !scratchFill || !scratchStroke) return false;
  if (scratchFill.width !== w) { scratchFill.width = w; scratchStroke.width = w; }
  if (scratchFill.height !== h) { scratchFill.height = h; scratchStroke.height = h; }
  scratchFillCtx.setTransform(1, 0, 0, 1, 0, 0);
  scratchStrokeCtx.setTransform(1, 0, 0, 1, 0, 0);
  scratchFillCtx.globalCompositeOperation = 'source-over';
  scratchStrokeCtx.globalCompositeOperation = 'source-over';
  scratchFillCtx.clearRect(0, 0, w, h);
  scratchStrokeCtx.clearRect(0, 0, w, h);
  return true;
}

function addReflectionTarget(
  el             ,
  anchor                 ,
  anchorEl             ,
  strength = 1
)                          {
  if (typeof document === 'undefined') return null;
  if (REFLECTION_BLOCKED_TAGS.has(el.tagName)) return null;
  for (const existing of targets) {
    if (existing.el === el) { existing.strength = strength; return existing; }
  }

  const wrap = document.createElement('div');
  wrap.setAttribute('data-metal-fx-reflection', '');
  wrap.setAttribute('aria-hidden', 'true');

  const canvas = document.createElement('canvas');
  canvas.className = 'metal-fx-reflection-canvas';
  const ctx = canvas.getContext('2d', { alpha: true });
  if (!ctx) return null;

  const strokeCanvas = document.createElement('canvas');
  strokeCanvas.className = 'metal-fx-reflection-stroke-canvas';
  const strokeCtx = strokeCanvas.getContext('2d', { alpha: true });
  if (!strokeCtx) return null;

  wrap.appendChild(canvas);
  wrap.appendChild(strokeCanvas);

  const cs = getComputedStyle(el);
  let appliedPositionRelative = false;
  if (cs.position === 'static') {
    el.style.position = 'relative';
    appliedPositionRelative = true;
  }
  let appliedIsolation = false;
  if (cs.isolation !== 'isolate') {
    el.style.isolation = 'isolate';
    appliedIsolation = true;
  }
  el.setAttribute('data-metal-fx-reflect-host', '');
  el.insertBefore(wrap, el.firstChild);

  const initialSpec = readHairlineSpec(el);
  const target                   = {
    el,
    anchor,
    anchorEl,
    strength,
    wrap,
    canvas,
    ctx,
    strokeCanvas,
    strokeCtx,
    cornerRadius: readCornerRadius(el),
    hairlineWidth: initialSpec.width,
    hairlineOuterCssPx: initialSpec.outerCssPx,
    appliedPositionRelative,
    appliedIsolation,
    resizeObserver: null,
    mutationObserver: null,
  };
  attachObservers(target);
  targets.add(target);
  ensurePointerTracking(true);
  return target;
}

function removeReflectionTarget(el             )       {
  for (const target of targets) {
    if (target.el === el) {
      detachObservers(target);
      target.canvas.width = 0;
      target.canvas.height = 0;
      target.strokeCanvas.width = 0;
      target.strokeCanvas.height = 0;
      if (target.wrap.parentNode === target.el) {
        target.el.removeChild(target.wrap);
      }
      target.el.removeAttribute('data-metal-fx-reflect-host');
      if (target.appliedPositionRelative) target.el.style.position = '';
      if (target.appliedIsolation) target.el.style.isolation = '';
      targets.delete(target);
      if (targets.size === 0) ensurePointerTracking(false);
      return;
    }
  }
}

/** Bounding box of pixels with alpha > 8 inside a sub-rect, device px. */
function alphaBBox(
  canvas                   , x0        , y0        , w        , h        
)                                                        {
  if (w < 1 || h < 1) return null;
  const g = canvas.getContext('2d');
  if (!g) return null;
  const d = g.getImageData(x0, y0, w, h).data;
  let minX = w, minY = h, maxX = -1, maxY = -1;
  for (let y = 0; y < h; y++) {
    const row = y * w;
    for (let x = 0; x < w; x++) {
      if (d[(row + x) * 4 + 3] > 8) {
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < 0) return null;
  return { x: x0 + minX, y: y0 + minY, w: maxX - minX + 1, h: maxY - minY + 1 };
}

function paintReflections()       {
  if (targets.size === 0) return;
  const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;

  const anchorRects = new Map                      ();

  for (const t of targets) {
    const tRect = t.el.getBoundingClientRect();
    let aRect = anchorRects.get(t.anchorEl);
    if (!aRect) {
      aRect = t.anchorEl.getBoundingClientRect();
      anchorRects.set(t.anchorEl, aRect);
    }
    if (tRect.width < 1 || tRect.height < 1) continue;
    if (aRect.width < 1 || aRect.height < 1) continue;
    // Glyph targets (text masked to its letterforms, see `data-metal-fx-text`)
    // are lit differently from a chip: no rim stroke or border highlight —
    // those are a white hairline along a box edge, which on text reads as a
    // flat light on the last letter — just the mirrored metal itself, at its
    // natural size so its banding stays legible, fading over a longer run so
    // more than one letter catches it.
    const glyph = t.el.hasAttribute('data-metal-fx-text');
    if (glyph && !t.glyphStyled) {
      // The chip blur (4 px) exists to melt the ring into a soft rim glow. On
      // letters it erases exactly the detail the mirror should show — the
      // stripes and dispersion fringes — so keep it to anti-aliasing width.
      t.canvas.style.filter = 'blur(0.4px) saturate(1.35) brightness(1.2)';
      t.glyphStyled = true;
    }

    if (
      !isHorizontalNeighbour(aRect, tRect, OVERLAP_MIN_PX, ATTACH_RANGE_PX) &&
      !isVerticalNeighbour(aRect, tRect, OVERLAP_MIN_PX, ATTACH_RANGE_PX)
    ) {
      if (t.canvas.width !== 1) { t.canvas.width = 1; t.canvas.height = 1; }
      if (t.strokeCanvas.width !== 1) { t.strokeCanvas.width = 1; t.strokeCanvas.height = 1; }
      continue;
    }

    // Glyph target on a masked anchor: mirror the metal *sheet*, not the
    // three thin letters cut from it. A mirror facing the "Pro" glyphs shows
    // the material's stripes across its whole face; the masked canvas would
    // give mostly transparency with a few slivers.
    const useRaw = glyph && !!t.anchor.mask;
    if (useRaw && !t.anchor.wantRaw) t.anchor.wantRaw = true;
    const anchorCanvas = (useRaw && t.anchor.rawCanvas) ? t.anchor.rawCanvas : t.anchor.canvas;
    // Sample only the anchor's CSS box. While a vector bend is active the
    // canvas carries an `overscan` margin on every side; reading it whole
    // would shrink the ring to the middle of the slice and miss the band.
    const ovs = Math.round(t.anchor.overscan * dpr);
    let ssx = ovs, ssy = ovs;
    let sw = (anchorCanvas.width | 0) - 2 * ovs;
    let sh = (anchorCanvas.height | 0) - 2 * ovs;
    // Custom-mask anchors (metal text): the metal is wherever the mask
    // painted, not at the box edge. Crop the source to its alpha bounding
    // box so the glyphs' edge — not the padding — lands on the target.
    if (t.anchor.mask && !useRaw) {
      const bb = alphaBBox(anchorCanvas, ssx, ssy, sw, sh);
      if (bb) { ssx = bb.x; ssy = bb.y; sw = bb.w; sh = bb.h; }
    }
    if (sw < 4 || sh < 4) continue;

    const acx = (aRect.left + aRect.right) * 0.5;
    const acy = (aRect.top + aRect.bottom) * 0.5;
    const tcx = (tRect.left + tRect.right) * 0.5;
    const tcy = (tRect.top + tRect.bottom) * 0.5;
    const dx = acx - tcx;
    const dy = acy - tcy;

    const edgeGapH = Math.max(aRect.left - tRect.right, tRect.left - aRect.right, 0);
    const edgeGapV = Math.max(aRect.top - tRect.bottom, tRect.top - aRect.bottom, 0);
    const isHorizontalLayout = edgeGapH >= edgeGapV;

    const dist = shortestRectDistance(aRect, tRect);
    let proximity = 1 - Math.min(1, dist / RANGE_PX);
    proximity = proximity * proximity * (3 - 2 * proximity);
    const intensity = BASE_ALPHA + (BOOST_ALPHA - BASE_ALPHA) * proximity;

    const reflectionAlpha = Math.min(
      MAX_ALPHA_STACK,
      intensity * INTENSITY_MULT * GLOBAL_ATTENUATION
    ) * t.strength;

    // A target that contains the anchor (the card the button lives in) has no
    // single "facing" edge — the button sits near a corner, so the echo lands
    // on both the closest vertical and closest horizontal inner edge.
    const contained =
      aRect.left >= tRect.left && aRect.right <= tRect.right &&
      aRect.top >= tRect.top && aRect.bottom <= tRect.bottom;
    const layouts            = contained ? [true, false] : [isHorizontalLayout];

    // Effective scale of the host element. Anything drawn on the reflection
    // canvas (strokes, border-highlight) is in DEVICE pixels, so it doesn't
    // automatically grow when the host is rendered at non-1× layout (CSS
    // zoom: 2, etc.). Multiply absolute-pixel constants by the anchor's
    // scale so the reflection scales together with the metal effect itself.
    const sScale = t.anchor.scale ?? 1;
    const hairlineCssPx = Math.max(STROKE_CSS_PX * sScale, t.hairlineWidth);
    const strokeBandPx = Math.max(1, Math.round(hairlineCssPx * dpr));
    const borderHighlightPx = Math.max(
      1,
      Math.round(Math.max(BORDER_HILITE_PX * sScale, t.hairlineWidth) * dpr)
    );

    const overscanCssPx = t.hairlineOuterCssPx;
    t.wrap.style.inset = `${-overscanCssPx}px`;
    t.wrap.style.borderRadius = `${Math.max(0, t.cornerRadius)}px`;

    const tw = Math.max(1, Math.round((tRect.width + overscanCssPx * 2) * dpr));
    const th = Math.max(1, Math.round((tRect.height + overscanCssPx * 2) * dpr));
    if (t.canvas.width !== tw) t.canvas.width = tw;
    if (t.canvas.height !== th) t.canvas.height = th;
    if (t.strokeCanvas.width !== tw) t.strokeCanvas.width = tw;
    if (t.strokeCanvas.height !== th) t.strokeCanvas.height = th;

    const ctx = t.ctx;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, tw, th);
    const strokeCtx = t.strokeCtx;
    strokeCtx.setTransform(1, 0, 0, 1, 0, 0);
    strokeCtx.clearRect(0, 0, tw, th);

    for (const [li, horiz] of layouts.entries()) {
      // First edge paints straight into the target; later edges go via scratch.
      const viaScratch = li > 0 && ensureScratch(tw, th);
      const fCtx = viaScratch ? (scratchFillCtx                            ) : ctx;
      const sCtx = viaScratch ? (scratchStrokeCtx                            ) : strokeCtx;
      const bandDevPx = Math.min((glyph ? RANGE_PX * 1.5 : RANGE_PX) * dpr, Math.max(tw, th));
      let g0x        , g0y        , g1x        , g1y        ;
      if (horiz) {
        g0x = dx > 0 ? tw : 0; g1x = dx > 0 ? tw - bandDevPx : bandDevPx;
        g0y = th * 0.5; g1y = th * 0.5;
      } else {
        g0y = dy > 0 ? th : 0; g1y = dy > 0 ? th - bandDevPx : bandDevPx;
        g0x = tw * 0.5; g1x = tw * 0.5;
      }
      const grad = ctx.createLinearGradient(g0x, g0y, g1x, g1y);
      grad.addColorStop(0, `rgba(0,0,0,${GRAD_NEAR})`);
      grad.addColorStop(0.5, `rgba(0,0,0,${GRAD_MID})`);
      grad.addColorStop(1, `rgba(0,0,0,${GRAD_FAR})`);

      const anchorCssW = sw / dpr;
      const refWdpr = glyph
        ? Math.max(1, Math.min(horiz ? tw : th, Math.round(horiz ? sw : sh)))
        : Math.max(1, Math.round(REF_DRAW_CSS_W * Math.max(0.1, anchorCssW / 140) * dpr));

      let drawX        , drawY        , drawW        , drawH        ;
      let flipX = false, flipY = false;
      if (horiz) {
        const overlapTop = Math.max(aRect.top, tRect.top);
        const overlapBot = Math.min(aRect.bottom, tRect.bottom);
        flipX = true;
        drawX = dx > 0 ? tw - refWdpr : 0;
        drawY = Math.round((overlapTop - tRect.top + overscanCssPx) * dpr);
        drawW = refWdpr;
        drawH = Math.max(1, Math.round((overlapBot - overlapTop) * dpr));
      } else {
        const overlapLeft = Math.max(aRect.left, tRect.left);
        const overlapRight = Math.min(aRect.right, tRect.right);
        flipY = true;
        drawX = Math.round((overlapLeft - tRect.left + overscanCssPx) * dpr);
        drawY = dy > 0 ? th - refWdpr : 0;
        drawW = Math.max(1, Math.round((overlapRight - overlapLeft) * dpr));
        drawH = refWdpr;
      }
      const drawDst          = { x: drawX, y: drawY, w: drawW, h: drawH, flipX, flipY, sx: ssx, sy: ssy };

      const strokeBox          = { x: 0, y: 0, w: tw, h: th, r: Math.max(0, t.cornerRadius * dpr) };

      // Glyphs: one pass, never over 1 — stacking `lighter` passes clips the
      // metal's highlights to white and the colour is gone.
      const fillReflectionAlpha = glyph
        ? Math.min(1, reflectionAlpha * FILL_OPACITY_MUL)
        : Math.min(MAX_ALPHA_STACK, reflectionAlpha * FILL_EXTRA_ALPHA * FILL_OPACITY_MUL * FILL_CIRCLE_ATTENUATION);
      maskedFillPasses(fCtx, anchorCanvas, sw, sh, tw, th, fillReflectionAlpha, grad, drawDst, strokeBox, dpr, glyph ? Math.max(tw, th) : undefined);

      if (!glyph) {
        maskedStrokePasses(
          sCtx, anchorCanvas, sw, sh, tw, th,
          strokeBox, reflectionAlpha, strokeBandPx, grad, STROKE_EXTRA_ALPHA, drawDst
        );

        drawBorderHighlight(
          sCtx, strokeBox, borderHighlightPx,
          g0x, g0y, g1x, g1y,
          Math.min(0.85, BORDER_HILITE_ALPHA * reflectionAlpha)
        );
      }

      if (viaScratch) {
        ctx.globalCompositeOperation = 'lighter';
        ctx.drawImage(scratchFill                     , 0, 0);
        strokeCtx.globalCompositeOperation = 'lighter';
        strokeCtx.drawImage(scratchStroke                     , 0, 0);
      }
    }

    for (const horiz of layouts) {
      applyOccluderShadow(ctx, strokeCtx, aRect, tRect, horiz, tw, th, overscanCssPx, dpr);
    }

    ctx.globalCompositeOperation = 'source-over';
    strokeCtx.globalCompositeOperation = 'source-over';
  }
}


/* ── engine/reflection/reflectionScheduler.ts ─────────────────────────────── */
/**
 * Auxiliary RAF driver for *target-side* work (currently: dark-mode reflections).
 *
 * Reflections run at 15 fps — the CSS blur(4px) on the fill canvas hides
 * temporal stepping completely. The scheduler coalesces rapid calls and
 * skips frames that arrive faster than the target interval.
 */



let scheduled = false;
let lastReflectionMs = 0;

function scheduleReflectionPaint()       {
  if (scheduled) return;
  scheduled = true;
  if (typeof requestAnimationFrame === 'undefined') return;
  requestAnimationFrame((now) => {
    scheduled = false;
    if (now - lastReflectionMs < REFLECTION_INTERVAL_MS) return;
    lastReflectionMs = now;
    paintReflections();
  });
}


/* ── engine/rim.ts ─────────────────────────────── */
/**
 * Inner-shadow rim on the metal ring — the same treatment the "Pro" text
 * carries (Figma: white 90 %, offset 0/1, blur 0.5): a hairline of light
 * along the top inside edge of the band.
 *
 * Computed, not CSS: band alpha minus the same alpha shifted down by the
 * offset leaves exactly the top rim (outer edge on the ring's upper half,
 * inner edge on its lower half — what a light from above does to a torus).
 * Blurred, tinted, drawn to a small overlay canvas above the metal. Redrawn
 * only when the outline changes (deform), so it's free at rest.
 */
                                                



                             
                                                             
                  
                      
               
                                        
                
                               
                
 

const RIM_DEFAULTS                       = Object.freeze({ offsetY: 1, blur: 0.5, alpha: 0.9, color: '#ffffff' });

                             
                            
                                
                             
                                 
                                                                                             
                              
                   
                                 
                                                                              
              
 

function injectRim(
  container             ,
  dims                                                                                                ,
  opts            
)                    {
  const dpr = Math.min(3, typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1);
  const margin = Math.ceil(3 * opts.blur + Math.abs(opts.offsetY) + 1);
  const cw = dims.width + 2 * margin, ch = dims.height + 2 * margin;
  const canvas = document.createElement('canvas');
  canvas.className = 'metal-fx-rim-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  canvas.width = Math.ceil(cw * dpr); canvas.height = Math.ceil(ch * dpr);
  canvas.style.cssText = `position:absolute;left:${-margin}px;top:${-margin}px;width:${cw}px;height:${ch}px;pointer-events:none`;
  const ctx = canvas.getContext('2d');
  const scratch = document.createElement('canvas');
  scratch.width = canvas.width; scratch.height = canvas.height;
  const sctx = scratch.getContext('2d', { willReadFrequently: true });
  if (!ctx || !sctx) return null;
  container.appendChild(canvas);
  const h             = {
    canvas, ctx, scratch, sctx,
    width: dims.width, height: dims.height, cornerRadius: dims.cornerRadius, kind: dims.kind, ring: dims.ring,
    margin, dpr, opts, mO: createOutlineBuf(), mI: createOutlineBuf(), sum: Number.NaN,
  };
  updateRim(h, null, true);
  return h;
}

function trace(g                          , buf            , off        )       {
  const xy = buf.xy;
  for (let i = 0; i < buf.n; i++) {
    const x = xy[i * 2] + off, y = xy[i * 2 + 1] + off;
    if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
  }
  g.closePath();
}

/** Redraw if the (deformed) outline changed. Cheap when it hasn't. */
function updateRim(h            , deform                 , force = false)       {
  const { width: W, height: H, cornerRadius: R, ring, margin: m, dpr } = h;
  roundRectOutline(0, 0, W, H, R, deform, h.mO);
  roundRectOutline(ring, ring, W - 2 * ring, H - 2 * ring, Math.max(0, R - ring), deform, h.mI);
  let sum = 0;
  const xy = h.mO.xy;
  for (let i = 0; i < h.mO.n; i += 4) sum += xy[i * 2] * 1.37 + xy[i * 2 + 1];
  if (!force && sum === h.sum) return;
  h.sum = sum;

  const { sctx: g, scratch: sc, ctx, canvas: cv, opts } = h;
  const w = sc.width, hh = sc.height;
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.clearRect(0, 0, w, hh);
  g.scale(dpr, dpr);
  g.fillStyle = '#fff';
  g.beginPath();
  trace(g, h.mO, m);
  trace(g, h.mI, m);
  g.fill('evenodd');

  const d = g.getImageData(0, 0, w, hh).data;
  const n = w * hh;
  const a = new Float32Array(n);
  for (let i = 0, j = 3; i < n; i++, j += 4) a[i] = d[j] / 255;
  const shift = Math.round(opts.offsetY * dpr) * w;
  const rim = new Float32Array(n);
  if (shift >= 0) {
    for (let i = 0; i < n; i++) rim[i] = a[i] * (1 - (i >= shift ? a[i - shift] : 0));
  } else {
    for (let i = 0; i < n; i++) rim[i] = a[i] * (1 - (i - shift < n ? a[i - shift] : 0));
  }
  const blurred = gaussBlur(rim, w, hh, opts.blur * dpr);

  const cr = parseInt(opts.color.slice(1, 3), 16), cg = parseInt(opts.color.slice(3, 5), 16), cb = parseInt(opts.color.slice(5, 7), 16);
  const img = ctx.createImageData(w, hh);
  const o = img.data;
  for (let i = 0, j = 0; i < n; i++, j += 4) {
    o[j] = cr; o[j + 1] = cg; o[j + 2] = cb;
    o[j + 3] = Math.round(Math.min(1, blurred[i] * opts.alpha) * 255);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.putImageData(img, 0, 0);
  void cv;
}

function removeRim(h                   )       {
  if (h) h.canvas.remove();
}


/* ── engine/textMask.ts ─────────────────────────────── */
/**
 * Paint an element's text run onto a canvas with the DOM's own font and
 * metrics, so canvas glyphs land on the DOM glyphs to within a device px.
 * Shared by the Pro badge's metal fill and the text reflection mask.
 *
 * `ctx` is expected in device px with origin at `root`'s top-left; the
 * function scales by `dpr` internally.
 */
function paintTextRun(
  ctx                          ,
  root             ,
  textEl             ,
  dpr        
)       {
  const cs = getComputedStyle(textEl);
  const rr = root.getBoundingClientRect();
  const range = document.createRange();
  range.selectNodeContents(textEl);
  const tr = range.getBoundingClientRect();
  range.detach();
  ctx.save();
  ctx.scale(dpr, dpr);
  ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  const text = textEl.textContent ?? '';
  const m = ctx.measureText(text);
  const asc = m.fontBoundingBoxAscent ?? parseFloat(cs.fontSize) * 0.9;
  const desc = m.fontBoundingBoxDescent ?? parseFloat(cs.fontSize) * 0.2;
  const x = tr.left - rr.left;
  const baseline = tr.top - rr.top + (tr.height - (asc + desc)) / 2 + asc;
  ctx.fillText(text, x, baseline);
  ctx.restore();
}

/** Render `textEl`'s glyphs white-on-transparent over `root`'s box → data URL. */
function textMaskDataUrl(root             , textEl             )                {
  const dpr = window.devicePixelRatio || 1;
  const rr = root.getBoundingClientRect();
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(rr.width * dpr));
  c.height = Math.max(1, Math.round(rr.height * dpr));
  const g = c.getContext('2d');
  if (!g) return null;
  g.fillStyle = '#fff';
  paintTextRun(g, root, textEl, dpr);
  return c.toDataURL('image/png');
}


/* ── styles.ts ─────────────────────────────── */
const STYLE_ID = 'metal-fx-styles';

const CSS = /* css */ `
.metal-fx-root {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  isolation: isolate;
  overflow: visible;
  background: #272727;
  color: #f8f8f8;
}
.metal-fx-root[data-theme='light'] {
  background: #ffffff;
  color: #1d1d1d;
}

.metal-fx-root::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  pointer-events: none;
  z-index: 2;
  box-shadow: inset 0 0 50px 0 rgba(255, 255, 255, 0.02);
}
.metal-fx-root[data-theme='light']::before {
  box-shadow: inset 0 0 50px 0 rgba(0, 0, 0, 0.02);
}

.metal-fx-root::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  pointer-events: none;
  z-index: 4;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.1);
}
.metal-fx-root[data-theme='light']::after {
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.06);
}
/* Circle variant gets a thicker outer rim than the button variant. */
.metal-fx-root[data-variant='circle']::after {
  box-shadow: inset 0 0 0 2px rgba(255, 255, 255, 0.1);
}
.metal-fx-root[data-theme='light'][data-variant='circle']::after {
  box-shadow: inset 0 0 0 2px rgba(0, 0, 0, 0.06);
}

.metal-fx-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  z-index: 0;
  pointer-events: none;
  border-radius: inherit;
}

/* The inner spacer — defines the inset geometry where the metal ring meets
   the interior (3 px for Button, 1-2 px for Circle) and carries the Circle dark
   hairline ('box-shadow: inset' rules below). Intentionally transparent so
   the wrapper's background propagates through to the punched shader centre,
   giving consumers a single surface tone to override. See "Single-surface
   background" in the file header for the rationale. */
.metal-fx-inner {
  position: absolute;
  inset: 3px;
  border-radius: inherit;
  z-index: 1;
  pointer-events: none;
}

.metal-fx-root[data-variant='button'][data-shape='pill'] .metal-fx-inner {
  border-radius: calc(var(--mfx-radius, 20px) - 3px);
}
.metal-fx-root[data-variant='button'][data-shape='circle'] .metal-fx-inner {
  border-radius: calc(var(--mfx-radius, 16px) - 3px);
}
.metal-fx-root[data-variant='circle'][data-shape='pill'] .metal-fx-inner {
  inset: 0;
  border-radius: var(--mfx-radius, 20px);
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.45);
}
.metal-fx-root[data-variant='circle'][data-shape='circle'] .metal-fx-inner {
  inset: 0;
  border-radius: var(--mfx-radius, 16px);
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.45);
}
/* Circle-variant hairline alpha — light mode.
   Source-of-truth: index.html L2261-2267. The 0.45-alpha black inset that
   reads as a single-pixel frame against the dark interior is too heavy
   on a #ffffff inner: it ends up looking like a hard 2-px black ring
   against the iridescent shader. Suppressed entirely (alpha 0) — the
   shader's own iridescent rim already defines the silhouette in light
   mode, so an extra dark hairline only competes with it. The rule is
   kept (rather than deleted) as a tunable hook in case a future variant
   wants to re-introduce a soft edge. NOTE: we keep the dark-mode inset
   and border-radius values because — unlike index.html — our renderer
   does NOT overscan the canvas in light mode, so there is no 1-px gap
   between inner element and shader to compensate for. */
.metal-fx-root[data-theme='light'][data-variant='circle'][data-shape='pill'] .metal-fx-inner,
.metal-fx-root[data-theme='light'][data-variant='circle'][data-shape='circle'] .metal-fx-inner {
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0);
}

/* ─── Combined glow SVG (z=3) ──────────────────────────────────────────────
   Single SVG per instance that holds BOTH the wide-halo group
   (#mfx_haloTravel) and the catch-light group (#mfx_extraTravel), exactly
   mirroring canonical's _buildGlowSvgInner (index.html L8078). One
   mix-blend-mode: screen lifts the combined composite onto the shader
   ring; per-frame opacity attributes on each inner group still drive the
   independent fade-in / fade-out cycles for the halo and the catch-light.

   Why a single SVG: the circle variant anchors halo + catch-light at the same
   perimeter point, so they overlap in the bright zone. Two separately-
   screened SVGs would double-screen the overlap (A + B + C - AB - AC -
   BC + ABC instead of A + B + C - AB - AC once both groups composite
   in source-over inside one SVG and then screen against the host once).
   That overlap looked muted versus canonical specifically on the circle
   variant where both layers travel together.

   Source-of-truth opacity: #btnGlowSvg drops to 0.7 in dark and 0.2746 in
   light (index.html L632/L643). */
.metal-fx-glow-svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
  z-index: 3;
  pointer-events: none;
  opacity: 0.7;
}
.metal-fx-root[data-theme='light'] .metal-fx-glow-svg {
  /* Light-mode 1-px overscan mirrors .btn-glow-svg in metal.html so the
     halo stays glued to the visible silhouette (the shader ring there sits
     1 px outside the host's padding box). */
  inset: -1px;
  width: calc(100% + 2px);
  height: calc(100% + 2px);
  mix-blend-mode: multiply;
  /* Source-of-truth: html[data-theme="light"] #btnGlowSvg { opacity: 0.2746 }
     → −35 % from 0.4225 from the original 0.7 dark-mode opacity. */
  opacity: 0.2746;
  filter: saturate(5.355) brightness(0.78);
}
/* Circle light-mode small variants (e.g. 36×36 send button): the geometrically
   shrunk halo loses density when multiplied against #ffffff. Mirror the
   canonical override at index.html L2316 — bump saturation + drop brightness
   so the small glow holds together visually. */
.metal-fx-root[data-variant='circle'][data-shape='circle'][data-theme='light'] .metal-fx-glow-svg {
  filter: saturate(7.5) brightness(0.6);
}

/* The wrapped child — hoisted into z=5 so it sits above every overlay, with
   normalized chrome so consumer button styles don't fight the metal frame. */
.metal-fx-content {
  position: relative;
  z-index: 5;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  pointer-events: none;
}
.metal-fx-content > * {
  pointer-events: auto;
}
.metal-fx-root[data-normalize='true'] .metal-fx-content > * {
  background: transparent !important;
  border: 0 !important;
  outline: 0 !important;
  box-shadow: none !important;
  /* Sizing: we deliberately DO NOT force \`width: 100%; height: 100%\` on the
     child here. That used to be the contract ("the wrapper is the visible
     button surface; the child stretches to fill it"), but it created a cyclic
     percentage dependency: the wrapper is \`inline-flex\` with no intrinsic
     size, .metal-fx-content is \`width/height: 100%\` of the wrapper, and the
     child was \`100%\` of .metal-fx-content. With nothing breaking the cycle,
     icon-only / class-sized children collapsed.

     The new contract: the child sizes itself (intrinsic content, CSS class,
     or inline style — all work), and the wrapper's \`inline-flex\` wraps it
     tightly. Consumers who want a metal frame BIGGER than the child (e.g.
     padding around an icon) size <MetalFx style={{ width, height }}> AND
     explicitly set width/height on the child to fill (or accept that the
     child renders at its intrinsic size, centered).

     Typography is intentionally NOT touched. We used to apply
     \`color: inherit; font: inherit;\` here to "match" the wrapper, but
     \`font: inherit\` is a shorthand that overrides font-family, font-size,
     font-weight, AND line-height on the child — which (a) shrank the
     button height (line-height changes propagate through the flex
     content box) and (b) scaled em-based icons / font-icons inside the
     child to whatever the wrapper inherited. The wrapper now stays out
     of the child's typography entirely; consumers who want typographic
     normalization can apply it themselves on the child element. */
}

[data-metal-fx-reflection] {
  position: absolute;
  inset: 0;
  pointer-events: none;
  border-radius: inherit;
  overflow: hidden;
  z-index: 0;
  isolation: isolate;
}
.metal-fx-reflection-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  filter: blur(4px) saturate(1.2) brightness(1.58);
}
.metal-fx-reflection-stroke-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  filter: saturate(1.35) brightness(1.75);
}
/* Hosts that participate as reflection targets need positioning + isolation
   so the wrap composites only against the host (not the parent stack). The
   wrap injects these inline as well, but stating them here keeps reflections
   working on hosts that already have other inline styles applied. */
[data-metal-fx-reflect-host] {
  isolation: isolate;
}
`;

let injected = false;

function ensureStylesInjected()       {
  if (injected) return;
  if (typeof document === 'undefined') return;
  if (document.getElementById(STYLE_ID)) { injected = true; return; }
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
  injected = true;
}

/* ── the vanilla mount ──────────────────────────────────────────────
   metal-fx ships a React component; Aethron has no React. Their own
   index.ts calls the engine primitives a "power-user surface ... for
   consumers building non-React integrations", so this is the sanctioned
   path, not a workaround. Everything below is MetalFx.tsx's lifecycle
   with the hooks removed — measure, create, glow, rim, observe, destroy.
   Nothing about the effect itself is reimplemented. */
const MFX_GLOW = new Map();     // instance -> {handles, themeRef}
setGlowCallback(function (inst, nowMs) {
  const e = MFX_GLOW.get(inst);
  if (e) updateGlow(e.handles, inst, nowMs, inst.opacityMul, e.themeRef.current);
});

/* Wrap an element that is already in the document. Returns a handle with
   .root (the new wrapper), .pause(bool) and .destroy(). */
function metalWrap(el, o) {
  o = o || {};
  if (!el) return null;
  if (el.__mfx) return el.__mfx;
  if (!isMetalFxSupported()) return null;   // no WebGL2: leave it alone
  ensureStylesInjected();

  const theme = o.theme || 'dark';
  const kind = o.variant === 'circle' ? 'circle' : 'pill';
  const scale = o.scale || 1;
  const mask = o.mask || null;

  const root = document.createElement('div');
  root.className = 'metal-fx-root' + (o.className ? ' ' + o.className : '');
  root.dataset.variant = o.variant || 'button';
  root.dataset.shape = kind;
  root.dataset.theme = theme;
  root.dataset.normalize = o.normalize === false ? 'false' : 'true';
  root.style.setProperty('--mfx-strength',
    String(o.strength == null ? 1 : o.strength));
  root.style.opacity = '0'; root.style.visibility = 'hidden';

  const canvas = document.createElement('canvas');
  canvas.className = 'metal-fx-canvas';
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%';
  const inner = document.createElement('div');
  inner.className = 'metal-fx-inner';
  inner.setAttribute('aria-hidden', 'true');
  inner.style.cssText = 'position:absolute;inset:3px';
  const glowHost = document.createElement('div');
  glowHost.setAttribute('aria-hidden', 'true');
  glowHost.style.cssText =
    'position:absolute;inset:0;pointer-events:none;z-index:3;border-radius:inherit';
  if (o.glow === false) glowHost.style.display = 'none';
  const rimHost = document.createElement('div');
  rimHost.setAttribute('aria-hidden', 'true');
  rimHost.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:4';
  const content = document.createElement('div');
  content.className = 'metal-fx-content';

  el.parentNode.insertBefore(root, el);
  content.appendChild(el);
  root.append(canvas, inner, glowHost, rimHost, content);

  const themeRef = { current: theme };
  setSharedPreset(o.preset || 'chromatic', theme);

  const radiusOf = function (w, h) {
    if (kind === 'circle') return Math.min(w, h) / 2;
    let raw = o.borderRadius;
    if (raw == null) {
      const p = parseFloat(getComputedStyle(el).borderTopLeftRadius);
      raw = Number.isFinite(p) && p > 0 ? p : 20;
    }
    return Math.min(raw, Math.min(w, h) / 2);
  };
  const measure = function () {
    const r = root.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    return { cssWidth: w, cssHeight: h, cornerRadius: radiusOf(w, h) };
  };

  const d0 = measure();
  const inst = createInstance({
    hostCanvas: canvas, cssWidth: d0.cssWidth, cssHeight: d0.cssHeight,
    cornerRadius: d0.cornerRadius, kind, paused: !!o.paused,
    shaderScale: o.shaderScale, ringCssPx: o.ringCssPx, scale,
    opacityMul: o.strength == null ? 1 : o.strength,
    glowGain: o.glowGain == null ? 1 : o.glowGain,
    mask: mask,
    onFirstCopy: reveal,
  });
  function reveal() {
    root.style.opacity = '1'; root.style.visibility = 'visible';
    root.style.transition = 'opacity .15s ease-out';
  }
  /* THE TRAP THIS PROJECT HAS ALREADY PAID FOR TWICE: a reveal that hangs
     off a frame callback never fires under starvation, and here that
     would mean the button simply is not there. A timer backs it. */
  setTimeout(reveal, 1200);
  root.style.setProperty('--mfx-radius', d0.cornerRadius + 'px');
  root.style.borderRadius = d0.cornerRadius + 'px';

  /* A masked instance (metal-filled glyphs, a filled badge) has no ring
     band, so the halo is given points inside the mask and the mask
     itself to clip against — their glowMaskData, unchanged. */
  const glowMaskData = function (w, h) {
    if (!mask || o.glowMode === 'ring') return {};
    const dpr = window.devicePixelRatio || 1;
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(w * dpr));
    c.height = Math.max(1, Math.round(h * dpr));
    const g = c.getContext('2d');
    if (!g) return {};
    g.fillStyle = '#fff';
    mask(g, c.width, c.height, dpr);
    const d = g.getImageData(0, 0, c.width, c.height).data;
    const pts = [], step = Math.max(1, Math.round(2 * dpr));
    for (let y = step >> 1; y < c.height; y += step)
      for (let x = step >> 1; x < c.width; x += step)
        if (d[(y * c.width + x) * 4 + 3] > 128) pts.push({ x: x / dpr, y: y / dpr });
    return { samplePoints: pts, maskDataUrl: c.toDataURL('image/png') };
  };

  let handles = null;
  const buildGlow = function (d) {
    if (o.glow === false) return;
    const prev = handles;
    glowHost.innerHTML = '';
    handles = injectGlow(glowHost, Object.assign({
      width: d.cssWidth, height: d.cssHeight,
      cornerRadius: d.cornerRadius, kind, scale,
    }, glowMaskData(d.cssWidth, d.cssHeight)));
    // a rebuilt glow starts invisible; carry the old state so a resize
    // does not read as the halo blinking out
    if (prev) carryGlowState(prev, handles);
    MFX_GLOW.set(inst, { handles, themeRef });
  };
  buildGlow(d0);
  if (o.glow !== false) registerGlowInstance(inst);

  let rim = null;
  const buildRim = function (d) {
    removeRim(rim); rim = null;
    if (!o.innerShadow) return;
    const ro = o.innerShadow === true ? RIM_DEFAULTS
      : Object.assign({}, RIM_DEFAULTS, o.innerShadow);
    rim = injectRim(rimHost, {
      width: d.cssWidth, height: d.cssHeight, cornerRadius: d.cornerRadius,
      kind, ring: inst.ringCssPx,
    }, ro);
  };
  buildRim(d0);

  let raf = 0, bw = d0.cssWidth, bh = d0.cssHeight, br = d0.cornerRadius;
  const ro = new ResizeObserver(function () {
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      const n = measure();
      if (Math.abs(n.cssWidth - bw) < .5 && Math.abs(n.cssHeight - bh) < .5 &&
          Math.abs(n.cornerRadius - br) < .5) return;
      bw = n.cssWidth; bh = n.cssHeight; br = n.cornerRadius;
      updateInstance(inst, n);
      root.style.setProperty('--mfx-radius', n.cornerRadius + 'px');
      root.style.borderRadius = n.cornerRadius + 'px';
      buildGlow(n); buildRim(n);
    });
  });
  ro.observe(root);

  const unsubGlow = subscribeGlowConfig(function (markupChanged) {
    if (markupChanged) buildGlow(measure());
  });

  let io = null;
  if (typeof IntersectionObserver !== 'undefined') {
    io = new IntersectionObserver(function (es) {
      for (const e of es) setInstanceVisible(inst, e.isIntersecting);
    }, { rootMargin: '64px' });
    io.observe(root);
  }
  attachCursorLight();

  /* Neighbours catch the light. This is the part a <div> cannot fake: the
     engine reads the shader's own pixels and paints a soft copy of them
     onto whatever stands near the button. Dark mode only, by design. */
  let refl = [];
  if (o.reflect && o.reflect.length && theme === 'dark') {
    inst.onAfterFrame = scheduleReflectionPaint;
    refl = o.reflect.filter(Boolean);
    for (const t of refl) addReflectionTarget(t.el || t, inst, root, t.strength == null ? 1 : t.strength);
  }

  const h = {
    root: root, inst: inst, el: el,
    pause: function (p) { updateInstance(inst, { paused: !!p }); },
    destroy: function () {
      detachCursorLight(); removeRim(rim); rim = null;
      ro.disconnect(); if (io) io.disconnect(); unsubGlow();
      if (raf) cancelAnimationFrame(raf);
      for (const t of refl) removeReflectionTarget(t.el || t);
      MFX_GLOW.delete(inst); unregisterGlowInstance(inst); destroyInstance(inst);
      delete el.__mfx;
      if (root.parentNode) { root.parentNode.insertBefore(el, root); root.remove(); }
    },
  };
  el.__mfx = h;
  return h;
}

window.metalWrap = metalWrap;
window.metalReady = true;
})();
"""

CALLBACK_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Signing in…</title>
<style>body{background:#161513;color:#96938a;font:15px system-ui,sans-serif;
height:100vh;display:flex;align-items:center;justify-content:center}</style>
</head><body><div id="m">Signing you in…</div>
<script>
(async()=>{
  // Supabase returns the session in the URL FRAGMENT (never sent to a
  // server); read it here and hand it to our backend for a session.
  const h=new URLSearchParams(location.hash.slice(1));
  const at=h.get('access_token');
  const err=h.get('error_description')||h.get('error');
  if(err){document.getElementById('m').textContent='Sign-in failed: '+err;return;}
  if(!at){document.getElementById('m').textContent='Sign-in failed (no token).';return;}
  try{
    const r=await fetch('/api/auth/session',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({access_token:at})});
    if(!r.ok)throw new Error((await r.json()).error||'failed');
    location.href='/';
  }catch(e){document.getElementById('m').textContent='Sign-in failed: '+e.message;}
})();
</script></body></html>
"""

# Shown in the SYSTEM browser after the direct-Google exchange completes;
# the app window is polling and logs itself in, so this tab is done.
CALLBACK_DONE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signed in — Aethron</title>
<style>body{background:#161513;color:#f2f0ea;font:15px/1.6 system-ui,sans-serif;
height:100vh;display:flex;align-items:center;justify-content:center;text-align:center}
.b{max-width:340px}.d{width:38px;height:38px;object-fit:contain;
display:block;margin:0 auto 14px}
.m{color:#96938a;font-size:13.5px;margin-top:6px}</style>
</head><body><div class="b"><img class="d" src="__MARK__" alt="">
<b>You're signed in.</b><div class="m">Return to the Aethron app — you can close this tab.</div>
</div></body></html>
"""

LOGIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aethron — sign in</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root{--ink:#1a1714;--paper:#f6f2ec;--card:#fffdfb;--line:#e7ded2;
--dim:#8a8075;--acc:#d97757}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{font:15px/1.5 Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased;
color:var(--ink)}
.wrap{display:flex;min-height:100vh}
/* left brand hero */
.hero{flex:1.05;position:relative;overflow:hidden;display:flex;flex-direction:column;
justify-content:space-between;padding:36px 44px;color:#efe8df;
background:radial-gradient(130% 100% at 15% 5%,#2b221b,#161513 60%,#100e0b)}
.hero::before{content:"";position:absolute;inset:0;pointer-events:none;
background:radial-gradient(42% 42% at 88% 92%,rgba(217,119,87,.30),transparent 70%)}
.hero::after{content:"";position:absolute;inset:0;opacity:.6;pointer-events:none;
background-image:linear-gradient(rgba(255,255,255,.045) 1px,transparent 1px),
linear-gradient(90deg,rgba(255,255,255,.045) 1px,transparent 1px);
background-size:46px 46px;
-webkit-mask-image:radial-gradient(72% 70% at 28% 18%,#000,transparent);
mask-image:radial-gradient(72% 70% at 28% 18%,#000,transparent)}
.htop{display:flex;align-items:center;justify-content:space-between;
position:relative;z-index:1}
.mark{display:flex;align-items:center;gap:10px;font-weight:800;
letter-spacing:-.02em;font-size:17px}
.mark .m{width:26px;height:26px;object-fit:contain;display:block}
.back{color:rgba(244,238,231,.7);font-size:13px;text-decoration:none}
.back:hover{color:#fff}
.hbtm{position:relative;z-index:1;max-width:430px}
.hbtm h1{font-size:40px;line-height:1.06;letter-spacing:-.03em;font-weight:800}
.hbtm p{margin-top:16px;color:rgba(239,232,223,.68);font-size:15px;line-height:1.65}
.dots{display:flex;gap:7px;margin-top:28px}
.dots i{width:22px;height:4px;border-radius:3px;background:rgba(255,255,255,.16)}
.dots i.on{background:var(--acc);width:30px}
/* right form pane */
.pane{flex:.95;background:var(--paper);display:flex;align-items:center;
justify-content:center;padding:44px 28px}
.form{width:100%;max-width:362px}
.form h2{font-size:28px;letter-spacing:-.02em;font-weight:800}
.form .sub{color:var(--dim);font-size:14px;margin:7px 0 24px}
label{display:block;font-size:12.5px;font-weight:600;color:#5f574d;margin:16px 0 6px}
.field{position:relative}
.field input{width:100%;background:var(--card);color:var(--ink);
border:1px solid var(--line);border-radius:11px;padding:12px 13px;font:inherit}
.field input::placeholder{color:#bcb2a4}
.field input:focus{outline:none;border-color:var(--acc);
box-shadow:0 0 0 3px rgba(217,119,87,.16)}
.eye{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;
border:0;width:34px;height:34px;padding:0;cursor:pointer;color:#a99f92;
display:grid;place-items:center}
.eye:hover{color:var(--ink)}.eye svg{width:18px;height:18px}
.row{display:flex;align-items:center;justify-content:space-between;
margin-top:14px;font-size:13px}
.rem{display:flex;align-items:center;gap:8px;color:#5f574d;cursor:pointer;
user-select:none}
.rem input{width:15px;height:15px;accent-color:var(--acc)}
.forgot{color:var(--acc);text-decoration:none;cursor:pointer;font-weight:500}
.forgot:hover{text-decoration:underline}
.primary{width:100%;margin-top:22px;background:var(--ink);color:var(--paper);
border:0;border-radius:12px;padding:13px;font:inherit;font-weight:650;
cursor:pointer;transition:transform .05s,filter .15s}
.primary:hover{filter:brightness(1.18)}.primary:active{transform:translateY(1px)}
.primary:disabled{opacity:.55;cursor:default}
.divider{display:flex;align-items:center;gap:12px;color:var(--dim);
font-size:12px;margin:20px 0}
.divider::before,.divider::after{content:"";flex:1;height:1px;background:var(--line)}
.google{width:100%;background:var(--card);color:#1f1f1f;border:1px solid var(--line);
border-radius:12px;padding:12px;font:inherit;font-weight:600;cursor:pointer;
display:flex;align-items:center;justify-content:center;gap:10px;transition:background .15s}
.google:hover{background:#f1ebe1}.google:disabled{opacity:.6;cursor:default}
.google svg{width:18px;height:18px}
.toggle{margin-top:22px;text-align:center;font-size:13.5px;color:var(--dim)}
.toggle a{color:var(--ink);font-weight:700;cursor:pointer;text-decoration:none}
.toggle a:hover{color:var(--acc)}
.err{color:#c0483f;font-size:13px;margin-top:14px;min-height:18px;text-align:center}
/* ── hero carousel (the dots actually drive these now) ───────────── */
.slides{position:relative;min-height:196px}
.slide{position:absolute;left:0;right:0;bottom:0;opacity:0;transform:translateY(16px);
transition:opacity .7s cubic-bezier(.22,1,.36,1),transform .7s cubic-bezier(.22,1,.36,1)}
.slide.on{opacity:1;transform:none}
.dots{display:flex;gap:7px;margin-top:28px}
.dots button{width:22px;height:4px;padding:0;border:0;border-radius:3px;cursor:pointer;
background:rgba(255,255,255,.16);
transition:width .45s cubic-bezier(.22,1,.36,1),background .45s}
.dots button:hover{background:rgba(255,255,255,.32)}
.dots button.on{background:var(--acc);width:30px}
.dots button:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
/* ── hero motion stage: one animated visual per slide ───────────── */
.stage{position:relative;flex:1;display:grid;place-items:center;z-index:1;
min-height:0;padding:18px 0}
.viz{position:absolute;width:min(82%,352px);opacity:0;
transform:translateY(12px) scale(.975);
transition:opacity .75s cubic-bezier(.22,1,.36,1),
transform .75s cubic-bezier(.22,1,.36,1)}
.viz.on{opacity:1;transform:none}
.viz svg{width:100%;height:auto;display:block}
.gl{fill:rgba(255,255,255,.05);stroke:rgba(255,255,255,.10)}
.ln{fill:rgba(255,255,255,.16)}
.ac{fill:#d97757}
.sc{stroke:#d97757;fill:none}
.fx{transform-box:fill-box}
/* v1 — rebranding a template in place */
.v1 .logo{animation:tint 4.6s ease-in-out infinite}
.v1 .t1{transform-origin:left center;
animation:tint 4.6s ease-in-out .12s infinite,grow 4.6s ease-in-out .12s infinite}
.v1 .t2{transform-origin:left center;animation:shrink 4.6s ease-in-out .24s infinite}
.v1 .sweep{animation:sweep 4.6s ease-in-out infinite}
@keyframes tint{0%,32%{fill:rgba(255,255,255,.2)}52%,100%{fill:#d97757}}
@keyframes grow{0%,32%{transform:scaleX(.74)}52%,100%{transform:scaleX(1)}}
@keyframes shrink{0%,32%{transform:scaleX(1)}52%,100%{transform:scaleX(.72)}}
@keyframes sweep{0%{transform:translateX(-60px);opacity:0}
22%{opacity:.55}58%{transform:translateX(320px);opacity:0}100%{opacity:0}}
/* v2 — click-to-edit: cursor flies in, element locks, panel opens */
.v2 .cur{animation:cur 5.2s cubic-bezier(.45,0,.25,1) infinite}
.v2 .ring{opacity:0;animation:ring 5.2s ease infinite}
.v2 .click{opacity:0;transform-origin:center;animation:clk 5.2s ease infinite}
.v2 .panel{opacity:0;transform-origin:left center;
animation:panel 5.2s cubic-bezier(.22,1,.36,1) infinite}
.v2 .edit{transform-origin:left center;animation:edit 5.2s ease infinite}
@keyframes cur{0%{transform:translate(232px,148px)}
26%,60%{transform:translate(104px,86px)}
30%{transform:translate(101px,83px)}100%{transform:translate(232px,148px)}}
@keyframes ring{0%,25%{opacity:0}33%,88%{opacity:1}100%{opacity:0}}
@keyframes clk{0%,26%{opacity:0;transform:scale(.35)}
31%{opacity:.9;transform:scale(1)}42%,100%{opacity:0;transform:scale(1.7)}}
@keyframes panel{0%,32%{opacity:0;transform:translateY(7px) scale(.96)}
42%,86%{opacity:1;transform:none}100%{opacity:0}}
@keyframes edit{0%,44%{transform:scaleX(1)}56%,100%{transform:scaleX(.6)}}
/* v3 — any model feeds the guarded pipeline */
.v3 .flow{stroke-dasharray:3 7;animation:flow 1.5s linear infinite}
.v3 .f2{animation-delay:-.5s}.v3 .f3{animation-delay:-1s}
.v3 .halo{transform-origin:center;animation:halo 2.8s ease-in-out infinite}
.v3 .n1{animation:bob 3.4s ease-in-out infinite}
.v3 .n2{animation:bob 3.4s ease-in-out -1.1s infinite}
.v3 .n3{animation:bob 3.4s ease-in-out -2.2s infinite}
.v3 .cap{animation:cap 4.2s ease-in-out infinite}
@keyframes flow{to{stroke-dashoffset:-20}}
@keyframes halo{0%,100%{opacity:.16;transform:scale(1)}
50%{opacity:.4;transform:scale(1.07)}}
@keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
@keyframes cap{0%,100%{opacity:.55}50%{opacity:1}}
/* ── intro splash ───────────────────────────────────────────────── */
.splash{position:fixed;inset:0;z-index:99;display:grid;place-items:center;
background:#100e0b;overflow:hidden;
animation:splashOut .6s cubic-bezier(.4,0,.2,1) 1.9s forwards}
.splash.skip{animation:splashOut .28s ease forwards}
.aur{position:absolute;border-radius:50%;filter:blur(90px);opacity:.5}
.a1{width:520px;height:520px;background:#d97757;top:-16%;left:-10%;
animation:drift 9s ease-in-out infinite alternate}
.a2{width:440px;height:440px;background:#7d5236;bottom:-18%;right:-8%;
animation:drift 11s ease-in-out infinite alternate-reverse}
.vig{position:absolute;inset:0;
background:radial-gradient(62% 56% at 50% 46%,transparent,rgba(16,14,11,.94))}
.sp-in{position:relative;text-align:center;padding:0 24px}
.sp-mark{width:82px;height:82px;object-fit:contain;display:block;margin:0 auto 22px;
clip-path:inset(100% 0 0 0);
animation:markIn .9s cubic-bezier(.22,1,.36,1) .12s forwards}
.sp-word{font-size:16px;font-weight:700;letter-spacing:.42em;text-indent:.42em;
color:#f4efe8;opacity:0;animation:up .7s cubic-bezier(.22,1,.36,1) .52s forwards}
.sp-by{margin-top:12px;font-size:12.5px;color:rgba(244,239,232,.45);opacity:0;
animation:up .7s cubic-bezier(.22,1,.36,1) .8s forwards}
.sp-by a{color:var(--acc);text-decoration:none;font-weight:600}
.sp-by a:hover{text-decoration:underline}
.sp-bar{width:118px;height:2px;margin:26px auto 0;border-radius:2px;
background:rgba(255,255,255,.1);overflow:hidden;opacity:0;
animation:up .5s ease .58s forwards}
.sp-bar i{display:block;height:100%;width:0;border-radius:2px;background:var(--acc);
animation:fill 1.3s cubic-bezier(.4,0,.2,1) .62s forwards}
@keyframes markIn{to{clip-path:inset(0 0 0 0)}}
@keyframes dotIn{to{transform:scale(1)}}
@keyframes up{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}
@keyframes fill{to{width:100%}}
@keyframes drift{to{transform:translate3d(26px,-22px,0) scale(1.12)}}
@keyframes splashOut{to{opacity:0;visibility:hidden}}
@media (prefers-reduced-motion:reduce){
 .splash{animation-delay:.8s}
 .sp-mark{clip-path:none}
 .sp-mark,.sp-word,.sp-by,.sp-bar,.a1,.a2{animation:none;opacity:1}
 .sp-bar i{animation:none;width:100%}
 .slide{transition:none}
}
@media (max-width:860px){.hero{display:none}.pane{flex:1}}
</style></head><body>
<div class="splash" id="splash" aria-hidden="true">
  <div class="aur a1"></div><div class="aur a2"></div><div class="vig"></div>
  <div class="sp-in">
    <img class="sp-mark" src="__MARK__" alt="Aethron">
    <div class="sp-word">AETHRON</div>
    <div class="sp-bar"><i></i></div>
    <div class="sp-by">Powered by
     <a href="https://jomiez.com" target="_blank" rel="noopener"
        tabindex="-1">Jomiez</a></div>
  </div>
</div>
<div class="wrap">
  <div class="hero">
    <div class="htop">
      <div class="mark"><img class="m" src="__MARK__" alt=""> Aethron</div>
      <a class="back" href="https://aethron.jomiez.com">&larr; Back to site</a>
    </div>
    <div class="stage" id="stage" aria-hidden="true">
      <!-- 1 · rebrand: the template repaints itself in your colours -->
      <div class="viz v1 on">
       <svg viewBox="0 0 320 190">
        <defs><linearGradient id="sw" x1="0" x2="1">
         <stop offset="0" stop-color="#d97757" stop-opacity="0"/>
         <stop offset=".5" stop-color="#d97757" stop-opacity=".4"/>
         <stop offset="1" stop-color="#d97757" stop-opacity="0"/></linearGradient>
         <clipPath id="cp"><rect x="8" y="8" width="304" height="174" rx="14"/></clipPath>
        </defs>
        <rect class="gl" x="8" y="8" width="304" height="174" rx="14"/>
        <circle cx="30" cy="28" r="3.4" fill="rgba(255,255,255,.2)"/>
        <circle cx="42" cy="28" r="3.4" fill="rgba(255,255,255,.12)"/>
        <circle cx="54" cy="28" r="3.4" fill="rgba(255,255,255,.12)"/>
        <path d="M8 46 H312" stroke="rgba(255,255,255,.08)"/>
        <rect class="logo fx" x="28" y="62" width="18" height="18" rx="5"/>
        <rect class="ln" x="232" y="68" width="26" height="6" rx="3"/>
        <rect class="ln" x="266" y="68" width="26" height="6" rx="3"/>
        <rect class="t1 ln fx" x="28" y="98" width="140" height="11" rx="5.5"/>
        <rect class="t2 ln fx" x="28" y="118" width="150" height="7" rx="3.5"/>
        <rect class="ln" x="28" y="136" width="96" height="7" rx="3.5" opacity=".6"/>
        <rect class="gl" x="200" y="92" width="92" height="60" rx="9"/>
        <path d="M210 142 L232 118 L248 133 L262 122 L282 142 Z"
              fill="rgba(255,255,255,.14)"/>
        <circle cx="268" cy="108" r="6" fill="rgba(255,255,255,.18)"/>
        <g clip-path="url(#cp)">
         <rect class="sweep" x="-30" y="8" width="52" height="174" fill="url(#sw)"/></g>
       </svg>
      </div>
      <!-- 2 · point and edit: cursor locks an element, inspector opens -->
      <div class="viz v2">
       <svg viewBox="0 0 320 190">
        <rect class="gl" x="8" y="8" width="304" height="174" rx="14"/>
        <rect class="ln" x="30" y="34" width="112" height="9" rx="4.5" opacity=".7"/>
        <rect class="gl" x="24" y="60" width="176" height="52" rx="10"/>
        <rect class="edit ln fx" x="38" y="74" width="124" height="9" rx="4.5"/>
        <rect class="ln" x="38" y="90" width="84" height="6" rx="3" opacity=".6"/>
        <rect class="ring sc" x="24" y="60" width="176" height="52" rx="10"
              stroke-width="1.6"/>
        <circle class="click sc fx" cx="104" cy="86" r="11" stroke-width="1.6"/>
        <g class="panel fx">
         <rect x="212" y="58" width="86" height="66" rx="10"
               fill="rgba(255,255,255,.07)" stroke="rgba(255,255,255,.12)"/>
         <rect class="ac" x="223" y="72" width="34" height="6" rx="3"/>
         <rect class="ln" x="223" y="85" width="62" height="5" rx="2.5"/>
         <rect class="ln" x="223" y="95" width="48" height="5" rx="2.5" opacity=".6"/>
         <rect class="ac" x="223" y="107" width="28" height="9" rx="4.5"/>
        </g>
        <rect class="ln" x="24" y="130" width="176" height="7" rx="3.5" opacity=".45"/>
        <g class="cur"><path d="M0 0 L0 16 L4.4 12.2 L7.4 19 L10.6 17.4 L7.6 10.8 L13 10.6 Z"
           fill="#fff" stroke="rgba(0,0,0,.35)" stroke-width=".6"/></g>
       </svg>
      </div>
      <!-- 3 · any model feeds it; guardrails hold the physics -->
      <div class="viz v3">
       <svg viewBox="0 0 320 190">
        <g class="sc" stroke-width="1.5" opacity=".9">
         <path class="flow" d="M62 46 C112 62 122 78 146 92"/>
         <path class="flow f2" d="M52 140 C106 132 120 110 146 98"/>
         <path class="flow f3" d="M262 44 C214 62 198 78 178 92"/>
        </g>
        <g class="n1"><circle class="gl" cx="54" cy="42" r="15"/>
         <circle class="ac" cx="54" cy="42" r="4"/></g>
        <g class="n2"><circle class="gl" cx="44" cy="146" r="15"/>
         <circle class="ac" cx="44" cy="146" r="4"/></g>
        <g class="n3"><circle class="gl" cx="270" cy="40" r="15"/>
         <circle class="ac" cx="270" cy="40" r="4"/></g>
        <rect class="halo fx" x="134" y="56" width="88" height="80" rx="16"
              fill="#d97757" opacity=".2"/>
        <rect class="gl" x="142" y="62" width="72" height="68" rx="12"/>
        <rect class="ac" x="154" y="78" width="30" height="7" rx="3.5"/>
        <rect class="ln" x="154" y="92" width="48" height="6" rx="3"/>
        <rect class="ln" x="154" y="104" width="36" height="6" rx="3" opacity=".6"/>
        <g class="cap" stroke="#d97757" stroke-width="1.4" fill="none" opacity=".7">
         <path d="M120 96 A 38 42 0 0 1 132 66"/>
         <path d="M236 96 A 38 42 0 0 0 224 66"/></g>
       </svg>
      </div>
    </div>
    <div class="hbtm">
      <div class="slides">
        <div class="slide on">
          <h1>Own any template.</h1>
          <p>Rebrand the copy, images, and logo &mdash; the design and animations
           stay pixel-perfect. Ship it anywhere, truly yours.</p>
        </div>
        <div class="slide">
          <h1>Edit by pointing.</h1>
          <p>Click any headline, image, button, or section on the live page and
           rewrite, restyle, or remove it &mdash; baked into the shipped code.</p>
        </div>
        <div class="slide">
          <h1>Driven by any AI.</h1>
          <p>Your model fills the copy; hard guardrails enforce the physics. No
           badge, no telemetry, no platform lock-in.</p>
        </div>
      </div>
      <div class="dots" id="dots">
        <button class="on" data-i="0" aria-label="Slide 1"></button>
        <button data-i="1" aria-label="Slide 2"></button>
        <button data-i="2" aria-label="Slide 3"></button>
      </div>
    </div>
  </div>
  <div class="pane">
   <div class="form">
    <h2 id="head">Welcome back</h2>
    <div class="sub" id="sub">Sign in to your account</div>
    <label>Email</label>
    <div class="field"><input id="email" type="email" autocomplete="email"
      placeholder="you@company.com"></div>
    <label>Password</label>
    <div class="field">
      <input id="pw" type="password" autocomplete="current-password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;">
      <button type="button" class="eye" onclick="togglePw()" aria-label="Show password">
       <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>
      </button>
    </div>
    <div class="row">
      <label class="rem"><input type="checkbox" id="rem"> Remember me</label>
      <a class="forgot" onclick="forgot()">Forgot password?</a>
    </div>
    <button id="go" class="primary" onclick="submit()">Sign in</button>
    <div class="err" id="err"></div>
    <div class="divider">Or continue with</div>
    <button class="google" id="gbtn" onclick="googleSignIn()">
     <svg viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
     <span id="glabel">Continue with Google</span></button>
    <div class="toggle" id="tog">Don't have an account?
     <a onclick="setMode('signup')">Sign up here</a></div>
   </div>
  </div>
</div>
<script>
let MODE='login';
const $=id=>document.getElementById(id);
// intro splash — CSS drives the timing (so it always clears, even if this
// script fails); a click/keypress just skips ahead.
(function(){const s=$('splash');if(!s)return;
  const skip=()=>{s.classList.add('skip');
    document.removeEventListener('keydown',skip);s.removeEventListener('click',skip);};
  s.addEventListener('click',skip);document.addEventListener('keydown',skip);
  setTimeout(()=>s.remove(),3200);})();
// hero carousel — the dots drive real slides (auto-advance + click)
(function(){const sl=[...document.querySelectorAll('.slide')],
  dt=[...document.querySelectorAll('#dots button')];
  if(sl.length<2)return;
  const still=matchMedia('(prefers-reduced-motion: reduce)').matches;
  let i=0,t=null;
  const vz=[...document.querySelectorAll('.viz')];
  const go=n=>{i=(n+sl.length)%sl.length;
    sl.forEach((s,k)=>s.classList.toggle('on',k===i));
    vz.forEach((v,k)=>v.classList.toggle('on',k===i));
    dt.forEach((d,k)=>d.classList.toggle('on',k===i));};
  const play=()=>{if(!still)t=setInterval(()=>go(i+1),5200);};
  dt.forEach(d=>d.addEventListener('click',()=>{clearInterval(t);
    go(+d.dataset.i);play();}));
  document.querySelector('.hero').addEventListener('mouseenter',
    ()=>clearInterval(t));
  document.querySelector('.hero').addEventListener('mouseleave',play);
  play();})();
async function googleSignIn(){
  // DIRECT flow: open Google in the system browser, then poll for the
  // session (works from a native app window). Falls back to Supabase's
  // brokered redirect if direct isn't configured.
  const btn=$('gbtn'), lbl=$('glabel');
  $('err').textContent='';
  let start;
  try{ start=await fetch('/api/auth/google/start'); }
  catch(_){ location='/api/auth/google'; return; }
  if(start.status===400){ location='/api/auth/google'; return; }  // fallback
  const {state}=await start.json();
  btn.disabled=true; lbl.textContent='Continue in your browser…';
  const t0=Date.now();
  const timer=setInterval(async()=>{
    if(Date.now()-t0>150000){ clearInterval(timer); btn.disabled=false;
      lbl.textContent='Continue with Google'; return; }
    let r; try{ r=await fetch('/api/auth/google/poll?state='+state); }catch(_){ return; }
    if(r.status===404){ return; }
    const j=await r.json();
    if(j.status==='ok'){ clearInterval(timer); location.href='/'; }
    else if(j.status==='error'){ clearInterval(timer); btn.disabled=false;
      lbl.textContent='Continue with Google';
      $('err').style.color='#e5695e'; $('err').textContent='Google sign-in: '+(j.error||'failed'); }
  },1500);
}
function setMode(m){MODE=m;
  $('head').textContent=m==='signup'?'Create your account':'Welcome back';
  $('sub').textContent=m==='signup'?'Start owning your templates':'Sign in to your account';
  $('go').textContent=m==='signup'?'Create account':'Sign in';
  $('tog').innerHTML=m==='signup'
    ?'Already have an account? <a onclick="setMode(\'login\')">Sign in</a>'
    :'Don\'t have an account? <a onclick="setMode(\'signup\')">Sign up here</a>';
  $('err').style.color='#c0483f';$('err').textContent='';}
function togglePw(){const p=$('pw');p.type=p.type==='password'?'text':'password';}
function forgot(){$('err').style.color='#8a8075';
  $('err').textContent='Password reset is coming soon — for now, use Continue with Google.';}
async function submit(){
  $('err').textContent='';$('go').disabled=true;
  try{
    const r=await fetch('/api/auth/'+MODE,{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:$('email').value.trim(),password:$('pw').value})});
    const j=await r.json();
    if(!r.ok)throw new Error(j.error||'failed');
    if(j.confirm){$('err').style.color='#7ec699';
      $('err').textContent='Check your email to confirm, then sign in.';
      return setMode('login');}
    location.href='/';
  }catch(e){$('err').style.color='#e5695e';$('err').textContent=e.message;}
  finally{$('go').disabled=false;}
}
$('pw').addEventListener('keydown',e=>{if(e.key==='Enter')submit();});
if(new URLSearchParams(location.search).get('locked')){
  $('err').style.color='#e5695e';
  $('err').textContent='Your free beta access has ended — upgrade to '
    +'Pro to keep using Aethron.';
}
</script></body></html>
"""

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aethron Studio</title>
<!-- The metal engine. `defer` because nothing above the fold waits on
     it: every surface it decorates is mounted from mountMetal(), which
     runs after a render and checks window.metalWrap first. A browser
     with no WebGL2 simply never gets it, and every one of those
     surfaces is a working button without it. -->
<script defer src="__METAL__"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root{
/* ── THE SYSTEM FACE ────────────────────────────────────────────────
   On macOS `-apple-system` resolves to SF Pro — the actual typeface
   iOS and macOS are set in, already on the machine, correctly hinted
   and optically sized. Aethron was loading Inter from Google Fonts,
   which is a very good face DESIGNED to look like it. Wearing a copy
   of Apple's font while asking to feel like Apple is the wrong way
   round: ask for the system and a Mac hands you the real one. Inter
   stays as the fallback so Windows and Linux still look deliberate. */
--ui:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",
     "Segoe UI Variable Text",Inter,system-ui,sans-serif;
--display:-apple-system,BlinkMacSystemFont,"SF Pro Display",Inter,
     system-ui,sans-serif;
/* iOS rounds bigger than the web habitually does, and the corners are
   CONCENTRIC: an inner radius equals the outer minus the gap between
   them, which is why nested cards there never look like two unrelated
   rectangles. */
--r-xs:8px;--r-sm:11px;--r-md:14px;--r-lg:20px;--r-xl:26px;--r-pill:999px;
--bg:#0b0b0c;--panel:#151417;--panel2:#1c1b1f;--field:#100f11;
--line:#26252a;--line2:#35333a;
--tx:#eceaf0;--dim:#a5a2ad;--lo:#78757f;--u:4px;
--acc:#d97757;--acc2:#e08b6d;--accg:#d97757;--acc-ink:#1c0f09;
--ok:#7ec699;--err:#e5695e;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace;
--r:8px;--r2:12px;
--shadow:0 1px 0 rgba(0,0,0,.45);
--inset:inset 0 1px 0 rgba(255,255,255,.04);
--ease:cubic-bezier(.2,.8,.2,1)}
*{box-sizing:border-box;margin:0}
::selection{background:rgba(217,119,87,.30)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:rgba(148,163,184,.16);border-radius:99px;
border:3px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:rgba(148,163,184,.3)}
::-webkit-scrollbar-track{background:transparent}
body{display:flex;height:100vh;background:var(--bg);color:var(--tx);
font:14px/1.55 var(--ui);overflow:hidden;
-webkit-font-smoothing:antialiased;letter-spacing:.005em}
button,input,select,textarea{font:inherit}
.ic{flex:none;pointer-events:none}
button{display:inline-flex;align-items:center;justify-content:center;gap:7px}
button.iconbtn{width:31px;height:31px;padding:0;color:var(--dim);
border-radius:8px}
button.iconbtn:hover{color:var(--tx)}
#tour{position:fixed;inset:0;z-index:1000;display:none}
#tour.on{display:block}
#tourhole{position:absolute;border-radius:12px;pointer-events:none;
box-shadow:0 0 0 9999px rgba(12,11,10,.72);
border:1.5px solid rgba(217,119,87,.7);transition:all .3s var(--ease)}
.tourbox{position:absolute;width:330px;max-width:calc(100vw - 32px);
background:var(--panel2);border:1px solid var(--line2);border-radius:12px;
padding:16px 16px 13px;box-shadow:0 16px 48px rgba(0,0,0,.55)}
.tourbox h4{font-size:14px;margin:0 0 6px;letter-spacing:-.01em}
.tourbox p{font-size:12.5px;color:var(--dim);line-height:1.65;margin:0}
.tourbox .tnav{display:flex;gap:8px;margin-top:13px;align-items:center}
.tourbox .tstep{font-size:11px;color:var(--dim);margin-right:auto;
letter-spacing:.06em}

/* ── sidebar ─────────────────────────────────────────────── */
aside{width:264px;min-width:264px;background:var(--panel);
border-right:1px solid var(--line);display:flex;flex-direction:column;
position:relative}
aside::before{content:"";position:absolute;inset:0 0 auto 0;height:220px;
background:radial-gradient(420px 200px at 20% -40px,rgba(217,119,87,.08),transparent 70%);
pointer-events:none}
.brand{padding:20px 18px 16px;font-weight:800;font-size:16.5px;
letter-spacing:-.02em;border-bottom:1px solid var(--line);position:relative;
display:flex;align-items:center;gap:8px}
.brand .bmark{width:20px;height:20px;object-fit:contain;flex:none}
.brand b{color:var(--tx)}
.brand span{color:var(--dim);font-weight:500;font-size:13px}
#plist{flex:1;overflow-y:auto;padding:10px;display:flex;
  flex-direction:column}
/* An empty list is a moment to explain the product, not a place to put
   one grey line at the top of 900px of nothing. */
.sideempty{margin:auto;padding:24px 14px;text-align:center;color:var(--lo);
  font-size:12.5px;line-height:1.65}
.sideempty .mk{display:block;margin:0 auto 12px;width:30px;height:30px;
  border-radius:9px;display:grid;place-items:center;
  background:var(--panel2);color:var(--dim)}
.sideempty b{display:block;color:var(--dim);font-weight:500;margin-bottom:4px}
.pitem{padding:11px 12px;border-radius:var(--r);cursor:pointer;display:flex;
justify-content:space-between;align-items:center;gap:8px;
border:1px solid transparent;transition:all .18s var(--ease);
margin-bottom:4px}
.pitem:hover{background:var(--panel2);border-color:var(--line);
transform:translateY(-1px)}
.pitem.sel{background:var(--panel2);border-color:var(--line2);
box-shadow:var(--inset),0 4px 16px -6px rgba(0,0,0,.5)}
.pitem .pmain{flex:1;min-width:0}
.pitem .pname{font-weight:600;font-size:13.5px;letter-spacing:-.01em;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pitem .pmeta{display:flex;gap:8px;align-items:center;margin-top:3px;
font-size:10.5px;color:var(--dim)}
.plat{text-transform:uppercase;letter-spacing:.08em;font-weight:600;
font-size:9.5px;padding:1.5px 7px;border-radius:99px;
border:1px solid var(--line2)}
.plat.framer{color:#a8b8e8;border-color:rgba(148,163,220,.3)}
.plat.webflow{color:#8fc8e8;border-color:rgba(120,180,220,.3)}
.pbar{height:3px;border-radius:99px;background:rgba(148,163,184,.12);
margin-top:7px;overflow:hidden}
.pbar i{display:block;height:100%;background:var(--accg);
border-radius:99px;transition:width .5s var(--ease)}
.pitem .del{color:var(--dim);visibility:hidden;font-size:12px;
padding:4px 7px;border-radius:7px;transition:all .15s}
.pitem:hover .del{visibility:visible}
.pitem .del:hover{color:var(--err);background:rgba(251,113,133,.12)}
.newproj{padding:14px;border-top:1px solid var(--line);display:flex;
flex-direction:column;gap:9px;background:rgba(255,255,255,.015)}
.libbtn{margin:2px 12px 10px;text-align:left;font-size:13px}
.libbtn.sel{border-color:var(--acc);color:var(--acc2)}

/* ── design library ──────────────────────────────────────── */
.libwrap{padding:20px;overflow:auto;height:100%}
.libmatch{display:flex;gap:10px;align-items:stretch}
.libmatch textarea{flex:1;resize:vertical}
.libgrid{display:grid;gap:14px;margin-top:16px;
grid-template-columns:repeat(auto-fill,minmax(255px,1fr))}
.libcard{background:var(--panel);border:1px solid var(--line);
border-radius:var(--r2);padding:15px;box-shadow:var(--shadow);
display:flex;flex-direction:column;gap:9px;
transition:border-color .16s var(--ease)}
.libcard:hover{border-color:var(--line2)}
.libcard.hit{border-color:rgba(217,119,87,.6)}
.lc-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.lc-top b{font-size:14px;letter-spacing:-.01em;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.lc-title{font-size:12px;color:var(--dim);line-height:1.5;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
overflow:hidden}
.lc-pal{display:flex;gap:5px}
.lc-pal i{width:24px;height:24px;border-radius:7px;
border:1px solid var(--line2);box-shadow:var(--inset)}
.lc-fonts{font-size:12px;color:var(--dim)}
.lc-feats{display:flex;flex-wrap:wrap;gap:6px}
.lc-feats span{font-size:11px;padding:3px 9px;border-radius:99px;
border:1px solid var(--line2);color:var(--dim)}
.lc-why{font-size:12.5px;color:var(--acc2);line-height:1.55;
border-top:1px dashed var(--line2);padding-top:9px}
.lc-btns{display:flex;gap:8px;margin-top:auto;padding-top:4px}
.lc-btns .primary{flex:1}

/* ── fields ──────────────────────────────────────────────── */
input,textarea,select{background:var(--field);color:var(--tx);
border:1px solid var(--line2);border-radius:9px;padding:8px 11px;
width:100%;transition:border-color .15s,box-shadow .15s}
input::placeholder,textarea::placeholder{color:var(--dim);opacity:.7}
input:focus,textarea:focus,select:focus{outline:none;
border-color:var(--acc);box-shadow:0 0 0 3px rgba(217,119,87,.20)}
input[type=color]{padding:3px;height:34px;cursor:pointer}
input[type=file]{border-style:dashed;cursor:pointer;font-size:12.5px}

/* ── buttons ─────────────────────────────────────────────── */
button{background:var(--panel2);color:var(--tx);
border:1px solid var(--line2);border-radius:9px;padding:7.5px 13px;
cursor:pointer;white-space:nowrap;font-weight:500;font-size:13px;
transition:all .16s var(--ease);box-shadow:var(--inset)}
button:hover{border-color:rgba(217,119,87,.55);color:var(--acc2);
transform:translateY(-1px);box-shadow:var(--inset),0 4px 12px -4px rgba(0,0,0,.5)}
button:active{transform:translateY(0) scale(.98)}
button:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(217,119,87,.35)}
button.primary,button.big{background:var(--accg);border:none;
color:#fff7f2;font-weight:650;
box-shadow:0 1px 2px rgba(0,0,0,.3),0 4px 16px -4px rgba(205,111,79,.35)}
button.primary:hover,button.big:hover{color:#fff;filter:brightness(1.07);
transform:translateY(-1px);
box-shadow:0 2px 4px rgba(0,0,0,.3),0 8px 24px -6px rgba(205,111,79,.45)}
button.big{font-size:13.5px;padding:9px 18px;border-radius:10px}
button:disabled{opacity:.4;pointer-events:none}

/* ── header / steps ──────────────────────────────────────── */
main{flex:1;display:flex;flex-direction:column;min-width:0}
header{display:flex;align-items:center;gap:14px;padding:13px 20px;
border-bottom:1px solid var(--line);
background:rgba(28,27,25,.85);backdrop-filter:blur(14px);z-index:5}
#ptitle{font-weight:700;font-size:15px;letter-spacing:-.015em}
.steps{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto;
align-items:center}
/* SCOPED, BECAUSE `.step` MEANS TWO DIFFERENT THINGS NOW. These are the
   header's PILL CHIPS. The run card's rows, added with the conversation,
   reuse the same class name — and unscoped, they inherited this pill's
   `border-radius:99px` (the new rule resets `border` but never the
   radius) and this `:hover`, which lifts the row 1px and turns it
   terracotta INSIDE a card with `overflow:hidden`. Measured on the run
   card: every row computed 99px. A class name is an interface; two
   components cannot share one. */
.steps .step,.stepchips .step{display:flex;align-items:center;gap:6px;
font-size:12.5px;
padding:6.5px 13px;border:1px solid var(--line2);border-radius:99px;
cursor:pointer;color:var(--dim);font-weight:500;
transition:all .16s var(--ease)}
.steps .step:hover,.stepchips .step:hover{
border-color:rgba(217,119,87,.55);color:var(--acc2);
transform:translateY(-1px)}
.steps .step.done,.stepchips .step.done{color:var(--ok);
border-color:rgba(74,222,128,.3);
background:rgba(74,222,128,.06)}
.steps .step.run,.stepchips .step.run{color:var(--acc2);
border-color:rgba(217,119,87,.55);
background:rgba(217,119,87,.10)}
.steps .step.run::before,.stepchips .step.run::before{content:"";
width:9px;height:9px;border-radius:50%;
border:2px solid var(--acc);border-top-color:transparent;
animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(1turn)}}
.steps.busy .step{opacity:.5;pointer-events:none}
.steps.busy .step.run{opacity:1}
.stepwrap{align-self:center;position:relative}
.stepwrap summary{cursor:pointer;color:var(--dim);font-size:12px;
list-style:none;padding:6px 6px;border-radius:7px;transition:color .15s}
.stepwrap summary:hover{color:var(--tx)}
.stepchips{display:flex;gap:6px;margin-top:8px;position:absolute;
background:var(--panel);border:1px solid var(--line2);border-radius:12px;
padding:10px;box-shadow:var(--shadow);z-index:30}

/* ── progress ────────────────────────────────────────────── */
#progress{display:none;align-items:center;gap:14px;padding:11px 20px;
background:var(--panel2);border-bottom:1px solid var(--line)}
#progress.on{display:flex;animation:rise .25s var(--ease)}
#progress .bar{flex:1;height:6px;border-radius:99px;
background:rgba(148,163,184,.12);overflow:hidden}
#progress .fill{height:100%;background:var(--accg);width:0;
border-radius:99px;transition:width .45s var(--ease);
box-shadow:0 0 12px rgba(205,111,79,.45)}
#progress .lbl{font-size:13px;color:var(--acc2);white-space:nowrap;
display:flex;align-items:center;gap:9px;font-weight:500}
#progress .lbl::before{content:"";width:11px;height:11px;border-radius:50%;
border:2px solid var(--acc);border-top-color:transparent;
animation:sp .7s linear infinite}
#progress.fail .fill{background:var(--err);box-shadow:none}
#progress.fail .lbl{color:var(--err)}
#progress.fail .lbl::before{animation:none;border:2px solid var(--err)}
#progress.done .fill{background:var(--ok);box-shadow:0 0 12px rgba(74,222,128,.4)}
#progress.done .lbl{color:var(--ok)}
#progress.done .lbl::before{animation:none;border-color:var(--ok)}

/* ── tabs / content ──────────────────────────────────────── */
nav{display:flex;gap:4px;padding:0 20px;background:var(--panel);
border-bottom:1px solid var(--line)}
nav .tab{padding:11px 15px;cursor:pointer;color:var(--dim);
font-size:13.5px;font-weight:500;border-bottom:2px solid transparent;
transition:color .15s;position:relative;top:1px}
nav .tab:hover{color:var(--tx)}
nav .tab.on{color:var(--acc2);border-bottom-color:var(--acc)}
#content{flex:1;overflow:auto;padding:22px;animation:rise .3s var(--ease)}
@keyframes rise{from{opacity:0;transform:translateY(6px)}
to{opacity:1;transform:none}}
.hint{color:var(--dim);font-size:13px;margin:6px 0 14px;line-height:1.6}

/* ── tables ──────────────────────────────────────────────── */
table{width:100%;border-collapse:collapse}
td{padding:10px 12px;border-bottom:1px solid var(--line);
vertical-align:top;transition:background .12s}
tr:hover td{background:rgba(255,255,255,.018)}
td.old{width:44%;color:var(--dim);font-size:13px;word-break:break-word}
td.old .orig{color:var(--tx)}
.tags{font-size:10.5px;color:var(--dim);letter-spacing:.02em}
.tags .cms{color:var(--acc2)}
.budget{font:11px var(--mono);color:var(--dim);margin-top:4px;
font-variant-numeric:tabular-nums}
.budget.over{color:var(--err);font-weight:700}
.budget.fit{color:var(--ok)}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px;
flex-wrap:wrap}
.toolbar .grow{flex:1}

/* ── surfaces ────────────────────────────────────────────── */
#logbox,pre.code{background:#07090d;border:1px solid var(--line);
border-radius:var(--r2);padding:14px 16px;font:12.5px/1.7 var(--mono);
white-space:pre-wrap;word-break:break-word;color:#aeb9cc;min-height:120px}
.card{background:linear-gradient(180deg,var(--panel),rgba(17,20,27,.6));
border:1px solid var(--line);border-radius:var(--r2);padding:20px;
margin-bottom:18px;box-shadow:var(--inset)}
.card h3{font-size:13px;margin-bottom:10px;color:var(--acc2);
font-weight:600;letter-spacing:.01em}
.row{display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.row>*{flex:1;min-width:140px}
.imgthumb{max-height:44px;max-width:110px;border-radius:7px;
background:rgba(255,255,255,.13);display:block}
iframe{width:100%;height:calc(100vh - 195px);border:1px solid var(--line2);
border-radius:var(--r2);background:#fff;box-shadow:var(--shadow)}
.pill{font-size:11px;padding:2.5px 9px;border-radius:99px;
border:1px solid var(--line2);color:var(--dim);font-weight:500}
.pill.ok{color:var(--ok);border-color:rgba(74,222,128,.35)}
.pill.err{color:var(--err);border-color:rgba(251,113,133,.35)}
.empty{color:var(--dim);text-align:center;padding:90px 24px;
font-size:14.5px;line-height:1.8;max-width:520px;margin:0 auto}
.empty b{background:var(--accg);-webkit-background-clip:text;
background-clip:text;color:transparent;font-size:17px}
label{font-size:11.5px;color:var(--dim);display:block;margin-bottom:5px;
font-weight:500;letter-spacing:.02em}
details summary{cursor:pointer;color:var(--dim);font-size:13px;margin:8px 0;
transition:color .15s}
details summary:hover{color:var(--tx)}

/* ── edit panel ──────────────────────────────────────────── */
#editpanel{position:fixed;right:20px;bottom:20px;width:392px;z-index:99;
background:rgba(28,27,25,.94);backdrop-filter:blur(20px);
border:1px solid var(--line2);border-radius:14px;
padding:18px;box-shadow:0 24px 64px -12px rgba(0,0,0,.7);
animation:panelin .28s var(--ease);max-height:calc(100vh - 60px);
overflow-y:auto}
/* edit mode docks the panel in its own right rail so the TEMPLATE
   stays fully visible and clickable — nothing floats over the site */
#editrow{flex:1;display:flex;min-height:0}
#editrow iframe{flex:1;min-width:0}
#editdock{width:0;overflow:hidden;flex:none;background:var(--panel);
transition:width .26s var(--ease)}
#editdock.on{width:390px;border-left:1px solid var(--line);
overflow-y:auto}
#editdock #editpanel{position:static;width:100%;max-height:none;
border:none;border-radius:0;box-shadow:none;background:transparent;
backdrop-filter:none;animation:dockin .24s var(--ease)}
@keyframes dockin{from{opacity:0;transform:translateX(16px)}
to{opacity:1;transform:none}}
@keyframes panelin{from{opacity:0;transform:translateY(14px) scale(.98)}
to{opacity:1;transform:none}}
#editpanel h3{font-size:13.5px;color:var(--acc2);margin-bottom:8px;
display:flex;gap:8px;align-items:center;font-weight:600}
/* ── code view: tree | editor | agent ─────────────────────── */
.codebar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
 padding:10px 14px;border-bottom:1px solid var(--line);background:var(--panel)}
.codebar .sep{width:1px;height:20px;background:var(--line)}
.codebar select,.codebar input{background:var(--bg);color:var(--tx);
 border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12px;
 max-width:210px;flex:0 1 auto;min-width:0}
.codebar input{max-width:150px}
.codestat{font-size:11px;color:var(--dim);margin-left:auto}
.ide{display:grid;grid-template-columns:250px 1fr 380px;
 height:520px;min-height:360px;overflow:hidden}  /* height set by fitIde() */
/* grid children default to min-height:auto and happily grow past the
   container — which pushed the chat input below an unscrollable fold */
.ide>*{min-height:0;min-width:0;overflow:hidden}
.idetree{overflow:auto;border-right:1px solid var(--line);padding:8px;
 font-size:12px;background:var(--panel)}
.treehead{font-weight:600;padding:4px 6px;color:var(--dim);
 text-transform:uppercase;letter-spacing:.06em;font-size:10px}
.tfile{display:flex;gap:6px;align-items:center;padding:3px 6px;border-radius:6px;
 cursor:pointer;color:var(--dim);white-space:nowrap;overflow:hidden}
.tfile span{overflow:hidden;text-overflow:ellipsis}
.tfile:hover{background:var(--panel2);color:var(--tx)}
.tfile.sel{background:var(--acc);color:#fff}
.ideedit{display:flex;flex-direction:column;min-width:0}
.idehead{display:flex;gap:10px;align-items:center;padding:8px 12px;
 border-bottom:1px solid var(--line);font-size:12px;color:var(--dim)}
.idehead button{margin-left:auto}
.lockchip{background:var(--panel2);border-radius:5px;padding:1px 6px;font-size:10px}
#ideta{flex:1;width:100%;border:0;outline:0;resize:none;padding:12px 14px;
 background:var(--bg);color:var(--tx);font:12px/1.55 ui-monospace,SFMono-Regular,
 Menlo,monospace;tab-size:2}
.idechat{display:flex;flex-direction:column;border-left:1px solid var(--line);
 background:var(--panel);min-width:0}
.chatlog{flex:1;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:6px}
.chatin{display:flex;gap:6px;padding:8px;border-top:1px solid var(--line)}
.chatin textarea{flex:1;background:var(--bg);color:var(--tx);border:1px solid var(--line);
 border-radius:8px;padding:8px;font-size:12px;resize:none}
.msg{font-size:12px;line-height:1.5;padding:7px 9px;border-radius:9px;
 white-space:pre-wrap;word-break:break-word}
.msg.you{background:var(--acc);color:#fff;align-self:flex-end;max-width:90%}
.msg.bot{background:var(--panel2)}
.msg.think{color:var(--dim);font-style:italic}
.msg.tool{background:var(--bg);border:1px solid var(--line);font-size:11px}
.msg.tool code{color:var(--dim)}
.msg.res{background:var(--bg);border-left:2px solid var(--line);color:var(--dim);
 font-size:11px;max-height:120px;overflow:auto}
.msg.res.bad{border-left-color:var(--err);color:var(--err)}
.msg.sys{color:var(--dim);font-size:11px;text-align:center}
@media (max-width:1100px){.ide{grid-template-columns:1fr;height:auto !important;
  overflow:visible}
 .ide>*{overflow:visible}
 .idetree{max-height:200px;overflow:auto;border-right:0;
  border-bottom:1px solid var(--line)}
 #ideta{min-height:260px}
 .idechat{min-height:340px;border-left:0;border-top:1px solid var(--line)}
 .chatlog{max-height:260px}}
@media (prefers-reduced-motion:reduce){
*,*::before,*::after{animation-duration:.01ms !important;
transition-duration:.01ms !important}}

/* ── PRECISION WORKBENCH ─────────────────────────────────────────────
   THE ONE ACCENT. The terracotta was on 19 selectors at once — tabs,
   headings, hovers, progress fills, chips — so nothing it touched read
   as important. It now lands on exactly ONE control per screen: the
   action that advances the work in front of you. Everything else earns
   hierarchy from weight, value and space.
   White on #d97757 is only 3.1:1, so the accent button carries dark ink
   (6.0:1) rather than white.                                          */
button.primary,button.big{background:var(--acc);color:var(--acc-ink);
  border-color:transparent;font-weight:600}
button.primary:hover,button.big:hover{background:var(--acc2);color:var(--acc-ink)}

/* demoted: state and emphasis in near-white or grey, never colour */
nav .tab.on{color:var(--tx);border-bottom-color:var(--tx)}
.card h3,#editpanel h3{color:var(--tx)}
button:hover{color:var(--tx)}
.step:hover,.step.run{color:var(--tx)}
.step.run::before{border-color:var(--tx)}
#progress .lbl{color:var(--dim)}
#progress .lbl::before{border-color:var(--line2)}
#progress .fill,.pbar i{background:var(--lo)}
.libbtn.sel{border-color:var(--line2);color:var(--tx)}
.tfile.sel{background:var(--panel2);color:var(--tx)}
.lc-why,.tags .cms{color:var(--dim)}
.empty b{background:var(--panel2);color:var(--tx)}
.msg.you{background:var(--panel2)}

/* focus stays obvious — a11y outranks restraint — but neutral */
input:focus,textarea:focus,select:focus{border-color:var(--tx);
  outline:none;box-shadow:0 0 0 3px rgba(236,234,240,.10)}
:focus-visible{outline:2px solid var(--tx);outline-offset:2px}

/* NUMBERS MUST NOT RE-FLOW as they count up during a fill */
.pitem .pmeta,.pbar,#progress,.tags,.lc-pal,code,.mono{
  font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}

/* quieter chrome: hairlines, not boxes */
.card,.libcard,.newproj{box-shadow:none;border-color:var(--line)}
.pitem.sel{background:var(--panel2)}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;
    transition-duration:.01ms!important}}

/* ── THE FOCAL MOMENT ────────────────────────────────────────────────
   The old empty state put three lines of explanation in the middle and
   exiled the actual entry point — the URL field — to a form in the
   sidebar, under a four-line caption. So the eye landed on prose and
   the thing you came to do was somewhere else.
   One line, one input, and the alternatives as quiet rows beneath.   */
/* CENTRED OVERFLOW CLIPS THE TOP. place-items:center on a grid spills
   equally in both directions once the content outgrows the container,
   so the headline rendered at top:-127px — present, styled, invisible.
   margin:auto centres without ever pushing content out of reach.     */
.empty{display:flex;min-height:70vh;padding:56px 24px;overflow:auto;
  text-align:left;color:var(--dim)}
.focal{margin:auto;width:100%;max-width:600px}
.focal h1,#welcome h1{font-size:32px;line-height:1.18;letter-spacing:-.02em;
  font-weight:600;color:var(--tx);margin:0 0 12px}
.focal .sub,#welcome .sub{font-size:13px;line-height:1.6;color:var(--dim);
  margin:0 0 32px;max-width:52ch}

.startrow{display:flex;gap:8px;align-items:center;margin-bottom:12px}
.startrow input{flex:1;height:44px;padding:0 14px;font-size:15px;
  background:var(--field);border:1px solid var(--line2);border-radius:10px;
  color:var(--tx)}
.startrow button.primary{width:44px;height:44px;padding:0;flex:none;
  border-radius:10px;display:grid;place-items:center}
.startrow button.primary [data-ic]{transform:rotate(90deg)}

.startmeta{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  font-size:12px;color:var(--lo);margin-bottom:22px}
.startmeta input{height:32px;padding:0 10px;font-size:12px;
  background:transparent;border:1px solid var(--line);border-radius:8px;
  color:var(--tx)}
.startmeta .or{color:var(--lo)}
.fpick{display:inline-flex;align-items:center;gap:6px;cursor:pointer;
  color:var(--lo);transition:color var(--dur,180ms) var(--ease)}
.fpick:hover{color:var(--tx)}
.fpick input{position:absolute;width:1px;height:1px;opacity:0;
  clip:rect(0 0 0 0)}

/* quiet rows — the alternatives, available without shouting */
.qrows,.srows{display:flex;flex-direction:column;margin-top:8px}
.qrow{display:flex;align-items:center;gap:12px;width:100%;
  padding:13px 12px;background:none;border:0;border-top:1px solid var(--line);
  color:var(--dim);font-size:13px;text-align:left;cursor:pointer;
  transition:color var(--dur,180ms) var(--ease),
             background var(--dur,180ms) var(--ease)}
.qrows .qrow:last-child,.srows .qrow:last-child{
  border-bottom:1px solid var(--line)}
.qrow:hover{color:var(--tx);background:var(--panel2)}
.qrow .ql{flex:1}
.qrow .qc{opacity:0;transform:rotate(90deg);
  transition:opacity var(--dur,180ms) var(--ease)}
.qrow:hover .qc{opacity:.5}
.srows{margin:16px 0 0;padding:0 6px}
.srows .qrow{padding:11px 8px;font-size:12.5px}

@media (max-width:900px){.focal h1,#welcome h1{font-size:24px}}

/* ══ THE CONVERSATION ═══════════════════════════════════════════════
   ONE SURFACE. The person says what they want in their own words and
   Aethron works out whether that is a migration, a framework port, a
   screenshot rebuild or a coding job — there is no mode to choose and
   nothing to switch to.

   TWO THINGS ARE KEPT APART, because they are different things and
   merging them gives an interface that is neither a conversation nor a
   progress report:
     WHAT IT SAYS    prose, unboxed, generous leading — a reply
     WHAT IT DOES    compact steps in a run card, each naming the REAL
                     file or command, folded away until asked for
   Raw tool JSON and the CLI's own chatter are never the surface. They
   are the truth underneath it, one click down.

   AND THE COMPOSER IS PINNED. It used to sit in normal flow after a
   46vh log, so on a tall window it fell below the fold — the single
   most important control in the product, invisible unless you thought
   to scroll. It now owns its own dock and cannot be pushed anywhere. */
#content.conv-host{flex:1;min-height:0;display:flex;flex-direction:column;
  padding:0;overflow:hidden}
.conv-shell{display:flex;flex-direction:column;flex:1;min-height:0}
.conv-scroll{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;
  padding:26px 24px 6px;scroll-behavior:smooth}
/* ══ THE OPENING ═══════════════════════════════════════════════════
   BEFORE THERE IS A CONVERSATION THERE IS NO DOCK. Pinning the composer
   to the floor is right once there is a log above it and wrong before
   there is anything at all: it strands the composer at the bottom of
   the window, far from the words it belongs to, with a dead void
   between them. First it was top-aligned and the void was underneath;
   then bottom-aligned and the void was on top. Both were the same
   mistake — treating two halves of one thought as two regions.
   Empty, they are ONE GROUP, centred: headline, the thing you type in,
   and the ways in. The floor is only a floor when something stands on
   it. */
/* Centred with AUTO MARGINS, not justify-content. On a window shorter
   than the group, `justify-content:center` overflows both ends and the
   top becomes unreachable — measured at 320px tall, the composer sat
   outside the viewport with no way to scroll to it. Auto margins centre
   when there is room and yield to scrolling when there is not. */
.conv-shell:not(.talking){overflow-y:auto}
.conv-shell:not(.talking) .conv-scroll{flex:0 0 auto;overflow:visible;
  padding:0 24px;margin-top:auto}
.conv-shell:not(.talking) .conv-dock{margin-bottom:auto}
.conv-shell:not(.talking) .conv-dock{background:none;padding:14px 24px 0}
.conv-shell:not(.talking) .conv-wrap{max-width:680px}
/* the group breathes in on arrival rather than simply being there */
.conv-shell:not(.talking) #welcome,
.conv-shell:not(.talking) .conv-dock{
  animation:fadeup .6s cubic-bezier(.22,.9,.28,1) both}
.conv-shell:not(.talking) .conv-dock{animation-delay:.07s}
.conv-wrap{width:100%;max-width:760px;margin:0 auto}
.conv-dock{flex:none;padding:8px 24px 16px;
  background:linear-gradient(to top,var(--bg) 62%,transparent)}

/* the composer */
/* THE COMPOSER IS THE FOCAL POINT, so it carries its own light: a warm
   bloom beneath it that lifts it off the field, and a ring that answers
   the moment you put the cursor in it. */
.composer{position:relative;display:flex;gap:8px;align-items:flex-end;
  padding:8px 8px 8px 14px;
  background:var(--field);border:1px solid var(--line2);border-radius:16px;
  transition:border-color .22s var(--ease),box-shadow .28s var(--ease),
             transform .22s cubic-bezier(.22,.9,.28,1)}
.composer::before{content:"";position:absolute;inset:-34px -18px -26px;
  z-index:-1;pointer-events:none;border-radius:34px;
  background:radial-gradient(56% 120% at 50% 62%,
    rgba(217,119,87,.17),transparent 72%);
  opacity:.75;transition:opacity .4s var(--ease)}
.composer:focus-within{border-color:rgba(217,119,87,.55);
  box-shadow:0 0 0 3px rgba(217,119,87,.13),0 10px 34px rgba(0,0,0,.34);
  transform:translateY(-1px)}
.composer:focus-within::before{opacity:1}
/* ── border-beam, from libraries.dev ────────────────────────────────
   MIT License · Copyright (c) 2026 Jakub Antalik
   https://github.com/Jakubantalik/Libraries.dev

   This is the author's OWN generated CSS — produced by running his
   `generateBeamCSS` for the `pulse-inner` variant at his tuned dark
   values (duration 2.3, stroke 1.54, inner 0.44, bloom 0.66,
   saturation 1.2, brightness 0.75) — plus his shared 30fps pulse
   driver, type-stripped. Not re-derived by hand.
   TWO THINGS ARE OURS, both deliberate: the palette is turned into the
   ember band (hue only — his lightness and saturation are untouched,
   which is the same rule this project enforces on any recolour), and
   the hue oscillator is narrowed from a full 360 circle to 26 degrees,
   because a full sweep walks a warm brand through green and blue.
   ──────────────────────────────────────────────────────────────────── */

@property --bw1-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bh1-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bw2-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bh2-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bw3-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bh3-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bgh-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bop-tl-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bop-tr-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bop-bl-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bop-br-ae {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --bx1-ae {
  syntax: "<length>";
  initial-value: 0px;
  inherits: true;
}

@property --by1-ae {
  syntax: "<length>";
  initial-value: 0px;
  inherits: true;
}

@property --bx2-ae {
  syntax: "<length>";
  initial-value: 0px;
  inherits: true;
}

@property --by2-ae {
  syntax: "<length>";
  initial-value: 0px;
  inherits: true;
}

@property --bx3-ae {
  syntax: "<length>";
  initial-value: 0px;
  inherits: true;
}

@property --by3-ae {
  syntax: "<length>";
  initial-value: 0px;
  inherits: true;
}

@property --beam-opacity-ae {
  syntax: "<number>";
  initial-value: 0;
  inherits: true;
}

@property --beam-hue-ae {
  syntax: "<angle>";
  initial-value: 0deg;
  inherits: true;
}

[data-beam="ae"] {
  position: relative;
  border-radius: 18px;
  overflow: hidden;
  isolation: isolate;
}

[data-beam="ae"][data-active] {
  animation: beam-fade-in-ae 0.6s ease forwards;
}

[data-beam="ae"][data-fading] {
  animation: beam-fade-out-ae 0.5s ease forwards;
}

[data-beam="ae"][data-active]::after,
[data-beam="ae"][data-fading]::after {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: 18px;
  padding: 1px;
  clip-path: inset(0 round 18px);
  background: radial-gradient(ellipse calc(70px * var(--bw1-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(40px * var(--bh1-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(33% + var(--bx1-ae)) calc(-7.4% + var(--by1-ae)), rgba(255, 199, 50, var(--bop-tl-ae)), transparent),
    radial-gradient(ellipse calc(60px * var(--bw2-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(35px * var(--bh2-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(12% + var(--bx2-ae)) calc(-5% + var(--by2-ae)), rgba(255, 147, 40, var(--bop-tl-ae)), transparent),
    radial-gradient(ellipse calc(40px * var(--bw3-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(70px * var(--bh3-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(2.1% + var(--bx3-ae)) calc(68.3% + var(--by3-ae)), rgba(200, 104, 50, var(--bop-bl-ae)), transparent),
    radial-gradient(ellipse calc(20px * var(--bw1-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(35px * var(--bh1-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(2.1% + var(--bx1-ae)) calc(68.3% + var(--by1-ae)), rgba(185, 97, 30, var(--bop-bl-ae)), transparent),
    radial-gradient(ellipse calc(180px * var(--bw2-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(32px * var(--bh2-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(74.4% + var(--bx2-ae)) calc(100% + var(--by2-ae)), rgba(255, 174, 70, var(--bop-br-ae)), transparent),
    radial-gradient(ellipse calc(85px * var(--bw3-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(26px * var(--bh3-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(55% + var(--bx3-ae)) calc(100% + var(--by3-ae)), rgba(255, 147, 40, var(--bop-br-ae)), transparent),
    radial-gradient(ellipse calc(74px * var(--bw1-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(32px * var(--bh1-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(93.9% + var(--bx1-ae)) calc(0% + var(--by1-ae)), rgba(255, 77, 40, var(--bop-tr-ae)), transparent),
    radial-gradient(ellipse calc(26px * var(--bw2-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(42px * var(--bh2-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(100% + var(--bx2-ae)) calc(27.1% + var(--by2-ae)), rgba(240, 179, 50, var(--bop-tr-ae)), transparent),
    radial-gradient(ellipse calc(52px * var(--bw3-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(48px * var(--bh3-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(100% + var(--bx3-ae)) calc(27.1% + var(--by3-ae)), rgba(240, 163, 40, var(--bop-tr-ae)), transparent);
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor;
  mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  mask-composite: exclude;
  pointer-events: none;
  z-index: 2;
  will-change: opacity, filter;
  opacity: calc(var(--beam-opacity-ae) * 1.54 * var(--beam-stroke-opacity, 1) * var(--beam-strength, 1));
  filter: hue-rotate(calc(var(--beam-hue-base, 0deg) + var(--beam-hue-ae))) brightness(0.75) saturate(1.20);
}

[data-beam="ae"][data-active]::before,
[data-beam="ae"][data-fading]::before {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: 18px;
  clip-path: inset(0 round 18px);
  background: radial-gradient(ellipse calc(65px * var(--bw1-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(35px * var(--bh1-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(33% + var(--bx1-ae)) calc(-7.4% + var(--by1-ae)), rgba(255, 199, 50, var(--bop-tl-ae)), transparent),
    radial-gradient(ellipse calc(55px * var(--bw2-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(30px * var(--bh2-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(12% + var(--bx2-ae)) calc(-5% + var(--by2-ae)), rgba(255, 147, 40, var(--bop-tl-ae)), transparent),
    radial-gradient(ellipse calc(35px * var(--bw3-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(65px * var(--bh3-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(2.1% + var(--bx3-ae)) calc(68.3% + var(--by3-ae)), rgba(200, 104, 50, var(--bop-bl-ae)), transparent),
    radial-gradient(ellipse calc(15px * var(--bw1-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(30px * var(--bh1-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(2.1% + var(--bx1-ae)) calc(68.3% + var(--by1-ae)), rgba(185, 97, 30, var(--bop-bl-ae)), transparent),
    radial-gradient(ellipse calc(173px * var(--bw2-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(28px * var(--bh2-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(74.4% + var(--bx2-ae)) calc(100% + var(--by2-ae)), rgba(255, 174, 70, var(--bop-br-ae)), transparent),
    radial-gradient(ellipse calc(80px * var(--bw3-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(22px * var(--bh3-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(55% + var(--bx3-ae)) calc(100% + var(--by3-ae)), rgba(255, 147, 40, var(--bop-br-ae)), transparent),
    radial-gradient(ellipse calc(69px * var(--bw1-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(28px * var(--bh1-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(93.9% + var(--bx1-ae)) calc(0% + var(--by1-ae)), rgba(255, 77, 40, var(--bop-tr-ae)), transparent),
    radial-gradient(ellipse calc(22px * var(--bw2-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(38px * var(--bh2-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(100% + var(--bx2-ae)) calc(27.1% + var(--by2-ae)), rgba(240, 179, 50, var(--bop-tr-ae)), transparent),
    radial-gradient(ellipse calc(47px * var(--bw3-ae) * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(44px * var(--bh3-ae) * var(--bgh-ae) * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at calc(100% + var(--bx3-ae)) calc(27.1% + var(--by3-ae)), rgba(240, 163, 40, var(--bop-tr-ae)), transparent),
    radial-gradient(ellipse 60px 60px at 0% 0%, rgba(255, 255, 255, calc(0.18 * var(--bop-tl-ae))), transparent 70%),
    radial-gradient(ellipse 60px 60px at 100% 0%, rgba(255, 255, 255, calc(0.18 * var(--bop-tr-ae))), transparent 70%),
    radial-gradient(ellipse 60px 60px at 0% 100%, rgba(255, 255, 255, calc(0.18 * var(--bop-bl-ae))), transparent 70%),
    radial-gradient(ellipse 60px 60px at 100% 100%, rgba(255, 255, 255, calc(0.18 * var(--bop-br-ae))), transparent 70%);
  -webkit-mask-image:
    linear-gradient(white, transparent 28px, transparent calc(100% - 28px), white),
    linear-gradient(to right, white, transparent 28px, transparent calc(100% - 28px), white);
  -webkit-mask-composite: source-over;
  mask-image:
    linear-gradient(white, transparent 28px, transparent calc(100% - 28px), white),
    linear-gradient(to right, white, transparent 28px, transparent calc(100% - 28px), white);
  mask-composite: add;
  pointer-events: none;
  z-index: 1;
  will-change: opacity, filter;
  opacity: calc(var(--beam-opacity-ae) * 0.44 * var(--beam-inner-opacity, 1) * var(--beam-strength, 1));
  filter: hue-rotate(calc(var(--beam-hue-base, 0deg) + var(--beam-hue-ae))) brightness(0.75) saturate(1.20);
}

[data-beam="ae"] [data-beam-bloom] {
  display: none;
  position: absolute;
  inset: 0;
  border-radius: 18px;
  clip-path: inset(0 round 18px);
  background: radial-gradient(ellipse calc(84px * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(48px * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at 33% -7.4%, rgba(255, 199, 50, 0.76), transparent),
    radial-gradient(ellipse calc(72px * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(42px * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at 12% -5%, rgba(255, 147, 40, 0.76), transparent),
    radial-gradient(ellipse calc(48px * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(84px * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at 2.1% 68.3%, rgba(200, 104, 50, 0.76), transparent),
    radial-gradient(ellipse calc(216px * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(38px * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at 74.4% 100%, rgba(255, 174, 70, 0.76), transparent),
    radial-gradient(ellipse calc(102px * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(31px * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at 55% 100%, rgba(255, 147, 40, 0.76), transparent),
    radial-gradient(ellipse calc(89px * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(38px * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at 93.9% 0%, rgba(255, 77, 40, 0.76), transparent),
    radial-gradient(ellipse calc(62px * var(--pulse-glow-sx, 1) * var(--pulse-glow-boost, 1)) calc(58px * var(--pulse-glow-sy, 1) * var(--pulse-glow-boost, 1)) at 100% 27.1%, rgba(240, 163, 40, 0.76), transparent);
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor;
  mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  mask-composite: exclude;
  padding: 1px;
  pointer-events: none;
  z-index: 3;
  will-change: opacity;
  opacity: 0;
}

[data-beam="ae"][data-active] [data-beam-bloom],
[data-beam="ae"][data-fading] [data-beam-bloom] {
  display: block;
  opacity: calc(var(--beam-opacity-ae) * 0.66 * var(--beam-bloom-opacity, 1) * var(--beam-strength, 1));
  filter: blur(8px) hue-rotate(calc(var(--beam-hue-base, 0deg) + var(--beam-hue-ae))) brightness(0.75) saturate(1.20);
}

@keyframes beam-fade-in-ae { to { --beam-opacity-ae: 1; } }
@keyframes beam-fade-out-ae { from { --beam-opacity-ae: 1; } to { --beam-opacity-ae: 0; } }

[data-beam="ae"][data-paused],
[data-beam="ae"][data-paused]::after,
[data-beam="ae"][data-paused]::before,
[data-beam="ae"][data-paused] [data-beam-bloom] {
  animation-play-state: paused !important;
}

@media (prefers-reduced-motion: reduce) {
  [data-beam="ae"][data-active],
  [data-beam="ae"][data-fading],
  [data-beam="ae"][data-active]::after,
  [data-beam="ae"][data-fading]::after,
  [data-beam="ae"][data-active]::before,
  [data-beam="ae"][data-fading]::before,
  [data-beam="ae"][data-active] [data-beam-bloom],
  [data-beam="ae"][data-fading] [data-beam-bloom] {
    animation: none !important;
  }
}



/* ── and the LINE variant, for a project sitting idle ───────────────
   Same generator, same MIT source, `size: 'line'` at his tuned 3.1s —
   a single travelling glow along the bottom edge instead of a ring.
   Pure CSS, no driver: the pulse family needs the oscillators, this
   one does not. Its hue drift is already only +/-13 degrees, so unlike
   the pulse variant it needed no clamping to stay in the warm family.

   THE THREE STATES, and each says something different:
     nothing started    the ring, breathing   "ready, nothing running"
     Aethron working    the ring, breathing   "this is live"
     a project at rest  the bottom line       "loaded, waiting on you"
   */

@property --beam-x-ln {
  syntax: "<number>";
  initial-value: 0;
  inherits: true;
}

@property --beam-w-ln {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --beam-h-ln {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --beam-spike-ln {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --beam-spike2-ln {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --beam-edge-ln {
  syntax: "<number>";
  initial-value: 1;
  inherits: true;
}

@property --beam-opacity-ln {
  syntax: "<number>";
  initial-value: 0;
  inherits: true;
}

[data-beam="ln"] {
  position: relative;
  border-radius: 18px;
  overflow: hidden;
}

[data-beam="ln"][data-active] {
  animation:
    beam-travel-ln 3.1s linear infinite,
    beam-edge-fade-ln 3.1s linear infinite,
    beam-breathe-ln 4.0s ease-in-out infinite,
    beam-spike-ln 4.1s ease-in-out infinite,
    beam-spike2-ln 5.3s ease-in-out infinite,
    beam-fade-in-ln 0.6s ease forwards;
}

[data-beam="ln"][data-fading] {
  animation:
    beam-travel-ln 3.1s linear infinite,
    beam-edge-fade-ln 3.1s linear infinite,
    beam-breathe-ln 4.0s ease-in-out infinite,
    beam-spike-ln 4.1s ease-in-out infinite,
    beam-spike2-ln 5.3s ease-in-out infinite,
    beam-fade-out-ln 0.5s ease forwards;
}

[data-beam="ln"][data-active]::after,
[data-beam="ln"][data-fading]::after {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: 17px;
  padding: 1px;
  clip-path: inset(0 round 18px);
  background: radial-gradient(
        ellipse calc(24px * var(--beam-w-ln)) calc(28px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) calc(100% + 2px),
        rgba(255, 255, 255, 0.38) 0%,
        rgba(255, 255, 255, 0.12) 30%,
        transparent 65%
      ), radial-gradient(ellipse calc(36px * var(--beam-w-ln)) calc(36px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) calc(100% + 2px), rgb(255, 50, 100), transparent),
       radial-gradient(ellipse calc(30px * var(--beam-w-ln)) calc(32px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% + 39px) calc(100%), rgb(40, 180, 220), transparent),
       radial-gradient(ellipse calc(33px * var(--beam-w-ln)) calc(28px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% - 36px) calc(100% + 2px), rgb(50, 200, 80), transparent),
       radial-gradient(ellipse calc(29px * var(--beam-w-ln)) calc(34px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% - 54px) calc(100%), rgb(180, 40, 240), transparent),
       radial-gradient(ellipse calc(27px * var(--beam-w-ln)) calc(30px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% + 51px) calc(100% - 1px), rgb(255, 160, 30), transparent),
       radial-gradient(ellipse calc(36px * var(--beam-w-ln)) calc(24px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% + 21px) calc(100% + 1px), rgb(100, 70, 255), transparent),
       radial-gradient(ellipse calc(30px * var(--beam-w-ln)) calc(22px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% - 21px) calc(100%), rgb(40, 140, 255), transparent),
       radial-gradient(ellipse calc(25px * var(--beam-w-ln)) calc(28px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% + 66px) calc(100% + 1px), rgb(240, 50, 180), transparent),
       radial-gradient(ellipse calc(23px * var(--beam-w-ln)) calc(30px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% - 66px) calc(100% - 1px), rgb(30, 185, 170), transparent);
  -webkit-mask:
    radial-gradient(
      ellipse calc(78px * var(--beam-w-ln)) calc(60px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) 100%,
      white 0%, rgba(255, 255, 255, 0.5) 45%, transparent 100%
    ),
    linear-gradient(#fff 0 0) content-box,
    linear-gradient(#fff 0 0);
  -webkit-mask-composite: source-in, xor;
  mask:
    radial-gradient(
      ellipse calc(78px * var(--beam-w-ln)) calc(60px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) 100%,
      white 0%, rgba(255, 255, 255, 0.5) 45%, transparent 100%
    ),
    linear-gradient(#fff 0 0) content-box,
    linear-gradient(#fff 0 0);
  mask-composite: intersect, exclude;
  pointer-events: none;
  z-index: 2;
  opacity: calc(var(--beam-opacity-ln) * var(--beam-edge-ln) * 0.46 * var(--beam-stroke-opacity, 1) * var(--beam-strength, 1));
  animation: beam-hue-shift-ln 12s ease-in-out infinite;
}

[data-beam="ln"][data-active]::before,
[data-beam="ln"][data-fading]::before {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: 18px;
  background: radial-gradient(ellipse calc(33px * var(--beam-w-ln)) calc(30px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) calc(100%), rgba(255, 199, 50, 0.48), transparent),
    radial-gradient(ellipse calc(24px * var(--beam-w-ln)) calc(26px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% + 39px) calc(100% - 3px), rgba(220, 124, 40, 0.42), transparent),
    radial-gradient(ellipse calc(27px * var(--beam-w-ln)) calc(24px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% - 36px) calc(100%), rgba(200, 104, 50, 0.48), transparent),
    radial-gradient(ellipse calc(23px * var(--beam-w-ln)) calc(28px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% - 54px) calc(100% - 2px), rgba(240, 163, 40, 0.42), transparent),
    radial-gradient(ellipse calc(24px * var(--beam-w-ln)) calc(24px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% + 51px) calc(100% - 1px), rgba(255, 73, 30, 0.50), transparent),
    radial-gradient(ellipse calc(30px * var(--beam-w-ln)) calc(20px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% + 21px) calc(100%), rgba(255, 174, 70, 0.45), transparent),
    radial-gradient(ellipse calc(25px * var(--beam-w-ln)) calc(18px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% - 21px) calc(100% - 2px), rgba(255, 147, 40, 0.40), transparent),
    radial-gradient(ellipse calc(21px * var(--beam-w-ln)) calc(24px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% + 66px) calc(100%), rgba(240, 179, 50, 0.45), transparent),
    radial-gradient(ellipse calc(18px * var(--beam-w-ln)) calc(26px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100% - 66px) calc(100% - 1px), rgba(185, 97, 30, 0.52), transparent);
  box-shadow: inset 0 0 9px 1px rgba(255,255,255,0.3);
  -webkit-mask-image:
    radial-gradient(
      ellipse calc(78px * var(--beam-w-ln)) calc(60px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) 100%,
      white 0%, rgba(255, 255, 255, 0.5) 45%, transparent 100%
    ),
    linear-gradient(white, transparent 28px, transparent calc(100% - 28px), white),
    linear-gradient(to right, white, transparent 28px, transparent calc(100% - 28px), white);
  -webkit-mask-composite: source-in, source-over;
  mask-image:
    radial-gradient(
      ellipse calc(78px * var(--beam-w-ln)) calc(60px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) 100%,
      white 0%, rgba(255, 255, 255, 0.5) 45%, transparent 100%
    ),
    linear-gradient(white, transparent 28px, transparent calc(100% - 28px), white),
    linear-gradient(to right, white, transparent 28px, transparent calc(100% - 28px), white);
  mask-composite: intersect, add;
  pointer-events: none;
  z-index: 1;
  opacity: calc(var(--beam-opacity-ln) * var(--beam-edge-ln) * 0.24 * var(--beam-inner-opacity, 1) * var(--beam-strength, 1));
  clip-path: inset(0 round 18px);
  animation: beam-hue-shift-ln 12s ease-in-out infinite;
}

[data-beam="ln"] [data-beam-bloom] {
  display: none;
  position: absolute;
  inset: 0;
  border-radius: 17px;
  clip-path: inset(0 round 18px);
  padding: 0;
  -webkit-mask: radial-gradient(
    ellipse calc(84px * var(--beam-w-ln)) calc(110px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) 100%,
    white 0%, rgba(255, 255, 255, 0.5) 35%, transparent 100%
  );
  -webkit-mask-composite: source-over;
  mask: radial-gradient(
    ellipse calc(84px * var(--beam-w-ln)) calc(110px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) 100%,
    white 0%, rgba(255, 255, 255, 0.5) 35%, transparent 100%
  );
  mask-composite: add;
  background: radial-gradient(ellipse calc(0.8px * var(--beam-spike-ln) * var(--beam-spike-mul, 1)) calc(92px * var(--beam-h-ln) * var(--beam-spike-mul, 1)) at 8% calc(100% - 2px), rgb(255, 60, 80), rgb(255, 60, 80) 30%, transparent 88%),
       radial-gradient(ellipse calc(10px * var(--beam-spike2-ln) * var(--beam-spike-mul, 1)) calc(35px * var(--beam-h-ln) * var(--beam-spike-mul, 1)) at 22% calc(100% - 4px), rgba(190, 105, 40, 0.98), rgba(190, 105, 40, 0.49) 50%, transparent 95%),
       radial-gradient(ellipse calc(2px * (2 - var(--beam-spike-ln)) * var(--beam-spike-mul, 1)) calc(72px * var(--beam-h-ln) * var(--beam-spike-mul, 1)) at 36% calc(100% - 3px), rgb(100, 70, 255), rgba(255, 174, 70, 1) 40%, transparent 90%),
       radial-gradient(ellipse calc(14px * var(--beam-spike2-ln) * var(--beam-spike-mul, 1)) calc(28px * var(--beam-h-ln) * var(--beam-spike-mul, 1)) at 50% calc(100% - 2px), rgba(255, 82, 40, 0.59), rgba(255, 82, 40, 0.29) 55%, transparent 96%),
       radial-gradient(ellipse calc(1.2px * (2 - var(--beam-spike2-ln)) * var(--beam-spike-mul, 1)) calc(85px * var(--beam-h-ln) * var(--beam-spike-mul, 1)) at 64% calc(100% - 4px), rgb(50, 200, 100), rgba(200, 106, 50, 1) 35%, transparent 89%),
       radial-gradient(ellipse calc(7px * var(--beam-spike-ln) * var(--beam-spike-mul, 1)) calc(45px * var(--beam-h-ln) * var(--beam-spike-mul, 1)) at 78% calc(100% - 2px), rgba(240, 169, 50, 0.91), rgba(240, 169, 50, 0.45) 48%, transparent 94%),
       radial-gradient(ellipse calc(0.6px * (2 - var(--beam-spike-ln)) * var(--beam-spike-mul, 1)) calc(60px * var(--beam-h-ln) * var(--beam-spike-mul, 1)) at 92% calc(100% - 3px), rgb(40, 140, 255), rgba(255, 147, 40, 1) 42%, transparent 91%),
       radial-gradient(ellipse calc(21px * var(--beam-spike-ln)) calc(15px * var(--beam-spike2-ln)) at calc(var(--beam-x-ln) * 100%) calc(100% + 1px), rgba(255, 255, 255, 1) 0%, rgba(255, 255, 255, 0.9) 20%, rgba(255, 255, 255, 0.5) 50%, transparent 100%),
       radial-gradient(ellipse calc(42px * var(--beam-w-ln)) calc(40px * var(--beam-h-ln)) at calc(var(--beam-x-ln) * 100%) 100%, rgba(255, 255, 255, 0.3) 0%, rgba(255, 255, 255, 0.12) 25%, rgba(255, 255, 255, 0.03) 55%, transparent 80%);
  
  pointer-events: none;
  z-index: 3;
  opacity: 0;
}

[data-beam="ln"][data-active] [data-beam-bloom],
[data-beam="ln"][data-fading] [data-beam-bloom] {
  display: block;
  opacity: calc(var(--beam-opacity-ln) * var(--beam-edge-ln) * 0.38 * var(--beam-bloom-opacity, 1) * var(--beam-strength, 1));
  animation: beam-hue-shift-bloom-ln 8s ease-in-out infinite;
}

@keyframes beam-travel-ln {
  0%   { --beam-x-ln: 0.06;  --beam-w-ln: 0.5; }
  10%  { --beam-x-ln: 0.15;  --beam-w-ln: 0.8; }
  20%  { --beam-x-ln: 0.25;  --beam-w-ln: 1.1; }
  30%  { --beam-x-ln: 0.35;  --beam-w-ln: 1.3; }
  40%  { --beam-x-ln: 0.44;  --beam-w-ln: 1.45; }
  50%  { --beam-x-ln: 0.5;   --beam-w-ln: 1.5; }
  60%  { --beam-x-ln: 0.56;  --beam-w-ln: 1.45; }
  70%  { --beam-x-ln: 0.65;  --beam-w-ln: 1.3; }
  80%  { --beam-x-ln: 0.75;  --beam-w-ln: 1.1; }
  90%  { --beam-x-ln: 0.85;  --beam-w-ln: 0.8; }
  100% { --beam-x-ln: 0.94;  --beam-w-ln: 0.5; }
}

@keyframes beam-edge-fade-ln {
  0%    { --beam-edge-ln: 0; }
  12.5% { --beam-edge-ln: 0; }
  32.5% { --beam-edge-ln: 1; }
  67.5% { --beam-edge-ln: 1; }
  87.5% { --beam-edge-ln: 0; }
  100%  { --beam-edge-ln: 0; }
}

@keyframes beam-breathe-ln {
  0%, 100% { --beam-h-ln: 0.8; }
  25%      { --beam-h-ln: 1.25; }
  55%      { --beam-h-ln: 0.85; }
  80%      { --beam-h-ln: 1.3; }
}

@keyframes beam-spike-ln {
  0%   { --beam-spike-ln: 0.8; }
  25%  { --beam-spike-ln: 1.3; }
  50%  { --beam-spike-ln: 0.9; }
  75%  { --beam-spike-ln: 1.4; }
  100% { --beam-spike-ln: 0.8; }
}

@keyframes beam-spike2-ln {
  0%   { --beam-spike2-ln: 1.2; }
  25%  { --beam-spike2-ln: 0.7; }
  50%  { --beam-spike2-ln: 1.4; }
  75%  { --beam-spike2-ln: 0.8; }
  100% { --beam-spike2-ln: 1.2; }
}

@keyframes beam-fade-in-ln {
  to { --beam-opacity-ln: 1; }
}

@keyframes beam-fade-out-ln {
  from { --beam-opacity-ln: 1; }
  to { --beam-opacity-ln: 0; }
}

@keyframes beam-hue-shift-ln {
  0% { filter: hue-rotate(calc(var(--beam-hue-base, 0deg) - 13deg)) brightness(1.00) saturate(1.20); }
  50% { filter: hue-rotate(calc(var(--beam-hue-base, 0deg) + 13deg)) brightness(1.00) saturate(1.20); }
  100% { filter: hue-rotate(calc(var(--beam-hue-base, 0deg) - 13deg)) brightness(1.00) saturate(1.20); }
}

@keyframes beam-hue-shift-bloom-ln {
  0% { filter: blur(8px) hue-rotate(calc(var(--beam-hue-base, 0deg) - 23deg)) brightness(1.00) saturate(1.20); }
  50% { filter: blur(8px) hue-rotate(calc(var(--beam-hue-base, 0deg) + 23deg)) brightness(1.00) saturate(1.20); }
  100% { filter: blur(8px) hue-rotate(calc(var(--beam-hue-base, 0deg) - 23deg)) brightness(1.00) saturate(1.20); }
}

[data-beam="ln"][data-paused],
[data-beam="ln"][data-paused]::after,
[data-beam="ln"][data-paused]::before,
[data-beam="ln"][data-paused] [data-beam-bloom] {
  animation-play-state: paused !important;
}


/* ── THE OTHER THREE VIEWS ──────────────────────────────────────────
   Code, Design and Library were built before any of this and still
   wore the old shapes: square-ish cards, flat rows, no motion, a
   different rhythm from the conversation they sit beside. They are the
   same product, so they take the same tokens — the system face, the
   shape scale, the glass on anything that floats, the one spring — and
   stop looking like a different application the sidebar happens to
   open. */
.libwrap,.setwrap,#codewrap,.designwrap{max-width:860px;margin:0 auto;
  padding:34px 26px 64px}
.libwrap h2,.designwrap h2,#codewrap h2{font-family:var(--display);
  font-size:26px;font-weight:600;letter-spacing:-.025em;margin:0 0 6px}
.libwrap .hint,.designwrap .hint,#codewrap .hint,.pane .hint{
  font-size:13px;line-height:1.6;color:var(--dim);margin:0 0 24px;
  max-width:60ch}
/* the cards in every one of them */
.libcard,.designcard,.pane,.libmatch{
  background:linear-gradient(180deg,rgba(255,255,255,.04),
    rgba(255,255,255,.008) 46%,transparent 70%),rgba(23,22,26,.5);
  -webkit-backdrop-filter:blur(18px) saturate(1.35);
  backdrop-filter:blur(18px) saturate(1.35);
  border:1px solid rgba(255,255,255,.075);
  border-radius:var(--r-lg);padding:16px 18px;
  animation:springup .5s var(--spring) both;
  transition:transform var(--s-fast) var(--spring),
             border-color var(--s-fast) var(--ease-out)}
.libcard:hover,.designcard:hover{transform:translateY(-2px);
  border-color:rgba(255,255,255,.14)}
.libwrap input,.libwrap textarea,.designwrap input,
#codewrap input,.pane input,.pane textarea{
  border-radius:var(--r-sm);background:rgba(0,0,0,.26);
  border:1px solid rgba(255,255,255,.08);color:var(--tx);
  padding:9px 12px;font:inherit;font-size:13.5px}
.libwrap input:focus,.designwrap input:focus,.pane input:focus,
.pane textarea:focus{outline:none;border-color:rgba(217,119,87,.5);
  box-shadow:0 0 0 3px rgba(217,119,87,.12)}
.empty{display:flex;min-height:60vh;padding:48px 24px}
.empty .focal{margin:auto}

/* ── THE CODE WORKSPACE, REBUILT ────────────────────────────────────
   It read as a form with three buttons and two empty boxes because that
   is what it was. As a sheet it gets the same treatment as everything
   else: a quiet toolbar instead of a row of primaries, glass panes, a
   tree and an editor that fill their space, and an empty state that
   says what to do rather than sitting blank.
   WHAT CSS CANNOT DO HERE IS THE EDGE. Measured: a displacement map on
   `backdrop-filter` is ignored by this engine, so a magnifying, mirroring
   rim is not available to an HTML surface at all — only to a native
   view, which is why the window's own glass is an NSGlassEffectView.
   Everything below is the honest ceiling for a <div>. */
.pvw-stage.dark .codebar{display:flex;align-items:center;gap:8px;
  flex-wrap:wrap;padding:12px 14px;margin:0 0 12px;
  border-radius:var(--r-md);
  background:rgba(255,255,255,.035);
  border:1px solid rgba(255,255,255,.07)}
.pvw-stage.dark .codebar select,.pvw-stage.dark .codebar input{
  height:30px;border-radius:var(--r-sm);padding:0 10px;font-size:12.5px;
  background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.09);
  color:var(--tx)}
.pvw-stage.dark .codebar button{height:30px;padding:0 13px;
  border-radius:var(--r-sm);font-size:12.5px;font-weight:500;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.09);
  color:var(--dim);box-shadow:none}
.pvw-stage.dark .codebar button:hover{color:var(--tx);
  background:rgba(255,255,255,.09);transform:none;box-shadow:none}
.pvw-stage.dark .codebar button.primary{background:var(--acc);
  color:#fff;border:0}
.pvw-stage.dark .codebar button.primary:hover{background:var(--acc);
  filter:brightness(1.08)}
.pvw-stage.dark .codestat{font-size:11px;color:var(--lo);
  width:100%;margin-top:2px}
.pvw-stage.dark .ide{display:grid;grid-template-columns:230px 1fr;
  gap:12px;min-height:0}
.pvw-stage.dark .ide>*{min-height:0}      /* the grid-overflow trap */
.pvw-stage.dark .idetree,.pvw-stage.dark .ideedit{
  border-radius:var(--r-md);overflow:auto;
  background:rgba(255,255,255,.028);
  border:1px solid rgba(255,255,255,.06)}
.pvw-stage.dark .idetree{padding:8px}
.pvw-stage.dark .idehead{font-size:11px;color:var(--lo);
  padding:8px 12px;letter-spacing:.04em}
.pvw-stage.dark .ideedit textarea{width:100%;height:100%;border:0;
  background:transparent;color:var(--tx);font-family:var(--mono);
  font-size:12.5px;line-height:1.65;padding:12px 14px;resize:none}
.pvw-stage.dark .ideedit textarea:focus{outline:none}
.pvw-stage.dark .empty{min-height:220px;display:grid;place-items:center;
  color:var(--lo);font-size:13px}
@media (max-width:1000px){.pvw-stage.dark .ide{grid-template-columns:1fr}}

/* ── WHEN THE WINDOW ITSELF IS GLASS ────────────────────────────────
   Launched through the native shell, an NSGlassEffectView sits in the
   window BEHIND this page and the web view is told to stop drawing its
   own background. None of that is visible unless the PAGE also stops
   painting over it — an opaque body is an opaque body however good the
   material underneath is. So `?glass=1` hands the ground back to the
   system: the page paints only what it actually draws, and the real
   Apple material shows through everywhere else.
   This is the one honest way to get Liquid Glass into a web-rendered
   interface: not by imitating it in CSS, but by getting out of its way. */
html.native-glass,html.native-glass body{background:transparent!important}
html.native-glass aside{background:rgba(18,17,20,.42)}
html.native-glass aside::before{opacity:.5}
html.native-glass header{background:rgba(18,17,20,.30)}
html.native-glass .conv-dock{background:linear-gradient(to top,
  rgba(11,11,12,.55) 55%,transparent)}
html.native-glass .composer{background:rgba(16,15,17,.45)}
html.native-glass .run,html.native-glass .prow:hover{
  background:rgba(21,20,23,.45)}
html.native-glass .setwrap .card{background:rgba(23,22,26,.34)}
html.native-glass .dotf.hero{opacity:.38}

/* ── METAL ──────────────────────────────────────────────────────────
   The engine paints its own canvas and glow; these rules are only the
   furniture around it — the surfaces it wraps, and the two places
   Aethron puts it that metal-fx does not ship a shape for. */

/* SEARCH. The field itself is the ring, so the light runs around what
   you are typing into. */
.find{position:relative;margin:12px 14px 4px;display:flex;align-items:center}
/* THE ATTRIBUTE IS GONE BY THE TIME THE PAGE IS PAINTED. [data-ic] is a
   placeholder: the icon pass replaces the whole element with an <svg
   class="ic">, so a rule written against [data-ic] matches nothing and
   the magnifier stayed in flow — pushing the field 13px to the right of
   its own box. Style the thing that ends up in the document. */
.find > .ic,.find > [data-ic]{position:absolute;left:11px;color:var(--lo);
  pointer-events:none;z-index:2}
.find input{width:100%;height:34px;padding:0 44px 0 31px;border-radius:999px;
  border:1px solid rgba(255,255,255,.07);background:rgba(255,255,255,.035);
  color:var(--tx);font-size:12.5px;font-family:inherit;outline:0;
  transition:background .16s var(--ease),border-color .16s var(--ease)}
.find input::placeholder{color:var(--lo)}
.find input:focus{background:rgba(255,255,255,.055);
  border-color:rgba(255,255,255,.13)}
.find kbd{position:absolute;right:10px;z-index:2;font:500 10px/1 ui-monospace,
  SFMono-Regular,monospace;color:var(--lo);padding:3px 5px;border-radius:5px;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.06);
  pointer-events:none}
/* the wrapper the engine inserts has to stretch like the input did */
.find .metal-fx-root{width:100%;background:transparent;border-radius:999px}
.find .metal-fx-root .metal-fx-content{width:100%;display:block}
.find .metal-fx-root::after{box-shadow:none}
.findhits{padding:2px 8px 6px;display:flex;flex-direction:column;gap:2px}
.fhit{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:9px;
  cursor:pointer;background:none;border:0;color:var(--tx);font-size:12.5px;
  text-align:left;width:100%;box-shadow:none;transition:background .13s var(--ease)}
.fhit:hover,.fhit.on{background:rgba(255,255,255,.06);transform:none;
  box-shadow:none}
.fhit .fk{margin-left:auto;font-size:10.5px;color:var(--lo)}
.fhit .ic{color:var(--lo);flex:none}
.findnone{padding:10px 18px;font-size:12px;color:var(--lo)}

/* "LIVE MODE · NEW" — the arrival. The badge is metal-fx's own; the row
   around it is the part that says something just happened, so it moves
   once and then holds still. */
.livenew{display:inline-flex;align-items:center;gap:8px;
  opacity:0;transform:translateY(5px);
  transition:opacity .55s var(--ease),transform .55s var(--spring)}
.livenew.in{opacity:1;transform:none}
.livenew i{width:6px;height:6px;border-radius:50%;background:#79e39a;
  box-shadow:0 0 0 3px rgba(121,227,154,.14);flex:none}
.livenew b{font-weight:500;font-size:11.5px;color:var(--dim);letter-spacing:.01em}
.mbadge{display:inline-flex;line-height:0}
@media (prefers-reduced-motion:reduce){
  .livenew{transition:none;opacity:1;transform:none}}
/* on a project row the badge sits at the end and takes no height */
.pitem .livenew{margin-left:auto;align-self:center}

/* THE SEND BUTTON. The ring is the engine's; what belongs here is only
   making sure the wrapper does not change the layout the button had. */
.cbar .metal-fx-root{margin-left:auto;background:var(--acc);border-radius:11px}
.cbar .metal-fx-root .cbtn{margin-left:0}
.cbar .metal-fx-root::after{box-shadow:none}
.cbar .metal-fx-root .metal-fx-inner{display:none}
/* WHEN REAL GLASS IS BEHIND IT, THE SHEET PAINTS NOTHING. Its own blur
   was an imitation standing in front of the genuine article; the only
   thing left is the hairline that separates it from the conversation. */
html.native-glass .pvw.onglass{background:transparent;
  -webkit-backdrop-filter:none;backdrop-filter:none;
  box-shadow:inset 1px 0 0 rgba(255,255,255,.10)}
html.native-glass .pvw.onglass .pvw-bar{background:rgba(255,255,255,.05)}

/* ── THE SHAPE SCALE, APPLIED ───────────────────────────────────────
   iOS rounds considerably harder than web convention and keeps the
   family consistent: chrome is softest, inline content firmer, pills
   fully round. Applied through the tokens so the scale can be tuned in
   one place instead of chasing forty literals. Glass is spent only on
   things that FLOAT — a sheet, a popover, a card that sits above the
   page. Putting material on inline content is the commonest way this
   look goes wrong: everything turns to soup and nothing reads as
   raised. */
.composer{border-radius:var(--r-xl)}
.pcard,.setwrap .card,.selbox,.pvw-bar{border-radius:var(--r-lg)}
.run,.askbar,.pvw-stage{border-radius:var(--r-md)}
.cpill,.pvw-seg button,.selbox .send,.selbox .ghost{border-radius:var(--r-sm)}
.turn.you .bubble{border-radius:20px 20px 6px 20px}
.pill,.tag{border-radius:var(--r-pill)}
.setwrap .card{background:
   linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.008) 46%,
     transparent 70%),rgba(23,22,26,.5);
  -webkit-backdrop-filter:blur(18px) saturate(1.35);
  backdrop-filter:blur(18px) saturate(1.35);
  border:1px solid rgba(255,255,255,.075)}

/* ── AND THINGS ARRIVE, THEY DO NOT APPEAR ──────────────────────────
   The same spring that moves the sheet moves everything that enters,
   so a reply, a step and a card all land with one physics. */
@keyframes springup{
  from{opacity:0;transform:translateY(10px) scale(.985)}
  to{opacity:1;transform:none}}
.turn{animation:springup .5s var(--spring) both}
.run{animation:springup .46s var(--spring) both}
.step{animation:springup .4s var(--spring) both}
.pcard{animation:springup .55s var(--spring) both}
@media (prefers-reduced-motion:reduce){
  .turn,.run,.step,.pcard{animation:none!important}}

/* ── THE MOTION SYSTEM ──────────────────────────────────────────────
   iOS does not ease, it SPRINGS, and the tell is not the curve on one
   element — it is that everything shares the same one, so the whole
   interface feels like a single physical system rather than a set of
   parts each animating to its own taste. One curve, three durations.
   AND CONTROLS ANSWER THE FINGER. Every button on iOS scales down the
   instant you press and springs back when you let go; that single
   detail carries most of the "premium" feeling people attribute to the
   glass. It costs one rule. */
:root{
  --s-fast:.22s;--s-mid:.38s;--s-slow:.62s;
  --ease-out:cubic-bezier(.22,.9,.28,1)}
button,.qrow,.prow,.cpill,.step,.runsum,.pshot,input,textarea,select{
  transition-timing-function:var(--spring);
  transition-duration:var(--s-fast)}
button:active,.cpill:active,.qrow:active,.prow:active,.runsum:active{
  transform:scale(.965)}
.composer .cbtn:active{transform:scale(.9)}
.pshot:active{transform:scale(.985)}
/* a press should never fight a spring that is still settling */
button,.cpill,.qrow,.prow,.runsum,.pshot{will-change:transform}
@media (prefers-reduced-motion:reduce){
  button:active,.cpill:active,.qrow:active,.prow:active,
  .runsum:active,.pshot:active,.composer .cbtn:active{transform:none}}

/* ── AND IT GETS OUT OF THE WAY WHEN THERE IS NO ROOM ───────────────
   The ambient field belongs to a wide empty panel. Beside an open
   preview the column is ~480px of solid content, and a lit field
   behind it is not atmosphere, it is noise — which is exactly what the
   owner saw. It goes out when the sheet opens and fades down on any
   narrow window, well before the layout itself breaks. */

/* ── GLASS ──────────────────────────────────────────────────────────
   The owner asked for Apple's liquid glass, and the honest version of
   that in CSS is four things layered, not one blur:
     the PANE      a real backdrop blur with saturation, so what is
                   behind it bends and brightens rather than greying out
     THICKNESS     a lit rim along the top inner edge and a dark one
                   along the bottom, which is what makes a sheet read as
                   having depth instead of being a translucent rectangle
     the SHADOW    the object's own drop shadow, tight and low, so it
                   sits ON the page rather than in it
     the SPECULAR  one soft diagonal highlight
   What CSS cannot do is Apple's refraction — real edge bending needs a
   displacement map, and doing it with an SVG filter on the backdrop is
   unreliable across browsers today. This is the closest honest thing,
   and it is not a claim to have matched them. */
.glass{position:relative;
  background:
    linear-gradient(180deg,rgba(255,255,255,.055),
      rgba(255,255,255,.012) 38%,rgba(255,255,255,0) 62%),
    rgba(23,22,26,.58);
  -webkit-backdrop-filter:blur(22px) saturate(1.45);
  backdrop-filter:blur(22px) saturate(1.45);
  border:1px solid rgba(255,255,255,.085);border-radius:18px;
  box-shadow:0 18px 40px -18px rgba(0,0,0,.78),
             0 2px 10px -4px rgba(0,0,0,.5)}
.glass::before{content:"";position:absolute;inset:0;border-radius:inherit;
  pointer-events:none;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.14),
             inset 0 14px 26px -18px rgba(255,255,255,.22),
             inset 0 -1px 0 rgba(0,0,0,.5),
             inset 0 -16px 26px -18px rgba(0,0,0,.55)}
.glass::after{content:"";position:absolute;inset:0;border-radius:inherit;
  pointer-events:none;
  background:linear-gradient(115deg,transparent 30%,
    rgba(255,255,255,.06) 45%,transparent 58%)}

/* ── the project, as an object you can see ───────────────────────── */
.pcard{display:flex;gap:16px;padding:14px;margin:0 0 22px;
  animation:fadeup .45s cubic-bezier(.22,.9,.28,1) both}
.pshot{position:relative;flex:none;width:232px;height:146px;
  border-radius:11px;overflow:hidden;cursor:pointer;
  background:#0d0d0f;border:1px solid rgba(255,255,255,.07)}
.pshot iframe{position:absolute;top:0;left:0;width:1280px;height:800px;
  border:0;transform:scale(.1812);transform-origin:0 0;
  pointer-events:none;filter:saturate(.95)}
.pveil{position:absolute;inset:0;
  background:linear-gradient(180deg,transparent 55%,rgba(0,0,0,.45));
  transition:opacity .25s var(--ease)}
.pshot:hover .pveil{opacity:.4}
.pside{display:flex;flex-direction:column;gap:6px;min-width:0;
  justify-content:center}
.pname{font-size:16px;font-weight:590;letter-spacing:-.02em;color:var(--tx)}
.pmeta{display:flex;align-items:center;gap:7px;flex-wrap:wrap;
  font-size:11.5px;color:var(--lo)}
.pacts{display:flex;gap:7px;margin-top:8px;flex-wrap:wrap}
@media (max-width:760px){.pcard{flex-direction:column}
  .pshot{width:100%;height:170px}
  .pshot iframe{transform:scale(.34)}}

/* ── THE PREVIEW, INSIDE AETHRON ───────────────────────────────────
   Point-and-edit belongs in the product, not in a browser tab: the
   moment you send someone out to Chrome you have lost the selection,
   the conversation and the undo history. So the built site is shown
   here, on a glass sheet over the conversation, and the picker that
   already exists is armed on it. */
/* ── IT TAKES THE RIGHT TWO THIRDS, NOT THE WHOLE SCREEN ───────────
   Covering everything meant losing the conversation the moment you
   wanted to look at the page — which is exactly when you want to say
   something about it. The sheet is a right-hand panel; the chat keeps
   the left third and stays live, so you can watch a change land while
   you are still talking about it.
   THE MOTION IS A SPRING, NOT AN EASE. `linear()` samples a real
   spring curve, so it arrives with the slight overshoot-and-settle iOS
   has rather than the flat decelerate of a cubic-bezier. Both sides run
   the SAME curve and duration, so the panel and the column that yields
   to it move as one object rather than two things that happen to be
   animating. */
:root{--spring:linear(0,.006,.025 2.8%,.101 6.1%,.539 18.9%,.721 25.3%,
  .849 31.5%,.937 38.1%,.968 41.8%,.991 45.7%,1.006 50.1%,1.015 55%,
  1.012 72.5%,1);
  /* two thirds of the room LEFT OF IT, not of the whole window — the
     sidebar is 264px and taking 66vw of everything left the chat a
     sliver once you subtracted it. */
  --pvw-w:calc((100vw - 264px) * .64)}
.pvw{position:fixed;top:0;right:0;bottom:0;width:var(--pvw-w);
  z-index:600;display:flex;flex-direction:column;
  padding:16px 16px 14px;gap:11px;
  background:rgba(8,8,9,.55);
  -webkit-backdrop-filter:blur(26px) saturate(1.3);
  backdrop-filter:blur(26px) saturate(1.3);
  box-shadow:-24px 0 60px -30px rgba(0,0,0,.9),
             inset 1px 0 0 rgba(255,255,255,.07);
  transform:translateX(100%);
  transition:transform .62s var(--spring)}
.pvw.in{transform:translateX(0)}
/* the conversation yields rather than disappears */
body.pvwopen main{margin-right:var(--pvw-w);
  transition:margin-right .62s var(--spring)}
body main{transition:margin-right .62s var(--spring)}
/* the column is whatever is left, not a fixed 560 — at 1600 the room
   beside the sheet is ~481px, so a 560 wrap clipped the card's last
   button off the edge. */
body.pvwopen .conv-wrap{max-width:100%}
/* the column is narrower now, so the opening line has to stop shouting */
body.pvwopen #welcome h1{font-size:25px;line-height:1.2}
body.pvwopen #welcome .sub{font-size:13px;max-width:46ch}
body.pvwopen .pacts{gap:6px}
body.pvwopen .pacts .cpill{flex:1 1 auto;justify-content:center}
body.pvwopen .pcard{flex-direction:column}
body.pvwopen .pshot{width:100%;height:150px}
body.pvwopen .pshot iframe{transform:scale(.28)}
@media (max-width:1100px){:root{--pvw-w:100vw}
  body.pvwopen main{margin-right:0}}
@media (prefers-reduced-motion:reduce){
  .pvw,body main,body.pvwopen main{transition:none}}
.pvw-bar{display:flex;align-items:center;gap:10px;flex:none;
  padding:9px 12px;border-radius:14px}
.pvw-name{font-size:13.5px;font-weight:590;letter-spacing:-.01em;
  color:var(--tx)}
.pvw-name span{color:var(--lo);font-weight:400;margin-left:8px;
  font-size:11.5px}
.pvw-seg{display:flex;gap:3px;padding:3px;border-radius:11px;
  background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.06)}
.pvw-seg button{height:25px;padding:0 12px;font-size:11.5px;border:0;
  border-radius:8px;background:none;color:var(--dim);box-shadow:none;
  font-weight:500}
.pvw-seg button:hover{color:var(--tx);background:rgba(255,255,255,.05);
  transform:none;box-shadow:none}
.pvw-seg button.on{background:var(--acc);color:#fff;box-shadow:none}
.pvw-seg button.on:hover{background:var(--acc);color:#fff}
.pvw-bar .sp{margin-left:auto}
.pvw-stage{flex:1;min-height:0;border-radius:var(--r-md);overflow:hidden;
  position:relative;background:#fff}
/* a sheet showing OUR interface, not someone's site, keeps our ground */
.pvw-stage.dark{background:rgba(14,13,16,.55);overflow:auto;
  -webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px);
  border:1px solid rgba(255,255,255,.07)}
.pvw-stage.dark .pane,.pvw-stage.dark .empty{margin:0}
.pvw-stage iframe{width:100%;height:100%;border:0;display:block}
.pvw-hint{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);
  font-size:11.5px;color:var(--tx);padding:7px 14px;border-radius:99px;
  background:rgba(16,15,17,.82);border:1px solid rgba(255,255,255,.09);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  pointer-events:none;animation:fadeup .4s var(--ease) both}

/* the ring that follows you around the page. It springs between
   elements, breathes while it waits, and never sits on a hard corner —
   a dashed 2px outline is a debugging tool, not a design. */
.selring{position:absolute;pointer-events:none;border-radius:9px;
  opacity:0;transform:scale(.97);
  border:1.5px solid var(--acc2);
  box-shadow:0 0 0 1px rgba(217,119,87,.28),
             0 0 22px -4px rgba(217,119,87,.55),
             inset 0 0 14px -6px rgba(217,119,87,.5);
  transition:left .34s var(--spring),top .34s var(--spring),
             width .34s var(--spring),height .34s var(--spring),
             opacity .18s ease,transform .3s var(--spring)}
.selring.on{opacity:1;transform:scale(1);animation:ringbreath 2.4s ease-in-out infinite}
@keyframes ringbreath{
  0%,100%{box-shadow:0 0 0 1px rgba(217,119,87,.28),
    0 0 22px -4px rgba(217,119,87,.55),inset 0 0 14px -6px rgba(217,119,87,.5)}
  50%{box-shadow:0 0 0 1px rgba(217,119,87,.42),
    0 0 34px -2px rgba(217,119,87,.75),inset 0 0 18px -6px rgba(217,119,87,.7)}}
@media (prefers-reduced-motion:reduce){
  .selring{transition:opacity .15s ease;animation:none}
  .selring.on{animation:none}}

/* ── SAY IT IN YOUR OWN WORDS, TO ONE THING ────────────────────────
   The old path was a command: pick an element, choose a property, type
   a value. This is a sentence about a specific element — the selector
   is carried for you, so the words can be ordinary ones. */
.selbox{position:fixed;z-index:620;width:340px;max-width:calc(100vw - 32px);
  padding:12px;border-radius:18px;transform-origin:50% 0;
  animation:selpop .44s var(--spring) both}
@keyframes selpop{
  from{opacity:0;transform:translateY(-8px) scale(.92)}
  to{opacity:1;transform:none}}
.selbox textarea,.selbox .row{position:relative;z-index:1}
@media (prefers-reduced-motion:reduce){.selbox{animation:none}}
.selbox .what{display:flex;align-items:center;gap:7px;font-size:11px;
  color:var(--lo);margin-bottom:9px;letter-spacing:.02em}
.selbox .what b{color:var(--acc2);font-weight:500;
  max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.selbox textarea{width:100%;min-height:58px;max-height:150px;resize:none;
  background:rgba(0,0,0,.28);border:1px solid rgba(255,255,255,.08);
  border-radius:11px;padding:9px 11px;color:var(--tx);font:inherit;
  font-size:13px;line-height:1.5}
.selbox textarea:focus{outline:none;border-color:rgba(217,119,87,.5);
  box-shadow:0 0 0 3px rgba(217,119,87,.12)}
.selbox .row{display:flex;align-items:center;gap:8px;margin-top:9px}
.selbox .row .sp{margin-left:auto}
.selbox .send{height:28px;padding:0 13px;border-radius:9px;border:0;
  background:var(--acc);color:#fff;font-size:12px;font-weight:500;
  box-shadow:none}
.selbox .send:hover{background:var(--acc);filter:brightness(1.08);
  transform:none;box-shadow:none}
.selbox .ghost{height:28px;padding:0 11px;border-radius:9px;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);
  color:var(--tx);font-size:12px;box-shadow:none}
.selbox .ghost:hover{background:rgba(255,255,255,.09);transform:none;
  box-shadow:none;color:var(--tx)}

/* Aethron's half of the exchange BEFORE anything runs: the part only
   the person can supply, asked in the dock rather than by starting the
   work and finding out. */
.askbar{font-size:12.5px;line-height:1.55;color:var(--dim);
  padding:9px 13px;margin-bottom:8px;border-radius:12px;
  background:rgba(217,119,87,.07);border:1px solid rgba(217,119,87,.16);
  animation:fadeup .3s cubic-bezier(.22,.9,.28,1) both}

/* ── THE CHAT BOX ───────────────────────────────────────────────────
   One row with a button on the end is a search field, not the front
   door of a product that can migrate a site, port a framework or write
   code. It is a real composer now: what you type on top, and the
   controls that decide HOW it runs underneath, inside the same box. */
.composer{display:block;padding:13px 13px 10px;border-radius:18px;
  align-items:stretch;gap:0}
.composer > [data-beam-bloom]{position:absolute;inset:0;border-radius:inherit;
  pointer-events:none}
.composer textarea{width:100%;min-height:24px;max-height:210px;padding:2px 2px 0;
  font-size:14.5px;line-height:1.55}
.cbar{display:flex;align-items:center;gap:7px;margin-top:9px;
  position:relative;z-index:3}
.cpill{display:inline-flex;align-items:center;justify-content:center;
  height:27px;padding:0 10px;font-size:11.5px;font-weight:500;cursor:pointer;
  border-radius:9px;background:rgba(255,255,255,.035);
  border:1px solid rgba(255,255,255,.07);color:var(--dim);
  box-shadow:none;white-space:nowrap;gap:6px;
  transition:color .16s var(--ease),background .16s var(--ease)}
.cpill:hover{color:var(--tx);background:rgba(255,255,255,.07);
  border-color:rgba(255,255,255,.12);transform:none;box-shadow:none}
.cpill .cv{color:var(--lo);font-weight:400}
.cbar .cbtn{margin-left:auto;width:30px;height:30px;border-radius:9px}
.composer .cbtn .ic,.composer .cbtn [data-ic]{transform:none}
@media (max-width:640px){.cpill.opt{display:none}}

/* ── A FINISHED RUN FOLDS AWAY ──────────────────────────────────────
   Every turn used to leave its whole step list open forever, so three
   requests meant three stacked logs and the reply you actually wanted
   was somewhere below them. A run that is still going stays open,
   because that is the thing you are watching; a finished one collapses
   to one line you can open again. */
.runsum{display:flex;align-items:center;gap:10px;padding:10px 13px;
  width:100%;background:none;border:0;border-radius:0;box-shadow:none;
  text-align:left;font-size:12.5px;color:var(--dim);cursor:pointer;
  font-weight:400;transition:background var(--dur,180ms) var(--ease)}
.runsum:hover{background:var(--panel2);transform:none;box-shadow:none;
  color:var(--tx);border-color:transparent}
.runsum .rs-n{color:var(--tx);font-weight:500}
.runsum .rs-bad{color:var(--err)}
.runsum .rs-c{margin-left:auto;color:var(--lo);font-size:11px;
  display:inline-flex;align-items:center;gap:6px}
.runsum .ic{transition:transform .22s var(--ease)}
.run.open .runsum .ic{transform:rotate(180deg)}
.composer textarea{flex:1;min-height:26px;max-height:190px;padding:7px 0;
  font:inherit;font-size:14.5px;line-height:1.5;resize:none;overflow:auto;
  background:transparent;border:0;color:var(--tx)}
.composer textarea:focus{outline:none}
.composer .cbtn{width:34px;height:34px;flex:none;padding:0;border-radius:11px;
  display:grid;place-items:center;border:0;cursor:pointer;
  background:var(--acc);color:#fff}
.composer .cbtn[disabled]{opacity:.35;cursor:default}
.composer .cbtn [data-ic]{transform:rotate(90deg)}
.composer .cbtn.stop{background:var(--panel2);color:var(--tx);
  border:1px solid var(--line2)}
.composer .cbtn.stop [data-ic]{transform:none}
.dockmeta{display:flex;align-items:center;gap:10px;justify-content:center;
  font-size:11px;color:var(--lo);margin-top:8px;min-height:14px}
.dockmeta .dot{width:3px;height:3px;border-radius:50%;background:var(--lo)}

/* what it SAYS */
.clog{display:flex;flex-direction:column}
/* A FLEX ITEM SHRINKS UNLESS TOLD NOT TO, and a run card that shrinks
   still lays its steps out at full size — they simply fall outside the
   box and `overflow:hidden` eats them. Measured: seven steps reporting
   tops 429…696 while only 95px of card ever painted, so five of them
   were invisible while every DOM check said they were fine. This is the
   same family as the `.ide>*{min-height:0}` bug already in this file. */
.clog>*{flex:0 0 auto}
.turn{margin:2px 0 16px}
.turn.you{display:flex;justify-content:flex-end}
.turn.you .bubble{background:var(--panel2);border:1px solid var(--line);
  border-radius:15px 15px 5px 15px;padding:10px 14px;max-width:84%;
  font-size:14px;line-height:1.55;color:var(--tx);white-space:pre-wrap;
  word-break:break-word}
.turn.bot .prose{font-size:14.5px;line-height:1.72;color:var(--tx);
  white-space:pre-wrap;word-break:break-word}
.turn.bot .prose>*:first-child{margin-top:0}
.turn.bot .prose>*:last-child{margin-bottom:0}
.turn.bot .prose p{margin:0 0 .82em}
.turn.bot .prose h2,.turn.bot .prose h3,.turn.bot .prose h4,
.turn.bot .prose h5,.turn.bot .prose h6{
  margin:1.5em 0 .5em;font-weight:650;letter-spacing:-.011em;
  line-height:1.3;color:var(--tx)}
.turn.bot .prose h2{font-size:17.5px}
.turn.bot .prose h3{font-size:15.5px}
.turn.bot .prose h4,.turn.bot .prose h5,.turn.bot .prose h6{
  font-size:12.5px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--dim);font-weight:600}
.turn.bot .prose ul,.turn.bot .prose ol{margin:0 0 .82em;padding-left:1.15em}
.turn.bot .prose li{margin:.24em 0}
.turn.bot .prose ul{list-style:none;padding-left:.1em}
.turn.bot .prose ul>li{position:relative;padding-left:1.05em}
/* a dot in the product's own colour, not a browser bullet */
.turn.bot .prose ul>li::before{content:"";position:absolute;left:.12em;
  top:.66em;width:4px;height:4px;border-radius:50%;
  background:var(--acc);opacity:.55}
.turn.bot .prose ol>li::marker{color:var(--lo);font-variant-numeric:tabular-nums}
.turn.bot .prose hr{border:0;height:1px;margin:1.5em 0;
  background:linear-gradient(to right,var(--line2),transparent)}
.turn.bot .prose blockquote{margin:0 0 .82em;padding:.1em 0 .1em .9em;
  border-left:2px solid var(--line2);color:var(--dim)}
.turn.bot .prose a{color:var(--acc);text-decoration:none;
  border-bottom:1px solid rgba(217,119,87,.35)}
.turn.bot .prose a:hover{border-bottom-color:var(--acc)}
.turn.bot .prose b{font-weight:640;color:#fff}
.turn.bot .prose pre.cb{margin:0 0 .9em;padding:12px 14px;overflow-x:auto;
  border-radius:12px;background:rgba(255,255,255,.035);
  border:1px solid rgba(255,255,255,.06);font-size:12.5px;line-height:1.6}
.turn.bot .prose pre.cb code{background:none;border:0;padding:0;font-size:inherit}
.turn.bot .prose code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:12.5px;background:var(--panel2);padding:1px 5px;border-radius:5px}
.turn.sys{font-size:11.5px;color:var(--lo);text-align:center;margin:12px 0}
/* a warning a person can read, with the log where it belongs: available,
   not shouted. Left-aligned and boxed because it is a thing that
   happened, not a passing note. */
.turn.sys.bad{max-width:560px;margin:14px auto;text-align:left;
  font-size:13px;line-height:1.6;color:var(--tx);
  padding:12px 14px;border-radius:var(--r-md);
  background:rgba(229,105,94,.07);border:1px solid rgba(229,105,94,.22)}
.errdet{margin-top:9px}
.errdet summary{font-size:11.5px;color:var(--lo);cursor:pointer;
  list-style:none}
.errdet summary::-webkit-details-marker{display:none}
.errdet summary:hover{color:var(--dim)}
.errdet div{margin-top:7px;font-family:var(--mono);font-size:11px;
  line-height:1.55;color:var(--dim);white-space:pre-wrap;
  max-height:220px;overflow:auto;word-break:break-word}
.turn.sys.bad{color:var(--err)}

/* thinking — present, never shouting */
.think{margin:2px 0 14px}
.think summary{font-size:12px;color:var(--lo);cursor:pointer;list-style:none;
  display:inline-flex;align-items:center;gap:6px}
.think summary::-webkit-details-marker{display:none}
.think summary:hover{color:var(--dim)}
.think .body{font-size:12.5px;line-height:1.65;color:var(--dim);
  white-space:pre-wrap;margin-top:8px;padding-left:12px;
  border-left:1px solid var(--line)}

/* what it DOES */
.run{border:1px solid var(--line);border-radius:13px;background:var(--panel);
  margin:2px 0 16px;overflow:hidden}
/* A ROW IS NOT A BUTTON-SHAPED THING. It IS a <button>, so it inherits
   the global button rule — which rounds it 9px, gives it an inset
   shadow, and LIFTS IT 1px on hover. Stacked flush inside a card with
   `overflow:hidden`, that reads as little boxes that twitch under the
   pointer. Resetting `border` alone was not enough: every property the
   global rule sets has to be answered here. */
.step{display:flex;align-items:center;gap:10px;padding:9px 13px;
  font-size:12.5px;width:100%;background:none;border:0;text-align:left;
  border-radius:0;box-shadow:none;font-weight:400;
  color:var(--dim);cursor:pointer;
  transition:background var(--dur,180ms) var(--ease)}
.step:hover{background:var(--panel2);transform:none;box-shadow:none;
  color:var(--dim);border-color:transparent}
.step+.step,.stepdet+.step{border-top:1px solid var(--line)}
.step .sdot{width:6px;height:6px;border-radius:50%;flex:none;
  background:var(--line2)}
.step.ok .sdot{background:#5c9a6b}
.step.bad .sdot{background:var(--err)}
.step.live .sdot{background:var(--acc);animation:beat 1.1s ease-in-out infinite}
@keyframes beat{0%,100%{opacity:1;transform:scale(1)}
  50%{opacity:.45;transform:scale(.72)}}
.step .sv{color:var(--tx);font-weight:500;flex:none}
.step .st{color:var(--dim);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  min-width:0;flex:1}
.step .sx{flex:none;color:var(--lo);font-size:11px}
.step.bad .sv{color:var(--err)}
.stepdet{padding:0 13px 11px 29px;font-size:11.5px;line-height:1.6;
  color:var(--dim);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto}
.stepdet.bad{color:var(--err)}

/* ══ MOTION ════════════════════════════════════════════════════════
   Motion here is never decoration: every one of these says something
   that would otherwise need a word. A step SLIDES IN because it just
   happened. A reply FADES UP because it arrived. The send button
   SINKS because you pressed it. Nothing loops for its own sake, and
   the whole layer switches off under prefers-reduced-motion — a
   captivating interface that gives somebody a migraine is not one. */
@keyframes rise{from{opacity:0;transform:translateY(7px)}
  to{opacity:1;transform:none}}
@keyframes fadeup{from{opacity:0;transform:translateY(10px)}
  to{opacity:1;transform:none}}
@keyframes sweep{from{background-position:-160% 0}
  to{background-position:260% 0}}
.turn{animation:fadeup .42s cubic-bezier(.22,.9,.28,1) both}
.run{animation:fadeup .38s cubic-bezier(.22,.9,.28,1) both}
/* each step lands after the one above it — the cascade IS the story */
.step{animation:rise .34s cubic-bezier(.22,.9,.28,1) both}
.step:nth-child(1){animation-delay:.00s}.step:nth-child(3){animation-delay:.03s}
.step:nth-child(5){animation-delay:.06s}.step:nth-child(7){animation-delay:.09s}
.step:nth-child(9){animation-delay:.12s}.step:nth-child(11){animation-delay:.15s}
.step:nth-child(13){animation-delay:.18s}
/* the one still running is lit by a slow sheen crossing its row */
.step.live{background-image:linear-gradient(100deg,transparent 38%,
  rgba(217,119,87,.10) 50%,transparent 62%);background-size:220% 100%;
  animation:rise .34s cubic-bezier(.22,.9,.28,1) both,
            sweep 2.1s linear infinite}
.composer .cbtn{transition:transform .14s cubic-bezier(.22,.9,.28,1),
  background .18s var(--ease),filter .18s var(--ease)}
.composer .cbtn:hover{filter:brightness(1.08)}
.composer .cbtn:active{transform:scale(.92)}
.qrow{transition:color var(--dur,180ms) var(--ease),
  background var(--dur,180ms) var(--ease),
  padding-left var(--dur,180ms) var(--ease)}
.qrow:hover{padding-left:16px}
.stepdet{animation:rise .2s ease both}
@media (prefers-reduced-motion:reduce){
  .turn,.run,.step,.stepdet{animation:none!important}
  .step.live{background-image:none}
}

/* ══ THE DOT FIELD ═════════════════════════════════════════════════
   The signature. One component, three jobs:
     IDLE     a slow shimmer behind the opening words, so a window with
              nothing in it still feels switched on rather than dead
     WORKING  a wave travelling through the grid while a turn runs —
              motion that MEANS something is happening, not a spinner
              that means nothing
     FILLING  the same grid lit left-to-right by real progress
   It is a canvas because a few hundred dots repainting at 60fps is
   free there and ruinous in the DOM. It stops when the tab is hidden,
   stops when it scrolls out of view, and never starts at all under
   prefers-reduced-motion — liveliness is not worth a battery or a
   headache. */
.dotf{display:block}
.dotf.inline{width:74px;height:16px;flex:none}
/* the thought-orb: a discrete object beside the verb, where the hero
   field is the light in the whole room. Different jobs, same ramp. */
.orb{display:block;flex:none}
.nowline .orb{margin:0 1px}
/* THE AMBIENT FIELD belongs to the whole panel, not to one paragraph.
   As a band behind the headline it read as a smudge; across the panel it
   reads as a room with the lights on. It is strongest when there is
   nothing to read and recedes the moment there is — liveliness must
   never compete with the thing the person came for. */
.dotf.hero{position:absolute;inset:0;width:100%;height:100%;z-index:0;
  pointer-events:none;opacity:1;
  transition:opacity 1.1s cubic-bezier(.4,0,.2,1);
  /* A HALO, NOT A WASH. The first mask was brightest at the centre —
     which is exactly where the words are, so the crests ran straight
     through the headline and made it harder to read. The field belongs
     AROUND the content: clear where the column sits, strongest in a
     ring outside it, gone again at the far edges so it never hits a
     hard border. */
  /* The light shapes itself now, so the mask does one job only: keep the
     column of words clear, and fade at the very edge so the field never
     meets a hard border. */
  /* ONE LAYER. Two layers plus mask-composite silently produced no mask
     at all and the field ran straight over every word on the page.
     A single gradient does the whole job: clear where the column sits,
     opaque outside it. */
  /* MEASURED, NOT GUESSED: the column of words occupies about 58% of the
     panel's width and 55% of its height, so transparency has to HOLD that
     far out before the field is allowed to appear at all. Twice I set a
     clear zone smaller than the thing it was meant to clear. */
  /* AND IT FADES BACK OUT. The comment above already said the field
     should be "gone again at the far edges so it never meets a hard
     border" — the gradient did the opposite, running to solid #000 at
     100%, so the field was BRIGHTEST exactly where the panel clips it
     and every render ended in a hard rectangular cut at the top-left
     and bottom-right. The ramp now rises through the ring and falls
     away again before the edge, which is what a halo is. */
  /* THE CLEAR ZONE WAS A CIRCLE, AND PEOPLE COULD SEE IT. A radial mask
     cuts a literal disc out of the field, so the light can never enter
     it and what a person reads is a big black circle with dots orbiting
     outside — the owner described exactly that without knowing it was
     the mask. The thing being kept clear is not a disc, it is a COLUMN
     of words: tall, narrow, rectangular. So the mask is horizontal now.
     The field lives in the margins either side, fades out before it
     meets the window edge, and there is no circle anywhere in it. */
  -webkit-mask-image:linear-gradient(to right,
     transparent 0,rgba(0,0,0,.45) 3%,#000 7%,#000 10%,
     rgba(0,0,0,.28) 13%,transparent 17%,transparent 83%,
     rgba(0,0,0,.28) 87%,#000 90%,#000 93%,rgba(0,0,0,.45) 97%,
     transparent 100%);
  mask-image:linear-gradient(to right,
     transparent 0,rgba(0,0,0,.45) 3%,#000 7%,#000 10%,
     rgba(0,0,0,.28) 13%,transparent 17%,transparent 83%,
     rgba(0,0,0,.28) 87%,#000 90%,#000 93%,rgba(0,0,0,.45) 97%,
     transparent 100%)}
/* OFF, NOT DIMMED. At .30 the field still ran behind the reply and the
   run card and made them harder to read — the owner watched it happen.
   The rule this file already states is that liveliness must never
   compete with the thing the person came for, and a conversation IS
   that thing. It lights an empty room; the moment there is something
   to read it goes out entirely, and comes back on a new project. */
.conv-shell.talking .dotf.hero{opacity:0}
/* AND IT LIVES HERE, AFTER THE BASE RULE — not three hundred lines
   earlier, where `.dotf.hero{opacity:1}` simply overrode it at equal
   specificity and the field kept running behind the text on every
   narrow window. Same weight means the later rule wins; the fix is
   position, not `!important`.
   MEASURED, NOT DIMMED: the mask keeps a clear COLUMN and the field
   lives in the margins either side, so once the column fills the panel
   there are no margins and fading only puts faint dots on the words
   instead of bright ones. The panel is the window minus a 264px
   sidebar and the column is 760px, so the margins run out near 1280. */
body.pvwopen .dotf.hero{opacity:0}
@media (max-width:1440px){.dotf.hero{opacity:.6}}
@media (max-width:1280px){.dotf.hero{opacity:0}}
.conv-shell{position:relative}
.conv-scroll,.conv-dock{position:relative;z-index:1}
.herowrap{position:relative}

/* the live line — what is happening RIGHT NOW */
.nowline{display:flex;align-items:center;gap:11px;padding:11px 2px;
  font-size:13px;color:var(--dim)}
.nowline .sv{color:var(--tx);font-weight:500}
.nowline .ac{margin-left:auto;font-size:11px;color:var(--lo)}

/* HIDDEN MEANS HIDDEN. The browser's own `[hidden]{display:none}` is a
   UA rule of the same specificity as any class, so every author rule
   like `.qrows{display:flex}` silently beat it: elements were marked
   hidden, reported hidden, and still took up their full height — which
   is exactly what kept pushing the composer down the window. */
[hidden]{display:none!important}

/* settings — the PANE owns the scroll, so the last card is reachable */
#content.set-host{flex:1;min-height:0;overflow-y:auto;display:block}
.setwrap{max-width:660px;margin:0 auto;padding:34px 24px 60px}
.seth{font-size:26px;font-family:var(--display);letter-spacing:-.02em;font-weight:600;color:var(--tx);
  margin:0 0 20px}
.setwrap .card{margin-bottom:16px}
.setwrap .card h3{display:flex;align-items:center;gap:8px}
.kv{display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding:9px 0;border-bottom:1px solid var(--line);font-size:13px;
  color:var(--dim)}
.kv:last-of-type{border-bottom:0}
.kv b{color:var(--tx);font-weight:500}
.rowbtns{display:flex;gap:8px;align-items:center;margin-top:12px}
.meter{height:5px;border-radius:3px;background:var(--panel2);overflow:hidden;
  margin:10px 0 2px}
.meter i{display:block;height:100%;background:var(--acc)}

.startrow textarea{flex:1;min-height:44px;max-height:180px;padding:12px 14px;
  font:inherit;font-size:15px;line-height:1.45;resize:none;overflow:auto;
  background:var(--field);border:1px solid var(--line2);border-radius:10px;
  color:var(--tx)}
.startrow textarea:focus{border-color:var(--tx);outline:none;
  box-shadow:0 0 0 3px rgba(236,234,240,.10)}
.startrow{align-items:flex-end}

/* ══ ATMOSPHERE ══════════════════════════════════════════════════════
   The flat version read as "dark grey rectangles" because nothing in it
   was LIT. Three things fix that, and none of them is colour:
     1. an ambient light layer — slow radial blooms behind everything,
        one of them terracotta, so the brand is the light source in the
        room rather than paint on a button;
     2. film grain — 2% noise, which is the difference between a surface
        and a fill;
     3. edge light — a 1px top highlight on raised surfaces, the way a
        real material catches light from above.
   All of it sits behind pointer-events:none and dies under
   prefers-reduced-motion.                                            */
body{background:#08080a;position:relative}
body::before{content:"";position:fixed;inset:-20%;z-index:0;
  pointer-events:none;
  background:
    radial-gradient(38% 30% at 22% 18%, rgba(217,119,87,.16), transparent 70%),
    radial-gradient(34% 28% at 78% 34%, rgba(96,116,168,.13), transparent 70%),
    radial-gradient(45% 38% at 55% 88%, rgba(217,119,87,.09), transparent 72%);
  filter:blur(20px);
  animation:drift 34s cubic-bezier(.45,0,.55,1) infinite alternate}
@keyframes drift{
  0%{transform:translate3d(0,0,0) scale(1)}
  50%{transform:translate3d(2.5%,-2%,0) scale(1.06)}
  100%{transform:translate3d(-2%,2.5%,0) scale(1.02)}}
body::after{content:"";position:fixed;inset:0;z-index:1;
  pointer-events:none;opacity:.028;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)'/%3E%3C/svg%3E")}
aside,main,.tourbox{position:relative;z-index:2}

/* ══ GLASS ═══════════════════════════════════════════════════════════ */
aside{background:rgba(17,16,19,.62);backdrop-filter:blur(26px) saturate(1.5);
  -webkit-backdrop-filter:blur(26px) saturate(1.5);
  border-right:1px solid rgba(255,255,255,.055)}
header{background:rgba(11,11,12,.5);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid rgba(255,255,255,.05)}

/* ══ THE FOCAL LINE ══════════════════════════════════════════════════
   Optically-tightened display type with a top-lit gradient fill: bright
   at the cap line, settling to warm grey at the baseline. It reads as
   lit from above, matching the ambient layer.                        */
.focal h1,#welcome h1{font-size:40px;line-height:1.1;letter-spacing:-.035em;
  font-weight:600;margin:0 0 14px;
  background:linear-gradient(176deg,#fff 8%,#e6e2df 45%,#a8a29d 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  -webkit-text-fill-color:transparent}
.focal .sub,#welcome .sub{font-size:14px;line-height:1.65;color:#8b8792;
  margin:0 0 36px;max-width:50ch;letter-spacing:-.005em}

/* ══ THE COMPOSER — the one lit object ═══════════════════════════════ */
.startrow{position:relative;align-items:flex-end;gap:10px}
.startrow::before{content:"";position:absolute;inset:-16px -20px;
  border-radius:26px;pointer-events:none;opacity:0;
  background:radial-gradient(58% 130% at 50% 50%,
    rgba(217,119,87,.20),transparent 72%);
  filter:blur(14px);transition:opacity .5s var(--e,cubic-bezier(.16,1,.3,1))}
.startrow:focus-within::before{opacity:1}
.startrow textarea{background:rgba(23,22,26,.78);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  border:1px solid rgba(255,255,255,.09);border-radius:14px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.06),
             0 12px 32px -16px rgba(0,0,0,.9);
  transition:border-color .3s cubic-bezier(.16,1,.3,1),
             box-shadow .3s cubic-bezier(.16,1,.3,1)}
.startrow textarea:focus{border-color:rgba(217,119,87,.42);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.09),
             0 0 0 4px rgba(217,119,87,.10),
             0 12px 36px -14px rgba(0,0,0,.95)}
.startrow button.primary{border-radius:13px;
  background:linear-gradient(178deg,#e2865f,#cf6a49);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.26),
             0 6px 20px -8px rgba(217,119,87,.6);
  transition:transform .22s cubic-bezier(.16,1,.3,1),
             box-shadow .22s cubic-bezier(.16,1,.3,1)}
.startrow button.primary:hover{transform:translateY(-1px);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.32),
             0 10px 26px -8px rgba(217,119,87,.75)}
.startrow button.primary:active{transform:translateY(0) scale(.97)}

/* ══ CONVERSATION ════════════════════════════════════════════════════ */
.msg{animation:rise .42s cubic-bezier(.16,1,.3,1) both}
@keyframes rise{from{opacity:0;transform:translateY(9px)}
                to{opacity:1;transform:none}}
.msg.you{background:linear-gradient(176deg,rgba(217,119,87,.20),
  rgba(217,119,87,.11));border:1px solid rgba(217,119,87,.24);
  border-radius:14px 14px 4px 14px;color:#f4efec;
  align-self:flex-end;max-width:82%;padding:11px 15px}
.msg.bot{background:rgba(255,255,255,.032);
  border:1px solid rgba(255,255,255,.055);
  border-radius:14px 14px 14px 4px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.045);
  max-width:88%;padding:13px 16px;line-height:1.62}
.msg.sys{background:none;border:0;color:#6f6b76;font-size:11.5px;
  text-align:center;letter-spacing:.02em}

/* ══ SIDEBAR — a project is a row, not a card ════════════════════════ */
.pitem{border:0;border-radius:10px;padding:10px 12px;
  transition:background .22s cubic-bezier(.16,1,.3,1)}
.pitem:hover{background:rgba(255,255,255,.038)}
.pitem.sel{background:rgba(255,255,255,.062);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.07)}
.pitem.sel .pname{color:#fff}
.plat{font-size:9.5px;letter-spacing:.1em;padding:2px 6px;border-radius:5px;
  background:rgba(255,255,255,.055);color:#807c88;border:0}
.pbar{height:2px;border-radius:2px;background:rgba(255,255,255,.06)}
.pbar i{background:rgba(217,119,87,.55)}

.qrow{border-top:1px solid rgba(255,255,255,.05);border-radius:0}
.qrow:hover{background:rgba(255,255,255,.035)}

@media (prefers-reduced-motion:reduce){
  body::before{animation:none}
  .msg{animation:none}}

/* ══ THE INSPECTOR DRAWER ════════════════════════════════════════════
   Six tabs said "you are on page 2 of 6". One button plus a chip row
   says "the forms are here if you want them". Same reach, no navigation. */
nav#tabs{display:flex;align-items:center;gap:10px;padding:0 22px;
  border-bottom:1px solid rgba(255,255,255,.05);min-height:46px}
.panelbtn{display:inline-flex;align-items:center;gap:7px;padding:6px 11px;
  background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);
  border-radius:9px;color:#8b8792;font-size:12.5px;cursor:pointer;
  transition:all .24s cubic-bezier(.16,1,.3,1)}
.panelbtn:hover{color:#eceaf0;background:rgba(255,255,255,.07)}
.panelbtn.on{color:#eceaf0;background:rgba(217,119,87,.14);
  border-color:rgba(217,119,87,.3)}
.panelpick{display:flex;gap:3px;overflow:hidden;
  max-width:640px;opacity:1;transition:max-width .34s cubic-bezier(.16,1,.3,1),
  opacity .24s ease}
.panelpick.off{max-width:0;opacity:0;pointer-events:none}
.pchip{padding:5px 11px;background:none;border:0;border-radius:8px;
  color:#78757f;font-size:12.5px;white-space:nowrap;cursor:pointer;
  transition:all .2s cubic-bezier(.16,1,.3,1)}
.pchip:hover{color:#c9c5cf;background:rgba(255,255,255,.045)}
.pchip.on{color:#eceaf0;background:rgba(255,255,255,.08)}

/* ══ RUNNING WORK ════════════════════════════════════════════════════
   One line, in the owner's language, never the tool's. Tool calls
   themselves collapse to a faint monospace trace for whoever wants it. */
.activity{display:flex;align-items:center;gap:10px;padding:11px 15px;
  border-radius:12px;background:rgba(255,255,255,.028);
  border:1px solid rgba(255,255,255,.05);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.045);
  font-size:13px;color:#c9c5cf;animation:rise .4s cubic-bezier(.16,1,.3,1) both}
.activity .aw{flex:1}
.activity .ac{font-size:11.5px;color:#6f6b76;
  font-variant-numeric:tabular-nums}
.spin{width:13px;height:13px;flex:none;border-radius:50%;
  border:1.5px solid rgba(217,119,87,.25);border-top-color:#d97757;
  animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.msg.tool{background:none;border:0;padding:3px 4px;font-size:11.5px;
  color:#5f5c66;font-family:var(--mono);opacity:.75;max-width:100%}
.msg.tool b{color:#807c88;font-weight:500}
.msg.think{background:none;border:0;color:#6f6b76;font-size:12.5px;
  font-style:italic;padding:4px 2px;max-width:88%}
@media (prefers-reduced-motion:reduce){.spin{animation-duration:2s}}

/* ══════════════════════════════════════════════════════════════════════
   SPEC v1 — a tool someone stares at for six hours, not a landing page
   they scroll for six seconds. This layer supersedes the earlier pass,
   which failed that test in five specific ways: glass over dense labels,
   an animated aurora behind reading text, a coloured focus ring on the
   most-touched object in the app, gradient text that cannot be contrast
   graded, and a spinner pretending to know how long work takes.
   ══════════════════════════════════════════════════════════════════════ */
:root{
  --g-0:#0b0a09;--g-1:#100e0d;--g-2:#151312;--g-3:#1b1817;
  --g-4:#221e1d;--g-5:#2a2624;
  --edge-1:rgba(255,255,255,.06);--edge-2:rgba(255,255,255,.10);
  --edge-3:rgba(255,255,255,.18);--edge-top:rgba(255,255,255,.07);
  --t-1:#ece8e5;--t-2:#b5aeaa;--t-3:#837c78;--t-4:#565150;
  --a:#d97757;--a-hi:#e6906f;--a-lo:#b95c3e;
  --a-08:rgba(217,119,87,.08);--a-16:rgba(217,119,87,.16);
  --ok:#5fa383;--bad:#c96a5f;
  --lift-1:0 .6px 1.6px -1.5px rgba(0,0,0,.20),
           0 2.3px 6px -3px rgba(0,0,0,.16),0 10px 26px -4.5px rgba(0,0,0,.04);
  --r-sm:6px;--r-md:10px;--r-lg:14px;--r-xl:18px;
  --eo:cubic-bezier(.23,1,.32,1);--d-press:120ms;--d-pop:160ms;--d-menu:200ms;
  /* remap the app's legacy names onto the ramp */
  --bg:var(--g-0);--panel:var(--g-1);--panel2:var(--g-4);--field:var(--g-3);
  --line:var(--edge-1);--line2:var(--edge-2);--tx:var(--t-1);--dim:var(--t-2);
  --lo:var(--t-3);--acc:var(--a);--acc2:var(--a-hi);--accg:var(--a);
}
body{background:var(--g-0);color:var(--t-1);
  font-feature-settings:"cv05" 1,"tnum" 1;font-optical-sizing:auto}

/* ── LIGHT: fixed, quiet, behind opaque surfaces. No drift. ────────── */
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
  animation:none;filter:none;
  background:
    radial-gradient(46% 40% at 14% -6%, rgba(217,119,87,.085) 0%,transparent 72%),
    radial-gradient(52% 44% at 92% 104%, rgba(120,145,190,.045) 0%,transparent 74%)}
body::after{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
  opacity:.032;mix-blend-mode:soft-light;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.72' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)'/%3E%3C/svg%3E")}
@media (prefers-reduced-transparency:reduce){body::after{display:none}}

/* ── NO GLASS ON ANYTHING THAT HOLDS A FORM OR A LABEL ─────────────── */
aside{background:var(--g-1);backdrop-filter:none;-webkit-backdrop-filter:none;
  box-shadow:1px 0 0 0 var(--edge-1);border-right:0}
header{background:var(--g-1);backdrop-filter:none;-webkit-backdrop-filter:none;
  box-shadow:0 1px 0 0 var(--edge-1);border-bottom:0}
section#content{background:var(--g-2)}

/* ── TYPE: 400 / 510 / 590. Never 700. Tracking tightens with size. ── */
.focal h1,#welcome h1{font-size:34px;font-family:var(--display);line-height:1.15em;letter-spacing:-.035em;
  font-weight:590;color:var(--t-1);
  background:none;-webkit-text-fill-color:currentColor;margin:0 0 14px}
.focal h1 em,#welcome h1 em{font-style:italic;color:var(--t-1)}
.focal .sub,#welcome .sub{font-size:15px;line-height:1.62em;letter-spacing:-.014em;
  color:var(--t-2);max-width:60ch;margin:0 0 var(--s-8,32px)}
#ptitle{font-size:20px;font-family:var(--display);line-height:1.3em;letter-spacing:-.028em;font-weight:590}

/* ── THE COMPOSER: one slab. Focus is a brightening hairline. ──────── */
.startrow{max-width:780px;margin:0 auto;gap:10px;align-items:flex-end}
.startrow::before{display:none}                 /* the coloured glow: gone */
.startrow textarea{background:linear-gradient(180deg,#1d1a19,#171514);
  border:0;border-radius:var(--r-xl);padding:14px 16px;
  font:400 15px/1.6em var(--sans,inherit);letter-spacing:-.014em;
  color:var(--t-1);caret-color:var(--a);max-height:220px;
  box-shadow:var(--lift-1),inset 0 0 0 1px var(--edge-2),
             inset 0 1px 0 0 rgba(255,255,255,.09);
  transition:box-shadow var(--d-pop) var(--eo)}
.startrow textarea::placeholder{color:var(--t-3)}
.startrow textarea:focus{background:linear-gradient(180deg,#1d1a19,#171514);
  box-shadow:var(--lift-1),inset 0 0 0 1px var(--edge-3),
             inset 0 1px 0 0 rgba(255,255,255,.13)}
.startrow button.primary{width:44px;height:44px;border-radius:var(--r-md);
  background:var(--a);color:#1c0f09;border:0;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.22);
  transition:background var(--d-press) var(--eo),transform var(--d-press) var(--eo)}
.startrow button.primary:hover{background:var(--a-hi);transform:none}
.startrow button.primary:active{background:var(--a-lo);transform:scale(.97)}

/* ── CHAT: full-bleed rows, capped measure. No bubbles. ────────────── */
/* THE LAST LINE HAS TO CLEAR THE DOCK. 20px put the final sentence
   right against the composer, and the dock's own gradient fade sits
   over the top of it — so the thing you just read was being dimmed
   by the thing you type into. The padding is INSIDE the scroller so
   scrolled-to-bottom still leaves the gap; the margin alone would
   collapse the moment the log overflows. */
.clog{max-height:52vh;gap:20px;max-width:780px;margin:0 auto 12px;
  width:100%;padding-bottom:26px;scroll-padding-bottom:26px}
.msg{animation:none;max-width:100%;border-radius:0;border:0;padding:0;
  font:400 15px/1.62em var(--sans,inherit);letter-spacing:-.014em}
.msg.you{background:none;color:var(--t-2);align-self:stretch;
  padding:0 0 0 14px;box-shadow:inset 2px 0 0 0 var(--a-16)}
.msg.bot{background:none;color:var(--t-1);box-shadow:none}
.msg.sys{color:var(--t-3);font-size:12px;text-align:left;letter-spacing:-.012em}
.msg.tool{font:400 12px/1.5em var(--mono);color:var(--t-3);opacity:1;
  padding:2px 0}
.msg.tool b{color:var(--t-2);font-weight:510}

/* ── RUNNING WORK: a dot that pulses, never a spinner. ─────────────── */
.activity{background:none;border:0;box-shadow:none;padding:2px 0;
  animation:none;color:var(--t-2);font:510 12.5px/1.4em var(--sans,inherit);
  gap:8px}
.spin{width:6px;height:6px;border:0;border-radius:9999px;background:var(--a);
  animation:pulse 1.8s cubic-bezier(.77,0,.175,1) infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.activity .ac{color:var(--t-3);font-size:11.5px}

/* ── ROWS: instant hover. A transition here lags under a sweeping cursor. */
.pitem,.qrow,.pchip{transition:none}
.pitem:hover,.qrow:hover{background:var(--g-4)}
.pitem.sel{background:var(--g-4);box-shadow:inset 2px 0 0 0 var(--a)}
.qrow{border-top:1px solid var(--edge-1);color:var(--t-2);
  font:510 13px/1.4em var(--sans,inherit);letter-spacing:-.022em}
.panelbtn,.pchip{font-weight:510;letter-spacing:-.022em}
.panelbtn.on{background:var(--a-08);border-color:var(--a-16);color:var(--t-1)}

@media (prefers-reduced-motion:reduce){
  .spin{animation:none;opacity:.8}
  *,*::before,*::after{animation-duration:.01ms!important;
    transition-duration:.01ms!important}}

/* ══ THE SPLIT: conversation always present, work beside it ══════════ */
section#content{display:block;position:relative;overflow:hidden}
section#content[data-split="1"]{display:grid;grid-template-columns:1fr 0fr;
  transition:grid-template-columns var(--d-menu,200ms) var(--eo);min-height:0}
body.split section#content[data-split="1"]{grid-template-columns:1fr minmax(380px,46%)}
section#content>*{min-height:0;min-width:0}      /* the grid-overflow trap */

/* The pane does NOT scroll — the log inside it does. When this scrolled,
   the composer scrolled away with it. */
.conv{display:flex;flex-direction:column;overflow:hidden;padding:0;min-height:0}
.work{display:grid;grid-template-rows:44px 1fr;min-height:0;overflow:hidden;
  background:var(--g-1);box-shadow:-1px 0 0 0 var(--edge-1);opacity:0;
  transition:opacity var(--d-menu,200ms) var(--eo)}
body.split .work{opacity:1}
.workhd{display:flex;align-items:center;justify-content:space-between;
  padding:0 12px 0 18px;box-shadow:0 1px 0 0 var(--edge-1);
  font:590 13px/1 var(--sans,inherit);letter-spacing:-.022em;color:var(--t-1)}
.workhd .iconbtn{transform:rotate(45deg)}
.workbody{overflow:auto;padding:16px 18px;min-height:0}
.workbody .card{background:none;border:0;box-shadow:none;padding:0;margin:0 0 18px}
.workbody iframe{width:100%;border-radius:var(--r-md);border:0;
  box-shadow:inset 0 0 0 1px var(--edge-1)}

/* the conversation keeps its measure even when the pane is open */
body.split .clog,body.split .startrow{max-width:100%}
/* NARROW WINDOWS COVER THE CHAT, THEY DO NOT DESTROY IT.
   display:none here was the same fault in a different costume: open the
   preview on a small window and the conversation was gone, scroll
   position and all. As an overlay it is still mounted, still scrolled
   where you left it, and Close or Esc brings it straight back. */
@media (max-width:1180px){
  body.split section#content[data-split="1"]{grid-template-columns:1fr}
  body.split .conv{display:flex}
  body.split .work{position:absolute;inset:0;z-index:5;
    box-shadow:-24px 0 48px -24px rgba(0,0,0,.6)}}

/* ── PANEL CHIPS: always present, one click each ─────────────────── */
.panelpick,.panelbtn{display:none!important}   /* the two-step: gone */
nav#tabs{display:flex;align-items:center;gap:2px;padding:0 18px;min-height:44px}
.tabgap{flex:1}
.pchip{padding:6px 12px;background:none;border:0;border-radius:var(--r-md);
  color:var(--t-3);font:510 12.5px/1 var(--sans,inherit);letter-spacing:-.022em;
  cursor:pointer;transition:none;display:inline-flex;align-items:center;gap:6px}
.pchip:hover{color:var(--t-1);background:var(--g-4)}
.pchip.on{color:var(--t-1);background:var(--g-4);
  box-shadow:inset 0 0 0 1px var(--edge-2)}
.pchip.ghost{color:var(--t-4)}
.pchip.ghost:hover{color:var(--t-2)}

/* the preview must actually fill its pane */
.workbody{padding:0}
.workbody>*{padding:16px 18px}
.workbody iframe{height:calc(100vh - 210px);min-height:420px;
  border-radius:0;box-shadow:none;padding:0}

/* ══ INSPECTOR FIELDS ════════════════════════════════════════════════
   The pane was a two-column form squeezed into ~460px: labels wrapped to
   two lines, the checkbox floated above its own text, and help text ran
   as wide as the paragraph it was explaining. One column, short labels,
   hints demoted under the field they explain. */
.insp{padding:0 0 22px}
.insp h4{position:sticky;top:0;z-index:2;margin:0 0 14px;padding:14px 0 9px;
  background:linear-gradient(180deg,var(--g-1) 74%,transparent);
  font:590 11px/1 var(--sans,inherit);letter-spacing:.09em;
  text-transform:uppercase;color:var(--t-3)}
.field{display:grid;gap:6px;margin-bottom:16px}
.field label{font:510 12px/1.3 var(--sans,inherit);letter-spacing:-.012em;
  color:var(--t-2)}
.field .fh{font:400 11.5px/1.45 var(--sans,inherit);color:var(--t-4)}
.insp input[type=text],.insp input:not([type]),.insp select,
.workbody input:not([type=checkbox]):not([type=file]),.workbody select{
  width:100%;min-height:34px;padding:8px 11px;border:0;border-radius:var(--r-md);
  background:var(--g-3);color:var(--t-1);
  font:400 13px/1.5 var(--sans,inherit);outline:none;
  box-shadow:inset 0 0 0 1px var(--edge-1),inset 0 1px 0 var(--edge-top);
  transition:box-shadow var(--d-pop) var(--eo),background var(--d-pop) var(--eo)}
.workbody input:hover:not([type=checkbox]){box-shadow:inset 0 0 0 1px var(--edge-2),
  inset 0 1px 0 var(--edge-top)}
.workbody input:focus:not([type=checkbox]),.workbody select:focus{
  background:var(--g-5);box-shadow:inset 0 0 0 1px var(--edge-3),
  inset 0 1px 0 rgba(255,255,255,.12)}
.check{display:flex;align-items:center;gap:9px;margin:2px 0 18px;
  font:400 13px/1.4 var(--sans,inherit);color:var(--t-2);cursor:pointer}
.check input{width:15px;height:15px;flex:none;margin:0;accent-color:var(--a)}
.frow{display:flex;align-items:center;gap:10px}
.workbody button{min-height:32px;padding:0 14px;border-radius:var(--r-md);
  background:var(--g-3);color:var(--t-1);border:0;
  font:510 12.5px/1 var(--sans,inherit);letter-spacing:-.022em;cursor:pointer;
  box-shadow:inset 0 0 0 1px var(--edge-1),inset 0 1px 0 var(--edge-top)}
.workbody button:hover{background:var(--g-4)}
.workbody button.primary{background:var(--a);color:#1c0f09;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.22)}
.workbody button.primary:hover{background:var(--a-hi)}
/* the model row: stack it, never four columns in a narrow pane */
.workbody .row{display:grid!important;grid-template-columns:1fr;gap:14px}
.workbody .row>div{min-width:0}
.workbody .hint{font:400 11.5px/1.5 var(--sans,inherit);color:var(--t-4);
  display:block;margin-top:8px}
.workbody .card{background:none;border:0;box-shadow:none;padding:0;margin:0}
.workbody .toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  margin-top:12px}

/* ══ INSPECTOR, TIGHTENED ════════════════════════════════════════════
   The sticky section heading was translucent over a 74% gradient, so the
   first field's label read straight through it — two words stacked on the
   same line. A sticky header has to be opaque where it overlaps, and it
   has to own the space above the first field rather than borrow it. */
.workbody{padding:0 18px 24px}
.workbody>*{padding:0}
.insp{padding:0}
.insp h4{position:sticky;top:0;z-index:3;
  margin:0 -18px 12px;padding:16px 18px 9px;
  background:var(--g-1);
  box-shadow:0 1px 0 var(--edge-1);
  font:590 10.5px/1 var(--sans,inherit);letter-spacing:.1em;
  text-transform:uppercase;color:var(--t-3)}
.insp+.insp h4{margin-top:8px}
.field{display:grid;gap:5px;margin:0 0 14px}
.field label{font:510 12px/1.35 var(--sans,inherit);color:var(--t-2)}
.field .fh{font:400 11px/1.4 var(--sans,inherit);color:var(--t-4);margin-top:1px}
.insp input,.workbody input:not([type=checkbox]):not([type=file]),
.workbody select{min-height:34px;font-size:12.5px}
.check{margin:0 0 14px;font-size:12.5px}
.frow{margin-bottom:4px}
.workbody .row{gap:14px}
.workbody .row>div{display:grid;gap:5px}
.workbody .row label{font:510 12px/1.35 var(--sans,inherit);color:var(--t-2)}
.workbody .hint{font-size:11px;line-height:1.5;margin-top:6px}
.workbody .toolbar{margin-top:10px}

/* ══ RESPONSIVE + RESIZABLE PANE ═════════════════════════════════════
   A fixed half-screen is wrong at both ends: cramped on a laptop, wasteful
   on a wide display. The pane now sizes from a stored width, clamped so
   the conversation always keeps a readable measure. */
body.split section#content[data-split="1"]{
  grid-template-columns:minmax(420px,1fr) var(--workw,clamp(360px,38%,560px))}
.work{position:relative}
.wgrip{position:absolute;left:-3px;top:0;bottom:0;width:7px;cursor:col-resize;
  z-index:6}
.wgrip::after{content:"";position:absolute;left:3px;top:0;bottom:0;width:1px;
  background:transparent;transition:background var(--d-pop) var(--eo)}
.wgrip:hover::after,.wgrip.drag::after{background:var(--a)}
@media (max-width:1180px){
  body.split section#content[data-split="1"]{grid-template-columns:1fr}
  .wgrip{display:none}}

/* ══ HEADER: title, one action, overflow ═════════════════════════════ */
header{gap:12px}
.steps{display:flex;align-items:center;gap:6px}
.steps button{min-height:32px;padding:0 13px;border-radius:var(--r-md);
  background:var(--g-3);color:var(--t-2);border:0;
  font:510 12.5px/1 var(--sans,inherit);letter-spacing:-.022em;cursor:pointer;
  display:inline-flex;align-items:center;gap:7px;
  box-shadow:inset 0 0 0 1px var(--edge-1),inset 0 1px 0 var(--edge-top);
  transition:background var(--d-press) var(--eo),color var(--d-press) var(--eo)}
.steps button:hover{background:var(--g-4);color:var(--t-1)}
.steps button.big{background:var(--a);color:#1c0f09;font-weight:590;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.22)}
.steps button.big:hover{background:var(--a-hi);color:#1c0f09}
.steps button.big:active{background:var(--a-lo);transform:scale(.97)}
.steps button.ghost{background:none;color:var(--t-3);box-shadow:none}
.steps button.ghost:hover{background:var(--g-3);color:var(--t-1)}
.steps button.warn{background:none;color:var(--bad);box-shadow:none}
.steps button.warn:hover{background:rgba(201,106,95,.10)}

/* overflow menu */
.omenu{position:relative}
.omenu>summary{list-style:none;width:32px;height:32px;border-radius:var(--r-md);
  display:grid;place-items:center;cursor:pointer;color:var(--t-3);
  box-shadow:inset 0 0 0 1px var(--edge-1)}
.omenu>summary::-webkit-details-marker{display:none}
.omenu>summary:hover{background:var(--g-3);color:var(--t-1)}
.omenu[open]>summary{background:var(--g-4);color:var(--t-1)}
.omlist{position:absolute;right:0;top:38px;z-index:40;min-width:216px;padding:6px;
  border-radius:var(--r-lg);background:rgba(26,23,22,.86);
  backdrop-filter:blur(20px) saturate(180%);
  -webkit-backdrop-filter:blur(20px) saturate(180%);
  box-shadow:0 1px 2px -1px rgba(0,0,0,.3),0 6px 14px -6px rgba(0,0,0,.28),
             0 24px 56px -12px rgba(0,0,0,.36),
             inset 0 0 0 1px rgba(255,255,255,.12),
             inset 0 1px 0 rgba(255,255,255,.16);
  animation:omin var(--d-menu) var(--eo) both}
@keyframes omin{from{opacity:0;transform:scale(.98) translateY(-4px)}
                to{opacity:1;transform:none}}
.omlist button{width:100%;justify-content:flex-start;background:none;
  box-shadow:none;color:var(--t-2);min-height:32px;padding:0 10px}
.omlist button:hover{background:rgba(255,255,255,.07);color:var(--t-1)}
.omlist hr{border:0;height:1px;background:var(--edge-1);margin:5px 8px}
@media (prefers-reduced-transparency:reduce){
  .omlist{background:#1a1716;backdrop-filter:none}}
@media (prefers-reduced-motion:reduce){.omlist{animation:none}}

/* ══ THE PANE SIZES TO ITS CONTENT ═══════════════════════════════════ */
body.split.wide section#content[data-split="1"]{
  grid-template-columns:minmax(360px,1fr) var(--workw,clamp(620px,62%,1180px))}
/* A display:none GRID CHILD LEAVES THE GRID. `0 1fr` plus a hidden
   conversation meant the work pane stopped being the second track and
   became the first — the 0px one — so it measured 0 wide and edit mode
   opened blank. With one visible child there must be exactly one track. */
body.split.editing section#content[data-split="1"]{
  grid-template-columns:1fr}
body.split.editing .conv{display:none}
body.split.editing .wgrip{display:none}

/* the frame never becomes a sliver, and the dock stacks when it must */
#editrow{gap:0}
#editframe{min-width:0;flex:1 1 auto}
#editdock{max-width:min(392px,42%)}
@media (max-width:1280px){
  body.editing #editrow{flex-direction:column}
  body.editing #editdock{max-width:none;width:100%!important;
    max-height:46vh;overflow:auto}
  body.editing #editframe{min-height:52vh}}
.workbody iframe{min-width:0}

/* ══ EDIT MODE NEEDS A HEIGHT AT EVERY WIDTH ═════════════════════════
   min-height lived only inside the <=1280px block, so on a wide window
   the frame had no height at all and edit mode opened blank. The narrow
   path worked, which is exactly why it shipped: the pane I tested in
   happened to be 957px. Height is unconditional now; the media query
   only changes DIRECTION. */
/* THE TOOLBAR IS NOT THE FRAME. `.workbody > * { height:100% }` hit the
   toolbar too, so it became full-height — that was the empty gap pushing
   the site below the fold — and it scrolled away with the content, which
   is why there was no way back out of edit mode. Column layout: toolbar
   sized to its content and pinned, frame takes the rest. */
body.editing .workbody{padding:0;height:100%;min-height:0;
  display:flex;flex-direction:column;overflow:hidden}
body.editing .workbody>.toolbar{flex:0 0 auto;height:auto;
  position:sticky;top:0;z-index:8;margin:0;padding:10px 14px;
  background:var(--g-1);box-shadow:0 1px 0 var(--edge-1);
  display:flex;align-items:center;gap:8px;flex-wrap:wrap}
body.editing .workbody>#editrow{flex:1 1 auto;min-height:0;height:auto}
#editrow{flex:1;display:flex;min-height:0;height:100%}
#editframe{min-width:0;min-height:0;flex:1 1 auto;width:100%;
  height:100%;border:0;display:block}
#editdock{max-width:min(392px,42%);height:100%;overflow:auto}
@media (max-width:1280px){
  body.editing #editrow{flex-direction:column;height:100%}
  body.editing #editdock{max-width:none;width:100%!important;
    height:auto;max-height:44vh}
  body.editing #editframe{height:auto;flex:1 1 56%;min-height:340px}}
</style></head><body>
<aside>
  <div class="brand"><img class="bmark" src="__MARK__" alt=""><b>Aethron</b> <span>Studio</span></div>
  <div class="find"><span data-ic="search" data-ics="13"></span>
    <input id="findq" placeholder="Search" autocomplete="off" spellcheck="false"
     oninput="runFind(this.value)"
     onkeydown="if(event.key==='Escape'){this.value='';runFind('')}
                if(event.key==='Enter')findFirst()">
    <kbd>&#8984;K</kbd></div>
  <div id="findhits" class="findhits" hidden></div>
  <div id="plist"></div>
  <div class="srows">
    <button class="qrow" onclick="newProject()">
      <span data-ic="plus"></span><span class="ql">New project</span>
      <span class="qc" data-ic="up"></span></button>
    <button class="qrow" id="codebtn" onclick="openCode()">
      <span data-ic="terminal"></span><span class="ql">Code workspace</span>
      <span class="qc" data-ic="up"></span></button>
<!-- "Design from a screenshot" used to live here. It is the same
     action as attaching a screenshot in the conversation, and two doors
     to one capability is exactly what let the old editor leak into the
     new one. The view itself is untouched and still reachable in code;
     only the duplicate entrance is gone. -->
    <button class="qrow" id="libbtn" onclick="openLibrary()">
      <span data-ic="library"></span><span class="ql">Design library</span>
      <span class="qc" data-ic="up"></span></button>
    <button class="qrow" id="setbtn" onclick="openSettings()">
      <span data-ic="gear"></span><span class="ql">Settings</span>
      <span class="qc" data-ic="up"></span></button>
  </div>
  </aside>
<main>
  <header>
    <div id="ptitle">no project selected</div>
    <div class="steps" id="steps"></div>
    <button class="iconbtn" id="helpbtn" title="show the walkthrough" onclick="startTour(0)"><span data-ic="help"></span></button>
  </header>
  <div id="progress"><div class="lbl" id="prog-lbl">working…</div>
    <div class="bar"><div class="fill" id="prog-fill"></div></div></div>
  <nav id="tabs"></nav>
  <section id="content" class="conv-host"><div class="conv-shell" id="convshell">
   <canvas class="dotf hero"></canvas>
   <div class="conv-scroll" id="convscroll"><div class="conv-wrap">
     <div id="welcome">
     <h1>Every template you buy<br>can be entirely yours.</h1>
     <p class="sub">Paste a live Framer or Webflow URL, drop in a screenshot,
      or just say what you want built. Aethron works out whether that is a
      migration, a port, a rebuild or a coding job &mdash; and does it here.</p>
     </div>
     <div id="chatlog" class="clog" hidden></div>
   </div></div>
   <div class="conv-dock"><div class="conv-wrap">
     <div class="composer" data-beam="ae"><span data-beam-bloom></span>
       <textarea id="npurl" rows="1"
        placeholder="Paste a template URL, or tell Aethron what you want…"
        oninput="growTa(this)"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();consoleSend()}"></textarea>
       <div class="cbar">
         <label class="cpill" for="npfile"
          title="an export, a zip, or pages you saved separately"><span
          data-ic="file" data-ics="13"></span>attach</label>
         <button class="cpill opt" onclick="openSettings()"
          title="change the model or the key"><span data-ic="key"
          data-ics="13"></span><span class="cv" id="cbarmodel">model</span></button>
         <button class="cbtn" id="sendbtn" onclick="consoleSend()"
          aria-label="Send" title="Send"><span data-ic="up"></span></button>
       </div>
     </div>
     <div class="dockmeta" id="dockmeta"></div>
     <div class="startmeta">
       <input id="npname" placeholder="Project name (optional)">
       <span class="or">or</span>
       <label class="fpick"><input id="npfile" type="file"
        accept=".html,.htm,.zip" multiple><span data-ic="file"></span>
        choose an export, a zip, or saved pages</label>
     </div>
     <div class="qrows">
       <button class="qrow" onclick="quick('Migrate ','Paste the live Framer or Webflow URL, and tell me the brand it should become.')">
         <span data-ic="zap"></span><span class="ql">Migrate a live site</span>
         <span class="qc" data-ic="up"></span></button>
       <button class="qrow" onclick="quick('Rebuild this screenshot as ','Attach or paste the screenshot, then say which framework you want.')">
         <span data-ic="image"></span><span class="ql">Rebuild a screenshot into real code</span>
         <span class="qc" data-ic="up"></span></button>
       <button class="qrow" onclick="openCode()">
         <span data-ic="terminal"></span><span class="ql">Open the coding
         workspace</span><span class="qc" data-ic="up"></span></button>
     </div>
   </div></div>
 </div></section>
</main>
<script>
const $=id=>document.getElementById(id);
/* set before anything renders, so no frame is painted opaque first */
if(location.search.indexOf('glass=1')>=0)
  document.documentElement.classList.add('native-glass');
const ICONS={
search:'<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
terminal:'<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>',
image:'<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
user:'<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
stop:'<rect x="6" y="6" width="12" height="12" rx="2"/>',
key:'<path d="m15.5 7.5 3 3L22 7l-3-3"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/>',
coins:'<circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18"/><path d="M7 6h1v4"/><path d="m16.71 13.88.7.71-2.82 2.82"/>',
file:'<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v5h5"/>',
send:'<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
lock:'<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
anvil:'<path d="M7 10H6a4 4 0 0 1-4-4 1 1 0 0 1 1-1h4"/><path d="M7 5a1 1 0 0 1 1-1h13a1 1 0 0 1 1 1 7 7 0 0 1-7 7H8a1 1 0 0 1-1-1z"/><path d="M9 12v5"/><path d="M15 12v5"/><path d="M5 20a3 3 0 0 1 3-3h8a3 3 0 0 1 3 3 1 1 0 0 1-1 1H6a1 1 0 0 1-1-1"/>',
zap:'<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
hammer:'<path d="m15 12-8.373 8.373a1 1 0 1 1-3-3L12 9"/><path d="m18 15 4-4"/><path d="m21.5 11.5-1.914-1.914A2 2 0 0 1 19 8.172V7l-2.26-2.26a6 6 0 0 0-4.202-1.756L9 2.96l.92.82A6.18 6.18 0 0 1 12 8.4V10l2 2h1.172a2 2 0 0 1 1.414.586L18.5 14.5"/>',
undo:'<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
play:'<polygon points="6 3 20 12 6 21 6 3"/>',
download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
package:'<path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
library:'<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
pencil:'<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
compass:'<circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>',
trash:'<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>',
palette:'<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/>',
sparkles:'<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/>',
wand:'<path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8 19 13"/><path d="M15 9h.01"/><path d="M17.8 6.2 19 5"/><path d="m3 21 9-9"/><path d="M12.2 6.2 11 5"/>',
alert:'<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
home:'<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
check:'<path d="M20 6 9 17l-5-5"/>',
plus:'<path d="M5 12h14"/><path d="M12 5v14"/>',
snow:'<line x1="2" x2="22" y1="12" y2="12"/><line x1="12" x2="12" y1="2" y2="22"/><path d="m20 16-4-4 4-4"/><path d="m4 8 4 4-4 4"/><path d="m16 4-4 4-4-4"/><path d="m8 20 4-4 4 4"/>',
pin:'<path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z"/>',
up:'<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
rocket:'<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
x:'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
help:'<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>'};
const I=(n,s=14)=>{
  /* An unknown name used to render an EMPTY svg — a silent hole that
     still takes its width, which is exactly how a misaligned sidebar row
     shipped once before. Say so in the console instead of hiding it. */
  if(!ICONS[n])console.warn('icon missing:',n);
  return `<svg class="ic" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[n]||''}</svg>`;};
document.querySelectorAll('[data-ic]').forEach(n=>{n.outerHTML=I(n.dataset.ic,+(n.dataset.ics||14))});
const S={projects:[],cur:null,info:null,cm:null,tab:'plan',log:'',running:null};
const enc=new TextEncoder();
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(path,body){
  const r=await fetch(path,body?{method:'POST',body:JSON.stringify(body)}:undefined);
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.error||r.statusText);
  return j;}
async function b64of(file){ // chunked: spread on big arrays blows the stack
  const buf=new Uint8Array(await file.arrayBuffer());let s='';
  for(let i=0;i<buf.length;i+=32768)
    s+=String.fromCharCode.apply(null,buf.subarray(i,i+32768));
  return btoa(s);}

async function refresh(keepTab){
  const before=S.listed?S.projects:null;
  S.projects=await api('/api/projects');
  S.listed=true;
  /* WHAT "SOMETHING NEW ENTERED AETHRON" ACTUALLY MEANS. Not the first
     listing — on a cold start every project would wear the badge and it
     would mean nothing. A name that was NOT in the previous listing and
     is not the one you just opened yourself: that is a thing that
     arrived, which on this product is usually the agent finishing a
     migration while you were reading something else. */
  if(before){
    const had=new Set(before.map(p=>p.name));
    S.fresh=S.fresh||new Set();
    for(const p of S.projects)
      if(!had.has(p.name)&&p.name!==S.cur)S.fresh.add(p.name);
  }else S.fresh=new Set();   /* the FIRST listing is not news */
  renderSidebar();
  if(S.cur){
    S.info=S.projects.find(p=>p.name===S.cur)||null;
    if(!S.info){S.view='';S.cur=null;S.cm=null;S.panel=false;{const _c=document.getElementById('content');if(_c)_c.dataset.split='';}}
  }
  renderHeader();
  if(!keepTab)renderTab();
}
function renderSidebar(){
  $('plist').innerHTML=S.projects.map(p=>{
    const pct=p.total?Math.round(p.filled/p.total*100):0;
    return `<div class="pitem${p.name===S.cur?' sel':''}" onclick="select('${p.name}')">
     <div class="pmain">
      <div class="pname">${esc(p.name)}</div>
      <div class="pmeta"><span class="plat ${p.platform}">${p.platform}</span>
       <span>${p.filled}/${p.total} filled</span></div>
      <div class="pbar"><i style="width:${pct}%"></i></div></div>
     ${S.fresh&&S.fresh.has(p.name)?liveNewHtml('Live mode',.82):''}
     <span class="del" onclick="event.stopPropagation();delProject('${p.name}')">✕</span>
     </div>`}).join('')||'<div class="sideempty"><span class="mk">'+I('package',15)+'</span>'
      +'<b>No projects yet</b>Paste a template URL, or say what you want '
      +'built. Whatever you start appears here.</div>';
  mountMetal(); playArrivals();
}
async function select(name){
  /* a sheet showing the LAST project must not survive into this one */
  closePvw&&closePvw();
  if(S.fresh)S.fresh.delete(name);      /* you have seen it; it is not new */
  S.cur=name;S.cm=null;S.tab='chat';S.view='project';
  const lb=$('libbtn');if(lb)lb.classList.remove('sel');
  await refresh();
  try{S.cm=await api('/api/copymap?project='+name);}catch(e){}
  renderTab();renderSidebar();
}
async function delProject(name){
  if(!confirm(`Delete project "${name}"? pristine/, copy_map and site/ all go.`))return;
  await api('/api/projects/delete',{name});
  if(S.cur===name){S.view='';S.cur=null;S.cm=null;S.panel=false;{const _c=document.getElementById('content');if(_c)_c.dataset.split='';}}
  refresh();
  checkUpdate();
}
async function createProject(){
  const fl=[...$('npfile').files],name=$('npname').value,
        url=$('npurl').value.trim();
  if(!name||(!fl.length&&!url))
    return alert('need a name plus a URL or file(s)');
  try{
    let r;
    if(url){
      r=await api('/api/projects',{name,url});
    }else{
      const files=[];
      for(const f of fl)files.push({filename:f.name,data_b64:await b64of(f)});
      r=await api('/api/projects',{name,files});
    }
    $('npname').value='';$('npfile').value='';$('npurl').value='';
    await select(r.name);
  }catch(e){alert(e.message)}
}

const STEPS=[['fetch','1 Fetch'],['inventory','2 Inventory'],
             ['build','3 Build'],['verify','4 Verify'],
             ['probe','5 Runtime check']];
function openWork(t){
  if(S.panel&&S.tab===t)return closeWork();
  S.panel=true;S.tab=t;renderHeader();renderTab();
}
/* Drag to resize, and remember it. A pane you keep re-adjusting is a
   pane that forgot. */
function initGrip(){
  const g=document.getElementById('wgrip');if(!g||g.dataset.on)return;
  g.dataset.on='1';
  const saved=localStorage.getItem('forge_workw');
  if(saved)document.documentElement.style.setProperty('--workw',saved);
  g.addEventListener('pointerdown',e=>{
    e.preventDefault();g.classList.add('drag');g.setPointerCapture(e.pointerId);
    const move=ev=>{
      const w=Math.min(Math.max(innerWidth-ev.clientX,320),innerWidth-460);
      document.documentElement.style.setProperty('--workw',w+'px');};
    const up=ev=>{
      g.classList.remove('drag');g.releasePointerCapture(e.pointerId);
      g.removeEventListener('pointermove',move);g.removeEventListener('pointerup',up);
      localStorage.setItem('forge_workw',
        getComputedStyle(document.documentElement).getPropertyValue('--workw').trim());};
    g.addEventListener('pointermove',move);g.addEventListener('pointerup',up);
  });
}
function closeWork(){S.panel=false;S.tab='chat';renderHeader();renderTab()}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&S.panel&&!/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName))
    closeWork();});
function togglePanel(){
  S.panel=!S.panel;
  if(S.panel){ if(S.tab==='chat')S.tab='plan'; }
  else S.tab='chat';
  renderHeader();renderTab();
}
/* SELF-UPDATE. Checked once at startup, shown as one quiet row — never
   a modal, never a nag. Applying replaces the bundle IN PLACE (that is
   what stops a second copy appearing) and relaunches. */
let UPD=null;
async function checkUpdate(){
  /* Confirm the last update BEFORE asking about the next one. The
     restart is the one moment the user is watching, and until now it
     landed in silence — same window, same everything, no way to tell
     the new build from the old one. */
  try{
    const done=await api('/api/update/just-updated');
    if(done&&done.updated){
      updPill('<b>Aethron updated</b><div style="color:var(--dim);'
        +'margin-top:3px">now running '+esc(done.version||'')
        +(done.from?' — was '+esc(done.from):'')+'</div>'+updBar(100));
      setTimeout(()=>{ const p=$('updpill'); if(p)p.remove(); },7000);
    }
  }catch(e){}
  try{ UPD=await api('/api/update/check'); }catch(e){ return }
  if(UPD&&UPD.available)renderHeader();
}
/* An update is MINUTES on a slow connection, not seconds. It gets a
   progress pill you can ignore and keep working behind — not a frozen
   button and not a modal that holds the app hostage. */
function updPill(html){
  let p=$('updpill');
  if(!p){ p=document.createElement('div'); p.id='updpill';
    p.style.cssText='position:fixed;right:18px;bottom:18px;z-index:9999;'
      +'background:var(--panel);border:1px solid var(--line2);'
      +'border-radius:var(--r-lg);padding:11px 14px;min-width:210px;'
      +'box-shadow:0 12px 34px rgba(0,0,0,.45);font-size:12.5px;'
      +'color:var(--tx)';
    document.body.appendChild(p); }
  p.innerHTML=html;
  return p;
}
function updBar(pct){
  /* pct===null means WORKING, LENGTH UNKNOWN. Unpacking a 43MB bundle,
     verifying its signature and swapping it are not instant and report
     no percentage, so the honest bar for them is a moving stripe. The
     alternatives are both lies: freeze at 100% (looks wedged, and the
     first update read exactly that way) or drop back to 0% (looks like
     the download was thrown away). */
  if(pct===null){
    if(!$('updkf')){ const s=document.createElement('style'); s.id='updkf';
      s.textContent='@keyframes updslide{0%{left:-40%}100%{left:100%}}';
      document.head.appendChild(s); }
    return '<div style="height:4px;border-radius:3px;background:var(--field);'
      +'margin-top:8px;overflow:hidden;position:relative"><div style="'
      +'position:absolute;top:0;height:100%;width:40%;background:var(--acc);'
      +'animation:updslide 1.1s var(--eo) infinite"></div></div>';
  }
  return '<div style="height:4px;border-radius:3px;background:var(--field);'
    +'margin-top:8px;overflow:hidden"><div style="height:100%;width:'+pct
    +'%;background:var(--acc);transition:width .3s var(--eo)"></div></div>';
}
/* The updater names its phase; the bar shape follows from the name.
   Anything unrecognised is treated as work-in-progress rather than as
   0% — a new phase added later must never read as "nothing happened". */
function updPct(line){
  const m=line.match(/(\d+)%/);
  if(m)return +m[1];
  if(/restarting/i.test(line))return 100;
  if(/checking for/i.test(line))return 0;
  return null;
}
async function applyUpdate(){
  updPill('<b>Updating Aethron</b><div style="color:var(--dim);'
    +'margin-top:3px">starting…</div>'+updBar(0));
  let j;
  try{
    const r=await api('/api/update/apply',{});
    if(r.job===undefined){                       // older server shape
      if(!r.ok)throw new Error(r.why||'unknown');
      j={done:true,ok:true};
    }else{
      do{
        await new Promise(s=>setTimeout(s,700));
        try{ j=await api('/api/job?id='+r.job); }
        catch(e){ break; }   // server exited under us = it restarted
        const line=(j.log||'').trim().split('\n').pop()||'';
        updPill('<b>Updating Aethron</b><div style="color:var(--dim);'
          +'margin-top:3px">'+esc(line||'working…')+'</div>'
          +updBar(updPct(line)));
      }while(!j.done);
    }
  }catch(e){
    updPill('<b>Update failed</b><div style="color:var(--dim);margin-top:3px">'
      +esc(e.message)+'<br>Your app is untouched.</div>');
    return;
  }
  if(j&&j.done&&!j.ok){
    const why=((j.log||'').match(/update failed: (.*)/)||[])[1]||'unknown';
    updPill('<b>Update failed</b><div style="color:var(--dim);margin-top:3px">'
      +esc(why)+'<br>Your app is untouched.</div>');
    return;
  }
  document.body.innerHTML='<div class="empty"><div class="focal">'
    +'<h1>Updated to '+esc((UPD&&UPD.latest)||'the latest version')+'</h1>'
    +'<p class="sub">Aethron is restarting.</p></div></div>';
}
function updateMenuItem(){
  if(!(UPD&&UPD.available))return '';
  return `<hr><button id="updrow" onclick="this.closest('details').open=false;applyUpdate()">
    ${I('download',14)}<span>Update to ${esc(UPD.latest||'')}</span></button>`;
}
function renderHeader(){
  $('ptitle').textContent=S.view==='settings'?'Settings':S.view==='library'?'Design library'
    :S.cur?S.cur+(S.info?` · ${S.info.platform.toUpperCase()}`:''):'no project selected';
  if(!S.cur){$('steps').innerHTML='';$('tabs').innerHTML='';return;}
  const done={fetch:S.info?.fetched,inventory:S.info?.inventoried,
              build:S.info?.built,verify:false,probe:false};
  const ready=S.info?.built;   // prepped at least once
  const busy=S.running||S.autoRunning;
  $('steps').className='steps'+(busy?' busy':'');
  // ONE PRIMARY ACTION, THEN AN OVERFLOW.
  //
  // The header carried seven controls — Prepare/Build, a steps
  // disclosure, Undo, Preview, site.zip, Dev handoff, Save design — all
  // shouting at the top of a product whose premise is "just ask". Most
  // of them are things you would SAY. What stays is the one action that
  // advances the work in front of you; the rest is one click away and
  // silent until wanted.
  const menu=[
    ready?['Re-run everything','zap',"runAll()"]:null,
    ready?['Localize assets','home',"if(confirm('Download every remote asset into the project and rebuild? The site stops depending on the platform CDN.'))runStep('localize').then(ok=>ok!==false&&runStep('build'))"]:null,
    null,
    ready?['Port to a framework','package',"portFramework()"]:null,
    null,
    ['Download site.zip','download',"location='/api/download?project='+S.cur"],
    ['Dev handoff','package',"location='/api/download?full=1&project='+S.cur"],
    ['Save design to library','library',"saveToLibrary()"],
  ];
  $('steps').innerHTML=
    (S.zeroFx&&S.zeroFx.length?`<button class="warn" onclick="showZeroFx()"
       title="filled entries that replaced nothing in the last build"
       >${I('alert')}${S.zeroFx.length} dead</button>`:'')
   +(S.info&&S.info.undo?`<button class="ghost" onclick="doUndo()"
       title="revert the last change">${I('undo')}Undo</button>`:'')
   +(ready
      ? `<button class="big" onclick="runStep('build')"
           title="apply your edits to the site">${I('hammer')}Build</button>`
      : `<button class="big" onclick="runAll()"
           title="fetch, inventory and build, in order">
           ${I('zap')}${busy?'Preparing…':'Prepare'}</button>`)
   +`<details class="omenu"><summary title="more">${I('tab',15)}</summary>
      <div class="omlist">`
   + menu.map(m=>m?`<button onclick="this.closest('details').open=false;${m[2]}">
        ${I(m[1],14)}<span>${m[0]}</span></button>`:'<hr>').join('')
   + updateMenuItem()
   + `</div></details>`;
  // ONE CLICK, NOT TWO. The previous version hid these behind an
  // "Inspect" button, so opening the preview meant clicking a toggle to
  // reveal a list to click again — the exact navigation this was meant
  // to remove. They are always here now; clicking one opens it beside
  // the chat, clicking it again closes it.
  //
  // "Plan & AI" is gone on purpose. The plan is something you TELL the
  // agent — a form that duplicates the conversation is one more place
  // the same fact can live, and one more thing to keep in sync.
  const panels=[['preview','Preview'],['strings','Strings'],
                ['images','Images'],['links','Links'],['logs','Logs']];
  $('tabs').innerHTML=
    panels.map(([t,l])=>`<button class="pchip${S.panel&&S.tab===t?' on':''}"
        onclick="openWork('${t}')">${l}</button>`).join('')
    +`<span class="tabgap"></span>`
    +`<button class="pchip ghost" title="model, key and project settings"
        onclick="openWork('plan')">${I('cur',13)} Settings</button>`;
}

async function doUndo(){
  try{
    const r=await api('/api/undo',{project:S.cur});
    try{S.cm=await api('/api/copymap?project='+S.cur);}catch(e){S.cm=null}
    const {job}=await api('/api/run',{project:S.cur,cmd:'build'});
    let j;do{await new Promise(x=>setTimeout(x,900));
      j=await api('/api/job?id='+job);}while(!j.done);
    const f=$('editframe');if(f)f.src='/edit/'+S.cur+'/?r='+Date.now();
    await refresh(true);renderHeader();renderTab();
  }catch(e){alert(e.message)}
}
function setProgress(pct,label,cls){
  const p=$('progress');if(!p)return;
  p.className='on'+(cls?' '+cls:'');
  $('prog-fill').style.width=pct+'%';
  $('prog-lbl').textContent=label;
}
function hideProgress(){const p=$('progress');if(p)setTimeout(()=>{
  if(!S.running)p.className='';},2500);}

async function runAll(){
  if(S.running||S.autoRunning)return;
  S.autoRunning=true;
  try{await _runAll();}finally{S.autoRunning=false;renderHeader();}
}
async function _runAll(){
  // Prepare = fetch -> inventory -> build only. Verify is a
  // ship-readiness check for AFTER you fill the brand in (it flags the
  // old brand words, which are all still present before you edit).
  const seq=['fetch','inventory','build'];
  const labels={fetch:'Downloading template runtime',
    inventory:'Extracting editable content',build:'Building your site'};
  for(let i=0;i<seq.length;i++){
    setProgress(Math.round(i/seq.length*100),
      `Step ${i+1}/${seq.length} — ${labels[seq[i]]}…`);
    const ok=await runStep(seq[i]);
    if(ok===false){
      setProgress(Math.round((i+1)/seq.length*100),
        `Failed at ${seq[i]} — open Logs for details`,'fail');
      hideProgress();return;
    }
  }
  setProgress(100,'Ready! Now write your plan & fill, or edit directly '
    +'in Preview →','done');
  hideProgress();
  S.tab='chat';renderHeader();renderTab();
}
async function runStep(cmd,extra){
  if(S.running)return;
  if(S.autoRunning&&!['fetch','inventory','build','verify','probe'].includes(cmd))return;
  try{
    const {job}=await api('/api/run',{project:S.cur,cmd,...(extra||{})});
    return await watchJob(job,cmd);
  }catch(e){alert(e.message);return false}
}
const LABELS={fetch:'Downloading the runtime…',
  inventory:'Finding every string, image and link…',
  build:'Rebuilding the site…',verify:'Checking the files…',
  probe:'Loading it in a browser…',localize:'Localising assets…',
  ai:'Rewriting the copy…',
  convert:'Porting to a framework — rendering every page, then grading '
          +'the result against your site…'};
async function watchJob(job,label){
  // A RUN NO LONGER YANKS YOU INTO THE LOG TAB. The conversation is the
  // surface; work reports INTO it. The log is still one click away for
  // anyone who wants the raw output.
  S.running=label;
  CODE.events.push({type:'sys',text:LABELS[label]||label});
  renderHeader();renderTab();
  let j;
  do{
    await new Promise(r=>setTimeout(r,800));
    j=await api('/api/job?id='+job);
    S.log=j.log;
    if(S.tab==='logs'&&$('logbox')){$('logbox').textContent=j.log;
      $('logbox').scrollTop=1e9;}
  }while(!j.done);
  S.running=null;
  S.log+=`\n— ${label}: ${j.ok?'OK':'FAILED'} —\n`;
  await refresh(true);
  if(['inventory','build'].includes(label)||label==='ai')
    try{S.cm=await api('/api/copymap?project='+S.cur);}catch(e){}
  if(j.ok&&(label==='build'||label==='ai')){
    // a build ALWAYS refreshes what you're looking at — stale previews
    // made real edits look like silent failures
    reloadFrames();
    checkZeroEffect();
  }
  // HAND OVER THE RESULT INSTEAD OF MAKING SOMEONE GO AND FIND IT.
  // The button chain used to end in silence: the build finished and the
  // site was somewhere else, behind another click. A finished build now
  // opens the preview and puts the address in the conversation.
  if(j.ok&&label==='build'){
    try{
      const {port}=await api('/api/preview',{project:S.cur});
      const url=`http://127.0.0.1:${port}/`;
      CODE.events.push({type:'sys',
        text:`Build complete — live at ${url}`});
      if(!S.panel)openWork('preview');
    }catch(e){
      CODE.events.push({type:'sys',text:'Build complete.'});
    }
  }else if(!j.ok){
    CODE.events.push({type:'sys',
      text:`${label} failed — open Logs for the output.`});
  }
  renderHeader();renderTab();
  return j.ok;
}
function reloadFrames(){
  const pf=$('previewframe');
  if(pf)pf.src=pf.src.split('?')[0]+'?r='+Date.now();
  const ef=$('editframe');
  if(ef)ef.src='/edit/'+S.cur+'/?r='+Date.now();
}
async function checkZeroEffect(){
  try{
    const rep=await api('/api/report?project='+S.cur);
    const risk=new Set(rep.__at_risk__||[]);
    const moot=new Set(rep.__moot__||[]);   // mopped-up already = fine
    const zeros=[];
    for(const sec of ['strings','images','links'])
      for(const e of (S.cm&&S.cm[sec])||[])
        if(e.new&&(((e.old in rep)&&rep[e.old]===0&&!moot.has(e.old))
                   ||risk.has(e.old)))
          zeros.push(e.old.slice(0,70));
    S.zeroFx=zeros;
  }catch(e){S.zeroFx=[]}
  renderHeader();
}
/* THE FRAMEWORK PORT, reachable at last.

   It existed for weeks as a script in the repo — no button, no tool the
   agent could call, and not even shipped inside the app. A capability
   the interface cannot reach is a capability the owner does not have.

   The referee is the point: it renders BOTH builds and compares what a
   reader actually sees, and REFUSES a port that is not the same site.
   So this can honestly report failure, and does. */
async function portFramework(){
  const fw=prompt(
    'Port this site to a framework you own outright.\n\n'
   +'  next   — React (Next.js)\n'
   +'  astro  — the measured default\n'
   +'  vite   — plain JS\n\n'
   +'The result runs with NO dependency on Framer or Webflow. Every '
   +'page is rendered in a real browser, then a referee compares the '
   +'port against the original and refuses it if they differ.\n\n'
   +'Needs Node installed. Takes several minutes.',
    'next');
  if(!fw)return;
  if(!['astro','next','vite','react'].includes(fw.trim().toLowerCase())){
    alert('Pick one of: react (or next), astro, vite');return;
  }
  S.tab='logs';S.panel=true;renderTab();
  try{
    const {job}=await api('/api/run',
      {project:S.cur,cmd:'convert',framework:fw.trim()});
    const ok=await watchJob(job,'convert');
    const log=S.log||'';
    const line=(log.match(/^(PIXEL-PERFECT PORT READY|CONTENT IDENTICAL[^\n]*|NOT ACCEPTED[^\n]*)/m)||[])[0];
    alert(ok===false
      ? 'The port was REFUSED.\n\n'+(line||'See Logs for the verdict.')
        +'\n\nThat refusal is the feature: a port that does not render '
        +'the same as your site is not handed over. The Logs tab names '
        +'exactly what differed.'
      : 'Port accepted.\n\n'+(line||'')
        +'\n\nIt is in the project folder as convert-'+fw.trim()+'/ — '
        +'a real project you can open, edit and deploy anywhere.');
  }catch(e){alert(e.message)}
}
async function agentHeal(){
  S.tab='logs';renderTab();
  try{
    const {job}=await api('/api/heal',{project:S.cur,agent:true});
    const ok=await watchJob(job,'heal');
    await refresh(true);
    alert(ok===false
      ? 'The AI healer could not clear the checks. The Logs tab lists '
        +'exactly what is still broken — nothing was hidden, and Undo '
        +'reverts everything it did.'
      : 'Healed — verify and probe are clean.');
  }catch(e){alert(e.message)}
}
async function showZeroFx(){
  if(!confirm('These filled entries did NOT take effect in the last '
   +'build (replaced nothing, or the chunks still spell the old text '
   +'so the live page reverts them):\n\n- '
   +(S.zeroFx||[]).join('\n- ')
   +'\n\nRun SELF-HEAL now? Deterministic only — flex matching, '
   +'source casing, nearest-source adoption. Nothing is guessed; '
   +'whatever it can\'t fix safely is reported with the reason. '
   +'(Undo covers it.)'))return;
  try{
    const h=await api('/api/heal',{project:S.cur});
    if(h.stuck>0&&confirm(`${h.stuck} problem(s) the deterministic fixer `
      +`cannot express.\n\nHand them to the AI healer? It reads the `
      +`machine evidence and repairs through the same guarded tools — `
      +`it cannot touch site/ or pristine/, and verify + probe (not the `
      +`model) decide whether it worked. Undo covers everything.`)){
      return agentHeal();
    }
    if(h.healed>0)await runStep('build');
    alert(`self-heal: ${h.healed} fixed, ${h.stuck} need you\n\n`
      +(h.log||'').split('\n').filter(l=>/^(HEALED|STUCK)/.test(l))
        .join('\n').slice(0,1500));
  }catch(e){alert(e.message)}
}

// ---------- design: a screenshot becomes a page, and words change it ----------
// Everything here goes through Aethron's own checks: the page is MEASURED off the
// image, and a change is kept only when every claim about it is measured true.
const DESIGN={name:'',pages:[],log:'',busy:false,wired:false};

async function openDesign(){
  S.view='design';S.cur=null;S.cm=null;S.panel=false;S.tab='';
  {const _c=document.getElementById('content');if(_c)_c.dataset.split='';}
  document.querySelectorAll('.libbtn').forEach(b=>b.classList.remove('sel'));
  const b=$('designbtn');if(b)b.classList.add('sel');
  if(!DESIGN.wired){          // ON THE DOCUMENT, not the element: this view is
    DESIGN.wired=true;        // re-rendered constantly and a per-node handler dies.
    document.addEventListener('paste',ev=>{
      if(S.view!=='design')return;
      const it=[...(ev.clipboardData||{items:[]}).items||[]]
        .find(i=>i.type&&i.type.startsWith('image/'));
      if(it)designNew(it.getAsFile());
    });
  }
  renderSidebar();renderHeader();renderTab();
}

function designSay(t){DESIGN.log=(DESIGN.log?DESIGN.log+'\n':'')+t;
  const el=$('dlog');if(el){el.textContent=DESIGN.log;el.scrollTop=el.scrollHeight;}}

async function designWatch(job){
  let j,seen=0;
  do{
    await new Promise(r=>setTimeout(r,900));
    j=await api('/api/job?id='+job);
    const fresh=(j.log||'').slice(seen);seen=(j.log||'').length;
    if(fresh.trim()){DESIGN.log+=fresh;const el=$('dlog');
      if(el){el.textContent=DESIGN.log;el.scrollTop=el.scrollHeight;}}
  }while(!j.done);
  return j;
}

async function designNew(file){
  if(!file||DESIGN.busy)return;
  DESIGN.busy=true;DESIGN.log='';designSay('reading the screenshot…');
  try{
    const up=await api('/api/image',{data:await b64of(file)});
    const name=(file.name||'page').replace(/\.[a-z]+$/i,'');
    const r=await api('/api/design/new',{image:up.path,name});
    const j=await designWatch(r.job);
    DESIGN.name=r.name;
    if(!j.ok)designSay('\nAethron did not hand this page over — nothing was kept.');
  }catch(e){designSay('failed: '+e.message);}
  DESIGN.busy=false;renderTab();
}

async function designChange(){
  const box=$('dask');const ask=box?box.value.trim():'';
  if(!ask||DESIGN.busy)return;
  DESIGN.busy=true;DESIGN.log='';designSay('writing the tests first…');
  if(box)box.value='';
  try{
    const r=await api('/api/design/change',{name:DESIGN.name,request:ask,budget:0});
    const j=await designWatch(r.job);
    if(j.ok){const f=$('dframe');if(f)f.src='/design/'+DESIGN.name+'/site.html?'+Date.now();}
  }catch(e){designSay('failed: '+e.message);}
  DESIGN.busy=false;
}

async function showWallet(){
  try{
    const w=await api('/api/wallet');const el=$('dwallet');
    if(el)el.textContent='$'+w.left_usd.toFixed(2)+' of $'+w.limit_usd.toFixed(2)+' left';
  }catch(e){}
}
async function designWallet(){
  const w=await api('/api/wallet').catch(()=>null);if(!w)return;
  const v=prompt('How much may Aethron spend on paid model calls, in total?\\n\\n'
    +'$'+w.spent_usd.toFixed(4)+' of $'+w.limit_usd.toFixed(2)+' has been spent over '+w.calls
    +' paid calls. Free calls never touch this.\\n\\nNew ceiling in dollars:', w.limit_usd.toFixed(2));
  if(v===null)return;
  try{await api('/api/wallet',{limit_usd:parseFloat(v)});await showWallet();}
  catch(e){alert(e.message);}
}

async function renderDesign(c){
  c.dataset.split='';
  let r={pages:[]};
  try{r=await api('/api/design/list');}catch(e){}
  DESIGN.pages=r.pages||[];
  if(DESIGN.name&&!DESIGN.pages.some(p=>p.name===DESIGN.name))DESIGN.name='';
  if(!DESIGN.name&&DESIGN.pages.length)DESIGN.name=DESIGN.pages[0].name;
  const esc=s=>String(s).replace(/[<>&"]/g,m=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[m]));
  const tabs=DESIGN.pages.map(p=>`<button class="qrow" style="margin:0;width:auto;
      ${p.name===DESIGN.name?'border-color:var(--acc);color:var(--acc2)':''}"
      onclick="DESIGN.name='${esc(p.name)}';renderTab()">${esc(p.name)}</button>`).join('');
  c.innerHTML=`
  <div style="padding:20px;display:grid;gap:16px;max-width:1120px">
    <div>
      <div style="font-weight:700;font-size:17px;letter-spacing:-.01em">Design from a screenshot</div>
      <p style="color:var(--dim);max-width:74ch;margin:6px 0 0;font-size:13.5px">
        Paste or choose a screenshot. Aethron measures it and writes the page — every colour,
        size and position read off the image rather than guessed. Then ask for changes in your
        own words: the tests are written first, and a change is kept only when the measurements
        prove it and nothing else on the page moved.</p>
      <p style="color:var(--dim);max-width:74ch;margin:6px 0 0;font-size:12.5px">
        Measuring a screenshot takes several minutes — it fits the background layer by layer and
        reads every line of type. The log below shows each stage as it happens.</p>
    </div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <input type="file" id="dfile" accept="image/*" style="display:none"
             onchange="designNew(this.files[0])">
      <button class="primary" onclick="$('dfile').click()">New page from a screenshot</button>
      <span style="color:var(--dim);font-size:12.5px">…or just paste one (⌘V)</span>
      <span style="flex:1"></span>
      <button class="qrow" style="margin:0;width:auto" onclick="designWallet()"
        title="what Aethron may still spend on paid model calls, across every run">
        <span id="dwallet" style="font:12.5px ui-monospace,Menlo,monospace">wallet…</span></button>
    </div>
    ${DESIGN.pages.length?`<div style="display:flex;gap:8px;flex-wrap:wrap">${tabs}</div>`:''}
    ${DESIGN.name?`
      <iframe id="dframe" src="/design/${esc(DESIGN.name)}/site.html"
        style="width:100%;aspect-ratio:1414/858;border:1px solid var(--line);
               border-radius:10px;background:#06050C"></iframe>
      <div style="display:flex;gap:8px">
        <input id="dask" placeholder="what should change? e.g. make the Generate button green"
          style="flex:1;padding:10px 12px;border-radius:9px;border:1px solid var(--line);
                 background:var(--panel2);color:var(--tx);font:inherit"
          onkeydown="if(event.key==='Enter')designChange()">
        <button class="primary" onclick="designChange()">Ask</button>
      </div>`
    :`<div class="empty">no pages yet — paste a screenshot to make one</div>`}
    <pre id="dlog" style="white-space:pre-wrap;font:12px/1.5 ui-monospace,Menlo,monospace;
      color:var(--dim);background:var(--panel2);border:1px solid var(--line);
      border-radius:10px;padding:12px;max-height:340px;overflow:auto;margin:0">${esc(DESIGN.log)}</pre>
  </div>`;
  showWallet();
}

// ---------- design library ----------
async function openLibrary(){
  S.view='library';S.cur=null;S.cm=null;S.panel=false;{const _c=document.getElementById('content');if(_c)_c.dataset.split='';}
  renderSidebar();renderHeader();
  const lb=$('libbtn');if(lb)lb.classList.add('sel');
  renderTab();
}
async function saveToLibrary(){
  try{
    const r=await api('/api/library/save',{project:S.cur});
    alert(`saved "${r.id}" to the design library — `
      +`${(r.card.palette||[]).length} colors, `
      +`${(r.card.fonts||[]).length} fonts, `
      +`${(r.card.sections||[]).length} sections captured`);
  }catch(e){alert(e.message)}
}
function libCard(cd,reason){
  const pal=(cd.palette||[]).slice(0,8)
    .map(x=>`<i title="${esc(x)}" style="background:${esc(x)}"></i>`).join('');
  const f=cd.features||{},ct=cd.counts||{},feats=[];
  if(f.hover_variants)feats.push('hover cards');
  if(f.rotators)feats.push('⟳ text rotator');
  if(f.split_text_runs)feats.push('split text');
  if(f.marquee)feats.push('∞ marquee');
  if(f.appear_animations)feats.push('appear');
  if(f.cms_collections)feats.push(`${f.cms_collections} CMS`);
  feats.push(`${ct.pages||1} page${(ct.pages||1)>1?'s':''}`);
  if(ct.images)feats.push(`${ct.images} images`);
  return `<div class="libcard${reason?' hit':''}">
   <div class="lc-top"><b>${esc(cd.id)}</b>
    <span class="plat ${esc(cd.platform||'static')}">${esc(cd.platform||'?')}</span></div>
   ${cd.title||cd.description
     ?`<div class="lc-title">${esc(cd.description||cd.title)}</div>`:''}
   <div class="lc-pal">${pal}</div>
   ${(cd.fonts||[]).length
     ?`<div class="lc-fonts">${esc(cd.fonts.slice(0,3).join(' · '))}</div>`:''}
   <div class="lc-feats">${feats.map(x=>`<span>${x}</span>`).join('')}</div>
   ${reason?`<div class="lc-why">${esc(reason)}</div>`:''}
   <div class="lc-btns">
    <button class="primary" onclick="startFromLibrary('${esc(cd.id)}')">${I('rocket')}Start from this</button>
    <button title="remove the card (the project itself is untouched)"
     onclick="delLibrary('${esc(cd.id)}')">✕</button></div></div>`;
}
async function renderLibrary(c){
  c.dataset.split='';        // this view replaces #content — #conv dies
  let cards=[];
  try{cards=await api('/api/library')}catch(e){}
  S.libCards=cards;
  c.innerHTML=`<div class="libwrap">
   <div class="libmatch">
    <textarea id="libplan" rows="3" placeholder="describe the new project — brand, industry, tone, must-have sections… the AI ranks your saved designs by fit">${esc(S.libPlan||'')}</textarea>
    <button class="primary" onclick="matchLibrary()">${I('sparkles')}Match my plan</button>
   </div>
   <div class="hint" style="margin-top:8px">cards hold only design
    fingerprints (palette, fonts, motion, structure) — never template
    files. Start-from re-imports from the card's source URL or your own local
    project, so a shared library stays license-clean.</div>
   <div id="libmatches"></div>
   <div class="libgrid">${cards.map(cd=>libCard(cd)).join('')
     ||'<div class="empty" style="grid-column:1/-1">library is empty — open a project and hit Save design</div>'}</div></div>`;
}
async function matchLibrary(){
  const plan=$('libplan').value.trim();S.libPlan=plan;
  const btn=document.querySelector('.libmatch .primary');
  btn.disabled=true;btn.textContent='matching…';
  try{
    const r=await api('/api/ai/match',{plan,...aiCfg()});
    $('libmatches').innerHTML=
     `<h3 style="margin:18px 0 2px;color:var(--acc2)">top matches</h3>
      <div class="libgrid">`+r.matches.map(m=>{
        const cd=S.libCards.find(c=>c.id===m.id);
        return cd?libCard(cd,(m.score!=null?m.score+'% — ':'')+(m.reason||'')):'';
      }).join('')+'</div>';
  }catch(e){alert(e.message)}
  btn.disabled=false;btn.innerHTML=I('sparkles')+'Match my plan';
}
async function startFromLibrary(id){
  const name=prompt(`new project name (built from "${id}"):`);
  if(!name)return;
  try{
    const r=await api('/api/library/start',{id,name});
    await select(r.name);
    alert(`project "${r.name}" created — hit Prepare project, then
write your plan and fill.`);
  }catch(e){alert(e.message)}
}
async function delLibrary(id){
  if(!confirm(`Remove "${id}" from the library? (the project itself is untouched)`))return;
  try{await api('/api/library/delete',{id});renderTab();}
  catch(e){alert(e.message)}
}

// ---------- code: the IDE + the coding agent ----------
// The agent is a PROCESS behind a stable event contract (see
// aethron_code.py). This UI never talks to a model — it talks to
// Aethron, which drives whichever runtime the user configured.
const CODE={key:'',since:0,events:[],tree:[],file:'',dirty:false,
            poll:0,status:null,showai:false,ws:{project:'',workspace:''}};

/* (the old openCode lived here and replaced the whole view; the sheet
   version above supersedes it — two definitions and the later one wins,
   which is a coin toss nobody should be making at read time.) */
const wsq=()=>CODE.ws.project?'project='+encodeURIComponent(CODE.ws.project)
                             :'workspace='+encodeURIComponent(CODE.ws.workspace);
const wsBody=o=>Object.assign({},CODE.ws,o||{});
const wsName=()=>CODE.ws.project||CODE.ws.workspace||'';

async function renderCode(c){
  c.dataset.split='';        // this view replaces #content — #conv dies
  if(!CODE.status)c.innerHTML='<div class="empty">opening the workspace…</div>';
  CODE.status=await api('/api/code/status?key='+encodeURIComponent(CODE.key));
  if(!CODE.status.available){
    c.innerHTML=`<div class="pane"><h3>Coding layer unavailable</h3>
      <div class="hint">${esc(CODE.status.why||'aethron_code.py failed to load')}</div></div>`;
    return;
  }
  const st=CODE.status,rt=st.runtimes['claude-code'],cfg=st.config;
  const prov=st.provider||{};
  const wsOpts=[...S.projects.map(p=>`<option value="p:${esc(p.name)}"
      ${CODE.ws.project===p.name?'selected':''}>${esc(p.name)} · project</option>`),
    ...(st.workspaces||[]).map(w=>`<option value="w:${esc(w)}"
      ${CODE.ws.workspace===w?'selected':''}>${esc(w)} · workspace</option>`)].join('');
  c.innerHTML=`
  <div class="codebar">
    <select id="cws" onchange="pickWs(this.value)">
      <option value="">— choose a workspace —</option>${wsOpts}</select>
    <button onclick="newWorkspace()">${I('plus')}New workspace</button>
    <span class="sep"></span>
    ${CODE.key?`<button class="danger" onclick="stopCode()">Stop session</button>`
              :`<button class="primary" ${prov.ready?'':'disabled'}
                 onclick="startCode()">${I('play')}Start session</button>`}
    <button onclick="CODE.showai=!CODE.showai;renderTab()">${I('sparkles')}AI settings</button>
    <span class="codestat">${rt.available
      ? esc(rt.version)+' · '+esc(prov.why||prov.label||'')
      : '<b>'+esc(rt.why)+'</b>'}</span>
  </div>
  ${CODE.showai||!prov.ready?`<div class="card" style="margin:12px 14px 0">
    <h3>AI — one key for everything</h3>${aiSettingsHtml()}
    ${prov.ready?'':`<div class="hint" style="color:var(--err)">${esc(prov.why||'')}</div>`}
    </div>`:''}
  <div class="ide">
    <div class="idetree" id="idetree"></div>
    <div class="ideedit">
      <div class="idehead"><span id="idefile">no file open</span>
        <button id="idesave" onclick="saveFile()" disabled>Save</button></div>
      <textarea id="ideta" spellcheck="false" oninput="CODE.dirty=true;$('idesave').disabled=false"
        placeholder="pick a file from the tree"></textarea>
    </div>
    <div class="idechat">
      <div class="chatlog" id="chatlog"></div>
      <div class="chatin">
        <textarea id="chatta" rows="3" placeholder="${CODE.key
          ?'ask the agent to build, fix or explain something…'
          :'start a session first'}"
          onkeydown="if(event.key==='Enter'&&(event.metaKey||event.ctrlKey))sendCode()"></textarea>
        <button class="primary" onclick="sendCode()">${I('send')}Send</button>
      </div>
    </div>
  </div>`;
  if(wsName())loadTree();
  bindAiSettings();renderChat();fitIde();
  if(CODE.key&&!CODE.poll)CODE.poll=setInterval(pollCode,900);
}
// The IDE must end exactly at the bottom of the window: guessing the
// chrome height once put the chat input 33px below an unscrollable
// fold — invisible, and the whole feature looked broken.
function fitIde(){
  const e=document.querySelector('.ide');if(!e)return;
  if(innerWidth<1100){e.style.height='';return;}   // stacked: page scrolls
  e.style.height='0px';   // measure the top with the pane collapsed
  e.style.height=Math.max(300,innerHeight-e.getBoundingClientRect().top-14)+'px';
}
addEventListener('resize',fitIde);
function pickWs(v){
  CODE.ws=v.startsWith('p:')?{project:v.slice(2),workspace:''}
        :v.startsWith('w:')?{project:'',workspace:v.slice(2)}:{project:'',workspace:''};
  CODE.file='';CODE.key='';CODE.events=[];CODE.since=0;renderTab();
}
async function newWorkspace(){
  const name=prompt('name for the new coding workspace');
  if(!name)return;
  try{const r=await api('/api/workspaces',{name});
    CODE.ws={project:'',workspace:r.workspace};renderTab();}
  catch(e){alert(e.message)}
}
async function saveCodeCfg(code){
  try{await api('/api/code/config',{code});}catch(e){alert(e.message)}
}
async function loadTree(){
  if(!document.getElementById('idetree'))return;
  try{
    const r=await api('/api/fs/tree?'+wsq());
    CODE.tree=r.files;
    $('idetree').innerHTML=`<div class="treehead">${esc(r.root)}</div>`
      +r.files.map(f=>`<div class="tfile${CODE.file===f.path?' sel':''}"
        onclick="openFile('${esc(f.path).replace(/'/g,"\\\\'")}')"
        title="${esc(f.path)}${f.locked?' — generated/sealed, read-only':''}">
        ${f.locked?I('lock',12):I('file',12)}<span>${esc(f.path)}</span></div>`).join('');
  }catch(e){$('idetree').innerHTML=`<div class="hint">${esc(e.message)}</div>`}
}
async function openFile(path){
  try{
    const r=await api('/api/fs/read?'+wsq()+'&path='+encodeURIComponent(path));
    CODE.file=path;CODE.dirty=false;
    $('idefile').innerHTML=esc(path)+(r.locked
      ?' <span class="lockchip">read-only — generated output</span>':'');
    const ta=$('ideta');
    ta.value=r.binary?'(binary file)':r.too_big?'(file too large to edit here)':r.text;
    ta.readOnly=!!(r.locked||r.binary||r.too_big);
    $('idesave').disabled=true;
    loadTree();
  }catch(e){alert(e.message)}
}
async function saveFile(){
  try{
    await api('/api/fs/write',wsBody({path:CODE.file,text:$('ideta').value}));
    CODE.dirty=false;$('idesave').disabled=true;
  }catch(e){alert(e.message)}
}
async function startCode(){
  if(!wsName())return alert('choose a workspace first');
  try{
    const r=await api('/api/code/start',wsBody({}));
    CODE.key=r.key;CODE.events=[];CODE.since=0;
    renderTab();
  }catch(e){alert(e.message)}
}
async function stopCode(){
  try{await api('/api/code/stop',{key:CODE.key})}catch(e){}
  clearInterval(CODE.poll);CODE.poll=0;CODE.key='';renderTab();
}
/* PASTE A SCREENSHOT STRAIGHT IN.
   A screenshot lives on the clipboard for a few seconds. Asking someone
   to save it, find it and type its path is how a feature goes unused —
   most people take a shot and paste, they never file it. The browser is
   already holding the bytes, so take them, store them once by content
   hash, and put the resulting path in the message where the agent can
   act on it.
   Wired on DOCUMENT, not on the elements: the composers are re-rendered
   whenever the view changes, and per-element listeners would silently
   stop working after the first navigation. */
const IMG_TAS=['npurl','chatta'];
function imgNote(ta,msg,busy){
  const row=ta.closest('.startrow')||ta.parentElement;
  let n=row.parentElement.querySelector('.imgnote');
  if(!n){ n=document.createElement('div'); n.className='imgnote';
    n.style.cssText='font-size:12px;color:var(--dim);margin-top:6px;'
      +'display:flex;align-items:center;gap:7px';
    row.parentElement.insertBefore(n,row.nextSibling); }
  n.textContent=(busy?'· ':'')+msg;
  return n;
}
async function attachImage(file,ta){
  if(!file)return;
  imgNote(ta,'reading '+(file.name||'pasted image')+'…',true);
  try{
    const b64=await new Promise((res,rej)=>{
      const r=new FileReader();
      r.onload=()=>res(String(r.result)); r.onerror=()=>rej(new Error('unreadable'));
      r.readAsDataURL(file);
    });
    const r=await api('/api/image',{data:b64});
    const cur=ta.value.trim();
    ta.value=(cur?cur+'\n':'')+r.path;
    growTa(ta); ta.focus();
    imgNote(ta,'attached '+r.name+' ('+Math.round(r.bytes/1024)+' KB) — '
      +'its path is in the message');
  }catch(e){ imgNote(ta,'could not attach that image: '+e.message); }
}
function activeImgTa(t){
  if(t&&t.id&&IMG_TAS.indexOf(t.id)>=0)return t;
  for(const id of IMG_TAS){ const e=$(id); if(e&&e.offsetParent!==null)return e; }
  return null;
}
document.addEventListener('paste',ev=>{
  const ta=activeImgTa(ev.target); if(!ta)return;
  const items=(ev.clipboardData&&ev.clipboardData.items)||[];
  for(let i=0;i<items.length;i++){
    if(items[i].kind==='file'&&/^image\//.test(items[i].type)){
      ev.preventDefault(); attachImage(items[i].getAsFile(),ta); return;
    }
  }
});
document.addEventListener('dragover',ev=>{
  if(ev.dataTransfer&&Array.prototype.indexOf.call(
      ev.dataTransfer.types||[],'Files')>=0&&activeImgTa(ev.target))
    ev.preventDefault();
});
document.addEventListener('drop',ev=>{
  const ta=activeImgTa(ev.target); if(!ta)return;
  const f=(ev.dataTransfer&&ev.dataTransfer.files||[])[0];
  if(f&&/^image\//.test(f.type)){ ev.preventDefault(); attachImage(f,ta); }
});
async function sendCode(){
  const ta=$('chatta'),text=ta.value.trim();
  if(!text)return;
  if(!CODE.key)return alert('start a session first');
  ta.value='';
  CODE.events.push({type:'you',text});renderChat();
  CODE.lastEv=Date.now();
  try{await api('/api/code/send',{key:CODE.key,text});}
  catch(e){CODE.events.push({type:'error',text:e.message});renderChat();}
}
/* One place stops a turn, so the log, the composer and the field can
   never disagree about whether something is still happening. */
function endTurn(){
  if(CODE.poll){clearInterval(CODE.poll);CODE.poll=0;}
  renderChat(); syncComposer(); syncHero();
}
/* NOTHING SHOULD EVER SPIN FOREVER WITH NOTHING TO SHOW. The key bug is
   fixed below, but "silent and endless" is the wrong failure mode for
   ANY cause — a wedged child, a provider that never answers, a socket
   that went away. If a turn produces no event at all for this long, say
   so and stop, rather than leaving a person watching an orb. */
const STALL_MS=90000;
async function pollCode(){
  if(!CODE.key)return;
  if(CODE.poll){
    CODE.lastEv=CODE.lastEv||Date.now();
    if(Date.now()-CODE.lastEv>STALL_MS){
      CODE.events.push({type:'error',text:
        'No response from the provider for 90 seconds, so Aethron stopped '
        +'waiting. Nothing was changed. If you just saved a new key, send '
        +'the message again \u2014 this one was still on the old session.'});
      endTurn(); return;
    }
  }
  try{
    const r=await api('/api/code/events?key='+encodeURIComponent(CODE.key)
                      +'&since='+CODE.since);
    if(r.events&&r.events.length){
      CODE.events.push(...r.events);CODE.since=r.n;CODE.lastEv=Date.now();
      renderChat();
      if(r.events.some(e=>['tool_result','done'].includes(e.type)))loadTree();
      /* AN ERROR ENDS THE TURN, AND THE UI MUST NOT WAIT TO BE TOLD.
         A wedged session can keep reporting running:true forever — after
         a 429 it did — so the orb kept spinning, the card kept saying
         Running and the button stayed a stop long after the reply had
         failed. The client ends its own turn the moment a done, error or
         exit arrives; the server catching up later changes nothing. */
      if(r.events.some(e=>['done','error','exit'].includes(e.type)))
        endTurn();
    }
    if(!r.running&&CODE.key){
      clearInterval(CODE.poll);CODE.poll=0;
      /* AND REDRAW. The live line ("Thinking", the orb, the step count)
         is part of the LOG, and renderChat only ran when new events
         arrived — so the final poll, the one that discovers the turn is
         over, cleared the flag and left the line on screen until
         something unrelated happened to re-render. That is why it sat
         there after the reply had already landed. */
      renderChat();
    }
    syncComposer();            // stop becomes send the moment it is over
  }catch(e){}
}

/* A PROJECT IS NOT A DIFFERENT KIND OF SCREEN. Selecting one used to
   swap the conversation for a control panel: six tabs, a plan textarea,
   forbidden words, hide selectors, an AI-settings block. That is a
   settings page, not a product. The conversation continues; the forms
   are still there, one tab away, for when you want to reach in by hand.
   Rooting the session in the project dir also means forge.json is
   present, so PROJECT_RULES and the site/ + pristine/ write-deny come
   into force automatically. */
/* ONE SHELL, EVERY STATE. The welcome, the project conversation and the
   coding session are the same surface with different opening words —
   because to the person using it they ARE the same thing: say what you
   want, watch it happen. The composer lives in its own dock at the
   bottom and is never pushed anywhere by what is above it. */
function convShell(o){
  o=o||{};
  return `<div class="conv-shell${CODE.events.length?' talking':''}" id="convshell">
    <canvas class="dotf hero"></canvas>
    <div class="conv-scroll" id="convscroll">
      <div class="conv-wrap">
        ${o.top||''}
        <div id="welcome" ${CODE.events.length?'hidden':''}>
          <h1>${o.title||''}</h1>
          <p class="sub">${o.sub||''}</p>
        </div>
        <div id="chatlog" class="clog" ${CODE.events.length?'':'hidden'}></div>
      </div>
    </div>
    <div class="conv-dock"><div class="conv-wrap">
      <div class="composer" data-beam="ae"><span data-beam-bloom></span>
        <textarea id="npurl" rows="1" placeholder="${esc(o.hint||'')}"
         oninput="growTa(this)"
         onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();consoleSend()}"></textarea>
        <div class="cbar">
          <label class="cpill" for="npfile"
           title="an export, a zip, or pages you saved separately">${I('file',13)}attach</label>
          <button class="cpill opt" onclick="openSettings()"
           title="change the model or the key">${I('key',13)}<span
           class="cv" id="cbarmodel">model</span></button>
          ${CODE.poll
            ?`<button class="cbtn stop" onclick="stopTurn()" aria-label="Stop"
                title="Stop">${I('stop',15)}</button>`
            :`<button class="cbtn" onclick="consoleSend()" aria-label="Send"
                title="Send">${I('up',15)}</button>`}
        </div>
      </div>
      <div class="dockmeta" id="dockmeta"></div>
      ${o.extra||''}
      <div class="qrows" id="qrows" ${CODE.events.length?'hidden':''}>${
        (o.quick||[]).map(q=>`<button class="qrow" onclick="quick(${
          JSON.stringify(q.ask).replace(/"/g,'&quot;')},${
          JSON.stringify(q.need||'').replace(/"/g,'&quot;')})">
          <span data-ic="${q.icon}"></span><span class="ql">${esc(q.label)}</span>
          <span class="qc" data-ic="up"></span></button>`).join('')}</div>
    </div></div></div>`;
}
/* ══ SETTINGS ═══════════════════════════════════════════════════════
   Who you are, what Aethron is using to think, and what it has spent.
   The key switcher is here deliberately and temporarily: while this is
   being driven hard by its owner, changing provider or key must take
   one click and no restart. It comes out when the hosted gateway lands
   and nobody has to hold a key at all. */
async function renderSettings(c){
  c.dataset.split='';c.classList.remove('conv-host');c.classList.add('set-host');
  renderHeader&&renderHeader();
  c.innerHTML='<div class="empty"><div class="focal">'
    +'<h1>Settings</h1><div class="hint">loading…</div></div></div>';
  let me={},wallet={};
  try{me=await api('/api/auth/me')}catch(e){}
  try{wallet=await api('/api/wallet')}catch(e){}
  const acc=me&&(me.email||me.user||me.id),
        plan=(me&&me.plan)||'beta';
  const left=wallet&&wallet.left_usd, lim=wallet&&wallet.limit_usd,
        spent=wallet&&wallet.spent_usd;
  c.innerHTML=`<div class="setwrap">
    <h1 class="seth">Settings</h1>

    <section class="card">
      <h3>${I('user',14)} Profile</h3>
      <div class="kv"><span>Signed in as</span><b>${esc(acc||'not signed in')}</b></div>
      <div class="kv"><span>Plan</span><b>${esc(plan)}</b></div>
      <div class="kv"><span>Projects</span><b>${(S.projects||[]).length}</b></div>
      ${acc?`<div class="rowbtns"><button onclick="signOut()">Sign out</button></div>`
           :`<div class="hint">This copy is running without the cloud layer,
              so nothing about you leaves this machine.</div>`}
    </section>

    <section class="card">
      <h3>${I('key',14)} Model and key</h3>
      <div class="hint">Whatever you set here powers everything — rebranding
        copy, writing code, measuring a change. Switch provider or key at any
        time; nothing needs restarting.</div>
      ${aiSettingsHtml()}
    </section>

    <section class="card">
      <h3>${I('coins',14)} Spending</h3>
      ${typeof left==='number'?`
        <div class="kv"><span>Ceiling</span><b>$${Number(lim).toFixed(2)}</b></div>
        <div class="kv"><span>Spent</span><b>$${Number(spent).toFixed(4)}</b></div>
        <div class="kv"><span>Left</span><b>$${Number(left).toFixed(4)}</b></div>
        <div class="meter"><i style="width:${Math.max(0,Math.min(100,
           (Number(spent)/Math.max(0.0001,Number(lim)))*100)).toFixed(1)}%"></i></div>
        <div class="rowbtns">
          <input id="wlimit" type="number" step="0.5" min="0"
            value="${Number(lim).toFixed(2)}" style="width:110px">
          <button onclick="setWallet()">Set ceiling</button></div>
        <div class="hint">A ceiling that survives restarts. It is checked
          BEFORE a paid call starts and counted down by the real bill, not an
          estimate — free keys never touch it.</div>`
      :`<div class="hint">No wallet is configured for this copy.</div>`}
    </section>
  </div>`;
  bindAiSettings();
}
async function setWallet(){
  const v=parseFloat(($('wlimit')||{}).value);
  if(!(v>=0))return;
  try{await api('/api/wallet',{limit_usd:v});renderTab();}
  catch(e){alert(e.message)}
}
async function signOut(){
  try{await api('/api/auth/logout',{})}catch(e){}
  location.reload();
}
/* ── LEAVING A VIEW IS PART OF ENTERING ONE ─────────────────────────
   Every opener sets S.view — settings, library, code, design — and the
   New project button set everything EXCEPT that, so from Settings it
   cleared the project, re-rendered, and renderTab() saw S.view still
   reading 'settings' and put you straight back. The button looked
   dead. It was doing its job and being overruled by one stale field.
   Everything that returns to the front door goes through here now. */
function newProject(){
  S.view=''; S.cur=null; S.cm=null; S.panel=false; S.tab='chat';
  closePvw&&closePvw();
  const c=document.getElementById('content');
  if(c)c.dataset.split='';
  document.body.classList.remove('pvwopen');
  refresh();
}
function openSettings(){S.view='settings';S.cur=null;S.panel=false;renderTab();renderSidebar&&renderSidebar();}

/* ── THE PROJECT, VISIBLE ─────────────────────────────────────────
   Selecting a project used to show a conversation about a thing you
   could not see. The site is BUILT and already served from this origin
   at /edit/<name>/, so the card shows the real page — not a screenshot
   of it, not a placeholder — scaled down and inert, with the two doors
   a person actually wants: look at it properly, or point at something
   and change it. That second one is how hand-editing is reached now;
   before this it was a tab you had to already know about. */
function projectCard(){
  if(!S.cur)return '';
  const i=S.info||{}, plat=(i.platform||'').toUpperCase(),
        n=i.strings||0, f=i.filled||0,
        pages=(i.pages&&i.pages.length)||0;
  return `<div class="pcard glass">
    <div class="pshot" onclick="openPreviewPane(false)" title="open the preview">
      <iframe src="/edit/${encodeURIComponent(S.cur)}/" tabindex="-1"
        scrolling="no" aria-hidden="true"></iframe>
      <span class="pveil"></span>
    </div>
    <div class="pside">
      <div class="pname">${esc(S.cur)}</div>
      <div class="pmeta">${plat?`<span class="tag">${esc(plat)}</span>`:''}
        ${n?`<span>${f} of ${n} strings filled</span>`:''}
        ${pages?`<span>&middot; ${pages} page${pages>1?'s':''}</span>`:''}</div>
      <div class="pacts">
        <button class="cpill" onclick="openPreviewPane(false)">${I('play',13)}Preview</button>
        <button class="cpill" onclick="openPreviewPane(true)">${I('pencil',13)}Point and edit</button>
        <button class="cpill opt" onclick="openInBrowser()">${I('compass',13)}Open in browser</button>
      </div>
    </div></div>`;
}
/* ── THE PREVIEW SHEET ─────────────────────────────────────────────
   Inside Aethron, over the conversation. Browse behaves like a browser;
   Select arms the picker that already ships in the edit mount — the one
   that walks the full elementsFromPoint stack, so a thing sitting behind
   a transparent overlay is still reachable, which is most of a Framer
   page. */
let PVW={edit:false,pick:null};
/* ── ONE SHEET, MANY CONTENTS ───────────────────────────────────────
   The preview taught the shape: the conversation keeps the left third
   and stays live, the work opens beside it, Escape and Close get you
   out. Anything that used to be a separate MODE belongs here instead —
   a mode takes the whole window and takes the conversation with it,
   which is the thing that made this product feel like several
   applications wearing one sidebar. */
function openSheet(o){
  closePvw();
  const d=document.createElement('div');
  d.className='pvw'; d.id='pvw';
  d.innerHTML=`<div class="pvw-bar glass">
      <span class="pvw-name">${esc(o.name||'')}<span>${esc(o.sub||'')}</span></span>
      ${o.bar||''}
      <span class="sp"></span>
      ${o.actions||''}
      <button class="cpill" onclick="closePvw()">${I('x',13)}Close</button>
    </div>
    <div class="pvw-stage${o.dark?' dark':''}" id="pvwstage">${o.body||''}</div>`;
  document.body.appendChild(d);
  let opened=false;
  const open=()=>{ if(opened)return; opened=true;
    d.classList.add('in'); document.body.classList.add('pvwopen');
    syncHero(); backWithGlass(d); };
  requestAnimationFrame(()=>requestAnimationFrame(open));
  setTimeout(open,120);
  document.addEventListener('keydown',pvwKey);
  if(o.onMount)o.onMount(document.getElementById('pvwstage'));
  return d;
}
function openPreviewPane(edit){
  if(!S.cur)return;
  PVW.edit=!!edit;
  const d=openSheet({
    name:S.cur, sub:'the built site',
    bar:`<div class="pvw-seg" id="pvwseg">
        <button class="${edit?'':'on'}" onclick="pvwMode(false)">Browse</button>
        <button class="${edit?'on':''}" onclick="pvwMode(true)">Select</button>
      </div>`,
    actions:`<button class="cpill opt" onclick="openInBrowser()">${
        I('compass',13)}Open in browser</button>`,
    body:`<iframe id="pvwframe" src="/edit/${encodeURIComponent(S.cur)}/"></iframe>
      <div class="selring" id="selring"></div>
      <div class="pvw-hint" id="pvwhint"></div>`});
  const f=$('pvwframe');
  if(f)f.onload=()=>pvwMode(PVW.edit);
}
/* THE CODE WORKSPACE IS A SHEET NOW, NOT A PLACE YOU GO. It used to
   replace the whole view, so opening it meant leaving the conversation
   — the same fault the preview had. The files open beside the chat and
   close again without losing anything. */
async function openCode(){
  if(S.cur)CODE.ws={project:S.cur,workspace:''};
  openSheet({name:'Code workspace',
    sub:S.cur||'a fresh workspace', dark:true,
    body:'<div class="empty">opening the workspace\u2026</div>',
    onMount:async stage=>{ try{ await renderCode(stage); }
      catch(e){ stage.innerHTML='<div class="pane"><h3>Could not open</h3>'
        +'<div class="hint">'+esc(e.message)+'</div></div>'; } }});
}
function pvwKey(e){if(e.key==='Escape'){if($('selbox'))closeSel();else closePvw();}}
function closePvw(){
  const d=$('pvw');
  document.body.classList.remove('pvwopen');
  syncHero(); clearGlass();
  closeSel(); document.removeEventListener('keydown',pvwKey);
  if(!d)return;
  d.classList.remove('in');            // let it spring out before it goes
  const gone=()=>d.remove();
  d.addEventListener('transitionend',gone,{once:true});
  setTimeout(gone,800);                // never leave it stranded
}
function pvwMode(edit){
  PVW.edit=!!edit; closeSel();
  const f=$('pvwframe'); if(!f||!f.contentWindow)return;
  f.contentWindow.postMessage({forge:'mode',picking:!!edit,hover:'freeze'},'*');
  const seg=$('pvwseg');
  if(seg)[...seg.children].forEach((b,i)=>b.classList.toggle('on',(i===1)===!!edit));
  const h=$('pvwhint');
  if(h)h.textContent=edit
    ?'Click anything on the page \u2014 text, an image, a whole section'
    :'Browsing. Switch to Select to change something.';
}
/* WHAT WAS PICKED, IN WORDS. The picker hands back the element plus six
   ancestors; a person does not want a CSS selector, and neither does the
   change path — it locates by the words an element contains, which is
   what survives a rebuild. */
function pickLabel(m){
  if(m.kind==='images'){
    const u=(m.srcs&&m.srcs[0])||''; 
    return {what:'this image',ref:'the image '+u.split('/').pop().split('?')[0]};
  }
  if(m.kind==='container'){
    const e=(m.el&&m.el[0])||{};
    const n=e.name||e.id||(e.classes||[]).slice(0,2).join('.')||e.tag||'section';
    return {what:'this section',ref:'the '+(e.tag||'section')+' "'+n+'"'};
  }
  const t=(m.text||'').trim().replace(/\s+/g,' ');
  return {what:t.slice(0,48)||'this text',ref:'the text that says "'+t.slice(0,120)+'"'};
}
window.addEventListener('message',ev=>{
  const m=ev.data;
  if(m&&m.forge==='hover'&&$('selring')){
    const ring=$('selring'), st=$('pvw').querySelector('.pvw-stage');
    if(!m.rect){ring.classList.remove('on');return;}
    const b=st.getBoundingClientRect(), R=m.rect;
    ring.style.left=R.l+'px'; ring.style.top=R.t+'px';
    ring.style.width=(R.r-R.l)+'px'; ring.style.height=(R.b-R.t)+'px';
    ring.classList.add('on');
    return;
  }
  if(!m||m.forge!=='pick'||!$('pvw'))return;
  PVW.pick=m;
  const L=pickLabel(m);
  closeSel();
  const b=document.createElement('div');
  b.className='selbox glass'; b.id='selbox';
  b.innerHTML=`<div class="what">${I('pencil',12)}<b>${esc(L.what)}</b></div>
    <textarea id="selta" placeholder="Say what should change here\u2026"></textarea>
    <div class="row"><span class="sp"></span>
      <button class="ghost" onclick="closeSel()">Cancel</button>
      <button class="send" onclick="selSend()">Ask Aethron</button></div>`;
  document.body.appendChild(b);
  /* anchored to the click, then pulled back inside the window */
  /* BESIDE THE THING, NOT ON TOP OF IT. Anchoring to the cursor put the
     box over the element a person had just chosen, which is the one
     thing it must never cover. Sit under the element's own box when
     there is room, above it when there is not. */
  const st=$('pvw').querySelector('.pvw-stage').getBoundingClientRect();
  const R=m.rect, H=200;
  const x=st.left+(R?(R.l+R.r)/2:(m.x||st.width/2));
  let y=R?st.top+R.b+12:st.top+(m.y||st.height/2)+14;
  if(y+H>innerHeight-12&&R)y=st.top+R.t-H-4;
  b.style.left=Math.max(12,Math.min(innerWidth-352,x-170))+'px';
  b.style.top=Math.max(12,Math.min(innerHeight-H+10,y))+'px';
  const ta=$('selta'); if(ta)ta.focus();
});
function closeSel(){const b=$('selbox'); if(b)b.remove();}
/* ── ASK THE WINDOW FOR REAL GLASS UNDER THIS RECTANGLE ─────────────
   Only inside the native shell, and only once the sheet has finished
   arriving — a native view cannot ride a CSS spring, so moving it early
   would show a slab of glass sliding in half a beat out of step. The
   page turns its own background off at the same moment, otherwise the
   material is behind an opaque div and nobody sees it. */
const NATIVE=document.documentElement.classList.contains('native-glass');
async function backWithGlass(el){
  if(!NATIVE||!el)return;
  const r=el.getBoundingClientRect();
  try{
    await api('/api/glass',{x:Math.round(r.left),y:Math.round(r.top),
      w:Math.round(r.width),h:Math.round(r.height),radius:0});
    el.classList.add('onglass');
  }catch(e){}
}
async function clearGlass(){
  if(!NATIVE)return;
  try{ await api('/api/glass',{}); }catch(e){}
}
/* the rectangle moves with the window */
addEventListener('resize',()=>{
  clearTimeout(window._glassT);
  window._glassT=setTimeout(()=>{
    const d=$('pvw'); if(d&&d.classList.contains('in'))backWithGlass(d);
  },200);
});

/* ══ METAL ══════════════════════════════════════════════════════════
   metal-fx's engine arrives on its own <script defer>; everything here
   is Aethron deciding WHERE it goes. Three places, all of them things
   the owner pointed at: the send button, a "Live mode · New" badge for
   when something new turns up, and the search.

   Every one of them is a working control with no engine at all — the
   ring is paint, never the affordance. A browser without WebGL2, or a
   metal.js that never loaded, loses the shine and loses nothing else. */
const MET=[];                       /* live handles, swept when orphaned */
/* EVERY RENDER PATH, NOT A LIST OF THEM. The composer is drawn by the
   static shell, by convShell, by renderCode and by renderDesign; the
   sidebar redraws on its own schedule; a run finishing redraws the chat.
   Hooking each one is how a surface silently stops being decorated the
   day a new view is added — the same reason this file's own listeners
   sit on document rather than on elements. One observer, debounced. */
let _metT=0;
function watchMetal(){
  const seen=new MutationObserver(()=>{
    clearTimeout(_metT);
    _metT=setTimeout(()=>{mountMetal();playArrivals();},60);
  });
  for(const id of ['content','plist','findhits']){
    const n=document.getElementById(id);
    if(n)seen.observe(n,{childList:true,subtree:true});
  }
  const dock=document.querySelector('.conv-dock');
  if(dock)seen.observe(dock,{childList:true,subtree:true});
}
function mountMetal(){
  if(!window.metalWrap)return;      /* not loaded yet — a later pass gets it */
  /* An element re-rendered away takes its wrapper with it, but the
     shared renderer would keep compositing for an instance nobody can
     see. Sweep first, mount second. */
  for(let i=MET.length-1;i>=0;i--)
    if(!document.body.contains(MET[i].root)){try{MET[i].destroy()}catch(e){}
      MET.splice(i,1);}

  const send=document.querySelector('.cbar .cbtn:not(.stop)');
  if(send&&!send.__mfx){
    /* 11, not 9: `.composer .cbtn` wins over `.cbar .cbtn` on equal
       specificity, so the button is drawn 34px at radius 11. A ring
       measured from the wrong rule reads as a ring that does not fit. */
    const h=window.metalWrap(send,{variant:'button',preset:'chromatic',
      theme:'dark',borderRadius:11,innerShadow:true,
      /* the owner's actual point: the light is in the room, not only on
         the button — the pills beside it catch the metal */
      reflect:[...document.querySelectorAll('.cbar .cpill')]
                .map(el=>({el,strength:.7}))});
    if(h)MET.push(h);
  }
  const q=$('findq');
  if(q&&!q.__mfx){
    const h=window.metalWrap(q,{variant:'button',preset:'silver',theme:'dark',
      borderRadius:999,strength:.85,ringCssPx:1});
    if(h)MET.push(h);
  }
  /* THE SELECTOR MUST NOT MATCH WHAT THE MOUNT CREATES. `.mbadge > div`
     matched the badge host on the first pass — and on the second pass it
     matched the WRAPPER the first pass had inserted, which is also a
     div, so every observer tick wrapped the wrapper: measured, 33 nested
     rings inside one badge. A dedicated class plus the handle guard, and
     the pass is idempotent. */
  document.querySelectorAll('.mb-host').forEach(host=>{
    if(host.__mfx)return;
    const R=55.556*(+host.dataset.k||1);
    const h=window.metalWrap(host,{preset:'chromatic',theme:'dark',
      strength:.8,shaderScale:1.6,borderRadius:R,glowMode:'ring',
      /* a badge is metal ALL OVER, not a ring around a hole — their
         MetalBadge does this with a full-pill mask, so this does too */
      mask:(ctx,w,hh,dpr)=>{ctx.beginPath();ctx.roundRect(0,0,w,hh,R*dpr);ctx.fill();}});
    if(h){h.root.style.background='#fff';MET.push(h);}
  });
}

/* metal-fx's MetalBadge, in plain DOM. The metrics are theirs, from the
   Figma their file cites: 45×25, r 55.556, label Inter 600 12.222/1.4
   #323232, and the layer order white fill → metal → white core under the
   words → gradient and inset rims → text. */
function metalBadgeHtml(text,k){
  k=k||1;
  const W=45*k,H=25*k,R=55.556*k,TW=26.667*k,pad=(W-TW)/2,
        core={r:46,blur:100,a:.94,size:49},g=.41;
  return `<span class="mbadge"><div class="mb-host" data-k="${k}" style="position:relative;`+
    `width:${W}px;height:${H}px;border-radius:${R}px">`+
    `<div aria-hidden="true" style="position:absolute;inset:0;`+
      `pointer-events:none;border-radius:${R}px;opacity:${core.a};`+
      `background:radial-gradient(ellipse ${core.size}% ${core.size}% at 50% 50%,`+
      `rgba(255,255,255,1) ${core.r}%,rgba(255,255,255,0) `+
      `${Math.min(100,core.r+core.blur)}%)"></div>`+
    `<div aria-hidden="true" style="position:absolute;inset:0;`+
      `pointer-events:none;border-radius:${R}px;box-shadow:`+
      `inset 0 0 ${8.333*k}px 0 rgba(255,255,255,${g}),`+
      `inset 0 0 ${8.333*k}px 0 rgba(255,255,255,${g}),`+
      `inset 0 0 0 ${.833*k}px rgba(255,255,255,.5),`+
      `inset 0 ${.833*k}px 0 0 rgba(255,255,255,.78)"></div>`+
    `<span style="position:relative;display:flex;align-items:center;`+
      `justify-content:center;width:${W}px;height:${H}px;padding:0 ${pad}px;`+
      `font:600 ${12.222*k}px/1.4 Inter,-apple-system,sans-serif;color:#323232;`+
      `white-space:nowrap">${esc(text||'New')}</span></div></span>`;
}
/* "Live mode · New" — what the owner asked to fire when something new
   enters Aethron. Marked .in on the next tick so it ARRIVES rather than
   simply being there; the timer is the same insurance every other
   reveal in this file carries. */
function liveNewHtml(label,k){
  return `<span class="livenew"><i></i><b>${esc(label||'Live mode')}</b>`+
         metalBadgeHtml('New',k)+`</span>`;
}
function playArrivals(){
  document.querySelectorAll('.livenew:not(.in)').forEach(r=>{
    requestAnimationFrame(()=>r.classList.add('in'));
    setTimeout(()=>r.classList.add('in'),150);
  });
}

/* ══ SEARCH ═════════════════════════════════════════════════════════
   Projects and the places you can go, in one field. It searches what is
   already in hand (S.projects) rather than asking the server, so it
   answers on the keystroke. */
const FIND_GO=[
  {icon:'plus',     label:'New project',    k:'new project start',  go:()=>newProject()},
  {icon:'terminal', label:'Code workspace', k:'code ide editor',    go:()=>openCode()},
  {icon:'library',  label:'Design library', k:'library designs',    go:()=>openLibrary()},
  {icon:'gear',     label:'Settings',       k:'settings key model wallet provider',
                                                                    go:()=>openSettings()},
];
let FIND_HITS=[];
function findMatches(raw){
  const q=(raw||'').trim().toLowerCase();
  if(!q)return [];
  const out=[];
  for(const p of (S.projects||[]))
    if((p.name+' '+(p.platform||'')).toLowerCase().includes(q))
      /* `package`, not `project`: there is no project glyph, and a name
         that is not in ICONS renders an empty <svg> — which is how a
         sidebar row once sat visibly misaligned beside its neighbours. */
      out.push({icon:'package',label:p.name,note:p.platform,
                go:()=>select(p.name)});
  for(const d of FIND_GO)
    if((d.label+' '+d.k).toLowerCase().includes(q))
      out.push({icon:d.icon,label:d.label,note:'go',go:d.go});
  return out.slice(0,9);
}
function runFind(raw){
  FIND_HITS=findMatches(raw);
  const box=$('findhits'),list=$('plist');
  if(!box||!list)return;
  const on=!!(raw||'').trim();
  list.hidden=on; box.hidden=!on;
  if(!on){box.innerHTML='';return;}
  box.innerHTML=FIND_HITS.length
    ?FIND_HITS.map((h,i)=>`<button class="fhit${i?'':' on'}" onclick="findGo(${i})">`+
       `${I(h.icon,14)}<span>${esc(h.label)}</span>`+
       `<span class="fk">${esc(h.note||'')}</span></button>`).join('')
    :`<div class="findnone">nothing matches that</div>`;
}
function findGo(i){
  const h=FIND_HITS[i]; if(!h)return;
  const f=$('findq'); if(f)f.value='';
  runFind('');
  h.go();
}
function findFirst(){ if(FIND_HITS.length)findGo(0); }
addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&(e.key==='k'||e.key==='K')){
    e.preventDefault();
    const f=$('findq'); if(f){f.focus();f.select();}
  }
});
/* The sheet is a share of the window, so every resize moves the page
   inside it. Ask once the dust settles — the ring then springs to the
   new geometry on its own, which is the whole point of animating it
   rather than drawing it. */
addEventListener('resize',()=>{
  clearTimeout(window._ringT);
  window._ringT=setTimeout(()=>{
    const f=$('pvwframe');
    if(f&&f.contentWindow)f.contentWindow.postMessage({forge:'remeasure'},'*');
  },140);
});
/* ONE SENTENCE, ABOUT ONE ELEMENT. The selection becomes the subject and
   the person's own words become the request; everything downstream is the
   measured change path, unchanged. */
function selSend(){
  const ta=$('selta'); const words=(ta&&ta.value||'').trim();
  if(!words||!PVW.pick)return;
  const L=pickLabel(PVW.pick);
  const ask=`On ${S.cur}, change ${L.ref}: ${words}`;
  closeSel(); closePvw();
  const box=$('npurl');
  if(box){box.value=ask; growTa(box);}
  consoleSend();
}
async function openInBrowser(){
  if(!S.cur)return;
  try{const {port}=await api('/api/preview',{project:S.cur});
      window.open('http://127.0.0.1:'+port+'/','_blank');}
  catch(e){note&&note('could not start the preview: '+e.message);}
}
/* The front door, rebuilt rather than remembered. Same words the page
   ships with, so returning here is indistinguishable from launching. */
function renderWelcome(c){
  if(!c)return;
  c.dataset.split='';
  c.className='conv-host';
  c.innerHTML=convShell({
    title:'Every template you buy<br>can be entirely yours.',
    sub:`Paste a live Framer or Webflow URL, drop in a screenshot, or just
      say what you want built. Aethron works out whether that is a
      migration, a port, a rebuild or a coding job &mdash; and does it here.`,
    hint:'Paste a template URL, or tell Aethron what you want\u2026',
    extra:`<div class="startmeta" id="startmeta">
        <input id="npname" placeholder="Project name (optional)">
        <span class="or">or</span>
        <label class="fpick"><input id="npfile" type="file"
         accept=".html,.htm,.zip" multiple>${I('file',13)}
         choose an export, a zip, or saved pages</label>
      </div>`,
    quick:[
      {icon:'zap',label:'Migrate a live site',
       ask:'Migrate ',
       need:'Paste the live Framer or Webflow URL, and tell me the brand it should become.'},
      {icon:'image',label:'Rebuild a screenshot into real code',
       ask:'Rebuild this screenshot as ',
       need:'Attach or paste the screenshot, then say which framework you want.'}]});
  bindStick(); renderChat(); mountDots(c); mountOrbs(c); syncComposer();
}
function renderChatTab(c){
  if(!c)return;              // never take the whole layout down with it
  const n=(S.info&&S.info.strings)||0, f=(S.info&&S.info.filled)||0;
  c.innerHTML=convShell({
    top:projectCard(),
    title:`What should <em>${esc(S.cur)}</em> become?`,
    sub:`Describe the brand and Aethron rebrands every string, fits the
      byte-locked slots, rebuilds and checks the result. Ask it to port the
      site to Astro, Next or Vue, swap a logo, edit a page, or fix what a
      check flagged.${n?` &mdash; ${f} of ${n} strings filled so far.`:''}`,
    hint:`Tell Aethron what to do with ${S.cur}…`,
    quick:[
      {icon:'wand',label:'Rebrand every string',
       ask:'Rebrand this whole site to',
       need:'What is the new brand called, and what tone should the copy take?'},
      {icon:'check',label:'Build and check it',
       ask:'Build, then verify, then probe. Report exactly what fails.',
       need:'Press Enter to run it, or add anything you want checked first.'},
      {icon:'package',label:'Port to another framework',
       ask:'Port this site to',
       need:'Which framework \u2014 astro, next, vue, svelte? Astro is the proven one.'}]});
  bindStick();renderChat();mountDots(c);mountOrbs(c);
}
/* A STOP THAT IS ALWAYS WITHIN REACH. An agent doing the wrong thing
   expensively, with no way to interrupt it but closing the window, is
   the worst moment this product can have. The send button becomes a
   stop button for exactly as long as there is something to stop. */
async function stopTurn(){
  try{await api('/api/code/stop',{key:CODE.key});}catch(e){}
  if(CODE.poll){clearInterval(CODE.poll);CODE.poll=0;}
  CODE.key='';
  CODE.events.push({type:'sys',text:'stopped'});
  syncComposer();renderChat();
}
/* The composer is rebuilt only where it changes, so typing is never
   interrupted by a poll landing mid-word. */
function syncComposer(){
  /* the beam is set BEFORE the early return below, which fires whenever
     the button already matches — otherwise the ring would only ever
     light on the frame the button happened to change */
  const cm=document.querySelector('.composer');
  /* working, or nothing started yet, both glow; a project that has
     finished its work sits on the quiet bottom line instead. */
  beamMode(cm, CODE.poll ? 'pulse' : (S.cur ? 'line' : 'pulse'));
  const mn=$('cbarmodel');
  if(mn&&CODE.model)mn.textContent=CODE.model;
  const b=document.querySelector('.composer .cbtn');if(!b)return;
  const busy=!!CODE.poll, isStop=b.classList.contains('stop');
  if(busy===isStop)return;
  b.outerHTML=busy
    ?`<button class="cbtn stop" onclick="stopTurn()" aria-label="Stop"
        title="Stop">${I('stop',15)}</button>`
    :`<button class="cbtn" onclick="consoleSend()" aria-label="Send"
        title="Send"><span data-ic="up"></span></button>`;
  document.querySelectorAll('.composer [data-ic]').forEach(n=>{
    n.outerHTML=I(n.dataset.ic,+(n.dataset.ics||14));});
}
/* ONE PLACE DECIDES WHETHER THE ROOM IS LIT. Opacity alone would leave
   it painting sixty times a second behind something invisible, so the
   field is stopped outright — and the three reasons to stop it are the
   same three the CSS fades it for, kept together so they cannot drift
   apart. */
function syncHero(){
  const hero=document.querySelector('canvas.dotf.hero');
  const hf=hero&&FIELDS.get(hero);
  if(!hf)return;
  const talking=!!(CODE.events&&CODE.events.length);
  const cramped=document.body.classList.contains('pvwopen')
               || window.innerWidth<1280;   // no margins left to live in
  (talking||cramped)?hf.stop():hf.start();
}
addEventListener('resize',()=>{clearTimeout(syncHero._t);
  syncHero._t=setTimeout(syncHero,180);});

/* Whether to follow the log is the READER's business, not the log's. */
function bindStick(){
  const sc=$('convscroll');if(!sc)return;
  CODE.stick=true;
  sc.onscroll=()=>{CODE.stick=
    sc.scrollHeight-sc.scrollTop-sc.clientHeight<60;};
}
/* A SUGGESTION IS A DIRECTION, NOT AN INSTRUCTION. Clicking one used to
   call consoleSend() straight away, so Aethron started working — and
   spending — before anyone had said which site, which brand, which
   framework. It now fills the composer and ASKS for the part only the
   person knows. Nothing runs until they press Enter themselves. */
function quick(t,need){
  const ta=$('npurl');if(!ta)return;
  ta.value=t.endsWith(' ')?t:t+' ';
  growTa(ta); ta.focus();
  try{ta.setSelectionRange(ta.value.length,ta.value.length);}catch(e){}
  const bar=$('askbar');
  if(bar){bar.innerHTML=need?esc(need):'';bar.hidden=!need;}
}
function growTa(el){el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,180)+'px'}

/* THE FRONT DOOR. The composer is the product: a URL, an instruction,
   or both. The first message starts a console session rooted outside
   any project, because the project does not exist yet — the agent
   creates it with the same mcp__aethron__* tools it uses for
   everything else. */
async function consoleSend(){
  const ta=$('npurl'), text=(ta&&ta.value||'').trim();
  if(!text)return;
  const w=$('welcome'), log=$('chatlog'),
        rows=document.querySelector('.focal .qrows'),
        meta=document.querySelector('.focal .startmeta');
  if(w)w.hidden=true; if(rows)rows.hidden=true; if(meta)meta.hidden=true;
  if(log)log.hidden=false;
  ta.value=''; growTa(ta);
  try{
    if(!CODE.key){
      CODE.events=[];CODE.since=0;
      const r=await api('/api/code/start',
        S.cur?{project:S.cur}:{console:true});
      CODE.key=r.key;
    }
    CODE.events.push({type:'you',text});renderChat();
    CODE.lastEv=Date.now();
    await api('/api/code/send',{key:CODE.key,text});
    if(!CODE.poll)CODE.poll=setInterval(pollCode,900);
  }catch(e){
    CODE.events.push({type:'text',text:'Could not reach the agent: '
      +e.message+' — open Plan & AI and check the model settings.'});
    renderChat();
  }
}
/* WHAT THE SYSTEM IS DOING, IN WORDS THE OWNER USES.
   The chain the owner had to click by hand — save plan, build, fill copy
   map, polish — is a sequence the agent already knows and already has
   tools for. So the UI stops asking anyone to drive it and starts
   REPORTING it: one line saying what is happening now, and the detail
   folded away for whoever wants it. */
const TOOLWORDS={
  create_project:'Reading the template',fetch:'Downloading the runtime',
  inventory:'Finding every string, image and link',
  localize:'Localising assets',localize_assets:'Localising assets',
  set_plan:'Saving the brand plan',get_plan:'Reading the plan',
  get_content:'Reading the copy',set_content:'Writing copy',
  set_content_bulk:'Rewriting the copy',build:'Rebuilding the site',
  verify:'Checking the files',probe:'Loading it in a browser',
  heal:'Repairing edits that did not land',
  generate_logo:'Drawing the wordmark',serve_preview:'Starting a preview',
  replace_image_slots:'Swapping images',remove_element:'Removing an element',
  undo:'Undoing',list_projects:'Looking at your projects'};
/* ══ THE DOT FIELD ═════════════════════════════════════════════════
   A grid of dots whose brightness is a function of a travelling wave,
   a per-dot shimmer, and (optionally) how far a real job has got. The
   maths is deliberately cheap — one sin() per dot per frame — because
   this runs behind everything else the app is doing.

   Three rules it will not break:
     * prefers-reduced-motion means it draws ONE still frame and stops;
     * a hidden tab or a field scrolled out of view costs nothing;
     * it never draws over anything interactive.                      */
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
class DotField{
  constructor(cv,o){
    o=o||{};
    this.cv=cv; this.cx=cv.getContext('2d');
    this.gap=o.gap||9; this.r=o.r||1.15;
    this.speed=o.speed||1; this.amp=o.amp||1; this.base=o.base||.10;
    this.tint=o.tint||[217,119,87];       // the brand is the light source
    this.cool=o.cool||[150,160,190];
    this.progress=null; this.t=Math.random()*40; this.on=false; this.raf=0;
    this.seen=true; this.px=null; this.py=null; this.reach=o.reach||150;
    this.orbit=!!o.orbit;
    this.resize();
    this._ro=new ResizeObserver(()=>this.resize()); this._ro.observe(cv);
    if('IntersectionObserver' in window){
      this._io=new IntersectionObserver(es=>{
        this.seen=es[0].isIntersecting; this.seen?this.start():this.stop();
      },{threshold:0}); this._io.observe(cv);
    }
    document.addEventListener('visibilitychange',()=>{
      document.hidden?this.stop():(this.seen&&this.start());});
  }
  resize(){
    const d=Math.min(devicePixelRatio||1,2),
          w=this.cv.clientWidth||1,h=this.cv.clientHeight||1;
    this.cv.width=Math.round(w*d); this.cv.height=Math.round(h*d);
    this.w=w; this.h=h; this.d=d;
    /* ALWAYS redraw. Assigning width/height CLEARS the canvas, and
       redrawing only when stopped left a blank field whenever a resize
       landed while running and the next frame never came — a hidden
       tab, a scroll out of view. Measured: one frame of ink, then zero
       for the rest of the run. */
    this.draw();
  }
  set(o){Object.assign(this,o||{}); if(!this.on)this.draw();}
  start(){
    if(this.on||REDUCED){this.draw();return;}
    this.on=true; const step=()=>{
      if(!this.on)return;
      this.t+=0.016*this.speed; this.draw(); this.raf=requestAnimationFrame(step);
    }; this.raf=requestAnimationFrame(step);
  }
  stop(){this.on=false; if(this.raf)cancelAnimationFrame(this.raf); this.raf=0;}
  /* A DOT THAT ONLY CHANGES BRIGHTNESS IS A MIST. What reads as designed
     is a dot that GROWS as it lights, crests that actually travel, and a
     glow on the peaks — and a field that answers the cursor, because
     nothing convinces a person a surface is alive like it noticing them.
     Two waves at different angles and speeds interfere, so the pattern
     never repeats visibly and never looks mechanical. */
  /* LIGHT, NOT A PATTERN. A grid with a wave running through it reads as
     a grid — the geometry is the first thing the eye finds, and that is
     what made it look like an exercise rather than a design. So there is
     no wave. There are LIGHT SOURCES drifting on slow independent paths,
     and every dot simply reports how much light reaches it: brighter,
     larger, and warmer the closer it is. The shape that comes out is the
     one from the screenshot Aethron rebuilt — an ember bloom opening out
     of black — and because the sources move on irrational periods it
     never repeats and never looks mechanical.
     Colour ramps ember -> terracotta -> warm white with intensity, which
     is what makes a glow read as heat rather than as paint. */
  draw(){
    const c=this.cx,g=this.gap,d=this.d,W=this.w,H=this.h;
    if(!W||!H)return;
    c.setTransform(d,0,0,d,0,0);
    c.clearRect(0,0,W,H);
    const t=this.t, m=Math.min(W,H), px=this.px, py=this.py;
    /* THE LIGHT ORBITS THE WORDS — it does not sit under them.
       The hero mask keeps the middle 58% clear, so a source placed
       INSIDE that zone can only ever show as the part of itself that
       spills past the edge: one lopsided patch, which is precisely
       what made the field read as a dot grid parked in the left
       margin instead of a bloom around the content. Measured: the
       brightest of the three sat at 0.30W, well inside the clear
       ellipse, and its whole visible contribution was that band.
       On a ring OUTSIDE the clear zone the same three sources light
       the halo all the way round, and because their angular speeds
       are unrelated the lit side keeps moving and never repeats.
       The inline field carries no mask and is 16px tall, so an orbit
       would swing its sources clean off the strip — it keeps the
       drifting placement. */
    const B = this.orbit ? (()=>{
      /* With a COLUMN masked out instead of a disc, the sources belong
         in the side margins rather than on a ring — an orbit now spends
         half its time behind the words where nothing shows. */
      const orb=(ph,sp,rr,aa)=>{
        const sway=Math.sin(t*sp+ph);
        return {x:W*(sway<0?0.16+0.10*Math.cos(t*sp*1.3+ph)
                          :0.84-0.10*Math.cos(t*sp*1.3+ph)),
                y:H*(0.5+0.42*Math.sin(t*sp*0.79+ph*1.7)),
                r:m*rr, a:aa};};
      return [orb(0.0,0.113,0.66,1.00),orb(2.3,0.081,0.54,0.84),
              orb(4.4,0.147,0.46,0.62)];
    })() : [
      {x:W*(0.30+0.13*Math.sin(t*0.183)), y:H*(0.40+0.15*Math.cos(t*0.127)),
       r:m*0.62, a:1.00},
      {x:W*(0.74+0.11*Math.cos(t*0.101)), y:H*(0.63+0.13*Math.sin(t*0.157)),
       r:m*0.50, a:0.78},
      {x:W*(0.52+0.16*Math.sin(t*0.071)), y:H*(0.18+0.10*Math.cos(t*0.113)),
       r:m*0.42, a:0.55}];
    if(px!=null)B.push({x:px,y:py,r:this.reach,a:1.15});
    const cols=Math.ceil(W/g)+1, rows=Math.ceil(H/g)+1,
          ox=(W-(cols-1)*g)/2, oy=(H-(rows-1)*g)/2;
    for(let j=0;j<rows;j++){
      for(let i=0;i<cols;i++){
        const x=ox+i*g, y=oy+j*g;
        let lit=0;
        for(let k=0;k<B.length;k++){
          const b=B[k], dx=(x-b.x)/b.r, dy=(y-b.y)/b.r, dd=dx*dx+dy*dy;
          if(dd<1){ const f=1-dd; lit+=b.a*f*f*f*f; }  // tight core, soft rim
        }
        if(this.progress!=null){
          const frac=(i+0.5)/cols;
          lit=frac<this.progress?0.95:0.04;
        }
        // a slow breath so even unlit air is never quite dead
        const breath=0.5+0.5*Math.sin(t*0.8+i*0.21+j*0.17);
        let v=this.base*(0.55+0.45*breath)+this.amp*Math.min(1.25,lit)*0.80;
        if(v<=0.02)continue;
        if(v>1)v=1;
        // ember -> terracotta -> warm white, by how much light lands
        const q=Math.min(1,lit), warm=q*q;
        const cr=Math.round(96+121*q+38*warm),
              cg=Math.round(44+ 75*q+95*warm),
              cb=Math.round(32+ 55*q+103*warm);
        const rad=this.r*(0.55+1.75*Math.min(1,lit));
        if(v>0.34){   // only the hot ones pay for a halo
          c.fillStyle='rgba('+cr+','+cg+','+cb+','+(v*0.13).toFixed(3)+')';
          c.beginPath(); c.arc(x,y,rad*3.4,0,6.2832); c.fill();
        }
        c.fillStyle='rgba('+cr+','+cg+','+cb+','+v.toFixed(3)+')';
        c.beginPath(); c.arc(x,y,rad,0,6.2832); c.fill();
      }
    }
  }
  /* The pointer is tracked on the field's OWN offset parent, so a cursor
     anywhere over the panel lights the field beneath it. */
  follow(host){
    if(!host)return;
    const move=e=>{
      const b=this.cv.getBoundingClientRect();
      this.px=e.clientX-b.left; this.py=e.clientY-b.top;
      if(!this.on&&!REDUCED)this.draw();
    };
    host.addEventListener('pointermove',move,{passive:true});
    host.addEventListener('pointerleave',()=>{this.px=this.py=null;},{passive:true});
  }
  destroy(){this.stop(); this._ro&&this._ro.disconnect(); this._io&&this._io.disconnect();}
}
/* Canvases are re-created on every render, so mounting is idempotent and
   keyed off the node itself. */
const FIELDS=new WeakMap();
function mountDots(root){
  (root||document).querySelectorAll('canvas.dotf').forEach(cv=>{
    if(FIELDS.has(cv))return;
    const hero=cv.classList.contains('hero');
    const f=new DotField(cv, hero
      ? {gap:22,r:1.30,speed:.52,amp:1.15,base:.055,reach:200,orbit:true}
      : {gap:8,r:1.0,speed:1.6,amp:1.15,base:.12,reach:0});
    FIELDS.set(cv,f);
    if(hero)f.follow(cv.parentElement);
    f.start();
  });
}

/* border-beam pulse driver — MIT, Jakub Antalik; type-stripped. */
                                                     

/**
 * Shared breathing driver for the Pulse effects.
 *
 * The pulse breathing (size / drift / per-quadrant opacity / height) and the
 * slow hue drift used to run as ~15 per-instance CSS `@property` keyframe
 * animations at the display refresh rate (60–120 Hz). Because each value feeds
 * the painted gradients/filters, that repainted the breathing layers 60–120×/s.
 *
 * The motion is very slow (1.6–6.4 s periods), so instead every registered
 * instance is driven from a SINGLE shared requestAnimationFrame loop throttled
 * to ~30 fps. This halves the paint frequency on 60 Hz displays and quarters it
 * on 120 Hz, with no perceptible change to the breathing.
 *
 * Each oscillator ping-pongs a CSS custom property between `a` and `b` with an
 * ease-in-out (cosine) curve over `period` seconds, offset by `delay` seconds so
 * otherwise-identical oscillators desync (matching the former CSS keyframes +
 * animation-delay).
 */

                         
                  
                            
 

const instances = new Set               ();
let rafId                = null;
let lastFrame = 0;

// ~30 fps. Subtract a small slack so a frame that lands a hair early still runs.
const FRAME_INTERVAL = 1000 / 30 - 2;

const TWO_PI = Math.PI * 2;

function now()         {
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

/** Cosine ease-in-out factor in [0, 1]: 0 at phase 0/1, 1 at phase 0.5. */
function pingPong(phase        )         {
  return (1 - Math.cos(TWO_PI * phase)) / 2;
}

function frame(ts        )       {
  rafId = requestAnimationFrame(frame);

  if (ts - lastFrame < FRAME_INTERVAL) return;
  lastFrame = ts;

  const tSec = ts / 1000;

  instances.forEach(({ el, config }) => {
    for (const osc of config.oscillators) {
      // Match CSS animation-delay semantics: a positive delay starts later.
      const phase = (tSec - osc.delay) / osc.period;
      const value = osc.a + (osc.b - osc.a) * pingPong(phase);
      el.style.setProperty(
        osc.prop,
        osc.unit === 'px' ? `${value.toFixed(2)}px` : value.toFixed(4)
      );
    }

    if (config.hue) {
      const { prop, range, period, continuous } = config.hue;
      // `continuous` rotates a full circle (0→range, looping) so every color
      // sweeps through every edge; otherwise drift between -range and +range.
      const value = continuous
        ? ((tSec / period) % 1) * range
        : -range + 2 * range * pingPong(tSec / period);
      el.style.setProperty(prop, `${value.toFixed(2)}deg`);
    }
  });
}

function startLoop()       {
  if (rafId == null) {
    lastFrame = 0;
    rafId = requestAnimationFrame(frame);
  }
}

function stopLoopIfIdle()       {
  if (instances.size === 0 && rafId != null) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
}

/**
 * Register an element to be driven by the shared pulse loop.
 *
 * @returns a cleanup function that unregisters the instance (and stops the
 *          shared loop once no instances remain).
 */
function registerPulseInstance(
  el             ,
  config                   
)             {
  const instance                = { el, config };
  instances.add(instance);
  startLoop();

  return () => {
    instances.delete(instance);
    stopLoopIfIdle();
  };
}

const BEAM_CFG={"oscillators": [{"prop": "--bw1-ae", "a": 0.72, "b": 1.308, "period": 2.3400000000000003, "delay": 0, "unit": ""}, {"prop": "--bh1-ae", "a": 1.252, "b": 0.762, "period": 3.2760000000000002, "delay": 0, "unit": ""}, {"prop": "--bx1-ae", "a": -33, "b": 29.7, "period": 3.04, "delay": 0, "unit": "px"}, {"prop": "--by1-ae", "a": 18.150000000000002, "b": -23.099999999999998, "period": 3.04, "delay": 0, "unit": "px"}, {"prop": "--bw2-ae", "a": 1.28, "b": 0.762, "period": 2.8600000000000003, "delay": 0, "unit": ""}, {"prop": "--bh2-ae", "a": 0.776, "b": 1.294, "period": 2.1060000000000003, "delay": 0, "unit": ""}, {"prop": "--bx2-ae", "a": 26.400000000000002, "b": -29.7, "period": 3.5719999999999996, "delay": 0, "unit": "px"}, {"prop": "--by2-ae", "a": -33, "b": 21.45, "period": 3.5719999999999996, "delay": 0, "unit": "px"}, {"prop": "--bw3-ae", "a": 0.832, "b": 1.322, "period": 2.548, "delay": 0, "unit": ""}, {"prop": "--bh3-ae", "a": 1.21, "b": 0.72, "period": 3.6399999999999997, "delay": 0, "unit": ""}, {"prop": "--bx3-ae", "a": -19.8, "b": 33, "period": 2.755, "delay": 0, "unit": "px"}, {"prop": "--by3-ae", "a": -28.05, "b": 14.85, "period": 2.755, "delay": 0, "unit": "px"}, {"prop": "--bgh-ae", "a": 0.6599999999999999, "b": 1.34, "period": 2.4, "delay": 0, "unit": ""}, {"prop": "--bop-tl-ae", "a": 0.52, "b": 1, "period": 1.9, "delay": 0, "unit": ""}, {"prop": "--bop-tr-ae", "a": 0.52, "b": 1, "period": 2.508, "delay": 0.532, "unit": ""}, {"prop": "--bop-bl-ae", "a": 0.52, "b": 1, "period": 1.5959999999999999, "delay": 1.045, "unit": ""}, {"prop": "--bop-br-ae", "a": 0.52, "b": 1, "period": 3.002, "delay": 1.577, "unit": ""}], "hue": {"prop": "--beam-hue-ae", "range": 26, "period": 16, "continuous": false}};

/* Turning it on is two things: the CSS state, and registering with the
   shared driver so its 17 oscillators are actually moving. Registering
   without the attribute gives a still beam; the attribute without the
   driver gives a frozen gradient. */
function beamOn(el,on){beamMode(el,on?'pulse':'off');}
/* 'pulse' is the ring and needs the shared oscillator driver; 'line' is
   the bottom-edge travel and is pure CSS, so registering it would spend
   17 property writes a frame on something nothing reads. */
const BEAM_STOP=new WeakMap();
function beamMode(el,mode){
  if(!el)return;
  if(el.dataset.mode===mode)return;
  el.dataset.mode=mode;
  /* registerPulseInstance RETURNS its own cleanup — there is no
     unregister function to call, and calling one there was is how this
     threw on the first state change. Keep the closure it hands back. */
  const stop=BEAM_STOP.get(el);
  if(stop){stop();BEAM_STOP.delete(el);}
  if(mode==='off'){el.removeAttribute('data-active');
    el.removeAttribute('data-beam');return;}
  el.setAttribute('data-beam',mode==='line'?'ln':'ae');
  el.setAttribute('data-active','');
  el.removeAttribute('data-fading');
  if(mode==='pulse')BEAM_STOP.set(el,registerPulseInstance(el,BEAM_CFG));
}

/* ── thinking-orbs engine, vendored ─────────────────────────────────
   MIT License · Copyright (c) 2026 Jakub Antalik
   https://github.com/Jakubantalik/thinking-orbs

   Vendored rather than npm-installed because Aethron's studio is a single
   stdlib-only file with no build step and no node on a user's machine.
   The GEOMETRY below is the author's, type-stripped and concatenated,
   unchanged — it reproduces all 72 of the project's own golden vectors
   to 1e-4. Aethron's changes are confined to the PAINTER, which is our
   own code further down: the original is strictly monochrome and this
   product is not.
   ──────────────────────────────────────────────────────────────────── */

/* ── engine/core.ts ───────────────────────────────────────── */
// Shared primitives for the dotted 3D thought-orbs. Ported from inkform
// (PlotterLab's HalftoneSphere lineage): honestly 3D — rotated,
// depth-shaded, z-sorted. Depth is carried by dot size and ink weight
// alone. Plain 2D canvas fills only: no ctx.filter, no SVG filters, so
// every mode renders identically in Chrome, Safari and Firefox.

                      
            
            
            
            
                                                                      
                
             
 

/** A stroked edge between two projected points (the `connecting` web). */
                       
             
             
             
             
                                                   
                
             
            
 

/**
 * One rendered instant: a complete, final set of draw instructions.
 * `dots` is already z-sorted into draw order and radius-clamped; `lines`
 * are drawn first. Nothing here needs further interpretation, which is what
 * makes a frame portable to any 2D renderer.
 */
                           
              
                
 

                                                                                      

function lerp(a        , b        , f        )         {
  return a + (b - a) * f;
}

function frac(x        )         {
  return x - Math.floor(x);
}

/** Value noise on a 2D lattice — smooth, deterministic, cheap. */
function vnoise(x        , y        )         {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  let fx = x - xi;
  let fy = y - yi;
  fx = fx * fx * (3 - 2 * fx);
  fy = fy * fy * (3 - 2 * fy);
  const a = hashD(xi, yi);
  const b = hashD(xi + 1, yi);
  const c = hashD(xi, yi + 1);
  const d = hashD(xi + 1, yi + 1);
  return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy;
}

/** Deterministic hash in [0, 1). */
function hashD(a        , b        )         {
  const h = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return h - Math.floor(h);
}

/** Stable directions on a unit sphere (Fibonacci lattice). */
function fibDir(i        , n        )                           {
  const golden = Math.PI * (3 - Math.sqrt(5));
  const y = 1 - (2 * (i + 0.5)) / n;
  const rad = Math.sqrt(1 - y * y);
  const a = i * golden;
  return [rad * Math.cos(a), y, rad * Math.sin(a)];
}

/** Shortest signed angular distance, wrapped to (-π, π]. */
function angleDelta(a        , b        )         {
  return Math.atan2(Math.sin(a - b), Math.cos(a - b));
}

/** Shared spin + tilt + orthographic projection. */
function makeProj(yaw        , tilt        , cx        , cy        , scale        )            {
  const st = Math.sin(tilt);
  const ct = Math.cos(tilt);
  const sy = Math.sin(yaw);
  const cyw = Math.cos(yaw);
  return (x, y, z) => {
    const x1 = x * cyw + z * sy;
    const z1 = -x * sy + z * cyw;
    const y1 = y * ct - z1 * st;
    const z2 = y * st + z1 * ct;
    return [cx + x1 * scale, cy - y1 * scale, z2];
  };
}

/**
 * Painter: z-sort far→near, matte grayscale dots. On dark substrates the
 * ink value is mirrored (1 - white) so near dots read bright — the same
 * depth language on an inverted substrate.
 */
function paint(ctx                          , dots       , dark         , rMin = 0.3)       {
  for (const d of dots) {
    const alpha = d.a ?? 1;
    const w = Math.min(1, Math.max(0, d.white));
    const g = Math.round((dark ? 1 - w : w) * 255);
    ctx.fillStyle = `rgba(${g},${g},${g},${alpha})`;
    ctx.beginPath();
    ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
    ctx.fill();
  }
}

/** Stroke pass for edge-based modes. Runs before `paint` so nodes sit on top. */
function paintLines(ctx                          , lines        , dark         )       {
  for (const l of lines) {
    const alpha = l.a ?? 1;
    const w = Math.min(1, Math.max(0, l.white));
    const g = Math.round((dark ? 1 - w : w) * 255);
    ctx.strokeStyle = `rgba(${g},${g},${g},${alpha})`;
    ctx.lineWidth = l.w;
    ctx.beginPath();
    ctx.moveTo(l.x1, l.y1);
    ctx.lineTo(l.x2, l.y2);
    ctx.stroke();
  }
}

/**
 * Turn raw mode output into a finished frame: drop invisible marks, clamp
 * radii to the mode's floor, and z-sort far→near into draw order.
 *
 * This runs in the GEOMETRY step, not the painter, so a frame is a complete
 * set of draw instructions: every value is final and the array order is the
 * order to draw in. That is what lets the RN and SwiftUI ports share this
 * output verbatim — a port draws the list, it never re-derives anything —
 * and what lets the golden-vector tests compare numbers instead of pixels.
 */
function finalizeFrame(dots       , lines        , rMin = 0.3)           {
  const visible        = [];
  for (const d of dots) {
    if ((d.a ?? 1) < 0.02) continue;
    d.r = Math.max(rMin, d.r);
    visible.push(d);
  }
  visible.sort((a, b) => a.z - b.z);
  return { dots: visible, lines: lines.filter((l) => (l.a ?? 1) >= 0.02) };
}

/** Paint a finished frame. Lines first, so nodes sit on top of their edges. */
function paintFrame(ctx                          , frame          , dark         )       {
  if (frame.lines.length) paintLines(ctx, frame.lines, dark);
  paint(ctx, frame.dots, dark);
}

/**
 * Dot radii were tuned for a 300pt frame; sub-linear scaling keeps small
 * spinners legible. Lower pow = radii shrink less with size.
 */
function radiusScale(size        , pow        )         {
  return (size / 300) ** pow;
}

/* ── engine/profiles.ts ───────────────────────────────────────── */
// Density profiles + the multiplier machinery that scales them. The base
// rows are inkform's `fine` profiles; each shipped preset (state × size)
// applies count / radius multipliers on top, resolved once per mount.

                           
                                    
 

// 2-D lattices (rings × dots-per-ring) come in pairs — each side takes
// √scale so the TOTAL dot count scales by `scale`; flat lists scale
// linearly. `iconD` sets the morph outline's sampling density.
const COUNT_PAIRS                                           = [
  ['latRings', 'lonDensity'],
  ['rings', 'lonDensity'],
  ['lanes', 'segs']
];
const COUNT_KEYS = ['orbitN', 'ghostN', 'nodeN', 'strandN', 'signals']         ;
const ICON_DENSITY_KEYS = ['iconD']         ;

// Every key that sets a dot's rendered radius — scaling all of them keeps
// a dot's near/far falloff intact while shrinking or growing the mark.
const RADIUS_KEYS = [
  'rBase',
  'rDepth',
  'rActive',
  'rDot',
  'ghostR',
  'partR',
  'partRDepth',
  'nodeR',
  'nodeRDepth'
]         ;

function scaleCounts(opts          , scale        )           {
  const out           = { ...opts };
  const done = new Set        ();
  const rt = Math.sqrt(scale);
  for (const [a, b] of COUNT_PAIRS) {
    const va = out[a];
    const vb = out[b];
    if (va != null && vb != null && !done.has(a) && !done.has(b)) {
      out[a] = Math.max(2, Math.round(va * rt));
      out[b] = Math.max(2, Math.round(vb * rt));
      done.add(a);
      done.add(b);
    }
  }
  for (const k of COUNT_KEYS) {
    const v = out[k];
    // 0 means the mode opted out of that layer entirely (ring has no ghost
    // sphere) — scaling must not resurrect it as a single stray dot
    if (v != null && v !== 0 && !done.has(k)) out[k] = Math.max(1, Math.round(v * scale));
  }
  for (const k of ICON_DENSITY_KEYS) {
    const v = out[k];
    if (v != null) out[k] = Math.max(0.02, v * scale);
  }
  return out;
}

function scaleRadii(opts          , scale        )           {
  const out           = { ...opts };
  for (const k of RADIUS_KEYS) {
    const v = out[k];
    if (v != null) out[k] = v * scale;
  }
  // remember the multiplier itself — spacing-derived radii (the morph
  // outline) use it, since they aren't based on any single radius key
  out.rSizeMul = (out.rSizeMul ?? 1) * scale;
  return out;
}

/** Base (fine) profiles per mode, before preset multipliers. */
const BASE_PROFILES                           = {
  globe: {
    latRings: 17,
    lonDensity: 44,
    rBase: 0.6,
    rDepth: 1.7,
    rBoost: 1.0,
    inkFar: 0.62,
    inkSpan: 0.54,
    rsPow: 0.6,
    rMin: 0.3
  },
  orbits: {
    orbitN: 12,
    ghostN: 40,
    ghostR: 0.9,
    ghostA: 0.5,
    particles: 3,
    partR: 1.2,
    partRDepth: 1.6,
    rsPow: 0.6,
    rMin: 0.3
  },
  rubik: {
    latRings: 15,
    lonDensity: 40,
    moveCount: 14,
    rBase: 0.6,
    rDepth: 1.7,
    rActive: 0.3,
    inkFar: 0.62,
    inkSpan: 0.54,
    rsPow: 0.6,
    rMin: 0.3
  },
  wave: {
    rings: 15,
    lonDensity: 40,
    rBase: 0.6,
    rDepth: 1.7,
    rsPow: 0.6,
    rMin: 0.3
  },
  web: {
    nodeN: 30,
    thr: 0.72,
    signals: 5,
    nodeR: 1.4,
    nodeRDepth: 1.8,
    lineW: 0.8,
    rsPow: 0.6,
    rMin: 0.3
  },
  braid: {
    strandN: 52,
    turns: 3.0,
    ghostN: 150,
    rBase: 1.2,
    rDepth: 1.8,
    rsPow: 0.6,
    rMin: 0.3
  },
  ribbon: {
    lanes: 5,
    segs: 88,
    ghostN: 150,
    rBase: 1.1,
    rDepth: 1.7,
    rsPow: 0.6,
    rMin: 0.3
  },
  // ring shares ribbon's painter; faceOn cancels the camera tilt and moves
  // the undulation onto the radius, and there is no ghost sphere behind it
  ring: {
    lanes: 5,
    segs: 88,
    ghostN: 0,
    faceOn: 1,
    rBase: 1.1,
    rDepth: 1.7,
    rsPow: 0.6,
    rMin: 0.3
  },
  morph: {
    rDot: 0.021,
    iconD: 1,
    rMin: 0.25
  }
};

/* ── engine/orbits.ts ───────────────────────────────────────── */
// Orbits: particles on tilted orbits — the "working" state. No nucleus
// (the tuned preset runs coreless): just ghost paths and the particles
// doing the work.

const frameOrbits            = (size, t, o) => {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.82;
  const pt = makeProj(t * 0.12, 0.3, cx, cy, 1);
  const rs = radiusScale(size, o.rsPow ?? 0.6);

  const dots        = [];
  const orbitN = o.orbitN ?? 12;
  const ghostN = o.ghostN ?? 40;
  const particles = o.particles ?? 3;

  // orbits: each a tilted circle — a ghost path + running particles
  for (let orb = 0; orb < orbitN; orb++) {
    const h1 = hashD(orb, 1.7);
    const h2 = hashD(orb, 5.2);
    const h3 = hashD(orb, 8.9);
    const ro = R * (0.45 + 0.52 * h1);
    const th = h1 * 2 * Math.PI;
    const phi = Math.acos(2 * h2 - 1);
    // orbit plane basis (u, v ⟂ normal n)
    const nx = Math.sin(phi) * Math.cos(th);
    const ny = Math.cos(phi);
    const nz = Math.sin(phi) * Math.sin(th);
    let ux = -ny;
    let uy = nx;
    const uz = 0;
    const ul = Math.max(1e-6, Math.sqrt(ux * ux + uy * uy));
    ux /= ul;
    uy /= ul;
    const vx = ny * uz - nz * uy;
    const vy = nz * ux - nx * uz;
    const vz = nx * uy - ny * ux;
    const speed = (0.25 + 0.55 * h3) * (h3 > 0.5 ? 1 : -1);

    // ghost path
    for (let k = 0; k < ghostN; k++) {
      const a = (k / ghostN) * 2 * Math.PI;
      const [px, py, z] = pt(
        (ux * Math.cos(a) + vx * Math.sin(a)) * ro,
        (uy * Math.cos(a) + vy * Math.sin(a)) * ro,
        (uz * Math.cos(a) + vz * Math.sin(a)) * ro
      );
      const depth = (z / ro + 1) / 2;
      dots.push({
        x: px,
        y: py,
        z,
        r: (o.ghostR ?? 0.9) * rs,
        white: 0.72,
        a: (o.ghostA ?? 0.5) * (0.4 + 0.6 * depth)
      });
    }
    // the particles doing the work
    for (let m = 0; m < particles; m++) {
      const a = t * speed + (m / particles) * 2 * Math.PI + h2 * 6;
      const [px, py, z] = pt(
        (ux * Math.cos(a) + vx * Math.sin(a)) * ro,
        (uy * Math.cos(a) + vy * Math.sin(a)) * ro,
        (uz * Math.cos(a) + vz * Math.sin(a)) * ro
      );
      const depth = (z / ro + 1) / 2;
      dots.push({
        x: px,
        y: py,
        z,
        r: ((o.partR ?? 1.2) + (o.partRDepth ?? 1.6) * depth) * rs,
        white: 0.3 - 0.22 * depth
      });
    }
  }
  return finalizeFrame(dots, [], o.rMin);
};

/* ── engine/lattice.ts ───────────────────────────────────────── */
// The sphere-lattice modes: globe (searching), rubik (solving) and
// wave (listening). All draw a lat/long dot field with mode-specific
// motion, then hand off to the shared z-sorted painter.

// --- the shared solver heartbeat (rubik) ------------------------------
// Rapid eased moves scramble, then replay in reverse (palindrome) so
// everything clicks back to solved, rests, repeats.

                
                  
             
             
              
 

function solveCycle(time        , count        , slotDur        , rest        ) {
  const cyc = 2 * count * slotDur + rest;
  const tc = time % cyc;
  const amount = new Array        (count).fill(0);
  let active = -1;
  if (tc < 2 * count * slotDur) {
    const slot = Math.floor(tc / slotDur);
    const p = (tc - slot * slotDur) / slotDur;
    const cl = Math.min(1, p / 0.7);
    const ep = 1 - (1 - cl) ** 3; // machine ease-out
    if (slot < count) {
      for (let i = 0; i < slot; i++) amount[i] = 1;
      amount[slot] = ep;
      active = slot;
    } else {
      const u = 2 * count - 1 - slot;
      for (let i = 0; i < u; i++) amount[i] = 1;
      amount[u] = 1 - ep;
      active = u;
    }
  }
  return { amount, active };
}

function applyMoves(
  pt3                          ,
  moves        ,
  sc                                      
)                                    {
  let [x, y, z] = pt3;
  let inActive = false;
  for (let i = 0; i < moves.length; i++) {
    if (sc.amount[i] <= 0) continue;
    const mv = moves[i];
    const coord = mv.axis === 0 ? x : mv.axis === 1 ? y : z;
    if (coord < mv.lo || coord >= mv.hi) continue;
    if (i === sc.active) inActive = true;
    const a = mv.ang * sc.amount[i];
    const ca = Math.cos(a);
    const sa = Math.sin(a);
    if (mv.axis === 0) {
      const y2 = y * ca - z * sa;
      z = y * sa + z * ca;
      y = y2;
    } else if (mv.axis === 1) {
      const x2 = x * ca + z * sa;
      z = -x * sa + z * ca;
      x = x2;
    } else {
      const x2 = x * ca - y * sa;
      y = x * sa + y * ca;
      x = x2;
    }
  }
  return [x, y, z, inActive];
}

function makeMoves(count        )         {
  const moves         = [];
  for (let i = 0; i < count; i++) {
    const axis = Math.min(2, Math.floor(hashD(i, 2.3) * 3))             ;
    const lo = -1.0 + 0.5 * Math.min(3, Math.floor(hashD(i, 5.9) * 4));
    const dir = hashD(i, 7.7) < 0.5 ? 1 : -1;
    moves.push({ axis, lo, hi: lo + 0.5, ang: (dir * Math.PI) / 2 });
  }
  return moves;
}

// --- Globe: lat/long field, a scan meridian sweeps — searching --------

const frameGlobe            = (size, t, o) => {
  const spin = 0.5;
  const cx = size / 2;
  const cy = size / 2;
  const radius = (size / 2) * 0.82;
  const tilt = 0.4 + 0.06 * Math.sin(t * 0.35);
  const pt = makeProj(t * spin, tilt, cx, cy, radius);
  // scan sweeps relative to the spin; scanMul scales that relative rate
  const scan = t * (spin + (1.7 - spin) * (o.scanMul ?? 1));
  const rs = radiusScale(size, o.rsPow ?? 0.6);
  const dimBase = o.dimBase ?? 1;

  const dots        = [];
  const latRings = o.latRings ?? 17;
  const lonDensity = o.lonDensity ?? 44;
  for (let li = 0; li <= latRings; li++) {
    const lat = -Math.PI / 2 + (li / latRings) * Math.PI;
    const cosLat = Math.cos(lat);
    const sinLat = Math.sin(lat);
    const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity));
    for (let lj = 0; lj < lonCount; lj++) {
      const lon = (lj / lonCount) * 2 * Math.PI;
      const [px, py, z] = pt(cosLat * Math.cos(lon), sinLat, cosLat * Math.sin(lon));
      const depth = (z + 1) / 2;
      // the scan: a moving meridian read as a size ripple, not a shine
      const d = angleDelta(lon + t * spin, scan);
      const boost = Math.exp(-(d * d) / 0.18) * Math.max(0, z);
      dots.push({
        x: px,
        y: py,
        z,
        r: ((o.rBase ?? 0.6) + (o.rDepth ?? 1.7) * depth + (o.rBoost ?? 1) * boost) * rs,
        white: (o.inkFar ?? 0.62) - (o.inkSpan ?? 0.54) * depth,
        // dimBase < 1 fades un-scanned dots so the meridian reads clearly
        a: dimBase + (1 - dimBase) * Math.min(1, boost)
      });
    }
  }
  return finalizeFrame(dots, [], o.rMin);
};

// --- Rubik: bands twist in quarter turns, scramble → solve — solving --

const frameRubik            = (size, t, o) => {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.82;
  const pt = makeProj(t * 0.55, 0.35 + 0.1 * Math.sin(t * 0.9), cx, cy, R);
  const rs = radiusScale(size, o.rsPow ?? 0.6);
  const moveCount = o.moveCount ?? 14;
  const moves = makeMoves(moveCount);
  const sc = solveCycle(t, moveCount, 0.42, 1.2);

  const dots        = [];
  const latRings = o.latRings ?? 15;
  const lonDensity = o.lonDensity ?? 40;
  for (let li = 0; li <= latRings; li++) {
    const lat = -Math.PI / 2 + (li / latRings) * Math.PI;
    const cosLat = Math.cos(lat);
    const sinLat = Math.sin(lat);
    const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity));
    for (let lj = 0; lj < lonCount; lj++) {
      const lon = (lj / lonCount) * 2 * Math.PI;
      const [x, y, z, inActive] = applyMoves([cosLat * Math.cos(lon), sinLat, cosLat * Math.sin(lon)], moves, sc);
      const [px, py, zr] = pt(x, y, z);
      const depth = (zr + 1) / 2;
      // the band being turned inks a touch darker — the "hand"
      dots.push({
        x: px,
        y: py,
        z: zr,
        r: ((o.rBase ?? 0.6) + (o.rDepth ?? 1.7) * depth + (inActive ? (o.rActive ?? 0.3) : 0)) * rs,
        white: (o.inkFar ?? 0.62) - (o.inkSpan ?? 0.54) * depth - (inActive ? 0.14 : 0)
      });
    }
  }
  return finalizeFrame(dots, [], o.rMin);
};

// --- Wave: a waveform rolls through the rings — listening -------------

const frameWave            = (size, t, o) => {
  const cx = size / 2;
  const cy = size / 2;
  // 0.76 base × 1.15 — the undulation pulls the sphere inward, so wave read
  // ~15% smaller than the other lattice modes; scaled up to match them
  const R = (size / 2) * 0.874;
  const pt = makeProj(t * 0.18, 0.38, cx, cy, 1);
  const rs = radiusScale(size, o.rsPow ?? 0.6);

  const dots        = [];
  const rings = o.rings ?? 15;
  const lonDensity = o.lonDensity ?? 40;
  for (let ri = 0; ri <= rings; ri++) {
    const lat = -Math.PI / 2 + (ri / rings) * Math.PI;
    const cosLat = Math.cos(lat);
    const sinLat = Math.sin(lat);
    // two waves, different tempi — organic, never quite repeating
    const w = 0.62 * Math.sin(t * 2.1 - ri * 0.52) + 0.38 * Math.sin(t * 1.27 + ri * 0.83);
    const rr = R * (0.88 + 0.105 * w);
    const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * lonDensity));
    for (let lj = 0; lj < lonCount; lj++) {
      const lon = (lj / lonCount) * 2 * Math.PI;
      const [px, py, z] = pt(cosLat * Math.cos(lon) * rr, sinLat * rr, cosLat * Math.sin(lon) * rr);
      const depth = (z / R + 1) / 2;
      const crest = Math.max(0, w);
      dots.push({
        x: px,
        y: py,
        z,
        r: ((o.rBase ?? 0.6) + (o.rDepth ?? 1.7) * depth) * (1 + 0.4 * crest) * rs,
        white: 0.66 - 0.56 * depth - 0.1 * crest
      });
    }
  }
  return finalizeFrame(dots, [], o.rMin);
};

/* ── engine/ribbon.ts ───────────────────────────────────────── */
// Ribbon: an undulating sash of parallel strands rides a great circle —
// the "composing" state. The tuned preset freezes the 3D tumble
// (spin 0), leaving the traveling undulation on a fixed band.
//
// The same painter also drives "breathing" (ring), via the `faceOn` flag:
// a face-on circle whose radius — not its out-of-plane offset — undulates,
// so it reads as a ring slowly morphing rather than a sash in orbit.

const frameRibbon            = (size, t, o) => {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.78;
  // spin scales the 3D tumble; spin=0 freezes the band's orientation,
  // leaving only the traveling undulation
  const spin = o.spin ?? 1;
  const camTilt = 0.3;
  const pt = makeProj(t * 0.1 * spin, camTilt, cx, cy, 1);
  const rs = radiusScale(size, o.rsPow ?? 0.6);

  const dots        = [];
  const ghostN = o.ghostN ?? 150;
  for (let i = 0; i < ghostN; i++) {
    const d = fibDir(i, ghostN);
    const [px, py, z] = pt(d[0] * R, d[1] * R, d[2] * R);
    const depth = (z / R + 1) / 2;
    dots.push({ x: px, y: py, z, r: 0.8 * rs, white: 0.78, a: 0.1 + 0.22 * depth });
  }

  // The band plane, precessing (frozen when spin=0). The projection squashes
  // the band's great circle vertically by cos(ta + camTilt); face-on sets
  // ta = -camTilt so that term is 1 and the band reads as a true circle
  // rather than ribbon's tilted ellipse.
  const ya = t * 0.24 * spin;
  const ta = o.faceOn ? -camTilt : 0.55 + 0.3 * Math.sin(t * 0.18) * spin;
  const ux = Math.cos(ya);
  const uy = 0;
  const uz = Math.sin(ya);
  const vx = -uz * Math.sin(ta);
  const vy = Math.cos(ta);
  const vz = ux * Math.sin(ta);
  // plane normal n = u × v
  const nx = uy * vz - uz * vy;
  const ny = uz * vx - ux * vz;
  const nz = ux * vy - uy * vx;

  // Radial lobes swell past R, so pull the base radius in by (most of) the
  // wobble amplitude. The silhouette then stays inside the frame however far
  // the deformation is pushed, while lobes keep getting deeper relative to
  // the mean radius.
  const wobAmp = 0.23 * (o.wobMul ?? 1);
  const baseR = o.faceOn ? R / (1 + 0.85 * wobAmp) : R;

  const baseLanes = o.lanes ?? 5;
  const segs = o.segs ?? 88;
  const lanes = Math.max(1, Math.round(baseLanes * (o.bandMul ?? 1)));
  for (let w = 0; w < lanes; w++) {
    const laneOff = (w - (lanes - 1) / 2) * 0.075;
    const edge = Math.abs(w - (lanes - 1) / 2) / Math.max(1, (lanes - 1) / 2);
    for (let k = 0; k < segs; k++) {
      const a = (k / segs) * 2 * Math.PI;
      // the undulation: two traveling waves along the band; wobMul
      // scales the deformation — 0 is a clean band
      const wob =
        (0.16 * Math.sin(a * 3 - t * 1.7 + w * 0.22) + 0.07 * Math.sin(a * 5 + t * 1.1)) * (o.wobMul ?? 1);
      // A normal-direction wobble is cancelled by the re-normalisation below:
      // the point lands back on the sphere, so the silhouette is pinned at R
      // and the deformation can only ever pull dots inward. Face-on instead
      // modulates the in-plane RADIUS, so lobes genuinely swell outward and
      // pinch inward. Ribbon keeps the original out-of-plane sash wobble.
      const radial = o.faceOn ? 1 + wob : 1;
      const off = o.faceOn ? laneOff : laneOff + wob;
      const x = ux * Math.cos(a) + vx * Math.sin(a) + nx * off;
      const y = uy * Math.cos(a) + vy * Math.sin(a) + ny * off;
      const z = uz * Math.cos(a) + vz * Math.sin(a) + nz * off;
      const l = Math.sqrt(x * x + y * y + z * z);
      const rr = baseR * radial;
      const [px, py, zr] = pt((x / l) * rr, (y / l) * rr, (z / l) * rr);
      const depth = (zr / R + 1) / 2;
      dots.push({
        x: px,
        y: py,
        z: zr,
        r: ((o.rBase ?? 1.1) + (o.rDepth ?? 1.7) * depth) * (1 - 0.25 * edge) * rs,
        white: 0.52 - 0.44 * depth + 0.18 * edge,
        a: 0.4 + 0.6 * depth
      });
    }
  }
  return finalizeFrame(dots, [], o.rMin);
};

/* ── engine/morph.ts ───────────────────────────────────────── */
// Morph: a dotted outline cycling circle → triangle → square → circle —
// the "shaping" state. Each shape is a continuous closed path
// parameterised by arc length (top-centre start, clockwise). Every
// frame the engine blends the two neighbouring paths, then lays the
// dots EVENLY along the blended outline — spacing stays uniform at
// every instant of the morph, holds and transitions alike. Plain
// circle fills only: no canvas/SVG filters, fully cross-browser.

function smoothE(x        )         {
  return x * x * (3 - 2 * x);
}

function polyPath(verts                                          )       {
  const V = verts.length;
  const L           = [];
  let total = 0;
  for (let i = 0; i < V; i++) {
    const a = verts[i];
    const b = verts[(i + 1) % V];
    const l = Math.hypot(b[0] - a[0], b[1] - a[1]);
    L.push(l);
    total += l;
  }
  return (f) => {
    let target = f * total;
    let i = 0;
    while (target > L[i] && i < V - 1) {
      target -= L[i];
      i++;
    }
    const a = verts[i];
    const b = verts[(i + 1) % V];
    const ff = L[i] ? Math.min(1, target / L[i]) : 0;
    return [a[0] + (b[0] - a[0]) * ff, a[1] + (b[1] - a[1]) * ff];
  };
}

const CIRCLE       = (f) => {
  const a = -Math.PI / 2 + f * 2 * Math.PI;
  return [Math.cos(a) * 0.24, Math.sin(a) * 0.24];
};
const TRIANGLE = polyPath([
  [0.0, -0.26],
  [0.24, 0.16],
  [-0.24, 0.16]
]);
// 5-vertex walk so the path STARTS at top-centre like the other shapes
const SQUARE = polyPath([
  [0, -0.2],
  [0.2, -0.2],
  [0.2, 0.2],
  [-0.2, 0.2],
  [-0.2, -0.2]
]);
const CYCLE         = [CIRCLE, TRIANGLE, SQUARE];

// low floor keeps sparse outlines possible while never degenerating
function morphN(d        )         {
  return Math.max(6, Math.round(34 * d));
}

const HOLD = 1.4;
const MORPH = 0.9;
const SEG = HOLD + MORPH;

// This state was tuned in inkform, which paints it through a blur +
// threshold "goo" filter; we draw plain circles instead, since `ctx.filter`
// and SVG filter refs are not safe to rely on across Chrome / Safari /
// Firefox. The dot GEOMETRY is identical either way — the threshold just
// yields a hard edge where a plain fill has an antialiased one, so these
// dots read a touch softer than inkform's. Don't "correct" for that by
// shrinking the radius: it makes the mark genuinely smaller than the tuning.

const frameMorph            = (size, t, o) => {
  const K = CYCLE.length;
  const tc = t % (SEG * K);
  const k = Math.floor(tc / SEG);
  const local = tc - k * SEG;
  const m = local > HOLD ? smoothE((local - HOLD) / MORPH) : 0;
  const sprd = o.spread ?? 1;

  // blend the two shape PATHS at m, then measure the blended outline
  const pA = CYCLE[k];
  const pB = CYCLE[(k + 1) % K];
  const M = 160;
  const pts                          = [];
  for (let i = 0; i < M; i++) {
    const f = i / M;
    const a = pA(f);
    const b = pB(f);
    pts.push([(a[0] + (b[0] - a[0]) * m) * sprd, (a[1] + (b[1] - a[1]) * m) * sprd]);
  }
  const L           = [];
  let total = 0;
  for (let i = 0; i < M; i++) {
    const a = pts[i];
    const b = pts[(i + 1) % M];
    const l = Math.hypot(b[0] - a[0], b[1] - a[1]);
    L.push(l);
    total += l;
  }

  // dot radius depends ONLY on rDot (the size knob); the count sets the
  // gaps. Formed shapes breathe a little (uniform pulse).
  const n = morphN(o.iconD ?? 1);
  const re = (o.rDot ?? 0.021) * 1.35 * sprd;
  const pulse = 1 + 0.02 * Math.sin(local * 3.1);

  const dots        = [];
  const c2 = size / 2;
  let seg = 0;
  let acc = 0;
  for (let k2 = 0; k2 < n; k2++) {
    const target = (k2 / n) * total;
    while (acc + L[seg] < target && seg < M - 1) {
      acc += L[seg];
      seg++;
    }
    const a = pts[seg];
    const b = pts[(seg + 1) % M];
    const f = L[seg] ? Math.min(1, (target - acc) / L[seg]) : 0;
    const x = (a[0] + (b[0] - a[0]) * f) * pulse;
    const y = (a[1] + (b[1] - a[1]) * f) * pulse;
    dots.push({
      x: c2 + x * size,
      y: c2 + y * size,
      z: 0,
      r: Math.max(0.35, re * size),
      white: 0.1
    });
  }
  return finalizeFrame(dots, [], o.rMin);
};

/* ── engine/braid.ts ───────────────────────────────────────── */
// Braid: three strands plait around the sphere — the "weaving" state.
// Each strand runs pole to pole on a helix, and a radial breathing term
// makes them trade places, reading as the over/under of a plait.

const frameBraid            = (size, t, o) => {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.76;
  const pt = makeProj(t * 0.4, 0.3, cx, cy, 1);
  const rs = radiusScale(size, o.rsPow ?? 0.6);

  const dots        = [];
  const ghostN = o.ghostN ?? 150;
  for (let i = 0; i < ghostN; i++) {
    const d = fibDir(i, ghostN);
    const [px, py, z] = pt(d[0] * R, d[1] * R, d[2] * R);
    const depth = (z / R + 1) / 2;
    dots.push({ x: px, y: py, z, r: 0.8 * rs, white: 0.78, a: 0.1 + 0.22 * depth });
  }

  const strandN = o.strandN ?? 52;
  const turns = o.turns ?? 3;
  for (let s = 0; s < 3; s++) {
    const phase = (s / 3) * 2 * Math.PI;
    for (let i = 0; i < strandN; i++) {
      // u walks pole to pole; the frac() drift slides the whole strand along
      const u = (frac(i / strandN + t * 0.045) * 2 - 1) * 0.96;
      const surf = Math.sqrt(Math.max(0, 1 - u * u));
      const endFade = Math.min(1, (1 - Math.abs(u)) / 0.1);
      const a = u * Math.PI * turns + phase;
      // radial breathing: strands trade places — the over/under of a plait
      const weave = 1 + 0.075 * Math.sin(u * Math.PI * turns * 2 + phase * 2 + t * 0.8);
      const rr = surf * R * weave;
      const [px, py, zr] = pt(Math.cos(a) * rr, u * R * weave, Math.sin(a) * rr);
      const depth = (zr / R + 1) / 2;
      dots.push({
        x: px,
        y: py,
        z: zr,
        r: ((o.rBase ?? 1.2) + (o.rDepth ?? 1.8) * depth) * rs,
        white: 0.55 - 0.45 * depth,
        a: endFade * (0.45 + 0.55 * depth)
      });
    }
  }
  return finalizeFrame(dots, [], o.rMin);
};

/* ── engine/web.ts ───────────────────────────────────────── */
// Web: a constellation wires itself — the "connecting" state. Nodes drift
// on the sphere under slow value noise; any pair closer than `thr` grows an
// edge, and bright packets run along randomly re-picked node pairs.

const frameWeb            = (size, t, o) => {
  const cx = size / 2;
  const cy = size / 2;
  const R = (size / 2) * 0.8 * (o.spread ?? 1);
  // note the projector carries the radius as its scale, so node vectors stay
  // unit-length and distances below are in unit-sphere space
  const pt = makeProj(t * 0.12, 0.32, cx, cy, R);
  const rs = radiusScale(size, o.rsPow ?? 0.6);

  const nodeN = o.nodeN ?? 30;
  const thr = o.thr ?? 0.72;
  const nodeR = o.nodeR ?? 1.4;
  const nodeRDepth = o.nodeRDepth ?? 1.8;

  // nodes: fib lattice + slow noise wander, renormalised to the surface
  const nodes                                  = [];
  for (let i = 0; i < nodeN; i++) {
    const d = fibDir(i, nodeN);
    const x = d[0] + 0.3 * (vnoise(i * 0.31 + 9, t * 0.24) - 0.5) * 2;
    const y = d[1] + 0.3 * (vnoise(i * 0.53 + 27, t * 0.21) - 0.5) * 2;
    const z = d[2] + 0.3 * (vnoise(i * 0.77 + 55, t * 0.27) - 0.5) * 2;
    const l = Math.sqrt(x * x + y * y + z * z);
    nodes.push([x / l, y / l, z / l]);
  }

  const lines         = [];
  const dots        = [];

  // edges between close neighbours, alpha by proximity + depth
  for (let i = 0; i < nodeN; i++) {
    for (let j = i + 1; j < nodeN; j++) {
      const dx = nodes[i][0] - nodes[j][0];
      const dy = nodes[i][1] - nodes[j][1];
      const dz = nodes[i][2] - nodes[j][2];
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
      if (dist >= thr) continue;
      const [x1, y1, z1] = pt(nodes[i][0], nodes[i][1], nodes[i][2]);
      const [x2, y2, z2] = pt(nodes[j][0], nodes[j][1], nodes[j][2]);
      const depth = ((z1 + z2) / 2 + 1) / 2;
      lines.push({
        x1,
        y1,
        x2,
        y2,
        white: 0.42,
        a: (1 - dist / thr) * (0.3 + 0.55 * depth),
        w: Math.max(0.6, (o.lineW ?? 0.8) * rs)
      });
    }
  }

  for (let i = 0; i < nodeN; i++) {
    const [px, py, z] = pt(nodes[i][0], nodes[i][1], nodes[i][2]);
    const depth = (z + 1) / 2;
    const pulse = 1 + 0.25 * Math.sin(t * 1.4 + i * 2.7);
    dots.push({
      x: px,
      y: py,
      z,
      r: (nodeR + nodeRDepth * depth) * pulse * rs,
      white: 0.55 - 0.45 * depth
    });
  }

  // signals: bright packets running between paired nodes
  const signals = o.signals ?? 5;
  for (let s = 0; s < signals; s++) {
    const seg = Math.floor(t * 0.55 + s * 7.31);
    const a = Math.floor(hashD(seg, s * 3.1 + 1.7) * nodeN);
    const b = Math.floor(hashD(seg, s * 5.7 + 4.2) * nodeN);
    if (a === b) continue;
    const f = frac(t * 0.55 + s * 7.31);
    const x = lerp(nodes[a][0], nodes[b][0], f);
    const y = lerp(nodes[a][1], nodes[b][1], f);
    const z = lerp(nodes[a][2], nodes[b][2], f);
    const l = Math.max(1e-6, Math.sqrt(x * x + y * y + z * z));
    const [px, py, zr] = pt(x / l, y / l, z / l);
    const depth = (zr + 1) / 2;
    dots.push({
      x: px,
      y: py,
      z: zr,
      r: (nodeR * 1.5 + nodeRDepth * depth) * rs,
      white: 0.05,
      a: 0.5 + 0.5 * depth
    });
  }

  return finalizeFrame(dots, lines, o.rMin);
};

/* ── engine/registry.ts ───────────────────────────────────────── */
// Mode key → geometry builder. Kept separate from the presets so tree
// shaking can in principle drop unused modes in custom builds.







/**
 * The portable surface: pure geometry, no canvas. The React Native port
 * imports exactly these functions, so its output is identical to the web's
 * by construction rather than by re-implementation.
 */
const MODE_FRAMES                             = {
  orbits: frameOrbits,
  globe: frameGlobe,
  rubik: frameRubik,
  wave: frameWave,
  web: frameWeb,
  braid: frameBraid,
  ribbon: frameRibbon,
  // ring shares ribbon's geometry — the `faceOn` profile flag switches it
  ring: frameRibbon,
  morph: frameMorph
};

/** Canvas painters, derived from the geometry. The 2D-canvas binding. */
const MODE_DRAWS                            = Object.fromEntries(
  Object.entries(MODE_FRAMES).map(([key, frame]) => [
    key,
    ((ctx, size, t, dark, opts) => paintFrame(ctx, frame(size, t, opts), dark))            
  ])
)                             ;

/* ── presets.ts ───────────────────────────────────────── */
// The shipped tunings: nine states × two sizes, baked from the inkform
// mini-page tuning session. `count`/`size` are multipliers over the base
// fine profiles; `speed` multiplies the shared clock. Resolved once per
// (state, size) pair and cached — the render loop sees plain numbers.

const STATE_TO_MODE                            = {
  working: 'orbits',
  searching: 'globe',
  solving: 'rubik',
  listening: 'wave',
  connecting: 'web',
  weaving: 'braid',
  composing: 'ribbon',
  breathing: 'ring',
  shaping: 'morph'
};

                         
                
                
               
                                                       
                   
 

/** Exported so `scripts/extract-spec.ts` can emit them for the native ports. */
const PRESETS                                           = {
  orbits: {
    64: { speed: 1.885, count: 1, size: 1 },
    20: { speed: 3.9, count: 0.238, size: 2.4 }
  },
  globe: {
    64: { speed: 2.015, count: 0.42, size: 1.15, extra: { scanMul: 4.08, dimBase: 0.45 } },
    20: { speed: 2.665, count: 0.105, size: 1.75, extra: { scanMul: 4.335, dimBase: 0.45 } }
  },
  rubik: {
    64: { speed: 1.82, count: 0.35, size: 1.05 },
    20: { speed: 1.95, count: 0.088, size: 1.9 }
  },
  wave: {
    64: { speed: 4.388, count: 0.341, size: 1 },
    20: { speed: 3.998, count: 0.105, size: 1.6 }
  },
  web: {
    64: { speed: 3.315, count: 1.35, size: 0.95 },
    20: { speed: 6.63, count: 0.25, size: 1.52 }
  },
  braid: {
    64: { speed: 1.625, count: 0.5, size: 1 },
    20: { speed: 2.75, count: 0.1125, size: 1.36 }
  },
  ribbon: {
    64: { speed: 2.34, count: 0.25, size: 0.85, extra: { spin: 0, bandMul: 3.9, wobMul: 1 } },
    20: { speed: 3.12, count: 0.051, size: 1.073, extra: { spin: 0, bandMul: 4.94, wobMul: 1 } }
  },
  ring: {
    64: { speed: 3.24, count: 0.25, size: 0.956, extra: { spin: 0, bandMul: 3.627, wobMul: 0.368 } },
    20: { speed: 3.78, count: 0.028, size: 1.622, extra: { spin: 0, bandMul: 3.968, wobMul: 0.565 } }
  },
  morph: {
    64: { speed: 2.405, count: 0.702, size: 0.395, extra: { spread: 1.45 } },
    20: { speed: 2.08, count: 0.53, size: 1.011, extra: { spread: 1.45 } }
  }
};

                           
                
                
                 
 

const cache = new Map                  ();

/** Resolve a (state, size) pair to its mode + fully-scaled draw options. */
function resolvePreset(state          , size         )           {
  const key = `${state}-${size}`;
  const hit = cache.get(key);
  if (hit) return hit;

  const mode = STATE_TO_MODE[state];
  const preset = PRESETS[mode][size];
  let opts           = { ...BASE_PROFILES[mode] };
  if (preset.count !== 1) opts = scaleCounts(opts, preset.count);
  if (preset.size !== 1) opts = scaleRadii(opts, preset.size);
  if (preset.extra) opts = { ...opts, ...preset.extra };

  const resolved           = { mode, speed: preset.speed, opts };
  cache.set(key, resolved);
  return resolved;
}

/* ── Aethron's painter and mount ─────────────────────────────────────
   The geometry above is Jakub Antalik's, unmodified. Everything below
   is ours, and it exists because of one difference: thinking-orbs is
   deliberately MONOCHROME, and Aethron's whole identity is the ember
   bloom on black. Grey orbs beside a terracotta field would read as a
   component borrowed from somewhere else.
   So the ink value the geometry reports is run through the SAME ramp
   the hero field uses — ember -> terracotta -> warm white with depth —
   and the orb becomes the same light as the room it sits in.

   AND IT HAS TO MEAN SOMETHING. The library ships nine states because
   an agent does nine distinguishable kinds of work; Aethron's run card
   already names exactly that in words. Mapping one to the other is the
   whole reason to take this: until now every verb got the identical
   74x16 dot strip, which told a person only THAT something was
   happening. */
const ORB_STATE={
  // reading and looking: a scan meridian sweeping a dotted globe
  Reading:'searching',Searching:'searching','Looking for':'searching',
  'Searching the web':'searching',
  // a command: particles on tilted orbits
  Running:'working',
  // pulling things in and wiring them up: a constellation assembling
  Fetching:'connecting','Naming elements':'connecting',
  // writing something new: an undulating sash
  Creating:'composing',Planning:'composing',
  // something taking form: a dotted outline circle -> triangle -> square
  Editing:'shaping',Starting:'shaping',
  // many parts plaited into one: three strands braiding
  Building:'weaving','Working on':'weaving',
  // scramble, then click back solved
  Checking:'solving',Healing:'solving','Running the page':'solving',
  // nothing named yet
  Thinking:'listening'};
const orbState=v=>ORB_STATE[v]||'breathing';

/* ONE CLOCK. Every orb on the page reads the same elapsed time, so two
   indicators never drift apart — the library's own rule, kept. */
let ORB_T0=null, ORB_RAF=0;
const ORB_LIVE=new Set();
function orbTick(){
  ORB_RAF=0;
  if(!ORB_LIVE.size)return;
  const now=performance.now();
  if(ORB_T0===null)ORB_T0=now;
  const el=(now-ORB_T0)/1000;
  for(const o of ORB_LIVE)o.draw(el);
  ORB_RAF=requestAnimationFrame(orbTick);
}
function orbWake(){if(!ORB_RAF&&ORB_LIVE.size)ORB_RAF=requestAnimationFrame(orbTick);}

class Orb{
  constructor(cv,state,size){
    this.cv=cv; this.cx=cv.getContext('2d');
    this.size=size||20;
    this.set(state);
    this.seen=true;
    const d=Math.min(devicePixelRatio||1,2);   // the library's cap, kept
    cv.width=Math.round(this.size*d); cv.height=Math.round(this.size*d);
    cv.style.width=this.size+'px'; cv.style.height=this.size+'px';
    this.d=d;
    if('IntersectionObserver' in window){
      this._io=new IntersectionObserver(es=>{
        this.seen=es[0].isIntersecting; this.seen?this.start():this.stop();
      },{threshold:0}); this._io.observe(cv);
    }
    document.addEventListener('visibilitychange',()=>{
      document.hidden?this.stop():(this.seen&&this.start());});
  }
  set(state){
    this.state=state;
    const r=resolvePreset(state,this.size===64?64:20);
    this.mode=r.mode; this.speed=r.speed; this.opts=r.opts;
    if(REDUCED)this.draw(1.7);   // one representative frame, then nothing
  }
  start(){ if(REDUCED){this.draw(1.7);return;} ORB_LIVE.add(this); orbWake(); }
  stop(){ ORB_LIVE.delete(this); }
  draw(el){
    const c=this.cx, S=this.size, d=this.d;
    c.setTransform(d,0,0,d,0,0);
    c.clearRect(0,0,S,S);
    const f=MODE_FRAMES[this.mode](S, el*this.speed, this.opts);
    /* the library draws its lines first so nodes sit on top; keep that */
    for(const l of f.lines){
      const q=1-Math.min(1,Math.max(0,l.white));
      c.strokeStyle=ORB_INK(q,(l.a==null?1:l.a)*0.9);
      c.lineWidth=l.w; c.beginPath();
      c.moveTo(l.x1,l.y1); c.lineTo(l.x2,l.y2); c.stroke();
    }
    for(const p of f.dots){
      const q=1-Math.min(1,Math.max(0,p.white));   // dark substrate: near = bright
      c.fillStyle=ORB_INK(q,p.a==null?1:p.a);
      c.beginPath(); c.arc(p.x,p.y,p.r,0,6.2832); c.fill();
    }
  }
  destroy(){this.stop(); this._io&&this._io.disconnect();}
}
/* the hero field's ramp, to the letter, so both read as one light */
function ORB_INK(q,a){
  const warm=q*q;
  const r=Math.round(96+121*q+38*warm),
        g=Math.round(44+ 75*q+95*warm),
        b=Math.round(32+ 55*q+103*warm);
  return 'rgba('+r+','+g+','+b+','+(a<0?0:a>1?1:a).toFixed(3)+')';
}
const ORBS=new WeakMap();
function mountOrbs(root){
  (root||document).querySelectorAll('canvas.orb').forEach(cv=>{
    const want=cv.dataset.state||'breathing',
          size=+(cv.dataset.size||20);
    const had=ORBS.get(cv);
    if(had){ if(had.state!==want)had.set(want); return; }
    const o=new Orb(cv,want,size);
    ORBS.set(cv,o); o.start();
  });
}

/* THE CODING TOOLS SPEAK TOO. The template verbs above were only half of
   it: the same session runs Read, Write, Edit, Bash and the rest, and
   those were reaching the surface as their raw class names next to a
   blob of JSON. A person does not want to read `Bash {"command":...}`;
   they want to know a command is running and WHICH. */
const VERBS={Read:'Reading',Write:'Creating',Edit:'Editing',
  MultiEdit:'Editing',NotebookEdit:'Editing',Bash:'Running',
  Grep:'Searching',Glob:'Looking for',WebFetch:'Fetching',
  WebSearch:'Searching the web',TodoWrite:'Planning',Task:'Working on'};
function toolVerb(n){
  const raw=String(n||'');
  if(VERBS[raw])return VERBS[raw];
  const a=raw.replace(/^mcp__[a-z_]+__/,'');
  return TOOLWORDS[a]||a.replace(/_/g,' ').replace(/^./,c=>c.toUpperCase());
}
/* AND THE TARGET IS THE REAL ONE. Whatever the tool was actually
   pointed at — this file, this command, this page — never a summary
   invented for the display. */
function toolTarget(n,inp){
  inp=inp||{};const raw=String(n||'');
  const tail=p=>String(p||'').split('/').filter(Boolean).slice(-2).join('/');
  if(raw==='Bash')return String(inp.description||inp.command||'').slice(0,90);
  for(const k of ['file_path','page','path'])if(inp[k])return tail(inp[k]);
  for(const k of ['pattern','query','request','name','project','workspace'])
    if(inp[k])return String(inp[k]).slice(0,74);
  if(inp.url)return String(inp.url).replace(/^https?:\/\//,'').slice(0,64);
  if(inp.old_url)return tail(inp.old_url);
  return '';
}
/* Light formatting only, and ESCAPED FIRST — the text is a model's
   output, never markup we trust. */
/* A REPLY IS PROSE, NOT A DUMP OF ITS OWN SOURCE. fmt knew only inline
   code and bold, so every heading arrived as a literal "### Core
   Capabilities", every bullet as "* File Operations:" and every rule as
   "---". The model was writing correct markdown and the surface was
   showing its working. Escaping happens FIRST on every fragment and
   only then are tags emitted, so nothing a model writes can inject
   markup. Fenced code is lifted out whole before anything touches it,
   behind a private-use sentinel no reply can contain. */
const CBK='';
function fmt(t){
  const code=[];
  let s=String(t||'').replace(/\r\n?/g,'\n')
    .replace(/```([a-zA-Z0-9+#.-]*)\n([\s\S]*?)```/g,(m,lang,body)=>{
      code.push('<pre class="cb"><code>'+esc(body.replace(/\n+$/,''))+'</code></pre>');
      return CBK+(code.length-1)+CBK;});
  const inline=x=>esc(x)
    .replace(/`([^`\n]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g,'$1<i>$2</i>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out=[];let list=null;
  const shut=()=>{if(list){out.push('</'+list+'>');list=null;}};
  for(const raw of s.split('\n')){
    const line=raw.trimEnd(), t2=line.trim();
    const cb=t2.match(new RegExp('^'+CBK+'(\\d+)'+CBK+'$'));
    if(cb){shut();out.push(code[+cb[1]]);continue;}
    if(!t2){shut();continue;}
    let m;
    if((m=line.match(/^\s{0,3}(#{1,6})\s+(.*)$/))){
      shut();const n=Math.min(6,Math.max(2,m[1].length+1));
      out.push('<h'+n+'>'+inline(m[2].replace(/\s*#+\s*$/,''))+'</h'+n+'>');continue;}
    if(/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)){shut();out.push('<hr>');continue;}
    if((m=line.match(/^\s{0,3}>\s?(.*)$/))){
      shut();out.push('<blockquote>'+inline(m[1])+'</blockquote>');continue;}
    if((m=line.match(/^\s*\d+[.)]\s+(.*)$/))){
      if(list!=='ol'){shut();out.push('<ol>');list='ol';}
      out.push('<li>'+inline(m[1])+'</li>');continue;}
    if((m=line.match(/^\s*[-*+]\s+(.*)$/))){
      if(list!=='ul'){shut();out.push('<ul>');list='ul';}
      out.push('<li>'+inline(m[1])+'</li>');continue;}
    shut();out.push('<p>'+inline(t2)+'</p>');
  }
  shut();
  return out.join('');
}
/* The event stream becomes BLOCKS: consecutive tool calls collapse into
   one run card, and a result is folded back onto the step that caused
   it instead of landing as its own wall of text. */
function convBlocks(evs){
  const out=[];let run=null,n=0;
  for(const e of evs){
    if(e.type==='tool'){
      if(!run){run={kind:'run',steps:[]};out.push(run);}
      run.steps.push({i:n++,name:e.name,input:e.input,
        /* DERIVED, NOT STORED: if the turn is over and this step never
           got its result, it did not finish — showing it as live is the
           card claiming work is in progress after everything stopped. */
        state:CODE.poll?'live':'bad'});
      continue;
    }
    if(e.type==='tool_result'){
      if(run)for(let i=run.steps.length-1;i>=0;i--){
        const s=run.steps[i];
        if(s.state==='live'){s.state=(e.ok===false?'bad':'ok');
          s.detail=e.text||'';break;}
      }
      continue;
    }
    run=null;
    if(e.type==='you')out.push({kind:'you',text:e.text});
    else if(e.type==='text'&&String(e.text||'').trim())
      out.push({kind:'bot',text:e.text});
    else if(e.type==='thinking'&&String(e.text||'').trim())
      out.push({kind:'think',text:e.text});
    else if(e.type==='ready')
      out.push({kind:'sys',text:'Connected to '+(e.model||'the model')});
    else if(e.type==='error')out.push({kind:'sys',...humanErr(e.text)});
    else if(e.type==='done'&&e.error)
      out.push({kind:'sys',...humanErr(e.text)});
    else if(e.type==='exit'&&e.code)
      out.push({kind:'sys',text:'the session ended',bad:true});
  }
  return out;
}
/* ── SAY WHAT HAPPENED, NOT WHAT THE SERVER SAID ────────────────────
   A provider's error body is written for a developer reading a log, and
   printing it whole is how a person ends up staring at a JSON blob with
   a rate-limit URL in it. The cases that actually occur get a sentence
   and the fix; the raw text is kept, one click down, because when it is
   NOT one of these the detail is the only thing that helps. */
function humanErr(raw){
  const t=String(raw||'');
  const has=(...k)=>k.every(x=>t.toLowerCase().includes(x));
  let msg=null;
  if(/\b429\b/.test(t)&&has('quota'))
    msg="Today's free quota on this key is spent. Aethron will use the "
       +"next key automatically; to keep going now, switch provider or "
       +"key in Settings.";
  else if(/\b429\b/.test(t))
    msg='The provider is rate-limiting this key. Waiting a moment usually clears it.';
  else if(/\b401\b|\b403\b/.test(t)||has('api key'))
    msg='That key was refused. Check it in Settings.';
  else if(has('timed out')||has('timeout'))
    msg='The provider stopped answering before it finished. Nothing was changed.';
  else if(has('getaddrinfo')||has('connection')||has('network')||has('dns'))
    msg='Could not reach the provider. Aethron did not ask, so nothing was changed.';
  else if(has('credit')||has('billing'))
    msg='This key has no credit left. Switch provider or top it up in Settings.';
  return msg?{text:msg,bad:true,detail:t}:{text:t,bad:true};
}
CODE.open=CODE.open||{};
function toggleStep(id){
  CODE.open[id]=!CODE.open[id];
  const d=$(id);if(d)d.hidden=!CODE.open[id];
}
CODE.openRun=CODE.openRun||{};
function toggleRun(k){
  CODE.openRun[k]=!runOpen(k);
  renderChat();
}
function runOpen(k){
  const b=CODE.openRun[k];
  return b===undefined?!RUN_DONE[k]:b;
}
const RUN_DONE={};
function runHtml(b){
  /* A run is finished when nothing in it is still live — no extra
     bookkeeping needed, because a step leaves `live` the moment its
     result lands. */
  const done=b.steps.every(x=>x.state!=='live');
  const key='r'+(b.steps[0]?b.steps[0].i:0);
  RUN_DONE[key]=done;
  const open=runOpen(key);
  const bad=b.steps.filter(x=>x.state==='bad').length;
  if(done&&!open){
    return `<div class="run"><button class="runsum"
      onclick="toggleRun('${key}')">
      ${I(bad?'alert':'check',13)}
      <span class="rs-n">${b.steps.length} step${b.steps.length>1?'s':''}</span>
      ${bad?`<span class="rs-bad">${bad} failed</span>`:''}
      <span class="st">${esc(toolVerb(b.steps[b.steps.length-1].name))}</span>
      <span class="rs-c">show${I('up',12)}</span></button></div>`;
  }
  return '<div class="run'+(done?' open':'')+'">'+(done?`<button
      class="runsum" onclick="toggleRun('${key}')">
      ${I(bad?'alert':'check',13)}
      <span class="rs-n">${b.steps.length} step${b.steps.length>1?'s':''}</span>
      ${bad?`<span class="rs-bad">${bad} failed</span>`:''}
      <span class="rs-c">hide${I('up',12)}</span></button>`:'')
    +b.steps.map(s=>{
    const id='sd'+s.i,det=String(s.detail||'').trim(),
          tgt=toolTarget(s.name,s.input);
    return `<button class="step ${s.state}" onclick="toggleStep('${id}')">
      <span class="sdot"></span><span class="sv">${esc(toolVerb(s.name))}</span>
      <span class="st">${esc(tgt)}</span>
      ${det?`<span class="sx">${s.state==='bad'?'failed':'detail'}</span>`:''}
      </button>${det?`<div class="stepdet${s.state==='bad'?' bad':''}"
      id="${id}" ${CODE.open[id]?'':'hidden'}>${esc(det.slice(0,4000))}</div>`:''}`;
  }).join('')+'</div>';
}
/* ONE LINE FOR RIGHT NOW. Not a spinner with no subject: the verb and
   the thing it is pointed at, and how far in we are. */
function nowHtml(){
  if(!CODE.poll)return '';
  let last=null;
  for(const e of CODE.events)if(e.type==='tool')last=e;
  const done=CODE.events.filter(e=>e.type==='tool_result').length;
  /* THE INDICATOR NOW SAYS WHICH KIND OF WORK. Every verb used to get
     the identical dot strip, which told a person only THAT something
     was happening. The orb's state is derived from the verb itself, so
     searching looks like searching and building looks like building. */
  const verb=last?toolVerb(last.name):'Thinking';
  return `<div class="nowline"><canvas class="orb" data-size="20"
      data-state="${orbState(verb)}"></canvas>
    <span class="sv">${esc(verb)}</span>
    <span class="st">${esc(last?toolTarget(last.name,last.input):'')}</span>
    ${done?`<span class="ac">${done} step${done>1?'s':''} done</span>`:''}</div>`;
}
function renderChat(){
  const box=$('chatlog');if(!box)return;
  const talking=!!CODE.events.length;
  box.hidden=!talking;
  /* THE WELCOME BECOMES THE CONVERSATION. Openers, suggestions and the
     file picker are scaffolding for the first message; once there IS a
     conversation they are only pushing the composer down the window. */
  for(const sel of ['welcome','qrows'])
    {const e=$(sel);if(e)e.hidden=talking;}
  const shell=$('convshell');
  if(shell)shell.classList.toggle('talking',talking);
  syncHero();
  document.querySelectorAll('.conv-dock .startmeta,.conv-dock .qrows')
    .forEach(e=>{e.hidden=talking});
  box.innerHTML=convBlocks(CODE.events).map(b=>
      b.kind==='you' ?`<div class="turn you"><div class="bubble">${esc(b.text)}</div></div>`
    : b.kind==='bot' ?`<div class="turn bot"><div class="prose">${fmt(b.text)}</div></div>`
    : b.kind==='think'?`<details class="think"><summary>${I('sparkles',12)
        }Aethron thought this through</summary><div class="body">${esc(b.text)}</div></details>`
    : b.kind==='run' ? runHtml(b)
    : b.text        ?`<div class="turn sys${b.bad?' bad':''}">${esc(b.text)}${
        b.detail?`<details class="errdet"><summary>what the provider said</summary>
          <div>${esc(b.detail)}</div></details>`:''}</div>`
    : '').join('')+nowHtml();
  dockMeta(); mountDots(box); mountOrbs(box);
  /* Follow the conversation only while the reader is already at the
     bottom. Yanking the view down while someone is reading back through
     a run is the rudest thing a live log can do. */
  const sc=$('convscroll');
  if(sc&&(CODE.stick===undefined||CODE.stick))sc.scrollTop=sc.scrollHeight;
}
/* The quiet line under the composer: what this session has cost, and
   what it is connected to. Money is never hidden and never shouted. */
function dockMeta(){
  const m=$('dockmeta');if(!m)return;
  let spent=0,model='';
  for(const e of CODE.events){
    if(e.type==='done'&&e.cost_usd)spent+=Number(e.cost_usd)||0;
    if(e.type==='ready'&&e.model)model=e.model;
  }
  const bits=[];
  if(model)bits.push(esc(model));
  if(spent)bits.push('$'+spent.toFixed(4)+' this session');
  if(CODE.poll)bits.push('working…');
  m.innerHTML=bits.length?bits.join('<span class="dot"></span>'):'';
}

let RT=0; // render token: async renderers must not overwrite a newer tab
/* A FAILURE BOUNDARY. Runs fn; if it throws, the error is REPORTED —
   in the pane it belongs to and in the log — instead of silently
   ending the render. An error the user can read is a bug report; an
   error that deforms the layout is a mystery. */
function guard(what, fn, host){
  try{ return fn(); }
  catch(e){
    const msg=(e&&e.message)||String(e);
    clientError(what+': '+msg, e&&e.stack);
    if(host){
      try{
        host.innerHTML='<div class="pane"><h3>'+esc(what)
          +' could not be drawn</h3>'
          +'<div class="hint">'+esc(msg)+'</div>'
          +'<div class="hint">The rest of Aethron is unaffected. '
          +'This has been written to the log.</div></div>';
      }catch(_){}
    }
    return null;
  }
}

/* Errors used to be invisible: no console is open in a desktop window,
   so a failure showed up only as a page that looked wrong. Now every
   one of them is written where it can be read back. */
function clientError(msg, stack){
  try{
    console.error(msg, stack||'');
    fetch('/api/clienterror',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({msg:String(msg).slice(0,2000),
                           stack:String(stack||'').slice(0,4000),
                           where:location.hash||'',
                           project:S&&S.cur||''})}).catch(()=>{});
  }catch(_){}
  try{ showErrBar(String(msg)); }catch(_){}
}

function showErrBar(msg){
  let b=$('errbar');
  if(!b){
    b=document.createElement('div'); b.id='errbar';
    b.style.cssText='position:fixed;left:50%;transform:translateX(-50%);'
      +'bottom:18px;z-index:10000;max-width:min(720px,90vw);'
      +'background:var(--panel);border:1px solid var(--line2);'
      +'border-left:3px solid var(--acc);border-radius:var(--r-md);'
      +'padding:10px 14px;font-size:12.5px;color:var(--tx);'
      +'box-shadow:0 12px 34px rgba(0,0,0,.45);cursor:pointer';
    b.onclick=()=>{openWork('logs');b.remove()};
    document.body.appendChild(b);
  }
  b.innerHTML='<b>Something failed</b><div style="color:var(--dim);'
    +'margin-top:3px">'+esc(msg).slice(0,220)
    +'</div><div style="color:var(--lo);margin-top:4px">'
    +'click to open the log</div>';
  clearTimeout(b._t); b._t=setTimeout(()=>b.remove(), 14000);
}

// Nothing gets to fail quietly any more, including code no guard wraps.
window.addEventListener('error', ev=>{
  clientError(ev.message||'script error',
              ev.error&&ev.error.stack||(ev.filename+':'+ev.lineno));
});
window.addEventListener('unhandledrejection', ev=>{
  const r=ev.reason;
  clientError('unhandled: '+((r&&r.message)||String(r)),
              r&&r.stack||'');
});

function renderTab(){
  const c=$('content');
  if(S.view!=='settings')c.classList.remove('set-host');
  if(S.view==='settings')return renderSettings(c);
  if(S.view==='code')return renderCode(c);
  if(S.view==='design')return renderDesign(c);
  if(S.view==='library')return renderLibrary(c);
  /* THE FRONT DOOR HAD NO RENDERER. This branch used to `return` and
     leave whatever was already in #content — which is fine on a fresh
     load, where the welcome is static HTML, and wrong the moment any
     view has overwritten it. Settings, Library, Code and Design all do.
     So "New project" cleared the project, cleared the view, re-rendered
     — and renderTab bailed out one line in, leaving Settings on screen.
     The button was never broken; there was simply nothing to draw. */
  if(!S.cur)return renderWelcome(c);
  const t=++RT;
  // TWO PANES THAT COEXIST — the fix for the real problem.
  //
  // Every surface used to replace every other one, so opening Preview
  // meant LOSING the conversation with no way back. That is a structural
  // fault, not a styling one: any layout that forces a trade between
  // "see the work" and "talk about the work" feels wrong however it is
  // painted. The conversation is now permanent on the left; the work
  // opens beside it on the right and can be closed without leaving.
  // TRUST THE DOM, NOT THE FLAG. This used to rebuild only when
  // dataset.split said so — but renderCode and renderLibrary overwrite
  // #content wholesale and left the flag set, so coming back to a
  // project found split==='1' with no #conv in the document. $('conv')
  // returned null, renderChatTab(null) threw, and EVERY line after it
  // was skipped — including the three body.classList toggles that
  // position the panes. That is why the interface came apart: not a
  // styling bug, an exception halfway through the renderer.
  if(c.dataset.split!=='1'||!document.getElementById('conv')){
    c.dataset.split='1';
    c.innerHTML=`<div class="conv" id="conv"></div>
                 <div class="work" id="work"><div class="wgrip" id="wgrip"></div><div class="workhd">
                   <span id="worktitle"></span>
                   <button class="iconbtn" title="close (Esc)"
                     onclick="closeWork()">${I('up',14)}</button></div>
                 <div class="workbody" id="workbody"></div></div>`;
  }
  const conv=$('conv'), work=$('workbody');

  // LAYOUT FIRST, AND UNCONDITIONALLY.
  //
  // These three classes are what position the panes. They used to sit
  // BELOW the render calls, so any exception above them — a missing
  // element, a bad panel, one undefined field — skipped all three and
  // left the page with its content half-drawn and no layout at all.
  // That is why "an error" and "the interface fell apart" kept being
  // the same event: the renderer abandoned the layout on the way past.
  //
  // Nothing here can throw, so the page can no longer lose its shape,
  // whatever fails afterwards.
  document.body.classList.toggle('split', !!S.panel);
  // WIDTH FOLLOWS WHAT IS IN THE PANE. A settings form is happy at 400px;
  // a website preview is not — squeezed to 38% with the edit dock open it
  // rendered one character per line. Editing is a focused act, so it takes
  // the whole work area and Done/Esc gives the conversation back.
  document.body.classList.toggle('wide', S.panel&&S.tab==='preview');
  document.body.classList.toggle('editing', !!(S.panel&&S.tab==='preview'&&S.editMode));

  // Each part renders inside its own failure boundary. A broken panel
  // is a broken PANEL — it says so, in place, and everything around it
  // keeps working. Previously it took the whole screen with it.
  guard('conversation', ()=>initGrip());
  guard('conversation', ()=>renderChatTab(conv), conv);
  if(!S.panel)return;
  const NAMES={plan:'Settings',strings:'Strings',images:'Images',
               links:'Links',preview:'Preview',logs:'Logs'};
  const wt=$('worktitle'); if(wt)wt.textContent=NAMES[S.tab]||'';
  if(S.tab==='plan')return guard('Settings',()=>renderPlan(work,t),work);
  if(S.tab==='strings')return guard('Strings',()=>renderStrings(work),work);
  if(S.tab==='images')return guard('Images',()=>renderImages(work),work);
  if(S.tab==='links')return guard('Links',()=>renderLinks(work),work);
  if(S.tab==='preview')return guard('Preview',()=>renderPreview(work,t),work);
  if(S.tab==='logs'){work.innerHTML=`<div id="logbox">${esc(S.log||'no output yet')}</div>`;
    $('logbox').scrollTop=1e9;return}
}

// ---------- Plan & AI ----------
// ONE KEY FOR EVERYTHING. The provider registry and the saved
// settings live on the SERVER (aethron_brain), so the same choice
// powers copy fill, plan polish, design matching, self-heal and the
// coding agent. The browser only renders it.
let AI={settings:{},providers:{},resolved:{}};
async function loadAi(){
  try{
    const r=await api('/api/ai/settings');
    if(r.available){AI={settings:r.settings,providers:r.providers,resolved:r.resolved};}
    // one-time migration from the old browser-only settings
    if(!AI.settings.api_key&&localStorage.forge_ai){
      try{const old=JSON.parse(localStorage.forge_ai);
        if(old.api_key){await saveAi(old);localStorage.removeItem('forge_ai');}
      }catch(e){}
    }
  }catch(e){}
  return AI;
}
async function saveAi(st){
  const r=await api('/api/ai/settings',{ai:st});
  AI.settings={...AI.settings,...st};
  /* The server just ended every open session, because each one is bound
     to the key it started with. Let go of the handle here too — holding
     a dead key is what made the next prompt hang instead of starting
     fresh on the new one. */
  if(CODE.key){
    CODE.key=''; CODE.since=0; CODE.events=[];
    if(window.endTurn)endTurn();
  }
  if(r&&r.ended&&window.note)
    note('key saved \u00b7 the open session ended, the next message starts a fresh one');
  return r;
}
function aiCfg(){return {}}   // server-side settings; no client override
async function renderPlan(c,t){
  const [{plan},cfg]=await Promise.all([api('/api/plan?project='+S.cur),
                                        api('/api/config?project='+S.cur)]);
  if(t!==RT)return;
  const a=aiCfg();
  c.innerHTML=`
  <!-- THE PLAN BLOCK IS GONE ON PURPOSE.
       A textarea that authors the brand, next to a chatbox that authors
       the brand, is two places the same fact can live — and nothing
       reconciles them when they disagree. "Polish rough plan with AI"
       and "Fill copy map with AI" were second, dumber routes to work the
       agent already does when you ask it. What is left here is real
       configuration: things with no conversational equivalent. -->
  <div class="insp">
   <h4>Project</h4>
   <div class="field">
    <label for="fwords">Forbidden words</label>
    <input id="fwords" value="${esc((cfg.forbidden_words||[]).join(', '))}">
    <span class="fh">the old brand — verify fails if any survive</span></div>
   <div class="field">
    <label for="hsel">Hide selectors</label>
    <input id="hsel" value="${esc((cfg.hide_selectors||[]).join(', '))}">
    <span class="fh">CSS for promos the defaults miss</span></div>
   <label class="check"><input type="checkbox" id="redmotion"
     ${cfg.reduce_motion?'checked':''}><span>Reduce all motion site-wide</span></label>
   <div class="frow"><button onclick="savePlan()">Save</button>
     <span class="hint" id="plansaved"></span></div>
  </div>

  <div class="insp"><h4>Model</h4>
  ${aiSettingsHtml()}
</div>`;
  bindAiSettings();
}

// The single AI settings block, rendered wherever it is needed (Plan
// tab, Code view). Editing it here changes the model behind copy fill,
// plan polish, design matching, self-heal AND the coding agent.
function aiSettingsHtml(compact){
  const a=AI.settings||{},P=AI.providers||{};
  const cur=a.provider||'deepseek',p=P[cur]||{};
  return `<div class="row">
   <div><label>Provider</label><select id="aiprov">
     ${Object.entries(P).map(([k,v])=>`<option value="${esc(k)}"
       ${cur===k?'selected':''}>${esc(v.label)}</option>`).join('')}
   </select></div>
   <div><label>Model</label><input id="aimodel"
     value="${esc(a.model||p.model||'')}" placeholder="${esc(p.model||'model name')}"></div>
   <div><label>API key</label><input id="aikey" type="password"
     value="" placeholder="${a.api_key?'•••••• (saved)':esc(p.keys||'')}"></div>
   <div><label>Base URL (optional)</label><input id="aibase"
     value="${esc(a.base_url||'')}" placeholder="${esc(p.base||'')}"></div>
  </div>
  <div class="toolbar"><button onclick="saveAiFromForm()">Save AI settings</button>
   <span class="hint" id="aisaved">${AI.resolved&&AI.resolved.ready
     ?'ready · '+esc(AI.resolved.label)+' · '+esc(AI.resolved.model||'')
     :esc((AI.resolved&&AI.resolved.why)||'')}</span></div>
  ${compact?'':`<div class="hint">this one setting powers copy fill, plan
   polish, design matching, self-heal and the coding agent. Providers
   that don't speak Anthropic's API are translated automatically, so a
   DeepSeek or Gemini key drives the code side too.</div>`}`;
}
function bindAiSettings(){
  const sel=$('aiprov');if(!sel)return;
  sel.onchange=e=>{const p=(AI.providers||{})[e.target.value]||{};
    $('aimodel').value=p.model||'';
    $('aimodel').placeholder=p.model||'model name';
    $('aikey').placeholder=p.keys||'';
    $('aibase').value='';$('aibase').placeholder=p.base||'';};
}
async function saveAiFromForm(){
  const st={provider:$('aiprov').value,model:$('aimodel').value.trim(),
            base_url:$('aibase').value.trim()};
  const k=$('aikey').value.trim();
  if(k)st.api_key=k;              // blank leaves the stored key alone
  try{
    await saveAi(st);await loadAi();
    if($('aisaved'))$('aisaved').textContent=AI.resolved.ready
      ?'saved ✓ · '+AI.resolved.label+' · '+(AI.resolved.model||'')
      :AI.resolved.why;
    if(S.view==='code')renderTab();
  }catch(e){if($('aisaved'))$('aisaved').textContent=e.message}
}
async function polishPlan(){
  $('plansaved').textContent='polishing…';
  try{
    const r=await api('/api/ai/plan',{project:S.cur,
      plan:($('plantxt')||{}).value});
    ($('plantxt')||{}).value=r.plan;
    $('plansaved').textContent='polished ✓ — review, tweak, then Save';
  }catch(e){$('plansaved').textContent=e.message}
}
async function savePlan(){
  // ONLY WRITE THE PLAN WHEN A PLAN FIELD EXISTS. With the textarea
  // removed this posted `undefined` and silently blanked the brand plan
  // the agent had written — a settings save destroying the work.
  const pt=$('plantxt');
  if(pt)await api('/api/plan',{project:S.cur,plan:pt.value});
  if($('fwords'))await api('/api/config',{project:S.cur,
    forbidden_words:$('fwords').value.split(','),
    hide_selectors:$('hsel').value.split(','),
    reduce_motion:$('redmotion')?$('redmotion').checked:false});
  $('plansaved').textContent='saved ✓';setTimeout(()=>$('plansaved').textContent='',1500);
  refresh(true);
}
async function aiFill(){
  await savePlan();
  try{const {job}=await api('/api/ai/fill',{project:S.cur});
    const ok=await watchJob(job,'ai');
    if(ok!==false){                    // apply the fills to the site now
      setProgress(66,'Applying your content to the site…');
      const b=await runStep('build');
      setProgress(100, b===false?'Filled, but build failed — see Logs'
        :'Filled & built — open Preview to see your site →',
        b===false?'fail':'done');
      hideProgress();
      if(b!==false){S.tab='preview';renderHeader();renderTab();}
    }
  }catch(e){alert(e.message)}
}
async function copyPrompt(){
  const {prompt}=await api('/api/ai/prompt?project='+S.cur);
  await navigator.clipboard.writeText(prompt);
  $('copied').textContent=`copied (${prompt.length.toLocaleString()} chars) ✓`;
}
async function mergePaste(){
  try{const r=await api('/api/ai/merge',{project:S.cur,text:$('pasteback').value});
    $('mergeres').textContent=`${r.applied} applied, ${r.errors.length} rejected`;
    if(r.errors.length){S.log+='\nREJECTED:\n'+r.errors.join('\n');}
    S.cm=await api('/api/copymap?project='+S.cur);refresh(true);
  }catch(e){$('mergeres').textContent=e.message}
}

// ---------- natural-language element editing ----------
function elementContext(el,r){
  const ctx={tag:el.tag,name:el.frname||null,classes:el.classes,
    old:r.old||null,current_text:r.new||r.old||null,
    max_bytes:r.max_bytes||null,selector:styleTargets(el)};
  try{
    const doc=$('editframe').contentDocument;
    const n=doc.querySelector(el.path);
    if(n){
      const cs=getComputedStyle(n);
      ctx.computed={color:cs.color,background:cs.backgroundColor,
        fontSize:cs.fontSize,fontWeight:cs.fontWeight,
        borderRadius:cs.borderRadius};
      // sample the page palette so the AI can harmonize colors
      const pal=new Set();
      let a=n;
      for(let i=0;i<6&&a;i++,a=a.parentElement){
        const c=getComputedStyle(a);
        [c.color,c.backgroundColor].forEach(x=>{
          if(x&&x!=='rgba(0, 0, 0, 0)')pal.add(x)});
      }
      [...doc.querySelectorAll('h1,h2,a,button')].slice(0,12).forEach(e2=>{
        const c=getComputedStyle(e2);
        [c.color,c.backgroundColor].forEach(x=>{
          if(x&&x!=='rgba(0, 0, 0, 0)')pal.add(x)});
      });
      ctx.page_palette=[...pal].slice(0,14);
    }
  }catch(e){}
  return ctx;
}
function wireAI(el,r){
  const go=$('aigo');if(!go)return;
  const run=async()=>{
    const instr=$('aiinstr').value.trim();
    if(!instr)return;
    const a=aiCfg();
    if(!a.api_key)return $('aiexplain').textContent=
      'set your model + API key in Plan & AI first';
    $('aiexplain').textContent='AI is thinking…';
    try{
      const res=await api('/api/ai/edit',{project:S.cur,instruction:instr,
        context:elementContext(el,r),...a});
      $('aiexplain').textContent=(res.explain||res.did.join(' · '));
      await rebuildAndReload($('epstatus'));
    }catch(e){$('aiexplain').textContent=e.message}
  };
  go.onclick=run;
  $('aiinstr').addEventListener('keydown',e=>{if(e.key==='Enter')run()});
}

// ---------- style editor (in the pick panel) ----------
const FREEZE={animation:'none',transition:'none',transform:'none',opacity:'1'};
function styleTargets(el){
  const scope=document.querySelector('input[name=stscope]:checked')?.value||'one';
  if(scope==='one')return el.path;
  if(el.frname)return `[data-framer-name="${el.frname}"]`;
  if(el.classes&&el.classes.length)
    return el.tag+el.classes.map(c=>'.'+CSS.escape(c)).join('');
  return el.path;
}
function collectCss(){
  const css={};
  if($('stcolor').dataset.touched)css['color']=$('stcolor').value;
  if($('stbg').dataset.touched)css['background-color']=$('stbg').value;
  if($('stfs').value)css['font-size']=$('stfs').value+'px';
  if($('stfw').value)css['font-weight']=$('stfw').value;
  if($('stfreeze').checked)Object.assign(css,FREEZE);
  for(const line of $('stcustom').value.split('\n')){
    const m=line.match(/^\s*([a-zA-Z-]+)\s*:\s*(.+?)\s*;?\s*$/);
    if(m)css[m[1]]=m[2];
  }
  return css;
}
function wireStylePanel(el){
  const live=()=>{
    const doc=$('editframe')?.contentDocument;if(!doc)return;
    const css=collectCss();
    try{doc.querySelectorAll(styleTargets(el)).forEach(n=>{
      LIVE.push({n,props:Object.keys(css)});
      for(const k in css)n.style.setProperty(k,css[k],'important');});}catch(e){}
  };
  for(const id of ['stcolor','stbg','stfs','stfw','stfreeze','stcustom']){
    const n=$(id);if(!n)continue;
    n.addEventListener('input',()=>{n.dataset.touched=1;live()});
    n.addEventListener('change',()=>{n.dataset.touched=1;live()});
  }
  $('stsave').onclick=async()=>{
    const css=collectCss();
    if(!Object.keys(css).length)
      return $('epstatus').textContent='no style changes to apply';
    try{
      $('epstatus').textContent='saving style…';
      await api('/api/style',{project:S.cur,selector:styleTargets(el),
        css,label:el.label||el.tag});
      await rebuildAndReload($('epstatus'));
    }catch(e){$('epstatus').textContent=e.message}
  };
}

// ---------- copy map editors ----------
function needCM(c){
  if(!S.cm){c.innerHTML=`<div class="empty">Run <b>Inventory</b> first —
   it extracts every editable string, image and link into copy_map.json.</div>`;
   return true}
  return false;
}
function budgetHTML(e){
  if(e.max_bytes==null)return '';
  const n=enc.encode(e.new||'').length;
  const cls=n>e.max_bytes?'over':(e.new?'fit':'');
  return `<div class="budget ${cls}">${n} / ${e.max_bytes} bytes (CMS)</div>`;
}
function bindInputs(sec){
  document.querySelectorAll(`input[data-sec="${sec}"]`).forEach(inp=>{
    inp.oninput=()=>{
      const e=S.cm[sec][+inp.dataset.i];
      e.new=inp.value;
      const b=inp.parentElement.querySelector('.budget');
      if(b)b.outerHTML=budgetHTML(e);
    };});
}
function saveBar(){
  return `<div class="toolbar">
  <button class="primary" onclick="saveCM(true)">Save & rebuild</button>
  <button onclick="saveCM(false)">Save only</button>
  <span class="hint">backticks/&#36;{ and byte budgets are enforced at
  build; changes appear after the rebuild</span>
  <span class="pill" id="savepill"></span></div>`;
}
async function saveCM(rebuild){
  const bad=S.cm.strings.filter(e=>e.max_bytes!=null&&enc.encode(e.new||'').length>e.max_bytes);
  if(bad.length&&!confirm(`${bad.length} string(s) exceed their CMS byte budget and will fail the build. Save anyway?`))return;
  await api('/api/copymap',{project:S.cur,copymap:S.cm});
  $('savepill').textContent='saved ✓';$('savepill').className='pill ok';
  if(rebuild){
    $('savepill').textContent='rebuilding…';
    const {job}=await api('/api/run',{project:S.cur,cmd:'build'});
    let j;do{await new Promise(r=>setTimeout(r,900));
      j=await api('/api/job?id='+job);}while(!j.done);
    S.log=j.log;
    $('savepill').textContent=j.ok?'rebuilt ✓':'build FAILED — see Logs';
    $('savepill').className='pill '+(j.ok?'ok':'err');
  }
  setTimeout(()=>{$('savepill').textContent=''; $('savepill').className='pill'},2500);
  refresh(true);
}
function renderStrings(c){
  if(needCM(c))return;
  c.innerHTML=`${saveBar()}
  <div class="toolbar"><input id="sfilter" placeholder="filter strings…" style="max-width:280px">
  <span class="hint">${S.cm.strings.filter(e=>e.new).length}/${S.cm.strings.length} filled ·
  amber = CMS byte-budgeted (must fit)</span></div>
  <table id="stbl"><tbody>${S.cm.strings.map((e,i)=>`
   <tr class="srow"><td class="old"><span class="orig">${esc(e.old)}</span><br>
    <span class="tags">${(e.where||[]).map(w=>`<span class="${w==='cms'?'cms':''}">${esc(w)}</span>`).join(' · ')}</span></td>
   <td><input data-sec="strings" data-i="${i}" value="${esc(e.new||'')}"
    placeholder="keep original">${budgetHTML(e)}</td></tr>`).join('')}
  </tbody></table>`;
  bindInputs('strings');
  $('sfilter').oninput=()=>{
    const f=$('sfilter').value.toLowerCase();
    document.querySelectorAll('#stbl .srow').forEach(r=>{
      r.style.display=r.textContent.toLowerCase().includes(f)?'':'none';});
  };
}
function renderImages(c){
  if(needCM(c))return;
  c.innerHTML=`
  <div class="card"><h3>Wordmark / logo generator — template's own font</h3>
  <div class="row">
   <div><label>Brand text</label><input id="lgtext" placeholder="Acme"></div>
   <div><label>Font (substring, run once to list)</label><input id="lgfont" placeholder="e.g. Outfit 700"></div>
   <div><label>Color</label><input id="lgcolor" value="#111111"></div>
  </div>
  <div class="toolbar"><button class="primary" onclick="genLogo()">Generate SVG wordmark</button>
  <span class="hint">writes assets/&lt;name&gt;-logo.svg — paste the printed
  /assets/… URL into the logo image below</span></div></div>
  ${saveBar()}
  <table><tbody>${S.cm.images.map((e,i)=>`
   <tr><td class="old"><img class="imgthumb" loading="lazy" src="${esc(e.old)}"
     onerror="this.style.display='none'"><br>${esc(e.old.split('/').pop())}</td>
   <td><input data-sec="images" data-i="${i}" value="${esc(e.new||'')}"
     placeholder="keep original — or /assets/…, or any URL">
    <div class="toolbar" style="margin-top:6px">
     <input type="file" data-up="${i}" accept="image/*,.svg" style="max-width:230px"></div>
   </td></tr>`).join('')}</tbody></table>`;
  bindInputs('images');
  document.querySelectorAll('input[type=file][data-up]').forEach(f=>{
    f.onchange=async()=>{
      const file=f.files[0];if(!file)return;
      const b64=await b64of(file);
      const r=await api('/api/assets',{project:S.cur,filename:file.name,data_b64:b64});
      const i=+f.dataset.up;S.cm.images[i].new=r.url;
      f.closest('td').querySelector('input[data-sec]').value=r.url;
    };});
}
async function genLogo(){
  if(!$('lgtext').value)return alert('enter the brand text');
  await runStep('logo',{text:$('lgtext').value,font:$('lgfont').value,
                        color:$('lgcolor').value});
  S.tab='images';renderHeader();renderTab();
}
function renderLinks(c){
  if(needCM(c))return;
  c.innerHTML=`${saveBar()}<table><tbody>${S.cm.links.map((e,i)=>`
   <tr><td class="old">${esc(e.old)}</td>
   <td><input data-sec="links" data-i="${i}" value="${esc(e.new||'')}"
    placeholder="keep original"></td></tr>`).join('')}</tbody></table>`;
  bindInputs('links');
}
async function renderPreview(c,t){
  c.innerHTML='<div class="empty">starting preview…</div>';
  if(S.editMode){
    c.innerHTML=`<div class="toolbar">
     <button class="primary" onclick="S.editMode=false;renderTab()">${I('check')}Done editing</button>
     <button id="pickToggle" onclick="togglePick()">${I('compass')}Browse</button>
     <button id="hoverToggle" onclick="toggleHover()"
      title="frozen: hover cards stay in rest state so you can edit the
front. sticky: hovering PINS a card's hover state so you can edit the
back (bio, socials).">${I('snow')}Hover frozen</button>
     <button onclick="try{$('editframe').contentWindow.history.back()}catch(e){}"
       title="back">←</button>
     <select id="editpage" style="max-width:200px"
      onchange="if(this.value!=='')$('editframe').src='/edit/${S.cur}/'+(this.value==='index.html'?'':this.value)">
      <option value="index.html">home</option></select>
     <span class="hint" id="edithint">EDIT MODE — click anything to change
      it. Browse switches to normal clicking (links navigate); the
      dropdown lists this site's pages.</span></div>
     <div id="editrow"><iframe id="editframe" src="/edit/${S.cur}/"
      onload="harvestRoutes();syncPickMode()"></iframe>
      <div id="editdock"></div></div>`;
    S.picking=true;
    return;
  }
  try{
    const {port}=await api('/api/preview',{project:S.cur});
    if(t!==undefined&&t!==RT)return;
    c.innerHTML=`<div class="toolbar">
     <button onclick="S.editMode=true;renderTab()">${I('pencil')}Edit mode</button>
     <span class="hint">live at <a href="http://127.0.0.1:${port}/" target="_blank"
      style="color:var(--acc2)">http://127.0.0.1:${port}/</a> — a real
      forge serve (range protocol + MIME), so what you see is what ships</span>
     <button onclick="renderPreview($('content'))">↻ reload</button></div>
     <iframe id="previewframe" src="http://127.0.0.1:${port}/"></iframe>`;
  }catch(e){c.innerHTML=`<div class="empty">${esc(e.message)}<br><br>
    Run <b>Build</b> first, then come back.</div>`}
}
async function openPreview(){S.tab='preview';renderHeader();renderTab();}

// ── edit-mode navigation: browse toggle + route discovery ───────────
function syncPickMode(){
  const f=$('editframe');
  if(f&&f.contentWindow)
    f.contentWindow.postMessage({forge:'mode',picking:S.picking!==false,
      hover:S.hoverMode||'freeze'},'*');
  const b=$('pickToggle');
  if(b)b.innerHTML=S.picking===false?I('pencil')+'Edit':I('compass')+'Browse';
  const ht=$('hoverToggle');
  if(ht)ht.innerHTML=(S.hoverMode||'freeze')==='freeze'
    ?I('snow')+'Hover frozen':I('pin')+'Hover sticky';
  const h=$('edithint');
  if(h)h.textContent=S.picking===false
    ?'BROWSE MODE — clicks navigate like a normal site. Hit Edit to pick elements again.'
    :'EDIT MODE — click anything to change it. Browse switches to normal clicking (links navigate); the dropdown lists this site\'s pages.';
}
function togglePick(){S.picking=S.picking===false?true:false;syncPickMode();}
function toggleHover(){
  S.hoverMode=(S.hoverMode||'freeze')==='freeze'?'sticky':'freeze';
  syncPickMode();
  if(S.hoverMode==='freeze')reloadFrames();  // unpin any stuck cards
}
function harvestRoutes(){
  try{
    const doc=$('editframe').contentDocument;
    const sel=$('editpage');
    if(!doc||!sel)return;
    // Framer = SPA: routes are extension-less (client-side). Webflow =
    // multi-page: routes are real .html FILES — keep the extension so
    // navigating loads the actual page instead of the SPA-fallback home.
    const framer=!(S.info&&S.info.platform==='webflow');
    const norm=r=>framer?r.replace(/\.html$/,''):(r&&!/\.html$/.test(r)?r+'.html':r);
    const set=new Set();
    (S.info&&S.info.pages||[]).forEach(p=>set.add(p==='index.html'?'':(framer?p.replace(/\.html$/,''):p)));
    [...doc.querySelectorAll('a[href]')].forEach(a=>{
      const h=a.getAttribute('href')||'';
      let r=null;
      if(h.startsWith('./'))r=h.slice(2);
      else if(h.startsWith('/')&&!h.startsWith('//'))r=h.slice(1);
      if(r===null)return;
      r=r.split('#')[0].split('?')[0];
      if(r.includes(':')||r.includes('//'))return;
      if(r.match(/\.(css|js|mjs|png|jpg|svg|ico|webp|zip|pdf)$/))return;
      // webflow = multi-page: only REAL pages belong in the dropdown
      // (a harvested /blog/post link with no local page would 404)
      if(!framer&&!(S.info&&S.info.pages||[]).includes(norm(r)))return;
      set.add(norm(r));
    });
    let cur=new URL($('editframe').contentWindow.location.href)
      .pathname.split('/').slice(3).join('/');
    if(framer)cur=cur.replace(/\.html$/,'');
    sel.innerHTML=[...set].sort().map(r=>{
      const val=r===''?'index.html':r;
      const home=(r===''||r==='index.html');
      return `<option value="${esc(val)}"
        ${val===cur||(home&&(cur===''||cur==='index.html'))?'selected':''}
        >${esc(home?'home':r.replace(/\.html$/,''))}</option>`;}).join('');
  }catch(e){}
}

// ── visual editor: picks arrive from the /edit iframe ──────────────
/* THE OLD PANEL AND THE NEW SHEET BOTH LISTEN FOR A PICK, so selecting
   one element opened two things: the new composer beside it AND the old
   property panel over the conversation. The sheet owns the pick while
   it is open; this listener is the fallback for the legacy preview tab,
   which still exists and still works. Two editors cannot share one
   event any more than two components can share one class name — the
   same fault as `split`, one layer down. */
window.addEventListener('message',async ev=>{
  const m=ev.data;
  if(!m||m.forge!=='pick'||!S.cur)return;
  if(document.getElementById('pvw'))return;
  try{
    if(m.kind==='container')
      return openEditPanel('container',
        {old:m.el.label||m.el.tag,huge:m.huge},m.el);
    if(m.kind==='images'&&m.srcs.length>1)return openImageChooser(m.srcs,m.el);
    const kind=m.kind==='images'?'image':m.kind;
    const src=m.srcs?m.srcs[0]:m.src;
    const r=await api('/api/entry/resolve',
      {project:S.cur,kind,text:m.text,texts:m.texts,src});
    openEditPanel(kind,r,m.el);
  }catch(e){alert(e.message)}
});
function openImageChooser(srcs,el){
  closePanel();
  const p=document.createElement('div');
  p.id='editpanel';
  p.innerHTML=`<h3>Several images are stacked here — which one?</h3>`
   +srcs.map((s,i)=>`<div class="toolbar" style="margin:6px 0">
     <img class="imgthumb" src="${esc(s)}" onerror="this.style.display='none'">
     <span class="hint grow" style="word-break:break-all">…${esc(s.slice(-42))}</span>
     <button data-pick="${i}">this one</button></div>`).join('')
   +`<div class="toolbar"><button onclick="closePanel()">Cancel</button></div>`;
  mountPanel(p);
  p.querySelectorAll('button[data-pick]').forEach(b=>b.onclick=async()=>{
    const r=await api('/api/entry/resolve',
      {project:S.cur,kind:'image',src:srcs[+b.dataset.pick]});
    openEditPanel('image',r,el);
  });
}
async function rebuildAndReload(statusEl,keepOpen){
  statusEl.textContent='rebuilding…';
  const {job}=await api('/api/run',{project:S.cur,cmd:'build'});
  let j;do{await new Promise(x=>setTimeout(x,900));
    j=await api('/api/job?id='+job);}while(!j.done);
  if(!j.ok){statusEl.textContent='build failed — see Logs';S.log=j.log;return false}
  if(!keepOpen)closePanel();
  reloadFrames();
  refresh(true);
  checkZeroEffect();
  return true;
}
async function assertTookEffect(old,statusEl){
  // BULLETPROOF RULE: a save must never silently no-op. If the report
  // says this entry replaced nothing (or hydration would revert it),
  // run the SELF-HEAL loop: deterministic fixer (flex upgrade, source
  // casing, nearest-source adoption) -> rebuild -> re-verify. One
  // bounded attempt, snapshotted (undo covers it), never a guess.
  try{
    let rep=await api('/api/report?project='+S.cur);
    const broken=o=>((o in rep)&&rep[o]===0&&!(rep.__moot__||[]).includes(o))
                    ||(rep.__at_risk__||[]).includes(o);
    if(!broken(old)){
      if(old in rep)statusEl.textContent=`✓ applied in ${rep[old]} place(s)`;
      return true;
    }
    statusEl.textContent='self-healing…';
    const h=await api('/api/heal',{project:S.cur,old});
    if(h.healed>0){
      const ok=await rebuildAndReload(statusEl,true);
      if(ok){
        rep=await api('/api/report?project='+S.cur);
        const key=h.new_old||old;
        if(!broken(key)){
          statusEl.innerHTML=`✓ <b>self-healed</b> — applied in `
            +`${rep[key]??'?'} place(s)`;
          return true;
        }
      }
    }
    const why=((h.log||'').match(/STUCK:.*$/m)||[])[0]||'';
    statusEl.innerHTML='<b style="color:var(--err)">This edit did not '
     +'take effect, and self-heal could not fix it safely.</b> '
     +(why?esc(why.replace(/^STUCK:\s*/,''))
          :'The text differs from the source — click a shorter/different '
           +'fragment, or tell the AI what you want instead.');
    return false;
  }catch(e){}
  return true;
}
let LIVE=[]; // nodes touched by live style preview — cleaned on Cancel
function clearLive(){
  LIVE.forEach(({n,props})=>{try{
    props.forEach(p=>n.style.removeProperty(p));}catch(e){}});
  LIVE=[];
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel()});
function hlTarget(sel){
  const doc=$('editframe')?.contentDocument;if(!doc)return;
  try{
    doc.querySelectorAll('.__forge-target').forEach(n=>n.classList.remove('__forge-target'));
    if(sel){const n=doc.querySelector(sel);
      if(n){n.classList.add('__forge-target');
        n.scrollIntoView({block:'nearest',behavior:'smooth'});}}
  }catch(e){}
}
function closePanel(){const p=$('editpanel');if(p)p.remove();
  const dk=$('editdock');if(dk)dk.classList.remove('on');
  hlTarget(null);clearLive();}
function mountPanel(p){
  const dk=$('editdock');
  if(dk){dk.appendChild(p);dk.classList.add('on')}
  else document.body.appendChild(p);
}
function openEditPanel(kind,r,el){
  closePanel();
  const p=document.createElement('div');
  p.id='editpanel';
  const budget=r.max_bytes!=null?
    `<div class="budget" id="epbudget"></div>`:'';
  const f=r.found;
  const lost=f&&!f.html&&!f.chunks&&!f.cms;
  const isC=kind==='container';
  const crumbs=(el&&el.ancestors&&el.ancestors.length)?
   `<div class="toolbar" style="flex-wrap:wrap;gap:4px;margin-top:6px">
     <span class="pill ok">${esc(el.tag)}${el.frname?' “'+esc(el.frname)+'”':''}</span>
     ${el.ancestors.map((a,i)=>`<button data-anc="${i}"
       style="font-size:11px;padding:3px 8px">${I('up',11)}${esc(a.tag)}${
       a.frname?' “'+esc(a.frname.slice(0,18))+'”':
       a.classes[0]?'.'+esc(a.classes[0].slice(0,16)):''}</button>`).join('')}
    </div><div class="hint">HOVER a chip to light up that element in
     the page (green outline) — click when it wraps exactly what you
     want to style</div>`:'';
  p.innerHTML=`<h3>${isC?'Style element':kind==='image'?'Replace image':'Edit text'}
    ${r.created?'<span class="pill">new entry</span>':''}</h3>
   ${lost?`<div class="hint" style="color:var(--err)">Heads-up: this exact text
    couldn't be located in the source — it's probably split into
    fragments. Try clicking a SHORTER piece of it.</div>`:''}
   ${f&&f.chunks&&!f.html?`<div class="hint" style="color:var(--ok)">
    Split-per-character text detected — handled automatically: the
    build rebuilds the character spans so the entrance animation is
    fully preserved.</div>`:''}
   ${r.huge?`<div class="hint" style="color:var(--acc2)">Careful: this element
    spans (almost) the whole page — a background here paints
    everything. Check the green outline before applying.</div>`:''}
   <div class="hint" style="word-break:break-word">${esc((r.old||'').slice(0,140))}</div>
   ${crumbs}
   ${isC?'':kind==='image'
     ?`<input id="epval" placeholder="/assets/… or any URL" value="${esc(r.new||'')}">
       <input type="file" id="epfile" accept="image/*,.svg" style="margin-top:8px">
       <div class="hint">replacing changes EVERY use of this image. Need a
        different image only in THIS spot (shared asset)? use custom
        CSS below: content: url(/assets/yourfile.svg) with "this element
        only"</div>`
     :r.rotator?`<div class="hint" style="color:var(--acc2)">⟳ rotating
       text — this element cycles through ALL of these. Edit any of them
       (empty = keep original):</div>
       ${r.rotator.map((ph,i)=>`<div style="margin-top:6px">
        <div class="hint" style="font-size:11px">${esc(ph.old)}</div>
        <input class="rotph" data-old="${esc(ph.old)}"
         value="${esc(ph.new||'')}" placeholder="keep original"></div>`).join('')}`
     :`<textarea id="epval" rows="3" placeholder="new text (empty = keep original)">${esc((r.new||'').trimEnd())}</textarea>`}
   ${budget}
   <div class="toolbar" style="margin-top:10px">
    ${isC?'':'<button class="primary" id="epsave">Save & rebuild</button>'}
    ${el?`<button id="epremove" style="border-color:var(--err);color:var(--err)">${I('trash')}Remove</button>`:''}
    <button onclick="closePanel()">Cancel</button>
    <span class="hint" id="epstatus"></span></div>
   ${el?`<div class="toolbar" style="margin-top:8px">
     <input id="aiinstr" class="grow" placeholder='tell the AI… e.g. "better color to match the page" or "punchier wording"'>
     <button id="aigo">Do it</button></div>
     <div class="hint" id="aiexplain"></div>`:''}
   ${el?`<details id="styledet" style="margin-top:8px"${isC?' open':''}>
    <summary>${I('palette')}Style & motion (baked into the code)</summary>
    <div class="row" style="margin-top:8px">
     <div><label>Text color</label><input type="color" id="stcolor" data-p="color"></div>
     <div><label>Background</label><input type="color" id="stbg" data-p="background-color"></div>
     <div><label>Font size px</label><input type="number" id="stfs" min="6" max="240" placeholder="–"></div>
     <div><label>Weight</label><select id="stfw"><option value="">–</option>
      <option>300</option><option>400</option><option>500</option>
      <option>600</option><option>700</option><option>900</option></select></div>
    </div>
    <label style="margin:6px 0"><input type="checkbox" id="stfreeze">
     freeze all motion on this element (kills entrance/hover animation)</label>
    <label>Custom CSS — one "property: value" per line</label>
    <textarea id="stcustom" rows="2" placeholder="letter-spacing: 2px&#10;border-radius: 12px"></textarea>
    <div class="toolbar" style="margin-top:6px">
     <label><input type="radio" name="stscope" value="one" checked> this element only</label>
     <label><input type="radio" name="stscope" value="all"> all matching</label>
     <button class="primary" id="stsave">Apply & rebuild</button>
    </div>
    <div class="hint">changes preview live; Apply writes them into the
     shipped code as !important rules (beats inline/runtime styles)</div>
   </details>`:''}`;
  mountPanel(p);
  const upd=()=>{const b=$('epbudget');if(!b||!$('epval'))return;
    const n=enc.encode($('epval').value).length;
    b.textContent=n+' / '+r.max_bytes+' bytes (CMS)';
    b.className='budget '+(n>r.max_bytes?'over':'fit');};
  if($('epval')){$('epval').oninput=upd;upd();}
  if($('epfile'))$('epfile').onchange=async()=>{
    const f=$('epfile').files[0];if(!f)return;
    const up=await api('/api/assets',{project:S.cur,filename:f.name,
      data_b64:await b64of(f)});
    $('epval').value=up.url;
  };
  if($('epsave'))$('epsave').onclick=async()=>{
    try{
      $('epstatus').textContent='saving…';
      const rots=[...p.querySelectorAll('input.rotph')];
      let checkOld=r.old;
      if(rots.length){                        // rotating text: save all
        for(const inp of rots){
          const old=inp.dataset.old, val=inp.value;
          if(val!==( (r.rotator.find(x=>x.old===old)||{}).new||'' )){
            await api('/api/entry/set',{project:S.cur,section:'strings',
              old,new:val});
            checkOld=old;
          }
        }
      }else{
        await api('/api/entry/set',{project:S.cur,section:r.section,
          old:r.old,new:$('epval').value});
      }
      const built=await rebuildAndReload($('epstatus'),true);
      if(built===false)return;
      const took=($('epval')&&!$('epval').value.trim()&&!rots.length)
        ||await assertTookEffect(checkOld,$('epstatus'));
      if(took)setTimeout(closePanel,900);
    }catch(e){$('epstatus').textContent=e.message}
  };
  if(el&&el.ancestors)p.querySelectorAll('button[data-anc]').forEach(b=>{
    const i=+b.dataset.anc,a=el.ancestors[i];
    b.onclick=()=>openEditPanel('container',{old:a.label||a.tag},
      {...a,ancestors:el.ancestors.slice(i+1)});
    b.onmouseenter=()=>hlTarget(a.path);
    b.onmouseleave=()=>hlTarget(el.path);
  });
  if(el){hlTarget(el.path);wireStylePanel(el);wireAI(el,r);}
  if(el&&$('epremove'))$('epremove').onclick=async()=>{
    if(!confirm(`Remove "${el.label||el.tag}" from the site entirely?`))return;
    try{
      $('epstatus').textContent='removing…';
      const res=await api('/api/entry/remove',{project:S.cur,el});
      $('epstatus').textContent=res.mode==='deleted'
        ?'deleted from the code…':'hidden permanently (Framer keeps DOM)…';
      await rebuildAndReload($('epstatus'));
    }catch(e){$('epstatus').textContent=e.message}
  };
}

// ---------- first-run walkthrough ----------
const TOUR=[
 {sel:'.newproj',t:'Bring a template',
  b:'Paste a live Framer or Webflow URL — every page is scraped automatically — or drop an export file, a zip, or all your separately saved pages at once.'},
 {sel:'#plist',t:'Your projects',
  b:'Each project tracks how much of the template\'s copy is already yours. Click one to open it; nothing you do here ever touches the pristine original.'},
 {sel:'#steps',t:'Prepare, then build',
  b:'Prepare runs fetch → inventory → build in order, automatically. After that: Build applies your edits to every layer, Verify runs machine checks, Undo reverts any change.'},
 {sel:'#tabs',t:'Fill it with AI — or by hand',
  b:'In Plan & AI, write a few rough words about your brand; the AI polishes a plan and fills every string within hard byte budgets — any model works, or paste fills manually with no API key. Strings, Images and Links give precise control.'},
 {sel:'#tabs',t:'Click-to-edit anything',
  b:'Preview → Edit mode: click any text, image, button or section on the live site to rewrite, restyle or remove it. Every change is baked into the shipped code — and edits that don\'t take effect self-heal automatically.'},
 {sel:'#steps',t:'Ship it anywhere',
  b:'site.zip is fully static — Cloudflare Pages, Netlify, Vercel, GitHub Pages, any host. Dev handoff exports the whole rebuildable project with a content API and an agent guide for AI IDEs.'}];
let tourEl=null;
function startTour(i){
  if(i==null||i<0||i>=TOUR.length){endTour();return}
  if(!tourEl){
    tourEl=document.createElement('div');tourEl.id='tour';
    tourEl.innerHTML='<div id="tourhole"></div><div class="tourbox"></div>';
    tourEl.onclick=e=>{if(e.target===tourEl)endTour()};
    document.body.appendChild(tourEl);
  }
  tourEl.className='on';
  const st=TOUR[i],el=document.querySelector(st.sel),
        hole=tourEl.querySelector('#tourhole'),
        box=tourEl.querySelector('.tourbox');
  let r=el&&el.getBoundingClientRect();
  if(!r||r.width<4||r.height<4)r=null;
  if(r){
    hole.style.display='block';
    hole.style.left=(r.left-7)+'px';hole.style.top=(r.top-7)+'px';
    hole.style.width=(r.width+14)+'px';hole.style.height=(r.height+14)+'px';
  }else hole.style.display='none';
  box.innerHTML=`<h4>${st.t}</h4><p>${st.b}</p>
   <div class="tnav"><span class="tstep">${i+1} / ${TOUR.length}</span>
   <button onclick="endTour()" style="border:none;background:none;color:var(--dim)">Skip</button>
   ${i?'<button onclick="startTour('+(i-1)+')">Back</button>':''}
   <button class="primary" onclick="${i<TOUR.length-1?'startTour('+(i+1)+')':'endTour()'}">
   ${i<TOUR.length-1?'Next':'Done'}</button></div>`;
  const bw=346,bh=box.offsetHeight||190,vw=innerWidth,vh=innerHeight;
  let x,y;
  if(r){
    x=Math.min(Math.max(r.left,16),vw-bw-16);
    y=r.bottom+18+bh<vh?r.bottom+16:r.top-bh-16;
    if(y<16)y=Math.max(16,(vh-bh)/2);
    if(r.right+bw+32<vw&&(r.bottom+18+bh>=vh)&&(r.top-bh-16<16))x=r.right+16,y=Math.max(16,Math.min(r.top,vh-bh-16));
  }else{x=(vw-bw)/2;y=(vh-bh)/2}
  box.style.left=x+'px';box.style.top=y+'px';
}
function endTour(){
  if(tourEl)tourEl.className='';
  localStorage.forge_tour='done';
}

refresh();
loadAi();          // the one AI setting, before anything asks for it
mountDots();       // the room is lit before anyone asks it to be
/* metal.js is deferred, so it may not be here yet. Mount now for the
   case where it is, and again on load for the case where it is not —
   and never wait on it, because every surface it touches already works. */
mountMetal();
addEventListener('load',()=>{mountMetal();playArrivals();});
watchMetal();
if(!localStorage.forge_tour)setTimeout(()=>startTour(0),700);
</script></body></html>
"""

# bake the inlined mark into every surface that shows the logo
LOGIN_HTML = LOGIN_HTML.replace("__MARK__", MARK)
# AN IMMUTABLE CACHE NEEDS A NEW URL, NOT A NEW FILE. /metal.js is sent
# with max-age=604800, immutable — which is right, and which means a
# browser that has it will never ask again. Measured the hard way: the
# first fixed build was served correctly and the page kept running the
# broken copy it already had, so the error on screen was about code the
# server no longer had. The fingerprint makes a changed engine a
# different URL.
METAL_VER = __import__("hashlib").sha256(METAL_JS.encode()).hexdigest()[:12]
INDEX_HTML = INDEX_HTML.replace("__METAL__", f"/metal.js?v={METAL_VER}")
INDEX_HTML = INDEX_HTML.replace("__MARK__", MARK)
CALLBACK_DONE_HTML = CALLBACK_DONE_HTML.replace("__MARK__", MARK)


# ───────────────────────── main ──────────────────────────────────────

if __name__ == "__main__":
    if not FROZEN and not FORGE.exists():
        print("ERROR: forge.py must sit next to studio.py")
        sys.exit(1)
    # local by default; PORT env (Render/Railway/Fly) binds 0.0.0.0 so
    # the platform's router can reach it. STUDIO_PASSWORD adds HTTP
    # Basic auth — REQUIRED before exposing an instance to the internet
    # (the studio is single-user by design: it writes to disk and runs
    # subprocesses on behalf of whoever can reach it).
    env_port = os.environ.get("PORT")
    port = int(sys.argv[1] if len(sys.argv) > 1 else (env_port or 8899))
    host = "0.0.0.0" if env_port else "127.0.0.1"
    PROJECTS.mkdir(parents=True, exist_ok=True)
    if os.environ.get("STUDIO_PASSWORD"):
        print("HTTP Basic auth: ON (user 'aethron')")
    elif env_port:
        print("WARNING: public bind without STUDIO_PASSWORD — anyone "
              "who finds the URL controls this instance.")
    print(f"Aethron Studio → http://{'127.0.0.1' if host != '0.0.0.0' else host}:{port}/")
    print(f"projects dir: {PROJECTS}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
