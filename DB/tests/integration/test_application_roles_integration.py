"""The database roles on a disposable database: what app_writer,
app_readonly and maintenance may do, and what they may not.

Every other integration test connects as the superuser the disposable
database is created with, and no missing grant can stop a superuser, so those
tests would pass with the roles broken. These tests switch to each role with
SET ROLE -- from then on every privilege check is that role's -- and use the
permission model DB/init/ sets up, granting nothing themselves:

- app_writer, the role routine ingestion runs as, writes a person, a
  publication, an authorship, a name alias and author emails through the
  package's own helpers, the emails through the SECURITY DEFINER functions of
  the pii schema, and reads the result back through the documented views;
  it may not read the pii tables directly or create objects;
- app_readonly, the role for reporting and inspection, reads the documented
  views and may not write, read personal data, or call the pii functions;
- maintenance, the role for maintaining personal data, finds and corrects pii
  rows by an address written in another case, as the citext columns promise,
  and may not reach the application schemas, call the pii functions or
  create objects;
- all three find the extension types and operators the application uses: a
  value cast to citext, and a citext column such as app.people.display_name
  or pii.people_pii.primary_email that compares case-insensitively, as it
  does for the superuser;
- every table and view grants each role what the model gives it: SELECT to
  app_readonly; SELECT, INSERT, UPDATE and DELETE on the tables of app,
  biblio and activity to app_writer, and on the tables of pii to maintenance,
  which gets no application schema; and USAGE, but not CREATE, on the schema
  that holds the extensions.

./run_disposable_integration.sh runs this module again after re-applying the
runtime patch, which re-creates views and re-issues grants.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from people_pubs.db.authorships import compute_order_tag, upsert_author_email, upsert_authorship
from people_pubs.db.people import upsert_person_name_alias
from people_pubs.db.publications import upsert_publication

pytestmark = pytest.mark.integration

APPLICATION_SCHEMAS = ("app", "biblio", "activity")
READABLE_SCHEMAS = APPLICATION_SCHEMAS + ("staging_raw",)
DOCUMENTED_VIEWS = (
    "biblio.publications_canon",
    "biblio.authorships_curated",
    "biblio.member_open_access_pdfs",
)


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


@contextmanager
def connected_as(role: str):
    """An autocommit connection whose privilege checks are role's."""
    with psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        assert conn.execute("SELECT current_user AS who").fetchone()["who"] == role
        yield conn


def _refused(conn: psycopg.Connection, statement: str, params: tuple = ()) -> bool:
    try:
        conn.execute(statement, params)
    except psycopg.errors.InsufficientPrivilege:
        return True
    return False


def _finds_person_case_insensitively(conn: psycopg.Connection, person_id: int, spelled: str) -> bool:
    """display_name is citext: compared with an untyped literal it has to
    ignore case for this role as it does for the superuser, which needs the
    citext operators to be visible, and the role has to be able to name the
    type in a cast, as the application's calls of the pii functions do."""
    query = sql.SQL("SELECT person_id FROM app.people WHERE display_name = {}").format(sql.Literal(spelled))
    found = [row["person_id"] for row in conn.execute(query).fetchall()]
    cast = conn.execute("SELECT %s::citext = %s::citext AS same", (spelled, spelled.lower())).fetchone()["same"]
    return found == [person_id] and cast is True


@pytest.fixture
def created():
    """Rows the tests create, removed again by the superuser afterwards."""
    dsn = _dsn()  # without a database the test skips here, before it starts
    rows: dict = {"pub_ids": [], "person_ids": []}
    yield rows
    with psycopg.connect(dsn, autocommit=True) as conn:
        pubs, people = rows["pub_ids"], rows["person_ids"]
        conn.execute("DELETE FROM pii.publication_author_emails WHERE pub_id = ANY(%s)", (pubs,))
        conn.execute("DELETE FROM biblio.authorships WHERE pub_id = ANY(%s)", (pubs,))
        conn.execute("DELETE FROM biblio.publications WHERE pub_id = ANY(%s)", (pubs,))
        conn.execute("DELETE FROM app.person_name_aliases_manual WHERE person_id = ANY(%s)", (people,))
        conn.execute("DELETE FROM pii.people_pii WHERE person_id = ANY(%s)", (people,))
        conn.execute("DELETE FROM app.people WHERE person_id = ANY(%s)", (people,))


def test_app_writer_runs_the_ingestion_writes_and_reads_them_back(created: dict) -> None:
    slug = uuid4().hex[:12]
    doi = f"10.5555/roles-{slug}"
    email = f"role-{slug}@example.org"
    with connected_as("app_writer") as conn:
        person_id = conn.execute(
            "INSERT INTO app.people (display_name, person_kind) VALUES (%s, 'internal') RETURNING person_id",
            (f"Role Example {slug}",),
        ).fetchone()["person_id"]
        created["person_ids"].append(person_id)
        pub_id = upsert_publication(conn, {
            "doi": doi, "title": f"Role title {slug}", "year": 2024, "venue": "Journal of Synthetic Roles",
            "raw_crossref_json": {"DOI": doi, "title": [f"Role title {slug}"]},
        })
        assert pub_id is not None
        created["pub_ids"].append(pub_id)
        upsert_authorship(
            conn, pub_id, 1, f"Role Example {slug}", None, person_id, ["Synthetic Institute"],
            False, compute_order_tag(1, 1), None, {"name": f"Role Example {slug}"},
        )
        upsert_person_name_alias(conn, person_id, f"Example, Role {slug}", source="roles.test")
        # Personal data only through the narrow SECURITY DEFINER functions.
        conn.execute(
            "SELECT * FROM pii.ensure_people_pii(%s, %s::citext, %s::citext[], TRUE)", (person_id, email, [email])
        )
        upsert_author_email(conn, pub_id, person_id, 1, email)
        # A rerun updates in place.
        assert upsert_publication(conn, {"doi": doi, "title": f"Role title {slug}", "year": 2024}) == pub_id

        stored = conn.execute(
            "SELECT title, year, source_of_truth FROM biblio.publications WHERE pub_id = %s", (pub_id,)
        ).fetchone()
        assert (stored["title"], stored["year"]) == (f"Role title {slug}", 2024)
        assert "crossref" in stored["source_of_truth"].split("+")
        assert _finds_person_case_insensitively(conn, person_id, f"ROLE EXAMPLE {slug.upper()}")
        curated = conn.execute(
            "SELECT person_id, is_internal_at_ingest FROM biblio.authorships_curated WHERE pub_id = %s", (pub_id,)
        ).fetchall()
        assert curated == [{"person_id": person_id, "is_internal_at_ingest": True}]
        for view in DOCUMENTED_VIEWS:
            conn.execute(sql.SQL("SELECT * FROM {} LIMIT 1").format(sql.SQL(view))).fetchall()

        # What the model withholds from it: the pii tables themselves, and DDL.
        assert _refused(conn, "SELECT emails FROM pii.people_pii WHERE person_id = %s", (person_id,))
        assert _refused(conn, "SELECT email FROM pii.publication_author_emails WHERE pub_id = %s", (pub_id,))
        assert _refused(conn, "CREATE TABLE biblio.role_probe (probe int)")

    # The functions wrote what the role itself may not read.
    with psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True) as conn:
        assert conn.execute(
            "SELECT primary_email FROM pii.people_pii WHERE person_id = %s", (person_id,)
        ).fetchone()["primary_email"] == email
        assert conn.execute(
            "SELECT count(*) AS n FROM pii.publication_author_emails WHERE pub_id = %s AND person_id = %s",
            (pub_id, person_id),
        ).fetchone()["n"] == 1


def test_app_readonly_reads_and_cannot_write_or_read_personal_data(created: dict) -> None:
    slug = uuid4().hex[:12]
    with psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True) as conn:
        person_id = conn.execute(
            "INSERT INTO app.people (display_name, person_kind) VALUES (%s, 'internal') RETURNING person_id",
            (f"Reader Example {slug}",),
        ).fetchone()["person_id"]
        created["person_ids"].append(person_id)
        pub_id = upsert_publication(conn, {"doi": f"10.5555/reader-{slug}", "title": f"Reader title {slug}",
                                           "raw_crossref_json": {"DOI": f"10.5555/reader-{slug}"}})
        created["pub_ids"].append(pub_id)

    with connected_as("app_readonly") as conn:
        assert conn.execute(
            "SELECT title FROM biblio.publications WHERE pub_id = %s", (pub_id,)
        ).fetchone()["title"] == f"Reader title {slug}"
        assert conn.execute(
            "SELECT display_name FROM app.people WHERE person_id = %s", (person_id,)
        ).fetchone()["display_name"] == f"Reader Example {slug}"
        assert _finds_person_case_insensitively(conn, person_id, f"reader example {slug}".upper())
        for view in DOCUMENTED_VIEWS:
            conn.execute(sql.SQL("SELECT * FROM {} LIMIT 1").format(sql.SQL(view))).fetchall()

        assert _refused(conn, "INSERT INTO biblio.publications (doi, title) VALUES (%s, 'x')", (f"10.5555/x-{slug}",))
        assert _refused(conn, "UPDATE biblio.publications SET title = 'changed' WHERE pub_id = %s", (pub_id,))
        assert _refused(conn, "DELETE FROM app.people WHERE person_id = %s", (person_id,))
        assert _refused(conn, "SELECT primary_email FROM pii.people_pii WHERE person_id = %s", (person_id,))
        assert _refused(
            conn, "SELECT * FROM pii.ensure_people_pii(%s, %s::citext, %s::citext[], TRUE)",
            (person_id, "reader@example.org", ["reader@example.org"]),
        )

    with psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True) as conn:
        assert conn.execute(
            "SELECT title FROM biblio.publications WHERE pub_id = %s", (pub_id,)
        ).fetchone()["title"] == f"Reader title {slug}", "nothing was changed"


def test_maintenance_corrects_personal_data_by_an_address_in_another_case(created: dict) -> None:
    slug = uuid4().hex[:12]
    address, other = f"role-{slug}@example.org", f"second-{slug}@example.org"
    with psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True) as conn:
        person_id = conn.execute(
            "INSERT INTO app.people (display_name, person_kind) VALUES (%s, 'internal') RETURNING person_id",
            (f"Maintained Example {slug}",),
        ).fetchone()["person_id"]
        created["person_ids"].append(person_id)
        conn.execute(
            "INSERT INTO pii.people_pii (person_id, primary_email, emails) VALUES (%s, %s, %s::citext[])",
            (person_id, address, [address, other]),
        )
        conn.execute("INSERT INTO pii.orcid_emails (person_id, email) VALUES (%s, %s)", (person_id, other))

    corrected = f"corrected-{slug}@example.org"
    with connected_as("maintenance") as conn:
        # As an operator types them: untyped literals, in another case.
        def literal(text: str) -> sql.Literal:
            return sql.Literal(text.upper())

        found = conn.execute(
            sql.SQL("SELECT person_id FROM pii.people_pii WHERE primary_email = {}").format(literal(address))
        ).fetchall()
        assert [row["person_id"] for row in found] == [person_id]
        listed = conn.execute(
            sql.SQL("SELECT person_id FROM pii.people_pii WHERE {} = ANY(emails)").format(literal(other))
        ).fetchall()
        assert [row["person_id"] for row in listed] == [person_id]
        assert conn.execute("SELECT %s::citext = %s::citext AS same", (address.upper(), address)).fetchone()["same"]
        updated = conn.execute(
            sql.SQL("UPDATE pii.people_pii SET primary_email = {} WHERE primary_email = {}").format(
                sql.Literal(corrected), literal(address)
            )
        )
        assert updated.rowcount == 1
        deleted = conn.execute(
            sql.SQL("DELETE FROM pii.orcid_emails WHERE person_id = {} AND email = {}").format(
                sql.Literal(person_id), literal(other)
            )
        )
        assert deleted.rowcount == 1

        # What the model withholds: the application schemas, the pii
        # functions (which serve app_writer), and objects of its own.
        assert _refused(conn, "SELECT display_name FROM app.people WHERE person_id = %s", (person_id,))
        assert _refused(conn, "SELECT title FROM biblio.publications LIMIT 1")
        assert _refused(
            conn, "SELECT * FROM pii.ensure_people_pii(%s, %s::citext, %s::citext[], TRUE)",
            (person_id, corrected, [corrected]),
        )
        assert _refused(conn, "CREATE TABLE public.maintenance_probe (probe int)")

    with psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True) as conn:
        assert conn.execute(
            "SELECT primary_email FROM pii.people_pii WHERE person_id = %s", (person_id,)
        ).fetchone()["primary_email"] == corrected
        assert conn.execute(
            "SELECT count(*) AS n FROM pii.orcid_emails WHERE person_id = %s", (person_id,)
        ).fetchone()["n"] == 0


def test_every_application_relation_grants_each_role_its_privileges() -> None:
    with psycopg.connect(_dsn(), row_factory=dict_row, autocommit=True) as conn:
        extension_schemas = conn.execute(
            """
            SELECT DISTINCT n.nspname AS schema,
                   has_schema_privilege('app_readonly', n.oid, 'USAGE') AS reader_usage,
                   has_schema_privilege('app_writer', n.oid, 'USAGE') AS writer_usage,
                   has_schema_privilege('maintenance', n.oid, 'USAGE') AS maintenance_usage,
                   has_schema_privilege('app_readonly', n.oid, 'CREATE')
                     OR has_schema_privilege('app_writer', n.oid, 'CREATE')
                     OR has_schema_privilege('maintenance', n.oid, 'CREATE') AS any_create
            FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
            WHERE e.extname IN ('citext', 'pgcrypto', 'unaccent', 'pg_trgm')
            """
        ).fetchall()
        # maintenance: the pii schema and nothing of the application's.
        maintenance_schemas = {
            row["schema"]: row["usage"]
            for row in conn.execute(
                "SELECT nspname AS schema, has_schema_privilege('maintenance', oid, 'USAGE') AS usage "
                "FROM pg_namespace WHERE nspname = ANY(%s)",
                (list(READABLE_SCHEMAS) + ["pii"],),
            ).fetchall()
        }
        pii_tables = conn.execute(
            """
            SELECT c.relname AS name,
                   has_table_privilege('maintenance', c.oid, 'SELECT')
                     AND has_table_privilege('maintenance', c.oid, 'INSERT')
                     AND has_table_privilege('maintenance', c.oid, 'UPDATE')
                     AND has_table_privilege('maintenance', c.oid, 'DELETE') AS maintenance_dml
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'pii' AND c.relkind IN ('r', 'p')
            """
        ).fetchall()
        relations = conn.execute(
            """
            SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
                   has_table_privilege('app_readonly', c.oid, 'SELECT') AS reader_select,
                   has_table_privilege('app_writer', c.oid, 'SELECT') AS writer_select,
                   has_table_privilege('app_writer', c.oid, 'INSERT')
                     AND has_table_privilege('app_writer', c.oid, 'UPDATE')
                     AND has_table_privilege('app_writer', c.oid, 'DELETE') AS writer_dml
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v', 'm')
            ORDER BY 1, 2
            """,
            (list(READABLE_SCHEMAS),),
        ).fetchall()
    assert {r["schema"] for r in relations} >= set(APPLICATION_SCHEMAS)
    names = {f"{r['schema']}.{r['name']}" for r in relations}
    assert {"biblio.publications", "biblio.authorships", "app.people"} | set(DOCUMENTED_VIEWS) <= names
    assert [f"{r['schema']}.{r['name']}" for r in relations if not r["reader_select"]] == []
    application = [r for r in relations if r["schema"] in APPLICATION_SCHEMAS]
    assert [f"{r['schema']}.{r['name']}" for r in application if not r["writer_select"]] == []
    assert [f"{r['schema']}.{r['name']}" for r in application if r["kind"] in ("r", "p") and not r["writer_dml"]] == []
    assert extension_schemas, "the extensions are installed"
    for schema in extension_schemas:
        assert schema["reader_usage"] and schema["writer_usage"] and schema["maintenance_usage"], schema["schema"]
        assert not schema["any_create"], f"only the owner creates objects in {schema['schema']}"
    assert maintenance_schemas == {
        "app": False, "biblio": False, "activity": False, "staging_raw": False, "pii": True,
    }
    assert {t["name"] for t in pii_tables} >= {"people_pii", "orcid_emails", "publication_author_emails"}
    assert [t["name"] for t in pii_tables if not t["maintenance_dml"]] == []
