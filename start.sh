#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import certifi' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -r requirements-elliott.txt
fi
exec .venv/bin/python -m elliottbot --open "$@"
