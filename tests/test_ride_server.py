"""Offline ride-service tests: temporary SQLite, fake requests, no bound sockets.

All fixture uploads are synthetic. ICS uses explicit version-bound authorization;
HTTP handler checks use memory streams and never bind real sockets.
"""
import base64
import concurrent.futures
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ride_server
from action_cards import ActionError, ActionStore

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
SCHEMA = ride_server.SCHEMA_VERSION


class RideServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luma-ride-server-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'ride-review' / 'rides.sqlite3'
        self.store = ride_server.RideStore(self.path)
        self.app = ride_server.App(self.store)

    def create_payload(self, kind='manual_summary', content=None):
        if content is None:
            if kind == 'manual_summary':
                content = {'text': '  合成骑行\r\n仅供测试  ', 'metrics': {'distance_m': 1000}}
            else:
                raw = (FIXTURES / ('ride_' + kind + '_normal.' + kind)).read_bytes()
                content = base64.b64encode(raw).decode('ascii')
        return {'schema_version': SCHEMA, 'kind': kind, 'content': content,
                'dataset_kind': 'synthetic', 'consent': True}

    def create(self, kind='manual_summary', content=None):
        return self.app.request('create', self.create_payload(kind, content))['review']

    def payload(self, card, **extra):
        return {'schema_version': SCHEMA, 'id': card['id'],
                'expected_revision': card['revision'], 'consent': True, **extra}

    def change(self, name, card, **extra):
        return self.app.request(name, self.payload(card, **extra))['review']

    def scheduled(self, kind='manual_summary'):
        card = self.create(kind)
        return self.change('edit', card, values={
            'scheduled_start': '2026-11-07T08:00:00+08:00', 'duration_s': 1800})

    def authorized(self):
        card = self.change('confirm', self.scheduled())
        return self.change('authorize', card, scope='generate_ics')

    def error(self, code, status, operation, *args, **kwargs):
        with self.assertRaises(ActionError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.status, status)
        return caught.exception

    def headers(self, body, **overrides):
        return {'Host': '127.0.0.1:8793', 'Origin': 'http://127.0.0.1:8793',
                'X-Luma-Token': self.app.token, 'Content-Type': 'application/json',
                'Content-Length': str(len(body)), **overrides}

    def fake_handler(self, method, path, body=b'', headers=None):
        # Capture the real handler class while replacing the socket constructor.
        with patch.object(ride_server, 'ThreadingHTTPServer') as constructor:
            ride_server.create_server(app=self.app)
        self.assertEqual(constructor.call_args.args[0], ('127.0.0.1', 8793))
        handler_type = constructor.call_args.args[1]
        handler = object.__new__(handler_type)
        handler.command = method
        handler.path = path
        handler.headers = headers if headers is not None else self.headers(body)
        handler.connection = Mock()
        handler.server = SimpleNamespace(server_port=8793)
        handler.rfile = Mock(wraps=io.BytesIO(body))
        handler.reply = Mock()
        handler.handle_request()
        return handler

    def test_gpx_and_tcx_upload_parse_into_unconfirmed_drafts(self):
        for kind in ('gpx', 'tcx'):
            with self.subTest(kind=kind):
                card = self.create(kind)
                self.assertEqual(card['source']['kind'], kind)
                self.assertEqual(card['source']['dataset_kind'], 'synthetic')
                self.assertTrue(card['source']['storage_consent'])
                self.assertEqual((card['status'], card['revision']), ('draft', 1))
                self.assertIsNone(card['confirmation'])
                self.assertIsNone(card['calendar_authorization'])
                self.assertEqual(card['receipts'], [])
                self.assertEqual(card['facts']['route_summary']['point_count'], 3)
                self.assertAlmostEqual(card['facts']['metrics']['distance_m']['value'], 222.390160467, places=6)
                self.assertEqual(card['facts']['metrics']['continuous_duration_s']['value'], 20)
                self.assertEqual(card['facts']['metrics']['elevation_gain_m']['value'], 5)
                self.assertEqual(card['facts']['metrics']['average_heart_rate_bpm']['value'], 120)
                self.assertEqual(self.store.get(card['id']), card)

    def test_manual_summary_uses_exact_original_text_not_file_facts(self):
        content = {'text': '  测试\r\n原样保存  ', 'metrics': {'distance_m': 1234}}
        card = self.create(content=content)
        raw = content['text'].encode('utf-8')
        self.assertEqual(card['source']['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(card['source']['hash_scope'], 'manual_text_utf8_exact')
        self.assertEqual(card['source']['byte_length'], len(raw))
        self.assertEqual(card['user_statements'][0]['text'], content['text'])
        self.assertIsNone(card['facts']['metrics']['distance_m']['value'])
        with self.store.connect() as db:
            row = db.execute('SELECT original,manual_json FROM rides WHERE id=?', (card['id'],)).fetchone()
        self.assertEqual(row['original'], raw)
        self.assertEqual(json.loads(row['manual_json']), content)

    def test_confirm_and_calendar_authorization_are_separate_versioned_actions(self):
        draft = self.scheduled()
        confirmed = self.change('confirm', draft)
        self.assertEqual(confirmed['status'], 'confirmed')
        self.assertEqual(confirmed['revision'], draft['revision'] + 1)
        self.assertEqual(confirmed['confirmation']['confirmed_revision'], confirmed['revision'])
        self.assertIsNone(confirmed['calendar_authorization'])
        self.error('calendar_not_authorized', 409, self.store.calendar, confirmed['id'], confirmed['revision'])
        authorized = self.change('authorize', confirmed, scope='generate_ics')
        auth = authorized['calendar_authorization']
        self.assertEqual(auth['state'], 'authorized')
        self.assertEqual(auth['scope'], 'generate_ics')
        self.assertEqual(auth['authorized_revision'], authorized['revision'])
        self.assertEqual(authorized['status'], 'confirmed')
        self.assertEqual(authorized['confirmation'], confirmed['confirmation'])
        self.assertEqual(authorized['receipts'], [])

    def test_ics_generation_consumes_authorization_persists_receipt_not_completion(self):
        card = self.authorized()
        result = self.app.request('calendar', self.payload(card))
        generated = result['review']
        self.assertEqual(generated['status'], 'confirmed')
        self.assertEqual(generated['revision'], card['revision'] + 1)
        self.assertEqual(generated['confirmation'], card['confirmation'])
        self.assertEqual(generated['calendar_authorization']['state'], 'consumed')
        self.assertEqual(generated['source'], card['source'])
        self.assertEqual(generated['facts'], card['facts'])
        self.assertTrue(result['receipt_persisted'])
        self.assertEqual(generated['receipts'][-1], result['receipt'])
        self.assertEqual((result['receipt']['action'], result['receipt']['outcome']), ('generate_ics', 'generated'))
        self.assertEqual((result['receipt']['input_revision'], result['receipt']['resulting_revision']),
                         (card['revision'], generated['revision']))
        self.assertEqual(result['artifact']['sha256'], hashlib.sha256(result['ics'].encode('utf-8')).hexdigest())
        self.assertEqual(result['receipt']['artifact'], result['artifact'])
        self.assertEqual(self.store.get(card['id']), generated)
        self.assertFalse(list(self.root.rglob('*.ics')))  # Streamed, no retained private artifact.
        self.error('revision_conflict', 409, self.app.request, 'calendar', self.payload(card))
        self.error('calendar_not_authorized', 409, self.app.request, 'calendar', self.payload(generated))

    def test_ics_utc_confirmed_duration_and_chinese_escaping_folding(self):
        text = '骑行训练，轻松安排;' + ('中文折行' * 24) + ',分段\\检查\n下一行'
        card = self.change('edit', self.scheduled('gpx'), values={'suggestion_text': text, 'duration_s': 1801})
        card = self.change('confirm', card)
        card = self.change('authorize', card, scope='generate_ics')
        with patch.object(ride_server, 'calendar_file', wraps=ride_server.calendar_file) as generator:
            result = self.app.request('calendar', self.payload(card))
        generator.assert_called_once()
        content = result['ics']
        unfolded = content.replace('\r\n ', '')
        self.assertIn('UID:ride-' + card['id'] + '@luma.local\r\n', unfolded)
        self.assertIn('DTSTART:20261107T000000Z\r\n', unfolded)
        self.assertIn('DTEND:20261107T003001Z\r\n', unfolded)
        self.assertIn('SEQUENCE:' + str(result['review']['revision']) + '\r\n', unfolded)
        self.assertRegex(unfolded, r'DTSTAMP:\d{8}T\d{6}Z\r\n')
        escaped = text.replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')
        self.assertIn('SUMMARY:' + escaped + '\r\n', unfolded)
        self.assertIn('DESCRIPTION:由用户确认', unfolded)
        self.assertNotIn('TZID=', content)
        self.assertIn('\r\n ', content)
        self.assertTrue(content.endswith('END:VCALENDAR\r\n'))
        self.assertNotIn('\n', content.replace('\r\n', ''))
        for line in content.split('\r\n'):
            self.assertLessEqual(len(line.encode('utf-8')), 75)
        self.assertIn('中文折行', unfolded)

    def test_ics_uid_stable_across_reauthorization_and_schedule_edits(self):
        first = self.app.request('calendar', self.payload(self.authorized()))
        second_card = self.change('authorize', first['review'], scope='generate_ics')
        second = self.app.request('calendar', self.payload(second_card))
        edited = self.change('edit', second['review'], values={
            'scheduled_start': '2026-11-08T10:00:00+08:00', 'duration_s': 900})
        self.error('calendar_not_authorized', 409, self.app.request, 'calendar', self.payload(edited))
        edited = self.change('confirm', edited)
        edited = self.change('authorize', edited, scope='generate_ics')
        third = self.app.request('calendar', self.payload(edited))
        uid = lambda r: next(line for line in r['ics'].split('\r\n') if line.startswith('UID:'))
        self.assertEqual(uid(first), uid(second))
        self.assertEqual(uid(first), uid(third))
        self.assertIn('DTSTART:20261108T020000Z', third['ics'])
        self.assertIn('DTEND:20261108T021500Z', third['ics'])
        self.assertNotEqual(first['receipt']['id'], second['receipt']['id'])
        self.assertEqual(len(third['review']['receipts']), 3)
        other = self.app.request('calendar', self.payload(self.authorized()))
        self.assertNotEqual(uid(first), uid(other))

    def test_ics_generation_failure_rolls_back_consent_receipt_and_version(self):
        card = self.authorized()
        with patch.object(ride_server, 'calendar_file', side_effect=ValueError('synthetic generator failure')):
            with self.assertRaisesRegex(ValueError, 'synthetic generator failure'):
                self.app.request('calendar', self.payload(card))
        self.assertEqual(self.store.get(card['id']), card)
        with patch.object(ride_server, 'calendar_file', return_value='BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n'):
            self.error('calendar_template_error', 500, self.app.request, 'calendar', self.payload(card))
        self.assertEqual(self.store.get(card['id']), card)
        self.assertEqual(self.app.request('calendar', self.payload(card))['review']['calendar_authorization']['state'], 'consumed')

    def test_two_clients_same_authorization_only_one_ics_generation_commits(self):
        card = self.authorized()
        def generate_once():
            try:
                return self.app.request('calendar', self.payload(card))['review']['revision']
            except ActionError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: generate_once(), range(2)))
        self.assertCountEqual(results, [card['revision'] + 1, 'revision_conflict'])
        current = self.store.get(card['id'])
        self.assertEqual(len(current['receipts']), 1)
        self.assertEqual(current['calendar_authorization']['state'], 'consumed')

    def test_http_upload_to_ics_download_headers_bytes_and_security(self):
        for kind in ('gpx', 'tcx'):
            with self.subTest(kind=kind):
                def post(name, payload):
                    raw = ride_server.dumps(payload).encode('utf-8')
                    handler = self.fake_handler('POST', '/api/rides/' + name, raw)
                    status, value = handler.reply.call_args.args
                    self.assertEqual(status, 200)
                    return handler, value
                _, result = post('create', self.create_payload(kind))
                card = result['review']
                source = card['source'].copy()
                _, result = post('edit', self.payload(card, values={
                    'scheduled_start': '2026-11-07T08:00:00+08:00', 'duration_s': 1800}))
                _, result = post('confirm', self.payload(result['review']))
                confirmed = result['review']
                self.error('calendar_not_authorized', 409, self.app.request, 'calendar', self.payload(confirmed))
                _, result = post('authorize', self.payload(confirmed, scope='generate_ics'))
                authorized = result['review']
                # Download still obeys token/origin/consent checks before spending authorization.
                raw = ride_server.dumps(self.payload(authorized)).encode('utf-8')
                for overrides in ({'X-Luma-Token': 'bad'}, {'Origin': 'https://evil.invalid'}):
                    rejected = self.fake_handler('POST', '/api/rides/calendar', raw, self.headers(raw, **overrides))
                    self.assertEqual(rejected.reply.call_args.args[0], 403)
                    rejected.rfile.read.assert_not_called()
                self.assertEqual(self.store.get(authorized['id']), authorized)
                self.error('consent_required', 403, self.app.request, 'calendar', self.payload(authorized, consent=False))
                handler, download = post('calendar', self.payload(authorized))
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = io.BytesIO()
                type(handler).reply(handler, 200, download)  # Exercise actual byte serialization, not a mock.
                headers = dict(call.args for call in handler.send_header.call_args_list)
                raw = handler.wfile.getvalue()
                handler.send_response.assert_called_once_with(200)
                self.assertEqual(headers['Content-Type'], 'text/calendar; charset=utf-8')
                self.assertEqual(int(headers['Content-Length']), len(raw))
                self.assertEqual(headers['Content-Disposition'], 'attachment; filename="' + download['filename'] + '"')
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(headers['X-Luma-Artifact-SHA256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(int(headers['X-Luma-Revision']), download['review']['revision'])
                self.assertEqual(headers['X-Luma-Receipt-Id'], download['receipt']['id'])
                self.assertEqual(raw, download['ics'].encode('utf-8'))
                self.assertEqual(download['review']['source'], source)
                with self.store.connect() as db:
                    original = db.execute('SELECT original FROM rides WHERE id=?', (card['id'],)).fetchone()['original']
                self.assertEqual(hashlib.sha256(original).hexdigest(), source['sha256'])

    def test_ics_after_completion_or_cancellation_is_denied_and_delete_warns(self):
        for state in ('done', 'cancelled'):
            with self.subTest(state=state):
                result = self.app.request('calendar', self.payload(self.authorized()))
                terminal = self.change('finish', result['review'], state=state)
                self.error('calendar_not_authorized', 409, self.app.request, 'calendar', self.payload(terminal))
                deleted = self.app.request('delete', self.payload(terminal))
                self.assertTrue(deleted['calendar_may_exist'])
                self.error('not_found', 404, self.app.request, 'calendar', self.payload(terminal))

    def test_draft_cannot_authorize_or_mark_done(self):
        card = self.scheduled()
        self.error('invalid_transition', 409, self.change, 'authorize', card, scope='generate_ics')
        self.error('invalid_transition', 409, self.change, 'finish', card, state='done')
        self.assertEqual(self.store.get(card['id']), card)

    def test_schedule_and_scope_required_for_independent_authorization(self):
        card = self.change('confirm', self.create())
        self.error('invalid_time', 400, self.change, 'authorize', card, scope='generate_ics')
        self.error('calendar_not_authorized', 409, self.change, 'authorize', card, scope='all')
        self.assertEqual(self.store.get(card['id']), card)

    def test_edit_revokes_confirmation_and_authorization_and_preserves_evidence(self):
        for values in ({'review_note': '用户补充'}, {'suggestion_text': '用户自己的安排'},
                       {'duration_s': 900}, {'scheduled_start': '2026-11-08T01:00:00+00:00'}):
            with self.subTest(values=values):
                card = self.authorized()
                edited = self.change('edit', card, values=values)
                self.assertEqual(edited['status'], 'draft')
                self.assertEqual(edited['revision'], card['revision'] + 1)
                self.assertIsNone(edited['confirmation'])
                self.assertIsNone(edited['calendar_authorization'])
                self.assertEqual(edited['source'], card['source'])
                self.assertEqual(edited['facts'], card['facts'])
                self.error('calendar_not_authorized', 409, self.store.calendar, edited['id'], edited['revision'])

    def test_feedback_is_only_marker_and_revokes_previous_revision_authorization(self):
        card = self.authorized()
        changed = self.change('feedback', card, value='useful')
        self.assertEqual(changed['feedback']['value'], 'useful')
        self.assertEqual(changed['feedback']['review_revision'], card['revision'])
        self.assertEqual(changed['calendar_authorization']['state'], 'revoked')
        self.assertEqual(changed['facts'], card['facts'])
        self.assertEqual(changed['training_suggestion'], card['training_suggestion'])
        self.error('calendar_not_authorized', 409, self.store.calendar, changed['id'], changed['revision'])
        changed = self.change('feedback', changed, value='not_useful')
        self.assertEqual(changed['feedback']['value'], 'not_useful')

    def test_user_done_and_terminal_state_cannot_be_edited_or_reconfirmed(self):
        card = self.authorized()
        done = self.change('finish', card, state='done')
        self.assertEqual(done['status'], 'done')
        receipt = done['receipts'][-1]
        self.assertEqual((receipt['action'], receipt['outcome']), ('mark_done', 'user_marked'))
        self.assertEqual((receipt['input_revision'], receipt['resulting_revision']),
                         (card['revision'], done['revision']))
        self.assertIsNone(receipt['artifact'])
        self.assertEqual(done['calendar_authorization']['state'], 'revoked')
        self.error('invalid_transition', 409, self.change, 'edit', done, values={'review_note': 'no'})
        self.error('invalid_transition', 409, self.change, 'confirm', done)
        self.error('invalid_transition', 409, self.change, 'finish', done, state='cancelled')
        self.assertEqual(self.store.get(done['id']), done)

    def test_cancel_allowed_for_draft_and_confirmed_but_not_reopened(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                card = self.create()
                if confirmed:
                    card = self.change('confirm', card)
                cancelled = self.change('finish', card, state='cancelled')
                self.assertEqual(cancelled['status'], 'cancelled')
                self.assertEqual(cancelled['receipts'][-1]['action'], 'cancel')
                self.assertEqual(cancelled['receipts'][-1]['outcome'], 'cancelled')
                self.error('invalid_transition', 409, self.change, 'confirm', cancelled)
                self.error('invalid_transition', 409, self.change, 'authorize', cancelled, scope='generate_ics')

    def test_stale_revision_rejected_across_all_current_version_operations(self):
        old = self.create()
        current = self.change('confirm', old)
        actions = (
            ('edit', {'values': {'review_note': 'stale'}}), ('confirm', {}),
            ('authorize', {'scope': 'generate_ics'}), ('calendar', {}),
            ('finish', {'state': 'done'}), ('feedback', {'value': 'useful'}),
            ('delete', {}), ('export', {}),
        )
        for name, extra in actions:
            with self.subTest(name=name):
                exc = self.error('revision_conflict', 409, self.app.request, name, self.payload(old, **extra))
                self.assertEqual(exc.current_revision, current['revision'])
                self.assertEqual(self.store.get(current['id']), current)

    def test_two_clients_same_revision_only_one_confirm_commits(self):
        card = self.create()
        def confirm_once():
            try:
                return self.store.confirm(card['id'], card['revision'])['revision']
            except ActionError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: confirm_once(), range(2)))
        self.assertCountEqual(results, [2, 'revision_conflict'])
        self.assertEqual(self.store.get(card['id'])['revision'], 2)

    def test_invalid_partial_edit_is_rolled_back_atomically(self):
        card = self.authorized()
        self.error('invalid_input', 400, self.change, 'edit', card,
                   values={'review_note': 'must not remain', 'duration_s': -1})
        self.assertEqual(self.store.get(card['id']), card)
        self.error('invalid_time', 400, self.change, 'edit', card,
                   values={'scheduled_start': '2026-11-07T08:00:00'})
        self.assertEqual(self.store.get(card['id']), card)

    def test_cannot_write_source_hash_identity_revision_facts_or_receipts(self):
        card = self.create('gpx')
        for key in ('source', 'sha256', 'id', 'revision', 'facts', 'receipts', 'confirmation', 'calendar_authorization'):
            with self.subTest(key=key):
                self.error('invalid_input', 400, self.change, 'edit', card, values={key: 'forged'})
                self.assertEqual(self.store.get(card['id']), card)

    def test_original_file_sha256_and_blob_unchanged_through_lifecycle(self):
        for kind in ('gpx', 'tcx'):
            with self.subTest(kind=kind):
                raw = (FIXTURES / ('ride_' + kind + '_normal.' + kind)).read_bytes()
                card = self.create(kind)
                source = dict(card['source'])
                self.assertEqual(source['sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(source['hash_scope'], 'raw_file_bytes')
                self.assertEqual(source['byte_length'], len(raw))
                card = self.change('edit', card, values={'review_note': '原文不变', 'duration_s': 600,
                                                        'scheduled_start': '2026-11-07T00:00:00+00:00'})
                card = self.change('confirm', card)
                card = self.change('authorize', card, scope='generate_ics')
                card = self.change('finish', card, state='done')
                self.assertEqual(card['source'], source)
                with self.store.connect() as db:
                    stored = db.execute('SELECT original FROM rides WHERE id=?', (card['id'],)).fetchone()[0]
                self.assertEqual(stored, raw)
                self.assertEqual(self.app.request('export', self.payload(card))['artifact']['sha256'],
                                 hashlib.sha256(self.app.request('export', self.payload(card))['content'].encode('utf-8')).hexdigest())

    def test_export_hashes_actual_snapshot_without_self_reference_or_version_write(self):
        card = self.create('gpx')
        exported = self.app.request('export', self.payload(card))
        self.assertEqual(json.loads(exported['content']), card)
        self.assertEqual(exported['artifact']['sha256'], hashlib.sha256(exported['content'].encode('utf-8')).hexdigest())
        self.assertEqual(exported['artifact']['media_type'], 'application/json')
        self.assertEqual(exported['receipt']['action'], 'export_json')
        self.assertEqual(exported['receipt']['outcome'], 'exported')
        self.assertEqual(exported['receipt']['input_revision'], exported['receipt']['resulting_revision'])
        self.assertFalse(exported['receipt_persisted'])
        self.assertEqual(self.store.get(card['id']), card)
        self.assertNotIn(exported['receipt']['id'], exported['content'])

    def test_delete_removes_managed_original_and_old_requests_do_not_resurrect(self):
        card = self.create('gpx')
        result = self.app.request('delete', self.payload(card))
        self.assertTrue(result['deleted'])
        self.assertEqual(result['id'], card['id'])
        self.assertEqual(result['deleted_revision'], card['revision'] + 1)
        self.assertTrue(result['deleted_at'].endswith('Z'))
        self.assertFalse(result['calendar_may_exist'])
        self.assertTrue(result['notice'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rides').fetchone()[0], 0)
        self.error('not_found', 404, self.store.get, card['id'])
        self.error('not_found', 404, self.change, 'confirm', card)
        self.error('not_found', 404, self.app.request, 'delete', self.payload(card))
        self.assertEqual(self.store.list(), [])

    def test_independent_sqlite_and_default_path_never_touch_action_store(self):
        action_path = self.root / 'action-pilot' / 'actions.sqlite3'
        action_path.parent.mkdir()
        # `with sqlite3.connect()` never closes the connection; on Windows an open
        # handle blocks tempdir cleanup, so close explicitly via closing().
        with closing(sqlite3.connect(action_path)) as db:
            with db:
                db.execute('CREATE TABLE actions (marker TEXT)')
                db.execute('INSERT INTO actions VALUES (?)', ('sentinel-do-not-change',))
        before = action_path.read_bytes()
        connect = sqlite3.connect
        with patch.object(ActionStore, 'connect', side_effect=AssertionError('action store forbidden')):
            with patch.object(ride_server.sqlite3, 'connect', wraps=connect) as opened:
                card = self.create()
                card = self.change('confirm', card)
                self.store.get(card['id'])
                self.app.request('export', self.payload(card))
                self.app.request('delete', self.payload(card))
            self.assertTrue(opened.call_args_list)
            self.assertTrue(all(Path(call.args[0]) == self.path for call in opened.call_args_list))
        self.assertEqual(action_path.read_bytes(), before)
        self.assertEqual(ride_server.RideStore().path, ride_server.DEFAULT_DB)
        self.assertEqual(ride_server.DEFAULT_DB, ride_server.ROOT / '.runtime/ride-review/rides.sqlite3')
        self.assertNotEqual(ride_server.DEFAULT_DB, ride_server.ROOT / '.runtime/action-pilot/actions.sqlite3')

    def test_missing_or_false_consent_cannot_create_or_mutate(self):
        for consent in (False, 1, 'true', None):
            payload = self.create_payload()
            payload['consent'] = consent
            self.error('consent_required', 403, self.app.request, 'create', payload)
        missing = self.create_payload()
        del missing['consent']
        self.error('invalid_input', 400, self.app.request, 'create', missing)
        self.assertFalse(self.path.exists())
        card = self.create()
        for name, extra in (('confirm', {}), ('authorize', {'scope': 'generate_ics'}),
                            ('edit', {'values': {'review_note': 'no'}}), ('calendar', {}),
                            ('finish', {'state': 'cancelled'}), ('feedback', {'value': 'useful'}),
                            ('export', {}), ('delete', {})):
            with self.subTest(name=name):
                self.error('consent_required', 403, self.app.request, name,
                           self.payload(card, consent=False, **extra))
        self.assertEqual(self.store.get(card['id']), card)

    def test_rejects_bad_schema_unknown_fields_ids_and_noninteger_revisions(self):
        payload = self.create_payload()
        payload['schema_version'] = 'unsupported'
        self.error('unsupported_schema', 400, self.app.request, 'create', payload)
        payload = self.create_payload()
        payload['revision'] = 999
        self.error('invalid_input', 400, self.app.request, 'create', payload)
        card = self.create()
        for revision in (True, 0, -1, '1'):
            self.error('invalid_input', 400, self.app.request, 'confirm', self.payload(card, expected_revision=revision))
        self.error('invalid_input', 400, self.store.get, '../actions.sqlite3')
        self.error('invalid_input', 400, self.app.request, 'create', self.create_payload('fit', 'AA=='))

    def test_malformed_and_unsafe_uploads_are_rejected_without_database_creation(self):
        for kind in ('gpx', 'tcx'):
            for suffix in ('external_entity', 'internal_entity'):
                raw = (FIXTURES / ('ride_' + kind + '_' + suffix + '.' + kind)).read_bytes()
                self.error('unsafe_xml', 400, self.app.request, 'create',
                           self.create_payload(kind, base64.b64encode(raw).decode('ascii')))
                self.assertFalse(self.path.exists())
        self.error('invalid_input', 400, self.app.request, 'create', self.create_payload('gpx', 'not base64!'))
        self.error('invalid_xml', 400, self.app.request, 'create', self.create_payload('gpx', base64.b64encode(b'<gpx').decode('ascii')))
        self.assertFalse(self.path.exists())

    def test_file_size_limit_rejects_before_parsing(self):
        with patch.object(ride_server, 'MAX_FILE_BYTES', 16):
            with patch.object(ride_server, 'parse_gpx') as parser:
                payload = self.create_payload('gpx', base64.b64encode(b'x' * 17).decode('ascii'))
                self.error('body_too_large', 413, self.app.request, 'create', payload)
                parser.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_list_and_status_do_not_create_storage_or_leak_tracks(self):
        self.assertEqual(self.app.request('list', {'schema_version': SCHEMA}), {'reviews': []})
        status = self.app.dispatch('GET', '/api/status', {'Host': '127.0.0.1:8793'})
        self.assertFalse(status['cloud_enabled'])
        self.assertTrue(status['ics_enabled'])
        self.assertEqual(status['token'], self.app.token)
        self.assertFalse(self.path.exists())
        card = self.create('gpx')
        self.assertEqual(self.store.list(), [{'id': card['id'], 'revision': card['revision']}])

    def test_fake_http_upload_get_and_conflict_have_real_status_codes(self):
        body = ride_server.dumps(self.create_payload('gpx')).encode('utf-8')
        handler = self.fake_handler('POST', '/api/rides/create', body)
        status, value = handler.reply.call_args.args
        self.assertEqual(status, 200)
        card = value['review']
        handler.rfile.read.assert_called_once_with(len(body))
        self.change('confirm', card)
        body = ride_server.dumps(self.payload(card)).encode('utf-8')
        handler = self.fake_handler('POST', '/api/rides/confirm', body)
        status, value = handler.reply.call_args.args
        self.assertEqual(status, 409)
        self.assertEqual(value['error_code'], 'revision_conflict')
        self.assertEqual(value['current_revision'], 2)
        self.assertFalse(value['retryable'])

    def test_host_origin_fetch_site_and_token_blocked_before_body_read(self):
        body = ride_server.dumps(self.create_payload()).encode('utf-8')
        for overrides, code in (
            ({'Host': 'evil.test'}, 'host_denied'), ({'Host': 'localhost:8793'}, 'host_denied'),
            ({'Origin': 'http://evil.test'}, 'origin_denied'), ({'Origin': None}, 'origin_denied'),
            ({'Sec-Fetch-Site': 'cross-site'}, 'origin_denied'), ({'X-Luma-Token': ''}, 'token_denied'),
            ({'X-Luma-Token': 'bad'}, 'token_denied'),
        ):
            with self.subTest(overrides=overrides):
                handler = self.fake_handler('POST', '/api/rides/create', body, self.headers(body, **overrides))
                status, value = handler.reply.call_args.args
                self.assertEqual((status, value['error_code']), (403, code))
                handler.rfile.read.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_http_body_type_lengths_and_transfer_encoding_rejected(self):
        body = b'{}'
        for overrides, status, code in (
            ({'Content-Type': 'text/plain'}, 415, 'invalid_body'),
            ({'Transfer-Encoding': 'chunked'}, 415, 'invalid_body'),
            ({'Content-Length': '0'}, 413, 'body_too_large'),
            ({'Content-Length': 'bad'}, 413, 'body_too_large'),
            ({'Content-Length': str(ride_server.MAX_BODY_BYTES + 1)}, 413, 'body_too_large'),
        ):
            with self.subTest(overrides=overrides):
                handler = self.fake_handler('POST', '/api/rides/create', body, self.headers(body, **overrides))
                actual_status, value = handler.reply.call_args.args
                self.assertEqual((actual_status, value['error_code']), (status, code))
                handler.rfile.read.assert_not_called()
        self.error('invalid_input', 400, self.app.dispatch, 'POST', '/api/rides/create',
                   self.headers(body, **{'Content-Length': '3'}), body)

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_bad_unicode(self):
        for body in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}',
                     b'{"a":"\\ud800"}', b'\xff', b'{'):
            with self.subTest(body=body):
                self.error('invalid_json', 400, self.app.dispatch, 'POST', '/api/rides/create', self.headers(body), body)
        self.assertFalse(self.path.exists())

    def test_utc_z_schedule_from_service_can_be_submitted_back(self):
        # Contract regression, deliberately not xfailed or version-skipped.
        # Python 3.10 fromisoformat does not itself accept Z. The service emits
        # Z, so feeding its own timestamp back must still be accepted.
        card = self.scheduled()
        timestamp = card['training_suggestion']['scheduled_start']
        self.assertEqual(timestamp, '2026-11-07T00:00:00Z')
        updated = self.change('edit', card, values={'scheduled_start': timestamp})
        self.assertEqual(updated['training_suggestion']['scheduled_start'], timestamp)
        self.assertEqual(updated['revision'], card['revision'] + 1)
    def test_loopback_factory_is_mocked_never_binds_or_serves(self):
        with patch.object(ride_server, 'ThreadingHTTPServer') as constructor:
            server = ride_server.create_server(app=self.app)
            self.assertEqual(constructor.call_args.args[0], ('127.0.0.1', 8793))
            self.assertIs(server, constructor.return_value)
            self.assertTrue(server.daemon_threads)
            server.serve_forever.assert_not_called()
        for port in ('8765', '8791'):
            with self.subTest(port=port), patch.object(sys, 'argv', ['ride_server.py', '--port', port]):
                with patch.object(ride_server, 'create_server') as create, patch('sys.stderr', new=io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        ride_server.main()
                    self.assertEqual(caught.exception.code, 2)
                    create.assert_not_called()

    def test_storage_failure_becomes_503_without_claiming_success(self):
        body = ride_server.dumps(self.create_payload()).encode('utf-8')
        with patch.object(self.store, 'create', side_effect=sqlite3.OperationalError('synthetic failure')):
            handler = self.fake_handler('POST', '/api/rides/create', body)
        status, value = handler.reply.call_args.args
        self.assertEqual((status, value['error_code']), (503, 'storage_unavailable'))
        self.assertFalse(value['retryable'])
        self.assertNotIn('synthetic failure', value['message'])
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()

