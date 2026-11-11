"""Real Mark location JavaScript, synthetic sensors only. No paid APIs or OS claims."""
import json
import subprocess
import unittest
import test_mark_recovery as recovery

WEATHER = recovery.HTML.split('let lastWeather=null,weatherFetchedAt=0;', 1)[1].split('async function fetchSessions()', 1)[0]

class MarkLocationTests(unittest.TestCase):
    def run_js(self, scenario, weather=False):
        source = recovery.RIDE
        if weather:
            source += 'let lastWeather=null,weatherFetchedAt=0;function updateDashboard(){}' + WEATHER
        result = subprocess.run([recovery.NODE, '-e', recovery.HARNESS], input=json.dumps({'ride': source, 'init': recovery.INIT, 'scenario': scenario}), capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_insecure_and_missing_api_are_explained_before_request(self):
        r=self.run_js("return await ctx.run(\"window.isSecureContext=false;startRide();const a=$('location-status').textContent;window.isSecureContext=true;delete navigator.geolocation;await checkLocation();return {a,b:$('location-status').textContent,riding};\");")
        self.assertIn('HTTPS',r['a']);self.assertIn('定位接口',r['b']);self.assertFalse(r['riding'])

    def test_denied_permission_is_not_blindly_retried(self):
        r=self.run_js("return await ctx.run(\"let count=0;navigator.geolocation.getCurrentPosition=(ok,err)=>{count++;err({code:1});};await checkLocation();return {count,status:$('location-status').textContent,disabled:$('location-check').disabled,requests:_calls.length};\");")
        self.assertEqual(r['count'],1);self.assertIn('PERMISSION_DENIED',r['status']);self.assertFalse(r['disabled']);self.assertEqual(r['requests'],0)

    def test_unavailable_retries_with_alternate_accuracy_once(self):
        r=self.run_js("return await ctx.run(\"let opts=[];navigator.geolocation.getCurrentPosition=(ok,err,o)=>{opts.push(o);if(opts.length===1)err({code:2});else ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:1000}});};const p=await getLocation();return {opts,accuracy:p.coords.accuracy};\");")
        self.assertEqual([o['enableHighAccuracy'] for o in r['opts']],[False,True]);self.assertEqual(r['accuracy'],1000)
        self.assertTrue(all(o['timeout']>=10000 for o in r['opts']))

    def test_silent_provider_has_bounded_timeout_and_late_results_ignored(self):
        r=self.run_js("return await ctx.run(\"let callbacks=[];navigator.geolocation.getCurrentPosition=ok=>callbacks.push(ok);const pending=checkLocation();[..._timeouts.values()][0].fn();await Promise.resolve();await Promise.resolve();[..._timeouts.values()][0].fn();await pending;const before=$('location-status').textContent;callbacks.forEach(ok=>ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:5}}));return {before,after:$('location-status').textContent,requests:_calls.length};\");")
        self.assertIn('TIMEOUT',r['before']);self.assertEqual(r['before'],r['after']);self.assertEqual(r['requests'],0)

    def test_double_click_coalesces_checks(self):
        r=self.run_js("return await ctx.run(\"let count=0,reply;navigator.geolocation.getCurrentPosition=ok=>{count++;reply=ok;};const a=checkLocation(),b=checkLocation();reply({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:5}});await Promise.all([a,b]);return count;\");")
        self.assertEqual(r,1)

    def test_coarse_location_can_query_weather_without_becoming_ride(self):
        r=self.run_js("return await ctx.run(\"navigator.geolocation.getCurrentPosition=ok=>ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:1000}});fetchWithTimeout=async(url,opts)=>{_calls.push({url,body:JSON.parse(opts.body)});return {ok:true,json:async()=>({weather_status:'ok',weather:{city:'测试城市',day_temp:20}})};};await checkLocation();return {status:$('location-status').textContent,weather:lastWeather,calls:_calls.length,riding,points:ridePoints.length};\");",weather=True)
        self.assertIn('粗定位',r['status']);self.assertEqual(r['weather']['city'],'测试城市');self.assertEqual(r['calls'],1);self.assertFalse(r['riding']);self.assertEqual(r['points'],0)

    def test_weather_configuration_failure_does_not_blame_permission(self):
        r=self.run_js("return await ctx.run(\"navigator.geolocation.getCurrentPosition=ok=>ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:5}});fetchWithTimeout=async()=>({ok:true,json:async()=>({weather_status:'not_configured'})});await checkLocation();return {status:$('location-status').textContent,note:$('dash-weather-note').textContent};\");",weather=True)
        self.assertIn('已获取位置',r['status']);self.assertIn('开发端',r['note']);self.assertIn('不是你没开定位',r['note'])

    def test_very_coarse_weather_is_not_sent(self):
        r=self.run_js("return await ctx.run(\"navigator.geolocation.getCurrentPosition=ok=>ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:50000}});await checkLocation();return {note:$('dash-weather-note').textContent,calls:_calls.length};\");",weather=True)
        self.assertIn('20 公里',r['note']);self.assertEqual(r['calls'],0)

    def test_watch_errors_visible_on_ride_and_clear_current_speed(self):
        for code,label in ((1,'PERMISSION_DENIED'),(2,'POSITION_UNAVAILABLE'),(3,'TIMEOUT')):
            with self.subTest(code=code):
                r=self.run_js(f"return await ctx.run(\"startRide();_clock.now+=1000;_watches.get(watchId).ok({{timestamp:Date.now(),coords:{{latitude:31,longitude:121,accuracy:5,speed:2}}}});_watches.get(watchId).error({{code:{code}}});return {{status:$('ride-location-status').textContent,speed:$('ride-speed').textContent,ready:liveFixReady()}};\");")
                self.assertIn(label,r['status']);self.assertEqual(r['speed'],'--');self.assertFalse(r['ready'])

    def test_coarse_watch_is_visible_but_not_recorded_and_can_recover(self):
        r=self.run_js("return await ctx.run(\"startRide();_clock.now+=1000;const watch=_watches.get(watchId);watch.ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:500,speed:2}});const coarse={points:ridePoints.length,status:$('ride-location-status').textContent};_clock.now+=1000;watch.ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:5,speed:2}});return {coarse,points:ridePoints.length,status:$('ride-location-status').textContent};\");")
        self.assertEqual(r['coarse']['points'],0);self.assertIn('80 米',r['coarse']['status']);self.assertEqual(r['points'],1);self.assertIn('已连接',r['status'])

    def test_watch_timeout_is_visible_even_if_browser_says_nothing(self):
        r=self.run_js("return await ctx.run(\"startRide();_clock.now+=22000;[..._timeouts.values()][0].fn();renderStrip();return {status:$('ride-location-status').textContent,points:ridePoints.length,time:$('ride-time').textContent};\");")
        self.assertIn('TIMEOUT',r['status']);self.assertEqual(r['points'],0)

    def test_retry_replaces_watch_and_ignores_old_callbacks(self):
        r=self.run_js("return await ctx.run(\"startRide();const old=_watches.get(watchId);await checkLocation();_clock.now+=1000;old.ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:5,speed:2}});old.error({code:1});return {watches:_watches.size,points:ridePoints.length,status:$('ride-location-status').textContent};\");")
        self.assertEqual(r['watches'],1);self.assertEqual(r['points'],0);self.assertIn('等待定位',r['status'])

    def test_ending_ride_clears_watch_and_timer_and_ignores_late_fix(self):
        r=self.run_js("return await ctx.run(\"startRide();const old=_watches.get(watchId);endRide();const status=$('ride-location-status').textContent;old.ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:5,speed:2}});return {watches:_watches.size,timers:_timeouts.size,points:ridePoints.length,status,after:$('ride-location-status').textContent};\");")
        self.assertEqual(r['watches'],0);self.assertEqual(r['timers'],0);self.assertEqual(r['points'],0);self.assertEqual(r['status'],r['after'])

    def test_error_during_live_request_does_not_speak_stale_response(self):
        r=self.run_js("return await ctx.run(\"startRide();_clock.now+=1000;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:5,speed:2}});fetchWithTimeout=async()=>{_watches.get(watchId).error({code:2});return {ok:true,json:async()=>({calls:[{speech:'old-location-advice'}]})};};await liveBeat();return _messages.includes('old-location-advice');\");")
        self.assertFalse(r)

    def test_negative_accuracy_is_rejected(self):
        r=self.run_js("return await ctx.run(\"startRide();_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:31,longitude:121,accuracy:-1,speed:2}});return {points:ridePoints.length,status:$('ride-location-status').textContent};\");")
        self.assertEqual(r['points'],0);self.assertIn('POSITION_UNAVAILABLE',r['status'])


    def test_amap_boundary_converts_but_record_keeps_raw_coordinates(self):
        r=self.run_js("return await ctx.run(\"navigator.geolocation.getCurrentPosition=ok=>ok({timestamp:Date.now(),coords:{latitude:39.908,longitude:116.397,accuracy:5}});fetchWithTimeout=async(url,opts)=>{_calls.push({url,body:JSON.parse(opts.body)});return {ok:true,json:async()=>({weather_status:'ok',weather:{city:'测试城市'}})};};await ensureWeather();startRide();_clock.now+=1000;_watches.get(watchId).ok({timestamp:Date.now(),coords:{latitude:39.908,longitude:116.397,accuracy:5,speed:2}});return {payload:_calls[0].body,raw:ridePoints[0],abroad:wgs84ToGcj02(-73.9857,40.7484)};\");",weather=True)
        self.assertTrue(116.398 < r['payload']['lng'] < 116.417)
        self.assertTrue(39.9085 < r['payload']['lat'] < 39.928)
        self.assertEqual(r['raw']['lng'],116.397);self.assertEqual(r['raw']['lat'],39.908)
        self.assertEqual(r['abroad'],[-73.9857,40.7484])

if __name__=='__main__':unittest.main()
