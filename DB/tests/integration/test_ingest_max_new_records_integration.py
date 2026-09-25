"""Disposable-DB proof that --max-new-records bounds what a discovery search
actually writes. A synthetic Crossref API offers 200 brand-new DOIs; with
--max-new-records=5 exactly five publications must be written and the run must
stop. Skipped unless PEOPLE_PUBS_INTEGRATION_DSN points at a disposable database.

No live network, no real people data. See DB/INGEST_SEARCH_SEMANTICS.md.
"""
from __future__ import annotations

import os
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.sync import crossref_search

pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True)


def _crossref_fake_get(items: list[dict], per_page: int = 100):
    pages = [items[i : i + per_page] for i in range(0, len(items), per_page)] or [[]]

    def fake_get(self, url, params=None, **kwargs):  # noqa: ANN001
        cursor = (params or {}).get("cursor", "*")
        idx = 0 if cursor == "*" else int(cursor)
        page = pages[idx] if idx < len(pages) else []

        class _Resp:
            status_code = 200
            text = ""
            headers: dict = {}

            def json(self_inner):  # noqa: N805
                message = {"items": page, "total-results": len(items)}
                if idx + 1 < len(pages) and page:
                    message["next-cursor"] = str(idx + 1)
                return {"message": message}

            def raise_for_status(self_inner):  # noqa: N805
                return None

        return _Resp()

    return fake_get


def _cleanup(prefix: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM biblio.authorships WHERE pub_id IN "
            "(SELECT pub_id FROM biblio.publications WHERE doi LIKE %s)",
            (prefix + "-%",),
        )
        cur.execute("DELETE FROM biblio.publications WHERE doi LIKE %s", (prefix + "-%",))


def test_crossref_max_new_records_stops_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = _dsn()
    slug = uuid4().hex[:12]
    prefix = f"10.5555/mnr-{slug}"
    items = [
        {"DOI": f"{prefix}-{i}", "title": [f"Title {i}"], "author": [{"given": "Ada", "family": f"L{i}"}]}
        for i in range(200)
    ]
    monkeypatch.setattr(httpx.Client, "get", _crossref_fake_get(items))

    try:
        stats = crossref_search.run_crossref_search(
            dsn=dsn, awards=[], affiliations=["Example University"], funders=[], author_orcids=[],
            only_person_ids=None, from_year=None, to_year=None, count=100,
            max_records=None, max_new_records=5, min_interval=0.0, no_db=False,
            dry_run=False, debug=False, debug_json=False, source_token="crossref",
            rewrite_sot=None, skip_existing=True, refresh_authorships=False,
            commit_every=50, award_mode="filter", require_affiliation_match=False,
        )
        assert stats.inserted == 5, f"expected 5 inserts, got {stats.inserted}"

        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM biblio.publications WHERE doi LIKE %s", (prefix + "-%",))
            written = int(cur.fetchone()["n"])
        assert written == 5, f"max-new-records=5 must write exactly 5 rows, wrote {written}"
    finally:
        _cleanup(prefix)
