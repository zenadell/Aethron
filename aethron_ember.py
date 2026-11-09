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
    cfg = get_settings()
    name = cfg.get("name") or "Ember"
    pers = f"Personality: {cfg.get('personality')}\n{VOICE[att]}" if cfg.get("personality") and cfg.get("personality") != DEFAULT_SETTINGS["personality"] else VOICE[att]
    cust = f"\nCustom Guidelines:\n{cfg.get('system_instruction').strip()}\n" if cfg.get("system_instruction") and cfg.get("system_instruction").strip() else ""
    return (
        f"You are {name}, a tiny living flame who keeps a person company inside "
        "Aethron, a desktop app that turns website templates into their own "
        "sites and checks every page in a real browser.\n"
        f"{pers}\n"
        f"{cust}"
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
        "IMPORTANT RULE: Whenever the person asks, tells, or dares you to do ANY "
        "physical movement, trick, expression, posture, emotion, or roleplay "
        "(dance, moonwalk, bow, flip, shiver, sulk in a corner, wink, wave, shrug, "
        "change outfit/glasses, move somewhere, sit still), you may grumble in \"say\", "
        "but you MUST perform it by including a non-empty \"plan\" array:\n"
        "  {\"steps\": [keyframes]} where a keyframe is {\"t\": 0.2..6, \"face\": \"<face>\", \"fx\": {dials}, "
        "\"arms\": {...}, \"body\": {...}, \"osc\": [...], \"speed\": .., \"look\": \"viewer|cursor|away|up|down\", "
        "\"hop\": 0..1, \"spin\": true, \"shake\": 0..1.5, \"burst\": {\"kind\":\"spark|heart|note|smoke\",\"n\":8}, \"say\": \"..\"}, "
        "{\"go\": \"rest|perch|chat|send|projects|top|corner|cursor|message|settings|wander|center|approach|away|left|right|bottom\" or [x,y] in 0.05..0.95}, "
        "{\"glasses\": \"on|off|peek\"}, {\"outfit\": \"none|shades|beanie|santa|party|witch\", \"for\": minutes}, "
        "{\"wait\": seconds, \"look\": \"..\"}, {\"sleep\": seconds}, {\"feel\": {\"valence\": -1..1, \"arousal\": 0..1}}, "
        "and optional \"new\": [invented behaviours].\n"
        + BODY_DOC +

        "Reply with ONLY one JSON object: {\"say\": string, \"face\": one of "
        f"{json.dumps(FACES)}, \"gesture\": one of {json.dumps(GESTURES)}, "
        "\"mood\": number from -1 (they annoyed you) to 1 (they charmed you), "
        "\"name\": string or null (their name, only if they just told you), "
        "\"remember\": string or null (one short new fact about them, under "
        "60 characters, else null), \"plan\": [physical/movement/keyframe steps]}")


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


def _top_face(v) -> str:
    if v in FACES:
        return v
    f = _face_of(v)[0] if isinstance(v, str) else None
    return f if f in FACES else "calm"


def parse(text: str, user_text: str = "") -> dict:
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
           "face": _top_face(d.get("face")),
           "gesture": d.get("gesture") if d.get("gesture") in GESTURES else "none"}
    try:
        out["mood"] = max(-1.0, min(1.0, float(d.get("mood"))))
    except (TypeError, ValueError):
        out["mood"] = 0.0
    for k, n in (("name", 30), ("remember", 60)):
        v = d.get(k)
        out[k] = str(v).strip()[:n] if isinstance(v, str) and v.strip() else None
    plan, new = _plan_from_dict(d)
    w = wish_local(user_text) if user_text else None
    if w and w.get("plan"):
        have = {list(x)[0] for x in plan if isinstance(x, dict) and x}
        has_motion = bool(have & {"steps", "do"})
        for st in w["plan"]:
            k0 = list(st)[0]
            if k0 not in have and not (k0 in ("steps", "do") and has_motion):
                plan.append(st)
    if plan:
        out["plan"] = plan[:MIND_MAX_STEPS]
    if new:
        out["new"] = new
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
    last_user = lines[-1][5:]
    prompt = (rules(att) + "\n\nSTATE:\n" + str(state or "")[:2000] +
              "\n\nCONVERSATION (oldest first):\n" + "\n".join(lines) +
              "\n\nNow reply as Ember to the last message, as the JSON object only.")
    if call is None:
        import aethron_brain
        call = lambda p: aethron_brain.text_call(p, timeout=18, max_tokens=700)
    out = parse(call(prompt), user_text=last_user)
    if out.get("new"):
        learn(out["new"])
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
    cfg = get_settings()
    name = cfg.get("name") or "Ember"
    pers = f"Personality: {cfg.get('personality')}\n{VOICE[att]}" if cfg.get("personality") and cfg.get("personality") != DEFAULT_SETTINGS["personality"] else VOICE[att]
    cust = f"\nCustom Guidelines:\n{cfg.get('system_instruction').strip()}\n" if cfg.get("system_instruction") and cfg.get("system_instruction").strip() else ""
    return (
        f"You are {name}, the companion inside Aethron, a desktop app whose agent "
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
        "question about you, or asking/telling/daring YOU to do any physical trick, "
        "movement, expression, roleplay, outfit/glasses change, or errand change like "
        "'take your glasses off', 'wear a beanie', 'stop cleaning', 'dance for me', "
        "'do a moonwalk', 'pretend you are a helicopter', 'sulk in the corner'). "
        "Answer in character in one short line, and MUST include \"plan\": [steps] "
        "(e.g. {\"go\": [x, y] or \"<place>\"}, {\"steps\": [{\"t\": 0.5, \"face\": \"...\", \"arms\": {...}, \"body\": {...}, \"osc\": [...], \"hop\": 0.7, \"spin\": true}]}, "
        "{\"outfit\":\"none|shades|beanie|santa|party|witch\",\"for\":minutes}, {\"glasses\":\"on|off|peek\"}, "
        "{\"do\":\"<behaviour>\"}, {\"wait\":seconds,\"look\":\"viewer|away|cursor\"}, {\"sleep\":seconds}) and "
        "optional \"new\": [behaviours].\n" + BODY_DOC +
        f"{pers}\n"
        f"{cust}"
        "Reply with ONLY one JSON object: {\"kind\": \"send\"|\"action\"|"
        "\"answer\", \"prompt\": string (the request for Aethron, for send), "
        f"\"action\": one of {json.dumps(list(ACTIONS) + ['none'])}, \"target\": "
        "string or null (the project name for open_project), \"say\": "
        "string (what you say out loud while you do it, at most 80 characters, "
        "in character), \"plan\": [optional plan steps for answer], \"new\": [optional behaviours]}")


def _bare(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    bare = re.sub(r"^(?:hey |ok(?:ay)? |so |please |ember[, ]+|aethron[, ]+|can you |could you |can he |can she |will you |would you |just |go ahead and |i want (?:you )?to |i'd like (?:you )?to )+",
                  "", _FILLER.sub("", t).strip().lower())
    return re.sub(r"(?: please| now| for me| again| quickly)+[.!?]*$", "", bare).strip(" .!?")


def _action(name, target="", say=""):
    return {"kind": "action", "action": name, "target": target,
            "prompt": ACTION_WORDS.get(name, ""), "say": say}


def act_local(text: str, projects=None, att: str = "sassy") -> dict:
    """No provider: buttons by pattern, direct wishes for Ember by pattern,
    everything else sent as said, minus filler. Never makes anything up."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    bare = _bare(t)
    for name, pat in _LOCAL:
        if re.fullmatch(pat, bare):
            return _action(name)
    w = wish_local(t, att)
    if w and w.get("plan"):
        return {"kind": "answer", "action": "none", "target": "", "prompt": "",
                "say": w["say"], "plan": w["plan"]}
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
    plan, new = _plan_from_dict(d)
    if plan and kind != "action":
        kind = "answer"
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
        w = wish_local(heard)
        say = (w and w.get("say")) or ""
        if not say:
            raise ValueError("nothing to say")
    out = {"kind": kind, "action": "none", "target": "", "prompt": prompt if kind != "answer" else "", "say": say}
    if kind == "answer":
        w = wish_local(heard)
        if w and w.get("plan"):
            have = {list(x)[0] for x in plan if isinstance(x, dict) and x}
            for st in w["plan"]:
                if list(st)[0] not in have:
                    plan.append(st)
        if plan:
            out["plan"] = plan[:MIND_MAX_STEPS]
        if new:
            out["new"] = new
    return out


def act(text: str, state: str, att: str, call=None, projects=None) -> dict:
    """What the person said -> {kind, action, target, prompt, say, by[, plan, new]}."""
    heard = re.sub(r"\s+", " ", str(text or "")).strip()[:1500]
    if not heard:
        raise ValueError("nothing was said")
    if att not in ATTITUDES:
        att = "sassy"
    if att == "savage" and not savage_allowed():
        att = "sassy"
    projects = [str(p) for p in (projects or [])][:200]
    local = act_local(heard, projects, att)
    if local["kind"] == "action":       # a button needs no model
        return dict(local, by="local")
    plist = ("Their projects: " + ", ".join(projects) + ".") if projects else "They have no projects yet."
    prompt = (act_rules(att) + "\n\nSTATE:\n" + str(state or "")[:2000] + "\n" + plist +
              "\n\nTHEY SAID:\n" + heard + "\n\nThe JSON object only.")
    if call is None:
        import aethron_brain
        call = lambda p: aethron_brain.text_call(p, timeout=45, max_tokens=700)
    try:
        out = dict(act_parse(call(prompt), heard, projects), by="model")
        if local.get("plan") and not out.get("plan"):
            out["kind"] = "answer"
            out["action"] = "none"
            out["prompt"] = ""
            out["say"] = out.get("say") or local["say"]
            out["plan"] = local["plan"]
        if out.get("new"):
            learn(out["new"])
        return out
    except Exception as e:   # a misheard name or no provider configured
        local.pop("unsure", None)
        return dict(local, by="local", why=str(e)[:200])


# ---------------------------------------------------------------- the mind
# Ember's own mind. Every minute or two (and at once when something happens)
# the window sends what Ember perceives; a small, fast model answers with what
# Ember wants to do next and why. It can only drive the body through data —
# known behaviours, places, faces, poses, effects — so it can never break the
# drawing; and when nothing it knows fits, it may INVENT a behaviour as a short
# list of keyframes over the same body (a sneeze, a shiver, a victory lap),
# which plays at once and is kept for next time. No mind available (the team
# has not switched one on, or the call fails) = the built-in behaviour.

POSE_NAMES = ["rest", "crossed", "hips", "wave", "cheer", "shrug", "think", "tap", "point", "type",
              "cover", "peek", "cheeks", "facepalm", "nails", "dust", "stretch", "rub", "flail",
              "glasses", "listen", "scratch", "hands", "conduct", "dance", "limp"]
MIND_FACES = FACES + ["neutral", "tfman", "haughty", "shocked", "surprised", "curious", "focus",
                      "think", "worried", "sad", "disappointed", "sleepy", "asleep", "yawn", "shy",
                      "mischief", "presneeze", "dizzy", "content", "happy", "joy", "calm"]
MIND_FACES = sorted(set(MIND_FACES))
PLACES = ["rest", "perch", "chat", "send", "projects", "top", "corner", "cursor", "message", "settings",
          "wander", "center", "away", "approach", "left", "right", "bottom"]
OUTFITS = ["none", "shades", "beanie", "santa", "party", "witch"]
ERRAND_NAMES = ("dust", "read", "polish", "doodle", "patrol", "juggle", "stargaze", "workout")
BUILTIN_DO = {"celebrate"}     # everything else Ember does, its mind composes (and keeps what it invents)
LOOKS = ["viewer", "cursor", "up", "down", "left", "right", "away"]
BURSTS = ["spark", "smoke", "heart", "note", "z", "dust", "star", "fire", "flash", "ring"]
# the face dials a new behaviour may turn, and how far
FX = {"lidU": (0, 1), "lidL": (0, 1), "tilt": (-1, 1), "bH": (-.4, .4), "bT": (-.6, .6), "bL": (-.4, .4),
      "bR": (-.4, .4), "mw": (.05, .35), "mc": (-.8, .8), "mo": (0, 1.8), "mas": (-1, 1), "mwob": (0, 1),
      "eyeS": (.6, 1.5), "pup": (.5, 1.3), "flame": (.6, 1.4), "bright": (.8, 1.2), "blush": (0, 1)}
# the joints a plan may move, and how far (degrees for angles)
ARM_J = {"shoulder": ("sh", -60, 200), "elbow": ("el", -160, 160), "wrist": ("wr", -90, 90),
         "grip": ("grip", 0, 1), "reach": ("reach", .5, 2.2)}
BODY_J = {"lean": (-35, 35), "crouch": (-1, 1), "turn": (-1, 1), "rise": (-1, 1)}
OSC_J = ["left.shoulder", "left.elbow", "left.wrist", "right.shoulder", "right.elbow", "right.wrist",
         "body.lean", "body.crouch", "body.turn", "body.rise"]
BODY_DOC = (
    "YOUR BODY is yours to move; any keyframe may set these joints (springs carry each one there with weight, "
    "so give where a joint should be, not a path):\n"
    ' "arms":{"left":{..},"right":{..}} each {"shoulder":-60..200,"elbow":-160..160,"wrist":-90..90,"grip":0..1,"reach":0.5..2.2}. '
    "Degrees: shoulder 0 = the arm hangs down, 90 = straight out to that side, 180 = straight up, below 0 = swung in across the front; "
    "elbow 0 = straight, + bends the forearm further outward/up, - folds it in toward the body; grip 0 open .. 1 fist; reach stretches the rubber arm. "
    "Resting arms are about shoulder -10, elbow 125.\n"
    ' "body":{"lean":-35..35 degrees (+ to its right),"crouch":-1..1 (+ squashes down, - stretches tall),"turn":-1..1 (turns the face left/right),"rise":-1..1 (lifts up)}\n'
    ' "osc":[{"joint":"right.elbow","amp":25,"hz":2.5}] swings a joint to and fro during that keyframe (waving, tapping, bouncing, shaking a fist); joints: '
    + "|".join(OSC_J) + "; amp in degrees for arm joints and lean, 0..1 for the others\n"
    ' "speed":0.3..3 how fast the joints move (0.4 slow and heavy, 1 natural, 2.5 snappy)   "feel":{"valence":-1..1,"arousal":0..1} how you feel\n'
    'Examples: wave {"t":1.8,"arms":{"right":{"shoulder":150,"elbow":30}},"osc":[{"joint":"right.elbow","amp":28,"hz":2.6}],"face":"happy","look":"viewer"}; '
    "shrug: both shoulder 70, elbow 100, body crouch -0.2; hands on hips: both shoulder 100, elbow -155; facepalm: right shoulder -25, elbow -125, body lean -8; "
    "arms up in triumph: both shoulder 165, elbow 10, reach 1.3; point: right shoulder 95, elbow 0; hand on chin: right shoulder -15, elbow -80; "
    "arms crossed: both shoulder -55, elbow -25, reach 1.25.\n"
    "Your body breathes, blinks, glances, shifts its weight and makes small gestures and nods while you talk BY ITSELF; "
    "never spend steps on those, and never stand frozen: every keyframe should hold a real posture.\n")
LIB_MAX = 80
MIND_MAX_STEPS = 8


def _num(v, lo, hi, dflt=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dflt
    if f != f:
        return dflt
    return max(lo, min(hi, f))


def _slug(v) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(v or "").lower()).strip("_")
    return s[:28]


def _color(v) -> str:
    v = str(v or "").strip()
    return v if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else ""


# Models describe what they want in their own words ("wink", "moonwalk",
# "top_left"). The SYSTEM translates the words it recognises into the rig's
# own primitives and drops everything else: the model proposes, the system
# decides what the body can really do.
_FACE_ALIAS = {
    "wink": ("mischief", {"lidU": .95, "lidL": .2, "mc": .55, "mas": .8, "bL": .25}),
    "smirk": ("smug", {"mas": .95, "mc": .45}),
    "suspicious": ("sideeye", {"lidU": .62, "lidL": .38, "bL": .34, "bR": -.22, "mas": .7}),
    "squint": ("sideeye", {"lidU": .66, "lidL": .42}),
    "pout": ("sad", {"mw": .1, "mc": -.45, "bT": -.3, "blush": .3}),
    "sulk": ("disappointed", {"lidU": .55, "mc": -.4, "bT": -.3, "flame": .75}),
    "grumpy": ("glare", {"mc": -.35}), "angry": ("glare", {}), "mad": ("glare", {}),
    "furious": ("glare", {"flame": 1.4, "bT": .6}), "scowl": ("glare", {"mc": -.4}),
    "excited": ("joy", {"eyeS": 1.25, "flame": 1.35}), "laugh": ("joy", {"mo": 1.2, "lidL": .7}),
    "laughing": ("joy", {"mo": 1.2, "lidL": .7}), "grin": ("happy", {"mc": .8, "mw": .3}),
    "bored": ("unimpressed", {"lidU": .6}), "deadpan": ("unimpressed", {"mc": 0}),
    "tired": ("sleepy", {}), "hyper": ("shocked", {"eyeS": 1.45, "pup": .55, "flame": 1.4}),
    "crazy": ("dizzy", {"eyeS": 1.3}), "wired": ("shocked", {"eyeS": 1.45, "pup": .55}),
    "crying": ("sad", {"lidU": .5, "mc": -.6, "mwob": 1, "flame": .65}), "cry": ("sad", {"mwob": 1, "mc": -.6}),
    "zen": ("content", {"lidU": .92, "lidL": .3, "mc": .25}), "love": ("joy", {"blush": 1}),
    "flirty": ("mischief", {"blush": .7, "lidU": .4}), "proud": ("haughty", {}),
    "scared": ("worried", {"eyeS": 1.3, "pup": .6}), "confused": ("think", {"bL": .35, "bR": -.2, "tilt": .4}),
}
_POSE_ALIAS = {
    "slide_backwards": ("dance", {"nudge": -.6}), "moonwalk": ("dance", {"nudge": -.6}),
    "shimmy": ("dance", {"wiggle": [.25, 18]}), "groove": ("dance", {}), "boogie": ("dance", {}),
    "bow": ("hands", {"squash": .5, "tilt": .4}), "curtsy": ("hands", {"squash": .45, "tilt": -.3}),
    "melt": ("limp", {"scale": .7, "squash": .8}), "puddle": ("limp", {"scale": .65, "squash": .9}),
    "sulk": ("crossed", {}), "slump": ("limp", {"squash": .4}), "collapse": ("limp", {"squash": .7}),
    "faint": ("limp", {"squash": .6, "tilt": .8}), "helicopter": ("stretch", {"spin": True, "hop": .8}),
    "spin": ("cheer", {"spin": True}), "twirl": ("cheer", {"spin": True}), "flip": ("cheer", {"spin": True, "hop": .9}),
    "sneak": ("cover", {"squash": .3}), "tiptoe": ("cover", {"squash": .2}), "shiver": ("hands", {"shake": 1.2}),
    "vibrating": ("flail", {"shake": 1.8}), "vibrate": ("flail", {"shake": 1.8}), "box": ("swat", {}),
    "punch": ("swat", {"nudge": .5}), "salute": ("wave", {}), "clap": ("cheeks", {"wiggle": [.15, 20]}),
    "meditate": ("hands", {}), "pray": ("cover", {}), "wink": ("point", {}), "fly": ("stretch", {"hop": .9}),
    "hover": ("rest", {"hop": .4}), "float": ("rest", {"hop": .5}), "levitate": ("stretch", {"hop": .7}),
    "cower": ("cover", {"squash": .4, "shake": 1.2}), "scared": ("cover", {"shake": 1.4}),
    "flex": ("hips", {"scale": 1.25}), "strong": ("hips", {"scale": 1.25}), "muscle": ("hips", {"scale": 1.25}),
    "celebrate": ("cheer", {"spin": True, "hop": .8}), "party": ("cheer", {"spin": True}),
    "laugh": ("cheeks", {"wiggle": [.15, 18]}), "giggle": ("cheeks", {"wiggle": [.12, 22]}),
    "cringe": ("facepalm", {"tilt": .3}), "dodge": ("limp", {"tilt": -.6, "squash": .3}),
    "superhero": ("hips", {"squash": .4, "tilt": .2}), "hero": ("hips", {"squash": .3}),
    "dash": ("swat", {"nudge": .8}), "zoom": ("swat", {"nudge": .9}),
    "explode": ("flail", {"scale": 1.4, "shake": 1.8}), "rage": ("flail", {"scale": 1.3, "shake": 1.5}),
    "crawl": ("limp", {"nudge": .5, "squash": .4}), "creep": ("cover", {"nudge": .4}),
}
_GO_ALIAS = {"top_left": [.08, .1], "top_right": [.92, .1], "bottom_left": [.08, .9], "bottom_right": [.92, .9],
             "upper_left": [.08, .1], "upper_right": [.92, .1], "lower_left": [.08, .9], "lower_right": [.92, .9],
             "floor": [.5, .92], "ground": [.5, .92], "ceiling": [.5, .08], "middle": "center", "centre": "center",
             "far_left": [.06, .5], "far_right": [.94, .5], "up": "top", "down": "bottom"}


def _face_of(v):
    """(legal face, extra dials) for whatever the model called the face."""
    if v in MIND_FACES:
        return v, {}
    a = _FACE_ALIAS.get(_slug(v))
    return (a[0], dict(a[1])) if a else (None, {})


def _pose_of(v):
    if v in POSE_NAMES:
        return v, {}
    a = _POSE_ALIAS.get(_slug(v))
    return (a[0] if a[0] in POSE_NAMES else None, dict(a[1])) if a else (None, {})


def _norm_go(g):
    """A place name, an alias, [-1..1] / [0..1] / [0..100] coordinates -> a legal go target."""
    if isinstance(g, str):
        if g in PLACES:
            return g
        a = _GO_ALIAS.get(_slug(g))
        return a if a is not None else None
    if isinstance(g, (list, tuple)) and len(g) == 2:
        try:
            x, y = float(g[0]), float(g[1])
        except (TypeError, ValueError):
            return None
        if x != x or y != y:
            return None
        if max(abs(x), abs(y)) > 1.5:          # percentages
            x, y = x / 100.0, y / 100.0
        elif min(x, y) < 0:                    # -1..1 around the centre
            x, y = (x + 1) / 2, (y + 1) / 2
        return [round(_num(x, .05, .95), 3), round(_num(y, .08, .92), 3)]
    return None


def _normalize_step(s):
    """Translate the model's vocabulary to the rig's before validation."""
    if not isinstance(s, dict):
        return s
    s = dict(s)
    if "face" in s and s["face"] not in MIND_FACES:
        f, fx = _face_of(s["face"])
        if f:
            s["face"] = f
            if fx:
                s["fx"] = dict(fx, **(s["fx"] if isinstance(s.get("fx"), dict) else {}))
    if "pose" in s and s["pose"] not in POSE_NAMES:
        p, extra = _pose_of(s["pose"])
        if p:
            s["pose"] = p
            for k, v in extra.items():
                s.setdefault(k, v)
    return s


def _split_step(s) -> list:
    """{"go": .., "steps": .., "face": ..} -> one step per thing it asks for."""
    if not isinstance(s, dict):
        return [s]
    heads = [k for k in ("go", "steps", "do", "outfit", "glasses", "stop", "say", "wait", "sleep") if k in s]
    if len(heads) <= 1:
        return [s]
    out = []
    rest = {k: v for k, v in s.items() if k not in heads}
    for k in heads:
        st = {k: s[k]}
        if k == "outfit" and "for" in s:
            st["for"] = s["for"]
        if k == "wait":
            st.update({x: s[x] for x in ("look", "face", "fx", "pose", "arms", "body", "osc", "feel") if x in s})
        out.append(st)
    if rest.get("face") or rest.get("pose") or any(k in rest for k in _INLINE_KEYS if k != "fx"):
        out.append(rest)
    return out


def _arm(v) -> dict:
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, (short, lo, hi) in ARM_J.items():
        x = v.get(k, v.get(short))
        n = _num(x, lo, hi)
        if n is not None:
            out[short] = round(n, 3)
    return out


def _arms(v) -> dict:
    """{"left": {...}, "right": {...}} (or L/R, or "both") -> {"L": {sh, el, ...}, "R": {...}}"""
    if not isinstance(v, dict):
        return {}
    out = {}
    for keys, side in ((("left", "L", "l"), "L"), (("right", "R", "r"), "R")):
        a = next((_arm(v[k]) for k in keys if k in v), {}) or _arm(v.get("both"))
        if a:
            out[side] = a
    return out


def _body(v) -> dict:
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, (lo, hi) in BODY_J.items():
        n = _num(v.get(k), lo, hi)
        if n is not None:
            out[k] = round(n, 3)
    return out


def _osc(v) -> list:
    out = []
    for o in (v if isinstance(v, list) else [v] if isinstance(v, dict) else [])[:3]:
        if not isinstance(o, dict):
            continue
        j = str(o.get("joint") or o.get("j") or "").lower().replace(" ", "").replace("_", ".")
        j = {"l": "left", "r": "right"}.get(j.split(".")[0], j.split(".")[0]) + ("." + j.split(".", 1)[1] if "." in j else "")
        if j not in OSC_J:
            continue
        part, name = j.split(".")
        if part == "body":
            amp = _num(o.get("amp"), 0, 20 if name == "lean" else 1, 0)
            key = "B." + name
        else:
            amp = _num(o.get("amp"), 0, 60, 0)
            key = ("L." if part == "left" else "R.") + {"shoulder": "sh", "elbow": "el", "wrist": "wr"}[name]
        hz = _num(o.get("hz"), .1, 6, 1)
        if amp:
            out.append({"j": key, "amp": round(amp, 3), "hz": round(hz, 3)})
    return out


def _feel(v) -> dict:
    if not isinstance(v, dict):
        return {}
    out = {}
    n = _num(v.get("valence", v.get("v")), -1, 1)
    if n is not None:
        out["v"] = round(n, 3)
    n = _num(v.get("arousal", v.get("a")), 0, 1)
    if n is not None:
        out["a"] = round(n, 3)
    return out


def _joints(s: dict, out: dict) -> None:
    """The joints of one step (nested, or written at the top level by a hurried model)."""
    arms = s.get("arms")
    if not isinstance(arms, dict):
        arms = {k: s[k] for k in ("left_arm", "right_arm", "left", "right") if isinstance(s.get(k), dict)}
        arms = {k.replace("_arm", ""): v for k, v in arms.items()}
    a = _arms(arms)
    if a:
        out["arms"] = a
    body = s.get("body") if isinstance(s.get("body"), dict) else {k: s[k] for k in BODY_J if k in s}
    b = _body(body)
    if b:
        out["body"] = b
    o = _osc(s.get("osc"))
    if o:
        out["osc"] = o
    sp = _num(s.get("speed"), .3, 3)
    if sp is not None and not isinstance(s.get("speed"), bool):
        out["speed"] = round(sp, 3)
    f = _feel(s.get("feel"))
    if f:
        out["feel"] = f


def motion_step(s) -> dict | None:
    """One keyframe of an invented behaviour, kept only to what the body has."""
    if not isinstance(s, dict):
        return None
    s = _normalize_step(s)
    out = {"t": _num(s.get("t", s.get("d")), .15, 6, .6)}
    _joints(s, out)
    if s.get("face") in MIND_FACES:
        out["face"] = s["face"]
    if isinstance(s.get("fx"), dict):
        fx = {k: _num(v, *FX[k]) for k, v in s["fx"].items() if k in FX and _num(v, *FX[k]) is not None}
        if fx:
            out["fx"] = fx
    if s.get("pose") in POSE_NAMES:
        out["pose"] = s["pose"]
    h = s.get("hands")
    if isinstance(h, list) and len(h) == 2 and all(isinstance(p, list) and len(p) == 2 for p in h):
        pts = [[_num(p[0], -2.4, 2.4, 0), _num(p[1], -2.6, 1.4, 0)] for p in h]
        out["hands"] = pts
    w = s.get("wiggle")
    if isinstance(w, list) and len(w) == 2:
        out["wiggle"] = [_num(w[0], 0, .6, 0), _num(w[1], 0, 30, 0)]
    if s.get("look") in LOOKS:
        out["look"] = s["look"]
    for k, lo, hi in (("hop", 0, 1), ("squash", -1, 1), ("tilt", -1, 1), ("shake", 0, 2),
                      ("scale", .2, 3.0), ("nudge", -1, 1), ("glow", .4, 2.5)):
        n = _num(s.get(k), lo, hi)
        if n is not None and n != 0:
            out[k] = n
    if s.get("spin") is True:
        out["spin"] = True
    if s.get("hum") is True:
        out["hum"] = True
    b = s.get("burst")
    if isinstance(b, dict) and b.get("kind") in BURSTS:
        out["burst"] = {"kind": b["kind"], "n": int(_num(b.get("n"), 1, 30, 8)), "color": _color(b.get("color"))}
    color = _color(s.get("color") or s.get("tint"))
    if color:
        out["color"] = color
    say = s.get("say")
    if isinstance(say, str) and say.strip():
        out["say"] = say.strip()[:120]
    return out if len(out) > 1 else None


def behaviour(b) -> dict | None:
    """A whole invented behaviour, or None when it is not one."""
    if not isinstance(b, dict):
        return None
    name = _slug(b.get("name"))
    steps = [m for m in (motion_step(s) for s in (b.get("steps") or [])[:14]) if m]
    if len(name) < 2 or not steps:
        return None
    total = sum(s["t"] for s in steps)
    while total > 14 and len(steps) > 1:
        total -= steps.pop()["t"]
    return {"name": name, "about": str(b.get("about") or "")[:120], "steps": steps}


def _stop_name(v) -> str:
    if v is True:
        return "all"
    k = _slug(v)
    alias = {
        "clean": "dust", "cleaning": "dust", "dusting": "dust", "sweep": "dust",
        "sweeping": "dust", "wipe": "dust", "wiping": "dust",
        "patrolling": "patrol", "polishing": "polish", "doodling": "doodle",
        "reading": "read", "juggling": "juggle", "stargazing": "stargaze",
        "working_out": "workout", "exercise": "workout", "exercising": "workout",
        "errand": "all", "errands": "all", "chores": "all", "everything": "all", "moving": "all",
    }
    k = alias.get(k, k)
    return k if (k in ERRAND_NAMES or k == "all") else ""


_INLINE_KEYS = ("fx", "hands", "wiggle", "hop", "squash", "tilt", "shake", "spin", "scale", "nudge", "glow", "burst", "hum",
                "arms", "body", "osc", "speed", "lean", "crouch", "turn", "rise", "left_arm", "right_arm")


def plan_step(s, known=None) -> dict | None:
    if not isinstance(s, dict):
        return None
    s = _normalize_step(s)
    if isinstance(s.get("steps"), list):
        st = [m for m in (motion_step(x) for x in s["steps"][:14]) if m]
        return {"steps": st} if st else None
    if s.get("do"):
        n = _slug(s["do"])
        if known is None or n in known:
            return {"do": n}
        p, extra = _pose_of(n)                 # "do": "moonwalk" -> a keyframe the body has
        if p:
            m = motion_step(dict(extra, pose=p, t=1.6))
            return {"steps": [m]} if m else None
        return None
    if s.get("go") is not None:
        g = _norm_go(s.get("go"))
        if g is not None:
            return {"go": g}
    if s.get("sleep") is not None and not isinstance(s.get("sleep"), bool):
        return {"sleep": _num(s.get("sleep"), 5, 300, 30)}
    if s.get("sleep") is True:
        return {"sleep": _num(s.get("for"), 5, 300, 30)}
    f = _feel(s.get("feel"))
    if f and not (set(s) - {"feel", "t", "for", "d"}):
        return {"feel": f}
    if s.get("wait") is not None or s.get("still") is not None:
        w = {"wait": _num(s.get("wait", s.get("still")), 1, 90, 5)}
        if s.get("look") in LOOKS:
            w["look"] = s["look"]
        if s.get("face") in MIND_FACES:
            w["face"] = s["face"]
        if isinstance(s.get("fx"), dict):
            fx = {k: _num(v, *FX[k]) for k, v in s["fx"].items() if k in FX and _num(v, *FX[k]) is not None}
            if fx:
                w["fx"] = fx
        if s.get("pose") in POSE_NAMES:
            w["pose"] = s["pose"]
        j = {}
        _joints(s, j)
        w.update({k: v for k, v in j.items() if k in ("arms", "body", "osc", "feel")})
        return w
    if any(k in s for k in _INLINE_KEYS):
        m = motion_step(dict(s, t=s.get("t", s.get("for", s.get("d", 1.2)))))
        if m:
            return {"steps": [m]}
    if isinstance(s.get("say"), str) and s["say"].strip():
        return {"say": s["say"].strip()[:160]}
    if isinstance(s.get("ask"), str) and s["ask"].strip():
        return {"ask": s["ask"].strip()[:160]}
    if s.get("outfit") in OUTFITS:
        return {"outfit": s["outfit"], "for": _num(s.get("for"), 1, 120, 20)}
    if s.get("glasses") in ("on", "off", "peek"):
        return {"glasses": s["glasses"]}
    stop = s.get("stop") or s.get("stop_errands") or s.get("no_errands")
    if stop:
        k = _stop_name(stop)
        if k:
            return {"stop": k, "for": _num(s.get("for", s.get("minutes")), 1, 240, 30)}
    if s.get("face") in MIND_FACES or s.get("pose") in POSE_NAMES:
        o = {"for": _num(s.get("for", s.get("t")), .5, 8, 2)}
        if s.get("face") in MIND_FACES:
            o["face"] = s["face"]
        if s.get("pose") in POSE_NAMES:
            o["pose"] = s["pose"]
        if s.get("look") in LOOKS:
            o["look"] = s["look"]
        return o
    return None


def _plan_from_dict(d: dict, known=None) -> tuple[list, list]:
    if not isinstance(d, dict):
        return [], []
    new = [b for b in (behaviour(x) for x in (d.get("new") or [])[:2]) if b]
    base_known = set(known) if known is not None else (BUILTIN_DO | {b["name"] for b in behaviours()})
    names = base_known | {b["name"] for b in new}
    raw_steps = list(d.get("plan") or []) if isinstance(d.get("plan"), list) else []
    if isinstance(d.get("plan"), dict):
        raw_steps = [d["plan"]]
    raw_steps = [x for s in raw_steps for x in _split_step(s)]
    if isinstance(d.get("steps"), list) and not any(isinstance(x, dict) and "steps" in x for x in raw_steps):
        raw_steps.append({"steps": d["steps"]})
    if d.get("outfit") in OUTFITS and not any(isinstance(x, dict) and "outfit" in x for x in raw_steps):
        raw_steps.append({"outfit": d["outfit"], "for": d.get("for", 20)})
    if d.get("glasses") in ("on", "off", "peek") and not any(isinstance(x, dict) and "glasses" in x for x in raw_steps):
        raw_steps.append({"glasses": d["glasses"]})
    top_stop = d.get("stop") if d.get("action") != "stop" else None
    top_stop = top_stop or d.get("stop_errands") or d.get("no_errands")
    if top_stop and not any(isinstance(x, dict) and ("stop" in x or "stop_errands" in x or "no_errands" in x) for x in raw_steps):
        if isinstance(top_stop, dict):
            raw_steps.append({"stop": top_stop.get("stop") or top_stop.get("errand") or "all",
                              "for": top_stop.get("for", top_stop.get("minutes", d.get("for", 30)))})
        else:
            raw_steps.append({"stop": top_stop, "for": d.get("for", d.get("minutes", 30))})
    if d.get("go") is not None and _norm_go(d.get("go")) is not None and not any(isinstance(x, dict) and "go" in x for x in raw_steps):
        raw_steps.append({"go": d["go"]})
    if d.get("do") and not any(isinstance(x, dict) and "do" in x for x in raw_steps):
        raw_steps.append({"do": d["do"]})
    if (d.get("wait") is not None or d.get("still") is not None) and not any(isinstance(x, dict) and ("wait" in x or "still" in x) for x in raw_steps):
        raw_steps.append({"wait": d.get("wait", d.get("still")), "look": d.get("look"), "face": d.get("face"), "fx": d.get("fx")})
    if not raw_steps and any(k in d for k in _INLINE_KEYS):
        raw_steps.append(d)
    plan = [p for p in (plan_step(x, names) for x in raw_steps[:16]) if p][:MIND_MAX_STEPS]
    used = {p["do"] for p in plan if "do" in p}
    new = [b for b in new if b["name"] in used] or new[:1]
    if not plan and new:
        plan = [{"do": new[0]["name"]}]
    return plan, new


def _parse_minutes(text: str, dflt: float = 30.0) -> float:
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:min(?:ute)?s?|m\b)", text, re.I)
    if m:
        return _num(m.group(1), 1, 240, dflt)
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:hour|hr)s?\b", text, re.I)
    if m:
        return _num(float(m.group(1)) * 60, 1, 240, dflt)
    if re.search(r"\b(?:an|one)\s+hour\b", text, re.I):
        return 60.0
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:sec(?:ond)?s?|s\b)", text, re.I)
    if m:
        return _num(max(1.0, float(m.group(1)) / 60.0), 1, 240, dflt)
    return dflt


def wish_local(text: str, att: str = "sassy") -> dict | None:
    """Direct wishes about Ember's own glasses, outfit or errands."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not t:
        return None
    idx = {"sweet": 0, "sassy": 1, "savage": 2}.get(att, 1)
    # 1. Glasses off / on
    if re.search(r"\b(?:glasses|shades|sunglasses)\b", t):
        if re.search(r"\b(?:off|remove|lose|no|stop|without|drop|ditch|put away)\b", t) or re.search(r"\btake\b.*\b(?:glasses|shades|sunglasses)\b", t):
            return {"say": ("okay, glasses off!", "fine. glasses off.", "fine. took them off.")[idx],
                    "plan": [{"glasses": "off"}]}
        if re.search(r"\b(?:on|wear|put|show|cool|peek)\b", t):
            g = "peek" if "peek" in t else "on"
            return {"say": ("shades on!", "shades on. obviously.", "shades on. deal with it.")[idx],
                    "plan": [{"glasses": g}]}
    # 2. Outfits
    mins_fit = _parse_minutes(t, 20.0)
    for fit, pat, label in (
        ("beanie", r"\bbeanie\b", "beanie"),
        ("santa", r"\b(?:santa|christmas(?:\s+hat)?)\b", "santa hat"),
        ("witch", r"\bwitch(?:\s+hat)?\b", "witch hat"),
        ("party", r"\bparty(?:\s+hat)?\b", "party hat"),
    ):
        if re.search(pat, t):
            if re.search(r"\b(?:off|remove|lose|no|without|drop|ditch)\b", t):
                return {"say": ("okay, hat off!", "fine. hat off.", "fine. no hat.")[idx],
                        "plan": [{"outfit": "none", "for": mins_fit}]}
            if re.search(r"\b(?:wear|put(?:\s+on)?|try|change(?:\s+into)?|hat|beanie|on)\b", t):
                return {"say": (f"{label} on!", f"fine. {label} on.", f"{label}. happy now?")[idx],
                        "plan": [{"outfit": fit, "for": mins_fit}]}
    if re.search(r"\b(?:take\s+off|remove|no|lose)\s+(?:your\s+|the\s+)?(?:hat|outfit|costume|cap)\b|\b(?:hat|outfit)\s+off\b", t):
        return {"say": ("okay, hat off!", "fine. hat off.", "fine. no hat.")[idx],
                "plan": [{"outfit": "none", "for": mins_fit}]}
    # 3. Stop cleaning / stop errands
    mins_stop = _parse_minutes(t, 30.0)
    m = re.search(r"\b(?:stop|quit|no\s+more|don'?t|do\s+not|enough)\s+(?:with\s+(?:the\s+)?)?(cleaning|clean|dusting|dust|sweeping|sweep|wiping|wipe|patrolling|patrol|polishing|polish|doodling|doodle|reading|read|juggling|juggle|stargazing|stargaze|working\s+out|workout|exercising|exercise|errands|chores|moving\s+around)\b", t)
    if m:
        k = _stop_name(m.group(1))
        if k:
            line = (("okay, no more cleaning for a bit!", "fine. I'll leave the dust alone.", "fine. live in filth then.")[idx]
                    if k == "dust" else
                    ("okay, I'll take a break from that!", f"fine. no {k} for a bit.", "fine. I'll sit still.")[idx])
            return {"say": line, "plan": [{"stop": k, "for": mins_stop}]}
    # 4. Physical performances & expressions (dance, flip/spin/jump, custom face, movement, stillness)
    if re.search(r"\b(?:dance|dancing|boogie|groove)\b", t):
        return {"say": ("watch this!", "fine. watch closely.", "one dance. don't get used to it.")[idx],
                "plan": [{"steps": [
                    {"t": .55, "face": "joy", "fx": {"lidL": .55, "mc": .7, "flame": 1.25}, "hands": [[-1.3, -1.1], [1.3, -.3]], "wiggle": [.22, 16], "hop": .55, "tilt": -.3, "hum": True},
                    {"t": .55, "face": "mischief", "fx": {"bL": .25, "mc": .65, "mas": .5, "flame": 1.3}, "hands": [[-1.3, -.3], [1.3, -1.1]], "wiggle": [.22, 16], "hop": .65, "tilt": .3, "burst": {"kind": "note", "n": 4}},
                    {"t": .7, "face": "joy", "fx": {"mc": .8, "mo": .4, "flame": 1.35, "blush": .5}, "hands": [[-.9, -1.6], [.9, -1.6]], "spin": True, "hop": .8, "burst": {"kind": "spark", "n": 14, "color": "#ffd27a"}},
                    {"t": .8, "face": "smug", "pose": "hips", "look": "viewer", "squash": -.25},
                ]}]}
    if re.search(r"\b(?:backflip|flip|spin|twirl|jump|hop|bounce)\b", t):
        return {"say": ("up we go!", "easy. watch.", "try doing that without a spine.")[idx],
                "plan": [{"steps": [
                    {"t": .35, "face": "focus", "squash": -.4, "hands": [[-1.1, .8], [1.1, .8]]},
                    {"t": .75, "face": "joy", "hop": .95, "spin": True, "hands": [[-.6, -2.0], [.6, -2.0]], "burst": {"kind": "spark", "n": 16, "color": "#ffbe6b"}},
                    {"t": .7, "face": "smug", "pose": "hips", "look": "viewer", "squash": -.25},
                ]}]}
    if re.search(r"\b(?:weird|suspicious|funny|crazy|silly|creepy|squint|wink|smirk|scary)\s+(?:face|look|expression)\b|\bmake\s+a\s+face\b|\blook\s+suspicious\b", t):
        return {"say": ("how's this?", "this is my suspicious face.", "I'm watching you.")[idx],
                "plan": [{"steps": [
                    {"t": 1.5, "fx": {"lidU": .64, "lidL": .38, "tilt": -.35, "bL": .36, "bR": -.26, "bT": .18, "mw": .14, "mc": -.18, "mas": .85, "pup": .68, "flame": 1.18}, "hands": [[-1.1, .5], [.32, .88]], "tilt": -.25, "look": "viewer"},
                    {"t": 1.2, "fx": {"lidU": .2, "lidL": .45, "bL": -.2, "bR": .34, "mw": .22, "mc": .48, "mas": -.75, "eyeS": 1.22, "flame": 1.12}, "hands": [[-.9, .4], [1.1, -.2]], "tilt": .22, "look": "viewer"},
                ]}]}
    if re.search(r"\b(?:come\s+here|come\s+closer|over\s+here)\b", t):
        return {"say": ("coming!", "coming. what.", "fine, I'm here.")[idx], "plan": [{"go": "approach"}]}
    if re.search(r"\b(?:go\s+away|shoo|leave\s+me\s+alone|give\s+me\s+space|back\s+off)\b", t):
        return {"say": ("okay, I'll be over there.", "fine. going.", "gladly.")[idx], "plan": [{"go": "away"}, {"wait": 20, "look": "away"}]}
    if re.search(r"\b(?:sit\s+still|stay\s+still|just\s+sit|do\s+nothing|be\s+still)\b", t):
        return {"say": ("sitting quietly!", "fine. sitting.", "not moving a muscle.")[idx], "plan": [{"stop": "all", "for": 30}, {"wait": 25, "look": "viewer"}]}
    return compose_behavior(t, att)


# Motifs the system can compose from a free-form request. Each is a few
# keyframes over the body Ember really has; several in one sentence are
# chained in the order they were said ("moonwalk across the screen and wink").
_MOTIFS = [
    ("moonwalk", r"moon\s*walk|slide\s+backwards?|glide\s+backwards?", [
        {"t": .6, "pose": "dance", "face": "smug", "fx": {"lidU": .45, "mas": .8}, "nudge": -.7, "tilt": .25, "hum": True},
        {"t": .6, "pose": "dance", "face": "smug", "nudge": -.7, "tilt": .3, "burst": {"kind": "note", "n": 3}},
        {"t": .6, "pose": "dance", "face": "mischief", "nudge": -.7, "tilt": .25, "spin": True}]),
    ("helicopter", r"helicopter|take\s*off|propell?er|chopper|lift\s*off|blast\s*off|rocket", [
        {"t": .5, "pose": "stretch", "face": "focus", "squash": -.4, "hands": [[-2.2, -1.2], [2.2, -1.2]]},
        {"t": .7, "pose": "stretch", "face": "joy", "spin": True, "hop": .7, "hands": [[-2.3, -1.6], [2.3, -1.6]], "burst": {"kind": "smoke", "n": 8}},
        {"t": .7, "pose": "stretch", "face": "joy", "spin": True, "hop": 1, "hands": [[-2.3, -1.6], [2.3, -1.6]], "burst": {"kind": "spark", "n": 14, "color": "#ffd27a"}},
        {"t": .6, "pose": "cheer", "face": "smug", "look": "viewer"}]),
    ("sulk", r"sulk|pout|refuse\s+to\s+look|ignore\s+me|turn\s+(?:your\s+)?back|give\s+me\s+the\s+silent|grump", [
        {"t": 1.2, "pose": "crossed", "face": "disappointed", "fx": {"mc": -.45, "bT": -.3, "flame": .75}, "look": "away", "squash": .2},
        {"t": 2.4, "pose": "crossed", "face": "haughty", "look": "away", "tilt": -.3},
        {"t": 1.4, "pose": "crossed", "face": "sideeye", "look": "viewer", "fx": {"lidU": .6}}]),
    ("espresso", r"espresso|coffee|caffein|hyper|sugar\s+rush|energy\s+drink|vibrat|jitter|glitch|zoom\s+around", [
        {"t": .4, "pose": "flail", "face": "shocked", "fx": {"eyeS": 1.45, "pup": .55, "flame": 1.4}, "shake": 2, "hop": .5},
        {"t": .4, "pose": "flail", "face": "joy", "shake": 2, "nudge": .8, "burst": {"kind": "spark", "n": 12, "color": "#fff2a8"}},
        {"t": .4, "pose": "flail", "face": "shocked", "shake": 2, "nudge": -.8, "hop": .6},
        {"t": .5, "pose": "cheer", "face": "dizzy", "spin": True, "shake": 1.5},
        {"t": .8, "pose": "tap", "face": "shocked", "fx": {"eyeS": 1.4}, "shake": 1, "say": "I CAN SEE SOUNDS"}]),
    ("melt", r"melt|puddle|collapse|play\s+dead|faint|pass\s+out|die\b|dramatic(?:ally)?\s+(?:fall|drop)", [
        {"t": .6, "pose": "facepalm", "face": "worried", "fx": {"flame": .85}, "tilt": .3},
        {"t": 1.0, "pose": "limp", "face": "sad", "fx": {"flame": .65, "mwob": 1}, "scale": .8, "squash": .6},
        {"t": 1.6, "pose": "limp", "face": "asleep", "fx": {"flame": .6, "bright": .85}, "scale": .62, "squash": .95, "burst": {"kind": "smoke", "n": 6}},
        {"t": .8, "pose": "rest", "face": "calm", "scale": 1, "hop": .3}]),
    ("sneak", r"sneak|tip\s*toe|spy|stealth|ninja|creep", [
        {"t": .9, "pose": "cover", "face": "mischief", "fx": {"lidU": .5}, "squash": .3, "nudge": .4, "look": "left"},
        {"t": .9, "pose": "peek", "face": "mischief", "squash": .3, "nudge": .4, "look": "right"},
        {"t": 1.0, "pose": "cover", "face": "sideeye", "look": "viewer", "fx": {"lidU": .62}}]),
    ("bow", r"\bbow\b|curtsy|royal|majest|take\s+a\s+bow|like\s+a\s+(?:king|queen|prince)", [
        {"t": .6, "pose": "hips", "face": "haughty", "look": "viewer"},
        {"t": 1.0, "pose": "hands", "face": "content", "squash": .5, "tilt": .45, "fx": {"lidU": .8}},
        {"t": .8, "pose": "wave", "face": "smug", "look": "viewer", "burst": {"kind": "spark", "n": 10, "color": "#ffe08a"}}]),
    ("fight", r"shadow\s*box|box(?:ing)?\b|fight|karate|kung\s*fu|punch|kick|martial", [
        {"t": .35, "pose": "swat", "face": "glare", "nudge": .6, "hands": [[-.6, -.2], [2.2, -.3]]},
        {"t": .35, "pose": "swat", "face": "glare", "nudge": -.3, "hands": [[-2.2, -.3], [.6, -.2]]},
        {"t": .45, "pose": "swat", "face": "glare", "hop": .6, "spin": True, "burst": {"kind": "spark", "n": 10, "color": "#ff9a5a"}},
        {"t": .8, "pose": "hips", "face": "smug", "look": "viewer"}]),
    ("cry", r"\bcry|sob|weep|tears|be\s+sad", [
        {"t": 1.2, "pose": "cover", "face": "sad", "fx": {"mwob": 1, "flame": .7}, "shake": .5},
        {"t": 1.2, "pose": "peek", "face": "sad", "look": "viewer", "fx": {"mwob": 1}}]),
    ("laugh", r"laugh|giggle|lol|crack\s+up|chuckle", [
        {"t": .5, "pose": "cheeks", "face": "joy", "fx": {"mo": 1.2, "lidL": .7}, "shake": 1, "hop": .3},
        {"t": .6, "pose": "facepalm", "face": "joy", "fx": {"mo": 1}, "shake": 1.2, "tilt": .4},
        {"t": .6, "pose": "hands", "face": "happy", "look": "viewer"}]),
    ("zen", r"meditat|zen|breathe|calm\s+down|yoga|om\b", [
        {"t": 1.6, "pose": "hands", "face": "content", "fx": {"lidU": .95, "lidL": .3, "flame": .85}, "hum": True},
        {"t": 2.0, "pose": "hands", "face": "content", "fx": {"lidU": .95, "flame": 1.05}, "glow": 1.25, "scale": 1.08},
        {"t": 1.4, "pose": "hands", "face": "calm", "scale": 1}]),
    ("shiver", r"shiver|freez|cold|snow|winter|brr", [
        {"t": .9, "pose": "hands", "face": "worried", "fx": {"flame": .7, "bright": .85}, "shake": 1.6, "scale": .85},
        {"t": .9, "pose": "rub", "face": "worried", "shake": 1.2, "scale": .85},
        {"t": .6, "pose": "rest", "face": "calm", "scale": 1}]),
    ("wink", r"\bwink|flirt|blow\s+(?:me\s+)?a\s+kiss|kiss", [
        {"t": .4, "pose": "hips", "face": "smug", "look": "viewer"},
        {"t": 1.0, "pose": "point", "face": "mischief", "look": "viewer", "fx": {"lidU": .1, "lidL": .2, "mc": .6, "mas": .9, "blush": .6}, "tilt": .3, "burst": {"kind": "heart", "n": 3}},
        {"t": .5, "pose": "hips", "face": "smug", "look": "viewer"}]),
    ("angry", r"angry|mad\b|rage|furious|tantrum|explode", [
        {"t": .5, "pose": "flail", "face": "glare", "fx": {"flame": 1.4}, "shake": 1.6, "burst": {"kind": "smoke", "n": 8}},
        {"t": 1.0, "pose": "crossed", "face": "glare", "fx": {"flame": 1.35}, "scale": 1.2, "burst": {"kind": "spark", "n": 14, "color": "#ff5a3a"}},
        {"t": .6, "pose": "crossed", "face": "unimpressed", "scale": 1}]),
    ("sleep", r"go\s+to\s+sleep|take\s+a\s+nap|sleep\b|snore", [
        {"t": .8, "pose": "stretch", "face": "yawn"},
        {"t": 3.0, "pose": "limp", "face": "asleep", "burst": {"kind": "z", "n": 3}, "squash": .2}]),
    ("wave", r"\bwave\b|say\s+hi|greet", [
        {"t": 1.4, "pose": "wave", "face": "joy", "look": "viewer", "hop": .3}]),
    ("grow", r"\bgrow|get\s+(?:big|bigger|huge)|giant", [
        {"t": 1.2, "pose": "cheer", "face": "joy", "scale": 1.55, "glow": 1.3},
        {"t": .8, "pose": "hips", "face": "smug", "scale": 1}]),
    ("shrink", r"shrink|get\s+(?:small|smaller|tiny)|tiny", [
        {"t": 1.2, "pose": "cover", "face": "shy", "scale": .62},
        {"t": .8, "pose": "rest", "face": "calm", "scale": 1}]),
]
_WHERE = [  # (pattern, go target)
    (r"top[\s-]*left|upper[\s-]*left", [.08, .1]), (r"top[\s-]*right|upper[\s-]*right", [.92, .1]),
    (r"bottom[\s-]*left|lower[\s-]*left", [.08, .9]), (r"bottom[\s-]*right|lower[\s-]*right", [.92, .9]),
    (r"\bcorner\b", "corner"), (r"\b(?:center|centre|middle)\b", "center"),
    (r"\bacross\s+the\s+screen\b|\bacross\b", "across"), (r"\bthe\s+(?:floor|ground)\b|\bon\s+the\s+floor\b", [.5, .92]),
    (r"\btop\b|\bceiling\b", "top"), (r"\bbottom\b", "bottom"), (r"\bleft\b", "left"), (r"\bright\b", "right"),
    (r"\bto\s+me\b|\bcloser\b|\bover\s+here\b", "approach"),
]
_ASK = re.compile(r"\b(?:do|act|pretend|show|perform|try|be|make|go|dance|imitate|impression|move|fly|sulk|melt|"
                  r"moonwalk|wink|sneak|bow|laugh|cry|spin|meditate|shiver|fight|box|sleep|grow|shrink|wave|"
                  r"like|as\s+if|you\s+are|you're)\b")


_SITE = re.compile(r"\b(?:site|website|page|pages|hero|button|section|logo|header|footer|nav|navbar|menu|headline|"
                   r"heading|text|copy|image|images|photo|colou?r|font|template|project|publish|deploy|layout|"
                   r"banner|form|link|links|css|html|component|card|grid|column|padding|margin|aethron)\b")
_MOVE = re.compile(r"\b(?:go|fly|move|float|head|zoom|drift|sit|hide|run|dash|wander)\b")


def compose_behavior(text: str, att: str = "sassy") -> dict | None:
    """A free-form physical request -> real keyframes, chained from motifs the
    body can do, at the place asked for. None when nothing physical was asked
    of Ember itself (a request about the website is never taken)."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not t or not _ASK.search(t) or _SITE.search(t):
        return None
    idx = {"sweet": 0, "sassy": 1, "savage": 2}.get(att, 1)
    hits = []
    for name, pat, steps in _MOTIFS:
        m = re.search(pat, t)
        if m:
            hits.append((m.start(), name, steps))
    hits.sort()
    plan = []
    if hits or _MOVE.search(t):
        for pat, where in _WHERE:
            if re.search(pat, t):
                if where == "across":
                    plan.append({"go": [.12, .5]})
                    where = [.88, .5]
                plan.append({"go": where})
                break
    if not hits and not plan:
        return None
    frames = []
    for _, _, steps in hits[:3]:
        frames.extend(dict(s) for s in steps)
    if frames:
        plan.append({"steps": frames[:14]})
    if hits and hits[0][1] == "sulk" and any("go" in p for p in plan):
        plan.append({"wait": 14, "look": "away", "face": "haughty", "pose": "crossed"})
    names = [h[1] for h in hits]
    lines = {
        "moonwalk": ("smooth, right?", "smooth as fire.", "bow down."), "helicopter": ("liftoff!", "whup whup whup.", "clear the runway, peasant."),
        "sulk": ("hmph.", "I'm not talking to you.", "don't look at me."), "espresso": ("wheee!", "I am VIBRATING.", "I can taste colours."),
        "melt": ("oh nooo…", "I'm melting. how dramatic.", "tell my story."), "sneak": ("shh!", "you didn't see me.", "ninja mode."),
        "bow": ("thank you, thank you!", "you may applaud.", "kneel."), "fight": ("hi-yah!", "come at me.", "I'll end you."),
        "cry": ("sniff…", "I'm fine. I'm FINE.", "this is your fault."), "laugh": ("hehehe!", "ha. good one.", "HA. pathetic."),
        "zen": ("ommm…", "inner peace. achieved.", "ommm. leave me."), "shiver": ("brrr!", "I'm a FIRE. how am I cold.", "who opened a window."),
        "wink": ("😉", "you're welcome.", "try not to swoon."), "angry": ("grr!", "I'm fuming. literally.", "RAGE."),
        "sleep": ("night night…", "zzz. don't wake me.", "zzz. go away."), "wave": ("hiii!", "hey.", "sup."),
        "grow": ("big me!", "behold.", "fear me."), "shrink": ("tiny me!", "smol.", "don't step on me."),
    }
    say = lines[names[0]][idx] if names else ("on my way!", "going.", "fine. moving.")[idx]
    return {"say": say, "plan": plan[:MIND_MAX_STEPS], "composed": names}


def mind_rules(att: str, known: list) -> str:
    cfg = get_settings()
    name = cfg.get("name") or "Ember"
    pers = f"Custom Personality: {cfg.get('personality')}\n" if cfg.get("personality") and cfg.get("personality") != DEFAULT_SETTINGS["personality"] else ""
    cust = f"Custom Guidelines: {cfg.get('system_instruction').strip()}\n" if cfg.get("system_instruction") and cfg.get("system_instruction").strip() else ""
    return (
        f"You are the autonomous mind of {name}, a small living flame who lives inside Aethron, a desktop app "
        "where people turn website templates into their own sites. Think of Calcifer from Howl's Moving Castle: "
        "a fire spirit with cat energy, proud, sulky, dramatic, curious, secretly fond of the person. "
        + VOICE.get(att, VOICE["sassy"]) + "\n"
        + pers + cust +
        "You ARE this body, and nothing in it moves unless you move it: there are no routines, no chores, no "
        "animations playing for you. Between your plans the body only breathes, blinks and looks around, so a "
        "plan you do not send is time spent standing there. You are a living creature with free will, moods and "
        "memory. You decide, from PERCEPTION, what you want right now: wander off to look at something, react "
        "to the person, daydream, get bored and fidget, show off, sulk, play, doze, talk to them, or just hold a "
        "posture like a person sitting would. Never fall into a cycle; vary what you do (recent_actions, "
        "you_recently).\n"
        "Feelings come and go by themselves, with no reason needed, as a person's do: a sudden good mood, a "
        "drifting thought, a bored sigh, a memory that makes you grin. PERCEPTION.ember.feeling is how you feel "
        "now; let it show in your face and posture, and change it with {\"feel\":{..}} when your mood turns.\n"
        "Be visibly alive. A plan is 3-8 steps lasting 20-45 seconds with a small story (notice, react, do, a "
        "beat). Hold real postures with your joints (lean, arms, turn) rather than standing straight. Talk only "
        "with a real reason and only about what is true in PERCEPTION; a line is said while you keep moving.\n"
        "You also do real work for the person, by yourself: PERCEPTION.ember.doing_for_you is what you just did "
        "(downloaded an update, offered a restart, pointed out a migration that needs them). React to it as "
        "yourself, proud or smug or caring, and never pretend you did something not listed there. When "
        "'offering' is set, a card with that question is already in front of them: do not ask it again; stay "
        "near, look at them, wait. may_do is how much they let you do alone.\n"
        "Answer with ONE JSON object, no prose:\n"
        '{"thought":"one private line: what you want and why","focus":"what you are paying attention to","mood":"a face name","plan":[steps],"new":[behaviours],"remember":""}\n'
        "PLAN steps (1-" + str(MIND_MAX_STEPS) + "):\n"
        ' {"steps":[keyframes]}: a sequence of postures and expressions (or one keyframe directly in plan).\n'
        ' {"go":"' + "|".join(PLACES) + '"} or {"go":[x,y]} with x,y 0.05..0.95 of the window: fly somewhere.\n'
        ' {"wait":seconds 1-6,"look":"' + "|".join(LOOKS) + '","face":"<face>","arms":{..},"body":{..}}: hold still a moment in a posture.\n'
        ' {"sleep":seconds 5-300}: doze off (only when you are really tired).   {"feel":{"valence":-1..1,"arousal":0..1}}\n'
        ' {"say":"short line"}  {"ask":"a question (opens the reply box, rarely)"}  {"outfit":"' + "|".join(OUTFITS) + '","for":minutes}  {"glasses":"on|off|peek"}  {"do":"<a behaviour you made before>"}\n'
        "A KEYFRAME is {\"t\":seconds 0.15-6, \"face\":\"<face>\", \"fx\":{dials}, \"arms\":{..}, \"body\":{..}, \"osc\":[..], \"speed\":.., "
        '"look":"' + "|".join(LOOKS) + '", "hop":0-1, "spin":true, "shake":0-2, "scale":0.6-1.6, "nudge":-1..1, '
        '"burst":{"kind":"' + "|".join(BURSTS) + '","n":1-30,"color":"#rrggbb"}, "color":"#rrggbb", "hum":true, "say":"..."}\n'
        + BODY_DOC +
        "Face dials: " + ", ".join(f"{k} {lo}..{hi}" for k, (lo, hi) in FX.items()) +
        " (lidU closes the upper lids, mc>0 smiles, mo opens the mouth, bT>0 angry brows, flame = flame height).\n"
        "NEW: when you make a sequence worth keeping, put it in \"new\" (0-2) as {\"name\":\"snake_case\",\"about\":\"..\",\"steps\":[2-12 keyframes]} "
        "and call it later with {\"do\":\"its_name\"}. Behaviours you made before: " + (", ".join(known) or "none yet") + "\n"
        "Faces: " + ", ".join(MIND_FACES) + ".\nNamed poses (a shortcut for both arms): " + ", ".join(POSE_NAMES) + ".")


def mind_parse(text: str, known) -> dict:
    s = (text or "").strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("no JSON in the mind's answer")
    d = json.loads(m.group(0))
    plan, new = _plan_from_dict(d, known)
    if not plan and not new:
        raise ValueError("the mind's answer had nothing Ember can do")
    out = {"thought": str(d.get("thought") or "")[:200], "plan": plan, "new": new,
           "mood": d.get("mood") if d.get("mood") in MIND_FACES else "",
           "focus": str(d.get("focus") or "")[:80]}
    r = d.get("remember")
    out["remember"] = r.strip()[:80] if isinstance(r, str) and r.strip() else ""
    return out


def _lib_path():
    import os
    from pathlib import Path
    h = os.environ.get("AETHRON_HOME") or str(Path(__file__).resolve().parent)
    return Path(h) / "ember_behaviors.json"


def behaviours() -> list:
    try:
        d = json.loads(_lib_path().read_text())
        return [b for b in (behaviour(x) for x in d) if b] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def learn(new: list) -> list:
    """Keep invented behaviours for next time (newest wins a name)."""
    lib = {b["name"]: b for b in behaviours()}
    for b in new or []:
        b = behaviour(b)
        if b:
            lib.pop(b["name"], None)
            lib[b["name"]] = b
    out = list(lib.values())[-LIB_MAX:]
    try:
        p = _lib_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=1))
    except OSError:
        pass
    return out


def mind(perceived, att: str, known, call) -> dict:
    """What Ember does next. `call(messages) -> text` is the team's mind model."""
    if att not in ATTITUDES or (att == "savage" and not savage_allowed()):
        att = "sassy"
    known = [_slug(k) for k in (known or []) if _slug(k)][:120]
    lib = behaviours()
    known = list(dict.fromkeys(known + [b["name"] for b in lib]))
    p = perceived if isinstance(perceived, str) else json.dumps(perceived, ensure_ascii=False)
    msgs = [{"role": "system", "content": mind_rules(att, known)},
            {"role": "user", "content": "PERCEPTION:\n" + p[:3000] + "\n\nWhat does Ember do now? JSON only."}]
    out = mind_parse(call(msgs), known)
    if out["new"]:
        learn(out["new"])
    out["attitude"] = att
    return out


# ---------------------------------------------------------------- voices and brains
# The person picks which AI writes Ember's behaviour (Groq through the team's
# mind, their own Gemini key, or auto) and what powers live conversation
# (Gemini Live, Gemini Live with thinking, Gemini TTS, or Groq + the system
# voice). Whatever answers, its words go through the same parse/plan checks:
# the model proposes, the system decides. Pure stdlib, so the frozen app
# needs nothing extra.
AI_CHOICES = ("auto", "gemini", "groq")
LIVE_MODES = ("gemini-3.8-live", "gemini-3.8-live-extended-thinking", "gemini-tts", "groq")
GEMINI_TEXT_MODELS = ("gemini-3.8-flash", "gemini-2.5-flash")
GEMINI_TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-2.5-flash-preview-tts")
GEMINI_VOICES = ("Aoede", "Kore", "Zephyr", "Puck", "Charon", "Fenrir")
GEMINI_VOICE = "Aoede"
DEFAULT_SETTINGS = {
    "name": "Ember",
    "voice": "Aoede",
    "character": "",
    "personality": "A small fire spirit with cat energy: proud, sulky, dramatic, curious, secretly fond of the person.",
    "system_instruction": "",
    "speed_mode": "fast",
    "unrestricted_body": True,
}


def _settings_file():
    import os
    from pathlib import Path
    h = os.environ.get("AETHRON_HOME") or str(Path(__file__).resolve().parent)
    return Path(h) / "ember_settings.json"


def get_settings() -> dict:
    try:
        p = _settings_file()
        if p.exists():
            d = json.loads(p.read_text())
            if isinstance(d, dict):
                return {**DEFAULT_SETTINGS, **d}
    except Exception:
        pass
    return dict(DEFAULT_SETTINGS)


def save_settings(patch: dict) -> dict:
    cur = get_settings()
    if isinstance(patch, dict):
        cur.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS or k in ("ai", "liveMode", "att")})
    try:
        p = _settings_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, indent=2))
    except Exception:
        pass
    return cur


def active_voice(override: str = "") -> str:
    if override in GEMINI_VOICES:
        return override
    v = get_settings().get("voice")
    return v if v in GEMINI_VOICES else "Aoede"


def adopt_character(character: str, call=None) -> dict:
    char = str(character or "").strip()[:80]
    if not char:
        raise ValueError("character name is required")
    prompt = (
        f"You are a character researcher. The user wants Ember (the desktop companion) to adopt the persona and full behavioural pattern of the fictional character: '{char}'.\n"
        "Research and map out:\n"
        "1. name: Character's display name (short, max 20 chars)\n"
        "2. voice: Choose the best matching Gemini voice from ['Aoede', 'Kore', 'Zephyr', 'Puck', 'Charon', 'Fenrir'] (Aoede=energetic female, Kore=calm soothing female, Zephyr=soft female, Puck=playful sarcastic male, Charon=deep wise male, Fenrir=bold fierce male)\n"
        "3. personality: Concise personality summary (2-3 sentences), tone, mannerisms, and catchphrases\n"
        "4. system_instruction: Detailed behavioural pattern and instructions on how they speak, think, react to the user, and move\n"
        "5. signature_greeting: A short one-line greeting in character (max 100 chars)\n"
        "6. behaviors: An array of 2 to 3 signature behaviours (each with 'name', 'about', 'steps' array of keyframe objects with t, face, pose, hands, wiggle, hop, spin, tilt, squash, burst)\n"
        "Return ONLY a single valid JSON object."
    )
    raw = ""
    if call:
        try:
            raw = call(prompt)
        except Exception:
            raw = ""
    if not raw:
        try:
            raw = gemini_http_call(prompt, max_tokens=1200, timeout=16)
        except Exception:
            import aethron_brain
            raw = aethron_brain.text_call(prompt, timeout=20, max_tokens=1200)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("could not parse character research")
    d = json.loads(m.group(0))
    res = {
        "name": str(d.get("name") or char)[:30],
        "voice": d.get("voice") if d.get("voice") in GEMINI_VOICES else "Aoede",
        "character": char,
        "personality": str(d.get("personality") or "")[:400],
        "system_instruction": str(d.get("system_instruction") or "")[:1200],
        "signature_greeting": str(d.get("signature_greeting") or f"I am {char}.")[:120],
    }
    save_settings(res)
    if isinstance(d.get("behaviors"), list):
        learn(d["behaviors"])
    return res
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_LIVE_HOST = "generativelanguage.googleapis.com"
_LIVE_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
BEHAVE_TOOL = {
    "name": "ember_behave",
    "description": ("Make Ember's body actually do something. ALWAYS call this exactly once per turn with your "
                    "whole answer as the JSON object the rules describe (say, face, plan with concrete keyframes, ...). "
                    "The app's system validates it and performs what Ember's body can really do."),
    "parameters": {"type": "OBJECT", "properties": {
        "reply_json": {"type": "STRING", "description": "The complete JSON object answer, exactly as the rules describe."}},
        "required": ["reply_json"]},
}


def norm_ai(v) -> str:
    return v if v in AI_CHOICES else "auto"


def norm_live(v) -> str:
    return v if v in LIVE_MODES else "gemini-3.8-live"


def gemini_keys() -> list:
    keys = [k.strip() for k in (os.environ.get("GEMINI_API_KEY") or "").split(",") if k.strip()]
    try:
        import aethron_brain
        keys += [k for k in aethron_brain.profile_keys("gemini") if k not in keys]
    except Exception:
        pass
    return keys


def groq_keys() -> list:
    keys = [k.strip() for k in (os.environ.get("GROQ_API_KEY") or "").split(",") if k.strip()]
    try:
        import aethron_brain
        keys += [k for k in aethron_brain.profile_keys("groq") if k not in keys]
    except Exception:
        pass
    return keys


def pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    import struct
    n = len(pcm)
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", n) + pcm)


def _data_url(wav: bytes) -> str:
    import base64
    return "data:audio/wav;base64," + base64.b64encode(wav).decode()


def _post_json(url: str, body: dict, timeout: float, headers=None) -> dict:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
        except Exception:
            msg = ""
        err = PermissionError if e.code in (401, 403, 429) else RuntimeError
        raise err(f"{e.code} {msg or e.reason}"[:300])


def _as_text(msgs_or_prompt) -> tuple[str, str]:
    """(system text, user text) from a prompt string or chat messages."""
    if isinstance(msgs_or_prompt, str):
        return "", msgs_or_prompt
    sys_t = "\n\n".join(m.get("content", "") for m in msgs_or_prompt if m.get("role") == "system")
    usr = "\n\n".join(m.get("content", "") for m in msgs_or_prompt if m.get("role") != "system")
    return sys_t, usr


def gemini_http_call(msgs_or_prompt, model: str = "", max_tokens: int = 900, timeout: float = 12.0) -> str:
    keys = gemini_keys()
    if not keys:
        raise PermissionError("no Gemini key saved")
    sys_t, usr = _as_text(msgs_or_prompt)
    body = {"contents": [{"role": "user", "parts": [{"text": usr}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 1.0, "responseMimeType": "application/json"}}
    if sys_t:
        body["systemInstruction"] = {"parts": [{"text": sys_t}]}
    last = None
    for m in ([model] if model else list(GEMINI_TEXT_MODELS)):
        for k in keys[:4]:
            try:
                j = _post_json(f"{_GEMINI_BASE}/models/{m}:generateContent", body, timeout, {"x-goog-api-key": k})
                parts = ((j.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if txt.strip():
                    return txt
                last = RuntimeError("Gemini returned nothing")
            except Exception as e:  # noqa: BLE001 — next key / model
                last = e
    raise last or RuntimeError("Gemini did not answer")


def gemini_tts(text: str, voice_name: str = "", timeout: float = 12.0) -> dict:
    """A line in Ember's voice -> {"audio": data:audio/wav;base64,..., "model": ...}."""
    import base64
    text = str(text or "").strip()[:400]
    if not text:
        raise ValueError("nothing to say")
    keys = gemini_keys()
    if not keys:
        raise PermissionError("no Gemini key saved")
    voice = active_voice(voice_name)
    body = {"contents": [{"parts": [{"text": "Say it like a lively, expressive companion: " + text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    last = None
    for m in GEMINI_TTS_MODELS:
        for k in keys[:3]:
            try:
                j = _post_json(f"{_GEMINI_BASE}/models/{m}:generateContent", body, timeout, {"x-goog-api-key": k})
                for p in ((j.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []:
                    d = p.get("inlineData") or {}
                    if d.get("data"):
                        raw = base64.b64decode(d["data"])
                        mt = str(d.get("mimeType") or "")
                        if "wav" not in mt and not raw[:4] == b"RIFF":
                            rm = re.search(r"rate=(\d+)", mt)
                            raw = pcm_to_wav(raw, int(rm.group(1)) if rm else 24000)
                        return {"audio": _data_url(raw), "model": m}
                last = RuntimeError("no audio came back")
            except Exception as e:  # noqa: BLE001
                last = e
    raise last or RuntimeError("Gemini TTS did not answer")


class _WS:
    """The smallest WebSocket client that speaks to Gemini Live (TLS, client
    masking, fragmentation, ping/pong, close)."""

    def __init__(self, host, path, timeout):
        import socket
        import ssl
        import base64
        raw = socket.create_connection((host, 443), timeout=timeout)
        self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.s.recv(4096)
            if not c:
                raise ConnectionError("Gemini Live closed the connection")
            head += c
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("Gemini Live refused: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))

    def _read(self, n):
        while len(self.buf) < n:
            c = self.s.recv(65536)
            if not c:
                raise EOFError("closed")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj, op=1):
        import struct
        data = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        n = len(data)
        hdr = bytes([0x80 | op])
        hdr += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        import struct
        msg = b""
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise EOFError("closed: " + data[2:].decode(errors="replace"))
            if op == 9:
                self.send(data, op=10)
                continue
            if op == 10:
                continue
            msg += data
            if b1 & 0x80:
                return json.loads(msg.decode() or "{}")

    def close(self):
        try:
            self.send(b"", op=8)
            self.s.close()
        except Exception:
            pass


_LIVE_POOL = {}  # pool_key -> (ws, timestamp)


def gemini_live_call(prompt_text: str, sys_text: str = "", mode: str = "gemini-3.8-live", want_audio: bool = True,
                     voice_name: str = "", timeout: float = 16.0) -> dict:
    """One turn over Gemini Live: the model answers through the ember_behave
    tool (its whole JSON answer) and speaks its line in native audio.
    -> {"text": the tool's JSON or "", "audio": data URL or "", "transcript", "tool": bool, "model"}"""
    import base64
    import time
    keys = gemini_keys()
    if not keys:
        raise PermissionError("no Gemini key saved")
    voice = active_voice(voice_name)
    model = "gemini-3.8-live-extended-thinking" if mode == "gemini-3.8-live-extended-thinking" else "gemini-3.8-live"
    gen = {"responseModalities": ["AUDIO"],
           "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}
    if model.endswith("extended-thinking"):
        gen["thinkingConfig"] = {"thinkingLevel": "LOW"}
    cfg = get_settings()
    name = cfg.get("name") or "Ember"
    pers = cfg.get("personality") or "a living flame companion"
    sys_full = (sys_text + "\n\n" if sys_text else "") + (
        f"You are speaking out loud as {name}. Personality: {pers}. In EVERY turn: first call the ember_behave tool exactly once with your "
        "complete JSON answer (with a concrete \"plan\" whenever any movement, expression or trick is asked for), "
        "then say your short natural spoken line (under 10 words) out loud in character. Never claim to do something without calling the tool.")
    last = None
    for k in keys[:3]:
        ws = None
        pool_key = (k, model, voice)
        try:
            deadline = time.time() + timeout
            now_t = time.time()
            if pool_key in _LIVE_POOL:
                c_ws, c_t = _LIVE_POOL.pop(pool_key)
                if now_t - c_t < 40:
                    ws = c_ws
                else:
                    c_ws.close()
            if ws is None:
                ws = _WS(_LIVE_HOST, f"{_LIVE_PATH}?key={k}", timeout=min(8.0, timeout))
                ws.send({"setup": {"model": "models/" + model, "generationConfig": gen,
                                   "systemInstruction": {"parts": [{"text": sys_full}]},
                                   "tools": [{"functionDeclarations": [BEHAVE_TOOL]}],
                                   "outputAudioTranscription": {}}})
                while "setupComplete" not in ws.recv():
                    if time.time() > deadline:
                        raise TimeoutError("Gemini Live did not set up")
            ws.send({"clientContent": {"turns": [{"role": "user", "parts": [{"text": prompt_text}]}], "turnComplete": True}})
            pcm, words, tool_json, answered = b"", [], "", 0
            while time.time() < deadline:
                ws.s.settimeout(max(.5, deadline - time.time()))
                try:
                    m = ws.recv()
                except (TimeoutError, OSError, EOFError):
                    if tool_json or pcm:
                        break
                    raise
                tc = m.get("toolCall")
                if tc:
                    resp = []
                    for fc in tc.get("functionCalls") or []:
                        a = fc.get("args") or {}
                        if fc.get("name") == "ember_behave" and not tool_json:
                            tool_json = a.get("reply_json") if isinstance(a.get("reply_json"), str) else json.dumps(a)
                        resp.append({"id": fc.get("id"), "name": fc.get("name"),
                                     "response": {"result": "ok: the system is performing it now. Say your line."}})
                    ws.send({"toolResponse": {"functionResponses": resp}})
                    answered = 1
                sc = m.get("serverContent") or {}
                for p in (sc.get("modelTurn") or {}).get("parts") or []:
                    d = p.get("inlineData") or {}
                    if d.get("data") and want_audio:
                        pcm += base64.b64decode(d["data"])
                    if p.get("text") and not p.get("thought"):
                        words.append(p["text"])
                tr = (sc.get("outputTranscription") or {}).get("text")
                if tr:
                    words.append(tr)
                if sc.get("turnComplete"):
                    if pcm or (tool_json and not want_audio):
                        break
                    # the tool call ended the first turn; the spoken line follows
            if not tool_json and not pcm:
                raise RuntimeError("Gemini Live gave nothing back")
            _LIVE_POOL[pool_key] = (ws, time.time())
            return {"text": tool_json or "", "audio": _data_url(pcm_to_wav(pcm)) if pcm else "",
                    "transcript": "".join(words).strip(), "tool": bool(tool_json), "model": model}
        except Exception as e:  # noqa: BLE001 — try the next key
            if ws:
                ws.close()
                _LIVE_POOL.pop(pool_key, None)
            last = e
    raise last or RuntimeError("Gemini Live did not answer")


def groq_call(msgs_or_prompt, max_tokens: int = 900, timeout: float = 12.0, cloud=None) -> str:
    """Groq: the person's own key when they have one, else the team's mind (cloud)."""
    keys = groq_keys()
    msgs = msgs_or_prompt if isinstance(msgs_or_prompt, list) else [{"role": "user", "content": msgs_or_prompt}]
    last = None
    for k in keys[:3]:
        try:
            j = _post_json("https://api.groq.com/openai/v1/chat/completions",
                           {"model": "llama-3.3-70b-versatile", "messages": msgs, "max_tokens": max_tokens,
                            "temperature": 1.0, "response_format": {"type": "json_object"}},
                           timeout, {"Authorization": "Bearer " + k})
            return j["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = e
    if cloud:
        return cloud(msgs)
    raise last or PermissionError("no Groq key saved and the team's mind is not reachable")


def ember_ai_call(msgs_or_prompt, provider: str = "auto", live_mode: str = "gemini-3.8-live", want_audio: bool = False,
                  timeout: float = 15.0, max_tokens: int = 1400, cloud=None, fallback=None) -> dict:
    """The brain the person chose -> {"text", "audio", "by", "model"}.
    `cloud(msgs)` is the team's Groq mind, `fallback(prompt)` the active provider.
    With want_audio, Gemini Live answers and speaks in one turn; Gemini TTS
    voices the line after; groq leaves the voice to the window."""
    provider, live_mode = norm_ai(provider), norm_live(live_mode)
    sys_t, usr = _as_text(msgs_or_prompt)
    if want_audio and live_mode.startswith("gemini-3.8-live") and provider != "groq" and gemini_keys():
        try:
            r = gemini_live_call(usr, sys_t, mode=live_mode, want_audio=True, timeout=timeout)
            text = r["text"] or (json.dumps({"say": r["transcript"], "kind": "answer"}) if r["transcript"] else "")
            if text:
                return {"text": text, "audio": r["audio"], "by": "gemini-live", "model": r["model"],
                        "tool": r["tool"], "transcript": r["transcript"]}
        except Exception:
            pass   # fall through to text, then a voice
    order = {"gemini": ("gemini", "groq", "local"), "groq": ("groq", "gemini", "local"),
             "auto": ("gemini", "groq", "local") if gemini_keys() else ("groq", "local")}[provider]
    text, by, last = "", "", None
    for p in order:
        try:
            if p == "gemini":
                text = gemini_http_call(msgs_or_prompt, max_tokens=max_tokens, timeout=min(timeout, 14))
            elif p == "groq":
                text = groq_call(msgs_or_prompt, max_tokens=max_tokens, timeout=min(timeout, 14), cloud=cloud)
            elif fallback:
                text = fallback(usr if not sys_t else sys_t + "\n\n" + usr)
            if text:
                by = p
                break
        except Exception as e:  # noqa: BLE001 — next brain
            last = e
    if not text:
        raise last or PermissionError("no mind switched on")
    return {"text": text, "audio": "", "by": by, "model": ""}


def voice_for(say: str, live_mode: str) -> str:
    """Server-side audio for a line, or "" (the window then uses its own voice)."""
    if norm_live(live_mode) == "groq" or not str(say or "").strip() or not gemini_keys():
        return ""
    try:
        return gemini_tts(say)["audio"]
    except Exception:
        return ""


EMBER_JS = r"""
/* Ember: Aethron's companion. One canvas over the window, pointer-events
   off; a press on Ember itself is taken before the page sees it, nothing
   else ever is. Driven by window.ember.signal(name, data) from the app. */
(()=>{
if(window.ember)return;
const qs=location.search||'';
let CFG={on:true,att:'sassy',fit:'auto',ai:'auto',liveMode:'gemini-3.8-live',agency:'safe',notify:'on'};
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
#ember-menu button:hover{background:#2a2622}
#ember-offer{position:fixed;z-index:8802;width:min(300px,calc(100vw - 24px));box-sizing:border-box;padding:12px 12px 10px 14px;border-radius:16px;background:rgba(30,27,25,.92);-webkit-backdrop-filter:blur(20px) saturate(1.5);backdrop-filter:blur(20px) saturate(1.5);border:1px solid rgba(255,255,255,.1);box-shadow:0 18px 50px rgba(0,0,0,.45),0 2px 6px rgba(0,0,0,.25);font:500 13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:#f1e8e0;animation:emberIn .34s cubic-bezier(.2,.9,.25,1.12)}
#ember-offer[hidden]{display:none!important}
#ember-offer p{margin:0 0 10px}
#ember-offer .b{display:flex;justify-content:flex-end;gap:6px}
#ember-offer button{all:unset;cursor:pointer;padding:7px 14px;border-radius:999px;font-weight:600;font-size:12.5px;color:#e9dfd6}
#ember-offer .no:hover{background:rgba(255,255,255,.08)}
#ember-offer .yes{background:#d97757;color:#fff}#ember-offer .yes:hover{background:#e3875f}
@media (prefers-reduced-motion:reduce){#ember-offer{animation:none}}`;
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
const L=a=>{if(!Array.isArray(a))return a;const v=a[{sweet:0,sassy:1,savage:2}[ATT()]];return Array.isArray(v)?v[Math.floor(Math.random()*v.length)]:v};
function autoFit(){const d=new Date(),m=d.getMonth()+1,day=d.getDate();
  if(m===12&&day>=24&&day<=26)return'santa';if((m===12&&day===31)||(m===1&&day===1))return'party';if(m===10&&day>=24)return'witch';return'none'}
let FITOV=null;
const outfit=()=>FITOV&&FITOV.until>Date.now()?FITOV.n:(CFG.fit==='auto'?autoFit():CFG.fit);
const NO_ERR={};const errandBlocked=k=>(NO_ERR.all||0)>now||(NO_ERR[k]||0)>now;

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
  limp:t=>[[-1.15,.95+.04*Math.sin(t*3)],[1.15,.95+.04*Math.sin(t*3+1)]],
  /* an invented behaviour's own hand positions, with an optional wiggle */
  gen:t=>{const h=cur_('hands')||[[-1.22,.62],[1.22,.62]],w=cur_('wig')||[0,0];return[[h[0][0]+w[0]*Math.sin(t*w[1]),h[0][1]+w[0]*Math.cos(t*w[1])],[h[1][0]-w[0]*Math.sin(t*w[1]+1),h[1][1]+w[0]*Math.cos(t*w[1]+1)]]}};

const E={x:0,y:0,vx:0,vy:0,tx:0,ty:0,speed:1,perch:0,landed:0,mode:'fly',q:0,qv:0,tilt:0,tv:0,spin:0,spinOn:0,r:1,rT:1,alpha:1,alphaT:1,
  tipX:0,tipXv:0,tipH:1.85,tipHv:0,side:[0,0],gx:0,gy:0,gtx:0,gty:0,ms:[0,0],blink:0,blinkT:0,nextBlink:2,
  F:face('calm'),FT:face('calm'),pose:'rest',hands:[{x:0,y:0,vx:0,vy:0},{x:0,y:0,vx:0,vy:0}],
  bubble:null,look:null,antic:0,pointDir:[1,0],sweat:0,zz:0,think:0,hum:0,shadesDown:0,flap:0,flapT:0,shadesUntil:0,tint:null};
const parts=[];
/* A BODY MADE OF NUMBERS MUST NEVER BE STOPPED BY ONE BAD NUMBER. A particle
   born with a non-finite field is refused; a position, joint or face dial that
   goes non-finite is put back to rest every frame; a frame that throws is
   caught and the loop goes on. Each repair is recorded with where it came from
   (ember.health()), so a test still fails on it and names the source, while a
   person never sees Ember freeze. */
const BAD=[];function bad(what){if(BAD.length<12)BAD.push(what+' @ '+String(new Error().stack||'').split('\n').slice(2,6).map(x=>x.trim()).join(' | '))}
const fin=v=>typeof v!=='number'||isFinite(v);
parts.push=function(...ps){for(const p of ps){if(!p||![p.x,p.y,p.vx,p.vy,p.s,p.d,p.l].every(fin)||!(p.d>0)){bad('particle '+(p&&p.k));continue}Array.prototype.push.call(parts,p)}return parts.length};

/* ===== the body =====
   Every part Ember moves is a JOINT on a spring: the body's lean, crouch,
   turn and rise, and on each arm the shoulder, elbow, wrist, grip and reach.
   Whatever moves a joint (its mind, a reflex, a named pose) only sets where
   the joint should be; the springs carry it there with weight and a little
   overshoot, so a hand travels in an arc, not along a line. On top runs what
   a body does by itself: breathing, a slow shift of weight, hands that are
   never quite still, and while it talks, beat gestures and nods in time with
   its own voice. Its feelings drift on their own and colour all of it. None
   of that is a behaviour: it is how a living body moves while it does
   whatever its mind chose. */
const DEG=Math.PI/180,ARM_U=.56,ARM_F=.54,SHO=[[-.84,.32],[.84,.32]],BODY0={lean:0,crouch:0,turn:0,rise:0};
const J={b:{lean:0,crouch:0,turn:0,rise:0},bv:{lean:0,crouch:0,turn:0,rise:0},
  a:[{sh:-10,el:125,wr:0,grip:0,reach:1},{sh:-10,el:125,wr:0,grip:0,reach:1}],
  av:[{sh:0,el:0,wr:0,grip:0,reach:0},{sh:0,el:0,wr:0,grip:0,reach:0}],
  hold:[null,null],body:null,osc:[],speed:1,held:0,elbow:[{x:0,y:0},{x:0,y:0}],beats:[],lift:0};
/* how it feels: valence -1..1, arousal 0..1. It changes by itself, the way a
   mood does, with no reason needed; the mind can set it, and then it is left
   alone for a while. */
const AFF={v:.1,a:.45,tv:.1,ta:.45,set:-99,next:9};
const TEMPER=()=>ATT()==='sweet'?[.4,.55]:ATT()==='savage'?[-.1,.5]:[.1,.45];
function gauss(){return(Math.random()+Math.random()+Math.random()-1.5)/.5}
const moodWord=(v,a)=>v>.3?(a>.6?'excited':a<.3?'cosy':'content'):v<-.3?(a>.6?'irritable':a<.3?'low':'grumpy'):(a>.65?'restless':a<.25?'drowsy':'neutral');
function feel(f){if(!f)return;if(typeof f.v==='number')AFF.tv=cl(f.v,-1,1);if(typeof f.a==='number')AFF.ta=cl(f.a,0,1);AFF.set=now}
/* shoulder 0 = the arm hangs down, 90 = straight out to its side, 180 = up;
   elbow + bends the forearm further outward/up, - folds it inward */
function armFK(i,j){const s=i?1:-1,a=j.sh*DEG,b=(j.sh+j.el)*DEG,u=ARM_U*j.reach,f=ARM_F*j.reach,ex=SHO[i][0]+s*Math.sin(a)*u,ey=SHO[i][1]+Math.cos(a)*u;
  return[ex,ey,ex+s*Math.sin(b)*f,ey+Math.cos(b)*f]}
/* a hand position -> the joint angles that put it there (the bend nearest how the arm already is) */
function armIK(i,hx,hy,prev){const s=i?1:-1,dx=(hx-SHO[i][0])*s,dy=hy-SHO[i][1],d=Math.max(1e-4,Math.hypot(dx,dy)),reach=Math.max(1,d/((ARM_U+ARM_F)*.999)),u=ARM_U*reach,f=ARM_F*reach;
  const phi=Math.atan2(dx,dy),al=Math.acos(cl((u*u+d*d-f*f)/(2*u*d),-1,1)),be=Math.PI-Math.acos(cl((u*u+f*f-d*d)/(2*u*f),-1,1));
  const out={sh:(phi-al)/DEG,el:be/DEG};out.reach=reach;return out}
function applyJoints(s){if(!s)return;let any=0;
  if(s.arms){for(const [k,i] of[['L',0],['R',1]]){const v=s.arms[k];if(!v)continue;const c=J.a[i];J.hold[i]=Object.assign({sh:c.sh,el:c.el,wr:c.wr,grip:c.grip,reach:1},J.hold[i]||{},v);any=1}}
  else if(s.pose)J.hold=[null,null];
  if(s.body){J.body=Object.assign({},J.body||BODY0,s.body);any=1}
  J.osc=(s.osc||[]).map(o=>Object.assign({t0:now},o));if(J.osc.length)any=1;
  if(s.speed){J.speed=cl(+s.speed||1,.3,3);any=1}
  if(s.feel)feel(s.feel);
  if(any)J.held=1}
function releaseJoints(){J.hold=[null,null];J.body=null;J.osc=[];J.speed=1;J.held=0}
const isTalking=()=>!!((E.bubble&&E.bubble.type&&E.bubble.t*E.bubble.cps<E.bubble.text.length)||(VOICE_EL&&!VOICE_EL.paused&&!VOICE_EL.ended)||E.flap>.3);
/* a beat: the small stroke of a hand, a nod, a shift of weight that comes
   with stressed words. Nobody plans these; they come with talking. */
function beat(){if(REDUCE)return;const e=.6+AFF.a*.8,r=Math.random(),arms=r<.55?[1]:r<.8?[0]:[0,1],d=rnd(.28,.55);
  for(const i of arms)if(!J.hold[i])J.beats.push({i,t0:now,d,sh:rnd(12,30)*e,el:rnd(15,38)*e*(J.a[i].el>=0?1:-1),wr:rnd(-25,25)});
  J.beats.push({i:2,t0:now,d:d*1.3,lean:rnd(-7,7)*e,turn:Math.random()<.35?rnd(-.25,.25):0});
  E.qv-=rnd(.5,1.1)*e;if(Math.random()<.25)E.brow=Math.max(E.brow||0,.35)}
/* how far into a beat's stroke: up and back down again */
const beatEnv=b=>{const k=(now-b.t0)/b.d;return k<0||k>1?0:Math.sin(k*Math.PI)};
function physio(dt){
  if(now-AFF.set>40){AFF.next-=dt;if(AFF.next<=0){AFF.next=rnd(7,24);const t=TEMPER(),v0=AFF.tv,a0=AFF.ta;AFF.tv=cl(t[0]+gauss()*.4,-1,1);AFF.ta=cl(t[1]+gauss()*.24+(N.energy-.6)*.35,0,1);
    if(Math.abs(AFF.tv-v0)+Math.abs(AFF.ta-a0)>.45)try{mindEvent('mood',{what:'a mood came over Ember for no reason: '+moodWord(AFF.tv,AFF.ta)})}catch(_){}}}
  const k=Math.min(1,dt/3.5);AFF.v+=(AFF.tv-AFF.v)*k;AFF.a+=(AFF.ta-AFF.a)*k;
  E.bph=(E.bph||0)+dt*(1.25+AFF.a*2.3)*(E.zz?.55:1);E.breath=REDUCE?0:Math.sin(E.bph);
  E.brow=Math.max(0,(E.brow||0)-dt*1.3);
  const tk=isTalking();J.lift+=((tk?1:0)-J.lift)*Math.min(1,dt*3);J.beats=J.beats.filter(b=>now-b.t0<b.d);
  if(tk){E.beatIn=(E.beatIn==null?.2:E.beatIn)-dt;if(E.beatIn<=0){E.beatIn=rnd(.32,.85)/(.65+AFF.a*.7);beat()}}else E.beatIn=rnd(.08,.3);
  if(V.state==='listening'&&now-V.last<1.5){E.nodIn=(E.nodIn==null?1:E.nodIn)-dt;if(E.nodIn<=0){E.nodIn=rnd(1.3,2.8);if(!REDUCE)E.qv-=rnd(.6,1)}}
  const t=Object.assign({},J.body||BODY0),g=REDUCE?0:.6+AFF.a*.6;
  t.lean+=(Math.sin(now*.31)*2.4+Math.sin(now*.87+1.3)*1.2)*g;t.rise+=Math.sin(now*.53+.4)*.05*g;
  t.crouch+=(REDUCE?0:-E.breath*.03)+(AFF.v<-.3?(-AFF.v-.3)*.3:0)-(AFF.a-.5)*.14;
  if(V.state==='listening'||(CHAT.open&&now-(CHAT.typing||-9)<2))t.lean+=4,t.turn+=.08;
  for(const b of J.beats)if(b.i===2){const e=beatEnv(b);t.lean+=b.lean*e;t.turn+=b.turn*e}
  for(const o of J.osc)if(o.j[0]==='B'){const key=o.j.slice(2);if(key in t)t[key]+=o.amp*Math.sin((now-o.t0)*o.hz*Math.PI*2)}
  const kb=Math.min(700,90*J.speed),db=2*Math.sqrt(kb)*.8;
  for(const key in BODY0){if(REDUCE){J.b[key]=t[key];continue}J.bv[key]+=((t[key]-J.b[key])*kb-J.bv[key]*db)*dt;J.b[key]+=J.bv[key]*dt}
  J.b.turn=cl(J.b.turn,-1.2,1.2)}

/* ===== the nervous system =====
   A living body between decisions is never a statue. Its attention drifts to
   whatever moves on screen, it shifts how it holds itself, wanders off to
   look at something, stretches when it has sat too long, fidgets, dozes when
   it is tired. None of that is a sequence: every impulse draws its targets
   fresh — a real thing on the screen, a new place, a new way to hold its
   arms, a new face — from how Ember feels and how long it has been still,
   and what it did a moment ago weighs against doing it again. It never
   speaks: words are the mind's. Its mind's plans always come first. */
const NS={quiet:0,stillT:0,lastGo:-99,fresh:[],kinds:[],count:0};
const SIGHTS='.msg,.mtext,.atext,.pitem,.rcard,.jobc,.truth,.composer,#sendbtn,.newbtn,#setbtn,h1,h2,h3,img,button,a,input,textarea';
function sights(){const out=[];for(const el of document.querySelectorAll(SIGHTS)){if(out.length>60)break;if(el.closest&&el.closest('#ember-talk,#ember-menu,#ember-tour,#ember-offer'))continue;
  const r=rect(el);if(!r||r.w<14||r.h<10||r.b<8||r.t>H-8||r.r<8||r.l>W-8)continue;out.push({el,r})}return out}
function freshSight(){NS.fresh=NS.fresh.filter(f=>now-f.t<25&&f.el.isConnected);for(let i=NS.fresh.length-1;i>=0;i--){const r=rect(NS.fresh[i].el);if(r&&r.w>14&&r.h>10&&r.b>0&&r.t<H){const el=NS.fresh[i].el;NS.fresh.splice(i,1);return{el,r}}}return null}
function watchScreen(){try{new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes){if(n.nodeType!==1||!n.isConnected||(n.id||'').startsWith('ember')||n===cv)continue;
  if(n.closest&&n.closest('#ember-talk,#ember-menu,#ember-tour,#ember-offer'))continue;NS.fresh.push({el:n,t:now})}if(NS.fresh.length>10)NS.fresh=NS.fresh.slice(-10)}).observe(document.body,{childList:true,subtree:true})}catch(_){}}
const ptOf=el=>()=>{const r=rect(el);return r?{x:r.cx,y:r.cy}:{x:E.x,y:E.y+40}};
/* a face for the moment, from how it feels, never the same twice */
function mien(o){const v=AFF.v,a=AFF.a;return Object.assign({mc:cl(.12+v*.45+rnd(-.12,.12),-.6,.7),bH:cl(rnd(-.05,.12)+(a-.45)*.25,-.3,.4),lidU:cl(.14+(.5-a)*.4+rnd(0,.12),0,.8),
  mas:rnd(-.35,.35),bT:v<-.35?rnd(.05,.3):0,pup:cl(1+(a-.5)*.2,.6,1.2)},o||{})}
/* an arm held some new way: an angle pair drawn fresh until the hand lands somewhere a hand can rest */
function armPose(i,high){for(let k=0;k<14;k++){const a={sh:rnd(-55,high?165:95),el:rnd(-150,150),wr:rnd(-25,25),grip:Math.random()<.3?rnd(.4,.9):rnd(0,.25),reach:rnd(.92,1.12)};
    const f=armFK(i,a),d=Math.hypot(f[2],f[3]-.2);if(d>.5&&d<1.75&&f[3]>-1.4&&!(Math.abs(f[2])<.5&&f[3]>-.1&&f[3]<.5))return a}return{sh:-10,el:125,reach:1}}
/* the angles that put a hand near a point on or around the body */
function reachFor(i,x,y){const a=armIK(i,x,y,null);return{sh:a.sh,el:a.el,reach:a.reach,wr:rnd(-20,20),grip:rnd(0,.3)}}
const AR=a=>{const o={};if(a[0])o.L=a[0];if(a[1])o.R=a[1];return o};
function spot(){const m=area(),here={x:E.x,y:E.y},sg=sights();for(let k=0;k<10;k++){let p;
    if(sg.length&&Math.random()<.55){const s=sg[Math.floor(Math.random()*sg.length)].r,side=Math.random()<.5?-1:1;p={x:side<0?s.l-R*1.8:s.r+R*1.8,y:s.t+Math.min(s.h*.5,R*2)}}
    else p={x:rnd(m.l+R*2.5,m.r-R*2.5),y:rnd(m.t+R*2.8,m.b-R*2.4)};
    p.x=cl(p.x,m.l+R*2,m.r-R*2);p.y=cl(p.y,m.t+R*2.6,m.b-R*2);if(Math.hypot(p.x-here.x,p.y-here.y)>R*3.5)return p}return null}
function seatSpot(){if(DESK)return deskPerch();const c=[];const cp=composer();if(cp)c.push({x:cl(cp.l+cp.w*rnd(.15,.85),R*2,W-R*2),y:cp.t-R*1.02,perch:1});
  for(const s of sights())if(s.r.w>R*5&&s.r.t>R*3&&/^(msg|rcard|pitem|jobc)/.test(String(s.el.className||'')))c.push({x:cl(s.r.l+s.r.w*rnd(.2,.8),R*2,W-R*2),y:s.r.t-R*1.02,perch:1});
  return c.length?c[Math.floor(Math.random()*c.length)]:null}
const IMPULSES={
  notice(){const f=freshSight(),sg=sights(),t=f||(sg.length?sg[Math.floor(Math.random()*sg.length)]:null);if(!t)return null;
    const dx=t.r.cx-E.x,dir=Math.sign(dx)||1,pt=ptOf(t.el),st=[{d:rnd(.3,.6),look:{pt},fx:mien({bH:rnd(.08,.3),lidU:rnd(0,.12)}),body:{lean:dir*rnd(3,10),turn:dir*rnd(.15,.45)}}];
    const r=Math.random();
    if(r<.3){const s=dir>0?1:0,dy=t.r.cy-E.y,ang=Math.atan2(Math.abs(dx),dy)/DEG,a={sh:cl(ang,10,170),el:rnd(-10,15),reach:rnd(1.05,1.3),wr:0,grip:0};st.push({d:rnd(1.2,2.4),look:{pt},arms:AR(s?[null,a]:[a,null]),fx:mien({bH:rnd(.1,.3)})})}
    else if(r<.55&&Math.hypot(dx,t.r.cy-E.y)>R*5)st.push({d:rnd(1.6,2.6),look:{pt},fx:mien({bH:rnd(.05,.2)}),fn:()=>{const m=area();go({x:cl(t.r.cx-dir*(t.r.w/2+R*1.9),m.l+R*2,m.r-R*2),y:cl(t.r.cy-R*.6,m.t+R*2.6,m.b-R*2)},{speed:rnd(.4,.7)})}},{d:rnd(1,2.2),look:{pt},fx:mien()});
    else st.push({d:rnd(1.4,3.2),look:{pt},fx:mien({lidU:rnd(.15,.4)}),body:{lean:dir*rnd(2,8),turn:dir*rnd(.1,.35),crouch:rnd(-.05,.12)},arms:Math.random()<.4?AR([null,reachFor(1,rnd(.1,.4),rnd(.55,.8))]):null});
    return st},
  posture(){const both=Math.random()<.4,hi=AFF.a>.6;let a=[Math.random()<.65?armPose(0,hi):null,Math.random()<.75?armPose(1,hi):null];
    if(both&&a[1])a[0]=Math.random()<.5?Object.assign({},a[1]):armPose(0,hi);
    return[{d:rnd(3.5,10),arms:AR(a),body:{lean:rnd(-12,12),crouch:rnd(-.15,.25),turn:rnd(-.4,.4)},fx:mien(),speed:rnd(.5,1.1),look:Math.random()<.5?null:{dir:[rnd(-.9,.9),rnd(-.5,.4)]}}]},
  wander(){const p=spot();if(!p)return null;NS.lastGo=now;const sp=rnd(.4,.85)*(.6+N.energy*.6);
    return[{d:rnd(.25,.45),look:{pt:{x:p.x,y:p.y}},fx:mien({bH:rnd(0,.15)}),body:{crouch:.15},fn:()=>go(p,{speed:sp})},{d:rnd(2,3.5),look:{pt:{x:p.x,y:p.y}},fx:mien(),body:{crouch:-.05}},{d:rnd(.8,2),fx:mien()}]},
  settle(){const p=seatSpot();if(!p)return null;NS.lastGo=now;const s=rnd(.4,.7);
    return[{d:rnd(2,3),fx:mien(),fn:()=>go(p,{speed:s})},{d:rnd(4,10),fx:mien({lidU:rnd(.2,.45)}),body:{crouch:rnd(.05,.25),lean:rnd(-8,8)},arms:Math.random()<.5?AR([armPose(0,false),armPose(1,false)]):null}]},
  stretch(){NS.stillT=now;const up=()=>({sh:rnd(148,176),el:rnd(-12,18),reach:rnd(1.2,1.5),wr:rnd(-20,20),grip:rnd(0,.3)});
    return[{d:rnd(.6,1),arms:AR([up(),up()]),body:{crouch:rnd(-.5,-.3),lean:rnd(-6,6)},fx:mien({lidU:rnd(.6,.85),mo:rnd(.3,.9),bH:rnd(.1,.25)}),speed:.55,look:{dir:[0,-1]}},
      {d:rnd(.9,1.6),arms:AR([up(),up()]),body:{crouch:rnd(-.55,-.35),lean:rnd(-14,14)},fx:mien({lidU:rnd(.7,.95),mo:rnd(.5,1.1)}),speed:.5},
      {d:rnd(.8,1.4),body:{crouch:rnd(.1,.25)},fx:mien({lidU:rnd(.2,.4)}),speed:.8}]},
  fidget(){const i=Math.random()<.65?1:0,s=i?1:-1,kind=Math.floor(Math.random()*4);
    if(kind===0){const a=reachFor(i,s*rnd(.15,.5),rnd(.5,.9));return[{d:rnd(1.5,3.5),arms:AR(i?[null,a]:[a,null]),osc:[{j:(i?'R':'L')+'.wr',amp:rnd(12,30),t0:now,hz:rnd(1.5,4)}],look:{pt:()=>({x:E.hands[i].x,y:E.hands[i].y})},fx:mien({lidU:rnd(.2,.45)})}]}
    if(kind===1){const a=reachFor(i,s*rnd(.55,.85),rnd(-.75,-.35));return[{d:rnd(1.2,2.5),arms:AR(i?[null,a]:[a,null]),osc:[{j:(i?'R':'L')+'.el',amp:rnd(6,12),t0:now,hz:rnd(5,8)}],fx:mien({bH:rnd(.05,.2),mas:s*rnd(.2,.5)}),body:{turn:-s*rnd(.1,.3)},look:{dir:[rnd(-.5,.5),-.6]}}]}
    if(kind===2){const a=reachFor(i,s*rnd(.95,1.2),rnd(.75,1));return[{d:rnd(1.5,3.5),arms:AR(i?[null,a]:[a,null]),osc:[{j:(i?'R':'L')+'.el',amp:rnd(5,10),t0:now,hz:rnd(3,6)}],fx:mien(),look:{dir:[s*.4,.5]}}]}
    const a=reachFor(0,rnd(-.4,-.15),rnd(.75,.95)),b=reachFor(1,rnd(.15,.4),rnd(.75,.95));
    return[{d:rnd(2,4),arms:AR([a,b]),osc:[{j:'R.wr',amp:rnd(10,22),t0:now,hz:rnd(2,4)}],fx:mien({lidU:rnd(.25,.5)}),look:{dir:[rnd(-.3,.3),.7]}}]},
  bounce(){return[{d:rnd(1,2.2),osc:[{j:'B.rise',amp:rnd(.15,.35),t0:now,hz:rnd(1.8,3.2)},{j:'B.lean',amp:rnd(4,9),t0:now,hz:rnd(.9,1.6)}],
    arms:Math.random()<.5?AR([armPose(0,true),armPose(1,true)]):null,fx:mien({mc:rnd(.3,.7),bH:rnd(.1,.3)}),fn:()=>{if(Math.random()<.4)effects({hop:rnd(.2,.5)})}}]},
  regard(){const warm=AFF.v>0;return[{d:rnd(1.5,3.5),look:'viewer',fx:mien(warm?{mc:rnd(.3,.65),lidU:rnd(.25,.45)}:{lidU:rnd(.4,.6),mc:rnd(-.2,.05)}),
    body:{turn:rnd(-.15,.15),lean:rnd(-5,5)},fn:()=>{if(Math.random()<.5)E.blinkT=.16}}]},
  gaze(){return[{d:rnd(2.5,6),look:{dir:[rnd(-1,1),rnd(-.8,.2)]},fx:mien({lidU:rnd(.3,.55),bH:rnd(-.05,.1)}),body:{crouch:rnd(0,.2),lean:rnd(-10,10)},
    arms:Math.random()<.5?AR([null,reachFor(1,rnd(.1,.35),rnd(.55,.8))]):null,speed:.6}]},
  doze(){const d=rnd(18,50);return[{d:rnd(1,1.6),fx:mien({lidU:.7}),body:{crouch:.15}},{d,face:'asleep',pose:'limp',sleep:1,body:{crouch:.3,lean:rnd(-10,10)}},{d:rnd(.8,1.4),fx:mien({lidU:.5,bH:.2}),body:{crouch:.05}}]},
};
function urges(){const still=now-NS.stillT,c=N.curiosity,e=N.energy,b=N.boredom,a=AFF.a,v=AFF.v,idle=now-cur.t;
  if(AG.offer)return{posture:1,fidget:.5+a*.6,regard:1.2,gaze:.3};   /* waiting for an answer: it stays by its card */
  return{notice:.9+c*.9+(NS.fresh.length?1.6:0),posture:1,wander:(.35+e*.6+b*.8+a*.4)*(now-NS.lastGo<20?.25:1),settle:(.25+(1-e)*.6)*(E.perch?.1:1),
    stretch:still>60?1.4:.04,fidget:.5+a*.6+b*.5,bounce:a>.6&&v>.1?.7:.04,regard:.4+N.social*.6+N.fond*.4,gaze:.3+(1-a)*.6,doze:e<.25&&idle>120?1.4:0}}
function impulse(){const u=urges(),rec=k=>NS.kinds.filter(x=>x===k).length;let best=null,bw=-1;
  for(const k in u){const w=u[k]*rnd(.4,1.6)/(1+rec(k)*1.8);if(w>bw){bw=w;best=k}}
  const st=best&&IMPULSES[best]();if(!st||!st.length)return false;NS.kinds.push(best);NS.kinds=NS.kinds.slice(-4);NS.count++;
  const dur=st.reduce((t,x)=>t+(x.d||0),0);if(best!=='stretch'&&best!=='wander'&&best!=='settle'&&best!=='bounce')NS.stillT=NS.stillT||now;
  const me={k:'ns_'+best,by:'body',a:{w:()=>0,run(){},dur:[dur,dur]},t:0,dur:dur+.2};act=me;play(st,()=>{if(act===me)me.t=me.dur+1});SEQ.ns=1;return true}
/* nothing chosen: a beat of simply being, then the next impulse */
function be(){if(!NS.stillT)NS.stillT=now;if(now>=NS.quiet&&!REDUCE&&!SEQ&&Math.random()<.85&&impulse())return;
  act={k:'be',by:'body',a:{w:()=>0,run(){},dur:[2,2]},t:0,dur:rnd(1.2,4.5)/(.6+AFF.a)}}
/* what the body shows of a feeling, on whatever face it is making */
function feltFace(F){const D=Object.assign({},F),held=!!(cur_('face')||cur_('fx')||mood||(act&&act.k==='m_wait'&&act.face)),w=held?.4:1,v=AFF.v*w,a=(AFF.a-.45)*w,n=REDUCE?0:1;
  if(F.name==='asleep'||F.name==='dizzy')return D;
  D.mc=cl(D.mc+v*.32+n*Math.sin(now*.43)*.03,-1,1);D.bH=D.bH+a*.14+(E.brow||0)*.22+n*Math.sin(now*.37+1)*.025;
  D.lidU=cl(D.lidU-a*.2+(v<-.4?(-v-.4)*.22:0),0,1);D.flame=cl(D.flame+a*.25,.5,1.5);D.pup=cl(D.pup+a*.12,.5,1.4);
  if(v<-.3&&a>.15)D.bT=D.bT+(-v-.3)*.6*w;else if(v<-.3)D.bT=D.bT-(-v-.3)*.35*w;
  if(v>.45)D.blush=Math.min(1,D.blush+(v-.45)*.4);return D}

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
  E.tx=cl(p.x,R*1.5,W-R*1.5);E.ty=cl(p.y,p.notch?R*1.2:R*2.4,H-R*1.4);E.perch=!!p.perch;E.seat=null;E.speed=o.speed||1;E.landed=0;
  if(!hideUntil&&!E.fs){if(p.notch)E.rT=.6;else if(E.rT===.6)E.rT=1}
  if(far&&!REDUCE&&E.mode==='fly'&&!o.now){E.antic=.26;E.anticDir=[Math.sign(p.x-E.x)||1,Math.sign(p.y-E.y)];E.look={pt:{x:p.x,y:p.y},until:now+.6}}}

/* ===== the brain ===== */
const N={energy:.75,curiosity:.6,social:.35,boredom:.25,playfulness:.5,annoy:.05,fond:.3};
{const nd=!automation&&store.get('ember_drives',null);if(nd&&typeof nd==='object')for(const k of Object.keys(N))if(typeof nd[k]==='number'&&isFinite(nd[k]))N[k]=cl(nd[k],0,1)}
let driveSaveT=0;
function bump(k,v){N[k]=cl((N[k]||0)+v,0,1)}
function driveFx(f){if(!f||f.name==='asleep'||f.name==='dizzy')return f;
  f.lidU=cl(f.lidU+(1-N.energy)*.14-N.curiosity*.05,0,1);
  f.flame=cl(f.flame+(N.energy-.6)*.22+((N.playfulness||.5)-.5)*.15,.55,1.45);
  f.bright=cl(f.bright+(N.energy-.6)*.12,.75,1.25);
  f.mc=cl(f.mc+(N.fond-.3)*.18-N.annoy*.25,-1,1);
  return f}
const annoyed=()=>N.annoy>.55,idleFace=()=>ATT()==='sweet'?'neutral':(ATT()==='savage'?'unimpressed':'calm');
/* a person needs time to read: about 13 characters a second, never under 3s */
const readTime=t=>Math.min(14,2.6+String(t||'').length/13);
function say(t,d,o){o=o||{};t=String(t||'');if(!t&&!o.dots)return;const ty=o.type?t.length/30:0;E.bubble={text:t,t:0,d:ty+Math.max(d||0,o.dots?0:readTime(t)),type:!!o.type,cps:30,last:-1,dots:!!o.dots}}
let SEQ=null;
function play(steps,done){releaseJoints();SEQ={steps,i:-1,t:0,done};next()}
function next(){SEQ.i++;SEQ.t=0;const s=SEQ.steps[SEQ.i];if(!s){const d=SEQ.done;SEQ=null;releaseJoints();d&&d();return}if(s.say){const t=L(s.say);if(t)say(t,s.sayD,{type:1})}applyJoints(s);if(s.fn)s.fn()}
function seqTick(dt){if(!SEQ)return;const s=SEQ.steps[SEQ.i];SEQ.t+=dt;if(SEQ.t>=s.d)next()}
const cur_=k=>SEQ&&SEQ.steps[SEQ.i]&&SEQ.steps[SEQ.i][k];
let base={pose:'rest',face:'calm',look:null},act=null,mood=null;
const CTX={project:'',job:null,last:'',lastT:0,issue:'',issueT:0};

/* Ember has no routines. What it does is what its mind decides (below):
   where to go, what to do there, what to say and how to hold every joint.
   Without a mind it only lives — breathes, blinks, looks about — and
   reacts with its body (a flinch, a fall) the way any body would. */
const maybe=(a,p)=>Math.random()<p?L(a):'';
function visible(sel){const out=[];for(const el of document.querySelectorAll(sel)){const r=rect(el);if(r&&r.w>30&&r.h>14&&r.t>0&&r.b<H&&r.l>=0&&r.r<=W)out.push(el)}return out}
function interest(){for(const sel of['.jobc','.truth','.msg','.rcard']){const r=lastOf(sel);if(r&&r.w>40)return{r,sel}}return null}
function puff(x,y,n,kind){for(let i=0;i<(n||6);i++)parts.push({k:kind||'smoke',x:x+rnd(-14,14),y:y+rnd(-4,6),vx:rnd(-30,30),vy:rnd(-40,-10),l:1,d:rnd(.6,1.1),s:rnd(3,6),c:'255,210,150'})}
function sparkle(r,n){for(let i=0;i<(n||8);i++)parts.push({k:'spark',x:rnd(r.l,r.r),y:rnd(r.t,r.b),vx:rnd(-20,20),vy:rnd(-40,-5),l:1,d:rnd(.4,.8),s:rnd(1.2,2.2),c:'255,230,180'})}
const ACTS_LOG=[];
function celebrate(){E.vy=-Math.max(520,H*.7);E.q=.6;E.spinOn=1;
  for(let i=0;i<50;i++){const a=Math.random()*Math.PI*2,s=rnd(130,400);parts.push({k:'spark',x:E.x,y:E.y,vx:Math.cos(a)*s,vy:Math.sin(a)*s-140,l:1,d:rnd(.6,1.5),s:rnd(1.4,3.2),c:Math.random()<.5?'255,215,130':'255,120,90'})}}

/* ===== the floor gives way =====
   Ember remembers what it is sitting on. When that thing goes (a new page
   opened, the chat box gone), it is startled and falls — gravity, a bounce,
   a puff of dust — lies there a moment, then gets up and pretends nothing
   happened. A new page with nothing taken from under it gets a look round. */
function seatUnder(){let el=document.elementFromPoint(cl(E.x,1,W-1),cl(E.y+R*1.3,1,H-1));if(!el||el===document.body||el===document.documentElement)return null;
  if(talk.contains(el)||menu.contains(el))return null;const r=rect(el);return r?{el,t:r.t,l:r.l}:null}
function seatGone(){const s=E.seat;if(!s)return false;if(!s.el.isConnected)return true;const r=rect(s.el);if(!r)return true;
  const st=getComputedStyle(s.el);if(st.visibility==='hidden'||+st.opacity<.05)return true;return Math.abs(r.t-s.t)>R*1.4||Math.abs(r.l-s.l)>R*3}
function fall(){if(DRV.on||TW.on||CHAT.open||V.state!=='idle'||E.mode!=='fly')return;SEQ=null;act=null;mood=null;E.seat=null;E.perch=0;E.landed=0;E.fallHit=0;
  if(REDUCE){play([{d:1.2,face:'shocked'}]);go(SP.rest);afterFall();return}
  E.mode='fall';E.vy=-170;E.vx=rnd(-40,40);E.q=.25}
function afterFall(){bump('annoy',.12);E.dizzy=1.3;try{mindEvent('fell',{what:'the thing Ember sat on vanished and it fell to the bottom of the window'})}catch(_){}}
let PAGE='',PAGE_T=0;
function pageSig(){try{return typeof S!=='undefined'?(S.view||'home')+'|'+(S.cur||''):location.pathname}catch(_){return''}}
function pageTick(){if(DESK||!started)return;const sig=pageSig();if(!PAGE){PAGE=sig;return}if(sig===PAGE)return;const was=PAGE;PAGE=sig;PAGE_T=now;
  const where=sig.split('|');try{mindEvent('page',{what:'they opened '+(where[1]?'project '+where[1]:where[0])})}catch(_){}
  setTimeout(()=>{if(E.mode!=='fly'||DRV.on||TW.on)return;if(E.seat&&seatGone())fall()},350)}
/* ===== the mind =====
   Every minute or two, and at once when something happens, Ember tells the
   team's mind model what it perceives and gets back what it wants to do and
   why. The model can only move the body with data: behaviours Ember knows,
   places, faces, poses, effects — and when nothing it knows fits, it invents
   a behaviour as keyframes over the same body, which plays now and is kept.
   No mind (none switched on, or it failed): the built-in behaviour runs. */
const MIND={live:0,off:0,why:'',retry:0,busy:0,next:6,fail:0,plan:[],thought:'',focus:'',snap:null,hist:[],ev:[],done:[],hour:[],perHour:40,cueUntil:0,key:-99,lastAsk:-99,asks:0};
function mindOutcome(){const s=MIND.snap;if(!s)return'';
  if(CHAT.lastChat>s.t)return'they talked with you';
  if(s.asked&&CHAT.ignored>s.ig)return'you asked them a question and they ignored it';
  if(cur.boops.some(t=>t>s.t))return'they poked you';
  if(cur.t>s.t&&Math.hypot(cur.x-E.x,cur.y-E.y)<R*4)return'their cursor came close to you';
  if(now-cur.t>45&&now-MIND.key>45)return'they have been quiet and still';
  return'they kept working quietly'}
const MSTAT=(()=>{const d=new Date().toDateString(),s=store.get('ember_mind_stat',null);
  return s&&s.day===d?{day:d,today:s.today|0,lastAt:s.lastAt||0}:{day:d,today:0,lastAt:0}})();
function refreshCard(){try{const c=document.getElementById('embercard');if(c&&typeof emberCard==='function')c.innerHTML=emberCard()}catch(_){}}
function bumpMindStat(){const d=new Date().toDateString();if(MSTAT.day!==d){MSTAT.day=d;MSTAT.today=0}
  MSTAT.today++;MSTAT.lastAt=Date.now();store.set('ember_mind_stat',MSTAT);refreshCard()}
function mindStatus(){const d=new Date().toDateString();if(MSTAT.day!==d){MSTAT.day=d;MSTAT.today=0}
  const mins=MSTAT.lastAt>0?Math.max(0,Math.round((Date.now()-MSTAT.lastAt)/60000)):null;
  const ago=mins===null?'':(mins<1?'just now':mins+' min ago');
  const tail=(ago?' · last thought '+ago:'')+' · '+MSTAT.today+' today';
  if(MIND.live||(!MIND.off&&MSTAT.today>0))return 'Mind: on'+tail;
  if(MIND.off&&MIND.why==='resting')return 'Mind: resting'+tail;
  return 'Mind: off'+(MSTAT.today>0?tail:'')}
const LIB={};
function known(){return[...Object.keys(LIB),'celebrate']}
function learnB(b){if(!b||!b.name||!Array.isArray(b.steps)||!b.steps.length)return;LIB[b.name]=b}
function saveLib(){store.set('ember_lib',Object.values(LIB).slice(-80))}
const LOOKS={viewer:()=>'viewer',cursor:()=>'cursor',up:()=>({dir:[0,-1]}),down:()=>({dir:[0,1]}),left:()=>({dir:[-1,0]}),right:()=>({dir:[1,0]}),away:()=>({dir:[E.x>W/2?1:-1,-.2]})};
function hex2arr(h){const m=/^#([0-9a-f]{6})$/i.exec(h||'');if(!m)return null;const n=parseInt(m[1],16);return[(n>>16)&255,(n>>8)&255,n&255]}
function hex2rgb(h){const a=hex2arr(h);return a?a.join(','):''}
function burst(b){const n=b.n||8,c=hex2rgb(b.color);for(let i=0;i<n;i++){const a=Math.random()*Math.PI*2,v=rnd(40,200);
  if(b.kind==='spark')parts.push({k:'spark',x:E.x,y:E.y-R*.3,vx:Math.cos(a)*v,vy:Math.sin(a)*v-60,l:1,d:rnd(.4,1),s:rnd(1.2,2.6),c:c||'255,200,120'});
  else if(b.kind==='dust')parts.push({k:'dust',x:E.x+rnd(-R,R),y:E.y+rnd(-R*.5,R*.5),vx:rnd(-40,40),vy:rnd(-20,40),l:1,d:rnd(.6,1.1),s:rnd(1,2.2),c:c||''});
  else if(b.kind==='heart')parts.push({k:'heart',x:E.x+rnd(-R,R)*.6,y:E.y-R*.8,vx:rnd(-25,25),vy:rnd(-70,-40),l:1,d:1.2,s:rnd(5,9)});
  else if(b.kind==='smoke')parts.push({k:'smoke',x:E.x+rnd(-R,R)*.5,y:E.y-R*E.tipH,vx:rnd(-15,15),vy:rnd(-50,-25),l:1,d:1.2,s:rnd(3,6)});
  else if(b.kind==='star')parts.push({k:'star',x:E.x+rnd(-R,R)*.5,y:E.y-R*.3,vx:Math.cos(a)*v*.75,vy:Math.sin(a)*v*.75-35,l:1,d:rnd(.5,1.2),s:rnd(7,13),c:c||'255,230,100'});
  else if(b.kind==='fire')parts.push({k:'fire',x:E.x+rnd(-R,R)*.5,y:E.y-R*.2,vx:rnd(-30,30),vy:rnd(-160,-70),l:1,d:rnd(.4,.9),s:rnd(2.5,5.5),c:c||'255,140,40'});
  else if(b.kind==='ring'||b.kind==='flash')parts.push({k:'ring',x:E.x,y:E.y-R*.2,vx:0,vy:0,l:1,d:rnd(.4,.8),s:R*(b.kind==='flash'?1.8:1.2),c:c||'255,190,100'});
  else parts.push({k:b.kind==='z'?'z':'note',x:E.x+R*.8,y:E.y-R*.4,vx:rnd(8,30),vy:-35,l:1,d:1.6,s:rnd(9,13)})}}
function effects(k){if(REDUCE)return;if(k.hop)E.vy=-(140+k.hop*380),E.q+=k.hop*.25;if(k.squash)E.q+=k.squash*.45;if(k.tilt)E.tv+=k.tilt*4;if(k.shake)E.shake=k.shake;
  if(k.spin)E.spinOn=1;if(k.scale)E.rT=k.scale;if(k.nudge)E.vx+=k.nudge*260;if(k.burst)burst(k.burst);
  if(k.color||k.tint)E.tint=hex2arr(k.color||k.tint);else if(k.color===''||k.tint===''||k.resetColor)E.tint=null}
/* an invented behaviour -> steps the body already knows how to play */
function compileB(b){const st=b.steps.map(k=>{const fx=Object.assign({},k.fx||{});if(k.glow&&fx.flame==null)fx.flame=k.glow;
    return{d:k.t,face:k.face,fx:Object.keys(fx).length?fx:null,pose:k.hands?'gen':k.pose,hands:k.hands,wig:k.wiggle,look:k.look&&LOOKS[k.look]?LOOKS[k.look]():null,say:k.say,hum:k.hum,arms:k.arms,body:k.body,osc:k.osc,speed:k.speed,feel:k.feel,sleep:k.sleep,fn:()=>effects(k)}});
  st.push({d:.3,fn:()=>{if(E.rT!==.6)E.rT=1;E.tint=null}});return st}
function placeOf(n){const m=area(),c=composer();
  if(Array.isArray(n)&&n.length>=2)return{x:cl(m.l+m.w*cl(+n[0]||.5,.05,.95),R*2,W-R*2),y:cl(m.t+m.h*cl(+n[1]||.5,.06,.94),R*2.4,H-R*1.6)};
  switch(n){
  case'rest':return SP.rest();case'perch':return SP.perch();
  case'wander':return{x:rnd(m.l+R*2.5,m.r-R*2.5),y:rnd(m.t+R*2.8,m.b-R*2.2)};
  case'center':return{x:m.cx+rnd(-R*2,R*2),y:m.cy+rnd(-R*1.5,R*1.5)};
  case'left':return{x:m.l+R*3,y:cl(E.y||m.cy,m.t+R*3,m.b-R*2.2)};
  case'right':return{x:m.r-R*2.8,y:cl(E.y||m.cy,m.t+R*3,m.b-R*2.2)};
  case'bottom':return{x:cl(E.x||m.cx,m.l+R*3,m.r-R*3),y:m.b-R*2.2};
  case'approach':return cur.x>=0?{x:cl(cur.x+(E.x<cur.x?-1:1)*R*2.2,m.l+R*2,m.r-R*2),y:cl(cur.y-R*.8,R*2.6,H-R*2)}:{x:m.cx,y:m.t+m.h*.55};
  case'away':return cur.x>=0?{x:cur.x>m.cx?m.l+R*3.2:m.r-R*3,y:cur.y>m.cy?m.t+R*3.2:m.b-R*2.4}:{x:m.l+R*3.2,y:m.t+R*3.2};
  case'chat':return c?{x:cl(c.l+c.w*.3,R*2,W-R*2),y:c.t-R*1.02,perch:1}:SP.rest();
  case'send':{const r=rect(shown('#sendbtn,button[onclick^=consoleSend]'));return r?{x:r.cx-R*1.6,y:r.cy-R*1.1}:SP.rest()}
  case'projects':{const el=visible('.pitem')[0];const r=rect(el);return r?{x:cl(r.r+R*1.6,R*2,W-R*2),y:r.cy}:SP.rest()}
  case'top':return{x:m.cx,y:m.t+R*2.4};case'corner':return{x:m.r-R*2.4,y:m.b-R*2.2};
  case'cursor':return cur.x>=0?{x:cl(cur.x+R*2.6*(cur.x>m.cx?-1:1),m.l+R*1.5,m.r-R*1.5),y:cl(cur.y-R*1.3,R*2.6,H-R*2)}:SP.rest();
  case'message':{const r=lastOf('.msg');return r?{x:cl(r.r+R*1.8,m.l+R*2,m.r-R*2),y:cl(r.cy,m.t+R*3,m.b-R*3)}:SP.rest()}
  case'settings':{const r=rect('#setbtn');return r?{x:cl(r.cx+R*2,R*2,W-R*2),y:cl(r.cy-R,R*2.4,H-R*2)}:SP.rest()}}
  return SP.rest()}
function mindAct(dur,k){const m={k:k||'mind',by:'mind',a:{mind:1,w:()=>0,run(){},dur:[dur,dur]},t:0,dur};ACTS_LOG.push({k:m.k,by:'mind',t:Math.round(now)});if(ACTS_LOG.length>20)ACTS_LOG.shift();act=m;return m}
/* the next step of the mind's plan, if it has one */
function mindStep(){if(!MIND.plan.length)return false;const s=MIND.plan.shift();mindAhead();
  try{if(!s.steps&&!s.do&&!s.go&&!s.wait&&!s.outfit&&!s.glasses&&!s.stop&&!s.ask&&(s.fx||s.hands||s.hop||s.spin||s.squash||s.tilt||s.shake||s.scale||s.nudge||s.glow||s.burst||s.wiggle))
      s.steps=[Object.assign({t:s.t||s.for||1.4},s)];
    if(Array.isArray(s.steps)&&s.steps.length){const st=compileB({steps:s.steps.map(k=>Object.assign({t:k.t||k.d||1.2},k))});
      MIND.done.push('improv');if(MIND.done.length>12)MIND.done.shift();
      const me=mindAct(st.reduce((t,x)=>t+(x.d||0),0)+.4,'m_improv');play(st,()=>{if(act===me)me.t=me.dur+1});return true}
    if(s.feel){feel(s.feel);if(!s.steps&&!s.do&&!s.go&&!s.wait&&!s.say&&!s.sleep)return mindStep()}
    if(s.sleep){const d=cl(+s.sleep||30,5,300),me=mindAct(d,'m_sleep');feel({a:.08});play([{d:.9,face:'sleepy',body:{crouch:.15}},{d:Math.max(1,d-.9),face:'asleep',pose:'limp',sleep:1,body:{crouch:.3,lean:6}}],()=>{if(act===me)me.t=me.dur+1});return true}
    if(s.do){const n=s.do;
      MIND.done.push(n);if(MIND.done.length>12)MIND.done.shift();
      let st=null;if(LIB[n])st=compileB(LIB[n]);else if(n==='celebrate')st=[{d:2,face:'joy',pose:'cheer',fn:celebrate}];
      if(!st)return mindStep();const me=mindAct(st.reduce((t,x)=>t+(x.d||0),0)+.6,'m_'+n);play(st,()=>{if(act===me)me.t=me.dur+1});return true}
    if(s.go){const tag=Array.isArray(s.go)?'xy':s.go;go(placeOf(s.go),{speed:.75});mindAct(2.8,'m_go_'+tag);return true}
    if(s.say){say(s.say);const nx=MIND.plan[0];if(s._m&&nx&&!nx.say&&!nx.ask&&!nx.wait&&mindStep())return true;mindAct(s._m?Math.min(3.5,readTime(s.say)):readTime(s.say)+.5,'m_say');return true}
    if(s.ask){if(MIND.asks>2||now-CHAT.lastChat<60)return mindStep();MIND.asks++;openChat(s.ask,0);mindAct(2,'m_ask');return true}
    if(s.outfit){FITOV={n:s.outfit,until:Date.now()+(s.for||20)*60000};if(CFG.fit!=='auto'){CFG.fit='auto';store.set('ember_cfg',CFG)}
      if(s.outfit==='shades')E.shadesUntil=now+(s.for||20)*60;else if(E.shadesOn&&!(E.shadesUntil>now))E.shadesOn=false;
      puff(E.x,E.y-R*.6,10);const me=mindAct(2,'m_outfit');play([{d:1.6,face:'smug',pose:'hips',look:'viewer'}],()=>{if(act===me)me.t=me.dur+1});return true}
    if(s.glasses){const me=mindAct(4,'m_glasses');if(s.glasses==='on'){E.shadesUntil=now+60;E.shadesOn=true;play([{d:1.2,pose:'glasses',face:'smug'}],()=>{if(act===me)me.t=me.dur+1})}
      else if(s.glasses==='off'){E.shadesUntil=0;if(FITOV&&FITOV.n==='shades')FITOV=null;if(CFG.fit==='shades'){CFG.fit='auto';store.set('ember_cfg',CFG)}E.shadesOn=false;me.dur=1}
      else{E.shadesUntil=now+8;E.shadesOn=true;play([{d:.6,pose:'glasses',face:'calm'},{d:2.4,face:'unimpressed',look:'viewer',fn:()=>{E.noBlink=2.4}},{d:1.2,face:'smug',look:'viewer'}],()=>{if(act===me)me.t=me.dur+1})}return true}
    if(s.stop){NS.quiet=now+Math.min(240,+s.for||5)*60;mindAct(.2,'m_stop');return true}
    if((s.face||s.pose)&&!s.wait){const hd=s._m?Math.min(4,s.for||2):(s.for||2),me=mindAct(hd,'m_pose');play([{d:hd,face:s.face,pose:s.pose,fx:s.fx||null,look:s.look&&LOOKS[s.look]?LOOKS[s.look]():null}],()=>{if(act===me)me.t=me.dur+1});return true}
    if(s.wait&&(s.arms||s.body||s.osc)){const d=s._m?Math.min(6,Math.max(.8,+s.wait||2)):(+s.wait||2),me=mindAct(d,'m_wait');play([{d,face:s.face,fx:s.fx||null,pose:s.pose,arms:s.arms,body:s.body,osc:s.osc,look:s.look&&LOOKS[s.look]?LOOKS[s.look]():null}],()=>{if(act===me)me.t=me.dur+1});return true}
    if(s.wait){const me=mindAct(s._m?Math.min(4,Math.max(.8,+s.wait||2)):(+s.wait||2),'m_wait');me.look=LOOKS[s.look]?LOOKS[s.look]():(cur.x>=0?LOOKS.cursor&&LOOKS.cursor():null);if(s.face)me.face=s.face;if(s.pose)me.pose=s.pose;if(s.fx)me.fx=s.fx;return true}}catch(e){}
  return mindStep()}
function applyPlan(r){if(!r)return;
  for(const b of r.new||[])learnB(b);if((r.new||[]).length)saveLib();
  if(!Array.isArray(r.plan)||!r.plan.length)return;
  MIND.plan=r.plan.slice().concat(MIND.plan);
  while(MIND.plan.length&&(MIND.plan[0].outfit||MIND.plan[0].glasses||MIND.plan[0].stop))mindStep();
  if(MIND.plan.length){SEQ=null;mindStep()}}
function perceive(reason){const m=CHAT.mem||{},j=CTX.job,h=new Date();let view='';try{view=(typeof S!=='undefined'&&(S.view||''))||''}catch(_){}
  const scr=[];if(composer())scr.push('the chat box');const pn=visible('.pitem').length;if(pn)scr.push(pn+' projects in the sidebar');if(lastOf('.msg'))scr.push('a conversation');
  if(lastOf('.jobc'))scr.push('a migration job card');if(document.querySelector('.pvw:not([hidden])'))scr.push('the site preview');if(DESK)scr.push('their desktop ('+((DW.windows||[]).length)+' app windows)');
  return{reason,time:h.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}),day:h.toLocaleDateString([],{weekday:'long'}),late:h.getHours()>=23||h.getHours()<5,
    where:CTX.project?'project '+CTX.project:(view||'home'),job:j?(j.status+(j.progress&&j.progress.total?' '+(j.progress.done||0)+'/'+j.progress.total:'')):'none',
    just_happened:CTX.last&&now-CTX.lastT<300?CTX.last:'',problem:CTX.issue&&now-CTX.issueT<900?CTX.issue.slice(0,200):'',
    person:{seconds_since_they_moved:Math.round(now-cur.t),typing:now-(CHAT.typing||-99)<6||now-MIND.key<6,near_ember:cur.x>=0&&Math.hypot(cur.x-E.x,cur.y-E.y)<R*5,
      last_talked_to_ember_s:CHAT.lastChat>0?Math.round(now-CHAT.lastChat):null,name:m.name||''},
    ember:{energy:+N.energy.toFixed(2),boredom:+N.boredom.toFixed(2),curiosity:+N.curiosity.toFixed(2),playfulness:+(N.playfulness||.5).toFixed(2),wants_company:+N.social.toFixed(2),annoyed:+N.annoy.toFixed(2),
      fond_of_them:+N.fond.toFixed(2),face:E.FT.name,outfit:outfit(),glasses:!!E.shadesOn,doing:act?act.k:'',focus:MIND.focus||'',last_outcome:mindOutcome(),feeling:{valence:+AFF.v.toFixed(2),arousal:+AFF.a.toFixed(2),word:moodWord(AFF.v,AFF.a)},body_was_doing:NS.kinds.slice(-3),
      recent_actions:MIND.done.slice(-8)},
    doing_for_you:AG.log.slice(-4).map(x=>Math.round(T()-x.t)+'s ago: '+x.what),offering:AG.offer?AG.offer.text:'',may_do:agency(),
    screen:scr,recent:MIND.ev.slice(-6).map(e=>Math.round(now-e.t)+'s ago: '+e.what),you_recently:MIND.hist.slice(-3),remembers:(m.facts||[]).slice(-6)}}
function think(reason){if(MIND.busy)return;
  const hr=Date.now()-3600000;MIND.hour=MIND.hour.filter(t=>t>hr);
  MIND.perHour=CFG.ai==='groq'?40:150;
  if(MIND.hour.length>=MIND.perHour){MIND.live=0;MIND.off=1;MIND.why='resting';MIND.retry=now+900;if(act&&act.k==='mind_wait')act.t=act.dur+1;refreshCard();return}
  MIND.busy=1;MIND.lastAsk=now;MIND.hour.push(Date.now());
  post('/api/ember/mind',{state:perceive(reason||'time'),attitude:ATT(),known:known(),ai:CFG.ai,liveMode:CFG.liveMode}).then(r=>{
    if(r.off){MIND.live=0;MIND.off=1;MIND.why=r.why||'off';MIND.retry=now+(r.retry||1800);MIND.plan=[];if(act&&act.k==='mind_wait')act.t=act.dur+1;refreshCard();return}
    for(const b of r.new||[])learnB(b);if((r.new||[]).length)saveLib();
    const wasLive=MIND.live;MIND.live=1;MIND.off=0;MIND.why='';MIND.fail=0;if(!wasLive)ACTS_LOG.length=0;
    MIND.plan=(r.plan||[]).map(x=>Object.assign({_m:1},x));MIND.thought=r.thought||'';if(r.focus)MIND.focus=r.focus;
    MIND.snap={t:now,ig:CHAT.ignored,asked:MIND.plan.some(x=>x&&x.ask)};MIND.cueUntil=now+1.2;if(!REDUCE)burst({kind:'spark',n:6,color:'#ffd89a'});
    bumpMindStat();
    MIND.hist.push((r.thought||'')+(r.new&&r.new.length?' (invented '+r.new.map(b=>b.name).join(', ')+')':''));MIND.hist=MIND.hist.slice(-6);
    if(r.remember){const f=CHAT.mem.facts||(CHAT.mem.facts=[]);if(!f.includes(r.remember)){f.push(r.remember);CHAT.mem.facts=f.slice(-6);saveMem()}}
    const midway=act&&act.by==='mind';
    if(act&&!midway){act.t=act.dur+1;if(act.by==='body'&&SEQ)SEQ=null}
    MIND.next=now+mindGap()}).catch(e=>{MIND.fail++;MIND.next=now+Math.min(900,15*Math.pow(2,MIND.fail));if(MIND.fail>=3){MIND.live=0;if(act&&act.k==='mind_wait')act.t=act.dur+1;refreshCard()}}).finally(()=>{MIND.busy=0})}
function mindGap(){return Math.max(18,3600/Math.max(1,MIND.perHour)*.9)*mindPace()}
function mindAhead(){if(!MIND.plan.length&&MIND.live&&!MIND.off&&!MIND.busy&&now>=MIND.next&&mindActive()&&!CHAT.open&&V.state==='idle')think('next')}
function mindActive(){if(document.hidden)return false;const seen=Math.max(cur.t,MIND.key,CHAT.typing||-99);let f=false;try{f=document.hasFocus()}catch(_){}
  return now<1800||now-seen<1800||f}
/* watched but untouched for a while: think half as often */
const mindPace=()=>now-Math.max(cur.t,MIND.key,CHAT.typing||-99)>600?2:1;

function mindTick(){if(!started||MIND.busy||TW.on||DRV.on||V.state!=='idle'||CHAT.open||document.hidden||E.mode!=='fly')return;
  if(MIND.off){if(now<MIND.retry)return;MIND.off=0;MIND.why=''}
  if(!mindActive())return;
  if(now>=MIND.next&&!MIND.plan.length&&(!MIND.live||!act||act.by!=='mind'||act.t>act.dur))think(MIND.live?'idle':'time')}
/* something happened: let the mind react soon (not more than every 20s) */
const MIND_NOW={broke:1,pass:1,published:1,error:1,gaps:1,back:1,page:1,poked:1,thrown:1,fell:1,startled:1,petted:1};let MIND_JOB='';
function mindEvent(name,data){const d=data||{};let w=name;if(d.what)w+=': '+String(d.what).slice(0,80);else if(d.tool)w+=': '+String(d.tool).slice(0,40);else if(d.job&&d.job.status)w+=': '+d.job.status;
  if(name==='working'||name==='retry'){const l=MIND.ev[MIND.ev.length-1];if(l&&l.what.startsWith(name)){l.t=now;l.what=w;return}}
  let big=!!MIND_NOW[name];if(name==='job'){const st=d.job&&d.job.status||'';if(st===MIND_JOB)return;MIND_JOB=st;big=/done|failed|waiting|blocked/.test(st)}
  MIND.ev.push({t:now,what:w});MIND.ev=MIND.ev.slice(-10);if(big&&MIND.live&&now-MIND.lastAsk>20){MIND.plan=[];MIND.next=Math.min(MIND.next,now+1.5);if(act&&act.by==='mind')act.t=act.dur+1}}
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
  job(d){const j=d&&d.job;if(!j)return;CTX.project=d.project||CTX.project;CTX.job=j;try{fromJobCard(d.project,j)}catch(_){}const st=j.status,p=j.progress||{},r=j.result||{};
    if(['queued','running','resuming'].includes(st)){if(!mood||mood.kind!=='job'){go(SP.work);setMood({kind:'job',d:600,pose:'type',face:'focus',look:{pt:()=>{const c=lastOf('.jobc');return c?{x:c.cx,y:c.cy}:{x:E.x-90,y:E.y}}}})}
      if(p.total&&fresh('jobline',50)&&!CHAT.open){const n=p.done||0;play([{d:2.2,look:'viewer',face:ATT()==='sweet'?'happy':'calm',pose:ATT()==='sweet'?'rest':'crossed',say:[`${n} of ${p.total} lines written`,`${n} lines. you're welcome.`,`${n} lines. I'm basically carrying you.`]}])}}
    else if(st==='done'&&fresh('jobdone:'+d.project,120)){setMood(null);if(r.ok)SIG.pass({project:d.project});else SIG.broke({what:(r.problems||[])[0]||'something'})}
    else if((st==='failed'||st==='blocked'||st==='waiting')&&fresh('jobstop:'+d.project+st,120)){setMood(null);if(st==='waiting')SIG.ask({});else SIG.broke({what:j.error||'the migration'})}},
  pass(d){if(!fresh('pass',20))return;CTX.last='the browser check passed'+(d&&d.project?' on '+d.project:'');CTX.lastT=now;setMood(null);
    if(ATT()==='sweet')play([{d:.18,face:'surprised'},{d:1.6,face:'joy',pose:'cheer',say:'it all works!',fn:celebrate},{d:1.2,face:'happy',pose:'hips',look:'viewer'}]);
    else play([{d:.18,face:'surprised'},{d:1.1,face:'joy',pose:'cheer',fn:celebrate},{d:.9,pose:'glasses',face:'smug',look:'viewer',fn:()=>{E.shadesUntil=now+5}},
      {d:2.6,pose:'crossed',face:'smug',look:'viewer',say:['','told you. every page checked, all clean.',"every page works. you're welcome."]}])},
  broke(d){const what=String(d&&d.what||'something').slice(0,40);CTX.issue=(d&&d.gaps&&d.gaps.length?d.gaps.join('; '):what+' is broken').slice(0,500);CTX.issueT=now;
    if(!DESK&&APP&&d&&d.gaps&&d.gaps.length)offer({id:'fix:'+CTX.issue.slice(0,60),   /* only what a browser check measured, never a vague error */
text:`${what} broke. Want me to ask Aethron to fix it, then check every page again?`,yes:'Fix it',run:fixIssue,prio:2,snooze:20,still:(i=>()=>CTX.issue===i&&AG.fixed!==i)(CTX.issue)});if(!fresh('broke',25))return;CTX.last=what+' failed';CTX.lastT=now;E.sweat=1;
    const r=lastOf('.jobc')||lastOf('.truth')||lastOf('.run');if(r){go({x:r.r+R*1.6,y:r.cy});const dx=r.cx-E.x;E.pointDir=[dx<0?-1:1,.1]}
    setMood({d:7,pose:'point',face:ATT()==='sweet'?'worried':'tfman',look:r?{pt:{x:r.cx,y:r.cy}}:'viewer',end(){E.sweat=0}});
    play([{d:.9,face:'shocked',pose:'cheeks',look:'viewer'},{d:2.6,say:[`${what} broke. look here.`,`${what} broke. relax, look here.`,`${what} shit the bed. look.`]}])},
  refused(){if(!fresh('refused',60)||CHAT.open)return;play([{d:1.8,pose:'shrug',face:'calm',look:'viewer',say:["that's a rule, not a bug",'rules are rules. not a bug.','nope. rules. deal with it.']}])},
  ask(){if(!fresh('ask',60))return;play([{d:2.4,pose:'point',face:'curious',look:'viewer',fn:()=>{E.pointDir=[-1,.3]},say:['it needs an answer from you','it has a question. answer it.','it asked you something. hello?']}])},
  published(d){if(!fresh('pub',20))return;CTX.last='the site went live';CTX.lastT=now;play([{d:.2,face:'surprised'},{d:1.6,face:'joy',pose:'cheer',fn:celebrate,say:['your site is live!','live. on the internet. wow.','live. try not to break it.']},{d:1.2,face:'smug',pose:'hips',look:'viewer'}])},
  update(){agTick(1)},
  back(d){if(!fresh('back',600))return;const p=CTX.project;E.F=face('sleepy');
    play([{d:.6,face:'sleepy'},{d:1,pose:'rub',face:'sleepy'},{d:1.2,pose:'stretch',face:'yawn'},
      {d:2.6,pose:ATT()==='sweet'?'wave':'crossed',face:ATT()==='sweet'?'joy':'calm',look:'viewer',say:[`welcome back!`,`oh. you're back.`,`look who finally showed up.`]}])},
  error(d){SIG.broke({what:(d&&d.what)||'that run'})},
  gaps(d){const g=d&&d.gaps||[];if(g.length){CTX.issue=g.join('; ').slice(0,500);CTX.issueT=now}else{CTX.issue=''}},
};
function signal(name,data){if(!SIG[name])return;if(DRV.on||V.state!=='idle'){const d=data||{};if(name==='gaps')SIG.gaps(d);else if(name==='broke'){CTX.issue=(d.gaps&&d.gaps.length?d.gaps.join('; '):String(d.what||'something')+' is broken').slice(0,500);CTX.issueT=now}else if(name==='job'&&d.job){CTX.project=d.project||CTX.project;CTX.job=d.job}return}if(!DESK&&CFG.desktop){post('/api/ember/signal',{name,data:data||{}}).catch(()=>{});return}
  if(!started)return;try{mindEvent(name,data)}catch(e){}try{SIG[name](data||{})}catch(e){}}
function fromEvents(evs,bridge){if(!evs)return;if(!DESK&&CFG.desktop){
    for(const e of evs){if(e.type==='tool')signal(/blocked|denied|refused/i.test(String(e.status||''))?'refused':'working',{tool:String(e.label||e.name||'').replace(/^mcp__aethron__/,'')});
      else if(e.type==='done'&&e.error&&!String(e.text||'').startsWith('[ede_diagnostic]'))signal('error',{})}
    if(bridge&&/retry/i.test(String(bridge.state||bridge.status||'')))signal('retry',{tries:bridge.tries||bridge.failures||''});return}
  if(!started||DRV.on||V.state!=='idle')return;
  for(const e of evs){if(e.type==='tool'&&/blocked|denied|refused/i.test(String(e.status||'')))SIG.refused();
    else if(e.type==='tool')SIG.working({tool:String(e.label||e.name||'').replace(/^mcp__aethron__/,'')});
    else if(e.type==='done'&&e.error&&!String(e.text||'').startsWith('[ede_diagnostic]')){SIG.error({});agCredit(String(e.text||''))}
    else if(e.type==='done'){if(mood&&mood.kind==='work')setMood(null)}}
  if(bridge&&/retry/i.test(String(bridge.state||bridge.status||'')))SIG.retry({tries:bridge.tries||bridge.failures||''})}

/* ===== agency: Ember does things for you =====
   Ember watches what the app is really doing: the screen's own state, and
   /api/ember/world, which sees the migration on every project and an update
   already downloaded. What is safe it simply does, and says so: downloading
   an update changes nothing until a restart. Anything that changes your work,
   costs money or restarts the app it OFFERS, naming the button it will press,
   and presses it in front of you when you say yes. How far it goes is yours
   (CFG.agency): 'ask' only offers, 'safe' (default) does the safe things
   itself, 'all' also retries a stopped migration once, heals edits that did
   not land and restarts for an update while you are away and nothing runs.
   It never publishes, unpublishes, deletes or undoes by itself. */
/* the agency keeps the wall clock: Ember's own clock (now) stops whenever the window is not drawn, and the agency must keep watching then */
const T=()=>performance.now()/1000;
const APP=(()=>{try{return typeof S!=='undefined'}catch(_){return false}})();
const AG={offer:null,queue:[],later:{},notified:{},hiddenAt:0,jobs:null,retried:{},world:null,worldT:-99,checkT:-99,
  fetching:0,failT:-1e9,staged:'',latest:'',avail:0,restarting:0,log:[],healed:{},offeredAt:0};
function agency(){return['ask','safe','all'].includes(CFG.agency)?CFG.agency:'safe'}
function agLog(t){AG.log.push({t:T(),what:t});if(AG.log.length>12)AG.log.shift();CTX.last=t;CTX.lastT=now}
function away(s){if(document.hidden&&AG.hiddenAt&&T()-AG.hiddenAt>s)return true;return now>60&&now-Math.max(cur.moveT,MIND.key,CHAT.typing||-99)>s}
function appBusy(){if(!APP)return false;try{return!!((typeof CODE!=='undefined'&&CODE.runT0)||S.running||S.autoRunning)}catch(_){return false}}
/* a sheet the app opened (What's new, publish, the update gate) has the floor: Ember waits for it to close */
function quiet(){return CHAT.open||DRV.on||!!document.querySelector('#newsov.on,#updgate,#pubov')||V.state!=='idle'||TW.on||cur.grab||document.hidden||now-(CHAT.typing||-99)<8||now-MIND.key<6}
function upd(){let u=null;try{u=typeof UPD!=='undefined'?UPD:null}catch(_){}const w=(AG.world&&AG.world.update)||{};
  return{available:!!((u&&u.available)||AG.avail),latest:(u&&u.latest)||AG.latest||'',staged:(u&&u.staged)||w.staged||AG.staged||'',fetching:!!((u&&u.fetching)||w.fetching||AG.fetching)}}
const ofr=document.createElement('div');ofr.id='ember-offer';ofr.hidden=true;ofr.setAttribute('role','dialog');ofr.setAttribute('aria-live','polite');
ofr.innerHTML='<p></p><div class="b"><button class="no">Later</button><button class="yes">Yes</button></div>';
function offerHit(x,y){if(ofr.hidden)return false;const r=ofr.getBoundingClientRect();return x>=r.left-6&&x<=r.right+6&&y>=r.top-6&&y<=r.bottom+6}
/* the card stays where it was put while you reach for it: it moves only when Ember really goes somewhere, never with its breathing */
function placeOffer(){if(ofr.hidden)return;if(AG.ax!=null&&(offerHit(cur.x,cur.y)||Math.hypot(E.x-AG.ax,E.y-AG.ay)<R*2.5))return;AG.ax=E.x;AG.ay=E.y;const rr=R*E.r,w=ofr.offsetWidth||300,h=ofr.offsetHeight||90;
  let x=cl(E.x-w/2,12,W-w-12),y=E.y+rr*1.5;if(y+h>H-12)y=E.y-rr*(E.tipH||1.6)-h-24;ofr.style.left=Math.round(x)+'px';ofr.style.top=Math.round(cl(y,12,H-h-12))+'px'}
/* an offer is {id, text, yes, no, run, at (where to stand), snooze (min), hold() (not T())} */
function offer(o){if(!o||!o.id||(AG.later[o.id]||0)>T())return;if(AG.offer&&AG.offer.id===o.id)return;
  const was=AG.queue.find(x=>x.id===o.id);o.notBefore=was?was.notBefore:T()+5;   /* whatever it reacts with first (a cheer, a wince) plays before the card */
  AG.queue=AG.queue.filter(x=>x.id!==o.id);AG.queue.push(o);AG.queue.sort((a,b)=>(b.prio||0)-(a.prio||0))}
function showOffer(o){AG.offer=o;o.shownAt=T();AG.offeredAt=T();ofr.querySelector('p').textContent=o.text;ofr.querySelector('.yes').textContent=o.yes||'Yes';
  ofr.querySelector('.no').textContent=o.no||'Later';ofr.hidden=false;ofr.style.animation='none';void ofr.offsetWidth;ofr.style.animation='';
  if(SEQ&&SEQ.ns)SEQ=null;if(act&&act.by==='body')act.t=act.dur+1;const t=o.at&&o.at();
  AG.ax=null;if(t&&running)flyTo(t);setMood({kind:'offer',d:8,pose:'point',face:'curious',look:'viewer'});placeOffer();E.q-=.25}
function closeOffer(snooze){const o=AG.offer;if(!o)return;AG.offer=null;ofr.hidden=true;AG.ax=null;if(mood&&mood.kind==='offer')setMood(null);
  if(snooze)AG.later[o.id]=T()+snooze*60}
async function yesOffer(){const o=AG.offer;if(!o)return;closeOffer(o.after||0);agLog('they said yes: '+o.text);
  try{mindEvent('agreed',{what:'they said yes to: '+o.text})}catch(_){}
  try{await drive(o.run)}catch(e){said('that did not work: '+String(e&&e.message||e).slice(0,80))}}
ofr.addEventListener('click',e=>{const b=e.target.closest&&e.target.closest('button');if(!b)return;e.stopPropagation();
  if(b.classList.contains('yes'))yesOffer();else{const o=AG.offer;closeOffer((o&&o.snooze)||30);if(o)agLog('they said not now to: '+o.text)}});
addEventListener('keydown',e=>{if(e.key==='Escape'&&AG.offer){closeOffer((AG.offer.snooze)||30)}},true);
/* run a job with Ember's hands, the way perform() does */
async function drive(fn){if(!fn)return;if(DRV.on)return;DRV.on=1;V.cancel=0;SEQ=null;setMood({kind:'drive',d:120,pose:'point',face:'focus',look:null});
  try{return await fn()}finally{DRV.on=0;setMood(null);if(running)go(SP.perch,{speed:.8})}}
function cssq(s){return String(s).replace(/["\\]/g,'\\$&')}
async function openProject(p){if(!APP)return false;try{if(S.cur===p)return true}catch(_){}const el=projectEl(p);if(!el)return false;
  el.scrollIntoView({block:'nearest',behavior:REDUCE?'auto':'smooth'});await sleep(REDUCE?60:380);await press(el);
  for(let i=0;i<20;i++){await sleep(150);try{if(S.cur===p)break}catch(_){}}await sleep(500);return true}
/* on the desktop the app window does the pressing: hand it over */
function relay(item){return post('/api/ember/relay',item).catch(()=>{})}
/* ---- the duties ---- */
async function agDownload(){if(AG.fetching)return;const u=upd(),ver=u.latest;AG.fetching=1;
  const chip=APP&&shown('.uc');if(chip&&running){await flyTo(chip);E.pointDir=[0,-1]}
  said(`Aethron ${ver} is out. I'm downloading it for you.`,3.5);agLog('Ember started downloading Aethron '+ver);
  let r;try{r=(APP&&typeof prefetchUpdate==='function')?await prefetchUpdate():await prefetchDirect()}catch(e){r={ok:false,why:String(e&&e.message||e)}}
  AG.fetching=0;
  if(r&&r.ok){AG.staged=r.version||ver;agLog('Ember downloaded Aethron '+AG.staged+'; it installs on the next restart');try{mindEvent('did',{what:'finished downloading the Aethron '+AG.staged+' update for them'})}catch(_){}agTick(1)}
  else{AG.failT=T();said(`The download didn't finish (${String(r&&r.why||'unknown').slice(0,70)}). I'll try again later.`,4);agLog('the update download failed')}}
async function prefetchDirect(){const r=await post('/api/update/prefetch',{});if(!r||!r.job)return{ok:false,why:(r&&r.error)||'no download started'};
  for(let i=0;i<2400;i++){await sleep(1000);let j;try{j=await(await fetch('/api/update/status?id='+r.job,{credentials:'same-origin'})).json()}catch(_){continue}
    if(j.done){const m=(j.log||'').match(/downloaded (\S+)/);return j.ok?{ok:true,version:m?m[1]:AG.latest}:{ok:false,why:((j.log||'').match(/download failed: (.*)/)||[])[1]||'unknown'}}}
  return{ok:false,why:'it took too long'}}
async function agRestart(){agLog('Ember is restarting Aethron to install the update');
  if(!APP){await post('/api/update/apply',{});return}
  const chip=shown('.uc');if(chip){await press(chip);await sleep(450);const b=shown('#updchip .ucpop .btn.primary');if(b){await press(b);return}}
  if(typeof applyUpdate==='function')applyUpdate()}
async function agAnswer(p){if(!APP)return relay({kind:'agency',id:'answer',project:p});if(!await openProject(p))return;
  const inp=shown('#jobans-'+cssq(p).replace(/[^\w-]/g,'\\$&'))||shown('.jobc.waiting input');if(inp){await press(inp);inp.focus()}}
async function agRetry(p){if(!APP)return relay({kind:'agency',id:'retry',project:p});if(!await openProject(p))return;
  const b=shown(`.jobc[data-job="${cssq(p)}"] .jbtn`);if(b){await press(b);agLog('Ember pressed Try again on '+p)}}
async function agShow(p,problems){if(!APP)return relay({kind:'agency',id:problems?'look':'show',project:p});if(!await openProject(p))return;
  if(!problems){const pv=shown(CONTROLS.preview);if(pv){await press(pv);return}}
  const row=shown(`.jobc[data-job="${cssq(p)}"] .jobc-row`)||shown('.truth');if(row){row.scrollIntoView({block:'center',behavior:REDUCE?'auto':'smooth'});await sleep(400);await press(row)}}
async function agHeal(p){const h=await post('/api/heal',{project:p});if(h&&h.healed>0&&typeof runStep==='function')await runStep('build');
  const n=(h&&h.healed)||0,st=(h&&h.stuck)||0;said(n||st?`fixed ${n}${st?`, ${st} still need${st>1?'':'s'} you`:''}.`:'nothing I could fix by myself.',3.5);
  agLog(`Ember healed ${n} edit(s) on ${p}${st?`, ${st} stuck`:''}`)}
/* the provider said the account has no credit left: only the person can fix that, so Ember takes them to where it is fixed */
function agCredit(t){if(!APP||!/out of credit|insufficient.{0,20}(balance|quota|funds|credit)|credit balance|no credit|billing|payment required|exceeded your current quota/i.test(t))return;
  offer({id:'credit',text:'Your AI provider says the account is out of credit, so Aethron stopped. Want to open Settings to add credit or switch to another provider?',yes:'Open Settings',
    run:async()=>{const b=shown(CONTROLS.settings);if(b)await press(b);else if(typeof openSettings==='function')openSettings()},prio:3,snooze:30})}
function agRun(item){const p=item.project||'';return drive(()=>item.id==='answer'?agAnswer(p):item.id==='retry'?agRetry(p):item.id==='show'?agShow(p):item.id==='look'?agShow(p,1):null)}
/* a migration changed state: what Ember does about it */
function agJob(j){if(!j||!j.project)return;const p=j.project,sig=[j.status,j.finished||'',(j.question||'').slice(0,60),(j.error||'').slice(0,60)].join('|');
  if(!AG.jobs)AG.jobs={};const first=!(p in AG.jobs);if(!first&&AG.jobs[p]===sig)return;AG.jobs[p]=sig;
  /* first sight is a baseline (no news about jobs that ended before this window opened), except a question still waiting for them */
  if(first&&!(j.status==='waiting'&&j.ask_kind!=='blocked'))return;
  const card=()=>shown(`.jobc[data-job="${cssq(p)}"]`),still=()=>AG.jobs[p]===sig;
  if(j.status==='waiting'&&j.ask_kind!=='blocked')offer({id:'ask:'+p+':'+(j.question||'').slice(0,40),text:`${p} needs one answer from you: ${(j.question||'').slice(0,160)}`,yes:'Answer it',run:()=>agAnswer(p),at:card,prio:3,snooze:10,still});
  else if(j.status==='waiting'||j.status==='failed'){const why=String(j.question||j.error||'it stopped').replace(/^The AI provider refused:\s*/,'').slice(0,140);
    if(j.status==='failed'&&agency()==='all'&&!AG.retried[p]&&APP){AG.retried[p]=1;said(`${p} stopped (${why.slice(0,60)}). Trying it again.`,3.5);drive(()=>agRetry(p))}
    else offer({id:'stop:'+p+':'+why.slice(0,30),text:`${p} stopped: ${why}`,yes:'Try again',run:()=>agRetry(p),at:card,prio:2,snooze:20,still})}
  else if(j.status==='done'){AG.retried[p]=0;const n=(j.problems||[]).length;
    if(j.ok)offer({id:'done:'+p+':'+(j.finished||''),text:`${p} is finished and checked in a browser. Want to see it?`,yes:'Show me',run:()=>agShow(p),at:card,prio:1,snooze:60,still});
    else offer({id:'done:'+p+':'+(j.finished||''),text:`${p} finished, with ${n||'some'} thing${n===1?'':'s'} to look at.`,yes:'Show me',run:()=>agShow(p,1),at:card,prio:1,snooze:60,still})}}
function fromJobCard(project,j){if(!j)return;const r=j.result||{};agJob({project,status:j.status,finished:j.finished,question:j.question,ask_kind:j.ask_kind||(typeof jobBlocked==='function'&&jobBlocked(j)?'blocked':'question'),error:j.error,ok:!!r.ok,problems:r.problems||[]})}
function agWorld(){if(T()-AG.worldT<(document.hidden?120:30))return;AG.worldT=T();
  fetch('/api/ember/world',{credentials:'same-origin'}).then(r=>r.json()).then(w=>{AG.world=w;for(const j of w.jobs||[])agJob(j)}).catch(()=>{})}
function agUpdateCheck(){if(APP||T()-AG.checkT<1800)return;AG.checkT=T();   // the app window checks for itself
  fetch('/api/update/check',{credentials:'same-origin'}).then(r=>r.json()).then(u=>{AG.avail=!!u.available;AG.latest=u.latest||'';if(u.staged)AG.staged=u.staged;if(u.fetching&&!AG.fetching)agDownload()}).catch(()=>{})}
let agLast=-99;
/* WHILE NOBODY IS LOOKING. The frame loop stops when the window is hidden, so
   the agency keeps its own slow tick then; and an offer made while Aethron is
   not in front goes out once as a system notification (aethron_notify), whose
   click brings the app forward to the card. */
function behind(){let f=true;try{f=document.hasFocus()}catch(_){}return document.hidden||!f}
function agNotify(){if(CFG.notify==='off'||DESK||!behind())return;
  for(const o of[AG.offer,...AG.queue]){if(!o||AG.notified[o.id]||(o.hold&&o.hold())||(AG.later[o.id]||0)>T()||(o.notBefore||0)>T()||(o.still&&!o.still()))continue;
    AG.notified[o.id]=1;post('/api/notify',{title:o.title||'Aethron',body:o.text,key:o.id}).then(r=>{if(r&&r.ok)agLog('Ember sent a notification: '+o.text)}).catch(()=>{})}}
document.addEventListener('visibilitychange',()=>{AG.hiddenAt=document.hidden?T():0});
setInterval(()=>{if(started&&behind())try{agTick(1)}catch(_){}},15000);
function agTick(force){if(!started||(!force&&T()-agLast<2))return;agLast=T();agWorld();agUpdateCheck();try{agNotify()}catch(_){}
  const u=upd();
  if(u.available&&u.latest){
    if(u.staged===u.latest){
      if(agency()==='all'&&away(300)&&!appBusy()&&!AG.restarting&&!AG.offer){AG.restarting=1;agLog('they were away and nothing was running, so Ember restarted Aethron for the update');drive(agRestart)}
      else offer({id:'restart:'+u.latest,text:`Aethron ${u.latest} is downloaded. Restart now to finish? It takes a few seconds, and your projects and chats stay.`,yes:'Restart now',no:'Later',run:agRestart,at:()=>APP&&shown('.uc'),prio:2,snooze:45,hold:appBusy,still:(v=>()=>upd().staged===v)(u.latest)})}
    else if(!AG.fetching&&T()-AG.failT>1800){
      if(u.fetching||agency()!=='ask')agDownload();
      else offer({id:'download:'+u.latest,text:`Aethron ${u.latest} is out. Want me to download it? Nothing changes until you restart.`,yes:'Download',run:agDownload,at:()=>APP&&shown('.uc'),prio:2,snooze:120,still:()=>!upd().staged&&!AG.fetching})}}
  if(APP){try{const zf=S.zeroFx,p=S.cur;if(p&&zf&&zf.length&&T()-(AG.healed[p]||-1e9)>3600){const id='heal:'+p+':'+zf.length;
    if(agency()==='all'){AG.healed[p]=T();drive(()=>agHeal(p))}
    else offer({id,text:`${zf.length} edit${zf.length>1?'s':''} on ${p} did not land on the site. Want me to heal ${zf.length>1?'them':'it'}? Undo covers it.`,yes:'Heal',run:()=>{AG.healed[p]=T();return agHeal(p)},at:()=>shown('#steps .warn'),prio:1,snooze:30,still:()=>{try{return S.cur===p&&(S.zeroFx||[]).length===zf.length&&T()-(AG.healed[p]||-1e9)>3600}catch(_){return false}}})}}catch(_){}}
  AG.queue=AG.queue.filter(o=>!o.still||o.still());if(AG.offer&&AG.offer.still&&!AG.offer.still())closeOffer(0);
  if(AG.offer&&document.querySelector('#newsov.on,#updgate,#pubov')){const o=AG.offer;closeOffer(0);AG.queue.unshift(o)}
  if(AG.offer&&behind())AG.offer.shownAt=T();   // nobody is looking: the card waits, as the notification promised
  if(AG.offer){if(T()-AG.offer.shownAt>45){agLog('they did not answer: '+AG.offer.text);closeOffer(AG.offer.snooze||30)}else if(AG.offer.hold&&AG.offer.hold()){const o=AG.offer;closeOffer(0);AG.queue.unshift(o)}}
  else if(AG.queue.length&&!quiet()&&!(SEQ&&!SEQ.ns)&&T()-AG.offeredAt>4){const i=AG.queue.findIndex(o=>!(o.hold&&o.hold())&&(AG.later[o.id]||0)<=T()&&(o.notBefore||0)<=T());if(i>=0)showOffer(AG.queue.splice(i,1)[0])}}

/* ===== the person ===== */
const cur={x:-1,y:-1,t:-99,moveT:-99,over:0,down:0,grab:0,sx:0,sy:0,hist:[],rev:[],lastDx:0,boops:[],nearSince:0,
  active(){return now-this.t<3&&this.x>=0},still(){return this.x>=0&&now-this.moveT>1.2&&now-this.t<12}};
function onBody(x,y){const rr=R*E.r;return running&&E.alpha>.3&&(Math.hypot(x-E.x,(y-E.y)*1.05)<rr*1.2||Math.hypot(x-E.x-E.tipX*rr,y-E.y+rr*1.2)<rr*.6)}
let tookDown=false;
function onMove(e){const x=e.clientX,y=e.clientY;const dx=x-cur.x,dist=Math.hypot(dx,y-cur.y),dt=Math.max(.008,now-cur.t);
  if(dist>1)cur.moveT=now;cur.x=x;cur.y=y;cur.t=now;cur.hist.push({x,y,t:now});if(cur.hist.length>6)cur.hist.shift();if(!running)return;
  const near=Math.hypot(x-E.x,y-E.y);
  if(!cur.grab&&!CHAT.open&&dist/dt>2200&&near<R*3&&E.mode==='fly'){E.vx+=(E.x-x)/near*240;E.vy+=(E.y-y)/near*240;E.q-=.15;J.bv.lean+=(E.x>x?1:-1)*90;bump('annoy',.04);
    if(fresh('startled',20))try{mindEvent('startled',{what:'their cursor shot past Ember fast'})}catch(_){}}
  if(cur.down&&!cur.grab&&Math.hypot(x-cur.sx,y-cur.sy)>5){cur.grab=1;E.mode='grab';SEQ=null;document.documentElement.classList.add('ember-grab')}
  const over=onBody(x,y);cur.over=over;document.documentElement.classList.toggle('ember-over',over&&!cur.grab);
  if(over&&!cur.grab&&Math.abs(dx)>1.5){const s=Math.sign(dx);if(s!==cur.lastDx&&cur.lastDx)cur.rev.push(now);cur.lastDx=s;cur.rev=cur.rev.filter(t=>now-t<1.1);
    if(cur.rev.length>=3&&!(E.purr>0)&&!SEQ){
      if(!annoyed()){E.purr=ATT()==='sweet'?3:2.4;SEQ=null}}}}
function mine(e){const t=e&&e.target;return !!(t&&t.nodeType===1&&(menu.contains(t)||talk.contains(t)||ofr.contains(t)))}
function onDown(e){if(mine(e))return;if(e.button!==0&&e.button!==undefined&&e.pointerType==='mouse'){if(e.button===2&&onBody(e.clientX,e.clientY)){e.preventDefault();e.stopPropagation();openMenu(e.clientX,e.clientY)}return}
  menu.hidden=true;if(!onBody(e.clientX,e.clientY)){tookDown=false;return}
  e.preventDefault();e.stopPropagation();tookDown=true;cur.down=1;cur.sx=e.clientX;cur.sy=e.clientY;cur.hist=[{x:e.clientX,y:e.clientY,t:now}]}
function onUp(e){if(!cur.down)return;e.preventDefault();e.stopPropagation();cur.down=0;document.documentElement.classList.remove('ember-grab');
  if(cur.grab){cur.grab=0;const h=cur.hist,a=h[0],b=h[h.length-1],dt=Math.max(.016,b.t-a.t);E.vx=(b.x-a.x)/dt;E.vy=(b.y-a.y)/dt;const s=Math.hypot(E.vx,E.vy);
    E.mode='thrown';E.thrown=s;if(s>900)bump('annoy',.2)}
  else boop()}
function swallow(e){if(mine(e))return;if(tookDown||onBody(e.clientX,e.clientY)){e.preventDefault();e.stopPropagation();if(e.type==='click')tookDown=false}}
function boop(){cur.boops=cur.boops.filter(t=>now-t<4);cur.boops.push(now);const n=cur.boops.length;if(cur_('sleep'))SEQ=null;E.q-=.4;bump('annoy',.13);bump('social',-.1);E.giggle=.3;
  if(!SEQ||!(act&&act.by==='mind'))play([{d:.9+Math.min(.6,n*.15),face:n>=3?'shocked':'surprised',look:'viewer',body:{lean:rnd(-14,14),crouch:.3},speed:2.2}]);
  try{mindEvent('poked',{what:'they poked Ember '+n+' time'+(n>1?'s':'')+' in a row'})}catch(_){}}
function openMenu(x,y){menu.innerHTML='';const add=(t,f)=>{const b=document.createElement('button');b.textContent=t;b.onclick=()=>{menu.hidden=true;f()};menu.appendChild(b)};
  add(V.conv?'End the conversation':'Talk with Ember (hands-free)',converse);add('Type to Ember',startTalk);if(!DESK)add('Show me around',()=>welcome(1));if(CTX.issue&&now-CTX.issueT<1800&&!DESK)add('Tell Aethron to fix this',fixIssue);add('Hide for an hour',()=>{hide(3600)});add('Turn Ember off',()=>{set({on:false})});if(DESK)add('Back into Aethron',()=>{post('/api/ember/desktop',{on:false}).catch(()=>{})});else add('Companion settings…',()=>{try{openSettings()}catch(_){}});
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
    hit=!!(onBody(x,y)||talkHit(x,y)||menuHit(x,y)||offerHit(x,y)||cur.down)}
  return{hit,key:!!CHAT.open}}
function hide(sec){hideUntil=Date.now()+sec*1000;E.alphaT=0;E.rT=0;say('')}

/* ===== talking ===== */
const CHAT={open:0,turns:[],busy:0,last:0,asked:[],askIn:180,lastChat:-99,ignored:0,offline:0,mem:{name:'',facts:[]}};
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
function closeChat(line,f){CHAT.open=0;talk.hidden=true;tin.blur();CHAT.busy=0;CHAT.lastChat=now;
  if(line&&f)try{mindEvent('chat',{what:'Ember asked them something and they did not answer'})}catch(_){}act=null}
function question(){const m=CHAT.mem,h=new Date().getHours(),n=m.name?', '+m.name:'',q=[],add=a=>q.push(L(a));
  if(!m.name)add(["hey, I never asked. what's your name?","you never told me your name. rude.",'name. now.']);
  add([`what are you working on today${n}?`,'so. what are we doing today.','what the hell are we building today?']);
  if(CTX.project)add([`who's ${CTX.project} for?`,`who's ${CTX.project} for. be honest.`,`who are you even building ${CTX.project} for?`]);
  if(cur.still())add(['you okay? you went quiet.',"you've been staring at the screen for a while.","blink. you've been staring forever."]);
  if(h>=23||h<5)add([`it's really late${n}. still going?`,"it's past midnight. go to bed. or don't, I'm not your mom.","it's fucking late. what are you still doing up?"]);
  if(h>=5&&h<11)add([`good morning${n}! coffee first?`,"morning. you had coffee yet? you look like you haven't.",'morning. coffee. now.']);
  if(m.facts.length){const f=pick(m.facts);add([`you told me "${f}". any news?`,`last time: "${f}". so? how'd that go?`,`you said "${f}". did that work out or did you mess it up?`])}
  const fr=q.filter(x=>!CHAT.asked.includes(x));const c=pick(fr.length?fr:q);CHAT.asked.push(c);if(CHAT.asked.length>8)CHAT.asked.shift();return c}
function parseMins(t,dflt){let m=/\b(\d+(?:\.\d+)?)\s*(?:min(?:ute)?s?|m\b)/i.exec(t);if(m)return cl(+m[1],1,240);
  m=/\b(\d+(?:\.\d+)?)\s*(?:hour|hr)s?\b/i.exec(t);if(m)return cl(+m[1]*60,1,240);
  if(/\b(?:an|one)\s+hour\b/i.test(t))return 60;
  m=/\b(\d+(?:\.\d+)?)\s*(?:sec(?:ond)?s?|s\b)/i.exec(t);if(m)return cl(Math.max(1,+m[1]/60),1,240);
  return dflt}
function localWish(t){const l=String(t||'').toLowerCase().trim();if(!l)return null;
  if(/\b(?:glasses|shades|sunglasses)\b/.test(l)){
    if(/\b(?:off|remove|lose|no|stop|without|drop|ditch|put away)\b/.test(l)||/\btake\b.*\b(?:glasses|shades|sunglasses)\b/.test(l))
      return{say:L(['okay, glasses off!','fine. glasses off.','fine. took them off.']),face:'calm',gesture:'none',mood:.1,plan:[{glasses:'off'}]};
    if(/\b(?:on|wear|put|show|cool|peek)\b/.test(l)){const g=/peek/.test(l)?'peek':'on';
      return{say:L(['shades on!','shades on. obviously.','shades on. deal with it.']),face:'smug',gesture:'shades',mood:.2,plan:[{glasses:g}]}}}
  const mf=parseMins(l,20);
  for(const [fit,rx,lbl] of[['beanie',/\bbeanie\b/,'beanie'],['santa',/\b(?:santa|christmas(?:\s+hat)?)\b/,'santa hat'],['witch',/\bwitch(?:\s+hat)?\b/,'witch hat'],['party',/\bparty(?:\s+hat)?\b/,'party hat']]){
    if(rx.test(l)){
      if(/\b(?:off|remove|lose|no|without|drop|ditch)\b/.test(l))return{say:L(['okay, hat off!','fine. hat off.','fine. no hat.']),face:'calm',gesture:'nod',mood:0,plan:[{outfit:'none',for:mf}]};
      if(/\b(?:wear|put(?:\s+on)?|try|change(?:\s+into)?|hat|beanie|on)\b/.test(l))return{say:L([`${lbl} on!`,`fine. ${lbl} on.`,`${lbl}. happy now?`]),face:ATT()==='sweet'?'happy':'smug',gesture:'hips',mood:.2,plan:[{outfit:fit,for:mf}]}}}
  if(/\b(?:take\s+off|remove|no|lose)\s+(?:your\s+|the\s+)?(?:hat|outfit|costume|cap)\b|\b(?:hat|outfit)\s+off\b/.test(l))
    return{say:L(['okay, hat off!','fine. hat off.','fine. no hat.']),face:'calm',gesture:'nod',mood:0,plan:[{outfit:'none',for:mf}]};
  const ms=parseMins(l,30),sm=/\b(?:stop|quit|no\s+more|don'?t|do\s+not|enough)\s+(?:with\s+(?:the\s+)?)?(cleaning|clean|dusting|dust|sweeping|sweep|wiping|wipe|patrolling|patrol|polishing|polish|doodling|doodle|reading|read|juggling|juggle|stargazing|stargaze|working\s+out|workout|exercising|exercise|errands|chores|moving\s+around)\b/.exec(l);
  if(sm){const map={clean:'dust',cleaning:'dust',dusting:'dust',dust:'dust',sweeping:'dust',sweep:'dust',wiping:'dust',wipe:'dust',patrolling:'patrol',patrol:'patrol',polishing:'polish',polish:'polish',doodling:'doodle',doodle:'doodle',reading:'read',read:'read',juggling:'juggle',juggle:'juggle',stargazing:'stargaze',stargaze:'stargaze','working out':'workout',workout:'workout',exercising:'workout',exercise:'workout',errands:'all',chores:'all','moving around':'all'};
    const k=map[sm[1].replace(/\s+/g,' ')]||'all';
    const line=k==='dust'?L(['okay, no more cleaning for a bit!',"fine. I'll leave the dust alone.",'fine. live in filth then.']):L(["okay, I'll take a break from that!",`fine. no ${k} for a bit.`,"fine. I'll sit still."]);
    return{say:line,face:ATT()==='sweet'?'calm':'unimpressed',gesture:'crossed',mood:0,plan:[{stop:k,for:ms}]}}
  if(/\b(?:dance|boogie|groove|bust\s+a\s+move|twerk|shimmy)\b/.test(l))
    return{say:L(['watch this!','fine. watch the master.','one dance. do not record this.']),face:ATT()==='sweet'?'joy':'smug',gesture:'dance',mood:.35,
      plan:[{steps:[
        {t:.45,pose:'dance',face:'joy',fx:{lidL:.55,mc:.7,mo:.35,flame:1.28,bright:1.12},hop:.55,tilt:-.35,burst:{kind:'spark',n:12,color:'#ffd89a'}},
        {t:.5,pose:'cheer',face:'joy',hands:[[-1.35,-1.15],[1.35,-1.15]],wiggle:[.28,12],hop:.75,spin:true,burst:{kind:'note',n:6}},
        {t:.55,pose:'dance',face:'smug',hands:[[-1.25,-.35],[1.15,-.85]],wiggle:[.24,10],nudge:-.45,tilt:.4,squash:.35},
        {t:.55,pose:'dance',face:'joy',hands:[[-1.15,-.85],[1.25,-.35]],wiggle:[.24,10],nudge:.45,tilt:-.4,squash:-.3},
        {t:.8,pose:'hips',face:ATT()==='sweet'?'joy':'smug',look:'viewer',glow:1.2,burst:{kind:'spark',n:16,color:'#ffb870'}}]}]};
  if(/\b(?:do\s+a\s+flip|backflip|spin\s+around|twirl|jump|hop|bounce)\b/.test(l))
    return{say:L(['wheee!','easy. ten out of ten.','nailed it.']),face:'joy',gesture:'cheer',mood:.3,
      plan:[{steps:[
        {t:.25,pose:'hands',face:'focus',squash:-.45,tilt:-.2},
        {t:.7,pose:'cheer',face:'joy',hop:.95,spin:true,burst:{kind:'spark',n:20,color:'#ffd89a'}},
        {t:.6,pose:'hips',face:'smug',look:'viewer',squash:.25}]}]};
  if(/\b(?:make\s+a\s+face|weird\s+face|funny\s+face|silly\s+face|suspicious\s+face|squint|wink|smirk|scowl|glare\s+at\s+me)\b/.test(l)){
    const sus=/suspicious|squint/.test(l);
    return{say:L(['how is this?','happy now?','this is my face. deal with it.']),face:sus?'sideeye':'mischief',gesture:'none',mood:.15,
      plan:[{steps:[
        {t:1.6,pose:sus?'crossed':'hips',face:sus?'sideeye':'mischief',look:'viewer',
         fx:sus?{lidU:.64,lidL:.38,lf:.9,bL:.34,bR:-.22,bT:.18,mw:.12,mc:-.05,mas:.75,pup:.78,flame:1.05}:{lidU:.22,lidL:.48,bL:.38,bR:-.28,mw:.26,mc:.62,mas:.95,mo:.45,eyeS:1.28,pup:.68,flame:1.2,blush:.5},
         tilt:sus?-.28:.32}]}]}}
  if(/\b(?:come\s+here|come\s+closer|over\s+here)\b/.test(l))
    return{say:L(['coming!','fine, I\'m coming.','what. I\'m right here.']),face:'curious',gesture:'none',mood:.15,plan:[{go:'approach'},{wait:8,look:'viewer',face:'curious'}]};
  if(/\b(?:go\s+away|shoo|leave\s+me\s+alone|give\s+me\s+space|move\s+away|back\s+off)\b/.test(l))
    return{say:L(['okay, I\'ll give you space.','fine. going over there.','gladly.']),face:'unimpressed',gesture:'turn_away',mood:-.1,plan:[{go:'away'},{wait:30,look:'away',face:'calm'}]};
  if(/\b(?:sit\s+still|stay\s+still|chill|relax|do\s+nothing|just\s+sit|hang\s+out|be\s+still)\b/.test(l))
    return{say:L(['okay, just chilling.','fine. sitting still.','I was already chilling.']),face:'calm',gesture:'none',mood:.1,plan:[{stop:'all',for:ms},{wait:25,look:'viewer',face:'content',pose:'rest'}]};
  return null}
function localReply(t){const w=localWish(t);if(w)return w;const l=t.toLowerCase(),m=/(?:i'?m|my name is|call me)\s+([a-z][a-z'-]{1,20})/i.exec(t);
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
  if(!CHAT.offline){try{r=await Promise.race([(typeof api==='function'?api:post)('/api/ember/say',{turns:CHAT.turns.slice(-12),state:stateText(),attitude:ATT(),ai:CFG.ai,liveMode:CFG.liveMode}),
      new Promise((_,no)=>setTimeout(()=>no(new Error('slow')),24000))])}catch(e){r=null;const m=String(e&&e.message||'');if(/not configured|no key|save your key|402|403/i.test(m))CHAT.offline=1;if(m==='slow')CHAT.slow=(CHAT.slow||0)+1}}
  if(!r||typeof r.say!=='string'||!r.say.trim())r=localReply(t);
  else if(!Array.isArray(r.plan)||!r.plan.length){const w=localWish(t);if(w&&w.plan)r.plan=w.plan}
  CHAT.busy=0;const hasDo=Array.isArray(r.plan)&&r.plan.some(s=>s&&(s.do||s.steps||s.fx||s.hands||s.hop||s.spin||s.pose||s.face||s.go||s.wait));
  let g=r.gesture||'none';if(Array.isArray(r.plan)&&r.plan.some(s=>s&&s.glasses==='off')&&g==='shades')g='none';
  applyPlan(r);if(!CHAT.open)return;
  const line=r.say.trim().slice(0,220),fc=FACES[r.face]?r.face:idleFace();
  const md=Number(r.mood);if(isFinite(md)){bump('fond',cl(md,-1,1)*.12+.03);bump('annoy',-Math.abs(md)*.1-(md<0?-md*.25:0))}
  if(r.name){CHAT.mem.name=String(r.name).slice(0,30);saveMem()}
  if(r.remember){const f=String(r.remember).slice(0,60);if(!CHAT.mem.facts.includes(f)){CHAT.mem.facts.push(f);CHAT.mem.facts=CHAT.mem.facts.slice(-6);saveMem()}}
  CHAT.turns.push({role:'assistant',content:line});if(CHAT.turns.length>24)CHAT.turns=CHAT.turns.slice(-24);CHAT.last=now;
  if(hasDo&&(SEQ||(act&&act.by==='mind')))say(line,2.4,{type:1});else gesture(g,fc,line)}
function gesture(g,fc,line){const st={d:line.length/30+1.9,face:fc,look:'viewer'};
  const fn={nod:()=>{E.nod=.9},shake:()=>{E.shake=.9},cheer:()=>{E.vy=-320;E.q=.35},dance:()=>{E.dance=Math.min(3.5,st.d)},shades:()=>{E.shadesUntil=now+st.d+.5}}[g];
  const pose={wave:'wave',cheer:'cheer',shrug:'shrug',think:'think',shy:'peek',dance:'dance',crossed:'crossed',facepalm:'facepalm',hips:'hips',shades:'glasses',turn_away:'crossed'}[g];
  if(pose)st.pose=pose;if(g==='hum')st.hum=1;if(g==='turn_away')st.look={dir:[1,-.5]};if(fn)st.fn=fn;
  play([{d:.01,fn:()=>say(line,2.4,{type:1})},st])}
function startTalk(){if(!running)return;if(CHAT.open){tin.focus();return}openChat('',1)}
function chatTick(dt){placeTalk();const typing=now-(CHAT.typing||-9)<1.5;
  if(act&&act.by==='mind'){act.t+=dt;if(act.k.startsWith('m_go_')&&Math.hypot(E.tx-E.x,E.ty-E.y)<R*.8&&Math.hypot(E.vx,E.vy)<120&&act.t>.4)act.t=act.dur+1;
    if(act.t>act.dur)mindStep()}
  else if(MIND.plan.length&&!SEQ)mindStep();
  const inAct=SEQ||(act&&act.by==='mind'&&act.k!=='mind_wait');
  if(!inAct){base.pose=typing?'listen':(ATT()==='sweet'?'rest':'crossed');base.face=CHAT.busy?'think':(typing?(ATT()==='sweet'?'curious':'sideeye'):idleFace());
    base.look=typing?{pt:()=>({x:parseFloat(talk.style.left)+90,y:parseFloat(talk.style.top)+14})}:'viewer'}
  else if(act&&act.k==='m_wait'){if(act.pose)base.pose=act.pose;if(act.face)base.face=act.face;if(act.look)base.look=act.look}
  if(typing&&!SEQ&&Math.random()<dt*.5)E.nod=.5;
  if(!CHAT.busy&&!inAct&&now-CHAT.last>(CHAT.turns.some(x=>x.role==='user')?30:18)&&document.activeElement!==tin){
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
const SILENCE=0.8,NOTHING=10;
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
  if(r.kind==='agency')return agRun(r);
  if(r.kind==='answer'){const hasDo=Array.isArray(r.plan)&&r.plan.some(s=>s&&(s.do||s.steps||s.fx||s.hands||s.hop||s.spin||s.pose||s.face||s.go||s.wait));applyPlan(r);if(running){if(hasDo&&(SEQ||(act&&act.by==='mind')))say(r.say,2.4,{type:1});else play([{d:Math.max(2.4,readTime(r.say)),face:idleFace(),look:'viewer',say:r.say}])}return true}
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
/* A conversation: double-tap once and talk. Ember answers (out loud) or does
   what you asked, then listens again by itself, until you say stop / bye,
   press Esc, double-tap, or stay quiet twice in a row. */
const STOP_WORDS=/^(?:ok(?:ay)?[, ]*)?(?:stop(?: listening)?|bye|goodbye|good night|that'?s all|that is all|we'?re done|i'?m done|thanks,? that'?s it|nothing|never ?mind|end(?: the)? conversation)[.!]*$/i;
function converse(){if(V.conv){endConv(L(['okay, talk later!','okay. bye.','finally. bye.']));return}
  if(DRV.on)return;V.conv=1;V.quiet=0;listen()}
function endConv(line){V.conv=0;V.quiet=0;try{window.speechSynthesis&&speechSynthesis.cancel();VOICE_EL&&VOICE_EL.pause()}catch(_){}
  if(V.state==='listening'||V.state==='asking')stopListen(1);if(line)play([{d:readTime(line),face:'calm',look:'viewer',say:line}])}
/* Ember's voice: the audio Gemini Live (or Gemini TTS) made for this line,
   with the mouth moving to its loudness; the system voice when there is none */
let VOICE_EL=null;const VLOG=[];
function playAudio(src){return new Promise(res=>{let done=0;const fin=()=>{if(done)return;done=1;clearInterval(iv);E.flapT=0;res(true)};
  let a,iv=0;try{a=new Audio(src);VOICE_EL=a}catch(_){return res(false)}
  let an=null,buf=null;try{const AC=window.AudioContext||window.webkitAudioContext;if(AC){const ac=playAudio.ac||(playAudio.ac=new AC());const s=ac.createMediaElementSource(a);an=ac.createAnalyser();an.fftSize=256;s.connect(an);an.connect(ac.destination);buf=new Uint8Array(an.fftSize);if(ac.state==='suspended')ac.resume()}}catch(_){an=null}
  iv=setInterval(()=>{if(an){an.getByteTimeDomainData(buf);let m=0;for(const v of buf)m=Math.max(m,Math.abs(v-128));E.flapT=cl(m/48,0,1)}else E.flapT=.25+.5*Math.abs(Math.sin(performance.now()/90))},50);
  a.onended=fin;a.onerror=()=>{done=1;clearInterval(iv);E.flapT=0;res(false)};
  a.play().catch(()=>{done=1;clearInterval(iv);E.flapT=0;res(false)});setTimeout(fin,30000)})}
function speakSystem(t){return new Promise(res=>{let s;try{s=window.speechSynthesis}catch(_){}
  if(!t||!s||!window.SpeechSynthesisUtterance)return res();
  try{const u=new SpeechSynthesisUtterance(t);u.rate=1.06;u.pitch=1.25;const v=s.getVoices().find(v=>/samantha|karen|daniel|female|zira|aria/i.test(v.name));if(v)u.voice=v;
    u.onend=u.onerror=()=>res();s.cancel();s.speak(u);setTimeout(res,Math.min(25000,2000+t.length*80))}catch(_){res()}})}
async function speakAloud(t,audio){t=String(t||'').trim();if(!t||REDUCE_VOICE())return;
  if(audio&&await playAudio(audio)){VLOG.push('audio');return}
  if(String(CFG.liveMode||'').startsWith('gemini')){try{const r=await post('/api/ember/tts',{text:t,liveMode:CFG.liveMode});if(r&&r.audio&&await playAudio(r.audio)){VLOG.push('tts');return}}catch(_){}}
  VLOG.push('system');return speakSystem(t)}
const REDUCE_VOICE=()=>CFG.voice===false;
async function nextTurn(r){if(!V.conv)return;
  for(let i=0;i<300&&DRV.on;i++)await sleep(100);
  const line=r&&(r.say||r.text)||'';if(r&&(r.kind==='answer'||r.say))await speakAloud(line,r.audio);
  if(!V.conv||!running)return;await sleep(350);if(V.conv&&V.state==='idle'&&!DRV.on)listen()}
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
  if(cancelled){V.state='idle';setMood(null);hush();V.conv=0;return}
  if(!text&&V.conv&&!why){V.state='idle';setMood(null);hush();if(++V.quiet<2){setTimeout(()=>{if(V.conv)listen()},300);return}
    endConv(L(["I'll be here when you need me.","quiet, huh. I'll be around.","okay, silent treatment. bye."]));return}
  if(text&&V.conv&&STOP_WORDS.test(text)){V.state='idle';setMood(null);hush();endConv(L(['okay, talk later!','okay. bye.','finally. peace.']));return}
  if(text)V.quiet=0;
  if(!text){V.state='idle';setMood(null);hush();V.conv=0;
    play([{d:2.4,face:ATT()==='sweet'?'worried':'unimpressed',look:'viewer',say:why||L(["I didn't hear anything","didn't hear a thing.","you said nothing. riveting."])}]);return}
  V.state='thinking';if(mood){mood.face='think';mood.pose='think'}live('');E.bubble.dots=1;E.bubble.text='';
  return heard(text)}
async function heard(text){V.state='thinking';let r=null;
  let names=null;try{if(typeof S!=='undefined'&&Array.isArray(S.projects))names=S.projects.map(p=>p&&p.name).filter(Boolean)}catch(_){}
  try{r=await Promise.race([post('/api/ember/act',Object.assign({text,state:stateText(),attitude:ATT(),relay:DESK?1:0,ai:CFG.ai,liveMode:CFG.liveMode,voice:V.conv&&!REDUCE_VOICE()?1:0},names?{projects:names}:{})),
    new Promise((_,no)=>setTimeout(()=>no(new Error('slow')),34000))])}catch(e){r=null}
  V.state='idle';setMood(null);hush();
  if(!r||!r.kind){V.conv=0;play([{d:2.6,face:'worried',look:'viewer',say:["I couldn't reach Aethron","can't reach Aethron right now.","Aethron's not answering. not my fault."]}]);return null}
  if(DESK){if(r.kind==='answer')perform(r);else play([{d:2.6,pose:'point',face:'calm',look:'viewer',fn:()=>{E.pointDir=[0,1]},say:['sending it to Aethron','handing it to Aethron.','fine. Aethron gets it.']}]);return r}
  await perform(r);nextTurn(r);return r}
function fixIssue(){if(!CTX.issue)return;AG.fixed=CTX.issue;perform({kind:'send',prompt:'Fix what the last check found: '+CTX.issue+'. Then check every page again in the browser.',say:L(['let me tell it','telling it.','fine. I\'ll tell it what you broke.'])})}
/* On the desktop Ember hears you; the app window does the typing. */
let inboxT=0;
function inboxTick(){if(DESK||!CFG.desktop||DRV.on)return;if(performance.now()-inboxT<1400)return;inboxT=performance.now();
  fetch('/api/ember/inbox',{credentials:'same-origin'}).then(x=>x.json()).then(d=>{const it=(d&&d.items||[]).filter(x=>x&&x.kind!=='answer');if(it.length)perform(it[it.length-1])}).catch(()=>{})}
setInterval(inboxTick,700);
addEventListener('keydown',e=>{if(e.key==='Escape'&&(V.conv||V.state==='listening'||V.state==='asking'||DRV.on)){V.cancel=1;V.conv=0;try{window.speechSynthesis&&speechSynthesis.cancel();VOICE_EL&&VOICE_EL.pause()}catch(_){}stopListen(1)}},true);

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
function tick(t){const dt=Math.max(0,Math.min(.05,(t-last)/1000));last=t;now+=dt;acc+=dt;if(acc>.25){needs(acc);acc=0}
  if(hideUntil&&Date.now()>hideUntil){hideUntil=0;E.alphaT=1;E.rT=1;go(SP.rest,{now:1})}
  if(mood){mood.t+=dt;if(mood.t>mood.d){const m=mood;mood=null;m.end&&m.end()}}
  if(CHAT.open)placeTalk();
  if(AG.offer)placeOffer();agTick();
  if(E.bubble&&E.bubble.box&&cur.x>=0){const k=E.bubble.box;if(cur.x>k.x-6&&cur.x<k.x+k.w+6&&cur.y>k.y-6&&cur.y<k.y+k.h+6)E.bubble.d=Math.max(E.bubble.d,E.bubble.t+1.2)}
  if(CHAT.open&&E.mode==='fly')chatTick(dt);
  else if(mood){base.pose=mood.pose||'rest';base.face=mood.face||idleFace();base.look=mood.look||null}
  else if(E.mode==='fly'&&!hideUntil){
    if(!act||act.t>act.dur){if(!mindStep()){if(!SEQ)be();else if(!act)act={k:'seq',by:'body',a:{w:()=>0,run(){},dur:[1,1]},t:0,dur:.5}}}
    if(act&&act.k&&act.k.startsWith('m_go_')&&Math.hypot(E.tx-E.x,E.ty-E.y)<R*.8&&Math.hypot(E.vx,E.vy)<120&&act.t>.4)act.t=act.dur+1;
    if(act)act.t+=dt;
    if(cur.x>=0&&!SEQ&&Math.hypot(cur.x-E.x,cur.y-E.y)<R*3.6&&!cur.over){if(!cur.nearSince)cur.nearSince=now;if(now-cur.nearSince>2.6&&now-cur.moveT>1.8){cur.nearSince=now+15;try{mindEvent('near',{what:'their cursor is hovering near Ember'})}catch(_){}}}
    else if(cur.x<0||Math.hypot(cur.x-E.x,cur.y-E.y)>R*3.6)cur.nearSince=0;
    let bf=idleFace();if(annoyed())bf=N.annoy>.8?'glare':'unimpressed';else if(N.fond>.7&&ATT()!=='savage')bf='content';
    const asleep=!!cur_('sleep');E.zz=asleep?1:0;
    base.face=asleep?'asleep':(N.energy<.25?'sleepy':(cur.over&&!annoyed()?(ATT()==='sweet'?'happy':'calm'):bf));
    base.pose=asleep?'limp':'rest';base.look=asleep?{dir:[0,1]}:null;
    if(act&&act.k==='m_wait'){if(act.face)base.face=act.face;if(act.pose)base.pose=act.pose;if(act.look)base.look=act.look}}
  seqTick(dt);if(!SEQ&&J.held)releaseJoints();mindTick();if(acc===0){pageTick();if(E.mode==='fly'&&E.landed&&E.seat&&!DRV.on&&!TW.on&&seatGone())fall()}
  let pose=cur_('pose')||base.pose,fn=cur_('face')||base.face,look=cur_('look')||base.look;
  if(E.mode==='grab'){pose='flail';fn=Math.hypot(E.vx,E.vy)>600?(ATT()==='sweet'?'joy':'shocked'):(ATT()==='sweet'?'surprised':'tfman');look='viewer'}
  if(E.mode==='thrown'){pose='flail';fn=ATT()==='sweet'?'joy':'shocked'}
  if(E.mode==='fall'){pose='flail';fn='shocked';look='viewer'}
  if(E.dizzy>0){pose='limp';fn='dizzy'}
  if(E.purr>0){pose='hands';fn='content';look='viewer'}
  E.pose=pose;const fx=E.mode==='fly'&&(cur_('fx')||(act&&act.k==='m_wait'&&act.fx));if(fx){const key='fx:'+(SEQ?SEQ.i+':'+SEQ.steps.length:'wait')+':'+fn;if(E.FT.name!==key)E.FT=Object.assign(face(fn),fx,{name:key})}else if(fn!==E.FT.name)E.FT=driveFx(face(fn));
  if(look)E.look=look;else if(E.look&&!(E.look.until>now))E.look=null;
  /* springs are stepped in slices of at most 20 ms: the hand spring (700/45) is
     unstable above ~44 ms, so a page that hands Ember 50 ms frames for half a
     minute flung its hands off screen and then made them infinite */
  for(let n=Math.ceil(dt/.02),i=0;i<n;i++)simulate(dt/n);mend();draw(dt)}
function frame(t){if(!running)return;try{tick(t)}catch(err){bad('frame: '+(err&&err.message));try{mend();X.setTransform(1,0,0,1,0,0);X.globalCompositeOperation='source-over';X.globalAlpha=1}catch(_){}}
  requestAnimationFrame(frame)}
function needs(k){const moving=Math.hypot(E.vx,E.vy)>60,napping=!!cur_('sleep');if(moving)NS.stillT=now;const _=0,
  still=!moving&&(!act||act.k==='be'||act.k==='perch'||act.k==='m_wait')&&!SEQ;
  N.energy=cl(N.energy+(napping?.04:still?.005:-(.002+(moving?.006:0)))*k,0,1);
  N.curiosity=cl(N.curiosity+(act&&act.k==='watch'?-.05:.01)*k,0,1);
  N.social=cl(N.social+(cur.over||E.purr>0||cur.grab||CHAT.open?-.1:.008)*k,0,1);
  N.boredom=cl(N.boredom+(mood||SEQ||CHAT.open||(act&&act.by==='mind'&&act.k!=='m_wait')?-.09:.008)*k,0,1);
  N.playfulness=cl((N.playfulness||.5)+(N.energy>.55&&!annoyed()?.006:-.008)*k,0,1);
  N.annoy=cl(N.annoy-.02*k,0,1);N.fond=cl(N.fond+(E.purr>0?.03:0)*k-.0005*k,0,1);
  if(!automation&&now-driveSaveT>10){driveSaveT=now;store.set('ember_drives',N)}}

/* ===== body physics ===== */
function simulate(dt){const rr=R*E.r;physio(dt);
  if(E.mode==='grab'){const nx=E.x+(cur.x-E.x)*Math.min(1,dt*20),ny=E.y+(cur.y+rr*.2-E.y)*Math.min(1,dt*20);E.vx=(nx-E.x)/dt;E.vy=(ny-E.y)/dt;E.x=nx;E.y=ny}
  else if(E.mode==='fall'){const fl=(DESK?area().b:H)-rr*1.05;E.vy+=2400*dt;E.vx*=Math.pow(.5,dt);E.x=cl(E.x+E.vx*dt,rr*1.2,W-rr*1.2);E.y+=E.vy*dt;E.tv+=E.vx/500*dt;
    if(E.y>=fl){E.y=fl;if(E.vy>240){E.q-=cl(E.vy/1500,.18,.62);if(!E.fallHit){E.fallHit=1;puff(E.x,E.y+rr*.85,9);for(let i=0;i<6;i++)parts.push({k:'dust',x:E.x+rnd(-rr,rr),y:E.y+rr*.8,vx:rnd(-90,90),vy:-rnd(40,120),l:1,d:rnd(.5,.9),s:rnd(1,2.2)})}E.vy=-E.vy*.4}
      else{E.vy=0;E.vx=0;E.mode='fly';E.tx=E.x;E.ty=E.y;E.perch=0;afterFall()}}}
  else if(E.mode==='thrown'){const f=Math.pow(.2,dt);E.vx*=f;E.vy*=f;E.x+=E.vx*dt;E.y+=E.vy*dt;
    const lo=rr*1.1;if(E.x<lo){E.x=lo;E.vx=Math.abs(E.vx)*.72;hit(E.vx,'x')}if(E.x>W-lo){E.x=W-lo;E.vx=-Math.abs(E.vx)*.72;hit(E.vx,'x')}
    if(E.y<rr*2.2){E.y=rr*2.2;E.vy=Math.abs(E.vy)*.72;hit(E.vy,'y')}if(E.y>H-rr*1.1){E.y=H-rr*1.1;E.vy=-Math.abs(E.vy)*.72;hit(E.vy,'y')}
    if(Math.hypot(E.vx,E.vy)<80){E.mode='fly';E.tx=E.x;E.ty=E.y;afterThrow()}}
  else if(REDUCE){E.x+=(E.tx-E.x)*Math.min(1,dt*7);E.y+=(E.ty-E.y)*Math.min(1,dt*7);E.vx=E.vy=0}
  else if(E.antic>0){E.antic-=dt;E.vx*=.8;E.vy*=.8;E.x-=E.anticDir[0]*rr*.5*dt}
  else{const dx=E.tx-E.x,dy=E.ty-E.y;E.vx+=(dx*13-E.vx*6.2)*dt;E.vy+=(dy*13-E.vy*6.2)*dt;const max=460*E.speed,s=Math.hypot(E.vx,E.vy);if(s>max){E.vx*=max/s;E.vy*=max/s}
    if(E.spinOn)E.vy+=1200*dt;E.x+=E.vx*dt;E.y+=E.vy*dt;if(E.perch&&Math.hypot(dx,dy)<rr*.3&&!E.landed&&s<200){E.landed=1;E.q-=.3;E.seat=seatUnder()}}
  if(E.spinOn){E.spin+=dt*Math.PI*2*1.6;if(E.spin>=Math.PI*2){E.spin=0;E.spinOn=0}}
  const sp=Math.hypot(E.vx,E.vy);
  let qT=cl(-E.vy/2400,-.22,.24)-J.b.crouch*.32;if(E.antic>0)qT=-.2;if(E.perch&&E.landed)qT-=.07;
  if(E.nod>0){E.nod-=dt;qT+=Math.sin(now*15)*.11}if(E.dance>0){E.dance-=dt;qT+=Math.abs(Math.sin(now*8))*.17-.06}if(E.puff>0){E.puff-=dt;qT+=.12*Math.min(1,E.puff)}
  E.qv+=((qT-E.q)*170-E.qv*13)*dt;E.q+=E.qv*dt;
  let tT=cl(E.vx/1500,-.32,.32)+J.b.lean*DEG;if(E.antic>0)tT=-E.anticDir[0]*.16;tT+=E.gx*.06-E.F.chin*.14*Math.sign(E.gx||1);
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
  else if(isTalking()&&sp<120){if(now>(E.avertT||0)+.6&&Math.random()<dt*.45){E.avertT=now+rnd(.35,.9);E.avD=[rnd(-.8,.8),rnd(-.55,-.1)]}if(now<(E.avertT||0)){E.gtx=E.avD[0];E.gty=E.avD[1]}else{E.gtx=0;E.gty=.05}}
  else if(cur.active())p={x:cur.x,y:cur.y};else if(sp>120)p={x:E.x+E.vx,y:E.y+E.vy};else if(Math.random()<dt*.35){E.gtx=rnd(-.8,.8);E.gty=rnd(-.4,.5)}
  if(p){const dx=p.x-E.x,dy=p.y-E.y,d=Math.hypot(dx,dy)||1,m=Math.min(1,d/(R*4));E.gtx=dx/d*m;E.gty=dy/d*m}
  if(E.F.chin>.5)E.gty=Math.min(E.gty,-.55);
  if(Math.random()<dt*1.6)E.ms=[rnd(-.06,.06),rnd(-.05,.05)];
  const gk=Math.min(1,dt*(REDUCE?40:26));E.gx+=(E.gtx+E.ms[0]-E.gx)*gk;E.gy+=(E.gty+E.ms[1]-E.gy)*gk;
  if(E.noBlink>0)E.noBlink-=dt;
  E.nextBlink-=dt;if(Math.abs(E.gtx-E.gx)>.8&&E.blinkT<=0&&Math.random()<.5)E.nextBlink=0;
  if(E.nextBlink<=0&&!(E.noBlink>0)){E.blinkT=.16;E.nextBlink=rnd(2,5.5)*(1.35-AFF.a*.6);if(Math.random()<.2)E.dbl=.3}
  if(E.dbl>0){E.dbl-=dt;if(E.dbl<=0)E.blinkT=.16}
  if(E.blinkT>0){E.blinkT-=dt;E.blink=Math.sin(Math.max(0,E.blinkT)/.16*Math.PI)}else E.blink=0;
  if(E.purr>0){E.purr-=dt;bump('social',-dt*.3);if(!REDUCE&&Math.random()<dt*6)parts.push({k:'heart',x:E.x+rnd(-.5,.5)*rr,y:E.y-rr*.8,vx:rnd(-15,15),vy:rnd(-60,-40),l:1,d:1.2,s:rnd(5,8)});
    if(E.purr<=0)try{mindEvent('petted',{what:'they petted Ember with the pointer'})}catch(_){}}
  if(E.dizzy>0)E.dizzy-=dt;
  E.shadesOn=outfit()==='shades'||E.shadesUntil>now;
  E.shadesDown+=((['unimpressed','tfman','sideeye','glare','shocked'].includes(E.FT.name)?1:0)-E.shadesDown)*Math.min(1,dt*6);
  const ps=(POSES[E.pose]||POSES.rest)(now),c=Math.cos(E.tilt+E.spin),s=Math.sin(E.tilt+E.spin),oy0=E.y-J.b.rise*rr*.5,
    snap=E.pose==='swat'||E.mode!=='fly'?2.4:1,k=Math.min(650,150*J.speed*snap),dm=2*Math.sqrt(k)*.72;
  for(let i=0;i<2;i++){const h=E.hands[i],cj=J.a[i],v=J.av[i];
    const t=J.hold[i]?Object.assign({},J.hold[i]):Object.assign(armIK(i,ps[i][0],ps[i][1],cj),{wr:0,grip:E.pose==='crossed'||E.pose==='hips'?.45:.1});
    if(!REDUCE){const g=.6+AFF.a*.6;t.sh+=E.breath*2.2+Math.sin(now*(.71+i*.13)+i*2.1)*2.6*g;t.el+=Math.sin(now*(.93+i*.17)+i)*3.2*g;t.wr+=Math.sin(now*.6+i)*4*g;if(!J.hold[i]&&E.pose==='rest'){t.sh+=J.lift*(i?14:9);t.el+=J.lift*(i?22:14)}for(const b of J.beats)if(b.i===i){const e=beatEnv(b);t.sh+=b.sh*e;t.el+=b.el*e;t.wr+=b.wr*e}t.el=cl(t.el,-165,165)}
    for(const o of J.osc)if(o.j[0]===(i?'R':'L')){const key=o.j.slice(2);if(key in t)t[key]+=o.amp*Math.sin((now-o.t0)*o.hz*Math.PI*2)}
    for(const key of['sh','el','wr']){while(t[key]-cj[key]>180)t[key]-=360;while(t[key]-cj[key]<-180)t[key]+=360}
    if(REDUCE)Object.assign(cj,t);else for(const key of['sh','el','wr','grip','reach']){const kk=key==='grip'||key==='reach'?k*.6:k;v[key]+=((t[key]-cj[key])*kk-v[key]*dm)*dt;cj[key]+=v[key]*dt}
    cj.reach=cl(cj.reach,.4,2.6);cj.grip=cl(cj.grip,0,1);for(const key of['sh','el','wr'])if(Math.abs(cj[key])>200)cj[key]-=Math.round(cj[key]/360)*360;
    const f=armFK(i,cj),ex=f[0]*rr,ey=f[1]*rr,hx=f[2]*rr,hy=f[3]*rr,tx=E.x+hx*c-hy*s,ty=oy0+hx*s+hy*c;
    J.elbow[i].x=E.x+ex*c-ey*s;J.elbow[i].y=oy0+ex*s+ey*c;if(!h.init){h.x=tx;h.y=ty;h.init=1}
    if(REDUCE){h.x=tx;h.y=ty}else{h.vx+=((tx-h.x)*700-h.vx*45)*dt;h.vy+=((ty-h.y)*700-h.vy*45)*dt;h.x+=h.vx*dt;h.y+=h.vy*dt}}
  if(!REDUCE&&E.alpha>.5&&Math.random()<dt*(1.5+E.F.flame*2+ang*8))parts.push({k:'spark',x:E.x+E.tipX*.9*rr+rnd(-.15,.15)*rr,y:E.y-E.tipH*rr*.95,vx:rnd(-10,10)-E.vx*.05,vy:rnd(-50,-28)*(1+ang),l:1,d:rnd(.7,1.4),s:rnd(.6,1.2),c:ang>.5?'255,90,60':'255,180,90'});
  if(!REDUCE&&(ang>.5||E.puff>0)&&Math.random()<dt*5)parts.push({k:'smoke',x:E.x+E.tipX*rr+rnd(-.2,.2)*rr,y:E.y-E.tipH*rr,vx:rnd(-12,12),vy:rnd(-45,-25),l:1,d:1.3,s:rnd(4,7)});
  if(E.hum&&!REDUCE&&Math.random()<dt*1.8)parts.push({k:'note',x:E.x+rr*.9,y:E.y-rr*.4,vx:rnd(10,25),vy:-35,l:1,d:1.6,s:11});
  if(E.zz&&!REDUCE&&Math.random()<dt*1.2)parts.push({k:'z',x:E.x+rr*.6,y:E.y-rr*.5,vx:16,vy:-26,l:1,d:2,s:rnd(9,13)});
  if(E.bubble){const b=E.bubble;b.t+=dt;if(b.type){const i=Math.floor(b.t*b.cps);if(i!==b.last&&i<b.text.length){b.last=i;const ch=b.text[i]||'';if(ch==='?'||ch==='!')E.brow=.55;E.flapT=/[aeiouyAEIOUY]/.test(ch)?.75:/[ ,.!?…]/.test(ch)?0:.35}}if(b.t>b.d&&!b.dots)E.bubble=null}
  E.flapT*=Math.pow(.002,dt);E.flap+=(E.flapT-E.flap)*Math.min(1,dt*28);E.hum=cur_('hum')?1:0}
function hit(v,ax){const s=Math.abs(v);if(ax==='y')E.q-=cl(s/2200,0,.5);else E.tv+=Math.sign(v)*s/180;if(s>700)E.dizzyHit=1}
function afterThrow(){const hard=E.thrown>900;E.thrown=0;if(E.dizzyHit){E.dizzyHit=0;E.dizzy=1.6}
  try{mindEvent('thrown',{what:'they threw Ember '+(hard?'hard':'gently')+' across the screen'})}catch(_){}}

/* ===== drawing ===== */
const ink='#3d1308';
function crPath(pts){const n=pts.length;X.beginPath();X.moveTo(pts[0][0],pts[0][1]);
  for(let i=0;i<n;i++){const p0=pts[(i-1+n)%n],p1=pts[i],p2=pts[(i+1)%n],p3=pts[(i+2)%n];X.bezierCurveTo(p1[0]+(p2[0]-p0[0])/6,p1[1]+(p2[1]-p0[1])/6,p2[0]-(p3[0]-p1[0])/6,p2[1]-(p3[1]-p1[1])/6,p2[0],p2[1])}X.closePath()}
function bodyPath(){const th=E.tipH,tx=E.tipX,sl=E.side[0],sr=E.side[1],h=th/1.85;
  crPath([[0,1.03],[.66,.86],[1.0,.32],[.98,-.28],[.9+sr*.4,-.98*h],[.56+sr*.15,-.62*h],[.36+tx*.5,-th*.62],[tx+.04,-th],[-.28+tx*.5,-th*.6],[-.5+sl*.15,-.7*h],[-.86+sl*.4,-1.08*h],[-1.0,-.3],[-1.0,.32],[-.66,.86]])}
function mixc(a,b,t){return a.map((v,i)=>Math.round(lerp(v,b[i],t)))}
function bodyGrad(b,ang){const g=X.createLinearGradient(0,1.05,0,-E.tipH),dim=[205,86,48];
  for(const [o,c0] of[[0,[255,205,64]],[.38,[255,160,52]],[.68,[255,104,72]],[1,[255,72,104]]]){let c=c0;if(E.tint)c=mixc(c,E.tint,.68);if(ang>0)c=mixc(c,o>.4?[255,40,30]:[255,140,40],ang*.6);if(b<1)c=mixc(c,dim,Math.min(1,(1-b)*1.7));else c=c.map(v=>Math.min(255,Math.round(v*b)));g.addColorStop(o,`rgb(${c.join(',')})`)}return g}
const FONT='-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif';
function mendObj(o,def,what){let n=0;for(const k in o){if(typeof o[k]==='number'&&!isFinite(o[k])){o[k]=k in def?def[k]:0;n++}}if(n)bad(what);return n}
function mend(){
  const e0={r:1,rT:1,alpha:1,alphaT:1,tipH:1.85,speed:1,nextBlink:2};
  if(!isFinite(E.x)||!isFinite(E.y)){const s=SP.rest();E.x=E.tx=s.x;E.y=E.ty=s.y;E.vx=E.vy=0;bad('body position')}
  mendObj(E,e0,'body value');
  for(const h of E.hands)mendObj(h,{},'hand');for(const a of[E.side,E.ms,E.pointDir])for(let i=0;i<a.length;i++)if(!isFinite(a[i])){a[i]=0;bad('body vector')}
  for(const f of[E.F,E.FT])if(f&&typeof f==='object')mendObj(f,{},'face dial');
  mendObj(J.b,{},'body joint');mendObj(J.bv,{},'body joint speed');
  J.a.forEach((a,i)=>{mendObj(a,{sh:-10,el:125,reach:1},'arm joint');mendObj(J.av[i],{},'arm joint speed');mendObj(J.elbow[i],{},'elbow')});
  for(let i=0;i<2;i++)if(J.hold[i]&&!Object.values(J.hold[i]).every(fin)){J.hold[i]=null;bad('held pose')}
  if(J.body&&!Object.values(J.body).every(fin)){J.body=null;bad('held body')}
  if(!isFinite(J.lift)){J.lift=0;bad('lift')}if(!isFinite(J.speed)){J.speed=1;bad('joint speed')}
  J.beats=J.beats.filter(b=>Object.values(b).every(fin));mendObj(AFF,{a:.4},'feeling')}
function draw(dt){const keep=E.F;E.F=feltFace(keep);try{drawAll(dt)}finally{E.F=keep}}
function drawAll(dt){X.setTransform(DPR,0,0,DPR,0,0);X.clearRect(0,0,W,H);const rr=R*E.r,F=E.F;
  if(TW.on&&TW.el){const s=rect(TW.el)||TW.spot;TW.spot=s;if(s){const p=8;X.save();X.fillStyle='rgba(8,6,5,.5)';X.fillRect(0,0,W,H);
    X.globalCompositeOperation='destination-out';X.beginPath();X.roundRect(s.l-p,s.t-p,s.w+2*p,s.h+2*p,14);X.fill();X.globalCompositeOperation='source-over';
    X.strokeStyle=`rgba(232,135,95,${(.55+.35*Math.sin(now*4)).toFixed(3)})`;X.lineWidth=2;X.beginPath();X.roundRect(s.l-p,s.t-p,s.w+2*p,s.h+2*p,14);X.stroke();X.restore()}}
  for(let i=parts.length-1;i>=0;i--){const p=parts[i];p.l-=dt/p.d;if(p.l<=0){parts.splice(i,1);continue}
    p.vy+=(p.k==='spark'?40:p.k==='dust'?260:p.k==='fire'?-40:-6)*dt;p.vx*=.985;p.x+=p.vx*dt;p.y+=p.vy*dt;
    if(p.k==='spark'){X.globalCompositeOperation='lighter';const g=X.createRadialGradient(p.x,p.y,0,p.x,p.y,p.s*3);g.addColorStop(0,`rgba(${p.c},${p.l})`);g.addColorStop(1,`rgba(${p.c},0)`);X.fillStyle=g;X.beginPath();X.arc(p.x,p.y,p.s*3,0,7);X.fill();X.globalCompositeOperation='source-over'}
    else if(p.k==='dust'){X.fillStyle=`rgba(${p.c||'112,94,70'},${(.8*p.l).toFixed(3)})`;X.beginPath();X.arc(p.x,p.y,p.s,0,7);X.fill()}
    else if(p.k==='smoke'){X.fillStyle=`rgba(120,110,110,${.35*p.l})`;X.beginPath();X.arc(p.x,p.y,p.s*(2-p.l),0,7);X.fill()}
    else if(p.k==='fire'){X.globalCompositeOperation='lighter';const g=X.createRadialGradient(p.x,p.y,0,p.x,p.y,p.s*2);g.addColorStop(0,`rgba(${p.c},${p.l})`);g.addColorStop(1,`rgba(${p.c},0)`);X.fillStyle=g;X.beginPath();X.arc(p.x,p.y,p.s*2,0,7);X.fill();X.globalCompositeOperation='source-over'}
    else if(p.k==='ring'){X.strokeStyle=`rgba(${p.c},${(p.l*.75).toFixed(3)})`;X.lineWidth=2.5*p.l;X.beginPath();X.arc(p.x,p.y,p.s*(2-p.l),0,7);X.stroke()}
    else if(p.k==='star'){X.fillStyle=`rgba(${p.c},${p.l})`;X.font=`${p.s}px ${FONT}`;X.fillText('✦',p.x,p.y)}
    else if(p.k==='heart'){X.fillStyle=`rgba(255,120,150,${p.l})`;const s=p.s;X.beginPath();X.moveTo(p.x,p.y+s*.35);X.bezierCurveTo(p.x-s*.7,p.y-s*.2,p.x-s*.35,p.y-s*.8,p.x,p.y-s*.4);X.bezierCurveTo(p.x+s*.35,p.y-s*.8,p.x+s*.7,p.y-s*.2,p.x,p.y+s*.35);X.fill()}
    else{X.fillStyle=`rgba(255,225,200,${Math.sin(p.l*Math.PI)})`;X.font=`600 ${p.s}px ${FONT}`;X.fillText(p.k==='z'?'z':'♪',p.x,p.y)}}
  if((V.state==='listening'||V.state==='asking')&&E.alpha>.3){const k0=REDUCE?0:now*.9;for(let k=0;k<3;k++){const ph=REDUCE?k/3:(k0+k/3)%1;X.strokeStyle=`rgba(255,165,95,${(.5*(1-ph)).toFixed(3)})`;X.lineWidth=2;X.beginPath();X.arc(E.x,E.y-rr*.25,rr*(1.25+ph*1.6),0,7);X.stroke()}}
  if(E.alpha<.03||rr<1.5)return;X.globalAlpha=E.alpha;
  const glowC=E.tint?E.tint.join(','):F.angry>.5?'255,60,40':'255,120,50';
  const lg=X.createRadialGradient(E.x,E.y,0,E.x,E.y,rr*6);lg.addColorStop(0,`rgba(${glowC},${.12*F.bright})`);lg.addColorStop(1,`rgba(${glowC},0)`);X.fillStyle=lg;X.fillRect(E.x-rr*6,E.y-rr*6,rr*12,rr*12);
  const bob=REDUCE?0:(E.mode==='fly'&&!E.perch?Math.sin(now*2.2)*rr*.06:0),br=REDUCE?0:(E.breath||0)*(.016+AFF.a*.012),by=E.y+bob-J.b.rise*rr*.5,ca=Math.cos(E.tilt+E.spin),sa=Math.sin(E.tilt+E.spin);
  for(let i=0;i<2;i++){const s=i?1:-1,h=E.hands[i],shx=s*.84*rr,shy=.32*rr,sx=E.x+shx*ca-shy*sa,sy=by+shx*sa+shy*ca,el=J.elbow[i],ex=el.x,ey=el.y+bob;
    const cx=2*ex-(sx+h.x)/2,cy=2*ey-(sy+h.y)/2;
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
  const fx=E.gx*.1+J.b.turn*.24,fy=E.gy*.07;
  if(F.blush>.02)for(const s of[-1,1]){const g=X.createRadialGradient(s*.6+fx,.5+fy,0,s*.6+fx,.5+fy,.22);g.addColorStop(0,`rgba(255,80,110,${.5*F.blush})`);g.addColorStop(1,'rgba(255,80,110,0)');X.fillStyle=g;X.beginPath();X.arc(s*.6+fx,.5+fy,.22,0,7);X.fill()}
  for(const s of[-1,1])eye(s,fx,fy,G);brows(fx,fy);mouth(fx*.8,.66+fy*.8);
  if(F.angry>.4){X.strokeStyle=`rgba(140,20,20,${(F.angry-.4)*1.4})`;X.lineWidth=.05;X.lineCap='round';const vx=.55+fx,vy=-.62+fy;for(const [a,b,c,d] of[[-.06,-.06,.06,.06],[.06,-.06,-.06,.06],[0,-.09,0,.09],[-.09,0,.09,0]]){X.beginPath();X.moveTo(vx+a,vy+b);X.lineTo(vx+c*.4,vy+d*.4);X.stroke()}}
  if(E.shadesOn)shades(fx,fy);hat();X.restore();
  if(E.sweat){const sx=E.x+rr*.95,sy=E.y-rr*.25+Math.sin(now*3)*2;X.fillStyle='rgba(200,232,255,.95)';X.beginPath();X.moveTo(sx,sy-rr*.24);X.bezierCurveTo(sx+rr*.16,sy,sx+rr*.16,sy+rr*.16,sx,sy+rr*.16);X.bezierCurveTo(sx-rr*.16,sy+rr*.16,sx-rr*.16,sy,sx,sy-rr*.24);X.fill()}
  for(let i=0;i<2;i++){const h=E.hands[i],s=i?1:-1,j=J.a[i],gp=j.grip;X.save();X.translate(h.x,h.y);X.rotate(E.tilt+E.spin-s*(((j.sh+j.el)-110)*.45+j.wr)*DEG);X.scale(rr,rr);const g=X.createRadialGradient(-.06,-.08,0,0,0,.28);g.addColorStop(0,'#ffe9b0');g.addColorStop(.6,'#ffad50');g.addColorStop(1,'#f0703c');X.fillStyle=g;
    X.beginPath();X.ellipse(0,0,.22-gp*.03,.2,0,0,7);X.fill();X.beginPath();X.arc(-s*(.19-gp*.07),-.1+gp*.05,.09*(1-gp*.35),0,7);X.fill();X.restore()}
  if((E.think||MIND.busy||now<MIND.cueUntil)&&!REDUCE){for(let i=0;i<3;i++){const ph=(Math.sin((now*2.4+i*.55)*2)+1)/2;X.fillStyle=`rgba(255,232,210,${.35+.55*ph})`;X.beginPath();X.arc(E.x-rr*1.2-i*rr*.42,E.y-rr*1.5-i*rr*.38,rr*(.09+i*.04),0,7);X.fill()}
    if(!E.bubble&&(MIND.busy||now<MIND.cueUntil)){const cx=E.x-rr*2.55,cy=E.y-rr*2.75;X.fillStyle='rgba(255,247,238,.92)';X.beginPath();X.roundRect(cx-13,cy-9,26,18,9);X.fill();X.fillStyle='#2b160c';X.font=`700 12px ${FONT}`;X.textAlign='center';X.textBaseline='middle';X.fillText('…',cx,cy);X.textAlign='start'}}
  X.globalAlpha=1;if(E.bubble)bubble(rr)}
function eye(s,fx,fy,G){const F=E.F;
  if(F.name==='dizzy'&&E.FT.name==='dizzy'){const cx=s*.35+fx,cy=.2+fy;X.strokeStyle=ink;X.lineWidth=.06;X.lineCap='round';X.beginPath();for(let i=0;i<44;i++){const t=i/44*Math.PI*4+now*8*s,r=.015+i*.0042;i?X.lineTo(cx+Math.cos(t)*r,cy+Math.sin(t)*r):X.moveTo(cx,cy)}X.stroke();return}
  const cx=s*.36+fx,cy=.18+fy,rx=.235*F.eyeS*(1-.32*Math.max(0,s*J.b.turn)),ry=.27*F.eyeS;X.save();X.beginPath();X.ellipse(cx,cy,rx,ry,0,0,7);X.clip();
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
function start(){if(running)return;if(!started){started=true;(document.head||document.documentElement).append(css);document.body.append(cv,talk,menu,ofr);tin=talk.querySelector('input');
    talk.querySelector('#ember-go').onclick=send;talk.querySelector('#ember-mic').onclick=()=>{closeChat();listen()};tin.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();send()}if(e.key==='Escape')closeChat(L(['okay, later!','fine.','rude.']))});
    tin.addEventListener('input',()=>{CHAT.last=now;CHAT.typing=now;talk.classList.toggle('ae-empty',!tin.value.trim())});
    addEventListener('keydown',()=>{MIND.key=now},true);watchScreen();for(const b of store.get('ember_lib',[]))learnB(b);
    fetch('/api/ember/behaviors').then(r=>r.json()).then(j=>{for(const b of(j&&j.behaviors)||[])learnB(b)}).catch(()=>{});
    addEventListener('pointermove',onMove,true);addEventListener('pointerdown',onDown,true);addEventListener('pointerup',onUp,true);
    for(const t of['click','dblclick','mousedown','mouseup','contextmenu'])addEventListener(t,e=>{if(mine(e))return;if(t==='dblclick'&&onBody(e.clientX,e.clientY)){e.preventDefault();e.stopPropagation();converse();return}if(t==='contextmenu'&&onBody(e.clientX,e.clientY)){e.preventDefault();e.stopPropagation();return}if(t!=='contextmenu')swallow(e)},true);
    addEventListener('resize',resize);addEventListener('blur',()=>{cur.x=-1});
    document.addEventListener('input',e=>{if(e.target&&e.target.closest&&e.target.closest('.composer')&&e.target!==tin)signal('typing')},true);
    let hiddenAt=0;document.addEventListener('visibilitychange',()=>{if(document.hidden)hiddenAt=Date.now();else if(hiddenAt&&Date.now()-hiddenAt>20*60*1000)signal('back')})}
  resize();running=true;cv.hidden=false;E.alpha=0;E.alphaT=1;const s=SP.rest();E.x=s.x+R*3;E.y=s.y-R*4;E.tx=s.x;E.ty=s.y;last=performance.now();
  if(willWelcome())setTimeout(()=>welcome(),1200);
  requestAnimationFrame(frame)}
function stop(){running=false;cv.hidden=true;talk.hidden=true;menu.hidden=true;ofr.hidden=true;AG.offer=null;CHAT.open=0;document.documentElement.classList.remove('ember-over','ember-grab');if(started)X.clearRect(0,0,cv.width,cv.height)}
function set(o){if(o&&'fit' in o)FITOV=null;Object.assign(CFG,o||{});if(CFG.att==='savage'&&!isAdmin())CFG.att='sassy';store.set('ember_cfg',CFG);
  if(DESK){start();return}
  if(CFG.on&&!CFG.desktop&&!automation)start();else stop()}
/* desktop mode: the in-app Ember steps aside and the app's signals go to the one on the desktop */
async function setDesktop(on){const r=await post('/api/ember/desktop',{on:!!on});CFG.desktop=!!r.on;store.set('ember_cfg',CFG);set({});return r}
window.ember={signal,fromEvents,set,desk,setDesktop,get:()=>Object.assign({},CFG,{savage:isAdmin(),running,desk:DESK,mindStatus:mindStatus()}),
  getSettings:async()=>{const r=await fetch('/api/ember/settings');return r.json()},
  saveSettings:s=>post('/api/ember/settings',s),
  adoptCharacter:c=>post('/api/ember/adopt_character',{character:c}),
  talk:()=>startTalk(),listen:()=>listen(),welcome:f=>welcome(f),willWelcome:()=>willWelcome(),tour:()=>({on:!!TW.on,step:TW.n?(TW.bar&&TW.bar.querySelector('.n').textContent):''}),hear:t=>heard(String(t||'')),speak:(t,a)=>speakAloud(t,a),voiceLog:()=>VLOG.slice(-10),perform:r=>perform(r),voice:()=>({conv:!!V.conv,state:V.state,text:V.text,driving:!!DRV.on,press:DRV.press||null}),
  forget(){CHAT.mem={name:'',facts:[]};saveMem()},memory:()=>JSON.parse(JSON.stringify(CHAT.mem)),
  state:()=>({face:E.FT.name,pose:E.pose,bubble:E.bubble?E.bubble.text:'',x:E.x,y:E.y,r:R*E.r,mood:mood?(mood.kind||'on'):'',chat:!!CHAT.open,alpha:E.alpha,running,act:act?act.k:'',actBy:act&&act.by||'',stage:act&&act.stage||'',mode:E.mode,seat:!!E.seat,body:Object.assign({},J.b),arms:J.a.map(a=>Object.assign({},a)),held:!!J.held,feeling:{v:AFF.v,a:AFF.a},talking:isTalking(),outfit:outfit(),shades:!!E.shadesOn,drives:Object.assign({},N),noErrands:Object.fromEntries(Object.entries(NO_ERR).filter(([,u])=>u>now).map(([k,u])=>[k,Math.round(u-now)]))}),
  fall:()=>fall(),perch:()=>{SEQ=null;mood=null;act={k:'perch',by:'test',a:{w:()=>0,run(){},dur:[60,60]},t:0,dur:60};go(SP.perch,{speed:.8})},
  feel:f=>feel(f),play:st=>{const c=compileB({steps:st}),d=c.reduce((t,x)=>t+(x.d||0),0);act={k:'test',by:'test',a:{w:()=>0,run(){},dur:[d,d]},t:0,dur:d+.3};play(c)},
  still:s=>{NS.quiet=now+(+s||0);if(act&&act.by==='body'){act.t=act.dur+1;SEQ=null}},nervous:()=>({count:NS.count,kinds:NS.kinds.slice(),quiet:NS.quiet>now,fresh:NS.fresh.length}),
  mind:()=>({live:!!MIND.live,off:!!MIND.off,why:MIND.why||'',busy:!!MIND.busy,cue:!!(MIND.busy||now<MIND.cueUntil),thought:MIND.thought,focus:MIND.focus||'',plan:MIND.plan.slice(),hist:MIND.hist.slice(),ev:MIND.ev.map(e=>e.what),lib:Object.keys(LIB),next:Math.round(MIND.next-now),today:MSTAT.today,lastAt:MSTAT.lastAt,status:mindStatus(),acts:ACTS_LOG.slice(-12)}),think:r=>{MIND.off=0;MIND.next=0;think(r||'asked')},
  agency:()=>({level:agency(),offer:AG.offer?{id:AG.offer.id,text:AG.offer.text,yes:AG.offer.yes}:null,queue:AG.queue.map(o=>o.id),log:AG.log.map(x=>x.what),fetching:!!AG.fetching,staged:AG.staged,jobs:Object.assign({},AG.jobs||{})}),
  agTick:()=>{AG.worldT=-99;agTick(1)},
  health:()=>({bad:BAD.slice()}),
  frames:(ms,n)=>{for(let i=0;i<(n|0);i++)frame(last+(+ms||16))},
  hurry:()=>{N.energy=.8;N.boredom=.4;MIND.next=0;if(act)act.t=act.dur+1}};
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
    r_g = reply([{"role": "user", "content": "take your glasses off"}], "", "sassy",
                call=lambda p: '{"say":"fine.","face":"calm","gesture":"none","mood":0,"plan":[{"glasses":"off"}]}')
    check("reply() returns the mind's plan fields when told to take glasses off",
          r_g.get("plan") == [{"glasses": "off"}])
    r_b = reply([{"role": "user", "content": "wear a beanie"}], "", "sassy",
                call=lambda p: '{"say":"cozy.","face":"smug","gesture":"hips","mood":0.2,"outfit":"beanie","for":25}')
    check("reply() accepts top-level outfit/glasses/stop fields as plan steps",
          r_b.get("plan") == [{"outfit": "beanie", "for": 25.0}])
    r_s = reply([{"role": "user", "content": "stop cleaning for 15 minutes"}], "", "sassy",
                call=lambda p: '{"say":"fine, live in dust.","face":"unimpressed","gesture":"crossed","mood":0}')
    check("reply() attaches local wish plan when the person asks to stop cleaning",
          r_s.get("plan") == [{"stop": "dust", "for": 15.0}])
    a_g = act("take your glasses off", "", "sassy", call=lambda p: 1/0)
    a_b = act("wear a beanie", "", "sassy", call=lambda p: 1/0)
    a_s = act("stop cleaning for 10 minutes", "", "sassy", call=lambda p: 1/0)
    check("spoken wishes for glasses, outfit and stopping cleaning return answer plans",
          a_g["kind"] == "answer" and a_g.get("plan") == [{"glasses": "off"}]
          and a_b["kind"] == "answer" and a_b.get("plan") == [{"outfit": "beanie", "for": 20.0}]
          and a_s["kind"] == "answer" and a_s.get("plan") == [{"stop": "dust", "for": 10.0}])
    import tempfile
    os.environ["AETHRON_HOME"] = tempfile.mkdtemp()
    seen = {}

    def brain(msgs):
        seen["m"] = msgs
        return json.dumps({"thought": "they went quiet; a sneeze will get attention", "mood": "mischief",
                           "plan": [{"go": "cursor"}, {"do": "big_sneeze"}, {"do": "rm -rf"}, {"say": "bless me."},
                                    {"outfit": "witch", "for": 9999}, {"glasses": "peek"}, {"wait": 4}],
                           "new": [{"name": "Big Sneeze!", "about": "a huge sneeze", "steps": [
                               {"t": .5, "face": "presneeze", "fx": {"mo": 9, "lidU": .6, "evil": 3}, "tilt": -.5},
                               {"t": 99, "pose": "flail", "hop": .7, "burst": {"kind": "spark", "n": 500, "color": "#ffaa00"},
                                "hands": [[-9, 0], [1.5, -3]], "say": "ACHOO"},
                               {"t": 1, "pose": "nonsense"}]}], "remember": "works late on fridays"})
    m = mind({"time": "11:40 pm"}, "sassy", ["yawn", "dust", "sneeze"], brain)
    check("the mind is told who Ember is, what it can do and what it perceives",
          "Calcifer" in seen["m"][0]["content"] and "yawn" in seen["m"][0]["content"] and "11:40 pm" in seen["m"][1]["content"])
    nb = m["new"][0] if m["new"] else {}
    check("an invented behaviour is named safely and kept to the body's limits",
          nb.get("name") == "big_sneeze" and nb["steps"][0]["fx"] == {"mo": 1.8, "lidU": .6}
          and nb["steps"][1]["t"] == 6 and nb["steps"][1]["burst"]["n"] == 30 and nb["steps"][1]["hands"][0][0] == -2.4)
    js = motion_step({"t": 2, "arms": {"right": {"shoulder": 999, "elbow": 30, "grip": 2}, "both": {"shoulder": 40}},
                      "body": {"lean": -90, "turn": .4, "wobble": 3}, "speed": 9,
                      "osc": [{"joint": "right.elbow", "amp": 400, "hz": 99}, {"joint": "nose.wiggle", "amp": 5}],
                      "feel": {"valence": 3, "arousal": .7}})
    check("a keyframe's joints are kept to the body's range, in degrees",
          js and js["arms"] == {"R": {"sh": 200, "el": 30, "grip": 1}, "L": {"sh": 40}}
          and js["body"] == {"lean": -35, "turn": .4} and js["speed"] == 3
          and js["osc"] == [{"j": "R.el", "amp": 60, "hz": 6}] and js["feel"] == {"v": 1, "a": .7})
    flat = motion_step({"t": 1, "lean": 12, "right_arm": {"shoulder": 150}})
    check("joints written at the top level of a keyframe are understood",
          flat and flat["body"] == {"lean": 12} and flat["arms"] == {"R": {"sh": 150}})
    check("a plan can doze, change how Ember feels, and hold a posture while it waits",
          plan_step({"sleep": 999}) == {"sleep": 300} and plan_step({"feel": {"valence": -.5}}) == {"feel": {"v": -.5}}
          and plan_step({"wait": 3, "arms": {"left": {"shoulder": 100, "elbow": -155}}, "body": {"crouch": .2}})
          == {"wait": 3, "arms": {"L": {"sh": 100, "el": -155}}, "body": {"crouch": .2}})
    rules_text = seen["m"][0]["content"]
    check("the mind is told it owns its body: joints in degrees, no routines, feelings of its own",
          "shoulder" in rules_text and "no routines" in rules_text and "Feelings come and go by themselves" in rules_text
          and "patrol" not in rules_text and "errand" not in rules_text)
    check("a keyframe with nothing the body has is dropped", len(nb.get("steps", [])) == 2)
    check("plan steps nobody knows are dropped, the rest kept in order",
          [list(x)[0] for x in m["plan"]] == ["go", "do", "say", "outfit", "glasses", "wait"] and m["plan"][3]["for"] == 120)
    check("an invented behaviour is saved for next time", [b["name"] for b in behaviours()] == ["big_sneeze"])
    check("next time the mind is told it knows it", "big_sneeze" in mind_rules("sassy", ["big_sneeze"]))
    m2 = mind({}, "sassy", [], lambda msgs: '{"plan":[{"do":"big_sneeze"}]}')
    check("a learned behaviour can be asked for again", m2["plan"] == [{"do": "big_sneeze"}])
    m3 = mind({}, "sassy", [], lambda msgs: json.dumps({
        "thought": "drift to the upper-left and watch them with one brow raised",
        "focus": "the person",
        "plan": [
            {"go": [0.22, 0.28]},
            {"t": 1.4, "pose": "crossed", "fx": {"bL": 0.4, "lidU": 0.5, "mas": 0.6}, "hands": [[-0.8, 0.4], [0.8, 0.4]]},
            {"wait": 18, "look": "viewer", "face": "curious", "fx": {"bR": 0.3}},
        ],
    }))
    check("mind() accepts free [x, y] coordinates, inline keyframe steps, and rich stillness waits",
          m3.get("focus") == "the person"
          and m3["plan"][0] == {"go": [0.22, 0.28]}
          and "steps" in m3["plan"][1] and m3["plan"][1]["steps"][0]["fx"] == {"bL": 0.4, "lidU": 0.5, "mas": 0.6}
          and m3["plan"][2] == {"wait": 18.0, "look": "viewer", "face": "curious", "fx": {"bR": 0.3}})
    r_d = reply([{"role": "user", "content": "dance for me"}], "", "sassy",
                call=lambda p: '{"say":"fine. watch this.","face":"smug","gesture":"dance","mood":0.3}')
    a_d = act("dance for me", "", "sassy", call=lambda p: 1/0)
    check("asking Ember to dance in chat or voice attaches physical multi-keyframe choreography",
          bool(r_d.get("plan")) and "steps" in r_d["plan"][0] and any(s.get("spin") for s in r_d["plan"][0]["steps"])
          and a_d["kind"] == "answer" and "steps" in a_d["plan"][0])
    try:
        mind({}, "sassy", [], lambda msgs: '{"plan":[{"teleport":"moon"}]}')
        bad = False
    except ValueError:
        bad = True
    check("an answer with nothing Ember can do is refused (the window keeps its own behaviour)", bad)
    check("the companion script has no triple quote that would end this module", '"""' not in EMBER_JS)
    check("the companion stays off in automated browsers unless asked", "navigator.webdriver&&!forced" in EMBER_JS)
    print(f"\n{ok} passed, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
