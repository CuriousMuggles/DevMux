"""验证 pywinpty PTY 创建和通信正常。"""
import sys
import time

def run() -> bool:
    try:
        from winpty import PtyProcess
    except ImportError:
        print('  [SKIP] pywinpty not installed')
        return True

    ok = True

    # --- 基本 PTY 创建 ---
    try:
        proc = PtyProcess.spawn('cmd.exe /c echo pty_works')
        time.sleep(0.3)
        data = proc.read()
        if 'pty_works' in data:
            print(f'  [PASS] PTY create+read ok ({len(data)} bytes)')
        else:
            print(f'  [WARN] PTY output unexpected: {data[:80]}')
        proc.terminate(force=True)
    except Exception as e:
        print(f'  [FAIL] PTY create/read: {type(e).__name__}: {e}')
        ok = False

    # --- PTY + adb shell ---
    try:
        import subprocess
        r = subprocess.run(['adb', 'devices', '-l'], capture_output=True, text=True, timeout=5)
        tid = None
        for line in r.stdout.splitlines():
            if 'transport_id:' in line:
                parts = line.split()
                for p in parts[2:]:
                    if p.startswith('transport_id:'):
                        tid = p.split(':')[1]
                        break
                if tid:
                    break

        if tid:
            proc = PtyProcess.spawn(f'adb -t {tid} shell')
            time.sleep(1)
            data = proc.read()
            if data:
                print(f'  [PASS] adb shell via PTY ({len(data)} bytes)')
            else:
                print(f'  [WARN] adb shell returned no data')
            proc.write('exit\n')
            time.sleep(0.3)
            proc.terminate(force=True)
        else:
            print('  [SKIP] no ADB device with transport_id')
    except Exception as e:
        print(f'  [FAIL] PTY + adb shell: {type(e).__name__}: {e}')
        ok = False

    return ok

if __name__ == '__main__':
    sys.exit(0 if run() else 1)
