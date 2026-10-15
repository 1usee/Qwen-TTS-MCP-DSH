"""Core layer: leaf models, configuration, secrets, audio, client, and services.

Modules in this package are imported explicitly (``from qwen_voice_mcp.core.models
import ToolResult``); this ``__init__`` deliberately imports nothing so that
importing a leaf never drags in the DashScope SDK or network code.
"""

from __future__ import annotations
