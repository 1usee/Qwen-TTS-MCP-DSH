"""``check_balance`` adapter (spec §12).

Read-only: the probe is never billable, so an unreachable balance endpoint is a
``success`` carrying ``balance: null`` plus a warning (spec §6.3, §7.2).
"""

from __future__ import annotations

from typing import Any

from qwen_voice_mcp.core.services import VoiceServices

__all__ = ["check_balance"]


def check_balance(services: VoiceServices) -> dict[str, Any]:
    """Return the current balance view."""
    return services.check_balance().to_dict()
