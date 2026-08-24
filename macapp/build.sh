#!/bin/sh
# Build Jornada Sync.app into macapp/dist/.
set -eu
cd "$(dirname "$0")"

swift build -c release
BIN=.build/release/JornadaSync

APP="dist/Jornada Sync.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Icon
ICON_TMP=$(mktemp -d)
swift run -c release -q IconGen "$ICON_TMP" >/dev/null
iconutil -c icns "$ICON_TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
cp "$ICON_TMP/logo-512.png" dist/logo-512.png 2>/dev/null || true
cp "$ICON_TMP/icon-preview-1024.png" dist/icon-1024.png 2>/dev/null || true
rm -rf "$ICON_TMP"

cp "$BIN" "$APP/Contents/MacOS/JornadaSync"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>JornadaSync</string>
    <key>CFBundleIdentifier</key><string>com.nicholasweiner.jornada-sync</string>
    <key>CFBundleName</key><string>Jornada Sync</string>
    <key>CFBundleDisplayName</key><string>Jornada Sync</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHumanReadableCopyright</key><string>Talks to an HP Jornada / Windows CE 2.x handheld over serial PPP.</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

xattr -cr "$APP" 2>/dev/null || true
codesign --force --sign - "$APP"
echo "built: $APP"
