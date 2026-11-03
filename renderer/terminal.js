// ============================================================
// 终端 UI：设备标签、面板、文件操作、布局、查找、设置、ADB 诊断。
// 依赖方向：core/net/notify/relay <- terminal.js（不反向引用 app.js）。
// ============================================================
import { $$, state, terminalDecoders } from './core.js';
import { callWS, reportUiAction, sendWS, ws } from './net.js';
import { showToast } from './notify.js';
import {
  deviceLabel,
  isDeviceUsable,
  isRemoteDevice,
  openRelayConfigDialog,
  relayPeerLabel,
} from './relay.js';

// Drag state stored per-element via dataset (no global shared state)
export function initDragResize() {
  document.addEventListener('mousedown', (e) => {
    const handle = e.target.closest('.cell-toolbar-handle');
    if (!handle) return;
    const cell = handle.closest('.terminal-cell');
    if (!cell) return;
    const cmdDiv = cell.querySelector('.terminal-cmd-input');
    if (!cmdDiv) return;

    handle.dataset.dragging = '1';
    handle.dataset.startY = e.clientY;
    handle.dataset.startHeight = cmdDiv.offsetHeight;
    handle.classList.add('dragging');
    document.body.style.cursor = 'ns-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', (e) => {
    const handle = document.querySelector('.cell-toolbar-handle.dragging');
    if (!handle) return;
    const cell = handle.closest('.terminal-cell');
    if (!cell) return;
    const cmdDiv = cell.querySelector('.terminal-cmd-input');
    if (!cmdDiv) return;

    const startY = parseFloat(handle.dataset.startY);
    const startHeight = parseFloat(handle.dataset.startHeight);
    const delta = startY - e.clientY;
    // Both handles adjust cmd-input height: drag UP (delta > 0) → cmd-input bigger → xterm smaller
    const newHeight = Math.max(34, Math.min(500, startHeight + delta));
    cmdDiv.style.height = newHeight + 'px';
    setTimeout(refreshAllTerminals, 50);
  });

  document.addEventListener('mouseup', () => {
    const handle = document.querySelector('.cell-toolbar-handle.dragging');
    if (!handle) return;
    handle.classList.remove('dragging');
    delete handle.dataset.dragging;
    delete handle.dataset.startY;
    delete handle.dataset.startHeight;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
}

// Active-region tracking: highlight the panel border until another region is clicked
export let _activePanel = null;

export function initHighlight() {
  document.getElementById('terminal-container').addEventListener('mousedown', (e) => {
    const target = e.target.closest('.terminal-xterm, .terminal-cmd-input');
    if (!target) return;
    const panel = target.closest('.terminal-panel');
    if (!panel) return;
    selectActiveDevice(panel.dataset.key);
    if (panel === _activePanel) return;
    if (_activePanel) _activePanel.classList.remove('highlight');
    _activePanel = panel;
    _activePanel.classList.add('highlight');
  });
}

// Global application state 已移入 core.js（状态与 DOM 助手是共享原语）。


// Quick-commands groups (each group has a name + commands array)
export const DEFAULT_COMMAND_GROUPS = [
  {
    name: '默认',
    commands: [
      { name: '重启', cmd: 'reboot' },
      { name: '重启 BL', cmd: 'reboot bootloader' },
      { name: 'Root', cmd: 'root' },
      { name: '内核日志', cmd: 'dmesg' },
      { name: '日志转储', cmd: 'logcat -b all -d' },
      { name: 'SDK 版本', cmd: 'getprop ro.build.version.sdk' },
      { name: 'Android 版本', cmd: 'getprop ro.build.version.release' },
      { name: '屏幕尺寸', cmd: 'wm size' },
      { name: '内存信息', cmd: 'cat /proc/meminfo' },
      { name: '磁盘信息', cmd: 'df -h' },
    ],
  },
];

export let _cachedGroups = null;

export async function loadCommandGroups() {
  const r = await callWS('get-commands');
  if (r.success && r.output) {
    _cachedGroups = r.output;
    return r.output;
  }
  // First launch: seed defaults on server
  const def = JSON.parse(JSON.stringify(DEFAULT_COMMAND_GROUPS));
  await callWS('save-commands', { commands: def });
  _cachedGroups = def;
  return def;
}

export async function saveCommandGroups(groups) {
  _cachedGroups = groups;
  await callWS('save-commands', { commands: groups });
}

// Expose for debugging
window.__debugCommands = function() {
  console.log('cached groups:', _cachedGroups);
  console.log('default groups:', DEFAULT_COMMAND_GROUPS);
};

// DOM helpers
// DOM helpers 已移入 core.js（$ / $$ 由各模块按需 import）。


// base64ToUint8Array 已移入 core.js。

// ============================================================
// Top tabs
// ============================================================
// 菜单栏里带下拉的标签：点它只开合下拉，不切内容面板（所以没有 panel-<tab>）。
export const DROPDOWN_TABS = {
  terminal: 'window-menu',
  file: 'file-menu',
  tools: 'tools-menu',
  help: 'help-menu',
};

export function initTopTabs() {
  $$('.tab-bar .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const menuWasOpen = document.getElementById(DROPDOWN_TABS[tab.dataset.tab])?.style.display === 'block';
      // Close dropdowns
      for (const menuId of Object.values(DROPDOWN_TABS)) {
        const menu = document.getElementById(menuId);
        if (menu) menu.style.display = 'none';
      }

      const menuId = DROPDOWN_TABS[tab.dataset.tab];
      if (menuId) {
        if (tab.dataset.tab === 'terminal') {
          state.currentTab = 'terminal';
          $$('.tab-bar .tab').forEach(t => t.classList.toggle('active', t === tab));
          $$('.content-panel').forEach(p => p.classList.toggle('active', p.id === 'panel-terminal'));
          setTimeout(refreshAllTerminals, 100);
        }
        const menu = document.getElementById(menuId);
        const rect = tab.getBoundingClientRect();
        if (menu) {
          menu.style.setProperty('--dl-left', rect.left + 'px');
          const hasItems = Array.from(menu.children).some(item => item.style.display !== 'none');
          menu.style.display = menuWasOpen || !hasItems ? 'none' : 'block';
        }
        return;
      }

      $$('.tab-bar .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      state.currentTab = tab.dataset.tab;
      $$('.content-panel').forEach(p => p.classList.remove('active'));
      const panel = document.getElementById(`panel-${tab.dataset.tab}`);
      if (panel) panel.classList.add('active');
      if (tab.dataset.tab === 'terminal') {
        setTimeout(refreshAllTerminals, 100);
      }
    });
  });
  // Close dropdowns when clicking outside
  document.addEventListener('click', (e) => {
    for (const [menuId, tabId] of [
      ['file-menu', 'file-tab'], ['window-menu', 'window-tab'], ['tools-menu', 'tools-tab'], ['help-menu', 'help-tab'],
    ]) {
      const menu = document.getElementById(menuId);
      const tabEl = document.getElementById(tabId);
      if (menu && menu.style.display === 'block' && !menu.contains(e.target) && e.target !== tabEl && !tabEl.contains(e.target)) {
        menu.style.display = 'none';
      }
    }
  });
  // File menu items
  const restartItem = document.getElementById('file-restart');
  if (restartItem) {
    restartItem.addEventListener('click', async () => {
      document.getElementById('file-menu').style.display = 'none';
      showToast('info', '应用重启', '正在重启后端服务...');
      const r = await callWS('restart-server');
      if (r.success) {
        setTimeout(() => location.reload(), 2000);
      } else {
        showToast('error', '应用重启', r.output || '重启失败');
      }
    });
  }
  const findItem = document.getElementById('file-find');
  if (findItem) {
    findItem.addEventListener('click', () => {
      document.getElementById('file-menu').style.display = 'none';
      openTerminalFind();
    });
  }
  // Tools menu items
  const relayItem = document.getElementById('tools-relay');
  if (relayItem) {
    relayItem.addEventListener('click', () => {
      document.getElementById('tools-menu').style.display = 'none';
      openRelayConfigDialog();
    });
  }
  // Help menu items
  const aboutItem = document.getElementById('help-about');
  if (aboutItem) {
    aboutItem.addEventListener('click', () => {
      document.getElementById('help-menu').style.display = 'none';
      showAboutDialog();
    });
  }
}

/** Show the "About" dialog with app info. */
export function showAboutDialog() {
  const overlay = document.createElement('div');
  overlay.className = 'cmd-edit-overlay';
  overlay.innerHTML = `
    <div class="about-dialog">
      <h3>关于 DevMux</h3>
      <div class="about-logo">📱</div>
      <div class="about-info">
        <div class="about-row"><span class="about-key">应用名称</span><span>DevMux</span></div>
        <div class="about-row"><span class="about-key">版本</span><span>v${APP_VERSION}</span></div>
      </div>
      <div class="about-btns">
        <button class="about-close primary">关闭</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.about-close').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
}

// ============================================================
// Terminal find
// ============================================================
// xterm 当前仅引入 FitAddon；查找直接读取其公开 buffer API，避免额外加载依赖。
export const TERMINAL_FIND_WORD_CHAR = /[\p{L}\p{N}_]/u;

export function initTerminalFind() {
  const input = document.getElementById('terminal-find-input');
  const caseOption = document.getElementById('terminal-find-case');
  const wholeOption = document.getElementById('terminal-find-whole');
  document.getElementById('terminal-find-close').addEventListener('click', closeTerminalFind);
  document.getElementById('terminal-find-next').addEventListener('click', () => findTerminalText(1));
  document.getElementById('terminal-find-prev').addEventListener('click', () => findTerminalText(-1));

  input.addEventListener('input', () => {
    state.find.currentIndex = -1;
    findTerminalText(1);
  });
  [caseOption, wholeOption].forEach(option => option.addEventListener('change', () => {
    state.find.currentIndex = -1;
    findTerminalText(1);
  }));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      findTerminalText(e.shiftKey ? -1 : 1);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      closeTerminalFind();
    }
  });
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'f') {
      e.preventDefault();
      e.stopPropagation();
      openTerminalFind();
    } else if (e.key === 'Escape' && document.getElementById('terminal-find-dialog').style.display === 'block') {
      closeTerminalFind();
    }
  }, true);
}

export function openTerminalFind() {
  const key = state.activeDevice;
  const target = key && state.terminals[key];
  if (!target) {
    showToast('warn', '终端查找', '请先选中一个设备窗口');
    return;
  }
  state.find.targetKey = key;
  state.find.currentIndex = -1;
  const dialog = document.getElementById('terminal-find-dialog');
  const device = target.device || {};
  document.getElementById('terminal-find-device').textContent = `${device.serial || key} (tid=${device.transportId || '?'})`;
  dialog.style.display = 'block';
  refreshTerminalFindMatches();
  const input = document.getElementById('terminal-find-input');
  input.focus();
  input.select();
}

export function closeTerminalFind() {
  const dialog = document.getElementById('terminal-find-dialog');
  if (dialog.style.display !== 'block') return;
  clearTerminalFindSelection();
  dialog.style.display = 'none';
  state.find.targetKey = null;
  state.find.matches = [];
  state.find.currentIndex = -1;
}

export function setTerminalFindStatus(text) {
  document.getElementById('terminal-find-status').textContent = text;
}

export function clearTerminalFindSelection() {
  const target = state.terminals[state.find.targetKey];
  if (!target) return;
  target.suppressSelectionCopy = true;
  try { target.term.clearSelection(); } catch {}
  setTimeout(() => { target.suppressSelectionCopy = false; }, 60);
}

// BufferLine 的字符索引和终端列数并不总是一致（中文占两列、Emoji 可占多个 UTF-16 字符）。
// 因此同时建立“字符偏移 → xterm 列”的映射，供选区高亮精确定位。
export function getTerminalFindLineInfo(line, row, cols) {
  let text = '';
  const columns = [0];
  if (line && typeof line.getCell === 'function') {
    for (let col = 0; col < cols; col++) {
      const cell = line.getCell(col);
      const width = cell && typeof cell.getWidth === 'function' ? cell.getWidth() : 1;
      if (width === 0) continue;
      const chars = cell && typeof cell.getChars === 'function' ? (cell.getChars() || ' ') : ' ';
      const start = text.length;
      text += chars;
      for (let index = start; index < text.length; index++) columns[index] = col;
      columns[text.length] = col + Math.max(1, width);
    }
  } else {
    text = line ? line.translateToString(false) : '';
    for (let index = 0; index <= text.length; index++) columns[index] = Math.min(index, cols);
  }
  // xterm 的物理行会用空格补齐至整行；查找逻辑按逻辑输出行工作，故去除这些填充空格。
  while (text.endsWith(' ')) text = text.slice(0, -1);
  columns.length = text.length + 1;
  if (columns[text.length] === undefined) columns[text.length] = Math.min(text.length, cols);
  return { row, text, columns };
}

export function getTerminalFindLogicalLines(term) {
  const buffer = term.buffer.active;
  const lines = [];
  let current = null;
  for (let row = 0; row < buffer.length; row++) {
    const source = buffer.getLine(row);
    const piece = getTerminalFindLineInfo(source, row, term.cols);
    if (source && source.isWrapped && current) {
      piece.offset = current.text.length;
      current.pieces.push(piece);
      current.text += piece.text;
    } else {
      current = { text: piece.text, pieces: [piece] };
      piece.offset = 0;
      lines.push(current);
    }
  }
  return lines;
}

export function isTerminalFindWholeWord(text, start, end) {
  const before = start > 0 ? Array.from(text.slice(0, start)).pop() : '';
  const after = end < text.length ? Array.from(text.slice(end))[0] : '';
  return !(before && TERMINAL_FIND_WORD_CHAR.test(before))
    && !(after && TERMINAL_FIND_WORD_CHAR.test(after));
}

export function getTerminalFindPosition(logicalLine, offset, isEnd) {
  const pieces = logicalLine.pieces;
  for (let index = 0; index < pieces.length; index++) {
    const piece = pieces[index];
    const end = piece.offset + piece.text.length;
    if ((isEnd ? offset <= end : offset < end) || index === pieces.length - 1) {
      const localOffset = Math.max(0, Math.min(piece.text.length, offset - piece.offset));
      return { row: piece.row, col: piece.columns[localOffset] ?? 0 };
    }
  }
  return { row: 0, col: 0 };
}

export function collectTerminalFindMatches(target, query, caseSensitive, wholeWord) {
  if (!query) return [];
  const needle = caseSensitive ? query : query.toLocaleLowerCase();
  const matches = [];
  for (const logicalLine of getTerminalFindLogicalLines(target.term)) {
    const source = caseSensitive ? logicalLine.text : logicalLine.text.toLocaleLowerCase();
    for (let start = source.indexOf(needle); start >= 0; start = source.indexOf(needle, start + Math.max(1, needle.length))) {
      const end = start + query.length;
      if (wholeWord && !isTerminalFindWholeWord(logicalLine.text, start, end)) continue;
      matches.push({
        start: getTerminalFindPosition(logicalLine, start, false),
        end: getTerminalFindPosition(logicalLine, end, true),
      });
    }
  }
  return matches;
}

export function refreshTerminalFindMatches() {
  const target = state.terminals[state.find.targetKey];
  const query = document.getElementById('terminal-find-input').value;
  if (!target || !query) {
    state.find.matches = [];
    setTerminalFindStatus(query ? '终端已关闭' : '输入字符串开始查找');
    return [];
  }
  state.find.matches = collectTerminalFindMatches(
    target,
    query,
    document.getElementById('terminal-find-case').checked,
    document.getElementById('terminal-find-whole').checked,
  );
  setTerminalFindStatus(state.find.matches.length ? `0 / ${state.find.matches.length}` : '未找到匹配项');
  return state.find.matches;
}

export function revealTerminalFindMatch(target, match) {
  const start = match.start;
  const end = match.end;
  const length = start.row === end.row
    ? end.col - start.col
    : (target.term.cols - start.col) + (end.row - start.row - 1) * target.term.cols + end.col;
  try {
    target.term.scrollToLine(start.row);
    target.suppressSelectionCopy = true;
    target.term.clearSelection();
    target.term.select(start.col, start.row, Math.max(1, length));
    setTimeout(() => { target.suppressSelectionCopy = false; }, 60);
  } catch {}
}

export function findTerminalText(direction) {
  const dialog = document.getElementById('terminal-find-dialog');
  if (dialog.style.display !== 'block') return;
  const matches = refreshTerminalFindMatches();
  if (!matches.length) {
    clearTerminalFindSelection();
    return;
  }
  const previous = state.find.currentIndex;
  const next = previous < 0
    ? (direction > 0 ? 0 : matches.length - 1)
    : (previous + direction + matches.length) % matches.length;
  state.find.currentIndex = next;
  revealTerminalFindMatch(state.terminals[state.find.targetKey], matches[next]);
  const wrapped = previous >= 0 && ((direction > 0 && next <= previous) || (direction < 0 && next >= previous));
  setTerminalFindStatus(`${next + 1} / ${matches.length}${wrapped ? '（已循环）' : ''}`);
}

// ============================================================
// Device tabs
// ============================================================
export function updateDeviceTabs() {
  const container = document.getElementById('device-tabs');
  const existingKeys = new Set();
  // 已存在的标签要**同步刷新**：远端设备的在线状态会随心跳变化，
  // 只在创建时写一次标签文本，会把「已离线」一直显示成在线。
  container.querySelectorAll('.device-tab').forEach(el => {
    const key = el.dataset.key;
    if (terminalPanelKeys().includes(key)) {
      existingKeys.add(key);
      syncDeviceTab(el, key);
    } else {
      el.remove();
    }
  });

  for (const key of terminalPanelKeys()) {
    if (existingKeys.has(key)) continue;
    const dev = state.devices.find(d => _deviceKey(d) === key) || state.terminals[key]?.computer;
    if (!dev) continue;
    const tab = document.createElement('div');
    tab.className = 'device-tab';
    tab.dataset.key = key;
    tab.innerHTML = `
      <span class="dot"></span>
      <span class="device-tab-label"></span>
      <button class="close-btn" title="关闭终端">×</button>
    `;
    syncDeviceTab(tab, key);
    tab.addEventListener('click', (e) => {
      if (e.target.closest('.close-btn')) return;
      const terminal = state.terminals[key];
      if (terminal?.panel.classList.contains('minimized')) terminal.windowManager?.toggleMinimize();
      selectActiveDevice(key);
      applyLayout(state.layout);
    });
    tab.querySelector('.close-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      await dismissTerminal(key);
    });
    container.appendChild(tab);
  }
}

export function syncDeviceTab(tab, key) {
  const computer = state.terminals[key]?.computer;
  if (computer) {
    const title = state.terminals[key]?.serialPort ? `Serial · ${state.terminals[key].serialPort}` : 'PowerShell';
    tab.querySelector('.device-tab-label').textContent = `${title} · ${computer.label}`;
    tab.querySelector('.dot').classList.toggle('remote', !computer.local);
    tab.title = `${title} · ${computer.local ? '本机' : '远程从机'}`;
    return;
  }
  const dev = state.devices.find(d => _deviceKey(d) === key);
  if (!dev) return;
  const label = tab.querySelector('.device-tab-label');
  if (label) label.textContent = deviceLabel(dev);
  const usable = isDeviceUsable(dev);
  const dot = tab.querySelector('.dot');
  if (dot) {
    dot.classList.toggle('offline', !usable);
    dot.classList.toggle('remote', isRemoteDevice(dev));
  }
  tab.classList.toggle('remote', isRemoteDevice(dev));
  tab.classList.toggle('offline', !usable);
  tab.title = isRemoteDevice(dev)
    ? `远端设备：由 ${relayPeerLabel(dev.relay)} 提供（${dev.relay.online === false ? '对端离线' : '在线'}）`
    : '本机设备';
}

// 设备标签和终端面板共享同一个“当前设备”状态，供单屏布局和终端查找使用。
export function selectActiveDevice(key) {
  if (!key || (!state.terminals[key] && !state.knownKeys.includes(key))) return;
  state.terminals[key]?.floatingWindow?.raise();
  if (!state.terminals[key]?.floatingWindow) state.layoutActiveDevice = key;
  state.activeDevice = key;
  document.querySelectorAll('#device-tabs .device-tab').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.key === key);
  });
}

// ============================================================
// Terminal panels
// ============================================================
export function _deviceKey(d) {
  // 服务端会给每台设备补上 key（远端设备带 relay:<peerId>: 前缀），优先用它，
  // 避免前端按 serial+tid 重新拼 key 时把远端设备拼成本机 key。
  if (d && d.key) return d.key;
  return d.transportId ? `${d.serial}:tid=${d.transportId}` : d.serial;
}

// 远程上传下载复用本机文件对话框，由后端经共享目录中转。
export const REMOTE_INLINE_EDIT_HINT =
  '跨机中转暂不支持直接编辑设备文件（写回需要 stdin 通道）';

export function syncTerminalControls() {
  for (const [key, t] of Object.entries(state.terminals)) {
    if (t.computer) continue;
    const remote = key.startsWith('relay:');
    const control = remote
      ? (((state.relay.status || {}).sessions || []).find(item => item.key === key) || {})
      : ((state.terminalControl || {})[key] || {});
    const agentControlled = !!(state.agentClaims || {})[key] || control.owner === 'agent';
    const readonly = agentControlled || (remote ? !!control.switchingControl || control.owner === 'local' : control.owner === 'remote');
    if (!remote && t._controlOwner === 'remote' && control.owner === 'local') {
      sendWS({type:'resize-terminal', key, cols:t.term.cols, rows:t.term.rows});
    }
    t._controlOwner = control.owner;
    t.term.options.disableStdin = readonly;
    if (t.cmdTextarea) {
      t.cmdTextarea.disabled = readonly;
      t.cmdTextarea.title = readonly ? '当前由另一端控制，终端只读' : '';
    }
    let tools = t.header.querySelector('.terminal-control-tools');
    if (!tools) {
      tools = document.createElement('span');
      tools.className = 'terminal-control-tools';
      t.header.appendChild(tools);
    }
    tools.replaceChildren();
    if (control.shared || control.held || agentControlled) {
      const label = document.createElement('span');
      label.textContent = agentControlled ? '只读' : control.owner === 'remote' ? '主机控制' : '从机控制';
      tools.appendChild(label);
      if (!remote || readonly) {
        const button = document.createElement('button');
        button.className = 'tool-btn';
        button.textContent = agentControlled || remote || control.owner === 'remote' ? '接管' : '交还主机';
        button.onclick = () => sendWS({type:'terminal-control', key,
          action: agentControlled || remote || control.owner === 'remote' ? 'take' : 'give', controlGeneration: control.generation});
        tools.appendChild(button);
      }
    }
  }
}

export function remoteTerminalState(key, device) {
  if (device.relay && device.relay.online === false) return '离线';
  const sessions = (state.relay.status && state.relay.status.sessions) || [];
  const session = sessions.find(item => item.key === key);
  if (session && session.switchingControl) return '控制权切换中';
  if (session && session.opened) return '终端已就绪';
  return session && session.failures ? '终端重试中' : '终端准备中';
}

export function applyRemoteDeviceGuards(cell, device) {
  const disable = (el, hint) => {
    if (!el) return;
    el.disabled = true;
    el.title = hint;
    el.classList.add('remote-disabled');
  };
  disable(cell.editBtn, REMOTE_INLINE_EDIT_HINT);
  disable(cell.editInline, REMOTE_INLINE_EDIT_HINT);

  if (cell.fileOps) {
    const notice = document.createElement('div');
    notice.className = 'remote-op-notice';
    notice.textContent = '远端设备：上传下载经共享目录中转，文件选择与保存均在本机；从机白名单需允许 push / pull。';
    cell.fileOps.insertBefore(notice, cell.fileOps.firstChild);
  }
  if (cell.editOps) {
    const notice = document.createElement('div');
    notice.className = 'remote-op-notice';
    notice.textContent = REMOTE_INLINE_EDIT_HINT;
    cell.editOps.insertBefore(notice, cell.editOps.firstChild);
  }
}

export function createTerminalPanel(key, device) {
  const container = document.getElementById('terminal-container');

  const panel = document.createElement('div');
  panel.className = 'terminal-panel';
  panel.dataset.key = key;

  // Build header
  const remoteBadge = isRemoteDevice(device)
    ? `<span class="header-badge remote-badge" title="远端设备：由 ${relayPeerLabel(device.relay)} 提供">远端 ${relayPeerLabel(device.relay)}</span>`
    : '';
  const header = document.createElement('div');
  header.className = 'terminal-panel-header' + (isRemoteDevice(device) ? ' remote' : '');
  header.innerHTML = `
    <span class="header-title">${deviceLabel(device)}</span>${remoteBadge}
    <button class="header-btn minimize" title="最小化">−</button>
    <button class="header-btn maximize" title="最大化">□</button>
    <button class="header-btn close" title="关闭">×</button>
  `;

  // Build cell: xterm + toolbar + cmd-input
  const cell = _buildPanelCell(key, device);
  if (isRemoteDevice(device)) applyRemoteDeviceGuards(cell, device);

  // Assemble panel
  panel.appendChild(header);
  panel.appendChild(cell.el);
  container.appendChild(panel);

  // Init xterm terminal
  const { term, fitAddon } = _initTerminal(key, device, cell.xtermDiv);
  term.write(`\x1b[32mConnected: ${deviceLabel(device)}\x1b[0m\r\n`);
  if (isRemoteDevice(device)) {
    term.write('\x1b[33m[跨机中转] 连接从机设备终端；共享会话由一方控制，另一方只读；直接编辑暂不可用\x1b[0m\r\n');
  }
  term.write(`\x1b[2m[WS: ${ws && ws.readyState === 1 ? 'OPEN' : 'CLOSED'}] [terminals: ${Object.keys(state.terminals).length + 1}]\x1b[0m\r\n`);

  // Flush buffered data
  const buf = state.terminalBuffer[key];
  if (buf && buf.length > 0) {
    let total = 0;
    for (const chunk of buf) { try { term.write(chunk); total += chunk.length; } catch {} }
    term.write(`\x1b[2m[flushed ${buf.length} chunks, ${total} bytes]\x1b[0m\r\n`);
    delete state.terminalBuffer[key];
  }

  // Save references
  state.terminals[key] = {
    term, fitAddon, panel, header,
    xtermDiv: cell.xtermDiv, cmdTextarea: cell.cmdTextarea,
    toolbar: cell.toolbar, fileOps: cell.fileOps, devBrowser: cell.devBrowser,
    foLocal: cell.foLocal, foRemote: cell.foRemote,
    foExec: cell.foExec, foClose: cell.foClose,
    foBrowseLocal: cell.foBrowseLocal, foLocalPicker: cell.foLocalPicker,
    foLocalPickerMenu: cell.foLocalPickerMenu, foPickFiles: cell.foPickFiles,
    foPickDirectory: cell.foPickDirectory,
    foBrowseRemote: cell.foBrowseRemote,
    uploadBtn: cell.uploadBtn, downloadBtn: cell.downloadBtn,
    editBtn: cell.editBtn, editOps: cell.editOps, editBrowser: cell.editBrowser,
    editRemote: cell.editRemote, editBrowse: cell.editBrowse,
    editInline: cell.editInline,
    editClose: cell.editClose,
    logBtn: cell.logBtn, logMenu: cell.logMenu,
    cmdBtn: cell.cmdBtn, cmdPopup: cell.cmdPopup,
    key, device,
  };

  const t = state.terminals[key];
  window.TerminalWindows?.attach(key, panel, header);
  syncTerminalControls();

  // Wire: terminal input → WS
  const input = data => sendWS({ type: 'write-terminal', key, data });
  t.nativeInput = input;
  term.onData(input);

  // Wire: command textarea keyboard
  cell.cmdTextarea.addEventListener('keydown', (e) => {
    // History navigation: ArrowUp / ArrowDown
    if (e.key === 'ArrowUp' && !(e.ctrlKey || e.shiftKey)) {
      e.preventDefault();
      if (t._cmdHistory.length === 0) return;
      if (t._cmdHistoryIdx < 0) t._cmdHistoryIdx = t._cmdHistory.length - 1;
      else if (t._cmdHistoryIdx > 0) t._cmdHistoryIdx--;
      cell.cmdTextarea.value = t._cmdHistory[t._cmdHistoryIdx];
      cell.cmdTextarea.selectionStart = cell.cmdTextarea.selectionEnd = cell.cmdTextarea.value.length;
      return;
    }
    if (e.key === 'ArrowDown' && !(e.ctrlKey || e.shiftKey)) {
      e.preventDefault();
      if (t._cmdHistoryIdx >= 0) t._cmdHistoryIdx++;
      if (t._cmdHistoryIdx >= t._cmdHistory.length) {
        t._cmdHistoryIdx = -1;
        cell.cmdTextarea.value = '';
      } else {
        cell.cmdTextarea.value = t._cmdHistory[t._cmdHistoryIdx];
      }
      cell.cmdTextarea.selectionStart = cell.cmdTextarea.selectionEnd = cell.cmdTextarea.value.length;
      return;
    }
    // Ctrl+Enter / Shift+Enter: insert newline
    if (e.key === 'Enter' && (e.ctrlKey || e.shiftKey)) {
      e.preventDefault();
      const s = cell.cmdTextarea.selectionStart, en = cell.cmdTextarea.selectionEnd;
      cell.cmdTextarea.value = cell.cmdTextarea.value.substring(0, s) + '\n' + cell.cmdTextarea.value.substring(en);
      cell.cmdTextarea.selectionStart = cell.cmdTextarea.selectionEnd = s + 1;
      return;
    }
    // Enter: send command, save to history
    if (e.key === 'Enter') {
      e.preventDefault();
      const text = cell.cmdTextarea.value;
      if (text) {
        sendWS({ type: 'write-terminal', key, data: text + '\n' });
        // Save to history (dedup with last entry)
        if (t._cmdHistory[t._cmdHistory.length - 1] !== text) {
          t._cmdHistory.push(text);
          if (t._cmdHistory.length > 50) t._cmdHistory.shift();
        }
        t._cmdHistoryIdx = -1;
        cell.cmdTextarea.value = '';
      }
    }
  });

  // Wire: panel focus → cmd textarea
  panel.addEventListener('mousedown', (e) => {
    if (e.target.closest('.terminal-xterm')) return;
    if (e.target.closest('.header-btn')) return;
    if (e.target.closest('.terminal-cmd-textarea')) return;
    cell.cmdTextarea.focus();
  });

  // Wire: terminal resize → WS
  term.onResize(({ cols, rows }) => { sendWS({ type: 'resize-terminal', key, cols, rows }); });

  // Wire: toolbar file-ops
  _wireFileOps(key, cell);
  _wireDeviceFileEdit(key, cell);

  // Wire: header buttons
  header.querySelector('.minimize').addEventListener('click', () => {
    state.terminals[key]?.windowManager?.toggleMinimize();
  });
  header.querySelector('.maximize').addEventListener('click', () => {
    state.terminals[key]?.windowManager?.toggleMaximize();
  });
  header.querySelector('.close').addEventListener('click', async () => {
    await dismissTerminal(key);
  });
}

/** Build the cell DOM: xterm + drag handles + toolbar + cmd-input */
export function _buildPanelCell(key, device) {
  const cellEl = document.createElement('div');
  cellEl.className = 'terminal-cell';

  const xtermDiv = document.createElement('div');
  xtermDiv.className = 'terminal-xterm';

  // Top handle
  const handleTop = document.createElement('div');
  handleTop.className = 'cell-toolbar-handle';
  handleTop.dataset.target = 'xterm';
  handleTop.title = '拖拽调整终端高度';

  // Toolbar
  const toolbar = document.createElement('div');
  toolbar.className = 'cell-toolbar';

  const toolbarRow = document.createElement('div');
  toolbarRow.className = 'cell-toolbar-row';
  const uploadBtn = document.createElement('button');
  uploadBtn.className = 'file-op-btn upload';
  uploadBtn.title = '文件上传 (adb push)';
  uploadBtn.innerHTML = '↑';
  uploadBtn.dataset.op = 'upload';
  const downloadBtn = document.createElement('button');
  downloadBtn.className = 'file-op-btn download';
  downloadBtn.title = '文件下载 (adb pull)';
  downloadBtn.innerHTML = '↓';
  downloadBtn.dataset.op = 'download';
  toolbarRow.appendChild(uploadBtn);
  toolbarRow.appendChild(downloadBtn);

  // Device file editor
  const editBtn = document.createElement('button');
  editBtn.className = 'file-op-btn edit';
  editBtn.title = '编辑设备文件';
  editBtn.innerHTML = '✎';
  toolbarRow.appendChild(editBtn);

  // Log recording button
  const logBtn = document.createElement('button');
  logBtn.className = 'file-op-btn log';
  logBtn.title = '设备日志记录';
  logBtn.innerHTML = '📋';
  toolbarRow.appendChild(logBtn);

  // Commands button
  const cmdBtn = document.createElement('button');
  cmdBtn.className = 'file-op-btn cmd';
  cmdBtn.title = '快捷指令';
  cmdBtn.innerHTML = '⚡';
  toolbarRow.appendChild(cmdBtn);

  // File ops form (initially hidden)
  const fileOps = document.createElement('div');
  fileOps.className = 'cell-toolbar-file-ops';
  fileOps.style.display = 'none';
  const fileOpsInner = document.createElement('div');
  fileOpsInner.className = 'file-ops-inner';
  fileOpsInner.innerHTML = `
    <input class="fo-path-local" type="text" placeholder="本地路径" spellcheck="false" />
    <div class="fo-local-picker">
      <button class="fo-browse-local" title="选择本地文件或文件夹">📁</button>
      <div class="fo-local-picker-menu" style="display:none">
        <button class="fo-pick-files" title="选择一个或多个文件">选择文件</button>
        <button class="fo-pick-directory" title="选择整个文件夹并递归上传">选择文件夹</button>
      </div>
    </div>
    <span class="fo-arrow">→</span>
    <div class="fo-remote-wrap">
      <input class="fo-path-remote" type="text" placeholder="设备路径" spellcheck="false" />
      <button class="fo-browse-remote" title="浏览设备文件">🔍</button>
    </div>
    <button class="fo-exec" title="执行">▶</button>
    <button class="fo-close" title="关闭">✕</button>
  `;
  fileOps.appendChild(fileOpsInner);

  // Device browser dropdown (hidden by default)
  const devBrowser = document.createElement('div');
  devBrowser.className = 'device-browser';
  devBrowser.style.display = 'none';
  devBrowser.innerHTML = `
    <div class="dev-browser-hint"></div>
    <div class="dev-browser-path">
      <span class="dev-browser-path-text"></span>
      <span class="dev-browser-path-btns">
        <button class="dev-browser-select-cur" title="选择当前目录到输入框">📁 当前</button>
        <button class="dev-browser-confirm" title="确认选择">✓ 确认</button>
      </span>
    </div>
    <div class="dev-browser-list"></div>
    <div class="dev-browser-loading" style="display:none">加载中...</div>
    <div class="dev-browser-error" style="display:none"></div>
  `;
  fileOps.appendChild(devBrowser);

  // Device file edit form (independent from upload/download)
  const editOps = document.createElement('div');
  editOps.className = 'cell-toolbar-file-ops edit-file-ops';
  editOps.style.display = 'none';
  editOps.innerHTML = `
    <div class="file-ops-inner edit-ops-inner">
      <input class="edit-path-remote" type="text" placeholder="设备文件路径" spellcheck="false" />
      <button class="edit-browse" title="浏览选择设备文件">🔍</button>
      <button class="edit-inline edit-action-btn" title="直接在应用内编辑，手动保存">📝 直接编辑</button>
      <button class="edit-close" title="关闭">✕</button>
    </div>
  `;
  const editBrowser = document.createElement('div');
  editBrowser.className = 'device-browser edit-device-browser';
  editBrowser.style.display = 'none';
  editBrowser.innerHTML = `
    <div class="dev-browser-hint">点击目录进入，点击文件选中，双击直接选择</div>
    <div class="dev-browser-path">
      <span class="dev-browser-path-text"></span>
      <span class="dev-browser-path-btns">
        <button class="edit-browser-confirm" title="选择当前文件" disabled>✓ 选择文件</button>
      </span>
    </div>
    <div class="dev-browser-list"></div>
    <div class="dev-browser-loading" style="display:none">加载中...</div>
    <div class="dev-browser-error" style="display:none"></div>
  `;
  editOps.appendChild(editBrowser);
  toolbar.appendChild(editOps);

  toolbar.appendChild(toolbarRow);
  toolbar.appendChild(fileOps);

  // Log submenu (initially hidden)
  const logMenu = document.createElement('div');
  logMenu.className = 'log-submenu';
  logMenu.style.display = 'none';
  logMenu.innerHTML = `
    <div class="log-submenu-row">
      <input class="log-save-path" type="text" placeholder="日志文件路径..." spellcheck="false" />
      <button class="log-browse" title="选择或新建日志文件（已有文件将追加）">📁</button>
      <button class="log-timestamp-toggle selected" type="button" aria-pressed="true" title="每行写入 [YYYY-MM-DD HH:MM:SS] 时间戳">⏱ 时间戳</button>
      <button class="log-toggle primary">🔴 记录</button>
    </div>
  `;
  toolbar.appendChild(logMenu);

  // Commands popup (initially hidden)
  const cmdPopup = document.createElement('div');
  cmdPopup.className = 'command-popup';
  cmdPopup.style.display = 'none';
  cmdPopup.innerHTML = `
    <div class="cmd-popup-header">
      <span class="cmd-popup-title">快捷指令</span>
      <select class="cmd-group-select" title="切换指令集">
        <option value="0">默认</option>
      </select>
      <div style="flex:1"></div>
      <button class="cmd-popup-group-btn" title="管理指令集">◈</button>
      <button class="cmd-popup-add" title="新建指令">+</button>
    </div>
    <div class="cmd-popup-body"></div>
  `;
  toolbar.appendChild(cmdPopup);

  // Bottom handle
  const handleBot = document.createElement('div');
  handleBot.className = 'cell-toolbar-handle';
  handleBot.dataset.target = 'cmd-input';
  handleBot.title = '拖拽调整命令框高度';

  // Cmd input
  const cmdDiv = document.createElement('div');
  cmdDiv.className = 'terminal-cmd-input';
  cmdDiv.style.height = '34px';
  const cmdTextarea = document.createElement('textarea');
  cmdTextarea.className = 'terminal-cmd-textarea';
  cmdTextarea.placeholder = '输入命令，Enter 发送，Ctrl+Enter 换行';
  cmdTextarea.spellcheck = false;
  cmdDiv.appendChild(cmdTextarea);

  cellEl.appendChild(xtermDiv);
  cellEl.appendChild(handleTop);
  cellEl.appendChild(toolbar);
  cellEl.appendChild(handleBot);
  cellEl.appendChild(cmdDiv);

  return {
    el: cellEl, xtermDiv, toolbar, fileOps, cmdDiv, cmdTextarea,
    uploadBtn, downloadBtn, editBtn, editOps, editBrowser, logBtn, logMenu, cmdBtn, cmdPopup, devBrowser,
    foLocal: fileOps.querySelector('.fo-path-local'),
    foRemote: fileOps.querySelector('.fo-path-remote'),
    foExec: fileOps.querySelector('.fo-exec'),
    foClose: fileOps.querySelector('.fo-close'),
    foBrowseLocal: fileOps.querySelector('.fo-browse-local'),
    foLocalPicker: fileOps.querySelector('.fo-local-picker'),
    foLocalPickerMenu: fileOps.querySelector('.fo-local-picker-menu'),
    foPickFiles: fileOps.querySelector('.fo-pick-files'),
    foPickDirectory: fileOps.querySelector('.fo-pick-directory'),
    foBrowseRemote: fileOps.querySelector('.fo-browse-remote'),
    editRemote: editOps.querySelector('.edit-path-remote'),
    editBrowse: editOps.querySelector('.edit-browse'),
    editInline: editOps.querySelector('.edit-inline'),
    editClose: editOps.querySelector('.edit-close'),
  };
}

/** Create xterm terminal and fit addon */
export function _initTerminal(key, device, xtermDiv) {
  const term = new Terminal({
    cursorBlink: true,
    cursorStyle: 'block',
    convertEol: true, // bare LF = CRLF (fixes staircase output from scripts)
    rightClickSelectsWord: false, // right-click = paste, not word-select
    fontSize: state.fontSize,
    fontFamily: "'Cascadia Mono', 'Cascadia Code', 'Fira Code', 'Consolas', 'Segoe UI Symbol', 'Microsoft YaHei UI', monospace",
    theme: {
      background: '#1a1a2e', foreground: '#cdd6f4', cursor: '#89b4fa',
      selectionBackground: '#45475a',
      black: '#45475a', red: '#f38ba8', green: '#a6e3a1',
      yellow: '#f9e2af', blue: '#89b4fa', magenta: '#f5c2e7',
      cyan: '#94e2d5', white: '#bac2de',
      brightBlack: '#585b70', brightRed: '#f38ba8', brightGreen: '#a6e3a1',
      brightYellow: '#f9e2af', brightBlue: '#89b4fa', brightMagenta: '#f5c2e7',
      brightCyan: '#94e2d5', brightWhite: '#a6adc8',
    },
    allowTransparency: false,
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(xtermDiv);
  setTimeout(() => { try { fitAddon.fit(); } catch {} }, 50);

  // Select = auto copy to clipboard
  term.onSelectionChange(() => {
    const terminalState = state.terminals[key];
    if (terminalState && terminalState.suppressSelectionCopy) return;
    const sel = term.getSelection();
    if (sel) copyTextToClipboard(sel);
  });

  // Right-click = paste clipboard into terminal
  xtermDiv.addEventListener('contextmenu', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    let text = '';
    try {
      if (navigator.clipboard && navigator.clipboard.readText) {
        text = await navigator.clipboard.readText();
      }
    } catch (err) {}
    if (!text) text = await readClipboardLegacy();
    if (!text) return;
    // Inject text through xterm's hidden textarea (triggers onData → WS)
    const ta = term.textarea;
    try {
      if (ta) {
        ta.focus();
        ta.value = text;
        ta.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
        ta.value = '';
      } else {
        term.write(text);
      }
    } catch (err) {
      term.write(text);
    }
  });

  window.TerminalWindows?.record(key, term);
  return { term, fitAddon };
 }

// --- Clipboard helpers ---
export function copyTextToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => legacyCopy(text));
  } else {
    legacyCopy(text);
  }
}

export function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
}

export function readClipboardLegacy() {
  return new Promise((resolve) => {
    const ta = document.createElement('textarea');
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta);
    ta.focus();
    let settled = false;
    const finish = (val) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('paste', onPaste, true);
      document.body.removeChild(ta);
      resolve(val);
    };
    const onPaste = (e) => {
      const val = (e.clipboardData && e.clipboardData.getData('text')) || ta.value;
      finish(val || '');
    };
    document.addEventListener('paste', onPaste, true);
    try { document.execCommand('paste'); } catch (e) { finish(''); }
    setTimeout(() => finish(ta.value || ''), 150);
  });
}

/** Wire file-ops toolbar buttons for a terminal panel */
export function _wireFileOps(key, cell) {
  const t = state.terminals[key];
  const { fileOps } = t;

  const toggleFileOps = (op) => {
    reportUiAction('file-op-toggle', `${key}:${op}`);
    const active = fileOps.style.display !== 'none';
    // Close other menus in the same panel only
    t.logMenu.style.display = 'none';
    t.cmdPopup.style.display = 'none';
    // If already showing the same tool → close; otherwise switch to the new tool without closing
    if (active && fileOps.dataset.op === op) {
      fileOps.style.display = 'none';
      t.devBrowser.style.display = 'none';
      return;
    }
    // 切换上传/下载时同步关闭旧的设备目录弹窗，避免下一次点击被当成“关闭”。
    t.devBrowser.style.display = 'none';
    fileOps.style.display = 'flex';
    fileOps.dataset.op = op;
    const arrow = fileOps.querySelector('.fo-arrow');
    if (op === 'upload') {
      arrow.textContent = '→';
      cell.foLocal.placeholder = '本地文件/目录';
      cell.foRemote.placeholder = '设备目标路径';
      cell.foBrowseLocal.title = '选择本地文件或文件夹';
    } else {
      arrow.textContent = '←';
      cell.foLocal.placeholder = '本地保存路径';
      cell.foRemote.placeholder = '设备文件/目录';
      cell.foBrowseLocal.title = '选择本地保存目录';
    }
    cell.foLocalPickerMenu.style.display = 'none';
    selectedLocalPaths = [];
    selectedRemotePaths = [];
    const hist = loadPathHistory(key, op);
    cell.foLocal.value = hist.local || '';
    cell.foRemote.value = hist.remote || '';
    setTimeout(() => cell.foLocal.focus(), 50);
  };

  t.uploadBtn.addEventListener('click', () => toggleFileOps('upload'));
  t.downloadBtn.addEventListener('click', () => toggleFileOps('download'));

  t.foClose.addEventListener('click', () => {
    fileOps.style.display = 'none';
    t.devBrowser.style.display = 'none';
    t.foLocalPickerMenu.style.display = 'none';
  });

  const browserPathText = t.devBrowser.querySelector('.dev-browser-path-text');
  const browserList = t.devBrowser.querySelector('.dev-browser-list');
  const browserLoading = t.devBrowser.querySelector('.dev-browser-loading');
  const browserError = t.devBrowser.querySelector('.dev-browser-error');
  const browserSelectCur = t.devBrowser.querySelector('.dev-browser-select-cur');
  const browserConfirm = t.devBrowser.querySelector('.dev-browser-confirm');
  const browserHint = t.devBrowser.querySelector('.dev-browser-hint');

  let browserHistory = [];
  let selectedRemotePaths = [];
  let selectedLocalPaths = [];
  let lastBrowseDir = '/';
  let browseRequestId = 0;
  let localPickerActive = false;

  const selectionText = (paths) => paths.join('；');

  const setRemoteSelection = (paths) => {
    selectedRemotePaths = [...paths];
    if (selectedRemotePaths.length) {
      t.foRemote.value = selectionText(selectedRemotePaths);
      t.foRemote.title = selectedRemotePaths.join('\n');
    }
  };

  // 手动输入路径后，改用输入框中的单一路径，不能再沿用上次多选结果。
  t.foLocal.addEventListener('input', () => { selectedLocalPaths = []; t.foLocal.title = ''; });
  t.foRemote.addEventListener('input', () => { selectedRemotePaths = []; t.foRemote.title = ''; });

  async function loadRemoteDir(dirPath) {
    const requestId = ++browseRequestId;
    console.log('[BROWSE] loadRemoteDir called, dirPath:', dirPath);

    const op = t.fileOps.dataset.op || 'upload';
    const isUpload = op === 'upload';
    console.log('[BROWSE] op:', op, 'isUpload:', isUpload);

    // 多选仅在当前目录内有效；进入其他目录后清空选择。
    selectedRemotePaths = [];
    lastBrowseDir = dirPath || '/';

    // Set hint
    browserHint.textContent = isUpload
      ? '点击目录导航进入子目录，📁当前：选择此路径'
      : '点击文件可多选，双击仅选此文件；📁当前：选择当前路径';

    browserLoading.style.display = 'block';
    browserError.style.display = 'none';
    browserList.innerHTML = '';
    browserPathText.textContent = dirPath || '/';

    const tidMatch = t.key.match(/tid=(\d+)/);
    console.log('[BROWSE] t.key:', t.key, 'tidMatch:', tidMatch, 'serial:', t.device.serial);

    const wsPayload = {
      // 服务端按 key 选路：远端设备经中转执行，本机设备走本地 adb。
      key: t.key,
      serial: t.device.serial,
      transportId: tidMatch ? tidMatch[1] : '',
      path: dirPath || '/',
    };
    console.log('[BROWSE] calling list-device-dir with:', JSON.stringify(wsPayload));

    const result = await callWS('list-device-dir', wsPayload, 35000);
    if (requestId !== browseRequestId) return;
    console.log('[BROWSE] list-device-dir result:', JSON.stringify(result).substring(0, 500));

    browserLoading.style.display = 'none';

    if (!result.success) {
      console.error('[BROWSE] FAIL - listing failed:', result.output);
      browserError.textContent = result.output || '无法列出目录';
      browserError.style.display = 'block';
      return;
    }

    const entries = result.entries || [];
    console.log('[BROWSE] entries count:', entries.length);
    console.log('[BROWSE] entry names:', entries.map(e => e.name + (e.isDir ? '/' : '')));

    browserPathText.textContent = result.currentPath || dirPath;

    if (entries.length === 0) {
      console.log('[BROWSE] entries empty, showing (空目录)');
      browserList.innerHTML = '<div class="dev-browser-empty">(空目录)</div>';
      return;
    }

    const ul = document.createElement('ul');
    ul.className = 'dev-browser-items';

    // .. entry (parent directory) — not shown at root
    if (dirPath && dirPath !== '/') {
      const li = document.createElement('li');
      li.className = 'dev-browser-item dir';
      li.innerHTML = '<span class="browser-icon">📁</span> ..';
      li.addEventListener('click', () => {
        const parent = dirPath.replace(/\/+$/, '').split('/').slice(0, -1).join('/') || '/';
        loadRemoteDir(parent);
      });
      ul.appendChild(li);
    }

    for (const entry of entries) {
      if (entry.name === '.' || entry.name === '..') continue;

      const li = document.createElement('li');
      const icon = entry.isDir ? '📁' : '📄';

      if (isUpload) {
        // Upload mode: only directories shown
        if (!entry.isDir) continue;
        li.className = 'dev-browser-item dir';
        li.innerHTML = `<span class="browser-icon">${icon}</span> ${entry.name}`;
        li.addEventListener('click', () => loadRemoteDir(entry.path));
      } else {
        // Download mode: files + directories
        li.className = 'dev-browser-item' + (entry.isDir ? ' dir' : ' file');
        li.innerHTML = `<span class="browser-icon">${icon}</span> ${entry.name}`;
        li.dataset.path = entry.path;
        li.dataset.isDir = entry.isDir;

        if (entry.isDir) {
          li.addEventListener('click', () => loadRemoteDir(entry.path));
        } else {
          // 单击切换选中状态，可连续选择多个文件。
          li.addEventListener('click', () => {
            const index = selectedRemotePaths.indexOf(entry.path);
            if (index >= 0) {
              selectedRemotePaths.splice(index, 1);
              li.classList.remove('selected');
            } else {
              selectedRemotePaths.push(entry.path);
              li.classList.add('selected');
            }
            console.log('[BROWSE] selected files:', selectedRemotePaths);
          });
          // 双击仅选择该文件并立即确认，保留单文件快捷操作。
          li.addEventListener('dblclick', () => {
            console.log('[BROWSE] double-click, confirming:', entry.path);
            setRemoteSelection([entry.path]);
            t.devBrowser.style.display = 'none';
          });
        }
      }
      ul.appendChild(li);
    }
    browserList.appendChild(ul);
  }

  t.foBrowseRemote.addEventListener('click', async () => {
    reportUiAction('device-path-browse', `${key}:${t.fileOps.dataset.op || 'upload'}`);
    // Close other browsers in other panels
    for (const k of Object.keys(state.terminals)) {
      if (k !== key && state.terminals[k].devBrowser) state.terminals[k].devBrowser.style.display = 'none';
    }

    const isOpen = t.devBrowser.style.display === 'flex';
    if (isOpen) { t.devBrowser.style.display = 'none'; return; }

    // Position as fixed overlay relative to viewport
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const fileOpsRect = t.fileOps.getBoundingClientRect();
    const toolbarRowRect = t.toolbar.querySelector('.cell-toolbar-row').getBoundingClientRect();
    const popupWidth = 420;
    // 向上弹出时以按钮行作为边界，不能覆盖上传/下载按钮。
    const spaceAbove = toolbarRowRect.top;
    const spaceBelow = viewportHeight - fileOpsRect.bottom;
    const idealHeight = 360;

    // Horizontal: align left with fileOps, clamp to viewport
    const popupLeft = Math.max(4, Math.min(fileOpsRect.left, viewportWidth - popupWidth - 4));
    const actualWidth = Math.min(popupWidth, viewportWidth - popupLeft - 4);
    t.devBrowser.style.left = popupLeft + 'px';
    t.devBrowser.style.width = actualWidth + 'px';

    // Vertical: prefer below, fallback above, worst-case largest side
    if (spaceBelow >= 200) {
      t.devBrowser.style.top = (fileOpsRect.bottom + 4) + 'px';
      t.devBrowser.style.bottom = 'auto';
      t.devBrowser.style.maxHeight = Math.min(idealHeight, spaceBelow - 8) + 'px';
    } else if (spaceAbove >= 200) {
      t.devBrowser.style.top = 'auto';
      t.devBrowser.style.bottom = (viewportHeight - toolbarRowRect.top + 4) + 'px';
      t.devBrowser.style.maxHeight = Math.min(idealHeight, spaceAbove - 8) + 'px';
    } else {
      // Pick the larger side
      if (spaceBelow >= spaceAbove) {
        t.devBrowser.style.top = (fileOpsRect.bottom + 4) + 'px';
        t.devBrowser.style.bottom = 'auto';
      } else {
        t.devBrowser.style.top = 'auto';
        t.devBrowser.style.bottom = (viewportHeight - toolbarRowRect.top + 4) + 'px';
      }
      // 不足以容纳完整弹窗时也不能强行撑高，否则会盖住工具栏，后续点击像是无效。
      t.devBrowser.style.maxHeight = Math.max(80, Math.max(spaceAbove, spaceBelow) - 8) + 'px';
    }

    t.devBrowser.style.display = 'flex';

    // 多选后再次打开时，从上次浏览的目录继续，避免把“已选择 N 个文件”当作路径。
    const curPath = (t.fileOps.dataset.op === 'download' && selectedRemotePaths.length)
      ? lastBrowseDir
      : (t.foRemote.value.trim() || '/');
    await loadRemoteDir(curPath);
  });

  // "Select current directory" button
  browserSelectCur.addEventListener('click', () => {
    const op = t.fileOps.dataset.op || 'upload';
    // 下载模式优先使用已选择的一个或多个文件。
    if (op === 'download' && selectedRemotePaths.length) {
      setRemoteSelection(selectedRemotePaths);
    } else {
      selectedRemotePaths = [];
      t.foRemote.value = browserPathText.textContent;
    }
    t.devBrowser.style.display = 'none';
  });

  // "Confirm" button
  browserConfirm.addEventListener('click', () => {
    const op = t.fileOps.dataset.op || 'upload';
    if (op === 'download') {
      // 使用选中的一个或多个文件；未选文件时使用当前目录。
      if (selectedRemotePaths.length) setRemoteSelection(selectedRemotePaths);
      else t.foRemote.value = browserPathText.textContent;
    } else {
      // Upload mode: use current directory path
      selectedRemotePaths = [];
      t.foRemote.value = browserPathText.textContent;
    }
    t.devBrowser.style.display = 'none';
  });

  // Auto-close browser popup when clicking outside
  // 使用 click（而非 mousedown）关闭弹窗，避免先关闭弹窗后吞掉工具栏按钮的点击。
  document.addEventListener('click', (e) => {
    if (t.devBrowser.style.display !== 'flex') return;
    const target = e.target;
    if (t.devBrowser.contains(target)) return;
    if (t.foBrowseRemote.contains(target)) return;
    if (t.fileOps.contains(target) && !t.devBrowser.contains(target)) {
      t.devBrowser.style.display = 'none';
    }
  });

  document.addEventListener('click', (e) => {
    if (!t.foLocalPicker.contains(e.target)) t.foLocalPickerMenu.style.display = 'none';
  });

  const pickLocalPath = async (kind) => {
    if (localPickerActive) return;
    const op = t.fileOps.dataset.op || 'upload';
    const isDirectory = kind === 'directory';
    reportUiAction(isDirectory ? 'local-directory-browse' : 'local-path-browse', `${key}:${op}`);
    localPickerActive = true;
    t.foBrowseLocal.disabled = true;
    try {
      const result = isDirectory
        ? await callWS(
          'pick-directory', op === 'upload' ? { title: '选择要递归上传的文件夹' } : {}, 120000
        )
        : await callWS('pick-file', { multi: true }, 120000);
      const paths = Array.isArray(result.paths) && result.paths.length
        ? result.paths
        : (result.path ? [result.path] : []);
      if (result.success && paths.length) {
        selectedLocalPaths = paths;
        t.foLocal.value = selectionText(paths);
        t.foLocal.title = paths.join('\n');
      } else if (result.output) {
        showToast('error', '选择本地路径', result.output);
      }
    } finally {
      localPickerActive = false;
      t.foBrowseLocal.disabled = false;
    }
  };

  t.foBrowseLocal.addEventListener('click', () => {
    const op = t.fileOps.dataset.op || 'upload';
    if (op === 'download') {
      pickLocalPath('directory');
      return;
    }
    t.foLocalPickerMenu.style.display = t.foLocalPickerMenu.style.display === 'none' ? 'flex' : 'none';
  });
  t.foPickFiles.addEventListener('click', () => {
    t.foLocalPickerMenu.style.display = 'none';
    pickLocalPath('file');
  });
  t.foPickDirectory.addEventListener('click', () => {
    t.foLocalPickerMenu.style.display = 'none';
    pickLocalPath('directory');
  });

  t.foExec.addEventListener('click', async () => {
    const op = t.fileOps.dataset.op || 'upload';
    const localPath = t.foLocal.value.trim();
    const remotePath = t.foRemote.value.trim();
    const localPaths = selectedLocalPaths.length ? selectedLocalPaths : (localPath ? [localPath] : []);
    const remotePaths = selectedRemotePaths.length ? selectedRemotePaths : (remotePath ? [remotePath] : []);
    if (!localPaths.length || !remotePaths.length) return;
    const tidMatch = t.key.match(/tid=(\d+)/);
    const transportArgs = tidMatch ? ['-t', tidMatch[1]] : ['-s', t.device.serial];
    const transferArgs = op === 'upload'
      ? [...transportArgs, 'push', ...localPaths, remotePaths[0]]
      : [...transportArgs, 'pull', ...remotePaths, localPaths[0]];
    const makeExecutable = op === 'upload' && localPaths.some((path) => /\.sh$/i.test(path));
    const args = transferArgs.map((arg) => /[\s"]/.test(arg) ? `"${arg.replace(/"/g, '\\"')}"` : arg).join(' ');
    savePathHistory(t.key, op, localPaths[0], remotePaths[0]);
    const opLabel = op === 'upload' ? '↑ 上传' : '↓ 下载';
    showToast('info', `${opLabel} ${t.device.serial}`, `adb ${args}`);
    const result = await callWS(
      'exec-adb', { key: t.key, args, argv: transferArgs, makeExecutable }, isRemoteDevice(t.device) ? 600000 : 60000
    );
    if (result.success) { showToast('success', `${opLabel} 完成`, result.output || '执行成功'); }
    else { showToast('error', `${opLabel} 失败`, result.output || '执行出错'); }
  });

  // ====== Log recording ======
  const logSavePath = t.logMenu.querySelector('.log-save-path');
  const logBrowse = t.logMenu.querySelector('.log-browse');
  const logTimestampToggle = t.logMenu.querySelector('.log-timestamp-toggle');
  const logToggle = t.logMenu.querySelector('.log-toggle');

  // Restore saved path
  const savedPath = localStorage.getItem('terminal-save-path');
  if (savedPath) logSavePath.value = savedPath;

  // 默认启用每行时间戳；仅在用户明确关闭后保留原始终端输出。
  function setTimestampEnabled(enabled) {
    logTimestampToggle.dataset.enabled = String(enabled);
    logTimestampToggle.classList.toggle('selected', enabled);
    logTimestampToggle.setAttribute('aria-pressed', String(enabled));
    logTimestampToggle.textContent = enabled ? '⏱ 时间戳' : '时间戳';
  }
  setTimestampEnabled(localStorage.getItem('terminal-record-timestamps') !== 'false');

  logTimestampToggle.addEventListener('click', () => {
    const enabled = logTimestampToggle.dataset.enabled !== 'true';
    setTimestampEnabled(enabled);
    localStorage.setItem('terminal-record-timestamps', String(enabled));
  });

  // Toggle log submenu visibility
  t.logBtn.addEventListener('click', () => {
    const wasOpen = t.logMenu.style.display === 'block';
    // Close other menus in the same panel only
    t.fileOps.style.display = 'none';
    t.cmdPopup.style.display = 'none';
    t.logMenu.style.display = wasOpen ? 'none' : 'block';
  });

  // 使用一个“另存为”窗口：可直接选择已有文件追加，或在任意目录输入新文件名。
  logBrowse.addEventListener('click', async () => {
    const serial = (t.device && t.device.serial) || t.key || 'device';
    const now = new Date();
    const ts = now.getFullYear()
      + String(now.getMonth() + 1).padStart(2, '0')
      + String(now.getDate()).padStart(2, '0')
      + 'T'
      + String(now.getHours()).padStart(2, '0')
      + String(now.getMinutes()).padStart(2, '0')
      + String(now.getSeconds()).padStart(2, '0');
    const suggestedName = `adb-terminal-${serial}-${ts}.log`;
    const result = await callWS('pick-recording-file', { suggestedName });
    if (result.success && result.path) {
      logSavePath.value = result.path;
      localStorage.setItem('terminal-save-path', result.path);
    }
  });

  // Save path on input change
  logSavePath.addEventListener('change', () => {
    localStorage.setItem('terminal-save-path', logSavePath.value);
  });

  // Start/Stop toggle
  logToggle.addEventListener('click', async () => {
    const path = logSavePath.value.trim();
    if (!path) { showToast('error', '终端记录', '请先选择保存路径'); return; }

    if (logToggle.dataset.running === 'true') {
      // Stop
      const result = await callWS('stop-recording', { key: t.key });
      if (result.success) {
        logToggle.textContent = '🔴 记录';
        logToggle.dataset.running = 'false';
        logToggle.className = 'log-toggle primary';
        logSavePath.disabled = false;
        logBrowse.disabled = false;
        logTimestampToggle.disabled = false;
        showToast('info', '终端记录', '已停止');
      }
    } else {
      // Start
      showToast('info', '终端记录', '正在启动...');
      const result = await callWS('start-recording', {
        key: t.key,
        path: path,
        timestamps: logTimestampToggle.dataset.enabled === 'true',
      });
      if (result.success) {
        logToggle.textContent = '⏹ 停止';
        logToggle.dataset.running = 'true';
        logToggle.className = 'log-toggle danger';
        logSavePath.disabled = true;
        logBrowse.disabled = true;
        logTimestampToggle.disabled = true;
        showToast('info', '终端记录', result.output || '记录中');
      } else {
        showToast('error', '终端记录', result.output || '启动失败');
      }
    }
  });

  // ====== Quick commands popup with groups ======

  // Per-terminal selected group index
  t._cmdGroupIndex = 0;
  // Per-terminal command history (up to 50)
  t._cmdHistory = [];
  t._cmdHistoryIdx = -1;

  async function renderCommands() {
    const groups = await loadCommandGroups();
    if (!groups || groups.length === 0) return;
    const sel = t.cmdPopup.querySelector('.cmd-group-select');
    const title = t.cmdPopup.querySelector('.cmd-popup-title');

    // Rebuild group selector
    sel.innerHTML = '';
    for (let g = 0; g < groups.length; g++) {
      const opt = document.createElement('option');
      opt.value = g;
      opt.textContent = groups[g].name;
      sel.appendChild(opt);
    }
    // Clamp index
    if (t._cmdGroupIndex >= groups.length) t._cmdGroupIndex = 0;
    sel.value = t._cmdGroupIndex;
    title.textContent = groups.length > 1 ? `指令集: ${groups[t._cmdGroupIndex].name}` : '快捷指令';

    const group = groups[t._cmdGroupIndex];
    const cmds = group.commands;
    const body = t.cmdPopup.querySelector('.cmd-popup-body');
    body.innerHTML = '';
    if (!cmds || cmds.length === 0) {
      body.innerHTML = '<span style="font-size:11px;color:var(--text-dim);padding:8px;display:block;text-align:center">当前指令集为空，点击 + 添加</span>';
      return;
    }
    for (let i = 0; i < cmds.length; i++) {
      const c = cmds[i];
      const btn = document.createElement('button');
      btn.className = 'cmd-popup-btn';
      btn.dataset.index = i;
      btn.innerHTML = `<span class="cmd-btn-name">${c.name}</span><span class="cmd-btn-cmd">${c.cmd}</span>`;
      // Left click → send to terminal
      btn.addEventListener('click', () => {
        const text = c.cmd + '\n';
        sendWS({ type: 'write-terminal', key: t.key, data: text });
        showToast('info', '快捷指令', `发送: ${c.cmd}`);
      });
      // Right click → edit/delete menu
      btn.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        showCmdContextMenu(e, i);
      });
      body.appendChild(btn);
    }
  }

  // Group selector change
  t.cmdPopup.querySelector('.cmd-group-select').addEventListener('change', (e) => {
    t._cmdGroupIndex = parseInt(e.target.value, 10);
    renderCommands();
  });

  function showCmdContextMenu(e, index) {
    const old = document.querySelector('.cmd-context-menu');
    if (old) old.remove();

    const menu = document.createElement('div');
    menu.className = 'cmd-context-menu';
    menu.style.left = Math.min(e.clientX, window.innerWidth - 140) + 'px';
    menu.style.top = e.clientY + 'px';
    menu.innerHTML = `
      <div class="cmd-context-item" data-action="edit">✏️ 编辑</div>
      <div class="cmd-context-item" data-action="delete">🗑️ 删除</div>
    `;
    document.body.appendChild(menu);

    menu.querySelector('[data-action="edit"]').addEventListener('click', async () => {
      menu.remove();
      await showCmdEditDialog(index);
    });
    menu.querySelector('[data-action="delete"]').addEventListener('click', async () => {
      menu.remove();
      const groups = await loadCommandGroups();
      const group = groups[t._cmdGroupIndex];
      if (group && index >= 0 && index < group.commands.length) {
        group.commands.splice(index, 1);
        await saveCommandGroups(groups);
        await renderCommands();
        showToast('info', '快捷指令', '已删除');
      }
    });
    // Click outside to close
    setTimeout(() => {
      document.addEventListener('click', () => { if (menu.parentNode) menu.remove(); }, { once: true });
    }, 10);
  }

  async function showCmdEditDialog(index) {
    const groups = await loadCommandGroups();
    const group = groups[t._cmdGroupIndex];
    if (!group) return;
    const cmds = group.commands;
    const existing = (index >= 0 && index < cmds.length) ? cmds[index] : { name: '', cmd: '' };
    const overlay = document.createElement('div');
    overlay.className = 'cmd-edit-overlay';
    overlay.innerHTML = `
      <div class="cmd-edit-dialog">
        <h3>${index >= 0 && index < cmds.length ? '编辑指令' : '新建指令'}</h3>
        <label>名称</label>
        <input class="cmd-edit-name" value="${existing.name.replace(/"/g, '&quot;')}" placeholder="指令名称" spellcheck="false" />
        <label>命令</label>
        <input class="cmd-edit-cmd" value="${existing.cmd.replace(/"/g, '&quot;')}" placeholder="adb shell 命令" spellcheck="false" />
        <div class="cmd-edit-btns">
          <button class="cmd-edit-cancel">取消</button>
          <button class="cmd-edit-save primary">保存</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    overlay.querySelector('.cmd-edit-cancel').addEventListener('click', () => overlay.remove());
    overlay.querySelector('.cmd-edit-save').addEventListener('click', async () => {
      const name = overlay.querySelector('.cmd-edit-name').value.trim();
      const cmd = overlay.querySelector('.cmd-edit-cmd').value.trim();
      if (!name || !cmd) { showToast('error', '快捷指令', '名称和命令不能为空'); return; }
      if (index >= 0 && index < cmds.length) {
        cmds[index] = { name, cmd };
      } else {
        cmds.push({ name, cmd });
      }
      await saveCommandGroups(groups);
      await renderCommands();
      overlay.remove();
      showToast('success', '快捷指令', index >= 0 && index < cmds.length ? '已更新' : '已创建');
    });
  }

  // --- Group management ---
  t.cmdPopup.querySelector('.cmd-popup-group-btn').addEventListener('click', async () => {
    const groups = await loadCommandGroups();
    const overlay = document.createElement('div');
    overlay.className = 'cmd-edit-overlay';
    let itemsHtml = '';
    for (let g = 0; g < groups.length; g++) {
      const delBtn = groups.length > 1 ? `<button class="cmd-group-del" data-g="${g}" title="删除指令集">✕</button>` : '';
      itemsHtml += `<div class="cmd-group-mgr-row">
        <input class="cmd-group-mgr-name" data-g="${g}" value="${groups[g].name.replace(/"/g, '&quot;')}" />
        ${delBtn}
      </div>`;
    }
    overlay.innerHTML = `
      <div class="cmd-edit-dialog" style="min-width:280px">
        <h3>管理指令集</h3>
        <div class="cmd-group-mgr-list">${itemsHtml}</div>
        <div class="cmd-edit-btns" style="margin-top:8px">
          <button class="cmd-group-add">+ 新建指令集</button>
          <div style="flex:1"></div>
          <button class="cmd-edit-cancel">关闭</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    // Rename on blur/enter
    overlay.querySelectorAll('.cmd-group-mgr-name').forEach(inp => {
      inp.addEventListener('change', async () => {
        const g = parseInt(inp.dataset.g, 10);
        const grps = await loadCommandGroups();
        if (grps[g]) {
          grps[g].name = inp.value.trim() || grps[g].name;
          await saveCommandGroups(grps);
          await renderCommands();
        }
      });
    });
    // Delete group
    overlay.querySelectorAll('.cmd-group-del').forEach(btn => {
      btn.addEventListener('click', async () => {
        const g = parseInt(btn.dataset.g, 10);
        const grps = await loadCommandGroups();
        grps.splice(g, 1);
        if (t._cmdGroupIndex >= grps.length) t._cmdGroupIndex = 0;
        await saveCommandGroups(grps);
        overlay.remove();
        await renderCommands();
      });
    });
    // Add group
    overlay.querySelector('.cmd-group-add').addEventListener('click', async () => {
      const grps = await loadCommandGroups();
      grps.push({ name: `指令集 ${grps.length + 1}`, commands: [] });
      await saveCommandGroups(grps);
      t._cmdGroupIndex = grps.length - 1;
      overlay.remove();
      await renderCommands();
    });
    // Cancel
    overlay.querySelector('.cmd-edit-cancel').addEventListener('click', () => overlay.remove());
    // Click overlay to close
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.remove();
    });
  });

  // Cancel on overlay click (edit dialog)
  document.addEventListener('click', (e) => {
    const overlay = e.target.closest('.cmd-edit-overlay');
    if (overlay && e.target === overlay) overlay.remove();
  });

  // Toggle commands popup
  t.cmdBtn.addEventListener('click', async () => {
    const wasOpen = t.cmdPopup.style.display === 'block';
    // Close other menus in the same panel only
    t.logMenu.style.display = 'none';
    t.fileOps.style.display = 'none';
    if (!wasOpen) await renderCommands();
    t.cmdPopup.style.display = wasOpen ? 'none' : 'block';
  });

  // Add button
  t.cmdPopup.querySelector('.cmd-popup-add').addEventListener('click', () => {
    showCmdEditDialog(-1);
  });
}

export function decodeUtf8Base64(value) {
  const binary = atob(value || '');
  const bytes = Uint8Array.from(binary, ch => ch.charCodeAt(0));
  return new TextDecoder('utf-8', {fatal: true}).decode(bytes);
}

export function encodeUtf8Base64(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

/** Wire the independent device-file edit toolbar. */
export function _wireDeviceFileEdit(key, cell) {
  const t = state.terminals[key];
  const { editBtn, editOps, editBrowser, editRemote, editBrowse,
    editInline, editClose } = t;
  const browserPathText = editBrowser.querySelector('.dev-browser-path-text');
  const browserList = editBrowser.querySelector('.dev-browser-list');
  const browserLoading = editBrowser.querySelector('.dev-browser-loading');
  const browserError = editBrowser.querySelector('.dev-browser-error');
  const browserConfirm = editBrowser.querySelector('.edit-browser-confirm');
  let selectedPath = '';
  let lastBrowseDir = '/';  // 模块是严格模式：这里不能再依赖经典脚本的隐式全局
  let inlineOverlay = null;
  let inlineTextarea = null;
  let inlineActive = false;
  let inlineDirty = false;
  let inlineSaveInFlight = false;

  const updateEditState = () => {
    editInline.disabled = !editRemote.value.trim();
  };

  const positionBrowser = () => {
    const rect = editOps.getBoundingClientRect();
    const width = Math.min(420, window.innerWidth - 8);
    const below = window.innerHeight - rect.bottom;
    const above = rect.top;
    editBrowser.style.left = Math.max(4, Math.min(rect.left, window.innerWidth - width - 4)) + 'px';
    editBrowser.style.width = width + 'px';
    if (below >= 200 || below >= above) {
      editBrowser.style.top = (rect.bottom + 4) + 'px';
      editBrowser.style.bottom = 'auto';
      editBrowser.style.maxHeight = Math.max(80, Math.min(360, below - 8)) + 'px';
    } else {
      editBrowser.style.top = 'auto';
      editBrowser.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
      editBrowser.style.maxHeight = Math.max(80, Math.min(360, above - 8)) + 'px';
    }
  };

  const loadEditDir = async (dirPath) => {
    selectedPath = '';
    browserConfirm.disabled = true;
    browserLoading.style.display = 'block';
    browserError.style.display = 'none';
    browserList.innerHTML = '';
    browserPathText.textContent = dirPath || '/';

    const tidMatch = t.key.match(/tid=(\d+)/);
    const result = await callWS('list-device-dir', {
      key: t.key,
      serial: t.device.serial,
      transportId: tidMatch ? tidMatch[1] : '',
      path: dirPath || '/',
    }, 30000);
    browserLoading.style.display = 'none';
    if (!result.success) {
      browserError.textContent = result.output || '无法列出目录';
      browserError.style.display = 'block';
      return;
    }

    browserPathText.textContent = result.currentPath || dirPath;
    lastBrowseDir = browserPathText.textContent || '/';
    const ul = document.createElement('ul');
    ul.className = 'dev-browser-items';
    if (dirPath && dirPath !== '/') {
      const parent = document.createElement('li');
      parent.className = 'dev-browser-item dir';
      parent.innerHTML = '<span class="browser-icon">📁</span> ..';
      parent.addEventListener('click', () => {
        const path = dirPath.replace(/\/+$/, '').split('/').slice(0, -1).join('/') || '/';
        loadEditDir(path);
      });
      ul.appendChild(parent);
    }

    for (const entry of result.entries || []) {
      const li = document.createElement('li');
      li.className = 'dev-browser-item' + (entry.isDir ? ' dir' : ' file');
      li.innerHTML = `<span class="browser-icon">${entry.isDir ? '📁' : '📄'}</span> ${entry.name}`;
      if (entry.isDir) {
        li.addEventListener('click', () => loadEditDir(entry.path));
      } else {
        li.addEventListener('click', () => {
          const previous = editBrowser.querySelector('.dev-browser-item.file.selected');
          if (previous) previous.classList.remove('selected');
          li.classList.add('selected');
          selectedPath = entry.path;
          editRemote.value = entry.path;
          browserConfirm.disabled = false;
          updateEditState();
        });
        li.addEventListener('dblclick', () => {
          editRemote.value = entry.path;
          editBrowser.style.display = 'none';
          updateEditState();
        });
      }
      ul.appendChild(li);
    }
    if (!ul.children.length) {
      browserList.innerHTML = '<div class="dev-browser-empty">(空目录)</div>';
    } else {
      browserList.appendChild(ul);
    }
  };

  editBtn.addEventListener('click', async () => {
    const open = editOps.style.display === 'flex';
    t.fileOps.style.display = 'none';
    t.devBrowser.style.display = 'none';
    t.logMenu.style.display = 'none';
    t.cmdPopup.style.display = 'none';
    editOps.style.display = open ? 'none' : 'flex';
    editBrowser.style.display = 'none';
    updateEditState();
    if (!open) setTimeout(() => editRemote.focus(), 50);
  });

  editRemote.addEventListener('input', updateEditState);
  editBrowse.addEventListener('click', async () => {
    for (const k of Object.keys(state.terminals)) {
      if (k !== key && state.terminals[k].editBrowser) {
        state.terminals[k].editBrowser.style.display = 'none';
      }
    }
    const open = editBrowser.style.display === 'flex';
    if (open) { editBrowser.style.display = 'none'; return; }
    positionBrowser();
    editBrowser.style.display = 'flex';
    await loadEditDir(editRemote.value.trim() || '/');
  });

  browserConfirm.addEventListener('click', () => {
    if (selectedPath) editRemote.value = selectedPath;
    editBrowser.style.display = 'none';
    updateEditState();
  });

  const closeInlineEditor = async () => {
    if (inlineDirty && !window.confirm('当前有未保存修改，确定关闭并放弃吗？')) return;
    const hadSession = inlineActive;
    inlineActive = false;
    inlineDirty = false;
    const overlay = inlineOverlay;
    inlineOverlay = null;
    inlineTextarea = null;
    if (overlay) overlay.remove();
    if (hadSession) await callWS('close-inline-edit', { key: t.key });
  };

  const saveInlineEditor = async () => {
    if (!inlineActive || !inlineTextarea) return;
    if (inlineSaveInFlight) {
      return;
    }
    const overlay = inlineOverlay;
    const textarea = inlineTextarea;
    const status = overlay && overlay.querySelector('.inline-edit-status');
    inlineSaveInFlight = true;
    if (status) status.textContent = '保存中...';
    const result = await callWS('save-inline-edit', {
      key: t.key,
      content: encodeUtf8Base64(textarea.value),
    }, 75000);
    inlineSaveInFlight = false;
    if (inlineActive && inlineOverlay === overlay && status) {
      status.textContent = result.success
        ? `已手动保存 ${new Date().toLocaleTimeString()}`
        : `保存失败：${result.output || '未知错误'}`;
    }
    if (result.success) inlineDirty = false;
  };

  const openInlineEditor = async () => {
    const remotePath = editRemote.value.trim();
    if (!remotePath) return;
    editInline.disabled = true;
    const tidMatch = t.key.match(/tid=(\d+)/);
    const result = await callWS('start-inline-edit', {
      key: t.key,
      serial: t.device.serial,
      transportId: tidMatch ? tidMatch[1] : '',
      path: remotePath,
    }, 75000);
    editInline.disabled = false;
    if (!result.success) {
      updateEditState();
      showToast('error', '直接编辑设备文件', result.output || '读取失败');
      return;
    }
    let content;
    try {
      content = decodeUtf8Base64(result.content);
    } catch (error) {
      await callWS('close-inline-edit', { key: t.key });
      updateEditState();
      showToast('error', '直接编辑设备文件', '设备文件不是有效的 UTF-8 文本');
      return;
    }

    const overlay = document.createElement('div');
    overlay.className = 'inline-edit-overlay';
    overlay.innerHTML = `
      <div class="inline-edit-dialog">
        <div class="inline-edit-header">
          <div class="inline-edit-title">直接编辑设备文件</div>
          <button class="inline-edit-close" title="关闭">✕</button>
        </div>
        <div class="inline-edit-path"></div>
        <textarea class="inline-edit-text" spellcheck="false"></textarea>
        <div class="inline-edit-footer">
          <span class="inline-edit-status">已读取，修改后请点击“立即保存”</span>
          <button class="inline-edit-save primary">立即保存</button>
        </div>
      </div>
    `;
    overlay.querySelector('.inline-edit-path').textContent = remotePath;
    const textarea = overlay.querySelector('.inline-edit-text');
    textarea.value = content;
    document.body.appendChild(overlay);
    inlineOverlay = overlay;
    inlineTextarea = textarea;
    inlineActive = true;

    const markInlineDirty = () => {
      inlineDirty = true;
      const status = overlay.querySelector('.inline-edit-status');
      if (status) status.textContent = '有未保存修改，请点击“立即保存”';
    };
    textarea.addEventListener('input', markInlineDirty);
    overlay.querySelector('.inline-edit-save').addEventListener('click', () => {
      saveInlineEditor();
    });
    overlay.querySelector('.inline-edit-close').addEventListener('click', () => closeInlineEditor());
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) closeInlineEditor();
    });
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  };

  editInline.addEventListener('click', openInlineEditor);

  editClose.addEventListener('click', () => {
    editOps.style.display = 'none';
    editBrowser.style.display = 'none';
  });

  document.addEventListener('mousedown', (e) => {
    if (editBrowser.style.display !== 'flex') return;
    if (editBrowser.contains(e.target) || editBrowse.contains(e.target)) return;
    editBrowser.style.display = 'none';
  });

  updateEditState();
}

export function removeTerminalPanel(key) {
  const t = state.terminals[key];
  if (t) {
    if (t.computer) { t.close(); return; }
    t.windowManager?.destroy();
    try { t.term.dispose(); } catch {}
    if (t.panel.parentNode) t.panel.parentNode.removeChild(t.panel);
    delete state.terminals[key];
  }
  terminalDecoders.delete(key);
  delete state.terminalBuffer[key];
}

export async function dismissTerminal(key) {
  state.dismissedKeys[key] = true;
  // 串口标题栏关闭是释放接入，不得被手动刷新重新打开。
  window.TerminalWindows?.dismiss(key, {restore: !state.terminals[key]?.serialPort});
  if (state.terminals[key]?.computer) { await state.terminals[key].close(); return; }
  sendWS({type: 'close-terminal', key});
  state.knownKeys = state.knownKeys.filter(candidate => candidate !== key);
  updateTerminalPanels(); updateDeviceTabs(); updateLayoutStatus(); updateStatusBar();
}

export function updateTerminalPanels() {
  for (const key of Object.keys(state.terminals)) {
    if (!state.knownKeys.includes(key) && !state.terminals[key].computer) {
      removeTerminalPanel(key);
    }
  }

  for (const key of state.knownKeys) {
    const dev = state.devices.find(d => _deviceKey(d) === key);
    if (!state.terminals[key]) {
      if (dev) createTerminalPanel(key, dev);
    }
    // 设备 key 不变时也要刷新部署标签，不能只在第一次创建面板时填写。
    if (dev && state.terminals[key]) {
      const panel = state.terminals[key].panel;
      const title = panel.querySelector('.header-title');
      if (title) title.textContent = deviceLabel(dev);
      const badge = panel.querySelector('.remote-badge');
      if (badge && dev.relay) {
        const phase = remoteTerminalState(key, dev);
        badge.textContent = `远端 ${relayPeerLabel(dev.relay)} · ${phase}`;
        badge.title = phase === '终端准备中'
          ? '正在启动远端终端；此时输入会排队，准备完成后按顺序发送'
          : `远端设备：由 ${relayPeerLabel(dev.relay)} 提供；${phase}`;
        const input = panel.querySelector('.terminal-cmd-input textarea');
        if (input) input.placeholder = phase === '终端准备中'
          ? '终端准备中，输入将排队；就绪后发送'
          : '输入命令，Enter 发送，Ctrl+Enter 换行';
      }
    }
  }

  syncTerminalControls();
  applyLayout(state.layout);
}

// 电脑终端由界面创建，不能被设备广播覆盖，也不占 ADB 设备配额。
export function terminalPanelKeys() {
  return [...state.knownKeys, ...Object.keys(state.terminals).filter(key => state.terminals[key].computer)];
}

export function applyLayout(layout) {
  const container = document.getElementById('terminal-container');
  container.className = 'terminal-container';
  const count = terminalPanelKeys().filter(k => state.terminals[k] && !state.terminals[k].floatingWindow && !state.terminals[k].panel.classList.contains('minimized')).length;
  container.classList.add(`device-count-${Math.min(count, 4)}`);
  container.style.setProperty('--terminal-columns', count < 2 ? 1 : 2);
  container.style.setProperty('--terminal-rows', Math.max(1, Math.ceil(count / 2)));

  if (layout === 'single') {
    container.classList.add('layout-single');
    // 浮动工具获得焦点时，保留原 ADB 单屏设备，不让主终端变成空白。
    const docked = terminalPanelKeys().filter(key => state.terminals[key] && !state.terminals[key].floatingWindow && !state.terminals[key].panel.classList.contains('minimized'));
    const layoutDevice = docked.includes(state.activeDevice) ? state.activeDevice
      : docked.includes(state.layoutActiveDevice) ? state.layoutActiveDevice : docked[0];
    for (const key of Object.keys(state.terminals)) {
      const pt = state.terminals[key];
      if (pt.floatingWindow) continue;
      if (key === layoutDevice) {
        pt.panel.style.display = '';
      } else {
        pt.panel.style.display = 'none';
      }
    }
  } else {
    container.classList.add(`layout-${layout}`);
    for (const key of Object.keys(state.terminals)) {
      if (state.terminals[key].floatingWindow) continue;
      state.terminals[key].panel.style.display = '';
    }
  }

  setTimeout(refreshAllTerminals, 150);
}

export function refreshAllTerminals() {
  for (const key of Object.keys(state.terminals)) {
    try {
      state.terminals[key].fitAddon.fit();
      state.terminals[key].windowManager?.snapshot();
    } catch {}
  }
}

// ============================================================
// Layout
// ============================================================
export function setLayout(layout) {
  state.layout = layout;
  $$('.layout-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.layout === layout);
  });
  applyLayout(layout);
  updateLayoutStatus();
}

export function initLayoutButtons() {
  $$('.layout-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      setLayout(btn.dataset.layout);
    });
  });

  const resetBtn = document.getElementById('layout-reset-btn');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      resetLayout();
    });
  }
}

// 恢复终端区域的初始布局：网格、全部展开、默认命令框高度
export function resetLayout() {
  window.TerminalWindows?.dockAll();
  state.activeDevice = null;
  _activePanel = null;

  for (const key of Object.keys(state.terminals)) {
    const t = state.terminals[key];
    if (t.floatingWindow) continue;
    t.panel.classList.remove('minimized', 'highlight');
    t.panel.style.display = '';
    const cmdInput = t.panel.querySelector('.terminal-cmd-input');
    if (cmdInput) cmdInput.style.height = '34px';
  }

  setLayout('grid');
  updateLayoutStatus();
  setTimeout(refreshAllTerminals, 150);
  showToast('info', '恢复布局', '已恢复为网格布局');
}

export function updateLayoutStatus() {
  const el = document.getElementById('layout-status');
  const visibleCount = terminalPanelKeys().filter(k => state.terminals[k] && !state.terminals[k].panel.classList.contains('minimized')).length;
  const names = {grid: '网格', horizontal: '水平单行', vertical: '垂直', single: '单屏'};
  const sameAsGrid = state.layout === 'horizontal' && visibleCount === 2;
  const computers = Object.values(state.terminals).filter(t => t.computer && !t.serialPort).length;
  const serials = Object.values(state.terminals).filter(t => t.serialPort).length;
  el.textContent = `${state.knownKeys.length} 台设备${computers ? ` · ${computers} 个 PowerShell` : ''}${serials ? ` · ${serials} 个串口` : ''} | ${names[state.layout] || state.layout}${sameAsGrid ? '（两台时与网格相同）' : ''}`;
  document.getElementById('status-layout').textContent = `布局: ${names[state.layout] || state.layout}`;
}

// ============================================================
// Status bar
// ============================================================
export function updateStatusBar() {
  const localCount = state.devices.filter(d => !isRemoteDevice(d)).length;
  const remoteCount = state.devices.filter(isRemoteDevice).length;
  const parts = [`${localCount} 台本机设备`];
  if (remoteCount) {
    const remoteOnline = state.devices.filter(d => isRemoteDevice(d) && d.relay.online !== false).length;
    parts.push(`${remoteOnline}/${remoteCount} 台远端设备在线`);
  }
  parts.push(`${terminalPanelKeys().length} 个已接入窗口`);
  const el = document.getElementById('status-devices');
  if (el) el.textContent = parts.join(' | ');
}

// ============================================================
// Select menus
// ============================================================
export function updateSelectMenus() {
  const selects = ['file-device', 'diag-device'];
  for (const id of selects) {
    const sel = document.getElementById(id);
    if (!sel) continue;
    const current = sel.value;
    sel.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = id === 'diag-device' ? '不检查特定设备' : '选择设备';
    sel.appendChild(placeholder);
    for (const d of state.devices) {
      // ADB 诊断只针对本机 adb 与它的设备：远端设备不在本机 adb 里，列进去只会误导。
      if (id === 'diag-device' && isRemoteDevice(d)) continue;
      const opt = document.createElement('option');
      opt.value = _deviceKey(d);
      opt.textContent = `${deviceLabel(d)} [${d.state || 'device'}]`;
      if (d.state !== 'device' && !isRemoteDevice(d)) opt.style.color = '#f38ba8';
      if (isRemoteDevice(d) && d.relay.online === false) opt.style.color = '#f38ba8';
      sel.appendChild(opt);
    }
    if (current) sel.value = current;
  }
}

// ============================================================
// Terminal recording — moved to toolbar log button
// ============================================================

// --- Device refresh ---
export function initDeviceRefresh() {
  const btn = document.getElementById('device-refresh-btn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    state.dismissedKeys = {};
    const fresh = [];
    for (const d of state.devices) {
      if (d.relay && d.relay.online === false) continue;
      const key = _deviceKey(d);
      if (!fresh.includes(key)) fresh.push(key);
    }
    state.knownKeys = fresh.slice(0, state.maxDevices);
    updateDeviceTabs();
    updateTerminalPanels();
    await window.TerminalWindows?.refreshClosed();
  });
}


// ============================================================
// Toolbar file ops helpers (path history via localStorage)
// ============================================================
export const FILE_HISTORY_KEY = 'adb-file-history';

export function loadPathHistory(terminalKey, op) {
  try {
    const raw = localStorage.getItem(FILE_HISTORY_KEY);
    const all = raw ? JSON.parse(raw) : {};
    const entry = all[terminalKey] || {};
    return entry[op] || { local: '', remote: '' };
  } catch { return { local: '', remote: '' }; }
}

export function savePathHistory(terminalKey, op, localPath, remotePath) {
  try {
    const raw = localStorage.getItem(FILE_HISTORY_KEY);
    const all = raw ? JSON.parse(raw) : {};
    if (!all[terminalKey]) all[terminalKey] = {};
    all[terminalKey][op] = { local: localPath, remote: remotePath };
    localStorage.setItem(FILE_HISTORY_KEY, JSON.stringify(all));
  } catch {}
}

// ============================================================
// Settings & debug log
// ============================================================
// Settings & debug log
// ============================================================
export function initSettings() {
  const fontInput = document.getElementById('set-font-size');
  if (!fontInput) return;
  fontInput.addEventListener('change', () => {
    const v = parseInt(fontInput.value, 10);
    if (v >= 8 && v <= 32) {
      state.fontSize = v;
      for (const key of Object.keys(state.terminals)) {
        try { state.terminals[key].term.options.fontSize = v; } catch {}
      }
    }
  });

  // Max devices
  const maxInput = document.getElementById('set-max-devices');
  if (maxInput) {
    maxInput.value = state.maxDevices;
    maxInput.addEventListener('change', () => {
      const v = parseInt(maxInput.value, 10);
      if (v >= 1 && v <= 20) {
        state.maxDevices = v;
        // Re-apply cap to currently known keys
        state.knownKeys = state.knownKeys.slice(0, state.maxDevices);
        updateDeviceTabs();
        updateTerminalPanels();
      }
    });
  }

  // Debug log (merged into settings)
  const refreshBtn = document.getElementById('debug-refresh');
  const clearBtn = document.getElementById('debug-clear');
  const output = document.getElementById('debug-output');
  if (refreshBtn && output) {
    refreshBtn.addEventListener('click', async () => {
      const result = await callWS('get-log');
      output.textContent = result.output || '';
      output.scrollTop = output.scrollHeight;
    });
  }
  if (clearBtn && output) {
    clearBtn.addEventListener('click', async () => {
      const result = await callWS('clear-log');
      output.textContent = result.success ? '' : (result.output || '清空日志失败');
    });
  }

  initAdbDiagnostic();
}

// ============================================================
// ADB 只读诊断
// ============================================================
export function initAdbDiagnostic() {
  const openBtn = document.getElementById('device-diagnostic-btn');
  const overlay = document.getElementById('diag-overlay');
  const closeBtn = document.getElementById('diag-close');
  const startBtn = document.getElementById('diag-start');
  const cancelBtn = document.getElementById('diag-cancel');
  const copyBtn = document.getElementById('diag-copy');
  const deviceSelect = document.getElementById('diag-device');
  if (!openBtn || !overlay || !closeBtn || !startBtn || !cancelBtn || !copyBtn || !deviceSelect) return;

  updateSelectMenus();
  openBtn.addEventListener('click', () => {
    window.ToolPanels.diagnostic.show();
    updateSelectMenus();
    setDiagnosticControls();
  });
  const closeDiagnostic = () => {
    window.ToolPanels.diagnostic.hide();
  };
  closeBtn.addEventListener('click', closeDiagnostic);
  overlay.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.stopPropagation(); closeDiagnostic(); }
  });
  startBtn.addEventListener('click', async () => {
    if (state.diagnostics.running) return;
    const selected = state.devices.find(d => _deviceKey(d) === deviceSelect.value) || null;
    state.diagnostics.running = true;
    state.diagnostics.report = null;
    setDiagnosticControls();
    setDiagnosticProgress('正在启动诊断…');
    clearDiagnosticResults();
    const result = await callWS('run-adb-diagnostic', { selectedDevice: selected }, 35000);
    state.diagnostics.running = false;
    state.diagnostics.requestId = null;
    setDiagnosticControls();
    if (result.success && result.report) {
      state.diagnostics.report = result.report;
      renderDiagnosticReport(result.report);
      return;
    }
    setDiagnosticProgress(`诊断失败：${result.output || '未知错误'}`);
    showToast('error', 'ADB 诊断', result.output || '诊断未返回结果');
  });

  cancelBtn.addEventListener('click', () => {
    const id = state.diagnostics.requestId;
    if (!state.diagnostics.running || id === null) return;
    cancelBtn.disabled = true;
    setDiagnosticProgress('正在取消诊断…');
    sendWS({ type: 'cancel-adb-diagnostic', id });
  });

  copyBtn.addEventListener('click', () => {
    if (!state.diagnostics.report) return;
    copyTextToClipboard(formatDiagnosticReport(state.diagnostics.report));
    showToast('success', 'ADB 诊断', '诊断报告已复制到剪贴板');
  });
}

export function setDiagnosticControls() {
  const startBtn = document.getElementById('diag-start');
  const cancelBtn = document.getElementById('diag-cancel');
  const copyBtn = document.getElementById('diag-copy');
  const running = state.diagnostics.running;
  if (startBtn) startBtn.disabled = running;
  if (cancelBtn) cancelBtn.disabled = !running;
  if (copyBtn) copyBtn.disabled = running || !state.diagnostics.report;
}

export function setDiagnosticProgress(text) {
  const el = document.getElementById('diag-progress');
  if (el) el.textContent = text;
}

export function handleDiagnosticProgress(msg) {
  if (!state.diagnostics.running) return;
  state.diagnostics.requestId = msg.id;
  setDiagnosticProgress(`正在检查 ${msg.current}/${msg.total}：${msg.title}`);
}

export function handleDiagnosticCancelled(msg) {
  if (state.diagnostics.running && msg.success) setDiagnosticProgress('正在取消诊断…');
}

export function clearDiagnosticResults() {
  const results = document.getElementById('diag-results');
  if (results) results.replaceChildren();
}

export function appendDiagnosticText(parent, tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text || '';
  parent.appendChild(el);
  return el;
}

export function renderDiagnosticReport(report) {
  const results = document.getElementById('diag-results');
  if (!results) return;
  results.replaceChildren();
  const checks = report.checks || [];
  const counts = { pass: 0, warn: 0, fail: 0, skip: 0, cancelled: 0 };
  checks.forEach(check => { counts[check.status] = (counts[check.status] || 0) + 1; });
  const primary = report.primary_action || {};
  const priority = primary.priority || '无';
  const header = document.createElement('div');
  header.className = `diagnostic-primary diagnostic-priority-${priority.toLowerCase()}`;
  appendDiagnosticText(header, 'strong', '', `首要修复：${priority} — ${primary.title || '未发现问题'}`);
  appendDiagnosticText(header, 'span', '', primary.summary || '');
  if (primary.advice) appendDiagnosticText(header, 'span', 'diagnostic-advice', primary.advice);
  results.appendChild(header);
  appendDiagnosticText(
    results, 'div', 'diagnostic-counts',
    `报告 ${report.id || ''}｜通过 ${counts.pass}｜警告 ${counts.warn}｜失败 ${counts.fail}｜跳过 ${counts.skip}｜取消 ${counts.cancelled}`,
  );
  checks.forEach(check => {
    const details = document.createElement('details');
    details.className = `diagnostic-check diagnostic-status-${check.status}`;
    if (check.status !== 'pass') details.open = true;
    const summary = document.createElement('summary');
    summary.textContent = `${check.status.toUpperCase()}｜${check.title}｜${check.summary}`;
    details.appendChild(summary);
    if (check.advice) appendDiagnosticText(details, 'div', 'diagnostic-advice', `建议：${check.advice}`);
    if (check.command && check.command.length) appendDiagnosticText(details, 'pre', 'diagnostic-command', `$ ${check.command.join(' ')}`);
    if (check.stdout) appendDiagnosticText(details, 'pre', 'diagnostic-output', `stdout\n${check.stdout}`);
    if (check.stderr) appendDiagnosticText(details, 'pre', 'diagnostic-output', `stderr\n${check.stderr}`);
    results.appendChild(details);
  });
  setDiagnosticProgress(`诊断完成：${report.finished_at || ''}`);
  setDiagnosticControls();
}

export function formatDiagnosticReport(report) {
  const lines = [
    'DevMux - ADB 诊断报告',
    `报告 ID: ${report.id || ''}`,
    `开始: ${report.started_at || ''}`,
    `结束: ${report.finished_at || ''}`,
  ];
  const primary = report.primary_action || {};
  lines.push(`首要修复: ${primary.priority || '无'} - ${primary.title || ''}`, primary.summary || '', primary.advice || '');
  for (const check of report.checks || []) {
    lines.push('', `[${check.status}] ${check.title}`, check.summary || '');
    if (check.advice) lines.push(`建议: ${check.advice}`);
    if (check.command && check.command.length) lines.push(`命令: ${check.command.join(' ')}`);
    if (check.stdout) lines.push(`stdout:\n${check.stdout}`);
    if (check.stderr) lines.push(`stderr:\n${check.stderr}`);
  }
  return lines.filter((line, index) => line || index === 0).join('\n');
}
