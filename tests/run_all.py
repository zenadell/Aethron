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
                "aethron_healer.py", "desktop.py"], False),
    ("brain (one key for everything)",
     [PY, "aethron_brain.py", "--selftest"], False),
    ("bridge (Anthropic <-> OpenAI translation)",
     [PY, "aethron_bridge.py", "--selftest"], False),
    ("code layer (drives the real CLI, mock providers)",
     [PY, "aethron_code.py", "--selftest"], True),
    ("probe battery (runtime + framework-port referee)",
     [PY, "tests/probe_battery.py"], True),
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
    rows, failed = [], 0
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
        summary = next((l for l in reversed(tail) if "green" in l
                        or "ok" in l.lower() or "FAIL" in l), "")
        failed += not ok
        rows.append((name, ("PASS  " + summary.strip())[:78] if ok
                     else ("FAIL  " + "\n".join(tail[-12:]))[:1200], dt))
    print("\n" + "=" * 70)
    for name, res, dt in rows:
        print(f"{name:52} {dt:5.1f}s  {res}")
    print("=" * 70)
    print("ALL GREEN" if not failed else f"{failed} suite(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
