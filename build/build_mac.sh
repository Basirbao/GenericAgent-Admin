#!/usr/bin/env bash
# Build a macOS .dmg installer for GenericAgent-Admin.
#
# Prerequisites (one-time):
#   * Python 3.11 or 3.12 with pyinstaller:  pip install pyinstaller
#   * Node.js LTS                             (for `npm run build`)
#   * create-dmg                              brew install create-dmg
#
# Usage:
#   bash build/build_mac.sh
#
# Output:
#   build/dist/GenericAgent Admin.app    (run-in-place app bundle)
#   build/dist/GenericAgent-Admin.dmg    (drag-to-Applications installer)

set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
DMG_NAME="GenericAgent-Admin-${VERSION}.dmg"

echo "==> [1/4] Building webui (npm)"
if [ ! -d "webui/node_modules" ]; then
    (cd webui && npm ci)
fi
(cd webui && npm run build)

echo "==> [2/4] Cleaning previous build output"
rm -rf build/build build/dist "build/${DMG_NAME}"

echo "==> [3/4] Running pyinstaller"
if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "    pyinstaller missing — installing into current Python"
    python3 -m pip install --upgrade "pyinstaller>=6.0"
fi
python3 -m PyInstaller build/admin.spec \
    --noconfirm \
    --workpath build/build \
    --distpath build/dist

APP="build/dist/GenericAgent Admin.app"
if [ ! -d "$APP" ]; then
    echo "ERROR: $APP not produced — check pyinstaller log above" >&2
    exit 1
fi

echo "==> [3.5/4] Nesting helper bundle into main app"
# admin.spec emits a sibling GenericAgent Admin Helper.app (LSUIElement=true).
# Move it under the main app's Contents/Frameworks/ so users see one bundle
# in /Applications and signing/notarization treats them as one unit. The
# launcher resolves the helper binary via launch_webui.py:_helper_executable
# at runtime (looks for Frameworks/GenericAgent Admin Helper.app/…).
HELPER_SRC="build/dist/GenericAgent Admin Helper.app"
HELPER_DST_DIR="$APP/Contents/Frameworks"
if [ -d "$HELPER_SRC" ]; then
    mkdir -p "$HELPER_DST_DIR"
    rm -rf "$HELPER_DST_DIR/GenericAgent Admin Helper.app"
    mv "$HELPER_SRC" "$HELPER_DST_DIR/"
else
    echo "WARNING: $HELPER_SRC not found — backend will fall back to main app's binary" >&2
    echo "         (Dock will show a second icon for the FastAPI subprocess)" >&2
fi

echo "==> [4/4] Wrapping into .dmg"
if ! command -v create-dmg >/dev/null; then
    echo "    create-dmg missing. Install with: brew install create-dmg" >&2
    echo "    (you can still run the .app directly from build/dist/)" >&2
    exit 0
fi

create-dmg \
    --volname "GenericAgent Admin" \
    --window-size 560 340 \
    --icon-size 96 \
    --icon "GenericAgent Admin.app" 140 170 \
    --app-drop-link 410 170 \
    --no-internet-enable \
    "build/${DMG_NAME}" \
    "$APP"

echo
echo "✅ Done."
echo "   App:  $APP"
echo "   DMG:  build/${DMG_NAME}"
echo
echo "First-launch on a fresh Mac: right-click the .app → Open (Gatekeeper bypass for unsigned builds)."
