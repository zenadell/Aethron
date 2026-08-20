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
    ("healer battery (deterministic -> agent -> checks decide)",
     [PY, "tests/healer_battery.py"], True),
]


def main():
    quick = "--quick" in sys.argv
    rows, failed = [], 0
    for name, cmd, slow in SUITES:
        if quick and slow:
            rows.append((name, "skipped", 0))
            continue
        t = time.time()
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        dt = time.time() - t
        tail = (r.stdout + r.stderr).strip().splitlines()
        summary = next((l for l in reversed(tail) if "green" in l
                        or "ok" in l.lower() or "FAIL" in l), "")
        ok = r.returncode == 0
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
