"""DB/init/020_role_passwords.sh, offline: the first-start provisioning of the
application role passwords from secret files, and the passwords that
set_passwords.sh hands it on a running server.

The real script runs against stub psql, setsid and hostname executables,
which record every call and, for \\password and a password login, the lines
psql reads from its input. That proves, without a database, that the script
changes nothing without its variables, refuses half a configuration and
unusable files before any role changes -- with messages that name the
variable, never the content -- hands each password to psql only through its
input and never on a command line or in its own output, refuses shared
passwords, takes the login away from maintenance on a first start, checks
the result, and keeps the image's initialisation running when it is sourced
rather than executed.

For --stdin it proves that the three passwords come from standard input
alone -- no file, which a running container may still see in an old version
-- that every line is checked before any role changes, that each password is
set and then has to authenticate a new connection by SCRAM, where a trusted
connection does not count, and that maintenance stays as it is. The
disposable-database runs and the Compose deployment exercise it against
PostgreSQL itself.
"""
from __future__ import annotations

import os
import pty
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from tests.test_password_files import MARKER, PASSWORD_SAMPLES, UNUSABLE_SAMPLES

DB_DIR = Path(__file__).resolve().parents[1]
SCRIPT = DB_DIR / "init" / "020_role_passwords.sh"
BASH = shutil.which("bash") or "/bin/bash"
READONLY_VARIABLE = "PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE"
WRITER_VARIABLE = "PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE"
READER_PASSWORD = "example-reader-41aa"
WRITER_PASSWORD = "example-writer-93cd"
SUPERUSER_PASSWORD = "example-superuser-07fe"
STDIN_CONFIRMATION = ("020_role_passwords.sh: postgres, app_readonly and app_writer have the passwords from "
                      "standard input, and each authenticated a new connection.")

# Both stubs log one call per line, with a line break inside an argument
# written as a backslash and "n".
STUB_PSQL = r'''#!/usr/bin/env bash
state="$STUB_STATE"
{ printf 'psql'; for a in "$@"; do printf '\037%s' "${a//$'\n'/\\n}"; done; printf '\n'; } >> "$state/events"
command=""
previous=""
for a in "$@"; do
  [ "$previous" = "-c" ] && command="$a"
  previous="$a"
done
case "$command" in
  "\\password "*)
    role="${command#\\password }"
    printf 'Enter new password for user "%s": ' "$role" >&2
    IFS= read -r first || exit 1
    printf 'Enter it again: ' >&2
    IFS= read -r second || exit 1
    printf '%s' "$first" > "$state/password.$role"
    printf '%s' "$second" > "$state/again.$role"
    case ",$STUB_MODE," in
      *",password-fails,"*) echo "ERROR:  stub failure for $role, password $first" >&2; exit 3 ;;
    esac
    exit 0 ;;
  *system_user*)
    # A password login: psql prompts, reads the password from its input and
    # prints the query's result, as the server authenticated the role.
    role=""; previous=""
    for a in "$@"; do [ "$previous" = "--username" ] && role="$a"; previous="$a"; done
    printf 'Password: ' >&2
    IFS= read -r given || exit 1
    printf '%s' "$given" > "$state/login.$role"
    case ",$STUB_MODE," in
      *",login-trusted,"*) echo "$role without a password"; exit 0 ;;
    esac
    if [ ! -e "$state/password.$role" ] || [ "$(cat "$state/password.$role")" != "$given" ]; then
      echo "psql: error: connection to server failed: FATAL:  password authentication failed for user \"$role\"" >&2
      exit 2
    fi
    echo "$role scram-sha-256:$role"; exit 0 ;;
  *pg_authid*) echo "${STUB_PROVISIONED:-2}"; exit 0 ;;
  *"ALTER ROLE maintenance NOLOGIN"*) exit 0 ;;
esac
echo "stub psql: unexpected command" >&2
exit 2
'''

STUB_HOSTNAME = r'''#!/usr/bin/env bash
{ printf 'hostname'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$STUB_STATE/events"
[ "$1" = "-i" ] && echo "192.0.2.15 2001:db8::15"
'''

STUB_SETSID = r'''#!/usr/bin/env bash
{ printf 'setsid'; for a in "$@"; do printf '\037%s' "${a//$'\n'/\\n}"; done; printf '\n'; } >> "$STUB_STATE/events"
[ "$1" = "--wait" ] && shift
exec "$@"
'''


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Provisioning:
    """The real script with stub psql and setsid, and the files it reads."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.bin.mkdir()
        self.state.mkdir()
        _executable(self.bin / "psql", STUB_PSQL)
        _executable(self.bin / "setsid", STUB_SETSID)
        _executable(self.bin / "hostname", STUB_HOSTNAME)
        self.readonly = self.secret("app_readonly_password", READER_PASSWORD.encode() + b"\n")
        self.writer = self.secret("app_writer_password", WRITER_PASSWORD.encode() + b"\n")

    def secret(self, name: str, content: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(content)
        return path

    def environment(self, files: bool = True, **extra: str) -> Dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "STUB_STATE": str(self.state),
            "POSTGRES_USER": "postgres",
            "POSTGRES_DB": "people_db",
        }
        if files:
            env[READONLY_VARIABLE] = str(self.readonly)
            env[WRITER_VARIABLE] = str(self.writer)
        env.update(extra)
        return {name: value for name, value in env.items() if value is not None}

    def run(self, *args: str, files: bool = True, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(SCRIPT), *args], env=self.environment(files, **extra), cwd=self.tmp,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def stdin(self, data: bytes, files: bool = False, **extra: str) -> subprocess.CompletedProcess:
        """--stdin as set_passwords.sh runs it: the lines on standard input, and
        by default none of the file variables."""
        proc = subprocess.run([str(SCRIPT), "--stdin"], env=self.environment(files, **extra), cwd=self.tmp,
                              input=data, capture_output=True, timeout=60)
        return subprocess.CompletedProcess(proc.args, proc.returncode, proc.stdout.decode("utf-8", "replace"),
                                           proc.stderr.decode("utf-8", "replace"))

    def calls(self) -> List[List[str]]:
        events = self.state / "events"
        if not events.exists():
            return []
        return [line.split("\x1f") for line in events.read_text(encoding="utf-8").splitlines()]

    def received(self, role: str) -> Optional[bytes]:
        path = self.state / f"password.{role}"
        return path.read_bytes() if path.exists() else None

    def tried(self, role: str) -> Optional[bytes]:
        path = self.state / f"login.{role}"
        return path.read_bytes() if path.exists() else None

    def commands(self) -> List[str]:
        return [call[call.index("-c") + 1] for call in self.calls() if call[0] == "psql" and "-c" in call]


@pytest.fixture
def provisioning(tmp_path: Path) -> Provisioning:
    return Provisioning(tmp_path)


def test_the_script_is_executable_and_runs_after_the_schema_files() -> None:
    assert os.access(SCRIPT, os.X_OK), "the image executes the script when it is executable"
    init_files = sorted(p.name for p in (DB_DIR / "init").iterdir())
    assert init_files[-1] == SCRIPT.name, "it runs last, after the roles exist"


def test_without_its_variables_it_changes_nothing(provisioning: Provisioning) -> None:
    proc = provisioning.run(files=False)
    assert proc.returncode == 0, proc.stderr
    assert "no role password files are configured" in proc.stdout
    assert provisioning.calls() == [], "no database command"


@pytest.mark.parametrize("missing", [READONLY_VARIABLE, WRITER_VARIABLE])
def test_half_a_configuration_is_refused_before_any_change(provisioning: Provisioning, missing: str) -> None:
    proc = provisioning.run(**{missing: ""})
    assert proc.returncode == 1
    assert "set both" in proc.stderr and missing in proc.stderr
    assert provisioning.calls() == []


def test_a_first_start_sets_both_passwords_through_psql_input_and_closes_maintenance(
        provisioning: Provisioning) -> None:
    proc = provisioning.run()
    assert proc.returncode == 0, proc.stderr
    assert provisioning.received("app_readonly") == READER_PASSWORD.encode()
    assert provisioning.received("app_writer") == WRITER_PASSWORD.encode()
    assert (provisioning.state / "again.app_writer").read_bytes() == WRITER_PASSWORD.encode()
    commands = provisioning.commands()
    assert commands[:2] == ["\\password app_readonly", "\\password app_writer"]
    assert any("ALTER ROLE maintenance NOLOGIN PASSWORD NULL" in c for c in commands)
    assert "pg_authid" in commands[-1], "the result is checked last"
    # psql runs without a controlling terminal, so it reads the pipe.
    setsid = [call for call in provisioning.calls() if call[0] == "setsid"]
    assert len(setsid) == 2 and all(call[1] == "--wait" for call in setsid)
    for call in provisioning.calls():
        assert "--username" in call or call[0] == "setsid"
        assert not any(READER_PASSWORD in arg or WRITER_PASSWORD in arg for arg in call), call
    assert READER_PASSWORD not in proc.stdout + proc.stderr
    assert WRITER_PASSWORD not in proc.stdout + proc.stderr
    assert "maintenance cannot log in" in proc.stdout


# --- --stdin: the passwords set_passwords.sh sends on a running server -------------

def _lines(superuser: str = SUPERUSER_PASSWORD, reader: str = READER_PASSWORD,
           writer: str = WRITER_PASSWORD) -> bytes:
    return f"postgres\t{superuser}\napp_readonly\t{reader}\napp_writer\t{writer}\n".encode("utf-8")


def test_stdin_sets_the_three_passwords_and_tries_each_with_a_new_connection(provisioning: Provisioning) -> None:
    # The file variables point at files with other passwords, as the files a
    # running container still sees after a replacement: they are not read.
    proc = provisioning.stdin(_lines(), files=True)
    assert proc.returncode == 0, proc.stderr
    for role, password in (("postgres", SUPERUSER_PASSWORD), ("app_readonly", READER_PASSWORD),
                           ("app_writer", WRITER_PASSWORD)):
        assert provisioning.received(role) == password.encode(), role
        assert provisioning.tried(role) == password.encode(), f"a new connection as {role} with its new password"
    commands = provisioning.commands()
    assert commands[:3] == ["\\password app_readonly", "\\password app_writer", "\\password postgres"]
    assert "pg_authid" in commands[3], "both application roles have SCRAM passwords"
    assert all("system_user" in command for command in commands[4:]) and len(commands) == 7, \
        "every password is tried after all three are set"
    logins = [call for call in provisioning.calls() if call[0] == "psql" and "-W" in call]
    for call in logins:
        # Over TCP to the container's own address, not loopback, which the image trusts.
        assert call[call.index("--host") + 1] == "192.0.2.15" and call[call.index("--port") + 1] == "5432"
    assert not any("maintenance" in command for command in commands), "maintenance stays as it is"
    for call in provisioning.calls():
        assert not any(p in arg for arg in call for p in (READER_PASSWORD, WRITER_PASSWORD, SUPERUSER_PASSWORD))
    for password in (READER_PASSWORD, WRITER_PASSWORD, SUPERUSER_PASSWORD):
        assert password not in proc.stdout + proc.stderr
    assert proc.stdout.strip() == STDIN_CONFIRMATION


def test_stdin_takes_the_lines_in_any_order(provisioning: Provisioning) -> None:
    data = f"app_writer\t{WRITER_PASSWORD}\npostgres\t{SUPERUSER_PASSWORD}\napp_readonly\t{READER_PASSWORD}\n"
    proc = provisioning.stdin(data.encode())
    assert proc.returncode == 0, proc.stderr
    assert provisioning.received("app_readonly") == READER_PASSWORD.encode()


@pytest.mark.parametrize("content, expected", PASSWORD_SAMPLES)
def test_stdin_hands_every_usable_password_to_psql_exactly(
        provisioning: Provisioning, content: bytes, expected: str) -> None:
    # set_passwords.sh sends a file's password as the containers read it: one
    # trailing line break removed.
    proc = provisioning.stdin(_lines(reader=expected))
    assert proc.returncode == 0, proc.stderr
    assert provisioning.received("app_readonly") == expected.encode("utf-8")
    assert provisioning.tried("app_readonly") == expected.encode("utf-8")
    assert expected not in proc.stdout + proc.stderr


@pytest.mark.parametrize("password, problem", [
    ("", "is empty"),
    (MARKER + "\r", "holds more than one line"),
    (MARKER + "\rsecond line", "holds more than one line"),
    ("x" * 4097, "is larger than 4096 bytes"),
], ids=["empty", "carriage-return", "second-line", "too-large"])
def test_stdin_refuses_an_unusable_password_by_role_before_any_change(
        provisioning: Provisioning, password: str, problem: str) -> None:
    proc = provisioning.stdin(_lines(writer=password))
    assert proc.returncode == 1
    assert f"the password for app_writer on standard input {problem}" in proc.stderr
    assert MARKER not in proc.stdout + proc.stderr and READER_PASSWORD not in proc.stderr
    assert provisioning.calls() == [], "nothing changed: every line is checked first"


def test_stdin_refuses_text_that_is_not_utf8(provisioning: Provisioning) -> None:
    data = b"postgres\t" + SUPERUSER_PASSWORD.encode() + b"\napp_readonly\t\xff\xfe" + MARKER.encode() + \
        b"\napp_writer\t" + WRITER_PASSWORD.encode() + b"\n"
    proc = provisioning.stdin(data)
    assert proc.returncode == 1 and "the password for app_readonly on standard input is not UTF-8 text" in proc.stderr
    assert MARKER not in proc.stderr and provisioning.calls() == []


@pytest.mark.parametrize("data, problem", [
    (b"", "standard input is empty"),
    (_lines()[:-1], "the last line of standard input has no line break"),
    (_lines() + MARKER.encode() + b"\n", "line 4 of standard input is not a role's name, a tab and its password"),
    (_lines().replace(b"app_writer\t", b"maintenance\t"), "line 3 of standard input names no role this script sets"),
    (_lines() + b"app_readonly\t" + MARKER.encode() + b"\n", "standard input names app_readonly more than once"),
    (b"\n".join(_lines().split(b"\n")[:2]) + b"\n", "standard input gives no password for app_writer"),
    (_lines().replace(b"\napp_readonly", b"\0\napp_readonly"), "standard input holds a NUL byte"),
    (_lines() + b"#" * 16384, "standard input is larger than three passwords"),
], ids=["empty", "no-final-break", "no-tab", "other-role", "twice", "missing-role", "nul", "too-large"])
def test_stdin_refuses_malformed_input_before_any_change(provisioning: Provisioning, data: bytes,
                                                          problem: str) -> None:
    proc = provisioning.stdin(data)
    assert proc.returncode == 1 and problem in proc.stderr, proc.stderr
    for value in (MARKER, READER_PASSWORD, WRITER_PASSWORD, SUPERUSER_PASSWORD):
        assert value not in proc.stdout + proc.stderr
    assert provisioning.calls() == []


@pytest.mark.parametrize("lines, problem", [
    (_lines(writer=READER_PASSWORD), "app_readonly and app_writer must not share a password"),
    (_lines(superuser=WRITER_PASSWORD), "an application role must not have the superuser's password"),
])
def test_stdin_refuses_shared_passwords(provisioning: Provisioning, lines: bytes, problem: str) -> None:
    proc = provisioning.stdin(lines)
    assert proc.returncode == 1 and problem in proc.stderr
    assert provisioning.calls() == []


def test_stdin_refuses_a_terminal(provisioning: Provisioning) -> None:
    primary, secondary = pty.openpty()
    try:
        proc = subprocess.run([str(SCRIPT), "--stdin"], env=provisioning.environment(False), cwd=provisioning.tmp,
                              stdin=secondary, capture_output=True, text=True, timeout=60)
    finally:
        os.close(secondary)
        os.close(primary)
    assert proc.returncode == 1 and "standard input, which is a terminal here" in proc.stderr
    assert provisioning.calls() == []


def test_stdin_fails_when_the_server_keeps_another_password(provisioning: Provisioning) -> None:
    # As if \password had not reached the server: after it, the stub server
    # holds another password for app_writer, and the new connection is refused.
    wrapper = provisioning.tmp / "after"
    wrapper.mkdir()
    real = provisioning.bin / "psql"
    _executable(wrapper / "psql", f'''#!/usr/bin/env bash
"{real}" "$@"; status=$?
case "$*" in *"\\\\password app_writer"*) printf 'stale' > "$STUB_STATE/password.app_writer" ;; esac
exit $status
''')
    proc = provisioning.stdin(_lines(), PATH=f"{wrapper}{os.pathsep}{provisioning.environment()['PATH']}")
    assert proc.returncode == 1
    assert "a new connection as app_writer with its new password failed" in proc.stderr
    assert "password authentication failed" in proc.stderr and "Password: " not in proc.stderr
    assert WRITER_PASSWORD not in proc.stdout + proc.stderr
    assert STDIN_CONFIRMATION not in proc.stdout


def test_stdin_does_not_count_a_connection_the_server_trusts(provisioning: Provisioning) -> None:
    proc = provisioning.stdin(_lines(), STUB_MODE="login-trusted")
    assert proc.returncode == 1
    assert "a new connection as app_readonly was not authenticated by its password" in proc.stderr
    assert STDIN_CONFIRMATION not in proc.stdout


def test_stdin_fails_when_psql_fails_without_showing_the_password(provisioning: Provisioning) -> None:
    proc = provisioning.stdin(_lines(), STUB_MODE="password-fails")
    assert proc.returncode == 1
    assert "could not set the password of app_readonly" in proc.stderr and "<password>" in proc.stderr
    assert READER_PASSWORD not in proc.stderr


@pytest.mark.parametrize("content, expected", PASSWORD_SAMPLES)
def test_every_usable_password_file_reaches_psql_exactly(
        provisioning: Provisioning, content: bytes, expected: str) -> None:
    provisioning.secret("app_readonly_password", content)
    proc = provisioning.run()
    assert proc.returncode == 0, proc.stderr
    assert provisioning.received("app_readonly") == expected.encode("utf-8")
    assert expected not in proc.stdout + proc.stderr


@pytest.mark.parametrize("content, problem", UNUSABLE_SAMPLES)
def test_an_unusable_file_is_refused_by_name_before_any_change(
        provisioning: Provisioning, content: bytes, problem: str) -> None:
    provisioning.secret("app_writer_password", content)
    proc = provisioning.run()
    assert proc.returncode == 1
    assert problem in proc.stderr and WRITER_VARIABLE in proc.stderr
    assert MARKER not in proc.stderr + proc.stdout and str(provisioning.tmp) not in proc.stderr
    assert provisioning.calls() == [], "nothing changed: both files are checked first"


def test_a_missing_file_or_a_directory_is_refused(provisioning: Provisioning) -> None:
    missing = provisioning.run(**{READONLY_VARIABLE: str(provisioning.tmp / "absent")})
    assert missing.returncode == 1 and "does not exist" in missing.stderr
    directory = provisioning.run(**{READONLY_VARIABLE: str(provisioning.tmp)})
    assert directory.returncode == 1 and "not a file" in directory.stderr
    assert provisioning.calls() == []


def test_the_two_roles_may_not_share_a_password(provisioning: Provisioning) -> None:
    provisioning.secret("app_writer_password", READER_PASSWORD.encode() + b"\r\n")
    proc = provisioning.run()
    assert proc.returncode == 1 and "must not share a password" in proc.stderr
    assert provisioning.calls() == []


def test_a_role_may_not_have_the_superusers_password(provisioning: Provisioning) -> None:
    # From the environment, as the image exports it during its initialisation ...
    proc = provisioning.run(POSTGRES_PASSWORD=WRITER_PASSWORD)
    assert proc.returncode == 1 and "superuser's password" in proc.stderr
    # ... and from its file, read as the image reads it.
    superuser = provisioning.secret("postgres_password", READER_PASSWORD.encode() + b"\n\n")
    proc = provisioning.run(POSTGRES_PASSWORD_FILE=str(superuser))
    assert proc.returncode == 1 and "superuser's password" in proc.stderr
    assert provisioning.calls() == []
    assert READER_PASSWORD not in proc.stderr and WRITER_PASSWORD not in proc.stderr


def test_a_failing_psql_fails_the_script_without_showing_the_password(provisioning: Provisioning) -> None:
    proc = provisioning.run(STUB_MODE="password-fails")
    assert proc.returncode == 1
    assert "could not set the password of app_readonly" in proc.stderr
    assert "<password>" in proc.stderr and READER_PASSWORD not in proc.stderr
    assert "Enter new password" not in proc.stderr


def test_a_result_that_does_not_check_out_fails_the_script(provisioning: Provisioning) -> None:
    proc = provisioning.run(STUB_PROVISIONED="1")
    assert proc.returncode == 1 and "do not both have a SCRAM password" in proc.stderr


@pytest.mark.parametrize("args, status", [(["--help"], 0), (["--unknown"], 1), (["--stdin", "extra"], 1),
                                          (["--resync"], 1)], ids=["help", "unknown", "extra", "former-resync"])
def test_arguments(provisioning: Provisioning, args: List[str], status: int) -> None:
    proc = provisioning.run(*args)
    assert proc.returncode == status
    assert "usage: 020_role_passwords.sh" in (proc.stdout if status == 0 else proc.stderr)
    if status:
        assert provisioning.calls() == []


def _sourced(provisioning: Provisioning, files: bool, **extra: str) -> subprocess.CompletedProcess:
    # As the image's entrypoint sources a script it cannot execute: under its
    # own options, then carrying on with its initialisation.
    code = 'set -Eeo pipefail; . "$1"; echo "initialisation continues"'
    return subprocess.run([BASH, "-c", code, "entrypoint", str(SCRIPT)], env=provisioning.environment(files, **extra),
                          cwd=provisioning.tmp, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=60)


def test_sourced_it_lets_the_initialisation_continue(provisioning: Provisioning) -> None:
    for files in (False, True):
        proc = _sourced(provisioning, files)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.rstrip().endswith("initialisation continues")
    assert provisioning.received("app_writer") == WRITER_PASSWORD.encode()


def test_sourced_a_failure_stops_the_initialisation(provisioning: Provisioning) -> None:
    provisioning.secret("app_writer_password", b"")
    proc = _sourced(provisioning, True)
    assert proc.returncode != 0
    assert "initialisation continues" not in proc.stdout
    assert "is empty" in proc.stderr
