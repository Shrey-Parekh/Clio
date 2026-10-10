"""GitHub (9.1).

GitHub itself is faked at the HTTP layer - replies shaped like the real API's -
so no network and no token. What is checked: the phrases (and that "what's new
on reddit" isn't GitHub); a spoken repo name is matched to his repos, and an
unclear one is asked about; reading PRs, issues (without the PRs GitHub mixes
in), CI with the failing step named, and commits; a PR summary sees a trimmed
diff; a merge is read back with title, branch and checks and is CONFIRM; one
with failing or running checks, conflicts or a draft is refused on the free
path and never asks; the merge is pinned to the commit read back; and every
failure is a sentence.

Run: python tests/test_github.py
"""

import asyncio
import io
import json
import shutil
import sys
import tempfile
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.github import GitHubCapability, Merge, Refusal, explain_failure, parse_github_request  # noqa: E402
from clio.core.github import GitHub, GitHubError  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

HOUR_AGO = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
REPOS = [{"name": "Clio", "full_name": "Shrey-Parekh/Clio", "default_branch": "main"},
         {"name": "ewaste-classifier", "full_name": "Shrey-Parekh/ewaste-classifier", "default_branch": "main"},
         {"name": "ewaste-data", "full_name": "Shrey-Parekh/ewaste-data", "default_branch": "main"}]


def pr(number, title, sha="abc123", **extra):
    return {"number": number, "title": title, "state": "open", "draft": False, "merged": False,
            "mergeable": True, "mergeable_state": "clean", "head": {"sha": sha}, "base": {"ref": "main"},
            "additions": 12, "deletions": 3, **extra}


class FakeGitHub:
    """Answers by method and path, records every request."""

    def __init__(self, routes):
        self.routes, self.sent = routes, []

    def __call__(self, request, timeout):
        path = request.full_url.split("api.github.com", 1)[1].split("?", 1)[0]
        self.sent.append((request.get_method(), path, request.data, dict(request.header_items())))
        answer = self.routes.get((request.get_method(), path))
        if answer is None:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", Message(), io.BytesIO(b"{}"))
        if isinstance(answer, Exception):
            raise answer
        body = answer if isinstance(answer, str) else json.dumps(answer)
        return io.BytesIO(body.encode("utf-8"))


def http_error(code, message="", remaining=None):
    headers = Message()
    if remaining is not None:
        headers["X-RateLimit-Remaining"] = remaining
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", headers,
                                  io.BytesIO(json.dumps({"message": message}).encode()))


def capability(routes):
    fake = FakeGitHub({("GET", "/user/repos"): REPOS, **routes})
    return GitHubCapability(GitHub(token="test-token", opener=fake)), fake


async def main():
    cases = {
        "any open PRs on clio": ("prs", "clio", 0),
        "what pull requests are open on Clio?": ("prs", "clio", 0),
        "what issues are open on ewaste": ("issues", "ewaste", 0),
        "did CI pass on clio": ("ci", "clio", 0),
        "what's failing on clio's CI": ("ci", "clio", 0),
        "what changed in PR four on clio": ("pr", "clio", 4),
        "merge PR 4 on clio": ("merge", "clio", 4),
        "latest commits on clio": ("commits", "clio", 0),
        "what's new on clio on github": ("commits", "clio", 0),
    }
    for said, (kind, repo, number) in cases.items():
        request = parse_github_request(said)
        assert request is not None and (request.kind, request.repo, request.number) == (kind, repo, number), \
            (said, request)
    for said in ("what's new on reddit", "search for prs", "merge the branch", "what went wrong"):
        assert parse_github_request(said) is None, said
    print("OK  GitHub phrases parse; 'what's new on reddit' is left for the news")

    gh, _ = capability({})
    assert gh.repo("clio") == ("Shrey-Parekh/Clio", "main")
    assert gh.repo("ewaste classifier")[0] == "Shrey-Parekh/ewaste-classifier"
    for said, expected in (("ewaste", "could be ewaste-classifier, ewaste-data"), ("nothing", "can't find")):
        try:
            gh.repo(said)
            raise AssertionError(said)
        except GitHubError as exc:
            assert expected in explain_failure(exc), explain_failure(exc)
    print("OK  a spoken name finds his repo; an unclear or unknown one is said")

    gh, fake = capability({
        ("GET", "/repos/Shrey-Parekh/Clio/pulls"): [pr(4, "Bump requests"), pr(7, "Fix HUD flicker")],
        ("GET", "/repos/Shrey-Parekh/Clio/issues"): [
            {"number": 9, "title": "Mic too quiet"}, {"number": 7, "title": "Fix HUD flicker", "pull_request": {}}],
        ("GET", "/repos/Shrey-Parekh/Clio/actions/runs"): {"workflow_runs": [
            {"id": 55, "status": "completed", "conclusion": "failure", "updated_at": HOUR_AGO,
             "created_at": HOUR_AGO, "html_url": "https://github.com/run/55"}]},
        ("GET", "/repos/Shrey-Parekh/Clio/actions/runs/55/jobs"): {"jobs": [
            {"name": "test", "conclusion": "failure",
             "steps": [{"name": "Install", "conclusion": "success"}, {"name": "Run pytest", "conclusion": "failure"}]}]},
        ("GET", "/repos/Shrey-Parekh/Clio/commits"): [
            {"commit": {"message": "8.5: meeting notes\n\nbody", "author": {"date": HOUR_AGO}}}],
    })
    said, full = gh.read(parse_github_request("any open PRs on clio"))
    assert said == "2 open pull requests on Clio: #4 Bump requests; #7 Fix HUD flicker.", said
    said, _ = gh.read(parse_github_request("any issues on clio"))
    assert said == "1 open issue on Clio: #9 Mic too quiet.", "PRs GitHub lists as issues are left out"
    said, _ = gh.read(parse_github_request("did CI pass on clio"))
    assert said == "CI failed on Clio, 1 hour ago - test, at Run pytest.", said
    said, _ = gh.read(parse_github_request("latest commits on clio"))
    assert said == "Latest on Clio: 8.5: meeting notes (1 hour ago).", said
    assert all(h.get("Authorization") == "Bearer test-token" for _, _, _, h in fake.sent)
    print("OK  PRs, issues, CI with the failing step, and commits read out")

    gh, _ = capability({
        ("GET", "/repos/Shrey-Parekh/Clio/pulls/4"): pr(4, "Bump requests"),
        ("GET", "/repos/Shrey-Parekh/Clio/pulls/4/files"): [{"filename": "requirements.txt"}],
    })
    real_call = gh._gh._call

    def diff_aware(method, path, params=None, body=None, accept="application/vnd.github+json"):
        if "diff" in accept:
            return "+" + "x" * 20_000
        return real_call(method, path, params, body, accept)

    gh._gh._call = diff_aware
    material = gh.pr_material(parse_github_request("what changed in PR 4 on clio"))
    assert material.startswith("PR 4 on Clio: Bump requests (1 files: requirements.txt; +12 -3)"), material[:80]
    assert len(material) < 6_200, "the diff is trimmed to fit Groq's budget"
    print("OK  a PR summary sees its title, files and a trimmed diff")

    def merge_case(pull, checks):
        return capability({("GET", "/repos/Shrey-Parekh/Clio/pulls/4"): pull,
                           ("GET", "/repos/Shrey-Parekh/Clio/commits/abc123/check-runs"): {"check_runs": checks},
                           ("PUT", "/repos/Shrey-Parekh/Clio/pulls/4/merge"): {"merged": True}})

    passed = [{"status": "completed", "conclusion": "success"}] * 3
    gh, fake = merge_case(pr(4, "Bump requests"), passed)
    plan = gh.resolve_merge("merge PR 4 on clio")
    assert isinstance(plan, Merge)
    assert GitHubCapability.describe(plan) == 'Merging PR 4 on Clio, "Bump requests", into main - 3 checks passed'
    calls = len(fake.sent)
    assert gh.resolve_merge("merge PR 4 on clio") is plan and len(fake.sent) == calls, "looked up once, not per match"
    for pull, checks, expected in (
        (pr(4, "Bump requests"), [{"status": "completed", "conclusion": "failure"}] + passed, "1 failing check"),
        (pr(4, "Bump requests"), [{"status": "in_progress"}], "still has checks running"),
        (pr(4, "Bump requests", mergeable=False, mergeable_state="dirty"), passed, "has conflicts with main"),
        (pr(4, "Bump requests", draft=True), passed, "still a draft"),
    ):
        refused = merge_case(pull, checks)[0].resolve_merge("merge PR 4 on clio")
        assert isinstance(refused, Refusal) and expected in refused.said, (expected, refused)
    print("OK  a merge reads back title, branch and checks; failing, running, conflicted or draft is refused")

    base = Path(tempfile.mkdtemp())
    try:
        o = Orchestrator(wake_detector=None, turn_detector=None, stt=None, llm=None, speaker=None,
                         persona_system_prompt="p", follow_up_window_s=1.0, memory_root=str(base / "m"))
        caps = {c.name: c for c in o._router.capabilities()}
        assert caps["github"].permission is Permission.FREE
        assert caps["github_merge"].permission is Permission.CONFIRM

        o._github, fake = merge_case(pr(4, "Bump requests"), passed)
        matched = o._router.match("merge PR 4 on clio")
        assert matched.intent == "github_merge" and "Bump requests" in matched.description, matched.description
        assert await matched.run() == "Merged PR 4 into main."
        method, path, data, _ = fake.sent[-1]
        assert (method, path, json.loads(data)) == ("PUT", "/repos/Shrey-Parekh/Clio/pulls/4/merge", {"sha": "abc123"}), \
            "merged exactly the commit that was read back"

        o._github, _ = merge_case(pr(4, "Bump requests"), [{"status": "completed", "conclusion": "failure"}])
        matched = o._router.match("merge PR 4 on clio")
        assert matched.intent == "github" and "won't merge" in await matched.run(), "refused without asking"
        print("OK  through the router: a mergeable PR asks first and merges that commit; a failing one never asks")

        o._github = GitHubCapability(GitHub(token="", opener=FakeGitHub({})))
        assert "GITHUB_TOKEN" in await o._router.match("any open PRs on clio").run()
        for error, expected in ((http_error(401), "expired"), (http_error(403, remaining="0"), "rate limit"),
                                (http_error(403), "isn't allowed"), (OSError("down"), "couldn't reach")):
            o._github = GitHubCapability(GitHub(token="t", opener=FakeGitHub({("GET", "/user/repos"): error})))
            spoken = await o._router.match("any open PRs on clio").run()
            assert expected in spoken, (expected, spoken)
        o._github, _ = capability({("PUT", "/repos/Shrey-Parekh/Clio/pulls/4/merge"):
                                   http_error(409, "Head branch was modified")})
        refused = await o._github.merge(Merge("Shrey-Parekh/Clio", 4, "t", "main", "abc123", ""))
        assert refused == "GitHub wouldn't merge it - Head branch was modified.", refused
        print("OK  no token, an expired one, rate limits, no permission, no network: each said, never raised")

        print("\nAll GitHub checks passed.")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
