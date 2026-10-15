"""Task 8 — in-memory MCP end-to-end tests for the four tools (spec §12, §15).

The MCP runtime under test is the real one: ``build_server()`` assembles a real
``FastMCP`` and the tools are discovered and called through
``mcp.shared.memory.create_connected_server_and_client_session``.  Nothing here
reaches the network:

* the vault is redirected into the test config home (``QWEN_VOICE_VAULT``) and
  ``DASHSCOPE_API_KEY`` is removed, so the balance probe short-circuits;
* every call that would talk to DashScope is rejected by input validation before
  the client is reached (empty text, unusable reference audio).
"""

from __future__ import annotations

import logging
import sys

import pytest

from qwen_voice_mcp.core.config_store import load_options
from qwen_voice_mcp.core.models import Status, ToolResult
from qwen_voice_mcp.core.services import VoiceServices
from qwen_voice_mcp.server import EXPECTED_TOOLS, build_server, selftest

EXPECTED = {"list_voice_models", "synthesize_speech", "clone_voice", "check_balance"}


class FakeChecker:
    def __init__(self, block=None, warnings=None):
        self.block = block
        self.warnings = list(warnings or [])
        self.gate_calls = []

    def gate(self, estimated_cost, strict):
        self.gate_calls.append((estimated_cost, strict))
        return self.block

    def warn_if_tight(self, estimated_cost):
        return list(self.warnings)

    def as_tool_result(self):
        return ToolResult(Status.SUCCESS, "余额 ¥12.50", {"balance": 12.5, "is_sufficient": True})


class FakeClient:
    def synthesize(self, text, model, voice, fmt, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfb\x90\x00")
        return ToolResult(
            Status.SUCCESS,
            "合成完成",
            {
                "audio_path": str(path),
                "audio_format": fmt,
                "model_used": model,
                "voice_used": voice,
                "text_length": len(text),
                "duration_seconds": 1.0,
            },
        )

    def clone_voice(self, url, target_model, prefix, language_hints=None):
        return ToolResult(
            Status.SUCCESS,
            "克隆完成",
            {"voice_id": "v1", "voice_prefix": prefix, "cloning_status": "completed"},
        )

    def upload_reference(self, path):
        return "https://f/ref.wav"


@pytest.fixture
def offline_vault(config_home, monkeypatch):
    """Point the vault at an empty test path so no real profile is ever read."""
    monkeypatch.setenv("QWEN_VOICE_VAULT", str(config_home / "vault.enc"))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    return config_home / "vault.enc"


@pytest.fixture
def server(config_home, offline_vault):
    return build_server(load_options())


@pytest.fixture
def injected_server(config_home, offline_vault):
    """A server whose gate/client are fakes — for tool-call result assertions."""
    services = VoiceServices(
        load_options(), client=FakeClient(), checker=FakeChecker(), secret_manager=None
    )
    return build_server(load_options(), services=services)


def _session(server):
    from mcp.shared.memory import create_connected_server_and_client_session

    return create_connected_server_and_client_session(server)


# ---------------------------------------------------------------------------
# Discovery and schema
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_all_four_tools_are_exposed(server):
    async with _session(server) as session:
        tools = await session.list_tools()
    assert {t.name for t in tools.tools} == EXPECTED


@pytest.mark.anyio
async def test_tools_carry_chinese_descriptions(server):
    async with _session(server) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert all(tools[name].description for name in EXPECTED)


@pytest.mark.anyio
async def test_synthesize_requires_text_argument(server):
    async with _session(server) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert "text" in tools["synthesize_speech"].inputSchema["required"]
    assert set(tools["clone_voice"].inputSchema["required"]) == {"audio_path", "voice_name"}
    assert "required" not in tools["list_voice_models"].inputSchema
    assert "required" not in tools["check_balance"].inputSchema


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_calling_list_voice_models_returns_models(injected_server):
    async with _session(injected_server) as session:
        res = await session.call_tool("list_voice_models", {})
    assert res.isError is False
    assert "models" in res.content[0].text
    assert "default_model" in res.content[0].text


@pytest.mark.anyio
async def test_calling_synthesize_speech_succeeds(injected_server):
    async with _session(injected_server) as session:
        res = await session.call_tool("synthesize_speech", {"text": "你好"})
    assert res.isError is False
    assert '"status": "success"' in res.content[0].text
    assert "audio_path" in res.content[0].text


@pytest.mark.anyio
async def test_calling_synthesize_speech_with_empty_text_is_invalid_input(injected_server):
    async with _session(injected_server) as session:
        res = await session.call_tool("synthesize_speech", {"text": "   "})
    assert res.isError is False
    assert '"status": "invalid_input"' in res.content[0].text


@pytest.mark.anyio
async def test_unknown_model_surfaces_as_model_not_supported(injected_server):
    async with _session(injected_server) as session:
        res = await session.call_tool(
            "synthesize_speech", {"text": "你好", "model": "no-such-model"}
        )
    assert '"status": "model_not_supported"' in res.content[0].text


@pytest.mark.anyio
async def test_blocked_gate_surfaces_as_insufficient_balance(config_home, offline_vault):
    services = VoiceServices(
        load_options(),
        client=FakeClient(),
        checker=FakeChecker(block=ToolResult(Status.INSUFFICIENT_BALANCE, "余额不足")),
        secret_manager=None,
    )
    server = build_server(load_options(), services=services)
    async with _session(server) as session:
        res = await session.call_tool("synthesize_speech", {"text": "你好"})
    assert res.isError is False
    assert '"status": "insufficient_balance"' in res.content[0].text


@pytest.mark.anyio
async def test_calling_clone_voice_with_unusable_audio_is_invalid_input(injected_server, tmp_path):
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio at all")
    async with _session(injected_server) as session:
        res = await session.call_tool(
            "clone_voice", {"audio_path": str(broken), "voice_name": "音色一"}
        )
    assert res.isError is False
    assert '"status": "invalid_input"' in res.content[0].text


@pytest.mark.anyio
async def test_calling_clone_voice_succeeds(injected_server, tmp_path):
    from qwen_voice_mcp.core import audio_utils as au

    wav = au.make_tone_wav(tmp_path / "ref.wav", seconds=10)
    async with _session(injected_server) as session:
        res = await session.call_tool(
            "clone_voice", {"audio_path": str(wav), "voice_name": "音色一"}
        )
    assert res.isError is False
    assert '"status": "success"' in res.content[0].text
    assert "voice_id" in res.content[0].text


@pytest.mark.anyio
async def test_calling_check_balance_is_offline_success(injected_server):
    async with _session(injected_server) as session:
        res = await session.call_tool("check_balance", {})
    assert res.isError is False
    assert '"status": "success"' in res.content[0].text


@pytest.mark.anyio
async def test_check_balance_without_a_key_is_success_with_unknown_balance(server):
    """The real ``BalanceChecker`` with no key must not call the network."""
    async with _session(server) as session:
        res = await session.call_tool("check_balance", {})
    assert res.isError is False
    assert '"status": "success"' in res.content[0].text
    assert '"balance": null' in res.content[0].text


# ---------------------------------------------------------------------------
# Server wiring, logging, and the offline self-test
# ---------------------------------------------------------------------------
def test_expected_tools_constant_matches_the_spec():
    assert set(EXPECTED_TOOLS) == EXPECTED


def test_stdio_logging_is_attached_to_stderr(server):
    logger = logging.getLogger("qwen_voice_mcp")
    streams = [getattr(handler, "stream", None) for handler in logger.handlers]
    assert any(stream is sys.stderr for stream in streams), streams
    assert logger.propagate is False


def test_build_server_does_not_pass_a_version_argument(config_home, offline_vault):
    """Deviation D1: mcp 1.30's ``FastMCP.__init__`` has no ``version`` parameter."""
    built = build_server(load_options())
    assert built.name == "qwen-voice-mcp"


def test_selftest_prints_the_four_tool_names(config_home, capsys):
    assert selftest(load_options()) == 0
    out = capsys.readouterr().out
    assert "SELFTEST OK" in out
    for name in EXPECTED:
        assert name in out
