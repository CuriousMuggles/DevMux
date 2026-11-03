// ============================================================
// 共享原语：应用状态、DOM 助手、编解码
// ============================================================
// 这一层只放「谁都要用、自己不依赖任何界面」的东西。依赖方向固定为：
//   core.js  <-  各特性模块  <-  app.js（入口）
// 反向依赖会让 ESM 在导入期成环，宁可把某个函数留在上层，也不要往这里加
// 需要读 DOM 结构或调用界面函数的代码。

// Global application state
export const state = {
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
export const terminalDecoders = new Map();

// DOM helpers
export const $ = (s) => document.querySelector(s);
export const $$ = (s) => document.querySelectorAll(s);

export function base64ToUint8Array(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
