"""Nothing is done twice: build, verify and probe give the answer on record
when nothing they read has changed, and run again the moment anything does.
  * an unchanged build is not run again, and says so;
  * a changed copy line, a new asset or forge.json runs it again;
  * --force always runs;
  * verify is remembered until the site changes;
  * a probe that FAILED or was SKIPPED is never served from the record.
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORGE = ROOT / "forge.py"
PASS = FAIL = 0
MARK = "not run again"


def ck(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))


def run(d, *a, env=None):
    p = subprocess.run([sys.executable, str(FORGE), *a], cwd=d, capture_output=True,
                       text=True, timeout=600, env=dict(os.environ, **(env or {})))
    return p.returncode, p.stdout + p.stderr


def main():
    tmp = Path(tempfile.mkdtemp(prefix="aecache-"))
    os.environ.update(AETHRON_REPORTS="off", AETHRON_RUNLOG="off")
    src = tmp / "export"; src.mkdir()
    shutil.copy(ROOT / "tests/pages/site.html", src / "index.html")
    subprocess.run([sys.executable, str(FORGE), "init", str(src / "index.html"), "demo"],
                   cwd=tmp, capture_output=True, text=True, timeout=300)
    d = next(p for p in tmp.iterdir() if (p / "forge.json").exists())
    run(d, "inventory")
    print("A. build")
    rc, out = run(d, "build")
    ck("the first build runs", rc == 0 and MARK not in out and "Build complete" in out, out[-300:])
    rc, out = run(d, "build")
    ck("an unchanged build is not run again, and says so", rc == 0 and MARK in out
       and "Build complete" in out, out[-300:])
    cm = json.loads((d / "copy_map.json").read_text())
    cm["strings"][0]["new"] = "Cached Words"
    (d / "copy_map.json").write_text(json.dumps(cm))
    rc, out = run(d, "build")
    ck("a changed line builds again", MARK not in out
       and "Cached Words" in (d / "site/index.html").read_text(), out[-300:])
    (d / "assets").mkdir(exist_ok=True)
    (d / "assets/new.txt").write_text("x")
    rc, out = run(d, "build")
    ck("a new asset builds again", MARK not in out
       and (d / "site/assets/new.txt").exists(), out[-300:])
    cfg = json.loads((d / "forge.json").read_text())
    cfg["hide_text"] = ["nothing here"]
    (d / "forge.json").write_text(json.dumps(cfg))
    rc, out = run(d, "build")
    ck("a changed forge.json builds again", MARK not in out, out[-300:])
    rc, out = run(d, "build", "--force")
    ck("--force always runs", MARK not in out and "Build complete" in out, out[-300:])
    print("B. verify and probe")
    rc1, out1 = run(d, "verify")
    rc2, out2 = run(d, "verify")
    ck("verify is remembered, with the same verdict and exit code",
       MARK in out2 and rc1 == rc2 and out1.strip() in out2, out2[-300:])
    cm["strings"][0]["new"] = "Other Words"
    (d / "copy_map.json").write_text(json.dumps(cm))
    run(d, "build")
    rc, out = run(d, "verify")
    ck("…until the site changes", MARK not in out, out[-200:])
    rc, out = run(d, "probe", env={"AETHRON_BROWSER": "none"})
    rc, out2 = run(d, "probe", env={"AETHRON_BROWSER": "none"})
    ck("a SKIPPED probe is never served from the record",
       "SKIPPED" in out2 and MARK not in out2, out2[-200:])
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\ncache battery: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
