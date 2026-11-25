-- Adds a synthetic table with its sequence, and a nullable column to biblio.publications.
-- backward-compatible: yes
--
-- A template of the test-only overlay, never part of a PubDex release.
-- release.sh build --test <name> --overlay b1|b2 copies the checkout into a
-- temporary directory and adds this file there as the schema change after
-- the copy's last one, for test releases B1 and B2. It only adds: the
-- preceding release keeps reading and writing every table it knows, one of
-- which gains a column it does not know, and the new table is one it never
-- uses. The default privileges of the schema give the new table to the
-- application roles; a sequence needs its grant.

CREATE SEQUENCE biblio.test_overlay_notes_note_id_seq;

CREATE TABLE biblio.test_overlay_notes (
  note_id bigint PRIMARY KEY DEFAULT nextval('biblio.test_overlay_notes_note_id_seq'),
  pub_id bigint,
  note text NOT NULL
);

ALTER SEQUENCE biblio.test_overlay_notes_note_id_seq OWNED BY biblio.test_overlay_notes.note_id;

GRANT USAGE, SELECT ON SEQUENCE biblio.test_overlay_notes_note_id_seq TO app_writer;

ALTER TABLE biblio.publications ADD COLUMN test_overlay_remark text;
