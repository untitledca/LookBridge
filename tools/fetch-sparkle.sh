#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
SPARKLE_VERSION=2.10.0
SPARKLE_SHA256=c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c
DESTINATION="$PROJECT_DIR/.build/dependencies/Sparkle-$SPARKLE_VERSION"
if [[ -f "$DESTINATION/.verified-sha256" && "$(cat "$DESTINATION/.verified-sha256")" == "$SPARKLE_SHA256" && -d "$DESTINATION/Sparkle.framework" ]]; then
    print -r -- "$DESTINATION"
    exit 0
fi
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/nacho-sparkle.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    "https://github.com/sparkle-project/Sparkle/releases/download/$SPARKLE_VERSION/Sparkle-$SPARKLE_VERSION.tar.xz" \
    --output "$STAGING/Sparkle.tar.xz"
ACTUAL_SHA256=$(shasum -a 256 "$STAGING/Sparkle.tar.xz")
if [[ "${ACTUAL_SHA256%% *}" != "$SPARKLE_SHA256" ]]; then
    print -u2 "Sparkle download checksum did not match the pinned official release."
    exit 1
fi
mkdir "$STAGING/extracted"
tar -xJf "$STAGING/Sparkle.tar.xz" -C "$STAGING/extracted"
codesign --verify --deep --strict "$STAGING/extracted/Sparkle.framework"
print -r -- "$SPARKLE_SHA256" > "$STAGING/extracted/.verified-sha256"
mkdir -p "${DESTINATION:h}"
if [[ -e "$DESTINATION" ]]; then
    mv "$DESTINATION" "$STAGING/previous"
fi
mv "$STAGING/extracted" "$DESTINATION"
print -r -- "$DESTINATION"
