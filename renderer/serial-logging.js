// 串口日志与ADB采用相同路径选择、追加和时间戳交互；数据由服务端接收游标捕获。
window.SerialLogging = function(panel, key, target, sessionId, getCursor) {
  const toolbar = panel.querySelector('.cell-toolbar'), button = panel.querySelector('.serial-log');
  const area = document.createElement('div'); area.className = 'log-submenu'; area.style.display = 'none';
  area.innerHTML = `<div class="log-submenu-row">
    <input class="log-save-path" type="text" placeholder="本机日志文件路径…" spellcheck="false">
    <button class="log-browse" title="选择或新建日志文件（已有文件将追加）">📁</button>
    <button class="log-timestamp-toggle selected" aria-pressed="true" title="逐行添加时间戳">⏱ 时间戳</button>
    <button class="log-toggle primary">🔴 记录</button></div><div class="serial-log-state" role="status"></div>`;
  toolbar.appendChild(area);
  const find = name => area.querySelector('.log-' + name);
  const path = find('save-path'), browse = find('browse'), stamps = find('timestamp-toggle'), toggle = find('toggle');
  const status = area.querySelector('.serial-log-state');
  const format = panel.querySelector('.serial-format'), encoding = panel.querySelector('.serial-encoding');
  path.value = localStorage.getItem('serial-terminal-save-path') || '';
  let running = false, busy = false, disposed = false;
  const stamp = value => { stamps.dataset.enabled = String(value); stamps.classList.toggle('selected', value); stamps.setAttribute('aria-pressed', String(value)); };
  stamp(localStorage.getItem('terminal-record-timestamps') !== 'false');
  function update() {
    toggle.dataset.running = String(running); toggle.disabled = busy || disposed;
    toggle.textContent = running ? '⏹ 停止' : '🔴 记录'; toggle.className = 'log-toggle ' + (running ? 'danger' : 'primary');
    for (const control of [path, browse, stamps, format, encoding]) control.disabled = running || busy || disposed;
    button.classList.toggle('selected', running); button.title = running ? '串口日志记录中' : '串口日志记录';
  }
  const call = action => callWS('serial-recording', {action, target: target.target, sessionId,
    path: path.value.trim(), cursor: getCursor(), format: format.value, encoding: encoding.value,
    timestamps: stamps.dataset.enabled === 'true'});
  button.onclick = () => {
    const open = area.style.display !== 'block';
    const shortcuts = panel.querySelector('.serial-command-popup'); if (shortcuts) shortcuts.style.display = 'none';
    area.style.display = open ? 'block' : 'none';
  };
  path.onchange = () => localStorage.setItem('serial-terminal-save-path', path.value);
  stamps.onclick = () => { stamp(stamps.dataset.enabled !== 'true'); localStorage.setItem('terminal-record-timestamps', stamps.dataset.enabled); };
  browse.onclick = async () => {
    const result = await callWS('pick-recording-file', {suggestedName: 'serial-' + sessionId + '-' + Date.now() + '.log'});
    if (!disposed && result.success && result.path) { path.value = result.path; path.onchange(); }
    else if (!disposed && result.output) status.textContent = result.output;
  };
  toggle.onclick = async () => {
    if (busy || disposed) return;
    if (!running && !path.value.trim()) { status.textContent = '请先选择本机日志保存路径'; return; }
    busy = true; update();
    try {
      const result = await call(running ? 'stop' : 'start');
      if (disposed) return;
      if (result.success) running = !running;
      status.textContent = result.output || (result.success ? '已完成' : '日志操作失败');
    } catch (error) { if (!disposed) status.textContent = error.message || '日志操作失败'; }
    finally { busy = false; update(); }
  };
  return {
    error(message) { running = false; status.textContent = message; area.style.display = 'block'; update(); },
    async stop() { if (!running || disposed) return; try { await call('stop'); } finally { running = false; update(); } },
    dispose() { disposed = true; running = false; update(); }
  };
};
