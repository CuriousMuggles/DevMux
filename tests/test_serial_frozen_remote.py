"""双冻结EXE及指定COM端口的远程持续流，只发送只读Linux命令。"""
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import agent_client
import relay_app
from tests.test_serial_ui import exclusive_port
from websockets.sync.client import connect


def run(executable,port='COM27'):
    processes=[];evidence={'port':port,'baudrate':921600,'success':False};shared_temp=None
    with tempfile.TemporaryDirectory(prefix='serial-frozen-remote-',ignore_cleanup_errors=True) as temporary:
        root=Path(temporary);runtime=root/'runtime'
        try:
            share=os.environ.get('ADB_SERIAL_FROZEN_TEST_SHARE_ROOT')
            if share:
                shared_temp=tempfile.TemporaryDirectory(prefix='serial-frozen-receipt-',dir=share)
                assert Path(shared_temp.name).resolve().is_relative_to(Path(share).resolve())
            bus=Path(shared_temp.name) if shared_temp else root/'bus'
            settings={}
            for index,role in enumerate(['master','slave']):
                directory=root/role;directory.mkdir();config=directory/'relay.json'
                peer='serial-check-'+role
                relay_app.save_config(str(config),relay_app.RelayConfig(enabled=True,role=role,peerId=peer,
                    root=str(bus),heartbeatIntervalS=.5))
                (directory/'relay-serial.json').write_text(json.dumps({'remoteAllowed':role=='slave'}),encoding='utf-8')
                http=exclusive_port(19480+index*20);ws=exclusive_port(19780+index*20)
                env={**os.environ,'ADB_MANAGER_HEADLESS':'1','ADB_MANAGER_NO_LOCAL_DEVICES':'1',
                    'RELAY_CONFIG':str(config),'ADB_AGENT_RUNTIME_DIR':str(runtime),'HTTP_PORT':str(http),'WS_PORT':str(ws)}
                process=subprocess.Popen([str(Path(executable).resolve())],cwd=directory,env=env,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                processes.append(process);settings[role]=(peer,http,ws)
            with patch.dict(os.environ,{'ADB_AGENT_RUNTIME_DIR':str(runtime)}):
                deadline=time.monotonic()+30
                while len(agent_client.instances())!=2 and time.monotonic()<deadline:time.sleep(.1)
                records=agent_client.instances();assert len(records)==2,'双EXE Agent注册失败'
                master=next(r for r in records if r['wsPort']==settings['master'][2])
                with connect(f'ws://127.0.0.1:{settings["master"][2]}',origin=f'http://127.0.0.1:{settings["master"][1]}',proxy=None) as ws:
                    def ui(typ,**arguments):
                        identifier=uuid.uuid4().hex;ws.send(json.dumps(dict(type=typ,id=identifier,**arguments)))
                        while True:
                            response=json.loads(ws.recv(timeout=15))
                            if response.get('id')==identifier:return response
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        status=ui('relay-status')
                        if any(p.get('peerId')==settings['slave'][0] and p.get('online') for p in status.get('pairing',[])):break
                        time.sleep(.15)
                    result=ui('relay-confirm-peer',peerId=settings['slave'][0]);assert result['success'],result
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        status=ui('relay-status')
                        if any(p.get('peerId')==settings['slave'][0] and p.get('connectionState')=='connected' for p in status.get('peers',[])):break
                        time.sleep(.1)
                    async def exercise():
                        target=None
                        for _ in range(100):
                            targets=await agent_client.call('targets_list',{},instance=master['instanceId'])
                            target=next((t['target'] for t in targets.get('data',{}).get('targets',[]) if t.get('peerId')==settings['slave'][0] and t.get('online')),None)
                            if target:break
                            await asyncio.sleep(.1)
                        assert target,targets
                        async def call(action,args):
                            response=await agent_client.call('extension_call',dict(target=target,tool='serial',action=action,arguments=args),instance=master['instanceId'])
                            assert response.get('success') and response['data']['success'],response
                            return response['data']['data']
                        evidence['ports']=(await call('ports',{}))['ports']
                        try:opened=await call('open',{'port':port})
                        except AssertionError as exc:
                            if '无法打开 '+port in str(exc) and 'PermissionError(13' in str(exc):
                                evidence['skipped']=str(exc);return
                            raise
                        assert opened.get('serialStreamProtocol')==1,opened
                        identifier=opened['sessionId'];evidence['target']=target;evidence['serialStreamProtocol']=1
                        nonce=uuid.uuid4().hex[:12];begin='REMOTE_SERIAL_'+nonce+'_BEGIN';end='REMOTE_SERIAL_'+nonce+'_END'
                        command=f"printf '\\n{begin}\\n'; uname -s; printf '{end}\\n'\r"
                        evidence['command']=command;transcript=bytearray();cursor=0;start=time.monotonic()
                        try:
                            await call('write',dict(sessionId=identifier,generation=opened['generation'],format='hex',data=command.encode().hex()))
                            for _ in range(240):
                                chunk=await call('read',dict(sessionId=identifier,cursor=cursor,limit=65536))
                                transcript.extend(base64.b64decode(chunk['data']));cursor=chunk['nextCursor']
                                text=transcript.decode('utf-8',errors='replace').replace('\r','')
                                if re.search(r'(?:^|\n)'+begin+r'\nLinux\n'+end+r'(?:\n|$)',text):
                                    evidence.update(success=True,elapsedMs=round((time.monotonic()-start)*1000,1));break
                                await asyncio.sleep(.02)
                            assert evidence['success'],'远程串口没有完整Linux回复'
                        finally:
                            evidence['outputText']=transcript.decode('utf-8',errors='replace')[-16000:]
                            evidence['released']=(await call('close',{'sessionId':identifier}))['closed']
                    asyncio.run(exercise())
            if evidence.get('skipped'):
                print('[PASS] 双冻结EXE远程COM枚举/授权选路；[SKIP] COM27拒绝访问，未停止正式占用实例，实机持续流未验证')
                return None
            print('[PASS] 双冻结EXE→真实共享目录→COM27/921600持续流，HEX只读命令Linux回复；端口已释放')
            return True
        finally:
            output=ROOT/'.agent-runtime/terminal-followup';output.mkdir(parents=True,exist_ok=True)
            (output/'frozen-remote-com27.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
            for process in processes:
                if process.poll() is None:
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));process.wait(timeout=10)
            if shared_temp:shared_temp.cleanup()


if __name__=='__main__':sys.exit(0 if run(sys.argv[1]) is not False else 1)
