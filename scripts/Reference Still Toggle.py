#!/usr/bin/env python3
"""One-click toggle for tracks built by Reference Still Auto Overlay."""

from __future__ import print_function

import os
import runpy


SCRIPT_FILENAME = "Reference Still Auto Overlay.py"


def _find_main_script():
    candidates = []
    runtime_file = globals().get("__file__")
    if runtime_file:
        candidates.append(os.path.join(
            os.path.dirname(os.path.abspath(runtime_file)), SCRIPT_FILENAME
        ))
    candidates.extend([
        os.path.expanduser(
            "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
            "LookBridge/Modules/" + SCRIPT_FILENAME
        ),
        os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")),
            "Blackmagic Design", "DaVinci Resolve", "Support", "LookBridge",
            "Modules", SCRIPT_FILENAME,
        ),
        os.path.expanduser(
            "~/.local/share/DaVinciResolve/LookBridge/Modules/" + SCRIPT_FILENAME
        ),
        os.path.expanduser(
            "~/Library/Application Support/Blackmagic Design/DaVinci "
            "Resolve/Fusion/Scripts/Utility/" + SCRIPT_FILENAME
        ),
        os.path.join(
            os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            "Blackmagic Design", "DaVinci Resolve", "Fusion", "Scripts",
            "Utility", SCRIPT_FILENAME,
        ),
        os.path.join(
            "/opt/resolve/Fusion/Scripts/Utility", SCRIPT_FILENAME
        ),
        os.path.join(os.getcwd(), SCRIPT_FILENAME),
    ])
    seen = set()
    for candidate in candidates:
        normalized = os.path.abspath(os.path.expanduser(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isfile(normalized):
            return normalized
    raise RuntimeError(
        "Could not locate '{}'; open LookBridge and choose Reinstall Resolve Tools"
        .format(SCRIPT_FILENAME)
    )


def main():
    namespace = runpy.run_path(
        _find_main_script(),
        init_globals={
            "resolve": globals().get("resolve"),
            "fusion": globals().get("fusion"),
            "bmd": globals().get("bmd"),
        },
        run_name="reference_still_auto_overlay",
    )
    return namespace["main"](["--toggle", "--notify"])


if __name__ == "__main__":
    main()
