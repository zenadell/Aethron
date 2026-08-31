#!/usr/bin/env python3
"""One URL in, a judged migration out — with nobody watching.

WHY THIS EXISTS

Every failure so far needed a person: something looked wrong, a throwaway
script found the cause, a human wrote the fix. That does not scale past
one owner with patience, and it is not a product.

This runs the whole thing unattended. It migrates a template, checks the
result, and when a check fails it hands the named defect and its evidence
to a model, applies what comes back, and re-checks. The model never
decides it succeeded:

    a fix survives only if the failing check now passes
    AND every other build in the corpus still passes everything

Everything it changes happens on a git branch created for the run, so the
whole attempt is one `git diff` from review and one `git checkout` from
never having happened.

WHAT IT CANNOT DO, stated plainly: it only sees defects the checks look
for. A template that breaks in an unmeasured way will finish "clean" and
be wrong, and you will find it by eye. That is the real frontier, and
every time it happens the answer is a new check here — not a bigger
model.

    python3 aethron_auto.py --url=https://example.framer.website --name=foo
    python3 aethron_auto.py --project=projects/foo        (skip the scrape)
    python3 aethron_auto.py --url=... --no-fix            (check, never fix)
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import aethron_fixer as fixer                        # noqa: E402

MAX_FIX_ATTEMPTS = 3          # per check, before giving up and saying so
LOG = []


def say(msg, indent=0):
    line = "  " * indent + msg
    print(line, flush=True)
    LOG.append(line)


def run(cmd, cwd=ROOT, timeout=3600, label=""):
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True,
                       cwd=str(cwd), timeout=timeout)
    ok = r.returncode == 0
    say(f"{label or cmd[1] if len(cmd) > 1 else cmd[0]}: "
        f"{'ok' if ok else 'FAILED'} ({time.time() - t0:.0f}s)", 1)
    if not ok:
        tail = (r.stdout + r.stderr).strip().splitlines()[-4:]
        for t in tail:
            say(t[:120], 3)
    return ok, r.stdout + r.stderr


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True,
                          text=True, cwd=str(ROOT))


def start_branch(name):
    dirty = [l for l in git("status", "--porcelain").stdout.splitlines()
             if l.strip() and not l.strip().startswith("??")]
    if dirty:
        say("REFUSING: uncommitted changes in tracked files. This run may "
            "edit the pipeline and reverts wholesale; commit or stash first.")
        return None
    branch = f"auto/{name}-{time.strftime('%m%d-%H%M')}"
    git("checkout", "-b", branch)
    say(f"working on branch {branch}")
    return branch


def scrape(url, name):
    say(f"scraping {url}")
    ok, out = run([sys.executable, str(ROOT / "forge.py"), "init", url,
                   "--name", name], label="init")
    if not ok:
        return None
    made = ROOT / name
    dest = ROOT / "projects" / name
    if made.is_dir() and not dest.exists():
        made.rename(dest)
    for line in out.splitlines():
        if "NOT SCRAPED" in line or "scraped" in line and "page(s)" in line:
            say(line.strip(), 1)
    return dest if dest.is_dir() else None


def prepare(proj):
    for step in ("inventory", "localize", "build"):
        ok, _ = run([sys.executable, str(ROOT / "forge.py"), step],
                    cwd=proj, label=step)
        if not ok:
            return False
    return True


def convert(proj):
    ok, out = run([sys.executable, str(ROOT / "aethron_convert.py"),
                   str(proj), "--framework=astro"], label="convert")
    for line in out.splitlines():
        if "PIXEL-PERFECT" in line or "NOT ACCEPTED" in line \
                or "MOTION INCOMPLETE" in line:
            say(line.strip(), 1)
    return ok


def rebuild_for(proj, build):
    """Re-make the artifact a check reads, so a pipeline fix can show."""
    ok, _ = run([sys.executable, str(ROOT / "forge.py"), "build"],
                cwd=proj, label="rebuild")
    if ok and build == "port":
        ok = convert(proj)
    return ok


def corpus_clear():
    r = subprocess.run([sys.executable, str(ROOT / "aethron_corpus.py")],
                       capture_output=True, text=True, cwd=str(ROOT),
                       timeout=5400)
    return r.returncode == 0, r.stdout


def fix_loop(proj, build, key, model, allow_fix=True):
    """-> (remaining failures, fixes applied). Never lies about either."""
    applied = []
    for attempt in range(MAX_FIX_ATTEMPTS):
        report = fixer.doctor(proj, build)
        fails = [k for k, v in report["checks"].items() if v == "FAIL"]
        if not fails:
            say(f"{build}: all checks pass", 1)
            return [], applied
        say(f"{build}: {len(fails)} failing — {fails}", 1)
        if not allow_fix:
            return fails, applied

        progressed = False
        for check in fails:
            say(f"asking {model} about '{check}'", 2)
            prompt = fixer.evidence_for(proj, build, check, report)
            try:
                text, meta = fixer.ask(model, prompt, key)
            except Exception as e:
                say(f"model call failed: {str(e)[:80]}", 3)
                continue
            prop = fixer.parse_proposal(text)
            if not prop or not prop.get("file"):
                why = (prop or {}).get("why") or f"finish={meta.get('finish')}"
                say(f"no usable proposal ({str(why)[:70]})", 3)
                continue
            ok, msg = fixer.apply_proposal(prop)
            say(f"{msg} — {str(prop.get('why'))[:80]}", 3)
            if not ok:
                continue
            if not rebuild_for(proj, build):
                say("rebuild failed with the change — reverting", 3)
                git("checkout", "--", prop["file"])
                continue
            after = fixer.doctor(proj, build)
            if after["checks"].get(check) == "FAIL":
                say(f"'{check}' still fails — reverting", 3)
                git("checkout", "--", prop["file"])
                continue
            clear, _ = corpus_clear()
            if not clear:
                say("breaks another build in the corpus — reverting", 3)
                git("checkout", "--", prop["file"])
                continue
            say(f"ACCEPTED — '{check}' passes and the corpus is clear", 3)
            applied.append({"check": check, "file": prop["file"],
                            "why": prop.get("why")})
            progressed = True
            break
        if not progressed:
            report = fixer.doctor(proj, build)
            return [k for k, v in report["checks"].items()
                    if v == "FAIL"], applied
    report = fixer.doctor(proj, build)
    return [k for k, v in report["checks"].items() if v == "FAIL"], applied


def main(argv):
    url = next((a.split("=", 1)[1] for a in argv if a.startswith("--url=")), None)
    name = next((a.split("=", 1)[1] for a in argv if a.startswith("--name=")), None)
    project = next((a.split("=", 1)[1] for a in argv
                    if a.startswith("--project=")), None)
    allow_fix = "--no-fix" not in argv
    key, default_model = fixer.load_key()
    model = next((a.split("=", 1)[1] for a in argv
                  if a.startswith("--model=")), None) \
        or "deepseek/deepseek-v4-pro"
    if allow_fix and not key:
        say("no API key configured — running with --no-fix")
        allow_fix = False
    if not url and not project:
        print(__doc__)
        return 2
    if url and not name:
        name = url.split("//")[-1].split(".")[0].replace("/", "") or "site"

    t0 = time.time()
    branch = start_branch(name) if allow_fix else "(not fixing)"
    if allow_fix and not branch:
        return 2

    proj = Path(project).resolve() if project else scrape(url, name)
    if not proj:
        say("scrape failed — nothing to check")
        return 1
    say(f"project: {proj.name}")

    if not project and not prepare(proj):
        say("preparation failed")
        return 1

    say("\n--- migration ---")
    mig_fails, mig_fixes = fix_loop(proj, "migration", key, model, allow_fix)

    say("\n--- framework port ---")
    if convert(proj):
        port_fails, port_fixes = fix_loop(proj, "port", key, model, allow_fix)
    else:
        port_fails, port_fixes = ["conversion failed"], []

    say("\n--- parity: does the port do everything the migration does? ---")
    r = subprocess.run([sys.executable, str(ROOT / "aethron_parity.py"),
                        str(proj), "--pair=migration:port"],
                       capture_output=True, text=True, cwd=str(ROOT),
                       timeout=3600)
    for line in r.stdout.splitlines():
        if any(k in line for k in ("MISSING", "LOST", "VERDICT", "elements:")):
            say(line.strip(), 1)

    say(f"\n{'=' * 60}")
    say(f"RESULT for {proj.name}   ({time.time() - t0:.0f}s)")
    say(f"  migration: {'clean' if not mig_fails else str(mig_fails)}")
    say(f"  port     : {'clean' if not port_fails else str(port_fails)}")
    fixes = mig_fixes + port_fixes
    say(f"  fixes accepted by the corpus: {len(fixes)}")
    for f in fixes:
        say(f"     {f['check']} -> {f['file']}: {str(f['why'])[:70]}", 1)
    if branch and branch.startswith("auto/"):
        say(f"  review with: git diff main...{branch}")
    say("  NOTE: only defects the checks look for were examined. A failure "
        "nobody has written a check for finishes 'clean'.")
    (ROOT / "tests" / f"auto-{proj.name}.log").write_text("\n".join(LOG))
    return 0 if not (mig_fails or port_fails) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
