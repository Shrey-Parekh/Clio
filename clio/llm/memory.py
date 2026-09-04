"""Rolling conversation memory: trims to a token budget by summarizing older turns
rather than silently dropping them, and keeps the most recent turns verbatim so
referential follow-ups ("do that again", "the second one") still resolve correctly.
"""

from __future__ import annotations

from clio.core.logging import get_logger
from clio.llm.provider import LLMProvider, Message

log = get_logger("clio.llm.memory")

_SUMMARY_SYSTEM_PROMPT = (
    "Summarize this conversation excerpt in 2-4 sentences. Keep specific facts, names, "
    "numbers, and any request that wasn't resolved yet. Drop small talk. Be concise."
)


def estimate_tokens(text: str) -> int:
    """Rough estimate (~4 chars/token for English), not an exact per-model count.
    Good enough for proactive trimming with a safety margin - the point is staying
    well clear of the real limit, not matching it exactly.
    """
    return max(1, len(text) // 4)


class ConversationMemory:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        max_tokens: int = 6000,
        system_prompt: str | None = None,
        keep_recent_turns: int = 6,
    ):
        self._provider = provider
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._keep_recent_turns = keep_recent_turns
        self._summary: str | None = None
        self._messages: list[Message] = []

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        self._messages.append({"role": "assistant", "content": content})

    def get_messages(self) -> list[Message]:
        """The full prompt to send to the LLM: system prompt, then the rolling
        summary if one exists, then verbatim recent messages."""
        msgs: list[Message] = []
        if self._system_prompt:
            msgs.append({"role": "system", "content": self._system_prompt})
        if self._summary:
            msgs.append({"role": "system", "content": f"Summary of earlier conversation: {self._summary}"})
        msgs.extend(self._messages)
        return msgs

    def total_tokens(self) -> int:
        return sum(estimate_tokens(m["content"]) for m in self.get_messages())

    async def trim_if_needed(self) -> bool:
        """Summarizes older turns into the rolling summary if over budget. Returns
        True if trimming happened. Safe to call after every turn - a no-op when
        under budget."""
        if self.total_tokens() <= self._max_tokens:
            return False

        keep_n = self._keep_recent_turns * 2  # user+assistant pairs
        if len(self._messages) <= keep_n:
            # Nothing safe to summarize without touching the recent turns that need
            # to stay verbatim for referential follow-ups - can't trim further.
            return False

        to_summarize = self._messages[:-keep_n]
        recent = self._messages[-keep_n:]
        self._summary = await self._summarize(to_summarize, previous_summary=self._summary)
        self._messages = recent
        log.info(
            "Trimmed conversation memory",
            extra={"extra_fields": {"summarized_messages": len(to_summarize), "kept_recent": len(recent)}},
        )
        return True

    async def _summarize(self, messages: list[Message], previous_summary: str | None) -> str:
        if self._provider is None:
            return self._naive_summary(messages, previous_summary)

        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        parts = []
        if previous_summary:
            parts.append(f"Summary so far: {previous_summary}")
        parts.append(f"New messages to fold in:\n{transcript}")

        try:
            result = await self._provider.complete(
                [
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": "\n\n".join(parts)},
                ],
                tier="fast",
            )
            summary = result.strip()
            if summary:
                return summary
        except Exception:
            log.warning("Summarization call failed, using naive fallback", exc_info=True)

        return self._naive_summary(messages, previous_summary)

    def _naive_summary(self, messages: list[Message], previous_summary: str | None) -> str:
        base = f"{previous_summary} " if previous_summary else ""
        return f"{base}[{len(messages)} earlier message(s) omitted to save context - no summary available]"
