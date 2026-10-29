"""显式指定端口的实机验证：仅运行用户授权的只读 Linux 命令。"""
import argparse
import base64
import concurrent.futures
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from serial_terminal import SerialService
from extension_tools import ExtensionTools


def run(ports, output):
    output=Path(output); output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='serial-hardware-') as directory:
        service=SerialService(Path(directory)/'serial.json')
        registry=ExtensionTools({'serial':service})
        results=[]
        def call(action,args=None):return registry.call('serial',action,args or {})['data']
        try:
            for port in ports:
                try:
                    opened=call('open',{'port':port,'requestId':uuid.uuid4().hex})
                    results.append({'port':port,'opened':opened,'success':False})
                except Exception as exc:
                    results.append({'port':port,'success':False,'error':str(exc)})
            def exercise(record):
                if 'opened' not in record:return
                session=record['opened'];identifier=session['sessionId']
                nonce=uuid.uuid4().hex[:12]
                begin='SERIAL_CHECK_'+nonce+'_BEGIN';end='SERIAL_CHECK_'+nonce+'_END'
                command=f"printf '\\n{begin}\\n'; uname -s; printf '{end}\\n'\r"
                # 第二端用 HEX 发送相同只读命令，不把任意二进制写到真实板卡。
                mode='text' if ports.index(record['port'])==0 else 'hex'
                args={'sessionId':identifier,'generation':session['generation'],'format':mode,
                      'data':command if mode=='text' else command.encode('ascii').hex()}
                transcript=bytearray();cursor=0
                record['format']=mode;record['command']=command
                try:
                    written=call('write',args);record['writtenBytes']=written['writtenBytes']
                    deadline=time.monotonic()+12
                    while time.monotonic()<deadline:
                        response=call('read',{'sessionId':identifier,'cursor':cursor,'limit':65536})
                        transcript.extend(base64.b64decode(response['data']));cursor=response['nextCursor']
                        text=transcript.decode('utf-8',errors='replace').replace('\r','')
                        if re.search(r'(?:^|\n)'+begin+r'\nLinux\n'+end+r'(?:\n|$)',text):
                            record['success']=True;break
                        if response['state']!='running':raise RuntimeError(response['error'] or '串口已断开')
                        time.sleep(.05)
                    if not record['success']:record['error']='未得到完整Linux标记回复，请检查端口/波特率/登录状态'
                except Exception as exc:record['error']=str(exc)
                finally:
                    record['outputBase64']=base64.b64encode(transcript).decode('ascii')
                    record['outputText']=transcript.decode('utf-8',errors='replace')[-16000:]
                    call('close',{'sessionId':identifier})
                    record['released']=record['port'] not in service.ports
            # 确认两个物理端口能同时保持打开，再并行等待各自输出。
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(exercise,results))
            output.write_text(json.dumps({'baudrate':921600,'commands':'仅printf/uname -s',
                'devices':results},ensure_ascii=False,indent=2),encoding='utf-8')
            for record in results:
                print(f"[{'PASS' if record['success'] else 'FAIL'}] {record['port']} · {record.get('format','未打开')} · {record.get('error','Linux回复匹配；已释放')}")
            return all(record['success'] and record.get('released') for record in results)
        finally:service.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--ports',nargs='+',required=True)
    parser.add_argument('--output',required=True);args=parser.parse_args()
    sys.exit(0 if run(args.ports,args.output) else 1)
