"""排故日志：本地副本、阶段快照及在主程序退出后仍能记录退出码的观察器。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import codecs
import tempfile

REVISION = 'relay-remote-tools-20261013-05'
_directory = None
_state = {}
_lock = threading.Lock()

# 观察器以明文 .ps1 经 -File 启动，不再使用 -EncodedCommand（安全软件把
# Base64 编码的 PowerShell 命令作为运行时行为评分的高权重信号）。退出码捕获
# 保留 Add-Type P/Invoke（GetExitCodeProcess 是读取外部进程退出码的唯一可靠
# 途径，纯 .NET ExitCode 对外部进程为空、CIM 实例退出即消失）；观察器默认
# 关闭（DEVMUX_EXIT_OBSERVER=1 才启用；旧变量继续兼容），不参与常规运行路径。
WATCH_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public class ExitObserver {
 [DllImport("kernel32.dll",SetLastError=true)] public static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
 [DllImport("kernel32.dll")] public static extern uint WaitForSingleObject(IntPtr h,uint ms);
 [DllImport("kernel32.dll")] public static extern bool GetExitCodeProcess(IntPtr h,out uint code);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}
'@
$meta=Get-Content -LiteralPath $env:ADB_EXIT_META -Raw -Encoding UTF8 | ConvertFrom-Json
$report=@{revision=$meta.revision;runId=$meta.runId;pid=$meta.pid;exe=$meta.exe;state='watching';startedAt=(Get-Date).ToString('o')}
function Save-Report {
 $json=$report | ConvertTo-Json -Depth 8
 [IO.File]::WriteAllText($env:ADB_EXIT_REPORT,$json,(New-Object Text.UTF8Encoding($false)))
 try { [IO.Directory]::CreateDirectory($env:ADB_EXIT_EXPORT) | Out-Null
   Copy-Item -LiteralPath $env:ADB_EXIT_REPORT -Destination $env:ADB_EXIT_EXPORT -Force } catch {}
}
$h=[ExitObserver]::OpenProcess(0x101000,$false,[uint32]$meta.pid)
try {
 if($h -eq [IntPtr]::Zero){throw '无法打开目标进程，未捕获退出码'}
 Save-Report
 while([ExitObserver]::WaitForSingleObject($h,2000) -eq 258) {
   $report.lastObservedAt=(Get-Date).ToString('o')
   try { $p=Get-Process -Id $meta.pid
     $report.process=@{threads=$p.Threads.Count;handles=$p.HandleCount;workingSet=$p.WorkingSet64;responding=$p.Responding} } catch {}
   if($meta.meipass){$report.runtimeResources=@{rootExists=(Test-Path -LiteralPath $meta.meipass);htmlExists=(Test-Path -LiteralPath (Join-Path $meta.meipass 'renderer\index.html'))}}
   # 在线时只写本地，避免共享目录卡顿阻塞退出观察。
   [IO.File]::WriteAllText($env:ADB_EXIT_REPORT,($report|ConvertTo-Json -Depth 8),(New-Object Text.UTF8Encoding($false)))
 }
 [uint32]$code=0
 if(-not [ExitObserver]::GetExitCodeProcess($h,[ref]$code)){throw '读取进程退出码失败'}
 $report.state='exited'; $report.exitCode=$code; $report.exitCodeHex=('0x{0:X8}' -f $code)
 $report.exitedAt=(Get-Date).ToString('o'); Save-Report
 Start-Sleep -Seconds 2
 try {
   $events=Get-WinEvent -FilterHashtable @{LogName='Application';Id=1000,1001,1026;StartTime=(Get-Date).AddMinutes(-3)} -MaxEvents 40 -ErrorAction Stop
   $report.events=@($events | Where-Object {$_.Message -match 'DevMux|ADBManager|OpenConsole|adb.exe|powershell.exe|msedgewebview2|python'} | ForEach-Object {
     @{time=$_.TimeCreated.ToString('o');id=$_.Id;provider=$_.ProviderName;message=$_.Message}
   })
 } catch {$report.eventReadError=$_.Exception.Message}
 Save-Report
 # 退出后导出本地完整日志，主程序来不及写共享目录时仍能保留证据。
 try { Get-ChildItem -LiteralPath (Split-Path -Parent $env:ADB_EXIT_REPORT) | Copy-Item -Destination $env:ADB_EXIT_EXPORT -Recurse -Force } catch {}
} catch { $report.state='observer-error';$report.error=$_.Exception.Message;Save-Report }
finally {if($h -ne [IntPtr]::Zero){[ExitObserver]::CloseHandle($h)|Out-Null}}
'''


def mark(stage, **detail):
    """按线程保存正在进行的阶段；快照仅写本地磁盘。"""
    if _directory is None:
        return
    try:
        with _lock:
            _state[str(threading.get_ident())] = dict(thread=threading.current_thread().name,
                stage=stage, at=time.strftime('%Y-%m-%d %H:%M:%S'), monotonic=time.monotonic(), **detail)
            temporary = _directory / 'phases.tmp'
            temporary.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(_directory / 'phases.json')
    except OSError:
        pass


def start_observer(pid, directory, export_directory):
    directory, export_directory = Path(directory), Path(export_directory)
    script_path = directory / 'observer.ps1'
    try:
        script_path.write_bytes(codecs.BOM_UTF8 + WATCH_SCRIPT.encode('utf-8'))
    except OSError:
        script_path = Path(tempfile.gettempdir()) / ('adm-observer-' + uuid.uuid4().hex + '.ps1')
        script_path.write_bytes(codecs.BOM_UTF8 + WATCH_SCRIPT.encode('utf-8'))
    env = {**os.environ, 'ADB_EXIT_META': str(directory/'run.json'),
           'ADB_EXIT_REPORT': str(directory/'exit-report.json'), 'ADB_EXIT_EXPORT': str(export_directory/directory.name)}
    command = [str(Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'),
               '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script_path)]
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    with open(directory/'observer.log', 'ab') as errors:
        try:
            # 尝试脱离父进程 Job；失败仍记录，不能静默认为观察器独立成功。
            process = subprocess.Popen(command, env=env, stdout=errors, stderr=errors,
                creationflags=flags | getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0))
            mode = 'breakaway'
        except OSError:
            process = subprocess.Popen(command, env=env, stdout=errors, stderr=errors, creationflags=flags)
            mode = 'default-job'
    return process, mode


def initialize(log, base_directory):
    global _directory
    run_id = time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}-' + uuid.uuid4().hex[:6]
    try:
        local = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or str(base_directory))/'ADBManager/diagnostics'/run_id
        local.mkdir(parents=True, exist_ok=True)
        _directory = local
        os.environ['ADB_DIAG_DIR'] = str(local)
        import adb_worker
        adb_worker.DIAGNOSTIC_LOG_FILE = str(local/'app.log')
        exe = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve()
        observer_enabled = (os.environ.get('DEVMUX_EXIT_OBSERVER') or
                            os.environ.get('ADB_MANAGER_EXIT_OBSERVER')) == '1'
        metadata = dict(revision=REVISION, runId=run_id, pid=os.getpid(), parentPid=os.getppid(),
                        exe=str(exe), exeSize=exe.stat().st_size, exeMtime=exe.stat().st_mtime,
                        frozen=bool(getattr(sys, 'frozen', False)), ptyIsolation=True, exitObserverEnabled=observer_enabled,
                        meipass=str(getattr(sys, '_MEIPASS', '')), pyiProcessLevel=os.environ.get('_PYI_PARENT_PROCESS_LEVEL'),
                        startedAt=time.strftime('%Y-%m-%d %H:%M:%S'))
        (local/'run.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        export = Path(base_directory)/'diagnostics'
        log('DIAG', f'运行标识 {run_id}，诊断版本 {REVISION}，PID {os.getpid()}，父PID {os.getppid()}，EXE {exe}，隔离PTY=启用')
        if observer_enabled:
            observer, mode = start_observer(os.getpid(), local, export)
            log('DIAG', f'退出观察器 PID {observer.pid}，模式 {mode}，本地证据 {local}，退出后导出至 {export}')
        else:
            log('DIAG', f'本地日志 {local}；应用内退出观察器未启用（DEVMUX_EXIT_OBSERVER=1 可按需开启）')
        mark('application-start')
        return local
    except Exception as exc:
        log('DIAG', f'诊断初始化失败：{type(exc).__name__}: {exc}')
        return None
