"""Synthetic offline Intervals.icu fixtures; no accounts, API keys or real rides."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_icu
import ride_gpx


def synthetic():
    activity = {"id": "synthetic-1", "type": "Ride", "start_date": "2026-11-06T00:00:00Z",
                "icu_ctl": "40", "icu_atl": 50, "icu_training_load": "60"}
    streams = [{"type": name, "data": values} for name, values in {
        "time": [0, "10", 20], "watts": [100, "200", 300],
        "heartrate": [100, 120, 140], "cadence": [0, "90", 100],
        "altitude": [10, 15, 12], "latlng": [[0, 0], [0, .001], [0, .002]]}.items()]
    return activity, streams


class CoachICUTests(unittest.TestCase):
    def test_normal_record_and_context(self):
        activity, streams = synthetic()
        record = coach_icu.normalize_activity(activity, streams,
            {"ftp": "250", "power_zones": [0, "138", 175], "lthr": 165, "hr_zones": [0, 130, 150]},
            {"resting_hr": "52", "sleep_secs": 27000})
        self.assertEqual(record["schema_version"], "luma-ride-gpx/1")
        self.assertEqual(record["source"]["kind"], "intervals_icu")
        self.assertEqual(record["point_count"], 3)
        self.assertEqual(record["metrics"]["average_power_w"]["value"], 200)
        self.assertEqual(record["metrics"]["total_duration_s"]["value"], 20)
        self.assertEqual(record["metrics"]["elevation_gain_m"]["value"], 5)
        self.assertEqual(record["coach_context"], {"ftp": 250, "power_zones": [0, 138, 175],
            "power_zones_note": None,
            "lthr": 165, "hr_zones": [0, 130, 150], "ctl": 40, "atl": 50,
            "training_load": 60, "resting_hr": 52, "sleep_secs": 27000})
        template = ride_gpx.parse_gpx(b'<gpx><trk><trkseg><trkpt/></trkseg></trk></gpx>')
        self.assertEqual(set(record) - {"coach_context"}, set(template))
        self.assertEqual(set(record["segments"][0]["points"][0]),
                         set(template["segments"][0]["points"][0]))
        self.assertEqual(set(record["source"]), set(template["source"]))
        for warning in record["warnings"]:
            self.assertEqual(set(warning), {"code", "segment_index", "point_index", "field"})

    def test_indoor_trainer_never_invents_position_or_distance(self):
        activity, streams = synthetic(); activity["trainer"] = True
        streams = [s for s in streams if s["type"] != "latlng"]
        record = coach_icu.normalize_activity(activity, streams)
        self.assertFalse(record["has_position"])
        self.assertIsNone(record["metrics"]["total_distance_m"]["value"])
        self.assertEqual(record["metrics"]["average_power_w"]["value"], 200)
        self.assertTrue(all(p["lat"] is None and p["lon"] is None for p in record["segments"][0]["points"]))
        self.assertFalse(any(w["field"] in ("lat", "lon") for w in record["warnings"]))

    def test_absent_power_and_heart_rate_are_record_level(self):
        for name, field in (("watts", "power_w"), ("heartrate", "heart_rate_bpm")):
            activity, streams = synthetic()
            record = coach_icu.normalize_activity(activity, [s for s in streams if s["type"] != name])
            relevant = [w for w in record["warnings"] if w["field"] == field]
            self.assertTrue(relevant)
            self.assertTrue(all(w["point_index"] is None for w in relevant))
            self.assertTrue(all(p[field] is None for p in record["segments"][0]["points"]))

    def test_short_and_null_streams_warn_at_points_without_truncation(self):
        activity, streams = synthetic(); streams[1]["data"] = [100, None]
        record = coach_icu.normalize_activity(activity, streams)
        self.assertEqual(record["point_count"], 3)
        self.assertEqual([p["power_w"] for p in record["segments"][0]["points"]], [100, None, None])
        self.assertEqual([w["point_index"] for w in record["warnings"]
            if w["field"] == "power_w" and w["code"] == "missing_value"], [1, 2])
        self.assertIn("stream_length_mismatch", [w["code"] for w in record["warnings"]])
        self.assertIsNone(record["metrics"]["average_power_w"]["value"])

    def test_no_optional_context_is_not_estimated(self):
        record = coach_icu.normalize_activity({"type": "Ride"}, [])
        self.assertTrue(all(value is None for value in record["coach_context"].values()))
        self.assertEqual(record["segments"], []); self.assertEqual(record["point_count"], 0)
        self.assertTrue(all(m["value"] is None for m in record["metrics"].values()))
        self.assertTrue(all(w["point_index"] is None for w in record["warnings"]))

    def test_missing_time_or_naive_origin_never_fabricates_utc(self):
        activity, streams = synthetic()
        for detail, values in ((activity, [s for s in streams if s["type"] != "time"]),
                               ({"type": "Ride", "start_date_local": "2026-11-06T08:00:00"}, streams)):
            record = coach_icu.normalize_activity(detail, values)
            self.assertTrue(all(p["time"] is None for p in record["segments"][0]["points"]))
            self.assertIsNone(record["metrics"]["total_duration_s"]["value"])

    def test_nonride_is_warning_not_rejection(self):
        activity, streams = synthetic(); activity["type"] = "Run"
        record = coach_icu.normalize_activity(activity, streams)
        self.assertEqual(record["point_count"], 3)
        self.assertIn("non_ride_activity", [w["code"] for w in record["warnings"]])

    def test_invalid_numbers_and_context_remain_missing(self):
        activity, streams = synthetic(); streams[1]["data"] = [True, "NaN", -1]
        streams[-1]["data"] = [[91, 0], None, [0, 181]]
        record = coach_icu.normalize_activity(activity, streams,
            {"ftp": False, "power_zones": [0, 100, 90], "lthr": float("inf")},
            {"sleep_secs": -1, "resting_hr": 0})
        self.assertFalse(record["has_position"])
        self.assertTrue(all(p["power_w"] is None for p in record["segments"][0]["points"]))
        for key in ("ftp", "power_zones", "lthr", "sleep_secs", "resting_hr"):
            self.assertIsNone(record["coach_context"][key])
        json.dumps(record, allow_nan=False)

    def test_no_mutation_and_deterministic_provenance(self):
        activity, streams = synthetic(); original = copy.deepcopy((activity, streams))
        first = coach_icu.normalize_activity(activity, streams)
        self.assertEqual(first, coach_icu.normalize_activity(activity, streams))
        self.assertEqual((activity, streams), original)
        self.assertNotEqual(first["source"]["hash_scope"], "raw_file_bytes")
        streams[1]["data"][0] = 101
        self.assertNotEqual(first["source"]["sha256"], coach_icu.normalize_activity(activity, streams)["source"]["sha256"])

    def test_bad_containers_duplicate_and_point_limits(self):
        for args in ((None, []), ({}, None), ({}, [], []), ({}, [{"type": "time", "data": None}])):
            with self.assertRaises(TypeError): coach_icu.normalize_activity(*args)
        with self.assertRaises(ValueError):
            coach_icu.normalize_activity({}, [{"type": "time", "data": []}] * 2)
        with self.assertRaises(ValueError):
            coach_icu.normalize_activity({}, [{"type": "time", "data": [0] * 50001}])

    def test_power_zones_percent_of_ftp_converted_to_watts(self):
        # Real Intervals.icu default: Coggan percent uppers with sentinel top.
        activity, streams = synthetic()
        record = coach_icu.normalize_activity(activity, streams,
            {"ftp": 250, "power_zones": [55, 75, 90, 105, 120, 150, 999]})
        context = record["coach_context"]
        self.assertEqual(context["power_zones"],
                         [0.0, 137.5, 187.5, 225.0, 262.5, 300.0, 375.0])
        self.assertEqual(context["power_zones_note"],
                         "percent_of_ftp_upper_bounds_converted_to_watts")

    def test_power_zones_absolute_watts_pass_through(self):
        activity, streams = synthetic()
        record = coach_icu.normalize_activity(activity, streams,
            {"ftp": 250, "power_zones": [0, 138, 188, 226, 263, 301]})
        context = record["coach_context"]
        self.assertEqual(context["power_zones"], [0, 138, 188, 226, 263, 301])
        self.assertIsNone(context["power_zones_note"])

    def test_power_zones_percent_without_ftp_degrades_to_none(self):
        activity, streams = synthetic()
        record = coach_icu.normalize_activity(activity, streams,
            {"power_zones": [55, 75, 90, 105, 120, 150, 999]})
        context = record["coach_context"]
        self.assertIsNone(context["power_zones"])
        self.assertEqual(context["power_zones_note"],
                         "power_zones_percent_without_ftp")

    def test_power_zones_nonmonotonic_conversion_rejected(self):
        activity, streams = synthetic()
        record = coach_icu.normalize_activity(activity, streams,
            {"ftp": 0, "power_zones": [55, 75, 90, 105, 120, 150, 999]})
        context = record["coach_context"]
        self.assertIsNone(context["power_zones"])
        self.assertEqual(context["power_zones_note"],
                         "power_zones_percent_without_ftp")


if __name__ == "__main__":
    unittest.main()