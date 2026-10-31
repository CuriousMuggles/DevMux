// 子窗只代理主页面的米家控件，不获取账号凭据或调用米家服务。
(() => {
  const [windowId, token] = location.hash.slice(1).split(':');
  history.replaceState(null, '', location.pathname);
  const host = document.getElementById('mijia-child-host');
  const message = document.getElementById('mijia-child-message');
  const ws = new WebSocket('ws://127.0.0.1:' + WS_PORT + '/desktop-terminal');
  let closing = false;
  function action(value) {
    if (ws.readyState === 1) ws.send(JSON.stringify({type:'desktop-terminal-action', action:value}));
    if (value === 'dock' || value === 'close') closing = true;
  }
  ws.onopen = () => ws.send(JSON.stringify({type:'desktop-terminal-attach', windowId, token}));
  ws.onmessage = event => {
    const msg = JSON.parse(event.data), data = msg.payload;
    if (msg.type !== 'desktop-terminal-view' || data?.kind !== 'mijia-snapshot') return;
    const template = document.createElement('template'); template.innerHTML = data.html;
    template.content.querySelectorAll('script').forEach(element => element.remove());
    const panel = template.content.firstElementChild;
    if (!panel?.classList.contains('mijia-panel')) return;
    const dock = document.createElement('button'); dock.className = 'mijia-child-dock tool-btn'; dock.textContent = '返回主窗口';
    panel.querySelector('.mijia-head > div')?.prepend(dock);
    host.replaceChildren(panel); message.hidden = true;
  };
  ws.onclose = event => {
    if (closing || event.reason === '独立视图已回收') { closing = true; window.close(); return; }
    message.hidden = false; message.textContent = '主应用连接已结束，无法操作设备。';
  };
  document.addEventListener('click', event => {
    if (event.target.closest('.mijia-child-dock')) { action('dock'); return; }
    if (event.target.closest('.mijia-close')) { action('close'); return; }
    const element = event.target.closest('[data-mijia-control]');
    if (!element || element.disabled || ws.readyState !== 1) return;
    const control = Number(element.dataset.mijiaControl);
    if (!Number.isSafeInteger(control)) return;
    ws.send(JSON.stringify({type:'desktop-terminal-event', payload:{kind:'control',event:'click',control}}));
  });
  window.addEventListener('beforeunload', () => { if (!closing && !window.__desktopClosing) action('close'); });
})();
