#!/usr/bin/env python3
"""Run a real migration through the PACKAGED app's own MCP server.

    python3 tests/frozen_migration.py [path/to/Aethron.app] [template-url]

This is the test the whole migration layer was missing. Every earlier
check drove forge_mcp.py from source, where `sys.executable` is Python.
Inside the app `sys.executable` is Aethron itself, and a hard-coded
`[sys.executable, forge.py, ...]` opened a SECOND APP instead of running
the engine — so create, fetch, inventory, build and verify all hung in the
product while passing every test in the repo. Only a run against the
bundle can see that, so this drives `Aethron --mcp` over stdio exactly as
the coding agent does, in a throwaway data folder.

It also watches the process table: a migration must not start any other
Aethron. A GUI instance appearing is the failure, whatever the tools say.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

APP = Path(sys.argv[1] if len(sys.argv) > 1 else
           "/Applications/Aethron.app").resolve()
URL = sys.argv[2] if len(sys.argv) > 2 else "https://lewius.framer.website/"
# a Mac .app, or the Windows/Linux folder holding Aethron(.exe)
EXE = (APP / "Contents" / "MacOS" / "Aethron" if APP.suffix == ".app" else
       APP / ("Aethron.exe" if os.name == "nt" else "Aethron"))
NAME = "frozen-migration-test"
STEPS = [("create_project", {"name": NAME, "url": URL}, 900),
         ("fetch", {"project": NAME}, 900),
         ("inventory", {"project": NAME}, 600),
         ("build", {"project": NAME}, 600),
         ("verify", {"project": NAME}, 600),
         # brand memory, inside the bundle: the throwaway home has no key,
         # so this also proves the honest fallback (keyword search, said so)
         ("learn_brand", {"project": NAME, "max_pages": 3,
                          "sources": ["https://www.jomiez.com/"]}, 300),
         ("search_brand", {"project": NAME,
                           "query": "What services does the studio offer?"}, 120),
         # the background job, inside the bundle: `Aethron --job` must run
         # the pipeline. With no brief and no key it stops at the brief and
         # asks, which proves the role without spending anything.
         ("start_migration", {"project": NAME}, 120)]


def stray_apps(own_pid):
    """Aethron processes that are neither this MCP server nor an engine
    run it spawned — i.e. a whole new copy of the app."""
    if os.name == "nt":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object "
             "{ \"$($_.ProcessId) $($_.ParentProcessId) $($_.CommandLine)\" }"],
            capture_output=True, text=True).stdout
    else:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                             capture_output=True, text=True).stdout
    bad = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or str(EXE) not in parts[2]:
            continue
        pid, cmd = int(parts[0]), parts[2]
        if pid == own_pid or " --forge" in cmd or " --mcp" in cmd \
                or " --job" in cmd \
                or " --permission-mcp" in cmd:
            continue
        bad.append(cmd[:160])
    return bad


def main():
    if not EXE.exists():
        print(f"SKIPPED — no app at {APP}")
        return 0
    home = Path(tempfile.mkdtemp(prefix="aethron-frozen-"))
    env = dict(os.environ, AETHRON_HOME=str(home))
    p = subprocess.Popen([str(EXE), "--mcp"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, env=env, bufsize=1)
    strays, stop = set(), threading.Event()

    def watch():
        while not stop.is_set():
            strays.update(stray_apps(p.pid))
            time.sleep(1.0)
    threading.Thread(target=watch, daemon=True).start()

    replies = {}
    def reader():
        for line in p.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "id" in m:
                replies[m["id"]] = m
    threading.Thread(target=reader, daemon=True).start()

    def call(i, method, params, timeout):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i,
                                  "method": method, "params": params}) + "\n")
        p.stdin.flush()
        t0 = time.time()
        while i not in replies:
            if time.time() - t0 > timeout or p.poll() is not None:
                return None, time.time() - t0
            time.sleep(0.2)
        return replies.pop(i), time.time() - t0

    results, ok_all = [], True
    r, _ = call(0, "initialize", {"protocolVersion": "2024-11-05",
                                  "capabilities": {}, "clientInfo":
                                  {"name": "frozen-test", "version": "1"}}, 60)
    if not r:
        print("FAIL — the packaged MCP server did not answer initialize")
        return 1
    for n, (tool, args, limit) in enumerate(STEPS, 1):
        r, took = call(n, "tools/call", {"name": tool, "arguments": args}, limit)
        if r is None:
            text, bad = f"no answer within {limit}s", True
        else:
            res = r.get("result") or {}
            text = " ".join(c.get("text", "") for c in res.get("content", []))
            bad = bool(res.get("isError")) or "error" in r
        tail = text.strip().splitlines()[-1][:150] if text.strip() else ""
        results.append((tool, not bad, took, tail))
        print(f"  {'ok  ' if not bad else 'FAIL'} {tool:15s} {took:6.1f}s  {tail}")
        if bad:
            ok_all = False
            break
    job = {}
    jf = home / "jobs" / NAME / "state.json"
    t = time.time()
    while ok_all and time.time() - t < 600:
        try:
            job = json.loads(jf.read_text())
        except (OSError, ValueError):
            job = {}
        if job.get("status") in ("waiting", "done", "failed"):
            break
        time.sleep(2)
    job_ok = job.get("status") == "waiting" and all(
        (job.get("steps") or {}).get(k, {}).get("status") == "done"
        for k in ("source", "fetch", "inventory"))
    print(f"  background job ran in the bundle: {job.get('status')} "
          f"— {job.get('question') or job.get('error') or ''}"[:200])
    ok_all = ok_all and job_ok
    stop.set()
    p.stdin.close()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    proj = home / "projects" / NAME
    where = "projects/" if proj.exists() else "NOT in projects/"
    print(f"  project stored {where}: {proj.exists()}  "
          f"({len(list((proj / 'site').glob('*.html'))) if (proj / 'site').exists() else 0} built pages)")
    print(f"  stray Aethron windows opened: {len(strays)}")
    for s in sorted(strays)[:3]:
        print("     ", s)
    good = ok_all and proj.exists() and not strays
    print("PASS" if good else "FAIL")
    shutil.rmtree(home, ignore_errors=True)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
