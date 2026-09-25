"""Disposable-DB coverage for configurable institutional attribution.

Pins the contract of `app.institution_attribution_rules`,
`biblio.institution_attribution_signal()` and the cached
`biblio.publications.institution_attributed` column:

- with no rules configured, nothing is attributed and canon stays empty of
  automatically-included rows, while ingestion keeps storing publications;
- a configured term attributes only payloads that actually contain it;
- a misleading `source_of_truth` label such as `crossref-funding` is NOT
  evidence on its own;
- attribution on one member of a duplicate group covers the whole group;
- curator include/exclude decisions keep their precedence, including the
  preprint restriction;
- changing the rules after publications exist recomputes the cached flags and,
  after a snapshot refresh, changes canon membership;
- the command's transaction boundaries: --dry-run touches neither rules, flags
  nor snapshots; a failed refresh leaves the committed rules and flags in place
  and rolls both snapshots back together; recompute re-syncs flags without
  changing rules; --refresh-only publishes both snapshots on retry.

Concurrent writers are covered in test_attribution_concurrency_integration.py.
Synthetic fixture data and a fictional institution only.
"""
from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.db.publications import upsert_publication

pytestmark = pytest.mark.integration

INSTITUTE = "Institute for Example Studies"
INSTITUTE_ACRONYM = "IFES"
AWARD = "EX-12345678"


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)


def _set_rules(conn: psycopg.Connection, rules: list[tuple[str, str]]) -> None:
    """Replace the whole rule set and recompute the cached flags, atomically."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.institution_attribution_rules")
            for kind, pattern in rules:
                cur.execute(
                    "INSERT INTO app.institution_attribution_rules (rule_kind, pattern) VALUES (%s, %s)",
                    (kind, pattern),
                )
            cur.execute("SELECT biblio.recompute_institution_attribution()")


def _attributed(conn: psycopg.Connection, doi: str) -> bool | None:
    with conn.cursor() as cur:
        cur.execute("SELECT institution_attributed FROM biblio.publications WHERE doi = %s", (doi,))
        row = cur.fetchone()
        return None if row is None else row["institution_attributed"]


def _in_canon(conn: psycopg.Connection, doi: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM biblio.publications_canon WHERE doi = %s", (doi,))
        return cur.fetchone() is not None


def _canon_titles(conn: psycopg.Connection, title: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM biblio.publications_canon WHERE title = %s", (title,))
        return int(cur.fetchone()["n"])


def _cleanup(dois: list[str]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app.institution_attribution_rules")
        cur.execute(
            "DELETE FROM biblio.canon_scope_decisions WHERE subject_doi_key = ANY(%s)", (dois,)
        )
        cur.execute(
            "DELETE FROM biblio.authorships WHERE pub_id IN "
            "(SELECT pub_id FROM biblio.publications WHERE doi = ANY(%s))",
            (dois,),
        )
        cur.execute("DELETE FROM biblio.publications WHERE doi = ANY(%s)", (dois,))
        cur.execute("SELECT biblio.recompute_institution_attribution()")


def _seed(conn: psycopg.Connection, doi: str, title: str, payload: dict, **kw) -> int:
    return upsert_publication(
        conn,
        {
            "doi": doi,
            "title": title,
            "year": 2024,
            "venue": "Journal of Synthetic Metadata",
            "raw_crossref_json": payload,
            "is_preprint": False,
            **kw,
        },
    )


def test_unconfigured_stores_publications_but_attributes_nothing() -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/attr-none-{slug}"
    try:
        with _connect() as conn:
            _set_rules(conn, [])
            _seed(conn, doi, f"unconfigured fixture {slug}",
                  {"funder": [{"name": INSTITUTE}], "author": [{"affiliation": [{"name": INSTITUTE}]}]})

            # Ingestion is unaffected: the row is stored.
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM biblio.publications WHERE doi = %s", (doi,))
                assert cur.fetchone(), "ingestion must still store publications when unconfigured"

            # But nothing is attributed, so nothing enters canon automatically.
            assert _attributed(conn, doi) is False
            assert not _in_canon(conn, doi), (
                "with no rules configured there must be no automatic institutional inclusion"
            )
    finally:
        _cleanup([doi])


def test_configured_term_matches_only_payloads_that_contain_it() -> None:
    slug = uuid4().hex[:12]
    hit = f"10.5555/attr-hit-{slug}"
    miss = f"10.5555/attr-miss-{slug}"
    try:
        with _connect() as conn:
            _seed(conn, hit, f"attributed fixture {slug}",
                  {"author": [{"affiliation": [{"name": f"{INSTITUTE} ({INSTITUTE_ACRONYM})"}]}]})
            _seed(conn, miss, f"unrelated fixture {slug}",
                  {"author": [{"affiliation": [{"name": "Unrelated Department, Elsewhere"}]}]})

            _set_rules(conn, [("affiliation", INSTITUTE)])
            assert _attributed(conn, hit) is True
            assert _attributed(conn, miss) is False
            assert _in_canon(conn, hit)
            assert not _in_canon(conn, miss)

            # Case-insensitive substring, and funding identifiers match the
            # same way as affiliation terms.
            _set_rules(conn, [("affiliation", INSTITUTE.lower())])
            assert _attributed(conn, hit) is True

            _set_rules(conn, [("funding", AWARD)])
            assert _attributed(conn, hit) is False, "a term that is absent must not match"
    finally:
        _cleanup([hit, miss])


def test_source_of_truth_label_is_not_attribution_evidence() -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/attr-label-{slug}"
    try:
        with _connect() as conn:
            _set_rules(conn, [("affiliation", INSTITUTE), ("funding", AWARD)])
            # Provenance says the row was FOUND by a funding search, but the
            # payload names a different organisation.
            _seed(
                conn, doi, f"misleading label fixture {slug}",
                {"funder": [{"name": "Unrelated Funding Body"}]},
                source_of_truth="crossref-funding+crossref-affiliation",
            )
            assert _attributed(conn, doi) is False, (
                "a '-funding'/'-affiliation' provenance token records how a record was "
                "discovered, not whose it is, and must not attribute on its own"
            )
            assert not _in_canon(conn, doi)
    finally:
        _cleanup([doi])


def test_attribution_on_one_group_member_covers_the_duplicate_group() -> None:
    slug = uuid4().hex[:12]
    title = f"duplicate group fixture {slug}"
    plain = f"10.5555/attr-grp-a-{slug}"
    evidenced = f"10.5555/attr-grp-b-{slug}"
    try:
        with _connect() as conn:
            _seed(conn, plain, title, {"author": [{"affiliation": [{"name": "Elsewhere"}]}]})
            _seed(conn, evidenced, title,
                  {"funder": [{"name": "Example Funding Council", "award": [AWARD]}]})

            _set_rules(conn, [("funding", AWARD)])
            assert _attributed(conn, plain) is False
            assert _attributed(conn, evidenced) is True

            # The two rows share a title/year, so they form one dedup group;
            # its winner is canonical because a MEMBER carries the evidence.
            assert _canon_titles(conn, title) == 1, (
                "evidence on any member must attribute the whole duplicate group"
            )
    finally:
        _cleanup([plain, evidenced])


def test_curator_decisions_keep_precedence_over_attribution() -> None:
    slug = uuid4().hex[:12]
    excluded = f"10.5555/attr-excl-{slug}"
    included = f"10.5555/attr-incl-{slug}"
    preprint = f"10.5555/attr-pre-{slug}"
    try:
        with _connect() as conn:
            _seed(conn, excluded, f"excluded fixture {slug}",
                  {"author": [{"affiliation": [{"name": INSTITUTE}]}]})
            _seed(conn, included, f"included fixture {slug}",
                  {"author": [{"affiliation": [{"name": "Elsewhere"}]}]})
            _seed(conn, preprint, f"preprint fixture {slug}",
                  {"author": [{"affiliation": [{"name": INSTITUTE}]}]}, is_preprint=True)
            _set_rules(conn, [("affiliation", INSTITUTE)])

            assert _in_canon(conn, excluded)
            assert not _in_canon(conn, included)
            assert not _in_canon(conn, preprint), "preprints stay out of canon"

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO biblio.canon_scope_decisions
                      (scope_type, mode, subject_doi, subject_doi_key, note, created_by, active)
                    VALUES ('global', 'exclude', %s, %s, 'pytest exclude', 'pytest', true),
                           ('global', 'include', %s, %s, 'pytest include', 'pytest', true),
                           ('global', 'include', %s, %s, 'pytest include preprint', 'pytest', true)
                    """,
                    (excluded, excluded, included, included, preprint, preprint),
                )

            assert not _in_canon(conn, excluded), "curator exclude wins over attribution"
            assert _in_canon(conn, included), "curator include works without attribution"
            assert not _in_canon(conn, preprint), (
                "the preprint restriction still applies to curator-included rows"
            )
    finally:
        _cleanup([excluded, included, preprint])


def test_changing_rules_recomputes_flags_and_snapshot() -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/attr-change-{slug}"
    try:
        with _connect() as conn:
            _set_rules(conn, [])
            _seed(conn, doi, f"late configuration fixture {slug}",
                  {"author": [{"affiliation": [{"name": INSTITUTE}]}]})
            assert _attributed(conn, doi) is False

            # Configure after the publication already exists.
            _set_rules(conn, [("affiliation", INSTITUTE)])
            assert _attributed(conn, doi) is True, (
                "changing the rules must recompute the cached flag for existing rows"
            )
            assert _in_canon(conn, doi)

            # The snapshot only follows once refreshed, map before canon.
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM biblio.publications_canon_web WHERE doi = %s", (doi,))
                assert cur.fetchone() is None, "snapshot is stale until refreshed"
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publication_dedup_map_web")
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publications_canon_web")
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM biblio.publications_canon_web WHERE doi = %s", (doi,))
                assert cur.fetchone(), "refresh must publish the new canon membership"

            # Removing the rule takes it back out again.
            _set_rules(conn, [])
            assert _attributed(conn, doi) is False
            assert not _in_canon(conn, doi)
    finally:
        _cleanup([doi])
        with _connect() as conn:
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publication_dedup_map_web")
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publications_canon_web")


def test_blank_and_too_short_patterns_are_rejected() -> None:
    try:
        with _connect() as conn:
            for bad in ("", "   ", "x"):
                with pytest.raises(psycopg.errors.CheckViolation):
                    with conn.transaction():
                        conn.execute(
                            "INSERT INTO app.institution_attribution_rules (rule_kind, pattern) "
                            "VALUES ('affiliation', %s)",
                            (bad,),
                        )
            # An empty rule set matches nothing rather than everything.
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.institution_attribution_rules")
                cur.execute(
                    "SELECT biblio.institution_attribution_signal(%s::jsonb) AS hit",
                    ('{"anything": "at all"}',),
                )
                assert cur.fetchone()["hit"] is False
    finally:
        _cleanup([])


def test_signal_function_is_stable_not_immutable() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.provolatile
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'biblio' AND p.proname = 'institution_attribution_signal'
            """
        )
        row = cur.fetchone()
        assert row is not None, "biblio.institution_attribution_signal must exist"
        assert row["provolatile"] == "s", (
            "the signal reads app.institution_attribution_rules, so it must be STABLE, "
            "never IMMUTABLE — an IMMUTABLE function may be folded to a constant"
        )


def test_attribution_rules_command_changes_rules_flags_and_snapshot() -> None:
    """The documented one operation, end to end against the disposable DB."""
    from people_pubs.sync import attribution_rules

    slug = uuid4().hex[:12]
    doi = f"10.5555/attr-cli-{slug}"
    dsn = _dsn()
    try:
        with _connect() as conn:
            _set_rules(conn, [])
            _seed(conn, doi, f"cli fixture {slug}",
                  {"author": [{"affiliation": [{"name": INSTITUTE}]}]})
            assert _attributed(conn, doi) is False

        # --dry-run must change nothing and refresh nothing, even with the
        # refresh left enabled.
        before = _snapshot_stamps()
        attribution_rules.run(
            mode="set", dsn=dsn, affiliations=[INSTITUTE], fundings=[],
            note=None, dry_run=True, refresh=True, refresh_only=False,
        )
        with _connect() as conn:
            assert _attributed(conn, doi) is False, "--dry-run must roll back"
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM app.institution_attribution_rules")
                assert cur.fetchone()["n"] == 0
        assert _snapshot_stamps() == before, "--dry-run must not refresh either snapshot"

        # The real thing: rules + flags + snapshots, in order.
        attribution_rules.run(
            mode="set", dsn=dsn, affiliations=[INSTITUTE], fundings=[AWARD],
            note="pytest", dry_run=False, refresh=True, refresh_only=False,
        )
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rule_kind, pattern FROM app.institution_attribution_rules "
                    "ORDER BY rule_kind"
                )
                rows = [(r["rule_kind"], r["pattern"]) for r in cur.fetchall()]
            assert rows == [("affiliation", INSTITUTE), ("funding", AWARD)]
            assert _attributed(conn, doi) is True, "the command must recompute the cached flag"
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM biblio.publications_canon_web WHERE doi = %s", (doi,))
                assert cur.fetchone(), "the command must refresh the serving snapshot"

        # add / remove operate on the existing set.
        attribution_rules.run(
            mode="remove", dsn=dsn, affiliations=[INSTITUTE], fundings=[],
            note=None, dry_run=False, refresh=True, refresh_only=False,
        )
        with _connect() as conn:
            assert _attributed(conn, doi) is False
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM app.institution_attribution_rules")
                assert cur.fetchone()["n"] == 1, "only the named rule is removed"

        # refresh-only is a safe retry after a failed refresh.
        attribution_rules.run(
            mode="recompute", dsn=dsn, affiliations=[], fundings=[],
            note=None, dry_run=False, refresh=True, refresh_only=True,
        )
    finally:
        _cleanup([doi])
        with _connect() as conn:
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publication_dedup_map_web")
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publications_canon_web")


def _snapshot_stamps() -> tuple:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT max(snapshot_refreshed_at) FROM biblio.publications_canon_web) AS canon, "
            "       (SELECT max(snapshot_refreshed_at) FROM biblio.publication_dedup_map_web) AS map"
        )
        row = cur.fetchone()
        return (row["canon"], row["map"])


def _in_snapshots(doi: str) -> tuple[bool, bool]:
    """(in publications_canon_web, in publication_dedup_map_web) for the DOI's row."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pub_id FROM biblio.publications WHERE doi = %s", (doi,))
        pub_id = cur.fetchone()["pub_id"]
        cur.execute("SELECT 1 FROM biblio.publications_canon_web WHERE pub_id = %s", (pub_id,))
        in_canon = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM biblio.publication_dedup_map_web WHERE pub_id = %s", (pub_id,))
        in_map = cur.fetchone() is not None
        return in_canon, in_map


def _drop_canon_unique_index(conn: psycopg.Connection) -> dict:
    """Break the canon refresh deterministically (CONCURRENTLY needs the unique index)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname, indexdef FROM pg_indexes
            WHERE schemaname = 'biblio' AND tablename = 'publications_canon_web'
              AND indexdef ILIKE 'CREATE UNIQUE INDEX%'
            """
        )
        idx = cur.fetchone()
    assert idx, "canon_web must carry a unique index for CONCURRENTLY"
    conn.execute(f"DROP INDEX biblio.{idx['indexname']}")
    return idx


def test_failed_refresh_rolls_back_both_snapshots_together() -> None:
    """The two snapshots are published as one transaction: when the canon
    refresh fails after the map refresh would have succeeded, the map is
    rolled back too, and the retry publishes both."""
    from people_pubs.sync import attribution_rules

    slug = uuid4().hex[:12]
    doi = f"10.5555/attr-atomic-{slug}"
    dsn = _dsn()
    idx = None
    try:
        with _connect() as conn:
            _set_rules(conn, [("affiliation", INSTITUTE)])
        attribution_rules.run(
            mode="recompute", dsn=dsn, affiliations=[], fundings=[],
            note=None, dry_run=False, refresh=True, refresh_only=True,
        )
        with _connect() as conn:
            _seed(conn, doi, f"atomic fixture {slug}",
                  {"author": [{"affiliation": [{"name": INSTITUTE}]}]})
            assert _attributed(conn, doi) is True
        assert _in_snapshots(doi) == (False, False), "new row is in neither snapshot yet"

        with _connect() as conn:
            idx = _drop_canon_unique_index(conn)
        before = _snapshot_stamps()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            attribution_rules.run(
                mode="recompute", dsn=dsn, affiliations=[], fundings=[],
                note=None, dry_run=False, refresh=True, refresh_only=True,
            )
        assert _in_snapshots(doi) == (False, False), (
            "a failed canon refresh must roll the map refresh back with it"
        )
        assert _snapshot_stamps() == before, "neither snapshot may change on failure"

        with _connect() as conn:
            conn.execute(idx["indexdef"])
            idx = None
        attribution_rules.run(
            mode="recompute", dsn=dsn, affiliations=[], fundings=[],
            note=None, dry_run=False, refresh=True, refresh_only=True,
        )
        assert _in_snapshots(doi) == (True, True), "the retry publishes both snapshots"
    finally:
        if idx is not None:
            with _connect() as conn:
                conn.execute(idx["indexdef"])
        _cleanup([doi])
        with _connect() as conn:
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publication_dedup_map_web")
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publications_canon_web")


def test_rule_change_survives_a_failed_refresh_and_refresh_only_retries() -> None:
    """Transaction 1 (rules + flags) commits even when transaction 2 (the
    refresh) fails; the documented retry then publishes the snapshots."""
    from people_pubs.sync import attribution_rules

    slug = uuid4().hex[:12]
    doi = f"10.5555/attr-retry-{slug}"
    dsn = _dsn()
    idx = None
    try:
        with _connect() as conn:
            _set_rules(conn, [])
            _seed(conn, doi, f"retry fixture {slug}",
                  {"author": [{"affiliation": [{"name": INSTITUTE}]}]})
            assert _attributed(conn, doi) is False
            idx = _drop_canon_unique_index(conn)
        before = _snapshot_stamps()

        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            attribution_rules.run(
                mode="set", dsn=dsn, affiliations=[INSTITUTE], fundings=[],
                note=None, dry_run=False, refresh=True, refresh_only=False,
            )
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pattern FROM app.institution_attribution_rules")
                assert [r["pattern"] for r in cur.fetchall()] == [INSTITUTE], "the rule change is committed"
            assert _attributed(conn, doi) is True, "the recomputed flag is committed with the rules"
            assert _in_canon(conn, doi)
        assert _snapshot_stamps() == before, "the failed refresh leaves both snapshots untouched"

        with _connect() as conn:
            conn.execute(idx["indexdef"])
            idx = None
        attribution_rules.run(
            mode="recompute", dsn=dsn, affiliations=[], fundings=[],
            note=None, dry_run=False, refresh=True, refresh_only=True,
        )
        assert _in_snapshots(doi) == (True, True)
    finally:
        if idx is not None:
            with _connect() as conn:
                conn.execute(idx["indexdef"])
        _cleanup([doi])
        with _connect() as conn:
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publication_dedup_map_web")
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publications_canon_web")


def test_recompute_resyncs_flags_without_changing_rules() -> None:
    """An ad hoc rule edit that skipped the recompute is repaired by
    `recompute`, which leaves the rules exactly as it found them."""
    from people_pubs.sync import attribution_rules

    slug = uuid4().hex[:12]
    doi = f"10.5555/attr-resync-{slug}"
    dsn = _dsn()
    try:
        with _connect() as conn:
            _set_rules(conn, [])
            _seed(conn, doi, f"resync fixture {slug}",
                  {"author": [{"affiliation": [{"name": INSTITUTE}]}]})
            # Rule added without recomputing: the cache is now stale on purpose.
            conn.execute(
                "INSERT INTO app.institution_attribution_rules (rule_kind, pattern, note) "
                "VALUES ('affiliation', %s, 'ad hoc')",
                (INSTITUTE,),
            )
            assert _attributed(conn, doi) is False
        attribution_rules.run(
            mode="recompute", dsn=dsn, affiliations=[], fundings=[],
            note=None, dry_run=False, refresh=False, refresh_only=False,
        )
        with _connect() as conn:
            assert _attributed(conn, doi) is True, "recompute re-syncs the cached flag"
            with conn.cursor() as cur:
                cur.execute("SELECT rule_kind, pattern, note FROM app.institution_attribution_rules")
                rows = [(r["rule_kind"], r["pattern"], r["note"]) for r in cur.fetchall()]
            assert rows == [("affiliation", INSTITUTE, "ad hoc")], "recompute changes no rule"
    finally:
        _cleanup([doi])


def test_too_short_pattern_is_rejected_before_any_write() -> None:
    from people_pubs.sync import attribution_rules

    with pytest.raises(SystemExit):
        attribution_rules.run(
            mode="set", dsn=_dsn(), affiliations=["x"], fundings=[],
            note=None, dry_run=False, refresh=False, refresh_only=False,
        )
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM app.institution_attribution_rules")
        assert cur.fetchone()["n"] == 0, "a rejected pattern must not write anything"
