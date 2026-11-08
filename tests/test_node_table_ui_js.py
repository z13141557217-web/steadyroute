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
assert(html.includes('恢复验证中'));
assert(html.includes('恢复进度 3/20'));
assert(!html.includes('<img'));
assert.equal((html.match(/<td[ >]/g)||[]).length,6);
assert(!html.includes('aria-controls='));
expandedNode=r.name;
assert(rowHtml(r,{policy:{min_samples:12}}).includes('aria-controls="'+esc(nodeDetailId(r.name))+'"'));
assert(nodeDetailHtml(r,{}).includes('id="'+esc(nodeDetailId(r.name))+'"'));
assert(!latestProbeCell({latest_probe:{status:'timeout',observed_at:995,stale_at:1100},last_success_delay_ms:999}).includes('999'));
assert(nodeDetailHtml(r,{}).includes('&lt;img'));
assert(availabilityCell(null,3).includes('—'));
assert(!availabilityCell(null,3).replace(/<[^>]*>/g,'').includes('0%'));
'''
        result = subprocess.run(["node", "-e", program],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
