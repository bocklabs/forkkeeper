import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

import jwt
from cryptography.exceptions import UnsupportedAlgorithm

from forkkeeper.controller import (
    GitHubApp,
    generate_app_jwt,
    main,
    sync_installation,
)
from forkkeeper.models import Fork, Installation
from forkkeeper.sync import GitHub, SyncError


class AppControllerTests(unittest.TestCase):
    @patch("time.time", return_value=1000)
    def test_installation_token_is_renewed_for_the_same_installation(self, clock):
        app = GitHubApp("client", "private")
        with patch.object(app, "app_api") as api:
            api.return_value.request.side_effect = [
                {"token": "first", "expires_at": "1970-01-01T01:16:40Z"},
                {"token": "renewed", "expires_at": "1970-01-01T02:13:20Z"},
            ]
            client = app.installation_api(Installation(42, "alice"))
            self.assertEqual(client.token, "first")
            clock.return_value = 3800
            self.assertEqual(client.token, "renewed")
            self.assertEqual(client.token, "renewed")
            self.assertEqual(api.return_value.request.call_args_list, [
                unittest.mock.call("POST", "/app/installations/42/access_tokens",
                                   {"permissions": {"contents": "write", "pull_requests": "write"}}),
                unittest.mock.call("POST", "/app/installations/42/access_tokens",
                                   {"permissions": {"contents": "write", "pull_requests": "write"}}),
            ])

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    @patch("forkkeeper.controller.GitHubApp")
    def test_unknown_account_fails(self, cls, generate):
        cls.return_value.installations.return_value = [Installation(1, "alice")]
        with patch.dict(os.environ, {"FORKKEEPER_APP_CLIENT_ID": "c", "FORKKEEPER_APP_PRIVATE_KEY": "p"}):
            self.assertEqual(main(["--account", "missing"]), 1)
        cls.return_value.installation_api.assert_not_called()

    def test_invalid_app_credentials(self):
        with (
            patch.dict(os.environ, {"FORKKEEPER_APP_CLIENT_ID": "", "FORKKEEPER_APP_PRIVATE_KEY": ""}),
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main([]), 1)
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["batch_started", "batch_completed"])
        self.assertEqual(events[-1]["exit_code"], 1)
        self.assertEqual(events[-1]["level"], "error")
        self.assertEqual(events[-1]["installations_processed"], 0)
        self.assertEqual(events[-1]["repositories_processed"], 0)
        self.assertIn("client ID and private key must both be supplied", stderr.getvalue())

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
             {"id": 102, "account": {"login": "another"},
              "permissions": {"workflows": "write", "administration": "write"}}],
        ]
        with patch("forkkeeper.controller.GitHub", return_value=repo):
            installations = app.installations()
        self.assertEqual(len(installations), 101)
        self.assertEqual(installations[-1], Installation(102, "another", workflows=True))
        self.assertEqual(repo.request.call_count, 2)

    def test_access_token_is_per_installation(self):
        app = GitHubApp("client", "private")
        with patch.object(app, "app_api") as api:
            api.return_value.request.return_value = {"token": "customer-token", "expires_at": "2099-01-01T00:00:00Z"}
            instance = app.installation_api(Installation(42, "alice", workflows=True))
        self.assertIsInstance(instance, GitHub)
        self.assertEqual(instance.token, "customer-token")
        api.return_value.request.assert_called_once_with(
            "POST", "/app/installations/42/access_tokens",
            {"permissions": {"contents": "write", "pull_requests": "write", "workflows": "write"}},
        )

    def test_invalid_token_response_is_rejected(self):
        app = GitHubApp("client", "private")
        installation = Installation(42, "alice")
        responses = [
            {}, {"token": 42}, {"token": "test"},
            {"token": "test", "expires_at": "invalid"},
            {"token": "test", "expires_at": "2099-01-01T00:00:00"},
        ]
        for response in responses:
            with self.subTest(response=response), patch.object(app, "app_api") as api:
                api.return_value.request.return_value = response
                with self.assertRaises(SyncError):
                    app.installation_api(installation)

    @patch("forkkeeper.sync.sync_one", return_value=("synced", "done"))
    @patch("forkkeeper.sync.describe_fork", return_value=Fork("alice/fork", "main", "up/foo", "main"))
    def test_repository_selection_enforced(self, describe, sync):
        api = MagicMock()
        api.selected_repositories.return_value = [
            {"full_name": "alice/standalone", "fork": False},
            {"full_name": "alice/fork", "fork": True},
            {"full_name": "alice/disabled", "fork": True, "archived": True},
            {"full_name": "alice/blocked", "fork": True, "disabled": True},
            {"fork": True},
        ]
        results = sync_installation(api, repository=None, mode="direct", dry_run=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "alice/fork")
        describe.assert_called_once_with(api, "alice/fork")
        self.assertEqual(sync_installation(api, repository="bob/unselected", mode="direct", dry_run=False), [])
        self.assertEqual(sync.call_count, 1)

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    @patch("forkkeeper.controller.sync_installation", side_effect=[
        SyncError("private-installation-error"),
        [("bob/fork", "synced", "ok"), ("bob/broken", "error", "private-repository-error")],
    ])
    @patch("forkkeeper.controller.GitHubApp")
    def test_one_failed_installation_does_not_stop_next(self, cls, sync, generate):
        app = cls.return_value
        app.installations.return_value = [Installation(1, "alice"), Installation(2, "bob")]
        with (
            patch.dict(os.environ, {"FORKKEEPER_APP_CLIENT_ID": "c", "FORKKEEPER_APP_PRIVATE_KEY": "p"}),
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main([]), 1)
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["exit_code"], 1)
        self.assertEqual(events[-1]["level"], "error")
        self.assertEqual(events[-1]["installations_processed"], 2)
        self.assertEqual(events[-1]["installation_errors"], 1)
        self.assertEqual(events[-1]["repositories_processed"], 2)
        self.assertEqual(events[-1]["repository_errors"], 1)
        self.assertEqual(stderr.getvalue(), "")
        for private_value in ("alice", "bob", "private-installation-error", "private-repository-error", "jwt"):
            self.assertNotIn(private_value, stdout.getvalue())
        self.assertEqual(sync.call_count, 2)

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    @patch("forkkeeper.controller.sync_installation", return_value=[("bob/fork", "created-pr", "private-pr-url")])
    @patch("forkkeeper.controller.GitHubApp")
    def test_account_filter(self, cls, sync, generate):
        app = cls.return_value
        app.installations.return_value = [Installation(1, "alice"), Installation(2, "bob")]
        with (
            patch.dict(os.environ, {"FORKKEEPER_APP_CLIENT_ID": "c", "FORKKEEPER_APP_PRIVATE_KEY": "p"}),
            patch("forkkeeper.telemetry.time.monotonic", side_effect=[100, 102.5]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(main(["--account", "bob", "--mode", "pr", "--sync-tags"]), 0)
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["batch_started", "batch_completed"])
        self.assertEqual(events[-1]["exit_code"], 0)
        self.assertEqual(events[-1]["level"], "info")
        self.assertEqual(events[-1]["duration_seconds"], 2.5)
        self.assertEqual(events[-1]["installations_processed"], 1)
        self.assertEqual(events[-1]["installation_errors"], 0)
        self.assertEqual(events[-1]["repositories_processed"], 1)
        self.assertEqual(events[-1]["repository_errors"], 0)
        self.assertEqual(events[-1]["pull_requests_pending"], 1)
        self.assertEqual(events[-1]["service"], "forkkeeper")
        self.assertEqual(events[-1]["mode"], "pr")
        self.assertFalse(events[-1]["dry_run"])
        self.assertTrue(events[-1]["sync_tags"])
        self.assertNotIn("private-pr-url", stdout.getvalue())
        self.assertTrue(events[-1]["timestamp"].endswith("+00:00"))
        app.installation_api.assert_called_once_with(Installation(2, "bob"))
        sync.assert_called_once_with(app.installation_api.return_value, repository=None,
                                     mode="pr", dry_run=False, sync_tags=True)

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    @patch("forkkeeper.controller.sync_installation", return_value=[("bob/fork", "synced", "ok")])
    @patch("forkkeeper.controller.GitHubApp")
    def test_repository_filter_skips_unrelated_installations(self, cls, sync, generate):
        app = cls.return_value
        app.installations.return_value = [Installation(1, "alice"), Installation(2, "bob")]

        def installation_api(installation):
            if installation.account == "alice":
                raise SyncError("Unrelated installation token failed")
            return MagicMock()

        app.installation_api.side_effect = installation_api
        self.assertEqual(main(["--repo", "BOB/fork"]), 0)
        app.installation_api.assert_called_once_with(Installation(2, "bob"))

    @patch("forkkeeper.controller.generate_app_jwt", return_value="jwt")
    @patch("forkkeeper.controller.GitHubApp")
    def test_repository_filter_respects_account_filter(self, cls, generate):
        app = cls.return_value
        app.installations.return_value = [Installation(1, "alice"), Installation(2, "bob")]
        self.assertEqual(main(["--account", "alice", "--repo", "bob/fork"]), 1)
        app.installation_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
