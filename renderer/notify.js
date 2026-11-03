// ============================================================
// Toast 通知：无依赖的界面原语，谁都可以 import。
// ============================================================
// ============================================================
// Toast notifications
// ============================================================
export function showToast(type, title, body, dedupeKey = null) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const existing = dedupeKey && [...container.children].find(item => item.dataset.dedupeKey === dedupeKey);
  if (existing) { existing.querySelector('.toast-body').textContent = body; return; }
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
  // Auto-remove after 15s
  setTimeout(() => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  }, 15000);
}
