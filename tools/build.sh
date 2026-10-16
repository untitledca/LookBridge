#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
APP_DIR="$PROJECT_DIR/dist/LookBridge.app"
SPARKLE_DIR=$("$SCRIPT_DIR/fetch-sparkle.sh")
mkdir -p "$PROJECT_DIR/.build" "$PROJECT_DIR/dist"
for required in 'LookBridge.py' 'CDL Auto Apply.py' 'Reference Still Auto Overlay.py' 'Reference Still Toggle.py' lookbridge_support.py reference_image.py comparison_capture.py psnr_guidance.py; do
    [[ -f "$PROJECT_DIR/scripts/$required" ]] || { print -u2 "Missing required script: $required"; exit 1; }
done
[[ -f "$PROJECT_DIR/assets/LookBridge.icns" ]] || { print -u2 'Missing assets/LookBridge.icns'; exit 1; }
BUILD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/lookbridge-build.XXXXXX")
trap 'rm -rf "$BUILD_DIR"' EXIT
BUILD_APP="$BUILD_DIR/LookBridge.app"
mkdir -p "$BUILD_APP/Contents/MacOS" "$BUILD_APP/Contents/Resources/Scripts" "$BUILD_APP/Contents/Frameworks"
ditto --noextattr --norsrc "$SPARKLE_DIR/Sparkle.framework" "$BUILD_APP/Contents/Frameworks/Sparkle.framework"
BINARIES=()
for architecture in arm64 x86_64; do
    binary="$BUILD_DIR/LookBridge-$architecture"
    xcrun swiftc -swift-version 5 -O -target "$architecture-apple-macos12.0" \
        -module-cache-path "$PROJECT_DIR/.build/module-cache-$architecture" \
        -F "$SPARKLE_DIR" -framework Sparkle \
        -Xlinker -rpath -Xlinker @executable_path/../Frameworks \
        "$PROJECT_DIR"/macos/Sources/*.swift -o "$binary"
    BINARIES+=("$binary")
done
xcrun lipo -create "${BINARIES[@]}" -output "$BUILD_APP/Contents/MacOS/LookBridge"
cp -X "$PROJECT_DIR/macos/Resources/Info.plist" "$BUILD_APP/Contents/Info.plist"
cp -X "$PROJECT_DIR/assets/LookBridge.icns" "$BUILD_APP/Contents/Resources/LookBridge.icns"
for resource in "$PROJECT_DIR"/macos/Resources/*(N); do
    [[ "${resource:t}" == Info.plist ]] || ditto --noextattr --norsrc "$resource" "$BUILD_APP/Contents/Resources/${resource:t}"
done
for script in "$PROJECT_DIR"/scripts/*.py; do cp -X "$script" "$BUILD_APP/Contents/Resources/Scripts/${script:t}"; done
cp -X "$SPARKLE_DIR/LICENSE" "$BUILD_APP/Contents/Resources/Sparkle-LICENSE.txt"
xattr -dr com.apple.FinderInfo "$BUILD_APP" 2>/dev/null || true
xattr -dr com.apple.ResourceFork "$BUILD_APP" 2>/dev/null || true
codesign --force --sign - "$BUILD_APP"
codesign --verify --deep --strict "$BUILD_APP"
for component in Sparkle Autoupdate Updater.app/Contents/MacOS/Updater XPCServices/Installer.xpc/Contents/MacOS/Installer XPCServices/Downloader.xpc/Contents/MacOS/Downloader; do
    slices=$(xcrun lipo -archs "$BUILD_APP/Contents/Frameworks/Sparkle.framework/Versions/B/$component")
    [[ " $slices " == *" arm64 "* && " $slices " == *" x86_64 "* ]] || { print -u2 "Non-universal Sparkle component: $component"; exit 1; }
done
if [[ -e "$APP_DIR" ]]; then mv "$APP_DIR" "$BUILD_DIR/previous.app"; fi
if ! mv "$BUILD_APP" "$APP_DIR"; then
    [[ ! -e "$BUILD_DIR/previous.app" ]] || mv "$BUILD_DIR/previous.app" "$APP_DIR"
    exit 1
fi
print "Built universal LookBridge app: $APP_DIR"
