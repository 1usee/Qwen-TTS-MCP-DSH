"""Startup credential resolution.

The unit tests for ``SecretManager`` prove the vault works in isolation. What a
first-time user actually depends on is the *join*: they ran ``setup`` once, set
no environment variable, and the MCP server process must still find the key.

That join lives in ``server._default_secret_manager`` → ``load_into_env``. Before
the key-file fallback existed, it silently degraded to "no key configured" on any
host whose keyring is unavailable (WSL, Docker, CI) — the exact population that
cannot debug it.
"""

from __future__ import annotations

import os

import pytest

from qwen_voice_mcp.core.secret_manager import SecretManager

KEY = "sk-from-vault-abcdefghijklmnop"


@pytest.fixture
def headless(monkeypatch):
    """No credential store, no env var, no dotenv — the worst realistic host."""
    import keyring

    def boom(*args, **kwargs):
        raise RuntimeError("keyring backend unavailable")

    monkeypatch.setattr(keyring, "get_password", boom)
    monkeypatch.setattr(keyring, "set_password", boom)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("QWEN_VOICE_NO_DOTENV", "1")
    return boom


def test_server_startup_injects_the_stored_key_without_any_env_var(config_home, headless):
    """`setup` once → every client process finds the key, with nothing exported."""
    SecretManager().store("DASHSCOPE_API_KEY", KEY)
    os.environ.pop("DASHSCOPE_API_KEY", None)

    from qwen_voice_mcp.server import _default_secret_manager

    _default_secret_manager()

    assert os.environ.get("DASHSCOPE_API_KEY") == KEY


def test_environment_still_wins_over_the_stored_key(config_home, headless, monkeypatch):
    """An explicitly exported key must not be silently replaced by the vault."""
    SecretManager().store("DASHSCOPE_API_KEY", KEY)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-explicit")

    from qwen_voice_mcp.server import _default_secret_manager

    _default_secret_manager()

    assert os.environ["DASHSCOPE_API_KEY"] == "sk-explicit"


def test_a_broken_vault_does_not_prevent_startup(config_home, headless):
    """A corrupt vault means "no stored key", never "the server will not start"."""
    vault = config_home / "vault.enc"
    vault.write_text("{ this is not a vault", encoding="utf-8")

    from qwen_voice_mcp.server import _default_secret_manager

    manager = _default_secret_manager()

    assert manager is not None
    assert os.environ.get("DASHSCOPE_API_KEY") is None
