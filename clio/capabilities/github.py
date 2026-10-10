"""GitHub by voice (9.1): "any open PRs on clio", "did CI pass on clio",
"what changed in PR 4 on clio", "merge PR 4 on clio".

Reading is free. Merging is the one change (his choice): read back with the
PR's title, target branch and checks, refused outright when checks are failing
or GitHub says it can't merge, and pinned to the commit that was read back.
The repo is named by voice and matched against his own repos' names.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from clio.capabilities.timer import _NUMBER_WORDS
from clio.core.github import GitHub, GitHubError
from clio.core.logging import get_logger

log = get_logger("clio.capabilities.github")

_LEAD = r"^(?:(?:hey |ok |okay )?clio,? )?(?:(?:can|could|would) you |please )?"
_NUM = r"(?P<n>\d+|" + "|".join(_NUMBER_WORDS) + r")"
_PR = r"(?:pr|p r|pull request)"
_ON = r"(?:on|in|for|of)"
_REPO = r"(?:my |the )?(?P<repo>[\w .'-]+?)(?:'s)?(?: repo(?:sitory)?)?(?: on github)?"
_ASK = r"(?:are there |any |what are (?:the )?|list |show (?:me )?)?(?:the )?(?:open )?"
_CI = r"(?:ci|build|tests|checks|actions)"

# One pattern per phrasing: a name can appear only once in a regex.
_PATTERNS = [
    ("merge", rf"merge {_PR} (?:number )?{_NUM} (?:on|in|into) {_REPO}"),
    ("pr", rf"(?:what(?:'s| has| did)? (?:changed?|in) in|summari[sz]e|tell me about|explain) "
           rf"{_PR} (?:number )?{_NUM} {_ON} {_REPO}"),
    ("prs", rf"{_ASK}(?:pull requests|prs|p rs)(?: are)?(?: open)? {_ON} {_REPO}"),
    ("prs", rf"what (?:pull requests|prs) are open {_ON} {_REPO}"),
    ("issues", rf"{_ASK}issues(?: are)?(?: open)? {_ON} {_REPO}"),
    ("issues", rf"what issues are open {_ON} {_REPO}"),
    ("ci", rf"(?:did|has|have|is|are) (?:the )?{_CI} (?:pass(?:ed|ing)?|fail(?:ed|ing)?|green|red|broken) {_ON} {_REPO}"),
    ("ci", rf"what'?s (?:failing|broken) {_ON} {_REPO}(?:'s)? (?:ci|build)"),
    ("ci", rf"(?:ci|build) status {_ON} {_REPO}"),
    ("ci", rf"how'?s (?:the )?(?:ci|build) {_ON} {_REPO}"),
    ("commits", rf"(?:latest|recent|last) commits {_ON} {_REPO}"),
    # "What's new on X" alone would claim the news; "on github" makes it his.
    ("commits", rf"what'?s new {_ON} (?:my |the )?(?P<repo>[\w .'-]+?) on github"),
]
_COMPILED = [(kind, re.compile(_LEAD + f"(?:{pattern})$")) for kind, pattern in _PATTERNS]

# What the diff summary may cost: Groq's ~8k tokens a minute.
_DIFF_CHARS = 6_000
_RESOLVE_TTL_S = 30.0


@dataclass(frozen=True)
class GitHubRequest:
    kind: str
    repo: str
    number: int = 0


@dataclass(frozen=True)
class Merge:
    """A merge ready to confirm: what was read back is what gets merged."""
    repo: str
    number: int
    title: str
    base: str
    sha: str
    checks: str


@dataclass(frozen=True)
class Refusal:
    said: str


def parse_github_request(text: str) -> GitHubRequest | None:
    said = re.sub(r"[.!?]+$", "", text.strip().lower())
    for kind, pattern in _COMPILED:
        found = pattern.match(said)
        if found is None:
            continue
        number = found.groupdict().get("n") or "0"
        number = int(number) if number.isdigit() else int(_NUMBER_WORDS[number])
        return GitHubRequest(kind, found.group("repo").strip(), number)
    return None


def _norm(name: str) -> str:
    return re.sub(r"[\s_.-]+", " ", name.lower()).strip()


def _ago(stamp: str) -> str:
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    minutes = int((datetime.now(timezone.utc) - when).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{hours // 24} days ago"


def explain_failure(exc: Exception) -> str | None:
    if not isinstance(exc, GitHubError):
        return None
    if exc.kind == "named":
        return str(exc).split(": ", 1)[-1]
    if exc.kind == "refused":
        detail = str(exc).split(": ", 1)[-1] if ": " in str(exc) else ""
        return "GitHub wouldn't merge it" + (f" - {detail}" if detail else "") + "."
    return {
        "no_token": "I need a GitHub token for that - GITHUB_TOKEN in your dot env file.",
        "auth": "GitHub turned the token down - it may have expired. Check GITHUB_TOKEN.",
        "forbidden": "The GitHub token isn't allowed to do that on this repo.",
        "rate": "GitHub's rate limit is used up for now - try again in a while.",
        "not_found": "GitHub couldn't find that - check the repo name or the PR number.",
        "network": "I couldn't reach GitHub.",
    }.get(exc.kind, "Something went wrong talking to GitHub.")


class GitHubCapability:
    def __init__(self, client: GitHub | None = None):
        self._gh = client or GitHub()
        self._resolved: dict[str, tuple[float, Merge | Refusal | None]] = {}

    def repo(self, said: str) -> tuple[str, str]:
        """(owner/name, default branch) for a spoken name. An unknown or
        ambiguous name raises GitHubError("named") carrying the sentence."""
        wanted = _norm(said)
        repos = self._gh.repos()
        exact = [r for r in repos if _norm(r["name"]) == wanted]
        close = exact or [r for r in repos if wanted and (wanted in _norm(r["name"])
                                                          or _norm(r["name"]) in wanted)]
        if len(close) == 1:
            return close[0]["full_name"], close[0].get("default_branch", "main")
        if not close:
            raise GitHubError("named", f"I can't find a repo called {said} on your GitHub.")
        names = ", ".join(r["name"] for r in close[:3])
        raise GitHubError("named", f"That could be {names} - which one?")

    # --- merging: resolved when matched, so the readback can be specific ---

    def resolve_merge(self, text: str) -> Merge | Refusal | None:
        request = parse_github_request(text)
        if request is None or request.kind != "merge":
            return None
        at, cached = self._resolved.get(text, (0.0, None))
        if cached is not None and time.monotonic() - at < _RESOLVE_TTL_S:
            return cached
        # ponytail: blocking network in the matcher, as software (7.8) does, so
        # the readback names the real PR. Upgrade path: an async describe hook.
        try:
            resolved = self._merge_plan(request)
        except GitHubError as exc:
            resolved = Refusal(explain_failure(exc))
        self._resolved[text] = (time.monotonic(), resolved)
        return resolved

    def _merge_plan(self, request: GitHubRequest) -> Merge | Refusal:
        repo, _ = self.repo(request.repo)
        pr = self._gh.pull(repo, request.number)
        name = f"PR {request.number}, {pr.get('title', '')},"
        if pr.get("state") != "open" or pr.get("merged"):
            return Refusal(f"{name} isn't open.")
        if pr.get("draft"):
            return Refusal(f"{name} is still a draft.")
        if pr.get("mergeable") is False or pr.get("mergeable_state") == "dirty":
            return Refusal(f"{name} has conflicts with {pr['base']['ref']}, so I can't merge it.")
        sha = pr["head"]["sha"]
        counts = self._gh.checks(repo, sha)
        if counts["failed"]:
            return Refusal(f"{name} has {counts['failed']} failing check"
                           f"{'s' if counts['failed'] != 1 else ''}, so I won't merge it.")
        if counts["running"]:
            return Refusal(f"{name} still has checks running - ask me again when they're done.")
        checks = f"{counts['passed']} checks passed" if counts["passed"] else "no checks on it"
        return Merge(repo, request.number, pr.get("title", ""), pr["base"]["ref"], sha, checks)

    @staticmethod
    def describe(merge: Merge) -> str:
        return (f"Merging PR {merge.number} on {merge.repo.split('/')[-1]}, "
                f"\"{merge.title}\", into {merge.base} - {merge.checks}")

    async def merge(self, merge: Merge) -> str:
        try:
            await asyncio.to_thread(self._gh.merge, merge.repo, merge.number, merge.sha)
        except GitHubError as exc:
            return explain_failure(exc)
        # Merged: the next "merge PR 4" must look again, not reuse this.
        self._resolved.clear()
        log.info("Merged a pull request", extra={"extra_fields": {"repo": merge.repo, "pr": merge.number}})
        return f"Merged PR {merge.number} into {merge.base}."

    # --- reading ---

    def read(self, request: GitHubRequest) -> tuple[str, str]:
        """(what to say, the fuller version for the chat window). Blocking."""
        repo, branch = self.repo(request.repo)
        short = repo.split("/")[-1]
        if request.kind in ("prs", "issues"):
            items = self._gh.pulls(repo) if request.kind == "prs" else self._gh.issues(repo)
            noun = "pull request" if request.kind == "prs" else "issue"
            if not items:
                return f"No open {noun}s on {short}.", ""
            lines = [f"#{i['number']} {i['title']}" for i in items]
            said = "; ".join(lines[:3]) + ("" if len(lines) <= 3 else f"; and {len(lines) - 3} more")
            plural = "s" if len(items) != 1 else ""
            return f"{len(items)} open {noun}{plural} on {short}: {said}.", "\n".join(lines)
        if request.kind == "ci":
            run = self._gh.latest_run(repo, branch)
            if run is None:
                return f"{short} has no CI runs on {branch}.", ""
            when = _ago(run.get("updated_at") or run["created_at"])
            if run.get("status") != "completed":
                return f"CI on {short} is still running, started {when}.", ""
            if run.get("conclusion") == "success":
                return f"CI passed on {short}, {when}.", ""
            failed = self._gh.failed_steps(repo, run["id"])
            where = "; ".join(f"{job}, at {step}" if step else job for job, step in failed[:2])
            return (f"CI failed on {short}, {when}" + (f" - {where}." if where else "."),
                    run.get("html_url", ""))
        if request.kind == "commits":
            commits = self._gh.commits(repo)
            lines = [f"{c['commit']['message'].splitlines()[0]} ({_ago(c['commit']['author']['date'])})"
                     for c in commits]
            return (f"Latest on {short}: {lines[0]}." if lines else f"No commits on {short}."), "\n".join(lines)
        raise ValueError(request.kind)

    def pr_material(self, request: GitHubRequest) -> str:
        """A header and the trimmed diff, for the model to summarise. Blocking."""
        repo, _ = self.repo(request.repo)
        pr = self._gh.pull(repo, request.number)
        files = self._gh.pull_files(repo, request.number)
        names = ", ".join(f["filename"] for f in files[:15]) + ("" if len(files) <= 15 else ", ...")
        header = (f"PR {request.number} on {repo.split('/')[-1]}: {pr.get('title', '')} "
                  f"({len(files)} files: {names}; +{pr.get('additions', 0)} -{pr.get('deletions', 0)})")
        diff = self._gh.pull_diff(repo, request.number)[:_DIFF_CHARS]
        return f"{header}\n\n{diff}"
