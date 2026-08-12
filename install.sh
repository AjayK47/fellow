#!/bin/sh
# Toda installer for macOS and Linux. Override TODA_VERSION to pin a release.
set -eu

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) printf '%s\n' "Unsupported system. Use install.ps1 on Windows." >&2; exit 1 ;;
esac

TODA_VERSION="${TODA_VERSION:-}"
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "Installing the uv Python runtime manager…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV_BIN="${XDG_BIN_HOME:-$HOME/.local/bin}/uv"
else
  UV_BIN="$(command -v uv)"
fi

if [ -n "$TODA_VERSION" ]; then
  PACKAGE="toda-ai==$TODA_VERSION"
else
  PACKAGE="toda-ai"
fi

printf '%s\n' "Installing $PACKAGE from PyPI…"
"$UV_BIN" tool install --python 3.12 --force "$PACKAGE"
TODA_BIN="${XDG_BIN_HOME:-$HOME/.local/bin}/toda"
if [ ! -x "$TODA_BIN" ]; then
  TODA_BIN="$(command -v toda 2>/dev/null || true)"
fi
if [ -z "$TODA_BIN" ]; then
  printf '%s\n' "Toda installed, but its executable is not on PATH. Add $HOME/.local/bin to PATH." >&2
  exit 1
fi

printf '%s\n' "Creating Toda's per-user service…"
"$TODA_BIN" service install

printf '\n%s\n' "Toda installed successfully."
printf '%s\n' "Service status: toda service status"
printf '%s\n' "Local console: http://127.0.0.1:8080"
