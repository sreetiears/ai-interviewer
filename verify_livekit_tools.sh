#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

command -v lk
command -v livekit-cli
command -v livekit-server

lk --version
livekit-server --version
