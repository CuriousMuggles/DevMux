"""DB 烧写扩展接口：从机只读文件浏览、准入与异步任务记录。"""
import itertools
import json
import os
from pathlib import Path
import re
import threading
import uuid


class DBFlashExtension:
    NAME = 'DB烧写'
    ACTIONS = frozenset({'config', 'save', 'browse', 'catalog', 'plan', 'start', 'status', 'cancel'})

    def __init__(self, service, config_path, *, config_storage=None):
        self._service = service
        self._lock = threading.RLock()
        self.config_path = Path(config_path)
        self.config_storage = config_storage
        self.journal_path = self.config_path.with_name(self.config_path.stem + '-jobs.json')
        self.remote_allowed = False
        self.tool_dir = ''
        self.starts = {}
        self.owner = 'local'
        config = config_storage.read() if config_storage is not None else None
        try:
            if config is None:
                config = json.loads(self.config_path.read_text(encoding='utf-8'))
            self.remote_allowed = config.get('remoteAllowed') is True
            self.tool_dir = str(config.get('toolDir') or '')
        except (OSError, ValueError, AttributeError):
            pass
        try:
            records = json.loads(self.journal_path.read_text(encoding='utf-8'))
            self.starts = {key: value for key, value in records.items()
                           if re.fullmatch('[a-f0-9]{32}', key) and isinstance(value, dict)}
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    @property
    def service(self):
        return self._service() if callable(self._service) else self._service

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)

    def _save(self):
        data = {'toolDir': self.tool_dir, 'remoteAllowed': self.remote_allowed}
        if self.config_storage is not None:
            self.config_storage.write(data)
        else:
            self._write(self.config_path, data)

    def set_remote_permission(self, allowed):
        if type(allowed) is not bool:
            raise ValueError('远程权限值必须为布尔值')
        with self._lock:
            previous = self.remote_allowed
            self.remote_allowed = allowed
            try:
                self._save()
            except OSError:
                self.remote_allowed = previous
                raise
            return dict(success=True, output='远程烧写权限已保存', data={'remoteAllowed': allowed})

    def _status(self, arguments, owner):
        record = self.starts.get(arguments.get('requestId'))
        job = self.service.status()
        if record:
            if owner != 'local' and record.get('owner') != owner:
                raise PermissionError('该烧写任务不属于当前主控')
            if record.get('cancelled'):
                return dict(jobId='',state='cancelled',events=[],completed=0,total=0,sequence=0,
                            notice='已撤回尚未确认的请求，不会再次烧写')
            if not job or record.get('jobId') != job['jobId']:
                return dict(jobId=record.get('jobId') or '', state='interrupted', events=[], completed=0,
                            total=0, sequence=0, notice='执行端已重启或记录已结束，不会自动重复烧写')
        if job and owner != 'local' and self.owner != owner:
            if arguments.get('jobId') or record:
                raise PermissionError('该烧写任务不属于当前主控')
            return None
        if arguments.get('jobId') and (not job or job['jobId'] != arguments['jobId']):
            raise ValueError('烧写任务不存在')
        if arguments.get('requestId') and record is None:
            return None
        if job and 'after' in arguments:
            after = arguments['after']
            if type(after) is not int or after < 0:
                raise ValueError('日志游标无效')
            earliest = job['events'][0]['sequence'] if job['events'] else job['sequence'] + 1
            job['events'] = [event for event in job['events'] if event['sequence'] > after][:32]
            job['logTruncated'] = after + 1 < earliest
            job.pop('plan', None)
        return job

    def call(self, action, arguments=None):
        return self.call_for_owner(action, arguments, owner='local')

    def call_for_owner(self, action, arguments=None, *, owner='local'):
        try:
            arguments = {} if arguments is None else arguments
            if not isinstance(arguments, dict):
                raise ValueError('烧写参数必须为对象')
            if owner != 'local' and any(key in arguments for key in ('pythonPath', 'fastbootPath')):
                raise PermissionError('远程烧写只能使用从机内置工具，不能指定其他执行程序')
            with self._lock:
                if action == 'config':
                    data = dict(toolDir=self.tool_dir, remoteAllowed=self.remote_allowed,
                                job=self._status({}, owner))
                    if data['job']:
                        data['job']['events'] = []
                elif action == 'save':
                    model = self.service.call('catalog', {'toolDir': arguments.get('toolDir', '')})
                    if not model['success'] or not model['data']['toolValid']:
                        raise ValueError('请选择包含 flashall.bat、下载脚本及平台文件的完整固化包目录')
                    old = self.tool_dir
                    self.tool_dir = model['data']['toolDir']
                    try:
                        self._save()
                    except OSError:
                        self.tool_dir = old
                        raise
                    data = dict(toolDir=self.tool_dir, remoteAllowed=self.remote_allowed)
                elif action == 'browse':
                    path = arguments.get('path') or self.tool_dir or str(Path.home())
                    if not isinstance(path, str):
                        raise ValueError('浏览路径必须为字符串')
                    directory = Path(path).resolve()
                    entries = []
                    with os.scandir(directory) as iterator:
                        for item in itertools.islice(iterator, 1001):
                            try:
                                is_dir = item.is_dir()
                                entries.append(dict(name=item.name, path=item.path, isDir=is_dir,
                                                    size=0 if is_dir else item.stat().st_size))
                            except OSError:
                                continue
                    truncated = len(entries) > 1000
                    entries = sorted(entries[:1000], key=lambda item: (not item['isDir'], item['name'].casefold()))
                    data = dict(path=str(directory), parent=str(directory.parent), entries=entries, truncated=truncated)
                elif action == 'start':
                    request_id = arguments.get('requestId') or uuid.uuid4().hex
                    if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
                        raise ValueError('烧写请求标识无效')
                    if request_id in self.starts:
                        return dict(success=True, output='', data=self._status({'requestId': request_id}, owner))
                    if len(self.starts) >= 512:
                        raise ValueError('任务记录已满，请退出程序后归档烧写任务记录')
                    job = self.service.status()
                    if job and job['state'] == 'running':
                        raise ValueError('执行端已有烧写任务正在运行')
                    # 固定计划先校验，任务标识先落盘，再允许启动子进程。
                    self.service.plan(arguments)
                    self.starts[request_id] = dict(owner=owner, jobId='')
                    try:
                        self._write(self.journal_path, self.starts)
                    except OSError:
                        self.starts.pop(request_id)
                        raise
                    self.owner = owner
                    result = self.service.call('start', arguments)
                    if not result['success']:
                        return result
                    data = result['data']
                    self.starts[request_id]['jobId'] = data['jobId']
                    try:
                        self._write(self.journal_path, self.starts)
                    except OSError:
                        # 已接纳的任务不能因写日志失败被报告为可安全重试。
                        data['notice'] = '任务已开始，但记录保存失败，请保留当前窗口查询结果'
                elif action == 'status':
                    data = self._status(arguments, owner)
                elif action == 'cancel':
                    request_id = arguments.get('requestId')
                    if request_id and request_id not in self.starts:
                        if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id):
                            raise ValueError('烧写请求标识无效')
                        if len(self.starts)>=512:
                            raise ValueError('任务记录已满')
                        # 查询/撤回先到达时留下拒绝记录，迟到的开始请求也不能执行。
                        self.starts[request_id] = dict(owner=owner,jobId='',cancelled=True)
                        self._write(self.journal_path,self.starts)
                        data = self._status({'requestId':request_id},owner)
                        return dict(success=True,output=data['notice'],data=data)
                    job = self._status(arguments, owner)
                    if not job or not job.get('jobId'):
                        raise ValueError('没有可停止的烧写任务')
                    return self.service.call('cancel', {'jobId': job['jobId']})
                elif action in ('catalog', 'plan'):
                    return self.service.call(action, arguments)
                else:
                    raise ValueError('未知的烧写操作')
            return dict(success=True, output='', data=data)
        except (OSError, ValueError, PermissionError) as exc:
            return dict(success=False, output=str(exc))
