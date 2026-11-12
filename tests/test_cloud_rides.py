"""Aggregate privacy, transactional sync and user-isolation tests; no network."""
import copy
import json
import unittest
from unittest.mock import patch

import cloud_rides as cloud
import db
from ride_memory import RideMemoryError, to_cloud_summary
from tests import test_ride_memory as fixtures


def summary(ride_id="ride-1"):
    value = to_cloud_summary(fixtures.memory())
    value["id"] = ride_id
    return value


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rows, self.closed = conn, [], False

    def execute(self, sql, params):
        self.conn.queries.append((sql, params))
        if self.conn.fail_execute:
            raise RuntimeError("driver password=secret token=private")
        normalized = " ".join(sql.split())
        store = self.conn.store
        if normalized.startswith("SELECT id FROM users WHERE id"):
            self.rows = [(params[0],)]
        elif normalized.startswith("DELETE FROM profiles"):
            self.conn.profiles.pop(params[0], None)
            self.rows = []
        elif normalized.startswith("SELECT profile, sample_count, updated_at"):
            row = self.conn.profiles.get(params[0])
            self.rows = [copy.deepcopy(row)] if row else []
        elif normalized.startswith("INSERT INTO profiles"):
            user, raw, sample_count, updated = params
            self.conn.profiles[user] = (json.loads(raw), sample_count, updated)
            self.rows = []
        elif normalized.startswith("INSERT INTO rides"):
            key, user, version, created, updated, raw = params
            if key in store:
                self.rows = []
            else:
                store[key] = {"user": user, "version": version, "created": created, "dataset": json.loads(raw)}
                self.rows = [(key,)]
        elif normalized.startswith("SELECT dataset FROM rides WHERE id"):
            key, user = params
            row = store.get(key)
            self.rows = [(copy.deepcopy(row["dataset"]),)] if row and row["user"] == user else []
        elif normalized.startswith("SELECT dataset FROM rides WHERE user_id"):
            user = params[0]
            limit = params[1] if len(params) == 2 else len(store)
            rows = sorted(((key, row) for key, row in store.items() if row["user"] == user),
                          key=lambda item: (item[1]["created"], item[0]), reverse=True)[:limit]
            self.rows = [(copy.deepcopy(row["dataset"]),) for _, row in rows]
        elif normalized.startswith("DELETE FROM rides"):
            key, user = params
            row = store.get(key)
            self.rows = [(key,)] if row and row["user"] == user else []
            if self.rows:
                del store[key]
        else:
            raise AssertionError("Unexpected SQL")

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows[:]

    def fetchmany(self, size):
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, store=None):
        self.store = store if store is not None else {}
        self.snapshot = copy.deepcopy(self.store)
        self.profiles = {}
        self.profile_snapshot = {}
        self.queries, self.cursors = [], []
        self.commits = self.rollbacks = 0
        self.fail_execute = self.fail_commit = False

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("private commit error")
        self.commits += 1
        self.snapshot = copy.deepcopy(self.store)
        self.profile_snapshot = copy.deepcopy(self.profiles)

    def rollback(self):
        self.rollbacks += 1
        self.store.clear()
        self.store.update(copy.deepcopy(self.snapshot))
        self.profiles.clear()
        self.profiles.update(copy.deepcopy(self.profile_snapshot))


class CloudSummaryTests(unittest.TestCase):
    def test_local_projection_v1_v2_v3_is_detached_and_allowlisted(self):
        sessions = [fixtures.memory(), fixtures.RideMemoryTests().v2(), fixtures.RideMemoryV3Tests().v3()]
        for session in sessions:
            with self.subTest(version=session["schema_version"]):
                original = copy.deepcopy(session)
                result = to_cloud_summary(session)
                self.assertEqual(set(result), {"id", "started_at", "ended_at", "summary",
                                                "source", "activity", "coach_notes"})
                self.assertEqual(session, original)
                self.assertEqual(cloud.validate_summary(result), result)
                for key in ("series", "lat", "lng", "alt", "samples_hz", "persona_version"):
                    self.assertNotIn('"' + key + '"', json.dumps(result))
                result["summary"]["alerts"] = 42
                result["coach_notes"].append("changed")
                self.assertEqual(session, original)

    def test_local_projection_preserves_nulls_simulation_and_activity(self):
        session = fixtures.memory()
        session["source"] = "simulated"
        result = to_cloud_summary(session)
        self.assertEqual(result["source"], "simulated")
        self.assertIsNone(result["summary"]["zone_target"])
        self.assertEqual(result["activity"], "ride")
        for activity in ("ride", "run", "hike"):
            session = fixtures.RideMemoryV3Tests().v3()
            session["activity"] = activity
            self.assertEqual(to_cloud_summary(session)["activity"], activity)

    def test_local_projection_does_not_repair_invalid_recording(self):
        for change in ({"summary": {}}, {"series": []}, {"source": "unknown"}, {"user_id": "forged"}):
            session = fixtures.memory()
            session.update(change)
            with self.subTest(change=change), self.assertRaises(RideMemoryError):
                to_cloud_summary(session)

    def test_cloud_boundary_rejects_raw_records_extra_fields_and_nested_data(self):
        bad = [fixtures.memory()]
        for key in ("series", "gps", "user_id", "token", "schema_version"):
            item = summary(); item[key] = "sensitive"
            bad.append(item)
        for key in ("series", "lat", "lng", "extra"):
            item = summary(); item["summary"][key] = [1, 2]
            bad.append(item)
        item = summary(); item["summary"]["zone_target"] = {"lower": 100, "upper": 150, "lat": 1}
        bad.append(item)
        for item in bad:
            with self.subTest(keys=list(item)), self.assertRaises(cloud.InvalidCloudRide):
                cloud.validate_summary(item)

    def test_cloud_summary_validates_source_activity_id_times_and_notes(self):
        bad = ({"id": "../private"}, {"id": "a' OR 1=1"}, {"id": ""}, {"id": True},
               {"started_at": "tomorrow"}, {"ended_at": "2026-11-06T00:00:00Z"},
               {"ended_at": "2026-11-07T08:02:00Z"}, {"source": []}, {"activity": "drive"},
               {"coach_notes": [3]}, {"coach_notes": [""]}, {"coach_notes": ["a" * 2001]})
        for update in bad:
            item = summary(); item.update(update)
            with self.subTest(update=update), self.assertRaises(cloud.InvalidCloudRide):
                cloud.validate_summary(item)

    def test_cloud_summary_rejects_invalid_aggregate_values(self):
        for key, value in (("duration_s", True), ("duration_s", float("nan")), ("avg_hr", float("inf")),
                           ("avg_hr", -1), ("avg_hr", 65536), ("max_hr", 100),
                           ("max_hr", None), ("gaps", 1.2), ("alerts", False)):
            item = summary(); item["summary"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(cloud.InvalidCloudRide):
                cloud.validate_summary(item)

    def test_cloud_summary_rejects_inconsistent_gps_aggregates_without_accepting_coordinates(self):
        valid = to_cloud_summary(fixtures.RideMemoryV3Tests().v3())
        for key, value in (("distance_m", None), ("avg_speed_mps", 20),
                           ("max_speed_mps", 100), ("ascent_m", -1), ("avg_cadence", 256)):
            item = copy.deepcopy(valid); item["summary"][key] = value
            with self.subTest(key=key), self.assertRaises(cloud.InvalidCloudRide):
                cloud.validate_summary(item)

    def test_cloud_summary_checks_zone_bounds_and_coverage(self):
        valid = summary()
        valid["summary"].update(zone_target={"lower": 100, "upper": 150},
                                time_in_zone_s={"below": 0, "within": 40, "above": 0})
        self.assertEqual(cloud.validate_summary(valid), valid)
        for update in ({"zone_target": {"lower": 150, "upper": 100}},
                       {"time_in_zone_s": {"below": 30, "within": 40, "above": 0}},
                       {"time_in_zone_s": {"below": 0, "within": 40, "above": 0, "gps": []}}):
            item = copy.deepcopy(valid); item["summary"].update(update)
            with self.subTest(update=update), self.assertRaises(cloud.InvalidCloudRide):
                cloud.validate_summary(item)


class CloudRideStorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()
        self.get = patch.object(cloud.db, "get_conn", return_value=self.conn).start()
        self.release = patch.object(cloud.db, "release_conn").start()
        self.refresh = patch.object(cloud, "_refresh_profile").start()
        self.addCleanup(patch.stopall)

    def test_sync_exact_replay_is_idempotent_and_no_raw_fields_reach_sql(self):
        result = cloud.sync("user-a", [summary()])
        self.assertEqual((result["saved"], result["duplicates"]), (1, 0))
        again = cloud.sync("user-a", [summary()])
        self.assertEqual((again["saved"], again["duplicates"]), (0, 1))
        self.assertEqual(len(self.conn.store), 1)
        row = self.conn.store["user-a:ride-1"]
        self.assertEqual(row["dataset"], summary())
        self.assertEqual(row["version"], cloud.SCHEMA_VERSION)
        insert, params = next((sql, params) for sql, params in self.conn.queries
                              if sql.startswith("INSERT INTO rides"))
        self.assertIn("ON CONFLICT (id) DO NOTHING", insert)
        self.assertNotIn('"series"', params[-1])
        self.assertTrue(all(c.closed for c in self.conn.cursors))
        self.assertEqual(self.release.call_count, 2)

    def test_same_local_id_is_independent_for_two_users_and_crud_is_isolated(self):
        first, second = summary(), summary()
        second["coach_notes"] = ["user b private note"]
        cloud.sync("user-a", [first])
        cloud.sync("user-b", [second])
        self.assertEqual(len(self.conn.store), 2)
        self.assertEqual(cloud.get_ride("user-a", "ride-1")["ride"], first)
        self.assertEqual(cloud.list_rides("user-b")["rides"], [second])
        self.assertTrue(cloud.delete_ride("user-a", "ride-1")["deleted"])
        self.assertEqual(cloud.get_ride("user-b", "ride-1")["ride"], second)
        self.assertFalse(cloud.delete_ride("user-a", "ride-1")["deleted"])
        with self.assertRaises(cloud.CloudRideNotFound):
            cloud.get_ride("user-a", "ride-1")

    def test_list_and_delete_never_expose_other_user_ids(self):
        cloud.sync("user-a", [summary("a")])
        self.assertEqual(cloud.list_rides("user-b")["rides"], [])
        self.assertFalse(cloud.delete_ride("user-b", "a")["deleted"])
        with self.assertRaises(cloud.CloudRideNotFound):
            cloud.get_ride("user-b", "a")
        for sql, params in self.conn.queries:
            if sql.startswith(("SELECT dataset", "DELETE FROM rides", "DELETE FROM profiles")):
                self.assertIn("user_id = %s", sql)

    def test_list_is_bounded_and_newest_first(self):
        cloud.sync("user-a", [summary("a"), summary("b")])
        self.assertEqual([row["id"] for row in cloud.list_rides("user-a", 1)["rides"]], ["b"])
        for limit in (0, 101, True, 1.2, "20", None):
            with self.subTest(limit=limit), self.assertRaises(cloud.InvalidCloudRide):
                cloud.list_rides("user-a", limit)

    def test_batch_duplicate_and_conflict_roll_back_whole_batch(self):
        cloud.sync("user-a", [summary()])
        changed = summary(); changed["coach_notes"] = ["different"]
        with self.assertRaises(cloud.CloudRideConflict):
            cloud.sync("user-a", [summary("new"), changed])
        self.assertNotIn("user-a:new", self.conn.store)
        self.assertEqual(self.conn.store["user-a:ride-1"]["dataset"], summary())
        self.assertEqual(self.conn.rollbacks, 1)
        result = cloud.sync("user-a", [summary("new"), summary("new")])
        self.assertEqual((result["saved"], result["duplicates"]), (1, 1))

    def test_all_records_validated_before_any_database_access(self):
        invalid = summary(); invalid["series"] = []
        for batch in ([], [summary()] * 21, [summary(), invalid], {}, None):
            with self.subTest(batch_type=type(batch)), self.assertRaises(cloud.InvalidCloudRide):
                cloud.sync("user-a", batch)
        self.get.assert_not_called()

    def test_storage_errors_are_explicit_sanitized_rolled_back_and_released(self):
        self.conn.fail_execute = True
        with self.assertRaises(db.DatabaseOperationError) as caught:
            cloud.sync("user-a", [summary()])
        self.assertNotIn("password", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.conn.rollbacks, 1)
        self.release.assert_called_once_with(self.conn)
        self.assertTrue(self.conn.cursors[0].closed)
        self.conn.fail_execute = False
        self.conn.fail_commit = True
        with self.assertRaises(db.DatabaseOperationError):
            cloud.sync("user-a", [summary()])
        self.assertEqual(self.conn.store, {})

    def test_cursor_release_failure_still_returns_connection_and_is_sanitized(self):
        with patch.object(FakeCursor, "close", side_effect=RuntimeError("secret driver detail")):
            with self.assertRaises(db.DatabaseOperationError) as caught:
                cloud.sync("user-a", [summary()])
        self.release.assert_called_once_with(self.conn)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("driver", str(caught.exception))

    def test_missing_database_is_not_reported_as_local_or_cloud_success(self):
        self.get.side_effect = db.DatabaseNotConfigured("not configured")
        with self.assertRaises(db.DatabaseNotConfigured):
            cloud.sync("user-a", [summary()])
        self.release.assert_not_called()

    def test_corrupt_or_raw_stored_record_is_not_returned(self):
        cloud.sync("user-a", [summary()])
        self.conn.store["user-a:ride-1"]["dataset"]["gps"] = {"lat": 1, "lng": 2}
        with self.assertRaises(db.DatabaseOperationError):
            cloud.list_rides("user-a")

    def test_data_survives_recreating_the_repository_connection(self):
        cloud.sync("user-a", [summary()])
        replacement = FakeConnection(self.conn.store)
        self.get.return_value = replacement
        self.assertEqual(cloud.get_ride("user-a", "ride-1")["ride"], summary())
        self.assertEqual(len(replacement.store), 1)


if __name__ == "__main__":
    unittest.main()
