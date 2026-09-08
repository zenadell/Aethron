#!/usr/bin/env python3
"""The checks get checked. An auditor for lying instruments.

WHY
---
Every hard bug in this project's history was found the same way: a tool
ran, returned cleanly, and was WRONG — and a person noticed an answer
that could not be true. Not by adding more checks. By distrusting one.

    Framer checks on a Next.js site .... reported healthy, blank page
    healer battery ..................... hung 30 min, exit 0, read as pass
    run_all.py | tail .................. reported TAIL's exit code
    converter's referee filter ......... "NOT OWNED" never reached the log
    port graded on probe exit code ..... folded in the ORIGINAL's defects
    motion under a virtual clock ....... 0.60 deg/s for 71.86 deg/s motion
    the motion gap tool ................ compared against a BROKEN render
                                         of the original
    a print format ..................... f"{0.999:.2f}" -> "1.00"
    today's rebrand verdict ............ "55% still the template" while
                                         another measure said 7 of 314

Each of those instruments emitted a verdict it had not earned. That is
the failure class this module exists to catch, and it is mechanisable —
not as intuition, but as rules.

THE PRIMITIVE
-------------
A check may no longer return a bare word. It returns a VERDICT plus the
evidence that it did the work:

    Verdict(check="probe", status=PASS,
            work={"pages": 8, "requests": 174},   <- proof of effort
            evidence={"failed_requests": 0},      <- what it measured
            artifact=Path("site/index.html"))     <- what it judged

PASS stops being a word a tool can simply emit. It becomes a claim with
a burden of proof, and `audit()` refuses the ones that cannot carry it.

KNOWN LIMIT, stated rather than hidden
--------------------------------------
tests/adapt_battery.py attacks these rules with verdicts crafted to
slip past them. ELEVEN lies got through the first version and are now
caught. ONE remains, and it is not fixable by rule:

    Verdict(PASS, work={"files": 9}, evidence={"criteria_checked": 1})

UNFALSIFIABLE only fires at zero criteria. One passes, and a check can
always find exactly one trivial thing to look at. Whether a criterion
is MEANINGFUL is not decidable from outside — nine files with one
genuine criterion is a legitimate check — so a ratio heuristic would
cry wolf on honest runs, and a warning that cries wolf gets ignored.

It stays open, named, and printed by the battery. Quietly dropping the
attack to reach a green number would be the exact failure this module
exists to catch.

WHAT THIS IS NOT
----------------
It is not a model, and it does not guess. Every rule below is a
deterministic reading of a verdict against its own evidence, so the
auditor cannot itself hallucinate a problem — and it is held to its own
standard: tests/audit_battery.py lies to it deliberately, once per
rule, and proves it catches each.

    python3 aethron_audit.py --selftest
"""
import json
import re
import sys
import time
from pathlib import Path

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"

# How far two instruments measuring the same property may disagree
# before one of them is lying. Not a tuning knob for making things
# quiet: widen it and you are choosing not to be told.
AGREE_TOL = 0.25


class Verdict:
    """One check's claim, with the evidence for it."""

    def __init__(self, check, status, work=None, evidence=None,
                 artifact=None, measures=None, baseline=None,
                 instrument=None, at=None, raw=""):
        self.check = check
        self.status = status
        self.work = dict(work or {})
        self.evidence = dict(evidence or {})
        self.artifact = Path(artifact) if artifact else None
        # measures: {property_name: value}. Two checks reporting the
        # same property is what makes contradiction detectable.
        self.measures = dict(measures or {})
        self.baseline = dict(baseline or {})
        self.instrument = dict(instrument or {})
        self.at = at if at is not None else time.time()
        self.raw = raw

    def __repr__(self):
        return f"<Verdict {self.check}={self.status} work={self.work}>"

    def to_json(self):
        return {"check": self.check, "status": self.status,
                "work": self.work, "evidence": self.evidence,
                "measures": self.measures, "baseline": self.baseline,
                "instrument": self.instrument, "at": self.at,
                "artifact": str(self.artifact) if self.artifact else None}


class Finding:
    def __init__(self, rule, check, why, fix, severity="serious"):
        self.rule, self.check = rule, check
        self.why, self.fix, self.severity = why, fix, severity

    def __repr__(self):
        return f"<{self.rule} {self.check}>"

    def line(self):
        return (f"{self.rule:<12} {self.check}\n"
                f"             {self.why}\n"
                f"             fix: {self.fix}")


# ─────────────────────────── the rules ───────────────────────────────
# Each one is a real incident from this project, generalised.

# Keys that measure the CLOCK, not the work. A hung process accrues
# seconds while examining nothing, so time is never evidence of effort.
TIME_KEYS = ("second", "ms", "millis", "duration", "elapsed", "time",
             "took", "runtime")


def _work_evidence(work):
    """-> (positive_counts, why_not). What did this check actually do?

    Hardened after an adversarial pass got ELEVEN lies past the first
    version. Each clause below is one of them:
      work={'files': 'many'}          a word is not a count
      work={'files': 0, 'ok': 'yes'}  a string key hid an all-zero result
      work={'files': -5}              impossible, and not zero
      work={'seconds_elapsed': 12}    the clock is not the work
    """
    if not work:
        return 0, "declared no work at all"
    counts, bad, timeonly = 0, [], True
    for k, v in work.items():
        if not any(t in str(k).lower() for t in TIME_KEYS):
            timeonly = False
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            bad.append(f"{k}={v!r} is not a number")
            continue
        if v != v or v in (float("inf"), float("-inf")):
            bad.append(f"{k} is not a finite number")
            continue
        if v < 0:
            bad.append(f"{k}={v:g} is negative, which counts nothing")
            continue
        counts += v
    if bad:
        return 0, "; ".join(bad[:3])
    if timeonly:
        return 0, ("only measures elapsed time — a process that hangs "
                   "accrues seconds while examining nothing")
    if counts <= 0:
        return 0, "every count is zero"
    return counts, ""


def r_status(vs, ctx):
    """A status the auditor does not recognise is audited by NOTHING.

    'pass' (lowercase) and 'MOSTLY_OK' both slipped every rule in the
    first version — a tool could define its way out of scrutiny simply
    by spelling its own verdict."""
    out = []
    for v in vs:
        if v.status not in (PASS, FAIL, SKIPPED):
            out.append(Finding(
                "UNKNOWN", v.check,
                f"status {v.status!r} is not PASS, FAIL or SKIPPED, so "
                f"no rule can judge it — an unrecognised verdict is "
                f"audited by nothing",
                "emit one of the three, exactly; a tool must not be "
                "able to define its way out of being checked"))
    return out


def r_vacuous(vs, ctx):
    """PASS with no work done.

    THE ORIGINAL SIN. Framer-shaped checks 'passed' a Next.js site and
    shipped a blank page, because none of them could even run against
    it — and a check that cannot run reporting PASS is worse than no
    check, since it converts an unknown into a false assurance."""
    out = []
    for v in vs:
        if v.status != PASS:
            continue
        counts, why = _work_evidence(v.work)
        if counts <= 0:
            out.append(Finding(
                "VACUOUS", v.check,
                f"reported PASS but {why} — there is no evidence it "
                f"examined anything",
                "report a POSITIVE COUNT of things actually examined "
                "(files, pages, pixels, requests). A PASS that cannot "
                "say what it counted is a SKIP."))
    return out


def r_silent(vs, ctx):
    """Exited successfully, said nothing.

    The healer battery blocked for thirty minutes inside a nested
    timeout, printed nothing, and exited 0. `tail` showed an empty
    string and the run above it looked green. A hang is the worst
    vacuous pass, because it looks exactly like speed."""
    out = []
    for v in vs:
        inst = v.instrument or {}
        if v.status == PASS and inst.get("exit_code") == 0 \
                and inst.get("output_bytes", 1) == 0:
            out.append(Finding(
                "SILENT", v.check,
                "the process exited 0 but produced NO output — an exit "
                "code is not a verdict, and a hang that is killed can "
                "exit 0 too",
                "require a verdict line in the output; absent one, the "
                "result is SKIPPED regardless of exit code"))
        if inst.get("through_pipe"):
            out.append(Finding(
                "MASKED", v.check,
                "the exit code was read through a pipe, so it belongs to "
                "the LAST command in it, not to the check",
                "redirect to a file and read $? directly; never judge a "
                "suite through `| tail`"))
    return out


def _norm_prop(name):
    """template_remaining, templateRemaining and Template-Remaining are
    one property. Two instruments must not escape comparison by
    disagreeing about capitalisation."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _as_number(val):
    """-> float, or None if it is not a usable measurement.

    Accepts '99%' and '0.55' because a tool reporting its number as
    text is still reporting a number — the first version skipped every
    string, so two instruments could contradict each other in words
    and never be compared. Rejects NaN and infinities outright."""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
    elif isinstance(val, str):
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*(%?)\s*$", val)
        if not m:
            return None
        f = float(m.group(1))
        if m.group(2):
            f /= 100.0
    else:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def r_contradiction(vs, ctx):
    """Two instruments, one property, different answers.

    Today: one measure said 55% of the copy was still the template's
    while another said 7 entries of 314. Both ran, both returned, and
    they were answering the same question differently. Nothing flagged
    it — a person noticed."""
    out = []
    byprop = {}
    for v in vs:
        for prop, val in v.measures.items():
            num = _as_number(val)
            if num is None:
                if isinstance(val, float) and val != val:
                    out.append(Finding(
                        "IMPOSSIBLE", v.check,
                        f"{prop!r} is NaN — not a measurement. NaN fails "
                        f"every comparison silently, so it reads as "
                        f"agreement with anything",
                        "a check that cannot compute the number must "
                        "report SKIPPED, not emit NaN"))
                continue
            # SAME QUANTITY, DIFFERENT SPELLING. An adversarial pass got
            # 0.55 and 0.02 past this rule simply by naming one key
            # template_remaining and the other templateRemaining.
            byprop.setdefault(_norm_prop(prop), []).append((v.check, num))
    for prop, pairs in byprop.items():
        if len(pairs) < 2:
            continue
        lo = min(pairs, key=lambda p: p[1])
        hi = max(pairs, key=lambda p: p[1])
        spread = abs(hi[1] - lo[1])
        scale = max(abs(hi[1]), abs(lo[1]), 1e-9)
        if spread / scale > AGREE_TOL:
            out.append(Finding(
                "CONTRADICTION", f"{lo[0]} vs {hi[0]}",
                f"both measured {prop!r} and disagree: "
                f"{lo[0]}={lo[1]:g}, {hi[0]}={hi[1]:g}",
                "at least one instrument is wrong — do not average them "
                "and do not pick the convenient one; find out which"))
    return out


def r_impossible(vs, ctx):
    """A value that cannot be true.

    A page that rendered while logging zero requests. A comparison
    reporting 100% identical on files that differ. Bounds are declared
    by the check itself, so this catches an instrument drifting outside
    the physics it claims to measure."""
    out = []
    for v in vs:
        for k, bound in (v.evidence.get("__bounds__") or {}).items():
            try:
                val, lo, hi = bound
            except Exception:
                continue
            num = _as_number(val)
            if num is None:
                # Declaring bounds and then reporting text is a way to
                # look checked without being checked. (It also CRASHED
                # the first version, which its own AUDITOR rule caught.)
                out.append(Finding(
                    "IMPOSSIBLE", v.check,
                    f"{k}={val!r} has declared bounds but is not a "
                    f"number, so the bound tests nothing",
                    "report the measurement as a number, or do not "
                    "claim it is bounded"))
                continue
            if num < _as_number(lo) or num > _as_number(hi):
                out.append(Finding(
                    "IMPOSSIBLE", v.check,
                    f"{k}={num:g} is outside the possible range "
                    f"[{_as_number(lo):g}, {_as_number(hi):g}]",
                    "the instrument is broken, not the subject — fix the "
                    "measurement before believing any verdict from it"))
        ren = v.evidence.get("rendered_chars")
        req = v.evidence.get("requests")
        if ren and req == 0:
            out.append(Finding(
                "IMPOSSIBLE", v.check,
                f"the page rendered {ren} characters while the request "
                f"log recorded ZERO requests — a page cannot render from "
                f"nothing",
                "the request hook is not wired; every verdict that "
                "relies on it is currently meaningless"))
    return out


def r_stale(vs, ctx):
    """Judged something older than itself.

    Measured, live: a process under test had started 33 seconds BEFORE
    the binary it was supposed to be testing. Fixes were reported as
    live while stale code was running. Timestamps settle it."""
    out = []
    for v in vs:
        a = v.artifact
        if not a or not a.exists():
            continue
        try:
            made = a.stat().st_mtime
        except OSError:
            continue
        if v.at < made - 1.0:
            out.append(Finding(
                "STALE", v.check,
                f"the verdict is older than what it judges: measured at "
                f"{time.strftime('%H:%M:%S', time.localtime(v.at))}, but "
                f"{a.name} was written at "
                f"{time.strftime('%H:%M:%S', time.localtime(made))}",
                "re-run the check against the current artifact — this "
                "result describes something that no longer exists"))
    return out


def r_broken_baseline(vs, ctx):
    """The reference was faulty, so the comparison means nothing.

    The motion gap tool compared a CORRECT port against a broken
    rendering of the original and reported the port as faulty. Whatever
    a comparison says, it says nothing at all if the thing it compared
    against was not itself sound."""
    out = []
    for v in vs:
        b = v.baseline or {}
        if not b:
            continue
        if b.get("healthy") is False:
            out.append(Finding(
                "BASELINE", v.check,
                f"the reference itself is unsound "
                f"({b.get('why', 'baseline failed its own check')}) — the "
                f"comparison is not evidence either way",
                "repair or re-capture the baseline, then compare again; "
                "a verdict against a broken reference is not a verdict"))
    return out


def r_inherited(vs, ctx):
    """Failed for a defect the baseline already had.

    A pixel-perfect Webflow port was condemned because the probe's exit
    code folded in two console errors the ORIGINAL template shipped.
    The port was blamed for faithfully reproducing a defect it was
    asked to preserve."""
    out = []
    for v in vs:
        if v.status != FAIL:
            continue
        mine = set(map(str, v.evidence.get("problems") or []))
        theirs = set(map(str, (v.baseline or {}).get("problems") or []))
        if not mine:
            continue
        shared = mine & theirs
        if shared and shared == mine:
            out.append(Finding(
                "INHERITED", v.check,
                f"every problem it failed on is ALSO present in the "
                f"baseline ({len(shared)} of {len(mine)}): "
                f"{', '.join(sorted(shared))[:120]}",
                "this is inherited, not caused — judge on the difference "
                "from the baseline, never on an absolute exit code",
                severity="misattribution"))
        elif shared:
            out.append(Finding(
                "INHERITED", v.check,
                f"{len(shared)} of {len(mine)} problems are inherited "
                f"from the baseline and are not this change's fault",
                "separate inherited defects from introduced ones before "
                "reporting a failure",
                severity="note"))
    return out


def r_unfalsifiable(vs, ctx):
    """A check with nothing it could have failed on.

    A verify that scans zero forbidden words will always pass. A
    referee with no reference always agrees. These are not passing —
    they are incapable of failing, which is a different thing wearing
    the same word."""
    out = []
    for v in vs:
        if v.status != PASS:
            continue
        crit = v.evidence.get("criteria_checked")
        if crit is not None and crit == 0:
            out.append(Finding(
                "UNFALSIFIABLE", v.check,
                "passed with zero criteria to test — it had no way to "
                "fail, so passing carries no information",
                "give it something falsifiable, or report SKIPPED"))
    return out


def r_uncheckable(vs, ctx):
    """A PASS that names no artifact cannot be tested for staleness.

    Not a lie by itself — but omitting the field is how a verdict
    becomes permanently unfalsifiable by the STALE rule, and that is
    worth saying out loud rather than passing over."""
    out = []
    for v in vs:
        if v.status == PASS and v.artifact is None:
            out.append(Finding(
                "UNCHECKABLE", v.check,
                "passed without naming what it judged, so it can never "
                "be caught measuring a stale artifact",
                "record the artifact the verdict is about; a claim with "
                "no subject cannot be checked against one",
                severity="note"))
    return out


RULES = [r_vacuous, r_status, r_silent, r_contradiction, r_impossible,
         r_stale, r_broken_baseline, r_inherited, r_unfalsifiable,
         r_uncheckable]


def audit(verdicts, ctx=None):
    """-> [Finding]. Deterministic; no model, no guessing."""
    ctx = ctx or {}
    out = []
    for rule in RULES:
        try:
            out.extend(rule(verdicts, ctx) or [])
        except Exception as e:
            # An auditor that dies on one rule silently stops auditing.
            out.append(Finding("AUDITOR", rule.__name__,
                               f"this rule crashed: {type(e).__name__}: {e}",
                               "the auditor is held to its own standard — "
                               "fix the rule; until then it is not "
                               "checking anything"))
    return out


def trust(verdicts, ctx=None):
    """The whole point, in one call.

    -> {trustworthy, findings, downgraded}. `downgraded` lists checks
    whose PASS the auditor refuses to accept — they are reported as
    SKIPPED, because 'not proven good' is the honest word for them."""
    findings = audit(verdicts, ctx)
    kill = {"VACUOUS", "SILENT", "UNFALSIFIABLE", "BASELINE", "STALE",
            "MASKED", "IMPOSSIBLE", "UNKNOWN"}
    downgraded = sorted({f.check for f in findings if f.rule in kill})
    # A NOTE IS INFORMATION, NOT A DISQUALIFICATION. trustworthy used to
    # be `not findings`, so adding UNCHECKABLE — which fires on any PASS
    # that names no artifact, i.e. most of them — instantly made every
    # honest verdict untrustworthy. A rule that flags everything is a
    # rule people learn to ignore, and this project has paid for that
    # once already with the probe.
    serious = [f for f in findings if f.severity != "note"]
    return {"trustworthy": not serious,
            "findings": findings,
            "serious": serious,
            "notes": [f for f in findings if f.severity == "note"],
            "downgraded": downgraded}


# ─────────────────────── adapters for what exists ────────────────────
# Retrofitting, not rewriting: the tools already emit these numbers.

def from_probe(report_path, artifact=None):
    """site/.forge-probe.json -> Verdict."""
    p = Path(report_path)
    if not p.is_file():
        return Verdict("probe", SKIPPED, instrument={"why": "no report"})
    d = json.loads(p.read_text())
    pages = d if isinstance(d, list) else (d.get("pages") or [])
    reqs = sum(int(x.get("requests") or 0) for x in pages)
    failed = sum(len(x.get("failed_requests") or []) for x in pages)
    chars = sum(int(x.get("rendered") or 0) for x in pages)
    return Verdict(
        "probe", FAIL if failed else PASS,
        work={"pages": len(pages), "requests": reqs},
        evidence={"failed_requests": failed, "rendered_chars": chars,
                  "requests": reqs,
                  "__bounds__": {"failed_requests": (failed, 0, 1e9)}},
        measures={"pages_rendered": len(pages)},
        artifact=artifact, at=p.stat().st_mtime)


def from_forge(check, exit_code, output, artifact=None):
    """A forge command's output -> Verdict, reading its VERDICT line.

    The exit code alone is not the answer and never was: `probe` exits
    0 when it reports SKIPPED, so a run with no browser installed
    looked identical to a clean one. That is the difference between
    'the site is fine' and 'nobody looked'."""
    out = output or ""
    m = re.search(r"^VERDICT:\s*(.+)$", out, re.M)
    verdict = (m.group(1).strip() if m else "")
    up = verdict.upper()
    if not m:
        status = SKIPPED          # no verdict line is not a pass
    elif up.startswith("SKIPPED") or "UNVERIFIED" in up:
        status = SKIPPED
    elif exit_code == 0 and not up.startswith("FAIL"):
        status = PASS
    else:
        status = FAIL
    work = {}
    for label, pat in (("pages", r"(\d+)\s+page\(s\)"),
                       ("requests", r"(\d+)\s+runtime request"),
                       ("chunks", r"(\d+)\s+chunk\(s\)")):
        mm = re.search(pat, out)
        if mm:
            work[label] = int(mm.group(1))
    if not work:
        work = {"output_lines": len(out.splitlines())}
    problems = re.findall(r"^(?:FAIL|PROBLEM)\s+(.{0,90})", out, re.M)
    return Verdict(
        check, status, work=work,
        evidence={"problems": problems,
                  "criteria_checked": len(re.findall(r"^(PASS|FAIL)\b",
                                                     out, re.M)),
                  "verdict_line": verdict},
        instrument={"exit_code": exit_code, "output_bytes": len(out)},
        artifact=artifact, raw=out[:4000])


def from_process(check, exit_code, output, artifact=None,
                 through_pipe=False, verdict_re=r"VERDICT[: ]"):
    """A subprocess check -> Verdict, WITHOUT trusting the exit code."""
    out = output or ""
    has_verdict = bool(re.search(verdict_re, out))
    status = PASS if (exit_code == 0 and has_verdict) else (
        FAIL if exit_code != 0 else SKIPPED)
    return Verdict(
        check, status,
        work={"output_lines": len(out.splitlines())},
        evidence={"has_verdict_line": has_verdict},
        instrument={"exit_code": exit_code, "output_bytes": len(out),
                    "through_pipe": through_pipe},
        artifact=artifact, raw=out[:4000])


# ─────────────────────────── selftest ────────────────────────────────

def _selftest():
    ok = True

    def c(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'ok  ' if cond else 'FAIL'} {name}"
              + (f"   {detail}" if not cond and detail else ""))

    def rules_of(vs):
        """Serious findings only. Notes (UNCHECKABLE) are advice, and
        counting them here would mean every clean case 'fails'."""
        return {f.rule for f in audit(vs) if f.severity != "note"}

    print("── it catches a check that did nothing")
    c("PASS with no work is VACUOUS",
      "VACUOUS" in rules_of([Verdict("verify", PASS)]))
    c("PASS with all-zero counts is VACUOUS",
      "VACUOUS" in rules_of([Verdict("verify", PASS,
                                     work={"files": 0, "pages": 0})]))
    c("PASS with real work is accepted",
      not rules_of([Verdict("verify", PASS, work={"files": 12})]))

    print("── it catches the hang that exits 0")
    c("exit 0 with no output is SILENT",
      "SILENT" in rules_of([Verdict("healer", PASS, work={"lines": 1},
                                    instrument={"exit_code": 0,
                                                "output_bytes": 0})]))
    c("an exit code read through a pipe is MASKED",
      "MASKED" in rules_of([Verdict("suite", PASS, work={"n": 3},
                                    instrument={"exit_code": 0,
                                                "output_bytes": 99,
                                                "through_pipe": True})]))

    print("── it catches two instruments disagreeing")
    two = [Verdict("word_overlap", PASS, work={"pages": 8},
                   measures={"template_remaining": 0.55}),
           Verdict("entry_scan", PASS, work={"entries": 314},
                   measures={"template_remaining": 0.02})]
    c("55% vs 2% on the same property is a CONTRADICTION",
      "CONTRADICTION" in rules_of(two))
    close = [Verdict("a", PASS, work={"n": 1}, measures={"x": 0.50}),
             Verdict("b", PASS, work={"n": 1}, measures={"x": 0.52})]
    c("instruments that agree are not flagged", not rules_of(close))

    print("── it catches values that cannot be true")
    c("rendered text with zero requests is IMPOSSIBLE",
      "IMPOSSIBLE" in rules_of([Verdict(
          "probe", PASS, work={"pages": 1},
          evidence={"rendered_chars": 5300, "requests": 0})]))
    c("a value outside its declared bounds is IMPOSSIBLE",
      "IMPOSSIBLE" in rules_of([Verdict(
          "grade", PASS, work={"px": 10},
          evidence={"__bounds__": {"identical": (1.4, 0.0, 1.0)}})]))

    print("── it catches a verdict older than what it judges")
    import tempfile
    t = Path(tempfile.mkdtemp()) / "site.html"
    t.write_text("x")
    old = Verdict("verify", PASS, work={"files": 3}, artifact=t,
                  at=t.stat().st_mtime - 600)
    c("a stale measurement is STALE", "STALE" in rules_of([old]))
    fresh = Verdict("verify", PASS, work={"files": 3}, artifact=t,
                    at=t.stat().st_mtime + 5)
    c("a current measurement is fine", not rules_of([fresh]))

    print("── it catches a broken reference")
    c("comparing against an unsound baseline is BASELINE",
      "BASELINE" in rules_of([Verdict(
          "gap", FAIL, work={"elements": 63},
          baseline={"healthy": False,
                    "why": "the original never finished rendering"})]))

    print("── it catches a defect blamed on the wrong thing")
    inh = [Verdict("port", FAIL, work={"pages": 4},
                   evidence={"problems": ["console error A",
                                          "console error B"]},
                   baseline={"problems": ["console error A",
                                          "console error B"]})]
    c("failing only on the baseline's own defects is INHERITED",
      "INHERITED" in rules_of(inh))
    mine = [Verdict("port", FAIL, work={"pages": 4},
                    evidence={"problems": ["A", "NEW"]},
                    baseline={"problems": ["A"]})]
    c("a genuinely new problem is not excused",
      all(f.severity != "misattribution" for f in audit(mine)))

    print("── it catches a check that could not have failed")
    c("zero criteria means UNFALSIFIABLE",
      "UNFALSIFIABLE" in rules_of([Verdict(
          "forbidden_words", PASS, work={"files": 9},
          evidence={"criteria_checked": 0})]))

    print("── it is held to its own standard")
    broken = lambda vs, ctx: 1 / 0                       # noqa: E731
    RULES.append(broken)
    try:
        c("a rule that crashes is REPORTED, not swallowed",
          "AUDITOR" in rules_of([Verdict("x", PASS, work={"n": 1})]))
    finally:
        RULES.remove(broken)

    print("── trust() downgrades what it cannot accept")
    r = trust([Verdict("verify", PASS)])
    c("an unearned PASS is downgraded",
      r["downgraded"] == ["verify"] and not r["trustworthy"], str(r))
    r2 = trust([Verdict("verify", PASS, work={"files": 12},
                        evidence={"criteria_checked": 4})])
    c("an earned PASS is trusted", r2["trustworthy"], str(r2))

    print("\naudit selftest:", "ok" if ok else "FAILED")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
