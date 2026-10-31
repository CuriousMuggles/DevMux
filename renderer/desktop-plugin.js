(() => {
  const [windowId,token]=location.hash.slice(1).split(':');
  const ws=new WebSocket('ws://127.0.0.1:'+WS_PORT+'/desktop-terminal');
  let frame;
  ws.onopen=()=>ws.send(JSON.stringify({type:'desktop-terminal-attach',windowId,token}));
  ws.onmessage=event=>{
    const message=JSON.parse(event.data),payload=message.payload;
    if(message.type!=='desktop-terminal-view'||!payload)return;
    if(payload.kind==='plugin-page'){
      frame=document.createElement('iframe');frame.setAttribute('sandbox','allow-scripts');frame.title=document.title;
      frame.onload=()=>document.body.dataset.pluginReady='true';frame.srcdoc=payload.html;document.body.replaceChildren(frame);
    }else if(payload.kind==='plugin-result')frame?.contentWindow.postMessage({type:'devmux-plugin-result',id:payload.id,result:payload.result},'*');
  };
  window.addEventListener('message',event=>{
    if(event.source!==frame?.contentWindow||ws.readyState!==1)return;
    if(event.data?.type==='devmux-plugin-close'){ws.send(JSON.stringify({type:'desktop-terminal-action',action:'close'}));return;}
    if(!['devmux-plugin-call','devmux-host-call'].includes(event.data?.type))return;
    ws.send(JSON.stringify({type:'desktop-terminal-event',payload:{request:event.data}}));
  });
  ws.onclose=()=>{document.body.innerHTML='<div id="message">插件窗口连接已结束，请从工具菜单重新打开。</div>';};
  window.addEventListener('beforeunload',()=>{if(ws.readyState===1)ws.send(JSON.stringify({type:'desktop-terminal-action',action:'close'}));});
})();
