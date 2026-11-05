# PyInstaller spec — build with:  bash build_desktop.sh
# One binary that is both the Studio launcher and (via --forge) the engine.
# fontTools/brotli are lazy imports inside forge.cmd_logo — declare them.
import os as _os
import sys
# bake the cloud config (Supabase URL + anon key) into the bundle if the
# developer created it — never committed (gitignored). Absent = the app
# ships fully local/offline (no login gate).
_datas = [("aethron_config.json", ".")] if _os.path.isfile(
    "aethron_config.json") else []

# ONE source of truth for the version. The updater compares
# aethron_update.VERSION against the feed; if the plist said something
# else, Finder and the About box would report a build that is not the
# one deciding whether to update — which is how you end up "certain"
# you are testing a fix you never installed.
import re as _re
_VERSION = "0.0.0"
try:
    _m = _re.search(r'^VERSION = "([^"]+)"',
                    open("aethron_update.py").read(), _re.M)
    _VERSION = _m.group(1) if _m else _VERSION
except Exception:
    pass
# A labelled build lets a tester install a preview beside their stable app.
# The normal release leaves both values empty, so its bundle identity stays
# exactly Aethron.app / app.aethron.studio.
_BUILD_NAME = _os.environ.get("AETHRON_BUILD_NAME", "Aethron").strip()
_BUILD_NAME = _re.sub(r"[^A-Za-z0-9 ._-]", "", _BUILD_NAME) or "Aethron"
_BUILD_LABEL = _os.environ.get("AETHRON_BUILD_LABEL", "").strip()
_BUNDLE_SUFFIX = _re.sub(r"[^a-z0-9]+", "-", _BUILD_NAME.lower()).strip("-")
_BUNDLE_ID = ("app.aethron.studio" if _BUILD_NAME == "Aethron" else
              "app.aethron.studio." + _BUNDLE_SUFFIX)
_DISPLAY_VERSION = (_VERSION if not _BUILD_LABEL else
                    f"{_VERSION} ({_BUILD_LABEL})")
# the compiled text-recognition helper (build_desktop.sh makes it on macOS)
_binaries = [("aethron_ocr", ".")] if _os.path.isfile("aethron_ocr") else []
a = Analysis(
    ["desktop.py"],
    pathex=["."],
    datas=_datas,
    binaries=_binaries,
    hiddenimports=['aethron_jobs', 'aethron_share', 'aethron_locate', 'aethron_fill', 'aethron_access', 'aethron_reports', 'aethron_publish', 'aethron_browser', 'aethron_templates', 'aethron_control', 
        "forge", "studio", "aethron_cloud",
        # THE AGENT'S TOOLS. Spawned as a SUBPROCESS entry point
        # (`Aethron --mcp`), not merely imported, so it must be in the
        # archive or the migration agent starts with no mcp__aethron__*
        # tools at all — which is exactly what shipped: a console agent
        # instructed to call create_project/fetch/inventory, holding
        # none of them, offering to hand-rewrite the site instead.
        "forge_mcp", "aethron_permission_mcp",
        # THE PORTER. Absent from the bundle until 2026-09-06, so the
        # shipped app could not convert at all — `forge convert` would
        # have died on the import inside a frozen build while working
        # perfectly in dev. aethron_convert pulls in the motion recorder
        # and the source-map recovery, and PyInstaller cannot see them
        # (they are imported inside functions).
        "aethron_convert", "aethron_motion", "aethron_source",
        # Measurement pass for screenshots, imported inside cmd_vision.
        # Listed the same day the MCP server taught this lesson twice.
        "aethron_vision",
        # Figma: a design file is an L0 source like a live site is.
        "aethron_figma", "aethron_figma_grade",
        # THE CHANGE STACK. A user's own words turned into code, kept only when measured
        # true — and the seam that makes it work on a page Aethron did NOT build. Every one
        # of these is imported INSIDE a function (forge.cmd_adopt, studio's design job,
        # forge_mcp's handlers), and the bundle that shipped before them was six days stale:
        # the whole capability existed in dev and in nothing a user could download. Named
        # explicitly rather than trusted to bytecode-walking, because this file's history
        # has that exact lesson twice already.
        "aethron_adopt", "aethron_change", "aethron_spec", "aethron_agm",
        "aethron_replicate", "aethron_surface", "aethron_gradient",
        "aethron_edit", "aethron_screen", "aethron_flow", "aethron_web",
        "aethron_eye", "aethron_design", "aethron_build", "aethron_generate",
        "aethron_shot2code", "aethron_frame", "aethron_fit", "aethron_pictures", "aethron_usage",
        "aethron_native",
        # Brand memory: imported inside forge_mcp's handlers, so named here
        # before it can ship as a capability that exists only in dev.
        "aethron_rag",
        "aethron_preview", "aethron_runlog", "aethron_news",
        # Ember, the companion: imported at studio start (its script is embedded).
        "aethron_ember", "aethron_desktop", "aethron_voice",
        "aethron_thumbs",
        "statistics",
        "fontTools", "fontTools.ttLib", "fontTools.pens.svgPathPen",
        "fontTools.pens.transformPen", "brotli",
        # native window; the pywebview PyInstaller hook pulls the pyobjc
        # cocoa/WebKit backend, but name the entry points explicitly too.
        "webview", "webview.platforms.cocoa",
        "webview.platforms.winforms", "webview.platforms.edgechromium",
    ] + (["Quartz", "Speech", "AVFoundation", "Foundation"]   # Ember: window frames; speech on the device
         if __import__("sys").platform == "darwin" else []),
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
)
pyz = PYZ(a.pure)
# ONEDIR, not onefile: onefile re-extracts the whole runtime to temp on
# EVERY launch (~20s cold start). Onedir starts in ~2s; the .app bundle
# hides the folder from users entirely. Windows ships the dist folder
# zipped (users run Aethron.exe inside it).
_icon = "brand/Aethron.icns" if _os.path.isfile("brand/Aethron.icns") else None
# WINDOWS: one binary, several roles (window, --forge engine, --mcp server,
# --job). A windowed .exe gets NO stdout, so the engine and the MCP server
# would answer nothing to the app that started them. So the Windows .exe is
# a console program whose console is hidden at once when it was opened for
# it (a double-click): nobody sees a window, the roles still talk over
# pipes, and the engine runs the app starts share its hidden console, so
# no window flashes per build.
_win = sys.platform == "win32"
_win_icon = "brand/Aethron.ico" if _os.path.isfile("brand/Aethron.ico") else None
exe = EXE(
    pyz,
    a.scripts,
    # UTF-8 everywhere on Windows: its default text encoding is cp1252, so
    # every open() and every engine line read through a pipe ("✓", "—",
    # any copy in another language) would fail or garble.
    [("X utf8_mode=1", None, "OPTION")] if _win else [],
    exclude_binaries=True,
    name=_BUILD_NAME,
    console=_win,           # Mac: no terminal window on double-click
    hide_console="hide-early" if _win else None,
    upx=False,
    icon=_win_icon if _win else _icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name=_BUILD_NAME, upx=False)
# macOS: wrap in a proper .app bundle so Finder/dock treat it right
import sys
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=_BUILD_NAME + ".app",
        icon=_icon,
        bundle_identifier=_BUNDLE_ID,
        info_plist={
            "CFBundleDisplayName": _BUILD_NAME,
            "CFBundleShortVersionString": _DISPLAY_VERSION,
            "CFBundleVersion": _VERSION,
            "AethronBuildLabel": _BUILD_LABEL,
            "NSHighResolutionCapable": True,
            # the app opens the user's browser; it has no UI of its own
            "LSUIElement": False,
            # Ember listens when you double-tap it; macOS asks once for each
            "NSMicrophoneUsageDescription":
                "Ember listens when you double-tap it, so you can tell Aethron what to do out loud.",
            "NSSpeechRecognitionUsageDescription":
                "Ember turns what you say into text on this Mac, so it can type your request for Aethron.",
        },
    )
