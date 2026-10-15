"""音色别名存储测试（spec §11.1：``voice_name`` → ``voice_id`` 本地映射）。"""

from __future__ import annotations

import json

import pytest

from qwen_voice_mcp.core.models import Status, ToolResult
from qwen_voice_mcp.core.voice_store import VoiceAliasStore, default_voices_path


def test_default_path_lives_under_the_config_home(config_home):
    assert default_voices_path().parent == config_home
    assert default_voices_path().name == "voices.json"


def test_remember_then_resolve_roundtrip(tmp_path):
    store = VoiceAliasStore(tmp_path / "voices.json")
    store.remember("用户自定义音色", "voice-abc123", prefix="qv1234")
    assert store.resolve("用户自定义音色") == "voice-abc123"
    assert store.get("用户自定义音色")["prefix"] == "qv1234"


def test_unknown_name_resolves_to_none(tmp_path):
    assert VoiceAliasStore(tmp_path / "voices.json").resolve("没这个音色") is None


def test_mapping_is_persisted_and_reloaded(tmp_path):
    path = tmp_path / "voices.json"
    VoiceAliasStore(path).remember("音色一", "voice-1")
    reloaded = VoiceAliasStore(path)
    assert reloaded.resolve("音色一") == "voice-1"


def test_file_is_valid_utf8_json_with_readable_names(tmp_path):
    path = tmp_path / "voices.json"
    VoiceAliasStore(path).remember("中文名", "voice-9")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["中文名"]["voice_id"] == "voice-9"


def test_missing_file_is_not_an_error(tmp_path):
    assert VoiceAliasStore(tmp_path / "absent.json").all() == {}


def test_corrupt_file_degrades_to_empty_instead_of_raising(tmp_path):
    path = tmp_path / "voices.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert VoiceAliasStore(path).all() == {}


def test_non_object_payload_degrades_to_empty(tmp_path):
    path = tmp_path / "voices.json"
    path.write_text('["nope"]', encoding="utf-8")
    assert VoiceAliasStore(path).all() == {}


def test_records_without_a_voice_id_are_ignored(tmp_path):
    path = tmp_path / "voices.json"
    path.write_text(json.dumps({"bad": {"prefix": "x"}}), encoding="utf-8")
    assert VoiceAliasStore(path).all() == {}


def test_remember_ignores_empty_arguments(tmp_path):
    store = VoiceAliasStore(tmp_path / "voices.json")
    store.remember("", "voice-1")
    store.remember("name", "")
    assert store.all() == {}


def test_forget_removes_and_persists(tmp_path):
    path = tmp_path / "voices.json"
    store = VoiceAliasStore(path)
    store.remember("音色一", "voice-1")
    assert store.forget("音色一") is True
    assert store.forget("音色一") is False
    assert VoiceAliasStore(path).all() == {}


def test_all_returns_a_copy_not_the_internal_dict(tmp_path):
    store = VoiceAliasStore(tmp_path / "voices.json")
    store.remember("音色一", "voice-1")
    snapshot = store.all()
    snapshot["音色一"]["voice_id"] = "tampered"
    assert store.resolve("音色一") == "voice-1"


# --------------------------------------------------------------------------- #
# Wiring: clone_voice must persist the mapping (Task 8 flagged this as a gap)
# --------------------------------------------------------------------------- #


class _RecordingStore:
    def __init__(self):
        self.calls = []

    def remember(self, voice_name, voice_id, **extra):
        self.calls.append((voice_name, voice_id, extra))


class _FailingStore:
    def remember(self, *args, **kwargs):
        raise OSError("disk full")


class _CloneClient:
    def upload_reference(self, path):
        return "https://example.invalid/ref.wav"

    def clone_voice(self, url, target_model, prefix, language_hints=None):
        return ToolResult(
            Status.SUCCESS,
            "声音克隆完成",
            {"voice_id": "voice-xyz", "voice_prefix": prefix, "cloning_status": "completed"},
        )


class _PassChecker:
    def gate(self, estimated_cost, strict):
        return None

    def warn_if_tight(self, estimated_cost):
        return []


def _services(config_home, tmp_path, store):
    from qwen_voice_mcp.core import audio_utils
    from qwen_voice_mcp.core.config_store import load_options
    from qwen_voice_mcp.core.services import VoiceServices

    wav = audio_utils.make_tone_wav(tmp_path / "ref.wav", seconds=10)
    services = VoiceServices(
        load_options(),
        client=_CloneClient(),
        checker=_PassChecker(),
        secret_manager=None,
        voices=store,
    )
    return services, str(wav)


def test_clone_persists_the_name_to_voice_id_mapping(config_home, tmp_path):
    store = _RecordingStore()
    services, wav = _services(config_home, tmp_path, store)
    result = services.clone_voice(wav, "用户自定义音色")

    assert result.status is Status.SUCCESS
    assert result.data["voice_alias_saved"] is True
    assert store.calls and store.calls[0][0] == "用户自定义音色"
    assert store.calls[0][1] == "voice-xyz"


def test_alias_store_failure_does_not_break_a_successful_clone(config_home, tmp_path):
    services, wav = _services(config_home, tmp_path, _FailingStore())
    result = services.clone_voice(wav, "音色一")

    assert result.status is Status.SUCCESS
    assert result.data["voice_alias_saved"] is False
    assert any("别名" in w for w in result.warnings)
