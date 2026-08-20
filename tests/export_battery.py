#!/usr/bin/env python3
"""Battery for the framework exporter — the whole loop, no API key.

A mock model that READS each request and answers it (write the file you
were asked for) proves everything except model quality: batching,
per-section turns, deterministic assembly, the real npm build, and the
referee's verdict. Model quality is the only thing a live run has to
buy.

    python3 tests/export_battery.py [project-dir]
"""
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PROJ = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 \
    else ROOT / "projects/agero"

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   {detail}" if not cond and detail else ""))


def main():
    import aethron_code as code
    import aethron_export as X

    if not (PROJ / "site").is_dir():
        print(f"SKIPPED — {PROJ} is not migrated/built")
        return 0
    if not code.find_claude():
        print("SKIPPED — the `claude` CLI is not installed")
        return 0

    def answer(body):
        """A model that does exactly what it was told, and nothing else."""
        last = ""
        for m in reversed(body.get("messages", [])):
            if m.get("role") == "user":
                c = m.get("content")
                last = c if isinstance(c, str) else json.dumps(c)
                break
        m = re.search(r"Write ONE file: `([^`]+)`", last)
        if not m:
            return [{"text": "nothing to do"}]
        path = m.group(1)
        head = re.search(r"^# (.+)$", last, re.M)
        title = (head.group(1) if head else "Section").replace("'", "")
        if path.endswith(".astro"):
            body_src = f"<section><h2>{title}</h2></section>\n"
        else:
            body_src = ("export default function S() {\n"
                        f"  return <section><h2>{title}</h2></section>;\n"
                        "}\n")
        return [{"tool": "Write", "input": {"file_path": path,
                                            "content": body_src}}]

    srv, url = code.mock_provider(script=answer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dest = PROJ / "export-next"
    had_modules = (dest / "node_modules").is_dir()
    events = []
    res = X.export(PROJ, "next", rounds=1, install=not had_modules,
                   on_event=lambda k, t: events.append((k, t)),
                   cfg={"provider": "custom", "base_url": url,
                        "token": "mock", "model": "mock-1"})
    srv.shutdown()

    comp = dest / "components"
    files = sorted(comp.glob("Section*.tsx")) if comp.is_dir() else []
    check("the outline was split into bounded batches",
          any("section batch" in t for _, t in events))
    check("a file was written per section", len(files) >= 3,
          f"{len(files)} files")
    page = (dest / "app/page.tsx").read_text(encoding="utf-8")
    check("Aethron assembled the page itself (not the model)",
          all(f.stem in page for f in files) and "export default" in page)
    check("the port actually builds",
          res.get("stage") not in ("build-failed", "no-sections"),
          str(res.get("verdict"))[:200])
    check("the referee returned a real verdict",
          "identical" in str(res.get("verdict", "")) or res.get("ok"),
          str(res.get("verdict"))[:120])
    check("a port this thin is REFUSED", not res.get("ok"))
    check("per-section cost is reported",
          all("seconds" in h for h in res.get("history", [])))

    # the guard is on by default for a live run
    import aethron_brain as brain
    if brain.is_paid():
        try:
            X.export(PROJ, "next", rounds=1, install=False)
            check("a live run needs --live", False, "it was not refused")
        except SystemExit as e:
            check("a live run needs --live", "--live" in str(e))
    else:
        print("  (no paid provider configured — --live check skipped)")

    for f in files:
        f.unlink()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} green")
    if bad:
        print("FAILED: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
