"""Typed result model shared by every tool and core service.

Dependency rule (spec §4): this module is a **leaf** — it imports nothing from
this project. ``Status`` is the single source of truth for the ``status`` string
every MCP tool returns (spec §6.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Status",
    "ToolResult",
    "VoiceModel",
    "BalanceState",
    "AudioProbe",
]


class Status(str, Enum):
    """Closed set of outcome statuses (spec §6.1).

    Subclassing ``str`` makes the member itself JSON-serialisable and comparable
    to its plain-string value, so tool payloads need no conversion step.
    """

    SUCCESS = "success"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    BALANCE_UNAVAILABLE = "balance_unavailable"
    AUTH_ERROR = "auth_error"
    INVALID_INPUT = "invalid_input"
    MODEL_NOT_SUPPORTED = "model_not_supported"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    UPSTREAM_ERROR = "upstream_error"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ToolResult:
    """The one shape every MCP tool returns.

    Contract (spec §6.2): ``to_dict()`` always yields ``status`` and ``message``,
    spreads ``data`` on top, and only adds ``warnings`` when it is non-empty.
    """

    status: Status
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only for a fully successful call."""
        return self.status is Status.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready payload handed to the MCP layer."""
        payload: dict[str, Any] = {
            "status": self.status.value,
            "message": self.message,
            **self.data,
        }
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


class VoiceModel(BaseModel):
    """A declared (optionally verified) Qwen speech model."""

    model_config = ConfigDict(frozen=False, protected_namespaces=())

    name: str
    type: str
    description: str
    capabilities: list[str] = Field(default_factory=list)
    pricing: str = ""
    #: Declared CNY price per 1,000 characters; ``None`` means "unknown", and
    #: callers must treat unknown as 0.0 with a warning rather than guessing.
    pricing_per_1k_chars: float | None = None
    #: ``None`` = declared only; set by an opt-in live probe (spec §D5).
    verified: bool | None = None
    source: str = "builtin"

    def to_dict(self) -> dict[str, Any]:
        """Return the model as a plain JSON-ready dict."""
        return self.model_dump()


@dataclass(frozen=True)
class BalanceState:
    """Outcome of the balance-probe chain, including the "unknown" case."""

    balance: float | None = None
    currency: str | None = None
    source: str | None = None
    last_error: str | None = None

    @property
    def known(self) -> bool:
        """True when a numeric balance was actually parsed."""
        return self.balance is not None


@dataclass(frozen=True)
class AudioProbe:
    """Best-effort description of an audio file (spec §10.1).

    Unknown fields stay ``None`` rather than raising — an unparsable container
    is a warning, never a hard failure.
    """

    path: Path
    format: str
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    size_bytes: int = 0
    backend: str = "unknown"
