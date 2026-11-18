# DevMux

**DevMux 是给 AI 和脚本用的、确定性的跨机设备操作层**——把 ADB、串口、PowerShell 和硬件工具收在一个窗口，再通过带认证的 MCP / CLI 交给 Agent 操作。

它不是屏幕共享，也不做 GUI 点击。每一步都是结构化调用：执行命令有 stdout 和退出码，传文件有回执，写串口带 generation 防并发串。**能验证、能回滚、能审计**。ToDesk 帮你「看」另一台电脑，DevMux 帮你「编程地」操作另一台机器上的设备。

- **多设备、多终端**：自动发现 Android 设备，每台一个终端；ADB、PowerShell、串口共用标签、网格布局和独立窗口。
- **跨机中转**：两台电脑共用一份共享目录，从机插的设备在主控直接操作，输入权可交还、可接管。
- **给 AI 用**：认证的 MCP / CLI，让 Agent 在授权范围内查目标、执行命令、传文件、调用扩展。

本软件不提供手机屏幕镜像或触摸控制。

## 能做什么

| 能力 | 要点 | 具体操作 |
|---|---|---|
| 多设备终端 | 自动发现设备；网格、单屏、拖出独立窗口；查找、录制、快捷命令 | [使用说明](/docs/用户/使用说明.md) 第三、四章 |
| 文件传输 | 电脑与设备间上传文件/文件夹、批量下载；远程设备同入口 | 同上，第四章 |
| 跨机中转 | 主控确认从机后可操作远端设备；权限与执行都在设备所在电脑 | 同上，第五章 |
| 串口 | COM 枚举、文本/HEX、日志、快捷指令、XMODEM | [使用说明](/docs/用户/使用说明.md) 第六章 |
| 电脑命令 | 本机或从机的 PowerShell 终端，保留目录和变量 | [使用说明](/docs/用户/使用说明.md) 第七章 |
| 继电器、DB 烧写、米家、eBPF | 以官方插件提供，装好并启用后才出现在工具菜单 | [使用说明](/docs/用户/使用说明.md) 第八至十一章 |
| 让 Agent 操作 | MCP / CLI：查目标、执行命令、传文件、调用扩展 | [开发者说明](/docs/用户/开发者说明.md) |

## 快速开始

1. 打开 [Releases](https://github.com/CuriousMuggles/DevMux/releases)，下载 `DevMux-版本号-windows.zip` 与 `SHA256SUMS.txt`；源码 ZIP 不含可执行程序。
2. 用 `Get-FileHash .\DevMux-版本号-windows.zip -Algorithm SHA256` 与清单核对，再解压到可写目录（例如 `D:\DevMux`）。不要在压缩包内直接运行。
3. 双击 `DevMux.exe`。Windows 10 1809 或更高、需 WebView2 Runtime；发布版内置 Python 运行时和 Android platform-tools。
4. 接上要用的目标：Android 设备（允许 USB 调试）、串口（记下 COM 号）或另一台电脑上的设备（配同一共享目录）。具体步骤见 [使用说明](/docs/用户/使用说明.md) 第二章。
5. 继电器、DB 烧写、米家、eBPF 由插件提供：在「工具 → 插件管理」安装 `plugin/` 里的 `.dmp` 并启用，工具菜单才会出现入口。

升级前退出所有旧实例并备份整个 `devmux-data/`，在原目录替换程序，保留运行数据。

## 从源码运行

需要 Python 3.10+、ADB，以及 Windows 的 WebView2 Runtime 和 ConPTY。

```powershell
pip install -r requirements.txt
python server.py
```

源码运行时把 ADB 加入 `PATH`，或用 `ADB_PATH` 指定路径；米家额外依赖见 `requirements-mijia.txt`。源码运行的运行数据（含已装插件、凭据）位于仓库根目录的 `devmux-data/`，与发布版 `DevMux.exe` 同目录的 `devmux-data/` 是两套独立数据，互不相通。

## 文档

| 文档 | 读者 | 覆盖范围 |
|---|---|---|
| [使用说明](/docs/用户/使用说明.md) | 使用者 | 安装、接入、终端与布局、文件传输、跨机中转、串口、PowerShell、插件工具、排错 |
| [开发者说明](/docs/用户/开发者说明.md) | Agent 接入方、插件开发者 | MCP / CLI、工具表、扩展与串口接口、插件 SDK、打包与发布 |
| [发布说明](/docs/用户/发布说明.md) | 使用者 | 每个版本的实际变化与升级要求 |
| [第三方许可](/docs/用户/第三方许可.md) | 分发者 | 随包组件及其许可证 |
| [来源记录](/vendor/usb-relay/来源记录.md) | 分发者 | 继电器 DLL 的上游来源与摘要 |

## 构建与发布

```powershell
python -m pip install -r requirements-build.txt
python -m pip install -r requirements-mijia.txt   # build.spec 会强制检查米家依赖
# ADB_BUNDLE_DIR 指向完整 Android platform-tools，须含 adb.exe、fastboot.exe、
# mke2fs.exe、mke2fs.conf 及 AdbWinApi.dll 等配套 DLL，缺任一 build.spec 会报错
$env:ADB_BUNDLE_DIR = "C:\tool\platform-tools"
python -m PyInstaller build.spec --clean --noconfirm
python release_bundle.py --release      # 生成 dist/releases/<版本-时间>/ 交付目录
python tests/run_all.py                 # 全量验证
```

正式版产物为 `dist\DevMux.exe`，官方插件由 `release_plugins.py` 生成到 `dist/plugin/`，交付文档同步到 `dist/`。完整的发布门槛、证据要求与验收清单在源码仓库的 `docs/internal/流程/发布流程.md`（不随发布包分发）。

## 许可

DevMux 自有代码以 [GPL-3.0-or-later](LICENSE) 授权。第三方组件使用各自许可证，详见 [第三方许可清单](/docs/用户/第三方许可.md)。
