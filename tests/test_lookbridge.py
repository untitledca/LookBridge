"""Behavioral coverage for Resolve runtime handoff and optional external links."""

from collections import defaultdict
import gc
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import weakref
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(SCRIPTS / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hub = load("tested_lookbridge", "LookBridge.py")
support = load("tested_lookbridge_support", "lookbridge_support.py")
toggle = load("tested_lookbridge_toggle", "Reference Still Toggle.py")


class UI(object):
    def __init__(self, existing=None):
        self.items = {}
        self.existing = existing or {}

    def FindWindow(self, identifier):
        return self.existing.get(identifier)

    def __getattr__(self, name):
        def construct(options=None, children=None):
            if not isinstance(options, dict):
                return object()
            widget = SimpleNamespace(**options)
            if "ID" in options:
                self.items[options["ID"]] = widget
            return widget
        return construct


class Dispatcher(object):
    def __init__(self, ui, clicks):
        self.ui = ui
        self.clicks = iter(clicks)
        self.running = False
        self.visible = False
        self.loop_count = 0
        self.exited = False
        self.window = SimpleNamespace(
            On=defaultdict(SimpleNamespace), GetItems=lambda: ui.items,
            Show=lambda: setattr(self, "visible", True),
            Hide=lambda: setattr(self, "visible", False),
        )

    def AddWindow(self, options, layout):
        return self.window

    def RunLoop(self):
        if self.running:
            raise AssertionError("Nested dispatcher loop")
        self.running = True
        self.exited = False
        self.loop_count += 1
        try:
            self.window.On[next(self.clicks)].Clicked({})
            if not self.exited:
                raise AssertionError("Action did not exit dispatcher loop")
        finally:
            self.running = False

    def ExitLoop(self):
        self.exited = True


class LauncherTests(unittest.TestCase):
    def setUp(self):
        hub._MODULES.clear()

    def test_child_starts_after_launcher_loop_exits_and_launcher_reopens(self):
        ui = UI()
        dispatcher = Dispatcher(ui, ["CDL", "References", "Close"])
        runtime = {"resolve": object(), "fusion": SimpleNamespace(UIManager=ui),
                   "bmd": SimpleNamespace(UIDispatcher=lambda ignored: dispatcher)}
        actions = []

        def child(action, passed_runtime):
            self.assertFalse(dispatcher.running)
            self.assertFalse(dispatcher.visible)
            self.assertIs(passed_runtime, runtime)
            actions.append(action)
            return 0

        with mock.patch.object(hub, "launch_tool", side_effect=child):
            self.assertEqual(hub.show_launcher(runtime), 0)
        self.assertEqual(actions, ["CDL", "References"])
        self.assertEqual(dispatcher.loop_count, 3)
        self.assertFalse(dispatcher.visible)

    def test_closed_native_style_window_cycles_are_collected_before_reopen(self):
        class Window(SimpleNamespace):
            pass

        class NativeStyleUI(UI):
            def FindWindow(self, identifier):
                reference = self.existing.get(identifier)
                return reference() if reference else None

        ui = NativeStyleUI()
        created = []

        def dispatcher_factory(ignored):
            dispatcher = Dispatcher(ui, ["Close"])
            dispatcher.window = Window(**vars(dispatcher.window))
            ui.existing[hub.WINDOW_ID] = weakref.ref(dispatcher.window)
            created.append(weakref.ref(dispatcher))
            return dispatcher

        runtime = {"fusion": SimpleNamespace(UIManager=ui),
                   "bmd": SimpleNamespace(UIDispatcher=dispatcher_factory)}
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            self.assertEqual(hub.show_launcher(runtime), 0)
            self.assertIsNotNone(ui.FindWindow(hub.WINDOW_ID))
            self.assertEqual(hub.show_launcher(runtime), 0)
            self.assertEqual(len(created), 2)
            self.assertIsNone(created[0]())
        finally:
            if was_enabled:
                gc.enable()
            gc.collect()

    def test_stale_native_window_is_ignored_even_when_garbage_collection_cannot_remove_it(self):
        stale = SimpleNamespace(Show=mock.Mock(), Raise=mock.Mock())
        ui = UI({hub.WINDOW_ID: stale, hub.REFERENCE_WINDOW_ID: stale})
        dispatcher = Dispatcher(ui, ["Close"])
        runtime = {"fusion": SimpleNamespace(UIManager=ui),
                   "bmd": SimpleNamespace(UIDispatcher=lambda ignored: dispatcher)}
        self.assertEqual(hub.show_launcher(runtime), 0)
        self.assertEqual(dispatcher.loop_count, 1)
        stale.Show.assert_not_called()
        self.assertIsNone(support.active_window(hub.WINDOW_ID))

    def test_child_failure_returns_to_launcher_with_message(self):
        ui = UI()
        dispatcher = Dispatcher(ui, ["References", "Close"])
        runtime = {"fusion": SimpleNamespace(UIManager=ui),
                   "bmd": SimpleNamespace(UIDispatcher=lambda ignored: dispatcher)}
        with mock.patch.object(hub, "launch_tool", side_effect=RuntimeError("Missing media")):
            self.assertEqual(hub.show_launcher(runtime), 0)
        self.assertIn("Missing media", ui.items["Status"].Text)
        self.assertEqual(dispatcher.loop_count, 2)

    def test_reopening_active_review_focuses_it_without_new_dispatcher(self):
        existing = SimpleNamespace(Show=mock.Mock(), Raise=mock.Mock())
        ui = UI({hub.REFERENCE_WINDOW_ID: existing})
        dispatcher_factory = mock.Mock()
        runtime = {"fusion": SimpleNamespace(UIManager=ui),
                   "bmd": SimpleNamespace(UIDispatcher=dispatcher_factory)}
        support.register_window(hub.REFERENCE_WINDOW_ID, existing)
        try:
            self.assertEqual(hub.show_launcher(runtime), 0)
        finally:
            support.unregister_window(hub.REFERENCE_WINDOW_ID, existing)
        existing.Show.assert_called_once_with()
        existing.Raise.assert_called_once_with()
        dispatcher_factory.assert_not_called()

    def test_load_preserves_and_refreshes_host_globals_without_main_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "example.py").write_text(
                "seen_on_import = resolve\n"
                "if __name__ == '__main__':\n    raise AssertionError('main executed')\n"
            )
            first, second = object(), object()
            with mock.patch.object(hub, "_module_directories", return_value=[folder]):
                module = hub._load_module("example.py", {"resolve": first})
                self.assertIs(module.seen_on_import, first)
                self.assertIs(hub._load_module("example.py", {"resolve": second}), module)
                self.assertIs(module.resolve, second)

    def test_tool_arguments_do_not_inherit_resolve_process_arguments(self):
        module = SimpleNamespace(main=mock.Mock(return_value=0))
        runtime = {"resolve": object(), "fusion": object(), "bmd": object()}
        with mock.patch.object(hub, "_load_module", return_value=module) as loader:
            hub.launch_tool("CDL", runtime)
            module.main.assert_called_with([])
            loader.assert_called_with("CDL Auto Apply.py", runtime)
            hub.launch_tool("Toggle", runtime)
            module.main.assert_called_with(["--toggle", "--notify"])

    def test_installed_menu_entries_find_private_modules(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home, "Library/Application Support/Blackmagic Design/DaVinci Resolve")
            payload = base / "LookBridge/Modules"
            menu = base / "Fusion/Scripts/Utility/LookBridge"
            payload.mkdir(parents=True)
            menu.mkdir(parents=True)
            Path(payload, "example.py").write_text("value = 42\n")
            Path(payload, toggle.SCRIPT_FILENAME).write_text("# test payload\n")
            with mock.patch.dict(os.environ, {"HOME": home}), \
                    mock.patch.object(hub.sys, "platform", "darwin"), \
                    mock.patch.object(hub, "__file__", str(menu / "LookBridge.py")), \
                    mock.patch.object(toggle, "__file__", str(menu / "Reference Still Toggle.py")):
                self.assertEqual(hub._load_module("example.py").value, 42)
                self.assertEqual(toggle._find_main_script(), str(payload / toggle.SCRIPT_FILENAME))

    def test_missing_module_reports_repair_action(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(hub, "_module_directories", return_value=[folder]):
            with self.assertRaisesRegex(RuntimeError, "Reinstall Resolve Tools"):
                hub._load_module("missing.py")


class SupportTests(unittest.TestCase):
    def test_footer_opens_nothing_until_explicit_click_and_selects_right_amount(self):
        ui = UI()
        window = SimpleNamespace(On=defaultdict(SimpleNamespace))
        errors = []
        with mock.patch.object(support, "_open_url") as open_url:
            support.footer(ui)
            support.bind_footer(window, errors.append)
            open_url.assert_not_called()
            for amount, url in support.SUPPORT_OPTIONS:
                window.On["LookBridgeSupport{}".format(amount)].Clicked({})
                open_url.assert_called_with(url)
            window.On["LookBridgeUpdates"].Clicked({})
            open_url.assert_called_with("lookbridge://check-for-updates")
        self.assertEqual(errors, [])

    def test_launch_failure_stays_in_window_and_displays_error(self):
        window = SimpleNamespace(On=defaultdict(SimpleNamespace))
        errors = []
        support.bind_footer(window, errors.append)
        with mock.patch.object(support, "_open_url", side_effect=RuntimeError("No registered app")):
            window.On["LookBridgeUpdates"].Clicked({})
        self.assertEqual(str(errors[0]), "No registered app")

    def test_macos_opens_exact_url_as_argument_without_shell(self):
        url = support.SUPPORT_OPTIONS[0][1]
        with mock.patch.object(support.sys, "platform", "darwin"), \
                mock.patch.object(support.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            support.open_support(5)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", url])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_unknown_support_amount_does_not_open_link(self):
        with mock.patch.object(support, "_open_url") as open_url:
            with self.assertRaises(ValueError):
                support.open_support(500)
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
