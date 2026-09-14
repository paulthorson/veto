#!/usr/bin/env bash
#
# veto-backup.sh — encrypted backup / verify / restore / drill.
#
# Usage:
#   bash scripts/veto-backup.sh create  <data-dir> <out.vetobackup>
#   bash scripts/veto-backup.sh verify  <backup.vetobackup>
#   bash scripts/veto-backup.sh restore <backup.vetobackup> <data-dir> [schema,...]
#   bash scripts/veto-backup.sh drill   <data-dir>
#
# The passphrase is read from $VETO_BACKUP_PASSPHRASE or prompted
# interactively (never from argv, so it stays out of shell history and
# process lists).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMD="${1:?usage: veto-backup.sh create|verify|restore|drill ...}"

get_passphrase() {
  if [ -n "${VETO_BACKUP_PASSPHRASE:-}" ]; then
    printf '%s' "$VETO_BACKUP_PASSPHRASE"
  else
    read -rsp "Backup passphrase: " PASS; echo
    printf '%s' "$PASS"
  fi
}

PASS="$(get_passphrase)"

case "$CMD" in
  create)
    DATA_DIR="${2:?data dir required}"; OUT="${3:?output path required}"
    python3 -m initiatives.i10.backup create --data-dir "$DATA_DIR" --out "$OUT" --passphrase "$PASS"
    ;;
  verify)
    IN="${2:?backup file required}"
    python3 -m initiatives.i10.backup verify --in "$IN" --passphrase "$PASS"
    ;;
  restore)
    IN="${2:?backup file required}"; DATA_DIR="${3:?data dir required}"
    SELECTIVE="${4:-}"
    if [ -n "$SELECTIVE" ]; then
      python3 -m initiatives.i10.backup restore --in "$IN" --passphrase "$PASS" --data-dir "$DATA_DIR" --selective "$SELECTIVE"
    else
      python3 -m initiatives.i10.backup restore --in "$IN" --passphrase "$PASS" --data-dir "$DATA_DIR"
    fi
    ;;
  drill)
    DATA_DIR="${2:?data dir required}"
    python3 -m initiatives.i10.backup drill --data-dir "$DATA_DIR" --passphrase "$PASS"
    ;;
  *)
    echo "unknown command: $CMD" >&2; exit 1 ;;
esac
