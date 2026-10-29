// 串口专有配置的独立桌面视图，仍由主页面持有控件与同一COM会话。
(() => {
  const windows = new Set();
  class SerialControlsWindow {
    constructor(key, panel, title) {
      Object.assign(this, {key: 'settings:' + key, panel, title, windowId: null, ready: false,
        opening: false, disposed: false, popup: null, popupName: 'serial-settings-' + key, counter: 1000000, revision: 0});
      windows.add(this);
      this.observer = new MutationObserver(() => this.schedule());
      this.observer.observe(panel, {subtree: true, attributes: true, childList: true, characterData: true});
      panel.addEventListener('input', () => this.schedule());
      panel.addEventListener('change', () => this.schedule());
    }
    schedule() { clearTimeout(this.timer); this.timer = setTimeout(() => this.snapshot(), 40); }
    async show() {
      if (this.disposed || this.opening) return;
      if (this.windowId) {
        sendWS({type: 'desktop-terminal-raise', windowId: this.windowId}); this.popup?.focus(); return;
      }
      this.opening = true;
      if (!window.pywebview) this.popup = window.open('about:blank', this.popupName, 'popup,width=520,height=600');
      try {
        const result = await callWS('desktop-terminal-open', {key: this.key, title: this.title,
          width: 520, height: 600, x: 180, y: 120});
        if (!result.success) throw Error(result.output || '无法打开串口设置');
        if (this.disposed) { sendWS({type: 'desktop-terminal-destroy', windowId: result.data.windowId}); return; }
        this.windowId = result.data.windowId;
        if (!result.data.native) {
          if (!this.popup) throw Error('请允许应用打开配置小窗');
          this.popup.location = result.data.url;
        } else { this.popup?.close(); this.popup = null; }
        sendWS({type: 'desktop-terminal-bind', windowId: this.windowId});
      } catch (error) { this.hide(); showToast('error', '串口设置', error.message); }
      finally { this.opening = false; }
    }
    snapshot() {
      if (!this.windowId || !this.ready) return;
      const controls = [...this.panel.querySelectorAll('*')];
      for (const e of controls) if (!e.dataset.desktopControl) e.dataset.desktopControl = String(++this.counter);
      const clone = this.panel.cloneNode(true); clone.hidden = false; clone.style.display = '';
      sendWS({type: 'desktop-terminal-publish', windowId: this.windowId, payload: {
        kind: 'snapshot', controlsOnly: true, initial: true, html: clone.outerHTML, controlRevision: this.revision,
        controls: Object.fromEntries(controls.map(e => [e.dataset.desktopControl,
          {value: e.value, checked: e.checked, disabled: e.disabled}]))}});
    }
    event(p) {
      if (p.kind !== 'control') return;
      const e = this.panel.querySelector('[data-desktop-control="' + Number(p.control) + '"]');
      if (!e || e.disabled) return;
      if (Number.isSafeInteger(p.controlRevision)) this.revision = Math.max(this.revision, p.controlRevision);
      if ('value' in p) e.value = p.value;
      if ('checked' in p) e.checked = p.checked;
      if (p.event === 'click') e.click();
      else if (['input', 'change'].includes(p.event)) e.dispatchEvent(new Event(p.event, {bubbles: true}));
      this.snapshot();
    }
    hide() {
      if (this.windowId) sendWS({type: 'desktop-terminal-destroy', windowId: this.windowId});
      this.windowId = null; this.ready = false;
      if (this.popup) { this.popup.__desktopClosing = true; this.popup.close(); this.popup = null; }
    }
    dispose() { this.disposed = true; this.hide(); clearTimeout(this.timer); this.observer.disconnect(); windows.delete(this); }
  }
  SerialControlsWindow.accept = msg => {
    const view = [...windows].find(w => w.windowId === msg.windowId);
    if (!view) return false;
    if (msg.type === 'desktop-terminal-ready' || msg.type === 'desktop-terminal-resync') { view.ready = true; view.snapshot(); }
    else if (msg.type === 'desktop-terminal-event') view.event(msg.payload);
    else if (msg.type === 'desktop-terminal-closed' || msg.type === 'desktop-terminal-dock') view.hide();
    return true;
  };
  window.SerialControlsWindow = SerialControlsWindow;
})();
