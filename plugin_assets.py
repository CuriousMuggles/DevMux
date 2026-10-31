"""Compile local plugin frontend resources into one portable HTML page."""
from html.parser import HTMLParser
from html import escape
from pathlib import Path
import re

SDK_ROOT = Path(__file__).resolve().parent / 'plugin_sdk'


def local_path(root, name):
    if not isinstance(name, str) or not name or '\\' in name or '?' in name or '#' in name:
        raise ValueError('资源路径无效: ' + str(name))
    if ':' in name or name.startswith('/'):
        raise ValueError('资源必须位于插件目录内: ' + name)
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('资源不存在或越过插件目录: ' + name)
    return path


def build_frontend(root, entry):
    root = Path(root).resolve()
    page = local_path(root, entry)
    output = []

    def resource(name, suffix):
        if name.startswith('devmux:'):
            key = name.removeprefix('devmux:')
            if key not in ('sdk.js', 'ui.css'):
                raise ValueError('未知 SDK 资源: ' + key)
            path = SDK_ROOT / key
        else:
            path = local_path(root, (page.parent / name).relative_to(root).as_posix())
        if path.suffix != suffix:
            raise ValueError('资源类型错误: ' + name)
        text = path.read_text(encoding='utf-8-sig')
        if suffix == '.css':
            # Portable plugins contain all resources; leave no relative assets behind.
            if re.search(r'@import\b', text, re.I) or any(not url.strip(' \"\'').startswith(('data:', '#')) for url in re.findall(r'url\((.*?)\)', text, re.I)):
                raise ValueError('样式资源须自包含，不支持 @import 或外部 url(): ' + name)
        return text

    class Compiler(HTMLParser):
        script_depth = False

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'script' and values.get('src'):
                if values.get('type') in ('module', 'importmap'):
                    raise ValueError('请先将模块构建为普通脚本再打包')
                text = resource(values['src'], '.js')
                output.append('<script>' + re.sub(r'</script', r'<\\/script', text, flags=re.I) + '</script>')
                self.script_depth = True
            elif tag == 'link' and values.get('rel', '').lower() == 'stylesheet':
                text = resource(values.get('href', ''), '.css')
                output.append('<style>' + re.sub(r'</style', r'<\\/style', text, flags=re.I) + '</style>')
            else:
                if tag in ('iframe', 'object', 'embed', 'base'):
                    raise ValueError('插件界面不支持外部嵌入或 base 标签')
                if tag in ('img', 'source', 'audio', 'video'):
                    for key in ('src', 'srcset', 'poster'):
                        if values.get(key) and not values[key].startswith('data:'):
                            raise ValueError('媒体资源请使用内联 data URL')
                output.append(self.get_starttag_text())

        def handle_endtag(self, tag):
            if tag == 'script' and self.script_depth:
                self.script_depth = False
            else:
                output.append('</' + tag + '>')

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)

        def handle_data(self, data):
            if not self.script_depth:
                output.append(data)

        def handle_entityref(self, name):
            output.append('&' + name + ';')

        def handle_charref(self, name):
            output.append('&#' + name + ';')

        def handle_decl(self, decl):
            output.append('<!' + decl + '>')

        def handle_comment(self, data):
            output.append('<!--' + data + '-->')

    Compiler(convert_charrefs=False).feed(page.read_text(encoding='utf-8-sig'))
    html = ''.join(output)
    if len(html.encode('utf-8')) > 4 * 1024 * 1024:
        raise ValueError('插件界面超过 4 MiB，请精简资源')
    return html
