/* DevMux public browser SDK, API 1. No host implementation imports. */
(() => {
  'use strict';
  if (window.DevMuxPlugin) return;
  let sequence = 0, disposed = false;
  const pending = new Map(), cleanups = [], views = new Map();
  class PluginError extends Error {
    constructor(message, code = 'plugin-error') { super(message); this.name = 'PluginError'; this.code = code; }
  }
  const failure = error => ({success:false, output:error.message || String(error), code:error.code || 'plugin-error'});
  function onDispose(callback) {
    if (typeof callback !== 'function') throw new TypeError('清理函数无效');
    if (disposed) callback(); else cleanups.push(callback);
    return callback;
  }
  function listen(element, event, callback, options) {
    if(disposed)throw new PluginError('插件界面已关闭','disposed');
    element.addEventListener(event,callback,options);
    let active=true;
    const off=()=>{if(active){active=false;element.removeEventListener(event,callback,options);}};
    onDispose(off);return off;
  }
  function dispose() {
    if(disposed)return;
    disposed=true;
    for(const [id,request] of pending){clearTimeout(request.timer);request.resolve(failure(new PluginError('插件界面已关闭','disposed')));}
    pending.clear();
    for(const cleanup of cleanups.splice(0).reverse())try{cleanup();}catch(error){console.error(error);}
    views.clear();
  }
  function wire(request, timeout) {
    if(disposed)return Promise.resolve(failure(new PluginError('插件界面已关闭','disposed')));
    const id=++sequence;
    timeout=Math.min(200000,Math.max(1,Number(timeout)||30000));
    return new Promise(resolve=>{
      const timer=setTimeout(()=>{pending.delete(id);resolve(failure(new PluginError('调用超时，结果未确认，不会自动重试','timeout')));},timeout);
      pending.set(id,{resolve,timer});
      try { parent.postMessage({...request,id},'*'); }
      catch(error){clearTimeout(timer);pending.delete(id);resolve(failure(error));}
    });
  }
  // request preserves wire envelopes for existing service adapters. Prefer call/hostCall for new code.
  function request(type,payload={},timeout=30000) {
    if(type==='plugin')return wire({type:'devmux-plugin-call',action:payload.action,arguments:payload.arguments||{}},timeout);
    if(!['pick-file','pick-directory','extension-tool'].includes(type))return Promise.resolve(failure(new PluginError('未提供该宿主接口','permission-denied')));
    return wire({type:'devmux-host-call',hostType:type,hostPayload:payload},timeout);
  }
  function unwrap(result){
    if(result?.success===false)throw new PluginError(result.output||result.error||'插件调用失败',result.code);
    return result?.success===true&&Object.hasOwn(result,'data')?result.data:result;
  }
  const call=(action,args={},options={})=>request('plugin',{action,arguments:args},options.timeout).then(unwrap);
  const hostCall=(type,args={},options={})=>request(type,args,options.timeout).then(unwrap);
  const close=()=>{dispose();parent.postMessage({type:'devmux-plugin-close'},'*');};
  listen(window,'message',event=>{
    if(event.source!==parent||event.data?.type!=='devmux-plugin-result')return;
    const item=pending.get(event.data.id);if(!item)return;
    pending.delete(event.data.id);clearTimeout(item.timer);
    item.resolve(event.data.error?failure(new PluginError(event.data.error)):event.data.result);
  });
  window.addEventListener('pagehide',dispose,{once:true});
  const element=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;};
  function button(label,callback,{variant='secondary',disabled=false}={}){
    const node=element('button','dmp-button '+variant,label);node.type='button';node.disabled=disabled;
    if(callback)listen(node,'click',async()=>{if(node.disabled)return;node.disabled=true;try{await callback();}catch(error){window.dispatchEvent(new CustomEvent('devmux-error',{detail:error}));}finally{node.disabled=disabled;}});
    return node;
  }
  function status(text='',kind='info'){
    const node=element('p','dmp-status '+kind,text);node.setAttribute('role','status');node.setAttribute('aria-live','polite');
    node.update=(text,kind='info')=>{node.textContent=text;node.className='dmp-status '+kind;};return node;
  }
  function mount({title='插件工具',description='',content}={}){
    const page=element('section','dmp-page');const header=element('header','dmp-page-header');
    const identity=element('div');identity.append(element('h1','',title));if(description)identity.append(element('p','',description));
    const target=element('span','dmp-target',window.DevMuxInitialTarget&&window.DevMuxInitialTarget!=='computer:local'?'远程从机':'本机');
    header.append(identity,target);const body=element('main','dmp-page-body');if(content)body.append(content);
    const message=status();page.append(header,message,body);document.body.append(page);
    listen(window,'devmux-error',event=>message.update(event.detail?.message||String(event.detail),'error'));
    return {element:page,body,message,get visible(){return !disposed&&!page.hidden;},show(){page.hidden=false;},hide:close,raise(){page.focus();},destroy(){page.remove();}};
  }
  function form(schema,values={}){
    const node=element('form','dmp-form');const controls=new Map();
    for(const field of schema){
      if(!field.name||controls.has(field.name))throw new PluginError('表单字段名称无效或重复');
      const label=element('label','dmp-field');label.append(element('span','',field.label||field.name));
      let control;
      if(field.type==='select'){control=element('select');for(const option of field.options||[])control.append(new Option(option.label??String(option.value),String(option.value)));}
      else {control=element('input');control.type=field.type==='boolean'?'checkbox':field.type||'text';}
      control.name=field.name;control.required=!!field.required;control.disabled=!!field.disabled;
      for(const key of ['placeholder','min','max','step'])if(field[key]!==undefined)control.setAttribute(key,field[key]);
      const value=values[field.name]??field.default;
      if(control.type==='checkbox')control.checked=!!value;else if(value!==undefined)control.value=String(value);
      controls.set(field.name,control);label.append(control);if(field.description)label.append(element('small','',field.description));node.append(label);
    }
    node.values=()=>Object.fromEntries([...controls].map(([key,control])=>[key,control.type==='checkbox'?control.checked:control.type==='number'?(control.value===''?null:Number(control.value)):control.value]));
    node.control=name=>controls.get(name);listen(node,'submit',event=>event.preventDefault());return node;
  }
  function table(columns,rows=[]){
    const wrapper=element('div','dmp-table-scroll'),node=element('table','dmp-table'),head=element('thead'),heading=element('tr'),body=element('tbody');
    for(const column of columns){const cell=element('th','',column.label||column.key);cell.scope='col';heading.append(cell);}head.append(heading);node.append(head,body);wrapper.append(node);
    wrapper.update=rows=>{body.replaceChildren();for(const row of rows){const line=element('tr');for(const column of columns)line.append(element('td','',String(row[column.key]??'')));body.append(line);}};
    wrapper.update(rows);return wrapper;
  }
  function log({limit=1000}={}){
    const node=element('pre','dmp-log');node.setAttribute('role','log');node.tabIndex=0;let lines=[];
    node.appendLine=text=>{lines.push(String(text));lines=lines.slice(-Math.max(1,limit));node.textContent=lines.join('\n');node.scrollTop=node.scrollHeight;};
    node.clear=()=>{lines=[];node.textContent='';};return node;
  }
  function dialog({title='提示',content}={}){
    const node=element('dialog','dmp-dialog'),head=element('header','dmp-dialog-header');head.append(element('h2','',title));
    const hide=()=>node.close();head.append(button('关闭',hide));node.append(head);if(content)node.append(content);document.body.append(node);
    onDispose(()=>node.remove());return {element:node,get visible(){return node.open;},show(){if(!node.open)node.showModal();},hide,destroy(){node.remove();}};
  }
  function registerView(name,factory){
    if(views.has(name)||typeof factory!=='function')throw new PluginError('界面注册无效或重复: '+name);
    views.set(name,factory);const off=()=>{if(views.get(name)===factory)views.delete(name);};onDispose(off);return off;
  }
  function renderView(name,container,props={}){
    if(!views.has(name))throw new PluginError('界面未注册: '+name);
    const content=views.get(name)(props);container.replaceChildren(content);return content;
  }
  window.DevMuxPlugin={apiVersion:1,PluginError,call,hostCall,request,close,onDispose,listen,dispose,
    get context(){return Object.freeze({target:window.DevMuxInitialTarget||'computer:local',remote:!!window.DevMuxInitialTarget&&window.DevMuxInitialTarget!=='computer:local'});},
    interval(callback,ms){if(disposed)throw new PluginError('插件界面已关闭','disposed');const id=setInterval(callback,ms);onDispose(()=>clearInterval(id));return id;},
    ui:{element,button,status,mount,form,table,log,dialog,registerView,renderView}};
})();

(() => {
// 扩展工具共用目标栏：目标不会因离线自动切回本机，旧响应不会覆盖新目标。
const ComputerTargets = {
  render(select, targets, previous = select.value) {
    select.replaceChildren();
    for (const target of targets) {
      const instance = targets.filter(item => item.label === target.label).length > 1 ? ` · 实例 ${(target.nodeId || target.peerId || 'local').slice(-6)}` : '';
      const label = `${target.local ? '本机' : '远程'} · ${target.label}${instance}（${target.role === 'master' ? '主控' : '从机'}）${target.online ? '' : ' · 离线'}`;
      const option = new Option(label, target.target);
      option.disabled = !target.online;
      select.appendChild(option);
    }
    if (previous && !targets.some(target => target.target === previous)) {
      select.appendChild(new Option('原目标已离线或解除连接', previous));
      select.lastElementChild.disabled = true;
    }
    if (previous) select.value = previous;
  }
};
DevMuxPlugin.RemoteContext = class {
  constructor(container, tool, onChange, name = '工具') {
    this.tool = tool; this.name = name; this.onChange = onChange; this.generation = 0;
    this.targets = []; this.target = window.DevMuxInitialTarget || 'computer:local'; this.ready = false; this.local = true; this.loading = false;
    container.classList.add('remote-tool-context');
    container.innerHTML = `<div class="remote-tool-target-row"><label>目标电脑 <select class="tool-select" aria-label="目标电脑"></select></label>
      <button class="tool-btn" type="button">刷新连接</button></div>
      <p class="remote-tool-status" role="status">正在读取目标…</p>
      <label class="remote-tool-permission"><input type="checkbox"> <span></span></label>`;
    this.select = container.querySelector('select'); this.refreshButton = container.querySelector('button');
    this.status = container.querySelector('p'); this.permission = container.querySelector('input');
    this.permissionRow = container.querySelector('.remote-tool-permission');
    this.permissionRow.querySelector('span').textContent = `允许主控使用本机${name}（自动启用插件）`;
    DevMuxPlugin.listen(this.select, 'change', () => this.choose());
    DevMuxPlugin.listen(this.refreshButton, 'click', () => this.refresh());
    DevMuxPlugin.listen(this.permission, 'change', async () => {
      this.permission.disabled = true;
      try {
        const result = await this.call('permission', {allowed: this.permission.checked});
        if (!result.stale && result.success) this.permission.checked = result.data.remoteAllowed;
        else if (!result.stale) { this.permission.checked = !this.permission.checked; this.status.textContent = result.output; }
      } finally { this.permission.disabled = false; }
    });
  }
  setBusy(busy) {
    busy = busy || this.loading;
    this.select.disabled = busy; this.refreshButton.disabled = busy;
    this.permission.disabled = busy;
  }
  async call(action, args = {}) {
    const generation = this.generation;
    const result = await DevMuxPlugin.request('extension-tool', {tool: this.tool, target: this.target, action, arguments: args}, 60000);
    if (generation !== this.generation) return {stale: true};
    if (result.targetUnavailable || result.output === 'timeout') { this.ready = false; this.status.textContent = result.output || '目标连接不可用'; }
    return result;
  }
  async refresh() {
    const generation = ++this.generation;
    this.ready = false; this.loading = true; this.setBusy(true); this.onChange(this);
    const result = await DevMuxPlugin.request('extension-tool', {tool: this.tool, action: 'targets'}, 20000);
    if (generation !== this.generation) return;
    this.targets = result.success ? result.data.targets : [];
    ComputerTargets.render(this.select, this.targets, this.target);
    await this.choose();
  }
  async choose() {
    const generation = ++this.generation;
    this.target = this.select.value || this.target;
    const selected = this.targets.find(item => item.target === this.target);
    this.local = selected?.local === true; this.ready = false; this.loading = true;
    this.permissionRow.hidden = !this.local;
    this.status.textContent = selected ? `操作执行于：${selected.label} · ${this.local ? '本机' : '远程从机'}` : '原目标不可用，请刷新连接';
    this.setBusy(true); this.onChange(this);
    if (selected && !this.local && selected.pluginEnabled === false) {
      this.status.textContent='从机已安装此插件，请在从机开启「允许主控使用」';
    } else if (selected?.online) {
      const result = await this.call('capabilities');
      if (generation !== this.generation) return;
      this.ready = result.success && result.data.allowed;
      if (result.success) this.permission.checked = result.data.remoteAllowed;
      if (!this.ready) this.status.textContent = result.output || '从机未允许该工具，请在从机本机面板开启远程权限';
    }
    if (generation !== this.generation) return;
    this.loading = false; this.setBusy(false); await this.onChange(this);
  }
};

})();
