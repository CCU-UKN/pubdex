-- ============================================================================
-- Person canon-coverage check (pgAdmin / inspection version)
--
-- For one person: window = [first, last] publication date of their canon
-- (attribution-scoped) publications; lists every deduped publication they authored
-- and classifies why each one is or is not in biblio.publications_canon.
--
-- Statuses:
--   in_canon               counted in the canonical list
--   excluded_manually      global canon exclude decision (canon_scope_decisions)
--   preprint_only          dedup winner is a preprint (no published version yet)
--   missing_institution_signal
--                          eligible, but no configured affiliation/funding
--                          evidence and no manual include decision
--                          -> review candidates
--   outside_dedup_universe source type marks it dataset/peer-review/posted-
--                          content/erratum etc.; never enters clean/canon
--
-- Uses biblio.publications_canon_web (materialized snapshot -> fast, data as
-- of the last snapshot refresh).  Runtime ~15-20 s: one evaluation each of
-- publication_dedup_memberships and publications_with_pub_date.
-- ============================================================================
WITH params AS (
    SELECT 122::bigint AS person_id
),
memberships AS MATERIALIZED (
    SELECT pub_id, canonical_pub_id, is_canonical, is_preprint, candidate_count
    FROM biblio.publication_dedup_memberships
),
person_pub_rows AS (              -- every pub row the person is linked to
    SELECT DISTINCT a.pub_id
    FROM params prm
    JOIN biblio.authorships_curated a ON a.person_id = prm.person_id
),
winners AS (                      -- collapse duplicate versions to the winner
    SELECT DISTINCT m.canonical_pub_id AS pub_id
    FROM person_pub_rows pp
    JOIN memberships m ON m.pub_id = pp.pub_id
),
dated AS MATERIALIZED (           -- winner metadata + derived publication date
    SELECT pwd.pub_id, pwd.doi, pwd.title, pwd.year, pwd.venue,
           pwd.source_of_truth, pwd.dedup_candidate_count,
           pwd.pub_date, pwd.pub_date_precision
    FROM biblio.publications_with_pub_date pwd
    JOIN winners w ON w.pub_id = pwd.pub_id
),
scope AS (                        -- manual canon include/exclude decisions
    SELECT pub_id, mode, note
    FROM biblio.canon_scope_matches
    WHERE scope_type = 'global' AND scope_person_id IS NULL
),
classified AS (
    SELECT d.*,
           (cw.pub_id IS NOT NULL) AS is_canon,
           CASE
               WHEN cw.pub_id IS NOT NULL THEN 'in_canon'
               WHEN s.mode = 'exclude'    THEN 'excluded_manually'
               WHEN m.is_preprint         THEN 'preprint_only'
               ELSE 'missing_institution_signal'
           END AS status,
           s.note AS scope_note
    FROM dated d
    JOIN memberships m ON m.pub_id = d.pub_id AND m.is_canonical
    LEFT JOIN biblio.publications_canon_web cw ON cw.pub_id = d.pub_id
    LEFT JOIN scope s ON s.pub_id = d.pub_id
    UNION ALL                     -- authored pubs outside the dedup universe
    SELECT p.pub_id, p.doi, p.title, p.year, p.venue, p.source_of_truth,
           NULL, NULL, NULL,
           false, 'outside_dedup_universe',
           coalesce(p.raw_json -> 'crossref' ->> 'type',
                    p.raw_json -> 'openalex' ->> 'type')
    FROM person_pub_rows pp
    JOIN biblio.publications p ON p.pub_id = pp.pub_id
    WHERE NOT EXISTS (SELECT 1 FROM memberships m WHERE m.pub_id = pp.pub_id)
),
win AS (
    SELECT min(pub_date) AS first_canon_date,
           max(pub_date) AS last_canon_date
    FROM classified
    WHERE is_canon
)
SELECT cl.status,
       CASE WHEN cl.pub_date IS NULL THEN 'undated'
            WHEN cl.pub_date BETWEEN w.first_canon_date AND w.last_canon_date
                 THEN 'in_window'
            ELSE 'outside_window'
       END AS window_position,
       cl.pub_id, cl.title, cl.year, cl.pub_date, cl.pub_date_precision,
       cl.venue, cl.doi, cl.source_of_truth, cl.dedup_candidate_count,
       cl.scope_note, w.first_canon_date, w.last_canon_date
FROM classified cl
CROSS JOIN win w
-- To reproduce the original "between first and last" list, uncomment:
-- WHERE cl.pub_date BETWEEN w.first_canon_date AND w.last_canon_date
ORDER BY cl.status = 'in_canon', cl.pub_date NULLS LAST, cl.pub_id;
