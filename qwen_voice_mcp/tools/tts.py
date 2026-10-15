"""``synthesize_speech`` adapter (spec §12).

Thin by design: the balance gate, the input validation, and the DashScope call
all live in :class:`~qwen_voice_mcp.core.services.VoiceServices`.  There is no
``try/except`` here — the service layer guarantees a ``ToolResult``, and
``tools/__init__.py`` adds the final outer guard required by spec §12.
"""

from __future__ import annotations

from typing import Any

from qwen_voice_mcp.core.services import VoiceServices

__all__ = ["synthesize_speech"]


def synthesize_speech(
    services: VoiceServices,
    text: str,
    model: str | None = None,
    voice: str | None = None,
    output_format: str | None = None,
) -> dict[str, Any]:
    """Synthesise ``text`` to a fresh file inside the configured output directory."""
    return services.synthesize_speech(
        text, model=model, voice=voice, output_format=output_format
    ).to_dict()
