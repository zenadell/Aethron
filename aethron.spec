# PyInstaller spec — build with:  bash build_desktop.sh
# One binary that is both the Studio launcher and (via --forge) the engine.
# fontTools/brotli are lazy imports inside forge.cmd_logo — declare them.
import os as _os
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
a = Analysis(
    ["desktop.py"],
    pathex=["."],
    datas=_datas,
    hiddenimports=[
        "forge", "studio", "aethron_cloud",
        # THE AGENT'S TOOLS. Spawned as a SUBPROCESS entry point
        # (`Aethron --mcp`), not merely imported, so it must be in the
        # archive or the migration agent starts with no mcp__aethron__*
        # tools at all — which is exactly what shipped: a console agent
        # instructed to call create_project/fetch/inventory, holding
        # none of them, offering to hand-rewrite the site instead.
        "forge_mcp",
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
        "statistics",
        "fontTools", "fontTools.ttLib", "fontTools.pens.svgPathPen",
        "fontTools.pens.transformPen", "brotli",
        # native window; the pywebview PyInstaller hook pulls the pyobjc
        # cocoa/WebKit backend, but name the entry points explicitly too.
        "webview", "webview.platforms.cocoa",
    ],
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
)
pyz = PYZ(a.pure)
# ONEDIR, not onefile: onefile re-extracts the whole runtime to temp on
# EVERY launch (~20s cold start). Onedir starts in ~2s; the .app bundle
# hides the folder from users entirely. Windows ships the dist folder
# zipped (users run Aethron.exe inside it).
_icon = "brand/Aethron.icns" if _os.path.isfile("brand/Aethron.icns") else None
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Aethron",
    console=False,          # no terminal window on double-click
    upx=False,
    icon=_icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Aethron", upx=False)
# macOS: wrap in a proper .app bundle so Finder/dock treat it right
import sys
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Aethron.app",
        icon=_icon,
        bundle_identifier="app.aethron.studio",
        info_plist={
            "CFBundleDisplayName": "Aethron",
            "CFBundleShortVersionString": _VERSION,
            "CFBundleVersion": _VERSION,
            "NSHighResolutionCapable": True,
            # the app opens the user's browser; it has no UI of its own
            "LSUIElement": False,
        },
    )
