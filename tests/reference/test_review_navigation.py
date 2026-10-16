import importlib.util
import json
import os
import tempfile
import unittest


SCRIPT_PATH = os.path.join(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "Reference Still Auto Overlay.py"
)
SPEC = importlib.util.spec_from_file_location(
    "reference_still_auto_overlay_review", SCRIPT_PATH
)
overlay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overlay)


class FakeMediaPoolItem(object):
    def __init__(self, properties):
        self.properties = dict(properties)

    def GetClipProperty(self, name=None):
        if name is None:
            return dict(self.properties)
        return self.properties.get(name, "")


class FakeSourceItem(object):
    def __init__(self, media, source_start_frames, source_duration,
                 actual_rate=24000.0 / 1001.0, speed=100.0):
        self.media = media
        self.source_start_time = float(source_start_frames) / actual_rate
        self.source_end_time = (
            float(source_start_frames + source_duration) / actual_rate
        )
        self.speed = speed

    def GetMediaPoolItem(self):
        return self.media

    def GetSourceStartTime(self):
        return self.source_start_time

    def GetSourceEndTime(self):
        return self.source_end_time

    def GetSpeed(self):
        return {"Percentage": self.speed}


class FakeTimeline(object):
    def __init__(self, start_frame=86400, markers=None,
                 fail_custom_delete=False):
        self.start_frame = start_frame
        self.markers = dict(markers or {})
        self.fail_custom_delete = bool(fail_custom_delete)

    def GetStartFrame(self):
        return self.start_frame

    def GetMarkers(self):
        return {key: dict(value) for key, value in self.markers.items()}

    def DeleteMarkerByCustomData(self, custom_data):
        if self.fail_custom_delete:
            return False
        for frame_id, info in list(self.markers.items()):
            if info.get("customData") == custom_data:
                del self.markers[frame_id]
                return True
        return False

    def DeleteMarkerAtFrame(self, frame_id):
        if frame_id not in self.markers:
            return False
        del self.markers[frame_id]
        return True

    def AddMarker(self, frame_id, color, name, note, duration, custom_data=None):
        if frame_id in self.markers:
            return False
        self.markers[frame_id] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": duration,
            "customData": custom_data or "",
        }
        return True


class FakeVerificationTimeline(object):
    def __init__(self, fail_restore_track=None):
        self.states = {1: True, 2: True}
        self.subtitle_states = {1: True}
        self.fail_restore_track = fail_restore_track
        self.current_timecode = "01:00:00:00"

    def GetTrackCount(self, track_type):
        if track_type == "video":
            return len(self.states)
        if track_type == "subtitle":
            return len(self.subtitle_states)
        return 0

    def GetIsTrackEnabled(self, track_type, track_index):
        states = self.states if track_type == "video" else self.subtitle_states
        return states[track_index]

    def SetTrackEnable(self, track_type, track_index, enabled):
        if (
            track_type == "video" and enabled
            and track_index == self.fail_restore_track
        ):
            return False
        states = self.states if track_type == "video" else self.subtitle_states
        states[track_index] = bool(enabled)
        return True

    def GetSetting(self, name):
        if name == "timelineFrameRate":
            return "24"
        if name == "timelineDropFrameTimecode":
            return "0"
        return ""

    def GetStartTimecode(self):
        return "01:00:00:00"

    def SetCurrentTimecode(self, timecode):
        self.current_timecode = timecode
        return True

    def GetCurrentTimecode(self):
        return self.current_timecode


class FakeVerificationProject(object):
    def __init__(self, timeline=None):
        self.timeline = timeline
        self.export_states = None

    def ExportCurrentFrameAsStill(self, path):
        if self.timeline is not None:
            self.export_states = (
                dict(self.timeline.states),
                dict(self.timeline.subtitle_states),
            )
        with open(path, "wb") as output:
            output.write(b"not-empty")
        return True


class FakePageResolve(object):
    def __init__(self, page):
        self.page = page
        self.opened = []

    def GetCurrentPage(self):
        return self.page

    def OpenPage(self, page):
        self.page = page
        self.opened.append(page)
        return True


def make_match(start=94358, end=95108, source_start_frames=319875,
               source_duration=750, start_tc="03:42:08:03",
               still_name="B188C010_260828RU.jpg", speed=100.0):
    media = FakeMediaPoolItem({
        "Start TC": start_tc,
        "Frames": str(source_duration),
        "FPS": 23.976,
        "Drop frame": "0",
    })
    item = FakeSourceItem(
        media, source_start_frames, source_duration, speed=speed
    )
    still = overlay.StillRecord("/references/" + still_name, ("unused",))
    return overlay.ClipMatch(
        item=item,
        track_index=1,
        start=start,
        end=end,
        uid="source-item-1",
        display_name="B188C010_260828RU.mov",
        source_names=("/camera/B188C010_260828RU.mov",),
        source_keys=("b188c010_260828ru",),
        still=still,
    )


class ReviewNavigationTests(unittest.TestCase):
    def test_23976_timecode_math_and_exact_rate_normalization(self):
        self.assertAlmostEqual(
            overlay.exact_frame_rate(23.976), 24000.0 / 1001.0
        )
        start = overlay.timecode_to_frames("03:42:08:03", 24, False)
        capture = overlay.timecode_to_frames("03:42:23:18", 24, False)
        self.assertEqual(start, 319875)
        self.assertEqual(capture, 320250)
        self.assertEqual(capture - start, 375)

    def test_drop_frame_roundtrips_boundaries_and_rejects_dropped_labels(self):
        vectors = {
            "00:00:59;29": 1799,
            "00:01:00;02": 1800,
            "00:09:59;29": 17981,
            "00:10:00;00": 17982,
            "01:00:00;00": 107892,
        }
        for timecode, frame_number in vectors.items():
            self.assertEqual(
                overlay.timecode_to_frames(timecode, 30, True), frame_number
            )
            self.assertEqual(
                overlay.frames_to_timecode(frame_number, 30, True), timecode
            )
        for invalid in ("00:01:00;00", "00:01:00;01"):
            with self.assertRaises(overlay.StillOverlayError):
                overlay.timecode_to_frames(invalid, 30, True)

    def test_three_digit_frame_field_at_120fps(self):
        frame_number = overlay.timecode_to_frames(
            "00:00:01:119", 120, False
        )
        self.assertEqual(frame_number, 239)
        self.assertEqual(
            overlay.frames_to_timecode(frame_number, 120, False),
            "00:00:01:119",
        )
        self.assertIn(
            "00:00:01:119",
            {value for _, value in overlay._timecode_variants(
                ["TC 00:00:01:119"], 120, False
            )},
        )

    def test_full_frame_candidate_parsing_is_position_independent(self):
        before = ["03:42:23:18", "Fixed-23.98 FPS", "B188C010"]
        after = ["Fixed-23.98 FPS", "B188C010", "03:42:23:18"]
        self.assertEqual(
            overlay._timecode_variants(before, 24, False),
            overlay._timecode_variants(after, 24, False),
        )
        noisy = overlay._timecode_variants(
            ["03:10:54: 06", "23: 44:13:11"], 24, False
        )
        values = {value for _, value in noisy}
        self.assertIn("03:10:54:06", values)
        self.assertIn("23:44:13:11", values)

    def test_vision_subprocess_decodes_non_ascii_ocr_as_utf8(self):
        captured = {}

        class FakeProcess(object):
            returncode = 0

            def __init__(self, command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs

            def communicate(self):
                payload = [{
                    "path": "/references/Sony.jpg",
                    "texts": ["• Rec", "03:42:23:18"],
                    "observations": [{
                        "text": "• Rec",
                        "bbox": [0.1, 0.1, 0.2, 0.1],
                    }],
                }]
                return json.dumps(payload, ensure_ascii=False), ""

        original_popen = overlay.subprocess.Popen
        original_platform = overlay.sys.platform
        original_isfile = overlay.os.path.isfile
        overlay.subprocess.Popen = FakeProcess
        overlay.sys.platform = "darwin"
        overlay.os.path.isfile = lambda path: (
            True if path == "/usr/bin/osascript" else original_isfile(path)
        )
        try:
            result, warnings = overlay.recognize_reference_text([
                "/references/Sony.jpg"
            ])
        finally:
            overlay.subprocess.Popen = original_popen
            overlay.sys.platform = original_platform
            overlay.os.path.isfile = original_isfile

        self.assertEqual(warnings, [])
        self.assertEqual(
            result[overlay._canonical_path("/references/Sony.jpg")][0]["text"],
            "• Rec",
        )
        self.assertEqual(captured["kwargs"]["encoding"], "utf-8")
        self.assertEqual(captured["kwargs"]["errors"], "replace")

    def test_source_range_validation_repairs_missing_hour_tens(self):
        start = overlay.timecode_to_frames("23:43:50:00", 24, False)
        match = make_match(
            end=94358 + 1200,
            source_start_frames=start,
            source_duration=1200,
            start_tc="23:43:50:00",
        )
        record, source_tc, exact, reason = overlay.locate_capture_in_match(
            match, ["TC", "3:44:13:12"]
        )
        self.assertEqual(source_tc, "23:44:13:12")
        self.assertTrue(exact)
        self.assertEqual(reason, "")
        self.assertEqual(record, match.start + (23 * 24 + 12))

    def test_visual_verification_detects_one_frame_burnin_correction(self):
        offset, reason = overlay._nearby_visual_timecode_offset(
            "23:15:02:12", ["TC", "23:15:02:11", "D037C001"],
            24, False,
        )
        self.assertEqual(offset, 1)
        self.assertEqual(reason, "")

    def test_boundary_tc_is_tentative_only_when_visual_check_is_available(self):
        match = make_match()
        media_start = overlay.timecode_to_frames("03:42:08:03", 24, False)
        before_start = overlay.frames_to_timecode(
            media_start - 1, 24, False
        )
        without_check = overlay.locate_capture_in_match(
            match, [before_start]
        )
        with_check = overlay.locate_capture_in_match(
            match, [before_start], allow_visual_tolerance=True
        )
        self.assertIsNone(without_check[0])
        self.assertEqual(with_check[0], match.start)
        self.assertTrue(with_check[2])
        self.assertIn("Pending visual verification", with_check[3])

    def test_one_frame_after_occurrence_is_tentatively_clamped_to_end(self):
        match = make_match()
        media_start = overlay.timecode_to_frames("03:42:08:03", 24, False)
        after_end = overlay.frames_to_timecode(
            media_start + (match.end - match.start), 24, False
        )
        record, _, exact, reason = overlay.locate_capture_in_match(
            match, [after_end], allow_visual_tolerance=True
        )
        self.assertEqual(record, match.end - 1)
        self.assertTrue(exact)
        self.assertIn("Pending visual verification", reason)

    def test_visual_verification_accepts_exact_and_wraps_midnight(self):
        exact, reason = overlay._nearby_visual_timecode_offset(
            "03:42:23:18", ["03:42:23:18"], 24, False,
        )
        wrapped, wrapped_reason = overlay._nearby_visual_timecode_offset(
            "00:00:00:00", ["23:59:59:23"], 24, False,
        )
        self.assertEqual((exact, reason), (0, ""))
        self.assertEqual((wrapped, wrapped_reason), (1, ""))

    def test_visual_verification_rejects_ambiguous_nearby_timecodes(self):
        offset, reason = overlay._nearby_visual_timecode_offset(
            "23:15:02:12", ["23:15:02:11", "23:15:02:12"],
            24, False,
        )
        self.assertIsNone(offset)
        self.assertIn("More than one", reason)

    def test_visual_verification_uses_reference_tc_region(self):
        reference_box = (0.0, 0.01, 0.12, 0.03)
        observations = [
            {"text": "23:15:02:12", "bbox": [0.65, 0.01, 0.12, 0.03]},
            {"text": "23:15:02:11", "bbox": [0.0, 0.01, 0.12, 0.03]},
        ]
        offset, reason = overlay._nearby_visual_timecode_offset(
            "23:15:02:12", observations, 24, False,
            reference_boxes=[reference_box],
        )
        self.assertEqual((offset, reason), (1, ""))

    def test_visual_export_reports_a_track_restore_failure_as_critical(self):
        timeline = FakeVerificationTimeline(fail_restore_track=2)
        project = FakeVerificationProject()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(overlay.ReviewStateRestoreError) as raised:
                overlay._export_isolated_review_frame(
                    project, timeline, 1, 0,
                    os.path.join(directory, "frame.png"),
                )
        self.assertIn("V2", str(raised.exception))
        self.assertFalse(timeline.states[2])

    def test_visual_export_isolates_video_and_subtitles_then_restores(self):
        timeline = FakeVerificationTimeline()
        project = FakeVerificationProject(timeline)
        with tempfile.TemporaryDirectory() as directory:
            overlay._export_isolated_review_frame(
                project, timeline, 1, 0,
                os.path.join(directory, "frame.png"),
            )
        self.assertEqual(project.export_states, (
            {1: True, 2: False}, {1: False},
        ))
        self.assertEqual(timeline.states, {1: True, 2: True})
        self.assertEqual(timeline.subtitle_states, {1: True})

    def test_controller_navigation_moves_unsupported_page_to_edit(self):
        resolve = FakePageResolve("fusion")
        self.assertEqual(
            overlay._ensure_timecode_navigation_page(resolve), "edit"
        )
        self.assertEqual(resolve.opened, ["edit"])
        resolve.page = "color"
        self.assertEqual(
            overlay._ensure_timecode_navigation_page(resolve), "color"
        )
        self.assertEqual(resolve.opened, ["edit"])

    def test_supplied_grab_maps_to_exact_timeline_marker_and_timecode(self):
        match = make_match()
        record, source_tc, exact, reason = overlay.locate_capture_in_match(
            match, ["Fixed-23.98 FPS", "03:42:23:18", "B188C010"]
        )
        self.assertEqual(record, 94733)
        self.assertEqual(source_tc, "03:42:23:18")
        self.assertTrue(exact)
        self.assertEqual(reason, "")
        points, warnings = overlay.build_review_points(
            FakeTimeline(), [match],
            {match.still.path: ["03:42:23:18"]},
        )
        self.assertEqual(warnings, [])
        self.assertEqual(points[0].frame_id, 8333)

    def test_unmapped_grab_gets_marker_only_and_no_overlay_placement(self):
        match = make_match()
        points, warnings, placements = overlay.build_review_plan(
            FakeTimeline(), [match], {match.still.path: []}
        )

        self.assertEqual(len(points), 1)
        self.assertFalse(points[0].mapped)
        self.assertFalse(points[0].exact)
        self.assertEqual(placements, [])
        self.assertTrue(any("no safely mappable" in value for value in warnings))

        timeline = FakeTimeline()
        added, marker_warnings = overlay.sync_review_markers(timeline, points)
        self.assertEqual(marker_warnings, [])
        self.assertEqual(added, points)
        restored = overlay.review_points_from_markers(timeline)
        self.assertEqual(len(restored), 1)
        self.assertFalse(restored[0].mapped)

    def test_retime_is_labeled_approximate(self):
        match = make_match(end=94733, speed=200.0)
        record, _, exact, reason = overlay.locate_capture_in_match(
            match, ["03:42:23:18"]
        )
        self.assertEqual(record, match.start + 188)
        self.assertFalse(exact)
        self.assertIn("retimed", reason)

    def test_marker_sync_preserves_every_non_owned_marker(self):
        old_owned = overlay.REVIEW_MARKER_PREFIX + '{"old":true}'
        timeline = FakeTimeline(markers={
            100: {"name": "User", "customData": "user-note"},
            200: {"name": "[AUTO REF] lookalike", "customData": ""},
            300: {"name": "Old owned", "customData": old_owned},
        })
        point = overlay.ReviewPoint(
            8333, "B188C010_260828RU.jpg", "03:42:23:18", True,
            still_path="/references/B188C010_260828RU.jpg",
        )
        added, warnings = overlay.sync_review_markers(timeline, [point])
        self.assertEqual(warnings, [])
        self.assertEqual(added, [point])
        self.assertIn(100, timeline.markers)
        self.assertIn(200, timeline.markers)
        self.assertNotIn(300, timeline.markers)
        self.assertIn(8333, timeline.markers)
        self.assertEqual(timeline.markers[100]["customData"], "user-note")

    def test_user_marker_collision_is_not_moved_or_overwritten(self):
        original = {"name": "User", "customData": "keep-me", "note": "x"}
        timeline = FakeTimeline(markers={8333: dict(original)})
        point = overlay.ReviewPoint(8333, "Reference.jpg", exact=True)
        added, warnings = overlay.sync_review_markers(timeline, [point])
        self.assertEqual(added, [])
        self.assertTrue(warnings)
        self.assertEqual(timeline.markers[8333], original)

    def test_owned_marker_delete_has_verified_frame_fallback(self):
        owned = overlay.REVIEW_MARKER_PREFIX + '{"old":true}'
        timeline = FakeTimeline(
            markers={
                100: {"name": "User", "customData": "keep"},
                300: {"name": "Old owned", "customData": owned},
            },
            fail_custom_delete=True,
        )
        self.assertEqual(overlay.remove_review_markers(timeline), 1)
        self.assertIn(100, timeline.markers)
        self.assertNotIn(300, timeline.markers)

    def test_previous_next_are_strict_and_wrap(self):
        points = [
            overlay.ReviewPoint(4790, "A.jpg"),
            overlay.ReviewPoint(8333, "B.jpg"),
            overlay.ReviewPoint(11106, "C.jpg"),
            overlay.ReviewPoint(14599, "D.jpg"),
        ]
        self.assertEqual(
            overlay.choose_adjacent_review_point(points, 4790, 1).frame_id,
            8333,
        )
        self.assertEqual(
            overlay.choose_adjacent_review_point(points, 4790, -1).frame_id,
            14599,
        )
        self.assertEqual(
            overlay.choose_adjacent_review_point(points, 9000, 1).frame_id,
            11106,
        )
        self.assertEqual(
            overlay.choose_adjacent_review_point(points, 9000, -1).frame_id,
            8333,
        )


if __name__ == "__main__":
    unittest.main()
