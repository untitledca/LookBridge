#!/usr/bin/env python3
"""Match ASC CDL files to timeline source filenames and apply them in Resolve.

Designed to run from DaVinci Resolve's Workspace > Scripts menu. The
official Resolve API can write CDL values to an existing node but cannot create
or label color nodes. On macOS, this script uses Resolve's own Color menu for
that small missing piece, verifies the resulting clip graph, and uses the API
for the actual CDL write.
"""

from __future__ import print_function

import argparse
import datetime
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET


SCRIPT_NAME = "CDL Auto Apply"
NODE_LABEL = "CDL"
CDL_EXTENSIONS = {".cdl", ".cc", ".ccc"}
FILE_PROPERTY_NAMES = (
    "File Path", "File Name", "Filename", "Source File", "Source Filename",
    "Source File Name", "Source Path", "Original Filename", "Original File Name",
    "Original File Path", "Path",
)
DIRECT_FILE_PROPERTY_NAMES = (
    "File Path", "File Name", "Filename", "Source File", "Source Filename"
)
FALLBACK_NAME_PROPERTY_NAMES = ("Name", "Clip Name")
MAX_CDL_BYTES = 5 * 1024 * 1024
LOG_DIRECTORY = os.path.expanduser("~/Library/Logs/Resolve CDL Auto Apply")


class CDLParseError(Exception):
    pass


class ResolveError(Exception):
    pass


class NodeGraphModeError(ResolveError):
    pass


class NodeCleanupError(ResolveError):
    pass


class CDLValues(object):
    __slots__ = ("slope", "offset", "power", "saturation")

    def __init__(self, slope, offset, power, saturation):
        self.slope = tuple(slope)
        self.offset = tuple(offset)
        self.power = tuple(power)
        self.saturation = float(saturation)

    def as_resolve_map(self, node_index):
        return {
            "NodeIndex": str(int(node_index)),
            "Slope": _format_triplet(self.slope),
            "Offset": _format_triplet(self.offset),
            "Power": _format_triplet(self.power),
            "Saturation": _format_number(self.saturation),
        }

    def __eq__(self, other):
        return (
            isinstance(other, CDLValues)
            and self.slope == other.slope
            and self.offset == other.offset
            and self.power == other.power
            and self.saturation == other.saturation
        )


class CDLRecord(object):
    __slots__ = ("path", "values", "aliases")

    def __init__(self, path, values, aliases):
        self.path = os.path.abspath(path)
        self.values = values
        self.aliases = tuple(aliases)


class ClipMatch(object):
    __slots__ = (
        "item", "track_index", "start", "end", "uid", "display_name",
        "source_names", "source_keys", "cdl", "node_state", "node_index",
        "node_count", "node_error",
    )

    def __init__(self, item, track_index, start, end, uid, display_name,
                 source_names, source_keys, cdl):
        self.item = item
        self.track_index = int(track_index)
        self.start = int(start)
        self.end = int(end)
        self.uid = uid
        self.display_name = display_name
        self.source_names = tuple(source_names)
        self.source_keys = tuple(source_keys)
        self.cdl = cdl
        self.node_state = "unknown"
        self.node_index = None
        self.node_count = 0
        self.node_error = ""


def _format_number(value):
    return "{:.12g}".format(float(value))


def _format_triplet(values):
    return " ".join(_format_number(value) for value in values)


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


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


def cdl_aliases_for_path(path):
    filename = os.path.basename(path)
    stem, _ = os.path.splitext(filename)
    return tuple(alias for alias in (_normalized_key(stem),) if alias)


def source_keys_for_name(name):
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

    # Resolve may display an image sequence as name_[000001-000240].dpx.
    sequence_stem = re.sub(r"[_ .-]?\[\d+\s*-\s*\d+\]$", "", stem)
    if sequence_stem != stem:
        keys.append(_normalized_key(sequence_stem + extension))
        keys.append(_normalized_key(sequence_stem))

    # RED stores one camera clip in numbered R3D chunks such as
    # A001_C001_0101AB_001.R3D. Dailies CDLs commonly use the logical camera
    # clip name, A001_C001_0101AB.cdl. Keep the physical filename candidates
    # first, then add the logical RED clip name as a fallback.
    if extension.casefold() == ".r3d":
        red_clip_stem = re.sub(r"_\d{3,4}$", "", stem)
        if red_clip_stem != stem:
            keys.append(_normalized_key(red_clip_stem + extension))
            keys.append(_normalized_key(red_clip_stem))

    # Resolve may identify spanned RED media by its .RDC clip directory.
    if "/" in source_path:
        parent_name = source_path.rsplit("/", 1)[0].rsplit("/", 1)[-1].strip()
        parent_stem, parent_extension = os.path.splitext(parent_name)
        if parent_extension.casefold() == ".rdc":
            keys.append(_normalized_key(parent_stem))
    return [key for key in _unique(keys) if key]


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
    # Some camera formats expose all spanned source paths in one property.
    parts = [part.strip() for part in re.split(r"[;|\r\n]+", text) if part.strip()]
    return _unique(parts or [text])


def _property_key(value):
    return re.sub(r"[^a-z0-9]", "", _normalized_key(value))


def _append_source_names(names, value):
    names.extend(_source_strings(value))


def _parse_numbers(text, count, field_name, path):
    if text is None:
        raise CDLParseError("{} is missing {}".format(path, field_name))
    parts = text.replace(",", " ").split()
    if len(parts) != count:
        raise CDLParseError(
            "{} has {} {} value(s); expected {}".format(
                path, len(parts), field_name, count
            )
        )
    try:
        values = [float(part) for part in parts]
    except ValueError:
        raise CDLParseError("{} contains a non-numeric {}".format(path, field_name))
    if not all(math.isfinite(value) for value in values):
        raise CDLParseError("{} contains a non-finite {}".format(path, field_name))
    return values


def _descendant_text(element, local_name):
    for child in element.iter():
        if _local_name(child.tag) == local_name:
            return child.text
    return None


def _select_color_correction(root, path):
    corrections = [
        element for element in root.iter()
        if _local_name(element.tag) == "ColorCorrection"
    ]
    if not corrections:
        raise CDLParseError("{} contains no ASC ColorCorrection".format(path))
    if len(corrections) == 1:
        return corrections[0]

    wanted = set(cdl_aliases_for_path(path))
    matching = []
    for correction in corrections:
        correction_id = correction.attrib.get("id", "")
        id_keys = source_keys_for_name(correction_id)
        if wanted.intersection(id_keys):
            matching.append(correction)
    if len(matching) == 1:
        return matching[0]
    raise CDLParseError(
        "{} contains {} ColorCorrection entries and no unique id matching the filename".format(
            path, len(corrections)
        )
    )


def parse_cdl_file(path):
    try:
        size = os.path.getsize(path)
    except OSError as error:
        raise CDLParseError("Cannot read {}: {}".format(path, error))
    if size > MAX_CDL_BYTES:
        raise CDLParseError("{} is larger than 5 MB".format(path))
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise CDLParseError("Cannot parse {}: {}".format(path, error))

    correction = _select_color_correction(root, path)
    slope = _parse_numbers(_descendant_text(correction, "Slope"), 3, "Slope", path)
    offset = _parse_numbers(_descendant_text(correction, "Offset"), 3, "Offset", path)
    power = _parse_numbers(_descendant_text(correction, "Power"), 3, "Power", path)
    saturation = _parse_numbers(
        _descendant_text(correction, "Saturation"), 1, "Saturation", path
    )[0]
    return CDLValues(slope, offset, power, saturation)


def find_cdl_paths(folder, recursive=True):
    folder = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        raise CDLParseError("CDL folder does not exist: {}".format(folder))
    paths = []
    if recursive:
        for root, directories, filenames in os.walk(folder, followlinks=False):
            directories[:] = [name for name in directories if not name.startswith(".")]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                if os.path.splitext(filename)[1].casefold() in CDL_EXTENSIONS:
                    paths.append(os.path.join(root, filename))
    else:
        for filename in os.listdir(folder):
            path = os.path.join(folder, filename)
            if os.path.isfile(path) and os.path.splitext(filename)[1].casefold() in CDL_EXTENSIONS:
                paths.append(path)
    return sorted(paths, key=lambda value: _normalized_key(value))


def build_cdl_library(folder, recursive=True):
    records = []
    errors = []
    index = {}
    for path in find_cdl_paths(folder, recursive=recursive):
        try:
            record = CDLRecord(path, parse_cdl_file(path), cdl_aliases_for_path(path))
        except CDLParseError as error:
            errors.append(str(error))
            continue
        records.append(record)
        for alias in record.aliases:
            index.setdefault(alias, []).append(record)
    return records, index, errors


def match_cdl(source_keys, index):
    for key in source_keys:
        candidates = index.get(key, [])
        unique_candidates = {}
        for candidate in candidates:
            unique_candidates[candidate.path] = candidate
        candidates = list(unique_candidates.values())
        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1:
            return None, "Ambiguous CDL filename '{}': {}".format(
                key, ", ".join(sorted(candidate.path for candidate in candidates))
            )
    return None, ""


def _safe_call(obj, method_name, default=None, *args):
    try:
        method = getattr(obj, method_name)
        return method(*args)
    except Exception:
        return default


def get_clip_source_names(item):
    file_names = []
    fallback_names = []
    media_pool_item = _safe_call(item, "GetMediaPoolItem")
    if media_pool_item:
        properties = _safe_call(media_pool_item, "GetClipProperty", {}) or {}
        normalized_properties = {}
        if isinstance(properties, dict):
            normalized_properties = {
                _property_key(key): value for key, value in properties.items()
            }
        for property_name in FILE_PROPERTY_NAMES:
            _append_source_names(
                file_names, normalized_properties.get(_property_key(property_name))
            )

        # Resolve can return incomplete bulk property maps for camera originals,
        # so query the physical filename fields directly as well.
        for property_name in DIRECT_FILE_PROPERTY_NAMES:
            _append_source_names(
                file_names,
                _safe_call(media_pool_item, "GetClipProperty", None, property_name),
            )

        if file_names:
            return _unique(file_names)

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


def _clip_graph(item):
    try:
        return item.GetNodeGraph(1)
    except TypeError:
        return item.GetNodeGraph()
    except Exception:
        return None


def inspect_cdl_node(item):
    graph = _clip_graph(item)
    if not graph:
        return "error", None, 0, "Clip node graph is unavailable"
    try:
        node_count = int(graph.GetNumNodes())
    except Exception as error:
        return "error", None, 0, "Cannot inspect clip nodes: {}".format(error)
    matches = []
    for node_index in range(1, node_count + 1):
        try:
            label = graph.GetNodeLabel(node_index) or ""
        except Exception:
            label = ""
        if _normalized_key(label) == _normalized_key(NODE_LABEL):
            matches.append(node_index)
    if len(matches) == 1:
        return "existing", matches[0], node_count, ""
    if len(matches) > 1:
        return (
            "duplicate", None, node_count,
            "Multiple nodes are labeled '{}': {}".format(
                NODE_LABEL, ", ".join(str(value) for value in matches)
            ),
        )
    return "missing", None, node_count, ""


def audit_timeline(timeline, cdl_index, parse_errors=None):
    audit = {
        "matches": [],
        "unmatched": [],
        "ambiguous": [],
        "no_source": [],
        "parse_errors": list(parse_errors or []),
        "total_video_items": 0,
    }
    track_count = int(timeline.GetTrackCount("video") or 0)
    for track_index in range(1, track_count + 1):
        items = timeline.GetItemListInTrack("video", track_index) or []
        for item in items:
            audit["total_video_items"] += 1
            source_names = get_clip_source_names(item)
            keys = clip_source_keys(source_names)
            display_name = str(_safe_call(item, "GetName", "Unnamed clip") or "Unnamed clip")
            if not keys:
                audit["no_source"].append("V{}: {}".format(track_index, display_name))
                continue
            cdl, match_error = match_cdl(keys, cdl_index)
            if match_error:
                audit["ambiguous"].append(
                    "V{}: {} - {}".format(track_index, display_name, match_error)
                )
                continue
            if not cdl:
                audit["unmatched"].append(
                    "V{}: {} (sources: {}; filename keys: {})".format(
                        track_index,
                        display_name,
                        " | ".join(source_names),
                        ", ".join(keys),
                    )
                )
                continue
            clip_match = ClipMatch(
                item=item,
                track_index=track_index,
                start=int(_safe_call(item, "GetStart", 0) or 0),
                end=int(_safe_call(item, "GetEnd", 0) or 0),
                uid=str(_safe_call(item, "GetUniqueId", "") or ""),
                display_name=display_name,
                source_names=source_names,
                source_keys=keys,
                cdl=cdl,
            )
            state, node_index, node_count, node_error = inspect_cdl_node(item)
            clip_match.node_state = state
            clip_match.node_index = node_index
            clip_match.node_count = node_count
            clip_match.node_error = node_error
            audit["matches"].append(clip_match)
    return audit


def audit_counts(audit):
    counts = {
        "matched": len(audit["matches"]),
        "existing": 0,
        "missing": 0,
        "node_errors": 0,
        "unmatched": len(audit["unmatched"]),
        "ambiguous": len(audit["ambiguous"]),
        "parse_errors": len(audit["parse_errors"]),
        "no_source": len(audit["no_source"]),
    }
    for match in audit["matches"]:
        if match.node_state == "existing":
            counts["existing"] += 1
        elif match.node_state == "missing":
            counts["missing"] += 1
        else:
            counts["node_errors"] += 1
    return counts


def format_audit_report(audit, folder, project_name, timeline_name, resolve_version):
    counts = audit_counts(audit)
    lines = [
        SCRIPT_NAME,
        "Generated: {}".format(datetime.datetime.now().isoformat(timespec="seconds")),
        "Resolve: {}".format(resolve_version or "unknown"),
        "Project: {}".format(project_name),
        "Timeline: {}".format(timeline_name),
        "CDL folder: {}".format(folder),
        "",
        "AUDIT SUMMARY",
        "Video items scanned: {}".format(audit["total_video_items"]),
        "Filename matches: {}".format(counts["matched"]),
        "Existing CDL nodes: {}".format(counts["existing"]),
        "CDL nodes to append: {}".format(counts["missing"]),
        "Node errors: {}".format(counts["node_errors"]),
        "Unmatched clips: {}".format(counts["unmatched"]),
        "Ambiguous matches: {}".format(counts["ambiguous"]),
        "Invalid CDL files: {}".format(counts["parse_errors"]),
        "Items without source names: {}".format(counts["no_source"]),
        "",
        "MATCHES",
    ]
    for match in audit["matches"]:
        node_description = match.node_state
        if match.node_index:
            node_description += " node {}".format(match.node_index)
        if match.node_error:
            node_description += " ({})".format(match.node_error)
        lines.append(
            "V{} {} -> {} [{}]".format(
                match.track_index,
                match.display_name,
                match.cdl.path,
                node_description,
            )
        )
    for title, key in (
        ("AMBIGUOUS", "ambiguous"),
        ("INVALID CDL FILES", "parse_errors"),
        ("UNMATCHED", "unmatched"),
        ("NO SOURCE FILENAME", "no_source"),
    ):
        lines.extend(["", title])
        lines.extend(audit[key] or ["None"])
    return "\n".join(lines) + "\n"


def parse_timeline_rate(rate_value, start_timecode=""):
    text = str(rate_value or "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        raise ResolveError("Cannot determine timeline frame rate from '{}'".format(text))
    actual_rate = float(match.group(0))
    nominal_rate = int(round(actual_rate))
    drop_frame = "DF" in text.upper() or ";" in str(start_timecode)
    if drop_frame and nominal_rate not in (30, 60, 120):
        raise ResolveError("Drop-frame timecode is unsupported at {} fps".format(actual_rate))
    return actual_rate, nominal_rate, drop_frame


def timecode_to_frames(timecode, nominal_rate, drop_frame=False):
    parts = re.split(r"[:;]", str(timecode))
    if len(parts) != 4:
        raise ResolveError("Invalid timeline timecode '{}'".format(timecode))
    try:
        hours, minutes, seconds, frames = [int(part) for part in parts]
    except ValueError:
        raise ResolveError("Invalid timeline timecode '{}'".format(timecode))
    frame_number = ((hours * 3600 + minutes * 60 + seconds) * nominal_rate) + frames
    if drop_frame:
        drop_frames = int(round(nominal_rate * 0.0666666667))
        total_minutes = hours * 60 + minutes
        frame_number -= drop_frames * (total_minutes - total_minutes // 10)
    return frame_number


def frames_to_timecode(frame_number, nominal_rate, drop_frame=False):
    frame_number = int(frame_number)
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
            frame_number += drop_frames * ((remainder - drop_frames) // frames_per_minute)
    else:
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


def clip_midpoint_timecode(project, timeline, match):
    start_timecode = str(timeline.GetStartTimecode())
    rate_value = timeline.GetSetting("timelineFrameRate")
    if not rate_value:
        rate_value = project.GetSetting("timelineFrameRate")
    _, nominal_rate, drop_frame = parse_timeline_rate(rate_value, start_timecode)
    timeline_start_frame = int(timeline.GetStartFrame())
    midpoint = match.start
    if match.end > match.start:
        midpoint = match.start + max(0, (match.end - match.start - 1) // 2)
    start_tc_frames = timecode_to_frames(start_timecode, nominal_rate, drop_frame)
    target_tc_frames = start_tc_frames + (midpoint - timeline_start_frame)
    return frames_to_timecode(target_tc_frames, nominal_rate, drop_frame)


def _osascript(script, arguments=None):
    command = ["/usr/bin/osascript", "-e", script]
    command.extend(arguments or [])
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise ResolveError((stderr or stdout or "AppleScript failed").strip())
    return stdout.strip()


def choose_cdl_folder():
    script = r'''
try
    set chosenFolder to choose folder with prompt "Choose the folder containing ASC CDL files"
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
    return _osascript(script, [message, title])


COLOR_NODE_ACTION_SCRIPT = r'''
on firstExisting(targetMenu, candidateNames)
    tell application "System Events"
        repeat with candidateName in candidateNames
            set cleanName to contents of candidateName
            if exists menu item cleanName of targetMenu then return cleanName
        end repeat
    end tell
    return ""
end firstExisting

on run argv
    set requestedAction to item 1 of argv
    set nodeLabel to "CDL"
    if (count of argv) > 1 then set nodeLabel to item 2 of argv
    tell application "System Events"
        if exists process "DaVinci Resolve" then
            set processName to "DaVinci Resolve"
        else if exists process "Resolve" then
            set processName to "Resolve"
        else
            error "DaVinci Resolve process was not found"
        end if
        tell process processName
            set frontmost to true
            if not (exists menu bar item "Color" of menu bar 1) then error "Color menu was not found; use the English Resolve interface"
            set colorMenu to menu 1 of menu bar item "Color" of menu bar 1
            if not (exists menu item "Nodes" of colorMenu) then error "Color > Nodes menu was not found"
            set nodesMenu to menu 1 of menu item "Nodes" of colorMenu
            set appendName to my firstExisting(nodesMenu, {"Append Node", "Append a Node", "Append Serial Node"})
            set labelName to my firstExisting(nodesMenu, {"Label Selected Node", "Node Label", "Change Label"})
            set deleteName to my firstExisting(nodesMenu, {"Delete Current Node", "Delete Selected Node", "Delete Node"})
            if requestedAction is "check" then
                if appendName is "" then error "Color > Nodes > Append Node was not found"
                if labelName is "" then error "Color > Nodes > Label Selected Node was not found"
                if deleteName is "" then error "Color > Nodes > Delete Current Node was not found"
                return appendName & "|" & labelName & "|" & deleteName
            else if requestedAction is "append" then
                if appendName is "" then error "Append Node command was not found"
                if not (enabled of menu item appendName of nodesMenu) then error "Append Node command is disabled for the current Color graph"
                click menu item appendName of nodesMenu
                return "APPENDED"
            else if requestedAction is "label" then
                if labelName is "" then error "Label Selected Node command was not found"
                if not (enabled of menu item labelName of nodesMenu) then error "Label Selected Node command is disabled"
                click menu item labelName of nodesMenu
                delay 0.15
                keystroke nodeLabel
                key code 36
                return "LABELED"
            else if requestedAction is "delete" then
                if deleteName is "" then error "Delete Current Node command was not found"
                if not (enabled of menu item deleteName of nodesMenu) then error "Delete Current Node command is disabled"
                click menu item deleteName of nodesMenu
                return "DELETED"
            else
                error "Unknown color node action"
            end if
        end tell
    end tell
end run
'''


def check_color_node_automation():
    if sys.platform != "darwin":
        raise ResolveError("Automatic node creation is currently supported on macOS only")
    return _osascript(COLOR_NODE_ACTION_SCRIPT, ["check", NODE_LABEL])


def color_node_action(action):
    return _osascript(COLOR_NODE_ACTION_SCRIPT, [action, NODE_LABEL])


def _items_match(left, right):
    if not left or not right:
        return False
    left_uid = str(_safe_call(left, "GetUniqueId", "") or "")
    right_uid = str(_safe_call(right, "GetUniqueId", "") or "")
    if left_uid and right_uid:
        return left_uid == right_uid
    if left is right:
        return True
    return (
        _safe_call(left, "GetName", "") == _safe_call(right, "GetName", "")
        and int(_safe_call(left, "GetStart", -1) or -1)
        == int(_safe_call(right, "GetStart", -2) or -2)
    )


def _wait_for_current_item(timeline, expected_item, timeout=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current_item = _safe_call(timeline, "GetCurrentVideoItem")
        if _items_match(current_item, expected_item):
            return True
        time.sleep(0.08)
    return False


def _set_track_state(timeline, track_index, enabled):
    result = timeline.SetTrackEnable("video", int(track_index), bool(enabled))
    return result is not False


def select_clip_for_color(project, timeline, match):
    target_timecode = clip_midpoint_timecode(project, timeline, match)
    if not timeline.SetCurrentTimecode(target_timecode):
        raise ResolveError("Could not move playhead to {}".format(target_timecode))
    if _wait_for_current_item(timeline, match.item):
        return lambda: None

    track_count = int(timeline.GetTrackCount("video") or 0)
    original_states = {}
    changed_tracks = []
    for track_index in range(1, track_count + 1):
        original_states[track_index] = bool(
            timeline.GetIsTrackEnabled("video", track_index)
        )
    try:
        if not original_states.get(match.track_index, True):
            if not _set_track_state(timeline, match.track_index, True):
                raise ResolveError("Could not temporarily enable V{}".format(match.track_index))
            changed_tracks.append(match.track_index)
        for track_index in range(match.track_index + 1, track_count + 1):
            if original_states.get(track_index, False):
                if not _set_track_state(timeline, track_index, False):
                    raise ResolveError("Could not temporarily hide V{}".format(track_index))
                changed_tracks.append(track_index)
        if not timeline.SetCurrentTimecode(target_timecode):
            raise ResolveError("Could not refresh playhead at {}".format(target_timecode))
        if not _wait_for_current_item(timeline, match.item):
            raise ResolveError(
                "Resolve selected a different clip at {} even after isolating V{}".format(
                    target_timecode, match.track_index
                )
            )
    except Exception:
        for track_index in reversed(changed_tracks):
            _set_track_state(timeline, track_index, original_states[track_index])
        raise

    restored = [False]

    def restore():
        if restored[0]:
            return
        restored[0] = True
        for track_index in reversed(changed_tracks):
            _set_track_state(timeline, track_index, original_states[track_index])

    return restore


def _wait_for_new_cdl_node(item, original_count, timeout=1.5):
    deadline = time.time() + timeout
    last_state = ("missing", None, original_count, "")
    while time.time() < deadline:
        last_state = inspect_cdl_node(item)
        state, node_index, node_count, _ = last_state
        if state == "existing" and node_count == original_count + 1:
            return last_state
        time.sleep(0.08)
    return last_state


def _wait_for_node_count(item, expected_count, timeout=1.0):
    deadline = time.time() + timeout
    last_count = None
    while time.time() < deadline:
        graph = _clip_graph(item)
        if graph:
            try:
                last_count = int(graph.GetNumNodes())
            except Exception:
                last_count = None
        if last_count == expected_count:
            return True
        time.sleep(0.08)
    return False


def create_verified_cdl_node(item, original_count):
    appended = False
    try:
        color_node_action("append")
        appended = True
        if not _wait_for_node_count(item, original_count + 1):
            raise NodeGraphModeError(
                "The Color page was not showing this clip's Clip - L1 graph"
            )
        color_node_action("label")
        state, node_index, node_count, error = _wait_for_new_cdl_node(
            item, original_count
        )
        if state == "existing" and node_count == original_count + 1:
            return node_index
        raise ResolveError(error or "The appended node could not be verified as 'CDL'")
    except Exception as original_error:
        if appended:
            try:
                color_node_action("delete")
                time.sleep(0.1)
            except Exception as cleanup_error:
                raise NodeCleanupError(
                    "{}; automatic cleanup also failed: {}. Inspect the active Color graph before continuing".format(
                        original_error, cleanup_error
                    )
                )
        raise original_error


def apply_cdl(item, node_index, values):
    try:
        result = item.SetCDL(values.as_resolve_map(node_index))
    except Exception as error:
        raise ResolveError("SetCDL failed: {}".format(error))
    if not result:
        raise ResolveError("Resolve rejected SetCDL for node {}".format(node_index))


def apply_audit(resolve, project, timeline, audit, create_missing_nodes=True):
    results = []
    original_page = _safe_call(resolve, "GetCurrentPage", "") or ""
    original_timecode = _safe_call(timeline, "GetCurrentTimecode", "") or ""
    missing_count = sum(1 for match in audit["matches"] if match.node_state == "missing")
    automation_error = ""
    if create_missing_nodes and missing_count:
        if not resolve.OpenPage("color"):
            automation_error = "Could not open Resolve's Color page"
        else:
            time.sleep(0.35)
            try:
                check_color_node_automation()
            except Exception as error:
                automation_error = str(error)

    try:
        for match in audit["matches"]:
            state, node_index, node_count, node_error = inspect_cdl_node(match.item)
            if state in ("duplicate", "error"):
                results.append(("SKIPPED", match, node_error or state))
                continue
            if state == "missing":
                if not create_missing_nodes:
                    results.append(("SKIPPED", match, "CDL node is missing"))
                    continue
                if automation_error:
                    results.append(("SKIPPED", match, automation_error))
                    continue
                restore_tracks = lambda: None
                try:
                    restore_tracks = select_clip_for_color(project, timeline, match)
                    node_index = create_verified_cdl_node(match.item, node_count)
                except Exception as error:
                    results.append(("FAILED", match, "Node creation: {}".format(error)))
                    if isinstance(error, (NodeGraphModeError, NodeCleanupError)):
                        automation_error = str(error)
                    continue
                finally:
                    restore_tracks()
            try:
                apply_cdl(match.item, node_index, match.cdl.values)
                results.append(("APPLIED", match, "node {}".format(node_index)))
            except Exception as error:
                results.append(("FAILED", match, str(error)))
    finally:
        if original_timecode:
            _safe_call(timeline, "SetCurrentTimecode", False, original_timecode)
        if original_page and original_page != _safe_call(resolve, "GetCurrentPage", ""):
            _safe_call(resolve, "OpenPage", False, original_page)
    return results


def format_apply_results(results):
    lines = ["", "APPLY RESULTS"]
    for status, match, detail in results:
        lines.append(
            "{} V{} {} -> {} ({})".format(
                status,
                match.track_index,
                match.display_name,
                os.path.basename(match.cdl.path),
                detail,
            )
        )
    counts = {"APPLIED": 0, "FAILED": 0, "SKIPPED": 0}
    for status, _, _ in results:
        counts[status] = counts.get(status, 0) + 1
    lines.extend([
        "",
        "Applied: {}".format(counts["APPLIED"]),
        "Failed: {}".format(counts["FAILED"]),
        "Skipped: {}".format(counts["SKIPPED"]),
        "Project save was not forced by the script.",
    ])
    return "\n".join(lines) + "\n", counts


def _slug(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    return value or "Resolve"


def create_report_path(project_name, timeline_name):
    if not os.path.isdir(LOG_DIRECTORY):
        os.makedirs(LOG_DIRECTORY)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = "{}-{}-{}.log".format(
        _slug(project_name), _slug(timeline_name), timestamp
    )
    return os.path.join(LOG_DIRECTORY, filename)


def write_report(path, content):
    with open(path, "w", encoding="utf-8") as report_file:
        report_file.write(content)


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
        raise ResolveError("Cannot import DaVinciResolveScript: {}".format(error))
    resolve_app = dvr_script.scriptapp("Resolve")
    if not resolve_app:
        raise ResolveError("DaVinci Resolve is not running or scripting access is disabled")
    return resolve_app


def _show_error(message):
    print("{}: {}".format(SCRIPT_NAME, message))
    if sys.platform == "darwin":
        try:
            macos_dialog(str(message), caution=True)
        except Exception:
            pass


def _confirmation_message(audit, report_path):
    counts = audit_counts(audit)
    return "\n".join([
        "Audit complete for the current timeline.",
        "",
        "Matched clips: {}".format(counts["matched"]),
        "Existing CDL nodes to update: {}".format(counts["existing"]),
        "CDL nodes to append at Clip - L1: {}".format(counts["missing"]),
        "Ambiguous/invalid/node errors: {}".format(
            counts["ambiguous"] + counts["parse_errors"] + counts["node_errors"]
        ),
        "Unmatched clips: {}".format(counts["unmatched"]),
        "",
        "Before applying, the Color node graph selector must be Clip - L1.",
        "Existing nodes are not reset or replaced. The project is not saved automatically.",
        "",
        "Audit report: {}".format(report_path),
    ])


def run(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", help="Folder containing .cdl/.cc/.ccc files")
    parser.add_argument("--audit-only", action="store_true", help="Do not apply grades")
    parser.add_argument("--apply", action="store_true", help="Apply without interactive mode")
    parser.add_argument("--yes", action="store_true", help="Skip apply confirmation")
    parser.add_argument("--no-create-nodes", action="store_true", help="Only update existing CDL nodes")
    parser.add_argument("--non-recursive", action="store_true", help="Do not scan CDL subfolders")
    parsed, _ = parser.parse_known_args(args)

    resolve_app = connect_to_resolve()
    project_manager = resolve_app.GetProjectManager()
    project = project_manager.GetCurrentProject() if project_manager else None
    if not project:
        raise ResolveError("Open a Resolve project first")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise ResolveError("Open the locked timeline you want to process first")

    folder = parsed.folder
    if not folder:
        if sys.platform != "darwin":
            raise ResolveError("Use --folder on non-macOS systems")
        folder = choose_cdl_folder()
    if not folder:
        return 0
    folder = os.path.abspath(os.path.expanduser(folder))

    records, index, parse_errors = build_cdl_library(
        folder, recursive=not parsed.non_recursive
    )
    if not records:
        raise ResolveError(
            "No valid .cdl, .cc, or .ccc files were found in {}".format(folder)
        )
    audit = audit_timeline(timeline, index, parse_errors=parse_errors)
    project_name = str(project.GetName() or "Untitled Project")
    timeline_name = str(timeline.GetName() or "Untitled Timeline")
    resolve_version = str(_safe_call(resolve_app, "GetVersionString", "") or "")
    report_path = create_report_path(project_name, timeline_name)
    audit_report = format_audit_report(
        audit, folder, project_name, timeline_name, resolve_version
    )
    write_report(report_path, audit_report)
    print(audit_report)
    print("Audit report: {}".format(report_path))

    if parsed.audit_only:
        if sys.platform == "darwin" and not parsed.yes:
            macos_dialog(_confirmation_message(audit, report_path), buttons=["OK"])
        return 0
    if not audit["matches"]:
        raise ResolveError("No timeline source filenames matched the CDL filenames")

    should_apply = parsed.apply or parsed.yes
    if not should_apply:
        answer = macos_dialog(
            _confirmation_message(audit, report_path),
            buttons=["Cancel", "Apply"],
            default_button="Cancel",
            cancel_button="Cancel",
            caution=True,
        )
        should_apply = answer == "Apply"
    if not should_apply:
        return 0

    results = apply_audit(
        resolve_app,
        project,
        timeline,
        audit,
        create_missing_nodes=not parsed.no_create_nodes,
    )
    result_report, counts = format_apply_results(results)
    write_report(report_path, audit_report + result_report)
    print(result_report)
    summary = "Applied: {}\nFailed: {}\nSkipped: {}\n\nReport: {}".format(
        counts["APPLIED"], counts["FAILED"], counts["SKIPPED"], report_path
    )
    if sys.platform == "darwin" and not parsed.yes:
        macos_dialog(summary, caution=bool(counts["FAILED"] or counts["SKIPPED"]))
    return 0 if not counts["FAILED"] else 2


def main(args=None):
    try:
        return run(args)
    except (CDLParseError, ResolveError, OSError) as error:
        _show_error(error)
        return 1
    except Exception as error:
        _show_error("Unexpected error: {}".format(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
