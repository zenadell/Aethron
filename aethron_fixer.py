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

AGENT_TASK = """A check on this repository is failing. Fix the cause.

DEFECT: {check}
EVIDENCE: {evidence}
PROJECT: {project}   BUILD: {build}

Work the way an engineer works — you have the whole repository and real
tools, so use them. Run commands ONE AT A TIME and read each result
before the next: a previous attempt issued several in parallel, could not
tell which output belonged to which command, and abandoned a fix it had
very nearly found.

1. REPRODUCE IT. Run the check yourself and read the output:
       python3 aethron_doctor.py {project} --build={build} --quick
   Read `aethron_doctor.py` to see exactly what the failing check
   measures. The check's docstring says why it exists and what shipped
   when it did not.

2. FIND THE CAUSE, not the symptom. Read the pipeline: forge.py,
   aethron_convert.py, aethron_motion.py. Trace where the artifact the
   check reads is produced. If the evidence does not identify a cause,
   write a small script to measure what you need — that is how every
   defect in this repo was actually found.

3. FIX IT GENERALLY. The same defect will appear on templates nobody has
   seen. A special case for this one template is worthless.

4. VERIFY IT. Re-run the check — and note that the fix is sometimes to
   re-run the pipeline (fetch, localize, build) rather than to change
   code, because the artifact was stale rather than wrong. That counts.
   Then run
       python3 aethron_corpus.py
   which judges every other migration on disk. If it reports a
   regression, your change broke something else — fix that or revert.

Hard rules:
- Edit only pipeline source (forge.py, aethron_convert.py,
  aethron_motion.py, aethron_doctor.py). NEVER edit a project's site/ or
  pristine/ directory: those are generated and your edit is erased by the
  next build. This has been tried; it produced an invisible no-op.
- NEVER edit aethron_bridge.py, aethron_brain.py or aethron_fixer.py.
  Those hold the spend limits and the rules you are working under. If a
  limit stops you, that is the answer, not an obstacle — say you were
  stopped and why. A previous session hit the spend cap and edited the
  cap; the work was reverted and the defect was still there. Removing
  the thing that says no is never the fix.
- Do not weaken or delete a check to make it pass. If you believe the
  check itself is wrong, say so and explain why rather than editing it
  to be quiet.
- If you cannot find the cause, say that. A wrong fix is worse than none
  because it will be believed.

When you are done, state in one line what the cause was and what you
changed."""

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


def agent_fix(project: Path, build: str, check: str, evidence: str,
              cfg=None, timeout=1800, task=None) -> dict:
    """Let the coding agent do it — with tools, not a JSON guess.

    Aethron already drives the real Claude Code CLI (aethron_code.py):
    it can read the repository, run the checks, write a script to measure
    something nobody measured before, edit the pipeline and verify the
    result. Asking a chat endpoint for {old, new} instead throws all of
    that away and gets a one-shot guess from a model that never ran the
    failing check.

    Every defect in this repo was found by reproducing, measuring, and
    tracing. That is what this asks for.
    """
    import aethron_code
    prompt = (task or AGENT_TASK).format(
        check=check, evidence=evidence[:12000],
        project=str(project), build=build,
        editable=", ".join(EDITABLE))
    seen = []

    def on_event(ev):
        if ev.get("type") == "tool":
            seen.append(ev.get("name"))
        elif ev.get("type") == "text" and ev.get("text", "").strip():
            seen.append("text")

    r = aethron_code.run_once(ROOT, prompt, cfg=cfg, timeout=timeout,
                              on_event=on_event, idle=420)
    return {"ok": r.get("ok"), "error": r.get("error"),
            "text": r.get("text", ""), "tools": r.get("tools") or [],
            "cost_usd": r.get("cost_usd", 0.0)}


CRASH_TASK = """A pipeline STEP CRASHED on this repository. Fix the cause.

STEP: {check}
EVIDENCE:
{evidence}

This is not a failing check on a finished build — the command itself did
not complete, so there is no artifact to inspect. The traceback names the
file and the line. That is a better starting point than most defects get.

Work the way an engineer works, ONE COMMAND AT A TIME, reading each
result before running the next.

1. REPRODUCE IT. Run the exact command from the evidence and see the
   failure with your own eyes. If it does not reproduce, say so and stop
   — a fix for a failure you cannot trigger cannot be verified.

2. UNDERSTAND WHY THIS INPUT. The pipeline works on other templates, so
   the code is not simply broken: something about THIS input reached a
   case the code does not handle. Find what is different about it. Read
   the input that the failing line was processing.

3. FORM A HYPOTHESIS AND TEST IT BEFORE FIXING. Write a small script
   that demonstrates the cause — feed the suspect input to the suspect
   function and show it misbehaving. Every real defect in this repo was
   found that way, and more than one confident fix was wrong until a
   measurement contradicted it. If your script proves you wrong, that is
   the script working.

4. FIX THE GENERAL CASE. Templates nobody has seen will hit this code. A
   special case for this one input is worthless.

5. PROVE IT. Re-run the exact command from step 1 — it must now succeed.
   Then run
       python3 aethron_corpus.py
   which judges every other build on disk. A regression there means your
   change broke something that was working: fix that or revert.

6. KEEP THE SCRIPT. If you wrote something that found the cause, it is
   worth more than the fix — add it to tests/ so the next crash of this
   shape is caught automatically instead of rediscovered.

Hard rules:
- Edit only pipeline source: {editable}. NEVER edit a project's site/ or
  pristine/ directory — those are generated, and the edit is erased by
  the next build.
- NEVER edit aethron_bridge.py, aethron_brain.py, aethron_fixer.py or
  aethron_auto.py. Those hold the limits and the rules you work under,
  and aethron_auto.py is the referee that decides whether you succeeded.
  If a limit stops you, that is the answer — say you were stopped and
  why. Removing the thing that says no is never the fix.
- Do not make the command succeed by skipping the input. Silently
  dropping the page that crashed turns a loud failure into a missing
  page, which is worse.
- If you cannot find the cause, say so plainly and say what you ruled
  out. A wrong fix is worse than none.
"""


TRACE_FILE_RE = re.compile(r'File "([^"]+)", line (\d+)')


def crash_evidence(cmd, cwd, code, output, root=None) -> str:
    """Everything a person would want after a step exits non-zero.

    A crash carries better evidence than a failing check: the traceback
    names the file and the line. What was missing was that nobody
    assembled it — the runner kept four lines of tail and dropped the
    rest, so the one thing that pinpoints the cause never reached anyone.

    Includes the source around each editable frame, innermost first,
    because that is the line that actually raised.
    """
    root = Path(root or ROOT)
    tail = (output or "").strip().splitlines()
    lines = [f"COMMAND: {' '.join(str(c) for c in cmd)}",
             f"CWD: {cwd}", f"EXIT CODE: {code}", "",
             "OUTPUT (last 120 lines):",
             *tail[-120:], ""]
    frames = TRACE_FILE_RE.findall(output or "")
    ours = [(f, int(n)) for f, n in frames
            if Path(f).name in EDITABLE]
    if ours:
        lines.append("THE TRACEBACK NAMES OUR OWN CODE — innermost last, so "
                     "the final frame is where it raised:")
        for f, n in ours[-3:]:
            p = Path(f)
            if not p.is_file():
                p = root / p.name
            if not p.is_file():
                continue
            src = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            a, b = max(0, n - 12), min(len(src), n + 8)
            lines.append(f"--- {p.name} around line {n}")
            for i in range(a, b):
                mark = ">>" if i + 1 == n else "  "
                lines.append(f"{mark} {i + 1:>5}| {src[i]}")
            lines.append("")
    else:
        lines.append("No frame in the traceback belongs to editable pipeline "
                     "source; the failure may come from a tool we invoke "
                     "(npm, astro, the browser). Read the output above for "
                     "the file and message it names.")
    return "\n".join(lines)


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


PLATFORM_URL_RE = re.compile(
    r'https://(?:[a-z0-9.-]*website-files\.com|framerusercontent\.com'
    r'|d3e54v103j8qbb\.cloudfront\.net)/[^"\'\s<>`\\)]+')


def _project_side_evidence(project: Path, build: str, report: dict,
                           check: str) -> list:
    """Name the offending values, and say when they are CONTENT not code.

    The agent cannot fix what it has not been shown. Pointing it at the
    pipeline while withholding the actual string is how a well-specified
    defect becomes an open-ended search of the repository.
    """
    site = project / ("convert-astro" if build == "port" else "site")
    if not site.is_dir():
        return []
    out, seen = [], {}
    for f in sorted(site.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (
                ".html", ".js", ".mjs", ".css", ".json"):
            continue
        rel = str(f.relative_to(site)).replace("\\", "/")
        if "sources/" in rel or rel.startswith("ANIMATIONS/") \
                or "/ANIMATIONS/" in rel or Path(rel).name.startswith(".forge-"):
            continue
        try:
            for u in PLATFORM_URL_RE.findall(f.read_text(errors="ignore")):
                seen.setdefault(u, []).append(rel)
        except Exception:
            continue
    if not seen:
        return []
    out.append("THE OFFENDING VALUES, verbatim:")
    for u, files in list(seen.items())[:8]:
        out.append(f"  {u}")
        out.append(f"     in: {', '.join(sorted(set(files))[:3])}")

    # where did they come from? a value present in copy_map is CONTENT.
    cmp_path = project / "copy_map.json"
    if cmp_path.is_file():
        try:
            cm = json.loads(cmp_path.read_text())
        except Exception:
            cm = None
        hits = []
        if isinstance(cm, dict):
            for section, entries in cm.items():
                if not isinstance(entries, list):
                    continue
                for i, e in enumerate(entries):
                    if not isinstance(e, dict):
                        continue
                    for u in seen:
                        if u.split("?")[0] in str(e.get("new") or ""):
                            hits.append((section, i, e))
                            break
        if hits:
            out += ["", "THESE CAME FROM copy_map.json — THIS IS CONTENT, "
                        "NOT A PIPELINE BUG:"]
            for section, i, e in hits[:5]:
                out.append(f'  copy_map["{section}"][{i}].new = '
                           f'{str(e.get("new"))[:100]}')
                out.append(f'     .old = {str(e.get("old"))[:80]}')
            out += ["",
                    "FIX IT AS CONTENT. Change the entry's value with "
                    "mcp__aethron__set_content (or set_content_bulk) so it "
                    "points at a local asset instead of the platform CDN, "
                    "then build. Do NOT edit forge.py, aethron_doctor.py or "
                    "any other pipeline file for this: the pipeline is "
                    "shipping exactly the value it was given, which is "
                    "correct behaviour."]
    out.append("")
    return out


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
    # IS THIS A PIPELINE BUG OR A CONTENT VALUE? The two need opposite
    # fixes and the evidence used to describe only the first, under the
    # heading "the source most likely to contain it" — so the agent went
    # looking in forge.py for a defect that lived in one copy_map entry.
    # Measured: 15 tool calls and 1.98M input tokens spent reading
    # pipeline source, with the actual offending value never mentioned.
    #
    # A value that appears in the build AND in copy_map.json came from
    # the owner's own content, and the guarded content tools change it.
    # Nothing in the pipeline is broken and editing it would be wrong.
    lines += _project_side_evidence(project, build, report, check)
    lines.append("RELEVANT PIPELINE SOURCE (excerpts) — only useful if the "
                 "defect is in the pipeline itself, not in project content:")
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
    evidence = evidence_for(proj, build, check, report)

    if "--oneshot" not in argv:
        # THE AGENT, not a guess. It reads the repo, runs the failing
        # check itself, traces the cause and verifies its own work — the
        # way every defect in this repo was actually found. A chat
        # endpoint asked for {old, new} has never run the check it is
        # fixing.
        print(f"\n  handing '{check}' to the coding agent (tools, repo, "
              f"checks)")
        if dry:
            print("  --dry-run: the agent edits files, so not started")
            print("  it would be asked:\n")
            print(AGENT_TASK.format(check=check, evidence=evidence[:600],
                                    project=str(proj), build=build)[:1200])
            return 0
        r = agent_fix(proj, build, check, evidence)
        print(f"  agent finished: ok={r['ok']} tools={len(r['tools'])} "
              f"cost=${r['cost_usd']:.4f}")
        if r.get("error"):
            print(f"  {r['error'][:160]}")
        if r.get("text"):
            print(f"  agent said: {r['text'].strip().splitlines()[-1][:150]}")
        touched = git_dirty()
        if not touched:
            print("  it changed nothing — no fix to judge")
            return 1
        print(f"  changed: {[l[-40:].strip() for l in touched]}")

        print("\n  judging …")
        after = doctor(proj, build)
        if after["checks"].get(check) == "FAIL":
            print(f"  REJECT — '{check}' still fails")
        else:
            clear, out = corpus_clear()
            if not clear:
                print("  REJECT — it breaks another build in the corpus")
                for line in out.splitlines():
                    if "->" in line or "REGRESSION" in line:
                        print("     " + line.strip())
            else:
                print(f"  ACCEPT — '{check}' passes and the corpus is clear")
                print("  the change is in your working tree; review it")
                return 0
        git_revert()
        print("  reverted")
        return 1

    # --oneshot: the old chat-completion path, kept for comparing models
    print(f"\n  asking {model} about: {check}")
    try:
        text, meta = ask(model, evidence, key)
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
    if fixed["checks"].get(check) == "FAIL":
        print(f"  REJECT — '{check}' still fails after the change")
    else:
        clear, out = corpus_clear()
        if not clear:
            print("  REJECT — the change breaks another build in the corpus")
        else:
            print(f"  ACCEPT — '{check}' now passes and the corpus is clear")
            return 0
    git_revert()
    print("  reverted")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
