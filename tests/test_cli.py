"""CLI 层测试（spec §14 / plan Task 9）。

全部离线：不访问网络、不需要真实密钥。
"""

from __future__ import annotations

import pytest

from qwen_voice_mcp.cli import main


def test_selftest_exits_zero_and_lists_the_four_tools(config_home, capsys):
    assert main(["--selftest"]) == 0
    out = capsys.readouterr().out
    for name in ("list_voice_models", "synthesize_speech", "clone_voice", "check_balance"):
        assert name in out, f"{name} missing from selftest output"


def test_config_show_prints_effective_options(config_home, capsys):
    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "127.0.0.1" in out
    assert "8765" in out
    assert "cosyvoice-v2" in out


def test_config_set_then_show_roundtrip(config_home, capsys):
    assert main(["config", "set", "default_voice", "longxiaochun"]) == 0
    capsys.readouterr()
    assert main(["config", "show"]) == 0
    assert "longxiaochun" in capsys.readouterr().out


def test_config_set_persists_to_the_config_file(config_home, capsys):
    assert main(["config", "set", "port", "8123"]) == 0
    capsys.readouterr()
    assert main(["config", "show"]) == 0
    assert "8123" in capsys.readouterr().out


def test_config_set_rejects_unknown_key(config_home, capsys):
    assert main(["config", "set", "not_a_field", "1"]) == 2
    assert "未知配置项" in capsys.readouterr().err


def test_config_set_rejects_invalid_value(config_home, capsys):
    assert main(["config", "set", "port", "not-a-number"]) == 2
    assert "无效的值" in capsys.readouterr().err


def test_config_set_refuses_to_store_secrets_in_plaintext(config_home, capsys):
    assert main(["config", "set", "http_auth_token", "secret"]) == 2
    assert "敏感" in capsys.readouterr().err


def test_config_show_redacts_the_auth_token(config_home, capsys):
    """A token can only reach the config through the environment; show must mask it."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("QWEN_VOICE_HTTP_AUTH_TOKEN", "tok-abcdefghijklmno")
    try:
        assert main(["config", "show"]) == 0
    finally:
        monkeypatch.undo()
    out = capsys.readouterr().out
    assert "tok-abcdefghijklmno" not in out
    # redact() keeps a short leading hint plus the tail: tok-ab…lmno
    assert "tok-ab" in out and "lmno" in out


def test_balance_without_key_reports_unavailable(config_home, capsys):
    assert main(["balance"]) == 0
    out = capsys.readouterr().out
    assert "余额" in out


def test_verify_models_offline_marks_unverified(config_home, capsys):
    assert main(["verify-models"]) == 0
    out = capsys.readouterr().out
    assert "verified" in out
    assert "cosyvoice-v2" in out


def test_version_prints_a_version(config_home, capsys):
    assert main(["version"]) == 0
    assert "1.0.0" in capsys.readouterr().out


def test_gui_import_does_not_require_a_display():
    from qwen_voice_mcp.gui import config_window

    assert callable(config_window.launch)
    assert callable(config_window.ConfigWindow)


def test_cli_never_writes_protocol_data_to_stdout_on_selftest(config_home, capsys):
    """stdout must stay usable for MCP framing; the selftest summary is its only stdout."""
    assert main(["--selftest"]) == 0
    captured = capsys.readouterr()
    assert "SELFTEST OK" in captured.out


# ---------------------------------------------------------------------------
# Key entry: never on the command line by default, always persisted
#
# The old `config set-key <KEY>` forced the secret onto argv, which lands in
# shell history, the process list, and any agent transcript that ran it.
# ---------------------------------------------------------------------------

KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def offline_vault(monkeypatch):
    """Deterministic vault: no OS credential store, no pre-existing key."""
    monkeypatch.setenv("QWEN_VOICE_NO_KEYRING", "1")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("QWEN_VOICE_NO_DOTENV", "1")


def _stored_key() -> str | None:
    from qwen_voice_mcp.core.secret_manager import SecretManager

    return SecretManager().retrieve("DASHSCOPE_API_KEY")


def test_set_key_reads_stdin_without_echoing_the_secret(config_home, offline_vault, capsys):
    monkeypatch = pytest.MonkeyPatch()
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    try:
        assert main(["config", "set-key", "--stdin", "--no-verify"]) == 0
    finally:
        monkeypatch.undo()

    captured = capsys.readouterr()
    assert KEY not in captured.out and KEY not in captured.err
    assert _stored_key() == KEY


def test_set_key_still_accepts_a_positional_argument(config_home, offline_vault, capsys):
    assert main(["config", "set-key", KEY, "--no-verify"]) == 0
    assert _stored_key() == KEY
    assert KEY not in capsys.readouterr().out


def test_set_key_prompts_with_hidden_input_when_no_argument_is_given(
    config_home, offline_vault, monkeypatch
):
    seen: dict[str, object] = {}

    def fake_prompt(text, **kwargs):
        seen.update(kwargs)
        seen["text"] = text
        return KEY

    monkeypatch.setattr("qwen_voice_mcp.cli.typer.prompt", fake_prompt)
    monkeypatch.setattr("sys.stdin", _Tty())

    assert main(["config", "set-key", "--no-verify"]) == 0
    assert seen.get("hide_input") is True
    assert _stored_key() == KEY


class _Tty:
    """Minimal terminal stand-in: `isatty()` is what the CLI guards on."""

    def isatty(self) -> bool:
        return True

    def read(self) -> str:  # pragma: no cover - prompt path does not read it
        return ""


def test_set_key_refuses_to_wait_for_input_when_stdin_is_not_a_terminal(
    config_home, offline_vault, capsys
):
    """A non-interactive caller must get an error, not a hang on a hidden prompt."""
    import io
    import sys

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    try:
        assert main(["config", "set-key", "--no-verify"]) == 2
    finally:
        monkeypatch.undo()

    assert "--stdin" in capsys.readouterr().err
    assert _stored_key() is None


def test_set_key_rejects_an_empty_key(config_home, offline_vault, capsys):
    import io
    import sys

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    try:
        assert main(["config", "set-key", "--stdin", "--no-verify"]) == 2
    finally:
        monkeypatch.undo()
    assert "空" in capsys.readouterr().err
    assert _stored_key() is None


def test_set_key_does_not_store_a_key_that_fails_validation(
    config_home, offline_vault, monkeypatch, capsys
):
    monkeypatch.setattr("qwen_voice_mcp.cli._validate_key", lambda key: False)
    assert main(["config", "set-key", KEY]) == 1
    assert _stored_key() is None


# ---------------------------------------------------------------------------
# setup: the one command a first-time user runs
# ---------------------------------------------------------------------------


def test_setup_stores_the_key_and_prints_next_steps(config_home, offline_vault, capsys):
    import io
    import sys

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    try:
        assert main(["setup", "--stdin", "--no-verify"]) == 0
    finally:
        monkeypatch.undo()

    out = capsys.readouterr().out
    assert _stored_key() == KEY
    assert KEY not in out
    assert "install" in out  # tells the user how to wire up a client


def test_setup_skips_storing_when_verification_fails(
    config_home, offline_vault, monkeypatch, capsys
):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    monkeypatch.setattr("qwen_voice_mcp.cli._validate_key", lambda key: False)

    assert main(["setup", "--stdin"]) == 1
    assert _stored_key() is None
    assert "验证失败" in capsys.readouterr().err


def test_setup_overwrites_a_previous_key(config_home, offline_vault, monkeypatch):
    import io
    import sys

    for value in (KEY, "sk-new-key-000000000000000000"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(value + "\n"))
        assert main(["setup", "--stdin", "--no-verify"]) == 0

    assert _stored_key() == "sk-new-key-000000000000000000"
    assert len(SecretManager_names()) == 1


def SecretManager_names() -> list[str]:
    from qwen_voice_mcp.core.secret_manager import SecretManager

    return SecretManager().list_names()


# ---------------------------------------------------------------------------
# doctor: make "installed but the tool does not show up" debuggable
# ---------------------------------------------------------------------------


def test_doctor_reports_the_key_without_leaking_it(config_home, offline_vault, monkeypatch, capsys):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    assert main(["setup", "--stdin", "--no-verify"]) == 0

    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert KEY not in out
    assert "已配置" in out


def test_doctor_exits_zero_and_explains_a_missing_key(config_home, offline_vault, capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "未配置" in out
    assert "setup" in out  # tells the user the fix


def test_doctor_reports_the_vault_protection_tier(config_home, offline_vault, monkeypatch, capsys):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    main(["setup", "--stdin", "--no-verify"])
    capsys.readouterr()

    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "keyfile" in out or "本机密钥文件" in out


# ---------------------------------------------------------------------------
# print-config: the zero-risk path when a client cannot be auto-configured
# ---------------------------------------------------------------------------


def test_print_config_claude_is_pasteable_json(capsys):
    import json

    assert main(["print-config", "--client", "claude"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mcpServers"]["qwen-voice"]["command"] == "qwen-voice-mcp"


def test_print_config_codex_is_pasteable_toml(capsys):
    import tomllib

    assert main(["print-config", "--client", "codex"]) == 0
    parsed = tomllib.loads(capsys.readouterr().out)
    assert parsed["mcp_servers"]["qwen-voice"]["command"] == "qwen-voice-mcp"


def test_print_config_dsh_is_yaml_without_a_key(capsys):
    assert main(["print-config", "--client", "dsh"]) == 0
    out = capsys.readouterr().out
    assert "mcp_servers:" in out
    assert "qwen-voice-mcp" in out
    assert "DASHSCOPE_API_KEY" not in out


def test_print_config_rejects_an_unknown_client(capsys):
    assert main(["print-config", "--client", "vscode"]) == 2
    assert "未知客户端" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# install: register the server with a real client, safely
# ---------------------------------------------------------------------------


def test_install_codex_writes_a_config_toml(config_home, tmp_path, capsys):
    import tomllib

    codex_home = tmp_path / "codex"
    assert main(["install", "--client", "codex", "--codex-home", str(codex_home)]) == 0

    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["qwen-voice"]["command"] == "qwen-voice-mcp"


def test_install_claude_writes_a_project_mcp_json(config_home, tmp_path, capsys):
    import json

    project = tmp_path / "proj"
    project.mkdir()
    assert main(["install", "--client", "claude", "--project-dir", str(project)]) == 0

    payload = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert payload["mcpServers"]["qwen-voice"]["command"] == "qwen-voice-mcp"


def test_install_never_writes_the_api_key(config_home, offline_vault, tmp_path, monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    assert main(["setup", "--stdin", "--no-verify"]) == 0

    codex_home = tmp_path / "codex"
    main(["install", "--client", "codex", "--codex-home", str(codex_home)])
    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert KEY not in text
    assert "DASHSCOPE_API_KEY" not in text


def test_install_dry_run_writes_nothing(config_home, tmp_path, capsys):
    codex_home = tmp_path / "codex"
    assert main(["install", "--client", "codex", "--codex-home", str(codex_home), "--dry-run"]) == 0
    assert not (codex_home / "config.toml").exists()
    assert "mcp_servers" in capsys.readouterr().out


def test_install_uninstall_removes_the_entry(config_home, tmp_path):
    import tomllib

    codex_home = tmp_path / "codex"
    main(["install", "--client", "codex", "--codex-home", str(codex_home)])
    assert main(["install", "--client", "codex", "--codex-home", str(codex_home), "--uninstall"]) == 0

    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert "qwen-voice" not in parsed.get("mcp_servers", {})


def test_install_accepts_a_comma_separated_client_list(config_home, tmp_path):
    import json
    import tomllib

    codex_home = tmp_path / "codex"
    project = tmp_path / "proj"
    project.mkdir()

    assert main([
        "install", "--client", "claude,codex",
        "--codex-home", str(codex_home), "--project-dir", str(project),
    ]) == 0

    assert json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]


def test_install_rejects_an_unknown_client(config_home, capsys):
    assert main(["install", "--client", "vscode"]) == 2
    assert "未知客户端" in capsys.readouterr().err
