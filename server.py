#!/usr/bin/env python
"""Root entry point: ``python server.py [--transport stdio] [--selftest]``.

Deviation D8 keeps both documented invocations working: the installable package
(``qwen-voice-mcp`` / ``python -m qwen_voice_mcp``) and the manual's
``python server.py --transport stdio``.  This module is the forwarding shim — it
parses the documented command line and delegates to
:mod:`qwen_voice_mcp.server` (and, when it exists, :mod:`qwen_voice_mcp.cli`).

``--selftest`` is handled here directly rather than through the CLI layer, so the
documented offline check works even before/without the optional CLI module.
"""

from __future__ import annotations

import argparse
import sys

from qwen_voice_mcp.core.config_store import Options, load_options


def build_parser() -> argparse.ArgumentParser:
    """Return the documented root-command argument parser (spec §14)."""
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Qwen Voice MCP server（等价于 qwen-voice-mcp run）",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default=None,
        help="传输方式；默认取配置（stdio）",
    )
    parser.add_argument("--host", default=None, help="监听地址；默认取配置（127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口；默认取配置（8765）")
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="允许 HTTP 传输绑定非回环地址（默认拒绝）",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="离线自检：打印四个已注册工具后退出，不访问网络",
    )
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, object]:
    """Translate parsed flags into ``load_options`` overrides (only what was set)."""
    overrides: dict[str, object] = {}
    if args.transport:
        overrides["transport"] = args.transport
    if args.host:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.allow_non_loopback:
        overrides["allow_non_loopback"] = True
    return overrides


def _delegate_to_cli(argv: list[str]) -> int | None:
    """Hand ``argv`` to :mod:`qwen_voice_mcp.cli` when that layer exists."""
    try:
        from qwen_voice_mcp import cli
    except ImportError:
        return None
    entry = getattr(cli, "main", None)
    if not callable(entry):
        return None
    return int(entry(argv) or 0)


# Flags this shim owns. They are NOT valid top-level CLI options (the CLI takes
# them on its `run` subcommand), so forwarding them to the CLI would make it print
# help and exit 2 — the documented `python server.py --transport stdio` must reach
# the server here instead.
_ROOT_OWNED_FLAGS = ("--transport", "--host", "--port", "--allow-non-loopback")


def _owns_argv(argv: list[str]) -> bool:
    """True when argv uses a flag this shim handles itself."""
    return any(arg.split("=", 1)[0] in _ROOT_OWNED_FLAGS for arg in argv)


def main(argv: list[str] | None = None) -> int:
    """Run the server (or the self-test) and return the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)

    if "--selftest" in args:
        from qwen_voice_mcp.server import selftest

        parsed = build_parser().parse_args(args)
        options: Options = load_options(**_overrides(parsed))
        return selftest(options)

    if not _owns_argv(args):
        delegated = _delegate_to_cli(args)
        if delegated is not None:
            return delegated

    from qwen_voice_mcp.server import run_server

    parsed = build_parser().parse_args(args)
    return run_server(load_options(**_overrides(parsed)))


if __name__ == "__main__":
    raise SystemExit(main())
