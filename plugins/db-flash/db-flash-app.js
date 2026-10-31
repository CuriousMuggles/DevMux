// 使用公开 SDK 的页面、传输和目标上下文；业务计划与轮询由面板管理。
(() => {
  'use strict';
  const host=document.createElement('section');
  const panel = new window.DBFlashPanel(host, (type, payload) => DevMuxPlugin.request(type==='db-flash'?'plugin':type, payload, 125000));
  panel.remoteFiles = new DBRemoteFiles(panel);
  panel.context = new DevMuxPlugin.RemoteContext(panel.el('target-context'), 'db-flash', async context => {
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
    const target = context.target;
    try {
      const config = await panel.call('config');
      if (context.target !== target) return;
      if (config.toolDir && !panel.directoryDirty) panel.el('directory').value = config.toolDir;
      if (config.job?.state === 'running') {
        panel.jobId = config.job.jobId; panel.sequence = 0; panel.requestId = '';
        panel.running = true; panel.showPlan(config.job.plan); panel.schedulePoll();
      }
    } catch (error) { panel.message(error.message, true); }
    panel.updateControls();
  }, 'DB烧写');
  host.querySelector('.db-flash-header').hidden=true;
  const page=DevMuxPlugin.ui.mount({title:'DB 固化烧写',description:'核对镜像与执行计划，查看实时烧写输出',content:host});
  panel.close=()=>DevMuxPlugin.close();
  window.DBFlashIntegration={panel};
  DevMuxPlugin.onDispose(()=>panel.dispose());
  panel.open();
  panel.context.refresh();
})();
