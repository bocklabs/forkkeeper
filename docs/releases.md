# Releases

Release Please runs on pushes to `main` and can be dispatched manually on `main`.
It uses Conventional Commits to open or update a release pull request containing
the changelog, Python package version, lockfile package version, and release manifest.
Merge that pull request to create the GitHub release and version tag. The image
workflow then builds, tests, and publishes the corresponding GHCR image.

Tags are unprefixed, such as `0.3.0`, matching the existing `0.2.0` tag. The image
workflow accepts both numeric and `v`-prefixed tags. The release manifest starts at
the current package version, `0.2.0`.

## Bot credentials

Install the release automation GitHub App on this repository with permission to
update contents, manage release pull requests, and apply labels. Configure
repository or shared organization Actions secrets:

- `RELEASE_CLIENT_ID`: the release App client ID.
- `RELEASE_APP_KEY`: the release App PEM private key.

The workflow mints a short-lived, repository-scoped installation token using
`actions/create-github-app-token`. Using that token lets release PRs and version
tags trigger their CI workflows. These credentials are separate from the
Forkkeeper App credentials used to synchronize users' forks.

GHCR publishing uses the workflow's `GITHUB_TOKEN` with `packages: write`,
rather than the token minted for the release App. No personal access token is
needed for either workflow.
