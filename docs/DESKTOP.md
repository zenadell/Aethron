# The desktop app

Aethron ships to users as a **downloadable desktop app**: they install it,
sign in, and everything runs locally — their template data never leaves
their machine. The thin cloud (Supabase) handles only login + telemetry
(see [CLOUD_SETUP.md](CLOUD_SETUP.md)).

## Build it

```bash
bash build_desktop.sh
```

- **macOS** → `dist/Aethron.app` (double-click; zip or DMG it to ship)
- **Windows** → run the same script on Windows → `dist/Aethron.exe`
- Build on the OS you target — PyInstaller does not cross-compile.

To ship a login-gated build, bake the cloud env into the launch (e.g. a
tiny wrapper that sets `AETHRON_SUPABASE_URL` / `AETHRON_SUPABASE_ANON_KEY`
before `main()`), or ship a config file next to the app. With neither set,
the app runs fully offline with no login — that's the dev/beta default.

## How the packaging works (the two tricks)

1. **One binary, two roles.** The Studio runs every mechanical step as a
   subprocess of `python3 forge.py …` — but a packaged app has no Python.
   So the app doubles as the engine: `Aethron --forge build …` dispatches
   straight into forge's command table (`desktop.py`), and the Studio's
   `forge_argv()` helper picks the right invocation for dev vs frozen.
   Crash isolation and parallel previews keep working, no interpreter
   shipped.

2. **User data lives outside the bundle.** App bundles are read-only, so
   `projects/` and `library/` live in the OS data dir —
   `~/Library/Application Support/Aethron` (macOS),
   `%APPDATA%\Aethron` (Windows) — via the `AETHRON_HOME` env var,
   which defaults to the repo dir in development.

## Signing (when you're ready to distribute widely)

Unsigned builds work but show a one-time OS warning:

- **macOS**: Gatekeeper "unidentified developer" → user right-clicks →
  Open. Proper fix: Apple Developer ($99/yr) → Developer ID cert →
  `codesign` + `notarytool`.
- **Windows**: SmartScreen "unrecognized app" → More info → Run anyway.
  Proper fix: an OV/EV code-signing certificate.

Fine for a beta; sign before public launch.

## Verified

The packaged app was proven end-to-end: frozen Studio boots on a free
port and opens the browser; `--forge` dispatch works; and a full
init → inventory → build pipeline ran **inside the frozen app** via
self-subprocesses — zero Python on the host.
