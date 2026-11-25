"""people_pubs.schema, offline: the schema upgrade's rules, decisions and messages.

Scratch directories of schema changes in tmp_path stand in for DB/schema/, and
a fake session for the database: it holds a record and the baseline's facts,
counts the lock, and applies a change by adding its row, or fails one on
request. That proves without a server how a file is read and when it is
refused -- its name, its opening comment and declaration, statements that end
the transaction or cannot run in one -- how the statements are split, which
statements the compatibility heuristic refuses, how a database is placed and
when it is refused, that changes are applied in order, each once, under the
lock, that a failed change ends the run, and what each case prints and
returns. The command line's usage errors, a broken directory and a password
file the command cannot use stop it before it connects. DB/tests/integration/
test_schema_runner_integration.py runs the same command on a real server.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import psycopg
import pytest

from people_pubs import schema

DB_DIR = Path(__file__).resolve().parents[1]
HEADER = "-- Adds a probe.\n-- backward-compatible: yes\n"


def _write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _change(number: int, compatible: bool = True, body: str = "SELECT 1;\n") -> schema.Change:
    text = f"-- Change {number}.\n-- backward-compatible: {'yes' if compatible else 'no'}\n{body}"
    return schema.Change(number, f"{number:04d}_change_{number}", hashlib.sha256(text.encode()).hexdigest(),
                         compatible, f"Change {number}.", text)


def _recorded(change: schema.Change, release: str = "0.2.0") -> schema.Recorded:
    return schema.Recorded(change.number, change.name, change.sha256, change.backward_compatible, release,
                           datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc), "postgres")


BASELINE = schema.Facts(schema.BASELINE_SCHEMAS, tuple(f"{k} {s}.{n}" for k, s, n in schema.BASELINE_MARKERS),
                        False, "postgres", "postgres")


# --- statements ------------------------------------------------------------------

def test_statements_split_at_semicolons_outside_comments_strings_names_and_bodies() -> None:
    text = (
        "-- a comment; with a semicolon\n"
        "CREATE TABLE app.t (note text DEFAULT 'a;b', \"odd;name\" int);\n"
        "/* a /* nested */ comment; */ COMMENT ON TABLE app.t IS E'it\\'s; fine';\n"
        "DO $$ BEGIN PERFORM 1; END $$;\n"
        "CREATE FUNCTION app.f() RETURNS int LANGUAGE plpgsql AS $body$ BEGIN RETURN 1; END $body$;\n"
        "CREATE FUNCTION app.g() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT CASE WHEN true THEN 1 END; END;\n"
        "SELECT 'it''s'\n"
    )
    found = schema.statements(text)
    assert [(s.line, s.words) for s in found] == [
        (2, "CREATE TABLE APP.T (NOTE TEXT DEFAULT '', \"\" INT)"),
        (3, "COMMENT ON TABLE APP.T IS E''"),
        (4, "DO $$"),
        (5, "CREATE FUNCTION APP.F() RETURNS INT LANGUAGE PLPGSQL AS $$"),
        (6, "CREATE FUNCTION APP.G() RETURNS INT LANGUAGE SQL BEGIN ATOMIC SELECT CASE WHEN TRUE THEN 1 END; END"),
        (7, "SELECT ''"),
    ]


def test_a_parameter_or_a_dollar_inside_a_name_is_no_dollar_quote() -> None:
    assert [s.words for s in schema.statements("SELECT $1, a$b$ FROM t; SELECT 2;")] == [
        "SELECT $1, A$B$ FROM T", "SELECT 2"]


@pytest.mark.parametrize("text, message", [
    ("SELECT 'open;\n", "line 1: a string is not closed"),
    ('SELECT "open;\n', "line 1: a quoted name is not closed"),
    ("SELECT 1;\n/* open\n", "line 2: a /* comment is not closed"),
    ("SELECT 1;\nDO $x$ BEGIN END $y$;\n", "line 2: a dollar-quoted string is not closed"),
])
def test_an_unclosed_quote_or_comment_is_reported_with_its_line(text: str, message: str) -> None:
    with pytest.raises(schema.SqlScanError, match=re.escape(message)):
        schema.statements(text)


# --- the transaction rules -------------------------------------------------------

@pytest.mark.parametrize("statement", [
    "BEGIN", "begin transaction", "START TRANSACTION", "COMMIT", "END", "ROLLBACK", "ABORT",
    "SAVEPOINT s", "RELEASE SAVEPOINT s", "PREPARE TRANSACTION 'x'", "COMMIT PREPARED 'x'",
])
def test_a_statement_that_controls_the_transaction_is_refused(statement: str) -> None:
    words = schema.statements(statement)[0].words
    assert "controls the transaction" in schema.transaction_problem(words)


@pytest.mark.parametrize("statement", [
    "VACUUM app.people", "CREATE DATABASE other", "DROP DATABASE other", "ALTER DATABASE x SET TABLESPACE t",
    "CREATE TABLESPACE t LOCATION '/x'", "DROP TABLESPACE t", "ALTER SYSTEM SET work_mem = '8MB'",
    "CREATE INDEX CONCURRENTLY i ON app.people (person_id)", "DROP INDEX CONCURRENTLY app.i",
    "REINDEX TABLE CONCURRENTLY app.people", "REINDEX SCHEMA app", "CLUSTER", "CLUSTER VERBOSE",
    "CREATE SUBSCRIPTION s CONNECTION 'x' PUBLICATION p", "DISCARD ALL",
    "ALTER TABLE app.p DETACH PARTITION app.c CONCURRENTLY",
])
def test_a_statement_postgresql_refuses_in_a_transaction_block_is_refused(statement: str) -> None:
    words = schema.statements(statement)[0].words
    assert "cannot run inside a transaction block" in schema.transaction_problem(words)


@pytest.mark.parametrize("statement", [
    "CREATE TABLE app.t (id bigserial PRIMARY KEY)", "COMMENT ON TABLE app.t IS 'commit; vacuum'",
    "REFRESH MATERIALIZED VIEW CONCURRENTLY biblio.v", "ALTER TYPE app.k ADD VALUE 'x'",
    "CLUSTER app.t USING t_pkey", "DO $$ BEGIN COMMIT; END $$", "SET LOCAL lock_timeout = '1s'",
    "CREATE INDEX i ON app.t (id)", "REINDEX INDEX app.database_idx",
])
def test_a_statement_that_runs_in_a_transaction_block_is_accepted(statement: str) -> None:
    assert schema.transaction_problem(schema.statements(statement)[0].words) is None


# --- the compatibility heuristic -------------------------------------------------

@pytest.mark.parametrize("statement, finding", [
    ("DROP TABLE app.t", "drops something"),
    ("ALTER TABLE app.t DROP COLUMN c", "drops something"),
    ("ALTER TABLE app.t RENAME COLUMN a TO b", "renames something"),
    ("ALTER TABLE app.t RENAME TO u", "renames something"),
    ("ALTER TABLE app.t ALTER COLUMN c TYPE bigint", "changes a column's type"),
    ("ALTER TABLE app.t ALTER c SET DATA TYPE bigint", "changes a column's type"),
    ("ALTER TABLE app.t ALTER COLUMN c SET NOT NULL", "makes a column NOT NULL"),
    ("ALTER TABLE app.t ADD COLUMN c int NOT NULL", "adds a NOT NULL column or constraint without a DEFAULT"),
])
def test_the_heuristic_finds_what_the_preceding_release_could_not_survive(statement: str, finding: str) -> None:
    assert schema.compatibility_findings(f"{HEADER}SELECT 1;\n{statement};\n") == [f"line 4: {finding}"]


@pytest.mark.parametrize("statement", [
    "CREATE TABLE app.t (id bigserial PRIMARY KEY, kind text NOT NULL, type text)",
    "ALTER TABLE app.t ADD COLUMN note text",
    "ALTER TABLE app.t ADD COLUMN flag boolean NOT NULL DEFAULT false",
    "ALTER TYPE app.k ADD VALUE 'x'",
    "CREATE INDEX t_kind ON app.t (kind)",
    "COMMENT ON TABLE app.t IS 'never dropped, never renamed'",
    "GRANT USAGE, SELECT ON SEQUENCE app.t_id_seq TO app_writer",
    "REVOKE EXECUTE ON FUNCTION app.f() FROM PUBLIC",
])
def test_the_heuristic_accepts_additions(statement: str) -> None:
    assert schema.compatibility_findings(f"{HEADER}{statement};\n") == []


# --- one file ----------------------------------------------------------------------

def test_a_file_is_read_with_its_number_name_digest_declaration_and_summary(tmp_path: Path) -> None:
    text = "-- Adds a probe table.\n--\n-- backward-compatible: no\n-- More words.\nCREATE TABLE app.p (id int);\n"
    path = _write(tmp_path, "0007_probe_table.sql", text)
    change = schema.read_change(path)
    assert change == schema.Change(7, "0007_probe_table", hashlib.sha256(text.encode()).hexdigest(), False,
                                   "Adds a probe table.", text)
    assert change.label == "0007_probe_table (not backward compatible)"


@pytest.mark.parametrize("name", [
    "1_short.sql", "00001_long.sql", "0001-dash.sql", "0001_Upper.sql", "0001_.sql", "0001_probe.SQL",
    "0001_probe.sql.bak", "README.md",
])
def test_a_file_with_another_name_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(schema.ChangeFileError, match="a schema change is a file NNNN_<name>.sql"):
        schema.read_change(_write(tmp_path, name, f"{HEADER}SELECT 1;\n"))


@pytest.mark.parametrize("text, message", [
    ("CREATE TABLE app.p (id int);\n", "its first line has to be a comment that says what the change does"),
    ("--\n-- backward-compatible: yes\nSELECT 1;\n", "its first line has to be a comment"),
    ("-- backward-compatible: yes\nSELECT 1;\n", "its first line has to be a comment"),
    ("-- Adds.\nSELECT 1;\n", "holds 0 such line(s)"),
    ("-- Adds.\n\n-- backward-compatible: yes\nSELECT 1;\n", "holds 0 such line(s)"),
    ("-- Adds.\n-- backward-compatible: yes\n-- backward-compatible: no\nSELECT 1;\n", "holds 2 such line(s)"),
    ("-- Adds.\n-- backward-compatible: maybe\nSELECT 1;\n", "holds 1 such line(s), saying 'maybe'"),
    ("-- Adds.\n-- backward-compatible: yes\n", "holds no statement"),
    ("-- Adds.\n-- backward-compatible: yes\nSELECT 1;\nCOMMIT;\n", "line 4: COMMIT controls the transaction"),
    ("-- Adds.\n-- backward-compatible: yes\nVACUUM;\n", "line 3: VACUUM cannot run inside a transaction"),
    ("-- Adds.\n-- backward-compatible: yes\nSELECT 'x\n", "line 3: a string is not closed"),
])
def test_a_file_that_breaks_a_rule_is_refused_with_the_reason(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(schema.ChangeFileError) as raised:
        schema.read_change(_write(tmp_path, "0001_probe.sql", text))
    assert str(raised.value).startswith("0001_probe.sql")
    assert message in str(raised.value)


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "0001_probe.sql"
    path.write_bytes(HEADER.encode() + b"SELECT '\xff';\n")
    with pytest.raises(schema.ChangeFileError, match="0001_probe.sql: is not UTF-8 text"):
        schema.read_change(path)


# --- the directory -----------------------------------------------------------------

def test_the_changes_come_in_number_order(tmp_path: Path) -> None:
    for number in (2, 1, 3):
        _write(tmp_path, f"{number:04d}_c.sql", f"-- Change {number}.\n-- backward-compatible: yes\nSELECT 1;\n")
    assert [c.name for c in schema.load_changes(tmp_path)] == ["0001_c", "0002_c", "0003_c"]


@pytest.mark.parametrize("names, held", [
    (["0001_a.sql", "0003_c.sql"], "0001, 0003"),
    (["0002_b.sql"], "0002"),
    (["0001_a.sql", "0001_b.sql"], "0001, 0001"),
], ids=["gap", "not-from-0001", "repeat"])
def test_a_gap_or_a_repeat_is_refused(tmp_path: Path, names: List[str], held: str) -> None:
    for name in names:
        _write(tmp_path, name, f"{HEADER}SELECT 1;\n")
    with pytest.raises(schema.ChangeFileError, match=f"without a gap or a repeat; .* holds {held}$"):
        schema.load_changes(tmp_path)


def test_an_empty_directory_a_subdirectory_or_a_link_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(schema.ChangeFileError, match="holds no schema change"):
        schema.load_changes(empty)
    with pytest.raises(schema.ChangeFileError, match="cannot list"):
        schema.load_changes(tmp_path / "absent")
    nested = tmp_path / "nested"
    _write(nested, "0001_a.sql", f"{HEADER}SELECT 1;\n")
    (nested / "0002_b.sql").mkdir()
    with pytest.raises(schema.ChangeFileError, match="0002_b.sql: not a regular file"):
        schema.load_changes(nested)
    linked = tmp_path / "linked"
    _write(linked, "0001_a.sql", f"{HEADER}SELECT 1;\n")
    (linked / "0002_b.sql").symlink_to(linked / "0001_a.sql")
    with pytest.raises(schema.ChangeFileError, match="0002_b.sql: not a regular file"):
        schema.load_changes(linked)


# --- placing a database ------------------------------------------------------------

CHANGES = [_change(1), _change(2), _change(3)]


def test_the_baseline_has_every_change_outstanding() -> None:
    assert schema.place_baseline(CHANGES, BASELINE) == schema.Placement((), tuple(CHANGES), ())


def test_a_database_without_the_baseline_is_refused() -> None:
    with pytest.raises(schema.Refused) as raised:
        schema.place_baseline(CHANGES, schema.Facts((), (), False, None, "postgres"))
    assert raised.value.reason == schema.SCHEMA_UPGRADE_REQUIRED
    assert "holds no PubDex schema" in str(raised.value)


def test_part_of_the_baseline_is_refused_naming_what_is_there_and_what_is_not() -> None:
    facts = schema.Facts(("app", "biblio"), ("table biblio.publications",), False, "postgres", "postgres")
    with pytest.raises(schema.Refused) as raised:
        schema.place_baseline(CHANGES, facts)
    assert raised.value.reason == schema.SCHEMA_NOT_SUPPORTED
    message = str(raised.value)
    assert "holds part of the baseline" in message
    assert "it has app, biblio, table biblio.publications but not activity, pii, staging_raw, " \
           "function biblio.refresh_serving_snapshots, table app.member_info_manual" in message


@pytest.mark.parametrize("recorded, outstanding", [
    ([CHANGES[0]], CHANGES[1:]),
    (CHANGES[:2], CHANGES[2:]),
    (CHANGES, []),
])
def test_what_the_record_lacks_is_outstanding(recorded, outstanding) -> None:
    placement = schema.place(CHANGES, [_recorded(c) for c in recorded])
    assert placement.outstanding == tuple(outstanding) and placement.later == ()


def test_later_backward_compatible_changes_are_accepted_and_kept_apart() -> None:
    later = _recorded(_change(4))
    placement = schema.place(CHANGES, [*(_recorded(c) for c in CHANGES), later])
    assert placement.outstanding == () and placement.later == (later,)


@pytest.mark.parametrize("rows, message", [
    (lambda: [], "holds no row"),
    (lambda: [_recorded(CHANGES[0]), _recorded(CHANGES[2])], "has a gap: it lacks 0002 but holds 0001, 0003"),
    (lambda: [_recorded(_change(1, body="SELECT 2;\n"))],
     "0001_change_1 was applied from a file with the SHA-256 .* the file was changed after the database had it"),
    (lambda: [schema.Recorded(1, "0001_other", CHANGES[0].sha256, True, "0.2.0",
                              datetime(2026, 11, 17, tzinfo=timezone.utc), "postgres")],
     "schema change 0001 is 0001_other, but this release's is 0001_change_1"),
    (lambda: [*(_recorded(c) for c in CHANGES), _recorded(_change(4)), _recorded(_change(5, compatible=False))],
     "later PubDex release that are not backward compatible: 0005_change_5"),
], ids=["emptied", "gap", "edited", "another-change", "later-incompatible"])
def test_a_record_this_release_cannot_place_is_refused(rows, message: str) -> None:
    with pytest.raises(schema.Refused, match=message) as raised:
        schema.place(CHANGES, rows())
    assert raised.value.reason == schema.SCHEMA_NOT_SUPPORTED


# --- the command on a fake database ------------------------------------------------

@dataclass
class FakeSession:
    """The database as the command sees it: facts, a record, a lock."""
    facts_: schema.Facts = BASELINE
    rows: Optional[List[schema.Recorded]] = None  # None: no record
    lock_free: bool = True
    lock_obtained_after_wait: bool = True
    fail: Dict[str, str] = field(default_factory=dict)  # change name -> SQLSTATE of its failure
    on_wait: Optional[object] = None  # called while "waiting": another runner's work
    calls: List[str] = field(default_factory=list)

    def where(self) -> str:
        return "database people_db on db.example.org port 5432 as postgres"

    def facts(self) -> schema.Facts:
        return schema.Facts(self.facts_.schemas, self.facts_.markers, self.rows is not None,
                            self.facts_.owner, self.facts_.role)

    def record(self) -> List[schema.Recorded]:
        return list(self.rows or [])

    def set_lock_timeout(self, seconds: int) -> None:
        self.calls.append(f"lock_timeout {seconds}")

    def try_lock(self) -> bool:
        self.calls.append("try_lock")
        return self.lock_free

    def lock(self) -> None:
        self.calls.append("lock")
        if self.on_wait:
            self.on_wait(self)
        if not self.lock_obtained_after_wait:
            raise schema.LockTimeout()

    def unlock(self) -> None:
        self.calls.append("unlock")

    def apply(self, change: schema.Change, release: str) -> None:
        self.calls.append(f"apply {change.name}")
        if change.name in self.fail:
            error = psycopg.errors.lookup(self.fail[change.name])
            raise error("division by zero")
        self.rows = [*(self.rows or []), _recorded(change, release)]


def _run(command: str, session: FakeSession, changes=CHANGES, **options):
    out, err = io.StringIO(), io.StringIO()
    status = schema.run(command, changes, session, "0.2.0", out=out, err=err, clock=iter(range(100)).__next__,
                        **options)
    return status, out.getvalue().splitlines(), err.getvalue().splitlines()


def test_status_of_the_baseline_lists_what_is_outstanding_and_returns_3() -> None:
    session = FakeSession()
    status, out, err = _run("status", session)
    assert status == schema.OUTSTANDING and err == []
    assert out == [
        "schema: PubDex 0.2.0, 3 schema changes, the last 0003_change_3; "
        "database people_db on db.example.org port 5432 as postgres",
        "schema: the database holds the baseline and has had no schema change",
        "schema: outstanding: 0001_change_1 (backward compatible): Change 1.",
        "schema: outstanding: 0002_change_2 (backward compatible): Change 2.",
        "schema: outstanding: 0003_change_3 (backward compatible): Change 3.",
        "schema: 3 schema changes outstanding; `python -m people_pubs.schema upgrade` applies what is outstanding",
    ]
    assert session.calls == [], "status takes no lock and applies nothing"


def test_status_of_an_upgraded_database_lists_the_record_and_returns_0() -> None:
    session = FakeSession(rows=[_recorded(c, "0.1.9") for c in CHANGES] + [_recorded(_change(4))])
    status, out, _err = _run("status", session)
    assert status == schema.OK
    assert out[1] == ("schema: recorded: 0001_change_1, backward compatible, applied 2026-11-17 08:00:00 UTC "
                      "as postgres by PubDex 0.1.9")
    assert out[-2:] == [
        "schema: the database also has 1 schema change of a later PubDex release, all backward compatible: "
        "0004_change_4",
        "schema: nothing is outstanding: the database has every schema change of PubDex 0.2.0, up to 0003_change_3",
    ]


def test_upgrade_applies_what_is_outstanding_in_order_under_the_lock() -> None:
    session = FakeSession(rows=[_recorded(CHANGES[0])])
    status, out, err = _run("upgrade", session, lock_timeout=7)
    assert status == schema.OK and err == []
    assert session.calls == ["lock_timeout 7", "try_lock", "apply 0002_change_2", "apply 0003_change_3", "unlock"]
    assert [row.name for row in session.rows] == ["0001_change_1", "0002_change_2", "0003_change_3"]
    assert [row.release for row in session.rows] == ["0.2.0"] * 3
    assert out[1:] == [
        "schema: applying 0002_change_2 (backward compatible): Change 2.",
        "schema: applied 0002_change_2 in 1.00 s",
        "schema: applying 0003_change_3 (backward compatible): Change 3.",
        "schema: applied 0003_change_3 in 1.00 s",
        "schema: done: applied 2 schema changes; the database has every schema change of PubDex 0.2.0, "
        "up to 0003_change_3",
    ]


def test_a_second_upgrade_changes_nothing_and_takes_no_lock() -> None:
    session = FakeSession()
    assert _run("upgrade", session)[0] == schema.OK
    rows, session.calls = list(session.rows), []
    status, out, _err = _run("upgrade", session)
    assert status == schema.OK and session.calls == [] and session.rows == rows
    assert out[-1] == "schema: nothing to do: the database has every schema change of PubDex 0.2.0, " \
                      "up to 0003_change_3"


def test_a_runner_that_waits_reads_the_record_again_under_the_lock() -> None:
    def other_runner(session: FakeSession) -> None:
        session.rows = [_recorded(c) for c in CHANGES]

    session = FakeSession(lock_free=False, on_wait=other_runner)
    status, out, _err = _run("upgrade", session, lock_timeout=30)
    assert status == schema.OK
    assert session.calls == ["lock_timeout 30", "try_lock", "lock", "unlock"], "nothing applied twice"
    assert "schema: another schema upgrade is running; waiting up to 30 s for it to finish" in out
    assert out[-1].startswith("schema: nothing to do: another schema upgrade applied what was outstanding")


def test_a_runner_that_waits_too_long_applies_nothing() -> None:
    session = FakeSession(lock_free=False, lock_obtained_after_wait=False)
    status, _out, err = _run("upgrade", session, lock_timeout=2)
    assert status == schema.FAILED
    assert session.rows is None and "unlock" not in session.calls
    assert err == ["schema: refused: another schema upgrade held the lock for 2 s, the most --lock-timeout "
                   "allows, and nothing was applied; run the schema upgrade again once it has finished"]


def test_a_failed_change_ends_the_run_and_the_next_run_continues_with_it() -> None:
    session = FakeSession(fail={"0002_change_2": "22012"})
    status, out, err = _run("upgrade", session)
    assert status == schema.FAILED
    assert [row.name for row in session.rows] == ["0001_change_1"], "the change before it stays recorded"
    assert "apply 0003_change_3" not in session.calls and session.calls[-1] == "unlock"
    assert out[-1] == "schema: applying 0002_change_2 (backward compatible): Change 2."
    assert err == [
        "schema: schema change 0002_change_2 failed and was rolled back, nothing of it remains: "
        "division by zero (SQLSTATE 22012)",
        "schema: what this run applied before it stays applied and recorded: 0001_change_1",
        "schema: run the schema upgrade again once the cause is fixed; it continues with 0002_change_2",
    ]
    session.fail = {}
    assert _run("upgrade", session)[0] == schema.OK
    assert [row.name for row in session.rows] == ["0001_change_1", "0002_change_2", "0003_change_3"]


def test_only_the_owner_of_the_baseline_may_upgrade_it() -> None:
    session = FakeSession(facts_=schema.Facts(BASELINE.schemas, BASELINE.markers, False, "schema_admin",
                                              "app_writer"))
    status, _out, err = _run("upgrade", session)
    assert status == schema.FAILED and session.calls == []
    assert err == ["schema: refused: the schema upgrade has to run as schema_admin, the role that owns the PubDex "
                   "schema and whose default privileges give new tables to the application roles; this "
                   "connection is the role app_writer"]
    # With nothing outstanding another role changes nothing, so it is told so.
    session.rows = [_recorded(c) for c in CHANGES]
    assert _run("upgrade", session)[0] == schema.OK


@pytest.mark.parametrize("command", ["upgrade", "status"])
def test_a_database_that_cannot_be_placed_is_refused_on_standard_error(command: str) -> None:
    session = FakeSession(rows=[_recorded(CHANGES[0]), _recorded(CHANGES[2])])
    status, out, err = _run(command, session)
    assert status == schema.FAILED and len(out) == 1 and session.calls == []
    assert err == ["schema: refused: the record of schema changes has a gap: it lacks 0002 but holds 0001, 0003, "
                   "so the database's schema cannot be known"]


# --- the command line -------------------------------------------------------------

@pytest.mark.parametrize("argv", [[], ["migrate"], ["upgrade", "--wait", "-1"], ["upgrade", "--wait", "soon"],
                                  ["upgrade", "--lock-timeout", "0"], ["status", "--lock-timeout", "5"]])
def test_usage_errors_exit_2_before_anything_else(monkeypatch: pytest.MonkeyPatch, argv: List[str]) -> None:
    monkeypatch.setattr(schema, "connect", lambda *a, **k: pytest.fail("connected"))
    with pytest.raises(SystemExit) as raised:
        schema.main(argv, out=io.StringIO(), err=io.StringIO())
    assert raised.value.code == schema.USAGE


def test_unusable_schema_changes_stop_the_command_before_it_connects(monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path: Path) -> None:
    _write(tmp_path, "0001_a.sql", f"{HEADER}SELECT 1;\n")
    _write(tmp_path, "0003_c.sql", f"{HEADER}SELECT 1;\n")
    monkeypatch.setattr(schema, "SCHEMA_DIR", tmp_path)
    monkeypatch.setattr(schema, "connect", lambda *a, **k: pytest.fail("connected"))
    err = io.StringIO()
    assert schema.main(["upgrade"], out=io.StringIO(), err=err) == schema.FAILED
    assert err.getvalue().startswith("schema: the schema changes of this release cannot be used: the schema "
                                     "changes have to be numbered from 0001 without a gap or a repeat")


def test_a_failed_connection_is_reported_with_libpq_s_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise psycopg.OperationalError('connection failed: connection to server at "192.0.2.1", port 5432 '
                                       "failed: Connection refused\n\tIs the server running on that host?")

    monkeypatch.setattr(schema, "connect", refuse)
    err = io.StringIO()
    assert schema.main(["status"], out=io.StringIO(), err=err) == schema.FAILED
    assert err.getvalue() == ('schema: cannot connect to the database: connection failed: connection to server '
                              'at "192.0.2.1", port 5432 failed: Connection refused Is the server running on '
                              "that host?\n")


def _command(env: Dict[str, str]) -> subprocess.CompletedProcess:
    base = {key: value for key, value in os.environ.items()
            if not key.startswith(("PG", "PEOPLE_DB_")) and key != "PEOPLE_PUBS_VERSION"}
    return subprocess.run([sys.executable, "-m", "people_pubs.schema", "status", "--wait", "0"], cwd=DB_DIR,
                          env={**base, "PEOPLE_PUBS_SKIP_DOTENV": "1", **env}, capture_output=True, text=True,
                          timeout=60)


@pytest.mark.parametrize("content, message", [
    (None, "PEOPLE_DB_PASSWORD_FILE names a file that does not exist"),
    ("", "the file PEOPLE_DB_PASSWORD_FILE names is empty"),
    ("example-first-line\nexample-second-line\n", "the file PEOPLE_DB_PASSWORD_FILE names holds more than one line"),
], ids=["missing", "empty", "two-lines"])
def test_a_password_file_it_cannot_use_stops_it_with_status_2_naming_the_variable_only(
        tmp_path: Path, content: Optional[str], message: str) -> None:
    path = tmp_path / "password"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    proc = _command({"PEOPLE_DB_PASSWORD_FILE": str(path), "PGHOST": "192.0.2.1"})
    assert proc.returncode == schema.USAGE
    assert message in proc.stderr
    assert str(path) not in proc.stdout + proc.stderr and "example-" not in proc.stdout + proc.stderr


def test_the_release_is_the_image_s_version_or_the_checkout_s(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    monkeypatch.setenv(schema.VERSION_VARIABLE, "0.2.0-test.a")
    assert schema.release_version() == "0.2.0-test.a"
    monkeypatch.delenv(schema.VERSION_VARIABLE)
    assert schema.release_version() == (DB_DIR.parent / "VERSION").read_text(encoding="utf-8").strip()
    monkeypatch.setattr(schema, "VERSION_FILE", tmp_path / "VERSION")
    assert schema.release_version() == schema.UNKNOWN_VERSION
