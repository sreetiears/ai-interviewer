#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

livekit-cli create-token --help >/dev/null
echo "LIVEKIT_CLI_COMPAT_OK"
