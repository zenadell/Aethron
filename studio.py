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
st.textContent='.__forge-hl{outline:2px dashed #f59e0b !important;'+
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
  e.preventDefault();e.stopPropagation();
  if(p.kind==='images')
    parent.postMessage({forge:'pick',kind:'images',srcs:p.srcs,
      el:elInfoFull(p.el)},'*');
  else if(p.kind==='container')
    parent.postMessage({forge:'pick',kind:'container',huge:!!p.huge,
      el:elInfoFull(p.el)},'*');
  else
    parent.postMessage({forge:'pick',kind:'string',text:p.text,
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
                      "/api/auth/session"):
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
            if u.path == "/":
                body = INDEX_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/update/check":
                # Never blocks the UI on a network call it cannot
                # control: a failed check reports why and the app
                # carries on unchanged.
                self.send_json(updater.check() if updater else
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
                r = updater.update()
                if r.get("ok"):
                    # relaunch the REPLACED bundle, then let this
                    # process die so the new one owns the port
                    threading.Timer(0.8, lambda: (
                        updater.relaunch(Path(r["path"])),
                        os._exit(0))).start()
                return self.send_json(r)
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
                elif cmd in RUN_CMDS:
                    argv = forge_argv(cmd)
                else:
                    return self.fail("command not allowed")
                self._track("run_step", step=cmd)
                self.send_json({"job": start_job(argv, d)})
            elif u.path == "/api/ai/settings":
                if not brain:
                    return self.fail("AI settings unavailable")
                saved = brain.save(body.get("ai") or {})
                brain.shutdown_bridge()   # next session picks up the change
                self.send_json({"ok": True, "ai": {**saved, "api_key":
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
                        if not res.get("ok"):
                            append("\nWHAT IS STILL BROKEN:")
                            append(aethron_healer.evidence_text(
                                res.get("evidence") or {})[:2000])
                            append("\n(Undo reverts everything the agent "
                                   "did.)")
                        return res.get("ok")

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
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root{
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
font:14px/1.55 Inter,-apple-system,system-ui,sans-serif;overflow:hidden;
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
#plist{flex:1;overflow-y:auto;padding:10px}
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
.step{display:flex;align-items:center;gap:6px;font-size:12.5px;
padding:6.5px 13px;border:1px solid var(--line2);border-radius:99px;
cursor:pointer;color:var(--dim);font-weight:500;
transition:all .16s var(--ease)}
.step:hover{border-color:rgba(217,119,87,.55);color:var(--acc2);
transform:translateY(-1px)}
.step.done{color:var(--ok);border-color:rgba(74,222,128,.3);
background:rgba(74,222,128,.06)}
.step.run{color:var(--acc2);border-color:rgba(217,119,87,.55);
background:rgba(217,119,87,.10)}
.step.run::before{content:"";width:9px;height:9px;border-radius:50%;
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
.focal h1{font-size:32px;line-height:1.18;letter-spacing:-.02em;
  font-weight:600;color:var(--tx);margin:0 0 12px}
.focal .sub{font-size:13px;line-height:1.6;color:var(--dim);
  margin:0 0 32px;max-width:52ch}

.startrow{display:flex;gap:8px;align-items:center;margin-bottom:12px}
.startrow input{flex:1;height:44px;padding:0 14px;font-size:15px;
  background:var(--field);border:1px solid var(--line2);border-radius:10px;
  color:var(--tx)}
.startrow button.primary{width:44px;height:44px;padding:0;flex:none;
  border-radius:10px;display:grid;place-items:center}
.startrow button.primary [data-ic]{transform:rotate(90deg)}

.startmeta{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  font-size:12px;color:var(--lo);margin-bottom:40px}
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

@media (max-width:900px){.focal h1{font-size:24px}}

/* the conversation lives where the welcome was */
.clog{max-height:46vh;overflow:auto;margin:0 0 16px;display:flex;
  flex-direction:column;gap:10px}
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
.focal h1{font-size:40px;line-height:1.1;letter-spacing:-.035em;
  font-weight:600;margin:0 0 14px;
  background:linear-gradient(176deg,#fff 8%,#e6e2df 45%,#a8a29d 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  -webkit-text-fill-color:transparent}
.focal .sub{font-size:14px;line-height:1.65;color:#8b8792;
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
.focal h1{font-size:34px;line-height:1.15em;letter-spacing:-.035em;
  font-weight:590;color:var(--t-1);
  background:none;-webkit-text-fill-color:currentColor;margin:0 0 14px}
.focal h1 em{font-style:italic;color:var(--t-1)}
.focal .sub{font-size:15px;line-height:1.62em;letter-spacing:-.014em;
  color:var(--t-2);max-width:60ch;margin:0 0 var(--s-8,32px)}
#ptitle{font-size:20px;line-height:1.3em;letter-spacing:-.028em;font-weight:590}

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
.clog{max-height:52vh;gap:20px;max-width:780px;margin:0 auto 20px;width:100%}
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

.conv{display:flex;flex-direction:column;overflow:auto;padding:0 24px}
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
  <div id="plist"></div>
  <div class="srows">
    <button class="qrow" onclick="S.cur=null;S.cm=null;S.panel=false;{const _c=document.getElementById('content');if(_c)_c.dataset.split='';}refresh()">
      <span data-ic="plus"></span><span class="ql">New project</span>
      <span class="qc" data-ic="up"></span></button>
    <button class="qrow" id="codebtn" onclick="openCode()">
      <span data-ic="terminal"></span><span class="ql">Code workspace</span>
      <span class="qc" data-ic="up"></span></button>
    <button class="qrow" id="libbtn" onclick="openLibrary()">
      <span data-ic="library"></span><span class="ql">Design library</span>
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
  <section id="content"><div class="empty"><div class="focal">
     <div id="welcome">
     <h1>Every template you buy<br>can be entirely yours.</h1>
     <p class="sub">Paste a live Framer or Webflow URL and tell Aethron what it
      should become. It rebrands every string, swaps the images, strips the
      badge &mdash; and hands back a site you fully own.</p>
     </div>
     <div id="chatlog" class="clog" hidden></div>
     <div class="startrow">
       <textarea id="npurl" rows="1"
        placeholder="Paste a template URL, or tell Aethron what you want…"
        oninput="growTa(this)"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();consoleSend()}"></textarea>
       <button class="primary" id="sendbtn" onclick="consoleSend()"
        aria-label="Send"><span data-ic="up"></span></button>
     </div>
     <div class="startmeta">
       <input id="npname" placeholder="Project name (optional)">
       <span class="or">or</span>
       <label class="fpick"><input id="npfile" type="file"
        accept=".html,.htm,.zip" multiple><span data-ic="file"></span>
        choose an export, a zip, or saved pages</label>
     </div>
     <div class="qrows">
       <button class="qrow" onclick="openLibrary()">
         <span data-ic="library"></span><span class="ql">Match a plan against
         your design library</span><span class="qc" data-ic="up"></span></button>
       <button class="qrow" onclick="openCode()">
         <span data-ic="terminal"></span><span class="ql">Open the coding
         workspace</span><span class="qc" data-ic="up"></span></button>
       <button class="qrow" onclick="startTour(0)">
         <span data-ic="help"></span><span class="ql">Show me around</span>
         <span class="qc" data-ic="up"></span></button>
     </div></div></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const ICONS={
terminal:'<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>',
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
help:'<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>'};
const I=(n,s=14)=>`<svg class="ic" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[n]||''}</svg>`;
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
  S.projects=await api('/api/projects');
  renderSidebar();
  if(S.cur){
    S.info=S.projects.find(p=>p.name===S.cur)||null;
    if(!S.info){S.cur=null;S.cm=null;S.panel=false;{const _c=document.getElementById('content');if(_c)_c.dataset.split='';}}
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
     <span class="del" onclick="event.stopPropagation();delProject('${p.name}')">✕</span>
     </div>`}).join('')||'<div class="hint" style="padding:8px">no projects yet</div>';
}
async function select(name){
  S.cur=name;S.cm=null;S.tab='chat';S.view='project';
  const lb=$('libbtn');if(lb)lb.classList.remove('sel');
  await refresh();
  try{S.cm=await api('/api/copymap?project='+name);}catch(e){}
  renderTab();renderSidebar();
}
async function delProject(name){
  if(!confirm(`Delete project "${name}"? pristine/, copy_map and site/ all go.`))return;
  await api('/api/projects/delete',{name});
  if(S.cur===name){S.cur=null;S.cm=null;S.panel=false;{const _c=document.getElementById('content');if(_c)_c.dataset.split='';}}
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
  try{ UPD=await api('/api/update/check'); }catch(e){ return }
  if(UPD&&UPD.available)renderHeader();
}
async function applyUpdate(){
  const el=$('updrow'); if(el)el.textContent='Downloading…';
  try{
    const r=await api('/api/update/apply',{});
    if(!r.ok){ alert('Update failed: '+(r.why||'unknown')); return }
    document.body.innerHTML='<div class="empty"><div class="focal">'
      +'<h1>Updated to '+esc(r.version)+'</h1>'
      +'<p class="sub">Aethron is restarting.</p></div></div>';
  }catch(e){ alert('Update failed: '+e.message) }
}
function updateMenuItem(){
  if(!(UPD&&UPD.available))return '';
  return `<hr><button id="updrow" onclick="this.closest('details').open=false;applyUpdate()">
    ${I('download',14)}<span>Update to ${esc(UPD.latest||'')}</span></button>`;
}
function renderHeader(){
  $('ptitle').textContent=S.view==='library'?'Design library'
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
  ai:'Rewriting the copy…'};
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

async function openCode(){
  S.view='code';S.tab='';
  document.querySelectorAll('.libbtn').forEach(b=>b.classList.remove('sel'));
  const b=$('codebtn');if(b)b.classList.add('sel');
  if(S.cur)CODE.ws={project:S.cur,workspace:''};
  renderHeader();renderTab();
}
const wsq=()=>CODE.ws.project?'project='+encodeURIComponent(CODE.ws.project)
                             :'workspace='+encodeURIComponent(CODE.ws.workspace);
const wsBody=o=>Object.assign({},CODE.ws,o||{});
const wsName=()=>CODE.ws.project||CODE.ws.workspace||'';

async function renderCode(c){
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
async function sendCode(){
  const ta=$('chatta'),text=ta.value.trim();
  if(!text)return;
  if(!CODE.key)return alert('start a session first');
  ta.value='';
  CODE.events.push({type:'you',text});renderChat();
  try{await api('/api/code/send',{key:CODE.key,text});}
  catch(e){CODE.events.push({type:'error',text:e.message});renderChat();}
}
async function pollCode(){
  if(!CODE.key)return;
  try{
    const r=await api('/api/code/events?key='+encodeURIComponent(CODE.key)
                      +'&since='+CODE.since);
    if(r.events&&r.events.length){
      CODE.events.push(...r.events);CODE.since=r.n;renderChat();
      if(r.events.some(e=>['tool_result','done'].includes(e.type)))loadTree();
    }
    if(!r.running&&CODE.key){clearInterval(CODE.poll);CODE.poll=0;}
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
function renderChatTab(c){
  const n=(S.info&&S.info.strings)||0, f=(S.info&&S.info.filled)||0;
  c.innerHTML=`<div class="empty"><div class="focal">
    <div id="welcome" ${CODE.events.length?'hidden':''}>
      <h1>What should <em>${esc(S.cur)}</em> become?</h1>
      <p class="sub">Describe the brand and Aethron rebrands every string,
       fits the byte-locked slots, rebuilds and checks the result. Ask it to
       port the site to Astro, Next or Vue, swap a logo, or fix what a
       check flagged.${n?` &mdash; ${f} of ${n} strings filled so far.`:''}</p>
    </div>
    <div id="chatlog" class="clog" ${CODE.events.length?'':'hidden'}></div>
    <div class="startrow">
      <textarea id="npurl" rows="1"
       placeholder="Tell Aethron what to do with ${esc(S.cur)}…"
       oninput="growTa(this)"
       onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();consoleSend()}"></textarea>
      <button class="primary" onclick="consoleSend()" aria-label="Send">
        <span data-ic="up"></span></button>
    </div>
    <div class="qrows">
      <button class="qrow" onclick="quick('Rebrand this whole site. Ask me for the brand first if you do not have it.')">
        <span data-ic="wand"></span><span class="ql">Rebrand every string</span>
        <span class="qc" data-ic="up"></span></button>
      <button class="qrow" onclick="quick('Build, then verify, then probe. Report exactly what fails.')">
        <span data-ic="check"></span><span class="ql">Build and check it</span>
        <span class="qc" data-ic="up"></span></button>
      <button class="qrow" onclick="quick('Port this site to Astro and grade the port against the original.')">
        <span data-ic="package"></span><span class="ql">Port to another framework</span>
        <span class="qc" data-ic="up"></span></button>
    </div></div></div>`;
  renderChat();
}
function quick(t){const ta=$('npurl');if(!ta)return;ta.value=t;consoleSend()}
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
const toolWord=n=>TOOLWORDS[String(n||'').replace(/^mcp__aethron__/,'')]
  ||String(n||'').replace(/^mcp__aethron__/,'').replace(/_/g,' ');
function activityHtml(){
  if(!CODE.poll)return '';
  let last=null;
  for(const e of CODE.events) if(e.type==='tool')last=e;
  const done=CODE.events.filter(e=>e.type==='tool_result').length;
  return `<div class="activity"><span class="spin"></span>
    <span class="aw">${esc(last?toolWord(last.name):'Thinking')}</span>
    ${done?`<span class="ac">${done} step${done>1?'s':''} done</span>`:''}</div>`;
}
function renderChat(){
  const box=$('chatlog');if(!box)return;
  const rows=CODE.events.map(e=>{
    if(e.type==='you')return `<div class="msg you">${esc(e.text)}</div>`;
    if(e.type==='text')return `<div class="msg bot">${esc(e.text)}</div>`;
    if(e.type==='thinking')return `<div class="msg think">${esc(e.text)}</div>`;
    if(e.type==='tool')return `<div class="msg tool">${I('terminal',12)}
      <b>${esc(e.name)}</b> <code>${esc(JSON.stringify(e.input||{}).slice(0,160))}</code></div>`;
    if(e.type==='tool_result')return `<div class="msg res${e.ok?'':' bad'}">${
      esc((e.text||'').slice(0,300))}</div>`;
    if(e.type==='ready')return `<div class="msg sys">session ready · ${esc(e.model||'')}
      · ${(e.tools||[]).length} tools · mcp: ${esc((e.mcp||[]).join(', ')||'none')}</div>`;
    if(e.type==='done')return `<div class="msg sys">${e.error?'ERROR: '+esc(e.text)
      :'turn complete'}${e.cost_usd?' · $'+Number(e.cost_usd).toFixed(4):''}</div>`;
    if(e.type==='exit')return `<div class="msg sys">session ended (${e.code})</div>`;
    if(e.type==='error')return `<div class="msg res bad">${esc(e.text||'')}</div>`;
    return '';
  }).join('');
  box.innerHTML=rows+activityHtml();
  box.scrollTop=1e9;
}

let RT=0; // render token: async renderers must not overwrite a newer tab
function renderTab(){
  const c=$('content');
  if(S.view==='code')return renderCode(c);
  if(S.view==='library')return renderLibrary(c);
  if(!S.cur){return}
  const t=++RT;
  // TWO PANES THAT COEXIST — the fix for the real problem.
  //
  // Every surface used to replace every other one, so opening Preview
  // meant LOSING the conversation with no way back. That is a structural
  // fault, not a styling one: any layout that forces a trade between
  // "see the work" and "talk about the work" feels wrong however it is
  // painted. The conversation is now permanent on the left; the work
  // opens beside it on the right and can be closed without leaving.
  if(c.dataset.split!=='1'){
    c.dataset.split='1';
    c.innerHTML=`<div class="conv" id="conv"></div>
                 <div class="work" id="work"><div class="wgrip" id="wgrip"></div><div class="workhd">
                   <span id="worktitle"></span>
                   <button class="iconbtn" title="close (Esc)"
                     onclick="closeWork()">${I('up',14)}</button></div>
                 <div class="workbody" id="workbody"></div></div>`;
  }
  const conv=$('conv'), work=$('workbody');
  initGrip();
  renderChatTab(conv);
  document.body.classList.toggle('split', !!S.panel);
  // WIDTH FOLLOWS WHAT IS IN THE PANE. A settings form is happy at 400px;
  // a website preview is not — squeezed to 38% with the edit dock open it
  // rendered one character per line. Editing is a focused act, so it takes
  // the whole work area and Done/Esc gives the conversation back.
  document.body.classList.toggle('wide', S.panel&&S.tab==='preview');
  document.body.classList.toggle('editing', !!(S.panel&&S.tab==='preview'&&S.editMode));
  if(!S.panel)return;
  const NAMES={plan:'Settings',strings:'Strings',images:'Images',
               links:'Links',preview:'Preview',logs:'Logs'};
  $('worktitle').textContent=NAMES[S.tab]||'';
  if(S.tab==='plan')return renderPlan(work,t);
  if(S.tab==='strings')return renderStrings(work);
  if(S.tab==='images')return renderImages(work);
  if(S.tab==='links')return renderLinks(work);
  if(S.tab==='preview')return renderPreview(work,t);
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
window.addEventListener('message',async ev=>{
  const m=ev.data;
  if(!m||m.forge!=='pick'||!S.cur)return;
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
if(!localStorage.forge_tour)setTimeout(()=>startTour(0),700);
</script></body></html>
"""

# bake the inlined mark into every surface that shows the logo
LOGIN_HTML = LOGIN_HTML.replace("__MARK__", MARK)
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
