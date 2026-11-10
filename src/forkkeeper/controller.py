"""Central controller for a public, multi-tenant GitHub App.

This process is operator-only. Installers never receive the private key or
access to installation credentials. Run only on an access-controlled worker.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import jwt
from cryptography.exceptions import UnsupportedAlgorithm

from forkkeeper.sync import (
    GitHub,
    SyncError,
    parse_repo_name,
    selected_forks,
    sync_repositories,
)


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


@dataclass(frozen=True)
class Installation:
    id: int
    account: str
    workflows: bool = False


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
    api: GitHub, *, repository: str | None, mode: str, dry_run: bool,
) -> list[tuple[str, str, str]]:
    """Process only repos granted to this specific installation token."""
    candidates = selected_forks(api.selected_repositories(), repository)
    return sync_repositories(api, candidates, mode=mode, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process every GitHub App installation")
    parser.add_argument("--account", help="Limit to one installed user/organization")
    parser.add_argument("--repo", help="Limit to one selected owner/repository")
    parser.add_argument("--mode", choices=("direct", "pr"), default="direct")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.repo:
            parse_repo_name(args.repo)
        app = GitHubApp(
            os.getenv("FORKKEEPER_APP_CLIENT_ID", ""),
            os.getenv("FORKKEEPER_APP_PRIVATE_KEY", ""),
        )
        generate_app_jwt(app.client_id, app.private_key)
        installations = app.installations()
        if args.account:
            installations = [i for i in installations if i.account.casefold() == args.account.casefold()]
        results: list[tuple[str, str, str]] = []
        matched = False
        for installation in installations:
            try:
                api = app.installation_api(installation)
                current = sync_installation(
                    api, repository=args.repo, mode=args.mode, dry_run=args.dry_run
                )
                matched |= bool(current)
                results.extend(current)
            except SyncError as exc:
                results.append((f"installation:{installation.account}", "error", str(exc)))
        if args.repo and not matched:
            return 1
        if not installations:
            return int(bool(args.account))
        return 1 if any(status == "error" for _, status, _ in results) else 0
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
