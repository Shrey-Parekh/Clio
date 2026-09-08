"""LLM provider behind one interface: streaming, tiered, with retry/backoff and a
local fallback when the primary is unreachable.

This is where 2.3's retry/fallback requirement gets its first real exercise rather
than being stubbed - every LLM call in Clio goes through FallbackLLMProvider, so
the network-down path is exercised by construction, not bolted on later.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from clio.core.config import Config
from clio.core.logging import get_logger
from clio.core.usage import Usage, UsageTracker

log = get_logger("clio.llm.provider")

Message = dict[str, str]  # {"role": "user"|"assistant"|"system", "content": "..."}


@dataclass(frozen=True)
class ToolSchema:
    """One tool the model can choose to call. `parameters` is a JSON Schema object
    (the standard OpenAI/Groq/Ollama "function.parameters" shape)."""

    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_api_dict(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict

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
    """Rate limited. Retrying inside our retry window will not clear it, and the
    local fallback is free and already running, so go there instead of waiting.
    """


class LLMPermanentError(LLMError):
    """A provider call failed in a way retrying can't fix (bad model name, auth
    failure, malformed request). FallbackLLMProvider skips the retry-with-backoff
    delay for these and falls back immediately instead of wasting time on retries
    that can never succeed.
    """


class LLMProvider(ABC):
    @abstractmethod
    def stream(self, messages: list[Message], tier: str = "default") -> AsyncIterator[str]:
        """Yield response text chunks as they arrive."""

    async def complete(self, messages: list[Message], tier: str = "default") -> str:
        """Non-streaming convenience: collect the full stream into one string."""
        chunks = [chunk async for chunk in self.stream(messages, tier)]
        return "".join(chunks)

    @abstractmethod
    async def call_tool(self, messages: list[Message], tools: list[ToolSchema], tier: str = "fast") -> ToolCall | None:
        """Ask the model to pick a tool for this conversation, or None if it
        responds with plain conversation instead - a valid, common outcome.
        Not streamed: tool arguments are JSON and need to arrive whole to parse.
        """


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

    async def call_tool(self, messages: list[Message], tools: list[ToolSchema], tier: str = "fast") -> ToolCall | None:
        import groq

        model = self._config.llm.model_for(tier)
        effort = self._config.llm.effort_for(tier)
        client = self._ensure_client()
        loop = asyncio.get_running_loop()

        def _call():
            return client.chat.completions.create(
                model=model,
                messages=messages,
                tools=[t.to_api_dict() for t in tools],
                tool_choice="auto",
                reasoning_effort=effort,
                timeout=_REQUEST_TIMEOUT_S,
            )

        try:
            resp = await asyncio.wait_for(loop.run_in_executor(None, _call), timeout=_REQUEST_TIMEOUT_S)
        except (groq.APIConnectionError, groq.APITimeoutError, groq.RateLimitError, groq.InternalServerError) as exc:
            raise LLMError(f"Groq unreachable: {exc}") from exc
        except (
            groq.AuthenticationError,
            groq.BadRequestError,
            groq.NotFoundError,
            groq.PermissionDeniedError,
            groq.UnprocessableEntityError,
        ) as exc:
            raise LLMPermanentError(f"Groq request failed: {exc}") from exc
        except groq.GroqError as exc:
            raise LLMError(f"Groq request failed: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise LLMError("Groq tool call timed out") from exc

        calls = resp.choices[0].message.tool_calls
        if not calls:
            return None
        first = calls[0]
        try:
            arguments = json.loads(first.function.arguments)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Groq returned malformed tool arguments JSON: {exc}") from exc
        return ToolCall(name=first.function.name, arguments=arguments)


class OllamaProvider(LLMProvider):
    """Local fallback via Ollama. Reads only message.content, not message.thinking -
    reasoning models (qwen3) stream those as separate fields, so this naturally
    excludes think-block text from what gets spoken, same as Groq's gpt-oss tiers.
    """

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

    async def call_tool(self, messages: list[Message], tools: list[ToolSchema], tier: str = "fast") -> ToolCall | None:
        import urllib.error
        import urllib.request

        loop = asyncio.get_running_loop()
        body = json.dumps(
            {"model": self._model, "messages": messages, "tools": [t.to_api_dict() for t in tools], "stream": False}
        ).encode()

        def _call():
            req = urllib.request.Request(
                f"{self._host}/api/chat", data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
                return json.load(resp)

        try:
            resp = await asyncio.wait_for(loop.run_in_executor(None, _call), timeout=_REQUEST_TIMEOUT_S)
        except (urllib.error.URLError, OSError, asyncio.TimeoutError) as exc:
            raise LLMError(f"Ollama unreachable at {self._host}: {exc}") from exc

        calls = resp.get("message", {}).get("tool_calls")
        if not calls:
            return None
        first = calls[0]
        # Ollama's arguments come back already as a dict, unlike Groq's JSON string.
        arguments = first["function"]["arguments"]
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise LLMError(f"Ollama returned malformed tool arguments JSON: {exc}") from exc
        return ToolCall(name=first["function"]["name"], arguments=arguments)


class FallbackLLMProvider(LLMProvider):
    """Primary provider with retry+backoff; falls back to a secondary provider
    (typically local) if the primary is exhausted. The fallback path is real,
    not simulated - it's the actual OllamaProvider, exercised on every genuine outage.
    """

    def __init__(self, primary: LLMProvider, fallback: LLMProvider):
        self._primary = primary
        self._fallback = fallback
        # Which model answered last: None until a call has been made, so status
        # can say "not tried yet" instead of asserting the cloud is fine.
        self.using_fallback: bool | None = None

    @property
    def local(self) -> LLMProvider:
        """The model that runs on this machine, addressable directly.

        Needed because `using_fallback` records what answered *last*, not where
        the next call will go - every call tries the cloud first. Anything that
        must not leave the machine has to ask for this explicitly rather than
        infer it.
        """
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

    async def call_tool(self, messages: list[Message], tools: list[ToolSchema], tier: str = "fast") -> ToolCall | None:
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await self._primary.call_tool(messages, tools, tier)
            except LLMPermanentError as exc:
                last_error = exc
                log.warning(
                    "Primary LLM failed permanently on tool call, skipping retries",
                    extra={"extra_fields": {"error": str(exc)}},
                )
                break
            except LLMError as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    backoff = _BASE_BACKOFF_S * (2**attempt)
                    log.warning(
                        "Primary LLM tool call failed, retrying",
                        extra={"extra_fields": {"attempt": attempt + 1, "backoff_s": backoff, "error": str(exc)}},
                    )
                    await asyncio.sleep(backoff)

        log.error(
            "Primary LLM unavailable for tool call, falling back to local model",
            extra={"extra_fields": {"error": str(last_error)}},
        )
        try:
            return await self._fallback.call_tool(messages, tools, tier)
        except LLMError as exc:
            raise LLMError(f"Both primary and fallback LLM failed. Primary: {last_error}. Fallback: {exc}") from exc


def build_default_provider(config: Config) -> FallbackLLMProvider:
    primary = GroqProvider(config)
    fallback = OllamaProvider(config.llm.local_fallback_model, config.llm.local_fallback_host)
    return FallbackLLMProvider(primary, fallback)
