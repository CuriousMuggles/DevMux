// XMODEM 文件分块中转；协议包与ACK由串口执行端处理。
window.SerialXmodem = function(panel, request, getGeneration, snapshot, message, onBusy, saveFile, openWindow) {
  const area = document.createElement('div'); area.className = 'serial-xmodem';
  area.innerHTML = `<div class="cell-toolbar-row serial-options">
    <strong>XMODEM</strong><label>发送分包 <select class="xmodem-block tool-select"><option value="128">128字节</option><option value="1024">1K（CRC）</option></select></label>
    <label>接收校验 <select class="xmodem-checksum tool-select"><option value="crc">CRC16</option><option value="checksum">Checksum</option></select></label>
    <span class="xmodem-action-break" aria-hidden="true"></span>
    <button class="xmodem-receive tool-btn">接收文件</button><button class="xmodem-send tool-btn">发送文件…</button><input class="xmodem-file" type="file" hidden>
    <button class="xmodem-resume tool-btn" hidden>查询 / 恢复准备</button>
    <button class="xmodem-cancel tool-btn" disabled>取消传输</button><button class="xmodem-download tool-btn" disabled>保存接收文件</button>
    </div><div class="serial-transfer-note">先在设备端启动对应的接收/发送程序；8数据位，禁XON/XOFF。单文件≤64MiB。接收保留尾部填充。</div>
    <div class="serial-transfer-status" role="status">未开始传输</div><progress class="xmodem-progress" max="1" value="0" aria-label="XMODEM传输进度"></progress>`;
  area.hidden = true;
  (panel.querySelector('.serial-transfer-content') || panel.querySelector('.cell-toolbar')).appendChild(area);
  const toggle = panel.querySelector('.serial-xmodem-icon');
  toggle.onclick = () => { area.hidden = false; openWindow?.(); };
  const find = name => area.querySelector('.xmodem-' + name);
  const active = data => data && ['preparing', 'waiting', 'running', 'cancelling'].includes(data.state);
  let updateSignature = '', cancelledRequest = '';
  let current = null, pending = null, file = null, working = false, uncertain = false, disposed = false, capability = false, cancelRequested = false;
  const states = {preparing: '准备中', waiting: '等待握手', running: '传输中', cancelling: '正在取消',
    succeeded: '成功', failed: '失败', cancelled: '已取消'};
  function update() {
    if (disposed) return;
    const signature = JSON.stringify([current, working, uncertain, capability, cancelRequested]);
    if (signature === updateSignature) return; updateSignature = signature;
    const busy = working || uncertain || active(current);
    onBusy(!!busy);
    for (const name of ['send', 'receive', 'block', 'checksum']) find(name).disabled = busy || !capability;
    find('cancel').disabled = cancelRequested || !current || !active(current);
    find('download').disabled = working || uncertain || current?.direction !== 'receive' || current.state !== 'succeeded';
    find('resume').hidden = !uncertain;
    if (current) {
      const total = current.direction === 'send' ? current.size : 0;
      area.querySelector('.serial-transfer-status').textContent = `${current.direction === 'send' ? '发送' : '接收'} · ${states[current.state] || current.state} · ${current.phase} · ${current.transferredBytes}${total ? ' / ' + total : ''} 字节 · 重试 ${current.retries}${current.error ? ' · ' + current.error : ''}${uncertain ? ' · 请求结果待查询，不会自动重启' : ''}`;
      const progress = find('progress');
      if (total || !active(current)) { progress.max = total || 1; progress.value = total ? current.transferredBytes : current.state === 'succeeded' ? 1 : 0; }
      else progress.removeAttribute('value');
    }
  }
  async function call(action, args = {}) {
    const result = await request('xmodem-' + action, {transferId: current?.transferId, ...args});
    if (disposed) throw Error('串口窗口已关闭');
    if (!result.success) {
      if (result.operationUncertain || result.output === 'timeout') uncertain = true;
      throw Error(result.output || 'XMODEM请求失败');
    }
    if (result.data.generation) snapshot(result.data, true);
    return result.data;
  }
  const guard = operation => async () => {
    if (working || disposed) return;
    working = true; update();
    try { await operation(); }
    catch (error) { message(error.message); }
    finally { working = false; update(); }
  };
  async function prepare() {
    // 同一requestId查询启动结果；同offset上传可校验重试，run始终幂等。
    const result = await request('xmodem-start', pending);
    if (disposed) return;
    if (!result.success) {
      uncertain = !!result.operationUncertain || result.output === 'timeout';
      throw Error(result.output || '准备传输失败');
    }
    current = result.data.xmodem; snapshot(result.data, true); update();
    if (disposed) return;
    if (current.state !== 'preparing') { uncertain = false; return; }
    if (current.direction === 'send') {
      if (!file) throw Error('源文件不可用，请取消后重新选择');
      for (let offset = current.uploadedBytes; offset < file.size;) {
        if (cancelRequested || disposed || cancelledRequest === pending.requestId) return;
        const raw = new Uint8Array(await file.slice(offset, offset + 32768).arrayBuffer());
        let binary = ''; for (const byte of raw) binary += String.fromCharCode(byte);
        const data = await call('upload', {offset, data: btoa(binary)});
        if (cancelledRequest === pending.requestId) return;
        current = data.xmodem; offset = data.uploadedBytes; update();
      }
    }
    if (cancelRequested || disposed || cancelledRequest === pending.requestId) return;
    const data = await call('run'); current = data.xmodem; uncertain = false;
  }
  function begin(direction, selectedFile = null) {
    if (active(current) || uncertain) return;
    if (selectedFile && selectedFile.size > 67108864) { message('单文件不能超过64MiB'); return; }
    file = selectedFile; cancelRequested = false; cancelledRequest = '';
    pending = {generation: getGeneration(), requestId: crypto.randomUUID().replace(/-/g, ''), direction,
      size: file?.size || 0, blockSize: Number(find('block').value), checksum: find('checksum').value};
    current = null;
    return prepare();
  }
  function dockForFile() {
    if (panel.querySelector('.serial-settings-panel')) return false;
    if (!panel.classList.contains('native-detached')) return false;
    state.terminals[panel.dataset.key]?.windowManager?.dock();
    message('已返回主窗口；请再次点击选择或保存文件'); return true;
  }
  find('send').onclick = () => { if (!dockForFile()) find('file').click(); };
  find('file').onchange = guard(async () => { const selected = find('file').files[0]; find('file').value = ''; if (selected) await begin('send', selected); });
  find('receive').onclick = guard(() => begin('receive'));
  find('resume').onclick = guard(prepare);
  find('cancel').onclick = async () => {
    if (!current || cancelRequested) return;
    cancelRequested = true; cancelledRequest = pending?.requestId || current.requestId; update();
    try { const data = await call('cancel'); current = data.xmodem; uncertain = false; }
    catch (error) { message(error.message); }
    finally { cancelRequested = false; update(); }
  };
  find('download').onclick = guard(async () => {
    if (dockForFile()) return;
    const result = await saveFile(current.transferId);
    if (!result.success) throw Error(result.output || '保存失败；请查询目标文件，不自动重复保存');
    message(result.output);
  });
  return {
    snapshot(data) {
      capability = Object.prototype.hasOwnProperty.call(data, 'xmodem');
      if (data.xmodem) {
        // 所有接入看到独占状态，只有启动此传输的窗口可取消/下载。
        if (!current || current.transferId === data.xmodem.transferId || !active(current)) current = data.xmodem;
      }
      update();
      const own = pending && current?.requestId === pending.requestId;
      if (!own) { if (!find('cancel').disabled) find('cancel').disabled = true; if (!find('download').disabled) find('download').disabled = true; }
    },
    stop() { disposed = true; file = null; for (const control of area.querySelectorAll('button,input,select')) control.disabled = true; toggle.disabled = true; },
    dispose() { disposed = true; file = null; }
  };
};
