"""同类中转日志错误合并，其他通知不受影响。"""
import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent

class RelayJournalToastTests(unittest.TestCase):
    def test_deduplication_and_expiry(self):
        js = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('renderer/notify.js', 'utf8');
const code = source.slice(source.indexOf('function scheduleToastRemoval(')).replace(/export /g, '');
const container = {children:[], appendChild(x){this.children.push(x);x.parentNode=this;}, removeChild(x){this.children.splice(this.children.indexOf(x),1);x.parentNode=null;}};
const timers=[];
const context={document:{getElementById:()=>container,createElement:()=>({dataset:{}, body:{}, close:{addEventListener(){}}, set innerHTML(v){this.html=v}, querySelector(q){return q==='.toast-body'?this.body:this.close;}})},setTimeout:f=>timers.push(f),clearTimeout:()=>{}};
vm.createContext(context);vm.runInContext(code, context);
context.showToast('error','relay','first','journal');
context.showToast('error','relay','second','journal');
assert.equal(container.children.length,1);assert.equal(container.children[0].body.textContent,'second');
context.showToast('error','other','other');assert.equal(container.children.length,2);
timers[1]();context.showToast('error','relay','again','journal');assert.equal(container.children.length,2);
"""
        result = subprocess.run(['node', '-e', js], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reject_events_merge_per_peer(self):
        js = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('renderer/notify.js', 'utf8');
const code = source.slice(source.indexOf('export function rejectDedupeKey(')).replace(/export /g, '');
const container = {children:[], appendChild(x){this.children.push(x);x.parentNode=this;}, removeChild(x){this.children.splice(this.children.indexOf(x),1);x.parentNode=null;}};
const timers=[];
const context={document:{getElementById:()=>container,createElement:()=>({dataset:{}, body:{}, close:{addEventListener(){}}, set innerHTML(v){this.html=v}, querySelector(q){return q==='.toast-body'?this.body:this.close;}})},setTimeout:f=>timers.push(f),clearTimeout:()=>{}};
vm.createContext(context);vm.runInContext(code, context);
const first='不下发请求给离线从机 CN3114001670W：心跳已中断 1748.1s';
const second='不下发请求给离线从机 CN3114001670W：心跳已中断 1742.5s';
assert.equal(context.rejectDedupeKey(first), context.rejectDedupeKey(second));
context.showToast('warning','relay',first,context.rejectDedupeKey(first));
context.showToast('warning','relay',second,context.rejectDedupeKey(second));
assert.equal(container.children.length,1);
assert.equal(container.children[0].body.textContent,second);
const otherPeer='不下发请求给离线从机 CN3114009999X：心跳已中断 1748.1s';
context.showToast('warning','relay',otherPeer,context.rejectDedupeKey(otherPeer));
assert.equal(container.children.length,2);
const otherReason='不下发请求给离线从机 CN3114001670W：尚未发现该从机（心跳未到达）';
context.showToast('warning','relay',otherReason,context.rejectDedupeKey(otherReason));
assert.equal(container.children.length,3);
const failure='心跳写入失败：OSError: [WinError 5] 拒绝访问';
context.showToast('error','relay',failure,context.errorDedupeKey(failure));
context.showToast('error','relay',failure,context.errorDedupeKey(failure));
assert.equal(container.children.length,4);
const another='枚举本机设备失败：adb server version mismatch';
context.showToast('error','relay',another,context.errorDedupeKey(another));
assert.equal(container.children.length,5);
"""
        result = subprocess.run(['node', '-e', js], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RelayJournalToastTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()

if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
