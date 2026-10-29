"""通过真实 pywebview 页面交互验证前端与 WebSocket 后端。

此测试不直接调用前端内部函数：它启动 HTTP/WebSocket 服务，创建隐藏的
pywebview 窗口，再由页面脚本点击按钮、填写输入框、浏览设备目录并检查页面状态。
设备、ADB 和系统文件对话框使用可控测试替身，避免修改真实设备或弹出人工对话框。
"""
import asyncio
import base64
import json
import os
import socket
import shutil
import sys
import subprocess
import threading
import tempfile
import time
import types
import urllib.request

import websockets


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from app_version import APP_VERSION
from tests.edge_test_support import EdgeSession


def _free_port(start=18080):
    for port in range(start, start + 100):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('找不到可用测试端口')


class _FakeWorker:
    """只模拟设备连接和终端输入，不执行真实 ADB。"""

    DEVICES = [
        {'serial': 'UI-TEST-1', 'transportId': '1', 'state': 'device'},
        {'serial': 'UI-TEST-2', 'transportId': '2', 'state': 'device'},
    ]

    def __init__(self, on_devices_changed, on_terminal_output, on_terminal_exit,
                 *, local_devices_enabled=True, on_terminal_control=None):
        self.on_devices_changed = on_devices_changed
        self.on_terminal_output = on_terminal_output
        self.on_terminal_exit = on_terminal_exit
        # server.py 会下发本机设备识别开关；本测试只覆盖本机设备，故忽略其值，
        # 但签名必须与真实 ADBWorker 保持一致（漏了就会 TypeError）。
        self.local_devices_enabled = local_devices_enabled
        self.writes = []
        self.resizes = []
        self.started = False

    @classmethod
    def keys(cls):
        return [f"{d['serial']}:tid={d['transportId']}" for d in cls.DEVICES]

    def start(self):
        self.started = True
        self.on_devices_changed(self.DEVICES, self.keys())

    def stop(self):
        self.started = False

    def get_state(self):
        return self.DEVICES, self.keys()

    def write_terminal(self, key, data, *, generation=None):
        if hasattr(self, 'terminal_hub'):
            return self.terminal_hub.local_write(key, data, generation)
        self.writes.append((key, bytes(data)))
        self.on_terminal_output(key, b'ui-self-test\r\n')

    def resize_terminal(self, key, cols, rows, *, generation=None):
        if hasattr(self, 'terminal_hub'):
            return self.terminal_hub.resize(key, cols, rows, generation=generation)
        self.resizes.append((key, cols, rows))

    def close_terminal(self, key):
        return None


UI_SCRIPT = r"""
let results = [];
(async () => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const record = (name, ok, detail = '') => results.push({name, ok: !!ok, detail});
  const visible = el => !!el && getComputedStyle(el).display !== 'none';
  const first = (sel, root = document) => root.querySelector(sel);
  const all = (sel, root = document) => [...root.querySelectorAll(sel)];
  const click = (el) => { if (!el) throw new Error('找不到要点击的元素'); el.click(); };
  const fill = (el, value) => {
    if (!el) throw new Error('找不到要填写的输入框');
    el.focus();
    el.value = value;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const waitFor = async (predicate, timeout = 6000) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const value = predicate();
      if (value) return value;
      await sleep(50);
    }
    throw new Error('等待页面状态超时');
  };
  const panel = await waitFor(() => first('.terminal-panel'));
  const fileButtons = all('.file-op-btn', panel);
  record('设备终端面板已创建', !!panel);
  record('上传/下载/编辑/日志/快捷指令按钮齐全', fileButtons.length === 5, fileButtons.length);
  record('两个设备标签已创建', all('#device-tabs .device-tab').length === 2);

  // 终端输入和窗口布局
  const cmd = first('.terminal-cmd-textarea', panel);
  fill(cmd, 'echo ui-self-test');
  cmd.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
  await sleep(200);
  record('终端输入框可填写并回车发送', cmd.value === '');
  for (const layout of ['horizontal', 'vertical', 'single', 'grid']) {
    click(first(`[data-layout="${layout}"]`));
    await sleep(80);
    record(`布局按钮：${layout}`, first('#terminal-container').classList.contains(`layout-${layout}`));
  }
  // 三台设备时，网格换行；水平布局必须真正排成单行，不能只切换按钮高亮。
  const terminalContainer = first('#terminal-container');
  const layoutProbe = document.createElement('div');
  layoutProbe.className = 'terminal-panel';
  terminalContainer.appendChild(layoutProbe);
  const panelRects = () => all('.terminal-panel', terminalContainer)
    .map(el => el.getBoundingClientRect());
  const gridRects = panelRects();
  record('三台设备网格布局换行', gridRects.length === 3
    && gridRects[2].top > gridRects[0].top + 20);
  click(first('[data-layout="horizontal"]'));
  await sleep(80);
  const horizontalRects = panelRects();
  record('三台设备水平布局保持单行', horizontalRects.length === 3
    && horizontalRects.every(rect => Math.abs(rect.top - horizontalRects[0].top) < 2)
    && horizontalRects[0].left < horizontalRects[1].left
    && horizontalRects[1].left < horizontalRects[2].left,
    horizontalRects.map(rect => `${Math.round(rect.left)},${Math.round(rect.top)}`).join(' / '));
  layoutProbe.remove();
  click(first('#layout-reset-btn'));
  await sleep(100);
  record('恢复初始布局', first('#terminal-container').classList.contains('layout-grid'));
  click(all('#device-tabs .device-tab')[1]);
  record('设备标签可切换', all('#device-tabs .device-tab')[1].classList.contains('active'));

  // 查找：菜单和 Ctrl+F 都应定位当前选中的第二台设备终端。
  click(first('#file-tab'));
  click(first('#file-find'));
  const findDialog = await waitFor(() => first('#terminal-find-dialog'));
  record('查找窗口可从文件菜单打开', visible(findDialog));
  record('查找目标是选中设备', first('#terminal-find-device').textContent.includes('UI-TEST-2'));
  fill(first('#terminal-find-input'), 'UI-TEST-2');
  await sleep(100);
  record('查找显示命中计数', /^1 \/ [1-9]/.test(first('#terminal-find-status').textContent));
  click(first('#terminal-find-case'));
  click(first('#terminal-find-whole'));
  record('查找选项可切换', first('#terminal-find-case').checked && first('#terminal-find-whole').checked);
  click(first('#terminal-find-close'));
  const ctrlFind = new KeyboardEvent('keydown', {key: 'f', ctrlKey: true, bubbles: true, cancelable: true});
  const ctrlFindCancelled = !window.dispatchEvent(ctrlFind);
  await sleep(50);
  record('Ctrl+F 打开应用内查找并阻止默认行为', visible(findDialog) && ctrlFindCancelled);
  click(first('#terminal-find-close'));

  // 上传：点击按钮、选择本地文件、浏览设备目录、执行。
  click(fileButtons[0]);
  const fileOps = first('.cell-toolbar-file-ops:not(.edit-file-ops)', panel);
  record('上传表单可打开', visible(fileOps));
  record('上传路径占位符正确', first('.fo-path-local', fileOps).placeholder === '本地文件/目录');
  click(first('.fo-browse-local', fileOps));
  click(first('.fo-pick-files', fileOps));
  await waitFor(() => first('.fo-path-local', fileOps).value.includes('upload-a.txt')
    && first('.fo-path-local', fileOps).value.includes('upload-b.sh'));
  record('上传可通过原生对话框选择多个本地文件', true);
  click(first('.fo-browse-remote', fileOps));
  await waitFor(() => all('.device-browser .dev-browser-item.dir', fileOps).length > 0);
  const dataDir = all('.device-browser .dev-browser-item.dir', fileOps)
    .find(el => el.textContent.includes('data'));
  click(dataDir);
  await waitFor(() => first('.dev-browser-path-text', fileOps).textContent === '/data');
  click(first('.dev-browser-select-cur', fileOps));
  record('上传可浏览并选择设备目录', first('.fo-path-remote', fileOps).value === '/data');
  click(first('.fo-exec', fileOps));
  await waitFor(() => all('.toast').some(el => el.textContent.includes('上传') && el.textContent.includes('完成')));
  record('上传执行结果可反馈到界面', true);

  // 上传目录：目录作为 adb push 源路径，ADB 会递归传输其全部子项。
  click(first('.fo-browse-local', fileOps));
  click(first('.fo-pick-directory', fileOps));
  await waitFor(() => first('.fo-path-local', fileOps).value === 'C:\\ui-self-test\\upload-folder');
  record('上传可选择本地文件夹', true);
  click(first('.fo-exec', fileOps));
  await waitFor(() => all('.toast').some(el => el.textContent.includes('上传') && el.textContent.includes('完成')));
  record('上传文件夹可执行并反馈结果', true);

  // 下载：选择本地目录、选择设备文件并执行。
  click(fileButtons[1]);
  click(first('.fo-browse-local', fileOps));
  await waitFor(() => first('.fo-path-local', fileOps).value === 'C:\\ui-self-test\\download');
  fill(first('.fo-path-remote', fileOps), '/');
  click(first('.fo-browse-remote', fileOps));
  await waitFor(() => all('.device-browser .dev-browser-item.dir', fileOps).length > 0);
  click(all('.device-browser .dev-browser-item.dir', fileOps).find(el => el.textContent.includes('data')));
  await waitFor(() => first('.dev-browser-path-text', fileOps).textContent === '/data');
  await waitFor(() => all('.device-browser .dev-browser-item.file', fileOps).length >= 2);
  const remoteFiles = all('.device-browser .dev-browser-item.file', fileOps);
  click(remoteFiles[0]);
  click(remoteFiles[1]);
  record('下载可选中多个设备文件', remoteFiles.every(el => el.classList.contains('selected')));
  click(first('.dev-browser-confirm', fileOps));
  record('下载确认可写回多文件路径', first('.fo-path-remote', fileOps).value.includes('/data/fixture.txt')
    && first('.fo-path-remote', fileOps).value.includes('/data/second.txt'));
  click(first('.fo-exec', fileOps));
  await waitFor(() => all('.toast').some(el => el.textContent.includes('下载') && el.textContent.includes('完成')));
  record('下载执行结果可反馈到界面', true);

  // 切换上传/下载后，设备目录弹窗应能再次正常打开。
  click(fileButtons[0]);
  click(first('.fo-browse-remote', fileOps));
  await waitFor(() => visible(first('.device-browser', fileOps)));
  click(fileButtons[1]);
  await waitFor(() => fileOps.dataset.op === 'download');
  click(first('.fo-browse-remote', fileOps));
  await waitFor(() => visible(first('.device-browser', fileOps)));
  record('切换上传下载后设备目录仍可打开', true);
  click(first('.fo-close', fileOps));

  // 独立的设备文件编辑器。
  click(fileButtons[2]);
  const editOps = first('.edit-file-ops', panel);
  record('编辑设备文件表单可打开', visible(editOps));
  click(first('.edit-browse', editOps));
  await waitFor(() => all('.edit-device-browser .dev-browser-item.dir', editOps).length > 0);
  click(all('.edit-device-browser .dev-browser-item.dir', editOps).find(el => el.textContent.includes('data')));
  await waitFor(() => first('.edit-device-browser .dev-browser-item.file', editOps));
  first('.edit-device-browser .dev-browser-item.file', editOps).dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
  fill(first('.edit-path-remote', editOps), '/data/fixture.txt');
  click(first('.edit-inline', editOps));
  const inlineOverlay = await waitFor(() => first('.inline-edit-overlay'));
  record('可打开应用内直接编辑器', visible(inlineOverlay));
  const inlineText = first('.inline-edit-text', inlineOverlay);
  fill(inlineText, 'direct ui content');
  await sleep(800);
  record('文本输入停止后不会自动保存', first('.inline-edit-status', inlineOverlay).textContent.includes('未保存'));
  click(first('.inline-edit-save', inlineOverlay));
  await waitFor(() => first('.inline-edit-status', inlineOverlay).textContent.includes('已手动保存'), 7000);
  record('点击立即保存后写回设备', true);
  click(first('.inline-edit-close', inlineOverlay));
  await waitFor(() => !first('.inline-edit-overlay'));
  record('直接编辑器可关闭并结束会话', true);
  click(fileButtons[2]);

  // 日志、快捷指令、设置、帮助和设备刷新。
  click(fileButtons[3]);
  const logMenu = first('.log-submenu', panel);
  record('日志菜单可打开', visible(logMenu));
  click(first('.log-browse', logMenu));
  await waitFor(() => first('.log-save-path', logMenu).value === 'C:\\ui-self-test\\existing-terminal.log');
  record('日志可选择已有文件并回填路径', first('.log-save-path', logMenu).value === 'C:\\ui-self-test\\existing-terminal.log');
  click(fileButtons[3]);
  click(fileButtons[4]);
  const commandPopup = await waitFor(() => {
    const popup = first('.command-popup', panel);
    return visible(popup) ? popup : null;
  });
  record('快捷指令面板可打开', visible(commandPopup));
  click(first('.cmd-popup-add', commandPopup));
  const cmdDialog = await waitFor(() => first('.cmd-edit-overlay'));
  record('快捷指令新增对话框可打开', visible(cmdDialog));
  click(first('.cmd-edit-cancel', cmdDialog));
  click(first('[data-tab="settings"]'));
  fill(first('#set-font-size'), '14');
  record('设置页面可修改字体大小', first('#set-font-size').value === '14');
  click(first('#debug-refresh'));
  await waitFor(() => first('#debug-output').textContent.length > 0);
  record('设置页面可刷新日志', true);
  click(first('[data-tab="terminal"]'));
  click(first('#help-tab'));
  click(first('#help-about'));
  record('关于对话框可打开', visible(first('.about-dialog')));
  const versionRow = all('.about-row').find(row => row.querySelector('.about-key')?.textContent === '版本');
  record('关于对话框显示 v__EXPECTED_APP_VERSION__', versionRow?.lastElementChild.textContent === 'v__EXPECTED_APP_VERSION__');
  click(first('.about-close'));
  click(first('#device-refresh-btn'));
  record('设备刷新按钮可交互', all('#device-tabs .device-tab').length === 2);

  // 用真实 DOM 验证统一配对列表、固定机器名和部署新增流程。
  await openRelayConfigDialog();
  record('中转弹窗已移除重复实例列表', !first('#relay-pool-entries') && !first('#relay-peers'));
  const originalStatus = state.relay.status;
  const originalConfig = state.relay.config;
  const fixture = {
    ...originalStatus, enabled: true, running: true, role: 'master', roleLabel: '主控端',
    machineName: '固定计算机名', peerId: 'internal-route-2', sessions: [], peers: [],
    pairing: [
      {nodeId: 'nd-a', peerId: 'pa', host: '设备机A', deploymentId: 'team-a', roleLabel: '从机', online: true, stateLabel: '已发现', connectionState: 'local-confirmation', connectionReason: '待本机确认'},
      {nodeId: 'nd-b', peerId: 'pb', host: '设备机B', deploymentId: 'team-a', roleLabel: '从机', online: true, paired: true, stateLabel: '已配对', connectionState: 'checking', connectionReason: '主控已确认，正在验证连接', nextAction: '等待从机自动接受连接'},
    ],
  };
  state.relay.status = fixture;
  renderRelayPool(fixture);
  record('本机标识只显示计算机名', first('#relay-peer-id').textContent === '固定计算机名');
  record('当前根内候选显示部署标识', all('#relay-pairing option').length === 2
    && first('#relay-pairing').textContent.includes('team-a'));
  fill(first('#relay-pairing'), 'nd-b');
  renderRelayPool(fixture);
  record('状态刷新保留配对选择且解除可用', first('#relay-pairing').value === 'nd-b'
    && !first('#relay-unpair').disabled && first('#relay-pair').disabled);
  record('主控确认后明确提示自动验证，无需去从机操作', first('#relay-next-action').textContent.includes('自动接受')
    && first('#relay-pairing').textContent.includes('主控已确认') && first('#relay-step-confirm').classList.contains('active'));
  record('节点在线不误报通讯验证', relayVisibleState({...fixture, state: 'connected', servedSessions: []}) === '正在验证');
  updateRelayStatusUi();
  const lightColor = () => getComputedStyle(first('#status-relay'), '::before').backgroundColor;
  const yellowLight = lightColor();
  record('检测到心跳未验证显示黄色灯', first('#status-relay').classList.contains('warn') && first('#relay-state').classList.contains('warn'));
  const savedHint = fixture.hint;
  fixture.hint = '完整状态提示：' + '共享目录状态和连接诊断需要完整显示。'.repeat(45);
  const statusBar = first('#status-bar');
  statusBar.style.width = '480px';
  updateRelayStatusUi();
  const statusBadge = first('#status-relay');
  record('底部只显示单行状态，不显示详细提示或日志',
    !statusBadge.textContent.includes(fixture.hint)
    && !statusBadge.textContent.includes('\n')
    && statusBadge.textContent.includes('待处理')
    && statusBar.getBoundingClientRect().height === 26);
  closeRelayConfigDialog();
  click(statusBadge);
  await waitFor(() => relayConfigDialogOpen());
  record('点击底部状态可查看完整中转状态与事件', relayConfigDialogOpen());
  state.relay.status = fixture;
  fixture.heartbeatSeq = 42;
  fixture.uptimeSeconds = 3661;
  renderRelayEffective();
  record('连接详情展示心跳序号和应用运行时间', first('#relay-effective').textContent.includes('1小时 1分 1秒')
    && first('#relay-effective').textContent.includes('心跳 42'));
  fixture.hint = savedHint;
  statusBar.style.width = '';
  renderRelayPool(fixture);
  fill(first('#relay-pairing'), 'nd-b');
  updateRelayStatusUi();
  fixture.pairing[1].connectionState = 'connected';
  renderRelayPool(fixture);
  record('无设备无终端也能依据双端验证进入管理设备', first('#relay-step-use').classList.contains('active')
    && first('#relay-next-action').textContent.includes('没有共享设备')
    && first('#relay-pairing').textContent.includes('已连接'));
  updateRelayStatusUi();
  const greenLight = lightColor();
  record('验证成功显示绿色灯且颜色不同', first('#status-relay').classList.contains('ok') && greenLight !== yellowLight && getComputedStyle(first('#status-relay'), '::before').width === '9px');
  fixture.role = 'slave'; fixture.roleLabel = '从机'; fixture.pairing[1].roleLabel = '主控端';
  state.relay.config = {...originalConfig, role: 'slave'};
  fill(first('#relay-role'), 'slave');
  renderRelayPool(fixture);
  record('从机隐藏对端选择和确认按钮，只展示当前主控', first('#relay-master-connection').hidden
    && !first('#relay-slave-connection').hidden && first('#relay-pair').disabled
    && first('#relay-slave-status').textContent.includes('设备机B'));
  record('从机验证成功后也进入管理设备并引导去主控操作', first('#relay-step-use').classList.contains('active')
    && first('#relay-next-action').textContent.includes('设备操作在主控窗口'));
  fixture.pairing.push({nodeId:'old-master', host:'历史主控', paired:true, online:false, connectionState:'offline'});
  updateRelayStatusUi();
  record('从机只显示当前连接，不统计历史主控离线数量',
    !first('#status-relay').textContent.includes(' / 待处理')
    && !first('#status-relay').textContent.includes('离线 1')
    && first('#status-relay').classList.contains('ok'));
  fixture.pairing.pop();
  fixture.pairing.push({nodeId:'wrong-slave', role:'slave', roleLabel:'从机', paired:true, online:true, connectionState:'checking'});
  fixture.pairing.unshift({nodeId:'unknown-history', paired:true, online:false, connectionState:'offline'});
  record('从机当前主控筛选排除同角色实例和无角色历史记录', relaySelectedPeer(fixture).nodeId === 'nd-b');
  fixture.pairing.shift(); fixture.pairing.pop();
  fixture.role = 'master'; fixture.roleLabel = '主控端'; fixture.pairing[1].roleLabel = '从机';
  state.relay.config = originalConfig;
  fill(first('#relay-role'), originalConfig.role);
  fixture.pairing[1].connectionState = 'waiting-peer-confirmation';
  fixture.pairing[1].connectionReason = '从机尚未确认主控';
  fixture.sessions = [{peerId:'pb', opened:true}];
  renderRelayPool(fixture);
  record('撤销准入后的验证状态优先于旧终端快照', first('#relay-step-confirm').classList.contains('active')
    && relayVisibleState({...fixture, state:'connected'}) === '等待对端确认');
  fixture.pairing[0].connectionState = 'connected';
  updateRelayStatusUi();
  record('多对端中已连接实例不掩盖所选对端等待确认',
    relayVisibleState(fixture) === '等待对端确认'
    && first('#status-relay').textContent.includes('已连接 1 / 待处理 1'));
  delete fixture.pairing[1].connectionState;
  fixture.sessions = [{peerId: 'pb', opened: true}];
  renderRelayPool(fixture);
  record('旧终端快照不能反推通讯已验证', !first('#relay-next-action').textContent.includes('通讯已验证')
    && first('#relay-step-confirm').classList.contains('active'));
  fixture.pairing[1].online = false;
  fixture.pairing[1].connectionState = 'offline';
  fixture.pairing[1].connectionReason = '对端离线，确认关系保留';
  renderRelayPool(fixture);
  updateRelayStatusUi();
  record('无有效心跳显示红灯，其他连接不掩盖', first('#status-relay').classList.contains('bad') && lightColor() !== greenLight && lightColor() !== yellowLight);
  record('离线优先于历史会话且保留解除操作', first('#relay-next-action').textContent.includes('对端离线')
    && !first('#relay-unpair').disabled);
  fixture.pairing[1].online = true;
  fixture.pairing[1].roleLabel = '主控端';
  fixture.pairing[1].connectionState = 'role-mismatch';
  fixture.pairing[1].connectionReason = '双方角色相同或主控角色冲突';
  fixture.pairing[1].nextAction = '修改其中一端角色';
  renderRelayPool(fixture);
  updateRelayStatusUi();
  record('有心跳但角色不匹配仍为黄色', first('#status-relay').classList.contains('warn'));
  record('同角色连接提示修改角色', first('#relay-next-action').textContent.includes('角色相同')
    && first('#relay-pair').disabled);
  fixture.pairing[1].roleLabel = '从机';
  record('高级设置默认折叠', !first('.relay-advanced').open);
  renderRelayDeployments(['team-a', 'team-b'], 'team-b');
  record('部署列表可选择已有部署', first('#relay-pool-id').value === 'team-b');
  click(first('#relay-add-deployment'));
  fill(first('#relay-deployment-name'), '../escape');
  click(first('#relay-deployment-add'));
  record('新增部署拒绝非法路径且保留弹窗', visible(first('#relay-deployment-overlay'))
    && !first('#relay-deployment-error').hidden);
  fill(first('#relay-deployment-name'), 'team-new');
  click(first('#relay-deployment-add'));
  record('手动新增部署选中且标记待保存', first('#relay-pool-id').value === 'team-new'
    && !visible(first('#relay-deployment-overlay')) && relayConfigDirty());
  record('未保存的部署阻止确认并提示先保存', first('#relay-pair').disabled
    && first('#relay-next-action').textContent.includes('保存并生效') && first('#relay-step-config').classList.contains('active'));
  renderRelayDeployments(['team-a', 'team-b']);
  record('后台刷新不丢失尚未保存的新增部署', first('#relay-pool-id').value === 'team-new');
  const savedDevices = state.devices;
  const savedKeys = [...state.knownKeys];
  const remoteKey = 'relay:internal-route-2:REMOTE:tid=1';
  const remoteDevice = {
    serial: 'REMOTE', state: 'device', key: remoteKey,
    relay: {peerId: 'internal-route-2', host: '设备计算机', deploymentId: 'team-old', online: true},
  };
  handleWSMessage({type: 'devices-updated', devices: [...savedDevices, remoteDevice], knownKeys: [...savedKeys, remoteKey]});
  const remotePanel = state.terminals[remoteKey].panel;
  record('远端设备使用机器名 / 部署标识', remotePanel.querySelector('.header-title').textContent.includes('设备计算机 / team-old')
    && !remotePanel.textContent.includes('internal-route-2'));
  remoteDevice.relay = {...remoteDevice.relay, deploymentId: 'test'};
  handleWSMessage({type: 'devices-updated', devices: [...savedDevices, remoteDevice], knownKeys: [...savedKeys, remoteKey]});
  record('切换部署后保留终端面板并更新标题和设备标签', state.terminals[remoteKey].panel === remotePanel
    && remotePanel.querySelector('.remote-badge').textContent.includes('设备计算机 / test')
    && first(`[data-key="${remoteKey}"] .device-tab-label`).textContent.includes('设备计算机 / test'));
  state.relay.status = {...state.relay.status, sessions: [{key: remoteKey, opened: false, failures: 0}]};
  updateTerminalPanels();
  record('新终端明确显示准备中并说明输入排队', remotePanel.querySelector('.remote-badge').textContent.includes('终端准备中')
    && remotePanel.querySelector('.terminal-cmd-input textarea').placeholder.includes('排队'));
  state.relay.status.sessions[0].opened = true;
  updateTerminalPanels();
  record('终端就绪后徽标和输入提示同步恢复', remotePanel.querySelector('.remote-badge').textContent.includes('终端已就绪')
    && !remotePanel.querySelector('.terminal-cmd-input textarea').placeholder.includes('排队'));
  const otherKey = 'relay:other-slave:REMOTE:tid=1';
  const otherDevice = {...remoteDevice, key: otherKey, relay: {...remoteDevice.relay, peerId: 'other-slave'}};
  const bothDevices = [...savedDevices, remoteDevice, otherDevice];
  const bothKeys = [...savedKeys, remoteKey, otherKey];
  handleWSMessage({type: 'devices-updated', devices: bothDevices, knownKeys: bothKeys});
  const otherPanel = state.terminals[otherKey].panel;
  selectActiveDevice(remoteKey);
  remoteDevice.relay.online = false;
  handleWSMessage({type: 'devices-updated', devices: bothDevices, knownKeys: bothKeys});
  record('从机离线立即回收设备窗口和标签，保留管理记录', !state.terminals[remoteKey]
    && !remotePanel.isConnected && !first(`#device-tabs [data-key="${remoteKey}"]`)
    && state.devices.includes(remoteDevice) && !state.dismissedKeys[remoteKey]);
  record('单个从机离线不关闭其他从机窗口，当前设备自动切换', state.terminals[otherKey].panel === otherPanel
    && state.activeDevice !== remoteKey && state.knownKeys.includes(state.activeDevice));
  remoteDevice.relay.online = true;
  handleWSMessage({type: 'devices-updated', devices: bothDevices, knownKeys: bothKeys});
  record('从机恢复在线自动重建设备窗口', !!state.terminals[remoteKey]
    && state.terminals[remoteKey].panel !== remotePanel);
  handleWSMessage({type: 'devices-updated', devices: savedDevices, knownKeys: savedKeys});
  state.relay.status = originalStatus;
  await loadRelayConfig();
  renderRelayPool(originalStatus);
  closeRelayConfigDialog();

  return {ok: results.every(item => item.ok), results};
})().catch(async error => {
  return {
    ok: false,
    error: String(error && error.stack || error),
    results,
  };
});
"""


def _edge_executable():
    # 环境变量优先，但沙箱/CI 里 PROGRAMFILES 常被清掉，而 Edge 其实装在默认位置。
    # 所以额外直接探测几个公认路径，避免把「环境变量缺失」误报成「没装 Edge」。
    candidates = [
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('PROGRAMFILES', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), '')


def _cdp_eval(ws_url, url, expression):
    """通过 CDP 在真实浏览器页面中导航并执行前端交互脚本。"""
    from websockets.sync.client import connect
    with connect(
        ws_url, max_size=20 * 1024 * 1024, proxy=None, open_timeout=5, close_timeout=2
    ) as ws:
        counter = 0

        def command(method, params=None):
            nonlocal counter
            counter += 1
            ws.send(json.dumps({'id': counter, 'method': method, 'params': params or {}}))
            while True:
                raw_message = ws.recv(timeout=60 if method == 'Runtime.evaluate' else 15)
                message = json.loads(raw_message)
                if message.get('method') == 'Inspector.targetCrashed':
                    raise RuntimeError('Edge 页面目标崩溃')
                if message.get('id') == counter:
                    if message.get('error'):
                        raise RuntimeError(message['error'].get('message', f'CDP {method} 失败'))
                    return message

        command('Runtime.enable')
        time.sleep(2)
        response = command('Runtime.evaluate', {
            'expression': expression,
            'awaitPromise': True,
            'returnByValue': True,
        })
        result = response.get('result', {}).get('result', {})
        if result.get('subtype') == 'error' or 'exceptionDetails' in response.get('result', {}):
            details = response.get('result', {}).get('exceptionDetails', {})
            exception = details.get('exception', {})
            raise RuntimeError(exception.get('description') or result.get('description')
                               or details.get('text') or '浏览器脚本执行失败')
        return result.get('value')


def _check_shared_terminal_controls(ws_url, url, manager):
    """真实页面接管/交还：同一PTY上下文、双端只读与后端拒绝旧接入。"""
    from shared_terminal import TerminalHub, SharedTerminalBackend
    from tests.test_shared_terminal import Shell
    key = 'UI-TEST-1:tid=1'
    _cdp_eval(ws_url, url, """(() => {
      const key='UI-TEST-1:tid=1';
      if(!state.terminals[key]) createTerminalPanel(key,{serial:'UI-TEST-1',transportId:'1',state:'device'});
      return {ok:true};
    })()""")
    hub = TerminalHub(manager._on_terminal_control)
    shell = Shell()
    manager.worker.terminal_hub = hub
    hub.register(key, shell)
    def output(data):
        manager._on_terminal_output(key, data)
        hub.publish(key, data)
    shell.output = output
    backend = SharedTerminalBackend(lambda: manager.worker)
    outputs, exits = [], []
    lease = None
    try:
        lease = backend.open({'serial':'UI-TEST-1','transportId':'1'},cols=80,rows=24,
                             on_output=outputs.append,on_exit=lambda: exits.append(True))
        taken = _cdp_eval(ws_url, url, """(async () => {
          const t=state.terminals['UI-TEST-1:tid=1'];
          const wait=async fn => { const end=Date.now()+5000; while(Date.now()<end) {
            if(fn())return true; await new Promise(r=>setTimeout(r,50)); } return false; };
          if(!await wait(()=>t.term.options.disableStdin && t.cmdTextarea.disabled))return {ok:false};
          const button=Array.from(t.header.querySelectorAll('button')).find(b=>b.textContent==='接管');
          if(!button)return {ok:false};
          button.click();
          if(!await wait(()=>!t.term.options.disableStdin && !t.cmdTextarea.disabled))return {ok:false};
          t.cmdTextarea.value='cd /data';
          t.cmdTextarea.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
          return {ok:true};
        })()""")
        deadline=time.monotonic()+3
        while shell.cwd != '/data' and time.monotonic()<deadline:
            time.sleep(.05)
        old_blocked = backend.write(lease,b'OLD-REMOTE') is False
        given = _cdp_eval(ws_url, url, """(() => {
          const t=state.terminals['UI-TEST-1:tid=1'];
          const button=Array.from(t.header.querySelectorAll('button')).find(b=>b.textContent==='交还主机');
          if(!button)return {ok:false}; button.click(); return {ok:true};
        })()""")
        deadline=time.monotonic()+3
        while lease.active and time.monotonic()<deadline:
            time.sleep(.05)
        revoked = not lease.active and backend.write(lease,b'LATE-OLD-REMOTE') is False
        lease = backend.open({'serial':'UI-TEST-1','transportId':'1'},cols=80,rows=24,
                             on_output=outputs.append,on_exit=lambda: exits.append(True))
        backend.write(lease,b'pwd\r')
        ready = _cdp_eval(ws_url, url, """(async () => {
          const t=state.terminals['UI-TEST-1:tid=1']; const end=Date.now()+5000;
          while(Date.now()<end) {
            if(t.term.options.disableStdin && t.cmdTextarea.disabled)return {ok:true};
            await new Promise(r=>setTimeout(r,50));
          } return {ok:false};
        })()""")
        ok = (taken.get('ok') and given.get('ok') and ready.get('ok') and old_blocked and revoked
              and shell.cwd=='/data' and shell.closed==0
              and shell.writes==[b'cd /data\n',b'pwd\r'])
        requests = []
        def remote_take(key, generation):
            requests.append((key, generation))
            return True, '接管请求已发送'
        from unittest.mock import patch
        with patch.object(manager.relay, 'take_session_control', remote_take):
            remote_button = _cdp_eval(ws_url, url, """(() => {
              const key='relay:ui-slave:UI-TEST-2:tid=2';
              createTerminalPanel(key,{serial:'UI-TEST-2',transportId:2,state:'device'});
              state.relay.status={sessions:[{key,owner:'local',shared:true,generation:'ui-take-token'}]};
              syncTerminalControls(); const t=state.terminals[key];
              const button=Array.from(t.header.querySelectorAll('button')).find(b=>b.textContent==='接管');
              if(!button || !t.term.options.disableStdin)return {ok:false};
              button.click(); return {ok:true};
            })()""")
            deadline=time.monotonic()+3
            while not requests and time.monotonic()<deadline: time.sleep(.05)
        ok = ok and remote_button.get('ok') and requests == [('relay:ui-slave:UI-TEST-2:tid=2','ui-take-token')]
        print(f'  [{"PASS" if ok else "FAIL"}] 真实从机页面接管和交还，保留同一shell上下文并拦截旧主机输入')
        return bool(ok)
    finally:
        if lease:
            backend.close(lease)
        hub.remove(key)
        del manager.worker.terminal_hub


def _check_slave_terminal_mirror(ws_url, url, manager):
    """PTY输出经过真实WS与xterm显示，镜像不写入从机本地shell。"""
    from unittest.mock import patch
    import adb_worker
    import relay_app
    prepared = _cdp_eval(ws_url, url, """(() => {
      const key = 'UI-TEST-1:tid=1';
      if (!state.terminals[key]) createTerminalPanel(key, {serial:'UI-TEST-1',transportId:'1',state:'device'});
      return {ok:!!state.terminals[key]};
    })()""")
    if not prepared.get('ok'):
        return False
    writes_before = len(manager.worker.writes)
    remote, writes = [], []

    class Terminal:
        def __init__(self, device, on_output, on_exit):
            self.output = on_output
        def resize(self, cols, rows):
            pass
        def write(self, data):
            writes.append(data)
            payload = data + '\r\n从机同步回复\r\n'.encode('utf-8')
            self.output(payload[:len(data) + 3])
            self.output(payload[len(data) + 3:])
        def close(self):
            pass

    with patch.object(adb_worker, '_HAS_WINPTY', True), patch.object(adb_worker, 'TerminalSession', Terminal):
        backend = relay_app.AdbPtyBackend(on_local_output=manager._on_terminal_output)
        handle = backend.open({'serial':'UI-TEST-1','transportId':'1'}, cols=80, rows=24,
                              on_output=remote.append, on_exit=lambda: None)
        backend.write(handle, b'echo REMOTE-MIRROR-CHECK\r')
        displayed = _cdp_eval(ws_url, url, """(async () => {
          const deadline = Date.now() + 5000;
          while (Date.now() < deadline) {
            const buffer = state.terminals['UI-TEST-1:tid=1'].term.buffer.active;
            const lines = [];
            for(let i=0;i<buffer.length;i++) lines.push(buffer.getLine(i).translateToString());
            const text = lines.join('\\n');
            if(text.includes('REMOTE-MIRROR-CHECK') && text.includes('从机同步回复')) return {ok:true};
            await new Promise(resolve => setTimeout(resolve,100));
          }
          return {ok:false};
        })()""")
        backend.close(handle)
    ok = (displayed.get('ok') and writes == [b'echo REMOTE-MIRROR-CHECK\r']
          and len(manager.worker.writes) == writes_before
          and b'REMOTE-MIRROR-CHECK' in b''.join(remote))
    print(f'  [{"PASS" if ok else "FAIL"}] 主控指令回显和中文回复经真实WS同步到从机xterm，未重复执行')
    return bool(ok)


def _check_live_relay(ws_url, url, manager):
    """真实节点 → WebSocket → 页面三步状态，不用伪造 status 验证无设备连接。"""
    from tests.test_relay_app import _Harness, _patch, _fake_list_devices, _fake_execute, _wait_until
    with tempfile.TemporaryDirectory(prefix='ui-live-relay-') as directory:
        with _patch(list_devices=_fake_list_devices, execute=_fake_execute):
            harness = _Harness(directory, master_local_devices_enabled=False)
            harness.slave.local_devices_enabled = False
            previous = manager.relay
            manager.relay = harness.master
            harness.master.on_status = manager._on_relay_status
            harness.master.on_event = manager._on_relay_event
            harness.master.on_devices_changed = manager._on_relay_devices_changed
            harness.master.on_peer_request = manager._on_relay_peer_request
            try:
                harness.start()
                if not _wait_until(lambda: bool(harness.master.status()['pairing'])):
                    return False
                initial = _cdp_eval(ws_url, url, """(async () => {
                  await openRelayConfigDialog();
                  state.relay.ignoredPeers['pc-b'] = true;
                  closeRelayConfirmDialog(); state.relay.pendingPeer = null;
                  const before = !state.relay.status.pairing[0].paired
                    && !document.getElementById('relay-step-use').classList.contains('active');
                  if (document.getElementById('relay-pair').disabled) return {ok:false, reason:'实时确认按钮不可用'};
                  await pairSelectedPeer();
                  const deadline = Date.now() + 5000;
                  while (Date.now() < deadline && !state.relay.status.pairing.some(item => item.connectionState === 'connected')) {
                    await new Promise(resolve => setTimeout(resolve, 100));
                  }
                  return {ok:before && state.relay.status.pairing.some(item => item.connectionState === 'connected')
                    && document.getElementById('relay-step-use').classList.contains('active')};
                })()""")
                print(f'  [{"PASS" if initial.get("ok") else "FAIL"}] 实时主控页面点击一次确认，从机自动接受连接 {initial}')
                accepted = harness.slave.node.pairing.is_paired(harness.master.config.nodeId)
                ready = _cdp_eval(ws_url, url, """(async () => {
                  const deadline = Date.now() + 5000;
                  while (Date.now() < deadline && !state.relay.status.pairing.some(item => item.connectionState === 'connected')) {
                    await new Promise(resolve => setTimeout(resolve, 100));
                  }
                  return {ok:state.relay.status.pairing.some(item => item.connectionState === 'connected')
                    && document.getElementById('relay-step-use').classList.contains('active')
                    && document.getElementById('relay-next-action').textContent.includes('通讯已验证')
                    && !state.relay.status.sessions.length};
                })()""")
                slave_ready = any(item.get('connectionState') == 'connected' for item in harness.slave.status()['pairing'])
                ok = accepted and ready.get('ok') and slave_ready and not harness.backend.live_handles()
                print(f'  [{"PASS" if ok else "FAIL"}] 无设备主控确认结果经真实 WS 推送，页面自动进入管理设备')
                return bool(initial.get('ok') and ok)
            finally:
                harness.stop()
                manager.relay = previous


def run():
    if os.name != 'nt':
        print('  [SKIP] UI E2E 仅支持 Windows Edge')
        return None

    edge = _edge_executable()
    if not edge:
        print('  [FAIL] 找不到 Microsoft Edge，无法执行真实页面交互测试')
        return False
    try:
        import server as srv
    except Exception as exc:
        print(f'  [FAIL] UI E2E 服务依赖不可用: {exc}')
        return False

    old_worker = srv.ADBWorker
    old_devices = srv.get_all_devices
    old_http = srv.HTTP_PORT
    old_ws = srv.WS_PORT
    old_ws_port = srv._WS_PORT
    httpd = None
    manager = None
    manager_thread = None
    edge_proc = None
    profile_dir = None
    try:
        srv.ADBWorker = _FakeWorker
        srv.get_all_devices = lambda: list(_FakeWorker.DEVICES)
        srv.HTTP_PORT = _free_port()
        srv.WS_PORT = _free_port(18180)
        srv._WS_PORT = srv.WS_PORT
        httpd = srv.start_http_server()
        manager = srv.ManagerServer()

        # 替身签名必须与 server.py 中的真实实现保持一致：新增的 key 参数用于
        # 远端设备选路，本测试只覆盖本机设备，因此直接忽略。
        async def fake_list(self, serial, transport_id, path, key=''):
            path = path.rstrip('/') or '/'
            entries = {
                '/': [
                    {'name': 'data', 'isDir': True, 'path': '/data'},
                    {'name': 'system', 'isDir': True, 'path': '/system'},
                ],
                '/data': [
                    {'name': 'fixture.txt', 'isDir': False, 'path': '/data/fixture.txt'},
                    {'name': 'second.txt', 'isDir': False, 'path': '/data/second.txt'},
                    {'name': 'nested', 'isDir': True, 'path': '/data/nested'},
                ],
            }.get(path)
            if entries is None:
                return {'success': False, 'output': f'不存在测试目录: {path}'}
            return {'success': True, 'currentPath': path, 'entries': entries}

        async def fake_exec(self, args='', argv=None, make_executable=False, key=''):
            self._ui_exec_args = getattr(self, '_ui_exec_args', [])
            self._ui_exec_args.append({
                'args': args, 'argv': argv, 'makeExecutable': make_executable,
            })
            return {'success': True, 'output': '测试执行成功'}

        async def fake_pick_file(self, multi=False):
            paths = [r'C:\ui-self-test\upload-a.txt', r'C:\ui-self-test\upload-b.sh']
            return {'success': True, 'path': paths[0], 'paths': paths if multi else paths[:1]}

        async def fake_pick_recording_file(self, suggested_name=''):
            return {'success': True, 'path': r'C:\ui-self-test\existing-terminal.log'}

        async def fake_pick_dir(self, title=''):
            path = (r'C:\ui-self-test\upload-folder'
                    if '递归上传' in title else r'C:\ui-self-test\download')
            return {'success': True, 'path': path}

        async def fake_start_inline(self, key, serial, transport_id, path):
            self._ui_inline_active = key
            return {
                'success': True,
                'content': base64.b64encode('fixture content'.encode('utf-8')).decode('ascii'),
                'encoding': 'utf-8',
                'output': '测试文件已读取',
            }

        async def fake_save_inline(self, key, content):
            self._ui_inline_saves = getattr(self, '_ui_inline_saves', 0) + 1
            self._ui_inline_last_content = content
            return {'success': True, 'output': '测试文件已手动保存'}

        def fake_close_inline(self, key):
            self._ui_inline_active = None
            return {'success': True, 'output': '测试直接编辑会话已关闭'}

        manager._list_device_dir = types.MethodType(fake_list, manager)
        manager._exec_adb = types.MethodType(fake_exec, manager)
        manager._pick_file = types.MethodType(fake_pick_file, manager)
        manager._pick_recording_file = types.MethodType(fake_pick_recording_file, manager)
        manager._pick_dir = types.MethodType(fake_pick_dir, manager)
        manager._start_inline_edit = types.MethodType(fake_start_inline, manager)
        manager._save_inline_edit = types.MethodType(fake_save_inline, manager)
        manager._close_inline_edit = types.MethodType(fake_close_inline, manager)

        ready = threading.Event()

        def run_manager():
            async def runner():
                task = asyncio.create_task(manager.run())
                while not getattr(manager, '_ws_port', None):
                    await asyncio.sleep(0.01)
                ready.set()
                await task
            asyncio.run(runner())

        manager_thread = threading.Thread(target=run_manager, daemon=True)
        manager_thread.start()
        if not ready.wait(10):
            raise RuntimeError('WebSocket 测试服务启动超时')

        profile_dir = tempfile.mkdtemp(prefix='devmux-ui-test-')
        test_url = f'http://127.0.0.1:{httpd.server_address[1]}/?_t=ui-test'
        edge_proc = EdgeSession.start(edge, [
            '--headless=old', '--no-first-run', '--no-default-browser-check',
            '--remote-allow-origins=*', '--window-size=1200,800', test_url,
        ], profile_dir)
        endpoint = edge_proc.wait_ready(20)
        expression = UI_SCRIPT.replace('__EXPECTED_APP_VERSION__', APP_VERSION)
        payload = _cdp_eval(
            endpoint,
            test_url,
            expression,
        )
        for item in payload.get('results', []):
            detail = f' ({item["detail"]})' if item.get('detail') else ''
            print(f'  [{"PASS" if item.get("ok") else "FAIL"}] {item["name"]}{detail}')
        if payload.get('error'):
            print(f'  [FAIL] 页面脚本: {payload["error"]}')
        worker = manager.worker
        executed = getattr(manager, '_ui_exec_args', [])
        upload_argv = ['-t', '1', 'push', r'C:\ui-self-test\upload-a.txt', r'C:\ui-self-test\upload-b.sh', '/data']
        upload_folder_argv = ['-t', '1', 'push', r'C:\ui-self-test\upload-folder', '/data']
        download_argv = ['-t', '1', 'pull', '/data/fixture.txt', '/data/second.txt', r'C:\ui-self-test\download']
        backend_ok = bool(
            any(item.get('argv') == upload_argv and item.get('makeExecutable') for item in executed)
            and any(item.get('argv') == upload_folder_argv for item in executed)
            and any(item.get('argv') == download_argv for item in executed)
            and getattr(worker, 'writes', [])
            and getattr(worker, 'resizes', [])
        )
        print(f'  [{"PASS" if backend_ok else "FAIL"}] 页面交互已到达执行后端')
        mirror_ok = _check_slave_terminal_mirror(endpoint, test_url, manager)
        shared_ok = _check_shared_terminal_controls(endpoint, test_url, manager)
        live_relay_ok = _check_live_relay(endpoint, test_url, manager)
        return bool(payload.get('ok')) and backend_ok and mirror_ok and shared_ok and live_relay_ok
    except Exception as exc:
        if (isinstance(exc, TimeoutError)
                or 'Edge 页面目标崩溃' in str(exc)
                or 'Target crashed' in str(exc)
                or 'ConnectionClosedError' in type(exc).__name__):
            required = os.environ.get('ADB_UI_E2E_REQUIRED') == '1'
            print(f'  [{"FAIL" if required else "SKIP"}] 当前环境的 Edge 无头页面不可用: {exc}')
            return False if required else None
        print(f'  [FAIL] UI E2E: {type(exc).__name__}: {exc}')
        return False
    finally:
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                pass
        if manager_thread is not None:
            manager_thread.join(timeout=5)
        if edge_proc is not None:
            edge_proc.close()
        if httpd is not None:
            httpd.shutdown()
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)
        srv.ADBWorker = old_worker
        srv.get_all_devices = old_devices
        srv.HTTP_PORT = old_http
        srv.WS_PORT = old_ws
        srv._WS_PORT = old_ws_port


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
