"""DB/fetch_verified.py, offline: the one downloader of the installations and
image builds.

Downloads over https: run in this process, through urllib's own handlers --
redirects included -- with only the connections of http.client replaced: a
transport that answers from a table and records every connection, before
anything is sent, and every local file that is opened. Shown: a redirect to
http:, ftp: or file: is refused before that destination is contacted, also
when it would lead back to https:; a page fetched over https: cannot lead to
a local file or to plain HTTP; redirects from https: to https: are followed,
with certificate verification and the timeout; a file whose SHA-256 the
lock does not list, a failed request, a file or page over its size limit, a
total over its limit and a download past its deadline leave nothing under
the file's name.

Whole runs read a local copy of an index (file: URLs) of synthetic wheels:
exactly one file per pin, the wheel that suits the target, a source archive
only where --allow-source names the project, and a refusal for a pin with no
such file, with two, or with a yanked one, for locks that disagree and for a
wheelhouse directory that is not empty. The staggered connection attempts
are shown with stand-in sockets, as the tests may not open network sockets.
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import io
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

import fetch_verified
from locked_files import LockError, Pin, Target, parse_filename

SCRIPT = Path(__file__).resolve().parents[1] / "fetch_verified.py"
TARGET = Target((3, 11), "x86_64", (2, 41))
OTHER_HASH = "0" * 64


def wheel(name: str, version: str, extra: bytes = b"") -> bytes:
    """A minimal wheel of name==version."""
    buffer = io.BytesIO()
    distribution = name.replace("-", "_")
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{distribution}/__init__.py", b"" + extra)
        archive.writestr(f"{distribution}-{version}.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return buffer.getvalue()


class Index:
    """A local copy of an index (PEP 503): pages of links to files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files = root / "files"
        self.files.mkdir(parents=True)
        self.links: dict = {}

    def serve(self, project: str, filename: str, content: bytes, fragment: bool = True, yanked: bool = False,
              href: str = "") -> str:
        (self.files / filename).write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        link = href or f"../../files/{filename}" + (f"#sha256={digest}" if fragment else "")
        self.links.setdefault(project, []).append(
            f'<a href="{link}"{" data-yanked" if yanked else ""}>{filename}</a>')
        page = self.root / "simple" / project
        page.mkdir(parents=True, exist_ok=True)
        (page / "index.html").write_text("<!DOCTYPE html><html><body>\n" + "\n".join(self.links[project])
                                         + "\n</body></html>\n", encoding="utf-8")
        return digest

    @property
    def url(self) -> str:
        return (self.root / "simple").as_uri() + "/"


def lock(path: Path, *pins: tuple) -> Path:
    """A hashed lock of (name, version, hashes...) pins."""
    text = "#\n# A synthetic lock.\n#\n"
    for name, version, *hashes in pins:
        text += f"{name}=={version}" + "".join(f" \\\n    --hash=sha256:{h}" for h in hashes) + "\n    # via test\n"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def index(tmp_path: Path) -> Index:
    return Index(tmp_path / "index")


def fetch(index: Index, locks: list, dest: Path, sources=(), target: Target = TARGET) -> dict:
    return fetch_verified.fetch_wheels(locks, dest, index.url, sources, target, fetch_verified.Fetcher())


# --- choosing the files ------------------------------------------------------------------


def test_exactly_one_file_per_pin_the_wheel_for_the_target_and_a_source_archive_only_where_allowed(
        index: Index, tmp_path: Path) -> None:
    pure = index.serve("alpha", "alpha-1.0-py3-none-any.whl", wheel("alpha", "1.0"))
    index.serve("alpha", "alpha-1.0.tar.gz", b"alpha source")
    built = index.serve("beta", "beta-2.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                        wheel("beta", "2.0"))
    index.serve("beta", "beta-2.0-cp311-cp311-musllinux_1_2_x86_64.whl", wheel("beta", "2.0", b"musl"))
    index.serve("beta", "beta-2.0-cp312-cp312-manylinux_2_17_x86_64.whl", wheel("beta", "2.0", b"312"))
    source = index.serve("gamma-c", "gamma_c-3.0.tar.gz", b"gamma source")
    first = lock(tmp_path / "first.txt", ("alpha", "1.0", pure, OTHER_HASH), ("beta", "2.0", built))
    second = lock(tmp_path / "second.txt", ("gamma-c", "3.0", source), ("alpha", "1.0", pure))
    dest = tmp_path / "wheelhouse"
    fetched = fetch(index, [first, second], dest, sources=["gamma_c"])
    assert sorted(path.name for path in dest.iterdir()) == [
        "alpha-1.0-py3-none-any.whl", "beta-2.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "gamma_c-3.0.tar.gz"]
    assert {name: digest for name, (_file, digest, _size) in fetched.items()} == {
        "alpha": pure, "beta": built, "gamma-c": source}


@pytest.mark.parametrize("served, message", [
    ([("beta-2.0-cp312-cp312-manylinux_2_17_x86_64.whl", True)], "lists no wheel for CPython 3.11, x86_64"),
    ([("beta-2.0-cp311-cp311-manylinux_2_43_x86_64.whl", True)], "lists no wheel for CPython 3.11"),
    ([("beta-2.0-cp311-cp311-musllinux_1_2_x86_64.whl", True)], "lists no wheel"),
    ([("beta-2.0-cp311-cp311-manylinux_2_17_aarch64.whl", True)], "lists no wheel"),
    ([("beta-2.0.tar.gz", True)], "lists no wheel"),
    ([("beta-2.0-py3-none-any.whl", True), ("beta-2.0-cp311-abi3-manylinux_2_28_x86_64.whl", True)],
     "more than one wheel for CPython 3.11, x86_64, glibc 2.41 would do"),
], ids=["other-python", "newer-glibc", "musl", "other-machine", "source-only", "ambiguous"])
def test_a_pin_without_exactly_one_suitable_wheel_is_refused(index: Index, tmp_path: Path, served: list,
                                                            message: str) -> None:
    hashes = [index.serve("beta", filename, wheel("beta", "2.0", filename.encode())) for filename, _ in served]
    dest = tmp_path / "wheelhouse"
    with pytest.raises(fetch_verified.FetchError, match=re.escape(message)):
        fetch(index, [lock(tmp_path / "lock.txt", ("beta", "2.0", *hashes))], dest)
    assert list(dest.iterdir()) == []


def test_a_yanked_file_and_a_file_whose_link_names_another_hash_are_not_taken(index: Index, tmp_path: Path) -> None:
    digest = index.serve("alpha", "alpha-1.0-py3-none-any.whl", wheel("alpha", "1.0"), yanked=True)
    with pytest.raises(fetch_verified.FetchError, match="the index marks its wheel .* yanked: alpha-1.0-py3-none-any"):
        fetch(index, [lock(tmp_path / "lock.txt", ("alpha", "1.0", digest))], tmp_path / "one")
    index.serve("beta", "beta-1.0-py3-none-any.whl", wheel("beta", "1.0"))
    with pytest.raises(fetch_verified.FetchError, match="lists no wheel .* whose hash the lock lists"):
        fetch(index, [lock(tmp_path / "lock2.txt", ("beta", "1.0", OTHER_HASH))], tmp_path / "two")


def test_a_corrupted_file_is_not_kept(index: Index, tmp_path: Path) -> None:
    # The link names no hash, so the file is downloaded; its SHA-256 decides.
    index.serve("alpha", "alpha-1.0-py3-none-any.whl", wheel("alpha", "1.0"), fragment=False)
    dest = tmp_path / "wheelhouse"
    with pytest.raises(fetch_verified.FetchError,
                       match="has the SHA-256 [0-9a-f]{64}, which is not one the repository fixes; it was not kept"):
        fetch(index, [lock(tmp_path / "lock.txt", ("alpha", "1.0", OTHER_HASH))], dest)
    assert list(dest.iterdir()) == [], "neither the file nor a partial download is left"


def test_a_file_matching_the_lock_that_is_not_a_wheel_of_the_pin_is_removed(index: Index, tmp_path: Path) -> None:
    digest = index.serve("alpha", "alpha-1.0-py3-none-any.whl", wheel("other", "1.0"))
    dest = tmp_path / "wheelhouse"
    with pytest.raises(fetch_verified.FetchError, match="matches the lock but is not a wheel of alpha==1.0"):
        fetch(index, [lock(tmp_path / "lock.txt", ("alpha", "1.0", digest))], dest)
    assert list(dest.iterdir()) == []


def test_locks_that_disagree_options_and_an_occupied_directory_are_refused_before_any_download(
        index: Index, tmp_path: Path) -> None:
    first = lock(tmp_path / "a.txt", ("alpha", "1.0", OTHER_HASH))
    with pytest.raises(LockError, match="pins alpha==2.0, another lock alpha==1.0"):
        fetch(index, [first, lock(tmp_path / "b.txt", ("alpha", "2.0", OTHER_HASH))], tmp_path / "w1")
    with pytest.raises(LockError, match="list no common hash for alpha==1.0"):
        fetch(index, [first, lock(tmp_path / "c.txt", ("alpha", "1.0", "1" * 64))], tmp_path / "w2")
    for text in ("alpha==1.0\n", "--index-url https://elsewhere.invalid/simple/\n",
                 f'alpha==1.0 ; python_version < "3.11" --hash=sha256:{OTHER_HASH}\n',
                 f"alpha @ https://elsewhere.invalid/alpha.whl --hash=sha256:{OTHER_HASH}\n"):
        (tmp_path / "bad.txt").write_text(text, encoding="utf-8")
        with pytest.raises(LockError, match="is not name==version followed by --hash"):
            fetch(index, [tmp_path / "bad.txt"], tmp_path / "w3")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "stray.whl").write_bytes(b"stray")
    with pytest.raises(fetch_verified.FetchError, match="has to be a new or empty directory"):
        fetch(index, [first], occupied)
    with pytest.raises(fetch_verified.FetchError, match="--allow-source names beta, which no lock pins"):
        fetch(index, [first], tmp_path / "w4", sources=["beta"])
    assert not (index.root / "simple").exists(), "no index page was read"
    assert [path.name for path in occupied.iterdir()] == ["stray.whl"]


@pytest.mark.parametrize("tags, accepted", [
    ("py3-none-any", True), ("py2.py3-none-any", True), ("py311-none-any", True), ("py310-none-any", True),
    ("cp311-cp311-manylinux_2_17_x86_64", True), ("cp311-cp311-manylinux2014_x86_64", True),
    ("cp311-cp311-manylinux1_x86_64", True), ("cp39-abi3-manylinux_2_28_x86_64", True),
    ("cp311-cp311-manylinux_2_41_x86_64", True), ("cp311-cp311-manylinux_2_42_x86_64", False),
    ("cp312-cp312-manylinux_2_17_x86_64", False), ("cp312-abi3-manylinux_2_17_x86_64", False),
    ("cp311-cp311-musllinux_1_2_x86_64", False), ("cp311-cp311-linux_x86_64", False),
    ("cp311-cp311-manylinux_2_17_aarch64", False), ("cp311-cp311-win_amd64", False), ("py2-none-any", False),
])
def test_the_target_accepts_pure_wheels_and_glibc_wheels_no_newer_than_its_own(tags: str, accepted: bool) -> None:
    parsed = parse_filename(f"example-1.0-{tags}.whl")
    assert parsed is not None and TARGET.accepts(parsed.tags) is accepted


# --- the transport -----------------------------------------------------------------------

HTTPS_INDEX = "https://index.invalid/simple/"
HTTPS_PAGE = "https://index.invalid/simple/alpha/"
FILENAME = "alpha-1.0-py3-none-any.whl"


class Transport:
    """http.client's connections for one test, answering from routes -- URL to
    (status, headers, body) -- instead of the network. Every connection is
    recorded as scheme://host before anything is sent on it, with its timeout
    and certificate checks; every request by its URL; and every local file
    urllib opens by its file: URL."""

    def __init__(self) -> None:
        self.routes: dict = {}
        self.contacted: list = []
        self.requested: list = []
        self.connections: set = set()

    def connect(self, scheme: str):
        def connect(connection) -> None:
            self.contacted.append(f"{scheme}://{connection.host}")
            context = getattr(connection, "_context", None)
            self.connections.add((connection.timeout, context and context.verify_mode,
                                  context and context.check_hostname))
            connection.sock = _Socket(self, scheme, connection.host)
        return connect

    def page(self, href: str) -> tuple:
        return 200, {"Content-Type": "text/html"}, f'<a href="{href}">{FILENAME}</a>'.encode()


class _Socket:
    def __init__(self, transport: Transport, scheme: str, host: str) -> None:
        self.transport, self.scheme, self.host, self.sent = transport, scheme, host, b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, mode: str):
        url = f"{self.scheme}://{self.host}{self.sent.split(b' ', 2)[1].decode()}"
        self.transport.requested.append(url)
        status, headers, body = self.transport.routes.get(url, (404, {}, b""))
        head = f"HTTP/1.1 {status} Reason\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        return io.BytesIO(f"{head}Content-Length: {len(body)}\r\n\r\n".encode() + body)

    def close(self) -> None:
        pass


def moved(location: str, status: int = 302) -> tuple:
    return status, {"Location": location}, b""


@pytest.fixture
def transport(monkeypatch) -> Transport:
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    transport = Transport()
    monkeypatch.setattr(http.client.HTTPConnection, "connect", transport.connect("http"))
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", transport.connect("https"))
    open_local_file = urllib.request.FileHandler.open_local_file

    def recorded(handler, request):
        transport.contacted.append(request.full_url)
        return open_local_file(handler, request)

    monkeypatch.setattr(urllib.request.FileHandler, "open_local_file", recorded)
    return transport


@pytest.mark.parametrize("chain", [
    [moved("http://plain.invalid/simple/alpha/", 301)],
    [moved("http://plain.invalid/hop"), moved("https://index.invalid/again/alpha/", 307)],
    [moved("ftp://files.invalid/simple/alpha/", 308)],
], ids=["https-http", "https-http-https", "https-ftp"])
def test_a_redirect_from_https_to_another_scheme_is_refused_before_it_is_contacted(transport, chain: list) -> None:
    url = HTTPS_PAGE
    for response in chain:
        transport.routes[url] = response
        url = response[1]["Location"]
    # Served, but only if it is ever asked for.
    transport.routes[url] = transport.page(f"https://index.invalid/files/{FILENAME}")
    with pytest.raises(fetch_verified.FetchError, match=re.escape(
            f"refusing {HTTPS_PAGE}: it redirects to {chain[0][1]['Location']}, and only https: URLs are used")):
        fetch_verified.index_files(fetch_verified.Fetcher(), HTTPS_INDEX, "alpha")
    assert transport.contacted == ["https://index.invalid"] and transport.requested == [HTTPS_PAGE]


def test_an_https_response_cannot_lead_to_a_local_file_or_plain_http(transport, tmp_path) -> None:
    local = tmp_path / FILENAME
    local.write_bytes(wheel("alpha", "1.0"))
    fetcher = fetch_verified.Fetcher()
    # Neither by a redirect, which urllib refuses itself ...
    transport.routes[HTTPS_PAGE] = moved(local.as_uri())
    with pytest.raises(urllib.error.HTTPError, match=re.escape(f"Redirection to url '{local.as_uri()}' is not allowed")):
        fetch_verified.index_files(fetcher, HTTPS_INDEX, "alpha")
    # ... nor by a link of the page, to a local file or over plain HTTP.
    for href in (local.as_uri(), f"http://files.invalid/{FILENAME}"):
        transport.routes[HTTPS_PAGE] = transport.page(href)
        with pytest.raises(fetch_verified.FetchError, match=re.escape(
                f"refusing {href}: the https: page {HTTPS_PAGE} links to it, and only https: URLs")):
            fetch_verified.index_files(fetcher, HTTPS_INDEX, "alpha")
    assert transport.contacted == ["https://index.invalid"] * 3
    with pytest.raises(fetch_verified.FetchError, match="only https: and file: URLs are used"):
        fetch_verified.index_files(fetcher, "http://index.invalid/simple/", "alpha")


def test_redirects_from_https_to_https_are_followed_with_verified_certificates(transport, tmp_path) -> None:
    content = wheel("alpha", "1.0")
    digest = hashlib.sha256(content).hexdigest()
    transport.routes.update({
        HTTPS_PAGE: moved("https://mirror.invalid/simple/alpha/", 301),
        "https://mirror.invalid/simple/alpha/": transport.page(f"../../files/{FILENAME}#sha256={digest}"),
        f"https://mirror.invalid/files/{FILENAME}": moved(f"https://cdn.invalid/{FILENAME}", 307),
        f"https://cdn.invalid/{FILENAME}": (200, {}, content),
    })
    fetcher = fetch_verified.Fetcher()
    [(name, url, sha256, yanked)] = fetch_verified.index_files(fetcher, HTTPS_INDEX, "alpha")
    assert (name, url, sha256, yanked) == (FILENAME, f"https://mirror.invalid/files/{FILENAME}", digest, False), \
        "relative links follow the page's final URL"
    assert fetcher.save(url, tmp_path / FILENAME, {digest}) == (digest, len(content))
    assert (tmp_path / FILENAME).read_bytes() == content
    assert transport.contacted == ["https://index.invalid", "https://mirror.invalid", "https://mirror.invalid",
                                   "https://cdn.invalid"]
    assert transport.connections == {(fetch_verified.TIMEOUT_SECONDS, ssl.CERT_REQUIRED, True)}


def test_a_failed_request_keeps_nothing(transport, tmp_path) -> None:
    transport.routes[f"https://index.invalid/files/{FILENAME}"] = (503, {}, b"unavailable")
    with pytest.raises(urllib.error.HTTPError, match="503"):
        fetch_verified.Fetcher().save(f"https://index.invalid/files/{FILENAME}", tmp_path / FILENAME, {OTHER_HASH})
    assert list(tmp_path.iterdir()) == []


def test_input_beyond_the_limits_is_refused_as_it_arrives(transport, monkeypatch, tmp_path) -> None:
    url = f"https://index.invalid/files/{FILENAME}"
    transport.routes[url] = (200, {}, b"x" * 1001)
    with pytest.raises(fetch_verified.FetchError, match="is larger than 1000 bytes"):
        fetch_verified.Fetcher(max_bytes=1000).save(url, tmp_path / FILENAME, {OTHER_HASH})
    with pytest.raises(fetch_verified.FetchError, match="the downloads exceed 1500 bytes in total"):
        fetcher = fetch_verified.Fetcher(max_bytes=2000, max_total_bytes=1500)
        fetcher.total = 1000
        fetcher.save(url, tmp_path / FILENAME, {OTHER_HASH})
    monkeypatch.setattr(fetch_verified, "MAX_PAGE_BYTES", 100)
    transport.routes[HTTPS_PAGE] = (200, {}, b"<a>" * 40)
    with pytest.raises(fetch_verified.FetchError, match=re.escape(f"{HTTPS_PAGE} is larger than 100 bytes")):
        fetch_verified.index_files(fetch_verified.Fetcher(), HTTPS_INDEX, "alpha")
    assert list(tmp_path.iterdir()) == [], "nothing, not even a partial download, is left"


# --- the staggered connections -----------------------------------------------------------


class _Attempt:
    """A stand-in socket: connect_ex reports the connection in progress;
    select reports it ready when the test says so."""

    attempts: list = []

    def __init__(self, family, kind, proto) -> None:
        self.family, self.closed, self.timeout, self.address = family, False, None, None
        _Attempt.attempts.append(self)

    def setblocking(self, flag) -> None:
        pass

    def connect_ex(self, address) -> int:
        self.address = address
        return errno.EINPROGRESS

    def getsockopt(self, level, option) -> int:
        return 0 if self.address[0] == "192.0.2.10" else errno.ECONNREFUSED

    def settimeout(self, timeout) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True


def test_an_address_that_does_not_answer_costs_a_quarter_of_a_second_not_the_timeout(monkeypatch) -> None:
    # An IPv6 address that never answers, a refused one, then the IPv4
    # address that does: attempts interleave the families and start every
    # CONNECT_DELAY while the earlier ones are pending.
    infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0)),
             (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::2", 443, 0, 0)),
             (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 443))]
    waits = []

    def select(read, write, error, timeout):
        waits.append((sorted(attempt.address[0] for attempt in write), timeout))
        return [], [attempt for attempt in write if attempt.address[0] == "192.0.2.10"], []

    _Attempt.attempts = []
    monkeypatch.setattr(fetch_verified.socket, "getaddrinfo", lambda *args: infos)
    monkeypatch.setattr(fetch_verified.socket, "socket", _Attempt)
    monkeypatch.setattr(fetch_verified.select, "select", select)
    sock = fetch_verified.connect(("index.invalid", 443), timeout=60)
    assert [attempt.address[0] for attempt in _Attempt.attempts] == ["2001:db8::1", "192.0.2.10"]
    assert sock.address[0] == "192.0.2.10" and sock.timeout == 60
    assert waits[0] == (["2001:db8::1"], fetch_verified.CONNECT_DELAY)
    assert _Attempt.attempts[0].closed, "the pending attempt is closed"


def test_connections_go_through_the_staggered_connect_under_the_deadline() -> None:
    deadline = fetch_verified.Deadline(60)
    connection = fetch_verified._Connection("index.invalid", 443, context=ssl.create_default_context(),
                                            deadline=deadline)
    assert connection._create_connection.func is fetch_verified.connect
    assert connection._create_connection.keywords == {"deadline": deadline}


# --- the deadline, against real sockets --------------------------------------------------
#
# A server on a Unix-domain socket stands in for the index -- the tests may
# open no network socket -- and every host name resolves to it, so that the
# downloader's own connect, http.client and urllib run unchanged against a
# peer that stalls. TLS is left out by a context that hands the socket back,
# except where the TLS handshake itself is what stalls. The per-operation
# timeout is far longer than the deadline, so only the deadline can end a
# stalled download within the bounds the tests check.

LONG_TIMEOUT = 30.0
DEADLINE = 0.5
#: Generous for a loaded machine, and far below what any stall here would
#: take without the deadline.
BOUND = DEADLINE + 2.5
#: The servers give up after this long, so that a broken deadline fails the
#: test instead of hanging it.
GIVE_UP = 12.0


class _UnixSocket(socket.socket):
    """A Unix-domain socket that ignores TCP's options, as http.client sets
    TCP_NODELAY on every connection."""

    def setsockopt(self, level, option, value, *rest):
        if level == socket.IPPROTO_TCP:
            return None
        return super().setsockopt(level, option, value, *rest)


class _NoTLS:
    verify_mode, check_hostname = ssl.CERT_REQUIRED, True

    def wrap_socket(self, sock, server_hostname=None):
        return sock


def read_request(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        more = conn.recv(4096)
        if not more:
            break
        data += more
    return data


def drip(conn: socket.socket, started: float, piece: bytes = b"x") -> None:
    """Sends piece every 20 ms -- well within any timeout -- until the peer
    shuts the connection down or GIVE_UP passes."""
    while time.monotonic() - started < GIVE_UP:
        conn.sendall(piece)
        time.sleep(0.02)


class LocalIndex:
    """Serves each connection with the next handler, in a thread of its own,
    and records how each one ended."""

    def __init__(self, path: Path, *handlers) -> None:
        self.handlers, self.requests, self.ended = list(handlers), [], []
        self.threads: list = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        self.path = str(path)
        self.accepting = threading.Thread(target=self._accept, daemon=True)
        self.accepting.start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(conn, self.handlers.pop(0)), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _serve(self, conn: socket.socket, handler) -> None:
        with conn:
            try:
                self.requests.append(read_request(conn).split(b"\r\n", 1)[0].decode())
                handler(conn, time.monotonic())
                self.ended.append("served")
            except OSError as exc:
                self.ended.append(type(exc).__name__)

    def close(self) -> None:
        if self.listener.fileno() != -1:
            self.listener.shutdown(socket.SHUT_RDWR)
            self.listener.close()
        self.accepting.join(5)
        for thread in self.threads:
            thread.join(GIVE_UP + 5)


def fetch_threads() -> list:
    return [thread for thread in threading.enumerate() if thread.name.startswith("fetch_verified")]


def open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.fixture
def served(monkeypatch, tmp_path: Path):
    """Starts a LocalIndex with the given handlers that every host name
    resolves to; checks afterwards that the downloader left no thread and no
    descriptor behind."""
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    sockets = types.SimpleNamespace(**vars(socket))
    sockets.socket = _UnixSocket
    monkeypatch.setattr(fetch_verified, "socket", sockets)
    monkeypatch.setattr(fetch_verified.ssl, "create_default_context", _NoTLS)
    started: list = []
    before = open_descriptors()

    def start(*handlers) -> LocalIndex:
        index = LocalIndex(tmp_path / "index.sock", *handlers)
        sockets.getaddrinfo = lambda host, port, *rest: [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", index.path)]
        started.append(index)
        return index

    yield start
    for index in started:
        index.close()
    assert fetch_threads() == [], "the deadline's timer is stopped and joined"
    assert open_descriptors() == before, "every socket and duplicate is closed"


def timed(call):
    started = time.monotonic()
    try:
        call()
    except fetch_verified.FetchError as exc:
        return time.monotonic() - started, str(exc)
    raise AssertionError("the download did not fail")


def test_a_body_that_trickles_into_a_buffered_read_is_cut_off_at_the_deadline(served, tmp_path: Path) -> None:
    # One byte every 20 ms of a body of 100000: each arrives well within the
    # timeout, and the first buffered read of 65536 bytes would wait for
    # twenty minutes. The deadline shuts the connection down under that
    # read, which returns no chunk at all.
    def trickle(conn, started):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        drip(conn, started)

    index = served(trickle, trickle)
    url = f"https://index.invalid/files/{FILENAME}"
    chunks: list = []
    fetcher = fetch_verified.Fetcher(timeout=LONG_TIMEOUT, deadline=DEADLINE)
    elapsed, message = timed(lambda: fetcher.read(url, chunks.append, fetch_verified.MAX_BYTES))
    assert message == f"{url} did not arrive within {DEADLINE:g} seconds"
    assert DEADLINE <= elapsed < BOUND, elapsed
    assert chunks == [] and fetcher.total == 0, "the read was blocked when the deadline came"
    # And saved: nothing is left under the file's name, nor a partial file.
    dest = tmp_path / "dest"
    dest.mkdir()
    elapsed, _ = timed(lambda: fetcher.save(url, dest / FILENAME, {OTHER_HASH}))
    assert DEADLINE <= elapsed < BOUND and list(dest.iterdir()) == []
    index.close()
    assert index.ended == ["BrokenPipeError"] * 2, "the server saw each connection shut down"


def test_a_lookup_that_does_not_return_is_abandoned_and_its_thread_ends(served, monkeypatch) -> None:
    index = served()
    release, calls = threading.Event(), []

    def stalled(host, port, *rest):
        calls.append(host)
        release.wait(GIVE_UP)
        return [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", index.path)]

    monkeypatch.setattr(fetch_verified.socket, "getaddrinfo", stalled)
    fetcher = fetch_verified.Fetcher(timeout=LONG_TIMEOUT, deadline=DEADLINE)
    elapsed, message = timed(lambda: fetcher.read("https://index.invalid/simple/", list().append, 100))
    assert "did not arrive within 0.5 seconds" in message and DEADLINE <= elapsed < BOUND, (elapsed, message)
    [lookup] = [thread for thread in fetch_threads() if thread.name == "fetch_verified lookup of index.invalid"]
    release.set()
    lookup.join(5)
    assert not lookup.is_alive() and calls == ["index.invalid"]
    assert index.requests == [], "what the lookup found late was dropped"


def test_a_tls_handshake_that_stalls_is_cut_off_at_the_deadline(served, monkeypatch) -> None:
    # The real TLS client against a peer that accepts and never answers its
    # hello: the handshake waits on the socket until the deadline shuts it
    # down.
    monkeypatch.setattr(fetch_verified.ssl, "create_default_context", ssl.create_default_context)
    index = served(lambda conn, started: time.sleep(GIVE_UP) if conn.recv(1) else None)
    fetcher = fetch_verified.Fetcher(timeout=LONG_TIMEOUT, deadline=DEADLINE)
    elapsed, message = timed(lambda: fetcher.read("https://index.invalid/simple/", list().append, 100))
    assert message == f"https://index.invalid/simple/ did not arrive within {DEADLINE:g} seconds"
    assert DEADLINE <= elapsed < BOUND, elapsed


def test_the_deadline_spans_redirects_and_response_headers_that_trickle(served) -> None:
    # A redirect after 0.3 s, then a status line and one header byte every
    # 20 ms: the deadline counts from the first request, so the download
    # ends at the deadline, not 0.3 s after it.
    def redirect(conn, started):
        time.sleep(0.3)
        conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: https://mirror.invalid/files/a.whl\r\n"
                     b"Content-Length: 0\r\n\r\n")

    def headers(conn, started):
        conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        drip(conn, started)

    index = served(redirect, headers)
    fetcher = fetch_verified.Fetcher(timeout=LONG_TIMEOUT, deadline=DEADLINE)
    elapsed, message = timed(lambda: fetcher.read("https://index.invalid/files/a.whl", list().append, 100))
    assert message == f"https://index.invalid/files/a.whl did not arrive within {DEADLINE:g} seconds"
    assert DEADLINE <= elapsed < DEADLINE + 0.3 + 0.2 or elapsed < BOUND, elapsed
    index.close()
    assert index.requests == ["GET /files/a.whl HTTP/1.1"] * 2
    assert index.ended == ["served", "BrokenPipeError"]


def test_a_download_within_its_deadline_is_kept(served, tmp_path: Path) -> None:
    content = wheel("alpha", "1.0") * 50

    def answer(conn, started):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(content))
        for offset in range(0, len(content), 1000):
            conn.sendall(content[offset:offset + 1000])

    served(answer)
    digest = hashlib.sha256(content).hexdigest()
    fetcher = fetch_verified.Fetcher(timeout=LONG_TIMEOUT, deadline=10)
    assert fetcher.save(f"https://index.invalid/files/{FILENAME}", tmp_path / FILENAME, {digest}) == (
        digest, len(content))
    assert (tmp_path / FILENAME).read_bytes() == content


# --- whole runs --------------------------------------------------------------------------


def test_main_fills_a_wheelhouse_from_a_local_index(index: Index, tmp_path: Path, capsys) -> None:
    digest = index.serve("alpha", FILENAME, wheel("alpha", "1.0"))
    dest = tmp_path / "wheelhouse"
    status = fetch_verified.main(["wheels", "--dest", str(dest), "--index-url", index.url,
                                  str(lock(tmp_path / "lock.txt", ("alpha", "1.0", digest)))])
    assert status == 0
    assert [path.name for path in dest.iterdir()] == [FILENAME]
    out = capsys.readouterr().out
    assert f"fetch_verified.py: {FILENAME} {digest}" in out and "1 files in" in out


def test_main_url_needs_https_a_full_hash_and_a_new_file(tmp_path: Path, capsys) -> None:
    assert fetch_verified.main(["url", "--sha256", OTHER_HASH, "http://files.invalid/a.tar.bz2",
                                str(tmp_path / "a")]) == 1
    assert "only https: URLs are used" in capsys.readouterr().err
    with pytest.raises(SystemExit) as stopped:
        fetch_verified.main(["url", "--sha256", "abc", "https://files.invalid/a.tar.bz2", str(tmp_path / "a")])
    assert stopped.value.code == 2
    (tmp_path / "a").write_bytes(b"kept")
    assert fetch_verified.main(["url", "--sha256", OTHER_HASH, "https://files.invalid/a.tar.bz2",
                                str(tmp_path / "a")]) == 1
    assert "exists already" in capsys.readouterr().err and (tmp_path / "a").read_bytes() == b"kept"


def test_the_script_needs_nothing_but_the_standard_library() -> None:
    proc = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), "--help"], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Download files whose SHA-256 the repository fixes" in proc.stdout
    assert fetch_verified.shown("https://reader:secret@example.org/simple/") == "https://example.org/simple/"
    assert fetch_verified.DEFAULT_INDEX == "https://pypi.org/simple/"


def test_the_repository_locks_fetch_as_one_wheelhouse() -> None:
    from locked_files import merge_locks, read_lock
    db = SCRIPT.parent
    pins = merge_locks({path: read_lock(path) for path in (
        db / "requirements-pip.txt", db / "requirements-build.txt", db / "requirements-dev.txt")})
    assert pins["psycopg-c"] == Pin("psycopg-c", "3.3.6", frozenset(
        {"29c568426ad61c1b702c7d73505c43ca2c9fc5d97a8431d8d9049e731e319ff8"}))
    assert "psycopg-binary" not in pins
