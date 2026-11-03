"""中转的 adb 执行层：设备枚举、命令执行与输出整形。

这一层只认识「adb 路径 + 一台设备」，不认识节点、主控、从机这些概念，因此可以
单独测试，也让只想跑一条 adb 命令的模块不必加载整个节点运行时。

``execute`` 与 ``list_devices`` 是所有 adb 子进程的**唯一出口**：需要打桩的测试
请打在 ``relay_adb`` 上（``patch.object(relay_adb, 'execute', ...)``）。打在
``relay_node`` 上不会生效 —— 那里只是兼容再导出，没有任何调用点读取它。
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


# 所有 adb 子进程都必须带这个 flag，否则 GUI 版会弹出控制台窗口。
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0


DEFAULT_ADB_PATH = os.environ.get('ADB_PATH') or 'adb'


DEFAULT_ADB_TIMEOUT_S = 30.0


DEFAULT_LOG_TIMEOUT_S = 15.0


MAX_OUTPUT_BYTES = 64 * 1024


# 远端设备 key 的前缀：``relay:<peerId>:<本机key>``。
# peerId 经 check_peer_id 校验后不含冒号，而本机 key 形如 ``serial:tid=2``，
# 因此按**前两个冒号**切分即可无损还原，不会被 tid 里的冒号干扰。
REMOTE_KEY_PREFIX = 'relay'


# 退出码标记：``adb shell`` 不回传设备端退出码，由设备端自己报。
EXIT_MARKER = '__ADM_EXIT:'


# ----------------------------------------------------------------------
# 设备枚举
# ----------------------------------------------------------------------
def device_key(serial: str, transport_id=None) -> str:
    """设备稳定标识，与 server.py / adb_worker.py 保持一致：``serial:tid=<id>``。"""
    text = str(serial or '')
    if transport_id:
        return f'{text}:tid={transport_id}'
    return text


def parse_devices(text: str) -> list[dict]:
    """解析 ``adb devices -l`` 的输出。

    只保留 ``device`` 状态的行交给上层使用，但 offline / unauthorized 也一并
    返回（state 字段区分），让主控能看到「从机有一台设备但不可用」。
    """
    devices: list[dict] = []
    for line in (text or '').splitlines():
        raw = line.strip()
        if not raw or raw.startswith('List of devices') or raw.startswith('*'):
            continue
        parts = raw.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        transport_id = None
        model = ''
        for token in parts[2:]:
            head, _, tail = token.partition(':')
            if head == 'transport_id':
                try:
                    transport_id = int(tail)
                except ValueError:
                    transport_id = None
            elif head == 'model':
                model = tail
        devices.append({
            'serial': serial,
            'state': state,
            'transportId': transport_id,
            'model': model,
            'key': device_key(serial, transport_id),
        })
    return devices


def list_devices(adb_path: str, timeout: float = 8.0) -> tuple[list[dict], str]:
    """执行 ``adb devices -l``，返回 ``(设备列表, 错误信息)``。

    注意这里**只读**，不启动/停止 adb server —— ADB server 的恢复逻辑仍然只在
    ``adb_worker`` 的轮询失败路径里，避免两处同时 kill/start 把 server 拉坏。
    """
    try:
        proc = subprocess.run(
            [adb_path, 'devices', '-l'],
            capture_output=True, timeout=max(1.0, float(timeout)),
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return [], f'adb devices 超时（{timeout:g}s）'
    except OSError as exc:
        return [], f'无法启动 adb：{exc}'
    text = (proc.stdout or b'').decode('utf-8', 'replace')
    if proc.returncode != 0:
        detail = (proc.stderr or b'').decode('utf-8', 'replace').strip()
        return parse_devices(text), detail or f'adb devices 退出码 {proc.returncode}'
    return parse_devices(text), ''


def remote_key(peer_id: str, local_key: str) -> str:
    return f'{REMOTE_KEY_PREFIX}:{peer_id}:{local_key}'


def split_remote_key(key: str) -> tuple[str, str]:
    """把 ``relay:<peerId>:<本机key>`` 还原为 ``(peerId, 本机key)``。"""
    text = str(key or '')
    if not text.startswith(f'{REMOTE_KEY_PREFIX}:'):
        return '', text
    rest = text[len(REMOTE_KEY_PREFIX) + 1:]
    peer_id, _, local = rest.partition(':')
    return peer_id, local


# ----------------------------------------------------------------------
# 设备端命令执行
# ----------------------------------------------------------------------
@dataclass
class ExecOutcome:
    exitCode: Optional[int] = None
    stdout: str = ''
    stderr: str = ''
    truncated: bool = False
    elapsedMs: int = 0
    timeout: bool = False
    error: Optional[str] = None
    exitCodeAvailable: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """超长输出保留头尾、中间标注省略。

    单纯截头会让 ``logcat`` / ``dmesg`` 丢掉最有价值的末尾；单纯截尾又会让
    ``df -h`` 这类丢掉表头。头尾各留一段，并**明确写出省掉了多少**，让调用方
    知道结果不完整。
    """
    raw = text or ''
    size = len(raw.encode('utf-8', 'replace'))
    if size <= limit or limit <= 0:
        return raw, False
    head_len = int(limit * 0.6)
    tail_len = limit - head_len
    head = raw.encode('utf-8', 'replace')[:head_len].decode('utf-8', 'replace')
    tail = raw.encode('utf-8', 'replace')[-tail_len:].decode('utf-8', 'replace')
    omitted = max(0, size - len(head.encode('utf-8', 'replace')) - len(tail.encode('utf-8', 'replace')))
    return f'{head}\n…… 〔已省略 {omitted} 字节〕 ……\n{tail}', True


def split_exit_marker(text: str) -> tuple[str, Optional[int]]:
    """从设备端输出里剥离 ``__ADM_EXIT:<code>`` 标记行。

    用 ``rfind`` 取最后一个标记（命令自身的输出里若出现同名字符串，前面那些不会
    被误判）；标记后必须紧跟可解析的整数，否则视为没有标记并返回 ``None``
    —— 退出码**不可得就是不可得**，绝不假装成 0。
    """
    raw = text or ''
    index = raw.rfind(EXIT_MARKER)
    if index < 0:
        return raw, None
    tail = raw[index + len(EXIT_MARKER):]
    number = tail.split('\r', 1)[0].split('\n', 1)[0].strip()
    try:
        code = int(number)
    except ValueError:
        return raw, None
    return raw[:index].rstrip('\r\n'), code


def device_selector_args(device: Optional[dict]) -> list[str]:
    """由**执行端**依据设备记录拼装选择符，永远不接受请求方传入。"""
    if not device:
        return []
    transport_id = device.get('transportId')
    if transport_id:
        return ['-t', str(transport_id)]
    serial = str(device.get('serial') or '')
    return ['-s', serial] if serial else []


def execute(
    adb_path: str,
    device: Optional[dict],
    argv,
    *,
    timeout: float = DEFAULT_ADB_TIMEOUT_S,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> ExecOutcome:
    """在指定设备上执行一条 adb 命令。

    ``shell`` 子命令的语义 = 把 ``argv[1:]`` 用空格拼成一条设备端 shell 命令，
    并在尾部追加退出码标记；其它子命令直接透传，退出码取 adb 自身的返回码。
    """
    started = time.time()
    tokens = [str(part) for part in (argv or [])]
    if not tokens:
        return ExecOutcome(error='命令为空', elapsedMs=0)

    is_shell = tokens[0] == 'shell'
    selector = device_selector_args(device)
    if is_shell:
        inner = ' '.join(tokens[1:]).strip()
        if not inner:
            return ExecOutcome(error='shell 命令为空', elapsedMs=0)
        # 列表形式传参，不经过本地 shell；只有设备端 shell 会解析这条串。
        command = [adb_path, *selector, 'shell', f'{inner}; echo {EXIT_MARKER}$?']
    else:
        command = [adb_path, *selector, *tokens]

    try:
        proc = subprocess.run(
            command, capture_output=True, timeout=max(1.0, float(timeout)),
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return ExecOutcome(
            timeout=True, error=f'执行超时（{timeout:g}s），已终止该命令',
            elapsedMs=int((time.time() - started) * 1000),
        )
    except OSError as exc:
        return ExecOutcome(
            error=f'无法启动 adb：{exc}', elapsedMs=int((time.time() - started) * 1000),
        )

    stdout = (proc.stdout or b'').decode('utf-8', 'replace').replace('\r\n', '\n')
    stderr = (proc.stderr or b'').decode('utf-8', 'replace').replace('\r\n', '\n')
    exit_code = proc.returncode
    exit_code_available = True
    if is_shell:
        stdout, parsed = split_exit_marker(stdout)
        if parsed is None:
            exit_code = None
            exit_code_available = False
        else:
            exit_code = parsed

    stdout, cut_out = truncate_text(stdout, max_bytes)
    stderr, cut_err = truncate_text(stderr, max_bytes)
    return ExecOutcome(
        exitCode=exit_code,
        stdout=stdout,
        stderr=stderr,
        truncated=cut_out or cut_err,
        elapsedMs=int((time.time() - started) * 1000),
        exitCodeAvailable=exit_code_available,
    )
