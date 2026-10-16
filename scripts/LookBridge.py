#!/usr/bin/env python3
"""LookBridge: one Resolve launcher for CDL matching and reference review.

The launcher stays in Resolve's Python process so injected resolve/fusion/bmd
objects remain available. A child tool starts only after the launcher's UI loop
has returned, and the launcher returns when the tool closes.
"""

from __future__ import print_function

import gc
import importlib.util
import os
import subprocess
import sys


WINDOW_ID = "com.atomfilmlab.LookBridge"
REFERENCE_WINDOW_ID = "com.atomfilmlab.ReferenceStillReview"
TOOL_FILES = {
    "CDL": "CDL Auto Apply.py",
    "References": "Reference Still Auto Overlay.py",
    "Toggle": "Reference Still Auto Overlay.py",
}
_MODULES = {}


def _module_directories():
    directories = []
    if globals().get("__file__"):
        directories.append(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "darwin":
        directories.append(os.path.expanduser(
            "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
            "LookBridge/Modules"
        ))
    elif os.name == "nt":
        directories.append(os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")),
            "Blackmagic Design", "DaVinci Resolve", "Support", "LookBridge", "Modules",
        ))
    else:
        directories.append(os.path.expanduser(
            "~/.local/share/DaVinciResolve/LookBridge/Modules"
        ))
    return directories


def _load_module(filename, runtime=None):
    if filename not in _MODULES:
        for directory in _module_directories():
            path = os.path.join(directory, filename)
            if os.path.isfile(path):
                name = "lookbridge_" + os.path.splitext(filename)[0].replace(" ", "_").lower()
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                for key, value in (runtime or {}).items():
                    setattr(module, key, value)
                spec.loader.exec_module(module)
                _MODULES[filename] = module
                break
        else:
            raise RuntimeError(
                "Missing LookBridge component: {}. Open LookBridge from "
                "Applications and choose Reinstall Resolve Tools.".format(filename)
            )
    module = _MODULES[filename]
    # Refresh host objects when a module is reused in an embedded interpreter.
    for key, value in (runtime or {}).items():
        setattr(module, key, value)
    return module


def _runtime():
    runtime = {name: globals().get(name) for name in ("resolve", "fusion", "bmd")}
    if not runtime["resolve"]:
        # Reuse the existing script's supported Resolve connection discovery.
        cdl = _load_module("CDL Auto Apply.py", runtime)
        runtime["resolve"] = cdl.connect_to_resolve()
    if not runtime["fusion"]:
        runtime["fusion"] = runtime["resolve"].Fusion()
    if not runtime["bmd"]:
        import DaVinciResolveScript
        runtime["bmd"] = DaVinciResolveScript
    if not runtime["fusion"] or not getattr(runtime["fusion"], "UIManager", None):
        raise RuntimeError("LookBridge's popup requires DaVinci Resolve Studio and its Fusion UI Manager")
    return runtime


def launch_tool(action, runtime):
    if action not in TOOL_FILES:
        raise ValueError("Unknown LookBridge action: {}".format(action))
    module = _load_module(TOOL_FILES[action], runtime)
    args = ["--toggle", "--notify"] if action == "Toggle" else []
    return module.main(args)


def create_window(ui, dispatcher, support):
    """Build the palette separately so native runtime layout can be verified."""
    return dispatcher.AddWindow(
        {"ID": WINDOW_ID, "WindowTitle": "LookBridge", "Geometry": [170, 170, 590, 320]},
        ui.VGroup({"Spacing": 12}, [
            ui.Label({"Text": "LookBridge", "Weight": 0}),
            ui.Label({
                "Text": "Bring on-set looks and reference stills into your Resolve timeline.",
                "WordWrap": True, "Weight": 0,
            }),
            ui.Button({"ID": "CDL", "Text": "Match & Apply CDLs…", "MinimumSize": [0, 42], "Weight": 0}),
            ui.Label({"Text": "Match ASC CDL files to source filenames, review the audit, then apply.", "WordWrap": True, "Weight": 0}),
            ui.Button({"ID": "References", "Text": "Reference Stills & Review…", "MinimumSize": [0, 42], "Weight": 0}),
            ui.Label({"Text": "Build matched reference overlays or reopen A/B, Difference, and PSNR review.", "WordWrap": True, "Weight": 0}),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Toggle", "Text": "Toggle Reference A/B"}),
                ui.HGap(1),
                ui.Button({"ID": "Close", "Text": "Close", "Weight": 0}),
            ]),
            ui.Label({"ID": "Status", "Text": "Choose a tool to work with the current timeline.", "WordWrap": True, "MinimumSize": [0, 30], "Weight": 0}),
            support.footer(ui),
        ]),
    )


def show_launcher(runtime):
    ui = runtime["fusion"].UIManager
    # Closed Fusion windows can remain discoverable until callback cycles are
    # collected. The shared registry identifies the actual active UI session.
    gc.collect()
    support = _load_module("lookbridge_support.py")
    # Reopening LookBridge while the modeless review controller is active
    # focuses that controller instead of starting a nested dispatcher.
    for identifier in (REFERENCE_WINDOW_ID, WINDOW_ID):
        existing = support.active_window(identifier)
        if existing:
            existing.Show()
            existing.Raise()
            return 0
    dispatcher = runtime["bmd"].UIDispatcher(ui)
    window = create_window(ui, dispatcher, support)
    items = window.GetItems()
    state = {"action": None}

    def choose(action):
        def clicked(event):
            state["action"] = action
            window.Hide()
            dispatcher.ExitLoop()
        return clicked

    for action in TOOL_FILES:
        window.On[action].Clicked = choose(action)
    window.On["Close"].Clicked = choose(None)
    window.On[WINDOW_ID].Close = choose(None)
    support.bind_footer(window, lambda error: setattr(items["Status"], "Text", str(error)))

    support.register_window(WINDOW_ID, window)
    try:
        while True:
            state["action"] = None
            window.Show()
            dispatcher.RunLoop()
            window.Hide()
            action = state["action"]
            if action is None:
                return 0
            # No child UI loop can overlap this launcher's dispatcher loop.
            try:
                result = launch_tool(action, runtime)
                items["Status"].Text = (
                    "Tool closed. Choose another tool for the current timeline."
                    if not result else "The tool reported an error. See its message or Resolve Console."
                )
            except Exception as error:
                items["Status"].Text = "Could not run the tool: {}".format(error)
    finally:
        support.unregister_window(WINDOW_ID, window)
        window.Hide()


def main():
    try:
        return show_launcher(_runtime())
    except Exception as error:
        print("LookBridge: {}".format(error))
        if sys.platform == "darwin":
            try:
                subprocess.run([
                    "/usr/bin/osascript", "-e",
                    'on run argv\ndisplay alert "LookBridge" message (item 1 of argv)\nend run',
                    str(error),
                ], timeout=120)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    main()
