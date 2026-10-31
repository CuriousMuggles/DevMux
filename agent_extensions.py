"""Agent 扩展桥：复用注册服务与从机准入，不启动第二套硬件执行器。"""
import re

from extension_tools import RemoteToolError, remote_call


def action(description, properties=None, required=()):
    return {'description': description, 'inputSchema': {'type': 'object',
        'properties': properties or {}, 'required': list(required), 'additionalProperties': False}}


STRING = {'type': 'string'}
STRINGS = {'type': 'array', 'items': STRING}
DB_PLAN = {'toolDir': STRING, 'files': STRINGS, 'selected': STRINGS, 'platform': STRING,
    'overrides': {'type': 'object', 'additionalProperties': STRING},
    'options': {'type': 'object', 'properties': {
        'prepare': {'type': 'boolean'}, 'eraseEnv': {'type': 'boolean'},
        'reboot': {'type': 'boolean'},
        'dataMode': {'type': 'string', 'enum': ['erase', 'flash', 'format']},
        'secureChip': {'type': 'string', 'enum': ['auto', 'secure', 'non-secure']}},
        'additionalProperties': False}, 'serial': STRING, 'pythonPath': STRING, 'fastbootPath': STRING}
DESCRIPTIONS = {
    'serial': {
        'config': action('读取串口默认参数和独立权限；支持原始字节与XMODEM文件传输'),
        'ports': action('枚举所选电脑连接的 COM 口，不打开端口'),
        'open': action('打开或共用 COM 连接；默认921600/8/N/1，GUI输入优先；超时用同一requestId查询',
            {'port': STRING, 'requestId': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'},
             'baudrate': {'type': 'integer', 'minimum': 1, 'maximum': 12000000},
             'bytesize': {'type': 'integer', 'enum': [5, 6, 7, 8]},
             'parity': {'type': 'string', 'enum': ['N', 'E', 'O', 'M', 'S']},
             'stopbits': {'type': 'number', 'enum': [1, 1.5, 2]},
             'flowcontrol': {'type': 'string', 'enum': ['none', 'xonxoff', 'rtscts', 'dsrdtr']},
             'dtr': {'type': 'boolean'}, 'rts': {'type': 'boolean'}}, ['port', 'requestId']),
        'read': action('无损base64输出和字节游标；droppedBytes表示历史裁剪，不自动补发',
            {'sessionId': STRING, 'cursor': {'type': 'integer', 'minimum': 0},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 65536}}, ['sessionId']),
        'status': action('查看连接、输入权和当前generation；观察输出不代表取得输入权', {'sessionId': STRING}, ['sessionId']),
        'write': action('按原始字节发送text/hex/base64；必须携带generation，GUI接管后旧输入拒绝；超时禁止重发',
            {'sessionId': STRING, 'generation': STRING, 'data': STRING,
             'format': {'type': 'string', 'enum': ['text', 'hex', 'base64']},
             'encoding': {'type': 'string', 'enum': ['utf-8', 'gbk', 'ascii', 'latin-1']}},
            ['sessionId', 'generation', 'data']),
        'control': action('take申请输入权（不能抢GUI/其他Agent）；release交还，需当前generation',
            {'sessionId': STRING, 'generation': STRING, 'operation': {'type': 'string', 'enum': ['take', 'release']}},
            ['sessionId', 'generation', 'operation']),
        'close': action('释放自己的接入；最后一个接入释放物理COM，不关闭其他接入', {'sessionId': STRING}, ['sessionId'])},
    'relay': {
        'config': action('读取继电器配置与状态'),
        'save': action('保存执行端工具目录，不控制触点', {'toolDir': STRING}, ['toolDir']),
        'scan': action('查找执行端的继电器设备'),
        'open': action('逻辑打开已扫描设备，不改变触点', {'serial': STRING}, ['serial']),
        'close': action('逻辑关闭设备，触点保持不变'),
        'status': action('读取已打开设备的通道状态'),
        'set': action('改变触点；channel=255 表示全部通道，不自动重发',
            {'channel': {'type': 'integer', 'enum': [1, 2, 3, 4, 5, 6, 7, 8, 255]},
             'operation': {'type': 'string', 'enum': ['open', 'close']}}, ['channel', 'operation']),
        'diagnose': action('分阶段诊断执行端工具', {'stage': {'type': 'string',
            'enum': ['check', 'load', 'init', 'scan', 'status']}, 'serial': STRING}, ['stage'])},
    'db-flash': {
        'config': action('读取执行端目录、权限与当前任务'),
        'save': action('验证并保存执行端固化包目录', {'toolDir': STRING}, ['toolDir']),
        'browse': action('只读浏览执行端目录，不传输镜像', {'path': STRING}),
        'catalog': action('识别工具与候选镜像，只读', {'toolDir': STRING, 'files': STRINGS, 'platform': STRING}),
        'plan': action('生成固定烧写计划，不执行', DB_PLAN),
        'start': action('开始异步烧写；相同 requestId 不重复执行，失联后查询原请求',
            {**DB_PLAN, 'requestId': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}}, ['requestId']),
        'status': action('查询任务及增量日志，after 每页最多32事件', {'jobId': STRING,
            'requestId': STRING, 'after': {'type': 'integer', 'minimum': 0}}),
        'cancel': action('停止所属任务或撤回未确认请求，沿用专用烧写取消策略',
            {'jobId': STRING, 'requestId': STRING})}}


DESCRIPTIONS['mijia'] = {
    'status': action('查询本机米家登录状态；请在界面登录'),
    'snapshot': action('列出本机可操作的在线开关；操作前先查询', {'refreshCatalog': {'type': 'boolean'}}),
    'read-switch': action('读取设备开关状态', {'did': STRING}, ['did']),
    'set-switch': action('设置设备开关并读取结果', {'did': STRING, 'value': {'type': 'boolean'}}, ['did', 'value'])}


DESCRIPTIONS['serial'].update({
    'xmodem-start': action('准备独占传输，未知结果使用同一requestId查询；8位且禁XON/XOFF，至多64MiB',
        {'sessionId': STRING, 'generation': STRING, 'requestId': STRING,
         'direction': {'type': 'string', 'enum': ['send', 'receive']},
         'size': {'type': 'integer', 'minimum': 0, 'maximum': 67108864},
         'blockSize': {'type': 'integer', 'enum': [128, 1024]},
         'checksum': {'type': 'string', 'enum': ['crc', 'checksum']}},
        ['sessionId', 'generation', 'requestId', 'direction']),
    'xmodem-upload': action('按连续offset上传base64块（至多32KiB），相同内容可查询重传',
        {'sessionId': STRING, 'transferId': STRING, 'offset': {'type': 'integer', 'minimum': 0}, 'data': STRING},
        ['sessionId', 'transferId', 'offset', 'data']),
    'xmodem-run': action('幂等启动原任务，绝不重新发送已结束文件',
        {'sessionId': STRING, 'transferId': STRING}, ['sessionId', 'transferId']),
    'xmodem-status': action('查询原传输；也可从read/status的xmodem字段观察进度',
        {'sessionId': STRING, 'transferId': STRING}, ['sessionId', 'transferId']),
    'xmodem-download': action('成功接收后下载base64块，保留协议尾部填充',
        {'sessionId': STRING, 'transferId': STRING, 'offset': {'type': 'integer', 'minimum': 0},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 32768}}, ['sessionId', 'transferId']),
    'xmodem-cancel': action('取消所属传输，运行时发送CAN；不等同回滚设备已接收字节',
        {'sessionId': STRING, 'transferId': STRING}, ['sessionId', 'transferId'])})


def describe(tool, actions, *, remote_capable=True):
    descriptions = DESCRIPTIONS.get(tool, {})
    result = {name: descriptions.get(name, {'description': '参数由执行端扩展校验',
        'inputSchema': {'type': 'object'}}) for name in actions}
    result['capabilities'] = action('查询能力和独立权限')
    if remote_capable:
        result['permission'] = action('仅在执行电脑本机修改该扩展的远程权限',
            {'allowed': {'type': 'boolean'}}, ['allowed'])
    return result


async def invoke(api, name, args):
    manager = api.manager
    if name == 'extensions_list':
        target=api.resolve(args.get('target','computer:local'),'computer')
        if not target['local']:
            response=await manager._run_blocking(remote_call,manager.relay,target['nodeId'],'__plugin_catalog__','list',{})
            if not response.get('success'):raise RuntimeError(response.get('output','无法读取从机插件列表'))
            tools=[{'tool':item['tool'],'name':item['name'],'enabled':item['enabled'],
                    'remoteAllowed':item['remoteAllowed'],'actions':describe(item['tool'],item.get('actions',[]))}
                   for item in response.get('data',{}).get('plugins',[]) if item.get('remoteCapable')]
            try:
                serial=await manager._run_blocking(remote_call,manager.relay,target['nodeId'],'serial','capabilities',{})
                if serial.get('success'):
                    data=serial['data'];tools.insert(0,{'tool':'serial','name':data['name'],'enabled':True,
                        'remoteAllowed':data['remoteAllowed'],'actions':describe('serial',data['actions'])})
            except Exception:pass  # A peer may have no serial service; plugin discovery still applies.
            return {'target':args['target'],'tools':tools}
        return {'tools': [{'tool': key, 'name': service.NAME,
            'actions': describe(key, sorted(service.ACTIONS),remote_capable=getattr(service,'remote_supported',True))}
            for key, service in sorted(manager.extension_tools.services.copy().items())]}
    target = api.resolve(args['target'], 'computer')
    tool = args['tool']
    operation = 'capabilities' if name == 'extension_capabilities' else args['action']
    arguments = args.get('arguments', {})
    if tool == 'mijia':
        if not target['local']:
            raise PermissionError('米家仅支持本机调用')
        if operation != 'capabilities':
            schema = DESCRIPTIONS['mijia'].get(operation, {}).get('inputSchema')
            if not schema or not isinstance(arguments, dict) or set(arguments) - set(schema['properties']) or set(schema['required']) - set(arguments):
                raise ValueError('米家动作或参数无效')
            for key, value in arguments.items():
                kind = schema['properties'][key]['type']
                if (kind == 'boolean' and type(value) is not bool) or (kind == 'string' and not isinstance(value, str)):
                    raise ValueError(f'参数 {key} 类型无效')
    # 薄客户端先生成标识；直接使用认证 WS 的调用者也必须事先知道原请求标识。
    if (tool == 'db-flash' and operation == 'start') or (tool == 'serial' and operation == 'open'):
        identifier = arguments.get('requestId')
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('该动作必须提供32位十六进制 requestId，用于超时查询和防重复')
    try:
        if target['local']:
            result = await manager._run_blocking(manager.extension_tools.call, tool, operation, arguments)
        else:
            result = await manager._run_blocking(remote_call, manager.relay, target['nodeId'], tool, operation, arguments)
    except RemoteToolError as exc:
        result = {'success': False, 'output': str(exc), 'operationUncertain': exc.operation_uncertain,
                  'data': {'requestId': arguments.get('requestId'), 'jobId': arguments.get('jobId')}}
    if operation == 'capabilities' and result.get('success'):
        data = result['data']
        remote_capable=data.get('remoteCapable',True)
        result = {**result, 'data': {**data, 'agentActions': describe(tool, data['actions'],remote_capable=remote_capable),
            'restrictions': (['远程不能修改执行端权限', '远程DB不能自定义pythonPath或fastbootPath',
                             '结果不确定时不得自动重复物理动作'] if remote_capable else ['仅支持本机调用'])}}
    return {**result, 'target': args['target'], 'tool': tool, 'action': operation,
            'requestId': arguments.get('requestId')}
