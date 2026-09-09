"""LLM provider behind one interface: streaming, tiered, with retry/backoff and a
local fallback when the primary is unreachable. Every call goes through
FallbackLLMProvider, so the network-down path is exercised by construction."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from clio.core.config import Config
from clio.core.logging import get_logger
from clio.core.usage import Usage, UsageTracker

log = get_logger("clio.llm.provider")

Message = dict[str, str]  # {"role": "user"|"assistant"|"system", "content": "..."}

# Runaway backstop only. Set well above a normal spoken reply, because a cap
# tight enough to enforce brevity truncates mid-sentence. Brevity is the
# persona's job.
_MAX_SPOKEN_TOKENS = 320

_MAX_RETRIES = 2
_BASE_BACKOFF_S = 0.5
_REQUEST_TIMEOUT_S = 20.0


class LLMError(Exception):
    """A provider call failed. Transient by default - worth retrying."""


class LLMRateLimited(LLMError):
    """Rate limited — retrying won't clear it, so fall back instead of waiting."""


class LLMPermanentError(LLMError):
    """Failed in a way retrying can't fix (bad model, auth, malformed request);
    FallbackLLMProvider skips the backoff and falls back immediately."""


class LLMProvider(ABC):
    @abstractmethod
    def stream(self, messages: list[Message], tier: str = "default") -> AsyncIterator[str]:
        """Yield response text chunks as they arrive."""

    async def complete(self, messages: list[Message], tier: str = "default") -> str:
        """Non-streaming convenience: collect the full stream into one string."""
        chunks = [chunk async for chunk in self.stream(messages, tier)]
        return "".join(chunks)


class GroqProvider(LLMProvider):
    def __init__(self, config: Config):
        self._config = config
        self._client = None
        self.usage = UsageTracker()

    def _ensure_client(self):
        if self._client is None:
            from groq import Groq

            self._client = Groq(api_key=Config.secret(self._config.llm.provider_secret_name()))
        return self._client

    async def stream(self, messages: list[Message], tier: str = "default") -> AsyncIterator[str]:
        import groq

        model = self._config.llm.model_for(tier)
        effort = self._config.llm.effort_for(tier)
        client = self._ensure_client()
        loop = asyncio.get_running_loop()

        def _open_stream():
            return client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                reasoning_effort=effort,
                max_completion_tokens=_MAX_SPOKEN_TOKENS,
                timeout=_REQUEST_TIMEOUT_S,
            )

        try:
            groq_stream = await asyncio.wait_for(loop.run_in_executor(None, _open_stream), timeout=_REQUEST_TIMEOUT_S)
        except groq.RateLimitError as exc:
            raise LLMRateLimited(f"Groq rate limited: {exc}") from exc
        except (groq.APIConnectionError, groq.APITimeoutError, groq.InternalServerError) as exc:
            # Transient: network blip, Groq-side 5xx. Worth retrying.
            raise LLMError(f"Groq unreachable: {exc}") from exc
        except (
            groq.AuthenticationError,
            groq.BadRequestError,
            groq.NotFoundError,
            groq.PermissionDeniedError,
            groq.UnprocessableEntityError,
        ) as exc:
            # Permanent: bad model name, invalid key, malformed request. No number of
            # retries fixes a wrong model name - skip straight to fallback.
            raise LLMPermanentError(f"Groq request failed: {exc}") from exc
        except groq.GroqError as exc:
            raise LLMError(f"Groq request failed: {exc}") from exc

        def _next_chunk(it):
            try:
                return next(it), False
            except StopIteration:
                return None, True

        it = iter(groq_stream)
        while True:
            try:
                chunk, done = await asyncio.wait_for(loop.run_in_executor(None, _next_chunk, it), timeout=_REQUEST_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise LLMError(f"Groq stream stalled (tier={tier})") from exc
            if done:
                return
            # Groq puts exact counts on the final chunk without stream_options,
            # so accounting costs nothing extra.
            reported = getattr(chunk, "usage", None)
            if reported is not None:
                details = getattr(reported, "completion_tokens_details", None)
                self.usage.record(
                    Usage(
                        model=model,
                        tier=tier,
                        prompt_tokens=reported.prompt_tokens or 0,
                        completion_tokens=reported.completion_tokens or 0,
                        reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
                    )
                )
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


class OllamaProvider(LLMProvider):
    """Local fallback via Ollama. Reads only message.content, not message.thinking,
    so a reasoning model's think-block text is never spoken."""

    def __init__(self, model: str, host: str):
        self.usage = UsageTracker()
        self._model = model
        self._host = host.rstrip("/")

    async def stream(self, messages: list[Message], tier: str = "default") -> AsyncIterator[str]:
        import urllib.error
        import urllib.request

        loop = asyncio.get_running_loop()
        body = json.dumps(
            {
                "model": self._model,
                "messages": messages,
                "stream": True,
                "options": {"num_predict": _MAX_SPOKEN_TOKENS},
            }
        ).encode()

        def _open():
            req = urllib.request.Request(
                f"{self._host}/api/chat", data=body, headers={"Content-Type": "application/json"}
            )
            return urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S)

        try:
            resp = await asyncio.wait_for(loop.run_in_executor(None, _open), timeout=_REQUEST_TIMEOUT_S)
        except (urllib.error.URLError, OSError, asyncio.TimeoutError) as exc:
            raise LLMError(f"Ollama unreachable at {self._host}: {exc}") from exc

        def _read_line():
            line = resp.readline()
            return line if line else None

        while True:
            try:
                line = await asyncio.wait_for(loop.run_in_executor(None, _read_line), timeout=_REQUEST_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise LLMError("Ollama stream stalled") from exc
            if line is None:
                return
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            content = obj.get("message", {}).get("content", "")
            if content:
                yield content
            if obj.get("done"):
                return


class FallbackLLMProvider(LLMProvider):
    """Primary provider with retry+backoff, falling back to a secondary (local)
    provider once the primary is exhausted."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider):
        self._primary = primary
        self._fallback = fallback
        # Which model answered last: None until a call has been made, so status
        # can say "not tried yet" instead of asserting the cloud is fine.
        self.using_fallback: bool | None = None

    @property
    def local(self) -> LLMProvider:
        """The on-machine model, addressable directly. Needed because
        `using_fallback` records what answered last, not where the next call
        goes; anything that must stay local asks for this explicitly."""
        return self._fallback

    @property
    def usage(self) -> UsageTracker:
        """Whichever model is answering owns the numbers."""
        source = self._fallback if self.using_fallback else self._primary
        return getattr(source, "usage", UsageTracker())

    async def stream(self, messages: list[Message], tier: str = "default") -> AsyncIterator[str]:
        last_error: Exception | None = None
        emitted = False

        for attempt in range(_MAX_RETRIES + 1):
            try:
                async for chunk in self._primary.stream(messages, tier):
                    emitted = True
                    self.using_fallback = False
                    yield chunk
                return
            except LLMError as exc:
                # Once chunks have gone out the caller already has that text.
                # Retrying or falling back would send the whole reply a second
                # time on top of it, so this failure is final.
                if emitted:
                    raise

                last_error = exc
                if isinstance(exc, (LLMPermanentError, LLMRateLimited)):
                    log.warning(
                        "Primary LLM failed, skipping retries",
                        extra={"extra_fields": {"error": str(exc), "reason": type(exc).__name__}},
                    )
                    break
                if attempt < _MAX_RETRIES:
                    backoff = _BASE_BACKOFF_S * (2**attempt)
                    log.warning(
                        "Primary LLM call failed, retrying",
                        extra={"extra_fields": {"attempt": attempt + 1, "backoff_s": backoff, "error": str(exc)}},
                    )
                    await asyncio.sleep(backoff)

        log.error(
            "Primary LLM unavailable, falling back to local model",
            extra={"extra_fields": {"error": str(last_error)}},
        )
        try:
            async for chunk in self._fallback.stream(messages, tier):
                self.using_fallback = True
                yield chunk
        except LLMError as exc:
            raise LLMError(f"Both primary and fallback LLM failed. Primary: {last_error}. Fallback: {exc}") from exc


def build_default_provider(config: Config) -> FallbackLLMProvider:
    primary = GroqProvider(config)
    fallback = OllamaProvider(config.llm.local_fallback_model, config.llm.local_fallback_host)
    return FallbackLLMProvider(primary, fallback)
