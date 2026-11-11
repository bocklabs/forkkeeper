import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Any

import jwt
from cryptography.exceptions import UnsupportedAlgorithm

from forkkeeper.models import Installation
from forkkeeper.sync import (
    GitHub,
    SyncError,
    parse_repo_name,
    selected_forks,
    sync_repositories,
)
from forkkeeper.telemetry import batch_telemetry


def generate_app_jwt(client_id: str, private_key: str) -> str:
    """Create a GitHub App JWT valid for under 10 minutes."""
    if not client_id or not private_key:
        raise SyncError("GitHub App client ID and private key must both be supplied")
    now = int(time.time())
    try:
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": client_id},
            private_key,
            algorithm="RS256",
        )
    except (jwt.PyJWTError, ValueError, TypeError, AttributeError, UnsupportedAlgorithm):
        raise SyncError("Could not sign GitHub App JWT; verify App credentials") from None


class GitHubApp:
    def __init__(self, client_id: str, private_key: str):
        self.client_id = client_id
        self.private_key = private_key

    def app_api(self) -> GitHub:
        return GitHub(generate_app_jwt(self.client_id, self.private_key))

    def installations(self) -> list[Installation]:
        installations: list[Installation] = []
        page = 1
        while True:
            batch: list[dict[str, Any]] = self.app_api().request(
                "GET", f"/app/installations?per_page=100&page={page}"
            )
            for item in batch:
                if item.get("suspended_at"):
                    continue
                account = (item.get("account") or {}).get("login", "")
                installation_id = item.get("id")
                if not account or not isinstance(installation_id, int):
                    raise SyncError("GitHub returned installation without valid ID/account")
                installations.append(Installation(
                    installation_id, account,
                    workflows=(item.get("permissions") or {}).get("workflows") == "write",
                ))
            if len(batch) < 100:
                break
            page += 1
        return installations

    def installation_api(self, installation: Installation) -> GitHub:
        token, expires_at = self._installation_token(installation)
        return GitHub(token, expires_at=expires_at,
                      refresh_token=lambda: self._installation_token(installation))

    def _installation_token(self, installation: Installation) -> tuple[str, float]:
        permissions = {"contents": "write", "pull_requests": "write"}
        if installation.workflows:
            permissions["workflows"] = "write"
        response = self.app_api().request(
            "POST", f"/app/installations/{installation.id}/access_tokens",
            {"permissions": permissions},
        )
        token = response.get("token", "")
        if not isinstance(token, str) or not token:
            raise SyncError("GitHub did not issue an installation access token")
        try:
            expires_at = datetime.fromisoformat(response.get("expires_at", ""))
        except (TypeError, ValueError):
            raise SyncError("GitHub returned an invalid installation token expiration") from None
        if expires_at.tzinfo is None:
            raise SyncError("GitHub returned an installation token expiration without a timezone")
        return token, expires_at.timestamp()


def sync_installation(
    api: GitHub, *, repository: str | None, mode: str, dry_run: bool, sync_tags: bool = False,
) -> list[tuple[str, str, str]]:
    """Process only repos granted to this specific installation token."""
    candidates = selected_forks(api.selected_repositories(), repository)
    return sync_repositories(api, candidates, mode=mode, dry_run=dry_run, sync_tags=sync_tags)


def _sync_batch(args: argparse.Namespace, counts: dict[str, int]) -> int:
    repo_owner = parse_repo_name(args.repo)[0] if args.repo else None
    app = GitHubApp(
        os.getenv("FORKKEEPER_APP_CLIENT_ID", ""),
        os.getenv("FORKKEEPER_APP_PRIVATE_KEY", ""),
    )
    generate_app_jwt(app.client_id, app.private_key)
    installations = [
        i for i in app.installations()
        if (not args.account or i.account.casefold() == args.account.casefold())
        and (repo_owner is None or i.account.casefold() == repo_owner.casefold())
    ]
    for installation in installations:
        counts["installations_processed"] += 1
        try:
            api = app.installation_api(installation)
            current = sync_installation(
                api, repository=args.repo, mode=args.mode, dry_run=args.dry_run, sync_tags=args.sync_tags
            )
            statuses = Counter(status for _, status, _ in current)
            counts["repositories_processed"] += len(current)
            counts["repository_errors"] += statuses["error"]
            counts["pull_requests_pending"] += statuses["created-pr"] + statuses["updated-pr"]
        except SyncError:
            counts["installation_errors"] += 1
    if args.repo and not counts["repositories_processed"]:
        return 1
    if not installations:
        return int(bool(args.account))
    return int(bool(counts["repository_errors"] or counts["installation_errors"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process every GitHub App installation")
    parser.add_argument("--account", help="Limit to one installed user/organization")
    parser.add_argument("--repo", help="Limit to one selected owner/repository")
    parser.add_argument("--mode", choices=("direct", "pr"), default="direct")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sync-tags", action="store_true", help="Copy missing upstream tags without replacing existing tags")
    args = parser.parse_args(argv)
    with batch_telemetry(args.mode, args.dry_run, sync_tags=args.sync_tags) as counts:
        try:
            counts["exit_code"] = _sync_batch(args, counts)
        except SyncError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
        return counts["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
