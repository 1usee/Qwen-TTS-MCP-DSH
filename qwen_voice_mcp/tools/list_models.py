"""``list_voice_models`` adapter (spec §12).

A tool module is deliberately boring: it takes the assembled
:class:`~qwen_voice_mcp.core.services.VoiceServices` plus the tool arguments and
returns the ``ToolResult`` dictionary.  All policy lives in the service layer, so
this file can be unit-tested without an MCP runtime, and ``tools/**`` never
imports ``dashscope`` (Global Constraints).
"""

from __future__ import annotations

from typing import Any

from qwen_voice_mcp.core.services import VoiceServices

__all__ = ["list_voice_models"]


def list_voice_models(services: VoiceServices) -> dict[str, Any]:
    """Return the declared model catalogue (spec §6.3)."""
    return services.list_voice_models().to_dict()
