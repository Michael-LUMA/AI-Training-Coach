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
const elements={}, calls=[], spoken=[], debug=[], storage=new Map(), clock={now:0}, intervals=new Map();
let nextTimer=0, bleCalls=0;
for(const match of input.html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)){
  const tag=match[0];
  elements[match[1]]={textContent:'',value:'',hidden:/\shidden(?:\s|>)/.test(tag),disabled:/\sdisabled(?:\s|>)/.test(tag),
    classList:{add(){},remove(){}},listeners:{},
    addEventListener(name,fn){this.listeners[name]=fn;},
    click(){return this.listeners.click ? this.listeners.click() : undefined;}};
}
const options=input.options || {};
const geo={watchId:0,watches:new Map()};
const geolocation={
  getCurrentPosition(ok,err){
    if(options.geoError){if(err)err({code:options.geoError,message:'geo error'});return;}
    const fix=options.geoFix || {lng:116.397,lat:39.908};
    ok({coords:{longitude:fix.lng,latitude:fix.lat,accuracy:10}});
  },
  watchPosition(ok){const id=++geo.watchId;geo.watches.set(id,ok);return id;},
  clearWatch(id){geo.watches.delete(id);}
};
const sandbox={URLSearchParams,Date,DataView,Uint32Array,Uint8Array,Uint16Array,Int16Array,ArrayBuffer,Math,Number,JSON,Promise,Error,
  location:{search:options.demo ? '?demo=1'+(options.simulate ? '&simulate=1' : '') : options.simulate ? '?simulate=1' : ''},performance:{now:()=>clock.now},
  crypto:require('node:crypto').webcrypto,navigator:{geolocation,bluetooth:{requestDevice:async()=>{bleCalls++;throw new Error('No real hardware in offline tests');}}},
  document:{getElementById:id=>elements[id],addEventListener(){},hidden:false},console:{debug:(...args)=>debug.push(args.join(' '))},
  localStorage:{setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k),removeItem:k=>storage.delete(k),key:i=>[...storage.keys()][i],get length(){return storage.size;}},
  setInterval:(fn,ms)=>{const id=++nextTimer;intervals.set(id,{fn,ms,next:clock.now+ms});return id;},clearInterval:id=>intervals.delete(id),setTimeout:()=>1,
  SpeechSynthesisUtterance:function(text){this.text=text;},
  _elements:elements,_calls:calls,_spoken:spoken,_debug:debug,_options:options,_clock:clock,_storage:storage,
  _geoEmit:(lng,lat)=>{for(const fn of geo.watches.values())fn({coords:{longitude:lng,latitude:lat,accuracy:5}});},
  _geoWatches:()=>geo.watches.size,
  _bleCalls:()=>bleCalls,_flush:()=>new Promise(resolve=>setImmediate(resolve)),
  _advance:async(ms)=>{const end=clock.now+ms;while(true){let nextId=null,next=null;for(const [id,item] of intervals){if(item.next<=end && (!next || item.next<next.next)){nextId=id;next=item;}}if(!next)break;clock.now=next.next;next.next+=next.ms;next.fn();await Promise.resolve();}clock.now=end;await new Promise(resolve=>setImmediate(resolve));}};
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
  // TEST_REVIEW_TRANSPORT
  /* optional ride review response in generic harness */
  if(url==='/api/coach/ride/review'){
    return {ok:true,status:200,json:async()=>options.brain || {speech_status:'not_configured',speech:null,fallback:'template',usage:null}};
  }
  if(url==='/api/coach/ride/route'){
    if(options.routeFail)return {ok:false,status:503,json:async()=>({error_code:'unavailable'})};
    return {ok:true,status:200,json:async()=>options.route || {route_status:'not_configured'}};
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
        for text in ('Mark 的记忆', '这不算趋势', '听 Mark 说', 'ride-persona/14', "name:'Mark'",
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
        self.assertEqual(result['version'], 'ride-persona/14')
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
        urls = [c['url'] for c in result['calls']]
        last_sessions = max(i for i, u in enumerate(urls) if u == '/api/coach/ride/sessions')
        self.assertLess(urls.index('/api/coach/ride/session'), last_sessions)
        self.assertIn('/api/coach/ride/review', urls)
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


    def test_demo_strings_source_notice_and_version(self):
        for text in ('demo_notice:', 'demo_start:', '看 Mark 演示', 'ride-persona/14',
                     '这是一段合成的 45 分钟骑行，90 秒放完', 'Mark 说的每句话都是真实逻辑，数据是假的。'):
            self.assertIn(text,self.html)
        result=self.run_js("return {notice:$('demo-notice').textContent,hidden:$('demo-notice').hidden,button:$('demo-start').hidden,bar:$('simulated-banner').hidden,startDisabled:$('start-ride').disabled,spoken:_spoken};",demo=True)
        self.assertFalse(result['hidden']);self.assertFalse(result['button']);self.assertFalse(result['bar'])
        self.assertFalse(result['startDisabled']);self.assertEqual(result['spoken'],[])

    def test_demo_full_playback_uses_real_pipeline_and_valid_memory(self):
        result=self.run_js("""
          let calls=0;const original=receiveHR;receiveHR=(...args)=>{calls++;return original(...args);};
          $('demo-start').click();const locked=$('demo-start').disabled && $('start-ride').disabled;
          await _advance(90000);
          const uploadCall=_calls.find(c=>c.url==='/api/coach/ride/session');
          return {calls,locked,active,ble:_bleCalls(),uploaded:JSON.parse(uploadCall.body),timeline:$('timeline').textContent,
                  summary:$('summary').textContent,elapsed:$('elapsed').textContent,bar:$('simulated-banner').hidden,
                  enabled:!$('demo-start').disabled && !$('start-ride').disabled,spoken:_spoken,
                  endpoints:_calls.map(c=>c.url),inputs:[$('lower').value,$('upper').value]};
        """,demo=True)
        from ride_memory import validate_session
        item=validate_session(result['uploaded'])
        self.assertEqual(item['source'],'simulated');self.assertEqual(item['persona_version'],'ride-persona/14')
        self.assertEqual(item['summary']['duration_s'],2700)
        self.assertEqual(item['series'][-1]['t'],2700)
        self.assertEqual(item['samples_hz'],1/30)
        self.assertEqual(result['calls'],91);self.assertEqual(len(item['series']),91)
        self.assertEqual(item['summary']['gaps'],0);self.assertEqual(item['summary']['alerts'],1)
        for index, expected in ((0, {'t':0,'hr':95}), (6, {'t':180,'hr':152}), (26, {'t':780,'hr':152}),
                                (32, {'t':960,'hr':120}), (-1, {'t':2700,'hr':124})):
            with self.subTest(index=index):
                self.assertEqual({key: item['series'][index][key] for key in ('t','hr')}, expected)
                self.assertTrue(60 <= item['series'][index]['cad'] <= 120)
                self.assertTrue(0 <= item['series'][index]['pwr'] <= 400)
        self.assertIsNotNone(item['summary']['avg_power'])
        self.assertIsNotNone(item['summary']['avg_cadence'])
        self.assertGreater(item['summary']['max_power'], 0)
        self.assertEqual(sum(item['summary']['time_in_zone_s'].values()),2700)
        self.assertTrue(result['locked']);self.assertTrue(result['enabled']);self.assertFalse(result['active'])
        self.assertEqual(result['ble'],0);self.assertFalse(result['bar']);self.assertEqual(result['inputs'],['',''])
        self.assertIn('2700.000s',result['timeline']);self.assertIn('模拟记录：2700秒',result['summary'])
        self.assertIn('/api/coach/ride/review',result['endpoints'])
        self.assertGreaterEqual(result['endpoints'].count('/api/coach/ride/sessions'),2)
        self.assertIn('心率超过你设的上限了。',result['spoken'])
        self.assertIn('平均功率', '。'.join(result['spoken']))  # milestone carries demo watts/rpm
        self.assertIn('样本平均功率', result['summary'])

    def test_demo_aux_channels_are_deterministic_and_reach_exports(self):
        scenario = """
          $('demo-start').click();await _advance(90000);
          const memory=JSON.parse(_calls.find(c=>c.url==='/api/coach/ride/session').body);
          return {series:memory.series,auxLive:$('aux-live').textContent,gpx:buildRideGpx(memory)};
        """
        a = self.run_js(scenario, demo=True)
        b = self.run_js(scenario, demo=True)
        self.assertEqual(a['series'], b['series'])  # no RNG anywhere in the demo script
        hr_points = [point for point in a['series'] if 'hr' in point]
        self.assertEqual(len(hr_points), 91)
        for point in hr_points:
            self.assertTrue(60 <= point['cad'] <= 120)
            self.assertTrue(0 <= point['pwr'] <= 400)
        self.assertIn('踏频', a['auxLive'])
        self.assertIn('功率', a['auxLive'])
        self.assertIn('<gpxtpx:cad>', a['gpx'])
        self.assertIn('<luma:pwr>', a['gpx'])

    def test_demo_dwell_and_cooldown_use_playback_seconds(self):
        result=self.run_js("""
          $('demo-start').click();await _advance(14000);
          const before=alerts;await _advance(3000);const after=alerts;
          await _advance(10000);return {before,after,end:alerts,elapsed:elapsedSeconds(),active,hr:$('heart-rate').textContent};
        """,demo=True)
        self.assertEqual(result['before'],0);self.assertEqual(result['after'],1);self.assertEqual(result['end'],1)
        self.assertEqual(result['elapsed'],810);self.assertTrue(result['active'])

    def test_demo_custom_target_and_script_are_deterministic(self):
        scenario="""
          $('lower').value='120';$('upper').value='150';$('demo-start').click();await _advance(90000);
          return {series:lastSession.series,target:lastSession.summary.zone_target,notice:$('demo-target').textContent};
        """
        a=self.run_js(scenario,demo=True);b=self.run_js(scenario,demo=True)
        self.assertEqual(a['series'],b['series']);self.assertEqual(a['target'],{'lower':120,'upper':150})
        self.assertEqual(a['series'][6]['hr'],162);self.assertEqual(a['series'][32]['hr'],135)
        self.assertIn('你填写的范围',a['notice'])

    def test_demo_early_stop_is_partial_not_fabricated_full_ride(self):
        result=self.run_js("""
          $('demo-start').click();await _advance(10000);$('end-ride').click();await _flush();
          const item=lastSession;await _advance(90000);
          return {item,unchanged:JSON.stringify(item)===JSON.stringify(lastSession),active,ble:_bleCalls()};
        """,demo=True)
        from ride_memory import validate_session
        item=validate_session(result['item'])
        self.assertEqual(item['summary']['duration_s'],300)
        self.assertEqual(item['series'][-1]['t'],300)
        self.assertEqual(item['source'],'simulated');self.assertTrue(result['unchanged']);self.assertFalse(result['active'])
        self.assertEqual(result['ble'],0)

    def test_demo_no_ble_and_real_ble_start_stay_mutually_exclusive(self):
        result=self.run_js("""
          $('demo-start').click();await $('start-ride').click();const during=_bleCalls();
          $('end-ride').click();await _flush();await $('start-ride').click();
          return {during,after:_bleCalls(),active};
        """,demo=True,simulate=True)
        self.assertEqual(result['during'],0);self.assertEqual(result['after'],1)
        self.assertFalse(result['active'])

    def test_real_ble_on_demo_page_never_uses_script_or_fake_thresholds(self):
        result=self.run_js("""
          const characteristic={addEventListener(){},removeEventListener(){},async startNotifications(){}};
          device={addEventListener(){},gatt:{connected:true,async connect(){return {async getPrimaryService(){return {async getCharacteristic(){return characteristic;}};}};},disconnect(){this.connected=false;}}};
          await $('start-ride').click();receiveHR(123);await _advance(1000);receiveHR(125);$('end-ride').click();await _flush();
          return {memory:lastSession,notice:$('simulated-banner').textContent,bar:$('simulated-banner').hidden};
        """,demo=True)
        from ride_memory import validate_session
        item=validate_session(result['memory'])
        self.assertEqual(item['source'],'ble_gatt_0x180d');self.assertEqual(item['samples_hz'],1)
        self.assertIsNone(item['summary']['zone_target']);self.assertEqual(item['summary']['duration_s'],1)
        self.assertEqual([p['hr'] for p in item['series']],[123,125])
        self.assertIn('真实 BLE',result['notice']);self.assertFalse(result['bar'])

    def test_demo_failed_upload_retains_simulated_memory(self):
        result=self.run_js("""
          $('demo-start').click();await _advance(90000);
          return {pending:[..._storage.entries()].filter(([k])=>k.startsWith('luma.ride.pending.')).map(([,v])=>JSON.parse(v)),status:$('save-status').textContent};
        """,demo=True,uploadFail=True)
        self.assertEqual(len(result['pending']),1);self.assertEqual(result['pending'][0]['source'],'simulated')
        self.assertEqual(result['pending'][0]['summary']['duration_s'],2700);self.assertIn('上报失败',result['status'])

    def test_demo_reload_recall_keeps_simulated_context(self):
        result=self.run_js("return {recall:$('memory-recall').textContent,notice:$('memory-source').textContent,hidden:$('memory-source').hidden};",demo=True,
                           history=[metadata(avg=130,duration=2700,source='simulated')])
        self.assertIn('45分钟',result['recall']);self.assertIn('SIMULATED',result['notice']);self.assertFalse(result['hidden'])

    def test_demo_cloud_speech_appends_without_autoplay(self):
        result=self.run_js("""
          $('demo-start').click();await _advance(90000);
          const before=_spoken.length;$('brain-listen').click();
          return {text:$('brain-speech').textContent,hidden:$('brain-card').hidden,before,after:_spoken.length,spoken:_spoken};
        """,demo=True,brain={'speech_status':'ok','speech':'模拟记录：我记到45分钟。','fallback':None})
        self.assertFalse(result['hidden']);self.assertIn('模拟记录：',result['text'])
        self.assertEqual(result['after'],result['before']+1)
        self.assertEqual(sum('模拟记录：我记到45分钟。' in text for text in result['spoken']),1)

    def test_demo_rejects_unplayable_thresholds_without_changing_real_inputs(self):
        for lower,upper in (('65535','65535'),('130.2','130.3'),('140','120')):
            result=self.run_js("$('lower').value=_options.lower;$('upper').value=_options.upper;$('demo-start').click();return {active,ble:_bleCalls(),disabled:$('demo-start').disabled,inputs:[$('lower').value,$('upper').value]};",demo=True,lower=lower,upper=upper)
            self.assertFalse(result['active']);self.assertEqual(result['ble'],0);self.assertFalse(result['disabled'])
            self.assertEqual(result['inputs'],[lower,upper])

    def test_s5_strings_and_manifest_link(self):
        for text in ("wakelock_unavailable:", "fuel:(minutes)=>", "fuel_again:", "ride-persona/14",
                     "分钟了，喝口水、补点吃的。", "又到补给的点了，别忘了喝。",
                     'rel="manifest"', "ride.webmanifest"):
            with self.subTest(text=text):
                self.assertIn(text, self.html)

    def test_manifest_and_icon_endpoints(self):
        from unittest.mock import Mock
        app = coach_server.App(store=Mock())
        body, csp, mime = app.dispatch('GET', '/ride.webmanifest', {'Host': '127.0.0.1:8794'})
        self.assertIn('manifest+json', mime)
        manifest = json.loads(body.decode('utf-8'))
        for key in ('name', 'short_name', 'start_url', 'display', 'icons'):
            self.assertIn(key, manifest)
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['start_url'], '/ride')
        body2, _, mime2 = app.dispatch('GET', '/icon.svg', {'Host': '127.0.0.1:8794'})
        self.assertEqual(mime2, 'image/svg+xml')
        self.assertIn(b'<svg', body2)

    def test_font_endpoints_public_and_traversal_rejected(self):
        from unittest.mock import Mock
        app = coach_server.App(store=Mock())
        for name in ('archivo-latin-700-normal.woff2', 'archivo-latin-800-normal.woff2',
                     'ibm-plex-mono-latin-400-normal.woff2', 'ibm-plex-mono-latin-600-normal.woff2'):
            with self.subTest(font=name):
                body, csp, mime = app.dispatch('GET', '/fonts/' + name, {'Host': '127.0.0.1:8794'})
                self.assertEqual(mime, 'font/woff2')
                self.assertTrue(body.startswith(b'wOF2'))
                self.assertIn("default-src 'none'", csp)
        for bad in ('/fonts/../coach_server.py', '/fonts/missing.woff2', '/fonts/ARCHIVO.woff2', '/fonts/a.eot'):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception):
                    app.dispatch('GET', bad, {'Host': '127.0.0.1:8794'})

    def test_fuel_speaks_once_at_45min_in_demo_and_lands_in_notes(self):
        result = self.run_js("""
          $('demo-start').click();
          await _advance(90000);
          const uploaded=JSON.parse(_calls.find(c=>c.url==='/api/coach/ride/session').body);
          return {spoken:_spoken, notes:uploaded.coach_notes};
        """, demo=True)
        fuel = [t for t in result['spoken'] if '喝口水' in t]
        self.assertEqual(fuel, ['骑了45分钟了，喝口水、补点吃的。'])
        self.assertTrue(any('喝口水' in n for n in result['notes']))

    def test_wakelock_absent_degrades_without_throwing(self):
        result = self.run_js("""
          $('start-ride').click();
          await _advance(3000);
          return {status:$('status').textContent, active};
        """, simulate=True)
        self.assertIn('请保持屏幕常亮', result['status'])
        self.assertTrue(result['active'])

    def test_s7_strings_milestone_gpx_and_ducking(self):
        for text in ("milestone:", "milestone_compare_flat:", "ride-persona/14",
                     "buildRideGpx", "下载 GPX 备份", "audioSession", "signal_gap",
                     "不能直接导入需要 GPS 轨迹的平台"):
            with self.subTest(text=text):
                self.assertIn(text, self.html)

    def test_milestone_speaks_at_30min_with_memory_comparison(self):
        result = self.run_js("""
          $('lower').value='100';$('upper').value='140';
          await $('start-ride').click();
          await _advance(1810000);
          return {spoken:_spoken, notes:session ? session.coach_notes : []};
        """, history=[metadata(avg=130)], simulate=True)
        milestones = [t for t in result['spoken'] if '骑了30分钟了' in t]
        self.assertEqual(len(milestones), 1)
        self.assertIn('在区间内。', milestones[0])
        self.assertIn('和上次差不多。', milestones[0])
        self.assertIn('平均心率', milestones[0])
        self.assertTrue(any('骑了30分钟了' in n for n in result['notes']))

    def test_milestone_defers_until_speech_gap_allows(self):
        result = self.run_js("""
          await $('start-ride').click();
          await _advance(1799000);
          window.speechSynthesis.speaking = true;
          await _advance(20000);
          const during=_spoken.filter(t=>t.includes('分钟了'));
          window.speechSynthesis.speaking = false;
          await _advance(30000);
          const after=_spoken.filter(t=>t.includes('分钟了'));
          return {during, after};
        """, simulate=True)
        self.assertEqual(result['during'], [])
        self.assertEqual(len(result['after']), 1)
        self.assertTrue(result['after'][0].startswith('骑了30分钟了。'))

    def test_speech_cadence_strings_and_defaults(self):
        for text in ('milestone-interval', 'fuel-interval', '每 15 分钟', '每 30 分钟（默认）',
                     '每 45 分钟（默认）', '关闭', 'MILESTONE_CHOICES', 'FUEL_CHOICES',
                     'luma.ride.prefs', 'saveSpeechPrefs', 'loadSpeechPrefs',
                     '越区和安全提醒不受影响'):
            with self.subTest(text=text):
                self.assertIn(text, self.html)
        result = self.run_js("return {m:$('milestone-interval').value,f:$('fuel-interval').value,prefs:_storage.get('luma.ride.prefs')||null};")
        self.assertEqual((result['m'], result['f']), ('1800', '2700'))
        self.assertIsNone(result['prefs'])  # nothing persisted before the rider chooses

    def test_custom_milestone_interval_15min(self):
        result = self.run_js("""
          $('milestone-interval').value='900';
          await $('start-ride').click();
          await _advance(910000);
          const first=_spoken.filter(t=>t.includes('分钟了'));
          await _advance(910000);
          const second=_spoken.filter(t=>t.includes('分钟了'));
          return {first, second, prefs:JSON.parse(_storage.get('luma.ride.prefs'))};
        """, simulate=True)
        self.assertTrue(any(t.startswith('骑了15分钟了。') for t in result['first']))
        self.assertTrue(any(t.startswith('骑了30分钟了。') for t in result['second']))
        self.assertEqual(result['prefs'], {'milestone_s': 900, 'fuel_s': 2700})

    def test_custom_fuel_interval_30min_then_repeats(self):
        result = self.run_js("""
          $('fuel-interval').value='1800';
          await $('start-ride').click();
          await _advance(1810000);
          const first=_spoken.filter(t=>t.includes('喝口水')||t.includes('别忘了喝'));
          await _advance(1810000);
          const second=_spoken.filter(t=>t.includes('喝口水')||t.includes('别忘了喝'));
          return {first, second};
        """, simulate=True)
        self.assertEqual(result['first'], ['骑了30分钟了，喝口水、补点吃的。'])
        self.assertEqual(result['second'], ['骑了30分钟了，喝口水、补点吃的。', '又到补给的点了，别忘了喝。'])

    def test_cadence_off_silences_only_milestone_and_fuel(self):
        result = self.run_js("""
          $('milestone-interval').value='0';$('fuel-interval').value='0';
          $('lower').value='100';$('upper').value='140';
          await $('start-ride').click();
          await _advance(3700000);
          return {spoken:_spoken, prefs:JSON.parse(_storage.get('luma.ride.prefs'))};
        """, simulate=True)
        self.assertFalse(any('分钟了' in t or '喝口水' in t or '别忘了喝' in t for t in result['spoken']))
        self.assertTrue(any('超过你设的上限' in t for t in result['spoken']))  # zone alerts unaffected
        self.assertEqual(result['prefs'], {'milestone_s': 0, 'fuel_s': 0})

    def test_prefs_load_whitelist_and_corrupt_fallback(self):
        result = self.run_js("""
          _storage.set('luma.ride.prefs',JSON.stringify({milestone_s:3600,fuel_s:1800}));
          loadSpeechPrefs();
          const loaded={m:$('milestone-interval').value,f:$('fuel-interval').value,ms:milestoneStepS,fs:fuelStepS};
          _storage.set('luma.ride.prefs',JSON.stringify({milestone_s:42,fuel_s:-7}));
          loadSpeechPrefs();
          const invalid={m:$('milestone-interval').value,f:$('fuel-interval').value,ms:milestoneStepS,fs:fuelStepS};
          _storage.set('luma.ride.prefs','{broken');
          loadSpeechPrefs();
          const corrupt={m:$('milestone-interval').value,ms:milestoneStepS};
          return {loaded, invalid, corrupt};
        """)
        self.assertEqual((result['loaded']['m'], result['loaded']['f']), ('3600', '1800'))
        self.assertEqual((result['loaded']['ms'], result['loaded']['fs']), (3600, 1800))
        self.assertEqual((result['invalid']['ms'], result['invalid']['fs']), (1800, 2700))  # whitelist fallback
        self.assertEqual((result['invalid']['m'], result['invalid']['f']), ('1800', '2700'))
        self.assertEqual((result['corrupt']['m'], result['corrupt']['ms']), ('1800', 1800))

    def test_cadence_selects_lock_during_ride(self):
        result = self.run_js("""
          await $('start-ride').click();
          const during=$('milestone-interval').disabled && $('fuel-interval').disabled;
          finishRide();
          const after=$('milestone-interval').disabled || $('fuel-interval').disabled;
          return {during, after};
        """, simulate=True)
        self.assertTrue(result['during'])
        self.assertFalse(result['after'])

    def test_build_ride_gpx_contract_and_wellformed_xml(self):
        result = self.run_js("""
          const memory={schema_version:'luma-ride-memory/1',id:'ride-x',started_at:'2026-11-07T08:00:00.000Z',source:'ble_gatt_0x180d',
            series:[{t:0,hr:120},{t:5,gap:true},{t:6,hr:121},{t:7,hr:122}]};
          const gpx=buildRideGpx(memory);
          let emptyErr='',badHr='',badOrder='';
          try{buildRideGpx({schema_version:'luma-ride-memory/1',started_at:memory.started_at,series:[{t:0,gap:true}]});}catch(e){emptyErr=e.message;}
          try{buildRideGpx({schema_version:'luma-ride-memory/1',started_at:memory.started_at,series:[{t:0,hr:12.5}]});}catch(e){badHr=e.message;}
          try{buildRideGpx({schema_version:'luma-ride-memory/1',started_at:memory.started_at,series:[{t:2,hr:120},{t:1,hr:121}]});}catch(e){badOrder=e.message;}
          return {gpx,emptyErr,badHr,badOrder};
        """)
        gpx = result['gpx']
        self.assertTrue(gpx.startswith('<?xml'))
        self.assertIn('<gpxtpx:hr>120</gpxtpx:hr>', gpx)
        self.assertIn('reason="signal_gap"', gpx)
        self.assertIn('time="2026-11-07T08:00:05.000Z"', gpx)
        self.assertEqual(gpx.count('<luma:segment>'), 2)
        self.assertIn('没有心率采样', result['emptyErr'])
        self.assertIn('心率值无效', result['badHr'])
        self.assertIn('未递增', result['badOrder'])
        import xml.etree.ElementTree as ET
        root = ET.fromstring(gpx)
        self.assertEqual(root.tag, '{http://www.topografix.com/GPX/1/1}gpx')

    def test_gpx_download_button_uses_finished_session(self):
        result = self.run_js("""
          let captured=null;
          Blob=function(parts){captured=parts[0];};
          URL={createObjectURL(){return 'blob:fake';},revokeObjectURL(){}};
          let link=null;
          document.createElement=()=>{link={click(){this.clicked=true;}};return link;};
          await $('start-ride').click();
          await _advance(5000);
          $('end-ride').click();await _flush();
          const disabled=$('download-gpx').disabled;
          $('download-gpx').click();
          return {disabled, gpx:captured, name:link ? link.download : '', status:$('save-status').textContent};
        """, simulate=True)
        self.assertFalse(result['disabled'])
        self.assertIn('<gpxtpx:hr>', result['gpx'])
        self.assertTrue(result['name'].endswith('.gpx'))
        self.assertNotIn('GPX 生成失败', result['status'])

    def test_ducking_toggles_audio_session_and_survives_denial(self):
        result = self.run_js("""
          navigator.audioSession={type:'auto'};
          $('memory-listen').click();
          const duringType=navigator.audioSession.type;
          navigator.audioSession={get type(){return 'auto';},set type(v){throw new Error('denied');}};
          let survived=true;
          try{speak('测试');}catch(e){survived=false;}
          return {duringType, survived};
        """, history=[metadata()])
        self.assertEqual(result['duringType'], 'transient')
        self.assertTrue(result['survived'])

    def test_fall_detection_strings_and_ui(self):
        for text in ('fall_alert:', 'fall_cancel:', 'fall_alarm:', 'ride-persona/14',
                     'crash-detect', 'fall-card', 'fall-ok', '跌倒检测（实验）',
                     '不是可靠的安全设备', 'devicemotion'):
            with self.subTest(text=text):
                self.assertIn(text, self.html)

    def test_fall_full_flow_alert_alarm_and_manual_cancel(self):
        result = self.run_js("""
          await $('start-ride').click();
          crashEnabled=true;
          crashMonitorSample(30, 1000);
          const watching=crashState;
          for(let i=1;i<=12;i++)crashMonitorSample(9.8, 1000+i*1000);
          const alertState=crashState, cardShown=!$('fall-card').hidden, countdown=$('fall-countdown').textContent;
          await _advance(31000);
          const alarm=crashState, alarmSpeech=_spoken.filter(t=>t.includes('警报')).length;
          $('fall-ok').click();
          return {watching, alertState, cardShown, countdown, alarm, alarmSpeech,
                  after:crashState, hidden:$('fall-card').hidden, notes:session.coach_notes};
        """, simulate=True)
        self.assertEqual(result['watching'], 'watch')
        self.assertEqual(result['alertState'], 'alert')
        self.assertTrue(result['cardShown'])
        self.assertEqual(result['countdown'], '30')
        self.assertEqual(result['alarm'], 'alarm')
        self.assertGreaterEqual(result['alarmSpeech'], 1)
        self.assertEqual(result['after'], 'idle')
        self.assertTrue(result['hidden'])
        notes = '\n'.join(result['notes'])
        for text in ('剧烈冲击', '确认倒计时', '警报状态', '确认没事'):
            self.assertIn(text, notes)

    def test_fall_movement_during_alert_cancels_with_kind_words(self):
        result = self.run_js("""
          await $('start-ride').click();
          crashEnabled=true;
          crashMonitorSample(30, 1000);
          for(let i=1;i<=11;i++)crashMonitorSample(9.8, 1000+i*1000);
          const alertState=crashState;
          crashMonitorSample(20, 12000);
          return {alertState, after:crashState, spoken:_spoken, hidden:$('fall-card').hidden};
        """, simulate=True)
        self.assertEqual(result['alertState'], 'alert')
        self.assertEqual(result['after'], 'idle')
        self.assertTrue(result['hidden'])
        self.assertIn('我好像检测到一次摔倒', result['spoken'][-2])
        self.assertEqual(result['spoken'][-1], '收到，你没事就好。')

    def test_fall_bump_with_continued_motion_never_alerts(self):
        result = self.run_js("""
          await $('start-ride').click();
          crashEnabled=true;
          crashMonitorSample(28, 1000);
          crashMonitorSample(20, 2000);
          await _advance(15000);
          return {state:crashState, hidden:$('fall-card').hidden,
                  alarmSpeech:_spoken.filter(t=>t.includes('警报')).length, notes:session.coach_notes};
        """, simulate=True)
        self.assertEqual(result['state'], 'idle')
        self.assertTrue(result['hidden'])
        self.assertEqual(result['alarmSpeech'], 0)
        self.assertTrue(any('颠簸' in n for n in result['notes']))

    def test_fall_disabled_by_default_and_requires_active_ride(self):
        result = self.run_js("""
          crashMonitorSample(30, 1000);
          const offState=crashState;
          await $('start-ride').click();
          crashMonitorSample(30, 2000);
          return {offState, rideState:crashState, cardHidden:$('fall-card').hidden, enabled:crashEnabled};
        """, simulate=True)
        self.assertEqual(result['offState'], 'idle')
        self.assertEqual(result['rideState'], 'idle')
        self.assertFalse(result['enabled'])
        self.assertTrue(result['cardHidden'])

    def test_s9_strings_aux_ui_and_version(self):
        for text in ('ride-persona/14', 'connect-aux', 'parseCadence', 'parsePower',
                     '0x1816', '0x2A5B', '0x1818', '0x2A63', 'milestone_power:', 'luma-ride-memory/2'):
            with self.subTest(text=text):
                self.assertIn(text, self.html)

    def test_parse_cadence_deltas_wraps_and_priming(self):
        result = self.run_js("""
          const dv=bytes=>new DataView(new Uint8Array(bytes).buffer);
          const u16=n=>[n&255,(n>>8)&255];
          const crank=(flags,revs,time)=>[flags,...u16(revs),...u16(time)];
          const first=parseCadence(dv(crank(0x02,10,1024)));
          const second=parseCadence(dv(crank(0x02,13,3072)));
          crankPrev={revs:65534,time:65024};
          const wrapped=parseCadence(dv(crank(0x02,1,1024)));
          crankPrev={revs:5,time:2048};
          const coasting=parseCadence(dv(crank(0x02,5,4096)));
          crankPrev=null;
          const wheelAndCrank=[0x03,1,0,0,0,0,4,...u16(20),...u16(2048)];
          const primed=parseCadence(dv(wheelAndCrank));
          const after=parseCadence(dv([0x03,1,0,0,0,0,4,...u16(22),...u16(4096)]));
          const noCrank=parseCadence(dv([0x01,1,0,0,0,0,4]));
          return {first,second,wrapped,coasting,primed,after,noCrank};
        """)
        self.assertIsNone(result['first'])   # first notification primes, invents nothing
        self.assertEqual(result['second'], 90)  # 3 revs / 2 s
        self.assertEqual(result['wrapped'], 120)  # uint16 wraparound on both counters
        self.assertIsNone(result['coasting'])  # zero revs: keep last known, not zero rpm
        self.assertIsNone(result['primed'])
        self.assertEqual(result['after'], 60)  # 2 revs / 2 s after 6-byte wheel data skipped
        self.assertIsNone(result['noCrank'])

    def test_parse_power_instantaneous_and_negative_artifacts(self):
        result = self.run_js("""
          const dv=bytes=>new DataView(new Uint8Array(bytes).buffer);
          return {normal:parsePower(dv([0,0,0xD2,0])),
                  zero:parsePower(dv([0,0,0,0])),
                  negative:parsePower(dv([0,0,0xFF,0xFF]))};
        """)
        self.assertEqual(result['normal'], 210)
        self.assertEqual(result['zero'], 0)
        self.assertIsNone(result['negative'])

    def test_aux_attach_only_when_fresh_and_summary_v2(self):
        result = self.run_js("""
          await $('start-ride').click();
          latestCad={value:90,mono:performance.now()};
          latestPwr={value:210,mono:performance.now()};
          receiveHR(120);
          const attached=session.series[session.series.length-1];
          await _advance(6000);
          receiveHR(122);  // aux readings now stale: must NOT attach
          const stale=session.series[session.series.length-1];
          $('end-ride').click();await _flush();
          return {attached,stale,summary:lastSession.summary,
                  version:lastSession.schema_version};
        """, simulate=True)
        self.assertEqual(result['attached'].get('cad'), 90)
        self.assertEqual(result['attached'].get('pwr'), 210)
        self.assertNotIn('cad', result['stale'])
        self.assertNotIn('pwr', result['stale'])
        self.assertEqual(result['version'], 'luma-ride-memory/2')
        self.assertEqual(result['summary']['avg_cadence'], 90)
        self.assertEqual(result['summary']['avg_power'], 210)
        self.assertEqual(result['summary']['max_power'], 210)
        from ride_memory import validate_session
        check = {'schema_version': 'luma-ride-memory/2', 'id': 'ride-check',
                 'started_at': '2026-11-07T08:00:00Z', 'ended_at': '2026-11-07T08:00:06Z',
                 'samples_hz': 1,
                 'series': [{'t': 0, 'hr': 120, 'cad': 90, 'pwr': 210}, {'t': 6, 'hr': 122}],
                 'summary': {'duration_s': 6, 'avg_hr': 121, 'max_hr': 122, 'time_in_zone_s': None,
                             'zone_target': None, 'gaps': 0, 'alerts': 0,
                             'avg_cadence': 90, 'avg_power': 210, 'max_power': 210},
                 'coach_notes': ['x'], 'persona_version': 'ride-persona/14', 'source': 'simulated'}
        self.assertEqual(validate_session(check)['summary']['avg_power'], 210)

    def test_milestone_mentions_power_and_cadence_when_present(self):
        result = self.run_js("""
          await $('start-ride').click();
          session.series.push({t:0,hr:120,pwr:200,cad:88},{t:10,hr:130,pwr:220});
          return buildMilestoneText(30);
        """, simulate=True)
        self.assertIn('骑了30分钟了。', result)
        self.assertIn('平均功率210瓦。', result)
        self.assertIn('平均踏频88转。', result)

    def test_gpx_export_includes_cadence_and_power(self):
        result = self.run_js("""
          const memory={schema_version:'luma-ride-memory/2',id:'ride-y',started_at:'2026-11-07T08:00:00.000Z',source:'ble_gatt_0x180d',
            series:[{t:0,hr:120,cad:88,pwr:190},{t:1,hr:121,pwr:240}]};
          return buildRideGpx(memory);
        """)
        self.assertIn('<gpxtpx:cad>88</gpxtpx:cad>', result)
        self.assertIn('<luma:pwr>240</luma:pwr>', result)
        import xml.etree.ElementTree as ET
        ET.fromstring(result)

    def test_connect_aux_flow_with_fake_power_meter(self):
        result = self.run_js("""
          const cadChar={addEventListener(){},removeEventListener(){},async startNotifications(){}};
          const pwrChar={addEventListener(){},removeEventListener(){},async startNotifications(){}};
          auxDevice={addEventListener(){},gatt:{connected:true,
            async connect(){return {async getPrimaryService(uuid){
              if(uuid===0x1816)return {async getCharacteristic(){return cadChar;}};
              if(uuid===0x1818)return {async getCharacteristic(){return pwrChar;}};
              throw new Error('no such service');}};},
            disconnect(){this.connected=false;}}};
          await connectAux();
          return {cad:!!auxCadChar,pwr:!!auxPwrChar,status:$('status').textContent,
                  disabled:$('connect-aux').disabled};
        """)
        self.assertTrue(result['cad'])
        self.assertTrue(result['pwr'])
        self.assertIn('已连接功率/踏频', result['status'])
        self.assertTrue(result['disabled'])  # stays disabled while connected


class RideNavUI(RideMemoryUI):
    ROUTE = {'route_status': 'ok', 'distance_m': 5231, 'duration_s': 1800, 'destination': '北京市东城区天安门',
             'steps': [{'instruction': '向东骑行200米右转', 'road': '长安街', 'distance_m': 200,
                        'end_lng': 116.392, 'end_lat': 39.908,
                        'path': [[116.394, 39.908], [116.392, 39.908]]},
                       {'instruction': '向南骑行5031米到达终点', 'road': '', 'distance_m': 5031,
                        'end_lng': 116.397477, 'end_lat': 39.908692,
                        'path': [[116.392, 39.908], [116.397477, 39.908692]]}]}

    def test_nav_page_strings(self):
        for text in ('nav-card', 'nav-destination', 'nav-plan', 'nav-clear', 'nav-status', 'nav-steps',
                     'nav-voice', 'wgs84ToGcj02', 'outOfChina', 'haversineM', 'NAV_TURN_M',
                     'distToPathM', 'distToSegmentM', 'NAV_ON_ROUTE_M', 'NAV_OFF_ROUTE_M',
                     'nav_offroute', '好像偏航了', '回到路线上了',
                     '/api/coach/ride/route', 'ride-persona/14', '偏航不会自动重算',
                     '不写入骑行记录', '高德'):
            with self.subTest(text=text):
                self.assertIn(text, self.html)

    def test_wgs84_gcj02_conversion_bounds(self):
        result = self.run_js("""
          const [bl,bt]=wgs84ToGcj02(116.397,39.908);
          const [ol,ot]=wgs84ToGcj02(-73.9857,40.7484);
          const [zl,zt]=wgs84ToGcj02(0,0);
          const again=wgs84ToGcj02(116.397,39.908);
          return {dlng:bl-116.397,dlat:bt-39.908,ol,ot,zl,zt,alng:again[0]-116.397,alat:again[1]-39.908};
        """)
        # Beijing offsets are small, positive and deterministic; abroad is identity.
        self.assertTrue(0.001 < result['dlng'] < 0.02)
        self.assertTrue(0.0005 < result['dlat'] < 0.02)
        self.assertEqual((result['ol'], result['ot']), (-73.9857, 40.7484))
        self.assertEqual((result['zl'], result['zt']), (0, 0))
        self.assertAlmostEqual(result['alng'], result['dlng'], places=12)
        self.assertAlmostEqual(result['alat'], result['dlat'], places=12)

    def test_haversine_known_distance(self):
        result = self.run_js("return {d:haversineM(116.0,39.0,116.0,40.0),zero:haversineM(1,1,1,1)};")
        self.assertTrue(110000 < result['d'] < 113000)
        self.assertEqual(result['zero'], 0)

    def test_plan_success_renders_steps_and_watches(self):
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          const call=_calls.find(c=>c.url==='/api/coach/ride/route');
          return {status:$('nav-status').textContent,steps:$('nav-steps').textContent,
                  clearHidden:$('nav-clear').hidden,watches:_geoWatches(),
                  body:call ? JSON.parse(call.body) : null,headers:call ? call.headers : null};
        """, route=self.ROUTE)
        self.assertIn('路线已规划', result['status'])
        self.assertIn('5.2', result['status'])
        self.assertIn('30', result['status'])
        self.assertIn('向东骑行200米右转（长安街）', result['steps'])
        self.assertIn('向南骑行5031米到达终点', result['steps'])
        self.assertFalse(result['clearHidden'])
        self.assertEqual(result['watches'], 1)
        self.assertEqual(result['headers']['X-Coach-Token'], 'synthetic-ui-token')
        self.assertEqual(result['body']['destination_address'], '天安门')
        self.assertRegex(result['body']['origin'], r'^116\.40[0-9]+,39\.90[0-9]+$')  # GCJ-02 shifted, not raw WGS-84
        self.assertNotEqual(result['body']['origin'], '116.397000,39.908000')

    def test_plan_status_mapping(self):
        cases = [('not_configured', '还没有配置'), ('key_rejected', '钥匙被拒绝'),
                 ('quota_exceeded', '额度用完'), ('address_not_found', '没找到这个目的地'),
                 ('no_route', '规划不到'), ('route_too_complex', '太复杂'),
                 ('upstream_error', '暂时不可用'), ('surprise', '暂时不可用')]
        for status, expected in cases:
            with self.subTest(status=status):
                result = self.run_js("""
                  $('nav-destination').value='天安门';
                  await $('nav-plan').click();
                  return {status:$('nav-status').textContent,steps:$('nav-steps').textContent,watches:_geoWatches()};
                """, route={'route_status': status})
                self.assertIn(expected, result['status'])
                self.assertEqual(result['steps'], '')
                self.assertEqual(result['watches'], 0)

    def test_plan_transport_failure_and_bad_shapes(self):
        for options in ({'routeFail': True},
                        {'route': {'route_status': 'ok', 'distance_m': 1, 'duration_s': 1, 'steps': []}},
                        {'route': {'route_status': 'ok', 'distance_m': 1, 'duration_s': 1, 'steps': [
                            {'instruction': '', 'road': '', 'distance_m': 1, 'end_lng': 116.4, 'end_lat': 39.9}]}},
                        {'route': {'route_status': 'ok', 'distance_m': 1, 'duration_s': 1, 'steps': [
                            {'instruction': '走', 'road': '', 'distance_m': 1, 'end_lng': 'x', 'end_lat': 39.9,
                             'path': [[116.4, 39.9]]}]}},
                        {'route': {'route_status': 'ok', 'distance_m': 1, 'duration_s': 1, 'steps': [
                            {'instruction': '走', 'road': '', 'distance_m': 1, 'end_lng': 116.4, 'end_lat': 39.9}]}},
                        {'route': {'route_status': 'ok', 'distance_m': 1, 'duration_s': 1, 'steps': [
                            {'instruction': '走', 'road': '', 'distance_m': 1, 'end_lng': 116.4, 'end_lat': 39.9,
                             'path': [['x', 39.9]]}]}}):
            with self.subTest(options=options):
                result = self.run_js("""
                  $('nav-destination').value='天安门';
                  await $('nav-plan').click();
                  return {status:$('nav-status').textContent,watches:_geoWatches()};
                """, **options)
                self.assertIn('暂时不可用', result['status'])
                self.assertEqual(result['watches'], 0)

    def test_plan_input_and_geo_failures(self):
        result = self.run_js("""
          await $('nav-plan').click();
          return {status:$('nav-status').textContent,calls:_calls.filter(c=>c.url==='/api/coach/ride/route').length};
        """)
        self.assertIn('先填写目的地', result['status'])
        self.assertEqual(result['calls'], 0)
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          return {status:$('nav-status').textContent};
        """, geoError=1)
        self.assertIn('定位权限被拒绝', result['status'])
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          return {status:$('nav-status').textContent};
        """, geoError=2)
        self.assertIn('获取当前位置失败', result['status'])

    def test_turn_guidance_announces_once_and_arrival(self):
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          await $('start-ride').click();
          // Invert the GCJ-02 shift so the emitted WGS-84 fix lands on the GCJ-02 step end.
          const step=navRoute.steps[0];
          const off=wgs84ToGcj02(step.end_lng,step.end_lat);
          _geoEmit(step.end_lng-(off[0]-step.end_lng), step.end_lat-(off[1]-step.end_lat));
          const afterFirst={spoken:_spoken.slice(),status:$('nav-status').textContent,index:navRoute.index};
          _geoEmit(step.end_lng-(off[0]-step.end_lng), step.end_lat-(off[1]-step.end_lat));
          const afterRepeat=_spoken.length;
          const last=navRoute.steps[navRoute.index];
          const off2=wgs84ToGcj02(last.end_lng,last.end_lat);
          _geoEmit(last.end_lng-(off2[0]-last.end_lng), last.end_lat-(off2[1]-last.end_lat));
          return {afterFirst,afterRepeat,spoken:_spoken,steps:$('nav-steps').textContent,
                  watches:_geoWatches(),notes:session.coach_notes};
        """, simulate=1, route=self.ROUTE)
        self.assertEqual(result['afterFirst']['index'], 1)
        self.assertIn('向东骑行200米右转', result['afterFirst']['spoken'])
        self.assertEqual(result['afterRepeat'], len(result['afterFirst']['spoken']))  # announced once
        self.assertIn('向南骑行5031米到达终点', result['spoken'])
        self.assertIn('已接近终点', result['spoken'][-1])
        self.assertEqual(result['watches'], 0)  # watch stopped at arrival
        self.assertIn('✓ 1.', result['steps'])
        self.assertTrue(any('向东骑行200米右转' in note for note in result['notes']))

    def test_guidance_respects_voice_toggle_and_requires_active(self):
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          $('nav-voice').checked=false;$('nav-voice').click();
          await $('start-ride').click();
          const step=navRoute.steps[0];
          const off=wgs84ToGcj02(step.end_lng,step.end_lat);
          _geoEmit(step.end_lng-(off[0]-step.end_lng), step.end_lat-(off[1]-step.end_lat));
          const muted={spoken:_spoken.slice(),status:$('nav-status').textContent,index:navRoute.index};
          return {muted};
        """, simulate=1, route=self.ROUTE)
        self.assertNotIn('向东骑行200米右转', result['muted']['spoken'])
        self.assertEqual(result['muted']['index'], 0)
        self.assertIn('下一转向', result['muted']['status'])

    def test_corridor_geometry_helpers(self):
        result = self.run_js("""
          const seg=distToSegmentM(0.001,0.005,[0,0],[0,0.01]);
          const beyond=distToSegmentM(0.001,0.02,[0,0],[0,0.01]);
          const path=distToPathM(0.001,0.005,[[0,0],[0,0.01]]);
          const single=distToPathM(0,0,[[0,0.01]]);
          const empty=distToPathM(0,0,[]);
          return {seg,beyond,path,single,empty};
        """)
        self.assertTrue(105 < result['seg'] < 117)        # ~111 m perpendicular to the segment
        self.assertTrue(1100 < result['beyond'] < 1160)   # clamps to the far endpoint
        self.assertAlmostEqual(result['path'], result['seg'], places=6)
        self.assertTrue(1100 < result['single'] < 1130)   # one-point path: distance to the point
        self.assertIsNone(result['empty'])                # Infinity serializes to null

    def test_offroute_warns_once_snoozes_and_recovers(self):
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          await $('start-ride').click();
          _geoEmit(116.396,39.920);   // far from both corridors even after the GCJ shift
          const first={spoken:_spoken.slice(),status:$('nav-status').textContent,flag:navOffRoute};
          _geoEmit(116.396,39.920);   // snoozed: no immediate repeat
          const repeat=_spoken.length;
          await _advance(181000);     // snooze expires
          _geoEmit(116.396,39.920);
          const third=_spoken.length;
          // Land back on the step-1 corridor within 60 m (invert the GCJ shift).
          const on=wgs84ToGcj02(116.394,39.9083);
          _geoEmit(116.394-(on[0]-116.394),39.9083-(on[1]-39.9083));
          return {first,repeat,third,spoken:_spoken,flag:navOffRoute};
        """, simulate=1, route=self.ROUTE)
        self.assertIn('好像偏航了', result['first']['spoken'][-1])
        self.assertIn('好像偏航了', result['first']['status'])
        self.assertTrue(result['first']['flag'])
        self.assertEqual(result['repeat'], len(result['first']['spoken']))
        self.assertEqual(result['third'], len(result['first']['spoken']) + 1)
        self.assertEqual(result['spoken'][-1], '回到路线上了。')
        self.assertFalse(result['flag'])

    def test_corridor_skip_advances_silently(self):
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          await $('start-ride').click();
          // Land on the step-2 corridor 300+ m past the skipped turn point.
          const on=wgs84ToGcj02(116.396,39.9085);
          _geoEmit(116.396-(on[0]-116.396),39.9085-(on[1]-39.9085));
          return {index:navRoute.index,spoken:_spoken.slice(),steps:$('nav-steps').textContent};
        """, simulate=1, route=self.ROUTE)
        self.assertEqual(result['index'], 1)
        self.assertFalse(any('向东骑行200米右转' in t for t in result['spoken']))
        self.assertIn('▶ 2.', result['steps'])

    def test_offroute_voice_off_updates_status_only(self):
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          $('nav-voice').checked=false;$('nav-voice').click();
          await $('start-ride').click();
          _geoEmit(116.396,39.920);
          return {spoken:_spoken.slice(),status:$('nav-status').textContent,flag:navOffRoute};
        """, simulate=1, route=self.ROUTE)
        self.assertFalse(any('好像偏航了' in t for t in result['spoken']))
        self.assertIn('好像偏航了', result['status'])
        self.assertTrue(result['flag'])

    def test_clear_and_finish_stop_watch(self):
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          $('nav-clear').click();
          const cleared={watches:_geoWatches(),steps:$('nav-steps').textContent,hidden:$('nav-clear').hidden,route:navRoute};
          return cleared;
        """, route=self.ROUTE)
        self.assertEqual(result['watches'], 0)
        self.assertEqual(result['steps'], '')
        self.assertTrue(result['hidden'])
        self.assertIsNone(result['route'])
        result = self.run_js("""
          $('nav-destination').value='天安门';
          await $('nav-plan').click();
          await $('start-ride').click();
          finishRide();
          return {watches:_geoWatches(),kept:$('nav-steps').textContent.length>0};
        """, simulate=1, route=self.ROUTE)
        self.assertEqual(result['watches'], 0)
        self.assertTrue(result['kept'])  # the plan stays visible after the ride


if __name__ == '__main__':
    unittest.main()
