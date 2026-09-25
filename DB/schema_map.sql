-- people_db schema map / introspection
--
-- Run from repo root:
--   psql "$PEOPLE_DB_DSN" -f DB/schema_map.sql
--
-- Or with Docker:
--   docker cp DB/schema_map.sql peopledb-postgres:/tmp/schema_map.sql
--   docker exec peopledb-postgres psql -U postgres -d people_db -f /tmp/schema_map.sql

-- 0) Quick existence checks for key operational tables/views
SELECT
  to_regclass('activity.source_refresh_state')        AS source_refresh_state,
  to_regclass('activity.person_refresh_state')        AS person_refresh_state,
  to_regclass('activity.refresh_run_log')             AS refresh_run_log,
  to_regclass('biblio.publication_dedup_overrides')  AS publication_dedup_overrides,
  to_regclass('biblio.publications_clean')            AS publications_clean,
  to_regclass('biblio.publications_canon')            AS publications_canon,
  to_regclass('biblio.member_open_access_pdfs')       AS member_open_access_pdfs,
  to_regclass('biblio.idx_pub_raw_json_trgm')         AS idx_pub_raw_json_trgm,
  to_regclass('biblio.idx_pub_sot_trgm')              AS idx_pub_sot_trgm,
  to_regclass('app.person_name_aliases_manual')       AS person_name_aliases_manual,
  to_regclass('app.person_name_aliases')              AS person_name_aliases;

-- 1) Objects map (tables/views/materialized views/sequences/foreign tables)
WITH target_schemas AS (
  SELECT unnest(ARRAY['app', 'biblio', 'activity', 'pii', 'staging_raw']) AS nspname
)
SELECT
  n.nspname AS schema_name,
  CASE c.relkind
    WHEN 'r' THEN 'table'
    WHEN 'p' THEN 'partitioned table'
    WHEN 'v' THEN 'view'
    WHEN 'm' THEN 'materialized view'
    WHEN 'S' THEN 'sequence'
    WHEN 'f' THEN 'foreign table'
    ELSE c.relkind::text
  END AS object_type,
  c.relname AS object_name,
  pg_get_userbyid(c.relowner) AS owner_name,
  CASE
    WHEN c.relkind IN ('r', 'p', 'm', 'f', 'S')
      THEN pg_size_pretty(pg_total_relation_size(c.oid))
    ELSE NULL
  END AS total_size,
  CASE
    WHEN c.relkind IN ('r', 'p')
      THEN COALESCE(st.n_live_tup::bigint, c.reltuples::bigint)
    ELSE NULL
  END AS est_rows
FROM pg_class c
JOIN pg_namespace n
  ON n.oid = c.relnamespace
JOIN target_schemas ts
  ON ts.nspname = n.nspname
LEFT JOIN pg_stat_user_tables st
  ON st.relid = c.oid
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
ORDER BY n.nspname, object_type, c.relname;

-- 2) Column map (tables + views)
SELECT
  c.table_schema,
  c.table_name,
  c.ordinal_position,
  c.column_name,
  c.udt_name AS pg_type,
  c.is_nullable,
  c.column_default
FROM information_schema.columns c
WHERE c.table_schema IN ('app', 'biblio', 'activity', 'pii', 'staging_raw')
ORDER BY c.table_schema, c.table_name, c.ordinal_position;

-- 3) Constraint map
SELECT
  n.nspname AS table_schema,
  cl.relname AS table_name,
  con.conname AS constraint_name,
  CASE con.contype
    WHEN 'p' THEN 'PRIMARY KEY'
    WHEN 'u' THEN 'UNIQUE'
    WHEN 'f' THEN 'FOREIGN KEY'
    WHEN 'c' THEN 'CHECK'
    WHEN 'x' THEN 'EXCLUSION'
    ELSE con.contype::text
  END AS constraint_type,
  pg_get_constraintdef(con.oid, true) AS constraint_def
FROM pg_constraint con
JOIN pg_class cl
  ON cl.oid = con.conrelid
JOIN pg_namespace n
  ON n.oid = cl.relnamespace
WHERE n.nspname IN ('app', 'biblio', 'activity', 'pii', 'staging_raw')
ORDER BY n.nspname, cl.relname, con.conname;

-- 4) Index map
SELECT
  schemaname AS table_schema,
  tablename AS table_name,
  indexname,
  indexdef
FROM pg_indexes
WHERE schemaname IN ('app', 'biblio', 'activity', 'pii', 'staging_raw')
ORDER BY schemaname, tablename, indexname;

-- 5) Trigger map
SELECT
  trigger_schema,
  event_object_schema AS table_schema,
  event_object_table AS table_name,
  trigger_name,
  action_timing,
  event_manipulation,
  action_statement
FROM information_schema.triggers
WHERE event_object_schema IN ('app', 'biblio', 'activity', 'pii', 'staging_raw')
ORDER BY event_object_schema, event_object_table, trigger_name;
