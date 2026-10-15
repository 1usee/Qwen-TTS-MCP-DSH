"""Local ``voice_name -> voice_id`` alias store (spec §11.1).

DashScope's voice-enrollment API requires a ``prefix`` that is lowercase
alphanumeric and shorter than 10 characters, so the user's chosen
``voice_name`` (which may be Chinese, contain spaces, or be long) cannot be sent
as-is. The name is kept here as a local alias that maps to the ``voice_id``
DashScope returns, so later TTS calls can refer to the friendly name.

The store is a plain JSON file under the config home. Corrupt or unreadable
content degrades to "no aliases" rather than raising: a broken alias file must
never prevent the server from starting.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from qwen_voice_mcp.core.config_store import config_home

logger = logging.getLogger(__name__)

VOICES_FILENAME = "voices.json"


def default_voices_path() -> Path:
    """``$QWEN_VOICE_HOME/voices.json`` (falls back to the platform config home)."""
    return config_home() / VOICES_FILENAME


class VoiceAliasStore:
    """Persist user-facing voice names alongside the ``voice_id`` they map to."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_voices_path()
        self._aliases: dict[str, dict[str, Any]] = self._load()

    # ------------------------------------------------------------------ loading

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return {}
        except OSError as exc:
            logger.warning("无法读取音色别名文件 %s: %s", self.path, exc)
            return {}

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("音色别名文件内容损坏，已忽略: %s", exc)
            return {}

        if not isinstance(payload, dict):
            logger.warning("音色别名文件格式不是对象，已忽略")
            return {}

        aliases: dict[str, dict[str, Any]] = {}
        for name, record in payload.items():
            if isinstance(record, dict) and isinstance(record.get("voice_id"), str):
                aliases[str(name)] = record
        return aliases

    # ------------------------------------------------------------------- saving

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._aliases, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            # Losing the alias is not worth failing a successful clone.
            logger.warning("无法保存音色别名文件 %s: %s", self.path, exc)

    # ------------------------------------------------------------------ public

    def remember(self, voice_name: str, voice_id: str, **extra: Any) -> None:
        """Record ``voice_name -> voice_id`` and persist the mapping."""
        if not voice_name or not voice_id:
            return
        self._aliases[voice_name] = {"voice_id": voice_id, **extra}
        self._save()

    def resolve(self, voice_name: str) -> str | None:
        """Return the ``voice_id`` for a previously cloned voice name."""
        record = self._aliases.get(voice_name)
        return record.get("voice_id") if record else None

    def get(self, voice_name: str) -> dict[str, Any] | None:
        """Return the full stored record for a voice name."""
        return self._aliases.get(voice_name)

    def all(self) -> dict[str, dict[str, Any]]:
        """Return a copy of every stored alias."""
        return {name: dict(record) for name, record in self._aliases.items()}

    def forget(self, voice_name: str) -> bool:
        """Remove an alias. Returns True when something was removed."""
        if voice_name not in self._aliases:
            return False
        del self._aliases[voice_name]
        self._save()
        return True
