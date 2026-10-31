#!/usr/bin/env python3
"""
DevMux - 自动化验证入口
=============================
主线合入或正式发布前的完整验证入口：python tests/run_all.py
开发分支先构建新包，再按改动范围选择相关测试组。
"""
import sys
import time
from pathlib import Path

# Windows 控制台可能使用 GBK；UI 断言结果会包含设备路径和终端回显中的任意字符。
# 诊断输出无法编码时转义单个字符，不能因此把已通过的功能测试判成失败。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(errors='backslashreplace')

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Test modules: (name, module_path)
TESTS = [
    ('Plugin Development SDK', 'tests.test_plugin_dev'),
    ('Plugin SDK Browser', 'tests.test_plugin_sdk_ui'),
    ('Plugin Store', 'tests.test_plugin_store'),
    ('Plugin Runtime', 'tests.test_plugin_runtime'),
    ('Remote Plugins', 'tests.test_remote_plugins'),
    ('Plugin UI', 'tests.test_plugin_ui'),
    ('Plugin Tool Frontends', 'tests.test_plugin_frontends'),
    ('Frozen Plugin Host', 'tests.test_plugin_frozen'),
    ('Mijia Security', 'tests.test_mijia_service'),
    ('Extension Tool Selection', 'tests.test_extension_tool_selection'),
    ('Syntax Check',    'tests.test_syntax'),
    ('Module Imports',  'tests.test_imports'),
    ('Logging',         'tests.test_logging'),
    ('Logging Persistent Paths', 'tests.test_logging_paths'),
    ('ADB Devices',     'tests.test_devices'),
    ('ADB Recovery',    'tests.test_adb_recovery'),
    ('ADB Diagnostics', 'tests.test_diagnostics'),
    ('ADB Diagnostics WS', 'tests.test_diagnostics_server'),
    ('Resource Lifecycle', 'tests.test_resource_lifecycle'),
    ('PTY Disconnect', 'tests.test_pty_disconnect'),
    ('PTY Host Isolation', 'tests.test_pty_host'),
    ('Crash Diagnostics', 'tests.test_crash_diagnostics'),
    ('pywinpty PTY',    'tests.test_pywinpty'),
    ('Server (HTTP+WS)','tests.test_server'),
    ('DB Flash Simulation', 'tests.test_db_flash'),
    ('DB Flash p300-udp', 'tests.test_db_flash_p300'),
    ('DB Flash Real Process', 'tests.test_db_flash_real'),
    ('DB Flash Remote', 'tests.test_db_flash_remote'),
    ('DB Flash WS', 'tests.test_db_flash_server'),
    ('USB Relay Local', 'tests.test_usb_relay'),
    ('Extension Tools Relay', 'tests.test_extension_tools'),
    ('HTTP Port Exclusivity', 'tests.test_http_port_conflict'),
    ('Terminal Recording', 'tests.test_terminal_recording'),
    ('Serial Recording', 'tests.test_serial_recording'),
    ('Serial Connection Config', 'tests.test_serial_config'),
    ('Paste Pacing',    'tests.test_paste_pacing'),
    ('Relay Transport', 'tests.test_relay_transport'),
    ('Relay Message Journal', 'tests.test_relay_journal'),
    ('Relay Journal Toast', 'tests.test_relay_journal_toast'),
    ('Legacy Archive Read Compatibility', 'tests.test_relay_archive'),
    ('Relay Carrier',   'tests.test_relay_carrier'),
    ('Relay Liveness',  'tests.test_relay_liveness'),
    ('Relay SMB Read',  'tests.test_relay_file_io'),
    ('Relay Authorization Clock', 'tests.test_relay_authorization_clock'),
    ('Relay Stability', 'tests.test_relay_stability'),
    ('Relay Startup', 'tests.test_relay_startup'),
    ('Relay Interactive Latency', 'tests.test_relay_interactive_latency'),
    ('Relay Node',      'tests.test_relay_node'),
    ('Relay Pairing',   'tests.test_relay_pairing'),
    ('Relay Pairing List', 'tests.test_relay_pairing_list'),
    ('Relay Cross Deployment', 'tests.test_relay_cross_deployment'),
    ('Relay Deployment Switch', 'tests.test_relay_deployment_switch'),
    ('Relay Connection Confirmation', 'tests.test_relay_connection_confirmation'),
    ('Relay Multi Instance', 'tests.test_relay_multi_instance'),
    ('Relay Unified Config', 'tests.test_unified_config'),
    ('Relay App',       'tests.test_relay_app'),
    ('Relay Session Lifecycle', 'tests.test_relay_lifecycle'),
    ('Relay Clear Cache', 'tests.test_relay_clear_cache'),
    ('Relay Clear Cache UI', 'tests.test_relay_clear_cache_ui'),
    ('Shared Terminal', 'tests.test_shared_terminal'),
    ('PowerShell Discovery', 'tests.test_powershell_discovery'),
    ('Agent PowerShell / MCP', 'tests.test_agent'),
    ('Agent Extension Tools', 'tests.test_agent_extensions'),
    ('Serial Terminal', 'tests.test_serial_terminal'),
    ('Serial XMODEM Protocol', 'tests.test_serial_xmodem'),
    ('Serial Enhancements / Remote', 'tests.test_serial_enhancements'),
    ('Serial Read Latency', 'tests.test_serial_latency'),
    ('Serial Character Pacing', 'tests.test_serial_pacing'),
    ('Serial Remote / Agent', 'tests.test_serial_remote'),
    ('Command Column Editor UI', 'tests.test_command_column_ui'),
    ('Serial Window UI', 'tests.test_serial_ui'),
    ('Serial Command Input', 'tests.test_serial_command_input'),
    ('Serial Log / Toolbar UI', 'tests.test_serial_log_toolbar_ui'),
    ('Serial Detached Shortcuts', 'tests.test_serial_shortcuts_detached'),
    ('Desktop Terminal Ownership', 'tests.test_desktop_terminals'),
    ('Native Terminal Window', 'tests.test_native_terminal_host'),
    ('Native Plugin Windows', 'tests.test_native_plugin_host'),
    ('Relay File Transfer', 'tests.test_relay_transfer'),
    ('Windows Unicode ADB Pull', 'tests.test_adb_pull_unicode'),
    ('Relay File Latency', 'tests.test_relay_file_latency'),
    ('Relay Row Layout', 'tests.test_relay_row_layout'),
    ('Local Device Mask', 'tests.test_local_device_mask'),
    ('Inline Edit',     'tests.test_inline_edit'),
    ('Packaging',       'tests.test_packaging'),
    ('Bundled ADB Runtime', 'tests.test_adb_runtime'),
    ('UI E2E',          'tests.test_agent_ui'),
    ('Git Status',      'tests.test_git'),
    ('Drag Resize',     'tests.test_drag_resize'),
]


def run_test(name: str, module: str) -> bool:
    try:
        mod = __import__(module, fromlist=['run'])
        if not hasattr(mod, 'run'):
            print(f'  [SKIP] {module} has no run()')
            return True
        return mod.run()
    except Exception as e:
        print(f'  [FAIL] {name} crashed: {type(e).__name__}: {e}')
        return False


def main():
    print('=' * 56)
    print('  DevMux - Automated Verification')
    print('=' * 56)
    print()

    passed = 0
    failed = 0
    skipped = 0
    results = []

    for name, module in TESTS:
        print(f'[{name}]')
        t0 = time.time()
        try:
            ok = run_test(name, module)
            elapsed = time.time() - t0
        except KeyboardInterrupt:
            print('  [INTERRUPTED]')
            failed += 1
            results.append((name, False, time.time() - t0))
            break
        elapsed = time.time() - t0

        if ok is None:
            skipped += 1
            status = 'SKIP'
        elif ok:
            passed += 1
            status = 'PASS'
        else:
            failed += 1
            status = 'FAIL'
        results.append((name, ok, elapsed))
        print(f'  -> {status} ({elapsed:.1f}s)')
        print()

    # Summary
    print('=' * 56)
    print(f'  Results:  {passed} passed,  {failed} failed,  {skipped} skipped')
    print(f'  Total:    {passed + failed + skipped} tests  ({sum(r[2] for r in results):.1f}s)')
    print('=' * 56)

    if failed > 0:
        print('\nFailed tests:')
        for name, ok, _ in results:
            if not ok:
                print(f'  - {name}')

    return failed == 0


if __name__ == '__main__':
    ok = main()
    sys.exit(0 if ok else 1)
