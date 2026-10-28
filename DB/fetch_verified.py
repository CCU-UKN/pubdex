#!/usr/bin/env python3
"""Download files whose SHA-256 the repository fixes, and nothing else.

    python3 DB/fetch_verified.py wheels --dest <new directory> [--allow-source <name>]... <lock>...
    python3 DB/fetch_verified.py url --sha256 <hash> [--max-bytes <n>] <https URL> <file>

The downloads of PubDex's installations and image builds, kept apart from
the installations themselves: pip never downloads anything. `wheels` fills a
new, empty directory -- the wheelhouse -- with exactly one file for every pin
of the given hashed locks (DB/locked_files.py reads them): the wheel that
suits the Python running this script -- a pure-Python wheel, or one built
for this CPython on a glibc-based Linux no newer than this one -- and, only
for a name given with --allow-source, the source archive instead. A pin
with no such file, or with more than one, is an error, and so is a yanked
file. DB/install_pip.py and DB/install_locked.py then install from that
directory with pip's --no-index. `url` downloads one file, such as the
PostgreSQL source the PostgreSQL image is built from.

How it downloads, with Python's own HTTP client, never pip's:

- only https: URLs, verified with the system's certificate store by
  Python's ssl module, through a proxy the environment names; a redirect
  is followed only to another https: URL, and a page fetched over https:
  may link only to https: URLs -- any other destination is refused before
  anything is sent to it. A file: URL serves as a local copy of an index;
- every network operation times out after --timeout seconds, and each file
  has to arrive within --deadline seconds, counted from before the host
  name's lookup to the last byte: the lookup, the connection, TLS, the
  request, every redirect, the response's headers and its body. Past the
  deadline every connection of the download is shut down, so that a peer
  sending a byte now and then, each within the timeout, cannot keep a read
  waiting; a lookup that does not return in time is abandoned to its own
  thread, which ends when the system's resolver returns;
- a file larger than --max-bytes is refused as soon as it passes the limit,
  and so is an index page larger than 16 MiB and a total beyond
  --max-total-bytes;
- each file is written under a temporary name and gets its own name only
  after its SHA-256 has matched one its lock lists; nothing downloaded is run
  or unpacked.

The index is https://pypi.org/simple/ (PEP 503), or the one --index-url or
PIP_INDEX_URL names. Exit status: 0 when every file is in place, 1 when any
step fails -- the directory then holds no file under its final name that
failed a check -- 2 for a usage error.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import html.parser
import http.client
import os
import select
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from locked_files import (LockError, Pin, Target, canonical, merge_locks,  # noqa: E402
                          parse_filename, read_lock)

DEFAULT_INDEX = "https://pypi.org/simple/"
MAX_PAGE_BYTES = 16 * 1024 * 1024
MAX_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
TIMEOUT_SECONDS = 60
DEADLINE_SECONDS = 600


class FetchError(Exception):
    """A file cannot be fetched as the repository fixes it."""


def shown(url: str) -> str:
    """url without any user name or password it carries, for messages."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]))


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an https: URL. urllib asks before it follows
    one, with the URL it would request next, so any other destination is
    refused before anything is sent there. (A redirect to a scheme other than
    http:, https: or ftp: urllib refuses itself, before asking.)"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            fp.close()
            raise FetchError(f"refusing {shown(req.full_url)}: it redirects to {shown(newurl)}, "
                             "and only https: URLs are used after an https: one")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Deadline:
    """The time one download has, over every step of it. Each socket the
    download opens is watched from the moment it exists, through a duplicate
    of its descriptor -- the TLS layer takes the original over -- and when
    the deadline passes a timer thread shuts every one of them down: whatever
    waits on one, a TLS handshake, a response header or a buffered read of
    a body that trickles in, returns at once and fails. Used as a context
    manager, which starts the timer and, on leaving, stops and joins it and
    closes the duplicates."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.end = time.monotonic() + seconds
        self.expired = threading.Event()
        self._lock = threading.Lock()
        self._watched: List[socket.socket] = []
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.name = "fetch_verified deadline"
        self._timer.daemon = True

    def __enter__(self) -> "Deadline":
        self._timer.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._timer.cancel()
        self._timer.join()
        with self._lock:
            for sock in self._watched:
                sock.close()
            self._watched.clear()

    def passed(self) -> bool:
        return self.expired.is_set() or time.monotonic() >= self.end

    def left(self) -> float:
        """The seconds left; a TimeoutError once there are none."""
        left = self.end - time.monotonic()
        if left <= 0 or self.expired.is_set():
            raise TimeoutError(f"the deadline of {self.seconds:g} seconds has passed")
        return left

    def watch(self, sock: socket.socket) -> None:
        with self._lock:
            if self.expired.is_set():
                raise TimeoutError(f"the deadline of {self.seconds:g} seconds has passed")
            self._watched.append(sock.dup())

    def _expire(self) -> None:
        with self._lock:
            self.expired.set()
            for sock in self._watched:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:  # never connected, or closed by its peer
                    pass


def resolve(host: str, port: int, deadline: Optional[Deadline] = None) -> list:
    """getaddrinfo's addresses for a TCP connection to host and port. With a
    deadline, the lookup runs in a thread of its own, since nothing can
    interrupt the system's resolver, and is abandoned when the deadline
    passes; the thread then ends whenever the resolver returns, and what it
    found is dropped."""
    if deadline is None:
        return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    found: Dict[str, object] = {}
    done = threading.Event()

    def lookup() -> None:
        try:
            found["infos"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except BaseException as exc:  # handed to the caller
            found["error"] = exc
        finally:
            done.set()

    threading.Thread(target=lookup, name=f"fetch_verified lookup of {host}", daemon=True).start()
    if not done.wait(deadline.left()):
        raise TimeoutError(f"looking up {host} did not end within the deadline of {deadline.seconds:g} seconds")
    if "error" in found:
        raise found["error"]  # type: ignore[misc]
    return found["infos"]  # type: ignore[return-value]


CONNECT_DELAY = 0.25


def connect(address: Tuple[str, int], timeout=TIMEOUT_SECONDS, source_address=None,
            deadline: Optional[Deadline] = None) -> socket.socket:
    """A TCP connection to address, tried on its IPv6 and IPv4 addresses in
    turn, a new attempt started every CONNECT_DELAY seconds while the earlier
    ones are still pending (RFC 8305's staggered connections): one address
    that does not answer -- an IPv6 route that drops packets -- costs a
    quarter of a second, not the whole timeout, and the first attempt to
    succeed is used. The connection gets the timeout for every later
    operation; all attempts together get it as their deadline, or the
    download's deadline if that comes first, which also watches every
    attempt."""
    host, port = address
    if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = TIMEOUT_SECONDS
    infos = resolve(host, port, deadline)
    queues: Dict[int, list] = {}
    for info in infos:
        queues.setdefault(info[0], []).append(info)
    order = []
    while any(queues.values()):
        for queue in queues.values():
            if queue:
                order.append(queue.pop(0))
    end = time.monotonic() + timeout
    if deadline is not None:
        end = min(end, deadline.end)
    pending: List[socket.socket] = []
    error: Optional[OSError] = None
    try:
        while order or pending:
            if order:
                family, kind, proto, _, sockaddr = order.pop(0)
                sock = socket.socket(family, kind, proto)
                try:
                    if deadline is not None:
                        deadline.watch(sock)
                    sock.setblocking(False)
                    if source_address:
                        sock.bind(source_address)
                    status = sock.connect_ex(sockaddr)
                except OSError as exc:
                    sock.close()
                    error = exc
                    continue
                if status == 0:
                    sock.settimeout(timeout)
                    return sock
                if status not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN):
                    sock.close()
                    error = OSError(status, os.strerror(status))
                    continue
                pending.append(sock)
            left = end - time.monotonic()
            if left <= 0:
                break
            _, ready, _ = select.select([], pending, [], min(CONNECT_DELAY, left) if order else left)
            for sock in ready:
                pending.remove(sock)
                status = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if status == 0:
                    sock.settimeout(timeout)
                    return sock
                sock.close()
                error = OSError(status, os.strerror(status))
        raise error or TimeoutError(f"connecting to {host} port {port} timed out")
    finally:
        for sock in pending:
            sock.close()


class _Connection(http.client.HTTPSConnection):
    def __init__(self, *args, deadline: Optional[Deadline] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = partial(connect, deadline=deadline)


class _HttpsHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, deadline: Optional[Deadline]) -> None:
        super().__init__(context=context)
        self._deadline = deadline

    def https_open(self, req):
        return self.do_open(partial(_Connection, deadline=self._deadline), req, context=self._context)


def _opener(scheme: str, deadline: Optional[Deadline] = None) -> urllib.request.OpenerDirector:
    """An opener for one scheme and nothing else: https:, verified with the
    system's certificate store, through a proxy that the environment names,
    with redirects to https: only and every connection under the deadline; or
    file:."""
    if scheme == "https":
        handlers = [urllib.request.ProxyHandler(),
                    _HttpsHandler(ssl.create_default_context(), deadline),
                    _HttpsRedirects(), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor()]
    else:
        handlers = [urllib.request.FileHandler()]
    opener = urllib.request.OpenerDirector()
    for handler in [*handlers, urllib.request.UnknownHandler()]:
        opener.add_handler(handler)
    return opener


class Fetcher:
    """Reads https: and file: URLs within the limits."""

    def __init__(self, timeout: float = TIMEOUT_SECONDS, deadline: float = DEADLINE_SECONDS,
                 max_bytes: int = MAX_BYTES, max_total_bytes: int = MAX_TOTAL_BYTES) -> None:
        self.timeout, self.deadline = timeout, deadline
        self.max_bytes, self.max_total_bytes = max_bytes, max_total_bytes
        self.total = 0

    def open(self, url: str, deadline: Optional[Deadline] = None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("https", "file"):
            raise FetchError(f"refusing {shown(url)}: only https: and file: URLs are used")
        if parts.scheme == "file" and os.path.isdir(urllib.request.url2pathname(parts.path)):
            url = urllib.parse.urljoin(url.rstrip("/") + "/", "index.html")
        request = urllib.request.Request(url, headers={"Accept": "text/html",
                                                       "User-Agent": "pubdex-fetch-verified"})
        return _opener(parts.scheme, deadline).open(request, timeout=self.timeout)

    def read(self, url: str, sink, limit: int) -> Tuple[str, int, str]:
        """Copies url's body to sink, within limit, the deadline and the total;
        its SHA-256, its size and the URL it finally came from. Whatever fails
        once the deadline has passed -- a read that the deadline's shutdown
        ended early among them -- is reported as the deadline."""
        digest, size = hashlib.sha256(), 0
        late = FetchError(f"{shown(url)} did not arrive within {self.deadline:g} seconds")
        with Deadline(self.deadline) as deadline:
            try:
                with self.open(url, deadline) as response:
                    final = response.geturl()
                    while True:
                        chunk = response.read(1 << 16)
                        if not chunk or deadline.passed():
                            break
                        size += len(chunk)
                        self.total += len(chunk)
                        if size > limit:
                            raise FetchError(f"{shown(url)} is larger than {limit} bytes")
                        if self.total > self.max_total_bytes:
                            raise FetchError(f"the downloads exceed {self.max_total_bytes} bytes in total")
                        digest.update(chunk)
                        sink(chunk)
            except Exception:
                if deadline.passed():
                    raise late from None
                raise
            if deadline.passed():
                raise late
        return digest.hexdigest(), size, final

    def save(self, url: str, target: Path, hashes: Set[str]) -> Tuple[str, int]:
        """Saves url as target, if its SHA-256 is one of hashes; else nothing
        is left under target's name."""
        handle, name = tempfile.mkstemp(prefix=".partial-", dir=target.parent)
        partial = Path(name)
        try:
            with os.fdopen(handle, "wb") as out:
                found, size, _ = self.read(url, out.write, self.max_bytes)
            if found not in hashes:
                raise FetchError(f"{target.name} from {shown(url)} has the SHA-256 {found}, which is not "
                                 "one the repository fixes; it was not kept")
            os.chmod(partial, 0o644)
            os.replace(partial, target)
        finally:
            if partial.exists():
                partial.unlink()
        return found, size


class _Links(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Dict[str, Optional[str]]] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "a" and dict(attrs).get("href"):
            self.links.append(dict(attrs))


def index_files(fetcher: Fetcher, index_url: str, name: str) -> List[Tuple[str, str, Optional[str], bool]]:
    """(file name, URL, sha256 from the link or None, yanked) of every file the
    index's simple page for name links to."""
    page = urllib.parse.urljoin(index_url.rstrip("/") + "/", name + "/")
    chunks: List[bytes] = []
    _, _, base = fetcher.read(page, chunks.append, MAX_PAGE_BYTES)
    parser = _Links()
    parser.feed(b"".join(chunks).decode("utf-8", "replace"))
    files = []
    for attrs in parser.links:
        url, fragment = urllib.parse.urldefrag(urllib.parse.urljoin(base, attrs["href"]))
        if urllib.parse.urlsplit(base).scheme == "https" and urllib.parse.urlsplit(url).scheme != "https":
            raise FetchError(f"refusing {shown(url)}: the https: page {shown(base)} links to it, "
                             "and only https: URLs are used after an https: one")
        filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
        sha256 = fragment[len("sha256="):] if fragment.startswith("sha256=") else None
        files.append((filename, url, sha256, "data-yanked" in attrs))
    return files


def choose(pin: Pin, files, target: Target, source: bool, index_url: str) -> Tuple[str, str]:
    """The one file of pin to fetch: (file name, URL)."""
    candidates, yanked = [], []
    for filename, url, sha256, is_yanked in files:
        parsed = parse_filename(filename)
        if parsed is None or parsed.name != pin.name or parsed.version != pin.version:
            continue
        if parsed.kind != ("sdist" if source else "wheel"):
            continue
        if parsed.kind == "wheel" and not target.accepts(parsed.tags):
            continue
        if sha256 is not None and sha256 not in pin.hashes:
            continue
        (yanked if is_yanked else candidates).append((filename, url))
    if len(candidates) == 1:
        return candidates[0]
    what = "source archive" if source else f"wheel for {target.describe()}"
    if not candidates and yanked:
        raise FetchError(f"{pin.name}=={pin.version}: the index marks its {what} yanked: {yanked[0][0]}")
    if not candidates:
        raise FetchError(f"{pin.name}=={pin.version}: {shown(index_url)} lists no {what} whose hash the "
                         "lock lists")
    raise FetchError(f"{pin.name}=={pin.version}: more than one {what} would do "
                     f"({', '.join(sorted(f for f, _ in candidates))}); the lock has to fix exactly one")


def fetch_wheels(locks: Sequence[Path], dest: Path, index_url: str, sources: Sequence[str],
                 target: Target, fetcher: Fetcher) -> Dict[str, Tuple[str, str, int]]:
    pins = merge_locks({path: read_lock(path) for path in locks})
    sources = {canonical(name) for name in sources}
    unknown = sources - set(pins)
    if unknown:
        raise FetchError(f"--allow-source names {', '.join(sorted(unknown))}, which no lock pins")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise FetchError(f"{dest} has to be a new or empty directory")
    dest.mkdir(parents=True, exist_ok=True)
    fetched: Dict[str, Tuple[str, str, int]] = {}
    for name, pin in sorted(pins.items()):
        filename, url = choose(pin, index_files(fetcher, index_url, name), target, name in sources, index_url)
        found, size = fetcher.save(url, dest / filename, set(pin.hashes))
        if filename.endswith(".whl") and not _is_wheel_of(dest / filename, pin):
            (dest / filename).unlink()
            raise FetchError(f"{filename} matches the lock but is not a wheel of {name}=={pin.version}")
        fetched[name] = (filename, found, size)
        print(f"fetch_verified.py: {filename} {found} ({size} bytes) from {shown(url)}", flush=True)
    return fetched


def _is_wheel_of(path: Path, pin: Pin) -> bool:
    if not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as wheel:
        return any(entry.count("/") == 1 and entry.endswith(".dist-info/METADATA")
                   and canonical(entry[:-len(".dist-info/METADATA")].rsplit("-", 1)[0]) == pin.name
                   for entry in wheel.namelist())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("wheels", "url"):
        sub = commands.add_parser(command)
        sub.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS,
                         help="seconds each network operation may take (%(default)s)")
        sub.add_argument("--deadline", type=float, default=DEADLINE_SECONDS,
                         help="seconds each file may take to arrive (%(default)s)")
        sub.add_argument("--max-bytes", type=int, default=MAX_BYTES, help="largest file accepted (%(default)s)")
    wheels = commands.choices["wheels"]
    wheels.add_argument("--dest", type=Path, required=True, help="the wheelhouse: a new or empty directory")
    wheels.add_argument("--index-url", default=os.environ.get("PIP_INDEX_URL") or DEFAULT_INDEX,
                        help="the package index (default: PIP_INDEX_URL, else %(default)s)")
    wheels.add_argument("--allow-source", action="append", default=[], metavar="NAME",
                        help="fetch the source archive of this pinned name instead of a wheel")
    wheels.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES,
                        help="largest total accepted (%(default)s)")
    wheels.add_argument("locks", nargs="+", type=Path)
    url = commands.choices["url"]
    url.add_argument("--sha256", required=True, help="the file's SHA-256, as the repository fixes it")
    url.add_argument("url")
    url.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "wheels":
            fetcher = Fetcher(args.timeout, args.deadline, args.max_bytes, args.max_total_bytes)
            fetched = fetch_wheels(args.locks, args.dest, args.index_url, args.allow_source,
                                   Target.current(), fetcher)
            print(f"fetch_verified.py: {len(fetched)} files in {args.dest}, each matching a hash its lock "
                  f"lists, for {Target.current().describe()}")
        else:
            if not urllib.parse.urlsplit(args.url).scheme == "https":
                raise FetchError(f"refusing {shown(args.url)}: only https: URLs are used")
            if len(args.sha256) != 64 or not all(c in "0123456789abcdef" for c in args.sha256):
                parser.error("--sha256 needs 64 lower-case hexadecimal digits")
            if args.file.exists():
                raise FetchError(f"{args.file} exists already")
            fetcher = Fetcher(args.timeout, args.deadline, args.max_bytes, args.max_bytes)
            found, size = fetcher.save(args.url, args.file, {args.sha256})
            print(f"fetch_verified.py: {args.file.name} {found} ({size} bytes) from {shown(args.url)}")
    except (FetchError, LockError) as exc:
        print(f"fetch_verified.py: {exc}", file=sys.stderr)
        return 1
    except (OSError, urllib.error.URLError, ssl.SSLError, ValueError) as exc:
        print(f"fetch_verified.py: could not fetch: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
