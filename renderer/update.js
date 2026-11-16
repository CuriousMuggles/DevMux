// ============================================================
// 主程序升级提示：检测结果 → 右下角卡片 → 下载 → 重启升级
// ============================================================
// 设计约束：
// - 启动时绝不弹模态窗；有新版只在右上角出现一张可关掉的卡片。
// - 检测失败保持安静：断网/代理环境的机器每次启动都会失败一次，报错会训练用户无视它。
// - 「跳过此版本」永久生效；「稍后」只关掉这一次，下次启动还提示（除非已下载，见下）。
(() => {
  const state = { report: null, busy: '', percent: 0 };
  let card = null;

  function elem(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function closeCard() {
    if (card) card.hidden = true;
  }

  function megabytes(size) {
    return typeof size === 'number' && size > 0 ? ' · ' + (size / 1048576).toFixed(1) + ' MB' : '';
  }

  function firstLine(text) {
    return String(text || '').split('\n').find(line => line.trim()) || '';
  }

  function build() {
    card = elem('div', 'app-update-card');
    card.hidden = true;
    card.innerHTML = `
      <div class="app-update-head">
        <span class="app-update-icon" aria-hidden="true">↑</span>
        <strong class="app-update-title"></strong>
        <button class="app-update-close" aria-label="关闭">✕</button>
      </div>
      <p class="app-update-body"></p>
      <div class="app-update-bar" hidden><span></span></div>
      <div class="app-update-actions"></div>`;
    document.body.append(card);
    card.querySelector('.app-update-close').onclick = closeCard;
  }

  function markMenu(active) {
    const item = document.getElementById('help-about');
    if (item) item.classList.toggle('has-update', !!active);
  }

  function show(title, body, actions, progress) {
    if (!card) build();
    card.querySelector('.app-update-title').textContent = title;
    card.querySelector('.app-update-body').textContent = body;
    const bar = card.querySelector('.app-update-bar');
    bar.hidden = progress === undefined;
    if (progress !== undefined) bar.firstElementChild.style.width = Math.max(2, progress) + '%';
    const row = card.querySelector('.app-update-actions');
    row.replaceChildren(...actions);
    card.hidden = false;
  }

  function button(label, className, handler) {
    const node = elem('button', 'app-update-btn ' + (className || ''), label);
    node.onclick = handler;
    return node;
  }

  async function call(action, payload = {}, timeoutMs = 120000) {
    const result = await window.callWS('app-update', { action, ...payload }, timeoutMs);
    if (!result || result.success === false) {
      throw new Error((result && result.output) || '升级服务无响应');
    }
    return result.data || {};
  }

  async function refresh() {
    try {
      render(await call('status', {}, 20000));
    } catch (error) {
      // 状态读不到（后端未就绪/重连中）时保持安静，等下次连接事件。
      console.warn('[UPDATE] status failed:', error.message);
    }
  }

  async function check() {
    state.busy = 'check';
    render(state.report);
    try {
      const data = await call('check', {}, 30000);
      state.busy = '';
      render(data);
      if (!data.available && !data.error) {
        window.showToast?.('success', '已是最新版本', '当前 v' + (data.current || ''));
      }
      return data;
    } catch (error) {
      state.busy = '';
      render(state.report);
      window.showToast?.('error', '检查更新失败', error.message);
      return null;
    }
  }

  async function download() {
    state.busy = 'download';
    state.percent = 0;
    render(state.report);
    try {
      const pending = await call('download', {}, 15 * 60 * 1000);
      state.busy = '';
      await refresh();
      window.showToast?.('success', '新版本已下载', 'v' + pending.version + ' 已就绪，重启后完成安装');
    } catch (error) {
      state.busy = '';
      render(state.report);
      window.showToast?.('error', '下载失败', error.message);
    }
  }

  async function restart() {
    state.busy = 'restart';
    render(state.report);
    try {
      await call('restart', {}, 20000);
      show('正在退出…', '窗口关闭后会自动安装新版本并重新打开。', []);
    } catch (error) {
      state.busy = '';
      render(state.report);
      window.showToast?.('error', '无法开始升级', error.message);
    }
  }

  async function skip(version) {
    try {
      await call('skip', { version }, 15000);
      closeCard();
      markMenu(!!(state.report && state.report.pending));
      await refresh();
    } catch (error) {
      window.showToast?.('error', '操作失败', error.message);
    }
  }

  function render(report) {
    if (!report) return;
    state.report = report;
    const pending = report.pending;
    markMenu(!!(report.available || pending));

    if (state.busy === 'restart') {
      show('正在退出…', '窗口关闭后会自动安装新版本并重新打开。', []);
      return;
    }
    if (state.busy === 'download') {
      show('正在下载 v' + report.latest, '下载完成后会先校验再解压，中断即丢弃。', [], state.percent);
      return;
    }
    if (pending) {
      show('新版本 v' + pending.version + ' 已下载完成',
        '重启后自动安装，约需几秒；当前会话会中断。',
        [button('立即重启升级', 'primary', restart), button('稍后', '', closeCard)]);
      return;
    }
    if (report.available) {
      const notes = firstLine(report.notes);
      const body = 'v' + report.current + ' → v' + report.latest + megabytes(report.size)
        + (report.required ? ' · 该版本为必需升级' : '') + (notes ? '\n' + notes : '');
      const actions = [button('下载并升级', 'primary', download)];
      if (report.notes) actions.push(button('更新说明', '', () => window.AppUpdate?.showNotes()));
      actions.push(button('跳过此版本', '', () => skip(report.latest)));
      show('发现新版本 v' + report.latest, body, actions);
      return;
    }
    if (state.busy === 'check') {
      show('正在检查更新…', '正在读取更新清单。', []);
      return;
    }
    closeCard();
  }

  function showNotes() {
    const report = state.report || {};
    if (!report.notes) return;
    const overlay = elem('div', 'cmd-edit-overlay');
    overlay.innerHTML = `
      <div class="about-dialog app-update-notes">
        <h3>DevMux v${report.latest} 更新说明</h3>
        <pre class="app-update-notestext"></pre>
        <div class="about-btns"><button class="about-close primary">关闭</button></div>
      </div>`;
    overlay.querySelector('.app-update-notestext').textContent = report.notes;
    document.body.append(overlay);
    overlay.querySelector('.about-close').onclick = () => overlay.remove();
    overlay.addEventListener('click', event => { if (event.target === overlay) overlay.remove(); });
  }

  function accept(message) {
    const type = String(message.type || '');
    if (type === 'app-update-progress') {
      state.percent = Number(message.percent) || 0;
      if (state.busy === 'download') render(state.report);
      return true;
    }
    if (type === 'app-update-available') {
      render(message.data);
      return true;
    }
    return false;
  }

  function mountAbout(row) {
    if (!row || row.dataset.ready) return;
    row.dataset.ready = '1';
    row.innerHTML = '<span class="about-key">版本更新</span><span class="about-update-wrap">'
      + '<span class="about-update-text">点击检查更新</span>'
      + '<button class="about-update-check">检查更新</button></span>';
    const text = row.querySelector('.about-update-text');
    row.querySelector('.about-update-check').onclick = async () => {
      text.textContent = '检查中…';
      const data = await check();
      if (!data) {
        text.textContent = '检查失败，可稍后重试';
      } else if (data.available) {
        text.textContent = 'v' + data.latest + ' 可用';
        open();
      } else {
        text.textContent = '已是最新版本';
      }
    };
  }

  function open() {
    if (state.report) render(state.report); else refresh();
    if (card) card.hidden = false;
  }

  window.AppUpdate = { refresh, check, open, accept, mountAbout, showNotes, render };

  window.addEventListener('manager-connection-status', event => {
    if (event.detail && event.detail.connected) refresh();
  });
  const boot = () => setTimeout(async () => {
    await refresh();
    const report = state.report;
    if (report && report.result && !report.result.acknowledged) {
      const ok = report.result.status === 'installed';
      window.showToast?.(ok ? 'success' : 'error', ok ? '已完成升级' : '升级未完成',
        ok ? 'DevMux 已更新到新版本' : (report.result.error || '新版未能安装，已恢复上一版本'));
      call('ack-result', {}, 15000).catch(() => {});
    }
  }, 1200);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
