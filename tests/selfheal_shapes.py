#!/usr/bin/env python3
"""Does the self-heal generalise, or did it solve one bug it was shown?

WHY THIS EXISTS

Aethron repaired one crash unattended: a comment containing "<body>"
broke document splitting. That is a demonstration, not a guarantee — one
crash, one shape, one model. The claim the owner actually needs is
"survives anything thrown at it", and a single success cannot support it.

So this throws SHAPES it has never seen. Each fault is injected into real
pipeline code and exercised on real project data, then reverted. They are
deliberately different KINDS of failure, because diagnosing each needs a
different move:

    attribute-on-None  the traceback names the line; the cause is upstream
    index-assumption   the line looks correct in isolation
    key-error          the missing key is named but not where it belongs
    hang               there is NO traceback at all, only a timeout

The last one matters most. Every other shape hands the agent a stack to
read; a hang hands it nothing, which is the case a person usually has to
take. If the loop can only work from tracebacks, its reach is smaller
than it looks and this is where that shows.

HONEST ABOUT WHAT THIS IS: these faults are injected, not discovered.
That tests the LOOP — evidence, diagnosis, machine-judged acceptance —
which is the part that must hold for unknown failures. It does not prove
the pipeline is bug-free, and nothing here should be read that way.

    python3 tests/selfheal_shapes.py            (all shapes, real model)
    python3 tests/selfheal_shapes.py --shape=hang
    python3 tests/selfheal_shapes.py --dry      (inject+verify only, free)
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROJECT = ROOT / "projects" / "jomiez-lesmana"     # 1 page: build is seconds


# Each shape: (file, anchor to find, replacement that breaks it, why it is
# a realistic class rather than a toy).
SHAPES = {
    "attribute-on-none": (
        "forge.py",
        'def cmd_build(_args):',
        'def cmd_build(_args):\n'
        '    _seal = read_cfg(Path.cwd()).get("manifest_version")\n'
        '    _ = _seal.strip()          # None on every existing project\n',
        "A config lookup that is None on real data and dereferenced. The "
        "traceback names the .strip() line, but the fix is upstream: the "
        "key is optional. This class has bitten this repo repeatedly.",
    ),
    "index-assumption": (
        "forge.py",
        '        for u, fn in lmap.items():',
        '        for u, fn in lmap.items():\n'
        '            _kind = fn.split(".")[2]   # every real name has ONE dot\n',
        "A split that assumes structure the input does not always have, "
        "sitting in a loop over real localized filenames. The line reads as "
        "obviously correct; only the data proves it wrong — exactly how the "
        "icon and chunk-name patterns failed.",
    ),
    "hang": (
        "forge.py",
        'def cmd_build(_args):',
        'def cmd_build(_args):\n'
        '    import time as _t\n'
        '    while True:                 # no traceback will ever arrive\n'
        '        _t.sleep(1)\n',
        "A step that never returns. There is no stack to read, so the only "
        "evidence is the command, its timeout and the code around where it "
        "stopped producing output. If the loop needs a traceback, it fails "
        "here — and a hang is the hardest failure for a person too.",
    ),
}


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True,
                          cwd=str(ROOT), **kw)


def inject(shape):
    path, anchor, replacement, _why = SHAPES[shape]
    f = ROOT / path
    src = f.read_text()
    if anchor not in src:
        return False, f"anchor not found in {path}: {anchor!r}"
    if src.count(anchor) != 1:
        return False, f"anchor is not unique in {path} ({src.count(anchor)}x)"
    f.write_text(src.replace(anchor, replacement, 1))
    return True, ""


def revert(shape):
    path = SHAPES[shape][0]
    sh("git", "checkout", "--", path)


def confirm_broken(shape, timeout=60):
    """The fault must actually break the step, or the test proves nothing.

    A shape that silently still works is the vacuous pass this project
    keeps re-learning: the run would 'succeed' having fixed nothing.
    """
    r = subprocess.run([sys.executable, str(ROOT / "forge.py"), "build"],
                       capture_output=True, text=True, cwd=str(PROJECT),
                       timeout=timeout)
    return r.returncode != 0


def run_shape(shape, live=True):
    path, _a, _r, why = SHAPES[shape]
    print(f"\n{'=' * 68}\nSHAPE: {shape}   (breaks {path})\n  {why}\n{'=' * 68}")
    ok, err = inject(shape)
    if not ok:
        print(f"  SKIP — {err}")
        return None
    try:
        if shape == "hang":
            broken = True          # by construction; confirming it costs the timeout
            print("  injected — the step now never returns (by construction)")
        else:
            try:
                broken = confirm_broken(shape)
            except subprocess.TimeoutExpired:
                broken = True
            print(f"  injected — step now fails: {broken}")
            if not broken:
                print("  SKIP — the fault does not break anything, so a pass "
                      "would be vacuous")
                return None
        if not live:
            return None

        import aethron_auto as auto
        auto.CRASH_FIXES.clear()
        t0 = time.time()
        # A hang must not wait an hour to be recognised as a hang.
        timeout = 90 if shape == "hang" else 900
        healed, _out = auto.step_with_fix(
            "build", [sys.executable, str(ROOT / "forge.py"), "build"],
            PROJECT, cwd=PROJECT, timeout=timeout, attempts=2, allow_fix=True)
        dt = time.time() - t0
        changed = bool(sh("git", "status", "--porcelain", path).stdout.strip())
        print(f"\n  RESULT  healed={healed}  touched_pipeline={changed}  "
              f"{dt:.0f}s")
        return {"shape": shape, "healed": bool(healed), "changed": changed,
                "seconds": round(dt)}
    finally:
        revert(shape)
        print(f"  reverted {path}")


def main(argv):
    live = "--dry" not in argv
    only = next((a.split("=", 1)[1] for a in argv
                 if a.startswith("--shape=")), None)
    shapes = [only] if only else list(SHAPES)
    dirty = [l for l in sh("git", "status", "--porcelain").stdout.splitlines()
             if l.strip() and not l.strip().startswith("??")]
    if dirty:
        print("REFUSING: uncommitted tracked changes — this test reverts "
              "files wholesale and would destroy them.")
        return 2
    results = [r for r in (run_shape(s, live) for s in shapes) if r]
    if results:
        print(f"\n{'=' * 68}\nSUMMARY")
        for r in results:
            print(f"  {r['shape']:<20} healed={str(r['healed']):<5} "
                  f"pipeline_changed={str(r['changed']):<5} {r['seconds']}s")
        n = sum(r["healed"] for r in results)
        print(f"\n  {n} of {len(results)} shape(s) healed unattended")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
