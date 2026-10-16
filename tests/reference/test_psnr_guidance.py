import importlib.util
import math
import os
import unittest


PATH = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts"), "psnr_guidance.py")
SPEC = importlib.util.spec_from_file_location("reference_psnr_guidance", PATH)
guidance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guidance)


class PSNRGuidanceTests(unittest.TestCase):
    def test_identical_pixels(self):
        text = guidance.format_psnr_result({"psnr_db": float("inf"), "mse": 0})
        self.assertIn("PSNR infinite;", text)
        self.assertIn("0.00% of full scale", text)
        self.assertIn("Identical measured RGB8 pixels", text)

    def test_mse_alone_and_score_alone_agree(self):
        a = guidance.format_psnr_result({"mse": 6.5025})
        b = guidance.format_psnr_result({"psnr_db": 40})
        self.assertEqual(a, b)
        self.assertIn("1.00% of full scale", a)
        self.assertIn("Low pixel error", a)

    def test_exact_two_percent_is_not_a_visual_pass(self):
        text = guidance.format_psnr_result({"mse": 26.01})
        self.assertIn("33.98 dB", text)
        self.assertIn("2.00% of full scale", text)
        self.assertIn("Visual check recommended", text)
        self.assertIn("not a visual pass/fail", guidance.PSNR_GUIDE)
        self.assertNotIn("visually perfect", text.lower())
        self.assertNotIn("40 dB ~1%", text)

    def test_bands_at_and_below_boundaries(self):
        for score, band in ((40, "Low pixel error"), (39.999, "Visual check recommended"),
                            (30, "Visual check recommended"), (29.999, "Larger pixel error")):
            with self.subTest(score=score):
                self.assertIn(band, guidance.format_psnr_result({"psnr_db": score}))

    def test_maximum_error_and_small_nonzero_error(self):
        self.assertIn("100.00%", guidance.format_psnr_result({"psnr_db": 0, "mse": 65025}))
        self.assertIn("<0.01%", guidance.format_psnr_result({"psnr_db": 100}))

    def test_full_scale_percent_is_not_percent_pixels_changed(self):
        self.assertIn("or % changed pixels", guidance.PSNR_GUIDE)
        self.assertIn("34 dB ~2%", guidance.PSNR_GUIDE)

    def test_capture_warning_for_unverified_frame_is_preserved(self):
        for extra in ({"exact": False}, {"warnings": ["The source frame is metadata-mapped, not visually verified: test"]},
                      {"warnings": "not visually verified"}):
            self.assertIn("Frame not visually verified.", guidance.format_psnr_result({"psnr_db": 35, **extra}))
        self.assertNotIn("Frame not visually verified.", guidance.format_psnr_result({"psnr_db": 35, "exact": True}))

    def test_labels_are_bounded_and_result_fits(self):
        text = guidance.format_psnr_result({"psnr_db": 12.3, "exact": False},
                                          "/some/folder/" + "very long shot name\n" * 20,
                                          "03:04:05:06")
        self.assertNotIn("/some/folder/", text)
        self.assertIn("03:04:05:06", text)
        self.assertLessEqual(len(text), 350)
        self.assertLessEqual(len(guidance.format_psnr_result({"psnr_db": 1e308}, "x" * 100, "y" * 50)), 350)

    def test_rejects_invalid_measurements(self):
        invalid = (None, [], {}, {"psnr_db": -1}, {"psnr_db": float("nan")},
                   {"psnr_db": float("-inf")}, {"psnr_db": True}, {"psnr_db": "bad"},
                   {"mse": -1}, {"mse": 65026}, {"mse": float("inf")},
                   {"mse": float("nan")}, {"mse": True},
                   {"mse": 0, "psnr_db": 40}, {"mse": 1, "psnr_db": float("inf")},
                   {"mse": 6.5025, "psnr_db": 30}, {"psnr_db": 40, "channels": 1},
                   {"psnr_db": 40, "domain": "RGB 16-bit encoded pixels"})
        for metric in invalid:
            with self.subTest(metric=metric):
                with self.assertRaises(ValueError):
                    guidance.format_psnr_result(metric)

    def test_engine_result_shape_and_tiny_positive_mse(self):
        metric = {"psnr_db": 10 * math.log10(255**2 / 12.0), "mse": 12.0,
                  "width": 1920, "height": 1080, "channels": 3,
                  "domain": "RGB 8-bit encoded pixels"}
        self.assertIn("37.34 dB", guidance.format_psnr_result(metric))
        self.assertIn("<0.01%", guidance.format_psnr_result({"mse": 5e-324}))


if __name__ == "__main__":
    unittest.main()
