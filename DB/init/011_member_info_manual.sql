-- Curated member information returned by colleagues on the member-info
-- spreadsheet, imported as upserts that fill this table. Idempotent, so it
-- can be applied to older databases after restore.
--
-- Deliberately holds no contact PII: emails from the same sheet go to
-- pii.people_pii through pii.ensure_people_pii(), never into an app.* column.
-- Keyed by ORCID rather than person_id so curation replays across resets that
-- renumber people, the same durability rule the biblio curation tables follow.

CREATE TABLE IF NOT EXISTS app.member_info_manual (
    orcid          text PRIMARY KEY,
    person_id      bigint NOT NULL,
    status         text,
    role           text,
    department_1   text,
    department_2   text,
    supervisor_1   text,
    supervisor_2   text,
    research_group text,
    research_area  text,
    start_year     integer,
    end_year       integer,
    notes          text,
    source         text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_member_info_manual_person
    ON app.member_info_manual (person_id);

GRANT SELECT ON app.member_info_manual TO app_readonly;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.member_info_manual TO app_writer;
