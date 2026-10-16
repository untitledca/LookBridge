import copy
import importlib.util
import os
from types import SimpleNamespace
import unittest
from unittest import mock


PATH = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "comparison_capture.py")
SPEC = importlib.util.spec_from_file_location("reference_comparison_capture", PATH)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class CaptureError(RuntimeError):
    pass


class RestoreError(CaptureError):
    pass


class Item:
    def __init__(self, uid, name, start, end):
        self.uid, self.name, self.start, self.end = uid, name, start, end
        self.properties = {"CompositeMode": 3, "Opacity": 42.0, "CompositeEnabled": False}
        self.fail_restore = False

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetProperty(self, name):
        return self.properties.get(name)

    def SetProperty(self, name, value):
        if self.fail_restore and name == "Opacity" and value == 42:
            return True  # A lying API must not be accepted as a restoration.
        self.properties[name] = value
        return True


class Timeline:
    def __init__(self):
        self.items = {
            1: [Item("camera-uid", "shot.mov", 1000, 1010)],
            2: [Item("reference-uid", "[AUTO REF] shot.jpg", 1005, 1006)],
            3: [Item("title-uid", "Title", 1000, 1010)],
            4: [],
        }
        self.states = {("video", 1): True, ("video", 2): False,
                       ("video", 3): True, ("video", 4): True,
                       ("subtitle", 1): True}
        self.locks = {1: False, 2: True, 3: False, 4: True}
        self.tc = "01:00:00:00"
        self.fail_restore_tc = False
        self.fail_restore_visibility = False

    def GetUniqueId(self):
        return "timeline-uid"

    def GetStartFrame(self):
        return 1000

    def GetTrackCount(self, kind):
        return 4 if kind == "video" else 1

    def GetItemListInTrack(self, kind, index):
        return self.items[index]

    def GetIsTrackEnabled(self, kind, index):
        return self.states[(kind, index)]

    def SetTrackEnable(self, kind, index, enabled):
        if self.fail_restore_visibility and (kind, index) == ("video", 4) and enabled:
            return False
        self.states[(kind, index)] = bool(enabled)
        return True

    def GetIsTrackLocked(self, kind, index):
        return self.locks[index]

    def SetTrackLock(self, kind, index, locked):
        self.locks[index] = bool(locked)
        return True

    def GetCurrentTimecode(self):
        return self.tc

    def SetCurrentTimecode(self, tc):
        if self.fail_restore_tc and tc == "01:00:00:00":
            return True
        self.tc = tc
        return True


class Resolve:
    def __init__(self):
        self.page = "fusion"
        self.fail_restore = False

    def GetCurrentPage(self):
        return self.page

    def OpenPage(self, page):
        if self.fail_restore and page == "fusion":
            return True
        self.page = page
        return True


class Project:
    def __init__(self, timeline):
        self.timeline = timeline
        self.exports = []
        self.fail_export = 0
        self.missing_export = False

    def GetCurrentTimeline(self):
        return self.timeline

    def ExportCurrentFrameAsStill(self, path):
        self.exports.append({"states": dict(self.timeline.states),
                             "properties": dict(self.timeline.items[2][0].properties),
                             "timecode": self.timeline.tc, "path": path})
        if self.fail_export == len(self.exports):
            return False
        if not self.missing_export:
            with open(path, "wb") as output:
                output.write(b"PNG stand-in")
        return True


class Engine:
    def __init__(self):
        self.calls = []
        self.fail = False

    def calculate_psnr(self, a, b):
        self.calls.append((a, b))
        if self.fail:
            raise ValueError("different frame dimensions")
        return {"psnr_db": 31.25, "mse": 49.0, "width": 1920,
                "height": 1080, "channels": 3, "sample_count": 6220800}


class PsnrCaptureTests(unittest.TestCase):
    def setUp(self):
        self.timeline = Timeline()
        self.resolve = Resolve()
        self.project = Project(self.timeline)
        self.engine = Engine()
        self.point = SimpleNamespace(mapped=True, exact=True, match_uid="camera-uid",
                                     frame_id=5, filename="shot.jpg", reason="verified")
        self.nav = SimpleNamespace(StillOverlayError=CaptureError,
                                   ReviewStateRestoreError=RestoreError,
                                   ITEM_PREFIX="[AUTO REF] ",
                                   validate_managed_track=lambda timeline, index: (True, ""),
                                   review_frame_to_timecode=lambda project, timeline, frame: "01:00:00:05")
        self.sleep = mock.patch.object(capture.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def run_capture(self):
        return capture.calculate_review_psnr(
            self.resolve, self.project, self.timeline, [2, 4], self.point,
            self.engine, self.nav
        )

    def snapshot(self):
        return copy.deepcopy((self.timeline.states, self.timeline.locks,
                              self.timeline.items[2][0].properties,
                              self.timeline.tc, self.resolve.page))

    def test_captures_source_and_reference_without_self_overlay_and_restores(self):
        original = self.snapshot()
        result = self.run_capture()
        self.assertEqual(result["psnr_db"], 31.25)
        self.assertEqual((result["source_track"], result["reference_track"]), (1, 2))
        self.assertEqual(result["timecode"], "01:00:00:05")
        self.assertEqual(len(self.project.exports), 2)
        camera, reference = self.project.exports
        self.assertEqual([key for key, state in camera["states"].items() if state], [("video", 1)])
        self.assertEqual([key for key, state in reference["states"].items() if state], [("video", 2)])
        self.assertEqual(reference["properties"], {"CompositeMode": 0, "Opacity": 100,
                                                   "CompositeEnabled": True})
        self.assertEqual(camera["timecode"], reference["timecode"])
        self.assertEqual(self.snapshot(), original)
        self.assertFalse(os.path.exists(os.path.dirname(camera["path"])))

    def test_measurement_does_not_depend_on_slider_or_reference_visibility(self):
        self.timeline.items[2][0].properties["Opacity"] = 0
        self.timeline.states[("video", 2)] = True
        original = self.snapshot()
        self.run_capture()
        self.assertEqual(self.project.exports[1]["properties"]["Opacity"], 100)
        self.assertEqual(self.snapshot(), original)

    def test_metadata_mapping_caveat_is_returned(self):
        self.point.exact = False
        self.point.reason = "No readable camera burn-in"
        result = self.run_capture()
        self.assertTrue(any("metadata-mapped" in warning for warning in result["warnings"]))

    def test_unmapped_point_fails_before_mutation(self):
        self.point.mapped = False
        original = self.snapshot()
        with self.assertRaisesRegex(CaptureError, "no mapped"):
            self.run_capture()
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(self.project.exports, [])

    def test_stale_source_uid_fails_before_mutation(self):
        self.point.match_uid = "old-uid"
        with self.assertRaisesRegex(CaptureError, "source clip is missing"):
            self.run_capture()
        self.assertEqual(self.project.exports, [])

    def test_reference_must_be_exactly_one_frame_and_above_same_source(self):
        self.timeline.items[2][0].end = 1010
        with self.assertRaisesRegex(CaptureError, "one-frame reference"):
            self.run_capture()
        self.assertEqual(self.project.exports, [])

    def test_reference_filename_must_match_marker(self):
        self.point.filename = "unrelated.jpg"
        with self.assertRaisesRegex(CaptureError, "one-frame reference"):
            self.run_capture()

    def test_source_disabled_refuses_misleading_measurement(self):
        self.timeline.states[("video", 1)] = False
        with self.assertRaisesRegex(CaptureError, "source track is disabled"):
            self.run_capture()

    def test_both_export_failures_restore_all_state(self):
        for fail_export in (1, 2):
            with self.subTest(fail_export=fail_export):
                self.project.exports = []
                self.project.fail_export = fail_export
                original = self.snapshot()
                with self.assertRaisesRegex(CaptureError, "export is unavailable"):
                    self.run_capture()
                self.assertEqual(self.snapshot(), original)

    def test_export_claim_without_file_is_not_accepted(self):
        self.project.missing_export = True
        original = self.snapshot()
        with self.assertRaisesRegex(CaptureError, "did not create"):
            self.run_capture()
        self.assertEqual(self.snapshot(), original)

    def test_engine_dimension_failure_restores_all_state(self):
        self.engine.fail = True
        original = self.snapshot()
        with self.assertRaisesRegex(CaptureError, "different frame dimensions"):
            self.run_capture()
        self.assertEqual(self.snapshot(), original)

    def test_unproven_opacity_restoration_is_critical(self):
        self.timeline.items[2][0].fail_restore = True
        with self.assertRaisesRegex(RestoreError, "restore Opacity"):
            self.run_capture()
        self.assertTrue(self.timeline.locks[2])
        self.assertEqual(self.timeline.tc, "01:00:00:00")

    def test_unproven_playhead_restoration_is_critical(self):
        self.timeline.fail_restore_tc = True
        with self.assertRaisesRegex(RestoreError, "restore playhead"):
            self.run_capture()

    def test_unproven_page_restoration_is_critical(self):
        self.resolve.fail_restore = True
        with self.assertRaisesRegex(RestoreError, "restore page"):
            self.run_capture()

    def test_visibility_restore_failure_is_critical_and_other_state_restored(self):
        self.timeline.fail_restore_visibility = True
        with self.assertRaisesRegex(RestoreError, "restore video 4"):
            self.run_capture()
        self.assertTrue(self.timeline.states[("video", 3)])
        self.assertTrue(self.timeline.states[("subtitle", 1)])
        self.assertEqual(self.timeline.items[2][0].properties["Opacity"], 42)


if __name__ == "__main__":
    unittest.main()
