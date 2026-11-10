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
Repository activity, repository-fetch errors, and synchronization results are
not logged. GitHub job summaries are disabled. A single batch exits with `0` for success,
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

Check the exit code. Dry runs produce no repository results. When ready to
synchronize, remove `--dry-run` from `command` and run `docker compose up -d`.
Set `FORKKEEPER_INTERVAL_SECONDS` in `.env` to change the delay, then recreate
the service with `docker compose up -d`.

For `docker run`, pass the delay with `-e FORKKEEPER_INTERVAL_SECONDS=3600`.
Existing deployments must recreate their containers to apply the new logging behavior.
Previously stored logs are not deleted by this change.

The container needs outbound GitHub HTTPS access and temporary disk space;
it requires no inbound ports, webhook receiver, database, or Docker socket.

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
