"""people_pubs.api.probe, offline: the read service's container health check.

The probe is asked about a stand-in HTTP server and about the real service
process, both on Unix-domain sockets in tmp_path, so no network is used. It
passes on 200 with the expected status only, says why it fails otherwise --
the service's reason included, never more of its answer -- and finds the
service as the service's own settings place it.
"""
from __future__ import annotations

import http.server
import json
import os
import signal
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator, Optional, Tuple

import pytest

from people_pubs.api import probe
from tests.test_api_server import Service, _child_environment

DB_DIR = Path(__file__).resolve().parents[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    answers: dict = {}
    seen: list = []

    def do_GET(self) -> None:
        self.seen.append(self.path)
        status, body = self.answers.get(self.path, (404, b'{"error": {"code": "not_found"}}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # a Unix socket has no client address to log
        pass


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def stand_in(tmp_path: Path) -> Iterator[Tuple[Path, type]]:
    path = tmp_path / "stand-in.sock"
    handler = type("Handler", (_Handler,), {"answers": {}, "seen": []})
    server = _UnixServer(str(path), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield path, handler
    finally:
        server.shutdown()
        server.server_close()


def _probe(monkeypatch, capsys, socket_path: Optional[Path], *args: str) -> Tuple[int, str, str]:
    environ = {"PEOPLE_PUBS_API_UNIX_SOCKET": str(socket_path)} if socket_path else {}
    monkeypatch.setattr(os, "environ", environ)
    status = probe.main(list(args))
    out, err = capsys.readouterr()
    return status, out.strip(), err.strip()


def test_ready_is_200_with_status_ready(stand_in, monkeypatch, capsys) -> None:
    path, handler = stand_in
    handler.answers["/readyz"] = (200, b'{"status": "ready"}')
    assert _probe(monkeypatch, capsys, path) == (0, "ready", "")
    assert handler.seen == ["/readyz"]


def test_live_asks_for_livez(stand_in, monkeypatch, capsys) -> None:
    path, handler = stand_in
    handler.answers["/livez"] = (200, b'{"status": "alive"}')
    assert _probe(monkeypatch, capsys, path, "--live") == (0, "alive", "")
    assert handler.seen == ["/livez"]


@pytest.mark.parametrize("status, body, message", [
    (503, b'{"status": "not_ready", "reason": "database_unavailable"}', "not ready: HTTP 503 (database_unavailable)"),
    (503, b'{"status": "not_ready", "reason": "read_surface_unavailable"}',
     "not ready: HTTP 503 (read_surface_unavailable)"),
    (503, b"Service Unavailable", "not ready: HTTP 503"),
    (200, b'{"status": "alive"}', "not ready: HTTP 200"),
    (200, b"not json", "not ready: HTTP 200"),
    (500, b'{"error": {"code": "internal_error", "message": "internal error"}}', "not ready: HTTP 500"),
])
def test_anything_else_fails_and_says_why(stand_in, monkeypatch, capsys, status: int, body: bytes,
                                          message: str) -> None:
    path, handler = stand_in
    handler.answers["/readyz"] = (status, body)
    assert _probe(monkeypatch, capsys, path) == (1, "", message)


@pytest.mark.parametrize("reason", ["schema_upgrade_required", "schema_not_supported"])
def test_schema_refusal_includes_the_upgrade_command(stand_in, monkeypatch, capsys, reason) -> None:
    path, handler = stand_in
    message = "database schema version unrecorded; this application supports 0001. Run python -m people_pubs.schema upgrade."
    handler.answers["/readyz"] = (503, json.dumps({"status": "not_ready", "reason": reason, "message": message}).encode())
    assert _probe(monkeypatch, capsys, path) == (1, "", f"not ready: HTTP 503 ({reason}): {message}")


def test_no_service_fails_without_a_traceback(tmp_path: Path, monkeypatch, capsys) -> None:
    status, out, err = _probe(monkeypatch, capsys, tmp_path / "absent.sock")
    assert status == 1 and out == ""
    assert err == "no answer from the service: FileNotFoundError"


def test_the_service_is_found_as_its_settings_place_it(monkeypatch) -> None:
    unix = probe.connection({"PEOPLE_PUBS_API_UNIX_SOCKET": "/run/api.sock", "PEOPLE_PUBS_API_PORT": "9"}, 2.0)
    assert isinstance(unix, probe.UnixHTTPConnection)
    default = probe.connection({}, 2.0)
    assert (default.host, default.port, default.timeout) == ("127.0.0.1", 8000, 2.0)
    assert probe.connection({"PEOPLE_PUBS_API_PORT": " 8123 "}, 2.0).port == 8123
    for bad in ("0", "65536", "80a", "-1", "８０"):
        with pytest.raises(ValueError, match="PEOPLE_PUBS_API_PORT"):
            probe.connection({"PEOPLE_PUBS_API_PORT": bad}, 2.0)


def test_a_bad_port_is_a_configuration_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(os, "environ", {"PEOPLE_PUBS_API_PORT": "http"})
    assert probe.main([]) == probe.CONFIGURATION_ERROR
    assert "PEOPLE_PUBS_API_PORT must be a whole number" in capsys.readouterr().err


@pytest.mark.parametrize("timeout", ["0", "-1", "61"])
def test_the_timeout_is_bounded(monkeypatch, timeout: str) -> None:
    monkeypatch.setattr(os, "environ", {})
    with pytest.raises(SystemExit) as raised:
        probe.main(["--timeout", timeout])
    assert raised.value.code == 2


def test_the_probe_imports_nothing_of_the_service() -> None:
    code = (
        "import sys, people_pubs.api.probe; "
        "print(' '.join(sorted(n for n in sys.modules if n.split('.')[0] in ('people_pubs', 'psycopg', 'starlette', "
        "'uvicorn'))))"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=DB_DIR, env=_child_environment(),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["people_pubs", "people_pubs.api", "people_pubs.api.probe"]


def test_liveness_of_the_real_service_process(tmp_path: Path) -> None:
    # Readiness would make the service connect to a database, which the
    # offline tests' network guard refuses; the integration suite and the
    # Compose deployment check readiness against PostgreSQL.
    running = Service(tmp_path)
    try:
        running.wait_until_listening()
        env = _child_environment(PEOPLE_PUBS_API_UNIX_SOCKET=str(running.socket))
        live = subprocess.run([sys.executable, "-m", "people_pubs.api.probe", "--live"], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=60)
        assert (live.returncode, live.stdout.strip(), live.stderr) == (0, "alive", "")
    finally:
        running.stop(signal.SIGTERM)
        running.kill()
