"""Token accounting. Run: python tests/test_usage.py"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.core import usage as usage_mod  # noqa: E402
from clio.core.usage import Usage, UsageTracker  # noqa: E402
from clio.llm.provider import FallbackLLMProvider, LLMError  # noqa: E402


class Fake:
    def __init__(self, reply="hi", tokens=(10, 5, 2)):
        self.usage = UsageTracker()
        self._reply = reply
        self._tokens = tokens

    async def stream(self, messages, tier="default"):
        p, c, r = self._tokens
        self.usage.record(Usage("fake-model", tier, p, c, r))
        yield self._reply

class Dead:
    def __init__(self):
        self.usage = UsageTracker()

    async def stream(self, messages, tier="default"):
        raise LLMError("down")
        yield  # pragma: no cover

async def main():
    t = UsageTracker()
    assert t.summary() == "No model calls yet this session."
    t.record(Usage("m", "default", 100, 50, 20))
    t.record(Usage("m", "fast", 40, 10, 0))
    assert (t.requests, t.prompt_tokens, t.completion_tokens, t.reasoning_tokens) == (2, 140, 60, 20)
    print(f"OK  totals accumulate: {t.summary()}")

    # No rate for a model means zero, not an invented number.
    assert Usage("unpriced", "default", 1_000_000, 1_000_000).cost_usd == 0.0
    print("OK  unpriced model costs 0.0 rather than guessing")

    # A rate, when one exists, is applied per million tokens.
    usage_mod._RATES["paid"] = (1.0, 2.0)
    try:
        assert Usage("paid", "default", 1_000_000, 500_000).cost_usd == 2.0
        print("OK  a known rate is applied correctly")
    finally:
        usage_mod._RATES.pop("paid")

    # Reasoning tokens are counted but never spoken, so the spoken total stays
    # prompt + completion.
    u = Usage("m", "default", 10, 27, 11)
    assert u.total_tokens == 37 and u.as_fields()["reasoning_tokens"] == 11
    print("OK  reasoning tokens tracked separately from the spoken total")

    # The fallback provider reports whichever model actually answered.
    primary, local = Fake("cloud", (10, 5, 0)), Fake("local", (7, 3, 0))
    fb = FallbackLLMProvider(primary, local)
    assert await fb.complete([]) == "cloud"
    assert fb.usage.last.prompt_tokens == 10
    fb2 = FallbackLLMProvider(Dead(), local)
    assert await fb2.complete([]) == "local"
    assert fb2.usage.last.prompt_tokens == 7, "should report the local model's numbers"
    print("OK  fallback reports the numbers of whichever model answered")

    print("\nAll usage checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
