"""Bounded offline TCX parser returning the existing GPX parser record shape.

The historical luma-ride-gpx/1 schema tag describes the shared parser record;
source.kind distinguishes TCX. This is not the full persisted review draft.
No source summary totals are substituted for calculated continuous-edge totals.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ride_gpx import (
    GPXError, MAX_FILE_BYTES, MAX_POINTS, MAX_GAP_S,
    _safe_tree, _split_tag, _number, _timestamp, _distance, _metric,
)

TCX_NAMESPACES = ("", "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2")
POWER_NAMESPACES = (
    "", "http://www.garmin.com/xmlschemas/ActivityExtension/v1",
    "http://www.garmin.com/xmlschemas/ActivityExtension/v2",
)


class TCXError(ValueError):
    """Machine-readable rejection without source XML or local paths."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _tracks(root, q):
    # Explicit paths only: Trackpoint-like metadata is not track evidence.
    # Preserve document order across activities, laps, courses and tracks.
    for container in root:
        if container.tag == q("Activities"):
            for activity in container.findall(q("Activity")):
                for lap in activity.findall(q("Lap")):
                    yield from lap.findall(q("Track"))
        elif container.tag == q("Courses"):
            for course in container.findall(q("Course")):
                yield from course.findall(q("Track"))


def _metrics(segments, datetimes):
    """Same edge exclusion and weighted-mean policy as ride_gpx/1."""
    flat = [point for segment in segments for point in segment["points"]]
    has_position = any(p["lat"] is not None and p["lon"] is not None for p in flat)
    time_by_point = dict(zip(((p["segment_index"], p["point_index"]) for p in flat), datetimes))
    warnings = []
    distance = duration = ascent = 0.0
    possible = timed = coordinate_edges = elevation_edges = 0
    for segment in segments:
        si = segment["segment_index"]
        for previous, current in zip(segment["points"], segment["points"][1:]):
            possible += 1
            ta = time_by_point[(si, previous["point_index"])]
            tb = time_by_point[(si, current["point_index"])]
            reason = None
            if ta is None or tb is None:
                reason = "missing_time_boundary"
            else:
                seconds = (tb - ta).total_seconds()
                if seconds <= 0:
                    reason = "non_increasing_time"
                elif seconds > MAX_GAP_S:
                    reason = "time_gap_boundary"
            if reason:
                warnings.append({"code": reason, "segment_index": si,
                                 "point_index": current["point_index"], "field": "time"})
                continue
            duration += seconds
            timed += 1
            if all(p[f] is not None for p in (previous, current) for f in ("lat", "lon")):
                distance += _distance(previous, current)
                coordinate_edges += 1
            else:
                warnings.append({"code": "missing_coordinate_boundary", "segment_index": si,
                                 "point_index": current["point_index"], "field": None})
            if previous["ele"] is not None and current["ele"] is not None:
                ascent += max(0.0, current["ele"] - previous["ele"])
                elevation_edges += 1
    coverage = {"possible_edges": possible, "timed_edges": timed,
                "excluded_time_edges": possible - timed, "declared_segments": len(segments),
                "max_gap_s": MAX_GAP_S}
    metrics = {
        "total_distance_m": _metric(distance if coordinate_edges else None, "m", "haversine_valid_edges/1",
                                    None if coordinate_edges else ("no_valid_positions" if not has_position else "insufficient_coordinates_or_time"),
                                    **coverage, used_edges=coordinate_edges),
        "total_duration_s": _metric(duration if timed else None, "s", "sum_continuous_intervals/1",
                                    None if timed else "insufficient_valid_time_intervals",
                                    **coverage, used_edges=timed),
        "elevation_gain_m": _metric(ascent if elevation_edges else None, "m", "sum_positive_raw_deltas/1",
                                    None if elevation_edges else "insufficient_elevation_or_time",
                                    **coverage, used_edges=elevation_edges),
    }
    for field, output, unit in (("heart_rate_bpm", "average_heart_rate_bpm", "bpm"),
                                ("power_w", "average_power_w", "W")):
        reason = None
        if len(flat) < 2:
            reason = "insufficient_samples"
        elif len(segments) != 1:
            reason = "discontinuous_track"
        elif any(t is None for t in datetimes):
            reason = "incomplete_time_sequence"
        elif timed != possible:
            reason = "invalid_or_gapped_time_sequence"
        elif any(p[field] is None for p in flat):
            reason = "incomplete_sensor_sequence"
        value = None
        if reason is None:
            integral = sum((a[field] + b[field]) / 2 * (tb - ta).total_seconds()
                           for a, b, ta, tb in zip(flat, flat[1:], datetimes, datetimes[1:]))
            value = integral / duration
        metrics[output] = _metric(value, unit, "trapezoidal_time_weighted/1", reason,
                                  **coverage, sample_count=len(flat),
                                  valid_sensor_samples=sum(p[field] is not None for p in flat))
    return metrics, warnings


def parse_tcx(data):
    """Parse original bytes, retaining missing fields and original point order.

    Each Track is a segment. Missing/reversed/duplicate times and >300 s gaps
    exclude that edge from totals. Sensor means require one complete segment.
    Safe XML primitives are shared with GPX; TCX point count is also bounded.
    """
    if not isinstance(data, bytes):
        raise TCXError("invalid_input", "TCX input must be bytes")
    if len(data) > MAX_FILE_BYTES:
        raise TCXError("file_too_large", "TCX exceeds 5 MiB")
    if not data:
        raise TCXError("empty_input", "TCX input is empty")
    source = {"kind": "tcx", "sha256": hashlib.sha256(data).hexdigest(),
              "hash_scope": "raw_file_bytes", "byte_length": len(data)}
    try:
        root = _safe_tree(data)
    except GPXError as exc:
        raise TCXError(exc.code, str(exc)) from None
    namespace, name = _split_tag(root.tag)
    if name != "TrainingCenterDatabase" or namespace not in TCX_NAMESPACES:
        raise TCXError("invalid_tcx", "Expected a TCX v2 root namespace")
    prefix = "{" + namespace + "}" if namespace else ""
    q = lambda local: prefix + local
    segments, datetimes, warnings = [], [], []
    point_count = 0
    observed_optional_fields = set()

    def warn(code, si, pi=None, field=None):
        warnings.append({"code": code, "segment_index": si, "point_index": pi, "field": field})

    for track in _tracks(root, q):
        si = len(segments)
        points = []
        for pi, element in enumerate(track.findall(q("Trackpoint"))):
            point_count += 1
            if point_count > MAX_POINTS:
                raise TCXError("too_many_points", "Track point limit exceeded")
            point = {"segment_index": si, "point_index": pi, "time": None,
                     "lat": None, "lon": None, "ele": None,
                     "heart_rate_bpm": None, "power_w": None, "cadence_rpm": None}
            positions = element.findall(q("Position"))
            heart_rates = element.findall(q("HeartRateBpm"))
            values = {
                "time": element.findall(q("Time")),
                "ele": element.findall(q("AltitudeMeters")),
                "lat": [n for p in positions for n in p.findall(q("LatitudeDegrees"))],
                "lon": [n for p in positions for n in p.findall(q("LongitudeDegrees"))],
                "heart_rate_bpm": [n for h in heart_rates for n in h.findall(q("Value"))],
                "power_w": element.findall(q("Watts")),
                "cadence_rpm": element.findall(q("Cadence")),
            }
            for extension in element.findall(q("Extensions")):
                for node in extension.iter():
                    sensor_ns, local = _split_tag(node.tag)
                    if local == "Watts" and sensor_ns in (*POWER_NAMESPACES, namespace):
                        values["power_w"].append(node)
            for field, nodes in values.items():
                if field in ("ele", "heart_rate_bpm", "power_w", "cadence_rpm") and nodes:
                    observed_optional_fields.add(field)
                ambiguous = len(nodes) > 1 or (field in ("lat", "lon") and len(positions) > 1) or (
                    field == "heart_rate_bpm" and len(heart_rates) > 1)
                raw = nodes[0].text if len(nodes) == 1 and not ambiguous else None
                if field == "time":
                    dt = _timestamp(raw)
                    datetimes.append(dt)
                    point[field] = dt.isoformat().replace("+00:00", "Z") if dt else None
                else:
                    minimum, maximum = {"lat": (-90, 90), "lon": (-180, 180),
                                        "heart_rate_bpm": (0, None), "power_w": (0, None),
                                        "cadence_rpm": (0, None)}.get(
                                            field, (None, None))
                    point[field] = _number(raw, minimum, maximum)
                    if field == "heart_rate_bpm" and point[field] == 0:
                        point[field] = None
                if point[field] is None:
                    warn("ambiguous_value" if ambiguous else
                         "missing_value" if raw is None else "invalid_value", si, pi, field)
            points.append(point)
        if not points:
            warn("empty_segment", si)
        segments.append({"segment_index": si, "points": points})
    if not point_count:
        raise TCXError("no_track_points", "No TCX track points found")
    # Match GPX optional-channel absence and point-level gap semantics.
    # Required time and coordinates are never filtered as absent channels.
    optional_labels = {"heart_rate_bpm": "心率", "power_w": "功率",
                       "cadence_rpm": "踏频", "ele": "海拔"}
    absent_fields = set(optional_labels) - observed_optional_fields
    warnings[:] = [w for w in warnings if not (
        w["code"] == "missing_value" and w.get("field") in absent_fields)]
    has_position = any(p["lat"] is not None and p["lon"] is not None
                       for segment in segments for p in segment["points"])
    if not has_position:
        warn("no_valid_positions", None, field="position")
    metrics, edge_warnings = _metrics(segments, datetimes)
    warnings.extend(edge_warnings)
    return {"schema_version": "luma-ride-gpx/1", "source": source,
            "segments": segments, "metrics": metrics, "warnings": warnings,
            "point_count": point_count, "has_position": has_position,
            "route_summary": {"value": None, "missing_reason":
                              "not_generated" if has_position else "no_valid_positions"},
            "limitations": ["Recorded data, not independently verified measurements",
                            "Totals exclude discontinuities; not wall-clock or moving time",
                            "Raw ascent has no noise filtering; no interpolation across gaps"]
                           + ["本次无%s数据（%s；全程未记录）" % (label, field)
                              for field, label in optional_labels.items() if field in absent_fields]}


def parse_tcx_file(path):
    """Read no more than the byte cap + 1; do not expose local paths on errors."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise TCXError("file_unavailable", "Cannot read local TCX file") from None
    return parse_tcx(data)
