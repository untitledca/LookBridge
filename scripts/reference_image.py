"""Deterministic reference framing and RGB8 PSNR, with no required pip packages.

JPEG decoding uses macOS's bundled ``sips``. PNG pixels, crop detection and PSNR
use the Python standard library; Pillow is an optional decoder on other systems.
Original images are never modified. Detection requires four supported straight
framelines and a surrounding monitor border; uncertain captures fail closed.
"""
from __future__ import division

import binascii
import math
import os
import struct
import subprocess
import tempfile
import zlib

ENGINE_VERSION = "1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImagePreparationError(RuntimeError):
    pass


class RGBImage(object):
    def __init__(self, width, height, pixels, metadata=()):
        self.width = int(width)
        self.height = int(height)
        self.pixels = bytes(pixels)
        self.metadata = list(metadata)
        if len(self.pixels) != self.width * self.height * 3:
            raise ImagePreparationError("Invalid RGB image buffer.")


def _chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff))


def write_png(image, path):
    """Write lossless RGB8 pixels; retain color-description chunks, not EXIF."""
    stride = image.width * 3
    compressor = zlib.compressobj(6)
    payload = []
    for offset in range(0, len(image.pixels), stride):
        payload.append(compressor.compress(b"\0" + image.pixels[offset:offset + stride]))
    payload.append(compressor.flush())
    with open(path, "wb") as handle:
        handle.write(PNG_SIGNATURE)
        handle.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", image.width, image.height,
                                                8, 2, 0, 0, 0)))
        for kind, data in image.metadata:
            if kind in (b"iCCP", b"sRGB", b"gAMA", b"cHRM"):
                handle.write(_chunk(kind, data))
        handle.write(_chunk(b"IDAT", b"".join(payload)))
        handle.write(_chunk(b"IEND", b""))


def read_png(path):
    """Decode non-interlaced PNG. 16-bit inputs are explicitly reduced to RGB8."""
    metadata, packed, palette, transparency = [], [], None, None
    width = height = bit_depth = color_type = None
    with open(path, "rb") as handle:
        if handle.read(8) != PNG_SIGNATURE:
            raise ImagePreparationError("Input is not a PNG image.")
        while True:
            header = handle.read(8)
            if len(header) != 8:
                raise ImagePreparationError("Truncated PNG image.")
            length, kind = struct.unpack(">I4s", header)
            if length > 512 * 1024 * 1024:
                raise ImagePreparationError("PNG chunk is too large.")
            data, crc = handle.read(length), handle.read(4)
            if len(data) != length or len(crc) != 4:
                raise ImagePreparationError("Truncated PNG chunk.")
            if (binascii.crc32(kind + data) & 0xffffffff) != struct.unpack(">I", crc)[0]:
                raise ImagePreparationError("PNG checksum mismatch.")
            if kind == b"IHDR":
                width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
                if not width or not height or width * height > 100000000:
                    raise ImagePreparationError("Unsupported PNG dimensions.")
                if compression or filtering or interlace:
                    raise ImagePreparationError("Interlaced PNG needs conversion before comparison.")
            elif kind == b"IDAT":
                packed.append(data)
            elif kind == b"PLTE":
                palette = data
            elif kind == b"tRNS":
                transparency = data
            elif kind in (b"iCCP", b"sRGB", b"gAMA", b"cHRM"):
                metadata.append((kind, data))
            elif kind == b"IEND":
                break
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None or bit_depth not in (8, 16) or (color_type == 3 and bit_depth != 8):
        raise ImagePreparationError("PNG must use 8/16-bit RGB or grayscale pixels.")
    byte_depth = bit_depth // 8
    bpp, stride = channels * byte_depth, width * channels * byte_depth
    raw = zlib.decompress(b"".join(packed))
    if len(raw) != (stride + 1) * height:
        raise ImagePreparationError("Unexpected PNG pixel-data length.")
    previous = bytearray(stride)
    decoded = bytearray()
    for row_id in range(height):
        offset = row_id * (stride + 1)
        filter_type = raw[offset]
        row = bytearray(raw[offset + 1:offset + 1 + stride])
        if filter_type == 1:
            for index in range(bpp, stride):
                row[index] = (row[index] + row[index - bpp]) & 255
        elif filter_type == 2:
            row = bytearray((value + above) & 255 for value, above in zip(row, previous))
        elif filter_type in (3, 4):
            for index in range(stride):
                left = row[index - bpp] if index >= bpp else 0
                above = previous[index]
                corner = previous[index - bpp] if index >= bpp else 0
                if filter_type == 3:
                    predictor = (left + above) // 2
                else:
                    estimate = left + above - corner
                    dl, da, dc = abs(estimate - left), abs(estimate - above), abs(estimate - corner)
                    predictor = left if dl <= da and dl <= dc else (above if da <= dc else corner)
                row[index] = (row[index] + predictor) & 255
        elif filter_type != 0:
            raise ImagePreparationError("Unsupported PNG filter.")
        decoded.extend(row)
        previous = row
    if color_type == 2 and bit_depth == 8 and transparency is None:
        pixels = decoded
    else:
        pixels = bytearray(width * height * 3)
        for pixel_id, offset in enumerate(range(0, len(decoded), bpp)):
            values = decoded[offset:offset + bpp:byte_depth]
            if color_type == 3:
                palette_offset = values[0] * 3
                if palette is None or palette_offset + 3 > len(palette):
                    raise ImagePreparationError("Invalid PNG palette.")
                rgb = palette[palette_offset:palette_offset + 3]
                if transparency is not None and values[0] < len(transparency) and transparency[values[0]] != 255:
                    raise ImagePreparationError("Transparent pixels cannot be compared as RGB.")
            else:
                rgb = values[:3] if color_type in (2, 6) else bytes([values[0]]) * 3
                if color_type in (4, 6) and values[-1] != 255:
                    raise ImagePreparationError("Transparent pixels cannot be compared as RGB.")
                if transparency is not None:
                    raise ImagePreparationError("Transparent PNG color keys are not supported.")
            pixels[pixel_id * 3:pixel_id * 3 + 3] = rgb
    return RGBImage(width, height, pixels, metadata)


def _sips(arguments):
    executable = "/usr/bin/sips"
    if not os.path.isfile(executable):
        return False
    command = [executable] + [os.fsencode(arg) if isinstance(arg, str) else arg for arg in arguments]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ImagePreparationError("Image conversion failed: " + detail)
    return True


def load_image(path):
    if str(path).lower().endswith(".png"):
        try:
            return read_png(path)
        except ImagePreparationError as exc:
            if "Interlaced" not in str(exc):
                raise
    with tempfile.TemporaryDirectory(prefix="resolve-reference-decode-") as temp_dir:
        converted = os.path.join(temp_dir, "decoded.png")
        if _sips(["-s", "format", "png", path, "--out", converted]):
            return read_png(converted)
    try:
        from PIL import Image
    except (ImportError, OSError):
        raise ImagePreparationError("JPEG decoding requires macOS sips or Pillow.")
    with Image.open(path) as image:
        if image.mode not in ("RGB", "L"):
            raise ImagePreparationError("Only RGB or grayscale reference images are supported.")
        image = image.convert("RGB")
        return RGBImage(image.width, image.height, image.tobytes())


def image_dimensions(path):
    """Read PNG/JPEG dimensions without decoding pixels; fall back for others."""
    with open(path, "rb") as handle:
        header = handle.read(24)
        if header[:8] == PNG_SIGNATURE and len(header) == 24:
            return struct.unpack(">II", header[16:24])
        if header[:2] == b"\xff\xd8":
            handle.seek(2)
            while True:
                marker = handle.read(1)
                if not marker:
                    break
                if marker != b"\xff":
                    continue
                while marker == b"\xff":
                    marker = handle.read(1)
                if not marker or marker[0] in (0xd9, 0xda):
                    break
                if marker[0] in (0x00, 0x01) or 0xd0 <= marker[0] <= 0xd8:
                    continue
                length_data = handle.read(2)
                if len(length_data) != 2:
                    break
                length = struct.unpack(">H", length_data)[0]
                if length < 2:
                    break
                if marker[0] in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                    frame_header = handle.read(5)
                    if len(frame_header) == 5:
                        height, width = struct.unpack(">HH", frame_header[1:])
                        return width, height
                    break
                handle.seek(length - 2, 1)
    image = load_image(path)
    return image.width, image.height


def _luma(image):
    pixels = image.pixels
    return bytearray((pixels[i] * 54 + pixels[i + 1] * 183 + pixels[i + 2] * 19) >> 8
                     for i in range(0, len(pixels), 3))


def _groups(scores, minimum, gap=6):
    groups = []
    for position, strength in scores:
        if strength < minimum:
            continue
        if groups and position - groups[-1][-1][0] <= gap:
            groups[-1].append((position, strength))
        else:
            groups.append([(position, strength)])
    result = []
    for group in groups:
        position, strength = max(group, key=lambda item: item[1])
        result.append((position, strength, group[0][0], group[-1][0]))
    return result


def _detect(image):
    width, height = image.width, image.height
    if width < 160 or height < 90:
        raise ImagePreparationError("Reference is too small for reliable frameline detection.")
    gray = _luma(image)
    pixels = image.pixels
    delta = max(4, int(round(min(width, height) / 180.0)))
    sx, sy = max(1, width // 480), max(1, height // 300)

    def ridge(x, y, vertical):
        offset = y * width + x
        d = delta if vertical else delta * width
        value = gray[offset]
        pixel = offset * 3
        neutral = max(pixels[pixel:pixel + 3]) - min(pixels[pixel:pixel + 3]) <= 38
        # A monitor may dim everything outside the frameline. Its neutral line
        # can then lie between the two neighboring brightness values, instead
        # of being brighter than both. Require contrast on both sides without
        # assuming the sign, which also supports dark lines on bright scenes.
        return (value >= 25 and neutral and abs(value - gray[offset - d]) >= 8
                and abs(value - gray[offset + d]) >= 8)

    xs = list(range(max(delta, int(width * .035)), min(width - delta, int(width * .965)), sx))
    ys = list(range(max(delta, int(height * .045)), min(height - delta, int(height * .955)), sy))
    horizontal = []
    for y in range(delta, height - delta):
        if .44 * height < y < .56 * height:
            continue
        horizontal.append((y, sum(ridge(x, y, False) for x in xs) / float(len(xs))))
    vertical = []
    for x in range(delta, width - delta):
        if .38 * width < x < .62 * width:
            continue
        vertical.append((x, sum(ridge(x, y, True) for y in ys) / float(len(ys))))
    row_groups = _groups(horizontal, .24, delta)
    column_groups = _groups(vertical, .25, delta)
    def line_band(scores, group):
        position, strength = group[:2]
        values = dict(scores)
        limit = strength * .70
        start = end = position
        while position - start < delta and values.get(start - 1, 0) >= limit:
            start -= 1
        while end - position < delta and values.get(end + 1, 0) >= limit:
            end += 1
        return start, end

    row_bounds = {item[0]: line_band(horizontal, item) for item in row_groups}
    column_bounds = {item[0]: line_band(vertical, item) for item in column_groups}
    rows = [item[:2] for item in sorted(row_groups, key=lambda pair: pair[1], reverse=True)[:12]]
    columns = [item[:2] for item in sorted(column_groups, key=lambda pair: pair[1], reverse=True)[:12]]

    edge_positions = []
    for y in range(0, height, sy * 2):
        edge_positions.extend((y * width + int(width * .015), y * width + int(width * .985)))
    for x in range(0, width, sx * 2):
        edge_positions.extend((int(height * .015) * width + x, int(height * .985) * width + x))
    black_border = sum(gray[index] < 20 for index in edge_positions) / float(len(edge_positions))

    def edge_support(start, end, fixed, vertical_edge):
        samples = range(start + delta, end - delta, max(1, (end - start) // 350))
        good = 0
        count = 0
        for along in samples:
            count += 1
            good += any(ridge(fixed + jitter, along, True) if vertical_edge
                        else ridge(along, fixed + jitter, False)
                        for jitter in (-1, 0, 1))
        return good / float(max(1, count))

    candidates = []
    for top, ts in rows:
        if top >= height * .44:
            continue
        for bottom, bs in rows:
            if bottom <= height * .56 or bottom - top < height * .45:
                continue
            for left, ls in columns:
                if left >= width * .38:
                    continue
                for right, rs in columns:
                    if right <= width * .62 or right - left < width * .45:
                        continue
                    if abs(left + right - width) > width * .10 or abs(top + bottom - height) > height * .12:
                        continue
                    support = (edge_support(left, right, top, False), edge_support(left, right, bottom, False),
                               edge_support(top, bottom, left, True), edge_support(top, bottom, right, True))
                    if min(support) < .32 or sum(support) / 4.0 < .52:
                        continue
                    # Camera framelines are normally centered. A strong scene
                    # edge (e.g. a door jamb) must not tie the complete frame
                    # merely because it intersects the top and bottom lines.
                    center_error = (abs(left + right - width) / float(width)
                                    + abs(top + bottom - height) / float(height))
                    score = sum(support) / 4.0 - .8 * center_error
                    candidates.append((score, (left, top, right, bottom), support))
    candidates.sort(reverse=True)
    if candidates and black_border >= .35:
        best = candidates[0]
        if len(candidates) > 1 and candidates[1][0] > best[0] - .10:
            other = candidates[1][1]
            if max(abs(a - b) for a, b in zip(best[1], other)) > delta * 3:
                return {"crop_box": None, "confidence": 0.0, "kind": "ambiguous", "usable": False,
                        "candidates": [{"box": list(item[1]), "score": round(item[0], 3)} for item in candidates[:4]],
                        "reason": "Multiple plausible frameline rectangles; crop must be reviewed."}
        left, top, right, bottom = best[1]
        # Use the *inner edge* of each detected line band. A fixed inset around
        # the brightest line pixel shifts framing as scene brightness changes.
        crop_box = [column_bounds[left][1] + 2, row_bounds[top][1] + 2,
                    column_bounds[right][0] - 1, row_bounds[bottom][0] - 1]
        return {"crop_box": crop_box,
                "confidence": round(min(.99, .65 + best[0] * .34), 3), "kind": "frameline", "usable": True,
                "frameline_box": list(best[1]), "boundary_inset": "detected line-band interior plus 1px antialias guard",
                "warnings": ["Burned-in graphics inside the framelines remain in the image."],
                "reason": "Four supported framelines detected; outside monitor UI and line pixels excluded."}
    if black_border < .30 and not candidates and len(rows) < 3 and len(columns) < 3:
        return {"crop_box": [0, 0, width, height], "confidence": .8, "kind": "full_frame", "usable": True,
                "reason": "No supported framelines or surrounding monitor border; using the complete image."}
    return {"crop_box": None, "confidence": 0.0, "kind": "ambiguous", "usable": False,
            "reason": "Could not confidently identify a clean image rectangle; automatic comparison is unavailable."}


def analyze_reference(path, target_width=None, target_height=None):
    image = load_image(path)
    analysis = _detect(image)
    analysis.update({"source_size": [image.width, image.height], "engine_version": ENGINE_VERSION})
    if target_width and target_height and analysis["crop_box"]:
        left, top, right, bottom = analysis["crop_box"]
        analysis["aspect_ratio_difference"] = abs((right - left) / float(bottom - top)
                                                  / (float(target_width) / target_height) - 1.0)
    return analysis


def crop_image(image, box):
    left, top, right, bottom = [int(value) for value in box]
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        raise ImagePreparationError("Crop rectangle lies outside the image.")
    pixels = b"".join(image.pixels[(row * image.width + left) * 3:(row * image.width + right) * 3]
                      for row in range(top, bottom))
    return RGBImage(right - left, bottom - top, pixels, image.metadata)


def _resize_bilinear(image, width, height):
    if (image.width, image.height) == (width, height):
        return image
    output = bytearray(width * height * 3)
    for y in range(height):
        source_y = max(0.0, min(image.height - 1.0, (y + .5) * image.height / height - .5))
        y0, fy = int(source_y), source_y - int(source_y)
        y1 = min(y0 + 1, image.height - 1)
        for x in range(width):
            source_x = max(0.0, min(image.width - 1.0, (x + .5) * image.width / width - .5))
            x0, fx = int(source_x), source_x - int(source_x)
            x1 = min(x0 + 1, image.width - 1)
            a, b, c, d = ((y0 * image.width + x0) * 3, (y0 * image.width + x1) * 3,
                          (y1 * image.width + x0) * 3, (y1 * image.width + x1) * 3)
            for channel in range(3):
                value = ((image.pixels[a + channel] * (1 - fx) + image.pixels[b + channel] * fx) * (1 - fy)
                         + (image.pixels[c + channel] * (1 - fx) + image.pixels[d + channel] * fx) * fy)
                output[(y * width + x) * 3 + channel] = int(value + .5)
    return RGBImage(width, height, output, image.metadata)


def prepare_reference(path, output_path, target_width, target_height, analysis=None):
    """Extract framelines and fit without stretching. Return geometry metadata."""
    width, height = int(target_width), int(target_height)
    if width <= 0 or height <= 0 or width * height > 100000000:
        raise ImagePreparationError("Invalid target canvas dimensions.")
    if os.path.realpath(path) == os.path.realpath(output_path):
        raise ImagePreparationError("Prepared output must not replace the original image.")
    image = load_image(path)
    analysis = dict(analysis or _detect(image))
    analysis["source_size"] = [image.width, image.height]
    analysis["engine_version"] = ENGINE_VERSION
    if not analysis.get("usable") or not analysis.get("crop_box"):
        raise ImagePreparationError(analysis.get("reason", "Uncertain framelines."))
    cropped = crop_image(image, analysis["crop_box"])
    scale = min(width / float(cropped.width), height / float(cropped.height))
    fit_width = min(width, max(1, int(round(cropped.width * scale))))
    fit_height = min(height, max(1, int(round(cropped.height * scale))))
    left, top = (width - fit_width) // 2, (height - fit_height) // 2
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    with tempfile.TemporaryDirectory(prefix=".reference-prepare-", dir=output_dir) as temp_dir:
        intermediate = os.path.join(temp_dir, "crop.png")
        resized = os.path.join(temp_dir, "resized.png")
        write_png(cropped, intermediate)
        if (fit_width, fit_height) == (cropped.width, cropped.height):
            fitted = cropped
        elif _sips(["--resampleHeightWidth", str(fit_height), str(fit_width), intermediate, "--out", resized]):
            fitted = read_png(resized)
        else:
            fitted = _resize_bilinear(cropped, fit_width, fit_height)
        canvas = bytearray(width * height * 3)
        for row in range(fit_height):
            offset = ((row + top) * width + left) * 3
            canvas[offset:offset + fit_width * 3] = fitted.pixels[row * fit_width * 3:(row + 1) * fit_width * 3]
        prepared = os.path.join(temp_dir, "prepared.png")
        write_png(RGBImage(width, height, canvas, fitted.metadata), prepared)
        os.replace(prepared, output_path)
    analysis.update({"prepared_path": os.path.abspath(output_path), "target_size": [width, height],
                     "content_box": [left, top, left + fit_width, top + fit_height],
                     "aspect_ratio_difference": abs(cropped.width / float(cropped.height) / (width / float(height)) - 1.0)})
    return analysis


def calculate_psnr(reference_path, comparison_path, region=None):
    """RGB8 encoded-pixel PSNR. Never normalizes color or aligns images silently."""
    reference, comparison = load_image(reference_path), load_image(comparison_path)
    if (reference.width, reference.height) != (comparison.width, comparison.height):
        raise ImagePreparationError("PSNR requires identical image dimensions.")
    if region is not None:
        reference, comparison = crop_image(reference, region), crop_image(comparison, region)
    squared_error = sum((a - b) * (a - b) for a, b in zip(reference.pixels, comparison.pixels))
    mse = squared_error / float(len(reference.pixels))
    return {"psnr_db": float("inf") if not mse else 10.0 * math.log10(255.0 * 255.0 / mse),
            "mse": mse, "width": reference.width, "height": reference.height, "channels": 3,
            "sample_count": len(reference.pixels), "domain": "RGB 8-bit encoded pixels",
            "region": list(region) if region is not None else None}
