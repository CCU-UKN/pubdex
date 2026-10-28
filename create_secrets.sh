#!/usr/bin/env bash
# Creates the secret files of the Compose deployment (compose.yaml), once:
# the password of the PostgreSQL superuser and those of the two application
# roles. Run it before the first `docker compose up`:
#
#   ./create_secrets.sh
#   bash create_secrets.sh    # the copy the image carries, which is not executable
#   ./create_secrets.sh --check
#
# It uses the directory secrets/ next to compose.yaml, or the one
# PEOPLE_PUBS_SECRETS_DIR names in its environment -- an absolute path, or one
# relative to the directory of compose.yaml, as Compose reads it; unlike
# Compose, it reads no .env file -- and creates it with mode 700 if it does
# not exist. In it, each missing file is created with a new
# random password: 64 hexadecimal characters from /dev/urandom. A file that
# exists is never overwritten or replaced: it is kept, and checked to be
# usable. Rerunning the script is therefore safe; it adds only what is
# missing. It never prints, logs or passes a password on a command line, and
# a new file appears under its name only once it is complete. With --check it
# creates nothing, neither directory nor file, and only checks; a missing
# file is then a problem. set_passwords.sh runs that check before it gives
# the database the passwords of the files.
#
#   postgres_password       the bootstrap superuser, postgres; used only in the
#                           database container
#   app_readonly_password   app_readonly, the read service's role
#   app_writer_password     app_writer, the ingestion role
#
# The files are readable for every user (mode 444) and the directory only for
# its owner (700). Compose hands a file secret to a container as a bind mount
# of the host file, keeping its owner and mode, and the containers run as
# users of their own -- PostgreSQL's initialisation as uid 999, PubDex as uid
# 10001 -- so the role files themselves have to be readable for other users;
# the directory keeps the other users of this machine out. Compose does not
# encrypt the files: whoever can read the directory -- its owner, root, a
# backup or a synchronised copy of it -- can read the passwords.
#
# Exit status: 0 when every file exists and is usable; 1 when the directory or
# a file is not, or with --check is missing; 2 for a usage error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRET_FILES=(postgres_password app_readonly_password app_writer_password)
# The files the containers' own users read; the superuser's is read by root.
ROLE_FILES=(app_readonly_password app_writer_password)
MAX_BYTES=4096

usage() {
  cat <<'USAGE'
usage: create_secrets.sh [--check]

Creates the missing secret files of the Compose deployment in secrets/ next to
compose.yaml, or in the directory PEOPLE_PUBS_SECRETS_DIR names, and checks
the existing ones. Existing files are never overwritten, and no password is
printed. With --check it creates nothing and only checks the directory and
the three files.
USAGE
}

check_only=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)
      check_only=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "create_secrets.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

fail() {
  echo "create_secrets.sh: $*" >&2
  exit 1
}

umask 077
setting="${PEOPLE_PUBS_SECRETS_DIR:-secrets}"
case "$setting" in
  /*) dir="$setting" ;;
  *) dir="$ROOT/$setting" ;;
esac

if [ -e "$dir" ] && [ ! -d "$dir" ]; then
  fail "$dir exists but is not a directory"
fi
if [ ! -e "$dir" ] && [ "$check_only" = 1 ]; then
  fail "$dir does not exist; create it and the secret files with: create_secrets.sh"
fi
if [ ! -e "$dir" ]; then
  mkdir -p -- "$dir"
  chmod 700 -- "$dir"
  echo "created the directory $dir (mode 700)"
fi
[ -O "$dir" ] || fail "$dir belongs to another user; the secrets directory has to be your own"
dir_mode="$(ls -ld -- "$dir/." | cut -c1-10)"
if [ "${dir_mode:4:6}" != "------" ]; then
  fail "$dir is open to other users ($dir_mode); restrict it with: chmod 700 '$dir'"
fi

# What is wrong with an existing secret file, or nothing. Never its content.
problem() {  # problem <name>
  local name="$1" file="$dir/$1" size content mode
  if [ ! -f "$file" ]; then
    echo "is not a file"
    return
  fi
  if [ ! -r "$file" ]; then
    echo "cannot be read"
    return
  fi
  mode="$(ls -lL -- "$file" | cut -c1-10)"
  case " ${ROLE_FILES[*]} " in
    *" $name "*)
      if [ "${mode:7:1}" != "r" ]; then
        echo "is not readable for other users ($mode), so its container cannot read it; allow it with: chmod 444 '$file'"
        return
      fi
      ;;
  esac
  size="$(wc -c < "$file" | tr -d '[:space:]')"
  if [ "$size" -gt "$MAX_BYTES" ]; then
    echo "is larger than $MAX_BYTES bytes"
    return
  fi
  if [ "$(tr -d '\000' < "$file" | wc -c | tr -d '[:space:]')" != "$size" ]; then
    echo "holds more than one line"
    return
  fi
  content=""
  IFS= read -r -d '' content < "$file" || true
  if [[ "$content" == *$'\r\n' ]]; then
    content="${content%$'\r\n'}"
  elif [[ "$content" == *$'\n' ]]; then
    content="${content%$'\n'}"
  fi
  if [ -z "$content" ]; then
    echo "is empty"
    return
  fi
  if [[ "$content" == *[$'\r\n']* ]]; then
    echo "holds more than one line"
    return
  fi
  if command -v iconv >/dev/null 2>&1 && ! printf '%s' "$content" | iconv -f UTF-8 -t UTF-8 > /dev/null 2>&1; then
    echo "is not UTF-8 text"
    return
  fi
}

new_file=""
trap 'if [ -n "$new_file" ]; then rm -f -- "$new_file"; fi' EXIT

echo "Secret files in $dir:"
status=0
for name in "${SECRET_FILES[@]}"; do
  file="$dir/$name"
  if [ -e "$file" ] || [ -L "$file" ]; then
    reason="$(problem "$name")"
    if [ -n "$reason" ]; then
      echo "  UNUSABLE  $name: $reason" >&2
      status=1
    else
      echo "  kept      $name"
    fi
    continue
  fi
  if [ "$check_only" = 1 ]; then
    echo "  MISSING   $name" >&2
    status=1
    continue
  fi
  # Written in full under a temporary name, then linked to its own name,
  # which fails rather than replace a file that appeared meanwhile.
  new_file="$(mktemp "$dir/.$name.XXXXXX")"
  od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$new_file"
  printf '\n' >> "$new_file"
  chmod 444 -- "$new_file"
  if ln -- "$new_file" "$file" 2>/dev/null; then
    echo "  created   $name"
  elif [ -e "$file" ] || [ -L "$file" ]; then
    echo "  kept      $name (it appeared while this ran)"
  else
    fail "cannot create $file"
  fi
  rm -f -- "$new_file"
  new_file=""
done

for pair in "postgres_password app_readonly_password" "postgres_password app_writer_password" \
            "app_readonly_password app_writer_password"; do
  set -- $pair
  if [ -f "$dir/$1" ] && [ -f "$dir/$2" ] && [ -r "$dir/$1" ] && [ -r "$dir/$2" ] \
     && cmp -s -- "$dir/$1" "$dir/$2"; then
    echo "  UNUSABLE  $1 and $2 hold the same password; each role needs its own" >&2
    status=1
  fi
done

if [ "$status" -ne 0 ]; then
  if [ "$check_only" = 1 ]; then
    echo "create_secrets.sh: fix the files marked UNUSABLE, and create the missing ones with create_secrets.sh." >&2
  else
    echo "create_secrets.sh: fix the files marked UNUSABLE; existing files are never changed by this script." >&2
  fi
  exit "$status"
fi
if [ "$check_only" = 1 ]; then
  echo "The three files are usable, in a directory only you can enter."
  exit 0
fi
echo "The files are mode 444 in a directory only you can enter; keep that directory out of backups"
echo "and synchronised folders you do not trust. Next, the one command, which pulls the images Docker lacks:"
echo "  docker compose up --wait"
