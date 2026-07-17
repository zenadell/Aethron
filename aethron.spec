# PyInstaller spec — build with:  bash build_desktop.sh
# One binary that is both the Studio launcher and (via --forge) the engine.
# fontTools/brotli are lazy imports inside forge.cmd_logo — declare them.
a = Analysis(
    ["desktop.py"],
    pathex=["."],
    hiddenimports=[
        "forge", "studio", "aethron_cloud",
        "fontTools", "fontTools.ttLib", "fontTools.pens.svgPathPen",
        "fontTools.pens.transformPen", "brotli",
    ],
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Aethron",
    console=False,          # no terminal window on double-click
    upx=False,
)
# macOS: wrap in a proper .app bundle so Finder/dock treat it right
import sys
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
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
