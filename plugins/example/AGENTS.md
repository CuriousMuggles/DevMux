# 插件开发约定

- 后端通过 create_plugin(root) 和 call(action, arguments) 提供业务接口，可继承 plugin_sdk.Plugin。
- 配置、日志及凭据写入 self.data_dir；不要写入插件代码目录。
- 前端使用 DevMuxPlugin SDK 与公共 UI 组件，不直接调用宿主 WebSocket，不再创建浮动窗口外壳。
- call/hostCall 抛出 PluginError；超时不代表业务未执行，不自动重试硬件写操作。
- 宿主文件选择权限须在 manifest.permissions 声明；远程操作还需实现执行端授权及任务所有者校验。
- 监听、定时器及资源用 onDispose/on_dispose 注册清理；存在活动任务时实现 can_stop()。
- 页面资源使用插件内相对路径或 devmux:sdk.js、devmux:ui.css；打包自动内联，不使用外部CDN。
- 修改后 check，再在隔离 serve 环境验证，最后 pack；测试随开发分支获准后一起合入主线。
