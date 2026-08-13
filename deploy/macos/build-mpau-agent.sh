#!/usr/bin/env bash
set -euo pipefail

# Build an Apple Silicon macOS desktop app and a drag-to-Applications DMG.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/../.." && pwd)"
python_bin="${PYTHON:-python3}"
build_venv="$project_root/.venv-agent-build-macos"
build_python="$build_venv/bin/python"
spec_file="$project_root/deploy/macos/mpau-agent.spec"
output_dir="$project_root/deploy/macos/output"
dmg_path="$output_dir/MPAU-Agent-macOS-arm64.dmg"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "This build produces Apple Silicon packages and must run on an arm64 Mac." >&2
  exit 1
fi

cd "$project_root"
if [[ ! -x "$build_python" ]]; then
  "$python_bin" -m venv "$build_venv"
fi
"$build_python" -m pip install --upgrade pip --quiet
"$build_python" -m pip install -e '.[desktop,build]' --quiet
"$build_python" -c 'import local_agent.desktop; import webapp.ai_copy.contracts; import webapp.ai_copy.product_lookup.tmall_reader'
"$build_python" -m PyInstaller --clean --noconfirm "$spec_file"

app_path="$project_root/dist/MPAU Agent.app"
if [[ ! -d "$app_path" ]]; then
  echo "PyInstaller did not create $app_path" >&2
  exit 1
fi

mkdir -p "$output_dir"
stage_dir="$(mktemp -d)"
trap 'rm -rf "$stage_dir"' EXIT
ditto "$app_path" "$stage_dir/MPAU Agent.app"
ln -s /Applications "$stage_dir/Applications"
rm -f "$dmg_path" "$dmg_path.sha256"
hdiutil create -volname 'MPAU Agent' -srcfolder "$stage_dir" -ov -format UDZO "$dmg_path"
checksum="$(shasum -a 256 "$dmg_path" | awk '{print $1}')"
printf '%s *MPAU-Agent-macOS-arm64.dmg\n' "$checksum" > "$dmg_path.sha256"
printf 'Installer created: %s\nSHA-256: %s\n' "$dmg_path" "$checksum"
