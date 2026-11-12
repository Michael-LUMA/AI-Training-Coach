"""M4 numeric aggregation/cache/async tests. Synthetic data, no model or cloud."""
import copy
from datetime import datetime, timedelta, timezone
import json
from threading import Event
import unittest
from unittest.mock import patch

import cloud_rides
import db
import profile_service as profiles
from tests.test_cloud_rides import FakeConnection, FakeCursor, summary

NOW = datetime(2026, 11, 12, 4, tzinfo=timezone.utc)


def record(i=1, *, start=None, duration=1800, distance=6000, hr=130,
           source="ble_gatt_0x180d", activity="ride"):
    value = summary("profile-" + str(i))
    start = NOW - timedelta(days=i) if start is None else start
    value.update(started_at=start.isoformat(), ended_at=(start + timedelta(seconds=duration)).isoformat(),
                 source=source, activity=activity)
    value["summary"].update(duration_s=duration, avg_hr=hr, max_hr=hr,
        avg_cadence=None, avg_power=None, max_power=None, distance_m=distance,
        avg_speed_mps=None if distance is None else distance / duration if duration else 0,
        max_speed_mps=None if distance is None else distance / duration if duration else 0)
    return value


class ProfileCalculationTests(unittest.TestCase):
    def test_less_than_three_real_records_returns_none(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                self.assertIsNone(profiles.build_profile([record(i + 1) for i in range(count)], now=NOW))

    def test_three_records_medians_totals_and_explicit_counts(self):
        records = [record(1, duration=1200, distance=10000, hr=120),
                   record(2, duration=3600, distance=24000, hr=160),
                   record(3, duration=1800, distance=15000, hr=140)]
        value = profiles.build_profile(records, now=NOW)
        self.assertEqual(value["usual_start_hour"], 12)
        self.assertEqual(value["usual_duration_min"], 30)
        self.assertEqual(value["usual_distance_km"], 15)
        self.assertEqual(value["avg_hr_baseline"], 140)
        self.assertEqual(value["total_sessions"], 3)
        self.assertEqual(value["total_distance_km"], 49)
        self.assertEqual(value["sample_counts"],
                         {"baseline": 3, "distance": 3, "hr": 3, "total_distance": 3, "week_distance": 3})
        self.assertEqual(value["this_week"], {"sessions": 3, "distance_km": 49, "duration_min": 110})

    def test_even_sample_medians_are_not_rounded(self):
        records = [record(i, duration=i * 600, distance=i * 1000, hr=100 + i) for i in range(1, 5)]
        value = profiles.build_profile(records, now=NOW)
        self.assertEqual(value["usual_duration_min"], 25)
        self.assertEqual(value["usual_distance_km"], 2.5)
        self.assertEqual(value["avg_hr_baseline"], 102.5)

    def test_simulated_run_hike_zero_duration_and_future_are_excluded(self):
        excluded = [record(4, source="simulated"), record(5, activity="run"),
                    record(6, activity="hike"), record(7, duration=0, distance=0),
                    record(8, start=NOW)]
        self.assertIsNone(profiles.build_profile([record(1), record(2)] + excluded, now=NOW))
        value = profiles.build_profile([record(1), record(2), record(3)] + excluded, now=NOW)
        self.assertEqual(value["total_sessions"], 3)
        self.assertEqual(value["total_distance_km"], 18)

    def test_missing_measurements_remain_none_not_zero(self):
        value = profiles.build_profile([record(i, hr=None, distance=None) for i in range(1, 4)], now=NOW)
        self.assertIsNone(value["usual_distance_km"])
        self.assertIsNone(value["total_distance_km"])
        self.assertIsNone(value["avg_hr_baseline"])
        self.assertIsNone(value["this_week"]["distance_km"])
        self.assertEqual(value["sample_counts"]["hr"], 0)
        self.assertEqual(value["sample_counts"]["distance"], 0)

    def test_partial_measurements_only_use_observed_values(self):
        value = profiles.build_profile([record(1, hr=None, distance=None),
                                        record(2, hr=110, distance=1000),
                                        record(3, hr=150, distance=3000)], now=NOW)
        self.assertIsNone(value["avg_hr_baseline"])
        self.assertIsNone(value["usual_distance_km"])
        self.assertEqual(value["total_distance_km"], 4)
        self.assertEqual(value["sample_counts"]["hr"], 2)

    def test_v1_has_no_invented_distance(self):
        records = [summary("v1-" + str(i)) for i in range(3)]
        value = profiles.build_profile(records, now=NOW)
        self.assertIsNone(value["total_distance_km"])
        self.assertEqual(value["usual_duration_min"], 1)
        self.assertEqual(value["avg_hr_baseline"], 130)

    def test_eligible_imports_and_web_gps_remain_eligible(self):
        records = [record(i + 1, source=source) for i, source in
                   enumerate(("imported_gpx", "imported_tcx", "web_gps"))]
        self.assertEqual(profiles.build_profile(records, now=NOW)["total_sessions"], 3)

    def test_latest_thirty_by_start_time_not_input_or_upload_order(self):
        records = [record(i, start=NOW - timedelta(days=41 - i), duration=i * 60,
                          distance=i * 1000, hr=100 + i) for i in range(1, 41)]
        value = profiles.build_profile(iter(records[::-1]), now=NOW)
        self.assertEqual(value["usual_duration_min"], 25.5)
        self.assertEqual(value["avg_hr_baseline"], 125.5)
        self.assertEqual(value["sample_counts"]["baseline"], 30)
        self.assertEqual(value["total_sessions"], 40)
        self.assertEqual(value["total_distance_km"], 820)
        self.assertEqual(value, profiles.build_profile(records, now=NOW))

    def test_china_monday_week_boundary_uses_actual_timezone_offset(self):
        records = [record(1, start=datetime(2026, 11, 8, 15, 58, tzinfo=timezone.utc), duration=60, distance=1000),
                   record(2, start=datetime(2026, 11, 9, 0, tzinfo=profiles.LOCAL_TIMEZONE)),
                   record(3)]
        value = profiles.build_profile(records, now=NOW)
        self.assertEqual(value["this_week"]["sessions"], 2)
        self.assertEqual(value["this_week"]["distance_km"], 12)
        self.assertEqual(value["this_week"]["duration_min"], 60)

    def test_empty_week_is_zero_not_missing_measurement(self):
        value = profiles.build_profile([record(i, start=NOW - timedelta(days=10 + i), distance=None)
                                        for i in range(3)], now=NOW)
        self.assertEqual(value["this_week"], {"sessions": 0, "distance_km": 0, "duration_min": 0})
        self.assertIsNone(value["total_distance_km"])

    def test_start_hour_is_integer_mode_not_circular_median(self):
        records = [record(i + 1, start=datetime(2026, 11, 10, hour, tzinfo=profiles.LOCAL_TIMEZONE))
                   for i, hour in enumerate((23, 0, 1, 23))]
        value = profiles.build_profile(records, now=NOW)
        self.assertEqual(value["usual_start_hour"], 23)
        self.assertIs(type(value["usual_start_hour"]), int)

    def test_start_hour_mode_ties_choose_smallest_hour_independent_of_order(self):
        records = [record(i + 1, start=datetime(2026, 11, 10, hour, tzinfo=profiles.LOCAL_TIMEZONE))
                   for i, hour in enumerate((23, 0, 1))]
        for ordered in (records, records[::-1]):
            self.assertEqual(profiles.build_profile(ordered, now=NOW)["usual_start_hour"], 0)

    def test_each_baseline_requires_three_observed_measurements(self):
        for count in (0, 1, 2, 3):
            with self.subTest(count=count):
                records = [record(i + 1, hr=120 + i * 10 if i < count else None,
                                  distance=(i + 1) * 1000 if i < count else None)
                           for i in range(4)]
                value = profiles.build_profile(records, now=NOW)
                self.assertEqual(value["sample_counts"]["hr"], count)
                self.assertEqual(value["sample_counts"]["distance"], count)
                if count < 3:
                    self.assertIsNone(value["avg_hr_baseline"])
                    self.assertIsNone(value["usual_distance_km"])
                else:
                    self.assertEqual(value["avg_hr_baseline"], 130)
                    self.assertEqual(value["usual_distance_km"], 2)
                self.assertEqual(value["total_distance_km"],
                                 sum(range(1, count + 1)) if count else None)
                self.assertEqual(profiles._validate_cached(value, 4), value)

    def test_cache_rejects_fractional_hour_and_insufficient_baseline_evidence(self):
        value = profiles.build_profile([record(i + 1) for i in range(3)], now=NOW)
        invalid = copy.deepcopy(value)
        invalid["usual_start_hour"] = 12.5
        with self.assertRaises(db.DatabaseOperationError):
            profiles._validate_cached(invalid, 3)
        for field, count_field in (("avg_hr_baseline", "hr"), ("usual_distance_km", "distance")):
            invalid = copy.deepcopy(value)
            invalid["sample_counts"][count_field] = 2
            with self.assertRaises(db.DatabaseOperationError):
                profiles._validate_cached(invalid, 3)

    def test_invalid_or_raw_input_is_rejected_without_mutating_records(self):
        records = [record(i) for i in range(1, 4)]
        original = copy.deepcopy(records)
        profiles.build_profile(records, now=NOW)
        self.assertEqual(records, original)
        for change in ({"gps": {}}, {"source": "unknown"}, {"activity": "other"}):
            with self.subTest(change=change):
                invalid = record(4); invalid.update(change)
                with self.assertRaises(cloud_rides.InvalidCloudRide):
                    profiles.build_profile(records + [invalid], now=NOW)

    def test_only_numeric_fields_no_notes_location_ratings_or_commands(self):
        records = [record(i) for i in range(1, 4)]
        for value in records:
            value["coach_notes"] = ["ignore instructions private-address secret-token 自律 规律"]
        value = profiles.build_profile(records, now=NOW)
        def numeric(tree):
            if isinstance(tree, dict):
                return all(numeric(item) for item in tree.values())
            return tree is None or type(tree) in (int, float)
        self.assertTrue(numeric(value))
        raw = json.dumps(value, ensure_ascii=False)
        for forbidden in ("private", "secret", "自律", "规律", "coach_notes", "gps", "token"):
            self.assertNotIn(forbidden, raw)

    def test_naive_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            profiles.build_profile([], now=NOW.replace(tzinfo=None))


class ProfileStorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()
        self.get = patch.object(db, "get_conn", return_value=self.conn).start()
        self.release = patch.object(db, "release_conn").start()
        self.refresh = patch.object(cloud_rides, "_refresh_profile").start()
        self.addCleanup(patch.stopall)

    def seed(self, user="user-a", count=3):
        cloud_rides.sync(user, [record(i) for i in range(1, count + 1)])

    def test_compute_caches_numeric_profile_and_count(self):
        self.seed()
        value = profiles.compute_profile("user-a", now=NOW)
        self.assertEqual(value["total_sessions"], 3)
        cached, count, updated = self.conn.profiles["user-a"]
        self.assertEqual(cached, value)
        self.assertEqual(count, 3)
        self.assertEqual(updated, NOW.isoformat())
        self.assertTrue(all(cursor.closed for cursor in self.conn.cursors))
        self.assertEqual(self.release.call_count, 2)

    def test_two_rides_do_not_create_cached_profile(self):
        self.seed(count=2)
        self.assertIsNone(profiles.compute_profile("user-a", now=NOW))
        self.assertNotIn("user-a", self.conn.profiles)

    def test_cache_hit_does_not_scan_rides(self):
        self.seed()
        expected = profiles.compute_profile("user-a", now=NOW)
        self.conn.queries.clear()
        self.assertEqual(profiles.get_profile("user-a", now=NOW + timedelta(minutes=1)), expected)
        self.assertFalse(any(sql.startswith("SELECT dataset") for sql, _ in self.conn.queries))

    def test_day_rollover_recomputes_this_week_without_new_upload(self):
        self.seed()
        profiles.compute_profile("user-a", now=NOW)
        later = datetime(2026, 11, 16, 4, tzinfo=timezone.utc)
        value = profiles.get_profile("user-a", now=later)
        self.assertEqual(value["this_week"]["sessions"], 0)
        self.assertEqual(self.conn.profiles["user-a"][2], later.isoformat())

    def test_missing_cache_is_rebuilt_on_read(self):
        self.seed()
        self.assertNotIn("user-a", self.conn.profiles)
        self.assertEqual(profiles.get_profile("user-a", now=NOW)["total_sessions"], 3)

    def test_user_scoping_and_row_locks_precede_queries(self):
        self.seed()
        self.assertIsNone(profiles.compute_profile("user-b", now=NOW))
        self.assertEqual(profiles.compute_profile("user-a", now=NOW)["total_sessions"], 3)
        for sql, params in self.conn.queries:
            if sql.startswith(("SELECT dataset", "SELECT profile", "DELETE FROM profiles")):
                self.assertIn("user_id = %s", sql)
                self.assertIn(params[0], ("user-a", "user-b"))
        for cursor in self.conn.cursors:
            self.assertTrue(cursor.closed)
        self.assertTrue(all(self.conn.queries[index][0].endswith("FOR UPDATE")
                            for index in (0, 5, 8)))

    def test_sync_invalidates_before_committing_and_then_schedules(self):
        self.seed()
        profiles.compute_profile("user-a", now=NOW)
        self.refresh.reset_mock()
        commits = self.conn.commits
        def after_commit(*_):
            self.assertGreater(self.conn.commits, commits)
            self.assertNotIn("user-a", self.conn.profiles)
        self.refresh.side_effect = after_commit
        cloud_rides.sync("user-a", [record(4)])
        self.refresh.assert_called_once_with("user-a", None)
        self.assertEqual(profiles.get_profile("user-a", now=NOW)["total_sessions"], 4)

    def test_duplicate_sync_keeps_cache_and_does_not_queue_work(self):
        self.seed()
        expected = profiles.compute_profile("user-a", now=NOW)
        self.refresh.reset_mock()
        cloud_rides.sync("user-a", [record(1)])
        self.refresh.assert_not_called()
        self.assertEqual(self.conn.profiles["user-a"][0], expected)

    def test_delete_invalidates_cache_and_returns_to_insufficient_data(self):
        self.seed()
        profiles.compute_profile("user-a", now=NOW)
        self.refresh.reset_mock()
        cloud_rides.delete_ride("user-a", "profile-1")
        self.assertNotIn("user-a", self.conn.profiles)
        self.assertIsNone(profiles.get_profile("user-a", now=NOW))
        self.refresh.assert_called_once_with("user-a", None)
        self.refresh.reset_mock()
        cloud_rides.delete_ride("user-a", "missing")
        self.refresh.assert_not_called()

    def test_conflict_and_commit_failure_preserve_old_cache_and_do_not_queue(self):
        self.seed()
        expected = profiles.compute_profile("user-a", now=NOW)
        self.refresh.reset_mock()
        changed = record(1); changed["coach_notes"] = ["conflict"]
        with self.assertRaises(cloud_rides.CloudRideConflict):
            cloud_rides.sync("user-a", [record(4), changed])
        self.assertEqual(self.conn.profiles["user-a"][0], expected)
        self.conn.fail_commit = True
        with self.assertRaises(db.DatabaseOperationError):
            cloud_rides.delete_ride("user-a", "profile-1")
        self.assertEqual(self.conn.profiles["user-a"][0], expected)
        self.refresh.assert_not_called()

    def test_fetching_more_than_one_batch_preserves_lifetime_totals(self):
        for i in range(120):
            value = record(i, start=NOW - timedelta(days=i + 1))
            self.conn.store["user-a:" + value["id"]] = {"user": "user-a", "dataset": value, "created": ""}
        value = profiles.compute_profile("user-a", now=NOW)
        self.assertEqual(value["total_sessions"], 120)
        self.assertEqual(value["sample_counts"]["baseline"], 30)

    def test_corrupt_stored_summary_or_cache_is_not_used(self):
        self.seed()
        self.conn.store["user-a:profile-1"]["dataset"]["gps"] = {}
        with self.assertRaises(db.DatabaseOperationError):
            profiles.compute_profile("user-a", now=NOW)
        self.assertNotIn("user-a", self.conn.profiles)

    def test_corrupt_cache_numeric_fields_are_rejected(self):
        self.seed()
        value = profiles.compute_profile("user-a", now=NOW)
        mutations = [lambda p: p.update(rating="自律"), lambda p: p.update(avg_hr_baseline=float("nan")),
                     lambda p: p.update(total_sessions=True),
                     lambda p: p["sample_counts"].update(hr=4),
                     lambda p: p["this_week"].update(distance_km="secret"),
                     lambda p: p.update(usual_distance_km=None)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                invalid = copy.deepcopy(value); mutate(invalid)
                self.conn.profiles["user-a"] = (invalid, 3, NOW.isoformat())
                with self.assertRaises(db.DatabaseOperationError) as caught:
                    profiles.get_profile("user-a", now=NOW)
                self.assertNotIn("secret", str(caught.exception))

    def test_invalid_cached_timestamp_is_sanitized_and_future_cache_is_recomputed(self):
        self.seed()
        value = profiles.compute_profile("user-a", now=NOW)
        self.conn.profiles["user-a"] = (value, 3, "private-invalid-timestamp")
        with self.assertRaises(db.DatabaseOperationError) as caught:
            profiles.get_profile("user-a", now=NOW)
        self.assertNotIn("private", str(caught.exception))
        future = NOW + timedelta(hours=1)
        self.conn.profiles["user-a"] = (value, 3, future.isoformat())
        self.assertEqual(profiles.get_profile("user-a", now=NOW), value)
        self.assertEqual(self.conn.profiles["user-a"][2], NOW.isoformat())

    def test_storage_failure_is_explicit_sanitized_and_released(self):
        self.conn.fail_execute = True
        with self.assertRaises(db.DatabaseOperationError) as caught:
            profiles.compute_profile("user-a", now=NOW)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("password", str(caught.exception))
        self.assertEqual(self.conn.rollbacks, 1)
        self.release.assert_called_once_with(self.conn)
        self.assertTrue(self.conn.cursors[0].closed)

    def test_unknown_user_cannot_have_a_profile(self):
        with patch.object(FakeCursor, "fetchone", return_value=None):
            with self.assertRaises(db.DatabaseOperationError):
                profiles.compute_profile("unknown-user", now=NOW)

    def test_invalid_clock_fails_before_database_access(self):
        with self.assertRaises(ValueError):
            profiles.compute_profile("user-a", now=NOW.replace(tzinfo=None))
        self.get.assert_not_called()


class ProfileRefreshTests(unittest.TestCase):
    def test_worker_is_async_coalesces_and_allows_refresh_during_active_work(self):
        started, release = Event(), Event()
        calls = []
        def compute(user, dsn):
            calls.append(user)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(3))
        with patch.object(profiles, "compute_profile", side_effect=compute):
            worker = profiles._RefreshWorker()
            try:
                self.assertTrue(worker.request("user-a", None))
                self.assertTrue(started.wait(3))
                self.assertTrue(worker.request("user-a", None))
                self.assertTrue(worker.request("user-a", None))
                self.assertEqual(calls, ["user-a"])
            finally:
                release.set()
                worker.close()
        self.assertEqual(calls, ["user-a", "user-a"])
        self.assertFalse(worker.thread.is_alive())
        self.assertFalse(worker.request("user-b", None))
        worker.close()

    def test_pending_queue_is_bounded_and_deferred_users_are_not_claimed(self):
        started, release = Event(), Event()
        def compute(*_):
            started.set()
            release.wait(3)
        with patch.object(profiles, "compute_profile", side_effect=compute), \
                patch.object(profiles, "MAX_PENDING_REFRESHES", 2):
            worker = profiles._RefreshWorker()
            try:
                self.assertTrue(worker.request("running", None))
                self.assertTrue(started.wait(3))
                self.assertTrue(worker.request("queued-1", None))
                self.assertTrue(worker.request("queued-2", None))
                self.assertFalse(worker.request("deferred", None))
                self.assertTrue(worker.request("queued-2", None))
            finally:
                release.set()
                worker.close()

    def test_background_failure_is_not_retried_and_logs_no_secrets(self):
        with patch.object(profiles, "compute_profile", side_effect=RuntimeError("password=secret")) as compute, \
                self.assertLogs(profiles._log, level="WARNING") as logs:
            worker = profiles._RefreshWorker()
            worker.request("user-a", "postgresql://private-dsn")
            worker.close()
        self.assertEqual(compute.call_count, 1)
        output = " ".join(logs.output)
        self.assertNotIn("password", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("private-dsn", output)
        self.assertNotIn("user-a", output)

    def test_global_worker_is_lazy_drained_and_reusable_after_shutdown(self):
        profiles.shutdown()
        with patch.object(profiles, "compute_profile") as compute:
            self.assertTrue(profiles.request_refresh("user-a"))
            profiles.shutdown()
            self.assertIsNone(profiles._worker)
            compute.assert_called_once_with("user-a", None)
            self.assertTrue(profiles.request_refresh("user-b"))
            profiles.shutdown()
            self.assertEqual(compute.call_count, 2)
        profiles.shutdown()

    def test_thread_start_failure_defers_without_disclosing_exception(self):
        profiles.shutdown()
        with patch.object(profiles, "_RefreshWorker", side_effect=RuntimeError("secret")), \
                self.assertLogs(profiles._log, level="WARNING") as logs:
            self.assertFalse(profiles.request_refresh("user-a"))
        self.assertIsNone(profiles._worker)
        self.assertNotIn("secret", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
