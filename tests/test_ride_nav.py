"""Offline contracts for ride_nav: AMap geocode + bicycling proxy.

No network access; every response comes from an injected http_get double.
The provider key must never appear in any returned structure.
"""
import json
import sys
import unittest
from pathlib import Path
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ride_nav

KEY = 'synthetic-amap-key-0123456789abcdef'
GEOCODE_OK = {'status': '1', 'info': 'OK', 'count': '1', 'geocodes': [
    {'formatted_address': '北京市东城区天安门', 'location': '116.397477,39.908692'}]}
DIRECTION_OK = {'errcode': 0, 'errmsg': 'OK', 'data': {'paths': [
    {'distance': 5231, 'duration': 1800, 'steps': [
        {'instruction': '向东骑行200米右转', 'road': '长安街', 'distance': 200,
         'polyline': '116.390,39.908;116.392,39.908'},
        {'instruction': '向南骑行5000米到达终点', 'road': '广场东侧路', 'distance': 5031,
         'polyline': '116.392,39.908;116.392,39.900;116.397477,39.908692'}]}]}}


def fake_transport(responses):
    """Build an http_get double replaying queued (status, payload) responses."""
    calls = []

    def http_get(request, *, timeout):
        calls.append(request)
        status, payload = responses.pop(0)
        return status, {}, json.dumps(payload, ensure_ascii=False).encode('utf-8')

    http_get.calls = calls
    return http_get


class InputValidation(unittest.TestCase):
    def test_origin_format_and_range(self):
        for origin in ('', 'abc', '116.1', '116.1,', ',39.9', '116.1;39.9', '181,39', '-181,39',
                       '116,91', '116,-91', '1234,39', '0x10,39', '116.12345678,39'):
            with self.subTest(origin=origin):
                self.assertEqual(ride_nav.plan_route(origin, '天安门', api_key=KEY)['route_status'], 'invalid_input')
        for origin in ('116.397,39.908', '-73.9857,40.7484', '180,90', '-180,-90', '0,0'):
            with self.subTest(origin=origin):
                result = ride_nav.plan_route(origin, '天安门', api_key=None)
                self.assertEqual(result['route_status'], 'not_configured')

    def test_address_validation(self):
        for address in ('', '   ', None, 42, ['天安门'], 'x' * 101, 'bad\x00address', 'bad\naddress'):
            with self.subTest(address=address):
                self.assertEqual(ride_nav.plan_route('116.397,39.908', address, api_key=KEY)['route_status'], 'invalid_input')

    def test_key_states(self):
        for key in (None, '   ', ''):
            with self.subTest(key=key):
                self.assertEqual(ride_nav.plan_route('116.397,39.908', '天安门', api_key=key)['route_status'], 'not_configured')
        for key in ('含中文', 'has space', 42):
            with self.subTest(key=key):
                self.assertEqual(ride_nav.plan_route('116.397,39.908', '天安门', api_key=key)['route_status'], 'upstream_error')


class GeocodeMapping(unittest.TestCase):
    def plan(self, geocode):
        http_get = fake_transport([(200, geocode)])
        return ride_nav.plan_route('116.397,39.908', '天安门', api_key=KEY, http_get=http_get), http_get

    def test_address_not_found(self):
        result, _ = self.plan({'status': '1', 'info': 'OK', 'geocodes': []})
        self.assertEqual(result['route_status'], 'address_not_found')

    def test_key_and_quota_codes(self):
        for code, expected in (('10001', 'key_rejected'), ('10009', 'key_rejected'),
                               ('10003', 'quota_exceeded'), ('10044', 'quota_exceeded'),
                               ('10005', 'upstream_error')):
            with self.subTest(code=code):
                result, _ = self.plan({'status': '0', 'info': 'INVALID', 'infocode': code})
                self.assertEqual(result['route_status'], expected)

    def test_malformed_geocode_shapes(self):
        for payload in ({'status': '1'}, {'status': '1', 'geocodes': 'nope'},
                        {'status': '1', 'geocodes': [{'location': 'bad'}]},
                        {'status': '1', 'geocodes': [{'location': '116.1,39.9', 'formatted_address': 42}]},
                        {'status': '1', 'geocodes': [42]}, ['not-a-dict'], 'text', 42):
            with self.subTest(payload=payload):
                result, _ = self.plan(payload)
                self.assertEqual(result['route_status'], 'upstream_error')

    def test_geocode_request_shape(self):
        _, http_get = self.plan({'status': '0', 'infocode': '10001'})
        request = http_get.calls[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.get_method(), 'GET')
        self.assertTrue(request.full_url.startswith(ride_nav.GEOCODE_ENDPOINT + '?'))
        self.assertIn('address=%E5%A4%A9%E5%AE%89%E9%97%A8', request.full_url)
        self.assertIn('key=' + KEY, request.full_url)


class DirectionMapping(unittest.TestCase):
    def plan(self, direction):
        http_get = fake_transport([(200, GEOCODE_OK), (200, direction)])
        return ride_nav.plan_route('116.397,39.908', '天安门', api_key=KEY, http_get=http_get), http_get

    def test_ok_normalized(self):
        result, http_get = self.plan(DIRECTION_OK)
        self.assertEqual(result['route_status'], 'ok')
        self.assertEqual(result['distance_m'], 5231)
        self.assertEqual(result['duration_s'], 1800)
        self.assertEqual(result['destination'], '北京市东城区天安门')
        self.assertEqual(len(result['steps']), 2)
        first, last = result['steps']
        self.assertEqual(first['instruction'], '向东骑行200米右转')
        self.assertEqual(first['road'], '长安街')
        self.assertEqual(first['distance_m'], 200)
        self.assertEqual((first['end_lng'], first['end_lat']), (116.392, 39.908))
        self.assertEqual(first['path'], [[116.390, 39.908], [116.392, 39.908]])
        self.assertEqual((last['end_lng'], last['end_lat']), (116.397477, 39.908692))
        self.assertEqual(last['path'], [[116.392, 39.908], [116.392, 39.900], [116.397477, 39.908692]])
        self.assertEqual(last['path'][-1], [last['end_lng'], last['end_lat']])
        # The direction request uses the geocoded destination, never the raw address.
        direction_request = http_get.calls[1]
        self.assertTrue(direction_request.full_url.startswith(ride_nav.DIRECTION_ENDPOINT + '?'))
        self.assertIn('origin=116.397%2C39.908', direction_request.full_url)
        self.assertIn('destination=116.397477%2C39.908692', direction_request.full_url)
        self.assertNotIn('polyline', json.dumps(result))

    def test_no_route_and_codes(self):
        result, _ = self.plan({'errcode': 0, 'data': {'paths': []}})
        self.assertEqual(result['route_status'], 'no_route')
        for code, expected in ((10001, 'key_rejected'), (10003, 'quota_exceeded'), (20800, 'upstream_error')):
            with self.subTest(code=code):
                result, _ = self.plan({'errcode': code, 'errmsg': 'REDACTED', 'data': {}})
                self.assertEqual(result['route_status'], expected)

    def test_malformed_direction_shapes(self):
        for payload in ({'errcode': 0}, {'errcode': 0, 'data': {}}, {'errcode': 0, 'data': {'paths': 'x'}},
                        {'errcode': 0, 'data': {'paths': [42]}},
                        {'errcode': 0, 'data': {'paths': [{'distance': 'far', 'duration': 1, 'steps': []}]}},
                        {'errcode': 0, 'data': {'paths': [{'distance': 1, 'duration': 1, 'steps': []}]}},
                        {'errcode': 0, 'data': {'paths': [{'distance': 1, 'duration': 1, 'steps': [
                            {'instruction': '', 'road': '', 'distance': 1, 'polyline': '1,1'}]}]}},
                        {'errcode': 0, 'data': {'paths': [{'distance': 1, 'duration': 1, 'steps': [
                            {'instruction': '走', 'road': '', 'distance': 1, 'polyline': 'bad'}]}]}},
                        {'errcode': 0, 'data': {'paths': [{'distance': 1, 'duration': 1, 'steps': [
                            {'instruction': '走', 'road': '', 'distance': 1}]}]}}):
            with self.subTest(payload=payload):
                result, _ = self.plan(payload)
                self.assertEqual(result['route_status'], 'upstream_error', json.dumps(payload))

    def test_route_too_complex(self):
        steps = [{'instruction': '直走', 'road': '', 'distance': 10, 'polyline': '116.1,39.9;116.2,39.9'}
                 for _ in range(ride_nav.MAX_STEPS + 1)]
        result, _ = self.plan({'errcode': 0, 'data': {'paths': [{'distance': 10, 'duration': 10, 'steps': steps}]}})
        self.assertEqual(result['route_status'], 'route_too_complex')

    def test_point_caps(self):
        too_many = ';'.join(['116.1,39.9'] * (ride_nav.MAX_STEP_POINTS + 1))
        payload = {'errcode': 0, 'data': {'paths': [{'distance': 10, 'duration': 10, 'steps': [
            {'instruction': '直走', 'road': '', 'distance': 10, 'polyline': too_many}]}]}}
        result, _ = self.plan(payload)
        self.assertEqual(result['route_status'], 'upstream_error')  # per-step cap: malformed shape
        hundred = ';'.join(['116.1,39.9'] * 100)
        steps = [{'instruction': '直走', 'road': '', 'distance': 10, 'polyline': hundred} for _ in range(51)]
        result, _ = self.plan({'errcode': 0, 'data': {'paths': [{'distance': 10, 'duration': 10, 'steps': steps}]}})
        self.assertEqual(result['route_status'], 'route_too_complex')  # total cap 5000

    def test_single_point_step_is_allowed(self):
        payload = {'errcode': 0, 'data': {'paths': [{'distance': 5, 'duration': 3, 'steps': [
            {'instruction': '到达终点', 'road': '', 'distance': 5, 'polyline': '116.392,39.908'}]}]}}
        result, _ = self.plan(payload)
        self.assertEqual(result['route_status'], 'ok')
        self.assertEqual(result['steps'][0]['path'], [[116.392, 39.908]])

    def test_number_coercion_and_control_strip(self):
        payload = {'errcode': 0, 'data': {'paths': [{'distance': 5231.4, 'duration': 1800.2, 'steps': [
            {'instruction': '向东\x07骑行', 'road': None, 'distance': 200.6,
             'polyline': '116.390,39.908;116.392,39.908'}]}]}}
        result, _ = self.plan(payload)
        self.assertEqual(result['route_status'], 'ok')
        self.assertEqual(result['distance_m'], 5231)
        self.assertEqual(result['duration_s'], 1800)
        self.assertEqual(result['steps'][0]['instruction'], '向东骑行')
        self.assertEqual(result['steps'][0]['road'], '')
        self.assertEqual(result['steps'][0]['distance_m'], 201)


class TransportFailures(unittest.TestCase):
    def test_non_200_and_bad_bodies(self):
        for status, body in ((500, b'{}'), (200, b'not json'), (200, b'{"a":1,"a":2}'),
                             (200, b'{"x": NaN}'), (200, b'\xff\xfe')):
            with self.subTest(status=status, body=body):
                http_get = lambda request, *, timeout: (status, {}, body)
                result = ride_nav.plan_route('116.397,39.908', '天安门', api_key=KEY, http_get=http_get)
                self.assertEqual(result['route_status'], 'upstream_error')

    def test_oversized_response(self):
        body = b'{"status":"1","geocodes":[],"pad":"' + b'x' * ride_nav.MAX_RESPONSE_BYTES + b'"}'
        http_get = lambda request, *, timeout: (200, {}, body)
        self.assertEqual(ride_nav.plan_route('116.397,39.908', '天安门', api_key=KEY, http_get=http_get)['route_status'],
                         'upstream_error')

    def test_transport_exceptions(self):
        class Boom:
            def __init__(self, error):
                self.error = error

            def __call__(self, request, *, timeout):
                raise self.error

        for error in (TimeoutError('slow'), OSError('offline'), ValueError('too large')):
            with self.subTest(error=error):
                result = ride_nav.plan_route('116.397,39.908', '天安门', api_key=KEY, http_get=Boom(error))
                self.assertEqual(result['route_status'], 'upstream_error')

    def test_key_never_leaks(self):
        fixtures = [
            [(200, {'status': '0', 'info': 'INVALID_USER_KEY', 'infocode': '10001'})],
            [(500, {})],
            [(200, {'status': '1', 'geocodes': []})],
            [(200, GEOCODE_OK), (200, {'errcode': 10001, 'errmsg': 'key ' + KEY, 'data': {}})],
        ]
        for responses in fixtures:
            with self.subTest(responses=responses):
                http_get = fake_transport(list(responses))
                result = ride_nav.plan_route('116.397,39.908', '天安门', api_key=KEY, http_get=http_get)
                self.assertNotEqual(result['route_status'], 'ok')
                self.assertNotIn(KEY, json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
