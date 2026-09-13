"""Provider abstract base class. All concrete providers implement this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Response:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    raw: dict | None = None


class Provider(ABC):
    name: str = ""

    @abstractmethod
    def generate(self, prompt: str, model: str, max_tokens: int = 1024, **kw) -> Response:
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """True if the provider is reachable + ready. False else."""
        ...

    def cost_estimate(self, tokens_in: int, tokens_out: int, model: str) -> float:
        """Default delegates to tier_router.PRICE_TABLE."""
        from tier_router import cost_estimate
        return cost_estimate(model, tokens_in, tokens_out)
