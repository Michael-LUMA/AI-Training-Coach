"""MARK agent-first page: static smoke checks (no network, no browser)."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'static' / 'mark.html').read_text(encoding='utf-8')


class MarkPageStructureTests(unittest.TestCase):
    def test_core_containers_exist(self):
        for element_id in ('stream-inner', 'chips-inner', 'input', 'send', 'mic',
                           'livestrip', 'status', 'status-text'):
            self.assertIn(f'id="{element_id}"', HTML)

    def test_no_external_assets(self):
        # 只允许同源相对路径：任何 http(s) 资源引用都是泄露面。
        externals = re.findall(r'(?:src|href)\s*=\s*"https?://', HTML)
        self.assertEqual(externals, [])

    def test_chat_contract_fields_present(self):
        # 聊天请求必须带时区与会话 id，与 coach_server 的校验契约一致。
        self.assertIn('tz_offset_min', HTML)
        self.assertIn('conv_id', HTML)
        self.assertIn('/api/coach/ride/chat', HTML)

    def test_proactive_live_and_voice_paths_present(self):
        for needle in ('/api/coach/ride/live', '/api/coach/ride/asr',
                       '/api/coach/ride/speak', '/api/coach/ride/weather',
                       '/api/coach/ride/route', '/api/coach/ride/sessions'):
            self.assertIn(needle, HTML)

    def test_thinking_state_rendered(self):
        self.assertIn('think-label', HTML)
        self.assertIn('addThinking', HTML)
        self.assertIn('removeThinking', HTML)

    def test_no_secret_literals(self):
        self.assertNotIn('sk-', HTML)
        self.assertNotIn('DASHSCOPE', HTML)
        self.assertNotIn('AMAP_API_KEY', HTML)

    def test_no_innerhtml_like_the_legacy_page(self):
        # 与 ride.html 同一纪律：动态文本只走 textContent。
        self.assertNotIn('innerHTML', HTML)

    def test_reliability_guards_are_present(self):
        # 这些保护直接对应已验证的卡顿/乱序风险：网络超时、语音队列、实时请求互斥、迟到响应丢弃。
        for needle in ('fetchWithTimeout', 'speechQueue', 'cancelSpeech',
                       'liveBusy', 'rideSeq', 'liveAbort', 'requestSeq!==rideSeq',
                       'lastFix=null', 'refreshChips();', 'const hour'):
            self.assertIn(needle, HTML)

    def test_voice_hmi_uses_capture_and_playback_events(self):
        for element_id in ('voice-stage', 'voice-main', 'voice-stop', 'voice-toggle', 'voice-label'):
            self.assertEqual(HTML.count(f'id="{element_id}"'), 1)
        for needle in ("voiceCapture='listening'", "voiceCapture='transcribing'",
                       "u.onstart=", "a.onplaying=", "voiceOutput='speaking'",
                       'Math.sqrt(power/pcm.length)', 'setTimeout(micStop,30000)',
                       "if(voiceAuto)speak(d.speech)", "$('voice-stop').onclick=",
                       "voiceReduced.matches", 'document.hidden'):
            self.assertIn(needle, HTML)

    def test_every_live_speech_has_priority_and_ride_tag(self):
        self.assertIn("speak(text,{tag:'ride',priority:urgent?100:40})", HTML)
        self.assertIn("speak(briefing,{tag:'ride',priority:35})", HTML)

    def test_voice_quality_and_record_closure_controls(self):
        for element_id in ('voice-choice', 'voice-preview', 'voice-source',
                           'ride-save-status', 'ride-save-retry', 'ride-save-export'):
            self.assertEqual(HTML.count(f'id="{element_id}"'), 1)
        for text in ('speechParts', 'browserSpeech', 'savePendingRide',
                     '/api/coach/ride/session', 'mark.pendingRide',
                     "source:'web_gps'", "avg_hr:null", 'recognitionText'):
            self.assertIn(text, HTML)
        self.assertLess(HTML.index("if(useCloud){"), HTML.index('await browserSpeech(part,generation)'))



class MarkPageServingTests(unittest.TestCase):
    def test_mark_allows_only_same_origin_fonts(self):
        from coach_server import _page
        _, csp = _page("mark.html")
        self.assertIn("font-src 'self'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("font-src", _page("coach.html")[1])

    def test_home_reuses_real_data_and_controls(self):
        for element_id in ('start-ride-primary', 'question-primary',
                           'dash-week-min', 'dash-week-count', 'dash-week-km',
                           'dash-weather', 'home-mark-preview'):
            self.assertEqual(HTML.count(f'id="{element_id}"'), 1)
        self.assertIn("$('home-mark-preview').textContent=String(text)", HTML)
        self.assertIn("prefers-reduced-motion: reduce", HTML)
        self.assertNotIn('.#livestrip', HTML)

    def test_mark_route_served_by_server(self):
        source = (ROOT / 'coach_server.py').read_text(encoding='utf-8')
        self.assertIn('"/mark": "mark.html"', source)


if __name__ == '__main__':
    unittest.main()
