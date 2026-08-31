#!/usr/bin/env python3
"""A model proposes. The corpus disposes.

WHY THIS EXISTS

Aethron detects the failures it has met. For one it has never met, some
intelligence has to write the missing check and the missing fix. The
danger is obvious — a model editing the pipeline is how you corrupt every
future migration silently — so the whole design is about making the
model's output cheap to reject:

    the model NEVER decides it succeeded
    a fix is accepted only when
        the failing check now passes on the build that motivated it, AND
        every other build in the corpus still passes everything

Both conditions are machine-checkable, so nobody has to referee. And the
work is applied in a git worktree, so rejection is `git checkout` rather
than archaeology.

The other half of the design is what the model is ASKED. Handing a model
"this site looks wrong" produced three weeks of plausible nonsense. The
doctor produces a NAMED defect with evidence and a reproduction, which is
a far smaller question — small enough that a cheap flash model can answer
it, which is what makes running this on every failure affordable.

    python3 aethron_fixer.py --project=projects/foo --build=port
    python3 aethron_fixer.py --project=projects/foo --dry-run
    python3 aethron_fixer.py --compare            (rank models on one defect)

Exit code is 0 only when a proposed fix was accepted by the corpus.
"""
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "aethron_config.json"
# Files a fix may touch. The pipeline, not its output: a model editing
# site/ or pristine/ produces an invisible no-op that the next build
# overwrites, which is exactly what happened the one time it was allowed.
EDITABLE = ("forge.py", "aethron_convert.py", "aethron_motion.py",
            "aethron_doctor.py", "aethron_source.py")

SYSTEM = """You fix defects in Aethron, a tool that migrates Framer and \
Webflow templates into self-owned sites and ports them to frameworks.

You are given ONE named defect, the evidence that proves it, and the \
relevant source. Propose the smallest change that removes the cause.

Rules that are not negotiable:
- Edit only the pipeline source you are shown. Never touch a project's
  site/ or pristine/ directory: those are generated, and an edit there is
  overwritten by the next build.
- Prefer a fix that is general. The same defect will appear on templates
  nobody has seen; a special case for this one template is worthless.
- If the evidence does not identify a cause, say so. A wrong fix is worse
  than none, because it will be believed.

Reply with ONLY a JSON object, no prose, no markdown fence:
{"file": "<one of the shown files>",
 "old": "<exact unique text to replace, copied verbatim from the source>",
 "new": "<replacement text>",
 "why": "<one sentence: the cause, not the symptom>"}
If you cannot identify the cause: {"file": null, "why": "<what is missing>"}
"""


def load_key():
    if not CONFIG.is_file():
        return None, None
    ai = (json.loads(CONFIG.read_text()).get("ai") or {})
    return ai.get("api_key"), ai.get("model") or "z-ai/glm-5.3-flash"


def ask(model, prompt, key, max_tokens=12000, timeout=600):
    """Budget generously: these are REASONING models.

    Their thinking counts against max_tokens, so a budget sized for the
    answer alone gets spent before a single character of it is emitted —
    measured, 5137 tokens of reasoning under a 2000 ceiling returned an
    empty `content` and the caller reported "could not parse a proposal"
    when the truth was "ran out of room to speak"."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0}).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://aethron.local",
                 "X-Title": "Aethron"})
    t0 = time.time()
    # A long reasoning answer is exactly when a connection drops mid-body:
    # measured, IncompleteRead(7722 bytes) on a call that had already been
    # thinking for a minute. Losing that work to a transport hiccup is the
    # difference between a loop that converges and one that looks broken.
    last = None
    for attempt in range(3):
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            r = json.loads(raw)
            break
        except Exception as e:
            last = e
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    choice = r["choices"][0]
    txt = (choice["message"].get("content") or "").strip()
    usage = r.get("usage") or {}
    return txt, {"seconds": round(time.time() - t0, 1),
                 "tokens": usage.get("total_tokens"),
                 "finish": choice.get("finish_reason"),
                 "reasoned": bool(choice["message"].get("reasoning"))}


def parse_proposal(text):
    """Models wrap JSON in prose and fences no matter what you ask."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def doctor(project: Path, build: str) -> dict:
    r = subprocess.run(
        [sys.executable, str(ROOT / "aethron_doctor.py"), str(project),
         f"--build={build}", "--quick"],
        capture_output=True, text=True, timeout=900, cwd=str(ROOT))
    checks, details = {}, {}
    for line in r.stdout.splitlines():
        s = line.strip()
        for token, status in (("PASS  ", "PASS"), ("FAIL  ", "FAIL"),
                              ("SKIP  ", "SKIPPED"), ("note  ", "NOTE")):
            if s.startswith(token):
                rest = s[len(token):]
                name, _, detail = rest.partition(":")
                checks[name.strip()] = status
                details[name.strip()] = detail.strip()
                break
    return {"checks": checks, "details": details, "raw": r.stdout}


def evidence_for(project: Path, build: str, check: str, report: dict) -> str:
    """The defect, its evidence, and the source most likely to contain it."""
    lines = [f"DEFECT: {check}",
             f"EVIDENCE: {report['details'].get(check, '(none)')}",
             f"PROJECT: {project.name}   BUILD: {build}", ""]
    # the doctor's own docstring for that check explains what it means
    doc = (ROOT / "aethron_doctor.py").read_text(encoding="utf-8")
    fn = re.search(r'def (check_\w+)\([^)]*\):\n\s+"""(.*?)"""',
                   doc, re.S)
    for m in re.finditer(r'def (check_\w+)\([^)]*\):\n\s+"""(.*?)"""', doc, re.S):
        if check.split()[0].lower() in m.group(1).lower() or \
                check.replace(" ", "_") in m.group(1):
            lines += ["WHAT THIS CHECK MEANS:", m.group(2).strip(), ""]
            break
    lines.append("RELEVANT SOURCE (excerpts):")
    for name in EDITABLE:
        p = ROOT / name
        if not p.is_file():
            continue
        src = p.read_text(encoding="utf-8")
        # a crude but effective locator: the parts of the pipeline whose
        # text mentions what the check is about
        key = check.split()[0].lower()
        hits = [m.start() for m in re.finditer(re.escape(key), src, re.I)][:3]
        for h in hits:
            a, b = max(0, h - 900), min(len(src), h + 900)
            lines.append(f"--- {name} @ {src[:h].count(chr(10)) + 1}")
            lines.append(src[a:b])
    return "\n".join(lines)[:60000]


def apply_proposal(prop) -> tuple:
    f = prop.get("file")
    if not f or f not in EDITABLE:
        return False, f"file not editable: {f}"
    p = ROOT / f
    src = p.read_text(encoding="utf-8")
    old, new = prop.get("old") or "", prop.get("new") or ""
    if not old:
        return False, "no 'old' text given"
    n = src.count(old)
    if n == 0:
        return False, "'old' text does not appear in the file"
    if n > 1:
        return False, f"'old' text appears {n} times — not unique"
    p.write_text(src.replace(old, new, 1), encoding="utf-8")
    return True, f"applied to {f}"


def git_dirty() -> list:
    r = subprocess.run(["git", "status", "--porcelain"] + list(EDITABLE),
                       capture_output=True, text=True, cwd=str(ROOT))
    return [l for l in r.stdout.splitlines() if l.strip()]


def git_revert():
    subprocess.run(["git", "checkout", "--"] + list(EDITABLE),
                   capture_output=True, cwd=str(ROOT))


def corpus_clear() -> tuple:
    r = subprocess.run([sys.executable, str(ROOT / "aethron_corpus.py")],
                       capture_output=True, text=True, timeout=3600,
                       cwd=str(ROOT))
    return r.returncode == 0, r.stdout


def main(argv):
    key, default_model = load_key()
    if not key:
        print("no API key in aethron_config.json — nothing to run")
        return 2
    project = next((a.split("=", 1)[1] for a in argv
                    if a.startswith("--project=")), None)
    build = next((a.split("=", 1)[1] for a in argv
                  if a.startswith("--build=")), "port")
    model = next((a.split("=", 1)[1] for a in argv
                  if a.startswith("--model=")), default_model)
    dry = "--dry-run" in argv
    if not project:
        print(__doc__)
        return 2
    proj = Path(project).resolve()

    print(f"reading {proj.name}:{build} …")
    report = doctor(proj, build)
    fails = [k for k, v in report["checks"].items() if v == "FAIL"]
    if not fails:
        print("  nothing fails — no defect to fix")
        return 0
    print(f"  {len(fails)} failing check(s): {fails}")

    if git_dirty() and not dry:
        # a dry run proposes without touching anything, so uncommitted
        # work is only a hazard for a real attempt
        print("\n  REFUSING: pipeline source has uncommitted changes.")
        print("  A rejected fix is reverted with git checkout, which would")
        print("  destroy them. Commit or stash first.")
        return 2

    check = fails[0]
    prompt = evidence_for(proj, build, check, report)
    print(f"\n  asking {model} about: {check}")
    try:
        text, meta = ask(model, prompt, key)
    except Exception as e:
        print(f"  model call failed: {str(e)[:160]}")
        return 2
    print(f"  replied in {meta['seconds']}s ({meta['tokens']} tokens, "
          f"finish={meta.get('finish')})")
    if not text and meta.get("finish") == "length":
        print("  the answer was truncated before it began — the model spent "
              "its whole budget reasoning. Raise max_tokens.")
        return 1
    prop = parse_proposal(text)
    if not prop:
        print("  could not parse a proposal from the reply:")
        print("   ", text[:300].replace("\n", " "))
        return 1
    if not prop.get("file"):
        print(f"  model declined: {prop.get('why')}")
        return 1
    print(f"  proposes editing {prop['file']}: {prop.get('why')}")
    if dry:
        print("\n--- dry run, not applying ---")
        print("OLD:", (prop.get("old") or "")[:200])
        print("NEW:", (prop.get("new") or "")[:200])
        return 0

    ok, msg = apply_proposal(prop)
    print(f"  {msg}")
    if not ok:
        return 1

    print("\n  judging …")
    fixed = doctor(proj, build)
    still = fixed["checks"].get(check) == "FAIL"
    clear, out = corpus_clear()
    if still:
        print(f"  REJECT — '{check}' still fails after the change")
    elif not clear:
        print("  REJECT — the change breaks another build in the corpus")
        for line in out.splitlines():
            if "REGRESSION" in line or "->" in line:
                print("     " + line.strip())
    else:
        print(f"  ACCEPT — '{check}' now passes and the corpus is clear")
        print("  the change is in your working tree; review and commit it")
        return 0
    git_revert()
    print("  reverted")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
