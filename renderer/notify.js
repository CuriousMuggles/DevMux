// ============================================================
// Toast 通知：无依赖的界面原语，谁都可以 import。
// ============================================================
// ============================================================
// Toast notifications
// ============================================================

// 中转拒绝提醒的合并键：同一对端的重复拒绝文案会随心跳时长变化
// （「心跳已中断 1748.1s」→「1742.5s」），只归一化变化的秒数，
// 不同对端、不同拒绝原因仍各留一条，避免把互不相关的提醒合并掉。
export function rejectDedupeKey(text) {
  return 'relay-reject:' + String(text || '')
    .replace(/心跳已中断\s+\d+(?:\.\d+)?\s*s/g, '心跳已中断 …s');
}

// 中转错误提醒的合并键：心跳写入、设备枚举、会话读写等失败会按固定节奏
// 重复推送，同一文案只保留一张卡片。
export function errorDedupeKey(text) {
  return 'relay-error:' + String(text || '');
}

function scheduleToastRemoval(toast, delayMs) {
  if (toast._removeTimer) clearTimeout(toast._removeTimer);
  toast._removeTimer = setTimeout(() => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  }, delayMs);
}

export function showToast(type, title, body, dedupeKey = null) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const existing = dedupeKey && [...container.children].find(item => item.dataset.dedupeKey === dedupeKey);
  if (existing) {
    existing.querySelector('.toast-body').textContent = body;
    scheduleToastRemoval(existing, 15000);
    return;
  }
  const toast = document.createElement('div');
  if (dedupeKey) toast.dataset.dedupeKey = dedupeKey;
  toast.className = `toast ${type}`;
  const icons = { success: '✓', error: '✗', info: 'ℹ' };
  toast.innerHTML = `
    <div class="toast-header">
      <span class="toast-icon">${icons[type] || 'ℹ'}</span>
      <span>${title}</span>
      <button class="toast-close">✕</button>
    </div>
    <div class="toast-body">${dedupeKey ? '' : body}</div>
  `;
  if (dedupeKey) toast.querySelector('.toast-body').textContent = body;
  toast.querySelector('.toast-close').addEventListener('click', () => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  });
  container.appendChild(toast);
  scheduleToastRemoval(toast, 15000);
}
