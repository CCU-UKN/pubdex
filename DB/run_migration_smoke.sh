#!/usr/bin/env bash
# Migration smoke test: bootstrap from empty, then re-apply the patch.
#
# Proves that DB/init/ bootstraps the full schema from an EMPTY PostgreSQL
# database, and that the runtime patch (010_ingestion_runtime_schema.sql) is
# idempotent: re-applying it to the freshly bootstrapped database must succeed
# without errors and leave every expected object in place.
#
# Runs entirely inside a disposable Docker container with its own data volume.
# Both carry a unique per-run name and a run label, no host port is published,
# and the cleanup registered before anything is created removes only those two
# resources — even after a failed start or an interrupted run. Existing
# containers and volumes are never touched: a name collision aborts instead of
# taking the resource over, and parallel runs do not interfere.
#
#   ./run_migration_smoke.sh                 # full run (needs Docker)
#   SMOKE_PG_IMAGE=postgres:16 ./run_migration_smoke.sh
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${SMOKE_PG_IMAGE:-postgres:16}"
WAIT_SECONDS="${SMOKE_WAIT_SECONDS:-120}"
RUN_ID="$(date +%Y%m%d%H%M%S)-$$-$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
CONTAINER="pubdex-migration-smoke-${RUN_ID}"
VOLUME="pubdex-migration-smoke-${RUN_ID}"
LABEL_KEY="pubdex.migration-smoke.run"

# Removes only the container and volume this run created: both must carry this
# run's label, so a resource that merely shares the name is left alone.
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

echo "[1/4] Starting disposable PostgreSQL ($IMAGE) with DB/init/ mounted (run $RUN_ID)..."
docker volume create --label "$LABEL_KEY=$RUN_ID" "$VOLUME" >/dev/null
docker run -d --name "$CONTAINER" --label "$LABEL_KEY=$RUN_ID" \
  -e POSTGRES_PASSWORD="smoke-$RUN_ID" -e POSTGRES_DB=people_db \
  -v "$VOLUME:/var/lib/postgresql/data" \
  -v "$PWD/init:/docker-entrypoint-initdb.d:ro" \
  "$IMAGE" >/dev/null

# The entrypoint runs the init scripts against a socket-only temporary server
# and only then starts the real one. Require its completion message, evidence
# that every init/*.sql was run, and TCP readiness; stop waiting as soon as the
# container exits (a failed init script stops it).
echo "[2/4] Waiting for bootstrap ($(ls init/*.sql | xargs -n1 basename | tr '\n' ' ')) to finish, up to ${WAIT_SECONDS}s..."
bootstrapped() {
  local logs
  logs="$(docker logs "$CONTAINER" 2>&1)" || return 1
  grep -q 'PostgreSQL init process complete' <<<"$logs" || return 1
  local script
  for script in init/*.sql; do
    grep -q "running /docker-entrypoint-initdb.d/$(basename "$script")" <<<"$logs" || return 1
  done
  docker exec "$CONTAINER" pg_isready -h 127.0.0.1 -U postgres -d people_db >/dev/null 2>&1
}
ready=0
for i in $(seq "$WAIT_SECONDS"); do
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
if [ "$ready" -ne 1 ]; then
  echo "ERROR: database not bootstrapped after ${WAIT_SECONDS}s; container log tail:" >&2
  docker logs --tail 40 "$CONTAINER" >&2 || true
  exit 1
fi

check_objects() {
  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U postgres -d people_db <<'SQL'
DO $$
DECLARE
  missing text := '';
  rel text;
BEGIN
  FOREACH rel IN ARRAY ARRAY[
    -- 000_init.sql bootstrap tables
    'app.people', 'app.identities_orcid', 'app.employments', 'app.person_keys',
    'biblio.publications', 'biblio.authorships',
    'pii.people_pii', 'pii.orcid_payloads', 'pii.orcid_emails',
    'pii.publication_author_emails',
    'activity.source_refresh_state', 'activity.refresh_run_log',
    -- 010 runtime-patch tables (curation durability)
    'biblio.publication_dedup_decisions', 'biblio.publication_dedup_overrides',
    'biblio.authorship_manual_overrides', 'biblio.curation_review_queue',
    'app.person_name_aliases_manual', 'app.institution_attribution_rules',
    -- 010 view chain consumed by the read layer
    'biblio.publications_clean', 'biblio.publications_canon',
    'biblio.publications_canon_web', 'biblio.authorships_curated',
    'biblio.member_open_access_pdfs'
  ] LOOP
    IF to_regclass(rel) IS NULL THEN
      missing := missing || ' ' || rel;
    END IF;
  END LOOP;
  IF missing <> '' THEN
    RAISE EXCEPTION 'schema smoke failed, missing relations:%', missing;
  END IF;
  -- Narrow SECURITY DEFINER PII functions must exist (PII write path)
  IF (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname = 'pii') < 4 THEN
    RAISE EXCEPTION 'schema smoke failed: expected pii.* helper functions';
  END IF;
  -- Per-source ingest envelope column
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'biblio' AND table_name = 'publications'
      AND column_name = 'source_provenance'
  ) THEN
    RAISE EXCEPTION 'schema smoke failed: biblio.publications.source_provenance missing';
  END IF;
  -- Institutional attribution: configurable rules, cached flag, and a signal
  -- that reads the rules table (so it must be STABLE, never IMMUTABLE).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'biblio' AND table_name = 'publications'
      AND column_name = 'institution_attributed'
  ) THEN
    RAISE EXCEPTION 'schema smoke failed: biblio.publications.institution_attributed missing';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'biblio' AND p.proname = 'institution_attribution_signal'
      AND p.provolatile = 's'
  ) THEN
    RAISE EXCEPTION 'schema smoke failed: biblio.institution_attribution_signal missing or not STABLE';
  END IF;
  IF (SELECT count(*) FROM app.institution_attribution_rules) <> 0 THEN
    RAISE EXCEPTION 'schema smoke failed: attribution rules must ship empty';
  END IF;
  IF (SELECT biblio.institution_attribution_signal('{"any": "payload"}'::jsonb)) THEN
    RAISE EXCEPTION 'schema smoke failed: an empty rule set must attribute nothing';
  END IF;

  -- Provisional flag on the canon view
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'biblio' AND table_name = 'publications_canon'
      AND column_name = 'is_provisional'
  ) THEN
    RAISE EXCEPTION 'schema smoke failed: publications_canon.is_provisional missing';
  END IF;
  -- Member OA-PDF harvesting view: stable manifest fields and read-only access.
  IF (
    SELECT count(*)
    FROM information_schema.columns
    WHERE table_schema = 'biblio'
      AND table_name = 'member_open_access_pdfs'
      AND column_name IN (
        'pub_id', 'pdf_url', 'pdf_license', 'rights_tier',
        'member_count', 'member_person_ids', 'members'
      )
  ) <> 7 THEN
    RAISE EXCEPTION 'schema smoke failed: member_open_access_pdfs columns missing';
  END IF;
  IF NOT has_table_privilege(
    'app_readonly',
    'biblio.member_open_access_pdfs',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'schema smoke failed: app_readonly cannot read member_open_access_pdfs';
  END IF;
END $$;
SQL
}

check_member_oa_view() {
  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U postgres -d people_db <<'SQL'
DO $$
DECLARE
  member_id BIGINT;
  external_id BIGINT;
  direct_pub_id BIGINT;
  fallback_pub_id BIGINT;
  external_pub_id BIGINT;
  no_pdf_pub_id BIGINT;
  matched_count INT;
  fallback_url TEXT;
  fallback_rights TEXT;
BEGIN
  INSERT INTO app.people (person_kind, display_name)
  VALUES ('internal', 'Migration Smoke Member')
  RETURNING person_id INTO member_id;

  INSERT INTO app.people (person_kind, display_name)
  VALUES ('external', 'Migration Smoke External')
  RETURNING person_id INTO external_id;

  INSERT INTO biblio.publications (doi, title, source_of_truth, raw_json)
  VALUES (
    '10.0000/member-oa-direct-smoke',
    'Member OA direct PDF smoke',
    'openalex',
    jsonb_build_object(
      'openalex', jsonb_build_object(
        'id', 'https://openalex.org/W000000001',
        'open_access', jsonb_build_object('is_oa', TRUE, 'oa_status', 'gold'),
        'best_oa_location', jsonb_build_object(
          'pdf_url', 'https://example.invalid/direct.pdf',
          'landing_page_url', 'https://example.invalid/direct',
          'license', 'cc-by'
        ),
        'locations', jsonb_build_array()
      )
    )
  )
  RETURNING pub_id INTO direct_pub_id;

  INSERT INTO biblio.publications (doi, title, source_of_truth, raw_json)
  VALUES (
    '10.0000/member-oa-fallback-smoke',
    'Member OA fallback PDF smoke',
    'openalex',
    jsonb_build_object(
      'openalex', jsonb_build_object(
        'id', 'https://openalex.org/W000000002',
        'open_access', jsonb_build_object('is_oa', TRUE, 'oa_status', 'green'),
        'best_oa_location', jsonb_build_object(
          'landing_page_url', 'https://example.invalid/fallback'
        ),
        'locations', jsonb_build_array(
          jsonb_build_object('landing_page_url', 'https://example.invalid/no-pdf'),
          jsonb_build_object(
            'pdf_url', 'https://example.invalid/fallback.pdf',
            'license', 'cc-by-nc'
          )
        )
      )
    )
  )
  RETURNING pub_id INTO fallback_pub_id;

  INSERT INTO biblio.publications (doi, title, source_of_truth, raw_json)
  VALUES (
    '10.0000/external-oa-smoke',
    'External OA PDF smoke',
    'openalex',
    jsonb_build_object(
      'openalex', jsonb_build_object(
        'open_access', jsonb_build_object('is_oa', TRUE, 'oa_status', 'gold'),
        'best_oa_location', jsonb_build_object(
          'pdf_url', 'https://example.invalid/external.pdf',
          'license', 'cc-by'
        ),
        'locations', jsonb_build_array()
      )
    )
  )
  RETURNING pub_id INTO external_pub_id;

  INSERT INTO biblio.publications (doi, title, source_of_truth, raw_json)
  VALUES (
    '10.0000/member-oa-no-pdf-smoke',
    'Member OA without PDF smoke',
    'openalex',
    jsonb_build_object(
      'openalex', jsonb_build_object(
        'open_access', jsonb_build_object('is_oa', TRUE, 'oa_status', 'green'),
        'best_oa_location', jsonb_build_object(
          'landing_page_url', 'https://example.invalid/member-no-pdf'
        ),
        'locations', jsonb_build_array()
      )
    )
  )
  RETURNING pub_id INTO no_pdf_pub_id;

  INSERT INTO biblio.authorships (pub_id, person_id, author_position)
  VALUES
    (direct_pub_id, member_id, 1),
    (fallback_pub_id, member_id, 1),
    (external_pub_id, external_id, 1),
    (no_pdf_pub_id, member_id, 1);

  SELECT count(*)
    INTO matched_count
  FROM biblio.member_open_access_pdfs
  WHERE pub_id IN (direct_pub_id, fallback_pub_id, external_pub_id, no_pdf_pub_id);

  IF matched_count <> 2 THEN
    RAISE EXCEPTION
      'member OA view smoke failed: expected 2 eligible publications, got %',
      matched_count;
  END IF;

  SELECT pdf_url, rights_tier
    INTO fallback_url, fallback_rights
  FROM biblio.member_open_access_pdfs
  WHERE pub_id = fallback_pub_id;

  IF fallback_url IS DISTINCT FROM 'https://example.invalid/fallback.pdf'
     OR fallback_rights IS DISTINCT FROM 'explicit_open_license' THEN
    RAISE EXCEPTION
      'member OA view smoke failed: fallback URL/license selection is wrong';
  END IF;

  DELETE FROM biblio.publications
  WHERE pub_id IN (direct_pub_id, fallback_pub_id, external_pub_id, no_pdf_pub_id);
  DELETE FROM app.people
  WHERE person_id IN (member_id, external_id);
END $$;
SQL
}

# Seeds authorship rows whose is_internal_at_ingest disagrees with the linked
# person's app.people.person_kind -- the shape older ingestion produced, when
# the flag was set from "a person was matched at all". Re-applying the runtime
# patch must repair them.
seed_legacy_internal_flags() {
  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U postgres -d people_db <<'SQL'
DO $$
DECLARE
  internal_id BIGINT;
  external_id BIGINT;
  pub BIGINT;
BEGIN
  INSERT INTO app.people (person_kind, display_name)
  VALUES ('internal', 'Smoke Internal Author')
  RETURNING person_id INTO internal_id;

  INSERT INTO app.people (person_kind, display_name)
  VALUES ('external', 'Smoke External Author')
  RETURNING person_id INTO external_id;

  INSERT INTO biblio.publications (doi, title, source_of_truth)
  VALUES ('10.0000/internal-flag-repair-smoke', 'Internal flag repair smoke', 'crossref')
  RETURNING pub_id INTO pub;

  INSERT INTO biblio.authorships
    (pub_id, person_id, author_position, author_name, is_internal_at_ingest)
  VALUES
    (pub, internal_id, 1, 'Smoke Internal Author', FALSE),
    (pub, external_id, 2, 'Smoke External Author', TRUE),
    (pub, NULL,        3, 'Smoke Unlinked Author', TRUE);
END $$;
SQL
}

check_internal_flag_repair() {
  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U postgres -d people_db <<'SQL'
DO $$
DECLARE
  seeded INT;
  wrong INT;
BEGIN
  -- Guard against a vacuous pass: the seeded rows must actually be there.
  SELECT count(*)
    INTO seeded
  FROM biblio.authorships a
  JOIN biblio.publications p ON p.pub_id = a.pub_id
  WHERE p.doi = '10.0000/internal-flag-repair-smoke';

  IF seeded <> 3 THEN
    RAISE EXCEPTION
      'internal-flag repair check is vacuous: expected 3 seeded rows, found %',
      seeded;
  END IF;

  SELECT count(*)
    INTO wrong
  FROM biblio.authorships a
  JOIN biblio.publications p ON p.pub_id = a.pub_id
  LEFT JOIN app.people pe ON pe.person_id = a.person_id
  WHERE p.doi = '10.0000/internal-flag-repair-smoke'
    AND a.is_internal_at_ingest
        IS DISTINCT FROM COALESCE(pe.person_kind = 'internal', FALSE);

  IF wrong <> 0 THEN
    RAISE EXCEPTION
      'internal-flag repair failed: % authorship row(s) still disagree with person_kind',
      wrong;
  END IF;

  DELETE FROM biblio.publications WHERE doi = '10.0000/internal-flag-repair-smoke';
  DELETE FROM app.people
  WHERE display_name IN ('Smoke Internal Author', 'Smoke External Author');
END $$;
SQL
}

echo "[3/4] Verifying expected schema objects after fresh bootstrap..."
check_objects
check_member_oa_view
echo "      OK"

seed_legacy_internal_flags

echo "[4/4] Re-applying 010_ingestion_runtime_schema.sql (idempotency)..."
docker exec "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U postgres -d people_db \
  -f /docker-entrypoint-initdb.d/010_ingestion_runtime_schema.sql >/dev/null
check_objects
check_member_oa_view
check_internal_flag_repair
echo "      OK — patch re-applied cleanly, all objects still present"

echo "PASS: schema bootstraps from empty DB and the runtime patch is re-runnable."
