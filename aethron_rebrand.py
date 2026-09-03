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


def make_brief(plan_text, cm, cfg=None):
    import aethron_brain as brain
    raw = brain.text_call(
        BRIEF_PROMPT % {"plan": plan_text.strip(),
                        "sample": _sample_template(cm)}, cfg=cfg)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("the model did not return a brief")
    return json.loads(m.group(0))


def brief_text(b):
    """The brief as the fill prompt should see it — prose, not JSON."""
    v = b.get("vocabulary") or {}
    lines = [
        f"BRAND: {b.get('brand','')}",
        f"WHAT THEY DO: {b.get('one_liner','')}",
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

    offenders = []
    for i, e in enumerate(cm.get("strings", [])):
        cur = str(e.get("new") or e.get("old", ""))
        if len(cur.strip()) < 3:
            continue
        why = None
        if not str(e.get("new", "")).strip() and len(e.get("old", "")) > 25:
            why = "never filled"
        elif avoid and any(w in cur.lower() for w in avoid):
            why = "still uses the template's vocabulary"
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
                if strings[i].get("max_bytes") else {})}
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
        for i, ent in zip(chunk, got):
            new = str(ent.get("new") or "").strip()
            if not new or new == strings[i]["old"]:
                continue
            cap = strings[i].get("max_bytes")
            if cap and len(new.encode()) > int(cap):
                continue                      # the lock is not negotiable
            if "`" in new or "${" in new:
                continue
            strings[i]["new"] = new
            filled += 1
        print(f"    batch {start // size + 1}/{(len(idx) + size - 1) // size}: "
              f"{filled} filled so far")
    cm_path.write_text(json.dumps(cm, indent=1, ensure_ascii=False))
    return filled


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

    plan = plan_arg or ""
    if not plan:
        p = root / "project_plan.md"
        plan = p.read_text() if p.is_file() else ""
    if not plan.strip():
        print("no plan — pass --plan=\"Name, what you do, email\" or write "
              "project_plan.md")
        return 1

    bp = root / "brand_brief.json"
    if bp.is_file():
        brief = json.loads(bp.read_text())
        print(f"brief: reusing {bp.name} for {brief.get('brand','?')!r}")
    else:
        print("brief: expanding the owner's notes…")
        brief = make_brief(plan, cm)
        bp.write_text(json.dumps(brief, indent=1, ensure_ascii=False))
        print(f"  brand={brief.get('brand')!r}  "
              f"vocabulary={len(brief.get('vocabulary') or {})} mappings  "
              f"avoid={len(brief.get('avoid') or [])} words")
    bstr = brief_text(brief)

    last = None
    for rnd in range(1, rounds + 1):
        same, offenders = audit(root)
        _report(same, offenders, prefix=f"round {rnd} before: ")
        if not offenders:
            break
        # Stop when a round buys nothing: another identical pass is just
        # spend. Saying so is more useful than looping to the cap.
        if last is not None and len(offenders) >= last:
            print("  no improvement on the last round — stopping and "
                  "reporting what remains")
            break
        last = len(offenders)
        print(f"  filling {len(offenders)} entr(ies)…")
        n = fill_round(root, bstr, offenders)
        print(f"  filled {n}")
        if not n:
            break
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
