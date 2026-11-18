"""提示策略：成功不弹，失败只留 3 秒，同类重复只在右下角角标里计数。"""
import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent

PRELUDE = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('renderer/notify.js', 'utf8');
const code = source.slice(source.indexOf('const TOAST_DURATION_MS')).replace(/export /g, '');
function El(tag) {
  this.tagName = tag; this.children = []; this.dataset = {}; this.className = '';
  this.textContent = ''; this.title = ''; this.parentNode = null; this.listeners = [];
}
El.prototype.appendChild = function (child) { child.parentNode = this; this.children.push(child); return child; };
El.prototype.removeChild = function (child) {
  const index = this.children.indexOf(child);
  if (index >= 0) this.children.splice(index, 1);
  child.parentNode = null;
  return child;
};
El.prototype.addEventListener = function (type, fn) { this.listeners.push([type, fn]); };
const container = new El('div');
const timers = [];
const context = {
  document: { getElementById: () => container, createElement: tag => new El(tag) },
  Date,
  setTimeout: (fn, ms) => { timers.push({ fn, ms, active: true }); return timers.length; },
  clearTimeout: id => { const timer = timers[id - 1]; if (timer) timer.active = false; },
};
vm.createContext(context);
vm.runInContext(code, context);
const toasts = () => container.children.filter(el => el.className.indexOf('toast ') === 0);
const badge = () => container.children.find(el => el.className === 'toast-badge');
"""


class RelayJournalToastTests(unittest.TestCase):
    def _run(self, body):
        result = subprocess.run(['node', '-e', PRELUDE + body], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_success_silent_and_three_second_failure(self):
        self._run(r"""
context.showToast('success', 'op', 'done');
assert.equal(toasts().length, 0);

context.showToast('error', 'relay', 'first', 'journal');
assert.equal(toasts().length, 1);
assert.equal(timers[0].ms, 3000);

// 同类重复不再生成卡片，也不覆盖原卡片文案，只累加角标。
context.showToast('error', 'relay', 'second', 'journal');
assert.equal(toasts().length, 1);
assert.equal(toasts()[0].children[1].textContent, 'first');
assert.equal(badge().textContent, '⚠ 1');
assert.ok(badge().title.indexOf('second') >= 0);

// 不同键仍是独立卡片。
context.showToast('error', 'other', 'other');
assert.equal(toasts().length, 2);

timers[0].fn();
assert.equal(toasts().length, 1);
context.showToast('error', 'relay', 'again', 'journal');
assert.equal(toasts().length, 1);
assert.equal(badge().textContent, '⚠ 2');

// 点击角标清零后，同一文案会重新弹一次。
badge().listeners[0][1]();
assert.equal(badge(), undefined);
context.showToast('error', 'relay', 'fresh', 'journal');
assert.equal(toasts().length, 2);
assert.equal(toasts()[1].children[1].textContent, 'fresh');
""")

    def test_reject_events_merge_per_peer(self):
        self._run(r"""
const first = '不下发请求给离线从机 CN3114001670W：心跳已中断 1748.1s';
const second = '不下发请求给离线从机 CN3114001670W：心跳已中断 1742.5s';
assert.equal(context.rejectDedupeKey(first), context.rejectDedupeKey(second));
context.showToast('warning', 'relay', first, context.rejectDedupeKey(first));
context.showToast('warning', 'relay', second, context.rejectDedupeKey(second));
assert.equal(toasts().length, 1);
assert.equal(badge().textContent, '⚠ 1');
const otherPeer = '不下发请求给离线从机 CN3114009999X：心跳已中断 1748.1s';
context.showToast('warning', 'relay', otherPeer, context.rejectDedupeKey(otherPeer));
assert.equal(toasts().length, 2);
const otherReason = '不下发请求给离线从机 CN3114001670W：尚未发现该从机（心跳未到达）';
context.showToast('warning', 'relay', otherReason, context.rejectDedupeKey(otherReason));
assert.equal(toasts().length, 3);
const failure = '心跳写入失败：OSError: [WinError 5] 拒绝访问';
context.showToast('error', 'relay', failure, context.errorDedupeKey(failure));
context.showToast('error', 'relay', failure, context.errorDedupeKey(failure));
const another = '枚举本机设备失败：adb server version mismatch';
context.showToast('error', 'relay', another, context.errorDedupeKey(another));
assert.equal(toasts().length, 5);
assert.equal(badge().textContent, '⚠ 2');
""")

    def test_warning_type_renders_as_warning_card(self):
        self._run(r"""
context.showToast('warn', '终端查找', '请先选中一个设备窗口');
assert.equal(toasts().length, 1);
assert.equal(toasts()[0].className, 'toast warning');
assert.equal(toasts()[0].children[0].children[0].textContent, '⚠');
""")


def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RelayJournalToastTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
