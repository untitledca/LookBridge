"""Exercise comparison transactions, including Resolve partial-write failures."""

import importlib.util
import os
import unittest


SCRIPT_PATH = os.path.join(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "Reference Still Auto Overlay.py"
)
SPEC = importlib.util.spec_from_file_location("reference_comparison_controls", SCRIPT_PATH)
overlay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overlay)


class ResolveConstants(object):
    COMPOSITE_NORMAL = None
    COMPOSITE_DIFF = None


class ReferenceItem(object):
    def __init__(self, mode=0, opacity=50):
        self.properties = {"CompositeMode": mode, "Opacity": opacity}
        self.timeline = None
        self.track = None
        self.failure = None
        self.silent_failure = None
        self.writes = []

    def GetProperty(self, name):
        return self.properties.get(name)

    def SetProperty(self, name, value):
        if self.timeline.locked[self.track]:
            return False
        self.writes.append((name, value))
        if self.silent_failure == (name, value):
            self.silent_failure = None
            return True
        self.properties[name] = value
        if self.failure == (name, value):
            self.failure = None
            return False
        return True


class ReferenceTimeline(object):
    def __init__(self, tracks):
        self.tracks = dict(enumerate(tracks, 1))
        self.locked = {index: True for index in self.tracks}
        self.enabled = {index: False for index in self.tracks}
        self.writes = []
        self.read_counts = {index: 0 for index in self.tracks}
        self.iterator_failure = None
        self.enable_failure = None
        self.lock_failure = None
        for index, items in self.tracks.items():
            for item in items:
                item.timeline, item.track = self, index

    def GetItemListInTrack(self, kind, index):
        self.read_counts[index] += 1
        if self.iterator_failure == (index, self.read_counts[index]):
            def broken_iterator():
                yield self.tracks[index][0]
                raise RuntimeError("item enumeration failed")
            return broken_iterator()
        return self.tracks[index]

    def GetIsTrackLocked(self, kind, index):
        return self.locked[index]

    def GetIsTrackEnabled(self, kind, index):
        return self.enabled[index]

    def SetTrackLock(self, kind, index, value):
        self.writes.append(("locked", index, value))
        if self.lock_failure == (index, value):
            self.lock_failure = None
            return False
        self.locked[index] = value
        return True

    def SetTrackEnable(self, kind, index, value):
        self.writes.append(("enabled", index, value))
        self.enabled[index] = value
        if self.enable_failure == (index, value):
            self.enable_failure = None
            return False
        return True


class ComparisonControlsTests(unittest.TestCase):
    def setUp(self):
        self.resolve = ResolveConstants()
        self.first = ReferenceItem()
        self.second = ReferenceItem(mode=3, opacity=25)
        self.timeline = ReferenceTimeline([[self.first], [self.second]])

    def assert_original_state(self):
        self.assertEqual(self.first.properties, {"CompositeMode": 0, "Opacity": 50})
        self.assertEqual(self.second.properties, {"CompositeMode": 3, "Opacity": 25})
        self.assertEqual(self.timeline.locked, {1: True, 2: True})
        self.assertEqual(self.timeline.enabled, {1: False, 2: False})

    def test_normal_defaults_to_full_opacity_and_preserves_track_states(self):
        self.timeline.locked[2] = False
        self.timeline.enabled[2] = True
        result = overlay.set_reference_comparison(
            self.resolve, self.timeline, iter([1, 2])
        )
        self.assertEqual(result["mode"], "normal")
        self.assertEqual(result["opacity"], 100)
        self.assertEqual(result["locks"], {1: True, 2: False})
        self.assertEqual(result["states"], {1: False, 2: True})
        self.assertEqual(result["item_count"], 2)

    def test_difference_mode_with_slider_opacity_and_visibility(self):
        result = overlay.set_reference_comparison(
            self.resolve, self.timeline, [1, 2], "difference", 37.5, True
        )
        self.assertEqual(result["mode"], "difference")
        self.assertEqual(result["opacity"], 37.5)
        self.assertEqual(result["states"], {1: True, 2: True})
        self.assertEqual(result["locks"], {1: True, 2: True})
        self.assertEqual(self.first.properties["CompositeMode"], 3)
        overlay.set_reference_comparison(
            self.resolve, self.timeline, [1, 2], "normal", 0, False
        )
        self.assertEqual(self.first.properties["Opacity"], 0)
        self.assertFalse(self.timeline.enabled[1])

    def test_reader_reports_mixed_properties_without_writes(self):
        result = overlay.read_reference_comparison(self.resolve, self.timeline, [1, 2])
        self.assertEqual(result["mode"], "mixed")
        self.assertIsNone(result["opacity"])
        self.assertFalse(result["enabled"])
        self.assertEqual(self.timeline.writes, [])

    def test_partial_property_failure_rolls_back_all_items(self):
        self.second.failure = ("Opacity", 100.0)
        with self.assertRaises(overlay.StillOverlayError):
            overlay.set_reference_comparison(self.resolve, self.timeline, [1, 2])
        self.assert_original_state()

    def test_silent_property_failure_is_detected_and_rolled_back(self):
        self.second.silent_failure = ("Opacity", 100.0)
        with self.assertRaises(overlay.StillOverlayError):
            overlay.set_reference_comparison(self.resolve, self.timeline, [1, 2])
        self.assert_original_state()

    def test_partial_visibility_failure_restores_both_tracks_and_properties(self):
        self.timeline.enable_failure = (2, True)
        with self.assertRaises(overlay.StillOverlayError):
            overlay.set_reference_comparison(
                self.resolve, self.timeline, [1, 2], "difference", 100, True
            )
        self.assert_original_state()

    def test_relock_failure_rolls_back_and_retries_original_lock(self):
        self.timeline.lock_failure = (2, True)
        with self.assertRaisesRegex(overlay.StillOverlayError, "lock"):
            overlay.set_reference_comparison(self.resolve, self.timeline, [1, 2])
        self.assert_original_state()

    def test_preflight_iterator_failure_makes_no_changes(self):
        self.timeline.iterator_failure = (2, 1)
        with self.assertRaisesRegex(overlay.StillOverlayError, "read reference items"):
            overlay.set_reference_comparison(self.resolve, self.timeline, [1, 2])
        self.assertEqual(self.timeline.writes, [])
        self.assert_original_state()

    def test_final_iterator_failure_rolls_back_instead_of_leaving_half_state(self):
        self.timeline.iterator_failure = (2, 2)
        with self.assertRaisesRegex(overlay.StillOverlayError, "read reference items"):
            overlay.set_reference_comparison(
                self.resolve, self.timeline, [1, 2], enabled=True
            )
        self.assert_original_state()

    def test_unreadable_property_aborts_before_any_unlock(self):
        self.second.properties["Opacity"] = None
        with self.assertRaisesRegex(overlay.StillOverlayError, "could not read"):
            overlay.set_reference_comparison(self.resolve, self.timeline, [1, 2])
        self.assertEqual(self.timeline.writes, [])
        self.assertEqual(self.first.writes, [])

    def test_invalid_requested_values_abort_before_any_change(self):
        for value in (-1, 101, float("nan"), float("inf"), "invalid"):
            with self.subTest(opacity=value):
                with self.assertRaises(overlay.StillOverlayError):
                    overlay.set_reference_comparison(
                        self.resolve, self.timeline, [1, 2], opacity=value
                    )
        with self.assertRaises(overlay.StillOverlayError):
            overlay.set_reference_comparison(
                self.resolve, self.timeline, [1, 2], mode="psnr"
            )
        self.assertEqual(self.timeline.writes, [])
        self.assert_original_state()


if __name__ == "__main__":
    unittest.main()
