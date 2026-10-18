"""History: every saved version says what its change did, and going back
restores exactly that state while saving the current one first. Offline,
no browser, no network."""
import json, os, pathlib, shutil, sys, tempfile, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
T = pathlib.Path(tempfile.mkdtemp(prefix="ae-hist-"))
os.environ["AETHRON_HOME"] = str(T)
import studio  # noqa: E402

ok = True
def check(n, c):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n)

try:
    d = studio.PROJECTS / "p"; d.mkdir(parents=True)
    (d / "forge.json").write_text('{"platform": "framer"}')
    cm = {"strings": [{"old": "Push Your", "new": ""}, {"old": "Limits", "new": ""}],
          "images": [], "links": []}
    f = d / "copy_map.json"
    def write(**kw):
        studio.snapshot(d)
        c = json.loads(f.read_text())
        for e in c["strings"]:
            if e["old"] in kw: e["new"] = kw[e["old"]]
        f.write_text(json.dumps(c)); time.sleep(0.01)
    f.write_text(json.dumps(cm))
    check("no versions before any change", studio.history_list(d) == [])
    write(**{"Push Your": "Train Smarter"})
    write(**{"Limits": "with Volt"})
    v = studio.history_list(d)
    check("one version per change", len(v) == 2)
    check("newest first", v[0]["t"] > v[1]["t"])
    check("each says the line it changed",
          v[0]["lines"][0]["old"] == "Limits" and v[0]["lines"][0]["after"] == "with Volt"
          and v[1]["lines"][0]["after"] == "Train Smarter")
    check("a plan change is named",
          (studio.snapshot(d), (d / "project_plan.md").write_text("x"),
           "plan" in studio.history_list(d)[0]["other"])[2])
    # going back to before the first change, the way the endpoint does it
    first = v[-1]["id"]
    snap = json.loads((d / ".history" / (first + ".json")).read_text())
    n_before = len(studio.history_list(d))
    studio.snapshot(d)
    for fn, content in snap.items():
        (d / fn).write_text(content)
    c = json.loads(f.read_text())
    check("going back restores that version",
          all(e["new"] == "" for e in c["strings"]))
    check("where you were is saved, so it can be undone",
          len(studio.history_list(d)) == n_before + 1)
    check("that newest version names what going back undid",
          studio.history_list(d)[0]["n"] == 2)
finally:
    shutil.rmtree(T, ignore_errors=True)
print("history battery:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
