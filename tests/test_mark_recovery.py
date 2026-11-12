"""Execute Mark's real ride functions in Node; no network, sensors, or real user data.

This verifies recovery/freshness logic, not mobile OS background behaviour.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from ride_memory import validate_session

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node') or str(Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
HTML = (ROOT / 'static/mark.html').read_text(encoding='utf-8')
RIDE = HTML.split('let ridePoints=[]', 1)[1].split('/* ================= 语音输入', 1)[0]
LOCATION = HTML.split('/* ================= 定位检查 ================= */', 1)[1].split('/* ================= 上下文数据 ================= */', 1)[0]
RIDE = LOCATION + 'let ridePoints=[]' + RIDE
INIT = HTML.split("try{const draft=JSON.parse(preferenceRead(PENDING_RIDE_KEY", 1)[1].split("window.addEventListener('beforeunload'", 1)[0]
INIT = 'try{const draft=JSON.parse(preferenceRead(PENDING_RIDE_KEY' + INIT
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const storage=new Map(), clock={now:Date.parse('2026-11-10T00:00:00Z')};
function boot(){
 const elements={},listeners={},watches=new Map(),messages=[],calls=[],intervals=[],timeouts=new Map();let timerId=0;
 const elem=id=>elements[id]||(elements[id]={textContent:'',hidden:true,dataset:{}});
 class ClockDate extends Date{constructor(...a){super(...(a.length?a:[clock.now]));}static now(){return clock.now;}}
 const s={Date:ClockDate,JSON,Math,Number,Array,String,Error,Promise,AbortController,
 crypto:require('node:crypto').webcrypto,window:{addEventListener:(k,f)=>listeners[k]=f,confirm:()=>true},
 document:{getElementById:elem,hidden:false,addEventListener:(k,f)=>listeners[k]=f},
 navigator:{geolocation:{watchPosition:(ok,error)=>{watches.set(watches.size,{ok,error});return watches.size-1;},clearWatch:id=>watches.delete(id)}},
 localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 setInterval:f=>{intervals.push(f);return intervals.length;},clearInterval(){},setTimeout:(fn,ms)=>{timeouts.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timeouts.delete(id),
 $:elem,setView(){},updateRidePanel(){},addMsg:(who,text)=>messages.push(text),speak(){},cancelSpeech(){},refreshChips(){},
 voiceAuto:false,ensureWeather:async()=>null,getToken:async()=>'synthetic',loadHistory:async()=>{},
 fetchWithTimeout:async(url,opts)=>{calls.push({url,body:JSON.parse(opts.body)});throw new Error('offline');},
 _storage:storage,_clock:clock,_elements:elements,_listeners:listeners,_watches:watches,_messages:messages,_calls:calls,_intervals:intervals,_timeouts:timeouts};
 vm.createContext(s);
 vm.runInContext(__MARK_CLOUD_SOURCE__,s);s.MarkCloud=s.window.MarkCloud;
 vm.runInContext(`function preferenceRead(k,f){try{return localStorage.getItem(k)||f;}catch(_){return f;}}
 function preferenceWrite(k,v){try{localStorage.setItem(k,v);return true;}catch(_){return false;}}`+input.ride+input.init,s);
 s.run=code=>vm.runInContext('(async()=>{'+code+'})()',s);
 return s;
}
(async()=>{const ctx=boot();const result=await new Function('boot','ctx','clock','storage','return (async()=>{'+input.scenario+'})()')(boot,ctx,clock,storage);console.log(JSON.stringify(result));})().catch(e=>{console.error(e);process.exit(1);});
""".replace("__MARK_CLOUD_SOURCE__", json.dumps((ROOT / "static/mark-cloud.js").read_text(encoding="utf-8")))


class MarkRecoveryTests(unittest.TestCase):
    def run_js(self, scenario):
        result = subprocess.run([NODE, '-e', HARNESS], input=json.dumps({'ride': RIDE, 'init': INIT, 'cloud': (ROOT / 'static/mark-cloud.js').read_text(encoding='utf-8'), 'scenario': scenario}), capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_start_creates_checkpoint_before_any_gps(self):
        record = self.run_js("await ctx.run('startRide();');return JSON.parse(storage.get('mark.activeRide'));")
        self.assertEqual(record['series'], [])
        self.assertIsNone(record['summary']['distance_m'])
        validate_session(record)

    def test_reload_recovers_original_id_without_auto_save_or_resume(self):
        result = self.run_js("""
          await ctx.run('startRide();');clock.now+=10000;await ctx.run('checkpointRide(true);');
          const prior=JSON.parse(storage.get('mark.activeRide'));clock.now+=3600000;
          const next=boot();const state=await next.run("startRide();return {riding,pendingRide,status:$('ride-save-status').textContent,calls:_calls.length};");
          return {prior,state};
        """)
        self.assertFalse(result['state']['riding'])
        recovered = result['state']['pendingRide']
        self.assertIn('中断检查点', recovered['coach_notes'][-1])
        recovered['coach_notes'] = result['prior']['coach_notes']
        self.assertEqual(recovered, result['prior'])
        self.assertEqual(result['state']['calls'], 0)
        self.assertEqual(result['state']['pendingRide']['summary']['duration_s'], 10)

    def test_checkpoint_storage_error_is_visible(self):
        result = self.run_js("return await ctx.run(\"localStorage.setItem=()=>{throw new Error('quota');};startRide();return $('ride-save-status').textContent;\");")
        self.assertIn('无法保存检查点', result)

    def test_pagehide_checkpoints_latest_elapsed_time(self):
        record = self.run_js("await ctx.run('startRide();');clock.now+=7000;ctx._listeners.pagehide();return JSON.parse(storage.get('mark.activeRide'));")
        self.assertEqual(record['summary']['duration_s'], 7)
        validate_session(record)

    def test_hidden_visibility_event_checkpoints(self):
        record = self.run_js("await ctx.run('startRide();');clock.now+=8000;ctx.document.hidden=true;ctx._listeners.visibilitychange();return JSON.parse(storage.get('mark.activeRide'));")
        self.assertEqual(record['summary']['duration_s'], 8)

    def test_periodic_checkpoint_without_gps(self):
        record = self.run_js("await ctx.run('startRide();');clock.now+=10000;ctx._intervals[0]();return JSON.parse(storage.get('mark.activeRide'));")
        self.assertEqual(record['summary']['duration_s'], 10)

    def test_corrupt_checkpoint_is_preserved_and_blocks_overwrite(self):
        result = self.run_js("storage.set('mark.activeRide','broken json');const next=boot();return await next.run(\"startRide();return {riding,recoveryProblem,raw:_storage.get(ACTIVE_RIDE_KEY),visible:!$('ride-save-export').hidden};\");")
        self.assertFalse(result['riding'])
        self.assertTrue(result['recoveryProblem'])
        self.assertEqual(result['raw'], 'broken json')
        self.assertTrue(result['visible'])

    def test_corrupt_checkpoint_discard_requires_confirmation(self):
        result = self.run_js("storage.set('mark.activeRide','broken');const next=boot();next.window.confirm=()=>false;next._elements['ride-draft-discard'].onclick();return storage.get('mark.activeRide');")
        self.assertEqual(result, 'broken')

    def test_single_gps_sample_can_be_saved_without_invented_distance(self):
        record = self.run_js("return await ctx.run(\"startRide();_clock.now+=2000;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:10,speed:0}});_clock.now+=2000;return buildRideRecord(Date.now());\");")
        self.assertEqual(len(record['series']), 1)
        self.assertIsNone(record['summary']['distance_m'])
        validate_session(record)

    def test_two_gps_samples_keep_true_track_aggregates(self):
        record = self.run_js("return await ctx.run(\"startRide();for(let i=1;i<=2;i++){_clock.now+=5000;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121+i*.00001,accuracy:10,speed:1}});}return buildRideRecord(Date.now());\");")
        self.assertGreater(record['summary']['distance_m'], 0)
        validate_session(record)

    def test_save_failure_keeps_recoverable_checkpoint_and_record_id(self):
        result = self.run_js("await ctx.run('startRide();');clock.now+=10000;await ctx.run('endRide();');await new Promise(r=>setImmediate(r));return await ctx.run('return {active:JSON.parse(_storage.get(ACTIVE_RIDE_KEY)),pending:pendingRide,stored:JSON.parse(_storage.get(PENDING_RIDE_KEY))};');")
        self.assertEqual(result['active']['id'], result['pending']['id'])
        self.assertEqual(result['stored'], result['pending'])
        validate_session(result['stored'])

    def test_confirmed_save_clears_both_drafts(self):
        result = self.run_js("""
          await ctx.run("startRide();_clock.now+=10000;fetchWithTimeout=async(url,opts)=>({ok:true,json:async()=>({status:'saved',id:JSON.parse(opts.body).id})});endRide();");
          await new Promise(r=>setImmediate(r));return [...storage.keys()];
        """)
        self.assertNotIn('mark.activeRide', result)
        self.assertNotIn('mark.pendingRide', result)

    def test_stale_gps_never_reuploaded_as_current(self):
        result = self.run_js("return await ctx.run(\"startRide();_clock.now+=1000;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:10,speed:5}});_clock.now+=16000;await liveBeat();renderStrip();return {calls:_calls.length,speed:$('ls-speed').textContent,status:geoNotice};\");")
        self.assertEqual(result['calls'], 0)
        self.assertEqual(result['speed'], '--')
        self.assertIn('过期', result['status'])

    def test_live_upload_uses_observation_timestamp(self):
        result = self.run_js("return await ctx.run(\"startRide();_clock.now+=1000;const observed=Date.now();_watches.get(watchId).ok({timestamp:observed,coords:{latitude:31,longitude:121,accuracy:10,speed:5}});_clock.now+=5000;await liveBeat();return {observed,point:_calls[0].body.point};\");")
        self.assertEqual(result['point']['t'], result['observed'] / 1000)

    def test_old_cached_gps_callback_is_rejected(self):
        result = self.run_js("return await ctx.run(\"startRide();_watches.get(watchId).ok({timestamp:Date.now()-20000,coords:{latitude:31,longitude:121,accuracy:10,speed:5}});return ridePoints.length;\");")
        self.assertEqual(result, 0)

    def test_fix_expiring_during_token_request_is_not_sent(self):
        result = self.run_js("return await ctx.run(\"startRide();_clock.now+=1000;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:10,speed:5}});getToken=async()=>{_clock.now+=16000;return 'token';};await liveBeat();return _calls.length;\");")
        self.assertEqual(result, 0)

    def test_expired_live_response_does_not_speak(self):
        result = self.run_js("return await ctx.run(\"startRide();_clock.now+=1000;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:10,speed:5}});fetchWithTimeout=async()=>{_clock.now+=16000;return {ok:true,json:async()=>({calls:[{speech:'stale advice'}]})};};await liveBeat();return _messages.includes('stale advice');\");")
        self.assertFalse(result)


    def test_unknown_speed_is_not_uploaded_as_zero(self):
        result = self.run_js("return await ctx.run(\"startRide();_clock.now+=1000;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:10,speed:null}});await liveBeat();return {calls:_calls.length,ready:liveFixReady(),points:ridePoints.length};\");")
        self.assertEqual(result['calls'], 0)
        self.assertFalse(result['ready'])
        self.assertEqual(result['points'], 1)

    def test_old_gps_callback_cannot_pollute_new_ride(self):
        result = self.run_js("return await ctx.run(\"startRide();const old=_watches.get(watchId).ok;endRide();pendingRide=null;rideSaving=false;startRide();_clock.now+=2000;old({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:10,speed:2}});return ridePoints.length;\");")
        self.assertEqual(result, 0)

    def test_sampling_gap_is_disclosed_in_saved_record(self):
        record = self.run_js("return await ctx.run(\"startRide();for(const ms of [1000,40000]){_clock.now+=ms;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:10,speed:0}});}return buildRideRecord(Date.now());\");")
        self.assertIn('采样间隔', record['coach_notes'][0])
        validate_session(record)

if __name__ == '__main__':
    unittest.main()
