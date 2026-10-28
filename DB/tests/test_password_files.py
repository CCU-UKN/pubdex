"""Password files, offline: the shared reader, and PEOPLE_DB_PASSWORD_FILE,
which gives the ingestion commands their database password.

The read service, the ingestion commands, DB/init/020_role_passwords.sh and
create_secrets.sh read the same files, so the reader's contract is pinned
once here and the shell scripts' own tests check it against the same samples
(PASSWORD_SAMPLES, UNUSABLE_SAMPLES). No error message may contain a
password or the file's path.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from people_pubs import config
from people_pubs.password_file import MAX_PASSWORD_BYTES, PasswordFileError, read_password_file

DB_DIR = Path(__file__).resolve().parents[1]
#: Values no message may contain.
MARKER = "marker-value-8c2e"
OTHER = "other-value-5d1a"
DOTENV_VALUE = "dotenv-value-77b0"
VARIABLE = "PEOPLE_DB_PASSWORD_FILE"

#: (file content, the password it holds): usable files.
PASSWORD_SAMPLES = [
    (b"example-password\n", "example-password"),
    (b"example-password\r\n", "example-password"),
    (b"example-password", "example-password"),
    (b"  spaced password  \n", "  spaced password  "),
    ("p@ss:w0rd's \"q\" \\ $HOME ;-- `x` %s {0} *\n".encode(), "p@ss:w0rd's \"q\" \\ $HOME ;-- `x` %s {0} *"),
    ("pässwörd-ünïcode\n".encode(), "pässwörd-ünïcode"),
    (b"x" * MAX_PASSWORD_BYTES, "x" * MAX_PASSWORD_BYTES),
]
#: (file content, what the refusal says): unusable files.
UNUSABLE_SAMPLES = [
    (b"", "is empty"),
    (b"\n", "is empty"),
    (b"\r\n", "is empty"),
    (MARKER.encode() + b"\nsecond line\n", "more than one line"),
    (MARKER.encode() + b"\n\n", "more than one line"),
    (MARKER.encode() + b"\r", "more than one line"),
    (MARKER.encode() + b"\0", "more than one line"),
    (b"\xff\xfe" + MARKER.encode(), "not UTF-8 text"),
    (b"x" * (MAX_PASSWORD_BYTES + 1), "larger than"),
]


@pytest.mark.parametrize("content, expected", PASSWORD_SAMPLES)
def test_a_password_file_holds_one_line_used_as_written(tmp_path: Path, content: bytes, expected: str) -> None:
    path = tmp_path / "password"
    path.write_bytes(content)
    assert read_password_file(str(path), VARIABLE) == expected


@pytest.mark.parametrize("content, problem", UNUSABLE_SAMPLES)
def test_an_unusable_password_file_is_refused_by_name_only(tmp_path: Path, content: bytes, problem: str) -> None:
    path = tmp_path / "password"
    path.write_bytes(content)
    with pytest.raises(PasswordFileError) as raised:
        read_password_file(str(path), VARIABLE)
    message = str(raised.value)
    assert problem in message and VARIABLE in message
    assert MARKER not in message and str(tmp_path) not in message


def test_a_missing_file_or_a_directory_is_refused_by_name_only(tmp_path: Path) -> None:
    for path, problem in ((tmp_path / "absent", "does not exist"), (tmp_path, "a directory, not a file")):
        with pytest.raises(PasswordFileError) as raised:
            read_password_file(str(path), VARIABLE)
        assert problem in str(raised.value) and str(tmp_path) not in str(raised.value)


# --- PEOPLE_DB_PASSWORD_FILE ----------------------------------------------------

@pytest.fixture
def environ(monkeypatch):
    """A private copy of os.environ without any connection setting."""
    copy = {key: value for key, value in os.environ.items()
            if not key.startswith(("PG", "POSTGRES_", "APP_", "PEOPLE_DB_")) and key != "MAINTENANCE_PASSWORD"}
    copy["PEOPLE_PUBS_SKIP_DOTENV"] = "1"
    monkeypatch.setattr(os, "environ", copy)
    return copy


def _password_file(tmp_path: Path, content: bytes = MARKER.encode() + b"\n") -> Path:
    path = tmp_path / "writer_password"
    path.write_bytes(content)
    return path


def test_the_password_file_supplies_pgpassword(tmp_path: Path, environ: dict) -> None:
    environ[VARIABLE] = str(_password_file(tmp_path))
    config._load_local_env([])
    assert environ["PGPASSWORD"] == MARKER


def test_the_password_file_wins_over_a_password_a_dotenv_file_would_fill_in(tmp_path: Path, environ: dict) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"PGPASSWORD={DOTENV_VALUE}\nPGUSER=app_writer\nAPP_WRITER_PASSWORD={DOTENV_VALUE}\n",
                      encoding="utf-8")
    environ["PEOPLE_PUBS_SKIP_DOTENV"] = ""
    environ[VARIABLE] = str(_password_file(tmp_path))
    config._load_local_env([dotenv])
    assert environ["PGPASSWORD"] == MARKER


def test_a_different_pgpassword_in_the_environment_is_a_configuration_error(
        tmp_path: Path, environ: dict, capsys) -> None:
    environ[VARIABLE] = str(_password_file(tmp_path))
    environ["PGPASSWORD"] = OTHER
    with pytest.raises(SystemExit) as raised:
        config._load_local_env([])
    assert raised.value.code == config.CONFIGURATION_ERROR
    err = capsys.readouterr().err
    assert "PEOPLE_DB_PASSWORD_FILE and PGPASSWORD are both set" in err
    assert MARKER not in err and OTHER not in err and str(tmp_path) not in err


def test_the_same_pgpassword_is_what_a_started_command_inherits(tmp_path: Path, environ: dict) -> None:
    environ[VARIABLE] = str(_password_file(tmp_path))
    environ["PGPASSWORD"] = MARKER
    config._load_local_env([])
    assert environ["PGPASSWORD"] == MARKER


@pytest.mark.parametrize("content, problem", [(b"", "is empty"), (MARKER.encode() + b"\nx\n", "more than one line")])
def test_an_unusable_password_file_stops_with_status_2_naming_only_the_variable(
        tmp_path: Path, environ: dict, capsys, content: bytes, problem: str) -> None:
    environ[VARIABLE] = str(_password_file(tmp_path, content))
    with pytest.raises(SystemExit) as raised:
        config._load_local_env([])
    assert raised.value.code == config.CONFIGURATION_ERROR
    err = capsys.readouterr().err
    assert problem in err and VARIABLE in err
    assert MARKER not in err and str(tmp_path) not in err
    assert "PGPASSWORD" not in environ


def test_an_empty_variable_counts_as_unset(environ: dict) -> None:
    environ[VARIABLE] = "  "
    environ["PGUSER"] = "app_writer"
    environ["APP_WRITER_PASSWORD"] = OTHER
    config._load_local_env([])
    assert environ["PGPASSWORD"] == OTHER


def test_a_command_reads_the_file_when_it_imports_the_configuration(tmp_path: Path) -> None:
    password = _password_file(tmp_path)
    code = (
        "import os, people_pubs.config; "
        f"print(os.environ['PGPASSWORD'] == {MARKER!r})"
    )
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(DB_DIR), "PEOPLE_PUBS_SKIP_DOTENV": "1",
           VARIABLE: str(password)}
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True"

    password.write_bytes(b"")
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "people_pubs: configuration error: the file PEOPLE_DB_PASSWORD_FILE names is empty" in proc.stderr
    assert "Traceback" not in proc.stderr
