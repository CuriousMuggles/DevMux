"""中转配置弹窗「按钮行」的布局不变量。

背景（用户实测）：同一个 exe 开两个窗口，中转配置弹窗里那排按钮长得不一样 ——
主控那个实例的「保存并生效」是正常的单行按钮，从机那个被压成两行。

根因：这一行是
    复选框 / 刷新状态 / 保存并生效 / 状态说明
其中「状态说明」的文本长度由运行时状态决定（「未启用」三个字 vs 一长串运行提示）。
它带着 ``.tool-label`` 的 ``white-space: nowrap`` 又不允许收缩，于是文本一长就把整行
顶到溢出，flex 只能回头压按钮：``保存并生效`` 被压到 ``min-width: 96px``，
去掉左右各 18px 内边距只剩 60px，于是折成两行、行高翻倍。

这个测试把真实页面里的那一行复制若干份，只改状态说明的长短，然后量按钮的矩形：
长短不同，按钮的尺寸必须一模一样；并且这一行永远不许溢出。

反向验证：把 ``.relay-config-dialog .tool-label`` 的 ``flex/min-width/overflow`` 去掉、
恢复 ``#relay-refresh { margin-left: auto }``，前三个用例的保存按钮就从 98x31 变成
96x31 且 ``scrollWidth`` 达到 1315 > 718（溢出），测试如实报错。
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from tests.edge_test_support import EdgeSession

SHORT = '未启用'
TYPICAL = '等待对端：本机为从机，未发现线的主控端；另有 1 条本机旧标识的残留心跳'
LONG = TYPICAL + '（HOST-EXAMPLE-b），已按「本机自己」忽略；状态说明再长也不该改变按钮的大小'
SERVED = TYPICAL + '（正在为对端提供 3 个终端会话：de47c9b2、f62ca3df、emulator-5554）'

PROBE_JS = """
(() => {
  const cases = %(cases)s;
  const dlg = document.querySelector('#relay-config-overlay .relay-config-dialog');
  if (!dlg) return JSON.stringify({error: '找不到中转配置弹窗'});
  const template = document.getElementById('relay-enabled').closest('.setting-row');
  const host = document.createElement('div');
  host.id = '__row_probe_host';
  host.style.cssText = 'position:fixed;left:0;top:0;width:820px;z-index:99999;'
                     + 'background:var(--bg-panel);padding:0';
  document.body.appendChild(host);
  const made = [];
  for (const item of cases) {
    const shell = document.createElement('div');
    shell.className = 'probe-case';
    const frame = dlg.cloneNode(false);          // 只要弹窗外壳，拿到同样的宽度与作用域
    const row = template.cloneNode(true);
    const state = row.querySelector('span.tool-label');
    state.removeAttribute('id');
    state.textContent = item.text;
    state.title = item.text;
    const box = row.querySelector('input[type=checkbox]');
    if (item.enabled) box.setAttribute('checked', 'checked');
    frame.appendChild(row);
    shell.appendChild(frame);
    host.appendChild(shell);
    made.push([item.name, row]);
  }
  const out = [];
  for (const [name, row] of made) {
    const btns = [...row.querySelectorAll('button.tool-btn')];
    const rec = {
      name: name,
      clientWidth: row.clientWidth,
      scrollWidth: row.scrollWidth,
      overflow: row.scrollWidth > row.clientWidth,
      buttons: btns.map(b => {
        const r = b.getBoundingClientRect();
        const c = getComputedStyle(b);
        return {
          text: (b.textContent || '').trim(),
          x: +r.left.toFixed(1),
          w: +r.width.toFixed(1),
          h: +r.height.toFixed(1),
          wrap: +r.height.toFixed(1) > parseFloat(c.lineHeight) * 1.6,
        };
      }),
      stateWidth: +row.querySelector('span.tool-label').getBoundingClientRect().width.toFixed(1),
      stateClip: (() => {
        const s = row.querySelector('span.tool-label');
        return { scroll: s.scrollWidth, client: s.clientWidth };
      })(),
    };
    out.push(rec);
  }
  host.remove();
  return JSON.stringify({cases: out});
})()
"""

REQUIRED_CSS = [
    # 状态说明必须是可收缩的那个，否则长文本会把按钮挤变形
    ('.relay-config-dialog .setting-row > .tool-label', 'flex: 1 1 auto'),
    ('.relay-config-dialog .setting-row > .tool-label', 'min-width: 0'),
    ('.relay-config-dialog .setting-row > .tool-label', 'text-overflow: ellipsis'),
    # 按钮按内容定宽、永不折行
    ('.relay-config-dialog .tool-btn', 'flex: 0 0 auto'),
    ('.tool-btn', 'white-space: nowrap'),
]


def _css_rules(css):
    """把样式表切成 {选择器: 规则体}，选择器列表按逗号拆开。

    不能简单地 ``css.find('.tool-btn {')``：那会先命中 ``.terminal-find-actions
    .tool-btn {`` 这种前缀更长的选择器，量到别的规则上去。
    """
    rules = {}
    # 注释里带中文说明，且夹在规则之间：不先去掉，选择器文本会被前一条注释污染
    # （量到的 key 变成 "*/ .relay-config-dialog ..."），查不到就误报 FAIL。
    css = re.sub(r'/\*.*?\*/', ' ', css, flags=re.S)
    for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        body = match.group(2)
        for sel in match.group(1).split(','):
            sel = ' '.join(sel.split())
            if sel:
                rules.setdefault(sel, body)
    return rules


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _edge_executable():
    candidates = [
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''),
                     'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('PROGRAMFILES', ''),
                     'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''),
                     'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    ]
    return next((p for p in candidates if p and os.path.isfile(p)), '')


def _start_edge(edge, profile_dir, port, url):
    return EdgeSession.start(edge, [
        '--headless=old', '--no-first-run', '--no-default-browser-check',
        '--hide-scrollbars', '--allow-file-access-from-files', '--remote-allow-origins=*',
        '--window-size=860,760', url,
    ], profile_dir)


def _measure_buttons(ws_url, cases):
    from websockets.sync.client import connect
    with connect(ws_url, max_size=8 * 1024 * 1024, proxy=None, open_timeout=10) as ws:
        counter = 0

        def cmd(method, params=None):
            nonlocal counter
            counter += 1
            ws.send(json.dumps({'id': counter, 'method': method, 'params': params or {}}))
            while True:
                msg = json.loads(ws.recv(timeout=25))
                if msg.get('id') == counter:
                    if msg.get('error'):
                        raise RuntimeError('CDP %s: %s' % (method, msg['error']))
                    return msg

        cmd('Page.enable')
        time.sleep(1.0)
        expr = PROBE_JS % {'cases': json.dumps(cases, ensure_ascii=False)}
        res = cmd('Runtime.evaluate', {'expression': expr, 'returnByValue': True})
        payload = res['result']['result'].get('value')
        if not payload:
            raise RuntimeError('页面内的布局探针没有返回数据')
        data = json.loads(payload)
        if data.get('error'):
            raise RuntimeError(data['error'])
        return data['cases']


def run() -> bool:
    if os.name != 'nt':
        print('  [SKIP] 按钮行布局测试仅支持 Windows Edge')
        return None

    edge = _edge_executable()
    if not edge:
        print('  [FAIL] 找不到 Microsoft Edge，无法量真实渲染')
        return False

    import server as srv

    ok = True
    old_http = srv.HTTP_PORT
    httpd = None
    edge_proc = None
    tag = 'edge-relay-row-%d' % os.getpid()
    profile_dir = os.path.join(os.environ.get('TEMP', '.'), tag)
    try:
        # 1. 样式表里的不变量（先说清楚意图，量不到时也知道该去看哪一条）
        css = open(os.path.join(ROOT, 'renderer', 'style.css'),
                   encoding='utf-8').read()
        rules = _css_rules(css)
        for selector, needle in REQUIRED_CSS:
            body = rules.get(selector, '')
            hit = needle in body
            print('  %s 样式表 %s 含 %s' % ('[OK]' if hit else '[FAIL]', selector, needle))
            ok = ok and hit
        no_auto_margin = '#relay-refresh { margin-left: auto' not in css
        print('  %s 样式表没有会跟 flex-grow 抢空间的 #relay-refresh 自动外边距'
              % ('[OK]' if no_auto_margin else '[FAIL]'))
        ok = ok and no_auto_margin

        # 2. 真实页面里的真实 DOM：同一个 exe 的两个窗口只差状态文本长短，按钮必须一样
        srv.HTTP_PORT = _free_port()
        httpd = srv.start_http_server()
        http_port = httpd.server_address[1]
        url = 'http://127.0.0.1:%d/' % http_port

        cdp_port = _free_port()
        edge_proc = _start_edge(edge, profile_dir, cdp_port, url)
        ws_url = edge_proc.wait_ready(25)

        cases = [
            {'name': '未启用（短）', 'text': SHORT, 'enabled': False},
            {'name': '等待对端（典型）', 'text': TYPICAL, 'enabled': True},
            {'name': '超长说明', 'text': LONG, 'enabled': True},
            {'name': '带被控会话数', 'text': SERVED, 'enabled': True},
        ]
        rows = _measure_buttons(ws_url, cases)

        shapes = set()
        for rec in rows:
            # x 也要进形状：修复前按钮位置会随状态说明长短在「靠左/靠右」之间跳
            shape = tuple((b['text'], b['x'], b['w'], b['h']) for b in rec['buttons'])
            shapes.add(shape)
            print('  %-16s 行宽 %4d/%-4d 溢出=%s 说明 %3d/%-3d  %s'
                  % (rec['name'], rec['clientWidth'], rec['scrollWidth'],
                     rec['overflow'], rec['stateClip']['client'],
                     rec['stateClip']['scroll'],
                     ' | '.join('%s %.0fx%.0f@%.0f' % (b['text'], b['w'], b['h'], b['x'])
                                for b in rec['buttons'])))
            if rec['overflow']:
                print('  [FAIL] %s 这一行溢出了（scrollWidth %d > clientWidth %d）'
                      % (rec['name'], rec['scrollWidth'], rec['clientWidth']))
                ok = False
            for b in rec['buttons']:
                if b['wrap']:
                    print('  [FAIL] %s 的「%s」折成了多行' % (rec['name'], b['text']))
                    ok = False

        if len(shapes) == 1:
            print('  [OK] 4 种状态说明下，按钮的尺寸与位置完全一致：%s'
                  % ' | '.join('%s %.0fx%.0f' % (b['text'], b['w'], b['h'])
                               for b in rows[0]['buttons']))
        else:
            print('  [FAIL] 按钮尺寸/位置随状态说明长短变了（%d 种形状）' % len(shapes))
            for shape in sorted(shapes):
                print('         %s' % (shape,))
            ok = False

        # 3. 长文本由状态说明自己缩（省略号），短文本不该被截断
        short_clip = rows[0]['stateClip']
        long_clip = next(r for r in rows if r['name'] == '超长说明')['stateClip']
        shrink_ok = long_clip['scroll'] > long_clip['client'] and short_clip['scroll'] <= short_clip['client'] + 1
        print('  %s 状态说明自己缩：短文本 %d/%d 不截断，超长文本 %d/%d 省略号截断'
              % ('[OK]' if shrink_ok else '[FAIL]',
                 short_clip['client'], short_clip['scroll'],
                 long_clip['client'], long_clip['scroll']))
        ok = ok and shrink_ok
        return ok
    finally:
        if edge_proc is not None:
            edge_proc.close()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        srv.HTTP_PORT = old_http
        time.sleep(0.2)
        subprocess.run(['cmd', '/c', 'rmdir', '/s', '/q', profile_dir],
                       capture_output=True, errors='replace')


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
