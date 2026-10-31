(() => {
  const windows = new Map();
  const opening = new Set();
  const windowKey=plugin=>(plugin.remoteTarget||'computer:local')+'|'+plugin.id;
  let refreshGeneration=0;
  function closeWindow(id) {
    const record=windows.get(id);windows.delete(id);
    if(record){sendWS({type:'desktop-terminal-destroy',windowId:record.windowId});record.popup?.close();}
  }
  const layer = document.createElement('div');
  layer.className = 'diagnostic-overlay'; layer.style.display = 'none';
  layer.id = 'plugin-manager';
  layer.innerHTML = `<div class="diagnostic-dialog plugin-dialog"><div class="diagnostic-header"><div><h3>插件管理</h3><p>按需添加工具，让工作区保持简洁</p></div><button class="diagnostic-close" aria-label="关闭">✕</button></div><div class="plugin-toolbar"><select class="tool-select" data-target aria-label="插件目标电脑"><option value="computer:local">本机</option></select><div><strong>已安装插件</strong><span data-count></span></div><button class="plugin-install">＋ 安装插件</button><input hidden type="file" accept=".dmp" multiple><button data-refresh aria-label="刷新插件列表">↻</button></div><p role="status" aria-live="polite"></p><div data-list></div><label class="plugin-footer" data-policy><input type="checkbox"> 允许已验证主控远程安装和卸载插件</label><div class="plugin-footer">选择官方 .dmp 文件安装 · 无需重启 · 更新前请先停用</div></div>`;
  document.body.append(layer);
  const managementFloating = new ToolFloatingWindow(layer, layer.firstChild, layer.querySelector('.diagnostic-header'), {width:800,height:640});
  const status = layer.querySelector('[role=status]');
  const targetSelect=layer.querySelector('[data-target]');
  targetSelect.onchange=()=>execute(refresh);
  async function rpc(action, args = {}, target=targetSelect.value) {
    const result = await callWS('plugin-manager', {action, arguments:args,target}, 200000);
    if (!result.success) throw Error(result.output || '插件操作失败');
    return result.data;
  }
  async function refresh() {
    const generation=++refreshGeneration,target=targetSelect.value;
    const local=await rpc('list',{},'computer:local');
    if(generation!==refreshGeneration)return;
    // Refresh local entries immediately; a slow remote connection must not hide them.
    renderMenu(local,[]);
    const targets=await loadTargets();
    const groups=await Promise.all(targets.filter(item=>!item.local&&item.online&&item.role==='slave').map(async item=>{
      const label=item.label+(targets.filter(other=>other.label===item.label).length>1?' · '+(item.nodeId||item.target).slice(-6):'');
      try {const result=await rpc('list',{},item.target);return {target:item.target,label,result,plugins:(result.plugins||[]).map(plugin=>({...plugin,remoteTarget:item.target,targetLabel:label}))};}
      catch(error){return {target:item.target,label:item.label,error};}
    }));
    const remote=target!=='computer:local',group=groups.find(item=>item.target===target);
    const result=remote?group?.result:local;
    if(generation!==refreshGeneration)return;
    renderMenu(local,groups);
    if(remote&&!result){layer.querySelector('[data-list]').replaceChildren();layer.querySelector('[data-count]').textContent='';throw Error(group?.error?.message||'从机连接不可用，请检查连接验证');}
    const list=remote?group.plugins:local;
    const policy=layer.querySelector('[data-policy]');policy.hidden=remote;
    if(!remote){const value=await rpc('install-policy');policy.querySelector('input').checked=value?.allowed===true;}
    if(remote && !result.installAllowed)status.textContent='从机未允许远程安装和卸载，请在从机插件管理中开启';

    for(const [key,record] of windows)if(record.target==='computer:local'&&!local.some(plugin=>plugin.id===record.pluginId&&plugin.enabled))closeWindow(key);
    for(const [key,record] of windows){const group=groups.find(item=>item.target===record.target);if(group?.result&&!group.plugins.some(plugin=>plugin.id===record.pluginId&&plugin.enabled&&plugin.remoteAllowed))closeWindow(key);}
    const container = layer.querySelector('[data-list]'); container.replaceChildren();
    layer.querySelector('[data-count]').textContent = list.length ? ` ${list.length} 个` : '';
    if (!list.length) {
      const empty = document.createElement('div'); empty.className = 'plugin-empty';
      empty.innerHTML = '<div class="plugin-empty-icon">＋</div><h4>添加你的第一个工具</h4><p>安装插件后，可直接从「工具」菜单打开。</p>';
      container.append(empty);
    }
    for (const plugin of list) {
      const row = document.createElement('article');row.className = 'plugin-card';
      const identity = document.createElement('div');identity.className = 'plugin-identity';
      const icon = document.createElement('div');icon.className='plugin-icon';icon.textContent=plugin.name.slice(0,1);identity.append(icon);
      const info=document.createElement('div');const title=document.createElement('h4');title.textContent=plugin.name;info.append(title);
      const detail=document.createElement('p');detail.textContent=plugin.description || '官方工具插件';info.append(detail);
      const version=document.createElement('small');version.textContent='v'+plugin.version;info.append(version);identity.append(info);row.append(identity);
      const actions=document.createElement('div');actions.className='plugin-actions';
      const badge=document.createElement('span');badge.className='plugin-badge '+(plugin.enabled?'is-enabled':'');badge.textContent=plugin.enabled?'已启用':'已停用';actions.append(badge);
      for (const [label, action] of (remote?[[!plugin.remoteCapable?'仅支持从机本地使用':plugin.remoteAllowed?'打开':'请在从机开启允许主控使用','open'],['卸载','uninstall']]:[[plugin.enabled ? '停用' : '启用',plugin.enabled ? 'disable' : 'enable'],...(plugin.frontend ? [['打开','open']] : []),['卸载','uninstall']])) {
        const button = document.createElement('button');button.textContent=label;button.dataset.action=action;if(remote&&(action==='open'?(!plugin.enabled||!plugin.remoteAllowed||!plugin.frontend||!plugin.remoteCapable):!result.installAllowed))button.disabled=true;actions.append(button);
        button.onclick = () => execute(async () => {
          if (action === 'open') return launch(plugin);
          if (action === 'uninstall' && !confirm(`卸载 ${plugin.name}？`)) return;
          await rpc(action,{id:plugin.id});
          if (action === 'disable' || action === 'uninstall') { closeWindow(windowKey(plugin)); }
          await refresh();
        });
      }
      if(!remote && plugin.actions?.length){
        const label=document.createElement('label');label.style.cssText='display:flex;align-items:center;gap:6px;font-size:12px';
        const toggle=document.createElement('input');toggle.type='checkbox';
        if(plugin.enabled){const config=await rpc('call',{id:plugin.id,action:'config'});toggle.checked=config?.data?.remoteAllowed===true;}
        label.append(toggle,document.createTextNode('允许主控使用（自动启用）'));actions.prepend(label);
        toggle.onchange=()=>execute(async()=>{await rpc('allow-remote',{id:plugin.id,allowed:toggle.checked});await refresh();});
      }
      row.append(actions);container.append(row);
    }
  }
  function renderMenu(local,groups){
    document.querySelectorAll('[data-plugin-menu], [data-plugin-heading]').forEach(item=>item.remove());
    const container=document.getElementById('tools-menu');
    for(const group of [{label:'本机工具',plugins:local.filter(plugin=>plugin.enabled)},...groups.map(group=>({...group,label:'远程 · '+group.label,plugins:(group.plugins||[]).filter(plugin=>plugin.remoteCapable)}))]){
      const plugins=group.plugins.filter(plugin=>plugin.frontend);if(!plugins.length)continue;
      const heading=document.createElement('div');heading.dataset.pluginHeading='true';heading.className='plugin-menu-heading';heading.textContent=group.label;container.append(heading);
      for(const plugin of plugins){
        const menu=document.createElement('div');menu.className='tab-dropdown-item';menu.dataset.pluginMenu=plugin.id;menu.dataset.pluginTarget=plugin.remoteTarget||'computer:local';
        const hint=plugin.remoteTarget?(!plugin.enabled?' · 未启用':!plugin.remoteAllowed?' · 未授权':''):'';
        menu.textContent=plugin.name+hint+'…';menu.tabIndex=0;menu.setAttribute('role','button');
        menu.onclick=()=>execute(async()=>{document.getElementById('tools-menu').style.display='none';
          if(plugin.remoteTarget&&(!plugin.enabled||!plugin.remoteAllowed)){targetSelect.value=plugin.remoteTarget;managementFloating.show();throw Error('请在从机开启插件的「允许主控使用」');}
          await launch(plugin);
        });
        menu.onkeydown=event=>{if(event.key==='Enter')menu.click();};container.append(menu);
      }
    }
  }
  async function launch(plugin) {
    const key=windowKey(plugin);
    if(opening.has(key))return;
    opening.add(key);
    const popup=windows.has(key)||window.pywebview?null:window.open('about:blank','devmux-plugin-'+key,'popup,width=900,height=700');
    try {
      if(plugin.remoteTarget&&(!plugin.enabled||!plugin.remoteAllowed))throw Error('请在从机开启插件的「允许主控使用」');
      if (!plugin.enabled) { await rpc('enable',{id:plugin.id},'computer:local');plugin={...plugin,enabled:true};await refresh(); }
      await open(plugin,popup);
    }catch(error){popup?.close();throw error;}
    finally{opening.delete(key);}
  }
  async function execute(fn) { try {status.textContent='处理中…';await fn();status.textContent='';} catch(e) {status.textContent=e.message;} }
  async function open(plugin,popup) {
    const key=windowKey(plugin);
    if (windows.has(key)) {sendWS({type:'desktop-terminal-raise',windowId:windows.get(key).windowId});windows.get(key).popup?.focus();return;}
    try {
      const result=await callWS('desktop-terminal-open',{view:'plugin',target:plugin.remoteTarget||'computer:local',key:'plugin:'+plugin.id,title:plugin.name+' · '+(plugin.targetLabel||'本机'),
        width:Math.round(Math.min(1600,Math.max(320,Number(plugin.window?.width)||900))),height:Math.round(Math.min(1200,Math.max(240,Number(plugin.window?.height)||700)))},40000);
      if(!result.success)throw Error(result.output||'插件窗口创建失败');
      if(result.native || result.data?.native)popup?.close();
      else if(popup)popup.location=result.data.url;
      else {sendWS({type:'desktop-terminal-destroy',windowId:result.data.windowId});throw Error('请允许弹出窗口后重试');}
      windows.set(key,{pluginId:plugin.id,windowId:result.data.windowId,target:plugin.remoteTarget||'computer:local',popup:result.data.native?null:popup});
      sendWS({type:'desktop-terminal-bind',windowId:result.data.windowId});
    } catch(error) {popup?.close();throw error;}
  }
  layer.querySelector('.diagnostic-header button').onclick=()=>managementFloating.hide();
  layer.querySelector('[data-refresh]').onclick=()=>execute(refresh);
  let installing = false;
  const input = layer.querySelector('input');
  const installButton = layer.querySelector('.plugin-toolbar .plugin-install');
  input.onchange=async event=>{
    const files=Array.from(event.target.files);
    if(!files.length || installing)return;
    installing=true;installButton.disabled=true;input.disabled=true;targetSelect.disabled=true;
    const failures=[];let succeeded=0;
    try {
      for(let index=0;index<files.length;index++){
        const file=files[index];status.textContent=`正在安装 ${index+1}/${files.length}：${file.name}`;
        try {
          if(file.size>4*1024*1024)throw Error('插件包过大');
          const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(Error('文件读取失败'));reader.readAsDataURL(file);});
          await rpc('install',{package:encoded});succeeded++;
        }catch(error){failures.push(`${file.name}：${error.message || '安装失败'}`);}
      }
      try{await refresh();}catch(error){failures.push(`刷新列表失败：${error.message}`);}
      status.textContent=`安装完成：成功 ${succeeded} 个，失败 ${files.length-succeeded} 个`+(failures.length?'；'+failures.join('；'):'');
    }finally{event.target.value='';input.disabled=false;installButton.disabled=false;targetSelect.disabled=false;installing=false;}
  };
  layer.querySelector('.plugin-toolbar .plugin-install').onclick=()=>layer.querySelector('input').click();
  window.PluginIntegration = {isOpen:id=>Array.from(windows.values()).some(record=>record.pluginId===id),refresh: () => execute(refresh), accept(message) {
    for(const [id,record] of windows)if(record.windowId===message.windowId){
      if(['desktop-terminal-closed','desktop-terminal-dock'].includes(message.type)){windows.delete(id);record.popup?.close();}
      return true;
    }
    return false;
  }};
  layer.querySelector('[data-policy] input').onchange=event=>execute(async()=>{await rpc('set-install-policy',{allowed:event.target.checked});});
  async function loadTargets(){
    const response=await callWS('extension-tool',{tool:'serial',action:'targets'});
    if(response.success&&Array.isArray(response.data?.targets)){ComputerTargets.render(targetSelect,response.data.targets,targetSelect.value);return response.data.targets;}
    return [];
  }
  document.getElementById('tools-tab').addEventListener('click',()=>{if(document.getElementById('tools-menu').style.display==='block')execute(refresh);});
  document.getElementById('tools-plugins').onclick=()=>{managementFloating.show();execute(refresh);};
})();
