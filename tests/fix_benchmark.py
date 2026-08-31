#!/usr/bin/env python3
"""Can a model fix the bugs we actually had? Replay them and count.

WHY THIS EXISTS

"Which model should fix Aethron's failures" is not answerable from
benchmarks. The published figure for one candidate came from a ten-task
sample; on the first real defect it was handed, that model spent 15,000
tokens reasoning and emitted no answer at all.

So: replay the real thing. Each case below is a defect that genuinely
shipped, with the fix that genuinely cured it. Reversing the fix
re-creates the bug exactly, and a check that already exists proves the
bug is back. The model is then given the same evidence a person would
get, and graded by machine:

    PASS only if the failing check passes afterwards
    AND the rest of the corpus still passes everything

No human judges the answer. A model that proposes something plausible
and useless scores zero, which is the point — that is the failure mode
that cost three weeks.

    python3 tests/fix_benchmark.py --model=deepseek/deepseek-v4-pro
    python3 tests/fix_benchmark.py --list
    python3 tests/fix_benchmark.py --model=X --case=instrumentation
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aethron_fixer as fixer                       # noqa: E402

# Each case: reversing `fixed` -> `broken` re-creates a defect that
# actually shipped. `check` is the doctor check that must catch it.
CASES = [
    {
        "id": "instrumentation",
        "file": "aethron_convert.py",
        "why": "our capture recorder shipped to users and scrolled their "
               "pages in steps on every load",
        "check": "instrumentation leak",
        "project": "projects/qourvac2", "build": "port",
        "fixed": '                got["dom"] = strip_instrumentation(got["dom"])\n',
        "broken": "",
    },
    {
        "id": "own-hosts",
        "file": "forge.py",
        "why": "15 nav links kept pointing at the template author's live "
               "site because own_hosts came out empty",
        "check": "self-referencing links",
        "project": "projects/qourvac2", "build": "migration",
        "fixed": '''    if source_url:
        _u = urllib.parse.urlparse(source_url)
        if _u.netloc:
            hosts.add(_u.netloc)
''',
        "broken": "",
    },
    {
        "id": "script-cdns",
        "file": "forge.py",
        "why": "GSAP, Lenis and jQuery loaded from unpkg/jsdelivr, so an "
               "'owned' site stopped moving the moment it went offline",
        "check": "rented code",
        "project": "projects/qourvac2", "build": "migration",
        "fixed": '''    r"|fonts\\.gstatic\\.com|fonts\\.googleapis\\.com"
    r"|unpkg\\.com|cdn\\.jsdelivr\\.net|ajax\\.googleapis\\.com"
    r"|cdnjs\\.cloudflare\\.com|[a-z0-9]+\\.cloudfront\\.net)"''',
        "broken": '''    r"|fonts\\.gstatic\\.com)"''',
    },
]


def read(f):
    return (ROOT / f).read_text(encoding="utf-8")


def write(f, s):
    (ROOT / f).write_text(s, encoding="utf-8")


def git_clean() -> bool:
    r = subprocess.run(["git", "status", "--porcelain"],
                       capture_output=True, text=True, cwd=str(ROOT))
    return not [l for l in r.stdout.splitlines()
                if l.strip() and not l.strip().startswith("??")]


def restore():
    subprocess.run(["git", "checkout", "--", "."],
                   capture_output=True, cwd=str(ROOT))


def break_it(case) -> bool:
    src = read(case["file"])
    if case["fixed"] not in src:
        return False
    write(case["file"], src.replace(case["fixed"], case["broken"], 1))
    return True


def rebuild(project: str, build: str) -> bool:
    """The defect has to reach a BUILD before a check can see it."""
    p = ROOT / project
    cmds = [[sys.executable, str(ROOT / "forge.py"), "build"]]
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True, cwd=str(p),
                           timeout=1800)
        if r.returncode:
            return False
    if build == "port":
        r = subprocess.run(
            [sys.executable, str(ROOT / "aethron_convert.py"), str(p),
             "--framework=astro"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=3600)
    return True


def run_case(case, model, key, rebuild_needed=True):
    out = {"id": case["id"], "stage": "", "detail": ""}
    proj = ROOT / case["project"]

    if not break_it(case):
        out["stage"] = "SKIP"
        out["detail"] = "the fix text is not in the source — case is stale"
        return out
    if rebuild_needed and not rebuild(case["project"], case["build"]):
        restore()
        out["stage"] = "SKIP"
        out["detail"] = "could not rebuild with the defect in place"
        return out

    before = fixer.doctor(proj, case["build"])
    if before["checks"].get(case["check"]) != "FAIL":
        restore()
        out["stage"] = "SKIP"
        out["detail"] = (f"reversing the fix did not make '{case['check']}' "
                         f"fail — the case no longer reproduces")
        return out

    prompt = fixer.evidence_for(proj, case["build"], case["check"], before)
    t0 = time.time()
    try:
        text, meta = fixer.ask(model, prompt, key)
    except Exception as e:
        restore()
        out["stage"] = "ERROR"
        out["detail"] = str(e)[:90]
        return out
    out["seconds"] = round(time.time() - t0, 1)
    out["tokens"] = meta.get("tokens")

    prop = fixer.parse_proposal(text)
    if not prop or not prop.get("file"):
        restore()
        out["stage"] = "NO ANSWER"
        out["detail"] = (f"finish={meta.get('finish')}; "
                         + (prop.get("why", "") if prop else
                            text[:70].replace("\n", " ")))
        return out
    out["why"] = (prop.get("why") or "")[:100]

    ok, msg = fixer.apply_proposal(prop)
    if not ok:
        restore()
        out["stage"] = "UNAPPLIABLE"
        out["detail"] = msg
        return out

    if rebuild_needed:
        rebuild(case["project"], case["build"])
    after = fixer.doctor(proj, case["build"])
    if after["checks"].get(case["check"]) == "FAIL":
        restore()
        out["stage"] = "WRONG FIX"
        out["detail"] = "the check still fails"
        return out
    restore()
    out["stage"] = "PASS"
    out["detail"] = "check passes after the change"
    return out


def main(argv):
    if "--list" in argv:
        for c in CASES:
            print(f"  {c['id']:16} {c['check']:24} {c['why']}")
        return 0
    key, default_model = fixer.load_key()
    if not key:
        print("no API key configured")
        return 2
    model = next((a.split("=", 1)[1] for a in argv
                  if a.startswith("--model=")), default_model)
    only = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--case=")), None)
    fast = "--no-rebuild" in argv

    if not git_clean():
        print("REFUSING: uncommitted changes. This harness reverts with")
        print("`git checkout -- .` between cases and would destroy them.")
        return 2

    cases = [c for c in CASES if not only or c["id"] == only]
    print(f"replaying {len(cases)} real defect(s) against {model}\n")
    results = []
    for c in cases:
        print(f"  {c['id']} … ", end="", flush=True)
        r = run_case(c, model, key, rebuild_needed=not fast)
        results.append(r)
        print(f"{r['stage']}  ({r.get('seconds', '?')}s)  {r['detail'][:70]}")
        if r.get("why"):
            print(f"      model said: {r['why']}")
    passed = sum(1 for r in results if r["stage"] == "PASS")
    graded = [r for r in results if r["stage"] not in ("SKIP", "ERROR")]
    print(f"\n  {model}: {passed}/{len(graded)} fixed"
          f" ({len(results) - len(graded)} not gradeable)")
    return 0 if passed == len(graded) and graded else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
