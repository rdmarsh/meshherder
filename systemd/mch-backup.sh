#!/bin/sh
# Mesh Herder desired-state backup.
#
# Writes a dated `mch config export` snapshot and keeps the newest N. Because the DB is now the
# only live copy of desired state (the config.yaml -> DB sunset), an automated export is the
# disaster-recovery mechanism: the export is a complete, restorable backup (full pubkeys +
# inventory + desired settings + operations, retired nodes tagged). Restore with
# `mch config import backup-YYYY-MM-DD.yaml --yes`.
#
# Overridable via the environment (see mch-backup.service):
#   MCH_BACKUP_DIR   where snapshots are written (default /var/lib/meshherder/backups)
#   MCH_BACKUP_KEEP  how many dated snapshots to retain (default 14)
set -eu

BACKUP_DIR="${MCH_BACKUP_DIR:-/var/lib/meshherder/backups}"
KEEP="${MCH_BACKUP_KEEP:-14}"

mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/backup-$(date -u +%Y-%m-%d).yaml"

# Export to a temp file first so a failed/partial export can never overwrite a good snapshot.
tmp="$(mktemp "$BACKUP_DIR/.backup-XXXXXX")"
if mch config export > "$tmp"; then
    mv "$tmp" "$out"
    echo "wrote $out"
else
    rc=$?
    rm -f "$tmp"
    echo "mch config export failed (exit $rc); no snapshot written" >&2
    exit "$rc"
fi

# Rotation: keep the newest $KEEP dated snapshots, delete older ones. Sorting by name works
# because the YYYY-MM-DD stamp sorts chronologically.
count=0
for f in $(ls -1 "$BACKUP_DIR"/backup-*.yaml 2>/dev/null | sort -r); do
    count=$((count + 1))
    if [ "$count" -gt "$KEEP" ]; then
        rm -f "$f"
        echo "pruned $f"
    fi
done
