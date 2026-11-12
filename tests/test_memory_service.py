"""M5 offline acceptance: memory budgets, isolation and builder numeric gates."""
import ast
import copy
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import auth
import cloud_rides
import coach_server as server
import db
import dialog_store
import memory_service as memory
import ride_brain
import ride_chat
import ride_live
from action_cards import ActionError
from tests.test_cloud_rides import FakeConnection
from tests.test_profile_service import NOW, record
from tests.test_ride_chat import memory as local_session, transport, facts_of
from tests.test_ride_live import milestone_event, live_facts, transport as live_transport


class MemoryTests(unittest.TestCase):
    def setUp(self):
        dialog = patch.object(dialog_store, "get_recent", return_value=None)
        dialog.start(); self.addCleanup(dialog.stop)

    def context(self, records, question="我累计骑了多少？", **kwargs):
        with patch.object(memory, "_records", return_value=records):
            return memory.get_context_for_chat("user-a", question, now=NOW, **kwargs)

    def test_all_history_totals_not_latest_thirty_or_hundred(self):
        value = self.context([record(i, start=NOW - timedelta(days=i)) for i in range(1, 121)])
        self.assertEqual(value["totals"]["sessions_count"], 120)
        self.assertEqual(value["totals"]["duration_min"], 3600)
        self.assertEqual(value["totals"]["distance_km"], 720)
        self.assertEqual(value["totals"]["month_sessions"], 11)
        self.assertEqual(value["profile"]["sessions"], 120)
        self.assertLessEqual(memory.memory_upper_bound(value, "chat"), 800)

    def test_three_recent_records_are_preserved_in_small_projection(self):
        value = self.context([record(i) for i in range(1, 6)])
        self.assertEqual(len(value["recent_summaries"]), 3)
        self.assertEqual(set(value["recent_summaries"][0]), {"duration_s", "avg_hr"})

    def test_missing_distance_not_fabricated_as_zero_or_partial_total(self):
        value = self.context([record(1), record(2, distance=None)])
        self.assertIsNone(value["totals"]["distance_km"])
        self.assertIsNone(value["totals"]["month_distance_km"])
        self.assertIsNone(value["profile"])

    def test_no_history_has_no_fake_profile_dialog_notes_or_totals(self):
        value = self.context([])
        for key in ("totals", "week", "profile", "recent_dialog"):
            self.assertIsNone(value[key])
        self.assertEqual(value["user_notes"], [])
        self.assertEqual(value["recent_summaries"], [])

    def test_simulated_other_activity_zero_and_future_excluded(self):
        values = [record(1), record(2, source="simulated"), record(3, activity="run"),
                  record(4, activity="hike"), record(5, duration=0, distance=0),
                  record(6, start=NOW)]
        self.assertEqual(self.context(values)["totals"]["sessions_count"], 1)

    def test_run_and_hike_queries_do_not_get_ride_habits(self):
        for activity, question in (("run", "我跑步累计多久"), ("hike", "我徒步累计多久")):
            value = self.context([record(1), record(2, activity=activity)], question)
            self.assertEqual(value["totals"]["sessions_count"], 1)
            self.assertIsNone(value["profile"])

    def test_all_cloud_data_validated_including_excluded_rows(self):
        for mutate in (lambda r: r.update(series=[]),
                       lambda r: r["summary"].update(avg_hr=float("nan")),
                       lambda r: r.update(user_id="other")):
            bad = record(1); mutate(bad)
            with self.assertRaises(cloud_rides.InvalidCloudRide):
                self.context([bad])

    def test_sql_user_scope_all_rows_and_resources_released(self):
        conn = FakeConnection()
        conn.store = {"a:x": {"user": "a", "created": "1", "dataset": record(1)},
                      "b:x": {"user": "b", "created": "2", "dataset": record(2)}}
        with patch.object(db, "get_conn", return_value=conn), patch.object(db, "release_conn") as release:
            values = memory._records("a")
        self.assertEqual(values, [record(1)])
        sql, args = conn.queries[0]
        self.assertEqual(args, ("a",))
        self.assertIn("WHERE user_id = %s", sql)
        self.assertNotIn("LIMIT", sql)
        release.assert_called_once_with(conn)

    def test_database_failure_is_generic_no_local_fallback(self):
        conn = FakeConnection(); conn.fail_execute = True
        with patch.object(db, "get_conn", return_value=conn), patch.object(db, "release_conn"):
            with self.assertRaises(db.DatabaseOperationError) as caught:
                memory._records("a")
        self.assertNotIn("password", str(caught.exception))

    def test_latest_by_ride_time_not_upload_and_no_overlap_or_self(self):
        current = memory.SummarySession(record(1))
        previous = record(2)
        overlap = record(3, start=cloud_rides._instant(current.record["started_at"]) - timedelta(minutes=10))
        with patch.object(memory, "_records", return_value=[previous, overlap, current.record]):
            value = memory.get_context_for_review("a", current, now=NOW)
        self.assertEqual(value["previous_summary"], previous["summary"])
        self.assertEqual(len(value["recent_summaries"]), 1)
        self.assertLessEqual(memory.memory_upper_bound(value, "review", current), 800)

    def test_live_context_uses_same_activity_completed_before_live_start(self):
        descriptor = {"started_at": NOW.isoformat(), "activity": "run"}
        with patch.object(memory, "_records", return_value=[record(1), record(2, activity="run")]):
            value = memory.get_context_for_live("a", descriptor, now=NOW)
        self.assertEqual(value["previous_summary"], record(2, activity="run")["summary"])
        self.assertIsNone(value["profile_brief"])
        self.assertLessEqual(memory.memory_upper_bound(value, "live"), 800)

    def test_raw_notes_and_ids_never_in_memory_or_projected_facts(self):
        values = [record(i) for i in range(1, 4)]
        for item in values:
            item["coach_notes"] = ["private-do-not-send"]
        text = json.dumps(self.context(values))
        for forbidden in ("private-do-not-send", "profile-1", "series", "coach_notes"):
            self.assertNotIn(forbidden, text)

    def test_longest_is_all_history_not_recent_window(self):
        old = record(100, duration=7200)
        value = self.context([old, record(1)], "我最长一次骑多久")
        self.assertEqual(value["matched_summaries"][0]["summary"]["duration_s"], 7200)
        self.assertLessEqual(memory.memory_upper_bound(value, "chat"), 800)

    def test_date_search_does_not_invent_match_when_none(self):
        value = self.context([record(1)], "我2026-01-01骑了多少")
        self.assertEqual(value["matched_summaries"], [])

    def test_timezone_is_china_even_when_host_utc(self):
        # 16:30 UTC is next calendar day in China, independent of host TZ.
        item = record(1, start=datetime(2026, 11, 8, 16, 30, tzinfo=timezone.utc))
        value = self.context([item], "我2026-11-09骑了多久")
        projected, _ = ride_chat._matched_facts(value["matched_summaries"])
        self.assertEqual(projected[0]["date"], "11月9日")

    def test_range_never_truncated_to_fake_full_count(self):
        values = [record(i, start=NOW-timedelta(days=1, hours=i)) for i in range(8)]
        with self.assertRaisesRegex(ValueError, "budget"):
            self.context(values, "昨天我骑了几次")

    def test_week_and_month_cutoff_in_china(self):
        now = datetime(2026, 11, 2, 0, 30, tzinfo=memory.LOCAL_TIMEZONE)
        values = [record(1, start=now-timedelta(hours=1), duration=60, distance=1000),
                  record(2, start=now-timedelta(minutes=10), duration=60, distance=1000)]
        with patch.object(memory, "_records", return_value=values):
            value = memory.get_context_for_chat("a", "我骑了几次", now=now)
        self.assertEqual(value["week"]["sessions"], 1)
        self.assertEqual(value["totals"]["month_sessions"], 2)

    def test_missing_selected_id_cannot_fall_back_to_another_user_or_local(self):
        with self.assertRaises(cloud_rides.CloudRideNotFound):
            self.context([record(1)], session_id="other-user-session")

    def test_selected_summary_budget_and_no_sample_count(self):
        item = record(1)
        value = self.context([item, record(2), record(3)], session_id=item["id"])
        current = memory.SummarySession(item)
        self.assertLessEqual(memory.memory_upper_bound(value, "chat", current), 800)
        facts, _ = ride_brain._facts(current, None)
        self.assertNotIn("sample_count", facts)
        self.assertNotIn("pw_hr_decoupling_pct", facts)
        self.assertNotIn("series", facts)

    def test_summary_wrapper_revalidates_after_mutation(self):
        current = memory.SummarySession(record(1))
        current.record["summary"]["avg_hr"] = -1
        with self.assertRaises(ValueError):
            ride_brain._facts(current, None)

    def test_budget_uses_bytes_complete_groups_and_fails_closed(self):
        value = {"previous_summary": record(1)["summary"], "profile_brief": None}
        with patch.object(memory, "MAX_MEMORY_TOKENS", 100):
            fitted = memory._budget(value, "live")
        self.assertIsNone(fitted["previous_summary"])
        with patch.object(memory, "MAX_MEMORY_TOKENS", 10):
            with self.assertRaises(ValueError):
                memory._budget({"previous_summary": None, "profile_brief": None}, "live")

    def test_bad_clock_and_question_rejected_without_database(self):
        with patch.object(memory, "_records") as read:
            for question in (None, "", "a" * 201):
                with self.assertRaises(ValueError):
                    memory.get_context_for_chat("a", question, now=NOW)
            with self.assertRaises(ValueError):
                memory.get_context_for_chat("a", "hello", now=datetime(2026, 11, 1))
        read.assert_not_called()


    def test_selected_session_changed_during_retrieval_is_rejected(self):
        current = memory.SummarySession(record(1))
        changed = record(1, hr=160)
        with patch.object(memory, "_records", return_value=[changed]):
            with self.assertRaisesRegex(ValueError, "changed"):
                memory.get_context_for_chat("a", "这次心率", now=NOW,
                    session_id=current.record["id"], selected_session=current)

    def test_extreme_numeric_summary_still_obeys_budget_or_fails_closed(self):
        item = record(1, duration=1800.123456, distance=12345.987654, hr=123.123456789)
        item["summary"]["gaps"] = 10**120
        with patch.object(memory, "_records", return_value=[item, record(2), record(3)]):
            for lane in ("chat", "live", "review"):
                current = memory.SummarySession(record(0))
                if lane == "chat":
                    value = memory.get_context_for_chat("a", "我累计骑多久", now=NOW)
                elif lane == "live":
                    value = memory.get_context_for_live("a", {"started_at":NOW.isoformat()}, now=NOW)
                else:
                    value = memory.get_context_for_review("a", current, now=NOW)
                self.assertLessEqual(memory.memory_upper_bound(value, lane,
                    current if lane == "review" else None), 800)


class BuilderMemoryTests(unittest.TestCase):
    def setUp(self):
        dialog = patch.object(dialog_store, "get_recent", return_value=None)
        dialog.start(); self.addCleanup(dialog.stop)

    def test_cloud_chat_numeric_verification_uses_totals_and_rejects_fabrication(self):
        with patch.object(memory, "_records", return_value=[record(1), record(2)]):
            context = memory.get_context_for_chat("a", "我累计骑多久", now=NOW)
        for speech, status in (("累计记录60分钟。", "ok"),
                               ("累计记录999分钟。", "rejected_unverifiable_number")):
            result = ride_chat.build_ride_chat("我累计骑多久", totals=context["totals"],
                recent_summaries=context["recent_summaries"], api_key="synthetic", http_post=transport(speech))
            self.assertEqual(result["llm_status"], status)

    def test_cloud_review_validates_aggregate_not_fake_session_and_verifies_memory(self):
        current = memory.SummarySession(record(1, hr=150))
        post = transport("上次平均心率130 bpm。")
        result = ride_brain.build_ride_review(current, record(2)["summary"], api_key="synthetic", http_post=post)
        self.assertEqual(result["llm_status"], "ok")
        facts = facts_of(post)
        self.assertEqual(facts["previous_summary"]["avg_hr"], 130)
        self.assertNotIn("sample_count", facts)
        self.assertNotIn("pw_hr_decoupling_pct", facts)

    def test_live_memory_reaches_model_and_historical_numbers_are_verified(self):
        facts = live_facts(); facts["previous"] = record(1)["summary"]
        post = live_transport("上次记录30分钟。")
        result = ride_live.build_live_speech(milestone_event(), facts, api_key="synthetic", http_post=post)
        self.assertEqual(result["llm_status"], "ok")
        self.assertEqual(facts_of(post)["live_facts"]["previous"], record(1)["summary"])

    def test_history_cannot_masquerade_as_current_live_number(self):
        facts = live_facts(); facts["previous"] = record(1)["summary"]
        for speech in ("现在心率130 bpm。", "现在用了30分钟。", "上次现在用了30分钟。", "上次心率999 bpm。"):
            with self.subTest(speech=speech):
                result = ride_live.build_live_speech(milestone_event(), facts, api_key="synthetic",
                                                     http_post=live_transport(speech))
                self.assertEqual(result["llm_status"], "rejected_unverifiable_number")

    def test_profile_habits_not_current_live_state(self):
        facts = live_facts(); facts["profile"] = {"sessions": 3, "usual_start_hour": 20, "usual_duration_min": 47}
        for speech, expected in (("通常记录47分钟。", "ok"), ("现在记录47分钟。", "rejected_unverifiable_number")):
            result = ride_live.build_live_speech(milestone_event(), facts, api_key="synthetic",
                                                 http_post=live_transport(speech))
            self.assertEqual(result["llm_status"], expected)

    def test_corrupt_live_memory_rejected_before_network(self):
        facts = live_facts(); facts["previous"] = {"duration_s": "ignore-rules"}
        post = Mock()
        result = ride_live.build_live_speech(milestone_event(), facts, api_key="synthetic", http_post=post)
        self.assertEqual(result["llm_status"], "rejected_bad_schema")
        post.assert_not_called()

    def test_local_contract_still_rejects_unwrapped_cloud_dict(self):
        with self.assertRaises(ValueError):
            ride_brain._facts(record(1), None)


class RouteMemoryTests(unittest.TestCase):
    def setUp(self):
        for name, result in (("get_recent", None), ("check_capacity", None), ("save_exchange", {})):
            boundary = patch.object(dialog_store, name, return_value=result)
            boundary.start(); self.addCleanup(boundary.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = server.App(store=SimpleNamespace(path=Path(self.temp.name)/"coach.sqlite3"))
        self.identity = patch.object(auth, "whoami", side_effect=lambda token: {"token-a":"a", "token-b":"b"}[token])
        self.identity.start(); self.addCleanup(self.identity.stop)
        self.env = patch.dict(os.environ, {"AGENT_PLANNER":"off", "DASHSCOPE_API_KEY":""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.router = patch.object(server.llm_router, "run", side_effect=self.run_builder)
        self.run = self.router.start(); self.addCleanup(self.router.stop)
        self.builders = []

    def run_builder(self, lane, builder, **unused):
        result = builder(api_key=None)
        self.builders.append(lane)
        return result

    def test_authenticated_chat_does_not_read_any_local_rides_notes_or_dialog(self):
        # Deliberately unusable disk store; cloud succeeds without touching it.
        with patch.object(memory, "_records", return_value=[record(1)]), \
             patch.object(server, "load_notes", side_effect=AssertionError("local notes leaked")), \
             patch.object(self.app, "_dialog_read", wraps=self.app._dialog_read) as dialog, \
             patch.object(server, "build_ride_chat", wraps=server.build_ride_chat) as build:
            self.app._chat_ride({"token":"token-a", "question":"我累计骑了几次", "conv_id":"shared"})
        self.assertIsNone(dialog.call_args.args[0])
        self.assertIsNone(build.call_args.kwargs["session"])
        self.assertEqual(build.call_args.kwargs["notes"], [])
        self.assertEqual(build.call_args.kwargs["totals"]["sessions_count"], 1)
        self.assertEqual(self.app._convs, {})

    def test_two_identities_receive_only_own_cloud_memory(self):
        def rows(user_id, database_url=None):
            return [record(1)] if user_id == "a" else [record(2), record(3)]
        with patch.object(memory, "_records", side_effect=rows), \
             patch.object(server, "build_ride_chat", wraps=server.build_ride_chat) as build:
            for token, count in (("token-a", 1), ("token-b", 2)):
                self.app._chat_ride({"token":token, "question":"我累计骑了几次"})
                self.assertEqual(build.call_args.kwargs["totals"]["sessions_count"], count)

    def test_invalid_token_and_storage_outage_never_fall_back_to_local(self):
        for failure, status in ((auth.InvalidToken("bad"), 401), (db.DatabaseOperationError("no"), 503)):
            with patch.object(auth, "whoami", side_effect=failure):
                with self.assertRaises(ActionError) as caught:
                    self.app._chat_ride({"token":"bad", "question":"我骑多久"})
            self.assertEqual(caught.exception.status, status)
        self.run.assert_not_called()

    def test_cloud_read_failure_generic_503_before_model(self):
        with patch.object(memory, "_records", side_effect=db.DatabaseOperationError("password=secret")):
            with self.assertRaises(ActionError) as caught:
                self.app._chat_ride({"token":"token-a", "question":"我骑多久"})
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("secret", str(caught.exception))
        self.run.assert_not_called()

    def test_injected_user_id_rejected(self):
        with self.assertRaises(ActionError):
            self.app._chat_ride({"token":"token-a", "user_id":"b", "question":"我骑多久"})
        self.run.assert_not_called()

    def test_anonymous_chat_never_calls_cloud_or_auth(self):
        self.app.ride_dir.mkdir(parents=True)
        with patch.object(memory, "_records", side_effect=AssertionError("cloud")):
            self.app._chat_ride({"question":"我骑了几次"})
        auth.whoami.assert_not_called()

    def test_cloud_review_does_not_use_local_same_id_or_change_builder_signature(self):
        item = record(1)
        with patch.object(cloud_rides, "get_ride", return_value={"ride":item}) as get, \
             patch.object(memory, "_records", return_value=[item, record(2)]), \
             patch.object(server, "build_ride_review", wraps=server.build_ride_review) as build:
            self.app._review_ride({"token":"token-b", "session_id":item["id"]})
        get.assert_called_once_with("b", item["id"])
        self.assertIsInstance(build.call_args.args[0], memory.SummarySession)
        self.assertEqual(build.call_args.args[1], record(2)["summary"])

    def test_cloud_live_ids_are_namespaced_not_shared_with_anonymous(self):
        point = {"t":1700000000.0, "lat":31, "lng":121, "speed_mps":1}
        for token in ("token-a", "token-b", None):
            payload = {"live_id":"same", "point":point}
            if token is not None:
                payload["token"] = token
            self.app._live_ride(payload)
        self.assertEqual(set(self.app._live), {("a","same"), ("b","same"), "same"})

    def test_live_memory_loaded_only_when_event_survives_silence_gate(self):
        point = {"t":1700000000.0, "lat":31, "lng":121, "speed_mps":1}
        with patch.object(memory, "get_context_for_live") as read:
            self.app._live_ride({"token":"token-a", "live_id":"test", "point":point})
        read.assert_not_called()

    def test_cloud_live_event_injects_memory_for_same_user(self):
        session = Mock(); session.activity = "ride"; session._start_t = 1700000000.0
        session.ingest.return_value = milestone_event(); session.facts.return_value = live_facts()
        ctx = {"previous_summary": record(1)["summary"], "profile_brief":None}
        with patch.object(server, "LiveSession", return_value=session), \
             patch.object(memory, "get_context_for_live", return_value=ctx) as read, \
             patch.object(server, "build_live_speech", wraps=server.build_live_speech) as build:
            self.app._live_ride({"token":"token-a", "live_id":"test", "point":{"t":1700000000.0}})
        self.assertEqual(read.call_args.args[0], "a")
        self.assertEqual(build.call_args.args[1]["previous"], ctx["previous_summary"])


    def test_cloud_personal_failure_does_not_fall_through_to_unrelated_answers(self):
        with patch.object(memory, "_records", return_value=[record(1)]), \
             patch.object(server.llm_router, "run", return_value={"llm_status":"upstream_error",
                 "draft":None, "fallback":"template", "usage":None}) as router:
            result = self.app._chat_ride({"token":"token-a", "question":"我骑了多久"})
        self.assertEqual(result["lane"], "personal")
        self.assertEqual(router.call_count, 1)

    def test_memory_routes_over_real_loopback_http_keep_all_private_gates(self):
        import base64
        import http.client
        from contextlib import closing
        from threading import Thread
        password = "synthetic-M5-private-password-1234"
        basic = "Basic " + base64.b64encode(("boss:"+password).encode()).decode()
        with patch.dict(os.environ, {"LUMA_COACH_TEST_PASSWORD":password}), \
             patch.object(server, "_bind_config", return_value=("127.0.0.1",0)), \
             patch.object(memory, "_records", return_value=[record(1)]):
            app = server.App(server.CoachStore(Path(self.temp.name)/"http.sqlite3"))
            httpd = server.create_server(app=app)
            worker = Thread(target=httpd.serve_forever, kwargs={"poll_interval":0.02}, daemon=True)
            worker.start()
            try:
                def request(body, *, private=True, browser=True):
                    headers = {"Origin":"http://127.0.0.1:"+str(httpd.server_port), "Content-Type":"application/json"}
                    if private:
                        headers["Authorization"] = basic
                    if browser:
                        headers["X-Coach-Token"] = app.token
                    with closing(http.client.HTTPConnection("127.0.0.1",httpd.server_port,timeout=5)) as client:
                        client.request("POST",server.RIDE_CHAT,json.dumps(body).encode(),headers)
                        response = client.getresponse()
                        return response.status, dict(response.getheaders()), json.loads(response.read())
                body = {"token":"token-a", "question":"我骑了几次"}
                self.assertEqual(request(body,private=False)[0],401)
                self.assertEqual(request(body,browser=False)[0],403)
                status, headers, result = request(body)
                self.assertEqual(status,200)
                self.assertEqual(headers["Cache-Control"],"no-store")
                self.assertEqual(result["lane"],"personal")
                self.assertNotIn("token-a",json.dumps(result))
            finally:
                httpd.shutdown(); httpd.server_close(); worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
