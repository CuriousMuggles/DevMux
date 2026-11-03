// ============================================================
// 跨机中转：角色徽标、连接状态、首连确认、配置弹窗。
// 依赖方向：core.js <- net.js <- notify.js <- relay.js（不反向引用 app.js）。
// ============================================================
import { state } from './core.js';
import { callWS } from './net.js';
import { showToast } from './notify.js';

// ============================================================
// 跨机中转（角色徽标、连接状态、首连确认、配置）
// ============================================================
export function isRemoteDevice(device) {
  return !!(device && device.relay);
}

export function isRemoteKey(key) {
  return typeof key === 'string' && key.startsWith('relay:');
}

/** 远端设备是否可用：对端离线时禁用操作（与本机设备离线时表现一致）。 */
export function isDeviceUsable(device) {
  if (!device) return false;
  if (device.relay) return device.relay.online !== false;
  return device.state === 'device' || device.state === undefined;
}

export function deviceLabel(device) {
  if (!device) return '(未知设备)';
  const tid = device.transportId ? ` (tid=${device.transportId})` : '';
  if (device.relay) {
    const online = device.relay.online === false ? ' · 离线' : '';
    return `${device.serial}${tid} ← ${relayPeerLabel(device.relay)}${online}`;
  }
  return `${device.serial}${tid}`;
}

/** 所有用户界面统一使用机器名 / 部署标识，peerId 只作为路由键。 */
export function relayPeerLabel(peer) {
  return peer.displayLabel || `${peer.host || '未知机器'} / ${peer.deploymentId || '默认部署'}`;
}

export function showRelayConfirmDialog(request) {
  if (state.relay.status && state.relay.status.role === 'slave') return;
  const overlay = document.getElementById('relay-confirm-overlay');
  if (!overlay || !request) return;
  state.relay.pendingPeer = request.peerId;
  const body = document.getElementById('relay-confirm-body');
  const detail = document.getElementById('relay-confirm-detail');
  if (body) {
    const rows = [
      ['对端部署', relayPeerLabel(request)],
      ['对端声明角色', request.roleLabel || '未知'],
      ['本机角色', state.relay.status ? (state.relay.status.roleLabel || '未知') : '未知'],
      ['主机名', request.host || '未知'],
      ['指纹', request.fingerprint || '未提供'],
      ['对外设备', `${request.deviceCount || 0} 台`],
      ['连接方式', request.carrier || '共享文件夹'],
      ['往返延迟', request.latencyMs !== undefined ? `${request.latencyMs} ms` : '未知'],
    ];
    body.innerHTML = rows
      .map(([k, v]) => `<div class="relay-confirm-row"><span>${k}</span><b>${v}</b></div>`)
      .join('');
  }
  if (detail) detail.textContent = request.detail || '';
  overlay.style.display = 'flex';
}

export function closeRelayConfirmDialog() {
  const overlay = document.getElementById('relay-confirm-overlay');
  if (overlay) overlay.style.display = 'none';
}

export async function answerRelayConfirm(accept) {
  const peerId = state.relay.pendingPeer;
  closeRelayConfirmDialog();
  state.relay.pendingPeer = null;
  if (!peerId) return;
  // 用户选择「暂不接受」后不再反复弹窗：安全边界在从机侧，本机只是知情。
  if (!accept) state.relay.ignoredPeers[peerId] = true;
  else delete state.relay.ignoredPeers[peerId];
  const result = await callWS('relay-confirm-peer', { peerId, accept }, 20000);
  showToast(
    result.success ? 'success' : 'error',
    accept ? '已确认对端' : '已忽略对端',
    result.output || ''
  );
  await refreshRelayStatus();
}

// 跨机时「离线」有两种完全不同的原因：对端被关掉了，还是它的中转节点卡住了。
// 状态里的 ageMs 现在是**真实静默时长**（以共享目录服务器时钟为基准，不再被两端
// 时钟差污染），所以可以放心显示给用户 —— 秒级说明对方刚停或网络断了，分钟级
// 说明对方确实关掉了。
export function relayAgeText(ms) {
  const secs = Math.floor((Number(ms) || 0) / 1000);
  if (secs <= 0) return '';
  if (secs < 60) return `（最后心跳 ${secs} 秒前）`;
  if (secs < 3600) return `（最后心跳 ${Math.floor(secs / 60)} 分钟前）`;
  return `（最后心跳 ${(secs / 3600).toFixed(1)} 小时前）`;
}

export function relayStateClass(status) {
  if (!status) return '';
  if (!status.enabled) return 'off';
  if (!status.running) return 'bad';
  const item = relaySelectedPeer(status);
  // 灯色只表示心跳与连接事实，验证失败但仍有心跳时保持黄色并展示原因。
  if (!item || !item.online) return 'bad';
  if (!status.conflict && item.connectionState === 'connected') return 'ok';
  return 'warn';
}

export function relayLightText(status) {
  const labels = { off: '未启用', bad: '未检测到有效心跳',
    warn: '已检测到心跳，等待或正在建立连接', ok: '连接成功' };
  return labels[relayStateClass(status)] || '尚未读取状态';
}

export function relaySelectedPeer(status) {
  const pairing = (status && status.pairing) || [];
  if (status && status.role === 'slave') {
    // 历史配对不是当前主控：仅从已发现的主控角色中选择。
    const masters = pairing.filter(item => item.role === 'master' || item.roleLabel === '主控端');
    return masters.find(item => item.paired && item.connectionState === 'connected')
      || masters.find(item => item.online && item.peerId === status.master)
      || masters.find(item => item.online && item.paired)
      || masters.find(item => item.online)
      || masters.find(item => item.paired) || null;
  }
  const selected = document.getElementById('relay-pairing');
  return pairing.find(item => selected && item.nodeId === selected.value) || pairing[0] || null;
}

export function relayVisibleState(status) {
  if (!status.enabled) return '未启用';
  if (!status.running) return '启动失败';
  const item = relaySelectedPeer(status);
  if (!item) return status.conflict ? '角色冲突'
    : status.role === 'slave' ? '等待主控连接' : '等待发现从机';
  const labels = {
    'local-confirmation': '待本机确认',
    'waiting-peer-confirmation': '等待对端确认',
    checking: '正在验证',
    'check-failed': '验证失败',
    connected: '通讯已验证',
    offline: '对端离线',
    'role-mismatch': '角色不匹配',
    'waiting-master': '等待主控连接',
  };
  return labels[item.connectionState] || '等待连接验证';
}

/** 共享目录中的可选实例统一显示在配对列表，本机名称固定不随多开变化。 */
export function renderRelayPool(status) {
  renderRelayRoleUi(status);
  const localIdEl = document.getElementById('relay-peer-id');
  if (localIdEl) {
    localIdEl.textContent = status.machineName || '计算机名称';
    localIdEl.title = '本机计算机名称，多开和切换部署时保持不变';
  }
  const nodeIdEl = document.getElementById('relay-node-id');
  if (nodeIdEl) nodeIdEl.textContent = status.nodeId || '未生成';
  const rootHint = document.getElementById('relay-pool-root');
  if (rootHint) {
    rootHint.textContent = status.effectiveRoot && status.effectiveRoot !== status.root
      ? `实际数据根 ${status.effectiveRoot}` : '';
    rootHint.title = rootHint.textContent;
  }
  const rootInput = document.getElementById('relay-root');
  if (rootInput && rootInput.value === status.root) {
    renderRelayDeployments(status.deployments || []);
  }
  const pairSelect = document.getElementById('relay-pairing');
  const pairing = status.pairing || [];
  if (pairSelect) {
    const previous = pairSelect.value;
    pairSelect.replaceChildren();
    for (const item of pairing) {
      const stateLabel = item.connectionState === 'connected' ? '已连接'
        : item.connectionState === 'waiting-peer-confirmation' ? '等待对端确认'
        : item.connectionState === 'check-failed' ? '验证失败'
        : item.connectionState === 'role-mismatch' ? '角色不匹配'
        : item.connectionState === 'checking' ? '主控已确认 · 验证中'
        : !item.online ? '离线'
        : item.paired ? '本机已确认'
        : item.state === 'revoked' ? '已解除' : '待本机确认';
      const roleText = item.roleLabel ? `${item.roleLabel} ` : '';
      const light = !item.online ? '🔴' : item.connectionState === 'connected' && !status.conflict ? '🟢' : '🟡';
      const label = `${light} ${item.host || item.peerId || item.nodeId} ｜ `
        + `${item.deploymentId || '默认部署'} ｜ ${roleText}${stateLabel}`;
      const option = new Option(label, item.nodeId);
      option.title = `实例 ${item.nodeId}`;
      pairSelect.add(option);
    }
    if (!pairing.length) pairSelect.add(new Option('(共享目录中暂无可配对实例)', ''));
    if (pairing.some(item => item.nodeId === previous)) pairSelect.value = previous;
    pairSelect.disabled = !pairing.length;
  }
  syncRelayPairButtons();
  const pairNote = document.getElementById('relay-pair-note');
  if (pairNote) {
    pairNote.textContent = '主控选择从机并确认后自动验证连接，从机无需操作。只有验证成功后才能管理远端设备。';
    pairNote.title = pairNote.textContent;
  }
}

/** 从机只配置并等待；当前主控和连接事实由后端提供，不使用隐藏下拉框选路。 */
export function renderRelayRoleUi(status) {
  const role = document.getElementById('relay-role').value || (status && status.role);
  const slave = role === 'slave';
  document.getElementById('relay-master-connection').hidden = slave;
  document.getElementById('relay-slave-connection').hidden = !slave;
  document.getElementById('relay-step-confirm').textContent = slave ? '2. 等待主控连接' : '2. 主控确认连接';
  document.getElementById('relay-step-use').textContent = slave ? '3. 提供设备' : '3. 管理设备';
  document.getElementById('relay-use-title').textContent = slave
    ? '3. 提供设备　连接后主控即可管理本机共享的设备，本机保持运行即可。'
    : '3. 管理设备　连接验证成功后，设备和终端自动出现在主控窗口。';
  const item = relaySelectedPeer(status);
  document.getElementById('relay-slave-status').textContent = item
    ? `当前主控：${relayPeerLabel(item)}。${item.connectionReason || '等待主控自动验证连接'}。`
    : '本机已就绪后，等待主控在其窗口选择本机并确认连接。';
  if (slave) {
    closeRelayConfirmDialog();
    state.relay.pendingPeer = null;
  }
}

/** 部署选项来自当前共享目录；保留正在编辑但尚未保存的新增项。 */
export function renderRelayDeployments(deployments, selected) {
  const select = document.getElementById('relay-pool-id');
  if (!select) return;
  const value = selected === undefined ? select.value : selected;
  const names = new Set(deployments.filter(Boolean));
  if (value) names.add(value);
  select.replaceChildren(new Option('默认部署（原目录）', ''));
  for (const name of [...names].sort()) select.add(new Option(name, name));
  select.value = value || '';
}

export let relayDeploymentRequest = 0;
export async function refreshRelayDeployments() {
  const root = document.getElementById('relay-root');
  if (!root) return;
  const path = root.value.trim();
  const request = ++relayDeploymentRequest;
  if (!path) { renderRelayDeployments([]); return; }
  const result = await callWS('relay-discover-deployments', { root: path }, 15000);
  if (request !== relayDeploymentRequest || root.value.trim() !== path) return;
  renderRelayDeployments(result.deployments || []);
}

export function openRelayDeploymentDialog() {
  const overlay = document.getElementById('relay-deployment-overlay');
  const input = document.getElementById('relay-deployment-name');
  document.getElementById('relay-deployment-error').hidden = true;
  input.value = '';
  overlay.style.display = 'flex';
  input.focus();
}

export function closeRelayDeploymentDialog() {
  document.getElementById('relay-deployment-overlay').style.display = 'none';
  document.getElementById('relay-add-deployment').focus();
}

export function addRelayDeployment() {
  const name = document.getElementById('relay-deployment-name').value.trim();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,40}$/.test(name)) {
    const error = document.getElementById('relay-deployment-error');
    error.textContent = '请输入 1–41 位部署标识，以字母或数字开头，只含字母、数字、点、下划线和短横线。';
    error.hidden = false;
    return;
  }
  const select = document.getElementById('relay-pool-id');
  renderRelayDeployments([...select.options].map(option => option.value), name);
  closeRelayDeploymentDialog();
  renderRelayEffective();
}

/** 配对/解除按钮的可用性跟着当前选中的那一条走，避免点了没反应。 */
export function syncRelayPairButtons() {
  const pairSelect = document.getElementById('relay-pairing');
  const pairBtn = document.getElementById('relay-pair');
  const unpairBtn = document.getElementById('relay-unpair');
  if (!pairSelect || !pairBtn || !unpairBtn) return;
  const status = state.relay.status || {};
  const item = status.role === 'slave' ? relaySelectedPeer(status)
    : (status.pairing || []).find(entry => entry.nodeId === pairSelect.value);
  const peer = (status.peers || []).find(entry => item && entry.peerId === item.peerId);
  const hasTarget = !!pairSelect.value;
  const sameRole = !!(item && item.roleLabel && item.roleLabel === status.roleLabel);
  const needsIdentity = !!(peer && peer.needsConfirmation);
  const needsReconnect = !!(item && item.connectionState === 'waiting-peer-confirmation');
  const master = status.role === 'master';
  pairBtn.disabled = !master || !hasTarget || !status.running || relayConfigDirty() || sameRole
    || !item || !item.online || (!!item.paired && !needsIdentity && !needsReconnect);
  pairBtn.textContent = needsIdentity ? '重新确认身份' : needsReconnect ? '重新确认连接'
    : (item && item.paired ? '主控已确认' : '确认连接');
  unpairBtn.disabled = !master || !hasTarget || !(item && item.paired);
  renderRelayNextAction(status, item, peer);
}

/** 只按后端逐对端验证结论推进步骤，不把本机确认或旧终端快照当作接通。 */
export function renderRelayNextAction(status, item, peer) {
  const el = document.getElementById('relay-next-action');
  if (!el) return;
  let step = 1;
  let text = '下一步：两端启用中转，选择同一个共享目录，分别保存主控端和从机配置。';
  if (relayConfigDirty()) {
    text = '下一步：先点击「保存并生效」。连接操作使用当前生效配置，不能使用尚未保存的目录或角色。';
  } else if (status.enabled && !status.running) {
    text = `配置尚未运行：${status.startError || status.hint || '请检查共享目录后保存。'}`;
  } else if (status.running) {
    step = 2;
    if (status.conflict) {
      text = '下一步：处理角色冲突。检查已确认的在线实例，让该连接的一端为主控、另一端为从机。';
    } else if (status.role === 'slave' && !item) {
      text = '本机已就绪，等待主控选择并确认连接；从机无需选择对端。可在高级设置调整共享设备和命令权限。';
    } else if (!item) {
      text = '下一步：在另一端启用中转并保存相同共享目录。发现后会出现在「连接对端」下拉列表，部署可以不同。';
    } else if (item.connectionState === 'connected') {
      step = 3;
      text = status.role === 'master'
        ? `通讯已验证：已与 ${relayPeerLabel(item)} 建立连接，可在主窗口管理设备。没有共享设备时，请在从机接入设备并检查共享范围。`
        : `通讯已验证：已与 ${relayPeerLabel(item)} 建立连接。设备操作在主控窗口完成，本机保持运行即可。`;
    } else {
      text = `${item.connectionReason || '尚未验证连接'}。${item.nextAction || '请检查对端状态。'}`;
    }
  }
  el.textContent = text;
  for (const [index, id] of ['relay-step-config', 'relay-step-confirm', 'relay-step-use'].entries()) {
    const node = document.getElementById(id);
    if (!node) continue;
    node.classList.toggle('active', index + 1 === step);
    node.classList.toggle('complete', index + 1 < step);
    if (index + 1 === step) node.setAttribute('aria-current', 'step');
    else node.removeAttribute('aria-current');
  }
}

export async function pairSelectedPeer() {
  if ((state.relay.status || {}).role !== 'master') return;
  const pairSelect = document.getElementById('relay-pairing');
  const nodeId = pairSelect ? pairSelect.value : '';
  if (!nodeId) return;
  const status = state.relay.status || {};
  const item = (status.pairing || []).find(entry => entry.nodeId === nodeId) || {};
  const result = await callWS('relay-pair-peer', { nodeId, peerId: item.peerId || '' }, 20000);
  showToast(result.success ? 'success' : 'error', '确认连接',
    result.success ? '主控已确认，正在自动验证连接；从机无需操作。' : result.output || '');
  await refreshRelayStatus();
}

export async function unpairSelectedPeer() {
  const pairSelect = document.getElementById('relay-pairing');
  const nodeId = pairSelect ? pairSelect.value : '';
  if (!nodeId) return;
  const result = await callWS('relay-unpair-peer', { nodeId }, 20000);
  showToast(result.success ? 'success' : 'error', '跨机中转配对', result.output || '');
  await refreshRelayStatus();
}

// --- 配置弹窗（入口：菜单栏 → 工具 → 跨机中转配置…）---
export function relayConfigDialogOpen() {
  const overlay = document.getElementById('relay-config-overlay');
  return !!overlay && overlay.style.display === 'flex';
}

export async function openRelayConfigDialog() {
  const overlay = document.getElementById('relay-config-overlay');
  if (!overlay) return;
  // 每次打开都从磁盘重读配置：relay.json 就在 exe 同目录，**另一个实例**或手工
  // 编辑都可能改过它。只在首连时读一次的话，这个窗口会一直拿着过期表单，一保存
  // 就把别人的改动覆盖回去 —— 表现出来就是「明明设了主控，却还是从机」。
  await loadRelayConfig();
  await refreshRelayStatus();
  window.ToolPanels.relayConfig.show();
  overlay.style.display = 'flex';
}

export function closeRelayConfigDialog() {
  const overlay = document.getElementById('relay-config-overlay');
  if (overlay) window.ToolPanels.relayConfig.hide();
}

/** 表单里的配置（与提交给后端的载荷同一套取值规则）。 */
export function relayFormValues() {
  return collectRelayConfigForm();
}

/** 表单里是否有还没保存的改动 —— 有就说明眼前这套界面不是「现在生效的」。 */
export function relayConfigDirty() {
  const saved = state.relay.config;
  if (!saved) return false;
  const savedView = {
    enabled: !!saved.enabled,
    role: saved.role || 'slave',
    // 没有 peerId：本机标识不再是表单项（自动生成），留在这里会让「表单 vs 已保存」
    // 的对比永远不相等 —— 界面会一直显示「有未保存的改动」。
    carrierKind: saved.carrierKind || 'shared-folder',
    root: String(saved.root || ''),
    poolId: String(saved.poolId || ''),
    pairingRequired: true,
    allowedSubcommands: saved.allowedSubcommands || [],
    allowShell: !!saved.allowShell,
    managedSerials: saved.managedSerials || [],
  };
  return JSON.stringify(relayFormValues()) !== JSON.stringify(savedView);
}

/**
 * 渲染「当前生效」那一行：角色与标识一律取自**运行中的节点**上报的状态，
 * 不取自表单。表单是「你想改成什么」，这行是「现在真正在跑什么」——分开显示，
 * 才能一眼看出「改了但没生效」。
 */
export function relayRuntimeText(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  return `${hours}小时 ${minutes}分 ${total % 60}秒`;
}

export function renderRelayEffective() {
  const el = document.getElementById('relay-effective');
  if (!el) return;
  const status = state.relay.status;
  renderRelayRoleUi(status);
  const dirty = relayConfigDirty();
  if (!status) {
    el.className = 'relay-effective off';
    el.textContent = '尚未读取中转状态。';
    return;
  }
  // 多实例隔离说明：本窗口因为默认配置被另一个实例占用而改用独立临时配置时，
  // 必须让用户知道（否则「为什么设置不保留」会变成灵异事件）。
  const noticeEl = document.getElementById('relay-config-notice');
  if (noticeEl) {
    noticeEl.textContent = status.configNotice || '';
    noticeEl.hidden = !status.configNotice;
  }
  const lines = [];
  if (!status.enabled) {
    lines.push('<span class="relay-effective-dim">当前未启用跨机中转。</span>');
  } else {
    lines.push(
      `当前生效：<span class="relay-effective-role">${status.roleLabel || '未知角色'}</span>`
      + `　本机标识 <b>${status.machineName || '未设置'}</b>`
      + `　运行状态 <span class="relay-effective-dim">${relayVisibleState(status)}</span>`
    );
    lines.push(`<span class="relay-effective-dim">本机运行 ${relayRuntimeText(status.uptimeSeconds)} · 心跳 ${Number(status.heartbeatSeq) || 0}</span>`);
    const selectedPeer = relaySelectedPeer(status);
    if (selectedPeer && selectedPeer.heartbeatSeq) {
      lines.push(`<span class="relay-effective-dim">对端运行 ${relayRuntimeText(selectedPeer.uptimeSeconds)} · 心跳 ${Number(selectedPeer.heartbeatSeq) || 0}</span>`);
    }
    const detail = selectedPeer ? selectedPeer.connectionReason : status.hint;
    if (detail) {
      lines.push(`<span class="relay-effective-dim">${detail}</span>`);
    }
  }
  if (dirty) {
    lines.push('<span class="relay-effective-pending">⚠ 表单里有未生效的改动，点「保存并生效」后才会起作用</span>');
  }
  el.className = 'relay-effective'
    + (dirty ? ' pending' : '')
    + (status.enabled ? '' : ' off');
  el.innerHTML = lines.join('<br>');
  syncRelayPairButtons();
}

export function updateRelayStatusUi() {
  const status = state.relay.status;
  // 本机作为从机正在被使用的会话数：坐在被控机器前的人必须看得见有人在操作它。
  const served = (status && status.servedSessions) || [];
  const selectedPeer = relaySelectedPeer(status);
  const paired = ((status && status.pairing) || []).filter(item => status.role === 'slave'
    ? item.paired && (item.role === 'master' || item.roleLabel === '主控端')
    : item.role === 'slave' || item.roleLabel === '从机');
  const connectedCount = paired.filter(item => item.connectionState === 'connected').length;
  const waitingCount = paired.filter(item => item.online && item.connectionState !== 'connected').length;
  const offlineCount = paired.filter(item => !item.online).length;
  const badge = document.getElementById('status-relay');
  if (badge) {
    if (!status || !status.enabled) {
      badge.hidden = true;
      badge.textContent = '';
    } else {
      badge.hidden = false;
      const classes = ['status-relay', relayStateClass(status)];
      badge.className = classes.filter(Boolean).join(' ');
      badge.textContent = `${status.roleLabel || ''} ${status.machineName || ''}｜${relayVisibleState(status)}`
        + (status.role === 'master' && paired.length > 1 ? `｜已连接 ${connectedCount} / 待处理 ${waitingCount} / 离线 ${offlineCount}` : '')
        + (served.length ? `｜被控 ${served.length}` : '');
      badge.title = `${relayLightText(status)}。点击查看详细状态`;
      badge.onclick = openRelayConfigDialog;
      badge.setAttribute('role', 'button');
      badge.tabIndex = 0;
      badge.onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openRelayConfigDialog();
        }
      };
    }
  }
  const stateLine = document.getElementById('relay-state');
  if (stateLine && status) {
    const servedText = served.length
      ? `（正在为对端提供 ${served.length} 个终端会话：`
        + served.map(s => s.serial || s.deviceKey || s.streamId).join('、') + '）'
      : '';
    const stateText = status.enabled
      ? `${relayVisibleState(status)}：${selectedPeer ? selectedPeer.connectionReason || '' : status.hint || ''}${servedText}`
      : '未启用';
    stateLine.textContent = stateText;
    stateLine.className = `tool-label relay-link-light ${relayStateClass(status)}`;
    // 这行在窄处会被省略号截断（见 style.css），完整内容挂 title 里仍可看。
    // 完整的运行状态在弹窗上方的「当前生效」框里也有一份。
    stateLine.title = `${relayLightText(status)}。${stateText}`;
  }
  const events = document.getElementById('relay-events');
  if (events && status) {
    const lines = (status.events || []).map(e => `[${e.at ? e.at.slice(11, 19) : ''}] ${e.text}`);
    events.textContent = lines.join('\n') || '(暂无事件)';
    events.scrollTop = events.scrollHeight;
  }
  // 冲突/待确认要显眼：这属于需要人做决定的状态，不能只在状态栏里灰着。
  const warning = document.getElementById('relay-warning');
  if (warning) {
    const pending = (status && status.pendingConfirmation) || [];
    if (status && status.conflict) {
      warning.hidden = false;
      warning.textContent = `⚠ 角色冲突：${status.hint || ''}`;
    } else if (status && status.role === 'slave') {
      warning.hidden = true;
      warning.textContent = '';
    } else if (pending.length) {
      warning.hidden = false;
      warning.textContent = `⚠ 有对端待本机确认（${pending.map(id => {
        const peer = (status.peers || []).find(item => item.peerId === id);
        return peer ? relayPeerLabel(peer) : '未知对端';
      }).join('、')}）：核对身份后确认连接`;
    } else {
      // 未配对实例是「门槛生效」的证据，不是故障；但它正是「对端在跑却连不上」
      // 的原因，所以必须在这里说一句，否则用户会去查网络。
      const unpaired = (status && status.unpairedPeers) || [];
      const online = unpaired.filter(item => item.online);
      if (status && status.pairingRequired && online.length) {
        warning.hidden = false;
        warning.textContent = `⚠ 有 ${online.length} 个实例未配对（`
          + online.map(item => relayPeerLabel(item)).join('、')
          + '）：请在「连接对端」中选择并确认连接';
      } else {
        warning.hidden = true;
        warning.textContent = '';
      }
    }
  }
  // 共享目录中的实例统一在配对列表显示。
  renderRelayPool(status);
  // 「当前生效」那一行要跟着状态走（它是从运行中的节点上报的，不是表单值）。
  renderRelayEffective();
}

export async function refreshRelayStatus() {
  const status = await callWS('relay-status', {}, 10000);
  if (status && status.state !== undefined) {
    state.relay.status = status;
    updateRelayStatusUi();
  }
}

export async function loadRelayConfig() {
  const result = await callWS('relay-config', {}, 10000);
  if (!result || !result.config) return;
  state.relay.config = result.config;
  state.relay.carriers = result.carriers || [];
  state.relay.configPath = result.configPath || '';
  state.relay.loaded = true;

  const set = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = !!value;
    else el.value = value === undefined || value === null ? '' : value;
  };
  set('relay-enabled', result.config.enabled);
  set('relay-role', result.config.role || 'master');
  set('relay-root', result.config.root || '');
  renderRelayDeployments([], result.config.poolId || '');
  await refreshRelayDeployments();
  // 三态：'' = 跟随池，'1' = 强制开启，'0' = 强制关闭。用字符串是因为 select 的
  // value 只能是字符串，而这里必须能表达「未表态」这第三种意思。
  set('relay-pairing-required', '1');
  set('relay-subcommands', (result.config.allowedSubcommands || []).join(', '));
  set('relay-allow-shell', result.config.allowShell);
  set('relay-managed-serials', (result.config.managedSerials || []).join(', '));

  const carrierSelect = document.getElementById('relay-carrier');
  if (carrierSelect) {
    carrierSelect.innerHTML = (result.carriers || []).map(c => (
      c.implemented
        ? `<option value="${c.kind}">${c.label}</option>`
        : `<option value="${c.kind}" disabled>${c.label}（未实现）</option>`
    )).join('');
    carrierSelect.value = result.config.carrierKind || 'shared-folder';
  }
  updateRelayCarrierNote();
  const pathHint = document.getElementById('relay-config-path');
  if (pathHint) {
    const text = result.configPath ? `配置：${result.configPath}` : '';
    pathHint.textContent = text;
    pathHint.title = text;   // 长路径会被省略号截断，完整路径挂 title
  }
  // 本机标识固定显示 status.machineName；内部路由键不作为用户的本机名称。
}

export function updateRelayCarrierNote() {
  const select = document.getElementById('relay-carrier');
  const note = document.getElementById('relay-carrier-note');
  if (!select || !note) return;
  const spec = (state.relay.carriers || []).find(c => c.kind === select.value);
  note.textContent = spec ? (spec.implemented ? '可用' : `未实现：${spec.note || ''}`) : '';
}

export function collectRelayConfigForm() {
  const value = (id) => {
    const el = document.getElementById(id);
    if (!el) return undefined;
    return el.type === 'checkbox' ? el.checked : el.value;
  };
  const splitList = (text) => String(text || '')
    .split(/[,，\s]+/)
    .map(s => s.trim())
    .filter(Boolean);
  return {
    enabled: value('relay-enabled'),
    role: value('relay-role'),
    carrierKind: value('relay-carrier'),
    root: String(value('relay-root') || '').trim(),
    poolId: String(value('relay-pool-id') || '').trim(),
    pairingRequired: true,
    allowedSubcommands: splitList(value('relay-subcommands')),
    allowShell: value('relay-allow-shell'),
    managedSerials: splitList(value('relay-managed-serials')),
  };
}

/**
 * 保存并生效。换到一个「已经有中转数据」的共享目录时，先问用户要沿用还是
 * 清空重建 —— 两轮数据（旧心跳、旧消息队列、别的机器的分区）默默混在一个
 * 目录里，排障时是灾难。返回是否真的保存了。
 */
export async function saveRelayConfig() {
  const form = collectRelayConfigForm();
  if (!await checkRelayDuplicate()) return false;
  const currentRoot = (state.relay.config && state.relay.config.root) || '';
  if (form.enabled && form.carrierKind === 'shared-folder' && form.root && form.root !== currentRoot) {
    const info = await callWS('relay-inspect-root', { root: form.root }, 15000);
    if (info && info.hasData) {
      const choice = await askRelayRootChoice(info);
      if (choice === 'cancel') return false;
      if (choice === 'clean') {
        const cleaned = await callWS('relay-clean-root', { root: form.root }, 60000);
        showToast(cleaned.success ? 'success' : 'error', '跨机中转', cleaned.output || '');
        if (!cleaned.success) return false;
      }
    }
  }
  const result = await callWS('relay-save-config', { config: form }, 30000);
  showToast(result.success ? 'success' : 'error', '跨机中转配置', result.output || '');
  if (!result.success) return false;
  // 自动刷新：配置刚生效，表单、状态徽标、「当前生效」那一行都要立刻跟上。
  await loadRelayConfig();
  await refreshRelayStatus();
  return true;
}

export async function checkRelayDuplicate(preview = false) {
  const form = collectRelayConfigForm();
  if (form.role !== 'slave' || !form.root || (!preview && !form.enabled)) return true;
  const result = await callWS('relay-check-config', { config: { ...form, enabled: true } }, 15000);
  if (!result || !result.success) {
    showToast('error', '跨机中转配置', result && result.output || '配置预检失败，请重试');
    return false;
  }
  return true;
}

/**
 * 「目录里已有中转数据」的选择框：沿用 / 清空后重建 / 取消。
 * 把盘点到的文件一样样列出来，用户才知道「清空」到底会删什么。
 */
export function askRelayRootChoice(info) {
  return new Promise((resolve) => {
    const overlay = document.getElementById('relay-root-choice-overlay');
    if (!overlay) { resolve('cancel'); return; }
    const desc = document.getElementById('relay-root-choice-desc');
    const files = document.getElementById('relay-root-choice-files');
    if (desc) desc.textContent = `目录 ${info.root || ''} 里已经有中转数据：`;
    if (files) {
      const rows = [];
      if (info.manifest) rows.push('<li><code>relay.json</code> —— 中转布局清单</li>');
      const slotLabels = {
        peers: '对端心跳', requests: '命令请求', results: '命令结果',
        streams: '终端流', archive: '归档',
      };
      for (const [slot, label] of Object.entries(slotLabels)) {
        const count = Number(info[slot] || 0);
        if (count > 0) rows.push(`<li><code>${slot}/</code> —— ${label}，${count} 项</li>`);
      }
      files.innerHTML = rows.join('');
      files.hidden = rows.length === 0;
    }
    let settled = false;
    const buttons = [
      ['relay-root-keep', 'keep'],
      ['relay-root-clean', 'clean'],
      ['relay-root-cancel', 'cancel'],
      ['relay-root-choice-close', 'cancel'],
    ];
    const onKey = (e) => {
      if (e.key === 'Escape') finish('cancel');
    };
    const onOverlayClick = (e) => {
      if (e.target === overlay) finish('cancel');
    };
    const cleanup = () => {
      for (const [id] of buttons) {
        const el = document.getElementById(id);
        if (el && el._relayChoiceHandler) {
          el.removeEventListener('click', el._relayChoiceHandler);
          delete el._relayChoiceHandler;
        }
      }
      document.removeEventListener('keydown', onKey);
      overlay.removeEventListener('click', onOverlayClick);
    };
    const finish = (value) => {
      if (settled) return;
      settled = true;
      overlay.style.display = 'none';
      cleanup();
      resolve(value);
    };
    for (const [id, value] of buttons) {
      const el = document.getElementById(id);
      if (!el) continue;
      el._relayChoiceHandler = () => finish(value);
      el.addEventListener('click', el._relayChoiceHandler);
    }
    overlay.addEventListener('click', onOverlayClick);
    document.addEventListener('keydown', onKey);
    overlay.style.display = 'flex';
  });
}

export let relayCacheBusy = false;

export function askRelayCacheClear(root) {
  return new Promise(resolve => {
    const overlay = document.getElementById('relay-cache-confirm-overlay');
    if (!overlay) { resolve(false); return; }
    document.getElementById('relay-cache-confirm-path').textContent = `已保存共享目录：${root}`;
    const bindings = [
      ['relay-cache-confirm-delete', true], ['relay-cache-confirm-cancel', false],
      ['relay-cache-confirm-close', false],
    ].map(([id, value]) => [document.getElementById(id), () => finish(value)]);
    const onKey = event => {
      if (event.key === 'Escape') { event.preventDefault(); finish(false); }
    };
    const onBackdrop = event => { if (event.target === overlay) finish(false); };
    let settled = false;
    const previousFocus = document.activeElement;
    const finish = value => {
      if (settled) return;
      settled = true;
      overlay.style.display = 'none';
      bindings.forEach(([el, handler]) => el.removeEventListener('click', handler));
      document.removeEventListener('keydown', onKey);
      overlay.removeEventListener('click', onBackdrop);
      if (previousFocus) previousFocus.focus();
      resolve(value);
    };
    bindings.forEach(([el, handler]) => el.addEventListener('click', handler));
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', onBackdrop);
    overlay.style.display = 'flex';
    document.getElementById('relay-cache-confirm-cancel').focus();
  });
}

export async function clearRelayCache() {
  if (relayCacheBusy) return;
  const saved = state.relay.config;
  const resultEl = document.getElementById('relay-clear-cache-result');
  const report = (text, error = false, partial = false) => {
    resultEl.textContent = text;
    resultEl.hidden = false;
    showToast(error ? 'error' : partial ? 'info' : 'success', partial ? '缓存清理部分完成' : '清除缓存', text);
  };
  if (!saved || !saved.root || saved.carrierKind !== 'shared-folder') {
    report('请先保存有效的共享目录配置。', true);
    return;
  }
  if (relayConfigDirty()) {
    report('配置有未保存的改动，请先保存或刷新配置，再清除缓存。', true);
    return;
  }
  relayCacheBusy = true;
  const controls = ['relay-clear-cache', 'relay-save', 'relay-refresh'].map(id => document.getElementById(id));
  const disabled = controls.map(el => el.disabled);
  controls.forEach(el => { el.disabled = true; });
  try {
    if (!await askRelayCacheClear(saved.root)) return;
    resultEl.hidden = false;
    resultEl.textContent = '正在扫描各部署闲置缓存，保留活动文件并统计减少空间；中转和终端保持运行…';
    const result = await callWS('relay-clear-cache', { root: saved.root, confirmed: true }, 120000);
    report(result.output && result.output !== 'timeout' ? result.output
      : '清除结果未确认，请检查目录和状态；不会自动重试。', !result.success, result.completed === false || result.limited === true);
    await loadRelayConfig();
    await refreshRelayStatus();
  } catch (error) {
    report('清除结果未确认，请检查目录和状态；不会自动重试。' + String(error.message || error), true);
  } finally {
    controls.forEach((el, index) => { el.disabled = disabled[index]; });
    relayCacheBusy = false;
  }
}

export function initRelay() {
  const clearCache = document.getElementById('relay-clear-cache');
  if (clearCache) clearCache.addEventListener('click', clearRelayCache);
  const save = document.getElementById('relay-save');
  if (save) {
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        await saveRelayConfig();
      } finally {
        save.disabled = false;
      }
    });
  }
  const refresh = document.getElementById('relay-refresh');
  if (refresh) {
    refresh.addEventListener('click', async () => {
      refresh.disabled = true;
      // 「刷新状态」不只是拉一次状态，而是**按磁盘上的配置重新生效**：配置可能被
      // 另一个实例或手工编辑改过，那种情况下光拉状态永远看不出角色已经变了。
      const result = await callWS('relay-reload-config', {}, 30000);
      refresh.disabled = false;
      showToast(
        result.success ? 'success' : 'error',
        '跨机中转',
        result.output || (result.success ? '状态已刷新' : '刷新失败'),
      );
      await loadRelayConfig();
      await refreshRelayStatus();
    });
  }
  const browser = document.getElementById('relay-browse');
  if (browser) {
    browser.addEventListener('click', async () => {
      const picked = await callWS('pick-directory', { title: '选择跨机中转的共享目录' });
      if (picked && picked.success && picked.path) {
        const root = document.getElementById('relay-root');
        if (root) root.value = picked.path;
        renderRelayEffective();
        await refreshRelayDeployments();
        await checkRelayDuplicate(true);
      }
    });
  }
  const carrier = document.getElementById('relay-carrier');
  if (carrier) carrier.addEventListener('change', updateRelayCarrierNote);
  const pairSelect = document.getElementById('relay-pairing');
  if (pairSelect) pairSelect.addEventListener('change', updateRelayStatusUi);
  document.getElementById('relay-add-deployment').addEventListener('click', openRelayDeploymentDialog);
  document.getElementById('relay-deployment-add').addEventListener('click', addRelayDeployment);
  for (const id of ['relay-deployment-close', 'relay-deployment-cancel']) {
    document.getElementById(id).addEventListener('click', closeRelayDeploymentDialog);
  }
  document.getElementById('relay-deployment-overlay').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) closeRelayDeploymentDialog();
  });
  document.getElementById('relay-deployment-name').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') addRelayDeployment();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.getElementById('relay-deployment-overlay').style.display === 'flex') {
      event.stopImmediatePropagation();
      closeRelayDeploymentDialog();
    }
  });
  document.getElementById('relay-root').addEventListener('change', async () => {
    await refreshRelayDeployments();
    await checkRelayDuplicate(true);
  });
  document.getElementById('relay-pool-id').addEventListener('change', () => checkRelayDuplicate(true));
  document.getElementById('relay-pool-id').addEventListener('focus', refreshRelayDeployments);
  const pairBtn = document.getElementById('relay-pair');
  if (pairBtn) pairBtn.addEventListener('click', () => { pairSelectedPeer(); });
  const unpairBtn = document.getElementById('relay-unpair');
  if (unpairBtn) unpairBtn.addEventListener('click', () => { unpairSelectedPeer(); });
  const confirmAccept = document.getElementById('relay-confirm-accept');
  if (confirmAccept) confirmAccept.addEventListener('click', () => answerRelayConfirm(true));
  const confirmIgnore = document.getElementById('relay-confirm-ignore');
  if (confirmIgnore) confirmIgnore.addEventListener('click', () => answerRelayConfirm(false));
  const confirmClose = document.getElementById('relay-confirm-close');
  if (confirmClose) confirmClose.addEventListener('click', () => answerRelayConfirm(false));

  // 配置弹窗：关闭方式与诊断弹窗一致（✕ / 点遮罩 / Esc）。
  const configClose = document.getElementById('relay-config-close');
  if (configClose) configClose.addEventListener('click', closeRelayConfigDialog);
  const configOverlay = document.getElementById('relay-config-overlay');
  configOverlay?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && relayConfigDialogOpen()) closeRelayConfigDialog();
  });
  // 表单一动就重算「未生效的改动」，用户能马上看出自己还没保存。
  // 本机标识不在这里：它已经不是输入项（自动生成，见 renderRelayPool）。
  const watched = [
    'relay-enabled', 'relay-role', 'relay-carrier', 'relay-root',
    'relay-pool-id', 'relay-pairing-required',
    'relay-subcommands', 'relay-allow-shell', 'relay-managed-serials',
  ];
  for (const id of watched) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.addEventListener('input', renderRelayEffective);
    el.addEventListener('change', renderRelayEffective);
  }
}
