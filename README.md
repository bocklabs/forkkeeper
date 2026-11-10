# Forkkeeper

Forkkeeper keeps selected GitHub forks in sync with their upstream repositories across GitHub users and organizations. Each installation uses its own GitHub App token, scoped to the repositories selected by the installer.

## Features

- Installers choose **Only select repositories** (or **All repositories**) during GitHub App installation. Only actual non-archived forks are synchronized.
- Scheduled **direct** mode uses GitHub's `merge-upstream` endpoint. It does **not** hard-reset or force-push fork history. If that fails with a sync conflict, it attempts a reviewable pull request.
- **PR-only** mode never updates the default branch without review.
- Filter runs by installation account or repository, and preview changes with dry-run mode.
- No user PATs, per-repository GitHub Actions, database, or hosted webhook endpoint required.
- Application errors only. No repository activity logs, repository error logs, or GitHub job summaries.

## Setup

### 1. Register the GitHub App

Create an App in your GitHub account or organization settings.

| Property | Value |
|---|---|
| Name | An available app name/slug, e.g. `bocklabs-forkkeeper` |
| Homepage | Public README / project URL |
| Where can it be installed? | **Any account** (public) |
| Repository permission: Contents | **Read and write** |
| Repository permission: Pull requests | **Read and write** |
| Repository permission: Workflows | **Read and write**, to synchronize workflow-file changes |
| Webhook | **Inactive** (scheduled polling) |

Create a private key in App settings, and copy the **Client ID**.

Existing installations must approve new permissions before workflow-file updates
can be synchronized. Installation tokens request Contents and Pull requests access,
plus Workflows when approved, and refresh automatically before expiry.

### 2. Deploy the controller

For a container on your own compute, follow the [container deployment guide](docs/container.md).
It covers the published image, periodic runs, and credentials.

### 3. Install the App

```text
https://github.com/apps/<app-slug>/installations/new
```

Open the URL above and select the forks to synchronize. The next scheduled run discovers the installation automatically. Organization policies may require administrator approval.

## Local development

Use a current uv release and have Git installed. uv installs the Python version in `.python-version`:

```bash
uv python install
uv sync --locked
source .venv/bin/activate
pre-commit install
python -m unittest discover -s tests -v
```

Tests run offline without credentials.
To preview synchronization, supply App credentials through the environment:

```bash
export FORKKEEPER_APP_CLIENT_ID='Iv1...'
export FORKKEEPER_APP_PRIVATE_KEY="$(cat path/to/private-key.pem)"
forkkeeper-public --account bocklabs --dry-run
forkkeeper-public --mode pr --repo bocklabs/copacetic --dry-run
```

Dry runs contact GitHub but do not push branches or create pull requests. Runs produce no repository output. Application errors go to stderr. Exit code `0` means success, and `1` means failure.

## Limitations

- Divergent `forkkeeper/*` synchronization branches require manual resolution; Forkkeeper never force-pushes them.

## Releases

[Release Please](docs/releases.md) prepares version and changelog updates.
Merging a release PR creates a version tag and publishes the container image.

## License

MIT
