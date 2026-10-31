"""扩展工具注册与本机/中转选路：固定接口、目标权限由执行端裁决。"""
import relay


class RemoteToolError(RuntimeError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.operation_uncertain = uncertain


class ExtensionTools:
    def __init__(self, services):
        self.services = dict(services)
        self.plugin_manager = None

    def call(self, tool, action, arguments=None, *, remote=False, authorize=None, caller='agent'):
        if tool == '__plugin_catalog__':
            if not remote or authorize is None or self.plugin_manager is None:
                raise PermissionError('插件管理仅允许已验证的远程连接')
            with self.plugin_manager.lock:
                owner=authorize()
                if not owner:raise PermissionError('缺少已验证的主控身份')
                return {'success':True,'output':'','data':self.plugin_manager.remote.call(action,arguments or {},owner)}
        service = self.services.get(tool)
        if service is None:
            raise ValueError('执行端未安装或未启用该工具插件')
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise ValueError('扩展工具参数必须为对象')
        if caller not in ('gui', 'agent'):
            raise ValueError('扩展调用来源无效')
        remote_supported=getattr(service,'remote_supported',True)
        if remote and not remote_supported:
            raise PermissionError('该插件仅支持执行端本机调用')
        # 锁覆盖权限检查与调用，禁止撤销权限和排队操作相互穿越。
        with service._lock:
            owner = 'local'
            if authorize is not None:
                owner = authorize() or ''
            if action == 'capabilities':
                return dict(success=True, output='', data=dict(tool=tool, version=1,
                    name=service.NAME, allowed=not remote or service.remote_allowed,
                    remoteAllowed=service.remote_allowed, remoteCapable=remote_supported,
                    actions=sorted(getattr(service,'remote_actions',service.ACTIONS) if remote else service.ACTIONS)))
            if action == 'permission':
                if not remote_supported:raise PermissionError('该插件不提供远程权限设置')
                if remote:
                    raise PermissionError('远程权限只能在执行端本机修改')
                return service.set_remote_permission(arguments.get('allowed'))
            if remote and not service.remote_allowed:
                raise PermissionError(f'从机未允许远程{service.NAME}控制，请在从机{service.NAME}面板开启')
            if action not in service.ACTIONS:
                raise ValueError('不支持的扩展工具动作')
            if remote and action not in getattr(service,'remote_actions',service.ACTIONS):
                raise PermissionError('该动作仅支持执行端本机调用')
            if hasattr(service, 'call_for_context'):
                if remote and not owner:
                    raise PermissionError('远程接入缺少已验证的主控身份')
                return service.call_for_context(action, arguments,
                    owner=owner if remote else 'local', caller=caller)
            if hasattr(service, 'call_for_owner'):
                if remote and not owner:
                    raise PermissionError('远程任务缺少已验证的主控身份')
                return service.call_for_owner(action, arguments, owner=owner if remote else 'local')
            return service.call(action, arguments)

    def close_owner(self, owner=None):
        """撤销连接的持续接入；不会结束其他工具的持久硬件任务。"""
        for service in list(self.services.values()):
            if hasattr(service, 'close_owner'):
                service.close_owner(owner, remote_only=owner is None)


def remote_call(controller, node_id, tool, action, arguments, *, caller='agent'):
    node = controller.node
    if node is None or node.role != relay.ROLE_MASTER:
        raise RuntimeError('请在主控端启用并确认跨机中转连接')
    candidates = [(peer, beat) for peer, beat in node._peers.items() if beat.nodeId == node_id]
    if len(candidates) != 1:
        raise ValueError('目标实例不存在或路由有歧义')
    peer, beat = candidates[0]
    if beat.role != relay.ROLE_SLAVE:
        raise PermissionError('远程扩展工具目标必须为从机')
    if not node.connection_ready(peer):
        raise RuntimeError('目标连接尚未验证，请刷新连接或重新确认')
    if tool=='serial':
        key=(node_id,arguments.get('sessionId'),caller)
        with controller._lock:
            clients=getattr(controller,'_serial_clients',{})
            client=clients.get(key)
        if client is not None and client.node is node:
            if action=='read':return dict(success=True,output='',data=client.read(arguments))
            if action in ('write','control','close'):
                data=client.send(action,arguments)
                if action=='close':
                    with controller._lock:clients.pop(key,None)
                return dict(success=True,output='',data=data)
    result = node.submit(peer, relay.KIND_EXTENSION_TOOL,
        payload={'tool': tool, 'action': action, 'args': arguments, 'caller': caller,
                 **({'serialStreamProtocol':1} if tool=='serial' and action=='open' else {})}, timeout=40, ttl_s=40)
    if result.status != relay.STATUS_OK:
        reason = result.error or '远程扩展工具执行失败'
        if '未实现该请求类型' in reason:
            reason += '；请升级从机'
        if result.extra.get('localTimeout'):
            reason += '；结果未确认，不会自动重试'
        raise RemoteToolError(reason, uncertain=bool(result.extra.get('localTimeout')))
    data = result.extra.get('data')
    if not isinstance(data, dict):
        raise RuntimeError('从机未返回扩展工具结果，请升级从机')
    opened=data.get('data',{})
    if tool=='serial' and action=='open' and opened.get('serialStreamProtocol')==1:
        import re
        from serial_stream import MasterSerialStream
        identifier=opened.get('sessionId','')
        if not re.fullmatch('[a-f0-9]{32}',identifier) or opened.get('streamId')!='serial-'+identifier:
            raise ValueError('从机返回非法串口流标识')
        with controller._lock:
            if controller.node is not node:raise RuntimeError('打开串口期间节点已停止')
            if not hasattr(controller,'_serial_clients'):controller._serial_clients={}
            key=(node_id,identifier,caller)
            if key not in controller._serial_clients:
                controller._serial_clients[key]=MasterSerialStream(node,peer,opened,caller)
    return data
