"""Command line interface: ``qwen-voice-mcp <command>``.

Commands mirror spec §14. ``main`` accepts a plain ``argv`` list so tests (and the
root ``server.py`` shim) can call it directly without spawning a process.

Stream discipline: user-facing summaries go to **stdout**, diagnostics go to
**stderr**. ``Console`` objects are created per call rather than at import time so
they always bind to the *current* ``sys.stdout``/``sys.stderr`` — a console built
at import time keeps writing to the stream that existed then, which breaks capture
in tests and in embedded callers.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Sequence

import typer
from rich.console import Console
from rich.table import Table

from qwen_voice_mcp import __version__
from qwen_voice_mcp.core.config_store import Options, config_home, load_options, save_options
from qwen_voice_mcp.core.model_registry import ModelRegistry
from qwen_voice_mcp.core.secret_manager import SecretManager, VaultError

app = typer.Typer(
    name="qwen-voice-mcp",
    help="Qwen 语音 MCP 服务（TTS / 声音克隆 / 余额查询）",
    add_completion=False,
    no_args_is_help=True,
)
config_app = typer.Typer(help="查看与修改配置", no_args_is_help=True)
app.add_typer(config_app, name="config")

_SECRET_FIELD_NAMES = {"http_auth_token"}


def _use_utf8_streams() -> None:
    """Make stdout/stderr UTF-8 so Chinese output is not mangled.

    Python on Windows defaults the console streams to the active code page (often
    GBK). This CLI prints Chinese help, tables and messages, which would otherwise
    appear as mojibake — to exactly the users least able to work around it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # detached or non-reconfigurable stream
            pass


_use_utf8_streams()


def out() -> Console:
    """Console bound to the current stdout (user-facing results).

    ``width=200`` keeps tables from being truncated when stdout is not a real
    terminal (piped output, capture in tests), which would otherwise rewrite
    values such as a redacted token into useless ellipses.
    """
    return Console(file=sys.stdout, width=200, soft_wrap=False, highlight=False)


def err() -> Console:
    """Console bound to the current stderr (diagnostics; keeps stdout clean for MCP)."""
    return Console(file=sys.stderr, width=200, soft_wrap=False, highlight=False)


def _configure_logging(level: str) -> None:
    """Send all logging to stderr; stdout belongs to the MCP protocol in stdio mode."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _services(options: Options):
    """Build the service container used by CLI commands."""
    from qwen_voice_mcp.core.services import VoiceServices

    return VoiceServices(options)


# --------------------------------------------------------------------------- run


@app.command()
def run(
    transport: str = typer.Option(None, "--transport", help="stdio | streamable-http | sse"),
    port: int = typer.Option(None, "--port", help="HTTP 端口"),
    host: str = typer.Option(None, "--host", help="监听地址"),
    allow_non_loopback: bool = typer.Option(False, "--allow-non-loopback", help="允许非回环绑定"),
) -> None:
    """启动 MCP 服务器。"""
    from qwen_voice_mcp.server import run_server

    overrides: dict[str, Any] = {}
    if transport:
        overrides["transport"] = transport
    if port is not None:
        overrides["port"] = port
    if host:
        overrides["host"] = host
    if allow_non_loopback:
        overrides["allow_non_loopback"] = True

    options = load_options(**overrides)
    _configure_logging(options.log_level)
    raise typer.Exit(run_server(options))


@app.command()
def gui() -> None:
    """打开 Tkinter 配置中心。"""
    from qwen_voice_mcp.gui import config_window

    raise typer.Exit(config_window.launch())


@app.command()
def selftest() -> None:
    """离线自检：确认四个工具已注册，不访问网络。"""
    from qwen_voice_mcp.server import selftest as _selftest

    raise typer.Exit(_selftest(load_options()))


@app.command("verify-models")
def verify_models() -> None:
    """探测模型清单的真实可用性（需要有效密钥）。"""
    options = load_options()
    registry = ModelRegistry(default_model=options.default_model)
    services = _services(options)
    client = getattr(services, "_client", None)

    table = Table(title="模型可用性探测", show_lines=False)
    table.add_column("模型")
    table.add_column("能力")
    table.add_column("verified")
    table.add_column("说明")

    api_key = getattr(client, "api_key", None)
    if client is None or not api_key:
        for model in registry.all():
            table.add_row(model.name, ", ".join(model.capabilities), "false",
                          "未配置 API 密钥，无法探测")
        out().print(table)
        return

    for model in registry.all():
        try:
            available = bool(client.verify_model(model.name))
        except Exception:  # defensive: a probe failure means "not verified"
            available = False
        registry.mark_verified(model.name, available)
        table.add_row(model.name, ", ".join(model.capabilities), str(available).lower(),
                      "可用" if available else "不可用/无权限")

    out().print(table)


@app.command()
def balance() -> None:
    """查询账户余额与阈值状态。"""
    result = _services(load_options()).check_balance().to_dict()
    out().print(f"[bold]{result.get('message', '')}[/bold]")
    for key, value in result.items():
        if key in {"status", "message", "warnings"}:
            continue
        out().print(f"  {key}: {value}")
    for warning in result.get("warnings", []) or []:
        out().print(f"  [yellow]警告: {warning}[/yellow]")


# ------------------------------------------------------------------------ config


@config_app.command("show")
def config_show() -> None:
    """显示生效配置及其来源路径。"""
    options = load_options()
    table = Table(title="生效配置", show_header=True)
    table.add_column("配置项")
    table.add_column("值")
    for name in Options.model_fields:
        value: Any = getattr(options, name)
        if name in _SECRET_FIELD_NAMES and value:
            value = SecretManager.redact(str(value))
        table.add_row(name, str(value))
    out().print(table)
    out().print(f"配置目录: {config_home()}")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="配置项名"),
    value: str = typer.Argument(..., help="新值"),
) -> None:
    """设置一个配置项并保存。"""
    if key not in Options.model_fields:
        err().print(f"[red]未知配置项: {key}[/red]")
        raise typer.Exit(2)
    if key in _SECRET_FIELD_NAMES:
        err().print(f"[red]{key} 属于敏感信息，请使用 `config set-key` 或 GUI 写入加密库。[/red]")
        raise typer.Exit(2)

    try:
        merged = load_options().model_copy(update={key: _coerce(key, value)})
        # Re-validate through the model so range/type errors surface here.
        validated = Options(**merged.model_dump())
    except Exception as exc:  # noqa: BLE001
        err().print(f"[red]无效的值: {exc}[/red]")
        raise typer.Exit(2) from exc

    path = save_options(validated)
    out().print(f"已保存 {key} = {getattr(validated, key)} -> {path}")


def _coerce(key: str, value: str) -> Any:
    """Coerce a CLI string into the field's declared type."""
    annotation = str(Options.model_fields[key].annotation)
    if "bool" in annotation:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if "int" in annotation:
        return int(value)
    if "float" in annotation:
        return float(value)
    if "Path" in annotation:
        return Path(value)
    return value


@config_app.command("set-key")
def config_set_key(
    api_key: str = typer.Argument(..., help="DashScope API 密钥"),
    no_verify: bool = typer.Option(False, "--no-verify", help="跳过有效性验证"),
) -> None:
    """把 API 密钥写入加密库（默认先验证）。"""
    if not no_verify and not _validate_key(api_key):
        err().print("[red]密钥验证失败，未写入。如确认密钥可用，可加 --no-verify。[/red]")
        raise typer.Exit(1)

    SecretManager().store("DASHSCOPE_API_KEY", api_key)
    out().print(f"已写入加密库: {SecretManager.redact(api_key)}")


@config_app.command("clear-key")
def config_clear_key() -> None:
    """从加密库删除 API 密钥。"""
    removed = SecretManager().delete("DASHSCOPE_API_KEY")
    out().print("已删除密钥。" if removed else "加密库中没有密钥。")


def _validate_key(api_key: str) -> bool:
    """Light-weight validity check: one balance probe, never a billable call.

    Delegates to :func:`qwen_voice_mcp.core.balance_checker.validate_credential`,
    which the config GUI also uses, so both agree on what "valid" means.
    """
    from qwen_voice_mcp.core.balance_checker import validate_credential

    result = validate_credential(api_key, timeout=load_options().balance_timeout_seconds)
    if not result.ok:
        err().print(f"[yellow]{result.detail}[/yellow]")
    return result.ok


@app.command("version")
def version() -> None:
    """打印版本号。"""
    out().print(__version__)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point accepting an explicit ``argv`` (tests call this directly).

    The documented root form ``server.py --selftest`` is normalised to the
    ``selftest`` subcommand so both spellings work.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in args:
        args = [a for a in args if a != "--selftest"] + ["selftest"]
    if not args:
        args = ["--help"]

    command = typer.main.get_command(app)
    try:
        # In non-standalone mode Typer *returns* the exit code for a deliberate
        # `raise typer.Exit(code)` instead of raising it, so the result must be
        # propagated — discarding it would report success for usage errors.
        result = command(args, prog_name="qwen-voice-mcp", standalone_mode=False)
        return int(result) if isinstance(result, int) else 0
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except SystemExit as exc:
        return int(exc.code or 0)
    except VaultError as exc:
        err().print(f"[red]密钥库错误: {exc}[/red]")
        return 1
    except Exception as exc:  # noqa: BLE001
        # typer.Exit and click's UsageError derive from RuntimeError, so a
        # deliberate `raise typer.Exit(code)` — and an arg-parsing failure —
        # arrive here with the decided code attached. Honour it instead of
        # reporting success or a crash.
        code = getattr(exc, "exit_code", None)
        if isinstance(code, int):
            return code
        # Show the reason, not just the class name: "PermissionError" alone tells
        # a user nothing. The text is scrubbed so a credential can never appear.
        from qwen_voice_mcp.core.errors import safe_message

        detail = safe_message(exc) or type(exc).__name__
        err().print(f"[red]错误: {type(exc).__name__}: {detail}[/red]")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
