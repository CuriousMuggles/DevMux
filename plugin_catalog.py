"""官方插件身份名单。

单独成模块而不是写在 release_plugins 里：运行时只需要这份白名单，而
release_plugins 会连带导入打包用的 plugin_pack/plugin_assets，冻结进主程序没有意义。
自动升级只认这里列出的 ID，且必须与包内 manifest 的 ID 一致——插件自报「我是官方的」
不作数。
"""

OFFICIAL_PLUGINS = ('usb-relay', 'db-flash', 'mijia', 'ebpf')


def is_official(identifier):
    return isinstance(identifier, str) and identifier in OFFICIAL_PLUGINS
