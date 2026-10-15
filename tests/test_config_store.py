# tests/test_config_store.py
import json
import pytest
from pathlib import Path
from qwen_voice_mcp.core.config_store import Options, config_home, load_options, save_options

# The `config_home` fixture shadows the `config_home()` function of the same name
# inside each test's namespace, so the accessor is aliased for the one test that
# has to call it. (Fixture is already exercised as a path by every other test.)
qv_config_home = config_home


def test_defaults_match_spec(config_home):
    o = load_options()
    assert (o.host, o.port, o.default_voice) == ("127.0.0.1", 8765, "longanyang")
    assert (o.balance_threshold, o.strict_balance) == (1.00, False)
    assert (o.audio_min_seconds, o.audio_max_seconds) == (5.0, 60.0)
    assert o.audio_max_bytes == 20 * 1024 * 1024


def test_file_beats_defaults(config_home):
    (config_home / "config.json").write_text(json.dumps({"port": 9999}), encoding="utf-8")
    assert load_options().port == 9999


def test_env_beats_file(config_home, monkeypatch):
    (config_home / "config.json").write_text(json.dumps({"port": 9999}), encoding="utf-8")
    monkeypatch.setenv("QWEN_VOICE_PORT", "7777")
    assert load_options().port == 7777


def test_cli_overrides_beat_env(config_home, monkeypatch):
    monkeypatch.setenv("QWEN_VOICE_PORT", "7777")
    assert load_options(port=6666).port == 6666


def test_roundtrip_save_then_load(config_home):
    save_options(load_options(default_voice="longxiaochun", port=8123))
    o = load_options()
    assert (o.default_voice, o.port) == ("longxiaochun", 8123)
    assert (config_home / "config.json").exists()


def test_bad_range_rejected(config_home):
    with pytest.raises(Exception):
        Options(audio_min_seconds=90.0, audio_max_seconds=10.0)


def test_port_out_of_range_rejected(config_home):
    with pytest.raises(Exception):
        Options(port=70000)


def test_non_loopback_rejected_by_default(config_home):
    with pytest.raises(ValueError):
        load_options(host="0.0.0.0").reject_non_loopback()


def test_non_loopback_allowed_when_opted_in(config_home):
    load_options(host="0.0.0.0", allow_non_loopback=True).reject_non_loopback()


def test_config_home_honours_env(config_home):
    assert qv_config_home() == config_home
