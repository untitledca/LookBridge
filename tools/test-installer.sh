#!/bin/zsh
set -eu
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/lookbridge-test.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT
xcrun swiftc -swift-version 5 "$PROJECT_DIR/macos/Sources/ScriptInstaller.swift" "$PROJECT_DIR/macos/tests/InstallerTests.swift" -o "$STAGING/test-installer"
"$STAGING/test-installer" "$PROJECT_DIR/scripts"

xcrun swiftc -swift-version 5 "$PROJECT_DIR/macos/Sources/ScriptInstaller.swift" "$PROJECT_DIR/macos/Sources/AppModel.swift" "$PROJECT_DIR/macos/tests/AppModelTests.swift" -o "$STAGING/test-app-model"
"$STAGING/test-app-model" "$PROJECT_DIR/scripts"
