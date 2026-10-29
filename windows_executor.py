"""Windows PowerShell 执行、交互及可恢复任务；不依赖 GUI 或中转。"""
import base64
import codecs
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
import hashlib
import getpass

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
ID_RE = re.compile(r'^[a-f0-9]{32}$')
FINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'}
ANSI_RE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


class OutputBuffer:
    """有界字节历史；原始数据和文本分别提供，游标不会因裁剪归零。"""
    def __init__(self, limit=1024 * 1024):
        self.limit = limit
        self.data = bytearray()
        self.total = 0
        self.lock = threading.RLock()

    def append(self, data):
        with self.lock:
            self.data.extend(data)
            self.total += len(data)
            if len(self.data) > self.limit:
                del self.data[:-self.limit]

    def read(self, cursor=0, limit=32768, final=False):
        cursor, limit = int(cursor), int(limit)
        if cursor < 0 or not 1 <= limit <= 65536:
            raise ValueError('输出游标或读取长度无效')
        with self.lock:
            start = self.total - len(self.data)
            if cursor > self.total:
                raise ValueError('输出游标超过当前长度')
            position = max(start, cursor)
            data = bytes(self.data[position-start:position-start+limit])
            # 完整字节仍返回；文本不消费末尾不完整的 UTF-8 字符。
            decoder = codecs.getincrementaldecoder('utf-8')('replace')
            text = decoder.decode(data, final=final and position+len(data) == self.total)
            pending = len(decoder.getstate()[0])
            if pending and len(data) == limit and len(data) == pending:
                raise ValueError('读取长度不足以容纳 UTF-8 字符')
            consumed = len(data) - pending
            return dict(text=ANSI_RE.sub('', text).replace('\r', ''), data=base64.b64encode(data[:consumed]).decode('ascii'),
                        nextCursor=position+consumed, hasMore=position+consumed < self.total,
                        droppedBytes=max(0, start-cursor), totalBytes=self.total)


class ProcessTree:
    """在脚本开始前加入 Job Object，避免取消时遗留子进程。"""
    def __init__(self, proc):
        self.proc, self.handle = proc, None
        if os.name != 'nt':
            return
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        class BasicLimits(ctypes.Structure):
            _fields_ = [('processTime', ctypes.c_longlong), ('jobTime', ctypes.c_longlong),
                ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t),
                ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class Limits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', ctypes.c_ulonglong * 6),
                ('processMemory', ctypes.c_size_t), ('jobMemory', ctypes.c_size_t),
                ('peakProcessMemory', ctypes.c_size_t), ('peakJobMemory', ctypes.c_size_t)]
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE，崩溃也回收子进程。
        if (not self.handle or not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(self.handle, int(proc._handle))):
            if self.handle:
                kernel.CloseHandle(self.handle)
            self.handle = None
            proc.kill()
            raise OSError(ctypes.get_last_error(), '无法建立 Windows 进程树隔离')

    def kill(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
        elif self.proc.poll() is None:
            self.proc.kill()

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _windows_directory():
    """优先询问系统，避免启动器缺少 SystemRoot 环境变量。"""
    if os.name == 'nt':
        try:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            query = kernel.GetWindowsDirectoryW
            query.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
            query.restype = ctypes.c_uint
            buffer = ctypes.create_unicode_buffer(32768)
            length = query(buffer, len(buffer))
            if 0 < length < len(buffer):
                return buffer.value
        except OSError:
            pass
    return os.environ.get('SystemRoot') or os.environ.get('WINDIR') or ''


def _find_powershell(shell):
    """能力探测与实际启动共用定位；不自动切换 Windows PowerShell / pwsh。"""
    if shell not in ('powershell', 'pwsh'):
        raise ValueError('仅支持 powershell 或 pwsh')
    executable = shutil.which(shell) or shutil.which(shell + '.exe')
    if executable:
        return executable
    candidates = []
    if os.name == 'nt':
        if shell == 'powershell':
            directory = _windows_directory()
            if directory and Path(directory).is_absolute():
                candidates = [Path(directory) / part / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
                              for part in ('System32', 'Sysnative', 'SysWOW64')]
        else:
            for name in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)'):
                directory = os.environ.get(name)
                if directory and Path(directory).is_absolute():
                    candidates.append(Path(directory) / 'PowerShell' / '7' / 'pwsh.exe')
    return next((str(path) for path in candidates if path.is_file()), None)


def powershell(shell='powershell'):
    executable = _find_powershell(shell)
    if not executable:
        raise RuntimeError(f'未找到可用的 {shell}.exe（已检查 PATH 和标准安装目录）；'
                           '请检查执行端运行账户及安装路径')
    return executable


class WindowsService:
    def __init__(self, directory, max_active=8):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.jobs = {}
        self.sessions = {}
        self.max_active = max_active
        self.closed = False
        self._session_stop = threading.Event()
        self._watchdog = None
        for path in self.directory.glob('*.json'):
            try:
                info = json.loads(path.read_text(encoding='utf-8'))
                if not ID_RE.fullmatch(info.get('jobId', '')) or not isinstance(info.get('owner'), str):
                    continue
                if info['state'] not in FINAL:
                    info.update(state='interrupted', error='执行端已重启，禁止自动重复执行')
                self.jobs[info['jobId']] = dict(info=info, proc=None, cancel=threading.Event(),
                    stdout=OutputBuffer(), stderr=OutputBuffer(), tree=None)
            except (OSError, ValueError, KeyError):
                continue

    def _save(self, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        identifier = record['info']['jobId']
        path = self.directory / (identifier + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record['info'], ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

    def _owned(self, identifier, owner, collection=None):
        records = self.jobs if collection is None else collection
        record = records.get(str(identifier))
        if not record or record['info']['owner'] != owner:
            raise PermissionError('任务或会话不存在，或不属于当前调用者')
        return record

    def start(self, args, owner):
        script = args.get('script')
        if not isinstance(script, str) or not script.strip() or len(script) > 256 * 1024:
            raise ValueError('PowerShell 脚本不能为空且不能超过 256 KiB')
        timeout = float(args.get('timeout', 30))
        if not 0 < timeout <= 86400:
            raise ValueError('执行超时范围为 0 到 86400 秒')
        cwd = str(args.get('cwd') or '')
        if cwd and not Path(cwd).is_dir():
            raise ValueError('执行目录不存在')
        executable = powershell(args.get('shell', 'powershell'))
        identifier = str(args.get('jobId') or uuid.uuid4().hex)
        if not ID_RE.fullmatch(identifier):
            raise ValueError('jobId 必须是 32 位小写十六进制标识')
        with self.lock:
            if self.closed:
                raise RuntimeError('执行服务已关闭')
            if identifier in self.jobs:
                return dict(self._owned(identifier, owner)['info'])
            if len(self.jobs) >= 512:
                raise RuntimeError('历史任务已达到 512 条，请停用执行服务后归档本机任务目录')
            if sum(r['info']['state'] not in FINAL for r in self.jobs.values()) >= self.max_active:
                raise RuntimeError('运行任务数已达到上限')
            record = dict(info=dict(jobId=identifier, owner=owner, state='queued',
                exitCode=None, startedAt=time.time(), finishedAt=None, cwd=cwd,
                shell=args.get('shell', 'powershell'), account=getpass.getuser(),
                scriptSha256=hashlib.sha256(script.encode('utf-8')).hexdigest(), error=''), proc=None,
                cancel=threading.Event(), stdout=OutputBuffer(), stderr=OutputBuffer(), tree=None)
            self.jobs[identifier] = record
            try:
                self._save(record)
            except Exception:
                self.jobs.pop(identifier, None)
                raise
            threading.Thread(target=self._execute, args=(record, executable, script, cwd, timeout),
                             daemon=True, name='windows-job-'+identifier[:8]).start()
            return dict(record['info'])

    def _execute(self, record, executable, script, cwd, timeout):
        # 用户脚本以明文写入临时 .ps1（UTF-8 BOM）后交给 -File 执行，不再使用
        # -EncodedCommand：安全软件把「Base64 编码的 PowerShell 命令」作为运行时
        # 行为评分的高权重信号（ATC.SuspiciousBehavior 复现均落在独立任务路径）。
        # 门闩语义保留：脚本先等待门闩文件出现，父进程在 Job Object 进程树隔离
        # 就绪后才放行，保证用户脚本不会在隔离建立前启动子进程。
        gate = self.directory / (record['info']['jobId'] + '.gate')
        script_path = self.directory / (record['info']['jobId'] + '.ps1')
        wrapper = ("$__adm_gate=$env:ADM_GATE; $__adm_n=0; "
                   "while($__adm_gate -and -not (Test-Path -LiteralPath $__adm_gate) -and $__adm_n -lt 1000){"
                   "Start-Sleep -Milliseconds 10; $__adm_n++}; "
                   "if($__adm_gate -and -not (Test-Path -LiteralPath $__adm_gate)){exit 125}; "
                   "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                   "$OutputEncoding=[Console]::OutputEncoding; $ProgressPreference='SilentlyContinue'; "
                   "$ErrorActionPreference='Stop'; $global:LASTEXITCODE=0; try { & {\n"
                   + script + "\n}; if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}; exit 0 } catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }")
        try:
            script_path.write_bytes(codecs.BOM_UTF8 + wrapper.encode('utf-8'))
        except OSError as exc:
            record['info'].update(state='failed', error=f'{type(exc).__name__}: {exc}',
                finishedAt=time.time())
            with self.lock:
                self._save(record)
            return
        readers = []
        try:
            if record['cancel'].is_set() or self.closed:
                record['info']['state'] = 'cancelled'
                return
            proc = subprocess.Popen([executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text',
                '-ExecutionPolicy', 'Bypass', '-File', str(script_path)],
                cwd=cwd or None, stdin=subprocess.DEVNULL,
                env=dict(os.environ, ADM_GATE=str(gate)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
            with self.lock:
                record['proc'] = proc
                record['tree'] = ProcessTree(proc)
                record['info']['state'] = 'running'
                self._save(record)
            for name in ('stdout', 'stderr'):
                thread = threading.Thread(target=self._drain, args=(record, name), daemon=True)
                thread.start()
                readers.append(thread)
            with self.lock:
                if record['cancel'].is_set() or self.closed:
                    record['tree'].kill()
                else:
                    gate.write_bytes(b'')
            deadline = time.monotonic() + timeout
            state = None
            while proc.poll() is None:
                if record['cancel'].wait(.05):
                    state = 'cancelled'
                elif time.monotonic() >= deadline:
                    state = 'timed_out'
                if state:
                    record['tree'].kill()
                    break
            code = proc.wait(timeout=5)
            # 脚本退出后也回收其后台子进程，防止日志句柄不关闭。
            record['tree'].kill()
            for thread in readers:
                thread.join(timeout=3)
            record['info'].update(state='failed' if record['info'].get('error') else
                                 state or ('succeeded' if code == 0 else 'failed'), exitCode=code)
        except Exception as exc:
            if record.get('tree'):
                record['tree'].kill()
            elif record.get('proc') and record['proc'].poll() is None:
                record['proc'].kill()
            record['info'].update(state='failed', error=f'{type(exc).__name__}: {exc}')
        finally:
            if record.get('tree'):
                record['tree'].close()
            record['info']['finishedAt'] = time.time()
            with self.lock:
                # 已完成任务从磁盘分页读取，释放有界历史占用的内存。
                record['proc'] = None
                record['stdout'] = OutputBuffer()
                record['stderr'] = OutputBuffer()
                self._save(record)
            for path in (script_path, gate):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _drain(self, record, name):
        path = self.directory / (record['info']['jobId'] + '.' + name)
        try:
            size = 0
            with path.open('wb') as output:
                while True:
                    chunk = record['proc'].__getattribute__(name).read1(8192)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 32*1024*1024:
                        record['info']['error'] = '单路任务日志超过 32 MiB，已停止执行'
                        record['cancel'].set()
                        break
                    record[name].append(chunk)
                    output.write(chunk)
        except Exception as exc:
            record['info']['error'] = f'保存任务日志失败：{exc}'
            record['cancel'].set()

    def status(self, identifier, owner):
        with self.lock:
            return dict(self._owned(identifier, owner)['info'])

    def output(self, args, owner):
        with self.lock:
            record = self._owned(args.get('jobId'), owner)
            stream = args.get('stream', 'stdout')
            if stream not in ('stdout', 'stderr'):
                raise ValueError('输出类型必须是 stdout 或 stderr')
            buffer = record[stream]
            # 重启后的历史从完整日志分页读取，不假装输出为空。
            if record['proc'] is None:
                path = self.directory / (record['info']['jobId'] + '.' + stream)
                cursor, limit = int(args.get('cursor', 0)), int(args.get('limit', 32768))
                if cursor < 0 or not 1 <= limit <= 65536:
                    raise ValueError('输出游标或读取长度无效')
                if path.exists():
                    with path.open('rb') as output:
                        output.seek(cursor)
                        raw = output.read(limit)
                    temporary = OutputBuffer(limit+4)
                    temporary.total = cursor
                    temporary.append(raw)
                    result = temporary.read(cursor, limit, final=cursor+len(raw) >= path.stat().st_size)
                    result['totalBytes'] = path.stat().st_size
                    result['hasMore'] = result['nextCursor'] < result['totalBytes']
                    return result
            return buffer.read(args.get('cursor', 0), args.get('limit', 32768), final=record['info']['state'] in FINAL)

    def cancel(self, identifier, owner):
        with self.lock:
            record = self._owned(identifier, owner)
            if record['info']['state'] not in FINAL:
                record['cancel'].set()
            return dict(record['info'])

    def session_open(self, args, owner):
        from powershell_pty import PowerShellPtyProcess as PtyProcess
        executable = powershell(args.get('shell', 'powershell'))
        cwd = str(args.get('cwd') or '')
        if cwd and not Path(cwd).is_dir():
            raise ValueError('执行目录不存在')
        with self.lock:
            if self.closed or len(self.sessions) >= self.max_active:
                raise RuntimeError('交互会话数已达到上限或服务已关闭')
            identifier = uuid.uuid4().hex
            cols, rows = int(args.get('cols', 120)), int(args.get('rows', 40))
            if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
                raise ValueError('终端尺寸无效')
            # 使用所选 shell 的标准模块目录，避免宿主注入另一版本的行编辑器。
            # 不加载 profile、不改变执行策略；自定义模块可用完整路径 Import-Module。
            environment = os.environ.copy()
            edition = 'WindowsPowerShell' if args.get('shell', 'powershell') == 'powershell' else 'PowerShell'
            environment['PSMODULEPATH'] = os.pathsep.join([str(Path(executable).parent / 'Modules'),
                str(Path.home() / 'Documents' / edition / 'Modules'),
                str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / edition / 'Modules')])
            proc = PtyProcess.spawn([executable, '-NoLogo', '-NoProfile'],
                                    cwd=cwd or None, env=environment, dimensions=(rows, cols))
            record = dict(info=dict(sessionId=identifier, owner=owner, state='running'),
                          proc=proc, output=OutputBuffer(), touched=time.monotonic())
            self.sessions[identifier] = record
            if self._watchdog is None:
                self._watchdog = threading.Thread(target=self._expire_sessions, daemon=True,
                                                   name='powershell-session-expiry')
                self._watchdog.start()
            threading.Thread(target=self._session_reader, args=(record,), daemon=True).start()
            return dict(record['info'])

    def _session_reader(self, record):
        try:
            while True:
                data = record['proc'].read(8192)
                if data:
                    record['output'].append(data.encode('utf-8') if isinstance(data, str) else data)
                elif not record['proc'].isalive():
                    break
        except EOFError:
            pass
        except Exception as exc:
            record['info']['error'] = f'PowerShell输出读取失败：{type(exc).__name__}: {exc}'
        finally:
            record['info']['state'] = 'closed'

    def _expire_sessions(self):
        # 页面失联后不立即中断操作；长期无人读取/输入的接入最终释放。
        while not self._session_stop.wait(5):
            with self.lock:
                for identifier, record in list(self.sessions.items()):
                    if time.monotonic() - record['touched'] > 1800:
                        try:
                            record['proc'].close(force=True)
                        except Exception:
                            pass
                        self.sessions.pop(identifier, None)

    def session_action(self, action, args, owner):
        with self.lock:
            # 关闭回执可能丢失；重复关闭不存在的标识按已释放处理，现存会话仍校验所有者。
            if action == 'session_close' and args.get('sessionId') not in self.sessions:
                return {'sessionId': args.get('sessionId'), 'state': 'closed'}
            record = self._owned(args.get('sessionId'), owner, self.sessions)
            record['touched'] = time.monotonic()
            if action == 'session_read':
                return {**record['info'], **record['output'].read(args.get('cursor', 0), args.get('limit', 32768))}
            if action == 'session_close':
                record['proc'].close(force=True)
                record['info']['state'] = 'closed'
                self.sessions.pop(record['info']['sessionId'], None)
            elif record['info']['state'] != 'running':
                raise RuntimeError('会话已结束')
            elif action == 'session_write':
                data = args.get('data')
                if not isinstance(data, str) or len(data) > 65536:
                    raise ValueError('终端输入无效')
                record['proc'].write(data)
            elif action == 'session_resize':
                cols, rows = int(args.get('cols', 120)), int(args.get('rows', 40))
                if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
                    raise ValueError('终端尺寸无效')
                record['proc'].setwinsize(rows, cols)
            else:
                raise ValueError('未知交互操作')
            return dict(record['info'])

    def call(self, action, args, owner):
        if action == 'capabilities':
            return dict(protocol=1, windowsExec=True, windowsSession=True,
                        filesRoot=str(self.directory / 'files'), windowsFiles=True,
                        shells=[name for name in ('powershell', 'pwsh') if _find_powershell(name)])
        if action == 'start':
            return self.start(args, owner)
        if action == 'status':
            return self.status(args.get('jobId'), owner)
        if action == 'output':
            return self.output(args, owner)
        if action == 'cancel':
            return self.cancel(args.get('jobId'), owner)
        if action == 'list':
            with self.lock:
                return {'jobs': [dict(r['info']) for r in self.jobs.values() if r['info']['owner'] == owner]}
        if action == 'session_open':
            return self.session_open(args, owner)
        if action.startswith('session_'):
            return self.session_action(action, args, owner)
        raise ValueError('未知 Windows 操作')

    def close_sessions(self, owner=None):
        with self.lock:
            for identifier, record in list(self.sessions.items()):
                if owner is None or record['info']['owner'] == owner or record['info']['owner'].startswith(owner+':'):
                    try:
                        record['proc'].close(force=True)
                    except Exception:
                        pass
                    self.sessions.pop(identifier, None)

    def file_path(self, relative, create=False):
        """电脑文件仅在本实例暂存目录内读写，不跟随链接或重解析点。"""
        import relay_transfer
        value = str(relative)
        path = Path(value)
        if path.is_absolute() or path.drive or '..' in path.parts or ':' in value:
            raise ValueError('电脑文件路径必须是暂存目录内的相对路径')
        root = self.directory / 'files'
        root.mkdir(parents=True, exist_ok=True)
        target = root / path
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError('电脑文件路径越界')
        current = root
        if relay_transfer.linked(root):
            raise ValueError('暂存目录不能是链接')
        for part in path.parts:
            current = current / part
            if current.exists() and relay_transfer.linked(current):
                raise ValueError('电脑文件路径包含链接')
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def close(self):
        self.closed = True
        self._session_stop.set()
        self.close_sessions()
        with self.lock:
            for record in self.jobs.values():
                if record['info']['state'] not in FINAL:
                    record['cancel'].set()
