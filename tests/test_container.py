"""Offline tests for the container's batch and periodic entrypoint."""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "container" / "entrypoint.sh"


@unittest.skipUnless(shutil.which("sh"), "Container entrypoint requires a POSIX shell")
class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.command = self.root / "forkkeeper-public"
        self.env = dict(os.environ, PATH=f"{self.root}{os.pathsep}{os.environ['PATH']}")
        self.env.pop("FORKKEEPER_APP_PRIVATE_KEY_FILE", None)
        self.env["FORKKEEPER_INTERVAL_SECONDS"] = "0"

    def script(self, content: str) -> None:
        self.command.write_text(f"#!/bin/sh\n{content}\n", encoding="utf-8")
        self.command.chmod(0o755)

    def run_entrypoint(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(ENTRYPOINT), *args], env=self.env,
            capture_output=True, text=True, timeout=5, check=False,
        )

    def test_one_batch_preserves_arguments_and_exit_status(self):
        self.script('printf "%s\\n" "$@"; exit 7')
        result = self.run_entrypoint("--repo", "some/fork", "--dry-run")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.splitlines(), ["--repo", "some/fork", "--dry-run"])

    def test_private_key_file_is_loaded(self):
        key = self.root / "key.pem"
        key.write_text("test-key\n", encoding="utf-8")
        self.env["FORKKEEPER_APP_PRIVATE_KEY_FILE"] = str(key)
        self.script('test "$FORKKEEPER_APP_PRIVATE_KEY" = "test-key"')
        self.assertEqual(self.run_entrypoint().returncode, 0)

    def test_invalid_interval_is_rejected(self):
        self.script("exit 0")
        for value in ("-1", "bad", "1.5"):
            with self.subTest(interval=value):
                self.env["FORKKEEPER_INTERVAL_SECONDS"] = value
                result = self.run_entrypoint()
                self.assertEqual(result.returncode, 2)
                self.assertIn("nonnegative integer", result.stderr)

    def test_periodic_failure_retries_and_sigterm_stops_sleep(self):
        self.script('echo batch; exit 7')
        self.env["FORKKEEPER_INTERVAL_SECONDS"] = "1"
        with subprocess.Popen(
            ["sh", str(ENTRYPOINT)], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as process:
            try:
                output = process.stdout
                if output is None:
                    self.fail("Container output was not captured")
                with selectors.DefaultSelector() as selector:
                    selector.register(output, selectors.EVENT_READ)
                    for _ in range(2):
                        self.assertTrue(selector.select(timeout=5), "Batch did not run")
                        self.assertEqual(output.readline().strip(), "batch")
                process.terminate()
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertIn("batch exited with status 7", stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
