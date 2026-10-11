#!/bin/sh
# Launch HueCanvas from any working directory using its local virtual environment.
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

if [ ! -d .venv ]; then
    printf '%s\n' 'Creating the project virtual environment...'
    python3 -m venv .venv
fi

if [ ! -x .venv/bin/python ]; then
    printf '%s\n' 'The .venv Python executable is missing. Recreate .venv with python3 -m venv .venv.' >&2
    exit 1
fi

if ! .venv/bin/python -c 'import uvicorn; from importlib.metadata import version; version("huecanvas")' >/dev/null 2>&1; then
    printf '%s\n' 'Installing HueCanvas dependencies in .venv...'
    .venv/bin/python -m pip install -e '.[dev]'
fi

printf '%s\n' "Starting HueCanvas at http://127.0.0.1:${PORT:-8000}" 'Press Ctrl+C to stop.'
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}" "$@"
