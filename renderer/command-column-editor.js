// 命令输入框共用列编辑：保留原textarea和发送链，列选区仅存在当前页面。
(() => {
  const editors = new WeakMap();
  const segmenter = new Intl.Segmenter('zh', { granularity: 'grapheme' });
  let active, canvas, observer, removal;
  const editable = area => !area.disabled && !area.readOnly;
  const snapshot = editor => ({ value: editor.area.value, start: editor.area.selectionStart,
    end: editor.area.selectionEnd, ranges: editor.ranges?.map(r => ({ ...r })) });
  function clear(editor) {
    editor.ranges = null;
    if (active !== editor) return;
    active = null; canvas?.remove(); canvas = null;
    observer?.disconnect(); removal?.disconnect();
  }
  function valid(editor) {
    if (editor.value !== editor.area.value) {
      clear(editor); editor.undo.length = editor.redo.length = 0; editor.value = editor.area.value;
    }
    return !!editor.ranges;
  }
  function geometry(area) {
    const css = getComputedStyle(area), ctx = document.createElement('canvas').getContext('2d');
    // font简写在设置了variant后可能为空，显式组合字体以免退回10px sans-serif。
    ctx.font = `${css.fontStyle} ${css.fontWeight} ${css.fontSize} ${css.fontFamily}`; ctx.fontKerning = 'none';
    const space = ctx.measureText(' ').width, tab = space * (parseFloat(css.tabSize) || 4);
    const rows = area.value.split('\n').map(text => {
      const points = [{ offset: 0, x: 0 }]; let x = 0;
      for (const part of segmenter.segment(text)) {
        x = part.segment === '\t' ? (Math.floor(x / tab) + 1) * tab : x + ctx.measureText(part.segment).width;
        points.push({ offset: part.index + part.segment.length, x });
      }
      return { text, points };
    });
    return { rows, line: parseFloat(css.lineHeight), left: parseFloat(css.paddingLeft),
      top: parseFloat(css.paddingTop), rect: area.getBoundingClientRect() };
  }
  const nearest = (points, x) => points.reduce((best, p) => Math.abs(p.x - x) < Math.abs(best.x - x) ? p : best);
  function point(area, event, g) {
    return { row: Math.max(0, Math.min(g.rows.length - 1,
      Math.floor((event.clientY - g.rect.top - area.clientTop - g.top + area.scrollTop) / g.line))),
    x: Math.max(0, event.clientX - g.rect.left - area.clientLeft - g.left + area.scrollLeft) };
  }
  function select(editor, anchor, end) {
    const g = geometry(editor.area);
    editor.ranges = [];
    for (let row = Math.min(anchor.row, end.row); row <= Math.max(anchor.row, end.row); row++) {
      const points = g.rows[row].points;
      editor.ranges.push({ row, start: nearest(points, Math.min(anchor.x, end.x)).offset,
        end: nearest(points, Math.max(anchor.x, end.x)).offset });
    }
    editor.value = editor.area.value;
    const first = editor.ranges[0], offset = g.rows.slice(0, first.row).reduce((n, r) => n + r.text.length + 1, 0) + first.start;
    editor.area.setSelectionRange(offset, offset);
    paint(editor);
  }
  function paint(editor) {
    if (!valid(editor) || !editor.area.isConnected) { clear(editor); return; }
    if (active && active !== editor) clear(active);
    active = editor;
    if (!canvas) {
      canvas = document.createElement('canvas'); canvas.className = 'command-column-overlay';
      canvas.setAttribute('aria-hidden', 'true'); document.body.appendChild(canvas);
      observer = new ResizeObserver(() => paint(editor)); observer.observe(editor.area);
      removal = new MutationObserver(() => { if (!editor.area.isConnected) clear(editor); });
      removal.observe(document.body, { childList: true, subtree: true });
    }
    const area = editor.area, g = geometry(area), ratio = devicePixelRatio || 1;
    Object.assign(canvas.style, { left: (g.rect.left + area.clientLeft) + 'px', top: (g.rect.top + area.clientTop) + 'px',
      width: area.clientWidth + 'px', height: area.clientHeight + 'px' });
    canvas.width = Math.round(area.clientWidth * ratio); canvas.height = Math.round(area.clientHeight * ratio);
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    for (const range of editor.ranges) {
      const points = g.rows[range.row]?.points; if (!points) continue;
      const x = points.find(p => p.offset === range.start)?.x || 0;
      const right = points.find(p => p.offset === range.end)?.x ?? x;
      ctx.fillStyle = right === x ? '#89b4fa' : 'rgba(137,180,250,0.38)';
      ctx.fillRect(g.left + x - area.scrollLeft, g.top + range.row * g.line - area.scrollTop, Math.max(2, right - x), g.line);
    }
  }
  function remember(editor, before) {
    const after = snapshot(editor); editor.value = after.value;
    if (before.value === after.value) return;
    editor.undo.push({ before, after }); if (editor.undo.length > 100) editor.undo.shift();
    editor.redo.length = 0;
  }
  function notify(editor, inputType) {
    editor.emitting = true;
    editor.area.dispatchEvent(new InputEvent('input', { bubbles: true, inputType }));
    editor.emitting = false;
  }
  function replace(editor, text, inputType = 'insertText') {
    if (!valid(editor) || !editable(editor.area)) return;
    const before = snapshot(editor), lines = editor.area.value.split('\n');
    const pasted = text.replace(/\r\n?/g, '\n').split('\n');
    const next = editor.ranges.map((range, index) => {
      const line = lines[range.row]; let { start, end } = range;
      if (start === end && ['deleteContentBackward','deleteContentForward'].includes(inputType)) {
        const points = [...segmenter.segment(line)].map(p => p.index).concat(line.length);
        if (inputType === 'deleteContentBackward') start = points.filter(p => p < start).pop() ?? start;
        else end = points.find(p => p > end) ?? end;
      }
      const insert = pasted.length === 1 ? pasted[0] : pasted[index] || '';
      lines[range.row] = line.slice(0, start) + insert + line.slice(end);
      return { row: range.row, start: start + insert.length, end: start + insert.length };
    });
    editor.area.value = lines.join('\n'); editor.value = editor.area.value; editor.ranges = next;
    const first = next[0], offset = lines.slice(0, first.row).reduce((n, line) => n + line.length + 1, 0) + first.start;
    const top = editor.area.scrollTop, left = editor.area.scrollLeft;
    editor.area.setSelectionRange(offset, offset); editor.area.scrollTop = top; editor.area.scrollLeft = left;
    remember(editor, before); paint(editor); notify(editor, inputType);
  }
  function restore(editor, state) {
    clear(editor); editor.area.value = editor.value = state.value;
    editor.area.setSelectionRange(state.start, state.end);
    editor.ranges = state.ranges?.map(r => ({ ...r }));
    if (editor.ranges) paint(editor);
    notify(editor, 'historyUndo');
  }
  function install(area) {
    if (editors.has(area)) return editors.get(area);
    area.wrap = 'off'; area.title = '按住 Alt 拖动鼠标进行列选择，可批量编辑、复制和粘贴；Esc 退出';
    const editor = { area, value: area.value, ranges: null, undo: [], redo: [] }; editors.set(area, editor);
    area.addEventListener('pointerdown', event => {
      valid(editor);
      if (!event.altKey || event.button !== 0 || !editable(area)) { clear(editor); return; }
      event.preventDefault(); area.focus();
      const anchor = point(area, event, geometry(area));
      editor.drag = { anchor, id: event.pointerId }; area.setPointerCapture(event.pointerId); select(editor, anchor, anchor);
    });
    area.addEventListener('pointermove', event => {
      if (!editor.drag || editor.drag.id !== event.pointerId) return;
      event.preventDefault(); const rect = area.getBoundingClientRect();
      if (event.clientY < rect.top || event.clientY > rect.bottom) area.scrollTop += event.clientY < rect.top ? -20 : 20;
      if (event.clientX < rect.left || event.clientX > rect.right) area.scrollLeft += event.clientX < rect.left ? -20 : 20;
      select(editor, editor.drag.anchor, point(area, event, geometry(area)));
    });
    const finish = () => { editor.drag = null; };
    area.addEventListener('pointerup', finish); area.addEventListener('pointercancel', finish); area.addEventListener('lostpointercapture', finish);
    area.addEventListener('blur', () => { finish(); clear(editor); });
    area.addEventListener('keydown', event => {
      if (event.isComposing || event.keyCode === 229 || editor.composition) { event.stopImmediatePropagation(); return; }
      valid(editor);
      const shortcut = (event.ctrlKey || event.metaKey) && !event.altKey;
      const undo = shortcut && event.key.toLowerCase() === 'z' && !event.shiftKey;
      const redo = shortcut && (event.key.toLowerCase() === 'y' || event.key.toLowerCase() === 'z' && event.shiftKey);
      if ((undo || redo) && editable(area)) {
        const source = undo ? editor.undo : editor.redo, entry = source.at(-1);
        if (entry && area.value === (undo ? entry.after : entry.before).value) {
          event.preventDefault(); event.stopImmediatePropagation(); source.pop();
          (undo ? editor.redo : editor.undo).push(entry); restore(editor, undo ? entry.before : entry.after); return;
        }
      }
      if (!editor.ranges) return;
      if (['Backspace', 'Delete'].includes(event.key)) {
        event.preventDefault(); event.stopImmediatePropagation();
        replace(editor, '', event.key === 'Backspace' ? 'deleteContentBackward' : 'deleteContentForward'); return;
      }
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); clear(editor); }
      else if (['Enter','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown','Tab'].includes(event.key) || shortcut && event.key.toLowerCase() === 'a') clear(editor);
    }, true);
    area.addEventListener('beforeinput', event => {
      valid(editor);
      if (editor.composition || event.isComposing) return;
      if (editor.ranges && editable(area)) {
        if (['insertText','insertReplacementText','insertFromPaste','deleteContentBackward','deleteContentForward'].includes(event.inputType)) {
          event.preventDefault(); replace(editor, event.data || '', event.inputType); return;
        }
        clear(editor);
      }
      editor.pending = snapshot(editor);
    });
    area.addEventListener('input', event => {
      if (editor.composition) { event.stopImmediatePropagation(); return; }
      if (editor.emitting) return;
      if (editor.pending) remember(editor, editor.pending);
      else if (editor.value !== area.value) { clear(editor); editor.undo.length = editor.redo.length = 0; }
      editor.pending = null; editor.value = area.value;
    });
    for (const type of ['copy','cut','paste']) area.addEventListener(type, event => {
      if (!valid(editor) || !event.clipboardData) return;
      if (type !== 'copy' && !editable(area)) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (type === 'paste') replace(editor, event.clipboardData.getData('text/plain'), 'insertFromPaste');
      else {
        const lines = area.value.split('\n');
        event.clipboardData.setData('text/plain', editor.ranges.map(r => lines[r.row].slice(r.start, r.end)).join('\n'));
        if (type === 'cut') replace(editor, '', 'deleteByCut');
      }
    });
    area.addEventListener('compositionstart', () => { if (valid(editor)) editor.composition = snapshot(editor); });
    area.addEventListener('compositionend', event => {
      const before = editor.composition; if (!before) return;
      editor.composition = null; area.value = editor.value = before.value; editor.ranges = before.ranges;
      if (event.data) replace(editor, event.data); else paint(editor);
    });
    return editor;
  }
  // 捕获阶段先安装，随后本次真实pointerdown即可进入控件监听器。
  for (const type of ['focusin','pointerdown']) document.addEventListener(type, event => {
    if (event.target.matches?.('textarea.terminal-cmd-textarea')) install(event.target);
  }, true);
  document.addEventListener('scroll', () => { if (active) paint(active); }, true);
  window.addEventListener('resize', () => { if (active) paint(active); });
  // 独立窗口不能用镜像值或选区恢复打断尚未提交的列输入法组合。
  window.CommandColumnEditor = { isComposing: area => !!editors.get(area)?.composition };
})();
