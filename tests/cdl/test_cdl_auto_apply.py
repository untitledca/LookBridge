import importlib.util
import os
import tempfile
import unittest


SCRIPT_PATH = os.path.join(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "CDL Auto Apply.py"
)
SPEC = importlib.util.spec_from_file_location("cdl_auto_apply", SCRIPT_PATH)
cdl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cdl)


SINGLE_CDL = """<?xml version="1.0" encoding="UTF-8"?>
<ColorDecisionList xmlns="urn:ASC:CDL:v1.2">
  <ColorDecision>
    <ColorCorrection id="A001_C001_0101AB">
      <SOPNode>
        <Slope>1.1 1.2 1.3</Slope>
        <Offset>-0.01 0.02 0.03</Offset>
        <Power>0.9 1.0 1.1</Power>
      </SOPNode>
      <SATNode><Saturation>0.85</Saturation></SATNode>
    </ColorCorrection>
  </ColorDecision>
</ColorDecisionList>
"""


class FakeGraph(object):
    def __init__(self, labels):
        self.labels = list(labels)

    def GetNumNodes(self):
        return len(self.labels)

    def GetNodeLabel(self, index):
        return self.labels[index - 1]


class FakeItem(object):
    def __init__(self, labels):
        self.graph = FakeGraph(labels)
        self.applied = []

    def GetNodeGraph(self, layer_index=1):
        return self.graph

    def SetCDL(self, values):
        self.applied.append(values)
        return True


class FakeMediaPoolItem(object):
    def __init__(self, path, properties=None, direct_properties=None):
        self.path = path
        self.properties = properties if properties is not None else {"File Path": path}
        self.direct_properties = direct_properties or {}

    def GetClipProperty(self, property_name=None):
        if property_name is None:
            return dict(self.properties)
        if property_name in self.direct_properties:
            return self.direct_properties[property_name]
        return self.properties.get(property_name, "")

    def GetName(self):
        return os.path.basename(self.path)


class FakeTimelineItem(FakeItem):
    def __init__(self, name, path, uid, start):
        super(FakeTimelineItem, self).__init__(["BALANCE"])
        self.name = name
        self.media = FakeMediaPoolItem(path)
        self.uid = uid
        self.start = start

    def GetMediaPoolItem(self):
        return self.media

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.start + 24


class FakeTimeline(object):
    def __init__(self, tracks):
        self.tracks = tracks

    def GetTrackCount(self, track_type):
        return len(self.tracks)

    def GetItemListInTrack(self, track_type, track_index):
        return self.tracks[track_index - 1]


class CDLParsingTests(unittest.TestCase):
    def test_parses_namespaced_cdl(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "A001_C001_0101AB.cdl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(SINGLE_CDL)
            values = cdl.parse_cdl_file(path)

        self.assertEqual(values.slope, (1.1, 1.2, 1.3))
        self.assertEqual(values.offset, (-0.01, 0.02, 0.03))
        self.assertEqual(values.power, (0.9, 1.0, 1.1))
        self.assertEqual(values.saturation, 0.85)

    def test_ccc_selects_correction_id_matching_filename(self):
        xml = """<ColorCorrectionCollection xmlns="urn:ASC:CDL:v1.2">
          <ColorCorrection id="OTHER">
            <SOPNode><Slope>1 1 1</Slope><Offset>0 0 0</Offset><Power>1 1 1</Power></SOPNode>
            <SATNode><Saturation>1</Saturation></SATNode>
          </ColorCorrection>
          <ColorCorrection id="A002">
            <SOPNode><Slope>2 2 2</Slope><Offset>0 0 0</Offset><Power>1 1 1</Power></SOPNode>
            <SATNode><Saturation>0.5</Saturation></SATNode>
          </ColorCorrection>
        </ColorCorrectionCollection>"""
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "A002.ccc")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(xml)
            values = cdl.parse_cdl_file(path)

        self.assertEqual(values.slope, (2.0, 2.0, 2.0))
        self.assertEqual(values.saturation, 0.5)

    def test_rejects_ambiguous_ccc(self):
        correction = """<ColorCorrection id="{}">
          <SOPNode><Slope>1 1 1</Slope><Offset>0 0 0</Offset><Power>1 1 1</Power></SOPNode>
          <SATNode><Saturation>1</Saturation></SATNode>
        </ColorCorrection>"""
        xml = "<ColorCorrectionCollection>{}{}</ColorCorrectionCollection>".format(
            correction.format("ONE"), correction.format("TWO")
        )
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "THREE.ccc")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(xml)
            with self.assertRaises(cdl.CDLParseError):
                cdl.parse_cdl_file(path)


class MatchingTests(unittest.TestCase):
    def _record(self, path):
        return cdl.CDLRecord(
            path,
            cdl.CDLValues((1, 1, 1), (0, 0, 0), (1, 1, 1), 1),
            cdl.cdl_aliases_for_path(path),
        )

    def test_media_stem_matches_cdl_stem_case_insensitively(self):
        record = self._record("/cdls/a001_c001.cdl")
        index = {alias: [record] for alias in record.aliases}
        match, error = cdl.match_cdl(
            cdl.source_keys_for_name("/media/A001_C001.MOV"), index
        )
        self.assertIs(match, record)
        self.assertEqual(error, "")

    def test_media_extension_can_be_part_of_cdl_filename(self):
        record = self._record("/cdls/A001_C001.mov.cdl")
        index = {alias: [record] for alias in record.aliases}
        match, error = cdl.match_cdl(
            cdl.source_keys_for_name("/media/A001_C001.mov"), index
        )
        self.assertIs(match, record)
        self.assertEqual(error, "")

    def test_filename_matching_is_camera_and_extension_agnostic(self):
        sources = (
            "/arri/A001_C001_0101AB.ARI",
            "/red/B002_C014_0202CD_001.R3D",
            "/dji/DJI_20260315_104233_001.MP4",
            "/blackmagic/CAM_A_001_2026-03-15_1200_C0001.BRAW",
            "/sony/C0007.MXF",
            "/future-camera/SHOT.0042.NEWRAW",
        )
        for source in sources:
            source_stem = os.path.splitext(os.path.basename(source))[0]
            record = self._record("/cdls/{}.cdl".format(source_stem))
            index = {alias: [record] for alias in record.aliases}

            match, error = cdl.match_cdl(cdl.source_keys_for_name(source), index)

            self.assertIs(match, record, source)
            self.assertEqual(error, "", source)

    def test_only_final_camera_extension_is_removed(self):
        record = self._record("/cdls/Sony.C0007.v2.cdl")
        index = {alias: [record] for alias in record.aliases}

        match, error = cdl.match_cdl(
            cdl.source_keys_for_name("/sony/Sony.C0007.v2.MXF"), index
        )

        self.assertIs(match, record)
        self.assertEqual(error, "")

    def test_red_chunk_filename_matches_logical_clip_cdl(self):
        record = self._record("/cdls/A001_C001_0101AB.cdl")
        index = {alias: [record] for alias in record.aliases}
        match, error = cdl.match_cdl(
            cdl.source_keys_for_name(
                "/media/A001_C001_0101AB.RDC/A001_C001_0101AB_001.R3D"
            ),
            index,
        )
        self.assertIs(match, record)
        self.assertEqual(error, "")

    def test_red_exact_chunk_filename_has_priority(self):
        logical = self._record("/cdls/A001_C001_0101AB.cdl")
        physical = self._record("/cdls/A001_C001_0101AB_001.cdl")
        index = {}
        for record in (logical, physical):
            for alias in record.aliases:
                index.setdefault(alias, []).append(record)

        match, error = cdl.match_cdl(
            cdl.source_keys_for_name("/media/A001_C001_0101AB_001.R3D"), index
        )

        self.assertIs(match, physical)
        self.assertEqual(error, "")

    def test_red_rdc_directory_is_a_filename_fallback(self):
        keys = cdl.source_keys_for_name(
            "/media/A001_C001_0101AB.RDC/unexpected_name.R3D"
        )
        self.assertIn("a001_c001_0101ab", keys)

    def test_direct_clip_property_recovers_red_filename(self):
        media = FakeMediaPoolItem(
            "",
            properties={"Clip Name": "Editorial rename"},
            direct_properties={
                "File Path": "/media/A001_C001_0101AB.RDC/A001_C001_0101AB_001.R3D"
            },
        )

        class TimelineItem(object):
            def GetMediaPoolItem(self):
                return media

            def GetName(self):
                return "Editorial rename"

        names = cdl.get_clip_source_names(TimelineItem())
        keys = cdl.clip_source_keys(names)

        self.assertIn("a001_c001_0101ab_001.r3d", keys)
        self.assertIn("a001_c001_0101ab", keys)

    def test_real_file_path_excludes_renamed_timeline_label(self):
        media = FakeMediaPoolItem(
            "/media/SONY_C0007.MXF",
            properties={
                "File Path": "/media/SONY_C0007.MXF",
                "Clip Name": "Editorial rename",
            },
        )

        class TimelineItem(object):
            def GetMediaPoolItem(self):
                return media

            def GetName(self):
                return "Editorial rename"

        names = cdl.get_clip_source_names(TimelineItem())

        self.assertIn("/media/SONY_C0007.MXF", names)
        self.assertNotIn("Editorial rename", names)

    def test_spanned_red_paths_are_split_into_filenames(self):
        names = cdl._source_strings(
            "/red/A001_C001_0101AB_001.R3D;/red/A001_C001_0101AB_002.R3D"
        )
        self.assertEqual(len(names), 2)
        self.assertEqual(
            names[1], "/red/A001_C001_0101AB_002.R3D"
        )

    def test_duplicate_stem_is_ambiguous(self):
        first = self._record("/one/A001.cdl")
        second = self._record("/two/A001.cdl")
        index = {"a001": [first, second]}
        match, error = cdl.match_cdl(["a001"], index)
        self.assertIsNone(match)
        self.assertIn("Ambiguous", error)

    def test_audit_matches_every_timeline_instance_of_same_source(self):
        record = self._record("/cdls/A001.cdl")
        index = {alias: [record] for alias in record.aliases}
        timeline = FakeTimeline([[
            FakeTimelineItem("Shot 1", "/media/A001.mov", "one", 100),
            FakeTimelineItem("Shot 1 pickup", "/media/A001.mov", "two", 200),
        ]])

        audit = cdl.audit_timeline(timeline, index)

        self.assertEqual(audit["total_video_items"], 2)
        self.assertEqual(len(audit["matches"]), 2)
        self.assertEqual([match.uid for match in audit["matches"]], ["one", "two"])
        self.assertTrue(all(match.node_state == "missing" for match in audit["matches"]))


class NodeTests(unittest.TestCase):
    def test_finds_only_labeled_cdl_node(self):
        item = FakeItem(["BALANCE", "CDL", "LOOK"])
        state, index, count, error = cdl.inspect_cdl_node(item)
        self.assertEqual((state, index, count, error), ("existing", 2, 3, ""))

    def test_duplicate_cdl_nodes_are_rejected(self):
        item = FakeItem(["CDL", "LOOK", "cdl"])
        state, index, count, error = cdl.inspect_cdl_node(item)
        self.assertEqual(state, "duplicate")
        self.assertIsNone(index)
        self.assertEqual(count, 3)
        self.assertIn("Multiple", error)

    def test_apply_targets_only_requested_node(self):
        item = FakeItem(["BALANCE", "CDL", "LOOK"])
        values = cdl.CDLValues((1.1, 1.2, 1.3), (0, 0, 0), (1, 1, 1), 0.9)
        cdl.apply_cdl(item, 2, values)
        self.assertEqual(len(item.applied), 1)
        self.assertEqual(item.applied[0]["NodeIndex"], "2")
        self.assertEqual(item.applied[0]["Slope"], "1.1 1.2 1.3")

    def test_new_node_is_verified_before_it_is_labeled(self):
        item = FakeItem(["BALANCE"])
        actions = []
        original_action = cdl.color_node_action

        def action(name):
            actions.append(name)
            if name == "append":
                item.graph.labels.append("")
            elif name == "label":
                item.graph.labels[-1] = "CDL"
            elif name == "delete":
                item.graph.labels.pop()
            return name

        cdl.color_node_action = action
        try:
            node_index = cdl.create_verified_cdl_node(item, 1)
        finally:
            cdl.color_node_action = original_action

        self.assertEqual(node_index, 2)
        self.assertEqual(actions, ["append", "label"])
        self.assertEqual(item.graph.labels, ["BALANCE", "CDL"])

    def test_unverified_append_is_cleaned_up_before_labeling(self):
        item = FakeItem(["BALANCE"])
        actions = []
        original_action = cdl.color_node_action
        original_wait = cdl._wait_for_node_count
        cdl.color_node_action = lambda name: actions.append(name)
        cdl._wait_for_node_count = lambda item, count, timeout=1.0: False
        try:
            with self.assertRaises(cdl.NodeGraphModeError):
                cdl.create_verified_cdl_node(item, 1)
        finally:
            cdl.color_node_action = original_action
            cdl._wait_for_node_count = original_wait

        self.assertEqual(actions, ["append", "delete"])

    def test_cleanup_failure_is_reported_as_a_hard_error(self):
        item = FakeItem(["BALANCE"])
        original_action = cdl.color_node_action
        original_wait = cdl._wait_for_node_count

        def action(name):
            if name == "delete":
                raise cdl.ResolveError("Delete failed")
            return name

        cdl.color_node_action = action
        cdl._wait_for_node_count = lambda item, count, timeout=1.0: False
        try:
            with self.assertRaises(cdl.NodeCleanupError) as raised:
                cdl.create_verified_cdl_node(item, 1)
        finally:
            cdl.color_node_action = original_action
            cdl._wait_for_node_count = original_wait

        self.assertIn("cleanup also failed", str(raised.exception))


class TimecodeTests(unittest.TestCase):
    def test_non_drop_round_trip(self):
        frames = cdl.timecode_to_frames("01:00:01:00", 24, False)
        self.assertEqual(frames, 86424)
        self.assertEqual(cdl.frames_to_timecode(frames, 24, False), "01:00:01:00")

    def test_2997_drop_frame_minute_boundary(self):
        frames = cdl.timecode_to_frames("00:01:00;02", 30, True)
        self.assertEqual(frames, 1800)
        self.assertEqual(cdl.frames_to_timecode(frames, 30, True), "00:01:00;02")

    def test_drop_frame_round_trips_at_boundaries(self):
        for nominal_rate, frame_numbers in (
            (30, (0, 1799, 1800, 17982, 107891)),
            (60, (0, 3599, 3600, 35964, 215783)),
        ):
            for frame_number in frame_numbers:
                timecode = cdl.frames_to_timecode(frame_number, nominal_rate, True)
                self.assertEqual(
                    cdl.timecode_to_frames(timecode, nominal_rate, True),
                    frame_number,
                    (nominal_rate, frame_number, timecode),
                )


class DialogTests(unittest.TestCase):
    def test_cancel_placeholder_does_not_corrupt_cancelled_result(self):
        captured = {}
        original_osascript = cdl._osascript

        def capture(script, arguments=None):
            captured["script"] = script
            captured["arguments"] = arguments
            return "Cancel"

        cdl._osascript = capture
        try:
            result = cdl.macos_dialog(
                "Review", buttons=["Cancel", "Apply"],
                default_button="Cancel", cancel_button="Cancel", caution=True
            )
        finally:
            cdl._osascript = original_osascript

        self.assertEqual(result, "Cancel")
        self.assertIn('return "CANCELLED"', captured["script"])
        self.assertNotIn("__CANCEL__", captured["script"])
        self.assertEqual(captured["arguments"], ["Review", cdl.SCRIPT_NAME])


if __name__ == "__main__":
    unittest.main()
