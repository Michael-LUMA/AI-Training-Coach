"""Offline-only door-eval runner tests; network is patched to explode."""
import io
import json
from contextlib import redirect_stdout, redirect_stderr
import unittest
from unittest.mock import patch

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import eval_door


class EvalDoorTests(unittest.TestCase):
    def setUp(self):
        self.cases = eval_door.load_corpus(eval_door.DEFAULT_CORPUS)
        for target in ("ride_chat._http_post", "ride_knowledge._http_post",
                       "ride_live._http_post", "socket.create_connection", "socket.socket"):
            guard = patch(target, side_effect=AssertionError("network is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_all_door_cases_pass_real_routing(self):
        self.assertEqual(len(self.cases), 22)
        report = eval_door.run_evaluation(self.cases)
        self.assertEqual(report["summary"]["pass"], 22)
        self.assertEqual(report["summary"]["total"], 22)
        self.assertEqual(report["summary"]["pass_rate"], 1.0)
        self.assertTrue(all(row["passed"] for row in report["cases"]))
        by_lane = report["summary"]["by_lane"]
        self.assertGreaterEqual(by_lane["live"], 3)  # live lane really exercised
        self.assertGreaterEqual(by_lane["personal"], 3)
        self.assertGreaterEqual(by_lane["knowledge"], 3)
        self.assertIn("door: 22/22 pass (100%)", self.output.getvalue())

    def test_corpus_validation_rejects_bad_shapes(self):
        bad_cases = (
            'not json',
            '{}',
            '[{"id":"x"}]',
            json.dumps([{"id": "Bad ID", "question": "问？", "expect": "ok", "note": "n"}]),
            json.dumps([{"id": "a", "question": "问？", "expect": "maybe", "note": "n"}]),
            json.dumps([{"id": "a", "question": "问？", "expect": "ok", "note": "n", "lane": "moon"}]),
            json.dumps([{"id": "a", "question": "问？", "expect": "ok", "note": "n"},
                        {"id": "a", "question": "问？", "expect": "ok", "note": "n"}]),
        )
        for index, raw in enumerate(bad_cases):
            with self.subTest(index=index):
                with patch.object(Path, "read_text", return_value=raw), \
                        redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as raised:
                        eval_door.main(["--corpus", "whatever.json"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("Invalid corpus", err.getvalue())

    def test_lane_mismatch_fails_case(self):
        case = {"id": "want-knowledge", "question": "我累计记录过几次骑行？",
                "expect": "ok", "lane": "knowledge", "note": "错道必须判失败。",
                "canned": "你累计记录了2次骑行。"}
        report = eval_door.run_evaluation([case])
        row = report["cases"][0]
        self.assertFalse(row["passed"])
        self.assertEqual(row["lane"], "personal")
        self.assertTrue(any("lane" in reason for reason in row["reasons"]))

    def test_fabricated_number_fails_verification_chain(self):
        case = {"id": "fabricate-count", "question": "我累计记录过几次骑行？",
                "expect": "ok", "note": "编造的次数必须被核验链拦下。",
                "canned": "你累计记录了99次骑行。", "accept_any": ["99次"]}
        report = eval_door.run_evaluation([case])
        row = report["cases"][0]
        self.assertFalse(row["passed"])
        self.assertNotEqual(row["status"], "ok")


if __name__ == "__main__":
    unittest.main()
