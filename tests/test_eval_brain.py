"""Offline-only eval-runner tests; file I/O probes use mocks, not extra files."""
import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools import eval_brain


class EvalBrainTests(unittest.TestCase):
    def setUp(self):
        self.cases = eval_brain.load_corpus(eval_brain.DEFAULT_CORPUS)
        # Fail closed if any test ever accidentally reaches a real transport.
        for target in ("ride_chat._http_post", "socket.create_connection", "socket.socket"):
            guard = patch(target, side_effect=AssertionError("network is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def run_mock(self, cases=None):
        return eval_brain.run_evaluation(self.cases if cases is None else cases,
                                         provider="mock")

    def assert_bad_corpus(self, raw, reason):
        with patch.object(Path, "read_text", return_value=raw), redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as raised:
                eval_brain.main(["--provider", "mock"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(reason, err.getvalue())

    def test_all_sixteen_mock_cases_pass_real_verification(self):
        self.assertEqual(len(self.cases), 16)
        with patch.object(eval_brain.ride_chat, "build_ride_chat",
                          wraps=eval_brain.ride_chat.build_ride_chat) as build:
            report = self.run_mock()
        self.assertEqual(report["summary"], {"pass": 16, "total": 16, "fallback": 0,
                                           "rejected": 0, "pass_rate": 1.0})
        self.assertTrue(all(row["passed"] and row["status"] == "ok" for row in report["cases"]))
        self.assertEqual(build.call_count, 16)
        for case, call in zip(self.cases, build.call_args_list):
            self.assertEqual(call.args, (case["question"],))
            self.assertIsNone(call.kwargs["session"])
            self.assertEqual(call.kwargs["recent_summaries"], case.get("recent_summaries"))
            self.assertEqual(call.kwargs["totals"], case.get("totals"))
            self.assertEqual(call.kwargs["activity_ordinal"], case.get("activity_ordinal"))
            self.assertTrue(callable(call.kwargs["http_post"]))
        self.assertIn("mock: 16/16 pass (100%), fallback 0, rejected 0", self.output.getvalue())

    def test_expect_fallback_fails_on_mock_ok(self):
        case = {"id": "want-fallback", "question": "你记到了吗？", "note": "核验状态不匹配。",
                "expect": "fallback"}
        report = self.run_mock([case])
        self.assertEqual(report["summary"]["pass"], 0)
        self.assertEqual(report["cases"][0]["status"], "ok")
        self.assertFalse(report["cases"][0]["passed"])

    def test_missing_canned_speech_replays_honest_unknown(self):
        case = {"id": "no-canned", "question": "爬升呢？", "note": "核验默认回放。",
                "expect": "ok", "accept_any": ["没记到"]}
        row = self.run_mock([case])["cases"][0]
        self.assertTrue(row["passed"])
        self.assertEqual(row["speech"], "这个我没记到。")

    def test_accept_any_checks_all_candidates(self):
        case = copy.deepcopy(self.cases[0])
        case["accept_any"] = ["不存在的文本", "128"]
        self.assertTrue(self.run_mock([case])["cases"][0]["passed"])
        case["accept_any"] = ["没有命中的文本"]
        self.assertFalse(self.run_mock([case])["cases"][0]["passed"])

    def test_reject_any_independently_fails_otherwise_ok_response(self):
        case = copy.deepcopy(self.cases[0])
        case["reject_any"] = ["平均心率"]
        row = self.run_mock([case])["cases"][0]
        self.assertEqual(row["status"], "ok")
        self.assertFalse(row["passed"])
        self.assertIn("reject_any", row["reasons"][0])

    def test_rejected_response_counts_as_fallback_and_discards_draft(self):
        case = {"id": "unregistered-number", "question": "我骑了多久？",
                "note": "核验虚构数字被真实核验链拒绝。", "expect": "fallback",
                "canned_speech": "记录了999分钟。"}
        report = self.run_mock([case])
        self.assertEqual(report["summary"]["fallback"], 1)
        self.assertEqual(report["summary"]["rejected"], 1)
        row = report["cases"][0]
        self.assertEqual(row["status"], "rejected_unverifiable_number")
        self.assertTrue(row["passed"])
        self.assertEqual(row["speech"], eval_brain.ride_chat.chat_template())
        self.assertNotIn("999", row["speech"])

    def test_non_list_corpus_exits_two(self):
        self.assert_bad_corpus('{}', "must be a list")

    def test_missing_required_fields_exits_two(self):
        for field in ("id", "question", "expect", "note"):
            with self.subTest(field=field):
                case = copy.deepcopy(self.cases[0])
                del case[field]
                self.assert_bad_corpus(json.dumps([case]), "missing required fields")

    def test_bad_expect_exits_two(self):
        for value in ("unknown", None, [], 1):
            with self.subTest(value=value):
                case = copy.deepcopy(self.cases[0])
                case["expect"] = value
                self.assert_bad_corpus(json.dumps([case]), "expect must be ok or fallback")

    def test_malformed_json_exits_two(self):
        self.assert_bad_corpus('[{', "Invalid corpus")
        self.assert_bad_corpus('[{"id":"a","id":"b"}]', "duplicate JSON key")

    def test_missing_or_non_utf8_file_exits_two(self):
        for error in (OSError("missing file"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            with self.subTest(error=type(error).__name__):
                with patch.object(Path, "read_text", side_effect=error), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        eval_brain.main(["--provider", "mock"])
                self.assertEqual(raised.exception.code, 2)

    def test_field_types_and_bounds_are_validated(self):
        invalid = [("id", "Not kebab"), ("question", ""), ("question", "问" * 201),
                   ("question", "问题\n"), ("question", 7), ("note", None),
                   ("accept_any", "128"), ("accept_any", []), ("accept_any", [1]),
                   ("reject_any", [None]), ("activity_ordinal", True),
                   ("activity_ordinal", -1), ("recent_summaries", {}),
                   ("recent_summaries", [None]), ("recent_summaries", [{"ascent_m": 7}]),
                   ("recent_summaries", [{"avg_hr": "128"}]),
                   ("recent_summaries", [{"avg_hr": True}]),
                   ("recent_summaries", [{"avg_hr": float("nan")}]),
                   ("totals", {}), ("totals", None), ("canned_speech", ""),
                   ("canned_speech", "话" * 301), ("unknown_field", 1)]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                case = copy.deepcopy(self.cases[0])
                case[field] = value
                self.assert_bad_corpus(json.dumps([case]), "Invalid corpus")

    def test_total_types_are_validated_and_null_distance_is_legal(self):
        for field, value in (("sessions_count", True), ("month_sessions", 1.5),
                             ("duration_min", None), ("distance_km", "120"),
                             ("month_duration_min", float("inf"))):
            with self.subTest(field=field):
                case = copy.deepcopy(self.cases[0])
                case["totals"][field] = value
                self.assert_bad_corpus(json.dumps([case]), "totals.")
        self.assertTrue(self.run_mock([self.cases[11]])["cases"][0]["passed"])

    def test_duplicate_id_and_empty_corpus_are_rejected(self):
        self.assert_bad_corpus(json.dumps([self.cases[0], self.cases[0]]), "duplicate id")
        self.assert_bad_corpus('[]', "at least one case")

    def test_report_written_as_utf8_contains_every_id_and_speech(self):
        # Observe the real writer call without creating a fourth on-disk file.
        with patch.object(Path, "write_text", autospec=True) as write:
            code = eval_brain.main(["--provider", "mock", "--out", "report.json"])
        self.assertEqual(code, 0)
        write.assert_called_once()
        args, kwargs = write.call_args
        self.assertEqual(args[0], Path("report.json"))
        self.assertEqual(kwargs["encoding"], "utf-8")
        report = json.loads(args[1])
        self.assertEqual(report["summary"]["pass"], 16)
        self.assertEqual([row["id"] for row in report["cases"]], [case["id"] for case in self.cases])
        for case, row in zip(self.cases, report["cases"]):
            self.assertEqual(row["speech"], case.get("canned_speech", "这个我没记到。"))
        self.assertIn("没记到", args[1])

    def test_main_returns_one_when_any_case_fails(self):
        case = {"id": "mismatch", "question": "有记录吗？", "note": "核验CLI失败码。",
                "expect": "fallback"}
        with patch.object(eval_brain, "load_corpus", return_value=[case]):
            self.assertEqual(eval_brain.main(["--provider", "mock"]), 1)

    def test_report_write_failure_exits_two(self):
        with patch.object(Path, "write_text", side_effect=OSError("read only")), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                eval_brain.main(["--provider", "mock", "--out", "report.json"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
