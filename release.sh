#!/usr/bin/env bash
# Cut an Aethron release — build, zip, publish, so the app can update
# itself. One command, because a release that takes ten steps is a
# release nobody cuts, and an app that cannot update itself makes its
# owner delete and reinstall by hand.
#
#   bash release.sh 1.0.1 "what changed"
#
# Needs `gh` authenticated once (gh auth login). Everything else is here.
set -euo pipefail
cd "$(dirname "$0")"

VER="${1:?usage: bash release.sh <version> [notes]}"
NOTES="${2:-}"
TAG="v${VER#v}"
VER="${VER#v}"

# 1. the version the running app compares against MUST match the tag,
#    or the updater will either never fire or loop forever offering an
#    update it already installed.
python3 - "$VER" <<'PY'
import pathlib, re, sys
ver = sys.argv[1]
p = pathlib.Path("aethron_update.py")
s = p.read_text()
new = re.sub(r'^VERSION = "[^"]*"', f'VERSION = "{ver}"', s, count=1, flags=re.M)
if new == s:
    raise SystemExit("could not set VERSION in aethron_update.py")
p.write_text(new)
print(f"  VERSION -> {ver}")
PY

# 2. gates BEFORE anything is published — a broken release auto-installs
#    itself onto every user, which is worse than no release at all.
echo "── gates"
python3 -m py_compile studio.py forge.py desktop.py aethron_update.py
python3 -c "
import re, studio, pathlib
pathlib.Path('/tmp/_rel.js').write_text(
    '\n'.join(re.findall(r'<script>(.*?)</script>', studio.INDEX_HTML, re.S)))"
node --check /tmp/_rel.js >/dev/null
python3 aethron_update.py --selftest >/dev/null
echo "  python, studio JS, updater selftest: ok"

# 3. build + zip
echo "── building"
rm -rf dist build
bash build_desktop.sh >/dev/null 2>&1
[ -d dist/Aethron.app ] || { echo "build produced no app"; exit 1; }
ditto -c -k --sequesterRsrc --keepParent dist/Aethron.app "dist/Aethron-mac.zip"
echo "  dist/Aethron-mac.zip ($(du -h dist/Aethron-mac.zip | cut -f1))"

# 4. commit the version bump, tag, publish
git add -A
git commit -qm "Release $TAG${NOTES:+ — $NOTES}" || true
git push -q origin HEAD:main
gh release create "$TAG" "dist/Aethron-mac.zip" \
    --title "Aethron $TAG" \
    --notes "${NOTES:-Maintenance release.}"

echo
echo "✓ published $TAG"
echo "  every installed copy will offer this update on next launch."
