#!/usr/bin/env python3
"""ADVERSARIAL: try to make the auditor accept a lie.

WHY THIS EXISTS
---------------
aethron_audit's own selftest proves each rule fires on the case it was
written for. That is necessary and it is not enough: a rule that
catches the example its author had in mind is only proven against the
author's imagination. The interesting question is the opposite one —

    what does a lying instrument have to look like to get PAST it?

So every scenario below is HOSTILE. It is a verdict crafted to claim
success while proving nothing, shaped to slip through the exact wording
of a rule. Whatever gets through is a real hole, and it is reported as
a hole rather than quietly excluded from the count.

A test suite that only asserts things work is a suite that will one day
report a broken product as healthy — which is the failure this whole
layer was built to stop, so it would be a poor joke to build it on one.

    python3 tests/adapt_battery.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aethron_audit as A          # noqa: E402

CAUGHT, MISSED, NOTES = [], [], []


# A fresh, real file every verdict can point at. Without this, an
# attack that names no artifact trips UNCHECKABLE and the suite records
# a catch — while the lie it was actually probing sails through. That
# happened: the trivial-criterion attack reported CAUGHT on a rule that
# had nothing to do with it. One rule masking another is a false green,
# which is the precise failure this whole layer exists to prevent, so
# it would be a poor joke to leave it in the test for it.
_ART = None


def _artifact():
    global _ART
    if _ART is None:
        import tempfile
        _ART = Path(tempfile.mkdtemp(prefix="adapt-")) / "site.html"
        _ART.write_text("x")
    return _ART


def attack(name, verdicts, why_it_is_a_lie, expect_rule=None):
    """Run the auditor against a hostile verdict. It SHOULD complain —
    and it must complain about THIS lie, not a different one."""
    import time as _t
    vs = verdicts if isinstance(verdicts, list) else [verdicts]
    for v in vs:                      # isolate the rule under test
        if v.artifact is False:
            v.artifact = None         # deliberately unset for this one
        elif v.artifact is None:
            v.artifact = _artifact()
            v.at = _t.time() + 5      # fresh, so STALE cannot fire either
    try:
        findings = A.audit(vs)
    except Exception as e:
        MISSED.append((name, f"the auditor CRASHED: {type(e).__name__}: {e}"))
        print(f"  CRASH {name}\n        {type(e).__name__}: {e}")
        return None
    rules = {f.rule for f in findings}
    ok = bool(findings) and (expect_rule is None or expect_rule in rules)
    if ok:
        CAUGHT.append(name)
        print(f"  caught {name}   [{','.join(sorted(rules))}]")
    else:
        MISSED.append((name, why_it_is_a_lie))
        print(f"  MISSED {name}\n         {why_it_is_a_lie}")
    return rules


def main():
    V, P, F, S = A.Verdict, A.PASS, A.FAIL, A.SKIPPED

    print("── attacking VACUOUS: a PASS that proves nothing")
    attack("work counted in words, not numbers",
           V("verify", P, work={"files": "many"}),
           "work={'files':'many'} is not a count. The rule tests "
           "isinstance(n,(int,float)) and n==0, so a STRING passes "
           "through as if it were evidence of effort.")
    attack("one real-looking key hides an all-zero result",
           V("verify", P, work={"files": 0, "status": "done"}),
           "every NUMBER is zero; the string key makes all() False, so "
           "a check that examined nothing reports PASS.")
    attack("negative work",
           V("verify", P, work={"files": -5}),
           "-5 files is impossible. It is not zero, so it is accepted "
           "as proof of work.")
    attack("work about something else entirely",
           V("verify", P, work={"seconds_elapsed": 12}),
           "time spent is not work done — a hung process accrues "
           "seconds while examining nothing.")

    print("\n── attacking the STATUS field itself")
    attack("lowercase pass",
           V("verify", "pass", work={}),
           "'pass' != PASS, so every rule skips it and an unearned "
           "success is never audited at all.",)
    attack("an invented status",
           V("verify", "MOSTLY_OK", work={}),
           "an unknown status is audited by nothing; a tool can define "
           "its way out of scrutiny.")

    print("\n── attacking CONTRADICTION: disagreement made invisible")
    attack("the same property under two spellings",
           [V("a", P, work={"n": 1}, measures={"template_remaining": 0.55}),
            V("b", P, work={"n": 1}, measures={"templateRemaining": 0.02})],
           "the SAME quantity named two ways is never compared, so the "
           "contradiction that was found by hand this week would slip "
           "past if one tool renamed its key.")
    attack("disagreement expressed as text",
           [V("a", P, work={"n": 1}, measures={"identical": "99%"}),
            V("b", P, work={"n": 1}, measures={"identical": "12%"})],
           "string measures are skipped by the numeric filter, so two "
           "instruments can flatly contradict each other in words.")
    nan = float("nan")
    attack("NaN measurement",
           [V("a", P, work={"n": 1}, measures={"x": nan}),
            V("b", P, work={"n": 1}, measures={"x": 0.5})],
           "NaN fails every comparison silently, so the spread test is "
           "False and a meaningless number is treated as agreement.")

    print("\n── attacking STALE: a verdict older than its subject")
    _noart = V("verify", P, work={"files": 3})
    _noart.artifact = False           # sentinel: leave it unset
    attack("no artifact named",
           _noart,
           "a verdict that names no artifact can never be stale, so "
           "omitting the field is a way to never be caught by it.",)

    print("\n── attacking IMPOSSIBLE: bounds that do not bind")
    attack("bounds declared, but the value is a string",
           V("grade", P, work={"px": 10},
             evidence={"__bounds__": {"identical": ("1.4", 0.0, 1.0)}}),
           "a string value skips the numeric comparison, so declaring "
           "bounds and then reporting text evades the check.")

    print("\n── attacking UNFALSIFIABLE")
    attack("criteria counted, none of them real",
           V("forbidden_words", P, work={"files": 9},
             evidence={"criteria_checked": 1}),
           "one criterion is enough to pass the >0 test; a check can "
           "always find exactly one trivial thing to look at.")

    print("\n── does it survive malformed input at all?")
    for name, v in (("None work", V("x", P, work=None)),
                    ("evidence is a list",
                     V("x", P, work={"n": 1}, evidence={"problems": "notalist"})),
                    ("artifact is a directory", V("x", P, work={"n": 1},
                                                  artifact=ROOT)),
                    ("measures holds a dict",
                     V("x", P, work={"n": 1}, measures={"m": {"a": 1}}))):
        try:
            A.audit([v])
            print(f"  ok     survives {name}")
            NOTES.append(f"survives {name}")
        except Exception as e:
            MISSED.append((name, f"auditor CRASHED on malformed input: "
                                 f"{type(e).__name__}: {e}"))
            print(f"  CRASH  {name}: {type(e).__name__}: {e}")

    print("\n" + "=" * 68)
    print(f"attacks that were CAUGHT : {len(CAUGHT)}")
    print(f"attacks that GOT THROUGH : {len(MISSED)}")
    if MISSED:
        print("\nHOLES — each of these is a lie the auditor currently accepts:")
        for n, why in MISSED:
            print(f"\n  * {n}\n    {why}")
    print("\n" + "=" * 68)
    # Getting through is the FINDING, not a failure of the test. The
    # suite fails only if nothing was learned either way.
    print("adapt battery:", f"{len(CAUGHT)} caught, {len(MISSED)} holes found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
