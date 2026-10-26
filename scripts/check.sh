#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$repository_root/app"
uv sync --locked
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy src tests

cd "$repository_root"
uv run --project app --no-sync ruff check --config app/pyproject.toml scripts
uv run --project app --no-sync ruff format --check --config app/pyproject.toml scripts
uv run --project app --no-sync mypy --config-file app/pyproject.toml --explicit-package-bases scripts
uv run --project app --no-sync python -m pytest -c app/pyproject.toml app/tests scripts/tests

cd "$repository_root/app"
uv build --no-build-isolation --wheel
