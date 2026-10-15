import httpx
import pytest
from qwen_voice_mcp.core.balance_checker import BalanceChecker, extract_balance
from qwen_voice_mcp.core.models import Status

ENDPOINTS = ["https://x.test/a", "https://x.test/b"]


def make(handler, **kw):
    return BalanceChecker("sk-test", endpoints=ENDPOINTS, timeout=1.0,
                          client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_extract_balance_reads_top_level_field():
    assert extract_balance({"balance": "12.50", "currency": "CNY"}) == (12.5, "CNY")


@pytest.mark.parametrize("key", ["total_balance", "available_balance", "remaining", "amount"])
def test_extract_balance_reads_alternate_field_names(key):
    assert extract_balance({key: 3})[0] == 3.0


def test_extract_balance_reads_nested_data():
    assert extract_balance({"data": {"balance": 7}})[0] == 7.0


def test_extract_balance_unrecognised_shape_returns_none():
    assert extract_balance({"unexpected": "shape"}) == (None, None)


def test_extract_balance_never_coerces_garbage_to_zero():
    assert extract_balance({"balance": "not-a-number"})[0] is None


def test_first_endpoint_success_is_used():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"balance": 12.5, "currency": "CNY"})

    state = make(handler).query()
    assert state.balance == 12.5 and state.currency == "CNY"
    assert len(seen) == 1


def test_falls_back_to_second_endpoint_on_http_error():
    def handler(req):
        if str(req.url).endswith("/a"):
            return httpx.Response(500)
        return httpx.Response(200, json={"balance": 5})

    assert make(handler).query().balance == 5.0


def test_falls_back_when_first_returns_unparsable_shape():
    def handler(req):
        if str(req.url).endswith("/a"):
            return httpx.Response(200, json={"unexpected": 1})
        return httpx.Response(200, json={"balance": 5})

    assert make(handler).query().balance == 5.0


def test_all_endpoints_failing_yields_unknown_with_last_error():
    def handler(req):
        raise httpx.ConnectError("boom")

    state = make(handler).query()
    assert state.balance is None and state.source is None and state.last_error


def test_all_endpoints_bad_shape_yields_unknown_not_zero():
    def handler(req):
        return httpx.Response(200, json={"unexpected": 1})

    assert make(handler).query().balance is None


def test_missing_api_key_does_not_call_network():
    def handler(req):  # pragma: no cover
        raise AssertionError("must not be called")

    state = BalanceChecker(None, endpoints=ENDPOINTS,
                           client=httpx.Client(transport=httpx.MockTransport(handler))).query()
    assert state.balance is None and state.last_error


def test_gate_blocks_when_balance_at_or_below_threshold():
    b = make(lambda r: httpx.Response(200, json={"balance": 1.0}), threshold=1.0)
    r = b.gate(estimated_cost=0.1, strict=False)
    assert r is not None and r.status is Status.INSUFFICIENT_BALANCE
    assert r.data["current_balance"] == 1.0 and r.data["threshold"] == 1.0


def test_gate_allows_when_balance_well_above_threshold_plus_cost():
    b = make(lambda r: httpx.Response(200, json={"balance": 12.5}), threshold=1.0)
    assert b.gate(estimated_cost=0.1, strict=False) is None
    assert b.warn_if_tight(0.1) == []


def test_gate_allows_but_warns_in_tight_band():
    b = make(lambda r: httpx.Response(200, json={"balance": 1.05}), threshold=1.0)
    assert b.gate(estimated_cost=0.1, strict=False) is None
    assert b.warn_if_tight(0.1)


def test_gate_blocks_unknown_balance_only_in_strict_mode():
    b = make(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert b.gate(0.1, strict=True).status is Status.BALANCE_UNAVAILABLE
    r = b.gate(0.1, strict=False)
    assert r is None and b.warn_if_tight(0.1)


def test_as_tool_result_success_shape():
    b = make(lambda r: httpx.Response(200, json={"balance": 12.5, "currency": "CNY"}), threshold=1.0)
    d = b.as_tool_result().to_dict()
    assert d["status"] == "success" and d["balance"] == 12.5
    assert d["is_sufficient"] is True and d["threshold"] == 1.0


def test_as_tool_result_unknown_shape_is_success_with_warning():
    b = make(lambda r: httpx.Response(500))
    d = b.as_tool_result().to_dict()
    assert d["status"] == "success" and d["balance"] is None
    assert d["is_sufficient"] is None and d["warnings"]


def test_insufficient_balance_message_contains_recharge_advice():
    b = make(lambda r: httpx.Response(200, json={"balance": 0.5}), threshold=1.0)
    msg = b.gate(0.11, strict=False).message
    assert "充值" in msg and "0.5" in msg
