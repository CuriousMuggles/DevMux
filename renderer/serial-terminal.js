// 串口浮窗：原始字节共享，文本/HEX 显示，GUI 输入优先。
(() => {
  const layer = document.createElement('div');
  layer.id = 'serial-select-overlay'; layer.className = 'diagnostic-overlay'; layer.style.display = 'none';
  layer.setAttribute('role', 'dialog'); layer.setAttribute('aria-label', '串口配置');
  layer.innerHTML = `<div class="diagnostic-dialog relay-config-dialog serial-select-dialog">
    <div class="diagnostic-header"><h3>Serial · 串口配置</h3><button id="serial-select-close" class="diagnostic-close" title="关闭">✕</button></div>
    <div id="serial-context"></div>
    <div class="setting-row"><label for="serial-port">COM 端口</label><select id="serial-port" class="tool-select"></select><button id="serial-scan" class="tool-btn">刷新端口</button></div>
    <div class="setting-row"><label for="serial-baudrate">波特率</label><input id="serial-baudrate" aria-label="波特率，可从常用值中选择或直接输入" list="serial-baudrate-values" type="number" min="1" max="12000000" step="1" value="921600">
      <datalist id="serial-baudrate-values">${[300,600,1200,2400,4800,9600,14400,19200,38400,57600,115200,230400,460800,500000,576000,921600,1000000,1500000,2000000,3000000].map(value => `<option value="${value}">`).join('')}</datalist></div>
    <div class="setting-row"><label for="serial-bytesize">数据位</label><select id="serial-bytesize" class="tool-select"><option>8</option><option>7</option><option>6</option><option>5</option></select></div>
    <div class="setting-row"><label for="serial-parity">校验</label><select id="serial-parity" class="tool-select"><option value="N">无（N）</option><option value="E">偶（E）</option><option value="O">奇（O）</option><option value="M">Mark（M）</option><option value="S">Space（S）</option></select></div>
    <div class="setting-row"><label for="serial-stopbits">停止位</label><select id="serial-stopbits" class="tool-select"><option>1</option><option>1.5</option><option>2</option></select></div>
    <div class="setting-row"><label for="serial-flowcontrol">流控</label><select id="serial-flowcontrol" class="tool-select"><option value="none">无</option><option value="xonxoff">XON/XOFF</option><option value="rtscts">RTS/CTS</option><option value="dsrdtr">DSR/DTR</option></select></div>
    <p class="relay-root-note">选择电脑后枚举该电脑的 COM 口。GUI 与 Agent 共用连接，GUI 输入优先；关闭最后一个接入释放 COM。</p>
    <p id="serial-select-status" class="relay-root-note" role="status"></p>
    <div class="setting-row"><button id="serial-open" class="tool-btn primary" disabled>打开终端</button></div>
  </div>`;
  document.body.appendChild(layer);
  const el = id => document.getElementById(id);
  const floating = new ToolFloatingWindow(layer, layer.firstElementChild, layer.querySelector('.diagnostic-header'),
    {width: 700, height: 680, side: 'right'});
  let scanning = false, opening = false, recovery = null;
  const windows = new Map();
  const context = new RemoteToolContext(el('serial-context'), 'serial', async current => {
    el('serial-open').disabled = true;
    if (current.loading || !current.ready) { el('serial-port').replaceChildren(); return; }
    await scan();
  }, '串口');

  async function scan() {
    if (scanning || !context.ready) return;
    scanning = true; el('serial-open').disabled = true; el('serial-scan').disabled = true;
    const target = context.target;
    const result = await context.call('ports');
    scanning = false; el('serial-scan').disabled = false;
    if (result.stale || target !== context.target) {
      if (context.ready) await scan();
      return;
    }
    const previous = el('serial-port').value;
    el('serial-port').replaceChildren();
    if (result.success) {
      for (const port of result.data.ports) {
        el('serial-port').appendChild(new Option(`${port.port} · ${port.description}${port.opened ? ' · 已打开（可共用）' : ''}`, port.port));
      }
      if ([...el('serial-port').options].some(item => item.value === previous)) el('serial-port').value = previous;
      el('serial-select-status').textContent = result.data.ports.length ? `找到 ${result.data.ports.length} 个 COM 口` : '未发现 COM 口，请连接设备后刷新';
    } else el('serial-select-status').textContent = result.output || '枚举失败';
    el('serial-open').disabled = !el('serial-port').value || !context.ready || opening;
  }

  async function openTerminal(target, opened) {
    state.currentTab = 'terminal';
    $$('.tab-bar .tab').forEach(tab => tab.classList.toggle('active', tab.dataset.tab === 'terminal'));
    $$('.content-panel').forEach(panel => panel.classList.toggle('active', panel.id === 'panel-terminal'));
    const key = `serial:${target.target}:${opened.sessionId}`;
    const baudListId = 'serial-baud-rates-' + crypto.randomUUID();
    if (windows.has(key)) { windows.get(key).floating.raise(); return; }
    const terminalLayer = document.createElement('div'); terminalLayer.className = 'powershell-floating-layer';
    terminalLayer.setAttribute('role', 'dialog'); terminalLayer.setAttribute('aria-label', `Serial · ${opened.port}`);
    terminalLayer.innerHTML = `<div class="terminal-panel powershell-terminal-panel serial-terminal-panel">
      <div class="terminal-panel-header"><span class="header-title"></span><span class="header-badge"></span>
      <button class="header-btn minimize" title="最小化">−</button><button class="header-btn maximize" title="最大化">□</button><button class="header-btn close" title="关闭串口接入">×</button></div>
      <div class="terminal-cell"><div class="terminal-xterm serial-terminal-host"></div>
      <div class="cell-toolbar-handle" data-target="xterm" title="拖拽调整终端高度"></div>
      <div class="cell-toolbar"><div class="cell-toolbar-row serial-toolbar">
        <button class="file-op-btn serial-xmodem-icon serial-xmodem-toggle" title="XMODEM 文件传输" aria-label="XMODEM 文件传输">⇄</button>
        <button class="file-op-btn log serial-log" title="串口日志记录">📋</button>
        <button class="file-op-btn cmd serial-shortcuts" title="快捷指令">⚡</button>
        <button class="file-op-btn serial-control-toggle" type="button" data-state="takeover" title="接管输入权" aria-label="接管输入权"><span class="serial-control-icon" aria-hidden="true">⌨</span><span class="serial-control-label">接管</span></button>
        <button class="file-op-btn serial-settings-toggle" title="串口设置">⚙</button>
        </div>
        <div class="cell-toolbar-row"><span class="serial-status" role="status"></span></div>
        <button class="serial-send" hidden aria-hidden="true" tabindex="-1">发送命令</button>
        <div class="terminal-panel serial-settings-panel" hidden>
          <div class="terminal-panel-header"><span class="header-title">串口设置</span><button class="header-btn close" title="关闭设置">×</button></div>
          <div class="serial-settings-content">
            <p class="serial-connection-info"></p>
            <fieldset class="serial-connection-config"><legend>连接参数</legend><div class="serial-options">
              <label>波特率 <input class="serial-config-baudrate" type="number" list="${baudListId}" min="1" max="12000000" step="1"><datalist id="${baudListId}">${[1200,2400,4800,9600,19200,38400,57600,115200,230400,460800,921600,1000000,1500000,2000000,3000000].map(value => `<option value="${value}">`).join('')}</datalist></label>
              <label>数据位 <select class="serial-config-bytesize tool-select"><option>8</option><option>7</option><option>6</option><option>5</option></select></label>
              <label>校验位 <select class="serial-config-parity tool-select"><option value="N">无（N）</option><option value="E">偶（E）</option><option value="O">奇（O）</option><option value="M">Mark（M）</option><option value="S">Space（S）</option></select></label>
              <label>停止位 <select class="serial-config-stopbits tool-select"><option>1</option><option>1.5</option><option>2</option></select></label>
              <label>流控 <select class="serial-config-flowcontrol tool-select"><option value="none">无</option><option value="xonxoff">XON/XOFF</option><option value="rtscts">RTS/CTS</option><option value="dsrdtr">DSR/DTR</option></select></label>
              <div class="serial-config-actions"><button class="serial-config-apply tool-btn" title="将当前所选参数应用到当前COM连接，成功后立即生效，无需重新打开终端">应用串口参数</button><button class="serial-config-reset tool-btn">恢复当前值</button></div>
              <div class="serial-config-hint" role="status"></div><div class="serial-config-result" role="status"></div>
            </div></fieldset>
            <div class="serial-options">
              <label>显示/发送 <select class="serial-format tool-select"><option value="text">文本</option><option value="hex">HEX</option></select></label>
              <label>编码 <select class="serial-encoding tool-select"><option value="utf-8">UTF-8</option><option value="gbk">GBK</option><option value="ascii">ASCII</option><option value="latin-1">Latin-1</option></select></label>
              <label>换行 <select class="serial-newline tool-select"><option value="cr">CR</option><option value="lf">LF</option><option value="crlf">CRLF</option><option value="none">无</option></select></label>
              <label>字符间隔 <select class="serial-char-delay tool-select"><option value="0">0 ms</option><option value="5">5 ms</option><option value="10" selected>10 ms</option><option value="20">20 ms</option></select></label>
              <label><input class="serial-echo" type="checkbox"> 本地回显</label>
            </div><p class="serial-settings-note">“应用串口参数”会把当前选中的波特率、数据位、校验位、停止位和流控应用到已打开的COM连接，成功后立即生效，无需重新打开终端；显示/发送设置即时生效。修改连接参数可能改变设备通信方式，COM端口更换需关闭重开。记录日志期间格式与编码固定。</p>
          </div></div>
        <div class="terminal-panel serial-transfer-panel" hidden><div class="terminal-panel-header"><span class="header-title">XMODEM 文件传输</span><button class="header-btn close" title="关闭传输小窗">×</button></div><div class="serial-transfer-content"></div></div></div>
      <div class="cell-toolbar-handle" data-target="cmd-input" title="拖拽调整输入框高度"></div>
      <div class="terminal-cmd-input" style="height:34px"><textarea class="terminal-cmd-textarea" rows="1" spellcheck="false" placeholder="Enter 发送，Ctrl/Shift+Enter 换行，↑↓ 历史；HEX 输入字节对，例如 00 FF 41"></textarea></div></div></div>`;
    const panel = terminalLayer.firstElementChild, header = panel.querySelector('.terminal-panel-header');
    document.getElementById('terminal-container').appendChild(panel);
    panel.dataset.key = key;
    header.querySelector('.header-title').textContent = `Serial · ${opened.port} · ${target.label}`;
    header.querySelector('.header-badge').textContent = `${target.local ? '本机' : '远程从机'} · ${opened.config.baudrate}`;
    header.classList.toggle('remote', !target.local);
    let terminalWindow;
    const host = panel.querySelector('.serial-terminal-host');
    const {term, fitAddon: fit} = _initTerminal(key, target, host);
    const command = panel.querySelector('.terminal-cmd-textarea'), status = panel.querySelector('.serial-status');
    const format = panel.querySelector('.serial-format'), encoding = panel.querySelector('.serial-encoding');
    const newline = panel.querySelector('.serial-newline'), charDelay = panel.querySelector('.serial-char-delay');
    const echo = panel.querySelector('.serial-echo');
    const pacingSupported = target.local || opened.serialWritePacingProtocol === 1;
    charDelay.disabled = !pacingSupported;
    let alive = true, failed = false, connectionLost = false, reading = false, cursor = 0, generation = opened.generation, pollTimer;
    let decoder = new TextDecoder('utf-8'), chain = Promise.resolve(), queueBytes = 0;
    let lastState = opened;
    let pendingReceipt = false, inputEpoch = 0, transferBusy = false, transferUI = null, configBusy = false;
    let controlRevision = opened.controlRevision || 0, activeUntil = 0;
    const sessionId = opened.sessionId;
    const settingsPanel = panel.querySelector('.serial-settings-panel');
    const controlToggle = panel.querySelector('.serial-control-toggle');
    settingsPanel.querySelector('.serial-connection-info').textContent = `${opened.port} · ${target.label} · ${opened.config.baudrate} / ${opened.config.bytesize} / ${opened.config.parity} / ${opened.config.stopbits} · ${opened.config.flowcontrol}`;
    const settingsWindow = new SerialControlsWindow(key, settingsPanel, `串口设置 · ${opened.port} · ${target.label}`);
    panel.querySelector('.serial-settings-toggle').dataset.settingsPopup = settingsWindow.popupName;
    panel.querySelector('.serial-settings-toggle').onclick = () => settingsWindow.show();
    settingsPanel.querySelector('.close').onclick = () => settingsWindow.hide();
    const transferPanel = panel.querySelector('.serial-transfer-panel');
    const transferWindow = new SerialControlsWindow('xmodem:' + key, transferPanel, `XMODEM · ${opened.port} · ${target.label}`);
    panel.querySelector('.serial-xmodem-icon').dataset.settingsPopup = transferWindow.popupName;
    transferPanel.querySelector('.close').onclick = () => transferWindow.hide();
    const configInputs = Object.fromEntries(['baudrate','bytesize','parity','stopbits','flowcontrol'].map(name => [name, panel.querySelector('.serial-config-' + name)]));
    const configApply = panel.querySelector('.serial-config-apply');
    const configHint = panel.querySelector('.serial-config-hint'), configResult = panel.querySelector('.serial-config-result');
    let physicalSignature = '';
    function updateConnectionConfig(data, force = false) {
      if (!data.config) return;
      const signature = JSON.stringify(data.config);
      if (!force && signature === physicalSignature) return;
      physicalSignature = signature;
      for (const [name, input] of Object.entries(configInputs)) {
        if (name === 'dtr' || name === 'rts') input.checked = !!data.config[name];
        else input.value = data.config[name];
      }
      settingsPanel.querySelector('.serial-connection-info').textContent = `${data.port || opened.port} · ${target.label} · 当前 ${data.config.baudrate} / ${data.config.bytesize} / ${data.config.parity} / ${data.config.stopbits} · ${data.config.flowcontrol}`;
      header.querySelector('.header-badge').textContent = `${target.local ? '本机' : '远程从机'} · ${data.config.baudrate}`;
    }
    panel.querySelector('.serial-config-reset').onclick = () => updateConnectionConfig(lastState, true);
    const recording = SerialLogging(panel, key, target, sessionId, () => cursor);

    const request = (action, args = {}) => callWS('extension-tool',
      {tool: 'serial', target: target.target, action, arguments: {sessionId, ...args}}, 60000);
    function display(bytes) {
      if (format.value === 'hex') {
        for (let offset = 0; offset < bytes.length; offset += 16) {
          term.writeln(Array.from(bytes.slice(offset, offset + 16), b => b.toString(16).padStart(2, '0').toUpperCase()).join(' '));
        }
      } else term.write(decoder.decode(bytes, {stream: true}));
    }
    let statusSignature = '';
    function pauseInput(message) {
      if (!pendingReceipt) { inputEpoch++; queueBytes = 0; }
      pendingReceipt = true; status.textContent = message;
      term.options.disableStdin = true; command.disabled = true;
      for (const button of panel.querySelectorAll('.serial-control-toggle,.serial-send')) button.disabled = true;
    }
    function snapshot(data, force = false) {
      if ((data.controlRevision || 0) < controlRevision) return;
      controlRevision = data.controlRevision || 0;
      lastState = data; generation = data.generation; updateConnectionConfig(data);
      if (transferUI) transferUI.snapshot(data);
      if (data.inputUncertain) {
        pauseInput('发送结果待确认，继续接收输出；不会重发，请等待回执或关闭后重开'); return;
      }
      if (pendingReceipt) {
        if (data.serialStreamProtocol !== 1 || data.inputUncertain !== false) return;
        pendingReceipt = false; force = true;
        term.options.disableStdin = false; command.disabled = false;
        for (const button of panel.querySelectorAll('.serial-control-toggle,.serial-send')) button.disabled = false;
      }
      const signature = JSON.stringify([data.state, data.error, data.writable, data.controller, data.shared, controlRevision]);
      updateControlToggle(data);
      if (force || signature !== statusSignature) {
        statusSignature = signature;
        status.textContent = `${data.state === 'running' ? '已连接' : data.error || '已断开'} · ${data.writable ? 'GUI 持有输入权' : data.controller === 'agent' ? 'Agent 持有输入权，GUI 输入可接管' : '可申请输入权'}${data.shared ? ' · 共用连接' : ''}${!target.local && opened.serialStreamProtocol !== 1 ? ' · 旧版从机，升级后可使用快速收发' : ''}${!pacingSupported ? ' · 旧版从机不支持字符间隔' : ''}`;
      }
      applyInputState();
    }
    function updateControlToggle(data) {
      const ownsInput = !!data.writable;
      controlToggle.dataset.state = ownsInput ? 'release' : 'takeover';
      controlToggle.querySelector('.serial-control-label').textContent = ownsInput ? '交还' : '接管';
      const title = ownsInput ? '交还输入权' : '接管输入权';
      controlToggle.title = title;
      controlToggle.setAttribute('aria-label', title);
      controlToggle.classList.toggle('serial-control-owned', ownsInput);
      controlToggle.classList.toggle('serial-control-available', !ownsInput);
    }
    function applyInputState() {
      const blocked = failed || pendingReceipt || transferBusy || configBusy;
      term.options.disableStdin = blocked; command.disabled = blocked;
      for (const button of panel.querySelectorAll('.serial-send,.serial-shortcuts')) button.disabled = blocked;
      controlToggle.disabled = blocked;
      const capable = target.local || lastState.serialConfigProtocol === 1;
      configApply.disabled = blocked || !capable || !lastState.writable || !!lastState.shared;
      for (const input of Object.values(configInputs)) input.disabled = blocked || !capable;
      panel.querySelector('.serial-config-reset').disabled = configBusy;
      const hint = !capable ? '从机不支持在线修改参数，请升级从机' : lastState.shared ? 'COM仍有其他接入，关闭其他接入后可应用' : !lastState.writable ? '请先接管输入权再应用串口参数' : '参数将在当前COM连接上应用，不会自动重开';
      if (configHint.textContent !== hint) configHint.textContent = hint;

    }
    function fail(message) {
      failed = true; clearTimeout(pollTimer); status.textContent = message;
      transferUI?.stop(); recording.stop().catch(() => {}); applyInputState();
      term.options.disableStdin = true; command.disabled = true;
      for (const button of panel.querySelectorAll('.serial-control-toggle,.serial-send')) button.disabled = true;
    }
    const connectionChanged = event => {
      if (alive && !event.detail.connected) {
        connectionLost = true;
        fail('应用连接已断开，旧串口接入停止输入；请关闭后手动重新打开，不会重发');
      }
    };
    window.addEventListener('manager-connection-status', connectionChanged);
    function enqueue(operation, isInput = true) {
      // 输入与控制按序执行；旧端读取仍串行，新端按控制版本拒绝过期状态。
      const epoch = inputEpoch;
      chain = chain.then(async () => {
        if (alive && !failed && (!isInput || (!pendingReceipt && epoch === inputEpoch))) await operation();
      }).catch(error => {
        if (alive) fail(error.message || '连接失败');
      });
      return chain;
    }
    async function poll() {
      if (!alive || failed || reading) return;
      reading = true;
      const readOutput = async () => {
        const result = await request('read', {cursor, limit: 32768});
        if (!alive || failed) return;
        if (!result.success) { fail(result.output || '读取失败，请关闭后重新连接'); return; }
        const output = result.data;
        if (output.droppedBytes) {
          decoder = new TextDecoder(encoding.value);
          term.writeln(`\r\n[历史已裁剪 ${output.droppedBytes} 字节]`);
        }
        if (output.data) { activeUntil = Date.now() + 1000; display(Uint8Array.from(atob(output.data), ch => ch.charCodeAt(0))); }
        cursor = output.nextCursor; snapshot(output);
        if (output.recordingError) recording.error(output.recordingError);
        if ((output.state !== 'running' || output.closed) && !output.hasMore) {
          if (format.value === 'text') term.write(decoder.decode());
          fail(output.error || '串口接入已结束'); return;
        }
        pollTimer = setTimeout(poll, output.hasMore ? 0 : Date.now() < activeUntil ? 10 : 50);
      };
      try {
        if (target.local || opened.serialStreamProtocol === 1) await readOutput();
        else await enqueue(readOutput, false);
      } catch (error) { if (alive) fail(error.message || '串口读取失败'); }
      finally { reading = false; }
    }
    const lineEnding = () => ({cr: '\r', lf: '\n', crlf: '\r\n', none: ''})[newline.value];
    function input(data, mode, selectedEncoding = encoding.value) {
      if (!alive || failed || pendingReceipt || transferBusy || configBusy || !data) return;
      activeUntil = Date.now() + 1000;
      clearTimeout(pollTimer);
      if (!reading) pollTimer = setTimeout(poll, 0);
      if (queueBytes + data.length > 262144) { status.textContent = '输入队列已满，本次输入未发送'; return; }
      const interCharDelayMs = pacingSupported ? Number(charDelay.value) : 0;
      queueBytes += data.length;
      enqueue(async () => {
        queueBytes -= data.length;
        if (failed) return;
        const result = await request('write', {data, format: mode, encoding: selectedEncoding, generation, interCharDelayMs});
        if (!alive) return;
        if (!result.success) {
          status.textContent = result.operationUncertain ? '发送结果未确认，不会自动重发' : result.output || '输入未发送';
          if (result.operationUncertain) pauseInput('发送结果待确认，继续接收输出；不会重发，请等待回执或关闭后重开');
          return;
        }
        snapshot(result.data, true);
        if (echo.checked) {
          if (mode === 'hex') display(Uint8Array.from(data.replace(/\s/g, '').match(/../g) || [], pair => parseInt(pair, 16)));
          else term.write(data);
        }
      });
      return true;
    }
    const commandHistory = [];
    let historyIndex = -1;
    function sendCommand(value = command.value) {
      if (!alive || failed || pendingReceipt || transferBusy || configBusy || !value) return;
      let accepted;
      if (format.value === 'hex') {
        const hexValue = value.replace(/\s/g, '');
        if (!/^(?:[a-fA-F0-9]{2})+$/.test(hexValue) || hexValue.length > 131072) { status.textContent = 'HEX 请输入完整字节对，单次最多 65536 字节'; return; }
        accepted = input(value, 'hex');
      } else {
        // 多行命令与快捷指令统一换行，已有末尾换行不再追加。
        const logical = value.replace(/\r\n|\r/g, '\n');
        const text = logical.endsWith('\n') ? logical : logical + '\n';
        accepted = input(text.replace(/\n/g, lineEnding()), 'text');
      }
      if (!accepted) return;
      if (commandHistory[commandHistory.length - 1] !== value) {
        commandHistory.push(value);
        if (commandHistory.length > 50) commandHistory.shift();
      }
      historyIndex = -1;
      command.value = '';
    }
    const terminalInput = data => {
      if (format.value === 'hex') { command.focus(); return; }
      input(data.replace(/\r\n|\r|\n/g, lineEnding()), 'text');
    };
    term.onData(terminalInput);
    command.onkeydown = event => {
      if (command.disabled || command.readOnly || event.isComposing || event.keyCode === 229) return;
      if ((event.key === 'ArrowUp' || event.key === 'ArrowDown') && !(event.ctrlKey || event.shiftKey)) {
        event.preventDefault();
        if (event.key === 'ArrowUp') {
          if (!commandHistory.length) return;
          if (historyIndex < 0) historyIndex = commandHistory.length - 1;
          else if (historyIndex > 0) historyIndex--;
        } else if (historyIndex >= 0 && ++historyIndex >= commandHistory.length) historyIndex = -1;
        command.value = historyIndex < 0 ? '' : commandHistory[historyIndex];
        command.selectionStart = command.selectionEnd = command.value.length;
        return;
      }
      if (event.key !== 'Enter') return;
      event.preventDefault();
      if (event.ctrlKey || event.shiftKey) {
        const start = command.selectionStart, end = command.selectionEnd;
        command.setRangeText('\n', start, end, 'end');
        command.dispatchEvent(new Event('input', {bubbles: true}));
      } else sendCommand();
    };
    panel.querySelector('.serial-send').onclick = () => sendCommand();
    const disposeShortcuts = SerialShortcuts(panel, sendCommand, text => { status.textContent = text; });
    configApply.onclick = () => {
      if (configApply.disabled || configBusy) return;
      if (!configInputs.baudrate.checkValidity() || !configInputs.baudrate.value) { configResult.textContent = '波特率必须为1～12000000的整数'; return; }
      const settings = Object.fromEntries(Object.entries(configInputs).map(([name, input]) => [name,
        ['baudrate','bytesize','stopbits'].includes(name) ? Number(input.value) : input.value]));
      settings.dtr = !!lastState.config?.dtr; settings.rts = !!lastState.config?.rts;
      configBusy = true; configResult.textContent = '正在应用串口参数…'; applyInputState();
      enqueue(async () => {
        try {
          if (pendingReceipt || transferBusy) { configResult.textContent = '当前输入结果待确认或XMODEM正在运行，参数未修改'; return; }
          const result = await request('configure', {...settings, generation});
          if (!alive) return;
          if (result.success) { snapshot(result.data, true); configResult.textContent = '连接参数已应用'; }
          else {
            configResult.textContent = result.output || '连接参数修改失败';
            if (result.operationUncertain) pauseInput('连接参数结果待确认，不会自动重试；请等待状态或手动关闭重开');
          }
        } finally { configBusy = false; if (alive) applyInputState(); }
      }, false);
    };

    transferUI = SerialXmodem(panel, request, () => generation, snapshot,
      text => { status.textContent = text; }, busy => {
        if (busy !== transferBusy) { inputEpoch++; queueBytes = 0; }
        transferBusy = busy; applyInputState();
      }, transferId => callWS('serial-xmodem-save', {target: target.target, sessionId, transferId}, 600000), () => transferWindow.show());
    controlToggle.onclick = () => enqueue(async () => {
      const operation = lastState.writable ? 'release' : 'take';
      const result = await request('control', {operation, generation});
      if (!alive) return;
      if (result.success) snapshot(result.data, true);
      else if (result.operationUncertain) pauseInput('控制结果待确认，继续接收输出；不会重发');
      else status.textContent = result.output || '控制权切换失败，旧输入未发送';
    });
    format.onchange = encoding.onchange = () => { decoder = new TextDecoder(encoding.value); term.clear(); };
    function resize() { if (alive && host.clientWidth && host.clientHeight) fit.fit(); }
    const observer = new ResizeObserver(resize); observer.observe(host);
    async function close() {
      if (!alive) return;
      alive = false; clearTimeout(pollTimer); observer.disconnect();
      // 先发送幂等关闭，不能让界面销毁异常或旧输入的迟到回执阻止释放远程COM。
      // 已发出的请求由同一WS顺序处理，尚未执行的队列因alive=false被丢弃。
      const closing = ws && ws.readyState === WebSocket.OPEN ? request('close') : null;
      recording.dispose(); settingsWindow.dispose(); transferWindow.dispose();
      disposeShortcuts(); transferUI.dispose();
      window.removeEventListener('manager-connection-status', connectionChanged);
      try { term.dispose(); } catch (error) { console.warn('串口终端显示资源清理失败', error); }
      terminalWindow.destroy(); panel.remove(); windows.delete(key);
      delete state.terminals[key];
      if (state.activeDevice === key) state.activeDevice = terminalPanelKeys()[0] || null;
      updateDeviceTabs(); applyLayout(state.layout); updateLayoutStatus(); updateStatusBar();
      if (!closing) {
        showToast('warning', '串口', '应用连接未恢复；本机退出回收或执行端闲置回收会释放旧接入');
        return;
      }
      const result = await closing;
      if (!result.success && !connectionLost) showToast('warning', '串口', result.output || '关闭结果未确认；执行端闲置回收或本机关闭可释放 COM');
    }
    panel.onmousedown = () => selectActiveDevice(key);
    header.querySelector('.close').onclick = () => dismissTerminal(key);
    header.querySelector('.maximize').onclick = () => { terminalWindow.toggleMaximize(); resize(); };
    header.querySelector('.minimize').onclick = () => {
      terminalWindow.toggleMinimize(); resize();
    };
    state.terminals[key] = {key, computer: target, serialPort: opened.port, term, fitAddon: fit, panel,
      header, xtermDiv: host, cmdTextarea: command, close, floatingWindow: null, nativeInput: terminalInput};
    terminalWindow = TerminalWindows.attach(key, panel, header, {width: 900, height: 620});
    windows.set(key, {floating: terminalWindow, close, panel, term, sessionId, target, snapshot: () => lastState});
    const restoreIdentity = JSON.stringify(['serial', target.target, String(opened.port).toUpperCase()]);
    TerminalWindows.remember(key, async () => {
      const existing = [...windows.values()].some(item => item.target.target === target.target &&
        String(item.snapshot().port).toUpperCase() === String(opened.port).toUpperCase());
      if (existing) return true;
      // 与手动打开共用门闩，刷新和用户点击不能同时新建接入。
      if (opening) return false;
      opening = true;
      try {
        const reopened = await callWS('extension-tool', {tool: 'serial', target: target.target, action: 'open',
          arguments: {port: opened.port, ...opened.config, requestId: crypto.randomUUID().replace(/-/g, '')}}, 60000);
        if (!reopened.success) throw new Error(reopened.output || '串口目标不可用');
        await openTerminal(target, reopened.data); return true;
      } finally { opening = false; }
    }, restoreIdentity);
    updateDeviceTabs(); selectActiveDevice(key); applyLayout(state.layout); updateLayoutStatus(); updateStatusBar();
    snapshot(opened);
    resize(); poll(); term.focus();
  }
  el('window-serial').onclick = async () => { el('window-menu').style.display = 'none'; floating.show(); await context.refresh(); };
  el('serial-select-close').onclick = () => floating.hide();
  el('serial-scan').onclick = scan;
  el('serial-open').onclick = async () => {
    if (opening || !context.ready) return;
    const target = context.targets.find(item => item.target === context.target);
    if (!target) return;
    if (!el('serial-baudrate').checkValidity() || !el('serial-baudrate').value) {
      el('serial-select-status').textContent = '波特率必须为1～12000000的整数'; return;
    }
    const args = {port: el('serial-port').value, baudrate: Number(el('serial-baudrate').value),
      bytesize: Number(el('serial-bytesize').value), parity: el('serial-parity').value,
      stopbits: Number(el('serial-stopbits').value), flowcontrol: el('serial-flowcontrol').value,
      dtr: false, rts: false};
    const identity = JSON.stringify({target: target.target, ...args});
    if (!recovery || recovery.identity !== identity) recovery = {identity, requestId: crypto.randomUUID().replace(/-/g, '')};
    opening = true; context.setBusy(true); el('serial-open').disabled = true;
    const result = await context.call('open', {...args, requestId: recovery.requestId});
    opening = false; context.setBusy(false);
    if (result.stale) return;
    if (!result.success) {
      el('serial-select-status').textContent = result.operationUncertain ? `打开结果未确认；保持配置并再次点击，查询原请求 ${recovery.requestId}` : result.output || '打开失败';
      el('serial-open').disabled = !context.ready; return;
    }
    recovery = null; floating.hide();
    if (result.data.closed || result.data.state !== 'running') { el('serial-select-status').textContent = '原接入已结束，请重新打开'; floating.show(); return; }
    await openTerminal(target, result.data);
  };
  window.SerialIntegration = {context, windows, floating};
})();
