"""Web search and page reading (6.1): triggered by asking, answered from what
Tavily actually returned, and every failure said out loud. The Tavily API is
faked; the POST helper is exercised against a local server, never the internet.
Run: python tests/test_web.py
"""

import asyncio
import json
import os
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import web as web_mod  # noqa: E402
from clio.capabilities.web import WebRequest, parse_web_request  # noqa: E402
from clio.core.http import post_json  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.prompts = []

    async def complete(self, messages, tier="default"):
        self.prompts.append((messages[-1]["content"], tier))
        return "spoken answer"


def build():
    llm = FakeLLM()
    o = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="persona", follow_up_window_s=1.0,
    )
    o._memory = ConversationMemory(provider=llm, system_prompt="persona")
    return o, llm


class FakeTavily:
    """Stands in for post_json: records calls, returns a canned body or raises."""

    def __init__(self, body=None, error=None):
        self.body = body if body is not None else {"results": []}
        self.error = error
        self.calls = []

    async def __call__(self, url, body, headers=None, timeout_s=6.0):
        self.calls.append((url, body, headers))
        if self.error:
            raise self.error
        return self.body


def http_error(code):
    return urllib.error.HTTPError("https://api.tavily.com/search", code, "refused", hdrs=None, fp=None)


async def main():
    os.environ["TAVILY_API_KEY"] = "tvly-test"

    # --- asked for, not guessed ---

    for text, want in [
        ("search for the population of Tokyo", WebRequest("search", "the population of tokyo")),
        ("Hey Clio, can you look up the release date of GTA 6?", WebRequest("search", "the release date of gta 6")),
        ("google best biryani in Mumbai", WebRequest("search", "best biryani in mumbai")),
        ("search the web for rust async book", WebRequest("search", "rust async book")),
        ("find out who won the match yesterday", WebRequest("search", "who won the match yesterday")),
        ("look it up", WebRequest("search", "")),
        ("Clio, google that.", WebRequest("search", "")),
        ("summarise https://example.org/Post-1?id=7.", WebRequest("page", "https://example.org/Post-1?id=7")),
        ("https://example.org/article", WebRequest("page", "https://example.org/article")),
        ("my site is https://example.org and it's down", None),
        ("search my files for chemistry", None),
        ("open google chrome", None),
        ("what is the capital of france", None),
        ("I looked up at the sky", None),
        ("search for", None),
    ]:
        assert parse_web_request(text) == want, (text, parse_web_request(text))
    print("OK  search and links triggered by asking, courtesies peeled off, file search left alone")

    o, _ = build()
    assert o._router.plan("find out who won the match")[0].intent == "web", "not a file lookup"
    assert o._router.plan("search my files for chemistry")[0].intent == "files"
    assert o._router.plan("summarise https://example.org/post")[0].intent == "web"
    caps = {c.name: c for c in o._router.capabilities()}
    assert caps["web"].offline is False and caps["web"].permission is Permission.FREE
    print("OK  routed ahead of files, needs the network, free tier")

    # --- a search answers from what came back, trimmed ---

    tavily = FakeTavily({"results": [
        {"title": "Tokyo", "url": "https://example.org/tokyo", "content": "About 14 million people. " + "x" * 2000},
        {"title": "Empty", "url": "https://example.org/empty", "content": ""},
    ]})
    web_mod.post_json = tavily
    o, llm = build()
    spoken, used = await o._handle_utterance("look up the population of Tokyo")
    assert spoken == "spoken answer" and used is True, (spoken, used)
    url, body, headers = tavily.calls[0]
    assert url.endswith("/search") and body["query"] == "the population of tokyo" and body["search_depth"] == "basic"
    assert headers == {"Authorization": "Bearer tvly-test"}
    prompt, tier = llm.prompts[0]
    assert "About 14 million people." in prompt and tier == "default"
    assert "x" * 800 not in prompt, "a long snippet must be trimmed"
    print("OK  search answered from Tavily's results, snippets trimmed, key sent as a Bearer header")

    o, llm = build()
    o._memory.add_user("who won the 2026 world cup")
    o._memory.add_assistant("No idea, honestly.")
    o._memory.add_user("look it up")
    tavily.calls.clear()
    await o._handle_utterance("look it up")
    assert tavily.calls[0][1]["query"] == "who won the 2026 world cup", tavily.calls
    print("OK  'look it up' searches the question asked before it")

    web_mod.post_json = FakeTavily({"results": []})
    o, llm = build()
    spoken, used = await o._handle_utterance("search for zzqx nonsense")
    assert "nothing useful" in spoken and used is False and not llm.prompts, (spoken, used)
    print("OK  no results is said plainly, with no model call")

    # --- a link is read and summarised ---

    tavily = FakeTavily({"results": [{"url": "https://example.org/post", "raw_content": "The post says hello. " + "y" * 9000}]})
    web_mod.post_json = tavily
    o, llm = build()
    spoken, used = await o._handle_utterance("summarise https://example.org/post")
    assert spoken == "spoken answer" and used is True
    assert tavily.calls[0][0].endswith("/extract") and tavily.calls[0][1]["urls"] == ["https://example.org/post"]
    assert "The post says hello." in llm.prompts[0][0] and "y" * 7000 not in llm.prompts[0][0]
    print("OK  a link is read through extract, page text trimmed, then summarised")

    # --- every failure is said, not raised ---

    for error, expected in [
        (http_error(401), "turned down the search key"),
        (http_error(432), "searches used up"),
        (http_error(429), "too fast"),
        (urllib.error.URLError("no route"), "network"),
    ]:
        web_mod.post_json = FakeTavily(error=error)
        o, llm = build()
        spoken, _ = await o._handle_utterance("search for anything")
        assert expected in spoken, (error, spoken)
    print("OK  bad key, used-up plan, rate limit and network loss each spoken plainly")

    del os.environ["TAVILY_API_KEY"]
    tavily = FakeTavily()
    web_mod.post_json = tavily
    o, _ = build()
    spoken, _ = await o._handle_utterance("search for anything")
    assert "Tavily key" in spoken and not tavily.calls, spoken
    print(f"OK  no key: says what's needed and sends nothing: {spoken!r}")

    # --- the POST helper, against a real local server ---

    seen = {}

    class Echo(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["type"] = self.headers.get("Content-Type")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        got = await post_json(f"http://127.0.0.1:{server.server_port}/search", {"query": "q"}, {"Authorization": "Bearer k"})
    finally:
        server.shutdown()
    assert got == {"ok": True} and seen == {"auth": "Bearer k", "type": "application/json", "body": {"query": "q"}}, seen
    print("OK  post_json sends JSON with the given headers and parses the reply")

    print("\nAll web search checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
