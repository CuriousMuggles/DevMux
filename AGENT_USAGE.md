# DevMux Agent 接入与电脑 PowerShell

人工操作和发布版接入请先读 [使用说明](使用说明.md)。发布包只需EXE和这一份说明，Agent可按其第五部分/附录生成仅依赖Windows Python与websockets的独立CLI/MCP脚本，无需本项目源码。本文后续源码入口命令用于已有源码的开发者，不作为发布版接入前提；原agent_cli.py/agent_mcp.py仍有项目传递导入，不能单独复制。

本地 Agent 通过 JSON CLI 或 MCP stdio 连接已运行的 DevMux。两者共用应用的设备列表、ADB shell 和中转连接，不启动第二套 ADB 轮询。电脑 PowerShell 和设备 ADB 权限独立。

## 启动和权限

1. 启动已交付且支持 Agent 的 DevMux；跨机操作时两端均运行支持对应能力的版本。源码运行可用 `python server.py`；接入已运行 EXE 时不要另起后端。
2. 按现有流程配置共享目录，由主控确认从机连接。
3. 在执行电脑的「窗口 → PowerShell…」直接控制「本机允许执行 PowerShell」。新配置及未设置该字段的旧配置默认开启，已有明确关闭的配置保留。修改后自动保存并立即生效，无需启用或重启中转；关闭将结束本机 PowerShell 交互会话。
4. 「窗口 → PowerShell…」选择本机主控或已连接的从机，点击「打开终端」在主终端区域新增对应的 PowerShell 面板和标签，沿用 ADB shell 的配色、字体、网格/单屏布局、最小化/最大化、复制粘贴及可拖拽命令框；可连续输入、保留变量/目录、使用键盘 Ctrl+C；关闭终端面板会停止对应 shell，不再显示 Ctrl+C 按钮。权限开关控制当前电脑；远程电脑权限必须在那台电脑的「PowerShell」中设置。界面不展示命令记录或 Agent 任务，Agent 通过下面的 MCP/CLI 操作接口接入。

PowerShell 按运行 DevMux 的 Windows 账户权限执行，不自动提权。允许通用脚本意味着该账户的常规文件/程序权限；它不是受限工具沙箱。共享目录须由受信任账户写入，既有连接确认不能替代对不可信共享目录写入者的密码学认证。

## CLI

在项目目录运行（Python 3.10+，依赖沿用项目的 websockets）：

```powershell
python -B agent_cli.py instances
python -B agent_cli.py tools
python -B agent_cli.py targets_list
```

存在多个运行实例时，加 `--instance <instances 返回的 instanceId>`。发现信息默认位于 `%LOCALAPPDATA%\ADBManager\runtime\`，按实例分文件。该历史目录名为兼容旧版 Agent 配置而保留。输出不包含令牌。需要覆盖时，两端客户端/应用使用同一个 `ADB_AGENT_RUNTIME_DIR`，目录应限当前用户访问。目录不可写会记录明确错误，桌面后端仍可启动。

为避免 PowerShell 多层引号，参数优先使用 UTF-8 JSON 文件。例如 `request.json`：

```json
{"target":"computer:local","script":"hostname; Get-Location","timeout":30}
```

```powershell
python -B agent_cli.py windows_exec --args-file request.json
```

`windows_exec` 立即返回 jobId，使用 `job_status` / `job_output` 查询；不要用调用返回快慢推断命令完成。远程电脑目标为 `computer:<nodeId>`，设备为 `device:<服务器返回的完整 key>`，均从 `targets_list` 读取。

```json
{"target":"computer:local","jobId":"返回的任务ID"}
```

```powershell
python -B agent_cli.py job_status --args-file request.json
python -B agent_cli.py job_output --args-file request.json
```

`job_output` 可指定 stream=stdout/stderr、cursor 和 limit，使用 nextCursor 继续分页。原始字节为 base64 data，text 去 ANSI；截断/裁剪通过 droppedBytes 报告。

## MCP

注册 stdio 服务器：命令 `python`，参数为 `-B` 和本项目的 `agent_mcp.py` **绝对路径**，多实例时再追加 `--instance` 与实例 ID。不需要额外 MCP SDK。入口示意：

```json
{
  "command": "python",
  "args": ["-B", "D:\\workspace\\ai-work\\adb-manager\\agent_mcp.py"]
}
```

MCP 支持 initialize、ping、tools/list、tools/call，stdio 为 UTF-8 逐行 JSON-RPC。协议依据 MCP 2025-03-26，并兼容 2024-11-05；输出不混普通日志。各宿主的注册位置按其配置方式设置，本次不自动修改宿主配置。

## 工具

| 工具 | 说明 |
|---|---|
| targets_list | 当前实例可见的电脑和设备；多目标不猜测 |
| target_status | 电脑能力、是否允许、shell 类型及 filesRoot |
| windows_exec | 独立 PowerShell 任务；script、cwd、shell、timeout、可选 jobId |
| job_status / job_output / job_cancel / jobs_list | 查询、分页读取、取消及任务列表 |
| adb_exec | 指定设备执行 argv，禁止自带前置设备或 ADB 服务端选择符 |
| files_transfer | 设备或电脑 push/pull，复用校验包和路径规则 |
| session_open / read / write / resize / close | Windows 会话或已有 ADB shell 接入 |
| extensions_list | 当前版本注册的扩展、动作与参数说明，新增注册工具自动进入列表 |
| extension_capabilities | 指定电脑的扩展版本、动作、参数说明与独立权限 |
| extension_call | 指定电脑的固定扩展动作，统一复用本机/从机注册服务 |

## 继电器和 DB 烧写扩展

先调用 `extensions_list`，再通过 `targets_list` 选择完整电脑目标 ID，以 `extension_capabilities` 查询执行端是否支持该扩展和是否允许。本版注册 `serial`（串口）、`relay`（继电器）及 `db-flash`（DB烧写）；列表描述的是本版工具，旧从机可能尚未支持，须实际查询。能力结果的 `agentActions` 含各动作参数 schema，`allowed` 表示该目标是否允许调用。

```powershell
python -B agent_cli.py extensions_list
python -B agent_cli.py extension_capabilities --args-file request.json
python -B agent_cli.py extension_call --args-file request.json
```

能力查询的 `request.json`：

```json
{"target":"computer:从机nodeId","tool":"relay"}
```

继电器支持 config、save、scan、open、close、status、set、diagnose。使用执行电脑的 toolDir 保存目录，scan 后按返回 serial 打开设备；set 的 operation 为 open/close，channel 为实际通道号，255 表示全部通道。逻辑 close 保持触点不变，控制结果不确定时读取 status，不自动重发控制动作。例如：

```json
{"target":"computer:从机nodeId","tool":"relay","action":"set","arguments":{"channel":1,"operation":"open"}}
```

DB 支持 config、save、browse、catalog、plan、start、status、cancel。目录、镜像和浏览路径属于执行电脑，browse 不传输文件。先 catalog/plan 核对固定计划，再 start；开始返回任务标识不代表烧写完成，使用 status 查询实际状态及日志。例如：

```json
{"target":"computer:从机nodeId","tool":"db-flash","action":"start","arguments":{"toolDir":"D:\\firmware\\DB","selected":["system"],"options":{"prepare":false},"requestId":"0123456789abcdef0123456789abcdef"}}
```

CLI/MCP 在未提供 start.requestId 时会在发请求之前生成32位十六进制标识；明确提供的标识不会被替换。直接调用认证 WS 必须提供它。正常响应在外层 data.requestId 返回；响应丢失时失败结果的 data.requestId 仍可查询。重复开始须复用原标识，不能生成新标识盲目重烧。查询参数如下，after 为事件游标、每页最多32事件：

```json
{"target":"computer:从机nodeId","tool":"db-flash","action":"status","arguments":{"requestId":"0123456789abcdef0123456789abcdef","after":0}}
```

cancel 使用 jobId 或 requestId，沿用 DB 任务停止策略；撤回未确认的 requestId 会阻止迟到开始。请求成功不等于任务已停止，继续查询实际状态。开始和状态结果不确定时通过 operationUncertain 标识，不自动重试。

扩展权限独立于 PowerShell。permission 仅能以 computer:local 在执行电脑本机设置 arguments.allowed；主控不能替从机开启权限。远程 DB 禁止自定义 pythonPath/fastbootPath，任务沿用已验证主控 nodeId 归属；其他主控不能停止它。既有配对、撤销、连接代次、锁内授权及 DB 防重烧均由执行端执行，Agent 不绕过这些检查。

## 持续交互

对电脑 `session_open` 创建 PowerShell PTY，同一会话保留变量/cwd；对设备则接入已有终端，不再创建第二个设备 shell。session_write 原样发送字符串，回车建议 `\r`，Ctrl+C 为 `\u0003`。read 用字节游标读取合并输出，不提供通用命令退出码，输出静默不代表命令完成。

PowerShell 会话不加载 profile，不修改 ExecutionPolicy；使用对应版本的标准用户/系统/安装模块目录，避免宿主注入另一版本 PSReadLine。自定义模块可按完整路径 Import-Module。

设备会话期间 GUI 只读，可用「接管」使旧 Agent 接入失效。从机本地接管沿用原有中转控制权；连接/会话代次变化后必须重新接入。设备 session_close 只释放 Agent 输入权，不关闭原设备 shell。电脑 session_close 则关闭创建的 PowerShell。

Agent 会话 ID 由常驻实例持有，可跨 CLI 调用；应用重启后失效，不自动重放输入。关闭不再使用的会话，避免占满上限。

## 文件传输

设备目标：push 的 paths 为主控本机来源，destination 为设备路径；pull 的 paths 为设备路径，destination 为主控保存路径。沿用从机 push/pull 白名单。

电脑目标：PowerShell 权限启用后可传输，远端路径是 `target_status.filesRoot` 内的**相对路径**，不允许绝对路径、上级路径或链接。push 的 destination 是暂存子目录；pull 的 paths 是暂存内文件/目录。主控本机路径可为绝对路径。

```json
{"target":"computer:从机nodeId","direction":"push","paths":["D:\\firmware\\image.bin"],"destination":"firmware"}
```

上传后在从机用 `filesRoot\firmware\image.bin` 调用工具。包限制沿用现有 10 GiB/100000 项；包校验成功不等于固化成功。传输超时保留可能仍在读取的包，不支持断点/取消。目标落盘失败可能部分完成，不承诺事务回滚。

## 任务恢复和限制

任务持久记录在配置旁 `windows-jobs-*`，包含所有者、状态、账户、脚本哈希及输出文件。重启时原运行任务标为 interrupted，不自动重跑；已完成日志通过接口读取，界面不展示 Agent 任务。

同一调用者重复 jobId 返回原任务；连接超时返回结果未知，应查询原 jobId/jobs_list，不能盲目重新启动。CLI 自动生成 jobId，也允许调用者提供固定的 32 位小写十六进制 ID。

单次任务最多 8 个并行、每路输出最多 32 MiB、历史最多 512 条；满额时明确拒绝新任务，停用执行服务后可归档任务目录。交互最多 8 个会话，有界历史 1 MiB；设备 Agent 接入历史 128 KiB，裁剪明确报告。单次任务默认 30 秒，最长 86400 秒；交互会话不使用命令超时，连续 30 分钟无读取/输入后释放。终端窗口打开时持续读取，不受该闲置限制。

普通 PowerShell 任务取消、超时、应用退出通过 Windows Job Object 回收子进程树。DB 烧写使用独立扩展的 start/status/cancel 和任务策略，不使用普通 job_cancel；不能将任务开始、停止请求或工具进程返回视为设备固化验收完成。

## 验收边界

`python -B tests/test_agent.py` 覆盖真实 PowerShell/PTY/子进程、双节点中转/权限/去重、电脑文件、认证接口、CLI 和 MCP；`tests/test_agent_ui.py` 保留原有 Edge 回归并增加 PowerShell 终端交互、Ctrl+C 和权限入口实测。运行全套 `python -B tests/run_all.py`。

`python -B tests/test_agent_extensions.py` 覆盖实际认证 WS/MCP/CLI、模拟继电器、模拟 DB 任务、本机/真实中转选路、执行端权限、身份、防重烧、取消、丢失回执追踪及后续注册工具发现。扩展通过模拟执行器验证，不操作实体继电器或板卡。

同机双节点验证不能替代真实双机 SMB、真实 ADB 设备、继电器和固化现场验收；未开展的硬件项不能标记通过。

协议参考：[MCP stdio](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)、[MCP tools](https://modelcontextprotocol.io/specification/2025-03-26/server/tools)。

## 远程交互响应

双端更新后，PowerShell 的创建和关闭走控制队列，持续输入、输出和尺寸调整使用固定的追加流文件，不再逐次等待共享目录请求与回执。输入返回 queued 表示已经送入流，命令结果以实际回显为准。从机持续检查权限、主控身份、在线状态及连接代次；权限关闭、解除或节点停止时关闭旧流。20 秒读取保活和 30 分钟闲置释放保持无人使用时的回收。旧从机没有协商持续流时自动沿用原控制协议。页面输入后以 10 毫秒间隔观察输出，闲置时降频。SMB 或同步盘自身延迟仍会影响字符流；日志中的延迟来自同机双实例，不能替代真实双电脑共享目录的测量。

## Serial 串口接入（2026-10-14）

通过 `extension_capabilities / extension_call` 使用 `tool=serial`；支持 ports/open/read/write/control/status/close，默认921600，共用字节输出，GUI输入优先。read 返回原始base64和字节游标，write支持text/hex/base64。完整参数、控制代次、requestId防重复和示例见 [串口使用说明](docs/使用说明/串口使用说明.md)。XMODEM支持发送/接收，使用xmodem-start/upload/run/status/download/cancel；参数与有限重试、独占和填充边界见同一说明。
