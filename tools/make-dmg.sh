#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
APP_DIR="${1:-$PROJECT_DIR/dist/LookBridge.app}"
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_DIR/Contents/Info.plist")
OUTPUT="${2:-$PROJECT_DIR/dist/LookBridge-$VERSION-universal.dmg}"
architectures=$(xcrun lipo -archs "$APP_DIR/Contents/MacOS/LookBridge")
[[ " $architectures " == *" arm64 "* && " $architectures " == *" x86_64 "* ]] || { print -u2 'The DMG requires a universal app.'; exit 1; }
codesign --verify --deep --strict "$APP_DIR"
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/lookbridge-dmg.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT
mkdir -p "$STAGING/LookBridge" "${OUTPUT:h}"
ditto --noextattr --norsrc "$APP_DIR" "$STAGING/LookBridge/LookBridge.app"
cp -X "$PROJECT_DIR/macos/Resources/Install.txt" "$STAGING/LookBridge/Start Here.txt"
ln -s /Applications "$STAGING/LookBridge/Applications"
cp -X "$PROJECT_DIR/assets/LookBridge.icns" "$STAGING/LookBridge/.VolumeIcon.icns"
if SETFILE=$(xcrun --find SetFile 2>/dev/null); then "$SETFILE" -a C "$STAGING/LookBridge"; fi
codesign --verify --deep --strict "$STAGING/LookBridge/LookBridge.app"
hdiutil create -volname LookBridge -srcfolder "$STAGING/LookBridge" -fs HFS+ -format UDZO -imagekey zlib-level=9 -ov "$OUTPUT"
hdiutil verify "$OUTPUT"
(cd "${OUTPUT:h}" && shasum -a 256 "${OUTPUT:t}") > "$OUTPUT.sha256"
print "Created $OUTPUT"
