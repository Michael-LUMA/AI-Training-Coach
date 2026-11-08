"""AMap weather proxy tests: synthetic envelopes, no network, temp storage."""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server as server
from action_cards import ActionError
from ride_weather import fetch_weather

KEY = 'synthetic-amap-key'
REGEO_OK = {"status": "1", "info": "OK",
            "regeocode": {"addressComponent": {"city": "上海市", "province": "上海市",
                                               "adcode": "310000"}}}
WEATHER_OK = {"status": "1", "info": "OK",
              "forecasts": [{"city": "上海市", "adcode": "310000",
                             "casts": [{"date": "2026-11-09", "dayweather": "晴",
                                        "nightweather": "多云", "daytemp": "18",
                                        "nighttemp": "10"}]}]}


def transport(regeo=REGEO_OK, weather=WEATHER_OK, status=200):
    def http_get(request, *, timeout):
        path = urlsplit(request.full_url).path
        payload = regeo if 'geocode' in path else weather
        if isinstance(payload, Exception):
            raise payload
        return status, {}, json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return http_get


class WeatherModuleTests(unittest.TestCase):
    def test_ok_returns_city_and_today_cast(self):
        result = fetch_weather(31.23, 121.47, api_key=KEY, http_get=transport())
        self.assertEqual(result, {"weather_status": "ok", "city": "上海市",
                                  "day_weather": "晴", "day_temp": 18,
                                  "night_weather": "多云", "night_temp": 10})

    def test_lng_lat_order_in_regeo_request(self):
        seen = []

        def http_get(request, *, timeout):
            seen.append(request.full_url)
            return 200, {}, json.dumps(REGEO_OK if 'geocode' in request.full_url
                                       else WEATHER_OK).encode('utf-8')
        fetch_weather(31.23, 121.47, api_key=KEY, http_get=http_get)
        query = parse_qs(urlsplit(seen[0]).query)
        self.assertEqual(query['location'], ['121.4700000,31.2300000'])

    def test_invalid_coordinates_rejected_without_network(self):
        def boom(request, *, timeout):
            raise AssertionError('network forbidden')
        for lat, lng in [(91.0, 0.0), (0.0, 181.0), (float('nan'), 0.0),
                         ('31.2', 0.0), (True, 0.0), (None, 0.0)]:
            with self.subTest(lat=lat, lng=lng):
                self.assertEqual(fetch_weather(lat, lng, api_key=KEY,
                                               http_get=boom)['weather_status'],
                                 'invalid_input')

    def test_not_configured_without_key(self):
        def boom(request, *, timeout):
            raise AssertionError('network forbidden')
        for key in (None, '', '   ', 123):
            self.assertEqual(fetch_weather(31.0, 121.0, api_key=key,
                                           http_get=boom)['weather_status'],
                             'not_configured')

    def test_key_rejected_and_quota_map_from_infocode(self):
        denied = dict(REGEO_OK, status='0', infocode='10001')
        quota = dict(REGEO_OK, status='0', infocode='10003')
        self.assertEqual(fetch_weather(31.0, 121.0, api_key=KEY,
                                       http_get=transport(regeo=denied))['weather_status'],
                         'key_rejected')
        self.assertEqual(fetch_weather(31.0, 121.0, api_key=KEY,
                                       http_get=transport(regeo=quota))['weather_status'],
                         'quota_exceeded')

    def test_upstream_failures_are_sanitized(self):
        cases = [transport(status=500),
                 transport(regeo={'status': '1', 'regeocode': {}}),
                 transport(regeo={'status': '1', 'regeocode': {'addressComponent': {'city': [], 'province': [], 'adcode': '31'}}}),
                 transport(weather={'status': '1', 'forecasts': []}),
                 transport(weather={'status': '1', 'forecasts': [{'casts': [{}]}]}),
                 transport(weather={'status': '1', 'forecasts': [{'casts': [
                     {'dayweather': '晴', 'nightweather': '晴',
                      'daytemp': 'abc', 'nighttemp': '10'}]}]})]
        for http_get in cases:
            with self.subTest(http_get=http_get):
                self.assertEqual(fetch_weather(31.0, 121.0, api_key=KEY,
                                               http_get=http_get)['weather_status'],
                                 'upstream_error')

    def test_empty_city_list_falls_back_to_province(self):
        regeo = {"status": "1", "regeocode": {"addressComponent":
                 {"city": [], "province": "上海市", "adcode": "310000"}}}
        result = fetch_weather(31.0, 121.0, api_key=KEY, http_get=transport(regeo=regeo))
        self.assertEqual(result['weather_status'], 'ok')
        self.assertEqual(result['city'], '上海市')

    def test_key_never_appears_in_results(self):
        for result in (fetch_weather(31.0, 121.0, api_key=KEY, http_get=transport()),
                       fetch_weather(91.0, 0.0, api_key=KEY, http_get=transport()),
                       fetch_weather(31.0, 121.0, api_key=KEY,
                                     http_get=transport(regeo=dict(REGEO_OK, status='0', infocode='10001')))):
            self.assertNotIn(KEY, json.dumps(result))


class WeatherServerTests(unittest.TestCase):
    def setUp(self):
        from coach_keystore import MasterKeyBackend
        master = 'synthetic-weather-test-master-key-32byte!'
        key_env = patch.dict('os.environ', {'LUMA_COACH_MASTER_KEY': master,
                                            'AMAP_API_KEY': KEY})
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
                              nav_get=transport(), today=lambda: date(2026, 11, 9))

    def post(self, payload):
        body = json.dumps(payload).encode()
        headers = {'Host': '127.0.0.1:8794', 'X-Coach-Token': self.app.token,
                   'Origin': 'http://127.0.0.1:8794', 'Content-Type': 'application/json',
                   'Content-Length': str(len(body))}
        return self.app.dispatch('POST', '/api/coach/ride/weather', headers, body)

    def error(self, code, call):
        with self.assertRaises(ActionError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_weather_endpoint_returns_today(self):
        result = self.post({'lat': 31.23, 'lng': 121.47})
        self.assertEqual(result['weather_status'], 'ok')
        self.assertEqual(result['city'], '上海市')
        self.assertEqual(result['day_temp'], 18)

    def test_rejects_bad_payload(self):
        self.error('invalid_input', lambda: self.post({'lat': 31.0}))
        self.error('invalid_input', lambda: self.post({'lat': 31.0, 'lng': 121.0, 'x': 1}))
        self.error('invalid_input', lambda: self.post({'lat': '31.0', 'lng': 121.0}))
        self.error('invalid_input', lambda: self.post({'lat': 91.0, 'lng': 0.0}))

    def test_key_stays_server_side(self):
        result = self.post({'lat': 31.23, 'lng': 121.47})
        self.assertNotIn(KEY, json.dumps(result))


if __name__ == '__main__':
    unittest.main()
