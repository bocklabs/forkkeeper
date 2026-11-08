"""Central controller for a public, multi-tenant GitHub App.

This process is operator-only. Installers never receive the private key or
access to installation credentials. Run only on an access-controlled worker.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.exceptions import UnsupportedAlgorithm

from forkkeeper.sync import (
    GitHub,
    SyncError,
    describe_fork,
    parse_repo_name,
    sync_one,
    write_summary,
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
                installations.append(Installation(installation_id, account))
            if len(batch) < 100:
                break
            page += 1
        return installations

    def installation_api(self, installation: Installation) -> GitHub:
        response = self.app_api().request(
            "POST", f"/app/installations/{installation.id}/access_tokens",
            {"permissions": {"contents": "write", "pull_requests": "write"}},
        )
        token = response.get("token", "")
        if not token:
            raise SyncError("GitHub did not issue an installation access token")
        return GitHub(token)


def sync_installation(
    api: GitHub, *, repository: str | None, mode: str, dry_run: bool,
) -> list[tuple[str, str, str]]:
    """Process only repos granted to this specific installation token."""
    selected = api.selected_repositories()
    names = {r.get("full_name") for r in selected}
    if repository and repository not in names:
        return []
    candidates = [
        r["full_name"] for r in selected
        if r.get("full_name") and r.get("fork")
        and not r.get("archived") and not r.get("disabled")
        and (not repository or r["full_name"] == repository)
    ]
    results: list[tuple[str, str, str]] = []
    for name in sorted(candidates):
        try:
            fork = describe_fork(api, name)
            status, detail = sync_one(api, fork, mode=mode, dry_run=dry_run)
        except (SyncError, subprocess.TimeoutExpired) as exc:
            status, detail = "error", str(exc)
        results.append((name, status, detail))
        print(json.dumps({"repository": name, "status": status, "details": detail}), flush=True)
    return results


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
            print(f"Processing installation account {installation.account}", flush=True)
            try:
                api = app.installation_api(installation)
                current = sync_installation(
                    api, repository=args.repo, mode=args.mode, dry_run=args.dry_run
                )
                matched |= bool(current)
                results.extend(current)
            except SyncError as exc:
                results.append((f"installation:{installation.account}", "error", str(exc)))
                print(f"ERROR for installation {installation.account}: {exc}", file=sys.stderr)
        write_summary(results)
        if args.repo and not matched:
            print("Requested fork is not available in any selected installation", file=sys.stderr)
            return 1
        if not installations:
            print("No active GitHub App installations found")
        return 1 if any(status == "error" for _, status, _ in results) else 0
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
