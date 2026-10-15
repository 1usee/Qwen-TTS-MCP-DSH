"""Tool registration for the four MCP tools (spec §12).

``register_all(mcp, services)`` binds the thin adapters in
:mod:`qwen_voice_mcp.tools.list_models`, :mod:`~qwen_voice_mcp.tools.tts`,
:mod:`~qwen_voice_mcp.tools.voice_clone` and :mod:`~qwen_voice_mcp.tools.balance`
onto a ``FastMCP`` instance with the Chinese trigger descriptions from the manual
(``Qwen-MCP.md`` §6.2).

Spec §12 says the tool bodies carry no ``try/except`` because the service layer
never raises; the ``_safe`` outer wrapper here is the belt-and-braces that turns
any *unexpected* escape into ``internal_error`` without leaking a stack trace.

``tools/**`` must never import ``dashscope`` (Global Constraints): this module
imports the MCP server type, the typed result model, and the service container —
nothing else.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from qwen_voice_mcp.core.models import Status, ToolResult
from qwen_voice_mcp.core.services import INTERNAL_ERROR_MESSAGE, VoiceServices
from qwen_voice_mcp.tools import balance as balance_tool
from qwen_voice_mcp.tools import list_models as list_models_tool
from qwen_voice_mcp.tools import tts as tts_tool
from qwen_voice_mcp.tools import voice_clone as voice_clone_tool

__all__ = ["TOOL_DESCRIPTIONS", "register_all", "TOOL_NAMES"]

logger = logging.getLogger(__name__)

#: Tool names in the order the spec lists them (spec §12 table).
TOOL_NAMES: tuple[str, ...] = (
    "list_voice_models",
    "synthesize_speech",
    "clone_voice",
    "check_balance",
)

#: Descriptions are the model-facing trigger sentences from ``Qwen-MCP.md`` §6.2.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_voice_models": (
        "查询可用语音模型。用户询问“有哪些语音模型可用”时调用；"
        "返回模型清单、能力与默认模型。只读，不计费。"
    ),
    "synthesize_speech": (
        "文字转语音。用户提交文字并要求语音输出时调用；"
        "执行前会自动检查账户余额，余额不足时返回 insufficient_balance 并提示充值。"
    ),
    "clone_voice": (
        "声音克隆。用户上传语音文件并要求复制音色时调用；"
        "audio_path 支持本地路径、data: URI 或 base64，执行前自动检查余额。"
    ),
    "check_balance": (
        "查询账户余额。用户询问余额，或执行计费调用前自动检查；"
        "端点不可用时返回 balance 为 null 并附警告，不算失败。"
    ),
}


def _safe(tool_name: str, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one adapter, converting an unexpected escape into ``internal_error``."""
    try:
        return call()
    except Exception:
        logger.exception("%s 工具执行失败", tool_name)
        return ToolResult(Status.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE).to_dict()


def register_all(mcp: FastMCP, services: VoiceServices) -> None:
    """Register ``list_voice_models``, ``synthesize_speech``, ``clone_voice`` and
    ``check_balance`` on ``mcp``.

    The closures keep explicit, annotated signatures: ``FastMCP`` derives each
    tool's ``inputSchema`` (and therefore its ``required`` list) from them.
    """

    @mcp.tool(name="list_voice_models", description=TOOL_DESCRIPTIONS["list_voice_models"])
    def list_voice_models() -> dict[str, Any]:
        """列出全部可用的 Qwen 语音模型（只读）。"""
        return _safe("list_voice_models", lambda: list_models_tool.list_voice_models(services))

    @mcp.tool(name="synthesize_speech", description=TOOL_DESCRIPTIONS["synthesize_speech"])
    def synthesize_speech(
        text: str,
        model: str | None = None,
        voice: str | None = None,
        output_format: str | None = None,
    ) -> dict[str, Any]:
        """把 text 合成为语音文件（执行前检查余额）。"""
        return _safe(
            "synthesize_speech",
            lambda: tts_tool.synthesize_speech(
                services, text, model=model, voice=voice, output_format=output_format
            ),
        )

    @mcp.tool(name="clone_voice", description=TOOL_DESCRIPTIONS["clone_voice"])
    def clone_voice(
        audio_path: str,
        voice_name: str,
        model: str | None = None,
        language_hints: list[str] | None = None,
    ) -> dict[str, Any]:
        """用参考音频克隆音色（执行前检查余额）。"""
        return _safe(
            "clone_voice",
            lambda: voice_clone_tool.clone_voice(
                services,
                audio_path,
                voice_name,
                model=model,
                language_hints=language_hints,
            ),
        )

    @mcp.tool(name="check_balance", description=TOOL_DESCRIPTIONS["check_balance"])
    def check_balance() -> dict[str, Any]:
        """查询 DashScope 账户余额（只读）。"""
        return _safe("check_balance", lambda: balance_tool.check_balance(services))
