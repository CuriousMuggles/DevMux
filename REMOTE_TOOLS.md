# 远程扩展工具：接口与前端模板

首个实现为继电器控制。主控和从机运行同一新版，经既有共享目录中转固定工具动作；工具目录和硬件调用在从机执行，不经远程 PowerShell。

## 一致的界面

工具继续从「工具」菜单进入，共用非模态浮动窗口、按钮和主题样式，按固定顺序组织：

| 区域 | 内容和规则 |
| --- | --- |
| 标题 | 工具名称、本机 / 远程从机控制 |
| 目标电脑 | 本机和已配对从机、刷新连接；显示机器及部署，重名时补充实例后缀 |
| 执行位置和权限 | 始终显示执行电脑；本机可修改本工具远程权限，从机视图只读取权限 |
| 工具配置 | 路径属于所选电脑；本机可浏览，远程输入从机路径，保存发生在执行端 |
| 工具操作 | 工具自己的控制区，继电器为查找、设备选择、逐路和全部控制 |
| 状态和诊断 | 后台只更新状态，不禁用全部按钮；手动操作防重复；诊断默认折叠 |

`renderer/remote-tool-context.js` 的 `RemoteToolContext` 管理目标列表、权限检查、执行位置、请求目标绑定和旧响应隔离。`ComputerTargets.render` 同时用于 PowerShell 和继电器，保持名称、角色及离线提示一致。

切换目标清空原目标快照，旧响应按 generation 丢弃。离线、解除连接或超时后禁止操作并保留原选择，不自动改为本机。切换目标、关闭面板和逻辑关闭设备不改变继电器触点。后台读取期间点击的动作等待读取结束后执行一次；控制超时只能显示结果未确认，不自动重发。

应用内浮窗使用 `renderer/tool-floating-window.js` 与 CSS，通过 `new ToolFloatingWindow(layer, dialog, header, options)` 注册。`show()` / `hide()` 管理显示，标题栏拖动、边缘缩放、点击置顶、可见范围约束及本次运行的位置记忆由共用组件负责。工具自行维护后台任务；关闭窗口不能自动停止烧写或改变继电器触点。浮窗不设置遮罩、焦点陷阱或全局 busy，允许多个工具同时操作。确认和权限对话框仍保留各自模态规则。

## 新工具接入模板

后端在 `ExtensionTools` 注册固定工具标识，目前为 `relay → USBRelayService` 和 `db-flash → DBFlashExtension`。服务需实现 `NAME`、`ACTIONS` 白名单、执行锁 `_lock`、本机持久化 `remote_allowed`（默认 false）、`set_remote_permission(bool)` 和 `call(action, arguments)`，返回 `{success, output, data}`。配置与状态由每个工具自行管理。

前端创建 `RemoteToolContext(container, toolId, onChange, name)`。onChange 清空旧模型，ready 后读取新目标配置；操作调用 context.call，处理 stale 响应；自身的按钮和设备模型由工具面板维护。后台刷新不能重建按钮，连接或设备逻辑状态改变时才重绘整个操作区。

浏览器发送 `extension-tool`，携带 `tool`、明确的 `target`、`action`、对象 `arguments`。targets 返回电脑列表，其余动作必须指定实例键，本机为 computer:local。入口验证本机应用页面 Origin。公共 capabilities 返回版本、动作和执行端权限；permission 只能本机调用。继电器权限与 PowerShell 分开，主控不能开启从机权限。

主控以 nodeId 选路，通过 extension_tools.remote_call 投递 extension-tool 类别，复用请求/回执、角色仲裁、配对、连接代次、心跳和跨部署路径。在从机通过 _authorize 后，取得执行锁时再检查实例、代次、权限与 TTL，阻止等待期间已失效的请求。每条请求只投递一次，复用既有回执去重。

当前是短时同步动作：中转等待和 TTL 为 40 秒，页面等待 60 秒；原生读取最长 20 秒、控制最长 10 秒。超时不能保证撤回已开始的物理动作。DB 烧写作为长任务，以任务 ID 和 requestId 异步读取状态，不增加同步等待来占用界面。旧版从机明确拒绝新类别，提示升级；不增加 manifest requiredFeatures，既有 ADB 中转保持兼容。

## 现场使用

1. 从机运行新版，在继电器面板选择本机，配置工具目录，勾选「允许已连接主控远程使用本机继电器」。
2. 主控确认跨机中转连接，等待连接验证成功。
3. 主控进入继电器面板，选择远程电脑，核对从机路径，查找并打开设备，再控制通道。

## 验证

tests/test_extension_tools.py 验证真实共享目录中转的从机配置、枚举、控制、权限撤销、旧代次和解除连接。Edge/WS 测试验证权限提示、远程路径、控制、从机本地关闭后的状态同步及离线保留目标。硬件使用模拟器；实体继电器、两台电脑 SMB 延迟和 Bitdefender 稳定性仍需双端现场验证。

源码启动前运行 `python -B relay_probe_build.py` 编译固定 x86 辅助程序，需要 Windows .NET Framework 4 的 csc.exe；发布构建和继电器回归会自动编译。最终 EXE 已内置辅助程序，目标电脑不需要编译器。

## DB 远程烧写及全工具浮窗（2026-10-13）

DB 使用独立执行端权限，默认 false。主控/从机都需升级，从机本机在 DB 面板开启权限后，主控选择从机。目录和镜像属于从机；只读 browse 列表批选返回路径，不传输镜像。固定 catalog/plan/start/status/cancel 通过已有中转，配置保存到执行端；远程请求禁止自定义 pythonPath/fastbootPath。

`DBFlashExtension` 管理本机配置和请求接受记录。`ExtensionTools` 在锁内重查准入，授权回调返回主控 nodeId，DB 任务绑定该身份；其他主控不能停止该任务，本机可以管理。开始请求先记录 requestId 再启动异步服务，重复标识不重复烧写，重启后的历史请求返回 interrupted。撤回未知标识时留下拒绝记录，阻止迟到开始请求执行。网络超时不自动重发；只有不确定的结果保留查询状态，明确拒绝与超时区分。

status 的 after 游标每次返回最多 32 个进度/输出事件，历史缓冲有界并注明截断。主控按序消费；本机进度推送不会混入远程窗口。已接纳的固定计划在失联或撤销准入后继续，不自动取消，执行端本机可以停止。

配置与专用工具继续使用共用浮窗，包括跨机配置、ADB 诊断、DB 和远程文件列表、继电器及命令行选择。TASK-12更新：ADB、PowerShell和Serial默认进入主终端区，共用布局；拖动标题栏可脱离成悬浮窗，↙按钮返回。迁移保留原会话/DOM和尺寸回传；动态浮窗卸载监听器。手动关闭后主界面刷新才恢复。

验证：test_db_flash_remote 的 4 项覆盖真实共享目录任务、从机权限、身份、重复/重启防重烧及迟到撤回。test_agent_ui 的 Edge/WS 覆盖从机文件批选、识别、开始、日志、撤销权限和所有工具浮窗；保留命令行和继电器回归。冻结双 EXE 仅验证远程接口，不烧写设备；硬件执行为假工具，实际双电脑和实机结果待用户验收。

## Agent 公共接入（2026-10-14）

MCP/CLI 通过 extensions_list、extension_capabilities、extension_call 接入同一注册表和选路，不另起硬件执行服务。发现接口包含动作说明及参数 schema；后续注册的扩展自动可发现并通过固定动作调用。远端实际能力与权限须向执行端查询，不能以本机注册列表当作远端升级完成。

Agent 始终传完整 computer 目标标识；本机调用 ExtensionTools.call，远程调用 remote_call，沿用执行锁、独立权限、身份和连接代次。远程 permission 仍禁止。DB start 的 requestId 在客户端先生成，直接认证 WS 调用须明确提供；响应丢失或中转超时保留查询标识和 operationUncertain，不自动重发。日志分页、任务身份、防重烧和专用取消由原 DB 扩展执行。

接入示例与参数见 AGENT_USAGE.md；tests/test_agent_extensions.py 验证实际认证 WS/MCP/CLI、模拟继电器及 DB 任务、真实共享目录选路、权限拒绝、任务归属、防重烧、取消和丢失回执恢复。模拟验证不等于现场硬件验收。

## Serial 远程串口（2026-10-14 / REQ-28）

`serial → SerialService` 复用统一扩展与共享目录选路；从机独立权限默认关闭。持续接入采用 `call_for_context(action, arguments, owner, caller)`，浏览器入口注入gui来源，认证Agent始终agent来源；远程句柄绑定nodeId+connectionId。`close_owner`在撤销/换代/停止时释放该连接接入，本机接入保留。每COM只有一个连接；GUI输入优先，旧generation不得写入；base64字节/游标支持HEX；XMODEM双向任务在目标端执行、传输独占，32KiB块上传/下载，进度通过原串口流元数据转发。完整接口与配置见 [串口使用说明](docs/使用说明/串口使用说明.md)，设计及稳定验收编号见 [串口交互接入设计](docs/需求与设计/串口交互接入设计.md)。

远程串口打开时协商`serialStreamProtocol=1`，后续读写/控制/关闭使用按接入隔离的持续流，避免逐次request/results扫描。输入带递增序号与回执，逐帧执行端准入，绑定启动身份和连接代次；旧从机兼容原协议并提示升级。输出JSON帧保留base64原始字节及绝对游标，GUI和Agent共用此路径。
