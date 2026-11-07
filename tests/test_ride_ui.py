"""Offline memory-replay UI contracts and real JS execution in a fake browser.

Node is used only as a local JavaScript engine; no BLE, TTS, network, or cloud
service is invoked. Real mobile autoplay and sensor behaviour need device QA.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import coach_server

cached = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
NODE = shutil.which('node.exe') or (str(cached) if cached.is_file() else shutil.which('node'))

HARNESS = r"""
const fs=require('node:fs'), vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={}, calls=[], spoken=[], debug=[], storage=new Map(), clock={now:0};
for(const match of input.html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)){
  const tag=match[0];
  elements[match[1]]={textContent:'',value:'',hidden:/\shidden(?:\s|>)/.test(tag),disabled:/\sdisabled(?:\s|>)/.test(tag),
    classList:{add(){},remove(){}},listeners:{},
    addEventListener(name,fn){this.listeners[name]=fn;},
    click(){return this.listeners.click ? this.listeners.click() : undefined;}};
}
const options=input.options || {};
const sandbox={URLSearchParams,Date,DataView,Uint32Array,Math,Number,JSON,Promise,Error,
  location:{search:options.simulate ? '?simulate=1' : ''},performance:{now:()=>clock.now},
  crypto:require('node:crypto').webcrypto,navigator:{bluetooth:{}},
  document:{getElementById:id=>elements[id]},console:{debug:(...args)=>debug.push(args.join(' '))},
  localStorage:{setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k),removeItem:k=>storage.delete(k),key:i=>[...storage.keys()][i],get length(){return storage.size;}},
  setInterval:()=>1,clearInterval(){},setTimeout:()=>1,
  SpeechSynthesisUtterance:function(text){this.text=text;},
  _elements:elements,_calls:calls,_spoken:spoken,_debug:debug,_options:options,_clock:clock,_storage:storage};
sandbox.window={isSecureContext:true,SpeechSynthesisUtterance:sandbox.SpeechSynthesisUtterance,
  speechSynthesis:{cancel(){},speak:utterance=>spoken.push(utterance.text)},addEventListener(){}};
sandbox.fetch=async(url,init={})=>{
  calls.push({url,...init});
  if(url==='/api/coach/config'){
    if(options.configFail)throw new Error('config offline');
    return {ok:true,status:200,json:async()=>({token:'synthetic-ui-token'})};
  }
  if(url==='/api/coach/ride/sessions'){
    if(options.listFail)throw new Error('history offline');
    return {ok:true,status:200,json:async()=>({sessions:options.history || []})};
  }
  if(url==='/api/coach/ride/session'){
    if(options.uploadFail)throw new Error('upload offline');
    return {ok:true,status:200,json:async()=>({status:'saved',id:JSON.parse(init.body).id})};
  }
  throw new Error('Unexpected request: '+url);
};
const context=vm.createContext(sandbox);
const script=input.html.match(/<script>([\s\S]*?)<\/script>/)[1];
(async()=>{
  vm.runInContext(script,context,{timeout:3000});
  await new Promise(resolve=>setImmediate(resolve));
  const value=await vm.runInContext('(async()=>{'+input.scenario+'})()',context,{timeout:3000});
  process.stdout.write(JSON.stringify(value));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


def metadata(identifier='previous', date='2026-11-06T08:00:00Z', avg=130, duration=3600, source='ble_gatt_0x180d'):
    return {'id': identifier, 'started_at': date, 'summary': {'avg_hr': avg, 'duration_s': duration}, 'source': source}


class RideMemoryUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'static/ride.html').read_text(encoding='utf-8')

    def run_js(self, scenario, **options):
        if not NODE:
            self.skipTest('A local Node runtime is required for offline JS execution')
        result = subprocess.run([NODE, '-e', HARNESS], input=json.dumps({'html': self.html, 'scenario': scenario, 'options': options}),
                                capture_output=True, text=True, encoding='utf-8', timeout=15, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_page_strings_and_existing_csp_with_mock_store(self):
        for text in ('Mark 的记忆', '这不算趋势', '听 Mark 说', 'ride-persona/3', "name:'Mark'",
                     'recall:', 'recall_no_history:', 'compare_better:', 'compare_worse:',
                     'compare_flat:', 'no_compare:', '两次记录还说明不了趋势', 'Record only. Not medical advice.'):
            with self.subTest(text=text):
                self.assertIn(text, self.html)
        app = coach_server.App(store=Mock())
        body, csp = app.dispatch('GET', '/ride', {'Host': '127.0.0.1:8794'})
        self.assertEqual(body.decode('utf-8'), self.html)
        self.assertIsNone(app._ride_dir)
        self.assertIn("script-src 'sha256-", csp)
        self.assertNotIn('innerHTML', self.html)
        self.assertNotIn('<script src=', self.html)

    def test_initial_fetch_has_token_and_never_autoplays(self):
        result = self.run_js("return {calls:_calls,spoken:_spoken,hidden:$('memory-card').hidden,text:$('memory-recall').textContent};", history=[metadata()])
        self.assertEqual([call['url'] for call in result['calls']], ['/api/coach/config', '/api/coach/ride/sessions'])
        self.assertEqual(result['calls'][1]['headers']['X-Coach-Token'], 'synthetic-ui-token')
        self.assertEqual(result['spoken'], [])
        self.assertFalse(result['hidden'])
        for text in ('我记得', '11月6日', '60分钟', '130 bpm', '记录还很少，这不算趋势。'):
            self.assertIn(text, result['text'])

    def test_no_history_stays_hidden_and_debug_only(self):
        result = self.run_js("return {hidden:$('memory-card').hidden,spoken:_spoken,debug:_debug,text:$('memory-recall').textContent};")
        self.assertTrue(result['hidden'])
        self.assertEqual(result['spoken'], [])
        self.assertEqual(result['text'], '')
        self.assertEqual(result['debug'], ['我还没有可回放的历史记录。'])

    def test_recall_omits_missing_fields_and_keeps_zero(self):
        empty = metadata(avg=None, duration=None)
        result = self.run_js("return {text:$('memory-recall').textContent,zero:recallMemory({started_at:'2026-11-06T08:00:00Z',summary:{avg_hr:0,duration_s:0}})};", history=[empty])
        self.assertNotIn('分钟', result['text'])
        self.assertNotIn('平均心率', result['text'])
        self.assertNotIn('null', result['text'])
        self.assertIn('0分钟', result['zero'])
        self.assertIn('0 bpm', result['zero'])

    def test_latest_ride_selected_by_timestamp_not_upload_order(self):
        result = self.run_js("return $('memory-recall').textContent;", history=[metadata(date='2026-11-01T08:00:00Z'), metadata('latest', avg=145)])
        self.assertIn('145 bpm', result)
        self.assertIn('11月6日', result)

    def test_explicit_listen_and_first_start_are_user_gated(self):
        result = self.run_js("await $('start-ride').click(); return {spoken:_spoken,version:session.persona_version};", history=[metadata()], simulate=True)
        self.assertEqual(len(result['spoken']), 1)
        self.assertIn('我记得', result['spoken'][0])
        self.assertEqual(result['version'], 'ride-persona/3')
        result = self.run_js("$('memory-listen').click(); await $('start-ride').click(); return _spoken;", history=[metadata()], simulate=True)
        self.assertEqual(len(result), 2)
        self.assertIn('我记得', result[0])
        self.assertEqual(result[1], '开始记录。')

    def test_fetch_failures_never_invent_history_or_speech(self):
        for options in ({'listFail': True}, {'configFail': True}):
            with self.subTest(options=options):
                result = self.run_js("return {hidden:$('memory-card').hidden,spoken:_spoken,text:$('memory-recall').textContent};", **options)
                self.assertTrue(result['hidden'])
                self.assertEqual(result['spoken'], [])
                self.assertEqual(result['text'], '')

    def test_three_percent_inclusive_and_over_threshold_directions(self):
        result = self.run_js("""
          const p={summary:{avg_hr:100,duration_s:6000}};
          return {edge:compareMemories({summary:{avg_hr:103,duration_s:5820}},p),
                  outside:compareMemories({summary:{avg_hr:96,duration_s:6720}},p),
                  reverse:compareMemories({summary:{avg_hr:104,duration_s:5280}},p)};
        """)
        self.assertEqual(result['edge'].count('变化不超过3%'), 2)
        self.assertIn('低4 bpm', result['outside'])
        self.assertIn('多12分钟', result['outside'])
        self.assertIn('高4 bpm', result['reverse'])
        self.assertIn('少12分钟', result['reverse'])
        for text in result.values():
            self.assertIn('两次记录还说明不了趋势。', text)
            for forbidden in ('变强', '进步', '退步', '提升', '建议', '应该', '更健康'):
                self.assertNotIn(forbidden, text)

    def test_incomplete_summaries_and_no_previous_are_explicit(self):
        result = self.run_js("""
          const good={summary:{avg_hr:130,duration_s:3600}};
          return [compareMemories(good,{summary:{duration_s:3600}}),
                  compareMemories({summary:{avg_hr:null,duration_s:3600}},good),
                  compareMemories(good,null),compareMemories(good,{summary:{avg_hr:NaN,duration_s:3600}})];
        """)
        for index in (0, 1, 3):
            self.assertIn('上次记录字段不全', result[index])
        self.assertIn('我还没有更早的记录', result[2])

    def test_zero_baseline_does_not_divide_by_zero(self):
        result = self.run_js("return {same:compareMetric('样本平均心率',0,0,' bpm'),changed:compareMetric('样本平均心率',1,0,' bpm')};")
        self.assertIn('变化不超过3%', result['same'])
        self.assertIn('高1 bpm', result['changed'])
        self.assertNotIn('Infinity', json.dumps(result))

    def test_upload_success_fetches_previous_excluding_current_and_future(self):
        history = [metadata('future', '2026-11-08T08:00:00Z', 10), metadata('current', '2026-11-07T08:00:00Z', 126), metadata()]
        result = self.run_js("""
          const memory={id:'current',started_at:'2026-11-07T08:00:00Z',source:'ble_gatt_0x180d',summary:{avg_hr:126,duration_s:4320}};
          lastSession=memory;retain(memory);const saved=await upload(memory,true);
          return {saved,calls:_calls,text:$('memory-compare').textContent,hidden:$('memory-compare').hidden,local:_storage.size,spoken:_spoken};
        """, history=history)
        self.assertTrue(result['saved'])
        self.assertEqual([c['url'] for c in result['calls']][-2:], ['/api/coach/ride/session', '/api/coach/ride/sessions'])
        self.assertIn('低4 bpm', result['text'])
        self.assertIn('多12分钟', result['text'])
        self.assertFalse(result['hidden'])
        self.assertEqual(result['local'], 0)
        self.assertEqual(result['spoken'], [])

    def test_upload_failure_keeps_local_and_never_compares(self):
        result = self.run_js("""
          const memory={id:'pending'};lastSession=memory;retain(memory);const saved=await upload(memory,true);
          return {saved,calls:_calls,local:_storage.size,hidden:$('memory-compare').hidden,status:$('save-status').textContent};
        """, history=[metadata()], uploadFail=True)
        self.assertFalse(result['saved'])
        self.assertEqual(result['local'], 1)
        self.assertTrue(result['hidden'])
        self.assertEqual(sum(c['url']=='/api/coach/ride/sessions' for c in result['calls']), 1)
        self.assertIn('上报失败', result['status'])

    def test_comparison_fetch_failure_does_not_undo_successful_save(self):
        result = self.run_js("""
          const memory={id:'saved',started_at:'2026-11-07T08:00:00Z',summary:{avg_hr:130,duration_s:3600}};
          lastSession=memory;retain(memory);_options.listFail=true;const saved=await upload(memory,true);
          return {saved,local:_storage.size,status:$('save-status').textContent,text:$('memory-compare').textContent};
        """)
        self.assertTrue(result['saved'])
        self.assertEqual(result['local'], 0)
        self.assertIn('已上传', result['status'])
        self.assertIn('历史记录暂时无法读取', result['text'])

    def test_simulated_history_label_in_text_and_explicit_speech(self):
        result = self.run_js("""
          $('memory-listen').click();
          const memory={id:'current',started_at:'2026-11-07T08:00:00Z',source:'ble_gatt_0x180d',summary:{avg_hr:134,duration_s:4320}};
          lastSession=memory;await refreshComparison(memory);
          return {notice:$('memory-source').textContent,visible:!$('memory-source').hidden,spoken:_spoken,
                  compareNotice:$('compare-source').textContent,compareVisible:!$('compare-source').hidden,bar:$('simulated-banner').hidden};
        """, history=[metadata(source='simulated')])
        self.assertTrue(result['visible'])
        self.assertTrue(result['compareVisible'])
        self.assertIn('SIMULATED', result['notice'])
        self.assertIn('SIMULATED', result['spoken'][0])
        self.assertIn('SIMULATED', result['compareNotice'])
        self.assertTrue(result['bar'])  # Real current page does not pretend to be simulated.
        result = self.run_js("return $('simulated-banner').hidden;", simulate=True)
        self.assertFalse(result)

    def test_stale_upload_cannot_overwrite_new_ride_comparison(self):
        result = self.run_js("""
          lastSession={id:'new'};$('memory-compare').textContent='keep current';
          await refreshComparison({id:'old',started_at:'2026-11-07T08:00:00Z',summary:{avg_hr:130,duration_s:3600}});
          return $('memory-compare').textContent;
        """, history=[metadata()])
        self.assertEqual(result, 'keep current')


if __name__ == '__main__':
    unittest.main()
