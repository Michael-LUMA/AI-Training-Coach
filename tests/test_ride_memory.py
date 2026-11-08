"""Synthetic v1 memory contract tests: no devices, network, DB or model calls."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ride_memory import MAX_SERIES, RideMemoryError, SCHEMA_VERSION, validate_session


def memory():
    """Synthetic observed record with an explicit missing-data interval."""
    return {"schema_version": SCHEMA_VERSION, "id": "synthetic-ride_01",
            "started_at": "2026-11-07T08:00:00Z", "ended_at": "2026-11-07T08:01:00Z",
            "samples_hz": 1, "series": [{"t": 0, "hr": 120}, {"t": 1, "gap": True},
                                       {"t": 30, "hr": 140}],
            "summary": {"duration_s": 60, "avg_hr": 130, "max_hr": 140,
                        "time_in_zone_s": None, "zone_target": None, "gaps": 1, "alerts": 0},
            "coach_notes": ["记录中断，缺口未补数。"], "persona_version": "ride-persona/1",
            "source": "ble_gatt_0x180d"}


class RideMemoryTests(unittest.TestCase):
    def reject(self, value):
        with self.assertRaises(RideMemoryError) as caught:
            validate_session(value)
        self.assertTrue(caught.exception.code)
        return caught.exception

    def test_valid_ble_contract_roundtrips_without_mutation(self):
        value = memory(); before = copy.deepcopy(value)
        result = validate_session(value)
        self.assertEqual(result, before)
        self.assertEqual(value, before)
        self.assertEqual(json.loads(json.dumps(result)), before)

    def test_returned_memory_is_deeply_detached(self):
        value = memory(); result = validate_session(value)
        result['series'][0]['hr'] = 999
        result['summary']['alerts'] = 999
        result['coach_notes'].append('changed')
        self.assertEqual(value, memory())
        value['series'][2]['hr'] = 888
        self.assertEqual(result['series'][2]['hr'], 140)

    def test_missing_and_unknown_root_fields(self):
        for key in memory():
            value = memory(); del value[key]
            with self.subTest(missing=key): self.reject(value)
        value = memory(); value['api_key'] = 'private-secret'
        error = self.reject(value)
        self.assertNotIn('private-secret', str(error))

    def test_wrong_root_types_and_version(self):
        for value in (None, [], 'text', True): self.reject(value)
        for version in ('luma-ride-memory/2', None, 1, {}):
            value = memory(); value['schema_version'] = version
            self.reject(value)

    def test_source_mandatory_simulated_preserved_never_assumed_ble(self):
        value = memory(); value['source'] = 'simulated'
        self.assertEqual(validate_session(value)['source'], 'simulated')
        for source in ('', 'BLE', 'SIMULATED', None, [], True):
            value = memory(); value['source'] = source
            self.reject(value)
        value = memory(); del value['source']; self.reject(value)

    def test_gap_preserved_no_interpolation_or_zero_fill(self):
        result = validate_session(memory())
        self.assertEqual(result['series'], [{'t': 0, 'hr': 120}, {'t': 1, 'gap': True}, {'t': 30, 'hr': 140}])
        self.assertIsNone(result['summary']['time_in_zone_s'])
        self.assertEqual(result['summary']['avg_hr'], 130)
        self.assertNotIn('hr', result['series'][1])

    def test_gap_requires_true_and_exclusive_shape(self):
        for point in ({'t': 1, 'gap': False}, {'t': 1, 'gap': 1}, {'t': 1, 'gap': None},
                      {'t': 1, 'hr': 130, 'gap': True}, {'t': 1}, {'t': 1, 'gap': True, 'why': 'disconnect'}, None):
            value = memory(); value['series'][1] = point
            self.reject(value)

    def test_summary_requires_exact_fields(self):
        for key in memory()['summary']:
            value = memory(); del value['summary'][key]; self.reject(value)
        for summary in (None, [], {'unknown': 1}):
            value = memory(); value['summary'] = summary; self.reject(value)
        value = memory(); value['summary']['fitness'] = 99; self.reject(value)

    def test_negative_nonfinite_boolean_and_strings_rejected(self):
        for bad in (-1, float('nan'), float('inf'), -float('inf'), True, '1', None, 10**400):
            for field in ('samples_hz',):
                value = memory(); value[field] = bad; self.reject(value)
            for field in ('duration_s', 'avg_hr', 'max_hr', 'gaps', 'alerts'):
                value = memory(); value['summary'][field] = bad
                with self.subTest(field=field, bad_type=type(bad).__name__): self.reject(value)
            value = memory(); value['series'][1]['t'] = bad; self.reject(value)

    def test_sampling_rate_must_be_positive(self):
        value = memory(); value['samples_hz'] = 0; self.reject(value)
        value['samples_hz'] = .5
        self.assertEqual(validate_session(value)['samples_hz'], .5)

    def test_hr_unsigned_gatt_value_not_invented_physiology(self):
        for hr in (-1, 65536, 120.5, True, None, '120', float('nan'), float('inf')):
            value = memory(); value['series'][0]['hr'] = hr; self.reject(value)
        value = memory(); value['series'] = [{'t': 0, 'hr': 0}, {'t': 1, 'hr': 65535}]
        value['summary'].update(avg_hr=32767.5, max_hr=65535, gaps=0)
        self.assertEqual(validate_session(value)['series'], value['series'])

    def test_series_order_bounds_and_type(self):
        for t in (0, -1, 61):
            value = memory(); value['series'][1]['t'] = t; self.reject(value)
        value = memory(); value['series'][0]['t'] = 2; self.reject(value)
        for series in (None, {}, 'bad', [None]):
            value = memory(); value['series'] = series; self.reject(value)

    def test_series_limit_20000_inclusive(self):
        value = memory(); value['ended_at'] = '2026-11-07T14:00:00Z'
        value['series'] = [{'t': i, 'hr': 120} for i in range(MAX_SERIES)]
        value['summary'].update(duration_s=21600, avg_hr=120, max_hr=120, gaps=0)
        self.assertEqual(len(validate_session(value)['series']), MAX_SERIES)
        value['series'].append({'t': MAX_SERIES, 'hr': 120})
        self.assertEqual(self.reject(value).code, 'series_too_large')

    def test_timestamps_must_be_valid_aware_and_ordered(self):
        for date in ('2026-11-07', '2026-11-07T08:00:00', '2026-02-30T08:00:00Z',
                     '2026-11-07T08:00:00+25:00', '2026-11-07T08:00:00-00:00',
                     '2026-11-07T08:00:60Z', None, 123):
            value = memory(); value['started_at'] = date; self.reject(value)
        value = memory(); value['ended_at'] = '2026-11-07T07:59:59Z'; self.reject(value)
        value = memory(); value['ended_at'] = '2026-11-07T08:01:00.123456Z'
        value['summary']['duration_s'] = 60.123456
        self.assertEqual(validate_session(value), value)

    def test_timezone_offsets_compare_instants_without_rewriting_strings(self):
        value = memory(); value['started_at'] = '2026-11-07T16:00:00+08:00'
        self.assertEqual(validate_session(value), value)
        value['summary']['duration_s'] = 59; self.reject(value)

    def test_empty_or_all_gap_series_keep_missing_averages_null(self):
        for series in ([], [{'t': 0, 'gap': True}]):
            value = memory(); value['series'] = series
            value['summary'].update(avg_hr=None, max_hr=None, gaps=len(series))
            self.assertEqual(validate_session(value), value)
            value['summary']['avg_hr'] = 0; self.reject(value)
        value = memory(); value['series'] = []
        value['ended_at'] = value['started_at']
        value['summary'].update(duration_s=0, avg_hr=None, max_hr=None, gaps=0)
        self.assertEqual(validate_session(value), value)

    def test_summary_matches_recorded_samples_and_gap_count(self):
        for updates in ({'avg_hr': 131}, {'max_hr': 150}, {'gaps': 0}, {'gaps': 1.0},
                        {'alerts': .5}, {'alerts': 2**53}, {'avg_hr': None}):
            value = memory(); value['summary'].update(updates); self.reject(value)

    def test_zone_contract_allows_partial_coverage_not_gap_allocation(self):
        value = memory(); value['summary'].update(zone_target={'lower': 120, 'upper': 140},
                                                 time_in_zone_s={'below': 0, 'within': 1, 'above': 0})
        result = validate_session(value)
        self.assertEqual(result, value)
        self.assertEqual(sum(result['summary']['time_in_zone_s'].values()), 1)
        value['summary']['time_in_zone_s'] = None
        self.assertEqual(validate_session(value), value)

    def test_zone_no_target_no_times_and_exact_shapes(self):
        for target in ({'lower': 150, 'upper': 120}, {'lower': -1, 'upper': 140},
                       {'lower': 120, 'upper': float('nan')}, {'lower': True, 'upper': 140},
                       {'lower': 120}, {'lower': 120, 'upper': 140, 'lthr': 160}):
            value = memory(); value['summary']['zone_target'] = target; self.reject(value)
        value = memory(); value['summary']['time_in_zone_s'] = {'below': 0, 'within': 0, 'above': 0}
        self.reject(value)
        for times in ({'below': 0, 'within': 61, 'above': 0}, {'below': 30, 'within': 30, 'above': 30},
                      {'below': -1, 'within': 1, 'above': 0}, {'below': 0, 'within': float('inf'), 'above': 0},
                      {'below': 0, 'within': 1}, {'below': 0, 'within': 1, 'above': 0, 'other': 0}):
            value = memory(); value['summary'].update(zone_target={'lower': 120, 'upper': 140}, time_in_zone_s=times)
            self.reject(value)

    def test_bounded_safe_identifiers_persona_and_notes(self):
        for identifier in ('../escape', '/abs', 'a/b', 'a\\b', '.', '', 'a'*65, None, '中文'):
            value = memory(); value['id'] = identifier; self.reject(value)
        for persona in ('', None, 'x'*65, '\ud800'):
            value = memory(); value['persona_version'] = persona; self.reject(value)
        for notes in (None, 'text', [{}], [''], ['x'*2001], ['x']*201, ['\ud800'], ['\x00']):
            value = memory(); value['coach_notes'] = notes; self.reject(value)

    def test_input_cycles_fail_with_contract_error_not_recursion(self):
        value = memory(); value['series'][0]['hr'] = value; self.reject(value)
        value = memory(); value['coach_notes'].append(value); self.reject(value)

    def v2(self):
        """Synthetic v2 record: some HR points carry cadence and/or power."""
        value = memory()
        value['schema_version'] = 'luma-ride-memory/2'
        value['series'] = [{'t': 0, 'hr': 120, 'cad': 88, 'pwr': 190},
                           {'t': 1, 'gap': True},
                           {'t': 30, 'hr': 140, 'pwr': 240}]
        value['summary'].update(avg_cadence=88, avg_power=215, max_power=240)
        return value

    def test_v2_valid_roundtrip_and_backward_compatible_v1(self):
        value = self.v2(); before = copy.deepcopy(value)
        self.assertEqual(validate_session(value), before)
        self.assertEqual(value, before)  # no mutation
        self.assertEqual(validate_session(memory()), memory())  # v1 untouched

    def test_v2_summary_requires_exact_ten_fields(self):
        for key in ('avg_cadence', 'avg_power', 'max_power'):
            value = self.v2(); del value['summary'][key]
            with self.subTest(missing=key): self.reject(value)
        value = self.v2(); value['summary']['unknown'] = 1; self.reject(value)
        value = memory(); value['summary']['avg_power'] = None  # v1 stays closed
        self.reject(value)

    def test_v2_cadence_power_ranges_and_gap_purity(self):
        for field, bad in (('cad', -1), ('cad', 256), ('cad', 88.5), ('cad', True), ('cad', '88'),
                           ('pwr', -1), ('pwr', 65536), ('pwr', 190.5), ('pwr', None)):
            value = self.v2(); value['series'][0][field] = bad
            with self.subTest(field=field, bad=bad): self.reject(value)
        value = self.v2(); value['series'][1]['cad'] = 88  # gaps never carry sensors
        self.reject(value)
        value = self.v2(); value['series'][1]['pwr'] = 240
        self.reject(value)

    def test_v2_stats_must_match_recorded_samples(self):
        for updates in ({'avg_cadence': 89}, {'avg_power': 216}, {'max_power': 239},
                        {'avg_power': None}, {'max_power': None}):
            value = self.v2(); value['summary'].update(updates)
            with self.subTest(updates=updates): self.reject(value)

    def test_v2_absent_channels_stay_null_not_zero_filled(self):
        value = self.v2()
        for point in value['series']:
            point.pop('cad', None); point.pop('pwr', None)
        value['summary'].update(avg_cadence=None, avg_power=None, max_power=None)
        self.assertEqual(validate_session(value), value)
        for key in ('avg_cadence', 'avg_power', 'max_power'):
            bad = self.v2()
            for point in bad['series']:
                point.pop('cad', None); point.pop('pwr', None)
            bad['summary'].update(avg_cadence=None, avg_power=None, max_power=None)
            bad['summary'][key] = 0
            with self.subTest(key=key): self.reject(bad)


class RideMemoryV3Tests(unittest.TestCase):
    """v3 contract: optional coordinates on HR points and track aggregates."""

    def reject(self, value):
        with self.assertRaises(RideMemoryError) as caught:
            validate_session(value)
        self.assertTrue(caught.exception.code)
        return caught.exception

    def v3(self):
        """Synthetic v3 record with a small recorded track and one gap."""
        value = {"schema_version": "luma-ride-memory/3", "id": "synthetic-ride_gps01",
                 "started_at": "2026-11-07T08:00:00Z", "ended_at": "2026-11-07T08:01:40Z",
                 "samples_hz": 1,
                 "series": [{"t": 10, "hr": 120, "lat": 40.0, "lng": 116.0, "alt": 100.0},
                            {"t": 20, "hr": 122, "lat": 40.0005, "lng": 116.0005, "alt": 112.0},
                            {"t": 30, "gap": True},
                            {"t": 40, "hr": 124, "lat": 40.001, "lng": 116.001, "alt": 107.0}],
                 "summary": None,
                 "coach_notes": [], "persona_version": "ride-persona/14",
                 "source": "ble_gatt_0x180d"}
        track = [(10.0, 40.0, 116.0, 100.0), (20.0, 40.0005, 116.0005, 112.0),
                 (40.0, 40.001, 116.001, 107.0)]
        from ride_memory import _track_stats, _ascent_m
        distance, max_speed = _track_stats(track)
        value["summary"] = {"duration_s": 100, "avg_hr": 122, "max_hr": 124,
                            "time_in_zone_s": None, "zone_target": None, "gaps": 1, "alerts": 0,
                            "avg_cadence": None, "avg_power": None, "max_power": None,
                            "distance_m": distance, "avg_speed_mps": distance / 100,
                            "max_speed_mps": max_speed, "ascent_m": _ascent_m(track)}
        return value

    def test_v3_valid_roundtrip_and_older_versions_untouched(self):
        value = self.v3(); before = copy.deepcopy(value)
        self.assertEqual(validate_session(value), before)
        self.assertEqual(value, before)

    def test_v3_summary_requires_exact_thirteen_fields(self):
        for key in ('distance_m', 'avg_speed_mps', 'max_speed_mps'):
            value = self.v3(); del value['summary'][key]
            with self.subTest(missing=key): self.reject(value)
        value = self.v3(); value['summary']['pace'] = 1; self.reject(value)

    def test_v3_aggregates_recomputed_from_recorded_track(self):
        for updates in ({'distance_m': 1}, {'avg_speed_mps': 1}, {'max_speed_mps': 1},
                        {'distance_m': None}, {'max_speed_mps': float('nan')}):
            value = self.v3(); value['summary'].update(updates)
            with self.subTest(updates=updates): self.reject(value)

    def test_v3_coordinate_pair_ranges_and_gap_purity(self):
        for field, bad in (('lat', -91), ('lat', 90.1), ('lat', '40'), ('lat', float('inf')),
                           ('lng', -181), ('lng', 180.1), ('lng', None)):
            value = self.v3(); value['series'][0][field] = bad
            with self.subTest(field=field, bad=bad): self.reject(value)
        value = self.v3(); del value['series'][0]['lng']  # pair required
        self.reject(value)
        value = self.v3(); value['series'][2]['lat'] = 1.0  # gaps never carry coords
        self.reject(value)

    def test_v3_stripped_upload_needs_internal_consistency(self):
        value = self.v3()
        for point in value['series']:
            point.pop('lat', None); point.pop('lng', None); point.pop('alt', None)
        self.assertEqual(validate_session(value), value)  # aggregates survive stripping
        for updates in ({'avg_speed_mps': 0.01}, {'max_speed_mps': 0.001},
                        {'max_speed_mps': 71}, {'distance_m': -1}):
            bad = self.v3()
            for point in bad['series']:
                point.pop('lat', None); point.pop('lng', None); point.pop('alt', None)
            bad['summary'].update(updates)
            with self.subTest(updates=updates): self.reject(bad)

    def test_v3_track_and_aggregates_all_or_none(self):
        value = self.v3()
        value['summary'].update(distance_m=None, avg_speed_mps=None, max_speed_mps=None)
        self.reject(value)  # recorded coordinates require aggregates
        bare = self.v3()
        for point in bare['series']:
            point.pop('lat', None); point.pop('lng', None); point.pop('alt', None)
        bare['summary'].update(distance_m=None, avg_speed_mps=None, max_speed_mps=None)
        self.assertEqual(validate_session(bare), bare)  # tracking disabled ride is honest
        bare['summary']['distance_m'] = 5  # one present, two null
        self.reject(bare)

    def test_v3_alt_requires_coords_and_plausible_range(self):
        for bad in (-501, 9001, float('nan'), float('inf'), '100'):
            value = self.v3(); value['series'][0]['alt'] = bad
            with self.subTest(bad=bad): self.reject(value)
        value = self.v3(); del value['series'][0]['lat']; del value['series'][0]['lng']
        self.reject(value)  # alt without the coordinate pair
        value = self.v3(); value['series'][2]['alt'] = 50.0  # gaps never carry alt
        self.reject(value)

    def test_v3_ascent_cross_validated_against_track(self):
        value = self.v3(); value['summary']['ascent_m'] = 999.0
        self.reject(value)  # must equal the recomputed climb
        value = self.v3(); del value['summary']['ascent_m']
        self.reject(value)  # recorded altitude pairs require ascent_m
        value = self.v3(); value['summary']['ascent_m'] = None
        self.reject(value)  # null is not a climb value when alt was recorded
        noalt = self.v3()
        for point in noalt['series']:
            point.pop('alt', None)
        noalt['summary']['ascent_m'] = None
        self.assertEqual(validate_session(noalt), noalt)  # no alt data, no climb claim
        noalt['summary']['ascent_m'] = 12.0
        self.reject(noalt)  # climb without altitude data is fabricated
        stripped = self.v3()
        for point in stripped['series']:
            point.pop('lat', None); point.pop('lng', None); point.pop('alt', None)
        self.assertEqual(validate_session(stripped), stripped)  # stripped keeps the number
        stripped['summary']['ascent_m'] = None
        self.assertEqual(validate_session(stripped), stripped)  # opt-out nulls it honestly
        stripped['summary']['ascent_m'] = -3
        self.reject(stripped)

    def test_v3_activity_label_optional_and_bounded(self):
        for good in ('hike', 'run'):
            value = self.v3(); value['activity'] = good
            with self.subTest(good=good): self.assertEqual(validate_session(value), value)
        for bad in ('swim', '', 1, None):
            value = self.v3(); value['activity'] = bad
            with self.subTest(bad=bad): self.reject(value)
        v2 = self.v3(); v2['schema_version'] = 'luma-ride-memory/2'
        for point in v2['series']:
            point.pop('lat', None); point.pop('lng', None); point.pop('alt', None)
        del v2['summary']['distance_m']; del v2['summary']['avg_speed_mps']
        del v2['summary']['max_speed_mps']; del v2['summary']['ascent_m']
        v2['activity'] = 'hike'
        self.reject(v2)  # activity is a v3 extension


if __name__ == '__main__':
    unittest.main()
