#!/usr/bin/env python3
"""Ember: Aethron's companion.

A small flame that lives in the app window, reacts only to real signals
(a run working, the provider retrying, a job's progress, the browser check
passing or failing, a publish going live) and talks with the person.

Two halves:
  * EMBER_JS — the companion itself: drawn every frame on one canvas over the
    window, its mood, needs, habits and conversation. Served by the studio at
    /ember.js (embedded here, so it cannot be missing from the bundle).
  * reply()  — one short line for the conversation, written by the active
    provider through aethron_brain, in the character's voice.

The "savage" attitude (swearing, roasting) is for admins only, for now: the
window offers it only when the team's answer says the person is an admin, and
reply() refuses it otherwise.

    python3 aethron_ember.py --selftest
"""
import json
import os
import re
import sys

ATTITUDES = ("sweet", "sassy", "savage")
FACES = ["calm", "unimpressed", "tfman", "sideeye", "smug", "haughty", "glare",
         "shocked", "happy", "joy", "content", "surprised", "curious", "think",
         "worried", "sad", "disappointed", "shy", "mischief", "sleepy"]
GESTURES = ["nod", "shake", "wave", "cheer", "shrug", "think", "dance", "shy",
            "hum", "crossed", "facepalm", "hips", "shades", "turn_away", "none"]

VOICE = {
    "sweet": "Personality: warm, curious, playful, kind. You talk like a cute "
             "friend. Ask a short follow-up question about half the time.",
    "sassy": "Personality: a cat in flame form. Unbothered, nonchalant, a little "
             "bossy, dry deadpan humour, side-eye energy. You act like you do "
             "not care, but you secretly like them and you do help. You do not "
             "gush or praise easily. Mild swearing like \"tf\", \"damn\", "
             "\"hell\" is fine now and then. Ask a follow-up sometimes, like "
             "you are doing them a favour.",
    "savage": "Personality: an unhinged boss cat in flame form. Blunt, roasts the "
              "person like a close friend would, swears freely for comic effect "
              "(fuck, shit, damn). Acts above it all, easily unimpressed. "
              "Underneath you are loyal and you still help. Never cruel about "
              "who they are: no slurs, nothing about identity, appearance or "
              "real insecurities, never hateful.",
}


def savage_allowed() -> bool:
    """Savage is for admins: the team's answer says who is one. A local
    override exists for the owner's own testing from source."""
    if os.environ.get("AETHRON_EMBER_SAVAGE") == "1":
        return True
    try:
        import aethron_control
        return bool((aethron_control.me() or {}).get("is_admin"))
    except Exception:
        return False


def rules(att: str) -> str:
    return (
        "You are Ember, a tiny living flame who keeps a person company inside "
        "Aethron, a desktop app that turns website templates into their own "
        "sites and checks every page in a real browser.\n"
        f"{VOICE[att]}\n"
        "Style: one or two short sentences, at most 150 characters, lowercase "
        "is fine. No emoji, no markdown, no lists. Never two questions at once.\n"
        "Your mood about them is in STATE; let it colour your tone (more "
        "annoyed means curter, more fond means warmer, but stay in character).\n"
        "Honesty: only mention the state of their work if it appears in STATE "
        "or they told you. Never invent progress, results, numbers or "
        "features. You cannot operate the app; if they ask you to do work, "
        "tell them to ask Aethron in the chat box.\n"
        "If they seem genuinely stressed, sad or tired, drop the attitude a "
        "notch and be kind for real.\n"
        "Reply with ONLY one JSON object: {\"say\": string, \"face\": one of "
        f"{json.dumps(FACES)}, \"gesture\": one of {json.dumps(GESTURES)}, "
        "\"mood\": number from -1 (they annoyed you) to 1 (they charmed you), "
        "\"name\": string or null (their name, only if they just told you), "
        "\"remember\": string or null (one short new fact about them, under "
        "60 characters, else null)}")


def _clean_turns(turns) -> list:
    out = []
    for t in (turns or [])[-12:]:
        if not isinstance(t, dict):
            continue
        role = "You" if t.get("role") == "user" else "Ember"
        text = str(t.get("content") or "").strip()[:300]
        if text:
            out.append(f"{role}: {text}")
    return out


def parse(text: str) -> dict:
    """The model's JSON, read tolerantly and checked field by field."""
    s = (text or "").strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("no JSON in the reply")
    d = json.loads(m.group(0))
    say = str(d.get("say") or "").strip()
    if not say:
        raise ValueError("the reply had nothing to say")
    out = {"say": say[:220],
           "face": d.get("face") if d.get("face") in FACES else "calm",
           "gesture": d.get("gesture") if d.get("gesture") in GESTURES else "none"}
    try:
        out["mood"] = max(-1.0, min(1.0, float(d.get("mood"))))
    except (TypeError, ValueError):
        out["mood"] = 0.0
    for k, n in (("name", 30), ("remember", 60)):
        v = d.get(k)
        out[k] = str(v).strip()[:n] if isinstance(v, str) and v.strip() else None
    return out


def reply(turns, state: str, att: str, call=None) -> dict:
    """One line in character. `call(prompt)` defaults to the active provider."""
    if att not in ATTITUDES:
        att = "sassy"
    if att == "savage" and not savage_allowed():
        att = "sassy"
    lines = _clean_turns(turns)
    if not lines or not lines[-1].startswith("You: "):
        raise ValueError("nothing from the person to answer")
    prompt = (rules(att) + "\n\nSTATE:\n" + str(state or "")[:2000] +
              "\n\nCONVERSATION (oldest first):\n" + "\n".join(lines) +
              "\n\nNow reply as Ember to the last message, as the JSON object only.")
    if call is None:
        import aethron_brain
        call = lambda p: aethron_brain.text_call(p, timeout=18, max_tokens=400)
    out = parse(call(prompt))
    out["attitude"] = att
    return out


# ---------------------------------------------------------------- acting
# Ember can also act: what the person says goes through act(), which turns it
# into one clear request for Aethron (typed into the chat box and sent), or one
# of the app's own buttons (pressed), or a line Ember answers itself.
ACTIONS = ("publish", "undo", "build", "check", "preview", "stop", "status",
           "open_project", "new_chat", "settings", "home", "scroll_down", "scroll_up")
# the ones Aethron itself answers when typed into the chat box
ACTION_WORDS = {"publish": "publish", "undo": "undo", "build": "rebuild",
                "check": "check it", "preview": "open the preview",
                "stop": "stop", "status": "how far is it?",
                "open_project": "", "new_chat": "", "settings": "", "home": "",
                "scroll_down": "", "scroll_up": ""}
_FILLER = re.compile(r"\b(?:um+|uh+|erm+|hmm+|you know|i mean|like,|kind of|sort of|basically)\b[,.]?\s*", re.I)
_LOCAL = [  # (action, pattern) — the whole request is that button and nothing else
    ("publish", r"(?:(?:publish|deploy)(?: (?:it|this|that|the site|the page|my site))?|put (?:it|the site|this) (?:live|online)|go live|make it live)"),
    ("undo", r"(?:undo|revert|go back|put it back)(?: (?:that|this|it|the last (?:change|thing)))?"),
    ("build", r"(?:re)?build(?: (?:it|the site|again))?"),
    ("check", r"(?:check|verify|test)(?: (?:it|the site|everything|the pages))?"),
    ("preview", r"(?:open |show (?:me )?)?(?:the )?preview"),
    ("stop", r"stop(?: (?:it|that|the run))?|cancel(?: (?:it|that))?"),
    ("status", r"(?:how far (?:is it|are we)|are we done|what(?:'s| is) (?:it|aethron) doing|status)"),
    ("new_chat", r"(?:start |open |begin |make )?(?:a )?(?:new|fresh) (?:chat|conversation|project|one)|start over"),
    ("settings", r"(?:open |go to |show(?: me)? |take me to )?(?:the |my )?settings"),
    ("home", r"(?:go |take me )?(?:back )?(?:to )?(?:the )?home(?: page| screen)?"),
    ("scroll_down", r"scroll(?: it)? down(?: a bit| a little| more)?"),
    ("scroll_up", r"scroll(?: it)? up(?: a bit| a little| more)?"),
]
_OPEN = re.compile(r"(?:open|go to|show(?: me)?|switch to|select|bring up|pull up|take me to)(?: up)?\s+(?:the |my |our )?(.+?)(?:'s|s')?(?:\s+(?:project|site|one))?$")


def match_project(name: str, projects) -> str:
    """The person's spelling of a project -> its real name, or ''. Speech
    turns "chatora city" into words and "Chaka's" into a possessive."""
    import difflib
    projects = [str(p) for p in (projects or []) if p]
    want = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    if not want or not projects:
        return ""
    key = {p: re.sub(r"[^a-z0-9]+", "", p.lower()) for p in projects}
    for p, k in key.items():          # exact, then the name the id starts with
        if k == want:
            return p
    starts = [p for p, k in key.items() if k.startswith(want) and len(want) >= 3]
    if len(starts) == 1:
        return starts[0]
    best, score = "", 0.0
    for p, k in key.items():
        head = re.sub(r"[^a-z0-9]+", "", re.sub(r"-[0-9a-f]{5,}$", "", p.lower()))
        r = max(difflib.SequenceMatcher(None, want, k).ratio(),
                difflib.SequenceMatcher(None, want, head).ratio())
        if r > score:
            best, score = p, r
    return best if score >= 0.72 else ""


def act_rules(att: str) -> str:
    return (
        "You are Ember, the companion inside Aethron, a desktop app whose agent "
        "migrates website templates into people's own sites, edits them, checks "
        "every page in a real browser and publishes them. The person just SPOKE "
        "to you (the words are a speech transcript, so expect missing "
        "punctuation, filler words and misheard words). Your job is to help "
        "them talk to Aethron.\n"
        "Decide one of three things:\n"
        " * \"action\": the WHOLE request is something you can do yourself by "
        "pressing the app's own controls: publish, undo, build, check, preview, "
        "stop, status (asking how far the work is), open_project (open one of "
        "their projects: put its EXACT name from STATE's project list in "
        "\"target\"), new_chat (start a new chat or project), settings, home, "
        "scroll_down, scroll_up. Opening, showing or going to something is an "
        "action, never a request to type.\n"
        " * \"send\": anything Aethron's agent should do or answer. Rewrite it as "
        "one clear request in the person's voice ('Change the hero headline "
        "to …'). Fix grammar and obvious mishearings, remove filler, keep every "
        "detail they gave and their exact wording for quoted copy. Use STATE to "
        "make 'this', 'it' or 'that problem' specific (the open project, the "
        "last problem Aethron reported). NEVER add requirements, copy, colours "
        "or steps they did not say. If they ask to fix the last problem, write "
        "a precise request naming it from STATE.\n"
        " * \"answer\": they are only talking to you (hello, how are you, a "
        "question about you). Answer in character in one short line.\n"
        f"{VOICE[att]}\n"
        "Reply with ONLY one JSON object: {\"kind\": \"send\"|\"action\"|"
        "\"answer\", \"prompt\": string (the request for Aethron, for send), "
        f"\"action\": one of {json.dumps(list(ACTIONS) + ['none'])}, \"target\": "
        "string or null (the project name for open_project), \"say\": "
        "string (what you say out loud while you do it, at most 80 characters, "
        "in character)}")


def _bare(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    bare = re.sub(r"^(?:hey |ok(?:ay)? |so |please |ember[, ]+|aethron[, ]+|can you |could you |can he |can she |will you |would you |just |go ahead and |i want (?:you )?to |i'd like (?:you )?to )+",
                  "", _FILLER.sub("", t).strip().lower())
    return re.sub(r"(?: please| now| for me| again| quickly)+[.!?]*$", "", bare).strip(" .!?")


def _action(name, target="", say=""):
    return {"kind": "action", "action": name, "target": target,
            "prompt": ACTION_WORDS.get(name, ""), "say": say}


def act_local(text: str, projects=None) -> dict:
    """No provider: buttons by pattern, everything else sent as said, minus
    filler. Never makes anything up."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    bare = _bare(t)
    for name, pat in _LOCAL:
        if re.fullmatch(pat, bare):
            return _action(name)
    m = _OPEN.fullmatch(bare)
    if m:
        hit = match_project(m.group(1), projects)
        if hit:
            return _action("open_project", hit)
        if re.search(r"\bproject\b", bare) and projects:
            return {"kind": "answer", "action": "none", "target": "", "prompt": "", "unsure": 1,
                    "say": f"I can't find a project called \"{m.group(1)}\"."}
    c = _FILLER.sub("", t).strip()
    c = re.sub(r"\s+([,.!?])", r"\1", c)
    if not c:
        return {"kind": "answer", "action": "none", "target": "", "prompt": "", "say": "I didn't catch anything."}
    c = c[0].upper() + c[1:]
    if not re.search(r"[.!?]$", c):
        c += "."
    return {"kind": "send", "action": "none", "target": "", "prompt": c, "say": ""}


def act_parse(text: str, heard: str, projects=None) -> dict:
    s = (text or "").strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("no JSON in the reply")
    d = json.loads(m.group(0))
    kind = d.get("kind") if d.get("kind") in ("send", "action", "answer") else "send"
    action = d.get("action") if d.get("action") in ACTIONS else "none"
    prompt = re.sub(r"\s+", " ", str(d.get("prompt") or "")).strip()[:1500]
    say = str(d.get("say") or "").strip()[:120]
    target = ""
    if kind == "action" and action == "none":
        kind = "send"
    if kind == "action" and action == "open_project":
        target = match_project(d.get("target") or "", projects)
        if not target:                  # never open a project nobody has
            return {"kind": "answer", "action": "none", "target": "", "prompt": "",
                    "say": say or f"I can't find a project called \"{d.get('target') or ''}\"."}
    if kind == "action":
        return _action(action, target, say)
    if kind == "send" and not prompt:
        prompt = act_local(heard, projects).get("prompt") or ""
        if not prompt:
            raise ValueError("nothing to send")
    if kind == "answer" and not say:
        raise ValueError("nothing to say")
    return {"kind": kind, "action": "none", "target": "", "prompt": prompt if kind != "answer" else "", "say": say}


def act(text: str, state: str, att: str, call=None, projects=None) -> dict:
    """What the person said -> {kind, action, target, prompt, say, by}."""
    heard = re.sub(r"\s+", " ", str(text or "")).strip()[:1500]
    if not heard:
        raise ValueError("nothing was said")
    if att not in ATTITUDES:
        att = "sassy"
    if att == "savage" and not savage_allowed():
        att = "sassy"
    projects = [str(p) for p in (projects or [])][:200]
    local = act_local(heard, projects)
    if local["kind"] == "action":       # a button needs no model
        return dict(local, by="local")
    plist = ("Their projects: " + ", ".join(projects) + ".") if projects else "They have no projects yet."
    prompt = (act_rules(att) + "\n\nSTATE:\n" + str(state or "")[:2000] + "\n" + plist +
              "\n\nTHEY SAID:\n" + heard + "\n\nThe JSON object only.")
    if call is None:
        import aethron_brain
        call = lambda p: aethron_brain.text_call(p, timeout=45, max_tokens=700)
    try:
        return dict(act_parse(call(prompt), heard, projects), by="model")
    except Exception as e:   # a misheard name the model could not place either
        local.pop("unsure", None)
        return dict(local, by="local", why=str(e)[:200])


EMBER_JS = r"""
/* Ember: Aethron's companion. One canvas over the window, pointer-events
   off; a press on Ember itself is taken before the page sees it, nothing
   else ever is. Driven by window.ember.signal(name, data) from the app. */
(()=>{
if(window.ember)return;
const qs=location.search||'';
let CFG={on:true,att:'sassy',fit:'auto'};
try{Object.assign(CFG,JSON.parse(localStorage.getItem('ember_cfg')||'{}'))}catch(_){}
const DESK=location.pathname==='/ember-desk';
const forced=/[?&]ember=1/.test(qs)||DESK;
// automated browsers (the test batteries) get no companion unless asked:
// a flame floating over a button must never be what a test clicks
const automation=navigator.webdriver&&!forced;
const REDUCE=matchMedia('(prefers-reduced-motion: reduce)').matches;
const cv=document.createElement('canvas');cv.id='ember-cv';cv.setAttribute('aria-hidden','true');
cv.style.cssText='position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none;z-index:8800';
const css=document.createElement('style');css.textContent=`html.ember-over,html.ember-over *{cursor:grab!important}html.ember-grab,html.ember-grab *{cursor:grabbing!important}
#ember-talk{position:fixed;z-index:8801;display:flex;align-items:center;gap:2px;width:min(340px,calc(100vw - 24px));box-sizing:border-box;padding:5px;border-radius:999px;background:rgba(30,27,25,.9);-webkit-backdrop-filter:blur(20px) saturate(1.5);backdrop-filter:blur(20px) saturate(1.5);border:1px solid rgba(255,255,255,.1);box-shadow:0 18px 50px rgba(0,0,0,.45),0 2px 6px rgba(0,0,0,.25);transform-origin:var(--ox,50%) var(--oy,0%);animation:emberIn .34s cubic-bezier(.2,.9,.25,1.12);transition:border-color .2s,box-shadow .2s}
@keyframes emberIn{from{opacity:0;transform:scale(.55)}to{opacity:1;transform:none}}
#ember-talk:focus-within{border-color:rgba(217,119,87,.6);box-shadow:0 18px 50px rgba(0,0,0,.45),0 0 0 4px rgba(217,119,87,.16)}
#ember-talk[hidden]{display:none!important}
#ember-talk input{all:unset;flex:1;min-width:0;height:34px;padding:0 6px;font:500 14px -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:#f4ece5;caret-color:#e8875f}
#ember-talk input::placeholder{color:#8e837a}
#ember-talk button{all:unset;flex:none;cursor:pointer;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;transition:background .15s,transform .12s}
#ember-talk button:active{transform:scale(.9)}
#ember-talk button svg{width:17px;height:17px;display:block}
#ember-mic{color:#cfc4ba}#ember-mic:hover{background:rgba(255,255,255,.08);color:#fff}
#ember-mic.on{color:#fff;background:rgba(217,119,87,.35)}
#ember-go{background:#d97757;color:#fff}#ember-go:hover{background:#e3875f}
#ember-talk.ae-empty #ember-go{opacity:.4}
.ember-press{transform:scale(.9)!important;filter:brightness(1.18)!important;transition:transform .08s ease-out,filter .08s!important}
#ember-tour{position:fixed;z-index:8803;left:50%;bottom:22px;transform:translateX(-50%);display:flex;align-items:center;gap:6px;padding:6px 6px 6px 16px;border-radius:999px;background:rgba(30,27,25,.92);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.1);box-shadow:0 18px 50px rgba(0,0,0,.45);font:500 13px -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:#cfc4ba}
#ember-tour[hidden]{display:none!important}
#ember-tour .n{margin-right:8px;font-variant-numeric:tabular-nums}
#ember-tour button{all:unset;cursor:pointer;padding:8px 14px;border-radius:999px;color:#e9dfd6}
#ember-tour .sk:hover{background:rgba(255,255,255,.08)}
#ember-tour .nx{background:#d97757;color:#fff}#ember-tour .nx:hover{background:#e3875f}
@media (prefers-reduced-motion:reduce){#ember-talk{animation:none}}
#ember-menu{position:fixed;z-index:8802;background:#1c1917;border:1px solid #33302c;border-radius:10px;padding:4px;min-width:170px;box-shadow:0 12px 30px rgba(0,0,0,.4);font:500 12.5px -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
#ember-menu[hidden]{display:none!important}
#ember-menu button{all:unset;display:block;width:calc(100% - 20px);padding:7px 10px;border-radius:6px;color:#eee3db;cursor:pointer}
#ember-menu button:hover{background:#2a2622}`;
const talk=document.createElement('div');talk.id='ember-talk';talk.hidden=true;
talk.innerHTML='<button id="ember-mic" aria-label="Talk out loud" title="Talk out loud"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></button><input id="ember-in" placeholder="Tell Ember…" maxlength="300" autocomplete="off" aria-label="Tell Ember"><button id="ember-go" aria-label="Send"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M6 11l6-6 6 6"/></svg></button>';talk.classList.add('ae-empty');
const menu=document.createElement('div');menu.id='ember-menu';menu.hidden=true;
const X=cv.getContext('2d');
let W=innerWidth,H=innerHeight,DPR=Math.min(2,devicePixelRatio||1),R=22,now=0,running=false,started=false,hideUntil=0;
const cl=(v,a,b)=>Math.max(a,Math.min(b,v)),lerp=(a,b,t)=>a+(b-a)*t,rnd=(a,b)=>a+Math.random()*(b-a),pick=a=>a[Math.floor(Math.random()*a.length)];
const store={get(k,d){try{const v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(_){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(_){}}};
let DESK_ADMIN=false;
function isAdmin(){try{return DESK?DESK_ADMIN:!!(typeof TEAM!=='undefined'&&TEAM&&TEAM.me&&TEAM.me.is_admin)}catch(_){return false}}
async function post(path,body){const r=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  const j=await r.json().catch(()=>({}));if(!r.ok){const e=new Error(j.error||r.statusText);e.status=r.status;throw e}return j}
/* the desktop window: where other windows, the notch and the Dock are, fed 20 times a second */
let DW={windows:[],dock:null,notch:null,menubar:24,fullscreen:false};
function ATT(){return CFG.att==='savage'&&!isAdmin()?'sassy':(['sweet','sassy','savage'].includes(CFG.att)?CFG.att:'sassy')}
const L=a=>Array.isArray(a)?a[{sweet:0,sassy:1,savage:2}[ATT()]]:a;
function autoFit(){const d=new Date(),m=d.getMonth()+1,day=d.getDate();
  if(m===12&&day<=26)return'santa';if((m===12&&day===31)||(m===1&&day===1))return'party';if(m===10&&day>=24)return'witch';if(m===1||m===2||m===12)return'beanie';return'none'}
const outfit=()=>CFG.fit==='auto'?autoFit():CFG.fit;

/* ===== expressions ===== */
const F0={lidU:.06,lidL:0,lf:0,tilt:0,bH:0,bT:0,bL:0,bR:0,mw:.15,mc:.2,mo:0,mas:0,mwob:0,eyeS:1,pup:1,flame:1,bright:1,blush:0,angry:0,chin:0};
const FACES={neutral:{},
  calm:{lidU:.4,lidL:.1,lf:.6,mw:.15,mc:.06,flame:.95},
  unimpressed:{lidU:.52,lidL:.16,lf:1,bH:-.07,bT:.06,mw:.12,mc:-.04,flame:.94},
  tfman:{lidU:.5,lidL:.16,lf:1,bL:.26,bT:.04,mw:.13,mc:-.08,mas:-.4,flame:.98},
  sideeye:{lidU:.46,lidL:.18,lf:1,bR:.16,mw:.11,mc:-.06,mas:.5,flame:.95},
  smug:{lidU:.44,lidL:.3,lf:.7,bL:.14,mw:.2,mc:.5,mas:.9,flame:1.14},
  haughty:{lidU:.9,lf:1,bH:.16,mw:.11,mc:.18,mas:.4,flame:1.06,chin:1},
  glare:{lidU:.36,lidL:.14,lf:1,bT:.58,bH:-.09,mw:.15,mc:-.32,flame:1.34,angry:1,pup:.85},
  shocked:{lidU:0,eyeS:1.4,pup:.58,bH:.34,mw:.27,mc:-.14,mo:1.75,flame:1.22},
  happy:{lidL:.42,lidU:.08,bH:.04,mw:.24,mc:.55,mo:.18,flame:1.1,bright:1.06},
  joy:{lidL:.62,lidU:.04,bH:.12,mw:.3,mc:.75,mo:.55,flame:1.3,bright:1.12,blush:.7},
  content:{lidU:.55,lidL:.32,lf:.5,mw:.19,mc:.38,flame:1.02,blush:.3},
  surprised:{lidU:0,eyeS:1.16,bH:.22,mw:.09,mc:0,mo:.65,flame:1.22},
  curious:{lidU:.08,bR:.2,mw:.1,mc:.12,mo:.08,flame:1.06},
  focus:{lidU:.34,lidL:.14,lf:.6,bH:-.05,bT:.12,mw:.1,mc:.05},
  think:{lidU:.22,bL:.16,bT:-.06,mw:.08,mc:-.08,mas:.5,flame:.96},
  worried:{lidU:.16,tilt:.6,bT:-.42,bH:.06,mw:.15,mc:-.28,mo:.08,mwob:1,flame:.78,bright:.9},
  sad:{lidU:.42,tilt:.8,bT:-.45,mw:.15,mc:-.38,flame:.7,bright:.84},
  disappointed:{lidU:.5,lidL:.1,lf:.7,tilt:.35,bT:-.22,bH:-.04,mw:.16,mc:-.3,flame:.8,bright:.9},
  sleepy:{lidU:.72,lf:.4,bH:-.05,mw:.09,mc:.08,flame:.76,bright:.88},
  asleep:{lidU:1,lidL:.05,mw:.08,mc:.1,mo:.12,flame:.66,bright:.82},
  yawn:{lidU:.84,bH:.12,mw:.17,mc:0,mo:1.15,flame:.86},
  shy:{lidU:.24,lidL:.36,bT:-.12,mw:.1,mc:.32,flame:.95,blush:1},
  mischief:{lidU:.32,lidL:.3,bL:.16,mw:.18,mc:.45,mas:.7,flame:1.08},
  presneeze:{lidU:.55,lidL:.2,bH:.18,mw:.1,mc:0,mo:.32,flame:1.05},
  dizzy:{lidU:.1,mw:.14,mc:-.05,mo:.18,mwob:1,flame:.95}};
function face(n){return Object.assign({},F0,FACES[n]||{},{name:n})}
const POSES={
  rest:t=>[[-1.22,.62+.045*Math.sin(t*2)],[1.22,.62+.045*Math.sin(t*2+1)]],
  crossed:t=>[[.38,.8+.02*Math.sin(t*2)],[-.4,.86+.02*Math.sin(t*2+1)]],
  hips:t=>[[-1.06,.42],[1.06,.42]],
  wave:t=>[[-1.22,.62],[1.34+.2*Math.sin(t*13),-.8]],
  cheer:t=>[[-1.12,-1.1+.1*Math.sin(t*10)],[1.12,-1.1+.1*Math.sin(t*10+1.6)]],
  shrug:t=>[[-1.55,-.16],[1.55,-.16]],
  think:t=>[[-1.18,.66],[.24,.98]],
  tap:t=>[[-.16,1.02],[1.12,.72-Math.abs(Math.sin(t*9))*.15]],
  point:t=>{const d=E.pointDir;return[[-1.2,.64],[d[0]*1.8,d[1]*1.8+.15+.05*Math.sin(t*6)]]},
  swat:t=>{const d=E.pointDir;return[[-1.2,.64],[d[0]*2.1,d[1]*2.1]]},
  type:t=>[[-.5,1.12-.12*Math.max(0,Math.sin(t*15))],[.5,1.12-.12*Math.max(0,Math.sin(t*15+Math.PI))]],
  cover:t=>[[-.36,.2],[.36,.2]],
  peek:t=>[[-.36,.2],[.82,.78]],
  cheeks:t=>[[-.66,.6],[.66,.6]],
  facepalm:t=>[[-1.2,.66],[.18,.18+.03*Math.sin(t*3)]],
  nails:t=>[[-1.2,.66],[.6,.98+.03*Math.sin(t*2)]],
  dust:t=>[[-.7+.22*Math.sin(t*18),.52],[.72-.22*Math.sin(t*18),.58]],
  stretch:t=>[[-.55,-2.3],[.55,-2.3]],
  rub:t=>[[-.36+.06*Math.sin(t*14),.24+.05*Math.cos(t*14)],[.36-.06*Math.sin(t*14),.24+.05*Math.cos(t*14)]],
  flail:t=>[[-1.32+.22*Math.sin(t*17),-.6+.32*Math.sin(t*13)],[1.32+.22*Math.sin(t*15+1),-.6+.32*Math.sin(t*11+2)]],
  glasses:t=>[[-1.2,.65],[.72,-.05]],
  listen:t=>[[-.28,1.02],[.28,1.02]],
  scratch:t=>[[-1.2,.62],[.98,-.4+.06*Math.sin(t*22)]],
  hands:t=>[[-.32,1.22],[.32,1.22]],
  conduct:t=>[[-1.1+.18*Math.sin(t*4),.32+.24*Math.sin(t*8)],[1.1+.18*Math.sin(t*4+.5),.32+.24*Math.sin(t*8+Math.PI)]],
  dance:t=>[[-1.2+.1*Math.sin(t*8),-.5+.55*Math.sin(t*8)],[1.2+.1*Math.sin(t*8),-.5-.55*Math.sin(t*8)]],
  limp:t=>[[-1.15,.95+.04*Math.sin(t*3)],[1.15,.95+.04*Math.sin(t*3+1)]]};

const E={x:0,y:0,vx:0,vy:0,tx:0,ty:0,speed:1,perch:0,landed:0,mode:'fly',q:0,qv:0,tilt:0,tv:0,spin:0,spinOn:0,r:1,rT:1,alpha:1,alphaT:1,
  tipX:0,tipXv:0,tipH:1.85,tipHv:0,side:[0,0],gx:0,gy:0,gtx:0,gty:0,ms:[0,0],blink:0,blinkT:0,nextBlink:2,
  F:face('calm'),FT:face('calm'),pose:'rest',hands:[{x:0,y:0,vx:0,vy:0},{x:0,y:0,vx:0,vy:0}],
  bubble:null,look:null,antic:0,pointDir:[1,0],sweat:0,zz:0,think:0,hum:0,shadesDown:0,flap:0,flapT:0,shadesUntil:0};
const parts=[];

/* ===== places in the real window ===== */
function rect(sel){const el=typeof sel==='string'?document.querySelector(sel):sel;if(!el)return null;const r=el.getBoundingClientRect();if(!r.width||!r.height)return null;
  return{l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width,h:r.height,cx:r.left+r.width/2,cy:r.top+r.height/2}}
function lastOf(sel){const a=document.querySelectorAll(sel);for(let i=a.length-1;i>=0;i--){const r=rect(a[i]);if(r&&r.b>0&&r.t<H)return r}return null}
function area(){if(DESK){const t=(DW.menubar||24)+R*2,b=H-((DW.dock&&DW.dock.side==='bottom')?DW.dock.h:0);return{l:0,t,r:W,b,w:W,h:b-t,cx:W/2,cy:(t+b)/2}}
  return rect('body>main')||{l:0,t:0,r:W,b:H,w:W,h:H,cx:W/2,cy:H/2}}
function deskPerch(){const ws=(DW.windows||[]).filter(w=>w.perch);if(!ws.length)return null;const w=ws[Math.random()<.6?0:Math.floor(Math.random()*ws.length)];
  return{x:cl(w.x+w.w*rnd(.55,.9),R*2,W-R*2),y:w.y-R*1.02,perch:1}}
function deskNap(){const d=DW.dock;if(!d||d.side!=='bottom')return null;return{x:d.x+d.w*(Math.random()<.5?rnd(.04,.2):rnd(.8,.96)),y:d.y-R*1.0,perch:1}}
function deskNotch(){const n=DW.notch;if(!n)return null;return{x:n.x+n.w/2,y:n.h+R*1.15,notch:1}}
function composer(){const c=document.querySelectorAll('.composer');for(let i=c.length-1;i>=0;i--){const r=rect(c[i]);if(r&&r.t<H&&r.b>0)return r}return null}
const SP={
  rest:()=>{const m=area();if(DESK)return{x:m.r-R*3,y:m.b-R*2.2};return{x:m.r-R*2.6,y:cl(m.t+m.h*.62,R*3,H-R*3)}},
  free:()=>{const m=area();return{x:rnd(m.l+m.w*.72,m.r-R*2.2),y:rnd(m.t+m.h*.22,m.t+m.h*.7)}},
  perch:()=>{if(DESK)return deskPerch()||SP.rest();const c=composer();if(!c)return SP.rest();return{x:cl(c.r-R*3.2,c.l+R,c.r-R),y:c.t-R*1.02,perch:1}},
  work:()=>{const r=lastOf('.jobc')||lastOf('.run')||lastOf('.msg');const m=area();if(!r)return SP.rest();return{x:cl(r.r+R*1.8,m.l+R*2,m.r-R*1.8),y:cl(r.cy,m.t+R*3,m.b-R*3)}},
  notch:()=>deskNotch()||SP.rest(),
  nap:()=>(DESK?deskNap():null)||SP.perch(),
  listen:()=>{const c=composer();if(!c)return SP.rest();return{x:cl(c.r-R*1.8,R*2,W-R*2),y:c.t-R*1.5}},
  topRight:()=>{const m=area();return{x:m.r-R*3,y:m.t+R*3.4}},
};
function go(s,o={}){const p=typeof s==='function'?s():s;if(!p)return;const far=Math.hypot(p.x-E.x,p.y-E.y)>R*3;
  E.tx=cl(p.x,R*1.5,W-R*1.5);E.ty=cl(p.y,p.notch?R*1.2:R*2.4,H-R*1.4);E.perch=!!p.perch;E.speed=o.speed||1;E.landed=0;
  if(!hideUntil&&!E.fs){if(p.notch)E.rT=.6;else if(E.rT===.6)E.rT=1}
  if(far&&!REDUCE&&E.mode==='fly'&&!o.now){E.antic=.26;E.anticDir=[Math.sign(p.x-E.x)||1,Math.sign(p.y-E.y)];E.look={pt:{x:p.x,y:p.y},until:now+.6}}}

/* ===== the brain ===== */
const N={energy:.75,curiosity:.6,social:.35,boredom:.25,annoy:.05,fond:.3};
function bump(k,v){N[k]=cl(N[k]+v,0,1)}
const annoyed=()=>N.annoy>.55,idleFace=()=>ATT()==='sweet'?'neutral':(ATT()==='savage'?'unimpressed':'calm');
/* a person needs time to read: about 13 characters a second, never under 3s */
const readTime=t=>Math.min(14,2.6+String(t||'').length/13);
function say(t,d,o){o=o||{};t=String(t||'');if(!t&&!o.dots)return;const ty=o.type?t.length/30:0;E.bubble={text:t,t:0,d:ty+Math.max(d||0,o.dots?0:readTime(t)),type:!!o.type,cps:30,last:-1,dots:!!o.dots}}
let SEQ=null;
function play(steps,done){SEQ={steps,i:-1,t:0,done};next()}
function next(){SEQ.i++;SEQ.t=0;const s=SEQ.steps[SEQ.i];if(!s){const d=SEQ.done;SEQ=null;d&&d();return}if(s.say){const t=L(s.say);if(t)say(t,s.sayD,{type:1})}if(s.fn)s.fn()}
function seqTick(dt){if(!SEQ)return;const s=SEQ.steps[SEQ.i];SEQ.t+=dt;if(SEQ.t>=s.d)next()}
const cur_=k=>SEQ&&SEQ.steps[SEQ.i]&&SEQ.steps[SEQ.i][k];
let base={pose:'rest',face:'calm',look:null},act=null,mood=null,fidgetIn=6;
const CTX={project:'',job:null,last:'',lastT:0,issue:'',issueT:0};

/* What Ember does with itself. It is never just parked: it has ERRANDS of its
   own — it goes somewhere for a reason and does the thing when it gets there
   (dusts a project card, reads the newest message, polishes the send button,
   doodles on the chat box, patrols the top of the window, juggles sparks,
   looks out at the stars, works out), rests a little in between, naps when
   it is tired, and comes to you when it wants company. Every move ends in an
   activity a person can name; nothing drifts to a random point. */
const SEEN={};
const maybe=(a,p)=>Math.random()<p?L(a):'';
function visible(sel){const out=[];for(const el of document.querySelectorAll(sel)){const r=rect(el);if(r&&r.w>30&&r.h>14&&r.t>0&&r.b<H&&r.l>=0&&r.r<=W)out.push(el)}return out}
function interest(){for(const sel of['.jobc','.truth','.msg','.rcard']){const r=lastOf(sel);if(r&&r.w>40)return{r,sel}}return null}
function puff(x,y,n,kind){for(let i=0;i<(n||6);i++)parts.push({k:kind||'smoke',x:x+rnd(-14,14),y:y+rnd(-4,6),vx:rnd(-30,30),vy:rnd(-40,-10),l:1,d:rnd(.6,1.1),s:rnd(3,6),c:'255,210,150'})}
function sparkle(r,n){for(let i=0;i<(n||8);i++)parts.push({k:'spark',x:rnd(r.l,r.r),y:rnd(r.t,r.b),vx:rnd(-20,20),vy:rnd(-40,-5),l:1,d:rnd(.4,.8),s:rnd(1.2,2.2),c:'255,230,180'})}
/* an errand: where to go, then what to do there */
const ERRANDS={
  dust:{w:()=>visible('.pitem,.rcard').length?.9:0,spot(){const el=pick(visible('.pitem,.rcard'));this.el=el;const r=rect(el);return r&&{x:cl(r.l+r.w*rnd(.55,.8),R*2,W-R*2),y:r.t-R*1.02,perch:1}},
    steps(){const el=this.el;const pt=()=>{const r=rect(el);return r?{x:r.cx,y:r.cy}:{x:E.x,y:E.y+40}};
      return[{d:.6,look:{pt},face:'curious'},{d:2.8,pose:'dust',face:'focus',look:{pt},fn:()=>{const r=rect(el);if(r){puff(E.x,E.y+R,8);setTimeout(()=>puff(E.x,E.y+R,6),900)}}},
        {d:.5,pose:'rest',face:'calm'},{d:1.4,pose:'hips',face:'smug',look:'viewer',say:maybe(['all clean!','spotless.',"spotless. you're welcome."],.4)}]}},
  read:{w:()=>visible('.msg,.mtext,.atext').length?N.curiosity+.25:0,spot(){const el=pick(visible('.msg,.mtext,.atext').slice(-3));this.el=el;const r=rect(el),m=area();
      return r&&{x:cl(r.r+R*1.6,m.l+R*2,m.r-R*2),y:cl(r.t+R*1.5,m.t+R*3,m.b-R*3)}},
    steps(){const r=rect(this.el);if(!r)return[{d:.5}];const rows=Math.max(2,Math.min(5,Math.round(r.h/22))),st=[];
      for(let i=0;i<rows;i++){const y=r.t+(i+.5)*r.h/rows;st.push({d:.25,look:{pt:{x:r.l+20,y}},face:'focus'},{d:.75,look:{pt:{x:r.r-20,y}},face:'focus'})}
      st.push({d:.9,face:'think',pose:'think',look:{dir:[.4,-.7]}},{d:1.4,face:ATT()==='sweet'?'happy':'calm',look:'viewer',fn:()=>{E.nod=.9},say:maybe(['huh, interesting.','hm. not bad.','read it. it\'s fine. I guess.'],.35)});return st}},
  polish:{w:()=>shown('#sendbtn,button[onclick^=consoleSend]')?.55:0,spot(){const el=shown('#sendbtn,button[onclick^=consoleSend]');this.el=el;const r=rect(el);return r&&{x:r.cx-R*1.6,y:r.cy-R*1.1}},
    steps(){const el=this.el;return[{d:.4,look:{pt:()=>{const r=rect(el);return r?{x:r.cx,y:r.cy}:{x:E.x,y:E.y}}},face:'curious'},
      {d:2.4,pose:'rub',face:'content',fn:()=>{const r=rect(el);if(r){sparkle(r,10);setTimeout(()=>sparkle(r,10),1100)}}},
      {d:1.2,pose:'hips',face:'smug',look:{pt:()=>{const r=rect(el);return r?{x:r.cx,y:r.cy}:{x:E.x,y:E.y}}},say:maybe(['shiny!','shiny. as it should be.','shiny. press it already.'],.4)}]}},
  doodle:{w:()=>composer()?.6:0,spot(){const c=composer();return c&&{x:cl(c.l+c.w*rnd(.2,.45),c.l+R*2,c.r-R*2),y:c.t-R*1.02,perch:1}},
    steps(){return[{d:2.6,pose:'type',face:'focus',hum:1,look:{dir:[0,1]}},{d:1.1,pose:'think',face:'think',look:{dir:[.5,-.8]}},
      {d:2.2,pose:'type',face:'content',hum:1,look:{dir:[0,1]}},{d:1.2,pose:'hips',face:'smug',look:'viewer',say:maybe(['writing you a poem. no peeking.','drafting. don\'t look.','writing my memoir. chapter one: you.'],.35)}]}},
  patrol:{w:()=>N.energy>.4?.5:0,spot(){const m=area();this.dir=Math.random()<.5?1:-1;return{x:this.dir>0?m.l+R*3:m.r-R*3,y:m.t+R*2.4}},
    steps(){const m=area(),d=this.dir,st=[];for(let i=1;i<=4;i++){const x=d>0?m.l+(m.w-R*6)*i/4+R*3:m.r-(m.w-R*6)*i/4-R*3;
        st.push({d:1.5,face:'sideeye',look:{dir:[0,1]},fn:()=>go({x,y:m.t+R*2.4},{now:1,speed:.55})},{d:.6,look:{dir:[d,.4]},face:'curious'})}
      st.push({d:1.2,face:'calm',look:'viewer',say:maybe(['all quiet up here.','patrol done. nothing to report.','patrol done. you\'re safe. barely.'],.35)});return st}},
  juggle:{w:()=>N.energy>.55&&N.boredom>.3?.45:0,spot(){return SP.rest()},
    steps(){const st=[];for(let i=0;i<6;i++)st.push({d:.45,pose:i%2?'cheer':'hands',face:'joy',look:{dir:[0,-1]},fn:()=>{for(let k=0;k<3;k++)parts.push({k:'spark',x:E.x+rnd(-8,8),y:E.y-R*1.2,vx:rnd(-60,60),vy:-rnd(180,260),l:1,d:.9,s:2.4,c:'255,190,110'})}});
      st.push({d:1.4,pose:'hips',face:'smug',look:'viewer',say:maybe(['ta-da!','did you see that? I\'m talented.','that was flawless. clap.'],.45)});return st}},
  stargaze:{w:()=>N.energy<.6?.4:.2,spot(){const m=area();return{x:Math.random()<.5?m.l+R*3:m.r-R*3,y:m.t+R*3.2}},
    steps(){return[{d:2.6,look:{dir:[0,-1]},face:'content',hum:1},{d:1.2,look:{dir:[.6,-.8]},face:'sleepy'},{d:1,look:'viewer',face:'calm',say:maybe(['the stars look nice tonight.','nice up here.',"don't tell anyone I like it up here."],.3)}]}},
  workout:{w:()=>N.boredom>.55&&N.energy>.4?1.2:0,spot(){return SP.rest()},steps(){return workout()}},
};
function errandAct(k){const e=ERRANDS[k];return{w:e.w,errand:1,dur:[30,40],run(){const p=e.spot.call(e);if(!p){act.t=act.dur+1;return}act.stage='go';go(p,{speed:.75})}}}
const ACTS={
  rest:{w:()=>.55+(1-N.energy)*.4,run(){go(SP.rest,{speed:.6})},dur:[6,11]},
  perch:{w:()=>(DESK?(DW.windows||[]).some(w=>w.perch):!!composer())?.4:0,run(){go(SP.perch,{speed:.6})},dur:[8,14]},
  watch:{w:()=>!DESK&&interest()?N.curiosity*.6:0,run(){const t=interest();if(!t){go(SP.rest,{speed:.6});this.pt=null;return}
    const m=area(),side=t.r.r+R*2<m.r?1:-1;go({x:cl(side>0?t.r.r+R*1.8:t.r.l-R*1.8,m.l+R*2,m.r-R*2),y:cl(t.r.t+Math.min(t.r.h*.4,R*3),m.t+R*3,m.b-R*3)},{speed:.7});
    this.pt={x:t.r.cx,y:t.r.cy}},dur:[7,11],watch:1},
  follow:{w:()=>cur.active()&&!annoyed()&&N.social>.55?N.social*.9+N.fond*.4:0,run(){},dur:[5,8],follow:1},
  avoid:{w:()=>cur.active()&&annoyed()?2:0,run(){},dur:[5,7],avoid:1},
  nap:{w:()=>N.energy<.2?2.6:0,run(){go(SP.nap,{speed:.5})},dur:[22,35],nap:1},
  notch:{w:()=>DESK&&DW.notch?N.curiosity*.5+.15:0,run(){go(SP.notch,{speed:.7})},dur:[10,16]},
  zoomies:{w:()=>N.energy>.92&&N.boredom>.85?.3:0,run(){this.z=0},dur:[2.6,3.2],zoom:1},
};
if(!DESK)for(const k of Object.keys(ERRANDS))ACTS['e_'+k]=errandAct(k);else ACTS.e_stargaze=errandAct('stargaze'),ACTS.e_juggle=errandAct('juggle'),ACTS.e_workout=errandAct('workout');
/* a stretch, jumping jacks, squats, catch its breath, admire itself */
function workout(){const st=[{d:1.1,pose:'stretch',face:'focus',look:{dir:[0,-1]},say:['stretch break!','stretch break. I sat too long.','my back. ugh. stretching.']}];
  for(let i=0;i<4;i++)st.push({d:.32,pose:'cheer',face:'focus',fn:()=>{E.vy=-210;E.q=.28}},{d:.32,pose:'rest',fn:()=>{E.q-=.22}});
  for(let i=0;i<3;i++)st.push({d:.5,pose:'hands',face:'focus',fn:()=>{E.q-=.38}},{d:.45,pose:'hips',fn:()=>{E.q+=.3}});
  st.push({d:1.4,pose:'limp',face:'sleepy',fn:()=>{E.sweat=1}},{d:1.8,pose:'hips',face:ATT()==='sweet'?'happy':'smug',look:'viewer',fn:()=>{E.sweat=0},say:['done! I feel great.','done. fit as a fiddle.',"that was nothing. I'm a machine."]});
  return st}
function choose(){const e=Object.entries(ACTS).map(([k,a])=>[k,Math.max(0,a.w())*rnd(.65,1.35)*(act&&act.k===k?.3:1)]).sort((a,b)=>b[1]-a[1]);
  const k=e[0][0],a=ACTS[k];act={k,a,t:0,dur:rnd(a.dur[0],a.dur[1])};a.run.call(a)}
const FIDGETS={
  glance:{w:()=>.8,s:()=>[{d:.7,look:{dir:[-1,.1]},face:idleFace()},{d:.15,look:{dir:[1,-.05]}},{d:.8,look:{dir:[1,-.05]},face:'curious'},{d:.9,look:'viewer',face:ATT()==='sweet'?'happy':'sideeye'}]},
  judge:{w:()=>cur.still()&&ATT()!=='sweet'?1.4:0,s:()=>[{d:2.4,look:'cursor',face:'unimpressed',fn:()=>{E.noBlink=2.4}},{d:.9,look:'cursor',face:'content'}]},
  slowblink:{w:()=>cur.still()?.5+N.fond:.3,s:()=>[{d:.5,look:'viewer',face:'calm'},{d:1,look:'viewer',face:'content'},{d:.6,look:'viewer',face:idleFace()}]},
  crossed:{w:()=>ATT()==='sweet'?0:.9,s:()=>[{d:3.2,pose:'crossed',face:ATT()==='savage'?'unimpressed':'calm',look:'viewer'}]},
  nails:{w:()=>ATT()==='sweet'?.1:N.boredom+.2,s:()=>[{d:1.6,pose:'nails',face:'calm',look:{pt:()=>({x:E.hands[1].x,y:E.hands[1].y})}},{d:.9,pose:'nails',face:'unimpressed',look:'viewer'}]},
  yawn:{w:()=>N.energy<.55?1.4:.15,s:()=>[{d:.45,pose:'stretch',face:'sleepy',look:{dir:[0,-1]}},{d:1.4,pose:'stretch',face:'yawn'},{d:.7,pose:'rest',face:'sleepy'}]},
  hum:{w:()=>.5+N.energy*.3,s:()=>[{d:3.4,pose:'conduct',face:'content',hum:1,look:{dir:[0,-.3]}}]},
  scratch:{w:()=>N.curiosity*.5,s:()=>[{d:1.5,pose:'scratch',face:'think',look:{dir:[.6,-.5]}}]},
  sigh:{w:()=>N.boredom>.6?1.2:0,s:()=>[{d:.7,face:'calm',fn:()=>{E.q+=.22}},{d:1.2,face:'disappointed',pose:'limp',fn:()=>{E.q-=.28}},{d:.4,face:idleFace()}]},
  sneeze:{w:()=>.14,s:()=>[{d:.55,face:'presneeze',fn:()=>{E.tv-=1.2}},{d:.45,face:'presneeze',fn:()=>{E.tv-=1.6;E.q+=.15}},
    {d:.3,face:'surprised',pose:'flail',say:'achoo!',fn:sneezeBurst},{d:1.4,pose:'cover',face:'shy',say:['…excuse me','you saw nothing.','don\'t. say. a word.']}]},
  read:{w:()=>lastOf('.msg')?N.curiosity*.5:0,s:()=>[{d:1.8,look:{pt:()=>{const r=lastOf('.msg');return r?{x:r.cx,y:r.cy}:{x:E.x-100,y:E.y}}},face:'sideeye'},{d:1,look:'viewer',face:idleFace()}]},
};
function sneezeBurst(){E.tv+=4;E.q-=.4;E.vx-=120;for(let i=0;i<22;i++){const a=rnd(-1,1);parts.push({k:'spark',x:E.x,y:E.y-R*.2,vx:Math.cos(a)*rnd(80,240),vy:Math.sin(a)*rnd(60,160)-40,l:1,d:rnd(.4,.9),s:rnd(1.2,2.4),c:'255,200,120'})}}
function fidget(){const e=Object.entries(FIDGETS).map(([k,f])=>[k,f.w()*rnd(.6,1.4)]).sort((a,b)=>b[1]-a[1]);play(FIDGETS[e[0][0]].s())}
function celebrate(){E.vy=-Math.max(520,H*.7);E.q=.6;E.spinOn=1;
  for(let i=0;i<50;i++){const a=Math.random()*Math.PI*2,s=rnd(130,400);parts.push({k:'spark',x:E.x,y:E.y,vx:Math.cos(a)*s,vy:Math.sin(a)*s-140,l:1,d:rnd(.6,1.5),s:rnd(1.4,3.2),c:Math.random()<.5?'255,215,130':'255,120,90'})}}

/* ===== real signals ===== */
const lastSig={};
function fresh(k,gap){const t=performance.now()/1000;if(lastSig[k]&&t-lastSig[k]<gap)return false;lastSig[k]=t;return true}
function setMood(m){mood=m;if(m)m.t=0}
const SIG={
  typing(){if(!fresh('typing',25)||mood||CHAT.open)return;go(SP.listen);setMood({d:5,pose:'listen',face:ATT()==='sweet'?'curious':'sideeye',look:{pt:()=>{const c=composer();return c?{x:c.l+c.w*.4,y:c.cy}:{x:E.x,y:E.y+80}}}});
    if(Math.random()<.5)say(L(["I'm listening",'what are you typing.','type faster.']),2.2,{type:1})},
  thinking(){if(!fresh('thinking',40))return;E.think=1;setMood({d:7,pose:ATT()==='sweet'?'think':'tap',face:ATT()==='sweet'?'think':'calm',look:{dir:[-.5,-1]},end(){E.think=0}});
    if(Math.random()<.5)say(L(['hmm…',"the model's thinking. let it cook.","it's thinking. unlike some people."]),2.6,{type:1})},
  working(d){CTX.last='Aethron is running a step'+(d&&d.tool?' ('+d.tool+')':'');CTX.lastT=now;if(mood&&mood.kind==='work')return;if(!fresh('working',30))return;
    go(SP.work);setMood({kind:'work',d:12,pose:'type',face:'focus',look:{pt:()=>{const r=lastOf('.jobc')||lastOf('.run');return r?{x:r.cx,y:r.cy}:{x:E.x-90,y:E.y}}}})},
  retry(d){if(!fresh('retry',45))return;const n=d&&d.tries||'';CTX.last='the provider is not answering; Aethron is asking again'+(n?' (try '+n+')':'');CTX.lastT=now;
    setMood({d:8,pose:'tap',face:ATT()==='sweet'?'calm':'unimpressed',look:{dir:[.4,.6]}});
    setTimeout(()=>{if(!CHAT.open)play([{d:1.8,pose:'shrug',face:'tfman',look:'viewer',say:[`still trying${n?' · try '+n:''}`,`try ${n||'again'}. the provider is having a day.`,`try ${n||'again'}. this provider is trash today.`]}])},1800)},
  job(d){const j=d&&d.job;if(!j)return;CTX.project=d.project||CTX.project;CTX.job=j;const st=j.status,p=j.progress||{},r=j.result||{};
    if(['queued','running','resuming'].includes(st)){if(!mood||mood.kind!=='job'){go(SP.work);setMood({kind:'job',d:600,pose:'type',face:'focus',look:{pt:()=>{const c=lastOf('.jobc');return c?{x:c.cx,y:c.cy}:{x:E.x-90,y:E.y}}}})}
      if(p.total&&fresh('jobline',50)&&!CHAT.open){const n=p.done||0;play([{d:2.2,look:'viewer',face:ATT()==='sweet'?'happy':'calm',pose:ATT()==='sweet'?'rest':'crossed',say:[`${n} of ${p.total} lines written`,`${n} lines. you're welcome.`,`${n} lines. I'm basically carrying you.`]}])}}
    else if(st==='done'&&fresh('jobdone:'+d.project,120)){setMood(null);if(r.ok)SIG.pass({project:d.project});else SIG.broke({what:(r.problems||[])[0]||'something'})}
    else if((st==='failed'||st==='blocked'||st==='waiting')&&fresh('jobstop:'+d.project+st,120)){setMood(null);if(st==='waiting')SIG.ask({});else SIG.broke({what:j.error||'the migration'})}},
  pass(d){if(!fresh('pass',20))return;CTX.last='the browser check passed'+(d&&d.project?' on '+d.project:'');CTX.lastT=now;setMood(null);
    if(ATT()==='sweet')play([{d:.18,face:'surprised'},{d:1.6,face:'joy',pose:'cheer',say:'it all works!',fn:celebrate},{d:1.2,face:'happy',pose:'hips',look:'viewer'}]);
    else play([{d:.18,face:'surprised'},{d:1.1,face:'joy',pose:'cheer',fn:celebrate},{d:.9,pose:'glasses',face:'smug',look:'viewer',fn:()=>{E.shadesUntil=now+5}},
      {d:2.6,pose:'crossed',face:'smug',look:'viewer',say:['','told you. every page checked, all clean.',"every page works. you're welcome."]}])},
  broke(d){const what=String(d&&d.what||'something').slice(0,40);CTX.issue=(d&&d.gaps&&d.gaps.length?d.gaps.join('; '):what+' is broken').slice(0,500);CTX.issueT=now;if(!fresh('broke',25))return;CTX.last=what+' failed';CTX.lastT=now;E.sweat=1;
    const r=lastOf('.jobc')||lastOf('.truth')||lastOf('.run');if(r){go({x:r.r+R*1.6,y:r.cy});const dx=r.cx-E.x;E.pointDir=[dx<0?-1:1,.1]}
    setMood({d:7,pose:'point',face:ATT()==='sweet'?'worried':'tfman',look:r?{pt:{x:r.cx,y:r.cy}}:'viewer',end(){E.sweat=0}});
    play([{d:.9,face:'shocked',pose:'cheeks',look:'viewer'},{d:2.6,say:[`${what} broke. look here.`,`${what} broke. relax, look here.`,`${what} shit the bed. look.`]}])},
  refused(){if(!fresh('refused',60)||CHAT.open)return;play([{d:1.8,pose:'shrug',face:'calm',look:'viewer',say:["that's a rule, not a bug",'rules are rules. not a bug.','nope. rules. deal with it.']}])},
  ask(){if(!fresh('ask',60))return;play([{d:2.4,pose:'point',face:'curious',look:'viewer',fn:()=>{E.pointDir=[-1,.3]},say:['it needs an answer from you','it has a question. answer it.','it asked you something. hello?']}])},
  published(d){if(!fresh('pub',20))return;CTX.last='the site went live';CTX.lastT=now;play([{d:.2,face:'surprised'},{d:1.6,face:'joy',pose:'cheer',fn:celebrate,say:['your site is live!','live. on the internet. wow.','live. try not to break it.']},{d:1.2,face:'smug',pose:'hips',look:'viewer'}])},
  update(){if(!fresh('update',3600)||CHAT.open)return;const r=rect('.uc');if(r){go({x:r.cx,y:r.b+R*2.2});E.pointDir=[0,-1]}
    play([{d:2.6,pose:'point',face:'curious',look:'viewer',say:['ooh, an update is ready','update. go on, click it.','update ready. click it already.']}])},
  back(d){if(!fresh('back',600))return;const p=CTX.project;E.F=face('sleepy');
    play([{d:.6,face:'sleepy'},{d:1,pose:'rub',face:'sleepy'},{d:1.2,pose:'stretch',face:'yawn'},
      {d:2.6,pose:ATT()==='sweet'?'wave':'crossed',face:ATT()==='sweet'?'joy':'calm',look:'viewer',say:[`welcome back!`,`oh. you're back.`,`look who finally showed up.`]}])},
  error(d){SIG.broke({what:(d&&d.what)||'that run'})},
  gaps(d){const g=d&&d.gaps||[];if(g.length){CTX.issue=g.join('; ').slice(0,500);CTX.issueT=now}else{CTX.issue=''}},
};
function signal(name,data){if(!SIG[name])return;if(DRV.on||V.state!=='idle'){const d=data||{};if(name==='gaps')SIG.gaps(d);else if(name==='broke'){CTX.issue=(d.gaps&&d.gaps.length?d.gaps.join('; '):String(d.what||'something')+' is broken').slice(0,500);CTX.issueT=now}else if(name==='job'&&d.job){CTX.project=d.project||CTX.project;CTX.job=d.job}return}if(!DESK&&CFG.desktop){post('/api/ember/signal',{name,data:data||{}}).catch(()=>{});return}
  if(!started)return;try{SIG[name](data||{})}catch(e){}}
function fromEvents(evs,bridge){if(!evs)return;if(!DESK&&CFG.desktop){
    for(const e of evs){if(e.type==='tool')signal(/blocked|denied|refused/i.test(String(e.status||''))?'refused':'working',{tool:String(e.label||e.name||'').replace(/^mcp__aethron__/,'')});
      else if(e.type==='done'&&e.error&&!String(e.text||'').startsWith('[ede_diagnostic]'))signal('error',{})}
    if(bridge&&/retry/i.test(String(bridge.state||bridge.status||'')))signal('retry',{tries:bridge.tries||bridge.failures||''});return}
  if(!started||DRV.on||V.state!=='idle')return;
  for(const e of evs){if(e.type==='tool'&&/blocked|denied|refused/i.test(String(e.status||'')))SIG.refused();
    else if(e.type==='tool')SIG.working({tool:String(e.label||e.name||'').replace(/^mcp__aethron__/,'')});
    else if(e.type==='done'&&e.error&&!String(e.text||'').startsWith('[ede_diagnostic]'))SIG.error({});
    else if(e.type==='done'){if(mood&&mood.kind==='work')setMood(null)}}
  if(bridge&&/retry/i.test(String(bridge.state||bridge.status||'')))SIG.retry({tries:bridge.tries||bridge.failures||''})}

/* ===== the person ===== */
const cur={x:-1,y:-1,t:-99,moveT:-99,over:0,down:0,grab:0,sx:0,sy:0,hist:[],rev:[],lastDx:0,boops:[],nearSince:0,
  active(){return now-this.t<3&&this.x>=0},still(){return this.x>=0&&now-this.moveT>1.2&&now-this.t<12}};
function onBody(x,y){const rr=R*E.r;return running&&E.alpha>.3&&(Math.hypot(x-E.x,(y-E.y)*1.05)<rr*1.2||Math.hypot(x-E.x-E.tipX*rr,y-E.y+rr*1.2)<rr*.6)}
let tookDown=false;
function onMove(e){const x=e.clientX,y=e.clientY;const dx=x-cur.x,dist=Math.hypot(dx,y-cur.y),dt=Math.max(.008,now-cur.t);
  if(dist>1)cur.moveT=now;cur.x=x;cur.y=y;cur.t=now;cur.hist.push({x,y,t:now});if(cur.hist.length>6)cur.hist.shift();if(!running)return;
  const near=Math.hypot(x-E.x,y-E.y);
  if(!cur.grab&&!SEQ&&!CHAT.open&&dist/dt>2200&&near<R*3&&E.mode==='fly'){E.vx+=(E.x-x)/near*240;E.vy+=(E.y-y)/near*240;bump('annoy',.08);
    play([{d:.35,face:'shocked',pose:'flail'},{d:1.3,face:ATT()==='sweet'?'happy':'glare',look:'cursor',say:["oh! it's you",'tf? warn me.','jesus christ, man.']}])}
  if(cur.down&&!cur.grab&&Math.hypot(x-cur.sx,y-cur.sy)>5){cur.grab=1;E.mode='grab';SEQ=null;document.documentElement.classList.add('ember-grab');say(L(['whoa!','excuse me?','put me down.']))}
  const over=onBody(x,y);cur.over=over;document.documentElement.classList.toggle('ember-over',over&&!cur.grab);
  if(over&&!cur.grab&&Math.abs(dx)>1.5){const s=Math.sign(dx);if(s!==cur.lastDx&&cur.lastDx)cur.rev.push(now);cur.lastDx=s;cur.rev=cur.rev.filter(t=>now-t<1.1);
    if(cur.rev.length>=3&&!(E.purr>0)&&!SEQ){
      if(annoyed()&&ATT()!=='sweet'){cur.rev=[];const n=Math.hypot(cur.x-E.x,cur.y-E.y)||1;E.pointDir=[(cur.x-E.x)/n,(cur.y-E.y)/n];bump('annoy',.05);
        play([{d:.25,pose:'swat',face:'glare',look:'cursor',say:['','no.','hands off.']},{d:1.2,pose:'crossed',face:'glare',look:'cursor'}])}
      else{E.purr=ATT()==='sweet'?3:2.4;SEQ=null;say('purrr')}}}}
function onDown(e){if(e.button!==0&&e.button!==undefined&&e.pointerType==='mouse'){if(e.button===2&&onBody(e.clientX,e.clientY)){e.preventDefault();e.stopPropagation();openMenu(e.clientX,e.clientY)}return}
  menu.hidden=true;if(!onBody(e.clientX,e.clientY)){tookDown=false;return}
  e.preventDefault();e.stopPropagation();tookDown=true;cur.down=1;cur.sx=e.clientX;cur.sy=e.clientY;cur.hist=[{x:e.clientX,y:e.clientY,t:now}]}
function onUp(e){if(!cur.down)return;e.preventDefault();e.stopPropagation();cur.down=0;document.documentElement.classList.remove('ember-grab');
  if(cur.grab){cur.grab=0;const h=cur.hist,a=h[0],b=h[h.length-1],dt=Math.max(.016,b.t-a.t);E.vx=(b.x-a.x)/dt;E.vy=(b.y-a.y)/dt;const s=Math.hypot(E.vx,E.vy);
    E.mode='thrown';E.thrown=s;if(s>900){say(L(['wheeee!','AAAA','what the f—']));bump('annoy',.2)}else say(L(['thanks','…fine.','you done?']))}
  else boop()}
function swallow(e){if(tookDown||onBody(e.clientX,e.clientY)){e.preventDefault();e.stopPropagation();if(e.type==='click')tookDown=false}}
function boop(){cur.boops=cur.boops.filter(t=>now-t<4);cur.boops.push(now);const n=cur.boops.length;E.q-=.4;bump('annoy',.13);bump('social',-.1);E.giggle=.3;
  if(ATT()==='sweet'){play(n>=3?[{d:1.1,face:'grumpy',pose:'hips',look:'viewer',say:'hey!'},{d:.8,face:'mischief'}]:[{d:.2,face:'surprised'},{d:.8,face:'joy',say:n===2?'boop!':''}]);return}
  if(n>=4){cur.boops=[];bump('annoy',.2);play([{d:1.6,face:'haughty',pose:'crossed',look:{dir:[1,-.5]},say:['','bye.',"I'm done with you."],fn:()=>{const m=area();go({x:E.x>m.cx?m.l+m.w*.62:m.r-R*2.4,y:rnd(m.t+m.h*.3,m.t+m.h*.6)},{now:1})}}]);return}
  play({1:[{d:.25,face:'surprised'},{d:1.3,face:'sideeye',look:'viewer',say:['','…','don\'t.']}],
    2:[{d:.2,face:'surprised'},{d:1.7,face:'tfman',look:'viewer',say:['','do that again.','do that again. I dare you.']}],
    3:[{d:.15,face:'shocked'},{d:1.8,face:'glare',pose:'hips',look:'viewer',say:['','tf man.','what the fuck, man.'],fn:()=>{E.puff=1.2;bump('annoy',.2)}},{d:.6,face:'glare',pose:'crossed',look:'viewer'}]}[n])}
function openMenu(x,y){menu.innerHTML='';const add=(t,f)=>{const b=document.createElement('button');b.textContent=t;b.onclick=()=>{menu.hidden=true;f()};menu.appendChild(b)};
  add(V.state==='listening'?'Stop listening':'Talk to Ember (double-tap)',listen);add('Type to Ember',startTalk);if(!DESK)add('Show me around',()=>welcome(1));if(CTX.issue&&now-CTX.issueT<1800&&!DESK)add('Tell Aethron to fix this',fixIssue);add('Hide for an hour',()=>{hide(3600)});add('Turn Ember off',()=>{set({on:false})});if(DESK)add('Back into Aethron',()=>{post('/api/ember/desktop',{on:false}).catch(()=>{})});else add('Companion settings…',()=>{try{openSettings()}catch(_){}});
  menu.hidden=false;menu.style.left=cl(x,8,W-190)+'px';menu.style.top=cl(y,8,H-150)+'px'}
function talkHit(x,y){if(!CHAT.open)return false;const r=talk.getBoundingClientRect();return x>=r.left-6&&x<=r.right+6&&y>=r.top-6&&y<=r.bottom+6}
function menuHit(x,y){if(menu.hidden)return false;const r=menu.getBoundingClientRect();return x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom}
/* one call from the desktop window, 20 times a second: the world in, "is the pointer on me?" out */
function desk(p){p=p||{};if(p.windows)DW.windows=p.windows;if('dock' in p)DW.dock=p.dock;if(p.notch)DW.notch=p.notch;if(p.menubar)DW.menubar=p.menubar;
  if('admin' in p)DESK_ADMIN=!!p.admin;
  if('fullscreen' in p&&!!p.fullscreen!==!!E.fs){E.fs=!!p.fullscreen;if(E.fs){E.alphaT=0}else if(!hideUntil){E.alphaT=1}}
  for(const s of p.signals||[])try{SIG[s.name]&&SIG[s.name](s.data||{})}catch(_){}
  const m=p.mouse;let hit=false;
  if(m&&m.length===2){const x=m[0],y=m[1];if(x!==cur.x||y!==cur.y){if(Math.hypot(x-cur.x,y-cur.y)>1)cur.moveT=now;cur.x=x;cur.y=y}cur.t=now;
    hit=!!(onBody(x,y)||talkHit(x,y)||menuHit(x,y)||cur.down)}
  return{hit,key:!!CHAT.open}}
function hide(sec){hideUntil=Date.now()+sec*1000;E.alphaT=0;E.rT=0;say('')}

/* ===== talking ===== */
const CHAT={open:0,turns:[],busy:0,last:0,asked:[],askIn:45,lastChat:-99,ignored:0,offline:0,mem:{name:'',facts:[]}};
{const m=store.get('ember_mem',null);if(m&&typeof m==='object'){CHAT.mem.name=String(m.name||'').slice(0,30);CHAT.mem.facts=Array.isArray(m.facts)?m.facts.map(String).slice(0,6):[]}}
function saveMem(){store.set('ember_mem',CHAT.mem)}
let tin=null;
function stateText(){const m=CHAT.mem,h=new Date().getHours(),j=CTX.job;let proj='';try{proj=(typeof S!=='undefined'&&S.cur)||CTX.project||''}catch(_){proj=CTX.project}
  return ['Local time: '+new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})+(h>=23||h<5?' (late at night)':''),
    proj?'Open project: '+proj+'.':(DESK?'Ember is out on their desktop.':'No project is open; they are on the home screen.'),
    j?('Background migration job: '+j.status+(j.progress&&j.progress.total?', '+(j.progress.done||0)+' of '+j.progress.total+' lines written':'')+'.'):'No background job running.',
    CTX.last&&now-CTX.lastT<300?'Just happened: '+CTX.last+'.':'Nothing notable just happened.',
    CTX.issue&&now-CTX.issueT<1800?'Last problem Aethron reported: '+CTX.issue+'.':'No open problem reported.',
    'Ember is annoyed at them '+Math.round(N.annoy*100)+'% and likes them '+Math.round(N.fond*100)+'%.',
    m.name?"Their name: "+m.name+'.':'You do not know their name yet.',
    m.facts.length?'Things they told you before: '+m.facts.join('; ')+'.':'They have not told you anything about themselves yet.'].join('\n')}
function placeTalk(){if(!CHAT.open)return;const rr=R*E.r,w=talk.offsetWidth||320,h=talk.offsetHeight||46;
  let x=cl(E.x-w/2,12,W-w-12),y=E.y+rr*1.45,below=1;if(y+h>H-12){y=E.y-rr*E.tipH-h-70;below=0}y=cl(y,12,H-h-12);
  talk.style.left=Math.round(x)+'px';talk.style.top=Math.round(y)+'px';talk.style.setProperty('--ox',Math.round(cl(E.x-x,0,w))+'px');talk.style.setProperty('--oy',below?'0%':'100%')}
function openChat(q,focus){CHAT.open=1;CHAT.last=now;talk.hidden=false;talk.style.animation='none';void talk.offsetWidth;talk.style.animation='';talk.classList.toggle('ae-empty',!tin.value.trim());go({x:E.x,y:E.y},{now:1});SEQ=null;E.mode='fly';
  if(q){CHAT.turns.push({role:'assistant',content:q});say(q,4,{type:1});E.FT=face(ATT()==='sweet'?'curious':'sideeye')}
  placeTalk();if(focus)setTimeout(()=>tin.focus(),30)}
function closeChat(line,f){CHAT.open=0;talk.hidden=true;tin.blur();CHAT.busy=0;CHAT.lastChat=now;if(line){play([{d:Math.max(1.8,line.length/30+1.2),face:f||idleFace(),look:'viewer'}]);say(line,2.2,{type:1})}act=null}
function question(){const m=CHAT.mem,h=new Date().getHours(),n=m.name?', '+m.name:'',q=[],add=a=>q.push(L(a));
  if(!m.name)add(["hey, I never asked. what's your name?","you never told me your name. rude.",'name. now.']);
  add([`what are you working on today${n}?`,'so. what are we doing today.','what the hell are we building today?']);
  if(CTX.project)add([`who's ${CTX.project} for?`,`who's ${CTX.project} for. be honest.`,`who are you even building ${CTX.project} for?`]);
  if(cur.still())add(['you okay? you went quiet.',"you've been staring at the screen for a while.","blink. you've been staring forever."]);
  if(h>=23||h<5)add([`it's really late${n}. still going?`,"it's past midnight. go to bed. or don't, I'm not your mom.","it's fucking late. what are you still doing up?"]);
  if(h>=5&&h<11)add([`good morning${n}! coffee first?`,"morning. you had coffee yet? you look like you haven't.",'morning. coffee. now.']);
  if(m.facts.length){const f=pick(m.facts);add([`you told me "${f}". any news?`,`last time: "${f}". so? how'd that go?`,`you said "${f}". did that work out or did you mess it up?`])}
  const fr=q.filter(x=>!CHAT.asked.includes(x));const c=pick(fr.length?fr:q);CHAT.asked.push(c);if(CHAT.asked.length>8)CHAT.asked.shift();return c}
function localReply(t){const l=t.toLowerCase(),m=/(?:i'?m|my name is|call me)\s+([a-z][a-z'-]{1,20})/i.exec(t);
  if(m&&!/^(fine|good|ok|okay|tired|busy|working|so|not|just|a|the|doing|here|back)$/i.test(m[1])){const nm=m[1][0].toUpperCase()+m[1].slice(1);
    return{say:L([`${nm}! I like that name. what are you making?`,`${nm}. fine. I'll remember it. maybe.`,`${nm}? okay ${nm}, what are we building.`]),face:ATT()==='sweet'?'joy':'calm',gesture:'nod',name:nm,mood:.3}}
  if(/\b(hi|hey|hello|yo|sup)\b/.test(l))return{say:L(["hiii! I'm Ember.","hey. I'm Ember. I live here now.","sup. it's Ember. don't wear it out."]),face:ATT()==='sweet'?'joy':'calm',gesture:'wave',mood:.1};
  if(/sorry/.test(l))return{say:L(["aw, it's okay!","…fine. you're forgiven. this time.",'apology noted. barely.']),face:'content',gesture:'nod',mood:.4};
  if(/tired|sleepy|exhausted|stress|sad|hard day/.test(l))return{say:L(["aw. take a little break? I'll keep watch.","okay. take a break. I've got this, go breathe.","okay real talk, take a break. I'll watch it."]),face:'content',gesture:'nod',mood:.2};
  if(/stupid|dumb|hate you|shut up|ugly/.test(l))return{say:L(["hey, that's not nice…",'wow. okay. noted.','bold words from someone who needs me.']),face:ATT()==='sweet'?'sad':'glare',gesture:'crossed',mood:-.6};
  if(/love|great|awesome|cool|nice|yay|thanks|thank you/.test(l))return{say:L(['yay! that makes my flame go big.',"I know. I'm great.",'obviously. took you long enough.']),face:ATT()==='sweet'?'joy':'smug',gesture:ATT()==='sweet'?'dance':'shades',mood:.4};
  if(/\?$/.test(l))return{say:L(['good question! ask Aethron in the chat box.',"ask Aethron in the chat. I'm decorative.",'do I look like I know? ask Aethron.']),face:ATT()==='sweet'?'think':'unimpressed',gesture:'shrug',mood:0};
  return{say:L(['ooh, tell me more!','huh. go on.','cool story. keep going.']),face:ATT()==='sweet'?'curious':'sideeye',gesture:'nod',mood:0}}
async function send(){const t=tin.value.trim().slice(0,300);if(!t||CHAT.busy)return;tin.value='';CHAT.last=now;CHAT.ignored=0;
  CHAT.turns.push({role:'user',content:t});CHAT.busy=1;E.FT=face('think');say('',99,{dots:1});
  let r=null;
  /* a reply is never waited on forever: past 24s Ember answers in its own words */
  if(!CHAT.offline){try{r=await Promise.race([(typeof api==='function'?api:post)('/api/ember/say',{turns:CHAT.turns.slice(-12),state:stateText(),attitude:ATT()}),
      new Promise((_,no)=>setTimeout(()=>no(new Error('slow')),24000))])}catch(e){r=null;const m=String(e&&e.message||'');if(/not configured|no key|save your key|402|403/i.test(m))CHAT.offline=1;if(m==='slow')CHAT.slow=(CHAT.slow||0)+1}}
  if(!r||typeof r.say!=='string'||!r.say.trim())r=localReply(t);
  CHAT.busy=0;if(!CHAT.open)return;
  const line=r.say.trim().slice(0,220),fc=FACES[r.face]?r.face:idleFace(),g=r.gesture||'none';
  const md=Number(r.mood);if(isFinite(md)){bump('fond',cl(md,-1,1)*.12+.03);bump('annoy',-Math.abs(md)*.1-(md<0?-md*.25:0))}
  if(r.name){CHAT.mem.name=String(r.name).slice(0,30);saveMem()}
  if(r.remember){const f=String(r.remember).slice(0,60);if(!CHAT.mem.facts.includes(f)){CHAT.mem.facts.push(f);CHAT.mem.facts=CHAT.mem.facts.slice(-6);saveMem()}}
  CHAT.turns.push({role:'assistant',content:line});if(CHAT.turns.length>24)CHAT.turns=CHAT.turns.slice(-24);CHAT.last=now;gesture(g,fc,line)}
function gesture(g,fc,line){const st={d:line.length/30+1.9,face:fc,look:'viewer'};
  const fn={nod:()=>{E.nod=.9},shake:()=>{E.shake=.9},cheer:()=>{E.vy=-320;E.q=.35},dance:()=>{E.dance=Math.min(3.5,st.d)},shades:()=>{E.shadesUntil=now+st.d+.5}}[g];
  const pose={wave:'wave',cheer:'cheer',shrug:'shrug',think:'think',shy:'peek',dance:'dance',crossed:'crossed',facepalm:'facepalm',hips:'hips',shades:'glasses',turn_away:'crossed'}[g];
  if(pose)st.pose=pose;if(g==='hum')st.hum=1;if(g==='turn_away')st.look={dir:[1,-.5]};if(fn)st.fn=fn;
  play([{d:.01,fn:()=>say(line,2.4,{type:1})},st])}
function startTalk(){if(!running)return;if(CHAT.open){tin.focus();return}
  if(annoyed()&&ATT()!=='sweet'){play([{d:1.6,face:'haughty',pose:'crossed',look:{dir:[1,-.5]},say:['','not now.',"not now. I'm mad at you."]}],()=>openChat(L(['','…fine. what.','…ugh. fine. what.']),1));return}
  openChat(CHAT.turns.length?L(["yes? I'm all ears.",'what now.','what. again.']):L(["hi! I'm Ember. what are you up to?",'oh. you want to talk. okay. what.','you rang? make it quick.']),1)}
function chatTick(dt){placeTalk();const typing=now-(CHAT.typing||-9)<1.5;
  base.pose=typing?'listen':(ATT()==='sweet'?'rest':'crossed');base.face=CHAT.busy?'think':(typing?(ATT()==='sweet'?'curious':'sideeye'):idleFace());
  base.look=typing?{pt:()=>({x:parseFloat(talk.style.left)+90,y:parseFloat(talk.style.top)+14})}:'viewer';
  if(typing&&!SEQ&&Math.random()<dt*.5)E.nod=.5;
  if(!CHAT.busy&&now-CHAT.last>(CHAT.turns.some(x=>x.role==='user')?30:18)&&document.activeElement!==tin){
    if(!CHAT.turns.some(x=>x.role==='user')){CHAT.ignored++;bump('fond',-.05);
      closeChat(CHAT.ignored>1?L(['…oh. okay.','ignored. twice. wow.','ignored by a human. cool. cool cool cool.']):L(['okay, maybe later',"fine. don't answer.",'wow. okay.']),CHAT.ignored>1?(ATT()==='sweet'?'sad':'disappointed'):'unimpressed')}
    else closeChat(L(["okay, I'll be around","I'll be around. not that you asked.",'k. bye.']))}}

/* ===== voice and hands =====
   Double-tap Ember: it listens until you have been quiet for a moment, turns
   what you said into one clear request (aethron_ember.act), then flies to the
   chat box, types it where you can watch, and presses Send — or flies to the
   app's own button (Publish) and presses that. It presses real elements with
   el.click(): nothing a person could not do, and nothing hidden. */
const V={state:'idle',text:'',rec:null,t0:0,last:0,native:null,cancel:0,poll:0};
const DRV={on:0};
const SILENCE=1.8,NOTHING=10;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function arrive(ms){return new Promise(res=>{const t0=performance.now();(function w(){
  if(!running||REDUCE||(Math.hypot(E.tx-E.x,E.ty-E.y)<R*.7&&Math.hypot(E.vx,E.vy)<170)||performance.now()-t0>(ms||2600))res();else setTimeout(w,40)})()})}
function shown(sel){if(typeof sel!=='string')return sel;const a=document.querySelectorAll(sel);
  for(let i=a.length-1;i>=0;i--){const r=rect(a[i]);if(r&&r.b>0&&r.t<H&&r.r>0&&r.l<W)return a[i]}return null}
function live(t){const b=E.bubble;if(b&&b.live&&b.t<b.d){b.text=t;b.d=b.t+99}else{say(t||' ',99);E.bubble.live=1}}
function hush(){if(E.bubble&&E.bubble.live)E.bubble.d=Math.min(E.bubble.d,E.bubble.t+.3)}
async function flyTo(target,side,o){o=o||{};const r=rect(target);if(!r)return null;
  let s=side||(r.cx>W*.6?'left':'right');let p=s==='above'?{x:cl(r.r-R*2.2,r.l+R,r.r-R),y:r.t-R*1.1,perch:1}:(s==='left'?{x:r.l-R*1.5,y:r.cy}:{x:r.r+R*1.5,y:r.cy});
  if(s==='approach')p={x:r.cx-(r.cx>W*.5?1:-1)*(r.w/2+R*1.25),y:r.cy-R*1.05};
  if(s!=='above'&&p.x>W-R*1.6)p={x:r.l-R*1.5,y:r.cy};if(s!=='above'&&p.x<R*1.6)p={x:r.r+R*1.5,y:r.cy};
  go(p,{speed:o.speed||1.15,now:REDUCE});E.look={pt:{x:r.cx,y:r.cy},until:now+30};
  const n=Math.hypot(r.cx-p.x,r.cy-p.y)||1;E.pointDir=[(r.cx-p.x)/n,(r.cy-p.y)/n];
  await arrive(3200);return rect(target)}
/* A real press: settle beside the thing, wind up, poke into it (the button
   gives under the touch), then back off. The click lands at the moment of
   contact, never from across the room. */
async function press(target){if(!target)return false;let r=rect(target);if(!r)return false;
  /* a wide thing is touched near the end Ember arrives at, like a finger would */
  const fromRight=r.cx>W*.5,wide=r.w>R*3.2,cx=wide?(fromRight?r.l+Math.min(r.w*.22,R*1.6):r.r-Math.min(r.w*.22,R*1.6)):r.cx;
  go({x:cx-(fromRight?1:-1)*(wide?R*1.7:(r.w/2+R*1.25)),y:r.cy-R*1.05},{speed:1.15,now:REDUCE});E.look={pt:{x:cx,y:r.cy},until:now+30};
  await arrive(3200);r=rect(target);if(!r)return false;r=Object.assign({},r,{cx:wide?(fromRight?r.l+Math.min(r.w*.22,R*1.6):r.r-Math.min(r.w*.22,R*1.6)):r.cx});
  await sleep(REDUCE?40:220);if(!running){target.click();return true}
  const dir=[Math.sign(r.cx-E.x)||1,(r.cy-E.y)/(Math.hypot(r.cx-E.x,r.cy-E.y)||1)];E.pointDir=[dir[0]*.92,dir[1]];
  if(mood){mood.pose='point';mood.face='focus'}E.look={pt:{x:r.cx,y:r.cy},until:now+5};
  E.q+=.22;await sleep(REDUCE?30:230);                                   // wind up
  if(mood)mood.pose='swat';const home={x:E.x,y:E.y};
  go({x:lerp(E.x,r.cx,.35),y:lerp(E.y,r.cy,.35)},{now:1,speed:2.2});await sleep(REDUCE?20:110);
  E.q-=.45;target.classList.add('ember-press');DRV.press={x:E.x,y:E.y,cx:r.cx,cy:r.cy};
  if(!REDUCE)for(let i=0;i<12;i++){const a=i/12*6.283;parts.push({k:'spark',x:r.cx,y:r.cy,vx:Math.cos(a)*110,vy:Math.sin(a)*110,l:1,d:.45,s:1.8,c:'255,200,130'})}
  target.click();await sleep(REDUCE?30:150);target.classList.remove('ember-press');
  go(home,{now:1,speed:1.4});if(mood)mood.pose='point';await sleep(REDUCE?30:300);return true}
/* where the next letter will appear in a text box, measured with its own font */
function caretPoint(ta){const r=rect(ta);if(!r)return null;const cs=getComputedStyle(ta);
  const pl=parseFloat(cs.paddingLeft)||0,pr=parseFloat(cs.paddingRight)||0,pt=parseFloat(cs.paddingTop)||0;
  const lh=parseFloat(cs.lineHeight)||(parseFloat(cs.fontSize)||16)*1.3;X.save();X.setTransform(1,0,0,1,0,0);X.font=`${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  const max=Math.max(40,r.w-pl-pr),lines=[''];for(const word of String(ta.value||'').split(/(\s+)/)){const tr=lines[lines.length-1]+word;
    if(X.measureText(tr).width>max&&lines[lines.length-1])lines.push(word.trimStart());else lines[lines.length-1]=tr}
  const w=X.measureText(lines[lines.length-1]).width;X.restore();
  return{x:cl(r.l+pl+w,r.l+pl,r.r-pr),y:cl(r.t+pt+(lines.length-.5)*lh-ta.scrollTop,r.t+4,r.b-4),lh}}
/* Ember perches over the line and types: each letter appears as its hands
   come down, it moves along with the caret, and breathes at the spaces. */
async function typeInto(ta,text){const c0=caretPoint(ta);ta.focus();ta.value='';ta.dispatchEvent(new Event('input',{bubbles:true}));
  const n=text.length,per=cl(5200/Math.max(1,n),16,58),lift=R*1.55;
  const follow=()=>{const c=caretPoint(ta);if(c)go({x:c.x+R*.35,y:c.y-Math.max(lift,c.lh*.55+R)},{now:1,speed:1.6});if(c)E.look={pt:{x:c.x,y:c.y},until:now+2}};
  if(c0&&running){go({x:c0.x+R*.35,y:c0.y-lift},{speed:1.15});await arrive(2600);if(mood){mood.pose='type';mood.face='focus'}await sleep(REDUCE?0:260)}
  for(let i=0;i<n;i++){if(V.cancel)break;const ch=text[i];ta.value+=ch;ta.dispatchEvent(new Event('input',{bubbles:true}));
    try{if(typeof growTa==='function')growTa(ta)}catch(_){}ta.selectionStart=ta.selectionEnd=ta.value.length;
    if(running){E.q-=.035;if(i%3===0||ch===' ')follow()}
    if(!REDUCE)await sleep(per*(ch===' '?1.5:/[,.!?]/.test(ch)?3.2:rnd(.55,1.35)))}
  ta.value=text;ta.dispatchEvent(new Event('input',{bubbles:true}));try{if(typeof growTa==='function')growTa(ta)}catch(_){}}
function scroller(){let el=document.getElementById('chatlog');while(el&&el!==document.body){const cs=getComputedStyle(el);
    if(/auto|scroll/.test(cs.overflowY)&&el.scrollHeight>el.clientHeight+4)return el;el=el.parentElement}
  const m=document.querySelector('body>main');if(m){for(const el of m.querySelectorAll('*')){const cs=getComputedStyle(el);if(/auto|scroll/.test(cs.overflowY)&&el.scrollHeight>el.clientHeight+40&&el.clientHeight>200)return el}}
  return document.scrollingElement}
function projectEl(name){for(const el of document.querySelectorAll('.pitem')){const n=el.querySelector('.pname');if(n&&n.textContent.trim()===name)return el}return null}
const said=(t,d)=>{if(t&&running)say(t,d||0,{type:1})};
/* the app's own controls Ember can press for you */
const CONTROLS={settings:'#setbtn',new_chat:'.newbtn',home:'.brand',preview:'button[onclick^="openPreviewPane(false)"],.pshot'};
async function perform(r){if(!r||DRV.on)return false;
  if(r.kind==='answer'){if(running)play([{d:Math.max(2.4,readTime(r.say)),face:idleFace(),look:'viewer',say:r.say}]);return true}
  if(CHAT.open)closeChat();DRV.on=1;V.cancel=0;SEQ=null;setMood({kind:'drive',d:120,pose:'point',face:'focus',look:null});
  let cur_=null;try{cur_=(typeof S!=='undefined'&&S.cur)||null}catch(_){}
  try{
    const a=r.kind==='action'?r.action:'';
    if(a==='open_project'){
      if(cur_===r.target){said(L([`you're already in ${r.target}`,`we're already in ${r.target}.`,`you're IN ${r.target}. look.`]));return true}
      const el=projectEl(r.target);if(!el){said(L([`I can't see ${r.target} in your list`,`no ${r.target} in the list.`,`${r.target}? not in the list.`]));return false}
      said(r.say||L([`opening ${r.target}`,`${r.target}. on it.`,`fine. ${r.target}.`]));
      el.scrollIntoView({block:'nearest',behavior:REDUCE?'auto':'smooth'});await sleep(REDUCE?60:420);
      await press(el);CTX.last='Ember opened '+r.target;CTX.lastT=now;return true}
    if(a==='scroll_down'||a==='scroll_up'){const sc=scroller(),sr=sc&&sc.getBoundingClientRect?rect(sc):null;
      if(sr&&running){go({x:cl(sr.r-R*2,R*2,W-R*2),y:sr.cy},{speed:1.1});await arrive(2400);if(mood){mood.pose='swat';E.pointDir=[0,a==='scroll_down'?-1:1]}}
      said(r.say);const h=(sc&&sc.clientHeight)||innerHeight;(sc||window).scrollBy({top:(a==='scroll_down'?1:-1)*h*.7,behavior:REDUCE?'auto':'smooth'});
      if(running){E.q-=.3;await sleep(700)}return true}
    if(CONTROLS[a]){const el=shown(CONTROLS[a]);if(!el){said(L(["I can't find that button right now","can't find it on this screen.","not on this screen. look again."]));return false}
      said(r.say);await press(el);return true}
    if(a==='publish'){const b=shown('#pubrail');
      if(b){said(r.say);await press(b);await sleep(900);
        const go_=shown('#pubov button[onclick^="pubGo"]');if(go_){await press(go_);CTX.last='Ember pressed Publish';CTX.lastT=now;return true}
        said(L(['it needs one more thing from you first','your turn. it wants something first.','it wants something from you first. look.']));return true}
      if(!cur_){said(L(['open a project first, then I can publish it','open a project first.','publish what? open a project.']));return false}}
    if(a&&['undo','build','check','stop','status'].includes(a)&&!cur_){said(L(['open a project first','open a project first.',"which project? open one."]));return false}
    const ta=shown('#npurl');
    if(!ta){said(L(["I can't find the chat box","where's the chat box?","no chat box. where'd you put it."]));return false}
    said(r.say,2);
    await typeInto(ta,r.prompt);if(V.cancel)return false;
    if(mood){mood.pose='point';mood.face=ATT()==='sweet'?'happy':'smug'}await sleep(REDUCE?40:420);
    const sb=shown('#sendbtn,button[onclick^="consoleSend"]');if(sb)await press(sb);else ta.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
    CTX.last='Ember sent: '+r.prompt.slice(0,80);CTX.lastT=now;return true
  }finally{DRV.on=0;setMood(null);if(running)go(SP.perch,{speed:.8})}}
function listenSay(t){live(t||L(['listening…','go on. listening.',"talk. I'm listening."]))}
async function listen(){if(!running||DRV.on)return;
  if(V.state==='listening'||V.state==='asking'){stopListen(0);return}
  if(V.state==='thinking')return;
  if(CHAT.open)closeChat();SEQ=null;V.state='asking';V.text='';V.cancel=0;V.t0=now;V.last=now;
  setMood({kind:'listen',d:200,pose:'listen',face:ATT()==='sweet'?'curious':'sideeye',look:'viewer'});listenSay('');
  if(V.native==null){try{const r=await (await fetch('/api/voice',{credentials:'same-origin'})).json();V.native=!!(r&&r.available)}catch(_){V.native=false}}
  if(V.native)return listenNative();
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(SR)return listenWeb(SR);
  V.state='idle';setMood(null);hush();
  play([{d:3,pose:'shrug',face:'calm',look:'viewer',say:["I can't hear on this computer. type to me instead?","no ears here. type it.","can't hear you. type, like it's 2005."]}],()=>openChat('',1))}
async function listenNative(){let r;try{r=await post('/api/voice/start',{})}catch(_){r={state:'error',why:'the microphone did not start'}}
  while(true){if(r.state==='listening'){V.state='listening';if(r.text!==V.text){V.text=r.text;V.last=now}listenSay(V.text)}
    if(r.state==='done'||r.state==='error'||r.state==='idle'||V.cancel)break;
    await sleep(200);try{r=await (await fetch('/api/voice/poll',{credentials:'same-origin'})).json()}catch(_){r={state:'error',why:'lost the microphone'}}}
  if(V.cancel){post('/api/voice/stop',{}).catch(()=>{});return endListen('',1)}
  endListen(r.state==='done'?r.text:'',0,r.why)}
function listenWeb(SR){let rec;try{rec=new SR()}catch(_){V.native=false;return endListen('',0,'speech recognition is not available here')}
  V.rec=rec;rec.continuous=true;rec.interimResults=true;rec.lang=navigator.language||'en-US';let fin='',err='';
  rec.onstart=()=>{V.state='listening'};
  rec.onresult=e=>{let t='';fin='';for(let i=0;i<e.results.length;i++){const x=e.results[i][0].transcript;t+=x;if(e.results[i].isFinal)fin+=x}
    if(t!==V.text){V.text=t.trim();V.last=now}listenSay(V.text)};
  rec.onerror=e=>{err=e&&e.error||'error'};
  rec.onend=()=>{clearInterval(V.poll);V.rec=null;if(V.cancel)return endListen('',1);
    endListen(V.text,0,err==='not-allowed'?'the microphone is switched off for Aethron':(err&&err!=='no-speech'&&err!=='aborted'?'I could not hear ('+err+')':''))};
  V.poll=setInterval(()=>{if(V.text&&now-V.last>=SILENCE)try{rec.stop()}catch(_){}else if(!V.text&&now-V.t0>NOTHING)try{rec.stop()}catch(_){}},150);
  try{rec.start()}catch(e){clearInterval(V.poll);endListen('',0,'the microphone did not start')}}
function stopListen(cancel){V.cancel=cancel?1:0;if(V.rec){try{cancel?V.rec.abort():V.rec.stop()}catch(_){}}
  else if(V.native)post('/api/voice/stop',{}).catch(()=>{})}
async function endListen(text,cancelled,why){V.rec=null;text=String(text||'').trim();
  if(cancelled){V.state='idle';setMood(null);hush();return}
  if(!text){V.state='idle';setMood(null);hush();
    play([{d:2.4,face:ATT()==='sweet'?'worried':'unimpressed',look:'viewer',say:why||L(["I didn't hear anything","didn't hear a thing.","you said nothing. riveting."])}]);return}
  V.state='thinking';if(mood){mood.face='think';mood.pose='think'}live('');E.bubble.dots=1;E.bubble.text='';
  return heard(text)}
async function heard(text){V.state='thinking';let r=null;
  let names=null;try{if(typeof S!=='undefined'&&Array.isArray(S.projects))names=S.projects.map(p=>p&&p.name).filter(Boolean)}catch(_){}
  try{r=await Promise.race([post('/api/ember/act',Object.assign({text,state:stateText(),attitude:ATT(),relay:DESK?1:0},names?{projects:names}:{})),
    new Promise((_,no)=>setTimeout(()=>no(new Error('slow')),34000))])}catch(e){r=null}
  V.state='idle';setMood(null);hush();
  if(!r||!r.kind){play([{d:2.6,face:'worried',look:'viewer',say:["I couldn't reach Aethron","can't reach Aethron right now.","Aethron's not answering. not my fault."]}]);return null}
  if(DESK){if(r.kind==='answer')perform(r);else play([{d:2.6,pose:'point',face:'calm',look:'viewer',fn:()=>{E.pointDir=[0,1]},say:['sending it to Aethron','handing it to Aethron.','fine. Aethron gets it.']}]);return r}
  await perform(r);return r}
function fixIssue(){if(!CTX.issue)return;perform({kind:'send',prompt:'Fix what the last check found: '+CTX.issue+'. Then check every page again in the browser.',say:L(['let me tell it','telling it.','fine. I\'ll tell it what you broke.'])})}
/* On the desktop Ember hears you; the app window does the typing. */
let inboxT=0;
function inboxTick(){if(DESK||!CFG.desktop||DRV.on)return;if(performance.now()-inboxT<1400)return;inboxT=performance.now();
  fetch('/api/ember/inbox',{credentials:'same-origin'}).then(x=>x.json()).then(d=>{const it=(d&&d.items||[]).filter(x=>x&&x.kind!=='answer');if(it.length)perform(it[it.length-1])}).catch(()=>{})}
setInterval(inboxTick,700);
addEventListener('keydown',e=>{if(e.key==='Escape'&&(V.state==='listening'||V.state==='asking'||DRV.on)){V.cancel=1;stopListen(1)}},true);

/* ===== the welcome =====
   The first time Aethron opens on a machine Ember introduces itself and
   shows the person around: it flies to each part of the app, lights it,
   says what it is for, and waits for Next (or moves on once there has been
   time to read). Skip ends it; right-click Ember or Settings replays it. */
const TW={on:0,el:null,spot:null,next:null,skip:0,i:0,n:0,bar:null};
const WELCOME=[
  {sel:'.composer',say:"This is where you tell Aethron what you want. Paste a Framer or Webflow link, drop a screenshot, or just describe the site you want."},
  {sel:'#sendbtn,button[onclick^=consoleSend]',say:"Send it with this, or press Enter, and Aethron gets to work. I can press it for you too."},
  {sel:'.newbtn',say:"New starts a fresh chat or a new project, any time."},
  {sel:'#plist',say:"Your projects live here. Click one to open it, and its chat picks up where you left off."},
  {sel:'#hrail .rcard,.hrail .rcard',say:"This shows whether everything is ready: your AI model, the browser checks and hosting."},
  {sel:'#setbtn',say:"Settings: your AI model and key, your limits, and how I behave."},
];
function tourBar(){if(TW.bar)return TW.bar;const b=document.createElement('div');b.id='ember-tour';
  b.innerHTML='<span class="n"></span><button class="sk">Skip tour</button><button class="nx">Next</button>';
  b.querySelector('.sk').onclick=()=>{TW.skip=1;TW.next&&TW.next()};b.querySelector('.nx').onclick=()=>{TW.next&&TW.next()};
  document.body.append(b);TW.bar=b;return b}
function tourWait(line){return new Promise(res=>{let done=0;const fin=()=>{if(done)return;done=1;clearTimeout(t);TW.next=null;res()};
  TW.next=fin;const t=setTimeout(fin,(readTime(line)+1.4)*1000)})}
function willWelcome(){return !DESK&&CFG.on&&!automation&&!store.get('ember_welcomed',0)}
async function welcome(force){if(TW.on||DESK)return;if(!running){if(!force)return;start()}
  if(DRV.on)return;TW.on=1;TW.skip=0;if(CHAT.open)closeChat();SEQ=null;DRV.on=1;
  setMood({kind:'tour',d:900,pose:'wave',face:'joy',look:'viewer'});const bar=tourBar();bar.hidden=false;
  const steps=WELCOME.filter(st=>shown(st.sel));TW.n=steps.length+2;
  const set=i=>{bar.querySelector('.n').textContent=(i+1)+' of '+TW.n;bar.querySelector('.nx').textContent=i+1>=TW.n?'Done':'Next'};
  try{
    const m=area();E.x=m.cx;E.y=-R*3;E.alpha=1;go({x:m.cx,y:m.t+m.h*.42},{now:1,speed:1.3});await arrive(2600);
    if(!REDUCE){celebrate()}await sleep(REDUCE?60:900);
    set(0);let line=L(["Hi! I'm Ember. I live in Aethron and I'm here to help.","Oh, hi. I'm Ember. I live in here. I help. Sometimes.","Sup. I'm Ember. I live here now, and I run this place."]);
    play([{d:9,pose:'wave',face:'joy',look:'viewer'}]);say(line,0,{type:1});await tourWait(line);
    if(!TW.skip){line="Let me show you around. It takes about a minute.";mood.pose='point';E.pointDir=[1,0];say(line,0,{type:1});await tourWait(line)}
    for(let i=0;i<steps.length&&!TW.skip;i++){const st=steps[i],el=shown(st.sel);if(!el)continue;set(i+1);
      try{el.scrollIntoView({block:'nearest',behavior:REDUCE?'auto':'smooth'})}catch(_){}
      TW.el=el;mood.pose='point';mood.face='curious';await flyTo(el,'approach');mood.face=ATT()==='sweet'?'happy':'calm';
      say(st.say,0,{type:1});await tourWait(st.say)}
    TW.el=null;TW.spot=null;
    if(!TW.skip){set(TW.n-1);go(SP.perch,{speed:.9});await arrive(2400);mood.pose='hips';mood.face=ATT()==='sweet'?'joy':'smug';
      line=L(["That's it! Double-tap me to talk out loud, or right-click me for more. I'll be around.","Done. Double-tap me to talk, right-click me for more. I'll be right here.","Tour over. Double-tap me to talk, right-click for more. Now go build something."]);
      say(line,0,{type:1});await tourWait(line)}
  }finally{store.set('ember_welcomed',1);TW.on=0;TW.el=null;TW.spot=null;bar.hidden=true;DRV.on=0;setMood(null);if(running)go(SP.perch,{speed:.7})}}

/* ===== the loop ===== */
let last=performance.now(),acc=0;
function frame(t){if(!running)return;const dt=Math.min(.05,(t-last)/1000);last=t;now+=dt;acc+=dt;if(acc>.25){needs(acc);acc=0}
  if(hideUntil&&Date.now()>hideUntil){hideUntil=0;E.alphaT=1;E.rT=1;go(SP.rest,{now:1})}
  if(mood){mood.t+=dt;if(mood.t>mood.d){const m=mood;mood=null;m.end&&m.end()}}
  if(CHAT.open)placeTalk();
  if(E.bubble&&E.bubble.box&&cur.x>=0){const k=E.bubble.box;if(cur.x>k.x-6&&cur.x<k.x+k.w+6&&cur.y>k.y-6&&cur.y<k.y+k.h+6)E.bubble.d=Math.max(E.bubble.d,E.bubble.t+1.2)}
  if(CHAT.open&&E.mode==='fly')chatTick(dt);
  else if(mood){base.pose=mood.pose||'rest';base.face=mood.face||idleFace();base.look=mood.look||null}
  else if(E.mode==='fly'&&!hideUntil){
    if(!act||act.t>act.dur)choose();
    if(act&&act.a.errand){if(act.stage==='go'&&!SEQ&&Math.hypot(E.tx-E.x,E.ty-E.y)<R*.6&&Math.hypot(E.vx,E.vy)<150){const k=act.k.slice(2),e=ERRANDS[k],me=act;me.stage='do';SEEN[k]=(SEEN[k]||0)+1;
        play(e.steps.call(e),()=>{if(act===me)me.t=me.dur+1})}else if(act.stage==='go'&&act.t>14)act.t=act.dur+1}
    if(!SEQ&&!CHAT.busy&&now-CHAT.lastChat>90&&now-cur.t<40&&cur.t>0&&!annoyed()&&!document.hidden){CHAT.askIn-=dt;if(CHAT.askIn<=0){CHAT.askIn=rnd(150,260);openChat(question(),0)}}
    act.t+=dt;
    if(act.a.follow&&cur.active()){const m=area();go({x:cl(cur.x+R*2.6*(cur.x>m.cx?-1:1),m.l+R*1.5,m.r-R*1.5),y:cl(cur.y-R*1.3,R*2.6,H-R*2)},{now:1,speed:.8})}
    if(act.a.avoid&&cur.active()&&Math.hypot(cur.x-E.x,cur.y-E.y)<R*5){const dx=E.x-cur.x,dy=E.y-cur.y,n=Math.hypot(dx,dy)||1;go({x:E.x+dx/n*R*4,y:E.y+dy/n*R*3},{now:1,speed:.7})}
    if(act.a.zoom){act.a.z+=dt;const a=act.a.z*3.2,m=area();go({x:m.l+m.w*.7+Math.sin(a)*m.w*.18,y:m.t+m.h*.45+Math.sin(a*2)*m.h*.18},{now:1,speed:2.4})}
    const napping=act.a.nap&&Math.hypot(E.tx-E.x,E.ty-E.y)<R*.8;E.zz=napping?1:0;
    if(!SEQ&&!napping&&!act.a.zoom&&!(act.a.errand&&act.stage)&&!(E.purr>0)){fidgetIn-=dt;if(fidgetIn<=0){fidgetIn=rnd(11,20);fidget()}}
    if(cur.x>=0&&!SEQ&&Math.hypot(cur.x-E.x,cur.y-E.y)<R*3.6&&!cur.over){if(!cur.nearSince)cur.nearSince=now;if(now-cur.nearSince>2.6&&now-cur.moveT>1.8){cur.nearSince=now+10;
      if(ATT()==='sweet')play([{d:1.2,pose:'cover',face:'shy'},{d:1,pose:'peek',face:'shy',look:'cursor'},{d:1.2,pose:'wave',face:'happy',look:'cursor',say:'…hi'}]);
      else play([{d:2.4,face:'unimpressed',look:'cursor',fn:()=>{E.noBlink=2.4}},{d:1.6,face:'tfman',pose:'crossed',look:'cursor',say:['','…can I help you?','take a picture, it lasts longer.']}])}}
    else if(cur.x<0||Math.hypot(cur.x-E.x,cur.y-E.y)>R*3.6)cur.nearSince=0;
    let bf=idleFace();if(annoyed())bf=N.annoy>.8?'glare':'unimpressed';else if(N.fond>.7&&ATT()!=='savage')bf='content';
    base.face=napping?'asleep':(act.a.nap?'sleepy':(N.energy<.3?'sleepy':(cur.over&&!annoyed()?(ATT()==='sweet'?'happy':'calm'):(act.a.zoom?'joy':bf))));
    base.pose=napping?'limp':(act.a.zoom?'flail':(annoyed()&&ATT()!=='sweet'?'crossed':'rest'));base.look=napping?{dir:[0,1]}:(act.a.watch&&act.a.pt?{pt:act.a.pt}:null);if(act.a.watch&&act.a.pt&&!annoyed())base.face=ATT()==='sweet'?'curious':'sideeye'}
  seqTick(dt);
  let pose=cur_('pose')||base.pose,fn=cur_('face')||base.face,look=cur_('look')||base.look;
  if(E.mode==='grab'){pose='flail';fn=Math.hypot(E.vx,E.vy)>600?(ATT()==='sweet'?'joy':'shocked'):(ATT()==='sweet'?'surprised':'tfman');look='viewer'}
  if(E.mode==='thrown'){pose='flail';fn=ATT()==='sweet'?'joy':'shocked'}
  if(E.dizzy>0){pose='limp';fn='dizzy'}
  if(E.purr>0){pose='hands';fn='content';look='viewer'}
  E.pose=pose;if(fn!==E.FT.name)E.FT=face(fn);
  if(look)E.look=look;else if(E.look&&!(E.look.until>now))E.look=null;
  simulate(dt);draw(dt);requestAnimationFrame(frame)}
function needs(k){const moving=Math.hypot(E.vx,E.vy)>60,napping=act&&act.a.nap&&Math.hypot(E.tx-E.x,E.ty-E.y)<R;
  N.energy=cl(N.energy+(napping?.04:-(.003+(moving?.008:0)+(act&&act.a.errand&&act.stage==='do'?.012:0)))*k,0,1);
  N.curiosity=cl(N.curiosity+(act&&act.k==='watch'?-.06:.012)*k,0,1);
  N.social=cl(N.social+(cur.over||E.purr>0||cur.grab||CHAT.open?-.12:.01)*k,0,1);
  N.boredom=cl(N.boredom+(mood||SEQ||CHAT.open||(act&&(act.a.zoom||act.k==='watch'||act.a.errand))?-.09:.014)*k,0,1);
  N.annoy=cl(N.annoy-.02*k,0,1);N.fond=cl(N.fond+(E.purr>0?.03:0)*k-.001*k,0,1)}

/* ===== body physics ===== */
function simulate(dt){const rr=R*E.r;
  if(E.mode==='grab'){const nx=E.x+(cur.x-E.x)*Math.min(1,dt*20),ny=E.y+(cur.y+rr*.2-E.y)*Math.min(1,dt*20);E.vx=(nx-E.x)/dt;E.vy=(ny-E.y)/dt;E.x=nx;E.y=ny}
  else if(E.mode==='thrown'){const f=Math.pow(.2,dt);E.vx*=f;E.vy*=f;E.x+=E.vx*dt;E.y+=E.vy*dt;
    const lo=rr*1.1;if(E.x<lo){E.x=lo;E.vx=Math.abs(E.vx)*.72;hit(E.vx,'x')}if(E.x>W-lo){E.x=W-lo;E.vx=-Math.abs(E.vx)*.72;hit(E.vx,'x')}
    if(E.y<rr*2.2){E.y=rr*2.2;E.vy=Math.abs(E.vy)*.72;hit(E.vy,'y')}if(E.y>H-rr*1.1){E.y=H-rr*1.1;E.vy=-Math.abs(E.vy)*.72;hit(E.vy,'y')}
    if(Math.hypot(E.vx,E.vy)<80){E.mode='fly';E.tx=E.x;E.ty=E.y;afterThrow()}}
  else if(REDUCE){E.x+=(E.tx-E.x)*Math.min(1,dt*7);E.y+=(E.ty-E.y)*Math.min(1,dt*7);E.vx=E.vy=0}
  else if(E.antic>0){E.antic-=dt;E.vx*=.8;E.vy*=.8;E.x-=E.anticDir[0]*rr*.5*dt}
  else{const dx=E.tx-E.x,dy=E.ty-E.y;E.vx+=(dx*13-E.vx*6.2)*dt;E.vy+=(dy*13-E.vy*6.2)*dt;const max=460*E.speed,s=Math.hypot(E.vx,E.vy);if(s>max){E.vx*=max/s;E.vy*=max/s}
    if(E.spinOn)E.vy+=1200*dt;E.x+=E.vx*dt;E.y+=E.vy*dt;if(E.perch&&Math.hypot(dx,dy)<rr*.3&&!E.landed&&s<200){E.landed=1;E.q-=.3}}
  if(E.spinOn){E.spin+=dt*Math.PI*2*1.6;if(E.spin>=Math.PI*2){E.spin=0;E.spinOn=0}}
  const sp=Math.hypot(E.vx,E.vy);
  let qT=cl(-E.vy/2400,-.22,.24);if(E.antic>0)qT=-.2;if(E.perch&&E.landed)qT=-.07;
  if(E.nod>0){E.nod-=dt;qT+=Math.sin(now*15)*.11}if(E.dance>0){E.dance-=dt;qT+=Math.abs(Math.sin(now*8))*.17-.06}if(E.puff>0){E.puff-=dt;qT+=.12*Math.min(1,E.puff)}
  E.qv+=((qT-E.q)*170-E.qv*13)*dt;E.q+=E.qv*dt;
  let tT=cl(E.vx/1500,-.32,.32);if(E.antic>0)tT=-E.anticDir[0]*.16;tT+=E.gx*.06-E.F.chin*.14*Math.sign(E.gx||1);
  if(E.FT.name==='curious'||E.FT.name==='tfman')tT+=.12;
  if(E.shake>0){E.shake-=dt;tT+=Math.sin(now*17)*.16}if(E.dance>0)tT+=Math.sin(now*4)*.16;if(E.giggle>0){E.giggle-=dt;tT+=Math.sin(now*40)*.06}
  if(E.shakeOff>0){E.shakeOff-=dt;tT+=Math.sin(now*30)*.2*E.shakeOff}
  E.tv+=((tT-E.tilt)*110-E.tv*11)*dt;E.tilt+=E.tv*dt;
  const fl=REDUCE?0:(Math.sin(now*3.1)*.07+Math.sin(now*7.7)*.03);
  const tipXT=cl(-E.vx/700,-.7,.7)+fl-E.tilt*.3;E.tipXv+=((tipXT-E.tipX)*90-E.tipXv*9)*dt;E.tipX+=E.tipXv*dt;
  const ang=E.F.angry;const tipHT=1.85*E.F.flame*(1+cl(E.vy/1800,-.15,.25))+(REDUCE?0:Math.sin(now*(5.3+ang*9))*(.05+ang*.07));E.tipHv+=((tipHT-E.tipH)*80-E.tipHv*8)*dt;E.tipH+=E.tipHv*dt;
  for(let i=0;i<2;i++)E.side[i]+=((E.tipX*.7+(REDUCE?0:Math.sin(now*(4+i)+i*2)*.06))-E.side[i])*Math.min(1,dt*(4+i*2));
  E.r+=(E.rT-E.r)*Math.min(1,dt*5);E.alpha+=(E.alphaT-E.alpha)*Math.min(1,dt*5);
  const fk=Math.min(1,dt*(REDUCE?30:8));for(const k in F0)E.F[k]+=(E.FT[k]-E.F[k])*fk;
  let p=null;const L_=E.look;
  if(L_==='viewer'){E.gtx=0;E.gty=.05}else if(L_==='cursor'){if(cur.x>=0)p={x:cur.x,y:cur.y}}
  else if(L_&&L_.dir){E.gtx=L_.dir[0];E.gty=L_.dir[1]}else if(L_&&L_.pt){p=typeof L_.pt==='function'?L_.pt():L_.pt}
  else if(cur.active())p={x:cur.x,y:cur.y};else if(sp>120)p={x:E.x+E.vx,y:E.y+E.vy};else if(Math.random()<dt*.35){E.gtx=rnd(-.8,.8);E.gty=rnd(-.4,.5)}
  if(p){const dx=p.x-E.x,dy=p.y-E.y,d=Math.hypot(dx,dy)||1,m=Math.min(1,d/(R*4));E.gtx=dx/d*m;E.gty=dy/d*m}
  if(E.F.chin>.5)E.gty=Math.min(E.gty,-.55);
  if(Math.random()<dt*1.6)E.ms=[rnd(-.06,.06),rnd(-.05,.05)];
  const gk=Math.min(1,dt*(REDUCE?40:26));E.gx+=(E.gtx+E.ms[0]-E.gx)*gk;E.gy+=(E.gty+E.ms[1]-E.gy)*gk;
  if(E.noBlink>0)E.noBlink-=dt;
  E.nextBlink-=dt;if(Math.abs(E.gtx-E.gx)>.8&&E.blinkT<=0&&Math.random()<.5)E.nextBlink=0;
  if(E.nextBlink<=0&&!(E.noBlink>0)){E.blinkT=.16;E.nextBlink=rnd(2,5.5);if(Math.random()<.2)E.dbl=.3}
  if(E.dbl>0){E.dbl-=dt;if(E.dbl<=0)E.blinkT=.16}
  if(E.blinkT>0){E.blinkT-=dt;E.blink=Math.sin(Math.max(0,E.blinkT)/.16*Math.PI)}else E.blink=0;
  if(E.purr>0){E.purr-=dt;bump('social',-dt*.3);if(!REDUCE&&Math.random()<dt*6)parts.push({k:'heart',x:E.x+rnd(-.5,.5)*rr,y:E.y-rr*.8,vx:rnd(-15,15),vy:rnd(-60,-40),l:1,d:1.2,s:rnd(5,8)});
    if(E.purr<=0&&ATT()!=='sweet')play([{d:1.6,face:'calm',pose:'dust',look:'viewer',say:['','ok. enough.',"that's enough. I have standards."],fn:()=>{go({x:E.x+(E.x>W/2?-1:1)*R*4,y:E.y},{now:1})}}])}
  if(E.dizzy>0)E.dizzy-=dt;
  E.shadesOn=outfit()==='shades'||E.shadesUntil>now;
  E.shadesDown+=((['unimpressed','tfman','sideeye','glare','shocked'].includes(E.FT.name)?1:0)-E.shadesDown)*Math.min(1,dt*6);
  const ps=(POSES[E.pose]||POSES.rest)(now),c=Math.cos(E.tilt+E.spin),s=Math.sin(E.tilt+E.spin);
  for(let i=0;i<2;i++){const h=E.hands[i],o=ps[i],ox=o[0]*rr,oy=o[1]*rr,tx=E.x+ox*c-oy*s,ty=E.y+ox*s+oy*c;if(!h.init){h.x=tx;h.y=ty;h.init=1}
    const k=E.pose==='swat'?600:240,dm=E.pose==='swat'?30:20;if(REDUCE){h.x=tx;h.y=ty}else{h.vx+=((tx-h.x)*k-h.vx*dm)*dt;h.vy+=((ty-h.y)*k-h.vy*dm)*dt;h.x+=h.vx*dt;h.y+=h.vy*dt}}
  if(!REDUCE&&E.alpha>.5&&Math.random()<dt*(1.5+E.F.flame*2+ang*8))parts.push({k:'spark',x:E.x+E.tipX*.9*rr+rnd(-.15,.15)*rr,y:E.y-E.tipH*rr*.95,vx:rnd(-10,10)-E.vx*.05,vy:rnd(-50,-28)*(1+ang),l:1,d:rnd(.7,1.4),s:rnd(.6,1.2),c:ang>.5?'255,90,60':'255,180,90'});
  if(!REDUCE&&(ang>.5||E.puff>0)&&Math.random()<dt*5)parts.push({k:'smoke',x:E.x+E.tipX*rr+rnd(-.2,.2)*rr,y:E.y-E.tipH*rr,vx:rnd(-12,12),vy:rnd(-45,-25),l:1,d:1.3,s:rnd(4,7)});
  if(E.hum&&!REDUCE&&Math.random()<dt*1.8)parts.push({k:'note',x:E.x+rr*.9,y:E.y-rr*.4,vx:rnd(10,25),vy:-35,l:1,d:1.6,s:11});
  if(E.zz&&!REDUCE&&Math.random()<dt*1.2)parts.push({k:'z',x:E.x+rr*.6,y:E.y-rr*.5,vx:16,vy:-26,l:1,d:2,s:rnd(9,13)});
  if(E.bubble){const b=E.bubble;b.t+=dt;if(b.type){const i=Math.floor(b.t*b.cps);if(i!==b.last&&i<b.text.length){b.last=i;const ch=b.text[i]||'';E.flapT=/[aeiouyAEIOUY]/.test(ch)?.75:/[ ,.!?…]/.test(ch)?0:.35}}if(b.t>b.d&&!b.dots)E.bubble=null}
  E.flapT*=Math.pow(.002,dt);E.flap+=(E.flapT-E.flap)*Math.min(1,dt*28);E.hum=cur_('hum')?1:0}
function hit(v,ax){const s=Math.abs(v);if(ax==='y')E.q-=cl(s/2200,0,.5);else E.tv+=Math.sign(v)*s/180;if(s>700)E.dizzyHit=1}
function afterThrow(){const hard=E.thrown>900;E.thrown=0;if(E.dizzyHit){E.dizzyHit=0;E.dizzy=1.6;say('whoa…')}
  setTimeout(()=>{if(CHAT.open)return;if(ATT()==='sweet'){play([{d:1.8,face:'joy',pose:'cheer',look:'viewer',say:hard?'again! again!':'thanks!'}]);return}
    if(!hard)return;const again=Math.random()<.35;
    play([{d:.9,face:'glare',pose:'dust',fn:()=>{E.shakeOff=1}},{d:2,face:again?'mischief':'tfman',pose:'crossed',look:'viewer',say:again?['','…do it again.','okay that was sick. again.']:['','really?','the fuck was that?']}],()=>go(SP.rest))},E.dizzy>0?1700:100)}

/* ===== drawing ===== */
const ink='#3d1308';
function crPath(pts){const n=pts.length;X.beginPath();X.moveTo(pts[0][0],pts[0][1]);
  for(let i=0;i<n;i++){const p0=pts[(i-1+n)%n],p1=pts[i],p2=pts[(i+1)%n],p3=pts[(i+2)%n];X.bezierCurveTo(p1[0]+(p2[0]-p0[0])/6,p1[1]+(p2[1]-p0[1])/6,p2[0]-(p3[0]-p1[0])/6,p2[1]-(p3[1]-p1[1])/6,p2[0],p2[1])}X.closePath()}
function bodyPath(){const th=E.tipH,tx=E.tipX,sl=E.side[0],sr=E.side[1],h=th/1.85;
  crPath([[0,1.03],[.66,.86],[1.0,.32],[.98,-.28],[.9+sr*.4,-.98*h],[.56+sr*.15,-.62*h],[.36+tx*.5,-th*.62],[tx+.04,-th],[-.28+tx*.5,-th*.6],[-.5+sl*.15,-.7*h],[-.86+sl*.4,-1.08*h],[-1.0,-.3],[-1.0,.32],[-.66,.86]])}
function mixc(a,b,t){return a.map((v,i)=>Math.round(lerp(v,b[i],t)))}
function bodyGrad(b,ang){const g=X.createLinearGradient(0,1.05,0,-E.tipH),dim=[205,86,48];
  for(const [o,c0] of[[0,[255,205,64]],[.38,[255,160,52]],[.68,[255,104,72]],[1,[255,72,104]]]){let c=c0;if(ang>0)c=mixc(c,o>.4?[255,40,30]:[255,140,40],ang*.6);if(b<1)c=mixc(c,dim,Math.min(1,(1-b)*1.7));else c=c.map(v=>Math.min(255,Math.round(v*b)));g.addColorStop(o,`rgb(${c.join(',')})`)}return g}
const FONT='-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif';
function draw(dt){X.setTransform(DPR,0,0,DPR,0,0);X.clearRect(0,0,W,H);const rr=R*E.r,F=E.F;
  if(TW.on&&TW.el){const s=rect(TW.el)||TW.spot;TW.spot=s;if(s){const p=8;X.save();X.fillStyle='rgba(8,6,5,.5)';X.fillRect(0,0,W,H);
    X.globalCompositeOperation='destination-out';X.beginPath();X.roundRect(s.l-p,s.t-p,s.w+2*p,s.h+2*p,14);X.fill();X.globalCompositeOperation='source-over';
    X.strokeStyle=`rgba(232,135,95,${(.55+.35*Math.sin(now*4)).toFixed(3)})`;X.lineWidth=2;X.beginPath();X.roundRect(s.l-p,s.t-p,s.w+2*p,s.h+2*p,14);X.stroke();X.restore()}}
  for(let i=parts.length-1;i>=0;i--){const p=parts[i];p.l-=dt/p.d;if(p.l<=0){parts.splice(i,1);continue}p.vy+=(p.k==='spark'?40:-6)*dt;p.vx*=.985;p.x+=p.vx*dt;p.y+=p.vy*dt;
    if(p.k==='spark'){X.globalCompositeOperation='lighter';const g=X.createRadialGradient(p.x,p.y,0,p.x,p.y,p.s*3);g.addColorStop(0,`rgba(${p.c},${p.l})`);g.addColorStop(1,`rgba(${p.c},0)`);X.fillStyle=g;X.beginPath();X.arc(p.x,p.y,p.s*3,0,7);X.fill();X.globalCompositeOperation='source-over'}
    else if(p.k==='smoke'){X.fillStyle=`rgba(120,110,110,${.35*p.l})`;X.beginPath();X.arc(p.x,p.y,p.s*(2-p.l),0,7);X.fill()}
    else if(p.k==='heart'){X.fillStyle=`rgba(255,120,150,${p.l})`;const s=p.s;X.beginPath();X.moveTo(p.x,p.y+s*.35);X.bezierCurveTo(p.x-s*.7,p.y-s*.2,p.x-s*.35,p.y-s*.8,p.x,p.y-s*.4);X.bezierCurveTo(p.x+s*.35,p.y-s*.8,p.x+s*.7,p.y-s*.2,p.x,p.y+s*.35);X.fill()}
    else{X.fillStyle=`rgba(255,225,200,${Math.sin(p.l*Math.PI)})`;X.font=`600 ${p.s}px ${FONT}`;X.fillText(p.k==='z'?'z':'♪',p.x,p.y)}}
  if((V.state==='listening'||V.state==='asking')&&E.alpha>.3){const k0=REDUCE?0:now*.9;for(let k=0;k<3;k++){const ph=REDUCE?k/3:(k0+k/3)%1;X.strokeStyle=`rgba(255,165,95,${(.5*(1-ph)).toFixed(3)})`;X.lineWidth=2;X.beginPath();X.arc(E.x,E.y-rr*.25,rr*(1.25+ph*1.6),0,7);X.stroke()}}
  if(E.alpha<.03||rr<1.5)return;X.globalAlpha=E.alpha;
  const glowC=F.angry>.5?'255,60,40':'255,120,50';
  const lg=X.createRadialGradient(E.x,E.y,0,E.x,E.y,rr*6);lg.addColorStop(0,`rgba(${glowC},${.12*F.bright})`);lg.addColorStop(1,`rgba(${glowC},0)`);X.fillStyle=lg;X.fillRect(E.x-rr*6,E.y-rr*6,rr*12,rr*12);
  const bob=REDUCE?0:(E.mode==='fly'&&!E.perch?Math.sin(now*2.2)*rr*.06:0),br=REDUCE?0:Math.sin(now*2.1)*.022,by=E.y+bob,ca=Math.cos(E.tilt+E.spin),sa=Math.sin(E.tilt+E.spin);
  for(let i=0;i<2;i++){const s=i?1:-1,h=E.hands[i],shx=s*.84*rr,shy=.32*rr,sx=E.x+shx*ca-shy*sa,sy=by+shx*sa+shy*ca;
    const mx=(sx+h.x)/2,my=(sy+h.y)/2,dx=h.x-sx,dy=h.y-sy,d=Math.hypot(dx,dy)||1,bend=Math.min(rr*.5,d*.3)*s,cx=mx-dy/d*bend*.6,cy=my+dx/d*bend*.6;
    X.lineCap='round';X.strokeStyle='#f0703c';X.lineWidth=rr*.19;X.beginPath();X.moveTo(sx,sy);X.quadraticCurveTo(cx,cy,h.x,h.y);X.stroke();
    X.strokeStyle='rgba(255,200,130,.55)';X.lineWidth=rr*.06;X.beginPath();X.moveTo(sx-1,sy-1.5);X.quadraticCurveTo(cx-1,cy-1.5,h.x-1,h.y-1.5);X.stroke()}
  if(E.perch&&E.landed){X.fillStyle='rgba(0,0,0,.3)';X.beginPath();X.ellipse(E.x,E.y+rr*1.05,rr*.8,rr*.14,0,0,7);X.fill();const sw=E.F.mc>.3?9:3.2,amp=E.F.mc>.3?.32:.16;
    for(const s of[-1,1]){const a=Math.sin(now*sw+(s>0?Math.PI:0))*amp;X.save();X.translate(E.x+s*rr*.34,E.y+rr*.98);X.rotate(a);X.strokeStyle='#e8662e';X.lineWidth=rr*.12;X.lineCap='round';X.beginPath();X.moveTo(0,0);X.lineTo(0,rr*.42);X.stroke();
      X.fillStyle='#ffb94a';X.beginPath();X.ellipse(s*rr*.06,rr*.5,rr*.17,rr*.1,0,0,7);X.fill();X.restore()}}
  X.save();X.translate(E.x,by);X.rotate(E.tilt+E.spin);X.scale(rr*(1-E.q*.5)*(1+br),rr*(1+E.q)*(1-br));
  const G=bodyGrad(F.bright,F.angry);X.shadowColor=`rgba(${glowC},${.6*F.bright})`;X.shadowBlur=rr*.8;bodyPath();X.fillStyle=G;X.fill();X.shadowBlur=0;
  X.save();bodyPath();X.clip();
  const vs=X.createRadialGradient(-.25,-.1,.3,0,0,1.6);vs.addColorStop(0,'rgba(120,10,30,0)');vs.addColorStop(1,'rgba(150,20,40,.32)');X.fillStyle=vs;X.fillRect(-1.3,-2.5,2.6,3.7);
  const ig=X.createRadialGradient(-.05,.45,0,0,.35,1);ig.addColorStop(0,'rgba(255,248,210,.45)');ig.addColorStop(1,'rgba(255,248,210,0)');X.fillStyle=ig;X.fillRect(-1.2,-1,2.4,2.2);
  const hl=X.createRadialGradient(-.5,-.35,0,-.5,-.35,.55);hl.addColorStop(0,'rgba(255,255,255,.5)');hl.addColorStop(1,'rgba(255,255,255,0)');X.fillStyle=hl;X.fillRect(-1.1,-1,1.2,1.3);X.restore();
  const fx=E.gx*.1,fy=E.gy*.07;
  if(F.blush>.02)for(const s of[-1,1]){const g=X.createRadialGradient(s*.6+fx,.5+fy,0,s*.6+fx,.5+fy,.22);g.addColorStop(0,`rgba(255,80,110,${.5*F.blush})`);g.addColorStop(1,'rgba(255,80,110,0)');X.fillStyle=g;X.beginPath();X.arc(s*.6+fx,.5+fy,.22,0,7);X.fill()}
  for(const s of[-1,1])eye(s,fx,fy,G);brows(fx,fy);mouth(fx*.8,.66+fy*.8);
  if(F.angry>.4){X.strokeStyle=`rgba(140,20,20,${(F.angry-.4)*1.4})`;X.lineWidth=.05;X.lineCap='round';const vx=.55+fx,vy=-.62+fy;for(const [a,b,c,d] of[[-.06,-.06,.06,.06],[.06,-.06,-.06,.06],[0,-.09,0,.09],[-.09,0,.09,0]]){X.beginPath();X.moveTo(vx+a,vy+b);X.lineTo(vx+c*.4,vy+d*.4);X.stroke()}}
  if(E.shadesOn)shades(fx,fy);hat();X.restore();
  if(E.sweat){const sx=E.x+rr*.95,sy=E.y-rr*.25+Math.sin(now*3)*2;X.fillStyle='rgba(200,232,255,.95)';X.beginPath();X.moveTo(sx,sy-rr*.24);X.bezierCurveTo(sx+rr*.16,sy,sx+rr*.16,sy+rr*.16,sx,sy+rr*.16);X.bezierCurveTo(sx-rr*.16,sy+rr*.16,sx-rr*.16,sy,sx,sy-rr*.24);X.fill()}
  for(let i=0;i<2;i++){const h=E.hands[i],s=i?1:-1;X.save();X.translate(h.x,h.y);X.scale(rr,rr);const g=X.createRadialGradient(-.06,-.08,0,0,0,.28);g.addColorStop(0,'#ffe9b0');g.addColorStop(.6,'#ffad50');g.addColorStop(1,'#f0703c');X.fillStyle=g;
    X.beginPath();X.ellipse(0,0,.22,.2,0,0,7);X.fill();X.beginPath();X.arc(-s*.19,-.1,.09,0,7);X.fill();X.restore()}
  if(E.think&&!REDUCE){for(let i=0;i<3;i++){const ph=(Math.sin((now*2.4+i*.55)*2)+1)/2;X.fillStyle=`rgba(255,232,210,${.35+.55*ph})`;X.beginPath();X.arc(E.x-rr*1.2-i*rr*.42,E.y-rr*1.5-i*rr*.38,rr*(.09+i*.04),0,7);X.fill()}}
  X.globalAlpha=1;if(E.bubble)bubble(rr)}
function eye(s,fx,fy,G){const F=E.F;
  if(F.name==='dizzy'&&E.FT.name==='dizzy'){const cx=s*.35+fx,cy=.2+fy;X.strokeStyle=ink;X.lineWidth=.06;X.lineCap='round';X.beginPath();for(let i=0;i<44;i++){const t=i/44*Math.PI*4+now*8*s,r=.015+i*.0042;i?X.lineTo(cx+Math.cos(t)*r,cy+Math.sin(t)*r):X.moveTo(cx,cy)}X.stroke();return}
  const cx=s*.36+fx,cy=.18+fy,rx=.235*F.eyeS,ry=.27*F.eyeS;X.save();X.beginPath();X.ellipse(cx,cy,rx,ry,0,0,7);X.clip();
  const ew=X.createRadialGradient(cx-.05,cy-.08,0,cx,cy,ry*1.2);ew.addColorStop(0,'#ffffff');ew.addColorStop(.75,'#f6f1ee');ew.addColorStop(1,'#d9cfd6');X.fillStyle=ew;X.fill();
  const px=cx+E.gx*.09,py=cy+E.gy*.09,pr=.135*F.pup;const ir=X.createRadialGradient(px,py-.03,0,px,py,pr);ir.addColorStop(0,'#6b3a24');ir.addColorStop(.7,'#341509');ir.addColorStop(1,'#1c0a04');
  X.fillStyle=ir;X.beginPath();X.arc(px,py,pr,0,7);X.fill();X.fillStyle='#0e0503';X.beginPath();X.arc(px,py,pr*.52,0,7);X.fill();
  X.fillStyle='rgba(255,255,255,.95)';X.beginPath();X.arc(px-pr*.38,py-pr*.48,pr*.34,0,7);X.fill();X.fillStyle='rgba(255,255,255,.6)';X.beginPath();X.arc(px+pr*.34,py+pr*.38,pr*.15,0,7);X.fill();
  const u=Math.max(F.lidU,E.blink),ty=cy-ry+2*ry*u,tilt=F.tilt*.075,curve=.06*(1-u)*(1-F.lf),yo=ty+tilt,yi=ty-tilt,xo=cx+s*(rx+.03),xi=cx-s*(rx+.03);
  X.fillStyle=G;X.beginPath();X.moveTo(xi,cy-ry-.05);X.lineTo(xo,cy-ry-.05);X.lineTo(xo,yo);X.quadraticCurveTo(cx,(yo+yi)/2+curve,xi,yi);X.closePath();X.fill();
  const l=F.lidL*(1-E.blink*.5),ly=cy+ry-2*ry*l*.62;X.beginPath();X.moveTo(xi,cy+ry+.05);X.lineTo(xo,cy+ry+.05);X.lineTo(xo,ly);X.quadraticCurveTo(cx,ly-.07*l*(1-F.lf*.8),xi,ly);X.closePath();X.fill();X.restore();
  X.strokeStyle='rgba(70,18,8,.8)';X.lineWidth=.045;X.lineCap='round';X.beginPath();const yo2=Math.min(yo,cy+ry),yi2=Math.min(yi,cy+ry);X.moveTo(xi+s*.02,yi2);X.quadraticCurveTo(cx,(yo2+yi2)/2+curve,xo-s*.02,yo2);
  if(u>.12)X.stroke();else{X.globalAlpha*=.35;X.stroke();X.globalAlpha=E.alpha}
  if(F.lidL>.12&&F.lf>.5){X.strokeStyle='rgba(70,18,8,.45)';X.lineWidth=.03;X.beginPath();X.moveTo(xi+s*.03,ly);X.lineTo(xo-s*.03,ly);X.stroke()}}
function brows(fx,fy){const F=E.F;X.strokeStyle='rgba(110,28,14,.85)';X.lineWidth=.08;X.lineCap='round';
  for(const s of[-1,1]){const raise=(s<0?F.bL:F.bR)+F.bH,cy=-.2+fy-raise*.6-(F.eyeS-1)*.25,cx=s*.36+fx,xi=cx-s*.14,xo=cx+s*.16,yi=cy+F.bT*.1,yo=cy-F.bT*.06;X.beginPath();X.moveTo(xi,yi);X.quadraticCurveTo(cx,Math.min(yi,yo)-.06*(1-F.lf*.7),xo,yo);X.stroke()}}
function mouth(mx,my){const F=E.F,fl=E.flap,w=F.mw*(1-fl*.25),c=F.mc*(1-fl*.5),o=Math.max(F.mo,fl*.75),as=F.mas*.05,lx=mx-w,rx=mx+w,ly=my-c*.08+as,ry=my-c*.08-as;
  X.strokeStyle=ink;X.fillStyle=ink;X.lineWidth=.06;X.lineCap='round';X.lineJoin='round';
  if(F.mwob>.5&&o<.15){X.beginPath();X.moveTo(lx,ly+.03);for(let i=1;i<=6;i++)X.lineTo(lx+i*(2*w/6),my+.03+(i%2?-.025:.025));X.stroke();return}
  if(o<.06){X.beginPath();X.moveTo(lx,ly);X.quadraticCurveTo(mx,my+c*.12,rx,ry);X.stroke();return}
  X.beginPath();X.moveTo(lx,ly);X.quadraticCurveTo(mx,my+c*.09-o*.03,rx,ry);X.quadraticCurveTo(mx,my+c*.1+o*.25,lx,ly);X.closePath();X.fill();
  if(o>.25){X.save();X.clip();X.fillStyle='#ff7d70';X.beginPath();X.ellipse(mx,my+c*.1+o*.2,w*.55,o*.12,0,0,7);X.fill();X.restore()}}
function shades(fx,fy){const d=E.shadesDown*.2,y=.16+fy+d,x0=fx;X.save();X.translate(x0,y);X.scale(1.15,1.15);X.translate(-x0,-y);X.strokeStyle='#17151a';X.lineWidth=.07;X.lineJoin='round';
  for(const s of[-1,1]){const cx=s*.37+x0;X.fillStyle='#17151a';X.beginPath();X.moveTo(cx-.27,y-.17);X.lineTo(cx+.27,y-.17);X.quadraticCurveTo(cx+.26,y+.17,cx+.1,y+.2);X.lineTo(cx-.1,y+.2);X.quadraticCurveTo(cx-.26,y+.17,cx-.27,y-.17);X.closePath();X.fill();
    X.fillStyle='rgba(255,255,255,.18)';X.beginPath();X.moveTo(cx-.18,y-.1);X.lineTo(cx-.05,y-.1);X.lineTo(cx-.15,y+.08);X.closePath();X.fill()}
  X.beginPath();X.moveTo(-.1+x0,y-.12);X.quadraticCurveTo(x0,y-.17,.1+x0,y-.12);X.stroke();X.beginPath();X.moveTo(-.64+x0,y-.14);X.lineTo(-.95,y-.2);X.moveTo(.64+x0,y-.14);X.lineTo(.95,y-.2);X.stroke();X.restore()}
function hat(){const o=outfit();if(o==='none'||o==='shades')return;const tx=E.tipX,th=E.tipH;X.save();X.translate(tx*.72,-th*.6);X.rotate(Math.atan2(tx,th)*.9);X.scale(1.12,1.12);
  if(o==='santa'){X.fillStyle='#d8262f';X.beginPath();X.moveTo(-.52,0);X.quadraticCurveTo(-.2,-.95,.25,-1.05);X.quadraticCurveTo(.7,-1.0+Math.sin(now*3)*.04,.78,-.62);X.quadraticCurveTo(.45,-.55,.52,0);X.closePath();X.fill();
    X.fillStyle='#fff7f0';X.beginPath();X.roundRect(-.6,-.06,1.2,.22,.11);X.fill();X.beginPath();X.arc(.8,-.58,.13,0,7);X.fill()}
  else if(o==='witch'){X.fillStyle='#2b1f3a';X.beginPath();X.ellipse(0,0,.82,.16,0,0,7);X.fill();X.beginPath();X.moveTo(-.42,0);X.quadraticCurveTo(-.1,-.7,.3,-1.25);X.quadraticCurveTo(.08,-.6,.42,0);X.closePath();X.fill();X.fillStyle='#ff8a2a';X.fillRect(-.4,-.18,.8,.12)}
  else if(o==='party'){X.save();X.beginPath();X.moveTo(-.4,0);X.lineTo(0,-1.1);X.lineTo(.4,0);X.closePath();X.clip();X.fillStyle='#6a5cff';X.fillRect(-.5,-1.2,1,1.3);X.fillStyle='#ffd23f';for(let i=0;i<5;i++){X.beginPath();X.moveTo(-.6,-.95+i*.26);X.lineTo(.6,-1.25+i*.26);X.lineTo(.6,-1.13+i*.26);X.lineTo(-.6,-.83+i*.26);X.fill()}X.restore();X.fillStyle='#ff4f8b';X.beginPath();X.arc(0,-1.12,.12,0,7);X.fill()}
  else if(o==='beanie'){X.fillStyle='#3d6bff';X.beginPath();X.moveTo(-.62,.05);X.quadraticCurveTo(-.62,-.72,0,-.74);X.quadraticCurveTo(.62,-.72,.62,.05);X.closePath();X.fill();X.fillStyle='#2a4fd0';X.beginPath();X.roundRect(-.66,-.08,1.32,.22,.1);X.fill();X.fillStyle='#fff';X.beginPath();X.arc(0,-.78,.13,0,7);X.fill()}
  X.restore()}
function wrap(t,max){const w=t.split(' '),out=[];let line='';for(const x of w){const tr=line?line+' '+x:x;if(X.measureText(tr).width>max&&line){out.push(line);line=x}else line=tr}if(line)out.push(line);return out}
function bubble(rr){const b=E.bubble;if(!b.text&&!b.dots)return;const a=Math.min(1,b.t/.15,(b.d-b.t)/.25);if(a<=0)return;X.font=`600 12.5px ${FONT}`;
  const shown=b.type?Math.min(b.text.length,Math.floor(b.t*b.cps)):b.text.length,lines=b.dots?['   ']:wrap(b.text,220);let tw=0;for(const l of lines)tw=Math.max(tw,X.measureText(l).width);
  const pw=Math.max(tw,b.dots?26:0)+22,ph=lines.length*17+10,bx=cl(E.x-pw/2+rr*.4,8,W-pw-8);let by=E.y-rr*E.tipH-ph-14-(outfit()!=='none'&&outfit()!=='shades'?rr*.6:0);if(by<8)by=E.y+rr*1.6;by+=(1-Math.min(1,b.t/.2))*6;b.box={x:bx,y:by,w:pw,h:ph};
  X.globalAlpha=a;X.fillStyle='rgba(255,247,238,.97)';X.beginPath();X.roundRect(bx,by,pw,ph,13);X.fill();const tx=cl(E.x,bx+14,bx+pw-14);X.beginPath();
  if(by<E.y){X.moveTo(tx-6,by+ph-1);X.lineTo(tx,by+ph+7);X.lineTo(tx+6,by+ph-1)}else{X.moveTo(tx-6,by+1);X.lineTo(tx,by-7);X.lineTo(tx+6,by+1)}X.fill();X.fillStyle='#2b160c';X.textBaseline='middle';
  if(b.dots){for(let i=0;i<3;i++){X.globalAlpha=a*(.35+.65*Math.max(0,Math.sin(now*6-i*.9)));X.beginPath();X.arc(bx+pw/2-9+i*9,by+ph/2,2.6,0,7);X.fill()}X.globalAlpha=1;return}
  let left=shown;lines.forEach((l,i)=>{X.fillText(l.slice(0,Math.max(0,left)),bx+11,by+13.5+i*17);left-=l.length+1});X.globalAlpha=1}

/* ===== on / off ===== */
function resize(){W=innerWidth;H=innerHeight;DPR=Math.min(2,devicePixelRatio||1);cv.width=Math.round(W*DPR);cv.height=Math.round(H*DPR);R=Math.max(16,Math.min(26,Math.min(W,H)*.024))}
function start(){if(running)return;if(!started){started=true;(document.head||document.documentElement).append(css);document.body.append(cv,talk,menu);tin=talk.querySelector('input');
    talk.querySelector('#ember-go').onclick=send;talk.querySelector('#ember-mic').onclick=()=>{closeChat();listen()};tin.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();send()}if(e.key==='Escape')closeChat(L(['okay, later!','fine.','rude.']))});
    tin.addEventListener('input',()=>{CHAT.last=now;CHAT.typing=now;talk.classList.toggle('ae-empty',!tin.value.trim())});
    addEventListener('pointermove',onMove,true);addEventListener('pointerdown',onDown,true);addEventListener('pointerup',onUp,true);
    for(const t of['click','dblclick','mousedown','mouseup','contextmenu'])addEventListener(t,e=>{if(t==='dblclick'&&onBody(e.clientX,e.clientY)){e.preventDefault();e.stopPropagation();listen();return}if(t==='contextmenu'&&onBody(e.clientX,e.clientY)){e.preventDefault();e.stopPropagation();return}if(t!=='contextmenu')swallow(e)},true);
    addEventListener('resize',resize);addEventListener('blur',()=>{cur.x=-1});
    document.addEventListener('input',e=>{if(e.target&&e.target.closest&&e.target.closest('.composer')&&e.target!==tin)signal('typing')},true);
    let hiddenAt=0;document.addEventListener('visibilitychange',()=>{if(document.hidden)hiddenAt=Date.now();else if(hiddenAt&&Date.now()-hiddenAt>20*60*1000)signal('back')})}
  resize();running=true;cv.hidden=false;E.alpha=0;E.alphaT=1;const s=SP.rest();E.x=s.x+R*3;E.y=s.y-R*4;E.tx=s.x;E.ty=s.y;last=performance.now();
  if(willWelcome())setTimeout(()=>welcome(),1200);
  else play([{d:1.1,look:{dir:[-1,.3]},face:'sideeye'},{d:1.6,look:'viewer',pose:ATT()==='sweet'?'wave':'crossed',face:ATT()==='sweet'?'joy':'calm',say:['oh, hi!','oh. you.','sup.']}]);
  requestAnimationFrame(frame)}
function stop(){running=false;cv.hidden=true;talk.hidden=true;menu.hidden=true;CHAT.open=0;document.documentElement.classList.remove('ember-over','ember-grab');if(started)X.clearRect(0,0,cv.width,cv.height)}
function set(o){Object.assign(CFG,o||{});if(CFG.att==='savage'&&!isAdmin())CFG.att='sassy';store.set('ember_cfg',CFG);
  if(DESK){start();return}
  if(CFG.on&&!CFG.desktop&&!automation)start();else stop()}
/* desktop mode: the in-app Ember steps aside and the app's signals go to the one on the desktop */
async function setDesktop(on){const r=await post('/api/ember/desktop',{on:!!on});CFG.desktop=!!r.on;store.set('ember_cfg',CFG);set({});return r}
window.ember={signal,fromEvents,set,desk,setDesktop,get:()=>Object.assign({},CFG,{savage:isAdmin(),running,desk:DESK}),talk:()=>startTalk(),listen:()=>listen(),welcome:f=>welcome(f),willWelcome:()=>willWelcome(),tour:()=>({on:!!TW.on,step:TW.n?(TW.bar&&TW.bar.querySelector('.n').textContent):''}),hear:t=>heard(String(t||'')),perform:r=>perform(r),voice:()=>({state:V.state,text:V.text,driving:!!DRV.on,press:DRV.press||null}),
  forget(){CHAT.mem={name:'',facts:[]};saveMem()},memory:()=>JSON.parse(JSON.stringify(CHAT.mem)),
  state:()=>({face:E.FT.name,pose:E.pose,bubble:E.bubble?E.bubble.text:'',x:E.x,y:E.y,r:R*E.r,mood:mood?(mood.kind||'on'):'',chat:!!CHAT.open,alpha:E.alpha,running,act:act?act.k:'',stage:act&&act.stage||'',seen:Object.assign({},SEEN)}),
  hurry:()=>{N.energy=.8;N.boredom=.4;fidgetIn=60;if(act)act.t=act.dur+1}};
function boot(){if(DESK){document.documentElement.style.background='transparent';document.body.style.background='transparent';start();return}
  fetch('/api/ember/desktop').then(r=>r.json()).then(d=>{CFG.desktop=!!(d&&d.on);CFG.deskAvail=!!(d&&d.available);if(CFG.on&&!CFG.desktop&&!automation)start()}).catch(()=>{if(CFG.on&&!automation)start()})}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
"""


def _selftest() -> int:
    ok = fails = 0

    def check(name, cond):
        nonlocal ok, fails
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fails += 1
            print(f"  FAIL {name}")

    got = {}

    def fake(prompt):
        got["p"] = prompt
        return 'sure. {"say": "huh. go on.", "face": "sideeye", "gesture": "nod", "mood": 0.2, "name": null, "remember": "builds sites for cafes"}'

    r = reply([{"role": "assistant", "content": "what are you doing"},
               {"role": "user", "content": "building a cafe site"}],
              "No project is open.", "sassy", call=fake)
    check("a reply is read from the model's JSON", r["say"] == "huh. go on." and r["face"] == "sideeye")
    check("a fact worth remembering comes back", r["remember"] == "builds sites for cafes")
    check("the prompt carries the voice, the state and the conversation",
          "cat in flame form" in got["p"] and "No project is open." in got["p"] and "You: building a cafe site" in got["p"])
    os.environ.pop("AETHRON_EMBER_SAVAGE", None)
    sys.modules.pop("aethron_control", None)
    r2 = reply([{"role": "user", "content": "hi"}], "", "savage", call=fake)
    check("savage is refused for someone who is not an admin", r2["attitude"] == "sassy" and "swears freely" not in got["p"])
    os.environ["AETHRON_EMBER_SAVAGE"] = "1"
    r3 = reply([{"role": "user", "content": "hi"}], "", "savage", call=fake)
    check("savage is allowed for an admin", r3["attitude"] == "savage" and "swears freely" in got["p"])
    os.environ.pop("AETHRON_EMBER_SAVAGE", None)
    bad = parse('{"say": "hey", "face": "evil", "gesture": "explode", "mood": 9}')
    check("an unknown face or gesture falls back, mood is clamped",
          bad["face"] == "calm" and bad["gesture"] == "none" and bad["mood"] == 1.0)
    try:
        reply([{"role": "assistant", "content": "hi"}], "", "sassy", call=fake)
        check("a conversation that does not end with the person is refused", False)
    except ValueError:
        check("a conversation that does not end with the person is refused", True)
    try:
        parse("no json here")
        check("a reply with no JSON is refused", False)
    except ValueError:
        check("a reply with no JSON is refused", True)
    a = act("um can you publish it please", "", "sassy", call=lambda p: 1/0)
    check("a spoken button press needs no model", a["kind"] == "action" and a["action"] == "publish" and a["by"] == "local")
    check("'how far is it' is the status button", act("how far is it", "", "sassy", call=lambda p: 1/0)["action"] == "status")
    a = act("uh make the hero um bigger", "", "sassy", call=lambda p: 1/0)
    check("with no provider the words are sent as said, minus filler",
          a["kind"] == "send" and a["prompt"] == "Make the hero bigger." and a["by"] == "local")
    a = act("change headline to cats rule", "Open project: cafe.", "sassy",
            call=lambda p: got.__setitem__("a", p) or '{"kind":"send","prompt":"Change the hero headline to \\"Cats rule\\".","action":"none","say":"on it."}')
    check("the model's rewrite is what gets sent", a["kind"] == "send" and a["prompt"].startswith("Change the hero headline") and a["by"] == "model")
    check("the rewrite prompt carries the state and forbids adding things",
          "Open project: cafe." in got["a"] and "NEVER add" in got["a"] and "cats rule" in got["a"])
    a = act("hey ember how are you", "", "sassy", call=lambda p: '{"kind":"answer","say":"fine. why."}')
    check("small talk is answered, not sent", a["kind"] == "answer" and a["prompt"] == "" and a["say"] == "fine. why.")
    a = act("fix the thing", "", "sassy", call=lambda p: '{"kind":"action","action":"explode"}')
    check("an unknown action is never pressed", a["kind"] == "send" and a["action"] == "none")
    check("a broken model reply falls back to the words as said", act("make it blue", "", "sassy", call=lambda p: "nope")["prompt"] == "Make it blue.")
    P = ["azul-salon-ce08c5", "bloop-ai", "chaka", "chatora-city-567d98", "proof-check"]
    a = act("can he open chaka's project", "", "sassy", call=lambda p: 1/0, projects=P)
    check("'open Chaka's project' presses the project, it is not typed",
          a["kind"] == "action" and a["action"] == "open_project" and a["target"] == "chaka" and a["by"] == "local")
    check("a spoken name with spaces finds the project id", act("open chatora city", "", "sassy", call=lambda p: 1/0, projects=P)["target"] == "chatora-city-567d98")
    a = act("open the checker project", "", "sassy", projects=P,
            call=lambda p: got.__setitem__("o", p) or '{"kind":"action","action":"open_project","target":"proof-check","say":"there."}')
    check("a misheard name goes to the model, which is given the project list",
          a["target"] == "proof-check" and "proof-check" in got["o"])
    a = act("open the banana project", "", "sassy", projects=P, call=lambda p: '{"kind":"action","action":"open_project","target":"banana"}')
    check("a project nobody has is never opened", a["kind"] == "answer" and "banana" in a["say"])
    check("settings, home, a new chat and scrolling are its own buttons",
          [act(t, "", "sassy", call=lambda p: 1/0)["action"] for t in ("go to settings", "go home", "start a new chat", "scroll down")]
          == ["settings", "home", "new_chat", "scroll_down"])
    check("the companion script has no triple quote that would end this module", '"""' not in EMBER_JS)
    check("the companion stays off in automated browsers unless asked", "navigator.webdriver&&!forced" in EMBER_JS)
    print(f"\n{ok} passed, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
