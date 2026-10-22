#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
mkdir -p "${BIN_DIR}"

fetch_asset_url() {
  local repo="$1"
  local prefix="$2"

  python3 - "$repo" "$prefix" <<'PY'
import json
import sys
import urllib.request

repo = sys.argv[1]
prefix = sys.argv[2]
url = f"https://api.github.com/repos/{repo}/releases/latest"

with urllib.request.urlopen(url) as response:
    data = json.load(response)

for asset in data.get("assets", []):
    name = asset.get("name", "")
    if name.startswith(prefix) and name.endswith("_linux_amd64.tar.gz"):
        print(asset["browser_download_url"])
        break
else:
    raise SystemExit(f"no matching asset found for {repo} and {prefix}")
PY
}

install_tar_binary() {
  local url="$1"
  local binary_name="$2"
  local temp_dir

  temp_dir="$(mktemp -d)"
  trap 'rm -rf "${temp_dir}"' RETURN

  curl -fsSL "${url}" -o "${temp_dir}/package.tar.gz"
  tar -xzf "${temp_dir}/package.tar.gz" -C "${temp_dir}"

  if [[ ! -f "${temp_dir}/${binary_name}" ]]; then
    echo "Expected binary ${binary_name} not found in ${url}" >&2
    exit 1
  fi

  install -m 0755 "${temp_dir}/${binary_name}" "${BIN_DIR}/${binary_name}"
  rm -rf "${temp_dir}"
  trap - RETURN
}

ensure_path_line() {
  local target_file="$1"
  local line='export PATH="$HOME/.local/bin:$PATH"'

  touch "${target_file}"
  if ! grep -Fq "$line" "${target_file}"; then
    printf '\n%s\n' "$line" >> "${target_file}"
  fi
}

CLI_URL="$(fetch_asset_url "livekit/livekit-cli" "lk_")"
SERVER_URL="$(fetch_asset_url "livekit/livekit" "livekit_")"

install_tar_binary "${CLI_URL}" "lk"
install_tar_binary "${SERVER_URL}" "livekit-server"

cat > "${BIN_DIR}/livekit-cli" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "create-token" ]]; then
  shift
  exec lk token create "$@"
fi

exec lk "$@"
EOF
chmod 0755 "${BIN_DIR}/livekit-cli"

ensure_path_line "${HOME}/.bashrc"
ensure_path_line "${HOME}/.profile"

echo "Installed:"
echo "  $(command -v lk || echo "${BIN_DIR}/lk")"
echo "  $(command -v livekit-cli || echo "${BIN_DIR}/livekit-cli")"
echo "  $(command -v livekit-server || echo "${BIN_DIR}/livekit-server")"
echo
echo "Open a new WSL shell, or run:"
echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
