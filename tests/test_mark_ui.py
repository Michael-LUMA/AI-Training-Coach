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


class MarkPageServingTests(unittest.TestCase):
    def test_mark_route_served_by_server(self):
        source = (ROOT / 'coach_server.py').read_text(encoding='utf-8')
        self.assertIn('"/mark": "mark.html"', source)


if __name__ == '__main__':
    unittest.main()
