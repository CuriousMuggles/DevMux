# Linux eBPF 调试插件

当前版本 0.0.1，基于本项目插件平台、DevMuxPlugin UI SDK 和 device-adb 宿主接口。

支持联合诊断（延迟分布、同一慢调用双栈和质量计数）、自动显示慢调用报告、设备端直接读取映射 ELF 的用户符号解析（无需传输 ELF 文件），内核符号与探针帧标识，以及单项采集（已移除重复的一键流水线）。

安装、操作、测试与限制见 [DEPLOY-TEST.md](DEPLOY-TEST.md)。native/session.c 是联合采集器；bin/session 为已在 ARM64 Linux 6.18.32 设备验证的静态程序。native/validation 仅保留早期验证原型。

开发分支尚未合入主线；不是正式发布版本。最新测试不替代正式发布前的完整测试组。
