"""
Roster-import validation and publication-export golden tests (
sections 3 and 9).

Covers, without any DB or network:
- the demo roster fixture stays valid against add_people's CSV contract,
- documented validation behavior for missing/malformed ORCID rows,
- roster input from a file or from standard input (--csv -): decoding, the
  header check, unreadable input, argument conflicts, and messages that never
  quote the roster,
- the deterministic, PII-free CSV export of the canonical publication list.
"""

import contextlib
import csv
import io
import logging
import os
import sys
from pathlib import Path

import pytest

from people_pubs.sync import add_people
from people_pubs.sync.add_people import _prepare_record
from people_pubs.sync.export_publications import (
    EXPORT_COLUMNS,
    sort_publications,
    write_publications_csv,
)

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


# --------------------------------------------------------------------------- #
# Demo roster fixture vs. the importer contract
# --------------------------------------------------------------------------- #

def _roster_rows() -> list[dict]:
    with (GOLDEN / "demo_roster_minimal.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_demo_roster_fixture_parses_cleanly():
    rows = _roster_rows()
    assert len(rows) == 3
    records = [_prepare_record(row, "internal") for row in rows]
    assert [r["orcid"] for r in records] == [
        "0000-0002-1825-0097",  # Josiah Carberry, ORCID's fictional researcher
        "0000-0000-0000-0001",
        "0000-0000-0000-001X",  # checksum-valid synthetic iD
    ]
    assert [r["display_name"] for r in records] == [
        "Josiah Carberry",
        "Ada Example",
        "Bert Example",
    ]
    assert [r["person_kind"] for r in records] == ["internal", "internal", "external"]


def test_demo_roster_fixture_is_public_safe():
    for row in _roster_rows():
        email = (row.get("primary_email") or "").strip()
        if email:
            assert email.endswith("@example.org")


def test_missing_orcid_is_rejected():
    with pytest.raises(ValueError, match="Missing or invalid ORCID"):
        _prepare_record({"orcid": "", "display_name": "No Orcid"}, "internal")


def test_malformed_orcid_is_rejected():
    with pytest.raises(ValueError, match="Missing or invalid ORCID"):
        _prepare_record({"orcid": "not-an-orcid", "display_name": "Bad"}, "internal")


def test_checksum_invalid_orcid_is_rejected():
    # Format-valid shape, wrong ISO 7064 MOD 11-2 check digit (a mistyped iD).
    with pytest.raises(ValueError, match="checksum"):
        _prepare_record({"orcid": "0000-0000-0000-0002", "display_name": "Bad Check"}, "internal")


def test_checksum_valid_orcid_normalizes_including_x_check_digit():
    record = _prepare_record(
        {"orcid": "https://orcid.org/0000-0000-0000-001x", "display_name": "Bert Example"},
        "internal",
    )
    assert record["orcid"] == "0000-0000-0000-001X"


def test_missing_display_name_is_rejected():
    with pytest.raises(ValueError, match="Missing display_name"):
        _prepare_record({"orcid": "0000-0000-0000-0001"}, "internal")


def test_display_name_derived_from_first_and_last():
    rec = _prepare_record(
        {"orcid": "0000-0000-0000-0001", "first_name": "Ada", "last_name": "Example"},
        "internal",
    )
    assert rec["display_name"] == "Ada Example"


def test_invalid_person_kind_is_rejected():
    with pytest.raises(ValueError, match="Invalid person_kind"):
        _prepare_record(
            {
                "orcid": "0000-0000-0000-0001",
                "display_name": "Ada Example",
                "person_kind": "robot",
            },
            "internal",
        )


# --------------------------------------------------------------------------- #
# Roster input: a file, or standard input with --csv -
# --------------------------------------------------------------------------- #

# Synthetic people; the names and addresses must never appear in a message.
_ROSTER = (
    "orcid,display_name,primary_email,person_kind\r\n"
    "0000-0000-0000-0001,Zoë Example,zoe@example.org,internal\r\n"
    "0000-0000-0000-001X,Bert Example,,external\r\n"
)
_PRIVATE = ("Zoë", "Zo", "Bert", "Ada", "example.org")


class _Stdin(io.TextIOWrapper):
    """Standard input holding data; a terminal when tty is set."""

    def __init__(self, data: bytes, tty: bool = False) -> None:
        super().__init__(io.BytesIO(data), encoding="utf-8")
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def _stdin(monkeypatch, data: bytes, tty: bool = False) -> _Stdin:
    stdin = _Stdin(data, tty)
    monkeypatch.setattr(sys, "stdin", stdin)
    return stdin


def _read(path: str, delimiter: str = ",") -> list:
    with contextlib.ExitStack() as stack:
        return list(add_people._open_roster(stack, path, delimiter))


def _assert_quotes_nothing(message: str) -> None:
    for value in _PRIVATE:
        assert value not in message, "the message quotes the roster"


def test_a_roster_on_standard_input_is_read_like_a_file(monkeypatch, tmp_path):
    data = b"\xef\xbb\xbf" + _ROSTER.encode("utf-8")  # a byte-order mark and CRLF line ends
    stdin = _stdin(monkeypatch, data)
    from_stdin = _read("-")
    path = tmp_path / "roster.csv"
    path.write_bytes(data)
    assert from_stdin == _read(str(path))
    assert [row["orcid"] for row in from_stdin] == ["0000-0000-0000-0001", "0000-0000-0000-001X"]
    assert from_stdin[0]["display_name"] == "Zoë Example"
    assert [_prepare_record(row, "internal")["person_kind"] for row in from_stdin] == ["internal", "external"]
    assert not stdin.buffer.closed, "the process's standard input stays open"


def test_the_delimiter_applies_to_standard_input(monkeypatch):
    _stdin(monkeypatch, _ROSTER.replace(",", ";").encode("utf-8"))
    assert [row["orcid"] for row in _read("-", ";")] == ["0000-0000-0000-0001", "0000-0000-0000-001X"]


def test_the_demo_roster_reads_through_the_file_reader():
    rows = list(add_people._iter_csv_records(str(GOLDEN / "demo_roster_minimal.csv"), ","))
    assert rows == _roster_rows()


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "the roster on standard input is empty"),
        (b"\n", "the roster on standard input is empty"),
        (b"display_name,primary_email\nAda Example,ada@example.org\n", "has no orcid column"),
        # A roster without its header: the first person would be read as one.
        (b"0000-0000-0000-0001,Ada Example,ada@example.org\n", "has no orcid column"),
        (_ROSTER.encode("latin-1"), "is not UTF-8 text"),
        (_ROSTER.encode("utf-16"), "is not UTF-8 text"),
    ],
    ids=["empty", "blank-line", "no-orcid-column", "no-header", "latin-1", "utf-16"],
)
def test_a_roster_that_cannot_be_read_stops_before_the_database(monkeypatch, data, message):
    _stdin(monkeypatch, data)
    with pytest.raises(add_people.RosterError, match=message) as excinfo:
        with contextlib.ExitStack() as stack:
            add_people._open_roster(stack, "-", ",")  # the header alone, before any row is used
    _assert_quotes_nothing(str(excinfo.value))


# A row the csv module cannot read: a field beyond its size limit, as a quote
# left open makes of the rest of a roster.
_UNREADABLE_ROW = '0000-0002-1825-0097,"' + "x" * (csv.field_size_limit() + 1) + '",,\r\n'


def test_an_unreadable_row_stops_the_iteration_with_its_line(monkeypatch):
    _stdin(monkeypatch, (_ROSTER + _UNREADABLE_ROW).encode("utf-8"))
    with pytest.raises(add_people.RosterError, match="standard input, line 4: field larger than field limit") as excinfo:
        _read("-")
    _assert_quotes_nothing(str(excinfo.value))


def test_standard_input_from_a_terminal_is_refused(monkeypatch):
    _stdin(monkeypatch, _ROSTER.encode("utf-8"), tty=True)
    with pytest.raises(add_people.RosterError, match=r"--csv - < roster.csv \(with docker compose run, add -T\)"):
        _read("-")


def test_a_missing_or_unreadable_file_is_reported_with_the_alternative(tmp_path):
    with pytest.raises(add_people.RosterError, match="CSV not found"):
        _read(str(tmp_path / "missing.csv"))
    with pytest.raises(add_people.RosterError, match="cannot read the roster .*: Is a directory"):
        _read(str(tmp_path))
    if os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    private = tmp_path / "roster.csv"
    private.write_text(_ROSTER, encoding="utf-8")
    private.chmod(0o000)
    try:
        with pytest.raises(add_people.RosterError, match="permission denied") as excinfo:
            _read(str(private))
        assert "--csv - < roster.csv" in str(excinfo.value), "the message names standard input"
    finally:
        private.chmod(0o600)


class _Conn:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def _offline(monkeypatch) -> tuple:
    """add_people without a database: every valid record counts as inserted."""
    conn, seen = _Conn(), []

    @contextlib.contextmanager
    def db_conn(dsn):
        yield conn

    def ensure_person(conn, record, *, update_existing, dry_run, stats, oc):
        stats.total += 1
        stats.inserted += 1
        seen.append(record["orcid"])

    monkeypatch.setattr(add_people, "db_conn", db_conn)
    monkeypatch.setattr(add_people, "_ensure_person", ensure_person)
    return conn, seen


def test_main_imports_a_roster_from_standard_input(monkeypatch, caplog):
    conn, seen = _offline(monkeypatch)
    _stdin(monkeypatch, _ROSTER.encode("utf-8"))
    with caplog.at_level(logging.INFO):
        add_people.main(["--csv", "-", "--no-orcid-lookup"])
    assert seen == ["0000-0000-0000-0001", "0000-0000-0000-001X"] and conn.commits == 1
    assert "add_people: total=2 inserted=2" in caplog.text
    _assert_quotes_nothing(caplog.text)


def test_a_row_that_cannot_be_read_writes_nothing(monkeypatch):
    conn, seen = _offline(monkeypatch)
    _stdin(monkeypatch, (_ROSTER + _UNREADABLE_ROW).encode("utf-8"))
    with pytest.raises(SystemExit, match=r"line 4: field larger than field limit \(\d+\); nothing was written") as excinfo:
        add_people.main(["--csv", "-", "--no-orcid-lookup"])
    assert len(seen) == 2 and conn.commits == 0, "the rows before it are not committed"
    _assert_quotes_nothing(str(excinfo.value))


@pytest.mark.parametrize("data", [b"", b"display_name\nAda Example\n"])
def test_main_refuses_an_unusable_roster_before_connecting(monkeypatch, data):
    monkeypatch.setattr(add_people, "db_conn", lambda dsn: pytest.fail("connected to the database"))
    _stdin(monkeypatch, data)
    with pytest.raises(SystemExit) as excinfo:
        add_people.main(["--csv", "-", "--no-orcid-lookup"])
    assert "the roster on standard input" in str(excinfo.value.code)


def test_standard_input_and_a_single_person_conflict(monkeypatch):
    stdin = _stdin(monkeypatch, _ROSTER.encode("utf-8"))
    with pytest.raises(SystemExit, match=r"Use either --csv or --orcid \(not both\)"):
        add_people.main(["--csv", "-", "--orcid", "0000-0000-0000-0001"])
    assert stdin.buffer.tell() == 0, "standard input is not read"


def test_an_invalid_row_is_reported_by_number_without_its_contents(monkeypatch, caplog):
    _offline(monkeypatch)
    rows = [
        {"orcid": "", "display_name": "Ada Example", "primary_email": "ada@example.org"},
        {"orcid": "0000-0000-0000-0001", "display_name": "Zoë Example", "person_kind": "Bert"},
        {"orcid": "0000-0000-0000-001X", "display_name": "Bert Example"},
    ]
    with caplog.at_level(logging.WARNING):
        stats = add_people.run_add_people(dsn=None, records=rows, default_person_kind="internal",
                                          update_existing=False, dry_run=False, lookup_orcid=False)
    assert stats.skipped_invalid == 2 and stats.inserted == 1
    assert "Skipping invalid row 1: Missing or invalid ORCID" in caplog.text
    assert "Skipping invalid row 2: Invalid person_kind" in caplog.text
    _assert_quotes_nothing(caplog.text)


# --------------------------------------------------------------------------- #
# Publication CSV export: golden file + determinism + PII stance
# --------------------------------------------------------------------------- #

_EXPORT_ROWS = [
    {
        "pub_id": 3,
        "doi": None,
        "title": "Older untitled venue-less record",
        "year": 2019,
        "venue": None,
        "source_of_truth": "orcid",
        "internal_authors": None,
    },
    {
        "pub_id": 1,
        "doi": "10.5555/pubdex-fixture-001",
        "title": "Synthetic Collective Behaviour Paper",
        "year": 2024,
        "venue": "Journal of Synthetic Metadata",
        "source_of_truth": "crossref+orcid",
        "internal_authors": "Ada Example",
    },
    {
        "pub_id": 2,
        "doi": "10.5555/pubdex-fixture-002",
        "title": "A second synthetic paper",
        "year": 2024,
        "venue": "Journal of Synthetic Metadata",
        "source_of_truth": "crossref",
        "internal_authors": "Ada Example, Cleo Example",
    },
    {
        "pub_id": 4,
        "doi": "10.5555/pubdex-fixture-004",
        "title": "Yearless provisional record",
        "year": None,
        "venue": None,
        "source_of_truth": "datacite",
        "internal_authors": None,
    },
]


def _export_text(rows) -> str:
    out = io.StringIO()
    write_publications_csv(sort_publications(rows), out)
    return out.getvalue()


def test_export_matches_golden_file():
    golden = (GOLDEN / "export_publications_minimal.csv").read_text()
    assert _export_text(_EXPORT_ROWS) == golden


def test_export_is_order_independent():
    assert _export_text(_EXPORT_ROWS) == _export_text(list(reversed(_EXPORT_ROWS)))


def test_export_columns_contain_no_contact_pii():
    forbidden = {"email", "primary_email", "emails", "phone", "address", "room"}
    assert not forbidden & set(EXPORT_COLUMNS)
