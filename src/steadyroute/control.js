/* Control plane UI: saved state is always distinct from drafts and last operation. */
(() => {
  'use strict';
  const esc = x => String(x ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const style = document.createElement('style');
  style.textContent = `.sr-control{font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink,#152536);background:var(--surface,#fff);border:1px solid var(--hair-2,#cbd5e1);border-radius:14px;padding:22px;margin:20px 0 28px}.sr-control h2{font-size:20px;line-height:1.4;margin:0 0 8px}.sr-control p{max-width:74ch;margin:8px 0}.sr-control .sr-actions{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.sr-control button{font:inherit;min-height:44px;padding:8px 16px;border:1px solid var(--hair-2,#bac7d3);border-radius:8px;cursor:pointer;color:inherit;background:var(--sunken,#f1f5f9)}.sr-control button:hover{border-color:var(--accent,#0e74a3)}.sr-control button:disabled{opacity:.55;cursor:wait}.sr-control button:focus-visible,.sr-control input:focus-visible{outline:2px solid var(--accent,#0e74a3);outline-offset:3px}.sr-control .sr-primary{color:white;background:#0b6590;border-color:#0b6590}.sr-control .sr-danger{color:var(--bad,#a52c26)}.sr-control .sr-error{color:var(--bad,#a52c26);font-weight:600}.sr-control label{display:flex;align-items:flex-start;gap:9px;min-height:44px;max-width:75ch}.sr-control input{margin-top:7px}.sr-control select{font:inherit;min-height:44px;max-width:100%;color:inherit;background:var(--surface,#fff);border:1px solid var(--hair-2,#bac7d3);border-radius:8px;padding:8px}.sr-control .sr-preview{padding:12px 0;border-top:1px solid var(--hair-2,#cbd5e1)}.sr-control ul{padding-left:22px;overflow-wrap:anywhere}.sr-control [hidden]{display:none!important}@media(max-width:600px){.sr-control{padding:16px}.sr-control .sr-actions button{flex:1 1 180px}}`;
  document.head.append(style);
  const mount = document.getElementById('control-mount') || document.querySelector('main') || document.body;
  const section = document.createElement('section'); section.className='sr-control'; section.setAttribute('aria-label','稳航总开关');
  section.innerHTML = `<h2>稳航总开关 · <span id="sr-actual">读取中</span></h2><p id="sr-effective" role="status" aria-live="polite">正在读取已保存的运行状态…</p><p id="sr-protection"></p><div class="sr-actions"><button id="sr-start" class="sr-primary">预览并启动</button><button id="sr-pause">暂停工作进程，保留规则</button><button id="sr-target">核对下次启动目标…</button><button id="sr-release" class="sr-danger">停用并交还 Clash…</button></div><label><input type="checkbox" id="sr-login">登录时启动工作进程（独立保存；已暂停时不会因登录重新接管，轻量控制页面可保留）</label><p id="sr-error" class="sr-error" role="alert"></p><div id="sr-preview" class="sr-preview" tabindex="-1" role="region" aria-label="运行操作预览" hidden></div>`;
  mount.prepend(section);
  const q = x => section.querySelector(x);
  let busy=false, state=null, plan=null, showingRunning=null, localError='';
  const names={running:'工作进程运行中',paused:'已暂停',released:'上次已交还 Clash',starting:'正在启动',stopping:'正在停止',releasing:'正在交还 Clash',failed:'需要处理'};
  async function request(path,data){
    const r=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-SteadyRoute':'1'}:{},body:data?JSON.stringify(data):undefined,cache:'no-store'});
    const value=await r.json(); if(!r.ok)throw Error(value.error||'操作没有完成');return value;
  }
  function disabled(value){busy=value; section.querySelectorAll('button,input').forEach(x=>x.disabled=value);}
  function render(s){
    state=s; q('#sr-actual').textContent=names[s.actual]||'未核实';q('#sr-login').checked=s.login_start;
    q('#sr-effective').textContent=`已保存：${s.desired_running?'允许运行':'保持停用'}。实际：${s.worker_running?'工作进程正在运行':'工作进程未运行'}；轻量控制页面正在运行。`;
    q('#sr-protection').textContent=s.protection;q('#sr-error').textContent=localError||s.error||'';
    q('#sr-start').disabled=busy||s.worker_running;q('#sr-target').disabled=busy||s.worker_running;
    q('#sr-pause').disabled=busy||(!s.worker_running&&s.actual==='paused');
    if(showingRunning!==null && showingRunning!==s.worker_running && location.pathname!='/control')location.reload();
    showingRunning=s.worker_running;
  }
  async function update(){if(busy)return;try{render(await request('/api/control'));}catch(e){q('#sr-actual').textContent='控制页面离线';q('#sr-effective').textContent='实际运行状态未核实；请使用独立启动或停止入口。';localError=e.message;q('#sr-error').textContent=localError;}}
  function focusPreviewOr(origin){
    if (!q('#sr-preview').hidden) q('#sr-preview').focus();
    else q(origin)?.focus();
  }
  async function preview(action){
    disabled(true);localError='';q('#sr-error').textContent='';
    try{
      plan=await request('/api/control/preview',{action});const release=action==='release';
      q('#sr-preview').innerHTML=`<h3>${release?'交还 Clash 前确认':'启动前核对'}</h3><p>当前订阅：${esc(plan.profile_uid)} · 保存地区：${esc(plan.country||'未设置')} · 专线偏好：${plan.saved_ai_enabled?'启用':'不使用'}</p><p>保存的锁定目标：${esc(plan.locked_node||'未设置')}。${release?'此次将撤销固定出口保护，AI 将按 Clash 原有规则分流。':'缺少原目标或选择漂移时会阻断启动；不会自动换国、换节点或回退 DIRECT。'}</p>${release?`<p>只撤销当前订阅的稳航内容；其他订阅的恢复资料保留。</p><ul>${plan.remove_groups.map(g=>`<li>分组：${esc(g)}</li>`).join('')}<li>受管规则：${plan.remove_rules.length} 条</li></ul><details><summary>查看将撤销的规则</summary><ul>${plan.remove_rules.map(r=>`<li>${esc(r)}</li>`).join('')}</ul></details><label><input type="checkbox" id="sr-confirm-release">我确认撤销当前订阅的稳航接管与固定出口保护，交还 Clash。</label>`:`<ul>${(plan.lines||[]).map(l=>`<li>${esc(l.group)}：${l.after.length} 个符合保存条件的节点</li>`).join('')}</ul>`}<div class="sr-actions"><button id="sr-confirm" class="${release?'sr-danger':'sr-primary'}">${release?'确认停用并交还':'确认启动'}</button><button id="sr-cancel">取消，保持现状</button></div>`;
      q('#sr-preview').hidden=false;q('#sr-preview').scrollIntoView({block:'nearest'});
      q('#sr-confirm').onclick=()=>change(action);q('#sr-cancel').onclick=()=>{plan=null;localError='';q('#sr-preview').hidden=true;if(state)render(state);q(action==='start'?'#sr-start':'#sr-release').focus();};
    }catch(e){localError=e.message;q('#sr-error').textContent=localError;}
    finally{disabled(false);if(state)render(state);focusPreviewOr(action==='start'?'#sr-start':'#sr-release');}
  }
  async function change(action){
    if(action==='release'&&!q('#sr-confirm-release')?.checked){localError='请先勾选确认撤销固定出口保护。';q('#sr-error').textContent=localError;q('#sr-confirm-release').focus();return;}
    disabled(true);localError='';q('#sr-error').textContent='';q('#sr-actual').textContent=action==='start'?'正在启动':'正在停止，请等待在途操作结束';
    try{await request('/api/control/change',{action,fingerprint:plan?.fingerprint,confirmed:action==='release'});location.reload();}
    catch(e){localError=e.message;q('#sr-error').textContent=localError;plan=null;q('#sr-preview').hidden=true;}
    finally{disabled(false);await update();}
  }
  q('#sr-target').onclick=async()=>{
    disabled(true);localError='';
    try{
      const result=await request('/api/control/targets');
      q('#sr-preview').innerHTML=`<h3>下次启动的固定目标</h3><p>保存只更新偏好，工作进程保持停止；不会重载 Clash 或选择节点。当前保存地区：${esc(result.saved_country||'未设置')}，目标：${esc(result.saved_target||'未设置')}。</p>${result.candidates.length?`<label>明确选择目标<select id="sr-new-target"><option value="">请选择，不自动选中</option>${result.candidates.map((n,i)=>`<option value="${i}">${esc(n.country)} · ${esc(n.name)}</option>`).join('')}</select></label><button id="sr-target-preview">预览目标更新</button>`:'<p>当前订阅没有可核对的 AI 家宽叶子节点。请补充原台湾目标或在 Clash 核对订阅；保持停用。</p>'}<button id="sr-target-cancel">取消，保持停用</button>`;
      q('#sr-preview').hidden=false;q('#sr-target-cancel').onclick=()=>{q('#sr-preview').hidden=true;q('#sr-target').focus();};
      if(result.candidates.length)q('#sr-target-preview').onclick=async()=>{
        const index=q('#sr-new-target').value;if(index===''){localError='请明确选择一个目标';q('#sr-error').textContent=localError;return;}
        disabled(true);
        try{
          const selected=result.candidates[Number(index)];
          const p=await request('/api/control/target-preview',{country:selected.country,locked_node:selected.name});
          q('#sr-preview').innerHTML=`<h3>确认更改下次启动目标</h3><p>原来：${esc(p.previous_country||'未设置')} · ${esc(p.previous_target||'未设置')}<br>改为：${esc(p.country)} · ${esc(p.locked_node)}</p><p>这会保存启用专线和固定节点的偏好；保持停用，不自动切换 Clash。之后启动仍会再次核对当前选择。</p><label><input type="checkbox" id="sr-target-confirm">我确认更新保存的地区与固定目标。</label><button id="sr-target-save">保存目标，保持停用</button><button id="sr-target-keep">保留原目标</button>`;
          q('#sr-target-keep').onclick=()=>{q('#sr-preview').hidden=true;q('#sr-target').focus();};
          q('#sr-target-save').onclick=async()=>{
            if(!q('#sr-target-confirm').checked){localError='请明确确认目标更新';q('#sr-error').textContent=localError;return;}
            disabled(true);try{await request('/api/control/target-save',{country:p.country,locked_node:p.locked_node,fingerprint:p.fingerprint,confirmed:true});location.reload();}catch(e){localError=e.message;q('#sr-error').textContent=localError;}finally{disabled(false);}
          };
        }catch(e){localError=e.message;q('#sr-error').textContent=localError;}finally{disabled(false);}
      };
    }catch(e){localError=e.message;q('#sr-error').textContent=localError;}finally{disabled(false);if(state)render(state);focusPreviewOr('#sr-target');}
  };
  q('#sr-start').onclick=()=>preview('start');q('#sr-pause').onclick=()=>change('pause');q('#sr-release').onclick=()=>preview('release');
  q('#sr-login').onchange=async()=>{const enabled=q('#sr-login').checked;localError='';disabled(true);try{render(await request('/api/control/login',{enabled}));}catch(e){localError=e.message;q('#sr-error').textContent=localError;}finally{disabled(false);await update();q('#sr-login').focus();}};
  update();setInterval(update,3000);
})();
