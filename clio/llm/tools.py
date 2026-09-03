"""Tool calling with schema validation. The model picks a tool and supplies
arguments; invalid arguments are never executed blind - they're rejected, the
model is told what was wrong, and it gets a bounded number of chances to correct
itself before the call fails clearly.
"""

from __future__ import annotations

from clio.core.logging import get_logger
from clio.llm.provider import LLMProvider, Message, ToolCall, ToolSchema

log = get_logger("clio.llm.tools")

_MAX_CORRECTION_ATTEMPTS = 2


class ToolCallError(Exception):
    """The model failed to produce a valid tool call after every retry."""


class ToolCaller:
    def __init__(self, provider: LLMProvider, tools: list[ToolSchema], tier: str = "fast"):
        self._provider = provider
        self._tools = tools
        self._tools_by_name = {t.name: t for t in tools}
        self._tier = tier

    async def call(self, messages: list[Message]) -> ToolCall | None:
        """Returns a validated ToolCall, or None if the model chose plain
        conversation instead of a tool - a valid, expected outcome, not an error.
        Raises ToolCallError only if the model can't produce valid arguments
        after every correction attempt.
        """
        import jsonschema

        conversation = list(messages)
        last_error: str | None = None

        for attempt in range(_MAX_CORRECTION_ATTEMPTS + 1):
            call = await self._provider.call_tool(conversation, self._tools, tier=self._tier)

            if call is None:
                return None

            schema = self._tools_by_name.get(call.name)
            if schema is None:
                last_error = (
                    f"'{call.name}' is not a real tool. Available tools: "
                    f"{', '.join(self._tools_by_name)}"
                )
            else:
                try:
                    jsonschema.validate(call.arguments, schema.parameters)
                    return call
                except jsonschema.ValidationError as exc:
                    last_error = exc.message

            log.warning(
                "Rejected invalid tool call, asking model to correct",
                extra={
                    "extra_fields": {
                        "attempt": attempt + 1,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "error": last_error,
                    }
                },
            )
            if attempt < _MAX_CORRECTION_ATTEMPTS:
                conversation = conversation + [
                    {
                        "role": "assistant",
                        "content": f"(attempted to call {call.name} with {call.arguments})",
                    },
                    {
                        "role": "user",
                        "content": f"That call was invalid: {last_error}. Correct the arguments and try again.",
                    },
                ]

        raise ToolCallError(
            f"Model failed to produce a valid tool call after {_MAX_CORRECTION_ATTEMPTS + 1} attempts. "
            f"Last error: {last_error}"
        )
