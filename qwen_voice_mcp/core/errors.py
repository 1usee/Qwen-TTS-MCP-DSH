"""Exception → :class:`Status` classification (spec §6.1, §11).

Every exception that can escape a DashScope call is funnelled through
:func:`classify` so the MCP layer only ever sees one of the ten statuses in
:class:`~qwen_voice_mcp.core.models.Status`.

Two safety rules are absolute (spec §8.4, Global Constraints):

* A secret must never reach a result message or a log line. :func:`safe_message`
  scrubs every credential-shaped token (``sk-…``, Aliyun ``LTAI…``, ``Bearer …``,
  ``api_key=…``) while leaving the surrounding diagnostic text readable.
* Classification never raises. An unclassifiable exception degrades to
  ``UPSTREAM_ERROR``; a failure of the scrubbing machinery itself degrades to a
  fixed placeholder rather than leaking the original text.
"""

from __future__ import annotations

import re
from typing import Any

from qwen_voice_mcp.core.models import Status

__all__ = ["classify", "safe_message", "status_for_code", "KEY_PATTERN"]

#: Credential shapes that must never survive into a message or a log line.
#: Each alternative is anchored on a prefix that is unambiguous enough not to
#: mask ordinary prose, so the surrounding diagnostic text stays readable.
KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # DashScope / OpenAI style: sk-abcdef…  (a too-short "sk-x" collapses too)
    re.compile(r"sk-\s*[A-Za-z0-9_\-]+"),
    # Aliyun AccessKey id, the shape DashScope actually issues for some accounts.
    re.compile(r"LTAI[A-Za-z0-9]{8,}"),
    # Bearer / Authorization header values.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{8,}"),
    # api_key=…, api-key: …, token=…, password=… assignments.
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password)\b\s*[:=]\s*"
        r"[\"']?[A-Za-z0-9\-._~+/=]{8,}[\"']?"
    ),
)

#: Backwards-compatible single pattern (first entry: the ``sk-`` shape).
KEY_PATTERN = KEY_PATTERNS[0]

#: Placeholder substituted for a scrubbed credential.
_REDACTION = "[REDACTED]"

#: Fixed fallback used when even the scrubber fails — never the raw text.
_FALLBACK_MESSAGE = "上游调用失败（错误详情已隐藏）"

#: HTTP-ish code → status. Used for ``exc.status_code`` and for 5xx ranges.
_CODE_MAP: dict[int, Status] = {
    400: Status.INVALID_INPUT,
    401: Status.AUTH_ERROR,
    403: Status.AUTH_ERROR,
    422: Status.INVALID_INPUT,
    429: Status.RATE_LIMITED,
}

#: DashScope error codes / message keywords → status. Matched case-insensitively
#: as substrings, in this order (first hit wins).
_KEYWORD_MAP: tuple[tuple[str, Status], ...] = (
    ("invalidapikey", Status.AUTH_ERROR),
    ("invalid_api_key", Status.AUTH_ERROR),
    ("accessdenied", Status.AUTH_ERROR),
    ("access_denied", Status.AUTH_ERROR),
    ("unauthorized", Status.AUTH_ERROR),
    ("authentication", Status.AUTH_ERROR),
    ("throttling", Status.RATE_LIMITED),
    ("ratelimit", Status.RATE_LIMITED),
    ("rate_limit", Status.RATE_LIMITED),
    ("toomanyrequests", Status.RATE_LIMITED),
    ("modelnotexist", Status.INVALID_INPUT),
    ("model_not_exist", Status.INVALID_INPUT),
    ("invalidparameter", Status.INVALID_INPUT),
    ("invalid_parameter", Status.INVALID_INPUT),
    ("datainspectionfailed", Status.INVALID_INPUT),
    ("data_inspection_failed", Status.INVALID_INPUT),
    ("timeout", Status.NETWORK_ERROR),
    ("timedout", Status.NETWORK_ERROR),
    ("connection", Status.NETWORK_ERROR),
)


def safe_message(exc: BaseException | str | None) -> str:
    """Return a credential-free description of ``exc``.

    Accepts the exception itself (``str(exc)`` is used) or already-stringified
    text. Only *secret-shaped tokens* are replaced (see :data:`KEY_PATTERNS`); the
    rest of the message is preserved verbatim, because an operator needs to read
    it — collapsing whole messages to a 10-character fragment made distinct
    failures indistinguishable.

    ``SecretManager.redact`` is deliberately **not** used here: it is a *value*
    displayer (it keeps 6 head and 4 tail characters of a single secret) and
    applying it to a sentence destroys the diagnostic text.
    """
    if exc is None:
        return ""
    if isinstance(exc, BaseException):
        try:
            text = str(exc)
        except Exception:  # a broken __str__ must not break error handling
            text = type(exc).__name__
    else:
        text = str(exc)

    try:
        for pattern in KEY_PATTERNS:
            text = pattern.sub(_REDACTION, text)
        return text
    except Exception:
        return _FALLBACK_MESSAGE


def _coerce_code(value: Any) -> int | None:
    """Best-effort conversion of a status code / code field to ``int``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        # DashScope sometimes returns "Throttling.RateQuota" or "429".
        match = re.search(r"\d{3}", value)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return None
    return None


def status_for_code(code: int | None) -> Status | None:
    """Map a numeric code to a :class:`Status`, or ``None`` when unknown."""
    if code is None:
        return None
    mapped = _CODE_MAP.get(code)
    if mapped is not None:
        return mapped
    if 500 <= code <= 599:
        return Status.UPSTREAM_ERROR
    return None


def _status_for_status_code(exc: BaseException) -> Status | None:
    """Classify by an explicit ``status_code``/``status``/``http_status`` field."""
    for attribute in ("status_code", "status", "http_status", "http_code"):
        raw = getattr(exc, attribute, None)
        if raw is None or callable(raw):
            continue
        status = status_for_code(_coerce_code(raw))
        if status is not None:
            return status
    return None


def _status_for_httpx(exc: BaseException) -> Status | None:
    """Classify an ``httpx`` transport/HTTP error (import guarded)."""
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx is a hard dependency in practice
        return None

    if isinstance(exc, httpx.TimeoutException):
        return Status.NETWORK_ERROR
    if isinstance(exc, httpx.HTTPStatusError):
        response = getattr(exc, "response", None)
        status = status_for_code(getattr(response, "status_code", None))
        return status or Status.UPSTREAM_ERROR
    if isinstance(exc, (httpx.ConnectError, httpx.TransportError)):
        return Status.NETWORK_ERROR
    if isinstance(exc, httpx.HTTPError):
        return Status.NETWORK_ERROR
    return None


def _status_for_keywords(exc: BaseException) -> Status | None:
    """Scan the error ``code``/``message``/``str(exc)`` for known keywords."""
    chunks: list[str] = []
    for attribute in ("code", "error_code", "message", "msg", "error"):
        value = getattr(exc, attribute, None)
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            chunks.extend(str(item) for item in value.values())
    try:
        chunks.append(str(exc))
    except Exception:
        pass
    haystack = " ".join(chunks).replace("-", "_").lower()
    compact = haystack.replace("_", "").replace(" ", "")
    for keyword, status in _KEYWORD_MAP:
        needle = keyword.replace("_", "")
        if keyword in haystack or needle in compact:
            return status
    return None


def classify(exc: BaseException) -> tuple[Status, str]:
    """Map ``exc`` to ``(status, safe_message)``.

    Order of inspection (plan Task 7, Step 3): explicit status code → ``httpx``
    exception type → generic ``OSError`` (socket-level failure) → error-code /
    message keyword scan → ``UPSTREAM_ERROR`` as the honest fallback.
    """
    message = safe_message(exc)

    status = _status_for_status_code(exc)
    if status is not None:
        return status, message

    status = _status_for_httpx(exc)
    if status is not None:
        return status, message

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return Status.NETWORK_ERROR, message
    if isinstance(exc, OSError):
        return Status.NETWORK_ERROR, message

    status = _status_for_keywords(exc)
    if status is not None:
        return status, message

    return Status.UPSTREAM_ERROR, message
