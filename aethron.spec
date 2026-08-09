# PyInstaller spec — build with:  bash build_desktop.sh
# One binary that is both the Studio launcher and (via --forge) the engine.
# fontTools/brotli are lazy imports inside forge.cmd_logo — declare them.
import os as _os
# bake the cloud config (Supabase URL + anon key) into the bundle if the
# developer created it — never committed (gitignored). Absent = the app
# ships fully local/offline (no login gate).
_datas = [("aethron_config.json", ".")] if _os.path.isfile(
    "aethron_config.json") else []
a = Analysis(
    ["desktop.py"],
    pathex=["."],
    datas=_datas,
    hiddenimports=[
        "forge", "studio", "aethron_cloud",
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
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Aethron",
    console=False,          # no terminal window on double-click
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Aethron", upx=False)
# macOS: wrap in a proper .app bundle so Finder/dock treat it right
import sys
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Aethron.app",
        bundle_identifier="app.aethron.studio",
        info_plist={
            "CFBundleDisplayName": "Aethron",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            # the app opens the user's browser; it has no UI of its own
            "LSUIElement": False,
        },
    )
