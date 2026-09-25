"""Command/option semantics of people_pubs.sync.attribution_rules, offline.

No database: an invalid combination must be rejected before any connection is
opened, --dry-run must never refresh a snapshot or commit, recompute must
never touch the rules, list must stay read-only, and --refresh-only belongs
to recompute alone. The connection factory and the snapshot refresh are
replaced by recording fakes.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from people_pubs.sync import attribution_rules as ar


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn
        self.rowcount = 1

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.conn.executed.append(" ".join(sql.split()))

    def fetchone(self):
        return {"changed": 0}

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, events: list[str]) -> None:
        self.executed: list[str] = []
        self.events = events
        self.autocommit = None
        self.isolation_level = None

    def cursor(self, row_factory=None) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


@pytest.fixture
def harness(monkeypatch):
    """Patch the connection factory and the refresh; return the recorders."""
    events: list[str] = []
    conns: list[_FakeConn] = []

    @contextmanager
    def fake_db_conn(dsn=None):
        conn = _FakeConn(events)
        conns.append(conn)
        events.append("connect")
        yield conn

    def fake_refresh(dsn=None):
        events.append("refresh")

    monkeypatch.setattr(ar, "db_conn", fake_db_conn)
    monkeypatch.setattr(ar, "_refresh_snapshots", fake_refresh)
    return events, conns


def _run(mode, **kw):
    params = dict(dsn=None, affiliations=[], fundings=[], note=None,
                  dry_run=False, refresh=True, refresh_only=False)
    params.update(kw)
    return ar.run(mode=mode, **params)


REJECTED = [
    ["list", "--affiliation", "Example Institute"],
    ["list", "--funding", "EX-1"],
    ["list", "--note", "n"],
    ["list", "--dry-run"],
    ["list", "--no-refresh"],
    ["list", "--refresh-only"],
    ["recompute", "--affiliation", "Example Institute"],
    ["recompute", "--funding", "EX-1"],
    ["recompute", "--note", "n"],
    ["remove", "--affiliation", "Example Institute", "--note", "n"],
    ["set"],
    ["add"],
    ["remove"],
    ["set", "--affiliation", "Example Institute", "--refresh-only"],
    ["add", "--funding", "EX-1", "--refresh-only"],
    ["remove", "--funding", "EX-1", "--refresh-only"],
    ["recompute", "--refresh-only", "--dry-run"],
    ["recompute", "--refresh-only", "--no-refresh"],
]

ACCEPTED = [
    ["list"],
    ["set", "--affiliation", "Example Institute", "--dry-run"],
    ["set", "--affiliation", "Example Institute", "--funding", "EX-1", "--note", "n"],
    ["add", "--funding", "EX-1", "--note", "n", "--no-refresh"],
    ["remove", "--funding", "EX-1"],
    ["remove", "--affiliation", "Example Institute", "--dry-run", "--no-refresh"],
    ["recompute"],
    ["recompute", "--dry-run"],
    ["recompute", "--no-refresh"],
    ["recompute", "--refresh-only"],
]


@pytest.mark.parametrize("argv", REJECTED, ids=[" ".join(a) for a in REJECTED])
def test_parser_rejects_incompatible_combinations(argv, harness) -> None:
    events, _conns = harness
    with pytest.raises(SystemExit) as exc:
        ar._parse_args(argv)
    assert exc.value.code == 2, "argparse usage error"
    assert events == [], "a rejected combination must not connect or refresh"


@pytest.mark.parametrize("argv", ACCEPTED, ids=[" ".join(a) for a in ACCEPTED])
def test_parser_accepts_documented_combinations(argv) -> None:
    args = ar._parse_args(argv)
    assert args.command == argv[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(mode="recompute", dry_run=True, refresh_only=True),
        dict(mode="recompute", refresh=False, refresh_only=True),
        dict(mode="set", affiliations=["Example Institute"], refresh_only=True),
        dict(mode="recompute", affiliations=["Example Institute"]),
        dict(mode="recompute", note="n"),
        dict(mode="list", dry_run=True),
        dict(mode="list", refresh=False),
        dict(mode="list", affiliations=["Example Institute"]),
        dict(mode="remove", fundings=["EX-1"], note="n"),
        dict(mode="set"),
        dict(mode="nonsense"),
    ],
)
def test_run_rejects_invalid_combinations_before_connecting(kwargs, harness) -> None:
    events, _conns = harness
    with pytest.raises(SystemExit):
        _run(**kwargs)
    assert events == [], "run() must refuse before opening a connection or refreshing"


def test_short_pattern_is_rejected_before_connecting(harness) -> None:
    events, _conns = harness
    with pytest.raises(SystemExit):
        _run("set", affiliations=["x"])
    assert events == []


def test_dry_run_rolls_back_and_never_refreshes(harness) -> None:
    events, conns = harness
    assert _run("set", affiliations=["Example Institute"], dry_run=True, refresh=True) == 0
    assert "commit" not in events
    assert "refresh" not in events, "--dry-run must never refresh a snapshot"
    assert events[-1] == "rollback"
    sql = conns[0].executed
    assert any(s == ar.LOCK_SQL for s in sql), "the change runs under the attribution lock"
    assert any("INSERT INTO app.institution_attribution_rules" in s for s in sql)
    assert any(s == ar.RECOMPUTE_SQL for s in sql), "the dry run still reports the recompute"


def test_recompute_never_changes_the_rules(harness) -> None:
    events, conns = harness
    assert _run("recompute", refresh=False) == 0
    sql = conns[0].executed
    assert not any(
        s.startswith(("INSERT INTO app.institution_attribution_rules", "DELETE FROM app.institution_attribution_rules"))
        for s in sql
    ), "recompute must not add, remove or replace rules"
    assert any(s == ar.LOCK_SQL for s in sql)
    assert any(s == ar.RECOMPUTE_SQL for s in sql)
    assert events.count("commit") == 1


def test_apply_rule_changes_refuses_non_rule_commands() -> None:
    with pytest.raises(ValueError):
        ar._apply_rule_changes(_FakeConn([]), mode="recompute", affiliations=[], fundings=[], note=None)


def test_list_is_read_only(harness) -> None:
    events, conns = harness
    assert _run("list") == 0
    sql = conns[0].executed
    assert len(sql) == 1 and sql[0].startswith("SELECT rule_id, rule_kind, pattern, active, note")
    assert "commit" not in events
    assert "refresh" not in events
    assert events[-1] == "rollback"


def test_refresh_only_refreshes_and_nothing_else(harness) -> None:
    events, conns = harness
    assert _run("recompute", refresh_only=True) == 0
    assert events == ["refresh"], "no rule transaction, just the refresh"
    assert conns == []


def test_no_refresh_commits_without_refreshing(harness) -> None:
    events, _conns = harness
    assert _run("add", fundings=["EX-1"], refresh=False) == 0
    assert events == ["connect", "commit"]


def test_set_commits_then_refreshes(harness) -> None:
    events, conns = harness
    assert _run("set", affiliations=["Example Institute"], fundings=["EX-1"], note="why") == 0
    assert events == ["connect", "commit", "refresh"], "refresh follows the committed change"
    sql = conns[0].executed
    assert sql.index(ar.LOCK_SQL) < sql.index(ar.RECOMPUTE_SQL)
    assert any(s.startswith("DELETE FROM app.institution_attribution_rules") for s in sql), "set replaces"


def test_remove_deletes_only_named_rules(harness) -> None:
    events, conns = harness
    assert _run("remove", affiliations=["Example Institute"], refresh=False) == 0
    sql = conns[0].executed
    deletes = [s for s in sql if s.startswith("DELETE FROM app.institution_attribution_rules")]
    assert deletes and all("WHERE rule_kind = %s" in s for s in deletes), "remove never clears the table"
    assert not any(s.startswith("INSERT INTO") for s in sql)


def test_failing_transaction_is_rolled_back_and_not_refreshed(harness, monkeypatch) -> None:
    events, _conns = harness

    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(ar, "_apply_rule_changes", boom)
    with pytest.raises(RuntimeError):
        _run("set", affiliations=["Example Institute"])
    assert "commit" not in events
    assert "refresh" not in events
    assert "rollback" in events
