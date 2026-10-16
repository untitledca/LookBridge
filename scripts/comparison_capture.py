"""Transactional PSNR capture of the same two images used for Resolve A/B.

No image alignment, exposure correction, or camera-frame cropping is performed:
the score describes the actual Resolve output, including any mismatch there.
The public function receives the main script module to avoid import assumptions
in Resolve's embedded Python environment.
"""

from __future__ import print_function

import math
import os
import shutil
import tempfile
import time


def _get(obj, method, *args):
    value = getattr(obj, method)(*args)
    if value is None:
        raise RuntimeError("Resolve did not expose {}".format(method))
    return value


def _same_timeline(project, timeline):
    current = _get(project, "GetCurrentTimeline")
    return str(_get(current, "GetUniqueId")) == str(
        _get(timeline, "GetUniqueId")
    )


def _set_track_state(timeline, track_type, index, state):
    result = timeline.SetTrackEnable(track_type, index, bool(state))
    actual = _get(timeline, "GetIsTrackEnabled", track_type, index)
    if result is False or bool(actual) != bool(state):
        raise RuntimeError("Cannot set {} track {} visibility".format(
            track_type, index
        ))


def _set_lock(timeline, index, state):
    result = timeline.SetTrackLock("video", index, bool(state))
    actual = _get(timeline, "GetIsTrackLocked", "video", index)
    if result is False or bool(actual) != bool(state):
        raise RuntimeError("Cannot set V{} lock".format(index))


def _set_property(item, name, value):
    result = item.SetProperty(name, value)
    actual = _get(item, "GetProperty", name)
    if result is False or abs(float(actual) - float(value)) > 0.001:
        raise RuntimeError("Cannot set reference {}".format(name))


def _isolate(timeline, states, selected_track):
    for (track_type, index), unused in sorted(states.items()):
        desired = track_type == "video" and index == selected_track
        if bool(_get(timeline, "GetIsTrackEnabled", track_type, index)) != desired:
            _set_track_state(timeline, track_type, index, desired)


def _export(project, timeline, target_tc, path):
    if not _same_timeline(project, timeline):
        raise RuntimeError("The current timeline changed during PSNR capture")
    result = timeline.SetCurrentTimecode(target_tc)
    if result is False or str(_get(timeline, "GetCurrentTimecode")) != target_tc:
        raise RuntimeError("Cannot seek to the reference frame for PSNR")
    # Readback validates logical state; allow the render graph to catch up too.
    time.sleep(0.1)
    if project.ExportCurrentFrameAsStill(path) is False:
        raise RuntimeError("Resolve frame export is unavailable for PSNR")
    for unused in range(20):
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            break
        time.sleep(0.05)
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise RuntimeError("Resolve did not create the PSNR frame export")
    if not _same_timeline(project, timeline):
        raise RuntimeError("The current timeline changed during PSNR capture")
    if str(_get(timeline, "GetCurrentTimecode")) != target_tc:
        raise RuntimeError("The playhead moved during PSNR capture")


def _locate_pair(timeline, track_indices, point, nav):
    if not bool(getattr(point, "mapped", False)):
        raise RuntimeError("This grab has no mapped source frame for PSNR")
    source_uid = str(getattr(point, "match_uid", "") or "")
    if not source_uid:
        raise RuntimeError("This marker lacks source identity; rebuild references")
    absolute_frame = int(_get(timeline, "GetStartFrame")) + int(point.frame_id)
    managed = set(int(index) for index in track_indices)
    expected_name = (str(nav.ITEM_PREFIX) + str(point.filename)).casefold()
    source_candidates = []
    reference_candidates = []
    for index in range(1, int(_get(timeline, "GetTrackCount", "video")) + 1):
        if index in managed:
            valid, reason = nav.validate_managed_track(timeline, index)
            if not valid:
                raise RuntimeError("Cannot measure unverified reference: " + reason)
        for item in _get(timeline, "GetItemListInTrack", "video", index):
            start = int(_get(item, "GetStart"))
            end = int(_get(item, "GetEnd"))
            if index in managed:
                if (start == absolute_frame and end == absolute_frame + 1
                        and str(_get(item, "GetName")).casefold() == expected_name):
                    reference_candidates.append((index, item))
            elif (start <= absolute_frame < end
                  and str(_get(item, "GetUniqueId")) == source_uid):
                source_candidates.append((index, item))
    if len(source_candidates) != 1:
        raise RuntimeError("The marker's source clip is missing or ambiguous")
    source_index = source_candidates[0][0]
    reference_candidates = [pair for pair in reference_candidates
                            if pair[0] == source_index + 1]
    if len(reference_candidates) != 1:
        raise RuntimeError("No unique one-frame reference is above this source clip")
    if not bool(_get(timeline, "GetIsTrackEnabled", "video", source_index)):
        raise RuntimeError("The camera source track is disabled")
    reference_index, reference_item = reference_candidates[0]
    return source_index, reference_index, reference_item


def calculate_review_psnr(resolve_app, project, timeline, track_indices, point,
                          image_engine, navigation_module):
    """Measure two isolated Resolve PNG exports, restoring the complete UI state.

    Returns the image engine's score dictionary augmented with ``warnings``,
    ``frame_id``, ``timecode``, and track indices. Raises StillOverlayError when
    PSNR is unavailable, and ReviewStateRestoreError if restoration is unsafe.
    Callers may fall back to Difference only for the former.
    """
    nav = navigation_module
    try:
        if not _same_timeline(project, timeline):
            raise RuntimeError("The selected project/timeline is no longer current")
        source_index, reference_index, item = _locate_pair(
            timeline, track_indices, point, nav
        )
        original_tc = str(_get(timeline, "GetCurrentTimecode"))
        original_page = str(_get(resolve_app, "GetCurrentPage"))
        if not original_tc or not original_page:
            raise RuntimeError("Resolve did not expose restorable page/playhead state")
        target_tc = nav.review_frame_to_timecode(project, timeline, point.frame_id)
        states = {}
        for track_type in ("video", "subtitle"):
            for index in range(1, int(_get(timeline, "GetTrackCount", track_type)) + 1):
                states[(track_type, index)] = bool(_get(
                    timeline, "GetIsTrackEnabled", track_type, index
                ))
        original_lock = bool(_get(timeline, "GetIsTrackLocked", "video", reference_index))
        properties = {name: _get(item, "GetProperty", name)
                      for name in ("CompositeMode", "Opacity")}
        for name, value in properties.items():
            if not math.isfinite(float(value)):
                raise RuntimeError("Unusable reference {} state".format(name))
        composite_enabled = item.GetProperty("CompositeEnabled")
        if isinstance(composite_enabled, (bool, int, float)):
            properties["CompositeEnabled"] = composite_enabled
    except Exception as error:
        raise nav.StillOverlayError("PSNR unavailable: {}".format(error))

    directory = tempfile.mkdtemp(prefix="resolve-reference-psnr-")
    source_path = os.path.join(directory, "camera.png")
    reference_path = os.path.join(directory, "reference.png")
    result = None
    pending_error = None
    restore_failures = []
    try:
        if original_page != "edit":
            opened = resolve_app.OpenPage("edit")
            if opened is False or str(_get(resolve_app, "GetCurrentPage")) != "edit":
                raise RuntimeError("Cannot open the Edit page for PSNR export")
        _isolate(timeline, states, source_index)
        _export(project, timeline, target_tc, source_path)

        if bool(_get(timeline, "GetIsTrackLocked", "video", reference_index)):
            _set_lock(timeline, reference_index, False)
        _set_property(item, "CompositeMode", 0)
        _set_property(item, "Opacity", 100.0)
        if "CompositeEnabled" in properties:
            _set_property(item, "CompositeEnabled", True)
        _isolate(timeline, states, reference_index)
        _export(project, timeline, target_tc, reference_path)
        result = image_engine.calculate_psnr(source_path, reference_path)
        if not isinstance(result, dict):
            raise RuntimeError("The PSNR engine returned an unsupported result")
        result = dict(result)
        warnings = list(result.get("warnings", []))
        warnings.append(
            "RGB8 Resolve-output comparison, not proof of CDL equivalence. "
            "Geometry, frame alignment, JPEG compression, color management, "
            "burn-ins, and output transforms can affect the score."
        )
        if not bool(getattr(point, "exact", False)):
            warnings.append("The source frame is metadata-mapped, not visually verified: {}".format(
                getattr(point, "reason", "") or "verify alignment before interpreting PSNR"
            ))
        result.update({
            "metric": "RGB8 PSNR (Resolve output)", "warnings": warnings,
            "frame_id": int(point.frame_id), "timecode": target_tc,
            "source_track": source_index, "reference_track": reference_index,
        })
    except Exception as error:
        pending_error = error
    finally:
        # Restore even after a setter reports failure: some Resolve operations
        # can partially succeed before returning an error.
        try:
            if bool(_get(timeline, "GetIsTrackLocked", "video", reference_index)):
                _set_lock(timeline, reference_index, False)
        except Exception as error:
            restore_failures.append("unlock reference: {}".format(error))
        for name, value in properties.items():
            try:
                _set_property(item, name, value)
            except Exception as error:
                restore_failures.append("restore {}: {}".format(name, error))
        try:
            _set_lock(timeline, reference_index, original_lock)
        except Exception as error:
            restore_failures.append("restore reference lock: {}".format(error))
        for (track_type, index), state in sorted(states.items(), reverse=True):
            try:
                if bool(_get(timeline, "GetIsTrackEnabled", track_type, index)) != state:
                    _set_track_state(timeline, track_type, index, state)
            except Exception as error:
                restore_failures.append("restore {} {}: {}".format(track_type, index, error))
        try:
            if timeline.SetCurrentTimecode(original_tc) is False or str(
                    _get(timeline, "GetCurrentTimecode")) != original_tc:
                raise RuntimeError("playhead readback failed")
        except Exception as error:
            restore_failures.append("restore playhead: {}".format(error))
        try:
            if str(_get(resolve_app, "GetCurrentPage")) != original_page:
                if resolve_app.OpenPage(original_page) is False:
                    raise RuntimeError("page restore was rejected")
            if str(_get(resolve_app, "GetCurrentPage")) != original_page:
                raise RuntimeError("page readback failed")
        except Exception as error:
            restore_failures.append("restore page: {}".format(error))
        shutil.rmtree(directory, ignore_errors=True)
    if restore_failures:
        raise nav.ReviewStateRestoreError(
            "PSNR stopped because Resolve could not restore state: {}".format(
                "; ".join(restore_failures)
            )
        )
    if pending_error:
        raise nav.StillOverlayError("PSNR unavailable: {}".format(pending_error))
    return result
