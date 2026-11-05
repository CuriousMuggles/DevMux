"""RelayController 的选路执行面（B7 拆分，见 docs/优化建议清单.md）。

按设备 key 决定「本机直连」还是「经从机执行」，并把操作端的设备选择符剥掉。
共享 ``self``：远端客户端缓存与关闭队列都由 RelayController.__init__ 建立。
"""
import concurrent.futures
import re
import tempfile
import time
import uuid

import relay
from . import relay_node
from . import relay_transfer
from . import relay_app_common
from .relay_app_common import strip_device_selector


class ControllerWindowsMixin:
    # --- 选路执行 ---

    def is_remote_key(self, key: str) -> bool:
        peer_id, _local = relay_node.split_remote_key(str(key or ''))
        return bool(peer_id)

    def _queue_windows_close(self, node, peer, identifier):
        token = (id(node), peer, identifier)
        with self._lock:
            self._retired_windows.setdefault(token, dict(node=node, peer=peer, identifier=identifier,
                                                         nextAttempt=0.0, closing=False))
        return token

    def _close_retired_windows(self, token):
        with self._lock:
            item = self._retired_windows.get(token)
        if item is None:
            return
        ok = False
        node = item['node']
        try:
            # 与终端旧接入回收一致：对端离线时不发请求，避免每轮重试都推一条
            # 「不下发请求给离线从机」的可见提醒；保留记录等心跳恢复。
            offline_check = getattr(node, '_peer_offline_reason', None)
            offline = offline_check(item['peer']) if callable(offline_check) else ''
            if offline:
                if not item.get('offlineNoted'):
                    item['offlineNoted'] = True
                    self.log('RELAY', f'对端离线，暂缓回收旧电脑终端接入（心跳恢复后继续）：{item["identifier"]}')
            else:
                item.pop('offlineNoted', None)
                result = node.submit(item['peer'], relay.KIND_WINDOWS,
                    payload={'action': 'session_close', 'args': {'sessionId': item['identifier']}}, timeout=5)
                ok = bool(result and result.ok)
        except Exception as exc:
            self.log('RELAY', f'旧PowerShell接入回收失败，保留重试：{type(exc).__name__}: {exc}')
        finally:
            with self._lock:
                if self._retired_windows.get(token) is item:
                    if ok:
                        self._retired_windows.pop(token, None)
                        key = next((key for key, client in self._windows_clients.items()
                                    if client.node is item['node'] and key[1] == item['identifier']), None)
                        if key is not None:
                            self._windows_clients.pop(key, None)
                    else:
                        item.update(closing=False, nextAttempt=time.time() + relay_app_common.SESSION_REOPEN_BACKOFF_S)

    def _drain_windows_closes(self):
        with self._lock:
            pending = [token for token, item in self._retired_windows.items()
                       if not item['closing'] and time.time() >= item['nextAttempt']]
            for token in pending:
                self._retired_windows[token]['closing'] = True
        for token in pending:
            executor = self._maintenance_executor
            if executor is None:
                self._close_retired_windows(token)
            else:
                executor.submit(self._close_retired_windows, token)

    def windows_call(self, node_id, action, args):
        node = self.node
        if node is None:
            raise RuntimeError('跨机中转尚未启动')
        candidates = [peer for peer, beat in node._peers.items() if beat.nodeId == node_id]
        if len(candidates) != 1:
            raise ValueError('目标实例不存在或路由有歧义')
        peer = candidates[0]
        key = (node_id, (args or {}).get('sessionId'))
        with self._lock:
            client = self._windows_clients.get(key)
        if client is not None and client.node is node:
            if action == 'session_read':
                return client.read(args)
            if action in ('session_write', 'session_resize'):
                return client.send(action, args)
            if action == 'session_close':
                if client.meta['state'] != 'running':
                    with self._lock:
                        self._windows_clients.pop(key, None)
                    return {'sessionId': key[1], 'state': 'closed'}
        close_token = None
        if action == 'session_close':
            close_token = self._queue_windows_close(node, peer, (args or {}).get('sessionId'))
            with self._lock:
                self._retired_windows[close_token]['closing'] = True
        try:
            result = node.submit(candidates[0], relay.KIND_WINDOWS,
                                 payload={'action': action, 'args': args, 'streamProtocol': 1}, timeout=15)
        finally:
            if close_token is not None:
                with self._lock:
                    item = self._retired_windows.get(close_token)
                    if item is not None:
                        item.update(closing=False, nextAttempt=time.time() + relay_app_common.SESSION_REOPEN_BACKOFF_S)
        if result.status != relay.STATUS_OK:
            raise RuntimeError(result.error or '远程 Windows 操作失败')
        if close_token is not None:
            with self._lock:
                self._retired_windows.pop(close_token, None)
                if self._windows_clients.get(key) is client:
                    self._windows_clients.pop(key, None)
        data = result.extra.get('data', {})
        if action == 'session_open' and data.get('streamProtocol') == 1:
            from windows_stream import MasterStream
            import re
            identifier = str(data.get('sessionId') or '')
            if not re.fullmatch(r'[a-f0-9]{32}', identifier) or data.get('streamId') != 'win-' + identifier:
                raise ValueError('从机返回了非法电脑流标识')
            try:
                client = MasterStream(node, peer, identifier, data['streamId'])
                with self._lock:
                    if self.node is not node:
                        raise RuntimeError('创建电脑终端期间中转节点已停止')
                    self._windows_clients[(node_id, identifier)] = client
            except Exception:
                if self.node is node:
                    token = self._queue_windows_close(node, peer, identifier)
                    self._close_retired_windows(token)
                else:
                    node.submit(peer, relay.KIND_WINDOWS,
                        payload={'action': 'session_close', 'args': {'sessionId': identifier}}, timeout=5)
                raise
        return data

    def remote_exec(self, remote_key: str, argv, timeout: float = relay_node.DEFAULT_ADB_TIMEOUT_S) -> dict:
        """把一条命令送到远端设备执行，返回与 ``exec-result`` 同构的结果。"""
        node = self.node
        peer_id, local_key = relay_node.split_remote_key(str(remote_key or ''))
        if not peer_id:
            return {'success': False, 'output': '该设备不是远端设备'}
        if node is None:
            return {'success': False, 'output': '跨机中转未启用'}
        tokens = strip_device_selector(argv)
        if not tokens:
            return {'success': False, 'output': '命令为空'}
        try:
            result = node.exec_remote(peer_id, local_key, tokens, timeout=timeout)
        except Exception as exc:
            return {'success': False, 'output': f'{type(exc).__name__}: {exc}'}
        return self._result_to_exec(result)

    def remote_list_dir(self, remote_key, path):
        """短时复用同目录结果，缓存绑定实例、连接代次和部署，不能跨撤销复用。"""
        import shlex
        node = self.node
        peer, local = relay_node.split_remote_key(str(remote_key or ''))
        argv = ['shell', 'ls -1F ' + shlex.quote(path)]
        if node is None or not peer or not node.connection_ready(peer):
            return self.remote_exec(remote_key, argv, 30.0)
        record = node.pairing.get(node._node_id_for(peer))
        generation = (id(node), node._connection_endpoint(peer), record.connectionId if record else '')
        key = (generation, local, path)
        with self._directory_lock:
            cached = self._directory_cache.get(key)
            if cached and time.monotonic() - cached[0] < 3.0:
                return dict(cached[1])
            pending = self._directory_pending.get(key)
            owner = pending is None
            if owner:
                pending = concurrent.futures.Future()
                self._directory_pending[key] = pending
        if not owner:
            return dict(pending.result(timeout=35))
        try:
            started = time.monotonic()
            result = self.remote_exec(remote_key, argv, 30.0)
            self.log('RELAY', f'远端目录读取往返 {round((time.monotonic() - started) * 1000)}ms：{remote_key} {path}')
            # 只缓存成功结果；身份或连接已变时由下次请求重新验证。
            with self._directory_lock:
                if result.get('success'):
                    if len(self._directory_cache) >= 64:
                        self._directory_cache.pop(next(iter(self._directory_cache)))
                    self._directory_cache[key] = (time.monotonic(), dict(result))
            pending.set_result(dict(result))
            return result
        except Exception as exc:
            result = {'success': False, 'output': f'远程目录读取失败：{exc}'}
            pending.set_result(result)
            return result
        finally:
            with self._directory_lock:
                self._directory_pending.pop(key, None)

    def _cleanup_download(self, node, peer_id, transfer_id):
        try:
            cleanup = node.submit(peer_id, relay_transfer.KIND_CLEANUP,
                                  payload={'transferId': transfer_id}, timeout=10)
            if not cleanup.ok:
                raise RuntimeError(cleanup.error or '对端未确认清理')
        except Exception as exc:
            self._push_event({'kind': 'error', 'text': f'文件传输临时包未清理：{exc}'})

    def windows_transfer(self, node_id, argv):
        node = self.node
        if node is None:
            return {'success': False, 'output': '跨机中转未启动'}
        peers = [peer for peer, beat in node._peers.items() if beat.nodeId == node_id]
        if len(peers) != 1:
            return {'success': False, 'output': '电脑目标不存在或路由有歧义'}
        return self.remote_transfer('relay:'+peers[0]+':windows', argv, computer=True)

    def remote_transfer(self, remote_key, argv, make_executable=False, *, computer=False):
        """沿用本机文件选择，把主控路径转为包引用，路径不会发送给从机。"""
        node = self.node
        peer_id, local_key = relay_node.split_remote_key(str(remote_key or ''))
        if node is None or not peer_id:
            return {'success': False, 'output': '跨机中转未启用或设备标识非法'}
        tokens = strip_device_selector(argv)
        if len(tokens) < 3 or tokens[0] not in ('push', 'pull'):
            return {'success': False, 'output': '上传/下载参数不完整'}
        operation = tokens[0]
        expected_endpoint = node._connection_endpoint(peer_id)
        expected_record = node.pairing.get(node._node_id_for(peer_id))
        expected_generation = expected_record.connectionId if expected_record else ''
        # 大文件打包前先验证连通性；从机对真正的文件请求再次准入。
        try:
            check = None if node.connection_ready(peer_id) else node.verify_connection(peer_id)
        except Exception as exc:
            return {'success': False, 'output': f'文件传输连接验证失败：{exc}'}
        if check is not None and not check.ok:
            return self._result_to_exec(check)
        transfer_id = uuid.uuid4().hex
        try:
            directory = relay_transfer.transfer_dir(node.channel_root(peer_id), peer_id, node.peer_id, transfer_id)
        except Exception as exc:
            return {'success': False, 'output': f'文件中转目录不可用：{exc}'}
        payload = {'transferId': transfer_id, 'operation': operation,
                   'makeExecutable': bool(make_executable),
                   'remotePaths': [tokens[-1]] if operation == 'push' else tokens[1:-1]}
        result = None
        try:
            if operation == 'push':
                started = time.monotonic()
                payload['package'] = relay_transfer.publish_upload(tokens[1:-1], directory / 'upload.zip')
                self.log('RELAY', f'上传包准备 {round((time.monotonic() - started) * 1000)}ms，'
                         f'{payload["package"]["size"]}字节：{remote_key}')
                if node._peer_offline_reason(peer_id):
                    self.log('RELAY', '上传包准备完毕，等待心跳序号恢复推进（最多3秒）')
                    node.wait_for_peer_heartbeat(peer_id, timeout=3.0)
            current_record = node.pairing.get(node._node_id_for(peer_id))
            if (self.node is not node or expected_endpoint != node._connection_endpoint(peer_id)
                    or expected_generation != (current_record.connectionId if current_record else '')):
                return {'success': False, 'output': '文件准备期间从机实例或连接已变化，请重新选择设备上传/下载'}
            result = node.submit(peer_id, relay.KIND_WINDOWS_TRANSFER if computer else relay_transfer.KIND_TRANSFER,
                                 device={} if computer else {'key': local_key}, payload=payload,
                                 timeout=relay_transfer.TIMEOUT + 30)
            if not result.ok:
                reply = self._result_to_exec(result)
                if '未实现该请求类型' in reply['output']:
                    reply['output'] += '；请升级从机以支持文件上传下载'
                self._push_event({'kind': 'error', 'text': f'远程文件传输失败：{reply["output"]}'})
                return reply
            if operation == 'pull':
                with tempfile.TemporaryDirectory(prefix='DevMux-download-') as work:
                    started = time.monotonic()
                    paths = relay_transfer.receive_package(directory / 'download.zip', work, result.extra.get('package') or {})
                    relay_transfer.save_download(paths, tokens[-1])
                    self.log('RELAY', f'下载包接收、校验与保存完成 {round((time.monotonic() - started) * 1000)}ms：{remote_key}')
                result.stdout += f'\n已保存到本机：{tokens[-1]}'
            return self._result_to_exec(result)
        except Exception as exc:
            return {'success': False, 'output': f'文件中转失败：{exc}'}
        finally:
            # 超时不能删仍可能被从机读取的上传包；保留供排查，不能误报成功。
            try:
                if operation == 'push' and not (result and result.extra.get('localTimeout')):
                    (directory / 'upload.zip').unlink(missing_ok=True)
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                # 明确拒绝没有生成下载包；无需再发清理请求拖慢错误提示。
                if operation == 'pull' and result and result.ok:
                    # 清理回执可能等待十秒，不能占用首次终端开启的工作线程。
                    executor = getattr(self, '_maintenance_executor', None)
                    if executor is not None:
                        executor.submit(self._cleanup_download, node, peer_id, transfer_id)
                    else:
                        self._cleanup_download(node, peer_id, transfer_id)
            except Exception as exc:
                self._push_event({'kind': 'error', 'text': f'文件传输临时包未清理：{exc}'})

    @staticmethod
    def _result_to_exec(result: relay.Result) -> dict:
        output = ''
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ('\n' if output else '') + result.stderr
        if result.truncated:
            output += '\n[输出已截断]'
        if not output:
            output = result.error or f'从机返回状态：{result.status}'
        elif result.error:
            output += f'\n{result.error}'
        return {'success': bool(result.ok), 'output': output, 'status': result.status, 'agentId': result.agentId}

    def exec_any(self, key: str, argv, timeout: float = relay_node.DEFAULT_ADB_TIMEOUT_S):
        """按 key 选路：远端走中转，本机返回 None 让调用方自己走本地路径。"""
        if not self.is_remote_key(key):
            return None
        return self.remote_exec(key, argv, timeout=timeout)
