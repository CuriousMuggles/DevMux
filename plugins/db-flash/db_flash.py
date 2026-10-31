"""DB 固化包识别、烧写计划与后台执行；原固化包保持只读。"""
from dataclasses import dataclass, asdict
from pathlib import Path
import copy
import threading
import uuid
import os
import sys
import shutil
import subprocess
import codecs
import base64
import time

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


@dataclass(frozen=True)
class ImageSpec:
    id: str
    label: str
    field: str
    suffix: str
    default: str
    partitions: tuple
    secure_default: str = ''


SPECS = (
    ImageSpec('spl', 'SPL 启动程序', 'spl_im_signed', '.img', 'images/spl/spl_im_signed.img', ('mmc0boot0', 'mmc0boot1')),
    ImageSpec('gpt', 'GPT 分区表', 'gpt', '.bin', 'images/gpt/gpt.bin', ('gpt',)),
    ImageSpec('preboot', '启动固件', 'preboot', '.itb', 'images/preboot.itb', ('preboot',)),
    ImageSpec('boot', '内核与设备树', 'boot', '.itb', 'images/boot.itb', ('boot',)),
    ImageSpec('rootfs', '根文件系统', 'rootfs', '.img', 'images/rootfs.img', ('rootfs',)),
    ImageSpec('system', '系统文件', 'system', '.img', 'images/system.img', ('system',)),
    ImageSpec('data', '用户数据', 'userdata', '.img', 'images/userdata.img', ('data',)),
)
SPEC_BY_ID = {spec.id: spec for spec in SPECS}

# p300-udp 固化包：download_image 使用 0x696d/0x7633
# 进入 BootROM，flashall.bat 按 is-secure-chip 切换签名/加密镜像，并额外烧写 env、ddr。
# 当前 p300-udp 固化包的 flashall.bat 不烧写 system，p300 平台不提供该项目。
P300_UDP_SPECS = (
    ImageSpec('spl', 'SPL 启动程序', 'spl_im_signed', '.img', 'images/spl/spl_im_signed.img',
              ('mmc0boot0', 'mmc0boot1'), secure_default='images/spl/spl_im_signed.img.pro.enc'),
    ImageSpec('gpt', 'GPT 分区表', 'gpt', '.bin', 'images/gpt/gpt.bin', ('gpt',)),
    ImageSpec('env', '启动环境', 'uboot_env', '.img', 'images/u-boot/uboot_env.img', ('env',)),
    ImageSpec('ddr', 'DDR 固件', 'lpddr5_fw', '.bin', 'images/lpddr_fw/lpddr5_fw.bin', ('ddr',)),
    ImageSpec('preboot', '启动固件', 'preboot', '.itb', 'images/preboot.itb', ('preboot',),
              secure_default='images/preboot_sign.itb'),
    ImageSpec('boot', '内核与设备树', 'boot', '.itb', 'images/boot.itb', ('boot',)),
    ImageSpec('rootfs', '根文件系统', 'rootfs', '.img', 'images/rootfs.img', ('rootfs',)),
    ImageSpec('data', '用户数据', 'userdata', '.img', 'images/userdata.img', ('data',)),
)
P300_UDP_SPEC_BY_ID = {spec.id: spec for spec in P300_UDP_SPECS}

# P400 固化包：独立识别其 is-secure-chip 流程，镜像项目按 flashall.bat 实际分区烧写项。
P400_SPECS = (
    ImageSpec('spl', 'SPL 启动程序', 'spl_im_signed', '.img', 'images/spl/spl_im_signed.img',
              ('mmc0boot0', 'mmc0boot1'), secure_default='images/spl/spl_im_signed.img.pro.enc'),
    ImageSpec('gpt', 'GPT 分区表', 'gpt', '.bin', 'images/gpt/gpt.bin', ('gpt',)),
    ImageSpec('ddr', 'DDR 固件', 'lpddr5_fw', '.bin', 'images/lpddr_fw/lpddr5_fw.bin', ('ddr',)),
    ImageSpec('preboot', '启动固件', 'preboot', '.itb', 'images/preboot.itb', ('preboot',),
              secure_default='images/preboot_sign.itb'),
    ImageSpec('boot', '内核与设备树', 'boot', '.itb', 'images/boot.itb', ('boot',)),
    ImageSpec('rootfs', '根文件系统', 'rootfs', '.img', 'images/rootfs.img', ('rootfs',)),
    ImageSpec('system', '系统文件', 'system', '.img', 'images/system.img', ('system',)),
    ImageSpec('data', '用户数据', 'userdata', '.img', 'images/userdata.img', ('data',)),
)


@dataclass(frozen=True)
class Platform:
    id: str
    label: str
    specs: tuple
    required: tuple
    data_modes: tuple
    default_data_mode: str
    prepare_spl: str
    prepare_uboot: str
    prepare_spl_args: tuple
    prepare_uboot_args: tuple
    secure_chip: bool = False


PLATFORMS = (
    Platform('legacy', '标准 DB 固化包', SPECS,
             ('flashall.bat', 'flash_all.sh', 'scripts/download_image.py'),
             (('erase', '擦除用户数据（原脚本 ALL 默认）'), ('flash', '烧写用户数据镜像')),
             'erase', 'images/spl/spl_im_signed.img', 'images/u-boot/u-boot.bin',
             ('-p', '2', '-v', '0x3902', '-u', '0x7734'), ('-v', '0x3902', '-u', '0x7634')),
    Platform('p500', 'p500', SPECS,
             ('flashall.bat', 'flash_all.sh', 'scripts/download_image.py'),
             (('erase', '擦除用户数据（原脚本 ALL 默认）'), ('flash', '烧写用户数据镜像')),
             'erase', 'images/spl/spl_im_signed.img', 'images/u-boot/u-boot.bin',
             ('-p', '2', '-v', '0x3902', '-u', '0x7734'), ('-v', '0x3902', '-u', '0x7634')),
    Platform('p300-udp', 'p300-udp', P300_UDP_SPECS,
             ('flashall.bat', 'scripts/download_image.py'),
             (('format', '格式化用户数据（对应 flash_cfg.ini 的 format data）'),
              ('flash', '烧写用户数据镜像'), ('erase', '擦除用户数据')),
             'format', 'images/spl/spl_im_burn_signed.img', 'images/u-boot/u-boot.bin',
             ('-p', '2', '-v', '0x696d', '-u', '0x7633'), ('-v', '0x3902', '-u', '0x7634'),
             secure_chip=True),
    Platform('p400', 'p400', P400_SPECS,
             ('flashall.bat', 'scripts/download_image.py'),
             (('format', '烧写 userdata.img 后格式化 data（P400 原脚本顺序）'),
              ('flash', '仅烧写用户数据镜像'), ('erase', '擦除用户数据')),
             'format', 'images/spl/spl_im_burn_signed.img', 'images/u-boot/u-boot.bin',
             ('-p', '2', '-v', '0x3902', '-u', '0x7734'), ('-v', '0x3902', '-u', '0x7634'),
             secure_chip=True),
)
PLATFORM_BY_ID = {platform.id: platform for platform in PLATFORMS}


def _read_text(path):
    try:
        return path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return ''


def detect_platform(root):
    """按固化包脚本特征识别平台；无法识别时保持原有标准流程。"""
    if root is None or not root.is_dir():
        return 'legacy', '未选择工具目录'
    flashall = _read_text(root / 'flashall.bat').casefold()
    config = _read_text(root / 'flash_cfg.ini').casefold()
    # P400 与 p300 都检测安全芯片；用 P400 独有的 userdata/system 烧写特征优先区分。
    if ('is-secure-chip' in flashall and 'fastboot flash system' in flashall
            and 'fastboot flash data images/userdata.img' in flashall):
        return 'p400', '检测到 P400 的 system 与 userdata 烧写流程'
    # P500 等平台也会使用 0x696d/0x7633 和同名 flash_cfg.ini；
    # p300-udp 需由安全芯片检测或 env+ddr 镜像流程确认。
    has_env_and_ddr = all(marker in (flashall + '\n' + config)
                          for marker in ('uboot_env.img', 'lpddr5_fw.bin'))
    if 'is-secure-chip' in flashall or 'is-secure-chip' in config or has_env_and_ddr:
        return 'p300-udp', '检测到 p300-udp 的安全芯片或 env/ddr 烧写流程'
    if (root / 'flash_all.sh').is_file():
        return 'p500', '检测到 P500 固化包脚本 flash_all.sh'
    return 'legacy', '未检测到专用平台特征，沿用标准流程'


def resolve_platform(root, requested=''):
    if requested:
        if not isinstance(requested, str) or requested not in PLATFORM_BY_ID:
            raise ValueError('未知的烧写平台')
        return PLATFORM_BY_ID[requested], False, '手动指定'
    platform_id, reason = detect_platform(root)
    return PLATFORM_BY_ID[platform_id], True, reason


def recognize(path, specs=SPECS):
    """完整字段子串匹配；包含关系取长字段，其他冲突交给用户处理。"""
    name = Path(str(path)).name.casefold()
    matches = [s for s in specs if s.field in name]
    matches = [s for s in matches if not any(s.field != other.field and s.field in other.field for other in matches)]
    if not matches:
        return {'path': str(path), 'state': 'unknown', 'matches': [], 'reason': '无法自动识别，可在列表中手动指定'}
    ids = [s.id for s in matches]
    if len(ids) > 1:
        return {'path': str(path), 'state': 'ambiguous', 'matches': ids, 'reason': '同时包含多个烧写字段，请手动指定'}
    if Path(name).suffix != matches[0].suffix:
        return {'path': str(path), 'state': 'invalid', 'matches': ids, 'reason': f'该项要求 {matches[0].suffix} 文件'}
    return {'path': str(path), 'state': 'matched', 'matches': ids, 'reason': ''}


def inspect_file(path):
    file = Path(path)
    try:
        exists = file.is_file()
        size = file.stat().st_size if exists else 0
    except OSError:
        exists, size = False, 0
    return {'path': str(file), 'exists': exists, 'size': size}


def catalog(tool_dir, files=None, platform=''):
    if not isinstance(tool_dir, str):
        raise ValueError('工具目录必须为路径字符串')
    root = Path(tool_dir.strip()).resolve() if tool_dir.strip() else None
    if not isinstance(files, (list, tuple, type(None))):
        raise ValueError('待烧写文件必须为路径列表')
    model_platform, detected, reason = resolve_platform(root, platform)
    paths = []
    for value in files or []:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('待烧写文件路径无效')
        path = str(Path(value).resolve())
        if path not in paths:
            paths.append(path)
    identified = [recognize(p, model_platform.specs) for p in paths]
    items = []
    for spec in model_platform.specs:
        default = inspect_file(root / spec.default) if root else {'path': '', 'exists': False, 'size': 0}
        matches = [p['path'] for p in identified if p['state'] == 'matched' and p['matches'] == [spec.id]]
        candidates = [inspect_file(p) for p in matches]
        # 多个匹配项不能偷偷取第一项，也不能回退到默认镜像。
        effective = candidates[0] if len(candidates) == 1 else (default if not candidates else {'path': '', 'exists': False, 'size': 0})
        items.append({
            **asdict(spec), **effective, 'defaultFile': default, 'candidates': candidates,
            'checked': bool(matches) if paths else True,
            'conflict': len(candidates) > 1, 'source': 'selected' if candidates else 'default',
        })
    options = {
        'prepare': True,
        'eraseEnv': not bool(paths) and not model_platform.secure_chip,
        'reboot': not bool(paths),
        'dataMode': ('flash' if any(p['state'] == 'matched' and p['matches'] == ['data'] for p in identified)
                     else model_platform.default_data_mode),
        'dataModes': [{'value': value, 'label': label} for value, label in model_platform.data_modes],
    }
    if model_platform.secure_chip:
        options['secureChip'] = 'auto'
    return {
        'toolDir': str(root) if root else '', 'platform': model_platform.id,
        'platformLabel': model_platform.label, 'platformDetected': detected, 'platformReason': reason,
        'items': items, 'files': identified,
        'issues': [p for p in identified if p['state'] != 'matched'],
        'options': options,
        'simulation': True,
        'toolValid': bool(root) and root.is_dir() and all((root / p).is_file() for p in model_platform.required),
        'toolIssues': ([f'工具目录缺少 {p}' for p in model_platform.required if not (root / p).is_file()] if root else ['请先选择工具目录']),
    }


@dataclass(frozen=True)
class Step:
    id: str
    label: str
    argv: tuple
    cwd: str
    image: str = ''
    secureImage: str = ''


def _bool(options, key, default):
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f'{key} 必须为布尔值')
    return value


def _image(path, suffix):
    if not isinstance(path, str) or not path.strip():
        raise ValueError('请选择明确的镜像文件')
    file = Path(path).resolve()
    if file.suffix.casefold() != suffix:
        raise ValueError(f'{file.name} 必须使用 {suffix} 扩展名')
    if not file.is_file() or not file.stat().st_size:
        raise ValueError(f'镜像缺失或为空：{file}')
    return str(file)


def build_plan(arguments):
    """只接受固定烧写项，路径用 argv 独立参数表示，绝不拼接 shell 命令。"""
    if not isinstance(arguments, dict):
        raise ValueError('烧写参数必须为对象')
    model = catalog(arguments.get('toolDir', ''), arguments.get('files', []), arguments.get('platform', ''))
    platform = PLATFORM_BY_ID[model['platform']]
    root = Path(model['toolDir']) if model['toolDir'] else None
    if root is None or not root.is_dir():
        raise ValueError('请先确认工具所在目录')
    for relative in platform.required:
        if not (root / relative).is_file():
            raise ValueError(f'工具目录缺少 {relative}')
    spec_ids = [spec.id for spec in platform.specs]
    selected = arguments.get('selected', [i['id'] for i in model['items'] if i['checked']])
    if not isinstance(selected, list) or any(not isinstance(i, str) or i not in spec_ids for i in selected):
        raise ValueError('烧写项无效')
    if len(selected) != len(set(selected)):
        raise ValueError('烧写项重复')
    if not selected:
        raise ValueError('请至少勾选一个烧写项')
    overrides = arguments.get('overrides', {})
    options = arguments.get('options', model['options'])
    if not isinstance(overrides, dict) or not isinstance(options, dict):
        raise ValueError('文件对应关系或选项无效')
    if any(key not in spec_ids for key in overrides):
        raise ValueError('存在未知的文件对应关系')
    prepare = _bool(options, 'prepare', True)
    reboot = _bool(options, 'reboot', False)
    if not platform.secure_chip:
        erase_env = _bool(options, 'eraseEnv', False)
        data_mode = options.get('dataMode', 'erase')
        secure_chip = ''
    else:
        erase_env = False
        data_mode = options.get('dataMode', platform.default_data_mode)
        secure_chip = options.get('secureChip', 'auto')
        if secure_chip not in ('auto', 'secure', 'non-secure'):
            raise ValueError('安全芯片选项无效')
    if data_mode not in [value for value, _label in platform.data_modes]:
        raise ValueError('用户数据处理方式无效')
    serial = arguments.get('serial', '')
    if not isinstance(serial, str) or any(c.isspace() or ord(c) < 32 for c in serial):
        raise ValueError('目标序列号不能包含空白或控制字符')
    # 留空时真实执行器只接受唯一的 fastboot 设备，并固定其序列号。
    python = arguments.get('pythonPath') or 'python'
    fastboot = arguments.get('fastbootPath') or 'fastboot'
    if not isinstance(python, str) or not isinstance(fastboot, str):
        raise ValueError('工具程序路径无效')
    fb = (fastboot,) + (('-s', serial) if serial else ())
    steps = []

    def add(id, label, argv, image='', secure_image=''):
        steps.append(Step(id, label, tuple(argv), str(root), image, secure_image))

    if prepare:
        spl = _image(str(root / platform.prepare_spl), '.img')
        uboot = _image(str(root / platform.prepare_uboot), '.bin')
        script = str(root / 'scripts/download_image.py')
        add('prepare-spl', '临时下载 SPL，等待开发板复位',
            (python, script, '-i', spl, '-m', 'bootrom_download', '-d', '0', *platform.prepare_spl_args), spl)
        add('prepare-uboot', '临时下载烧写用 U-Boot',
            (python, script, '-i', uboot, '-m', 'bootrom_download', '-d', '0', *platform.prepare_uboot_args), uboot)
    add('verify-mode', '设置原脚本的镜像校验模式', fb + ('oem', 'run:im_verify set none'))
    if platform.secure_chip and secure_chip == 'auto' and ({'spl', 'preboot'} & set(selected)):
        add('secure-check', '检测安全芯片并选择对应镜像', fb + ('getvar', 'is-secure-chip'))
    lookup = {i['id']: i for i in model['items']}
    # 前端传入顺序无效，固定沿用原脚本的烧写顺序。
    for spec in platform.specs:
        if spec.id == 'preboot' and erase_env:
            add('erase-env', '擦除启动环境', fb + ('erase', 'env'))
        if spec.id not in selected:
            continue
        if spec.id == 'data':
            if data_mode == 'erase':
                add('erase-data', '擦除用户数据（原脚本默认）', fb + ('erase', 'data'))
            elif data_mode == 'format':
                if platform.id == 'p400':
                    item = lookup[spec.id]
                    path = overrides.get(spec.id)
                    if path is None:
                        if item['conflict']:
                            raise ValueError(f'{spec.label} 有多个候选文件，请指定一个')
                        path = item['path']
                    image = _image(path, spec.suffix)
                    add('flash-data', '烧写用户数据 → data（P400 原脚本）', fb + ('flash', 'data', image), image)
                    add('format-data', '格式化用户数据（P400 原脚本）', fb + ('format', 'data'))
                else:
                    add('format-data', '格式化用户数据（对应 flash_cfg.ini 的 format data）', fb + ('format', 'data'))
            else:
                item = lookup[spec.id]
                path = overrides.get(spec.id)
                if path is None:
                    if item['conflict']:
                        raise ValueError(f'{spec.label} 有多个候选文件，请指定一个')
                    path = item['path']
                image = _image(path, spec.suffix)
                add('flash-data', f'烧写{spec.label} → data', fb + ('flash', 'data', image), image)
            continue
        item = lookup[spec.id]
        path = overrides.get(spec.id)
        if path is None:
            if item['conflict']:
                raise ValueError(f'{spec.label} 有多个候选文件，请指定一个')
            path = item['path']
        image = _image(path, spec.suffix)
        secure_image = ''
        if platform.secure_chip and spec.secure_default:
            secure_path = root / spec.secure_default
            if secure_chip == 'secure':
                image = _image(str(secure_path), secure_path.suffix)
            elif secure_chip == 'auto' and secure_path.is_file() and secure_path.stat().st_size:
                secure_image = str(secure_path)
        for partition in spec.partitions:
            add(f'flash-{partition}', f'烧写{spec.label} → {partition}', fb + ('flash', partition, image), image, secure_image)
        if spec.id == 'gpt':
            add('resize-gpt', '调整 GPT 分区表', fb + ('oem', 'gpt_resize'))
            if platform.id == 'p400':
                add('erase-env', '擦除启动环境（P400 原脚本）', fb + ('erase', 'env'))
    if reboot:
        add('reboot', '烧写完成后重启', fb + ('reboot',))
    result_options = {'prepare': prepare, 'eraseEnv': erase_env, 'reboot': reboot, 'dataMode': data_mode}
    if platform.secure_chip:
        result_options['secureChip'] = secure_chip
    return {
        'simulation': True, 'toolDir': str(root), 'platform': platform.id, 'platformLabel': platform.label,
        'selected': selected, 'options': result_options,
        'steps': [asdict(step) for step in steps],
        'notice': '仅模拟：不会调用下载工具或 fastboot，不会写入、擦除或重启设备。',
    }


class SimulationExecutor:
    """假执行器：记录参数，可注入失败和延迟，不创建任何设备子进程。"""
    simulation = True

    def __init__(self, delay=0.08, fail_step=''):
        self.delay = delay
        self.fail_step = fail_step
        self.calls = []

    def execute(self, step, cancel_event, output):
        self.calls.append(copy.deepcopy(step))
        if cancel_event.wait(self.delay):
            return False
        if step['id'] == self.fail_step:
            raise RuntimeError('模拟故障：该步骤失败')
        output('模拟执行完成；设备未操作')
        return True


class RealExecutor:
    """固定步骤直接调用工具，连续读取 stdout/stderr，不拼接 shell 命令。"""
    simulation = False

    def __init__(self, timeout=1800, device_timeout=60):
        self.timeout = timeout
        self.device_timeout = device_timeout
        self._serial = ''
        self._secure = None

    def prepare(self, plan):
        self._serial = ''
        self._secure = None
        # 开始前查清所有工具；冻结版复用自身的下载辅助入口，无需目标机安装 Python。
        for step in plan['steps']:
            self.command(step)

    def _secure_step(self, step):
        """安全芯片检测结果只影响本作业后续的 SPL/preboot 镜像。"""
        if self._secure and step.get('secureImage') and step.get('image'):
            argv = tuple(step['secureImage'] if str(arg) == step['image'] else arg for arg in step['argv'])
            return {**step, 'argv': argv, 'image': step['secureImage']}
        return step

    def fastboot(self, requested):
        if requested == 'fastboot':
            bundled = Path(getattr(sys, '_MEIPASS', '')) / 'adb/fastboot.exe'
            if getattr(sys, 'frozen', False) and bundled.is_file():
                return str(bundled)
        resolved = shutil.which(requested)
        if not resolved:
            raise ValueError('未找到 fastboot.exe，请安装 platform-tools 或指定程序路径')
        return resolved

    def command(self, step):
        argv = list(step['argv'])
        if step['id'].startswith('prepare-'):
            script = argv[1]
            if argv[0] == 'python':
                if getattr(sys, 'frozen', False):
                    return [sys.executable, '--db-download-host', script, *argv[2:]]
                return [sys.executable, '-B', '-u', str(Path(__file__).with_name('db_download_host.py')), script, *argv[2:]]
            python = shutil.which(argv[0])
            if not python:
                raise ValueError('未找到指定的 Python 解释器')
            return [python, '-B', '-u', str(Path(__file__).with_name('db_download_host.py')), script, *argv[2:]]
        argv[0] = self.fastboot(argv[0])
        return argv

    @staticmethod
    def _environment(step_id):
        env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}
        if getattr(sys, 'frozen', False) and step_id == 'format-data':
            bundled_tools = Path(getattr(sys, '_MEIPASS', '')) / 'adb'
            env['PATH'] = str(bundled_tools) + os.pathsep + env.get('PATH', '')
            env['MKE2FS_CONFIG'] = str(bundled_tools / 'mke2fs.conf')
        return env

    def _target(self, argv, step, cancel, output):
        requested = argv[2] if len(argv) > 2 and argv[1] == '-s' else ''
        end = time.monotonic() + self.device_timeout
        output('等待 fastboot 设备…\n')
        while not cancel.is_set():
            found = subprocess.run([argv[0], 'devices'], capture_output=True, timeout=5,
                                   creationflags=CREATE_NO_WINDOW)
            if found.returncode:
                raise RuntimeError('fastboot devices 失败：' + found.stderr.decode('utf-8', 'replace'))
            devices = [line.split()[0] for line in found.stdout.decode('utf-8', 'replace').splitlines() if line.strip()]
            if requested and requested in devices:
                self._serial = requested
                break
            if not requested and len(devices) == 1:
                self._serial = devices[0]
                break
            if not requested and len(devices) > 1:
                raise ValueError('发现多台 fastboot 设备，请填写目标序列号后重试')
            if time.monotonic() >= end:
                raise RuntimeError('等待目标 fastboot 设备超时，请检查 USB 连接和驱动')
            cancel.wait(.25)
        if not cancel.is_set():
            output('目标 fastboot 设备：' + self._serial + '\n')

    def execute(self, step, cancel_event, output):
        from windows_executor import ProcessTree
        step = self._secure_step(step)
        argv = self.command(step)
        download = step['id'].startswith('prepare-')
        if not download:
            if not self._serial:
                self._target(argv, step, cancel_event, output)
            if len(argv) < 2 or argv[1] != '-s':
                argv[1:1] = ['-s', self._serial]
        if cancel_event.is_set():
            return False
        output('$ ' + subprocess.list2cmdline(argv) + '\n')
        env = self._environment(step['id'])
        # 独立进程防止 USB 原生库异常带走主程序；进程树在执行前建立。
        proc = subprocess.Popen(argv, cwd=step['cwd'], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=CREATE_NO_WINDOW, bufsize=0)
        tree = None
        reader = None
        complete = threading.Event()
        failures = []
        marker = []
        script_errors = []
        secure_marker = []
        def drain():
            decoder = codecs.getincrementaldecoder('utf-8')('replace')
            tail = ''
            try:
                while True:
                    data = os.read(proc.stdout.fileno(), 1024)
                    if not data:
                        break
                    text = decoder.decode(data)
                    tail = (tail + text)[-2048:]
                    if 'PC: END OF THIS USB LOAD!' in tail:
                        marker.append(True)
                    if 'Exceptions Happens' in tail:
                        script_errors.append(True)
                    if 'secure-soc' in tail.casefold():
                        secure_marker.append(True)
                    output(text, data)
                final = decoder.decode(b'', final=True)
                if final:
                    output(final)
            except Exception as exc:
                failures.append(exc)
            finally:
                complete.set()
        try:
            tree = ProcessTree(proc)
            reader = threading.Thread(target=drain, name='db-flash-output', daemon=True)
            reader.start()
            end = time.monotonic() + self.timeout
            while proc.poll() is None:
                if cancel_event.wait(.05):
                    tree.kill()
                    break
                if failures:
                    raise RuntimeError('读取工具输出失败：' + str(failures[0]))
                if time.monotonic() >= end:
                    raise RuntimeError('执行超时，已停止当前工具')
            proc.wait(timeout=5)
            complete.wait(3)
            if failures:
                raise RuntimeError('读取工具输出失败：' + str(failures[0]))
            if cancel_event.is_set():
                return False
            output(f'\n[退出码 {proc.returncode}]\n')
            if proc.returncode:
                raise RuntimeError(f'{step["label"]}失败，退出码 {proc.returncode}')
            if step['id'] == 'secure-check':
                # 与 flashall.bat 一致：输出含 secure-soc 即视为安全芯片。
                self._secure = bool(secure_marker)
                output('安全芯片：' + ('是，后续使用签名/加密镜像\n' if self._secure else '否，后续使用普通镜像\n'))
            # 原下载脚本有捕获异常却不返回非零的路径，必须校验实际完成标记。
            if download and (not marker or script_errors):
                raise RuntimeError('下载脚本未报告 USB LOAD 完成，已停止后续烧写')
            return True
        finally:
            if tree:
                tree.kill()
            elif proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            if reader:
                reader.join(timeout=3)
            proc.stdout.close()
            if tree:
                tree.close()


class DBFlashService:
    """后台作业独立线程执行；进度可回调广播，也可按 jobId 查询。"""
    def __init__(self, executor=None, progress=None):
        self.executor = executor if executor is not None else RealExecutor()
        if not isinstance(self.executor, (SimulationExecutor, RealExecutor)):
            raise ValueError('无效的烧写执行器')
        self.progress = progress or (lambda _event: None)
        self._lock = threading.RLock()
        self._job = None
        self._cancel = threading.Event()
        self._thread = None
        self._closed = False

    def close(self):
        """应用退出时取消并回收作业线程；不得从广播线程阻塞等待自己。"""
        with self._lock:
            self._closed = True
            self._cancel.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _event(self, kind, **data):
        with self._lock:
            event = {'type': 'db-flash-progress', 'jobId': self._job['jobId'],
                     'sequence': self._job['sequence'] + 1, 'kind': kind, 'simulation': self.executor.simulation, **data}
            self._job['sequence'] = event['sequence']
            self._job['events'].append(event)
            self._job['events'] = self._job['events'][-300:]
            if kind in ('succeeded', 'failed', 'cancelled'):
                self._job['state'] = kind
            elif kind == 'step':
                self._job['completed'] = data.get('completed', self._job['completed'])
            snapshot = copy.deepcopy(event)
        try:
            self.progress(snapshot)
        except Exception:
            # 广播失败不能改变已经完成的作业，也不能导致重试设备操作。
            pass

    def status(self, job_id=''):
        with self._lock:
            if job_id and (self._job is None or job_id != self._job['jobId']):
                raise ValueError('烧写作业不存在')
            return copy.deepcopy(self._job)

    def start(self, arguments):
        plan = self.plan(arguments)
        with self._lock:
            if self._closed:
                raise ValueError('烧写服务已关闭')
            if self._job and self._job['state'] == 'running':
                raise ValueError('已有烧写作业正在执行')
            if not self.executor.simulation:
                self.executor.prepare(plan)
            self._cancel = threading.Event()
            self._job = {'jobId': uuid.uuid4().hex, 'state': 'running', 'sequence': 0,
                         'completed': 0, 'total': len(plan['steps']), 'events': [], 'plan': plan, 'simulation': self.executor.simulation}
            self._thread = threading.Thread(target=self._run, args=(plan,), name='db-flash-job', daemon=True)
            result = copy.deepcopy(self._job)
            self._thread.start()
            return result

    def plan(self, arguments):
        plan = build_plan(arguments)
        plan['simulation'] = self.executor.simulation
        if not self.executor.simulation:
            plan['notice'] = '真实烧写：按当前勾选写入设备；擦除与重启以当前选项为准。'
            for step in plan['steps']:
                step['displayArgv'] = self.executor.command(step)
        return plan

    def _run(self, plan):
        try:
            self._event('started', text=plan['notice'], completed=0, total=len(plan['steps']))
            for index, step in enumerate(plan['steps']):
                if self._cancel.is_set():
                    self._event('cancelled', text='已取消，后续步骤未执行；已写入的内容不会恢复')
                    return
                self._event('step-start', step=step, completed=index, total=len(plan['steps']), text=step['label'])
                def output(text, data=None):
                    payload = {'text': text}
                    if data is not None:
                        payload['data'] = base64.b64encode(data).decode('ascii')
                    self._event('output', **payload)
                ok = self.executor.execute(step, self._cancel, output)
                if not ok or self._cancel.is_set():
                    self._event('cancelled', text='已取消，后续步骤未执行；已写入的内容不会恢复')
                    return
                self._event('step', completed=index + 1, total=len(plan['steps']), text=step['label'] + ('：模拟通过' if self.executor.simulation else '：完成'))
            self._event('succeeded', completed=len(plan['steps']), total=len(plan['steps']), text='模拟全部完成；未进行硬件验证' if self.executor.simulation else '全部所选烧写步骤完成')
        except Exception as exc:
            self._event('failed', text=str(exc))

    def call(self, action, arguments=None):
        try:
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ValueError('参数必须为对象')
            if action == 'catalog':
                data = catalog(arguments.get('toolDir', ''), arguments.get('files', []), arguments.get('platform', ''))
                data['simulation'] = self.executor.simulation
            elif action == 'plan':
                data = self.plan(arguments)
            elif action == 'start':
                data = self.start(arguments)
            elif action == 'status':
                data = self.status(arguments.get('jobId', ''))
            elif action == 'cancel':
                with self._lock:
                    job_id = arguments.get('jobId')
                    if not job_id or not self._job or job_id != self._job['jobId'] or self._job['state'] != 'running':
                        raise ValueError('没有匹配的运行中烧写作业')
                    self._cancel.set()
                    data = copy.deepcopy(self._job)
            else:
                raise ValueError('未知的烧写操作')
            return {'success': True, 'data': data, 'output': ''}
        except (ValueError, OSError) as exc:
            return {'success': False, 'output': str(exc)}
