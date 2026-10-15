"""Task 8 — ``core/services.py``: the single assembly point and the single gate.

The tests here pin the spec's hard constraint (spec §7.2, plan "Review Focus" 4):
the balance gate runs **before** any billable call, and invalid input is rejected
before the gate is even consulted.  When the gate blocks, the DashScope-facing
client must receive exactly zero calls.

Everything is offline: the client and the checker are fakes, and
``secret_manager=None`` means "never touch the vault".
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import pytest

from qwen_voice_mcp.core import audio_utils as au
from qwen_voice_mcp.core import services as sv
from qwen_voice_mcp.core.config_store import load_options
from qwen_voice_mcp.core.models import Status, ToolResult


class FakeChecker:
    """Records gate calls so the ordering assertions below mean something."""

    def __init__(self, block=None, warnings=None):
        self.block = block
        self.warnings = list(warnings or [])
        self.gate_calls: list[tuple[float, bool]] = []

    def gate(self, estimated_cost, strict):
        self.gate_calls.append((estimated_cost, strict))
        return self.block

    def warn_if_tight(self, estimated_cost):
        return list(self.warnings)

    def as_tool_result(self):
        return ToolResult(Status.SUCCESS, "余额 ¥12.50", {"balance": 12.5})


class FakeClient:
    """Stands in for ``QwenClient``; records every call it receives."""

    def __init__(self, result=None, clone=None):
        self.calls: list[tuple] = []
        self.result = result or ToolResult(
            Status.SUCCESS, "合成完成", {"audio_path": "x.mp3", "text_length": 2}
        )
        self.clone = clone or ToolResult(Status.SUCCESS, "克隆完成", {"voice_id": "v1"})
        self.uploaded: list[str] = []

    def synthesize(self, text, model, voice, fmt, path):
        self.calls.append(("synthesize", text, model, voice, fmt, str(path)))
        return self.result

    def clone_voice(self, url, target_model, prefix, language_hints=None):
        self.calls.append(("clone", url, target_model, prefix, language_hints))
        return self.clone

    def upload_reference(self, path):
        self.uploaded.append(str(path))
        return "https://f/ref.wav"


def build(config_home, checker=None, client=None, **overrides):
    """Assemble a fully injected ``VoiceServices`` (no network, no vault)."""
    return sv.VoiceServices(
        load_options(**overrides),
        client=client or FakeClient(),
        checker=checker or FakeChecker(),
        secret_manager=None,
    )


# ---------------------------------------------------------------------------
# Hard constraint: the gate runs first
# ---------------------------------------------------------------------------
def test_billable_call_is_blocked_without_touching_the_api(config_home):
    blk = ToolResult(Status.INSUFFICIENT_BALANCE, "余额不足")
    client = FakeClient()
    r = build(config_home, FakeChecker(block=blk), client).synthesize_speech("你好")
    assert r.status is Status.INSUFFICIENT_BALANCE
    assert client.calls == [], "the API must not be called when the gate blocks"


def test_clone_is_blocked_without_uploading(config_home, tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10)
    blk = ToolResult(Status.INSUFFICIENT_BALANCE, "余额不足")
    client = FakeClient()
    r = build(config_home, FakeChecker(block=blk), client).clone_voice(str(wav), "音色一")
    assert r.status is Status.INSUFFICIENT_BALANCE
    assert client.uploaded == []


def test_gate_runs_before_the_client_and_receives_the_estimated_cost(config_home):
    order: list[str] = []

    class OrderedChecker(FakeChecker):
        def gate(self, estimated_cost, strict):
            order.append("gate")
            return super().gate(estimated_cost, strict)

    class OrderedClient(FakeClient):
        def synthesize(self, text, model, voice, fmt, path):
            order.append("client")
            return super().synthesize(text, model, voice, fmt, path)

    checker, client = OrderedChecker(), OrderedClient()
    r = build(config_home, checker, client).synthesize_speech("你好世界")
    assert r.status is Status.SUCCESS
    assert order == ["gate", "client"]
    # 0.26 CNY / 1k chars * 4 chars, rounded to 4 decimals by the registry.
    assert checker.gate_calls == [(0.001, False)]


def test_gate_receives_the_configured_strict_flag(config_home):
    checker = FakeChecker()
    build(config_home, checker, strict_balance=True).synthesize_speech("你好")
    assert checker.gate_calls == [(0.0005, True)]


def test_both_billable_tools_funnel_through_ensure_balance(config_home, tmp_path, monkeypatch):
    seen: list[float] = []

    def fake_ensure_balance(self, estimated_cost, strict=None):
        seen.append(estimated_cost)
        return None

    monkeypatch.setattr(sv.VoiceServices, "ensure_balance", fake_ensure_balance)
    services = build(config_home)
    assert services.synthesize_speech("你好").status is Status.SUCCESS
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10)
    assert services.clone_voice(str(wav), "音色一").status is Status.SUCCESS
    assert seen == [0.0005, 0.20]


# ---------------------------------------------------------------------------
# Invalid input is rejected before the gate
# ---------------------------------------------------------------------------
def test_empty_text_is_invalid_input_and_never_bills(config_home):
    checker, client = FakeChecker(), FakeClient()
    for bad in ("", "   ", "\n\t"):
        r = build(config_home, checker, client).synthesize_speech(bad)
        assert r.status is Status.INVALID_INPUT
    assert client.calls == []
    assert checker.gate_calls == [], "empty text must not trigger a balance check"


def test_unknown_model_is_model_not_supported(config_home):
    checker = FakeChecker()
    r = build(config_home, checker).synthesize_speech("hi", model="does-not-exist")
    assert r.status is Status.MODEL_NOT_SUPPORTED
    assert checker.gate_calls == [], "an unknown model must not trigger a balance check"


def test_clone_unknown_model_is_model_not_supported(config_home, tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10)
    client = FakeClient()
    r = build(config_home, client=client).clone_voice(str(wav), "音色一", model="nope")
    assert r.status is Status.MODEL_NOT_SUPPORTED
    assert client.uploaded == []


# ---------------------------------------------------------------------------
# Successful paths
# ---------------------------------------------------------------------------
def test_warnings_from_checker_are_attached(config_home):
    r = build(config_home, FakeChecker(warnings=["余额偏低"])).synthesize_speech("你好")
    assert r.status is Status.SUCCESS and "余额偏低" in r.warnings


def test_synthesize_passes_resolved_defaults_and_reports_cost(config_home):
    client = FakeClient()
    r = build(config_home, client=client).synthesize_speech("你好世界")
    assert r.status is Status.SUCCESS
    kind, text, model, voice, fmt, path = client.calls[0]
    assert (kind, text, model, voice, fmt) == (
        "synthesize",
        "你好世界",
        "cosyvoice-v2",
        "longanyang",
        "mp3",
    )
    assert Path(path).parent == load_options().output_dir
    assert r.data["estimated_cost"] == 0.001


def test_synthesize_honours_explicit_model_voice_and_format(config_home):
    client = FakeClient()
    build(config_home, client=client).synthesize_speech(
        "hi", model="qwen-tts", voice="longxiaochun", output_format="wav"
    )
    assert client.calls[0][2:5] == ("qwen-tts", "longxiaochun", "wav")


def test_failed_client_result_is_returned_unchanged(config_home):
    failure = ToolResult(Status.AUTH_ERROR, "未配置 DashScope API Key")
    r = build(config_home, client=FakeClient(result=failure)).synthesize_speech("你好")
    assert r.status is Status.AUTH_ERROR and r.message == "未配置 DashScope API Key"


def test_clone_accepts_base64_input(config_home, tmp_path):
    raw = au.make_tone_wav(tmp_path / "s.wav", seconds=10).read_bytes()
    r = build(config_home).clone_voice(base64.b64encode(raw).decode(), "音色一")
    assert r.status is Status.SUCCESS


def test_clone_accepts_data_uri_input(config_home, tmp_path):
    raw = au.make_tone_wav(tmp_path / "s.wav", seconds=10).read_bytes()
    uri = "data:audio/wav;base64," + base64.b64encode(raw).decode()
    assert build(config_home).clone_voice(uri, "音色一").status is Status.SUCCESS


def test_clone_passes_a_legal_prefix_and_round_trips_the_alias(config_home, tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10)
    client = FakeClient()
    r = build(config_home, client=client).clone_voice(str(wav), "用户自定义音色")
    assert r.status is Status.SUCCESS
    kind, url, model, prefix, hints = client.calls[0]
    assert kind == "clone" and url == "https://f/ref.wav" and model == "cosyvoice-v2"
    assert re.fullmatch(r"[a-z0-9]+", prefix) and len(prefix) < 10
    assert hints is None
    assert r.data["voice_name"] == "用户自定义音色"
    assert r.data["voice_prefix"] == prefix


def test_clone_forwards_language_hints(config_home, tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10)
    client = FakeClient()
    build(config_home, client=client).clone_voice(str(wav), "音色一", language_hints=["zh"])
    assert client.calls[0][4] == ["zh"]


def test_clone_empty_voice_name_is_invalid_input(config_home, tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10)
    client = FakeClient()
    r = build(config_home, client=client).clone_voice(str(wav), "  ")
    assert r.status is Status.INVALID_INPUT
    assert client.uploaded == []


# ---------------------------------------------------------------------------
# Reference-audio validation happens before upload (plan Review Focus)
# ---------------------------------------------------------------------------
def test_clone_rejects_illegal_audio_before_upload(config_home, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"nope" * 100)
    client = FakeClient()
    r = build(config_home, client=client).clone_voice(str(bad), "音色一")
    assert r.status is Status.INVALID_INPUT
    assert client.uploaded == []


def test_clone_rejects_too_short_audio_before_upload(config_home, tmp_path):
    short = au.make_tone_wav(tmp_path / "short.wav", seconds=2)
    client = FakeClient()
    r = build(config_home, client=client).clone_voice(str(short), "音色一")
    assert r.status is Status.INVALID_INPUT
    assert client.uploaded == []


def test_clone_rejects_garbage_base64_before_upload(config_home):
    client = FakeClient()
    r = build(config_home, client=client).clone_voice("!!!not base64 and not a path!!!", "音色一")
    assert r.status is Status.INVALID_INPUT
    assert client.uploaded == []


def test_clone_attaches_checker_and_reference_warnings(config_home, tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10, sample_rate=96000)
    r = build(config_home, FakeChecker(warnings=["余额偏低"]), client=FakeClient()).clone_voice(
        str(wav), "音色一"
    )
    assert r.status is Status.SUCCESS
    assert "余额偏低" in r.warnings
    assert any("采样率" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# Read-only tool and internal errors
# ---------------------------------------------------------------------------
def test_list_models_reports_declared_not_verified(config_home):
    d = build(config_home).list_voice_models().to_dict()
    assert d["status"] == "success" and d["count"] == len(d["models"])
    assert all(m["verified"] is None for m in d["models"])
    assert d["default_model"]


def test_check_balance_is_read_only_success(config_home):
    class C:
        def as_tool_result(self):
            return ToolResult(Status.SUCCESS, "余额 ¥12.50", {"balance": 12.5})

    s = sv.VoiceServices(load_options(), client=FakeClient(), checker=C(), secret_manager=None)
    assert s.check_balance().status is Status.SUCCESS


def test_check_balance_without_a_usable_checker_is_internal_error(config_home):
    """Even a broken checker must not propagate out of a public service method."""

    class Broken:
        pass

    s = sv.VoiceServices(load_options(), client=FakeClient(), checker=Broken(), secret_manager=None)
    r = s.check_balance()
    assert r.status is Status.INTERNAL_ERROR
    assert r.message == sv.INTERNAL_ERROR_MESSAGE


def test_internal_exception_becomes_internal_error_not_a_crash(config_home):
    class Boom(FakeClient):
        def synthesize(self, *a, **k):
            raise RuntimeError("kaboom")

    r = build(config_home, client=Boom()).synthesize_speech("你好")
    assert r.status is Status.INTERNAL_ERROR
    assert "kaboom" not in r.message


def test_internal_exception_is_logged_but_never_returned(config_home):
    class Boom(FakeClient):
        def synthesize(self, *a, **k):
            raise RuntimeError("kaboom-secret-detail")

    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):  # noqa: D102 - test double
            captured.append(logging.Formatter().format(record))

    logger = logging.getLogger("qwen_voice_mcp.core.services")
    handler = Capture()
    logger.addHandler(handler)
    try:
        r = build(config_home, client=Boom()).synthesize_speech("你好")
    finally:
        logger.removeHandler(handler)

    assert r.status is Status.INTERNAL_ERROR
    assert "kaboom-secret-detail" not in r.message
    assert any("kaboom-secret-detail" in text for text in captured), captured


def test_internal_error_when_upload_fails_unexpectedly(config_home, tmp_path):
    class Boom(FakeClient):
        def upload_reference(self, path):
            raise RuntimeError("upload exploded")

    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=10)
    r = build(config_home, client=Boom()).clone_voice(str(wav), "音色一")
    assert r.status is Status.INTERNAL_ERROR
    assert "exploded" not in r.message


def test_ensure_balance_delegates_to_the_checker(config_home):
    blocked = ToolResult(Status.BALANCE_UNAVAILABLE, "余额未知")
    strict_services = build(config_home, FakeChecker(block=blocked))
    assert strict_services.ensure_balance(0.1, True) is blocked

    permissive = FakeChecker()
    open_services = build(config_home, permissive)
    assert open_services.ensure_balance(0.1, False) is None
    assert permissive.gate_calls == [(0.1, False)]
    assert strict_services.checker.gate_calls == [(0.1, True)]


@pytest.mark.parametrize("bad", [None, ""])
def test_clone_missing_audio_path_is_invalid_input(config_home, bad):
    client = FakeClient()
    r = build(config_home, client=client).clone_voice(bad, "音色一")
    assert r.status is Status.INVALID_INPUT
    assert client.uploaded == []


def test_tools_layer_and_services_never_import_dashscope():
    """Global Constraints: only ``core/qwen_client.py`` may import ``dashscope``.

    Checked statically (AST) because an in-process ``sys.modules`` check would be
    contaminated by the other test modules that legitimately import the client.
    """
    import ast
    from pathlib import Path

    import qwen_voice_mcp.tools as tools_pkg

    root = Path(tools_pkg.__file__).parent
    sources = sorted(root.glob("*.py")) + [Path(sv.__file__)]
    assert len(sources) >= 6, sources
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
                assert "dashscope" not in names, path
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "dashscope", path
