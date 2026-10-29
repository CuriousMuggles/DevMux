"""指定实机端口的冻结 Agent 测试，只发 HEX 编码的只读 Linux 命令。"""
import argparse
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
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import agent_client
from tests.test_serial_ui import exclusive_port


def run(executable,port,output):
    binary=Path(executable).resolve();output=Path(output)
    with tempfile.TemporaryDirectory(prefix='serial-real-agent-',ignore_cleanup_errors=True) as directory:
        runtime=Path(directory)
        env={**os.environ,'ADB_MANAGER_HEADLESS':'1','ADB_MANAGER_NO_LOCAL_DEVICES':'1',
            'RELAY_CONFIG':str(runtime/'relay.json'),'ADB_AGENT_RUNTIME_DIR':str(runtime/'runtime'),
            'HTTP_PORT':str(exclusive_port(19380)),'WS_PORT':str(exclusive_port(19650))}
        process=subprocess.Popen([str(binary)],cwd=runtime,env=env,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        result={'port':port,'baudrate':921600,'format':'hex','success':False}
        try:
            with patch.dict(os.environ,{'ADB_AGENT_RUNTIME_DIR':env['ADB_AGENT_RUNTIME_DIR']}):
                deadline=time.monotonic()+30
                while not agent_client.instances() and time.monotonic()<deadline:time.sleep(.1)
                assert len(agent_client.instances())==1,'测试EXE未注册'
                async def exercise():
                    async def call(action,args):
                        response=await agent_client.call('extension_call',{'target':'computer:local','tool':'serial',
                            'action':action,'arguments':args})
                        assert response['success'] and response['data']['success'],response
                        return response['data']['data']
                    opened=await call('open',{'port':port,'requestId':uuid.uuid4().hex})
                    result['config']=opened['config'];identifier=opened['sessionId']
                    nonce=uuid.uuid4().hex[:12];begin='FROZEN_SERIAL_'+nonce+'_BEGIN';end='FROZEN_SERIAL_'+nonce+'_END'
                    command=f"printf '\\n{begin}\\n'; uname -s; printf '{end}\\n'\r"
                    result['command']=command;transcript=bytearray();cursor=0
                    try:
                        written=await call('write',{'sessionId':identifier,'generation':opened['generation'],
                            'format':'hex','data':command.encode('ascii').hex()})
                        result['writtenBytes']=written['writtenBytes'];deadline=time.monotonic()+12
                        while time.monotonic()<deadline:
                            chunk=await call('read',{'sessionId':identifier,'cursor':cursor,'limit':65536})
                            transcript.extend(base64.b64decode(chunk['data']));cursor=chunk['nextCursor']
                            text=transcript.decode('utf-8',errors='replace').replace('\r','')
                            if re.search(r'(?:^|\n)'+begin+r'\nLinux\n'+end+r'(?:\n|$)',text):
                                result['success']=True;break
                            await asyncio.sleep(.05)
                        assert result['success'],'未收到完整Linux回复'
                    finally:
                        result['outputBase64']=base64.b64encode(transcript).decode()
                        result['outputText']=transcript.decode('utf-8',errors='replace')[-16000:]
                        closed=await call('close',{'sessionId':identifier});result['released']=closed['closed']
                asyncio.run(exercise())
            print(f'[PASS] 冻结 Agent/WS · {port} · 921600 · HEX只读命令与Linux回复 · 端口已释放')
            return True
        finally:
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            if process.poll() is None:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));process.wait(timeout=10)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--exe',required=True)
    parser.add_argument('--port',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    sys.exit(0 if run(args.exe,args.port,args.output) else 1)
