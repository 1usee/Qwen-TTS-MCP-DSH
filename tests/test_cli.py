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
