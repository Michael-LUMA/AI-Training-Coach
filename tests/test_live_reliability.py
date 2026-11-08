"""Regression checks for current telemetry, freshness and client failover. Offline only."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import coach_server
import test_ride_ui as ui


class LiveReliabilityEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.post = Mock(side_effect=AssertionError("No real upstream calls allowed"))
        self.app = coach_server.App(store=SimpleNamespace(path=Path(self.temp.name) / "coach.sqlite3"), llm_post=self.post)
        self.env = patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Host": "127.0.0.1:8794", "Origin": "http://127.0.0.1:8794",
                   "Content-Type": "application/json", "Content-Length": str(len(body)),
                   "X-Coach-Token": self.app.token}
        return self.app.dispatch("POST", path, headers, body)

    def feed(self, i=0, **metrics):
        point = {"t": 1700000000 + i * 10, "lat": 31.0, "lng": 121.0 + i * .00001, "speed_mps": 5.0}
        point.update(metrics)
        return self.request("/api/coach/ride/live", {"live_id": "reliability", "point": point})

    def ask(self, question="我现在心率多少", live_id="reliability"):
        return self.request("/api/coach/ride/chat", {"question": question, "live_id": live_id})

    def test_heart_rate_and_speed_follow_latest_point(self):
        self.feed(hr=100)
        self.feed(1, hr=160, speed_mps=6)
        self.assertEqual(self.ask()["speech"], "现在心率160。")
        self.assertEqual(self.ask("我现在速度多少")["speech"], "现在速度21.6公里每小时。")
        self.post.assert_not_called()

    def test_heart_rate_can_arrive_after_first_point(self):
        self.feed()
        self.feed(1, hr=160)
        self.assertEqual(self.ask()["speech"], "现在心率160。")

    def test_missing_new_heart_rate_never_reuses_old_value(self):
        self.feed(hr=100)
        self.feed(1)
        self.assertEqual(self.ask()["speech"], "这个我现在看不到。")
        self.post.assert_not_called()

    def test_live_storage_keeps_one_snapshot_not_every_point(self):
        for i in range(100):
            with patch.object(coach_server.time, "monotonic", return_value=1000 + i * 10):
                self.feed(i, hr=100 + i % 50)
        self.assertEqual(len(self.app._live["reliability"]), 4)

    def test_stale_session_does_not_answer_current_metrics(self):
        with patch.object(coach_server.time, "monotonic", return_value=1000):
            self.feed(hr=100)
        with patch.object(coach_server.time, "monotonic", return_value=1031):
            result = self.ask()
            duration = self.ask("我现在运动多久了")
        self.assertEqual(result["lane"], "live")
        self.assertEqual(result["speech"], "这个我现在看不到。")
        self.assertEqual(duration["speech"], "这个我现在看不到。")
        self.post.assert_not_called()

    def test_unknown_live_session_never_falls_back_to_history(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "synthetic-key"}):
            result = self.ask(live_id="unknown")
        self.assertEqual(result["lane"], "live")
        self.assertEqual(result["speech"], "这个我现在看不到。")
        self.post.assert_not_called()

    def test_bad_sample_does_not_refresh_valid_snapshot(self):
        with patch.object(coach_server.time, "monotonic", return_value=1000):
            self.feed(hr=100)
        with patch.object(coach_server.time, "monotonic", return_value=1031):
            with self.assertRaises(coach_server.ActionError):
                self.feed(1, hr=999)
            result = self.ask()
        self.assertEqual(result["speech"], "这个我现在看不到。")
        self.assertEqual(self.app._live["reliability"][2], 1000)

    def test_live_answer_failure_does_not_switch_to_personal_history(self):
        self.feed(hr=100)
        failed = {"llm_status": "error_upstream", "speech": "这个我现在看不到。", "fallback": "template", "usage": None}
        with patch.object(coach_server, "build_live_answer", return_value=failed), \
                patch.object(coach_server, "build_ride_chat", side_effect=AssertionError("Do not answer current questions from history")):
            result = self.ask()
        self.assertEqual(result["lane"], "live")
        self.assertEqual(result["speech_status"], "error_upstream")


class LiveReliabilityUI(unittest.TestCase):
    run_js = ui.RideMemoryUI.run_js

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static/ride.html").read_text(encoding="utf-8")

    def test_live_errors_and_malformed_calls_do_not_claim_healthy(self):
        for response in ["{ok:false,json:async()=>({error_code:'unavailable'})}",
                         "{ok:true,json:async()=>({error_code:'bad_schema'})}",
                         "{ok:true,json:async()=>({calls:[{speech:123}]})}"]:
            with self.subTest(response=response):
                result = self.run_js("""
                  active=true;liveOn=true;liveId='current';liveOkAt=0;_clock.now=100;
                  lastFix={mono:100,lat:31,lng:121,spd:5};
                  fetch=async()=>(RESPONSE);
                  await liveBeat();return {healthy:Number.isFinite(liveOkAt),spoken:_spoken};
                """.replace("RESPONSE", response))
                self.assertFalse(result["healthy"])
                self.assertEqual(result["spoken"], [])

    def test_healthy_empty_call_response_is_accepted(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=100;
          lastFix={mono:100,lat:31,lng:121,spd:5};
          fetch=async()=>({ok:true,json:async()=>({calls:[]})});
          await liveBeat();return {health:liveOkAt};
        """)
        self.assertEqual(result["health"], 100)

    def test_stale_heart_rate_is_not_uploaded(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=6000;
          previous={hr:160,mono:0};lastFix={mono:6000,lat:31,lng:121,spd:5};let payload;
          fetch=async(url,init)=>{payload=JSON.parse(init.body);return {ok:true,json:async()=>({calls:[]})};};
          await liveBeat();return payload.point;
        """)
        self.assertNotIn("hr", result)

    def test_fresh_heart_rate_is_uploaded(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=1000;
          previous={hr:160,mono:900};lastFix={mono:1000,lat:31,lng:121,spd:5};let payload;
          fetch=async(url,init)=>{payload=JSON.parse(init.body);return {ok:true,json:async()=>({calls:[]})};};
          await liveBeat();return payload.point;
        """)
        self.assertEqual(result["hr"], 160)

    def test_late_old_session_response_does_not_speak(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='old';_clock.now=100;
          lastFix={mono:100,lat:31,lng:121,spd:5};let resolve;
          fetch=()=>new Promise(r=>{resolve=r;});
          const beat=liveBeat();liveResetSession();liveId='new';
          resolve({ok:true,json:async()=>({calls:[{speech:'旧运动播报',urgent:true}]})});
          await beat;return {healthy:Number.isFinite(liveOkAt),spoken:_spoken};
        """)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["spoken"], [])

    def test_reset_session_clears_old_health(self):
        result = self.run_js("liveOkAt=0;liveResetSession();return {healthy:Number.isFinite(liveOkAt)};")
        self.assertFalse(result["healthy"])

    def test_finished_session_questions_only_carry_record_id(self):
        result = self.run_js("""
          active=false;liveOn=true;liveId='old';lastSession={id:'saved'};$('chat-input').value='我现在心率多少';
          await askMark();return JSON.parse(_calls.filter(x=>x.url==='/api/coach/ride/chat').pop().body);
        """)
        self.assertEqual(result["session_id"], "saved")
        self.assertNotIn("live_id", result)

    def test_active_session_questions_only_carry_live_id(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';lastSession={id:'saved'};$('chat-input').value='我现在心率多少';
          await askMark();return JSON.parse(_calls.filter(x=>x.url==='/api/coach/ride/chat').pop().body);
        """)
        self.assertEqual(result["live_id"], "current")
        self.assertNotIn("session_id", result)
