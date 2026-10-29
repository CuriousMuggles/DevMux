// 菜单显示偏好只影响入口，不改变执行权限或已有任务。
(() => {
  const key = 'extension-tool-menu-v1';
  const tools = [
    ['mijia', '米家设备', 'tools-mijia'],
    ['usb-relay', '继电器控制', 'tools-usb-relay'],
    ['relay', '跨机中转配置', 'tools-relay'],
    ['db-flash', 'DB 固化烧写', 'tools-db-flash'],
  ];
  let preferences = {};
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && typeof saved === 'object' && !Array.isArray(saved)) preferences = saved;
  } catch (_) { /* 存储损坏或不可用时按默认显示。 */ }
  const host = document.getElementById('extension-tool-options');
  const status = document.getElementById('extension-tool-save-status');
  const apply = () => {
    for (const [id, , menuId] of tools) {
      document.getElementById(menuId).style.display = preferences[id] === false ? 'none' : '';
    }
    const empty = tools.filter(([, , menuId]) => menuId.startsWith('tools-'))
      .every(([id]) => preferences[id] === false);
    document.getElementById('tools-tab').style.display = empty ? 'none' : '';
    if (empty) document.getElementById('tools-menu').style.display = 'none';
  };
  for (const [id, name] of tools) {
    const row = document.createElement('div');
    row.className = 'setting-row';
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.id = `extension-tool-${id}`;
    input.checked = preferences[id] !== false;
    label.append(input, document.createTextNode(` ${name}`));
    row.append(label);
    host.append(row);
    input.addEventListener('change', () => {
      preferences[id] = input.checked;
      apply();
      try {
        localStorage.setItem(key, JSON.stringify(preferences));
        status.textContent = '已保存，菜单立即生效';
      } catch (_) {
        status.textContent = '菜单已生效，但无法保存；刷新后可能恢复默认';
      }
    });
  }
  apply();
})();
