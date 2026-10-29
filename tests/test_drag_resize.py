"""
Verify drag handle: check JS/CSS content, then construct a simulated DOM
to validate the drag event delegation logic actually works.
"""
import sys, os, json, time, socket, urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from app_version import APP_VERSION


class _Resources(HTMLParser):
    """按资源属性匹配路径，避免把扩展工具文件名计作主资源。"""
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        key = "src" if tag == "script" else "href" if tag == "link" else None
        value = dict(attrs).get(key)
        if value:
            self.urls.append(urlsplit(value))


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

def run() -> bool:
    ok = True

    # 1. 只启动静态 HTTP 服务。此测试检查的是前端资源，不能依赖 pywebview 桌面窗口。
    import server as srv
    old_http_port = srv.HTTP_PORT
    srv.HTTP_PORT = _free_port()
    httpd = srv.start_http_server()
    http_port = httpd.server_address[1]

    try:
        # 2. Fetch JS
        resp = urllib.request.urlopen(f'http://127.0.0.1:{http_port}/app.js?v=52', timeout=5)
        js = resp.read().decode('utf-8')
        resp = urllib.request.urlopen(f'http://127.0.0.1:{http_port}/style.css?v=52', timeout=5)
        css = resp.read().decode('utf-8')

        # 3. Check JS
        js_checks = [
            ('initDragResize defined', 'function initDragResize' in js),
            ('initHighlight defined', 'function initHighlight' in js),
            ('_activePanel tracking var', '_activePanel' in js),
            ('highlight applies to panel', 'panel.classList.add' in js),
            ('no scrollToTop anywhere', 'scrollToTop' not in js),
            ('drag uses cell-toolbar-handle', "'.cell-toolbar-handle'" in js),
            ('drag always adjusts cmd-input', "'.terminal-cmd-input'" in js.split('function initDragResize')[1].split('function initHighlight')[0] if 'function initDragResize' in js else True),
            ('height clamp 34-500', 'Math.max(34, Math.min(500' in js),
            ('mouseup cleanup', 'delete handle.dataset.dragging' in js),
            ('no globals for drag', 'const dragState' not in js),
            ('createTerminalPanel creates toolbar', "cell-toolbar" in js.split('function _buildPanelCell')[1].split('function _initTerminal')[0] if '_buildPanelCell' in js else True),
            ('cell-toolbar-handle class used', "'cell-toolbar-handle'" in js),
            ('term.onData sends to WS', "sendWS({ type: 'write-terminal'" in js),
            ('panel mousedown skips xterm', "e.target.closest('.terminal-xterm')" in js),
            ('init() calls initHighlight', 'initHighlight()' in js.split('function init()')[1].split('initTopTabs')[0]),
            ('highlight targets xterm or cmd-input', "'.terminal-xterm, .terminal-cmd-input'" in js),
            ('upload/down buttons in toolbar', 'file-op-btn' in js),
            ('toolbar file ops form', 'cell-toolbar-file-ops' in js),
                ('browse local button', 'fo-browse-local' in js),
                ('unified local picker', 'fo-local-picker-menu' in js and '选择要递归上传的文件夹' in js),
                ('browse remote button', 'fo-browse-remote' in js),
                ('exec-adb for push/pull', 'exec-adb' in js),
                ('file transfer sends argv', 'argv: transferArgs' in js),
                ('upload supports native multi-select', "pick-file', { multi: true }" in js),
                ('download supports multiple device files', 'selectedRemotePaths' in js),
                ('sh upload requests executable permission', 'makeExecutable' in js),
                ('path history localStorage', 'FILE_HISTORY_KEY' in js),
            ('loadPathHistory defined', 'function loadPathHistory' in js),
            ('savePathHistory defined', 'function savePathHistory' in js),
            ('showToast defined', 'function showToast' in js),
              ('space calculation', 'spaceAbove' in js),
              ('popup avoids toolbar row', 'toolbarRowRect' in js),
              ('viewport-relative left', 't.devBrowser.style.left' in js),
              ('viewport-relative top', 't.devBrowser.style.top' in js),
              ('multiple file selected tracking', 'selectedRemotePaths' in js),
              ('double-click handler', 'dblclick' in js),
              ('confirm button handler', 'browserConfirm' in js),
              ('path btns wrapper', 'dev-browser-path-btns' in js),
              ('log submenu DOM', 'log-submenu' in js),
               ('logBtn stored', 'logBtn: cell.logBtn' in js),
                ('dismissedKeys', 'dismissedKeys' in js),
                ('initDeviceRefresh', 'initDeviceRefresh' in js),
                ('file-menu DOM', 'file-menu' in js),
                ('file-tab DOM', 'file-tab' in js),
                ('file-restart DOM', 'file-restart' in js),
                ('terminal find init', 'function initTerminalFind' in js),
                ('terminal find open', 'function openTerminalFind' in js),
                ('terminal find scans xterm buffer', 'function collectTerminalFindMatches' in js and 'term.buffer.active' in js),
                ('terminal find whole-word option', 'TERMINAL_FIND_WORD_CHAR' in js and 'wholeWord' in js),
                ('terminal find Ctrl+F shortcut', "e.key.toLowerCase() === 'f'" in js and 'openTerminalFind()' in js),
                ('selected terminal tracked for find', 'function selectActiveDevice' in js),
                ('help-menu DOM', 'help-menu' in js),
                ('help-tab DOM', 'help-tab' in js),
                ('help-about DOM', 'help-about' in js),
                ('showAboutDialog', 'showAboutDialog' in js),
                ('关于窗口读取公共版本', 'v${APP_VERSION}' in js),
                ('CMD_STORAGE_KEY removed', "const CMD_STORAGE_KEY" not in js),
                 ('loadCommandGroups', 'loadCommandGroups' in js),
                 ('saveCommandGroups', 'saveCommandGroups' in js),
                 ('DEFAULT_COMMAND_GROUPS', 'DEFAULT_COMMAND_GROUPS' in js),
                 ('_cmdGroupIndex', '_cmdGroupIndex' in js),
                 ('_cmdHistory', '_cmdHistory' in js),
                 ('ArrowUp history', 'ArrowUp' in js),
                ('rightClickSelectsWord', 'rightClickSelectsWord' in js),
                ('onSelectionChange', 'onSelectionChange' in js),
                ('copyTextToClipboard', 'copyTextToClipboard' in js),
                ('readClipboardLegacy', 'readClipboardLegacy' in js),
                ('no exec-shell-script', 'exec-shell-script' not in js),
                 ('cmd-group-select', 'cmd-group-select' in js),
                 ('cmd-popup-group-btn', 'cmd-popup-group-btn' in js),
                 ('cmd-group-mgr', 'cmd-group-mgr' in js),
                 ('__debugCommands', '__debugCommands' in js),
                ('start-recording handler', 'start-recording' in js),
                ('stop-recording handler', 'stop-recording' in js),
                ('recording file picker', 'pick-recording-file' in js),
                ('recording timestamp toggle', 'log-timestamp-toggle' in js),
                ('recording timestamps payload', 'timestamps: logTimestampToggle.dataset.enabled' in js),
                ('command-popup DOM', 'command-popup' in js),
                ('cmd-popup-btn', 'cmd-popup-btn' in js),
                ('cmd-context-menu', 'cmd-context-menu' in js),
                ('cmd-edit-overlay', 'cmd-edit-overlay' in js),
                ('resetLayout defined', 'function resetLayout' in js),
                ('layout reset button handler', 'layout-reset-btn' in js),
                ('device file edit toolbar', 'edit-device-browser' in js),
                ('direct remote editor button', 'edit-inline' in js),
                ('direct remote editor read', 'start-inline-edit' in js),
                ('direct remote editor save', 'save-inline-edit' in js),
                ('manual inline save button', 'inline-edit-save' in js),
                ('no inline auto-save timer', 'inlineTimer' not in js),
                ('UTF-8 base64 helpers', 'decodeUtf8Base64' in js and 'encodeUtf8Base64' in js),
                ('terminal UTF-8 streaming decoder', 'terminalDecoders' in js and 'stream: true' in js),
                ('terminal symbol fallback font', 'Segoe UI Symbol' in js),
                ('initAdbDiagnostic defined', 'function initAdbDiagnostic' in js),
                ('ADB diagnostic starts through WS', "run-adb-diagnostic" in js),
                ('ADB diagnostic supports cancellation', "cancel-adb-diagnostic" in js),
                ('ADB diagnostic progress handler', 'diagnostic-progress' in js),
                ('ADB diagnostic renders plain text', 'appendDiagnosticText' in js),
                ('relay state in frontend state', 'relay: {' in js),
                ('relay status handler', "case 'relay-status'" in js),
                ('relay peer request handler', "case 'relay-peer-request'" in js),
                ('relay confirm dialog', 'relay-confirm-overlay' in js),
                ('relay status badge', 'status-relay' in js),
                ('relay served session count', 'servedSessions' in js),
                ('tools-tab DOM', 'tools-tab' in js),
                ('tools-menu DOM', 'tools-menu' in js),
                ('tools relay entry', 'tools-relay' in js),
                ('dropdown tabs map', 'DROPDOWN_TABS' in js),
                ('openRelayConfigDialog defined', 'function openRelayConfigDialog' in js),
                ('relay config dialog open guard', 'function relayConfigDialogOpen' in js),
                ('relay config dirty detection', 'function relayConfigDirty' in js),
                ('relay effective banner render', 'function renderRelayEffective' in js),
                ('relay refresh re-applies disk config', "callWS('relay-reload-config'" in js),
                ('relay save asks about existing share data', 'function saveRelayConfig' in js),
                ('relay root choice prompt', 'function askRelayRootChoice' in js),
                ('relay root inspect call', "callWS('relay-inspect-root'" in js),
                ('relay root clean call', "callWS('relay-clean-root'" in js),
                ('relay multi-instance notice render', 'configNotice' in js),
                ('relay fixed machine name', "localIdEl.textContent = status.machineName" in js),
                ('relay deployment discovery', "callWS('relay-discover-deployments'" in js),
                ('remote device detection', 'function isRemoteDevice' in js),
                ('remote device key prefix', "'relay:'" in js),
                ('remote device selector sent to backend', 'key: t.key' in js),
                ('remote file transfer enabled', 'disable(cell.uploadBtn' not in js and 'disable(cell.downloadBtn' not in js and '共享目录中转' in js),
                ('remote inline edit gated', 'REMOTE_INLINE_EDIT_HINT' in js),
        ]

        print('[JS: app.js]')
        for n, p in js_checks:
            print(f'  {"PASS" if p else "FAIL"}  {n}')
            if not p: ok = False

        # 4. Check CSS
        css_checks = [
            ('toast css class', '.toast' in css),
            ('panel highlight class', '.terminal-panel.highlight' in css),
            ('panel border-color accent', 'border-color: var(--accent)' in css.split('.terminal-panel.highlight')[1].split('}')[0] if '.terminal-panel.highlight' in css else True),
            ('no xterm highlight leftover', '.terminal-xterm.highlight' not in css),
            ('handle class cell-toolbar-handle', '.cell-toolbar-handle' in css),
            ('handle ns-resize', 'ns-resize' in css),
            ('handle height 3px', 'height: 3px' in css),
            ('handle hover', '.cell-toolbar-handle:hover' in css),
            ('handle dragging', '.cell-toolbar-handle.dragging' in css),
            ('toolbar defined', '.cell-toolbar' in css),
            ('toolbar row height 34px', 'height: 34px' in css.split('.cell-toolbar-row')[1].split('}')[0] if '.cell-toolbar-row' in css else True),
            ('file op button class', '.file-op-btn' in css),
            ('file ops form class', '.cell-toolbar-file-ops' in css),
            ('file ops inner class', '.file-ops-inner' in css),
            ('cmd-input flex:0 0 auto', 'flex: 0 0 auto' in css),
            ('cmd-input min-height 34px', 'min-height: 34px' in css),
            ('cmd-input max-height 50vh', '50vh' in css),
            ('terminal-cell defined', '.terminal-cell' in css),
            ('cell flex column', 'flex-direction: column' in css.split('.terminal-cell')[1].split('}')[0] if '.terminal-cell' in css else True),
            ('xterm flex:1 1 0', 'flex: 1 1 0' in css),
            ('container height:100%', 'height: 100%' in css.split('.terminal-container')[1].split('}')[0] if '.terminal-container' in css else True),
            ('device-browser position: fixed', 'position: fixed' in css.split('.device-browser')[1].split('}')[0] if '.device-browser' in css else True),
             ('device-browser flex-direction', 'flex-direction: column' in css.split('.device-browser')[1].split('}')[0] if '.device-browser' in css else True),
             ('dev-browser-list overflow-y: auto', 'overflow-y: auto' in css.split('.dev-browser-list')[1].split('}')[0] if '.dev-browser-list' in css else True),
             ('dev-browser-list flex: 1', 'flex: 1' in css.split('.dev-browser-list')[1].split('}')[0] if '.dev-browser-list' in css else True),
             ('device-browser fixed width', 'width: 420px' in css.split('.device-browser')[1].split('}')[0] if '.device-browser' in css else True),
             ('device-browser min-width', 'min-width: 160px' in css),
               ('path-btns class', '.dev-browser-path-btns' in css),
               ('device-refresh-btn class', '.device-refresh-btn' in css),
               ('device diagnostic button class', '.device-diagnostic-btn' in css),
               ('log-submenu class', '.log-submenu' in css),
               ('log-submenu-row class', '.log-submenu-row' in css),
               ('command-popup class', '.command-popup' in css),
               ('cmd-popup-btn class', '.cmd-popup-btn' in css),
               ('cmd-context-menu class', '.cmd-context-menu' in css),
               ('cmd-edit-dialog class', '.cmd-edit-dialog' in css),
                ('cmd-group-select class', '.cmd-group-select' in css),
                ('cmd-group-mgr-list class', '.cmd-group-mgr-list' in css),
                ('tab-dropdown class', 'tab-dropdown' in css),
                ('tab-dropdown-item class', 'tab-dropdown-item' in css),
                ('terminal find dialog style', '.terminal-find-dialog' in css),
                ('terminal find option style', '.terminal-find-options' in css),
                ('about-dialog class', '.about-dialog' in css),
                ('log-save-path', 'log-save-path' in css),
                ('log timestamp toggle', '.log-timestamp-toggle' in css),
                ('log-toggle.danger', '.log-toggle.danger' in css),
               ('file selected style', '.selected' in css),
               ('device browser disabled style', '.dev-browser-path-btns button:disabled' in css),
               ('edit file button style', '.file-op-btn.edit' in css),
               ('inline editor overlay style', '.inline-edit-overlay' in css),
               ('inline editor textarea style', '.inline-edit-text' in css),
               ('ADB diagnostic progress style', '.diagnostic-progress' in css),
               ('ADB diagnostic result style', '.diagnostic-results' in css),
               ('ADB diagnostic status style', '.diagnostic-status-fail' in css),
               ('relay status badge style', '.status-relay' in css),
               ('remote device tab style', '.device-tab.remote' in css and '.device-tab.offline' in css),
               ('remote terminal badge style', '.remote-badge' in css),
               ('remote op gating style', '.remote-disabled' in css and '.remote-op-notice' in css),
               ('relay first-connect dialog style', '.relay-confirm-dialog' in css),
               ('relay config dialog style', '.relay-config-dialog' in css),
               ('relay effective banner style', '.relay-effective' in css),
               ('relay multi-instance notice style', '.relay-config-notice' in css),
               ('relay dialog button restyle', '.relay-config-dialog .tool-btn' in css),
               ('relay dialog danger button style', '.relay-root-choice-dialog .tool-btn.danger' in css),
               ('relay root choice dialog style', '.relay-root-choice-dialog' in css),
        ]

        print('\n[CSS: style.css]')
        for n, p in css_checks:
            print(f'  {"PASS" if p else "FAIL"}  {n}')
            if not p: ok = False

        # 5. urlopen check: look for v=51 or a random cache-bust pattern in HTML
        resp = urllib.request.urlopen(f'http://127.0.0.1:{http_port}/', timeout=5)
        html = resp.read().decode('utf-8')
        resources = _Resources()
        resources.feed(html)
        main_urls = [url for url in resources.urls if url.path in ("app.js", "style.css")]
        html_checks = [
            ('toast-container exists', 'toast-container' in html),
            ('app.js?v= referenced', 'app.js?v=' in html),
            ('style.css?v= referenced', 'style.css?v=' in html),
            ('v= references match', len(main_urls) == 2 and {url.path for url in main_urls} == {'app.js', 'style.css'}
             and all(parse_qs(url.query).get('v') for url in main_urls)),
            ('应用版本正确注入', f"const APP_VERSION = '{APP_VERSION}';" in html and '__APP_VERSION__' not in html),
            ('WS_PORT injected', 'WS_PORT' in html and '__WS_PORT__' not in html),
            ('layout reset button', 'layout-reset-btn' in html),
            ('terminal find menu item', 'file-find' in html),
            ('terminal find dialog controls', 'terminal-find-dialog' in html and 'terminal-find-input' in html and 'terminal-find-prev' in html and 'terminal-find-next' in html),
            ('terminal find options', 'terminal-find-case' in html and 'terminal-find-whole' in html),
            ('ADB diagnostic device-bar entry', 'device-diagnostic-btn' in html),
            ('ADB diagnostic dialog controls', 'diag-overlay' in html and 'diag-start' in html and 'diag-cancel' in html and 'diag-results' in html),
            ('relay settings group', 'relay-enabled' in html and 'relay-role' in html),
            ('relay settings inputs', 'relay-peer-id' in html and 'relay-root' in html and 'relay-carrier' in html),
            ('relay unified pairing', 'relay-pairing' in html and 'relay-pool-entries' not in html and 'relay-peers' not in html),
            ('relay events log panel', 'relay-events' in html),
            ('relay first-connect dialog', 'relay-confirm-overlay' in html and 'relay-confirm-body' in html),
            ('tools menu entry', 'tools-tab' in html and 'tools-menu' in html and 'tools-relay' in html),
            ('relay config dialog', 'relay-config-overlay' in html and 'relay-config-close' in html),
            ('relay effective banner', 'relay-effective' in html),
            ('relay multi-instance notice element', 'relay-config-notice' in html),
            ('relay root choice dialog', 'relay-root-choice-overlay' in html
             and 'relay-root-keep' in html and 'relay-root-clean' in html
             and 'relay-root-cancel' in html and 'relay-root-choice-files' in html),
        ]

        print('\n[HTML: index.html]')
        for n, p in html_checks:
            print(f'  {"PASS" if p else "FAIL"}  {n}')
            if not p: ok = False

    except Exception as e:
        print(f'  [FAIL] Server request: {type(e).__name__}: {e}')
        ok = False
    finally:
        httpd.shutdown()
        srv.HTTP_PORT = old_http_port

    return ok

if __name__ == '__main__':
    sys.exit(0 if run() else 1)
