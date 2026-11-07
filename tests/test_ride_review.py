"""Synthetic dict inputs only. No cloud, service, filesystem data or workouts."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ride_review


def metric(value, unit, reason=None):
    return {"value": value, "unit": unit, "origin": "calculated", "method": "synthetic/1",
            "missing_reason": reason, "coverage": {"used_edges": 2, "excluded_time_edges": 0}}


def complete_record(kind="gpx"):
    return {
        "schema_version": "luma-ride-gpx/1",
        "source": {"kind": kind, "sha256": "a" * 64, "hash_scope": "raw_file_bytes", "byte_length": 1234},
        "metrics": {
            "total_distance_m": metric(10000, "m"), "total_duration_s": metric(3600, "s"),
            "elevation_gain_m": metric(300, "m"), "average_heart_rate_bpm": metric(120, "bpm"),
            "average_power_w": metric(150, "W"),
        },
        "point_count": 3,
        "segments": [{"segment_index": 0, "points": [
            {"segment_index": 0, "point_index": i, "time": None, "lat": None, "lon": None,
             "ele": None, "heart_rate_bpm": None, "power_w": None} for i in range(3)
        ]}],
        "warnings": [], "limitations": ["Synthetic dict only"],
    }


class RideReviewTests(unittest.TestCase):
    def assert_suggestions(self, review):
        suggestion = review["training_suggestion"]
        self.assertEqual(suggestion["origin"], "rule_template")
        self.assertEqual(suggestion["template_version"], ride_review.TEMPLATE_VERSION)
        self.assertTrue(1 <= len(suggestion["items"]) <= 3)
        self.assertIn("不宣称训练科学效果", suggestion["disclaimer"])
        self.assertIsNone(suggestion["scheduled_start"])
        self.assertIsNone(suggestion["duration_s"])
        for item in suggestion["items"]:
            self.assertEqual(item["origin"], "rule_template")
            self.assertTrue(item["template_id"])
            self.assertEqual(item["disclaimer"], suggestion["disclaimer"])

    def test_complete_facts_version_and_draft_lifecycle(self):
        review = ride_review.build_review(complete_record())
        self.assertEqual(review["schema_version"], "luma-ride-review/1-draft")
        self.assertEqual(review["revision"], 1)
        self.assertTrue(review["id"])
        self.assertEqual(review["status"], "draft")
        self.assertTrue(review["created_at"].endswith("Z"))
        self.assertEqual(review["created_at"], review["updated_at"])
        for key in ("feedback", "calendar_authorization", "confirmation"):
            self.assertIsNone(review[key])
        self.assertEqual(review["receipts"], [])
        self.assertEqual(review["ai_inferences"], [])
        self.assertFalse(review["source"]["storage_consent"])
        for name, value, unit, locator in (
            ("distance_m", 10000, "m", "total_distance_m"),
            ("continuous_duration_s", 3600, "s", "total_duration_s"),
            ("elevation_gain_m", 300, "m", "elevation_gain_m"),
            ("average_heart_rate_bpm", 120, "bpm", "average_heart_rate_bpm"),
            ("average_power_w", 150, "W", "average_power_w"),
        ):
            fact = review["facts"]["metrics"][name]
            self.assertEqual((fact["value"], fact["unit"], fact["origin"]), (value, unit, "calculated"))
            self.assertEqual(fact["evidence_ref"], {"source_sha256": "a" * 64, "locator": "metrics." + locator + ".value"})
            self.assertIsNone(fact["missing_reason"])
        self.assert_suggestions(review)

    def test_continuous_time_not_mislabeled_as_elapsed_or_moving(self):
        review = ride_review.build_review(complete_record())
        for name in ("elapsed_duration_s", "moving_duration_s", "average_speed_mps"):
            metric_value = review["facts"]["metrics"][name]
            self.assertIsNone(metric_value["value"])
            self.assertIsNone(metric_value["evidence_ref"])
            self.assertEqual(metric_value["missing_reason"], "not_recorded")
        self.assertIn("本次无移动时长数据", review["facts"]["messages"])

    def test_missing_heart_rate_explicit_null_and_message(self):
        record = complete_record()
        record["metrics"]["average_heart_rate_bpm"] = metric(None, "bpm", "incomplete_sensor_sequence")
        review = ride_review.build_review(record)
        fact = review["facts"]["metrics"]["average_heart_rate_bpm"]
        self.assertIsNone(fact["value"])
        self.assertEqual(fact["missing_reason"], "incomplete_sensor_sequence")
        self.assertIn("本次无心率数据", review["facts"]["messages"])
        self.assert_suggestions(review)

    def test_missing_power_key_is_not_skipped_or_filled(self):
        record = complete_record()
        del record["metrics"]["average_power_w"]
        review = ride_review.build_review(record)
        fact = review["facts"]["metrics"]["average_power_w"]
        self.assertIsNone(fact["value"])
        self.assertIsNone(fact["evidence_ref"])
        self.assertIn("本次无功率数据", review["facts"]["messages"])
        self.assert_suggestions(review)

    def test_empty_manual_summary_has_no_inferences_or_substantive_prescription(self):
        review = ride_review.build_review(manual_summary={})
        self.assertEqual(review["source"]["kind"], "manual_summary")
        self.assertIsNone(review["source"]["sha256"])
        self.assertEqual(review["user_statements"], [])
        self.assertEqual(review["rule_inferences"], [])
        self.assertEqual(review["ai_inferences"], [])
        self.assertTrue(all(v["value"] is None for v in review["facts"]["metrics"].values()))
        for label in ("距离", "有效连续时长", "爬升", "心率", "功率"):
            self.assertIn("本次无" + label + "数据", review["facts"]["messages"])
        self.assert_suggestions(review)
        self.assertEqual([i["kind"] for i in review["training_suggestion"]["items"]], ["data_quality"])

    def test_entirely_null_manual_metrics_do_not_trigger_rules(self):
        review = ride_review.build_review(manual_summary={"metrics": {m[0]: None for m in ride_review.METRICS}})
        self.assertEqual(review["user_statements"], [])
        self.assertEqual(review["rule_inferences"], [])
        self.assertEqual(len(review["training_suggestion"]["items"]), 1)

    def test_manual_values_are_user_statements_and_do_not_overwrite_facts(self):
        review = ride_review.build_review(complete_record(), {"metrics": {"distance_m": 999, "average_power_w": 999}})
        self.assertEqual(review["facts"]["metrics"]["distance_m"]["value"], 10000)
        self.assertEqual(review["facts"]["metrics"]["average_power_w"]["value"], 150)
        self.assertEqual([s["value"] for s in review["user_statements"]], [999, 999])
        self.assertTrue(all(s["origin"] == "user" for s in review["user_statements"]))

    def test_manual_only_rules_cite_statements_not_fabricated_file_evidence(self):
        review = ride_review.build_review(manual_summary={"metrics": {"distance_m": 1000, "elevation_gain_m": 30}})
        self.assertIsNone(review["facts"]["metrics"]["distance_m"]["value"])
        inference = review["rule_inferences"][0]
        self.assertEqual(inference["basis_refs"], [
            {"source_sha256": None, "locator": "user_statements[0]"},
            {"source_sha256": None, "locator": "user_statements[1]"},
        ])
        self.assert_suggestions(review)

    def test_every_inference_is_labeled_rule_and_cites_present_fields(self):
        record = complete_record()
        review = ride_review.build_review(record)
        self.assertEqual(len(review["rule_inferences"]), 1)
        for inference in review["rule_inferences"]:
            self.assertIn("[推测]", inference["text"])
            self.assertEqual(inference["origin"], "rule_template")
            self.assertEqual(inference["template_version"], ride_review.TEMPLATE_VERSION)
            self.assertTrue(inference["limitations"])
            for ref in inference["basis_refs"]:
                _, key, value = ref["locator"].split(".")
                self.assertIsNotNone(record["metrics"][key][value])
                self.assertEqual(ref["source_sha256"], record["source"]["sha256"])
        self.assertEqual(review["ai_inferences"], [])

    def test_rules_do_not_trigger_without_distance_or_gain_or_positive_distance(self):
        for field, value in (("total_distance_m", None), ("total_distance_m", 0), ("elevation_gain_m", None),
                             ("elevation_gain_m", 199)):
            with self.subTest(field=field, value=value):
                record = complete_record()
                record["metrics"][field]["value"] = value
                record["metrics"][field]["missing_reason"] = "not_recorded" if value is None else None
                self.assertEqual(ride_review.build_review(record)["rule_inferences"], [])

    def test_zero_power_is_not_missing(self):
        record = complete_record()
        record["metrics"]["average_power_w"]["value"] = 0
        review = ride_review.build_review(record)
        self.assertEqual(review["facts"]["metrics"]["average_power_w"]["value"], 0)
        self.assertNotIn("本次无功率数据", review["facts"]["messages"])

    def test_manual_text_hash_preserves_whitespace_newlines_and_unicode(self):
        text = "  合成骑行\r\n备注：未测心率  "
        review = ride_review.build_review(manual_summary={"text": text})
        self.assertEqual(review["source"]["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(review["source"]["hash_scope"], "manual_text_utf8_exact")
        self.assertEqual(review["source"]["byte_length"], len(text.encode("utf-8")))
        self.assertEqual(review["user_statements"][0]["text"], text)
        self.assertEqual(review["rule_inferences"], [])

    def test_overlong_input_is_explicitly_rejected_not_truncated(self):
        with self.assertRaises(ride_review.ReviewError) as caught:
            ride_review.build_review(manual_summary={"text": "测" * (ride_review.MAX_TEXT_CHARS + 1)})
        self.assertEqual(caught.exception.code, "input_too_large")
        accepted = "x" * ride_review.MAX_TEXT_CHARS
        self.assertEqual(ride_review.build_review(manual_summary={"text": accepted})["user_statements"][0]["text"], accepted)
        with patch.object(ride_review, "MAX_INPUT_BYTES", 128):
            with self.assertRaises(ride_review.ReviewError) as caught:
                ride_review.build_review(manual_summary={"text": "x" * 200})
            self.assertEqual(caught.exception.code, "input_too_large")

    def test_cyclic_deep_nonfinite_and_non_json_inputs_rejected(self):
        cycle = {}
        cycle["text"] = cycle
        cases = [cycle, {"metrics": {"distance_m": float("nan")}}, {"metrics": {"distance_m": float("inf")}},
                 {"text": object()}, {"text": "\ud800"}]
        for summary in cases:
            with self.subTest(kind=str(type(summary))):
                with self.assertRaises(ride_review.ReviewError):
                    ride_review.build_review(manual_summary=summary)
        deep = []
        for _ in range(ride_review.MAX_INPUT_DEPTH + 1):
            deep = [deep]
        with self.assertRaises(ride_review.ReviewError) as caught:
            ride_review.build_review(manual_summary={"text": deep})
        self.assertEqual(caught.exception.code, "input_too_large")

    def test_invalid_metrics_units_source_and_unknown_manual_fields_rejected(self):
        for summary in ({"unexpected": True}, {"metrics": {"unknown": 1}}, {"metrics": {"distance_m": -1}},
                        {"metrics": {"distance_m": True}}, {"metrics": {"average_heart_rate_bpm": 0}}):
            with self.assertRaises(ride_review.ReviewError):
                ride_review.build_review(manual_summary=summary)
        for key, value in (("unit", "km"), ("origin", "user"), ("missing_reason", "not_recorded")):
            record = complete_record()
            record["metrics"]["total_distance_m"][key] = value
            with self.assertRaises(ride_review.ReviewError):
                ride_review.build_review(record)
        record = complete_record()
        record["source"]["sha256"] = "not-a-hash"
        with self.assertRaises(ride_review.ReviewError):
            ride_review.build_review(record)

    def test_no_input_mutation_or_shared_mutable_output(self):
        record = complete_record()
        summary = {"metrics": {"distance_m": 999}, "text": "synthetic"}
        before = copy.deepcopy((record, summary))
        review = ride_review.build_review(record, summary)
        self.assertEqual((record, summary), before)
        review["facts"]["segments"][0]["points"][0]["lat"] = 42
        review["facts"]["metrics"]["distance_m"]["coverage"]["used_edges"] = 999
        self.assertEqual((record, summary), before)

    def test_gpx_tcx_have_same_review_semantics_and_output_is_strict_json(self):
        a = ride_review.build_review(complete_record("gpx"))
        b = ride_review.build_review(complete_record("tcx"))
        self.assertEqual(a["facts"], b["facts"])
        self.assertEqual(a["rule_inferences"], b["rule_inferences"])
        self.assertEqual(a["training_suggestion"], b["training_suggestion"])
        self.assertIsInstance(json.dumps(a, allow_nan=False, ensure_ascii=False), str)
        self.assertIsNone(a["feedback"])


if __name__ == "__main__":
    unittest.main()
