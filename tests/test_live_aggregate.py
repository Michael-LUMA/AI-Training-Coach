import unittest
from ride_live import LiveSession

class AggregateTelemetryTests(unittest.TestCase):
    def test_distance_and_facts_without_track(self):
        s = LiveSession()
        s.ingest({'t': 1000, 'distance_m': 0, 'speed_mps': 5, 'hr': 120})
        events = s.ingest({'t': 2000, 'distance_m': 5000, 'speed_mps': 5, 'hr': 125})
        self.assertTrue(any(e['type'] == 'distance_milestone' for e in events))
        self.assertEqual(s.facts()['distance_m'], 5000)
        self.assertFalse(hasattr(s, "points"))
        self.assertEqual(s._last, (2000, None, None))

    def test_invalid_aggregate_never_mutates_session(self):
        s = LiveSession()
        s.ingest({'t': 1000, 'distance_m': 100, 'speed_mps': 5})
        for extra in ({'distance_m': 99}, {'distance_m': float('nan')}, {'lat': 31}, {'alt': 10}):
            p = {'t': 1010, 'distance_m': 100, 'speed_mps': 5, **extra}
            with self.subTest(extra=extra), self.assertRaises(ValueError): s.ingest(p)
            self.assertEqual(s.facts()['distance_m'], 100)
            self.assertEqual(s._last[0], 1000)

    def test_mixed_modes_are_rejected(self):
        s = LiveSession()
        s.ingest({'t': 1000, 'distance_m': 0, 'speed_mps': 5})
        with self.assertRaises(ValueError):
            s.ingest({'t': 1010, 'lat': 31, 'lng': 121, 'speed_mps': 5})

if __name__ == '__main__': unittest.main()
