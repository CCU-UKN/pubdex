-- =========================
-- Extensions
-- =========================
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for digest()
CREATE EXTENSION IF NOT EXISTS unaccent;  -- for name/affil key normalization

-- =========================
-- Schemas
-- =========================
CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS biblio;
CREATE SCHEMA IF NOT EXISTS activity;
CREATE SCHEMA IF NOT EXISTS pii;
CREATE SCHEMA IF NOT EXISTS staging_raw;

-- Encourage SCRAM
ALTER SYSTEM SET password_encryption = 'scram-sha-256';
SELECT pg_reload_conf();

-- =========================
-- Helper function + triggers
-- =========================
CREATE OR REPLACE FUNCTION app.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END $$;

-- Deterministic "name + affiliation" key for de-dup (lowercased, unaccented, SHA-256)
CREATE OR REPLACE FUNCTION app.name_affil_key(p_name TEXT, p_affil TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE AS $$
  SELECT encode(
    digest(
      lower(unaccent(coalesce(p_name,'') || '|' || coalesce(p_affil,''))),
      'sha256'
    ),
    'hex'
  );
$$;

-- =========================
-- app schema
-- =========================

-- Enums
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'person_kind') THEN
    CREATE TYPE app.person_kind AS ENUM ('internal','external');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'person_key_type') THEN
    CREATE TYPE app.person_key_type AS ENUM
      ('orcid','email','gs_profile','crossref','name_affil_key');
  END IF;
END $$;

-- People
CREATE TABLE app.people (
  person_id              BIGSERIAL PRIMARY KEY,
  person_kind            app.person_kind NOT NULL DEFAULT 'external',
  display_name           CITEXT NOT NULL,
  first_name             CITEXT,
  last_name              CITEXT,
  last_profile_refresh_at TIMESTAMPTZ,
  last_change_detected_at TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_people_kind_name ON app.people(person_kind, display_name);
CREATE TRIGGER trg_people_updated
BEFORE UPDATE ON app.people
FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ORCID identity (kept for rich metadata)
CREATE TABLE app.identities_orcid (
  person_id            BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  orcid                TEXT   NOT NULL UNIQUE,
  orcid_first_name     CITEXT,
  orcid_last_name      CITEXT,
  name_match_score     NUMERIC(4,3),
  orcid_last_updated   DATE,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (person_id, orcid),
  CONSTRAINT orcid_format CHECK (orcid ~ '^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9X]{4}$')
);
CREATE TRIGGER trg_id_orcid_updated
BEFORE UPDATE ON app.identities_orcid
FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Generic identity keys for de-dup
CREATE TABLE app.person_keys (
  person_id   BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  key_type    app.person_key_type NOT NULL,
  key_value   CITEXT NOT NULL,
  is_verified BOOLEAN NOT NULL DEFAULT FALSE,
  source      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (key_type, key_value),
  UNIQUE (person_id, key_type, key_value)
);
CREATE INDEX idx_person_keys_person ON app.person_keys(person_id);
-- (Optional) soft format checks
-- ORCID keys look like ORCID
-- You can enforce via partial indexes if you want:
-- CREATE UNIQUE INDEX uq_keys_orcid ON app.person_keys(key_value) WHERE key_type='orcid';

-- Employments (no emails)
CREATE TABLE app.employments (
  employment_id        BIGSERIAL PRIMARY KEY,
  person_id            BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  org_name             TEXT   NOT NULL,
  department           TEXT,
  role_title           TEXT,
  start_date           DATE,
  end_date             DATE,
  is_current           BOOLEAN DEFAULT FALSE,
  source               TEXT   NOT NULL DEFAULT 'orcid',
  raw_json             JSONB,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_employments_person_current_start
  ON app.employments(person_id, is_current DESC, start_date ASC);
CREATE TRIGGER trg_employments_updated
BEFORE UPDATE ON app.employments
FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Touch people.last_change_detected_at when core facts change
CREATE OR REPLACE FUNCTION app.touch_person_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  UPDATE app.people
    SET last_change_detected_at = NOW()
  WHERE person_id = COALESCE(NEW.person_id, OLD.person_id);
  RETURN NEW;
END $$;
CREATE TRIGGER trg_employments_touch
AFTER INSERT OR UPDATE OR DELETE ON app.employments
FOR EACH ROW EXECUTE FUNCTION app.touch_person_change();

-- =========================
-- biblio schema
-- =========================
CREATE TABLE biblio.publications (
  pub_id               BIGSERIAL PRIMARY KEY,
  doi                  TEXT UNIQUE,
  title                TEXT NOT NULL,
  year                 INT,
  venue                TEXT,
  source_of_truth      TEXT NOT NULL DEFAULT 'orcid', -- or crossref/scholar
  raw_json             JSONB,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_publications_title_tsv
  ON biblio.publications
  USING GIN (to_tsvector('simple', coalesce(title,'')));
CREATE TRIGGER trg_publications_updated
BEFORE UPDATE ON biblio.publications
FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- All authors, ordered; store paper-rendered name/affil; no emails here.
CREATE TABLE biblio.authorships (
  pub_id               BIGINT NOT NULL REFERENCES biblio.publications(pub_id) ON DELETE CASCADE,
  person_id            BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  author_position      INT,
  display_name_at_pub  TEXT,
  paper_affiliation    TEXT,
  paper_orcid         TEXT,
  is_corresponding     BOOLEAN DEFAULT FALSE,
  -- snapshot of the person's internal/external status at ingest time
  is_internal_at_ingest BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (pub_id, person_id),
  CONSTRAINT paper_orcid_format CHECK (paper_orcid IS NULL OR paper_orcid ~ '^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9X]{4}$')
);
CREATE INDEX idx_authorships_pub_pos ON biblio.authorships(pub_id, author_position);
CREATE INDEX idx_authorships_person ON biblio.authorships(person_id);

-- Co-author edges view (undirected, person-person)
CREATE OR REPLACE VIEW biblio.v_coauthor_edges AS
WITH pairs AS (
  SELECT
    a1.pub_id,
    LEAST(a1.person_id, a2.person_id) AS u,
    GREATEST(a1.person_id, a2.person_id) AS v
  FROM biblio.authorships a1
  JOIN biblio.authorships a2
    ON a1.pub_id = a2.pub_id
   AND a1.person_id < a2.person_id
)
SELECT
  p.u AS person_id_u,
  p.v AS person_id_v,
  COUNT(*) AS joint_publications,
  MIN(pub.year) AS first_year,
  MAX(pub.year) AS last_year,
  -- current labels (dynamic)
  (SELECT person_kind FROM app.people x WHERE x.person_id = p.u) AS u_kind,
  (SELECT person_kind FROM app.people y WHERE y.person_id = p.v) AS v_kind
FROM pairs p
JOIN biblio.publications pub ON pub.pub_id = p.pub_id
GROUP BY p.u, p.v;

-- =========================
-- activity schema
-- =========================
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'job_type') THEN
    CREATE TYPE activity.job_type AS ENUM ('orcid_refresh','crossref_refresh','scholar_refresh','email_notify');
  END IF;
END $$;

CREATE TABLE activity.fetch_jobs (
  job_id               BIGSERIAL PRIMARY KEY,
  person_id            BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  job_type             activity.job_type NOT NULL,
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at           TIMESTAMPTZ,
  finished_at          TIMESTAMPTZ,
  status               TEXT NOT NULL DEFAULT 'queued',
  error_msg            TEXT
);

-- Per-person ORCID refresh state (legacy + current ORCID loops use this).
CREATE TABLE activity.person_refresh_state (
  person_id                  BIGINT PRIMARY KEY REFERENCES app.people(person_id) ON DELETE CASCADE,
  last_orcid_profile_at      TIMESTAMPTZ,
  last_orcid_pub_refresh_at  TIMESTAMPTZ,
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TRIGGER trg_person_refresh_state_updated
BEFORE UPDATE ON activity.person_refresh_state
FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Generic stale-refresh state used by refresh_state_runner.py
-- Supports:
--   scope_type='person' -> keyed by (source, person_id)
--   scope_type='query'  -> keyed by (source, scope_key)
CREATE TABLE activity.source_refresh_state (
  source               TEXT NOT NULL,
  scope_type           TEXT NOT NULL CHECK (scope_type IN ('person','query')),
  person_id            BIGINT REFERENCES app.people(person_id) ON DELETE CASCADE,
  scope_key            TEXT,
  last_attempt_at      TIMESTAMPTZ,
  last_success_at      TIMESTAMPTZ,
  external_updated_at  TIMESTAMPTZ,
  last_error_at        TIMESTAMPTZ,
  last_error_msg       TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    (scope_type = 'person' AND person_id IS NOT NULL AND scope_key IS NULL)
    OR
    (scope_type = 'query' AND person_id IS NULL AND scope_key IS NOT NULL)
  )
);
CREATE UNIQUE INDEX uq_source_refresh_state_person
  ON activity.source_refresh_state (source, scope_type, person_id)
  WHERE scope_type = 'person';
CREATE UNIQUE INDEX uq_source_refresh_state_query
  ON activity.source_refresh_state (source, scope_type, scope_key)
  WHERE scope_type = 'query';
CREATE INDEX idx_source_refresh_state_last_success
  ON activity.source_refresh_state (source, last_success_at);
CREATE TRIGGER trg_source_refresh_state_updated
BEFORE UPDATE ON activity.source_refresh_state
FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Optional run history (one row per refresh_state_runner job invocation).
CREATE TABLE activity.refresh_run_log (
  run_id               BIGSERIAL PRIMARY KEY,
  runner               TEXT NOT NULL DEFAULT 'refresh_state_runner',
  job_name             TEXT NOT NULL,
  source               TEXT NOT NULL,
  scope_type           TEXT NOT NULL CHECK (scope_type IN ('person','query')),
  person_id            BIGINT REFERENCES app.people(person_id) ON DELETE CASCADE,
  scope_key            TEXT,
  started_at           TIMESTAMPTZ NOT NULL,
  finished_at          TIMESTAMPTZ NOT NULL,
  status               TEXT NOT NULL CHECK (status IN ('success','failed')),
  exit_code            INT,
  error_msg            TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    (scope_type = 'person' AND person_id IS NOT NULL AND scope_key IS NULL)
    OR
    (scope_type = 'query' AND person_id IS NULL AND scope_key IS NOT NULL)
  )
);
CREATE INDEX idx_refresh_run_log_source_finished
  ON activity.refresh_run_log (source, finished_at DESC);
CREATE INDEX idx_refresh_run_log_job_finished
  ON activity.refresh_run_log (job_name, finished_at DESC);

-- =========================
-- pii schema (restricted)
-- =========================
CREATE TABLE pii.people_pii (
  person_id            BIGINT PRIMARY KEY REFERENCES app.people(person_id) ON DELETE CASCADE,
  primary_email        CITEXT,
  emails               CITEXT[],
  consent_flags        JSONB,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TRIGGER trg_people_pii_updated
BEFORE UPDATE ON pii.people_pii
FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ORCID-sourced public-verified emails (still PII)
CREATE TABLE pii.orcid_emails (
  person_id            BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  email                CITEXT NOT NULL,
  verified             BOOLEAN DEFAULT FALSE,
  visibility           TEXT,
  source               TEXT NOT NULL DEFAULT 'orcid',
  PRIMARY KEY (person_id, email)
);

-- Per-publication author email (as printed), for contact history/traceability
CREATE TABLE pii.publication_author_emails (
  pub_id               BIGINT NOT NULL REFERENCES biblio.publications(pub_id) ON DELETE CASCADE,
  person_id            BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  email                CITEXT NOT NULL,
  is_corresponding     BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (pub_id, person_id, email)
);

-- Email sending log
CREATE TABLE pii.email_log (
  email_id             BIGSERIAL PRIMARY KEY,
  person_id            BIGINT REFERENCES app.people(person_id) ON DELETE SET NULL,
  to_address           CITEXT NOT NULL,
  subject              TEXT NOT NULL,
  sent_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status               TEXT NOT NULL DEFAULT 'sent',
  provider_msg_id      TEXT,
  payload_json         JSONB
);

-- Raw payloads (may contain PII)
CREATE TABLE pii.orcid_payloads (
  person_id            BIGINT PRIMARY KEY REFERENCES app.people(person_id) ON DELETE CASCADE,
  person_json          JSONB,
  record_json          JSONB,
  fetched_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================
-- staging_raw (optional)
-- =========================
-- add sanitized debugging tables here if desired

-- =========================
-- Roles & Grants
-- =========================
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_readonly') THEN
    CREATE ROLE app_readonly LOGIN PASSWORD 'change-me-ro';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_writer') THEN
    CREATE ROLE app_writer  LOGIN PASSWORD 'change-me-rw';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'maintenance') THEN
    CREATE ROLE maintenance LOGIN PASSWORD 'change-me-admin';
  END IF;
END $$;

-- Allow using these schemas
GRANT USAGE ON SCHEMA app, biblio, activity TO app_readonly, app_writer;
GRANT USAGE ON SCHEMA pii TO maintenance;
GRANT USAGE ON SCHEMA staging_raw TO app_readonly, app_writer;

-- Read-only can SELECT app/biblio/activity/staging_raw
-- (staging_raw included to match the grants applied on live databases)
GRANT SELECT ON ALL TABLES IN SCHEMA app, biblio, activity, staging_raw TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA app      GRANT SELECT ON TABLES TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA biblio   GRANT SELECT ON TABLES TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA activity GRANT SELECT ON TABLES TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA staging_raw GRANT SELECT ON TABLES TO app_readonly;

-- Writer can R/W app/biblio/activity
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app, biblio, activity TO app_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA app      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA biblio   GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA activity GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_writer;

-- PII locked to maintenance only
REVOKE ALL ON SCHEMA pii FROM PUBLIC;
GRANT USAGE ON SCHEMA pii TO maintenance;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA pii TO maintenance;
ALTER DEFAULT PRIVILEGES IN SCHEMA pii GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO maintenance;

-- Optional: lock down public schema
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO postgres;
