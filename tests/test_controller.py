"""Tests for the multi-tenant GitHub App controller."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
from cryptography.exceptions import UnsupportedAlgorithm

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from forkkeeper.controller import (
    GitHubApp,
    Installation,
    generate_app_jwt,
    main,
    sync_installation,
)
from forkkeeper.sync import Fork, SyncError


class AppControllerTests(unittest.TestCase):
    def test_invalid_app_credentials(self):
        with self.assertRaises(SyncError):
            generate_app_jwt("", "")

    def test_signing_errors_do_not_expose_credentials(self):
        for error_type in (jwt.InvalidKeyError, ValueError, TypeError, AttributeError, UnsupportedAlgorithm):
            with self.subTest(error=error_type.__name__):
                with (
                    patch("forkkeeper.controller.jwt.encode", side_effect=error_type("private-key-content")),
                    self.assertRaises(SyncError) as raised,
                ):
                    generate_app_jwt("client", "private-key-content")
                self.assertEqual(str(raised.exception), "Could not sign GitHub App JWT; verify App credentials")
                self.assertTrue(raised.exception.__suppress_context__)

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    def test_pagination_and_suspended_filter(self, generate):
        app = GitHubApp("client", "private-key")
        repo = MagicMock()
        repo.request.side_effect = [
            [{"id": i, "account": {"login": f"account{i}"}} for i in range(100)],
            [{"id": 101, "account": {"login": "disabled"}, "suspended_at": "now"},
             {"id": 102, "account": {"login": "another"}}],
        ]
        with patch("forkkeeper.controller.GitHub", return_value=repo):
            installations = app.installations()
        self.assertEqual(len(installations), 101)
        self.assertEqual(installations[-1], Installation(102, "another"))
        self.assertEqual(repo.request.call_count, 2)

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    def test_access_token_is_per_installation(self, generate):
        app = GitHubApp("client", "private")
        with patch("forkkeeper.controller.GitHub") as github:
            github.return_value.request.return_value = {"token": "customer-token"}
            instance = app.installation_api(Installation(42, "alice"))
        self.assertEqual(instance, github.return_value)
        github.return_value.request.assert_called_once_with(
            "POST", "/app/installations/42/access_tokens",
            {"permissions": {"contents": "write", "pull_requests": "write"}},
        )
        github.assert_any_call("customer-token")

    @patch("forkkeeper.controller.sync_one", return_value=("synced", "done"))
    @patch("forkkeeper.controller.describe_fork", return_value=Fork("alice/fork", "main", "up/foo", "main"))
    def test_repository_selection_enforced(self, describe, sync):
        api = MagicMock()
        api.selected_repositories.return_value = [
            {"full_name": "alice/standalone", "fork": False},
            {"full_name": "alice/fork", "fork": True},
            {"full_name": "alice/disabled", "fork": True, "archived": True},
        ]
        results = sync_installation(api, repository=None, mode="direct", dry_run=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "alice/fork")
        describe.assert_called_once_with(api, "alice/fork")
        self.assertEqual(sync_installation(api, repository="bob/unselected", mode="direct", dry_run=False), [])
        self.assertEqual(sync.call_count, 1)

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    @patch("forkkeeper.controller.sync_installation", side_effect=[SyncError("revoked"), [("bob/fork", "synced", "ok")]])
    @patch("forkkeeper.controller.GitHubApp")
    def test_one_failed_installation_does_not_stop_next(self, cls, sync, generate):
        app = cls.return_value
        app.installations.return_value = [Installation(1, "alice"), Installation(2, "bob")]
        with patch.dict(os.environ, {"FORKKEEPER_APP_CLIENT_ID": "c", "FORKKEEPER_APP_PRIVATE_KEY": "p"}):
            self.assertEqual(main([]), 1)
        self.assertEqual(sync.call_count, 2)

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    @patch("forkkeeper.controller.sync_installation", return_value=[("bob/fork", "synced", "ok")])
    @patch("forkkeeper.controller.GitHubApp")
    def test_account_filter(self, cls, sync, generate):
        app = cls.return_value
        app.installations.return_value = [Installation(1, "alice"), Installation(2, "bob")]
        with patch.dict(os.environ, {"FORKKEEPER_APP_CLIENT_ID": "c", "FORKKEEPER_APP_PRIVATE_KEY": "p"}):
            self.assertEqual(main(["--account", "bob"]), 0)
        app.installation_api.assert_called_once_with(Installation(2, "bob"))


if __name__ == "__main__":
    unittest.main()
