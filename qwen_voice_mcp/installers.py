"""Registration of the MCP server with Claude Code and Codex.

The interesting part is not "write a JSON file" but doing it *safely* on files
the user and two other tools also own:

* Codex keeps ``~/.codex/config.toml`` with unrelated sections and comments. A
  naive append creates a duplicate table (a TOML syntax error), and a
  ``tomllib`` round-trip would delete the comments. So the merge is surgical:
  only the ``[mcp_servers.qwen-voice]`` segments are replaced, everything else
  is kept byte-for-byte, and the result is re-parsed before it is written.
* Claude Code's project config is ``.mcp.json``; other servers live in the same
  object and must survive.
* Neither file may ever contain the API key — the key lives in the vault.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "InstallError",
    "InstallResult",
    "SERVER_ARGS",
    "SERVER_COMMAND",
    "SERVER_NAME",
    "claude_mcp_json",
    "claude_server_entry",
    "codex_config_path",
    "codex_server_values",
    "codex_toml_snippet",
    "install_claude",
    "install_codex",
    "uninstall_claude",
    "uninstall_codex",
]

#: Key used in both clients' config files.
SERVER_NAME = "qwen-voice"
#: Console script installed by ``pipx install .`` / ``uv tool install .``.
SERVER_COMMAND = "qwen-voice-mcp"
#: ``run`` defaults to stdio already, but being explicit survives config drift.
SERVER_ARGS: tuple[str, ...] = ("run", "--transport", "stdio")

#: ``[mcp_servers.<name>]`` dotted path in Codex's config.toml.
_CODEX_TABLE = ("mcp_servers", SERVER_NAME)

#: Matches a TOML table header and captures its dotted path.
_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$")


class InstallError(Exception):
    """Raised when a config file cannot be merged safely. Nothing is written."""


@dataclass
class InstallResult:
    """What an install/uninstall call did (or would do, for a dry run)."""

    client: str
    path: Path
    action: str  # written | removed | unchanged | preview
    detail: str = ""
    commands: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.action in ("written", "removed")


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def claude_server_entry() -> dict[str, Any]:
    """The ``.mcp.json`` object for this server (no key, no absolute paths)."""
    return {"command": SERVER_COMMAND, "args": list(SERVER_ARGS)}


def claude_mcp_json() -> str:
    """A complete, pasteable ``.mcp.json`` document."""
    document = {"mcpServers": {SERVER_NAME: claude_server_entry()}}
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def codex_server_values() -> dict[str, Any]:
    """The ``[mcp_servers.qwen-voice]`` table values."""
    return {"command": SERVER_COMMAND, "args": list(SERVER_ARGS)}


def codex_toml_snippet() -> str:
    """A pasteable ``config.toml`` fragment."""
    return _render_codex_block()


def codex_config_path(home: Path | None = None) -> Path:
    """Resolve ``config.toml``: explicit ``home``, else ``$CODEX_HOME``, else ``~/.codex``."""
    if home is not None:
        return Path(home) / "config.toml"
    import os

    env_home = os.environ.get("CODEX_HOME")
    return (Path(env_home) if env_home else Path.home() / ".codex") / "config.toml"


# ---------------------------------------------------------------------------
# TOML rendering / merging
# ---------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    """Render a scalar/list the way TOML wants it (strings JSON-quoted is valid TOML)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise InstallError(f"无法写入 TOML 的值类型: {type(value).__name__}")


def _render_codex_block() -> str:
    header = "[" + ".".join(_CODEX_TABLE) + "]"
    lines = [header]
    for key, value in codex_server_values().items():
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _header_path(line: str) -> tuple[str, ...] | None:
    """Return the dotted path of a table header line, or ``None`` for non-headers."""
    match = _HEADER_RE.match(line)
    if match is None:
        return None
    raw = match.group(1)
    # Split on dots that are not inside a quoted key.
    parts = [part.strip().strip('"').strip("'") for part in raw.split(".")]
    return tuple(part for part in parts if part)


def _is_our_table(path: tuple[str, ...]) -> bool:
    """True for ``[mcp_servers.qwen-voice]`` and any of its sub-tables."""
    return path[: len(_CODEX_TABLE)] == _CODEX_TABLE


def _split_segments(text: str) -> list[tuple[tuple[str, ...] | None, list[str]]]:
    """Split TOML text into ``(header_path, lines)`` segments.

    The preamble before the first header gets the ``None`` key. Keeping the raw
    lines means comments and formatting outside our table survive untouched.
    """
    segments: list[tuple[tuple[str, ...] | None, list[str]]] = []
    current_header: tuple[str, ...] | None = None
    current_lines: list[str] = []

    for line in text.splitlines(keepends=True):
        path = _header_path(line)
        if path is not None:
            segments.append((current_header, current_lines))
            current_header = path
            current_lines = [line]
        else:
            current_lines.append(line)

    segments.append((current_header, current_lines))
    return segments


def merge_codex_toml(existing: str, *, remove: bool = False) -> str:
    """Return ``existing`` with our table replaced/added, everything else intact.

    ``remove=True`` drops our table instead, keeping every other byte.

    Raises :class:`InstallError` when the input is not valid TOML or when the
    merged text fails verification — in both cases the caller writes nothing.
    """
    if existing.strip():
        try:
            tomllib.loads(existing)
        except tomllib.TOMLDecodeError as exc:
            raise InstallError(f"现有 config.toml 不是合法的 TOML，已停止以免损坏: {exc}") from exc

    segments = _split_segments(existing)
    kept = [
        (header, lines)
        for header, lines in segments
        if header is not None and not _is_our_table(header)
    ]
    # A file with no tables at all (only key-values) contributed its preamble via
    # `segments[0]`; that segment is dropped only when it consists of whitespace.
    preamble = segments[0][1] if segments and segments[0][0] is None else []
    if preamble and "".join(preamble).strip():
        kept.insert(0, (None, preamble))

    body = "".join("".join(lines) for _, lines in kept).rstrip("\n")
    if remove:
        merged = f"{body}\n" if body else ""
        if existing.strip():
            _verify_codex_removal(merged)
        return merged

    block = _render_codex_block()
    merged = f"{body}\n\n{block}" if body else block

    _verify_codex_merge(existing, merged)
    return merged


def _verify_codex_removal(after: str) -> None:
    """Prove the removal left a parseable document without our table."""
    try:
        parsed = tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
        raise InstallError(f"移除后 config.toml 无法解析，已停止: {exc}") from exc
    table: Any = parsed
    for part in _CODEX_TABLE:
        table = table.get(part, {}) if isinstance(table, dict) else {}
    if table:
        raise InstallError("移除后仍残留 mcp_servers.qwen-voice，已停止")


def _verify_codex_merge(before: str, after: str) -> None:
    """Re-parse the merged document and prove nothing else was lost."""
    try:
        parsed = tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
        raise InstallError(f"合并后的 config.toml 无法解析，已停止: {exc}") from exc

    table: Any = parsed
    for part in _CODEX_TABLE:
        table = table.get(part, {}) if isinstance(table, dict) else {}
    if table.get("command") != SERVER_COMMAND:
        raise InstallError("合并后未能写入 mcp_servers.qwen-voice，已停止")

    if not before.strip():
        return
    original = tomllib.loads(before)
    for key, value in original.items():
        if key == "mcp_servers":
            continue
        if parsed.get(key) != value:
            raise InstallError(f"合并会破坏 config.toml 的 [{key}] 配置，已停止")


# ---------------------------------------------------------------------------
# JSON merging
# ---------------------------------------------------------------------------


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path.name} 不是合法的 JSON，已停止以免损坏: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallError(f"{path.name} 的顶层不是对象，已停止")
    return payload


def merge_claude_json(existing: str, *, remove: bool = False) -> str:
    """Return ``.mcp.json`` text with our server added/updated (or removed)."""
    if existing.strip():
        try:
            payload = json.loads(existing)
        except json.JSONDecodeError as exc:
            raise InstallError(f".mcp.json 不是合法的 JSON，已停止以免损坏: {exc}") from exc
        if not isinstance(payload, dict):
            raise InstallError(".mcp.json 的顶层不是对象，已停止")
    else:
        payload = {}

    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        payload["mcpServers"] = servers

    if remove:
        servers.pop(SERVER_NAME, None)
        if not servers:
            payload.pop("mcpServers", None)
    else:
        servers[SERVER_NAME] = claude_server_entry()

    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Filesystem operations
# ---------------------------------------------------------------------------


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _claude_scope_label(path: Path) -> str:
    return f"项目配置 {path}"


def install_claude(
    config_path: Path,
    *,
    dry_run: bool = False,
    use_cli: bool = False,
) -> InstallResult:
    """Register the server in a Claude Code ``.mcp.json``.

    ``use_cli`` shells out to ``claude mcp add`` instead, which is the right
    thing for user scope but cannot be exercised offline; it is opt-in.
    """
    config_path = Path(config_path)

    if use_cli and not dry_run:
        executable = shutil.which("claude")
        if executable is None:
            raise InstallError("未找到 `claude` 命令；请改用项目级 .mcp.json（去掉 --use-cli）")
        command = [
            executable,
            "mcp",
            "add",
            SERVER_NAME,
            "--",
            SERVER_COMMAND,
            *SERVER_ARGS,
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise InstallError(f"`claude mcp add` 失败（退出码 {completed.returncode}）: {detail}")
        return InstallResult(
            "claude", config_path, "written", "已通过 `claude mcp add` 注册", [" ".join(command)]
        )

    existing = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""
    merged = merge_claude_json(existing)
    if dry_run:
        return InstallResult("claude", config_path, "preview", merged, [" ".join(
            ["claude", "mcp", "add", SERVER_NAME, "--", SERVER_COMMAND, *SERVER_ARGS]
        )])
    if merged == existing:
        return InstallResult("claude", config_path, "unchanged", _claude_scope_label(config_path))
    _write_text_atomic(config_path, merged)
    return InstallResult("claude", config_path, "written", _claude_scope_label(config_path))


def uninstall_claude(config_path: Path, *, dry_run: bool = False) -> InstallResult:
    config_path = Path(config_path)
    if not config_path.exists():
        return InstallResult("claude", config_path, "unchanged", "配置文件不存在")
    existing = config_path.read_text(encoding="utf-8-sig")
    merged = merge_claude_json(existing, remove=True)
    if dry_run:
        return InstallResult("claude", config_path, "preview", merged)
    if merged == existing:
        return InstallResult("claude", config_path, "unchanged", "未注册，无需移除")
    _write_text_atomic(config_path, merged)
    return InstallResult("claude", config_path, "removed", _claude_scope_label(config_path))


def install_codex(config_path: Path, *, dry_run: bool = False) -> InstallResult:
    """Register the server in a Codex ``config.toml``."""
    config_path = Path(config_path)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    merged = merge_codex_toml(existing)
    if dry_run:
        return InstallResult("codex", config_path, "preview", merged)
    if merged == existing:
        return InstallResult("codex", config_path, "unchanged", f"Codex 配置 {config_path}")
    _write_text_atomic(config_path, merged)
    return InstallResult("codex", config_path, "written", f"Codex 配置 {config_path}")


def uninstall_codex(config_path: Path, *, dry_run: bool = False) -> InstallResult:
    config_path = Path(config_path)
    if not config_path.exists():
        return InstallResult("codex", config_path, "unchanged", "配置文件不存在")
    existing = config_path.read_text(encoding="utf-8")
    merged = merge_codex_toml(existing, remove=True)
    if dry_run:
        return InstallResult("codex", config_path, "preview", merged)
    if merged == existing:
        return InstallResult("codex", config_path, "unchanged", "未注册，无需移除")
    _write_text_atomic(config_path, merged)
    return InstallResult("codex", config_path, "removed", f"Codex 配置 {config_path}")
