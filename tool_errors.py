"""远程扩展工具的错误类型。

它原本定义在 extension_tools 里，而 serial_stream 又要用它来拒绝「回执未确认」
的写入 —— 两个模块因此互相延迟导入。把叶子类型下沉到独立模块，两边都只向下
依赖，环消失。
"""
from __future__ import annotations


class RemoteToolError(RuntimeError):
    """远程扩展工具执行失败；uncertain 表示结果未确认，禁止自动重试。"""

    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.operation_uncertain = uncertain


__all__ = ['RemoteToolError']
