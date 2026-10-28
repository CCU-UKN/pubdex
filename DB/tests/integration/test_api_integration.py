"""The read service on a disposable PostgreSQL: real rows, read through the
real database layer as app_readonly in read-only sessions.

The application is served in process (httpx's ASGI transport) from
people_pubs.api.database.ReadDatabase, configured through the service's own
settings. Its sessions reach the disposable database the integration DSN
names, as the superuser that database was created with, switched to a role by
the role option of the connection string -- so every privilege check is that
role's, and no role password has to be set on the database. The superuser
password goes through the password file, as a deployment's role password
would.

The demonstration test runs the same commands as DB/run_task_1a_demo.sh --
roster import, fixture ingestion, attribution -- and then asks the service
for the publication they produce: once as the superuser, as that script
does, and once as app_writer, as the Compose deployment does, snapshot
refresh included. Like the other attribution tests, these
tests own app.institution_attribution_rules and the serving snapshots for
the length of the run, so they need a disposable database.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from people_pubs.api.app import MAX_PUB_ID, create_app
from people_pubs.api.database import PUBLICATION_FIELDS, ReadDatabase
from people_pubs.api.settings import Settings, load_settings
from people_pubs.sync import attribution_rules
from people_pubs.sync.add_people import _iter_csv_records, run_add_people
from people_pubs.sync.fixture_demo import DEMO_AFFILIATION, DEMO_DOI, DEMO_TITLE, run_fixture_demo

pytestmark = pytest.mark.integration

ROSTER = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "demo_roster_minimal.csv"
ROSTER_ORCIDS = ["0000-0002-1825-0097", "0000-0000-0000-0001", "0000-0000-0000-001X"]


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


@pytest.fixture
def admin() -> Iterator[psycopg.Connection]:
    """A superuser connection for setting up and inspecting the database."""
    with psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True) as conn:
        yield conn


@pytest.fixture
def service_settings(tmp_path: Path):
    """Builds the service's settings from its own environment variables."""
    def build(role: str | None = "app_readonly", wait: float = 2.0, **params: str) -> Settings:
        target = conninfo_to_dict(_dsn())
        password_file = tmp_path / f"password-{uuid4().hex[:8]}"
        password_file.write_text(target.pop("password") + "\n", encoding="utf-8")
        password_file.chmod(0o600)
        if role is not None:
            target["options"] = f"-c role={role}"
        target.update(params)
        return load_settings({
            "PEOPLE_PUBS_API_DSN": make_conninfo("", **target),
            "PEOPLE_PUBS_API_DB_PASSWORD_FILE": str(password_file),
            "PEOPLE_PUBS_API_DB_WAIT_SECONDS": str(wait),
            "PEOPLE_PUBS_API_STATEMENT_TIMEOUT_MS": "3000",
        })

    return build


@contextmanager
def serving(settings: Settings) -> Iterator[tuple]:
    source = ReadDatabase(settings)
    source.open()
    try:
        yield create_app(source), source
    finally:
        source.close()


def get(app, target: str) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://service.test", trust_env=False) as client:
            return await client.get(target)

    return asyncio.run(send())


def all_pages(app, limit: int) -> List[dict]:
    items: List[dict] = []
    after = 0
    while True:
        response = get(app, f"/v0/publications?limit={limit}&after={after}")
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) <= limit
        items += page["items"]
        if page["next_after"] is None:
            return items
        assert page["next_after"] == page["items"][-1]["pub_id"]
        after = page["next_after"]


def refresh_snapshots(conn: psycopg.Connection) -> None:
    """What DB/refresh_publications_canon_web.sql does, as the superuser."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('biblio.publications_canon_web.refresh'))")
        conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publication_dedup_map_web")
        conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.publications_canon_web")


def reset_demonstration(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM biblio.publications WHERE doi = %s", (DEMO_DOI,))
    conn.execute(
        "DELETE FROM app.people WHERE person_id IN "
        "(SELECT person_id FROM app.identities_orcid WHERE orcid = ANY(%s))",
        (ROSTER_ORCIDS,),
    )
    conn.execute("DELETE FROM app.institution_attribution_rules")
    conn.execute("SELECT biblio.recompute_institution_attribution()")
    refresh_snapshots(conn)


def snapshot_ids(conn: psycopg.Connection) -> List[int]:
    return [row["pub_id"] for row in conn.execute(
        "SELECT pub_id FROM biblio.publications_canon_web ORDER BY pub_id").fetchall()]


@pytest.mark.parametrize("ingestion_role", [None, "app_writer"], ids=["as-superuser", "as-app_writer"])
def test_the_demonstration_publication_is_served_as_app_readonly(admin, service_settings, ingestion_role) -> None:
    # DB/run_task_1a_demo.sh ingests as the superuser; the Compose deployment
    # as app_writer, whose privilege checks the role option makes apply.
    dsn = _dsn() if ingestion_role is None else make_conninfo(_dsn(), options=f"-c role={ingestion_role}")
    reset_demonstration(admin)
    try:
        # The demonstration's own steps, in its order (DB/run_task_1a_demo.sh).
        run_add_people(dsn=dsn, records=_iter_csv_records(str(ROSTER), ","), default_person_kind="external",
                       update_existing=False, dry_run=False, lookup_orcid=False)
        run_fixture_demo(dsn=dsn)
        assert attribution_rules.run(mode="set", dsn=dsn, affiliations=[DEMO_AFFILIATION], fundings=[], note=None,
                                     dry_run=False, refresh=True, refresh_only=False) == 0
        stored = admin.execute("SELECT pub_id FROM biblio.publications WHERE doi = %s", (DEMO_DOI,)).fetchone()

        with serving(service_settings()) as (app, _):
            assert get(app, "/readyz").json() == {"status": "ready"}
            response = get(app, "/v0/publications?limit=100")
            items = all_pages(app, limit=1)

        assert response.status_code == 200
        served = [item for item in items if item["doi"] == DEMO_DOI]
        assert served == [{
            "pub_id": stored["pub_id"],
            "doi": DEMO_DOI,
            "title": DEMO_TITLE,
            "year": 2024,
            "venue": "Journal of Synthetic Metadata",
            "source_of_truth": served[0]["source_of_truth"],
            "internal_authors": "Ada Example",
        }]
        assert set(served[0]["source_of_truth"].split("+")) >= {"crossref", "orcid"}
        # The external co-author, the affiliation the payload carries and the
        # payloads themselves are stored, and none of them is served.
        for withheld in ("Bert Example", DEMO_AFFILIATION, "raw_json", "ada.example@example.org"):
            assert withheld not in response.text
        assert [tuple(item) for item in items] == [PUBLICATION_FIELDS] * len(items)
    finally:
        reset_demonstration(admin)


@pytest.fixture
def three_publications(admin) -> Iterator[List[int]]:
    """Three canonical publications of a rule of their own, in the snapshot."""
    slug = uuid4().hex[:12]
    marker = f"Synthetic Paging Institute {slug}"
    pub_ids = []
    try:
        for n in range(3):
            doi = f"10.5555/api-paging-{slug}-{n}"
            pub_ids.append(admin.execute(
                "INSERT INTO biblio.publications (doi, title, year, source_of_truth, raw_json) "
                "VALUES (%s, %s, 2023, 'crossref', %s) RETURNING pub_id",
                (doi, f"Paging title {slug} {n}", Jsonb({"crossref": {"affiliation": marker}})),
            ).fetchone()["pub_id"])
        admin.execute("INSERT INTO app.institution_attribution_rules (rule_kind, pattern) VALUES ('affiliation', %s)",
                      (marker,))
        admin.execute("SELECT biblio.recompute_institution_attribution()")
        refresh_snapshots(admin)
        yield pub_ids
    finally:
        admin.execute("DELETE FROM biblio.publications WHERE pub_id = ANY(%s)", (pub_ids,))
        admin.execute("DELETE FROM app.institution_attribution_rules WHERE pattern = %s", (marker,))
        admin.execute("SELECT biblio.recompute_institution_attribution()")
        refresh_snapshots(admin)


def test_pages_cover_the_snapshot_in_pub_id_order(admin, service_settings, three_publications) -> None:
    expected = snapshot_ids(admin)
    assert set(three_publications) <= set(expected)
    with serving(service_settings()) as (app, _):
        for limit in (1, 2, 100):
            assert [item["pub_id"] for item in all_pages(app, limit)] == expected
        before_first = get(app, f"/v0/publications?limit=2&after={three_publications[0] - 1}").json()
    assert [item["pub_id"] for item in before_first["items"]] == three_publications[:2]
    assert before_first["next_after"] == three_publications[1]


def test_a_page_after_the_last_publication_is_empty(admin, service_settings, three_publications) -> None:
    last = snapshot_ids(admin)[-1]
    with serving(service_settings()) as (app, _):
        for after in (last, MAX_PUB_ID):
            page = get(app, f"/v0/publications?after={after}")
            assert page.status_code == 200
            assert page.json() == {"items": [], "limit": 25, "next_after": None}


def test_the_sessions_run_as_app_readonly_and_cannot_write(service_settings) -> None:
    with serving(service_settings()) as (_, source):
        with source.pool.connection() as conn:
            session = conn.execute(
                "SELECT current_user AS role, current_setting('transaction_read_only') AS read_only, "
                "current_setting('statement_timeout') AS timeout, current_setting('application_name') AS name"
            ).fetchone()
            assert session == {"role": "app_readonly", "read_only": "on", "timeout": "3s", "name": "people-pubs-api"}
            with pytest.raises((errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)):
                conn.execute("INSERT INTO biblio.publications (doi, title) VALUES ('10.5555/api-write', 'x')")


def test_a_session_of_a_role_that_could_write_is_read_only_all_the_same(admin, service_settings) -> None:
    # Misconfigured on purpose: no role option, so the session is the superuser's.
    with serving(service_settings(role=None)) as (app, source):
        assert get(app, "/readyz").status_code == 200
        with source.pool.connection() as conn:
            with pytest.raises(errors.ReadOnlySqlTransaction):
                conn.execute("INSERT INTO biblio.publications (doi, title) VALUES ('10.5555/api-write', 'x')")
            with pytest.raises(errors.ReadOnlySqlTransaction):
                conn.execute("CREATE TABLE biblio.api_write_probe (probe int)")
    assert admin.execute("SELECT count(*) AS n FROM biblio.publications WHERE doi = '10.5555/api-write'"
                         ).fetchone()["n"] == 0


def test_a_database_that_cannot_be_reached_is_reported_within_the_wait(service_settings) -> None:
    absent = f"people_db_absent_{uuid4().hex[:8]}"
    with serving(service_settings(wait=1.0, dbname=absent)) as (app, _):
        started = time.monotonic()
        ready = get(app, "/readyz")
        assert time.monotonic() - started < 5
        page = get(app, "/v0/publications")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reason": "database_unavailable"}
    assert page.status_code == 503 and page.json()["error"]["code"] == "database_unavailable"
    for text in (ready.text, page.text):
        assert absent not in text and "FATAL" not in text and "127.0.0.1" not in text


def test_a_read_surface_the_role_cannot_use_is_reported(service_settings) -> None:
    # maintenance has no USAGE on biblio.
    with serving(service_settings(role="maintenance")) as (app, _):
        ready = get(app, "/readyz")
        page = get(app, "/v0/publications")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reason": "read_surface_unavailable"}
    assert page.status_code == 503 and page.json()["error"]["code"] == "read_surface_unavailable"
    assert "permission" not in page.text and "biblio" not in page.text


def test_an_unpopulated_snapshot_is_not_ready_until_it_is_refreshed(admin, service_settings) -> None:
    with serving(service_settings()) as (app, _):
        assert get(app, "/readyz").status_code == 200
        try:
            admin.execute("REFRESH MATERIALIZED VIEW biblio.publications_canon_web WITH NO DATA")
            ready = get(app, "/readyz")
            assert ready.status_code == 503
            assert ready.json() == {"status": "not_ready", "reason": "read_surface_unavailable"}
            assert get(app, "/v0/publications").json()["error"]["code"] == "read_surface_unavailable"
        finally:
            admin.execute("REFRESH MATERIALIZED VIEW biblio.publications_canon_web")
        assert get(app, "/readyz").json() == {"status": "ready"}


def _service_backends(conn: psycopg.Connection, name: str) -> List[int]:
    return [row["pid"] for row in conn.execute(
        "SELECT pid FROM pg_stat_activity WHERE application_name = %s ORDER BY pid", (name,)).fetchall()]


def test_connections_the_server_drops_are_replaced_without_a_failed_request(admin, service_settings) -> None:
    name = f"people-pubs-api-test-{uuid4().hex[:8]}"
    with serving(service_settings(wait=5.0, application_name=name)) as (app, _):
        assert get(app, "/readyz").status_code == 200
        dropped = _service_backends(admin, name)
        assert dropped, "the pool keeps its connection"
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = %s", (name,))
        assert get(app, "/readyz").json() == {"status": "ready"}
        assert get(app, "/v0/publications?limit=1").status_code == 200
        replaced = _service_backends(admin, name)
    assert replaced and not set(replaced) & set(dropped)


def test_readiness_fails_while_the_database_refuses_connections_and_recovers(service_settings) -> None:
    name = f"people-pubs-api-test-{uuid4().hex[:8]}"
    target = conninfo_to_dict(_dsn())
    database = sql.Identifier(target.get("dbname") or "people_db")
    # PostgreSQL refuses to close the database it is connected to, so this
    # connection goes to the server's maintenance database; it stays open
    # while new connections to the tested database, the superuser's
    # included, are refused.
    with psycopg.connect(make_conninfo("", **{**target, "dbname": "postgres"}), row_factory=dict_row,
                         autocommit=True) as server_admin:
        with serving(service_settings(wait=1.0, application_name=name)) as (app, _):
            assert get(app, "/readyz").status_code == 200
            try:
                server_admin.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(database))
                server_admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = %s", (name,))
                started = time.monotonic()
                refused = get(app, "/readyz")
                assert time.monotonic() - started < 5
                assert refused.status_code == 503
                assert refused.json() == {"status": "not_ready", "reason": "database_unavailable"}
            finally:
                server_admin.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(database))
            deadline = time.monotonic() + 30
            while get(app, "/readyz").status_code != 200:
                assert time.monotonic() < deadline, "readiness did not recover once connections were allowed again"
                time.sleep(0.5)
            assert get(app, "/v0/publications?limit=1").status_code == 200
