"""Throw varied requests at Aethron, untouched, and record what it did with each.

Nothing here is scripted: every request goes through aethron_spec.build on the free keys,
exactly as `forge edit --ask` would. The point is the SPREAD — text, colour, size, a new
element, behaviour, hover, removal, motion, position, readability — because "it works" on
three requests of one shape says nothing about the next shape a user invents.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUB = ROOT / "tests/pages"   # in the REPO: /private/tmp is wiped on reboot, and was
OUT = ROOT / "tests" / ".stress"

ASKS = [
    ("text", "Change the headline to say 'Ship apps people love'"),
    ("colour", "Make the Generate button green"),
    ("remove", "Remove the '• Launch app 10x faster' line at the bottom"),
    ("hover", "Make the Windows button glow a little when I hover over it"),
    ("loader", "When I click Generate, show a spinning loader inside the button for about 2 seconds"),
    ("badge", "Add a small 'Free trial' badge just above the chat box"),
    ("size", "Make the Generate button a bit wider"),
    ("motion", "Make the logo in the top left spin slowly, around once every 6 seconds"),
]


BUDGET = "0.30"   # per request; the WALLET caps the batch, and every run together


def main(names):   # names are run IN THE ORDER GIVEN — the free quota may not reach the end
    OUT.mkdir(parents=True, exist_ok=True)
    # The page's own assets sit beside it: a result rendered without them shows a broken logo
    # that has nothing to do with the change being judged.
    if (PUB / "assets").is_dir() and not (OUT / "assets").exists():
        import shutil
        shutil.copytree(PUB / "assets", OUT / "assets")
    byname = dict(ASKS)
    picked = [(n, byname[n]) for n in names if n in byname] if names else list(ASKS)
    summary = []
    for name, ask in picked:
        out = OUT / f"site-{name}.html"
        log = OUT / f"{name}.log"
        t = time.time()
        r = subprocess.run([sys.executable, "aethron_spec.py", str(PUB / "site.html"), ask,
                            "--out", str(out), "--budget", BUDGET],
                           cwd=ROOT, capture_output=True, text=True, timeout=3600)
        log.write_text(r.stdout + r.stderr)
        rec = {"name": name, "ask": ask, "seconds": round(time.time() - t), "exit": r.returncode}
        try:
            sys.path.insert(0, str(ROOT))
            import aethron_brain as B
            rec["wallet_left"] = round(B.wallet()["left_usd"], 4)
        except Exception:
            pass
        spec = out.with_suffix(".spec.json")
        if spec.exists():
            got = json.loads(spec.read_text())
            rec["verdict"] = got.get("verdict")
            rec["problems"] = [p[:220] for p in (got.get("problems") or [])[:4]]
            rec["ledger"] = {k: got.get("ledger", {}).get(k) for k in ("calls", "free_calls", "usd", "stopped")}
            rec["attempts"] = [{"phase": a.get("phase"), "problems": [p[:160] for p in (a.get("problems") or [])[:2]]}
                               for a in (got.get("attempts") or [])]
        summary.append(rec)
        print(json.dumps(rec, indent=1), flush=True)
        (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n=== VERDICTS")
    for rec in summary:
        print(f"  {rec['name']:8} {rec.get('verdict', '?'):10} {rec['seconds']:5}s  "
              f"{(rec.get('ledger') or {}).get('free_calls', '?')} free calls   {rec['ask'][:60]}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
