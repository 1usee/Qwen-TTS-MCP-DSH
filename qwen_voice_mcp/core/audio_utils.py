"""Audio probing, validation, and temp-file management (spec §10).

Backend chain (spec §10.1)::

    soundfile (libsndfile)  ->  pydub (only when ffmpeg exists)  ->  stdlib

Two invariants matter more than completeness:

* Every probe is **best effort**. An unreadable, empty, or unrecognised
  container yields ``duration_seconds=None`` instead of raising, because the
  callers must degrade to a *warning* and never to a false failure (spec §10.2:
  "m4a/aac 若后端解析不出时长 → 仅报 format 并附 warning，不误判失败").
* ``make_tone_wav`` uses only the standard library (``wave`` + ``math`` +
  ``struct``) so the test suite never depends on an audio backend.

``materialise_audio`` is the spec §12 entry point for a ``clone_voice`` caller
that sends an ``audio_path`` as a filesystem path, a ``data:`` URI, or bare
base64 — a 100 KB base64 string must never reach ``Path(...)`` as if it were a
filename (plan "Review Focus" item 1).
"""

from __future__ import annotations

import base64
import binascii
import importlib
import importlib.util
import math
import os
import secrets
import shutil
import struct
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from qwen_voice_mcp.core.models import AudioProbe, Status, ToolResult

__all__ = [
    "cleanup_temp_files",
    "detect_backend",
    "make_tone_wav",
    "materialise_audio",
    "new_output_path",
    "probe",
    "validate_reference",
]

#: Number of leading bytes read for magic-number classification.
_MAGIC_HEAD_LENGTH = 16

#: MPEG-1 Layer III bitrate table (kbps), indexed by the frame header's
#: bitrate_index. Index 0 and 15 are reserved ("free"/"bad"), hence the 0.0.
_MP3_BITRATE_KBPS = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
)

#: MPEG-1 sample rates, indexed by the frame header's sample_rate_index.
_MP3_SAMPLE_RATES = (44100, 48000, 32000)

#: How much of the file the stdlib MP3 frame scanner inspects.
_MP3_SCAN_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
def _importable(module_name: str) -> bool:
    """Return True when ``module_name`` can actually be imported.

    A broken or partially installed optional backend must degrade the chain, not
    the process.
    """
    try:
        importlib.import_module(module_name)
    except Exception:
        return False
    return True


def _present(module_name: str) -> bool:
    """Return True when ``module_name`` exists without executing it.

    Used for the *fallback* tier so that a healthy preferred backend never pays
    for importing pydub (whose import emits an unrelated ``audioop``
    DeprecationWarning on Python 3.11+).
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _ffmpeg_available() -> bool:
    """True when an ``ffmpeg`` executable is on PATH (pydub's hard requirement)."""
    try:
        return bool(shutil.which("ffmpeg"))
    except Exception:
        return False


def _backend_chain() -> list[str]:
    """Return the available probing backends in priority order (spec §10.1)."""
    chain: list[str] = []
    if _importable("soundfile"):
        chain.append("soundfile")
    if _ffmpeg_available() and _present("pydub"):
        chain.append("pydub")
    chain.append("stdlib")
    return chain


def detect_backend() -> str:
    """Return the preferred audio backend: ``soundfile``, ``pydub`` or ``stdlib``."""
    for name in _backend_chain():
        if name != "stdlib":
            return name
    return "stdlib"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _extension(path: Path) -> str:
    """Return the lowercase extension without the dot ('' when there is none)."""
    return path.suffix.lower().lstrip(".")


def _safe_size(path: Path) -> int:
    """Return the file size in bytes, or 0 when it cannot be stat-ed."""
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _read_head(path: Path, length: int = _MAGIC_HEAD_LENGTH) -> bytes:
    """Return the first ``length`` bytes of ``path`` (b'' when unreadable)."""
    try:
        with path.open("rb") as handle:
            return handle.read(length)
    except OSError:
        return b""


def _sniff_extension(head: bytes) -> str | None:
    """Classify ``head`` by magic number, returning a canonical extension.

    Used both to pick an extension for a decoded base64 payload and to catch a
    renamed file whose content disagrees with its suffix (spec §10.2
    "后缀 + 魔数双重校验"). ``None`` means "not recognised" and is always treated
    as *unknown*, never as a mismatch.
    """
    if head[:4] == b"RIFF":
        return "wav"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    return None


def _extension_from_data_uri(value: str) -> str | None:
    """Extract a usable extension from a ``data:audio/<fmt>;base64,...`` URI."""
    if not value.startswith("data:"):
        return None
    meta = value[5:].split(",", 1)[0]
    mime = meta.split(";", 1)[0].strip().lower()
    if "/" not in mime:
        return None
    subtype = mime.rsplit("/", 1)[1]
    if subtype in {"mpeg", "mp3"}:
        return "mp3"
    if subtype in {"x-m4a", "mp4", "m4a"}:
        return "m4a"
    if subtype in {"x-wav", "wave", "wav"}:
        return "wav"
    if subtype in {"x-flac", "flac"}:
        return "flac"
    if subtype in {"ogg", "opus"}:
        return "ogg"
    if subtype in {"aac", "x-aac"}:
        return "aac"
    return None


# ---------------------------------------------------------------------------
# Per-backend probes: each returns (duration, sample_rate, channels) or None
# ---------------------------------------------------------------------------
def _probe_soundfile(path: Path) -> tuple[float, int, int | None] | None:
    """Probe with libsndfile (native WAV/MP3/FLAC/OGG/AIFF support)."""
    import soundfile  # imported lazily: optional dependency

    info = soundfile.info(str(path))
    samplerate = int(info.samplerate or 0)
    channels = int(info.channels or 0)
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    if duration <= 0 and samplerate > 0 and info.frames:
        duration = float(info.frames) / samplerate
    if duration <= 0 or samplerate <= 0:
        return None
    return duration, samplerate, channels or None


def _probe_pydub(path: Path) -> tuple[float, int, int | None] | None:
    """Probe with pydub (needs ffmpeg; only reached when it is on PATH)."""
    from pydub import AudioSegment  # imported lazily: optional dependency

    segment = AudioSegment.from_file(str(path))
    duration = len(segment) / 1000.0
    if duration <= 0:
        return None
    return duration, int(segment.frame_rate), int(segment.channels) or None


def _probe_wave_stdlib(path: Path) -> tuple[float, int, int] | None:
    """Probe a RIFF/WAVE container with the stdlib ``wave`` module."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes()
        channels = handle.getnchannels()
    if rate <= 0 or frames <= 0:
        return None
    return frames / rate, rate, channels


def _find_mp3_sync(head: bytes, start: int) -> int | None:
    """Return the offset of the next MPEG audio frame sync at/after ``start``."""
    index = head.find(b"\xff", start)
    while index != -1:
        if index + 3 < len(head) and (head[index + 1] & 0xE0) == 0xE0:
            return index
        index = head.find(b"\xff", index + 1)
    return None


def _probe_mp3_naive(path: Path) -> tuple[float, int, int | None] | None:
    """Estimate MP3 duration from the first MPEG-1 Layer III frame header.

    Duration = remaining bytes * 8 / bitrate. Good enough to validate the 5–60 s
    window; a container we cannot parse returns ``None`` instead of a guess.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(_MP3_SCAN_BYTES)
    except OSError:
        return None

    offset = 0
    if head[:3] == b"ID3" and len(head) >= 10:
        tag_size = (
            ((head[6] & 0x7F) << 21)
            | ((head[7] & 0x7F) << 14)
            | ((head[8] & 0x7F) << 7)
            | (head[9] & 0x7F)
        )
        offset = 10 + tag_size

    index = _find_mp3_sync(head, offset)
    while index is not None:
        version = (head[index + 1] >> 3) & 0x03
        layer = (head[index + 1] >> 1) & 0x03
        bitrate_index = (head[index + 2] >> 4) & 0x0F
        rate_index = (head[index + 2] >> 2) & 0x03
        if version == 3 and layer == 1 and 0 < bitrate_index < 15 and rate_index != 3:
            bitrate = _MP3_BITRATE_KBPS[bitrate_index] * 1000
            if bitrate > 0 and size > index:
                return (size - index) * 8 / bitrate, _MP3_SAMPLE_RATES[rate_index], None
        index = _find_mp3_sync(head, index + 1)
    return None


def _probe_stdlib(path: Path) -> tuple[float, int, int | None] | None:
    """Last-resort probe: stdlib ``wave`` for WAV, frame scan for MP3."""
    extension = _extension(path)
    if extension == "wav" or _sniff_extension(_read_head(path, 4)) == "wav":
        try:
            found = _probe_wave_stdlib(path)
        except Exception:
            found = None
        if found is not None:
            return found
    if extension == "mp3":
        return _probe_mp3_naive(path)
    return None


_PROBERS: dict[str, Any] = {
    "soundfile": _probe_soundfile,
    "pydub": _probe_pydub,
    "stdlib": _probe_stdlib,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def probe(path: Path) -> AudioProbe:
    """Describe ``path`` as well as the available backends allow.

    Never raises for a missing, unreadable, or non-audio file: unknown fields
    stay ``None`` and ``backend`` records which probe (or the detection result)
    produced the answer.
    """
    target = Path(path)
    extension = _extension(target)
    chain = _backend_chain()
    backend = chain[0]
    duration: float | None = None
    sample_rate: int | None = None
    channels: int | None = None

    try:
        present = target.is_file()
    except OSError:
        present = False
    if not present:
        # Nothing to decode: do not spawn ffprobe/ffmpeg for a path that is not
        # a regular file. The probe is still returned, with unknown fields.
        return AudioProbe(
            path=target,
            format=extension or "unknown",
            size_bytes=_safe_size(target),
            backend=backend,
        )

    for name in chain:
        try:
            found = _PROBERS[name](target)
        except Exception:
            # Any backend failure (LibsndfileError, CouldntDecodeError, wave.Error,
            # a missing optional import, ...) just moves on to the next backend.
            found = None
        if found is not None:
            duration, sample_rate, channels = found
            backend = name
            break

    return AudioProbe(
        path=target,
        format=extension or "unknown",
        duration_seconds=duration,
        sample_rate=sample_rate,
        channels=channels,
        size_bytes=_safe_size(target),
        backend=backend,
    )


def make_tone_wav(path: Path, seconds: float, sample_rate: int = 16000) -> Path:
    """Write a mono 16-bit sine WAV using only ``wave`` + ``math`` + ``struct``.

    Tests use this so they never depend on an audio backend being installed.
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    frames = max(0, int(round(float(seconds) * sample_rate)))
    amplitude = 0.25 * 32767.0
    step = 2.0 * math.pi * 440.0 / sample_rate
    payload = bytearray()
    for index in range(frames):
        payload += struct.pack("<h", int(amplitude * math.sin(step * index)))

    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(payload))
    return target


def materialise_audio(value: str, temp_dir: Path) -> Path:
    """Return a real file path for ``value``.

    Accepts, in order: an existing filesystem path (returned unchanged), a
    ``data:`` URI (the part after the comma is decoded), or bare base64. Decoded
    bytes are written into ``temp_dir`` with an extension sniffed from the magic
    number, so a 100 KB base64 string never reaches ``Path(...)`` as a filename.
    """
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("audio_path 为空：请提供音频文件的本地路径，或 data: URI / base64 音频数据")

    text = value.strip()

    candidate = Path(text)
    try:
        if candidate.is_file():
            return candidate
    except (OSError, ValueError):
        pass  # not a usable path (too long, illegal characters, ...) -> try base64

    message = "audio_path 既不是存在的文件，也不是合法的 base64 音频数据"
    if text.startswith("data:"):
        _, _, payload = text.partition(",")
        if not payload:
            raise ValueError("audio_path 的 data: URI 缺少逗号后的 base64 数据")
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(message) from exc
    else:
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(message) from exc

    if not raw:
        raise ValueError("audio_path 解码后为空，无法作为参考音频")

    extension = _sniff_extension(raw) or _extension_from_data_uri(text) or "bin"
    directory = Path(temp_dir)
    directory.mkdir(parents=True, exist_ok=True)
    materialised = directory / f"ref_{secrets.token_hex(4)}.{extension}"
    materialised.write_bytes(raw)
    return materialised


def validate_reference(
    path: Path,
    *,
    min_seconds: float | None = None,
    max_seconds: float | None = None,
    max_bytes: int | None = None,
) -> ToolResult:
    """Validate a reference clip: exists → size → extension → magic → duration.

    A file whose duration cannot be determined is a **success with a warning**,
    never a failure (spec §10.2).
    """
    target = Path(path)
    minimum = float(config.AUDIO_MIN_SECONDS if min_seconds is None else min_seconds)
    maximum = float(config.AUDIO_MAX_SECONDS if max_seconds is None else max_seconds)
    byte_limit = int(config.AUDIO_MAX_BYTES if max_bytes is None else max_bytes)

    try:
        size = target.stat().st_size
    except OSError:
        return ToolResult(
            Status.INVALID_INPUT,
            f"参考音频不存在或不可读：{target.name}",
        )
    if not target.is_file():
        return ToolResult(Status.INVALID_INPUT, f"参考音频不是普通文件：{target.name}")
    if size <= 0:
        return ToolResult(Status.INVALID_INPUT, f"参考音频是空文件：{target.name}")
    if size > byte_limit:
        return ToolResult(
            Status.INVALID_INPUT,
            f"参考音频大小 {size} 字节（{size / (1024 * 1024):.2f} MB），"
            f"超过上限 {byte_limit} 字节（{byte_limit / (1024 * 1024):.2f} MB）",
        )

    extension = _extension(target)
    if extension not in config.AUDIO_ALLOWED_EXTENSIONS:
        allowed = "/".join(config.AUDIO_ALLOWED_EXTENSIONS)
        return ToolResult(
            Status.INVALID_INPUT,
            f"不支持的音频格式 .{extension or '(无扩展名)'}，仅支持：{allowed}",
        )

    sniffed = _sniff_extension(_read_head(target))
    if sniffed is not None and sniffed != extension:
        return ToolResult(
            Status.INVALID_INPUT,
            f"参考音频内容（{sniffed}）与扩展名 .{extension} 不一致，请确认文件未被改名",
        )

    info = probe(target)
    warnings: list[str] = []
    if info.sample_rate is not None and not 8000 <= info.sample_rate <= 48000:
        warnings.append(
            f"采样率 {info.sample_rate} Hz 超出推荐的 8000–48000 Hz，可能影响克隆效果"
        )

    data: dict[str, Any] = {
        "audio_format": info.format,
        "duration_seconds": info.duration_seconds,
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "size_bytes": size,
        "backend": info.backend,
    }

    if info.duration_seconds is None:
        warnings.append(
            f"无法解析音频时长（后端 {info.backend}），已跳过 {minimum:g}–{maximum:g} 秒的时长校验"
        )
        return ToolResult(
            Status.SUCCESS,
            "参考音频校验通过（时长未知，已跳过时长范围校验）",
            data=data,
            warnings=warnings,
        )

    duration = float(info.duration_seconds)
    if duration < minimum:
        return ToolResult(
            Status.INVALID_INPUT,
            f"参考音频时长 {duration:.2f} 秒，短于最小要求 {minimum:g} 秒",
            data={"duration_seconds": duration, "min_seconds": minimum, "max_seconds": maximum},
        )
    if duration > maximum:
        return ToolResult(
            Status.INVALID_INPUT,
            f"参考音频时长 {duration:.2f} 秒，超过上限 {maximum:g} 秒",
            data={"duration_seconds": duration, "min_seconds": minimum, "max_seconds": maximum},
        )

    return ToolResult(
        Status.SUCCESS,
        f"参考音频校验通过（{info.format}，{duration:.2f} 秒）",
        data=data,
        warnings=warnings,
    )


def new_output_path(output_dir: Path, fmt: str) -> Path:
    """Return ``qwen_tts_<UTC timestamp>_<4 hex>.<fmt>`` inside ``output_dir``."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    clean = (fmt or config.DEFAULT_OUTPUT_FORMAT).strip().lower().lstrip(".")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return directory / f"qwen_tts_{stamp}_{secrets.token_hex(2)}.{clean}"


def cleanup_temp_files(directory: Path, max_age_hours: float = 24.0) -> int:
    """Delete regular files older than ``max_age_hours``, returning the count."""
    root = Path(directory)
    cutoff = time.time() - float(max_age_hours) * 3600.0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0

    removed = 0
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed
