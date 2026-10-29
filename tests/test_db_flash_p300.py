"""p300-udp 平台识别、固定烧写顺序与只读回归；假文件不连接任何设备。"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db_flash import P300_UDP_SPECS, RealExecutor, build_plan, catalog, detect_platform, recognize

FILES = [
    'flashall.bat', 'flash_cfg.ini', 'scripts/download_image.py',
    'images/spl/spl_im_burn_signed.img', 'images/spl/spl_im_signed.img',
    'images/spl/spl_im_signed.img.pro.enc', 'images/gpt/gpt.bin',
    'images/u-boot/u-boot.bin', 'images/u-boot/uboot_env.img',
    'images/lpddr_fw/lpddr5_fw.bin', 'images/preboot.itb', 'images/preboot_sign.itb',
    'images/boot.itb', 'images/rootfs.img', 'images/userdata.img',
]


class P300Tests(unittest.TestCase):
    def setUp(self):
        runtime = ROOT / '.agent-runtime/db-flash'
        runtime.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='test-p300-', dir=runtime)
        self.root = (Path(self.temporary.name) / 'p300 udp 固化包').resolve()
        for name in FILES:
            file = self.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            if name == 'flashall.bat':
                content = (b'python scripts/download_image.py -i images/spl/spl_im_burn_signed.img '
                           b'-m bootrom_download -d 0 -p 2 -v 0x696d -u 0x7633\r\n'
                           b'fastboot getvar is-secure-chip\r\n'
                           b'fastboot flash env images/u-boot/uboot_env.img\r\n')
            elif name == 'flash_cfg.ini':
                content = b'[UsbInfo]\r\nUSBID_VID=0x1234,0x696D,0x3902\r\n'
            else:
                content = b'fake image'
            file.write_bytes(content)
        self.before = self.snapshot()
        self.args = {'toolDir': str(self.root)}

    def tearDown(self):
        self.assertEqual(self.before, self.snapshot(), 'p300-udp 固化包必须保持只读')
        self.temporary.cleanup()

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def plan(self, **arguments):
        return build_plan({**self.args, **arguments})

    def path(self, relative):
        return str((self.root / relative).resolve())

    def test_catalog_detects_platform(self):
        model = catalog(str(self.root))
        self.assertEqual(model['platform'], 'p300-udp')
        self.assertTrue(model['platformDetected'])
        self.assertTrue(model['toolValid'])
        self.assertEqual(len(model['items']), 8)
        checked = [item['id'] for item in model['items'] if item['checked']]
        self.assertEqual(checked, ['spl', 'gpt', 'env', 'ddr', 'preboot', 'boot', 'rootfs', 'data'])
        self.assertEqual(model['options']['dataMode'], 'format')
        self.assertEqual(model['options']['secureChip'], 'auto')
        self.assertFalse(model['options']['eraseEnv'])
        self.assertEqual([mode['value'] for mode in model['options']['dataModes']],
                         ['format', 'flash', 'erase'])

    def test_generic_flash_cfg_does_not_misclassify_legacy_platform(self):
        root = Path(self.temporary.name) / 'generic-config-platform'
        root.mkdir()
        (root / 'flashall.bat').write_text('fastboot flash system images/system.img', encoding='utf-8')
        (root / 'flash_cfg.ini').write_text(
            '[UsbInfo]\nUSBID_VID=0x1234,0x696D,0x3902\nUSBID_PID_ROM=0x7633,0x7734\n'
            '[BootromInfo]\nspl_path=images/spl/spl_im_burn_signed.img\n'
            '[FlashInfo]\ncmd="flash",part="system",param="images/system.img"', encoding='utf-8')
        self.assertEqual(detect_platform(root)[0], 'legacy')

    def test_flash_all_sh_identifies_p500_label_with_legacy_plan(self):
        root = Path(self.temporary.name) / 'p500-package'
        root.mkdir()
        (root / 'flash_all.sh').write_text('#!/bin/sh\nfastboot flash system images/system.img', encoding='utf-8')
        platform, reason = detect_platform(root)
        self.assertEqual(platform, 'p500')
        self.assertIn('P500', reason)
        model = catalog(str(root))
        self.assertEqual(model['platformLabel'], 'p500')
        self.assertTrue(model['options']['eraseEnv'])
        self.assertEqual(model['options']['dataMode'], 'erase')

    def test_p300_marker_in_config_is_recognized(self):
        root = Path(self.temporary.name) / 'p300-config-platform'
        root.mkdir()
        (root / 'flashall.bat').write_text('fastboot flash system images/system.img', encoding='utf-8')
        (root / 'flash_cfg.ini').write_text(
            'cmd="flash",part="env",param="images/u-boot/uboot_env.img"\n'
            'cmd="flash",part="ddr",param="images/lpddr_fw/lpddr5_fw.bin"', encoding='utf-8')
        self.assertEqual(detect_platform(root)[0], 'p300-udp')

    def test_default_plan_follows_flashall_script(self):
        steps = self.plan()['steps']
        self.assertEqual([step['id'] for step in steps], [
            'prepare-spl', 'prepare-uboot', 'verify-mode', 'secure-check',
            'flash-mmc0boot0', 'flash-mmc0boot1', 'flash-gpt', 'resize-gpt',
            'flash-env', 'flash-ddr', 'flash-preboot', 'flash-boot', 'flash-rootfs',
            'format-data', 'reboot'])
        by_id = {step['id']: step for step in steps}
        self.assertEqual(by_id['prepare-spl']['argv'], (
            'python', self.path('scripts/download_image.py'), '-i', self.path('images/spl/spl_im_burn_signed.img'),
            '-m', 'bootrom_download', '-d', '0', '-p', '2', '-v', '0x696d', '-u', '0x7633'))
        self.assertEqual(by_id['prepare-uboot']['argv'][-6:], ('-d', '0', '-v', '0x3902', '-u', '0x7634'))
        self.assertEqual(by_id['secure-check']['argv'], ('fastboot', 'getvar', 'is-secure-chip'))
        self.assertEqual(by_id['flash-env']['argv'][-3:], ('flash', 'env', self.path('images/u-boot/uboot_env.img')))
        self.assertEqual(by_id['flash-ddr']['argv'][-3:], ('flash', 'ddr', self.path('images/lpddr_fw/lpddr5_fw.bin')))
        self.assertNotIn('flash-system', by_id)
        self.assertEqual(by_id['flash-mmc0boot0']['image'], self.path('images/spl/spl_im_signed.img'))
        self.assertEqual(by_id['flash-mmc0boot0']['secureImage'],
                         self.path('images/spl/spl_im_signed.img.pro.enc'))
        self.assertEqual(by_id['flash-preboot']['secureImage'], self.path('images/preboot_sign.itb'))
        self.assertEqual(by_id['format-data']['argv'], ('fastboot', 'format', 'data'))

    def test_secure_and_data_modes(self):
        secure = self.plan(options={'prepare': False, 'reboot': False,
                                    'secureChip': 'secure', 'dataMode': 'format'})
        secure_ids = [step['id'] for step in secure['steps']]
        self.assertNotIn('secure-check', secure_ids)
        secure_by_id = {step['id']: step for step in secure['steps']}
        self.assertEqual(secure_by_id['flash-mmc0boot0']['argv'][-1],
                         self.path('images/spl/spl_im_signed.img.pro.enc'))
        self.assertEqual(secure_by_id['flash-preboot']['argv'][-1], self.path('images/preboot_sign.itb'))

        normal = self.plan(options={'prepare': False, 'reboot': False,
                                    'secureChip': 'non-secure', 'dataMode': 'format'})
        normal_by_id = {step['id']: step for step in normal['steps']}
        self.assertNotIn('secure-check', normal_by_id)
        self.assertEqual(normal_by_id['flash-mmc0boot0']['argv'][-1], self.path('images/spl/spl_im_signed.img'))

        erase = self.plan(options={'prepare': False, 'reboot': False, 'dataMode': 'erase'})
        self.assertIn('erase-data', [step['id'] for step in erase['steps']])
        self.assertNotIn('format-data', [step['id'] for step in erase['steps']])

        flash = self.plan(options={'prepare': False, 'reboot': False, 'dataMode': 'flash'})
        flash_by_id = {step['id']: step for step in flash['steps']}
        self.assertEqual(flash_by_id['flash-data']['argv'][-1], self.path('images/userdata.img'))

        with self.assertRaises(ValueError):
            self.plan(options={'dataMode': 'unknown'})
        with self.assertRaises(ValueError):
            self.plan(options={'secureChip': 'maybe'})

    def test_p300_has_no_system_item_and_external_data(self):
        external = Path(self.temporary.name) / '外置 userdata.img'
        external.write_bytes(b'external fixture')
        model = catalog(str(self.root))
        self.assertNotIn('system', [item['id'] for item in model['items']])
        with self.assertRaises(ValueError):
            self.plan(selected=['system'])
        plan = self.plan(selected=['data'], overrides={'data': str(external.resolve())},
                         options={'prepare': False, 'reboot': False, 'dataMode': 'flash'})
        self.assertEqual([step['id'] for step in plan['steps']],
                         ['verify-mode', 'flash-data'])
        self.assertEqual(plan['steps'][-1]['argv'][-1], str(external.resolve()))
        self.assertFalse((self.root / external.name).exists())

    def test_platform_override_and_unknown(self):
        model = catalog(str(self.root), platform='legacy')
        self.assertEqual(model['platform'], 'legacy')
        self.assertFalse(model['platformDetected'])
        self.assertFalse(model['toolValid'])
        with self.assertRaises(ValueError):
            catalog(str(self.root), platform='unknown')
        with self.assertRaises(ValueError):
            build_plan({'toolDir': str(self.root), 'platform': 'unknown'})

    def test_p300_recognition(self):
        self.assertEqual(recognize('uboot_env.img', P300_UDP_SPECS)['matches'], ['env'])
        self.assertEqual(recognize('lpddr5_fw.bin', P300_UDP_SPECS)['matches'], ['ddr'])
        self.assertEqual(recognize('preboot_sign.itb', P300_UDP_SPECS)['matches'], ['preboot'])
        # 安全镜像只从固化包默认路径取用，不允许把 .pro.enc 当成普通 SPL 手动指定。
        self.assertEqual(recognize('spl_im_signed.img.pro.enc', P300_UDP_SPECS)['state'], 'invalid')

    def test_real_executor_secure_image_switch(self):
        executor = RealExecutor()
        step = {'id': 'flash-mmc0boot0', 'label': '', 'cwd': str(self.root),
                'argv': ('fastboot', 'flash', 'mmc0boot0', self.path('images/spl/spl_im_signed.img')),
                'image': self.path('images/spl/spl_im_signed.img'),
                'secureImage': self.path('images/spl/spl_im_signed.img.pro.enc')}
        self.assertEqual(executor._secure_step(step)['argv'][-1], self.path('images/spl/spl_im_signed.img'))
        executor._secure = True
        switched = executor._secure_step(step)
        self.assertEqual(switched['argv'][-1], self.path('images/spl/spl_im_signed.img.pro.enc'))
        self.assertEqual(switched['image'], self.path('images/spl/spl_im_signed.img.pro.enc'))


def run():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(P300Tests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
