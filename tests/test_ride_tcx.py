"""Synthetic, offline TCX checks. Execution belongs to strategy, not authoring."""
import hashlib
import io
import math
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ride_gpx
import ride_tcx

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class RideTCXTests(unittest.TestCase):
    def fixture(self, name):
        return (FIXTURES / ("ride_tcx_" + name + ".tcx")).read_bytes()

    def parse(self, name):
        return ride_tcx.parse_tcx(self.fixture(name))

    def assert_metric(self, result, name, expected, unit, reason=None):
        metric = result["metrics"][name]
        self.assertEqual(metric["unit"], unit)
        self.assertEqual(metric["origin"], "calculated")
        self.assertTrue(metric["method"])
        self.assertEqual(metric["missing_reason"], reason)
        if expected is None:
            self.assertIsNone(metric["value"])
        else:
            self.assertAlmostEqual(metric["value"], expected, places=6)
        return metric

    def test_normal_fields_provenance_and_file_api(self):
        raw = self.fixture("normal")
        result = ride_tcx.parse_tcx(raw)
        self.assertEqual(result["source"], {
            "kind": "tcx", "sha256": hashlib.sha256(raw).hexdigest(),
            "hash_scope": "raw_file_bytes", "byte_length": len(raw),
        })
        self.assertEqual(result["point_count"], 3)
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["segment_index"], 0)
        for index, (ele, hr) in enumerate(((10, 100), (15, 120), (12, 140))):
            self.assertEqual(result["segments"][0]["points"][index], {
                "segment_index": 0, "point_index": index,
                "time": "2026-01-01T00:00:%02dZ" % (index * 10),
                "lat": 0, "lon": index * 0.001, "ele": ele,
                "heart_rate_bpm": hr, "power_w": None, "cadence_rpm": None,
            })
        self.assertEqual(ride_tcx.parse_tcx_file(FIXTURES / "ride_tcx_normal.tcx"), result)

    def test_normal_totals_ignore_lap_reported_summary(self):
        result = self.parse("normal")
        self.assert_metric(result, "total_distance_m", 6371008.8 * math.radians(0.002), "m")
        duration = self.assert_metric(result, "total_duration_s", 20, "s")
        self.assert_metric(result, "elevation_gain_m", 5, "m")
        self.assert_metric(result, "average_heart_rate_bpm", 120, "bpm")
        self.assert_metric(result, "average_power_w", None, "W", "incomplete_sensor_sequence")
        self.assertEqual(duration["coverage"], {
            "possible_edges": 2, "timed_edges": 2, "excluded_time_edges": 0,
            "declared_segments": 1, "max_gap_s": 300, "used_edges": 2,
        })

    def test_same_record_shape_and_values_as_equivalent_gpx(self):
        gpx = (FIXTURES / "ride_gpx_normal.gpx").read_bytes()
        gpx = re.sub(rb"<power>[^<]*</power>", b"", gpx)
        expected = ride_gpx.parse_gpx(gpx)
        actual = self.parse("normal")
        self.assertEqual(set(actual), set(expected))
        self.assertEqual({k: v for k, v in actual.items() if k != "source"},
                         {k: v for k, v in expected.items() if k != "source"})
        self.assertEqual(set(actual["source"]), set(expected["source"]))

    def test_missing_hr_is_null_with_explicit_reason(self):
        result = self.parse("missing_hr")
        self.assertEqual([p["heart_rate_bpm"] for p in result["segments"][0]["points"]],
                         [100, None, 140])
        hr = self.assert_metric(result, "average_heart_rate_bpm", None, "bpm",
                                "incomplete_sensor_sequence")
        self.assertEqual(hr["coverage"]["valid_sensor_samples"], 2)
        self.assertIn({"code": "missing_value", "segment_index": 0,
                       "point_index": 1, "field": "heart_rate_bpm"}, result["warnings"])
        self.assert_metric(result, "total_duration_s", 20, "s")
        self.assert_metric(result, "elevation_gain_m", 5, "m")

    def test_power_extensions_zero_is_valid_and_means_are_time_weighted(self):
        raw = self.fixture("power")
        result = ride_tcx.parse_tcx(raw)
        self.assertEqual([p["power_w"] for p in result["segments"][0]["points"]], [0, 100, 200])
        self.assert_metric(result, "average_power_w", 100, "W")
        self.assertEqual(result["warnings"], [])
        uneven = ride_tcx.parse_tcx(raw.replace(b"00:00:20Z", b"00:00:30Z"))
        self.assert_metric(uneven, "average_power_w", (50 * 10 + 150 * 20) / 30, "W")
        self.assert_metric(uneven, "average_heart_rate_bpm", (110 * 10 + 130 * 20) / 30, "bpm")

    def test_direct_watts_supported_and_unknown_extension_namespace_ignored(self):
        raw = self.fixture("power")
        direct = re.sub(rb"<Extensions><ae:TPX><ae:Watts>([^<]*)</ae:Watts></ae:TPX></Extensions>",
                        rb"<Watts>\1</Watts>", raw)
        self.assert_metric(ride_tcx.parse_tcx(direct), "average_power_w", 100, "W")
        unknown = raw.replace(b"http://www.garmin.com/xmlschemas/ActivityExtension/v2", b"urn:unknown")
        result = ride_tcx.parse_tcx(unknown)
        self.assertTrue(all(p["power_w"] is None for p in result["segments"][0]["points"]))
        self.assert_metric(result, "average_power_w", None, "W", "incomplete_sensor_sequence")

    def test_breaks_never_bridge_laps_or_tracks(self):
        result = self.parse("breaks")
        self.assertEqual(result["point_count"], 4)
        self.assertEqual([s["segment_index"] for s in result["segments"]], [0, 1])
        for si, segment in enumerate(result["segments"]):
            for pi, point in enumerate(segment["points"]):
                self.assertEqual((point["segment_index"], point["point_index"]), (si, pi))
        distance = 6371008.8 * math.radians(0.001) + 2 * 6371008.8 * math.asin(
            math.cos(math.radians(45)) * math.sin(math.radians(0.001) / 2))
        self.assert_metric(result, "total_distance_m", distance, "m")
        self.assert_metric(result, "total_duration_s", 20, "s")
        self.assert_metric(result, "elevation_gain_m", 8, "m")
        for name, unit in (("average_heart_rate_bpm", "bpm"), ("average_power_w", "W")):
            metric = self.assert_metric(result, name, None, unit, "discontinuous_track")
            self.assertEqual(metric["coverage"]["declared_segments"], 2)

    def test_missing_reverse_duplicate_and_gapped_time_exclude_edges(self):
        raw = self.fixture("power")
        tag = b"<Time>2026-01-01T00:00:20Z</Time>"
        cases = (
            (b"", "missing_time_boundary", "incomplete_time_sequence", None),
            (b"<Time>2026-01-01T00:00:05Z</Time>", "non_increasing_time",
             "invalid_or_gapped_time_sequence", "2026-01-01T00:00:05Z"),
            (b"<Time>2026-01-01T00:00:10Z</Time>", "non_increasing_time",
             "invalid_or_gapped_time_sequence", "2026-01-01T00:00:10Z"),
            (b"<Time>2026-01-01T00:10:20Z</Time>", "time_gap_boundary",
             "invalid_or_gapped_time_sequence", "2026-01-01T00:10:20Z"),
        )
        for replacement, warning, reason, time in cases:
            with self.subTest(warning=warning, replacement=replacement):
                result = ride_tcx.parse_tcx(raw.replace(tag, replacement))
                self.assertEqual(result["point_count"], 3)
                self.assertEqual(result["segments"][0]["points"][2]["time"], time)
                self.assertEqual([p["point_index"] for p in result["segments"][0]["points"]], [0, 1, 2])
                self.assert_metric(result, "total_distance_m", 6371008.8 * math.radians(0.001), "m")
                duration = self.assert_metric(result, "total_duration_s", 10, "s")
                self.assert_metric(result, "elevation_gain_m", 5, "m")
                self.assertEqual(duration["coverage"]["excluded_time_edges"], 1)
                self.assertIn({"code": warning, "segment_index": 0,
                               "point_index": 2, "field": "time"}, result["warnings"])
                for name, unit in (("average_heart_rate_bpm", "bpm"), ("average_power_w", "W")):
                    self.assert_metric(result, name, None, unit, reason)

    def test_missing_position_does_not_invent_coordinates_or_lose_duration(self):
        raw = self.fixture("normal").replace(
            b"<Position><LatitudeDegrees>0</LatitudeDegrees><LongitudeDegrees>0.001</LongitudeDegrees></Position>", b"")
        result = ride_tcx.parse_tcx(raw)
        point = result["segments"][0]["points"][1]
        self.assertIsNone(point["lat"])
        self.assertIsNone(point["lon"])
        self.assert_metric(result, "total_distance_m", None, "m", "insufficient_coordinates_or_time")
        self.assert_metric(result, "total_duration_s", 20, "s")
        self.assert_metric(result, "elevation_gain_m", 5, "m")

    def test_invalid_and_ambiguous_sensor_values_stay_null(self):
        raw = self.fixture("power")
        for old, new, field, warning in (
            (b"<Value>120</Value>", b"<Value>0</Value>", "heart_rate_bpm", "invalid_value"),
            (b"<ae:Watts>100</ae:Watts>", b"<ae:Watts>-1</ae:Watts>", "power_w", "invalid_value"),
            (b"<ae:Watts>100</ae:Watts>", b"<ae:Watts>NaN</ae:Watts>", "power_w", "invalid_value"),
            (b"<ae:Watts>100</ae:Watts>", b"<ae:Watts>100</ae:Watts><ae:Watts>200</ae:Watts>",
             "power_w", "ambiguous_value"),
        ):
            with self.subTest(field=field, replacement=new):
                result = ride_tcx.parse_tcx(raw.replace(old, new))
                self.assertIsNone(result["segments"][0]["points"][1][field])
                self.assertIn({"code": warning, "segment_index": 0,
                               "point_index": 1, "field": field}, result["warnings"])
                name, unit = ("average_power_w", "W") if field == "power_w" else ("average_heart_rate_bpm", "bpm")
                self.assert_metric(result, name, None, unit, "incomplete_sensor_sequence")

    def test_empty_point_fields_and_unavailable_metrics_are_explicit_nulls(self):
        result = self.parse("oversize_seed")
        self.assertEqual(result["segments"][0]["points"][0], {
            "segment_index": 0, "point_index": 0, "time": None, "lat": None,
            "lon": None, "ele": None, "heart_rate_bpm": None, "power_w": None, "cadence_rpm": None,
        })
        for name, unit, reason in (
            ("total_distance_m", "m", "no_valid_positions"),
            ("total_duration_s", "s", "insufficient_valid_time_intervals"),
            ("elevation_gain_m", "m", "insufficient_elevation_or_time"),
            ("average_heart_rate_bpm", "bpm", "insufficient_samples"),
            ("average_power_w", "W", "insufficient_samples"),
        ):
            self.assert_metric(result, name, None, unit, reason)

    def test_five_mib_boundary_and_oversize_rejected_before_parsing(self):
        self.assertEqual(ride_tcx.MAX_FILE_BYTES, 5 * 1024 * 1024)
        seed = self.fixture("oversize_seed")
        data = seed + b" " * (ride_tcx.MAX_FILE_BYTES - len(seed))
        self.assertEqual(ride_tcx.parse_tcx(data)["source"]["byte_length"], len(data))
        with patch.object(ride_tcx, "_safe_tree") as parser:
            with self.assertRaises(ride_tcx.TCXError) as caught:
                ride_tcx.parse_tcx(data + b" ")
            self.assertEqual(caught.exception.code, "file_too_large")
            parser.assert_not_called()

    def test_file_api_limits_read_and_rejects_oversize(self):
        seed = self.fixture("oversize_seed")
        stream = io.BytesIO(seed + b" " * (ride_tcx.MAX_FILE_BYTES + 1 - len(seed)))
        with patch.object(Path, "open", return_value=stream):
            with patch.object(stream, "read", wraps=stream.read) as read:
                with patch.object(ride_tcx, "_safe_tree") as parser:
                    with self.assertRaises(ride_tcx.TCXError) as caught:
                        ride_tcx.parse_tcx_file("synthetic-oversize.tcx")
                    self.assertEqual(caught.exception.code, "file_too_large")
                    read.assert_called_once_with(ride_tcx.MAX_FILE_BYTES + 1)
                    parser.assert_not_called()

    def test_entities_rejected_before_any_tree_or_expanded_text(self):
        for name in ("external_entity", "internal_entity"):
            raw = self.fixture(name)
            for encoding, data in (("original", raw), ("utf-16", raw.decode("utf-8-sig").encode("utf-16"))):
                with self.subTest(fixture=name, encoding=encoding):
                    builder = Mock(wraps=ride_gpx.TreeBuilder())
                    with patch.object(ride_gpx, "TreeBuilder", return_value=builder):
                        with self.assertRaises(ride_tcx.TCXError) as caught:
                            ride_tcx.parse_tcx(data)
                    self.assertEqual(caught.exception.code, "unsafe_xml")
                    self.assertIn("forbidden", str(caught.exception))
                    builder.start.assert_not_called()
                    builder.data.assert_not_called()
                    builder.close.assert_not_called()

    def test_xml_depth_xinclude_and_point_limits_are_explicit_rejections(self):
        cases = (
            (b'<TrainingCenterDatabase xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="file:///LUMA_SYNTHETIC_DO_NOT_READ"/></TrainingCenterDatabase>', "unsafe_xml"),
            (b"<TrainingCenterDatabase>" + b"<x>" * 65 + b"</x>" * 65 + b"</TrainingCenterDatabase>", "xml_too_deep"),
        )
        for data, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ride_tcx.TCXError) as caught:
                    ride_tcx.parse_tcx(data)
                self.assertEqual(caught.exception.code, code)
        with patch.object(ride_tcx, "MAX_POINTS", 2):
            with self.assertRaises(ride_tcx.TCXError) as caught:
                self.parse("normal")
            self.assertEqual(caught.exception.code, "too_many_points")

    def test_invalid_inputs_and_file_errors_have_stable_codes(self):
        for data, code in (("not bytes", "invalid_input"), (b"", "empty_input"),
                           (b"<broken", "invalid_xml"), (b"<gpx/>", "invalid_tcx"),
                           (b"<TrainingCenterDatabase/>", "no_track_points")):
            with self.subTest(code=code):
                with self.assertRaises(ride_tcx.TCXError) as caught:
                    ride_tcx.parse_tcx(data)
                self.assertEqual(caught.exception.code, code)
        with patch.object(Path, "open", side_effect=OSError("private synthetic path")):
            with self.assertRaises(ride_tcx.TCXError) as caught:
                ride_tcx.parse_tcx_file("unused.tcx")
            self.assertEqual(caught.exception.code, "file_unavailable")
            self.assertNotIn("private synthetic path", str(caught.exception))

    def test_course_tracks_are_supported(self):
        raw = self.fixture("normal")
        track = re.search(rb"<Track>.*?</Track>", raw, re.DOTALL).group(0)
        data = (b'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
                b'<Courses><Course>' + track + b'</Course></Courses></TrainingCenterDatabase>')
        self.assertEqual(ride_tcx.parse_tcx(data)["metrics"], self.parse("normal")["metrics"])


    def test_position_contract_matches_gpx_without_inventing_route_summary(self):
        actual = self.parse("normal")
        expected = ride_gpx.parse_gpx((FIXTURES / "ride_gpx_normal.gpx").read_bytes())
        self.assertIs(actual["has_position"], True)
        self.assertEqual(actual["route_summary"], {"value": None, "missing_reason": "not_generated"})
        for key in ("has_position", "route_summary"):
            self.assertEqual(actual[key], expected[key])

    def test_sensor_only_record_matches_equivalent_gpx_including_no_position_reason(self):
        raw = re.sub(rb"<Position>.*?</Position>", b"", self.fixture("normal"), flags=re.DOTALL)
        gpx = (FIXTURES / "ride_gpx_normal.gpx").read_bytes()
        gpx = re.sub(rb"<power>[^<]*</power>", b"", gpx)
        gpx = re.sub(rb' lat="[^"]*" lon="[^"]*"', b"", gpx)
        actual = ride_tcx.parse_tcx(raw)
        expected = ride_gpx.parse_gpx(gpx)
        self.assertIs(actual["has_position"], False)
        self.assertEqual(actual["route_summary"], {"value": None, "missing_reason": "no_valid_positions"})
        self.assertEqual({k: v for k, v in actual.items() if k != "source"},
                         {k: v for k, v in expected.items() if k != "source"})
        self.assert_metric(actual, "total_distance_m", None, "m", "no_valid_positions")
        self.assert_metric(actual, "total_duration_s", 20, "s")
        self.assert_metric(actual, "average_heart_rate_bpm", 120, "bpm")
        self.assertEqual(actual["source"]["sha256"], hashlib.sha256(raw).hexdigest())

    def test_invalid_or_incomplete_coordinate_pairs_are_not_positions(self):
        for position in (b"", b"<LatitudeDegrees>0</LatitudeDegrees>",
                         b"<LongitudeDegrees>0</LongitudeDegrees>",
                         b"<LatitudeDegrees>NaN</LatitudeDegrees><LongitudeDegrees>0</LongitudeDegrees>",
                         b"<LatitudeDegrees>91</LatitudeDegrees><LongitudeDegrees>0</LongitudeDegrees>"):
            with self.subTest(position=position):
                raw = re.sub(rb"<Position>.*?</Position>", b"<Position>" + position + b"</Position>",
                             self.fixture("normal"), flags=re.DOTALL)
                result = ride_tcx.parse_tcx(raw)
                self.assertIs(result["has_position"], False)
                self.assertEqual(result["route_summary"], {"value": None, "missing_reason": "no_valid_positions"})
                self.assert_metric(result, "total_distance_m", None, "m", "no_valid_positions")

    def test_one_valid_point_sets_position_flag_without_claiming_route_distance(self):
        raw = self.fixture("normal")
        raw = re.sub(rb"<Position>.*?</Position>", b"", raw, count=2, flags=re.DOTALL)
        result = ride_tcx.parse_tcx(raw)
        self.assertIs(result["has_position"], True)
        self.assertEqual(result["route_summary"], {"value": None, "missing_reason": "not_generated"})
        self.assert_metric(result, "total_distance_m", None, "m", "insufficient_coordinates_or_time")
        self.assertTrue(any(w["code"] == "missing_coordinate_boundary" for w in result["warnings"]))
        self.assertFalse(any(w["code"] == "no_valid_positions" for w in result["warnings"]))

    def test_shared_optional_absence_keeps_required_time_warning(self):
        result = self.parse("oversize_seed")
        for field, label in (("heart_rate_bpm", "心率"), ("power_w", "功率"),
                             ("cadence_rpm", "踏频"), ("ele", "海拔")):
            with self.subTest(field=field):
                self.assertIn("本次无%s数据（%s；全程未记录）" % (label, field), result["limitations"])
                self.assertFalse(any(w["code"] == "missing_value" and w.get("field") == field
                                     for w in result["warnings"]))
        self.assertIn({"code": "missing_value", "segment_index": 0, "point_index": 0,
                       "field": "time"}, result["warnings"])

    def test_cadence_shared_point_field_preserves_zero_gaps_and_invalid_values(self):
        for element, value, code in ((b"<Cadence>0</Cadence>", 0, None),
                                     (b"<Cadence>90</Cadence>", 90, None),
                                     (b"<Cadence>NaN</Cadence>", None, "invalid_value"),
                                     (b"<Cadence>80</Cadence><Cadence>90</Cadence>", None, "ambiguous_value")):
            with self.subTest(element=element):
                result = ride_tcx.parse_tcx(self.fixture("normal").replace(b"</Trackpoint>", element + b"</Trackpoint>", 1))
                self.assertEqual([p["cadence_rpm"] for p in result["segments"][0]["points"]], [value, None, None])
                self.assertNotIn("本次无踏频数据（cadence_rpm；全程未记录）", result["limitations"])
                if code:
                    self.assertIn({"code": code, "segment_index": 0, "point_index": 0,
                                   "field": "cadence_rpm"}, result["warnings"])
                for index in (1, 2):
                    self.assertIn({"code": "missing_value", "segment_index": 0, "point_index": index,
                                   "field": "cadence_rpm"}, result["warnings"])

if __name__ == "__main__":
    unittest.main()
