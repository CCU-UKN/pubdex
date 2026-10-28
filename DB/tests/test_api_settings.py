"""The read service's configuration, offline: what it accepts, what it
refuses, and that no refusal repeats the value it refused."""
from __future__ import annotations

from pathlib import Path

import pytest

from people_pubs import password_file
from people_pubs.api.settings import (
    MAX_PASSWORD_BYTES,
    Settings,
    SettingsError,
    load_settings,
    read_password_file,
)

DSN = "host=db.example.org dbname=people_db user=app_readonly"
#: A value no error message may contain.
MARKER = "marker-value-4f1d"


def settings(**environ: str) -> Settings:
    return load_settings({"PEOPLE_PUBS_API_DSN": DSN, **environ})


def refusal(**environ: str) -> str:
    with pytest.raises(SettingsError) as raised:
        load_settings(environ)
    return str(raised.value)


def test_the_defaults() -> None:
    loaded = load_settings({"PEOPLE_PUBS_API_DSN": DSN})
    assert loaded == Settings(dsn=DSN, user="app_readonly")
    assert (loaded.host, loaded.port, loaded.unix_socket) == ("127.0.0.1", 8000, None)
    assert (loaded.pool_size, loaded.db_wait_seconds, loaded.statement_timeout_ms) == (4, 5.0, 5000)
    assert (loaded.password, loaded.log_level) == (None, "INFO")


def test_every_setting_is_read_and_empty_values_count_as_unset(tmp_path: Path) -> None:
    secret_file = tmp_path / "password"
    secret_file.write_text("example-password\n", encoding="utf-8")
    loaded = settings(
        PEOPLE_PUBS_API_DB_PASSWORD_FILE=str(secret_file),
        PEOPLE_PUBS_API_HOST="0.0.0.0",
        PEOPLE_PUBS_API_PORT="8080",
        PEOPLE_PUBS_API_POOL_SIZE="8",
        PEOPLE_PUBS_API_DB_WAIT_SECONDS="0.5",
        PEOPLE_PUBS_API_STATEMENT_TIMEOUT_MS="2500",
        PEOPLE_PUBS_API_LOG_LEVEL="warning",
    )
    assert (loaded.password, loaded.host, loaded.port) == ("example-password", "0.0.0.0", 8080)
    assert (loaded.pool_size, loaded.db_wait_seconds, loaded.statement_timeout_ms) == (8, 0.5, 2500)
    assert loaded.log_level == "WARNING"
    assert settings(PEOPLE_PUBS_API_PORT="", PEOPLE_PUBS_API_HOST=" ") == Settings(dsn=DSN, user="app_readonly")
    unix = settings(PEOPLE_PUBS_API_UNIX_SOCKET=str(tmp_path / "api.sock"))
    assert unix.unix_socket == str(tmp_path / "api.sock")


def test_the_password_never_shows_in_the_settings_repr(tmp_path: Path) -> None:
    secret_file = tmp_path / "password"
    secret_file.write_text(MARKER, encoding="utf-8")
    loaded = settings(PEOPLE_PUBS_API_DB_PASSWORD_FILE=str(secret_file))
    assert loaded.password == MARKER
    assert MARKER not in repr(loaded)


def test_the_connection_string_is_required() -> None:
    assert "PEOPLE_PUBS_API_DSN is not set" in refusal()
    assert "PEOPLE_PUBS_API_DSN is not set" in refusal(PEOPLE_PUBS_API_DSN="   ")


def test_ingestion_settings_do_not_stand_in_for_the_service_settings() -> None:
    message = refusal(PEOPLE_DB_DSN=DSN, PGHOST="db.example.org", PGUSER="app_readonly", PGDATABASE="people_db")
    assert "PEOPLE_PUBS_API_DSN is not set" in message


@pytest.mark.parametrize("dsn", [
    # libpq's own message quotes the marker for these two
    f"host=db user=app_readonly {MARKER}",
    f"postgresql://app_readonly@db/{MARKER}%zz",
    f"host=db user='app_readonly {MARKER}",
    f"host=db nosuchoption={MARKER} user=app_readonly",
])
def test_a_connection_string_that_does_not_parse_is_not_repeated(dsn: str) -> None:
    message = refusal(PEOPLE_PUBS_API_DSN=dsn)
    assert "PEOPLE_PUBS_API_DSN is not a valid libpq connection string or URI" in message
    assert MARKER not in message


@pytest.mark.parametrize("dsn", [
    f"host=db dbname=people_db user=app_readonly password={MARKER}",
    f"postgresql://app_readonly:{MARKER}@db/people_db",
    f"postgresql://app_readonly@db/people_db?password={MARKER}",
    "host=db user=app_readonly password=",
])
def test_a_password_in_the_connection_string_is_refused(dsn: str) -> None:
    message = refusal(PEOPLE_PUBS_API_DSN=dsn)
    assert "must not contain a password" in message and "PEOPLE_PUBS_API_DB_PASSWORD_FILE" in message
    assert MARKER not in message


@pytest.mark.parametrize("dsn", ["host=db dbname=people_db", "postgresql://db/people_db", "host=db user="])
def test_the_user_has_to_be_named(dsn: str) -> None:
    assert "must name the database user" in refusal(PEOPLE_PUBS_API_DSN=dsn)


def test_a_uri_and_options_are_accepted() -> None:
    loaded = settings(PEOPLE_PUBS_API_DSN="postgresql://app_readonly@db:5433/people_db?sslmode=require")
    assert loaded.user == "app_readonly"
    with_options = settings(PEOPLE_PUBS_API_DSN=DSN + " options='-c search_path=biblio'")
    assert with_options.user == "app_readonly"


@pytest.mark.parametrize("content, expected", [
    ("example-password\n", "example-password"),
    ("example-password\r\n", "example-password"),
    ("example-password", "example-password"),
    ("  spaced password  \n", "  spaced password  "),
    ("p@ss:w0rd's \"q\" \\ $HOME ;--\n", "p@ss:w0rd's \"q\" \\ $HOME ;--"),
    ("pässwörd-ünïcode\n", "pässwörd-ünïcode"),
])
def test_a_password_file_holds_one_line_used_as_written(tmp_path: Path, content: str, expected: str) -> None:
    path = tmp_path / "password"
    path.write_bytes(content.encode("utf-8"))
    assert read_password_file(str(path)) == expected


@pytest.mark.parametrize("content, problem", [
    (b"", "is empty"),
    (b"\n", "is empty"),
    (MARKER.encode() + b"\nsecond line\n", "more than one line"),
    (MARKER.encode() + b"\n\n", "more than one line"),
    (MARKER.encode() + b"\0", "more than one line"),
    (b"\xff\xfe" + MARKER.encode(), "not UTF-8 text"),
    (MARKER.encode() * (MAX_PASSWORD_BYTES // len(MARKER) + 1), "larger than"),
])
def test_an_unusable_password_file_is_refused_without_its_content(tmp_path: Path, content: bytes, problem: str) -> None:
    path = tmp_path / "password"
    path.write_bytes(content)
    message = refusal(PEOPLE_PUBS_API_DSN=DSN, PEOPLE_PUBS_API_DB_PASSWORD_FILE=str(path))
    assert problem in message and "PEOPLE_PUBS_API_DB_PASSWORD_FILE" in message
    assert MARKER not in message and str(path) not in message


def test_a_missing_password_file_or_a_directory_is_refused(tmp_path: Path) -> None:
    missing = refusal(PEOPLE_PUBS_API_DSN=DSN, PEOPLE_PUBS_API_DB_PASSWORD_FILE=str(tmp_path / "absent"))
    assert "does not exist" in missing
    directory = refusal(PEOPLE_PUBS_API_DSN=DSN, PEOPLE_PUBS_API_DB_PASSWORD_FILE=str(tmp_path))
    assert "a directory, not a file" in directory


def test_an_unreadable_password_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # File permissions do not stop root, so the refusal is produced directly.
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(tmp_path / "password"))

    monkeypatch.setattr(password_file, "open", refuse, raising=False)
    message = refusal(PEOPLE_PUBS_API_DSN=DSN, PEOPLE_PUBS_API_DB_PASSWORD_FILE=str(tmp_path / "password"))
    assert "may not read" in message and str(tmp_path) not in message


@pytest.mark.parametrize("name, value, words", [
    ("PEOPLE_PUBS_API_PORT", "0", "from 1 to 65535"),
    ("PEOPLE_PUBS_API_PORT", "65536", "from 1 to 65535"),
    ("PEOPLE_PUBS_API_PORT", "80a", "from 1 to 65535"),
    ("PEOPLE_PUBS_API_PORT", "-1", "from 1 to 65535"),
    ("PEOPLE_PUBS_API_POOL_SIZE", "0", "from 1 to 32"),
    ("PEOPLE_PUBS_API_POOL_SIZE", "33", "from 1 to 32"),
    ("PEOPLE_PUBS_API_DB_WAIT_SECONDS", "0", "from 0.1 to 300"),
    ("PEOPLE_PUBS_API_DB_WAIT_SECONDS", "301", "from 0.1 to 300"),
    ("PEOPLE_PUBS_API_DB_WAIT_SECONDS", "1e3", "from 0.1 to 300"),
    ("PEOPLE_PUBS_API_DB_WAIT_SECONDS", "nan", "from 0.1 to 300"),
    ("PEOPLE_PUBS_API_STATEMENT_TIMEOUT_MS", "99", "from 100 to 600000"),
    ("PEOPLE_PUBS_API_STATEMENT_TIMEOUT_MS", "600001", "from 100 to 600000"),
    ("PEOPLE_PUBS_API_LOG_LEVEL", "verbose", "DEBUG, INFO, WARNING, ERROR"),
    ("PEOPLE_PUBS_API_HOST", "local host", "without spaces"),
    ("PEOPLE_PUBS_API_UNIX_SOCKET", "relative/api.sock", "absolute path"),
])
def test_out_of_range_settings_are_refused_by_name(name: str, value: str, words: str) -> None:
    message = refusal(PEOPLE_PUBS_API_DSN=DSN, **{name: value})
    assert message.startswith(name) and words in message
