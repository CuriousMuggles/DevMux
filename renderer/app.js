// ============================================================
// DevMux - Frontend (WebSocket version)
// ============================================================
console.log('[APP] v=51 loaded, CMD_STORAGE_KEY=' + 'quick-commands-data');

const WS_URL = `ws://127.0.0.1:${typeof WS_PORT !== 'undefined' ? WS_PORT : 8765}`;

// Drag state stored per-element via dataset (no global shared state)
function initDragResize() {
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
let _activePanel = null;

function initHighlight() {
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

// Global application state
const state = {
  devices: [],
  knownKeys: [],
  terminals: {},
  terminalBuffer: {},
  activeDevice: null,
  layout: 'grid',
  currentTab: 'terminal',
  dismissedKeys: {},  // keys the user manually closed — never auto-re-add
  fontSize: 13,
  maxDevices: 6,
  diagnostics: { running: false, requestId: null, report: null },
  find: { targetKey: null, matches: [], currentIndex: -1 },
  // 跨机中转：状态快照、待确认对端、配置表单是否已加载
  relay: { status: null, config: null, carriers: [], configPath: '', pendingPeer: null, loaded: false, ignoredPeers: {} },
};

// 每个终端保留一个 UTF-8 解码器，避免多字节字符恰好跨 WebSocket 消息时
// 被分成两个替换字符。xterm 的字体同时保留符号字体作为缺字回退。
const terminalDecoders = new Map();

let ws = null;
let wsReconnectTimer = null;
let reqIdCounter = 0;
const pendingReqs = new Map();

// Quick-commands groups (each group has a name + commands array)
const DEFAULT_COMMAND_GROUPS = [
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

let _cachedGroups = null;

async function loadCommandGroups() {
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

async function saveCommandGroups(groups) {
  _cachedGroups = groups;
  await callWS('save-commands', { commands: groups });
}

// Expose for debugging
window.__debugCommands = function() {
  console.log('cached groups:', _cachedGroups);
  console.log('default groups:', DEFAULT_COMMAND_GROUPS);
};

// DOM helpers
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ============================================================
// WebSocket layer
// ============================================================
function connectWS() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    console.error('WS connect error:', e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    console.log('WS connected');
    updateConnectionStatus(true);
    // 中转状态与配置在连接建立后拉取（断线重连时也刷新）。配置不许盖掉用户正在
    // 弹窗里编、还没保存的改动，那种情况下等他保存或点刷新即可。
    refreshRelayStatus();
    if (!relayConfigDialogOpen() || !relayConfigDirty()) loadRelayConfig();
  };

  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleWSMessage(msg);
    } catch (e) {
      console.error('WS parse error:', e);
    }
  };

  ws.onclose = () => {
    console.log('WS disconnected');
    updateConnectionStatus(false);
    scheduleReconnect();
  };

  ws.onerror = (e) => {
    console.error('WS error:', e);
  };
}

function scheduleReconnect() {
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    connectWS();
  }, 2000);
}

function sendWS(msg) {
  const control = (state.terminalControl || {})[msg.key];
  if (['write-terminal', 'resize-terminal', 'close-terminal', 'terminal-control'].includes(msg.type) && control) {
    msg.controlGeneration = control.generation;
    if (msg.type !== 'terminal-control' && control.owner === 'remote') return;
  }
  if (msg.type === 'write-terminal' && String(msg.key || '').startsWith('relay:')) {
    const session = ((state.relay.status || {}).sessions || []).find(item => item.key === msg.key);
    if (session && session.owner === 'local') return;
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

// 仅用于本地排障：确认桌面窗口的路径按钮是否真正收到点击。
function reportUiAction(action, detail = '') {
  sendWS({ type: 'ui-debug', action, detail });
}

function callWS(type, payload = {}, timeoutMs = 15000) {
  return new Promise((resolve) => {
    const id = ++reqIdCounter;
    pendingReqs.set(id, resolve);
    sendWS({ type, id, ...payload });
    // timeout
    setTimeout(() => {
      if (pendingReqs.has(id)) {
        pendingReqs.delete(id);
        resolve({ success: false, output: 'timeout' });
      }
    }, timeoutMs);
  });
}

function handleWSMessage(msg) {
  if (String(msg.type || '').startsWith('desktop-terminal-')) { if (!window.MijiaIntegration?.accept(msg) && !window.SerialControlsWindow?.accept(msg)) window.TerminalWindows?.accept(msg); return; }
  if (msg.type === 'db-flash-progress') {
    window.DBFlashIntegration?.acceptProgress(msg);
    return;
  }
  if (msg.type === 'diagnostic-progress') {
    handleDiagnosticProgress(msg);
    return;
  }
  if (msg.type === 'diagnostic-cancelled') {
    handleDiagnosticCancelled(msg);
    return;
  }

  // Handle response to pending request
  if (msg.id !== undefined && pendingReqs.has(msg.id)) {
    const cb = pendingReqs.get(msg.id);
    pendingReqs.delete(msg.id);
    cb(msg);
    return;
  }

  switch (msg.type) {
    case 'agent-control':
      state.agentClaims = state.agentClaims || {};
      state.agentClaims[msg.key] = !!msg.active;
      syncTerminalControls();
      break;
    case 'devices-updated':
      state.devices = msg.devices || [];
      const rawKeys = msg.knownKeys || [];
      // 离线设备记录仍供管理列表展示，终端窗口立即回收；恢复在线后可自动重建。
      const offlineRemoteKeys = new Set(state.devices
        .filter(d => isRemoteDevice(d) && d.relay.online === false).map(_deviceKey));
      state.knownKeys = rawKeys.filter(k => !state.dismissedKeys[k] && !offlineRemoteKeys.has(k))
        .slice(0, state.maxDevices);
      if (!terminalPanelKeys().includes(state.activeDevice)) {
        state.activeDevice = state.knownKeys[0] || null;
      }
      updateDeviceTabs();
      updateTerminalPanels();
      updateLayoutStatus();
      updateStatusBar();
      updateSelectMenus();
      break;

    case 'terminal-control-status':
      state.terminalControl = state.terminalControl || {};
      state.terminalControl[msg.key] = msg;
      syncTerminalControls();
      break;

    case 'terminal-data': {
      const key = msg.key;
      const t = state.terminals[key];
      const data = base64ToUint8Array(msg.data);
      let decoder = terminalDecoders.get(key);
      if (!decoder) {
        decoder = new TextDecoder('utf-8', { fatal: false });
        terminalDecoders.set(key, decoder);
      }
      const text = decoder.decode(data, { stream: true });
      if (t && t.term) {
        try {
          t.term.write(text);
        } catch {}
      } else {
        if (!state.terminalBuffer[key]) state.terminalBuffer[key] = [];
        state.terminalBuffer[key].push(text);
        // Visible diagnostic for buffered data
        const statusEl = document.getElementById('status-devices');
        if (statusEl) statusEl.textContent = `Buffering terminal data for ${key}...`;
      }
      break;
    }

    case 'terminal-switch':
    case 'terminal-exit': {
      const key = msg.key;
      const decoder = terminalDecoders.get(key);
      if (decoder) {
        const tail = decoder.decode();
        const t = state.terminals[key];
        if (tail && t && t.term) {
          try { t.term.write(tail); } catch {}
        }
      }
      terminalDecoders.delete(key);
      const t = state.terminals[key];
      if (t && t.term) {
        const switching = msg.type === 'terminal-switch';
        const text = switching ? '正在切换控制权，重新接入终端' : (msg.reason || '设备终端已结束');
        try { t.term.write(`\r\n\x1b[${switching ? '36' : '31'}m[${text}]\x1b[0m\r\n`); } catch {}
      }
      break;
    }

    case 'all-devices':
      // Can be used for populating selects if needed
      break;

    // --- 跨机中转 ---
    case 'relay-status':
      state.relay.status = msg;
      updateRelayStatusUi();
      // 远端设备的在线状态会随心跳变化，标签的灰显与徽标要跟着更新。
      updateDeviceTabs();
      updateTerminalPanels();
      updateStatusBar();
      break;

    case 'relay-event': {
      if (!msg.text) break;
      const kind = msg.kind || 'info';
      if (kind === 'error') showToast('error', '跨机中转', msg.text,
        msg.text.startsWith('控制日志消费暂停：') ? 'relay-journal-consume' : null);
      else if (kind === 'reject') showToast('warning', '跨机中转', msg.text);
      break;
    }

    case 'relay-peer-request':
      // 发现只更新提示，由主控在连接列表选择后确认，不抢占当前操作。
      updateRelayStatusUi();
      break;
  }
}

// ============================================================
// 跨机中转（角色徽标、连接状态、首连确认、配置）
// ============================================================
function isRemoteDevice(device) {
  return !!(device && device.relay);
}

function isRemoteKey(key) {
  return typeof key === 'string' && key.startsWith('relay:');
}

/** 远端设备是否可用：对端离线时禁用操作（与本机设备离线时表现一致）。 */
function isDeviceUsable(device) {
  if (!device) return false;
  if (device.relay) return device.relay.online !== false;
  return device.state === 'device' || device.state === undefined;
}

function deviceLabel(device) {
  if (!device) return '(未知设备)';
  const tid = device.transportId ? ` (tid=${device.transportId})` : '';
  if (device.relay) {
    const online = device.relay.online === false ? ' · 离线' : '';
    return `${device.serial}${tid} ← ${relayPeerLabel(device.relay)}${online}`;
  }
  return `${device.serial}${tid}`;
}

/** 所有用户界面统一使用机器名 / 部署标识，peerId 只作为路由键。 */
function relayPeerLabel(peer) {
  return peer.displayLabel || `${peer.host || '未知机器'} / ${peer.deploymentId || '默认部署'}`;
}

function showRelayConfirmDialog(request) {
  if (state.relay.status && state.relay.status.role === 'slave') return;
  const overlay = document.getElementById('relay-confirm-overlay');
  if (!overlay || !request) return;
  state.relay.pendingPeer = request.peerId;
  const body = document.getElementById('relay-confirm-body');
  const detail = document.getElementById('relay-confirm-detail');
  if (body) {
    const rows = [
      ['对端部署', relayPeerLabel(request)],
      ['对端声明角色', request.roleLabel || '未知'],
      ['本机角色', state.relay.status ? (state.relay.status.roleLabel || '未知') : '未知'],
      ['主机名', request.host || '未知'],
      ['指纹', request.fingerprint || '未提供'],
      ['对外设备', `${request.deviceCount || 0} 台`],
      ['连接方式', request.carrier || '共享文件夹'],
      ['往返延迟', request.latencyMs !== undefined ? `${request.latencyMs} ms` : '未知'],
    ];
    body.innerHTML = rows
      .map(([k, v]) => `<div class="relay-confirm-row"><span>${k}</span><b>${v}</b></div>`)
      .join('');
  }
  if (detail) detail.textContent = request.detail || '';
  overlay.style.display = 'flex';
}

function closeRelayConfirmDialog() {
  const overlay = document.getElementById('relay-confirm-overlay');
  if (overlay) overlay.style.display = 'none';
}

async function answerRelayConfirm(accept) {
  const peerId = state.relay.pendingPeer;
  closeRelayConfirmDialog();
  state.relay.pendingPeer = null;
  if (!peerId) return;
  // 用户选择「暂不接受」后不再反复弹窗：安全边界在从机侧，本机只是知情。
  if (!accept) state.relay.ignoredPeers[peerId] = true;
  else delete state.relay.ignoredPeers[peerId];
  const result = await callWS('relay-confirm-peer', { peerId, accept }, 20000);
  showToast(
    result.success ? 'success' : 'error',
    accept ? '已确认对端' : '已忽略对端',
    result.output || ''
  );
  await refreshRelayStatus();
}

// 跨机时「离线」有两种完全不同的原因：对端被关掉了，还是它的中转节点卡住了。
// 状态里的 ageMs 现在是**真实静默时长**（以共享目录服务器时钟为基准，不再被两端
// 时钟差污染），所以可以放心显示给用户 —— 秒级说明对方刚停或网络断了，分钟级
// 说明对方确实关掉了。
function relayAgeText(ms) {
  const secs = Math.floor((Number(ms) || 0) / 1000);
  if (secs <= 0) return '';
  if (secs < 60) return `（最后心跳 ${secs} 秒前）`;
  if (secs < 3600) return `（最后心跳 ${Math.floor(secs / 60)} 分钟前）`;
  return `（最后心跳 ${(secs / 3600).toFixed(1)} 小时前）`;
}

function relayStateClass(status) {
  if (!status) return '';
  if (!status.enabled) return 'off';
  if (!status.running) return 'bad';
  const item = relaySelectedPeer(status);
  // 灯色只表示心跳与连接事实，验证失败但仍有心跳时保持黄色并展示原因。
  if (!item || !item.online) return 'bad';
  if (!status.conflict && item.connectionState === 'connected') return 'ok';
  return 'warn';
}

function relayLightText(status) {
  const labels = { off: '未启用', bad: '未检测到有效心跳',
    warn: '已检测到心跳，等待或正在建立连接', ok: '连接成功' };
  return labels[relayStateClass(status)] || '尚未读取状态';
}

function relaySelectedPeer(status) {
  const pairing = (status && status.pairing) || [];
  if (status && status.role === 'slave') {
    // 历史配对不是当前主控：仅从已发现的主控角色中选择。
    const masters = pairing.filter(item => item.role === 'master' || item.roleLabel === '主控端');
    return masters.find(item => item.paired && item.connectionState === 'connected')
      || masters.find(item => item.online && item.peerId === status.master)
      || masters.find(item => item.online && item.paired)
      || masters.find(item => item.online)
      || masters.find(item => item.paired) || null;
  }
  const selected = document.getElementById('relay-pairing');
  return pairing.find(item => selected && item.nodeId === selected.value) || pairing[0] || null;
}

function relayVisibleState(status) {
  if (!status.enabled) return '未启用';
  if (!status.running) return '启动失败';
  const item = relaySelectedPeer(status);
  if (!item) return status.conflict ? '角色冲突'
    : status.role === 'slave' ? '等待主控连接' : '等待发现从机';
  const labels = {
    'local-confirmation': '待本机确认',
    'waiting-peer-confirmation': '等待对端确认',
    checking: '正在验证',
    'check-failed': '验证失败',
    connected: '通讯已验证',
    offline: '对端离线',
    'role-mismatch': '角色不匹配',
    'waiting-master': '等待主控连接',
  };
  return labels[item.connectionState] || '等待连接验证';
}

/** 共享目录中的可选实例统一显示在配对列表，本机名称固定不随多开变化。 */
function renderRelayPool(status) {
  renderRelayRoleUi(status);
  const localIdEl = document.getElementById('relay-peer-id');
  if (localIdEl) {
    localIdEl.textContent = status.machineName || '计算机名称';
    localIdEl.title = '本机计算机名称，多开和切换部署时保持不变';
  }
  const nodeIdEl = document.getElementById('relay-node-id');
  if (nodeIdEl) nodeIdEl.textContent = status.nodeId || '未生成';
  const rootHint = document.getElementById('relay-pool-root');
  if (rootHint) {
    rootHint.textContent = status.effectiveRoot && status.effectiveRoot !== status.root
      ? `实际数据根 ${status.effectiveRoot}` : '';
    rootHint.title = rootHint.textContent;
  }
  const rootInput = document.getElementById('relay-root');
  if (rootInput && rootInput.value === status.root) {
    renderRelayDeployments(status.deployments || []);
  }
  const pairSelect = document.getElementById('relay-pairing');
  const pairing = status.pairing || [];
  if (pairSelect) {
    const previous = pairSelect.value;
    pairSelect.replaceChildren();
    for (const item of pairing) {
      const stateLabel = item.connectionState === 'connected' ? '已连接'
        : item.connectionState === 'waiting-peer-confirmation' ? '等待对端确认'
        : item.connectionState === 'check-failed' ? '验证失败'
        : item.connectionState === 'role-mismatch' ? '角色不匹配'
        : item.connectionState === 'checking' ? '主控已确认 · 验证中'
        : !item.online ? '离线'
        : item.paired ? '本机已确认'
        : item.state === 'revoked' ? '已解除' : '待本机确认';
      const roleText = item.roleLabel ? `${item.roleLabel} ` : '';
      const light = !item.online ? '🔴' : item.connectionState === 'connected' && !status.conflict ? '🟢' : '🟡';
      const label = `${light} ${item.host || item.peerId || item.nodeId} ｜ `
        + `${item.deploymentId || '默认部署'} ｜ ${roleText}${stateLabel}`;
      const option = new Option(label, item.nodeId);
      option.title = `实例 ${item.nodeId}`;
      pairSelect.add(option);
    }
    if (!pairing.length) pairSelect.add(new Option('(共享目录中暂无可配对实例)', ''));
    if (pairing.some(item => item.nodeId === previous)) pairSelect.value = previous;
    pairSelect.disabled = !pairing.length;
  }
  syncRelayPairButtons();
  const pairNote = document.getElementById('relay-pair-note');
  if (pairNote) {
    pairNote.textContent = '主控选择从机并确认后自动验证连接，从机无需操作。只有验证成功后才能管理远端设备。';
    pairNote.title = pairNote.textContent;
  }
}

/** 从机只配置并等待；当前主控和连接事实由后端提供，不使用隐藏下拉框选路。 */
function renderRelayRoleUi(status) {
  const role = document.getElementById('relay-role').value || (status && status.role);
  const slave = role === 'slave';
  document.getElementById('relay-master-connection').hidden = slave;
  document.getElementById('relay-slave-connection').hidden = !slave;
  document.getElementById('relay-step-confirm').textContent = slave ? '2. 等待主控连接' : '2. 主控确认连接';
  document.getElementById('relay-step-use').textContent = slave ? '3. 提供设备' : '3. 管理设备';
  document.getElementById('relay-use-title').textContent = slave
    ? '3. 提供设备　连接后主控即可管理本机共享的设备，本机保持运行即可。'
    : '3. 管理设备　连接验证成功后，设备和终端自动出现在主控窗口。';
  const item = relaySelectedPeer(status);
  document.getElementById('relay-slave-status').textContent = item
    ? `当前主控：${relayPeerLabel(item)}。${item.connectionReason || '等待主控自动验证连接'}。`
    : '本机已就绪后，等待主控在其窗口选择本机并确认连接。';
  if (slave) {
    closeRelayConfirmDialog();
    state.relay.pendingPeer = null;
  }
}

/** 部署选项来自当前共享目录；保留正在编辑但尚未保存的新增项。 */
function renderRelayDeployments(deployments, selected) {
  const select = document.getElementById('relay-pool-id');
  if (!select) return;
  const value = selected === undefined ? select.value : selected;
  const names = new Set(deployments.filter(Boolean));
  if (value) names.add(value);
  select.replaceChildren(new Option('默认部署（原目录）', ''));
  for (const name of [...names].sort()) select.add(new Option(name, name));
  select.value = value || '';
}

let relayDeploymentRequest = 0;
async function refreshRelayDeployments() {
  const root = document.getElementById('relay-root');
  if (!root) return;
  const path = root.value.trim();
  const request = ++relayDeploymentRequest;
  if (!path) { renderRelayDeployments([]); return; }
  const result = await callWS('relay-discover-deployments', { root: path }, 15000);
  if (request !== relayDeploymentRequest || root.value.trim() !== path) return;
  renderRelayDeployments(result.deployments || []);
}

function openRelayDeploymentDialog() {
  const overlay = document.getElementById('relay-deployment-overlay');
  const input = document.getElementById('relay-deployment-name');
  document.getElementById('relay-deployment-error').hidden = true;
  input.value = '';
  overlay.style.display = 'flex';
  input.focus();
}

function closeRelayDeploymentDialog() {
  document.getElementById('relay-deployment-overlay').style.display = 'none';
  document.getElementById('relay-add-deployment').focus();
}

function addRelayDeployment() {
  const name = document.getElementById('relay-deployment-name').value.trim();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,40}$/.test(name)) {
    const error = document.getElementById('relay-deployment-error');
    error.textContent = '请输入 1–41 位部署标识，以字母或数字开头，只含字母、数字、点、下划线和短横线。';
    error.hidden = false;
    return;
  }
  const select = document.getElementById('relay-pool-id');
  renderRelayDeployments([...select.options].map(option => option.value), name);
  closeRelayDeploymentDialog();
  renderRelayEffective();
}

/** 配对/解除按钮的可用性跟着当前选中的那一条走，避免点了没反应。 */
function syncRelayPairButtons() {
  const pairSelect = document.getElementById('relay-pairing');
  const pairBtn = document.getElementById('relay-pair');
  const unpairBtn = document.getElementById('relay-unpair');
  if (!pairSelect || !pairBtn || !unpairBtn) return;
  const status = state.relay.status || {};
  const item = status.role === 'slave' ? relaySelectedPeer(status)
    : (status.pairing || []).find(entry => entry.nodeId === pairSelect.value);
  const peer = (status.peers || []).find(entry => item && entry.peerId === item.peerId);
  const hasTarget = !!pairSelect.value;
  const sameRole = !!(item && item.roleLabel && item.roleLabel === status.roleLabel);
  const needsIdentity = !!(peer && peer.needsConfirmation);
  const needsReconnect = !!(item && item.connectionState === 'waiting-peer-confirmation');
  const master = status.role === 'master';
  pairBtn.disabled = !master || !hasTarget || !status.running || relayConfigDirty() || sameRole
    || !item || !item.online || (!!item.paired && !needsIdentity && !needsReconnect);
  pairBtn.textContent = needsIdentity ? '重新确认身份' : needsReconnect ? '重新确认连接'
    : (item && item.paired ? '主控已确认' : '确认连接');
  unpairBtn.disabled = !master || !hasTarget || !(item && item.paired);
  renderRelayNextAction(status, item, peer);
}

/** 只按后端逐对端验证结论推进步骤，不把本机确认或旧终端快照当作接通。 */
function renderRelayNextAction(status, item, peer) {
  const el = document.getElementById('relay-next-action');
  if (!el) return;
  let step = 1;
  let text = '下一步：两端启用中转，选择同一个共享目录，分别保存主控端和从机配置。';
  if (relayConfigDirty()) {
    text = '下一步：先点击「保存并生效」。连接操作使用当前生效配置，不能使用尚未保存的目录或角色。';
  } else if (status.enabled && !status.running) {
    text = `配置尚未运行：${status.startError || status.hint || '请检查共享目录后保存。'}`;
  } else if (status.running) {
    step = 2;
    if (status.conflict) {
      text = '下一步：处理角色冲突。检查已确认的在线实例，让该连接的一端为主控、另一端为从机。';
    } else if (status.role === 'slave' && !item) {
      text = '本机已就绪，等待主控选择并确认连接；从机无需选择对端。可在高级设置调整共享设备和命令权限。';
    } else if (!item) {
      text = '下一步：在另一端启用中转并保存相同共享目录。发现后会出现在「连接对端」下拉列表，部署可以不同。';
    } else if (item.connectionState === 'connected') {
      step = 3;
      text = status.role === 'master'
        ? `通讯已验证：已与 ${relayPeerLabel(item)} 建立连接，可在主窗口管理设备。没有共享设备时，请在从机接入设备并检查共享范围。`
        : `通讯已验证：已与 ${relayPeerLabel(item)} 建立连接。设备操作在主控窗口完成，本机保持运行即可。`;
    } else {
      text = `${item.connectionReason || '尚未验证连接'}。${item.nextAction || '请检查对端状态。'}`;
    }
  }
  el.textContent = text;
  for (const [index, id] of ['relay-step-config', 'relay-step-confirm', 'relay-step-use'].entries()) {
    const node = document.getElementById(id);
    if (!node) continue;
    node.classList.toggle('active', index + 1 === step);
    node.classList.toggle('complete', index + 1 < step);
    if (index + 1 === step) node.setAttribute('aria-current', 'step');
    else node.removeAttribute('aria-current');
  }
}

async function pairSelectedPeer() {
  if ((state.relay.status || {}).role !== 'master') return;
  const pairSelect = document.getElementById('relay-pairing');
  const nodeId = pairSelect ? pairSelect.value : '';
  if (!nodeId) return;
  const status = state.relay.status || {};
  const item = (status.pairing || []).find(entry => entry.nodeId === nodeId) || {};
  const result = await callWS('relay-pair-peer', { nodeId, peerId: item.peerId || '' }, 20000);
  showToast(result.success ? 'success' : 'error', '确认连接',
    result.success ? '主控已确认，正在自动验证连接；从机无需操作。' : result.output || '');
  await refreshRelayStatus();
}

async function unpairSelectedPeer() {
  const pairSelect = document.getElementById('relay-pairing');
  const nodeId = pairSelect ? pairSelect.value : '';
  if (!nodeId) return;
  const result = await callWS('relay-unpair-peer', { nodeId }, 20000);
  showToast(result.success ? 'success' : 'error', '跨机中转配对', result.output || '');
  await refreshRelayStatus();
}

// --- 配置弹窗（入口：菜单栏 → 工具 → 跨机中转配置…）---
function relayConfigDialogOpen() {
  const overlay = document.getElementById('relay-config-overlay');
  return !!overlay && overlay.style.display === 'flex';
}

async function openRelayConfigDialog() {
  const overlay = document.getElementById('relay-config-overlay');
  if (!overlay) return;
  // 每次打开都从磁盘重读配置：relay.json 就在 exe 同目录，**另一个实例**或手工
  // 编辑都可能改过它。只在首连时读一次的话，这个窗口会一直拿着过期表单，一保存
  // 就把别人的改动覆盖回去 —— 表现出来就是「明明设了主控，却还是从机」。
  await loadRelayConfig();
  await refreshRelayStatus();
  window.ToolPanels.relayConfig.show();
  overlay.style.display = 'flex';
}

function closeRelayConfigDialog() {
  const overlay = document.getElementById('relay-config-overlay');
  if (overlay) window.ToolPanels.relayConfig.hide();
}

/** 表单里的配置（与提交给后端的载荷同一套取值规则）。 */
function relayFormValues() {
  return collectRelayConfigForm();
}

/** 表单里是否有还没保存的改动 —— 有就说明眼前这套界面不是「现在生效的」。 */
function relayConfigDirty() {
  const saved = state.relay.config;
  if (!saved) return false;
  const savedView = {
    enabled: !!saved.enabled,
    role: saved.role || 'slave',
    // 没有 peerId：本机标识不再是表单项（自动生成），留在这里会让「表单 vs 已保存」
    // 的对比永远不相等 —— 界面会一直显示「有未保存的改动」。
    carrierKind: saved.carrierKind || 'shared-folder',
    root: String(saved.root || ''),
    poolId: String(saved.poolId || ''),
    pairingRequired: true,
    allowedSubcommands: saved.allowedSubcommands || [],
    allowShell: !!saved.allowShell,
    managedSerials: saved.managedSerials || [],
  };
  return JSON.stringify(relayFormValues()) !== JSON.stringify(savedView);
}

/**
 * 渲染「当前生效」那一行：角色与标识一律取自**运行中的节点**上报的状态，
 * 不取自表单。表单是「你想改成什么」，这行是「现在真正在跑什么」——分开显示，
 * 才能一眼看出「改了但没生效」。
 */
function relayRuntimeText(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  return `${hours}小时 ${minutes}分 ${total % 60}秒`;
}

function renderRelayEffective() {
  const el = document.getElementById('relay-effective');
  if (!el) return;
  const status = state.relay.status;
  renderRelayRoleUi(status);
  const dirty = relayConfigDirty();
  if (!status) {
    el.className = 'relay-effective off';
    el.textContent = '尚未读取中转状态。';
    return;
  }
  // 多实例隔离说明：本窗口因为默认配置被另一个实例占用而改用独立临时配置时，
  // 必须让用户知道（否则「为什么设置不保留」会变成灵异事件）。
  const noticeEl = document.getElementById('relay-config-notice');
  if (noticeEl) {
    noticeEl.textContent = status.configNotice || '';
    noticeEl.hidden = !status.configNotice;
  }
  const lines = [];
  if (!status.enabled) {
    lines.push('<span class="relay-effective-dim">当前未启用跨机中转。</span>');
  } else {
    lines.push(
      `当前生效：<span class="relay-effective-role">${status.roleLabel || '未知角色'}</span>`
      + `　本机标识 <b>${status.machineName || '未设置'}</b>`
      + `　运行状态 <span class="relay-effective-dim">${relayVisibleState(status)}</span>`
    );
    lines.push(`<span class="relay-effective-dim">本机运行 ${relayRuntimeText(status.uptimeSeconds)} · 心跳 ${Number(status.heartbeatSeq) || 0}</span>`);
    const selectedPeer = relaySelectedPeer(status);
    if (selectedPeer && selectedPeer.heartbeatSeq) {
      lines.push(`<span class="relay-effective-dim">对端运行 ${relayRuntimeText(selectedPeer.uptimeSeconds)} · 心跳 ${Number(selectedPeer.heartbeatSeq) || 0}</span>`);
    }
    const detail = selectedPeer ? selectedPeer.connectionReason : status.hint;
    if (detail) {
      lines.push(`<span class="relay-effective-dim">${detail}</span>`);
    }
  }
  if (dirty) {
    lines.push('<span class="relay-effective-pending">⚠ 表单里有未生效的改动，点「保存并生效」后才会起作用</span>');
  }
  el.className = 'relay-effective'
    + (dirty ? ' pending' : '')
    + (status.enabled ? '' : ' off');
  el.innerHTML = lines.join('<br>');
  syncRelayPairButtons();
}

function updateRelayStatusUi() {
  const status = state.relay.status;
  // 本机作为从机正在被使用的会话数：坐在被控机器前的人必须看得见有人在操作它。
  const served = (status && status.servedSessions) || [];
  const selectedPeer = relaySelectedPeer(status);
  const paired = ((status && status.pairing) || []).filter(item => status.role === 'slave'
    ? item.paired && (item.role === 'master' || item.roleLabel === '主控端')
    : item.role === 'slave' || item.roleLabel === '从机');
  const connectedCount = paired.filter(item => item.connectionState === 'connected').length;
  const waitingCount = paired.filter(item => item.online && item.connectionState !== 'connected').length;
  const offlineCount = paired.filter(item => !item.online).length;
  const badge = document.getElementById('status-relay');
  if (badge) {
    if (!status || !status.enabled) {
      badge.hidden = true;
      badge.textContent = '';
    } else {
      badge.hidden = false;
      const classes = ['status-relay', relayStateClass(status)];
      badge.className = classes.filter(Boolean).join(' ');
      badge.textContent = `${status.roleLabel || ''} ${status.machineName || ''}｜${relayVisibleState(status)}`
        + (status.role === 'master' && paired.length > 1 ? `｜已连接 ${connectedCount} / 待处理 ${waitingCount} / 离线 ${offlineCount}` : '')
        + (served.length ? `｜被控 ${served.length}` : '');
      badge.title = `${relayLightText(status)}。点击查看详细状态`;
      badge.onclick = openRelayConfigDialog;
      badge.setAttribute('role', 'button');
      badge.tabIndex = 0;
      badge.onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openRelayConfigDialog();
        }
      };
    }
  }
  const stateLine = document.getElementById('relay-state');
  if (stateLine && status) {
    const servedText = served.length
      ? `（正在为对端提供 ${served.length} 个终端会话：`
        + served.map(s => s.serial || s.deviceKey || s.streamId).join('、') + '）'
      : '';
    const stateText = status.enabled
      ? `${relayVisibleState(status)}：${selectedPeer ? selectedPeer.connectionReason || '' : status.hint || ''}${servedText}`
      : '未启用';
    stateLine.textContent = stateText;
    stateLine.className = `tool-label relay-link-light ${relayStateClass(status)}`;
    // 这行在窄处会被省略号截断（见 style.css），完整内容挂 title 里仍可看。
    // 完整的运行状态在弹窗上方的「当前生效」框里也有一份。
    stateLine.title = `${relayLightText(status)}。${stateText}`;
  }
  const events = document.getElementById('relay-events');
  if (events && status) {
    const lines = (status.events || []).map(e => `[${e.at ? e.at.slice(11, 19) : ''}] ${e.text}`);
    events.textContent = lines.join('\n') || '(暂无事件)';
    events.scrollTop = events.scrollHeight;
  }
  // 冲突/待确认要显眼：这属于需要人做决定的状态，不能只在状态栏里灰着。
  const warning = document.getElementById('relay-warning');
  if (warning) {
    const pending = (status && status.pendingConfirmation) || [];
    if (status && status.conflict) {
      warning.hidden = false;
      warning.textContent = `⚠ 角色冲突：${status.hint || ''}`;
    } else if (status && status.role === 'slave') {
      warning.hidden = true;
      warning.textContent = '';
    } else if (pending.length) {
      warning.hidden = false;
      warning.textContent = `⚠ 有对端待本机确认（${pending.map(id => {
        const peer = (status.peers || []).find(item => item.peerId === id);
        return peer ? relayPeerLabel(peer) : '未知对端';
      }).join('、')}）：核对身份后确认连接`;
    } else {
      // 未配对实例是「门槛生效」的证据，不是故障；但它正是「对端在跑却连不上」
      // 的原因，所以必须在这里说一句，否则用户会去查网络。
      const unpaired = (status && status.unpairedPeers) || [];
      const online = unpaired.filter(item => item.online);
      if (status && status.pairingRequired && online.length) {
        warning.hidden = false;
        warning.textContent = `⚠ 有 ${online.length} 个实例未配对（`
          + online.map(item => relayPeerLabel(item)).join('、')
          + '）：请在「连接对端」中选择并确认连接';
      } else {
        warning.hidden = true;
        warning.textContent = '';
      }
    }
  }
  // 共享目录中的实例统一在配对列表显示。
  renderRelayPool(status);
  // 「当前生效」那一行要跟着状态走（它是从运行中的节点上报的，不是表单值）。
  renderRelayEffective();
}

async function refreshRelayStatus() {
  const status = await callWS('relay-status', {}, 10000);
  if (status && status.state !== undefined) {
    state.relay.status = status;
    updateRelayStatusUi();
  }
}

async function loadRelayConfig() {
  const result = await callWS('relay-config', {}, 10000);
  if (!result || !result.config) return;
  state.relay.config = result.config;
  state.relay.carriers = result.carriers || [];
  state.relay.configPath = result.configPath || '';
  state.relay.loaded = true;

  const set = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = !!value;
    else el.value = value === undefined || value === null ? '' : value;
  };
  set('relay-enabled', result.config.enabled);
  set('relay-role', result.config.role || 'master');
  set('relay-root', result.config.root || '');
  renderRelayDeployments([], result.config.poolId || '');
  await refreshRelayDeployments();
  // 三态：'' = 跟随池，'1' = 强制开启，'0' = 强制关闭。用字符串是因为 select 的
  // value 只能是字符串，而这里必须能表达「未表态」这第三种意思。
  set('relay-pairing-required', '1');
  set('relay-subcommands', (result.config.allowedSubcommands || []).join(', '));
  set('relay-allow-shell', result.config.allowShell);
  set('relay-managed-serials', (result.config.managedSerials || []).join(', '));

  const carrierSelect = document.getElementById('relay-carrier');
  if (carrierSelect) {
    carrierSelect.innerHTML = (result.carriers || []).map(c => (
      c.implemented
        ? `<option value="${c.kind}">${c.label}</option>`
        : `<option value="${c.kind}" disabled>${c.label}（未实现）</option>`
    )).join('');
    carrierSelect.value = result.config.carrierKind || 'shared-folder';
  }
  updateRelayCarrierNote();
  const pathHint = document.getElementById('relay-config-path');
  if (pathHint) {
    const text = result.configPath ? `配置：${result.configPath}` : '';
    pathHint.textContent = text;
    pathHint.title = text;   // 长路径会被省略号截断，完整路径挂 title
  }
  // 本机标识固定显示 status.machineName；内部路由键不作为用户的本机名称。
}

function updateRelayCarrierNote() {
  const select = document.getElementById('relay-carrier');
  const note = document.getElementById('relay-carrier-note');
  if (!select || !note) return;
  const spec = (state.relay.carriers || []).find(c => c.kind === select.value);
  note.textContent = spec ? (spec.implemented ? '可用' : `未实现：${spec.note || ''}`) : '';
}

function collectRelayConfigForm() {
  const value = (id) => {
    const el = document.getElementById(id);
    if (!el) return undefined;
    return el.type === 'checkbox' ? el.checked : el.value;
  };
  const splitList = (text) => String(text || '')
    .split(/[,，\s]+/)
    .map(s => s.trim())
    .filter(Boolean);
  return {
    enabled: value('relay-enabled'),
    role: value('relay-role'),
    carrierKind: value('relay-carrier'),
    root: String(value('relay-root') || '').trim(),
    poolId: String(value('relay-pool-id') || '').trim(),
    pairingRequired: true,
    allowedSubcommands: splitList(value('relay-subcommands')),
    allowShell: value('relay-allow-shell'),
    managedSerials: splitList(value('relay-managed-serials')),
  };
}

/**
 * 保存并生效。换到一个「已经有中转数据」的共享目录时，先问用户要沿用还是
 * 清空重建 —— 两轮数据（旧心跳、旧消息队列、别的机器的分区）默默混在一个
 * 目录里，排障时是灾难。返回是否真的保存了。
 */
async function saveRelayConfig() {
  const form = collectRelayConfigForm();
  if (!await checkRelayDuplicate()) return false;
  const currentRoot = (state.relay.config && state.relay.config.root) || '';
  if (form.enabled && form.carrierKind === 'shared-folder' && form.root && form.root !== currentRoot) {
    const info = await callWS('relay-inspect-root', { root: form.root }, 15000);
    if (info && info.hasData) {
      const choice = await askRelayRootChoice(info);
      if (choice === 'cancel') return false;
      if (choice === 'clean') {
        const cleaned = await callWS('relay-clean-root', { root: form.root }, 60000);
        showToast(cleaned.success ? 'success' : 'error', '跨机中转', cleaned.output || '');
        if (!cleaned.success) return false;
      }
    }
  }
  const result = await callWS('relay-save-config', { config: form }, 30000);
  showToast(result.success ? 'success' : 'error', '跨机中转配置', result.output || '');
  if (!result.success) return false;
  // 自动刷新：配置刚生效，表单、状态徽标、「当前生效」那一行都要立刻跟上。
  await loadRelayConfig();
  await refreshRelayStatus();
  return true;
}

async function checkRelayDuplicate(preview = false) {
  const form = collectRelayConfigForm();
  if (form.role !== 'slave' || !form.root || (!preview && !form.enabled)) return true;
  const result = await callWS('relay-check-config', { config: { ...form, enabled: true } }, 15000);
  if (!result || !result.success) {
    showToast('error', '跨机中转配置', result && result.output || '配置预检失败，请重试');
    return false;
  }
  return true;
}

/**
 * 「目录里已有中转数据」的选择框：沿用 / 清空后重建 / 取消。
 * 把盘点到的文件一样样列出来，用户才知道「清空」到底会删什么。
 */
function askRelayRootChoice(info) {
  return new Promise((resolve) => {
    const overlay = document.getElementById('relay-root-choice-overlay');
    if (!overlay) { resolve('cancel'); return; }
    const desc = document.getElementById('relay-root-choice-desc');
    const files = document.getElementById('relay-root-choice-files');
    if (desc) desc.textContent = `目录 ${info.root || ''} 里已经有中转数据：`;
    if (files) {
      const rows = [];
      if (info.manifest) rows.push('<li><code>relay.json</code> —— 中转布局清单</li>');
      const slotLabels = {
        peers: '对端心跳', requests: '命令请求', results: '命令结果',
        streams: '终端流', archive: '归档',
      };
      for (const [slot, label] of Object.entries(slotLabels)) {
        const count = Number(info[slot] || 0);
        if (count > 0) rows.push(`<li><code>${slot}/</code> —— ${label}，${count} 项</li>`);
      }
      files.innerHTML = rows.join('');
      files.hidden = rows.length === 0;
    }
    let settled = false;
    const buttons = [
      ['relay-root-keep', 'keep'],
      ['relay-root-clean', 'clean'],
      ['relay-root-cancel', 'cancel'],
      ['relay-root-choice-close', 'cancel'],
    ];
    const onKey = (e) => {
      if (e.key === 'Escape') finish('cancel');
    };
    const onOverlayClick = (e) => {
      if (e.target === overlay) finish('cancel');
    };
    const cleanup = () => {
      for (const [id] of buttons) {
        const el = document.getElementById(id);
        if (el && el._relayChoiceHandler) {
          el.removeEventListener('click', el._relayChoiceHandler);
          delete el._relayChoiceHandler;
        }
      }
      document.removeEventListener('keydown', onKey);
      overlay.removeEventListener('click', onOverlayClick);
    };
    const finish = (value) => {
      if (settled) return;
      settled = true;
      overlay.style.display = 'none';
      cleanup();
      resolve(value);
    };
    for (const [id, value] of buttons) {
      const el = document.getElementById(id);
      if (!el) continue;
      el._relayChoiceHandler = () => finish(value);
      el.addEventListener('click', el._relayChoiceHandler);
    }
    overlay.addEventListener('click', onOverlayClick);
    document.addEventListener('keydown', onKey);
    overlay.style.display = 'flex';
  });
}

let relayCacheBusy = false;

function askRelayCacheClear(root) {
  return new Promise(resolve => {
    const overlay = document.getElementById('relay-cache-confirm-overlay');
    if (!overlay) { resolve(false); return; }
    document.getElementById('relay-cache-confirm-path').textContent = `已保存共享目录：${root}`;
    const bindings = [
      ['relay-cache-confirm-delete', true], ['relay-cache-confirm-cancel', false],
      ['relay-cache-confirm-close', false],
    ].map(([id, value]) => [document.getElementById(id), () => finish(value)]);
    const onKey = event => {
      if (event.key === 'Escape') { event.preventDefault(); finish(false); }
    };
    const onBackdrop = event => { if (event.target === overlay) finish(false); };
    let settled = false;
    const previousFocus = document.activeElement;
    const finish = value => {
      if (settled) return;
      settled = true;
      overlay.style.display = 'none';
      bindings.forEach(([el, handler]) => el.removeEventListener('click', handler));
      document.removeEventListener('keydown', onKey);
      overlay.removeEventListener('click', onBackdrop);
      if (previousFocus) previousFocus.focus();
      resolve(value);
    };
    bindings.forEach(([el, handler]) => el.addEventListener('click', handler));
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', onBackdrop);
    overlay.style.display = 'flex';
    document.getElementById('relay-cache-confirm-cancel').focus();
  });
}

async function clearRelayCache() {
  if (relayCacheBusy) return;
  const saved = state.relay.config;
  const resultEl = document.getElementById('relay-clear-cache-result');
  const report = (text, error = false, partial = false) => {
    resultEl.textContent = text;
    resultEl.hidden = false;
    showToast(error ? 'error' : partial ? 'info' : 'success', partial ? '缓存清理部分完成' : '清除缓存', text);
  };
  if (!saved || !saved.root || saved.carrierKind !== 'shared-folder') {
    report('请先保存有效的共享目录配置。', true);
    return;
  }
  if (relayConfigDirty()) {
    report('配置有未保存的改动，请先保存或刷新配置，再清除缓存。', true);
    return;
  }
  relayCacheBusy = true;
  const controls = ['relay-clear-cache', 'relay-save', 'relay-refresh'].map(id => document.getElementById(id));
  const disabled = controls.map(el => el.disabled);
  controls.forEach(el => { el.disabled = true; });
  try {
    if (!await askRelayCacheClear(saved.root)) return;
    resultEl.hidden = false;
    resultEl.textContent = '正在扫描各部署闲置缓存，保留活动文件并统计减少空间；中转和终端保持运行…';
    const result = await callWS('relay-clear-cache', { root: saved.root, confirmed: true }, 120000);
    report(result.output && result.output !== 'timeout' ? result.output
      : '清除结果未确认，请检查目录和状态；不会自动重试。', !result.success, result.completed === false || result.limited === true);
    await loadRelayConfig();
    await refreshRelayStatus();
  } catch (error) {
    report('清除结果未确认，请检查目录和状态；不会自动重试。' + String(error.message || error), true);
  } finally {
    controls.forEach((el, index) => { el.disabled = disabled[index]; });
    relayCacheBusy = false;
  }
}

function initRelay() {
  const clearCache = document.getElementById('relay-clear-cache');
  if (clearCache) clearCache.addEventListener('click', clearRelayCache);
  const save = document.getElementById('relay-save');
  if (save) {
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        await saveRelayConfig();
      } finally {
        save.disabled = false;
      }
    });
  }
  const refresh = document.getElementById('relay-refresh');
  if (refresh) {
    refresh.addEventListener('click', async () => {
      refresh.disabled = true;
      // 「刷新状态」不只是拉一次状态，而是**按磁盘上的配置重新生效**：配置可能被
      // 另一个实例或手工编辑改过，那种情况下光拉状态永远看不出角色已经变了。
      const result = await callWS('relay-reload-config', {}, 30000);
      refresh.disabled = false;
      showToast(
        result.success ? 'success' : 'error',
        '跨机中转',
        result.output || (result.success ? '状态已刷新' : '刷新失败'),
      );
      await loadRelayConfig();
      await refreshRelayStatus();
    });
  }
  const browser = document.getElementById('relay-browse');
  if (browser) {
    browser.addEventListener('click', async () => {
      const picked = await callWS('pick-directory', { title: '选择跨机中转的共享目录' });
      if (picked && picked.success && picked.path) {
        const root = document.getElementById('relay-root');
        if (root) root.value = picked.path;
        renderRelayEffective();
        await refreshRelayDeployments();
        await checkRelayDuplicate(true);
      }
    });
  }
  const carrier = document.getElementById('relay-carrier');
  if (carrier) carrier.addEventListener('change', updateRelayCarrierNote);
  const pairSelect = document.getElementById('relay-pairing');
  if (pairSelect) pairSelect.addEventListener('change', updateRelayStatusUi);
  document.getElementById('relay-add-deployment').addEventListener('click', openRelayDeploymentDialog);
  document.getElementById('relay-deployment-add').addEventListener('click', addRelayDeployment);
  for (const id of ['relay-deployment-close', 'relay-deployment-cancel']) {
    document.getElementById(id).addEventListener('click', closeRelayDeploymentDialog);
  }
  document.getElementById('relay-deployment-overlay').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) closeRelayDeploymentDialog();
  });
  document.getElementById('relay-deployment-name').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') addRelayDeployment();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.getElementById('relay-deployment-overlay').style.display === 'flex') {
      event.stopImmediatePropagation();
      closeRelayDeploymentDialog();
    }
  });
  document.getElementById('relay-root').addEventListener('change', async () => {
    await refreshRelayDeployments();
    await checkRelayDuplicate(true);
  });
  document.getElementById('relay-pool-id').addEventListener('change', () => checkRelayDuplicate(true));
  document.getElementById('relay-pool-id').addEventListener('focus', refreshRelayDeployments);
  const pairBtn = document.getElementById('relay-pair');
  if (pairBtn) pairBtn.addEventListener('click', () => { pairSelectedPeer(); });
  const unpairBtn = document.getElementById('relay-unpair');
  if (unpairBtn) unpairBtn.addEventListener('click', () => { unpairSelectedPeer(); });
  const confirmAccept = document.getElementById('relay-confirm-accept');
  if (confirmAccept) confirmAccept.addEventListener('click', () => answerRelayConfirm(true));
  const confirmIgnore = document.getElementById('relay-confirm-ignore');
  if (confirmIgnore) confirmIgnore.addEventListener('click', () => answerRelayConfirm(false));
  const confirmClose = document.getElementById('relay-confirm-close');
  if (confirmClose) confirmClose.addEventListener('click', () => answerRelayConfirm(false));

  // 配置弹窗：关闭方式与诊断弹窗一致（✕ / 点遮罩 / Esc）。
  const configClose = document.getElementById('relay-config-close');
  if (configClose) configClose.addEventListener('click', closeRelayConfigDialog);
  const configOverlay = document.getElementById('relay-config-overlay');
  configOverlay?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && relayConfigDialogOpen()) closeRelayConfigDialog();
  });
  // 表单一动就重算「未生效的改动」，用户能马上看出自己还没保存。
  // 本机标识不在这里：它已经不是输入项（自动生成，见 renderRelayPool）。
  const watched = [
    'relay-enabled', 'relay-role', 'relay-carrier', 'relay-root',
    'relay-pool-id', 'relay-pairing-required',
    'relay-subcommands', 'relay-allow-shell', 'relay-managed-serials',
  ];
  for (const id of watched) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.addEventListener('input', renderRelayEffective);
    el.addEventListener('change', renderRelayEffective);
  }
}

function base64ToUint8Array(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function updateConnectionStatus(connected) {
  window.dispatchEvent(new CustomEvent('manager-connection-status', {detail: {connected}}));
  const el = document.getElementById('status-devices');
  if (!el) return;
  if (!connected) {
    el.textContent = 'WebSocket 断开 - 重连中...';
    el.style.color = 'var(--error)';
  } else {
    el.style.color = '';
  }
}

// ============================================================
// Top tabs
// ============================================================
// 菜单栏里带下拉的标签：点它只开合下拉，不切内容面板（所以没有 panel-<tab>）。
const DROPDOWN_TABS = {
  terminal: 'window-menu',
  file: 'file-menu',
  tools: 'tools-menu',
  help: 'help-menu',
};

function initTopTabs() {
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
function showAboutDialog() {
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
const TERMINAL_FIND_WORD_CHAR = /[\p{L}\p{N}_]/u;

function initTerminalFind() {
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

function openTerminalFind() {
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

function closeTerminalFind() {
  const dialog = document.getElementById('terminal-find-dialog');
  if (dialog.style.display !== 'block') return;
  clearTerminalFindSelection();
  dialog.style.display = 'none';
  state.find.targetKey = null;
  state.find.matches = [];
  state.find.currentIndex = -1;
}

function setTerminalFindStatus(text) {
  document.getElementById('terminal-find-status').textContent = text;
}

function clearTerminalFindSelection() {
  const target = state.terminals[state.find.targetKey];
  if (!target) return;
  target.suppressSelectionCopy = true;
  try { target.term.clearSelection(); } catch {}
  setTimeout(() => { target.suppressSelectionCopy = false; }, 60);
}

// BufferLine 的字符索引和终端列数并不总是一致（中文占两列、Emoji 可占多个 UTF-16 字符）。
// 因此同时建立“字符偏移 → xterm 列”的映射，供选区高亮精确定位。
function getTerminalFindLineInfo(line, row, cols) {
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

function getTerminalFindLogicalLines(term) {
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

function isTerminalFindWholeWord(text, start, end) {
  const before = start > 0 ? Array.from(text.slice(0, start)).pop() : '';
  const after = end < text.length ? Array.from(text.slice(end))[0] : '';
  return !(before && TERMINAL_FIND_WORD_CHAR.test(before))
    && !(after && TERMINAL_FIND_WORD_CHAR.test(after));
}

function getTerminalFindPosition(logicalLine, offset, isEnd) {
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

function collectTerminalFindMatches(target, query, caseSensitive, wholeWord) {
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

function refreshTerminalFindMatches() {
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

function revealTerminalFindMatch(target, match) {
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

function findTerminalText(direction) {
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
function updateDeviceTabs() {
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

function syncDeviceTab(tab, key) {
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
function selectActiveDevice(key) {
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
function _deviceKey(d) {
  // 服务端会给每台设备补上 key（远端设备带 relay:<peerId>: 前缀），优先用它，
  // 避免前端按 serial+tid 重新拼 key 时把远端设备拼成本机 key。
  if (d && d.key) return d.key;
  return d.transportId ? `${d.serial}:tid=${d.transportId}` : d.serial;
}

// 远程上传下载复用本机文件对话框，由后端经共享目录中转。
const REMOTE_INLINE_EDIT_HINT =
  '跨机中转暂不支持直接编辑设备文件（写回需要 stdin 通道）';

function syncTerminalControls() {
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

function remoteTerminalState(key, device) {
  if (device.relay && device.relay.online === false) return '离线';
  const sessions = (state.relay.status && state.relay.status.sessions) || [];
  const session = sessions.find(item => item.key === key);
  if (session && session.switchingControl) return '控制权切换中';
  if (session && session.opened) return '终端已就绪';
  return session && session.failures ? '终端重试中' : '终端准备中';
}

function applyRemoteDeviceGuards(cell, device) {
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

function createTerminalPanel(key, device) {
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
function _buildPanelCell(key, device) {
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
function _initTerminal(key, device, xtermDiv) {
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
function copyTextToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => legacyCopy(text));
  } else {
    legacyCopy(text);
  }
}

function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
}

function readClipboardLegacy() {
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
function _wireFileOps(key, cell) {
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

function decodeUtf8Base64(value) {
  const binary = atob(value || '');
  const bytes = Uint8Array.from(binary, ch => ch.charCodeAt(0));
  return new TextDecoder('utf-8', {fatal: true}).decode(bytes);
}

function encodeUtf8Base64(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

/** Wire the independent device-file edit toolbar. */
function _wireDeviceFileEdit(key, cell) {
  const t = state.terminals[key];
  const { editBtn, editOps, editBrowser, editRemote, editBrowse,
    editInline, editClose } = t;
  const browserPathText = editBrowser.querySelector('.dev-browser-path-text');
  const browserList = editBrowser.querySelector('.dev-browser-list');
  const browserLoading = editBrowser.querySelector('.dev-browser-loading');
  const browserError = editBrowser.querySelector('.dev-browser-error');
  const browserConfirm = editBrowser.querySelector('.edit-browser-confirm');
  let selectedPath = '';
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

function removeTerminalPanel(key) {
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

async function dismissTerminal(key) {
  state.dismissedKeys[key] = true;
  // 串口标题栏关闭是释放接入，不得被手动刷新重新打开。
  window.TerminalWindows?.dismiss(key, {restore: !state.terminals[key]?.serialPort});
  if (state.terminals[key]?.computer) { await state.terminals[key].close(); return; }
  sendWS({type: 'close-terminal', key});
  state.knownKeys = state.knownKeys.filter(candidate => candidate !== key);
  updateTerminalPanels(); updateDeviceTabs(); updateLayoutStatus(); updateStatusBar();
}

function updateTerminalPanels() {
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
function terminalPanelKeys() {
  return [...state.knownKeys, ...Object.keys(state.terminals).filter(key => state.terminals[key].computer)];
}

function applyLayout(layout) {
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

function refreshAllTerminals() {
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
function setLayout(layout) {
  state.layout = layout;
  $$('.layout-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.layout === layout);
  });
  applyLayout(layout);
  updateLayoutStatus();
}

function initLayoutButtons() {
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
function resetLayout() {
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

function updateLayoutStatus() {
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
function updateStatusBar() {
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
function updateSelectMenus() {
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
function initDeviceRefresh() {
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
// Toast notifications
// ============================================================
function showToast(type, title, body, dedupeKey = null) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const existing = dedupeKey && [...container.children].find(item => item.dataset.dedupeKey === dedupeKey);
  if (existing) { existing.querySelector('.toast-body').textContent = body; return; }
  const toast = document.createElement('div');
  if (dedupeKey) toast.dataset.dedupeKey = dedupeKey;
  toast.className = `toast ${type}`;
  const icons = { success: '✓', error: '✗', info: 'ℹ' };
  toast.innerHTML = `
    <div class="toast-header">
      <span class="toast-icon">${icons[type] || 'ℹ'}</span>
      <span>${title}</span>
      <button class="toast-close">✕</button>
    </div>
    <div class="toast-body">${dedupeKey ? '' : body}</div>
  `;
  if (dedupeKey) toast.querySelector('.toast-body').textContent = body;
  toast.querySelector('.toast-close').addEventListener('click', () => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  });
  container.appendChild(toast);
  // Auto-remove after 15s
  setTimeout(() => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  }, 15000);
}

// ============================================================
// Toolbar file ops helpers (path history via localStorage)
// ============================================================
const FILE_HISTORY_KEY = 'adb-file-history';

function loadPathHistory(terminalKey, op) {
  try {
    const raw = localStorage.getItem(FILE_HISTORY_KEY);
    const all = raw ? JSON.parse(raw) : {};
    const entry = all[terminalKey] || {};
    return entry[op] || { local: '', remote: '' };
  } catch { return { local: '', remote: '' }; }
}

function savePathHistory(terminalKey, op, localPath, remotePath) {
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
function initSettings() {
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
function initAdbDiagnostic() {
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

function setDiagnosticControls() {
  const startBtn = document.getElementById('diag-start');
  const cancelBtn = document.getElementById('diag-cancel');
  const copyBtn = document.getElementById('diag-copy');
  const running = state.diagnostics.running;
  if (startBtn) startBtn.disabled = running;
  if (cancelBtn) cancelBtn.disabled = !running;
  if (copyBtn) copyBtn.disabled = running || !state.diagnostics.report;
}

function setDiagnosticProgress(text) {
  const el = document.getElementById('diag-progress');
  if (el) el.textContent = text;
}

function handleDiagnosticProgress(msg) {
  if (!state.diagnostics.running) return;
  state.diagnostics.requestId = msg.id;
  setDiagnosticProgress(`正在检查 ${msg.current}/${msg.total}：${msg.title}`);
}

function handleDiagnosticCancelled(msg) {
  if (state.diagnostics.running && msg.success) setDiagnosticProgress('正在取消诊断…');
}

function clearDiagnosticResults() {
  const results = document.getElementById('diag-results');
  if (results) results.replaceChildren();
}

function appendDiagnosticText(parent, tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text || '';
  parent.appendChild(el);
  return el;
}

function renderDiagnosticReport(report) {
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

function formatDiagnosticReport(report) {
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

// ============================================================
// Init
// ============================================================
function init() {
  initDragResize();
  initHighlight();
  initTopTabs();
  initTerminalFind();
  initLayoutButtons();
  initSettings();
  initRelay();
  initDeviceRefresh();
  connectWS();

  // 状态提示换行后高度会变化，终端随可用空间重新适配。
  if (window.ResizeObserver) {
    let statusHeight = 0;
    new ResizeObserver(entries => {
      const height = entries[0].contentRect.height;
      if (height !== statusHeight) {
        statusHeight = height;
        setTimeout(refreshAllTerminals, 100);
      }
    }).observe(document.getElementById('status-bar'));
  }

  window.addEventListener('resize', () => {
    if (state.currentTab === 'terminal') {
      setTimeout(refreshAllTerminals, 100);
    }
  });
}

init();
