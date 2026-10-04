"""Interactive preview: the real dashboard.html replaying the recorded end-to-end run, and the
real settings.html on a simulated backend built from responses recorded in the same run."""
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src" / "steadyroute"
OUT = HERE / "interactive"
VERSION = "v" + (HERE.parents[1] / "VERSION").read_text(encoding="utf-8").strip()
OUT.mkdir(exist_ok=True)
REC = json.load(open(str(HERE / "work" / "recording.json"), encoding="utf-8"))

t0 = REC["status"][0]["at"]
snapshots = [{"t": round(s["at"] - t0, 2), "now0": t0, "data": s["data"]} for s in REC["status"]]
markers = [{"t": round(m["at"] - t0, 1), "label": m["label"]} for m in REC["markers"]]
scenario = [{"id": "e2e", "title": VERSION + " 迁移实录：安装 → 确认迁移 → 台湾节点故障", "duration": int(snapshots[-1]["t"]) + 1,
             "snapshots": snapshots, "markers": markers}]


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


BAR_CSS = """<style>
.pv { position: fixed; left: 50%; transform: translateX(-50%); bottom: calc(12px + env(safe-area-inset-bottom, 0px)); z-index: 40;
  width: min(980px, calc(100% - 24px)); display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 9px 12px;
  background: var(--surface); border: 1px solid var(--hair-2); border-radius: 14px; box-shadow: 0 14px 40px rgba(0,0,0,.25);
  color: var(--ink-3); font-size: 12px; }
.pv .tag { color: var(--accent); background: var(--accent-soft); border-radius: 999px; padding: 2px 9px; white-space: nowrap; font-weight: 600; }
.pv select, .pv button { font: inherit; font-size: 12px; color: var(--ink); background: var(--surface-2); border: 1px solid var(--hair); border-radius: 999px; padding: 4px 10px; cursor: pointer; max-width: 100%; }
.pv button[aria-pressed="true"] { background: var(--ink); color: var(--page); border-color: var(--ink); }
.pv input[type=range] { flex: 1 1 160px; accent-color: var(--accent); min-width: 120px; }
.pv .clock { color: var(--ink); white-space: nowrap; font-family: var(--mono); }
.pv .script { flex-basis: 100%; color: var(--ink-2); }
.pv a { font-weight: 600; }
body { padding-bottom: 130px; }
.pv-toast { position: fixed; left: 50%; top: 76px; transform: translateX(-50%); z-index: 90; padding: 9px 14px; border-radius: 10px;
  background: var(--ink); color: var(--page); font-size: 13px; box-shadow: 0 10px 30px rgba(0,0,0,.25); }
</style>"""

TOAST_JS = """<script>(function(){
  var toast=document.createElement('div');toast.className='pv-toast';toast.setAttribute('role','status');toast.hidden=true;
  document.addEventListener('DOMContentLoaded',function(){document.body.appendChild(toast)});var timer=null;
  window.__pvToast=function(text){toast.textContent=text;toast.hidden=false;clearTimeout(timer);timer=setTimeout(function(){toast.hidden=true},2600)};
  document.addEventListener('click',function(e){var a=e.target.closest('a[href^="#/"]');if(!a)return;e.preventDefault();
    window.__pvToast('预览只包含看板与设置页；其他页面的改动见数据页')});
})();</script>"""


def links(body):
    body = body.replace('href="/settings"', 'href="settings.html"')
    body = body.replace('href="/"', 'href="index.html"')
    return re.sub(r'href="/(?!/)', 'href="#/', body)


# ------------------------------------------------------------------ dashboard replay
dash = (SRC / "dashboard.html").read_text(encoding="utf-8")
head = dash[dash.index("<head>") + 6:dash.index("</head>")]
body = dash[dash.index("<body>") + 6:dash.index("</body>")]
head = re.sub(r"<title>.*?</title>", "<title>稳航 %s 预览</title>" % VERSION, head)
body = links(body).replace("`<a href=\"#/guide#${t.anchor || ''}\">", "`<a href=\"#/guide#${t.anchor || ''}\">")
shim = """<script>(function(){window.__replay={snap:null,now:0};window.fetch=function(){var s=window.__replay.snap;return Promise.resolve({ok:!!s,status:s?200:503,json:function(){return Promise.resolve(s)}})};var real=Date.now.bind(Date);Date.now=function(){return window.__replay.now?window.__replay.now*1000:real()};})();</script>"""
bar = """<div class="pv" role="region" aria-label="预览控制">
  <span class="tag">预览 · v0.5.1 · 端到端实录（模拟 Clash 服务模式）</span>
  <button type="button" id="pv-play">暂停</button>
  <button type="button" id="pv-1x" aria-pressed="true">1×</button>
  <button type="button" id="pv-10x" aria-pressed="false">10×</button>
  <input type="range" id="pv-scrub" min="0" max="200" step="1" value="0" aria-label="时间">
  <span class="clock" id="pv-clock">第 0 秒</span>
  <a href="settings.html">打开设置页 →</a>
  <span class="script" id="pv-script"></span>
</div>"""
controller = """<script>(function(){
  var SC=JSON.parse(document.getElementById('pv-data').textContent),cur=SC[0];
  var play=document.getElementById('pv-play'),scrub=document.getElementById('pv-scrub'),clock=document.getElementById('pv-clock'),script=document.getElementById('pv-script');
  var t=0,playing=true,speed=1,last=performance.now(),lastSnap=null,lastApply=0;
  function snapAt(x){var b=null;for(var i=0;i<cur.snapshots.length;i++){if(cur.snapshots[i].t<=x)b=cur.snapshots[i];else break}return b||cur.snapshots[0]}
  function apply(force){
    var sn=snapAt(t);window.__replay.snap=sn.data;window.__replay.now=sn.now0+t;
    if((force||sn!==lastSnap)&&typeof refresh==='function'){lastSnap=sn;refresh()}
    else if(typeof snapshot!=='undefined'&&snapshot){renderHead(snapshot);renderRoutes(snapshot)}
    clock.textContent='第 '+Math.round(t)+' 秒';scrub.value=String(Math.round(t));
    var past=cur.markers.filter(function(m){return m.t<=t}).pop(),next=cur.markers.filter(function(m){return m.t>t})[0];
    script.textContent=(past?('第 '+Math.round(past.t)+' 秒：'+past.label):'')+(next?('　·　第 '+Math.round(next.t)+' 秒将发生：'+next.label):'　·　实录结束');
  }
  function tick(ts){var dt=(ts-last)/1000;last=ts;if(playing){t=Math.min(cur.duration,t+dt*speed);if(t>=cur.duration)setPlaying(false)}
    window.__replay.now=cur.snapshots[0].now0+t;if(ts-lastApply>250){lastApply=ts;apply(false)}requestAnimationFrame(tick)}
  function setPlaying(v){playing=v;play.textContent=v?'暂停':'播放'}
  function setSpeed(v){speed=v;document.getElementById('pv-1x').setAttribute('aria-pressed',String(v===1));document.getElementById('pv-10x').setAttribute('aria-pressed',String(v===10))}
  play.addEventListener('click',function(){if(!playing&&t>=cur.duration)t=0;setPlaying(!playing)});
  document.getElementById('pv-1x').addEventListener('click',function(){setSpeed(1)});
  document.getElementById('pv-10x').addEventListener('click',function(){setSpeed(10)});
  scrub.addEventListener('input',function(){t=Number(scrub.value);lastSnap=null;apply(true)});
  scrub.max=String(cur.duration);apply(true);requestAnimationFrame(tick);
})();</script>"""
page = head + BAR_CSS + shim + TOAST_JS + body.replace("<script>\n'use strict';", bar.replace("v0.5.1", VERSION) + "\n<script>\n'use strict';", 1) + \
    '<script type="application/json" id="pv-data">' + dump(scenario) + "</script>\n" + controller
(OUT / "index.html").write_text(page, encoding="utf-8")

# ------------------------------------------------------------------ settings on a simulated backend
settings = (SRC / "settings.html").read_text(encoding="utf-8")
css = (SRC / "pages.css").read_text(encoding="utf-8")
shead = settings[settings.index("<head>") + 6:settings.index("</head>")]
sbody = settings[settings.index("<body>") + 6:settings.index("</body>")]
shead = shead.replace('<link rel="stylesheet" href="/assets/pages.css">', "<style>\n" + css + "\n</style>")
shead = re.sub(r"<title>.*?</title>", "<title>稳航 %s 设置预览</title>" % VERSION, shead)
sbody = links(sbody)
last_status = json.loads(json.dumps(REC["status"][-1]["data"]))
last_status["service"].pop("stale_at", None)
import sys
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))
import ai_line as AL  # noqa: E402
import ai_rules as AR  # noqa: E402
import regions as RG  # noqa: E402
import world as W  # noqa: E402
api = {k: REC["api"][k] for k in ("settings_before", "settings_after", "check_before", "check_after", "check_off")}
api["status"] = last_status
api["members"] = {code: AL.members(code, W.NODES) for code in ("TW", "JP", "US", "HK", "SG")}
api["labels"] = {code: RG.label(code) for code in api["members"]}
api["unsupported"] = AL.AI_UNSUPPORTED
api["old_defs"] = {"AI 台湾家宽线路": W.TW[:3], "香港家宽自动备援": W.HK[:2]}
api["discovery"] = ["SteadyRoute 发现·台湾家宽", "SteadyRoute 发现·香港家宽"]
api["samples"] = {g: AR.build(g)[0][:8] for g in ("AI 家宽专线", "AI 台湾家宽线路")}
api["breakdown"] = [b["source"] for b in AR.build("x")[1]]
api["base_rules"] = len(AR.build("x")[0])
api["breakdown_counts"] = [b["count"] for b in AR.build("x")[1]][:4]
api["rules_full"] = {g: AR.build(g)[0] for g in ("AI 家宽专线", "AI 台湾家宽线路")}
api["group_defs"] = {code: AL.group_definition("", code, True) for code in api["members"]}
api["managed_defs"] = {code: AL.group_definition("", code, False) for code in api["members"]}
api["files"] = {"groups_file": "GgroupE2E.yaml", "rules_file": "RrulesE2E.yaml", "runtime_file": "clash-verge.yaml"}
sbar = """<div class="pv" role="region" aria-label="预览控制">
  <span class="tag">预览 · 设置页 · 模拟后端（订阅与规则取自端到端运行）</span>
  <span id="pv-state"></span>
  <button type="button" id="pv-reset">恢复到迁移前</button>
  <a href="index.html">← 看板实录</a>
  <span class="script">所有操作都可试：升级或保持旧线路、选择出口国家、添加 / 移除网站、开关分组的自动切换、高级信息里的立即同步（预览不联网，会演示失败后的重试）。改动只在本预览内生效。</span>
</div>"""
backend = """<script>(function(){
  var API=JSON.parse(document.getElementById('pv-api').textContent);
  var KEY='sr-v051-preview-2';
  function clone(v){return JSON.parse(JSON.stringify(v))}
  function fresh(){return {phase:'before',ai:{enabled:false,country:null,group:'AI 家宽专线'},managed:[],manual:[],exclude:[],on:{},
    applied:{groups:[],rules:0,at:null},clash:clone(API.old_defs),taken:[]}}
  var S=fresh();try{var saved=JSON.parse(sessionStorage.getItem(KEY)||'null');if(saved)S=saved}catch(e){}
  function save(){try{sessionStorage.setItem(KEY,JSON.stringify(S))}catch(e){}label()}
  function lab(c){return API.labels[c]||c}
  function label(){var el=document.getElementById('pv-state');if(!el)return;
    el.textContent=(S.phase==='before'?'当前：迁移前':S.phase==='dismissed'?'当前：未迁移':'当前：已迁移')+(S.ai.enabled?' · AI 专线 '+lab(S.ai.country):' · AI 专线未启用')}
  function wanted(c){var l=[];if(c.ai.enabled)l.push({group:c.ai.group,country:c.ai.country,ai:true});
    c.managed.forEach(function(m){l.push({group:m.group,country:m.country,ai:false})});return l}
  function config(){return {ai:clone(S.ai),managed:clone(S.managed),manual:S.manual.slice()}}
  function next(body){
    var c=config(),migrating=false;
    if(body.migration==='accept'){if(S.phase!=='before')throw '没有待迁移的旧版线路';migrating=true;
      c.ai={enabled:true,country:'TW',group:'AI 台湾家宽线路'};c.managed=[{group:'香港家宽自动备援',country:'HK'}]}
    if(body.ai_line){c.ai.enabled=!!body.ai_line.enabled;if(body.ai_line.country)c.ai.country=body.ai_line.country}
    if(body.manual)c.manual=body.manual.slice();
    if(c.ai.enabled&&API.unsupported[c.ai.country])throw API.unsupported[c.ai.country]+'不在 ChatGPT / Claude 的服务地区内，不能用作 AI 专线';
    return {c:c,migrating:migrating}}
  function plan(n){
    var c=n.c,lines=wanted(c),names=lines.map(function(l){return l.group});
    var same=JSON.stringify([c.ai,c.managed,c.manual])===JSON.stringify([S.ai,S.managed,S.manual]);
    if(same&&!n.migrating)return {clash_change:false};
    if(!lines.length)return {clash_change:true,action:'disable',remove_groups:S.applied.groups.slice(),remove_rules:S.applied.rules,
      remove_rule_lines:(S.ai.enabled?(API.rules_full[S.ai.group]||[]).concat(S.manual.map(function(d){return 'DOMAIN-SUFFIX,'+d+','+S.ai.group})):[]),
      restore:S.taken.length,restore_groups:S.taken.slice(),profile_uid:API.settings_after.applied.profile_uid};
    var rules=c.ai.enabled?API.base_rules+c.manual.length:0;
    return {clash_change:true,action:'apply',profile_uid:API.settings_after.applied.profile_uid,
      lines:lines.map(function(l){var before=S.clash[l.group]||null,after=API.members[l.country]||[];
        return {group:l.group,country:l.country,country_label:lab(l.country),ai:l.ai,exists:!!before,before:before||[],after:after,
          added:after.filter(function(x){return !(before||[]).includes(x)}),removed:(before||[]).filter(function(x){return !after.includes(x)}),ai_unsupported:false}}),
      removed_legacy:n.migrating?['AI 台湾家宽线路','SteadyRoute 发现·台湾家宽','香港家宽自动备援','SteadyRoute 发现·香港家宽']:[],
      restored:S.applied.groups.filter(function(g){return !names.includes(g)&&S.taken.includes(g)}),
      removed_rules:(function(){var now=S.ai.enabled?(API.rules_full[S.ai.group]||[]).concat(S.manual.map(function(d){return 'DOMAIN-SUFFIX,'+d+','+S.ai.group})):[];
        var next=c.ai.enabled?(API.rules_full[c.ai.group]||[]).concat(c.manual.map(function(d){return 'DOMAIN-SUFFIX,'+d+','+c.ai.group})):[];
        return S.applied.groups.length?now.filter(function(r){return next.indexOf(r)<0}):[]})(),
      rule_count:rules,rules:c.ai.enabled?(API.rules_full[c.ai.group]||[]).concat(c.manual.map(function(d){return 'DOMAIN-SUFFIX,'+d+','+c.ai.group})):[],
      groups:lines.map(function(l){var g=clone((l.ai?API.group_defs:API.managed_defs)[l.country]);g.name=l.group;return g}),
      groups_file:API.files.groups_file,rules_file:API.files.rules_file,runtime_file:API.files.runtime_file,
      rule_breakdown:API.breakdown.map(function(src,i){return {source:src,count:c.ai.enabled?API.breakdown_counts.concat([c.manual.length])[i]:0}}),dropped:[]}}
  function commit(n,p){
    var c=n.c,names=wanted(c).map(function(l){return l.group});
    if(p.action==='disable'){S.applied.groups.forEach(function(g){delete S.clash[g]});S.taken.forEach(function(g){S.clash[g]=clone(API.old_defs[g])});S.taken=[]}
    else{
      if(n.migrating){S.taken=['AI 台湾家宽线路','香港家宽自动备援'];S.phase='after'}
      S.applied.groups.forEach(function(g){if(!names.includes(g)){if(S.taken.includes(g)){S.clash[g]=clone(API.old_defs[g]);S.taken=S.taken.filter(function(x){return x!==g})}else delete S.clash[g]}});
      wanted(c).forEach(function(l){S.clash[l.group]=clone(API.members[l.country]||[])})}
    S.ai=c.ai;S.managed=c.managed;S.manual=c.manual;
    S.applied={groups:p.action==='disable'?[]:names,rules:p.rule_count||0,at:Date.now()/1000};
    return p.action==='disable'?{clash_change:true,action:'disable'}:{clash_change:true,action:'apply',groups:names,rule_count:p.rule_count,dropped:[]}}
  function settingsSnap(){
    var s=clone(S.phase==='after'?API.settings_after:API.settings_before);
    s.migration=S.phase==='before'?API.settings_before.migration:null;
    s.ai_line={enabled:S.ai.enabled,country:S.ai.country,group_name:S.ai.group};
    s.managed_lines=S.managed.map(function(m){return {group_name:m.group,country:m.country}});
    s.manual=S.manual.slice();s.exclude_groups=S.exclude.slice();
    s.applied={at:S.applied.at,groups:S.applied.groups.slice(),dropped:[],profile_uid:API.settings_after.applied.profile_uid};
    s.applied_rule_count=S.applied.rules;
    var rows=clone(API.settings_before.group_details||[]);
    S.applied.groups.forEach(function(g){if(!rows.some(function(r){return r.name===g})){var line=wanted(config()).filter(function(l){return l.group===g})[0];
      if(line)rows.unshift({name:g,current:(S.clash[g]||[])[0],country:line.country,country_label:lab(line.country),residential:(API.members[line.country]||[]).length,status:'switching'})}});
    rows.forEach(function(r){if(S.clash[r.name]&&API.old_defs[r.name]===undefined){}
      var line=wanted(config()).filter(function(l){return l.group===r.name})[0];
      if(line){r.country=line.country;r.country_label=lab(line.country);r.residential=(API.members[line.country]||[]).length;r.current=(S.clash[r.name]||[])[0]}
      var orig=r.status==='excluded'?false:true, want=(r.name in S.on)?S.on[r.name]:(line?true:orig);
      if(!want)r.status='excluded';else if(r.status==='excluded')r.status=(r.residential?(r.country?'switching':'unknown'):'no_residential')});
    s.group_details=rows;
    if(S.sync){s.rules_source.checked_at=S.sync.at;s.rules_source.next_check_at=S.sync.next;s.rules_source.last_error=S.sync.error;s.rules_source.failures=S.sync.failures}
    return s}
  function checkSnap(){
    if(!S.ai.enabled)return S.phase==='before'?API.check_before:API.check_off;
    var node=(S.clash[S.ai.group]||[])[0],r=clone(API.check_after);
    if(S.ai.group==='AI 台湾家宽线路'&&S.ai.country==='TW')return r;
    r.line_group=S.ai.group;r.line_country=lab(S.ai.country);
    r.rows.forEach(function(x){x.group=S.ai.group;x.chain=[S.ai.group,node];x.exit=node;x.exit_country=lab(S.ai.country);x.ok=true});return r}
  function reply(status,data){return Promise.resolve({ok:status<300,status:status,json:function(){return Promise.resolve(clone(data))}})}
  window.fetch=function(path,opts){
    opts=opts||{};var method=(opts.method||'GET').toUpperCase();
    if(path==='/api/status')return reply(200,API.status);
    if(path==='/api/settings')return reply(200,settingsSnap());
    if(path==='/api/ai-check')return reply(200,checkSnap());
    var body={};try{body=JSON.parse(opts.body||'{}')}catch(e){}
    if(method==='POST'&&path==='/api/settings/sync'){
      var t=Date.now()/1000,f=((S.sync&&S.sync.failures)||0)+1,d=new Date(t*1000);
      var stamp=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
      S.sync={at:t,failures:f,next:t+(f<3?6*3600:86400),error:'预览环境无法联网（'+stamp+'）'};save();
      return reply(200,{ok:false,changed:false,error:S.sync.error,last_change:null})}
    if(method==='POST'&&(path==='/api/settings/preview'||path==='/api/settings/apply')){
      var n,p;try{n=next(body);p=plan(n)}catch(err){return reply(409,{error:String(err)})}
      if(path==='/api/settings/preview')return reply(200,p);
      if(body.takeover)S.on[body.takeover.group]=!!body.takeover.on;
      if(body.migration==='dismiss'&&S.phase==='before')S.phase='dismissed';
      var result=p.clash_change?commit(n,p):{clash_change:false};save();return reply(200,result)}
    return reply(404,{error:'not_found'})};
  document.addEventListener('DOMContentLoaded',function(){label();
    document.getElementById('pv-reset').addEventListener('click',function(){S=fresh();save();location.reload()})});
})();</script>"""
spage = '<!doctype html>\n<html lang="zh-CN">\n<head>\n' + shead + BAR_CSS + TOAST_JS + \
    '<script type="application/json" id="pv-api">' + dump(api) + "</script>\n" + backend + "</head>\n<body>\n" + \
    sbody.replace("<script>\n'use strict';", sbar + "\n<script>\n'use strict';", 1) + "</body>\n</html>\n"
(OUT / "settings.html").write_text(spage, encoding="utf-8")
for name in ("index.html", "settings.html"):
    print(name, (OUT / name).stat().st_size)
