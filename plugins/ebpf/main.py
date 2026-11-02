"""Collector packaging and strict command planning; device IO belongs to the host."""
import base64
import hashlib
import re
import uuid
import json
from plugin_sdk import Plugin
from evidence import analyze_latency, resolve_kernel
from elf_symbols import ELF
from session_report import correlated_report


class EBPF(Plugin):
    def call(self, action, arguments):
        if action in ('symbol-plan','symbol-result'):
            pid=arguments.get('pid');addresses=arguments.get('addresses')
            kind=arguments.get('kind','user')
            if kind not in ('user','kernel'):raise ValueError('符号类型无效')
            if type(pid) is not int or not 1<=pid<=4194304:raise ValueError('PID 无效')
            if not isinstance(addresses,list) or not 1<=len(addresses)<=127 or any(
                not isinstance(a,str) or not re.fullmatch('0x[0-9a-fA-F]{1,16}',a) for a in addresses):
                raise ValueError('符号地址列表无效')
            if action=='symbol-plan':
                digest=arguments.get('sha256')
                if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('摘要无效')
                path='/tmp/devmux-ebpf-'+digest[:24]
                flag='--kernel ' if kind=='kernel' else ''
                return dict(path=path,timeout=8,command=f'(chmod 700 {path} && timeout 5 {path} {pid} '+flag+' '.join(addresses)+')')
            text=arguments.get('text')
            if not isinstance(text,str) or len(text)>65536:raise ValueError('符号结果过大或无效')
            report=json.loads(text)
            if not isinstance(report,dict):raise ValueError('符号结果必须为对象')
            frames=report.get('frames')
            if report.get('schemaVersion')!=1 or report.get('pid')!=pid or not isinstance(frames,list) or len(frames)!=len(addresses):
                raise ValueError('符号结果与请求不匹配')
            allowed={'resolved','mapping-unavailable','file-identity-unavailable','file-too-large',
                'file-read-failed','unsupported-elf','invalid-elf','mapping-offset-mismatch',
                'no-function-symbol','no-symbol-table','identity-changed',
                'kernel-symbol-unavailable','kernel-address-unmapped','kernel-scan-incomplete'}
            for address,frame in zip(addresses,frames):
                if (not isinstance(frame,dict) or not isinstance(frame.get('address'),str) or
                    not re.fullmatch('0x[0-9a-fA-F]{1,16}',frame['address']) or int(frame['address'],16)!=int(address,16) or
                    frame.get('status') not in allowed or type(frame.get('offset')) is not int or frame['offset']<0):
                    raise ValueError('符号帧无效')
                if frame['status']=='resolved':
                    if not isinstance(frame.get('symbol'),str) or not 1<=len(frame['symbol'])<=256:raise ValueError('函数名无效')
                elif frame.get('symbol') is not None:raise ValueError('未解析帧不能声明函数名')
            return dict(frames=frames,resolvedFrames=sum(f['status']=='resolved' for f in frames))
        if action=='asset' and arguments.get('mode')=='symbols':
            binary=(self.root/'bin'/'symbols').read_bytes()
            return dict(data=base64.b64encode(binary).decode(),sha256=hashlib.sha256(binary).hexdigest())
        if action == 'symbol-release':
            getattr(self,'symbol_uploads',{}).pop(arguments.get('token'),None)
            return {}
        if action == 'symbol-chunk':
            if not hasattr(self,'symbol_uploads'):self.symbol_uploads={}
            token=arguments.get('token')
            if token is None:
                if len(self.symbol_uploads)>=8:raise ValueError('符号上传数量达到上限')
                token=uuid.uuid4().hex;self.symbol_uploads[token]=bytearray()
            if token not in self.symbol_uploads:raise ValueError('符号上传已失效')
            chunk=arguments.get('data')
            if not isinstance(chunk,str) or len(chunk)>65536:raise ValueError('符号分块无效')
            buffer=self.symbol_uploads[token]
            decoded=base64.b64decode(chunk,validate=True)
            if len(buffer)+len(decoded)>6*1024*1024:raise ValueError('符号文件超过 6 MiB')
            buffer.extend(decoded)
            return {'token':token,'bytes':len(buffer)}
        if action == 'parse-session':
            return correlated_report(arguments.get('report'))
        if action == 'analyze':
            return analyze_latency(arguments.get('result', {}))
        if action == 'resolve-kernel':
            return resolve_kernel(arguments.get('addresses'), arguments.get('symbols'))
        if action == 'resolve-user':
            token=arguments.get('token')
            encoded=arguments.get('elf')
            if token is not None:
                binary=bytes(getattr(self,'symbol_uploads',{}).get(token,b''))
                if not binary:raise ValueError('符号上传已失效')
                encoded=base64.b64encode(binary).decode()
            if not isinstance(encoded,str) or len(encoded)>8*1024*1024:
                raise ValueError('符号文件过大或无效')
            binary=base64.b64decode(encoded,validate=True)
            expected_digest=arguments.get('sha256')
            if expected_digest is not None and (not isinstance(expected_digest,str) or
                    not re.fullmatch('[0-9a-f]{64}',expected_digest) or
                    hashlib.sha256(binary).hexdigest()!=expected_digest):
                raise ValueError('符号文件与设备文件摘要不匹配')
            elf=ELF(binary)
            addresses=arguments.get('addresses');mapping=arguments.get('mapping')
            if not isinstance(addresses,list) or len(addresses)>127 or not isinstance(mapping,dict):
                raise ValueError('用户栈与映射无效')
            output=[]
            for address in addresses:
                if not isinstance(address,str) or not re.fullmatch('0x[0-9a-fA-F]{1,16}',address):
                    raise ValueError('用户栈地址无效')
                output.append(elf.resolve_mapping(int(address,16),mapping,
                    arguments.get('buildId') or (elf.build_id if expected_digest else None)))
            return {'frames':output,'buildId':elf.build_id,
                    'resolvedFrames':sum(f['status']=='resolved' for f in output)}
        mode = arguments.get('mode', 'latency')
        if mode not in ('latency', 'kernel', 'user', 'session'):
            raise ValueError('采集模式无效')
        if action == 'asset':
            binary = (self.root / 'bin' / (mode if mode in ('latency','session') else 'stack')).read_bytes()
            return {'data': base64.b64encode(binary).decode(),
                    'sha256': hashlib.sha256(binary).hexdigest()}
        if action == 'plan':
            pid, duration = arguments.get('pid'), arguments.get('duration')
            if type(pid) is not int or not 1 <= pid <= 4194304:
                raise ValueError('PID 无效')
            if type(duration) is not int or not 1 <= duration <= 30:
                raise ValueError('采集时长须为 1–30 秒')
            digest = arguments.get('sha256')
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError('摘要无效')
            path = '/tmp/devmux-ebpf-' + digest[:24]
            ticket = '/tmp/devmux-ebpf-run-' + uuid.uuid4().hex
            suffix = '' if mode == 'latency' else ' ' + mode
            session_id = uuid.uuid4().hex
            if mode == 'session':
                suffix = ' 2000000 ' + session_id
            command = (f'chmod 700 {path} || exit 1; timeout {duration+5} {path} {pid} {duration}{suffix} & '
                       f'child=$!; echo $child > {ticket}; wait $child; code=$?; rm -f {ticket}; exit $code')
            # Keep exit inside a subshell: the host appends its own exit marker.
            command = '(' + command + ')'
            # The PID file identifies the timeout supervisor for this invocation.
            cancel = f'if test -f {ticket}; then kill -TERM $(cat {ticket}); fi'
            return {'command': command, 'cancel': cancel, 'timeout': duration+15,
                    'path': path, 'ticket': ticket, 'sessionId': session_id}
        if action == 'parse':
            text = arguments.get('text')
            if not isinstance(text, str) or len(text) > 1048576:
                raise ValueError('结果过大或无效')
            if mode == 'session':
                source=json.loads(text)
                report=correlated_report(source)
                buckets=source.get('latencyBuckets')
                if (not isinstance(buckets,list) or len(buckets)!=32 or
                    any(type(n) is not int or n<0 for n in buckets) or
                    sum(buckets)!=report['quality']['completedCalls'] or source.get('bucketWidthNs')!=65536):
                    raise ValueError('联合采集分桶与完成计数不一致')
                return dict(mode=mode,samples=sum(buckets),buckets=buckets,bucketWidthNs=65536,
                            session=report,raw=text)
            if mode == 'latency':
                buckets = [0]*32
                for index, count in re.findall(r'bucket=(\d+) .*?: (\d+)', text):
                    if int(index) >= 32:
                        raise ValueError('分桶下标无效')
                    buckets[int(index)] += int(count)
                total = re.search(r'samples=(\d+)', text)
                if not total or sum(buckets) != int(total[1]):
                    raise ValueError('采集结果不完整或计数不一致')
                return {'mode': mode, 'samples': int(total[1]), 'buckets': buckets,
                        'bucketWidthNs': 65536, 'raw': text}
            frames = re.findall(r'^frame=([0-9a-f]{16})$', text, re.M)
            if not frames:
                raise ValueError('未采到有效栈；进程可能没有系统调用或不支持栈展开')
            symbols = [None] * len(frames)
            for index, name, offset in re.findall(r'^symbol=(\d+) (\S+) (0x[0-9a-f]+)$', text, re.M):
                if mode != 'kernel' or int(index) >= len(frames):
                    raise ValueError('符号记录与调用栈不匹配')
                symbols[int(index)] = {'name': name, 'offset': int(offset,16)}
            return {'mode': mode, 'frames': ['0x'+item for item in frames], 'symbols': symbols,
                    'resolvedFrames': sum(item is not None for item in symbols), 'raw': text}
        raise ValueError('未知动作')


def create_plugin(root):
    return EBPF(root)
