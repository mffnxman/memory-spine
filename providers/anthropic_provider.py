"""Anthropic SDK wrapper. Lazy-imports anthropic so the module loads without
the SDK installed (we just won't be able to .generate)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import register
from providers.base import Provider, Response


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self):
        self._client = None

    def _client_lazy(self):
        if self._client is not None:
            return self._client
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError("anthropic SDK not installed. pip install anthropic") from e
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
        self._client = Anthropic(api_key=api_key)
        return self._client

    def health_check(self) -> bool:
        # Cheap check: SDK importable + API key present
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except ImportError:
            return False

    def generate(self, prompt: str, model: str, max_tokens: int = 1024, **kw) -> Response:
        client = self._client_lazy()
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **{k: v for k, v in kw.items() if k in ("system", "temperature", "top_p", "stop_sequences")},
        )
        text = "".join(getattr(b, "text", "") for b in (msg.content or []))
        usage = getattr(msg, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
        return Response(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            raw={"id": getattr(msg, "id", None), "stop_reason": getattr(msg, "stop_reason", None)},
        )


register("anthropic", AnthropicProvider)
