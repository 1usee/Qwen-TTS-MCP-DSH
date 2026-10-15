"""FastMCP assembly, logging, and the documented startup path (spec §12.1).

* :func:`build_server` is the one place that turns options + services into a
  ``FastMCP`` instance.  ``FastMCP`` receives ``name``/``instructions``/``host``/
  ``port`` only — the installed ``mcp`` 1.30 ``__init__`` has **no** ``version``
  parameter (spec §3 deviation D1); the version lives in ``__version__``.
* :func:`configure_logging` attaches a ``sys.stderr`` handler to the package
  logger, because stdio transport shares stdout with the JSON-RPC protocol and
  nothing else may be printed there.
* The default (production) path builds a real :class:`SecretManager` and calls
  ``load_into_env()`` so the documented "环境变量优先，其次密钥库" startup behaviour
  holds.  ``VoiceServices(..., secret_manager=None)`` is the test path and never
  touches the vault.
* :func:`selftest` is offline by construction: it assembles the server with an
  inert checker/client and only lists the registered tools.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from qwen_voice_mcp import __version__
from qwen_voice_mcp.core.config_store import Options, load_options
from qwen_voice_mcp.core.models import Status, ToolResult
from qwen_voice_mcp.core.secret_manager import SecretManager
from qwen_voice_mcp.core.services import VoiceServices
from qwen_voice_mcp.tools import TOOL_NAMES, register_all

__all__ = [
    "EXPECTED_TOOLS",
    "INSTRUCTIONS",
    "SERVER_NAME",
    "build_server",
    "configure_logging",
    "list_tool_names",
    "run_server",
    "selftest",
]

#: Advertised server name (spec §12.1 / deviation D1).
SERVER_NAME = "qwen-voice-mcp"

#: The four tools, in the order the spec lists them.
EXPECTED_TOOLS: tuple[str, ...] = TOOL_NAMES

INSTRUCTIONS = (
    "Qwen 语音工具集：文字转语音（synthesize_speech）、声音克隆（clone_voice）、"
    "模型清单（list_voice_models）与余额查询（check_balance）。\n"
    "两个计费工具在执行前都会自动检查账户余额；余额 ≤ 阈值时返回 "
    "insufficient_balance 并提示充值，余额未知时按 strict_balance 决定是否放行。\n"
    f"版本 {__version__}。"
)

LOG_LOGGER_NAME = "qwen_voice_mcp"


def configure_logging(level: str = "INFO", *, stream: Any = None) -> logging.Logger:
    """Attach a single logging handler writing to ``stream`` (stderr by default).

    stdio transport must never write protocol data to stdout, so the handler is
    bound explicitly to ``sys.stderr`` and ``propagate`` is disabled — a root
    handler can then never duplicate a record onto a stream we do not control.
    """
    target = sys.stderr if stream is None else stream
    logger = logging.getLogger(LOG_LOGGER_NAME)
    handler = logging.StreamHandler(target)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.handlers[:] = [handler]
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    return logger


def _default_secret_manager() -> SecretManager:
    """Build the real vault and apply the documented env-injection behaviour.

    A vault that cannot be opened (wrong master key, tampered file, no keyring)
    must never stop the server from starting — it just means "no stored key".
    """
    manager = SecretManager()
    try:
        manager.load_into_env()
    except Exception:
        logging.getLogger(LOG_LOGGER_NAME).warning(
            "密钥库不可用，本次启动按未配置 API Key 处理", exc_info=True
        )
    return manager


def build_server(
    options: Options | None = None, *, services: VoiceServices | None = None
) -> FastMCP:
    """Assemble the four tools into a ``FastMCP`` instance.

    Parameters
    ----------
    options:
        Effective options; defaults to :func:`load_options`.
    services:
        Pre-built service container (used by ``--selftest`` and tests).  When
        omitted, the production path builds a real ``SecretManager`` and injects
        its stored key into the environment before assembling the services.
    """
    opts = options if options is not None else load_options()
    configure_logging(opts.log_level)
    if services is None:
        services = VoiceServices(opts, secret_manager=_default_secret_manager())

    server = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        host=opts.host,
        port=opts.port,
    )
    register_all(server, services)
    return server


async def _list_tool_names(server: FastMCP) -> list[str]:
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
    return [tool.name for tool in listed.tools]


def list_tool_names(server: FastMCP) -> list[str]:
    """Discover the tools ``server`` advertises through the in-memory session."""
    return asyncio.run(_list_tool_names(server))


def run_server(options: Options | None = None) -> int:
    """Build and run the server with the configured transport (blocking)."""
    opts = options if options is not None else load_options()
    if opts.transport != "stdio":
        # stdio binds nothing; an HTTP transport must stay on loopback unless the
        # operator explicitly opted in.
        opts.reject_non_loopback()
    server = build_server(opts)
    server.run(transport=opts.transport)
    return 0


class _OfflineChecker:
    """No-network checker for ``--selftest``: never probes the balance endpoint."""

    def gate(self, estimated_cost: float, strict: bool) -> ToolResult | None:
        return None

    def warn_if_tight(self, estimated_cost: float) -> list[str]:
        return []

    def as_tool_result(self) -> ToolResult:
        return ToolResult(
            Status.SUCCESS,
            "离线自检：未查询余额",
            {"balance": None, "currency": None, "threshold": None, "is_sufficient": None},
        )


class _OfflineClient:
    """Inert client for ``--selftest``: refuses to pretend it reached DashScope."""

    def synthesize(self, *args: Any, **kwargs: Any) -> ToolResult:
        return ToolResult(Status.INTERNAL_ERROR, "自检模式未连接 DashScope 合成接口")

    def clone_voice(self, *args: Any, **kwargs: Any) -> ToolResult:
        return ToolResult(Status.INTERNAL_ERROR, "自检模式未连接 DashScope 克隆接口")

    def upload_reference(self, path: Any) -> str:
        raise RuntimeError("自检模式不会上传参考音频")


def selftest(options: Options | None = None, *, stream: Any = None) -> int:
    """Offline self-check: build the server, list the tools, print their names.

    Returns ``0`` when all four documented tools are exposed, otherwise ``1``.
    No network call is made and no vault is opened.
    """
    out = sys.stdout if stream is None else stream
    opts = options if options is not None else load_options()
    services = VoiceServices(
        opts, secret_manager=None, client=_OfflineClient(), checker=_OfflineChecker()
    )
    server = build_server(opts, services=services)
    names = list_tool_names(server)

    print("SELFTEST OK", file=out)
    for name in EXPECTED_TOOLS:
        print(f"  - {name}", file=out)
    for name in names:
        if name not in EXPECTED_TOOLS:
            print(f"  - {name} (未在规格中声明)", file=out)

    missing = [name for name in EXPECTED_TOOLS if name not in names]
    if missing:
        print(f"缺少工具：{', '.join(missing)}", file=sys.stderr)
        return 1
    return 0
