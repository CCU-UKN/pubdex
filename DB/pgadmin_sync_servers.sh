#!/usr/bin/env bash
set -euo pipefail

# Sync the same pgAdmin server template to every active pgAdmin user.
# This keeps users as role=User while still giving them a preconfigured server.

CONTAINER="${PGADMIN_CONTAINER:-peopledb-pgadmin}"
SERVERS_FILE="${PGADMIN_SERVERS_FILE:-/pgadmin4/servers.json}"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Container '$CONTAINER' is not running."
  exit 1
fi

users_json="$(docker exec "$CONTAINER" /venv/bin/python /pgadmin4/setup.py get-users --json)"

emails="$(
  printf '%s' "$users_json" | python3 -c '
import json, sys
users = json.load(sys.stdin)
for u in users:
    if u.get("active") and u.get("email"):
        print(u["email"])
'
)"

if [ -z "$emails" ]; then
  echo "No active pgAdmin users found."
  exit 0
fi

while IFS= read -r email; do
  [ -z "$email" ] && continue
  echo "Syncing server template for: $email"
  docker exec "$CONTAINER" /venv/bin/python /pgadmin4/setup.py \
    load-servers "$SERVERS_FILE" \
    --user "$email" \
    --replace > /dev/null
done <<< "$emails"

echo "pgAdmin server sync complete."
