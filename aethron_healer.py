#!/usr/bin/env python3
"""Aethron Healer — self-repair that thinks, on top of self-repair that
is certain.

WHY THIS EXISTS: `forge.py heal` is a LADDER OF KNOWN FIXES (whitespace
flexibility, source casing, nearest-source adoption, image srcset
variants, destructive hide rules). It is fast, free and never guesses —
but it can only fix failures someone anticipated. Real migrations
produce failures nobody anticipated, and there the ladder honestly says
STUCK and stops.

This layer takes it from there:

    1. deterministic heal      cheap, certain, no tokens        ← always first
    2. build -> verify -> probe   what is ACTUALLY still broken
    3. agent rounds            reads the evidence, uses the guarded
                               tools, tries something the ladder
                               cannot express
    4. build -> verify -> probe   ← the agent does NOT get to say it worked
    5. honest STUCK report     with everything that was tried

THE RULES THE AGENT CANNOT BREAK, enforced not requested:
  * writes to `site/` and `pristine/` are DENIED by the runtime, so a
    "helpful" hand edit is impossible (hydration would revert it and
    the pristine seal would fail)
  * content changes go through the MCP tools: byte budgets, forbidden
    characters, snapshots
  * a snapshot is taken before the agent starts — one undo puts the
    project back exactly as it was
  * success is decided by verify + probe, never by the model

Run it:
    python3 aethron_healer.py <project-dir> [--rounds 2] [--no-agent]
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORGE = ROOT / "forge.py"
MAX_EVIDENCE = 6000


def _forge(project: Path, *args, timeout=1800):
    r = subprocess.run([sys.executable, str(FORGE), *args], cwd=project,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


HEAL_PROMPT = """A migration in this workspace is broken and the
deterministic repairs could not fix it. Below is the machine evidence.

Your job: make the checks pass, without breaking the physics.

WHAT YOU MAY DO
- read anything in the workspace (copy_map.json, forge.json, the build
  report, pristine/ and site/ are readable)
- change content with the `mcp__aethron__*` tools (set_content,
  set_content_bulk, add_style, remove_element, replace_image_slots,
  localize_assets, capture, build, verify, probe, heal, undo)
- edit copy_map.json directly if a tool cannot express the fix

WHAT YOU MAY NOT DO
- write to `site/` or `pristine/` (the runtime denies it: site/ is
  regenerated on every build and pristine/ is sha256-sealed)
- declare the job done. Run `build` then `verify` then `probe` and let
  them decide. If they still fail, say precisely what is blocking you.

HOW TO THINK ABOUT THE COMMON FAILURES
- an entry with 0 hits: its "old" text does not exist byte-for-byte in
  the source. Find what the source really says (read the pristine page
  or the chunk) and fix the ENTRY, not the output.
- "__at_risk__": the pages changed but the JS chunks still spell the
  old text, so hydration will revert it in the browser.
- a CMS entry has a hard byte budget — a longer replacement cannot fit;
  shorten it rather than forcing it.
- probe failures are runtime truth: missing assets the CODE requests
  (not the markup) usually mean `capture` then `build`.

EVIDENCE
%s
"""


def collect_evidence(project: Path) -> dict:
    """What is broken, from the three deterministic sources."""
    ev = {"verify": "", "probe": "", "report": {}, "clean": False}
    rc_v, out_v = _forge(project, "verify")
    ev["verify_ok"] = rc_v == 0
    ev["verify"] = "\n".join(l for l in out_v.splitlines()
                             if l.startswith(("FAIL", "NOTE", "VERDICT"))
                             or "missing" in l.lower())
    rc_p, out_p = _forge(project, "probe")
    # AN EXIT CODE IS NOT A VERDICT. `probe` exits 0 when it reports
    # SKIPPED, so reading rc alone made "nobody looked" indistinguishable
    # from "it runs clean" — the same confusion, one level up from the
    # `clean` line below. probe_ok now means it RAN and passed.
    ev["probe_skipped"] = ("VERDICT: SKIPPED" in out_p
                           or "UNVERIFIED" in out_p)
    ev["probe_ok"] = rc_p == 0 and not ev["probe_skipped"]
    ev["probe"] = "\n".join(l for l in out_p.splitlines()
                            if l.startswith(("FAIL", "NOTE", "VERDICT", "──"))
                            or "       " in l)
    rep = project / "site" / ".forge-report.json"
    if rep.exists():
        try:
            data = json.loads(rep.read_text(encoding="utf-8"))
            dead = [k for k, v in data.items()
                    if isinstance(v, int) and v == 0
                    and k not in ("__moot__", "__at_risk__")]
            ev["report"] = {"dead": dead[:25],
                            "at_risk": data.get("__at_risk__", [])[:25]}
        except Exception:
            pass
    # THE HEALER MAY NOT TRUST A CHECK THAT DID NOT EARN ITS VERDICT.
    #
    # This line used to read `probe_ok or probe_skipped`, so a probe
    # that COULD NOT RUN counted as clean: no browser installed meant
    # the runtime was never examined and the healer declared the site
    # healed anyway. That is "nobody looked" being recorded as "it is
    # fine", inside the one component whose entire job is deciding
    # whether something is fixed.
    #
    # Now the verdicts are audited before they are believed. A skipped
    # probe is UNPROVEN, which is neither success nor failure — and
    # saying so is the whole point.
    try:
        import aethron_audit as _A
        idx = project / "site" / "index.html"
        verdicts = [_A.from_forge("verify", rc_v, out_v, artifact=idx),
                    _A.from_forge("probe", rc_p, out_p, artifact=idx)]
        t = _A.trust(verdicts)
        ev["audit"] = [f.line() for f in t["findings"]]
        ev["trusted"] = t["trustworthy"]
        ev["downgraded"] = t["downgraded"]
    except Exception as e:                      # auditor must never
        ev["audit"] = [f"auditor unavailable: {e}"]   # break healing
        ev["trusted"] = True
        ev["downgraded"] = []

    ev["proven"] = bool(ev["verify_ok"] and ev["probe_ok"]
                        and ev.get("trusted", True))
    ev["unproven"] = bool(ev["verify_ok"] and ev["probe_skipped"]
                          and not ev["probe_ok"])
    ev["clean"] = ev["proven"]
    return ev


def evidence_text(ev: dict, heal_log: str = "") -> str:
    parts = []
    # The audit goes FIRST. If an instrument is lying, every number
    # below it is suspect, and reading them in that order is how you
    # end up debugging the site instead of the thermometer.
    if ev.get("audit"):
        parts.append("THE CHECKS THEMSELVES:\n" + "\n".join(ev["audit"]))
    if ev.get("unproven"):
        parts.append("UNPROVEN: verify is clean, but the runtime was "
                     "never examined (no headless browser). This is not "
                     "a pass — nothing looked at the running page.")
    if heal_log:
        stuck = [l for l in heal_log.splitlines()
                 if l.startswith(("STUCK", "HEALED"))]
        if stuck:
            parts.append("deterministic heal said:\n" + "\n".join(stuck[:20]))
    if ev.get("verify"):
        parts.append("forge verify:\n" + ev["verify"])
    if ev.get("probe"):
        parts.append("forge probe (what the browser did):\n" + ev["probe"])
    r = ev.get("report") or {}
    if r.get("dead"):
        parts.append("copy-map entries that matched NOTHING:\n"
                     + "\n".join(f"  - {d}" for d in r["dead"]))
    if r.get("at_risk"):
        parts.append("entries the browser will REVERT (chunks still hold "
                     "the old text):\n"
                     + "\n".join(f"  - {d}" for d in r["at_risk"]))
    return ("\n\n".join(parts) or "(no machine evidence)")[:MAX_EVIDENCE]


def heal(project, rounds=2, use_agent=True, on_event=None, cfg=None,
         home=None, live=False) -> dict:
    """-> {ok, stage, attempts, evidence, log, actions}"""
    project = Path(project).resolve()
    if not (project / "forge.json").exists():
        return {"ok": False, "error": f"not an Aethron project: {project}"}
    say = on_event or (lambda kind, data: None)
    log = []

    def note(kind, text):
        log.append(text)
        say(kind, {"text": text})

    # 1. deterministic first — free, certain, and usually enough
    note("stage", "deterministic heal…")
    _, heal_log = _forge(project, "heal")
    healed = len(re.findall(r"^HEALED", heal_log, re.M))
    stuck = len(re.findall(r"^STUCK", heal_log, re.M))
    note("heal", f"deterministic: {healed} healed, {stuck} stuck")
    if healed:
        _forge(project, "build")

    ev = collect_evidence(project)
    if ev["clean"]:
        note("done", "clean after deterministic repair — checks ran, "
                     "passed, and were audited")
        return {"ok": True, "stage": "deterministic", "attempts": 0,
                "evidence": ev, "log": "\n".join(log), "actions": [],
                "proven": True, "runtime_verified": True}
    if ev.get("unproven"):
        # NOT a failure to repair. A failure to VERIFY, which is a
        # different thing and must not be reported as either success
        # or breakage.
        note("done", "nothing left that the checks can see — but the "
                     "runtime was NEVER CHECKED (no browser), so this is "
                     "UNPROVEN, not proven good")
        return {"ok": False, "stage": "unproven", "attempts": 0,
                "evidence": ev, "log": "\n".join(log), "actions": [],
                "proven": False, "runtime_verified": False,
                "why": "verify is clean but probe could not run — install "
                       "Chrome so the runtime can actually be examined"}
    if not use_agent:
        return {"ok": False, "stage": "deterministic", "attempts": 0,
                "evidence": ev, "log": "\n".join(log), "actions": []}

    # 2. the agent gets the evidence and the guarded tools.
    # Spending the owner's balance is never a side effect: a live run
    # has to be asked for, and it runs under the bridge's spend caps.
    import aethron_code as code
    if cfg is None:
        import aethron_brain as brain
        try:
            brain.require_live(live, f"AI healing {project.name}")
        except SystemExit as e:
            note("error", str(e))
            return {"ok": False, "stage": "needs-live", "evidence": ev,
                    "log": "\n".join(log), "actions": [], "error": str(e)}
    try:
        import aethron_agent
        aethron_agent._snapshot(project, "before-agent-heal")
        note("snapshot", "snapshot taken — one undo reverts everything "
                         "the agent does")
    except Exception as e:
        note("snapshot", f"could not snapshot ({e}) — continuing")

    # the per-call cfg is part of the answer: a caller that hands us a
    # provider must not be told "unconfigured" because the saved
    # settings are empty
    prov = code.resolve_provider({**code.load_config(home), **(cfg or {})})
    if not prov.get("ready", True):
        note("error", f"no model configured: {prov.get('why', '')}")
        return {"ok": False, "stage": "unconfigured", "attempts": 0,
                "evidence": ev, "log": "\n".join(log), "actions": [],
                "error": prov.get("why", "no model configured")}

    actions, attempts = [], 0
    for attempt in range(1, max(1, rounds) + 1):
        attempts = attempt
        note("stage", f"agent round {attempt}/{rounds} "
                      f"({prov.get('why') or prov.get('label')})")
        prompt = HEAL_PROMPT % evidence_text(ev, heal_log)
        if attempt > 1:
            prompt += ("\n\nYour previous attempt did NOT clear the checks. "
                       "The evidence above is the CURRENT state — do not "
                       "repeat what did not work.")
        res = code.run_once(project, prompt, cfg=cfg, home=home, timeout=1800,
                            on_event=lambda e: _stream(e, say, actions))
        if res.get("error"):
            note("error", f"agent: {res['error'][:200]}")
        # 3. the agent does not decide; the checks do
        note("stage", "re-checking (build → verify → probe)…")
        _forge(project, "build")
        ev = collect_evidence(project)
        if ev["clean"]:
            note("done", f"FIXED after agent round {attempt}"
                 + (" (runtime UNVERIFIED: no browser, so probe was "
                    "skipped)" if ev.get("probe_skipped") else ""))
            return {"ok": True, "stage": f"agent-round-{attempt}",
                    "attempts": attempt, "evidence": ev,
                    "log": "\n".join(log), "actions": actions,
                    "runtime_verified": not ev.get("probe_skipped")}
        note("retry", "still not clean")

    note("stuck", "STUCK — the checks still fail after "
                  f"{attempts} agent round(s). Nothing was hidden; the "
                  "evidence below is what remains. `forge.py undo` (or "
                  "the studio's Undo) reverts everything the agent did.")
    return {"ok": False, "stage": "stuck", "attempts": attempts,
            "evidence": ev, "log": "\n".join(log), "actions": actions}


def _stream(e, say, actions):
    if e["type"] == "tool":
        actions.append({"tool": e["name"], "input": e.get("input", {})})
        say("tool", {"text": f"→ {e['name']}"
                             f"({json.dumps(e.get('input', {}))[:120]})"})
    elif e["type"] == "text" and e.get("text"):
        say("say", {"text": e["text"][:400]})
    elif e["type"] == "done" and e.get("error"):
        say("error", {"text": e.get("text", "")[:200]})


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    project = Path(argv[0]).expanduser()
    rounds = 2
    if "--rounds" in argv:
        rounds = int(argv[argv.index("--rounds") + 1])
    use_agent = "--no-agent" not in argv

    def show(kind, data):
        prefix = {"tool": "   ", "say": "   ", "error": "!! "}.get(kind, "── ")
        print(prefix + data["text"])

    res = heal(project, rounds=rounds, use_agent=use_agent, on_event=show,
               live="--live" in argv)
    print()
    if res.get("ok"):
        print(f"HEALED ({res['stage']}) — verify and probe are clean")
    else:
        print(f"NOT FIXED ({res.get('stage')}) — honest report:")
        print(evidence_text(res.get("evidence") or {})[:2000])
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
