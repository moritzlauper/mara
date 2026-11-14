#!/bin/sh
# Start MARA in the browser without installing the desktop app (macOS and Linux).
#
#   curl -LsSf https://raw.githubusercontent.com/moritzlauper/mara/main/scripts/mara.sh | sh
#
# Installs uv if needed, fetches the latest MARA from GitHub, and opens
# http://127.0.0.1:8765. Run the same line again to update and start.
set -eu

REPO="${MARA_REPO:-moritzlauper/mara}"
REF="${MARA_REF:-main}"
HOME_DIR="${MARA_HOME:-$HOME/.mara}"
if [ "$(uname -s)" = "Darwin" ]; then
  DEFAULT_WORKSPACE="$HOME/Library/Application Support/MARA"
else
  DEFAULT_WORKSPACE="${XDG_DATA_HOME:-$HOME/.local/share}/MARA"
fi
WORKSPACE="${MARA_WORKSPACE:-$DEFAULT_WORKSPACE}"

say() { printf '\033[1mMARA\033[0m %s\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python package manager) …"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

mkdir -p "$HOME_DIR" "$WORKSPACE"
say "Fetching the latest version …"
tmp="$(mktemp -d "$HOME_DIR/download.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
if curl -LsSf "https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" | tar -xz -C "$tmp" --strip-components 1; then
  rm -rf "$HOME_DIR/app"
  mv "$tmp" "$HOME_DIR/app"
elif [ -f "$HOME_DIR/app/pyproject.toml" ]; then
  say "Download failed; starting the version already installed."
else
  say "Download failed. Check your internet connection and try again."
  exit 1
fi

# The environment lives outside app/, so updates only reinstall what changed.
export UV_PROJECT_ENVIRONMENT="$HOME_DIR/venv"
cd "$WORKSPACE"
say "Starting (data: $WORKSPACE). Your browser opens MARA; stop it with Ctrl+C."
exec uv run --quiet --python 3.12 --project "$HOME_DIR/app" mixmatch ui "$@"
