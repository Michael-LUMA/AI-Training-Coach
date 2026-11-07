"""Synthetic GPX contracts; offline only, with no real entity targets."""
import hashlib
import io
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ride_gpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class RideGPXTests(unittest.TestCase):
    def fixture(self, name):
        return (FIXTURES / ("ride_gpx_" + name + ".gpx")).read_bytes()

    def parse(self, name):
        return ride_gpx.parse_gpx(self.fixture(name))

    def assert_metric(self, result, name, expected, unit, reason=None):
        metric = result["metrics"][name]
        self.assertEqual(metric["unit"], unit)
        self.assertEqual(metric["origin"], "calculated")
        self.assertTrue(metric["method"])
        self.assertEqual(metric["missing_reason"], reason)
        if expected is None:
            self.assertIsNone(metric["value"])
        else:
            self.assertAlmostEqual(metric["value"], expected, places=6)
        return metric

    @staticmethod
    def eastward_distance(latitude, longitude_delta=0.001):
        # Independent spherical formula for equal-latitude fixture endpoints.
        radius = 6371008.8
        half_chord = math.cos(math.radians(latitude)) * math.sin(
            math.radians(longitude_delta) / 2
        )
        return 2 * radius * math.asin(abs(half_chord))

    def test_normal_fields_and_raw_byte_provenance(self):
        raw = self.fixture("normal")
        result = ride_gpx.parse_gpx(raw)
        self.assertEqual(result["schema_version"], "luma-ride-gpx/1")
        self.assertEqual(result["source"], {
            "kind": "gpx", "sha256": hashlib.sha256(raw).hexdigest(),
            "hash_scope": "raw_file_bytes", "byte_length": len(raw),
        })
        self.assertEqual(result["point_count"], 3)
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["segment_index"], 0)
        points = result["segments"][0]["points"]
        for index, (elevation, heart_rate, power) in enumerate(
            ((10, 100, 100), (15, 120, 200), (12, 140, 300))
        ):
            with self.subTest(point=index):
                self.assertEqual(points[index], {
                    "segment_index": 0, "point_index": index,
                    "time": "2026-01-01T00:00:%02dZ" % (index * 10),
                    "lat": 0.0, "lon": index * 0.001, "ele": elevation,
                    "heart_rate_bpm": heart_rate, "power_w": power, "cadence_rpm": None,
                })
        self.assertEqual(result["warnings"], [])

    def test_normal_derived_metrics_units_and_coverage(self):
        result = self.parse("normal")
        expected = (
            ("total_distance_m", 6371008.8 * math.radians(0.002), "m"),
            ("total_duration_s", 20, "s"),
            ("elevation_gain_m", 5, "m"),
            ("average_heart_rate_bpm", 120, "bpm"),
            ("average_power_w", 200, "W"),
        )
        self.assertEqual(set(result["metrics"]), {item[0] for item in expected})
        for name, value, unit in expected:
            with self.subTest(metric=name):
                metric = self.assert_metric(result, name, value, unit)
                for key, count in (("possible_edges", 2), ("timed_edges", 2),
                                   ("excluded_time_edges", 0), ("declared_segments", 1)):
                    self.assertEqual(metric["coverage"][key], count)

    def test_file_api_matches_original_bytes_api(self):
        path = FIXTURES / "ride_gpx_normal.gpx"
        self.assertEqual(ride_gpx.parse_gpx_file(path), self.parse("normal"))

    def test_sensor_means_are_time_weighted_not_sample_means(self):
        raw = self.fixture("normal").replace(b"00:00:20Z", b"00:00:30Z")
        result = ride_gpx.parse_gpx(raw)
        self.assert_metric(result, "total_duration_s", 30, "s")
        self.assert_metric(result, "average_heart_rate_bpm", (110 * 10 + 130 * 20) / 30, "bpm")
        self.assert_metric(result, "average_power_w", (150 * 10 + 250 * 20) / 30, "W")

    def test_declared_breaks_do_not_bridge_distance_time_or_elevation(self):
        result = self.parse("breaks")
        self.assertEqual(result["point_count"], 4)
        self.assertEqual([s["segment_index"] for s in result["segments"]], [0, 1])
        self.assertEqual([len(s["points"]) for s in result["segments"]], [2, 2])
        for segment_index, segment in enumerate(result["segments"]):
            for point_index, point in enumerate(segment["points"]):
                self.assertEqual(point["segment_index"], segment_index)
                self.assertEqual(point["point_index"], point_index)
                self.assertIsNone(point["heart_rate_bpm"])
                self.assertIsNone(point["power_w"])
        distance = self.eastward_distance(0) + self.eastward_distance(45)
        self.assert_metric(result, "total_distance_m", distance, "m")
        duration = self.assert_metric(result, "total_duration_s", 20, "s")
        self.assert_metric(result, "elevation_gain_m", 8, "m")
        self.assertEqual(duration["coverage"]["declared_segments"], 2)
        self.assertEqual(duration["coverage"]["possible_edges"], 2)
        for name, unit in (("average_heart_rate_bpm", "bpm"), ("average_power_w", "W")):
            self.assert_metric(result, name, None, unit, "discontinuous_track")

    def test_reverse_time_preserves_order_and_excludes_only_invalid_edge(self):
        result = self.parse("reverse")
        points = result["segments"][0]["points"]
        self.assertEqual(result["point_count"], 4)
        self.assertEqual([p["point_index"] for p in points], [0, 1, 2, 3])
        self.assertEqual([p["time"] for p in points], [
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z",
            "2026-01-01T00:00:05Z", "2026-01-01T00:00:15Z",
        ])
        self.assertEqual([p["lat"] for p in points], [0, 0, 50, 50])
        self.assertEqual([p["power_w"] for p in points], [100, 200, 300, 400])
        self.assert_metric(result, "total_distance_m",
                           self.eastward_distance(0) + self.eastward_distance(50), "m")
        duration = self.assert_metric(result, "total_duration_s", 20, "s")
        self.assert_metric(result, "elevation_gain_m", 5, "m")
        self.assertEqual(duration["coverage"]["possible_edges"], 3)
        self.assertEqual(duration["coverage"]["timed_edges"], 2)
        self.assertEqual(duration["coverage"]["excluded_time_edges"], 1)
        self.assertIn({"code": "non_increasing_time", "segment_index": 0,
                       "point_index": 2, "field": "time"}, result["warnings"])
        for name, unit in (("average_heart_rate_bpm", "bpm"), ("average_power_w", "W")):
            self.assert_metric(result, name, None, unit, "invalid_or_gapped_time_sequence")

    def test_missing_hr_is_null_with_reason_but_zero_power_is_valid(self):
        result = self.parse("missing_hr")
        points = result["segments"][0]["points"]
        self.assertEqual([p["heart_rate_bpm"] for p in points], [100, None, 120])
        self.assertEqual([p["power_w"] for p in points], [0, 100, 200])
        self.assertIn({"code": "missing_value", "segment_index": 0,
                       "point_index": 1, "field": "heart_rate_bpm"}, result["warnings"])
        hr = self.assert_metric(result, "average_heart_rate_bpm", None, "bpm",
                                "incomplete_sensor_sequence")
        self.assertEqual(hr["coverage"]["sample_count"], 3)
        self.assertEqual(hr["coverage"]["valid_sensor_samples"], 2)
        self.assert_metric(result, "average_power_w", 100, "W")
        self.assert_metric(result, "total_duration_s", 20, "s")
        self.assert_metric(result, "elevation_gain_m", 0, "m")
        self.assert_metric(result, "total_distance_m", 6371008.8 * math.radians(0.002), "m")

    def test_seed_missing_fields_and_unavailable_metrics_stay_null(self):
        result = self.parse("oversize_seed")
        self.assertEqual(result["point_count"], 1)
        self.assertEqual(result["segments"][0]["points"][0], {
            "segment_index": 0, "point_index": 0, "time": None,
            "lat": 0, "lon": 0, "ele": None, "heart_rate_bpm": None, "power_w": None, "cadence_rpm": None,
        })
        self.assertIn({"code": "missing_value", "segment_index": 0,
                       "point_index": 0, "field": "time"}, result["warnings"])
        for field, label in (("ele", "海拔"), ("heart_rate_bpm", "心率"),
                             ("power_w", "功率"), ("cadence_rpm", "踏频")):
            self.assertNotIn({"code": "missing_value", "segment_index": 0,
                              "point_index": 0, "field": field}, result["warnings"])
            self.assertIn("本次无%s数据（%s；全程未记录）" % (label, field),
                          result["limitations"])
        for name, unit, reason in (
            ("total_distance_m", "m", "insufficient_coordinates_or_time"),
            ("total_duration_s", "s", "insufficient_valid_time_intervals"),
            ("elevation_gain_m", "m", "insufficient_elevation_or_time"),
            ("average_heart_rate_bpm", "bpm", "insufficient_samples"),
            ("average_power_w", "W", "insufficient_samples"),
        ):
            self.assert_metric(result, name, None, unit, reason)

    def test_five_mib_boundary_and_oversize_rejected_before_xml_parser(self):
        self.assertEqual(ride_gpx.MAX_FILE_BYTES, 5 * 1024 * 1024)
        seed = self.fixture("oversize_seed")
        at_limit = seed + b" " * (ride_gpx.MAX_FILE_BYTES - len(seed))
        self.assertEqual(ride_gpx.parse_gpx(at_limit)["source"]["byte_length"], len(at_limit))
        with patch.object(ride_gpx, "_safe_tree") as parser:
            with self.assertRaises(ride_gpx.GPXError) as caught:
                ride_gpx.parse_gpx(at_limit + b" ")
            self.assertEqual(caught.exception.code, "file_too_large")
            parser.assert_not_called()

    def test_file_api_bounds_read_and_rejects_oversize_before_parser(self):
        seed = self.fixture("oversize_seed")
        oversized = seed + b" " * (ride_gpx.MAX_FILE_BYTES + 1 - len(seed))
        stream = io.BytesIO(oversized)
        with patch.object(Path, "open", return_value=stream) as open_file:
            with patch.object(stream, "read", wraps=stream.read) as read_file:
                with patch.object(ride_gpx, "_safe_tree") as parser:
                    with self.assertRaises(ride_gpx.GPXError) as caught:
                        ride_gpx.parse_gpx_file("synthetic-oversize.gpx")
                    self.assertEqual(caught.exception.code, "file_too_large")
                    read_file.assert_called_once_with(ride_gpx.MAX_FILE_BYTES + 1)
                    parser.assert_not_called()
            open_file.assert_called_once_with("rb")

    def assert_entity_rejected_before_tree(self, name):
        raw = self.fixture(name)
        # Exercise the actual Expat parser, not a mocked rejection. UTF-16 also
        # rules out relying solely on an ASCII DOCTYPE substring blacklist.
        for encoding, data in (("fixture", raw),
                               ("utf-16", raw.decode("utf-8-sig").encode("utf-16"))):
            with self.subTest(fixture=name, encoding=encoding):
                builder = Mock(wraps=ride_gpx.TreeBuilder())
                with patch.object(ride_gpx, "TreeBuilder", return_value=builder):
                    with self.assertRaises(ride_gpx.GPXError) as caught:
                        ride_gpx.parse_gpx(data)
                self.assertEqual(caught.exception.code, "unsafe_xml")
                self.assertIn("forbidden", str(caught.exception))
                # DTD rejection must happen before any document element or
                # entity-expanded text reaches the tree (not a later numeric
                # conversion failure, missing field, or generic XML error).
                builder.start.assert_not_called()
                builder.data.assert_not_called()
                builder.close.assert_not_called()

    def test_external_entity_explicitly_rejected_without_expansion(self):
        self.assert_entity_rejected_before_tree("external_entity")

    def test_internal_entities_explicitly_rejected_without_expansion(self):
        self.assert_entity_rejected_before_tree("internal_entity")


    @staticmethod
    def sensor_track(points):
        return ('<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1" '
                'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" '
                'xmlns:p="http://www.garmin.com/xmlschemas/PowerExtension/v1">'
                '<trk><trkseg>' + ''.join(points) + '</trkseg></trk></gpx>').encode('utf-8')

    @staticmethod
    def sensor_point(second, position='', sensors=''):
        return ('<trkpt ' + position + '><time>2026-11-06T00:00:%02dZ</time>' % second
                + '<extensions>' + sensors + '</extensions></trkpt>')

    def test_sensor_track_without_any_position_has_no_route_or_distance(self):
        raw = self.sensor_track([self.sensor_point(t) for t in (0, 10, 20)])
        result = ride_gpx.parse_gpx(raw)
        self.assertFalse(result['has_position'])
        self.assertEqual(result['route_summary'], {'value': None, 'missing_reason': 'no_valid_positions'})
        for point in result['segments'][0]['points']:
            for field in ('lat', 'lon', 'ele', 'heart_rate_bpm', 'power_w', 'cadence_rpm'):
                self.assertIsNone(point[field])
        self.assert_metric(result, 'total_duration_s', 20, 's')
        self.assert_metric(result, 'total_distance_m', None, 'm', 'no_valid_positions')
        self.assert_metric(result, 'elevation_gain_m', None, 'm', 'insufficient_elevation_or_time')
        self.assert_metric(result, 'average_heart_rate_bpm', None, 'bpm', 'incomplete_sensor_sequence')
        self.assertEqual(result['source']['sha256'], hashlib.sha256(raw).hexdigest())

    def test_sensor_track_without_position_preserves_hr_power_and_cadence(self):
        points = [self.sensor_point(t, sensors=(
            '<gpxtpx:TrackPointExtension><gpxtpx:hr>%d</gpxtpx:hr>'
            '<gpxtpx:cad>%d</gpxtpx:cad></gpxtpx:TrackPointExtension>'
            '<p:power>%d</p:power>' % (hr, cad, power)))
            for t, hr, cad, power in ((0, 100, 80, 0), (10, 120, 90, 100), (20, 140, 100, 200))]
        result = ride_gpx.parse_gpx(self.sensor_track(points))
        self.assertFalse(result['has_position'])
        self.assertEqual([p['cadence_rpm'] for p in result['segments'][0]['points']], [80, 90, 100])
        self.assert_metric(result, 'average_heart_rate_bpm', 120, 'bpm')
        self.assert_metric(result, 'average_power_w', 100, 'W')
        self.assert_metric(result, 'total_distance_m', None, 'm', 'no_valid_positions')

    def test_partial_positions_exclude_both_adjacent_route_edges_not_sensor_receipts(self):
        points = [self.sensor_point(i * 10, '' if i == 2 else 'lat="0" lon="%s"' % (i * .001),
                                    '<gpxtpx:hr>100</gpxtpx:hr>') for i in range(5)]
        result = ride_gpx.parse_gpx(self.sensor_track(points))
        self.assertTrue(result['has_position'])
        metric = self.assert_metric(result, 'total_distance_m', 6371008.8 * math.radians(.002), 'm')
        self.assertEqual(metric['coverage']['used_edges'], 2)
        missing = [w['point_index'] for w in result['warnings'] if w['code'] == 'missing_coordinate_boundary']
        self.assertEqual(missing, [2, 3])
        self.assertIsNone(result['segments'][0]['points'][2]['lat'])
        self.assert_metric(result, 'total_duration_s', 40, 's')
        self.assert_metric(result, 'average_heart_rate_bpm', 100, 'bpm')

    def test_incomplete_or_invalid_coordinate_pairs_do_not_claim_position(self):
        for attrs in ('lat="0"', 'lon="1"', 'lat="NaN" lon="1"', 'lat="91" lon="1"'):
            with self.subTest(attrs=attrs):
                result = ride_gpx.parse_gpx(self.sensor_track([
                    self.sensor_point(0, attrs), self.sensor_point(10, attrs)]))
                self.assertFalse(result['has_position'])
                self.assertIsNone(result['metrics']['total_distance_m']['value'])

    def test_cadence_missing_unknown_namespace_invalid_and_duplicate_stay_null(self):
        for sensors in ('', '<cad xmlns="urn:unknown">90</cad>', '<gpxtpx:cad>NaN</gpxtpx:cad>',
                        '<gpxtpx:cad>-1</gpxtpx:cad>', '<gpxtpx:cad>80</gpxtpx:cad><gpxtpx:cad>90</gpxtpx:cad>'):
            with self.subTest(sensors=sensors):
                result = ride_gpx.parse_gpx(self.sensor_track([self.sensor_point(0, sensors=sensors)]))
                self.assertIsNone(result['segments'][0]['points'][0]['cadence_rpm'])
        result = ride_gpx.parse_gpx(self.sensor_track([self.sensor_point(0, sensors='<gpxtpx:cad>0</gpxtpx:cad>')]))
        self.assertEqual(result['segments'][0]['points'][0]['cadence_rpm'], 0)

    def test_real_capture_serializer_fixture_is_accepted_without_fake_trackpoints(self):
        raw = self.fixture('capture_sensor_only')
        self.assertNotIn(b'<trkpt', raw)
        result = ride_gpx.parse_gpx(raw)
        self.assertEqual(result['point_count'], 3)
        self.assertFalse(result['has_position'])
        self.assertEqual([p['heart_rate_bpm'] for p in result['segments'][0]['points']], [100, 120, 140])
        self.assert_metric(result, 'total_duration_s', 2, 's')
        self.assert_metric(result, 'average_heart_rate_bpm', 120, 'bpm')
        self.assertEqual(result['source']['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['source']['byte_length'], len(raw))

    def test_capture_gap_is_retained_and_breaks_continuous_edges_even_without_split(self):
        raw = self.fixture('capture_sensor_only').replace(b'</luma:sensorSeries>',
            b'<luma:gap time="2026-11-06T00:00:01Z" reason="disconnected"/></luma:sensorSeries>')
        result = ride_gpx.parse_gpx(raw)
        self.assertEqual([len(s['points']) for s in result['segments']], [1, 2])
        self.assert_metric(result, 'total_duration_s', 1, 's')
        self.assert_metric(result, 'average_heart_rate_bpm', None, 'bpm', 'discontinuous_track')
        self.assertTrue(any(w['code'] == 'capture_gap' and w['reason'] == 'disconnected' for w in result['warnings']))

    def test_capture_unknown_version_mixed_format_and_forged_position_rejected(self):
        raw = self.fixture('capture_sensor_only')
        for data in (raw.replace(b'schema_version="luma-ride-capture/1"', b'schema_version="unknown"'),
                     raw.replace(b'gps="none"', b'gps="yes"'),
                     raw.replace(b'</gpx>', b'<trk><trkseg><trkpt lat="0" lon="0"/></trkseg></trk></gpx>'),
                     raw.replace(b'<luma:sample time=', b'<luma:sample lat="0" time=')):
            with self.subTest(data=data[:30]):
                with self.assertRaises(ride_gpx.GPXError) as caught:
                    ride_gpx.parse_gpx(data)
                self.assertEqual(caught.exception.code, 'invalid_capture_series')

    def test_capture_samples_share_the_existing_point_limit_before_adaptation(self):
        with patch.object(ride_gpx, 'MAX_POINTS', 1), patch.object(ride_gpx, '_capture_tracks') as adapt:
            with self.assertRaises(ride_gpx.GPXError) as caught:
                ride_gpx.parse_gpx(self.fixture('capture_sensor_only'))
            self.assertEqual(caught.exception.code, 'too_many_points')
            adapt.assert_not_called()

    def test_capture_entities_are_still_explicitly_rejected_before_expansion(self):
        raw = self.fixture('capture_sensor_only')
        for entity in (b'<!ENTITY x "not-expanded">', b'<!ENTITY x SYSTEM "file:///nonexistent-synthetic-target">'):
            data = raw.replace(b'?>', b'?>\n<!DOCTYPE gpx [' + entity + b']>', 1)
            with self.assertRaises(ride_gpx.GPXError) as caught:
                ride_gpx.parse_gpx(data)
            self.assertEqual(caught.exception.code, 'unsafe_xml')


    def test_cadence_never_recorded_is_record_level_absence_not_point_warning(self):
        result = self.parse('normal')
        self.assertEqual(result['warnings'], [])
        self.assertTrue(all(p['cadence_rpm'] is None for p in result['segments'][0]['points']))
        self.assertIn('本次无踏频数据（cadence_rpm；全程未记录）', result['limitations'])

    def test_partially_recorded_cadence_keeps_point_level_missing_warnings(self):
        raw = self.fixture('normal').replace(b'</gpxtpx:TrackPointExtension>',
            b'<gpxtpx:cad>90</gpxtpx:cad></gpxtpx:TrackPointExtension>', 1)
        result = ride_gpx.parse_gpx(raw)
        self.assertEqual([p['cadence_rpm'] for p in result['segments'][0]['points']], [90, None, None])
        self.assertEqual([w for w in result['warnings'] if w.get('field') == 'cadence_rpm'], [
            {'code': 'missing_value', 'segment_index': 0, 'point_index': 1, 'field': 'cadence_rpm'},
            {'code': 'missing_value', 'segment_index': 0, 'point_index': 2, 'field': 'cadence_rpm'},
        ])
        self.assertNotIn('本次无踏频数据（cadence_rpm；全程未记录）', result['limitations'])

    def test_declared_invalid_cadence_is_not_silently_classified_as_never_recorded(self):
        raw = self.fixture('normal').replace(b'</gpxtpx:TrackPointExtension>',
            b'<gpxtpx:cad>NaN</gpxtpx:cad></gpxtpx:TrackPointExtension>', 1)
        result = ride_gpx.parse_gpx(raw)
        self.assertIn({'code': 'invalid_value', 'segment_index': 0, 'point_index': 0,
                       'field': 'cadence_rpm'}, result['warnings'])
        self.assertIn({'code': 'missing_value', 'segment_index': 0, 'point_index': 1,
                       'field': 'cadence_rpm'}, result['warnings'])
        self.assertNotIn('本次无踏频数据（cadence_rpm；全程未记录）', result['limitations'])


    # Presence is based on recognized XML elements across the whole record.
    OPTIONAL_CHANNELS = (("heart_rate_bpm", "心率", "gpxtpx:hr"),
                         ("power_w", "功率", "p:power"),
                         ("cadence_rpm", "踏频", "gpxtpx:cad"),
                         ("ele", "海拔", "ele"))

    def optional_channel_point(self, second, field, tag, values):
        elements = ''.join('<%s>%s</%s>' % (tag, value, tag) for value in values)
        point = self.sensor_point(second, 'lat="0" lon="0"',
                                  '' if field == 'ele' else elements)
        return point.replace('</trkpt>', elements + '</trkpt>') if field == 'ele' else point

    def test_all_optional_channels_never_present_have_only_record_absence(self):
        result = ride_gpx.parse_gpx(self.sensor_track([
            self.sensor_point(t, 'lat="0" lon="0"') for t in (0, 10)]))
        self.assertEqual(result['warnings'], [])
        for field, label, tag in self.OPTIONAL_CHANNELS:
            with self.subTest(field=field):
                self.assertIn('本次无%s数据（%s；全程未记录）' % (label, field),
                              result['limitations'])
                self.assertTrue(all(p[field] is None for p in result['segments'][0]['points']))

    def test_each_optional_channel_partial_presence_keeps_point_gap(self):
        for field, label, tag in self.OPTIONAL_CHANNELS:
            with self.subTest(field=field):
                result = ride_gpx.parse_gpx(self.sensor_track([
                    self.optional_channel_point(0, field, tag, ['100']),
                    self.sensor_point(10, 'lat="0" lon="0"')]))
                self.assertEqual([p[field] for p in result['segments'][0]['points']], [100, None])
                self.assertEqual([w for w in result['warnings'] if w.get('field') == field], [
                    {'code': 'missing_value', 'segment_index': 0, 'point_index': 1, 'field': field}])
                self.assertNotIn('本次无%s数据（%s；全程未记录）' % (label, field),
                                 result['limitations'])

    def test_declared_bad_optional_channels_are_not_record_absence(self):
        for field, label, tag in self.OPTIONAL_CHANNELS:
            for values, code in ((['NaN'], 'invalid_value'),
                                 (['100', '110'], 'ambiguous_value'),
                                 ([''], 'missing_value' if field == 'ele' else 'invalid_value')):
                with self.subTest(field=field, values=values):
                    result = ride_gpx.parse_gpx(self.sensor_track([
                        self.optional_channel_point(0, field, tag, values),
                        self.sensor_point(10, 'lat="0" lon="0"')]))
                    self.assertIsNone(result['segments'][0]['points'][0][field])
                    self.assertIn({'code': code, 'segment_index': 0, 'point_index': 0,
                                   'field': field}, result['warnings'])
                    self.assertIn({'code': 'missing_value', 'segment_index': 0, 'point_index': 1,
                                   'field': field}, result['warnings'])
                    self.assertNotIn('本次无%s数据（%s；全程未记录）' % (label, field),
                                     result['limitations'])

    def test_optional_channel_presence_is_record_wide_across_segments(self):
        for field, label, tag in self.OPTIONAL_CHANNELS:
            with self.subTest(field=field):
                raw = self.sensor_track([
                    self.sensor_point(0, 'lat="0" lon="0"') + '</trkseg><trkseg>',
                    self.optional_channel_point(10, field, tag, ['100'])])
                result = ride_gpx.parse_gpx(raw)
                self.assertEqual(len(result['segments']), 2)
                self.assertEqual([w for w in result['warnings'] if w.get('field') == field], [
                    {'code': 'missing_value', 'segment_index': 0, 'point_index': 0, 'field': field}])
                self.assertNotIn('本次无%s数据（%s；全程未记录）' % (label, field),
                                 result['limitations'])

    def test_missing_time_always_warns_even_when_every_optional_channel_absent(self):
        for missing_indices in ((0, 1), (1,)):
            with self.subTest(missing_indices=missing_indices):
                points = [self.sensor_point(t, 'lat="0" lon="0"') for t in (0, 10)]
                for index in missing_indices:
                    timestamp = '<time>2026-11-06T00:00:%02dZ</time>' % (index * 10)
                    points[index] = points[index].replace(timestamp, '')
                result = ride_gpx.parse_gpx(self.sensor_track(points))
                self.assertEqual([w for w in result['warnings']
                                  if w['code'] == 'missing_value' and w.get('field') == 'time'], [
                    {'code': 'missing_value', 'segment_index': 0, 'point_index': index,
                     'field': 'time'} for index in missing_indices])
                for index in missing_indices:
                    self.assertIsNone(result['segments'][0]['points'][index]['time'])

if __name__ == "__main__":
    unittest.main()
