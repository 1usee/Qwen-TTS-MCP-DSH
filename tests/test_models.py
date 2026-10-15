import json
from qwen_voice_mcp.core.models import Status, ToolResult, VoiceModel, BalanceState


def test_tool_result_ok_only_for_success():
    assert ToolResult(status=Status.SUCCESS, message="ok").ok is True
    assert ToolResult(status=Status.INSUFFICIENT_BALANCE, message="no").ok is False


def test_tool_result_to_dict_always_has_status_and_message():
    d = ToolResult(status=Status.SUCCESS, message="done", data={"a": 1}).to_dict()
    assert d["status"] == "success"
    assert d["message"] == "done"
    assert d["a"] == 1
    assert "warnings" not in d


def test_tool_result_includes_warnings_when_present():
    d = ToolResult(status=Status.SUCCESS, message="done", warnings=["w"]).to_dict()
    assert d["warnings"] == ["w"]


def test_tool_result_is_json_serialisable():
    d = ToolResult(status=Status.SUCCESS, message="m", data={"n": None}).to_dict()
    assert json.loads(json.dumps(d))["n"] is None


def test_status_enum_values_are_the_spec_strings():
    assert Status.INSUFFICIENT_BALANCE.value == "insufficient_balance"
    assert Status.BALANCE_UNAVAILABLE.value == "balance_unavailable"
    assert Status.MODEL_NOT_SUPPORTED.value == "model_not_supported"
    assert Status.NETWORK_ERROR.value == "network_error"


def test_voicemodel_requires_name_and_capabilities():
    m = VoiceModel(name="cosyvoice-v2", type="voice_cloning_and_tts",
                   description="d", capabilities=["text_to_speech"], pricing="p")
    assert m.verified is None
    assert m.source == "builtin"


def test_balance_state_unknown_shape():
    b = BalanceState(balance=None, currency=None, source=None, last_error="all failed")
    assert b.balance is None
    assert b.last_error == "all failed"


def test_default_models_have_required_keys_and_unique_names():
    import config
    names = [m["name"] for m in config.DEFAULT_MODELS]
    assert len(names) == len(set(names))
    for m in config.DEFAULT_MODELS:
        for key in ("name", "type", "description", "capabilities", "pricing", "pricing_per_1k_chars"):
            assert key in m, key


def test_balance_endpoints_are_https_and_ordered():
    import config
    assert config.BALANCE_ENDPOINTS
    assert all(u.startswith("https://") for u in config.BALANCE_ENDPOINTS)
    assert config.BALANCE_ENDPOINTS[0].endswith("/api/v1/account/quota")
