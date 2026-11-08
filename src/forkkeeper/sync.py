"""Synchronize selected GitHub App installation forks with their parents.

No third-party Python dependencies. Fork selection is GitHub App installation scope.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any

API_BASE = "https://api.github.com"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class SyncError(Exception):
    """An expected upstream synchronization error."""


class GitHubError(SyncError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"GitHub returned HTTP {status}: {message}")


class GitHub:
    def __init__(
        self, token: str, *, expires_at: float = float("inf"),
        refresh_token: Callable[[], tuple[str, float]] | None = None,
    ) -> None:
        if not token:
            raise SyncError("GH_APP_TOKEN is missing")
        self._token = token
        self._expires_at = expires_at
        self._refresh_token = refresh_token

    @property
    def token(self) -> str:
        # Leave enough lifetime for the bounded Git fetch/push subprocesses.
        if self._refresh_token and time.time() >= self._expires_at - 900:
            self._token, self._expires_at = self._refresh_token()
        return self._token

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            API_BASE + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bocklabs-forkkeeper",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                with exc:
                    error_body = exc.read(4096).decode("utf-8", errors="replace")
            except (OSError, HTTPException):
                raise GitHubError(exc.code, str(exc.reason)) from None
            try:
                payload = json.loads(error_body)
                message = payload.get("message", error_body) if isinstance(payload, dict) else error_body
            except ValueError:
                message = error_body
            raise GitHubError(exc.code, str(message)) from None
        except (urllib.error.URLError, OSError, HTTPException) as exc:
            raise SyncError(f"GitHub API request failed: {exc}") from None
        except ValueError:
            raise SyncError("GitHub API response was not valid JSON") from None

    def selected_repositories(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.request("GET", f"/installation/repositories?per_page=100&page={page}")
            repos = response.get("repositories", [])
            results.extend(repos)
            if len(repos) < 100:
                return results
            page += 1
            if page > 100:
                raise SyncError("Installation has over 10,000 repositories; pagination limit reached")

    def repository(self, full_name: str) -> dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}")

    def sync_native(self, full_name: str, branch: str) -> dict[str, Any]:
        return self.request("POST", f"/repos/{full_name}/merge-upstream", {"branch": branch})

    def open_prs(self, full_name: str, owner: str, sync_branch: str, base: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "state": "open", "head": f"{owner}:{sync_branch}", "base": base, "per_page": 100
        })
        return self.request("GET", f"/repos/{full_name}/pulls?{query}")

    def create_pr(self, full_name: str, sync_branch: str, base: str, upstream: str, upstream_branch: str) -> dict[str, Any]:
        return self.request("POST", f"/repos/{full_name}/pulls", {
            "title": f"chore: sync {upstream}:{upstream_branch}",
            "head": sync_branch,
            "base": base,
            "body": (
                "Automated upstream synchronization by Forkkeeper.\\n\\n"
                f"Source: `{upstream}:{upstream_branch}`\\n"
                f"Target: `{full_name}:{base}`\\n\\n"
                "Please review the changes before merging. If GitHub reports merge "
                "conflicts, resolve them manually. No fork history was overwritten."
            ).replace("\\n", "\n"),
            "maintainer_can_modify": True,
        })


def parse_repo_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split("/")
    if len(parts) != 2 or any(not NAME_RE.fullmatch(p) or p in {".", ".."} for p in parts):
        raise SyncError(f"Invalid repository name: {full_name!r}")
    return parts[0], parts[1]


def validate_branch(branch: str) -> str:
    # API and git remote ref safety, no shell use. The simpler GitHub-compatible
    # subset is deliberate; unsupported branch naming is reported clearly.
    if (
        not branch or branch.startswith("/") or branch.endswith(("/", "."))
        or ".." in branch or "@{" in branch
        or any(c in branch for c in " ~^:?*[\\")
        or any(ord(c) < 32 or ord(c) == 127 for c in branch)
        or any(part in {"", ".", ".."} or part.endswith(".lock") for part in branch.split("/"))
    ):
        raise SyncError(f"Unsupported branch name: {branch!r}")
    return branch


@dataclass(frozen=True)
class Fork:
    full_name: str
    base: str
    upstream: str
    upstream_branch: str


def describe_fork(api: GitHub, full_name: str) -> Fork:
    repo = api.repository(full_name)
    if not repo.get("fork"):
        raise SyncError(f"{full_name} is not a fork")
    if repo.get("archived") or repo.get("disabled"):
        raise SyncError(f"{full_name} is archived or disabled")
    upstream = repo.get("parent") or {}
    if not upstream.get("full_name") or not upstream.get("default_branch"):
        raise SyncError(f"GitHub did not expose the parent of {full_name}; check upstream access")
    parse_repo_name(full_name)
    parse_repo_name(upstream["full_name"])
    return Fork(
        full_name=full_name,
        base=validate_branch(repo["default_branch"]),
        upstream=upstream["full_name"],
        upstream_branch=validate_branch(upstream["default_branch"]),
    )


class Git:
    def __init__(self, token: str, workdir: Path) -> None:
        self.workdir = workdir
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        self.env = dict(os.environ)
        self.env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
        })

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args], cwd=self.workdir, env=self.env,
            text=True, capture_output=True, check=False, timeout=180,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-900:]
            raise SyncError(f"git {args[0]} failed ({result.returncode}): {detail}")
        return result

    def fetch_fork(self, fork: Fork) -> None:
        self.run("init", "-q")
        self.run("remote", "add", "fork", f"https://github.com/{fork.full_name}.git")
        self.run("remote", "add", "upstream", f"https://github.com/{fork.upstream}.git")
        self.run("fetch", "--quiet", "--no-tags", "fork",
                 f"+refs/heads/{fork.base}:refs/remotes/fork/base")
        self.run("fetch", "--quiet", "--no-tags", "upstream",
                 f"+refs/heads/{fork.upstream_branch}:refs/remotes/upstream/base")

    def upstream_already_merged(self) -> bool:
        status = self.run("merge-base", "--is-ancestor", "refs/remotes/upstream/base", "refs/remotes/fork/base", check=False)
        if status.returncode == 0:
            return True
        if status.returncode == 1:
            # If there is no common ancestor at all, fail instead of generating
            # an unrelated-history PR that GitHub cannot merge.
            common = self.run("merge-base", "refs/remotes/upstream/base", "refs/remotes/fork/base", check=False)
            if common.returncode != 0 or not common.stdout.strip():
                raise SyncError("Fork and upstream have no common commit history")
            return False
        raise SyncError(f"Cannot compare Git history: {status.stderr.strip()[-500:]}")

    def push_sync_branch(self, branch: str) -> None:
        # No --force. Never overwrite somebody else's branch history.
        self.run("push", "--quiet", "fork", f"refs/remotes/upstream/base:refs/heads/{branch}")


def pr_sync(api: GitHub, fork: Fork, *, dry_run: bool = False) -> tuple[str, str]:
    owner, _ = parse_repo_name(fork.full_name)
    sync_branch = validate_branch(f"forkkeeper/{fork.upstream_branch}")
    if sync_branch == fork.base:
        raise SyncError("Synchronization branch matches the default branch; refusing a direct push")
    with tempfile.TemporaryDirectory(prefix="forkkeeper-") as tmp:
        git = Git(api.token, Path(tmp))
        git.fetch_fork(fork)
        if git.upstream_already_merged():
            return "current", "Fork already contains all upstream commits"
        if dry_run:
            return "would-open-pr", f"Would update {sync_branch} and open/update a PR"
        existing = api.open_prs(fork.full_name, owner, sync_branch, fork.base)
        git.push_sync_branch(sync_branch)
        if existing:
            return "updated-pr", str(existing[0].get("html_url", "PR updated"))
        try:
            pr = api.create_pr(fork.full_name, sync_branch, fork.base, fork.upstream, fork.upstream_branch)
        except GitHubError as exc:
            if exc.status == 422:
                # Eventual consistency or a concurrent workflow could create the PR.
                existing = api.open_prs(fork.full_name, owner, sync_branch, fork.base)
                if existing:
                    return "updated-pr", str(existing[0].get("html_url", "PR updated"))
            raise
        return "created-pr", str(pr.get("html_url", "PR created"))


def sync_one(api: GitHub, fork: Fork, *, mode: str, dry_run: bool) -> tuple[str, str]:
    if mode == "pr":
        return pr_sync(api, fork, dry_run=dry_run)
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="forkkeeper-") as tmp:
            git = Git(api.token, Path(tmp))
            git.fetch_fork(fork)
            if git.upstream_already_merged():
                return "current", "Fork already contains all upstream commits"
        return "would-sync", "Would attempt native upstream sync with PR fallback on conflict"
    try:
        result = api.sync_native(fork.full_name, fork.base)
        return "synced", str(result.get("message", "GitHub synced the fork"))
    except GitHubError as exc:
        if exc.status not in (409, 422):
            raise
        # Direct sync cannot resolve conflicts / branch restrictions. Fall back
        # to a reviewable PR rather than resetting any commits.
        status, details = pr_sync(api, fork)
        return status, f"Native sync rejected (HTTP {exc.status}); {details}"


def write_summary(results: list[tuple[str, str, str]]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as out:
        out.write("## Forkkeeper synchronization\n\n| Repository | Result | Details |\n|---|---|---|\n")
        for name, status, detail in results:
            safe_detail = detail.replace("|", "\\|").replace("\n", " ")
            out.write(f"| `{name}` | `{status}` | {safe_detail} |\n")


def selected_forks(selected: list[dict[str, Any]], repository: str | None) -> list[str]:
    requested = repository.casefold() if repository else None
    return [
        r["full_name"] for r in selected
        if r.get("full_name") and r.get("fork")
        and not r.get("archived") and not r.get("disabled")
        and (requested is None or r["full_name"].casefold() == requested)
    ]


def sync_repositories(
    api: GitHub, repositories: list[str], *, mode: str, dry_run: bool,
) -> list[tuple[str, str, str]]:
    """Synchronize each repository, recording failures without stopping the batch."""
    results: list[tuple[str, str, str]] = []
    for name in sorted(repositories):
        try:
            fork = describe_fork(api, name)
            status, detail = sync_one(api, fork, mode=mode, dry_run=dry_run)
        except (SyncError, subprocess.TimeoutExpired) as exc:
            status, detail = "error", str(exc)
        results.append((name, status, detail))
        print(json.dumps({"repository": name, "status": status, "details": detail}), flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync forks selected via GitHub App installation")
    parser.add_argument("--repo", help="Only process this installed owner/repo")
    parser.add_argument("--mode", choices=("direct", "pr"), default="direct",
                        help="Direct GitHub merge-upstream with PR fallback, or PR-only")
    parser.add_argument("--dry-run", action="store_true", help="Check without writing")
    args = parser.parse_args(argv)
    try:
        if args.repo:
            parse_repo_name(args.repo)
        api = GitHub(os.getenv("GH_APP_TOKEN", ""))
        selected = api.selected_repositories()
        names = {r["full_name"].casefold() for r in selected if r.get("full_name")}
        if args.repo and args.repo.casefold() not in names:
            raise SyncError(f"{args.repo} was not selected in this GitHub App installation")
        candidates = selected_forks(selected, args.repo)
        results = sync_repositories(api, candidates, mode=args.mode, dry_run=args.dry_run)
        if not results:
            print("No eligible fork repositories selected. Select forks in the App installation settings.")
        write_summary(results)
        return 1 if any(status == "error" for _, status, _ in results) else 0
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
