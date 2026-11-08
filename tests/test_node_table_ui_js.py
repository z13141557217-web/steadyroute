"""User-facing node states, independent filters and escaped details."""
import pathlib
import shutil
import subprocess
import unittest

PAGE = pathlib.Path(__file__).resolve().parents[1] / "src/steadyroute/dashboard.html"


@unittest.skipUnless(shutil.which("node"), "Node is optional for frontend checks")
class NodeTableUiTests(unittest.TestCase):
    def test_filters_and_unknown_stale_success_do_not_change_lifecycle(self):
        source = PAGE.read_text()
        functions = source[source.index("function matches(r,"):source.index("function renderNodes(d)")]
        program = r'''
const assert = require('assert');
let activeFilter='focus',activeRegion='TW',expandedNode=null;
const nowSeconds=()=>1000, age=s=>`${s}秒前`;
const pct=v=>v==null?'—':Math.round(v*100)+'%';
const ms=v=>v==null?'—':v+' ms';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
''' + functions + r'''
assert(matches({region:'TW',current:true,cls:'good'}));
assert(!matches({region:'HK',current:true,cls:'good'}));
assert(!matches({region:'TW',current:false,standby:false,cls:'good'}));
assert(matches({region:'TW',current:false,cls:'good'},'all'));
assert.equal(probePresentation({}).text,'未知');
assert.equal(probePresentation({latest_probe:{status:'success',delay_ms:80,stale_at:900}}).text,'已过期');
const r={name:'long <img src=x onerror=alert(1)>',cls:'warn',node:{
 latest_probe:{status:'success',delay_ms:80,observed_at:995,stale_at:1100},
 lifecycle_title:'节点恢复验证中（3/20）',lifecycle_detail:'仍需成功样本',
 window_samples:40,window_success:.9,long_availability:.98,quality:50,
 last_success_delay_ms:80,last_success_at:995
}};
const html=rowHtml(r,{policy:{min_samples:12}});
assert(html.includes('80 ms'));
assert(html.includes('恢复验证'));
assert(html.includes('恢复验证 3/20'));
assert(!html.includes('<small>'));
assert(rowHtml({...r,node:{...r.node,lifecycle_title:'节点暂停使用（0/20）'}},{}).includes('节点暂停使用'));
for (const node of [{latest_probe:{status:'unavailable'}},{latest_probe:{status:'success',stale:true}},{window_samples:3},{window_samples:40,window_success:null,latest_probe:{status:'success'}},{}]) assert(matches({region:'TW',cls:'good',node}));
assert(html.includes('rate-bar'));
assert(!html.includes('24小时'));
assert(html.includes('节点状态'));
assert(!html.includes('<img'));
assert.equal((html.match(/<td[ >]/g)||[]).length,5);
assert(!html.includes('aria-controls='));
expandedNode=r.name;
assert(rowHtml(r,{policy:{min_samples:12}}).includes('aria-controls="'+esc(nodeDetailId(r.name))+'"'));
assert(nodeDetailHtml(r,{}).includes('id="'+esc(nodeDetailId(r.name))+'"'));
assert(!latestProbeCell({latest_probe:{status:'timeout',observed_at:995,stale_at:1100},last_success_delay_ms:999}).includes('999'));
assert(nodeDetailHtml(r,{}).includes('&lt;img'));
assert(availabilityCell(null,3).includes('样本不足'));
assert(!availabilityCell(null,3).includes('rate-bar'));
assert(availabilityCell(.97,40).includes('width:97%'));
assert(availabilityCell(0,40).includes('0%'));
assert(availabilityCell(null,40).includes('未知'));
assert(nodeDetailHtml(r,{}).includes('24小时检测成功率'));
assert(nodeDetailHtml(r,{}).includes('colspan="5"'));
for(const [status,label] of [['timeout','超时'],['error','错误'],['unavailable','检测不可用']]) {
 const cell=latestProbeCell({latest_probe:{status,stale_at:1100}});
 assert(cell.includes(label)); assert(!cell.includes('<small>'));
}
assert(!availabilityCell(null,3).replace(/<[^>]*>/g,'').includes('0%'));
'''
        result = subprocess.run(["node", "-e", program],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
