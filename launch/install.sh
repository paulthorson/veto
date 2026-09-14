#!/usr/bin/env bash
#
# install.sh — Veto one-command installer entry point (launch/).
#
# Usage: bash launch/install.sh [--yes] [--with-browser]
set -euo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/../scripts/veto-install.sh" "$@"
