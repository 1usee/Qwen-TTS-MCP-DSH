import pytest
from qwen_voice_mcp.core.model_registry import ModelRegistry, UnknownModelError


def test_builtin_registry_is_non_empty_and_has_default():
    reg = ModelRegistry()
    assert reg.all()
    assert reg.default().name in reg.names()


def test_default_is_first_model_when_unspecified():
    reg = ModelRegistry([{"name": "a", "type": "t", "description": "d",
                          "capabilities": [], "pricing": "p", "pricing_per_1k_chars": 1.0}])
    assert reg.default().name == "a"


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        ModelRegistry().require("no-such-model")


def test_get_returns_none_for_unknown():
    assert ModelRegistry().get("no-such-model") is None


def test_verified_defaults_to_none():
    assert ModelRegistry().default().verified is None


def test_mark_verified_sets_flag():
    reg = ModelRegistry()
    name = reg.default().name
    reg.mark_verified(name, True)
    assert reg.get(name).verified is True


def test_declared_price_used_for_cost_estimate():
    reg = ModelRegistry([{"name": "m", "type": "t", "description": "d",
                          "capabilities": ["text_to_speech"], "pricing": "p",
                          "pricing_per_1k_chars": 0.26}])
    assert reg.estimated_cost("m", 1000) == pytest.approx(0.26)
    assert reg.estimated_cost("m", 0) == 0.0


def test_cost_rounds_to_four_decimals():
    reg = ModelRegistry([{"name": "m", "type": "t", "description": "d",
                          "capabilities": [], "pricing": "p", "pricing_per_1k_chars": 0.26}])
    assert reg.estimated_cost("m", 42) == 0.0109


def test_unknown_price_is_zero_not_an_error():
    reg = ModelRegistry([{"name": "m", "type": "t", "description": "d",
                          "capabilities": [], "pricing": "p"}])
    assert reg.estimated_cost("m", 500) == 0.0


def test_supports_reports_capability():
    reg = ModelRegistry()
    clone = [m for m in reg.all() if "voice_clone" in m.capabilities]
    assert clone, "at least one builtin model must declare voice_clone"
    assert reg.supports(clone[0].name, "voice_clone") is True


def test_clone_cost_is_positive_constant():
    assert ModelRegistry().clone_cost() > 0


def test_custom_models_replace_builtins_entirely():
    reg = ModelRegistry([{"name": "only", "type": "t", "description": "d",
                          "capabilities": [], "pricing": "p"}])
    assert reg.names() == ["only"]
