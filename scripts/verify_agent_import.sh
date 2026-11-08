#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source venv/bin/activate

python3 -m py_compile agent.py
python3 -c "import agent; print('IMPORT_OK')"
