"""Balance probing with honest degradation (spec §3 D2, §7).

The DashScope balance endpoint is **not** a documented public API. This module
therefore probes a short chain of candidate endpoints and, when none of them
yields a parsable number, reports the balance as *unknown* (``balance=None``)
instead of inventing ``0.00``. Treating unknown as zero would block every
billable call, so the unknown case is modelled explicitly and only ``strict``
mode refuses to proceed.

Threshold tiers (spec §7.2):

===============================  =========================================
``balance > threshold + cost``   execute normally
``threshold < balance <= ...``   execute, with a warning from ``warn_if_tight``
``balance <= threshold``         refuse: ``insufficient_balance``
unknown + ``strict=True``        refuse: ``balance_unavailable``
unknown + ``strict=False``       execute, with a warning
===============================  =========================================

Dependency rule (spec §4): only ``config``, ``core.models``, ``httpx`` and the
standard library are imported here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx

import config
from qwen_voice_mcp.core.models import BalanceState, Status, ToolResult

__all__ = ["BalanceChecker", "extract_balance", "validate_credential", "CredentialCheck"]

#: Candidate field names walked by :func:`extract_balance` (spec §7.1).
BALANCE_FIELDS: tuple[str, ...] = (
    "balance",
    "total_balance",
    "available_balance",
    "remaining",
    "amount",
)


@dataclass(frozen=True)
class CredentialCheck:
    """Outcome of a light-weight credential probe.

    ``ok`` means the endpoint accepted the credential. ``detail`` is a short
    Chinese explanation suitable for a GUI status line or a CLI message.
    """

    ok: bool
    detail: str
    state: BalanceState | None = None

    @property
    def balance(self) -> float | None:
        return self.state.balance if self.state else None


def validate_credential(
    api_key: str | None,
    *,
    timeout: float | None = None,
    endpoints: Sequence[str] | None = None,
    client: httpx.Client | None = None,
) -> CredentialCheck:
    """Probe whether ``api_key`` is accepted, without making a billable call.

    Only an answer that proves the endpoint accepted the credential counts. A
    missing key, an unreachable endpoint, or an unparsable response all mean "not
    validated" — we must never claim a key works without establishing that.

    This is the single implementation shared by the CLI and the config GUI
    (spec §13 requires the GUI to validate before storing).
    """
    if not api_key or not api_key.strip():
        return CredentialCheck(False, "未提供 API 密钥")

    if timeout is None:
        timeout = float(getattr(config, "BALANCE_TIMEOUT_SECONDS", 8.0))

    try:
        checker = BalanceChecker(api_key, endpoints=endpoints, timeout=timeout, client=client)
        state = checker.query()
    except Exception as exc:  # noqa: BLE001 - a probe must never raise at the caller
        return CredentialCheck(False, f"验证请求失败：{type(exc).__name__}")

    if state.balance is not None:
        return CredentialCheck(True, f"已验证 — 账户余额 ¥{state.balance:.2f}", state)

    if state.last_error is None and state.source is not None:
        # The endpoint answered cleanly, which proves the credential was accepted,
        # but it did not report a number we understand.
        return CredentialCheck(True, "已验证 — 接口可用但未能解析余额", state)

    detail = "密钥验证失败：接口拒绝或无权限"
    if state.last_error:
        detail = f"密钥验证失败：{state.last_error}"
    return CredentialCheck(False, detail, state)

#: First number-looking run inside an otherwise decorated string ("¥12.50").
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

#: Fallback currency when an endpoint omits the field (spec §7.1).
DEFAULT_CURRENCY = "CNY"

RECHARGE_ADVICE = "请为 Qwen 账户充值后重试。"


def _coerce_number(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` when it is not a number.

    Strings may carry decoration (``"¥12.50"``, ``"1,234.5"``). Anything that
    yields no numeric run returns ``None`` — never ``0.0``, because a fabricated
    zero would be read as "out of balance" and block every call.
    """
    if isinstance(value, bool):  # bool is an int subclass; JSON `true` is not a balance
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace(" ", "").strip()
        match = _NUMBER_RE.search(cleaned)
        if match is None:
            return None
        try:
            return float(match.group(0))
        except ValueError:  # pragma: no cover - regex already guarantees a number
            return None
    return None


def _currency_from(payload: dict[str, Any], nested: dict[str, Any] | None) -> str:
    """Read ``currency`` from the payload (or nested ``data``), defaulting to CNY."""
    for source in (payload, nested):
        if not isinstance(source, dict):
            continue
        raw = source.get("currency")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().upper()
    return DEFAULT_CURRENCY


def extract_balance(payload: Any) -> tuple[float | None, str | None]:
    """Pull ``(balance, currency)`` out of an unrecognised balance response.

    Returns ``(None, None)`` whenever no candidate field holds a parsable
    number, so callers can keep probing instead of trusting a bogus value.
    """
    if not isinstance(payload, dict):
        return (None, None)

    nested = payload.get("data")
    nested = nested if isinstance(nested, dict) else None

    for container in (payload, nested):
        if container is None:
            continue
        for field in BALANCE_FIELDS:
            if field not in container:
                continue
            amount = _coerce_number(container[field])
            if amount is not None:
                return (amount, _currency_from(payload, nested))

    return (None, None)


class BalanceChecker:
    """Probe the account balance once and gate billable calls on it."""

    def __init__(
        self,
        api_key: str | None,
        *,
        endpoints: Sequence[str] | None = None,
        timeout: float = config.BALANCE_TIMEOUT_SECONDS,
        threshold: float = config.BALANCE_THRESHOLD,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.endpoints: list[str] = list(endpoints) if endpoints else list(config.BALANCE_ENDPOINTS)
        self.timeout = timeout
        self.threshold = threshold
        self._client = client
        self._owns_client = client is None
        #: One probe per instance: ``gate`` and ``warn_if_tight`` share it.
        self._state: BalanceState | None = None

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def close(self) -> None:
        """Close the underlying client when this instance created it."""
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "BalanceChecker":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _probe(self) -> BalanceState:
        key = (self.api_key or "").strip()
        if not key:
            # Short-circuit: without a key there is nothing to authorise with,
            # and issuing the request anyway would only leak a 401 round trip.
            return BalanceState(
                balance=None,
                currency=None,
                source=None,
                last_error="未配置 DASHSCOPE_API_KEY，无法查询账户余额",
            )

        last_error: str | None = None
        for url in self.endpoints:
            try:
                response = self._http().get(url, headers=self._headers(), timeout=self.timeout)
            except httpx.HTTPError as exc:
                last_error = f"{url}: 请求失败（{type(exc).__name__}: {exc}）"
                continue
            except Exception as exc:  # pragma: no cover - defensive, never fatal
                last_error = f"{url}: 请求异常（{type(exc).__name__}: {exc}）"
                continue

            if not 200 <= response.status_code < 300:
                last_error = f"{url}: HTTP {response.status_code}"
                continue

            try:
                payload = response.json()
            except Exception:
                last_error = f"{url}: 响应不是合法 JSON"
                continue

            balance, currency = extract_balance(payload)
            if balance is None:
                last_error = f"{url}: 响应结构无法解析出余额字段"
                continue

            return BalanceState(
                balance=balance,
                currency=currency or DEFAULT_CURRENCY,
                source=url,
                last_error=None,
            )

        return BalanceState(
            balance=None,
            currency=None,
            source=None,
            last_error=last_error or "所有余额探测端点均失败",
        )

    def query(self, *, refresh: bool = False) -> BalanceState:
        """Return the cached probe result, performing it at most once."""
        if self._state is None or refresh:
            self._state = self._probe()
        return self._state

    # ------------------------------------------------------------------
    # Gating
    # ------------------------------------------------------------------
    def _unknown_warning(self) -> str:
        state = self.query()
        detail = state.last_error or "所有探测端点均失败"
        return f"余额状态未知（{detail}），已在非严格模式下放行，请自行确认账户余额。"

    def gate(self, estimated_cost: float, strict: bool) -> ToolResult | None:
        """Return ``None`` to allow the call, else the blocking result."""
        cost = float(estimated_cost or 0.0)
        state = self.query()

        if state.balance is None:
            if strict:
                return ToolResult(
                    status=Status.BALANCE_UNAVAILABLE,
                    message=(
                        "余额查询接口不可用，严格模式（strict_balance）下已拒绝本次调用。"
                        f"请稍后重试，或 {RECHARGE_ADVICE}"
                    ),
                    data={
                        "current_balance": None,
                        "threshold": self.threshold,
                        "estimated_cost": cost,
                        "recharge_url": config.RECHARGE_URL,
                    },
                )
            return None

        if state.balance <= self.threshold:
            return ToolResult(
                status=Status.INSUFFICIENT_BALANCE,
                message=(
                    f"账户余额不足，当前余额 ¥{state.balance:.2f}，"
                    f"预估本次调用消耗约 ¥{cost:.2f}，余额阈值 ¥{self.threshold:.2f}。"
                    + RECHARGE_ADVICE
                ),
                data={
                    "current_balance": state.balance,
                    "threshold": self.threshold,
                    "estimated_cost": cost,
                    "recharge_url": config.RECHARGE_URL,
                },
            )

        return None

    def warn_if_tight(self, estimated_cost: float) -> list[str]:
        """Warnings for the allowed-but-close tier; ``[]`` when comfortably funded."""
        cost = float(estimated_cost or 0.0)
        state = self.query()

        if state.balance is None:
            return [self._unknown_warning()]

        if state.balance <= self.threshold:
            return [
                f"账户余额 ¥{state.balance:.2f} 已低于阈值 ¥{self.threshold:.2f}，"
                + RECHARGE_ADVICE
            ]

        if state.balance <= self.threshold + cost:
            return [
                f"账户余额 ¥{state.balance:.2f} 接近阈值 ¥{self.threshold:.2f}，"
                f"本次预估消耗约 ¥{cost:.2f} 后余额可能不足，建议及时充值。"
            ]

        return []

    # ------------------------------------------------------------------
    # Read-only tool view
    # ------------------------------------------------------------------
    def as_tool_result(self) -> ToolResult:
        """Describe the balance. Always ``SUCCESS`` — the probe is read-only."""
        state = self.query()

        if state.balance is None:
            return ToolResult(
                status=Status.SUCCESS,
                message="余额查询接口不可用，余额状态未知，已在非严格模式下放行。",
                data={
                    "balance": None,
                    "currency": None,
                    "threshold": self.threshold,
                    "is_sufficient": None,
                    "source": None,
                },
                warnings=[self._unknown_warning()],
            )

        sufficient = state.balance > self.threshold
        message = (
            f"账户余额 ¥{state.balance:.2f}，状态正常。"
            if sufficient
            else f"账户余额 ¥{state.balance:.2f}，已低于阈值 ¥{self.threshold:.2f}，"
            + RECHARGE_ADVICE
        )
        warnings = [] if sufficient else [
            f"账户余额低于阈值 ¥{self.threshold:.2f}，计费调用将被拒绝。" + RECHARGE_ADVICE
        ]
        return ToolResult(
            status=Status.SUCCESS,
            message=message,
            data={
                "balance": state.balance,
                "currency": state.currency,
                "threshold": self.threshold,
                "is_sufficient": sufficient,
                "source": state.source,
            },
            warnings=warnings,
        )
