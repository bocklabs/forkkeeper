import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime


@contextmanager
def batch_telemetry(mode: str, dry_run: bool, *, sync_tags: bool = False) -> Iterator[dict[str, int]]:
    counts = {
        "exit_code": 1,
        "installations_processed": 0,
        "installation_errors": 0,
        "repositories_processed": 0,
        "repository_errors": 0,
        "pull_requests_pending": 0,
    }
    fields = {"service": "forkkeeper", "mode": mode, "dry_run": dry_run, "sync_tags": sync_tags}
    started = time.monotonic()
    print(json.dumps({
        **fields,
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "info",
        "event": "batch_started",
    }), flush=True)
    try:
        yield counts
    finally:
        print(json.dumps({
            **fields,
            **counts,
            "timestamp": datetime.now(UTC).isoformat(),
            "level": "info" if counts["exit_code"] == 0 else "error",
            "event": "batch_completed",
            "duration_seconds": round(time.monotonic() - started, 3),
        }), flush=True)
