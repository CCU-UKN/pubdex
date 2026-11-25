-- Creates pubdex.schema_changes, the record of the schema changes a database has had.
-- backward-compatible: yes
--
-- The schema upgrade (python -m people_pubs.schema upgrade) applies every
-- schema change of DB/schema/ that a database has not had yet, each in a
-- transaction of its own that also adds the change's row here. A database
-- without this table has had no schema change: it holds the baseline, the
-- schema DB/init/ creates.
--
-- The schema pubdex lies outside the default privileges DB/init/ gives the
-- application roles: app_readonly and app_writer may read the record and
-- nothing else, and only its owner, the role that runs the schema upgrade,
-- writes it. PubDex 0.1.0 reads none of it.

CREATE SCHEMA pubdex;

COMMENT ON SCHEMA pubdex IS
  'PubDex''s own bookkeeping: the record of the schema changes this database has had.';

CREATE TABLE pubdex.schema_changes (
  number integer PRIMARY KEY CHECK (number > 0),
  name text NOT NULL UNIQUE CHECK (
    CASE WHEN name ~ '^[0-9]{4}_[a-z0-9_]+$' THEN left(name, 4)::integer = number ELSE false END
  ),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  backward_compatible boolean NOT NULL,
  release text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  applied_by name NOT NULL DEFAULT current_user
);

COMMENT ON TABLE pubdex.schema_changes IS
  'One row per schema change applied to this database: its number and name (the file NNNN_<name>.sql), '
  'the SHA-256 of the file, whether the preceding PubDex release still works on the changed schema, '
  'the PubDex release whose schema upgrade applied it, when, and as which role.';

GRANT USAGE ON SCHEMA pubdex TO app_readonly, app_writer;
GRANT SELECT ON pubdex.schema_changes TO app_readonly, app_writer;
