// PowerShell：选择本机或已连接从机，打开独立的 PowerShell 交互会话。
(() => {
  const selectOverlay = document.createElement('div');
  selectOverlay.id = 'powershell-select-overlay';
  selectOverlay.className = 'diagnostic-overlay';
  selectOverlay.style.display = 'none';
  selectOverlay.setAttribute('role', 'dialog');
  selectOverlay.setAttribute('aria-modal', 'false');
  selectOverlay.setAttribute('aria-label', 'PowerShell');
  selectOverlay.innerHTML = `<div class="diagnostic-dialog relay-config-dialog powershell-select-dialog">
    <div class="diagnostic-header"><h3>PowerShell</h3><button id="powershell-select-close" class="diagnostic-close" title="关闭">✕</button></div>
    <p>选择电脑，打开 PowerShell 终端。命令在选定电脑上执行。</p>
    <div class="setting-row"><label for="powershell-target">电脑</label><select id="powershell-target" class="tool-select"></select>
      <button id="powershell-refresh" class="tool-btn">刷新</button></div>
    <div class="setting-row"><label><input id="powershell-allow-local" type="checkbox" checked disabled> 本机允许执行 PowerShell</label></div>
    <p class="relay-root-note">默认开启，修改后自动保存并立即生效。关闭将结束本机 PowerShell 交互会话。远程电脑的权限请在那台电脑的「PowerShell」中设置。</p>
    <p id="powershell-permission-status" class="relay-root-note" role="status"></p>
    <div class="setting-row"><button id="powershell-open" class="tool-btn primary" disabled>打开终端</button></div>
  </div>`;
  document.body.appendChild(selectOverlay);
  const floating = new window.ToolFloatingWindow(selectOverlay, selectOverlay.firstElementChild,
    selectOverlay.querySelector('.diagnostic-header'), {width: 680, height: 360, side: 'right'});
  const el = id => document.getElementById(id);
  const call = (name, args, timeout = 20000) => callWS('computer-tool', {name, arguments: args}, timeout);
  let targets = [];
  let selectionGeneration = 0;
  let opening = false;
  const terminals = new Set();

  async function permission() {
    const generation = ++selectionGeneration;
    el('powershell-open').disabled = true;
    const selected = targets.find(item => item.target === el('powershell-target').value);
    if (!selected || !selected.online) {
      el('powershell-permission-status').textContent = '电脑未连接';
      return;
    }
    el('powershell-permission-status').textContent = '正在检查连接与权限…';
    const result = await call('target_status', {target: selected.target});
    if (generation !== selectionGeneration) return;
    const allowed = result.success && result.data.allowed;
    el('powershell-open').disabled = !allowed || opening;
    el('powershell-permission-status').textContent = allowed ? '已允许 PowerShell，可以打开终端。'
      : result.success ? (selected.local ? '本机已关闭 PowerShell，请勾选上方权限开关。'
          : '从机已关闭 PowerShell，请在那台电脑的「PowerShell」中开启。') : result.output || '连接不可用';
  }

  async function refresh() {
    const generation = ++selectionGeneration;
    el('powershell-open').disabled = true;
    const checkbox = el('powershell-allow-local');
    checkbox.disabled = true;
    const local = await call('local_windows_permission', {});
    if (generation !== selectionGeneration) return;
    if (!local.success) {
      el('powershell-permission-status').textContent = local.output || '读取本机权限失败';
      return;
    }
    checkbox.checked = local.data.allowed;
    checkbox.disabled = false;
    const result = await call('targets_list', {});
    if (generation !== selectionGeneration) return;
    const list = el('powershell-target');
    const previous = list.value;
    targets = result.success ? result.data.targets.filter(item => item.kind === 'computer'
      && (item.local || item.role === 'slave')) : [];
    ComputerTargets.render(list, targets, previous);
    if (!result.success) el('powershell-permission-status').textContent = result.output || '读取电脑失败';
    else await permission();
  }

  function closeSelect() {
    floating.hide();
    ++selectionGeneration;
  }

  async function openTerminal(target, sessionId) {
    state.currentTab = 'terminal';
    $$('.tab-bar .tab').forEach(tab => tab.classList.toggle('active', tab.dataset.tab === 'terminal'));
    $$('.content-panel').forEach(panel => panel.classList.toggle('active', panel.id === 'panel-terminal'));
    const key = `powershell:${sessionId}`;
    const overlay = document.createElement('div');
    overlay.className = 'terminal-panel powershell-terminal-panel';
    overlay.dataset.key = key;
    overlay.setAttribute('aria-label', `PowerShell · ${target.label}`);
    overlay.innerHTML = `<div class="terminal-panel-header">
      <span class="header-title"></span><span class="header-badge"></span>
      <button class="header-btn minimize" title="最小化">−</button>
      <button class="header-btn maximize" title="最大化">□</button>
      <button class="header-btn close powershell-terminal-close" title="关闭终端">×</button></div>
      <div class="terminal-cell"><div class="terminal-xterm powershell-terminal-host"></div>
      <div class="cell-toolbar-handle" data-target="xterm" title="拖拽调整终端高度"></div>
      <div class="cell-toolbar"><div class="cell-toolbar-row">
        <span class="powershell-terminal-status" role="status"></span></div></div>
      <div class="cell-toolbar-handle" data-target="cmd-input" title="拖拽调整命令框高度"></div>
      <div class="terminal-cmd-input" style="height:34px"><textarea class="terminal-cmd-textarea" rows="1" spellcheck="false"
        placeholder="输入 PowerShell 命令，Enter 发送，Ctrl+Enter 换行"></textarea></div></div>`;
    const header = overlay.querySelector('.terminal-panel-header');
    header.querySelector('.header-title').textContent = `PowerShell · ${target.label}`;
    header.querySelector('.header-badge').textContent = target.local ? '本机' : '远程从机';
    header.classList.toggle('remote', !target.local);
    document.getElementById('terminal-container').appendChild(overlay);
    let terminalWindow;
    const host = overlay.querySelector('.powershell-terminal-host');
    const status = overlay.querySelector('.powershell-terminal-status');
    const {term, fitAddon: fit} = _initTerminal(key, target, host);
    const command = overlay.querySelector('.terminal-cmd-textarea');
    let alive = true;
    let cursor = 0;
    let pollTimer = null;
    let inputTimer = null;
    let pending = '';
    let writing = false;
    let resizeTimer = null;
    let resizePending = null;
    let resizing = false;
    let reading = false;
    let activeUntil = 0;
    const context = {overlay, close};
    terminals.add(context);
    state.terminals[key] = {key, computer: target, term, fitAddon: fit, panel: overlay,
      header, xtermDiv: host, cmdTextarea: command, close, floatingWindow: null, nativeInput: input};
    terminalWindow = TerminalWindows.attach(key, overlay, header, {width: 820, height: 600});
    TerminalWindows.remember(key, async () => {
      const reopened = await call('session_open', {target: target.target, cols: 120, rows: 32});
      if (!reopened.success) throw new Error(reopened.output || 'PowerShell目标不可用');
      await openTerminal(target, reopened.data.sessionId); return true;
    });
    updateDeviceTabs();
    selectActiveDevice(key);
    applyLayout(state.layout);
    updateLayoutStatus();
    updateStatusBar();
    term.focus();
    status.textContent = '已连接 · 关闭窗口将结束此 PowerShell 会话';

    function fail(message) {
      if (!alive) return;
      status.textContent = message;
      term.options.disableStdin = true;
      command.disabled = true;
      document.querySelector(`#device-tabs .device-tab[data-key="${key}"] .dot`)?.classList.add('offline');
      pending = '';
      clearTimeout(inputTimer);
      clearTimeout(pollTimer);
    }

    async function flushInput() {
      if (!alive || writing || !pending || term.options.disableStdin) return;
      writing = true;
      const data = pending.slice(0, 65536);
      pending = pending.slice(data.length);
      const result = await call('session_write', {sessionId, data});
      writing = false;
      if (!alive) return;
      if (!result.success) { fail(result.output || '输入失败，请关闭后重新连接'); return; }
      if (pending) inputTimer = setTimeout(flushInput, 0);
    }

    function input(data) {
      if (!alive || term.options.disableStdin) return;
      if (pending.length + data.length > 256 * 1024) { fail('输入队列已满，请关闭后重新连接'); return; }
      pending += data;
      activeUntil = Date.now() + 1000;
      clearTimeout(inputTimer);
      inputTimer = setTimeout(flushInput, 5);
      clearTimeout(pollTimer);
      if (!reading) pollTimer = setTimeout(poll, 0);
    }

    async function poll() {
      if (!alive || reading || term.options.disableStdin) return;
      reading = true;
      const result = await call('session_read', {sessionId, cursor, limit: 32768});
      reading = false;
      if (!alive) return;
      if (!result.success) { fail(result.output || '会话已失效，请关闭后重新连接'); return; }
      const output = result.data;
      if (output.droppedBytes) term.writeln(`\r\n[较早输出已裁剪 ${output.droppedBytes} 字节]\r\n`);
      if (output.data) {
        activeUntil = Date.now() + 1000;
        term.write(Uint8Array.from(atob(output.data), character => character.charCodeAt(0)));
      }
      cursor = output.nextCursor;
      if (output.state !== 'running' && !output.hasMore) { fail(output.error || 'PowerShell 已结束'); return; }
      pollTimer = setTimeout(poll, output.hasMore ? 0 : Date.now() < activeUntil ? 10 : 75);
    }

    async function sendResize() {
      if (!alive || resizing || !resizePending || term.options.disableStdin) return;
      const size = resizePending;
      resizePending = null;
      resizing = true;
      const result = await call('session_resize', {sessionId, ...size});
      resizing = false;
      if (!alive) return;
      if (!result.success) { fail(result.output || '调整终端失败'); return; }
      if (resizePending) resizeTimer = setTimeout(sendResize, 0);
    }

    function resize() {
      if (!alive || !host.clientWidth || !host.clientHeight) return;
      fit.fit();
      resizePending = {cols: term.cols, rows: term.rows};
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(sendResize, 100);
    }

    async function close() {
      if (!alive) return;
      alive = false;
      clearTimeout(inputTimer);
      clearTimeout(pollTimer);
      clearTimeout(resizeTimer);
      observer.disconnect();
      terminals.delete(context);
      term.dispose();
      terminalWindow.destroy();
      overlay.remove();
      delete state.terminals[key];
      if (state.activeDevice === key) state.activeDevice = terminalPanelKeys()[0] || null;
      updateDeviceTabs();
      applyLayout(state.layout);
      updateLayoutStatus();
      updateStatusBar();
      await call('session_close', {sessionId});
    }

    const observer = new ResizeObserver(resize);
    observer.observe(host);
    term.onData(input);
    term.onResize(({cols, rows}) => {
      resizePending = {cols, rows};
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(sendResize, 50);
    });
    overlay.onmousedown = () => selectActiveDevice(key);
    command.onkeydown = event => {
      if (event.key === 'Enter' && !event.ctrlKey && !event.shiftKey) {
        event.preventDefault();
        input(command.value.replace(/\r?\n/g, '\r') + '\r');
        command.value = '';
        term.focus();
      }
    };
    header.querySelector('.minimize').onclick = () => {
      terminalWindow.toggleMinimize();
    };
    header.querySelector('.maximize').onclick = () => {
      terminalWindow.toggleMaximize();
    };
    overlay.querySelector('.powershell-terminal-close').onclick = () => dismissTerminal(key);
    resize();
    poll();
  }

  el('window-powershell').onclick = async () => {
    el('window-menu').style.display = 'none';
    if (floating.visible) { floating.raise(); return; }
    floating.show();
    await refresh();
  };
  el('powershell-refresh').onclick = refresh;
  el('powershell-target').onchange = permission;
  el('powershell-select-close').onclick = closeSelect;
  selectOverlay.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.stopPropagation(); closeSelect(); }
  });
  el('powershell-allow-local').onchange = async () => {
    const checkbox = el('powershell-allow-local');
    checkbox.disabled = true;
    el('powershell-open').disabled = true;
    const result = await call('local_windows_permission', {allowed: checkbox.checked});
    checkbox.disabled = false;
    if (!result.success) {
      checkbox.checked = !checkbox.checked;
      el('powershell-permission-status').textContent = result.output || '权限保存失败';
      return;
    }
    checkbox.checked = result.data.allowed;
    // 清除旧配置快照，防止中转表单缓存覆盖独立保存的权限。
    if (state.relay.config) state.relay.config.allowWindows = result.data.allowed;
    await permission();
  };
  el('powershell-open').onclick = async () => {
    if (opening) return;
    const target = targets.find(item => item.target === el('powershell-target').value);
    if (!target) return;
    opening = true;
    el('powershell-open').disabled = true;
    el('powershell-permission-status').textContent = '正在打开终端…';
    const result = await call('session_open', {target: target.target, cols: 120, rows: 32});
    opening = false;
    if (!result.success) { el('powershell-permission-status').textContent = result.output || '打开失败'; return; }
    closeSelect();
    await openTerminal(target, result.data.sessionId);
  };
})();
