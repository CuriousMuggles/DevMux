# 第三方组件许可核查

本清单是 2026-10-28 本机环境的初步盘点，不是发布包完整物料清单。实际构建版本和冻结内容可能不同；发布前必须核对实际产物、传递依赖、原生文件及各自许可通知。自有代码授权不改变第三方文件的许可证。

## 本机 Python 环境（元数据）

| 组件 | 已安装版本 | 元数据中的许可 |
|---|---|---|
| mijiaAPI | 3.2.0 | GPL-3.0-or-later |
| websockets | 16.1.1 | BSD-3-Clause |
| pywebview | 6.2.1 | BSD 3-Clause License Copyright (c) 2014-2017, Roman Sirokov All rights reserved. Redistribution and use in source and bi… |
| pywinpty | 3.0.5 | MIT License |
| pyserial | 3.5 | BSD |
| pyinstaller | 6.21.0 | GPLv2-or-later with a special exception which allows to use PyInstaller to build and distribute non-free programs (inclu… |
| qrcode | 8.2 | BSD |
| pycryptodome | 3.23.0 | BSD, Public Domain |
| tzlocal | 5.4.3 | MIT |
| requests | 2.32.5 | Apache-2.0 |
| pyusb | 1.3.1 | BSD License |
| crcmod | 1.7 | MIT |
| pythonnet | 3.1.0 | MIT |
| clr-loader | 0.3.1 | 未在元数据中明确 |
| bottle | 0.13.4 | MIT |
| proxy-tools | 0.1.0 | MIT |
| typing-extensions | 4.15.0 | PSF-2.0 |
| certifi | 2026.2.25 | MPL-2.0 |
| urllib3 | 2.6.3 | MIT |
| idna | 3.11 | BSD-3-Clause |
| charset-normalizer | 3.4.6 | MIT |
| tzdata | 2025.3 | Apache-2.0 |
| pillow | 12.2.0 | MIT-CMU |

许可元数据只是线索，不能替代对实际发行文件的许可证、版权声明和传递依赖的核验。PyInstaller 的打包例外须按其发行版本条款核查。

## 仓库与本机二进制

- `renderer/vendor/`：xterm.js 及 addon；上游采用 MIT，需确认所包含文件的准确版本并提供对应许可和版权声明。
- `vendor/usb-relay/`：上游来源及 DLL 摘要见 [来源记录](vendor/usb-relay/PROVENANCE.md)。部分源码声明 GPL v2、GPL v3 或商业许可；尚未证明当前 DLL 与所保存源码对应，须核对完整对应源码及构建方式。
- Android platform-tools：构建从 `ADB_BUNDLE_DIR` 或本机 PATH 获取 adb、fastboot、mke2fs 及 DLL；实际发行版本、各文件条款和通知须逐项核对。本机 `C:/tool/platform-tools/NOTICE.txt` 仅是核查线索，构建不保证使用该路径。
- pywinpty 携带的 OpenConsole.exe、winpty-agent.exe，Python 运行时、PyInstaller bootloader 与其他冻结原生依赖：需以实际产物逐项核查来源、版本、许可证及通知。

## 发布前待完成

1. 固定构建依赖和 platform-tools 版本，检查 PyInstaller 产物清单与内置原生文件。
2. 收集各组件实际版本的许可证、版权声明及必要通知，随源码和二进制发行物提供。
3. 核实 USB 继电器 DLL 的完整对应源码与构建方式，满足适用 GPL 条款；不要把现有快照视作已完成证明。
4. 评估组合发行的适用条件，提供要求的对应源码及构建脚本；确认项目所有贡献代码的授权。
5. 对发布内容进行独立合规复核；本清单不构成法律意见，也不表示发布条件已满足。
