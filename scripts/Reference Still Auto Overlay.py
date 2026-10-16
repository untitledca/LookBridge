#!/usr/bin/env python3
"""Build and control filename-matched JPEG reference overlays in Resolve.

The script is designed for DaVinci Resolve's Workspace > Scripts menu. It
matches JPEG basenames to the physical source filenames on the current
timeline, imports the references into a dedicated Media Pool bin, and places
one full-opacity frame at each readable grab timecode. One locked reference
track is created per matched source track so stacked source clips retain their
z-order. The generated tracks can then be shown or hidden as a group for fast
A/B comparison. Cropped lossless two-frame PNG image sequences make
every one-frame reference item trim-addressable despite Resolve's Standard
Still Duration preference. On macOS, full-image Vision OCR locates a readable
source timecode burn-in and creates review markers; a persistent controller
provides Normal/Difference, a separate PSNR measurement, adjustable opacity,
A/B toggle, and grab navigation.
"""

from __future__ import print_function

import argparse
import datetime
import gc
import hashlib
import json
import importlib.util
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata


SCRIPT_NAME = "Reference Still Auto Overlay"
TRACK_PREFIX = "[AUTO REF] Lane "
ITEM_PREFIX = "[AUTO REF] "
STAGING_PREFIX = "[AUTO REF BUILD "
BACKUP_PREFIX = "[AUTO REF OLD "
RESERVED_TRACK_PREFIX = "[AUTO REF"
MEDIA_POOL_FOLDER = "Reference Stills (Auto)"
SEQUENCE_MEDIA_POOL_FOLDER = "Timeline Sources (Auto)"
STILL_EXTENSIONS = {".jpg", ".jpeg"}
REFERENCE_MEDIA_EXTENSIONS = STILL_EXTENSIONS | {".png"}
FILE_PROPERTY_NAMES = (
    "File Path", "File Name", "Filename", "Source File", "Source Filename",
    "Source File Name", "Source Path", "Original Filename", "Original File Name",
    "Original File Path", "Path",
)
DIRECT_FILE_PROPERTY_NAMES = (
    "File Path", "File Name", "Filename", "Source File", "Source Filename"
)
FALLBACK_NAME_PROPERTY_NAMES = ("Name", "Clip Name")
LOG_DIRECTORY = os.path.expanduser(
    "~/Library/Logs/Resolve Reference Still Auto Overlay"
)
if sys.platform == "darwin":
    SEQUENCE_CACHE_DIRECTORY = os.path.expanduser(
        "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
        "Reference Still Auto Overlay/Sequence Cache"
    )
elif os.name == "nt":
    SEQUENCE_CACHE_DIRECTORY = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "Blackmagic Design", "DaVinci Resolve",
        "Reference Still Auto Overlay", "Sequence Cache",
    )
else:
    SEQUENCE_CACHE_DIRECTORY = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
        "resolve-reference-still-auto-overlay", "sequences",
    )
SEQUENCE_FRAME_DIGITS = 8
REFERENCE_OPACITY = 100.0
CROP_PIPELINE_VERSION = "frameline-v1"
_HELPER_MODULES = {}
REVIEW_WINDOW_ID = "com.atomfilmlab.ReferenceStillReview"
REVIEW_MARKER_PREFIX = "resolve-reference-still-auto-overlay:review:v1:"
REVIEW_MARKER_NAME_PREFIX = "[AUTO REF] Grab "
REVIEW_MARKER_COLOR_EXACT = "Pink"
REVIEW_MARKER_COLOR_APPROXIMATE = "Yellow"
VISION_OCR_BATCH_SIZE = 64
REVIEW_VISUAL_CORRECTION_FRAMES = 2
TIMECODE_NAVIGATION_PAGES = {
    "cut", "edit", "color", "fairlight", "deliver",
}
TRACK_PATTERN = re.compile(r"^\[AUTO REF\] Lane ([1-9][0-9]*)$")
STAGING_PATTERN = re.compile(
    r"^\[AUTO REF BUILD [^]]+\] Lane ([1-9][0-9]*)$"
)
BACKUP_PATTERN = re.compile(
    r"^\[AUTO REF OLD [^]]+\] Lane ([1-9][0-9]*)$"
)
TIMECODE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,2})\s*([:;.])\s*(\d{2})\s*[:;.]\s*"
    r"(\d{2})\s*([:;.])\s*(\d{2,3})(?!\d)"
)

VISION_TEXT_JXA = r'''
ObjC.import("Foundation");
ObjC.import("Vision");
function run(argv) {
    var output = [];
    for (var pathIndex = 0; pathIndex < argv.length; pathIndex++) {
        var path = String(argv[pathIndex]);
        try {
            var url = $.NSURL.fileURLWithPath(path);
            var handler = $.VNImageRequestHandler.alloc.initWithURLOptions(
                url, $({})
            );
            var request = $.VNRecognizeTextRequest.alloc.init;
            request.recognitionLevel = 0;
            request.usesLanguageCorrection = false;
            var error = Ref();
            var ok = handler.performRequestsError($([request]), error);
            if (!ok) {
                throw new Error(String(error[0]));
            }
            var texts = [];
            var recognized = [];
            var observations = request.results;
            for (var resultIndex = 0;
                 resultIndex < observations.count;
                 resultIndex++) {
                var observation = observations.objectAtIndex(resultIndex);
                var candidates = observation.topCandidates(1);
                if (candidates.count > 0) {
                    var text = ObjC.unwrap(candidates.firstObject.string);
                    var box = observation.boundingBox;
                    texts.push(text);
                    recognized.push({
                        "text": text,
                        "bbox": [
                            Number(box.origin.x), Number(box.origin.y),
                            Number(box.size.width), Number(box.size.height)
                        ]
                    });
                }
            }
            output.push({
                "path": path, "texts": texts,
                "observations": recognized
            });
        } catch (problem) {
            output.push({"path": path, "error": String(problem)});
        }
    }
    return JSON.stringify(output);
}
'''


class StillOverlayError(Exception):
    pass


class ReviewStateRestoreError(StillOverlayError):
    """Raised when temporary review verification state cannot be restored."""


def _load_helper(name):
    """Load bundled helpers even when Fusion omits __file__/sys.path."""
    if name in _HELPER_MODULES:
        return _HELPER_MODULES[name]
    candidates = []
    if globals().get("__file__"):
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(os.path.dirname(SEQUENCE_CACHE_DIRECTORY), "Modules"))
    candidates.append(os.path.expanduser(
        "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
        "Fusion/Scripts/Utility"
    ))
    for directory in candidates:
        path = os.path.join(directory, name + ".py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("autoref_" + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _HELPER_MODULES[name] = module
            return module
    raise StillOverlayError(
        "Missing bundled helper {}.py; open LookBridge and choose Reinstall Resolve Tools.".format(name)
    )


class StillRecord(object):
    __slots__ = ("path", "aliases")

    def __init__(self, path, aliases):
        self.path = os.path.abspath(path)
        self.aliases = tuple(aliases)


class SequenceRecord(object):
    __slots__ = (
        "original_path", "required_frames", "frame_count", "frame_pattern",
        "media_path", "media_name",
    )

    def __init__(self, original_path, required_frames, frame_count,
                 frame_pattern, media_path, media_name):
        self.original_path = os.path.abspath(original_path)
        self.required_frames = int(required_frames)
        self.frame_count = int(frame_count)
        self.frame_pattern = os.path.abspath(frame_pattern)
        self.media_path = os.path.abspath(media_path)
        self.media_name = str(media_name)


class ClipMatch(object):
    __slots__ = (
        "item", "track_index", "start", "end", "uid", "display_name",
        "source_names", "source_keys", "still",
    )

    def __init__(self, item, track_index, start, end, uid, display_name,
                 source_names, source_keys, still):
        self.item = item
        self.track_index = int(track_index)
        self.start = int(start)
        self.end = int(end)
        self.uid = str(uid or "")
        self.display_name = str(display_name or "Unnamed clip")
        self.source_names = tuple(source_names)
        self.source_keys = tuple(source_keys)
        self.still = still


class ReviewPoint(object):
    __slots__ = (
        "frame_id", "filename", "source_timecode", "exact", "reason",
        "still_path", "match_uid", "mapped",
    )

    def __init__(self, frame_id, filename, source_timecode="", exact=False,
                 reason="", still_path="", match_uid="", mapped=True):
        self.frame_id = int(frame_id)
        self.filename = str(filename or "Reference still")
        self.source_timecode = str(source_timecode or "")
        self.exact = bool(exact)
        self.reason = str(reason or "")
        self.still_path = str(still_path or "")
        self.match_uid = str(match_uid or "")
        self.mapped = bool(mapped)


class BuildResult(object):
    __slots__ = (
        "track_indices", "track_count", "overlay_count", "segment_count",
        "imported_count", "replaced_track_count", "references_enabled",
        "cleanup_warning", "review_point_count", "exact_review_point_count",
        "review_warning", "review_points",
    )

    def __init__(self, track_indices, overlay_count, imported_count,
                 replaced_track_count, references_enabled,
                 cleanup_warning="", segment_count=None):
        self.track_indices = tuple(track_indices)
        self.track_count = len(self.track_indices)
        self.overlay_count = int(overlay_count)
        self.segment_count = int(
            overlay_count if segment_count is None else segment_count
        )
        self.imported_count = int(imported_count)
        self.replaced_track_count = int(replaced_track_count)
        self.references_enabled = bool(references_enabled)
        self.cleanup_warning = str(cleanup_warning or "")
        self.review_point_count = 0
        self.exact_review_point_count = 0
        self.review_warning = ""
        self.review_points = ()


def _normalized_key(value):
    value = unicodedata.normalize("NFC", str(value or "")).strip()
    return value.casefold()


def _unique(items):
    result = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _canonical_path(path):
    # Filename match keys are intentionally case-insensitive, but physical
    # path identity must respect the host filesystem. In particular, two
    # case-distinct folders can contain different JPEGs on Linux or a
    # case-sensitive APFS volume. normcase folds on Windows and preserves case
    # on POSIX, while realpath also prevents a symlink spelling from looking
    # like a second file.
    resolved = os.path.realpath(os.path.abspath(str(path)))
    return os.path.normcase(unicodedata.normalize("NFC", resolved))


def _property_key(value):
    return re.sub(r"[^a-z0-9]", "", _normalized_key(value))


def _safe_call(obj, method_name, default=None, *args):
    try:
        method = getattr(obj, method_name)
        return method(*args)
    except Exception:
        return default


def _timeline_item_frame(item, method_name, default=0):
    """Read an integer timeline frame across strict and permissive API builds."""
    try:
        return int(getattr(item, method_name)(False))
    except TypeError:
        try:
            return int(getattr(item, method_name)())
        except Exception:
            return int(default)
    except Exception:
        return int(default)


def _same_resolve_object(left, right):
    if not left or not right:
        return False
    left_uid = str(_safe_call(left, "GetUniqueId", "") or "")
    right_uid = str(_safe_call(right, "GetUniqueId", "") or "")
    if left_uid and right_uid:
        return left_uid == right_uid
    return left is right


def _float_value(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int_value(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _bool_value(value):
    if isinstance(value, str):
        return value.strip().casefold() in (
            "1", "true", "yes", "on", "df", "drop", "drop frame",
        )
    return bool(value)


def exact_frame_rate(rate_value):
    """Normalize Resolve's rounded NTSC strings before long-TC arithmetic."""
    rate = _float_value(rate_value, 0.0)
    known_rates = (
        (23.976, 24000.0 / 1001.0),
        (29.97, 30000.0 / 1001.0),
        (47.952, 48000.0 / 1001.0),
        (59.94, 60000.0 / 1001.0),
        (119.88, 120000.0 / 1001.0),
    )
    for displayed, exact in known_rates:
        if abs(rate - displayed) < 0.01:
            return exact
    return rate


def parse_frame_rate(rate_value, start_timecode="", drop_value=None):
    actual_rate = exact_frame_rate(rate_value)
    if actual_rate <= 0:
        raise StillOverlayError("The clip or timeline frame rate is unavailable")
    nominal_rate = int(round(actual_rate))
    drop_frame = (
        _bool_value(drop_value) if drop_value is not None else False
    ) or ";" in str(start_timecode or "")
    if drop_frame and nominal_rate not in (30, 60, 120):
        raise StillOverlayError(
            "Drop-frame timecode is unsupported at {} fps".format(rate_value)
        )
    return actual_rate, nominal_rate, drop_frame


def timecode_to_frames(timecode, nominal_rate, drop_frame=False):
    match = re.match(
        r"^\s*(\d{1,2})\s*[:;.]\s*(\d{2})\s*[:;.]\s*"
        r"(\d{2})\s*[:;.]\s*(\d{2,3})\s*$",
        str(timecode or ""),
    )
    if not match:
        raise StillOverlayError("Invalid timecode '{}'".format(timecode))
    hours, minutes, seconds, frames = [int(value) for value in match.groups()]
    if (
        hours > 23 or minutes > 59 or seconds > 59
        or frames >= int(nominal_rate)
    ):
        raise StillOverlayError("Invalid timecode '{}'".format(timecode))
    frame_number = (
        (hours * 3600 + minutes * 60 + seconds) * int(nominal_rate)
        + frames
    )
    if drop_frame:
        drop_frames = int(round(int(nominal_rate) * 0.0666666667))
        if minutes % 10 and seconds == 0 and frames < drop_frames:
            raise StillOverlayError(
                "Invalid dropped timecode label '{}'".format(timecode)
            )
        total_minutes = hours * 60 + minutes
        frame_number -= drop_frames * (total_minutes - total_minutes // 10)
    return frame_number


def frames_to_timecode(frame_number, nominal_rate, drop_frame=False):
    frame_number = int(frame_number)
    nominal_rate = int(nominal_rate)
    separator = ";" if drop_frame else ":"
    if drop_frame:
        drop_frames = int(round(nominal_rate * 0.0666666667))
        frames_per_minute = nominal_rate * 60 - drop_frames
        frames_per_10_minutes = nominal_rate * 600 - drop_frames * 9
        frames_per_hour = nominal_rate * 3600 - drop_frames * 54
        frames_per_24_hours = frames_per_hour * 24
        frame_number %= frames_per_24_hours
        ten_minute_blocks = frame_number // frames_per_10_minutes
        remainder = frame_number % frames_per_10_minutes
        frame_number += drop_frames * 9 * ten_minute_blocks
        if remainder > drop_frames:
            frame_number += drop_frames * (
                (remainder - drop_frames) // frames_per_minute
            )
    frame_number %= nominal_rate * 3600 * 24
    hours = frame_number // (nominal_rate * 3600)
    frame_number %= nominal_rate * 3600
    minutes = frame_number // (nominal_rate * 60)
    frame_number %= nominal_rate * 60
    seconds = frame_number // nominal_rate
    frames = frame_number % nominal_rate
    return "{:02d}:{:02d}:{:02d}{}{:02d}".format(
        hours, minutes, seconds, separator, frames
    )


def _frames_per_24_hours(nominal_rate, drop_frame=False):
    nominal_rate = int(nominal_rate)
    if not drop_frame:
        return nominal_rate * 3600 * 24
    drop_frames = int(round(nominal_rate * 0.0666666667))
    return nominal_rate * 3600 * 24 - drop_frames * 54 * 24


def _wrapped_frame_delta(target_frame, start_frame, frames_per_day):
    return (int(target_frame) - int(start_frame)) % int(frames_per_day)


def _ocr_observation_text(observation):
    if isinstance(observation, dict):
        return str(observation.get("text", "") or "")
    return str(observation or "")


def _ocr_observation_bbox(observation):
    if not isinstance(observation, dict):
        return None
    values = observation.get("bbox")
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    try:
        box = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if box[2] <= 0.0 or box[3] <= 0.0:
        return None
    return box


def _timecode_variants(texts, nominal_rate, drop_frame=False):
    """Extract whole-image TC candidates, including a safely inferred hour tens."""
    candidates = []
    seen = set()
    scan_texts = [
        _ocr_observation_text(observation)
        for observation in (texts or [])
    ]
    # Vision occasionally splits a timecode and its frame pair into adjacent
    # observations. Scan the combined full-image text as well as every line.
    if scan_texts:
        scan_texts.append(" ".join(scan_texts))
    for text in scan_texts:
        for match in TIMECODE_PATTERN.finditer(str(text or "")):
            hour_text, first_separator, minute_text, second_text, last_separator, frame_text = (
                match.groups()
            )
            minute = int(minute_text)
            second = int(second_text)
            frame = int(frame_text)
            if minute > 59 or second > 59 or frame >= int(nominal_rate):
                continue
            base_hour = int(hour_text)
            hours = [base_hour]
            # Vision can omit the first digit in a dark two-digit hour. Only
            # add alternatives when the recognized hour itself has one digit;
            # source-range validation below must prove the correct one.
            if len(hour_text) == 1:
                hours.extend([base_hour + 10, base_hour + 20])
            for hour in hours:
                if hour > 23:
                    continue
                separator = ";" if (
                    drop_frame or ";" in (first_separator, last_separator)
                ) else ":"
                value = "{:02d}:{:02d}:{:02d}{}{:02d}".format(
                    hour, minute, second, separator, frame
                )
                try:
                    frame_number = timecode_to_frames(
                        value, nominal_rate, drop_frame
                    )
                except StillOverlayError:
                    continue
                key = (frame_number, value)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((frame_number, value))
    return candidates


def recognize_reference_text(paths):
    """Read text anywhere in each still using macOS Vision, without a crop."""
    unique_paths = _unique(
        os.path.abspath(str(path)) for path in paths if str(path or "").strip()
    )
    result = {}
    warnings = []
    if not unique_paths:
        return result, warnings
    if sys.platform != "darwin" or not os.path.isfile("/usr/bin/osascript"):
        return result, [
            "Full-frame reference timecode recognition is available on macOS only"
        ]
    for offset in range(0, len(unique_paths), VISION_OCR_BATCH_SIZE):
        batch = unique_paths[offset:offset + VISION_OCR_BATCH_SIZE]
        command = [
            "/usr/bin/osascript", "-l", "JavaScript", "-e", VISION_TEXT_JXA,
        ] + batch
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            warnings.append(
                "Vision could not inspect {} reference still(s): {}".format(
                    len(batch),
                    (stderr or stdout or "unknown OCR error").strip(),
                )
            )
            continue
        try:
            records = json.loads(stdout or "[]")
        except (TypeError, ValueError) as error:
            warnings.append("Vision returned invalid OCR data: {}".format(error))
            continue
        for record in records:
            path = os.path.abspath(str(record.get("path", "") or ""))
            if not path:
                continue
            observations = record.get("observations")
            if isinstance(observations, list):
                result[_canonical_path(path)] = observations
            else:
                # Compatibility with older cached/test OCR payloads.
                result[_canonical_path(path)] = list(
                    record.get("texts", []) or []
                )
            if record.get("error"):
                warnings.append(
                    "{}: {}".format(os.path.basename(path), record["error"])
                )
    return result, warnings


def _assert_current_context(resolve_app, project, timeline):
    """Prevent MediaPool.AppendToTimeline from targeting another timeline."""
    marker = object()
    project_manager = _safe_call(resolve_app, "GetProjectManager", marker)
    if project_manager is marker:
        return True  # Allows isolated unit fakes without a full Resolve graph.
    current_project = _safe_call(project_manager, "GetCurrentProject")
    if not _same_resolve_object(current_project, project):
        raise StillOverlayError(
            "The current Resolve project changed during the operation; rebuild cancelled"
        )
    current_timeline = _safe_call(project, "GetCurrentTimeline")
    if not _same_resolve_object(current_timeline, timeline):
        raise StillOverlayError(
            "The current Resolve timeline changed during the operation; rebuild cancelled"
        )
    return True


def _source_strings(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_source_strings(item))
        return _unique(values)
    if isinstance(value, dict):
        return []
    text = str(value).strip()
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"[;|\r\n]+", text) if part.strip()]
    return _unique(parts or [text])


def _append_source_names(names, value):
    names.extend(_source_strings(value))


def still_aliases_for_path(path):
    filename = os.path.basename(path)
    stem, _ = os.path.splitext(filename)
    return tuple(alias for alias in (_normalized_key(stem),) if alias)


def source_keys_for_name(name):
    """Return filename match keys in most-specific-to-fallback order."""
    if not name:
        return []
    source_path = str(name).strip().strip('"').replace("\\", "/")
    filename = source_path.rsplit("/", 1)[-1].strip()
    if not filename:
        return []
    stem, extension = os.path.splitext(filename)
    keys = [_normalized_key(filename)]
    if extension:
        keys.append(_normalized_key(stem))

    sequence_stem = re.sub(r"[_ .-]?\[\d+\s*-\s*\d+\]$", "", stem)
    if sequence_stem != stem:
        keys.append(_normalized_key(sequence_stem + extension))
        keys.append(_normalized_key(sequence_stem))

    if extension.casefold() == ".r3d":
        red_clip_stem = re.sub(r"_\d{3,4}$", "", stem)
        if red_clip_stem != stem:
            keys.append(_normalized_key(red_clip_stem + extension))
            keys.append(_normalized_key(red_clip_stem))

    if "/" in source_path:
        parent_name = source_path.rsplit("/", 1)[0].rsplit("/", 1)[-1].strip()
        parent_stem, parent_extension = os.path.splitext(parent_name)
        if parent_extension.casefold() == ".rdc":
            keys.append(_normalized_key(parent_stem))
    return [key for key in _unique(keys) if key]


def find_still_paths(folder, recursive=True):
    folder = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        raise StillOverlayError("Reference still folder does not exist: {}".format(folder))
    paths = []
    if recursive:
        for root, directories, filenames in os.walk(folder, followlinks=False):
            directories[:] = [name for name in directories if not name.startswith(".")]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                if os.path.splitext(filename)[1].casefold() in STILL_EXTENSIONS:
                    paths.append(os.path.join(root, filename))
    else:
        for filename in os.listdir(folder):
            path = os.path.join(folder, filename)
            if (
                not filename.startswith(".")
                and os.path.isfile(path)
                and os.path.splitext(filename)[1].casefold() in STILL_EXTENSIONS
            ):
                paths.append(path)
    return sorted(paths, key=lambda value: _normalized_key(value))


def build_still_library(folder, recursive=True):
    records = []
    index = {}
    for path in find_still_paths(folder, recursive=recursive):
        record = StillRecord(path, still_aliases_for_path(path))
        if not record.aliases:
            continue
        records.append(record)
        for alias in record.aliases:
            index.setdefault(alias, []).append(record)
    return records, index


def match_still(source_keys, index):
    for key in source_keys:
        candidates_by_path = {}
        for candidate in index.get(key, []):
            candidates_by_path[_canonical_path(candidate.path)] = candidate
        candidates = list(candidates_by_path.values())
        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1:
            return None, "Ambiguous JPEG filename '{}': {}".format(
                key,
                ", ".join(sorted(candidate.path for candidate in candidates)),
            )
    return None, ""


def _media_file_names(media_pool_item):
    names = []
    if not media_pool_item:
        return names
    properties = _safe_call(media_pool_item, "GetClipProperty", {}) or {}
    normalized_properties = {}
    if isinstance(properties, dict):
        normalized_properties = {
            _property_key(key): value for key, value in properties.items()
        }
    for property_name in FILE_PROPERTY_NAMES:
        _append_source_names(
            names, normalized_properties.get(_property_key(property_name))
        )
    for property_name in DIRECT_FILE_PROPERTY_NAMES:
        _append_source_names(
            names,
            _safe_call(media_pool_item, "GetClipProperty", None, property_name),
        )
    return _unique(names)


def _media_physical_paths(media_pool_item):
    paths = set()
    for value in _media_file_names(media_pool_item):
        text = str(value or "").strip().strip('"')
        if not text:
            continue
        if not (
            os.path.isabs(text)
            or "/" in text
            or "\\" in text
            or re.match(r"^[A-Za-z]:", text)
        ):
            continue
        paths.add(_canonical_path(text))
    return paths


def get_clip_source_names(item):
    file_names = []
    fallback_names = []
    media_pool_item = _safe_call(item, "GetMediaPoolItem")
    if media_pool_item:
        file_names.extend(_media_file_names(media_pool_item))
        if file_names:
            return _unique(file_names)

        properties = _safe_call(media_pool_item, "GetClipProperty", {}) or {}
        normalized_properties = {}
        if isinstance(properties, dict):
            normalized_properties = {
                _property_key(key): value for key, value in properties.items()
            }
        for property_name in FALLBACK_NAME_PROPERTY_NAMES:
            _append_source_names(
                fallback_names,
                normalized_properties.get(_property_key(property_name)),
            )
        media_name = _safe_call(media_pool_item, "GetName", "")
        if media_name:
            _append_source_names(fallback_names, media_name)
    item_name = _safe_call(item, "GetName", "")
    if item_name:
        _append_source_names(fallback_names, item_name)
    return _unique(fallback_names)


def clip_source_keys(source_names):
    keys = []
    for name in source_names:
        keys.extend(source_keys_for_name(name))
    return _unique(keys)


def _timeline_structure_signature(timeline):
    signature = []
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        track_name = str(_safe_call(
            timeline, "GetTrackName", "", "video", track_index
        ) or "")
        if _is_reserved_track_name(track_name):
            continue
        enabled = bool(_safe_call(
            timeline, "GetIsTrackEnabled", True, "video", track_index
        ))
        item_signatures = []
        for item in timeline.GetItemListInTrack("video", track_index) or []:
            item_signatures.append((
                str(_safe_call(item, "GetUniqueId", "") or ""),
                str(_safe_call(item, "GetName", "") or ""),
                _timeline_item_frame(item, "GetStart", -1),
                _timeline_item_frame(item, "GetEnd", -1),
                tuple(_normalized_key(name) for name in get_clip_source_names(item)),
            ))
        signature.append((
            track_index, track_name, enabled, tuple(item_signatures)
        ))
    return tuple(signature)


def _source_content_signature(timeline):
    """Describe source-track content independently of inserted ref indices."""
    return tuple(
        (enabled, item_signatures)
        for _, _, enabled, item_signatures
        in _timeline_structure_signature(timeline)
    )


def _is_reserved_track_name(name):
    return str(name or "").startswith(RESERVED_TRACK_PREFIX)


def audit_timeline(timeline, still_index, managed_track_prefix=TRACK_PREFIX):
    audit = {
        "matches": [],
        "unmatched": [],
        "ambiguous": [],
        "no_source": [],
        "invalid_timing": [],
        "skipped_managed_tracks": [],
        "skipped_disabled_tracks": [],
        "timeline_signature": (),
        "source_content_signature": (),
        "total_video_items": 0,
    }
    audit["timeline_signature"] = _timeline_structure_signature(timeline)
    audit["source_content_signature"] = tuple(
        (enabled, item_signatures)
        for _, _, enabled, item_signatures in audit["timeline_signature"]
    )
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        track_name = str(_safe_call(
            timeline, "GetTrackName", "", "video", track_index
        ) or "")
        if (
            track_name.startswith(str(managed_track_prefix or TRACK_PREFIX))
            or _is_reserved_track_name(track_name)
        ):
            audit["skipped_managed_tracks"].append(
                "V{}: {}".format(track_index, track_name or "managed reference track")
            )
            continue
        if _safe_call(
            timeline, "GetIsTrackEnabled", True, "video", track_index
        ) is False:
            audit["skipped_disabled_tracks"].append(
                "V{}: {}".format(track_index, track_name or "disabled video track")
            )
            continue
        items = timeline.GetItemListInTrack("video", track_index) or []
        for item in items:
            audit["total_video_items"] += 1
            display_name = str(
                _safe_call(item, "GetName", "Unnamed clip") or "Unnamed clip"
            )
            source_names = get_clip_source_names(item)
            keys = clip_source_keys(source_names)
            if not keys:
                audit["no_source"].append("V{}: {}".format(track_index, display_name))
                continue
            still, match_error = match_still(keys, still_index)
            if match_error:
                audit["ambiguous"].append(
                    "V{}: {} - {}".format(track_index, display_name, match_error)
                )
                continue
            if not still:
                audit["unmatched"].append(
                    "V{}: {} (sources: {}; filename keys: {})".format(
                        track_index,
                        display_name,
                        " | ".join(source_names),
                        ", ".join(keys),
                    )
                )
                continue
            start = _timeline_item_frame(item, "GetStart", 0)
            end = _timeline_item_frame(item, "GetEnd", 0)
            if end <= start:
                audit["invalid_timing"].append(
                    "V{}: {} has invalid timeline range {}-{}".format(
                        track_index, display_name, start, end
                    )
                )
                continue
            audit["matches"].append(ClipMatch(
                item=item,
                track_index=track_index,
                start=start,
                end=end,
                uid=str(_safe_call(item, "GetUniqueId", "") or ""),
                display_name=display_name,
                source_names=source_names,
                source_keys=keys,
                still=still,
            ))
    return audit


def _clip_property_map(media_pool_item):
    properties = _safe_call(media_pool_item, "GetClipProperty", {}) or {}
    return properties if isinstance(properties, dict) else {}


def _first_property(properties, names, default=""):
    normalized = {
        _property_key(key): value for key, value in (properties or {}).items()
    }
    for name in names:
        value = normalized.get(_property_key(name))
        if value not in (None, ""):
            return value
    return default


def _match_source_context(match):
    media_pool_item = _safe_call(match.item, "GetMediaPoolItem")
    properties = _clip_property_map(media_pool_item)
    start_timecode = str(_first_property(
        properties, ("Start TC", "Start Timecode", "Start"), ""
    ) or "")
    rate_value = _first_property(
        properties, ("FPS", "Video Frame Rate", "Frame Rate"), 0
    )
    drop_value = _first_property(
        properties, ("Drop frame", "Drop Frame", "Drop-Frame"), None
    )
    actual_rate, nominal_rate, drop_frame = parse_frame_rate(
        rate_value, start_timecode, drop_value
    )
    frames_per_day = _frames_per_24_hours(nominal_rate, drop_frame)

    media_start_frame = None
    if start_timecode:
        try:
            media_start_frame = timecode_to_frames(
                start_timecode, nominal_rate, drop_frame
            )
        except StillOverlayError:
            media_start_frame = None
    media_frame_count = _int_value(_first_property(
        properties, ("Frames", "Frame Count"), 0
    ), 0)

    source_start_time = _safe_call(match.item, "GetSourceStartTime", None)
    source_end_time = _safe_call(match.item, "GetSourceEndTime", None)
    source_start_frame = None
    source_end_frame = None
    if source_start_time is not None and source_end_time is not None:
        try:
            source_start_frame = int(round(float(source_start_time) * actual_rate))
            source_end_frame = int(round(float(source_end_time) * actual_rate))
        except (TypeError, ValueError):
            source_start_frame = None
            source_end_frame = None
    if source_start_frame is None or source_end_frame is None:
        if media_start_frame is not None:
            source_offset = _int_value(
                _safe_call(match.item, "GetSourceStartFrame", 0), 0
            )
            source_start_frame = media_start_frame + source_offset
            source_end_offset = _safe_call(
                match.item, "GetSourceEndFrame", None
            )
            if source_end_offset is not None and _int_value(
                source_end_offset, source_offset
            ) >= source_offset:
                source_end_frame = (
                    media_start_frame
                    + _int_value(source_end_offset, source_offset)
                    + 1
                )
            else:
                source_end_frame = (
                    source_start_frame + max(1, match.end - match.start)
                )

    speed_percentage = None
    speed = _safe_call(match.item, "GetSpeed", None)
    if isinstance(speed, dict) and speed.get("Percentage") is not None:
        speed_percentage = _float_value(speed.get("Percentage"), 0.0)

    return {
        "actual_rate": actual_rate,
        "nominal_rate": nominal_rate,
        "drop_frame": drop_frame,
        "frames_per_day": frames_per_day,
        "media_start_frame": media_start_frame,
        "media_frame_count": media_frame_count,
        "source_start_frame": source_start_frame,
        "source_end_frame": source_end_frame,
        "speed_percentage": speed_percentage,
    }


def _cyclic_interval_outside_distance(delta, interval_length, frames_per_day):
    """Return zero inside [0, length), otherwise nearest cyclic edge distance."""
    frames_per_day = max(1, int(frames_per_day))
    interval_length = max(1, min(int(interval_length), frames_per_day))
    delta = int(delta) % frames_per_day
    if delta < interval_length:
        return 0
    return min(
        frames_per_day - delta,
        delta - (interval_length - 1),
    )


def locate_capture_in_match(match, recognized_texts,
                            allow_visual_tolerance=False):
    """Return (absolute record frame, TC, exact, reason) for one occurrence."""
    try:
        context = _match_source_context(match)
    except StillOverlayError as error:
        return None, "", False, str(error)
    candidates = _timecode_variants(
        recognized_texts,
        context["nominal_rate"],
        context["drop_frame"],
    )
    if not candidates:
        return (
            None, "", False,
            "No readable source timecode was found anywhere in the still",
        )

    frames_per_day = context["frames_per_day"]
    media_start = context["media_start_frame"]
    media_frames = context["media_frame_count"]
    source_start = context["source_start_frame"]
    source_end = context["source_end_frame"]

    visual_tolerance = (
        REVIEW_VISUAL_CORRECTION_FRAMES if allow_visual_tolerance else 0
    )
    valid = []
    for candidate_frame, candidate_timecode in candidates:
        outside_distance = 0
        if media_start is not None and media_frames > 0:
            media_delta = _wrapped_frame_delta(
                candidate_frame, media_start, frames_per_day
            )
            outside_distance = _cyclic_interval_outside_distance(
                media_delta, media_frames, frames_per_day
            )
            if outside_distance > visual_tolerance:
                continue
        elif source_start is not None and source_end is not None:
            source_span = abs(source_end - source_start)
            source_delta = (
                _wrapped_frame_delta(candidate_frame, source_start, frames_per_day)
                if source_end >= source_start
                else _wrapped_frame_delta(source_start, candidate_frame, frames_per_day)
            )
            outside_distance = _cyclic_interval_outside_distance(
                source_delta, source_span, frames_per_day
            )
            if outside_distance > visual_tolerance:
                continue
        valid.append((candidate_frame, candidate_timecode, outside_distance))
    unique_valid = []
    seen_frames = set()
    for candidate in valid:
        if candidate[0] in seen_frames:
            continue
        seen_frames.add(candidate[0])
        unique_valid.append(candidate)
    if unique_valid:
        nearest_distance = min(candidate[2] for candidate in unique_valid)
        unique_valid = [
            candidate for candidate in unique_valid
            if candidate[2] == nearest_distance
        ]
    if not unique_valid:
        return (
            None, "", False,
            "Recognized timecode does not fall inside the matching source clip",
        )
    if len(unique_valid) > 1:
        return (
            None, "", False,
            "More than one recognized timecode falls inside the source clip",
        )
    capture_frame, capture_timecode, media_outside_distance = unique_valid[0]
    if source_start is None or source_end is None or source_end == source_start:
        return (
            None, capture_timecode, False,
            "Resolve did not expose this edit's source-time range",
        )

    source_span = abs(source_end - source_start)
    if source_end >= source_start:
        source_delta = _wrapped_frame_delta(
            capture_frame, source_start, frames_per_day
        )
    else:
        source_delta = _wrapped_frame_delta(
            source_start, capture_frame, frames_per_day
        )
    source_outside_distance = _cyclic_interval_outside_distance(
        source_delta, source_span, frames_per_day
    )
    if source_outside_distance > visual_tolerance:
        return (
            None, capture_timecode, False,
            "The captured source frame is outside this timeline occurrence",
        )
    source_relative_delta = source_delta
    if (
        source_delta >= source_span
        and frames_per_day - source_delta <= visual_tolerance
    ):
        source_relative_delta = source_delta - frames_per_day

    timeline_duration = int(match.end) - int(match.start)
    # GetSpeed was added after Resolve 21.0.4. On that target version, equal
    # source/timeline endpoint spans are the strongest public-API proof of a
    # one-to-one mapping. A hidden variable-speed curve with identical net
    # duration cannot be detected, which the UI/readme call out explicitly.
    normal_speed = (
        abs(source_span - timeline_duration) <= 1
        and (
            context["speed_percentage"] is None
            or abs(context["speed_percentage"] - 100.0) < 0.001
        )
    )
    if normal_speed:
        record_delta = source_relative_delta
        exact = True
        if max(media_outside_distance, source_outside_distance) > 0:
            reason = (
                "Pending visual verification of a nearby metadata/burn-in "
                "boundary offset"
            )
        else:
            reason = ""
    else:
        record_delta = int(round(
            float(source_relative_delta) * float(timeline_duration)
            / float(source_span)
        ))
        exact = False
        reason = "The clip is retimed; the mapped review position is approximate"
    record_delta = min(max(0, record_delta), max(0, timeline_duration - 1))
    return int(match.start) + record_delta, capture_timecode, exact, reason


def _signed_wrapped_frame_delta(target_frame, observed_frame, frames_per_day):
    """Return the shortest signed distance from observed to target."""
    frames_per_day = int(frames_per_day)
    delta = (int(target_frame) - int(observed_frame)) % frames_per_day
    if delta > frames_per_day // 2:
        delta -= frames_per_day
    return delta


def _timecode_observation_boxes(observations, target_timecode,
                                nominal_rate, drop_frame):
    """Return regions whose individual OCR observation contains target TC."""
    target_frame = timecode_to_frames(
        target_timecode, nominal_rate, drop_frame
    )
    boxes = []
    for observation in observations or []:
        box = _ocr_observation_bbox(observation)
        if box is None:
            continue
        candidates = _timecode_variants(
            [_ocr_observation_text(observation)], nominal_rate, drop_frame
        )
        if any(frame == target_frame for frame, _ in candidates):
            boxes.append(box)
    return boxes


def _bbox_regions_match(left, right):
    """Allow normal OCR jitter while rejecting a TC from another overlay."""
    left_center = (left[0] + left[2] / 2.0, left[1] + left[3] / 2.0)
    right_center = (right[0] + right[2] / 2.0, right[1] + right[3] / 2.0)
    width_ratio = left[2] / right[2]
    height_ratio = left[3] / right[3]
    return (
        0.35 <= width_ratio <= 2.85
        and 0.25 <= height_ratio <= 4.0
        and abs(left_center[0] - right_center[0]) <= max(
            0.04, max(left[2], right[2]) * 0.75
        )
        and abs(left_center[1] - right_center[1]) <= max(
            0.04, max(left[3], right[3]) * 2.0
        )
    )


def _nearby_visual_timecode_offset(target_timecode, recognized_texts,
                                   nominal_rate, drop_frame,
                                   maximum_offset=REVIEW_VISUAL_CORRECTION_FRAMES,
                                   reference_boxes=None):
    """Return a unique small source-frame offset proved by an exported frame."""
    target_frame = timecode_to_frames(
        target_timecode, nominal_rate, drop_frame
    )
    frames_per_day = _frames_per_24_hours(nominal_rate, drop_frame)
    all_candidates = _timecode_variants(
        recognized_texts, nominal_rate, drop_frame
    )
    candidates = all_candidates
    if reference_boxes is not None:
        candidates = []
        for observation in recognized_texts or []:
            observed_box = _ocr_observation_bbox(observation)
            if observed_box is None or not any(
                _bbox_regions_match(reference_box, observed_box)
                for reference_box in reference_boxes
            ):
                continue
            candidates.extend(_timecode_variants(
                [_ocr_observation_text(observation)],
                nominal_rate, drop_frame,
            ))
    nearby_offsets = set()
    for candidate_frame, _ in candidates:
        delta = _signed_wrapped_frame_delta(
            target_frame, candidate_frame, frames_per_day
        )
        if abs(delta) <= int(maximum_offset):
            nearby_offsets.add(delta)
    if len(nearby_offsets) == 1:
        return list(nearby_offsets)[0], ""
    if reference_boxes is not None and not reference_boxes:
        return None, (
            "The reference timecode's image region could not be identified"
        )
    if reference_boxes is not None and not candidates and all_candidates:
        return None, (
            "A timecode was visible in the exported frame, but not in the same "
            "image region as the reference burn-in"
        )
    if not all_candidates:
        return None, (
            "The exported timeline frame had no readable timecode burn-in"
        )
    if not nearby_offsets:
        return None, (
            "The exported timeline frame did not show the reference timecode "
            "within {} frame(s) of the metadata-derived position"
            .format(int(maximum_offset))
        )
    return None, (
        "More than one nearby timecode was visible in the exported timeline frame"
    )


def _export_isolated_review_frame(project, timeline, target_track_index,
                                  frame_id, output_path):
    """Export one intended source layer, restoring every track state afterward."""
    original_states = {}
    track_labels = {"video": "V", "subtitle": "S"}
    for track_type in ("video", "subtitle"):
        track_count = _int_value(
            _safe_call(timeline, "GetTrackCount", 0, track_type), 0
        )
        for track_index in range(1, track_count + 1):
            state = _safe_call(
                timeline, "GetIsTrackEnabled", None,
                track_type, track_index
            )
            if state is None:
                raise StillOverlayError(
                    "Resolve did not expose the enabled state of {}{}"
                    .format(track_labels[track_type], track_index)
                )
            original_states[(track_type, track_index)] = bool(state)
    target_track_index = int(target_track_index)
    if not original_states.get(("video", target_track_index), False):
        raise StillOverlayError(
            "The source track for visual frame verification is disabled"
        )

    changed = []
    pending_error = None
    try:
        # Isolate the audited source track so a title or stacked source layer
        # cannot supply an unrelated burn-in to the full-frame OCR pass.
        for (track_type, track_index), original_state in original_states.items():
            desired_state = (
                track_type == "video" and track_index == target_track_index
            )
            if original_state == desired_state:
                continue
            result = _safe_call(
                timeline, "SetTrackEnable", None,
                track_type, track_index, desired_state
            )
            changed.append((track_type, track_index))
            actual_state = _safe_call(
                timeline, "GetIsTrackEnabled", None,
                track_type, track_index
            )
            if result is False or actual_state is None or bool(
                actual_state
            ) != desired_state:
                raise StillOverlayError(
                    "Resolve could not set {}{} while isolating V{} for visual "
                    "frame verification".format(
                        track_labels[track_type], track_index,
                        target_track_index,
                    )
                )
        if changed:
            # Track readback proves the logical state; this short yield also
            # gives Resolve's viewer/render graph time to consume the change.
            time.sleep(0.05)

        target_timecode = review_frame_to_timecode(
            project, timeline, int(frame_id)
        )
        if _safe_call(
            timeline, "SetCurrentTimecode", False, target_timecode
        ) is False:
            raise StillOverlayError(
                "Resolve could not seek to {} for visual frame verification"
                .format(target_timecode)
            )
        actual_timecode = str(
            _safe_call(timeline, "GetCurrentTimecode", "") or ""
        )
        if actual_timecode != target_timecode:
            raise StillOverlayError(
                "Resolve reported playhead {} after seeking to {} for visual "
                "frame verification".format(
                    actual_timecode or "unavailable", target_timecode
                )
            )
        time.sleep(0.05)
        if _safe_call(
            project, "ExportCurrentFrameAsStill", False, output_path
        ) is False:
            raise StillOverlayError(
                "Resolve could not export the timeline frame used for visual "
                "timecode verification"
            )
        for _ in range(20):
            if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
                break
            time.sleep(0.05)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise StillOverlayError(
                "Resolve reported a frame export but did not create the image"
            )
    except Exception as error:
        pending_error = error
    finally:
        restore_failures = []
        for track_type, track_index in reversed(changed):
            desired_state = original_states[(track_type, track_index)]
            result = _safe_call(
                timeline, "SetTrackEnable", None,
                track_type, track_index, desired_state
            )
            actual_state = _safe_call(
                timeline, "GetIsTrackEnabled", None,
                track_type, track_index
            )
            if result is False or actual_state is None or bool(
                actual_state
            ) != desired_state:
                restore_failures.append(
                    "{}{}".format(track_labels[track_type], track_index)
                )
        if restore_failures:
            raise ReviewStateRestoreError(
                "Resolve could not restore video/subtitle-track visibility "
                "after review verification: {}".format(
                    ", ".join(restore_failures)
                )
            )
    if pending_error:
        raise pending_error
    return output_path


def _verification_texts(recognized_by_path, path):
    normalized = {
        _canonical_path(candidate_path): list(texts or [])
        for candidate_path, texts in (recognized_by_path or {}).items()
    }
    return normalized.get(_canonical_path(path), [])


def _downgrade_review_point(point, reason):
    point.exact = False
    point.reason = str(reason or "The metadata-derived frame was not verified")


def _visually_verify_review_points(resolve_app, project, timeline,
                                   match_points, timeline_start_frame,
                                   reference_observations_by_path):
    """Prove metadata positions against full-resolution Resolve frame exports."""
    pending = [
        (match, point) for match, point in match_points
        if point.exact and point.source_timecode
    ]
    if not pending:
        return []
    reference_boxes_by_point = {}
    bbox_pending = []
    for match, point in pending:
        context = _match_source_context(match)
        reference_boxes = _timecode_observation_boxes(
            _verification_texts(
                reference_observations_by_path, match.still.path
            ),
            point.source_timecode,
            context["nominal_rate"], context["drop_frame"],
        )
        if not reference_boxes:
            _downgrade_review_point(
                point,
                "The reference timecode's image region could not be identified "
                "for visual verification",
            )
            continue
        reference_boxes_by_point[id(point)] = reference_boxes
        bbox_pending.append((match, point))
    pending = bbox_pending
    if not pending:
        return []
    try:
        track_locations = _current_match_track_indices(
            timeline, [match for match, _ in pending]
        )
    except StillOverlayError as error:
        reason = "Visual frame verification was unavailable: {}".format(error)
        for _, point in pending:
            _downgrade_review_point(point, reason)
        return [reason]

    original_page = str(
        _safe_call(resolve_app, "GetCurrentPage", "") or ""
    ) if resolve_app is not None else ""
    if original_page and original_page != "edit" and _safe_call(
        resolve_app, "OpenPage", False, "edit"
    ) is False:
        reason = (
            "Visual frame verification was unavailable because Resolve could "
            "not open the Edit page"
        )
        for _, point in pending:
            _downgrade_review_point(point, reason)
        return [reason]

    original_timecode = str(
        _safe_call(timeline, "GetCurrentTimecode", "") or ""
    )
    temp_directory = tempfile.mkdtemp(prefix="resolve-reference-verify-")
    warnings = []
    first_exports = []
    try:
        for index, (match, point) in enumerate(pending):
            output_path = os.path.join(
                temp_directory, "candidate-{:05d}.png".format(index)
            )
            try:
                _export_isolated_review_frame(
                    project, timeline, track_locations[id(match)],
                    point.frame_id, output_path
                )
            except ReviewStateRestoreError:
                raise
            except Exception as error:
                _downgrade_review_point(
                    point,
                    "Visual frame verification was unavailable: {}".format(error),
                )
                continue
            first_exports.append((match, point, output_path))

        first_text, first_warnings = recognize_reference_text(
            [path for _, _, path in first_exports]
        )
        warnings.extend(
            "Visual verification: {}".format(warning)
            for warning in first_warnings
        )
        correction_exports = []
        for correction_index, (match, point, output_path) in enumerate(
            first_exports
        ):
            context = _match_source_context(match)
            offset, reason = _nearby_visual_timecode_offset(
                point.source_timecode,
                _verification_texts(first_text, output_path),
                context["nominal_rate"], context["drop_frame"],
                reference_boxes=reference_boxes_by_point[id(point)],
            )
            if offset is None:
                _downgrade_review_point(point, reason)
                continue
            if offset == 0:
                # The still TC is visibly present on the exported target frame.
                point.reason = ""
                continue

            source_forward = (
                context["source_end_frame"] >= context["source_start_frame"]
            )
            record_adjustment = offset if source_forward else -offset
            corrected_frame_id = int(point.frame_id) + int(record_adjustment)
            corrected_record_frame = (
                int(timeline_start_frame) + corrected_frame_id
            )
            if not (
                int(match.start) <= corrected_record_frame < int(match.end)
            ):
                _downgrade_review_point(
                    point,
                    "A nearby burn-in correction would fall outside this "
                    "timeline occurrence",
                )
                continue
            corrected_path = os.path.join(
                temp_directory,
                "corrected-{:05d}.png".format(correction_index),
            )
            try:
                _export_isolated_review_frame(
                    project, timeline, track_locations[id(match)],
                    corrected_frame_id, corrected_path
                )
            except ReviewStateRestoreError:
                raise
            except Exception as error:
                _downgrade_review_point(
                    point,
                    "The nearby burn-in correction could not be verified: {}"
                    .format(error),
                )
                continue
            correction_exports.append((
                point, corrected_frame_id, record_adjustment,
                context, corrected_path,
                reference_boxes_by_point[id(point)],
            ))

        corrected_text, corrected_warnings = recognize_reference_text(
            [record[4] for record in correction_exports]
        )
        warnings.extend(
            "Visual verification: {}".format(warning)
            for warning in corrected_warnings
        )
        for (
            point, corrected_frame_id, record_adjustment, context, path,
            reference_boxes,
        ) in correction_exports:
            corrected_offset, corrected_reason = _nearby_visual_timecode_offset(
                point.source_timecode,
                _verification_texts(corrected_text, path),
                context["nominal_rate"], context["drop_frame"],
                maximum_offset=0,
                reference_boxes=reference_boxes,
            )
            if corrected_offset != 0:
                _downgrade_review_point(
                    point,
                    "The nearby burn-in correction could not be proved: {}"
                    .format(corrected_reason),
                )
                continue
            point.frame_id = int(corrected_frame_id)
            point.reason = (
                "Visually verified after a {:+d}-frame metadata/burn-in "
                "correction".format(int(record_adjustment))
            )
    finally:
        if original_timecode and _safe_call(
            timeline, "SetCurrentTimecode", False, original_timecode
        ) is False:
            warnings.append(
                "Resolve could not restore the playhead after visual frame "
                "verification"
            )
        if original_page and original_page != str(
            _safe_call(resolve_app, "GetCurrentPage", "") or ""
        ) and _safe_call(resolve_app, "OpenPage", False, original_page) is False:
            warnings.append(
                "Resolve could not restore the original page after visual "
                "frame verification"
            )
        shutil.rmtree(temp_directory, ignore_errors=True)
    return warnings


def build_review_plan(timeline, matches, recognized_by_path=None,
                      project=None, resolve_app=None):
    """Return navigation points plus only the matches with a mapped grab TC."""
    paths = [match.still.path for match in matches]
    warnings = []
    if recognized_by_path is None:
        recognized_by_path, ocr_warnings = recognize_reference_text(paths)
        warnings.extend(ocr_warnings)
    normalized_recognized = {}
    for path, texts in (recognized_by_path or {}).items():
        normalized_recognized[_canonical_path(path)] = list(texts or [])

    timeline_start_frame = _int_value(
        _safe_call(timeline, "GetStartFrame", 0), 0
    )
    points = []
    match_points = []
    mapped_placements = []
    for match in matches:
        texts = normalized_recognized.get(_canonical_path(match.still.path), [])
        record_frame, source_tc, exact, reason = locate_capture_in_match(
            match, texts, allow_visual_tolerance=project is not None
        )
        mapped = record_frame is not None
        if not mapped:
            record_frame = int(match.start) + max(
                0, (int(match.end) - int(match.start) - 1) // 2
            )
        point = ReviewPoint(
            frame_id=record_frame - timeline_start_frame,
            filename=os.path.basename(match.still.path),
            source_timecode=source_tc,
            exact=exact,
            reason=reason,
            still_path=match.still.path,
            match_uid=match.uid,
            mapped=mapped,
        )
        points.append(point)
        match_points.append((match, point))
        if mapped:
            # Keep the object pair rather than reconstructing identity from a
            # UID/path later. Visual verification may adjust point.frame_id by
            # one or two frames before the sparse reference item is placed.
            mapped_placements.append((match, point))
    if project is not None:
        warnings.extend(_visually_verify_review_points(
            resolve_app, project, timeline, match_points, timeline_start_frame,
            normalized_recognized,
        ))
    points.sort(key=lambda point: (
        point.frame_id, _normalized_key(point.filename), point.match_uid
    ))
    approximate = sum(1 for point in points if not point.exact)
    if approximate:
        warnings.append(
            "{} review point(s) are approximate; their yellow markers explain why"
            .format(approximate)
        )
    marker_only = sum(1 for point in points if not point.mapped)
    if marker_only:
        warnings.append(
            "{} reference image(s) had no safely mappable source timecode; "
            "their yellow midpoint markers are navigation aids only and no "
            "image was placed there".format(marker_only)
        )
    return points, warnings, mapped_placements


def build_review_points(timeline, matches, recognized_by_path=None,
                        project=None, resolve_app=None):
    """Compatibility wrapper returning marker/navigation points and warnings."""
    points, warnings, _ = build_review_plan(
        timeline,
        matches,
        recognized_by_path=recognized_by_path,
        project=project,
        resolve_app=resolve_app,
    )
    return points, warnings


def _review_marker_custom_data(point):
    payload = {
        "exact": bool(point.exact),
        "filename": point.filename,
        "frame_id": int(point.frame_id),
        "match_uid": point.match_uid,
        "mapped": bool(point.mapped),
        "reason": point.reason,
        "source_tc": point.source_timecode,
        "still_id": hashlib.sha256(
            _canonical_path(point.still_path).encode("utf-8")
        ).hexdigest()[:16] if point.still_path else "",
    }
    return REVIEW_MARKER_PREFIX + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


def _review_marker_note(point):
    if point.exact:
        status = "Exact captured frame"
    elif point.mapped:
        status = "Approximate mapped review point"
    else:
        status = "Marker only; no reference image was placed"
    lines = [status, "Reference: {}".format(point.filename)]
    if point.source_timecode:
        lines.append("Source TC: {}".format(point.source_timecode))
    if point.reason:
        lines.append(point.reason)
    return "\n".join(lines)


def _is_owned_review_marker(info):
    if not isinstance(info, dict):
        return False
    return str(info.get("customData", "") or "").startswith(
        REVIEW_MARKER_PREFIX
    )


def remove_review_markers(timeline):
    markers = _safe_call(timeline, "GetMarkers", {}) or {}
    owned = [
        (int(round(float(frame_id))), str(info.get("customData", "") or ""))
        for frame_id, info in markers.items()
        if _is_owned_review_marker(info)
    ]
    failures = []
    for frame_id, custom_data in owned:
        if _safe_call(
            timeline, "DeleteMarkerByCustomData", False, custom_data
        ) is False:
            # Delete-by-custom-data has been inconsistent in some Resolve
            # builds. A frame delete is still safe after re-reading and
            # proving that the marker at this exact frame is ours.
            current = (_safe_call(timeline, "GetMarkers", {}) or {}).get(
                frame_id
            )
            if (
                not _is_owned_review_marker(current)
                or str(current.get("customData", "") or "") != custom_data
                or _safe_call(
                    timeline, "DeleteMarkerAtFrame", False, frame_id
                ) is False
            ):
                failures.append(custom_data)
    if failures:
        raise StillOverlayError(
            "Resolve could not remove {} old review marker(s)".format(
                len(failures)
            )
        )
    return len(owned)


def sync_review_markers(timeline, points):
    warnings = []
    try:
        remove_review_markers(timeline)
    except StillOverlayError as error:
        # Do not mix a fresh marker set with stale owned markers. The caller
        # still receives the in-memory review points for the current session.
        return [], [str(error)]
    markers = _safe_call(timeline, "GetMarkers", {}) or {}
    occupied_frames = {
        int(round(float(frame_id))) for frame_id, info in markers.items()
        if not _is_owned_review_marker(info)
    }
    added = []
    for point in points:
        frame_id = int(point.frame_id)
        if frame_id < 0:
            warnings.append(
                "{} falls before the timeline start".format(point.filename)
            )
            continue
        if frame_id in occupied_frames:
            warnings.append(
                "Kept the existing user marker at frame {}; navigation for '{}' "
                "is available only during this review session".format(
                    frame_id, point.filename
                )
            )
            continue
        color = (
            REVIEW_MARKER_COLOR_EXACT
            if point.exact else REVIEW_MARKER_COLOR_APPROXIMATE
        )
        name = REVIEW_MARKER_NAME_PREFIX + os.path.splitext(point.filename)[0]
        custom_data = _review_marker_custom_data(point)
        if _safe_call(
            timeline, "AddMarker", False,
            frame_id, color, name, _review_marker_note(point), 1, custom_data
        ) is False:
            warnings.append(
                "Resolve could not add the review marker for '{}'".format(
                    point.filename
                )
            )
            continue
        occupied_frames.add(frame_id)
        added.append(point)
    return added, warnings


def review_points_from_markers(timeline):
    points = []
    for marker_frame, info in (_safe_call(timeline, "GetMarkers", {}) or {}).items():
        if not _is_owned_review_marker(info):
            continue
        custom_data = str(info.get("customData", "") or "")
        try:
            payload = json.loads(custom_data[len(REVIEW_MARKER_PREFIX):])
        except (TypeError, ValueError):
            continue
        points.append(ReviewPoint(
            frame_id=_int_value(marker_frame, payload.get("frame_id", 0)),
            filename=payload.get("filename", "Reference still"),
            source_timecode=payload.get("source_tc", ""),
            exact=payload.get("exact", False),
            reason=payload.get("reason", ""),
            match_uid=payload.get("match_uid", ""),
            mapped=payload.get("mapped", True),
        ))
    points.sort(key=lambda point: (
        point.frame_id, _normalized_key(point.filename), point.match_uid
    ))
    return points


def choose_adjacent_review_point(points, current_frame_id, direction):
    points = sorted(points, key=lambda point: (
        point.frame_id, _normalized_key(point.filename), point.match_uid
    ))
    if not points:
        return None
    current_frame_id = int(current_frame_id)
    if int(direction) >= 0:
        for point in points:
            if point.frame_id > current_frame_id:
                return point
        return points[0]
    for point in reversed(points):
        if point.frame_id < current_frame_id:
            return point
    return points[-1]


def _timeline_rate(project, timeline):
    rate_value = _safe_call(timeline, "GetSetting", None, "timelineFrameRate")
    if not rate_value:
        rate_value = _safe_call(project, "GetSetting", None, "timelineFrameRate")
    start_timecode = str(_safe_call(timeline, "GetStartTimecode", "") or "")
    drop_value = _safe_call(
        timeline, "GetSetting", None, "timelineDropFrameTimecode"
    )
    return parse_frame_rate(rate_value, start_timecode, drop_value)


def review_frame_to_timecode(project, timeline, frame_id):
    _, nominal_rate, drop_frame = _timeline_rate(project, timeline)
    start_timecode = str(_safe_call(timeline, "GetStartTimecode", "") or "")
    start_frames = timecode_to_frames(
        start_timecode, nominal_rate, drop_frame
    )
    return frames_to_timecode(
        start_frames + int(frame_id), nominal_rate, drop_frame
    )


def _ensure_timecode_navigation_page(resolve_app):
    """Move to Edit when the current Resolve page lacks TC navigation APIs."""
    if resolve_app is None:
        return ""
    current_page = str(_safe_call(resolve_app, "GetCurrentPage", "") or "")
    if current_page in TIMECODE_NAVIGATION_PAGES:
        return current_page
    if _safe_call(resolve_app, "OpenPage", False, "edit") is False:
        raise StillOverlayError(
            "Resolve must open the Edit page to navigate review stops"
        )
    return "edit"


def current_review_frame(project, timeline, resolve_app=None):
    _ensure_timecode_navigation_page(resolve_app)
    _, nominal_rate, drop_frame = _timeline_rate(project, timeline)
    start_timecode = str(_safe_call(timeline, "GetStartTimecode", "") or "")
    current_timecode = str(_safe_call(timeline, "GetCurrentTimecode", "") or "")
    start_frames = timecode_to_frames(
        start_timecode, nominal_rate, drop_frame
    )
    current_frames = timecode_to_frames(
        current_timecode, nominal_rate, drop_frame
    )
    return _wrapped_frame_delta(
        current_frames, start_frames,
        _frames_per_24_hours(nominal_rate, drop_frame),
    )


def navigate_to_review_point(project, timeline, point, resolve_app=None):
    _ensure_timecode_navigation_page(resolve_app)
    target_timecode = review_frame_to_timecode(
        project, timeline, point.frame_id
    )
    if _safe_call(
        timeline, "SetCurrentTimecode", False, target_timecode
    ) is False:
        raise StillOverlayError(
            "Resolve could not move the playhead to {}".format(target_timecode)
        )
    return target_timecode


def _validate_match_snapshot(timeline, matches):
    for match in matches:
        if _safe_call(
            timeline, "GetIsTrackEnabled", True,
            "video", match.track_index
        ) is False:
            raise StillOverlayError(
                "Source V{} was disabled after the audit; run the audit again".format(
                    match.track_index
                )
            )
        current_items = timeline.GetItemListInTrack(
            "video", match.track_index
        ) or []
        current_item = None
        for candidate in current_items:
            if _same_resolve_object(candidate, match.item):
                current_item = candidate
                break
        if not current_item:
            raise StillOverlayError(
                "Timeline item '{}' moved or was removed after the audit".format(
                    match.display_name
                )
            )
        current_start = _timeline_item_frame(current_item, "GetStart", -1)
        current_end = _timeline_item_frame(current_item, "GetEnd", -1)
        if current_start != match.start or current_end != match.end:
            raise StillOverlayError(
                "Timeline item '{}' was trimmed or moved after the audit".format(
                    match.display_name
                )
            )
    return True


def _current_match_track_indices(timeline, matches):
    """Locate audited source items after reference-track indices have shifted."""
    by_uid = {}
    fallback_candidates = []
    enabled_by_track = {}
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        track_name = str(_safe_call(
            timeline, "GetTrackName", "", "video", track_index
        ) or "")
        if _is_reserved_track_name(track_name):
            continue
        enabled_by_track[track_index] = (
            _safe_call(
                timeline, "GetIsTrackEnabled", True,
                "video", track_index
            ) is not False
        )
        for item in timeline.GetItemListInTrack("video", track_index) or []:
            uid = str(_safe_call(item, "GetUniqueId", "") or "")
            if uid:
                by_uid.setdefault(uid, []).append((track_index, item))
            fallback_candidates.append((track_index, item))

    locations = {}
    for match in matches:
        candidates = by_uid.get(match.uid, []) if match.uid else []
        if not candidates:
            candidates = []
            expected_name = _normalized_key(match.display_name)
            expected_keys = tuple(match.source_keys)
            for track_index, item in fallback_candidates:
                if item is match.item:
                    candidates.append((track_index, item))
                    continue
                if _normalized_key(_safe_call(item, "GetName", "")) != expected_name:
                    continue
                if tuple(clip_source_keys(get_clip_source_names(item))) != expected_keys:
                    continue
                if (
                    _timeline_item_frame(item, "GetStart", -1) == match.start
                    and _timeline_item_frame(item, "GetEnd", -1) == match.end
                ):
                    candidates.append((track_index, item))
        if len(candidates) != 1:
            raise StillOverlayError(
                "Could not uniquely relocate timeline item '{}' after building; "
                "the edit may have changed".format(match.display_name)
            )
        track_index, item = candidates[0]
        if not enabled_by_track.get(track_index, False):
            raise StillOverlayError(
                "Source track containing '{}' was disabled while building".format(
                    match.display_name
                )
            )
        if (
            _timeline_item_frame(item, "GetStart", -1) != match.start
            or _timeline_item_frame(item, "GetEnd", -1) != match.end
        ):
            raise StillOverlayError(
                "Timeline item '{}' was trimmed or moved while building".format(
                    match.display_name
                )
            )
        locations[id(match)] = track_index
    return locations


def _validate_final_layout(timeline, audit, by_source_track,
                           final_name_by_source, media_by_path,
                           expected_segments_by_source):
    validate_source_layout = bool(audit.get("timeline_signature"))
    expected_content = audit.get("source_content_signature")
    if expected_content and _source_content_signature(timeline) != expected_content:
        raise StillOverlayError(
            "The source timeline changed while references were building; the "
            "new overlay was not committed"
        )
    current_locations = {}
    if validate_source_layout:
        all_matches = [
            match
            for source_track in by_source_track
            for match in by_source_track[source_track]
        ]
        current_locations = _current_match_track_indices(timeline, all_matches)
    final_tracks = _find_tracks_by_exact_names(
        timeline, final_name_by_source.values()
    )
    if len(final_tracks) != len(final_name_by_source):
        raise StillOverlayError(
            "Could not rediscover every finalized reference track"
        )
    for audited_source_track, matches in by_source_track.items():
        reference_track = final_tracks[final_name_by_source[audited_source_track]]
        if validate_source_layout:
            source_indices = {
                current_locations[id(match)] for match in matches
            }
            if len(source_indices) != 1:
                raise StillOverlayError(
                    "Clips audited on source V{} no longer share one track".format(
                        audited_source_track
                    )
                )
            current_source_track = next(iter(source_indices))
            if reference_track != current_source_track + 1:
                raise StillOverlayError(
                    "Reference lane '{}' is no longer immediately above its "
                    "source track".format(
                        final_name_by_source[audited_source_track]
                    )
                )
        actual_items = list(
            timeline.GetItemListInTrack("video", reference_track) or []
        )
        expected_segments = list(
            expected_segments_by_source.get(audited_source_track, [])
        )
        for match, item_start, item_end in expected_segments:
            if (
                int(item_start) < int(match.start)
                or int(item_end) != int(item_start) + 1
                or int(item_end) > int(match.end)
            ):
                raise StillOverlayError(
                    "Reference item plan is not one frame inside '{}'".format(
                        match.display_name
                    )
                )
        if len(actual_items) != len(expected_segments):
            raise StillOverlayError(
                "Reference lane '{}' contains {} item(s), expected {}".format(
                    final_name_by_source[audited_source_track],
                    len(actual_items),
                    len(expected_segments),
                )
            )
        remaining_items = list(actual_items)
        for match, segment_start, segment_end in expected_segments:
            range_matches = [
                item for item in remaining_items
                if (
                    _timeline_item_frame(item, "GetStart", -1)
                    == int(segment_start)
                    and _timeline_item_frame(item, "GetEnd", -1)
                    == int(segment_end)
                )
            ]
            if len(range_matches) != 1:
                raise StillOverlayError(
                    "Reference lane '{}' is missing the exact [{}-{}) overlay "
                    "for '{}'".format(
                        final_name_by_source[audited_source_track],
                        segment_start,
                        segment_end,
                        match.display_name,
                    )
                )
            actual_item = range_matches[0]
            remaining_items.remove(actual_item)
            expected_name = ITEM_PREFIX + os.path.basename(match.still.path)
            actual_name = str(_safe_call(actual_item, "GetName", "") or "")
            if actual_name != expected_name:
                raise StillOverlayError(
                    "Reference overlay label changed for '{}'".format(
                        match.display_name
                    )
                )
            opacity = _safe_call(actual_item, "GetProperty", None, "Opacity")
            composite_mode = _safe_call(
                actual_item, "GetProperty", None, "CompositeMode"
            )
            try:
                opacity_is_expected = (
                    abs(float(opacity) - float(REFERENCE_OPACITY)) < 0.001
                )
            except (TypeError, ValueError):
                opacity_is_expected = False
            try:
                composite_is_normal = int(composite_mode) == 0
            except (TypeError, ValueError):
                composite_is_normal = False
            if not opacity_is_expected or not composite_is_normal:
                raise StillOverlayError(
                    "Reference overlay properties changed for '{}'".format(
                        match.display_name
                    )
                )
            expected_media_item = media_by_path.get(
                _canonical_path(match.still.path)
            )
            actual_media_item = _safe_call(actual_item, "GetMediaPoolItem")
            if (
                not expected_media_item
                or not _media_item_matches_reference(
                    actual_media_item, expected_media_item, match.still.path
                )
            ):
                raise StillOverlayError(
                    "Reference lane '{}' contains the wrong JPEG for '{}'".format(
                        final_name_by_source[audited_source_track],
                        match.display_name,
                    )
                )
    return sorted(final_tracks.values())


def audit_counts(audit):
    return {
        "matched": len(audit["matches"]),
        "unique_stills": len(set(
            _canonical_path(match.still.path) for match in audit["matches"]
        )),
        "source_tracks": len(set(
            match.track_index for match in audit["matches"]
        )),
        "unmatched": len(audit["unmatched"]),
        "ambiguous": len(audit["ambiguous"]),
        "no_source": len(audit["no_source"]),
        "invalid_timing": len(audit["invalid_timing"]),
    }


def format_audit_report(audit, folder, project_name, timeline_name, resolve_version):
    counts = audit_counts(audit)
    lines = [
        SCRIPT_NAME,
        "Generated: {}".format(datetime.datetime.now().isoformat(timespec="seconds")),
        "Resolve: {}".format(resolve_version or "unknown"),
        "Project: {}".format(project_name),
        "Timeline: {}".format(timeline_name),
        "Reference folder: {}".format(folder),
        "",
        "AUDIT SUMMARY",
        "Video items scanned: {}".format(audit["total_video_items"]),
        "Matched timeline items: {}".format(counts["matched"]),
        "Unique JPEGs used: {}".format(counts["unique_stills"]),
        "Reference tracks required: {}".format(counts["source_tracks"]),
        "Unmatched items: {}".format(counts["unmatched"]),
        "Ambiguous matches: {}".format(counts["ambiguous"]),
        "Items without source filenames: {}".format(counts["no_source"]),
        "Invalid item ranges: {}".format(counts["invalid_timing"]),
        "Existing managed tracks excluded: {}".format(
            len(audit["skipped_managed_tracks"])
        ),
        "Disabled source tracks excluded: {}".format(
            len(audit["skipped_disabled_tracks"])
        ),
        "",
        "MATCHES",
    ]
    for match in audit["matches"]:
        lines.append(
            "V{} {} [{}-{}) -> {}".format(
                match.track_index,
                match.display_name,
                match.start,
                match.end,
                match.still.path,
            )
        )
    for title, key in (
        ("AMBIGUOUS", "ambiguous"),
        ("INVALID TIMING", "invalid_timing"),
        ("UNMATCHED", "unmatched"),
        ("NO SOURCE FILENAME", "no_source"),
        ("EXCLUDED MANAGED TRACKS", "skipped_managed_tracks"),
        ("EXCLUDED DISABLED SOURCE TRACKS", "skipped_disabled_tracks"),
    ):
        lines.extend(["", title])
        lines.extend(audit[key] or ["None"])
    return "\n".join(lines) + "\n"


def build_overlay_clip_info(match, media_pool_item, record_start=None,
                            record_end=None, source_end_is_exclusive=False):
    if record_start is None:
        record_start = int(match.start)
    if record_end is None:
        record_end = int(match.end)
    record_start = int(record_start)
    record_end = int(record_end)
    duration = record_end - record_start
    if duration <= 0:
        raise StillOverlayError(
            "{} has a non-positive duration".format(match.display_name)
        )
    return {
        "mediaPoolItem": media_pool_item,
        "startFrame": 0,
        # Resolve 21 treats endFrame as an exclusive source endpoint for an
        # imported video/image sequence. The legacy Still path below keeps its
        # prior inclusive request shape for isolated fakes and diagnostics.
        "endFrame": duration if source_end_is_exclusive else duration - 1,
        "mediaType": 1,
        "trackIndex": int(match.track_index),
        "recordFrame": record_start,
    }


def _track_name_is_managed(name):
    return bool(TRACK_PATTERN.match(str(name or "")))


def _validate_reference_track_contents(timeline, track_index, track_name,
                                       allow_empty=False):
    items = list(timeline.GetItemListInTrack("video", track_index) or [])
    if not items and not allow_empty:
        return False, "{} is empty and cannot be proven script-owned".format(track_name)
    for item in items:
        item_name = str(_safe_call(item, "GetName", "") or "")
        if not item_name.startswith(ITEM_PREFIX):
            return False, "{} contains an unknown item '{}'".format(
                track_name, item_name or "Unnamed clip"
            )
        media_pool_item = _safe_call(item, "GetMediaPoolItem")
        physical_names = _media_file_names(media_pool_item)
        if not physical_names:
            return False, "{} contains an item without a physical JPEG".format(track_name)
        if not any(
            os.path.splitext(str(name).strip().strip('"'))[1].casefold()
            in REFERENCE_MEDIA_EXTENSIONS
            for name in physical_names
        ):
            return False, "{} contains a non-JPEG item '{}'".format(
                track_name, item_name
            )
    return True, ""


def validate_managed_track(timeline, track_index):
    try:
        track_count = int(timeline.GetTrackCount("video") or 0)
        track_index = int(track_index)
    except Exception:
        return False, "Invalid video track index"
    if track_index < 1 or track_index > track_count:
        return False, "V{} does not exist".format(track_index)
    track_name = str(_safe_call(
        timeline, "GetTrackName", "", "video", track_index
    ) or "")
    if not _track_name_is_managed(track_name):
        return False, "V{} is not an exact managed reference track".format(track_index)
    if not bool(_safe_call(
        timeline, "GetIsTrackLocked", False, "video", track_index
    )):
        return False, "{} is not locked".format(track_name)
    return _validate_reference_track_contents(timeline, track_index, track_name)


def managed_tracks_use_current_spec(timeline, track_indices):
    """Allow intentional opacity/comparison changes on sparse reference items."""
    for track_index in [int(value) for value in track_indices]:
        for item in _safe_call(
            timeline, "GetItemListInTrack", [], "video", track_index
        ) or []:
            start = _timeline_item_frame(item, "GetStart", -1)
            end = _timeline_item_frame(item, "GetEnd", -1)
            opacity = _safe_call(item, "GetProperty", None, "Opacity")
            composite_mode = _safe_call(
                item, "GetProperty", None, "CompositeMode"
            )
            try:
                opacity_is_expected = (
                    math.isfinite(float(opacity)) and 0 <= float(opacity) <= 100
                )
            except (TypeError, ValueError):
                opacity_is_expected = False
            try:
                composite_is_normal = float(composite_mode) in (0.0, 3.0)
            except (TypeError, ValueError):
                composite_is_normal = False
            if end != start + 1 or not opacity_is_expected or not composite_is_normal:
                return False
    return True


def find_managed_track_indices(timeline, strict=True):
    indices = []
    lane_numbers = {}
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        track_name = str(_safe_call(
            timeline, "GetTrackName", "", "video", track_index
        ) or "")
        match = TRACK_PATTERN.match(track_name)
        if not match:
            continue
        valid, reason = validate_managed_track(timeline, track_index)
        if strict and not valid:
            raise StillOverlayError(
                "Refusing to manage V{}: {}".format(track_index, reason)
            )
        if valid or not strict:
            lane_number = int(match.group(1))
            if strict and lane_number in lane_numbers:
                raise StillOverlayError(
                    "Duplicate managed reference tracks claim lane {}".format(
                        lane_number
                    )
                )
            lane_numbers[lane_number] = track_index
            indices.append(track_index)
    return indices


def find_staging_track_indices(timeline, strict=True):
    indices = []
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        track_name = str(_safe_call(
            timeline, "GetTrackName", "", "video", track_index
        ) or "")
        if not STAGING_PATTERN.match(track_name):
            continue
        valid, reason = _validate_reference_track_contents(
            timeline, track_index, track_name, allow_empty=True
        )
        if strict and not valid:
            raise StillOverlayError(
                "Refusing to remove incomplete V{}: {}".format(
                    track_index, reason
                )
            )
        if valid or not strict:
            indices.append(track_index)
    return indices


def find_backup_track_indices(timeline, strict=True):
    indices = []
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        track_name = str(_safe_call(
            timeline, "GetTrackName", "", "video", track_index
        ) or "")
        if not BACKUP_PATTERN.match(track_name):
            continue
        if not bool(_safe_call(
            timeline, "GetIsTrackLocked", False, "video", track_index
        )):
            valid = False
            reason = "{} is not locked".format(track_name)
        else:
            valid, reason = _validate_reference_track_contents(
                timeline, track_index, track_name, allow_empty=False
            )
        if strict and not valid:
            raise StillOverlayError(
                "Refusing to remove backup V{}: {}".format(track_index, reason)
            )
        if valid or not strict:
            indices.append(track_index)
    return indices


def set_reference_visibility(timeline, track_indices, enabled):
    indices = [int(index) for index in track_indices]
    if not indices:
        raise StillOverlayError("No managed reference tracks exist on this timeline")
    original_states = {}
    changed = []
    try:
        for track_index in indices:
            original_states[track_index] = bool(
                timeline.GetIsTrackEnabled("video", track_index)
            )
            # Include this track in rollback even if Resolve reports False
            # after partially applying the requested state.
            changed.append(track_index)
            result = timeline.SetTrackEnable("video", track_index, bool(enabled))
            if result is False:
                raise StillOverlayError(
                    "Resolve could not change reference track V{}".format(track_index)
                )
        for track_index in indices:
            actual = bool(timeline.GetIsTrackEnabled("video", track_index))
            if actual != bool(enabled):
                raise StillOverlayError(
                    "Reference track V{} did not reach the requested state".format(
                        track_index
                    )
                )
    except Exception as original_error:
        rollback_failures = []
        for track_index in reversed(changed):
            desired = original_states[track_index]
            result = _safe_call(
                timeline, "SetTrackEnable", False,
                "video", track_index, desired
            )
            actual = bool(_safe_call(
                timeline, "GetIsTrackEnabled", not desired,
                "video", track_index
            ))
            if result is False or actual != desired:
                rollback_failures.append("V{}".format(track_index))
        if rollback_failures:
            raise StillOverlayError(
                "{}; visibility rollback also failed for {}".format(
                    original_error, ", ".join(rollback_failures)
                )
            )
        raise
    return bool(enabled)


def toggle_reference_tracks(timeline, track_indices):
    indices = [int(index) for index in track_indices]
    if not indices:
        raise StillOverlayError("No managed reference tracks exist on this timeline")
    any_enabled = any(
        bool(timeline.GetIsTrackEnabled("video", track_index))
        for track_index in indices
    )
    return set_reference_visibility(timeline, indices, not any_enabled)


def toggle_all_overlays(timeline, track_indices, enabled=None):
    if isinstance(track_indices, int):
        track_indices = [track_indices]
    if enabled is None:
        return toggle_reference_tracks(timeline, track_indices)
    return set_reference_visibility(timeline, track_indices, enabled)


def _all_media_in_folder(folder):
    return list(_safe_call(folder, "GetClipList", []) or [])


def _find_subfolder(parent, wanted_name):
    for folder in _safe_call(parent, "GetSubFolderList", []) or []:
        if str(_safe_call(folder, "GetName", "") or "") == wanted_name:
            return folder
    return None


def _media_items_by_path(items):
    result = {}
    for item in items:
        for path in _media_file_names(item):
            if os.path.splitext(path)[1].casefold() in STILL_EXTENSIONS:
                result[_canonical_path(path)] = item
    return result


def _media_items_by_unique_basename(items):
    grouped = {}
    for item in items:
        name = str(_safe_call(item, "GetName", "") or "")
        key = _normalized_key(os.path.basename(name))
        if key:
            grouped.setdefault(key, []).append(item)
    return {
        key: values[0] for key, values in grouped.items() if len(values) == 1
    }


def import_reference_media(media_pool, paths):
    """Import/reuse reference files and restore the user's selected bin."""
    unique_paths = []
    seen = set()
    for path in paths:
        canonical = _canonical_path(path)
        if canonical not in seen:
            seen.add(canonical)
            unique_paths.append(os.path.abspath(path))
    original_folder = _safe_call(media_pool, "GetCurrentFolder")
    root_folder = _safe_call(media_pool, "GetRootFolder")
    if not root_folder:
        raise StillOverlayError("Resolve's Media Pool root folder is unavailable")
    target_folder = _find_subfolder(root_folder, MEDIA_POOL_FOLDER)
    if not target_folder:
        target_folder = _safe_call(
            media_pool, "AddSubFolder", None, root_folder, MEDIA_POOL_FOLDER
        )
    if not target_folder:
        raise StillOverlayError(
            "Could not create the '{}' Media Pool bin".format(MEDIA_POOL_FOLDER)
        )

    imported_count = 0
    try:
        if _safe_call(media_pool, "SetCurrentFolder", False, target_folder) is False:
            raise StillOverlayError(
                "Could not select the '{}' Media Pool bin".format(MEDIA_POOL_FOLDER)
            )
        items = _all_media_in_folder(target_folder)
        by_path = _media_items_by_path(items)
        result = {
            _canonical_path(path): by_path[_canonical_path(path)]
            for path in unique_paths if _canonical_path(path) in by_path
        }
        missing = [
            path for path in unique_paths if _canonical_path(path) not in result
        ]

        # Import one file per API call. Apart from making failures attributable
        # to one path, this prevents consecutively numbered Livegrade JPEGs
        # from being silently consolidated into a single image sequence.
        for path in missing:
            imported = _safe_call(media_pool, "ImportMedia", None, [path])
            if imported is None:
                raise StillOverlayError(
                    "Resolve failed while importing {}".format(path)
                )
            imported = list(imported or [])
            imported_count += len(imported)
            by_path = _media_items_by_path(_all_media_in_folder(target_folder))
            item = by_path.get(_canonical_path(path))
            if not item:
                # Some still formats/Resolve builds omit File Path from the
                # returned clip. Only trust a basename fallback among the
                # items returned by this exact one-file import call.
                by_basename = _media_items_by_unique_basename(imported)
                item = by_basename.get(_normalized_key(os.path.basename(path)))
            if item:
                result[_canonical_path(path)] = item

        unresolved = [
            path for path in unique_paths if _canonical_path(path) not in result
        ]
        if unresolved:
            raise StillOverlayError(
                "Resolve did not import {} reference JPEG(s): {}".format(
                    len(unresolved), ", ".join(unresolved)
                )
            )
        return result, imported_count
    finally:
        if original_folder:
            _safe_call(media_pool, "SetCurrentFolder", False, original_folder)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source_file:
        while True:
            chunk = source_file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sequence_safe_stem(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return (stem or "reference")[:48]


def _sequence_frame_path(directory, prefix, frame_index, extension=".jpg"):
    return os.path.join(
        directory,
        "{}.{:0{}d}{}".format(
            prefix, int(frame_index), SEQUENCE_FRAME_DIGITS, extension
        ),
    )


def _prepare_reference_sequence(path, required_frames):
    """Create a persistent byte-identical JPEG sequence for exact trimming."""
    path = os.path.abspath(path)
    extension = os.path.splitext(path)[1].lower()
    if extension not in REFERENCE_MEDIA_EXTENSIONS:
        raise StillOverlayError("Unsupported reference image: {}".format(path))
    required_frames = int(required_frames)
    if required_frames <= 0:
        raise StillOverlayError(
            "Reference sequence duration must be positive for {}".format(path)
        )
    if not os.path.isfile(path):
        raise StillOverlayError(
            "Reference JPEG is no longer available: {}".format(path)
        )

    # A one-file numbered import is still classified as a Still by Resolve and
    # receives the user's Standard Still Duration. Keep at least two identical
    # source frames so every helper is imported as a trim-addressable Video.
    frame_count = max(2, required_frames)
    digest = _sha256_file(path)
    cache_key = "{}-{}".format(digest[:24], frame_count)
    sequence_directory = os.path.join(SEQUENCE_CACHE_DIRECTORY, cache_key)
    try:
        os.makedirs(sequence_directory, exist_ok=True)
    except OSError as error:
        raise StillOverlayError(
            "Could not create the reference sequence cache: {}".format(error)
        )

    prefix = "autoref_{}_{}".format(
        _sequence_safe_stem(path), digest[:12]
    )
    seed_path = _sequence_frame_path(sequence_directory, prefix, 0, extension)
    if os.path.exists(seed_path):
        if not os.path.isfile(seed_path) or _sha256_file(seed_path) != digest:
            raise StillOverlayError(
                "Reference sequence cache is damaged: {}".format(seed_path)
            )
    else:
        temporary_seed = ""
        try:
            descriptor, temporary_seed = tempfile.mkstemp(
                prefix=".{}-".format(prefix),
                suffix=".tmp",
                dir=sequence_directory,
            )
            os.close(descriptor)
            shutil.copy2(path, temporary_seed)
            if (
                _sha256_file(temporary_seed) != digest
                or _sha256_file(path) != digest
            ):
                raise StillOverlayError(
                    "Reference JPEG changed while it was being cached; run "
                    "the build again: {}".format(path)
                )
            os.replace(temporary_seed, seed_path)
        except StillOverlayError:
            raise
        except OSError as error:
            raise StillOverlayError(
                "Could not cache reference JPEG '{}': {}".format(path, error)
            )
        finally:
            if temporary_seed and os.path.exists(temporary_seed):
                try:
                    os.remove(temporary_seed)
                except OSError:
                    pass

    seed_size = os.path.getsize(seed_path)
    for frame_index in range(1, frame_count):
        frame_path = _sequence_frame_path(
            sequence_directory, prefix, frame_index, extension
        )
        if os.path.isfile(frame_path):
            try:
                same_file = os.path.samefile(seed_path, frame_path)
            except OSError:
                same_file = False
            if same_file:
                continue
            if (
                os.path.getsize(frame_path) == seed_size
                and _sha256_file(frame_path) == digest
            ):
                continue
            raise StillOverlayError(
                "Reference sequence cache is damaged: {}".format(frame_path)
            )
        if os.path.lexists(frame_path):
            raise StillOverlayError(
                "Reference sequence cache contains an invalid frame: {}"
                .format(frame_path)
            )
        try:
            os.link(seed_path, frame_path)
        except OSError as error:
            # A concurrent build of the same helper may have won the race
            # between the existence check and link creation. Accept only the
            # exact seed inode/content in that case.
            if os.path.isfile(frame_path):
                try:
                    same_file = os.path.samefile(seed_path, frame_path)
                except OSError:
                    same_file = False
                if same_file or (
                    os.path.getsize(frame_path) == seed_size
                    and _sha256_file(frame_path) == digest
                ):
                    continue
            raise StillOverlayError(
                "Could not hard-link cached reference frame '{}': {}. The "
                "script stopped to avoid duplicating the JPEG payload many "
                "times.".format(
                    frame_path, error
                )
            )

    index_format = "%0{}d".format(SEQUENCE_FRAME_DIGITS)
    frame_pattern = os.path.join(
        sequence_directory, "{}.{}{}".format(prefix, index_format, extension)
    )
    first_index = "{:0{}d}".format(0, SEQUENCE_FRAME_DIGITS)
    last_index = "{:0{}d}".format(frame_count - 1, SEQUENCE_FRAME_DIGITS)
    media_name = "{}.[{}-{}]{}".format(
        prefix, first_index, last_index, extension
    )
    media_path = os.path.join(sequence_directory, media_name)
    return SequenceRecord(
        path,
        required_frames,
        frame_count,
        frame_pattern,
        media_path,
        media_name,
    )


def prepare_reference_sequences(matches, prepared_paths=None):
    required_by_path = {}
    original_by_path = {}
    for match in matches:
        duration = int(match.end) - int(match.start)
        if duration <= 0:
            raise StillOverlayError(
                "{} has a non-positive duration".format(match.display_name)
            )
        key = _canonical_path(match.still.path)
        original_by_path[key] = os.path.abspath(
            (prepared_paths or {}).get(key, match.still.path)
        )
        required_by_path[key] = 1
    # Each reference appears for exactly one timeline frame. Two physical,
    # byte-identical frames are still created by _prepare_reference_sequence
    # because Resolve classifies a one-file numbered import as a Still and
    # expands it to the user's Standard Still Duration.
    return {
        key: _prepare_reference_sequence(
            original_by_path[key], required_by_path[key],
        )
        for key in sorted(required_by_path)
    }


def _sequence_item_frame_count(item):
    properties = _safe_call(item, "GetClipProperty", {}) or {}
    raw_value = properties.get("Frames", "") if isinstance(
        properties, dict
    ) else ""
    try:
        return int(float(str(raw_value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _sequence_item_is_expected(item, record):
    if not item:
        return False
    actual_paths = _media_physical_paths(item)
    expected_path = _canonical_path(record.media_path)
    actual_name = os.path.basename(str(_safe_call(item, "GetName", "") or ""))
    if actual_paths:
        if expected_path not in actual_paths:
            return False
    elif _normalized_key(actual_name) != _normalized_key(record.media_name):
        return False
    properties = _safe_call(item, "GetClipProperty", {}) or {}
    clip_type = str(properties.get("Type", "") or "") if isinstance(
        properties, dict
    ) else ""
    return (
        _normalized_key(clip_type) == "video"
        and _sequence_item_frame_count(item) >= int(record.required_frames)
    )


def import_reference_sequence_media(media_pool, records_by_path):
    """Import/reuse cached image sequences in a private child Media Pool bin."""
    original_folder = _safe_call(media_pool, "GetCurrentFolder")
    root_folder = _safe_call(media_pool, "GetRootFolder")
    if not root_folder:
        raise StillOverlayError("Resolve's Media Pool root folder is unavailable")
    parent_folder = _find_subfolder(root_folder, MEDIA_POOL_FOLDER)
    if not parent_folder:
        parent_folder = _safe_call(
            media_pool, "AddSubFolder", None, root_folder, MEDIA_POOL_FOLDER
        )
    if not parent_folder:
        raise StillOverlayError(
            "Could not create the '{}' Media Pool bin".format(MEDIA_POOL_FOLDER)
        )
    target_folder = _find_subfolder(
        parent_folder, SEQUENCE_MEDIA_POOL_FOLDER
    )
    if not target_folder:
        target_folder = _safe_call(
            media_pool,
            "AddSubFolder",
            None,
            parent_folder,
            SEQUENCE_MEDIA_POOL_FOLDER,
        )
    if not target_folder:
        raise StillOverlayError(
            "Could not create the '{}' Media Pool bin".format(
                SEQUENCE_MEDIA_POOL_FOLDER
            )
        )

    result = {}
    imported_count = 0
    try:
        if _safe_call(media_pool, "SetCurrentFolder", False, target_folder) is False:
            raise StillOverlayError(
                "Could not select the '{}' Media Pool bin".format(
                    SEQUENCE_MEDIA_POOL_FOLDER
                )
            )
        for original_key in sorted(records_by_path):
            record = records_by_path[original_key]
            existing = [
                item for item in _all_media_in_folder(target_folder)
                if _sequence_item_is_expected(item, record)
            ]
            if len(existing) > 1:
                raise StillOverlayError(
                    "The reference sequence bin contains duplicate helpers for "
                    "{}".format(record.original_path)
                )
            item = existing[0] if existing else None
            if not item:
                imported = _safe_call(
                    media_pool,
                    "ImportMedia",
                    None,
                    [{
                        "FilePath": record.frame_pattern,
                        "StartIndex": 0,
                        "EndIndex": int(record.frame_count) - 1,
                    }],
                )
                if imported is None:
                    raise StillOverlayError(
                        "Resolve failed while importing the reference sequence "
                        "for {}".format(record.original_path)
                    )
                imported = list(imported or [])
                imported_count += len(imported)
                candidates = [
                    value for value in imported
                    if _sequence_item_is_expected(value, record)
                ]
                if len(candidates) != 1:
                    candidates = [
                        value for value in _all_media_in_folder(target_folder)
                        if _sequence_item_is_expected(value, record)
                    ]
                if len(candidates) != 1:
                    raise StillOverlayError(
                        "Resolve did not import one trim-addressable reference "
                        "sequence for {}".format(record.original_path)
                    )
                item = candidates[0]
            result[original_key] = item
        return result, imported_count
    finally:
        if original_folder:
            _safe_call(media_pool, "SetCurrentFolder", False, original_folder)


def prepare_timeline_reference_media(media_pool, matches, prepared_paths=None):
    """Import visible JPEGs plus one-frame, trim-addressable helpers."""
    records_by_path = prepare_reference_sequences(matches, prepared_paths)
    _unused_stills, still_imported_count = import_reference_media(
        media_pool, [match.still.path for match in matches]
    )
    sequence_media, sequence_imported_count = import_reference_sequence_media(
        media_pool, records_by_path
    )
    return (
        sequence_media,
        still_imported_count + sequence_imported_count,
        True,
        {
            key: int(record.frame_count)
            for key, record in records_by_path.items()
        },
    )


def reference_canvas_size(project, timeline):
    dimensions = []
    for key in ("timelineResolutionWidth", "timelineResolutionHeight"):
        value = _safe_call(timeline, "GetSetting", "", key)
        if not value:
            value = _safe_call(project, "GetSetting", "", key)
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 0
        if number < 16 or number > 16384:
            raise StillOverlayError("Cannot determine a safe timeline image size ({})".format(key))
        dimensions.append(number)
    return tuple(dimensions)


def prepare_cropped_reference_paths(project, timeline, matches):
    """Preflight all image geometry before changing any timeline tracks.

    OCR always sees the untouched JPEG. Only private, lossless PNG helpers are
    cropped and fit (never stretched) onto the timeline's output canvas.
    """
    engine = _load_helper("reference_image")
    width, height = reference_canvas_size(project, timeline)
    prepared, failures, notes = {}, {}, []
    paths = {_canonical_path(m.still.path): m.still.path for m in matches}
    for key, path in sorted(paths.items()):
        try:
            digest = _sha256_file(path)
            cache_key = "{}-{}-{}x{}".format(CROP_PIPELINE_VERSION, digest[:24], width, height)
            directory = os.path.join(SEQUENCE_CACHE_DIRECTORY, "Prepared", cache_key)
            os.makedirs(directory, exist_ok=True)
            output = os.path.join(directory, _sequence_safe_stem(path) + ".png")
            manifest_path = output + ".json"
            metadata = None
            if os.path.isfile(output) and os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, encoding="utf-8") as stream:
                        cached = json.load(stream)
                    if (cached.get("source_sha256") == digest
                            and cached.get("prepared_sha256") == _sha256_file(output)
                            and cached.get("pipeline") == CROP_PIPELINE_VERSION):
                        metadata = cached
                except (OSError, ValueError):
                    pass
            if metadata is None:
                analysis = engine.analyze_reference(path, width, height)
                if not analysis.get("usable"):
                    raise StillOverlayError(analysis.get("reason", "Ambiguous framelines"))
                metadata = engine.prepare_reference(path, output, width, height, analysis=analysis)
                if _sha256_file(path) != digest:
                    raise StillOverlayError("Source JPEG changed during cropping; rebuild again")
                metadata.update({"source_sha256": digest, "prepared_sha256": _sha256_file(output),
                                 "source_path": path, "pipeline": CROP_PIPELINE_VERSION})
                with open(manifest_path, "w", encoding="utf-8") as stream:
                    json.dump(metadata, stream, indent=2)
            prepared[key] = output
            notes.append("{}: {} crop {} -> {}x{}; {}".format(
                os.path.basename(path), metadata.get("kind"), metadata.get("crop_box"),
                width, height, metadata.get("reason", "")))
        except Exception as error:
            failures[key] = str(error)
            notes.append("SKIPPED {}: {}".format(os.path.basename(path), error))
    return prepared, failures, notes


def _track_identity_name(name):
    text = str(name or "")
    if re.match(r"^Video\s+[1-9][0-9]*$", text, re.IGNORECASE):
        return "<default video track>"
    return text


def _video_track_fingerprints(timeline):
    """Capture enough public state to prove where AddTrack inserted a lane."""
    fingerprints = []
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        item_fingerprints = []
        for item in timeline.GetItemListInTrack("video", track_index) or []:
            uid = str(_safe_call(item, "GetUniqueId", "") or "")
            if uid:
                item_fingerprints.append(("uid", uid))
            else:
                item_fingerprints.append((
                    "fallback",
                    _normalized_key(_safe_call(item, "GetName", "")),
                    _timeline_item_frame(item, "GetStart", -1),
                    _timeline_item_frame(item, "GetEnd", -1),
                    tuple(clip_source_keys(get_clip_source_names(item))),
                ))
        fingerprints.append((
            _track_identity_name(_safe_call(
                timeline, "GetTrackName", "", "video", track_index
            )),
            _safe_call(
                timeline, "GetIsTrackEnabled", True,
                "video", track_index
            ) is not False,
            bool(_safe_call(
                timeline, "GetIsTrackLocked", False,
                "video", track_index
            )),
            tuple(item_fingerprints),
        ))
    return tuple(fingerprints)


def _candidate_inserted_track_indices(before, after):
    if len(after) != len(before) + 1:
        return []
    candidates = []
    for offset in range(len(after)):
        if after[:offset] + after[offset + 1:] == before:
            candidates.append(offset + 1)
    return candidates


def _delete_track_indices(timeline, indices):
    for track_index in sorted(set(int(value) for value in indices), reverse=True):
        before_count = int(timeline.GetTrackCount("video") or 0)
        track_name = str(_safe_call(
            timeline, "GetTrackName", "", "video", track_index
        ) or "")
        if bool(_safe_call(
            timeline, "GetIsTrackLocked", False, "video", track_index
        )):
            _safe_call(
                timeline, "SetTrackLock", False,
                "video", track_index, False
            )
            still_locked = bool(_safe_call(
                timeline, "GetIsTrackLocked", True,
                "video", track_index
            ))
            if still_locked:
                raise StillOverlayError(
                    "Resolve could not unlock managed reference track V{} for "
                    "removal".format(track_index)
                )
        _safe_call(
            timeline, "DeleteTrack", False, "video", track_index
        )
        after_count = int(timeline.GetTrackCount("video") or 0)
        if after_count == before_count - 1:
            continue
        if after_count != before_count:
            raise StillOverlayError(
                "Resolve changed an unexpected number of tracks while removing "
                "managed reference V{} ('{}')".format(
                    track_index, track_name
                )
            )
        # The failed target still occupies this exact index. Contain every
        # script-owned survivor as locked even when an interrupted staging
        # track began unlocked, and verify the remote call.
        lock_result = _safe_call(
            timeline, "SetTrackLock", False,
            "video", track_index, True
        )
        locked = bool(_safe_call(
            timeline, "GetIsTrackLocked", False,
            "video", track_index
        ))
        detail = ""
        if lock_result is False or not locked:
            detail = "; Resolve also could not re-lock the surviving track"
        raise StillOverlayError(
            "Resolve could not delete managed reference track V{} ('{}'){}".format(
                track_index, track_name, detail
            )
        )


def _find_tracks_by_exact_names(timeline, names):
    wanted = set(names)
    result = {}
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        name = str(_safe_call(
            timeline, "GetTrackName", "", "video", track_index
        ) or "")
        if name in wanted:
            result[name] = track_index
    return result


def _media_item_matches_reference(actual_media_item, expected_media_item,
                                  expected_path):
    if not actual_media_item or not expected_media_item:
        return False
    if _same_resolve_object(actual_media_item, expected_media_item):
        return True
    actual_paths = _media_physical_paths(actual_media_item)
    expected_paths = _media_physical_paths(expected_media_item)
    if actual_paths and expected_paths:
        return bool(actual_paths.intersection(expected_paths))
    # Legacy still providers can expose no path on the expected wrapper. The
    # original JPEG remains a safe fallback only in that pathless case.
    return (
        not expected_paths
        and _canonical_path(expected_path) in actual_paths
    )


def _verify_appended_item(item, match, track_index, expected_media_item,
                          expected_start=None, requested_end=None):
    if expected_start is None:
        expected_start = int(match.start)
    if requested_end is None:
        requested_end = int(match.end)
    expected_start = int(expected_start)
    requested_end = int(requested_end)
    actual_start = _timeline_item_frame(item, "GetStart", -1)
    actual_end = _timeline_item_frame(item, "GetEnd", -1)
    if (
        actual_start != expected_start
        or actual_end <= expected_start
        or actual_end > requested_end
    ):
        raise StillOverlayError(
            "Resolve placed '{}' at [{}-{}), outside the usable requested "
            "range [{}-{}). Resolve returned an unusable reference "
            "duration.".format(
                os.path.basename(match.still.path),
                actual_start,
                actual_end,
                expected_start,
                requested_end,
            )
        )
    track_info = _safe_call(item, "GetTrackTypeAndIndex")
    if isinstance(track_info, (list, tuple)) and len(track_info) >= 2:
        if str(track_info[0]) != "video" or int(track_info[1]) != int(track_index):
            raise StillOverlayError(
                "Resolve placed '{}' on the wrong track".format(
                    os.path.basename(match.still.path)
                )
            )
    actual_media_item = _safe_call(item, "GetMediaPoolItem")
    if not actual_media_item:
        raise StillOverlayError(
            "Resolve returned a reference item without Media Pool identity"
        )
    if not _media_item_matches_reference(
        actual_media_item, expected_media_item, match.still.path
    ):
        raise StillOverlayError(
            "Resolve placed the wrong JPEG for '{}'; expected {}".format(
                match.display_name, match.still.path
            )
        )
    return actual_end


def _set_item_property_verified(item, property_name, value):
    current = _safe_call(item, "GetProperty", None, property_name)
    try:
        already_set = float(current) == float(value)
    except (TypeError, ValueError):
        already_set = current == value
    if not already_set:
        if _safe_call(item, "SetProperty", False, property_name, value) is False:
            raise StillOverlayError(
                "Resolve could not set reference {}".format(property_name)
            )
    actual = _safe_call(item, "GetProperty", None, property_name)
    try:
        verified = float(actual) == float(value)
    except (TypeError, ValueError):
        verified = actual == value
    if not verified:
        raise StillOverlayError(
            "Resolve did not retain reference {}={}".format(property_name, value)
        )


def _staging_track_name(run_tag, lane_number):
    return "{}{}] Lane {}".format(
        STAGING_PREFIX, run_tag, int(lane_number)
    )


def _backup_track_name(run_tag, lane_number):
    return "{}{}] Lane {}".format(
        BACKUP_PREFIX, run_tag, int(lane_number)
    )


def _validated_review_placements(timeline, matches, review_placements):
    """Validate sparse point/match pairs before any timeline mutation."""
    allowed_match_ids = {id(match) for match in matches}
    timeline_start_frame = _int_value(
        _safe_call(timeline, "GetStartFrame", 0), 0
    )
    result = []
    used_match_ids = set()
    occupied = set()
    for value in list(review_placements or []):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise StillOverlayError("The review placement plan is invalid")
        match, point = value
        if id(match) not in allowed_match_ids:
            raise StillOverlayError(
                "A review placement does not belong to the current audit"
            )
        if id(match) in used_match_ids:
            raise StillOverlayError(
                "More than one reference placement was planned for '{}'".format(
                    match.display_name
                )
            )
        if not isinstance(point, ReviewPoint) or not point.mapped:
            raise StillOverlayError(
                "An unmapped review point was included in the overlay plan"
            )
        record_frame = timeline_start_frame + int(point.frame_id)
        if not int(match.start) <= record_frame < int(match.end):
            raise StillOverlayError(
                "The mapped grab for '{}' falls outside its timeline occurrence"
                .format(match.display_name)
            )
        position = (int(match.track_index), record_frame)
        if position in occupied:
            raise StillOverlayError(
                "Two reference grabs overlap at frame {} on source V{}".format(
                    record_frame, match.track_index
                )
            )
        used_match_ids.add(id(match))
        occupied.add(position)
        result.append((match, point, record_frame))
    if not result:
        raise StillOverlayError(
            "No reference JPEG had a readable source timecode that could be "
            "mapped into the current timeline"
        )
    result.sort(key=lambda value: (
        value[0].track_index, value[2], value[0].display_name
    ))
    return result


def build_overlays(resolve_app, project, timeline, audit,
                   review_placements=None,
                   reference_media_provider=None):
    audited_matches = list(audit.get("matches", []))
    if not audited_matches:
        raise StillOverlayError("No timeline source filenames matched the JPEG filenames")
    placements = _validated_review_placements(
        timeline, audited_matches, review_placements
    )
    matches = [match for match, _, _ in placements]
    _assert_current_context(resolve_app, project, timeline)
    enforce_snapshot = bool(audit.get("timeline_signature"))
    if enforce_snapshot:
        if _timeline_structure_signature(timeline) != audit["timeline_signature"]:
            raise StillOverlayError(
                "The timeline changed after the audit; run the script again so "
                "reference positions and enabled tracks can be re-audited"
            )
        _validate_match_snapshot(timeline, audited_matches)
    stale_staging = find_staging_track_indices(timeline, strict=True)
    # Keep interrupted-build lanes in place until the new overlay has
    # committed. Removing one here could shift an audited source-track index.
    # They stay disabled and are cleaned up with the old backup lanes after
    # final tracks have been validated.
    stale_backups = find_backup_track_indices(timeline, strict=True)
    old_indices = find_managed_track_indices(timeline, strict=True)
    old_records = []
    for track_index in old_indices:
        track_name = str(timeline.GetTrackName("video", track_index) or "")
        old_records.append({
            "index": track_index,
            "name": track_name,
            "enabled": bool(timeline.GetIsTrackEnabled("video", track_index)),
            "backup_name": "",
        })
    media_pool = _safe_call(project, "GetMediaPool")
    if not media_pool:
        raise StillOverlayError("Resolve's Media Pool is unavailable")
    if reference_media_provider is None:
        reference_media_provider = prepare_timeline_reference_media
    provider_result = reference_media_provider(media_pool, matches)
    if (
        not isinstance(provider_result, (list, tuple))
        or len(provider_result) not in (3, 4)
    ):
        raise StillOverlayError(
            "Reference media provider returned an invalid result"
        )
    media_by_path, imported_count, source_end_is_exclusive = provider_result[:3]
    source_frame_limits = provider_result[3] if len(provider_result) == 4 else {}
    if not isinstance(source_frame_limits, dict):
        raise StillOverlayError(
            "Reference media provider returned invalid source limits"
        )
    source_end_is_exclusive = bool(source_end_is_exclusive)

    # Importing multiple JPEGs can take long enough for the user to change the
    # open edit. Recheck the complete audited structure immediately before the
    # first timeline mutation.
    _assert_current_context(resolve_app, project, timeline)
    if enforce_snapshot:
        if _timeline_structure_signature(timeline) != audit["timeline_signature"]:
            raise StillOverlayError(
                "The timeline changed while reference media was importing; "
                "run the script again"
            )
        _validate_match_snapshot(timeline, audited_matches)

    for track_index in stale_staging:
        if _safe_call(
            timeline, "SetTrackEnable", False,
            "video", track_index, False
        ) is False:
            raise StillOverlayError(
                "Could not disable an incomplete reference build track"
            )
    for track_index in stale_backups:
        if _safe_call(
            timeline, "SetTrackEnable", False,
            "video", track_index, False
        ) is False:
            raise StillOverlayError(
                "Could not disable an old reference backup track"
            )

    by_source_track = {}
    placement_by_match = {}
    for match, point, record_frame in placements:
        by_source_track.setdefault(match.track_index, []).append(match)
        placement_by_match[id(match)] = (point, record_frame)
    for source_track in by_source_track:
        by_source_track[source_track].sort(
            key=lambda value: (value.start, value.end, value.display_name)
        )

    run_tag = datetime.datetime.now().strftime("%H%M%S%f")
    staging_names = []
    created_items = []
    expected_segments_by_source = {
        source_track: [] for source_track in by_source_track
    }
    old_renamed = False
    new_finalized = False
    cleanup_warning = ""
    lane_number_by_source = {
        source_track: lane_number
        for lane_number, source_track in enumerate(sorted(by_source_track), 1)
    }
    final_name_by_source = {
        source_track: TRACK_PREFIX + str(lane_number_by_source[source_track])
        for source_track in sorted(by_source_track)
    }
    planned_final_names = list(final_name_by_source.values())
    try:
        # Insert immediately above each source track, working from high to low
        # so later lower insertions cannot invalidate an unprocessed source
        # index. This preserves unmatched titles/alternate takes on higher
        # tracks instead of covering them with a lower track's reference.
        for source_track in sorted(by_source_track, reverse=True):
            before_count = int(timeline.GetTrackCount("video") or 0)
            before_tracks = _video_track_fingerprints(timeline)
            target_track = int(source_track) + 1
            add_result = _safe_call(
                timeline, "AddTrack", False,
                "video", {"index": target_track}
            )
            after_count = int(timeline.GetTrackCount("video") or 0)
            if add_result is False and after_count == before_count:
                raise StillOverlayError("Resolve could not add a reference video track")
            if after_count != before_count + 1:
                raise StillOverlayError("Resolve did not add exactly one reference track")
            after_tracks = _video_track_fingerprints(timeline)
            insertion_candidates = _candidate_inserted_track_indices(
                before_tracks, after_tracks
            )
            insertion_is_proven = (
                target_track in insertion_candidates
                and (
                    bool(add_result)
                    or insertion_candidates == [target_track]
                )
            )
            if not insertion_is_proven:
                cleanup_detail = ""
                if len(insertion_candidates) == 1:
                    unexpected_track = insertion_candidates[0]
                    try:
                        _delete_track_indices(timeline, [unexpected_track])
                    except Exception as cleanup_error:
                        containment_failures = []
                        if int(timeline.GetTrackCount("video") or 0) == after_count:
                            disable_result = _safe_call(
                                timeline, "SetTrackEnable", False,
                                "video", unexpected_track, False
                            )
                            if (
                                disable_result is False
                                or bool(_safe_call(
                                    timeline, "GetIsTrackEnabled", True,
                                    "video", unexpected_track
                                ))
                            ):
                                containment_failures.append("disable it")
                            lock_result = _safe_call(
                                timeline, "SetTrackLock", False,
                                "video", unexpected_track, True
                            )
                            if (
                                lock_result is False
                                or not bool(_safe_call(
                                    timeline, "GetIsTrackLocked", False,
                                    "video", unexpected_track
                                ))
                            ):
                                containment_failures.append("lock it")
                        cleanup_detail = (
                            "; the unexpected V{} also could not be removed: {}{}"
                        ).format(
                            unexpected_track,
                            cleanup_error,
                            "; could not {}".format(
                                " or ".join(containment_failures)
                            ) if containment_failures else "",
                        )
                else:
                    cleanup_detail = (
                        "; the extra empty track could not be uniquely identified, "
                        "so no existing track was modified"
                    )
                raise StillOverlayError(
                    "Resolve did not insert the reference track at requested V{}{}"
                    .format(target_track, cleanup_detail)
                )
            staging_name = _staging_track_name(
                run_tag, lane_number_by_source[source_track]
            )
            # Register the planned identity before asking Resolve to label it.
            # Some remote API calls can report False after partially applying.
            staging_names.append(staging_name)
            name_result = _safe_call(
                timeline, "SetTrackName", False,
                "video", target_track, staging_name
            )
            actual_staging_name = str(_safe_call(
                timeline, "GetTrackName", "",
                "video", target_track
            ) or "")
            if (
                name_result is False
                or actual_staging_name != staging_name
            ):
                try:
                    _delete_track_indices(timeline, [target_track])
                except Exception as cleanup_error:
                    containment_errors = []
                    # DeleteTrack reported failure. Only touch target_track if
                    # the track count proves the inserted lane still exists;
                    # otherwise a shifted source could now occupy that index.
                    if int(timeline.GetTrackCount("video") or 0) == after_count:
                        actual_name = str(_safe_call(
                            timeline, "GetTrackName", "",
                            "video", target_track
                        ) or "")
                        if actual_name != staging_name:
                            rename_result = _safe_call(
                                timeline, "SetTrackName", False,
                                "video", target_track, staging_name
                            )
                            actual_name = str(_safe_call(
                                timeline, "GetTrackName", "",
                                "video", target_track
                            ) or "")
                            if (
                                rename_result is False
                                or actual_name != staging_name
                            ):
                                containment_errors.append(
                                    "label the leftover empty V{}".format(target_track)
                                )
                        if _safe_call(
                            timeline, "SetTrackEnable", False,
                            "video", target_track, False
                        ) is False or bool(_safe_call(
                            timeline, "GetIsTrackEnabled", True,
                            "video", target_track
                        )):
                            containment_errors.append(
                                "disable the leftover empty V{}".format(target_track)
                            )
                        if _safe_call(
                            timeline, "SetTrackLock", False,
                            "video", target_track, True
                        ) is False or not bool(_safe_call(
                            timeline, "GetIsTrackLocked", False,
                            "video", target_track
                        )):
                            containment_errors.append(
                                "lock the leftover empty V{}".format(target_track)
                            )
                    detail = "; could not {}".format(
                        ", ".join(containment_errors)
                    ) if containment_errors else ""
                    raise StillOverlayError(
                        "Resolve could not label the new reference track, and "
                        "could not remove it during rollback: {}{}".format(
                            cleanup_error, detail
                        )
                    )
                raise StillOverlayError(
                    "Resolve could not label the new reference track"
                )

            # Disable the lane before any JPEG is inserted so an in-progress
            # build can never flash a reference into the viewer or a render.
            if _safe_call(
                timeline, "SetTrackEnable", False,
                "video", target_track, False
            ) is False or bool(_safe_call(
                timeline, "GetIsTrackEnabled", True,
                "video", target_track
            )):
                raise StillOverlayError("Could not disable the staged reference track")

            for match in by_source_track[source_track]:
                media_key = _canonical_path(match.still.path)
                media_item = media_by_path.get(media_key)
                if not media_item:
                    raise StillOverlayError(
                        "Imported media is unavailable for {}".format(match.still.path)
                    )
                point, record_frame = placement_by_match[id(match)]
                _assert_current_context(resolve_app, project, timeline)
                if enforce_snapshot:
                    _validate_match_snapshot(timeline, [match])
                source_frame_limit = int(
                    source_frame_limits.get(media_key, 0) or 0
                )
                if source_frame_limit and source_frame_limit < 1:
                    raise StillOverlayError(
                        "Reference helper has no usable frame for {}".format(
                            match.still.path
                        )
                    )
                requested_end = int(record_frame) + 1
                clip_info = build_overlay_clip_info(
                    match,
                    media_item,
                    record_start=record_frame,
                    record_end=requested_end,
                    source_end_is_exclusive=source_end_is_exclusive,
                )
                clip_info["trackIndex"] = target_track
                appended = _safe_call(
                    media_pool, "AppendToTimeline", None, [clip_info]
                )
                if not appended or len(appended) != 1:
                    raise StillOverlayError(
                        "Resolve could not place reference '{}' at frame {}"
                        .format(
                            os.path.basename(match.still.path), record_frame,
                        )
                    )
                overlay_item = appended[0]
                actual_end = _verify_appended_item(
                    overlay_item,
                    match,
                    target_track,
                    media_item,
                    expected_start=record_frame,
                    requested_end=requested_end,
                )
                item_name = ITEM_PREFIX + os.path.basename(match.still.path)
                if _safe_call(
                    overlay_item, "SetName", False, item_name
                ) is False:
                    raise StillOverlayError(
                        "Resolve could not label reference '{}'".format(
                            os.path.basename(match.still.path)
                        )
                    )
                _safe_call(overlay_item, "SetClipColor", False, "Pink")
                _set_item_property_verified(
                    overlay_item, "CompositeMode", 0
                )
                _set_item_property_verified(
                    overlay_item, "Opacity", REFERENCE_OPACITY
                )
                created_items.append(overlay_item)
                expected_segments_by_source[source_track].append((
                    match, int(record_frame), actual_end
                ))

            if _safe_call(
                timeline, "SetTrackLock", False,
                "video", target_track, True
            ) is False or not bool(_safe_call(
                timeline, "GetIsTrackLocked", False,
                "video", target_track
            )):
                raise StillOverlayError("Could not lock the staged reference track")

        # Commit without deleting the old working overlay first. Old lanes are
        # moved to a unique backup namespace and disabled; until the new lanes
        # are finalized, those name/state changes can be rolled back.
        current_old_tracks = _find_tracks_by_exact_names(
            timeline, [record["name"] for record in old_records]
        )
        if len(current_old_tracks) != len(old_records):
            raise StillOverlayError(
                "Could not rediscover every prior managed reference track"
            )
        for record in old_records:
            current_old_index = current_old_tracks[record["name"]]
            lane_match = TRACK_PATTERN.match(record["name"])
            lane_number = int(lane_match.group(1))
            backup_name = _backup_track_name(run_tag, lane_number)
            # Record the attempted name before calling Resolve so rollback can
            # recover even if the API returns False after partially applying it.
            record["backup_name"] = backup_name
            if _safe_call(
                timeline, "SetTrackName", False,
                "video", current_old_index, backup_name
            ) is False:
                raise StillOverlayError(
                    "Resolve could not stage the prior reference track for replacement"
                )
            if _safe_call(
                timeline, "SetTrackEnable", False,
                "video", current_old_index, False
            ) is False:
                raise StillOverlayError(
                    "Resolve could not disable the prior reference track"
                )
        old_renamed = True

        staging_tracks = _find_tracks_by_exact_names(timeline, staging_names)
        if len(staging_tracks) != len(staging_names):
            raise StillOverlayError("Could not rediscover every staged reference track")
        final_names = []
        for source_track in sorted(by_source_track):
            staging_name = _staging_track_name(
                run_tag, lane_number_by_source[source_track]
            )
            track_index = staging_tracks[staging_name]
            final_name = final_name_by_source[source_track]
            if _safe_call(
                timeline, "SetTrackName", False,
                "video", track_index, final_name
            ) is False:
                raise StillOverlayError(
                    "Resolve could not finalize reference track '{}'".format(final_name)
                )
            final_names.append(final_name)

        final_tracks = _find_tracks_by_exact_names(timeline, final_names)
        if len(final_tracks) != len(final_names):
            raise StillOverlayError("Could not rediscover every finalized reference track")
        final_indices = sorted(final_tracks.values())
        for track_index in final_indices:
            valid, reason = validate_managed_track(timeline, track_index)
            if not valid:
                raise StillOverlayError(
                    "New reference track validation failed: {}".format(reason)
                )
        # Every build commits OFF, even when the replaced reference was ON.
        # This prevents a rebuild from silently placing baked JPEGs in a render.
        references_enabled = False
        set_reference_visibility(timeline, final_indices, references_enabled)
        final_indices = _validate_final_layout(
            timeline, audit, by_source_track, final_name_by_source,
            media_by_path, expected_segments_by_source
        )
        new_finalized = True

        # The new build is now complete and usable. Cleanup of old backup lanes
        # is best-effort: a DeleteTrack failure must never remove the new build.
        recovery_indices = (
            find_staging_track_indices(timeline, strict=True)
            + find_backup_track_indices(timeline, strict=True)
        )
        if recovery_indices:
            try:
                _delete_track_indices(timeline, recovery_indices)
            except Exception as error:
                cleanup_warning = (
                    "New references are usable, but an old disabled backup or "
                    "recovery track could not be removed: {}".format(error)
                )
                remaining = (
                    find_staging_track_indices(timeline, strict=False)
                    + find_backup_track_indices(timeline, strict=False)
                )
                containment_failures = []
                for track_index in remaining:
                    disable_result = _safe_call(
                        timeline, "SetTrackEnable", False,
                        "video", track_index, False
                    )
                    actual_enabled = bool(_safe_call(
                        timeline, "GetIsTrackEnabled", True,
                        "video", track_index
                    ))
                    if disable_result is False or actual_enabled:
                        containment_failures.append(
                            "disable V{}".format(track_index)
                        )
                    lock_result = _safe_call(
                        timeline, "SetTrackLock", False,
                        "video", track_index, True
                    )
                    actual_locked = bool(_safe_call(
                        timeline, "GetIsTrackLocked", False,
                        "video", track_index
                    ))
                    if lock_result is False or not actual_locked:
                        containment_failures.append(
                            "lock V{}".format(track_index)
                        )
                if containment_failures:
                    cleanup_warning += (
                        " Resolve also could not {}; manually disable and lock "
                        "the recovery track before continuing.".format(
                            ", ".join(containment_failures)
                        )
                    )
                references_enabled = any(
                    bool(_safe_call(
                        timeline, "GetIsTrackEnabled", False,
                        "video", track_index
                    ))
                    for track_index in final_indices + remaining
                )

        final_tracks = _find_tracks_by_exact_names(timeline, final_names)
        if len(final_tracks) != len(final_names):
            raise StillOverlayError(
                "A finalized reference track disappeared during recovery cleanup"
            )
        final_indices = sorted(final_tracks.values())
        final_indices = _validate_final_layout(
            timeline, audit, by_source_track, final_name_by_source,
            media_by_path, expected_segments_by_source
        )
        return BuildResult(
            final_indices,
            overlay_count=len(matches),
            imported_count=imported_count,
            replaced_track_count=len(old_indices),
            references_enabled=references_enabled,
            cleanup_warning=cleanup_warning,
            segment_count=len(created_items),
        )
    except Exception as original_error:
        if not new_finalized:
            rollback_errors = []
            # Remove only this run's staged/finalized-new lanes. At this point
            # the old lanes have not been deleted and can be restored by name.
            new_names = list(staging_names)
            if old_renamed:
                # All old lanes are in the unique backup namespace before any
                # new lane receives a final name, so this cannot select an old
                # working track during rollback.
                new_names.extend(planned_final_names)
            new_tracks = _find_tracks_by_exact_names(timeline, new_names)
            if new_tracks:
                try:
                    _delete_track_indices(timeline, new_tracks.values())
                except Exception as cleanup_error:
                    rollback_errors.append(
                        "remove new track(s): {}".format(cleanup_error)
                    )
                    # A descending multi-track delete can partly succeed before
                    # a later DeleteTrack fails. Rediscover by exact name;
                    # indices captured before deletion may now point at source
                    # tracks and must never be reused.
                    remaining_new_tracks = _find_tracks_by_exact_names(
                        timeline, new_names
                    )
                    final_to_staging = {
                        final_name_by_source[source_track]: _staging_track_name(
                            run_tag, lane_number_by_source[source_track]
                        )
                        for source_track in by_source_track
                    }
                    for current_name, track_index in remaining_new_tracks.items():
                        if current_name in final_to_staging:
                            staging_name = final_to_staging[current_name]
                            rename_result = _safe_call(
                                timeline, "SetTrackName", False,
                                "video", track_index, staging_name
                            )
                            actual_name = str(_safe_call(
                                timeline, "GetTrackName", "",
                                "video", track_index
                            ) or "")
                            if (
                                rename_result is False
                                or actual_name != staging_name
                            ):
                                rollback_errors.append(
                                    "return {} to its recovery namespace".format(
                                        current_name
                                    )
                                )
                        disable_result = _safe_call(
                            timeline, "SetTrackEnable", False,
                            "video", track_index, False
                        )
                        if (
                            disable_result is False
                            or bool(_safe_call(
                                timeline, "GetIsTrackEnabled", True,
                                "video", track_index
                            ))
                        ):
                            rollback_errors.append(
                                "disable remaining new V{}".format(track_index)
                            )
                        lock_result = _safe_call(
                            timeline, "SetTrackLock", False,
                            "video", track_index, True
                        )
                        if (
                            lock_result is False
                            or not bool(_safe_call(
                                timeline, "GetIsTrackLocked", False,
                                "video", track_index
                            ))
                        ):
                            rollback_errors.append(
                                "lock remaining new V{}".format(track_index)
                            )
            if old_renamed or any(record["backup_name"] for record in old_records):
                backup_names = [
                    record["backup_name"] for record in old_records
                    if record["backup_name"]
                ]
                restore_names = backup_names + [
                    record["name"] for record in old_records
                ]
                restore_tracks = _find_tracks_by_exact_names(
                    timeline, restore_names
                )
                restore_errors = []
                for record in old_records:
                    track_index = (
                        restore_tracks.get(record["backup_name"])
                        or restore_tracks.get(record["name"])
                    )
                    if not track_index:
                        restore_errors.append(
                            "locate {}".format(record["name"])
                        )
                        continue
                    actual_name = str(_safe_call(
                        timeline, "GetTrackName", "",
                        "video", track_index
                    ) or "")
                    if actual_name != record["name"]:
                        name_result = _safe_call(
                            timeline, "SetTrackName", False,
                            "video", track_index, record["name"]
                        )
                        actual_name = str(_safe_call(
                            timeline, "GetTrackName", "",
                            "video", track_index
                        ) or "")
                        if name_result is False or actual_name != record["name"]:
                            restore_errors.append(
                                "restore name {}".format(record["name"])
                            )
                    enable_result = _safe_call(
                        timeline, "SetTrackEnable", False,
                        "video", track_index, record["enabled"]
                    )
                    actual_enabled = bool(_safe_call(
                        timeline, "GetIsTrackEnabled", not record["enabled"],
                        "video", track_index
                    ))
                    if (
                        enable_result is False
                        or actual_enabled != record["enabled"]
                    ):
                        restore_errors.append(
                            "restore visibility {}".format(record["name"])
                        )
                    lock_result = _safe_call(
                        timeline, "SetTrackLock", False,
                        "video", track_index, True
                    )
                    actual_locked = bool(_safe_call(
                        timeline, "GetIsTrackLocked", False,
                        "video", track_index
                    ))
                    if lock_result is False or not actual_locked:
                        restore_errors.append(
                            "restore lock {}".format(record["name"])
                        )
                rollback_errors.extend(restore_errors)
            if rollback_errors:
                raise StillOverlayError(
                    "{}; rollback also failed: {}".format(
                        original_error, ", ".join(rollback_errors)
                    )
                )
        raise


def remove_managed_tracks(timeline):
    indices = (
        find_managed_track_indices(timeline, strict=True)
        + find_staging_track_indices(timeline, strict=True)
        + find_backup_track_indices(timeline, strict=True)
    )
    if not indices:
        raise StillOverlayError("No managed reference tracks exist on this timeline")
    _delete_track_indices(timeline, indices)
    remove_review_markers(timeline)
    return len(indices)


def _reference_composite_values(resolve_app):
    normal_value = getattr(resolve_app, "COMPOSITE_NORMAL", None)
    difference_value = getattr(resolve_app, "COMPOSITE_DIFF", None)
    # PyRemoteObject returns None for unknown dynamic attributes instead of
    # honoring getattr's default. These numeric values are the documented
    # TimelineItem CompositeMode enum values in Resolve 21.
    normal_value = 0 if normal_value is None else int(normal_value)
    difference_value = 3 if difference_value is None else int(difference_value)
    return {"normal": normal_value, "difference": difference_value}


def _reference_property_equal(name, actual, expected):
    try:
        if name == "CompositeMode":
            return float(actual) == float(expected)
        return abs(float(actual) - float(expected)) < 0.001
    except (TypeError, ValueError, OverflowError):
        return False


def _reference_track_boolean(timeline, method, track_index):
    value = _safe_call(timeline, method, None, "video", track_index)
    if value is None:
        raise StillOverlayError(
            "Resolve could not read {} on reference track V{}".format(
                method, track_index
            )
        )
    return bool(value)


def _reference_snapshot(timeline, track_indices, property_names):
    """Read the entire operation before allowing any track or item write."""
    indices = list(dict.fromkeys(int(value) for value in track_indices))
    if not indices:
        raise StillOverlayError("No managed reference tracks exist on this timeline")
    records = []
    for track_index in indices:
        record = {
            "index": track_index,
            "locked": _reference_track_boolean(
                timeline, "GetIsTrackLocked", track_index
            ),
            "enabled": _reference_track_boolean(
                timeline, "GetIsTrackEnabled", track_index
            ),
            "items": [],
        }
        try:
            items = list(timeline.GetItemListInTrack("video", track_index) or [])
        except Exception as error:
            raise StillOverlayError(
                "Resolve could not read reference items on V{}: {}".format(
                    track_index, error
                )
            )
        if not items:
            raise StillOverlayError(
                "Managed reference track V{} has no reference items".format(
                    track_index
                )
            )
        for item in items:
            properties = {}
            for name in property_names:
                value = _safe_call(item, "GetProperty", None, name)
                try:
                    number = float(value)
                    readable = number == number and abs(number) != float("inf")
                except (TypeError, ValueError, OverflowError):
                    readable = False
                if not readable:
                    raise StillOverlayError(
                        "Resolve could not read the existing reference {} on "
                        "V{}; no comparison change was made".format(name, track_index)
                    )
                properties[name] = value
            record["items"].append((item, properties))
        records.append(record)
    return records


def _reference_comparison_summary(resolve_app, records):
    composite_values = _reference_composite_values(resolve_app)
    mode_names = {value: key for key, value in composite_values.items()}
    properties = [
        properties for record in records for _, properties in record["items"]
    ]
    modes = {
        mode_names.get(float(values["CompositeMode"]), "mixed")
        for values in properties
    }
    opacities = [float(values["Opacity"]) for values in properties]
    opacity = opacities[0]
    if any(abs(value - opacity) >= 0.001 for value in opacities):
        opacity = None
    states = {record["index"]: record["enabled"] for record in records}
    return {
        "mode": next(iter(modes)) if len(modes) == 1 else "mixed",
        "opacity": opacity,
        "enabled": any(states.values()),
        "states": states,
        "locks": {record["index"]: record["locked"] for record in records},
        "item_count": len(properties),
    }


def read_reference_comparison(resolve_app, timeline, track_indices):
    """Return current item mode/opacity and actual reference track states.

    Mixed modes return ``mode='mixed'``; mixed opacities return ``opacity=None``.
    ``enabled`` means at least one reference track is visible. The caller can
    inspect ``states`` when distinguishing partially visible track groups.
    """
    return _reference_comparison_summary(
        resolve_app,
        _reference_snapshot(timeline, track_indices, ("CompositeMode", "Opacity")),
    )


def _set_reference_properties(timeline, track_indices, target_properties,
                              enabled=None):
    track_indices = list(dict.fromkeys(int(value) for value in track_indices))
    original = _reference_snapshot(timeline, track_indices, target_properties)
    changed = []
    operation_error = None

    def set_track_boolean(record, setter, getter, desired):
        result = _safe_call(
            timeline, setter, False, "video", record["index"], desired
        )
        actual = _reference_track_boolean(timeline, getter, record["index"])
        if result is False or actual != desired:
            raise StillOverlayError(
                "Resolve could not {} reference track V{}".format(
                    "restore the lock on" if setter == "SetTrackLock" else
                    "change visibility on", record["index"]
                )
            )

    def restore_locks():
        failures = []
        for record in reversed(original):
            try:
                actual = _reference_track_boolean(
                    timeline, "GetIsTrackLocked", record["index"]
                )
                if actual != record["locked"]:
                    set_track_boolean(
                        record, "SetTrackLock", "GetIsTrackLocked", record["locked"]
                    )
            except Exception:
                failures.append("restore lock on V{}".format(record["index"]))
        return failures

    try:
        for record in original:
            if record["locked"]:
                set_track_boolean(record, "SetTrackLock", "GetIsTrackLocked", False)
            for item, previous in record["items"]:
                for name, value in target_properties.items():
                    if _reference_property_equal(name, previous[name], value):
                        continue
                    # Track the attempted write before calling Resolve: a False
                    # response may still have applied a property partially.
                    changed.append((record, item, name, previous[name]))
                    result = _safe_call(item, "SetProperty", False, name, value)
                    actual = _safe_call(item, "GetProperty", None, name)
                    if result is False or not _reference_property_equal(name, actual, value):
                        raise StillOverlayError(
                            "Resolve did not retain reference {} on V{}".format(
                                name, record["index"]
                            )
                        )
        if enabled is not None:
            for record in original:
                set_track_boolean(
                    record, "SetTrackEnable", "GetIsTrackEnabled", bool(enabled)
                )
        lock_failures = restore_locks()
        if lock_failures:
            raise StillOverlayError("Resolve could not " + ", ".join(lock_failures))
        final = _reference_snapshot(timeline, track_indices, target_properties)
        for record in final:
            before = next(value for value in original if value["index"] == record["index"])
            if record["locked"] != before["locked"]:
                raise StillOverlayError("Reference track lock changed during comparison")
            expected_enabled = before["enabled"] if enabled is None else bool(enabled)
            if record["enabled"] != expected_enabled:
                raise StillOverlayError("Reference visibility changed during comparison")
            if len(record["items"]) != len(before["items"]):
                raise StillOverlayError("Reference items changed during comparison")
            for _, properties in record["items"]:
                for name, value in target_properties.items():
                    if not _reference_property_equal(name, properties[name], value):
                        raise StillOverlayError("Reference properties changed during comparison")
        return final
    except Exception as error:
        operation_error = error

    rollback_failures = []
    changed_indices = {record["index"] for record, _, _, _ in changed}
    for record in original:
        if record["index"] not in changed_indices:
            continue
        try:
            if _reference_track_boolean(timeline, "GetIsTrackLocked", record["index"]):
                set_track_boolean(record, "SetTrackLock", "GetIsTrackLocked", False)
        except Exception:
            rollback_failures.append("unlock V{}".format(record["index"]))
    for record, item, name, previous in reversed(changed):
        result = _safe_call(item, "SetProperty", False, name, previous)
        actual = _safe_call(item, "GetProperty", None, name)
        if result is False or not _reference_property_equal(name, actual, previous):
            rollback_failures.append("restore {} on V{}".format(name, record["index"]))
    # Restore visibility even after a partial/False write or a failed readback.
    for record in original:
        try:
            current = _reference_track_boolean(timeline, "GetIsTrackEnabled", record["index"])
            if current != record["enabled"]:
                set_track_boolean(
                    record, "SetTrackEnable", "GetIsTrackEnabled", record["enabled"]
                )
        except Exception:
            rollback_failures.append("restore visibility on V{}".format(record["index"]))
    rollback_failures.extend(restore_locks())
    if rollback_failures:
        raise StillOverlayError(
            "{}; rollback also could not {}".format(
                operation_error, ", ".join(rollback_failures)
            )
        )
    raise operation_error


def set_reference_comparison(resolve_app, timeline, track_indices,
                             mode="normal", opacity=100.0, enabled=None):
    """Transactionally apply a comparison mode and opacity to all references.

    Visibility is unchanged when enabled is None. Locks, item properties and
    visibility are restored if any write, readback, or relock fails.
    """
    mode = str(mode).strip().lower()
    composite_values = _reference_composite_values(resolve_app)
    if mode not in composite_values:
        raise StillOverlayError("Comparison mode must be Normal or Difference")
    try:
        opacity = float(opacity)
    except (TypeError, ValueError, OverflowError):
        raise StillOverlayError("Reference opacity must be between 0 and 100")
    if not 0.0 <= opacity <= 100.0:
        raise StillOverlayError("Reference opacity must be between 0 and 100")
    return _reference_comparison_summary(
        resolve_app,
        _set_reference_properties(
            timeline, track_indices,
            {"CompositeMode": composite_values[mode], "Opacity": opacity},
            enabled=enabled,
        ),
    )


def set_reference_difference(resolve_app, timeline, track_indices, enabled,
                             show_reference=False):
    """Compatibility entry point for callers changing only composite mode."""
    composite_values = _reference_composite_values(resolve_app)
    _set_reference_properties(
        timeline, track_indices,
        {"CompositeMode": composite_values["difference" if enabled else "normal"]},
        enabled=True if enabled or show_reference else None,
    )
    return bool(enabled)


def _slug(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    return value or "Resolve"


def create_report_path(project_name, timeline_name):
    if not os.path.isdir(LOG_DIRECTORY):
        os.makedirs(LOG_DIRECTORY)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(
        LOG_DIRECTORY,
        "{}-{}-{}.log".format(
            _slug(project_name), _slug(timeline_name), timestamp
        ),
    )


def write_report(path, content):
    with open(path, "w", encoding="utf-8") as report_file:
        report_file.write(content)


def _osascript(script, arguments=None):
    command = ["/usr/bin/osascript", "-e", script]
    command.extend(arguments or [])
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise StillOverlayError((stderr or stdout or "AppleScript failed").strip())
    return stdout.strip()


def choose_still_folder():
    script = r'''
try
    set chosenFolder to choose folder with prompt "Choose the folder containing Livegrade reference JPEGs"
    return POSIX path of chosenFolder
on error number -128
    return ""
end try
'''
    return _osascript(script)


def macos_dialog(message, title=SCRIPT_NAME, buttons=None, default_button=None,
                 cancel_button=None, caution=False):
    buttons = buttons or ["OK"]
    default_button = default_button or buttons[-1]
    cancel_button = cancel_button or ""
    button_list = "{" + ", ".join('"{}"'.format(value) for value in buttons) + "}"
    icon_clause = " with icon caution" if caution else ""
    cancel_clause = ' cancel button "{}"'.format(cancel_button) if cancel_button else ""
    script = r'''
on run argv
    set dialogMessage to item 1 of argv
    set dialogTitle to item 2 of argv
    try
        set answer to display dialog dialogMessage with title dialogTitle buttons __BUTTONS__ default button "__DEFAULT__"__CANCEL__ __ICON__
        return button returned of answer
    on error number -128
        return "CANCELLED"
    end try
end run
'''
    script = script.replace("__BUTTONS__", button_list)
    script = script.replace("__DEFAULT__", default_button)
    script = script.replace("__CANCEL__", cancel_clause)
    script = script.replace("__ICON__", icon_clause)
    return _osascript(script, [str(message), str(title)])


def macos_notification(message):
    if sys.platform != "darwin":
        return
    script = r'''
on run argv
    display notification (item 1 of argv) with title (item 2 of argv)
end run
'''
    try:
        _osascript(script, [str(message), SCRIPT_NAME])
    except Exception:
        pass


def connect_to_resolve():
    injected = globals().get("resolve")
    if injected:
        return injected
    module_paths = [
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
        "/opt/resolve/Developer/Scripting/Modules",
    ]
    for path in module_paths:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        import DaVinciResolveScript as dvr_script
    except ImportError as error:
        raise StillOverlayError(
            "Cannot import DaVinciResolveScript: {}".format(error)
        )
    resolve_app = dvr_script.scriptapp("Resolve")
    if not resolve_app:
        raise StillOverlayError(
            "DaVinci Resolve is not running or scripting access is disabled"
        )
    return resolve_app


def _current_context(resolve_app):
    project_manager = _safe_call(resolve_app, "GetProjectManager")
    project = _safe_call(project_manager, "GetCurrentProject")
    if not project:
        raise StillOverlayError("Open a Resolve project first")
    timeline = _safe_call(project, "GetCurrentTimeline")
    if not timeline:
        raise StillOverlayError("Open the timeline you want to process first")
    return project, timeline


def _confirmation_message(audit, report_path, replacing_tracks):
    counts = audit_counts(audit)
    lines = [
        "Audit complete for the current timeline.",
        "",
        "Matched timeline items: {}".format(counts["matched"]),
        "Unique JPEGs: {}".format(counts["unique_stills"]),
        "Reference tracks to build: {}".format(counts["source_tracks"]),
        "Ambiguous/invalid: {}".format(
            counts["ambiguous"] + counts["invalid_timing"]
        ),
        "Unmatched items: {}".format(counts["unmatched"]),
        "Disabled source tracks skipped: {}".format(
            len(audit.get("skipped_disabled_tracks", []))
        ),
    ]
    if replacing_tracks:
        lines.append("Existing managed tracks to replace: {}".format(replacing_tracks))
    lines.extend([
        "",
        "Each readable grab TC gets one cropped one-frame reference at 100% opacity.",
        "References are added on locked companion tracks and start OFF.",
        "Original tracks and grades are not changed. The project is not saved automatically.",
        "",
        "Audit report: {}".format(report_path),
    ])
    return "\n".join(lines)


def _format_build_result(result, report_path):
    state = "ON" if result.references_enabled else "OFF"
    lines = [
        "Reference overlays built.",
        "",
        "Reference grabs placed: {}".format(result.overlay_count),
        "Timeline reference items: {}".format(result.segment_count),
        "Reference tracks: {}".format(result.track_count),
        "Media Pool items newly imported: {}".format(result.imported_count),
        "Old managed tracks replaced: {}".format(result.replaced_track_count),
        "Review stops: {} ({} exact)".format(
            result.review_point_count, result.exact_review_point_count
        ),
        "Current reference state: {}".format(state),
        "",
        "Normal starts at 100% opacity. Use the review controller or 'Reference "
        "Still Toggle' to A/B and step between grabs.",
        "Resolve's Image Wipe button is for Gallery stills and does not control "
        "these timeline overlays.",
        "The project was not saved automatically.",
        "",
        "Report: {}".format(report_path),
    ]
    if result.cleanup_warning:
        lines.extend(["", "CLEANUP WARNING: {}".format(result.cleanup_warning)])
    if result.review_warning:
        lines.extend(["", "REVIEW WARNING: {}".format(result.review_warning)])
    return "\n".join(lines)


def _announce_build_result(result, report_path, verb="built"):
    if result.cleanup_warning or result.review_warning:
        macos_dialog(
            _format_build_result(result, report_path),
            caution=True,
        )
    else:
        macos_notification(
            "Reference overlay {} with {} review stops ({} exact)".format(
                verb, result.review_point_count,
                result.exact_review_point_count,
            )
        )


def build_from_folder(resolve_app, project, timeline, folder, recursive=True,
                      audit_only=False, assume_yes=False):
    folder = os.path.abspath(os.path.expanduser(folder))
    records, still_index = build_still_library(folder, recursive=recursive)
    if not records:
        raise StillOverlayError(
            "No .jpg or .jpeg files were found in {}".format(folder)
        )
    audit = audit_timeline(timeline, still_index)
    project_name = str(_safe_call(project, "GetName", "Untitled Project") or "Untitled Project")
    timeline_name = str(_safe_call(timeline, "GetName", "Untitled Timeline") or "Untitled Timeline")
    resolve_version = str(_safe_call(resolve_app, "GetVersionString", "") or "")
    report_path = create_report_path(project_name, timeline_name)
    report = format_audit_report(
        audit, folder, project_name, timeline_name, resolve_version
    )
    write_report(report_path, report)
    print(report)
    print("Audit report: {}".format(report_path))
    if audit_only:
        return audit, None, report_path
    if not audit["matches"]:
        raise StillOverlayError(
            "No timeline source filenames matched the JPEG filenames"
        )
    existing = find_managed_track_indices(timeline, strict=True)
    if not assume_yes:
        if sys.platform != "darwin":
            raise StillOverlayError("Use --yes for non-interactive builds")
        answer = macos_dialog(
            _confirmation_message(audit, report_path, len(existing)),
            buttons=["Cancel", "Build"],
            default_button="Cancel",
            cancel_button="Cancel",
            caution=True,
        )
        if answer != "Build":
            return audit, None, report_path
    fresh_audit = audit_timeline(timeline, still_index)
    if fresh_audit.get("timeline_signature") != audit.get("timeline_signature"):
        raise StillOverlayError(
            "The timeline changed after the audit; run the script again so "
            "reference positions and enabled tracks can be re-audited"
        )
    audit = fresh_audit
    original_page = str(_safe_call(resolve_app, "GetCurrentPage", "") or "")
    original_timecode = str(_safe_call(timeline, "GetCurrentTimecode", "") or "")
    try:
        try:
            review_points, review_warnings, review_placements = build_review_plan(
                timeline, audit["matches"], project=project,
                resolve_app=resolve_app
            )
        except ReviewStateRestoreError as restore_error:
            raise ReviewStateRestoreError(
                "CRITICAL: {}. Review verification stopped before the prior "
                "reference overlay was replaced. Check the named video/"
                "subtitle-track enable state(s) manually. The project was not "
                "saved automatically.".format(restore_error)
            )
        except Exception as review_error:
            raise StillOverlayError(
                "Reference timecodes could not be mapped before building; the "
                "prior reference overlay was left unchanged: {}".format(
                    review_error
                )
            )
        if not review_placements:
            raise StillOverlayError(
                "No reference JPEG had a readable source timecode that could "
                "be placed in this edit. The prior reference overlay was left "
                "unchanged. {}".format("; ".join(review_warnings))
            )

        prepared_paths, crop_failures, crop_notes = prepare_cropped_reference_paths(
            project, timeline, [match for match, _ in review_placements]
        )
        if crop_failures:
            for match, point in review_placements:
                problem = crop_failures.get(_canonical_path(match.still.path))
                if problem:
                    point.mapped = False
                    point.exact = False
                    point.reason = "Crop unavailable; marker only: " + problem
                    review_warnings.append("{}: {}".format(point.filename, point.reason))
            review_placements = [(match, point) for match, point in review_placements if point.mapped]
        if not review_placements:
            raise StillOverlayError(
                "No safely cropped references remain; previous tracks were left unchanged. "
                + "; ".join(crop_notes)
            )
        report += "\nREFERENCE IMAGE PREPARATION\n" + "\n".join(crop_notes) + "\n"
        write_report(report_path, report)
        result = build_overlays(
            resolve_app, project, timeline, audit,
            review_placements=review_placements,
            reference_media_provider=lambda media_pool, matches:
                prepare_timeline_reference_media(media_pool, matches, prepared_paths),
        )
        result.review_points = tuple(review_points)
        result.review_point_count = len(review_points)
        result.exact_review_point_count = sum(
            1 for point in review_points if point.exact
        )
        marker_warnings = []
        try:
            _, marker_warnings = sync_review_markers(timeline, review_points)
        except Exception as marker_error:
            marker_cleanup_error = ""
            try:
                remove_review_markers(timeline)
            except Exception as cleanup_error:
                marker_cleanup_error = (
                    "; old review markers also could not be cleared: {}"
                    .format(cleanup_error)
                )
            marker_warnings.append(
                "Reference items are usable and in-memory navigation remains "
                "available, but timeline markers could not be created: {}{}"
                .format(marker_error, marker_cleanup_error)
            )
        result.review_warning = "; ".join(
            review_warnings + marker_warnings
        )
    finally:
        if original_timecode:
            _safe_call(timeline, "SetCurrentTimecode", False, original_timecode)
        if original_page and original_page != str(
            _safe_call(resolve_app, "GetCurrentPage", "") or ""
        ):
            _safe_call(resolve_app, "OpenPage", False, original_page)
    result_text = _format_build_result(result, report_path)
    write_report(report_path, report + "\nBUILD RESULTS\n" + result_text + "\n")
    print(result_text)
    return audit, result, report_path


def _show_error(message):
    print("{}: {}".format(SCRIPT_NAME, message))
    if sys.platform == "darwin":
        try:
            macos_dialog(str(message), caution=True)
        except Exception:
            pass


def _ui_runtime(resolve_app):
    fusion_app = globals().get("fusion")
    if not fusion_app:
        fusion_app = _safe_call(resolve_app, "Fusion")
    if not fusion_app:
        raise StillOverlayError("Resolve's Fusion UI runtime is unavailable")
    ui = getattr(fusion_app, "UIManager", None)
    if not ui:
        raise StillOverlayError("Resolve's UI Manager is unavailable")
    bmd_api = globals().get("bmd")
    if not bmd_api or not hasattr(bmd_api, "UIDispatcher"):
        try:
            import DaVinciResolveScript as bmd_api
        except ImportError as error:
            raise StillOverlayError(
                "Cannot load Resolve's UI dispatcher: {}".format(error)
            )
    return ui, bmd_api.UIDispatcher(ui)


def show_review_controller(resolve_app, initial_points=None,
                           initial_timeline=None):
    """Show a persistent, modeless A/B and grab-navigation palette."""
    _ensure_timecode_navigation_page(resolve_app)
    ui, dispatcher = _ui_runtime(resolve_app)
    # Native FindWindow can return a closed window retained by callback cycles.
    # Focus only a controller that still owns an active UI session.
    gc.collect()
    support = _load_helper("lookbridge_support")
    existing = support.active_window(REVIEW_WINDOW_ID)
    if existing:
        existing.Show()
        existing.Raise()
        return 0

    guidance = _load_helper("psnr_guidance")

    window = dispatcher.AddWindow(
        {
            "ID": REVIEW_WINDOW_ID,
            "WindowTitle": "LookBridge — Reference Still Review",
            "Geometry": [140, 140, 790, 545],
            "Events": {"KeyPress": True},
        },
        ui.VGroup({"Spacing": 8}, [
            ui.Label({
                "ID": "OverlayState",
                "Text": "Overlay state unavailable",
                "Weight": 0,
            }),
            ui.Label({
                "ID": "GrabStatus",
                "Text": "No reference grab selected",
                "WordWrap": True,
                "MinimumSize": [0, 66],
                "Weight": 0,
            }),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Compare", "Weight": 0}),
                ui.ComboBox({"ID": "CompareMode"}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Opacity", "Weight": 0}),
                ui.Slider({"ID": "Opacity", "Orientation": "Horizontal",
                           "Minimum": 0, "Maximum": 100, "Value": 100,
                           "Tracking": False}),
                ui.Label({"ID": "OpacityValue", "Text": "100%", "Weight": 0}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "OverlayOn", "Text": "Reference ON"}),
                ui.Button({"ID": "OverlayOff", "Text": "Original / OFF"}),
                ui.Button({"ID": "Toggle", "Text": "Toggle A/B"}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Previous", "Text": "◀ Previous Grab"}),
                ui.Button({"ID": "Next", "Text": "Next Grab ▶"}),
            ]),
            ui.Label({"ID": "ComparisonHint", "WordWrap": True, "Weight": 0,
                      "Text": "Normal: full-opacity reference/original A/B. Difference: matching pixels are black at 100%."}),
            ui.Button({"ID": "Measure", "Text": "Measure PSNR — current grab", "Weight": 0}),
            ui.Label({"ID": "MetricStatus", "WordWrap": True, "Weight": 0,
                      "MinimumSize": [0, 90],
                      "Text": "PSNR not measured. Click Measure PSNR at a grab; remeasure after changing the grade."}),
            ui.Label({"ID": "PSNRGuide", "WordWrap": True, "Weight": 0,
                      "MinimumSize": [0, 44], "Text": guidance.PSNR_GUIDE}),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Rebuild", "Text": "Rebuild…"}),
                ui.HGap(1),
                ui.Button({"ID": "Close", "Text": "Close"}),
            ]),
            support.footer(ui),
        ]),
    )
    items = window.GetItems()
    for label in ("Normal", "Difference"):
        items["CompareMode"].AddItem(label)
    ui_state = {"syncing": False, "timeline_uid": "", "measuring": False}
    session_points = {}
    if initial_points is not None and initial_timeline:
        timeline_uid = str(
            _safe_call(initial_timeline, "GetUniqueId", "") or ""
        )
        if timeline_uid:
            session_points[timeline_uid] = tuple(initial_points)

    def current_context_and_points():
        project, timeline = _current_context(resolve_app)
        timeline_uid = str(_safe_call(timeline, "GetUniqueId", "") or "")
        if timeline_uid in session_points:
            # The just-built in-memory set remains authoritative for this
            # window, including targets whose exact frame already had a user
            # marker and therefore could not receive our marker.
            points = list(session_points[timeline_uid])
        else:
            points = review_points_from_markers(timeline)
        points.sort(key=lambda point: (
            point.frame_id, _normalized_key(point.filename), point.match_uid
        ))
        return project, timeline, points

    def set_callback_error(error):
        items["GrabStatus"].Text = "Error: {}".format(error)

    def resync_comparison_controls():
        """A rolled-back edit must not leave the palette showing the failed request."""
        _, timeline, _ = current_context_and_points()
        indices = find_managed_track_indices(timeline, strict=True)
        actual = read_reference_comparison(resolve_app, timeline, indices)
        ui_state["syncing"] = True
        try:
            if actual["mode"] in ("normal", "difference"):
                items["CompareMode"].CurrentIndex = 1 if actual["mode"] == "difference" else 0
            if actual["opacity"] is not None:
                items["Opacity"].Value = int(round(actual["opacity"]))
                items["OpacityValue"].Text = "{}%".format(int(round(actual["opacity"])))
        finally:
            ui_state["syncing"] = False
        refresh_status()

    def refresh_status(selected=None):
        try:
            project, timeline, points = current_context_and_points()
            try:
                indices = find_managed_track_indices(timeline, strict=True)
            except StillOverlayError:
                indices = []
            states = [
                bool(timeline.GetIsTrackEnabled("video", index))
                for index in indices
            ]
            current_spec = bool(indices) and managed_tracks_use_current_spec(
                timeline, indices
            )
            uid = str(_safe_call(timeline, "GetUniqueId", "") or "")
            if current_spec and uid != ui_state["timeline_uid"]:
                # A newly opened review starts with full-opacity Normal A/B.
                set_reference_comparison(resolve_app, timeline, indices, "normal", 100)
                ui_state["syncing"] = True
                items["CompareMode"].CurrentIndex = 0
                items["Opacity"].Value = 100
                items["OpacityValue"].Text = "100%"
                ui_state["syncing"] = False
            ui_state["timeline_uid"] = uid
            if not states:
                state = "NO OVERLAY"
            elif not current_spec:
                state = "LEGACY — REBUILD REQUIRED"
            elif all(states):
                state = "ON"
            elif any(states):
                state = "MIXED"
            else:
                state = "OFF"
            items["OverlayState"].Text = (
                "{} {}  ·  {}% opacity  ·  {} managed track{}".format(
                    ("Normal", "Difference")[int(items["CompareMode"].CurrentIndex)],
                    state, int(items["Opacity"].Value), len(indices), "" if len(indices) == 1 else "s"
                )
            )
            items["CompareMode"].Enabled = current_spec
            items["Opacity"].Enabled = current_spec
            items["Measure"].Enabled = current_spec and not ui_state["measuring"] and bool(points)
            items["OverlayOn"].Enabled = current_spec
            items["Toggle"].Enabled = current_spec
            items["OverlayOff"].Enabled = bool(indices)
            items["Previous"].Enabled = bool(points)
            items["Next"].Enabled = bool(points)
            if not points:
                items["GrabStatus"].Text = (
                    "No review stops. Rebuild the overlay to scan the full still "
                    "image and create markers."
                )
                return
            current_frame_id = current_review_frame(
                project, timeline, resolve_app
            )
            if selected is None:
                selected = next(
                    (point for point in points
                     if point.frame_id == current_frame_id),
                    None,
                )
            if selected is None:
                upcoming = choose_adjacent_review_point(
                    points, current_frame_id, 1
                )
                items["GrabStatus"].Text = (
                    "{} review stop{}  ·  Next: {}".format(
                        len(points), "" if len(points) == 1 else "s",
                        upcoming.filename,
                    )
                )
                return
            selected_index = next(
                (index for index, point in enumerate(points)
                 if point.frame_id == selected.frame_id
                 and point.filename == selected.filename),
                0,
            )
            target_tc = review_frame_to_timecode(
                project, timeline, selected.frame_id
            )
            if not selected.mapped:
                accuracy = "MARKER ONLY — NO IMAGE PLACED"
            else:
                accuracy = "EXACT" if selected.exact else "APPROXIMATE"
            source = (
                "  ·  Source TC {}".format(selected.source_timecode)
                if selected.source_timecode else ""
            )
            detail = (
                "  ·  {}".format(selected.reason) if selected.reason else ""
            )
            items["GrabStatus"].Text = (
                "Grab {} of {}  ·  {}  ·  Timeline {}{}  ·  {}{}".format(
                    selected_index + 1, len(points), selected.filename,
                    target_tc, source, accuracy, detail,
                )
            )
        except Exception as error:
            set_callback_error(error)

    def control_overlay(mode):
        if ui_state["measuring"]:
            return
        try:
            _, timeline, _ = current_context_and_points()
            indices = find_managed_track_indices(timeline, strict=True)
            if mode != "off" and not managed_tracks_use_current_spec(
                timeline, indices
            ):
                raise StillOverlayError(
                    "This timeline has a legacy full-clip reference lane; click "
                    "Rebuild before enabling the reference"
                )
            if mode == "on":
                set_reference_comparison(
                    resolve_app, timeline, indices,
                    "difference" if int(items["CompareMode"].CurrentIndex) == 1 else "normal",
                    float(items["Opacity"].Value), enabled=True,
                )
            elif mode == "off":
                set_reference_visibility(timeline, indices, False)
            else:
                toggle_reference_tracks(timeline, indices)
            refresh_status()
        except Exception as error:
            set_callback_error(error)

    def navigate(direction):
        if ui_state["measuring"]:
            return
        try:
            project, timeline, points = current_context_and_points()
            point = choose_adjacent_review_point(
                points,
                current_review_frame(project, timeline, resolve_app),
                direction,
            )
            if point is None:
                raise StillOverlayError(
                    "No review stops exist; rebuild the overlay first"
                )
            navigate_to_review_point(
                project, timeline, point, resolve_app
            )
            refresh_status(point)
            items["MetricStatus"].Text = "PSNR not measured for this grab. Click Measure PSNR when ready."
        except Exception as error:
            set_callback_error(error)

    def measure_psnr(event=None):
        if ui_state["measuring"]:
            return
        hidden = False
        try:
            project, timeline, points = current_context_and_points()
            frame = current_review_frame(project, timeline, resolve_app)
            selected = [point for point in points if point.frame_id == frame and point.mapped]
            if len(selected) != 1:
                items["MetricStatus"].Text = "PSNR: go to a single mapped grab with Previous / Next first."
                return
            indices = find_managed_track_indices(timeline, strict=True)
            ui_state["measuring"] = True
            for name in ("Measure", "CompareMode", "Opacity", "OverlayOn", "OverlayOff", "Toggle", "Previous", "Next", "Rebuild", "Close"):
                items[name].Enabled = False
            items["MetricStatus"].Text = "Measuring two isolated Resolve output frames..."
            capture = _load_helper("comparison_capture")
            # Functions are also callable when Resolve executes without a module entry.
            import types
            navigation = types.SimpleNamespace(**globals())
            # Match the build/export workflow: let Resolve render without the
            # floating palette covering or holding focus over its viewer.
            window.Hide()
            hidden = True
            metric = capture.calculate_review_psnr(
                resolve_app, project, timeline, indices, selected[0],
                _load_helper("reference_image"), navigation,
            )
            metric["exact"] = selected[0].exact
            items["MetricStatus"].Text = "Last measured — " + guidance.format_psnr_result(
                metric, filename=selected[0].filename, source_timecode=selected[0].source_timecode)
        except ReviewStateRestoreError as error:
            # A failed restoration must not be hidden by a fallback mutation.
            items["MetricStatus"].Text = "CRITICAL — check track state: {}".format(error)
        except Exception as error:
            items["MetricStatus"].Text = "PSNR unavailable: {}. Your A/B mode is unchanged; choose Difference for a visual check.".format(error)
        finally:
            ui_state["measuring"] = False
            if hidden:
                window.Show()
                window.Raise()
            items["Rebuild"].Enabled = True
            items["Close"].Enabled = True
            refresh_status()

    def on_comparison_changed(event):
        if ui_state["syncing"] or ui_state["measuring"]:
            return
        try:
            _, timeline, _ = current_context_and_points()
            indices = find_managed_track_indices(timeline, strict=True)
            index = int(items["CompareMode"].CurrentIndex)
            set_reference_comparison(resolve_app, timeline, indices,
                                     "difference" if index == 1 else "normal",
                                     float(items["Opacity"].Value))
            items["OpacityValue"].Text = "{}%".format(int(items["Opacity"].Value))
            refresh_status()
            items["ComparisonHint"].Text = ("Difference: black means matching output pixels at 100% opacity."
                if index == 1 else "Normal A/B: use 100% opacity for reference vs original.")
        except Exception as error:
            try:
                resync_comparison_controls()
            except Exception as read_error:
                error = StillOverlayError("{}; state readback failed: {}".format(error, read_error))
            set_callback_error(error)

    def on_overlay_on(event):
        control_overlay("on")

    def on_overlay_off(event):
        control_overlay("off")

    def on_toggle(event):
        control_overlay("toggle")

    def on_previous(event):
        navigate(-1)

    def on_next(event):
        navigate(1)

    def on_rebuild(event):
        if ui_state["measuring"]:
            return
        window.Hide()
        rebuild_error = None
        try:
            project, timeline = _current_context(resolve_app)
            folder = choose_still_folder()
            if folder:
                _, result, report_path = build_from_folder(
                    resolve_app, project, timeline, folder,
                    assume_yes=False,
                )
                if result:
                    timeline_uid = str(
                        _safe_call(timeline, "GetUniqueId", "") or ""
                    )
                    if timeline_uid:
                        session_points[timeline_uid] = result.review_points
                    ui_state["timeline_uid"] = ""
                    items["MetricStatus"].Text = "Rebuilt cropped references. Normal starts at 100% opacity."
                    _announce_build_result(result, report_path, "rebuilt")
        except Exception as error:
            rebuild_error = error
        finally:
            window.Show()
            window.Raise()
            refresh_status()
            if rebuild_error is not None:
                set_callback_error(rebuild_error)

    def on_key_press(event):
        key = _int_value(event.get("Key"), 0)
        text = str(event.get("Text", "") or "")
        if key == 16777234 or text == "[":
            navigate(-1)
        elif key == 16777236 or text == "]":
            navigate(1)

    def on_close(event):
        if ui_state["measuring"]:
            return
        window.Hide()
        dispatcher.ExitLoop()

    window.On["OverlayOn"].Clicked = on_overlay_on
    window.On["OverlayOff"].Clicked = on_overlay_off
    window.On["Toggle"].Clicked = on_toggle
    window.On["CompareMode"].CurrentIndexChanged = on_comparison_changed
    window.On["Opacity"].ValueChanged = on_comparison_changed
    window.On["Measure"].Clicked = measure_psnr
    window.On["Previous"].Clicked = on_previous
    window.On["Next"].Clicked = on_next
    window.On["Rebuild"].Clicked = on_rebuild
    window.On["Close"].Clicked = on_close
    window.On[REVIEW_WINDOW_ID].Close = on_close
    window.On[REVIEW_WINDOW_ID].KeyPress = on_key_press
    support.bind_footer(window, set_callback_error)

    support.register_window(REVIEW_WINDOW_ID, window)
    try:
        window.Show()
        refresh_status()
        items["Next"].SetFocus("OtherFocusReason")
        dispatcher.RunLoop()
        return 0
    finally:
        support.unregister_window(REVIEW_WINDOW_ID, window)
        window.Hide()


def _run_default_interactive(resolve_app, project, timeline):
    stale_staging = find_staging_track_indices(timeline, strict=True)
    if stale_staging:
        answer = macos_dialog(
            "Resolve contains {} incomplete reference build track(s) from an "
            "interrupted run. Rebuild will keep them disabled while creating "
            "the new overlay, then safely remove them after validation.".format(
                len(stale_staging)
            ),
            buttons=["Cancel", "Rebuild..."],
            default_button="Rebuild...",
            cancel_button="Cancel",
            caution=True,
        )
        if answer != "Rebuild...":
            return 0
        folder = choose_still_folder()
        if not folder:
            return 0
        _, result, report_path = build_from_folder(
            resolve_app, project, timeline, folder, assume_yes=False
        )
        if result:
            _announce_build_result(result, report_path, "rebuilt")
            show_review_controller(
                resolve_app, result.review_points, timeline
            )
        return 0

    for track_index in find_backup_track_indices(timeline, strict=True):
        if _safe_call(
            timeline, "SetTrackEnable", False,
            "video", track_index, False
        ) is False:
            raise StillOverlayError(
                "Could not disable an old reference backup track"
            )

    existing = find_managed_track_indices(timeline, strict=True)
    if existing:
        return show_review_controller(resolve_app)
    folder = choose_still_folder()
    if not folder:
        return 0
    _, result, report_path = build_from_folder(
        resolve_app, project, timeline, folder, assume_yes=False
    )
    if result:
        _announce_build_result(result, report_path, "built")
        show_review_controller(resolve_app, result.review_points, timeline)
    return 0


def run(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", help="Folder containing reference JPEGs")
    parser.add_argument("--audit-only", action="store_true", help="Audit without changing Resolve")
    parser.add_argument("--build", action="store_true", help="Build/rebuild overlay tracks")
    parser.add_argument("--toggle", action="store_true", help="Toggle all managed reference tracks")
    parser.add_argument("--show-reference", action="store_true", help="Show reference overlays")
    parser.add_argument("--show-camera", action="store_true", help="Hide all reference images")
    parser.add_argument("--difference", action="store_true", help="Show Difference composite mode")
    parser.add_argument("--remove", action="store_true", help="Remove managed reference tracks")
    parser.add_argument("--yes", action="store_true", help="Skip confirmations")
    parser.add_argument("--non-recursive", action="store_true", help="Do not scan JPEG subfolders")
    parser.add_argument("--notify", action="store_true", help=argparse.SUPPRESS)
    parsed, _ = parser.parse_known_args(args)

    actions = [
        parsed.audit_only, parsed.build, parsed.toggle, parsed.show_reference,
        parsed.show_camera, parsed.difference, parsed.remove,
    ]
    if sum(1 for value in actions if value) > 1:
        raise StillOverlayError("Choose only one build, audit, or control action")
    if parsed.folder and any((
        parsed.toggle, parsed.show_reference, parsed.show_camera,
        parsed.difference, parsed.remove,
    )):
        raise StillOverlayError(
            "--folder can only be used with --build or --audit-only"
        )

    resolve_app = connect_to_resolve()
    project, timeline = _current_context(resolve_app)

    if parsed.toggle or parsed.show_reference or parsed.show_camera or parsed.difference:
        stale_staging = find_staging_track_indices(timeline, strict=True)
        if stale_staging:
            raise StillOverlayError(
                "Incomplete reference build tracks exist; rebuild or remove them first"
            )
        backup_indices = find_backup_track_indices(timeline, strict=True)
        for track_index in backup_indices:
            if _safe_call(
                timeline, "SetTrackEnable", False,
                "video", track_index, False
            ) is False:
                raise StillOverlayError(
                    "Could not disable an old reference backup track"
                )
        indices = find_managed_track_indices(timeline, strict=True)
        if not parsed.show_camera and not managed_tracks_use_current_spec(
            timeline, indices
        ):
            raise StillOverlayError(
                "This timeline has a legacy full-clip reference lane; rebuild "
                "it before enabling or toggling the reference"
            )
        if parsed.toggle:
            enabled = toggle_reference_tracks(timeline, indices)
            message = "References {}".format("ON" if enabled else "OFF")
        elif parsed.show_reference:
            set_reference_difference(
                resolve_app, timeline, indices, False,
                show_reference=True
            )
            message = "References ON"
        elif parsed.show_camera:
            set_reference_visibility(timeline, indices, False)
            message = "References OFF"
        else:
            set_reference_difference(resolve_app, timeline, indices, True)
            message = "Reference Difference mode ON"
        print("{}: {}".format(SCRIPT_NAME, message))
        if parsed.notify:
            macos_notification(message)
        return 0

    if parsed.remove:
        indices = (
            find_managed_track_indices(timeline, strict=True)
            + find_staging_track_indices(timeline, strict=True)
            + find_backup_track_indices(timeline, strict=True)
        )
        if not indices:
            raise StillOverlayError(
                "No managed reference tracks exist on this timeline"
            )
        if not parsed.yes:
            if sys.platform != "darwin":
                raise StillOverlayError("Use --yes to confirm removal")
            answer = macos_dialog(
                "Remove {} managed reference track(s)?\n\n"
                "Imported JPEGs remain in the '{}' Media Pool bin. Source files "
                "are never deleted.".format(len(indices), MEDIA_POOL_FOLDER),
                buttons=["Cancel", "Remove"],
                default_button="Cancel",
                cancel_button="Cancel",
                caution=True,
            )
            if answer != "Remove":
                return 0
        removed = remove_managed_tracks(timeline)
        print("Removed {} managed reference track(s).".format(removed))
        return 0

    if parsed.folder or parsed.build or parsed.audit_only:
        folder = parsed.folder
        if not folder:
            if sys.platform != "darwin":
                raise StillOverlayError("Use --folder on non-macOS systems")
            folder = choose_still_folder()
        if not folder:
            return 0
        _, result, report_path = build_from_folder(
            resolve_app,
            project,
            timeline,
            folder,
            recursive=not parsed.non_recursive,
            audit_only=parsed.audit_only,
            assume_yes=parsed.yes,
        )
        if result and sys.platform == "darwin" and not parsed.yes:
            macos_dialog(_format_build_result(result, report_path))
        return 0

    if sys.platform != "darwin":
        raise StillOverlayError(
            "Use --folder to build or a control flag such as --toggle"
        )
    return _run_default_interactive(resolve_app, project, timeline)


def main(args=None):
    try:
        return run(args)
    except (StillOverlayError, OSError) as error:
        _show_error(error)
        return 1
    except Exception as error:
        _show_error("Unexpected error: {}".format(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
