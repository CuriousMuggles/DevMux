# DevMux

**DevMux 是一款 Windows 多设备调试与控制工作台**，将 ADB 终端、串口、文件传输和常用硬件工具集中在一个桌面界面中。

它的两个核心特点是：

- **跨电脑控制**：通过共享目录连接多台电脑，在主控端查看和操作从机上的设备。
- **AI 接入**：通过认证的 MCP 或 CLI 接口，让 AI Agent 在授权范围内使用设备和工具能力。

## 功能

- **多设备 ADB 终端**：自动发现设备，为每台设备提供独立终端，并支持快捷指令和 ADB 诊断。
- **跨电脑中转**：一台主控电脑可连接多台从机，管理从机上的 ADB 设备；支持配对、连接状态提示和远程终端。
- **文件传输**：在电脑和设备之间上传文件或文件夹、批量下载文件；支持通过中转操作远程设备。
- **AI Agent 接入**：通过 MCP 或 CLI 调用统一接口，支持设备和部分扩展工具操作。配置与调用方式见 [Agent 使用说明](AGENT_USAGE.md)。
- **串口与系统终端**：打开本机或远程 PowerShell、串口终端，支持文本和 HEX 交互、快捷指令及 XMODEM 传输。
- **扩展工具**：提供 USB 继电器控制、DB 固化烧写，以及受支持的米家设备管理。

## 快速开始

### 运行打包版

在 Windows 上运行 `DevMux.exe`。打包版内置 Python、ADB/fastboot 和前端资源；通过 USB 连接设备时，仍需安装相应设备驱动。

### 从源码运行

需要 Python 3.10+、ADB，以及 Windows 的 WebView2 Runtime 和 ConPTY。

```powershell
pip install websockets pywebview pywinpty pyserial
python server.py
```

源码运行时，请将 ADB 加入 `PATH`，或通过 `ADB_PATH` 指定可执行文件路径。米家功能的额外依赖见 `requirements-mijia.txt`。

## 跨电脑使用

在需要连接设备的电脑上配置并启用从机，在主控电脑上选择并确认从机。双方通过可访问的共享目录交换控制请求和终端数据；设备操作由从机执行，并受从机权限控制。

配置与操作步骤见 [双端交互流程](docs/使用说明/双端交互流程.md)。

## AI 接入

DevMux 提供经过认证的 MCP 和 CLI 入口，可供 AI Agent 发现设备并调用支持的操作。当前 Agent 入口需要匹配的客户端源码和 Python 环境。

配置示例、首次调用和权限说明见 [AGENT_USAGE.md](AGENT_USAGE.md)。

## 构建与验证

```powershell
# 构建正式版
python -m pip install pyinstaller
python -m PyInstaller build.spec --clean --noconfirm

# 运行自动化验证
python -B tests\run_all.py
```

正式版产物为 `dist\DevMux.exe`。DB 烧写仍需在执行端配置相应固化包；具体要求见使用说明。

## 文档

- [使用说明](使用说明.md)：启动、设备连接和界面操作
- [Agent 使用说明](AGENT_USAGE.md)：MCP 和 CLI 接入
- [双端交互流程](docs/使用说明/双端交互流程.md)：跨电脑配置与连接
- [串口使用说明](docs/使用说明/串口使用说明.md)：串口终端操作

## 许可

DevMux 自有代码以 [GPL-3.0-or-later](LICENSE) 授权。第三方组件使用各自的许可证，详见 [第三方许可清单](THIRD_PARTY_LICENSES.md)。公开发布前仍需完成该清单中列出的核查。
