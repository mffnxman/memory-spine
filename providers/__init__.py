"""Provider registry — pluggable LLM backends.

Usage:
    from providers import get_provider
    prov = get_provider("anthropic")
    resp = prov.generate("hello", model="claude-haiku-4-5-20251001", max_tokens=64)
"""

from __future__ import annotations

from .base import Provider, Response  # noqa: F401

_REGISTRY: dict[str, type[Provider]] = {}


def register(name: str, cls: type[Provider]) -> None:
    _REGISTRY[name] = cls


def get_provider(name: str) -> Provider:
    if name not in _REGISTRY:
        # Lazy import to avoid loading providers we don't use
        if name == "anthropic":
            from . import anthropic_provider  # noqa: F401
        elif name == "dartagnan":
            from . import dartagnan_provider  # noqa: F401
        elif name == "dart2":
            from . import dart2_provider  # noqa: F401
        elif name == "subscription":
            from . import subscription_provider  # noqa: F401
        elif name == "mock":
            from . import mock_provider  # noqa: F401
    if name not in _REGISTRY:
        raise KeyError(f"Unknown provider: {name}")
    return _REGISTRY[name]()


def list_providers() -> list[str]:
    return sorted(_REGISTRY.keys())
