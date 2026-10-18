"""The PACKAGED app can read the words in a screenshot.

screenshot-to-page worked from source and failed on every installed copy:
the bundle shipped no text-recognition helper, could not compile one, and
reported "OCR unavailable". This drives the app's own engine
(`Aethron --forge vision <img> --check <img>`) with a throwaway data folder
and no swiftc on PATH, so only a helper INSIDE the bundle can pass it.

usage: python3 tests/ocr_frozen.py dist/Aethron.app
"""
import os, pathlib, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if sys.platform != "darwin":
    print("VERDICT: SKIPPED — text recognition needs macOS"); sys.exit(0)
app = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dist/Aethron.app")
exe = app / "Contents/MacOS/Aethron"
if not exe.exists():
    print(f"VERDICT: SKIPPED — no app at {app}"); sys.exit(0)
img = ROOT / "docs/screenshots/library.png"
home = tempfile.mkdtemp(prefix="ae-ocr-")
env = dict(os.environ, AETHRON_HOME=home, PATH="/usr/bin:/bin")
r = subprocess.run([str(exe), "--forge", "vision", str(img), "--check", str(img)],
                   capture_output=True, text=True, timeout=600, env=env)
out = r.stdout + r.stderr
print(out[-1500:])
ok = "lines correct" in out and "SKIPPED" not in out
print("VERDICT:", "PASS" if ok else "FAIL — the packaged app could not read text")
sys.exit(0 if ok else 1)
