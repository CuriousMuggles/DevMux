"""Permission-scoped device access for plugin windows, through the host router."""

async def dispatch(manager, record, arguments):
    if 'device-adb' not in record.get('plugin_permissions', []):
        raise PermissionError('插件清单未声明 device-adb 权限')
    if not isinstance(arguments, dict):
        raise ValueError('设备接口参数必须为对象')
    operation = arguments.get('operation')
    computer = record.get('plugin_target', 'computer:local')
    node_id = None
    if computer != 'computer:local':
        node_id = manager.agent.resolve(computer, 'computer')['nodeId']

    def permitted(target):
        return target.get('kind') == 'device' and (node_id is None or
            target.get('device', {}).get('relay', {}).get('nodeId') == node_id)

    if operation == 'targets':
        return {'targets': [item for item in manager.agent.targets() if permitted(item)]}
    if operation not in ('exec', 'deploy'):
        raise ValueError('不支持的设备接口动作')
    target = manager.agent.resolve(arguments.get('target'), 'device')
    if not permitted(target):
        raise PermissionError('远程插件窗口只能操作原从机上的设备')
    if operation == 'deploy':
        import base64
        import hashlib
        import tempfile
        from pathlib import Path
        raw = arguments.get('data')
        if not isinstance(raw, str) or len(raw) > 4 * 1024 * 1024:
            raise ValueError('采集器大小无效')
        binary = base64.b64decode(raw, validate=True)
        digest = hashlib.sha256(binary).hexdigest()
        if not binary.startswith(b'\x7fELF') or arguments.get('sha256') != digest:
            raise ValueError('采集器格式或摘要无效')
        destination = '/tmp/devmux-ebpf-' + digest[:24]
        # Only a host-created temporary file is exposed to the existing transfer
        # router. A plugin cannot request arbitrary host files or destinations.
        with tempfile.TemporaryDirectory(prefix='devmux-ebpf-') as directory:
            path = (Path(directory) / 'collector').resolve()
            path.write_bytes(binary)
            result = await manager.agent.call('files_transfer', {
                'target': target['target'], 'direction': 'push',
                'paths': [str(path)], 'destination': destination})
        if not result.get('success'):
            return result
        return {'success': True, 'data': {'path': destination, 'sha256': digest}}
    # Agent validation and execution retain selector checks, output limits,
    # the verified relay connection and the execution computer's ADB policy.
    return await manager.agent.call('adb_exec', {
        'target': target['target'], 'argv': arguments.get('argv'),
        'timeout': arguments.get('timeout', 30)})
