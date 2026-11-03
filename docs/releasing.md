# DevMux 3.1.4 发布流程

开发分支先构建新包，再运行专项测试；合入主线后重新构建主线目标包，再按 `python tests/run_all.py` 顺序全量验证，全部通过后才能公开发布。失败、跳过、环境阻塞均不能算通过。每次提交前必须展示完整 message 并取得明确确认。

## 构建

使用 64 位 Windows、Python 3.10+（实际验证环境见构建证据）、WebView2 Runtime，安装 `pip install -r requirements-build.txt`。准备完整 Android platform-tools，设置 `ADB_BUNDLE_DIR` 指向含 adb.exe、fastboot.exe、mke2fs.exe、mke2fs.conf 及配套 DLL 的目录；不得只复制 adb.exe。源码运行还可通过 ADB_PATH 指定 ADB。

`python release_bundle.py` 构建候选包，同时记录未提交状态和每个源码文件摘要。候选包仅供验证。正式发布使用 `python release_bundle.py --release`，要求干净工作树、HEAD 与 `v3.1.4` tag 完整提交一致。先获准提交及合入，再创建对应 tag，执行正式构建和主线全量测试；测试不得改写待发布产物。

正式版 `build.spec` 与调试版 `build_debug.spec` 均调用 `release_plugins.py`，四个官方包在 dist/plugin/，命名为插件ID-插件版本.dmp；调试版用 `python -m PyInstaller build_debug.spec --clean --noconfirm`，不混入正式 ZIP。

## 交付清单

每次输出使用独立 `dist/releases/版本-UTC时间/`，保留历史文件及运行数据。Windows ZIP 只从白名单整理：DevMux.exe、plugin/ 中 usb-relay、db-flash、mijia、ebpf 四包、README、使用说明、AGENT_USAGE、发布说明、许可、需求与发布流程、BUILD-INFO 和内部 SHA256SUMS。禁止打入 devmux-data、配置、日志、凭据及已安装插件数据。

同一个 Release 上传 Windows ZIP、对应源码 ZIP、BUILD-INFO.json、build.log、build-environment.txt、SHA256SUMS.txt。BUILD-INFO 记录 tag、完整提交、是否 dirty、构建命令、Python/系统、依赖清单与日志摘要、源码文件摘要；校验清单包含外部交付文件摘要，ZIP 内清单单独校验内容。候选证据明确标为 candidate，不能伪装正式版本。版本号或校验摘要本身不能证明二进制来源，当前证据是本地构建追溯记录，不声称独立可复现或签名认证。

## 验收与发布

专项测试：`python -m unittest tests.test_agent_plugins tests.test_release_bundle`，以及 `python -B tests/test_app_paths.py`、`python -B tests/test_plugin_runtime.py`、`python -B tests/test_plugin_frozen.py`、`python -B tests/test_agent.py`、`python -B -m tests.test_agent_extensions`。新增专项用例也纳入 run_all.py。

主线全量测试前先构建目标包。米家 DPAPI 受限失败时，保留原结果，在获授权的当前用户沙箱外重跑同一测试，不操作用户凭据。全部结果如实记录，未完成不得发布。正式上传需通过 MCP；若连接器没有 Release 创建/附件上传能力，说明缺口并取得用户同意后才能改用其他方式。

公开后从 Releases 下载 Windows ZIP 与校验清单并解压，核对版本、四包、说明、SHA-256 和完整提交，普通桌面启动并连接 MCP，使用隔离测试插件验证安装、启用、查询、停用。不得以无窗口启动代替桌面启动，也不得用打包或模拟测试冒充首次使用验收；受环境限制时明确列出未验证项。
