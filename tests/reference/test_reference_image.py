import importlib.util
import math
import os
import struct
import tempfile
import unittest
import zlib
from unittest import mock


PATH = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "reference_image.py")
SPEC = importlib.util.spec_from_file_location("reference_image_tested", PATH)
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


def solid(width, height, color):
    return engine.RGBImage(width, height, bytes(color) * (width * height))


def framed_image(rectangles=((30, 25, 290, 155),)):
    image = bytearray(320 * 180 * 3)
    for y in range(20, 160):
        for x in range(20, 300):
            # Colored scene with nonuniform scene detail, black monitor border.
            image[(y * 320 + x) * 3:(y * 320 + x) * 3 + 3] = bytes((20 + x % 23, 42 + y % 31, 11))
    for left, top, right, bottom in rectangles:
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                if y in (top, bottom) or x in (left, right):
                    image[(y * 320 + x) * 3:(y * 320 + x) * 3 + 3] = b"\xc0\xc0\xc0"
    return engine.RGBImage(320, 180, image)


class ReferenceImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write(self, name, image):
        path = os.path.join(self.temp.name, name)
        engine.write_png(image, path)
        return path

    def test_rgb_png_lossless_roundtrip_retains_color_metadata(self):
        image = engine.RGBImage(2, 2, bytes(range(12)), [(b"gAMA", struct.pack(">I", 45455))])
        loaded = engine.read_png(self.write("roundtrip.png", image))
        self.assertEqual((loaded.width, loaded.height), (2, 2))
        self.assertEqual(loaded.pixels, image.pixels)
        self.assertEqual(loaded.metadata, image.metadata)

    def test_png_sub_filter_decodes_real_pixels(self):
        data = (engine.PNG_SIGNATURE
                + engine._chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
                + engine._chunk(b"IDAT", zlib.compress(bytes([1, 10, 20, 30, 5, 6, 7])))
                + engine._chunk(b"IEND", b""))
        path = os.path.join(self.temp.name, "sub.png")
        with open(path, "wb") as handle:
            handle.write(data)
        self.assertEqual(engine.read_png(path).pixels, bytes([10, 20, 30, 15, 26, 37]))

    def test_png_rejects_transparency(self):
        data = (engine.PNG_SIGNATURE
                + engine._chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                + engine._chunk(b"IDAT", zlib.compress(bytes([0, 10, 20, 30, 127])))
                + engine._chunk(b"IEND", b""))
        path = os.path.join(self.temp.name, "alpha.png")
        with open(path, "wb") as handle:
            handle.write(data)
        with self.assertRaisesRegex(engine.ImagePreparationError, "Transparent"):
            engine.read_png(path)

    def test_psnr_identical_is_infinity(self):
        path = self.write("identical.png", solid(10, 5, (110, 20, 40)))
        result = engine.calculate_psnr(path, path)
        self.assertTrue(math.isinf(result["psnr_db"]))
        self.assertEqual(result["mse"], 0.0)

    def test_psnr_known_one_level_error_no_color_normalization(self):
        a = self.write("a.png", solid(7, 3, (100, 40, 20)))
        b = self.write("b.png", solid(7, 3, (101, 41, 21)))
        result = engine.calculate_psnr(a, b)
        self.assertEqual(result["mse"], 1.0)
        self.assertAlmostEqual(result["psnr_db"], 48.1308036086791)

    def test_psnr_mismatched_geometry_rejected(self):
        a = self.write("a.png", solid(3, 2, (0, 0, 0)))
        b = self.write("b.png", solid(2, 3, (0, 0, 0)))
        with self.assertRaisesRegex(engine.ImagePreparationError, "identical image dimensions"):
            engine.calculate_psnr(a, b)

    def test_psnr_region_excludes_letterbox(self):
        a = self.write("a.png", solid(3, 2, (0, 0, 0)))
        pixels = bytearray(18)
        pixels[:3] = bytes([255] * 3)
        b = self.write("b.png", engine.RGBImage(3, 2, pixels))
        self.assertTrue(math.isinf(engine.calculate_psnr(a, b, [1, 0, 3, 2])["psnr_db"]))

    def test_detects_four_frame_lines_with_monitor_surround(self):
        result = engine._detect(framed_image())
        self.assertEqual(result["kind"], "frameline")
        left, top, right, bottom = result["crop_box"]
        self.assertGreater(left, 30)
        self.assertGreater(top, 25)
        self.assertLessEqual(right, 290)
        self.assertLessEqual(bottom, 155)

    def test_two_similar_rectangles_fail_closed(self):
        result = engine._detect(framed_image(((30, 25, 290, 155), (50, 40, 270, 140))))
        self.assertFalse(result["usable"])
        self.assertEqual(result["kind"], "ambiguous")

    def test_clean_frame_accepted_black_capture_fails_closed(self):
        self.assertEqual(engine._detect(solid(320, 180, (32, 100, 190)))["kind"], "full_frame")
        self.assertFalse(engine._detect(solid(320, 180, (0, 0, 0)))["usable"])

    def test_preparation_crops_and_letterboxes_without_stretch(self):
        path = self.write("source.png", solid(320, 180, (20, 50, 80)))
        output = os.path.join(self.temp.name, "prepared.png")
        analysis = {"usable": True, "kind": "frameline", "crop_box": [30, 25, 290, 155]}
        # Test pure Python path too; target is half-scale exact 2:1 content.
        with mock.patch.object(engine, "_sips", return_value=False):
            result = engine.prepare_reference(path, output, 160, 90, analysis)
        self.assertEqual(result["content_box"], [0, 5, 160, 85])
        image = engine.read_png(output)
        self.assertEqual(image.pixels[:3], bytes([0, 0, 0]))
        self.assertEqual(image.pixels[(45 * 160 + 80) * 3:(45 * 160 + 80) * 3 + 3], bytes([20, 50, 80]))
        self.assertEqual(engine.read_png(path).pixels, bytes([20, 50, 80]) * (320 * 180))

    def test_ambiguous_preparation_and_original_overwrite_rejected(self):
        path = self.write("source.png", solid(320, 180, (0, 0, 0)))
        with self.assertRaisesRegex(engine.ImagePreparationError, "original"):
            engine.prepare_reference(path, path, 320, 180)
        with self.assertRaises(engine.ImagePreparationError):
            engine.prepare_reference(path, os.path.join(self.temp.name, "out.png"), 320, 180)


if __name__ == "__main__":
    unittest.main()
