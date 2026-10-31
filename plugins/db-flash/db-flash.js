// 独立烧写面板：后端识别文件并执行，工具输出实时显示。
(() => {
  'use strict';
  // 标准名称用于说明镜像角色；实际烧写文件仍取右侧选定路径。
  const IMAGE_HELP = {
    spl: '设备上电时最先运行的引导程序，写入两个 eMMC 启动区。',
    gpt: '规定存储空间中各分区的名称、位置和大小。',
    env: '保存 U-Boot 启动参数的分区镜像。',
    ddr: 'DDR 初始化固件，需与板卡内存型号一致。',
    preboot: '包含 U-Boot、ATF、OP-TEE 和 SCP，负责后续系统启动。',
    boot: 'Linux 内核与板卡硬件配置（设备树）。',
    rootfs: 'Linux 基础目录、启动脚本和命令环境。',
    system: '系统程序、驱动模块、共享库及辅助处理器固件。',
    data: '用户数据分区的初始镜像；选择「烧写镜像」时使用。',
  };
  const STANDARD_FILES = {
    spl: 'spl_im_signed.img', gpt: 'gpt.bin', env: 'uboot_env.img', ddr: 'lpddr5_fw.bin',
    preboot: 'preboot.itb', boot: 'boot.itb', rootfs: 'rootfs.img', system: 'system.img',
    data: 'userdata.img',
  };
  const LEGACY_IDS = ['spl', 'gpt', 'preboot', 'boot', 'rootfs', 'system', 'data'];
  class DBFlashPanel {
    constructor(host, request) {
      this.host = host;
      this.request = request;
      this.files = [];
      this.model = null;
      this.overrides = {};
      this.busy = false;
      this.running = false;
      this.jobId = '';
      this.sequence = 0;
      this.timer = null;
      this.previousFocus = null;
      this.requestId = '';
      host.classList.add('db-flash-panel');
      host.innerHTML = `
        <header class="db-flash-header"><div><h2>DB 固化烧写</h2><p data-el="mode">本机烧写 · 实时工具输出</p></div>
          <button data-el="close" type="button" aria-label="关闭烧写窗口">关闭</button></header>
        <p data-el="banner" class="db-flash-banner">点击开始后实际写入所选分区；ALL 默认擦除用户数据。请先核对文件和操作选项。</p>
        <div data-el="target-context"></div>
        <label for="db-flash-tool-dir">工具所在目录（包含 flashall.bat 的固化包根目录）</label>
        <div class="db-flash-path-row"><input id="db-flash-tool-dir" data-el="directory" type="text" placeholder="先选择并确认工具目录">
          <button data-el="browse" type="button">浏览…</button><button data-el="confirm" type="button">确认目录</button></div>
        <p data-el="platform" class="db-flash-platform">识别平台：—</p>
        <label for="db-flash-platform-choice">平台识别</label>
        <select id="db-flash-platform-choice" data-el="platform-choice"><option value="">自动识别</option><option value="p400">手动指定 p400</option></select>
        <div class="db-flash-files-bar"><button data-el="pick" type="button">批量选择待烧写文件…</button>
          <button data-el="defaults" type="button">恢复默认 ALL</button>
          <span data-el="source">未选其他文件，使用工具目录中的默认镜像</span></div>
        <p class="db-flash-help">按文件名包含完整字段识别，忽略大小写。可修改勾选和文件对应关系；候选文件冲突必须手动选择。</p>
        <div data-el="issues" class="db-flash-issues"></div>
        <label class="db-flash-all"><input data-el="all" type="checkbox" checked> ALL · 全选烧写项（仍可逐项修改）</label>
        <div class="db-flash-table-scroll">
          <table class="db-flash-table">
            <caption>可独立烧写的文件清单</caption>
            <colgroup><col class="db-flash-col-check"><col class="db-flash-col-name"><col class="db-flash-col-description"><col></colgroup>
            <thead><tr><th scope="col">勾选</th><th scope="col">标准文件名</th><th scope="col">功能说明</th><th scope="col">实际烧写文件</th></tr></thead>
            <tbody data-el="items"></tbody>
          </table>
        </div>
        <fieldset class="db-flash-options"><legend>烧写前后操作（均可修改）</legend>
          <div class="db-flash-option">
            <label><input data-el="prepare" type="checkbox" checked> 准备 USB 烧写环境</label>
            <code data-el="prepare-files">spl_im_signed.img + u-boot.bin</code>
            <p>临时运行这两个配套文件，让设备进入烧写模式，不更新存储中的启动程序。设备已进入 fastboot 模式时可取消。</p>
          </div>
          <div class="db-flash-option" data-el="erase-env-row">
            <label><input data-el="erase-env" type="checkbox" checked> 清除已保存的启动设置</label>
            <small>不使用镜像文件 · 操作 env 分区</small>
            <p>清除 U-Boot 保存的启动参数。更新单个镜像时通常可保留。</p>
          </div>
          <div class="db-flash-option" data-el="secure-chip-row" hidden>
            <label for="db-flash-secure-chip">安全芯片镜像</label>
            <select id="db-flash-secure-chip" data-el="secure-chip">
              <option value="auto">自动检测（fastboot getvar is-secure-chip）</option>
              <option value="secure">始终使用签名/加密镜像</option>
              <option value="non-secure">始终使用普通镜像</option>
            </select>
            <p>按芯片类型自动改用 .pro.enc / preboot_sign.itb；仅在明确板型时手动固定。</p>
          </div>
          <div class="db-flash-option">
            <label><input data-el="reboot" type="checkbox" checked> 完成后重启设备</label>
            <small>不使用镜像文件</small>
            <p>全部所选步骤成功后，重启设备并按其当前启动配置运行。</p>
          </div>
          <div class="db-flash-option">
            <label for="db-flash-data-mode">用户数据处理（data 分区）</label>
            <code>userdata.img</code>
            <select id="db-flash-data-mode" data-el="data-mode">
              <option value="erase">擦除用户数据（原脚本 ALL 默认）</option><option value="flash">烧写用户数据镜像</option></select>
            <p data-el="data-mode-help">仅在上方勾选「用户数据」时生效。擦除不会使用 userdata.img；烧写使用该项选定的镜像。</p>
          </div>
          <div class="db-flash-option db-flash-target">
            <label for="db-flash-serial">目标 fastboot 序列号（只接一台时可留空）</label>
            <input id="db-flash-serial" data-el="serial" type="text">
            <p>多台 fastboot 设备必须指定序列号；USB 准备阶段请只连接一块测试板。</p>
          </div>
        </fieldset>
        <p class="db-flash-help">只勾选单个镜像时不会额外烧写其他镜像。是否擦除环境、用户数据及重启，以当前选项为准。</p>
        <div class="db-flash-actions"><button data-el="plan" type="button">预览执行计划</button>
          <button data-el="start" type="button" class="db-flash-primary">开始烧写</button>
          <button data-el="cancel" type="button" disabled>停止烧写</button></div>
        <p data-el="message" class="db-flash-message" role="status" aria-live="polite">请先确认工具目录</p>
        <progress data-el="progress" max="1" value="0" aria-label="烧写执行进度"></progress>
        <details open><summary>最终执行计划</summary><ol data-el="steps" class="db-flash-steps"></ol></details>
        <details open><summary>命令与实时输出（stdout / stderr）</summary><pre data-el="log" class="db-flash-log" aria-label="工具实时输出"></pre></details>`;
      this.el = name => host.querySelector('[data-el="' + name + '"]');
      this.directoryDirty = false;
      this.el('directory').value = this.savedDir();
      this.el('platform-choice').addEventListener('change', () => this.guard(async () => this.loadCatalog()));
      this.el('close').addEventListener('click', () => this.close());
      this.el('browse').addEventListener('click', () => this.guard(async () => {
        const result = await this.pick('directory');
        if (result.success && result.path) {
          this.el('directory').value = result.path;
          this.directoryDirty = true;
          this.toolChanged();
          await this.loadCatalog();
        }
      }));
      this.el('directory').addEventListener('input', () => { this.directoryDirty = true; this.toolChanged(); });
      this.el('confirm').addEventListener('click', () => this.guard(async () => {
        // 用户确认的目录优先，后台配置刷新不得在请求期间覆盖输入框。
        this.directoryDirty = true;
        await this.loadCatalog();
      }));
      this.el('pick').addEventListener('click', () => this.guard(async () => {
        const result = await this.pick('file');
        if (!result.success) {
          if (result.output) throw new Error(result.output);
          return;
        }
        const paths = result.paths || (result.path ? [result.path] : []);
        if (!paths.length) return;
        // 同一烧写项再次选择镜像时，用本批次候选替换旧批次候选。
        const picked = [...new Set(paths)];
        const selected = await this.call('catalog', {toolDir: this.el('directory').value, files: picked, platform: this.el('platform-choice').value});
        const replaceIds = new Set(selected.files.filter(file => file.state === 'matched')
          .flatMap(file => file.matches));
        const previous = new Map((this.model?.files || []).map(file => [file.path, file]));
        this.files = [...new Set([...this.files.filter(path => {
          const file = previous.get(path);
          return !file || file.state !== 'matched' || !file.matches.some(id => replaceIds.has(id));
        }), ...picked])];
        this.overrides = {};
        await this.loadCatalog();
      }));
      this.el('defaults').addEventListener('click', () => this.guard(async () => {
        this.files = [];
        this.overrides = {};
        await this.loadCatalog();
      }));
      this.el('all').addEventListener('change', () => {
        if (this.model) this.model.items.forEach(item => { item.checked = this.el('all').checked; });
        this.invalidatePlan();
        this.renderItems();
      });
      for (const name of ['prepare', 'erase-env', 'reboot', 'serial', 'secure-chip']) {
        this.el(name).addEventListener('change', () => this.invalidatePlan());
      }
      this.el('data-mode').addEventListener('change', () => {
        this.invalidatePlan();
        this.renderItems();
      });
      this.el('plan').addEventListener('click', () => this.guard(() => this.preview()));
      this.el('start').addEventListener('click', () => this.guard(async () => {
        this.requestId = crypto.randomUUID().replaceAll('-', '');
        this.sequence = 0;
        this.el('log').textContent = '';
        let result;
        try { result = await this.call('start', {...this.arguments(), requestId: this.requestId}); }
        catch (error) {
          // 超时不重发开始请求，用唯一标识查询是否已在执行端接纳。
          if (this.context && error.uncertain) {
            this.jobId = '';
            this.running = true;
            this.schedulePoll();
          } else this.requestId = '';
          throw error;
        }
        this.jobId = result.jobId;
        this.running = result.state === 'running';
        if (result.state === 'interrupted' || (result.state === 'cancelled' && !result.plan)) {
          this.message(result.notice, result.state === 'interrupted'); return;
        }
        this.showPlan(result.plan);
        this.el('progress').value = 0;
        this.el('progress').max = result.total || 1;
        this.message(result.simulation ? '正在模拟…' : '正在烧写，请查看下方工具输出…');
        this.updateControls();
        this.schedulePoll();
        await this.poll();
      }));
      this.el('cancel').addEventListener('click', () => this.guard(async () => {
        const result = await this.call('cancel', {jobId: this.jobId, requestId: this.requestId});
        if (result.state === 'cancelled' && !result.jobId) {
          this.running = false; clearTimeout(this.timer); this.message(result.notice); return;
        }
        this.message('正在停止当前工具；已写入的内容不会恢复…');
      }));
      host.addEventListener('keydown', event => {
        if (event.key === 'Escape') { event.stopPropagation(); this.close(); }
      });
      this.renderItems();
    }
    savedDir() {
      try { return localStorage.getItem('db-flash-tool-dir') || ''; } catch (_) { return ''; }
    }
    message(text, error = false) {
      this.el('message').textContent = text;
      this.el('message').classList.toggle('error', error);
    }
    toolChanged() {
      this.model = null;
      this.files = [];
      this.overrides = {};
      this.renderItems();
      this.invalidatePlan();
      this.message('工具目录已变更，请重新确认');
      this.updateControls();
    }
    invalidatePlan() {
      this.el('steps').replaceChildren();
    }
    async call(action, args = {}) {
      const result = this.context ? await this.context.call(action, args)
        : await this.request('db-flash', {action, arguments: args});
      if (result.stale) {
        const error = new Error('目标已切换，旧结果已忽略');
        error.stale = true;
        throw error;
      }
      if (!result.success) {
        const error = new Error(result.output || '操作失败');
        error.uncertain = result.output === 'timeout' || Boolean(result.operationUncertain);
        throw error;
      }
      return result.data;
    }
    pick(kind) {
      if (this.context && !this.context.local) return this.remoteFiles.open(kind);
      return this.request(kind === 'file' ? 'pick-file' : 'pick-directory',
        kind === 'file' ? {multi: true} : {title: '选择 DB 固化工具目录'});
    }
    async guard(action) {
      if (this.busy) return;
      this.busy = true;
      this.updateControls();
      try { await action(); }
      catch (error) { this.message(error.message || String(error), true); }
      finally { this.busy = false; this.updateControls(); }
    }
    async loadCatalog() {
      // 远程上下文刷新或用户改目录可能并发；旧响应不能覆盖后发起的目录。
      const requested = this.el('directory').value;
      const sequence = this.catalogSequence = (this.catalogSequence || 0) + 1;
      let model;
      for (let attempt = 0; ; attempt++) {
        try { model = await this.call('catalog', {toolDir: requested, files: this.files, platform: this.el('platform-choice').value}); break; }
        catch (error) {
          // 编目只读：目标选择刷新导致的旧响应可直接按当前选择重试。
          if (!error.stale || attempt >= 2 || this.el('directory').value.trim() !== requested.trim()) throw error;
        }
      }
      if (sequence !== this.catalogSequence) return;
      if (this.el('directory').value.trim() !== requested.trim()) return;
      this.model = model;
      this.el('platform-choice').value = model.platformDetected ? '' : model.platform;
      this.el('mode').textContent = this.model.simulation ? '测试宿主 · 模拟执行'
        : this.context && !this.context.local ? '远程烧写 · 从机实时工具输出' : '本机烧写 · 实时工具输出';
      this.el('platform').textContent = this.model.toolDir
        ? '识别平台：' + this.model.platformLabel + (this.model.platformDetected ? '（自动识别）' : '（手动指定）')
        : '识别平台：—';
      this.el('platform').title = this.model.platformReason || '';
      const dataAction = {erase: '擦除', format: '格式化', flash: '烧写'}[this.model.options.dataMode] || '处理';
      this.el('banner').textContent = this.model.simulation
        ? '测试宿主注入假执行器，不操作真实设备。'
        : `点击开始后实际写入所选分区；ALL 默认${dataAction}用户数据。请先核对文件和操作选项。`;
      this.el('start').textContent = this.model.simulation ? '开始模拟' : '开始烧写';
      this.el('directory').value = this.model.toolDir;
      this.el('prepare').checked = this.model.options.prepare;
      this.el('prepare-files').textContent = ['p300-udp', 'p400'].includes(this.model.platform)
        ? 'spl_im_burn_signed.img + u-boot.bin' : 'spl_im_signed.img + u-boot.bin';
      this.el('erase-env').checked = this.model.options.eraseEnv;
      this.el('erase-env-row').hidden = Boolean(this.model.options.secureChip);
      this.el('secure-chip-row').hidden = !this.model.options.secureChip;
      if (this.model.options.secureChip) this.el('secure-chip').value = this.model.options.secureChip;
      this.el('reboot').checked = this.model.options.reboot;
      const modes = this.model.options.dataModes || [{value: 'erase', label: '擦除用户数据'}];
      this.el('data-mode').replaceChildren(...modes.map(mode => new Option(mode.label, mode.value)));
      this.el('data-mode').value = this.model.options.dataMode;
      this.el('source').textContent = this.files.length
        ? '已选 ' + this.files.length + ' 个文件；补选项目可使用默认镜像' : '未选其他文件，使用工具目录中的默认镜像';
      this.el('issues').replaceChildren();
      for (const item of this.model.issues) {
        const p = document.createElement('p');
        p.textContent = item.path + '：' + item.reason;
        this.el('issues').appendChild(p);
      }
      this.invalidatePlan();
      this.renderItems();
      if (this.model.toolValid) {
        if (this.context) await this.call('save', {toolDir: this.model.toolDir});
        if (!this.context || this.context.local) {
          try { localStorage.setItem('db-flash-tool-dir', this.model.toolDir); } catch (_) {}
        }
        const prefix = this.model.platformDetected
          ? `已自动识别：${this.model.platformLabel}`
          : `已按${this.model.platformLabel}加载`;
        const action = this.files.length ? '已勾选匹配文件，可继续手动修改' : '默认 ALL 已加载，可继续手动修改';
        this.message(`${prefix}（${this.model.items.length} 项）；${action}`);
      } else {
        const issues = (this.model.toolIssues || ['请确认工具目录']).join('；');
        const fallback = this.model.platform === 'legacy'
          && (this.model.platformReason || '').includes('未检测到专用平台特征')
          ? '；未识别到 p300-udp 平台特征，若这是 p300-udp 固化包，请检查 flashall.bat/flash_cfg.ini 是否完整或是否选错目录'
          : '';
        this.message(issues + fallback, true);
      }
    }
    renderItems() {
      this.el('items').replaceChildren();
      const items = this.model?.items || LEGACY_IDS.map(id => ({
        id, label: STANDARD_FILES[id], default: STANDARD_FILES[id], path: '',
        defaultFile: {path: '', size: 0, exists: false}, candidates: [], checked: true,
      }));
      const manual = (this.model?.files || []).filter(file => file.state !== 'matched').map(file => file.path);
      for (const item of items) {
        const standardName = (item.default || STANDARD_FILES[item.id] || item.id).split(/[\\/]/).pop();
        const row = document.createElement('tr');
        row.className = 'db-flash-item';
        const checkCell = document.createElement('td');
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = item.checked;
        check.dataset.item = item.id;
        check.setAttribute('aria-label', '选择 ' + standardName);
        checkCell.appendChild(check);
        const nameCell = document.createElement('th');
        nameCell.scope = 'row';
        const standard = document.createElement('code');
        standard.className = 'db-flash-standard';
        standard.textContent = standardName;
        nameCell.appendChild(standard);
        const descriptionCell = document.createElement('td');
        const description = document.createElement('p');
        description.className = 'db-flash-description';
        const dataMode = this.el('data-mode').value;
        const dataInactive = item.id === 'data' && dataMode !== 'flash' && !(this.model?.platform === 'p400' && dataMode === 'format');
        description.textContent = IMAGE_HELP[item.id] || item.label || '';
        if (dataInactive) {
          description.textContent += dataMode === 'format'
            ? ' 当前为格式化模式，不使用此文件。' : ' 当前为擦除模式，不烧写此文件。';
        }
        descriptionCell.appendChild(description);
        const fileCell = document.createElement('td');
        const select = document.createElement('select');
        select.dataset.file = item.id;
        select.setAttribute('aria-label', standardName + '对应文件');
        const choices = new Map();
        // 已明确选择该项镜像时，只展示所选候选，避免默认文件看起来也被选中。
        if (!item.candidates.length && item.defaultFile.path) choices.set(item.defaultFile.path, item.defaultFile);
        for (const file of item.candidates) choices.set(file.path, file);
        for (const path of manual) choices.set(path, {path, size: null, exists: true});
        const effective = this.overrides[item.id] || item.path;
        if (!effective) select.appendChild(new Option(item.conflict ? '请选择一个候选文件' : '请先确认工具目录', ''));
        for (const file of choices.values()) {
          const size = file.size === null ? '手动指定' : (file.size / 1048576).toFixed(2) + ' MiB';
          const basename = file.path.split(/[\\/]/).pop();
          const caption = basename + ' · ' + size + (file.exists ? '' : ' · 缺失');
          const option = new Option(caption, file.path);
          option.title = file.path;
          select.appendChild(option);
        }
        select.value = effective || '';
        select.title = effective || '请选择文件';
        const note = document.createElement('small');
        note.textContent = !this.model ? '确认目录后加载默认文件'
          : dataInactive ? (dataMode === 'format' ? '本项执行格式化，不使用镜像' : '本项执行擦除，不使用镜像')
          : item.conflict && !this.overrides[item.id] ? '多个文件匹配，请手动选择'
          : effective === item.defaultFile.path ? '默认文件' : '所选文件 · 不复制到 DB';
        if (!dataInactive && item.conflict && !this.overrides[item.id]) note.className = 'error';
        check.addEventListener('change', () => {
          item.checked = check.checked;
          this.invalidatePlan();
          this.renderItems();
        });
        select.addEventListener('change', () => {
          this.overrides[item.id] = select.value;
          this.invalidatePlan();
          this.renderItems();
        });
        fileCell.append(select, note);
        if (!dataInactive && effective) {
          const details = document.createElement('details');
          details.className = 'db-flash-file-path';
          const summary = document.createElement('summary');
          summary.textContent = '查看完整路径';
          const fullPath = document.createElement('small');
          fullPath.textContent = effective;
          details.append(summary, fullPath);
          fileCell.appendChild(details);
        }
        row.append(checkCell, nameCell, descriptionCell, fileCell);
        this.el('items').appendChild(row);
      }
      const count = items.filter(item => item.checked).length;
      const dataChecked = items.find(item => item.id === 'data')?.checked;
      const mode = this.el('data-mode').value;
      this.el('data-mode-help').textContent = !dataChecked
        ? '上方未勾选用户数据，本次保留用户数据分区，不执行擦除、格式化或镜像烧写。'
        : mode === 'format'
          ? (this.model?.platform === 'p400' ? '将先烧写所选 userdata 镜像，再格式化用户数据分区。' : '将格式化用户数据分区（对应 flash_cfg.ini 的 format data），不使用 userdata.img。')
          : mode === 'erase'
            ? '将擦除用户数据分区，不使用 userdata.img。' + (this.model?.platform === 'legacy' ? '原脚本 ALL 默认执行此操作。' : '')
            : '将所选 userdata 镜像写入用户数据分区，用于初始化或替换该分区内容。';
      this.el('all').checked = count === items.length;
      this.el('all').indeterminate = count > 0 && count < items.length;
      this.updateControls();
    }
    arguments() {
      if (!this.model?.toolValid) throw new Error('请先确认有效的工具目录');
      const options = {prepare: this.el('prepare').checked, eraseEnv: this.el('erase-env').checked,
        reboot: this.el('reboot').checked, dataMode: this.el('data-mode').value};
      if (this.model.options.secureChip) options.secureChip = this.el('secure-chip').value;
      return {
        platform: this.model.platform,
        toolDir: this.model.toolDir, files: [...this.files],
        selected: this.model.items.filter(item => item.checked).map(item => item.id),
        overrides: {...this.overrides}, serial: this.el('serial').value.trim(),
        options,
      };
    }
    async preview() {
      const plan = await this.call('plan', this.arguments());
      this.showPlan(plan);
      this.message(plan.simulation ? '执行计划已生成；所有步骤仅用于模拟' : '执行计划已生成；点击开始烧写后按此计划执行');
    }
    showPlan(plan) {
      this.el('steps').replaceChildren();
      for (const step of plan.steps) {
        const li = document.createElement('li');
        const title = document.createElement('strong');
        title.textContent = step.label;
        const code = document.createElement('code');
        code.textContent = (step.displayArgv || step.argv).map(arg => JSON.stringify(arg)).join(' ');
        li.append(title, code);
        this.el('steps').appendChild(li);
      }
    }
    acceptProgress(event) {
      if (event.jobId !== this.jobId || event.sequence <= this.sequence) return;
      this.sequence = event.sequence;
      if (typeof event.completed === 'number') this.el('progress').value = event.completed;
      if (event.total) this.el('progress').max = event.total;
      if (event.text) {
        const log = this.el('log');
        const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
        // 工具输出可能逐字符到达，连续拼接，保留原有换行与进度打印。
        const prefix = event.kind !== 'output' && log.textContent && !log.textContent.endsWith('\n') ? '\n' : '';
        log.textContent = (log.textContent + prefix + event.text + (event.kind === 'output' ? '' : '\n')).slice(-200000);
        if (follow) log.scrollTop = log.scrollHeight;
      }
      if (['succeeded', 'failed', 'cancelled'].includes(event.kind)) {
        this.running = false;
        clearTimeout(this.timer);
        this.message(event.text, event.kind === 'failed');
        this.updateControls();
      }
    }
    async poll() {
      if (!this.jobId || !this.running) return;
      try {
        const job = await this.call('status', {jobId: this.jobId, requestId: this.requestId, after: this.sequence});
        if (!job) { this.message('执行端尚未确认任务，正在查询；不会自动重发烧写命令', true); return; }
        if (job.state === 'interrupted') {
          this.running = false; this.message(job.notice, true); this.updateControls(); return;
        }
        if (job.state === 'cancelled' && !job.jobId) {
          this.running = false; this.message(job.notice); this.updateControls(); return;
        }
        if (this.context && !this.context.ready) { this.context.ready = true; this.updateControls(); }
        if (!this.jobId) this.jobId = job.jobId;
        this.el('progress').max = job.total || 1;
        if (job.logTruncated) this.message('较早的输出已截断，正在显示可用日志');
        for (const event of job.events) this.acceptProgress(event);
      } catch (error) {
        this.message('状态暂未确认：' + error.message + '；请保持连接，恢复后继续查询', true);
      }
    }
    schedulePoll() {
      clearTimeout(this.timer);
      this.timer = setTimeout(async () => {
        await this.poll();
        if (this.running) this.schedulePoll();
      }, 1000);
    }
    updateControls() {
      const locked = this.busy || this.running;
      this.host.querySelectorAll('input,select,button').forEach(node => {
        if (node === this.el('close')) return;
        if (node.closest('.remote-tool-context')) return;
        node.disabled = locked || (this.context && !this.context.ready);
      });
      for (const name of ['pick', 'defaults', 'plan', 'start', 'all']) {
        this.el(name).disabled = locked || !this.model?.toolValid || (this.context && !this.context.ready);
      }
      this.host.querySelectorAll('input[data-item]').forEach(check => {
        check.disabled = locked || !this.model?.toolValid;
      });
      this.host.querySelectorAll('select[data-file]').forEach(select => {
        const item = this.model?.items.find(value => value.id === select.dataset.file);
        const dataInactive = item?.id === 'data' && this.el('data-mode').value !== 'flash' && !(this.model?.platform === 'p400' && this.el('data-mode').value === 'format');
        select.disabled = locked || !this.model?.toolValid || !item?.checked || dataInactive;
      });
      this.el('cancel').disabled = this.busy || !this.running || (this.context && !this.context.ready);
      if (this.context) {
        this.context.setBusy(locked);
        this.context.refreshButton.disabled = this.busy;
      }
      this.host.setAttribute('aria-busy', String(locked));
    }
    open() {
      this.previousFocus = document.activeElement;
      this.host.hidden = false;
      this.el('directory').focus();
    }
    close() {
      this.host.hidden = true;
      this.previousFocus?.focus();
      // 关闭窗口不取消后台作业，轮询继续，重开可看到结果。
    }
    dispose() {
      clearTimeout(this.timer);
    }
  }
  window.DBFlashPanel = DBFlashPanel;
})();
