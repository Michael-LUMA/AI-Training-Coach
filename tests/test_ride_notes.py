"""Verbatim-note memory tests: temp storage, no network."""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach_server as server
import ride_notes
from action_cards import ActionError
from ride_notes import NoteError, add_note, load_notes, remove_note


class NoteModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'notes.json'

    def test_add_and_load_roundtrip_verbatim(self):
        entry = add_note(self.path, '  我每周二四骑车  ',
                         now=datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(entry['text'], '我每周二四骑车')  # trimmed, never reworded
        self.assertEqual(entry['created_at'], '2026-11-09T03:00:00Z')
        notes = load_notes(self.path)
        self.assertEqual([n['text'] for n in notes], ['我每周二四骑车'])
        self.assertEqual(notes[0]['id'], entry['id'])

    def test_empty_store_reads_as_empty_list(self):
        self.assertEqual(load_notes(self.path), [])

    def test_rejects_bad_text(self):
        for bad in ('', '   ', None, 123, 'x' * 141):
            with self.subTest(bad=repr(bad)[:20]):
                with self.assertRaises(NoteError) as caught:
                    add_note(self.path, bad)
                self.assertEqual(caught.exception.code, 'invalid_input')

    def test_control_characters_are_stripped_not_reworded(self):
        entry = add_note(self.path, '带\n换行\t和制表')
        self.assertEqual(entry['text'], '带换行和制表')

    def test_fifty_first_note_refused_not_dropped(self):
        for i in range(ride_notes.MAX_NOTES):
            add_note(self.path, f'笔记{i}')
        with self.assertRaises(NoteError) as caught:
            add_note(self.path, '第五十一条')
        self.assertEqual(caught.exception.code, 'note_limit')
        self.assertEqual(len(load_notes(self.path)), ride_notes.MAX_NOTES)

    def test_remove_by_id_and_unknown_id(self):
        first = add_note(self.path, '第一条')
        second = add_note(self.path, '第二条')
        remove_note(self.path, first['id'])
        self.assertEqual([n['id'] for n in load_notes(self.path)], [second['id']])
        with self.assertRaises(NoteError) as caught:
            remove_note(self.path, first['id'])
        self.assertEqual(caught.exception.code, 'not_found')
        with self.assertRaises(NoteError):
            remove_note(self.path, 'not-a-note-id')

    def test_corrupt_file_is_honest_error_not_repair(self):
        self.path.write_bytes(b'{broken json')
        with self.assertRaises(NoteError) as caught:
            load_notes(self.path)
        self.assertEqual(caught.exception.code, 'storage_corrupt')

    def test_atomic_write_leaves_no_temp_files(self):
        add_note(self.path, '原子写')
        leftovers = list(Path(self.temp.name).glob('.pending-*'))
        self.assertEqual(leftovers, [])


class NoteServerTests(unittest.TestCase):
    def setUp(self):
        from coach_keystore import MasterKeyBackend
        master = 'synthetic-notes-test-master-key-32-bytes'
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

    def call(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b''
        headers = {'Host': '127.0.0.1:8794', 'X-Coach-Token': self.app.token}
        if method == 'POST':
            headers.update({'Origin': 'http://127.0.0.1:8794', 'Content-Type': 'application/json',
                            'Content-Length': str(len(body))})
        return self.app.dispatch(method, path, headers, body)

    def error(self, code, call):
        with self.assertRaises(ActionError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_add_list_delete_roundtrip(self):
        added = self.call('POST', '/api/coach/ride/note', {'text': '我膝盖有旧伤'})
        self.assertEqual(added['status'], 'saved')
        listed = self.call('GET', '/api/coach/ride/notes')
        self.assertEqual([n['text'] for n in listed['notes']], ['我膝盖有旧伤'])
        deleted = self.call('POST', '/api/coach/ride/note/delete', {'id': added['id']})
        self.assertEqual(deleted['status'], 'deleted')
        self.assertEqual(self.call('GET', '/api/coach/ride/notes')['notes'], [])

    def test_rejects_bad_payloads(self):
        self.error('invalid_input', lambda: self.call('POST', '/api/coach/ride/note', {'text': ''}))
        self.error('invalid_input', lambda: self.call('POST', '/api/coach/ride/note', {'note': 'x'}))
        self.error('invalid_input', lambda: self.call('POST', '/api/coach/ride/note/delete', {'id': 'bogus'}))
        self.error('not_found', lambda: self.call('POST', '/api/coach/ride/note/delete', {'id': 'note-000000000000'}))

    def test_notes_endpoint_rejects_query_string(self):
        self.error('not_found', lambda: self.call('GET', '/api/coach/ride/notes?x=1'))

    def test_notes_reach_chat_facts(self):
        captured = {}

        def fake_post(request, *, timeout):
            captured['body'] = json.loads(request.data.decode('utf-8'))
            envelope = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
                {'speech': '你让我记住：膝盖有旧伤。'}, ensure_ascii=False)}}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 6}}
            return 200, {}, json.dumps(envelope, ensure_ascii=False).encode('utf-8')

        self.call('POST', '/api/coach/ride/note', {'text': '我膝盖有旧伤'})
        app = server.App(server.CoachStore(self.path),
                         fetch=lambda req, *, timeout: (_ for _ in ()).throw(AssertionError('no network')),
                         llm_post=fake_post, today=lambda: date(2026, 11, 9))
        body = json.dumps({'question': '我的膝盖怎么了？'}).encode()
        headers = {'Host': '127.0.0.1:8794', 'X-Coach-Token': app.token,
                   'Origin': 'http://127.0.0.1:8794', 'Content-Type': 'application/json',
                   'Content-Length': str(len(body))}
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'synthetic-dashscope-key'}):
            result = app.dispatch('POST', '/api/coach/ride/chat', headers, body)
        facts = json.loads(captured['body']['messages'][1]['content'])
        self.assertEqual(facts.get('user_notes'), ['我膝盖有旧伤'])
        self.assertEqual(result['speech'], '你让我记住：膝盖有旧伤。')

    def test_corrupt_notes_never_block_chat(self):
        self.app.ride_dir.mkdir(parents=True, exist_ok=True)
        self.app.notes_path.write_bytes(b'{broken')
        body = json.dumps({'question': '累计骑了多久？'}).encode()
        headers = {'Host': '127.0.0.1:8794', 'X-Coach-Token': self.app.token,
                   'Origin': 'http://127.0.0.1:8794', 'Content-Type': 'application/json',
                   'Content-Length': str(len(body))}
        result = self.app.dispatch('POST', '/api/coach/ride/chat', headers, body)
        self.assertIn('speech_status', result)


if __name__ == '__main__':
    unittest.main()
