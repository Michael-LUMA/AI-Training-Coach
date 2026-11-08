"""GPX/TCX import tests: synthetic file text only, no network, temp storage."""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server as server
import ride_import
from action_cards import ActionError
from ride_import import ImportBuildError, build_import_session

GPX = '''<?xml version="1.0"?>
<gpx version="1.1" creator="synthetic-test" xmlns="http://www.topografix.com/GPX/1/1"
 xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
 <trk><name>Morning ride</name><trkseg>
  <trkpt lat="31.2304" lon="121.4737"><ele>12.0</ele><time>2026-11-01T08:00:00Z</time>
   <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>120</gpxtpx:hr><gpxtpx:cad>80</gpxtpx:cad></gpxtpx:TrackPointExtension></extensions></trkpt>
  <trkpt lat="31.2314" lon="121.4747"><ele>15.0</ele><time>2026-11-01T08:00:10Z</time>
   <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>125</gpxtpx:hr><gpxtpx:cad>82</gpxtpx:cad></gpxtpx:TrackPointExtension></extensions></trkpt>
  <trkpt lat="31.2324" lon="121.4757"><ele>18.0</ele><time>2026-11-01T08:00:20Z</time>
   <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>130</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions></trkpt>
 </trkseg></trk></gpx>'''

TCX = '''<?xml version="1.0"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
 <Activities><Activity Sport="Biking">
  <Id>2026-11-02T07:00:00Z</Id>
  <Lap StartTime="2026-11-02T07:00:00Z"><Track>
   <Trackpoint><Time>2026-11-02T07:00:00Z</Time><Position><LatitudeDegrees>30.0</LatitudeDegrees><LongitudeDegrees>120.0</LongitudeDegrees></Position><AltitudeMeters>10.0</AltitudeMeters><HeartRateBpm><Value>110</Value></HeartRateBpm></Trackpoint>
   <Trackpoint><Time>2026-11-02T07:00:10Z</Time><Position><LatitudeDegrees>30.001</LatitudeDegrees><LongitudeDegrees>120.001</LongitudeDegrees></Position><AltitudeMeters>13.0</AltitudeMeters><HeartRateBpm><Value>118</Value></HeartRateBpm></Trackpoint>
   <Trackpoint><Time>2026-11-02T07:00:20Z</Time><Position><LatitudeDegrees>30.002</LatitudeDegrees><LongitudeDegrees>120.002</LongitudeDegrees></Position><AltitudeMeters>17.0</AltitudeMeters><HeartRateBpm><Value>122</Value></HeartRateBpm></Trackpoint>
  </Track></Lap>
 </Activity></Activities>
</TrainingCenterDatabase>'''

GPX_NO_HR = (GPX.replace('<gpxtpx:hr>120</gpxtpx:hr>', '')
             .replace('<gpxtpx:hr>125</gpxtpx:hr>', '')
             .replace('<gpxtpx:hr>130</gpxtpx:hr>', ''))


def rejects(code, text):
    with unittest.TestCase().assertRaises(ImportBuildError) as caught:
        build_import_session(text)
    unittest.TestCase().assertEqual(caught.exception.code, code)
    return caught.exception


class ImportBuildTests(unittest.TestCase):
    def test_gpx_builds_valid_v3_session(self):
        session = build_import_session(GPX)
        self.assertEqual(session['schema_version'], 'luma-ride-memory/3')
        self.assertEqual(session['source'], 'imported_gpx')
        self.assertTrue(session['id'].startswith('import-'))
        self.assertEqual(session['started_at'], '2026-11-01T08:00:00Z')
        self.assertEqual(session['ended_at'], '2026-11-01T08:00:20Z')
        summary = session['summary']
        self.assertEqual(summary['duration_s'], 20.0)
        self.assertEqual(summary['avg_hr'], 125.0)
        self.assertEqual(summary['max_hr'], 130)
        self.assertEqual(summary['avg_cadence'], 81.0)
        self.assertIsNone(summary['avg_power'])
        self.assertGreater(summary['distance_m'], 200.0)
        self.assertEqual(summary['ascent_m'], 6.0)
        self.assertEqual(summary['gaps'], 0)
        self.assertIsNone(summary['time_in_zone_s'])
        self.assertEqual(len(session['series']), 3)
        self.assertEqual(session['series'][0]['t'], 0.0)
        self.assertIn('lat', session['series'][0])
        self.assertIn('导入', session['coach_notes'][0])

    def test_tcx_builds_valid_v3_session(self):
        session = build_import_session(TCX)
        self.assertEqual(session['source'], 'imported_tcx')
        self.assertEqual(session['summary']['duration_s'], 20.0)
        self.assertAlmostEqual(session['summary']['avg_hr'], 350 / 3)
        self.assertEqual(session['summary']['max_hr'], 122)
        self.assertEqual(session['summary']['ascent_m'], 7.0)

    def test_deterministic_id_for_same_file(self):
        self.assertEqual(build_import_session(GPX)['id'], build_import_session(GPX)['id'])
        self.assertNotEqual(build_import_session(GPX)['id'], build_import_session(TCX)['id'])

    def test_rejects_unsupported_format(self):
        error = rejects('unsupported_format', '<x/>')
        self.assertNotIn('<x/>', str(error))

    def test_rejects_empty_and_wrong_type(self):
        rejects('invalid_input', '   ')
        with self.assertRaises(ImportBuildError):
            build_import_session(None)

    def test_rejects_without_heart_rate(self):
        rejects('no_heart_rate', GPX_NO_HR)

    def test_rejects_oversized_file(self):
        rejects('body_too_large', '<gpx' + ' ' * (ride_import.MAX_IMPORT_BYTES + 1))

    def test_rejects_implausible_speed(self):
        # Two points ~111 km apart within 10 seconds.
        fast = (GPX.replace('31.2314', '32.2304').replace('121.4747', '121.4737')
                .replace('31.2324', '31.2304').replace('121.4757', '121.4737'))
        rejects('implausible_speed', fast)

    def test_stride_decimation_keeps_contract(self):
        cap = patch.object(ride_import, 'MAX_SERIES', 2)
        cap.start()
        self.addCleanup(cap.stop)
        session = build_import_session(GPX)  # 3 HR points -> stride 2 -> 2 kept
        self.assertEqual(len(session['series']), 2)
        hrs = [p['hr'] for p in session['series']]
        self.assertEqual(session['summary']['avg_hr'], sum(hrs) / len(hrs))
        self.assertEqual(session['summary']['max_hr'], max(hrs))

    def test_rejection_never_echoes_file_content(self):
        error = rejects('no_heart_rate', GPX_NO_HR)
        self.assertNotIn('trkpt', str(error))
        self.assertNotIn('31.2304', str(error))


class ImportServerTests(unittest.TestCase):
    def setUp(self):
        from coach_keystore import MasterKeyBackend
        master = 'synthetic-import-test-master-key-32-bytes!'
        key_env = patch.dict('os.environ', {'LUMA_COACH_MASTER_KEY': master})
        key_env.start()
        self.addCleanup(key_env.stop)
        backend = patch('coach_keystore.DPAPIBackend',
                        side_effect=lambda: MasterKeyBackend(master))
        backend.start()
        self.addCleanup(backend.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'coach/coach.sqlite3'
        self.app = server.App(server.CoachStore(self.path),
                              fetch=lambda req, *, timeout: (_ for _ in ()).throw(AssertionError('no network')),
                              today=lambda: date(2026, 11, 9))

    def post(self, payload, path='/api/coach/ride/import'):
        body = json.dumps(payload).encode()
        headers = {'Host': '127.0.0.1:8794', 'X-Coach-Token': self.app.token,
                   'Origin': 'http://127.0.0.1:8794', 'Content-Type': 'application/json',
                   'Content-Length': str(len(body))}
        return self.app.dispatch('POST', path, headers, body)

    def error(self, code, call):
        with self.assertRaises(ActionError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_import_saves_session_to_ride_dir(self):
        result = self.post({'data': GPX})
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(result['source'], 'imported_gpx')
        stored = self.app.ride_dir / (result['id'] + '.json')
        self.assertTrue(stored.is_file())
        session = json.loads(stored.read_text(encoding='utf-8'))
        self.assertEqual(session['summary']['avg_hr'], 125.0)

    def test_reimport_is_idempotent_duplicate(self):
        first = self.post({'data': GPX})
        second = self.post({'data': GPX})
        self.assertEqual(first['status'], 'saved')
        self.assertEqual(second['status'], 'duplicate')
        self.assertEqual(first['id'], second['id'])

    def test_imported_session_appears_in_ride_list(self):
        result = self.post({'data': GPX})
        headers = {'Host': '127.0.0.1:8794', 'X-Coach-Token': self.app.token}
        listing = self.app.dispatch('GET', '/api/coach/ride/sessions?limit=5', headers)
        ids = [item['id'] for item in listing['sessions']]
        self.assertIn(result['id'], ids)

    def test_rejects_bad_payload_shape(self):
        self.error('invalid_input', lambda: self.post({'data': GPX, 'extra': 1}))
        self.error('invalid_input', lambda: self.post({'file': GPX}))
        self.error('invalid_input', lambda: self.post({'data': 123}))

    def test_rejects_unsupported_and_hr_less_files(self):
        self.error('unsupported_format', lambda: self.post({'data': '<x/>'}))
        self.error('no_heart_rate', lambda: self.post({'data': GPX_NO_HR}))

    def test_rejection_leaves_no_file(self):
        self.error('no_heart_rate', lambda: self.post({'data': GPX_NO_HR}))
        self.assertFalse(self.app.ride_dir.exists() and any(self.app.ride_dir.iterdir()))

    def test_error_never_echoes_file_content(self):
        error = self.error('no_heart_rate', lambda: self.post({'data': GPX_NO_HR}))
        self.assertNotIn('trkpt', str(error))

    def test_imported_session_is_reviewable(self):
        result = self.post({'data': GPX})
        review = self.post({'session_id': result['id']}, path='/api/coach/ride/review')
        # Same degradation as live rides: without an LLM key the status is
        # not_configured and speech stays None; with one, speech is non-empty.
        self.assertIn(review['speech_status'], ('fallback', 'ok', 'not_configured'))
        if review['speech'] is not None:
            self.assertIsInstance(review['speech'], str)
            self.assertTrue(review['speech'])

    def test_imported_session_is_chat_context(self):
        result = self.post({'data': GPX})
        answer = self.post({'session_id': result['id'], 'question': '这次骑了多久？'},
                           path='/api/coach/ride/chat')
        self.assertIsInstance(answer, dict)
        text = json.dumps(answer, ensure_ascii=False)
        self.assertNotIn('error_code', text)


if __name__ == '__main__':
    unittest.main()
