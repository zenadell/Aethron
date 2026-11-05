#!/usr/bin/env bash
# Build the Aethron desktop app (macOS -> dist/Aethron.app,
# Windows/Linux -> dist/Aethron[.exe]). Run on the OS you target.
set -euo pipefail
cd "$(dirname "$0")"

# pywebview gives the app a real native window (no browser chrome). On
# macOS it uses the built-in WebKit via pyobjc; if it's ever missing at
# runtime the app falls back to opening the system browser.
python3 -m pip install --quiet --upgrade pyinstaller fonttools brotli pywebview pyobjc-framework-Quartz

# clean previous build artifacts (never touches projects/ or library/)
rm -rf build dist

# THE TEXT-RECOGNITION HELPER SHIPS COMPILED. Screenshot-to-page reads the
# words with macOS Vision through a small Swift helper; the app used to
# compile it on first use, which an installed copy can never do (the bundle
# is read-only, the source was not in it, and most Macs have no swiftc).
# Built here for both Apple silicon and Intel, and packed by aethron.spec.
rm -f aethron_ocr
if [ "$(uname)" = "Darwin" ]; then
  swiftc -O -target arm64-apple-macos11 aethron_ocr.swift -o build_ocr_arm64
  swiftc -O -target x86_64-apple-macos11 aethron_ocr.swift -o build_ocr_x86_64
  lipo -create build_ocr_arm64 build_ocr_x86_64 -output aethron_ocr
  rm -f build_ocr_arm64 build_ocr_x86_64
  chmod +x aethron_ocr
fi

python3 -m PyInstaller aethron.spec --noconfirm

# A built copy must never show up as a SECOND Aethron in Spotlight or
# Launchpad: the owner's real app is the one in /Applications, updated in
# place by its Update button. Keep the build folders out of the index.
touch dist/.metadata_never_index build/.metadata_never_index 2>/dev/null || true

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
