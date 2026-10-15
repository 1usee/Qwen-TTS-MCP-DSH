"""The only module that talks to the DashScope SDK (spec §11).

Design rules that shape this file:

* **Import isolation (Global Constraints).** ``tools/**`` must never import
  ``dashscope``; everything goes through this wrapper. The import happens inside
  a module-level ``try`` and binds :data:`SpeechSynthesizer`,
  :data:`VoiceEnrollmentService`, :data:`AudioFormat` and :data:`Files` as plain
  module globals, so ``import qwen_voice_mcp.core.qwen_client`` never hard-fails
  and tests can monkeypatch those exact names.
* **Percent-encoding safety is invisible here.** Every failure path is mapped
  through :func:`qwen_voice_mcp.core.errors.classify`, so a caller only ever sees
  a :class:`~qwen_voice_mcp.core.models.Status`.
* **No lying.** ``clone_voice`` reports ``success`` only when DashScope returned
  a ``voice_id``; an exception from ``create_voice`` is never downgraded to a
  success, and a ``voice_id`` whose enrolment query keeps failing is reported with
  an honest ``cloning_status`` of ``"pending"``/``"unknown"`` — never
  ``"completed"``.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

import config

from qwen_voice_mcp.core import audio_utils
from qwen_voice_mcp.core.errors import classify, safe_message
from qwen_voice_mcp.core.model_registry import ModelRegistry, UnknownModelError
from qwen_voice_mcp.core.models import Status, ToolResult

__all__ = [
    "FORMAT_MAP",
    "ENROLL_MAX_POLLS",
    "ENROLL_POLL_INTERVAL_SECONDS",
    "QwenClient",
    "make_voice_prefix",
]

# ---------------------------------------------------------------------------
# SDK binding — module globals so tests can monkeypatch them by name
# ---------------------------------------------------------------------------
SDK_AVAILABLE = False
SDK_IMPORT_ERROR: str | None = None
SpeechSynthesizer: Any = None
VoiceEnrollmentService: Any = None
AudioFormat: Any = None
Files: Any = None

try:
    import dashscope
    from dashscope import Files as _Files
    from dashscope.audio.tts_v2 import AudioFormat as _AudioFormat
    # Both live in ``dashscope.audio.tts_v2``; there is no
    # ``dashscope.audio.voice_enrollment`` module. Importing them separately -- and
    # never in one shared try block -- keeps one missing name from nulling the
    # others, which is what previously disabled TTS entirely.
    from dashscope.audio.tts_v2 import SpeechSynthesizer as _SpeechSynthesizer
    from dashscope.audio.tts_v2 import VoiceEnrollmentService as _VoiceEnrollmentService

    SpeechSynthesizer = _SpeechSynthesizer
    VoiceEnrollmentService = _VoiceEnrollmentService
    AudioFormat = _AudioFormat
    Files = _Files
except Exception as exc:  # only on a genuinely broken install
    SDK_IMPORT_ERROR = str(exc)
else:
    SDK_AVAILABLE = True

#: ``output_format`` → ``AudioFormat`` member (spec §11). Declared even when the
#: SDK is missing, so callers can validate input without a hard dependency.
FORMAT_MAP: dict[str, Any] = {
    "mp3": getattr(AudioFormat, "MP3_24000HZ_MONO_256KBPS", "mp3"),
    "wav": getattr(AudioFormat, "WAV_16000HZ_MONO_16BIT", "wav"),
    "pcm": getattr(AudioFormat, "PCM_16000HZ_MONO_16BIT", "pcm"),
    "opus": getattr(AudioFormat, "OGG_OPUS_24KHZ_MONO_32KBPS", "opus"),
}

#: Voice enrolment is asynchronous upstream; poll a few times, then report the
#: real state instead of inventing a "completed".
ENROLL_MAX_POLLS = 3
ENROLL_POLL_INTERVAL_SECONDS = 1.0

#: DashScope accepts ``prefix`` only when short, lowercase and alphanumeric
#: (spec deviation D3/D4). Nine is the hard ceiling.
_PREFIX_MAX_LENGTH = 9
_PREFIX_HASH_LENGTH = 3

#: Legal characters for a voice name.
_PREFIX_LEGAL = re.compile(r"[^a-z0-9]")


def make_voice_prefix(voice_name: str) -> str:
    """Derive a legal DashScope voice ``prefix`` from an arbitrary local name.

    DashScope rejects a ``prefix`` that is not lowercase alphanumeric and shorter
    than 10 characters, so a Chinese name ("用户自定义音色"), an uppercase name, a
    name with spaces or a 40-character name all have to collapse into the same
    tiny safe alphabet. A short hash suffix is appended so two similar names
    (``"voice one"`` / ``"voice two"``) never collide, and the whole thing stays
    deterministic for a given input.

    A name with no usable ``[a-z0-9]`` character at all falls back to a hash-only
    prefix, because the spec requires the local alias to round-trip the user's
    name elsewhere (``voices.json``) — the prefix only has to be legal.
    """
    name = voice_name or ""
    cleaned = _PREFIX_LEGAL.sub("", name.lower())
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()

    if not cleaned:
        # Nothing survived sanitising: build the whole prefix from the hash.
        return ("qv" + digest[:_PREFIX_MAX_LENGTH - 2])[:_PREFIX_MAX_LENGTH]

    prefix = cleaned[:6] + digest[:_PREFIX_HASH_LENGTH]
    if len(prefix) < 3:  # pragma: no cover - unreachable with a hash suffix
        prefix = "qv" + digest[:_PREFIX_MAX_LENGTH - 2]
    if prefix[0].isdigit():
        # DashScope documents a prefix as starting with a lowercase letter, so a
        # purely numeric name ("音色-2026" -> "2026…") gets a safe lead-in char.
        prefix = "q" + prefix
    return prefix[:_PREFIX_MAX_LENGTH]


def _audio_format_for(output_format: str) -> Any:
    """Return the SDK ``AudioFormat`` value for ``output_format``.

    Kept as a module-level function (not a method) so tests can patch it.
    """
    return FORMAT_MAP[output_format]


class QwenClient:
    """Thin, offline-testable wrapper around the DashScope speech SDK.

    Parameters
    ----------
    api_key:
        Explicit key. ``None`` falls back to ``DASHSCOPE_API_KEY`` and then to the
        vault (via :meth:`resolve_api_key`).
    registry:
        The declared model catalogue; used to reject unknown models and to
        annotate results with a cost estimate.
    timeout:
        Per-call timeout in seconds, converted to ``timeout_millis`` for the SDK.
    """

    def __init__(
        self,
        api_key: str | None,
        registry: ModelRegistry | None = None,
        *,
        timeout: float = 60.0,
        secret_manager: Any = None,
    ) -> None:
        self.api_key = api_key
        self.registry = registry if registry is not None else ModelRegistry()
        self.timeout = float(timeout)
        self.secret_manager = secret_manager

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def resolve_api_key(self) -> str | None:
        """Resolve the key: explicit argument → env → vault (spec §11)."""
        if self.api_key:
            return self.api_key
        from_env = os.environ.get(config.API_KEY_ENV)
        if from_env:
            return from_env
        if self.secret_manager is not None:
            try:
                stored = self.secret_manager.retrieve(config.API_KEY_ENV)
            except Exception:
                stored = None
            if stored:
                return str(stored)
        return None

    # ------------------------------------------------------------------
    # Text to speech
    # ------------------------------------------------------------------
    def synthesize(
        self,
        text: str,
        model: str,
        voice: str,
        output_format: str,
        output_path: Path,
    ) -> ToolResult:
        """Synthesise ``text`` to ``output_path`` (spec §6.3 successful shape)."""
        fmt = (output_format or "").strip().lower().lstrip(".")
        if fmt not in FORMAT_MAP:
            supported = "/".join(sorted(FORMAT_MAP))
            return ToolResult(
                Status.INVALID_INPUT,
                f"不支持的音频格式 {output_format!r}，仅支持：{supported}",
                data={"output_format": output_format, "supported_formats": sorted(FORMAT_MAP)},
            )

        if not isinstance(text, str) or not text.strip():
            return ToolResult(Status.INVALID_INPUT, "待合成的文本为空，请提供非空白文本")

        try:
            self.registry.require(model)
        except UnknownModelError:
            return ToolResult(
                Status.MODEL_NOT_SUPPORTED,
                f"未知模型 {model!r}，请用 list_voice_models 查看可用模型",
                data={"model": model, "available_models": self.registry.names()},
            )

        api_key = self.resolve_api_key()
        if not api_key:
            return ToolResult(
                Status.AUTH_ERROR,
                f"未配置 DashScope API Key（环境变量 {config.API_KEY_ENV} 或加密密钥库中均未找到）",
            )

        target = Path(output_path)
        if target.parent and not target.parent.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                status, message = classify(exc)
                return ToolResult(status, message, data={"audio_path": str(target)})

        if SpeechSynthesizer is None:
            return ToolResult(
                Status.INTERNAL_ERROR,
                f"dashscope SDK 不可用：{SDK_IMPORT_ERROR or '未能导入 SpeechSynthesizer'}",
            )

        try:
            synthesizer = SpeechSynthesizer(
                model=model,
                voice=voice,
                format=_audio_format_for(fmt),
            )
            audio = synthesizer.call(text, timeout_millis=self._timeout_millis())
        except Exception as exc:
            status, message = classify(exc)
            return ToolResult(status, message, data={"model_used": model, "voice_used": voice})

        if not audio:
            return ToolResult(
                Status.UPSTREAM_ERROR,
                "上游未返回音频数据（空响应）",
                data={"model_used": model, "voice_used": voice, "audio_format": fmt},
            )

        try:
            target.write_bytes(audio)
        except OSError as exc:
            status, message = classify(exc)
            return ToolResult(status, message, data={"audio_path": str(target)})

        info = audio_utils.probe(target)
        return ToolResult(
            Status.SUCCESS,
            "语音合成完成",
            data={
                "audio_path": str(target),
                "audio_format": fmt,
                "duration_seconds": info.duration_seconds,
                "text_length": len(text),
                "model_used": model,
                "voice_used": voice,
                "estimated_cost": self.registry.estimated_cost(model, len(text)),
            },
        )

    # ------------------------------------------------------------------
    # Reference upload
    # ------------------------------------------------------------------
    def upload_reference(self, path: Path | str) -> str:
        """Upload a reference clip and return a URL (spec deviation D3).

        Already-remote references (``http(s)://``) are passed through unchanged,
        so a caller that already has a URL needs no upload at all.
        """
        text = str(path or "").strip()
        if text.lower().startswith(("http://", "https://")):
            return text
        if not text:
            raise ValueError("参考音频路径为空，无法上传")

        target = Path(text)
        if not target.is_file():
            raise FileNotFoundError(f"参考音频不存在：{target}")

        if Files is None:
            raise RuntimeError("dashscope SDK 不可用：无法上传参考音频")

        uploaded = Files.upload(file_path=str(target), purpose="voice-enrollment")
        url = self._extract_url(uploaded)
        if not url:
            raise RuntimeError(f"上传参考音频成功但未返回可用 URL：{safe_message(str(uploaded))}")
        return url

    @staticmethod
    def _extract_url(uploaded: Any) -> str | None:
        """Pull a usable URL (or file id) out of whatever ``Files.upload`` gave."""
        candidates = (
            getattr(uploaded, "url", None),
            getattr(getattr(uploaded, "output", None), "url", None),
            getattr(getattr(uploaded, "data", None), "url", None),
            getattr(uploaded, "file_id", None),
            getattr(getattr(uploaded, "output", None), "file_id", None),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if isinstance(uploaded, dict):
            for key in ("url", "file_url", "file_id"):
                candidate = uploaded.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        if isinstance(uploaded, str) and uploaded.strip():
            return uploaded.strip()
        return None

    # ------------------------------------------------------------------
    # Voice cloning
    # ------------------------------------------------------------------
    def clone_voice(
        self,
        url: str,
        target_model: str,
        prefix: str,
        language_hints: list[str] | None = None,
    ) -> ToolResult:
        """Enrol a cloned voice from ``url`` and poll its status.

        A failure from ``create_voice`` is mapped through
        :func:`~qwen_voice_mcp.core.errors.classify` and reported as a failure —
        never as a success with an invented status.
        """
        try:
            self.registry.require(target_model)
        except UnknownModelError:
            return ToolResult(
                Status.MODEL_NOT_SUPPORTED,
                f"未知模型 {target_model!r}，无法用于声音克隆",
                data={"model": target_model, "available_models": self.registry.names()},
            )

        api_key = self.resolve_api_key()
        if not api_key:
            return ToolResult(
                Status.AUTH_ERROR,
                f"未配置 DashScope API Key（环境变量 {config.API_KEY_ENV} 或加密密钥库中均未找到）",
            )

        if VoiceEnrollmentService is None:
            return ToolResult(
                Status.INTERNAL_ERROR,
                f"dashscope SDK 不可用：{SDK_IMPORT_ERROR or '未能导入 VoiceEnrollmentService'}",
            )

        try:
            service = VoiceEnrollmentService(api_key=api_key)
            voice_id = service.create_voice(
                target_model=target_model,
                prefix=prefix,
                url=url,
                language_hints=language_hints,
            )
        except Exception as exc:
            status, message = classify(exc)
            return ToolResult(
                status,
                message,
                data={"model_used": target_model, "voice_prefix": prefix, "estimated_cost": self.registry.clone_cost()},
            )

        if not voice_id:
            return ToolResult(
                Status.UPSTREAM_ERROR,
                "声音克隆请求已提交但上游未返回 voice_id",
                data={"model_used": target_model, "voice_prefix": prefix, "cloning_status": "unknown"},
            )

        cloning_status, poll_error = self._poll_enrollment(service, str(voice_id))
        data: dict[str, Any] = {
            "voice_id": str(voice_id),
            "model_used": target_model,
            "voice_prefix": prefix,
            "cloning_status": cloning_status,
            "estimated_cost": self.registry.clone_cost(),
        }
        if poll_error:
            data["status_detail"] = poll_error

        if cloning_status == "completed":
            return ToolResult(Status.SUCCESS, "声音克隆完成", data=data)
        return ToolResult(
            Status.SUCCESS,
            "声音克隆请求已提交，上游尚未返回完成状态，可稍后用 voice_id 查询",
            data=data,
            warnings=[poll_error] if poll_error else [],
        )

    def _poll_enrollment(self, service: Any, voice_id: str) -> tuple[str, str | None]:
        """Poll ``query_voice`` up to :data:`ENROLL_MAX_POLLS` times.

        Returns ``(cloning_status, detail)`` where the status is one of
        ``"completed"``, ``"processing"`` or ``"unknown"`` — never a claim the
        upstream did not make.
        """
        last_error: str | None = None
        for attempt in range(ENROLL_MAX_POLLS):
            try:
                result = service.query_voice(voice_id)
            except Exception as exc:
                last_error = safe_message(exc)
                result = None
            if result:
                joined = " ".join(str(item) for item in result) if isinstance(result, (list, tuple)) else str(result)
                if "ok" in joined.lower() or "success" in joined.lower():
                    return "completed", None
                return "processing", last_error
            if attempt < ENROLL_MAX_POLLS - 1:
                time.sleep(ENROLL_POLL_INTERVAL_SECONDS)
        return "unknown", last_error

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def query_voice(self, voice_id: str) -> dict[str, Any] | None:
        """Return the upstream status dict for ``voice_id``, or ``None`` on error."""
        if not voice_id:
            return None
        api_key = self.resolve_api_key()
        if not api_key or VoiceEnrollmentService is None:
            return None
        try:
            service = VoiceEnrollmentService(api_key=api_key)
            result = service.query_voice(voice_id)
        except Exception:
            return None
        if result is None:
            return None
        if isinstance(result, dict):
            return {"voice_id": voice_id, "status_code": 200, **result}
        if isinstance(result, (list, tuple)):
            items = [str(item) for item in result]
            return {"voice_id": voice_id, "status_code": 200, "results": items}
        return {"voice_id": voice_id, "status_code": 200, "result": str(result)}

    def verify_model(self, model: str) -> bool:
        """True only when the live SDK accepts ``model`` for speech synthesis.

        An opt-in probe (``--verify-models``); any failure — including a missing
        key or an offline machine — means "not verified", never an exception.
        """
        if not model:
            return False
        api_key = self.resolve_api_key()
        if not api_key or SpeechSynthesizer is None:
            return False
        try:
            synthesizer = SpeechSynthesizer(model=model, voice=config.DEFAULT_VOICE)
            audio = synthesizer.call("你好", timeout_millis=self._timeout_millis())
        except Exception:
            return False
        return bool(audio)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _timeout_millis(self) -> int:
        """Convert :attr:`timeout` (seconds) to the SDK's ``timeout_millis``."""
        return max(1, int(self.timeout * 1000))
