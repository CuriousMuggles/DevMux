// 串口快捷指令：独立指令集，沿用串口的格式、编码与换行。
window.SerialShortcuts = function(panel, send, message) {
  const area = document.createElement('div'); area.className = 'command-popup serial-command-popup';
  area.innerHTML = `<div class="cmd-popup-header"><span>串口快捷指令</span>
    <select class="cmd-group-select" aria-label="串口指令集"></select>
    <button class="tool-btn group-add">新建指令集</button><button class="tool-btn group-rename">改名</button>
    <button class="tool-btn group-delete">删除指令集</button><button class="cmd-popup-add" title="新建指令">+</button></div>
    <div class="cmd-popup-body"></div><div class="serial-shortcut-note">点击发送；右键编辑或删除。按当前文本/HEX、编码及换行设置发送。</div>`;
  panel.querySelector('.cell-toolbar').appendChild(area);
  const toggle = panel.querySelector('.serial-shortcuts');
  let groups = [], index = 0, disposed = false, dialog = null;
  const select = area.querySelector('select');
  const guard = fn => async () => { try { await fn(); } catch (error) { message(error.message); } };
  async function load() {
    const result = await callWS('get-serial-commands');
    if (!result.success) throw Error(result.output || '读取串口指令失败');
    groups = result.data; index = Math.min(index, groups.length - 1);
  }
  async function save(next) {
    const result = await callWS('save-serial-commands', {commands: next});
    if (!result.success) throw Error(result.output || '保存串口指令失败');
    groups = next; index = Math.min(index, groups.length - 1); render();
  }
  function edit(title, fields, accept, remove) {
    if (dialog) dialog.remove();
    dialog = document.createElement('div'); dialog.className = 'cmd-edit-overlay serial-shortcut-dialog';
    const box = document.createElement('div'); box.className = 'cmd-edit-dialog';
    const heading = document.createElement('h3'); heading.textContent = title; box.appendChild(heading);
    const inputs = fields.map(([label, value, multiline]) => {
      const text = document.createElement('label'); text.textContent = label;
      const input = document.createElement(multiline ? 'textarea' : 'input'); input.value = value;
      input.className = multiline ? 'serial-shortcut-text' : 'cmd-edit-name'; input.spellcheck = false;
      box.append(text, input); return input;
    });
    const buttons = document.createElement('div'); buttons.className = 'cmd-edit-btns';
    const close = () => { dialog?.remove(); dialog = null; };
    for (const [label, action] of [['取消', close], ...(remove ? [['删除', async () => { await remove(); close(); }]] : []),
      ['保存', async () => { await accept(inputs.map(input => input.value)); close(); }]]) {
      const button = document.createElement('button'); button.textContent = label;
      button.className = label === '保存' ? 'serial-shortcut-save' : label === '删除' ? 'serial-shortcut-delete' : 'serial-shortcut-cancel';
      button.onclick = guard(action); buttons.appendChild(button);
    }
    box.appendChild(buttons); dialog.appendChild(box); dialog.onclick = event => { if (event.target === dialog) close(); };
    panel.appendChild(dialog); inputs[0].focus();
  }
  const copy = () => JSON.parse(JSON.stringify(groups));
  function editCommand(position = -1) {
    const groupIndex = index, item = groups[groupIndex].commands[position] || {name: '', cmd: ''};
    edit(position < 0 ? '新建串口指令' : '编辑串口指令', [['名称', item.name], ['指令（按当前格式发送）', item.cmd, true]],
      async ([name, cmd]) => {
        if (!name.trim() || !cmd.length) throw Error('名称和指令不能为空');
        const next = copy(); const entry = {name: name.trim(), cmd};
        if (position < 0) next[groupIndex].commands.push(entry); else next[groupIndex].commands[position] = entry;
        await save(next);
      }, position < 0 ? null : async () => { const next = copy(); next[groupIndex].commands.splice(position, 1); await save(next); });
  }
  function render() {
    if (disposed) return;
    select.replaceChildren(...groups.map((group, i) => new Option(group.name, i))); select.value = index;
    const body = area.querySelector('.cmd-popup-body'); body.replaceChildren();
    if (!groups[index].commands.length) body.textContent = '当前指令集为空，点击 + 添加';
    groups[index].commands.forEach((item, i) => {
      const button = document.createElement('button'); button.className = 'cmd-popup-btn';
      for (const [className, value] of [['cmd-btn-name', item.name], ['cmd-btn-cmd', item.cmd]]) {
        const span = document.createElement('span'); span.className = className; span.textContent = value; button.appendChild(span);
      }
      button.title = item.cmd; button.onclick = () => send(item.cmd);
      button.oncontextmenu = event => { event.preventDefault(); editCommand(i); }; body.appendChild(button);
    });
  }
  toggle.onclick = guard(async () => {
    if (area.style.display === 'block') { area.style.display = 'none'; return; }
    const log = panel.querySelector('.log-submenu'); if (log) log.style.display = 'none';
    await load(); render(); area.style.display = 'block';
  });
  select.onchange = () => { index = Number(select.value); render(); };
  area.querySelector('.cmd-popup-add').onclick = () => editCommand();
  area.querySelector('.group-add').onclick = () => edit('新建串口指令集', [['名称', '']], async ([name]) => {
    const next = copy(); next.push({name: name.trim(), commands: []}); await save(next); index = groups.length - 1; render();
  });
  area.querySelector('.group-rename').onclick = () => edit('指令集改名', [['名称', groups[index].name]], async ([name]) => {
    const next = copy(); next[index].name = name.trim(); await save(next);
  });
  area.querySelector('.group-delete').onclick = () => edit('删除指令集及全部指令', [['确认指令集名称', '']], async ([name]) => {
    if (groups.length === 1) throw Error('至少保留一个指令集');
    if (name !== groups[index].name) throw Error('请输入当前指令集名称确认删除');
    const next = copy(); next.splice(index, 1); await save(next);
  });
  return () => { disposed = true; dialog?.remove(); };
};
