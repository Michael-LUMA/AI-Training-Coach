"""Offline synthetic demo and guarded endpoint tests; no real credentials."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from action_cards import ActionError
from coach_demo import DEMO_BANNER, demo_inputs, build_demo_review
from coach_icu import normalize_activity
import coach_server as server

class CoachDemoTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.get_key.side_effect = AssertionError('Demo must not read credentials')
        self.store.save_key.side_effect = AssertionError('Demo must not persist credentials')
        self.fetch = Mock(side_effect=AssertionError('Demo must be offline'))
        self.app = server.App(self.store, fetch=self.fetch)

    def request(self, payload=None, **overrides):
        body = json.dumps({} if payload is None else payload).encode()
        headers = {'Host':'127.0.0.1:8794', 'Origin':'http://127.0.0.1:8794',
                   'Content-Type':'application/json', 'Content-Length':str(len(body)),
                   'X-Coach-Token':self.app.token}
        headers.update(overrides)
        return self.app.dispatch('POST', '/api/coach/demo-review', headers, body)

    def test_fixture_two_hour_endurance_sensors_and_context(self):
        activity, streams, settings, wellness = demo_inputs()
        self.assertTrue(activity['synthetic'])
        self.assertTrue(activity['trainer'])
        record = normalize_activity(activity, streams, settings, wellness)
        self.assertEqual(record['metrics']['total_duration_s']['value'], 7200)
        self.assertEqual(record['coach_context']['ftp'], 250)
        self.assertTrue(150 <= record['metrics']['average_power_w']['value'] <= 200)
        self.assertTrue(120 <= record['metrics']['average_heart_rate_bpm']['value'] <= 145)
        data = {s['type']:s['data'] for s in streams}
        self.assertEqual(set(map(len, data.values())), {721})
        self.assertTrue(all(75 <= n <= 95 for n in data['cadence']))
        self.assertTrue(all(150 <= n <= 200 for n in data['watts']))
        self.assertFalse(record['has_position'])
        self.assertIsNone(record['metrics']['total_distance_m']['value'])
        self.assertEqual(wellness['sleep_secs'], 28800)

    def test_fixture_returns_fresh_objects(self):
        first = demo_inputs()
        first[0]['id'] = 'changed'
        first[1][0]['data'][0] = 999
        self.assertEqual(demo_inputs()[1][0]['data'][0], 0)
        self.assertNotEqual(demo_inputs()[0]['id'], 'changed')

    def test_endpoint_no_key_no_network_no_storage(self):
        result = self.request()
        self.assertTrue(result['synthetic'])
        self.assertFalse(result['verified'])
        self.assertEqual(result['demo_banner'], DEMO_BANNER)
        self.assertTrue(result['review']['synthetic'])
        self.assertEqual(result['review']['facts']['messages'][0], DEMO_BANNER)
        self.store.get_key.assert_not_called()
        self.store.save_key.assert_not_called()
        self.fetch.assert_not_called()

    def test_still_requires_token(self):
        for token in ('', 'wrong'):
            with self.subTest(token=token), self.assertRaises(ActionError) as caught:
                self.request(**{'X-Coach-Token':token})
            self.assertEqual(caught.exception.code, 'token_denied')

    def test_host_origin_and_json_guards(self):
        for override in ({'Host':'evil.invalid'}, {'Origin':'https://evil.invalid'},
                         {'Sec-Fetch-Site':'cross-site'}, {'Content-Type':'text/plain'},
                         {'Transfer-Encoding':'chunked'}):
            with self.subTest(override=override), self.assertRaises(ActionError):
                self.request(**override)

    def test_no_payload_customization_or_get(self):
        for payload in ({'api_key':'do-not-use'}, {'activity_id':'real'}, [], 'x'):
            with self.subTest(payload=payload), self.assertRaises(ActionError):
                self.request(payload)
        with self.assertRaises(ActionError):
            self.app.dispatch('GET','/api/coach/demo-review',
                {'Host':'127.0.0.1:8794','X-Coach-Token':self.app.token})

    def test_normal_english_review_discipline(self):
        review = build_demo_review()['review']
        self.assertIn('facts',review)
        self.assertIn('rule_inferences',review)
        self.assertIn('training_suggestion',review)
        self.assertIn('insufficient data', ' '.join(review['facts']['messages']).lower())
        self.assertIn('not a training prescription', review['training_suggestion']['disclaimer'])

    def test_ui_button_and_banner(self):
        page = (server.ROOT/'static/coach.html').read_text(encoding='utf-8')
        self.assertLess(page.index('id="demo"'),page.index('2. Choose an activity'))
        self.assertIn('Try a demo review',page)
        self.assertIn(DEMO_BANNER,page)
        self.assertIn("banner.className = 'demo-banner'",page)
        self.assertIn("api('/api/coach/demo-review', {})",page)
        self.assertIn('render(result.review, result.synthetic === true)',page)
        self.assertNotIn('innerHTML',page)

if __name__ == '__main__':
    unittest.main()
