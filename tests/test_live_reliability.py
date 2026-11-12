"""Regression checks for current telemetry, freshness and client failover. Offline only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
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

    def test_raw_location_rejected_without_allocating_session(self):
        for raw in ({'lat': 31, 'lng': 121}, {'distance_m': 0, 'alt': 100}):
            with self.subTest(raw=raw):
                with self.assertRaises(coach_server.ActionError):
                    self.request('/api/coach/ride/live', {'live_id': 'private', 'point': {'t': 1700000000, **raw}})
                self.assertEqual(self.app._live, {})

    def feed(self, i=0, **metrics):
        point = {"t": 1700000000 + i * 10, "distance_m": i * 1.0, "speed_mps": 5.0}
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

    def test_failed_or_empty_live_response_releases_reservation(self):
        from ride_live import LiveSession
        for failure in (RuntimeError('offline'), {'speech': '  '}):
            with self.subTest(failure=failure):
                session = LiveSession()
                session.ingest = Mock(return_value=[{'type': 'speed_drop'}])
                outcome = {'speech': '有效提醒', 'llm_status': 'ok', 'fallback': None, 'usage': None}
                with patch.object(coach_server, 'LiveSession', return_value=session), \
                     patch.object(coach_server.time, 'monotonic', return_value=1000), \
                     patch.object(coach_server.llm_router, 'run', side_effect=[failure, outcome]):
                    self.app._live.clear()
                    payload = {'live_id': 'failed', 'point': {'distance_m': 0}}
                    if isinstance(failure, Exception):
                        with self.assertRaises(RuntimeError): self.app._live_ride(payload)
                    else:
                        self.assertEqual(self.app._live_ride(payload), {'calls': []})
                    self.assertIsNone(self.app._live['failed'][1])
                    self.assertEqual(session._l4_counts['speed_drop'], 0)
                    self.assertEqual(self.app._live_ride(payload)['calls'][0]['speech'], '有效提醒')
                    self.assertEqual(session._l4_counts['speed_drop'], 1)
                    self.assertEqual(self.app._live['failed'][1], 1000)

    def test_live_rejects_unknown_fields_before_allocating_session(self):
        for extra in ({'latitude': 31}, {'location': {'lat': 31, 'lng': 121}}, {'alt': 10}):
            with self.subTest(extra=extra):
                point = {'t': 1000, 'distance_m': 0, 'speed_mps': 5, **extra}
                with self.assertRaises(coach_server.ActionError):
                    self.app._live_ride({'live_id': 'private', 'point': point})
                self.assertNotIn('private', self.app._live)

    def test_cloud_memory_failure_restores_one_shot_for_retry(self):
        from ride_live import LiveSession
        session = LiveSession()
        event = {'type': 'fuel'}
        session.ingest = Mock(side_effect=[[event], []])
        session._start_t = 1700000000
        outcome = {'speech': '记得补给', 'llm_status': 'ok', 'fallback': None, 'usage': None}
        with patch.object(self.app, '_memory_identity', side_effect=lambda p: ('user', p)), \
             patch.object(self.app, '_cloud_memory', side_effect=[RuntimeError('offline'), {'previous_summary': None, 'profile_brief': None}]), \
             patch.object(coach_server, 'LiveSession', return_value=session), \
             patch.object(coach_server.time, 'monotonic', return_value=1000), \
             patch.object(coach_server.llm_router, 'run', return_value=outcome):
            with self.assertRaises(RuntimeError):
                self.app._live_ride({'live_id': 'test', 'point': {'distance_m': 0}})
            self.assertEqual(self.app._live_ride({'live_id': 'test', 'point': {'distance_m': 0}})['events'], ['fuel'])
            self.assertFalse(session._reservations)

    def test_old_completion_cannot_modify_recreated_session(self):
        from ride_live import LiveSession
        first, second = LiveSession(), LiveSession()
        first.ingest = Mock(return_value=[{'type': 'fuel'}])
        second.ingest = Mock(return_value=[])
        started, release = threading.Event(), threading.Event()
        errors, responses = [], []
        def delayed(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            return {'speech': '记得补给', 'llm_status': 'ok', 'fallback': None, 'usage': None}
        def request():
            try:
                responses.append(self.app._live_ride({'live_id': 'test', 'point': {'distance_m': 0}}))
            except Exception as exc:
                errors.append(exc)
        with patch.object(coach_server, 'LiveSession', side_effect=[first, second]), \
             patch.object(coach_server.llm_router, 'run', side_effect=delayed):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                with self.app._live_lock:
                    self.app._live.pop('test')
                self.assertEqual(self.app._live_ride({'live_id': 'test', 'point': {'distance_m': 0}}), {'calls': []})
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
                self.assertEqual(responses, [{"calls": []}])
                self.assertIs(self.app._live['test'][0], second)
                self.assertIsNone(self.app._live['test'][1])
            finally:
                release.set()
                thread.join(5)

    def test_cancelled_heart_rate_warning_is_not_returned_after_model_delay(self):
        started, release = threading.Event(), threading.Event()
        result, errors = [], []
        def delayed(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test timed out")
            return {"speech": "心率过高", "llm_status": "ok", "fallback": None, "usage": None}
        def request():
            try:
                result.append(self.feed(1, hr=170, hr_upper=140))
            except Exception as exc:
                errors.append(exc)
        self.feed(0, hr=170, hr_upper=140)
        with patch.object(coach_server, "build_live_speech", side_effect=delayed), \
             patch.object(coach_server.llm_router, "run", side_effect=lambda lane, build: build()):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                self.feed(2, hr=120)  # threshold removed while the model is running
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["calls"], [])
            finally:
                release.set()
                thread.join(5)

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

    def test_local_distance_is_sent_without_coordinates_or_heart_rate(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=1000;
          onTrackFix({coords:{latitude:31,longitude:121,accuracy:5,speed:5}});
          _clock.now=11000;
          onTrackFix({coords:{latitude:31.0001,longitude:121,accuracy:5,speed:5}});
          let payload;fetch=async(url,init)=>{payload=JSON.parse(init.body);return {ok:true,json:async()=>({calls:[]})};};
          await liveBeat();return payload.point;
        """)
        self.assertGreater(result['distance_m'], 10)
        for field in ('lat', 'lng', 'alt', 'hr'):
            self.assertNotIn(field, result)

    def test_missing_or_invalid_speed_does_not_send_or_report_healthy(self):
        for speed in ('null', 'undefined', 'NaN', 'Infinity', '-1', '31'):
            with self.subTest(speed=speed):
                result = self.run_js("""
                  active=true;liveOn=true;liveId='current';_clock.now=100;
                  onTrackFix({coords:{latitude:31,longitude:121,accuracy:5,speed:SPEED}});
                  let sent=0;fetch=async()=>{sent++;return {ok:true,json:async()=>({calls:[]})};};
                  await liveBeat();return {speed:lastFix.spd,sent,healthy:Number.isFinite(liveOkAt)};
                """.replace('SPEED', speed))
                self.assertIsNone(result['speed'])
                self.assertEqual(result['sent'], 0)
                self.assertFalse(result['healthy'])

    def test_bad_location_accuracy_never_sends_live_data(self):
        for accuracy in ('null', 'undefined', 'NaN', 'Infinity', '-1', '31'):
            with self.subTest(accuracy=accuracy):
                result = self.run_js("""
                  active=true;liveOn=true;liveId='current';_clock.now=100;
                  onTrackFix({coords:{latitude:31,longitude:121,accuracy:ACCURACY,speed:5}});
                  let sent=0;fetch=async()=>{sent++;return {ok:true,json:async()=>({calls:[]})};};
                  await liveBeat();return sent;
                """.replace('ACCURACY', accuracy))
                self.assertEqual(result, 0)

    def test_identity_wait_cannot_send_old_fix_when_new_fix_arrives(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=100;
          lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};let release;
          MarkCloud.withIdentity=body=>new Promise(r=>{release=()=>r(body);});
          const before=_calls.length;const beat=liveBeat();
          _clock.now=10200;lastFix={mono:10200,lat:31,lng:121,spd:6,acc:5};
          release();await beat;return {uploads:_calls.length-before,busy:liveBusy};
        """)
        self.assertEqual(result, {'uploads': 0, 'busy': False})

    def test_identity_wait_cannot_send_after_location_switch_off(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=100;
          lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};let release;
          MarkCloud.withIdentity=body=>new Promise(r=>{release=()=>r(body);});
          const before=_calls.length;const beat=liveBeat();
          $('track-enabled').checked=false;release();await beat;
          return {uploads:_calls.length-before,busy:liveBusy};
        """)
        self.assertEqual(result, {'uploads': 0, 'busy': False})

    def test_actual_zero_speed_is_sent(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=100;
          onTrackFix({coords:{latitude:31,longitude:121,accuracy:5,speed:0}});
          let payload;fetch=async(url,init)=>{payload=JSON.parse(init.body);return {ok:true,json:async()=>({calls:[]})};};
          await liveBeat();return payload.point;
        """)
        self.assertEqual(result['speed_mps'], 0)

    def test_distance_ignores_noise_jumps_and_gaps(self):
        result = self.run_js("""
          active=true;
          const f=(lat,mono,acc=5)=>accumulateLiveDistance({lat,lng:121,mono,acc});
          f(31,0);f(31.00001,1000);f(31.0001,10000);
          const moved=liveTrackDistance;
          f(32,11000);f(32.0001,50000);
          return {moved,after:liveTrackDistance};
        """)
        self.assertGreater(result['moved'], 10)
        self.assertEqual(result['after'], result['moved'])

    def test_live_errors_and_malformed_calls_do_not_claim_healthy(self):
        for response in ["{ok:false,json:async()=>({error_code:'unavailable'})}",
                         "{ok:true,json:async()=>({error_code:'bad_schema'})}",
                         "{ok:true,json:async()=>({calls:[{speech:123}]})}"]:
            with self.subTest(response=response):
                result = self.run_js("""
                  active=true;liveOn=true;liveId='current';liveOkAt=0;_clock.now=100;
                  lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};
                  fetch=async()=>(RESPONSE);
                  await liveBeat();return {healthy:Number.isFinite(liveOkAt),spoken:_spoken};
                """.replace("RESPONSE", response))
                self.assertFalse(result["healthy"])
                self.assertEqual(result["spoken"], [])

    def test_healthy_empty_call_response_is_accepted(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=100;
          lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};
          fetch=async()=>({ok:true,json:async()=>({calls:[]})});
          await liveBeat();return {health:liveOkAt};
        """)
        self.assertEqual(result["health"], 100)

    def test_stale_heart_rate_is_not_uploaded(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=6000;
          previous={hr:160,mono:0};lastFix={mono:6000,lat:31,lng:121,spd:5,acc:5};let payload;
          fetch=async(url,init)=>{payload=JSON.parse(init.body);return {ok:true,json:async()=>({calls:[]})};};
          await liveBeat();return payload.point;
        """)
        self.assertNotIn("hr", result)

    def test_identity_wait_drops_expired_heart_rate_even_with_new_sample(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=100;
          previous={hr:160,mono:100};lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};
          let release,payload;MarkCloud.withIdentity=body=>new Promise(r=>{release=()=>r(body);});
          fetch=async(url,init)=>{payload=JSON.parse(init.body);return {ok:true,json:async()=>({calls:[]})};};
          const beat=liveBeat();_clock.now=5200;previous={hr:165,mono:5200};release();await beat;
          return payload.point;
        """)
        self.assertNotIn("hr", result)
        self.assertEqual(result["speed_mps"], 5)

    def test_identity_wait_drops_heart_rate_if_signal_gap_opens(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=100;
          previous={hr:160,mono:100};lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};
          let release,payload;MarkCloud.withIdentity=body=>new Promise(r=>{release=()=>r(body);});
          fetch=async(url,init)=>{payload=JSON.parse(init.body);return {ok:true,json:async()=>({calls:[]})};};
          const beat=liveBeat();gapOpen=true;release();await beat;return payload.point;
        """)
        self.assertNotIn("hr", result)

    def test_fresh_heart_rate_is_uploaded(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=1000;
          previous={hr:160,mono:900};lastFix={mono:1000,lat:31,lng:121,spd:5,acc:5};let payload;
          fetch=async(url,init)=>{payload=JSON.parse(init.body);return {ok:true,json:async()=>({calls:[]})};};
          await liveBeat();return payload.point;
        """)
        self.assertEqual(result["hr"], 160)

    def test_late_old_session_response_does_not_speak(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='old';_clock.now=100;
          lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};let resolve;
          fetch=()=>new Promise(r=>{resolve=r;});
          const beat=liveBeat();await _flush();liveResetSession();liveId='new';
          resolve({ok:true,json:async()=>({calls:[{speech:'旧运动播报',urgent:true}]})});
          await beat;return {healthy:Number.isFinite(liveOkAt),spoken:_spoken};
        """)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["spoken"], [])

    def test_identity_wait_cannot_upload_after_session_reset(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='old';_clock.now=100;
          lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};let release;
          MarkCloud.withIdentity=body=>new Promise(r=>{release=()=>r(body);});
          const before=_calls.length;const beat=liveBeat();
          liveResetSession();liveId='new';release();await beat;
          return _calls.length-before;
        """)
        self.assertEqual(result, 0)

    def test_identity_wait_cannot_upload_after_location_disabled(self):
        result = self.run_js("""
          active=true;liveOn=true;liveId='current';_clock.now=100;
          lastFix={mono:100,lat:31,lng:121,spd:5,acc:5};let release;
          MarkCloud.withIdentity=body=>new Promise(r=>{release=()=>r(body);});
          const before=_calls.length;const beat=liveBeat();
          lastFix=null;release();await beat;
          return {uploads:_calls.length-before,busy:liveBusy};
        """)
        self.assertEqual(result, {'uploads': 0, 'busy': False})

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
