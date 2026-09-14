#!/usr/bin/env bash
#
# veto-uninstall.sh — clean Veto uninstall.
#
# Usage:
#   bash scripts/veto-uninstall.sh              # keeps data
#   bash scripts/veto-uninstall.sh --purge-data # deletes data too (asks for
#                                               # the typed confirmation)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "${1:-}" = "--purge-data" ]; then
  echo "This will DELETE your Veto data directory."
  read -rp "Type PURGE MY DATA to confirm: " CONFIRM
  exec python3 "$REPO_DIR/initiatives/i10/installer.py" uninstall --purge-data --confirm "$CONFIRM"
else
  exec python3 "$REPO_DIR/initiatives/i10/installer.py" uninstall
fi
