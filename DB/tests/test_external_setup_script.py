"""charts/pubdex/files/external-setup.sh, offline: the preparation of a
database on an operator's PostgreSQL server for the chart's external mode.

The real script runs against stub psql and setsid executables. The stub
psql answers the script's one read-only query with the facts a test gives
it, records every call -- its arguments, the libpq variables it sees, and
what it reads from its input -- and acts as the server would for the schema,
the passwords and a password login. That proves, without a database:

- what stops the run before any change, each with its reason and all of them
  at once: a server that is not PostgreSQL 16, an admin role without
  CREATEROLE or ownership, part of the schema, the roles of another
  installation, missing ADMIN OPTION, extensions it cannot create; and
  before the server is even contacted, unusable or shared password files;
- that a database without the schema gets the baseline in one transaction,
  from the files in name order and 000_init.sql without exactly its three
  lines, and a database with it skips that step and leaves the schema
  changes to the schema upgrade that follows;
- that both passwords reach psql only on its input, for \\password, the
  admin's only through a password file of mode 0600 that is removed
  afterwards, and none on a command line, in the environment or in the
  output;
- that each role has to log in with its new password by SCRAM;
- that every connection, the admin's three as well as the two logins, runs
  with libpq's require_auth=scram-sha-256 whether the environment left
  PGREQUIREAUTH unset, empty or set to it; that a server asking any of them
  for a cleartext password gets none and ends the run there, as the stub
  answers such a request the way libpq does; and that any other inherited
  PGREQUIREAUTH stops the run before the server is contacted.

The disposable-database runs exercise it against PostgreSQL itself.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from tests.test_password_files import PASSWORD_SAMPLES, UNUSABLE_SAMPLES

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "charts" / "pubdex" / "files" / "external-setup.sh"
INIT = ROOT / "DB" / "init"
BASH = shutil.which("bash") or "/bin/bash"
ADMIN_PASSWORD = "example-admin-07c5"
READER_PASSWORD = "example-reader-41aa"
WRITER_PASSWORD = "example-writer-93cd"
LEFT_OUT = (
    "ALTER SYSTEM SET password_encryption = 'scram-sha-256';",
    "SELECT pg_reload_conf();",
    "GRANT USAGE ON SCHEMA public TO postgres;",
)
NOTHING_CHANGED = "cannot be prepared; nothing was changed:"

# Each call: "psql", then each argument, separated by \037, a line break in
# an argument written as \n. The stub keeps what each call saw in files
# numbered by call.
STUB_PSQL = r'''#!/usr/bin/env bash
state="$STUB_STATE"
n=$(( $(cat "$state/count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$state/count"
{ printf 'psql'; for a in "$@"; do printf '\037%s' "${a//$'\n'/\\n}"; done; printf '\n'; } >> "$state/events"
env > "$state/env.$n"
args="$*"
case ",$STUB_MODE," in *",connect-fails,"*)
  echo "psql: error: connection to server at \"$PGHOST\", port 5432 failed: FATAL:  password authentication failed for user \"$PGUSER\"" >&2
  exit 2 ;;
esac
# The server asks this connection for a cleartext password. libpq refuses
# under require_auth=scram-sha-256 before it answers; otherwise it sends the
# password, which the stub records, and the server goes on as usual.
case ",$STUB_MODE," in *",cleartext-$n,"*)
  if [ "${PGREQUIREAUTH:-}" = scram-sha-256 ]; then
    echo "psql: error: connection to server at \"$PGHOST\", port 5432 failed: authentication method requirement \"scram-sha-256\" failed: server requested a cleartext password" >&2
    exit 2
  fi
  if [ -r "${PGPASSFILE:-}" ]; then cp "$PGPASSFILE" "$state/sent.$n"; else echo "from the input" > "$state/sent.$n"; fi ;;
esac
if [[ "$args" == *"-F"* ]]; then
  cp "$PGPASSFILE" "$state/pgpass"
  stat -c %a "$PGPASSFILE" > "$state/pgpass.mode"
  cat "$state/facts"
  exit 0
fi
if [[ "$args" == *"ALTER ROLE maintenance NOLOGIN"* ]]; then
  previous=""
  for a in "$@"; do
    if [ "$previous" = "-f" ]; then echo "$a" >> "$state/schema.files"; cp "$a" "$state/schema.$(basename "$a")"; fi
    previous="$a"
  done
  echo "psql:010_ingestion_runtime_schema.sql:1: NOTICE:  a notice that is not shown" >&2
  case ",$STUB_MODE," in *",schema-fails,"*)
    echo "psql:/init/010_ingestion_runtime_schema.sql:7: ERROR:  stub failure" >&2; exit 3 ;;
  esac
  exit 0
fi
if [[ "$args" == *"\\password app_readonly"* ]]; then
  for i in 1 2 3 4; do
    IFS= read -r line || exit 1
    printf '%s\n' "$line" >> "$state/password.input"
  done
  case ",$STUB_MODE," in *",password-fails,"*)
    echo "ERROR:  stub failure for the password $line" >&2; exit 3 ;;
  esac
  exit 0
fi
if [[ "$args" == *"system_user"* ]]; then
  printf 'Password: ' >&2
  IFS= read -r given || exit 1
  printf '%s|%s|%s\n' "$PGUSER" "$PGREQUIREAUTH" "$given" >> "$state/logins"
  case ",$STUB_MODE," in
    *",login-trusted,"*) echo "$PGUSER without authentication"; exit 0 ;;
    *",login-md5,"*) echo "$PGUSER md5:$PGUSER"; exit 0 ;;
    *",login-fails,"*) echo "psql: error: FATAL:  password authentication failed for user \"$PGUSER\"" >&2; exit 2 ;;
  esac
  echo "$PGUSER scram-sha-256:$PGUSER"
  exit 0
fi
echo "stub psql: unexpected call" >&2
exit 2
'''

STUB_SETSID = r'''#!/usr/bin/env bash
{ printf 'setsid'; for a in "$@"; do printf '\037%s' "${a//$'\n'/\\n}"; done; printf '\n'; } >> "$STUB_STATE/events"
[ "$1" = "--wait" ] && shift
exec "$@"
'''

FRESH = {
    "version": "160015", "superuser": "no", "createrole": "yes", "owner": "yes", "tls": "TLSv1.3",
    **{f"schema:{name}": "no" for name in ("app", "biblio", "activity", "pii", "staging_raw")},
    "marker:table biblio.publications": "no",
    "marker:function biblio.refresh_serving_snapshots": "no",
    "marker:table app.member_info_manual": "no",
    **{f"role:{name}": "absent" for name in ("app_readonly", "app_writer", "maintenance")},
    **{f"extension:{name}": "trusted" for name in ("citext", "pgcrypto", "unaccent", "pg_trgm")},
}
INITIALISED = {
    **FRESH,
    **{key: "yes" for key in FRESH if key.startswith(("schema:", "marker:"))},
    **{f"role:{name}": "admin" for name in ("app_readonly", "app_writer", "maintenance")},
    **{f"extension:{name}": "installed:public" for name in ("citext", "pgcrypto", "unaccent", "pg_trgm")},
}


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Run:
    """One run of the script in a directory of its own."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.secrets = tmp_path / "secrets"
        self.work = tmp_path / "tmp"
        self.init = tmp_path / "init"
        for directory in (self.bin, self.state, self.secrets, self.work, self.init):
            directory.mkdir()
        _executable(self.bin / "psql", STUB_PSQL)
        _executable(self.bin / "setsid", STUB_SETSID)
        for source in sorted(INIT.glob("*.sql")):
            shutil.copy(source, self.init / source.name)
        self.write_secret("admin_password", ADMIN_PASSWORD)
        self.write_secret("app_readonly_password", READER_PASSWORD)
        self.write_secret("app_writer_password", WRITER_PASSWORD)
        self.facts(FRESH)

    def write_secret(self, name: str, value, newline: bool = True) -> Path:
        data = value if isinstance(value, bytes) else (value + ("\n" if newline else "")).encode("utf-8")
        path = self.secrets / name
        path.write_bytes(data)
        return path

    def facts(self, facts: Dict[str, str]) -> None:
        (self.state / "facts").write_text("".join(f"{key}\t{value}\n" for key, value in facts.items()),
                                          encoding="utf-8")

    def env(self, **overrides: Optional[str]) -> Dict[str, str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "TMPDIR": str(self.work),
            "STUB_STATE": str(self.state),
            "STUB_MODE": "",
            "PGHOST": "db.example.org",
            "PGPORT": "5432",
            "PGDATABASE": "people_db",
            "PGUSER": "dbadmin",
            "PGSSLMODE": "verify-full",
            "PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE": str(self.secrets / "admin_password"),
            "PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE": str(self.secrets / "app_readonly_password"),
            "PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE": str(self.secrets / "app_writer_password"),
            "PEOPLE_PUBS_SETUP_INIT_DIR": str(self.init),
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def __call__(self, *args: str, stdin: Optional[int] = subprocess.DEVNULL, **env: Optional[str]):
        return subprocess.run([BASH, str(SCRIPT), *args], env=self.env(**env), stdin=stdin, capture_output=True,
                              text=True, timeout=60)

    def calls(self) -> List[List[str]]:
        path = self.state / "events"
        if not path.exists():
            return []
        return [line.split("\037") for line in path.read_text(encoding="utf-8").splitlines()]

    def psql_calls(self) -> List[List[str]]:
        return [call for call in self.calls() if call[0] == "psql"]

    def read(self, name: str) -> str:
        return (self.state / name).read_text(encoding="utf-8")


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


def _no_password_leaves(run: Run, proc: subprocess.CompletedProcess) -> None:
    """No password in the output, on a command line or in any environment."""
    events = run.state / "events"
    command_lines = events.read_text(encoding="utf-8") if events.exists() else ""
    for password in (ADMIN_PASSWORD, READER_PASSWORD, WRITER_PASSWORD):
        assert password not in proc.stdout + proc.stderr
        assert password not in command_lines
        for env in run.state.glob("env.*"):
            assert password not in env.read_text(encoding="utf-8"), env.name


# --- usage and the files, before the server is contacted -----------------------

def test_help_and_usage_errors(run: Run) -> None:
    proc = run("--help")
    assert proc.returncode == 0 and "usage: external-setup.sh" in proc.stdout
    proc = run("--force")
    assert proc.returncode == 2 and "unknown argument: --force" in proc.stderr
    for variable in ("PGHOST", "PGDATABASE", "PGUSER", "PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE",
                     "PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE", "PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE",
                     "PEOPLE_PUBS_SETUP_INIT_DIR"):
        proc = run(**{variable: None})
        assert proc.returncode == 2, variable
        assert f"external-setup.sh: {variable} is not set" in proc.stderr
    assert run.calls() == []


@pytest.mark.parametrize("role", ["app_readonly", "app_writer", "maintenance"])
def test_the_admin_is_never_a_role_the_setup_creates(run: Run, role: str) -> None:
    proc = run(PGUSER=role)
    assert proc.returncode == 1
    assert f"PGUSER is {role}, one of the roles this setup creates" in proc.stderr
    assert run.calls() == []


def test_an_init_directory_without_000_init_is_refused(run: Run) -> None:
    (run.init / "000_init.sql").unlink()
    proc = run()
    assert proc.returncode == 1 and "PEOPLE_PUBS_SETUP_INIT_DIR holds no 000_init.sql" in proc.stderr
    assert run.calls() == []


@pytest.mark.parametrize("variable", ["PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE", "PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE",
                                      "PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE"])
@pytest.mark.parametrize("content, reason", UNUSABLE_SAMPLES, ids=[reason for _c, reason in UNUSABLE_SAMPLES])
def test_unusable_password_files_stop_the_run_before_the_server_is_contacted(
        run: Run, variable: str, content: bytes, reason: str) -> None:
    path = run.write_secret("unusable", content)
    proc = run(**{variable: str(path)})
    assert proc.returncode == 1
    assert variable in proc.stderr and reason in proc.stderr
    assert run.psql_calls() == []


def test_a_missing_password_file_names_the_secret_to_check(run: Run) -> None:
    proc = run(PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE=str(run.secrets / "absent"))
    assert proc.returncode == 1
    assert "PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE names a file that does not exist" in proc.stderr
    assert "the Secret that holds it is missing or lacks the key" in proc.stderr
    assert run.calls() == []


def test_shared_passwords_are_refused_before_the_server_is_contacted(run: Run) -> None:
    run.write_secret("app_writer_password", READER_PASSWORD)
    proc = run()
    assert proc.returncode == 1 and "app_readonly and app_writer must not share a password" in proc.stderr
    run.write_secret("app_writer_password", ADMIN_PASSWORD)
    proc = run()
    assert proc.returncode == 1 and "an application role must not have the password of dbadmin" in proc.stderr
    assert run.calls() == []


# --- the preparation --------------------------------------------------------------

def test_a_database_without_the_schema_is_prepared_in_order(run: Run) -> None:
    proc = run(PGPASSWORD="example-ambient-0b1c")
    assert proc.returncode == 0, proc.stderr
    calls = run.psql_calls()
    assert len(calls) == 5, calls
    facts, schema, passwords, reader, writer = calls
    # 1. One query that changes nothing.
    assert "-F" in facts and "--no-password" in facts and "SELECT" in facts[-1] and "ALTER" not in facts[-1]
    # 2. The schema: one transaction, the files in name order, then maintenance.
    assert "--single-transaction" in schema and schema[-2:] == ["-c", "ALTER ROLE maintenance NOLOGIN PASSWORD NULL"]
    files = [Path(line).name for line in run.read("schema.files").splitlines()]
    assert files == sorted(path.name for path in INIT.glob("*.sql"))
    assert not run.read("schema.files").startswith(str(run.init) + "/000"), "000_init.sql is a filtered copy"
    original = (INIT / "000_init.sql").read_text(encoding="utf-8").split("\n")
    assert run.read("schema.000_init.sql").split("\n") == [line for line in original if line not in LEFT_OUT]
    # 3. Both passwords on psql's input, twice each, with SCRAM for this
    #    transaction, and CONNECT for the two roles only.
    assert passwords[-6:-2] == ["-c", "\\password app_readonly", "-c", "\\password app_writer"]
    assert "SET LOCAL password_encryption = 'scram-sha-256'" in passwords
    assert "--single-transaction" in passwords
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC" in passwords[-1]
    assert "GRANT CONNECT ON DATABASE %I TO app_readonly, app_writer" in passwords[-1]
    assert run.read("password.input").split("\n")[:4] == [READER_PASSWORD] * 2 + [WRITER_PASSWORD] * 2
    setsid = [call for call in run.calls() if call[0] == "setsid"]
    assert len(setsid) == 3 and all(call[1] == "--wait" for call in setsid), "psql without a terminal"
    # 4. A new SCRAM login per role, its password on psql's input.
    assert "-W" in reader and "-W" in writer
    assert run.read("logins").splitlines() == [f"app_readonly|scram-sha-256|{READER_PASSWORD}",
                                               f"app_writer|scram-sha-256|{WRITER_PASSWORD}"]
    out = proc.stdout
    assert ("connected to db.example.org:5432, database people_db, as dbadmin: PostgreSQL 16.15, "
            "sslmode verify-full, TLS TLSv1.3") in out
    assert "applied the schema in one transaction: 000_init.sql 010_ingestion_runtime_schema.sql " \
           "011_member_info_manual.sql (3 line(s) of 000_init.sql left out)" in out
    # The schema changes are the schema upgrade's, which runs after it.
    assert ("each authenticated a new connection by SCRAM; the database people_db is prepared, "
            "and the schema upgrade comes next") in out
    assert "people_pubs.schema" not in "\n".join(" ".join(call) for call in calls)
    assert "a notice that is not shown" not in proc.stdout + proc.stderr
    _no_password_leaves(run, proc)
    assert "example-ambient-0b1c" not in run.read("env.1"), "PGPASSWORD is not used"


@pytest.mark.parametrize("given, used", [(None, "verify-full"), ("", "verify-full"), ("require", "require")],
                         ids=["unset", "empty", "explicit"])
def test_tls_checks_the_server_unless_a_weaker_mode_is_set(run: Run, given: Optional[str], used: str) -> None:
    # Run by hand without PGSSLMODE, the script uses the chart's default,
    # not libpq's prefer; a weaker mode has to be set explicitly.
    proc = run(PGSSLMODE=given)
    assert proc.returncode == 0, proc.stderr
    assert f"sslmode {used}, TLS" in proc.stdout
    for env in sorted(run.state.glob("env.*")):
        assert f"PGSSLMODE={used}" in env.read_text(encoding="utf-8").splitlines(), env.name


def test_the_admin_password_reaches_libpq_only_through_a_private_file(run: Run) -> None:
    # A password with a colon, a backslash, quotes and shell syntax.
    content, password = next(sample for sample in PASSWORD_SAMPLES if ":" in sample[1] and "\\" in sample[1])
    run.write_secret("admin_password", content)
    proc = run(PGUSER="db:admin\\x")
    assert proc.returncode == 0, proc.stderr
    # libpq's password file format: colon and backslash escaped.
    escaped = password.replace("\\", "\\\\").replace(":", "\\:")
    assert run.read("pgpass") == f"*:*:*:db\\:admin\\\\x:{escaped}\n"
    assert run.read("pgpass.mode") == "600\n"
    assert password not in proc.stdout + proc.stderr
    assert password not in (run.state / "events").read_text(encoding="utf-8")
    for env in sorted(run.state.glob("env.*")):
        lines = env.read_text(encoding="utf-8").splitlines()
        assert not [line for line in lines if line.startswith("PGPASSWORD=")], env.name
        assert "PGGSSENCMODE=disable" in lines and "PGCLIENTENCODING=UTF8" in lines
    # The logins never use the admin's file.
    for n in (4, 5):
        assert f"PGPASSFILE={run.work}" in run.read(f"env.{n}")
        assert "admin.pgpass" not in run.read(f"env.{n}")
    assert list(run.work.iterdir()) == [], "the private directory is removed"


@pytest.mark.parametrize("content, password", PASSWORD_SAMPLES[:6], ids=[str(i) for i in range(6)])
def test_every_usable_password_reaches_psql_exactly(run: Run, content: bytes, password: str) -> None:
    run.write_secret("app_readonly_password", content)
    proc = run()
    assert proc.returncode == 0, proc.stderr
    assert run.read("password.input").split("\n")[:2] == [password, password]
    assert run.read("logins").splitlines()[0] == f"app_readonly|scram-sha-256|{password}"


def test_a_database_with_the_schema_skips_the_schema_step(run: Run) -> None:
    # The baseline only: whether schema changes are outstanding is the
    # schema upgrade's to decide, which runs after this script.
    run.facts(INITIALISED)
    proc = run()
    assert proc.returncode == 0, proc.stderr
    assert ("the database holds the PubDex baseline already; this step is skipped, and the schema upgrade "
            "decides which schema changes are outstanding") in proc.stdout
    calls = run.psql_calls()
    assert len(calls) == 4 and not any("ALTER ROLE maintenance NOLOGIN PASSWORD NULL" in call for call in calls)
    assert not (run.state / "schema.files").exists()


def test_a_superuser_needs_neither_createrole_nor_ownership_nor_trusted_extensions(run: Run) -> None:
    run.facts({**FRESH, "superuser": "yes", "createrole": "no", "owner": "no",
               "extension:pg_trgm": "untrusted"})
    proc = run()
    assert proc.returncode == 0, proc.stderr
    run.facts({**INITIALISED, "superuser": "yes", "role:app_readonly": "admin", "role:app_writer": "present"})
    proc = run()
    assert proc.returncode == 0, proc.stderr


def test_extensions_installed_in_public_already_are_fine(run: Run) -> None:
    run.facts({**FRESH, "extension:citext": "installed:public", "extension:pgcrypto": "untrusted"})
    proc = run()
    assert proc.returncode == 1
    assert "the extension pgcrypto is not trusted on this server" in proc.stderr
    assert "citext" not in proc.stderr


# --- what stops the run before any change -------------------------------------------

REFUSALS = {
    "postgresql-15": ({"version": "150010"}, ["the server runs PostgreSQL 15.10; PubDex supports PostgreSQL 16 only"]),
    "postgresql-17": ({"version": "170002"}, ["the server runs PostgreSQL 17.2"]),
    "no-createrole": ({"createrole": "no"}, ["the role dbadmin lacks CREATEROLE"]),
    "not-the-owner": ({"owner": "no"}, ["the role dbadmin does not own the database people_db"]),
    "part-of-the-schema": ({"schema:app": "yes", "schema:pii": "yes"},
                           ["holds part of the PubDex schema, or objects of other software with its names: it has "
                            "app, pii but not biblio, activity, staging_raw, table biblio.publications"]),
    "roles-of-another-installation": ({"role:app_writer": "present", "role:maintenance": "admin"},
                                      ["the role(s) app_writer, maintenance exist on this server, but the database "
                                       "people_db holds no PubDex schema",
                                       "One PubDex installation per PostgreSQL server"]),
    "extension-untrusted": ({"extension:unaccent": "untrusted"},
                            ["the extension unaccent is not trusted on this server, so only a superuser can create "
                             "it: have one run CREATE EXTENSION unaccent; in the database people_db first"]),
    "extension-unavailable": ({"extension:pg_trgm": "unavailable"},
                              ["the extension pg_trgm is not available on this server"]),
    "extension-elsewhere": ({"extension:citext": "installed:extensions"},
                            ["the extension citext is installed in schema extensions of people_db; PubDex needs it "
                             "in schema public"]),
}
INITIALISED_REFUSALS = {
    "no-admin-option": ({"role:app_readonly": "present", "role:app_writer": "present"},
                        ["the role dbadmin lacks the ADMIN OPTION on app_readonly",
                         "GRANT app_writer TO dbadmin WITH ADMIN OPTION"]),
    "role-missing": ({"role:app_writer": "absent"},
                     ["the database people_db holds the PubDex schema, but the role app_writer does not exist"]),
    "marker-missing": ({"marker:function biblio.refresh_serving_snapshots": "no"},
                       ["holds part of the PubDex schema", "but not function biblio.refresh_serving_snapshots"]),
}


def _refused(run: Run, proc: subprocess.CompletedProcess, reasons: List[str]) -> None:
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"the database people_db {NOTHING_CHANGED}" in proc.stderr
    for reason in reasons:
        assert reason in proc.stderr
    calls = run.psql_calls()
    assert len(calls) == 1 and "-F" in calls[0], "only the read-only query ran"
    _no_password_leaves(run, proc)


@pytest.mark.parametrize("facts, reasons", REFUSALS.values(), ids=REFUSALS.keys())
def test_a_database_that_cannot_be_prepared_is_left_unchanged(run: Run, facts: dict, reasons: List[str]) -> None:
    run.facts({**FRESH, **facts})
    _refused(run, run(), reasons)


@pytest.mark.parametrize("facts, reasons", INITIALISED_REFUSALS.values(), ids=INITIALISED_REFUSALS.keys())
def test_an_initialised_database_that_cannot_be_prepared_is_left_unchanged(
        run: Run, facts: dict, reasons: List[str]) -> None:
    run.facts({**INITIALISED, **facts})
    _refused(run, run(), reasons)


def test_every_reason_is_named_at_once(run: Run) -> None:
    run.facts({**FRESH, "version": "150000", "createrole": "no", "owner": "no", "role:app_readonly": "present"})
    proc = run()
    _refused(run, proc, ["PostgreSQL 15.0", "lacks CREATEROLE", "does not own", "the role(s) app_readonly exist"])
    assert proc.stderr.count("\n  - ") == 4


def test_a_server_that_reports_no_version_is_refused(run: Run) -> None:
    run.facts({key: value for key, value in FRESH.items() if key != "version"})
    proc = run()
    assert proc.returncode == 1 and "the server reported no version" in proc.stderr


def test_a_failed_connection_says_where_to_and_as_whom(run: Run) -> None:
    proc = run(STUB_MODE="connect-fails")
    assert proc.returncode == 1
    assert "cannot connect to db.example.org:5432, database people_db, as dbadmin" in proc.stderr
    assert "password authentication failed" in proc.stderr
    assert len(run.psql_calls()) == 1
    assert list(run.work.iterdir()) == []


# --- failures after the checks ------------------------------------------------------------

def test_a_failed_schema_reports_its_error_without_the_notices(run: Run) -> None:
    proc = run(STUB_MODE="schema-fails")
    assert proc.returncode == 1
    assert "the schema could not be applied, and nothing of it was kept" in proc.stderr
    assert "ERROR:  stub failure" in proc.stderr
    assert len(run.psql_calls()) == 2, "no password is set after a failed schema"


def test_a_failed_password_change_masks_the_password(run: Run) -> None:
    proc = run(STUB_MODE="password-fails")
    assert proc.returncode == 1
    assert "the passwords could not be set, and none was changed" in proc.stderr
    assert "stub failure for the password <password>" in proc.stderr
    _no_password_leaves(run, proc)


@pytest.mark.parametrize("mode, accepted", [("login-md5", True), ("login-trusted", False), ("login-fails", False)])
def test_each_role_has_to_log_in_by_its_password(run: Run, mode: str, accepted: bool) -> None:
    proc = run(STUB_MODE=mode)
    assert (proc.returncode == 0) is accepted, proc.stderr
    if not accepted:
        assert "app_readonly" in proc.stderr and "Password: " not in proc.stderr
        assert ("was not authenticated by its password" in proc.stderr
                or "with its new password failed" in proc.stderr)
    _no_password_leaves(run, proc)


# --- SCRAM for every connection -----------------------------------------------------------

# The connections of a run on a database without the schema, in order: the
# admin's three, then one login per role. What tells each call apart.
CONNECTIONS = {
    "facts": "-F",
    "schema": "ALTER ROLE maintenance NOLOGIN PASSWORD NULL",
    "passwords": "\\password app_readonly",
    "app_readonly login": "-W",
    "app_writer login": "-W",
}
REFUSED_AT = {
    "facts": "cannot connect to db.example.org:5432, database people_db, as dbadmin",
    "schema": "the schema could not be applied, and nothing of it was kept",
    "passwords": "the passwords could not be set, and none was changed",
    "app_readonly login": "a new connection as app_readonly with its new password failed",
    "app_writer login": "a new connection as app_writer with its new password failed",
}


@pytest.mark.parametrize("given", [None, "", "scram-sha-256"], ids=["unset", "empty", "explicit"])
def test_every_connection_requires_scram(run: Run, given: Optional[str]) -> None:
    proc = run(PGREQUIREAUTH=given)
    assert proc.returncode == 0, proc.stderr
    calls = run.psql_calls()
    assert len(calls) == len(CONNECTIONS)
    for n, (name, marker) in enumerate(CONNECTIONS.items(), start=1):
        assert marker in calls[n - 1], name
        assert "PGREQUIREAUTH=scram-sha-256" in run.read(f"env.{n}").splitlines(), name


@pytest.mark.parametrize("n, name", list(enumerate(CONNECTIONS, start=1)), ids=list(CONNECTIONS))
def test_a_server_that_asks_for_a_cleartext_password_gets_none(run: Run, n: int, name: str) -> None:
    proc = run(STUB_MODE=f"cleartext-{n}")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert REFUSED_AT[name] in proc.stderr
    assert ('authentication method requirement "scram-sha-256" failed: server requested a cleartext password'
            in proc.stderr)
    assert len(run.psql_calls()) == n, "the run ends at the refused connection"
    assert not list(run.state.glob("sent.*")), "no password was sent"
    assert list(run.work.iterdir()) == [], "the private directory is removed"
    _no_password_leaves(run, proc)


@pytest.mark.parametrize("given", ["password", "md5", "none", "scram-sha-256,password", "!md5"])
def test_an_inherited_weaker_requirement_stops_the_run_before_the_server_is_contacted(run: Run, given: str) -> None:
    proc = run(PGREQUIREAUTH=given)
    assert proc.returncode == 1
    assert (f'PGREQUIREAUTH is "{given}", but this setup lets the server authenticate its connections by SCRAM '
            "only: unset PGREQUIREAUTH or set it to scram-sha-256") in proc.stderr
    assert run.calls() == []
    _no_password_leaves(run, proc)


# --- the three lines of 000_init.sql -----------------------------------------------------

def _filter(run: Run, text: str) -> subprocess.CompletedProcess:
    source = run.dir / "000_init.sql"
    source.write_text(text, encoding="utf-8")
    return subprocess.run([BASH, "-c", 'source "$1" && external_setup_filter_init "$2" "$3"', "filter", str(SCRIPT),
                           str(source), str(run.dir / "out.sql")], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})


def test_the_filter_matches_whole_lines_and_tolerates_their_absence(run: Run) -> None:
    proc = _filter(run, "SELECT 1;\n  SELECT pg_reload_conf();\nSELECT pg_reload_conf(); -- kept\n")
    assert proc.returncode == 0 and proc.stdout.strip() == "0"
    assert (run.dir / "out.sql").read_text(encoding="utf-8") == \
        "SELECT 1;\n  SELECT pg_reload_conf();\nSELECT pg_reload_conf(); -- kept\n"
    proc = _filter(run, "SELECT 1;\nGRANT USAGE ON SCHEMA public TO postgres;\nSELECT 2;\n")
    assert proc.returncode == 0 and proc.stdout.strip() == "1"
    assert (run.dir / "out.sql").read_text(encoding="utf-8") == "SELECT 1;\nSELECT 2;\n"


def test_the_filter_refuses_a_line_that_occurs_twice(run: Run) -> None:
    proc = _filter(run, "SELECT pg_reload_conf();\nSELECT 1;\nSELECT pg_reload_conf();\n")
    assert proc.returncode == 1
    assert "holds the line \"SELECT pg_reload_conf();\" 2 times" in proc.stderr


def test_the_script_is_sourced_without_running(run: Run) -> None:
    proc = subprocess.run([BASH, "-c", 'source "$1" && echo sourced', "s", str(SCRIPT)], capture_output=True,
                          text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0 and proc.stdout == "sourced\n" and proc.stderr == ""
    assert os.access(SCRIPT, os.X_OK) and SCRIPT.stat().st_mode & stat.S_IXOTH
