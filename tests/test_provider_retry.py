"""Retry, backoff and fallback semantics. Run: python tests/test_provider_retry.py"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.llm.provider import (  # noqa: E402
    FallbackLLMProvider,
    LLMError,
    LLMPermanentError,
    LLMRateLimited,
)


class Scripted:
    """Yields `chunks`, raising `error` once `fail_after` of them are out."""

    def __init__(self, chunks=(), error=None, fail_after=0):
        self.chunks = list(chunks)
        self.error = error
        self.fail_after = fail_after
        self.attempts = 0

    async def stream(self, messages, tier="default"):
        self.attempts += 1
        for i, chunk in enumerate(self.chunks):
            if self.error and i >= self.fail_after:
                raise self.error
            yield chunk
        if self.error and self.fail_after >= len(self.chunks):
            raise self.error

    async def call_tool(self, messages, tools, tier="fast"):
        raise NotImplementedError


async def collect(provider):
    return "".join([c async for c in provider.stream([{"role": "user", "content": "hi"}])])


async def main():
    # Transient failure before any output: retried, then falls back.
    primary = Scripted(error=LLMError("boom"))
    assert await collect(FallbackLLMProvider(primary, Scripted(chunks=["local ", "answer"]))) == "local answer"
    assert primary.attempts == 3, primary.attempts  # initial + 2 retries
    print("OK  transient error retried then fell back")

    # Rate limit: no retries, straight to the local model that is free and up.
    primary = Scripted(error=LLMRateLimited("429"))
    assert await collect(FallbackLLMProvider(primary, Scripted(chunks=["local"]))) == "local"
    assert primary.attempts == 1, primary.attempts
    print("OK  rate limit skipped retries, fell back immediately")

    # Permanent error: no retries either.
    primary = Scripted(error=LLMPermanentError("bad model"))
    assert await collect(FallbackLLMProvider(primary, Scripted(chunks=["local"]))) == "local"
    assert primary.attempts == 1, primary.attempts
    print("OK  permanent error skipped retries")

    # Failure AFTER output: must not retry or fall back, or the caller hears the
    # reply twice with a duplicated prefix.
    primary = Scripted(chunks=["The capital ", "of France"], error=LLMError("mid"), fail_after=1)
    fallback = Scripted(chunks=["The capital of France is Paris"])
    got = ""
    try:
        async for chunk in FallbackLLMProvider(primary, fallback).stream([]):
            got += chunk
    except LLMError:
        pass
    else:
        raise AssertionError("mid-stream failure should propagate")
    assert got == "The capital ", got
    assert primary.attempts == 1 and fallback.attempts == 0
    print("OK  mid-stream failure not retried, no duplicated text")

    # Both down: one error naming both.
    both = FallbackLLMProvider(Scripted(error=LLMError("p")), Scripted(error=LLMError("f")))
    try:
        await collect(both)
    except LLMError as exc:
        assert "Primary" in str(exc) and "Fallback" in str(exc)
        print("OK  both providers down reported together")

    print("\nAll retry checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
