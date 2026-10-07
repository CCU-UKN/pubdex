"""Network access for the test suite: none offline, one database in integration.

The offline tests need no network at all, and the integration tests may reach
exactly one place: the disposable PostgreSQL database that
PEOPLE_PUBS_INTEGRATION_DSN or PEOPLE_DB_TEST_DSN names. tests/conftest.py
installs this guard in the pytest process before any test module is imported,
and tests/subprocess_guard/sitecustomize.py installs the same guard in every
Python process the tests start with the test process's environment.

What it refuses, by raising NetworkAccessRefused before anything is sent:

- a connection, or a datagram, to any IPv4 or IPv6 address except the
  disposable database's endpoint. A wrapper around socket.socket.connect and
  connect_ex checks the address before a host name in it is resolved, and an
  audit hook checks every socket connect and send again, so code that calls
  the lower-level _socket methods directly is refused as well;
- a host-name lookup (getaddrinfo, gethostbyname) of any name except
  localhost, this machine's own name and the database host, so not even a DNS
  query for a metadata provider leaves the machine;
- a connection through psycopg's public entry points -- psycopg.connect,
  Connection.connect and AsyncConnection.connect -- to any server except the
  disposable database. libpq opens its connections in C, out of sight of the
  socket module, so this check wraps those methods instead, checking the
  server libpq would reach before it is contacted. The wrapper is applied
  when psycopg is imported, so a process that never imports psycopg, or runs
  on an interpreter that does not have it, does not load it for the guard.

Unix-domain sockets are local and stay allowed. Every refusal is also
appended to the file that PEOPLE_PUBS_TEST_NETWORK_LOG names, so that the test
which caused it fails even when the code under test caught the exception, in a
child process as well (see tests/conftest.py). The guard fails closed: if it
cannot be set up, or cannot record a refusal, it reports that, records it
where it can, and stops the process.

Not covered -- this is a guard inside Python processes, not operating-system
network isolation: processes started with a cleared environment (the
repository's Docker-backed scripts clear theirs on purpose), with python -I,
-E or -S, or programs that are not Python, such as psql or curl; libpq reached
other than through those psycopg entry points (psycopg.pq or ctypes);
reverse lookups (gethostbyaddr, getnameinfo); and what Docker itself fetches,
such as the PostgreSQL image.

Child processes load this module at start-up, so it imports only what it
needs there.
"""
from __future__ import annotations

import _socket
import os
import socket
import sys

POLICY_VARIABLE = "PEOPLE_PUBS_TEST_NETWORK_GUARD"
LOG_VARIABLE = "PEOPLE_PUBS_TEST_NETWORK_LOG"
DATABASE_VARIABLES = ("PEOPLE_PUBS_INTEGRATION_DSN", "PEOPLE_DB_TEST_DSN")
# Exit status of a process whose guard could not be set up or could not record
# a refusal.
GUARD_FAILURE_STATUS = 70

# An endpoint is (host, port) as libpq uses them: host is an address, a name,
# a socket directory, or "" for libpq's default local socket.


class NetworkAccessRefused(RuntimeError):
    """Network access the test suite does not allow.

    Deliberately no OSError: a client that retries or wraps connection
    errors must not treat a refusal as a passing network failure.
    """


def _host(host: object) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host).strip().strip("[]").lower()


def _is_address(host: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host.split("%", 1)[0])
        except (OSError, ValueError):
            continue
        return True
    return False


def _show(host: str, port: object) -> str:
    if not host:
        return f"the default local socket, port {port}"
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def database_endpoints(params: dict, environ: dict | None = None) -> list[tuple[str, str]]:
    """The servers a libpq connection with these parameters reaches.

    Each parameter comes from the connection string, else from its PG*
    environment variable, else from libpq's default; hostaddr, when set, is
    where libpq connects, whatever host says.
    """
    environ = os.environ if environ is None else environ
    hostaddr = str(params.get("hostaddr") or environ.get("PGHOSTADDR") or "")
    host = str(params.get("host") or environ.get("PGHOST") or "")
    hosts = (hostaddr or host).split(",")
    ports = str(params.get("port") or environ.get("PGPORT") or "5432").split(",")
    if len(ports) == 1:
        ports = ports * len(hosts)
    return [(_host(h), p.strip() or "5432") for h, p in zip(hosts, ports)]


class Policy:
    """What the guard lets through: the database endpoints, nothing else."""

    def __init__(self, endpoints=()) -> None:
        self.endpoints = {(_host(h), str(p)) for h, p in endpoints}
        self.names = {"localhost"}
        try:
            self.names.add(socket.gethostname().lower())
        except OSError:
            pass
        self.addresses = set()
        for host, port in self.endpoints:
            if not host or host.startswith("/") or not port.isdigit():
                continue  # a local socket: no network address to allow
            if _is_address(host):
                self.addresses.add((host, int(port)))
                continue
            self.names.add(host)
            try:
                found = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
            except OSError:
                found = []
            self.addresses.update((_host(item[4][0]), int(port)) for item in found)

    def allows_address(self, host: object, port: object) -> bool:
        try:
            return (_host(host), int(port)) in self.addresses  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def allows_lookup(self, host: object) -> bool:
        if host is None:
            return True
        name = _host(host).rstrip(".")
        return not name or _is_address(name) or name in self.names

    def allows_database(self, endpoint: tuple[str, str]) -> bool:
        return endpoint in self.endpoints

    def describe(self) -> str:
        if not self.endpoints:
            return "offline tests may not use the network"
        shown = ", ".join(_show(host, port) for host, port in sorted(self.endpoints))
        return f"integration tests may reach only the disposable database ({shown})"

    def to_environment(self) -> str:
        """One "host<TAB>port" line per endpoint; empty for the offline policy."""
        return "\n".join(f"{host}\t{port}" for host, port in sorted(self.endpoints))


_policy: Policy | None = None
_hook_added = False
# The C methods themselves, so that a second copy of this module (a test run
# started by a test) never wraps the first one's wrapper.
_original_connect = _socket.socket.connect
_original_connect_ex = _socket.socket.connect_ex
# Refusals accounted for already: (start, end) offsets into the refusal log.
_acknowledged: list = []


def policy() -> Policy | None:
    return _policy


def _record(entry: str) -> bool:
    """Appends an entry to the refusal log; False if there is no log."""
    path = os.environ.get(LOG_VARIABLE)
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as log:
        log.write(f"{entry} (process {os.getpid()})\n")
    return True


def stop_process(message: str) -> None:
    """Fail closed: report, record where possible, and end the process."""
    try:
        _record(message)
    except OSError:
        pass
    try:
        sys.stderr.write(f"{message}\n")
        sys.stderr.flush()
    finally:
        os._exit(GUARD_FAILURE_STATUS)


def _refuse(what: str) -> None:
    assert _policy is not None
    try:
        recorded = _record(what)
    except OSError as exc:
        recorded, problem = False, f"{type(exc).__name__}: {exc}"
    else:
        problem = f"{LOG_VARIABLE} is not set"
    if not recorded:
        stop_process(f"the test network guard refused {what} but could not record it ({problem})")
    raise NetworkAccessRefused(f"the test network guard refused {what}: {_policy.describe()}")


def _check_socket(sock: object, address: object, action: str) -> None:
    if _policy is None or address is None:
        return
    if getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
        return
    if not isinstance(address, tuple) or len(address) < 2:
        return
    host, port = address[0], address[1]
    if not _policy.allows_address(host, port):
        _refuse(f"{action} to {_show(_host(host), port)}")


def _audit(event: str, args: tuple) -> None:
    if not event.startswith("socket.") or _policy is None:
        return
    if event == "socket.connect":
        _check_socket(args[0], args[1], "a connection")
    elif event in ("socket.sendto", "socket.sendmsg"):
        _check_socket(args[0], args[1], "a datagram")
    elif event in ("socket.getaddrinfo", "socket.gethostbyname"):
        if not _policy.allows_lookup(args[0]):
            _refuse(f"a host-name lookup of {_host(args[0])}")


def _guarded_connect(self: socket.socket, address: object) -> None:
    _check_socket(self, address, "a connection")
    return _original_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    _check_socket(self, address, "a connection")
    return _original_connect_ex(self, address)


def check_database(conninfo: str = "", **kwargs: object) -> None:
    """Refuses a psycopg connection to anything but an allowed database."""
    if _policy is None:
        return
    from psycopg.conninfo import conninfo_to_dict

    target = {key: value for key, value in kwargs.items()
              if key in ("host", "hostaddr", "port", "service") and value is not None}
    params = conninfo_to_dict(conninfo, **target)
    if params.get("service") or os.environ.get("PGSERVICE"):
        _refuse("a database connection through a service definition, which can name any server")
    for endpoint in database_endpoints(params):
        if not _policy.allows_database(endpoint):
            _refuse(f"a database connection to {_show(*endpoint)}")


def _guarded_here(func) -> bool:
    """Whether this copy of the module already wraps func."""
    while func is not None:
        if getattr(func, "network_guard", None) == __name__:
            return True
        func = getattr(func, "__wrapped__", None)
    return False


def _guarded_psycopg_connect(func):
    import functools
    import inspect

    if inspect.iscoroutinefunction(func):
        async def connect(cls, conninfo: str = "", **kwargs):
            check_database(conninfo, **kwargs)
            return await func(cls, conninfo, **kwargs)
    else:
        def connect(cls, conninfo: str = "", **kwargs):
            check_database(conninfo, **kwargs)
            return func(cls, conninfo, **kwargs)
    functools.update_wrapper(connect, func)
    connect.network_guard = __name__  # type: ignore[attr-defined]
    return classmethod(connect)


def _guard_psycopg(psycopg) -> None:
    """Wraps psycopg's public connect entry points. Raises when one is not
    where it should be, rather than leave psycopg unguarded."""
    for name in ("Connection", "AsyncConnection"):
        cls = getattr(psycopg, name, None)
        func = getattr(getattr(cls, "__dict__", {}).get("connect"), "__func__", None)
        if func is None:
            raise RuntimeError(f"the test network guard cannot find psycopg.{name}.connect to guard")
        if not _guarded_here(func):
            cls.connect = _guarded_psycopg_connect(func)
    psycopg.connect = psycopg.Connection.connect


class _GuardPsycopgOnImport:
    """Meta-path finder that hands psycopg to its guards as soon as the
    package has run; every other import passes through untouched.

    A process holds one such finder, whichever copy of this module created
    it: a later copy (a test run started by a test) adds its guard to the
    finder already there, and finders skip each other, so the guards of
    every copy apply in turn and no finder ever consults another one.
    """

    network_guard_finder = True

    def __init__(self) -> None:
        self.guards: list = []

    def find_spec(self, name, path=None, target=None):
        if name != "psycopg":
            return None
        for finder in sys.meta_path:
            if getattr(finder, "network_guard_finder", False):
                continue
            find_spec = getattr(finder, "find_spec", None)
            spec = find_spec(name, path, target) if find_spec is not None else None
            if spec is not None:
                break
        else:
            return None
        loader = spec.loader
        if loader is None or not hasattr(loader, "exec_module"):
            raise ImportError("the test network guard cannot guard psycopg loaded this way", name=name)
        execute, guards = loader.exec_module, list(self.guards)

        def exec_module(module):
            execute(module)
            for guard in guards:
                try:
                    guard(module)
                except Exception as exc:
                    # The import fails, so psycopg is never left unguarded;
                    # the record makes the enclosing test fail even if the
                    # failed import is caught.
                    try:
                        _record(f"the test network guard could not guard psycopg: {exc}")
                    except OSError:
                        pass
                    raise

        loader.exec_module = exec_module
        return spec


def _guard_psycopg_when_imported() -> None:
    psycopg = sys.modules.get("psycopg")
    if psycopg is not None:
        _guard_psycopg(psycopg)
        return
    finder = next((f for f in sys.meta_path if getattr(f, "network_guard_finder", False)), None)
    if finder is None:
        finder = _GuardPsycopgOnImport()
        sys.meta_path.insert(0, finder)
    if _guard_psycopg not in finder.guards:
        finder.guards.append(_guard_psycopg)


def _new_policy(endpoints) -> Policy:
    """Builds a policy with the guard paused: resolving an allowed database
    host name is not a refusal of the policy being replaced."""
    global _policy
    previous, _policy = _policy, None
    try:
        return Policy(endpoints)
    finally:
        _policy = previous


def install(endpoints=()) -> Policy:
    """Applies the policy to this process: sockets now, psycopg as soon as it
    is imported."""
    global _policy, _hook_added
    new_policy = _new_policy(endpoints)
    _policy = new_policy
    if not _hook_added:
        sys.addaudithook(_audit)
        _hook_added = True
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    _guard_psycopg_when_imported()
    return new_policy


def endpoints_from_environment(environ: dict | None = None) -> list[tuple[str, str]]:
    """The database endpoints the integration DSNs name (needs psycopg)."""
    environ = os.environ if environ is None else environ
    endpoints: list[tuple[str, str]] = []
    for variable in DATABASE_VARIABLES:
        dsn = environ.get(variable)
        if dsn:
            from psycopg.conninfo import conninfo_to_dict

            endpoints.extend(database_endpoints(conninfo_to_dict(dsn), environ))
    return endpoints


def install_from_environment() -> None:
    """The guard for a child process, as the test process described it.
    Raises on a description it cannot read, or without a refusal log."""
    described = os.environ.get(POLICY_VARIABLE)
    if described is None:
        return
    if not os.environ.get(LOG_VARIABLE):
        raise ValueError(f"{POLICY_VARIABLE} is set but {LOG_VARIABLE} is not")
    endpoints = []
    for line in described.splitlines():
        host, tab, port = line.partition("\t")
        if not tab or not port.strip():
            raise ValueError(f"unreadable {POLICY_VARIABLE} entry {line!r}")
        endpoints.append((host, port))
    install(endpoints)


class using:
    """For tests of the guard itself: another policy for the duration of a
    with block, the installed one again afterwards."""

    def __init__(self, endpoints) -> None:
        self.endpoints = endpoints

    def __enter__(self) -> Policy:
        global _policy
        self.previous = _policy
        _policy = _new_policy(self.endpoints)
        return _policy

    def __exit__(self, *exc_info) -> None:
        global _policy
        _policy = self.previous


# --------------------------------------------------------------------------
# The refusal log, read by tests/conftest.py after every test
# --------------------------------------------------------------------------
def refusal_mark() -> int:
    path = os.environ.get(LOG_VARIABLE)
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def _refusals_between(start: int, end: int) -> list:
    path = os.environ.get(LOG_VARIABLE)
    if not path or end <= start:
        return []
    with open(path, "rb") as log:
        log.seek(start)
        data = log.read(end - start)
    found = []
    offset = start
    for line in data.splitlines(keepends=True):
        found.append((offset, line.decode("utf-8", "replace").rstrip("\n")))
        offset += len(line)
    return found


def unexpected_refusals(start: int = 0) -> list[str]:
    """Refusals logged since start that nothing has accounted for yet -- no
    expect_refusals() block and no earlier call of this function."""
    end = refusal_mark()
    found = [
        what for offset, what in _refusals_between(start, end)
        if not any(low <= offset < high for low, high in _acknowledged)
    ]
    _acknowledged.append((start, end))
    return found


class expect_refusals:
    """For tests of the guard itself: collects in a list the refusals logged
    inside a with block, which then do not fail the test."""

    def __enter__(self) -> list[str]:
        self.start = refusal_mark()
        self.seen: list[str] = []
        return self.seen

    def __exit__(self, *exc_info) -> None:
        end = refusal_mark()
        self.seen.extend(what for _offset, what in _refusals_between(self.start, end))
        _acknowledged.append((self.start, end))
