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

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import typer
from rich.console import Console
from rich.table import Table

import config
from qwen_voice_mcp import __version__
from qwen_voice_mcp.core.config_store import (
    Options,
    config_home,
    load_options,
    save_options,
)
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
    api_key: str = typer.Argument(
        None,
        help="DashScope API 密钥；省略则安全提示输入（推荐），或用 --stdin 从管道读",
    ),
    no_verify: bool = typer.Option(False, "--no-verify", help="跳过有效性验证"),
    use_stdin: bool = typer.Option(False, "--stdin", help="从标准输入读取密钥"),
) -> None:
    """把 API 密钥写入加密库（默认先验证）。

    省略密钥参数时改用隐藏输入，因此密钥不会进入 shell 历史、进程列表或
    agent 的调用记录。位置参数仍保留，兼容旧脚本。
    """
    key = (api_key or "").strip()
    if not key:
        key = _read_key(use_stdin=use_stdin)
    if not key:
        err().print("[red]密钥为空，未写入。请重新执行并粘贴密钥。[/red]")
        raise typer.Exit(2)
    if not no_verify and not _validate_key(key):
        err().print("[red]密钥验证失败，未写入。如已知密钥可用，可加 --no-verify。[/red]")
        raise typer.Exit(1)

    manager = SecretManager()
    manager.store(config.API_KEY_ENV, key)
    out().print(f"已写入加密库: {SecretManager.redact(key)}")
    out().print(f"密钥库位置: {manager.path}（{_protection_tier(manager.path)}）")
    out().print("接下来: qwen-voice-mcp install --client claude,codex")


def _read_key(*, use_stdin: bool) -> str:
    """Read the API key without putting it on the command line.

    ``--stdin`` covers scripts and agents; the hidden prompt covers humans. Both
    keep the secret out of argv, which is world-readable on Unix and recorded by
    every shell history file.
    """
    if use_stdin:
        try:
            return (sys.stdin.read() or "").strip()
        except (OSError, AttributeError):  # detached stdin
            return ""
    # A hidden prompt on a non-terminal stdin blocks forever when the caller is an
    # agent or a service. Refuse loudly instead of hanging the tool call.
    stdin = sys.stdin
    if stdin is None or not getattr(stdin, "isatty", lambda: False)():
        err().print(
            "[red]当前不是交互终端，无法安全询问密钥。[/red]"
            "请改用管道输入（加 `--stdin`），或在终端里直接运行本命令。"
        )
        return ""
    try:
        value = typer.prompt("DashScope API Key（输入不回显）", hide_input=True)
    except (typer.Abort, EOFError):  # Ctrl-C / Ctrl-D
        return ""
    return (value or "").strip()


def _protection_tier(vault_path: Path) -> str:
    """Describe how the vault's master key is protected (for `set-key`)."""
    return {
        "keyring": "系统凭据管理器",
        "keyfile": "keyfile（本机密钥文件，与密文同盘，弱于凭据管理器）",
        "passphrase": "口令派生（PBKDF2）",
    }.get(_vault_mode(vault_path) or "", "未知")


def _tier_label(mode: str | None) -> str:
    """Short protection label for the `doctor` table."""
    return {
        "keyring": "系统凭据管理器",
        "keyfile": "本机密钥文件（弱于凭据管理器）",
        "passphrase": "口令派生",
    }.get(mode or "", "未知")


def _vault_mode(vault_path: Path) -> str | None:
    """Read the ``mode`` field of the vault document; never raises."""
    try:
        payload = json.loads(Path(vault_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = payload.get("mode") if isinstance(payload, dict) else None
    return mode if isinstance(mode, str) else None


@config_app.command("clear-key")
def config_clear_key() -> None:
    """从加密库删除 API 密钥。"""
    removed = SecretManager().delete(config.API_KEY_ENV)
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


# ------------------------------------------------------------------------- setup

#: Clients `install` / `print-config` know how to configure.
CLIENTS: tuple[str, ...] = ("claude", "codex", "dsh")


def _parse_clients(raw: str | None) -> list[str]:
    """Split ``"claude,codex"`` into known client names, or exit 2."""
    names = [part.strip().lower() for part in (raw or "").split(",") if part.strip()]
    unknown = [name for name in names if name not in CLIENTS]
    if unknown:
        err().print(
            f"[red]未知客户端: {', '.join(unknown)}（可选: {', '.join(CLIENTS)}）[/red]"
        )
        raise typer.Exit(2)
    return names


@app.command()
def setup(
    use_stdin: bool = typer.Option(False, "--stdin", help="从标准输入读取密钥"),
    no_verify: bool = typer.Option(False, "--no-verify", help="跳过有效性验证"),
    client: str = typer.Option(
        None, "--client", help="保存后顺便注册客户端：claude / codex / dsh（逗号分隔）"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览客户端配置改动"),
) -> None:
    """首次配置：安全录入 API Key（默认隐藏输入），可选顺带接入客户端。"""
    key = _read_key(use_stdin=use_stdin)
    if not key:
        err().print("[red]未读取到密钥，已取消。[/red]")
        raise typer.Exit(2)
    if not no_verify and not _validate_key(key):
        err().print("[red]密钥验证失败，未写入。确认密钥可用后可加 --no-verify。[/red]")
        raise typer.Exit(1)

    manager = SecretManager()
    manager.store(config.API_KEY_ENV, key)
    out().print(f"[green]已保存密钥[/green] {SecretManager.redact(key)}")
    out().print(f"  密钥库: {manager.path}")
    out().print(f"  保护方式: {_protection_tier(manager.path)}")

    targets = _parse_clients(client)
    if targets:
        raise typer.Exit(_install_clients(targets, dry_run=dry_run))

    out().print("")
    out().print("接下来接入客户端（任选）:")
    out().print("  qwen-voice-mcp install --client claude,codex")
    out().print("  qwen-voice-mcp print-config --client claude   # 只打印配置，不改文件")
    out().print("  qwen-voice-mcp doctor                         # 体检")


# ------------------------------------------------------------------------ doctor


def _key_status() -> tuple[str, str]:
    """Return ``(status, detail)`` for the resolved API key, without leaking it."""
    from_env = os.environ.get(config.API_KEY_ENV)
    if from_env:
        return "已配置", f"来源: 环境变量 {SecretManager.redact(from_env)}"
    try:
        stored = SecretManager().retrieve(config.API_KEY_ENV)
    except VaultError as exc:
        return "不可用", f"密钥库无法读取: {exc}"
    except Exception as exc:  # noqa: BLE001 - doctor must never crash
        return "不可用", f"密钥库异常: {type(exc).__name__}"
    if stored:
        return "已配置", f"来源: 加密库 {SecretManager.redact(str(stored))}"
    return "未配置", "运行 `qwen-voice-mcp setup` 录入"


def _check_import(module: str) -> tuple[str, str]:
    try:
        __import__(module)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return "缺失", f"import 失败: {type(exc).__name__}"
    try:
        from importlib.metadata import version

        return "OK", version(module)
    except Exception:  # noqa: BLE001 - installed but not a distribution
        return "OK", "已安装"


def _claude_registered(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    return isinstance(servers, dict) and "qwen-voice" in servers


def _codex_registered(path: Path) -> bool:
    try:
        import tomllib

        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    servers = payload.get("mcp_servers") if isinstance(payload, dict) else None
    return isinstance(servers, dict) and "qwen-voice" in servers


@app.command()
def doctor() -> None:
    """体检：依赖、密钥来源、密钥库保护方式、客户端注册状态。"""
    import platform

    from qwen_voice_mcp.core.config_store import load_dotenv_files
    from qwen_voice_mcp.installers import codex_config_path

    # Doctor must resolve the key the same way the server does, including .env.
    load_dotenv_files()

    table = Table(title="qwen-voice-mcp 体检", show_header=True)
    table.add_column("检查项")
    table.add_column("状态")
    table.add_column("说明")

    table.add_row("版本", "OK", __version__)
    table.add_row("Python", "OK", platform.python_version())

    mcp_status, mcp_detail = _check_import("mcp")
    try:  # the exact API the server needs, not just the package
        from mcp.server.fastmcp import FastMCP  # noqa: F401

        mcp_detail = f"{mcp_detail}（FastMCP 可用）"
    except ImportError:
        mcp_status = "不兼容"
        mcp_detail = f"{mcp_detail}，缺少 mcp.server.fastmcp；需要 mcp<2"
    table.add_row("mcp 包", mcp_status, mcp_detail)

    for module in ("dashscope", "soundfile"):
        status, detail = _check_import(module)
        table.add_row(module, status, detail or "—")

    try:
        import keyring

        backend = type(keyring.get_keyring()).__name__
        keyring_state = "OK"
        keyring_detail = backend
    except Exception as exc:  # noqa: BLE001
        keyring_state, keyring_detail = "不可用", f"{type(exc).__name__}，将回退到本机密钥文件"
    table.add_row("keyring", keyring_state, keyring_detail)

    home = config_home()
    table.add_row("配置目录", "OK" if home.exists() else "新建", str(home))

    key_state, key_detail = _key_status()
    table.add_row("API Key", key_state, key_detail)

    vault_path = SecretManager().path
    mode = _vault_mode(vault_path)
    table.add_row(
        "密钥库",
        mode or "不存在",
        f"{vault_path} · {_tier_label(mode)}" if mode else str(vault_path),
    )

    claude_path = Path.cwd() / ".mcp.json"
    table.add_row(
        "Claude Code",
        "已注册" if _claude_registered(claude_path) else "未注册",
        str(claude_path),
    )

    codex_path = codex_config_path()
    table.add_row(
        "Codex",
        "已注册" if _codex_registered(codex_path) else "未注册",
        str(codex_path),
    )

    out().print(table)

    if key_state != "已配置":
        out().print("")
        out().print("下一步: qwen-voice-mcp setup")
    if not _claude_registered(claude_path) and not _codex_registered(codex_path):
        out().print("接入客户端: qwen-voice-mcp install --client claude,codex")


# ------------------------------------------------------------------ print-config


def _dsh_yaml() -> str:
    """DSH / DeepseekHarness ``cordis.patch.yml`` fragment."""
    return (
        "mcp_servers:\n"
        "  qwen-voice:\n"
        "    command: qwen-voice-mcp\n"
        '    args: ["run", "--transport", "stdio"]\n'
    )


@app.command("print-config")
def print_config(
    client: str = typer.Option(..., "--client", help="claude | codex | dsh"),
) -> None:
    """打印可直接粘贴的客户端配置（不修改任何文件，不含 API Key）。"""
    from qwen_voice_mcp.installers import claude_mcp_json, codex_toml_snippet

    name = (client or "").strip().lower()
    if name not in CLIENTS:
        err().print(f"[red]未知客户端: {client}（可选: {', '.join(CLIENTS)}）[/red]")
        raise typer.Exit(2)

    if name == "claude":
        sys.stdout.write(claude_mcp_json())
    elif name == "codex":
        sys.stdout.write(codex_toml_snippet())
    else:
        sys.stdout.write(_dsh_yaml())


# --------------------------------------------------------------------- install


def _report_install(result: Any) -> None:
    """Print one install/uninstall outcome."""
    from qwen_voice_mcp.installers import InstallResult

    assert isinstance(result, InstallResult)
    label = f"{result.client} ({result.path})"
    if result.action == "preview":
        out().print(f"[bold]预览：{label}（未写盘）[/bold]")
        out().print(result.detail, markup=False)
    elif result.action == "written":
        out().print(f"[green]已写入[/green] {label}")
    elif result.action == "removed":
        out().print(f"[green]已移除[/green] {label}")
    else:
        out().print(f"[dim]无需改动[/dim] {label}")


def _install_clients(
    targets: Sequence[str],
    *,
    dry_run: bool = False,
    uninstall: bool = False,
    project_dir: Path | None = None,
    codex_home: Path | None = None,
    use_cli: bool = False,
) -> int:
    """Register (or remove) the server for each client; return an exit code."""
    from qwen_voice_mcp.installers import (
        InstallError,
        codex_config_path,
        install_claude,
        install_codex,
        uninstall_claude,
        uninstall_codex,
    )

    for name in targets:
        try:
            if name == "claude":
                base = Path(project_dir) if project_dir else Path.cwd()
                path = base / ".mcp.json"
                result = (
                    uninstall_claude(path, dry_run=dry_run)
                    if uninstall
                    else install_claude(path, dry_run=dry_run, use_cli=use_cli)
                )
            elif name == "codex":
                path = codex_config_path(codex_home)
                result = (
                    uninstall_codex(path, dry_run=dry_run)
                    if uninstall
                    else install_codex(path, dry_run=dry_run)
                )
            else:  # dsh: no writable config format we can safely merge
                if uninstall:
                    out().print(
                        "[bold]DeepseekHarness[/bold] — 没有自动注册，请在配置里手动删除 "
                        "`qwen-voice` 条目。"
                    )
                else:
                    out().print(
                        "[bold]DeepseekHarness[/bold] — 把下面片段加入 cordis.patch.yml:"
                    )
                    out().print(_dsh_yaml(), markup=False)
                continue
        except InstallError as exc:
            err().print(f"[red]{name}: {exc}[/red]")
            return 1
        _report_install(result)
    return 0


@app.command()
def install(
    client: str = typer.Option(
        "claude,codex", "--client", help="claude / codex / dsh，逗号分隔"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览，不写文件"),
    uninstall: bool = typer.Option(False, "--uninstall", help="移除注册"),
    project_dir: Path = typer.Option(None, "--project-dir", help="Claude Code 项目目录"),
    codex_home: Path = typer.Option(None, "--codex-home", help="Codex 配置目录"),
    use_cli: bool = typer.Option(
        False, "--use-cli", help="Claude Code 改用 `claude mcp add`（用户级）"
    ),
) -> None:
    """把本服务注册进 Claude Code / Codex 的 MCP 配置（幂等，可撤销）。"""
    targets = _parse_clients(client)
    raise typer.Exit(
        _install_clients(
            targets,
            dry_run=dry_run,
            uninstall=uninstall,
            project_dir=project_dir,
            codex_home=codex_home,
            use_cli=use_cli,
        )
    )


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
