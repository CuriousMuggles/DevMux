"""构建固定的 x86 继电器读取程序；只在明确构建时调用编译器。"""
import os
from pathlib import Path
import subprocess


def build_relay_probe(root=None):
    root = Path(root or Path(__file__).resolve().parent)
    source = root / 'native' / 'RelayProbe.cs'
    output = root / 'build' / 'relay-probe' / 'RelayProbe.exe'
    compiler = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
    if not compiler.is_file():
        raise RuntimeError('构建需要 .NET Framework 4 的 x86 C# 编译器')
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x86', '/optimize+',
                             '/debug-', f'/out:{output}', str(source)], capture_output=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=60)
    if result.returncode:
        raise RuntimeError('继电器辅助程序编译失败：'+(result.stdout+result.stderr).decode('utf-8', errors='replace'))
    return output


if __name__ == '__main__':
    print(build_relay_probe())
