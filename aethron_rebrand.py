#!/usr/bin/env python3
"""Rebrand a whole site from a few lines about the owner's business.

WHY THIS EXISTS

Aethron could already replace a brand token everywhere and report "no
leftover 'Makro'". That is a relabel: the name changes and the site still
sells someone else's product. Measured on a real migration — 0 brand
mentions, 72% of the copy still word-for-word the template's, and the
owner saw it on the page in seconds.

Doing it properly by hand took a day: writing 281 fills, mapping the
template's domain onto the owner's, fitting byte-locked CMS slots, and
finding the strings inventory never harvested. This does that loop
without a person:

    BRIEF   a few lines -> a structured brand brief, including the
            vocabulary map that decides what every domain noun becomes
    FILL    every string, batched, with its byte budget in front of the
            model. Coverage is the target; brand tokens are not enough
    BUILD   through the existing guarded engine, which refuses anything
            over a lock or carrying a backtick
    AUDIT   compare the built pages against pristine, word for word, and
            list what still reads as the template's
    LOOP    re-fill only what the audit flagged, until coverage stops
            improving — then say plainly what it could not do

Nothing here decides it succeeded. The audit is a measurement, the byte
budgets are physics, and verify still has the last word.

    python3 forge.py rebrand                    (uses project_plan.md)
    python3 forge.py rebrand --plan="Jomiez, design ownership tools"
    python3 forge.py rebrand --audit            (measure only, no model)
    python3 forge.py rebrand --rounds=3
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# NOT COPY — never offer these to a model.
#
# Inventory harvests every string it can reach, and some of what it reaches
# is machinery: React/Framer runtime warnings, physics-library messages
# carried in the chunks, snake_case meta values, bare numbers and prices.
# Asking a model to rebrand `summary_large_image` or `95%` costs tokens at
# best and corrupts a meta tag or a statistic at worst.
#
# CALIBRATED, NOT GUESSED. Two candidate patterns that looked obviously
# right — "contains a brace or semicolon" and "looks like a CSS block" —
# were measured against the 288 strings a real migration filled CORRECTLY
# and rejected 5 and 4 of them: Framer's CMS rich-text envelopes
# (`[1,[4,"p",null,[5,"...`) are punctuation-dense and carry real prose.
# Every pattern below rejects ZERO of those 288 and catches 35 of the 384
# machine strings. Re-measure before adding to it.
NOT_COPY = re.compile(
    r'(?:^(?:Warning:|Error:|Minified React|\w+\.\w+ (?:takes|expects|must)))'
    r'|framer-text'
    r'|\b(?:Bodies|Composites|Svg|Runner|Engine|ReactDOM)\.'
    r'|forceFrameRate|pushState|importFonts|counter-increment|non-minified'
    r'|dev environment|maxBatchSize'
    r'|\n'
    r'|^[^A-Za-z]+$'                       # '95%', '$550', '+$', '.00'
    r'|^[a-z0-9]+(?:_[a-z0-9]+)+$')        # 'summary_large_image'

BRIEF_PROMPT = """The owner of a website is rebranding a template into \
their own business. They have written very little — often just a name, \
what they do, and an email. Turn it into a brief that another model can \
rebrand an ENTIRE website from.

OWNER'S NOTES:
%(plan)s

WHAT THE TEMPLATE CURRENTLY SELLS (sampled from its own copy):
%(sample)s

Reply with ONLY a JSON object:
{"brand": "<the company name exactly as it should be written>",
 "brand_short": "<the shortest form the company is genuinely known by — \
normally the first word of the name. Used only where a byte-locked slot \
cannot hold the full name. Never an invented acronym>",
 "one_liner": "<what they do, one sentence, their voice>",
 "positioning": "<2-3 sentences a visitor should come away believing>",
 "audience": "<who the site talks to>",
 "tone": "<3-5 adjectives>",
 "product_names": ["<any product/feature names to use>"],
 "contact": {"email": "", "socials": ""},
 "vocabulary": {"<template's domain noun>": "<the owner's equivalent>"},
 "avoid": ["<words from the template's industry that must not survive>"]}

The vocabulary map is the important part: list EVERY recurring noun of \
the template's industry and give the owner's equivalent, so the whole \
site stays coherent instead of half-translated. If the owner's notes do \
not say something, infer it sensibly from the name and what they do — an \
invented plausible detail is better than leaving the template's."""


def _sample_template(cm, n=28):
    """A sample of the template's own copy, longest first — that is where
    the industry actually shows."""
    olds = [e["old"] for e in cm.get("strings", []) if len(e.get("old", "")) > 25]
    olds.sort(key=len, reverse=True)
    return "\n".join("- " + o[:140] for o in olds[:n])


def brief_from_url(url, cfg=None, pages=6):
    """Learn the owner's brand from their EXISTING website.

    Most owners have one already, and it says everything a brief needs in
    their own words — what they do, who they serve, what they call their
    products, how they write. Asking them to retype it into a plan file
    is asking them to do the model's job.

    Reads the live pages, strips markup, and hands the visible copy to
    the brief expander. Text only: layout and assets belong to the
    template being rebranded, not to the source of the words.
    """
    import forge
    import urllib.request

    def fetch(u):
        req = urllib.request.Request(u, headers={
            "User-Agent": "Mozilla/5.0 (Aethron brief reader)"})
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.read().decode("utf8", "replace")

    seen, texts = set(), []
    try:
        home = fetch(url)
    except Exception as e:
        raise ValueError(f"could not read {url}: {e}")
    texts.append(forge._visible_text(home))
    base = url.rstrip("/")
    for href in re.findall(r'href="([^"#?]+)"', home):
        if len(texts) >= pages:
            break
        if href.startswith("http") and base.split("//")[-1].split("/")[0] \
                not in href:
            continue                       # someone else's site
        link = href if href.startswith("http") else \
            base + "/" + href.lstrip("./")
        if link in seen or link.rstrip("/") == base:
            continue
        seen.add(link)
        try:
            body = forge._visible_text(fetch(link))
            if len(body.split()) > 40:
                texts.append(body)
        except Exception:
            continue
    joined = re.sub(r"\s+", " ", " ".join(texts))[:14000]
    if len(joined.split()) < 60:
        raise ValueError(f"{url} gave too little copy to learn a brand from "
                         f"({len(joined.split())} words) — write a short "
                         f"project_plan.md instead")
    print(f"  read {len(texts)} page(s), {len(joined.split())} words of copy")
    return joined


def make_brief(plan_text, cm, cfg=None):
    import aethron_brain as brain
    raw = brain.text_call(
        BRIEF_PROMPT % {"plan": plan_text.strip(),
                        "sample": _sample_template(cm)}, cfg=cfg)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("the model did not return a brief")
    return _validate_brief(json.loads(m.group(0)))


def _validate_brief(b):
    """The short brand is load-bearing, so it is not left to the model.

    Byte-locked slots hold exactly as many bytes as the template's own
    brand. Measured on the Heath migration: the full name 'Heath Ceramics'
    (14 bytes) fits NONE of the 8 brand-bearing locked slots and 'Heath'
    (5 bytes) fits all of them exactly. So a short form is mandatory —
    but an invented one is worse than a truncated one, because 'HC' reads
    as a different company. Accept the model's answer only when it is a
    genuine prefix of the brand; otherwise take the first word.
    """
    brand = str(b.get("brand") or "").strip()
    short = str(b.get("brand_short") or "").strip()
    if not brand:
        return b
    if not short or not brand.lower().startswith(short.lower()):
        short = re.split(r"[\s\-–—:,]+", brand)[0]
    b["brand"], b["brand_short"] = brand, short
    return b


def brief_text(b):
    """The brief as the fill prompt should see it — prose, not JSON."""
    v = b.get("vocabulary") or {}
    lines = [
        f"BRAND: {b.get('brand','')}",
        f"WHAT THEY DO: {b.get('one_liner','')}",
    ]
    if b.get("brand_short") and b["brand_short"] != b.get("brand"):
        lines.append(f"SHORT BRAND (use ONLY where max_bytes cannot hold "
                     f"the full name): {b['brand_short']}")
    lines += [
        f"POSITIONING: {b.get('positioning','')}",
        f"AUDIENCE: {b.get('audience','')}",
        f"TONE: {b.get('tone','')}",
    ]
    if b.get("product_names"):
        lines.append("PRODUCT NAMES: " + ", ".join(b["product_names"]))
    c = b.get("contact") or {}
    if c.get("email"):
        lines.append(f"EMAIL: {c['email']}")
    if c.get("socials"):
        lines.append(f"SOCIALS: {c['socials']}")
    if v:
        lines.append("VOCABULARY MAP — translate these concepts wherever "
                     "they appear, and stay consistent:")
        lines += [f"  {k} -> {val}" for k, val in list(v.items())[:40]]
    if b.get("avoid"):
        lines.append("MUST NOT SURVIVE ANYWHERE: " + ", ".join(b["avoid"]))
    return "\n".join(lines)


def audit(root: Path):
    """What still reads as the template's? -> (fraction_same, offenders).

    Two signals, because either alone lies. Word-for-word sameness against
    pristine catches copy nobody touched; the brief's own `avoid` list
    catches copy that was touched and still says "invoice".
    """
    import forge
    cm = json.loads((root / "copy_map.json").read_text())
    brief = {}
    bp = root / "brand_brief.json"
    if bp.is_file():
        try:
            brief = json.loads(bp.read_text())
        except Exception:
            brief = {}
    avoid = [w.lower() for w in (brief.get("avoid") or []) if len(w) > 3]

    # THE TEMPLATE'S OWN WORDS ARE THE ONES THAT MUST NOT SURVIVE.
    #
    # `avoid` is written by the brief, which is built from the OWNER's
    # site — so it lists the owner's industry, not the template's. A fill
    # that rewrote half a sentence and left "Makro is a creative clarity
    # platform … understand cash" passed every check: it is filled, so it
    # is not "never filled", and "cash" is not in an art studio's avoid
    # list. The page still said Makro.
    #
    # The vocabulary map already names them: its KEYS are the template's
    # domain nouns, chosen precisely because they must be translated. Any
    # key surviving inside a FILLED value is a half-done rewrite. So is
    # the template's own brand, which the config records as forbidden.
    stale = [str(k).lower() for k in (brief.get("vocabulary") or {})
             if len(str(k)) > 3]
    try:
        cfg = json.loads((root / "forge.json").read_text())
        stale += [str(w).lower() for w in (cfg.get("forbidden_words") or [])
                  if len(str(w)) > 2]
    except Exception:
        pass

    # WORD BOUNDARIES. Plain `in` made 'data' match 'Last updated' and
    # 'security' match 'Privacy & Security', minting offenders that can
    # never be resolved — they then sit in the count forever and corrupt
    # the loop's "did this round help?" signal.
    def _wordset(words):
        ws = sorted({w for w in words if w}, key=len, reverse=True)
        if not ws:
            return None
        # Hyphen and underscore count as word characters here on purpose:
        # without them 'data' matched the CMS attribute 'data-preset-tag'
        # and minted four permanent offenders that no rewrite could ever
        # clear, which is exactly the noise that corrupts the loop's
        # stopping signal.
        return re.compile(r"(?<![a-z0-9_-])(?:%s)(?![a-z0-9_-])"
                          % "|".join(re.escape(w) for w in ws), re.I)

    avoid_rx, stale_rx = _wordset(avoid), _wordset(stale)

    offenders = []
    for i, e in enumerate(cm.get("strings", [])):
        cur = str(e.get("new") or e.get("old", ""))
        old = str(e.get("old", ""))
        # DECLINED TWICE IS AN ANSWER. Without this the widened gate
        # re-sends 'Home' and 'Blog' every round for ever: the model
        # rightly refuses, the offender count never falls, and the loop
        # burns a full pass to buy nothing.
        if int(e.get("_tries") or 0) >= 2 and not str(e.get("new", "")).strip():
            continue
        if len(cur.strip()) < 2 or NOT_COPY.search(old):
            continue
        why = None
        if not str(e.get("new", "")).strip():
            # THE LENGTH GATE WAS THE BUG. This branch used to require
            # len(old) > 25, so every short string was invisible to the
            # loop no matter how many rounds ran. Measured on the Heath
            # migration: 356 of 384 unfilled entries were never sent to
            # the model in ANY round, and all 31 of the leftovers an
            # owner could see on the page were <= 25 characters —
            # 'Makro Free', 'Who is Makro for?', 'Predict your income'.
            # Length was never evidence of anything; NOT_COPY is.
            why = "never filled"
        elif avoid_rx and avoid_rx.search(cur):
            why = "uses a word the brief said to avoid"
        elif str(e.get("new", "")).strip():
            # CARRIED OVER, NOT CHOSEN. A template word is only evidence
            # of a half-rewrite when it survived from the OLD text into
            # the new one. The same word chosen freshly is usually right:
            # "No subscriptions, no paywalls" is exactly what a free
            # painting app should say, and flagging it would teach the
            # owner to ignore this check — which is how the real
            # half-rewrite ("Makro is a creative clarity platform ...
            # understand cash", where 'cash' came straight from the
            # original) gets missed.
            if stale_rx:
                hit = stale_rx.search(cur)
                if hit and stale_rx.search(old):
                    why = (f"carried {hit.group(0)!r} over from the "
                           f"original — half-rewritten")
        if why:
            offenders.append({"i": i, "old": e.get("old", "")[:90],
                              "cur": cur[:90], "why": why,
                              "max_bytes": e.get("max_bytes")})
    depth = forge._rebrand_depth(root, root / "site")
    return (depth[0] if depth else None), offenders


def fill_round(root: Path, brief_str, entries, cfg=None, size=25):
    """One pass of batched filling. -> number of entries actually filled."""
    import studio
    cm_path = root / "copy_map.json"
    cm = json.loads(cm_path.read_text())
    strings = cm["strings"]
    idx = [o["i"] for o in entries]
    filled = 0
    for start in range(0, len(idx), size):
        chunk = idx[start:start + size]
        batch = {"strings": [
            {"old": strings[i]["old"], "new": "",
             **({"max_bytes": strings[i]["max_bytes"]}
                if strings[i].get("max_bytes") is not None else {})}
            for i in chunk]}
        prompt = studio.build_prompt(brief_str, batch)
        try:
            raw = studio.call_model({}, prompt) if False else None
        except Exception:
            raw = None
        if raw is None:
            import aethron_brain as brain
            raw = brain.text_call(prompt, cfg=cfg)
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            print(f"    batch {start // size + 1}: no JSON returned, skipped")
            continue
        try:
            got = json.loads(m.group(0)).get("strings", [])
        except ValueError:
            print(f"    batch {start // size + 1}: unparseable, skipped")
            continue
        # SAY WHY AN ENTRY WAS NOT FILLED. The first version printed only
        # a running total, so a batch that filled 0 of 25 looked identical
        # whether the model declined every string, the answers overflowed
        # their byte locks, or the reply came back misaligned. Without the
        # reason there is nothing to fix.
        why = {"empty": 0, "over_budget_degraded": 0, "backtick": 0,
               "same": 0}
        if len(got) != len(chunk):
            print(f"    batch {start // size + 1}: model returned "
                  f"{len(got)} of {len(chunk)} entries — alignment lost, "
                  f"matching by 'old' instead")
            by_old = {str(e.get("old", "")): e for e in got}
            got = [by_old.get(strings[i]["old"], {}) for i in chunk]
        for i, ent in zip(chunk, got):
            # ASKED COUNTS AS AN ATTEMPT, answered or not. audit() drops
            # an entry after two refusals so the widened gate cannot
            # re-offer 'Home' on every round for ever.
            strings[i]["_tries"] = int(strings[i].get("_tries") or 0) + 1
            new = str(ent.get("new") or "").strip()
            if not new:
                why["empty"] += 1
                continue
            if new == strings[i]["old"]:
                why["same"] += 1
                continue
            cap = strings[i].get("max_bytes")
            if cap is not None and int(cap) > 0 \
                    and len(new.encode()) > int(cap):
                # DISCARDING LEFT THE STRING 100% THE TEMPLATE'S — which
                # is strictly worse than what the build engine already
                # does with an over-slot fill. forge._pairs_from_map
                # degrades it honestly: the text layers take the new copy
                # (staying identical to each other, so hydration holds),
                # the CMS blob keeps its bytes, build prints a NOTE and
                # verify names the leftover. The comment that used to sit
                # here ("the lock is not negotiable") predated that.
                strings[i]["new"] = new
                why["over_budget_degraded"] += 1
                filled += 1
                continue
            if "`" in new or "${" in new:
                why["backtick"] += 1
                continue
            strings[i]["new"] = new
            filled += 1
        skipped = ", ".join(f"{k}={v}" for k, v in why.items() if v)
        print(f"    batch {start // size + 1}/{(len(idx) + size - 1) // size}: "
              f"{filled} filled so far" + (f"   (skipped: {skipped})"
                                           if skipped else ""))
    cm_path.write_text(json.dumps(cm, indent=1, ensure_ascii=False))
    return filled


def brand_sweep(root: Path, brief):
    """Mechanically retire the template's brand from anything left over.

    A brand swap is the one part of a rebrand that needs no judgement:
    'Makro Free' becomes 'Heath Ceramics Free' by substitution, and asking
    a model to decide it only adds a way to fail. Measured on the Heath
    run, the model was never even asked about most of them — but it also
    declined the bare token 'Makro' when it was asked, and one declined
    token is enough to leave the brand on the page.

    So this runs AFTER the model rounds, as a backstop over whatever is
    left: the model gets first refusal because it writes better copy
    ('Who is Makro for?' deserves a real sentence), and this guarantees
    the brand is gone either way. -> number of entries swept.
    """
    cm_path = root / "copy_map.json"
    cm = json.loads(cm_path.read_text())
    try:
        cfg = json.loads((root / "forge.json").read_text())
    except Exception:
        cfg = {}
    tokens = [str(w) for w in (cfg.get("forbidden_words") or []) if len(str(w)) > 2]
    brand = str(brief.get("brand") or "").strip()
    short = str(brief.get("brand_short") or brand).strip()
    if not tokens or not brand:
        return 0
    rx = re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])"
                    % "|".join(re.escape(t) for t in
                               sorted(tokens, key=len, reverse=True)), re.I)

    def replace(old, name):
        # SLUGS AND ADDRESSES ARE NOT PROSE. 'help@makro.ai' must not
        # become 'help@Heath Ceramics.ai', and 'who-is-makro-for' must
        # stay a slug — so in those the brand collapses to one lowercase
        # word. Everywhere else the case of the token is preserved.
        sluggy = ("@" in old or "/" in old
                  or re.fullmatch(r"[a-z0-9][a-z0-9\-._]*", old or ""))
        def one(m):
            hit = m.group(0)
            rep = re.split(r"\s+", name)[0].lower() if sluggy else name
            if not sluggy:
                if hit.isupper():
                    return rep.upper()
                if hit.islower():
                    return rep.lower()
            return rep
        return rx.sub(one, old)

    swept = 0
    for e in cm.get("strings", []):
        cur = str(e.get("new") or "")
        src = cur if cur else str(e.get("old", ""))
        if not rx.search(src) or NOT_COPY.search(str(e.get("old", ""))):
            continue
        cap = e.get("max_bytes")
        for name in (brand, short):
            cand = replace(src, name)
            if cand == src:
                break
            if cap is None or len(cand.encode()) <= int(cap):
                e["new"] = cand
                swept += 1
                break
        else:
            # Neither form fits the lock. Take the full name anyway and
            # let the engine degrade it to the text layers with a NOTE —
            # a brand visible only inside a CMS blob beats one on screen.
            cand = replace(src, brand)
            if cand != src:
                e["new"] = cand
                swept += 1
    if swept:
        cm_path.write_text(json.dumps(cm, indent=1, ensure_ascii=False))
    return swept


def cmd_rebrand(args):
    """The whole loop: brief -> fill -> build -> audit -> repeat."""
    import subprocess
    root = Path.cwd()
    if not (root / "copy_map.json").is_file():
        print("no copy_map.json — run inventory first")
        return 1
    audit_only = "--audit" in args
    rounds = int(next((a.split("=", 1)[1] for a in args
                       if a.startswith("--rounds=")), "2"))
    plan_arg = next((a.split("=", 1)[1] for a in args
                     if a.startswith("--plan=")), None)

    cm = json.loads((root / "copy_map.json").read_text())

    if audit_only:
        same, offenders = audit(root)
        _report(same, offenders)
        return 0

    from_url = next((a.split("=", 1)[1] for a in args
                     if a.startswith("--from=")), None)
    plan = plan_arg or ""
    if from_url and not plan:
        print(f"brief: learning the brand from {from_url}")
        plan = brief_from_url(from_url)
    if not plan:
        p = root / "project_plan.md"
        plan = p.read_text() if p.is_file() else ""
    # A BRIEF ALREADY ON DISK IS A PLAN. Demanding the notes again made
    # a second round impossible: the first run writes brand_brief.json,
    # and the next invocation refused with "no plan" while the brief sat
    # beside it. Resuming is the normal case, not the exception.
    bp = root / "brand_brief.json"
    if not plan.strip() and not bp.is_file():
        print("no plan — pass --plan=\"Name, what you do, email\", "
              "--from=<your website>, or write project_plan.md")
        return 1

    if bp.is_file():
        # Validate on the way in, not just at creation: briefs written by
        # an older version have no short brand, and without one every
        # byte-locked slot falls back to the full name and degrades.
        brief = _validate_brief(json.loads(bp.read_text()))
        bp.write_text(json.dumps(brief, indent=1, ensure_ascii=False))
        print(f"brief: reusing {bp.name} for {brief.get('brand','?')!r} "
              f"(short: {brief.get('brand_short','?')})")
    else:
        print("brief: expanding the owner's notes…")
        brief = make_brief(plan, cm)
        bp.write_text(json.dumps(brief, indent=1, ensure_ascii=False))
        print(f"  brand={brief.get('brand')!r}  "
              f"vocabulary={len(brief.get('vocabulary') or {})} mappings  "
              f"avoid={len(brief.get('avoid') or [])} words")
    bstr = brief_text(brief)

    last = first = last_depth = None
    for rnd in range(1, rounds + 1):
        same, offenders = audit(root)
        _report(same, offenders, prefix=f"round {rnd} before: ")
        if not offenders:
            break
        # Stop when a round buys nothing: another identical pass is just
        # spend. Saying so is more useful than looping to the cap.
        # THE OFFENDER COUNT IS NOT THE MEASURE — DEPTH IS.
        #
        # Measured on the fathom migration: offenders fell 570 -> 161 -> 1
        # while the copy stayed 53% the template's the whole way. The queue
        # drained because the attempt ledger retires entries a model has
        # declined twice, not because anything was rewritten, so the rule
        # below saw a delta of 160 and happily paid for another round that
        # moved the page by nothing.
        #
        # Depth cannot be faked that way: it compares the built pages
        # against pristine. Note this can only ever stop the SECOND
        # unproductive round — a round's futility is not visible until the
        # audit that follows it — so it is a brake, not a cure.
        if (last_depth is not None and same is not None
                and last_depth - same < 0.01):
            print(f"  the last round changed {(last_depth - same) * 100:.1f}% "
                  f"of the copy — stopping: the queue is draining but the "
                  f"page is not changing")
            break
        last_depth = same

        # A MINIMUM DELTA, NOT MERE IMPROVEMENT. With the corrected gate
        # the offender set is ~10x bigger and contains an irreducible
        # floor (strings a model rightly refuses to change). Strict
        # improvement would keep paying for a full pass to move three of
        # them; the attempt ledger drains the floor, this stops the loop
        # once the rounds stop earning their cost.
        if last is not None and (last - len(offenders)) < max(3, first // 50):
            print(f"  round {rnd - 1} moved only {last - len(offenders)} of "
                  f"{last} — stopping and reporting what remains")
            break
        last = len(offenders)
        if first is None:
            first = last
        print(f"  filling {len(offenders)} entr(ies)…")
        n = fill_round(root, bstr, offenders)
        print(f"  filled {n}")
        if not n:
            break
        subprocess.run([sys.executable, str(ROOT / "forge.py"), "build"],
                       capture_output=True, cwd=str(root))

    swept = brand_sweep(root, brief)
    if swept:
        print(f"  brand sweep: retired the template's name from {swept} "
              f"entr(ies) the model left behind")
    subprocess.run([sys.executable, str(ROOT / "forge.py"), "build"],
                   capture_output=True, cwd=str(root))

    same, offenders = audit(root)
    _report(same, offenders, prefix="final: ")
    return 0


def _report(same, offenders, prefix=""):
    pct = f"{int(same * 100)}%" if same is not None else "?"
    print(f"  {prefix}copy identical to the template: {pct}   "
          f"entries still the template's: {len(offenders)}")
    for o in offenders[:5]:
        print(f"     [{o['i']}] {o['why']}: {o['cur'][:64]!r}")


if __name__ == "__main__":
    sys.exit(cmd_rebrand(sys.argv[1:]))
