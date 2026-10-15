import re
import pytest
from pathlib import Path
from qwen_voice_mcp.core import qwen_client as qc
from qwen_voice_mcp.core.errors import classify
from qwen_voice_mcp.core.model_registry import ModelRegistry
from qwen_voice_mcp.core.models import Status


@pytest.mark.parametrize("name", [
    "用户自定义音色", "My Voice", "UPPER", "a" * 40, "音色-2026", "", "  ", "voice_with_underscore",
])
def test_make_voice_prefix_is_always_legal(name):
    p = qc.make_voice_prefix(name)
    assert re.fullmatch(r"[a-z0-9]+", p), p
    assert len(p) < 10, p


def test_make_voice_prefix_is_deterministic_for_same_name():
    assert qc.make_voice_prefix("Long Xiao Chun") == qc.make_voice_prefix("Long Xiao Chun")


def test_make_voice_prefix_differs_across_similar_names():
    assert qc.make_voice_prefix("voice one") != qc.make_voice_prefix("voice two")


def test_format_map_covers_documented_formats():
    for fmt in ("mp3", "wav", "pcm", "opus"):
        assert fmt in qc.FORMAT_MAP


def test_unknown_output_format_is_invalid_input(tmp_path):
    c = qc.QwenClient("sk-x", ModelRegistry())
    r = c.synthesize("hi", "cosyvoice-v2", "longanyang", "zzz", tmp_path / "o.zzz")
    assert r.status is Status.INVALID_INPUT


def test_classify_recognises_auth_error_by_status_code():
    class E(Exception):
        status_code = 401
    assert classify(E("nope"))[0] is Status.AUTH_ERROR


def test_classify_recognises_rate_limit():
    class E(Exception):
        status_code = 429
    assert classify(E("slow down"))[0] is Status.RATE_LIMITED


def test_classify_recognises_timeout():
    import httpx
    assert classify(httpx.ConnectTimeout("t"))[0] is Status.NETWORK_ERROR


def test_classify_unknown_exception_is_upstream_error():
    assert classify(ValueError("???"))[0] is Status.UPSTREAM_ERROR


def test_classify_never_leaks_the_api_key():
    key = "sk-secretsecretsecret1234567890"
    status, msg = classify(RuntimeError(f"failed with {key}"))
    assert key not in msg


def test_synthesize_success_writes_file_and_reports_duration(tmp_path, monkeypatch):
    class FakeSynth:
        def __init__(self, **kw): self.kw = kw
        def call(self, text, timeout_millis=None): return b"\xff\xfb\x90\x00" + b"\x00" * 4000
    monkeypatch.setattr(qc, "SpeechSynthesizer", FakeSynth, raising=False)
    monkeypatch.setattr(qc, "_audio_format_for", lambda fmt: "MP3", raising=False)
    out = tmp_path / "o.mp3"
    r = qc.QwenClient("sk-x", ModelRegistry()).synthesize("你好", "cosyvoice-v2", "longanyang", "mp3", out)
    assert r.status is Status.SUCCESS
    assert out.exists() and r.data["audio_path"] == str(out)
    assert r.data["text_length"] == 2 and r.data["model_used"] == "cosyvoice-v2"


def test_synthesize_empty_result_is_upstream_error(tmp_path, monkeypatch):
    class FakeSynth:
        def __init__(self, **kw): pass
        def call(self, text, timeout_millis=None): return b""
    monkeypatch.setattr(qc, "SpeechSynthesizer", FakeSynth, raising=False)
    monkeypatch.setattr(qc, "_audio_format_for", lambda fmt: "MP3", raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).synthesize("hi", "cosyvoice-v2", "v", "mp3", tmp_path / "o.mp3")
    assert r.status is not Status.SUCCESS


def test_synthesize_maps_sdk_exception(tmp_path, monkeypatch):
    class Boom(Exception):
        status_code = 429
    class FakeSynth:
        def __init__(self, **kw): pass
        def call(self, text, timeout_millis=None): raise Boom("throttled")
    monkeypatch.setattr(qc, "SpeechSynthesizer", FakeSynth, raising=False)
    monkeypatch.setattr(qc, "_audio_format_for", lambda fmt: "MP3", raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).synthesize("hi", "cosyvoice-v2", "v", "mp3", tmp_path / "o.mp3")
    assert r.status is Status.RATE_LIMITED


def test_missing_api_key_reports_auth_error(tmp_path):
    r = qc.QwenClient(None, ModelRegistry()).synthesize("hi", "cosyvoice-v2", "v", "mp3", tmp_path / "o.mp3")
    assert r.status is Status.AUTH_ERROR


def test_resolve_api_key_prefers_explicit_then_env(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env")
    assert qc.QwenClient("sk-arg", ModelRegistry()).resolve_api_key() == "sk-arg"
    assert qc.QwenClient(None, ModelRegistry()).resolve_api_key() == "sk-env"


def test_clone_voice_success_shape(tmp_path, monkeypatch):
    class FakeVES:
        def __init__(self, **kw): pass
        def create_voice(self, target_model, prefix, url, language_hints=None, **kw): return "voice-abc123"
        def query_voice(self, voice_id): return ["OK"]
    monkeypatch.setattr(qc, "VoiceEnrollmentService", FakeVES, raising=False)
    monkeypatch.setattr(qc.QwenClient, "upload_reference", lambda self, p: "https://f/x.wav", raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).clone_voice("https://f/x.wav", "cosyvoice-v2", "qwenvoic")
    assert r.status is Status.SUCCESS
    assert r.data["voice_id"] == "voice-abc123" and r.data["cloning_status"] == "completed"


def test_clone_voice_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    class Boom(Exception):
        status_code = 403
    class FakeVES:
        def __init__(self, **kw): pass
        def create_voice(self, *a, **k): raise Boom("denied")
    monkeypatch.setattr(qc, "VoiceEnrollmentService", FakeVES, raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).clone_voice("https://f/x.wav", "cosyvoice-v2", "qwenvoic")
    assert r.status is Status.AUTH_ERROR


def test_clone_voice_pending_status_is_reported_honestly(monkeypatch):
    class FakeVES:
        def __init__(self, **kw): pass
        def create_voice(self, *a, **k): return "voice-pending"
        def query_voice(self, voice_id): raise RuntimeError("still processing")
    monkeypatch.setattr(qc, "VoiceEnrollmentService", FakeVES, raising=False)
    monkeypatch.setattr(qc, "ENROLL_POLL_INTERVAL_SECONDS", 0, raising=False)
    r = qc.QwenClient("sk-x", ModelRegistry()).clone_voice("https://f/x.wav", "cosyvoice-v2", "qwenvoic")
    assert r.data.get("cloning_status") in {"pending", "processing", "unknown"}
