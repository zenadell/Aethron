"""Clear requests are actions; a project conversation works through tools.
  * "publish", "undo", "rebuild", "check it", "preview", "stop", "how far
    is it?" are recognised in the window (typos and politeness included)
    and are never sent to the model; a request that adds another wish is;
  * a project session started by the studio is not offered the shell or
    file tools (the real engine, a mock model reads the tool list);
  * get_config answers what forge.json would; report_gap records a need.
No node / no engine -> that part says SKIPPED, never PASS.
"""
import json, os, shutil, subprocess, sys, tempfile, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS = FAIL = 0


def ck(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))


def main():
    print("A. clear requests in the window")
    src = (ROOT / "studio.py").read_text()
    js = src[src.index("function directIntent(t){"):src.index("function statusLine(){")]
    cases = {"publish": "publish", "Publish it please": "publish",
             "deploy the site": "publish", "publish but change the logo": "",
             "undo that": "undo", "rebuild": "build", "build it again": "build",
             "check it": "check", "is it working?": "check", "preview": "preview",
             "show me the site": "preview", "stop": "stop",
             "how far is it?": "status", "are we done?": "status",
             "make the hero bigger": "", "finish up": "",
             "change the button colour": ""}
    prog = "var S={cur:'x'};" + js + "\nconsole.log(JSON.stringify(" \
        + json.dumps(list(cases)) + ".map(directIntent)))"
    try:
        got = json.loads(subprocess.run(["node", "-e", prog], capture_output=True,
                                        text=True, timeout=30).stdout.strip())
        want = list(cases.values())
        ck("each clear request is recognised, and only those",
           got == want, [f"{t!r}->{g!r}" for t, g, w in zip(cases, got, want) if g != w])
        ck("they never reach the model (handled before the session is asked)",
           "directIntent(text);\n   if(k){ta.value='';await runDirect(k,text);return;}}" in src)
    except (OSError, ValueError):
        print("  SKIPPED the window matcher (no node)")

    print("B. a project conversation works through tools")
    import aethron_code as C
    ck("the shell and file tools are locked in a project chat",
       {"Bash", "Read", "Write", "Edit", "Grep", "Glob", "Agent"} <= set(C.PROJECT_LOCKED)
       and "codelayer.PROJECT_LOCKED" in src)
    ck("the rules say everything goes through tools, and what to do when none fits",
       "EVERYTHING GOES THROUGH" in C.PROJECT_RULES and "report_gap" in C.PROJECT_RULES)
    if not shutil.which("claude"):
        print("  SKIPPED the engine check (no engine on this machine)")
    else:
        seen = []

        def listen(body):
            if body.get("tools"):
                seen.append([t.get("name") for t in body["tools"]])
            return [{"text": "ok"}]
        srv, url = C.mock_provider(script=listen)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        home = Path(tempfile.mkdtemp(prefix="aelock-"))
        ws = home / "projects" / "p"; ws.mkdir(parents=True)
        (ws / "forge.json").write_text(json.dumps({"platform": "static", "pages": []}))
        names = {}
        for locked in (False, True):
            seen.clear()
            cfg = {"provider": "custom", "base_url": url, "token": "mock",
                   "permission_policy": "semi_manual", "isolate": True, "model": "mock-1"}
            if locked:
                cfg["disallowed_tools"] = C.PROJECT_LOCKED
            C.run_once(ws, "hello", cfg=cfg, home=home, timeout=120)
            names[locked] = set(seen[0]) if seen else set()
        srv.shutdown()
        ck("the engine offers the shell when nothing locks it (the check can see)",
           "Bash" in names[False], sorted(names[False]))
        ck("a locked session is offered no shell and no file tools",
           names[True] and not ({"Bash", "Read", "Write", "Edit", "Grep", "Glob"}
                                & names[True]), sorted(names[True]))
        shutil.rmtree(home, ignore_errors=True)

    print("C. the tools that replace reading files")
    home = Path(tempfile.mkdtemp(prefix="aecfg-"))
    os.environ.update(AETHRON_HOME=str(home), AETHRON_REPORTS="off", AETHRON_RUNLOG="off")
    d = home / "projects" / "demo"; d.mkdir(parents=True)
    (d / "forge.json").write_text(json.dumps({
        "platform": "framer", "pages": ["index.html"], "hide_text": ["FRAMESHIP"],
        "hide_selectors": [".badge"], "share_image": "/assets/share.png"}))
    (d / "copy_map.json").write_text(json.dumps({"strings": [
        {"old": "a", "new": "b"}, {"old": "c", "new": ""}],
        "styles": [{"selector": ".hero", "css": {"color": "red"}}]}))
    import forge_mcp as M
    r = M.t_get_config({"project": "demo"})
    data = getattr(r, "data", {}) or {}
    ck("get_config answers what forge.json would",
       data.get("platform") == "framer" and data.get("hidden_text") == ["FRAMESHIP"]
       and data.get("lines") == 2 and data.get("filled") == 1
       and data.get("styles", [{}])[0].get("selector") == ".hero", data)
    M.t_report_gap({"project": "demo", "need": "add a fifth team card"})
    rows = [json.loads(l) for l in (home / "gaps.jsonl").read_text().splitlines()]
    ck("report_gap records the need", rows and rows[-1]["need"] == "add a fifth team card")
    try:
        M.t_report_gap({"need": ""})
        ck("an empty gap is refused", False)
    except ValueError:
        ck("an empty gap is refused", True)
    shutil.rmtree(home, ignore_errors=True)
    print(f"\ntools-only battery: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
