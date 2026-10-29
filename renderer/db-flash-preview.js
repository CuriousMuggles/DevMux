// 独立预览传输；正式接入时由现有 callWS 替换，不修改公共 app.js。
(() => {
  const pending = new Map();
  let sequence = 0;
  let socket;
  let retry;
  function connect() {
    socket = new WebSocket('ws://127.0.0.1:__DB_FLASH_WS_PORT__');
    socket.addEventListener('message', event => {
      const msg = JSON.parse(event.data);
      if (msg.type === 'db-flash-progress') { panel.acceptProgress(msg); return; }
      const item = pending.get(msg.id);
      if (item) { clearTimeout(item.timer); pending.delete(msg.id); item.resolve(msg); }
    });
    socket.addEventListener('close', () => {
      for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('模拟服务连接已断开')); }
      pending.clear();
      retry = setTimeout(connect, 1000);
    });
    socket.addEventListener('error', () => {});
  }
  async function request(type, args) {
    if (!socket || socket.readyState !== WebSocket.OPEN) throw new Error('模拟服务未连接，请稍后重试');
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('请求超时')); }, 125000);
      pending.set(id, {resolve, reject, timer});
      socket.send(JSON.stringify({type, id, ...args}));
    });
  }
  const panel = new window.DBFlashPanel(document.getElementById('db-flash-preview'), request);
  document.getElementById('db-flash-open').addEventListener('click', () => panel.open());
  window.dbFlashPreview = panel;
  connect();
  window.addEventListener('beforeunload', () => { clearTimeout(retry); panel.dispose(); });
})();
