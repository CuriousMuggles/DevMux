// ============================================================
// WebSocket 传输层：连接、请求-响应配对、原生 WS 发送。
// 只依赖 core.js；上层的 onOpen / onMessage 通过回调注入，避免反向依赖。
// ============================================================
import { state } from './core.js';

const WS_URL = `ws://127.0.0.1:${typeof WS_PORT !== 'undefined' ? WS_PORT : 8765}`;
export let ws = null;  // 实时绑定：导入方读到的是当前值
let wsReconnectTimer = null;
let reqIdCounter = 0;
// 回调只在首次 connectWS(config) 时注入；scheduleReconnect() 无参重连时继续复用，
// 否则断线重连之后消息会没有人分派。
let wsCallbacks = {};
export const pendingReqs = new Map();

export function setWs(value) { ws = value; }

// ============================================================
// WebSocket layer
// ============================================================
export function connectWS({ onOpen, onMessage } = {}) {
  if (onOpen || onMessage) wsCallbacks = { onOpen, onMessage };
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
    window.PluginIntegration?.refresh();
    updateConnectionStatus(true);
    // 连接建立后的业务刷新由组合根 app.js 注入，传输层不认识中转界面。
    wsCallbacks.onOpen?.();
  };

  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      wsCallbacks.onMessage?.(msg);
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

export function scheduleReconnect() {
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    connectWS();
  }, 2000);
}

export function sendWS(msg) {
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
export function reportUiAction(action, detail = '') {
  sendWS({ type: 'ui-debug', action, detail });
}

// 请求层留一个可替换的门钩。经典脚本与 e2e 驱动脚本历来直接给全局 callWS 赋值
// 来拦截请求（数次数、造失败）；模块化后 window 上的赋值不落到模块内调用点，
// 补丁会静默失效，页面只表现为「点了按钮没反应」。
export const callWSDefault = function callWSDefault(type, payload = {}, timeoutMs = 15000) {
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
};
// 替换实现拿到的 original 必须是真实实现而不是门函数，否则「包一层再转发」
// 的写法会自递归；而经典脚本的裸调用又必须被拦到。所以 window.callWS 的默认值
// 就是 callWSDefault：测试存下来的 original 是它，覆盖之后 ESM 内部调用再尊重覆盖。
// 这个门钩只服务于过渡期桥接层，模块内部一律 import callWS。
function activeCallWS() {
  const external = typeof window !== 'undefined' ? window.callWS : null;
  if (typeof external === 'function' && external !== callWS && external !== callWSDefault) {
    return external;
  }
  return callWSDefault;
}

export function callWS(type, payload = {}, timeoutMs = 15000) {
  return activeCallWS()(type, payload, timeoutMs);
}


export function updateConnectionStatus(connected) {
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
