#!/usr/bin/env bash
#
# veto-install.sh — one-command Veto installer.
#
# Usage:
#   curl -fsSL <release-url>/veto-install.sh | bash
#   bash veto-install.sh [--yes] [--with-browser] [--channel stable|preview]
#
# Idempotent: re-running repairs a partial install. Never touches the
# data directory except to create it; uninstall is a separate script.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS=("$@")

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python >= 3.10 first." >&2
  exit 1
fi

exec python3 "$REPO_DIR/initiatives/i10/installer.py" install "${ARGS[@]}"
