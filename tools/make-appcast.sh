#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
APP_DIR="$PROJECT_DIR/dist/LookBridge.app"
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_DIR/Contents/Info.plist")
DMG="$PROJECT_DIR/dist/LookBridge-$VERSION-universal.dmg"
NOTES="${1:-}"
ACCOUNT=com.atomfilmlab.lookbridge.sparkle
SPARKLE_DIR=$("$SCRIPT_DIR/fetch-sparkle.sh")
if [[ ! -f "$DMG" || -z "$NOTES" || ! -f "$NOTES" ]]; then
    print -u2 "Usage: tools/make-appcast.sh /path/to/release-notes.html (after build.sh and make-dmg.sh)"
    exit 1
fi
PUBLIC_KEY=$("$SPARKLE_DIR/bin/generate_keys" --account "$ACCOUNT" -p)
EXPECTED_KEY=$(/usr/libexec/PlistBuddy -c 'Print :SUPublicEDKey' "$APP_DIR/Contents/Info.plist")
if [[ "$PUBLIC_KEY" != "$EXPECTED_KEY" ]]; then
    print -u2 "The signing key does not match this app. Restore the existing LookBridge signing key; do not generate a replacement."
    exit 1
fi
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/lookbridge-appcast.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT
cp -X "$DMG" "$STAGING/${DMG:t}"
cp -X "$NOTES" "$STAGING/${DMG:t:r}.html"
"$SPARKLE_DIR/bin/generate_appcast" --account "$ACCOUNT" \
    --download-url-prefix "https://github.com/untitledca/LookBridge/releases/download/v$VERSION/" \
    --link 'https://github.com/untitledca/LookBridge' \
    --embed-release-notes --maximum-deltas 0 "$STAGING"
xcrun swift "$PROJECT_DIR/tools/verify-sparkle-update.swift" "$PUBLIC_KEY" "$STAGING/appcast.xml" "$DMG"
FEED_VERSION=$(xmllint --xpath 'string(//*[local-name()="version"])' "$STAGING/appcast.xml")
BUILD_VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP_DIR/Contents/Info.plist")
FEED_URL=$(xmllint --xpath 'string(//enclosure/@url)' "$STAGING/appcast.xml")
FEED_LENGTH=$(xmllint --xpath 'string(//enclosure/@length)' "$STAGING/appcast.xml")
if [[ "$FEED_VERSION" != "$BUILD_VERSION" || "$FEED_URL" != "https://github.com/untitledca/LookBridge/releases/download/v$VERSION/${DMG:t}" || "$FEED_LENGTH" != "$(stat -f %z "$DMG")" ]]; then
    print -u2 "The generated appcast does not match the built app and installer."
    exit 1
fi
cp -X "$STAGING/appcast.xml" "$PROJECT_DIR/dist/appcast.xml"
print 'Created dist/appcast.xml. Upload it and the unchanged DMG/checksum to the SAME draft release before publishing.'
