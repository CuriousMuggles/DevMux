import { $, $$, base64ToUint8Array, state, terminalDecoders } from './core.js';
import { callWS, callWSDefault, connectWS, pendingReqs, sendWS, setWs, ws } from './net.js';
import { showToast } from './notify.js';
import {
  closeRelayConfigDialog,
  closeRelayConfirmDialog,
  initRelay,
  isRemoteDevice,
  loadRelayConfig,
  openRelayConfigDialog,
  pairSelectedPeer,
  refreshRelayStatus,
  relayConfigDialogOpen,
  relayConfigDirty,
  relaySelectedPeer,
  relayVisibleState,
  renderRelayDeployments,
  renderRelayEffective,
  renderRelayPool,
  updateRelayStatusUi,
} from './relay.js';
import {
  _deviceKey,
  _initTerminal,
  applyLayout,
  createTerminalPanel,
  dismissTerminal,
  handleDiagnosticCancelled,
  handleDiagnosticProgress,
  initDeviceRefresh,
  initDragResize,
  initHighlight,
  initLayoutButtons,
  initSettings,
  initTerminalFind,
  initTopTabs,
  refreshAllTerminals,
  selectActiveDevice,
  setLayout,
  syncTerminalControls,
  terminalPanelKeys,
  updateDeviceTabs,
  updateLayoutStatus,
  updateSelectMenus,
  updateStatusBar,
  updateTerminalPanels,
} from './terminal.js';
// ============================================================
// DevMux - Frontend (WebSocket version)
// ============================================================
console.log('[APP] v=51 loaded, CMD_STORAGE_KEY=' + 'quick-commands-data');


function handleWSMessage(msg) {
  if (String(msg.type || '').startsWith('desktop-terminal-')) { if (!window.PluginIntegration?.accept(msg) && !window.SerialControlsWindow?.accept(msg)) window.TerminalWindows?.accept(msg); return; }
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
// Init
// ============================================================
// WS 连接建立后的业务刷新：状态与配置在这里组合，传输层保持无业务依赖。
function onWsOpen() {
  refreshRelayStatus();
  // 配置不许盖掉用户正在弹窗里编、还没保存的改动，那种情况下等他保存或点刷新即可。
  if (!relayConfigDialogOpen() || !relayConfigDirty()) loadRelayConfig();
}

function init() {
  initDragResize();
  initHighlight();
  initTopTabs();
  initTerminalFind();
  initLayoutButtons();
  initSettings();
  initRelay();
  initDeviceRefresh();
  connectWS({ onOpen: onWsOpen, onMessage: handleWSMessage });

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

// ============================================================
// 过渡桥：其余 13 个脚本仍是经典脚本，按全局名调用这些符号。
// 每个符号迁移到 ESM 后删掉对应条目；**新增跨文件调用请改成 import**，
// 往这里加名字等于把刚去掉的全局污染又加回来。
// ============================================================
Object.assign(window, {
  $, $$, state,
  sendWS, showToast,
  // 请求原语：默认实现放出去给经典脚本与 e2e 用（它们按全局名调用 / 覆盖），
  // 覆盖后 ESM 内部调用也会走它，见 net.js 的 activeCallWS。
  callWS: callWSDefault,
  updateDeviceTabs, selectActiveDevice,
  applyLayout, updateLayoutStatus, updateStatusBar,
  _initTerminal, terminalPanelKeys, setLayout,
  // 终端面板与文件操作
  createTerminalPanel, updateTerminalPanels, syncTerminalControls,
  dismissTerminal, handleWSMessage,
  // 设备 key 计算：e2e 白盒脚本（tests/test_terminal_windows.py）在页面上下文
  // 按全局名取它构造 devices-updated 的 knownKeys；生产代码请 import。
  _deviceKey,
  // 跨机中转：e2e 驱动脚本（tests/test_ui_e2e.py UI_SCRIPT）在页面上下文
  // 按全局名白盒调用这一组；二期切出 relay 特性模块后随迁。
  openRelayConfigDialog, closeRelayConfigDialog, closeRelayConfirmDialog,
  relayConfigDialogOpen, loadRelayConfig, pairSelectedPeer,
  renderRelayPool, renderRelayEffective, renderRelayDeployments,
  relayVisibleState, relaySelectedPeer, relayConfigDirty,
  updateRelayStatusUi,
});
// ws 是可变绑定（重连时会换实例），必须用访问器桥接，否则经典脚本拿到的是旧值。
Object.defineProperty(window, 'ws', {
  get: () => ws,
  set: (value) => setWs(value),
  configurable: true,
});

