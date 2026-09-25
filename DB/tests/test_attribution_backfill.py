from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from people_pubs.sync import backfill_all, crossref_backfill
from people_pubs.sync.scaffold import BackfillStats


class _FakeCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: Any = None
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: Any) -> None:
        self.sql = sql
        self.params = params

    def executemany(self, _sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.rows = list(rows)

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.commit_count = 0
        self.rollback_count = 0

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


def test_error_attempts_bypass_cooldown(monkeypatch) -> None:
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(backfill_all, "db_conn", lambda _dsn: conn)

    assert (
        backfill_all.select_unattributed_missing_payload_pub_ids(
            dsn="postgresql://test",
            max_n=50,
            cooldown_days=30,
        )
        == []
    )
    assert "a.last_outcome = 'error'" in cursor.sql
    assert cursor.params == {"cooldown_days": 30, "max_n": 50}


def test_attribution_selector_targets_a_missing_crossref_payload(monkeypatch) -> None:
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(backfill_all, "db_conn", lambda _dsn: conn)

    assert backfill_all.ATTRIBUTION_PAYLOAD_KEYS == ("crossref",)

    backfill_all.select_unattributed_missing_payload_pub_ids(
        dsn="postgresql://test",
        max_n=5,
        cooldown_days=30,
    )
    assert "AND (NOT (p.raw_json ? 'crossref'))" in cursor.sql

    assert backfill_all._payload_state("postgresql://test", [1]) == {}
    assert "(NOT (p.raw_json ? 'crossref')) AS missing_crossref" in cursor.sql
    assert cursor.sql.count(" AS missing_") == 1


def test_attribution_attempts_distinguish_errors_from_completed_lookups(
    monkeypatch,
) -> None:
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(backfill_all, "db_conn", lambda _dsn: conn)

    # The attempt bookkeeping never interprets payload keys: any source names
    # work, so the keys below only need to be distinct.
    counts = backfill_all.record_attribution_attempts(
        dsn="postgresql://test",
        before={
            1: (["crossref", "openalex"], False),
            2: (["crossref"], False),
            3: (["datacite"], False),
            4: (["openalex"], False),
        },
        after={
            1: (["crossref", "openalex"], False),
            2: ([], True),
            3: (["datacite"], False),
        },
        provider_errors={
            "crossref": {1, 2},
            "openalex": set(),
            "datacite": set(),
        },
    )

    assert counts == {
        "enriched": 1,
        "no_new_payload": 1,
        "error": 1,
        "became_attributed": 1,
        "merged_away": 1,
    }
    assert [row[1] for row in cursor.rows] == [
        "error",
        "enriched",
        "no_new_payload",
    ]
    assert conn.committed is True


def test_backfill_stats_keep_failed_publications_per_run() -> None:
    first = BackfillStats()
    second = BackfillStats()

    first.failed_pub_ids.add(42)

    assert first.failed_pub_ids == {42}
    assert second.failed_pub_ids == set()


def test_crossref_commits_success_before_isolating_later_failure(monkeypatch) -> None:
    # Per-publication failure isolation is what the attribution attempts rely
    # on: a failing publication is rolled back and reported in failed_pub_ids
    # without undoing the publication committed before it.
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    pubs = [SimpleNamespace(pub_id=10), SimpleNamespace(pub_id=11)]

    def _backfill_one(
        _conn: _FakeConnection,
        _client: _Client,
        pub: SimpleNamespace,
        **_kwargs: Any,
    ) -> None:
        if pub.pub_id == 11:
            raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(crossref_backfill, "db_conn", lambda _dsn: conn)
    monkeypatch.setattr(crossref_backfill, "CrossrefHTTP", _Client)
    monkeypatch.setattr(
        crossref_backfill,
        "iter_publications_to_backfill",
        lambda *_args, **_kwargs: iter(pubs),
    )
    monkeypatch.setattr(crossref_backfill, "backfill_one", _backfill_one)

    stats = crossref_backfill.run_crossref_backfill(
        dsn="postgresql://test",
        mailto=None,
        max_n=2,
        only_pub_ids=[10, 11],
        merge_mode="doi",
        sleep_s=0,
        dry_run=False,
        force=True,
        refresh_authorships=False,
        source_token="crossref",
        include_department=False,
    )

    assert stats.processed == 2
    assert conn.commit_count == 1
    assert conn.rollback_count == 1
    assert stats.failed_pub_ids == {11}
    assert stats.errors == 1
