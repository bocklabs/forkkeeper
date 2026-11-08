"""Offline regression tests for fork synchronization decision logic."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from forkkeeper.sync import (
    Fork,
    Git,
    GitHub,
    GitHubError,
    SyncError,
    describe_fork,
    main,
    parse_repo_name,
    pr_sync,
    sync_one,
    validate_branch,
)


class ValidationTests(unittest.TestCase):
    def test_full_repo_name(self):
        self.assertEqual(parse_repo_name("bocklabs/example"), ("bocklabs", "example"))
        for invalid in ("x", "owner/repo/extra", "owner/-- bad", "../repo", "foo/.git/other"):
            with self.subTest(invalid=invalid), self.assertRaises(SyncError):
                parse_repo_name(invalid)

    def test_branch_validation(self):
        self.assertEqual(validate_branch("release/2.0"), "release/2.0")
        for bad in ("", "refs..foo", "main;echo hi", "bad branch", "a//b", "a.lock", "refs/@{a", "../bad", "main/", "main."):
            with self.subTest(bad=bad), self.assertRaises(SyncError):
                validate_branch(bad)

    def test_repo_metadata(self):
        api = MagicMock()
        api.repository.return_value = {
            "fork": True, "archived": False, "default_branch": "main",
            "parent": {"full_name": "upstream/project", "default_branch": "develop"},
        }
        self.assertEqual(describe_fork(api, "bocklabs/project"),
                         Fork("bocklabs/project", "main", "upstream/project", "develop"))

    def test_missing_parent_is_error(self):
        api = MagicMock()
        api.repository.return_value = {"fork": True, "default_branch": "main"}
        with self.assertRaisesRegex(SyncError, "parent"):
            describe_fork(api, "bocklabs/project")


class ApiTests(unittest.TestCase):
    @patch("forkkeeper.sync.urllib.request.urlopen")
    def test_truncated_response_is_a_sync_error(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.side_effect = IncompleteRead(b"partial", 10)
        with self.assertRaises(SyncError):
            GitHub("fake").request("GET", "/test")

    @patch("forkkeeper.sync.urllib.request.urlopen")
    def test_timeout_reading_http_error_preserves_status(self, urlopen):
        body = MagicMock()
        body.closed = False
        body.read.side_effect = TimeoutError("timed out")
        urlopen.side_effect = urllib.error.HTTPError(
            "https://api.github.com/test", 503, "Unavailable", Message(), body,
        )
        with self.assertRaises(GitHubError) as raised:
            GitHub("fake").request("GET", "/test")
        self.assertEqual(raised.exception.status, 503)

    @patch("forkkeeper.sync.urllib.request.urlopen", side_effect=TimeoutError("timed out"))
    def test_timeout_is_a_sync_error(self, urlopen):
        with self.assertRaises(SyncError):
            GitHub("fake").request("GET", "/installation/repositories")

    @patch("forkkeeper.sync.urllib.request.urlopen")
    def test_invalid_json_is_a_sync_error(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = b"not JSON"
        with self.assertRaises(SyncError):
            GitHub("fake").request("GET", "/installation/repositories")

    @patch("forkkeeper.sync.urllib.request.urlopen")
    def test_http_error_with_non_object_json_remains_a_github_error(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://api.github.com/test", 502, "Bad gateway", Message(), io.BytesIO(b'[]'),
        )
        with self.assertRaises(GitHubError) as raised:
            GitHub("fake").request("GET", "/test")
        self.assertEqual(raised.exception.status, 502)

    @patch("forkkeeper.sync.time.time", return_value=1500)
    @patch("forkkeeper.sync.urllib.request.urlopen")
    def test_token_refresh_is_used_by_http_and_git(self, urlopen, clock):
        renew = MagicMock(return_value=("renewed", 10000))
        api = GitHub("old", expires_at=2000, refresh_token=renew)
        self.assertEqual(api.token, "renewed")
        urlopen.return_value.__enter__.return_value.read.return_value = b'{}'
        api.request("GET", "/test")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer renewed")
        with tempfile.TemporaryDirectory() as directory:
            git = Git(api.token, Path(directory))
            self.assertEqual(git.env["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46cmVuZXdlZA==")
        renew.assert_called_once()

    def test_selected_repo_pagination(self):
        api = GitHub("fake")
        with patch.object(api, "request", side_effect=[
            {"repositories": [{"full_name": f"o/r{i}"} for i in range(100)]},
            {"repositories": [{"full_name": "o/r100"}]},
        ]) as request:
            self.assertEqual(len(api.selected_repositories()), 101)
            self.assertEqual(request.call_count, 2)

    def test_native_clean_sync(self):
        api = MagicMock()
        api.sync_native.return_value = {"message": "Fast-forwarded"}
        fork = Fork("bocklabs/foo", "main", "acme/foo", "main")
        self.assertEqual(sync_one(api, fork, mode="direct", dry_run=False), ("synced", "Fast-forwarded"))

    @patch("forkkeeper.sync.pr_sync", return_value=("created-pr", "https://github.com/bocklabs/foo/pull/1"))
    def test_conflict_fallback_to_pr(self, fallback):
        api = MagicMock()
        api.sync_native.side_effect = GitHubError(409, "Merge conflict")
        fork = Fork("bocklabs/foo", "main", "acme/foo", "main")
        status, message = sync_one(api, fork, mode="direct", dry_run=False)
        self.assertEqual(status, "created-pr")
        self.assertIn("409", message)
        fallback.assert_called_once()

    @patch("forkkeeper.sync.pr_sync")
    def test_denied_write_does_not_try_pr(self, fallback):
        api = MagicMock()
        api.sync_native.side_effect = GitHubError(403, "Access denied")
        with self.assertRaises(GitHubError):
            sync_one(api, Fork("bocklabs/foo", "main", "acme/foo", "main"), mode="direct", dry_run=False)
        fallback.assert_not_called()

    @patch("forkkeeper.sync.pr_sync", return_value=("would-open-pr", "Would open a PR"))
    @patch("forkkeeper.sync.Git")
    def test_dry_run_never_calls_native_write(self, git_cls, fallback):
        git_cls.return_value.upstream_already_merged.return_value = False
        for base in ("main", "forkkeeper/main"):
            with self.subTest(base=base):
                api = MagicMock(token="fake")
                status, detail = sync_one(api, Fork("bocklabs/foo", base, "acme/foo", "main"),
                                          mode="direct", dry_run=True)
                self.assertEqual(status, "would-sync")
                self.assertIn("native", detail)
                api.sync_native.assert_not_called()
                api.create_pr.assert_not_called()
                git_cls.return_value.push_sync_branch.assert_not_called()
                fallback.assert_not_called()

    @patch("forkkeeper.sync.Git")
    def test_direct_dry_run_reports_current_forks(self, git_cls):
        git_cls.return_value.upstream_already_merged.return_value = True
        api = MagicMock(token="fake")
        status, _ = sync_one(api, Fork("bocklabs/foo", "main", "acme/foo", "main"),
                             mode="direct", dry_run=True)
        self.assertEqual(status, "current")
        api.sync_native.assert_not_called()
        git_cls.return_value.push_sync_branch.assert_not_called()


class PrSyncTests(unittest.TestCase):
    @patch("forkkeeper.sync.Git")
    def test_pr_branch_cannot_be_the_default_branch(self, git_cls):
        api = MagicMock(token="fake")
        with self.assertRaises(SyncError):
            pr_sync(api, Fork("alice/fork", "forkkeeper/main", "upstream/fork", "main"))
        git_cls.assert_not_called()
        api.create_pr.assert_not_called()

    @patch("forkkeeper.sync.Git")
    def test_concurrent_pr_creation_is_reused(self, git_cls):
        git_cls.return_value.upstream_already_merged.return_value = False
        api = MagicMock(token="fake")
        api.open_prs.side_effect = [[], [{"html_url": "https://github.com/alice/fork/pull/1"}]]
        api.create_pr.side_effect = GitHubError(422, "PR already exists")
        self.assertEqual(
            pr_sync(api, Fork("alice/fork", "main", "upstream/fork", "main")),
            ("updated-pr", "https://github.com/alice/fork/pull/1"),
        )

    @patch("forkkeeper.sync.Git")
    def test_current_fork_no_push(self, git_cls):
        git = git_cls.return_value
        git.upstream_already_merged.return_value = True
        api = MagicMock(token="fake")
        result = pr_sync(api, Fork("bocklabs/foo", "main", "other/foo", "main"))
        self.assertEqual(result[0], "current")
        git.push_sync_branch.assert_not_called()

    @patch("forkkeeper.sync.Git")
    def test_create_pr(self, git_cls):
        git = git_cls.return_value
        git.upstream_already_merged.return_value = False
        api = MagicMock(token="fake")
        api.open_prs.return_value = []
        api.create_pr.return_value = {"html_url": "https://github.com/bocklabs/foo/pull/9"}
        result = pr_sync(api, Fork("bocklabs/foo", "main", "other/foo", "dev"))
        self.assertEqual(result, ("created-pr", "https://github.com/bocklabs/foo/pull/9"))
        git.push_sync_branch.assert_called_once_with("forkkeeper/dev")

    @patch("forkkeeper.sync.Git")
    def test_update_existing_pr(self, git_cls):
        git_cls.return_value.upstream_already_merged.return_value = False
        api = MagicMock(token="fake")
        api.open_prs.return_value = [{"html_url": "https://github.com/bocklabs/foo/pull/9"}]
        result = pr_sync(api, Fork("bocklabs/foo", "main", "other/foo", "main"))
        self.assertEqual(result[0], "updated-pr")
        api.create_pr.assert_not_called()

    @patch("forkkeeper.sync.Git")
    def test_pr_dry_run_no_writes(self, git_cls):
        git_cls.return_value.upstream_already_merged.return_value = False
        api = MagicMock(token="fake")
        result = pr_sync(api, Fork("bocklabs/foo", "main", "other/foo", "main"), dry_run=True)
        self.assertEqual(result[0], "would-open-pr")
        api.create_pr.assert_not_called()
        git_cls.return_value.push_sync_branch.assert_not_called()


class GitHistoryTests(unittest.TestCase):
    def test_merge_base_detects_behind_and_caught_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def run(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
            run("init", "-q")
            run("config", "user.name", "Test")
            run("config", "user.email", "test@example.com")
            (root / "test.txt").write_text("first", encoding="utf-8")
            run("add", ".")
            run("commit", "-qm", "First")
            run("branch", "fork-point")
            (root / "test.txt").write_text("second", encoding="utf-8")
            run("add", ".")
            run("commit", "-qm", "Second")
            run("update-ref", "refs/remotes/upstream/base", run("rev-parse", "HEAD"))
            run("update-ref", "refs/remotes/fork/base", run("rev-parse", "fork-point"))
            git = Git("fake", root)
            self.assertFalse(git.upstream_already_merged())
            run("update-ref", "refs/remotes/fork/base", run("rev-parse", "HEAD"))
            self.assertTrue(git.upstream_already_merged())
            run("checkout", "-q", "--orphan", "unrelated")
            run("commit", "--allow-empty", "-qm", "Unrelated")
            run("update-ref", "refs/remotes/upstream/base", run("rev-parse", "HEAD"))
            with self.assertRaisesRegex(SyncError, "no common commit history"):
                git.upstream_already_merged()


class CommandTests(unittest.TestCase):
    @patch.dict(os.environ, {"GH_APP_TOKEN": "fake"})
    @patch("forkkeeper.sync.sync_one", return_value=("synced", "ok"))
    @patch("forkkeeper.sync.describe_fork", return_value=Fork("Alice/Fork", "main", "upstream/fork", "main"))
    @patch("forkkeeper.sync.GitHub")
    def test_repository_filter_ignores_case(self, api_cls, describe, sync):
        api_cls.return_value.selected_repositories.return_value = [{"full_name": "Alice/Fork", "fork": True}]
        self.assertEqual(main(["--repo", "alice/fork"]), 0)
        describe.assert_called_once_with(api_cls.return_value, "Alice/Fork")

    @patch.dict(os.environ, {"GH_APP_TOKEN": "fake"})
    @patch("forkkeeper.sync.write_summary")
    @patch("forkkeeper.sync.sync_one", return_value=("synced", "ok"))
    @patch("forkkeeper.sync.describe_fork")
    @patch("forkkeeper.sync.GitHub")
    def test_failed_repository_does_not_stop_next(self, api_cls, describe, sync, summary):
        api = api_cls.return_value
        api.selected_repositories.return_value = [
            {"full_name": "bocklabs/b", "fork": True},
            {"full_name": "bocklabs/a", "fork": True},
        ]
        fork = Fork("bocklabs/b", "main", "upstream/b", "main")
        for error in (SyncError("failed"), subprocess.TimeoutExpired("git", 180)):
            with self.subTest(error=type(error).__name__):
                describe.side_effect = [error, fork]
                self.assertEqual(main(["--mode", "pr", "--dry-run"]), 1)
                self.assertEqual(describe.call_args_list[-2:], [
                    unittest.mock.call(api, "bocklabs/a"),
                    unittest.mock.call(api, "bocklabs/b"),
                ])
                sync.assert_called_with(api, fork, mode="pr", dry_run=True)
                summary.assert_called_with([
                    ("bocklabs/a", "error", str(error)),
                    ("bocklabs/b", "synced", "ok"),
                ])

    @patch.dict(os.environ, {"GH_APP_TOKEN": "fake"})
    @patch("forkkeeper.sync.sync_one", return_value=("synced", "ok"))
    @patch("forkkeeper.sync.describe_fork", return_value=Fork("bocklabs/fork", "main", "src/fork", "main"))
    @patch("forkkeeper.sync.GitHub")
    def test_only_selected_forks_processed(self, api_cls, describe, sync):
        api_cls.return_value.selected_repositories.return_value = [
            {"full_name": "bocklabs/controller", "fork": False},
            {"full_name": "bocklabs/fork", "fork": True},
            {"full_name": "bocklabs/archived", "fork": True, "archived": True},
            {"fork": True},
        ]
        self.assertEqual(main([]), 0)
        describe.assert_called_once_with(api_cls.return_value, "bocklabs/fork")
        sync.assert_called_once()

    @patch.dict(os.environ, {"GH_APP_TOKEN": "fake"})
    @patch("forkkeeper.sync.GitHub")
    def test_reject_unselected_repo(self, api_cls):
        api_cls.return_value.selected_repositories.return_value = [{"full_name": "bocklabs/a", "fork": True}]
        self.assertEqual(main(["--repo", "bocklabs/b"]), 1)


if __name__ == "__main__":
    unittest.main()
