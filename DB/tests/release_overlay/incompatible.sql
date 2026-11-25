-- Renames the title column of the read service's serving snapshot.
-- backward-compatible: no
--
-- A template of the test-only overlay, never part of a PubDex release, for
-- test release B2 alone: release.sh build --test <name> --overlay b2 adds it
-- after compatible.sql. A's real readiness and publication queries require
-- title. B2's temporary application overlay reads test_overlay_title AS title
-- instead, keeping the response interface while changing its SQL dependency.

ALTER MATERIALIZED VIEW biblio.publications_canon_web RENAME COLUMN title TO test_overlay_title;
