# Forkkeeper

Forkkeeper keeps selected GitHub forks in sync with their upstream repositories across GitHub users and organizations. Each installation uses its own GitHub App token, scoped to the repositories selected by the installer.

## Features

- Installers choose **Only select repositories** (or **All repositories**) during GitHub App installation. Only actual non-archived forks are synchronized.
- Scheduled **direct** mode uses GitHub's `merge-upstream` endpoint. It does **not** hard-reset or force-push fork history. If that fails with a sync conflict, it attempts a reviewable pull request.
- **PR-only** mode never updates the default branch without review.
- Filter runs by installation account or repository, and preview changes with dry-run mode.
- No user PATs, per-repository GitHub Actions, database, or hosted webhook endpoint required.

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
| Webhook | **Inactive** (scheduled polling) |

Create a private key in App settings, and copy the **Client ID**.

### 2. Configure the private controller

Create a **separate private** repository such as `bocklabs/forkkeeper-controller`. Copy [the controller workflow](.github/workflows/controller-workflow.yaml) to `.github/workflows/synchronize.yaml` in that repository.

Configure repository Actions:

- Variable: `FORKKEEPER_APP_CLIENT_ID` = App client ID.
- Secret: `FORKKEEPER_APP_PRIVATE_KEY` = entire private key PEM.

The workflow checks out `bocklabs/forkkeeper`, reads its `.python-version`, and runs `forkkeeper-public` with a separate token for each installation.

Keep the controller private: logs and job summaries can contain private repository names. Store the App key only in Actions secrets and restrict access to workflow edits and dispatches.

When testing, use **Run workflow → dry_run=true** before allowing writes. The workflow runs approximately every six hours, subject to GitHub Actions scheduling delays and runtime limits.

### 3. Install the App

```text
https://github.com/apps/<app-slug>/installations/new
```

Open the URL above and select the forks to synchronize. The next scheduled run discovers the installation automatically. Organization policies may require administrator approval.

## Local development

Use the Python version in `.python-version` and have Git installed. With pyenv shell integration enabled:

```bash
pyenv install --skip-existing "$(cat .python-version)"
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests run offline without credentials. To preview synchronization, supply App credentials through the environment:

```bash
export FORKKEEPER_APP_CLIENT_ID='Iv1...'
export FORKKEEPER_APP_PRIVATE_KEY="$(cat path/to/private-key.pem)"
forkkeeper-public --account bocklabs --dry-run
forkkeeper-public --mode pr --repo bocklabs/copacetic --dry-run
```

Dry runs contact GitHub but do not push branches or create pull requests. Never commit credentials or private keys.

## Limitations

- All installations share one workflow job with a 60-minute timeout.
- Divergent `forkkeeper/*` synchronization branches require manual resolution; Forkkeeper never force-pushes them.

## License

MIT
