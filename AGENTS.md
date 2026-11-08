# Repository Guidelines

## Project Structure & Module Organization

Forkkeeper is a Python CLI for synchronizing GitHub App-selected forks. Code lives in `src/forkkeeper/`: `sync.py` contains the GitHub API client, Git operations, and single-installation CLI; `controller.py` discovers installations and runs synchronization per installation. Offline regression tests live in `tests/`. Packaging and entry points are defined in `pyproject.toml`. GitHub Actions workflows live in `.github/workflows/`. `Dockerfile` and `container/entrypoint.sh` package the worker; `docs/container.md` covers deployment.

## Build, Test, and Development Commands

Use the interpreter specified in `.python-version` for development and CI; Renovate's `pyenv` manager tracks this file. `requires-python` in `pyproject.toml` defines the package's minimum supported version. Git must be available for synchronization and Git-history tests.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
forkkeeper-public --account example --dry-run
forkkeeper-public --mode pr --repo example/project --dry-run
```

Editable installation exposes `forkkeeper-public` and the single-installation `forkkeeper` command. Dry runs require valid credentials and contact GitHub. To build a wheel, run `python -m pip wheel --no-deps . --wheel-dir /tmp/forkkeeper-wheels`.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and uppercase constants. Use type hints for new interfaces, concise docstrings, and `SyncError` for expected synchronization failures. Keep Git invocation argument-based through `subprocess`. Pre-commit runs Ruff, complexipy, and mypy; install development dependencies with `uv sync --locked`.

## Testing Guidelines

Use standard-library `unittest`, `unittest.mock`, and temporary repositories for Git behavior. Name files `test_*.py` and methods `test_*`. Mock GitHub calls so tests run offline without credentials. Cover changed behavior, especially repository selection, installation isolation, conflicts, and dry-run write prevention. CI runs the discovery command above; no coverage threshold is configured.

## Commit & Pull Request Guidelines

Use Conventional Commit subjects such as `feat: add a feature`, `fix: correct a bug`, or `ci: update a workflow` so Release Please can classify changes. Keep commits focused. PR descriptions should explain the behavior change, link relevant issues, and report test results. Flag permission or workflow changes explicitly.

## Security & Configuration

Supply `FORKKEEPER_APP_CLIENT_ID` and `FORKKEEPER_APP_PRIVATE_KEY` through the environment; the container also supports a mounted key through `FORKKEEPER_APP_PRIVATE_KEY_FILE`. Single-installation debugging uses `GH_APP_TOKEN`. Never commit credentials or PEM files. Run one scheduler on private compute because logs can expose customer repository names. Preserve installation-scoped access and avoid force-pushing or resetting fork history.
