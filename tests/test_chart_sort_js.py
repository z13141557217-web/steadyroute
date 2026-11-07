import pathlib
import re
import shutil
import subprocess
import unittest

PAGE = pathlib.Path(__file__).resolve().parents[1] / "src/steadyroute/dashboard.html"


@unittest.skipUnless(shutil.which("node"), "Node is optional for frontend regression checks")
class ChartSortTests(unittest.TestCase):
    def test_chart_boundaries_and_availability_sort(self):
        text = PAGE.read_text()
        segments = text[text.index("function chartSegments("):text.index("function makeChart(")]
        sorting = text[text.index("function allRows("):text.index("/* Region order")]
        program = """
const assert = require('assert');
let nodeSort = 'recent';
const groupRegion = g => g.region;
const regionOrder = d => ['TW'];
const sevClass = s => s;
""" + segments + sorting + """
const point = (t,ms,node='a') => [t,ms,'probe',node];
assert.equal(chartSegments([point(0,50),point(5,null),point(10,70)]).length,2);
assert.equal(chartSegments([point(0,50),point(5,70,'b')]).length,2);
assert.equal(chartSegments([point(0,50),point(30,70)]).length,2);
assert.equal(chartSegments([point(0,50),point(5,70)]).length,1);
const data={groups:[{region:'TW',current:'low',candidates:['low','high','warm']}],nodes:{
 low:{window_samples:50,window_success:.68,long_availability:.68},
 high:{window_samples:50,window_success:.97,long_availability:.97},
 warm:{window_samples:1,window_success:1,long_availability:1}
}};
assert.deepEqual(allRows(data).map(x=>x.name),['high','low','warm']);
nodeSort='long';
assert.deepEqual(allRows(data).map(x=>x.name),['warm','high','low']);
nodeSort='role';
assert.equal(allRows(data)[0].name,'low');
"""
        result = subprocess.run(["node", "-e", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_embedded_scripts_parse(self):
        for script in re.findall(r"<script>(.*?)</script>", PAGE.read_text(), re.S):
            result = subprocess.run(["node", "-e", "new Function(process.argv[1])", script],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
