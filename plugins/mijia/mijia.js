(() => {
  'use strict';
  const overlay = document.createElement('div');
  overlay.className = 'mijia-overlay diagnostic-overlay';
  overlay.hidden = true;
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'false');
  overlay.setAttribute('aria-label', '米家设备');
  overlay.innerHTML = `<section class="mijia-panel diagnostic-dialog">
    <header class="mijia-head diagnostic-header"><h3>米家设备</h3><div><button class="mijia-detach tool-btn" title="打开独立窗口">独立窗口</button> <button class="mijia-close diagnostic-close" aria-label="关闭">✕</button></div></header>
    <main class="mijia-body"><div class="mijia-note" role="status"></div>
      <div class="mijia-actions"><button class="mijia-login tool-btn primary">扫码登录</button><button class="mijia-refresh tool-btn">刷新设备</button><button class="mijia-logout tool-btn">退出登录</button></div>
      <div class="mijia-qr" hidden><p>请使用米家 App 扫码并确认登录</p><img alt="米家登录二维码"><p><button class="mijia-cancel tool-btn">取消登录</button></p></div>
      <div class="mijia-grid"></div></main></section>`;
  const dialog=overlay.querySelector('.mijia-panel');
  const $ = selector => dialog.querySelector(selector);
  $('.mijia-head').hidden=true;
  const floating=DevMuxPlugin.ui.mount({title:'米家设备',description:'登录米家账号，查看与控制本机设备',content:dialog});
  let epoch = 0;
  const visible = () => floating.visible;
  let refreshEpoch = 0, refreshTimer = null, refreshing = false, operationPending = false;
  let autoRefresh = false, refreshQueued = false;
  const call = (action, args = {}) => DevMuxPlugin.request('plugin', {action, arguments:args}, action === 'snapshot' ? 180000 : 45000);
  const note = text => { $('.mijia-note').textContent = text; };
  function stopRefresh() { autoRefresh = false; refreshQueued = false; ++refreshEpoch; clearTimeout(refreshTimer); }
  function scheduleRefresh(delay = 5000) {
    clearTimeout(refreshTimer);
    if (autoRefresh && visible()) refreshTimer = setTimeout(() => refresh(false), delay);
  }
  async function refresh(force = false) {
    clearTimeout(refreshTimer);
    if (!visible() || !autoRefresh) return;
    if (refreshing || operationPending) { refreshQueued = refreshQueued || force; return; }
    const current = ++refreshEpoch;
    refreshing = true;
    if (force || !$('.mijia-grid').children.length) note('正在查找可操作设备…');
    let result;
    try { result = await call('snapshot', {refreshCatalog:force}); }
    finally { refreshing = false; }
    if (current !== refreshEpoch || !visible() || !autoRefresh) {
      if (autoRefresh && visible() && !operationPending && refreshQueued) { refreshQueued=false; refresh(true); }
      return;
    }
    if (!result.success) {
      note('状态刷新失败，显示的可能是旧状态；请检查网络后刷新');
      // 超时后后端可能仍在执行，不自动排入新一轮请求。

      return;
    }
    const devices = result.data.devices;
    const grid = $('.mijia-grid');
    const existing = new Map([...grid.children].map(card => [card.dataset.did, card]));
    for (const d of devices) {
      if (typeof d.on !== 'boolean') continue;
      let card = existing.get(d.did);
      if (!card) {
        card = document.createElement('article'); card.className = 'mijia-card'; card.dataset.did = d.did;
        const name=document.createElement('div'); name.className='mijia-name';
        const room=document.createElement('div'); room.className='mijia-sub mijia-room';
        const model=document.createElement('div'); model.className='mijia-sub mijia-model';
        const button=document.createElement('button'); button.className='tool-btn';
        card.append(name,room,model,button); grid.appendChild(card);
        button.addEventListener('click', async () => {
          if (operationPending) return;
          operationPending = true; ++refreshEpoch; clearTimeout(refreshTimer);
          button.disabled = true; button.textContent = '正在操作…';
          const expected = card.dataset.on !== 'true';
          const deadline = performance.now() + 3000;
          const bounded = promise => Promise.race([promise, new Promise(resolve => setTimeout(() => resolve(null), Math.max(0, deadline - performance.now())))]);
          try {
            let response = await bounded(call('set-switch', {did:card.dataset.did,value:expected}));
            while (response && performance.now() < deadline && !(response.success && response.data?.on === expected)) {
              await new Promise(resolve => setTimeout(resolve, Math.min(100, Math.max(0, deadline - performance.now()))));
              if (performance.now() >= deadline) break;
              response = await bounded(call('read-switch', {did:card.dataset.did}));
            }
            if (response?.success && response.data?.on === expected && performance.now() <= deadline) {
              card.dataset.on = String(expected); renderButton(card); button.disabled = false;
            } else { button.textContent = '状态未知，请刷新'; note('操作未在 3 秒内确认，请刷新设备后查看'); }
          } catch (_) { button.textContent = '状态未知，请刷新'; note('操作结果未确认，请刷新设备后查看'); }
          finally {
            operationPending = false;
            if (autoRefresh && visible() && refreshQueued) { refreshQueued=false; refresh(true); }
          }
        });
      }
      existing.delete(d.did);
      card.querySelector('.mijia-name').textContent=d.name;
      card.querySelector('.mijia-room').textContent=`${d.room} · 在线`;
      card.querySelector('.mijia-model').textContent=d.model;
      card.dataset.on=String(d.on); card.querySelector('button').disabled=false; renderButton(card);
    }
    for (const card of existing.values()) card.remove();
    const count=grid.children.length;
    const suffix=result.data.unavailable ? `；${result.data.unavailable} 台状态暂不可用` : '';
    note(count ? `共 ${count} 台可操作设备${suffix}` : `没有找到可操作设备${suffix}，请检查设备在线状态或稍后刷新`);
    const queued=refreshQueued; refreshQueued=false; if (queued) refresh(true);
  }
  function renderButton(card) {
    const button=card.querySelector('button'), on=card.dataset.on==='true';
    button.textContent=on ? '已开启 · 点击关闭' : '已关闭 · 点击开启';
    button.classList.toggle('primary', on);
  }
  async function login() {
    const current = ++epoch; $('.mijia-qr').hidden = false; $('.mijia-qr img').removeAttribute('src'); note('正在准备二维码…');
    const started = await call('login'); if (!started.success) { note(started.output); return; }
    while (epoch === current && visible()) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const r = await call('login-status'); if (epoch !== current) return;
      if (!r.success) { note(r.output); return; }
      if (r.data.qr) { $('.mijia-qr img').src = 'data:image/svg+xml;base64,' + r.data.qr; note('请扫码登录'); }
      if (r.data.state === 'success') { $('.mijia-qr').hidden = true; $('.mijia-qr img').removeAttribute('src'); autoRefresh=true; await refresh(true); return; }
      if (r.data.state === 'error') { $('.mijia-qr').hidden = true; note('登录失败或二维码已过期，请重试'); return; }
    }
  }
  function cancel() { ++epoch; $('.mijia-qr').hidden = true; $('.mijia-qr img').removeAttribute('src'); call('cancel'); }
  async function start() {
    floating.show();
    const r=await call('status');
    if (!visible()) return;
    if (r.success && r.data.loggedIn) { autoRefresh=true; await refresh(true); }
    else note('请扫码登录米家账号');
  }
  $('.mijia-close').addEventListener('click', () => {stopRefresh();cancel();DevMuxPlugin.close();});
  $('.mijia-cancel').addEventListener('click', cancel);
  $('.mijia-login').addEventListener('click', login);
  $('.mijia-refresh').addEventListener('click', () => { if (autoRefresh) refresh(true); else note('请先扫码登录'); });
  $('.mijia-logout').addEventListener('click', async () => { stopRefresh(); cancel(); const r=await call('logout'); $('.mijia-grid').replaceChildren(); note(r.success ? '已清除本机登录信息' : r.output); });
  DevMuxPlugin.onDispose(()=>{++epoch;stopRefresh();});
  start();
})();
