#!/usr/bin/env python3
"""Every check Aethron can run on itself, in one command.

    python3 tests/run_all.py            everything
    python3 tests/run_all.py --quick    skip the browser/CLI batteries

Nothing here needs an API key, a login or a network: the model-facing
paths are proved against in-process mock providers, and the runtime
paths against a real headless browser if one is installed (and honestly
SKIPPED if not).
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

SUITES = [
    ("syntax", [PY, "-m", "py_compile", "forge.py", "studio.py",
                "forge_mcp.py", "aethron_agent.py", "aethron_code.py",
                "aethron_brain.py", "aethron_bridge.py", "aethron_cloud.py",
                "aethron_healer.py", "desktop.py",
                "aethron_figma.py", "aethron_figma_grade.py",
                "aethron_audit.py", "aethron_vision.py",
                "aethron_edit.py", "aethron_screen.py",
                "aethron_web.py"], False),
    ("brain (one key for everything)",
     [PY, "aethron_brain.py", "--selftest"], False),
    ("self-update (in-place, never a second copy)",
     [PY, "aethron_update.py", "--selftest"], False),
    ("bridge (Anthropic <-> OpenAI translation)",
     [PY, "aethron_bridge.py", "--selftest"], False),
    ("code layer (drives the real CLI, mock providers)",
     [PY, "aethron_code.py", "--selftest"], True),
    ("key ring (free keys first, paid last)",
     [PY, "tests/keyring_battery.py"], False),
    # The auditor is held to its own standard: this lies to it once
    # per rule and proves it catches each.
    ("audit (does it catch a lying instrument?)",
     [PY, "aethron_audit.py", "--selftest"], False),
    # Adversarial: tries to make the auditor ACCEPT a lie. Reports the
    # holes it finds rather than asserting there are none.
    ("adapt battery (can the auditor be fooled?)",
     [PY, "tests/adapt_battery.py"], False),
    ("figma battery (design import + the pixel referee)",
     [PY, "tests/figma_battery.py"], True),
    # Measures a page whose values WE set, so every number has a right
    # answer. Vision models score 7.89% on font size when it breaks the
    # expected pattern; a measurement either reads it or is broken.
    ("vision (measure a screenshot against ground truth)",
     [PY, "aethron_vision.py", "--selftest"], True),
    # The seam where a model is allowed near a measured page. Its own
    # selftest proves the allow-list refuses a bad edit; this renders,
    # and proves an ALLOWED edit that damages the page is caught too.
    ("edit (can a model change the page without breaking it?)",
     [PY, "aethron_edit.py", "--selftest"], False),
    # The owner's correction: a rebuild that matches a screenshot
    # perfectly has faithfully reproduced its blur and its complete
    # absence of behaviour. This asks whether the output is a WEBSITE.
    ("web (is it a website, or a picture of one?)",
     [PY, "aethron_web.py", "--selftest"], False),
    ("screen (one page, six frameworks, one description)",
     [PY, "aethron_screen.py", "--selftest"], False),
    ("design battery (does each design rule actually fire?)",
     [PY, "tests/design_battery.py"], True),
    ("eye battery (can the referee see, and can it be fooled?)",
     [PY, "tests/eye_battery.py"], True),
    ("flow battery (a poster becomes a website, both halves)",
     [PY, "tests/flow_battery.py"], True),
    ("edit battery (is collateral damage actually noticed?)",
     [PY, "tests/edit_battery.py"], True),
    ("probe battery (runtime + framework-port referee)",
     [PY, "tests/probe_battery.py"], True),
    # Adversarial: builds sites that are obviously broken to a human and
    # asks whether the probe hands them over as healthy. It did, eight
    # times out of nine, because it measured the HTML string instead of
    # the screen.
    ("probe adversary (can a blank page pass as healthy?)",
     [PY, "tests/probe_adversary.py"], True),
    ("motion battery (does the PORT actually move)",
     [PY, "tests/motion_battery.py"], True),
    ("healer battery (deterministic -> agent -> checks decide)",
     [PY, "tests/healer_battery.py"], True),
    # A crash was the one failure that never reached the agent at all,
    # so every one of them needed a person. This asserts the routing and
    # the refusals; whether a given model fixes it needs --live.
    ("self-heal battery (a crash reaches the agent, refusals hold)",
     [PY, "tests/selfheal_battery.py"], False),
]

# A suite that hangs is worse than one that fails: it reports nothing at
# all, and the run above it looked green for half an hour before anyone
# noticed. The healer battery did exactly that — blocked inside heal()
# with a 30 minute inner timeout, so `tail -25` printed an empty string
# and exit 0 read as success. Time out here and call it a failure.
LIMIT = 900


def main():
    quick = "--quick" in sys.argv
    rows, failed, unproven = [], 0, 0
    for name, cmd, slow in SUITES:
        if quick and slow:
            rows.append((name, "skipped", 0))
            continue
        t = time.time()
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                               timeout=LIMIT)
            out = r.stdout + r.stderr
            ok = r.returncode == 0
        except subprocess.TimeoutExpired as e:
            out = ((e.stdout or b"").decode("utf-8", "replace")
                   if isinstance(e.stdout, bytes) else (e.stdout or ""))
            out += f"\nTIMED OUT after {LIMIT}s — no verdict, not a pass"
            ok = False
        dt = time.time() - t
        tail = out.strip().splitlines()
        # A SUITE THAT DID NOT RUN IS NOT A SUITE THAT PASSED.
        #
        # The rule is written in this repo's invariants and the HARNESS
        # was breaking it: the motion battery reported
        # "VERDICT: SKIPPED — the built port is STALE" and exited 0,
        # because refusing to grade stale output is correct behaviour,
        # not an error. This table then printed PASS and the last line
        # said ALL GREEN, so twelve checks that never executed read as
        # twelve checks that succeeded. Exit code is not a verdict; the
        # verdict is a verdict.
        skipped = any(l.strip().startswith(("VERDICT: SKIPPED", "SKIPPED"))
                      for l in tail)
        summary = next((l for l in reversed(tail) if "green" in l
                        or "ok" in l.lower() or "FAIL" in l), "")
        if skipped:
            why = next((l.strip() for l in tail
                        if "SKIPPED" in l), "reason not given")
            note = why.split("SKIPPED", 1)[-1].lstrip(" —-:")
            rows.append((name, ("SKIP  " + note)[:78], dt))
            unproven += 1
            continue
        failed += not ok
        rows.append((name, ("PASS  " + summary.strip())[:78] if ok
                     else ("FAIL  " + "\n".join(tail[-12:]))[:1200], dt))
    print("\n" + "=" * 70)
    for name, res, dt in rows:
        print(f"{name:52} {dt:5.1f}s  {res}")
    print("=" * 70)
    if failed:
        print(f"{failed} suite(s) failed")
    elif unproven:
        print(f"NOT ALL GREEN — {unproven} suite(s) SKIPPED and are "
              f"UNVERIFIED (not proven good, not proven bad)")
    else:
        print("ALL GREEN")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
