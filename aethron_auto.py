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
    branch = f"auto/{name or 'run'}-{time.strftime('%m%d-%H%M')}"
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


def prepare(proj, allow_fix=True):
    # FETCH FIRST. A Framer export's chunks, CMS blobs and icons live on
    # the platform CDN and are pulled by `fetch`; without it the site
    # still renders — because that CDN is reachable — and every check
    # that asks whether the copy is self-contained fails. Measured on
    # createstudio: 188 assets localized, 0 chunks, and the main script
    # still loading from framerusercontent.com.
    for step in ("fetch", "inventory", "localize", "build"):
        ok, _ = step_with_fix(step,
                              [sys.executable, str(ROOT / "forge.py"), step],
                              proj, cwd=proj, allow_fix=allow_fix)
        if not ok:
            return False
    return True


def convert(proj, allow_fix=True):
    # A framework port installs a toolchain and renders every page in a
    # browser. Measured: 16992s on a 16-page Framer site, where npm alone
    # exceeded a 1800s ceiling and killed the whole run.
    ok, out = step_with_fix(
        "convert",
        [sys.executable, str(ROOT / "aethron_convert.py"),
         str(proj), "--framework=astro"],
        proj, timeout=21600, allow_fix=allow_fix)
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


GUARDED = ("aethron_bridge.py", "aethron_brain.py", "aethron_fixer.py",
           "aethron_corpus.py", "aethron_auto.py")


def run_step(cmd, cwd=ROOT, timeout=3600, label=""):
    """-> (ok, output, exit_code). run() drops the code; a crash needs it."""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=str(cwd), timeout=timeout)
        out, code = r.stdout + r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(
            e.stdout, str) else ""
        out += f"\nTIMEOUT after {timeout}s"
        code = -9
    ok = code == 0
    say(f"{label}: {'ok' if ok else f'CRASHED (exit {code})'} "
        f"({time.time() - t0:.0f}s)", 1)
    return ok, out, code


def step_with_fix(label, cmd, proj, cwd=ROOT, timeout=3600,
                  attempts=3, allow_fix=True):
    """Run a pipeline step; when it CRASHES, let the agent fix it and retry.

    THE GAP THIS CLOSES. Only a named check on a finished build ever
    reached the agent. A step that exited non-zero became the string
    "conversion failed" and the run moved on — so a crash was the one
    failure Aethron could not even attempt, and every one of them needed
    a person. The Astro port died exactly there: a code comment
    containing "<body>" broke document splitting, and the whole 22-page
    conversion stopped on it.

    A crash is the BEST-evidenced defect there is. The traceback names
    the file and the line; nothing else in this pipeline points that
    precisely. What was missing was that nobody assembled it and handed
    it over.

    The acceptance rule is the same one that has held all along, because
    it is the only one that cannot be talked around: the model never
    decides it succeeded. The exact command that crashed must now
    complete, AND the corpus must still be clear. Anything else is
    reverted.
    """
    for attempt in range(1, attempts + 1):
        ok, out, code = run_step(cmd, cwd, timeout, label)
        if ok:
            if attempt > 1:
                # The command completing is necessary, not sufficient: a
                # fix that unblocks this input by breaking others is the
                # failure mode the corpus exists to catch, and it caught
                # one today.
                say("the step now completes — checking the corpus", 2)
                clear, _ = corpus_clear()
                if not clear:
                    say("the fix breaks another build in the corpus — "
                        "reverting it", 3)
                    git("checkout", "--", ".")
                    return False, out
                say(f"ACCEPTED — {label} completes and the corpus is clear", 2)
            return True, out
        for line in out.strip().splitlines()[-4:]:
            say(line[:120], 3)
        if not allow_fix or attempt == attempts:
            break
        say(f"handing the {label} crash to the coding agent "
            f"(attempt {attempt} of {attempts - 1})", 2)
        evidence = fixer.crash_evidence(cmd, cwd, code, out, root=ROOT)
        try:
            r = fixer.agent_fix(proj, "pipeline", label, evidence,
                                task=fixer.CRASH_TASK)
        except Exception as e:
            say(f"agent failed to start: {str(e)[:90]}", 3)
            break
        say(f"finished: ok={r['ok']} tools={len(r['tools'])} "
            f"cost=${r['cost_usd']:.4f}", 3)
        if r.get("error"):
            say(r["error"][:120], 3)
        touched = [l[-40:].strip()
                   for l in git("status", "--porcelain").stdout.splitlines()
                   if l.strip() and not l.strip().startswith("??")]
        crossed = [f for f in GUARDED if any(f in c for c in touched)]
        if crossed:
            say(f"REFUSED: touched {crossed} — the limits and the referee "
                f"are not editable. Reverting.", 3)
            git("checkout", "--", ".")
            break
        if not touched:
            say("changed nothing — the crash stands", 3)
            break
        say(f"changed: {touched}", 3)
    return False, ""


def _fills(proj):
    """{(section, index): value} for every non-empty fill in copy_map.

    The owner's choices, in the one place they live. A fix that shrinks
    this set has removed content, whatever it did for the checks.
    """
    p = proj / "copy_map.json"
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for sec, entries in (d.items() if isinstance(d, dict) else []):
        if not isinstance(entries, list):
            continue
        for i, e in enumerate(entries):
            if isinstance(e, dict) and str(e.get("new") or "").strip():
                out[(sec, i)] = str(e["new"])
    return out


def _cmap_bytes(proj):
    p = proj / "copy_map.json"
    return p.read_bytes() if p.is_file() else None


def _restore_cmap(proj, data):
    """copy_map lives under gitignored projects/, so `git checkout` cannot
    put it back. Keep the bytes and write them."""
    if data is not None:
        (proj / "copy_map.json").write_bytes(data)


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
            # THE CODING AGENT, with the repository and real tools. It
            # reproduces the failure by running the check itself, traces
            # the cause through the pipeline, writes a script if it needs
            # a measurement nobody took, edits, and verifies. Asking a
            # chat endpoint for a {old,new} pair instead gets a guess
            # from something that never ran the check it is fixing.
            say(f"handing '{check}' to the coding agent", 2)
            evidence = fixer.evidence_for(proj, build, check, report)
            fills_before, cmap_before = _fills(proj), _cmap_bytes(proj)
            try:
                r = fixer.agent_fix(proj, build, check, evidence)
            except Exception as e:
                say(f"agent failed to start: {str(e)[:90]}", 3)
                continue
            # DELETING THE CONTENT IS NOT FIXING THE DEFECT.
            #
            # "platform urls in shipped files" failed because one image
            # fill pointed at the template's CDN. The agent emptied the
            # entry: the check passed, the corpus stayed green, and the
            # owner's chosen image was silently gone — replaced by the
            # template's original. Every guard approved, because no guard
            # was watching the content.
            #
            # A check measures a property of the build. Passing it by
            # removing what the build was supposed to contain satisfies
            # the letter and destroys the point, and it is the cheapest
            # move available to anything being graded on checks alone.
            lost = [k for k in fills_before if k not in _fills(proj)]
            if lost:
                _restore_cmap(proj, cmap_before)
                say(f"REFUSED: the check would pass only because "
                    f"{len(lost)} owner fill(s) were deleted "
                    f"({', '.join(f'{s}[{i}]' for s, i in lost[:3])}). "
                    f"Content restored.", 3)
                say("a fix must keep what the owner chose — localize the "
                    "asset, do not discard it", 3)
                continue
            say(f"finished: ok={r['ok']} tools={len(r['tools'])} "
                f"cost=${r['cost_usd']:.4f}", 3)
            if r.get("error"):
                say(r["error"][:120], 3)
            if r.get("text"):
                say("said: " + r["text"].strip().splitlines()[-1][:110], 3)
            touched = [l[-40:].strip()
                       for l in git("status", "--porcelain").stdout.splitlines()
                       if l.strip() and not l.strip().startswith("??")]
            # ENFORCED, not merely asked. The prompt tells the agent its
            # limits are off-limits; this makes it true. A session that
            # hit the spend cap went and edited the spend cap — following
            # the error message to its source, which is reasonable
            # behaviour and still exactly what must not happen.
            # aethron_auto.py is on this list because it is the referee:
            # it decides what failed, what counts as fixed, and what gets
            # reverted. An agent editing it can pass itself.
            GUARDED = ("aethron_bridge.py", "aethron_brain.py",
                       "aethron_fixer.py", "aethron_corpus.py",
                       "aethron_auto.py")
            crossed = [f for f in GUARDED if any(f in c for c in touched)]
            if crossed:
                say(f"REFUSED — it edited its own limits ({crossed}); "
                    f"reverting everything", 3)
                git("checkout", "--", ".")
                continue
            # A FIX IS NOT ALWAYS AN EDIT. The correct answer to
            # "createstudio still fetches from the platform CDN" was to
            # re-run the pipeline, not to change a line — and the agent
            # did exactly that, took the migration from 2 failures to 0,
            # and was logged as "changed nothing" because only git was
            # consulted. Ask the check, not the diff.
            settled = fixer.doctor(proj, build)
            if settled["checks"].get(check) != "FAIL":
                say(f"'{check}' now passes (no source change — the agent "
                    f"corrected the build itself)", 3)
                applied.append({"check": check, "file": "(rebuild)",
                                "why": (r.get("text") or "").strip()[-120:]})
                progressed = True
                break
            if not touched:
                say("changed nothing and the check still fails", 3)
                continue
            say(f"changed: {touched}", 3)
            if not rebuild_for(proj, build):
                say("rebuild failed with the change — reverting", 3)
                git("checkout", "--", ".")
                continue
            after = fixer.doctor(proj, build)
            if after["checks"].get(check) == "FAIL":
                say(f"'{check}' still fails — reverting", 3)
                git("checkout", "--", ".")
                continue
            clear, _ = corpus_clear()
            if not clear:
                say("breaks another build in the corpus — reverting", 3)
                git("checkout", "--", ".")
                continue
            say(f"ACCEPTED — '{check}' passes and the corpus is clear", 3)
            applied.append({"check": check, "file": ", ".join(touched),
                            "why": (r.get("text") or "").strip()[-120:]})
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
    if not name and project:
        name = Path(project).resolve().name      # the branch should say which
    branch = start_branch(name) if allow_fix else "(not fixing)"
    if allow_fix and not branch:
        return 2

    proj = Path(project).resolve() if project else scrape(url, name)
    if not proj:
        say("scrape failed — nothing to check")
        return 1
    say(f"project: {proj.name}")

    if not project and not prepare(proj, allow_fix):
        say("preparation failed")
        return 1

    say("\n--- migration ---")
    mig_fails, mig_fixes = fix_loop(proj, "migration", key, model, allow_fix)

    say("\n--- framework port ---")
    if convert(proj, allow_fix):
        port_fails, port_fixes = fix_loop(proj, "port", key, model, allow_fix)
    else:
        port_fails, port_fixes = ["conversion failed"], []

    say("\n--- differential: anything no check looks for ---")
    d = subprocess.run([sys.executable, str(ROOT / "aethron_diff.py"),
                        str(proj), "--pair=migration:port"],
                       capture_output=True, text=True, cwd=str(ROOT),
                       timeout=5400)
    for line in d.stdout.splitlines():
        if any(k in line for k in ("noise floor", "DIFFERENCES", "missing "
                                   "from", "VERDICT", "ELEMENTS")):
            say(line.strip(), 1)
    diff_clean = d.returncode == 0

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
    say("  differential: " + ("no unexplained differences" if diff_clean
                              else "FOUND differences no check looks for"))
    # WHAT IT ACTUALLY COST, in the one unit that is not a guess. Token
    # counts come from the provider's own usage field and are exact; the
    # dollar figure multiplies them by our PRICES table, which is an
    # estimate nobody has ever checked against a real invoice. Printing
    # both makes that comparison possible: read the tokens here, read the
    # bill there, and the ratio settles whether the table is honest.
    try:
        import aethron_bridge
        u = aethron_bridge.usage_report()
        if u.get("requests"):
            say(f"  spend: {u['requests']} request(s), "
                f"{u['input']:,} in + {u['output']:,} out tokens "
                f"= ~${u['usd']:.4f} by our price table (ESTIMATE)")
    except Exception:
        pass
    (ROOT / "tests" / f"auto-{proj.name}.log").write_text("\n".join(LOG))
    return 0 if not (mig_fails or port_fails) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
