#!/usr/bin/env bash
# Clean-clone acceptance demonstration.
#
# Makes no ORCID, Crossref or other metadata-provider request: the input and
# the expected publication data come entirely from repository fixtures. Docker
# still needs the PostgreSQL image, and the interpreter needs the installed
# dependencies, so this is reproducible on supported Docker environments once
# those are available -- not on an arbitrary machine with no cache.
#
# Shows, from a fresh clone, that PubDex can:
#   1. bootstrap the schema from DB/init/ into an empty PostgreSQL database;
#   2. import the bundled synthetic roster;
#   3. ingest the bundled ORCID payload through the production ingestion path;
#   4. enrich the same publication with the bundled Crossref payload;
#   5. query the stored record and its cumulative provenance;
#   6. query the canonical list after institutional attribution is configured;
#   7. produce a nonempty export and verify its contents.
#
# The bundled roster marks Ada Example internal and Bert Example external, so
# both are linked as authors of the work but only Ada is reported as one of the
# organisation's own authors.
#
# Runs against a disposable PostgreSQL container with its own volume. Both
# carry a unique per-run name and a run label; the database is published only
# on a dynamically assigned loopback port; and the cleanup registered before
# anything is created removes only those resources, even after a failed start
# or an interrupted run. Existing containers and volumes are never touched: a
# name collision aborts instead of taking the resource over. Docker Compose is
# never used, so no long-lived instance of this project can be reached.
#
#   ./run_task_1a_demo.sh                    # full run (needs Docker)
#   TASK_1A_PG_IMAGE=postgres:16 ./run_task_1a_demo.sh
set -euo pipefail

DB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DB_DIR/.." && pwd)"

PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "ERROR: no interpreter at .venv/bin/python" >&2
  echo "Create it from the repository root with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r DB/requirements-dev.txt" >&2
  exit 1
fi

IMAGE="${TASK_1A_PG_IMAGE:-postgres:16}"
WAIT_SECONDS="${TASK_1A_WAIT_SECONDS:-120}"
RUN_ID="$(date +%Y%m%d%H%M%S)-$$-$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
CONTAINER="pubdex-task-1a-demo-${RUN_ID}"
VOLUME="pubdex-task-1a-demo-${RUN_ID}"
LABEL_KEY="pubdex.task-1a-demo.run"

# Throwaway, test-only password for a container that lives for the length of
# this run and is published on loopback only. It appears in no tracked
# configuration and is discarded with the container and its volume.
PGPASS="demo-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
WORKDIR="$(mktemp -d)"
chmod 700 "$WORKDIR"

# Registered before anything is created: removes only the container and volume
# carrying this run's label, so a resource that merely shares a name is left
# alone. The long-lived project containers carry no such label and are
# therefore unreachable from here.
cleanup() {
  local rc=$?
  trap - EXIT
  if docker container inspect "$CONTAINER" >/dev/null 2>&1 \
     && [ "$(docker container inspect -f "{{ index .Config.Labels \"$LABEL_KEY\" }}" "$CONTAINER" 2>/dev/null)" = "$RUN_ID" ]; then
    docker rm -f -v "$CONTAINER" >/dev/null 2>&1 || true
  fi
  if docker volume inspect "$VOLUME" >/dev/null 2>&1 \
     && [ "$(docker volume inspect -f "{{ index .Labels \"$LABEL_KEY\" }}" "$VOLUME" 2>/dev/null)" = "$RUN_ID" ]; then
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  fi
  [ -d "$WORKDIR" ] && rm -rf "$WORKDIR"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Never take over a resource that already exists, however unlikely a collision.
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "ERROR: a container named $CONTAINER already exists; not touching it." >&2
  exit 1
fi
if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  echo "ERROR: a volume named $VOLUME already exists; not touching it." >&2
  exit 1
fi

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

psql_demo() {
  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U postgres -d people_db "$@"
}

echo "[1/8] Starting disposable PostgreSQL ($IMAGE) with DB/init/ mounted (run $RUN_ID)..."
docker volume create --label "$LABEL_KEY=$RUN_ID" "$VOLUME" >/dev/null
docker run -d --name "$CONTAINER" --label "$LABEL_KEY=$RUN_ID" \
  -e POSTGRES_PASSWORD="$PGPASS" -e POSTGRES_DB=people_db \
  -p 127.0.0.1::5432 \
  -v "$VOLUME:/var/lib/postgresql/data" \
  -v "$DB_DIR/init:/docker-entrypoint-initdb.d:ro" \
  "$IMAGE" >/dev/null

# The entrypoint runs the init scripts against a socket-only temporary server
# and only then starts the real one. Require its completion message, evidence
# that every init/*.sql was run, and TCP readiness; stop waiting as soon as the
# container exits (a failed init script stops it).
echo "[2/8] Waiting for the schema to bootstrap, up to ${WAIT_SECONDS}s..."
bootstrapped() {
  local logs
  logs="$(docker logs "$CONTAINER" 2>&1)" || return 1
  grep -q 'PostgreSQL init process complete' <<<"$logs" || return 1
  local script
  for script in "$DB_DIR"/init/*.sql; do
    grep -q "running /docker-entrypoint-initdb.d/$(basename "$script")" <<<"$logs" || return 1
  done
  docker exec "$CONTAINER" pg_isready -h 127.0.0.1 -U postgres -d people_db >/dev/null 2>&1
}
ready=0
for _ in $(seq "$WAIT_SECONDS"); do
  if [ "$(docker container inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
    echo "ERROR: the database container stopped during bootstrap; container log tail:" >&2
    docker logs --tail 40 "$CONTAINER" >&2 || true
    exit 1
  fi
  if bootstrapped; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || fail "database not bootstrapped after ${WAIT_SECONDS}s"

PORT="$(docker port "$CONTAINER" 5432/tcp | head -1 | sed 's/.*://')"
[ -n "$PORT" ] || fail "could not determine the mapped loopback port"
DSN="postgresql://postgres:${PGPASS}@127.0.0.1:${PORT}/people_db"

# Every Python step runs with a cleared environment and an explicit DSN, so no
# PG*/POSTGRES_* variable and no DB/.env value can redirect a write towards a
# database that is not the disposable one created above.
demo_python() {
  env -i \
    HOME="$HOME" \
    PATH="$PATH" \
    LANG="${LANG:-C.UTF-8}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$DB_DIR" \
    PEOPLE_DB_DSN="$DSN" \
    PEOPLE_PUBS_CROSSREF_MAILTO="you@example.org" \
    "$PYTHON" "$@"
}

echo "[3/8] Importing the bundled synthetic roster..."
demo_python -m people_pubs.sync.add_people \
  --dsn "$DSN" \
  --csv "$DB_DIR/tests/fixtures/golden/demo_roster_minimal.csv" \
  --no-orcid-lookup

echo "[4/8] Ingesting the bundled ORCID payload, then enriching it with Crossref..."
demo_python -m people_pubs.sync.fixture_demo --dsn "$DSN"

echo "[5/8] Configuring fictional institutional attribution..."
demo_python -m people_pubs.sync.attribution_rules set \
  --dsn "$DSN" \
  --affiliation "Synthetic Institute for Collective Behaviour"

echo "[6/8] Querying the stored publication and its cumulative provenance..."
psql_demo -c "
  SELECT p.pub_id, p.doi, p.title, p.year, p.venue, p.source_of_truth,
         p.institution_attributed
  FROM biblio.publications p
  ORDER BY p.pub_id;"
psql_demo -c "
  SELECT a.author_position, a.author_name,
         COALESCE(a.paper_orcid, a.author_orcid) AS orcid,
         a.is_internal_at_ingest
  FROM biblio.authorships_curated a
  JOIN biblio.publications p ON p.pub_id = a.pub_id
  WHERE p.doi = '10.5555/pubdex-fixture-001'
  ORDER BY a.author_position;"
psql_demo <<'SQL'
DO $$
DECLARE
  internal_names TEXT;
  external_rows INT;
BEGIN
  -- Both authors stay linked and visible, but only the internal one counts as
  -- one of the organisation's authors.
  SELECT string_agg(a.author_name, ', ' ORDER BY a.author_position)
    INTO internal_names
  FROM biblio.authorships_curated a
  JOIN biblio.publications p ON p.pub_id = a.pub_id
  WHERE p.doi = '10.5555/pubdex-fixture-001'
    AND a.is_internal_at_ingest IS TRUE;
  IF internal_names IS DISTINCT FROM 'Ada Example' THEN
    RAISE EXCEPTION
      'acceptance failed: expected only Ada Example to be internal, got %',
      COALESCE(internal_names, '(none)');
  END IF;

  SELECT count(*)
    INTO external_rows
  FROM biblio.authorships_curated a
  JOIN biblio.publications p ON p.pub_id = a.pub_id
  WHERE p.doi = '10.5555/pubdex-fixture-001'
    AND a.author_name = 'Bert Example'
    AND a.person_id IS NOT NULL
    AND a.is_internal_at_ingest IS FALSE;
  IF external_rows <> 1 THEN
    RAISE EXCEPTION
      'acceptance failed: the external co-author must stay linked and stay external (rows = %)',
      external_rows;
  END IF;
END $$;
SQL

psql_demo <<'SQL'
DO $$
DECLARE
  sot TEXT;
BEGIN
  SELECT source_of_truth INTO sot
  FROM biblio.publications
  WHERE doi = '10.5555/pubdex-fixture-001';
  IF sot IS NULL THEN
    RAISE EXCEPTION 'acceptance failed: the fixture publication was not stored';
  END IF;
  IF position('orcid' IN sot) = 0 OR position('crossref' IN sot) = 0 THEN
    RAISE EXCEPTION
      'acceptance failed: provenance is not cumulative (source_of_truth = %)', sot;
  END IF;
END $$;
SQL

echo "[7/8] Querying the canonical list..."
psql_demo -c "
  SELECT pub_id, doi, title, year, venue, source_of_truth, internal_authors
  FROM biblio.publications_canon
  ORDER BY pub_id;"
psql_demo <<'SQL'
DO $$
DECLARE
  canon_count INT;
BEGIN
  SELECT count(*) INTO canon_count
  FROM biblio.publications_canon
  WHERE doi = '10.5555/pubdex-fixture-001';
  IF canon_count <> 1 THEN
    RAISE EXCEPTION
      'acceptance failed: expected the fixture publication in the canonical list, found % row(s)',
      canon_count;
  END IF;
END $$;
SQL

echo "[8/8] Producing and verifying the export..."
demo_python -m people_pubs.sync.export_publications \
  --dsn "$DSN" \
  --output "$WORKDIR/publications.csv"

demo_python - "$WORKDIR/publications.csv" <<'PY'
import csv
import sys

path = sys.argv[1]
with open(path, newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))

if not rows:
    sys.exit("acceptance failed: the export is empty")

wanted = [r for r in rows if r["doi"] == "10.5555/pubdex-fixture-001"]
if len(wanted) != 1:
    sys.exit(
        "acceptance failed: expected exactly one row for the fixture DOI, "
        f"found {len(wanted)}"
    )

row = wanted[0]
problems = []
if row["title"] != "Synthetic Collective Behaviour Paper":
    problems.append(f"title = {row['title']!r}")
if row["year"] != "2024":
    problems.append(f"year = {row['year']!r}")
if row["venue"] != "Journal of Synthetic Metadata":
    problems.append(f"venue = {row['venue']!r}")
if "orcid" not in row["source_of_truth"] or "crossref" not in row["source_of_truth"]:
    problems.append(f"source_of_truth = {row['source_of_truth']!r}")
# Ada is internal in the bundled roster and Bert is external, so only Ada
# may appear here even though both are linked as authors of the work.
if row["internal_authors"] != "Ada Example":
    problems.append(f"internal_authors = {row['internal_authors']!r}")
if problems:
    sys.exit("acceptance failed: " + "; ".join(problems))

print(f"export verified: {len(rows)} row(s), fixture DOI present with the expected values")
PY

echo
echo "--- publications.csv ---"
cat "$WORKDIR/publications.csv"
echo "--- end of export ---"
echo
echo "PASS: clean-clone acceptance run completed with no metadata-provider request."
echo "      bootstrap -> roster -> ORCID fixture -> Crossref fixture -> queries -> export"
