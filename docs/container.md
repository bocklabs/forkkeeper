# Container deployment

The image workflow builds and tests pull requests using Docker Buildx without
publishing. After a Release Please PR is merged, the release workflow calls it
with the newly created release tag. It checks out that tag, tests the image, and
publishes AMD64 and ARM64 images to `ghcr.io/bocklabs/forkkeeper` using the same tag,
such as `0.3.0`, and updates `latest` to the same image. Ordinary merges and tag
pushes do not trigger image publishing.
Actions and the base image are pinned to immutable hashes.
Build and test steps run in the `image` job. The separate `publish` job runs only
after those steps pass and checks out the same commit that the tests used.

[Release Please](releases.md) manages version tags and GitHub releases.

The image installs Python from `.python-version` and production dependencies from
`uv.lock`. It includes Git and CA certificates and runs as UID/GID `10001:10001`.

## Configuration

| Variable | Purpose |
|---|---|
| `FORKKEEPER_APP_CLIENT_ID` | GitHub App client ID |
| `FORKKEEPER_APP_PRIVATE_KEY_FILE` | Path to a mounted PEM file inside the container |
| `FORKKEEPER_APP_PRIVATE_KEY` | Alternative: PEM contents supplied through the environment |
| `FORKKEEPER_INTERVAL_SECONDS` | Delay in seconds after each batch; default `0` runs one batch and exits |

The key file takes precedence over the environment value. A positive interval
runs a batch immediately, then waits after each completed batch. `3600` means
one hour, and `21600` means six hours. The batch duration adds to this interval.
Failed batches retry after the same delay. SIGTERM stops the active batch or
sleep. Run one replica to avoid duplicate processing.

Application errors, such as invalid credentials or configuration, go to stderr.
Aggregate batch telemetry goes to stdout as JSON lines. Repository names,
account names, and repository error details are not included in telemetry.
GitHub job summaries are disabled. A single batch exits with `0` for success,
`1` for synchronization failure, or `2` for invalid arguments or interval.
Periodic mode continues after failed batches, so a running container does not
prove successful synchronization. GitHub Actions retains build and test logs.

## Docker Compose example

Use a published release version in the image reference below.

Create a `.env` file with your App client ID and desired interval:

```dotenv
FORKKEEPER_APP_CLIENT_ID=replace-with-your-app-client-id
FORKKEEPER_INTERVAL_SECONDS=21600
```

Place the App key in `app-private-key.pem` beside `compose.yaml`. Keep the env and
key files out of version control. The key must be readable by container UID
`10001`; on a standard Linux Docker host, use owner `10001:10001` and mode `400`
for this dedicated copy. Account for mapped UIDs if using user namespace remapping.

```yaml
services:
  forkkeeper:
    image: ghcr.io/bocklabs/forkkeeper:0.3.0
    init: true
    env_file: .env
    environment:
      FORKKEEPER_APP_PRIVATE_KEY_FILE: /run/secrets/forkkeeper.pem
      FORKKEEPER_INTERVAL_SECONDS: ${FORKKEEPER_INTERVAL_SECONDS:-21600}
    volumes:
      - ./app-private-key.pem:/run/secrets/forkkeeper.pem:ro
    command: ["--mode", "direct", "--dry-run"]
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp:mode=1777
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
```

For reproducible deployments, append the image digest reported by the publishing
workflow, such as `ghcr.io/bocklabs/forkkeeper:0.3.0@sha256:<published-digest>`.

Validate the configuration and run one dry-run batch:

```bash
docker compose config --quiet
docker compose run --rm -e FORKKEEPER_INTERVAL_SECONDS=0 forkkeeper
```

Check the exit code and the `batch_completed` event. Dry runs emit the same
telemetry with `dry_run: true`. When ready to
synchronize, remove `--dry-run` from `command` and run `docker compose up -d`.
Set `FORKKEEPER_INTERVAL_SECONDS` in `.env` to change the delay, then recreate
the service with `docker compose up -d`.

For `docker run`, pass the delay with `-e FORKKEEPER_INTERVAL_SECONDS=3600`.
Existing deployments must recreate their containers to apply the new logging behavior.
Previously stored logs are not deleted by this change.

The container needs outbound GitHub HTTPS access and temporary disk space;
it requires no inbound ports, webhook receiver, database, or Docker socket.

## Optional tag synchronization

Add `--sync-tags` to the container command to copy missing tags from each selected
fork's parent. Preview it with `--sync-tags --dry-run` first.
Tags are copied directly even with `--mode pr`; pull requests cannot review tag creation.
Creating tags can trigger workflows in the fork.

Existing fork tags are never replaced or deleted. If any upstream tag points to a
different object than the existing fork tag, the entire tag push fails without
creating other tags. Git's `--atomic` option enforces this rule. A server that
does not support atomic pushes causes a failure. See the [Git push reference](https://git-scm.com/docs/git-push).

Tag synchronization follows branch synchronization. A tag failure marks the repository
and batch as failed, but does not undo a branch update or PR that already succeeded.
This option fetches tag histories and needs additional temporary disk space and network
traffic. GitHub release descriptions and assets are not copied.

## Telemetry

`forkkeeper-public` emits one `batch_started` event and one `batch_completed`
event per batch. Each event is one JSON line, flushed immediately to stdout.
The single-installation `forkkeeper` debugging command keeps its existing output.
Forkkeeper does not send telemetry to an external service or expose a metrics port.

For example, a completed batch can emit:

```json
{"service":"forkkeeper","mode":"direct","dry_run":false,"sync_tags":false,"exit_code":1,"installations_processed":2,"installation_errors":1,"repositories_processed":5,"repository_errors":1,"pull_requests_pending":1,"timestamp":"2026-11-11T12:00:00+00:00","level":"error","event":"batch_completed","duration_seconds":8.25}
```

| Field | Meaning |
|---|---|
| `timestamp` | UTC event time in RFC 3339 format |
| `service`, `event`, `level` | Service name, event type, and `info` or `error` severity |
| `mode`, `dry_run`, `sync_tags` | Synchronization mode, whether writes are disabled, and whether tags are synchronized |
| `exit_code` | Batch result: `0` for success or `1` for failure |
| `duration_seconds` | Elapsed batch time, measured with a monotonic clock |
| `installations_processed` | Installations attempted, including failed attempts |
| `installation_errors` | Failed installation token requests or repository discovery attempts |
| `repositories_processed` | Eligible repositories attempted, including failed attempts |
| `repository_errors` | Repository synchronization failures |
| `pull_requests_pending` | Repositories returning a created or existing sync PR in this batch |

The completion event contains the result, duration, and counts. A failed batch
uses `level: error`, including credential errors or a requested filter with no match.
An installation failure does not count as a repository failure.
Zero processed repositories can mean that no eligible forks exist.
Counts include dry-run checks and repositories that are already current.
Filter `dry_run="false"` to exclude previews. Processed counts are not write counts.
Alert on `pull_requests_pending > 0` to detect work that needs review, even when
`exit_code` is `0`. A failed repository can have an open PR that this count omits.

Telemetry excludes credentials, installation IDs, account and repository names,
PR URLs, and raw repository error messages. Existing application errors remain
on stderr. Invalid CLI arguments, invalid intervals, and unreadable key files
can fail before a batch starts. A killed process may emit no completion event.
Monitor missing completion events as well as failure events. Set the alert window
longer than the configured interval plus the expected batch duration.

### Collect with Grafana Alloy

An existing container log collector can forward stdout to Loki.
For the Compose service above, add these components to your Alloy configuration:

```alloy
discovery.docker "forkkeeper" {
  host = "unix:///var/run/docker.sock"
}

discovery.relabel "forkkeeper" {
  targets = discovery.docker.forkkeeper.targets

  rule {
    source_labels = ["__meta_docker_container_label_com_docker_compose_service"]
    regex         = "forkkeeper"
    action        = "keep"
  }
}

loki.source.docker "forkkeeper" {
  host       = "unix:///var/run/docker.sock"
  targets    = discovery.relabel.forkkeeper.output
  labels     = { "service_name" = "forkkeeper" }
  forward_to = [loki.write.forkkeeper.receiver]
}

loki.write "forkkeeper" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
```

Use your Loki endpoint and its required authentication settings.
Alloy needs access to the Docker daemon and persistent storage for its read positions.
Keep the Docker socket outside the Forkkeeper container.
See the [Alloy Docker collection reference](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/).

### Query in Grafana

Select your Loki data source in Grafana Explore. These queries use the
`service_name="forkkeeper"` label from the example above.
Keep numeric counts, timestamps, and durations inside the JSON body.
Use service and host labels to identify streams.

Failed batches:

```logql
{service_name="forkkeeper"} | json | __error__="" | event="batch_completed" | exit_code != 0
```

Average batch duration over the last 24 hours:

```logql
avg_over_time({service_name="forkkeeper"} | json event="event", duration_seconds="duration_seconds" | event="batch_completed" | unwrap duration_seconds | __error__="" [24h]) by (service_name)
```

Repository failures over the last 24 hours, excluding dry runs:

```logql
sum(sum_over_time({service_name="forkkeeper"} | json event="event", dry_run="dry_run", repository_errors="repository_errors" | event="batch_completed" | dry_run="false" | unwrap repository_errors | __error__="" [24h]))
```

The `__error__` filter excludes plain stderr lines and values that cannot be parsed.
See the [LogQL metric query reference](https://grafana.com/docs/loki/latest/query/metric_queries/).

## Image access and local builds

The first GHCR package may be private. Set its visibility to public for anonymous
pulls, or authenticate the Docker host with package read access. Registry
authentication is separate from the GitHub App credentials.

```bash
docker buildx build --load -t forkkeeper:local .
docker run --rm forkkeeper:local --help
```

Direct mode updates eligible default branches where permissions and branch rules
allow it. Conflicts, missing workflow permissions, inaccessible private upstreams,
and divergent sync branches still require attention. Periodic execution retries
failed batches but does not add API rate-limit backoff or per-user scheduling.
