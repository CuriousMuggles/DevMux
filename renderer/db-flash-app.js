// 主应用适配：复用 callWS 和原生文件选择，独立本机烧写入口。
(() => {
  'use strict';
  const entry = document.getElementById('tools-db-flash');
  if (!entry || !window.DBFlashPanel) return;
  const toolsTab = document.getElementById('tools-tab');
  toolsTab.tabIndex = 0;
  toolsTab.setAttribute('role', 'button');
  toolsTab.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toolsTab.click(); }
  });
  const overlay = document.createElement('div');
  overlay.id = 'db-flash-overlay';
  overlay.className = 'db-flash-overlay';
  overlay.hidden = true;
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'false');
  overlay.setAttribute('aria-label', 'DB 固化烧写（本机）');
  const host = document.createElement('section');
  overlay.appendChild(host);
  document.body.appendChild(overlay);
  const panel = new window.DBFlashPanel(host, (type, payload) => callWS(type, payload, 125000));
  panel.remoteFiles = new DBRemoteFiles(panel);
  panel.context = new RemoteToolContext(panel.el('target-context'), 'db-flash', async context => {
    if (panel.displayTarget !== context.target) {
      panel.displayTarget = context.target;
      panel.files = []; panel.overrides = {}; panel.model = null;
      panel.jobId = ''; panel.requestId = ''; panel.sequence = 0;
      panel.directoryDirty = false;
      panel.el('directory').value = context.local ? panel.savedDir() : '';
      panel.el('log').textContent = '';
      panel.invalidatePlan(); panel.renderItems();
    }
    panel.el('browse').title = context.local ? '选择本机目录' : '浏览所选从机目录';
    panel.el('pick').textContent = context.local ? '批量选择待烧写文件…' : '选择从机待烧写文件…';
    panel.updateControls();
    if (!context.ready || panel.running) return;
    try {
      const config = await panel.call('config');
      if (config.toolDir && !panel.directoryDirty) panel.el('directory').value = config.toolDir;
      if (config.job?.state === 'running') {
        panel.jobId = config.job.jobId; panel.sequence = 0; panel.requestId = '';
        panel.running = true; panel.showPlan(config.job.plan); panel.schedulePoll();
      }
    } catch (error) { panel.message(error.message, true); }
    panel.updateControls();
  }, 'DB烧写');
  const floating = new window.ToolFloatingWindow(overlay, host, host.querySelector('.db-flash-header'),
    {width: 1000, height: 780, minWidth: 360, side: 'left'});
  host.hidden = true;
  const baseOpen = panel.open.bind(panel);
  const baseClose = panel.close.bind(panel);
  panel.open = () => {
    if (floating.visible) { floating.raise(); return; }
    floating.show();
    baseOpen();
    panel.context.refresh();
  };
  panel.close = () => {
    if (overlay.hidden) return;
    floating.hide();
    baseClose();
    toolsTab.focus();
  };
  function open() {
    document.getElementById('tools-menu').style.display = 'none';
    panel.open();
  }
  entry.addEventListener('click', open);
  entry.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
  });
  window.DBFlashIntegration = {
    panel, floating,
    acceptProgress(event) {
      if (panel.context.local) panel.acceptProgress(event);
    },
  };
  window.addEventListener('beforeunload', () => panel.dispose());
})();
