"""真实Edge独立串口窗口的快捷指令回归，使用假COM与独立配置。"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import server
from tests.test_serial_terminal import service
from tests.test_serial_ui import exclusive_port
from tests.test_ui_e2e import _edge_executable,_FakeWorker
from tests.edge_test_support import EdgeSession
from websockets.sync.client import connect


def exercise(endpoint):
 with connect(endpoint,proxy=None,max_size=20*1024*1024) as socket:
  counter=0
  def command(method,params=None,session=None):
   nonlocal counter
   counter+=1;payload={'id':counter,'method':method,'params':params or {}}
   if session:payload['sessionId']=session
   socket.send(json.dumps(payload))
   while True:
    result=json.loads(socket.recv(timeout=30))
    if result.get('id')==counter:
     assert not result.get('error'),result
     return result['result']
  def evaluate(expression,session=None):
   result=command('Runtime.evaluate',dict(expression=expression,awaitPromise=True,returnByValue=True),session)
   assert 'exceptionDetails' not in result,result
   return result['result'].get('value')
  def click(selector,session=None,button='left'):
   rect=evaluate("(()=>{const e=document.querySelector("+json.dumps(selector)+");e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()",session)
   command('Input.dispatchMouseEvent',dict(type='mousePressed',button=button,clickCount=1,**rect),session)
   command('Input.dispatchMouseEvent',dict(type='mouseReleased',button=button,clickCount=1,**rect),session)
  command('Runtime.enable');time.sleep(2)
  evaluate(r'''(async()=>{
   window.waitFor=async fn=>{for(let i=0;i<400;i++){if(fn())return;await new Promise(r=>setTimeout(r,20));}throw Error('快捷指令独立窗口等待超时');};
   await waitFor(()=>document.getElementById('window-serial')&&typeof ws!=='undefined'&&ws?.readyState===1);
   document.getElementById('window-serial').click();await waitFor(()=>!document.getElementById('serial-open').disabled);
   document.getElementById('serial-port').value='COM3';document.getElementById('serial-open').click();await waitFor(()=>SerialIntegration.windows.size===1);
   window.shortcutWindow=[...SerialIntegration.windows.values()][0];window.originalSession=shortcutWindow.sessionId;
   window.originalTerm=shortcutWindow.term;window.shortcutManager=state.terminals[shortcutWindow.panel.dataset.key].windowManager;
   state.terminals[shortcutWindow.panel.dataset.key].panel.querySelector('.serial-char-delay').value='0';

   return true;
  })()''')
  rect=evaluate("(()=>{const r=shortcutWindow.panel.querySelector('.terminal-panel-header').getBoundingClientRect();return {x:r.x+80,y:r.y+14};})()")
  command('Input.dispatchMouseEvent',dict(type='mousePressed',button='left',clickCount=1,**rect))
  for step in range(1,9):command('Input.dispatchMouseEvent',dict(type='mouseMoved',x=rect['x']+160*step/8,y=rect['y']+90*step/8,button='left',buttons=1))
  command('Input.dispatchMouseEvent',dict(type='mouseReleased',x=rect['x']+160,y=rect['y']+90,button='left',clickCount=1))
  identifier=evaluate("(async()=>{await waitFor(()=>shortcutManager.windowId);window.originalWindowId=shortcutManager.windowId;return originalWindowId;})()")
  target=None;deadline=time.monotonic()+10
  while not target and time.monotonic()<deadline:
   target=next((t for t in command('Target.getTargets')['targetInfos'] if '/desktop-terminal.html#'+identifier+':' in t['url']),None)
   if not target:time.sleep(.05)
  assert target,'独立窗口未建立'
  child=command('Target.attachToTarget',dict(targetId=target['targetId'],flatten=True))['sessionId']
  evaluate(r'''(async()=>{window.waitFor=async fn=>{for(let i=0;i<400;i++){if(fn())return;await new Promise(r=>setTimeout(r,20));}throw Error('子窗口等待超时');};await waitFor(()=>document.getElementById('desktop-message').hidden);return true;})()''',child)
  def retained():
   time.sleep(.15)
   assert evaluate('shortcutManager.windowId===originalWindowId&&shortcutWindow.sessionId===originalSession&&shortcutWindow.term===originalTerm'),'快捷指令操作使串口停靠或重建会话'
  click('.serial-shortcuts',child)
  evaluate("(async()=>{await waitFor(()=>document.querySelector('.serial-command-popup')?.style.display==='block');return true;})()",child);retained()
  click('.cmd-popup-add',child)
  # 旧代码此处dock并关闭子窗口；必须保持原窗口并在其中编辑。
  retained()
  evaluate("(async()=>{await waitFor(()=>document.querySelector('.serial-shortcut-dialog input'));return document.activeElement===document.querySelector('.serial-shortcut-dialog input');})()",child)
  assert evaluate("document.activeElement===document.querySelector('.serial-shortcut-dialog input')",child),'新增对话框未获得焦点'
  command('Input.insertText',{'text':'独立命令'},child)
  click('.serial-shortcut-dialog textarea',child)
  command('Input.insertText',{'text':'LINE1'},child)
  command('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13,text='\r'),child)
  command('Input.dispatchKeyEvent',dict(type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13),child)
  command('Input.insertText',{'text':'LINE2'},child)
  evaluate("(async()=>{await new Promise(r=>setTimeout(r,180));return true;})()",child)
  assert evaluate("document.querySelector('.serial-shortcut-dialog textarea').value",child)=='LINE1\nLINE2','多行编辑被截断或丢失'
  click('.serial-shortcut-dialog .serial-shortcut-save',child)
  evaluate("(async()=>{await waitFor(()=>!document.querySelector('.serial-shortcut-dialog')&&document.querySelector('.cmd-popup-btn'));return true;})()",child);retained()
  click('.cmd-popup-btn',child)
  time.sleep(.2);retained()
  click('.cmd-popup-btn',child,button='right')
  evaluate("(async()=>{await waitFor(()=>document.querySelector('.serial-shortcut-dialog'));return true;})()",child);retained()
  evaluate("(()=>{const e=document.querySelector('.serial-shortcut-dialog textarea');e.value='EDITED';e.dispatchEvent(new Event('input',{bubbles:true}));return true;})()",child)
  click('.serial-shortcut-save',child)
  evaluate("(async()=>{await waitFor(()=>!document.querySelector('.serial-shortcut-dialog'));return true;})()",child)
  click('.cmd-popup-btn',child);time.sleep(.2);retained()
  for selector,title in [('.group-add','新建串口指令集'),('.group-rename','指令集改名'),('.group-delete','删除指令集及全部指令')]:
   click(selector,child)
   evaluate("(async()=>{await waitFor(()=>document.querySelector('.serial-shortcut-dialog'));return true;})()",child);retained()
   assert evaluate("document.querySelector('.serial-shortcut-dialog h3').textContent",child)==title
   click('.serial-shortcut-cancel',child)
   evaluate("(async()=>{await waitFor(()=>!document.querySelector('.serial-shortcut-dialog'));return true;})()",child)
  click('.cmd-popup-btn',child,button='right')
  evaluate("(async()=>{await waitFor(()=>document.querySelector('.serial-shortcut-dialog'));return true;})()",child)
  click('.serial-shortcut-delete',child)
  evaluate("(async()=>{await waitFor(()=>!document.querySelector('.serial-shortcut-dialog')&&!document.querySelector('.cmd-popup-btn'));return true;})()",child);retained()
  click('.serial-shortcuts',child);retained()
  # 使用真实子窗键盘事件验证Ctrl+Enter选区换行及历史代理，不重建会话。
  click('.terminal-cmd-textarea',child)
  command('Input.insertText',{'text':'AXB'},child)
  evaluate("(()=>{const e=document.querySelector('.terminal-cmd-textarea');e.setSelectionRange(1,2);return true;})()",child)
  command('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13,modifiers=2),child)
  command('Input.dispatchKeyEvent',dict(type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13),child)
  evaluate("(async()=>{await waitFor(()=>document.querySelector('.terminal-cmd-textarea').value==='A\\nB');return true;})()",child)
  assert evaluate("document.querySelector('.terminal-cmd-textarea').selectionStart",child)==2,'独立命令框光标未保留'
  command('Input.dispatchKeyEvent',dict(type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13),child)
  command('Input.dispatchKeyEvent',dict(type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13),child)
  evaluate("(async()=>{await waitFor(()=>document.querySelector('.terminal-cmd-textarea').value==='');return true;})()",child)
  for key,code in [('ArrowUp',38),('ArrowDown',40)]:
   command('Input.dispatchKeyEvent',dict(type='keyDown',key=key,code=key,windowsVirtualKeyCode=code),child)
   command('Input.dispatchKeyEvent',dict(type='keyUp',key=key,code=key,windowsVirtualKeyCode=code),child)
   expected='A\nB' if key=='ArrowUp' else ''
   assert evaluate("(async()=>{await waitFor(()=>document.querySelector('.terminal-cmd-textarea').value==="+json.dumps(expected)+");return true;})()",child)
  retained()
  click('.terminal-dock',child)
  evaluate("(async()=>{await waitFor(()=>!shortcutManager.windowId);await shortcutWindow.close();return true;})()")
  return True


def run():
 edge=_edge_executable()
 if not edge:print('[SKIP] 未找到Edge');return None
 backend=browser=http=None;endpoint=None
 previous=(server.HTTP_PORT,server.WS_PORT)
 with tempfile.TemporaryDirectory(prefix='serial-detached-shortcuts-',ignore_cleanup_errors=True) as directory:
  runtime=Path(directory);fake,created=service(runtime/'serial.json')
  server.HTTP_PORT=exclusive_port(18580);server.WS_PORT=exclusive_port(18850)
  try:
   with patch.dict(os.environ,{'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),'ADB_MANAGER_NO_LOCAL_DEVICES':'1'}),patch.object(server,'ADBWorker',_FakeWorker),patch('serial_terminal.SerialService',side_effect=lambda path, *, config_storage=None:service(path, config_storage=config_storage)[0]),patch.object(server.ManagerServer,'_commands_path',return_value=str(runtime/'commands.json')):
    manager=server.ManagerServer();manager.serial=fake;manager.extension_tools.services['serial']=fake
    failures=[]
    def serve():
     try:asyncio.run(manager.run())
     except Exception as error:failures.append(repr(error))
    backend=threading.Thread(target=serve,daemon=True);backend.start()
    deadline=time.monotonic()+10
    while not getattr(manager,'_ws_port',0) and time.monotonic()<deadline:time.sleep(.05)
    assert getattr(manager,'_ws_port',0)
    http=server.start_http_server()
    browser=EdgeSession.start(edge,['--headless=new','--no-first-run','--remote-allow-origins=*',f'http://127.0.0.1:{server._HTTP_PORT}/'],runtime/'edge')
    endpoint=browser.wait_ready(20)
    assert exercise(endpoint)
    assert created[0][2].writes==[b'LINE1\rLINE2\r',b'EDITED\r',b'A\rB\r'],created[0][2].writes
    assert created[0][2].closed and not failures,failures
    print('[PASS] 独立串口快捷指令：展开/发送/多行新增/右键编辑删除/指令集操作均保持同窗口同会话，显式停靠有效')
    return True
  finally:
   if browser:browser.close()
   if backend:manager.stop();backend.join(timeout=15)
   if http:http.shutdown();http.server_close()
   fake.close();server.HTTP_PORT,server.WS_PORT=previous
if __name__=='__main__':sys.exit(0 if run() else 1)
