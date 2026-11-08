"""llm_router: lane tiers, provider plan construction, bounded failover."""
from __future__ import annotations

import json
import unittest

import llm_router
from llm_review import ENDPOINT, build_llm_review
from ride_chat import build_ride_chat
from ride_live import build_live_speech

PRIMARY = 'sk-primary-key'
BACKUP = 'sk-backup-key'
BACKUP_ENV = {'LLM_BACKUP_BASE_URL': 'https://backup.example.com/v1/chat/completions',
              'LLM_BACKUP_API_KEY': BACKUP,
              'LLM_BACKUP_MODEL_FAST': 'backup-fast',
              'LLM_BACKUP_MODEL_SMART': 'backup-smart'}


def env(**extra):
    base = {'DASHSCOPE_API_KEY': PRIMARY}
    base.update(extra)
    return base


class TierTests(unittest.TestCase):
    def test_lane_tiers(self):
        self.assertEqual(llm_router.model_tier('live'), llm_router.FAST)
        self.assertEqual(llm_router.model_tier('live_answer'), llm_router.FAST)
        self.assertEqual(llm_router.model_tier('review'), llm_router.FAST)
        self.assertEqual(llm_router.model_tier('review_l2'), llm_router.FAST)
        self.assertEqual(llm_router.model_tier('weekly'), llm_router.FAST)
        self.assertEqual(llm_router.model_tier('personal'), llm_router.SMART)
        self.assertEqual(llm_router.model_tier('knowledge'), llm_router.SMART)

    def test_unknown_lane_rejected(self):
        with self.assertRaises(ValueError):
            llm_router.model_tier('nope')

    def test_small_talk_drops_personal_to_fast(self):
        for question in ('你好', '谢谢你', '早上好', '在吗', 'hello'):
            self.assertEqual(llm_router.model_tier('personal', question), llm_router.FAST, question)

    def test_real_questions_keep_smart(self):
        for question in ('这次平均心率多少', '你好，这次平均心率多少？',
                         '我这个月骑了多少公里', '最近状态怎么样', 'x' * 13 + '你好'):
            self.assertEqual(llm_router.model_tier('personal', question), llm_router.SMART, question)

    def test_small_talk_only_affects_personal(self):
        self.assertEqual(llm_router.model_tier('knowledge', '你好'), llm_router.SMART)


class PlanTests(unittest.TestCase):
    def test_primary_only(self):
        attempts = llm_router.plan('live', environ=env())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['provider'], 'dashscope')
        self.assertEqual(attempts[0]['endpoint'], ENDPOINT)
        self.assertEqual(attempts[0]['model'], 'qwen-flash')
        self.assertEqual(attempts[0]['api_key'], PRIMARY)

    def test_personal_uses_plus(self):
        attempts = llm_router.plan('personal', environ=env())
        self.assertEqual(attempts[0]['model'], 'qwen-plus')

    def test_small_talk_plan_uses_flash(self):
        attempts = llm_router.plan('personal', question='你好', environ=env())
        self.assertEqual(attempts[0]['model'], 'qwen-flash')

    def test_backup_appended_when_fully_configured(self):
        attempts = llm_router.plan('personal', environ=env(**BACKUP_ENV))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1]['provider'], 'backup')
        self.assertEqual(attempts[1]['endpoint'], BACKUP_ENV['LLM_BACKUP_BASE_URL'])
        self.assertEqual(attempts[1]['model'], 'backup-smart')
        self.assertEqual(attempts[1]['api_key'], BACKUP)

    def test_backup_fast_lane_model(self):
        attempts = llm_router.plan('live', environ=env(**BACKUP_ENV))
        self.assertEqual(attempts[1]['model'], 'backup-fast')

    def test_backup_skipped_when_incomplete(self):
        # Each lane needs only its own tier's backup model: dropping the other
        # tier's model must not affect this lane's plan.
        for drop in BACKUP_ENV:
            partial = {k: v for k, v in BACKUP_ENV.items() if k != drop}
            expected = 2 if drop == 'LLM_BACKUP_MODEL_SMART' else 1
            attempts = llm_router.plan('live', environ=env(**partial))
            self.assertEqual(len(attempts), expected, drop)
        partial = {k: v for k, v in BACKUP_ENV.items() if k != 'LLM_BACKUP_MODEL_SMART'}
        self.assertEqual(len(llm_router.plan('personal', environ=env(**partial))), 1)

    def test_backup_rejects_http_url(self):
        bad = dict(BACKUP_ENV, LLM_BACKUP_BASE_URL='http://backup.example.com/v1/chat/completions')
        self.assertEqual(len(llm_router.plan('live', environ=env(**bad))), 1)

    def test_backup_rejects_whitespace_model(self):
        bad = dict(BACKUP_ENV, LLM_BACKUP_MODEL_FAST='bad model')
        self.assertEqual(len(llm_router.plan('live', environ=env(**bad))), 1)

    def test_missing_primary_key_uses_backup_first(self):
        attempts = llm_router.plan('live', environ=dict(BACKUP_ENV))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['provider'], 'backup')

    def test_no_keys_empty_plan(self):
        self.assertEqual(llm_router.plan('live', environ={}), [])

    def test_blank_primary_key_skipped(self):
        self.assertEqual(llm_router.plan('live', environ={'DASHSCOPE_API_KEY': '  '}), [])


class RunTests(unittest.TestCase):
    def test_no_provider_calls_with_none_credentials(self):
        seen = []

        def call(**kw):
            seen.append(kw)
            return {'llm_status': 'not_configured', 'draft': None, 'usage': None, 'fallback': 'rule_draft'}

        result = llm_router.run('live', call, environ={})
        self.assertEqual(result['llm_status'], 'not_configured')
        self.assertEqual(seen, [{'endpoint': None, 'model': None, 'api_key': None}])

    def test_ok_stops_plan(self):
        seen = []

        def call(**kw):
            seen.append(kw['api_key'])
            return {'llm_status': 'ok'}

        llm_router.run('live', call, environ=env(**BACKUP_ENV))
        self.assertEqual(seen, [PRIMARY])

    def test_upstream_error_fails_over_to_backup(self):
        seen = []

        def call(**kw):
            seen.append(kw)
            status = 'upstream_error' if kw['api_key'] == PRIMARY else 'ok'
            return {'llm_status': status}

        result = llm_router.run('live', call, environ=env(**BACKUP_ENV))
        self.assertEqual(result['llm_status'], 'ok')
        self.assertEqual([s['api_key'] for s in seen], [PRIMARY, BACKUP])
        self.assertEqual(seen[1]['endpoint'], BACKUP_ENV['LLM_BACKUP_BASE_URL'])
        self.assertEqual(seen[1]['model'], 'backup-fast')

    def test_all_providers_down_returns_last_upstream_error(self):
        result = llm_router.run('live', lambda **kw: {'llm_status': 'upstream_error'},
                                environ=env(**BACKUP_ENV))
        self.assertEqual(result['llm_status'], 'upstream_error')

    def test_verification_rejection_never_fails_over(self):
        seen = []

        def call(**kw):
            seen.append(kw['api_key'])
            return {'llm_status': 'rejected_unverifiable_number'}

        result = llm_router.run('personal', call, environ=env(**BACKUP_ENV))
        self.assertEqual(result['llm_status'], 'rejected_unverifiable_number')
        self.assertEqual(seen, [PRIMARY])

    def test_bad_schema_rejection_never_fails_over(self):
        seen = []

        def call(**kw):
            seen.append(kw['api_key'])
            return {'llm_status': 'rejected_bad_schema'}

        llm_router.run('knowledge', call, environ=env(**BACKUP_ENV))
        self.assertEqual(seen, [PRIMARY])


def _ok_transport(captured):
    def post(request, *, timeout):
        captured['url'] = request.full_url
        captured['model'] = json.loads(request.data.decode('utf-8'))['model']
        captured['auth'] = request.headers.get('Authorization')
        body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}],
                           'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}).encode('utf-8')
        return 200, {}, body
    return post


class BuilderOverrideTests(unittest.TestCase):
    """Builders honor router-supplied endpoint/model instead of the constants."""

    def test_chat_uses_override(self):
        captured = {}
        build_ride_chat('这个我没记到。', api_key=PRIMARY, http_post=_ok_transport(captured),
                        endpoint='https://backup.example.com/v1/chat/completions',
                        model='backup-smart')
        self.assertEqual(captured['url'], 'https://backup.example.com/v1/chat/completions')
        self.assertEqual(captured['model'], 'backup-smart')
        self.assertEqual(captured['auth'], 'Bearer ' + PRIMARY)

    def test_chat_defaults_unchanged(self):
        captured = {}
        build_ride_chat('这个我没记到。', api_key=PRIMARY, http_post=_ok_transport(captured))
        self.assertEqual(captured['url'], ENDPOINT)
        self.assertEqual(captured['model'], 'qwen-plus')

    def test_live_speech_uses_override(self):
        captured = {}
        events = [{'type': 'distance_milestone', 'km': 5, 'distance_m': 5001.0,
                   'duration_s': 510.0, 'avg_speed_kmh': 35.3}]
        facts = {'distance_m': 5001.0, 'duration_s': 510.0,
                 'avg_speed_kmh': 35.3, 'paused': False}
        build_live_speech(events, facts, api_key=PRIMARY, http_post=_ok_transport(captured),
                          endpoint='https://backup.example.com/v1/chat/completions',
                          model='backup-fast')
        self.assertEqual(captured['model'], 'backup-fast')

    def test_review_l2_uses_override(self):
        captured = {}
        build_llm_review({'facts': {}}, api_key=PRIMARY, http_post=_ok_transport(captured),
                         endpoint='https://backup.example.com/v1/chat/completions',
                         model='backup-fast')
        self.assertEqual(captured['url'], 'https://backup.example.com/v1/chat/completions')
        self.assertEqual(captured['model'], 'backup-fast')


if __name__ == '__main__':
    unittest.main()
