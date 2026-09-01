#!/usr/bin/env python3
"""The jury. Every check is judged against every migration we trust.

WHY THIS EXISTS

Aethron can now detect the failures it has already met. The open problem
is the failure nobody has met yet — a template with a structure no one
has seen, breaking in a way no check looks for. The answer is for the
system to write the missing check itself. The reason that is safe, and
the reason it needs this file, is an asymmetry:

    a wrong FIX fails silently — it corrupts output and nobody notices
    a wrong CHECK fails loudly — it fires on a build that is fine

So a new check can be judged automatically, which a new fix cannot:

    ACCEPT a check only if it
        FAILS on the build that motivated it
        PASSES on every migration already known to be good

That is what this runs. The corpus is every project on disk that has
been verified; the baseline is what they scored when they were known
good. Anything that newly fails is either a real regression or a check
that cries wolf, and both need a person before they ship.

This was not a theory. The doctor's first run produced two failures and
both were its OWN false positives — a `preconnect` hint counted as a
rented dependency, and a minified script read as missing. They were
caught in seconds because they fired on builds known to be fine. Without
a corpus they would have shipped, and a checker that cries wolf teaches
you to ignore the one that is real.

    python3 aethron_corpus.py --baseline      record what "good" means
    python3 aethron_corpus.py                 run and compare to baseline
    python3 aethron_corpus.py --validate --broken=projects/x
                                              does the check set actually
                                              tell broken from good?
    python3 aethron_corpus.py --live          include browser checks (slow)

Exit code is 0 only when nothing regressed and nothing cried wolf.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

BASELINE = ROOT / "tests" / "corpus_baseline.json"
BUILDS = ("migration", "port")


def projects(root: Path):
    """Every Aethron project on disk, in a stable order."""
    base = root / "projects"
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.iterdir()):
        # a leading dot means retired: superseded projects are set aside
        # rather than deleted, and judging them reports failures nobody
        # intends to fix
        if d.name.startswith("."):
            continue
        if (d / "forge.json").is_file() and (d / "site").is_dir():
            out.append(d)
    return out


def has_build(proj: Path, build: str) -> bool:
    sub = {"migration": "site", "port": "convert-astro/dist"}[build]
    return (proj / sub / "index.html").is_file()


def run_doctor(proj: Path, build: str, live: bool) -> dict:
    """-> {check name: status}. A crash is itself a result worth keeping."""
    cmd = [sys.executable, str(ROOT / "aethron_doctor.py"), str(proj),
           f"--build={build}"]
    if not live:
        cmd.append("--quick")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=1800, cwd=str(ROOT))
        out = r.stdout
    except subprocess.TimeoutExpired:
        return {"__crashed__": "FAIL", "__detail__": "doctor timed out"}
    except Exception as e:                                # pragma: no cover
        return {"__crashed__": "FAIL", "__detail__": str(e)[:120]}
    checks = {}
    for line in out.splitlines():
        s = line.strip()
        for token, status in (("PASS  ", "PASS"), ("FAIL  ", "FAIL"),
                              ("SKIP  ", "SKIPPED"), ("note  ", "NOTE")):
            if s.startswith(token):
                rest = s[len(token):]
                name = rest.split(":", 1)[0].strip()
                if name:
                    checks[name] = status
                break
    checks["__seconds__"] = round(time.time() - t0, 1)
    return checks


def survey(root: Path, live: bool) -> dict:
    result = {}
    for proj in projects(root):
        for build in BUILDS:
            if not has_build(proj, build):
                continue
            key = f"{proj.name}:{build}"
            print(f"  running {key} …", flush=True)
            result[key] = run_doctor(proj, build, live)
    return result


def compare(base: dict, now: dict):
    """-> (regressions, improvements, unseen). A check that newly FAILS on
    a build that was good is the signal this whole file exists for."""
    regressions, improvements, unseen = [], [], []
    for key, checks in now.items():
        old = base.get(key)
        if old is None:
            unseen.append(key)
            continue
        for name, status in checks.items():
            if name.startswith("__"):
                continue
            was = old.get(name)
            if was is None:
                # a check that did not exist when the baseline was taken.
                # If it fails here, it is either finding something real or
                # crying wolf — either way a person decides.
                if status == "FAIL":
                    regressions.append((key, name, "new check fails", status))
                continue
            if was == "PASS" and status == "FAIL":
                regressions.append((key, name, was, status))
            elif was == "FAIL" and status == "PASS":
                improvements.append((key, name))
    return regressions, improvements, unseen


def main(argv):
    live = "--live" in argv
    root = ROOT

    if "--validate" in argv:
        broken = next((a.split("=", 1)[1] for a in argv
                       if a.startswith("--broken=")), None)
        if not broken:
            print("--validate needs --broken=<project path>")
            return 2
        bp = Path(broken).resolve()
        build = next((a.split("=", 1)[1] for a in argv
                      if a.startswith("--build=")), "port")
        print(f"VALIDATING the check set against {bp.name}:{build}\n")
        bad = run_doctor(bp, build, live)
        bad_fails = [k for k, v in bad.items()
                     if v == "FAIL" and not k.startswith("__")]
        print(f"  on the BROKEN build: {len(bad_fails)} check(s) fire")
        for k in bad_fails:
            print(f"     FAIL {k}")
        good_noise = []
        for proj in projects(root):
            if proj.resolve() == bp:
                continue
            for b in BUILDS:
                if not has_build(proj, b):
                    continue
                res = run_doctor(proj, b, live)
                for k, v in res.items():
                    if v == "FAIL" and not k.startswith("__"):
                        good_noise.append((f"{proj.name}:{b}", k))
        print(f"\n  on KNOWN-GOOD builds: {len(good_noise)} false positive(s)")
        for key, k in good_noise[:10]:
            print(f"     {key}: {k}")
        ok = bool(bad_fails) and not good_noise
        print(f"\n  VERDICT: " + (
            "ACCEPT — the checks tell broken from good"
            if ok else
            "REJECT — " + ("nothing detected the breakage"
                           if not bad_fails else
                           "fires on builds that are fine")))
        return 0 if ok else 1

    print(f"corpus: {len(projects(root))} project(s), "
          f"{'live' if live else 'static'} checks\n")
    now = survey(root, live)
    if not now:
        print("no projects with builds — nothing to judge")
        return 2

    if "--baseline" in argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(now, indent=1))
        n_checks = sum(len([k for k in v if not k.startswith("__")])
                       for v in now.values())
        print(f"\nbaseline recorded: {len(now)} build(s), {n_checks} check "
              f"result(s) -> {BASELINE.relative_to(ROOT)}")
        fails = [(k, c) for k, v in now.items() for c, s in v.items()
                 if s == "FAIL" and not c.startswith("__")]
        if fails:
            print(f"NOTE: {len(fails)} check(s) already fail — the baseline "
                  f"records reality, not perfection:")
            for k, c in fails[:10]:
                print(f"   {k}: {c}")
        return 0

    if not BASELINE.is_file():
        print("no baseline yet — run with --baseline first")
        return 2
    base = json.loads(BASELINE.read_text())
    regressions, improvements, unseen = compare(base, now)

    print(f"\n{'=' * 62}\nCORPUS: {len(now)} build(s) judged against baseline"
          f"\n{'=' * 62}")
    if improvements:
        print(f"  FIXED: {len(improvements)}")
        for key, name in improvements[:10]:
            print(f"     {key}: {name}")
    if unseen:
        print(f"  new build(s) with no baseline: {', '.join(unseen[:6])}")
    if regressions:
        # TWO DIFFERENT FINDINGS, REPORTED SEPARATELY.
        #
        # Both stop the run, but they mean opposite things and lead to
        # opposite investigations. A true regression means MY CHANGE broke
        # a build that used to pass. A new check failing means the build
        # was never judged on it — the defect may be years old, or the
        # check may be crying wolf.
        #
        # Printing both under "a build that was good now fails a check"
        # sent a reader hunting for a regression that did not exist: eight
        # builds were flagged, the baseline held only seven checks, and
        # none of the eight had ever been measured on the new one.
        true_regs = [r for r in regressions if r[2] != "new check fails"]
        new_fails = [r for r in regressions if r[2] == "new check fails"]
        if true_regs:
            print(f"  REGRESSIONS — was passing, now fails: {len(true_regs)}")
            for key, name, was, now_s in true_regs:
                print(f"     {key}: {name}  ({was} -> {now_s})")
        if new_fails:
            print(f"  NEW CHECK, NOT IN BASELINE — fails: {len(new_fails)}")
            for key, name, _was, _now in new_fails:
                print(f"     {key}: {name}")
            print("     (never measured before: a real old defect, or the "
                  "check is wrong. Not caused by this change.)")
        print("\n  VERDICT: STOP —", end=" ")
        if true_regs:
            print("a build that was good now fails a check.")
            print("  Either the build broke, or the check does. Both need a "
                  "person.")
        else:
            print("a new check fails on builds it never judged before.")
            print("  Confirm each is real, then fix it or the check, then "
                  "re-baseline.")
        return 1
    print("  no regressions")
    print("\n  VERDICT: CLEAR — every known-good build still passes "
          "every check")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
