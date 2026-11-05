#!/usr/bin/env python3
"""Every check Aethron can run on itself, in one command.

    python3 tests/run_all.py            everything
    python3 tests/run_all.py --quick    skip the browser/CLI batteries

Nothing here needs an API key, a login or a network: the model-facing
paths are proved against in-process mock providers, and the runtime
paths against a real headless browser if one is installed (and honestly
SKIPPED if not).
"""
import os
import re
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
                "aethron_web.py", "aethron_eye.py", "aethron_design.py",
                "aethron_build.py", "aethron_flow.py",
                "aethron_generate.py", "aethron_gradient.py",
                "aethron_surface.py", "aethron_replicate.py",
                "aethron_preview.py", "aethron_jobs.py", "aethron_fill.py",
                "tests/job_battery.py", "aethron_share.py", "tests/share_battery.py", "aethron_locate.py", "tests/locate_battery.py", "tests/cache_battery.py", "tests/tools_only_battery.py",
                "aethron_change.py", "tests/change_battery.py", "tests/interact_battery.py",
                "aethron_spec.py", "tests/spec_battery.py", "tests/gemini_ring_battery.py",
                "aethron_agm.py", "tests/agm_battery.py", "aethron_adopt.py",
                "tests/adopt_battery.py",
                "tests/alive_battery.py", "tests/engine_battery.py",
                "tests/provider_battery.py", "tests/openai_battery.py",
                "aethron_usage.py", "tests/routing_battery.py",
                "tests/preview_battery.py",
                "tests/remote_battery.py", "aethron_runlog.py",
                "tests/keeper_battery.py",
                "aethron_news.py", "tests/news_battery.py", "tests/image_battery.py",
                "aethron_ember.py", "tests/ember_battery.py", "aethron_desktop.py", "aethron_voice.py",
                "tests/stall_battery.py", "tests/conversation_battery.py",
                "tests/page_preview_battery.py",
                "aethron_shot2code.py", "tests/shot2code_battery.py",
                "tests/page_resume_battery.py",
                "aethron_frame.py", "tests/frame_battery.py",
                "aethron_fit.py", "aethron_pictures.py", "tests/pictures_fit_battery.py",
                "aethron_native.py", "tests/drop_battery.py",
                "aethron_publish.py", "tests/publish_battery.py", "tests/hosting_battery.py",
                "aethron_control.py", "tests/control_battery.py",
                "tests/admin_console_battery.py", "tests/team_ui_battery.py",
                "aethron_browser.py", "tests/browser_battery.py", "aethron_templates.py",
                "tests/run_all.py"], False),
    # The replacement engine may change COPY and nothing else: a fill of
    # "me" -> "us" once renamed variables inside Framer's runtime.
    ("browser selftest (open, click, new tabs, type, scroll, back)",
     [PY, "aethron_browser.py", "--selftest"], False),
    ("template finder selftest (free Framer/Webflow templates read from saved pages)",
     [PY, "aethron_templates.py", "--selftest"], False),
    # "the button goes to my contact page" is proven by pressing it
    ("browser battery (a widget's link checked, fixed and re-checked)",
     [PY, "tests/browser_battery.py"], False),
    ("engine battery (copy changes, code and addresses do not)",
     [PY, "tests/engine_battery.py"], False),
    # what the uploaded failure reports showed, held as checks
    ("field-log battery (faults from real migrations)",
     [PY, "tests/fieldlog_battery.py"], False),
    # Saved providers: a switch changes what runs, and nothing else.
    ("provider battery (one-click switch, no Gemini left behind)",
     [PY, "tests/provider_battery.py"], False),
    # The selected provider is the one called; side jobs on another
    # provider's key only when allowed; every request recorded by address.
    ("routing battery (the selected model is the one that runs)",
     [PY, "tests/routing_battery.py"], False),
    ("model-use record", [PY, "aethron_usage.py", "--selftest"], False),
    ("MCP Studio lock", [PY, "aethron_access.py", "--selftest"], False),
    ("failure reports", [PY, "aethron_reports.py", "--selftest"], False),
    ("publish to Netlify", [PY, "aethron_publish.py", "--selftest"], False),
    ("publish battery (the dialog, end to end, against a fake Netlify)",
     [PY, "tests/publish_battery.py"], False),
    ("sites publish to <name>.aethron.site (the real Worker, fake storage)",
     [PY, "tests/hosting_battery.py"], False),
    # The admin: the edge function (bun), the app obeying its switches, and
    # the console in a browser — all against a stand-in Supabase.
    ("admin edge function (bun test)", [os.path.expanduser("~/.bun/bin/bun") if os.path.exists(
        os.path.expanduser("~/.bun/bin/bun")) else "bun", "test", "tests/cloud"], False),
    ("control battery (kill switch, holds, sign-out, managed AI, offline)",
     [PY, "tests/control_battery.py"], False),
    ("free-plan quota (no function call per minute)",
     [PY, "tests/quota_battery.py"], False),
    ("admin console battery (real browser, real function)",
     [PY, "tests/admin_console_battery.py"], False),
    ("team messages in the app (paused card, chip, announcement card)",
     [PY, "tests/team_ui_battery.py"], False),
    # A tripped guard is said, not retried in silence; the cost shown is
    # the provider's own; a thinking model keeps the stream alive.
    ("guard battery (no silent refusals, honest cost)",
     [PY, "tests/guard_battery.py"], False),
    # Limits the person sets (dollars or Unlimited), a session window that
    # resets, the run clock, and where a run's time went.
    ("limits battery (spend guard, session window, run clock)",
     [PY, "tests/limits_battery.py"], False),
    # A run that outgrew the model: trimmed before the wall, and a size
    # refusal survived instead of ending every later message.
    ("context battery (trim before the wall, survive a size refusal)",
     [PY, "tests/context_battery.py"], False),
    # OpenAI: gpt-4o-mini and gpt-5 accepted, honest prices, readable refusals.
    ("openai battery (gpt-4o-mini keeps working)",
     [PY, "tests/openai_battery.py"], False),
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
    # ANY change in the user's words, written as code — and kept only when every claim
    # the model makes is measured true and matches the words it was given.
    ("change (claims held to the request, offline)",
     [PY, "aethron_change.py", "--selftest"], False),
    ("change battery (honest replies land, lying ones are refused)",
     [PY, "tests/change_battery.py"], True),
    # Something that OPENS is used like a person uses it — hovered, reached, left, clicked,
    # Escaped — and must not break the button it hangs from or cover the one beside it.
    ("interact battery (does a pop-up work for a person?)",
     [PY, "tests/interact_battery.py"], True),
    # A request nobody built a check for: tests first, shown failing today, reviewed blind,
    # then code every test must pass and every part of which must be needed.
    ("spec (tests held to the request, offline)",
     [PY, "aethron_spec.py", "--selftest"], False),
    ("spec battery (a change nobody wired lands; fakes are refused)",
     [PY, "tests/spec_battery.py"], True),
    ("gemini ring battery (free keys first, no quota read as no credit)",
     [PY, "tests/gemini_ring_battery.py"], False),
    # The Automatic General Measure: record everything, diff everything, every difference must be
    # explained by the request — including kinds of change nobody wrote a check for.
    ("agm (every difference filed and explained, offline)",
     [PY, "aethron_agm.py", "--selftest"], False),
    ("agm battery (finds what no purpose-built check could)",
     [PY, "tests/agm_battery.py"], True),
    # Every measured path reads [data-ae-id], and Aethron stamped those only on pages it
    # built itself — so a user's own site was unreadable and therefore unchangeable.
    ("adopt (any page becomes measurable, offline)",
     [PY, "aethron_adopt.py", "--selftest"], False),
    ("adopt battery (and ids on the WRONG elements change no pixels)",
     [PY, "tests/adopt_battery.py"], True),
    # The owner's correction: a rebuild that matches a screenshot
    # perfectly has faithfully reproduced its blur and its complete
    # absence of behaviour. This asks whether the output is a WEBSITE.
    ("web (is it a website, or a picture of one?)",
     [PY, "aethron_web.py", "--selftest"], False),
    ("screen (one page, six frameworks, one description)",
     [PY, "aethron_screen.py", "--selftest"], False),
    ("build battery (does the loop refuse, and keep the best?)",
     [PY, "tests/build_battery.py"], True),
    ("design battery (does each design rule actually fire?)",
     [PY, "tests/design_battery.py"], True),
    # Taste rules refuse generic design on the rendered page instead of
    # asking the model to grade itself — and must stay silent on real
    # design and while cloning, or they do more harm than good.
    ("taste battery (refuses slop, and only slop)",
     [PY, "tests/taste_battery.py"], True),
    ("eye battery (can the referee see, and can it be fooled?)",
     [PY, "tests/eye_battery.py"], True),
    ("flow battery (a poster becomes a website, both halves)",
     [PY, "tests/flow_battery.py"], True),
    ("edit battery (is collateral damage actually noticed?)",
     [PY, "tests/edit_battery.py"], True),
    # A background that moves is only a feature if frame 0 is still the
    # fitted design and the motion is measured, not assumed.
    ("alive battery (does the background move, and rest on the design?)",
     [PY, "tests/alive_battery.py"], True),
    ("probe battery (runtime + framework-port referee)",
     [PY, "tests/probe_battery.py"], True),
    # A REGULAR site, not a template: its own stylesheet, data drawn from
    # its API after the page opens, a chunk whose name is computed, a
    # private token and a POST-only form. Copied what belongs, recorded
    # the data, refused the secret, named what needs a server.
    ("site battery (any website, taken from its URL)",
     [PY, "tests/site_battery.py"], True),
    # A page that starts hidden and is revealed by a script whose address
    # the page builds in code; the brand fill renames that address. The
    # script must be owned, the filled page painted, an old project healed
    # on its next build, and the probe must name the script when it fails.
    ("reveal battery (a brand fill must not blank the page)",
     [PY, "tests/reveal_battery.py"], True),
    # A personalised preview for one lead: only the chosen pages, extras
    # placed, checked in a browser AT /preview/<slug> — where every
    # root-absolute address would otherwise 404.
    ("preview battery (a lead's site at /preview/<slug>)",
     [PY, "tests/preview_battery.py"], True),
    # The same tools for an agent that is NOT on this computer: over HTTP,
    # behind a token, the agent choosing its own pages, and the preview
    # served from the same address and rendered in a browser.
    ("remote battery (Aethron over HTTP for a hosted agent)",
     [PY, "tests/remote_battery.py"], True),
    # Run records: every agent that drives Aethron, what it called and
    # what Aethron answered — no secret ever written.
    ("run records (who called what, what came back)",
     [PY, "aethron_runlog.py", "--selftest"], False),
    # A keeper-like agent loses a hero image: Aethron refuses the dead
    # address, FAILs the preview when a picture dies later, and the run
    # record holds both as findings.
    # What's new: once after an update, and every button goes where it says.
    ("what's new (selftest)", [PY, "aethron_news.py", "--selftest"], False),
    ("what's new card (real browser)", [PY, "tests/news_battery.py"], False),
    # Ember, the companion: real signals, never steals a click, Savage for admins only.
    ("companion (selftest)", [PY, "aethron_ember.py", "--selftest"], False),
    ("companion in the window (real browser)", [PY, "tests/ember_battery.py"], False),
    ("companion on the desktop (selftest)", [PY, "aethron_desktop.py", "--selftest"], False),
    ("companion listens (selftest)", [PY, "aethron_voice.py", "--selftest"], False),
    ("attached images are seen (engine + browser)", [PY, "tests/image_battery.py"], False),
    ("a stalled provider is retried, the run carries on", [PY, "tests/stall_battery.py"], False),
    ("a migration runs as one background job and survives being killed",
     [PY, "tests/job_battery.py"], False),
    ("clear requests are actions; a project chat works through tools only",
     [PY, "tests/tools_only_battery.py"], False),
    ("nothing is done twice: build, verify, probe remember their answer",
     [PY, "tests/cache_battery.py"], False),
    ("the link preview shows the migrated site, not the template's",
     [PY, "tests/share_battery.py"], False),
    ("a screenshot of part of the site is found on the site",
     [PY, "tests/locate_battery.py"], False),
    ("the Home chat: New, the projects rail, a reconnect that continues",
     [PY, "tests/chatui_battery.py"], False),
    ("a conversation survives a restart, at Home and in its project",
     [PY, "tests/conversation_battery.py"], False),
    ("a rebuilt page opens in the preview (tool + browser)",
     [PY, "tests/page_preview_battery.py"], False),
    ("a screenshot becomes real code, never a picture of itself",
     [PY, "tests/shot2code_battery.py"], False),
    ("one screenshot request is one page, continued, never restarted",
     [PY, "tests/page_resume_battery.py"], False),
    ("a screenshot builds the design, not its backdrop or pen marks",
     [PY, "tests/frame_battery.py"], False),
    ("type fitted from the pixels (selftest)", [PY, "aethron_fit.py", "--selftest"], False),
    ("pictures are pictures (selftest)", [PY, "aethron_pictures.py", "--selftest"], False),
    ("pictures stay pictures, placement fitted (the owner's example)",
     [PY, "tests/pictures_fit_battery.py"], False),
    ("images dropped or pasted anywhere reach the chat (routes + browser)",
     [PY, "tests/drop_battery.py"], False),
    ("history and going back", [PY, "tests/history_battery.py"], False),
    ("keeper battery (an agent's run, and what went missing)",
     [PY, "tests/keeper_battery.py"], True),
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
# A few suites are honestly long, not hung. The alive battery renders a
# moving background frame by frame: about 13 minutes on its own, and over
# 15 when it shares the machine with three other suites, so the flat
# limit failed it with every check it had reached passing.
LONG = {"tests/alive_battery.py": 1800}


def _limit(cmd):
    return max([LIMIT] + [LONG[c] for c in cmd if c in LONG])


# How much wall time may pass beyond monotonic time before a suite is
# treated as having run across a system sleep.
SLEEP_TOL = 30.0


def _run(cmd):
    """Run one suite; return (ok, output, wall seconds, seconds slept).

    time.monotonic() stops while macOS sleeps and time.time() does not,
    so the difference between them is how long the machine was asleep
    during the suite — a measurement, not a guess about flakiness.
    """
    t, m = time.time(), time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=_limit(cmd))
        out = r.stdout + r.stderr
        ok = r.returncode == 0
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode("utf-8", "replace")
               if isinstance(e.stdout, bytes) else (e.stdout or ""))
        out += f"\nTIMED OUT after {_limit(cmd)}s — no verdict, not a pass"
        ok = False
    wall = time.time() - t
    gap = wall - (time.monotonic() - m)
    return ok, out, wall, (gap if gap > SLEEP_TOL else 0.0)


def _shares_projects(cmd) -> bool:
    """Does this suite read or change the real projects/ folder?

    Those suites build, break and restore the same fixture projects
    (the probe battery corrupts a chunk and puts it back), so two of
    them at once would grade each other's damage. They run one at a
    time in their own lane; everything else runs alongside them."""
    for part in cmd[1:]:
        f = ROOT / part
        if f.suffix == ".py" and f.is_file():
            t = f.read_text(errors="ignore")
            if "projects/" in t or "PROJECTS" in t or '"projects"' in t:
                return True
    return False


def _jobs() -> int:
    for a in sys.argv:
        if a.startswith("--jobs="):
            return max(1, int(a.split("=", 1)[1]))
    return max(1, min(4, os.cpu_count() or 1))


def _grade(name, cmd):
    """Run one suite (re-running once across a sleep) and grade it.
    Returns (row, failed, unproven, line to print)."""
    ok, out, dt, slept = _run(cmd)
    if slept:
        # A RESULT THAT CROSSED A SLEEP IS NOT A RESULT. Measured
        # 2026-09-14: the lid closed at 13:42:58, two seconds before
        # the probe battery started; the Mac slept until 14:12:18.
        # The battery "took" 1,834s under a 900s timeout and never
        # timed out, because the timeout runs on a monotonic clock
        # that does not tick during sleep while this table uses wall
        # time. Its first render was in flight when the machine went
        # down, so it FAILED; alone, awake, it was 33/33. Re-run once.
        print(f"  {name}: the machine slept ~{slept:.0f}s mid-suite "
              f"— re-running once", flush=True)
        ok, out, dt2, slept = _run(cmd)
        dt += dt2
        if slept:
            return ((name, "SKIP  interrupted by system sleep twice — "
                     "not graded", dt), 0, 1,
                    f"      SKIP ({dt:.0f}s) {name} — slept twice")
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
        return ((name, ("SKIP  " + note)[:78], dt), 0, 1,
                f"      SKIP ({dt:.0f}s) {name} — {note[:60]}")
    # KEEP THE WHOLE STORY OF A FAILURE. Only the last twelve lines
    # used to reach this table, so when the probe battery failed its
    # FIRST check in a full run — and passed alone — the probe's own
    # output for that check was already gone, and the cause could
    # only be argued about, not read. The diagnosis was collected and
    # then thrown away, which is the fourth time that pattern has
    # appeared in this project's history.
    saved = ""
    if not ok:
        logdir = ROOT / "tests" / ".last-failures"
        logdir.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        (logdir / f"{slug}.log").write_text(out)
        saved = f"\n  full output: tests/.last-failures/{slug}.log"
    row = (name, ("PASS  " + summary.strip())[:78] if ok
           else ("FAIL  " + "\n".join(tail[-12:]))[:1200] + saved, dt)
    return (row, int(not ok), 0,
            f"      {'PASS' if ok else 'FAIL'} ({dt:.0f}s) {name}: "
            f"{summary.strip()[:58]}")


def main():
    import concurrent.futures as cf
    import threading
    quick = "--quick" in sys.argv
    jobs = _jobs()
    # SAY WHAT IS HAPPENING WHILE IT HAPPENS. The table was printed only at the very end, so
    # a forty-minute run produced not one line until it finished — indistinguishable from a
    # hang, which is the worst thing a test harness can look like, and the same
    # reporting-blindness this project has now recorded five times. One line per suite, and
    # one before it starts, so a stall names the suite it stalled in.
    #
    # SUITES RUN SIDE BY SIDE (--jobs=N, default up to 4). One after
    # another the full suite took over two hours, most of it browser
    # batteries waiting on renders while the other cores sat idle.
    # Suites that touch projects/ keep to one lane between them.
    todo = [(i, n, c) for i, (n, c, slow) in enumerate(SUITES)
            if not (quick and slow)]
    total, done = len(todo), [0]
    rows = {i: (n, "skipped", 0) for i, (n, c, slow) in enumerate(SUITES)
            if quick and slow}
    lock = threading.Lock()
    tally = [0, 0]
    t0 = time.time()

    def one(i, name, cmd):
        with lock:
            print(f"[start] {name} …", flush=True)
        row, f, u, line = _grade(name, cmd)
        with lock:
            done[0] += 1
            rows[i] = row
            tally[0] += f
            tally[1] += u
            print(f"[{done[0]}/{total}]{line[5:]}", flush=True)

    print(f"running {total} suites, {jobs} at a time", flush=True)
    # The projects/ lane is ONE task that runs its suites in order, so it
    # never parks pool workers waiting on each other; it is submitted
    # first because it is the longest chain.
    serial = [t for t in todo if _shares_projects(t[2])]
    free = [t for t in todo if t not in serial]
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = [ex.submit(lambda: [one(*t) for t in serial])]
        futs += [ex.submit(one, *t) for t in free]
        for f in futs:
            f.result()
    failed, unproven = tally
    rows = [rows[i] for i in sorted(rows)]
    print("\n" + "=" * 70)
    for name, res, dt in rows:
        print(f"{name:52} {dt:5.1f}s  {res}")
    print("=" * 70)
    print(f"wall time {time.time() - t0:.0f}s with {jobs} at a time")
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
