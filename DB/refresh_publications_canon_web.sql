\set ON_ERROR_STOP on

BEGIN;

-- Serialize manual and scheduled refreshes across all hosts. The lock is
-- transaction-level, so a failed run (psql exits on ON_ERROR_STOP) releases it
-- together with the rollback.
SELECT pg_advisory_xact_lock(hashtext('biblio.publications_canon_web.refresh'));

-- Both snapshots are refreshed in ONE transaction: readers keep the previous
-- pair until COMMIT publishes the new pair together, so the read layer — which
-- reloads its runtime caches when publications_canon_web.snapshot_refreshed_at
-- changes and then reads publication_dedup_map_web — can never pair a newer
-- canon snapshot with an older winner map. CONCURRENTLY keeps both snapshots
-- readable while the refresh runs, and PostgreSQL allows it inside a
-- transaction block.
REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publication_dedup_map_web;

REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publications_canon_web;

SELECT
  count(*) AS publications,
  max(snapshot_refreshed_at) AS snapshot_refreshed_at
FROM biblio.publications_canon_web;

COMMIT;
