"""验证 ADB 设备检测功能是否正常。"""
import sys
import subprocess

def run() -> bool:
    ok = True

    # --- adb 可执行文件是否存在 ---
    try:
        r = subprocess.run(['adb', '--version'], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            ver = r.stdout.splitlines()[0] if r.stdout else 'unknown'
            print(f'  [PASS] adb available: {ver.strip()}')
        else:
            print(f'  [FAIL] adb --version returned {r.returncode}')
            ok = False
    except FileNotFoundError:
        print('  [FAIL] adb not found in PATH')
        ok = False
    except subprocess.TimeoutExpired:
        print('  [FAIL] adb --version timed out')
        ok = False

    # --- adb devices -l 可解析 ---
    try:
        r = subprocess.run(['adb', 'devices', '-l'], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            lines = [l.strip() for l in r.stdout.splitlines() if l.strip() and not l.startswith('List')]
            if len(lines) >= 1 and 'device' in lines[0]:
                serial = lines[0].split()[0]
                has_tid = 'transport_id:' in lines[0]
                print(f'  [PASS] device detected: {serial}')
                if has_tid:
                    print(f'  [PASS] transport_id present')
                else:
                    print(f'  [WARN] no transport_id (will not use -t flag)')
            else:
                print(f'  [WARN] no device connected ({len(lines)} lines)')
        else:
            print(f'  [FAIL] adb devices -l returned {r.returncode}')
            ok = False
    except subprocess.TimeoutExpired:
        print('  [FAIL] adb devices -l timed out')
        ok = False
    except Exception as e:
        print(f'  [FAIL] adb devices -l error: {e}')
        ok = False

    return ok

if __name__ == '__main__':
    sys.exit(0 if run() else 1)
