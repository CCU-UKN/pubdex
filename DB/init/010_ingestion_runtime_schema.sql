-- Bring fresh bootstrap databases in line with the current ingestion code.
-- This file is intentionally idempotent so it can also be applied manually to
-- older databases after restore.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Publications: current importers write preprint classification.
ALTER TABLE biblio.publications
  ADD COLUMN IF NOT EXISTS is_preprint BOOLEAN;

-- Per-source ingest envelope, keyed by the same source families as raw_json:
--   {"<source>": {"fetched_at", "transform_version", "payload_sha256"}}
-- Maintained by upsert_publication(), the shared backfill updater, and both
-- duplicate-merge paths. Exports/API may expose this envelope; raw payloads
-- themselves stay in the database.
ALTER TABLE biblio.publications
  ADD COLUMN IF NOT EXISTS source_provenance JSONB;

CREATE INDEX IF NOT EXISTS idx_pub_raw_json_trgm
  ON biblio.publications
  USING GIN (lower(raw_json::text) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_pub_sot_trgm
  ON biblio.publications
  USING GIN (lower(source_of_truth) gin_trgm_ops);

-- Authorships: current importers key reruns by publication + author position.
ALTER TABLE biblio.authorships
  ALTER COLUMN author_position SET NOT NULL,
  ADD COLUMN IF NOT EXISTS order_tag TEXT,
  ADD COLUMN IF NOT EXISTS equal_contrib_tag TEXT,
  ADD COLUMN IF NOT EXISTS author_name TEXT,
  ADD COLUMN IF NOT EXISTS author_orcid TEXT,
  ADD COLUMN IF NOT EXISTS affiliations TEXT[],
  ADD COLUMN IF NOT EXISTS raw_crossref_author_json JSONB,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
DECLARE
  pkey_cols TEXT;
BEGIN
  SELECT string_agg(att.attname, ',' ORDER BY ord.n)
    INTO pkey_cols
  FROM pg_constraint c
  JOIN unnest(c.conkey) WITH ORDINALITY AS ord(attnum, n) ON TRUE
  JOIN pg_attribute att
    ON att.attrelid = c.conrelid
   AND att.attnum = ord.attnum
  WHERE c.conrelid = 'biblio.authorships'::regclass
    AND c.contype = 'p';

  IF pkey_cols IS DISTINCT FROM 'pub_id,author_position' THEN
    IF pkey_cols IS NOT NULL THEN
      ALTER TABLE biblio.authorships DROP CONSTRAINT authorships_pkey;
    END IF;
    ALTER TABLE biblio.authorships ALTER COLUMN person_id DROP NOT NULL;
    ALTER TABLE biblio.authorships
      ADD CONSTRAINT authorships_pkey PRIMARY KEY (pub_id, author_position);
  ELSE
    ALTER TABLE biblio.authorships ALTER COLUMN person_id DROP NOT NULL;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS authorships_pub_person_uniq
  ON biblio.authorships (pub_id, person_id)
  WHERE person_id IS NOT NULL;

-- is_internal_at_ingest is documented as a snapshot of the linked person's
-- app.people.person_kind, but ingestion used to set it from "a person was
-- matched at all". Because external co-authors are linked to person rows too,
-- they were recorded -- and reported by biblio.publications_clean -- as
-- internal authors. Ingestion now derives the flag from person_kind; this
-- repairs rows written before that.
--
-- Re-deriving is the right correction rather than a loss of history: every
-- importer rewrites this flag when it re-ingests a row, so it has always
-- tracked the latest ingest rather than the first one.
--
-- Idempotent: each statement only touches rows that disagree with the
-- classification, so a second run updates nothing. Curator detach decisions
-- clear person_id, so they are untouched by the first statement and settled
-- by the second.
UPDATE biblio.authorships a
SET is_internal_at_ingest = (p.person_kind = 'internal'),
    updated_at = NOW()
FROM app.people p
WHERE p.person_id = a.person_id
  AND a.is_internal_at_ingest IS DISTINCT FROM (p.person_kind = 'internal');

UPDATE biblio.authorships a
SET is_internal_at_ingest = FALSE,
    updated_at = NOW()
WHERE a.person_id IS NULL
  AND a.is_internal_at_ingest IS TRUE;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_authorships_updated'
      AND tgrelid = 'biblio.authorships'::regclass
  ) THEN
    CREATE TRIGGER trg_authorships_updated
    BEFORE UPDATE ON biblio.authorships
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
  END IF;
END $$;

-- ORCID/profile and alias support used by add_people, merge_people, and
-- authorship matching helpers.
ALTER TABLE app.identities_orcid
  ADD COLUMN IF NOT EXISTS orcid_display_name TEXT;

CREATE TABLE IF NOT EXISTS app.identities_scholar (
  person_id                 BIGINT PRIMARY KEY REFERENCES app.people(person_id) ON DELETE CASCADE,
  scholar_id                TEXT NOT NULL,
  profile_url               TEXT,
  scholar_display_name      TEXT,
  scholar_affiliation       TEXT,
  scholar_email_domain      TEXT,
  h_index_all               INT,
  h_index_recent            INT,
  citations_all             INT,
  citations_recent          INT,
  name_match_score          NUMERIC,
  paper_overlap_score       NUMERIC,
  affiliation_match_score   NUMERIC,
  last_scholar_profile_at   TIMESTAMPTZ,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_id_scholar_updated'
      AND tgrelid = 'app.identities_scholar'::regclass
  ) THEN
    CREATE TRIGGER trg_id_scholar_updated
    BEFORE UPDATE ON app.identities_scholar
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS app.person_name_aliases_manual (
  person_id       BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  alias_name      TEXT NOT NULL,
  alias_sources   TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS person_name_aliases_manual_uq
  ON app.person_name_aliases_manual (person_id, lower(alias_name));

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_person_name_aliases_manual_updated'
      AND tgrelid = 'app.person_name_aliases_manual'::regclass
  ) THEN
    CREATE TRIGGER trg_person_name_aliases_manual_updated
    BEFORE UPDATE ON app.person_name_aliases_manual
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
  END IF;
END $$;

CREATE OR REPLACE VIEW app.person_name_aliases AS
WITH raw_aliases AS (
  SELECT
    p.person_id,
    'people.display_name'::TEXT AS source,
    p.display_name::TEXT AS alias_name
  FROM app.people p
  WHERE p.display_name IS NOT NULL
    AND btrim(p.display_name::TEXT) <> ''
  UNION ALL
  SELECT
    io.person_id,
    'orcid.display_name'::TEXT AS source,
    io.orcid_display_name AS alias_name
  FROM app.identities_orcid io
  WHERE io.orcid_display_name IS NOT NULL
    AND btrim(io.orcid_display_name) <> ''
  UNION ALL
  SELECT
    io.person_id,
    'orcid.given+family'::TEXT AS source,
    io.orcid_first_name::TEXT || ' ' || io.orcid_last_name::TEXT AS alias_name
  FROM app.identities_orcid io
  WHERE io.orcid_first_name IS NOT NULL
    AND io.orcid_last_name IS NOT NULL
    AND btrim(io.orcid_first_name::TEXT) <> ''
    AND btrim(io.orcid_last_name::TEXT) <> ''
  UNION ALL
  SELECT
    a.person_id,
    'authorship.display_name_at_pub'::TEXT AS source,
    a.display_name_at_pub AS alias_name
  FROM biblio.authorships a
  WHERE a.person_id IS NOT NULL
    AND a.display_name_at_pub IS NOT NULL
    AND btrim(a.display_name_at_pub) <> ''
  UNION ALL
  SELECT
    a.person_id,
    'authorship.author_name'::TEXT AS source,
    a.author_name AS alias_name
  FROM biblio.authorships a
  WHERE a.person_id IS NOT NULL
    AND a.author_name IS NOT NULL
    AND btrim(a.author_name) <> ''
  UNION ALL
  SELECT
    pam.person_id,
    s.source,
    pam.alias_name
  FROM app.person_name_aliases_manual pam
  CROSS JOIN LATERAL unnest(COALESCE(pam.alias_sources, '{}'::TEXT[])) AS s(source)
  WHERE pam.alias_name IS NOT NULL
    AND btrim(pam.alias_name) <> ''
),
normalized AS (
  SELECT
    person_id,
    btrim(alias_name) AS alias_name,
    source
  FROM raw_aliases
  WHERE alias_name IS NOT NULL
    AND alias_name <> ''
)
SELECT
  person_id,
  alias_name,
  array_agg(DISTINCT source ORDER BY source) AS alias_sources
FROM normalized
GROUP BY person_id, alias_name;

-- Refresh state: current runner supports generic person/query scopes and
-- resumable checkpoints.
ALTER TABLE activity.person_refresh_state
  ADD COLUMN IF NOT EXISTS last_orcid_works_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_scholar_sync_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_email_reminder_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_scholar_pub_refresh_at TIMESTAMPTZ;

ALTER TABLE activity.source_refresh_state
  ADD COLUMN IF NOT EXISTS checkpoint_json JSONB NOT NULL DEFAULT '{}'::JSONB;

-- Manual curation support used by attach_authorship, dedup override tools, and
-- curation administration (people_pubs.curation_admin).
CREATE TABLE IF NOT EXISTS biblio.manual_corrections_log (
  log_id            BIGSERIAL PRIMARY KEY,
  correction_type   TEXT NOT NULL,
  target            JSONB NOT NULL,
  before_state      JSONB,
  after_state       JSONB,
  applied_by        TEXT,
  applied_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  note              TEXT
);

CREATE TABLE IF NOT EXISTS biblio.publication_dedup_overrides (
  pub_id         BIGINT PRIMARY KEY REFERENCES biblio.publications(pub_id) ON DELETE CASCADE,
  mode           TEXT NOT NULL CHECK (mode IN ('merge_to', 'keep_separate')),
  winner_pub_id  BIGINT REFERENCES biblio.publications(pub_id) ON DELETE CASCADE,
  note           TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    (mode = 'merge_to' AND winner_pub_id IS NOT NULL AND winner_pub_id <> pub_id)
    OR
    (mode = 'keep_separate' AND winner_pub_id IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_publication_dedup_overrides_winner
  ON biblio.publication_dedup_overrides (winner_pub_id);

CREATE TABLE IF NOT EXISTS biblio.publication_dedup_decisions (
  decision_id        BIGSERIAL PRIMARY KEY,
  mode               TEXT NOT NULL CHECK (mode IN ('merge_to', 'keep_separate', 'confirm_winner')),
  subject_pub_id     BIGINT,
  subject_doi        TEXT,
  subject_doi_key    TEXT,
  subject_title      TEXT,
  subject_title_key  TEXT,
  subject_year       INT,
  subject_snapshot   JSONB NOT NULL DEFAULT '{}'::JSONB,
  winner_pub_id      BIGINT,
  winner_doi         TEXT,
  winner_doi_key     TEXT,
  winner_title       TEXT,
  winner_title_key   TEXT,
  winner_year        INT,
  winner_snapshot    JSONB NOT NULL DEFAULT '{}'::JSONB,
  active             BOOLEAN NOT NULL DEFAULT TRUE,
  note               TEXT,
  evidence           JSONB NOT NULL DEFAULT '{}'::JSONB,
  created_by         TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (subject_doi_key IS NOT NULL OR subject_title_key IS NOT NULL),
  CHECK (
    (mode IN ('merge_to', 'confirm_winner') AND (winner_doi_key IS NOT NULL OR winner_title_key IS NOT NULL))
    OR
    (mode = 'keep_separate' AND winner_doi_key IS NULL AND winner_title_key IS NULL)
  )
);

ALTER TABLE biblio.publication_dedup_decisions
  DROP CONSTRAINT IF EXISTS publication_dedup_decisions_mode_check,
  DROP CONSTRAINT IF EXISTS publication_dedup_decisions_selector_check,
  DROP CONSTRAINT IF EXISTS publication_dedup_decisions_winner_check;

DO $$
DECLARE
  old_conname TEXT;
BEGIN
  FOR old_conname IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'biblio.publication_dedup_decisions'::regclass
      AND contype = 'c'
      AND (
        pg_get_constraintdef(oid) LIKE '%mode IN (''merge_to'', ''keep_separate'')%'
        OR pg_get_constraintdef(oid) LIKE '%mode = ''merge_to''%'
        OR pg_get_constraintdef(oid) LIKE '%winner_doi_key IS NULL AND winner_title_key IS NULL%'
      )
  LOOP
    EXECUTE format(
      'ALTER TABLE biblio.publication_dedup_decisions DROP CONSTRAINT %I',
      old_conname
    );
  END LOOP;
END $$;

ALTER TABLE biblio.publication_dedup_decisions
  ADD CONSTRAINT publication_dedup_decisions_mode_check
    CHECK (mode IN ('merge_to', 'keep_separate', 'confirm_winner')),
  ADD CONSTRAINT publication_dedup_decisions_selector_check
    CHECK (subject_doi_key IS NOT NULL OR subject_title_key IS NOT NULL),
  ADD CONSTRAINT publication_dedup_decisions_winner_check
    CHECK (
      (mode IN ('merge_to', 'confirm_winner') AND (winner_doi_key IS NOT NULL OR winner_title_key IS NOT NULL))
      OR
      (mode = 'keep_separate' AND winner_doi_key IS NULL AND winner_title_key IS NULL)
    );

CREATE INDEX IF NOT EXISTS idx_publication_dedup_decisions_subject_doi
  ON biblio.publication_dedup_decisions (subject_doi_key)
  WHERE active IS TRUE AND subject_doi_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_publication_dedup_decisions_subject_title
  ON biblio.publication_dedup_decisions (subject_title_key, subject_year)
  WHERE active IS TRUE AND subject_title_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_publication_dedup_decisions_winner_doi
  ON biblio.publication_dedup_decisions (winner_doi_key)
  WHERE active IS TRUE AND winner_doi_key IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_publication_dedup_overrides_updated'
      AND tgrelid = 'biblio.publication_dedup_overrides'::regclass
  ) THEN
    CREATE TRIGGER trg_publication_dedup_overrides_updated
    BEFORE UPDATE ON biblio.publication_dedup_overrides
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_publication_dedup_decisions_updated'
      AND tgrelid = 'biblio.publication_dedup_decisions'::regclass
  ) THEN
    CREATE TRIGGER trg_publication_dedup_decisions_updated
    BEFORE UPDATE ON biblio.publication_dedup_decisions
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS biblio.curation_review_queue (
  queue_id             BIGSERIAL PRIMARY KEY,
  review_type          TEXT NOT NULL,
  subject_type         TEXT NOT NULL,
  subject_pub_id       BIGINT REFERENCES biblio.publications(pub_id) ON DELETE SET NULL,
  subject_person_id    BIGINT REFERENCES app.people(person_id) ON DELETE SET NULL,
  status               TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'cancelled')),
  requested_by         TEXT,
  requested_role       TEXT,
  approved_by          TEXT,
  approved_role        TEXT,
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  approved_at          TIMESTAMPTZ,
  applied_at           TIMESTAMPTZ,
  note                 TEXT,
  payload              JSONB NOT NULL DEFAULT '{}'::JSONB,
  resolution           JSONB NOT NULL DEFAULT '{}'::JSONB,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_curation_review_queue_subject
  ON biblio.curation_review_queue (subject_type, subject_pub_id, subject_person_id);

CREATE INDEX IF NOT EXISTS idx_curation_review_queue_status
  ON biblio.curation_review_queue (status, requested_at DESC);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_curation_review_queue_updated'
      AND tgrelid = 'biblio.curation_review_queue'::regclass
  ) THEN
    CREATE TRIGGER trg_curation_review_queue_updated
    BEFORE UPDATE ON biblio.curation_review_queue
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS biblio.canon_scope_decisions (
  decision_id          BIGSERIAL PRIMARY KEY,
  scope_type           TEXT NOT NULL DEFAULT 'global'
                       CHECK (scope_type IN ('global', 'person')),
  scope_person_id      BIGINT REFERENCES app.people(person_id) ON DELETE CASCADE,
  mode                 TEXT NOT NULL CHECK (mode IN ('include', 'exclude')),
  subject_pub_id       BIGINT,
  subject_doi          TEXT,
  subject_doi_key      TEXT,
  subject_title        TEXT,
  subject_title_key    TEXT,
  subject_year         INT,
  subject_snapshot     JSONB NOT NULL DEFAULT '{}'::JSONB,
  active               BOOLEAN NOT NULL DEFAULT TRUE,
  queue_id             BIGINT REFERENCES biblio.curation_review_queue(queue_id) ON DELETE SET NULL,
  note                 TEXT,
  evidence             JSONB NOT NULL DEFAULT '{}'::JSONB,
  created_by           TEXT,
  created_role         TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (subject_doi_key IS NOT NULL OR subject_title_key IS NOT NULL),
  CHECK (
    (scope_type = 'global' AND scope_person_id IS NULL)
    OR
    (scope_type = 'person' AND scope_person_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_canon_scope_decisions_subject_doi
  ON biblio.canon_scope_decisions (scope_type, scope_person_id, subject_doi_key)
  WHERE active IS TRUE AND subject_doi_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_canon_scope_decisions_subject_title
  ON biblio.canon_scope_decisions (scope_type, scope_person_id, subject_title_key, subject_year)
  WHERE active IS TRUE AND subject_title_key IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_canon_scope_decisions_updated'
      AND tgrelid = 'biblio.canon_scope_decisions'::regclass
  ) THEN
    CREATE TRIGGER trg_canon_scope_decisions_updated
    BEFORE UPDATE ON biblio.canon_scope_decisions
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS biblio.authorship_manual_overrides (
  override_id          BIGSERIAL PRIMARY KEY,
  pub_id               BIGINT NOT NULL REFERENCES biblio.publications(pub_id) ON DELETE CASCADE,
  author_position      INT,
  display_name_at_pub  TEXT,
  paper_orcid          TEXT,
  mode                 TEXT NOT NULL CHECK (mode IN ('force_person', 'force_null', 'ignore_row')),
  person_id            BIGINT REFERENCES app.people(person_id) ON DELETE RESTRICT,
  lock_row             BOOLEAN NOT NULL DEFAULT TRUE,
  active               BOOLEAN NOT NULL DEFAULT TRUE,
  reason               TEXT,
  evidence             JSONB NOT NULL DEFAULT '{}'::JSONB,
  source_file          TEXT,
  source_row           INT,
  created_by           TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    author_position IS NOT NULL
    OR NULLIF(btrim(display_name_at_pub), '') IS NOT NULL
    OR NULLIF(btrim(paper_orcid), '') IS NOT NULL
  ),
  CHECK (
    (mode = 'force_person' AND person_id IS NOT NULL)
    OR mode IN ('force_null', 'ignore_row')
  )
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_authorship_manual_overrides_updated'
      AND tgrelid = 'biblio.authorship_manual_overrides'::regclass
  ) THEN
    CREATE TRIGGER trg_authorship_manual_overrides_updated
    BEFORE UPDATE ON biblio.authorship_manual_overrides
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
  END IF;
END $$;

CREATE OR REPLACE VIEW biblio.authorships_curated AS
WITH matched_overrides AS (
  SELECT
    a.pub_id,
    a.author_position,
    o.override_id,
    o.mode,
    o.person_id AS override_person_id,
    row_number() OVER (
      PARTITION BY a.pub_id, a.author_position
      ORDER BY
        CASE
          WHEN NULLIF(btrim(o.paper_orcid), '') IS NOT NULL THEN 0
          WHEN NULLIF(btrim(o.display_name_at_pub), '') IS NOT NULL THEN 1
          WHEN o.author_position IS NOT NULL THEN 2
          ELSE 3
        END,
        o.updated_at DESC,
        o.override_id DESC
    ) AS rn
  FROM biblio.authorships a
  JOIN biblio.authorship_manual_overrides o
    ON o.pub_id = a.pub_id
   AND o.active IS TRUE
   AND (o.author_position IS NULL OR o.author_position = a.author_position)
   AND (
     NULLIF(btrim(o.display_name_at_pub), '') IS NULL
     OR lower(COALESCE(a.display_name_at_pub, a.author_name, '')) = lower(btrim(o.display_name_at_pub))
   )
   AND (
     NULLIF(btrim(o.paper_orcid), '') IS NULL
     OR lower(COALESCE(a.paper_orcid, a.author_orcid, '')) = lower(btrim(o.paper_orcid))
   )
)
SELECT
  a.pub_id,
  CASE
    WHEN mo.mode = 'force_person' THEN mo.override_person_id
    WHEN mo.mode IN ('force_null', 'ignore_row') THEN NULL
    ELSE a.person_id
  END AS person_id,
  a.author_position,
  a.display_name_at_pub,
  a.paper_affiliation,
  a.paper_orcid,
  a.is_corresponding,
  CASE
    -- Forcing a person records who the author is, not that they are ours:
    -- the overridden person's own classification decides.
    WHEN mo.mode = 'force_person' THEN COALESCE(ov.person_kind = 'internal', FALSE)
    WHEN mo.mode IN ('force_null', 'ignore_row') THEN FALSE
    ELSE a.is_internal_at_ingest
  END AS is_internal_at_ingest,
  a.order_tag,
  a.equal_contrib_tag,
  a.author_name,
  a.author_orcid,
  a.affiliations,
  a.raw_crossref_author_json,
  a.created_at,
  a.updated_at,
  mo.override_id AS manual_override_id,
  mo.mode AS manual_override_mode,
  mo.override_person_id AS manual_override_person_id
FROM biblio.authorships a
LEFT JOIN matched_overrides mo
  ON mo.pub_id = a.pub_id
 AND mo.author_position = a.author_position
 AND mo.rn = 1
LEFT JOIN app.people ov
  ON ov.person_id = mo.override_person_id
WHERE COALESCE(mo.mode, '') <> 'ignore_row';

-- One row per OpenAlex-confirmed open-access publication that has both a PDF
-- URL and at least one currently-internal person through the curated
-- authorship overlay. The selected PDF location prefers best_oa_location when
-- it has a URL, then falls back deterministically to the first locations[]
-- entry with one. License/source fields always describe that selected
-- location; rights_tier is a harvesting aid, not a grant of additional rights.
CREATE OR REPLACE VIEW biblio.member_open_access_pdfs AS
WITH linked_members AS (
  SELECT DISTINCT
    a.pub_id,
    a.person_id
  FROM biblio.authorships_curated a
  JOIN app.people pe ON pe.person_id = a.person_id
  WHERE a.person_id IS NOT NULL
    AND pe.person_kind::TEXT = 'internal'
),
member_details AS (
  SELECT
    lm.pub_id,
    lm.person_id,
    pe.display_name::TEXT AS display_name,
    COALESCE(
      (
        SELECT jsonb_agg(io.orcid ORDER BY io.orcid)
        FROM app.identities_orcid io
        WHERE io.person_id = lm.person_id
      ),
      '[]'::JSONB
    ) AS orcids
  FROM linked_members lm
  JOIN app.people pe ON pe.person_id = lm.person_id
),
member_summary AS (
  SELECT
    md.pub_id,
    count(*)::INT AS member_count,
    array_agg(md.person_id ORDER BY md.person_id) AS member_person_ids,
    jsonb_agg(
      jsonb_build_object(
        'person_id', md.person_id,
        'display_name', md.display_name,
        'orcids', md.orcids
      )
      ORDER BY md.person_id
    ) AS members
  FROM member_details md
  GROUP BY md.pub_id
)
SELECT
  p.pub_id,
  p.doi,
  p.title,
  p.year,
  p.venue,
  p.is_preprint,
  p.source_of_truth,
  p.raw_json #>> '{openalex,id}' AS openalex_id,
  p.raw_json #>> '{openalex,open_access,oa_status}' AS oa_status,
  p.raw_json #>> '{openalex,open_access,oa_url}' AS oa_url,
  selected.location ->> 'pdf_url' AS pdf_url,
  selected.location ->> 'landing_page_url' AS pdf_landing_page_url,
  selected.location ->> 'license' AS pdf_license,
  selected.location ->> 'license_id' AS pdf_license_id,
  selected.location ->> 'version' AS pdf_version,
  selected.location #>> '{source,id}' AS pdf_source_id,
  selected.location #>> '{source,display_name}' AS pdf_source_name,
  selected.location #>> '{source,type}' AS pdf_source_type,
  p.raw_json #>> '{openalex,best_oa_location,license}' AS best_oa_license,
  CASE
    WHEN lower(COALESCE(selected.location ->> 'license', '')) = 'public-domain'
      OR lower(COALESCE(selected.location ->> 'license', '')) LIKE 'cc-%'
      THEN 'explicit_open_license'
    WHEN NULLIF(btrim(selected.location ->> 'license'), '') IS NULL
      THEN 'license_missing'
    ELSE 'source_terms_review'
  END AS rights_tier,
  ms.member_count,
  ms.member_person_ids,
  ms.members
FROM biblio.publications p
JOIN member_summary ms ON ms.pub_id = p.pub_id
CROSS JOIN LATERAL (
  SELECT candidate.location
  FROM (
    SELECT
      p.raw_json #> '{openalex,best_oa_location}' AS location,
      0::BIGINT AS priority,
      0::BIGINT AS ordinality
    UNION ALL
    SELECT
      location_item.value AS location,
      1::BIGINT AS priority,
      location_item.ordinality::BIGINT
    FROM jsonb_array_elements(
      CASE
        WHEN jsonb_typeof(p.raw_json #> '{openalex,locations}') = 'array'
          THEN p.raw_json #> '{openalex,locations}'
        ELSE '[]'::JSONB
      END
    ) WITH ORDINALITY AS location_item(value, ordinality)
  ) candidate
  WHERE jsonb_typeof(candidate.location) = 'object'
    AND NULLIF(btrim(candidate.location ->> 'pdf_url'), '') IS NOT NULL
  ORDER BY candidate.priority, candidate.ordinality
  LIMIT 1
) selected
WHERE COALESCE(
  (p.raw_json #>> '{openalex,open_access,is_oa}')::BOOLEAN,
  FALSE
);

COMMENT ON VIEW biblio.member_open_access_pdfs IS
  'Download-manifest source: OpenAlex-confirmed OA publications with a PDF URL and at least one curated internal member; inspect pdf_license and rights_tier before use or redistribution.';

CREATE OR REPLACE VIEW app.person_name_aliases AS
WITH raw_aliases AS (
  SELECT
    p.person_id,
    'people.display_name'::TEXT AS source,
    p.display_name::TEXT AS alias_name
  FROM app.people p
  WHERE p.display_name IS NOT NULL
    AND btrim(p.display_name::TEXT) <> ''
  UNION ALL
  SELECT
    io.person_id,
    'orcid.display_name'::TEXT AS source,
    io.orcid_display_name AS alias_name
  FROM app.identities_orcid io
  WHERE io.orcid_display_name IS NOT NULL
    AND btrim(io.orcid_display_name) <> ''
  UNION ALL
  SELECT
    io.person_id,
    'orcid.given+family'::TEXT AS source,
    io.orcid_first_name::TEXT || ' ' || io.orcid_last_name::TEXT AS alias_name
  FROM app.identities_orcid io
  WHERE io.orcid_first_name IS NOT NULL
    AND io.orcid_last_name IS NOT NULL
    AND btrim(io.orcid_first_name::TEXT) <> ''
    AND btrim(io.orcid_last_name::TEXT) <> ''
  UNION ALL
  SELECT
    a.person_id,
    'authorship.display_name_at_pub'::TEXT AS source,
    a.display_name_at_pub AS alias_name
  FROM biblio.authorships_curated a
  WHERE a.person_id IS NOT NULL
    AND a.display_name_at_pub IS NOT NULL
    AND btrim(a.display_name_at_pub) <> ''
  UNION ALL
  SELECT
    a.person_id,
    'authorship.author_name'::TEXT AS source,
    a.author_name AS alias_name
  FROM biblio.authorships_curated a
  WHERE a.person_id IS NOT NULL
    AND a.author_name IS NOT NULL
    AND btrim(a.author_name) <> ''
  UNION ALL
  SELECT
    pam.person_id,
    s.source,
    pam.alias_name
  FROM app.person_name_aliases_manual pam
  CROSS JOIN LATERAL unnest(COALESCE(pam.alias_sources, '{}'::TEXT[])) AS s(source)
  WHERE pam.alias_name IS NOT NULL
    AND btrim(pam.alias_name) <> ''
),
normalized AS (
  SELECT
    person_id,
    btrim(alias_name) AS alias_name,
    source
  FROM raw_aliases
  WHERE alias_name IS NOT NULL
    AND alias_name <> ''
)
SELECT
  person_id,
  alias_name,
  array_agg(DISTINCT source ORDER BY source) AS alias_sources
FROM normalized
GROUP BY person_id, alias_name;

-- publication_dedup_map_web (recreated further below) snapshots this view;
-- drop it first so replacing the view can never be blocked by the dependent
-- materialized view when the view's signature changes.
DROP MATERIALIZED VIEW IF EXISTS biblio.publication_dedup_map_web;

CREATE OR REPLACE VIEW biblio.publication_dedup_memberships AS
WITH pub_keys AS (
  SELECT
    p.*,
    lower(regexp_replace(btrim(p.title), '\s+', ' ', 'g')) AS norm_title,
    NULLIF(lower(
      regexp_replace(
        regexp_replace(btrim(COALESCE(p.doi, '')), '^\s*doi:\s*', '', 'i'),
        '^\s*https?://(?:dx\.)?doi\.org/', '', 'i'
      )
    ), '') AS doi_key
  FROM biblio.publications p
),
durable_override_matches AS (
  SELECT
    p.pub_id,
    d.decision_id,
    d.mode,
    CASE WHEN d.mode IN ('merge_to', 'confirm_winner') THEN winner.pub_id ELSE NULL END AS winner_pub_id,
    row_number() OVER (
      PARTITION BY p.pub_id
      ORDER BY
        CASE
          WHEN d.subject_doi_key IS NOT NULL AND p.doi_key = d.subject_doi_key THEN 0
          ELSE 1
        END,
        d.updated_at DESC,
        d.decision_id DESC
    ) AS rn
  FROM pub_keys p
  JOIN biblio.publication_dedup_decisions d
    ON d.active IS TRUE
   AND (
      (d.subject_doi_key IS NOT NULL AND p.doi_key = d.subject_doi_key)
      OR
      (
        d.subject_doi_key IS NULL
        AND
        d.subject_title_key IS NOT NULL
        AND p.norm_title = d.subject_title_key
        AND (d.subject_year IS NULL OR p.year = d.subject_year)
      )
    )
  LEFT JOIN LATERAL (
    SELECT wp.pub_id
    FROM pub_keys wp
    WHERE d.mode IN ('merge_to', 'confirm_winner')
      AND (
        (d.winner_doi_key IS NOT NULL AND wp.doi_key = d.winner_doi_key)
        OR
        (
          d.winner_doi_key IS NULL
          AND
          d.winner_title_key IS NOT NULL
          AND wp.norm_title = d.winner_title_key
          AND (d.winner_year IS NULL OR wp.year = d.winner_year)
        )
      )
    ORDER BY
      CASE
        WHEN d.winner_doi_key IS NOT NULL AND wp.doi_key = d.winner_doi_key THEN 0
        ELSE 1
      END,
      wp.is_preprint,
      wp.pub_id
    LIMIT 1
  ) winner ON TRUE
  WHERE d.mode = 'keep_separate'
     OR winner.pub_id IS NOT NULL
),
durable_overrides AS (
  SELECT pub_id, decision_id, mode, winner_pub_id
  FROM durable_override_matches
  WHERE rn = 1
),
source_typed AS (
  SELECT
    p.pub_id,
    p.title,
    p.year,
    p.venue,
    p.doi,
    p.source_of_truth,
    p.is_preprint,
    COALESCE(o.mode, durable.mode) AS dedup_override_mode,
    CASE
      WHEN o.mode IS NOT NULL THEN o.winner_pub_id
      ELSE durable.winner_pub_id
    END AS dedup_override_winner_pub_id,
    durable.decision_id AS durable_dedup_decision_id,
    p.norm_title,
    CASE
      WHEN COALESCE(o.mode, durable.mode) = 'keep_separate'
        THEN p.norm_title || '#pub:' || p.pub_id::TEXT
      ELSE p.norm_title
    END AS dedup_key,
    COALESCE((p.raw_json -> 'crossref' ->> 'type') = ANY (
      ARRAY['dataset', 'posted-content', 'peer-review', 'reference-entry', 'other', 'preprint']
    ), FALSE) AS excluded_by_crossref_type,
    (
      COALESCE((p.raw_json -> 'openalex' ->> 'type') = ANY (
        ARRAY['dataset', 'preprint', 'paratext', 'peer-review', 'other', 'editorial', 'erratum']
      ), FALSE)
      AND (p.raw_json -> 'crossref' ->> 'type') IS NULL
    ) AS excluded_by_openalex_type
  FROM pub_keys p
  LEFT JOIN biblio.publication_dedup_overrides o ON o.pub_id = p.pub_id
  LEFT JOIN durable_overrides durable ON durable.pub_id = p.pub_id
),
base AS (
  SELECT *
  FROM source_typed
  WHERE NOT (
    excluded_by_crossref_type
    OR excluded_by_openalex_type
  )
),
confirmed_winners AS (
  SELECT DISTINCT ON (b.dedup_key)
    b.dedup_key,
    COALESCE(b.dedup_override_winner_pub_id, b.pub_id) AS confirmed_pub_id,
    b.durable_dedup_decision_id AS confirm_decision_id
  FROM base b
  WHERE b.dedup_override_mode = 'confirm_winner'
    AND EXISTS (
      SELECT 1
      FROM base bw
      WHERE bw.pub_id = COALESCE(b.dedup_override_winner_pub_id, b.pub_id)
    )
  ORDER BY
    b.dedup_key,
    b.durable_dedup_decision_id DESC NULLS LAST,
    b.pub_id DESC
),
dedup AS (
  SELECT
    dedup_key,
    (array_agg(
      pub_id
      ORDER BY
        is_preprint,
        CASE
          WHEN source_of_truth ILIKE '%manual%' THEN 0
          WHEN source_of_truth ILIKE '%funding%' THEN 0
          ELSE 1
        END,
        CASE
          WHEN doi ~* '(/v[0-9]+$|\.v[0-9]+$|_v[0-9]+$)' THEN 1
          ELSE 0
        END,
        pub_id
    ))[1] AS canonical_pub_id
  FROM base
  GROUP BY dedup_key
),
merge_winners AS (
  SELECT DISTINCT b.dedup_override_winner_pub_id AS pub_id
  FROM base b
  WHERE b.dedup_override_mode = 'merge_to'
    AND b.dedup_override_winner_pub_id IS NOT NULL
    AND EXISTS (
      SELECT 1 FROM base bw WHERE bw.pub_id = b.dedup_override_winner_pub_id
    )
),
canonical_map AS (
  SELECT
    b.pub_id,
    b.dedup_key,
    CASE
      WHEN cw.confirmed_pub_id IS NOT NULL THEN cw.confirmed_pub_id
      WHEN mw.pub_id IS NOT NULL THEN b.pub_id
      WHEN b.dedup_override_mode = 'merge_to'
        AND b.dedup_override_winner_pub_id IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM base bw WHERE bw.pub_id = b.dedup_override_winner_pub_id
        )
        THEN b.dedup_override_winner_pub_id
      ELSE d.canonical_pub_id
    END AS canonical_pub_id,
    cw.confirmed_pub_id,
    cw.confirm_decision_id
  FROM base b
  JOIN dedup d ON d.dedup_key = b.dedup_key
  LEFT JOIN merge_winners mw ON mw.pub_id = b.pub_id
  LEFT JOIN confirmed_winners cw ON cw.dedup_key = b.dedup_key
),
group_sizes AS (
  SELECT canonical_pub_id, count(*) AS candidate_count
  FROM canonical_map
  GROUP BY canonical_pub_id
)
SELECT
  b.pub_id,
  cm.canonical_pub_id,
  (b.pub_id = cm.canonical_pub_id) AS is_canonical,
  gs.candidate_count,
  b.doi,
  b.title,
  b.year,
  b.venue,
  b.source_of_truth,
  b.is_preprint,
  b.dedup_key,
  b.dedup_override_mode,
  b.dedup_override_winner_pub_id,
  b.durable_dedup_decision_id,
  cm.confirmed_pub_id,
  (cm.confirmed_pub_id IS NOT NULL AND cm.canonical_pub_id = cm.confirmed_pub_id) AS canonical_locked,
  cm.confirm_decision_id AS confirm_winner_decision_id
FROM base b
JOIN canonical_map cm ON cm.pub_id = b.pub_id
JOIN group_sizes gs ON gs.canonical_pub_id = cm.canonical_pub_id;

-- Older DBs may already have publications_clean/publications_canon with a
-- narrower column layout. Drop the dependent pair explicitly so the recreated
-- views can add the newer dedup/provenance columns without CREATE OR REPLACE
-- failing on an incompatible signature.
DROP MATERIALIZED VIEW IF EXISTS biblio.publications_canon_web;
DROP VIEW IF EXISTS biblio.publications_with_pub_date;
DROP VIEW IF EXISTS biblio.publications_canon;
DROP VIEW IF EXISTS biblio.publications_clean;

CREATE OR REPLACE VIEW biblio.publications_clean AS
WITH memberships AS (
  SELECT *
  FROM biblio.publication_dedup_memberships
),
auth AS (
  SELECT
    m.canonical_pub_id AS pub_id,
    a.person_id,
    a.paper_affiliation,
    a.affiliations
  FROM biblio.authorships_curated a
  JOIN memberships m ON m.pub_id = a.pub_id
  WHERE a.person_id IS NOT NULL
    AND a.is_internal_at_ingest IS TRUE
),
pubs AS (
  SELECT
    canonical_pub_id AS pub_id,
    doi,
    title,
    year,
    venue,
    source_of_truth,
    candidate_count AS dedup_candidate_count,
    canonical_locked AS dedup_canonical_locked,
    confirm_winner_decision_id AS dedup_confirm_decision_id
  FROM memberships
  WHERE is_canonical
)
SELECT
  p.pub_id,
  p.doi,
  p.title,
  p.year,
  p.venue,
  p.source_of_truth,
  p.dedup_candidate_count,
  p.dedup_canonical_locked,
  p.dedup_confirm_decision_id,
  string_agg(DISTINCT pe.display_name::TEXT, ', ' ORDER BY pe.display_name::TEXT) AS internal_authors,
  string_agg(
    DISTINCT COALESCE(a.paper_affiliation, array_to_string(a.affiliations, '; ')),
    '; '
    ORDER BY COALESCE(a.paper_affiliation, array_to_string(a.affiliations, '; '))
  ) AS internal_affiliations
FROM pubs p
LEFT JOIN auth a ON a.pub_id = p.pub_id
LEFT JOIN app.people pe ON pe.person_id = a.person_id
GROUP BY
  p.pub_id,
  p.doi,
  p.title,
  p.year,
  p.venue,
  p.source_of_truth,
  p.dedup_candidate_count,
  p.dedup_canonical_locked,
  p.dedup_confirm_decision_id;

CREATE OR REPLACE VIEW biblio.canon_scope_matches AS
WITH pub_keys AS (
  SELECT
    p.pub_id,
    NULLIF(lower(
      regexp_replace(
        regexp_replace(btrim(COALESCE(p.doi, '')), '^\s*doi:\s*', '', 'i'),
        '^\s*https?://(?:dx\.)?doi\.org/', '', 'i'
      )
    ), '') AS doi_key,
    lower(regexp_replace(btrim(p.title), '\s+', ' ', 'g')) AS title_key,
    p.year
  FROM biblio.publications p
)
SELECT
  matched.pub_id,
  matched.decision_id,
  matched.scope_type,
  matched.scope_person_id,
  matched.mode,
  matched.created_by,
  matched.created_role,
  matched.note,
  matched.updated_at
FROM (
  SELECT
    p.pub_id,
    d.decision_id,
    d.scope_type,
    d.scope_person_id,
    d.mode,
    d.created_by,
    d.created_role,
    d.note,
    d.updated_at,
    row_number() OVER (
      PARTITION BY p.pub_id, d.scope_type, COALESCE(d.scope_person_id, 0)
      ORDER BY
        CASE
          WHEN d.subject_doi_key IS NOT NULL AND p.doi_key = d.subject_doi_key THEN 0
          ELSE 1
        END,
        d.updated_at DESC,
        d.decision_id DESC
    ) AS rn
  FROM pub_keys p
  JOIN biblio.canon_scope_decisions d
    ON d.active IS TRUE
   AND (
      (d.subject_doi_key IS NOT NULL AND p.doi_key = d.subject_doi_key)
      OR
      (
        d.subject_doi_key IS NULL
        AND
        d.subject_title_key IS NOT NULL
        AND p.title_key = d.subject_title_key
        AND (d.subject_year IS NULL OR p.year = d.subject_year)
      )
    )
) matched
WHERE matched.rn = 1;

-- --------------------------------------------------------------------------
-- Institutional attribution
--
-- Which publications count as "ours" is installation-specific, so the rule is
-- data, not code: app.institution_attribution_rules holds the affiliation
-- terms and funding identifiers of the institution running this instance.
-- The table SHIPS EMPTY and, while it is empty, nothing is attributed
-- automatically (see biblio.institution_attribution_signal below).
--
-- Matching (exact contract):
--   * Evidence is the publication's stored provider payloads, i.e. the text of
--     biblio.publications.raw_json. Nothing else is consulted — in particular
--     source_of_truth is NOT evidence: tokens like 'crossref-funding' record
--     how a record was discovered, not which institution it belongs to.
--   * A rule matches when its pattern occurs as a case-insensitive SUBSTRING
--     of that payload text. 'affiliation' and 'funding' rules use identical
--     matching; the kind is descriptive and exists so operators can manage the
--     two lists separately.
--   * Patterns are compared literally: LIKE wildcards and regex metacharacters
--     carry no special meaning ('%' matches a per-cent sign).
--   * Only rows with active = true participate.
--   * A publication is attributed when AT LEAST ONE active rule matches.
--     Zero active rules therefore attribute NOTHING. Blank patterns cannot be
--     stored (CHECK constraint), so an empty setting can never match
--     everything.
--   * Patterns must be at least two characters. A very short or very common
--     pattern will over-match: prefer a distinctive acronym, the full centre
--     name, or a grant number.
--
-- Attribution is per publication row, but canon membership is per duplicate
-- GROUP: biblio.publications_canon attributes a group when any of its members
-- is attributed, so evidence found on one version of a paper is preserved for
-- the whole group.
--
-- biblio.publications.institution_attributed is a pure, recomputable cache of
-- the signal, maintained on write by a trigger. Because the signal reads a
-- configuration table, the cache goes stale when the rules change — change
-- rules only through the one operation that also recomputes the cache in the
-- same transaction (under the coordination lock described further below) and
-- then refreshes the dependent snapshots:
--
--   python -m people_pubs.sync.attribution_rules set \
--       --affiliation "Example Institute" --funding "EX-12345678"
--
-- Nothing is lost by a rule change: raw_json is never modified, so any later
-- rule can be re-derived from the untouched payloads.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.institution_attribution_rules (
  rule_id     BIGSERIAL PRIMARY KEY,
  rule_kind   TEXT        NOT NULL CHECK (rule_kind IN ('affiliation', 'funding')),
  pattern     TEXT        NOT NULL CHECK (char_length(btrim(pattern)) >= 2),
  active      BOOLEAN     NOT NULL DEFAULT TRUE,
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS institution_attribution_rules_kind_pattern_key
  ON app.institution_attribution_rules (rule_kind, lower(btrim(pattern)));

COMMENT ON TABLE app.institution_attribution_rules IS
  'Installation-specific affiliation terms and funding identifiers that mark a publication as belonging to this institution. Ships empty: with no active row, nothing is attributed automatically. Patterns are matched case-insensitively as literal substrings of biblio.publications.raw_json text.';
COMMENT ON COLUMN app.institution_attribution_rules.rule_kind IS
  'affiliation or funding. Descriptive only: both kinds match identically.';
COMMENT ON COLUMN app.institution_attribution_rules.pattern IS
  'Literal substring to look for, at least two characters. LIKE/regex metacharacters are not special.';

-- Reads the rules table, so this is STABLE, never IMMUTABLE.
CREATE OR REPLACE FUNCTION biblio.institution_attribution_signal(
  p_raw_json jsonb
) RETURNS boolean
LANGUAGE sql
STABLE
AS $func$
  SELECT EXISTS (
    SELECT 1
    FROM app.institution_attribution_rules r
    WHERE r.active
      AND btrim(r.pattern) <> ''
      AND position(lower(btrim(r.pattern)) IN lower(coalesce(p_raw_json, '{}'::jsonb)::text)) > 0
  );
$func$;

COMMENT ON FUNCTION biblio.institution_attribution_signal(jsonb) IS
  'True when at least one active row of app.institution_attribution_rules occurs as a case-insensitive literal substring of the payload text. With no active rules this is always false. biblio.publications.institution_attributed caches it.';

ALTER TABLE biblio.publications
  ADD COLUMN IF NOT EXISTS institution_attributed boolean;

COMMENT ON COLUMN biblio.publications.institution_attributed IS
  'Cached biblio.institution_attribution_signal(raw_json); maintained by trigger biblio_publications_institution_attributed_biu. Recompute with people_pubs.sync.attribution_rules after changing the rules.';

-- --------------------------------------------------------------------------
-- Coordination between rule changes and publication writers
--
-- The cached flag is computed from the rules a writer's transaction can see,
-- so a rule change that recomputes existing rows can race a concurrent
-- publication write: the writer's trigger computes its flag under the old
-- rules while the recompute cannot see the writer's uncommitted row, and
-- after both commit the cache disagrees with the committed rules. Two
-- transaction-level advisory locks on one key serialise the two sides inside
-- the database, so every write path is covered without cooperation from the
-- client (upsert_publication, the backfills and merges, COPY during a
-- restore, ad hoc psql and the attribution command alike):
--   * publication writers take the lock SHARED in the trigger below and hold
--     it until they commit; concurrent writers do not block each other;
--   * rule changes (any INSERT, UPDATE, DELETE or TRUNCATE on the rules
--     table, through the statement trigger below) and the cache recompute
--     take it EXCLUSIVE: they wait for in-flight writers to commit, and
--     writers that arrive later wait for the rule change to commit.
-- Visibility: the guarantee relies on READ COMMITTED, where every statement
-- (the trigger's own queries included) takes its snapshot after its lock
-- wait ends, so a writer released by a committed rule change evaluates the
-- new rules and the recompute sees every writer that committed before it.
-- Under REPEATABLE READ or SERIALIZABLE the transaction snapshot predates
-- the wait, so the helper refuses rather than cache a value computed from
-- rules that may already have been replaced. Every shipped writer uses the
-- default READ COMMITTED level.
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION biblio.institution_attribution_lock(p_exclusive boolean)
RETURNS void
LANGUAGE plpgsql
AS $func$
BEGIN
  IF current_setting('transaction_isolation') NOT IN ('read committed', 'read uncommitted') THEN
    RAISE EXCEPTION
      'institution attribution requires READ COMMITTED (transaction isolation is %): the cached flag would be computed from a snapshot older than a rule change it waited for',
      current_setting('transaction_isolation')
      USING ERRCODE = 'invalid_transaction_state';
  END IF;
  IF p_exclusive THEN
    PERFORM pg_advisory_xact_lock(hashtext('app.institution_attribution_rules'));
  ELSE
    PERFORM pg_advisory_xact_lock_shared(hashtext('app.institution_attribution_rules'));
  END IF;
END;
$func$;

COMMENT ON FUNCTION biblio.institution_attribution_lock(boolean) IS
  'Transaction-level advisory lock coordinating institution attribution: publication writers hold it shared (trigger), rule changes and biblio.recompute_institution_attribution() hold it exclusive. Refuses transactions that are not READ COMMITTED.';

-- Publication writers: shared lock first, then the signal. The assignment is
-- a separate statement, so under READ COMMITTED it evaluates the rules under
-- a snapshot taken after any in-progress rule change has committed.
CREATE OR REPLACE FUNCTION biblio.publications_set_institution_attributed()
RETURNS trigger
LANGUAGE plpgsql
AS $trg$
BEGIN
  PERFORM biblio.institution_attribution_lock(false);
  NEW.institution_attributed := biblio.institution_attribution_signal(NEW.raw_json);
  RETURN NEW;
END;
$trg$;

DROP TRIGGER IF EXISTS biblio_publications_institution_attributed_biu ON biblio.publications;
CREATE TRIGGER biblio_publications_institution_attributed_biu
  BEFORE INSERT OR UPDATE OF raw_json
  ON biblio.publications
  FOR EACH ROW
  EXECUTE FUNCTION biblio.publications_set_institution_attributed();

-- Rule changes: exclusive lock for the rest of the transaction, whoever makes
-- the change and however many statements it takes.
CREATE OR REPLACE FUNCTION app.institution_attribution_rules_lock()
RETURNS trigger
LANGUAGE plpgsql
AS $trg$
BEGIN
  PERFORM biblio.institution_attribution_lock(true);
  RETURN NULL;
END;
$trg$;

DROP TRIGGER IF EXISTS institution_attribution_rules_lock_bs ON app.institution_attribution_rules;
CREATE TRIGGER institution_attribution_rules_lock_bs
  BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE
  ON app.institution_attribution_rules
  FOR EACH STATEMENT
  EXECUTE FUNCTION app.institution_attribution_rules_lock();

-- Recompute the cache for rows whose stored flag no longer matches the
-- current rules, under the exclusive lock so no writer can commit a stale
-- value around it. Returns the number of rows changed. Idempotent and
-- self-healing: the attribution command calls it in the same transaction as
-- its rule change, and re-applying this patch (for example after a restore)
-- calls it below. The UPDATE does not touch raw_json, so it does not fire the
-- writer trigger.
CREATE OR REPLACE FUNCTION biblio.recompute_institution_attribution()
RETURNS bigint
LANGUAGE plpgsql
AS $func$
DECLARE
  changed bigint;
BEGIN
  PERFORM biblio.institution_attribution_lock(true);
  UPDATE biblio.publications p
  SET institution_attributed = biblio.institution_attribution_signal(p.raw_json)
  WHERE p.institution_attributed IS DISTINCT FROM biblio.institution_attribution_signal(p.raw_json);
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed;
END;
$func$;

COMMENT ON FUNCTION biblio.recompute_institution_attribution() IS
  'Re-syncs biblio.publications.institution_attributed with biblio.institution_attribution_signal(raw_json) for every row, under the exclusive attribution lock; returns the number of rows changed. Call it in the same transaction as a rule change.';

-- On a fresh install with no rules configured this touches nothing.
SELECT biblio.recompute_institution_attribution();

GRANT SELECT ON app.institution_attribution_rules TO app_readonly;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.institution_attribution_rules TO app_writer;
GRANT USAGE, SELECT ON SEQUENCE app.institution_attribution_rules_rule_id_seq TO app_writer;

-- is_provisional: normalized DOI is the only automatic identity key, so
-- DOI-less records are provisional — they stay out of canon unless a curator
-- includes them (canon scope include) or recovers a DOI, and any manually
-- included DOI-less row carries the flag downstream.
-- enqueue_provisional keeps a pending curation_review_queue entry per DOI-less
-- publication so these records surface for review instead of silently
-- disappearing.
CREATE OR REPLACE VIEW biblio.publications_canon AS
WITH scope_matches AS (
  SELECT
    pub_id,
    mode,
    decision_id
  FROM biblio.canon_scope_matches
  WHERE scope_type = 'global'
    AND scope_person_id IS NULL
)
SELECT
  pc.pub_id,
  pc.doi,
  pc.title,
  pc.year,
  pc.venue,
  pc.source_of_truth,
  pc.dedup_candidate_count,
  pc.dedup_canonical_locked,
  pc.dedup_confirm_decision_id,
  sm.mode AS canon_scope_mode,
  sm.decision_id AS canon_scope_decision_id,
  pc.internal_authors,
  pc.internal_affiliations,
  (pc.doi IS NULL) AS is_provisional
FROM biblio.publications_clean pc
JOIN biblio.publications pcanon ON pc.pub_id = pcanon.pub_id
LEFT JOIN scope_matches sm ON sm.pub_id = pc.pub_id
WHERE pcanon.is_preprint IS NOT TRUE
  AND COALESCE(sm.mode, '') <> 'exclude'
  AND (
    sm.mode = 'include'
    OR EXISTS (
      SELECT 1
      FROM biblio.publication_dedup_memberships pm
      JOIN biblio.publications p ON p.pub_id = pm.pub_id
      WHERE pm.canonical_pub_id = pc.pub_id
        -- Group-level attribution: evidence on ANY member of the duplicate
        -- group attributes the group. Reads the persisted cache maintained
        -- from biblio.institution_attribution_signal().
        AND p.institution_attributed IS TRUE
    )
  );

CREATE MATERIALIZED VIEW IF NOT EXISTS biblio.publications_canon_web AS
SELECT
  c.*,
  statement_timestamp() AS snapshot_refreshed_at
FROM biblio.publications_canon c
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS publications_canon_web_pub_id_uq
  ON biblio.publications_canon_web (pub_id);

CREATE INDEX IF NOT EXISTS publications_canon_web_doi_idx
  ON biblio.publications_canon_web (lower(doi))
  WHERE doi IS NOT NULL;

COMMENT ON MATERIALIZED VIEW biblio.publications_canon_web IS
  'Read-optimized PubDex snapshot of biblio.publications_canon; refresh outside user requests.';

-- Read-optimized snapshot of the pub_id -> canonical dedup winner mapping.
-- biblio.publication_dedup_memberships evaluates the dedup grouping over the
-- whole publications table (~6 s however it is filtered); the read layer must
-- never pay that at worker boot or snapshot rotation.
-- refresh_publications_canon_web.sql refreshes this map and
-- publications_canon_web in one transaction so the read layer can never
-- observe a newer canon snapshot paired with an older winner map.
CREATE MATERIALIZED VIEW IF NOT EXISTS biblio.publication_dedup_map_web AS
SELECT
  m.pub_id,
  m.canonical_pub_id,
  statement_timestamp() AS snapshot_refreshed_at
FROM biblio.publication_dedup_memberships m
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS publication_dedup_map_web_pub_id_uq
  ON biblio.publication_dedup_map_web (pub_id);

COMMENT ON MATERIALIZED VIEW biblio.publication_dedup_map_web IS
  'Read-optimized PubDex snapshot of biblio.publication_dedup_memberships (pub_id -> canonical winner); refresh alongside publications_canon_web.';

-- Publication list with normalized publication date fields (year/month/day precision).
CREATE OR REPLACE VIEW biblio.publications_with_pub_date AS
WITH clean AS MATERIALIZED (
    SELECT *
    FROM biblio.publications_clean
),
raw_parts AS MATERIALIZED (
    SELECT
        p.pub_id,
        clean.year::int AS pub_year,

        COALESCE(
            CASE WHEN (p.raw_json #>> '{crossref,issued,date-parts,0,1}') ~ '^\d+$'
                THEN (p.raw_json #>> '{crossref,issued,date-parts,0,1}')::int END,
            CASE WHEN (p.raw_json #>> '{crossref,published,date-parts,0,1}') ~ '^\d+$'
                THEN (p.raw_json #>> '{crossref,published,date-parts,0,1}')::int END,
            CASE WHEN (p.raw_json #>> '{crossref,published-print,date-parts,0,1}') ~ '^\d+$'
                THEN (p.raw_json #>> '{crossref,published-print,date-parts,0,1}')::int END,
            CASE WHEN (p.raw_json #>> '{orcid,publication-date,month,value}') ~ '^\d+$'
                THEN (p.raw_json #>> '{orcid,publication-date,month,value}')::int END,
            CASE WHEN (p.raw_json #>> '{openalex,publication_date}') ~ '^\d{4}-\d{2}-\d{2}$'
                THEN EXTRACT(MONTH FROM (p.raw_json #>> '{openalex,publication_date}')::date)::int END
        ) AS raw_month,

        COALESCE(
            CASE WHEN (p.raw_json #>> '{crossref,issued,date-parts,0,2}') ~ '^\d+$'
                THEN (p.raw_json #>> '{crossref,issued,date-parts,0,2}')::int END,
            CASE WHEN (p.raw_json #>> '{crossref,published,date-parts,0,2}') ~ '^\d+$'
                THEN (p.raw_json #>> '{crossref,published,date-parts,0,2}')::int END,
            CASE WHEN (p.raw_json #>> '{crossref,published-print,date-parts,0,2}') ~ '^\d+$'
                THEN (p.raw_json #>> '{crossref,published-print,date-parts,0,2}')::int END,
            CASE WHEN (p.raw_json #>> '{orcid,publication-date,day,value}') ~ '^\d+$'
                THEN (p.raw_json #>> '{orcid,publication-date,day,value}')::int END,
            CASE WHEN (p.raw_json #>> '{openalex,publication_date}') ~ '^\d{4}-\d{2}-\d{2}$'
                THEN EXTRACT(DAY FROM (p.raw_json #>> '{openalex,publication_date}')::date)::int END
        ) AS raw_day
    FROM biblio.publications p
    JOIN clean
      ON clean.pub_id = p.pub_id
    WHERE clean.year IS NOT NULL
),
normalized AS MATERIALIZED (
    SELECT
        pub_id,
        pub_year,
        LEAST(GREATEST(COALESCE(raw_month, 1), 1), 12) AS m,
        raw_month,
        raw_day,
        EXTRACT(
            DAY FROM (
                make_date(pub_year, LEAST(GREATEST(COALESCE(raw_month, 1), 1), 12), 1)
                + interval '1 month - 1 day'
            )
        )::int AS last_dom
    FROM raw_parts
),
pub_dates AS MATERIALIZED (
    SELECT
        n.pub_id,
        d.day_value AS pub_day,
        n.m AS pub_month,
        make_date(n.pub_year, n.m, d.day_value) AS pub_date,
        CASE
            WHEN n.raw_day IS NOT NULL THEN 'day'
            WHEN n.raw_month IS NOT NULL THEN 'month'
            ELSE 'year'
        END AS pub_date_precision
    FROM normalized n
    CROSS JOIN LATERAL (
        SELECT LEAST(GREATEST(COALESCE(n.raw_day, 1), 1), n.last_dom) AS day_value
    ) d
)
SELECT
    clean.*,
    pd.pub_date,
    pd.pub_month,
    pd.pub_day,
    pd.pub_date_precision
FROM clean
LEFT JOIN pub_dates pd
  ON pd.pub_id = clean.pub_id;

-- Narrow PII write surfaces for routine ingestion. These functions are
-- SECURITY DEFINER so app_writer does not need direct DML grants on pii tables.
CREATE OR REPLACE FUNCTION pii.merge_people_verified_emails(
  p_person_id BIGINT,
  p_verified_emails CITEXT[]
)
RETURNS TABLE(row_found BOOLEAN, primary_email CITEXT, emails CITEXT[])
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pii, app, biblio, public
AS $$
DECLARE
  v_existing_primary CITEXT;
  v_existing_emails CITEXT[];
  v_merged CITEXT[];
  v_primary CITEXT;
BEGIN
  SELECT pp.primary_email, pp.emails
    INTO v_existing_primary, v_existing_emails
  FROM pii.people_pii pp
  WHERE pp.person_id = p_person_id
  FOR UPDATE;

  IF NOT FOUND THEN
    row_found := FALSE;
    primary_email := NULL;
    emails := NULL;
    RETURN NEXT;
    RETURN;
  END IF;

  WITH raw(email, ord) AS (
    SELECT e, ord
    FROM unnest(COALESCE(v_existing_emails, '{}'::CITEXT[])) WITH ORDINALITY AS x(e, ord)
    UNION ALL
    SELECT e, ord + 100000
    FROM unnest(COALESCE(p_verified_emails, '{}'::CITEXT[])) WITH ORDINALITY AS x(e, ord)
  ),
  clean AS (
    SELECT btrim(email::TEXT)::CITEXT AS email, ord
    FROM raw
    WHERE email IS NOT NULL
      AND btrim(email::TEXT) <> ''
      AND lower(btrim(email::TEXT)) NOT IN ('emails', 'email', 'none', 'n/a', '[]')
  ),
  dedup AS (
    SELECT DISTINCT ON (lower(email::TEXT)) email, ord
    FROM clean
    ORDER BY lower(email::TEXT), ord
  )
  SELECT COALESCE(array_agg(email ORDER BY ord), '{}'::CITEXT[])
    INTO v_merged
  FROM dedup;

  IF v_existing_primary IS NOT NULL
     AND btrim(v_existing_primary::TEXT) <> ''
     AND lower(btrim(v_existing_primary::TEXT)) NOT IN ('primary_email', 'email', 'none', 'n/a') THEN
    v_primary := v_existing_primary;
  ELSE
    -- Domain-neutral: no organisation, domain or top-level domain is
    -- preferred. The array was built in merged input order, and unnest()
    -- alone does not promise to preserve it, so carry the ordinality.
    SELECT e
      INTO v_primary
    FROM unnest(v_merged) WITH ORDINALITY AS x(e, ord)
    ORDER BY ord
    LIMIT 1;
  END IF;

  UPDATE pii.people_pii pp
  SET emails = v_merged,
      primary_email = v_primary,
      updated_at = NOW()
  WHERE pp.person_id = p_person_id;

  row_found := TRUE;
  primary_email := v_primary;
  emails := v_merged;
  RETURN NEXT;
END
$$;

CREATE OR REPLACE FUNCTION pii.ensure_people_pii(
  p_person_id BIGINT,
  p_primary_email CITEXT,
  p_emails CITEXT[],
  p_update_existing BOOLEAN DEFAULT TRUE
)
RETURNS TABLE(action TEXT, primary_email CITEXT, emails CITEXT[])
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pii, app, biblio, public
AS $$
DECLARE
  v_existing_primary CITEXT;
  v_existing_emails CITEXT[];
  v_incoming CITEXT[];
  v_merged CITEXT[];
  v_primary CITEXT;
BEGIN
  WITH raw(email, ord) AS (
    SELECT e, ord
    FROM unnest(COALESCE(p_emails, '{}'::CITEXT[])) WITH ORDINALITY AS x(e, ord)
    UNION ALL
    SELECT p_primary_email, 99999
    WHERE p_primary_email IS NOT NULL
  ),
  clean AS (
    SELECT btrim(email::TEXT)::CITEXT AS email, ord
    FROM raw
    WHERE email IS NOT NULL
      AND btrim(email::TEXT) <> ''
      AND lower(btrim(email::TEXT)) NOT IN ('emails', 'email', 'none', 'n/a', '[]')
  ),
  dedup AS (
    SELECT DISTINCT ON (lower(email::TEXT)) email, ord
    FROM clean
    ORDER BY lower(email::TEXT), ord
  )
  SELECT COALESCE(array_agg(email ORDER BY ord), '{}'::CITEXT[])
    INTO v_incoming
  FROM dedup;

  SELECT pp.primary_email, pp.emails
    INTO v_existing_primary, v_existing_emails
  FROM pii.people_pii pp
  WHERE pp.person_id = p_person_id
  FOR UPDATE;

  IF NOT FOUND THEN
    IF p_primary_email IS NOT NULL
       AND btrim(p_primary_email::TEXT) <> ''
       AND lower(btrim(p_primary_email::TEXT)) NOT IN ('primary_email', 'email', 'none', 'n/a') THEN
      v_primary := p_primary_email;
    ELSE
      -- Domain-neutral: no organisation, domain or top-level domain is
      -- preferred. The array was built in input order, and unnest() alone
      -- does not promise to preserve it, so carry the ordinality.
      SELECT e
        INTO v_primary
      FROM unnest(v_incoming) WITH ORDINALITY AS x(e, ord)
      ORDER BY ord
      LIMIT 1;
    END IF;

    INSERT INTO pii.people_pii (person_id, primary_email, emails)
    VALUES (p_person_id, v_primary, v_incoming);

    action := 'inserted';
    primary_email := v_primary;
    emails := v_incoming;
    RETURN NEXT;
    RETURN;
  END IF;

  IF NOT p_update_existing THEN
    action := 'unchanged';
    primary_email := v_existing_primary;
    emails := v_existing_emails;
    RETURN NEXT;
    RETURN;
  END IF;

  WITH raw(email, ord) AS (
    SELECT e, ord
    FROM unnest(COALESCE(v_existing_emails, '{}'::CITEXT[])) WITH ORDINALITY AS x(e, ord)
    UNION ALL
    SELECT e, ord + 100000
    FROM unnest(COALESCE(v_incoming, '{}'::CITEXT[])) WITH ORDINALITY AS x(e, ord)
  ),
  clean AS (
    SELECT btrim(email::TEXT)::CITEXT AS email, ord
    FROM raw
    WHERE email IS NOT NULL
      AND btrim(email::TEXT) <> ''
      AND lower(btrim(email::TEXT)) NOT IN ('emails', 'email', 'none', 'n/a', '[]')
  ),
  dedup AS (
    SELECT DISTINCT ON (lower(email::TEXT)) email, ord
    FROM clean
    ORDER BY lower(email::TEXT), ord
  )
  SELECT COALESCE(array_agg(email ORDER BY ord), '{}'::CITEXT[])
    INTO v_merged
  FROM dedup;

  IF v_existing_primary IS NOT NULL
     AND btrim(v_existing_primary::TEXT) <> ''
     AND lower(btrim(v_existing_primary::TEXT)) NOT IN ('primary_email', 'email', 'none', 'n/a') THEN
    v_primary := v_existing_primary;
  ELSIF p_primary_email IS NOT NULL
        AND btrim(p_primary_email::TEXT) <> ''
        AND lower(btrim(p_primary_email::TEXT)) NOT IN ('primary_email', 'email', 'none', 'n/a') THEN
    v_primary := p_primary_email;
  ELSE
    -- Domain-neutral: no organisation, domain or top-level domain is
    -- preferred. The array was built in merged input order, and unnest()
    -- alone does not promise to preserve it, so carry the ordinality.
    SELECT e
      INTO v_primary
    FROM unnest(v_merged) WITH ORDINALITY AS x(e, ord)
    ORDER BY ord
    LIMIT 1;
  END IF;

  UPDATE pii.people_pii pp
  SET emails = v_merged,
      primary_email = v_primary,
      updated_at = NOW()
  WHERE pp.person_id = p_person_id;

  action := 'updated';
  primary_email := v_primary;
  emails := v_merged;
  RETURN NEXT;
END
$$;

CREATE OR REPLACE FUNCTION pii.upsert_publication_author_email(
  p_pub_id BIGINT,
  p_person_id BIGINT,
  p_email CITEXT,
  p_is_corresponding BOOLEAN DEFAULT FALSE
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pii, app, biblio, public
AS $$
DECLARE
  v_inserted INT;
BEGIN
  IF p_pub_id IS NULL OR p_person_id IS NULL OR p_email IS NULL OR btrim(p_email::TEXT) = '' THEN
    RETURN FALSE;
  END IF;

  INSERT INTO pii.publication_author_emails (pub_id, person_id, email, is_corresponding)
  VALUES (p_pub_id, p_person_id, btrim(p_email::TEXT)::CITEXT, COALESCE(p_is_corresponding, FALSE))
  ON CONFLICT (pub_id, person_id, email) DO NOTHING;

  GET DIAGNOSTICS v_inserted = ROW_COUNT;
  RETURN v_inserted > 0;
END
$$;

CREATE OR REPLACE FUNCTION pii.move_publication_author_emails(
  p_winner_pub_id BIGINT,
  p_loser_pub_id BIGINT
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pii, app, biblio, public
AS $$
DECLARE
  v_inserted INT;
BEGIN
  IF p_winner_pub_id IS NULL OR p_loser_pub_id IS NULL OR p_winner_pub_id = p_loser_pub_id THEN
    RETURN 0;
  END IF;

  INSERT INTO pii.publication_author_emails (pub_id, person_id, email, is_corresponding)
  SELECT p_winner_pub_id, e.person_id, e.email, e.is_corresponding
  FROM pii.publication_author_emails e
  WHERE e.pub_id = p_loser_pub_id
  ON CONFLICT DO NOTHING;

  GET DIAGNOSTICS v_inserted = ROW_COUNT;

  DELETE FROM pii.publication_author_emails
  WHERE pub_id = p_loser_pub_id;

  RETURN v_inserted;
END
$$;

REVOKE ALL ON FUNCTION pii.merge_people_verified_emails(BIGINT, CITEXT[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION pii.ensure_people_pii(BIGINT, CITEXT, CITEXT[], BOOLEAN) FROM PUBLIC;
REVOKE ALL ON FUNCTION pii.upsert_publication_author_email(BIGINT, BIGINT, CITEXT, BOOLEAN) FROM PUBLIC;
REVOKE ALL ON FUNCTION pii.move_publication_author_emails(BIGINT, BIGINT) FROM PUBLIC;

GRANT USAGE ON SCHEMA app, biblio, activity TO app_readonly, app_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA app, biblio, activity TO app_readonly;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app, biblio, activity TO app_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app, biblio, activity TO app_writer;

-- Scheduled ingestion intentionally touches only these PII surfaces:
-- ORCID profile refresh merges verified emails into people_pii, and publication
-- refreshes can store per-publication author emails when a person is matched.
GRANT USAGE ON SCHEMA pii TO app_writer;
REVOKE SELECT, INSERT, UPDATE, DELETE ON pii.people_pii FROM app_writer;
REVOKE SELECT, INSERT, UPDATE, DELETE ON pii.publication_author_emails FROM app_writer;
GRANT EXECUTE ON FUNCTION pii.merge_people_verified_emails(BIGINT, CITEXT[]) TO app_writer;
GRANT EXECUTE ON FUNCTION pii.ensure_people_pii(BIGINT, CITEXT, CITEXT[], BOOLEAN) TO app_writer;
GRANT EXECUTE ON FUNCTION pii.upsert_publication_author_email(BIGINT, BIGINT, CITEXT, BOOLEAN) TO app_writer;
GRANT EXECUTE ON FUNCTION pii.move_publication_author_emails(BIGINT, BIGINT) TO app_writer;

-- =========================================================================
-- Member introductions (semi-automatic introduction workflow, 2026-07-17)
-- =========================================================================
-- Curator-queued introduction suggestions between two internal researchers
-- who have never co-authored. The queue row is PII-free: person ids,
-- workflow status, and a
-- public-safe evidence snapshot. Contact addresses are reachable only via
-- the pair-narrow SECURITY DEFINER reader below; sent-mail bookkeeping goes
-- through the logger, which returns ids only. Both are EXECUTE-granted to
-- app_writer alone.

CREATE TABLE IF NOT EXISTS app.member_introductions (
  intro_id        BIGSERIAL PRIMARY KEY,
  person_a        BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  person_b        BIGINT NOT NULL REFERENCES app.people(person_id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'packaged', 'drafted', 'sent', 'dismissed')),
  since_year      INT,
  evidence_json   JSONB,
  requested_by    TEXT NOT NULL,
  requested_role  TEXT,
  note            TEXT,
  status_history  JSONB NOT NULL DEFAULT '[]'::jsonb,
  packaged_at     TIMESTAMPTZ,
  drafted_at      TIMESTAMPTZ,
  sent_at         TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT member_introductions_pair_order CHECK (person_a < person_b),
  CONSTRAINT member_introductions_pair_unique UNIQUE (person_a, person_b)
);

CREATE INDEX IF NOT EXISTS idx_member_introductions_status
  ON app.member_introductions (status, updated_at DESC);

-- First (and deliberately pair-narrow) PII reader: resolves the two contact
-- addresses needed to assemble one introduction email. Per person it emits a
-- reason code and yields the address only on 'ok'; a consent opt-out
-- (consent_flags->>'introductions' = 'false', boolean or string) blocks the
-- address entirely.
CREATE OR REPLACE FUNCTION pii.get_introduction_contacts(
  p_person_a BIGINT,
  p_person_b BIGINT
)
RETURNS TABLE(person_id BIGINT, primary_email CITEXT, reason TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pii, app, biblio, public
AS $$
DECLARE
  v_pid BIGINT;
  v_kind TEXT;
  v_email CITEXT;
  v_opt TEXT;
BEGIN
  IF p_person_a IS NULL OR p_person_b IS NULL OR p_person_a = p_person_b THEN
    RAISE EXCEPTION 'get_introduction_contacts needs two distinct person ids';
  END IF;

  FOREACH v_pid IN ARRAY ARRAY[p_person_a, p_person_b] LOOP
    person_id := v_pid;
    primary_email := NULL;

    SELECT p.person_kind::TEXT INTO v_kind
    FROM app.people p
    WHERE p.person_id = v_pid;
    IF NOT FOUND THEN
      reason := 'not_found';
      RETURN NEXT;
      CONTINUE;
    END IF;
    IF v_kind <> 'internal' THEN
      reason := 'not_internal';
      RETURN NEXT;
      CONTINUE;
    END IF;

    SELECT pp.primary_email, pp.consent_flags ->> 'introductions'
      INTO v_email, v_opt
    FROM pii.people_pii pp
    WHERE pp.person_id = v_pid;
    IF NOT FOUND THEN
      reason := 'no_pii_row';
      RETURN NEXT;
      CONTINUE;
    END IF;
    IF COALESCE(v_opt, 'true') = 'false' THEN
      reason := 'opted_out';
      RETURN NEXT;
      CONTINUE;
    END IF;
    IF v_email IS NULL OR btrim(v_email::TEXT) = '' THEN
      reason := 'no_email';
      RETURN NEXT;
      CONTINUE;
    END IF;

    primary_email := v_email;
    reason := 'ok';
    RETURN NEXT;
  END LOOP;
END
$$;

-- Bookkeeping writer for introductions the operator has actually sent:
-- inserts one pii.email_log row per member that has a stored primary email
-- and returns ids only, so callers never hold an address. Consent is deliberately not re-checked here — it gates assembly;
-- refusing to log an already-sent email would lose the audit trail.
CREATE OR REPLACE FUNCTION pii.log_introduction_email(
  p_intro_id BIGINT,
  p_subject TEXT,
  p_payload JSONB DEFAULT NULL
)
RETURNS TABLE(email_id BIGINT, person_id BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pii, app, biblio, public
AS $$
DECLARE
  v_a BIGINT;
  v_b BIGINT;
BEGIN
  IF p_intro_id IS NULL THEN
    RAISE EXCEPTION 'log_introduction_email needs an introduction id';
  END IF;
  IF p_subject IS NULL OR btrim(p_subject) = '' THEN
    RAISE EXCEPTION 'log_introduction_email needs a subject';
  END IF;

  SELECT mi.person_a, mi.person_b
    INTO v_a, v_b
  FROM app.member_introductions mi
  WHERE mi.intro_id = p_intro_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'unknown introduction id %', p_intro_id;
  END IF;

  RETURN QUERY
  INSERT INTO pii.email_log AS el (person_id, to_address, subject, status, payload_json)
  SELECT pp.person_id, pp.primary_email, p_subject, 'sent',
         COALESCE(p_payload, '{}'::jsonb)
           || jsonb_build_object('intro_id', p_intro_id, 'kind', 'member_introduction')
  FROM pii.people_pii pp
  WHERE pp.person_id IN (v_a, v_b)
    AND pp.primary_email IS NOT NULL
    AND btrim(pp.primary_email::TEXT) <> ''
  RETURNING el.email_id, el.person_id;
END
$$;

REVOKE ALL ON FUNCTION pii.get_introduction_contacts(BIGINT, BIGINT) FROM PUBLIC;
REVOKE ALL ON FUNCTION pii.log_introduction_email(BIGINT, TEXT, JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pii.get_introduction_contacts(BIGINT, BIGINT) TO app_writer;
GRANT EXECUTE ON FUNCTION pii.log_introduction_email(BIGINT, TEXT, JSONB) TO app_writer;

-- The queue table is PII-free; explicit grants because the bulk schema-wide
-- grants earlier in this file run before this section on a full re-run.
GRANT SELECT, INSERT, UPDATE, DELETE ON app.member_introductions TO app_writer;
GRANT USAGE, SELECT ON SEQUENCE app.member_introductions_intro_id_seq TO app_writer;
GRANT SELECT ON app.member_introductions TO app_readonly;

-- ---------------------------------------------------------------------------
-- Attribution backfill attempts (2026-07-28)
--
-- The attribution backfill re-fetches provider payloads for publications that
-- carry an internal author but no institutional attribution signal, selecting rows by
-- *which payload is missing* (see people_pubs/sync/backfill_all.py
-- --only-unattributed-missing-payload).
--
-- Without a memory of what was already tried, that selector never advances: a
-- publication the provider simply does not hold stays "missing" forever and
-- is re-served on every run. This table records each attempt so the selector
-- can skip recently-tried rows, and so "provider has no record" becomes a
-- measurable fact rather than a silent retry loop.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS activity.attribution_backfill_attempts (
  pub_id            BIGINT PRIMARY KEY
                      REFERENCES biblio.publications(pub_id) ON DELETE CASCADE,
  attempt_count     INTEGER     NOT NULL DEFAULT 0,
  first_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_outcome      TEXT        NOT NULL DEFAULT 'unknown',
  missing_before    TEXT[],
  missing_after     TEXT[],
  became_attributed BOOLEAN     NOT NULL DEFAULT FALSE,
  CONSTRAINT attribution_backfill_attempts_outcome_chk
    CHECK (last_outcome IN ('enriched', 'no_new_payload', 'error', 'unknown'))
);

CREATE INDEX IF NOT EXISTS idx_attribution_backfill_attempts_last
  ON activity.attribution_backfill_attempts (last_attempt_at);
CREATE INDEX IF NOT EXISTS idx_attribution_backfill_attempts_outcome
  ON activity.attribution_backfill_attempts (last_outcome, last_attempt_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON activity.attribution_backfill_attempts TO app_writer;
GRANT SELECT ON activity.attribution_backfill_attempts TO app_readonly;
