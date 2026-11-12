'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('static/mark-cloud.js','utf8');
function setup({stored=null,deleted=false,failStorage=false}={}){
 const calls=[],values=new Map(),elements=new Map();let registrations=0,confirm=true,prompt='删除云数据',reloaded=false;
 if(stored)values.set('mark.cloud.identity.v1',JSON.stringify(stored));if(deleted)values.set('mark.cloud.deleted.v1','1');
 const document={getElementById:id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);},createElement:()=>({click(){},remove(){}}),body:{appendChild(){}}};
 const localStorage={getItem:k=>values.get(k)||null,setItem(k,v){if(failStorage)throw new Error('storage denied');values.set(k,v);},removeItem:k=>values.delete(k)};
 const context={window:{confirm:()=>confirm,prompt:()=>prompt,location:{reload:()=>{reloaded=true;}}},document,localStorage,crypto:{randomUUID:()=> 'random-'+registrations},Blob,URL:{createObjectURL:()=> 'blob:export',revokeObjectURL(){}},AbortController,setTimeout:()=>1,clearTimeout(){},
 fetch:async(path,options)=>{
  const body=JSON.parse(options.body);calls.push({path,body,headers:options.headers});
  let data={},status=200;
  if(path==='/api/auth/register'){registrations++;data={user_id:'user-'+registrations,token:'secret-'+registrations};}
  else if(body.token==='expired'){status=401;data={error_code:'invalid_token'};}
  else if(path==='/api/auth/whoami')data={user_id:'user-1'};
  else if(path==='/api/data/export')data={rides:[],profile:null,dialog:[],notes:[]};
  else if(path==='/api/data/delete')data={deleted:{users:1},total_deleted:1,token_revoked:true};
  return {ok:status===200,status,json:async()=>data};
 }};
 vm.createContext(context);vm.runInContext(source,context);const api=context.window.MarkCloud;
 return {api,calls,values,elements,context,setConfirm:v=>confirm=v,setPrompt:v=>prompt=v,get reloaded(){return reloaded;},get registrations(){return registrations;}};
}
test('automatic register persists token; reload reuses credential without device recovery',async()=>{
 const s=setup();await s.api.configure({enabled:true,browserToken:'browser-gate'});
 assert.equal(s.registrations,1);assert.ok(s.values.has('mark.cloud.identity.v1'));
 const account=JSON.parse(s.values.get('mark.cloud.identity.v1'));
 const second=setup({stored:account});await second.api.configure({enabled:true,browserToken:'new-browser-gate'});
 assert.equal(second.registrations,0);assert.equal(second.calls[0].path,'/api/auth/whoami');
});
test('all three chains include identity but keep the browser gate separate',async()=>{
 const s=setup();await s.api.configure({enabled:true,browserToken:'browser-gate'});
 for(const path of ['/api/coach/ride/chat','/api/coach/ride/review','/api/coach/ride/live']){
  await s.api.post(path,{question:'test'});const c=s.calls.at(-1);
  assert.equal(c.body.token,'secret-1');assert.equal(c.headers['X-Coach-Token'],'browser-gate');assert.equal(c.body.user_id,undefined);
 }
 for(const [name,paths] of [['mark',['chat','live']],['ride',['chat','review','live']]]){
  const html=fs.readFileSync('static/'+name+'.html','utf8');
  for(const path of paths){
   const lines=html.split('\n'),index=lines.findIndex(l=>l.includes('fetch')&&l.includes('/api/coach/ride/'+path+"'"));
   assert.ok(index>=0,name+':'+path);
   if(path==='live'){
    assert.ok(lines[index].includes('JSON.stringify(payload)'),name+':live sends authenticated payload');
    assert.ok(lines.slice(index-2,index).some(l=>l.includes('const payload=await MarkCloud.withIdentity(')),name+':live identity resolved before freshness guard');
   }else assert.ok(lines[index].includes('MarkCloud.withIdentity'),name+':'+path);
  }
 }
});
test('concurrent requests share one registration',async()=>{
 const s=setup();void s.api.configure({enabled:true,browserToken:'gate'});
 const accounts=await Promise.all(Array.from({length:8},()=>s.api.identity()));
 assert.equal(s.registrations,1);assert.ok(accounts.every(x=>x.token===accounts[0].token));
});
test('expired credential does not fall back or register a replacement',async()=>{
 const s=setup({stored:{user_id:'old',token:'expired'}});await s.api.configure({enabled:true,browserToken:'gate'});
 await assert.rejects(()=>s.api.post('/api/data/export'));
 assert.equal(s.registrations,0);assert.ok(s.calls.every(c=>c.body.token==='expired'));
});
test('blocked storage prevents registration; local-only mode stays local',async()=>{
 const s=setup({failStorage:true});await s.api.configure({enabled:true,browserToken:'gate'});
 await assert.rejects(()=>s.api.identity());assert.equal(s.registrations,0);
 const local=setup();await local.api.configure({enabled:false,browserToken:'gate'});
 const body={question:'local'};assert.equal(await local.api.withIdentity(body),body);assert.equal(local.calls.length,0);
});
test('export is acknowledged before a file is generated; no token in exported data',async()=>{
 const s=setup();await s.api.configure({enabled:true,browserToken:'gate'});
 const data=await s.api.exportData();assert.deepEqual(Object.keys(data),['rides','profile','dialog','notes']);assert.ok(!JSON.stringify(data).includes('secret'));
});
test('first and second cancel cause no destructive request',async()=>{
 const s=setup();await s.api.configure({enabled:true,browserToken:'gate'});s.setConfirm(false);await s.api.deleteData();
 s.setConfirm(true);s.setPrompt('wrong');await s.api.deleteData();assert.ok(!s.calls.some(c=>c.path==='/api/data/delete'));
});
test('confirmed delete hard-revokes, clears token, prevents automatic re-register',async()=>{
 const s=setup();await s.api.configure({enabled:true,browserToken:'gate'});await s.api.deleteData();
 const deletion=s.calls.find(c=>c.path==='/api/data/delete');assert.equal(deletion.body.confirm,true);
 assert.ok(!s.values.has('mark.cloud.identity.v1'));assert.equal(s.values.get('mark.cloud.deleted.v1'),'1');assert.ok(s.reloaded);
 await assert.rejects(()=>s.api.identity());assert.equal(s.registrations,1);
 const next=setup({deleted:true});await next.api.configure({enabled:true,browserToken:'gate'});assert.equal(next.registrations,0);
});
test('active ride blocks deletion before either confirmation',async()=>{
 const s=setup();await s.api.configure({enabled:true,browserToken:'gate',canDelete:()=>false});await assert.rejects(()=>s.api.deleteData());assert.ok(!s.calls.some(c=>c.path==='/api/data/delete'));
});
test('summary projection never uploads raw track, audio or sensor series',()=>{
 const s=setup();const value=s.api.summary({id:'ride',series:[{lat:1,lng:2}],track:[1],audio:'private',summary:{duration_s:60},source:'web_gps',started_at:'start',ended_at:'end'});
 assert.deepEqual(Object.keys(value).sort(),['activity','coach_notes','ended_at','id','source','started_at','summary']);assert.ok(!JSON.stringify(value).includes('private'));
});
