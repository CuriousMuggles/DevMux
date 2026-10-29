// 从机只读文件列表：传递从机路径，不传送或复制镜像内容。
window.DBRemoteFiles = class {
  constructor(panel) {
    this.panel = panel;
    this.layer = document.createElement('div');
    this.layer.hidden = true;
    this.layer.setAttribute('role', 'dialog');
    this.layer.setAttribute('aria-label', '选择从机文件');
    const dialog = document.createElement('section');
    dialog.className = 'db-flash-panel';
    dialog.innerHTML = `<header class="db-flash-header"><h2>选择从机文件</h2><button data-picker="close">关闭</button></header>
      <p>读取所选从机的目录。镜像保留在从机，不复制到主控。</p>
      <div class="db-flash-path-row"><input data-picker="path" type="text" aria-label="从机目录路径">
        <button data-picker="load">打开目录</button><button data-picker="up">上一级</button></div>
      <p data-picker="message" role="status"></p><div data-picker="files" class="db-remote-file-list"></div>
      <div class="db-flash-actions"><button data-picker="accept">使用所选文件</button></div>`;
    this.layer.appendChild(dialog);
    document.body.appendChild(this.layer);
    this.el = name => dialog.querySelector('[data-picker="' + name + '"]');
    this.floating = new ToolFloatingWindow(this.layer, dialog, dialog.querySelector('header'),
      {width: 700, height: 600, side: 'right'});
    this.selected = new Set();
    this.el('close').onclick = () => this.finish(null);
    this.el('accept').onclick = () => this.finish(this.kind === 'directory'
      ? {success: true, path: this.current.path} : {success: true, paths: [...this.selected]});
    this.el('load').onclick = () => this.load();
    this.el('path').onkeydown = event => { if (event.key === 'Enter') this.load(); };
    this.el('up').onclick = () => { this.el('path').value = this.current.parent; this.load(); };
    this.layer.onkeydown = event => {
      if (event.key === 'Escape') { event.stopPropagation(); this.finish(null); }
    };
  }
  finish(result) {
    this.generation = (this.generation || 0) + 1;
    this.loading = false;
    this.floating.hide();
    this.resolve?.(result || {success: false});
    this.resolve = null;
  }
  open(kind) {
    this.generation = (this.generation || 0) + 1;
    this.kind = kind;
    this.current = null;
    this.selected.clear();
    this.el('accept').textContent = kind === 'directory' ? '选择此目录' : '使用所选文件';
    this.el('path').value = this.panel.el('directory').value || '';
    this.el('files').replaceChildren();
    this.el('accept').disabled = true;
    this.floating.show();
    this.el('path').focus();
    this.load();
    return new Promise(resolve => { this.resolve = resolve; });
  }
  async load() {
    if (this.loading) return;
    this.loading = true;
    const generation = this.generation;
    this.el('load').disabled = this.el('up').disabled = this.el('accept').disabled = true;
    this.el('message').textContent = '正在读取从机目录…';
    try {
      const model = await this.panel.call('browse', {path: this.el('path').value});
      if (!this.resolve || generation !== this.generation) return;
      this.current = model;
      this.el('path').value = model.path;
      this.el('files').replaceChildren();
      for (const item of model.entries) {
        const row = document.createElement('label');
        row.className = 'db-remote-file-row';
        if (item.isDir) {
          const button = document.createElement('button');
          button.textContent = '📁 ' + item.name;
          button.onclick = () => { this.el('path').value = item.path; this.load(); };
          row.appendChild(button);
        } else if (this.kind !== 'directory') {
          const check = document.createElement('input');
          check.type = 'checkbox'; check.checked = this.selected.has(item.path);
          check.onchange = () => {
            if (check.checked) this.selected.add(item.path); else this.selected.delete(item.path);
            this.update();
          };
          const label = document.createElement('span');
          label.textContent = item.name + ' · ' + (item.size / 1048576).toFixed(2) + ' MiB';
          row.append(check, label);
        } else continue;
        this.el('files').appendChild(row);
      }
      this.update();
    } catch (error) {
      this.el('message').textContent = error.message;
    } finally {
      if (generation !== this.generation) return;
      this.loading = false;
      this.el('load').disabled = false;
      this.el('up').disabled = !this.current;
    }
  }
  update() {
    this.el('accept').disabled = !this.current || (this.kind !== 'directory' && !this.selected.size);
    this.el('message').textContent = (this.kind === 'directory' ? '选择当前目录或继续浏览' : '已勾选 ' + this.selected.size + ' 个从机文件')
      + (this.current?.truncated ? '；最多显示 1000 项，请选择更具体的目录' : '');
  }
};
