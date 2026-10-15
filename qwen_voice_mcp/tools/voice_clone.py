"""``clone_voice`` adapter (spec §12).

``audio_path`` may be a local path, a ``data:`` URI, or bare base64 — the
decoding and validation order (materialise → validate → gate → upload → enrol)
is owned by :class:`~qwen_voice_mcp.core.services.VoiceServices`, so this adapter
only forwards arguments.
"""

from __future__ import annotations

from typing import Any

from qwen_voice_mcp.core.services import VoiceServices

__all__ = ["clone_voice"]


def clone_voice(
    services: VoiceServices,
    audio_path: str,
    voice_name: str,
    model: str | None = None,
    language_hints: list[str] | None = None,
) -> dict[str, Any]:
    """Enrol ``voice_name`` from the reference clip at ``audio_path``."""
    return services.clone_voice(
        audio_path, voice_name, model=model, language_hints=language_hints
    ).to_dict()
