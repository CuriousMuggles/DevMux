# 官方插件开发（接口版本 1）

## 五分钟开始

在 DevMux 仓库根目录使用项目 Python 环境，无需安装额外前端框架：

```powershell
python plugin_dev.py create plugins/my-tool --id my-tool --name "我的工具"
python plugin_dev.py check plugins/my-tool
python plugin_dev.py serve plugins/my-tool
python plugin_dev.py pack plugins/my-tool dist/my-tool.dmp
```

`serve` 输出包含随机访问令牌的本机 URL，复制到浏览器即可调试。修改前端源码后点击“刷新界面”；修改 Python 后点击“重新加载后端”。这会重新校验源码、重建临时包并重启调试进程；有活动任务时 `can_stop()` 可阻止重载。配置和日志写到临时环境，不读取应用里已安装插件的数据，退出后清理。开发预览不会模拟远程连接或原生文件选择器，这两类能力须在实际应用中验证。本机工具上下文和后端调用可直接调试；调试仍会执行真实业务代码，设备操作由开发者自行选择。

生成的工程包含 `manifest.json`、`main.py`、`index.html`、`app.js`、README 和供 AI 开发遵循的 AGENTS.md。参考 `plugins/example` 可运行示例，三个官方插件也使用同一套公开 SDK。

## 前端 SDK 与界面

源页面引用公共资源和插件内资源，打包器会自动内联为单个页面：

```html
<link rel="stylesheet" href="devmux:ui.css">
<script src="devmux:sdk.js"></script>
<script src="app.js"></script>
```

打包不支持外部脚本、外部样式、CSS `@import`、外部媒体和越界路径。图片使用 data URL；复杂前端可先用自己的构建工具输出普通脚本，再交给插件打包器。`check` 校验清单、工厂函数、Python 语法和前端引用，不执行插件后端。

```javascript
const {ui, call} = DevMuxPlugin;
const page = ui.mount({title:'我的工具', description:'示例功能'});
const form = ui.form([{name:'text', label:'内容', type:'text', required:true}]);
const output = ui.log({limit:100});
page.body.append(form, ui.button('执行', async () => {
  if (!form.reportValidity()) return;
  const result = await call('echo', form.values());
  output.appendLine(result.text);
}, {variant:'primary'}), output);
```

| 接口 | 用途 |
|---|---|
| `call(action, arguments, {timeout})` | 调用自己的后端，返回业务数据；失败抛出 `PluginError` |
| `hostCall(type, payload, {timeout})` | 调用 `pick-file`、`pick-directory` 或自己的 `extension-tool` |
| `request(type, payload, timeout)` | 保留原始结果结构的底层适配；type 为 `plugin` 或上述宿主接口 |
| `context` | 只读的窗口初始 `{target, remote}`，远程窗口由宿主锁定目标 |
| `RemoteContext(container, tool, onChange, name)` | 公共目标栏、连接状态与执行端权限提示；使用 `new` 创建 |
| `close()` | 请求关闭当前独立窗口，释放本页资源，不停止插件后端 |
| `listen(element, event, callback, options)` | 注册随页面释放的监听，返回取消函数 |
| `interval(callback, milliseconds)` | 注册随页面释放的定时器，返回定时器 ID |
| `onDispose(callback)` / `dispose()` | 注册清理 / 幂等释放，回调按注册逆序执行 |

`call` 和 `hostCall` 对 `{success:true,data:...}` 返回 `data`，普通业务对象原样返回；`success:false` 抛错。`request` 始终保留结果结构，超时等通信失败返回 `{success:false, output, code}`。请求 ID、响应来源检查、超时和待处理请求释放由 SDK 管理。超时结果未确认，不自动重试硬件操作。

| 公共 UI 接口 | 返回与行为 |
|---|---|
| `ui.mount({title,description,content})` | 标准页面骨架，返回 `body`、`message`、`element` 等；每个工具使用一个主页面 |
| `ui.button(label, callback, {variant,disabled})` | 自动处理执行中禁用及错误提示的按钮 |
| `ui.form(schema, values)` | 表单 DOM；支持 text/password/number/boolean/select，`values()` 读取，`control(name)` 获取控件 |
| `ui.status(text, kind)` | 状态 DOM，`update(text,kind)` 更新；kind 为 info/success/warning/error |
| `ui.table(columns, rows)` | 安全文本表格；列为 `{key,label}`，`update(rows)` 更新 |
| `ui.log({limit})` | 限定行数的日志 DOM；`appendLine(text)` 和 `clear()` |
| `ui.dialog({title,content})` | 标准模态框；`show()`、`hide()`、`destroy()` |
| `ui.registerView(name,factory)` / `renderView(name,container,props)` | 注册和渲染插件内自定义视图；卸载页面时自动撤销注册 |

自定义业务布局可以使用普通 DOM，但按钮、表单、表格等通用外观应复用 SDK。主题集中在 `plugin_sdk/ui.css`，通过 `--bg-dark`、`--bg-panel`、`--text`、`--text-dim`、`--border`、`--accent`、`--error` 等变量扩展。不要复制主程序 CSS，也不要为主页面创建浮动窗口。宿主负责工具菜单注册、插件管理卡片和原生独立窗口，内部业务切页可使用视图注册接口。

## 后端资源与权限

```python
from plugin_sdk import Plugin

class Tool(Plugin):
    def __init__(self, root):
        super().__init__(root)
        # self.data_dir: 当前插件的独立持久数据目录
        # self.on_dispose(resource.close): 注册资源清理

    def call(self, action, arguments):
        if action == 'echo':
            self.logger.info('Echo invoked')
            return {'text': arguments.get('text', '')}
        raise ValueError('未知动作')

def create_plugin(root):
    return Tool(root)
```

继承 SDK 基类是可选的，原有工厂与调用契约仍有效。SDK 清理按逆序执行，一个回调失败不阻止其余清理；关闭过程幂等。日志和标准错误输出保存到 `.data/<插件ID>/runtime.log`；开发预览可直接点击“查看日志”。主程序的本机管理接口 `logs` 返回最多 64 KiB 日志文本；不通过远程目录接口暴露日志。

新插件在 `permissions` 中显式声明需要的 `pick-file`、`pick-directory`；未声明时宿主拒绝相应接口。三个官方插件已显式声明，旧包没有该字段时保留原有文件选择兼容行为。`extension-tool` 始终限制为自身工具，远程窗口只能操作原从机。需要远程能力的插件还须声明 `tool`、`actions`，实现执行端权限与 `__owned__` 任务所有者校验；仅声明清单不会自动获得远程授权。

## MCP / Agent 访问

MCP 继续使用统一入口 `extensions_list`、`extension_capabilities` 和 `extension_call`，与界面共享同一个插件进程及数据目录，无需再启动一套设备服务。

- 清单 `actions` 声明可供本机及经授权从机调用的动作；`localActions` 声明仅供执行端本机 MCP/Agent 调用的动作。可以同时声明，但 `localActions` 不会赋予远程能力。
- 插件启用时注册，停用或卸载后撤销。`extensions_list({})` 仅返回本机已启用的服务，不会虚报未安装的插件。
- `extensions_list({"target":"computer:<从机nodeId>"})` 经已验证的连接查询从机已安装的远程插件，包含启用和授权状态；主机无需安装同一插件。停用插件仍显示已安装状态，调用前须在从机启用并授权。
- `extension_capabilities` 和 `extension_call` 使用明确的 `target`、`tool`，远程调用保留连接验证、执行端授权和任务所有者校验；不会回退到本机。
- 米家声明 `localActions: ["status", "snapshot", "read-switch", "set-switch"]`，须先在本机界面登录。MCP 不开放登录、退出登录或凭据操作，也不能通过远程目标控制米家。

例如调用本机米家登录状态：`extension_call({"target":"computer:local","tool":"mijia","action":"status","arguments":{}})`。新插件无须修改 MCP 工具列表；声明动作并实现后端校验后即可动态发现。

## 打包与运行约定

在 `plugins/example` 基础上开发，使用：

```powershell
python plugin_pack.py plugins/example dist/example.dmp
```

在工具 → 插件管理中选择 `.dmp` 文件安装，然后启用、打开。无需重启。更新和卸载前先停用；插件停止后，其窗口一起关闭。

清单声明 `id`、`name`、`version`、`apiVersion: 1`、`backend`（Python 文件）及可选的 `frontend`（HTML 文件）。ID 是稳定的小写字母、数字和连字符标识。

后端导出 `create_plugin(root)`，返回提供 `call(action, arguments)` 和可选 `close()` 的对象。每个插件在独立进程内串行处理调用。结果必须可 JSON 序列化；异常返回错误。普通 print 输出转到诊断流，不能自行写入协议标准输出。冻结版插件可使用主程序已打包的 Python 模块；新增第三方依赖须先验证打包兼容性。

前端为自包含 HTML（样式、脚本内联），在可移出主程序边界的独立桌面窗口中加载。通过 `parent.postMessage({type:'devmux-plugin-call', id, action, arguments}, '*')` 调用自己的后端。父页面返回 `{type:'devmux-plugin-result', id, result, error}`。使用唯一请求 ID，并检查消息来源是 parent。页面在 sandbox iframe 中运行。

当前包通过本地文件分发，仅安装项目发布的可信包。安装成功不表示已启用；启动失败会显示错误。调用超时会结束插件进程，返回结果未确认，不自动重试。插件需要自行管理物理操作的幂等性；尚不支持在任务执行中安全强制停用，开发硬件插件前须补齐任务准入和停止协议。

继电器、DB 和米家现在均由插件包提供，基础安装包不再创建它们的内置服务。串口和跨机中转保持核心能力。本机有前端入口的已启用插件显示在工具菜单的“本机工具”分组中；已停用插件可从插件管理启用或打开。继电器和 DB 启用时注册到现有远程与 Agent 扩展接口，权限仍由执行端插件裁决；米家仅允许本机使用。

插件管理选择的电脑只影响管理对象。工具菜单另外按从机分组展示其已安装、具有前端且支持远程操作的插件；未启用或未授权保留状态提示。本机米家等入口不会随管理对象切换而消失。打开工具菜单时刷新发现状态。

从机独有的插件无需再安装到主机。开启“允许主控使用”后，主机通过已验证连接分块获取自包含 HTML，校验接口版本、大小和 SHA256，将页面缓存在插件目录的 `.frontend-cache` 中；不下载后端，不安装或启用本机插件。每次打开先向从机重新校验状态和页面摘要，连接失败、卸载、停用或撤销授权时不能使用旧缓存打开。远程窗口按“电脑 + 插件”独立管理，标题标注机器名，界面请求固定在原从机；本机与多台从机可同时打开同一插件。

清单可以声明 `window.width`、`window.height` 以及 `description`。`tool` 和 `actions` 用于声明远程/Agent 接口；对应插件须实现 config、permission 和 __owned__ 动作。插件存在后台任务时应实现 can_stop() 并返回 False，底座将拒绝停用。清单 `timeout` 可将本地调用上限配置为 1–180 秒。

插件 HTML 只应提供工具内容，不要再创建自己的浮动窗口外壳、拖动标题栏或缩放边框。系统窗口由底座统一管理，关闭视图不会停用插件或结束后台任务。浏览器运行时使用独立弹出页面，需要允许弹出窗口。
