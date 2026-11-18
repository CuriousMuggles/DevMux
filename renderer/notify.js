// ============================================================
// Toast 通知：无依赖的界面原语，谁都可以 import。
// ============================================================
// 提示策略：成功类不弹（界面自身的变化就是反馈）；失败/警告/信息只留 3 秒；
// 同一个键再次出现时不再弹窗抢界面，只在右下角角标里累加次数，点击清零。
const TOAST_DURATION_MS = 3000;
// 只计数、没有可见卡片的同类提示，超过这个时间不再视为同一件事，
// 避免一次偶发错误导致同文案以后再也不弹。
const REPEAT_TTL_MS = 10 * 60 * 1000;

const toastEntries = new Map(); // dedupeKey -> { count, text, toast, timer, lastSeen }
let toastBadge = null;

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

function fallbackDedupeKey(type, title, body) {
  return `${type}:${title}:${body}`;
}

function pruneEntries(now) {
  for (const [key, entry] of toastEntries) {
    if (!entry.count && !entry.toast && now - entry.lastSeen > REPEAT_TTL_MS) toastEntries.delete(key);
  }
}

function repeatTotal() {
  let total = 0;
  for (const entry of toastEntries.values()) total += entry.count;
  return total;
}

function latestRepeatText() {
  let latest = '';
  for (const entry of toastEntries.values()) if (entry.count) latest = entry.text;
  return latest;
}

function clearRepeats() {
  const container = toastBadge && toastBadge.parentNode;
  for (const [key, entry] of toastEntries) {
    if (entry.toast) entry.count = 0;
    else toastEntries.delete(key);
  }
  if (container) renderBadge(container);
}

function renderBadge(container) {
  const total = repeatTotal();
  if (!total) {
    if (toastBadge && toastBadge.parentNode) toastBadge.parentNode.removeChild(toastBadge);
    toastBadge = null;
    return;
  }
  if (!toastBadge) {
    toastBadge = document.createElement('div');
    toastBadge.className = 'toast-badge';
    toastBadge.addEventListener('click', clearRepeats);
    container.appendChild(toastBadge);
  }
  toastBadge.textContent = `⚠ ${total}`;
  toastBadge.title = `同类提示已重复 ${total} 次，点击清除\n${latestRepeatText()}`;
}

function scheduleToastRemoval(entry, delayMs = TOAST_DURATION_MS) {
  if (entry.timer) clearTimeout(entry.timer);
  entry.timer = setTimeout(() => {
    if (entry.toast && entry.toast.parentNode) entry.toast.parentNode.removeChild(entry.toast);
    entry.toast = null;
    entry.timer = null;
  }, delayMs);
}

export function showToast(type, title, body, dedupeKey = null) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  // 成功类的提示太吵：操作成功本身在界面上已经能看到，不再弹窗。
  if (type === 'success') return;

  const kind = type === 'warn' ? 'warning' : type;
  const key = dedupeKey || fallbackDedupeKey(kind, title, body);
  const now = Date.now();
  pruneEntries(now);

  const known = toastEntries.get(key);
  if (known) {
    // 同类提示已经弹过：只累加角标计数，不再生成新卡片。
    known.count += 1;
    known.text = body;
    known.lastSeen = now;
    renderBadge(container);
    return;
  }

  const toast = document.createElement('div');
  toast.className = `toast ${kind}`;
  toast.dataset.dedupeKey = key;
  const icons = { error: '✗', warning: '⚠', info: 'ℹ' };

  const entry = { count: 0, text: body, toast, timer: null, lastSeen: now };
  const header = document.createElement('div');
  header.className = 'toast-header';
  const icon = document.createElement('span');
  icon.className = 'toast-icon';
  icon.textContent = icons[kind] || 'ℹ';
  const label = document.createElement('span');
  label.textContent = title;
  const close = document.createElement('button');
  close.className = 'toast-close';
  close.textContent = '✕';
  close.addEventListener('click', () => {
    if (entry.timer) { clearTimeout(entry.timer); entry.timer = null; }
    if (toast.parentNode) toast.parentNode.removeChild(toast);
    entry.toast = null;
  });
  const content = document.createElement('div');
  content.className = 'toast-body';
  content.textContent = body;

  header.appendChild(icon);
  header.appendChild(label);
  header.appendChild(close);
  toast.appendChild(header);
  toast.appendChild(content);
  container.appendChild(toast);
  toastEntries.set(key, entry);
  scheduleToastRemoval(entry);
}
