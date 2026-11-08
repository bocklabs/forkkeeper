# Bocklabs Forkkeeper

A small GitHub App automation that keeps selected organization forks synchronized with their upstream repositories. No web server, PAT, database, or per-repository workflow is required.

## Behavior

1. An organization admin installs the **Bocklabs Forkkeeper** GitHub App with **Only select repositories**. These are the forks to synchronize.
2. A scheduled workflow in this controller repository issues a short-lived **GitHub App installation token** scoped to the selected repositories.
3. The workflow enumerates those repositories using GitHub's installation API. Only GitHub forks are processed; standalone and archived repositories are ignored.
4. **Default `direct` mode:** GitHub's `merge-upstream` API merges/fast-forwards each fork's default branch from the parent, preserving independent fork commits. If GitHub rejects the sync due to a conflict or other non-syncable state (HTTP 409 or 422), Forkkeeper creates/updates a reviewable PR instead. It **never hard-resets** the fork.
5. **`pr` mode:** always creates/updates a PR from the upstream default branch into the fork default branch. This mode requires a person or separate auto-merge policy to merge the PR; PR creation alone does not update the default branch.

The workflow runs **every six hours** (17 minutes after 00:00, 06:00, 12:00 and 18:00 UTC) and also supports manual triggering, filtering to one repository, and dry-run. Schedules are best-effort and may be delayed by GitHub; schedule workflows in inactive public repositories can be disabled after 60 days.

## Installation (GitHub.com)

### 1. Create the controller repository

Create `bocklabs/forkkeeper` (private recommended) and add the contents of this project. The workflow needs to be on the default branch. Enable GitHub Actions for this repository.

### 2. Register an organization-owned GitHub App

Open [New GitHub App in bocklabs](https://github.com/organizations/bocklabs/settings/apps/new), and configure:

| Setting | Value |
|---|---|
| Name | `bocklabs-forkkeeper` (or another available unique name) |
| Homepage | `https://github.com/bocklabs/forkkeeper` |
| Owner | `bocklabs` organization |
| Where can this GitHub App be installed? | Only on this account |
| Repository permissions → Contents | **Read and write** |
| Repository permissions → Pull requests | **Read and write** |
| Webhooks → Active | **Off** (polling-based; no HTTP endpoint) |
| Other permissions/events | None beyond automatic Metadata (read) |

Click **Create GitHub App**. Under the app settings, copy its **Client ID** and generate a **private key** (downloaded PEM file).

### 3. Install the App and select forks

From the app settings, choose **Install App**, then install it for `bocklabs` with **Only select repositories**. Select the forks you want synchronized. You do *not* need to include the controller repository among the selected repositories; the workflow explicitly requests an organization installation token.

To start managing another fork later, open **Organization settings → GitHub Apps → Installed GitHub Apps → Configure** for the Forkkeeper app and add the fork to the installation. Removing a fork stops its future synchronization.

**Private upstream repositories:** the app token must also have read access to the upstream parent, so include that parent among selected repositories when it is accessible under the same installation. Cross-organization private parents require an additional authentication design and are not supported by this MVP.

### 4. Set credentials in `bocklabs/forkkeeper`

At **Settings → Secrets and variables → Actions**:

- Repository **variable**: `FORKKEEPER_APP_CLIENT_ID` = App Client ID (e.g. `Iv1...`).
- Repository **secret**: `FORKKEEPER_APP_PRIVATE_KEY` = **entire generated PEM private key**, including BEGIN/END lines.

Never commit the private key. GitHub Actions uses `actions/create-github-app-token@v3` to mint short-lived credentials. The key remains stored as a GitHub Actions secret.

### 5. Test and activate

From **Actions → Sync selected forks → Run workflow**, choose:

- `dry_run: true`, `mode: direct`, and optionally a single `owner/repo` to verify that the app sees the expected installation repositories.
- Review the workflow summary and logs.
- Run again with `dry_run: false` to perform the sync. On subsequent scheduled runs, `direct` mode is used.

The workflow is scheduled automatically once it is present on the default branch. Test execution is performed separately by `.github/workflows/tests.yml`.

## Example

If `bocklabs/some-project` is a fork of `some-upstream/some-project`, and that fork is selected in the installation:

- If upstream has commits absent from the fork, GitHub's native sync is attempted against the fork default branch.
- If GitHub can merge them, the fork default branch is updated.
- If GitHub rejects the update because of a conflict or protection policy, a branch `forkkeeper/<upstream-default-branch>` is pushed to the fork and a PR to the fork default branch is opened or updated.
- If the fork is already current, the next direct API call is effectively a no-op.

Only the parent's default branch and the fork's default branch are handled in v1; different branch names are supported.

## Safeguards and limitations

- **No hard reset, force-push, or automatic merge of PRs.** A conflicting PR may need manual conflict resolution.
- Only actual GitHub forks are synchronized (`fork: true`); separately cloned repositories and mirrors are not supported.
- Direct sync with protected branches can fail. The PR fallback is designed for this case, but restrictions on branch creation/app credentials can still prevent the fallback.
- Existing `forkkeeper/*` synchronization branches must be bot-managed. They are **never force-pushed**. If such a branch has diverged, the update fails until an administrator resolves it.
- Forkkeeper doesn't automatically delete PR branches or reopen a manually closed PR. If you close an update PR without merging and leave the upstream commits absent, a future run may open a replacement PR.
- Public upstreams work without installing the app upstream; private upstreams require permissions as described above.
- For very large repositories, fetching Git history for the PR fallback may consume time; the default direct path uses only GitHub's API.
- GitHub-hosted runners have workflow time limits; if many forks need synchronization, use a smaller schedule interval or split by repository.
- The app's contents-write permission is powerful across selected repositories; select only forks you trust it to update and protect the controller's Actions secrets.

## Local checks

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

To run the controller locally, supply a GitHub App installation token:

```sh
GH_APP_TOKEN=... python3 src/forkkeeper/sync.py --repo bocklabs/some-fork --dry-run
```

## References

- [GitHub REST API: Sync a fork branch](https://docs.github.com/en/rest/branches/branches#sync-a-fork-branch-with-the-upstream-repository)
- [GitHub REST API: List repositories accessible to the app installation](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation)
- [GitHub App registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
- [GitHub App token action](https://github.com/actions/create-github-app-token)
- [wei/pull (inspiration)](https://github.com/wei/pull)

License: MIT.
