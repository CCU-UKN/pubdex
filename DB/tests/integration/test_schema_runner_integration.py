"""The schema upgrade on a disposable PostgreSQL: people_pubs.schema on real databases.

Each test makes databases of its own on the disposable server and drops them
again. A baseline database is DB/init/'s SQL applied in one transaction, as
the chart's setup applies it in external mode, less the three lines that
setup leaves out (the server-wide password setting, its reload and a grant to
a role named postgres); the server applied them when it initialised. The
tests prove:

- a baseline takes every schema change of DB/schema/, recorded with its name,
  SHA-256, declaration and the release that applied it; status reports what
  is outstanding before and nothing after; a second run changes nothing;
- afterwards app_readonly reads every table of app, biblio and activity,
  app_writer writes each of them and uses each sequence there, and both read
  the record and nothing else of the schema pubdex, which only its owner
  writes;
- a change that fails halfway leaves nothing of itself: the changes before it
  stay recorded, and the next run applies it once it is fixed;
- two runners at once apply each change once: the second waits for the lock
  and then finds nothing to do, or gives up after its lock timeout;
- a role that is not a superuser, owner of its database and of the baseline
  it applied there, as in external mode, upgrades it with the command line,
  its password read from a pipe; a superuser that does not own the baseline
  is refused;
- a database the command cannot place is refused: no PubDex schema, part of
  the baseline, a recorded change whose file changed, a later release's
  change that is not backward compatible;
- the suite's own database, which ./run_disposable_integration.sh upgrades
  before the tests, has every schema change.
"""
from __future__ import annotations

import io
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from people_pubs import schema

pytestmark = pytest.mark.integration

DB_DIR = Path(__file__).resolve().parents[2]
INIT = DB_DIR / "init"
#: The lines of 000_init.sql the chart's setup leaves out, as whole lines.
LEFT_OUT = (
    "ALTER SYSTEM SET password_encryption = 'scram-sha-256';",
    "SELECT pg_reload_conf();",
    "GRANT USAGE ON SCHEMA public TO postgres;",
)
RELEASE = "integration"


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _baseline_sql() -> bytes:
    lines = (INIT / "000_init.sql").read_text(encoding="utf-8").splitlines(keepends=True)
    for line in LEFT_OUT:
        assert sum(kept.strip() == line for kept in lines) == 1, line
    parts = ["".join(kept for kept in lines if kept.strip() not in LEFT_OUT)]
    parts += [(INIT / name).read_text(encoding="utf-8")
              for name in ("010_ingestion_runtime_schema.sql", "011_member_info_manual.sql")]
    return "\n".join(parts).encode("utf-8")


@contextmanager
def _server() -> Iterator[psycopg.Connection]:
    with psycopg.connect(make_conninfo(_dsn(), dbname="postgres"), autocommit=True) as conn:
        yield conn


class Databases:
    """Databases and roles made for one test, dropped afterwards."""

    def __init__(self) -> None:
        self.databases: List[str] = []
        self.roles: List[str] = []

    def create(self, *, owner: Optional[str] = None, template: Optional[str] = None) -> str:
        name = f"schema_upgrade_test_{uuid4().hex[:12]}"
        statement = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if owner:
            statement += sql.SQL(" OWNER {}").format(sql.Identifier(owner))
        if template:
            statement += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
        with _server() as server:
            server.execute(statement)
        self.databases.append(name)
        return name

    def role(self) -> tuple:
        name, password = f"schema_owner_{uuid4().hex[:10]}", secrets.token_hex(16)
        with _server() as server:
            server.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)))
        self.roles.append(name)
        return name, password

    def drop(self) -> None:
        with _server() as server:
            for name in reversed(self.databases):
                server.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            for name in self.roles:
                server.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def databases() -> Iterator[Databases]:
    _dsn()
    made = Databases()
    yield made
    made.drop()


@pytest.fixture(scope="module")
def template() -> Iterator[str]:
    """A baseline database that is never connected to again, to copy from."""
    _dsn()
    made = Databases()
    name = made.create()
    with psycopg.connect(make_conninfo(_dsn(), dbname=name)) as conn:
        conn.execute(_baseline_sql())
    yield name
    made.drop()


def _dsn_of(database: str, **options: str) -> str:
    return make_conninfo(_dsn(), dbname=database, **options)


def _run(command: str, dsn: str, changes: Optional[Sequence[schema.Change]] = None, **options) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with psycopg.connect(dsn, autocommit=True) as conn:
        status = schema.run(command, changes or schema.load_changes(schema.SCHEMA_DIR), schema.Session(conn),
                            RELEASE, out=out, err=err, **options)
    return status, out.getvalue(), err.getvalue()


def _record(dsn: str) -> List[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT number, name, sha256, backward_compatible, release, applied_at, applied_by "
                            "FROM pubdex.schema_changes ORDER BY number").fetchall()


def _exists(dsn: str, relation: str) -> bool:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (relation,)).fetchone()[0]


def _changes(tmp_path: Path, extra: Dict[str, str]) -> List[schema.Change]:
    """This release's schema changes plus extra ones, from a scratch directory."""
    directory = tmp_path / "schema"
    directory.mkdir(exist_ok=True)
    for path in schema.SCHEMA_DIR.iterdir():
        (directory / path.name).write_bytes(path.read_bytes())
    for name, text in extra.items():
        (directory / name).write_text(text, encoding="utf-8")
    return schema.load_changes(directory)


def _privilege_problems(dsn: str) -> List[str]:
    """What the application roles lack, or have beyond their share, after a
    schema upgrade."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT format('%s %I.%I', CASE c.relkind WHEN 'S' THEN 'sequence' ELSE 'table' END,
                          n.nspname, c.relname) AS name,
                   CASE WHEN c.relkind = 'S' THEN NOT has_sequence_privilege('app_writer', c.oid, 'USAGE')
                        ELSE NOT (has_table_privilege('app_readonly', c.oid, 'SELECT')
                                  AND has_table_privilege('app_writer', c.oid, 'SELECT')
                                  AND has_table_privilege('app_writer', c.oid, 'INSERT')
                                  AND has_table_privilege('app_writer', c.oid, 'UPDATE')
                                  AND has_table_privilege('app_writer', c.oid, 'DELETE'))
                   END AS lacking
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('app', 'biblio', 'activity') AND c.relkind IN ('r', 'p', 'S')
            """).fetchall()
        problems = [f"{name}: the application roles lack their privileges" for name, lacking in rows if lacking]
        # A list of privileges asks whether the role holds any of them.
        record = conn.execute(
            """
            SELECT r.rolname,
                   has_schema_privilege(r.oid, 'pubdex', 'USAGE'),
                   has_schema_privilege(r.oid, 'pubdex', 'CREATE'),
                   has_table_privilege(r.oid, 'pubdex.schema_changes', 'SELECT'),
                   has_table_privilege(r.oid, 'pubdex.schema_changes', 'INSERT, UPDATE, DELETE, TRUNCATE')
            FROM pg_roles r WHERE r.rolname IN ('app_readonly', 'app_writer', 'maintenance') ORDER BY 1
            """).fetchall()
        public = conn.execute(
            "SELECT count(*) FROM pg_namespace n, aclexplode(n.nspacl) a WHERE n.nspname = 'pubdex' "
            "AND a.grantee = 0").fetchone()[0]
    # Read the record, and nothing else of the schema; maintenance nothing.
    expected = {"app_readonly": (True, False, True, False), "app_writer": (True, False, True, False),
                "maintenance": (False, False, False, False)}
    held = {row[0]: tuple(row[1:]) for row in record}
    if held != {role: expected[role] for role in held} or not {"app_readonly", "app_writer"} <= set(held):
        problems.append(f"the record's privileges are {record}")
    if public:
        problems.append("PUBLIC has a privilege on the schema pubdex")
    return problems


def test_a_baseline_takes_every_change_once_and_a_second_run_changes_nothing(databases, template) -> None:
    dsn = _dsn_of(databases.create(template=template))
    changes = schema.load_changes(schema.SCHEMA_DIR)
    status, out, _err = _run("status", dsn)
    assert status == schema.OUTSTANDING
    assert "the database holds the baseline and has had no schema change" in out
    assert [line for line in out.splitlines() if "outstanding: " in line] == [
        f"schema: outstanding: {change.label}: {change.summary}" for change in changes]

    status, out, err = _run("upgrade", dsn)
    assert status == schema.OK, err
    assert out.splitlines()[-1].startswith(f"schema: done: applied {len(changes)} schema change")
    record = _record(dsn)
    assert [row[:5] for row in record] == [
        (change.number, change.name, change.sha256, change.backward_compatible, RELEASE) for change in changes]
    assert {row[6] for row in record} == {conninfo_to_dict(_dsn())["user"]}
    assert _privilege_problems(dsn) == []

    status, out, err = _run("upgrade", dsn)
    assert status == schema.OK and err == ""
    assert out.splitlines()[-1].startswith("schema: nothing to do: the database has every schema change")
    assert _record(dsn) == record, "the second run changed nothing"
    assert _run("status", dsn)[0] == schema.OK


def test_a_change_that_fails_halfway_leaves_nothing_of_itself(databases, template, tmp_path: Path) -> None:
    dsn = _dsn_of(databases.create(template=template))
    first = len(schema.load_changes(schema.SCHEMA_DIR)) + 1
    failing = (f"-- Adds a probe table, then fails.\n-- backward-compatible: yes\n"
               f"CREATE TABLE app.schema_probe_partial (id bigserial PRIMARY KEY);\n"
               f"GRANT USAGE, SELECT ON SEQUENCE app.schema_probe_partial_id_seq TO app_writer;\n"
               f"INSERT INTO app.schema_probe_partial DEFAULT VALUES;\n"
               f"SELECT 1 / 0;\n")
    after = "-- Adds a second probe table.\n-- backward-compatible: yes\nCREATE TABLE app.schema_probe_after (id int);\n"
    changes = _changes(tmp_path, {f"{first:04d}_partial.sql": failing, f"{first + 1:04d}_after.sql": after})
    status, _out, err = _run("upgrade", dsn, changes)
    assert status == schema.FAILED
    assert f"schema change {first:04d}_partial failed and was rolled back, nothing of it remains: division by " \
           "zero (SQLSTATE 22012)" in err
    assert [row[1] for row in _record(dsn)] == [change.name for change in changes[:first - 1]], \
        "the changes before it stay recorded"
    assert not _exists(dsn, "app.schema_probe_partial") and not _exists(dsn, "app.schema_probe_after")

    fixed = _changes(tmp_path, {f"{first:04d}_partial.sql": failing.replace("SELECT 1 / 0;\n", "")})
    status, _out, err = _run("upgrade", dsn, fixed)
    assert status == schema.OK, err
    assert [row[1] for row in _record(dsn)] == [change.name for change in fixed]
    assert _exists(dsn, "app.schema_probe_partial") and _exists(dsn, "app.schema_probe_after")
    assert _privilege_problems(dsn) == []


class _Watched(io.StringIO):
    def __init__(self, wanted: str) -> None:
        super().__init__()
        self.wanted, self.seen = wanted, threading.Event()

    def write(self, text: str) -> int:
        if self.wanted in text:
            self.seen.set()
        return super().write(text)


def test_two_runners_at_once_apply_each_change_once(databases, template, tmp_path: Path) -> None:
    dsn = _dsn_of(databases.create(template=template))
    number = len(schema.load_changes(schema.SCHEMA_DIR)) + 1
    slow = f"-- Takes its time.\n-- backward-compatible: yes\nSELECT pg_sleep(2);\nCREATE TABLE app.schema_probe_slow (id int);\n"
    changes = _changes(tmp_path, {f"{number:04d}_slow.sql": slow})
    results: Dict[str, tuple] = {}
    first_out = _Watched(f"applying {number:04d}_slow")

    def runner(label: str, out: io.StringIO) -> None:
        err = io.StringIO()
        with psycopg.connect(dsn, autocommit=True) as conn:
            results[label] = (schema.run("upgrade", changes, schema.Session(conn), RELEASE, lock_timeout=60,
                                         out=out, err=err), out.getvalue(), err.getvalue())

    first = threading.Thread(target=runner, args=("first", first_out))
    first.start()
    assert first_out.seen.wait(30), "the first runner never began its slow change"
    second = threading.Thread(target=runner, args=("second", io.StringIO()))
    second.start()
    first.join(60)
    second.join(60)
    assert results["first"][0] == schema.OK, results["first"][2]
    assert results["second"][0] == schema.OK, results["second"][2]
    assert "another schema upgrade is running; waiting up to 60 s for it to finish" in results["second"][1]
    assert "nothing to do: another schema upgrade applied what was outstanding" in results["second"][1]
    assert [row[1] for row in _record(dsn)] == [change.name for change in changes], "each change once"


def test_a_runner_gives_up_after_its_lock_timeout_and_applies_nothing(databases, template) -> None:
    dsn = _dsn_of(databases.create(template=template))
    with psycopg.connect(dsn, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (schema.LOCK_KEY,))
        started = time.monotonic()
        status, _out, err = _run("upgrade", dsn, lock_timeout=1)
        waited = time.monotonic() - started
    assert status == schema.FAILED
    assert "another schema upgrade held the lock for 1 s" in err
    assert 0.9 < waited < 30
    assert not _exists(dsn, "pubdex.schema_changes"), "nothing was applied"


def _command(command: str, database: str, user: str, password: str, *args: str) -> subprocess.CompletedProcess:
    """The command line, as in a container: the libpq environment, and the
    password from a file -- here a pipe, so it is never written anywhere."""
    server = conninfo_to_dict(_dsn())
    env = {key: value for key, value in os.environ.items()
           if key not in {"PGHOST", "PGHOSTADDR", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD",
                          "PEOPLE_DB_DSN", "PEOPLE_DB_PASSWORD_FILE", "PEOPLE_PUBS_VERSION"}}
    reader, writer = os.pipe()
    try:
        os.write(writer, f"{password}\n".encode("utf-8"))
        os.close(writer)
        env.update(PGHOST=server.get("host", "127.0.0.1"), PGPORT=str(server.get("port", "5432")),
                   PGDATABASE=database, PGUSER=user, PEOPLE_PUBS_SKIP_DOTENV="1",
                   PEOPLE_DB_PASSWORD_FILE=f"/dev/fd/{reader}", PEOPLE_PUBS_VERSION=RELEASE)
        return subprocess.run([sys.executable, "-m", "people_pubs.schema", command, *args], cwd=DB_DIR, env=env,
                              pass_fds=(reader,), capture_output=True, text=True, timeout=180)
    finally:
        os.close(reader)


def test_a_non_superuser_owner_upgrades_the_baseline_it_applied(databases) -> None:
    owner, password = databases.role()
    database = databases.create(owner=owner)
    with psycopg.connect(_dsn_of(database, user=owner, password=password)) as conn:
        conn.execute(_baseline_sql())
    superuser = conninfo_to_dict(_dsn())

    refused = _command("upgrade", database, superuser["user"], superuser["password"])
    assert refused.returncode == schema.FAILED
    assert f"refused: the schema upgrade has to run as {owner}, the role that owns the PubDex schema" in refused.stderr
    assert not _exists(_dsn_of(database), "pubdex.schema_changes")

    proc = _command("upgrade", database, owner, password)
    assert proc.returncode == schema.OK, proc.stderr
    assert f"as {owner}" in proc.stdout.splitlines()[0]
    with psycopg.connect(_dsn_of(database)) as conn:
        owners = conn.execute(
            "SELECT DISTINCT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'pubdex' UNION "
            "SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'pubdex'").fetchall()
    assert owners == [(owner,)]
    assert {row[6] for row in _record(_dsn_of(database))} == {owner}
    assert {row[4] for row in _record(_dsn_of(database))} == {RELEASE}
    assert _privilege_problems(_dsn_of(database)) == []
    again = _command("upgrade", database, owner, password)
    assert again.returncode == schema.OK and "nothing to do" in again.stdout
    status = _command("status", database, owner, password)
    assert status.returncode == schema.OK and "nothing is outstanding" in status.stdout
    for proc in (refused, proc, again, status):
        shown = password in proc.stdout + proc.stderr or superuser["password"] in proc.stdout + proc.stderr
        assert not shown, "no password in any output"


def test_a_database_the_command_cannot_place_is_refused(databases, template) -> None:
    empty = _dsn_of(databases.create())
    status, _out, err = _run("upgrade", empty)
    assert status == schema.FAILED and "the database holds no PubDex schema" in err

    partial = _dsn_of(databases.create(template=template))
    with psycopg.connect(partial) as conn:
        conn.execute("DROP TABLE app.member_info_manual CASCADE")
    status, _out, err = _run("upgrade", partial)
    assert status == schema.FAILED and "holds part of the baseline" in err
    assert "but not table app.member_info_manual" in err

    upgraded = _dsn_of(databases.create(template=template))
    assert _run("upgrade", upgraded)[0] == schema.OK
    with psycopg.connect(upgraded) as conn:
        conn.execute("UPDATE pubdex.schema_changes SET sha256 = repeat('0', 64) WHERE number = 1")
    status, _out, err = _run("status", upgraded)
    assert status == schema.FAILED and "the file was changed after the database had it" in err

    later = _dsn_of(databases.create(template=template))
    assert _run("upgrade", later)[0] == schema.OK
    beyond = len(schema.load_changes(schema.SCHEMA_DIR)) + 1
    with psycopg.connect(later) as conn:
        conn.execute("INSERT INTO pubdex.schema_changes (number, name, sha256, backward_compatible, release) "
                     "VALUES (%s, %s, repeat('a', 64), true, 'later')", (beyond, f"{beyond:04d}_later_addition"))
    status, out, _err = _run("upgrade", later)
    assert status == schema.OK
    assert f"1 schema change of a later PubDex release, all backward compatible: {beyond:04d}_later_addition" in out
    with psycopg.connect(later) as conn:
        conn.execute("INSERT INTO pubdex.schema_changes (number, name, sha256, backward_compatible, release) "
                     "VALUES (%s, %s, repeat('b', 64), false, 'later')",
                     (beyond + 1, f"{beyond + 1:04d}_later_rename"))
    status, _out, err = _run("status", later)
    assert status == schema.FAILED
    assert f"not backward compatible: {beyond + 1:04d}_later_rename" in err


def test_the_suite_s_database_has_every_schema_change() -> None:
    superuser = conninfo_to_dict(_dsn())
    proc = _command("status", superuser["dbname"], superuser["user"], superuser["password"])
    assert proc.returncode == schema.OK, proc.stdout + proc.stderr
    assert "nothing is outstanding" in proc.stdout
