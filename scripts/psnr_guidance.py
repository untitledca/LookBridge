"""Short, non-perceptual guidance for the review controller's RGB8 PSNR.

The RGB8 peak is 255: PSNR = 10 * log10(255**2 / MSE), so full-scale
RMS error percent = 100 * sqrt(MSE) / 255 = 100 * 10**(-PSNR / 20).
Formula source: https://www.mathworks.com/help/images/ref/psnr.html

The suggested bands describe numeric pixel error only. They are not a
perceptual-quality standard, a CDL validation test, or an automatic pass/fail.
"""

import math
from collections.abc import Mapping


PSNR_GUIDE = (
    "Higher dB is closer: 40 dB ~1% RMS; 34 dB ~2%; 30 dB ~3.2%; "
    "infinite = identical pixels. RMS is numeric error, not a visual pass/fail "
    "or % changed pixels."
)


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError("{} must be a number, not a boolean".format(name))
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("{} must be a number".format(name)) from None


def _validated_score(metric):
    if not isinstance(metric, Mapping):
        raise ValueError("PSNR result must be a mapping")
    if "psnr_db" not in metric and "mse" not in metric:
        raise ValueError("PSNR result has neither psnr_db nor mse")
    if "channels" in metric and metric["channels"] != 3:
        raise ValueError("The guidance requires three-channel RGB8 measurements")
    if "domain" in metric and metric["domain"] != "RGB 8-bit encoded pixels":
        raise ValueError("The guidance requires RGB 8-bit encoded pixels")

    score = _number(metric["psnr_db"], "psnr_db") if "psnr_db" in metric else None
    mse = _number(metric["mse"], "mse") if "mse" in metric else None
    if score is not None and (math.isnan(score) or score < 0):
        raise ValueError("RGB8 PSNR must be nonnegative or positive infinity")
    if mse is not None and (not math.isfinite(mse) or not 0 <= mse <= 255.0**2):
        raise ValueError("RGB8 MSE must be finite and between 0 and 65025")
    if mse is not None:
        expected = float("inf") if mse == 0 else 20.0 * math.log10(255.0) - 10.0 * math.log10(mse)
        if score is not None and not math.isclose(score, expected, rel_tol=1e-7, abs_tol=1e-6):
            raise ValueError("PSNR and MSE are inconsistent for RGB8 pixels")
        score = expected
        percent = 100.0 * math.sqrt(mse) / 255.0
    else:
        percent = 0.0 if math.isinf(score) else 100.0 * 10.0**(-score / 20.0)
    return score, percent


def _short_label(value, limit):
    # Keep paths, line breaks, and exceptionally long names from filling the UI.
    text = " ".join(str(value or "").replace("\\", "/").rsplit("/", 1)[-1].split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def format_psnr_result(metric, filename="", source_timecode=""):
    """Return a compact interpretation of an unrounded RGB8 score dictionary.

    ``psnr_db`` and/or ``mse`` are accepted. If both exist they must agree.
    A zero MSE produces infinite PSNR, not a division-by-zero error. Optional
    ``exact=False`` or a capture warning containing "not visually verified"
    adds a frame-alignment warning. No score is labelled a perceptual pass.
    Display ``PSNR_GUIDE`` separately beside this result for conversion guidance.
    """
    score, percent = _validated_score(metric)
    if math.isinf(score):
        value = "infinite"
        advice = "Identical measured RGB8 pixels."
    else:
        value = ("{:.2f} dB" if score < 10000 else "{:.3g} dB").format(score)
        if score >= 40.0 - 1e-10:
            advice = "Low pixel error - inspect visually."
        elif score >= 30.0 - 1e-10:
            advice = "Visual check recommended."
        else:
            advice = "Larger pixel error - check frame, framing and color."
    percent_text = "<0.01" if not math.isinf(score) and percent < 0.005 else "{:.2f}".format(percent)
    identity = " @ ".join(part for part in (
        _short_label(filename, 30), _short_label(source_timecode, 14)
    ) if part)
    header = "PSNR {}; RGB8 RMS error {}% of full scale.".format(value, percent_text)
    if identity:
        header = identity + ": " + header
    warnings = metric.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    unverified = metric.get("exact") is False or any(
        "not visually verified" in str(warning).lower() for warning in warnings
    )
    caution = "Frame, crop, JPEG and color transforms affect the result."
    if unverified:
        caution = "Frame not visually verified. " + caution
    return "\n".join((header, advice, caution))
