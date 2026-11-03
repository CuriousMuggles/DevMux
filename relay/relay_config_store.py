"""当前实例的统一配置存储；共享目录协议文件和烧写任务记录不经过这里。"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import weakref

_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


def _path_lock(path):
    key = os.path.normcase(os.path.realpath(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _read_object(path):
    try:
        with Path(path).open(encoding='utf-8-sig') as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise OSError(f'配置文件损坏，保留原文件：{path}') from exc
    if not isinstance(value, dict):
        raise OSError(f'配置必须为JSON对象：{path}')
    return value


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.lock = _path_lock(self.path)

    def _read(self):
        data = _read_object(self.path)
        if data is None:
            return {}
        if data.get('storageVersion', 1) != 1:
            raise OSError('不支持的统一配置版本，未修改文件')
        private = data.get('private', {})
        if not isinstance(private, dict):
            raise OSError('统一配置private小节损坏')
        for group in ('pairings', 'trust', 'tools'):
            if group in private and not isinstance(private[group], dict):
                raise OSError(f'统一配置{group}小节损坏')
        return data

    def read(self):
        with self.lock:
            return self._read()

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + '.', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def update_config(self, values):
        with self.lock:
            data = self._read()
            data.update(deepcopy(values))
            self._write(data)

    def section(self, group, key, legacy_path):
        return ConfigSection(self, group, key, legacy_path)


class ConfigSection:
    def __init__(self, store, group, key, legacy_path):
        self.store = store
        self.group, self.key = group, key
        self.legacy_path = Path(legacy_path)
        self._observed = None

    def _validate(self, value):
        if not isinstance(value, dict):
            raise OSError(f'统一配置{self.group}/{self.key}必须为对象')
        records_key = {'pairings': 'pairs', 'trust': 'peers'}.get(self.group)
        if records_key:
            records = value.get(records_key)
            if not isinstance(records, dict) or any(not isinstance(v, dict) for v in records.values()):
                raise OSError(f'统一配置{self.group}/{self.key}名单损坏')
        else:
            if 'remoteAllowed' in value and type(value['remoteAllowed']) is not bool:
                raise OSError(f'统一配置工具{self.key}权限必须为布尔值')
            if 'toolDir' in value and not isinstance(value['toolDir'], str):
                raise OSError(f'统一配置工具{self.key}路径必须为字符串')
        return value

    def _get(self, data):
        group = data.get('private', {}).get(self.group, {})
        if self.key in group:
            return self._validate(group[self.key])
        return None

    def _put(self, data, value):
        data.setdefault('private', {}).setdefault(self.group, {})[self.key] = deepcopy(value)
        data['storageVersion'] = 1

    def read(self):
        with self.store.lock:
            data = self.store._read()
            value = self._get(data)
            if value is None:
                value = _read_object(self.legacy_path)
                if value is not None:
                    self._validate(value)
                    self._put(data, value)
                    self.store._write(data)
                    if self._get(self.store._read()) != value:
                        raise OSError('迁移后复核失败，旧配置保持原位')
                else:
                    value = {}
            self._observed = deepcopy(value)
            return deepcopy(value)

    def write(self, value):
        self._validate(value)
        with self.store.lock:
            if self._observed is None:
                self.read()
            data = self.store._read()
            current = self._get(data)
            if (current if current is not None else {}) != self._observed:
                raise OSError('同一配置小节已被更新，请刷新后重试')
            self._put(data, value)
            self.store._write(data)
            self._observed = deepcopy(value)
