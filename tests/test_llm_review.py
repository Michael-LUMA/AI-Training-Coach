"""Offline module contract tests for the L2 cloud review engine.

中文摘要：所有 HTTP 均为测试桩；不读取真实密钥、不请求云端、不测端点/UI。
"""
from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_review as llm
from coach_review_en import DISCLAIMER

KEY = "synthetic-l2-key-not-a-real-credential"
_DEFAULT_POST = llm._http_post


def review_fixture():
    return {
        "facts": {
            "metrics": {
                "average_power_w": {"value": 175, "unit": "W", "missing_reason": None},
                "continuous_duration_s": {"value": 7200, "unit": "s"},
                "distance_m": {"value": 42000, "unit": "m"},
                "average_speed_mps": {"value": 5, "unit": "m/s"},
                "average_heart_rate_bpm": {"value": None, "unit": "bpm", "missing_reason": "not recorded"},
            },
            "messages": ["Only reported facts are available."],
        },
        "training_suggestion": {"text": "Do not use this rule's 180 as a fact."},
        "user_statements": [{"text": "Private statement 180 W"}],
        "source": {"sha256": "abc180def"},
    }


def draft_fixture(text="Average power was 175W."):
    return {
        "summary": text,
        "observations": [{"text": "Heart rate was not recorded.",
                          "fact_refs": ["facts.metrics.average_heart_rate_bpm"]}],
        "next_ride_note": "Inspect the recorded data before making your own decision.",
    }


def reply(draft=None, *, usage=None, status=200, finish="stop"):
    envelope = {"choices": [{"finish_reason": finish, "message": {
        "content": json.dumps(draft_fixture() if draft is None else draft)}}]}
    if usage is not None:
        envelope["usage"] = usage
    return status, {}, json.dumps(envelope).encode("utf-8")


class LLMReviewTests(unittest.TestCase):
    def setUp(self):
        self.review = review_fixture()
        # A missing injection must never accidentally call a real network transport.
        self.guard = patch.object(llm, "_http_post", side_effect=AssertionError("Real HTTP forbidden"))
        self.network = self.guard.start()
        self.addCleanup(self.guard.stop)

    def build(self, draft=None, *, usage=None, **kwargs):
        transport = Mock(return_value=reply(draft, usage=usage))
        result = llm.build_llm_review(self.review, api_key=KEY, http_post=transport, **kwargs)
        self.assertEqual(transport.call_count, 1)
        return result

    def assert_fallback(self, result, status):
        self.assertEqual(result["llm_status"], status)
        self.assertIsNone(result["draft"])
        self.assertEqual(result["fallback"], "rule_draft")
        self.assertEqual(set(result), {"llm_status", "draft", "usage", "fallback"})

    def test_not_configured_without_transport_or_review_validation(self):
        for key in (None, "", " \t\n"):
            with self.subTest(key=key):
                transport = Mock(side_effect=AssertionError("Must not call transport"))
                result = llm.build_llm_review(None, api_key=key, http_post=transport)
                self.assert_fallback(result, "not_configured")
                self.assertIsNone(result["usage"])
                transport.assert_not_called()
        self.network.assert_not_called()

    def test_explicit_key_contract_does_not_read_environment(self):
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": KEY}):
            self.assert_fallback(llm.build_llm_review(self.review, api_key=None), "not_configured")
        self.network.assert_not_called()

    def test_ok_175w_fixed_disclaimer_and_no_input_mutation(self):
        before = copy.deepcopy(self.review)
        result = self.build()
        self.assertEqual(result["llm_status"], "ok")
        self.assertIsNone(result["fallback"])
        self.assertIsNone(result["usage"])
        self.assertEqual(result["draft"]["summary"], "Average power was 175W.")
        self.assertEqual(result["draft"]["disclaimer"], "AI-assisted draft. " + DISCLAIMER)
        self.assertEqual(self.review, before)

    def test_unverifiable_180w_rejects_entire_draft(self):
        result = self.build(draft_fixture("Average power was 180W."))
        self.assert_fallback(result, "rejected_unverifiable_number")

    def test_time_unit_conversions(self):
        for text in ("Duration was 7200s.", "Duration was 120min.", "Duration was 2h.",
                     "Duration was 120 minutes or 2.0 hours."):
            with self.subTest(text=text):
                self.assertEqual(self.build(draft_fixture(text))["llm_status"], "ok")
        for text in ("Duration was 120.5min.", "Duration was 2.1h."):
            with self.subTest(text=text):
                self.assert_fallback(self.build(draft_fixture(text)), "rejected_unverifiable_number")

    def test_decimal_120_point_5_minutes_is_allowed_only_if_equivalent(self):
        self.review["facts"]["metrics"]["continuous_duration_s"]["value"] = 7230
        self.assertEqual(self.build(draft_fixture("Duration was 120.5min."))["llm_status"], "ok")

    def test_same_precision_rounding_and_no_broad_tolerance(self):
        self.review["facts"]["metrics"]["average_power_w"]["value"] = 175.25
        for text in ("Power was 175W.", "Power was 175.3 W."):
            with self.subTest(text=text):
                self.assertEqual(self.build(draft_fixture(text))["llm_status"], "ok")
        self.assert_fallback(self.build(draft_fixture("Power was 175.4 W.")), "rejected_unverifiable_number")

    def test_exact_ties_accepted_in_both_rounding_directions(self):
        # 175.25 -> 175.2 (ties-to-even / truncation) is as truthful as 175.3.
        self.review["facts"]["metrics"]["average_power_w"]["value"] = 175.25
        for text in ("Power was 175.2 W.", "Power was 175.3 W.", "Power was 175W."):
            with self.subTest(text=text):
                self.assertEqual(self.build(draft_fixture(text))["llm_status"], "ok")

    def test_integer_ties_accepted_in_both_directions(self):
        self.review["facts"]["metrics"]["average_power_w"]["value"] = 174.5
        for text in ("Power was 174W.", "Power was 175W."):
            with self.subTest(text=text):
                self.assertEqual(self.build(draft_fixture(text))["llm_status"], "ok")

    def test_beyond_half_a_unit_remains_fabrication(self):
        self.review["facts"]["metrics"]["average_power_w"]["value"] = 175.26
        self.assert_fallback(self.build(draft_fixture("Power was 175.2 W.")), "rejected_unverifiable_number")
        self.review["facts"]["metrics"]["average_power_w"]["value"] = 174.51
        self.assert_fallback(self.build(draft_fixture("Power was 173W.")), "rejected_unverifiable_number")

    def test_scientific_notation_cannot_relax_integer_precision(self):
        for text in ("Power was 1.8e2 W.", "Power was 18e1 W.", "Power was .18e3 W."):
            with self.subTest(text=text):
                self.assert_fallback(self.build(draft_fixture(text)), "rejected_unverifiable_number")
        self.assertEqual(self.build(draft_fixture("Power was 1.75e2 W."))["llm_status"], "ok")

    def test_units_cannot_launder_power_from_duration_or_missing_heart_rate(self):
        self.review["facts"]["metrics"]["continuous_duration_s"]["value"] = 180
        for text in ("Power was 180W.", "Heart rate was 175bpm.", "Duration was 175s."):
            with self.subTest(text=text):
                self.assert_fallback(self.build(draft_fixture(text)), "rejected_unverifiable_number")

    def test_distance_speed_and_grouped_number_equivalents(self):
        for text in ("Distance was 42km.", "Distance was 42,000m.", "Speed was 18km/h."):
            with self.subTest(text=text):
                self.assertEqual(self.build(draft_fixture(text))["llm_status"], "ok")

    def test_all_prose_fields_are_checked_not_just_summary(self):
        draft = draft_fixture()
        draft["next_ride_note"] = "Record another 999 minutes."
        self.assert_fallback(self.build(draft), "rejected_unverifiable_number")
        draft = draft_fixture()
        draft["observations"][0]["text"] = "Power was 180W."
        self.assert_fallback(self.build(draft), "rejected_unverifiable_number")

    def test_raw_samples_hashes_dates_and_rule_numbers_are_not_evidence(self):
        self.review["facts"]["segments"] = [{"points": [{"power_w": 180}]}]
        self.review["facts"]["metrics"]["average_power_w"]["evidence_ref"] = {"source_sha256": "180"}
        self.review["facts"]["messages"].append("Untrusted message: 180 W on 2026-11-07.")
        self.assert_fallback(self.build(draft_fixture("Power was 180W.")), "rejected_unverifiable_number")

    def test_negative_boolean_and_unsupported_numerals_do_not_match(self):
        self.review["facts"]["metrics"]["boolean"] = {"value": True, "unit": "W"}
        for text in ("Power was -175W.", "Power was −175W.", "Power was 1W.",
                     "Power was １８０W.", "Power was 175kW."):
            with self.subTest(text=text):
                self.assert_fallback(self.build(draft_fixture(text)), "rejected_unverifiable_number")

    def test_zone_refs_are_existing_paths_not_freeform_claims(self):
        self.review["facts"]["zones"] = {"power": {"bins": [{"zone": 2, "percentage": 100}]}}
        draft = draft_fixture("Z2 covered 100% of classified time.")
        draft["observations"][0] = {"text": "Z2 covered 100%.", "fact_refs": ["facts.zones.power.bins[0]"]}
        self.assertEqual(self.build(draft)["llm_status"], "ok")
        draft["observations"][0]["fact_refs"] = ["facts.zones.power.bins[999]"]
        self.assert_fallback(self.build(draft), "rejected_bad_schema")

    def test_bad_schema_missing_extra_keys_types_and_unknown_refs(self):
        bad = [[], {"summary": "Missing fields"}]
        for field, value in (("summary", None), ("summary", " "), ("next_ride_note", 175),
                             ("observations", {}), ("observations", ["text"]),
                             ("observations", [{"text": "Recorded", "fact_refs": []}]),
                             ("observations", [{"text": "Recorded", "fact_refs": ["facts.fake"]}]),
                             ("observations", [{"text": "Recorded", "fact_refs": [True]}]),
                             ("disclaimer", "Override")):
            draft = draft_fixture(); draft[field] = value; bad.append(draft)
        for draft in bad:
            with self.subTest(draft=draft):
                self.assert_fallback(self.build(draft), "rejected_bad_schema")

    def test_bad_json_duplicate_keys_nonfinite_and_markdown_are_rejected(self):
        for content in ("not JSON", "```json\n{}\n```", '{"summary":"A","summary":"B"}',
                        '{"summary":NaN}', '{"summary":Infinity}'):
            with self.subTest(content=content):
                transport = Mock(return_value=(200, {}, json.dumps({"choices": [{
                    "message": {"content": content}}]}).encode()))
                self.assert_fallback(llm.build_llm_review(self.review, api_key=KEY, http_post=transport),
                                     "rejected_bad_schema")

    def test_truncation_refusal_or_missing_message_rejected_with_usage(self):
        for choices in ([], [None], [{"finish_reason": "length", "message": {"content": json.dumps(draft_fixture())}}],
                        [{"message": {"content": None}}], [{"message": {"refusal": "Refused"}}]):
            with self.subTest(choices=choices):
                transport = Mock(return_value=(200, {}, json.dumps({"choices": choices,
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3}}).encode()))
                result = llm.build_llm_review(self.review, api_key=KEY, http_post=transport)
                self.assert_fallback(result, "rejected_bad_schema")
                self.assertEqual(result["usage"], {"prompt_tokens": 12, "completion_tokens": 3})

    def test_usage_transmitted_even_when_number_rejected(self):
        usage = {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133}
        for text, status in (("Power was 175W.", "ok"), ("Power was 180W.", "rejected_unverifiable_number")):
            with self.subTest(text=text):
                result = self.build(draft_fixture(text), usage=usage)
                self.assertEqual(result["llm_status"], status)
                self.assertEqual(result["usage"], {"prompt_tokens": 111, "completion_tokens": 22})

    def test_missing_and_partial_invalid_usage_not_estimated(self):
        self.assertIsNone(self.build()["usage"])
        cases = [({}, {"prompt_tokens": None, "completion_tokens": None}),
                 ({"prompt_tokens": 0}, {"prompt_tokens": 0, "completion_tokens": None}),
                 ({"prompt_tokens": True, "completion_tokens": -1}, {"prompt_tokens": None, "completion_tokens": None}),
                 ({"prompt_tokens": "100", "completion_tokens": 2.5}, {"prompt_tokens": None, "completion_tokens": None})]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.build(usage=raw)["usage"], expected)

    def test_http_errors_no_retries_or_sensitive_error_echo(self):
        for status in (301, 401, 429, 500, 503):
            with self.subTest(status=status):
                transport = Mock(return_value=(status, {}, json.dumps({"error": {
                    "message": KEY}, "usage": {"prompt_tokens": 5}}).encode()))
                result = llm.build_llm_review(self.review, api_key=KEY, http_post=transport)
                self.assert_fallback(result, "upstream_error")
                transport.assert_called_once()
                self.assertNotIn(KEY, json.dumps(result))
                self.assertEqual(result["usage"], {"prompt_tokens": 5, "completion_tokens": None})

    def test_timeout_urlerror_and_oserror_mapping_no_retry(self):
        for error in (TimeoutError(KEY), URLError(TimeoutError(KEY)), OSError(KEY)):
            with self.subTest(error=type(error).__name__):
                transport = Mock(side_effect=error)
                result = llm.build_llm_review(self.review, api_key=KEY, http_post=transport)
                self.assert_fallback(result, "upstream_error")
                self.assertIsNone(result["usage"])
                self.assertNotIn(KEY, json.dumps(result))
                transport.assert_called_once()

    def test_injected_http_error_is_closed_and_not_echoed(self):
        stream = io.BytesIO(KEY.encode())
        transport = Mock(side_effect=HTTPError(llm.ENDPOINT, 401, KEY, {}, stream))
        result = llm.build_llm_review(self.review, api_key=KEY, http_post=transport)
        self.assert_fallback(result, "upstream_error")
        self.assertTrue(stream.closed)
        self.assertNotIn(KEY, json.dumps(result))

    def test_invalid_transport_and_envelope_fail_closed(self):
        responses = [None, (200, {}, "text"), ("200", {}, b"{}"), (200, None, b"{}"),
                     (200, {}, b"\xff"), (200, {}, b"not json"), (200, {}, b"[]"),
                     (200, {}, b'{"error":"failure"}'),
                     (200, {}, b'{"usage":{"prompt_tokens":NaN}}'),
                     (200, {}, b'{"choices":[],"choices":[]}'),
                     (200, {}, b"x" * (llm.MAX_RESPONSE_BYTES + 1))]
        for response in responses:
            with self.subTest(response=str(response)[:80]):
                result = llm.build_llm_review(self.review, api_key=KEY, http_post=Mock(return_value=response))
                self.assert_fallback(result, "upstream_error")

    def test_request_endpoint_model_budget_timeout_format_and_prompt(self):
        transport = Mock(return_value=reply())
        llm.build_llm_review(self.review, api_key=KEY, http_post=transport)
        transport.assert_called_once()
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, llm.ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(transport.call_args.kwargs, {"timeout": 20})
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "qwen-flash")
        self.assertEqual(payload["max_tokens"], 800)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertIn('"not recorded"', payload["messages"][0]["content"])
        self.assertIn("medical advice", payload["messages"][0]["content"])
        facts = json.loads(payload["messages"][1]["content"])
        self.assertEqual(set(facts), {"facts"})
        self.assertNotIn("Private statement", request.data.decode())
        self.assertNotIn(KEY, request.data.decode())

    def test_invalid_review_or_oversized_nonfinite_input_never_calls_transport(self):
        reviews = [None, [], {}, {"facts": []}, {"facts": {"metrics": {"value": float("nan")}}},
                   {"facts": {"messages": ["x" * (llm.MAX_FACTS_BYTES + 1)]}}]
        cyclic = {}; cyclic["metrics"] = cyclic; reviews.append({"facts": cyclic})
        for review in reviews:
            with self.subTest(review_type=type(review).__name__):
                transport = Mock(side_effect=AssertionError("Unexpected HTTP"))
                self.assert_fallback(llm.build_llm_review(review, api_key=KEY, http_post=transport),
                                     "rejected_bad_schema")
                transport.assert_not_called()

    def test_invalid_api_key_never_enters_header_or_transport(self):
        for key in (True, 123, "bad\r\nHeader", "space key", "非ASCII", "x" * 4097):
            with self.subTest(key_type=type(key).__name__):
                transport = Mock(side_effect=AssertionError("Unexpected HTTP"))
                self.assert_fallback(llm.build_llm_review(self.review, api_key=key, http_post=transport),
                                     "upstream_error")
                transport.assert_not_called()

    def test_real_coach_review_aggregate_175_accepts_but_sample_180_rejected(self):
        from coach_demo import build_demo_review
        review = build_demo_review()["review"]
        for power, status in ((175, "ok"), (180, "rejected_unverifiable_number")):
            with self.subTest(power=power):
                transport = Mock(return_value=reply(draft_fixture(f"Average power was {power}W.")))
                result = llm.build_llm_review(review, api_key=KEY, http_post=transport)
                self.assertEqual(result["llm_status"], status)
                sent = json.loads(json.loads(transport.call_args.args[0].data)["messages"][1]["content"])
                self.assertNotIn("segments", sent["facts"])
                self.assertNotIn("warnings", sent["facts"])
                self.assertNotIn("source_sha256", json.dumps(sent))

    def test_default_transport_bounded_read_timeout_and_no_redirect(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200; response.headers = {}; response.read.return_value = b"{}"
        opener = Mock(); opener.open.return_value = response
        request = Request(llm.ENDPOINT, data=b"{}", method="POST")
        with patch.object(llm, "build_opener", return_value=opener) as factory:
            self.assertEqual(_DEFAULT_POST(request, timeout=20), (200, {}, b"{}"))
        opener.open.assert_called_once_with(request, timeout=20)
        response.read.assert_called_once_with(llm.MAX_RESPONSE_BYTES + 1)
        proxy, redirect = factory.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirect, llm._NoRedirect)
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://evil.invalid/"))

    def test_default_transport_oversize_and_error_body_not_read(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"x" * (llm.MAX_RESPONSE_BYTES + 1)
        opener = Mock(); opener.open.return_value = response
        with patch.object(llm, "build_opener", return_value=opener), self.assertRaises(ValueError):
            _DEFAULT_POST(Request(llm.ENDPOINT), timeout=20)
        stream = Mock()
        error = HTTPError(llm.ENDPOINT, 401, KEY, {}, stream)
        opener.open.side_effect = error
        with patch.object(llm, "build_opener", return_value=opener):
            self.assertEqual(_DEFAULT_POST(Request(llm.ENDPOINT), timeout=20), (401, {}, b""))
        stream.read.assert_not_called()
        stream.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
