"""ADB 命令执行、设备文件读写与内联编辑。

以 mixin 形式承载 ``ManagerServer`` 的 ADB 相关方法：这些方法全部经 ``self``
访问宿主的 worker、relay 与事件循环，抽成独立对象要下传十余个依赖，mixin
可以让调用点零改动。

宿主必须提供（见 ``server.ManagerServer``）：
    self.worker / self.relay / self.loop
    self._run_blocking() / self._is_remote_key() / self._broadcast()
    self.desktop_terminals
"""
import asyncio
import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from typing import Optional

from websockets import ServerConnection as WebSocketServerProtocol

from adb_worker import ADB_PATH, log
from diagnostics import ADBDiagnosticRunner

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0


class AdbServiceMixin:
    """ADB 相关方法集合，由 ManagerServer 继承；方法体沿用原有的 self 约定。"""

    def _start_adb_diagnostic(self, ws: WebSocketServerProtocol, request_id, selected_device: Optional[dict]):
        """启动单个后台诊断任务；不能在 handler 中等待，以便同客户端可以取消。"""
        with self._diagnostic_lock:
            if self._diagnostic_session is not None:
                return False
            cancel_event = threading.Event()
            self._diagnostic_session = {'id': request_id, 'cancelEvent': cancel_event}
        asyncio.create_task(self._execute_adb_diagnostic(ws, request_id, selected_device, cancel_event))
        return True

    def _cancel_adb_diagnostic(self, request_id) -> bool:
        with self._diagnostic_lock:
            session = self._diagnostic_session
            if not session or session.get('id') != request_id:
                return False
            session['cancelEvent'].set()
            return True

    async def _execute_adb_diagnostic(self, ws: WebSocketServerProtocol, request_id, selected_device: Optional[dict], cancel_event: threading.Event):
        """在线程池执行只读探针，并把进度安全地转回 WebSocket loop。"""
        def progress(current: int, total: int, check_id: str, title: str):
            self._broadcast_async({
                'type': 'diagnostic-progress', 'id': request_id,
                'current': current, 'total': total, 'checkId': check_id, 'title': title,
            })

        runner = ADBDiagnosticRunner(ADB_PATH, CREATE_NO_WINDOW, log, progress)
        try:
            report = await self._run_blocking(runner.run, selected_device, cancel_event)
            await self._send(ws, {
                'type': 'diagnostic-result', 'id': request_id,
                'success': True, 'report': report.to_dict(),
            })
        except Exception as e:
            log('DIAG', f'诊断执行失败: {type(e).__name__}: {e}')
            await self._send(ws, {
                'type': 'diagnostic-result', 'id': request_id,
                'success': False, 'output': f'诊断执行失败：{type(e).__name__}: {e}',
            })
        finally:
            with self._diagnostic_lock:
                if self._diagnostic_session and self._diagnostic_session.get('id') == request_id:
                    self._diagnostic_session = None

    async def _exec_adb(self, args: str = '', argv=None, make_executable=False, key: str = ''):
        """执行 adb 命令。

        普通命令仍接收字符串；文件传输使用 argv，避免 Windows 本地路径
        中的反斜杠被 shlex.split 当作转义字符吞掉。

        带 ``key`` 且该 key 属于远端设备时改走中转：操作端自带的设备选择符
        （``-s``/``-t``）会在这里被剥掉 —— 那两个值在从机上没有意义，甚至可能
        指向从机上的**另一台**设备。
        """
        command_args = list(argv) if isinstance(argv, list) else shlex.split(args or '')
        if key and self._is_remote_key(key):
            return await self._exec_remote_adb(key, command_args, make_executable)
        timeout = 60 if isinstance(argv, list) else 30
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    [ADB_PATH] + command_args,
                    capture_output=True, text=True, timeout=timeout,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            output = (proc.stdout or '') + (proc.stderr or '')
            # 移除最后的换行
            output = output.rstrip('\n')
            if proc.returncode == 0:
                if make_executable:
                    permission = await self._make_uploaded_script_executable(command_args)
                    if not permission['success']:
                        detail = permission.get('output') or '未知错误'
                        return {
                            'success': False,
                            'output': f'上传成功，但自动添加执行权限失败：{detail}',
                        }
                    targets = permission.get('targets') or ([permission['target']] if permission.get('target') else [])
                    suffix = f'；已添加可执行权限 → {"、".join(targets)}' if targets else '；已添加可执行权限'
                    output = f'{output}{suffix}' if output else f'上传成功{suffix}'
                if 'push' in command_args:
                    sync_result = await self._sync_uploaded_files(command_args)
                    if not sync_result['success']:
                        detail = sync_result.get('output') or '未知错误'
                        return {
                            'success': False,
                            'output': f'上传成功，但设备文件同步失败：{detail}',
                        }
                    output = f'{output}；设备文件已同步' if output else '上传成功；设备文件已同步'
                return {'success': True, 'output': output}
            return {'success': False, 'output': output or f'exit code {proc.returncode}'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': f'执行超时 ({timeout}s)'}
        except Exception as e:
            return {'success': False, 'output': str(e)}

    async def _exec_remote_adb(self, key: str, command_args, make_executable=False) -> dict:
        """远端文件经共享目录搬运，普通命令经请求队列执行。"""
        from relay.relay_app import strip_device_selector
        tokens = strip_device_selector(command_args)
        if tokens and tokens[0] in ('push', 'pull'):
            result = await self._run_blocking(self.relay.remote_transfer, key, command_args, make_executable)
        else:
            result = await self._run_blocking(self.relay.remote_exec, key, command_args, 60.0)
        result['remote'] = True
        return result

    async def _sync_uploaded_files(self, command_args):
        """在文件上传及可执行权限设置完成后，刷新目标设备的文件系统缓存。"""
        try:
            push_index = command_args.index('push')
            transport = command_args[:push_index]
            sync_args = [ADB_PATH, *transport, 'shell', 'sync']
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    sync_args,
                    capture_output=True, text=True, timeout=15,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            output = ((proc.stdout or '') + (proc.stderr or '')).rstrip('\n')
            if proc.returncode == 0:
                return {'success': True, 'output': output}
            return {'success': False, 'output': output or f'exit code {proc.returncode}'}
        except ValueError:
            return {'success': False, 'output': '上传参数不完整'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': 'sync 超时 (15s)'}
        except Exception as e:
            return {'success': False, 'output': str(e)}

    async def _make_uploaded_script_executable(self, command_args):
        """上传 shell 脚本后，在设备端补上可执行权限。"""
        try:
            push_index = command_args.index('push')
            if push_index + 2 >= len(command_args):
                return {'success': False, 'output': '上传参数不完整'}
            local_paths = [str(path) for path in command_args[push_index + 1:-1]]
            remote_path = str(command_args[-1])
            script_paths = [path for path in local_paths if path.lower().endswith('.sh')]
            if not script_paths:
                return {'success': True, 'targets': []}
            transport = command_args[:push_index]

            check_args = [ADB_PATH, *transport, 'shell', 'test', '-d', remote_path]
            check = await self._run_blocking(
                lambda: subprocess.run(
                    check_args,
                    capture_output=True, text=False, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            targets = []
            for local_path in script_paths:
                filename = os.path.basename(local_path.replace('\\', '/').rstrip('/'))
                if not filename:
                    return {'success': False, 'output': '无法确定脚本文件名'}
                target = remote_path.rstrip('/') + '/' + filename if check.returncode == 0 else remote_path
                chmod_args = [ADB_PATH, *transport, 'shell', 'chmod', '+x', target]
                log('ADB', f'add executable permission: {chmod_args!r}')
                chmod = await self._run_blocking(
                    lambda: subprocess.run(
                        chmod_args,
                        capture_output=True, text=False, timeout=10,
                        creationflags=CREATE_NO_WINDOW,
                    )
                )
                if chmod.returncode != 0:
                    error = ((chmod.stdout or b'') + (chmod.stderr or b'')).decode(
                        'utf-8', errors='replace'
                    ).strip()
                    return {'success': False, 'output': error or f'exit code {chmod.returncode}'}
                targets.append(target)
            return {'success': True, 'target': targets[0], 'targets': targets}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': '设置执行权限超时 (10s)'}
        except Exception as e:
            log('ADB', f'add executable permission failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    async def _exec_logcat(self, serial: str, key: str = ''):
        try:
            if key and self._is_remote_key(key):
                # 远端设备：命令里不能带 -s，由从机自己选定设备。
                result = await self._run_blocking(
                    self.relay.remote_exec, key, ['logcat', '-d', '-v', 'brief'], 30.0,
                )
                if not result.get('success'):
                    return result
                lines = (result.get('output') or '').splitlines()
                return {'success': True, 'output': '\n'.join(lines[-2000:]), 'remote': True}
            args = ['-s', serial, 'logcat', '-d', '-v', 'brief'] if serial else ['logcat', '-d', '-v', 'brief']
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    [ADB_PATH] + args,
                    capture_output=True, text=True, timeout=15,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            lines = (proc.stdout or '').splitlines()
            trimmed = '\n'.join(lines[-2000:])
            return {'success': True, 'output': trimmed}
        except Exception as e:
            return {'success': False, 'output': str(e)}

    async def _list_device_dir(self, serial: str, transport_id: str, path: str, key: str = ''):
        """List contents of a directory on the Android device via adb shell."""
        log('LDDIR', f'list_device_dir called: serial={serial!r} tid={transport_id!r} path={path!r} key={key!r}')
        try:
            path = (path or '/').strip()
            if not path.startswith('/'):
                return {'success': False, 'output': '设备目录路径必须是绝对路径', 'entries': []}
            if key and self._is_remote_key(key):
                return await self._list_remote_device_dir(key, path)
            transport = ['-t', str(transport_id)] if transport_id else ['-s', serial]
            list_args = [ADB_PATH, *transport, 'shell', 'ls', '-1F', path]
            log('LDDIR', f'Step 1 - args: {list_args!r}')
            log('LDDIR', f'Step 1 - path({type(path).__name__})={path!r}')

            proc = await self._run_blocking(
                lambda: subprocess.run(
                    list_args,
                    capture_output=True, text=True, timeout=10,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            log('LDDIR', f'Step 1 - returncode={proc.returncode}')
            log('LDDIR', f'Step 1 - stdout ({len(proc.stdout or "")} chars): {proc.stdout!r}')
            log('LDDIR', f'Step 1 - stderr ({len(proc.stderr or "")} chars): {proc.stderr!r}')

            output = ((proc.stdout or '') + (proc.stderr or '')).strip()
            log('LDDIR', f'Step 1 - trimmed output ({len(output)} chars): {output[:500]!r}')

            # Step 2: Detect errors
            if proc.returncode != 0:
                log('LDDIR', f'Step 2 - command failed: {proc.returncode}')
                return {'success': False, 'output': output or f'exit code {proc.returncode}', 'entries': []}
            if not output:
                log('LDDIR', 'Step 2 - output empty, returning []')
                return {'success': True, 'entries': [], 'currentPath': path}
            err_lower = output.lower()
            if any(kw in err_lower for kw in ('permission denied', 'cannot access', 'no such file', 'not found', 'opendir failed')):
                log('LDDIR', f'Step 2 - error detected in output: {output[:200]}')
                return {'success': False, 'output': output, 'entries': []}
            log('LDDIR', 'Step 2 - no error detected')

            # Step 3: Parse lines（与远端目录列举共用同一套解析规则）
            lines = [l for l in output.splitlines() if l.strip()]
            log('LDDIR', f'Step 3 - {len(lines)} lines: {lines[:20]}')
            entries = self._parse_device_dir(lines, path)
            log('LDDIR', 'SUCCESS - returning entries')
            return {'success': True, 'entries': entries, 'currentPath': path}
        except Exception as e:
            log('LDDIR', f'ERROR: {type(e).__name__}: {e}')
            import traceback
            log('LDDIR', f'ERROR traceback: {traceback.format_exc()}')
            return {'success': False, 'output': str(e), 'entries': []}

    @staticmethod
    def _parse_device_dir(lines, path: str) -> list[dict]:
        """把 ``ls -1F`` 的输出解析成目录项列表。

        ``-F`` 会在目录后加 ``/``、可执行文件后加 ``*`` 等标记，这里只保留
        目录判定需要的信息。远端列举与本机列举共用它，避免两条路径的解析分叉。
        """
        entries = []
        for line in lines:
            if line in ('.', '..'):
                continue
            if line.endswith('/'):
                name, is_dir = line[:-1], True
            else:
                name, is_dir = line.rstrip('@*|='), False
            full_path = path.rstrip('/') + '/' + name if name else ''
            entries.append({'name': name, 'isDir': is_dir, 'path': full_path})
        entries.sort(key=lambda e: (not e['isDir'], e['name'].lower()))
        return entries

    async def _list_remote_device_dir(self, key: str, path: str):
        """远端设备的目录列举：同一条 ``ls -1F``，只是经从机执行。"""
        result = await self._run_blocking(
            self.relay.remote_list_dir, key, path,
        )
        if not result.get('success'):
            return {'success': False, 'output': result.get('output', ''), 'entries': []}
        output = (result.get('output') or '').strip()
        if not output:
            return {'success': True, 'entries': [], 'currentPath': path}
        lines = [line for line in output.splitlines() if line.strip()]
        return {
            'success': True,
            'entries': self._parse_device_dir(lines, path),
            'currentPath': path,
            'remote': True,
        }

    async def _start_device_edit(self, key: str, serial: str, transport_id: str, remote_path: str):
        """拉取到临时目录，并通过 Windows“打开方式”选择编辑程序。"""
        if not key:
            return {'success': False, 'output': '缺少编辑会话标识'}
        if not remote_path or not remote_path.startswith('/'):
            return {'success': False, 'output': '设备文件路径必须是绝对路径'}
        if not serial and not transport_id:
            return {'success': False, 'output': '缺少设备标识'}
        with self._edit_lock:
            if key in self._edit_sessions:
                return {'success': False, 'output': '该设备已有未结束的编辑会话'}

        temp_dir = tempfile.mkdtemp(prefix='devmux-edit-')
        filename = remote_path.rstrip('/').rsplit('/', 1)[-1] or 'device-file'
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        local_path = os.path.join(temp_dir, filename)
        transport = ['-t', str(transport_id)] if transport_id else ['-s', serial]
        pull_args = [ADB_PATH, *transport, 'pull', remote_path, local_path]
        log('EDIT', f'pull device file: {pull_args!r}')
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    pull_args,
                    capture_output=True, text=True, timeout=60,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            output = ((proc.stdout or '') + (proc.stderr or '')).strip()
            if proc.returncode != 0 or not os.path.isfile(local_path):
                shutil.rmtree(temp_dir, ignore_errors=True)
                return {'success': False, 'output': output or f'exit code {proc.returncode}'}

            with self._edit_lock:
                self._edit_sessions[key] = {
                    'tempDir': temp_dir,
                    'localPath': local_path,
                    'remotePath': remote_path,
                    'transport': transport,
                }
            try:
                subprocess.Popen(
                    ['rundll32.exe', 'shell32.dll,OpenAs_RunDLL', local_path],
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception:
                self._cleanup_device_edit(key)
                return {'success': False, 'output': '无法打开 Windows“打开方式”窗口'}
            return {'success': True, 'output': '文件已打开；编辑并保存后，点击“保存覆盖”写回设备'}
        except subprocess.TimeoutExpired:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {'success': False, 'output': '拉取文件超时 (60s)'}
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            log('EDIT', f'start edit failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    async def _start_inline_edit(self, key: str, serial: str, transport_id: str, remote_path: str):
        """直接读取设备上的 UTF-8 文本，不创建本地临时文件。"""
        if not key:
            return {'success': False, 'output': '缺少编辑会话标识'}
        if not remote_path or not remote_path.startswith('/'):
            return {'success': False, 'output': '设备文件路径必须是绝对路径'}
        if not serial and not transport_id:
            return {'success': False, 'output': '缺少设备标识'}
        with self._edit_lock:
            if key in self._edit_sessions:
                return {'success': False, 'output': '该设备正在使用外部编辑会话，请先结束后再直接编辑'}
        with self._inline_lock:
            if key in self._inline_sessions:
                return {'success': False, 'output': '该设备已有直接编辑会话'}

        transport = ['-t', str(transport_id)] if transport_id else ['-s', serial]
        read_args = [ADB_PATH, *transport, 'exec-out', 'cat', remote_path]
        log('INLINE', f'read remote text: {read_args!r}')
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    read_args,
                    capture_output=True, text=False, timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            if proc.returncode != 0:
                error = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
                return {'success': False, 'output': error or f'exit code {proc.returncode}'}
            content = proc.stdout or b''
            if len(content) > 4 * 1024 * 1024:
                return {'success': False, 'output': '文件超过 4MB，无法直接编辑，请使用打开方式'}
            try:
                content.decode('utf-8')
            except UnicodeDecodeError:
                return {'success': False, 'output': '仅支持 UTF-8 文本文件；二进制文件请使用打开方式'}
            with self._inline_lock:
                self._inline_sessions[key] = {
                    'remotePath': remote_path,
                    'transport': transport,
                    'content': content,
                }
            return {
                'success': True,
                'content': base64.b64encode(content).decode('ascii'),
                'encoding': 'utf-8',
                'output': '文件已读取，可直接编辑；修改后点击“立即保存”写回设备',
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': '读取文件超时 (60s)'}
        except Exception as e:
            log('INLINE', f'start inline edit failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    async def _save_inline_edit(self, key: str, content_b64: str):
        """把应用内编辑器的 UTF-8 内容通过 adb shell 标准输入写回设备。"""
        with self._inline_lock:
            session = self._inline_sessions.get(key)
        if not session:
            return {'success': False, 'output': '没有可保存的直接编辑会话'}
        try:
            content = base64.b64decode(content_b64 or '', validate=True)
            content.decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            return {'success': False, 'output': '编辑内容不是有效的 UTF-8 文本'}
        if len(content) > 4 * 1024 * 1024:
            return {'success': False, 'output': '文件超过 4MB，无法直接保存'}

        remote_path = session['remotePath']
        shell_command = f'cat > {shlex.quote(remote_path)}'
        write_args = [ADB_PATH, *session['transport'], 'shell', shell_command]
        log('INLINE', f'write remote text: {write_args!r}')
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    write_args,
                    input=content, capture_output=True, text=False, timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            if proc.returncode != 0:
                error = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
                return {'success': False, 'output': error or f'exit code {proc.returncode}'}
            with self._inline_lock:
                current = self._inline_sessions.get(key)
                if current:
                    current['content'] = content
            return {'success': True, 'output': '已保存到设备'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': '保存文件超时 (60s)'}
        except Exception as e:
            log('INLINE', f'save inline edit failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    def _close_inline_edit(self, key: str):
        with self._inline_lock:
            session = self._inline_sessions.pop(key, None)
        return {'success': True, 'output': '直接编辑会话已关闭'} if session else {'success': True, 'output': ''}

    def _cleanup_all_inline_edits(self):
        with self._inline_lock:
            self._inline_sessions.clear()

    async def _finish_device_edit(self, key: str, save: bool):
        """保存或放弃编辑会话，并清理临时文件。"""
        with self._edit_lock:
            session = self._edit_sessions.get(key)
        if not session:
            return {'success': False, 'output': '没有可结束的编辑会话'}
        if not save:
            self._cleanup_device_edit(key)
            return {'success': True, 'output': '已放弃编辑，临时文件已删除'}

        push_args = [
            ADB_PATH, *session['transport'], 'push',
            session['localPath'], session['remotePath'],
        ]
        log('EDIT', f'push edited file: {push_args!r}')
        try:
            proc = await self._run_blocking(
                lambda: subprocess.run(
                    push_args,
                    capture_output=True, text=True, timeout=60,
                    encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
            )
            output = ((proc.stdout or '') + (proc.stderr or '')).strip()
            if proc.returncode != 0:
                return {'success': False, 'output': output or f'exit code {proc.returncode}'}
            self._cleanup_device_edit(key)
            return {'success': True, 'output': '已覆盖设备端文件，临时文件已删除'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'output': '保存文件超时 (60s)'}
        except Exception as e:
            log('EDIT', f'save edit failed: {type(e).__name__}: {e}')
            return {'success': False, 'output': str(e)}

    def _cleanup_device_edit(self, key: str):
        with self._edit_lock:
            session = self._edit_sessions.pop(key, None)
        if session:
            shutil.rmtree(session.get('tempDir', ''), ignore_errors=True)

    def _cleanup_all_device_edits(self):
        with self._edit_lock:
            keys = list(self._edit_sessions.keys())
        for key in keys:
            self._cleanup_device_edit(key)
