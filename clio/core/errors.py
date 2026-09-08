
from __future__ import annotations

import asyncio
import urllib.error
from dataclasses import dataclass

from clio.core.config import ConfigError
from clio.core.events import EventBus
from clio.core.logging import get_logger
from clio.llm.provider import LLMError, LLMPermanentError

log = get_logger("clio.errors")

ERROR_EVENT = "clio.error"


@dataclass(frozen=True)
class SpokenError:
    spoken: str
    detail: str
    category: str
    retryable: bool


# Ordered most-specific first: LLMPermanentError is an LLMError,
# isn't a subclass of anything else here. Checked in order, first match wins.
_RULES: list[tuple[type[Exception], str, str, bool]] = [
    (ConfigError, "config", "I can't start up - my configuration has a problem. Check the logs for details.", False),
    (LLMPermanentError, "llm", "I couldn't reach the language model - that request can't succeed no matter how many times I try it.", False),
    (LLMError, "llm", "I'm having trouble reaching the language model right now.", True),
    (asyncio.TimeoutError, "timeout", "That took too long and I had to give up.", True),
    (TimeoutError, "timeout", "That took too long and I had to give up.", True),
    (urllib.error.URLError, "network", "I'm having trouble connecting - looks like a network issue.", True),
    (ConnectionError, "network", "I'm having trouble connecting - looks like a network issue.", True),
    (OSError, "system", "Something at the system level went wrong - a file, device, or connection I needed wasn't available.", False),
    (ValueError, "input", "Something about that request didn't make sense to me.", False),
]


def describe_error(exc: Exception) -> SpokenError:

    for exc_type, category, spoken, retryable in _RULES:
        if isinstance(exc, exc_type):
            return SpokenError(spoken=spoken, detail=str(exc), category=category, retryable=retryable)

    return SpokenError(
        spoken="Something went wrong on my end and I'm not sure why. I've logged the details.",
        detail=f"{type(exc).__name__}: {exc}",
        category="unknown",
        retryable=False,
    )


async def report_error(
    bus: EventBus | None,
    exc: Exception,
    *,
    context: str,
    source: str = "unknown",
) -> SpokenError:
    described = describe_error(exc)
    log.error(
        f"{context}: {described.category} error",
        exc_info=exc,
        extra={"extra_fields": {"context": context, "category": described.category, "retryable": described.retryable}},
    )

    if bus is not None:
        await bus.publish(
            ERROR_EVENT,
            {
                "spoken": described.spoken,
                "detail": described.detail,
                "category": described.category,
                "retryable": described.retryable,
                "context": context,
            },
            source=source,
        )

    return described
