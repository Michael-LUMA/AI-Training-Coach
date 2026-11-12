/* Cloud credentials are separate from X-Coach-Token. No device-id recovery. */
(function (root) {
  'use strict';
  const KEY='mark.cloud.identity.v1', DELETED='mark.cloud.deleted.v1';
  let enabled=false, browserToken='', flight=null, deleting=false, canDelete=()=>true;
  const status=text=>{const e=document.getElementById('cloud-status');if(e)e.textContent=text;};
  function read(){
    const value=localStorage.getItem(KEY);
    if(!value)return null;
    const record=JSON.parse(value);
    if(!record||typeof record.token!=='string'||!record.token||typeof record.user_id!=='string')throw new Error('云身份损坏，请勿清理站点数据；先导出已有备份。');
    return record;
  }
  async function raw(path,payload){
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),20000);
    try {
      const response=await fetch(path,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Coach-Token':browserToken},body:JSON.stringify(payload),signal:controller.signal});
      const value=await response.json().catch(()=>null);
      if(!response.ok){const error=new Error(response.status===401?'云身份无效，已停止云请求；不会自动换身份。':response.status===409?'发生冲突，请先导出数据再处理。':'云请求失败（'+response.status+'），数据未确认保存。');error.code=value&&value.error_code;error.status=response.status;throw error;}
      if(!value||typeof value!=='object')throw new Error('云响应无效');
      return value;
    } finally {clearTimeout(timer);}
  }
  async function identity(){
    if(!enabled)throw new Error('此服务未启用云数据库');
    if(deleting)throw new Error('正在删除数据，请稍后重试');
    if(localStorage.getItem(DELETED))throw new Error('云数据已删除；需要手动创建新身份才能继续。');
    const existing=read();
    if(existing)return existing;
    if(flight)return flight;
    flight=(async()=>{
      // Fail BEFORE registering when persistent browser storage is unavailable.
      localStorage.setItem('mark.cloud.storage-probe','1');localStorage.removeItem('mark.cloud.storage-probe');
      const device='web-'+crypto.randomUUID();
      const record=await raw('/api/auth/register',{device_id:device});
      if(typeof record.token!=='string'||!record.token||typeof record.user_id!=='string')throw new Error('注册响应无效');
      // Another tab may have registered while this request was in flight.
      const winner=read();if(winner)return winner;
      localStorage.setItem(KEY,JSON.stringify(record));
      status('云身份已建立 · 仅上传运动聚合摘要、对话文字与云笔记；原始音频和轨迹不在数据包内。');
      return record;
    })();
    try{return await flight;}finally{flight=null;}
  }
  async function withIdentity(payload){
    if(!enabled)return payload;
    const account=await identity();
    return {...payload,token:account.token};
  }
  async function post(path,payload={}){
    try{return await raw(path,await withIdentity(payload));}
    catch(error){status(error.message);throw error;}
  }
  function summary(record){
    // Explicit seven-field projection: never copy series/track/GPS/audio.
    return {id:record.id,started_at:record.started_at,ended_at:record.ended_at,
      summary:record.summary,source:record.source,activity:record.activity||'ride',coach_notes:record.coach_notes||[]};
  }
  async function exportData(){
    const data=await post('/api/data/export');
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download='Mark-cloud-data-'+new Date().toISOString().slice(0,10)+'.json';
    document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    status('已生成完整云数据下载，请确认文件已保存；不包含此浏览器的本地轨迹/草稿。');
    return data;
  }
  async function deleteData(){
    if(!canDelete())throw new Error('请先结束运动并等待当前对话/保存完成，再删除云数据。');
    if(!window.confirm('将永久硬删除当前身份的全部云运动摘要、画像、对话、笔记和身份，且不可恢复。建议先导出。浏览器本地轨迹、草稿及旧版共享记录不受影响。继续？'))return;
    if(window.prompt('二次确认：请输入 删除云数据')!=='删除云数据')return;
    const account=await identity();deleting=true;
    try{
      const result=await raw('/api/data/delete',{token:account.token,confirm:true});
      // Never create a replacement account automatically after deletion/reload.
      localStorage.setItem(DELETED,'1');localStorage.removeItem(KEY);
      status('云端已硬删除 '+result.total_deleted+' 条记录，旧凭证已失效。');
      window.location.reload();
      return result;
    }catch(error){status('删除结果未能确认，请重试或检查身份状态；不要当作已删除。');throw error;}
    finally{deleting=false;}
  }
  async function configure(options){
    enabled=options.enabled===true;browserToken=options.browserToken;
    if(options.canDelete)canDelete=options.canDelete;
    const exp=document.getElementById('cloud-export'),del=document.getElementById('cloud-delete'),fresh=document.getElementById('cloud-new');
    if(exp)exp.onclick=()=>exportData().catch(error=>status(error.message));
    if(del)del.onclick=()=>deleteData().catch(error=>status(error.message));
    if(fresh)fresh.onclick=()=>{
      if(!window.confirm('创建全新云身份？这不会删除旧身份的数据，也不会转移数据；旧凭证将从此浏览器移除，不能凭设备 ID 找回。请先导出能访问的数据。'))return;
      localStorage.removeItem(KEY);localStorage.removeItem(DELETED);identity().then(()=>window.location.reload()).catch(error=>status(error.message));
    };
    if(exp)exp.disabled=!enabled;if(del)del.disabled=!enabled;
    if(fresh)fresh.hidden=!enabled||!localStorage.getItem(DELETED);
    if(!enabled){status('当前是本机模式：DATABASE_URL 未启用。云导出与云删除不可用。');return;}
    try{
      const account=await identity();await raw('/api/auth/whoami',{token:account.token});
      status('云身份有效 · 凭证保存在此浏览器，不是跨设备账号。请勿清理站点数据；device_id 无法找回 token。');
    }catch(error){status(error.message);if(fresh)fresh.hidden=false;}
  }
  root.MarkCloud={configure,identity,withIdentity,post,summary,exportData,deleteData,get enabled(){return enabled;}};
})(window);
