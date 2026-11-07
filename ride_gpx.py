"""Bounded, offline GPX track parser. No uploads, persistence or training advice.

parse_gpx(bytes) / parse_gpx_file(path) return luma-ride-gpx/1 dictionaries.
Distances use a spherical Earth (6371008.8 m); ascent is unfiltered positive
altitude deltas. Totals cover continuous, usable edges ONLY, not missing gaps
or wall-clock ride duration. Coverage/warnings expose excluded edges. A sensor
mean requires one continuous, fully timed track with every sensor value present;
it is a trapezoidal time-weighted estimate, not a device-reported average.
"""
from __future__ import annotations

import hashlib
import math
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from xml.etree.ElementTree import TreeBuilder, Element, SubElement
from xml.parsers import expat

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_POINTS = 50000
MAX_XML_DEPTH = 64
MAX_GAP_S = 300  # Explicit conservative discontinuity policy, not device truth.
EARTH_RADIUS_M = 6371008.8
GPX_NAMESPACES = ("", "http://www.topografix.com/GPX/1/0",
                  "http://www.topografix.com/GPX/1/1")
TRACK_POINT_NAMESPACES = (
    "http://www.garmin.com/xmlschemas/TrackPointExtension/v1",
    "http://www.garmin.com/xmlschemas/TrackPointExtension/v2",
)
POWER_NAMESPACE = "http://www.garmin.com/xmlschemas/PowerExtension/v1"
CAPTURE_NAMESPACE = "urn:luma:ride-capture:1"
CAPTURE_VERSION = "luma-ride-capture/1"
CAPTURE_GAP_REASONS = frozenset((
    "disconnected", "notification_timeout", "invalid_notification", "time_reversal",
    "page_hidden", "refresh_recovery", "connect_failed", "manual_disconnect",
))


class GPXError(ValueError):
    """Machine-readable input rejection; never contains the uploaded XML."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _split_tag(tag):
    if tag.startswith("{"):
        return tag[1:].split("}", 1)
    return "", tag


def _safe_tree(data):
    # Expat callbacks reject declarations BEFORE any entity expansion. This also
    # covers UTF-16 input, unlike a byte-substring DOCTYPE blacklist.
    builder = TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    depth = 0
    points = 0

    def tag(name):
        return "{" + name if "}" in name else name

    def reject(*_args):
        raise GPXError("unsafe_xml", "DTD and entity declarations are forbidden")

    def start(name, attrs):
        nonlocal depth, points
        depth += 1
        if depth > MAX_XML_DEPTH:
            raise GPXError("xml_too_deep", "XML nesting exceeds limit")
        expanded = tag(name)
        namespace, local = _split_tag(expanded)
        if namespace == "http://www.w3.org/2001/XInclude":
            raise GPXError("unsafe_xml", "XInclude is forbidden")
        if (local == "trkpt" and namespace in GPX_NAMESPACES) or (
                local == "sample" and namespace == CAPTURE_NAMESPACE):
            points += 1
            if points > MAX_POINTS:
                raise GPXError("too_many_points", "Track point limit exceeded")
        builder.start(expanded, {tag(k): v for k, v in attrs.items()})

    def end(name):
        nonlocal depth
        builder.end(tag(name))
        depth -= 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.UnparsedEntityDeclHandler = reject
    try:
        parser.Parse(data, True)
        return builder.close()
    except (expat.ExpatError, ValueError) as exc:
        if isinstance(exc, GPXError):
            raise
        raise GPXError("invalid_xml", "Malformed XML") from None


def _number(raw, minimum=None, maximum=None):
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _timestamp(raw):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _distance(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1., max(0., h))))


def _metric(value, unit, method, reason=None, **coverage):
    if value is not None and not math.isfinite(value):
        value, reason = None, "numeric_overflow"
    return {"value": value, "unit": unit, "origin": "calculated",
            "method": method, "missing_reason": reason, "coverage": coverage}


def _capture_tracks(root, q):
    """Adapt only the versioned sensor-only capture format, without editing bytes.

    Internal elements reuse ordinary point parsing; they are never exported as
    purported standard GPX trackpoints. No coordinates or elevation are invented.
    """
    lq = lambda name: "{" + CAPTURE_NAMESPACE + "}" + name
    found = list(root.iter(lq("sensorSeries")))
    if not found:
        return root.findall(q("trk")), []

    def invalid():
        raise GPXError("invalid_capture_series", "Invalid or mixed sensor-only capture series")

    direct = [node for ext in root.findall(q("extensions"))
              for node in ext.findall(lq("sensorSeries"))]
    if len(found) != 1 or direct != found or root.findall(q("trk")):
        invalid()
    series = found[0]
    if (set(series.attrib) != {"schema_version", "gps", "started_at", "ended_at"}
            or series.get("schema_version") != CAPTURE_VERSION or series.get("gps") != "none"):
        invalid()
    started, ended = (_timestamp(series.get(name)) for name in ("started_at", "ended_at"))
    if started is None or ended is None or ended < started:
        invalid()
    gap_times, warnings = [], []
    for node in series:
        if node.tag == lq("gap"):
            time = _timestamp(node.get("time"))
            if (set(node.attrib) != {"time", "reason"} or len(node)
                    or time is None or node.get("reason") not in CAPTURE_GAP_REASONS):
                invalid()
            # Refresh/clock markers can lie outside the recorded sample range.
            gap_times.append(time)
            warnings.append({"code": "capture_gap", "segment_index": None,
                             "point_index": None, "field": "time",
                             "time": time.isoformat().replace("+00:00", "Z"),
                             "reason": node.get("reason")})
        elif node.tag != lq("segment"):
            invalid()
    gap_times.sort()
    track = Element(q("trk"))
    for segment in series.findall(lq("segment")):
        if segment.attrib or not len(segment):
            invalid()
        output = SubElement(track, q("trkseg"))
        previous = None
        for sample in segment:
            time = _timestamp(sample.get("time"))
            if (sample.tag != lq("sample") or set(sample.attrib) != {"time"}
                    or time is None or not started <= time <= ended):
                invalid()
            # An explicit marker between receipts must not be treated as a
            # continuous sensor edge, even if the producer forgot to split it.
            if (previous is not None and previous < time
                    and bisect_right(gap_times, previous) < bisect_right(gap_times, time)):
                output = SubElement(track, q("trkseg"))
            point = SubElement(output, q("trkpt"))
            SubElement(point, q("time")).text = sample.get("time")
            extension = SubElement(point, q("extensions"))
            for child in sample:
                namespace, _ = _split_tag(child.tag)
                if namespace not in (*TRACK_POINT_NAMESPACES, POWER_NAMESPACE):
                    invalid()
                extension.append(child)
            previous = time
    return [track], warnings


def parse_gpx(data):
    """Parse original bytes (<=5 MiB); never sort points or fill missing values.

    Only GPX trk/trkseg/trkpt are consumed; waypoints/routes are not track data.
    All point fields are present, nullable, and traceable by segment/point index.
    Unknown sensor namespaces are ignored rather than guessed. No heart/power
    zones, speed, moving time or fitness conclusions are inferred.
    """
    if not isinstance(data, bytes):
        raise GPXError("invalid_input", "GPX input must be bytes")
    if len(data) > MAX_FILE_BYTES:
        raise GPXError("file_too_large", "GPX exceeds 5 MiB")
    if not data:
        raise GPXError("empty_input", "GPX input is empty")
    root = _safe_tree(data)
    ns, local = _split_tag(root.tag)
    if local != "gpx" or ns not in GPX_NAMESPACES:
        raise GPXError("invalid_gpx", "Expected a GPX root namespace")
    prefix = "{" + ns + "}" if ns else ""
    q = lambda name: prefix + name
    tracks, warnings = _capture_tracks(root, q)
    segments = []
    datetimes = []
    observed_optional_fields = set()

    def warn(code, segment, point=None, field=None):
        warnings.append({"code": code, "segment_index": segment,
                         "point_index": point, "field": field})

    for track in tracks:
        for segment in track.findall(q("trkseg")):
            index = len(segments)
            points = []
            for pi, element in enumerate(segment.findall(q("trkpt"))):
                point = {"segment_index": index, "point_index": pi,
                         "time": None, "lat": None, "lon": None, "ele": None,
                         "heart_rate_bpm": None, "power_w": None, "cadence_rpm": None}
                for field, minimum, maximum in (("lat", -90, 90), ("lon", -180, 180)):
                    raw = element.get(field)
                    point[field] = _number(raw, minimum, maximum)
                    if point[field] is None:
                        warn("missing_value" if raw is None else "invalid_value", index, pi, field)
                for field in ("time", "ele"):
                    values = element.findall(q(field))
                    if field == "ele" and values:
                        observed_optional_fields.add(field)
                    raw = values[0].text if len(values) == 1 else None
                    if field == "time":
                        dt = _timestamp(raw)
                        datetimes.append(dt)
                        point[field] = dt.isoformat().replace("+00:00", "Z") if dt else None
                    else:
                        point[field] = _number(raw)
                    if point[field] is None:
                        warn("ambiguous_value" if len(values) > 1 else
                             "missing_value" if raw is None else "invalid_value", index, pi, field)
                sensors = {"heart_rate_bpm": [], "power_w": [], "cadence_rpm": []}
                for extension in element.findall(q("extensions")):
                    for node in extension.iter():
                        sensor_ns, name = _split_tag(node.tag)
                        if name in ("hr", "heartrate") and sensor_ns in ("", *TRACK_POINT_NAMESPACES):
                            sensors["heart_rate_bpm"].append(node.text)
                        if name in ("power", "watts") and sensor_ns in ("", ns, POWER_NAMESPACE, *TRACK_POINT_NAMESPACES):
                            sensors["power_w"].append(node.text)
                        if name in ("cad", "cadence") and sensor_ns in ("", *TRACK_POINT_NAMESPACES):
                            sensors["cadence_rpm"].append(node.text)
                for field, values in sensors.items():
                    if values:
                        observed_optional_fields.add(field)
                    if len(values) == 1:
                        value = _number(values[0], 0)
                        if field == "heart_rate_bpm" and value == 0:
                            value = None
                        point[field] = value
                    if point[field] is None:
                        warn("ambiguous_value" if len(values) > 1 else
                             "missing_value" if not values else "invalid_value", index, pi, field)
                points.append(point)
            if not points:
                warn("empty_segment", index)
            segments.append({"segment_index": index, "points": points})
    if not any(s["points"] for s in segments):
        raise GPXError("no_track_points", "No GPX track points found")

    # Each segment is independent. Invalid/gapped time and invalid coordinates
    # break the edge, without dropping or reordering the original points.
    flat = [p for s in segments for p in s["points"]]
    # Optional channels are absent only if no recognized element occurred
    # anywhere in the record, not merely if every value was invalid/empty.
    # Time is required: its point-level integrity warnings are never filtered.
    optional_labels = {"heart_rate_bpm": "心率", "power_w": "功率",
                       "cadence_rpm": "踏频", "ele": "海拔"}
    absent_fields = set(optional_labels) - observed_optional_fields
    warnings[:] = [w for w in warnings if not (
        w["code"] == "missing_value" and w.get("field") in absent_fields)]
    has_position = any(p["lat"] is not None and p["lon"] is not None for p in flat)
    if not has_position:
        warn("no_valid_positions", None, field="position")
    time_by_point = dict(zip(((p["segment_index"], p["point_index"]) for p in flat), datetimes))
    total_distance = total_duration = ascent = 0.
    usable_edges = ascent_edges = possible_edges = coordinate_edges = 0
    for segment in segments:
        points = segment["points"]
        si = segment["segment_index"]
        for previous, current in zip(points, points[1:]):
            possible_edges += 1
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
                warn(reason, si, current["point_index"], "time")
                continue
            # Duration has a valid time basis even if location is invalid.
            total_duration += seconds
            usable_edges += 1
            if all(p[f] is not None for p in (previous, current) for f in ("lat", "lon")):
                total_distance += _distance(previous, current)
                coordinate_edges += 1
            else:
                warn("missing_coordinate_boundary", si, current["point_index"])
            if previous["ele"] is not None and current["ele"] is not None:
                ascent += max(0., current["ele"] - previous["ele"])
                ascent_edges += 1

    coverage = {"possible_edges": possible_edges, "timed_edges": usable_edges,
                "excluded_time_edges": possible_edges - usable_edges,
                "declared_segments": len(segments), "max_gap_s": MAX_GAP_S}
    metrics = {
        "total_distance_m": _metric(total_distance if coordinate_edges else None, "m", "haversine_valid_edges/1",
                                    None if coordinate_edges else ("no_valid_positions" if not has_position else "insufficient_coordinates_or_time"),
                                    **coverage, used_edges=coordinate_edges),
        "total_duration_s": _metric(total_duration if usable_edges else None, "s", "sum_continuous_intervals/1",
                                    None if usable_edges else "insufficient_valid_time_intervals",
                                    **coverage, used_edges=usable_edges),
        "elevation_gain_m": _metric(ascent if ascent_edges else None, "m", "sum_positive_raw_deltas/1",
                                    None if ascent_edges else "insufficient_elevation_or_time",
                                    **coverage, used_edges=ascent_edges),
    }
    # A mean cannot silently ignore missing samples or inter-segment gaps.
    for field, output, unit in (("heart_rate_bpm", "average_heart_rate_bpm", "bpm"),
                                ("power_w", "average_power_w", "W")):
        reason = None
        if len(flat) < 2:
            reason = "insufficient_samples"
        elif len(segments) != 1:
            reason = "discontinuous_track"
        elif any(w["code"] == "capture_gap" for w in warnings):
            reason = "declared_capture_gap"
        elif any(t is None for t in datetimes):
            reason = "incomplete_time_sequence"
        elif usable_edges != possible_edges:
            reason = "invalid_or_gapped_time_sequence"
        elif any(p[field] is None for p in flat):
            reason = "incomplete_sensor_sequence"
        value = None
        if reason is None:
            integral = sum((a[field] + b[field]) / 2 * (tb - ta).total_seconds()
                           for a, b, ta, tb in zip(flat, flat[1:], datetimes, datetimes[1:]))
            value = integral / total_duration
        metrics[output] = _metric(value, unit, "trapezoidal_time_weighted/1", reason,
                                  **coverage, sample_count=len(flat),
                                  valid_sensor_samples=sum(p[field] is not None for p in flat))
    return {"schema_version": "luma-ride-gpx/1",
            "source": {"kind": "gpx", "sha256": hashlib.sha256(data).hexdigest(),
                       "hash_scope": "raw_file_bytes", "byte_length": len(data)},
            "segments": segments, "metrics": metrics, "warnings": warnings,
            "point_count": len(flat), "has_position": has_position,
            "route_summary": {"value": None, "missing_reason":
                              "not_generated" if has_position else "no_valid_positions"},
            "limitations": ["Recorded data, not independently verified measurements",
                            "Totals exclude discontinuities; not wall-clock or moving time",
                            "Raw ascent has no noise filtering; no interpolation across gaps"]
                           + ["本次无%s数据（%s；全程未记录）" % (label, field)
                              for field, label in optional_labels.items() if field in absent_fields]}


def parse_gpx_file(path):
    """Read at most the limit + 1 bytes, even if a file grows after opening."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise GPXError("file_unavailable", "Cannot read local GPX file") from None
    return parse_gpx(data)

