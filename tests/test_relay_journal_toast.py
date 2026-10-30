"""同类中转日志错误合并，其他通知不受影响。"""
import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent

class RelayJournalToastTests(unittest.TestCase):
    def test_deduplication_and_expiry(self):
        js = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('renderer/app.js', 'utf8');
const code = source.slice(source.indexOf('function showToast('), source.indexOf('const FILE_HISTORY_KEY', source.indexOf('function showToast(')));
const container = {children:[], appendChild(x){this.children.push(x);x.parentNode=this;}, removeChild(x){this.children.splice(this.children.indexOf(x),1);x.parentNode=null;}};
const timers=[];
const context={document:{getElementById:()=>container,createElement:()=>({dataset:{}, body:{}, close:{addEventListener(){}}, set innerHTML(v){this.html=v}, querySelector(q){return q==='.toast-body'?this.body:this.close;}})},setTimeout:f=>timers.push(f)};
vm.createContext(context);vm.runInContext(code, context);
context.showToast('error','relay','first','journal');
context.showToast('error','relay','second','journal');
assert.equal(container.children.length,1);assert.equal(container.children[0].body.textContent,'second');
context.showToast('error','other','other');assert.equal(container.children.length,2);
timers[0]();context.showToast('error','relay','again','journal');assert.equal(container.children.length,2);
"""
        result = subprocess.run(['node', '-e', js], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RelayJournalToastTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()

if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
