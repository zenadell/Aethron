#!/usr/bin/env python3
"""Template Forge Studio — a local AI IDE for template migrations.

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
PROJECTS = ROOT / "projects"

sys.path.insert(0, str(ROOT))
from forge import _flex_pat  # shared whitespace-tolerant matcher  # noqa: E402

JOBS = {}       # job id -> {"done": bool, "ok": bool|None, "log": str}
PREVIEWS = {}   # project -> (port, Popen)
RUN_CMDS = {"fetch", "inventory", "build", "verify", "localize"}


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
    p = subprocess.Popen([sys.executable, str(FORGE), "serve", str(port)],
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


# ───────────────────────── AI copy-fill ──────────────────────────────
# The model's only job, verbatim from PLAYBOOK Part 2 Step 4. Batched so
# small models (DeepSeek flash, 7B local) never blow their output limit.

PROMPT_RULES = """Rules:
1. If max_bytes is set, the UTF-8 byte length of "new" must be <= it.
   Em-dashes and curly quotes are 3 bytes each. When unsure, write shorter.
2. Never use backticks or ${ in any "new" value.
3. Keep the same tone-length-shape as the original (a 3-word button stays
   ~3 words; a one-line subtitle stays one line).
4. Leave "new" as "" for anything that should keep the original text.
5. Fill every brand-token entry (marked in "where").
6. Links: retarget emails, phone numbers and socials per the plan; leave
   internal anchors (#...) alone unless the plan says otherwise.
7. Images: leave "new" empty unless the plan supplies a replacement
   URL or path for that image.
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
var PICKING=true;
window.addEventListener('message',function(e){
  if(e.data&&e.data.forge==='mode')PICKING=!!e.data.picking;
});
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
  if(!PICKING){if(cur){cur.classList.remove('__forge-hl');cur=null}return}
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
  if(e.altKey||e.metaKey)return; // ⌥/⌘-click: follow links, navigate
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
    def log_message(self, *a):
        pass

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

    # ------------------------------------------------------------ GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path.startswith("/edit/"):
                return self.serve_edit(u, q)
            if u.path == "/":
                body = INDEX_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/projects":
                PROJECTS.mkdir(exist_ok=True)
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
            elif u.path == "/api/job":
                self.send_json(JOBS.get(q.get("id", [""])[0])
                               or {"error": "no such job"})
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
                    subprocess.run([sys.executable, str(FORGE), "backend"],
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
            if "." not in Path(rest).name:   # SPA route (about, works…)
                f = site / "index.html"
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
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.fail("bad JSON body")
        try:
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
                    argv = [sys.executable, str(FORGE), "logo",
                            body.get("text") or "Brand"]
                    for flag in ("font", "color", "tracking"):
                        if body.get(flag):
                            argv += ["--" + flag, str(body[flag])]
                elif cmd in RUN_CMDS:
                    argv = [sys.executable, str(FORGE), cmd]
                else:
                    return self.fail("command not allowed")
                self.send_json({"job": start_job(argv, d)})
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
                st = {k: body.get(k, "") for k in
                      ("provider", "base_url", "api_key", "model")}
                if not st["model"]:
                    return self.fail("set a model in the AI panel first")
                out = call_model(st, PLAN_POLISH_PROMPT + raw)
                out = re.sub(r"^```\w*\n?|```$", "", out.strip(), flags=re.M)
                (d / "project_plan.md").write_text(out, encoding="utf-8")
                self.send_json({"plan": out})
            elif u.path == "/api/ai/edit":
                # natural-language element editing: instruction + element
                # context in, guarded text/css changes out
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                st = {k: body.get(k, "") for k in
                      ("provider", "base_url", "api_key", "model")}
                if not st["model"]:
                    return self.fail("set a model + API key in Plan & AI first")
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
                st = {k: body.get(k, "") for k in
                      ("provider", "base_url", "api_key", "model")}
                if not st["model"]:
                    return self.fail("set a model name")
                self.send_json({"job": start_ai_job(name, st)})
            elif u.path == "/api/ai/merge":
                d = self.project_dir({"name": [body.get("project", "")]})
                snapshot(d)
                filled = extract_json(body.get("text", ""))
                applied, errors = merge_fill(d, filled)
                self.send_json({"applied": applied, "errors": errors})
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
                    # bake a permanent hide rule into every build instead
                    if el.get("id"):
                        sel = "#" + el["id"]
                    elif el.get("frname"):
                        sel = f'[data-framer-name="{el["frname"]}"]'
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
        except Exception as e:
            self.fail(e, 500)

    def api_create(self, body):
        raw = body.get("name", "")
        name = re.sub(r"[^\w-]+", "-", raw.strip().lower()).strip("-")
        if not name:
            return self.fail("give the project a name")
        if (PROJECTS / name).exists():
            return self.fail(f"project '{name}' already exists")
        PROJECTS.mkdir(exist_ok=True)
        # a LIVE URL: forge scrapes the home page + same-host routes
        url = (body.get("url") or "").strip()
        if url:
            if not url.startswith(("http://", "https://")):
                return self.fail("url must start with http(s)://")
            r = subprocess.run(
                [sys.executable, str(FORGE), "init", url, "--name", name],
                cwd=PROJECTS, capture_output=True, text=True)
            if r.returncode:
                return self.fail((r.stdout + r.stderr).strip() or "init failed")
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
                [sys.executable, str(FORGE), "init", str(target),
                 "--name", name],
                cwd=PROJECTS, capture_output=True, text=True)
        if r.returncode:
            return self.fail((r.stdout + r.stderr).strip() or "init failed")
        self.send_json({"name": name, "log": r.stdout})


# ───────────────────────── the app (single page) ─────────────────────

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Template Forge Studio</title>
<style>
:root{--bg:#0e1116;--panel:#151a22;--panel2:#1b212c;--line:#28303e;
--tx:#d6dce6;--dim:#8b95a6;--acc:#f59e0b;--acc2:#fbbf24;--ok:#34d399;
--err:#f87171;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box;margin:0}
body{display:flex;height:100vh;background:var(--bg);color:var(--tx);
font:14px/1.45 system-ui,-apple-system,sans-serif;overflow:hidden}
aside{width:250px;min-width:250px;background:var(--panel);
border-right:1px solid var(--line);display:flex;flex-direction:column}
.brand{padding:16px;font-weight:700;font-size:16px;
border-bottom:1px solid var(--line)}
.brand b{color:var(--acc)}.brand span{color:var(--dim);font-weight:400}
#plist{flex:1;overflow-y:auto;padding:8px}
.pitem{padding:9px 10px;border-radius:8px;cursor:pointer;display:flex;
justify-content:space-between;align-items:center;gap:6px}
.pitem:hover{background:var(--panel2)}
.pitem.sel{background:var(--panel2);outline:1px solid var(--line)}
.pitem .plat{font-size:10px;color:var(--dim);text-transform:uppercase}
.pitem .del{color:var(--dim);visibility:hidden;font-size:12px;padding:2px 5px}
.pitem:hover .del{visibility:visible}.pitem .del:hover{color:var(--err)}
.newproj{padding:12px;border-top:1px solid var(--line);display:flex;
flex-direction:column;gap:8px}
input,textarea,select{background:var(--bg);color:var(--tx);
border:1px solid var(--line);border-radius:7px;padding:7px 9px;
font:inherit;width:100%}
input:focus,textarea:focus{outline:1px solid var(--acc);border-color:var(--acc)}
button{background:var(--panel2);color:var(--tx);border:1px solid var(--line);
border-radius:7px;padding:7px 12px;font:inherit;cursor:pointer;
white-space:nowrap}
button:hover{border-color:var(--acc);color:var(--acc2)}
button.primary{background:var(--acc);border-color:var(--acc);color:#171207;
font-weight:600}
button.primary:hover{background:var(--acc2);color:#171207}
button:disabled{opacity:.4;pointer-events:none}
main{flex:1;display:flex;flex-direction:column;min-width:0}
header{display:flex;align-items:center;gap:14px;padding:12px 18px;
border-bottom:1px solid var(--line);background:var(--panel)}
#ptitle{font-weight:700;font-size:15px}
.steps{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
.step{display:flex;align-items:center;gap:6px;font-size:12.5px;
padding:6px 11px;border:1px solid var(--line);border-radius:99px;
cursor:pointer;color:var(--dim)}
.step:hover{border-color:var(--acc);color:var(--acc2)}
.step.done{color:var(--ok);border-color:#1d4a3c}
.step.run{color:var(--acc2);border-color:var(--acc)}
.step.run::before{content:"";width:9px;height:9px;border-radius:50%;
border:2px solid var(--acc);border-top-color:transparent;
animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(1turn)}}
.steps.busy .step{opacity:.5;pointer-events:none}
.steps.busy .step.run{opacity:1}
.stepwrap{align-self:center}
.stepwrap summary{cursor:pointer;color:var(--dim);font-size:12px;
list-style:none;padding:6px 4px}
.stepwrap summary:hover{color:var(--tx)}
.stepchips{display:flex;gap:6px;margin-top:6px}
#progress{display:none;align-items:center;gap:12px;padding:10px 18px;
background:var(--panel2);border-bottom:1px solid var(--line)}
#progress.on{display:flex}
#progress .bar{flex:1;height:6px;border-radius:99px;background:var(--bg);
overflow:hidden}
#progress .fill{height:100%;background:var(--acc);width:0;
transition:width .4s ease}
#progress .lbl{font-size:13px;color:var(--acc2);white-space:nowrap;
display:flex;align-items:center;gap:8px}
#progress .lbl::before{content:"";width:11px;height:11px;border-radius:50%;
border:2px solid var(--acc);border-top-color:transparent;
animation:sp .7s linear infinite}
#progress.fail .fill{background:var(--err)}
#progress.fail .lbl{color:var(--err)}
#progress.fail .lbl::before{animation:none;border:2px solid var(--err)}
#progress.done .fill{background:var(--ok)}
#progress.done .lbl{color:var(--ok)}
#progress.done .lbl::before{animation:none;border-color:var(--ok);
border-top-color:var(--ok)}
button.big{background:var(--acc);border-color:var(--acc);color:#171207;
font-weight:700;font-size:13.5px;padding:8px 16px}
button.big:hover{background:var(--acc2)}
nav{display:flex;gap:2px;padding:0 18px;background:var(--panel);
border-bottom:1px solid var(--line)}
nav .tab{padding:9px 14px;cursor:pointer;color:var(--dim);font-size:13.5px;
border-bottom:2px solid transparent}
nav .tab:hover{color:var(--tx)}
nav .tab.on{color:var(--acc2);border-bottom-color:var(--acc)}
#content{flex:1;overflow:auto;padding:18px}
.hint{color:var(--dim);font-size:13px;margin:6px 0 14px}
table{width:100%;border-collapse:collapse}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
td.old{width:44%;color:var(--dim);font-size:13px;word-break:break-word}
td.old .orig{color:var(--tx)}
.tags{font-size:10.5px;color:var(--dim)}
.tags .cms{color:var(--acc2)}
.budget{font:11px var(--mono);color:var(--dim);margin-top:3px}
.budget.over{color:var(--err);font-weight:700}
.budget.fit{color:var(--ok)}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:12px;
flex-wrap:wrap}
.toolbar .grow{flex:1}
#logbox,pre.code{background:#0a0d12;border:1px solid var(--line);
border-radius:8px;padding:12px;font:12.5px var(--mono);white-space:pre-wrap;
word-break:break-word;color:#b9c3d3;min-height:120px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:16px}
.card h3{font-size:13.5px;margin-bottom:10px;color:var(--acc2)}
.row{display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.row>*{flex:1;min-width:140px}
.imgthumb{max-height:44px;max-width:110px;border-radius:5px;
background:#fff2;display:block}
iframe{width:100%;height:calc(100vh - 190px);border:1px solid var(--line);
border-radius:10px;background:#fff}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;
border:1px solid var(--line);color:var(--dim)}
.pill.ok{color:var(--ok);border-color:#1d4a3c}
.pill.err{color:var(--err);border-color:#5a2a2a}
.empty{color:var(--dim);text-align:center;padding:60px 20px}
label{font-size:12px;color:var(--dim);display:block;margin-bottom:4px}
details summary{cursor:pointer;color:var(--dim);font-size:13px;margin:8px 0}
#editpanel{position:fixed;right:18px;bottom:18px;width:380px;z-index:99;
background:var(--panel);border:1px solid var(--acc);border-radius:12px;
padding:16px;box-shadow:0 12px 40px #000a}
#editpanel h3{font-size:13.5px;color:var(--acc2);margin-bottom:8px;
display:flex;gap:8px;align-items:center}
</style></head><body>
<aside>
  <div class="brand">⚒ <b>Template Forge</b> <span>Studio</span></div>
  <div id="plist"></div>
  <div class="newproj">
    <input id="npname" placeholder="new project name">
    <input id="npurl" placeholder="live template URL (scrapes all pages)">
    <input id="npfile" type="file" accept=".html,.htm,.zip" multiple>
    <button class="primary" onclick="createProject()">＋ Create project</button>
    <div class="hint" style="font-size:11px">paste the LIVE template URL
     (home + subpages scraped automatically) — or upload an export, a
     zip, or all your separately-saved pages at once</div>
  </div>
</aside>
<main>
  <header>
    <div id="ptitle">no project selected</div>
    <div class="steps" id="steps"></div>
  </header>
  <div id="progress"><div class="lbl" id="prog-lbl">working…</div>
    <div class="bar"><div class="fill" id="prog-fill"></div></div></div>
  <nav id="tabs"></nav>
  <section id="content"><div class="empty">Drop a Framer/Webflow export
  (.html or .zip) on the left to start a migration.<br><br>
  The pipeline: Fetch → Plan → Inventory → AI Fill → Build → Verify →
  Preview → Download.</div></section>
</main>
<script>
const $=id=>document.getElementById(id);
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
    if(!S.info){S.cur=null;S.cm=null;}
  }
  renderHeader();
  if(!keepTab)renderTab();
}
function renderSidebar(){
  $('plist').innerHTML=S.projects.map(p=>
    `<div class="pitem${p.name===S.cur?' sel':''}" onclick="select('${p.name}')">
     <div><div>${esc(p.name)}</div>
     <div class="plat">${p.platform} · ${p.filled}/${p.total} filled</div></div>
     <span class="del" onclick="event.stopPropagation();delProject('${p.name}')">✕</span>
     </div>`).join('')||'<div class="hint" style="padding:8px">no projects yet</div>';
}
async function select(name){
  S.cur=name;S.cm=null;S.tab='plan';
  await refresh();
  try{S.cm=await api('/api/copymap?project='+name);}catch(e){}
  renderTab();renderSidebar();
}
async function delProject(name){
  if(!confirm(`Delete project "${name}"? pristine/, copy_map and site/ all go.`))return;
  await api('/api/projects/delete',{name});
  if(S.cur===name){S.cur=null;S.cm=null;}
  refresh();
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
             ['build','3 Build'],['verify','4 Verify']];
function renderHeader(){
  $('ptitle').textContent=S.cur?S.cur+(S.info?` · ${S.info.platform.toUpperCase()}`:''):'no project selected';
  if(!S.cur){$('steps').innerHTML='';$('tabs').innerHTML='';return;}
  const done={fetch:S.info?.fetched,inventory:S.info?.inventoried,
              build:S.info?.built,verify:false};
  const ready=S.info?.built;   // prepped at least once
  const busy=S.running||S.autoRunning;
  $('steps').className='steps'+(busy?' busy':'');
  $('steps').innerHTML=
    (ready?'':`<button class="big" onclick="runAll()"
      title="fetch → inventory → build, in order, automatically">
      ⚡ ${busy?'Preparing…':'Prepare project'}</button>`)
   +`<details class="stepwrap"><summary>steps</summary><div class="stepchips">`
   +STEPS.map(([c,l])=>
    `<div class="step ${S.running===c?'run':done[c]?'done':''}"
      onclick="runStep('${c}')">${done[c]&&S.running!==c?'✓ ':''}${l}</div>`).join('')
   +(ready?`<div class="step" onclick="runAll()"
      title="re-fetch + re-inventory + build (fills are preserved)">
      ⚡ Re-run all</div>
     <div class="step" onclick="if(confirm('Download EVERY remote asset (css/js/images/fonts) into the project with brand-free names, then rebuild? The site stops depending on the template platform\\'s CDN entirely.'))runStep('localize').then(ok=>ok!==false&&runStep('build'))"
      title="full ownership: no more CDN dependency">
      🏠 Localize assets</div>`:'')
   +`</div></details>`
   +(ready?`<button class="big" onclick="runStep('build')"
      title="apply your edits to the site">🔨 Build</button>`:'')
   +(S.info&&S.info.undo?`<button onclick="doUndo()"
      title="revert the last change (copy map, styles, removals, config)"
      >↩ Undo (${S.info.undo})</button>`:'')
   +`<button onclick="openPreview()">▶ Preview</button>
     <button onclick="location='/api/download?project='+S.cur">⬇ site.zip</button>
     <button title="whole rebuildable project: pristine + copy map +
      forge.py + backend API + AGENT_GUIDE — hand this to any dev or AI IDE"
      onclick="location='/api/download?full=1&project='+S.cur">⬇ dev handoff</button>`;
  const tabs=[['plan','Plan & AI'],['strings','Strings'],['images','Images'],
              ['links','Links'],['preview','Preview'],['logs','Logs']];
  $('tabs').innerHTML=tabs.map(([t,l])=>
    `<div class="tab${S.tab===t?' on':''}" onclick="S.tab='${t}';renderHeader();renderTab()">${l}</div>`).join('');
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
  S.tab='plan';renderHeader();renderTab();
}
async function runStep(cmd,extra){
  if(S.running)return;
  if(S.autoRunning&&!['fetch','inventory','build','verify'].includes(cmd))return;
  try{
    const {job}=await api('/api/run',{project:S.cur,cmd,...(extra||{})});
    return await watchJob(job,cmd);
  }catch(e){alert(e.message);return false}
}
async function watchJob(job,label){
  S.running=label;S.tab='logs';renderHeader();renderTab();
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
  renderHeader();renderTab();
  return j.ok;
}

let RT=0; // render token: async renderers must not overwrite a newer tab
function renderTab(){
  const c=$('content');
  if(!S.cur){return}
  const t=++RT;
  if(S.tab==='plan')return renderPlan(c,t);
  if(S.tab==='strings')return renderStrings(c);
  if(S.tab==='images')return renderImages(c);
  if(S.tab==='links')return renderLinks(c);
  if(S.tab==='preview')return renderPreview(c,t);
  if(S.tab==='logs'){c.innerHTML=`<div id="logbox">${esc(S.log||'no output yet')}</div>`;
    $('logbox').scrollTop=1e9;return}
}

// ---------- Plan & AI ----------
const PRESETS={anthropic:{base:'https://api.anthropic.com',model:'claude-sonnet-5'},
  deepseek:{base:'https://api.deepseek.com',model:'deepseek-v4-pro'},
  gemini:{base:'https://generativelanguage.googleapis.com/v1beta/openai',model:'gemini-2.5-flash'},
  openai:{base:'https://api.openai.com/v1',model:'gpt-5'},
  ollama:{base:'http://localhost:11434/v1',model:'llama3.2'}};
function aiCfg(){try{return JSON.parse(localStorage.forge_ai||'{}')}catch(e){return{}}}
async function renderPlan(c,t){
  const [{plan},cfg]=await Promise.all([api('/api/plan?project='+S.cur),
                                        api('/api/config?project='+S.cur)]);
  if(t!==RT)return;
  const a=aiCfg();
  c.innerHTML=`
  <div class="card"><h3>Project plan — what this template becomes</h3>
  <div class="hint">Brand name, one-line description, voice/tone, contact
  email & phone, social links, image swaps. The AI reads ONLY this + the
  copy map. The more specific, the better every model performs.</div>
  <textarea id="plantxt" rows="10"
   placeholder="Brand: Acme Cleaning&#10;What it is: professional home cleaning in Austin&#10;Tone: warm, confident, no jargon&#10;Email: hello@acme.com   Phone: +1 512 …&#10;Instagram: https://instagram.com/acme …">${esc(plan)}</textarea>
  <div class="row" style="margin-top:10px">
   <div><label>Forbidden words — old brand; verify FAILS if any survive
    (auto-set by inventory, comma-separated)</label>
    <input id="fwords" value="${esc((cfg.forbidden_words||[]).join(', '))}"></div>
   <div><label>Extra hide selectors — CSS for promos/pills the defaults
    miss (comma-separated)</label>
    <input id="hsel" value="${esc((cfg.hide_selectors||[]).join(', '))}"></div>
  </div>
  <label style="margin:8px 0"><input type="checkbox" id="redmotion"
    ${cfg.reduce_motion?'checked':''}> reduce ALL motion site-wide
    (near-instant animations — baked into the build)</label>
  <div class="toolbar" style="margin-top:10px">
    <button class="primary" onclick="savePlan()">Save plan</button>
    <button onclick="polishPlan()">✨ Polish rough plan with AI</button>
    <span class="hint" id="plansaved"></span></div>
  <div class="hint">too lazy for the format? type a few rough words
   ("jomiez, ai agency, chill tone, insta @jomiez") and hit Polish —
   the AI rewrites it into the full plan for you to review.</div></div>

  <div class="card"><h3>Fill the copy map with AI — any model</h3>
  <div class="row">
   <div><label>Provider</label><select id="aiprov">
     ${Object.keys(PRESETS).map(p=>`<option ${a.provider===p?'selected':''}>${p}</option>`).join('')}
   </select></div>
   <div><label>Base URL</label><input id="aibase" value="${esc(a.base_url||PRESETS[a.provider||'anthropic'].base)}"></div>
   <div><label>Model</label><input id="aimodel" value="${esc(a.model||PRESETS[a.provider||'anthropic'].model)}"></div>
   <div><label>API key</label><input id="aikey" type="password" value="${esc(a.api_key||'')}"></div>
  </div>
  <div class="toolbar">
   <button class="primary" onclick="aiFill()">✦ Fill copy map with AI</button>
   <span class="hint">batched · byte budgets & forbidden chars enforced
   server-side · rejected lines shown in Logs</span></div>
  <details><summary>No API key? Manual mode — paste into any chat model</summary>
   <div class="toolbar"><button onclick="copyPrompt()">Copy prompt + JSON</button>
   <span class="hint" id="copied"></span></div>
   <textarea id="pasteback" rows="4" placeholder="paste the model's JSON answer here"></textarea>
   <div class="toolbar" style="margin-top:8px">
   <button onclick="mergePaste()">Merge pasted answer</button>
   <span class="hint" id="mergeres"></span></div>
  </details></div>`;
  $('aiprov').onchange=e=>{const p=PRESETS[e.target.value];
    $('aibase').value=p.base;$('aimodel').value=p.model;};
}
async function polishPlan(){
  const st={provider:$('aiprov').value,base_url:$('aibase').value,
            model:$('aimodel').value,api_key:$('aikey').value};
  localStorage.forge_ai=JSON.stringify(st);
  $('plansaved').textContent='polishing…';
  try{
    const r=await api('/api/ai/plan',{project:S.cur,
      plan:$('plantxt').value,...st});
    $('plantxt').value=r.plan;
    $('plansaved').textContent='polished ✓ — review, tweak, then Save';
  }catch(e){$('plansaved').textContent=e.message}
}
async function savePlan(){
  await api('/api/plan',{project:S.cur,plan:$('plantxt').value});
  if($('fwords'))await api('/api/config',{project:S.cur,
    forbidden_words:$('fwords').value.split(','),
    hide_selectors:$('hsel').value.split(','),
    reduce_motion:$('redmotion')?$('redmotion').checked:false});
  $('plansaved').textContent='saved ✓';setTimeout(()=>$('plansaved').textContent='',1500);
  refresh(true);
}
async function aiFill(){
  await savePlan();
  const st={provider:$('aiprov').value,base_url:$('aibase').value,
            model:$('aimodel').value,api_key:$('aikey').value};
  localStorage.forge_ai=JSON.stringify(st);
  try{const {job}=await api('/api/ai/fill',{project:S.cur,...st});
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

// ---------- 🤖 natural-language element editing ----------
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
    $('aiexplain').textContent='🤖 thinking…';
    try{
      const res=await api('/api/ai/edit',{project:S.cur,instruction:instr,
        context:elementContext(el,r),...a});
      $('aiexplain').textContent='🤖 '+(res.explain||res.did.join(' · '));
      await rebuildAndReload($('epstatus'));
    }catch(e){$('aiexplain').textContent=e.message}
  };
  go.onclick=run;
  $('aiinstr').addEventListener('keydown',e=>{if(e.key==='Enter')run()});
}

// ---------- style editor (🎨 in the pick panel) ----------
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
     <button class="primary" onclick="S.editMode=false;renderTab()">✔ done editing</button>
     <button id="pickToggle" onclick="togglePick()">🧭 Browse</button>
     <button onclick="try{$('editframe').contentWindow.history.back()}catch(e){}"
       title="back">←</button>
     <select id="editpage" style="max-width:200px"
      onchange="if(this.value!=='')$('editframe').src='/edit/${S.cur}/'+(this.value==='index.html'?'':this.value)">
      <option value="index.html">home</option></select>
     <span class="hint" id="edithint">EDIT MODE — click anything to change
      it. 🧭 Browse switches to normal clicking (links navigate); the
      dropdown lists this site's pages.</span></div>
     <iframe id="editframe" src="/edit/${S.cur}/"
      onload="harvestRoutes();syncPickMode()"></iframe>`;
    S.picking=true;
    return;
  }
  try{
    const {port}=await api('/api/preview',{project:S.cur});
    if(t!==undefined&&t!==RT)return;
    c.innerHTML=`<div class="toolbar">
     <button onclick="S.editMode=true;renderTab()">✏️ edit mode</button>
     <span class="hint">live at <a href="http://127.0.0.1:${port}/" target="_blank"
      style="color:var(--acc2)">http://127.0.0.1:${port}/</a> — a real
      forge serve (range protocol + MIME), so what you see is what ships</span>
     <button onclick="renderPreview($('content'))">↻ reload</button></div>
     <iframe src="http://127.0.0.1:${port}/"></iframe>`;
  }catch(e){c.innerHTML=`<div class="empty">${esc(e.message)}<br><br>
    Run <b>Build</b> first, then come back.</div>`}
}
async function openPreview(){S.tab='preview';renderHeader();renderTab();}

// ── edit-mode navigation: browse toggle + route discovery ───────────
function syncPickMode(){
  const f=$('editframe');
  if(f&&f.contentWindow)
    f.contentWindow.postMessage({forge:'mode',picking:S.picking!==false},'*');
  const b=$('pickToggle');
  if(b)b.textContent=S.picking===false?'✏️ Edit':'🧭 Browse';
  const h=$('edithint');
  if(h)h.textContent=S.picking===false
    ?'BROWSE MODE — clicks navigate like a normal site. Hit ✏️ Edit to pick elements again.'
    :'EDIT MODE — click anything to change it. 🧭 Browse switches to normal clicking (links navigate); the dropdown lists this site\'s pages.';
}
function togglePick(){S.picking=S.picking===false?true:false;syncPickMode();}
function harvestRoutes(){
  try{
    const doc=$('editframe').contentDocument;
    const sel=$('editpage');
    if(!doc||!sel)return;
    const set=new Set((S.info&&S.info.pages||[]).map(p=>p==='index.html'?'':p.replace(/\.html$/,'')));
    [...doc.querySelectorAll('a[href]')].forEach(a=>{
      const h=a.getAttribute('href')||'';
      let r=null;
      if(h.startsWith('./'))r=h.slice(2);
      else if(h.startsWith('/')&&!h.startsWith('//'))r=h.slice(1);
      if(r===null)return;
      r=r.split('#')[0].split('?')[0].replace(/\.html$/,'');
      if(r.includes(':')||r.includes('//'))return;
      if(r.match(/\.(css|js|mjs|png|jpg|svg|ico|webp|zip|pdf)$/))return;
      set.add(r);
    });
    const cur=new URL($('editframe').contentWindow.location.href)
      .pathname.split('/').slice(3).join('/').replace(/\.html$/,'');
    sel.innerHTML=[...set].sort().map(r=>
      `<option value="${esc(r===''?'index.html':r)}"
        ${r===cur?'selected':''}>${esc(r===''?'home':r)}</option>`).join('');
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
  document.body.appendChild(p);
  p.querySelectorAll('button[data-pick]').forEach(b=>b.onclick=async()=>{
    const r=await api('/api/entry/resolve',
      {project:S.cur,kind:'image',src:srcs[+b.dataset.pick]});
    openEditPanel('image',r,el);
  });
}
async function rebuildAndReload(statusEl){
  statusEl.textContent='rebuilding…';
  const {job}=await api('/api/run',{project:S.cur,cmd:'build'});
  let j;do{await new Promise(x=>setTimeout(x,900));
    j=await api('/api/job?id='+job);}while(!j.done);
  if(!j.ok){statusEl.textContent='build failed — see Logs';S.log=j.log;return false}
  closePanel();
  const f=$('editframe');if(f)f.src='/edit/'+S.cur+'/?r='+Date.now();
  refresh(true);
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
  hlTarget(null);clearLive();}
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
       style="font-size:11px;padding:3px 8px">⬆ ${esc(a.tag)}${
       a.frname?' “'+esc(a.frname.slice(0,18))+'”':
       a.classes[0]?'.'+esc(a.classes[0].slice(0,16)):''}</button>`).join('')}
    </div><div class="hint">HOVER a chip to light up that element in
     the page (green outline) — click when it wraps exactly what you
     want to style</div>`:'';
  p.innerHTML=`<h3>${isC?'Style element':kind==='image'?'Replace image':'Edit text'}
    ${r.created?'<span class="pill">new entry</span>':''}</h3>
   ${lost?`<div class="hint" style="color:var(--err)">⚠ this exact text
    couldn't be located in the source — it's probably split into
    fragments. Try clicking a SHORTER piece of it.</div>`:''}
   ${f&&f.chunks&&!f.html?`<div class="hint" style="color:var(--ok)">
    ✂ split-per-character text detected — handled automatically: the
    build rebuilds the character spans so the entrance animation is
    fully preserved.</div>`:''}
   ${r.huge?`<div class="hint" style="color:var(--acc2)">⚠ this element
    spans (almost) the whole page — a background here paints
    everything. Check the green outline before applying.</div>`:''}
   <div class="hint" style="word-break:break-word">${esc((r.old||'').slice(0,140))}</div>
   ${crumbs}
   ${isC?'':kind==='image'
     ?`<input id="epval" placeholder="/assets/… or any URL" value="${esc(r.new||'')}">
       <input type="file" id="epfile" accept="image/*,.svg" style="margin-top:8px">
       <div class="hint">replacing changes EVERY use of this image. Need a
        different image only in THIS spot (shared asset)? use 🎨 custom
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
    ${el?`<button id="epremove" style="border-color:var(--err);color:var(--err)">🗑 Remove</button>`:''}
    <button onclick="closePanel()">Cancel</button>
    <span class="hint" id="epstatus"></span></div>
   ${el?`<div class="toolbar" style="margin-top:8px">
     <input id="aiinstr" class="grow" placeholder='🤖 tell the AI… e.g. "better color to match the page" or "punchier wording"'>
     <button id="aigo">Do it</button></div>
     <div class="hint" id="aiexplain"></div>`:''}
   ${el?`<details id="styledet" style="margin-top:8px"${isC?' open':''}>
    <summary>🎨 Style & motion (baked into the code)</summary>
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
  document.body.appendChild(p);
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
      if(rots.length){                        // rotating text: save all
        for(const inp of rots){
          const old=inp.dataset.old, val=inp.value;
          if(val!==( (r.rotator.find(x=>x.old===old)||{}).new||'' ))
            await api('/api/entry/set',{project:S.cur,section:'strings',
              old,new:val});
        }
      }else{
        await api('/api/entry/set',{project:S.cur,section:r.section,
          old:r.old,new:$('epval').value});
      }
      await rebuildAndReload($('epstatus'));
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

refresh();
</script></body></html>
"""

# ───────────────────────── main ──────────────────────────────────────

if __name__ == "__main__":
    if not FORGE.exists():
        print("ERROR: forge.py must sit next to studio.py")
        sys.exit(1)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    PROJECTS.mkdir(exist_ok=True)
    print(f"Template Forge Studio → http://127.0.0.1:{port}/")
    print(f"projects dir: {PROJECTS}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
