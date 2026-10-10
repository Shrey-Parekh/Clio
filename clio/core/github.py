"""GitHub's REST API, read freely and merged only on a confirmed yes (9.1).

A fine-grained token in .env (GITHUB_TOKEN), his choice: limited by GitHub
itself to his repos and to reading, plus writing pull requests - so even a bug
here could not delete a branch or touch an issue. Standard library only.

Blocking calls; the capability runs them on a thread.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TOKEN = "GITHUB_TOKEN"
_TIMEOUT_S = 10.0
_MAX_BYTES = 2_000_000
_REPOS_TTL_S = 3600.0


class GitHubError(Exception):
    """A failure with a kind the voice side turns into a sentence."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind   # no_token | auth | forbidden | not_found | rate | network | refused


def _opener(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


class GitHub:
    def __init__(self, token: str | None = None, opener=_opener):
        self._token = token
        self._open = opener
        self._repos: tuple[float, list[dict]] = (0.0, [])

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
              accept: str = "application/vnd.github+json"):
        token = self._token if self._token is not None else os.environ.get(TOKEN, "")
        if not token:
            raise GitHubError("no_token")
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        request = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Accept": accept,
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Clio"})
        try:
            with self._open(request, _TIMEOUT_S) as response:
                raw = response.read(_MAX_BYTES).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise GitHubError("auth") from exc
            if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
                raise GitHubError("rate") from exc
            if exc.code == 403:
                raise GitHubError("forbidden") from exc
            if exc.code == 404:
                raise GitHubError("not_found") from exc
            if exc.code in (405, 409, 422):
                # Merge refused by GitHub itself: not mergeable, head moved on.
                raise GitHubError("refused", _message(exc)) from exc
            raise GitHubError("network", f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GitHubError("network", str(exc)) from exc
        return raw if "json" not in accept else (json.loads(raw) if raw else {})

    # --- reads ---

    def repos(self) -> list[dict]:
        """His own repos, cached for an hour: the names "on clio" is matched against."""
        at, cached = self._repos
        if cached and time.monotonic() - at < _REPOS_TTL_S:
            return cached
        found = self._call("GET", "/user/repos", {"per_page": 100, "affiliation": "owner",
                                                  "sort": "pushed"})
        self._repos = (time.monotonic(), found)
        return found

    def pulls(self, repo: str) -> list[dict]:
        return self._call("GET", f"/repos/{repo}/pulls", {"state": "open", "per_page": 30})

    def pull(self, repo: str, number: int) -> dict:
        return self._call("GET", f"/repos/{repo}/pulls/{number}")

    def pull_files(self, repo: str, number: int) -> list[dict]:
        return self._call("GET", f"/repos/{repo}/pulls/{number}/files", {"per_page": 100})

    def pull_diff(self, repo: str, number: int) -> str:
        return self._call("GET", f"/repos/{repo}/pulls/{number}", accept="application/vnd.github.diff")

    def issues(self, repo: str) -> list[dict]:
        found = self._call("GET", f"/repos/{repo}/issues", {"state": "open", "per_page": 30})
        return [i for i in found if "pull_request" not in i]   # the issues list includes PRs

    def latest_run(self, repo: str, branch: str) -> dict | None:
        runs = self._call("GET", f"/repos/{repo}/actions/runs", {"branch": branch, "per_page": 1})
        return (runs.get("workflow_runs") or [None])[0]

    def failed_steps(self, repo: str, run_id: int) -> list[tuple[str, str]]:
        jobs = self._call("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs").get("jobs", [])
        found = []
        for job in jobs:
            if job.get("conclusion") == "failure":
                step = next((s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"), "")
                found.append((job.get("name", "a job"), step))
        return found

    def checks(self, repo: str, sha: str) -> dict[str, int]:
        """Counts of check runs on a commit by outcome: passed, failed, running."""
        runs = self._call("GET", f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})
        counts = {"passed": 0, "failed": 0, "running": 0}
        for run in runs.get("check_runs", []):
            if run.get("status") != "completed":
                counts["running"] += 1
            elif run.get("conclusion") in ("success", "neutral", "skipped"):
                counts["passed"] += 1
            else:
                counts["failed"] += 1
        return counts

    def commits(self, repo: str, count: int = 5) -> list[dict]:
        return self._call("GET", f"/repos/{repo}/commits", {"per_page": count})

    # --- the one write ---

    def merge(self, repo: str, number: int, sha: str) -> dict:
        """Merges exactly the commit that was read back: if the PR moved on in
        between, GitHub refuses rather than merging something he never heard."""
        return self._call("PUT", f"/repos/{repo}/pulls/{number}/merge", body={"sha": sha})


def _message(exc: urllib.error.HTTPError) -> str:
    try:
        return json.loads(exc.read().decode("utf-8")).get("message", "")
    except (ValueError, OSError):
        return ""
