// 独立视图仅代理原会话的显示和UI事件，不能新开后台会话。
(() => {
  const [windowId,token]=location.hash.slice(1).split(':');
  const host=document.getElementById('desktop-host'),message=document.getElementById('desktop-message');
  const ws=new WebSocket('ws://127.0.0.1:'+WS_PORT+'/desktop-terminal');
  let term,fit,panel,seq=0,closing=false,resizeTimer,applying=false,controlRevision=0;
  const send=payload=>{if(ws.readyState===1&&!closing)ws.send(JSON.stringify({type:'desktop-terminal-event',payload}));};
  const action=value=>{if(ws.readyState===1)ws.send(JSON.stringify({type:'desktop-terminal-action',action:value}));if(['dock','close'].includes(value))closing=true;};
  const bytes=data=>Uint8Array.from(atob(data),c=>c.charCodeAt(0));
  function resize(){clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(term&&host.clientHeight){fit.fit();send({kind:'resize',cols:term.cols,rows:term.rows});}},80);}
  function syncView(current,fresh){
    if(current.nodeType!==Node.ELEMENT_NODE){if(current.nodeValue!==fresh.nodeValue)current.nodeValue=fresh.nodeValue;return;}
    // xterm保持连接，状态同步不能中断其焦点、选区或输入法组合。
    if(current.classList.contains('terminal-xterm'))return;
    for(const attribute of [...current.attributes])if(!fresh.hasAttribute(attribute.name))current.removeAttribute(attribute.name);
    for(const attribute of [...fresh.attributes])if(current.getAttribute(attribute.name)!==attribute.value)current.setAttribute(attribute.name,attribute.value);
    const previous=[...current.childNodes],used=new Set();
    const key=node=>node.nodeType===Node.ELEMENT_NODE?(node.classList.contains('terminal-xterm')?'xterm':node.dataset.desktopControl||''):'';
    [...fresh.childNodes].forEach((desired,index)=>{
      let node=previous.find(candidate=>!used.has(candidate)&&candidate.nodeType===desired.nodeType&&candidate.nodeName===desired.nodeName&&key(candidate)===key(desired));
      const reused=!!node;
      if(!node)node=desired.cloneNode(true);
      used.add(node);
      if(node!==current.childNodes[index])current.insertBefore(node,current.childNodes[index]||null);
      if(reused)syncView(node,desired);
    });
    for(const node of previous)if(!used.has(node))node.remove();
  }
  function snapshot(data){
    applying=true;
    const previousDialog=panel?.querySelector('.serial-shortcut-dialog');
    const previousDialogId=previousDialog?.dataset.desktopControl;
    const terminalFocused=!!term&&document.activeElement===term.textarea;
    const focused=document.activeElement?.dataset.desktopControl;
    let selection=document.activeElement?.selectionStart,selectionEnd=document.activeElement?.selectionEnd;
    const template=document.createElement('template');template.innerHTML=data.html;
    const fresh=template.content.firstElementChild;
    if(panel)syncView(panel,fresh);
    else{host.replaceChildren(fresh);panel=fresh;}
    panel.querySelector('.terminal-panel-header').classList.add('pywebview-drag-region');
    panel.querySelectorAll('[data-desktop-control]').forEach(e=>{
      const value=data.controls[Number(e.dataset.desktopControl)];if(!value)return;
      // 未确认的本地草稿和输入法组合不能被较早的镜像快照覆盖。
      const localDraft=e===document.activeElement&&(data.controlRevision||0)<controlRevision;
      if(value.value!==undefined&&e.value!==value.value&&!localDraft&&!window.CommandColumnEditor?.isComposing(e)){
        e.value=value.value;
        // 已确认的换行/历史替换使用主窗口计算后的光标，普通快照保留本地选区。
        if(e.dataset.desktopControl===focused&&Number.isInteger(value.selectionStart)&&Number.isInteger(value.selectionEnd)){
          selection=value.selectionStart;selectionEnd=value.selectionEnd;
        }
      }
      if(value.checked!==undefined)e.checked=value.checked;
      if(value.disabled!==undefined)e.disabled=value.disabled;
    });
    if(!term&&!data.controlsOnly){
      term=new Terminal({fontSize:data.fontSize,theme:data.theme,convertEol:false,cursorBlink:true,scrollback:5000});
      fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open(panel.querySelector('.terminal-xterm'));
      // 设备查询只由原xterm回复；显示历史与镜像输出不能变成第二份设备输入。
      for(const id of [{final:'c'},{prefix:'>',final:'c'},{prefix:'=',final:'c'},
        {final:'n'},{prefix:'?',final:'n'},{final:'t'}])term.parser.registerCsiHandler(id,()=>true);
      term.parser.registerEscHandler({final:'Z'},()=>true);
      term.parser.registerDcsHandler({intermediates:'$',final:'q'},()=>true);
      for(const id of [4,10,11,12])term.parser.registerOscHandler(id,value=>value.includes('?'));
      term.onData(value=>{
        if(panel.querySelector('.serial-format')?.value==='hex'){panel.querySelector('.terminal-cmd-textarea')?.focus();return;}
        send({kind:'input',data:value});
      });
      panel.querySelector('.terminal-xterm').addEventListener('contextmenu',async e=>{
        e.preventDefault();try{const value=await navigator.clipboard.readText();if(value&&!term.options.disableStdin)send({kind:'input',data:value});}catch{}
      });
      term.onSelectionChange(()=>{const value=term.getSelection();if(value)navigator.clipboard?.writeText(value).catch(()=>{});});
    }
    if(term){
    term.options.fontSize=data.fontSize;term.options.disableStdin=!!data.readonly;
    if(data.initial){term.reset();term.resize(data.cols,data.rows);if(data.dropped)term.writeln('[较早显示历史已裁剪]');for(const chunk of data.history)term.write(bytes(chunk));seq=data.seq;}
    if(data.initial||(terminalFocused&&document.activeElement!==term.textarea))term.focus();
    }
    const dialog=panel.querySelector('.serial-shortcut-dialog');
    if(dialog&&dialog.dataset.desktopControl!==previousDialogId)dialog.querySelector('input,textarea')?.focus();
    else if(previousDialog&&!dialog)term.focus();
    else if(focused){const element=panel.querySelector('[data-desktop-control="'+focused+'"]');element?.focus();if(typeof selection==='number'&&!window.CommandColumnEditor?.isComposing(element))try{element.setSelectionRange(selection,selectionEnd);}catch{}}
    message.hidden=true;applying=false;resize();
  }
  ws.onopen=()=>ws.send(JSON.stringify({type:'desktop-terminal-attach',windowId,token}));
  ws.onmessage=e=>{
    const msg=JSON.parse(e.data);if(msg.type!=='desktop-terminal-view')return;
    const data=msg.payload;
    if(data.kind==='snapshot')snapshot(data);
    else if(data.kind==='data'&&term&&data.seq>seq){term.write(bytes(data.data));seq=data.seq;}
    else if(data.kind==='clear')term?.clear();
  };
  ws.onclose=e=>{if(closing||e.reason==='独立视图已回收'){closing=true;window.close();return;}message.hidden=false;message.textContent='主应用连接已结束，旧输入不会重发。';if(term)term.options.disableStdin=true;};
  window.addEventListener('resize',resize);
  window.addEventListener('beforeunload',()=>{if(!closing&&!window.__desktopClosing)action('close');});
  document.addEventListener('click',e=>{
    if(e.target.closest('.terminal-dock')){action('dock');return;}
    if(e.target.closest('.terminal-panel-header .close')){action('close');return;}
    if(e.target.closest('.terminal-panel-header .minimize')){action('minimize');return;}
    if(e.target.closest('.terminal-panel-header .maximize')){action('maximize');return;}
    if(e.target.closest('.terminal-xterm,input,select,textarea,label'))return;
    const element=e.target.closest('[data-desktop-control]');if(!element||element.disabled)return;
    // 浏览器回退视图在用户点击时预留命名小窗，主页面代理后可直接复用。
    if(element.dataset.settingsPopup&&!window.pywebview)window.open('about:blank',element.dataset.settingsPopup,'popup,width=520,height=600');
    e.preventDefault();send({kind:'control',event:'click',control:element.dataset.desktopControl});
  });
  document.addEventListener('contextmenu',e=>{const element=e.target.closest('.serial-command-popup .cmd-popup-btn[data-desktop-control]');if(element){e.preventDefault();send({kind:'control',event:'contextmenu',control:element.dataset.desktopControl});}});
  document.addEventListener('dblclick',e=>{if(e.target.closest('.terminal-xterm'))return;const element=e.target.closest('[data-desktop-control]');if(element)send({kind:'control',event:'dblclick',control:element.dataset.desktopControl});});
  for(const type of ['input','change','keydown'])document.addEventListener(type,e=>{
    if(applying||e.target.closest('.terminal-xterm'))return;
    const element=e.target.closest('[data-desktop-control]');if(!element||element.disabled)return;
    // 编辑器保留原生多行与焦点导航，值通过input/change代理同步。
    if(type==='keydown'&&(e.isComposing||e.keyCode===229||e.target.closest('.serial-shortcut-dialog')))return;
    if(type==='keydown'&&!['Enter','ArrowUp','ArrowDown','Escape'].includes(e.key))return;
    if(type==='keydown'&&(e.key==='Enter'||((e.key==='ArrowUp'||e.key==='ArrowDown')&&!e.ctrlKey&&!e.shiftKey)))e.preventDefault();
    send({kind:'control',event:type,control:element.dataset.desktopControl,value:element.value,checked:element.checked,selectionStart:element.selectionStart,selectionEnd:element.selectionEnd,controlRevision:++controlRevision,
      keys:type==='keydown'?{key:e.key,code:e.code,ctrlKey:e.ctrlKey,shiftKey:e.shiftKey,altKey:e.altKey}:undefined});
  });
  let drag;
  document.addEventListener('pointerdown',e=>{
    if(!e.target.closest('.cell-toolbar-handle'))return;
    const command=panel.querySelector('.terminal-cmd-input');drag={y:e.clientY,height:command.offsetHeight};e.preventDefault();
  });
  document.addEventListener('pointermove',e=>{if(drag){const height=Math.max(34,Math.min(500,drag.height+drag.y-e.clientY));panel.querySelector('.terminal-cmd-input').style.height=height+'px';send({kind:'command-height',height});resize();}});
  document.addEventListener('pointerup',()=>{drag=null;});
})();
