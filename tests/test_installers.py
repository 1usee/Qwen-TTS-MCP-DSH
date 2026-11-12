"""Tests for the Claude Code / Codex registration module.

Every case is offline and touches only ``tmp_path``.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from qwen_voice_mcp.installers import (
    SERVER_ARGS,
    SERVER_COMMAND,
    SERVER_NAME,
    InstallError,
    claude_mcp_json,
    claude_server_entry,
    codex_config_path,
    codex_server_values,
    codex_toml_snippet,
    install_claude,
    install_codex,
    merge_claude_json,
    merge_codex_toml,
    uninstall_claude,
    uninstall_codex,
)


# ---------------------------------------------------------------------------
# Payloads: portable, key-free, quoting-safe
# ---------------------------------------------------------------------------


def test_claude_entry_uses_the_installed_console_script():
    entry = claude_server_entry()
    assert entry == {"command": SERVER_COMMAND, "args": list(SERVER_ARGS)}
    assert "DASHSCOPE_API_KEY" not in entry


def test_claude_payload_round_trips_as_json():
    document = json.loads(claude_mcp_json())
    assert document["mcpServers"][SERVER_NAME]["command"] == SERVER_COMMAND


def test_codex_snippet_quotes_windows_paths_correctly():
    """A backslash in a value must not produce invalid TOML."""
    values = codex_server_values()
    assert values["command"] == SERVER_COMMAND

    snippet = codex_toml_snippet()
    parsed = tomllib.loads(snippet)
    assert parsed["mcp_servers"][SERVER_NAME] == values


def test_codex_config_path_honours_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert codex_config_path() == tmp_path / "cx" / "config.toml"


def test_codex_config_path_explicit_home_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ignored"))
    assert codex_config_path(tmp_path / "explicit") == tmp_path / "explicit" / "config.toml"


# ---------------------------------------------------------------------------
# Claude Code: .mcp.json
# ---------------------------------------------------------------------------


def test_install_claude_writes_a_parseable_project_config(tmp_path):
    target = tmp_path / ".mcp.json"
    result = install_claude(target)

    assert result.changed
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["mcpServers"][SERVER_NAME]["command"] == SERVER_COMMAND
    assert "DASHSCOPE_API_KEY" not in target.read_text(encoding="utf-8")


def test_install_claude_preserves_other_servers(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other-server"}}}), encoding="utf-8"
    )

    install_claude(target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["other"] == {"command": "other-server"}
    assert payload["mcpServers"][SERVER_NAME]["command"] == SERVER_COMMAND


def test_install_claude_is_idempotent(tmp_path):
    target = tmp_path / ".mcp.json"
    install_claude(target)
    first = target.read_text(encoding="utf-8")

    result = install_claude(target)

    assert result.action == "unchanged"
    assert target.read_text(encoding="utf-8") == first


def test_install_claude_updates_a_stale_entry(tmp_path):
    """An entry left by an older version (absolute interpreter path) is replaced."""
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps(
            {"mcpServers": {SERVER_NAME: {"command": "D:\\old\\python.exe", "args": ["x"]}}}
        ),
        encoding="utf-8",
    )

    install_claude(target)

    entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    assert entry == {"command": SERVER_COMMAND, "args": list(SERVER_ARGS)}


def test_install_claude_dry_run_does_not_touch_the_file(tmp_path):
    target = tmp_path / ".mcp.json"
    result = install_claude(target, dry_run=True)

    assert result.action == "preview"
    assert not target.exists()


def test_install_claude_refuses_to_clobber_broken_json(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(InstallError):
        install_claude(target)
    assert target.read_text(encoding="utf-8") == "{not json"


def test_uninstall_claude_removes_only_our_entry(tmp_path):
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "keep"}, SERVER_NAME: {}}}),
        encoding="utf-8",
    )

    result = uninstall_claude(target)

    assert result.action == "removed"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert SERVER_NAME not in payload["mcpServers"]
    assert payload["mcpServers"]["other"] == {"command": "keep"}


def test_uninstall_claude_on_a_missing_file_is_a_noop(tmp_path):
    result = uninstall_claude(tmp_path / ".mcp.json")
    assert result.action == "unchanged"


def test_merge_claude_json_keeps_unknown_top_level_keys():
    text = json.dumps({"mcpServers": {}, "someOtherTool": {"a": 1}})
    payload = json.loads(merge_claude_json(text))
    assert payload["someOtherTool"] == {"a": 1}


# ---------------------------------------------------------------------------
# Codex: config.toml
# ---------------------------------------------------------------------------


def test_install_codex_creates_the_config_file(tmp_path):
    target = tmp_path / "config.toml"
    result = install_codex(target)

    assert result.changed
    parsed = tomllib.loads(target.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"][SERVER_NAME]["command"] == SERVER_COMMAND


def test_install_codex_preserves_comments_and_unrelated_sections(tmp_path):
    target = tmp_path / "config.toml"
    original = (
        "# my hand-written config\n"
        'model = "gpt-5-codex"   # keep this comment\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "other-server"\n'
        "args = []\n"
    )
    target.write_text(original, encoding="utf-8")

    install_codex(target)

    text = target.read_text(encoding="utf-8")
    assert "# my hand-written config" in text
    assert "# keep this comment" in text
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5-codex"
    assert parsed["mcp_servers"]["other"]["command"] == "other-server"
    assert parsed["mcp_servers"][SERVER_NAME]["command"] == SERVER_COMMAND


def test_install_codex_does_not_duplicate_the_table(tmp_path):
    target = tmp_path / "config.toml"
    install_codex(target)
    install_codex(target)

    text = target.read_text(encoding="utf-8")
    assert text.count(f"[mcp_servers.{SERVER_NAME}]") == 1
    tomllib.loads(text)  # a duplicate table would have raised


def test_install_codex_is_idempotent(tmp_path):
    target = tmp_path / "config.toml"
    install_codex(target)
    first = target.read_text(encoding="utf-8")

    result = install_codex(target)

    assert result.action == "unchanged"
    assert target.read_text(encoding="utf-8") == first


def test_install_codex_replaces_an_existing_entry(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        f'[mcp_servers.{SERVER_NAME}]\ncommand = "old"\nargs = ["old"]\n', encoding="utf-8"
    )

    install_codex(target)

    parsed = tomllib.loads(target.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"][SERVER_NAME] == codex_server_values()


def test_install_codex_handles_a_file_without_a_trailing_newline(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text('model = "x"', encoding="utf-8")

    install_codex(target)

    parsed = tomllib.loads(target.read_text(encoding="utf-8"))
    assert parsed["model"] == "x"
    assert parsed["mcp_servers"][SERVER_NAME]["command"] == SERVER_COMMAND


def test_install_codex_dry_run_writes_nothing(tmp_path):
    target = tmp_path / "config.toml"
    result = install_codex(target, dry_run=True)

    assert result.action == "preview"
    assert not target.exists()


def test_install_codex_refuses_to_clobber_broken_toml(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("[unclosed\n", encoding="utf-8")

    with pytest.raises(InstallError):
        install_codex(target)
    assert target.read_text(encoding="utf-8") == "[unclosed\n"


def test_uninstall_codex_removes_the_table_and_keeps_the_rest(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        'model = "x"\n\n[mcp_servers.other]\ncommand = "keep"\n', encoding="utf-8"
    )
    install_codex(target)

    result = uninstall_codex(target)

    assert result.action == "removed"
    parsed = tomllib.loads(target.read_text(encoding="utf-8"))
    assert SERVER_NAME not in parsed.get("mcp_servers", {})
    assert parsed["mcp_servers"]["other"]["command"] == "keep"
    assert parsed["model"] == "x"


def test_uninstall_codex_on_a_missing_file_is_a_noop(tmp_path):
    result = uninstall_codex(tmp_path / "config.toml")
    assert result.action == "unchanged"


def test_merge_codex_toml_rejects_documents_it_cannot_verify():
    """The verifier must be able to veto a merge (proves it is not vacuous)."""
    with pytest.raises(InstallError):
        merge_codex_toml("[mcp_servers.qwen-voice]\ncommand = 1\n\n[[broken\n")
