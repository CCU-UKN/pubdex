"""
people_pubs.api.probe

`python -m people_pubs.api.probe`: the health check of the read service in
its container. It asks the service on this host for /readyz -- or /livez
with --live -- and exits with 0 when the answer is 200 with the expected
status, and with 1 otherwise, printing one line that says why. The image
needs no curl or wget for it: only the standard library is used, and none
of the service's own modules.

It finds the service the way the service finds its address: on the
Unix-domain socket PEOPLE_PUBS_API_UNIX_SOCKET names, or else on 127.0.0.1
at PEOPLE_PUBS_API_PORT (default 8000). A port that is not a whole number
from 1 to 65535 is a configuration error, exit status 2.

  python -m people_pubs.api.probe             # ready: database and read surface usable
  python -m people_pubs.api.probe --live      # alive: the process answers
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
from typing import Mapping, Optional, Sequence, Tuple

PORT_VARIABLE = "PEOPLE_PUBS_API_PORT"
UNIX_SOCKET_VARIABLE = "PEOPLE_PUBS_API_UNIX_SOCKET"
DEFAULT_PORT = 8000
DEFAULT_TIMEOUT_SECONDS = 4.0
CONFIGURATION_ERROR = 2
#: path and the status its body has to report
CHECKS = {"ready": ("/readyz", "ready"), "live": ("/livez", "alive")}


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a Unix-domain socket."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def connection(environ: Mapping[str, str], timeout: float) -> http.client.HTTPConnection:
    """A connection to the service as its settings place it; ValueError for a bad port."""
    unix_socket = environ.get(UNIX_SOCKET_VARIABLE, "").strip()
    if unix_socket:
        return UnixHTTPConnection(unix_socket, timeout)
    text = environ.get(PORT_VARIABLE, "").strip()
    port = DEFAULT_PORT
    if text:
        if not (text.isascii() and text.isdigit() and 1 <= int(text) <= 65535):
            raise ValueError(f"{PORT_VARIABLE} must be a whole number from 1 to 65535")
        port = int(text)
    return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)


def check(conn: http.client.HTTPConnection, path: str, expected: str) -> Tuple[bool, str]:
    """(passed, what to print) for one request."""
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        response = conn.getresponse()
        body = response.read(4096)
    except (OSError, http.client.HTTPException) as exc:
        return False, f"no answer from the service: {type(exc).__name__}"
    finally:
        conn.close()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        payload = None
    status: Optional[str] = payload.get("status") if isinstance(payload, dict) else None
    if response.status == 200 and status == expected:
        return True, expected
    reason = payload.get("reason") if isinstance(payload, dict) else None
    detail = f" ({reason})" if isinstance(reason, str) and reason else ""
    if reason in {"schema_upgrade_required", "schema_not_supported"}:
        message = payload.get("message")
        if isinstance(message, str) and message:
            detail += ": " + " ".join(message.splitlines())
    return False, f"not {expected}: HTTP {response.status}{detail}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m people_pubs.api.probe",
        description="Health check of the read service on this host: exit 0 when it is ready (or alive).",
    )
    parser.add_argument("--live", action="store_true", help="check liveness (/livez) instead of readiness (/readyz)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help=f"seconds to wait for the answer (default {DEFAULT_TIMEOUT_SECONDS:g})")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 60:
        parser.error("--timeout must be more than 0 and at most 60 seconds")
    path, expected = CHECKS["live" if args.live else "ready"]
    try:
        conn = connection(os.environ, args.timeout)
    except ValueError as exc:
        print(f"people_pubs.api.probe: configuration error: {exc}", file=sys.stderr)
        return CONFIGURATION_ERROR
    passed, message = check(conn, path, expected)
    print(message, file=sys.stdout if passed else sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
