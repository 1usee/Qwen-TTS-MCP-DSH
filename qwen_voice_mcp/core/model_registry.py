"""Declared model catalogue with opt-in verification (spec §3, deviation D5).

The registry is **declaration-driven**: it renders whatever the configuration
declares and never claims a model exists upstream.  Every model therefore
starts with ``verified is None``; only an opt-in live probe
(``--verify-models``, Task 10) may flip that flag through :meth:`mark_verified`.

Dependency rule (spec §4): this module imports only the leaf modules ``config``
and :mod:`qwen_voice_mcp.core.models` plus the standard library.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import config

from qwen_voice_mcp.core.models import VoiceModel

__all__ = ["ModelRegistry", "UnknownModelError"]


class UnknownModelError(Exception):
    """Raised by :meth:`ModelRegistry.require` for an unregistered model name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown model: {name!r}")
        #: The name that was not found, kept for programmatic callers.
        self.name = name


class ModelRegistry:
    """An ordered, runtime-mutable catalogue of :class:`VoiceModel` objects.

    Parameters
    ----------
    models:
        Raw declaration dicts (or ready ``VoiceModel`` objects).  ``None`` uses
        :data:`config.DEFAULT_MODELS`; any list passed explicitly **replaces**
        the builtins entirely, preserving declaration order.
    default_model:
        Name to prefer for :meth:`default`.  ``None`` means "the first declared
        model".
    """

    def __init__(
        self,
        models: Iterable[Mapping[str, Any] | VoiceModel] | None = None,
        *,
        default_model: str | None = None,
    ) -> None:
        source = config.DEFAULT_MODELS if models is None else models
        # dict preserves insertion order, so declaration order survives both
        # `all()` and `names()`.
        self._models: dict[str, VoiceModel] = {}
        for entry in source:
            model = entry if isinstance(entry, VoiceModel) else VoiceModel(**dict(entry))
            self._models[model.name] = model
        self._default_name = default_model

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def all(self) -> list[VoiceModel]:
        """Return every registered model, in declaration order."""
        return list(self._models.values())

    def get(self, name: str) -> VoiceModel | None:
        """Return the model called ``name``, or ``None`` if it is unknown."""
        return self._models.get(name)

    def require(self, name: str) -> VoiceModel:
        """Return the model called ``name``.

        Raises
        ------
        UnknownModelError
            When no such model was declared.
        """
        model = self._models.get(name)
        if model is None:
            raise UnknownModelError(name)
        return model

    def default(self) -> VoiceModel:
        """Return the preferred default model.

        An explicit ``default_model`` that is not registered falls back to the
        first declared model, so a stale configuration cannot break the server.
        """
        if self._default_name is not None:
            model = self._models.get(self._default_name)
            if model is not None:
                return model
        if not self._models:
            raise UnknownModelError(self._default_name or config.DEFAULT_MODEL)
        return next(iter(self._models.values()))

    def names(self) -> list[str]:
        """Return every registered model name, in declaration order."""
        return list(self._models)

    # ------------------------------------------------------------------
    # Capabilities and cost
    # ------------------------------------------------------------------
    def supports(self, name: str, capability: str) -> bool:
        """True only when ``name`` is registered *and* declares ``capability``."""
        model = self._models.get(name)
        if model is None:
            return False
        return capability in model.capabilities

    def estimated_cost(self, name: str, text_length: int) -> float:
        """Estimate the declared CNY cost of synthesising ``text_length`` chars.

        Unknown model or unknown (``None``) price yields ``0.0`` — a missing
        declaration is never an exception, because the estimate only annotates a
        result and must not abort a call.
        """
        model = self._models.get(name)
        if model is None or model.pricing_per_1k_chars is None:
            return 0.0
        return round(model.pricing_per_1k_chars * text_length / 1000, 4)

    def clone_cost(self) -> float:
        """Return the declared per-call voice-cloning price (spec §3)."""
        return float(config.CLONE_UNIT_PRICE)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def mark_verified(self, name: str, verified: bool) -> None:
        """Annotate ``name`` with the outcome of an opt-in live probe.

        The stored model is replaced by a copy, so the registry stays mutable at
        runtime without ever mutating a model another caller already holds.
        """
        model = self.require(name)
        self._models[name] = model.model_copy(update={"verified": verified})
