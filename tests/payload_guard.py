#!/usr/bin/env python3
"""Every embedded JS payload must substitute and parse. Both, always.

Two failure modes that have each cost real time here, and neither of
which any other check catches:

1. A STRAY PERCENT SIGN. The payloads are Python templates, so one bare
   '%' in a comment breaks substitution at runtime — far from the edit
   that caused it. A comment written to explain this bug contained two
   of them and broke the template it was warning about.

2. A STALE ARTIFACT. `node --check` on a file written by an earlier run
   reports the OLD payload as fine. The check must render the payload
   from the module in the same breath as validating it, or it is
   measuring history.

Run it directly, or let tests/run_all.py do it:

    python3 tests/payload_guard.py
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A valid placeholder is %(name)s / %(name)d / %(name).2f, and %% is an
# escaped literal. Anything else is a mistake waiting to happen.
VALID = re.compile(r"%(?:%|\(\w+\)[-+ 0-9.]*[sdifgr])")

# Substitutions wide enough to render every payload in the project.
SUBS = {
    "props": "[]", "watched": "[]", "want": "[]", "sel": '""', "idx": 0,
    "ms": 1000, "scroll": -1, "from": 0, "to": 1000, "times": "[0]",
    "span": 1000, "delay": 100, "post": "/x", "watch": 1000, "stops": 4,
    "settle": 100, "hold": 100, "every": 40, "max": 1000, "samples": 4,
    "step": 0.5, "dwell": 100, "owned": "{}", "steps": 8,
}


def payloads():
    import aethron_lab, aethron_motion, aethron_convert
    for mod in (aethron_lab, aethron_motion, aethron_convert):
        for name in dir(mod):
            if name.endswith("_JS") or name == "MOTION_JS":
                yield mod.__name__, name, getattr(mod, name)


def main():
    node = subprocess.run(["node", "--version"], capture_output=True)
    have_node = node.returncode == 0
    if not have_node:
        print("SKIPPED — no node, JS syntax is UNVERIFIED (not proven good)")

    bad, checked = [], 0
    for modname, name, src in payloads():
        if not isinstance(src, str):
            continue
        checked += 1
        # 1. no stray percent signs — but ONLY in payloads that are
        #    actually substituted. A payload with no placeholders never
        #    goes through Python's formatting, so translate(-50%,-50%)
        #    and rootMargin '-8% 0px' are correct CSS in it, not bugs.
        is_template = "%(" in src
        if is_template:
            for m in re.finditer(r"%", src):
                i = m.start()
                if VALID.match(src, i):
                    continue
                if i and src[i - 1] == "%":       # second half of an escape
                    continue
                bad.append(f"{modname}.{name}: stray '%' at {i} — "
                           f"...{src[max(0, i - 40):i + 20]!r}")

        # 2. it must actually substitute
        try:
            rendered = src % SUBS if is_template else src
        except Exception as exc:
            bad.append(f"{modname}.{name}: substitution failed — {exc}")
            continue

        # 3. and parse, rendered FRESH — never from a file on disk
        if have_node:
            body = rendered
            if not body.lstrip().startswith("(function"):
                body = "(function(){" + body + "})();"
            f = Path(tempfile.mkstemp(suffix=".js")[1])
            f.write_text(body)
            r = subprocess.run(["node", "--check", str(f)],
                               capture_output=True, text=True)
            f.unlink(missing_ok=True)
            if r.returncode:
                first = (r.stderr.strip().splitlines() or [""])[:3]
                bad.append(f"{modname}.{name}: JS syntax — {' / '.join(first)}")

    print(f"checked {checked} payload(s)")
    for b in bad:
        print("  FAIL " + b)
    if bad:
        print(f"\n{len(bad)} problem(s)")
        return 1
    print("all payloads substitute and parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
