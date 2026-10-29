// 本机继电器面板：设备打开不改变通断，状态灯来自 DLL 实际读取。
(() => {
  const overlay = document.createElement('div');
  overlay.id = 'usb-relay-overlay';
  overlay.className = 'diagnostic-overlay';
  overlay.style.display = 'none';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'false');
  overlay.setAttribute('aria-labelledby', 'usb-relay-title');
  overlay.innerHTML = `<div class="diagnostic-dialog usb-relay-dialog">
    <div class="diagnostic-header"><div><h3 id="usb-relay-title">继电器控制</h3>
      <p class="usb-relay-subtitle">本机 / 远程从机控制</p></div>
      <button id="usb-relay-dismiss" class="diagnostic-close" aria-label="关闭面板">✕</button></div>
    <div id="usb-relay-target-context"></div>
    <div class="usb-relay-device-row"><button id="usb-relay-scan" class="tool-btn">查找设备</button>
      <label class="usb-relay-sr-only" for="usb-relay-device">继电器设备</label>
      <select id="usb-relay-device" class="tool-select"><option value="">请先查找设备</option></select></div>
    <div class="usb-relay-connection"><button id="usb-relay-open" class="tool-btn primary">打开设备</button>
      <span id="usb-relay-connection-state"><i class="usb-relay-led"></i><span>未打开</span></span>
      <button id="usb-relay-close" class="tool-btn">关闭设备</button></div>
    <div class="usb-relay-channel-head"><span>继电器通道</span><button id="usb-relay-refresh" class="tool-btn">刷新状态</button></div>
    <div id="usb-relay-channels" class="usb-relay-channels"></div>
    <div class="usb-relay-all"><button id="usb-relay-all-open" class="tool-btn">打开所有</button>
      <button id="usb-relay-all-close" class="tool-btn">关闭所有</button></div>
    <div class="usb-relay-legend"><span><i class="usb-relay-led on"></i>已接通</span>
      <span><i class="usb-relay-led off"></i>已断开</span><span><i class="usb-relay-led"></i>未知</span></div>
    <p class="usb-relay-note">关闭设备或面板不会断开继电器。要断开全部通道，请点击「关闭所有」。</p>
    <details id="usb-relay-diagnostics"><summary>分阶段诊断（只读）</summary>
      <p>每次只执行选中的阶段及其前置调用。展开时暂停自动刷新；失败后请保留日志。</p>
      <div class="usb-relay-diagnostic-row">
        <select id="usb-relay-stage" class="tool-select" aria-label="诊断阶段">
          <option value="check">1 文件检查</option><option value="load">2 DLL 加载</option>
          <option value="init">3 初始化</option><option value="scan">4 枚举设备</option><option value="status">5 读取状态</option>
        </select><input id="usb-relay-diagnostic-serial" class="tool-input" placeholder="设备序列号，如 RELAY1234" aria-label="诊断设备序列号">
        <button id="usb-relay-diagnose" class="tool-btn">执行选中阶段</button>
      </div>
    </details>
    <p id="usb-relay-message" role="status" aria-live="polite">查找并打开设备。</p>
  </div>`;
  document.body.appendChild(overlay);
  const floating = new window.ToolFloatingWindow(overlay, overlay.querySelector('.usb-relay-dialog'),
    overlay.querySelector('.diagnostic-header'), {width: 560, height: 720, side: 'right'});
  const el = suffix => document.getElementById('usb-relay-' + suffix);
  let model = {devices: [], states: [], opened: false, channels: 0, serial: ''};
  let busy = false;
  let refreshPending = null;
  let previousFocus;
  let timer;
  const context = new RemoteToolContext(el('target-context'), 'relay', async () => {
    model = {devices: [], states: [], opened: false, channels: 0, serial: ''};
    el('message').textContent = context.status.textContent;
    render();
    if (context.ready) await call('config');
  }, '继电器');
  function render() {
    const blocked = busy || !context.ready;
    const selected = el('device').value;
    el('device').replaceChildren();
    if (!model.devices.length) el('device').appendChild(new Option('未发现设备', ''));
    for (const device of model.devices) el('device').appendChild(new Option(`${device.serial} · ${device.channels} 路`, device.serial));
    el('device').value = model.opened ? model.serial : (model.devices.some(d => d.serial === selected) ? selected : model.devices[0]?.serial || '');
    const connected = el('connection-state');
    connected.firstElementChild.className = 'usb-relay-led' + (model.opened ? ' on' : '');
    connected.lastElementChild.textContent = model.opened ? '已打开' : '未打开';
    el('channels').replaceChildren();
    const count = model.channels || model.devices.find(d => d.serial === el('device').value)?.channels || 8;
    for (let i = 0; i < count; i++) {
      const value = model.opened ? model.states[i] : null;
      const label = value === true ? '已接通' : value === false ? '已断开' : '未知';
      const row = document.createElement('div');
      row.className = 'usb-relay-channel';
      row.innerHTML = `<span class="usb-relay-channel-name">继电器 ${i + 1}</span>
        <button class="tool-btn" data-operation="open" data-channel="${i + 1}">打开</button>
        <span class="usb-relay-channel-state"><i class="usb-relay-led ${value === true ? 'on' : value === false ? 'off' : ''}"></i><span>${label}</span></span>
        <button class="tool-btn" data-operation="close" data-channel="${i + 1}">关闭</button>`;
      row.querySelectorAll('button').forEach(button => {
        button.disabled = blocked || !model.opened;
        button.setAttribute('aria-label', `${button.textContent}继电器 ${i + 1}`);
      });
      el('channels').appendChild(row);
    }
    for (const name of ['scan', 'stage', 'diagnostic-serial', 'diagnose']) el(name).disabled = blocked;
    el('device').disabled = blocked || model.opened;
    el('open').disabled = blocked || model.opened || !el('device').value;
    for (const name of ['close', 'refresh', 'all-open', 'all-close']) el(name).disabled = blocked || !model.opened;
    context.setBusy(busy);
    overlay.setAttribute('aria-busy', String(busy || context.loading));
  }
  async function call(action, args = {}, quiet = false) {
    if (quiet && action === 'status') return refreshStatus();
    if (busy || !context.ready) return;
    busy = true;
    if (!quiet) el('message').textContent = '正在执行…';
    render();
    try {
      // 用户操作等待已有读取结束，不能丢弃点击或并发占用设备。
      if (refreshPending) await refreshPending;
      const result = await context.call(action, args);
      if (result.stale) return;
      if (result.data) model = result.data;
      else model.states = Array(model.channels).fill(null);
      if (!quiet || !result.success) {
        el('message').textContent = result.output === 'timeout' ? '连接超时，操作结果未确认，请刷新状态。' : result.output || '状态已刷新';
        el('message').classList.toggle('error', !result.success);
      }
      return result;
    } finally {
      busy = false;
      render();
    }
  }
  function renderStates() {
    [...el('channels').children].forEach((row, i) => {
      const value = model.opened ? model.states[i] : null;
      const state = row.querySelector('.usb-relay-channel-state');
      state.firstElementChild.className = 'usb-relay-led' + (value === true ? ' on' : value === false ? ' off' : '');
      state.lastElementChild.textContent = value === true ? '已接通' : value === false ? '已断开' : '未知';
    });
  }
  async function refreshStatus() {
    if (busy || refreshPending || !context.ready) return;
    // 后台读取不切换 busy、不重建按钮，保留按钮外观和键盘焦点。
    refreshPending = (async () => {
      try {
        const result = await context.call('status');
        if (result.stale) return;
        const previous = model;
        if (result.data) model = result.data;
        else model.states = Array(model.channels).fill(null);
        if (!result.success) {
          el('message').textContent = result.output === 'timeout' ? '连接超时，状态未确认，请刷新状态。' : result.output || '状态读取失败';
          el('message').classList.add('error');
        }
        if (!context.ready || model.opened !== previous.opened || model.channels !== previous.channels || model.serial !== previous.serial) render();
        else renderStates();
      } catch (error) {
        model.states = Array(model.channels).fill(null);
        renderStates();
        el('message').textContent = '状态读取失败：' + error.message;
        el('message').classList.add('error');
      }
    })();
    try { await refreshPending; } finally { refreshPending = null; }
  }
  function dismiss() {
    floating.hide();
    clearInterval(timer);
    const focus = previousFocus?.getClientRects().length && !previousFocus.disabled
      ? previousFocus : document.getElementById('tools-tab');
    focus?.focus();
  }
  document.getElementById('tools-usb-relay').addEventListener('click', async () => {
    document.getElementById('tools-menu').style.display = 'none';
    if (floating.visible) { floating.raise(); return; }
    previousFocus = document.activeElement;
    floating.show();
    el('dismiss').focus();
    await context.refresh();
    clearInterval(timer);
    timer = setInterval(() => {
      if (overlay.style.display !== 'none' && model.opened && context.ready && !busy && !document.hidden && !el('diagnostics').open) call('status', {}, true);
    }, 5000);
  });
  el('dismiss').addEventListener('click', dismiss);
  overlay.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.stopPropagation(); dismiss(); }
  });
  el('scan').addEventListener('click', () => call('scan'));
  el('diagnose').addEventListener('click', () => call('diagnose', {stage: el('stage').value,
    serial: el('diagnostic-serial').value.trim() || el('device').value}));
  el('device').addEventListener('change', render);
  el('open').addEventListener('click', () => call('open', {serial: el('device').value}));
  el('close').addEventListener('click', () => call('close'));
  el('refresh').addEventListener('click', () => call('status'));
  el('channels').addEventListener('click', e => {
    const button = e.target.closest('button[data-operation]');
    if (button && !button.disabled) call('set', {channel: Number(button.dataset.channel), operation: button.dataset.operation});
  });
  for (const operation of ['open', 'close']) el('all-' + operation).addEventListener('click', () => call('set', {channel: 255, operation}));
  render();
})();
