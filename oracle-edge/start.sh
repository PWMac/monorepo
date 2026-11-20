#!/usr/bin/env bash
# Oracle Edge for macOS/Linux: needs uv (https://docs.astral.sh/uv/) on PATH.
cd "$(dirname "$0")"
export UV_PROJECT_ENVIRONMENT="$PWD/runtime/venv" UV_PYTHON_INSTALL_DIR="$PWD/runtime/python"
uv sync --frozen --no-dev --quiet && exec "$PWD/runtime/venv/bin/python" -m oracle_edge
