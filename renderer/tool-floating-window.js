// 扩展工具共用浮动窗口：只管理位置、尺寸和层级，不改变工具作业。
(() => {
  'use strict';
  const windows = [];
  class ToolFloatingWindow {
    constructor(layer, dialog, header, options = {}) {
      this.layer = layer;
      this.dialog = dialog;
      this.header = header;
      this.options = {width: 640, height: 680, minWidth: 320, minHeight: 240, ...options};
      this.bounds = null;
      this.gesture = null;
      layer.classList.add('tool-floating-layer');
      layer.setAttribute('aria-modal', 'false');
      dialog.classList.add('tool-floating-window');
      header.classList.add('tool-floating-header');
      header.tabIndex = 0;
      header.title = '拖动标题栏移动；拖动边缘调整大小。标题栏方向键移动，Ctrl + 方向键调整大小。';
      const content = document.createElement('div');
      content.className = 'tool-floating-content';
      for (const child of [...dialog.childNodes]) {
        if (child !== header) content.appendChild(child);
      }
      dialog.appendChild(content);
      for (const edge of ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']) {
        const handle = document.createElement('div');
        handle.className = 'tool-window-resize tool-window-resize-' + edge;
        handle.dataset.edge = edge;
        if (edge === 'se') {
          handle.tabIndex = 0;
          handle.setAttribute('role', 'button');
          handle.setAttribute('aria-label', '调整窗口大小（方向键）');
          handle.addEventListener('keydown', event => this.keyboard(event, true));
        }
        handle.addEventListener('pointerdown', event => this.begin(event, edge));
        dialog.appendChild(handle);
      }
      this.onRaise = () => this.raise();
      layer.addEventListener('pointerdown', this.onRaise, true);
      layer.addEventListener('focusin', this.onRaise);
      this.onHeaderPointer = event => {
        if (!event.target.closest('button,input,select,a')) this.begin(event, 'move');
      };
      header.addEventListener('pointerdown', this.onHeaderPointer);
      this.onHeaderKey = event => {
        if (event.target === header) this.keyboard(event, event.ctrlKey);
      };
      header.addEventListener('keydown', this.onHeaderKey);
      this.move = event => this.updateGesture(event);
      this.end = event => {
        if (event && this.gesture && event.pointerId !== this.gesture.id) return;
        const gesture = this.gesture;
        this.gesture = null;
        if (gesture?.target.hasPointerCapture?.(gesture.id)) gesture.target.releasePointerCapture(gesture.id);
        document.removeEventListener('pointermove', this.move);
        document.removeEventListener('pointerup', this.end);
        document.removeEventListener('pointercancel', this.end);
        dialog.classList.remove('tool-window-moving');
      };
      this.onViewportResize = () => {
        if (this.visible) this.apply(this.bounds);
      };
      window.addEventListener('resize', this.onViewportResize);
      windows.push(this);
    }
    get visible() { return !this.layer.hidden && this.layer.style.display !== 'none'; }
    raise() {
      const index = windows.indexOf(this);
      if (index >= 0) windows.splice(index, 1);
      windows.push(this);
      // 保持确认弹窗和主菜单在工具窗口之上，多个工具之间按最近操作排序。
      windows.forEach((item, order) => { item.layer.style.zIndex = String(3000 + order); });
    }
    apply(bounds) {
      const margin = 8;
      const widthAvailable = Math.max(1, window.innerWidth - margin * 2);
      const top = Math.min(76, Math.max(margin, window.innerHeight - 120));
      const heightAvailable = Math.max(1, window.innerHeight - top - margin);
      const width = Math.min(widthAvailable, Math.max(Math.min(this.options.minWidth, widthAvailable), bounds.width));
      const height = Math.min(heightAvailable, Math.max(Math.min(this.options.minHeight, heightAvailable), bounds.height));
      const x = Math.max(margin, Math.min(bounds.x, window.innerWidth - width - margin));
      const y = Math.max(top, Math.min(bounds.y, window.innerHeight - height - margin));
      this.bounds = {x, y, width, height};
      Object.assign(this.dialog.style, {left: x + 'px', top: y + 'px', width: width + 'px', height: height + 'px'});
    }
    show() {
      this.layer.hidden = false;
      this.layer.style.display = 'block';
      if (!this.bounds) {
        const width = Math.min(this.options.width, window.innerWidth - 32);
        this.bounds = {x: this.options.side === 'right' ? window.innerWidth - width - 16 : 16,
          y: 88, width, height: this.options.height};
      }
      this.apply(this.bounds);
      this.raise();
    }
    hide() {
      this.end();
      this.layer.hidden = true;
      this.layer.style.display = 'none';
    }
    destroy() {
      this.hide();
      this.layer.removeEventListener('pointerdown', this.onRaise, true);
      this.layer.removeEventListener('focusin', this.onRaise);
      this.header.removeEventListener('pointerdown', this.onHeaderPointer);
      this.header.removeEventListener('keydown', this.onHeaderKey);
      window.removeEventListener('resize', this.onViewportResize);
      const index = windows.indexOf(this);
      if (index >= 0) windows.splice(index, 1);
    }
    toggleMaximize() {
      if (this.previousBounds) {
        this.apply(this.previousBounds);
        this.previousBounds = null;
      } else {
        this.previousBounds = {...this.bounds};
        this.apply({x: 8, y: 76, width: window.innerWidth - 16, height: window.innerHeight - 84});
      }
    }
    begin(event, edge) {
      if (event.button !== 0 || this.gesture) return;
      event.preventDefault();
      this.raise();
      this.gesture = {id: event.pointerId, target: event.currentTarget, edge,
        x: event.clientX, y: event.clientY, bounds: {...this.bounds}};
      try { event.currentTarget.setPointerCapture(event.pointerId); } catch (_) {}
      this.dialog.classList.add('tool-window-moving');
      document.addEventListener('pointermove', this.move);
      document.addEventListener('pointerup', this.end);
      document.addEventListener('pointercancel', this.end);
    }
    updateGesture(event) {
      const gesture = this.gesture;
      if (!gesture || gesture.id !== event.pointerId) return;
      const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
      const bounds = {...gesture.bounds};
      const edge = gesture.edge;
      if (edge === 'move') { bounds.x += dx; bounds.y += dy; }
      else {
        if (edge.includes('e')) bounds.width += dx;
        if (edge.includes('s')) bounds.height += dy;
        if (edge.includes('w')) {
          bounds.width = Math.max(Math.min(this.options.minWidth, window.innerWidth - 16), bounds.width - dx);
          bounds.x = gesture.bounds.x + gesture.bounds.width - bounds.width;
        }
        if (edge.includes('n')) {
          bounds.height = Math.max(Math.min(this.options.minHeight, window.innerHeight - 84), bounds.height - dy);
          bounds.y = gesture.bounds.y + gesture.bounds.height - bounds.height;
        }
      }
      this.apply(bounds);
    }
    keyboard(event, resize) {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      this.raise();
      const amount = event.shiftKey ? 40 : 10;
      const dx = event.key === 'ArrowLeft' ? -amount : event.key === 'ArrowRight' ? amount : 0;
      const dy = event.key === 'ArrowUp' ? -amount : event.key === 'ArrowDown' ? amount : 0;
      const bounds = {...this.bounds};
      if (resize) { bounds.width += dx; bounds.height += dy; }
      else { bounds.x += dx; bounds.y += dy; }
      this.apply(bounds);
    }
  }
  window.ToolFloatingWindow = ToolFloatingWindow;
})();
