import importlib.util
import inspect
import os
import tempfile
import unittest


SCRIPT_PATH = os.path.join(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "Reference Still Auto Overlay.py"
)
SPEC = importlib.util.spec_from_file_location(
    "reference_still_auto_overlay", SCRIPT_PATH
)
overlay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overlay)


class FakeMediaPoolItem(object):
    def __init__(self, path, properties=None, direct_properties=None,
                 source_end_is_exclusive=False):
        self.path = path
        self.properties = (
            properties if properties is not None else {"File Path": path}
        )
        self.direct_properties = direct_properties or {}
        self.source_end_is_exclusive = bool(source_end_is_exclusive)

    def GetClipProperty(self, property_name=None):
        if property_name is None:
            return dict(self.properties)
        if property_name in self.direct_properties:
            return self.direct_properties[property_name]
        return self.properties.get(property_name, "")

    def GetName(self):
        return os.path.basename(self.path)


class FakeTimelineItem(object):
    def __init__(self, name, path, uid, start, end):
        self.name = name
        self.media = FakeMediaPoolItem(path)
        self.uid = uid
        self.start = start
        self.end = end

    def GetMediaPoolItem(self):
        return self.media

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetStart(self, subframe_precision=False):
        return self.start

    def GetEnd(self, subframe_precision=False):
        return self.end


class FakeOverlayItem(FakeTimelineItem):
    def __init__(self, media_pool_item, start, end, track_index):
        super(FakeOverlayItem, self).__init__(
            media_pool_item.GetName(),
            media_pool_item.path,
            "overlay-{}-{}".format(track_index, start),
            start,
            end,
        )
        self.media = media_pool_item
        self.track_index = int(track_index)
        self.clip_color = ""
        self.properties = {}

    def GetTrackTypeAndIndex(self):
        return ("video", self.track_index)

    def SetName(self, name):
        self.name = str(name)
        return True

    def SetClipColor(self, color):
        self.clip_color = str(color)
        return True

    def SetProperty(self, name, value):
        self.properties[name] = value
        return True

    def GetProperty(self, name):
        return self.properties.get(name)


class FakeTimeline(object):
    def __init__(self, tracks, names=None, locked=None, enabled=None,
                 events=None, delete_failure_prefixes=None,
                 delete_failure_suffixes=None, lock_failures=None,
                 enable_failures=None, rename_failure_targets=None,
                 rename_noop_targets=None, rename_noop_prefixes=None,
                 add_returns_false_after_insert=False,
                 add_ignores_requested_index=False, start_frame=0):
        self.tracks = list(tracks)
        self.names = dict(names or {})
        self.locked = dict(locked or {})
        self.enabled = dict(enabled or {})
        self.enable_calls = []
        self.events = events if events is not None else []
        self.deleted_track_names = []
        self.delete_failure_prefixes = tuple(delete_failure_prefixes or ())
        self.delete_failure_suffixes = tuple(delete_failure_suffixes or ())
        self.lock_failures = set(lock_failures or ())
        self.enable_failures = set(enable_failures or ())
        self.rename_failure_targets = set(rename_failure_targets or ())
        self.rename_noop_targets = set(rename_noop_targets or ())
        self.rename_noop_prefixes = tuple(rename_noop_prefixes or ())
        self.add_returns_false_after_insert = bool(
            add_returns_false_after_insert
        )
        self.add_ignores_requested_index = bool(add_ignores_requested_index)
        self.start_frame = int(start_frame)

    def GetStartFrame(self):
        return self.start_frame

    def GetTrackCount(self, track_type):
        if track_type != "video":
            return 0
        return len(self.tracks)

    def GetItemListInTrack(self, track_type, track_index):
        if track_type != "video":
            return []
        return self.tracks[track_index - 1]

    def GetTrackName(self, track_type, track_index):
        if track_type != "video":
            return ""
        return self.names.get(track_index, "Video {}".format(track_index))

    def GetIsTrackLocked(self, track_type, track_index):
        if track_type != "video":
            return False
        return bool(self.locked.get(track_index, False))

    def GetIsTrackEnabled(self, track_type, track_index):
        if track_type != "video":
            return False
        return bool(self.enabled.get(track_index, True))

    def SetTrackEnable(self, track_type, track_index, enabled):
        if track_type != "video" or not 1 <= int(track_index) <= len(self.tracks):
            return False
        track_index = int(track_index)
        enabled = bool(enabled)
        if (track_index, enabled) in self.enable_failures:
            self.events.append(("enable_track_failed", track_index, enabled))
            return False
        self.enabled[track_index] = enabled
        self.enable_calls.append((track_type, track_index, enabled))
        return True

    def SetTrackLock(self, track_type, track_index, locked):
        if track_type != "video" or not 1 <= int(track_index) <= len(self.tracks):
            return False
        track_index = int(track_index)
        locked = bool(locked)
        if (track_index, locked) in self.lock_failures:
            self.events.append(("lock_track_failed", track_index, locked))
            return False
        self.locked[track_index] = locked
        return True

    @staticmethod
    def _mapping_after_track_insert(mapping, inserted_index):
        shifted = {}
        for track_index, value in mapping.items():
            if track_index < inserted_index:
                shifted[track_index] = value
            else:
                shifted[track_index + 1] = value
        return shifted

    def AddTrack(self, track_type, options=None):
        if track_type != "video":
            return False
        track_index = len(self.tracks) + 1
        if isinstance(options, dict) and not self.add_ignores_requested_index:
            requested = int(options.get("index", track_index))
            if 1 <= requested <= len(self.tracks) + 1:
                track_index = requested
        for items in self.tracks[track_index - 1:]:
            for item in items:
                if hasattr(item, "track_index"):
                    item.track_index += 1
        self.tracks.insert(track_index - 1, [])
        self.names = self._mapping_after_track_insert(self.names, track_index)
        self.locked = self._mapping_after_track_insert(self.locked, track_index)
        self.enabled = self._mapping_after_track_insert(self.enabled, track_index)
        self.names[track_index] = "Video {}".format(track_index)
        self.locked[track_index] = False
        self.enabled[track_index] = True
        self.events.append(("add_track", track_index))
        return not self.add_returns_false_after_insert

    def SetTrackName(self, track_type, track_index, name):
        if track_type != "video" or not 1 <= int(track_index) <= len(self.tracks):
            return False
        track_index = int(track_index)
        old_name = self.GetTrackName("video", track_index)
        if str(name) in self.rename_failure_targets:
            self.events.append(("rename_track_failed", old_name, str(name)))
            return False
        if str(name) in self.rename_noop_targets or any(
            str(name).startswith(prefix) for prefix in self.rename_noop_prefixes
        ):
            self.events.append(("rename_track_noop", old_name, str(name)))
            return True
        self.names[track_index] = str(name)
        self.events.append(("rename_track", old_name, str(name)))
        return True

    @staticmethod
    def _mapping_after_track_delete(mapping, deleted_index):
        shifted = {}
        for track_index, value in mapping.items():
            if track_index < deleted_index:
                shifted[track_index] = value
            elif track_index > deleted_index:
                shifted[track_index - 1] = value
        return shifted

    def DeleteTrack(self, track_type, track_index):
        if track_type != "video" or not 1 <= int(track_index) <= len(self.tracks):
            return False
        track_index = int(track_index)
        deleted_name = self.GetTrackName("video", track_index)
        if any(
            deleted_name.startswith(prefix)
            for prefix in self.delete_failure_prefixes
        ) or any(
            deleted_name.endswith(suffix)
            for suffix in self.delete_failure_suffixes
        ):
            self.events.append(("delete_track_failed", deleted_name))
            return False
        self.events.append(("delete_track", deleted_name))
        self.deleted_track_names.append(deleted_name)
        self.tracks.pop(track_index - 1)
        self.names = self._mapping_after_track_delete(self.names, track_index)
        self.locked = self._mapping_after_track_delete(self.locked, track_index)
        self.enabled = self._mapping_after_track_delete(self.enabled, track_index)
        return True


class FakeMediaPoolFolder(object):
    def __init__(self, name, clips=None, subfolders=None):
        self.name = name
        self.clips = list(clips or [])
        self.subfolders = list(subfolders or [])

    def GetName(self):
        return self.name

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subfolders)


class FakeMediaPool(object):
    def __init__(self, timeline=None, root=None, current=None, events=None,
                 append_failure_at=None, duration_delta_at=None,
                 max_still_duration=None):
        self.timeline = timeline
        self.root = root or FakeMediaPoolFolder("Master")
        self.current = current or self.root
        self.events = events if events is not None else []
        self.append_failure_at = append_failure_at
        self.duration_delta_at = duration_delta_at
        self.max_still_duration = max_still_duration
        self.append_count = 0
        self.append_clip_infos = []
        self.import_calls = []
        self.folder_changes = []

    def GetCurrentFolder(self):
        return self.current

    def GetRootFolder(self):
        return self.root

    def AddSubFolder(self, parent, name):
        folder = FakeMediaPoolFolder(name)
        parent.subfolders.append(folder)
        return folder

    def SetCurrentFolder(self, folder):
        self.current = folder
        self.folder_changes.append(folder)
        return True

    def ImportMedia(self, paths):
        self.import_calls.append(list(paths))
        imported = []
        for path in paths:
            item = FakeMediaPoolItem(path)
            self.current.clips.append(item)
            imported.append(item)
        return imported

    def AppendToTimeline(self, clip_infos):
        self.append_count += 1
        clip_info = dict(clip_infos[0])
        self.append_clip_infos.append(clip_info)
        track_index = int(clip_info["trackIndex"])
        self.events.append((
            "append",
            track_index,
            int(clip_info["recordFrame"]),
            clip_info["mediaPoolItem"].GetName(),
        ))
        if self.append_failure_at == self.append_count:
            return []
        is_sequence = bool(
            clip_info["mediaPoolItem"].source_end_is_exclusive
        )
        duration = int(clip_info["endFrame"]) - int(clip_info["startFrame"])
        if not is_sequence:
            duration += 1
        if self.max_still_duration is not None and not is_sequence:
            duration = min(duration, int(self.max_still_duration))
        end = int(clip_info["recordFrame"]) + duration
        if self.duration_delta_at == self.append_count:
            end += 1
        item = FakeOverlayItem(
            clip_info["mediaPoolItem"],
            int(clip_info["recordFrame"]),
            end,
            track_index,
        )
        self.timeline.tracks[track_index - 1].append(item)
        return [item]


class FakeProject(object):
    def __init__(self, media_pool):
        self.media_pool = media_pool

    def GetMediaPool(self):
        return self.media_pool


def make_record(path):
    return overlay.StillRecord(path, overlay.still_aliases_for_path(path))


def make_index(*records):
    index = {}
    for record in records:
        for alias in record.aliases:
            index.setdefault(alias, []).append(record)
    return index


def make_match(still_path, track_index, start=100, end=124, uid=None):
    source_name = os.path.splitext(os.path.basename(still_path))[0] + ".mov"
    source_path = os.path.join("/camera", source_name)
    source_item = FakeTimelineItem(
        source_name,
        source_path,
        uid or "source-{}-{}".format(track_index, start),
        start,
        end,
    )
    still = make_record(still_path)
    source_names = (source_path,)
    source_keys = tuple(overlay.source_keys_for_name(source_path))
    return overlay.ClipMatch(
        source_item,
        track_index,
        start,
        end,
        source_item.uid,
        source_item.name,
        source_names,
        source_keys,
        still,
    )


def managed_reference_item(path="/stills/OLD.jpg", start=0, end=24):
    return FakeTimelineItem(
        overlay.ITEM_PREFIX + os.path.basename(path),
        path,
        "old-reference",
        start,
        end,
    )


class MatchingTests(unittest.TestCase):
    def test_supported_extensions_are_jpg_and_jpeg(self):
        self.assertEqual(overlay.STILL_EXTENSIONS, {".jpg", ".jpeg"})

    def test_jpg_and_jpeg_match_source_stem_case_insensitively(self):
        for still_path in (
            "/stills/a001_c001_0101ab.JPG",
            "/stills/a001_c001_0101ab.JpEg",
        ):
            record = make_record(still_path)
            match, error = overlay.match_still(
                overlay.source_keys_for_name(
                    "/camera/A001_C001_0101AB.MOV"
                ),
                make_index(record),
            )

            self.assertIs(match, record, still_path)
            self.assertEqual(error, "", still_path)

    def test_source_extension_can_be_part_of_still_stem(self):
        record = make_record("/stills/A001_C001.mov.jpg")

        match, error = overlay.match_still(
            overlay.source_keys_for_name("/camera/a001_c001.MOV"),
            make_index(record),
        )

        self.assertIs(match, record)
        self.assertEqual(error, "")
        self.assertEqual(record.aliases, ("a001_c001.mov",))

    def test_red_chunk_filename_matches_logical_still_name(self):
        record = make_record("/stills/A001_C001_0101AB.jpeg")

        match, error = overlay.match_still(
            overlay.source_keys_for_name(
                "/camera/A001_C001_0101AB.RDC/"
                "A001_C001_0101AB_001.R3D"
            ),
            make_index(record),
        )

        self.assertIs(match, record)
        self.assertEqual(error, "")

    def test_exact_red_chunk_still_has_priority_over_logical_fallback(self):
        logical = make_record("/stills/A001_C001_0101AB.jpg")
        physical = make_record("/stills/A001_C001_0101AB_001.jpg")

        match, error = overlay.match_still(
            overlay.source_keys_for_name(
                "/camera/A001_C001_0101AB_001.R3D"
            ),
            make_index(logical, physical),
        )

        self.assertIs(match, physical)
        self.assertEqual(error, "")

    def test_duplicate_still_alias_is_ambiguous(self):
        first = make_record("/day-one/A001.jpg")
        second = make_record("/day-two/a001.JPEG")

        match, error = overlay.match_still(
            overlay.source_keys_for_name("/camera/A001.mov"),
            make_index(first, second),
        )

        self.assertIsNone(match)
        self.assertIn("Ambiguous", error)
        self.assertIn(first.path, error)
        self.assertIn(second.path, error)

    def test_case_distinct_directories_are_not_deduplicated_when_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            upper_directory = os.path.join(folder, "CameraA")
            lower_directory = os.path.join(folder, "cameraa")
            os.makedirs(upper_directory)
            try:
                os.makedirs(lower_directory)
            except FileExistsError:
                self.skipTest("filesystem is case-insensitive")
            if os.path.samefile(upper_directory, lower_directory):
                self.skipTest("filesystem is case-insensitive")
            upper_path = os.path.join(upper_directory, "A001.jpg")
            lower_path = os.path.join(lower_directory, "A001.jpg")
            for path in (upper_path, lower_path):
                with open(path, "wb") as handle:
                    handle.write(b"jpeg")

            records, index = overlay.build_still_library(folder)
            match, error = overlay.match_still(
                overlay.source_keys_for_name("/camera/A001.mov"), index
            )

        self.assertEqual(len(records), 2)
        self.assertIsNone(match)
        self.assertIn("Ambiguous", error)
        self.assertIn(upper_path, error)
        self.assertIn(lower_path, error)


class DiscoveryTests(unittest.TestCase):
    def test_recursive_discovery_accepts_jpg_and_jpeg_only(self):
        with tempfile.TemporaryDirectory() as folder:
            nested = os.path.join(folder, "day-02")
            hidden = os.path.join(folder, ".hidden")
            os.makedirs(nested)
            os.makedirs(hidden)
            paths = (
                os.path.join(folder, "A001.JPG"),
                os.path.join(nested, "B002.jPeG"),
                os.path.join(folder, "ignore.png"),
                os.path.join(folder, ".ignored.jpg"),
                os.path.join(hidden, "C003.jpg"),
            )
            for path in paths:
                with open(path, "wb") as handle:
                    handle.write(b"test")

            recursive = overlay.find_still_paths(folder)
            flat = overlay.find_still_paths(folder, recursive=False)

        self.assertEqual(
            {os.path.basename(path) for path in recursive},
            {"A001.JPG", "B002.jPeG"},
        )
        self.assertEqual(
            {os.path.basename(path) for path in flat},
            {"A001.JPG"},
        )


class AuditAndClipInfoTests(unittest.TestCase):
    def _audit_one(self, start=100, end=124):
        source = FakeTimelineItem(
            "A001 editorial name", "/camera/A001.mov", "source-one", start, end
        )
        timeline = FakeTimeline([[source]], names={1: "V1"})
        record = make_record("/stills/A001.jpg")
        audit = overlay.audit_timeline(timeline, make_index(record))
        self.assertEqual(len(audit["matches"]), 1)
        self.assertIsInstance(audit["matches"][0], overlay.ClipMatch)
        return audit["matches"][0]

    def test_audit_excludes_managed_reference_tracks(self):
        source = FakeTimelineItem(
            "A001", "/camera/A001.mov", "source", 100, 124
        )
        managed = FakeTimelineItem(
            overlay.ITEM_PREFIX + "A001.jpg",
            "/stills/A001.jpg",
            "managed-reference",
            100,
            124,
        )
        timeline = FakeTimeline(
            [[source], [managed]],
            names={
                1: "V1",
                2: overlay.TRACK_PREFIX + "1",
            },
            locked={2: True},
        )
        record = make_record("/stills/A001.jpg")

        audit = overlay.audit_timeline(
            timeline,
            make_index(record),
            managed_track_prefix=overlay.TRACK_PREFIX,
        )

        self.assertEqual(audit["total_video_items"], 1)
        self.assertEqual(len(audit["matches"]), 1)
        self.assertIs(audit["matches"][0].item, source)

    def test_audit_skips_disabled_source_tracks(self):
        enabled_source = FakeTimelineItem(
            "A001", "/camera/A001.mov", "enabled-source", 100, 124
        )
        disabled_source = FakeTimelineItem(
            "B002", "/camera/B002.mov", "disabled-source", 200, 224
        )
        timeline = FakeTimeline(
            [[enabled_source], [disabled_source]],
            names={1: "V1", 2: "V2"},
            enabled={1: True, 2: False},
        )
        first = make_record("/stills/A001.jpg")
        second = make_record("/stills/B002.jpg")

        audit = overlay.audit_timeline(
            timeline, make_index(first, second)
        )

        self.assertEqual(audit["total_video_items"], 1)
        self.assertEqual(len(audit["matches"]), 1)
        self.assertIs(audit["matches"][0].item, enabled_source)
        self.assertEqual(len(audit["skipped_disabled_tracks"]), 1)
        self.assertIn("V2", audit["skipped_disabled_tracks"][0])

    def test_overlay_clip_info_uses_record_start_and_inclusive_end(self):
        match = self._audit_one(start=100, end=124)
        media_pool_item = FakeMediaPoolItem("/stills/A001.jpg")

        clip_info = overlay.build_overlay_clip_info(match, media_pool_item)

        self.assertIs(clip_info["mediaPoolItem"], media_pool_item)
        self.assertEqual(clip_info["recordFrame"], 100)
        self.assertEqual(clip_info["startFrame"], 0)
        self.assertEqual(clip_info["endFrame"], 23)

    def test_one_frame_overlay_has_zero_inclusive_end_frame(self):
        match = self._audit_one(start=42, end=43)

        clip_info = overlay.build_overlay_clip_info(
            match, FakeMediaPoolItem("/stills/A001.jpeg")
        )

        self.assertEqual(clip_info["recordFrame"], 42)
        self.assertEqual(clip_info["endFrame"], 0)

    def test_sequence_clip_info_uses_exclusive_source_end(self):
        match = self._audit_one(start=100, end=124)

        clip_info = overlay.build_overlay_clip_info(
            match,
            FakeMediaPoolItem(
                "/stills/A001_[000000-000023].jpg",
                source_end_is_exclusive=True,
            ),
            source_end_is_exclusive=True,
        )

        self.assertEqual(clip_info["recordFrame"], 100)
        self.assertEqual(clip_info["startFrame"], 0)
        self.assertEqual(clip_info["endFrame"], 24)

    def test_full_timeline_signature_change_stops_before_build_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            still_path = os.path.join(folder, "A001.jpg")
            with open(still_path, "wb") as handle:
                handle.write(b"jpeg")
            matched = FakeTimelineItem(
                "A001", "/camera/A001.mov", "matched", 100, 124
            )
            unrelated = FakeTimelineItem(
                "UNMATCHED", "/camera/UNMATCHED.mov", "unmatched", 200, 224
            )
            events = []
            timeline = FakeTimeline(
                [[matched, unrelated]], names={1: "V1"}, events=events
            )
            media_pool = FakeMediaPool(timeline=timeline, events=events)
            project = FakeProject(media_pool)
            original_audit = overlay.audit_timeline
            original_report_path = overlay.create_report_path
            original_write_report = overlay.write_report
            original_build = overlay.build_overlays
            audit_calls = [0]
            build_calls = [0]

            def audit_then_change_unmatched_item(*args, **kwargs):
                result = original_audit(*args, **kwargs)
                audit_calls[0] += 1
                if audit_calls[0] == 1:
                    unrelated.start += 5
                return result

            def forbidden_build(*args, **kwargs):
                build_calls[0] += 1
                raise AssertionError("build mutation should not begin")

            overlay.audit_timeline = audit_then_change_unmatched_item
            overlay.create_report_path = lambda *args: os.path.join(
                folder, "audit.log"
            )
            overlay.write_report = lambda *args: None
            overlay.build_overlays = forbidden_build
            try:
                with self.assertRaises(overlay.StillOverlayError) as raised:
                    overlay.build_from_folder(
                        object(), project, timeline, folder, assume_yes=True
                    )
            finally:
                overlay.audit_timeline = original_audit
                overlay.create_report_path = original_report_path
                overlay.write_report = original_write_report
                overlay.build_overlays = original_build

        self.assertIn("timeline changed", str(raised.exception).lower())
        self.assertEqual(audit_calls[0], 2)
        self.assertEqual(build_calls[0], 0)
        self.assertEqual(events, [])
        self.assertEqual(media_pool.import_calls, [])


class ManagedTrackValidationTests(unittest.TestCase):
    def test_locked_empty_managed_track_is_rejected(self):
        timeline = FakeTimeline(
            [[]],
            names={1: overlay.TRACK_PREFIX + "1"},
            locked={1: True},
        )

        valid, reason = overlay.validate_managed_track(timeline, 1)

        self.assertFalse(valid)
        self.assertIn("empty", reason)

    def test_locked_managed_track_with_managed_jpeg_items_is_valid(self):
        item = FakeTimelineItem(
            overlay.ITEM_PREFIX + "A001.jpg",
            "/stills/A001.JPG",
            "reference",
            0,
            24,
        )
        timeline = FakeTimeline(
            [[item]],
            names={1: overlay.TRACK_PREFIX + "1"},
            locked={1: True},
        )

        valid, reason = overlay.validate_managed_track(timeline, 1)

        self.assertTrue(valid)
        self.assertEqual(reason, "")

    def test_current_spec_accepts_sparse_comparison_settings_but_not_legacy_lane(self):
        legacy = FakeTimelineItem(
            overlay.ITEM_PREFIX + "OLD.jpg",
            "/stills/OLD.jpg",
            "legacy",
            0,
            120,
        )
        legacy_timeline = FakeTimeline([[legacy]])
        self.assertFalse(overlay.managed_tracks_use_current_spec(
            legacy_timeline, [1]
        ))

        current = FakeOverlayItem(
            FakeMediaPoolItem("/stills/A001.jpg"), 42, 43, 1
        )
        current.SetName(overlay.ITEM_PREFIX + "A001.jpg")
        current.SetProperty("Opacity", overlay.REFERENCE_OPACITY)
        current.SetProperty("CompositeMode", 0)
        current_timeline = FakeTimeline([[current]])
        self.assertTrue(overlay.managed_tracks_use_current_spec(
            current_timeline, [1]
        ))
        current.SetProperty("Opacity", 37.5)
        current.SetProperty("CompositeMode", 3)
        self.assertTrue(overlay.managed_tracks_use_current_spec(
            current_timeline, [1]
        ))
        current.SetProperty("Opacity", 101)
        self.assertFalse(overlay.managed_tracks_use_current_spec(
            current_timeline, [1]
        ))

    def test_accepts_owned_lossless_png_reference_helpers(self):
        reference = FakeTimelineItem(
            overlay.ITEM_PREFIX + "A001.jpg",
            "/reference-cache/A001.[00000000-00000001].png",
            "prepared-helper",
            42,
            43,
        )
        timeline = FakeTimeline(
            [[reference]],
            names={1: overlay.TRACK_PREFIX + "1"},
            locked={1: True},
        )
        self.assertEqual(overlay.validate_managed_track(timeline, 1), (True, ""))

    def test_rejects_invalid_or_contaminated_managed_tracks(self):
        cases = (
            (
                FakeTimeline([[]], names={1: "V1"}, locked={1: True}),
                1,
            ),
            (
                FakeTimeline(
                    [[]],
                    names={1: overlay.TRACK_PREFIX + "1"},
                    locked={1: False},
                ),
                1,
            ),
            (
                FakeTimeline(
                    [[FakeTimelineItem(
                        "Foreign clip", "/stills/A001.jpg", "foreign", 0, 24
                    )]],
                    names={1: overlay.TRACK_PREFIX + "1"},
                    locked={1: True},
                ),
                1,
            ),
            (
                FakeTimeline(
                    [[FakeTimelineItem(
                        overlay.ITEM_PREFIX + "A001.mov",
                        "/stills/A001.mov",
                        "wrong-extension",
                        0,
                        24,
                    )]],
                    names={1: overlay.TRACK_PREFIX + "1"},
                    locked={1: True},
                ),
                1,
            ),
            (
                FakeTimeline([[]], names={1: overlay.TRACK_PREFIX + "1"}, locked={1: True}),
                2,
            ),
        )

        for timeline, track_index in cases:
            valid, reason = overlay.validate_managed_track(
                timeline, track_index
            )
            self.assertFalse(valid, (track_index, reason))
            self.assertTrue(reason, track_index)

    def test_remove_managed_tracks_also_removes_valid_backup_tracks(self):
        managed_item = managed_reference_item("/stills/CURRENT.jpg")
        backup_item = managed_reference_item("/stills/PREVIOUS.jpg")
        managed_name = overlay.TRACK_PREFIX + "1"
        backup_name = overlay._backup_track_name("orphaned", 1)
        timeline = FakeTimeline(
            [[], [managed_item], [backup_item]],
            names={1: "V1", 2: managed_name, 3: backup_name},
            locked={2: True, 3: True},
            enabled={2: False, 3: False},
        )

        removed = overlay.remove_managed_tracks(timeline)

        self.assertEqual(removed, 2)
        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertEqual(timeline.GetTrackName("video", 1), "V1")
        self.assertCountEqual(
            timeline.deleted_track_names, [managed_name, backup_name]
        )

    def test_failed_remove_relocks_the_surviving_managed_track(self):
        item = managed_reference_item("/stills/CURRENT.jpg")
        name = overlay.TRACK_PREFIX + "1"
        timeline = FakeTimeline(
            [[item]],
            names={1: name},
            locked={1: True},
            enabled={1: False},
            delete_failure_prefixes=(overlay.TRACK_PREFIX,),
        )

        with self.assertRaises(overlay.StillOverlayError):
            overlay.remove_managed_tracks(timeline)

        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertTrue(timeline.GetIsTrackLocked("video", 1))


class VisibilityTests(unittest.TestCase):
    def test_set_reference_visibility_updates_every_requested_track(self):
        timeline = FakeTimeline(
            [[], [], []], enabled={1: True, 2: True, 3: True}
        )

        overlay.set_reference_visibility(timeline, [2, 3], False)

        self.assertTrue(timeline.enabled[1])
        self.assertFalse(timeline.enabled[2])
        self.assertFalse(timeline.enabled[3])
        self.assertEqual(
            timeline.enable_calls,
            [("video", 2, False), ("video", 3, False)],
        )

    def test_toggle_enables_all_when_every_reference_track_is_off(self):
        timeline = FakeTimeline(
            [[], [], []], enabled={1: True, 2: False, 3: False}
        )

        enabled = overlay.toggle_reference_tracks(timeline, [2, 3])

        self.assertTrue(enabled)
        self.assertTrue(timeline.enabled[2])
        self.assertTrue(timeline.enabled[3])

    def test_toggle_disables_all_when_states_are_mixed(self):
        timeline = FakeTimeline(
            [[], [], []], enabled={1: True, 2: True, 3: False}
        )

        enabled = overlay.toggle_reference_tracks(timeline, [2, 3])

        self.assertFalse(enabled)
        self.assertFalse(timeline.enabled[2])
        self.assertFalse(timeline.enabled[3])

    def test_toggle_disables_all_when_every_reference_track_is_on(self):
        timeline = FakeTimeline(
            [[], [], []], enabled={1: True, 2: True, 3: True}
        )

        enabled = overlay.toggle_reference_tracks(timeline, [2, 3])

        self.assertFalse(enabled)
        self.assertFalse(timeline.enabled[2])
        self.assertFalse(timeline.enabled[3])

    def test_partial_visibility_failure_restores_every_changed_track(self):
        timeline = FakeTimeline(
            [[], []],
            enabled={1: True, 2: True},
            enable_failures={(2, False)},
        )

        with self.assertRaises(overlay.StillOverlayError):
            overlay.set_reference_visibility(timeline, [1, 2], False)

        self.assertTrue(timeline.GetIsTrackEnabled("video", 1))
        self.assertTrue(timeline.GetIsTrackEnabled("video", 2))

    def test_visibility_rollback_failure_is_reported(self):
        timeline = FakeTimeline(
            [[], []],
            enabled={1: True, 2: True},
            enable_failures={(2, False), (1, True)},
        )

        with self.assertRaises(overlay.StillOverlayError) as raised:
            overlay.set_reference_visibility(timeline, [1, 2], False)

        self.assertIn("rollback", str(raised.exception).lower())


class DifferenceModeTests(unittest.TestCase):
    def test_relock_failure_is_reported(self):
        media_item = FakeMediaPoolItem("/stills/A001.jpg")
        reference = FakeOverlayItem(media_item, 100, 124, 1)
        reference.properties["CompositeMode"] = 0
        timeline = FakeTimeline(
            [[reference]],
            names={1: overlay.TRACK_PREFIX + "1"},
            locked={1: True},
            enabled={1: False},
            lock_failures={(1, True)},
        )
        resolve_app = type(
            "FakeResolveConstants",
            (object,),
            {"COMPOSITE_NORMAL": 0, "COMPOSITE_DIFF": 3},
        )()

        with self.assertRaises(overlay.StillOverlayError) as raised:
            overlay.set_reference_difference(
                resolve_app, timeline, [1], True
            )

        self.assertIn("lock", str(raised.exception).lower())

    def test_show_reference_visibility_failure_restores_prior_difference_mode(self):
        media_item = FakeMediaPoolItem("/stills/A001.jpg")
        reference = FakeOverlayItem(media_item, 100, 124, 1)
        reference.properties["CompositeMode"] = 3
        timeline = FakeTimeline(
            [[reference]],
            names={1: overlay.TRACK_PREFIX + "1"},
            locked={1: True},
            enabled={1: False},
            enable_failures={(1, True)},
        )
        resolve_app = type(
            "FakeResolveConstants",
            (object,),
            {"COMPOSITE_NORMAL": 0, "COMPOSITE_DIFF": 3},
        )()

        with self.assertRaises(overlay.StillOverlayError):
            overlay.set_reference_difference(
                resolve_app, timeline, [1], False,
                show_reference=True
            )

        self.assertEqual(reference.GetProperty("CompositeMode"), 3)
        self.assertFalse(timeline.GetIsTrackEnabled("video", 1))
        self.assertTrue(timeline.GetIsTrackLocked("video", 1))

    def test_unreadable_original_composite_mode_aborts_without_guessing(self):
        class UnreadableOverlay(FakeOverlayItem):
            def GetProperty(self, name):
                return None

        media_item = FakeMediaPoolItem("/stills/A001.jpg")
        reference = UnreadableOverlay(media_item, 100, 124, 1)
        timeline = FakeTimeline(
            [[reference]],
            names={1: overlay.TRACK_PREFIX + "1"},
            locked={1: True},
            enabled={1: False},
        )
        resolve_app = type(
            "FakeResolveConstants",
            (object,),
            {"COMPOSITE_NORMAL": 0, "COMPOSITE_DIFF": 3},
        )()

        with self.assertRaises(overlay.StillOverlayError) as raised:
            overlay.set_reference_difference(
                resolve_app, timeline, [1], True
            )

        self.assertIn("could not read", str(raised.exception).lower())
        self.assertTrue(timeline.GetIsTrackLocked("video", 1))
        self.assertFalse(timeline.GetIsTrackEnabled("video", 1))


class ImportReferenceMediaTests(unittest.TestCase):
    def test_import_reuses_only_exact_paths_and_restores_current_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            first_folder = os.path.join(folder, "first")
            second_folder = os.path.join(folder, "second")
            os.makedirs(first_folder)
            os.makedirs(second_folder)
            first_path = os.path.join(first_folder, "A001.jpg")
            second_path = os.path.join(second_folder, "A001.jpg")
            for path in (first_path, second_path):
                with open(path, "wb") as handle:
                    handle.write(b"jpeg")

            existing = FakeMediaPoolItem(first_path)
            target = FakeMediaPoolFolder(
                overlay.MEDIA_POOL_FOLDER, clips=[existing]
            )
            root = FakeMediaPoolFolder("Master", subfolders=[target])
            user_folder = FakeMediaPoolFolder("User Selected Bin")
            media_pool = FakeMediaPool(root=root, current=user_folder)

            media_by_path, imported_count = overlay.import_reference_media(
                media_pool, [first_path, second_path, first_path]
            )

        first_key = overlay._canonical_path(first_path)
        second_key = overlay._canonical_path(second_path)
        self.assertIs(media_by_path[first_key], existing)
        self.assertIsNot(media_by_path[second_key], existing)
        self.assertEqual(imported_count, 1)
        self.assertEqual(media_pool.import_calls, [[second_path]])
        self.assertIs(media_pool.current, user_folder)
        self.assertEqual(media_pool.folder_changes, [target, user_folder])


class ReferenceSequenceTests(unittest.TestCase):
    def test_prepare_uses_two_frame_video_helper_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            still_path = os.path.join(folder, "B188C010.jpg")
            source_bytes = b"byte-identical-reference-jpeg"
            with open(still_path, "wb") as handle:
                handle.write(source_bytes)

            one_frame = make_match(still_path, 1, 100, 101)
            long_match = make_match(still_path, 2, 200, 350)
            cache_directory = os.path.join(folder, "sequence-cache")
            original_cache_directory = overlay.SEQUENCE_CACHE_DIRECTORY
            overlay.SEQUENCE_CACHE_DIRECTORY = cache_directory
            try:
                records = overlay.prepare_reference_sequences([
                    one_frame, long_match
                ])
                key = overlay._canonical_path(still_path)
                record = records[key]

                self.assertEqual(record.required_frames, 1)
                self.assertEqual(record.frame_count, 2)
                self.assertIn("%08d", record.frame_pattern)
                frame_paths = [
                    record.frame_pattern % frame_index
                    for frame_index in range(2)
                ]
                self.assertEqual(
                    sorted(os.listdir(os.path.dirname(record.frame_pattern))),
                    sorted(os.path.basename(path) for path in frame_paths),
                )
                for frame_path in frame_paths:
                    with open(frame_path, "rb") as handle:
                        self.assertEqual(handle.read(), source_bytes)

                original_stats = [
                    (os.stat(path).st_ino, os.stat(path).st_mtime_ns)
                    for path in frame_paths
                ]
                reused = overlay.prepare_reference_sequences([
                    long_match, one_frame
                ])[key]
                self.assertEqual(reused.frame_pattern, record.frame_pattern)
                self.assertEqual([
                    (os.stat(path).st_ino, os.stat(path).st_mtime_ns)
                    for path in frame_paths
                ], original_stats)

                one_frame_record = overlay.prepare_reference_sequences([
                    one_frame
                ])[key]
                self.assertEqual(one_frame_record.frame_pattern, record.frame_pattern)
                for frame_index in range(2):
                    with open(
                        one_frame_record.frame_pattern % frame_index, "rb"
                    ) as handle:
                        self.assertEqual(handle.read(), source_bytes)
            finally:
                overlay.SEQUENCE_CACHE_DIRECTORY = original_cache_directory

    def test_sequence_import_validates_video_frames_restores_and_reuses(self):
        class SequenceMediaPool(FakeMediaPool):
            def __init__(self, *args, **kwargs):
                super(SequenceMediaPool, self).__init__(*args, **kwargs)
                self.import_folders = []

            def ImportMedia(self, specifications):
                self.import_calls.append(list(specifications))
                self.import_folders.append(self.current.GetName())
                imported = []
                for specification in specifications:
                    start = int(specification["StartIndex"])
                    end = int(specification["EndIndex"])
                    index_range = "[{:08d}-{:08d}]".format(start, end)
                    media_path = specification["FilePath"].replace(
                        "%08d", index_range
                    )
                    item = FakeMediaPoolItem(
                        media_path,
                        properties={
                            "File Path": media_path,
                            "Type": "Video",
                            "Frames": str(end - start + 1),
                        },
                        source_end_is_exclusive=True,
                    )
                    self.current.clips.append(item)
                    imported.append(item)
                return imported

        original_path = "/references/B188C010.jpg"
        frame_pattern = "/cache/autoref_B188C010.%08d.jpg"
        media_path = "/cache/autoref_B188C010.[00000000-00000004].jpg"
        record = overlay.SequenceRecord(
            original_path,
            5,
            5,
            frame_pattern,
            media_path,
            os.path.basename(media_path),
        )
        invalid_type = FakeMediaPoolItem(
            media_path,
            properties={
                "File Path": media_path,
                "Type": "Still",
                "Frames": "5",
            },
        )
        invalid_frames = FakeMediaPoolItem(
            media_path,
            properties={
                "File Path": media_path,
                "Type": "Video",
                "Frames": "4",
            },
        )
        sequence_folder = FakeMediaPoolFolder(
            overlay.SEQUENCE_MEDIA_POOL_FOLDER,
            clips=[invalid_type, invalid_frames],
        )
        parent_folder = FakeMediaPoolFolder(
            overlay.MEDIA_POOL_FOLDER,
            subfolders=[sequence_folder],
        )
        root = FakeMediaPoolFolder("Master", subfolders=[parent_folder])
        user_folder = FakeMediaPoolFolder("User Selected Bin")
        media_pool = SequenceMediaPool(root=root, current=user_folder)
        records = {overlay._canonical_path(original_path): record}

        first_result, first_count = overlay.import_reference_sequence_media(
            media_pool, records
        )
        second_result, second_count = overlay.import_reference_sequence_media(
            media_pool, records
        )

        original_key = overlay._canonical_path(original_path)
        imported_item = first_result[original_key]
        self.assertIs(second_result[original_key], imported_item)
        self.assertEqual((first_count, second_count), (1, 0))
        self.assertTrue(overlay._sequence_item_is_expected(
            imported_item, record
        ))
        self.assertEqual(imported_item.GetClipProperty("Type"), "Video")
        self.assertEqual(imported_item.GetClipProperty("Frames"), "5")
        self.assertEqual(media_pool.import_folders, [
            overlay.SEQUENCE_MEDIA_POOL_FOLDER
        ])
        self.assertEqual(media_pool.import_calls, [[{
            "FilePath": frame_pattern,
            "StartIndex": 0,
            "EndIndex": 4,
        }]])
        self.assertIs(media_pool.current, user_folder)
        self.assertEqual(media_pool.folder_changes, [
            sequence_folder,
            user_folder,
            sequence_folder,
            user_folder,
        ])

    def test_sequence_name_cannot_override_contradictory_physical_path(self):
        media_name = "autoref_A001.[00000000-00000119].jpg"
        record = overlay.SequenceRecord(
            "/references/A001.jpg",
            120,
            120,
            "/expected/autoref_A001.%08d.jpg",
            os.path.join("/expected", media_name),
            media_name,
        )
        wrong_item = FakeMediaPoolItem(
            os.path.join("/different", media_name),
            properties={
                "File Path": os.path.join("/different", media_name),
                "Type": "Video",
                "Frames": "120",
            },
        )

        self.assertFalse(overlay._sequence_item_is_expected(
            wrong_item, record
        ))

    def test_media_identity_uses_path_sets_and_pathless_legacy_fallback(self):
        shared_path = "/sequence/A001.[00000000-00000119].jpg"
        actual = FakeMediaPoolItem(
            shared_path, properties={"File Path": shared_path}
        )
        same_path_wrapper = FakeMediaPoolItem(
            "ignored-name.jpg", properties={"Source File": shared_path}
        )
        contradictory_wrapper = FakeMediaPoolItem(
            "/different/A001.[00000000-00000119].jpg"
        )
        pathless_legacy_wrapper = FakeMediaPoolItem("", properties={})

        self.assertTrue(overlay._media_item_matches_reference(
            actual, same_path_wrapper, "/references/A001.jpg"
        ))
        self.assertFalse(overlay._media_item_matches_reference(
            actual, contradictory_wrapper, shared_path
        ))
        self.assertTrue(overlay._media_item_matches_reference(
            actual, pathless_legacy_wrapper, shared_path
        ))


class BuildOverlayTransactionTests(unittest.TestCase):
    @staticmethod
    def _legacy_reference_media_provider(media_pool, matches):
        paths = sorted({match.still.path for match in matches})
        media_by_path, imported_count = overlay.import_reference_media(
            media_pool, paths
        )
        return media_by_path, imported_count, False

    def _build(self, project, timeline, audit,
               reference_media_provider=None, review_placements=None):
        if review_placements is None:
            review_placements = [
                (
                    match,
                    overlay.ReviewPoint(
                        int(match.start) - int(timeline.GetStartFrame()),
                        os.path.basename(match.still.path),
                        exact=True,
                        still_path=match.still.path,
                        match_uid=match.uid,
                        mapped=True,
                    ),
                )
                for match in audit["matches"]
            ]
        return overlay.build_overlays(
            object(),
            project,
            timeline,
            audit,
            review_placements=review_placements,
            reference_media_provider=(
                reference_media_provider
                or self._legacy_reference_media_provider
            ),
        )

    def _new_build_context(self, source_track_count, events=None,
                           append_failure_at=None, duration_delta_at=None,
                           max_still_duration=None):
        events = events if events is not None else []
        timeline = FakeTimeline(
            [[] for _ in range(source_track_count)],
            names={
                index: "V{}".format(index)
                for index in range(1, source_track_count + 1)
            },
            events=events,
        )
        media_pool = FakeMediaPool(
            timeline=timeline,
            events=events,
            append_failure_at=append_failure_at,
            duration_delta_at=duration_delta_at,
            max_still_duration=max_still_duration,
        )
        return timeline, media_pool, FakeProject(media_pool)

    def _rebuild_context(self, events=None, append_failure_at=None,
                         duration_delta_at=None,
                         delete_failure_prefixes=None,
                         max_still_duration=None):
        events = events if events is not None else []
        old_item = managed_reference_item()
        old_name = overlay.TRACK_PREFIX + "1"
        timeline = FakeTimeline(
            [[], [], [old_item]],
            names={1: "V1", 2: "V2", 3: old_name},
            locked={3: True},
            enabled={3: False},
            events=events,
            delete_failure_prefixes=delete_failure_prefixes,
        )
        media_pool = FakeMediaPool(
            timeline=timeline,
            events=events,
            append_failure_at=append_failure_at,
            duration_delta_at=duration_delta_at,
            max_still_duration=max_still_duration,
        )
        return timeline, media_pool, FakeProject(media_pool), old_item, old_name

    def test_success_builds_one_companion_track_per_source_and_keeps_refs_off(self):
        events = []
        timeline, media_pool, project = self._new_build_context(3, events)
        high_match = make_match("/references/C003.jpg", 3, 300, 324)
        low_match = make_match("/references/A001.jpeg", 1, 100, 124)
        audit = {"matches": [high_match, low_match]}

        result = self._build(project, timeline, audit)

        self.assertEqual(timeline.GetTrackCount("video"), 5)
        self.assertEqual(result.track_indices, (2, 5))
        self.assertEqual(result.track_count, 2)
        self.assertEqual(result.overlay_count, 2)
        self.assertFalse(result.references_enabled)
        self.assertEqual(timeline.GetTrackName("video", 2), overlay.TRACK_PREFIX + "1")
        self.assertEqual(timeline.GetTrackName("video", 5), overlay.TRACK_PREFIX + "2")
        self.assertTrue(timeline.GetIsTrackLocked("video", 2))
        self.assertTrue(timeline.GetIsTrackLocked("video", 5))
        self.assertFalse(timeline.GetIsTrackEnabled("video", 2))
        self.assertFalse(timeline.GetIsTrackEnabled("video", 5))
        self.assertEqual(len(timeline.GetItemListInTrack("video", 2)), 1)
        self.assertEqual(len(timeline.GetItemListInTrack("video", 5)), 1)
        for track_index in (2, 5):
            item = timeline.GetItemListInTrack("video", track_index)[0]
            self.assertEqual(item.GetEnd() - item.GetStart(), 1)
            self.assertEqual(
                item.GetProperty("Opacity"), overlay.REFERENCE_OPACITY
            )
            self.assertEqual(item.GetProperty("CompositeMode"), 0)
        append_events = [event for event in events if event[0] == "append"]
        self.assertEqual(
            [event[3] for event in append_events],
            ["C003.jpg", "A001.jpeg"],
        )

    def test_sparse_placement_uses_review_point_instead_of_clip_start(self):
        timeline, media_pool, project = self._new_build_context(1)
        match = make_match("/references/A001.jpg", 1, 100, 124)
        point = overlay.ReviewPoint(
            112,
            "A001.jpg",
            source_timecode="01:00:04:16",
            exact=True,
            still_path=match.still.path,
            match_uid=match.uid,
            mapped=True,
        )

        result = self._build(
            project,
            timeline,
            {"matches": [match]},
            review_placements=[(match, point)],
        )

        self.assertEqual(result.segment_count, 1)
        item = timeline.GetItemListInTrack("video", 2)[0]
        self.assertEqual((item.GetStart(), item.GetEnd()), (112, 113))
        self.assertEqual(item.GetProperty("Opacity"), 100.0)
        overlay.set_reference_visibility(timeline, [2], True)
        overlay.set_reference_visibility(timeline, [2], False)
        self.assertEqual(item.GetProperty("Opacity"), 100.0)

    def test_zero_mapped_placements_aborts_before_timeline_mutation(self):
        events = []
        timeline, media_pool, project = self._new_build_context(1, events)
        match = make_match("/references/A001.jpg", 1, 100, 124)

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(
                project,
                timeline,
                {"matches": [match]},
                review_placements=[],
            )

        self.assertIn("readable source timecode", str(raised.exception))
        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertEqual(events, [])
        self.assertEqual(media_pool.import_calls, [])

    def test_long_source_clip_gets_one_sparse_reference_frame(self):
        timeline, media_pool, project = self._new_build_context(
            1, max_still_duration=120
        )
        audit = {
            "matches": [make_match(
                "/references/B188C010_260828RU.jpg",
                1,
                86400,
                87150,
            )]
        }

        result = self._build(project, timeline, audit)

        self.assertEqual(result.overlay_count, 1)
        self.assertEqual(result.segment_count, 1)
        self.assertEqual(result.track_count, 1)
        self.assertEqual(result.track_indices, (2,))
        self.assertTrue(timeline.GetIsTrackLocked("video", 2))
        self.assertFalse(timeline.GetIsTrackEnabled("video", 2))

        items = timeline.GetItemListInTrack("video", 2)
        self.assertEqual(len(items), 1)
        ranges = [(item.GetStart(), item.GetEnd()) for item in items]
        self.assertEqual(ranges, [(86400, 86401)])
        self.assertEqual(items[0].GetProperty("Opacity"), 100.0)

    def test_sequence_media_places_only_the_mapped_frame(self):
        sequence_items = []

        def sequence_provider(unused_media_pool, matches):
            original_path = matches[0].still.path
            sequence_item = FakeMediaPoolItem(
                "/sequence/B188C010_260828RU_[000000-000749].jpg",
                source_end_is_exclusive=True,
            )
            sequence_items.append(sequence_item)
            return {
                overlay._canonical_path(original_path): sequence_item
            }, 1, True

        timeline, media_pool, project = self._new_build_context(
            1, max_still_duration=120
        )
        audit = {
            "matches": [make_match(
                "/references/B188C010_260828RU.jpg",
                1,
                86400,
                87150,
            )]
        }

        result = self._build(
            project, timeline, audit, sequence_provider
        )

        self.assertEqual(result.overlay_count, 1)
        self.assertEqual(result.segment_count, 1)
        self.assertEqual(media_pool.append_count, 1)
        items = timeline.GetItemListInTrack("video", 2)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].GetStart(), items[0].GetEnd()), (
            86400, 86401
        ))
        self.assertIs(items[0].GetMediaPoolItem(), sequence_items[0])

    def test_sequence_helper_places_one_frame_for_each_match(self):
        sequence_item = FakeMediaPoolItem(
            "/sequence/A001_[00000000-00000119].jpg",
            properties={
                "File Path": "/sequence/A001_[00000000-00000119].jpg",
                "Type": "Video",
                "Frames": "120",
            },
            source_end_is_exclusive=True,
        )

        def sequence_provider(unused_media_pool, matches):
            self.assertEqual(
                max(match.end - match.start for match in matches), 150
            )
            media_key = overlay._canonical_path(matches[0].still.path)
            return {media_key: sequence_item}, 1, True, {media_key: 120}

        timeline, media_pool, project = self._new_build_context(
            1, max_still_duration=120
        )
        still_path = "/references/A001.jpg"
        audit = {
            "matches": [
                make_match(still_path, 1, 100, 101),
                make_match(still_path, 1, 200, 230),
                make_match(still_path, 1, 300, 450),
            ]
        }

        result = self._build(
            project, timeline, audit, sequence_provider
        )

        self.assertEqual(result.overlay_count, 3)
        self.assertEqual(result.segment_count, 3)
        self.assertEqual(media_pool.append_count, 3)
        self.assertEqual([
            (info["startFrame"], info["endFrame"])
            for info in media_pool.append_clip_infos
        ], [(0, 1), (0, 1), (0, 1)])
        items = timeline.GetItemListInTrack("video", 2)
        self.assertEqual([
            (item.GetStart(), item.GetEnd()) for item in items
        ], [(100, 101), (200, 201), (300, 301)])
        self.assertTrue(all(
            item.GetMediaPoolItem() is sequence_item for item in items
        ))

    def test_zero_duration_segment_rolls_back_staged_lane(self):
        timeline, media_pool, project = self._new_build_context(
            1, max_still_duration=0
        )
        audit = {
            "matches": [make_match(
                "/references/A001.jpg", 1, 100, 850
            )]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("unusable reference duration", str(raised.exception))
        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertEqual(timeline.GetTrackName("video", 1), "V1")

    def test_third_sparse_append_failure_preserves_prior_managed_lane(self):
        timeline, media_pool, project, old_item, old_name = self._rebuild_context(
            append_failure_at=3, max_still_duration=120
        )
        audit = {
            "matches": [
                make_match("/references/B002.jpg", 2, 200, 224),
                make_match("/references/C003.jpg", 2, 400, 424),
                make_match("/references/D004.jpg", 2, 600, 624),
            ]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("at frame 600", str(raised.exception))
        self.assertEqual(timeline.GetTrackCount("video"), 3)
        self.assertEqual(timeline.GetTrackName("video", 3), old_name)
        self.assertIs(timeline.GetItemListInTrack("video", 3)[0], old_item)
        self.assertTrue(timeline.GetIsTrackLocked("video", 3))
        self.assertFalse(timeline.GetIsTrackEnabled("video", 3))

    def test_final_validation_rejects_dropped_sparse_item(self):
        class DroppingSparseItemMediaPool(FakeMediaPool):
            def AppendToTimeline(self, clip_infos):
                result = super(
                    DroppingSparseItemMediaPool, self
                ).AppendToTimeline(clip_infos)
                if self.append_count == 3:
                    track_index = int(clip_infos[0]["trackIndex"])
                    self.timeline.tracks[track_index - 1].pop(1)
                return result

        timeline = FakeTimeline([[]], names={1: "V1"})
        media_pool = DroppingSparseItemMediaPool(
            timeline=timeline, max_still_duration=120
        )
        project = FakeProject(media_pool)
        audit = {
            "matches": [
                make_match("/references/A001.jpg", 1, 100, 124),
                make_match("/references/B002.jpg", 1, 200, 224),
                make_match("/references/C003.jpg", 1, 300, 324),
            ]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("contains 2 item", str(raised.exception))
        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertEqual(timeline.GetTrackName("video", 1), "V1")

    def test_add_track_false_return_is_accepted_when_track_was_inserted(self):
        timeline = FakeTimeline(
            [[]],
            names={1: "V1"},
            add_returns_false_after_insert=True,
        )
        media_pool = FakeMediaPool(timeline=timeline)
        project = FakeProject(media_pool)
        audit = {
            "matches": [make_match(
                "/references/A001.jpg", 1, 100, 124
            )]
        }

        result = self._build(project, timeline, audit)

        self.assertEqual(result.track_indices, (2,))
        self.assertEqual(
            timeline.GetTrackName("video", 2), overlay.TRACK_PREFIX + "1"
        )

    def test_wrong_add_track_position_never_mutates_existing_target_track(self):
        user_title = FakeTimelineItem(
            "USER TITLE", "/graphics/title.setting", "user-title", 0, 24
        )
        timeline = FakeTimeline(
            [[], [user_title]],
            names={1: "V1", 2: "USER TITLE TRACK"},
            add_returns_false_after_insert=True,
            add_ignores_requested_index=True,
        )
        media_pool = FakeMediaPool(timeline=timeline)
        project = FakeProject(media_pool)
        audit = {
            "matches": [make_match(
                "/references/A001.jpg", 1, 100, 124
            )]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("requested V2", str(raised.exception))
        self.assertEqual(timeline.GetTrackCount("video"), 2)
        self.assertEqual(timeline.GetTrackName("video", 2), "USER TITLE TRACK")
        self.assertIs(timeline.GetItemListInTrack("video", 2)[0], user_title)
        self.assertNotIn("USER TITLE TRACK", timeline.deleted_track_names)

    def test_staging_name_noop_is_detected_and_extra_track_is_removed(self):
        timeline = FakeTimeline(
            [[]],
            names={1: "V1"},
            rename_noop_prefixes=(overlay.STAGING_PREFIX,),
        )
        media_pool = FakeMediaPool(timeline=timeline)
        project = FakeProject(media_pool)
        audit = {
            "matches": [make_match(
                "/references/A001.jpg", 1, 100, 124
            )]
        }

        with self.assertRaises(overlay.StillOverlayError):
            self._build(project, timeline, audit)

        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertEqual(timeline.GetTrackName("video", 1), "V1")

    def test_final_validation_catches_source_changed_after_its_append(self):
        low_source = FakeTimelineItem(
            "A001", "/camera/A001.mov", "source-a001", 100, 124
        )
        high_source = FakeTimelineItem(
            "B002", "/camera/B002.mov", "source-b002", 200, 224
        )
        timeline = FakeTimeline(
            [[low_source], [high_source]], names={1: "V1", 2: "V2"}
        )

        class MutatingMediaPool(FakeMediaPool):
            def AppendToTimeline(self, clip_infos):
                result = super(MutatingMediaPool, self).AppendToTimeline(
                    clip_infos
                )
                if self.append_count == 2:
                    high_source.end += 1
                return result

        media_pool = MutatingMediaPool(timeline=timeline)
        project = FakeProject(media_pool)
        first = make_record("/references/A001.jpg")
        second = make_record("/references/B002.jpg")
        audit = overlay.audit_timeline(
            timeline, make_index(first, second)
        )

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("changed", str(raised.exception).lower())
        self.assertFalse(any(
            timeline.GetTrackName("video", index).startswith(
                overlay.RESERVED_TRACK_PREFIX
            )
            for index in range(1, timeline.GetTrackCount("video") + 1)
        ))

    def test_wrong_jpeg_returned_by_append_is_rejected(self):
        class WrongMediaPool(FakeMediaPool):
            def AppendToTimeline(self, clip_infos):
                wrong_info = dict(clip_infos[0])
                wrong_info["mediaPoolItem"] = FakeMediaPoolItem(
                    "/references/WRONG.jpg"
                )
                return super(WrongMediaPool, self).AppendToTimeline(
                    [wrong_info]
                )

        timeline = FakeTimeline([[]], names={1: "V1"})
        media_pool = WrongMediaPool(timeline=timeline)
        project = FakeProject(media_pool)
        audit = {
            "matches": [make_match(
                "/references/A001.jpg", 1, 100, 124
            )]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("wrong JPEG", str(raised.exception))
        self.assertEqual(timeline.GetTrackCount("video"), 1)

    def test_final_validation_rejects_dropped_overlay_item(self):
        class DroppingMediaPool(FakeMediaPool):
            def AppendToTimeline(self, clip_infos):
                result = super(DroppingMediaPool, self).AppendToTimeline(
                    clip_infos
                )
                if self.append_count == 2:
                    track_index = int(clip_infos[0]["trackIndex"])
                    self.timeline.tracks[track_index - 1].pop(0)
                return result

        timeline = FakeTimeline([[]], names={1: "V1"})
        media_pool = DroppingMediaPool(timeline=timeline)
        project = FakeProject(media_pool)
        audit = {
            "matches": [
                make_match("/references/A001.jpg", 1, 100, 124),
                make_match("/references/B002.jpg", 1, 200, 224),
            ]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("contains 1 item", str(raised.exception))
        self.assertEqual(timeline.GetTrackCount("video"), 1)

    def test_failed_append_deletes_only_staging_tracks_and_keeps_prior_build(self):
        timeline, media_pool, project, old_item, old_name = self._rebuild_context(
            append_failure_at=2
        )
        audit = {
            "matches": [
                make_match("/references/A001.jpg", 1, 100, 124),
                make_match("/references/B002.jpg", 2, 200, 224),
            ]
        }

        with self.assertRaises(overlay.StillOverlayError):
            self._build(project, timeline, audit)

        self.assertEqual(timeline.GetTrackCount("video"), 3)
        self.assertEqual(timeline.GetTrackName("video", 3), old_name)
        self.assertIs(timeline.GetItemListInTrack("video", 3)[0], old_item)
        self.assertTrue(timeline.GetIsTrackLocked("video", 3))
        self.assertEqual(len(timeline.deleted_track_names), 2)
        self.assertTrue(all(
            name.startswith(overlay.STAGING_PREFIX)
            for name in timeline.deleted_track_names
        ))
        self.assertNotIn(old_name, timeline.deleted_track_names)

    def test_stale_staging_below_source_does_not_shift_audited_target(self):
        source = FakeTimelineItem(
            "B002", "/camera/B002.mov", "source-b002", 200, 224
        )
        stale_name = overlay._staging_track_name("orphaned", 1)
        events = []
        timeline = FakeTimeline(
            [[], [], [source]],
            names={1: "V1", 2: stale_name, 3: "V3"},
            enabled={2: False},
            events=events,
        )
        media_pool = FakeMediaPool(timeline=timeline, events=events)
        project = FakeProject(media_pool)
        record = make_record("/references/B002.jpg")
        audit = overlay.audit_timeline(timeline, make_index(record))

        result = self._build(project, timeline, audit)

        self.assertEqual(result.track_indices, (3,))
        self.assertEqual(timeline.GetTrackName("video", 2), "V3")
        self.assertEqual(
            timeline.GetTrackName("video", 3), overlay.TRACK_PREFIX + "1"
        )
        self.assertIn(stale_name, timeline.deleted_track_names)

    def test_partial_rollback_delete_never_disables_shifted_source_track(self):
        events = []
        timeline = FakeTimeline(
            [[], [], [], [], []],
            names={index: "V{}".format(index) for index in range(1, 6)},
            events=events,
            delete_failure_suffixes=("Lane 1",),
        )
        media_pool = FakeMediaPool(
            timeline=timeline, events=events, append_failure_at=2
        )
        project = FakeProject(media_pool)
        audit = {
            "matches": [
                make_match("/references/A001.jpg", 1, 100, 124),
                make_match("/references/C003.jpg", 3, 300, 324),
            ]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("rollback", str(raised.exception).lower())
        for track_index in range(1, timeline.GetTrackCount("video") + 1):
            name = timeline.GetTrackName("video", track_index)
            if not name.startswith(overlay.RESERVED_TRACK_PREFIX):
                self.assertTrue(
                    timeline.GetIsTrackEnabled("video", track_index), name
                )
        remaining = overlay.find_staging_track_indices(timeline, strict=True)
        self.assertEqual(len(remaining), 1)
        self.assertFalse(timeline.GetIsTrackEnabled("video", remaining[0]))
        self.assertTrue(timeline.GetIsTrackLocked("video", remaining[0]))

    def test_duration_verification_failure_deletes_staging_not_prior_build(self):
        timeline, media_pool, project, old_item, old_name = self._rebuild_context(
            duration_delta_at=1
        )
        audit = {
            "matches": [make_match(
                "/references/B002.jpg", 2, 200, 224
            )]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("unusable reference duration", str(raised.exception))
        self.assertEqual(timeline.GetTrackCount("video"), 3)
        self.assertEqual(timeline.GetTrackName("video", 3), old_name)
        self.assertIs(timeline.GetItemListInTrack("video", 3)[0], old_item)
        self.assertTrue(timeline.GetIsTrackLocked("video", 3))
        self.assertEqual(len(timeline.deleted_track_names), 1)
        self.assertTrue(
            timeline.deleted_track_names[0].startswith(overlay.STAGING_PREFIX)
        )
        self.assertNotIn(old_name, timeline.deleted_track_names)

    def test_rebuild_deletes_old_tracks_only_after_every_new_insert_succeeds(self):
        events = []
        timeline, media_pool, project, old_item, old_name = self._rebuild_context(
            events=events
        )
        audit = {
            "matches": [
                make_match("/references/B002.jpg", 2, 200, 224),
                make_match("/references/A001.jpg", 1, 100, 124),
            ]
        }

        result = self._build(project, timeline, audit)

        append_positions = [
            index for index, event in enumerate(events) if event[0] == "append"
        ]
        old_delete_position = next(
            index for index, event in enumerate(events)
            if event[0] == "delete_track"
            and event[1].startswith(overlay.BACKUP_PREFIX)
        )
        self.assertEqual(len(append_positions), 2)
        self.assertLess(max(append_positions), old_delete_position)
        self.assertEqual(len(timeline.deleted_track_names), 1)
        self.assertTrue(
            timeline.deleted_track_names[0].startswith(overlay.BACKUP_PREFIX)
        )
        self.assertEqual(result.replaced_track_count, 1)
        self.assertEqual(result.track_indices, (2, 4))
        self.assertEqual(timeline.GetTrackName("video", 2), overlay.TRACK_PREFIX + "1")
        self.assertEqual(timeline.GetTrackName("video", 4), overlay.TRACK_PREFIX + "2")
        self.assertFalse(timeline.GetIsTrackEnabled("video", 2))
        self.assertFalse(timeline.GetIsTrackEnabled("video", 4))
        self.assertFalse(any(
            timeline.GetTrackName("video", index).startswith(overlay.STAGING_PREFIX)
            for index in range(1, timeline.GetTrackCount("video") + 1)
        ))

    def test_backup_cleanup_failure_keeps_final_lanes_and_returns_warning(self):
        events = []
        timeline, media_pool, project, old_item, old_name = self._rebuild_context(
            events=events,
            delete_failure_prefixes=(overlay.BACKUP_PREFIX,),
        )
        audit = {
            "matches": [make_match(
                "/references/B002.jpg", 2, 200, 224
            )]
        }

        result = self._build(project, timeline, audit)

        self.assertTrue(result.cleanup_warning)
        self.assertIn("could not be removed", result.cleanup_warning.lower())
        self.assertEqual(result.replaced_track_count, 1)
        final_indices = overlay.find_managed_track_indices(
            timeline, strict=True
        )
        self.assertEqual(tuple(final_indices), result.track_indices)
        self.assertEqual(len(final_indices), 1)
        final_valid, final_reason = overlay.validate_managed_track(
            timeline, final_indices[0]
        )
        self.assertTrue(final_valid, final_reason)
        self.assertFalse(
            timeline.GetIsTrackEnabled("video", final_indices[0])
        )
        backup_indices = overlay.find_backup_track_indices(
            timeline, strict=True
        )
        self.assertEqual(len(backup_indices), 1)
        self.assertTrue(
            timeline.GetIsTrackLocked("video", backup_indices[0])
        )
        self.assertFalse(
            timeline.GetIsTrackEnabled("video", backup_indices[0])
        )
        self.assertTrue(any(
            event[0] == "delete_track_failed"
            and event[1].startswith(overlay.BACKUP_PREFIX)
            for event in events
        ))

    def test_failed_finalization_with_failed_cleanup_avoids_duplicate_final_lane(self):
        old_item = managed_reference_item()
        old_name = overlay.TRACK_PREFIX + "1"
        events = []
        timeline = FakeTimeline(
            [[], [], [old_item]],
            names={1: "V1", 2: "V2", 3: old_name},
            locked={3: True},
            enabled={3: False},
            events=events,
            delete_failure_suffixes=("Lane 1",),
            rename_failure_targets={overlay.TRACK_PREFIX + "2"},
        )
        media_pool = FakeMediaPool(timeline=timeline, events=events)
        project = FakeProject(media_pool)
        audit = {
            "matches": [
                make_match("/references/A001.jpg", 1, 100, 124),
                make_match("/references/B002.jpg", 2, 200, 224),
            ]
        }

        with self.assertRaises(overlay.StillOverlayError) as raised:
            self._build(project, timeline, audit)

        self.assertIn("rollback", str(raised.exception).lower())
        managed = overlay.find_managed_track_indices(timeline, strict=True)
        self.assertEqual(len(managed), 1)
        self.assertIs(timeline.GetItemListInTrack("video", managed[0])[0], old_item)
        staging = overlay.find_staging_track_indices(timeline, strict=True)
        self.assertEqual(len(staging), 1)
        self.assertFalse(timeline.GetIsTrackEnabled("video", staging[0]))
        self.assertTrue(timeline.GetIsTrackLocked("video", staging[0]))


class SaveSafetyTests(unittest.TestCase):
    def test_script_contains_no_project_save_call(self):
        source = inspect.getsource(overlay)
        compact_source = "".join(source.split())
        self.assertNotIn(".SaveProject(", compact_source)


if __name__ == "__main__":
    unittest.main()
