#!/bin/sh
set -eu
umask 077
app_dir=${COURSEWATCH_APP_DIR:-/opt/coursewatch/app}
backup_dir=${COURSEWATCH_BACKUP_DIR:-/opt/coursewatch/backups}
mkdir -p "$backup_dir"
cd "$app_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_file="$backup_dir/coursewatch-$stamp.dump"
trap 'rm -f "$backup_file.tmp"' EXIT

docker compose --env-file .env.production -p coursewatch \
    -f compose.production.yaml -f compose.vm.yaml exec -T db \
    pg_dump -U coursewatch -Fc coursewatch > "$backup_file.tmp"
mv "$backup_file.tmp" "$backup_file"
printf 'Database backup saved: %s\n' "$backup_file"
