"""Options model and layered configuration store.

Resolution order (highest first):

1. explicit CLI keyword arguments passed to :func:`load_options`
2. environment variables ``QWEN_VOICE_<FIELD>``
3. ``config.json`` inside :func:`config_home`
4. the declared defaults in :mod:`config`

Dependency rule (spec §4): this module is a **near-leaf**. It imports only
``config`` and third-party ``pydantic`` — never another project module — so that
higher layers may depend on it without creating an import cycle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

import config as _config

__all__ = [
    "Options",
    "config_home",
    "config_path",
    "load_options",
    "save_options",
    "LOOPBACK_HOSTS",
]

#: Hosts treated as loopback by :meth:`Options.reject_non_loopback`.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Fields of :class:`Options` that may be supplied by env vars or ``config.json``.
OVERRIDABLE_FIELDS: tuple[str, ...] = (
    "transport",
    "host",
    "port",
    "default_model",
    "default_voice",
    "default_output_format",
    "balance_threshold",
    "strict_balance",
    "balance_timeout_seconds",
    "http_auth_token",
    "output_dir",
    "temp_dir",
    "audio_min_seconds",
    "audio_max_seconds",
    "audio_max_bytes",
    "log_level",
    "allow_non_loopback",
)

#: Filename of the persisted options document.
CONFIG_FILENAME = "config.json"


def config_home() -> Path:
    """Return the configuration directory, honouring ``QWEN_VOICE_HOME``.

    Pure resolution — no directory is created, so read-only callers such as
    :func:`load_options` keep working when the config home is missing or not
    writable. Writers create what they need (see :func:`save_options` and the
    ``output_dir``/``temp_dir`` factories).
    """
    override = os.environ.get(_config.CONFIG_HOME_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "qwen-voice-mcp"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "qwen-voice-mcp"


def config_path() -> Path:
    """Return the path of the persisted ``config.json``."""
    return config_home() / CONFIG_FILENAME


def _default_output_dir() -> Path:
    return _prepared_directory(config_home() / "output")


def _default_temp_dir() -> Path:
    return _prepared_directory(config_home() / "tmp")


def _prepared_directory(path: Path) -> Path:
    """Best-effort recursive create; the path is returned either way."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


class Options(BaseModel):
    """Effective runtime options.

    Every default is copied from :mod:`config` (spec §9) rather than re-typed,
    so the declared constants remain the single source of truth.
    """

    transport: Literal["stdio", "streamable-http", "sse"] = _config.DEFAULT_TRANSPORT
    host: str = _config.DEFAULT_HOST
    port: int = _config.DEFAULT_PORT
    default_model: str = _config.DEFAULT_MODEL
    default_voice: str = _config.DEFAULT_VOICE
    default_output_format: str = _config.DEFAULT_OUTPUT_FORMAT
    balance_threshold: float = _config.BALANCE_THRESHOLD
    strict_balance: bool = _config.STRICT_BALANCE
    balance_timeout_seconds: float = _config.BALANCE_TIMEOUT_SECONDS
    http_auth_token: str | None = None
    output_dir: Path = Field(default_factory=_default_output_dir)
    temp_dir: Path = Field(default_factory=_default_temp_dir)
    audio_min_seconds: float = _config.AUDIO_MIN_SECONDS
    audio_max_seconds: float = _config.AUDIO_MAX_SECONDS
    audio_max_bytes: int = _config.AUDIO_MAX_BYTES
    log_level: str = _config.LOG_LEVEL
    allow_non_loopback: bool = False

    @model_validator(mode="after")
    def _check_ranges(self) -> "Options":
        if not 1 <= self.port <= 65535:
            raise ValueError(f"port 必须在 1..65535 之间，当前为 {self.port}")
        if self.audio_min_seconds >= self.audio_max_seconds:
            raise ValueError(
                "audio_min_seconds 必须小于 audio_max_seconds"
                f"（当前 {self.audio_min_seconds} >= {self.audio_max_seconds}）"
            )
        if self.balance_threshold < 0:
            raise ValueError(f"balance_threshold 不能为负数，当前为 {self.balance_threshold}")
        if self.audio_max_bytes <= 0:
            raise ValueError(f"audio_max_bytes 必须为正数，当前为 {self.audio_max_bytes}")
        self._ensure_directories()
        return self

    def _ensure_directories(self) -> None:
        """Create ``output_dir``/``temp_dir`` so tools never race on ``mkdir``."""
        for directory in (self.output_dir, self.temp_dir):
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
            except OSError:  # pragma: no cover - unwritable path surfaces at use
                pass

    def reject_non_loopback(self) -> "Options":
        """Raise ``ValueError`` for a non-loopback bind unless explicitly allowed."""
        if self.allow_non_loopback:
            return self
        if self.host.strip().lower() not in LOOPBACK_HOSTS:
            raise ValueError(
                f"host={self.host!r} 不是回环地址；如需对外监听请显式设置 allow_non_loopback=True"
            )
        return self


def _read_config_file(home: Path) -> dict[str, Any]:
    """Return the persisted options, or ``{}`` when absent/torn/unreadable."""
    path = home / CONFIG_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # A torn or hand-broken file must not be fatal — fall back to defaults.
        return {}
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k in OVERRIDABLE_FIELDS and v is not None}


def _read_env() -> dict[str, Any]:
    """Return env-var overrides named ``QWEN_VOICE_<FIELD_UPPER>``."""
    found: dict[str, Any] = {}
    for field in OVERRIDABLE_FIELDS:
        raw = os.environ.get(f"{_config.ENV_PREFIX}{field.upper()}")
        if raw is None or raw == "":
            continue
        found[field] = raw
    return found


def load_options(**overrides: Any) -> Options:
    """Build :class:`Options` from CLI kwargs > env > ``config.json`` > defaults.

    Never fails because the config home is missing or unreadable: an absent,
    torn, or inaccessible ``config.json`` simply yields the defaults.
    """
    home = config_home()
    merged: dict[str, Any] = _read_config_file(home)
    merged.update(_read_env())
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    unknown = set(merged) - set(OVERRIDABLE_FIELDS)
    if unknown:
        raise TypeError(f"未知配置项: {', '.join(sorted(unknown))}")
    return Options(**merged)


def save_options(opts: Options) -> Path:
    """Persist ``opts`` to ``config_home()/config.json`` atomically (UTF-8)."""
    home = config_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / CONFIG_FILENAME
    payload = opts.model_dump(mode="json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


# Re-exported so callers can catch store-level validation uniformly.
StoreValidationError = ValidationError
