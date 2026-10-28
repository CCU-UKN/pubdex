"""
people_pubs.password_file

The format of a password file, as the read service (PEOPLE_PUBS_API_DB_PASSWORD_FILE)
and the ingestion commands (PEOPLE_DB_PASSWORD_FILE) read it, and as the
Compose deployment's secret files hold it: one line of UTF-8 text, at most
MAX_PASSWORD_BYTES bytes, used exactly as written once one trailing line
break (LF or CRLF) is removed. Spaces and punctuation are part of the
password. An empty file, a second line or a NUL byte is refused, so that a
wrong file is reported instead of being sent as a password.

Errors name the variable that names the file, never the file's path or
content: the path can reveal a layout the operator did not mean to print,
and the content is the secret.
"""
from __future__ import annotations

#: A password file larger than this is refused rather than read.
MAX_PASSWORD_BYTES = 4096


class PasswordFileError(ValueError):
    """The file cannot be used as a password file; the message says why."""


def read_password_file(path: str, variable: str) -> str:
    """The password the file at path holds, without one trailing line break.

    variable is the setting that names the file; error messages use it.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_PASSWORD_BYTES + 1)
    except FileNotFoundError:
        raise PasswordFileError(f"{variable} names a file that does not exist") from None
    except IsADirectoryError:
        raise PasswordFileError(f"{variable} names a directory, not a file") from None
    except PermissionError:
        raise PasswordFileError(f"{variable} names a file this process may not read") from None
    except OSError:
        raise PasswordFileError(f"{variable} names a file that cannot be read") from None
    if len(data) > MAX_PASSWORD_BYTES:
        raise PasswordFileError(f"the file {variable} names is larger than {MAX_PASSWORD_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PasswordFileError(f"the file {variable} names is not UTF-8 text") from None
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise PasswordFileError(f"the file {variable} names is empty")
    if any(c in text for c in "\r\n\0"):
        raise PasswordFileError(f"the file {variable} names holds more than one line")
    return text
