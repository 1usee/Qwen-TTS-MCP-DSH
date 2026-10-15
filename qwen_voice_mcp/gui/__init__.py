"""Tkinter 配置中心（GUI 层）。

导入本包不会导入 ``tkinter``；只有调用 ``config_window.launch()`` 时才会。
这样无显示环境下 CLI 依然可用。
"""

from __future__ import annotations

from qwen_voice_mcp.gui import config_window

__all__ = ["config_window"]
