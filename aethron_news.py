"""What's new — the card the app shows once after an update.

WHEN TO ADD AN ENTRY: only when a release gives the person something
new to DO. A fix, a speed-up or a quieter error is not news; the update
chip already says "Updated". A card that shows for everything is a card
people learn to close without reading.

Each entry is shown ONCE per machine (remembered in AETHRON_HOME, which
survives updates). Entries are keyed by id, not by version: releases are
numbered by the build run, so a version is not known when the entry is
written.

    id      unique and never reused: "2026-10-preview-rail"
    title   a few words
    body    one or two sentences, what it does for them
    art     the coded illustration to play: rail | back | runs
    go      what "Try it" does (studio.py newsGo): preview | back | runs
    label   the button's words (default "Try it")

Kept as code rather than a data file so it is always inside the bundle.
"""
import json
from pathlib import Path

NEWS = [
    {"id": "2026-10-preview-rail",
     "title": "Your site, always beside the chat",
     "body": "Open a project and its preview sits on the right — with "
             "progress, Build and downloads in the same place. Strings, "
             "Images and Links open in a sheet, so the chat never gets "
             "squeezed.",
     "art": "rail", "go": "preview", "label": "Open a project"},
    {"id": "2026-10-back",
     "title": "A way back from anywhere",
     "body": "Settings, Library, Agent runs — the arrow at the top takes "
             "you back to where you were. Esc and ⌘[ do the same.",
     "art": "back", "go": "back", "label": "Show me"},
    {"id": "2026-10-agent-runs",
     "title": "See what an agent did",
     "body": "Every call an agent makes to Aethron is recorded: what it "
             "asked, what came back, and anything that failed — keys are "
             "never kept. Find them under Agent runs.",
     "art": "runs", "go": "runs", "label": "Open Agent runs"},
]

SEEN = ".news-seen.json"


def used(home: Path) -> bool:
    """Has anyone used Aethron on this machine? The app creates empty
    projects/ and library/ folders the moment it starts, so an empty
    folder is not evidence — something in it, or saved settings, is."""
    for n in ("projects", "library"):
        try:
            if any((home / n).iterdir()):
                return True
        except Exception:
            pass
    return (home / "aethron_config.json").exists()


def _read(home: Path) -> list | None:
    try:
        return list(json.loads((home / SEEN).read_text(encoding="utf-8")))
    except Exception:
        return None


def _write(home: Path, ids):
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / SEEN).write_text(json.dumps(sorted(set(ids))),
                                 encoding="utf-8")
    except Exception:
        pass                    # a card shown twice is not a failure


def unseen(home: Path, news=None) -> list:
    """Entries this machine has not been shown, newest first.

    A FRESH INSTALL HAS NOTHING TO CATCH UP ON: with no record and no
    sign the app was used before, everything is marked seen and nothing
    is shown — a new person gets the walkthrough, not a list of changes
    to a product they have never seen. Only a used machine gets news.
    (studio's first-launch check also marks a fresh install seen, before
    its own version stamp would make the machine look used.)
    """
    news = NEWS if news is None else news
    seen = _read(home)
    if seen is None:
        if not used(home):
            _write(home, [n["id"] for n in news])
            return []
        seen = []
    return [n for n in news if n["id"] not in seen]


def mark_seen(home: Path, ids) -> None:
    _write(home, (_read(home) or []) + [str(i) for i in ids])


def _selftest() -> int:
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name)

    with tempfile.TemporaryDirectory() as t:
        fresh = Path(t) / "fresh"
        (fresh / "projects").mkdir(parents=True)     # what startup makes
        check("a fresh install is shown nothing", unseen(fresh) == [])
        check("...and is not shown it later either", unseen(fresh) == [])
        old = Path(t) / "used"
        (old / "projects" / "a-site").mkdir(parents=True)
        got = unseen(old)
        check("a used machine is shown every entry", len(got) == len(NEWS))
        mark_seen(old, [got[0]["id"]])
        check("a seen entry is not shown again",
              [n["id"] for n in unseen(old)] == [n["id"] for n in NEWS[1:]])
        extra = NEWS + [{"id": "later", "title": "t", "body": "b",
                         "art": "rail", "go": "preview"}]
        mark_seen(old, [n["id"] for n in NEWS])
        check("a newer entry appears after the old ones were seen",
              [n["id"] for n in unseen(old, extra)] == ["later"])
    ids = [n["id"] for n in NEWS]
    check("ids are unique", len(ids) == len(set(ids)))
    check("every entry names a known illustration and action",
          all(n["art"] in ("rail", "back", "runs")
              and n["go"] in ("preview", "back", "runs") for n in NEWS))
    print("ALL PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
