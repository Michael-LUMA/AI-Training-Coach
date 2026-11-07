"""Synthetic-only English coach tests: no API, accounts, payment or real rides."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_icu
import coach_review_en
import ride_review


def synthetic(**settings):
    """Synthetic trainer; average power 150 W, zone times 10/10 seconds."""
    return coach_icu.normalize_activity(
        {"type": "Ride", "start_date": "2026-11-06T00:00:00Z", "trainer": True,
         "icu_ctl": 40, "icu_atl": 50, "icu_training_load": 60},
        [{"type": "time", "data": [0, 10, 20]},
         {"type": "watts", "data": [100, 200, 100]}],
        settings or {"ftp": 250, "power_zones": [0, 150, 250]},
        {"resting_hr": 52, "sleep_secs": 27000})


class CoachEnglishTests(unittest.TestCase):
    def test_shared_fact_projection_and_legacy_default(self):
        record = synthetic()
        default = ride_review.build_review(record)
        explicit = ride_review.build_review(record, lang="zh")
        english = ride_review.build_review(record, lang="en")
        for review in (default, explicit):
            for key in ("id", "created_at", "updated_at"):
                review.pop(key)
        self.assertEqual(default, explicit)
        self.assertEqual(default["facts"]["metrics"], english["facts"]["metrics"])
        self.assertEqual(english["facts"]["metrics"]["average_power_w"]["value"], 150)
        self.assertNotIn("coach_context", default["facts"])

    def test_intervals_provenance_is_not_raw_bytes(self):
        review = coach_review_en.build_review(synthetic())
        self.assertEqual(review["source"]["kind"], "intervals_icu")
        self.assertEqual(review["source"]["hash_scope"], "canonical_python_json_inputs/1")
        record = synthetic(); record["source"]["hash_scope"] = "raw_file_bytes"
        with self.assertRaises(ride_review.ReviewError):
            coach_review_en.build_review(record)
        record = synthetic(); record["source"]["kind"] = "gpx"
        with self.assertRaises(ride_review.ReviewError):
            coach_review_en.build_review(record)

    def test_zones_are_seconds_not_sample_percentages(self):
        record = synthetic()
        record["segments"][0]["points"][2]["time"] = "2026-11-06T00:00:40Z"
        zones = coach_review_en.build_review(record)["facts"]["zones"]["power"]
        self.assertEqual([b["duration_s"] for b in zones["bins"]], [10, 30, 0])
        self.assertEqual([b["percentage"] for b in zones["bins"]], [25, 75, 0])
        self.assertEqual(zones["classified_duration_s"], 40)
        self.assertEqual(zones["used_edges"], 2)
        self.assertTrue(zones["basis_refs"])

    def test_exact_zone_boundaries_and_zero_power(self):
        record = synthetic()
        for point, power in zip(record["segments"][0]["points"], [0, 150, 250]):
            point["power_w"] = power
        bins = coach_review_en.build_review(record)["facts"]["zones"]["power"]["bins"]
        self.assertEqual([b["duration_s"] for b in bins], [10, 10, 0])

    def test_context_is_reported_with_refs_not_fatigue_diagnosis(self):
        facts = coach_review_en.build_review(synthetic())["facts"]
        for key, value in (("ctl", 40), ("atl", 50), ("training_load", 60),
                           ("resting_hr", 52), ("sleep_secs", 27000)):
            self.assertEqual(facts["coach_context"][key]["value"], value)
            self.assertTrue(facts["coach_context"][key]["evidence_ref"]["locator"])
        self.assertIn("not proof of fatigue", facts["messages"][-1])

    def test_missing_context_degrades_explicitly(self):
        review = coach_review_en.build_review(coach_icu.normalize_activity({"type": "Ride"}, []))
        self.assertIsNone(review["facts"]["zones"]["power"])
        self.assertTrue(all(v["value"] is None for v in review["facts"]["coach_context"].values()))
        self.assertIn("insufficient data", " ".join(review["facts"]["messages"]))
        self.assertEqual(review["rule_inferences"], [])

    def test_no_ftp_or_no_zones_never_uses_default(self):
        # Percent-of-FTP uppers without an FTP still degrade; watts without FTP too.
        for settings in ({"ftp": 250}, {"power_zones": [0, 150]},
                         {"power_zones": [55, 75, 90]}):
            self.assertIsNone(coach_review_en.build_review(synthetic(**settings))["facts"]["zones"]["power"])
        # Percent uppers WITH an FTP are converted, never discarded.
        review = coach_review_en.build_review(synthetic(ftp=250, power_zones=[50, 150]))
        zones = review["facts"]["zones"]["power"]
        self.assertIsNotNone(zones)
        self.assertEqual([b["lower_w"] for b in zones["bins"]], [0.0, 125.0])
        self.assertIn("percent-of-FTP", zones["limitations"][0])

    def test_gap_duplicate_reverse_and_missing_power_exclude_edges(self):
        for field, value, expected in (("time", "2026-11-06T01:00:00Z", None),
                                       ("time", "2026-11-06T00:00:00Z", 20),
                                       ("time", "2026-11-05T23:59:59Z", 21),
                                       ("time", None, None), ("power_w", None, None)):
            record = synthetic(); record["segments"][0]["points"][1][field] = value
            zones = coach_review_en.build_review(record)["facts"]["zones"]["power"]
            if expected is None:
                self.assertIsNone(zones)
            else:
                self.assertEqual(zones["classified_duration_s"], expected)
                self.assertEqual(zones["used_edges"], 1)
                self.assertEqual(zones["excluded_edges"], 1)

    def test_segments_not_bridged_and_partial_coverage_explicit(self):
        record = synthetic()
        record["segments"][0]["points"][2]["power_w"] = None
        record["segments"].append({"segment_index": 1, "points": [copy.deepcopy(record["segments"][0]["points"][0])]})
        zones = coach_review_en.build_review(record)["facts"]["zones"]["power"]
        self.assertEqual(zones["classified_duration_s"], 10)
        self.assertEqual(zones["excluded_edges"], 1)
        record["warnings"].append({"code": "capture_gap"})
        self.assertIsNone(coach_review_en.build_review(record)["facts"]["zones"]["power"])

    def test_english_templates_and_inference_evidence(self):
        record = synthetic()
        for name, value in (("total_distance_m", 1000), ("elevation_gain_m", 30)):
            record["metrics"][name].update(value=value, missing_reason=None)
        review = coach_review_en.build_review(record)
        self.assertTrue(review["rule_inferences"])
        for inference in review["rule_inferences"]:
            self.assertTrue(inference["basis_refs"])
            self.assertIn("[Inference]", inference["text"])
        texts = review["facts"]["messages"] + [i["text"] for i in review["rule_inferences"]]
        texts += [i["text"] + i["disclaimer"] for i in review["training_suggestion"]["items"]]
        self.assertFalse(any('\u4e00' <= ch <= '\u9fff' for ch in ''.join(texts)))
        self.assertEqual(review["ai_inferences"], [])
        self.assertIsNone(review["confirmation"])
        self.assertIsNone(review["calendar_authorization"])
        self.assertEqual(review["status"], "draft")

    def test_user_text_preserved_and_no_override_of_record_facts(self):
        review = coach_review_en.build_review(synthetic(),
            {"text": "用户自己的记录", "metrics": {"average_power_w": 999}})
        self.assertEqual(review["facts"]["metrics"]["average_power_w"]["value"], 150)
        self.assertEqual(review["user_statements"][0]["text"], "用户自己的记录")
        self.assertEqual(review["user_statements"][1]["text"], "User-reported measurement")

    def test_context_override_is_not_claimed_in_record_hash(self):
        context = {"ftp": 300, "power_zones": [0, 150, 300]}
        fact = coach_review_en.build_review(synthetic(), coach_context=context)["facts"]["coach_context"]["ftp"]
        self.assertEqual(fact["value"], 300)
        self.assertIsNone(fact["evidence_ref"]["source_sha256"])

    def test_invalid_language_and_context_rejected(self):
        with self.assertRaises(ride_review.ReviewError):
            ride_review.build_review(lang="fr")
        for context in ([], {"ftp": True}, {"ftp": 0}, {"ftp": float("nan")},
                        {"ftp": float("inf")}, {"ctl": -1}, {"unknown": 1},
                        {"power_zones": [0, 100, 90]}, {"power_zones": "0,100"}):
            with self.assertRaises(ride_review.ReviewError):
                coach_review_en.build_review(synthetic(), coach_context=context)

    def test_no_input_mutation_and_serializable(self):
        record = synthetic(); before = copy.deepcopy(record)
        json.dumps(coach_review_en.build_review(record), allow_nan=False)
        self.assertEqual(record, before)


if __name__ == "__main__":
    unittest.main()
