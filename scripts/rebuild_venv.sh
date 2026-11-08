#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -d venv ]]; then
  if [[ -s venv/bin/python3 ]]; then
    echo "Existing venv already has a non-empty python3 executable."
    echo "Stop here if you do not want to replace it."
    exit 1
  fi

  backup_dir="venv.broken.$(date +%Y%m%d-%H%M%S)"
  mv venv "$backup_dir"
  echo "Moved broken venv to $backup_dir"
fi

python3 -m venv venv
source venv/bin/activate

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

python3 -m py_compile agent.py
python3 -c "import agent; print('IMPORT_OK')"

echo "WSL virtualenv rebuilt successfully."
