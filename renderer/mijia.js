(() => {
  'use strict';
  const entry = document.getElementById('tools-mijia');
  if (!entry) return;
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
  document.body.appendChild(overlay);
  const $ = selector => overlay.querySelector(selector);
  const floating = new window.ToolFloatingWindow(overlay, $('.mijia-panel'), $('.mijia-head'),
    {width: 760, height: 660, minWidth: 320, side: 'left'});
  let epoch = 0;
  let detachedId = null, detachedReady = false, opening = false, popup = null, detachGeneration = 0;
  let controlCounter = 0, dragGesture = null, snapshotTimer = null;
  const visible = () => floating.visible || !!detachedId;
  let refreshEpoch = 0, refreshTimer = null, refreshing = false, operationPending = false;
  let autoRefresh = false, refreshQueued = false;
  const call = (action, args = {}) => callWS('mijia', {action, arguments:args, pageToken:MIJIA_PAGE_TOKEN}, action === 'snapshot' ? 180000 : 45000);
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
  entry.addEventListener('click', async () => {
    document.getElementById('tools-menu').style.display='none';
    if (detachedId) { sendWS({type:'desktop-terminal-raise',windowId:detachedId}); return; }
    if (floating.visible) { floating.raise(); return; }
    floating.show();
    const r=await call('status');
    if (!visible()) return;
    if (r.success && r.data.loggedIn) { autoRefresh=true; await refresh(true); }
    else note('请扫码登录米家账号');
  });
  $('.mijia-close').addEventListener('click', () => { ++detachGeneration; stopRefresh(); cancel(); closeDetached(); floating.hide(); entry.focus(); });
  $('.mijia-cancel').addEventListener('click', cancel);
  $('.mijia-login').addEventListener('click', login);
  $('.mijia-refresh').addEventListener('click', () => { if (autoRefresh) refresh(true); else note('请先扫码登录'); });
  $('.mijia-logout').addEventListener('click', async () => { stopRefresh(); cancel(); const r=await call('logout'); $('.mijia-grid').replaceChildren(); note(r.success ? '已清除本机登录信息' : r.output); });
  function closePopup() {
    if (!popup) return;
    try { popup.__desktopClosing = true; popup.close(); } catch (_) {}
    popup = null;
  }
  function closeDetached() {
    if (detachedId) sendWS({type:'desktop-terminal-destroy', windowId:detachedId});
    detachedId = null; detachedReady = false; closePopup();
  }
  function snapshot(initial = false) {
    if (!detachedId || !detachedReady) return;
    const panel = $('.mijia-panel');
    panel.querySelectorAll('button').forEach(element => {
      if (!element.dataset.mijiaControl) element.dataset.mijiaControl = String(++controlCounter);
    });
    const clone = panel.cloneNode(true);
    clone.removeAttribute('style');
    clone.classList.remove('tool-floating-window');
    clone.querySelectorAll('.tool-window-resize').forEach(element => element.remove());
    clone.querySelector('.mijia-detach')?.remove();
    sendWS({type:'desktop-terminal-publish',windowId:detachedId,
      payload:{kind:'mijia-snapshot',initial,html:clone.outerHTML}});
  }
  const observer = new MutationObserver(() => {
    clearTimeout(snapshotTimer); snapshotTimer = setTimeout(() => snapshot(), 40);
  });
  observer.observe($('.mijia-panel'), {subtree:true, childList:true, attributes:true, characterData:true});
  async function detach(x, y) {
    if (detachedId || opening || !floating.visible) return;
    opening = true;
    const generation = ++detachGeneration;
    if (!window.pywebview) popup = window.open('about:blank', '', 'popup,width=760,height=660');
    const rect = $('.mijia-panel').getBoundingClientRect();
    const result = await callWS('desktop-terminal-open', {view:'mijia',key:'mijia-panel',title:'米家设备',
      x:Math.round(x-120),y:Math.round(y-14),width:Math.round(rect.width),height:Math.round(rect.height)});
    opening = false;
    if (!result.success || generation !== detachGeneration || !floating.visible) {
      if (result.success) sendWS({type:'desktop-terminal-destroy',windowId:result.data.windowId});
      closePopup();
      if (!result.success && generation === detachGeneration) note('无法打开独立窗口，请检查浏览器弹窗权限');
      return;
    }
    detachedId = result.data.windowId;
    if (!result.data.native) {
      if (!popup) { closeDetached(); note('浏览器阻止了独立窗口，请允许弹窗'); return; }
      popup.location = result.data.url;
    } else closePopup();
    floating.hide();
    sendWS({type:'desktop-terminal-bind',windowId:detachedId});
  }
  function dock(fromChild = false) {
    if (!detachedId) return;
    const id = detachedId; detachedId = null; detachedReady = false;
    if (!fromChild) sendWS({type:'desktop-terminal-destroy',windowId:id});
    closePopup(); floating.show();
  }
  $('.mijia-detach').addEventListener('click', () => {
    const rect = $('.mijia-panel').getBoundingClientRect();
    detach(window.screenX + rect.left + 120, window.screenY + rect.top + 90);
  });
  $('.mijia-head').addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('button,input,select,a')) return;
    dragGesture = {id:event.pointerId};
  });
  document.addEventListener('pointermove', event => {
    if (!dragGesture || dragGesture.id !== event.pointerId) return;
    if (detachedId) {
      sendWS({type:'desktop-terminal-move',windowId:detachedId,x:Math.round(event.screenX-120),y:Math.round(event.screenY-14)});
    } else if (event.clientX < 0 || event.clientY < 0 || event.clientX > window.innerWidth || event.clientY > window.innerHeight) {
      detach(event.screenX,event.screenY);
    }
  });
  document.addEventListener('pointerup', () => { dragGesture = null; });
  document.addEventListener('pointercancel', () => { dragGesture = null; });
  window.MijiaIntegration = {
    detach,
    accept(msg) {
      if (!detachedId || msg.windowId !== detachedId) return false;
      if (msg.type === 'desktop-terminal-ready' || msg.type === 'desktop-terminal-resync') {
        detachedReady = true; snapshot(true);
      } else if (msg.type === 'desktop-terminal-event') {
        const payload = msg.payload || {};
        if (payload.kind === 'control' && payload.event === 'click' && Number.isSafeInteger(payload.control)) {
          const element = $('.mijia-panel').querySelector(`[data-mijia-control="${payload.control}"]`);
          if (element && !element.disabled) element.click();
        }
      } else if (msg.type === 'desktop-terminal-dock') dock(true);
      else if (msg.type === 'desktop-terminal-closed') { detachedId = null; detachedReady = false; closePopup(); $('.mijia-close').click(); }
      return true;
    }
  };
  window.addEventListener('beforeunload', () => { stopRefresh(); observer.disconnect(); clearTimeout(snapshotTimer); closeDetached(); floating.destroy(); });
})();
