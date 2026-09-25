"""Two-connection coverage for the coordination between attribution rule
changes and concurrent publication writers (DB/init/010, "Coordination
between rule changes and publication writers").

Every scenario drives the two sides explicitly: one side holds its
transaction open at a chosen point, the other side runs in a thread and is
observed to block on a lock (through pg_stat_activity, never through elapsed
time) before the first side commits. After all transactions have finished the
cached flag of every touched row — and of every row in the table — is
compared with biblio.institution_attribution_signal(), the invariant the lock
exists to protect. Both directions are covered, rules being added
(attribution appears) and removed (attribution disappears), for inserts,
payload updates and concurrent rule edits, plus the real command against an
in-flight writer and the isolation-level guard.

Publication writes go through upsert_publication(), the shared ingestion
entry point. Synthetic payloads and a fictional institution only.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from people_pubs.db.publications import upsert_publication

pytestmark = pytest.mark.integration

INSTITUTE = "Institute for Example Studies"
OTHER_TERM = "Example Funding Council"
BLOCK_TIMEOUT = 20.0


def _dsn() -> str:
    dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
    if not dsn:
        pytest.skip("set PEOPLE_PUBS_INTEGRATION_DSN to run disposable-DB integration tests")
    return dsn


def _connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(_dsn(), row_factory=dict_row, autocommit=autocommit)


def _payload(term: str) -> dict:
    return {"author": [{"affiliation": [{"name": f"Department of Synthetic Data, {term}"}]}]}


def _pub(doi: str, term: str) -> dict:
    return {
        "doi": doi,
        "title": f"concurrency fixture {doi}",
        "year": 2024,
        "venue": "Journal of Synthetic Metadata",
        "raw_crossref_json": _payload(term),
        "is_preprint": False,
    }


def _set_rules(rules: list[tuple[str, str]]) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM app.institution_attribution_rules")
        for kind, pattern in rules:
            conn.execute(
                "INSERT INTO app.institution_attribution_rules (rule_kind, pattern) VALUES (%s, %s)",
                (kind, pattern),
            )
        conn.execute("SELECT biblio.recompute_institution_attribution()")
        conn.commit()


def _cleanup(slug: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM biblio.publications WHERE doi LIKE %s", (f"10.5555/conc-{slug}-%",))
        conn.execute("DELETE FROM app.institution_attribution_rules")
        conn.execute("SELECT biblio.recompute_institution_attribution()")
        conn.commit()


def _flag_and_signal(doi: str) -> tuple[Optional[bool], bool]:
    with _connect(autocommit=True) as conn:
        row = conn.execute(
            "SELECT institution_attributed AS flag, "
            "biblio.institution_attribution_signal(raw_json) AS signal "
            "FROM biblio.publications WHERE doi = %s",
            (doi,),
        ).fetchone()
    assert row is not None, f"{doi} must exist after the writer committed"
    return row["flag"], row["signal"]


def _assert_cache_consistent(*dois: str, expected: Optional[bool] = None) -> None:
    """The invariant: every cached flag equals the signal under the committed rules."""
    for doi in dois:
        flag, signal = _flag_and_signal(doi)
        assert flag == signal, f"{doi}: cached flag {flag!r} disagrees with the signal {signal!r}"
        if expected is not None:
            assert flag is expected, f"{doi}: expected {expected!r}, cached {flag!r}"
    with _connect(autocommit=True) as conn:
        stale = conn.execute(
            "SELECT count(*) AS n FROM biblio.publications "
            "WHERE institution_attributed IS DISTINCT FROM biblio.institution_attribution_signal(raw_json)"
        ).fetchone()["n"]
    assert stale == 0, f"{stale} row(s) have a cached flag that disagrees with the committed rules"


def _wait_until_blocked(pid: int) -> str:
    """Poll pg_stat_activity until the backend waits on a heavyweight lock.

    Returns the wait event ('advisory' for the attribution lock, or a row lock
    event when the writer first collides with a row the recompute updated).
    """
    deadline = time.monotonic() + BLOCK_TIMEOUT
    with _connect(autocommit=True) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                (pid,),
            ).fetchone()
            if row and row["wait_event_type"] == "Lock":
                return str(row["wait_event"])
            time.sleep(0.02)
    pytest.fail(f"backend {pid} never blocked on a lock within {BLOCK_TIMEOUT}s")


class _Side:
    """Runs one connection's work in a thread and records when it finished."""

    def __init__(self, work: Callable[[psycopg.Connection], None]) -> None:
        self._work = work
        self.pid: Optional[int] = None
        self.started = threading.Event()
        self.finished = threading.Event()
        self.error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            with _connect() as conn:
                self.pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
                self.started.set()
                self._work(conn)
                conn.commit()
        except BaseException as exc:  # re-raised in the test thread
            self.error = exc
        finally:
            self.started.set()
            self.finished.set()

    def start(self) -> "_Side":
        self._thread.start()
        assert self.started.wait(BLOCK_TIMEOUT), "side never started"
        assert self.pid is not None, f"side failed before connecting: {self.error!r}"
        return self

    def join(self) -> None:
        assert self.finished.wait(BLOCK_TIMEOUT), "side never finished after the lock was released"
        self._thread.join(BLOCK_TIMEOUT)
        if self.error is not None:
            raise self.error


def _open_rule_change(conn: psycopg.Connection, *, add: list[str] = (), remove: list[str] = ()) -> int:
    """A rule change with its recompute, left uncommitted on `conn`."""
    for pattern in add:
        conn.execute(
            "INSERT INTO app.institution_attribution_rules (rule_kind, pattern) VALUES ('affiliation', %s)",
            (pattern,),
        )
    for pattern in remove:
        conn.execute("DELETE FROM app.institution_attribution_rules WHERE pattern = %s", (pattern,))
    return int(conn.execute("SELECT biblio.recompute_institution_attribution() AS n").fetchone()["n"])


@pytest.mark.parametrize("direction", ["add", "remove"])
def test_insert_during_rule_change_waits_and_uses_the_committed_rules(direction: str) -> None:
    """The reviewed race: A recomputes under new rules, B inserts a matching
    publication before A commits. B must block until A commits and then
    compute its flag from the committed rules."""
    slug = uuid4().hex[:10]
    doi = f"10.5555/conc-{slug}-insert"
    try:
        _set_rules([] if direction == "add" else [("affiliation", INSTITUTE)])
        editor = _connect()
        if direction == "add":
            _open_rule_change(editor, add=[INSTITUTE])
        else:
            _open_rule_change(editor, remove=[INSTITUTE])

        writer = _Side(lambda conn: upsert_publication(conn, _pub(doi, INSTITUTE))).start()
        event = _wait_until_blocked(writer.pid)
        assert event == "advisory", f"the writer must wait on the attribution lock, saw {event}"
        assert not writer.finished.is_set(), "the writer must not commit before the rule change"

        editor.commit()
        editor.close()
        writer.join()
        _assert_cache_consistent(doi, expected=(direction == "add"))
    finally:
        _cleanup(slug)


@pytest.mark.parametrize("direction", ["add", "remove"])
def test_rule_change_during_in_flight_insert_waits_for_the_writer(direction: str) -> None:
    """The reverse order: B holds an uncommitted matching insert, A's rule
    change must wait for B and its recompute must then cover B's row."""
    slug = uuid4().hex[:10]
    doi = f"10.5555/conc-{slug}-inflight"
    try:
        _set_rules([] if direction == "add" else [("affiliation", INSTITUTE)])
        writer = _connect()
        upsert_publication(writer, _pub(doi, INSTITUTE))

        changed: dict = {}

        def edit(conn: psycopg.Connection) -> None:
            if direction == "add":
                changed["n"] = _open_rule_change(conn, add=[INSTITUTE])
            else:
                changed["n"] = _open_rule_change(conn, remove=[INSTITUTE])

        editor = _Side(edit).start()
        event = _wait_until_blocked(editor.pid)
        assert event == "advisory", f"the rule change must wait on the attribution lock, saw {event}"
        assert not editor.finished.is_set()

        writer.commit()
        writer.close()
        editor.join()
        assert changed["n"] == 1, "the recompute must see the row the writer committed while it waited"
        _assert_cache_consistent(doi, expected=(direction == "add"))
    finally:
        _cleanup(slug)


@pytest.mark.parametrize("direction", ["add", "remove"])
def test_payload_update_during_rule_change_uses_the_committed_rules(direction: str) -> None:
    """An existing publication whose payload is rewritten (the ON CONFLICT
    update path, which fires the trigger through UPDATE OF raw_json) while a
    rule change is open."""
    slug = uuid4().hex[:10]
    doi = f"10.5555/conc-{slug}-update"
    try:
        if direction == "add":
            _set_rules([])
            with _connect() as conn:
                upsert_publication(conn, _pub(doi, OTHER_TERM))
                conn.commit()
            editor = _connect()
            _open_rule_change(editor, add=[INSTITUTE])
            new_term = INSTITUTE  # the update makes the payload match the rule being added
        else:
            _set_rules([("affiliation", INSTITUTE)])
            with _connect() as conn:
                upsert_publication(conn, _pub(doi, INSTITUTE))
                conn.commit()
            editor = _connect()
            _open_rule_change(editor, remove=[INSTITUTE])
            new_term = INSTITUTE  # still names the institute; the rule is going away

        writer = _Side(lambda conn: upsert_publication(conn, _pub(doi, new_term))).start()
        # In the remove direction the recompute already updated this row, so the
        # writer first waits on the row lock; either way it waits on A.
        _wait_until_blocked(writer.pid)
        assert not writer.finished.is_set(), "the update must not commit before the rule change"

        editor.commit()
        editor.close()
        writer.join()
        _assert_cache_consistent(doi, expected=(direction == "add"))
    finally:
        _cleanup(slug)


def test_concurrent_rule_edits_serialise_and_both_recompute_correctly() -> None:
    """Two rule editors: the second waits for the first, then recomputes under
    the union of both changes."""
    slug = uuid4().hex[:10]
    doi_a = f"10.5555/conc-{slug}-rule-a"
    doi_b = f"10.5555/conc-{slug}-rule-b"
    doi_none = f"10.5555/conc-{slug}-rule-none"
    try:
        _set_rules([])
        with _connect() as conn:
            upsert_publication(conn, _pub(doi_a, INSTITUTE))
            upsert_publication(conn, _pub(doi_b, OTHER_TERM))
            upsert_publication(conn, _pub(doi_none, "Unrelated Department"))
            conn.commit()

        first = _connect()
        _open_rule_change(first, add=[INSTITUTE])

        second = _Side(lambda conn: _open_rule_change(conn, add=[OTHER_TERM])).start()
        event = _wait_until_blocked(second.pid)
        assert event == "advisory", f"the second editor must wait on the attribution lock, saw {event}"
        assert not second.finished.is_set()

        first.commit()
        first.close()
        second.join()

        with _connect(autocommit=True) as conn:
            patterns = [
                r["pattern"] for r in conn.execute(
                    "SELECT pattern FROM app.institution_attribution_rules ORDER BY pattern"
                ).fetchall()
            ]
        assert patterns == sorted([INSTITUTE, OTHER_TERM]), "both edits must survive"
        _assert_cache_consistent(doi_a, doi_b, expected=True)
        _assert_cache_consistent(doi_none, expected=False)
    finally:
        _cleanup(slug)


def test_command_waits_for_an_in_flight_writer_and_covers_its_row() -> None:
    """The real command against an uncommitted ingestion write."""
    from people_pubs.sync import attribution_rules

    slug = uuid4().hex[:10]
    doi = f"10.5555/conc-{slug}-command"
    dsn = _dsn()
    try:
        _set_rules([])
        writer = _connect()
        upsert_publication(writer, _pub(doi, INSTITUTE))

        outcome: dict = {}

        def command() -> None:
            try:
                outcome["rc"] = attribution_rules.run(
                    mode="add", dsn=dsn, affiliations=[INSTITUTE], fundings=[],
                    note=None, dry_run=False, refresh=False, refresh_only=False,
                )
            except BaseException as exc:  # re-raised below
                outcome["error"] = exc

        thread = threading.Thread(target=command, daemon=True)
        thread.start()

        # Find the command's backend: it is the one waiting inside the lock call.
        deadline = time.monotonic() + BLOCK_TIMEOUT
        blocked = False
        with _connect(autocommit=True) as probe:
            while time.monotonic() < deadline and not blocked:
                row = probe.execute(
                    "SELECT 1 FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND wait_event = 'advisory' "
                    "  AND query = %s",
                    (attribution_rules.LOCK_SQL,),
                ).fetchone()
                blocked = row is not None
                time.sleep(0.02)
        assert blocked, "the command must wait on the attribution lock while the writer is in flight"
        assert thread.is_alive(), "the command must not finish before the writer commits"

        writer.commit()
        writer.close()
        thread.join(BLOCK_TIMEOUT)
        assert not thread.is_alive(), "the command must finish once the writer has committed"
        if "error" in outcome:
            raise outcome["error"]
        assert outcome["rc"] == 0
        _assert_cache_consistent(doi, expected=True)
    finally:
        _cleanup(slug)


@pytest.mark.parametrize(
    "level", [psycopg.IsolationLevel.REPEATABLE_READ, psycopg.IsolationLevel.SERIALIZABLE]
)
def test_writers_outside_read_committed_are_refused(level) -> None:
    """The guarantee needs a post-wait snapshot, which only READ COMMITTED
    gives; stricter levels are refused instead of caching a stale value."""
    slug = uuid4().hex[:10]
    doi = f"10.5555/conc-{slug}-isolation"
    try:
        with _connect() as conn:
            conn.isolation_level = level
            with pytest.raises(psycopg.errors.InvalidTransactionState):
                upsert_publication(conn, _pub(doi, INSTITUTE))
            conn.rollback()
        with _connect(autocommit=True) as conn:
            assert conn.execute(
                "SELECT 1 FROM biblio.publications WHERE doi = %s", (doi,)
            ).fetchone() is None, "the refused write must not persist"
    finally:
        _cleanup(slug)
