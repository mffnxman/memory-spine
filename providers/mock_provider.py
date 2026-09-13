"""Mock provider — for tests + fail-open fallback when no real provider available."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import register
from providers.base import Provider, Response


class MockProvider(Provider):
    name = "mock"

    def health_check(self) -> bool:
        return True

    def generate(self, prompt: str, model: str = "mock", max_tokens: int = 1024, **kw) -> Response:
        # Deterministic echo for test reproducibility.
        text = f"[mock:{model}] " + (prompt[:200].replace("\n", " "))
        return Response(text=text, tokens_in=len(prompt) // 4, tokens_out=len(text) // 4, model=model)


register("mock", MockProvider)
