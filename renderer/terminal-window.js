// 真正独立桌面窗口，主页面继续持有原会话与输入处理器。
(() => {
  const managers=new Map(), recipes=new Map(), closed=new Map(), histories=new Map();
  let refreshing=false;
  const changed=()=>{updateDeviceTabs();applyLayout(state.layout);updateLayoutStatus();updateStatusBar();};
  const base64=bytes=>{let value='';for(let i=0;i<bytes.length;i+=8192)value+=String.fromCharCode(...bytes.slice(i,i+8192));return btoa(value);};
  function record(key,term) {
    const h={chunks:[],size:0,seq:0,dropped:false};histories.set(key,h);
    const write=term.write.bind(term), clear=term.clear.bind(term);
    term.write=(data,callback)=>{
      const bytes=typeof data==='string'?new TextEncoder().encode(data):new Uint8Array(data);
      h.chunks.push(bytes);h.size+=bytes.length;h.seq++;
      while(h.size>1048576&&h.chunks.length){h.size-=h.chunks.shift().length;h.dropped=true;}
      const manager=managers.get(key);
      if(manager?.windowId&&manager.ready)manager.publish({kind:'data',data:base64(bytes),seq:h.seq});
      return write(data,callback);
    };
    term.writeln=(data='',callback)=>{
      if(typeof data==='string')return term.write(data+'\r\n',callback);
      const bytes=new Uint8Array(data.length+2);bytes.set(data);bytes.set([13,10],data.length);return term.write(bytes,callback);
    };
    term.clear=()=>{h.chunks=[];h.size=0;h.dropped=false;managers.get(key)?.publish({kind:'clear'});return clear();};
  }
  class TerminalWindow {
    constructor(key,panel,header,options={}) {
      Object.assign(this,{key,panel,header,options,windowId:null,ready:false,opening:false,gesture:null,popup:null,controlCounter:0});
      this.dockButton=document.createElement('button');
      Object.assign(this.dockButton,{className:'header-btn terminal-dock',title:'回到终端区',textContent:'↙',hidden:true});
      header.insertBefore(this.dockButton,header.querySelector('.minimize'));this.dockButton.onclick=()=>this.dock();
      this.onPointer=e=>{
        if(e.button!==0||this.windowId||this.opening||e.target.closest('button,input,select,a'))return;
        this.gesture={id:e.pointerId,x:e.clientX,y:e.clientY};
        try{header.setPointerCapture(e.pointerId);}catch{}
      };
      this.onMove=e=>{
        if(!this.gesture||e.pointerId!==this.gesture.id)return;
        if(!this.windowId){
          if(this.opening||Math.hypot(e.clientX-this.gesture.x,e.clientY-this.gesture.y)<8)return;
          this.detach(e.screenX,e.screenY);
        }else sendWS({type:'desktop-terminal-move',windowId:this.windowId,x:Math.round(e.screenX-120),y:Math.round(e.screenY-14)});
      };
      this.onEnd=()=>{this.gesture=null;};
      header.addEventListener('pointerdown',this.onPointer);document.addEventListener('pointermove',this.onMove);
      document.addEventListener('pointerup',this.onEnd);document.addEventListener('pointercancel',this.onEnd);
      header.classList.add('terminal-draggable');header.title='拖动标题栏可移出为独立桌面窗口';
      managers.set(key,this);document.getElementById('terminal-container').appendChild(panel);
      this.observer=new MutationObserver(records=>{
        if(records.some(r=>!(r.target.nodeType===1?r.target:r.target.parentElement)?.closest('.terminal-xterm'))){
          clearTimeout(this.snapshotTimer);this.snapshotTimer=setTimeout(()=>this.snapshot(),60);
        }
      });
      this.observer.observe(panel,{subtree:true,childList:true,attributes:true,characterData:true});
    }
    publish(payload){if(this.windowId&&this.ready)sendWS({type:'desktop-terminal-publish',windowId:this.windowId,payload});}
    snapshot(initial=false) {
      const t=state.terminals[this.key];if(!t||!this.windowId||!this.ready)return;
      const controls=[...this.panel.querySelectorAll('*')].filter(e=>!e.closest('.terminal-xterm'));
      // 标识不复用，旧视图的迟到点击不能落到另一个新控件上。
      controls.forEach(e=>{if(!e.dataset.desktopControl)e.dataset.desktopControl=String(++this.controlCounter);});
      const clone=this.panel.cloneNode(true);clone.querySelectorAll('.terminal-xterm').forEach(e=>e.replaceChildren());
      clone.classList.remove('native-detached','minimized');clone.style.cssText='';
      clone.querySelectorAll('script').forEach(e=>e.remove());
      const h=histories.get(this.key);
      this.publish({kind:'snapshot',html:clone.outerHTML,initial,controlRevision:this.controlRevision||0,controls:Object.fromEntries(controls.map(e=>[e.dataset.desktopControl,{value:e.value,checked:e.checked,disabled:e.disabled,selectionStart:e.selectionStart,selectionEnd:e.selectionEnd}])),
        readonly:t.term.options.disableStdin,fontSize:t.term.options.fontSize,theme:t.term.options.theme,cols:t.term.cols,rows:t.term.rows,
        ...(initial?{history:h?.chunks.map(base64)||[],seq:h?.seq||0,dropped:h?.dropped||false}:{})});
    }
    raise(){if(this.windowId)sendWS({type:'desktop-terminal-raise',windowId:this.windowId});else{selectActiveDevice(this.key);applyLayout(state.layout);}}
    async detach(x,y) {
      if(this.windowId||this.opening)return;
      this.opening=true;this.controlRevision=0;
      if(!window.pywebview)this.popup=window.open('about:blank','','popup,width=820,height=600');
      const rect=this.panel.getBoundingClientRect();
      const result=await callWS('desktop-terminal-open',{key:this.key,title:this.header.querySelector('.header-title').textContent,
        x:Math.round(x-120),y:Math.round(y-14),width:Math.round(this.options.width||Math.max(500,rect.width)),height:Math.round(this.options.height||Math.max(400,rect.height))});
      this.opening=false;
      if(!result.success||managers.get(this.key)!==this){
        if(result.success)sendWS({type:'desktop-terminal-destroy',windowId:result.data.windowId});
        this.closePopup();showToast('error','独立终端',result.output||'终端已关闭');return;
      }
      this.windowId=result.data.windowId;
      if(!result.data.native){
        if(!this.popup){await callWS('desktop-terminal-destroy',{windowId:this.windowId});this.windowId=null;showToast('warning','独立终端','浏览器阻止了新窗口，请允许弹窗后再拖出');return;}
        this.popup.location=result.data.url;
      }else this.closePopup();
      this.dockButton.hidden=false;this.panel.classList.add('native-detached');
      state.terminals[this.key].floatingWindow=this;
      sendWS({type:'desktop-terminal-bind',windowId:this.windowId});changed();
    }
    dock(fromChild=false) {
      if(!this.windowId)return;
      const id=this.windowId;this.windowId=null;this.ready=false;this.gesture=null;
      if(!fromChild)sendWS({type:'desktop-terminal-destroy',windowId:id});
      this.closePopup();this.panel.classList.remove('native-detached','minimized');this.dockButton.hidden=true;
      state.currentTab='terminal';
      $$('.tab-bar .tab').forEach(tab=>tab.classList.toggle('active',tab.dataset.tab==='terminal'));
      $$('.content-panel').forEach(panel=>panel.classList.toggle('active',panel.id==='panel-terminal'));
      if(state.terminals[this.key])state.terminals[this.key].floatingWindow=null;
      selectActiveDevice(this.key);changed();
    }
    toggleMinimize(){this.panel.classList.toggle('minimized');changed();}
    toggleMaximize(){if(this.panel.classList.contains('minimized'))this.toggleMinimize();selectActiveDevice(this.key);setLayout(state.layout==='single'?'grid':'single');}
    event(p) {
      const t=state.terminals[this.key];if(!t)return;
      if(p.kind!=='resize'){state.activeDevice=this.key;updateDeviceTabs();}
      if(p.kind==='input')t.nativeInput?.(p.data);
      else if(p.kind==='resize'){t.term.resize(Math.max(2,Math.min(1000,p.cols)),Math.max(2,Math.min(500,p.rows)));return;}
      else if(p.kind==='command-height')this.panel.querySelector('.terminal-cmd-input').style.height=Math.max(34,Math.min(500,p.height))+'px';
      else if(p.kind==='control'){
        if(Number.isSafeInteger(p.controlRevision)&&p.controlRevision>0)this.controlRevision=Math.max(this.controlRevision||0,p.controlRevision);
        const e=this.panel.querySelector('[data-desktop-control="'+Number(p.control)+'"]');if(!e||e.disabled)return;
        if('value' in p)e.value=p.value;if('checked' in p)e.checked=p.checked;
        // 先恢复子窗光标/选区，再执行命令框换行。
        if(typeof e.setSelectionRange==='function'&&Number.isInteger(p.selectionStart)&&Number.isInteger(p.selectionEnd))e.setSelectionRange(p.selectionStart,p.selectionEnd);
        if(p.event==='click')e.click();
        else if(p.event==='contextmenu'&&e.closest('.serial-command-popup'))e.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));
        else if(p.event==='dblclick')e.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,cancelable:true}));
        else if(p.event==='keydown')e.dispatchEvent(new KeyboardEvent('keydown',{...p.keys,bubbles:true,cancelable:true}));
        else if(['input','change'].includes(p.event))e.dispatchEvent(new Event(p.event,{bubbles:true}));
      }
      this.snapshot();
    }
    closePopup(){if(this.popup){try{this.popup.__desktopClosing=true;}catch{}this.popup.close();this.popup=null;}}
    destroy(){
      if(this.windowId)sendWS({type:'desktop-terminal-destroy',windowId:this.windowId});this.windowId=null;
      this.closePopup();this.observer.disconnect();clearTimeout(this.snapshotTimer);
      this.header.removeEventListener('pointerdown',this.onPointer);document.removeEventListener('pointermove',this.onMove);
      document.removeEventListener('pointerup',this.onEnd);document.removeEventListener('pointercancel',this.onEnd);
      managers.delete(this.key);histories.delete(this.key);recipes.delete(this.key);
    }
  }
  window.TerminalWindows={
    record,
    attach(key,panel,header,options){const m=new TerminalWindow(key,panel,header,options);if(state.terminals[key])state.terminals[key].windowManager=m;return m;},
    accept(msg){
      const m=[...managers.values()].find(m=>m.windowId===msg.windowId);if(!m)return;
      if(msg.type==='desktop-terminal-ready'){m.ready=true;m.snapshot(true);}
      else if(msg.type==='desktop-terminal-resync')m.snapshot(true);
      else if(msg.type==='desktop-terminal-event')m.event(msg.payload);
      else if(msg.type==='desktop-terminal-dock')m.dock(true);
      else if(msg.type==='desktop-terminal-closed')dismissTerminal(m.key);
    },
    remember(key,reopen,identity=key){
      recipes.set(key,{identity,reopen});
      // 同一目标重新打开后，旧会话的关闭记录不能再次制造窗口。
      closed.delete(identity);
    },
    dismiss(key,{restore=true}={}){
      const recipe=recipes.get(key);
      if(recipe){
        if(restore)closed.set(recipe.identity,recipe.reopen);
        else closed.delete(recipe.identity);
        recipes.delete(key);
      }
      while(closed.size>64)closed.delete(closed.keys().next().value);
    },
    async refreshClosed(){
      if(refreshing)return;refreshing=true;
      try{
        for(const[identity,reopen]of[...closed]){
          if(closed.get(identity)!==reopen)continue;
          try{
            // 等待期间可能又关闭了新会话，旧回执不能清除它的恢复记录。
            if(await reopen()&&closed.get(identity)===reopen)closed.delete(identity);
          }catch(e){showToast('warning','刷新终端',e.message||'目标不可用');}
        }
      }finally{refreshing=false;}
    },
    dockAll(){for(const m of managers.values())m.dock();},managers,closed
  };
})();
