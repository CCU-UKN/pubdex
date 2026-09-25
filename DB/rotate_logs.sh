#!/usr/bin/env bash
# Monthly rotation for the scheduled-job logs in DB/ (review §6.4).
#
# Archives every top-level *.log / *.err.log into a dated tar.gz under
# backups/logs/, then truncates the live files in place (same inodes, so
# already-running cron appends continue seamlessly). Keeps the last 12
# monthly archives. Scheduled from DB/cron on the 1st of each month.
set -euo pipefail

cd "$(dirname "$0")"

archive_dir="backups/logs"
mkdir -p "${archive_dir}"

shopt -s nullglob
logs=( *.log )
if [[ ${#logs[@]} -eq 0 ]]; then
  echo "rotate_logs: no log files to rotate."
  exit 0
fi

ts="$(date -u +%Y%m%d)"
archive="${archive_dir}/db_logs_${ts}.tar.gz"
tmp="${archive}.tmp"
trap 'rm -f "${tmp}"' EXIT

tar -czf "${tmp}" -- "${logs[@]}"
mv "${tmp}" "${archive}"

for f in "${logs[@]}"; do
  : > "${f}"
done

# Retention: keep the 12 newest monthly archives.
ls -1t "${archive_dir}"/db_logs_*.tar.gz 2>/dev/null | tail -n +13 | xargs -r rm --

echo "rotate_logs: archived ${#logs[@]} log files to ${archive} ($(du -h "${archive}" | cut -f1))"
