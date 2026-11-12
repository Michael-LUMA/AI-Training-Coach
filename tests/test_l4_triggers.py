import unittest
from ride_live import LiveSession


class L4PredictionTests(unittest.TestCase):
    def setUp(self):
        self.s = LiveSession()

    def predict(self, t, speed=5, hr=None, alt=None, distance=None, duration=None, **limits):
        return self.s._prediction_events(t, speed, hr, alt, t * 5 if distance is None else distance,
                                         t if duration is None else duration, **limits)

    def test_drop_requires_thirty_seconds(self):
        for t in (10, 20, 30):
            self.assertFalse(self.predict(t, speed=2))
        self.assertEqual(self.predict(40, speed=2)[0]['type'], 'speed_drop')

    def test_drop_resets_on_recovery(self):
        for t in (10, 20): self.predict(t, speed=2)
        self.predict(30, speed=5)
        self.assertFalse(self.predict(40, speed=2))

    def test_drop_resets_on_sparse_samples(self):
        self.predict(10, speed=2)
        self.assertFalse(self.predict(45, speed=2))

    def test_drop_limit(self):
        events = []
        for t in range(10, 171, 10): events += self.s.arbitrate(self.predict(t, speed=2), t, False)
        self.assertEqual(sum(e['type'] == 'speed_drop' for e in events), 2)

    def test_discarded_prediction_does_not_consume_quota(self):
        for t in (10, 20, 30): self.predict(t, speed=2)
        candidates = self.predict(40, speed=2)
        self.s.arbitrate(candidates + [{'type': 'hr_high'}], 40, False)
        self.assertEqual(self.s._l4_counts['speed_drop'], 0)
        self.assertEqual(self.s.arbitrate(self.predict(50, speed=2), 50, False)[0]['type'], 'speed_drop')
        self.assertEqual(self.s._l4_counts['speed_drop'], 1)

    def test_expired_queued_prediction_does_not_consume_quota(self):
        for t in (10, 20, 30): self.predict(t, speed=2)
        self.s.arbitrate(self.predict(40, speed=2), 40, True)
        self.assertFalse(self.s.arbitrate([], 71, False))
        self.assertEqual(self.s._l4_counts['speed_drop'], 0)

    def baseline(self):
        for t in (0, 30, 60): self.predict(t, hr=120)

    def test_drift_at_five(self):
        self.baseline()
        self.assertEqual(self.predict(70, hr=125)[0]['type'], 'hr_drift')

    def test_drift_below_threshold(self):
        self.baseline()
        self.assertFalse(self.predict(70, hr=124))

    def test_drift_requires_hr(self):
        self.baseline()
        self.assertFalse(self.predict(70))

    def test_drift_requires_comparable_speed(self):
        self.baseline()
        self.assertFalse(self.predict(70, speed=6, hr=135))

    def test_drift_rejects_sparse_baseline(self):
        self.predict(0, hr=120)
        self.assertFalse(self.predict(70, hr=130))

    def test_drift_rejects_exceeding_limit(self):
        self.baseline()
        self.assertFalse(self.predict(70, hr=150, hr_upper=140))

    def test_drift_limit(self):
        self.baseline()
        events = []
        for t in (70, 130, 190, 250): events += self.s.arbitrate(self.predict(t, hr=150), t, False)
        self.assertEqual(sum(e['type'] == 'hr_drift' for e in events), 2)

    def test_history_does_not_claim_forward_climb(self):
        for t in range(0, 400, 10):
            self.assertFalse(any(e['type'] == 'climb_ahead' for e in self.predict(t, alt=t/2)))


class SafetyRetryTests(unittest.TestCase):
    def test_failed_safety_rechecks_fresh_samples(self):
        for kind, limit, outside, recovered in (
                ('hr_high', 'hr_upper', 155, 130),
                ('hr_low', 'hr_lower', 85, 110)):
            for still_outside in (True, False):
                with self.subTest(kind=kind, still_outside=still_outside):
                    session = LiveSession()
                    def point(t, hr):
                        return {'t': t, 'distance_m': t * 3, 'speed_mps': 3,
                                'hr': hr, limit: 140 if kind == 'hr_high' else 100}
                    session.ingest(point(0, outside))
                    first = session.ingest(point(10, outside))
                    token = object()
                    self.assertEqual(session.arbitrate(first, 10, False, token)[0]['type'], kind)
                    session.finish_reservation(token, False)
                    fresh = session.ingest(point(20, outside if still_outside else recovered))
                    matching = [event for event in fresh if event['type'] == kind]
                    self.assertEqual(len(matching), int(still_outside))
                    if matching:
                        self.assertEqual(matching[0]['hr'], outside)

    def test_late_failure_after_recovery_does_not_revive_old_warning(self):
        s = LiveSession()
        def p(t, hr):
            return {'t': t, 'distance_m': t * 3, 'speed_mps': 3,
                    'hr': hr, 'hr_upper': 140}
        s.ingest(p(0, 155))
        first = s.ingest(p(10, 155))
        token = object()
        s.arbitrate(first, 10, False, token)
        s.ingest(p(20, 130))
        s.ingest(p(35, 130))
        s.finish_reservation(token, False)
        self.assertFalse(s._failed_safety)
        self.assertFalse(any(e['type'] == 'hr_high' for e in s.ingest(p(40, 155))))
        self.assertTrue(any(e['type'] == 'hr_high' for e in s.ingest(p(50, 155))))

    def test_removed_limit_invalidates_late_failure(self):
        for kind, limit, outside in (('hr_high', 'hr_upper', 170),
                                     ('hr_low', 'hr_lower', 90)):
            with self.subTest(kind=kind):
                s = LiveSession()
                def point(t, hr=None, enabled=True):
                    p = {'t': t, 'distance_m': t * 3, 'speed_mps': 3}
                    if hr is not None: p['hr'] = hr
                    if enabled: p[limit] = 160 if kind == 'hr_high' else 100
                    return p
                s.ingest(point(0, outside))
                token = object()
                s.arbitrate(s.ingest(point(10, outside)), 10, False, token)
                s.ingest(point(11, enabled=False))
                s.finish_reservation(token, False)
                self.assertNotIn(kind, s._failed_safety)
                self.assertFalse(s.ingest(point(12, outside)))
                self.assertEqual(s.ingest(point(22, outside))[0]['type'], kind)

    def test_old_failure_after_new_success_does_not_retry(self):
        s = LiveSession()
        old, new = object(), object()
        event = {'type': 'hr_high'}
        s.arbitrate([event], 0, False, old)
        s.arbitrate([event], 61, False, new)
        s.finish_reservation(new, True)
        s.finish_reservation(old, False)
        self.assertFalse(s._failed_safety)

    def test_missing_samples_invalidate_late_failure(self):
        s = LiveSession()
        def p(t, hr):
            point = {'t': t, 'distance_m': t * 3, 'speed_mps': 3, 'hr_upper': 140}
            if hr is not None: point['hr'] = hr
            return point
        s.ingest(p(0, 155))
        token = object()
        s.arbitrate(s.ingest(p(10, 155)), 10, False, token)
        s.ingest(p(20, None))
        s.ingest(p(80, None))
        s.finish_reservation(token, False)
        self.assertFalse(s._failed_safety)
        self.assertFalse(any(e['type'] == 'hr_high' for e in s.ingest(p(90, 155))))
        self.assertTrue(any(e['type'] == 'hr_high' for e in s.ingest(p(100, 155))))

class L4ArbiterTests(unittest.TestCase):
    def setUp(self): self.s = LiveSession()
    def event(self, name): return {'type': name}
    def test_safety_over_milestone(self):
        self.assertEqual(self.s.arbitrate([self.event('fuel'), self.event('hr_high')], 0, False)[0]['type'], 'hr_high')
    def test_state_over_fuel(self):
        self.assertEqual(self.s.arbitrate([self.event('fuel'), self.event('speed_drop')], 0, False)[0]['type'], 'speed_drop')
    def test_same_type_deduplicated(self):
        e = self.event('fuel')
        self.assertTrue(self.s.arbitrate([e], 0, False))
        self.assertFalse(self.s.arbitrate([e], 59, False))
        self.assertTrue(self.s.arbitrate([e], 60, False))
    def test_silence_queue(self):
        e = self.event('fuel')
        self.assertFalse(self.s.arbitrate([e], 0, True))
        self.assertEqual(self.s.arbitrate([], 10, False), [e])
    def test_queue_expires(self):
        self.s.arbitrate([self.event('fuel')], 0, True)
        self.assertFalse(self.s.arbitrate([], 31, False))
    def test_recovered_prediction_is_not_spoken_from_queue(self):
        for name in ('speed_drop', 'hr_drift'):
            with self.subTest(name=name):
                session = LiveSession()
                session.arbitrate([self.event(name)], 0, True)
                self.assertFalse(session.arbitrate([], 10, False))
                self.assertEqual(session._l4_counts[name], 0)

    def test_safety_bypasses_silence_preserves_pending(self):
        fuel, safety = self.event('fuel'), self.event('hr_high')
        self.s.arbitrate([fuel], 0, True)
        self.assertEqual(self.s.arbitrate([safety], 1, True), [safety])
        self.assertEqual(self.s.arbitrate([], 15, False), [fuel])


class ReservationTests(unittest.TestCase):
    def test_interleaved_success_failure_do_not_corrupt_counts_or_cooldown(self):
        for first_success in (True, False):
            s = LiveSession()
            a, b = object(), object()
            s.arbitrate([{'type': 'speed_drop'}], 0, False, reservation=a)
            s.arbitrate([{'type': 'speed_drop'}], 61, False, reservation=b)
            self.assertEqual(s._reserved_count('speed_drop'), 2)
            s.finish_reservation(b, not first_success)
            s.finish_reservation(a, first_success)
            self.assertEqual(s._l4_counts['speed_drop'], 1)
            self.assertEqual(s._spoken_at['speed_drop'], 0 if first_success else 61)
            self.assertFalse(s._reservations)

    def test_failed_reservation_releases_quota_and_cooldown(self):
        s = LiveSession()
        token = object()
        event = {'type': 'speed_drop'}
        s.arbitrate([event], 0, False, reservation=token)
        self.assertFalse(s.arbitrate([event], 1, False))
        s.finish_reservation(token, False)
        self.assertEqual(s._l4_counts['speed_drop'], 0)
        self.assertEqual(s.arbitrate([event], 1, False), [event])


class PendingReservationTests(unittest.TestCase):
    def test_failed_queued_one_shot_can_retry_without_extending_expiry(self):
        s = LiveSession()
        event = {'type': 'fuel'}
        s.arbitrate([event], 0, True)
        token = object()
        self.assertEqual(s.arbitrate([], 10, False, reservation=token), [event])
        s.finish_reservation(token, False)
        self.assertEqual(s.arbitrate([], 20, False), [event])

    def test_failed_queued_event_still_expires_from_original_time(self):
        s = LiveSession()
        s.arbitrate([{'type': 'fuel'}], 0, True)
        token = object()
        s.arbitrate([], 10, False, reservation=token)
        s.finish_reservation(token, False)
        self.assertFalse(s.arbitrate([], 31, False))

    def test_failure_does_not_overwrite_newer_pending_event(self):
        s = LiveSession()
        s.arbitrate([{'type': 'fuel'}], 0, True)
        token = object()
        s.arbitrate([], 10, False, reservation=token)
        newer = {'type': 'distance_milestone'}
        s.arbitrate([newer], 11, True)
        s.finish_reservation(token, False)
        self.assertEqual(s.arbitrate([], 12, False), [newer])

    def test_failed_prediction_is_not_replayed_after_recovery(self):
        s = LiveSession()
        e = {'type': 'speed_drop'}
        s.arbitrate([e], 0, True)
        token = object()
        s.arbitrate([e], 10, False, reservation=token)
        s.finish_reservation(token, False)
        self.assertFalse(s.arbitrate([], 11, False))

    def test_failed_unqueued_one_shot_can_retry(self):
        s = LiveSession()
        event = {'type': 'distance_milestone'}
        token = object()
        s.arbitrate([event], 0, False, reservation=token)
        s.finish_reservation(token, False)
        self.assertEqual(s.arbitrate([], 10, False), [event])
