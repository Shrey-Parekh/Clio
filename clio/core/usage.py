"""Token accounting per request, and a running session total.

Cost is reported only where a rate is known; the current stack bills nothing,
so `_RATES` is empty and cost is zero. Reasoning tokens are tracked separately
because gpt-oss spends them thinking without speaking them.
"""

from __future__ import annotations

from dataclasses import dataclass

# model -> (USD per 1M prompt tokens, USD per 1M completion tokens)
_RATES: dict[str, tuple[float, float]] = {}


@dataclass(frozen=True)
class Usage:
    model: str
    tier: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float:
        rate = _RATES.get(self.model)
        if rate is None:
            return 0.0
        return (self.prompt_tokens * rate[0] + self.completion_tokens * rate[1]) / 1_000_000

    def as_fields(self) -> dict:
        return {
            "model": self.model,
            "tier": self.tier,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class UsageTracker:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    last: Usage | None = None

    def record(self, usage: Usage) -> Usage:
        self.requests += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.cost_usd += usage.cost_usd
        self.last = usage
        return usage

    def summary(self) -> str:
        if not self.requests:
            return "No model calls yet this session."
        total = self.prompt_tokens + self.completion_tokens
        money = f", ${self.cost_usd:.4f}" if self.cost_usd else ", free tier"
        return (
            f"{self.requests} model calls this session, {total} tokens "
            f"({self.prompt_tokens} in, {self.completion_tokens} out{money})."
        )
