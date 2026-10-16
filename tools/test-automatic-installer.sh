#!/bin/zsh
# Uses unique app identifiers, a localhost feed and temporary Resolve folders only.
# Requires the dedicated LookBridge Sparkle release key in the login Keychain.
set -eu
SCRIPT_DIR="${0:A:h}"
"$SCRIPT_DIR/fetch-sparkle.sh" >/dev/null
exec python3 "$SCRIPT_DIR/test-automatic-installer.py" "$@"
