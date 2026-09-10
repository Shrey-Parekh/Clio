"""Web search, news and page reading (6.1, 6.2), through Tavily.

"Search for", "look up", "google" and "find out" run a search. "What's the news",
"any news about X", "headlines" and "brief me" get a briefing from the last day's
news. A link typed into the chat window with "summarise" or "read" (or pasted on
its own) reads that page. Every time she answers from what came back, with her
own model: the search always runs, so nothing is made up from memory, and results
are trimmed so the prompt stays small.

Triggered by asking, never guessed and never scheduled: a question that merely
could use the web still goes to the conversation model, and there is no briefing
he didn't ask for.
"""

from __future__ import annotations

import re
import urllib.error
from dataclasses import dataclass

from clio.core.config import Config, ConfigError
from clio.core.http import post_json
from clio.core.logging import get_logger

log = get_logger("clio.capabilities.web")

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"
_KEY = "TAVILY_API_KEY"
_MAX_RESULTS = 5
_TIMEOUT_S = 15.0
# Cut to fit: the chat model gets 8,000 tokens a minute, and a spoken answer
# needs a paragraph of evidence, not five whole pages.
_SNIPPET_CHARS = 700
_PAGE_CHARS = 6000

_ANSWER = (
    "Answer out loud in two or three short sentences, using only what's below. Lead with "
    "the answer. No links, lists or citation marks. If it doesn't settle it, say so plainly."
)
_BRIEFING = (
    "Brief him out loud on the three or four biggest stories below, a sentence or two each, "
    "most important first. Say roughly when something happened if it matters. No links, "
    "lists or citation marks, and don't pad it with anything that isn't below."
)


@dataclass(frozen=True)
class WebRequest:
    kind: str   # "search", "news" or "page"
    value: str  # the query ("" = the question before it), the news topic ("" = top stories), or the URL


# Whisper hands back courtesies and her name ahead of the request - "Hey Clio,
# can you look up..." - and an anchored verb list rejected exactly that for notes
# (3.7), so they're peeled off first.
_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay|so|um|uh|clio|please|quickly|can you|could you|would you|"
    r"will you|i want you to|i need you to|go and|go)\b[\s,]*)+"
)
# Before the plain search verbs, so "search for news about X" is a briefing - but
# "news" has to be the thing asked for: "search for news aggregator apps" isn't.
_NEWS = re.compile(
    r"^(?:(?:what'?s|what is|what are|any|give me|tell me|read me|get me|search for|look up|google|find out)\s+)?"
    r"(?:the\s+)?(?:latest\s+|today'?s\s+|top\s+)?(?:news|headlines|news briefing)"
    r"(?:\s+(?:today|this morning))?"
    r"(?:\s+(?:about|on|in|from|for)\s+(?P<topic>.+))?$"
)
_BRIEF = re.compile(
    r"^(?:brief me|catch me up|what'?s happening in the world(?: today)?|what'?s going on in the world|"
    r"(?:what'?s|anything) in the news(?: today)?)"
    r"(?:\s+on\s+(?P<topic>.+))?$"
)
_VERB = re.compile(
    r"^(?:search (?:the web|the internet|online|google) for|search (?:the web|the internet|online)|"
    r"search for|look up|google|find out)\b\s*(?P<query>.*)$"
)
# "Look it up" means the question just asked.
_REFER_BACK = re.compile(
    r"^(?:look (?:it|that|this) up|google (?:it|that|this)|search (?:for )?(?:it|that|this)|find out)$"
)
_PUNCT = re.compile(r"[.!?,;:]+$")
_LINK = re.compile(r"https?://\S+")
_READ_VERB = re.compile(r"\b(?:summari[sz]e|read|tl;?dr|what'?s (?:on|in))\b")


def parse_web_request(text: str) -> WebRequest | None:
    link = _LINK.search(text)
    if link is not None:
        url = link.group(0).rstrip(".,!?;:)'\"")
        # A link is only ever typed, so asking to read it - or pasting it alone - is enough.
        if _READ_VERB.search(text.lower()) or text.strip().rstrip(".,!?") == url:
            return WebRequest("page", url)

    spoken = _PUNCT.sub("", " ".join(text.lower().split()))
    spoken = _LEAD.sub("", spoken).strip()
    news = _NEWS.match(spoken) or _BRIEF.match(spoken)
    if news is not None:
        return WebRequest("news", (news.group("topic") or "").strip())
    if _REFER_BACK.match(spoken):
        return WebRequest("search", "")
    found = _VERB.match(spoken)
    if found is None:
        return None
    query = _PUNCT.sub("", found.group("query")).strip()
    # "Search for" with nothing after it has nothing to search.
    return WebRequest("search", query) if len(query) > 1 else None


async def search(query: str, *, news: bool = False, time_range: str | None = None) -> list[dict]:
    body: dict[str, object] = {"query": query, "search_depth": "basic", "max_results": _MAX_RESULTS}
    if news:
        body.update(topic="news", include_published_date=True)
    if time_range:
        body["time_range"] = time_range
    payload = await post_json(_SEARCH_URL, body, _auth(), timeout_s=_TIMEOUT_S)
    results = [r for r in payload.get("results") or [] if r.get("content")]
    # Logged, not spoken: where an answer came from, for when it's wrong.
    log.info(
        "Web search",
        extra={"extra_fields": {
            "query": query, "news": news, "time_range": time_range,
            "sources": [r.get("url") for r in results],
        }},
    )
    return results


async def read_page(url: str) -> str:
    payload = await post_json(_EXTRACT_URL, {"urls": [url], "format": "text"}, _auth(), timeout_s=_TIMEOUT_S)
    results = payload.get("results") or []
    text = (results[0].get("raw_content") or "").strip() if results else ""
    log.info("Read page", extra={"extra_fields": {"url": url, "chars": len(text)}})
    return text[:_PAGE_CHARS]


def format_results(results: list[dict]) -> str:
    blocks = []
    for r in results:
        # A date is only there for news, and only when Tavily has one.
        dated = f" ({r['published_date']})" if r.get("published_date") else ""
        blocks.append(f"{r.get('title', '')}{dated}\n{r['content'][:_SNIPPET_CHARS]}")
    return "\n\n".join(blocks)


def answer_messages(persona: str, request: str, material: str, shape: str = _ANSWER) -> list[dict[str, str]]:
    """Her persona, and an instruction shaped for speech: what came back is
    long, and a spoken answer is not."""
    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": f"{request}\n\n{shape}\n\n---\n{material}"},
    ]


async def answer(request: WebRequest, persona: str, llm, region: str = "") -> tuple[str, bool]:
    """What to say, and whether the model was asked. Raises on failure, for the
    caller to say. `region` narrows a general briefing to where he lives."""
    shape = _ANSWER
    if request.kind == "page":
        text = await read_page(request.value)
        if not text:
            return "I couldn't read anything from that link.", False
        ask, material = "What is this page about?", text
    elif request.kind == "news":
        topic = request.value
        where = f" in {region}" if region else ""
        query = f"latest news about {topic}" if topic else f"top news headlines today{where}"
        # The last day first; a quiet topic still gets last week's rather than nothing.
        results = await search(query, news=True, time_range="day") or await search(
            query, news=True, time_range="week"
        )
        if not results:
            return (f"Nothing recent in the news about {topic}." if topic
                    else "I couldn't get the news just now."), False
        ask = f"Brief me on the news about {topic}." if topic else "Brief me on today's news."
        material, shape = format_results(results), _BRIEFING
    else:
        results = await search(request.value)
        if not results:
            return f"I searched for {request.value} and nothing useful came back.", False
        ask, material = request.value, format_results(results)
    reply = await llm.complete(answer_messages(persona, ask, material, shape), tier="default")
    return reply, True


def explain_failure(exc: Exception) -> str | None:
    """Tavily's own refusals, said as what they mean. None leaves anything else
    to describe_error."""
    if isinstance(exc, ConfigError):
        return "I can't search yet. I need a free Tavily key, TAVILY_API_KEY, in your dot env file."
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "Tavily turned down the search key. Check TAVILY_API_KEY in your dot env file."
        if exc.code in (432, 433):
            return "That's this month's Tavily searches used up."
        if exc.code == 429:
            return "Tavily says I'm searching too fast. Give it a minute."
    return None


def _auth() -> dict[str, str]:
    # Read per request, not at import: a key added to .env works after a restart
    # without anything else changing, and a missing one fails here, spoken.
    return {"Authorization": f"Bearer {Config.secret(_KEY)}"}
