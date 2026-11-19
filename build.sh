#!/bin/zsh
set -euo pipefail

VERSION="${1:-1.1.0}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
APP="$BUILD/BiliMonitor.app"

rm -rf "$BUILD" "$DIST"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$DIST"

cp "$ROOT/monitor.py" "$ROOT/config.example.json" "$APP/Contents/Resources/"
cp "$ROOT/packaging/launcher" "$APP/Contents/MacOS/BiliMonitor"
chmod +x "$APP/Contents/MacOS/BiliMonitor"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>BiliMonitor</string>
    <key>CFBundleDisplayName</key>
    <string>BiliMonitor</string>
    <key>CFBundleIdentifier</key>
    <string>com.billwgong.bilimonitor</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleExecutable</key>
    <string>BiliMonitor</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

mkdir -p "$BUILD/pkgroot/Applications"
cp -R "$APP" "$BUILD/pkgroot/Applications/"

pkgbuild \
  --root "$BUILD/pkgroot" \
  --identifier com.billwgong.bilimonitor \
  --version "$VERSION" \
  --install-location "/" \
  "$DIST/BiliMonitor-$VERSION.pkg"

ditto -c -k --keepParent "$APP" "$DIST/BiliMonitor-$VERSION.zip"

echo "built:"
ls -lh "$DIST"
