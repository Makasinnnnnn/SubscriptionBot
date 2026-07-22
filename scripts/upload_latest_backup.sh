#!/usr/bin/env sh
set -eu

ROOT=${PROPGATE_ROOT:-/opt/propgate}
TARGET=${BACKUP_OFFSITE_TARGET:-}

if [ -z "$TARGET" ]; then
  echo "BACKUP_OFFSITE_TARGET is empty; verified backup remains on this VPS."
  exit 0
fi

LATEST=$(find "$ROOT/backups" -maxdepth 1 -type f -name 'subscription_*.dump' -printf '%T@ %p\n' |
  sort -rn | head -n 1 | cut -d' ' -f2-)
[ -n "$LATEST" ] || { echo "No backup found for offsite upload." >&2; exit 1; }

scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=yes "$LATEST" "$TARGET"
echo "Latest backup uploaded to the configured offsite target."
