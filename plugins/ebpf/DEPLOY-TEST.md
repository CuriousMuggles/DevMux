# eBPF 0.0.1 开发验证包

本包沿用现有插件平台及 device-adb 宿主接口，无需为本次插件更新重新构建主程序。不是正式发布版本。

## 上手流程

1. 主控插件管理安装 `ebpf-0.0.1.dmp`，启用并重新打开插件窗口。
2. 从机连接 ARM64 Linux 设备，允许 ADB shell 和 push；设备需要 root。
3. 上传带函数符号和帧指针的测试负载到 `/tmp/devmux-ebpf-workload`。
4. 设备终端运行 `chmod 700 /tmp/devmux-ebpf-workload; /tmp/devmux-ebpf-workload 600`，记录 workload_pid。
5. 插件选择设备，输入 PID，保持默认“一键诊断”，时长设为 3 秒，点击开始采集。
6. 查看延迟直方图和“慢调用与关联栈”：报告自动展示 TID、系统调用号、耗时和双栈；不需要手工导入 JSON。
7. 内核及用户函数名自动解析；无法解析的帧保留地址。报告说明解析失败原因，可导出 JSON 保留结果。

## 实现与边界

- 联合采集采用同一 TID 的入口与出口关联，阈值为 2 ms；只保留最近一条慢调用。其余符合阈值的调用计入 droppedSlowCalls，不能解释为完整慢调用列表或最慢排名。
- 延迟分桶 32 个，每桶 65536 ns，末桶为 ≥2031616 ns。末桶无法提供精确最大值或精确尾部分位数。
- 质量指标包括 completedCalls、unmatchedExits、mapUpdateFailures、stackFailures、droppedSlowCalls；入口栈和调用耗时不能单独证明根因。
- 用户符号在设备端直接解析，不再逐块传输整个 ELF。辅助程序通过映射设备号/inode 匹配真实文件，核对读取前后文件元数据及映射，支持主程序、PIE 与共享库。
- 采集前后仅比较进程起始标识和栈地址对应映射，无关堆/匿名映射变化不会使结果失效。
- 不强制 ELF 含 Build ID；真实映射文件保留函数符号即可解析。被剥离符号、地址不在函数范围、文件不可读等情况显示具体原因。
- 文件上限 128 MiB；辅助解析命令限时 5 秒。原有 ELF 分块后端接口只保留兼容用途，GUI 自动解析不使用它。
- 主程序、map_files、进程 root 下的库路径均在设备上读取；后备路径必须与映射的设备号及 inode 一致。设备不支持 map_files 时，仍可验证并读取共享库原文件。无需新增 pull 权限或更新主程序。
- 界面只保留一个一键诊断入口；取消旧三阶段流水线，避免三次独立采集被误认为同一调用。延迟、内核栈及用户栈单项采集仍保留。
- 联合与单项栈采集均使用设备端辅助程序解析符号。采集器不再同步全量读取 kallsyms；符号解析失败不会丢弃已采到的数据。
- 内核 kallsyms 读取限时 4 秒，整个辅助命令限时 5 秒；保留已取得可信边界的符号，剩余帧报告具体原因，不扩展最后一个符号到任意地址。
- 联合采集器通过 BPF_OBJ_GET_INFO_BY_FD 记录自身 JIT 地址范围，用于标识探针帧，显示为 devmux_sys_enter [采集探针]；不把它们伪装成业务函数。内核不允许获取 JIT 范围时保留原始地址。
- 当前窗口保存最近 30 次成功记录；导出结果包含关联慢调用、解析结果及质量指标。关闭窗口后不持久化历史。

## 验证入口

先打包：`python plugin_pack.py plugins/ebpf dist/ebpf-0.0.1.dmp`。

后端与权限：`python -m unittest tests.test_ebpf_plugin tests.test_plugin_devices`。

GUI 专项：`python -c "from tests.test_plugin_frontends import run_for; run_for('ebpf')"`。

本次未合入主线；正式发布前仍须构建目标版本并按 `python tests/run_all.py` 全量验证。
