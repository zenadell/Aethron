#!/usr/bin/env bash
# Build the Aethron desktop app (macOS -> dist/Aethron.app,
# Windows/Linux -> dist/Aethron[.exe]). Run on the OS you target.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install --quiet --upgrade pyinstaller fonttools brotli

# clean previous build artifacts (never touches projects/ or library/)
rm -rf build dist

python3 -m PyInstaller aethron.spec --noconfirm

echo
if [ -d "dist/Aethron.app" ]; then
  echo "✓ built dist/Aethron.app"
  echo "  test:  open dist/Aethron.app"
  echo "  ship:  zip it (or make a DMG). Unsigned builds show a one-time"
  echo "         Gatekeeper warning — users right-click → Open. Code-sign"
  echo "         + notarize when you have an Apple Developer account."
else
  echo "✓ built dist/Aethron (or Aethron.exe)"
  echo "  Windows: SmartScreen shows 'unrecognized app' until you sign."
fi
