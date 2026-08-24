#!/bin/sh
# Build and install Jornada Sync.app.
# Stages + signs in a temp dir (iCloud Desktop re-attaches xattrs that break
# codesign), then installs to ~/Applications and mirrors into macapp/dist.
set -eu
cd "$(dirname "$0")"

swift build -c release
BIN=.build/release/JornadaSync

STAGE=$(mktemp -d)
APP="$STAGE/Jornada Sync.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

ICON_TMP=$(mktemp -d)
swift run -c release -q IconGen "$ICON_TMP" >/dev/null
iconutil -c icns "$ICON_TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
mkdir -p dist
cp "$ICON_TMP/logo-512.png" "$ICON_TMP/icon-preview-1024.png" dist/ 2>/dev/null || true
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
    <key>CFBundleVersion</key><string>2</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHumanReadableCopyright</key><string>Talks to an HP Jornada / Windows CE 2.x handheld over serial PPP.</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

xattr -cr "$APP" 2>/dev/null || true
codesign --force --sign - "$APP"
codesign --verify "$APP"

TARGET="$HOME/Applications/Jornada Sync.app"
mkdir -p "$HOME/Applications"
rm -rf "$TARGET"
ditto "$APP" "$TARGET"

rm -rf "dist/Jornada Sync.app"
ditto "$APP" "dist/Jornada Sync.app" 2>/dev/null || true
rm -rf "$STAGE"
echo "installed: $TARGET"
