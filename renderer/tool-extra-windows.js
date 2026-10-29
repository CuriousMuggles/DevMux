// 其余工具主窗口统一浮动；确认/权限选择仍由各自流程管理。
(() => {
  window.ToolPanels = {};
  for (const [name, id, width, height] of [
    ['relayConfig', 'relay-config-overlay', 840, 740],
    ['diagnostic', 'diag-overlay', 820, 680],
  ]) {
    const layer = document.getElementById(id);
    const dialog = layer?.firstElementChild;
    if (!dialog) continue;
    const floating = new ToolFloatingWindow(layer, dialog, dialog.querySelector('.diagnostic-header'),
      {width, height, side: 'left'});
    window.ToolPanels[name] = floating;
  }
})();
