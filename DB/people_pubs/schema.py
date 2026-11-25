"""
people_pubs.schema

The schema upgrade of a PubDex database, and its report:

  python -m people_pubs.schema upgrade   applies the schema changes the database has not had yet
  python -m people_pubs.schema status    reports the recorded and the outstanding ones; changes nothing

Every database starts from the baseline, the schema DB/init/ creates when
PostgreSQL initialises it (in the chart's external mode its setup applies the
same files). Every later change of its structure is a schema change: one file
NNNN_<name>.sql in DB/schema/ -- /opt/pubdex/schema in the image, next to the
package, as DB/init/ is -- numbered from 0001 without a gap, <name> in
lower-case letters, digits and underscores. A new installation is the
baseline plus every schema change, which this command applies exactly as it
does on an upgrade.

Each file opens with a block of comment lines. The first says what the change
does, and exactly one line of the block reads `-- backward-compatible: yes`
or `-- backward-compatible: no`: whether the preceding PubDex release still
works on the changed schema. A change runs in a transaction of its own, so
its file neither ends nor opens a transaction (BEGIN, COMMIT, ROLLBACK,
SAVEPOINT and their kind) and holds no statement PostgreSQL refuses in a
transaction block (VACUUM, CREATE INDEX CONCURRENTLY, ALTER SYSTEM and the
others transaction_problem() lists). A file that breaks one of these rules
stops the command before it connects.

Schema change 0001 creates the record, the table pubdex.schema_changes: one
row per change applied, with its number and name, the SHA-256 of its file,
whether it is backward compatible, the PubDex release that applied it, when,
and as which role. The command places a database before it changes anything:

  - Without the record the database has had no schema change, and it has to
    hold the baseline, which the five schemas of DB/init/ and three of its
    objects mark (the markers of the chart's external-setup.sh). A database
    with none of them holds no PubDex schema; one with only some holds part
    of the baseline, or another application's objects. Both are refused.
  - With the record, its numbers have to run from 1 without a gap, and each
    recorded change that this release also ships has to have the name and
    the SHA-256 of this release's file: another change under the same number,
    or a file changed after the database had it, is refused. Recorded changes
    beyond this release's last come from a later release; they are accepted,
    as after an image rollback, when every one of them is backward
    compatible, and refused otherwise.

What is outstanding is applied in number order, as the role that owns the
PubDex schema: the role that applied the baseline, whose default privileges
give each new table to the application roles. Any other role is refused,
even a superuser. The command first takes a session advisory lock, waiting at
most --lock-timeout seconds for another schema upgrade to finish, and reads
the record again; the same timeout bounds each change's wait for a lock on a
table. Each change runs in a transaction of its own: its file, then its row
in the record, then the commit. A change that fails is rolled back whole and
ends the run; the changes applied before it stay applied and recorded, and
the next run continues with it. A run with nothing outstanding changes
nothing and takes no lock.

It connects as the ingestion commands do: to PEOPLE_DB_DSN or else through
libpq's environment (PGHOST, PGPORT, PGDATABASE, PGUSER, PGSSLMODE and the
others), with the password from the file PEOPLE_DB_PASSWORD_FILE names, and
with DB/.env filling gaps unless PEOPLE_PUBS_SKIP_DOTENV is set, as the image
sets it (people_pubs.config); the server has to authenticate it by SCRAM
(people_pubs.connection_security). It waits up to --wait seconds for the
server to accept connections, as libpq's ping sees it. It prints no password
and no SQL text: a failed change is reported by the server's message.

The release it records is PEOPLE_PUBS_VERSION, which the image sets from the
version ./build_image.sh builds it as, or else the checkout's VERSION file.

Exit status: 0 when the database has every schema change of this release, after
the upgrade or before it; 1 when the files or the database are refused, the
lock is not obtained, the connection fails or a change fails; 2 for a usage
or configuration error. status exits 0 when nothing is outstanding and 3 when
a schema upgrade would apply something.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, TextIO, Tuple

import psycopg
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import tuple_row

from people_pubs import connection_security

#: DB/schema/ next to the package: /opt/pubdex/schema in the image.
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"
#: The repository's VERSION file, for a run from a checkout.
VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"
#: The PubDex version of this code; the image sets it (Dockerfile).
VERSION_VARIABLE = "PEOPLE_PUBS_VERSION"
UNKNOWN_VERSION = "unknown"

FILE_NAME = re.compile(r"(?P<number>[0-9]{4})_[a-z0-9_]+\.sql")
DECLARATION = re.compile(r"--\s*backward-compatible\s*:(?P<value>.*)")
SUMMARY = re.compile(r"--\s*\S.*")

#: The session advisory lock that serialises schema upgrades: the first eight
#: bytes of the SHA-256 of "pubdex.schema_changes", as a signed integer. The
#: other advisory locks of PubDex take 32-bit keys.
LOCK_KEY = 770005998694741599
DEFAULT_WAIT_SECONDS = 120
DEFAULT_LOCK_TIMEOUT_SECONDS = 300
#: How long one ping may take, unless the connection settings say otherwise.
PING_TIMEOUT_SECONDS = 5

#: The baseline's schemas and the three objects that mark it, as
#: charts/pubdex/files/external-setup.sh recognises it.
BASELINE_SCHEMAS = ("app", "biblio", "activity", "pii", "staging_raw")
BASELINE_MARKERS = (
    ("table", "biblio", "publications"),
    ("function", "biblio", "refresh_serving_snapshots"),
    ("table", "app", "member_info_manual"),
)
#: The schema whose owner applied the baseline and owns the default privileges.
OWNER_SCHEMA = "biblio"

COMMAND = "python -m people_pubs.schema"
APPLICATION_NAME = "pubdex-schema-upgrade"

#: Exit statuses.
OK = 0
FAILED = 1
USAGE = 2
OUTSTANDING = 3

#: Why a database is not one this release can work with, as the application
#: reports it: it needs a schema upgrade, or no schema upgrade of this release
#: can bring it to this release's schema.
SCHEMA_UPGRADE_REQUIRED = "schema_upgrade_required"
SCHEMA_NOT_SUPPORTED = "schema_not_supported"


class SchemaError(Exception):
    """Why the command stops; the message names files, numbers and roles,
    never a password or SQL text."""


class ChangeFileError(SchemaError):
    """A file of the schema changes breaks a rule."""


class SqlScanError(ChangeFileError):
    """A file cannot be read as SQL: a quote or comment that is not closed."""


class Refused(SchemaError):
    """The database cannot be placed, or this role may not upgrade it."""

    def __init__(self, message: str, reason: str = SCHEMA_NOT_SUPPORTED) -> None:
        super().__init__(message)
        self.reason = reason


class LockTimeout(SchemaError):
    """Another schema upgrade held the lock longer than allowed."""


class ChangeFailed(SchemaError):
    """A schema change failed and was rolled back."""

    def __init__(self, change: "Change", applied: int, detail: str) -> None:
        super().__init__(f"schema change {change.name} failed and was rolled back, nothing of it remains: {detail}")
        self.change = change
        self.applied = applied


# --- The files -----------------------------------------------------------------

@dataclass(frozen=True)
class Statement:
    """A top-level statement of a file: the line it starts on, and its words
    outside comments, with every string, quoted name and dollar-quoted body
    replaced by '', "" or $$, upper-cased and single-spaced."""
    line: int
    words: str


_WORD = re.compile(r"[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_$\u0080-\U0010ffff]*")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")
_ROUTINE = re.compile(r"CREATE (OR REPLACE )?(FUNCTION|PROCEDURE)\b")


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _closing_quote(text: str, start: int, quote: str, backslashes: bool) -> int:
    """The offset after the quote that closes the one at start."""
    i = start + 1
    while i < len(text):
        if backslashes and text[i] == "\\":
            i += 2
        elif text[i] == quote:
            if text.startswith(quote, i + 1):
                i += 2
            else:
                return i + 1
        else:
            i += 1
    what = "string" if quote == "'" else "quoted name"
    raise SqlScanError(f"line {_line(text, start)}: a {what} is not closed")


def _closing_comment(text: str, start: int) -> int:
    """The offset after the */ that closes the /* at start; they nest."""
    depth, i = 1, start + 2
    while depth:
        opening, closing = text.find("/*", i), text.find("*/", i)
        if closing < 0:
            raise SqlScanError(f"line {_line(text, start)}: a /* comment is not closed")
        if 0 <= opening < closing:
            depth, i = depth + 1, opening + 2
        else:
            depth, i = depth - 1, closing + 2
    return i


def statements(text: str) -> List[Statement]:
    """The top-level statements of text, split at semicolons outside
    comments, strings, quoted names and dollar-quoted bodies, and outside a
    function's BEGIN ATOMIC ... END body."""
    found: List[Statement] = []
    words: List[str] = []
    first: Optional[int] = None
    depth = 0  # BEGIN ATOMIC bodies and the CASE expressions in them
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            words.append(" ")
            continue
        if text.startswith("/*", i):
            i = _closing_comment(text, i)
            words.append(" ")
            continue
        if c == ";" and depth == 0:
            if first is not None:
                found.append(Statement(_line(text, first), " ".join("".join(words).split()).upper()))
            words, first = [], None
            i += 1
            continue
        if c.isspace():
            words.append(" ")
            i += 1
            continue
        if first is None:
            first = i
        word = _WORD.match(text, i)
        if word:
            upper = word.group().upper()
            if upper == "BEGIN" and _ROUTINE.match(" ".join("".join(words).split()).upper()):
                depth += 1
            elif upper == "CASE" and depth:
                depth += 1
            elif upper == "END" and depth:
                depth -= 1
            words.append(word.group())
            i = word.end()
            continue
        if c == "'":
            # E'...' takes backslash escapes; E is then a word of its own.
            escaped = i > 0 and text[i - 1] in "eE" and (i < 2 or not _WORD.match(text[i - 2]))
            i = _closing_quote(text, i, "'", escaped)
            words.append("''")
            continue
        if c == '"':
            i = _closing_quote(text, i, '"', False)
            words.append('""')
            continue
        tag = _DOLLAR_TAG.match(text, i) if c == "$" else None
        if tag:
            end = text.find(tag.group(), tag.end())
            if end < 0:
                raise SqlScanError(f"line {_line(text, i)}: a dollar-quoted string is not closed")
            i = end + len(tag.group())
            words.append("$$")
            continue
        words.append(c)
        i += 1
    if first is not None:
        found.append(Statement(_line(text, first), " ".join("".join(words).split()).upper()))
    return found


#: Statements that end or open a transaction: the schema upgrade owns it.
_TRANSACTION_CONTROL = re.compile(
    r"(BEGIN|START TRANSACTION|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE TRANSACTION)\b")
#: Statements PostgreSQL 16 refuses inside a transaction block.
_OUTSIDE_A_TRANSACTION = (
    re.compile(r"VACUUM\b"),
    re.compile(r"(CREATE|DROP) DATABASE\b"),
    re.compile(r"ALTER DATABASE\b.*\bSET TABLESPACE\b"),
    re.compile(r"(CREATE|DROP) TABLESPACE\b"),
    re.compile(r"ALTER SYSTEM\b"),
    re.compile(r"(CREATE|ALTER|DROP) SUBSCRIPTION\b"),
    re.compile(r"REINDEX\b.*\b(SCHEMA|DATABASE|SYSTEM)\b"),
    re.compile(r"CLUSTER( VERBOSE)?$"),
    re.compile(r"DISCARD ALL\b"),
)


def transaction_problem(words: str) -> Optional[str]:
    """Why a statement, given as Statement.words, cannot be part of a
    schema change's transaction; None when it can."""
    if _TRANSACTION_CONTROL.match(words):
        return (f"{words.split()[0]} controls the transaction, which the schema upgrade owns: each change "
                "runs in one transaction of its own")
    if any(rule.match(words) for rule in _OUTSIDE_A_TRANSACTION) or (
            re.search(r"\bCONCURRENTLY\b", words) and not words.startswith("REFRESH MATERIALIZED VIEW")):
        return f"{' '.join(words.split()[:3])} cannot run inside a transaction block, and each change runs in one"
    return None


#: What a backward-compatible change may not do to what the preceding
#: release reads or writes. A heuristic over the statements' words, which
#: the offline tests apply to every file that declares itself backward
#: compatible; a dollar-quoted body is not looked into.
_INCOMPATIBLE = (
    (re.compile(r"\bDROP\b"), "drops something"),
    (re.compile(r"\bRENAME\b"), "renames something"),
    (re.compile(r"\bALTER (COLUMN )?\S+ (SET DATA )?TYPE\b"), "changes a column's type"),
    (re.compile(r"\bSET NOT NULL\b"), "makes a column NOT NULL"),
)


def compatibility_findings(text: str) -> List[str]:
    """Statements of a schema change that a backward-compatible change may
    not hold, one finding each, by the rules above and one more: a new NOT
    NULL constraint without a DEFAULT in an ALTER TABLE."""
    findings = []
    for statement in statements(text):
        for rule, what in _INCOMPATIBLE:
            if rule.search(statement.words):
                findings.append(f"line {statement.line}: {what}")
        words = statement.words
        if (words.startswith("ALTER TABLE") and re.search(r"\bADD\b", words)
                and re.search(r"\bNOT NULL\b", words) and not re.search(r"\bDEFAULT\b", words)):
            findings.append(f"line {statement.line}: adds a NOT NULL column or constraint without a DEFAULT")
    return findings


@dataclass(frozen=True)
class Change:
    """One schema change, as its file holds it."""
    number: int
    name: str
    sha256: str
    backward_compatible: bool
    summary: str
    text: str

    @property
    def label(self) -> str:
        return f"{self.name} ({_compatibility(self.backward_compatible)})"


def _compatibility(backward_compatible: bool) -> str:
    return "backward compatible" if backward_compatible else "not backward compatible"


def read_change(path: Path) -> Change:
    """The schema change in path, checked against the rules of a file."""
    name = path.name
    match = FILE_NAME.fullmatch(name)
    if not match:
        raise ChangeFileError(f"{name}: a schema change is a file NNNN_<name>.sql, with four digits and a name of "
                              "lower-case letters, digits and underscores")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ChangeFileError(f"{name}: cannot be read: {exc.strerror or exc}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ChangeFileError(f"{name}: is not UTF-8 text") from None
    block = []
    for line in text.splitlines():
        if not line.startswith("--"):
            break
        block.append(line.rstrip())
    if not block or not SUMMARY.fullmatch(block[0]) or DECLARATION.fullmatch(block[0]):
        raise ChangeFileError(f"{name}: its first line has to be a comment that says what the change does")
    declarations = [found.group("value").strip() for found in map(DECLARATION.fullmatch, block) if found]
    if len(declarations) != 1 or declarations[0] not in ("yes", "no"):
        raise ChangeFileError(
            f"{name}: its opening comment block has to hold exactly one line '-- backward-compatible: yes' or "
            f"'-- backward-compatible: no'; it holds {len(declarations)} such line(s)"
            + (f", saying {declarations[0]!r}" if len(declarations) == 1 else ""))
    try:
        found = statements(text)
    except SqlScanError as exc:
        raise ChangeFileError(f"{name}: {exc}") from None
    if not found:
        raise ChangeFileError(f"{name}: holds no statement")
    for statement in found:
        problem = transaction_problem(statement.words)
        if problem:
            raise ChangeFileError(f"{name}, line {statement.line}: {problem}")
    return Change(
        number=int(match.group("number")),
        name=name[:-len(".sql")],
        sha256=hashlib.sha256(data).hexdigest(),
        backward_compatible=declarations[0] == "yes",
        summary=re.sub(r"^--\s*", "", block[0]),
        text=text,
    )


def load_changes(directory: Path) -> List[Change]:
    """Every schema change in directory, in number order: 0001 to N, each
    number once, and nothing else in the directory."""
    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        raise ChangeFileError(f"cannot list {directory}: {exc.strerror or exc}") from None
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise ChangeFileError(f"{entry.name}: not a regular file; {directory} holds schema changes only")
    changes = [read_change(entry) for entry in entries]
    if not changes:
        raise ChangeFileError(f"{directory} holds no schema change")
    numbers = [change.number for change in changes]
    if numbers != list(range(1, len(changes) + 1)):
        raise ChangeFileError(f"the schema changes have to be numbered from 0001 without a gap or a repeat; "
                              f"{directory} holds {_numbers(numbers)}")
    return changes


def _numbers(numbers: Sequence[int]) -> str:
    return ", ".join(f"{number:04d}" for number in numbers)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# --- Placing a database --------------------------------------------------------

@dataclass(frozen=True)
class Recorded:
    """A row of pubdex.schema_changes."""
    number: int
    name: str
    sha256: str
    backward_compatible: bool
    release: str
    applied_at: datetime
    applied_by: str

    def describe(self) -> str:
        when = self.applied_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return (f"{self.name}, {_compatibility(self.backward_compatible)}, applied {when} as {self.applied_by} "
                f"by PubDex {self.release}")


@dataclass(frozen=True)
class Facts:
    """What placing a database needs to know about it."""
    schemas: Tuple[str, ...]
    markers: Tuple[str, ...]
    has_record: bool
    owner: Optional[str]
    role: str


@dataclass(frozen=True)
class Placement:
    """Where a database stands: its recorded changes (none for the baseline),
    the changes of this release it has not had, and the recorded changes of a
    later release."""
    recorded: Tuple[Recorded, ...]
    outstanding: Tuple[Change, ...]
    later: Tuple[Recorded, ...]


def _marker_label(kind: str, schema: str, name: str) -> str:
    return f"{kind} {schema}.{name}"


def place_baseline(changes: Sequence[Change], facts: Facts) -> Placement:
    """A database without the record: the baseline, or refused."""
    expected = [*BASELINE_SCHEMAS, *(_marker_label(*marker) for marker in BASELINE_MARKERS)]
    present = [*facts.schemas, *facts.markers]
    if not present:
        raise Refused("the database holds no PubDex schema. The schema upgrade starts from the baseline, which "
                      "is applied when the database is initialised: by PostgreSQL from DB/init/, or by the "
                      "chart's setup in external mode", SCHEMA_UPGRADE_REQUIRED)
    missing = [item for item in expected if item not in present]
    if missing:
        raise Refused(f"the database holds part of the baseline, or another application's objects under its "
                      f"names: it has {', '.join(present)} but not {', '.join(missing)}. PubDex needs a database "
                      "of its own that holds the whole baseline")
    return Placement((), tuple(changes), ())


def place(changes: Sequence[Change], recorded: Sequence[Recorded]) -> Placement:
    """A database with the record: its recorded changes against this
    release's, or refused."""
    if not recorded:
        raise Refused("the record pubdex.schema_changes holds no row, although schema change 0001 records itself "
                      "as it creates it: the record was emptied, and the database's schema cannot be known")
    numbers = [row.number for row in recorded]
    if numbers != list(range(1, len(numbers) + 1)):
        missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
        raise Refused(f"the record of schema changes has a gap: it lacks {_numbers(missing)} but holds "
                      f"{_numbers(numbers)}, so the database's schema cannot be known")
    for row in recorded[:len(changes)]:
        change = changes[row.number - 1]
        if row.name != change.name:
            raise Refused(f"the database's schema change {row.number:04d} is {row.name}, but this release's is "
                          f"{change.name}: the database had another change under that number")
        if row.sha256 != change.sha256:
            raise Refused(f"the database's schema change {row.name} was applied from a file with the SHA-256 "
                          f"{row.sha256}, but this release's file has {change.sha256}: the file was changed "
                          "after the database had it")
    later = tuple(recorded[len(changes):])
    incompatible = [row.name for row in later if not row.backward_compatible]
    if incompatible:
        raise Refused(f"the database has schema changes of a later PubDex release that are not backward "
                      f"compatible: {', '.join(incompatible)}. This release cannot work on that schema: use the "
                      "later release, or restore the backup taken before the upgrade to it")
    return Placement(tuple(recorded), tuple(changes[len(recorded):]), later)


def require_supported(changes: Sequence[Change], recorded: Optional[Sequence[Recorded]]) -> Placement:
    """The application's schema contract; None means there is no record.

    The runner also uses place(), but can upgrade an older database. The
    application needs every change it ships before it can read or write.
    """
    version = "unrecorded" if recorded is None else f"{max((row.number for row in recorded), default=0):04d}"
    supported = f"{len(changes):04d} and later versions with only backward-compatible schema changes"
    prefix = f"database schema version {version}; this application supports {supported}. "
    command = "Run the schema upgrade: python -m people_pubs.schema upgrade."
    try:
        if recorded is None:
            raise Refused("the database has no recorded schema version", SCHEMA_UPGRADE_REQUIRED)
        placement = place(changes, recorded)
        if placement.outstanding:
            raise Refused("missing schema changes: " + ", ".join(c.name for c in placement.outstanding),
                          SCHEMA_UPGRADE_REQUIRED)
        return placement
    except Refused as exc:
        raise Refused(f"{prefix}{exc}. {command}", exc.reason) from None


def check_supported(conn: psycopg.Connection, changes: Optional[Sequence[Change]] = None) -> Placement:
    """Read the schema record on this session, without changing its row factory."""
    if changes is None:
        changes = load_changes(SCHEMA_DIR)
    try:
        with conn.cursor(row_factory=tuple_row) as cursor:
            has_record = cursor.execute("SELECT to_regclass('pubdex.schema_changes') IS NOT NULL").fetchone()[0]
            recorded = [Recorded(*row) for row in cursor.execute(_RECORD_QUERY).fetchall()] if has_record else None
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable,
            psycopg.errors.InvalidSchemaName, psycopg.errors.UndefinedColumn):
        raise Refused("database schema version unreadable; this application supports "
                      f"{len(changes):04d} and later versions with only backward-compatible schema changes. "
                      "The schema record cannot be read. Run the schema upgrade: "
                      "python -m people_pubs.schema upgrade.") from None
    return require_supported(changes, recorded)


# --- The database ----------------------------------------------------------------

_FACTS_QUERY = """
SELECT
  ARRAY(SELECT s FROM unnest(%(schemas)s::text[]) WITH ORDINALITY AS u(s, i)
        WHERE EXISTS (SELECT FROM pg_catalog.pg_namespace n WHERE n.nspname = u.s) ORDER BY u.i) AS schemas,
  ARRAY(SELECT m.label FROM unnest(%(kinds)s::text[], %(namespaces)s::text[], %(names)s::text[], %(labels)s::text[])
          WITH ORDINALITY AS m(kind, namespace, name, label, i)
        WHERE CASE m.kind
          WHEN 'function' THEN EXISTS (
            SELECT FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = m.namespace AND p.proname = m.name)
          ELSE EXISTS (
            SELECT FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = m.namespace AND c.relname = m.name)
          END
        ORDER BY m.i) AS markers,
  EXISTS (SELECT FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = 'pubdex' AND c.relname = 'schema_changes') AS has_record,
  (SELECT pg_catalog.pg_get_userbyid(n.nspowner) FROM pg_catalog.pg_namespace n
   WHERE n.nspname = %(owner_schema)s) AS owner,
  current_user AS role
"""

_RECORD_QUERY = """
SELECT number, name, sha256, backward_compatible, release, applied_at, applied_by
FROM pubdex.schema_changes ORDER BY number
"""

_RECORD_INSERT = """
INSERT INTO pubdex.schema_changes (number, name, sha256, backward_compatible, release)
VALUES (%s, %s, %s, %s, %s)
"""


class Session:
    """The queries of the schema upgrade, on one connection in autocommit
    mode: the session lock is the connection's, and each change opens a
    transaction of its own."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def where(self) -> str:
        info = self.conn.info
        return f"database {info.dbname} on {info.host} port {info.port} as {info.user}"

    def facts(self) -> Facts:
        row = self.conn.execute(_FACTS_QUERY, {
            "schemas": list(BASELINE_SCHEMAS),
            "kinds": [kind for kind, _schema, _name in BASELINE_MARKERS],
            "namespaces": [schema for _kind, schema, _name in BASELINE_MARKERS],
            "names": [name for _kind, _schema, name in BASELINE_MARKERS],
            "labels": [_marker_label(*marker) for marker in BASELINE_MARKERS],
            "owner_schema": OWNER_SCHEMA,
        }).fetchone()
        return Facts(tuple(row[0]), tuple(row[1]), row[2], row[3], row[4])

    def record(self) -> List[Recorded]:
        return [Recorded(*row) for row in self.conn.execute(_RECORD_QUERY).fetchall()]

    def set_lock_timeout(self, seconds: int) -> None:
        self.conn.execute("SELECT pg_catalog.set_config('lock_timeout', %s, false)", (f"{seconds * 1000}ms",))

    def try_lock(self) -> bool:
        return bool(self.conn.execute("SELECT pg_catalog.pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0])

    def lock(self) -> None:
        try:
            self.conn.execute("SELECT pg_catalog.pg_advisory_lock(%s)", (LOCK_KEY,))
        except psycopg.errors.LockNotAvailable:
            raise LockTimeout() from None

    def unlock(self) -> None:
        """Releases the lock; closing the connection would as well, and a
        connection that broke has released it already."""
        if self.conn.closed or self.conn.broken:
            return
        try:
            self.conn.execute("SELECT pg_catalog.pg_advisory_unlock(%s)", (LOCK_KEY,))
        except psycopg.Error:
            pass  # the session ends with the command, and the lock with it

    def apply(self, change: Change, release: str) -> None:
        """The change and its row of the record, in one transaction."""
        with self.conn.transaction():
            # As bytes and without parameters: sent as it is, statement by
            # statement in the one transaction.
            self.conn.execute(change.text.encode("utf-8"))
            self.conn.execute(_RECORD_INSERT, (change.number, change.name, change.sha256,
                                               change.backward_compatible, release))


def _server_message(exc: Exception) -> str:
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    sqlstate = getattr(exc, "sqlstate", None)
    text = primary or (str(exc).strip().splitlines() or ["no message"])[0]
    return f"{text} (SQLSTATE {sqlstate})" if sqlstate else text


# --- The command -----------------------------------------------------------------

def release_version() -> str:
    """The PubDex version this code belongs to."""
    value = os.environ.get(VERSION_VARIABLE, "").strip()
    if value:
        return value
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or UNKNOWN_VERSION
    except OSError:
        return UNKNOWN_VERSION


class _Output:
    def __init__(self, out: TextIO, err: TextIO) -> None:
        self.out, self.err = out, err

    def say(self, message: str) -> None:
        print(f"schema: {message}", file=self.out, flush=True)

    def warn(self, message: str) -> None:
        print(f"schema: {message}", file=self.err, flush=True)


def _locate(changes: Sequence[Change], session: Session) -> Tuple[Facts, Placement]:
    facts = session.facts()
    if not facts.has_record:
        return facts, place_baseline(changes, facts)
    return facts, place(changes, session.record())


def _report_later(placement: Placement, output: _Output) -> None:
    if placement.later:
        output.say(f"the database also has {_count(len(placement.later), 'schema change')} of a later PubDex "
                   f"release, all backward compatible: {', '.join(row.name for row in placement.later)}")


def run(command: str, changes: Sequence[Change], session: Session, release: str, *,
        lock_timeout: int = DEFAULT_LOCK_TIMEOUT_SECONDS, out: TextIO = sys.stdout,
        err: TextIO = sys.stderr, clock=time.monotonic) -> int:
    """`command` (upgrade or status) on the database of session; the exit status."""
    output = _Output(out, err)
    everything = f"every schema change of PubDex {release}, up to {changes[-1].name}"
    output.say(f"PubDex {release}, {_count(len(changes), 'schema change')}, the last {changes[-1].name}; "
               f"{session.where()}")
    try:
        facts, placement = _locate(changes, session)
    except Refused as exc:
        output.warn(f"refused: {exc}")
        return FAILED
    if command == "status":
        if not placement.recorded:
            output.say("the database holds the baseline and has had no schema change")
        for row in placement.recorded:
            output.say(f"recorded: {row.describe()}")
        _report_later(placement, output)
        for change in placement.outstanding:
            output.say(f"outstanding: {change.label}: {change.summary}")
        if placement.outstanding:
            output.say(f"{_count(len(placement.outstanding), 'schema change')} outstanding; "
                       f"`{COMMAND} upgrade` applies what is outstanding")
            return OUTSTANDING
        output.say(f"nothing is outstanding: the database has {everything}")
        return OK

    if not placement.outstanding:
        _report_later(placement, output)
        output.say(f"nothing to do: the database has {everything}")
        return OK
    if facts.owner != facts.role:
        output.warn(f"refused: the schema upgrade has to run as {facts.owner}, the role that owns the PubDex "
                    "schema and whose default privileges give new tables to the application roles; this "
                    f"connection is the role {facts.role}")
        return FAILED
    if not placement.recorded:
        output.say("the database holds the baseline and has had no schema change")
    session.set_lock_timeout(lock_timeout)
    if not session.try_lock():
        output.say(f"another schema upgrade is running; waiting up to {lock_timeout} s for it to finish")
        try:
            session.lock()
        except LockTimeout:
            output.warn(f"refused: another schema upgrade held the lock for {lock_timeout} s, the most "
                        "--lock-timeout allows, and nothing was applied; run the schema upgrade again once it "
                        "has finished")
            return FAILED
    try:
        return _upgrade_locked(changes, session, release, output, everything, clock)
    finally:
        session.unlock()


def _upgrade_locked(changes: Sequence[Change], session: Session, release: str, output: _Output,
                    everything: str, clock) -> int:
    # Under the lock: what another schema upgrade applied meanwhile is no
    # longer outstanding.
    try:
        _facts, placement = _locate(changes, session)
    except Refused as exc:
        output.warn(f"refused: {exc}")
        return FAILED
    if not placement.outstanding:
        _report_later(placement, output)
        output.say(f"nothing to do: another schema upgrade applied what was outstanding; the database "
                   f"has {everything}")
        return OK
    applied: List[str] = []
    for change in placement.outstanding:
        output.say(f"applying {change.label}: {change.summary}")
        started = clock()
        try:
            session.apply(change, release)
        except psycopg.Error as exc:
            output.warn(str(ChangeFailed(change, len(applied), _server_message(exc))))
            if applied:
                output.warn(f"what this run applied before it stays applied and recorded: {', '.join(applied)}")
            output.warn("run the schema upgrade again once the cause is fixed; it continues with "
                        f"{change.name}")
            return FAILED
        applied.append(change.name)
        output.say(f"applied {change.name} in {clock() - started:.2f} s")
    output.say(f"done: applied {_count(len(applied), 'schema change')}; the database has {everything}")
    return OK


def connect(wait_seconds: int, output: Optional[_Output] = None) -> psycopg.Connection:
    """A connection made as the ingestion commands make theirs, once the
    server accepts connections or wait_seconds have passed."""
    # Imported here, not above: loading the configuration reads the password
    # file, and DB/.env unless told not to, which only this command needs.
    from people_pubs import config

    dsn = config.PEOPLE_DB_DSN or ""
    params = conninfo_to_dict(dsn) if dsn else {}
    probe = dsn
    if not params.get("connect_timeout") and not os.environ.get("PGCONNECT_TIMEOUT", "").strip():
        probe = make_conninfo(dsn, connect_timeout=PING_TIMEOUT_SECONDS)
    deadline = time.monotonic() + wait_seconds
    announced = False
    while pq.PGconn.ping(probe.encode("utf-8")) not in (pq.Ping.OK, pq.Ping.NO_ATTEMPT):
        if time.monotonic() >= deadline:
            break
        if not announced and output is not None:
            output.say(f"waiting up to {wait_seconds} s for the database server to accept connections")
            announced = True
        time.sleep(1)
    security = connection_security.defaults(params, os.environ)
    return psycopg.connect(dsn, autocommit=True, application_name=APPLICATION_NAME, **security)


def _seconds(minimum: int):
    def parse(value: str) -> int:
        if not re.fullmatch(r"[0-9]+", value) or int(value) < minimum:
            raise argparse.ArgumentTypeError(f"a whole number of seconds, at least {minimum}, not {value!r}")
        return int(value)
    return parse


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="The schema upgrade of a PubDex database: applies the schema changes of this release that "
                    "the database has not had yet, or reports them.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="{upgrade,status}")
    for name, text in (
        ("upgrade", "apply the outstanding schema changes, in order, each in a transaction of its own"),
        ("status", "report the recorded and the outstanding schema changes; change nothing"),
    ):
        command = commands.add_parser(name, help=text, description=text)
        command.add_argument("--wait", type=_seconds(0), default=DEFAULT_WAIT_SECONDS, metavar="SECONDS",
                             help="how long to wait for the server to accept connections "
                                  f"(default {DEFAULT_WAIT_SECONDS})")
        if name == "upgrade":
            command.add_argument("--lock-timeout", type=_seconds(1), default=DEFAULT_LOCK_TIMEOUT_SECONDS,
                                 metavar="SECONDS",
                                 help="how long to wait for another schema upgrade, and for each lock a change "
                                      f"needs (default {DEFAULT_LOCK_TIMEOUT_SECONDS})")
    args = parser.parse_args(argv)
    output = _Output(out, err)
    try:
        changes = load_changes(SCHEMA_DIR)
    except ChangeFileError as exc:
        output.warn(f"the schema changes of this release cannot be used: {exc}")
        return FAILED
    release = release_version()
    try:
        conn = connect(args.wait, output)
    except psycopg.Error as exc:
        output.warn(f"cannot connect to the database: {' '.join(str(exc).split())}")
        return FAILED
    try:
        with conn:
            return run(args.command, changes, Session(conn), release,
                       lock_timeout=getattr(args, "lock_timeout", DEFAULT_LOCK_TIMEOUT_SECONDS), out=out, err=err)
    except psycopg.Error as exc:
        output.warn(f"the database could not be read: {_server_message(exc)}")
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
