"""Persistent palette callbacks with a minimal, noninteractive Fusion UI fake."""

from collections import defaultdict
from contextlib import ExitStack
import importlib.util
import os
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT_PATH = os.path.join(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "Reference Still Auto Overlay.py"
)
SPEC = importlib.util.spec_from_file_location("reference_review_controller", SCRIPT_PATH)
overlay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overlay)


class Widget(SimpleNamespace):
    def AddItem(self, label):
        self.options.append(label)

    def SetFocus(self, reason):
        pass


class UI(object):
    def __init__(self):
        self.items = {}

    def FindWindow(self, identifier):
        return None

    def __getattr__(self, name):
        def construct(options=None, children=None):
            if not isinstance(options, dict):
                return object()
            widget = Widget(CurrentIndex=0, options=[])
            widget.__dict__.update(options)
            if "ID" in options:
                self.items[options["ID"]] = widget
            return widget
        return construct


class Dispatcher(object):
    def __init__(self, ui, callback):
        self.ui, self.callback = ui, callback
        self.visible = False
        self.show_count = 0
        self.hide_count = 0
        self.window = SimpleNamespace(
            On=defaultdict(SimpleNamespace), GetItems=lambda: ui.items,
            Show=self.show, Hide=self.hide, Raise=lambda: None,
        )

    def show(self):
        self.visible = True
        self.show_count += 1

    def hide(self):
        self.visible = False
        self.hide_count += 1

    def AddWindow(self, options, layout):
        return self.window

    def RunLoop(self):
        self.callback(self.ui.items, self.window.On)

    def ExitLoop(self):
        pass


class ReviewControllerTests(unittest.TestCase):
    def setUp(self):
        self.state = {"mode": "normal", "opacity": 50.0, "enabled": False}
        self.resolve = object()
        self.project = object()
        self.timeline = SimpleNamespace(
            GetUniqueId=lambda: "timeline-1",
            GetIsTrackEnabled=lambda kind, index: self.state["enabled"],
        )
        self.point = overlay.ReviewPoint(
            12, "camera.jpg", exact=True, mapped=True, match_uid="source-1"
        )
        self.capture = SimpleNamespace(calculate_review_psnr=mock.Mock(return_value={
            "psnr_db": 37.5, "mse": 255.0 ** 2 / 10 ** (37.5 / 10),
            "width": 1920, "height": 1080, "warnings": []
        }))
        self.guidance = SimpleNamespace(
            PSNR_GUIDE="Higher dB means a closer pixel match; no universal pass threshold.",
            format_psnr_result=lambda metric, filename="", source_timecode="": (
                "PSNR {:.2f} dB · RGB8 {}x{} · {}".format(
                    metric["psnr_db"], metric["width"], metric["height"], filename
                )
            ),
        )
        self.navigation = mock.Mock()
        self.comparison_calls = []
        self.fail_comparison = False

    def summary(self):
        return dict(self.state, states={2: self.state["enabled"]}, locks={2: True}, item_count=1)

    def set_comparison(self, resolve, timeline, indices, mode="normal", opacity=100, enabled=None):
        self.comparison_calls.append((mode, opacity, enabled))
        if self.fail_comparison:
            self.fail_comparison = False
            raise overlay.StillOverlayError("Comparison property write failed and rolled back")
        self.state.update(mode=mode, opacity=opacity)
        if enabled is not None:
            self.state["enabled"] = enabled
        return self.summary()

    def visibility(self, timeline, indices, enabled):
        self.state["enabled"] = enabled
        return enabled

    def toggle(self, timeline, indices):
        return self.visibility(timeline, indices, not self.state["enabled"])

    def run_palette(self, callback, extra=None):
        ui = UI()
        dispatcher = Dispatcher(ui, callback)
        self.dispatcher = dispatcher
        support = overlay._load_helper("lookbridge_support")
        replacements = {
            "_ensure_timecode_navigation_page": lambda resolve: None,
            "_ui_runtime": lambda resolve: (ui, dispatcher),
            "_current_context": lambda resolve: (self.project, self.timeline),
            "find_managed_track_indices": lambda timeline, strict=True: [2],
            "managed_tracks_use_current_spec": lambda timeline, indices: True,
            "current_review_frame": lambda *args: 12,
            "review_frame_to_timecode": lambda *args: "01:00:00:12",
            "set_reference_comparison": self.set_comparison,
            "read_reference_comparison": lambda *args: self.summary(),
            "set_reference_visibility": self.visibility,
            "toggle_reference_tracks": self.toggle,
            "navigate_to_review_point": self.navigation,
            "_load_helper": lambda name: {
                "comparison_capture": self.capture, "psnr_guidance": self.guidance,
                "lookbridge_support": support,
            }.get(name, object()),
        }
        replacements.update(extra or {})
        with ExitStack() as stack:
            for name, value in replacements.items():
                stack.enter_context(mock.patch.object(overlay, name, value))
            overlay.show_review_controller(self.resolve, [self.point], self.timeline)

    def test_optional_support_and_update_links_leave_review_state_unchanged(self):
        support = overlay._load_helper("lookbridge_support")

        def interact(items, events):
            before = self.state.copy()
            calls_before = list(self.comparison_calls)
            events["LookBridgeSupport10"].Clicked({})
            events["LookBridgeUpdates"].Clicked({})
            self.assertEqual(self.state, before)
            self.assertEqual(self.comparison_calls, calls_before)
            self.capture.calculate_review_psnr.assert_not_called()

        with mock.patch.object(support, "open_support") as open_support, \
                mock.patch.object(support, "check_for_updates") as check_updates:
            self.run_palette(interact)
        open_support.assert_called_once_with(10)
        check_updates.assert_called_once_with()

    def test_normal_default_dropdown_and_slider_then_difference_toggle(self):
        def interact(items, events):
            self.assertEqual(items["CompareMode"].options, ["Normal", "Difference"])
            self.assertTrue(items["Measure"].Enabled)
            self.assertEqual(items["Opacity"].Value, 100)
            self.assertEqual(self.state["opacity"], 100)
            items["CompareMode"].CurrentIndex = 1
            events["CompareMode"].CurrentIndexChanged({})
            events["Toggle"].Clicked({})
            self.assertEqual(self.state, {"mode": "difference", "opacity": 100.0, "enabled": True})
            items["Opacity"].Value = 25
            events["Opacity"].ValueChanged({})
            self.assertEqual(self.state["opacity"], 25)
            events["Toggle"].Clicked({})
            self.assertEqual(self.state["mode"], "difference")
            self.assertEqual(self.state["opacity"], 25)
            self.assertFalse(self.state["enabled"])
            items["CompareMode"].CurrentIndex = 0
            events["CompareMode"].CurrentIndexChanged({})
            self.assertEqual(self.state["mode"], "normal")
            self.assertTrue(items["Measure"].Enabled)
            self.capture.calculate_review_psnr.assert_not_called()
        self.run_palette(interact)

    def test_standalone_psnr_measures_in_normal_and_displays_metric(self):
        def interact(items, events):
            self.assertTrue(items["Measure"].Enabled)
            state_before = self.state.copy()
            calls_before = list(self.comparison_calls)
            events["Measure"].Clicked({})
            self.assertEqual(self.capture.calculate_review_psnr.call_count, 1)
            self.assertIn("37.50 dB", items["MetricStatus"].Text)
            self.assertIn("1920x1080", items["MetricStatus"].Text)
            self.assertTrue(items["Measure"].Enabled)
            self.assertEqual(self.state, state_before)
            self.assertEqual(self.comparison_calls, calls_before)
            self.assertTrue(self.dispatcher.visible)
        self.run_palette(interact)

    def test_standalone_psnr_preserves_difference_opacity_and_visibility(self):
        def interact(items, events):
            items["CompareMode"].CurrentIndex = 1
            items["Opacity"].Value = 25
            events["CompareMode"].CurrentIndexChanged({})
            events["OverlayOn"].Clicked({})
            self.assertEqual(self.state, {"mode": "difference", "opacity": 25.0, "enabled": True})
            self.assertTrue(items["Measure"].Enabled)
            self.capture.calculate_review_psnr.assert_not_called()
            calls_before = list(self.comparison_calls)
            events["Measure"].Clicked({})
            self.assertEqual(self.state, {"mode": "difference", "opacity": 25.0, "enabled": True})
            self.assertEqual(items["CompareMode"].CurrentIndex, 1)
            self.assertEqual(items["Opacity"].Value, 25)
            self.assertEqual(self.comparison_calls, calls_before)
            self.assertIn("37.50 dB", items["MetricStatus"].Text)
            events["Toggle"].Clicked({})
            self.assertEqual(self.capture.calculate_review_psnr.call_count, 1)
            self.assertEqual(self.state, {"mode": "difference", "opacity": 25.0, "enabled": False})
        self.run_palette(interact)

    def test_unavailable_psnr_changes_status_only_without_composite_fallback(self):
        self.capture.calculate_review_psnr.side_effect = RuntimeError("Export unavailable")
        def interact(items, events):
            state_before = self.state.copy()
            calls_before = list(self.comparison_calls)
            events["Measure"].Clicked({})
            self.assertEqual(items["CompareMode"].CurrentIndex, 0)
            self.assertEqual(self.state, state_before)
            self.assertEqual(self.comparison_calls, calls_before)
            self.assertIn("PSNR unavailable", items["MetricStatus"].Text)
            self.assertIn("Export unavailable", items["MetricStatus"].Text)
            self.assertNotIn("Difference selected", items["MetricStatus"].Text)
            self.assertTrue(self.dispatcher.visible)
            self.assertTrue(items["Measure"].Enabled)
        self.run_palette(interact)

    def test_critical_restore_failure_does_not_mutate_into_difference(self):
        self.capture.calculate_review_psnr.side_effect = overlay.ReviewStateRestoreError("Cannot restore V2")
        def interact(items, events):
            calls_before = list(self.comparison_calls)
            events["Measure"].Clicked({})
            self.assertEqual(items["CompareMode"].CurrentIndex, 0)
            self.assertEqual(self.state["mode"], "normal")
            self.assertIn("CRITICAL", items["MetricStatus"].Text)
            self.assertEqual(self.comparison_calls, calls_before)
            self.assertTrue(self.dispatcher.visible)
        self.run_palette(interact)

    def test_navigation_and_visibility_callbacks_never_measure_automatically(self):
        def interact(items, events):
            for index in (0, 1):
                items["CompareMode"].CurrentIndex = index
                events["CompareMode"].CurrentIndexChanged({})
                items["Opacity"].Value = 25
                events["Opacity"].ValueChanged({})
                for button in ("OverlayOn", "OverlayOff", "Toggle", "Previous", "Next"):
                    events[button].Clicked({})
            self.assertEqual(self.navigation.call_count, 4)
            self.capture.calculate_review_psnr.assert_not_called()
        self.run_palette(interact)

    def test_capture_hides_palette_and_blocks_reentrant_callbacks_until_restored(self):
        controls = ("CompareMode", "Opacity", "Measure", "OverlayOn", "OverlayOff",
                    "Toggle", "Previous", "Next", "Rebuild", "Close")

        def interact(items, events):
            state_before = self.state.copy()
            calls_before = list(self.comparison_calls)
            original_metric = self.capture.calculate_review_psnr.return_value

            def capture(*args):
                self.assertFalse(self.dispatcher.visible)
                for control in controls:
                    self.assertFalse(items[control].Enabled, control)
                # Fusion may deliver already queued callbacks after a control
                # is disabled. The in-flight guard must reject those too.
                events["CompareMode"].CurrentIndexChanged({})
                events["Opacity"].ValueChanged({})
                for button in ("Measure", "OverlayOn", "OverlayOff", "Toggle", "Previous", "Next", "Rebuild", "Close"):
                    events[button].Clicked({})
                events[overlay.REVIEW_WINDOW_ID].KeyPress({"Text": "]"})
                events[overlay.REVIEW_WINDOW_ID].Close({})
                self.assertEqual(self.state, state_before)
                self.assertEqual(self.comparison_calls, calls_before)
                self.navigation.assert_not_called()
                return original_metric

            self.capture.calculate_review_psnr.side_effect = capture
            events["Measure"].Clicked({})
            self.assertEqual(self.capture.calculate_review_psnr.call_count, 1)
            self.assertTrue(self.dispatcher.visible)
            self.assertEqual(self.dispatcher.hide_count, 1)
            for control in controls:
                self.assertTrue(items[control].Enabled, control)
            self.assertIn("37.50 dB", items["MetricStatus"].Text)
        self.run_palette(interact)

    def test_failed_comparison_resynchronizes_display_to_rolled_back_values(self):
        def interact(items, events):
            self.fail_comparison = True
            items["CompareMode"].CurrentIndex = 1
            items["Opacity"].Value = 20
            events["CompareMode"].CurrentIndexChanged({})
            self.assertEqual(items["CompareMode"].CurrentIndex, 0)
            self.assertEqual(items["Opacity"].Value, 100)
            self.assertIn("Error", items["GrabStatus"].Text)
        self.run_palette(interact)

    def test_rebuild_same_timeline_keeps_controls_consistent_with_new_items(self):
        def build(*args, **kwargs):
            self.state.update(mode="normal", opacity=100, enabled=False)
            return {}, SimpleNamespace(review_points=[self.point]), "/tmp/report.log"
        def interact(items, events):
            items["CompareMode"].CurrentIndex = 1
            items["Opacity"].Value = 20
            events["CompareMode"].CurrentIndexChanged({})
            events["Rebuild"].Clicked({})
            expected_mode = ("normal", "difference")[items["CompareMode"].CurrentIndex]
            self.assertEqual(self.state["mode"], expected_mode)
            self.assertEqual(self.state["opacity"], items["Opacity"].Value)
        self.run_palette(interact, {
            "choose_still_folder": lambda: "/stills",
            "build_from_folder": build,
            "_announce_build_result": lambda *args: None,
        })


if __name__ == "__main__":
    unittest.main()
