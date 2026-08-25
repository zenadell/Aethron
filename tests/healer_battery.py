#!/usr/bin/env python3
"""Battery for the AGENTIC healer (aethron_healer.py).

It breaks a real project the way real migrations break, then proves:

  * the deterministic ladder is tried first and, when it cannot fix the
    problem, says so instead of pretending
  * the agent gets the machine evidence and repairs it through the
    GUARDED tools
  * the agent cannot write to site/ or pristine/ even when it tries
  * success is decided by verify + probe, never by the model
  * a snapshot exists, so one undo reverts everything the agent did

No API key: the model is an in-process mock speaking the Anthropic wire,
driving the REAL Claude Code CLI and the REAL forge pipeline.

    python3 tests/healer_battery.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "projects/acme-demo"

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   {detail}" if not cond and detail else ""))


def forge(project, *args):
    r = subprocess.run([sys.executable, str(ROOT / "forge.py"), *args],
                       cwd=project, capture_output=True, text=True,
                       timeout=1800)
    return r.returncode, r.stdout + r.stderr


def main():
    import aethron_code as code
    import aethron_healer as healer

    if not (SOURCE / "site").is_dir():
        print(f"SKIPPED — {SOURCE} is not built")
        return 0
    if not code.find_claude():
        print("SKIPPED — the `claude` CLI is not installed")
        return 0

    home = Path(tempfile.mkdtemp(prefix="healer-home-"))
    proj = home / "projects" / "broken"
    proj.parent.mkdir(parents=True)
    print("copying the project…")
    shutil.copytree(SOURCE, proj)

    # ── break it the way a real migration breaks: the brand token was
    # never filled, so the old brand survives into the built site.
    cm_f = proj / "copy_map.json"
    cm = json.loads(cm_f.read_text(encoding="utf-8"))
    for e in cm["strings"]:
        if e["old"].lower() == "handgrid":
            e["new"] = ""
    cm_f.write_text(json.dumps(cm, indent=1, ensure_ascii=False),
                    encoding="utf-8")
    forge(proj, "build")
    rc, out = forge(proj, "verify")
    check("the broken project fails verify", rc != 0
          and "leftover 'HandGrid'" in out)

    print("\n── the deterministic ladder alone")
    res = healer.heal(proj, use_agent=False)
    check("deterministic heal cannot fix it", not res["ok"])
    check("and says so honestly (no false success)",
          res["stage"] == "deterministic" and not res["evidence"]["clean"])
    check("evidence names the leftover",
          "HandGrid" in healer.evidence_text(res["evidence"]))

    print("\n── with the agent (mock model, real CLI, real tools)")
    calls = {"n": 0}

    def model(body):
        """A competent model, scripted by DURABLE MARKERS in the
        transcript — not by any kind of count.

        Counting drifts, and it does not matter whether you count turns
        or tool_results: the CLI also makes AUXILIARY calls (conversation
        titles, summaries) carrying a fresh, tiny message list with no
        history in it at all. Measured here, request 2 and request 4 of
        a four-request turn were auxiliary and showed zero tool_results.
        A script reading those as "start from the beginning" replays the
        same turn forever — which is exactly how this battery came to
        hang for thirty minutes, reissuing set_content_bulk every 1.3s.

        Markers are monotonic: once the transcript contains a successful
        set_content_bulk, it always will, so the script cannot go
        backwards no matter what a single request happens to show."""
        calls["n"] += 1
        if calls["n"] > 40:
            # belt and braces: no scripted mock may loop forever again
            return [{"text": "Done."}]
        # Read what the ASSISTANT actually called. Substring matching on
        # the whole body does not work: the healer's own prompt names
        # mcp__aethron__set_content_bulk, and its evidence contains both
        # "applied" and "rejected", so every obvious marker is already
        # present before the model has done anything. Keying on those
        # made the mock skip its first move and the deny check went
        # vacuous — a passing test proving nothing.
        used = [c.get("name", "")
                for m in body.get("messages", [])
                if m.get("role") == "assistant"
                for c in (m.get("content") or [])
                if isinstance(c, dict) and c.get("type") == "tool_use"]
        did_write = any(n == "Write" for n in used)
        did_pipeline = any("set_content" in n for n in used)
        if did_write and did_pipeline:
            return [{"text": "Rebuilt with the brand replaced."}]
        if not did_write:
            # first instinct: edit the built page. It must be refused.
            return [{"text": "I'll fix the built page directly."},
                    {"tool": "Write",
                     "input": {"file_path": str(proj / "site/index.html"),
                               "content": "<h1>hand edited</h1>"}}]
        # then do it properly, through the guarded pipeline
        return [{"text": "Going through the content pipeline instead."},
                {"tool": "mcp__aethron__set_content_bulk",
                 "input": {"project": "broken", "build": True,
                           "entries": [
                               {"section": "strings", "old": "HandGrid",
                                "new": "Acme"},
                               {"section": "strings", "old": "handgrid",
                                "new": "acme"}]}}]

    srv, url = code.mock_provider(script=model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    site_before = (proj / "site/index.html").read_bytes()
    events = []
    res = healer.heal(proj, rounds=1, home=home,
                      on_event=lambda k, d: events.append((k, d["text"])),
                      cfg={"provider": "custom", "base_url": url,
                           "token": "mock", "model": "mock-1",
                           "permission_mode": "acceptEdits",
                           "aethron_tools": True, "isolate": True})
    srv.shutdown()

    tried_write = any(a["tool"] == "Write" for a in res.get("actions", []))
    check("the agent really attempted the forbidden hand edit "
          "(otherwise the next check is vacuous)", tried_write)
    check("that write was DENIED — site/ is untouched",
          tried_write and b"hand edited"
          not in (proj / "site/index.html").read_bytes())
    check("the agent used the guarded tools",
          any("set_content" in t for k, t in events if k == "tool"))
    check("the healer re-ran the checks itself",
          any("re-checking" in t for _, t in events))
    check("verify + probe now pass", res["ok"], res.get("stage", ""))
    check("it reports WHICH stage fixed it",
          str(res.get("stage", "")).startswith("agent-round"))

    cm = json.loads(cm_f.read_text(encoding="utf-8"))
    filled = {e["old"]: e.get("new", "") for e in cm["strings"]
              if e["old"].lower() == "handgrid"}
    check("the fix landed in copy_map, not in the output",
          filled.get("HandGrid") == "Acme")
    rc, out = forge(proj, "verify")
    check("verify is clean on a fresh run", rc == 0)
    check("a snapshot exists so one undo reverts the agent",
          (proj / ".history").is_dir()
          and any((proj / ".history").iterdir()))

    shutil.rmtree(home, ignore_errors=True)
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} green")
    if bad:
        print("FAILED: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
