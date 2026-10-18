#!/usr/bin/env python3
"""THE LOOP. A brief goes in; a project that PASSES comes out, or nothing.

This is the piece the rest of the toolkit was built for. `aethron_eye`
measures a page against a target. `aethron_design` holds it to a system.
Neither of them drives anything — and a referee nobody is running is a
referee nobody is using.

WHAT EVERY OTHER TOOL IN THIS CLASS DOES: generate, render, hand it
over. The hand-over is unconditional. Cursor and Claude Code do not
render at all; Lovable and v0 render for a person to judge; the best
published research loop, UI2Code^N, renders and asks a vision model
whether it got better, which its own paper admits oscillates.

WHAT THIS DOES: refuses. A build is ACCEPTED only when the checks pass,
and if no revision passes, the honest output is the best attempt plus
the list of what is still wrong — never a green light over a red page.
That is the same contract `verify -> probe -> heal` has enforced on
template migrations since the rogue-agent incident: THE AGENT NEVER
DECIDES SUCCESS, THE CHECKS DO.

THREE TARGETS, AND THE THIRD IS THE ONE NOBODY CHECKS:

    a screenshot / a URL   the eye measures against it, element by
                           element, at four widths
    a design system        declared, or read off a reference — type
                           scale, spacing grid, contrast, tap targets
    THE BRIEF ITSELF       "a pricing page with three tiers" contains
                           testable claims: the words the user asked
                           for, and the COUNTS they asked for

That last one closes the gap that made "build me a dashboard"
ungradeable. A brief is not a vague wish; most of it is a requirements
list nobody was reading. If the user asked for three tiers and the page
has two, that is not a matter of taste — it is a defect, and it is
measurable without a model.

MONOTONE, ALWAYS. Every revision is scored against the same contract and
kept only if it scores better. A model's revision is a coin flip; a
change kept only when it measures better cannot lose. The project left
on disk is the best one seen, not the last one tried.
"""
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aethron_eye as EYE            # noqa: E402
import aethron_design as DES         # noqa: E402

NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
           "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
           "a": 1, "an": 1}

# Words that carry no requirement — asking for "a page" is not a claim
# that the word "page" appears on it.
NOISE = {"page", "site", "website", "app", "design", "layout", "build",
         "make", "create", "with", "that", "this", "using", "look",
         "looks", "like", "and", "the", "for", "from", "into", "some",
         "very", "really", "please", "want", "need", "should", "must",
         "have", "has", "its", "it's", "your", "you", "our", "will",
         "can", "clean", "modern", "simple", "nice", "good", "great",
         "beautiful", "responsive", "mobile", "desktop", "dark", "light"}


# ─────────────────────────── the brief as a check ────────────────────

def requirements(brief):
    """The testable claims inside a brief.

    A BRIEF IS NOT A VAGUE WISH. "A pricing page with three tiers, a FAQ
    and a Get Started button" contains four things a rendered page can
    be asked about, and nobody was asking. Extracted here:

        quoted strings   "Get Started" — exact copy the user specified,
                         and the only part of a brief that is literal
        counted nouns    "three tiers" -> at least 3 repeated structures
        proper nouns     Stripe, Northwind — names that must appear

    Deliberately CONSERVATIVE. A requirement that fires on a page which
    honoured the brief is worse than a requirement that misses, because
    the loop would spend its rounds chasing a phantom — and this project
    has already watched a correction loop get further from the answer
    every round it ran.
    """
    reqs = []
    for q in re.findall(r'"([^"]{2,60})"|“([^”]{2,60})”',
                        brief):
        text = (q[0] or q[1]).strip()
        if text:
            reqs.append({"kind": "text", "value": text})
    low = brief.lower()
    for m in re.finditer(r"\b(\d{1,2}|" + "|".join(NUMBERS) + r")\s+"
                         r"([a-z][a-z-]{2,20}s)\b", low):
        word, noun = m.group(1), m.group(2)
        n = int(word) if word.isdigit() else NUMBERS.get(word, 0)
        if n >= 2 and noun not in NOISE:
            reqs.append({"kind": "count", "value": noun, "n": n})
    for w in re.findall(r"\b([A-Z][a-zA-Z]{2,})\b", brief):
        if w.lower() in NOISE or len(w) < 3:
            continue
        if any(r["kind"] == "text" and w in r["value"] for r in reqs):
            continue
        reqs.append({"kind": "name", "value": w})
    seen, out = set(), []
    for r in reqs:
        k = (r["kind"], r["value"].lower())
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _groups(items, tol=0.12):
    """Repeated structures — cards, tiers, rows — by their shape.

    Three pricing tiers are three boxes of the SAME SIZE sitting in a
    row. That is a measurement, not a guess, and it is how "three
    tiers" becomes checkable without anyone naming a class.
    """
    boxes = [i for i in items
             if i.get("w", 0) > 60 and i.get("h", 0) > 60]
    used, groups = set(), []
    for i, a in enumerate(boxes):
        if i in used:
            continue
        same = [a]
        used.add(i)
        for j, b in enumerate(boxes):
            if j <= i or j in used:
                continue
            if (abs(a["w"] - b["w"]) <= tol * max(a["w"], b["w"])
                    and abs(a["h"] - b["h"]) <= tol * max(a["h"], b["h"])):
                same.append(b)
                used.add(j)
        if len(same) >= 2:
            groups.append(same)
    return groups


def covers(reqs, items):
    """Which of the brief's claims the page actually honours."""
    blob = " ".join((i.get("text") or "") for i in items).lower()
    groups = _groups(items)
    biggest = max((len(g) for g in groups), default=0)
    out = []
    for r in reqs:
        if r["kind"] in ("text", "name"):
            ok = EYE._norm(r["value"]) in EYE._norm(blob)
            if not ok:
                out.append({
                    "kind": "NOT IN BRIEF" if False else "MISSING FROM BRIEF",
                    "text": r["value"], "selector": None, "tag": "brief",
                    "want": r["value"], "got": None,
                    "fix": f'the brief asks for "{r["value"]}" and it is '
                           f"not on the page",
                })
        elif r["kind"] == "count":
            if biggest < r["n"]:
                out.append({
                    "kind": "WRONG COUNT", "text": r["value"],
                    "selector": None, "tag": "brief",
                    "want": r["n"], "got": biggest,
                    "fix": f'the brief asks for {r["n"]} {r["value"]}; '
                           f"the page's largest group of repeated "
                           f"elements is {biggest}",
                })
    return out


# ────────────────────────────── the contract ─────────────────────────

def check(project, brief="", reference=None, widths=None, spec=None,
          verbose=True, taste=None):
    """Everything the build must satisfy, in one verdict.

    Four questions, and a build has to answer all of them:
        does it contain what was ASKED FOR       (the brief)
        does it match the TARGET                 (the eye)
        is it a good interface                   (the design referee)
        does it survive a narrow screen          (spill, at every width)
    """
    def say(*a):
        if verbose:
            print(*a)

    widths = tuple(widths or (390, 768, 1280, 1920))
    # TASTE RULES AND FIDELITY PULL IN OPPOSITE DIRECTIONS, so the mode
    # decides which one wins. With a REFERENCE the job is a clone, and a
    # rule like "no flat hierarchy" or "no gradient hero" would "improve"
    # the design away from the thing the user asked to copy. With only a
    # BRIEF there is nothing to copy, and the model falls back on its
    # defaults — the generic look the rules exist to refuse.
    if taste is None:
        taste = reference is None
    page = _entry(project)
    if page is None:
        return {"verdict": "SKIPPED", "score": 0.0,
                "why": "no page to check — the build produced nothing",
                "findings": []}
    read = EYE.read_page(page, widths[len(widths) // 2])
    if read is None:
        return {"verdict": "SKIPPED", "score": 0.0,
                "why": "the page did not render", "findings": []}

    findings, parts = [], {}
    reqs = requirements(brief) if brief else []
    if reqs:
        miss = covers(reqs, read["items"])
        findings += miss
        parts["brief"] = (len(reqs) - len(miss)) / len(reqs)
        say(f"  brief    {len(reqs) - len(miss)}/{len(reqs)} "
            f"requirement(s) honoured")

    if reference:
        r = EYE.look(page, reference, widths=widths, verbose=False)
        if r.get("score") is not None:
            findings += r.get("findings", [])
            parts["match"] = r["score"]
            say(f"  target   {r['ok']}/{r['expected']} element(s) "
                f"correct across {len(widths)} width(s)")
        else:
            say(f"  target   UNMEASURED — {r.get('why', '')}")

    d = DES.look(page, widths=(widths[0], widths[-2] if len(widths) > 2
                               else widths[-1]), spec=spec, verbose=False,
                 taste=taste)
    if d["verdict"] != "SKIPPED":
        serious = [f for f in d["findings"]
                   if f["kind"] in ("LOW CONTRAST", "TAP TARGET",
                                    "NO TYPE SCALE", "FLAT HIERARCHY",
                                    "AI GRADIENT")]
        findings += d["findings"]
        # A DESIGN SCORE IS NOT A COUNT. One unreadable heading matters
        # more than a dozen gaps two pixels off a grid nobody measures.
        parts["design"] = 1.0 if not serious else max(
            0.0, 1.0 - 0.2 * len(serious))
        say(f"  design   {len(d['findings'])} finding(s), "
            f"{len(serious)} serious")

    spills = 0
    for w in widths:
        rr = EYE.read_page(page, w)
        if rr is None:
            continue
        s = sum(1 for it in rr["items"]
                if it["x"] + it["w"] > rr["vw"] + 2)
        if s:
            spills += s
            findings.append({
                "kind": "SPILLS", "text": "", "selector": None,
                "tag": "layout", "want": 0, "got": s,
                "fix": f"{s} element(s) hang off the right edge at "
                       f"{w}px — the width where agent-written UI "
                       f"overwhelmingly breaks",
            })
    parts["flow"] = 1.0 if not spills else 0.0
    say(f"  flow     " + (f"{spills} spill(s)" if spills
                          else "nothing spills at any width"))

    score = sum(parts.values()) / max(1, len(parts))
    hard = [f for f in findings
            if f["kind"] in ("MISSING FROM BRIEF", "WRONG COUNT",
                             "LOW CONTRAST", "TAP TARGET", "SPILLS",
                             "BROKEN IMAGE", "MISSING",
                             "FLAT HIERARCHY", "AI GRADIENT")]
    verdict = "ACCEPTED" if not hard and score >= 0.9 else "REFUSED"
    return {"verdict": verdict, "score": score, "parts": parts,
            "findings": findings, "blocking": hard,
            "why": (f"{', '.join(f'{k} {v * 100:.0f}%' for k, v in parts.items())}"
                    f" · {len(hard)} blocking finding(s)")}


def _entry(project):
    p = Path(project)
    if p.is_file():
        return p
    for c in ("index.html", "dist/index.html", "out/index.html",
              "build/index.html", "public/index.html"):
        if (p / c).is_file():
            return p / c
    return None


# ──────────────────────────────── the loop ───────────────────────────

def session(project, writer, brief="", reference=None, rounds=5,
            widths=None, spec=None, verbose=True):
    """Write, check, feed back the repairs, keep the best. Refuse or ship.

    `writer(prompt, project, round)` is whatever produces code — a model
    driven through `aethron_code`, a deterministic emitter, or a test's
    mock. The loop does not care, and that is deliberate: the contract
    is enforced on the OUTPUT, so it holds for any writer, including one
    that is having a bad day.

    THE PROJECT ON DISK IS THE BEST ONE SEEN. Not the last one tried —
    a loop that leaves its final attempt in place hands over a
    regression whenever the last round happened to be the worst, which
    is roughly half the time if the writer is a model.
    """
    def say(*a):
        if verbose:
            print(*a)

    project = Path(project)
    best, best_snap, trail = None, None, []
    for i in range(rounds + 1):
        if i == 0:
            prompt = _first_prompt(brief, reference)
        else:
            if not best or not best["findings"]:
                break
            prompt = _repair_prompt(best, brief)
        try:
            writer(prompt, project, i)
        except Exception as e:
            say(f"  round {i}  the writer failed: {e}")
            break
        r = check(project, brief, reference, widths, spec, verbose=False)
        trail.append(round(r["score"], 4))
        keep = best is None or r["score"] > best["score"] + 1e-6
        say(f"  round {i}  {r['verdict']:<8} {r['score'] * 100:5.1f}%  "
            f"{len(r.get('blocking', []))} blocking  "
            + ("kept" if keep else "worse — reverting to the best so far"))
        if keep:
            best = r
            best_snap = _snapshot(project)
        else:
            _restore(project, best_snap)
        if best["verdict"] == "ACCEPTED":
            break
    if best_snap:
        _restore(project, best_snap)
    if best is None:
        return {"verdict": "REFUSED", "score": 0.0, "trail": trail,
                "why": "no round produced a page that could be checked",
                "findings": []}
    return {**best, "trail": trail}


def _snapshot(project):
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ae-build-snap-"))
    dst = d / "p"
    p = Path(project)
    if p.is_file():
        dst.mkdir(parents=True)
        shutil.copy2(p, dst / p.name)
    else:
        shutil.copytree(p, dst)
    return dst


def _restore(project, snap):
    if not snap:
        return
    p = Path(project)
    if p.is_file():
        src = snap / p.name
        if src.is_file():
            shutil.copy2(src, p)
        return
    if p.exists():
        shutil.rmtree(p)
    shutil.copytree(snap, p)


def _first_prompt(brief, reference):
    lines = ["Build this as a real, working page.", ""]
    if brief:
        lines += ["WHAT WAS ASKED FOR:", brief, ""]
    if reference:
        lines += [f"TARGET TO MATCH: {reference}", ""]
    reqs = requirements(brief) if brief else []
    if reqs:
        lines.append("These will be CHECKED on the rendered page:")
        for r in reqs:
            if r["kind"] == "count":
                lines.append(f"  - at least {r['n']} repeated "
                             f"{r['value']}")
            else:
                lines.append(f'  - the text "{r["value"]}" appears')
        lines.append("")
    lines += [
        "It will also be measured for: WCAG AA contrast, a type scale of "
        "at most 7 sizes, a consistent spacing step, tap targets of at "
        "least 44px on a phone, and NOTHING hanging off the right edge "
        "at 390px. Those are measured, not reviewed.",
    ]
    if not reference:
        # THE FIRST LIVE PAGE OBEYED EVERY MEASURED RULE AND CAME OUT
        # TIMID: it echoed the checks back as CSS comments, read "at most
        # 7 sizes" as "use as few as possible", and set its headline at
        # 18px, the same size as its card titles. A limit is not a target,
        # and a page can be correct and generic at once.
        lines += [
            "",
            "DESIGN, not just correctness — these are also measured:",
            "  - A real typographic hierarchy: the headline must clearly "
            "dominate (at least 1.8x the body size, and larger than every "
            "section title). The size limit is a maximum, not a goal.",
            "  - No generic purple/indigo-to-blue gradient backgrounds.",
            "  - No em dashes in the copy.",
            "  - Commit to one specific visual direction for this brand "
            "rather than a neutral template: a deliberate palette, a type "
            "pairing with character, and spacing with rhythm.",
            "Do not describe the checks in comments; meet them.",
        ]
    return "\n".join(lines)


def _repair_prompt(report, brief):
    """The findings, as instructions. Blocking ones first.

    Each names a selector IN THE BUILT PAGE, which is the whole reason
    this is usable: the previous generation of this report named
    coordinates in an image the writer had never seen, and three
    separate correction strategies driven by it all scored WORSE than
    leaving the page alone.
    """
    lines = [f"The build was REFUSED: {report['why']}.",
             "Fix these, highest priority first. Each names a selector "
             "in the page you wrote.", ""]
    rank = {"MISSING FROM BRIEF": 0, "WRONG COUNT": 1, "SPILLS": 2,
            "LOW CONTRAST": 3, "BROKEN IMAGE": 4, "MISSING": 5,
            "TAP TARGET": 6, "FLAT HIERARCHY": 7, "AI GRADIENT": 8,
            "MISPLACED": 9, "WRONG SIZE": 10}
    for f in sorted(report["findings"],
                    key=lambda f: rank.get(f["kind"], 99))[:20]:
        who = f.get("selector") or "(not on the page)"
        lines.append(f"- {f['kind']}: {f.get('fix', '')}")
        if f.get("selector"):
            lines.append(f"    at: {who}")
    lines += ["", "Change only what is listed. Everything else measured "
              "correctly and will be re-checked."]
    return "\n".join(lines)


# ─────────────────────────────── writers ─────────────────────────────

def model_writer(provider=None, model=None, timeout=900):
    """A writer backed by whatever model the brain is configured with.

    Uses the same seam as the healer: any provider, through the bridge
    when it does not speak Anthropic. The loop does not trust it — the
    checks decide — which is exactly what makes a cheap model usable
    here.
    """
    def write(prompt, project, rnd):
        import aethron_code
        cfg = {}
        if provider:
            cfg["provider"] = provider
        if model:
            cfg["model"] = model
        s = aethron_code.CodeSession(workspace=str(Path(project).parent
                                                   if Path(project).is_file()
                                                   else project),
                                     cfg=cfg)
        try:
            s.send(prompt)
            for _ in s.events(timeout=timeout):
                pass
        finally:
            try:
                s.close()
            except Exception:
                pass
    return write


# PRICES CHECKED 2026-09-14 (Gemini 3.6 Flash, introductory rate to
# 2026-12-31). Thinking tokens bill as OUTPUT, which is the one cost a
# token count of the prompt cannot see.
GEMINI_IN = 0.75 / 1e6
GEMINI_OUT = 3.75 / 1e6


class BudgetExhausted(RuntimeError):
    """Refused to START a call that could exceed the cap."""


def _active(model=None):
    """The ACTIVE saved provider, as one call needs it. These two writers
    used to post to Google's address with a Gemini model name written in,
    whatever the person had chosen — switching to DeepSeek changed the
    settings and nothing these called."""
    import aethron_brain as _B
    r = _B.resolve()
    if model:
        r = {**r, "model": model}
    return r


def _price(r):
    """(in, out) dollars per token for the active model. A local model is
    free; an unknown one is priced at the dearest known rate, so a cap is
    never turned off by a model name nobody listed."""
    if r.get("local"):
        return 0.0, 0.0
    import aethron_bridge as _br
    pin, pout = _br.price_of(r.get("model") or "")
    return pin / 1e6, pout / 1e6


def _content(r, text, images):
    """The user turn: text alone, or the pictures FIRST and then the text,
    in the wire's own shape. `images` is a list of (mime, bytes)."""
    if not images:
        return text
    import base64 as _b
    parts = []
    for mime, data in images:
        b64 = _b.b64encode(data).decode()
        if r["wire"] == "anthropic":
            parts.append({"type": "image", "source": {
                "type": "base64", "media_type": mime, "data": b64}})
        else:
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{b64}", "detail": "high"}})
    parts.append({"type": "text", "text": text})
    return parts


def _request(r, key, text, max_tokens, temperature, images=None):
    """One request in the provider's own wire format."""
    import json as _j
    import urllib.request as _u
    if r["wire"] == "anthropic-cli":
        raise BudgetExhausted(
            f"{r['label']} has no API to call directly — it signs in "
            "through the Claude Code app, which only the coding agent uses. "
            "Switch to a provider with an API key for this.")
    if r["wire"] == "anthropic":
        body = {"model": r["model"], "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [{"role": "user",
                              "content": _content(r, text, images)}]}
        return _u.Request(r["base"] + "/v1/messages",
                          data=_j.dumps(body).encode(),
                          headers={"Content-Type": "application/json",
                                   "x-api-key": key,
                                   "anthropic-version": "2023-06-01"})
    body = {"model": r["model"], "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user",
                          "content": _content(r, text, images)}]}
    # Only where it is accepted: an unknown field is a 400 on some. OpenAI
    # takes it ONLY on reasoning models — sent to gpt-4o-mini it is a 400
    # ("Unrecognized request argument"), which is how every change and edit
    # failed on it from 2026-10-06 — and those models also refuse
    # max_tokens and any temperature but the default.
    import aethron_bridge as _br
    if r.get("provider") == "gemini":
        body["reasoning_effort"] = "low"
    elif r.get("provider") == "openai" and _br.openai_reasoning(r["model"]):
        body["reasoning_effort"] = "low"
        body["max_completion_tokens"] = body.pop("max_tokens")
        body.pop("temperature", None)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return _u.Request(r["base"] + "/chat/completions",
                      data=_j.dumps(body).encode(), headers=headers)


def _reply(r, d):
    """-> (text, cut_off, tokens_in, tokens_out) from either wire."""
    if r["wire"] == "anthropic":
        u = d.get("usage") or {}
        text = "".join(b.get("text", "") for b in d.get("content") or []
                       if b.get("type") == "text")
        return (text, d.get("stop_reason") == "max_tokens",
                int(u.get("input_tokens") or 0),
                int(u.get("output_tokens") or 0))
    u = d.get("usage") or {}
    p_in = int(u.get("prompt_tokens") or 0)
    # total - prompt, NOT completion_tokens: on thinking models the
    # completion count can omit the reasoning that was billed.
    p_out = int((u.get("total_tokens") or 0) - p_in) or \
        int(u.get("completion_tokens") or 0)
    choice = d["choices"][0]
    return ((choice.get("message") or {}).get("content") or "",
            choice.get("finish_reason") == "length", p_in, p_out)


def _limit_kind(r, code, body):
    """('wall'|'pace', seconds) for a refusal. Google's quota ids say which;
    elsewhere an exhausted balance (402, "insufficient", "depleted") is a
    wall and any other 429 is a pause."""
    low = body.lower()
    if r.get("provider") == "gemini":
        return _gemini_429(body)
    if code == 402 or "insufficient" in low or "depleted" in low \
            or "balance" in low:
        return "wall", 0
    return "pace", 15


def gemini_writer(budget_usd=0.25, model=None,
                  max_tokens=12000, key=None, verbose=True):
    """One direct call per round, with a spend cap that cannot be crossed.

    WHY NOT THE AGENT PATH. `model_writer` drives the CLI, which resends
    its whole system prompt and every tool definition on every request,
    several requests per round. That is the right tool for a large
    codebase and the wrong one for $0.38. A page is one file: one call
    writes it.

    THE CAP IS CHECKED BEFORE THE CALL, NOT AFTER. Adding up the bill
    once a response arrives tells you that you overspent; it does not
    stop you. Before each call the writer asks whether the WORST CASE —
    the prompt plus every token `max_tokens` allows, thinking included —
    would cross the budget, and if it could, the call is never made.
    Spend is then taken from the usage the API actually reports, so the
    ledger is a measurement rather than an estimate.
    """
    import json as _j
    import time as _t
    import urllib.request as _u
    import urllib.error as _ue
    # the ACTIVE saved provider — the name is historical
    r = _active(model)
    if key is None:
        key = r.get("key") or ""
    if not key and not r.get("local") and r["wire"] != "anthropic-cli":
        raise BudgetExhausted(f"no API key saved for {r['label']}")
    PIN, POUT = _price(r)
    ledger = {"calls": 0, "in": 0, "out": 0, "usd": 0.0,
              "budget": budget_usd, "stopped": None}

    def say(*a):
        if verbose:
            print(*a)

    def write(prompt, project, rnd):
        project = Path(project)
        project.mkdir(parents=True, exist_ok=True)
        page = project / "index.html"
        text = prompt
        if rnd > 0 and page.is_file():
            text += "\n\nTHE CURRENT PAGE (return it complete, fixed):\n" \
                    + page.read_text()
        text += ("\n\nReturn ONLY one complete, self-contained index.html "
                 "(inline CSS, no external assets, no markdown fences, "
                 "no commentary).")
        est_in = len(text) // 3 + 50          # generous: ~3 chars/token
        worst = est_in * PIN + max_tokens * POUT
        if ledger["usd"] + worst > budget_usd:
            ledger["stopped"] = (f"cap: ${ledger['usd']:.4f} spent, next "
                                 f"call could cost up to ${worst:.4f}, "
                                 f"budget ${budget_usd:.2f}")
            raise BudgetExhausted(ledger["stopped"])
        req = _request(r, key, text, max_tokens, 0.3)
        d = None
        for attempt in range(4):
            try:
                with _u.urlopen(req, timeout=300) as resp:
                    d = _j.loads(resp.read())
                break
            except _ue.HTTPError as e:
                msg = e.read()[:400].decode(errors="replace")
                # "credits depleted" also arrives as a 429, and retrying
                # it is how the last run spent four rounds of back-off
                # waiting on a balance that was not coming back.
                if _limit_kind(r, e.code, msg)[0] == "wall" and (
                        r.get("provider") != "gemini"
                        or "depleted" in msg.lower()):
                    ledger["stopped"] = "provider: credits depleted"
                    raise BudgetExhausted(ledger["stopped"])
                if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f"provider said {e.code}: {msg}")
                _t.sleep(6 * (attempt + 1))
        html, cut, p_in, p_out = _reply(r, d)
        cost = p_in * PIN + p_out * POUT
        ledger["calls"] += 1
        ledger["in"] += p_in
        ledger["out"] += p_out
        ledger["usd"] += cost
        say(f"    call {ledger['calls']}: {p_in} in / {p_out} out "
            f"= ${cost:.4f}  (total ${ledger['usd']:.4f} of "
            f"${budget_usd:.2f})")
        if cut:
            raise RuntimeError("the model ran out of tokens mid-page — "
                               "a truncated page is not written")
        import aethron_generate as _G
        html = _G._clean(html)
        if "<" not in html:
            raise RuntimeError("the reply was not a page")
        page.write_text(html)

    write.ledger = ledger
    return write


def _gemini_429(body):
    """-> ('pace', seconds) when waiting on the same key helps, ('wall', 0) when it will not today.

    Google sends the same words for both — "You exceeded your current quota" — and the
    difference is only in the quota id: ...PerMinute... clears in seconds, ...PerDay... (measured:
    GenerateRequestsPerDayPerProjectPerModel-FreeTier, quotaValue 20) does not clear today.
    """
    import re as _re
    if "PerMinute" in body:
        m = _re.search(r'"retryDelay":\s*"(\d+)', body)
        return "pace", min(60, (int(m.group(1)) + 1) if m else 20)
    if "PerDay" in body or "depleted" in body.lower():
        return "wall", 0
    import aethron_bridge as _br
    return ("wall", 0) if _br.QUOTA_WALL_RE.search(body) else ("pace", 15)


def gemini_text(prompt, ledger, budget_usd, model=None, max_tokens=4000,
                key=None, temperature=0.2, timeout=180, images=None):
    """One call that returns TEXT, through the KEY RING — free keys first, the paid key last.

    MEASURED 2026-09-15: this path took the ring's first key and read Google's free-tier quota
    message ("...check your plan and billing details") as "credits depleted", because it
    contains the word billing. One spent free key stopped every request while two free keys
    that answer in two seconds sat unused. Now a key whose DAILY free allowance is spent is
    marked for today and the next key is tried; a per-minute limit is waited out on the same
    key; a key that does not answer is parked; the paid key is reached only after that.

    THE CAP STILL GUARDS REAL MONEY: before a paid call the worst case — the prompt plus every
    token max_tokens allows — is priced, and the call is never started if it could cross the
    budget. A free call costs nothing, so it adds nothing to `usd`; its list price is kept in
    `list_usd`, so no report can mistake it for money charged.
    """
    import json as _j
    import time as _t
    import urllib.request as _u
    import urllib.error as _ue
    import aethron_brain as _B
    for k, v in (("calls", 0), ("in", 0), ("out", 0), ("usd", 0.0), ("list_usd", 0.0), ("free_calls", 0)):
        ledger.setdefault(k, v)
    # THE ACTIVE SAVED PROVIDER, not Google's address. The name is
    # historical; what it calls is whatever the person has switched to.
    r = _active(model)
    model = r["model"]
    PIN, POUT = _price(r)
    if r.get("local") and not key:
        key = "local"           # a local model takes no key; one pass, free
        ring = [(key, "given")]
    else:
        spent = _B.spent_for(model)
        ring = [(k, "free") for k in _B.free_ring() if _B._fp(k) not in spent and not _B._is_cold(k)]
        paid = _B.paid_key()
        if paid and _B._fp(paid) not in spent and paid not in [k for k, _ in ring]:
            ring.append((paid, "paid"))
    if not ring:
        ledger["stopped"] = ("no key can serve this today: every free allowance is spent and there is no "
                             "usable paid key")
        raise BudgetExhausted(ledger["stopped"])
    # A picture is priced as a large one (~1,600 tokens) so the cap holds.
    est_in = len(prompt) // 3 + 50 + 1600 * len(images or ())
    tried = []
    for k, tier in ring:
        fp = _B._fp(k)
        if tier != "free":
            worst = est_in * PIN + max_tokens * POUT
            if ledger["usd"] + worst > budget_usd:
                tried.append(f"{tier} key {fp}: not tried — its next call could cost up to ${worst:.4f}, "
                             f"over the ${budget_usd:.2f} budget with ${ledger['usd']:.4f} spent")
                continue
            # AND THE WALLET, which the per-run budget cannot see: twenty runs of $0.20 empty a
            # card exactly as fast as one run of $4, and nothing in a per-run cap notices.
            left = _B.wallet()["left_usd"]
            if worst > left:
                tried.append(f"{tier} key {fp}: not tried — its next call could cost up to ${worst:.4f} "
                             f"and the wallet has ${left:.4f} left (raise it with the wallet "
                             f"button in the Design view)")
                continue
        req = _request(r, "" if k == "local" else k, prompt, max_tokens, temperature, images)
        d, waited = None, 0
        for attempt in range(5):
            try:
                with _u.urlopen(req, timeout=timeout) as resp:
                    d = _j.loads(resp.read())
                break
            except _ue.HTTPError as e:
                msg = e.read()[:2000].decode(errors="replace")
                if e.code in (402, 429) or "depleted" in msg.lower():
                    kind, pause = _limit_kind(r, e.code, msg)
                    if kind == "wall":
                        if tier != "given":
                            _B.mark_exhausted(k, model)
                        tried.append(f"{tier} key {fp}: " + ("prepaid credit is depleted" if "depleted" in msg.lower()
                                                            else "today's free allowance is spent"))
                        break
                    if attempt < 4 and waited + pause <= 150:
                        waited += pause
                        _t.sleep(pause)
                        continue
                    tried.append(f"{tier} key {fp}: still rate-limited after waiting {waited}s")
                    break
                if e.code in (500, 502, 503, 504) and attempt < 3:
                    _t.sleep(6 * (attempt + 1))
                    continue
                # A MODEL WITH NO EYES refuses the picture, not the job: say
                # so in the prompt and ask once more with the words alone.
                if images and e.code == 400 and re.search(
                        r"image|vision|content.?type|multimodal", msg, re.I):
                    images = None
                    prompt += ("\n\n(The picture could not be sent: this model "
                               "does not take image input. Work from the "
                               "measurements above.)")
                    req = _request(r, "" if k == "local" else k, prompt,
                                   max_tokens, temperature)
                    ledger["no_images"] = True
                    continue
                tried.append(f"{tier} key {fp}: provider said {e.code}: {msg[:200]}")
                break
            except (_ue.URLError, TimeoutError, ConnectionError, OSError) as e:
                # A key that accepts the connection and never answers is parked, not retried: the
                # next key takes the request instead of the caller waiting out the same timeout.
                if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
                    if tier != "given":
                        _B.mark_cold(k)
                    tried.append(f"{tier} key {fp}: did not answer within {timeout}s")
                    break
                # A CONNECTION THAT DROPS IS NOT AN ANSWER: a request that never reached the
                # provider is safe to send again.
                if attempt >= 3:
                    tried.append(f"{tier} key {fp}: could not reach the provider: {e}")
                    break
                _t.sleep(4 * (attempt + 1))
        if d is None:
            continue
        text, cut, p_in, p_out = _reply(r, d)
        cost = p_in * PIN + p_out * POUT
        ledger["calls"] += 1
        ledger["in"] += p_in
        ledger["out"] += p_out
        ledger["list_usd"] += cost
        if tier == "free":
            ledger["free_calls"] += 1
        else:
            ledger["usd"] += cost
            st = _B.wallet_spend(cost)      # once: a second call would count the same money twice
            ledger["wallet_left"] = max(0.0, st["limit_usd"] - st["spent_usd"])
        ledger["last_key"] = f"{tier} {fp}"
        if cut:
            raise RuntimeError("the model ran out of tokens mid-reply — a cut-off plan is not applied")
        return text
    ledger["stopped"] = "no key could serve this request — " + "; ".join(tried)
    raise BudgetExhausted(ledger["stopped"])

def main(argv):
    if not argv or {"-h", "--help"} & set(argv):
        print(__doc__.split("\n\n")[0])
        print("\nusage: aethron_build.py <project> --brief '...' "
              "[--reference <url|png>] [--rounds 5] [--check-only]")
        return 0
    project = argv[0]
    brief = ""
    if "--brief" in argv:
        brief = argv[argv.index("--brief") + 1]
    reference = None
    if "--reference" in argv:
        reference = argv[argv.index("--reference") + 1]
    if "--check-only" in argv:
        r = check(project, brief, reference)
        print()
        print(f"{r['verdict']} — {r['why']}")
        print(EYE.brief(r, limit=30) if r.get("findings") else "")
        return 0 if r["verdict"] == "ACCEPTED" else 1
    rounds = int(argv[argv.index("--rounds") + 1]) if "--rounds" in argv \
        else 5
    r = session(project, model_writer(), brief, reference, rounds=rounds)
    print()
    print(f"{r['verdict']} — {r['why']}")
    print("trajectory: " + " -> ".join(f"{t * 100:.1f}%"
                                       for t in r.get("trail", [])))
    return 0 if r["verdict"] == "ACCEPTED" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
