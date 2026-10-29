"""验证当前项目处于正确的 git 状态。"""
import sys
import subprocess
from pathlib import Path

def run() -> bool:
    root = Path(__file__).resolve().parent.parent
    ok = True

    # --- 普通仓库使用 .git 目录，worktree 使用指向公共 Git 目录的 .git 文件 ---
    git_dir = root / '.git'
    if git_dir.exists():
        print(f'  [PASS] .git metadata exists')
    else:
        print('  [FAIL] not a git repository')
        return False

    # --- git status 可执行 ---
    try:
        r = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, timeout=10, cwd=root)
        untracked = [l for l in r.stdout.splitlines() if l.startswith('??')]
        modified = [l for l in r.stdout.splitlines() if l.startswith(' M') or l.startswith('M ')]
        if untracked:
            for f in untracked:
                print(f'  [INFO] untracked: {f[3:]}')
        if modified:
            for f in modified:
                print(f'  [INFO] modified: {f[3:]}')
        print(f'  [PASS] git status ok ({len(untracked)} untracked, {len(modified)} modified)')
    except Exception as e:
        print(f'  [FAIL] git status: {e}')
        ok = False

    return ok

if __name__ == '__main__':
    sys.exit(0 if run() else 1)
