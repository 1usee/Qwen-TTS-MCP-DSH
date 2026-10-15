"""Service container: the one assembly point and the one balance gate.

Dependency direction (spec §4)::

    tools/*  →  core.services.VoiceServices  →  ┬ core.qwen_client → dashscope
                                                 ├ core.balance_checker
                                                 ├ core.audio_utils
                                                 └ core/model_registry

Design rules this module exists to enforce:

* **One gate, one place.** :meth:`VoiceServices.ensure_balance` is the *only*
  implementation of the "余额 ≤ 阈值 → 拒绝" rule; both billable tools call it
  (spec §4: "计费闸门 ``services.ensure_balance()`` 只有一处实现").
* **Order is a hard constraint (spec §7.2).** ``synthesize_speech`` rejects empty
  text and unknown models *before* it looks at the balance, and the balance is
  checked *before* the SDK is reached — when the gate blocks, the client receives
  exactly zero calls.  ``clone_voice`` mirrors that: materialise → validate →
  gate → upload → enrol, so an unusable reference clip is never uploaded.
* **No leaks.** Every public method is wrapped in ``try/except Exception``: the
  caller gets ``internal_error`` with a fixed Chinese message while the real
  exception (which may embed a key, a URL, or a stack) goes to the log only.
* ``secret_manager=None`` means "do not touch the vault" — the documented startup
  path supplies a real one (see :func:`qwen_voice_mcp.server.build_server`).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

import config

from qwen_voice_mcp.core import audio_utils
from qwen_voice_mcp.core.balance_checker import BalanceChecker
from qwen_voice_mcp.core.config_store import Options, load_options
from qwen_voice_mcp.core.model_registry import ModelRegistry, UnknownModelError
from qwen_voice_mcp.core.models import Status, ToolResult
from qwen_voice_mcp.core.voice_store import VoiceAliasStore

__all__ = ["INTERNAL_ERROR_MESSAGE", "VoiceServices"]

logger = logging.getLogger(__name__)

#: The only ``internal_error`` message a caller ever sees (spec §6.1: 兜底，不含堆栈).
INTERNAL_ERROR_MESSAGE = "内部错误，请查看服务端日志"

#: Per-call timeout handed to the DashScope SDK (the balance probe has its own,
#: much shorter, ``balance_timeout_seconds``).
SYNTHESIS_TIMEOUT_SECONDS = 60.0

#: Container signatures that prove a reference clip really is audio.  This is the
#: services-level strictness rule: ``audio_utils.validate_reference`` deliberately
#: downgrades "duration unknown" to a warning (spec §10.2), but a payload that
#: matches no known container *and* could not be probed is not audio we can
#: honestly hand to DashScope, so the gate rejects it before any upload.
_REFERENCE_MAGIC: tuple[bytes, ...] = (
    b"RIFF",  # wav
    b"ID3",  # mp3 with tags
    b"\xff\xfb",  # mp3 frame sync
    b"\xff\xf3",
    b"\xff\xf2",
    b"fLaC",  # flac
    b"OggS",  # ogg/opus
    b"FORM",  # aiff
    b"ADIF",  # aac
    b"#!AMR",  # amr
)


def _content_is_recognised_audio(path: Path) -> bool:
    """True when the first bytes match a known audio container signature."""
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(16)
    except OSError:  # pragma: no cover - the caller already stat-ed the file
        return False
    if head[4:8] == b"ftyp":  # ISO base media file format: m4a / mp4
        return True
    return any(head.startswith(signature) for signature in _REFERENCE_MAGIC)


def _build_client(
    key: str | None, registry: ModelRegistry, secret_manager: Any
) -> Any:
    """Construct the real DashScope wrapper.

    Imported lazily so that neither ``qwen_voice_mcp.core.services`` nor anything
    under ``qwen_voice_mcp.tools`` pulls in the SDK at import time — only
    :mod:`qwen_voice_mcp.core.qwen_client` is allowed to import ``dashscope``
    (Global Constraints), and the tools layer must stay SDK-free.
    """
    from qwen_voice_mcp.core.qwen_client import QwenClient

    return QwenClient(
        key,
        registry,
        timeout=SYNTHESIS_TIMEOUT_SECONDS,
        secret_manager=secret_manager,
    )


def _voice_prefix(voice_name: str) -> str:
    """Derive a legal DashScope ``prefix`` (<10, lowercase alphanumeric)."""
    from qwen_voice_mcp.core.qwen_client import make_voice_prefix

    return make_voice_prefix(voice_name)


class VoiceServices:
    """Compose every core module and expose the four tool behaviours.

    Parameters
    ----------
    options:
        Effective runtime options (defaults to :func:`load_options`).
    registry:
        Model catalogue.  Defaults to the declared models, with ``options.default_model``
        as the preferred default (falling back to the first declared model when the
        configured name is unknown).
    api_key:
        Explicit DashScope key; ``None`` falls back to ``DASHSCOPE_API_KEY`` and
        then to ``secret_manager``.
    secret_manager:
        Vault used only when no key was resolved.  ``None`` means "do not touch
        the vault at all" — that is the tested, offline-safe path.
    client / checker:
        Injection seams.  ``None`` builds the real ``QwenClient`` / ``BalanceChecker``.
    voices:
        Alias store for ``voice_name -> voice_id`` (spec §11.1).  ``None`` builds
        the real :class:`VoiceAliasStore` under the config home; the store
        degrades to "no aliases" when the file is missing or unreadable.
    """

    def __init__(
        self,
        options: Options | None = None,
        registry: ModelRegistry | None = None,
        *,
        api_key: str | None = None,
        secret_manager: Any = None,
        client: Any = None,
        checker: Any = None,
        voices: Any = None,
    ) -> None:
        self.options = options if options is not None else load_options()
        self.registry = (
            registry
            if registry is not None
            else ModelRegistry(default_model=self.options.default_model)
        )
        self.secret_manager = secret_manager
        self.api_key = api_key
        self.voices = voices if voices is not None else VoiceAliasStore()

        key = self._resolve_api_key()
        self._client = (
            client
            if client is not None
            else _build_client(key, self.registry, secret_manager)
        )
        self._checker = (
            checker
            if checker is not None
            else BalanceChecker(
                key,
                endpoints=config.BALANCE_ENDPOINTS,
                timeout=self.options.balance_timeout_seconds,
                threshold=self.options.balance_threshold,
            )
        )

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def _resolve_api_key(self) -> str | None:
        """Explicit key → env var → vault.  Never logs the key itself."""
        if self.api_key:
            return self.api_key
        from_env = os.environ.get(config.API_KEY_ENV)
        if from_env:
            return from_env
        if self.secret_manager is None:
            return None  # "do not touch the vault"
        try:
            stored = self.secret_manager.retrieve(config.API_KEY_ENV)
        except Exception:
            logger.warning("读取密钥库失败，本次按未配置 API Key 处理", exc_info=True)
            return None
        return str(stored) if stored else None

    @property
    def client(self) -> Any:
        """The DashScope-facing client (fake in tests)."""
        return self._client

    @property
    def checker(self) -> Any:
        """The balance checker backing :meth:`ensure_balance`."""
        return self._checker

    # ------------------------------------------------------------------
    # The single balance gate
    # ------------------------------------------------------------------
    def ensure_balance(
        self, estimated_cost: float, strict: bool | None = None
    ) -> ToolResult | None:
        """Return ``None`` to allow the billable call, else the blocking result.

        This is the *only* implementation of the gate (spec §4, §7.2).  ``strict``
        defaults to the configured ``strict_balance``.
        """
        if strict is None:
            strict = bool(self.options.strict_balance)
        return self._checker.gate(float(estimated_cost or 0.0), bool(strict))

    @staticmethod
    def _guard(where: str, body: Callable[[], ToolResult]) -> ToolResult:
        """Run ``body``; log the real exception (stderr) and return the opaque fallback.

        The exception detail never crosses this boundary — ``message`` is always
        :data:`INTERNAL_ERROR_MESSAGE` (spec §6.1: 兜底，不含堆栈).
        """
        try:
            return body()
        except Exception:
            logger.exception("%s 执行失败", where)
            return ToolResult(Status.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE)

    # ------------------------------------------------------------------
    # list_voice_models
    # ------------------------------------------------------------------
    def list_voice_models(self) -> ToolResult:
        """Return the declared catalogue (spec §6.3).  Read-only, never bills."""

        def body() -> ToolResult:
            models = [model.to_dict() for model in self.registry.all()]
            return ToolResult(
                Status.SUCCESS,
                f"共 {len(models)} 个语音模型",
                data={
                    "models": models,
                    "default_model": self.registry.default().name,
                    "count": len(models),
                },
            )

        return self._guard("list_voice_models", body)

    # ------------------------------------------------------------------
    # synthesize_speech
    # ------------------------------------------------------------------
    def synthesize_speech(
        self,
        text: str,
        model: str | None = None,
        voice: str | None = None,
        output_format: str | None = None,
    ) -> ToolResult:
        """Synthesise ``text``.  Input validation → gate → SDK, in that order."""

        def body() -> ToolResult:
            # 1. empty / whitespace-only text: rejected before any balance work.
            if not isinstance(text, str) or not text.strip():
                return ToolResult(
                    Status.INVALID_INPUT,
                    "待合成的文本为空或仅含空白字符，请提供非空文本",
                )

            # 2. resolve the model before the gate: an unknown name is free to reject.
            requested = str(model).strip() if model else self.registry.default().name
            try:
                entry = self.registry.require(requested)
            except UnknownModelError:
                return ToolResult(
                    Status.MODEL_NOT_SUPPORTED,
                    f"未知模型 {requested!r}，请先用 list_voice_models 查看可用模型",
                    data={"model": requested, "available_models": self.registry.names()},
                )

            fmt = (
                output_format
                or self.options.default_output_format
                or config.DEFAULT_OUTPUT_FORMAT
            )
            fmt = str(fmt).strip().lower().lstrip(".")
            resolved_voice = str(voice or self.options.default_voice or config.DEFAULT_VOICE)

            # 3. estimate, 4. gate — and only then touch the client.
            estimated = self.registry.estimated_cost(requested, len(text))
            blocked = self.ensure_balance(estimated, self.options.strict_balance)
            if blocked is not None:
                return blocked

            # 5. the billable call.
            output_path = audio_utils.new_output_path(self.options.output_dir, fmt)
            result = self._client.synthesize(text, requested, resolved_voice, fmt, output_path)
            if result.status is not Status.SUCCESS:
                return result

            warnings = list(result.warnings)
            warnings.extend(self._checker.warn_if_tight(estimated))
            if entry.pricing_per_1k_chars is None:
                warnings.append(
                    "模型未声明价格，estimated_cost 按 0 计算，实际费用以账单为准"
                )
            return ToolResult(
                Status.SUCCESS,
                result.message,
                data={**result.data, "estimated_cost": estimated},
                warnings=warnings,
            )

        return self._guard("synthesize_speech", body)

    # ------------------------------------------------------------------
    # clone_voice
    # ------------------------------------------------------------------
    def clone_voice(
        self,
        audio_path: str,
        voice_name: str,
        model: str | None = None,
        language_hints: list[str] | None = None,
    ) -> ToolResult:
        """Enrol a cloned voice.  Validate → gate → upload → enrol, in that order."""

        def body() -> ToolResult:
            if not isinstance(voice_name, str) or not voice_name.strip():
                return ToolResult(
                    Status.INVALID_INPUT,
                    "voice_name 不能为空，请为克隆出的音色提供一个名称",
                )
            name = voice_name.strip()

            requested = str(model).strip() if model else self.registry.default().name
            try:
                self.registry.require(requested)
            except UnknownModelError:
                return ToolResult(
                    Status.MODEL_NOT_SUPPORTED,
                    f"未知模型 {requested!r}，请先用 list_voice_models 查看可用模型",
                    data={"model": requested, "available_models": self.registry.names()},
                )

            # 1. a local path, a data: URI, or bare base64 all become a real file.
            try:
                local = audio_utils.materialise_audio(audio_path, self.options.temp_dir)
            except ValueError as exc:
                return ToolResult(Status.INVALID_INPUT, str(exc))

            # 2. validate before uploading anything at all.
            reference = audio_utils.validate_reference(
                local,
                min_seconds=self.options.audio_min_seconds,
                max_seconds=self.options.audio_max_seconds,
                max_bytes=self.options.audio_max_bytes,
            )
            if not reference.ok:
                return reference
            if reference.data.get("duration_seconds") is None and not _content_is_recognised_audio(
                local
            ):
                return ToolResult(
                    Status.INVALID_INPUT,
                    "参考音频内容无法识别为音频文件（与扩展名不符或已损坏），"
                    "请提供有效的 wav/mp3/m4a/flac/ogg 文件",
                    data=dict(reference.data),
                )

            # 3. the gate.
            estimated = self.registry.clone_cost()
            blocked = self.ensure_balance(estimated, self.options.strict_balance)
            if blocked is not None:
                return blocked

            # 4. upload, then 5. enrol.
            url = self._client.upload_reference(local)
            prefix = _voice_prefix(name)
            result = self._client.clone_voice(url, requested, prefix, language_hints)
            if result.status is not Status.SUCCESS:
                return result

            warnings = list(result.warnings)
            warnings.extend(reference.warnings)
            warnings.extend(self._checker.warn_if_tight(estimated))
            data = {
                **result.data,
                "voice_name": name,
                "voice_prefix": result.data.get("voice_prefix") or prefix,
                "estimated_cost": result.data.get("estimated_cost", estimated),
            }

            # Persist the friendly name -> voice_id mapping (spec §11.1) so later
            # TTS calls can use `voice_name` directly. A store failure must not
            # turn a successful clone into an error.
            voice_id = result.data.get("voice_id")
            if voice_id:
                try:
                    self.voices.remember(
                        name,
                        str(voice_id),
                        prefix=prefix,
                        model=requested,
                    )
                    data["voice_alias_saved"] = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("保存音色别名失败: %s", exc)
                    warnings.append(f"音色别名未能保存到本地：{type(exc).__name__}")
                    data["voice_alias_saved"] = False

            return ToolResult(Status.SUCCESS, result.message, data=data, warnings=warnings)

        return self._guard("clone_voice", body)

    # ------------------------------------------------------------------
    # check_balance
    # ------------------------------------------------------------------
    def check_balance(self) -> ToolResult:
        """Read-only balance view.  Always a ``success`` result (spec §6.3)."""
        # The lambda keeps the attribute lookup inside the guarded body, so a
        # checker missing ``as_tool_result`` becomes ``internal_error`` too.
        return self._guard("check_balance", lambda: self._checker.as_tool_result())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Release the checker's HTTP client, ignoring a checker without one."""
        close = getattr(self._checker, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("关闭余额检查器失败", exc_info=True)

    def __enter__(self) -> "VoiceServices":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
