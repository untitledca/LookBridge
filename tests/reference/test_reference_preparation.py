"""Integration boundaries for crop preparation, caching and source identity."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT_PATH = os.path.join(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "Reference Still Auto Overlay.py"
)
SPEC = importlib.util.spec_from_file_location("reference_preparation", SCRIPT_PATH)
overlay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overlay)


class Settings(object):
    def __init__(self, width=None, height=None):
        self.values = {
            "timelineResolutionWidth": width,
            "timelineResolutionHeight": height,
        }

    def GetSetting(self, key):
        return self.values.get(key)


class ImageEngine(object):
    """Deterministic image boundary; geometric detection has separate tests."""
    payload = b"prepared-lossless-png-payload"

    def __init__(self, usable=True):
        self.usable = usable
        self.analysis_calls = []
        self.preparation_calls = []

    def analyze_reference(self, path, width, height):
        self.analysis_calls.append((path, width, height))
        return {
            "usable": self.usable,
            "reason": "Detected frameline" if self.usable else "Ambiguous framelines",
            "kind": "frameline",
            "crop_box": [90, 108, 1830, 974],
        }

    def prepare_reference(self, path, output, width, height, analysis=None):
        self.preparation_calls.append((path, output, width, height))
        with open(output, "wb") as stream:
            stream.write(self.payload)
        return dict(analysis, target_size=[width, height])


class ReferencePreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "camera-still.jpg"
        self.source.write_bytes(b"untouched-original-jpeg-with-burnin")
        self.key = overlay._canonical_path(str(self.source))
        self.match = SimpleNamespace(
            start=100, end=850, display_name="camera-still",
            still=SimpleNamespace(path=str(self.source)),
        )
        self.cache = Path(self.directory.name) / "private-cache"
        self.engine = ImageEngine()
        self.project = Settings("1920", "1080")
        self.timeline = Settings()
        self.cache_patch = mock.patch.object(overlay, "SEQUENCE_CACHE_DIRECTORY", str(self.cache))
        self.engine_patch = mock.patch.object(overlay, "_load_helper", return_value=self.engine)
        self.cache_patch.start()
        self.engine_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.engine_patch.stop)

    def prepare(self, matches=None):
        return overlay.prepare_cropped_reference_paths(
            self.project, self.timeline, matches or [self.match]
        )

    def test_canvas_uses_timeline_overrides_with_project_fallback(self):
        self.assertEqual(overlay.reference_canvas_size(
            Settings("1920", "1080"), Settings("4096", "1716")
        ), (4096, 1716))
        self.assertEqual(overlay.reference_canvas_size(
            Settings("2048", "858"), Settings()
        ), (2048, 858))
        for invalid in (0, -1, 20000, "unreadable"):
            with self.subTest(width=invalid):
                with self.assertRaises(overlay.StillOverlayError):
                    overlay.reference_canvas_size(Settings(invalid, 1080), Settings())

    def test_preparation_deduplicates_original_paths_and_preserves_source(self):
        original_bytes = self.source.read_bytes()
        prepared, failures, notes = self.prepare([self.match, self.match])
        self.assertEqual(failures, {})
        self.assertEqual(set(prepared), {self.key})
        self.assertTrue(prepared[self.key].endswith(".png"))
        self.assertEqual(Path(prepared[self.key]).read_bytes(), self.engine.payload)
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.assertEqual(len(self.engine.preparation_calls), 1)
        self.assertEqual(len(notes), 1)

    def test_ambiguous_framelines_skip_without_creating_media(self):
        self.engine.usable = False
        prepared, failures, notes = self.prepare()
        self.assertEqual(prepared, {})
        self.assertIn("Ambiguous framelines", failures[self.key])
        self.assertIn("SKIPPED", notes[0])
        self.assertEqual(self.engine.preparation_calls, [])
        self.assertEqual([path for path in self.cache.rglob("*") if path.is_file()], [])

    def test_cache_reuse_validates_output_digest_and_regenerates_corruption(self):
        prepared, _, _ = self.prepare()
        output = Path(prepared[self.key])
        reused, failures, _ = self.prepare()
        self.assertEqual(reused, prepared)
        self.assertEqual(failures, {})
        self.assertEqual(len(self.engine.analysis_calls), 1)
        output.write_bytes(b"corrupted-prepared-png")
        regenerated, failures, _ = self.prepare()
        self.assertEqual(regenerated, prepared)
        self.assertEqual(failures, {})
        self.assertEqual(len(self.engine.preparation_calls), 2)
        self.assertEqual(output.read_bytes(), self.engine.payload)
        metadata = json.loads(Path(str(output) + ".json").read_text())
        self.assertEqual(metadata["prepared_sha256"], overlay._sha256_file(str(output)))
        self.assertEqual(metadata["source_sha256"], overlay._sha256_file(str(self.source)))

    def test_cache_separates_source_revisions_and_non_default_canvas(self):
        first, _, _ = self.prepare()
        self.timeline = Settings("2048", "858")
        resized, failures, _ = self.prepare()
        self.assertEqual(failures, {})
        self.assertNotEqual(first[self.key], resized[self.key])
        self.assertIn("2048x858", resized[self.key])
        self.assertEqual(self.engine.analysis_calls[-1][1:], (2048, 858))
        self.assertEqual(self.engine.preparation_calls[-1][2:], (2048, 858))
        self.source.write_bytes(b"new-on-set-jpeg-content")
        changed, failures, _ = self.prepare()
        self.assertEqual(failures, {})
        self.assertNotEqual(changed[self.key], resized[self.key])

    def test_prepared_png_sequences_keep_original_keys_and_exact_payload(self):
        prepared, _, _ = self.prepare()
        records = overlay.prepare_reference_sequences([self.match], prepared)
        self.assertEqual(set(records), {self.key})
        record = records[self.key]
        self.assertEqual(record.original_path, prepared[self.key])
        self.assertEqual((record.required_frames, record.frame_count), (1, 2))
        self.assertTrue(record.frame_pattern.endswith(".png"))
        self.assertTrue(record.media_path.endswith(".png"))
        for frame in range(record.frame_count):
            self.assertEqual(Path(record.frame_pattern % frame).read_bytes(), self.engine.payload)

    def test_provider_imports_original_jpeg_and_returns_prepared_sequence_by_original_key(self):
        prepared, _, _ = self.prepare()
        media_item = object()
        media_pool = object()
        with mock.patch.object(overlay, "import_reference_media", return_value=({}, 1)) as import_stills:
            with mock.patch.object(
                overlay, "import_reference_sequence_media", return_value=({self.key: media_item}, 1)
            ) as import_sequences:
                result = overlay.prepare_timeline_reference_media(media_pool, [self.match], prepared)
        self.assertEqual(result, ({self.key: media_item}, 2, True, {self.key: 2}))
        import_stills.assert_called_once_with(media_pool, [str(self.source)])
        records = import_sequences.call_args.args[1]
        self.assertEqual(set(records), {self.key})
        self.assertTrue(records[self.key].frame_pattern.endswith(".png"))


if __name__ == "__main__":
    unittest.main()
