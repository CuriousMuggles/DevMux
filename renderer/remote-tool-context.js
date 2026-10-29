// 扩展工具共用目标栏：目标不会因离线自动切回本机，旧响应不会覆盖新目标。
window.ComputerTargets = {
  render(select, targets, previous = select.value) {
    select.replaceChildren();
    for (const target of targets) {
      const instance = targets.filter(item => item.label === target.label).length > 1 ? ` · 实例 ${(target.nodeId || target.peerId || 'local').slice(-6)}` : '';
      const label = `${target.local ? '本机' : '远程'} · ${target.label}${instance}（${target.role === 'master' ? '主控' : '从机'}）${target.online ? '' : ' · 离线'}`;
      const option = new Option(label, target.target);
      option.disabled = !target.online;
      select.appendChild(option);
    }
    if (previous && !targets.some(target => target.target === previous)) {
      select.appendChild(new Option('原目标已离线或解除连接', previous));
      select.lastElementChild.disabled = true;
    }
    if (previous) select.value = previous;
  }
};
window.RemoteToolContext = class {
  constructor(container, tool, onChange, name = '工具') {
    this.tool = tool; this.name = name; this.onChange = onChange; this.generation = 0;
    this.targets = []; this.target = 'computer:local'; this.ready = false; this.local = true; this.loading = false;
    container.classList.add('remote-tool-context');
    container.innerHTML = `<div class="remote-tool-target-row"><label>目标电脑 <select class="tool-select" aria-label="目标电脑"></select></label>
      <button class="tool-btn" type="button">刷新连接</button></div>
      <p class="remote-tool-status" role="status">正在读取目标…</p>
      <label class="remote-tool-permission"><input type="checkbox"> <span></span></label>`;
    this.select = container.querySelector('select'); this.refreshButton = container.querySelector('button');
    this.status = container.querySelector('p'); this.permission = container.querySelector('input');
    this.permissionRow = container.querySelector('.remote-tool-permission');
    this.permissionRow.querySelector('span').textContent = `允许已连接主控远程使用本机${name}`;
    this.select.addEventListener('change', () => this.choose());
    this.refreshButton.addEventListener('click', () => this.refresh());
    this.permission.addEventListener('change', async () => {
      this.permission.disabled = true;
      try {
        const result = await this.call('permission', {allowed: this.permission.checked});
        if (!result.stale && result.success) this.permission.checked = result.data.remoteAllowed;
        else if (!result.stale) { this.permission.checked = !this.permission.checked; this.status.textContent = result.output; }
      } finally { this.permission.disabled = false; }
    });
  }
  setBusy(busy) {
    busy = busy || this.loading;
    this.select.disabled = busy; this.refreshButton.disabled = busy;
    this.permission.disabled = busy;
  }
  async call(action, args = {}) {
    const generation = this.generation;
    const result = await callWS('extension-tool', {tool: this.tool, target: this.target, action, arguments: args}, 60000);
    if (generation !== this.generation) return {stale: true};
    if (result.targetUnavailable || result.output === 'timeout') { this.ready = false; this.status.textContent = result.output || '目标连接不可用'; }
    return result;
  }
  async refresh() {
    const generation = ++this.generation;
    this.ready = false; this.loading = true; this.setBusy(true); this.onChange(this);
    const result = await callWS('extension-tool', {tool: this.tool, action: 'targets'}, 20000);
    if (generation !== this.generation) return;
    this.targets = result.success ? result.data.targets : [];
    ComputerTargets.render(this.select, this.targets, this.target);
    await this.choose();
  }
  async choose() {
    const generation = ++this.generation;
    this.target = this.select.value || this.target;
    const selected = this.targets.find(item => item.target === this.target);
    this.local = selected?.local === true; this.ready = false; this.loading = true;
    this.permissionRow.hidden = !this.local;
    this.status.textContent = selected ? `操作执行于：${selected.label} · ${this.local ? '本机' : '远程从机'}` : '原目标不可用，请刷新连接';
    this.setBusy(true); this.onChange(this);
    if (selected?.online) {
      const result = await this.call('capabilities');
      if (generation !== this.generation) return;
      this.ready = result.success && result.data.allowed;
      if (result.success) this.permission.checked = result.data.remoteAllowed;
      if (!this.ready) this.status.textContent = result.output || '从机未允许该工具，请在从机本机面板开启远程权限';
    }
    if (generation !== this.generation) return;
    this.loading = false; this.setBusy(false); await this.onChange(this);
  }
};
