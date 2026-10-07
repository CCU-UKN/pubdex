"""Lifecycle of DB/run_disposable_integration.sh, offline.

The real script runs from a scratch copy of the checkout layout against a stub
`docker` executable, which records every call and keeps an inventory of the
containers and volumes it was asked to create, and a stub
run_integration_tests.sh, which records its arguments and environment, prints
its connection string and writes the JUnit report pytest would. That proves,
without Docker and without touching any existing service, that the runner
creates uniquely named resources carrying its own ownership label, never
pre-deletes, publishes only a loopback port, binds every connection setting to
the disposable database, keeps the password out of command lines and output,
preserves the tests' exit status, fails on a skipped or empty run, and removes
exactly what it created after success, failure, a timeout, a failed start, a
failed bootstrap, an exited container or a signal -- also when a second signal
arrives during the cleanup. A cleanup that fails -- a removal the daemon
refuses, one that reports success but leaves the resource, a daemon that no
longer answers, a temporary directory that cannot be removed -- is reported
with the commands that find the leftovers and fails an otherwise successful
run, while a test failure or a signal keeps its own exit status.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

DB_DIR = Path(__file__).resolve().parents[1]
SCRIPT = DB_DIR / "run_disposable_integration.sh"
LABEL_KEY = "pubdex.integration.run"
PORT = "54321"
RERUN_TESTS = [
    "tests/integration/test_publication_views_integration.py",
    "tests/integration/test_application_roles_integration.py",
]
BASH = shutil.which("bash") or "/bin/bash"
REAL_ENV = shutil.which("env") or "/usr/bin/env"
REAL_RM = shutil.which("rm") or "/bin/rm"
# What a shell adds on its own when it starts the test command.
SHELL_VARIABLES = {"PWD", "OLDPWD", "SHLVL", "_"}
BOUND_ENVIRONMENT = {
    "HOME", "PATH", "LANG", "TMPDIR", "PYTHONDONTWRITEBYTECODE",
    "PEOPLE_PUBS_INTEGRATION_DSN", "PEOPLE_DB_TEST_DSN", "PEOPLE_DB_DSN",
    "PGHOST", "PGHOSTADDR", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE",
    "PGSSLMODE", "PGSERVICEFILE", "PGSYSCONFDIR", "PGPASSFILE", "PEOPLE_PUBS_SKIP_DOTENV",
}

# Stub docker CLI. Calls go to the shared event log; the inventory is one file
# per resource holding its label; STUB_MODE is a comma-separated list.
STUB_DOCKER = r'''#!/usr/bin/env bash
set -u
state="$STUB_STATE"
mkdir -p "$state/containers" "$state/volumes"
{ printf 'docker'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$STUB_EVENTS"
has() { case ",$STUB_MODE," in *",$1,"*) return 0 ;; esac; return 1; }
option() {  # option <flag> <args...>: the value after the first <flag>
  local flag="$1" previous=""; shift
  for a in "$@"; do [ "$previous" = "$flag" ] && { printf '%s' "$a"; return; }; previous="$a"; done
}
labelled() {  # labelled <kind> <args...>: resources whose label matches --filter label=K=V
  local kind="$1" wanted f; shift
  has list-fails && { echo "stub: cannot connect to the Docker daemon" >&2; exit 1; }
  wanted="$(option --filter "$@")"; wanted="${wanted#label=}"
  for f in "$state/$kind"/*; do
    [ -e "$f" ] && [ "$(cat "$f")" = "$wanted" ] && basename "$f"
  done
  return 0
}
last="${!#}"
case "$1" in
  info) has no-daemon && exit 1; exit 0 ;;
  container)
    if [ -e "$state/containers/$last" ]; then
      if [ "$2 $3" = "inspect -f" ]; then has container-exits && echo false || echo true; fi
      exit 0
    fi
    has collision-container && exit 0; exit 1 ;;
  volume)
    case "$2" in
      inspect) { [ -e "$state/volumes/$last" ] || has collision-volume; } && exit 0; exit 1 ;;
      create) option --label "$@" > "$state/volumes/$last"; echo "$last"; exit 0 ;;
      ls) labelled volumes "$@"; exit 0 ;;
      rm)
        has volume-rm-fails && { echo "stub: volume is in use" >&2; exit 1; }
        rm -f "$state/volumes/$last"; exit 0 ;;
    esac ;;
  run)
    printf '%s' "${POSTGRES_PASSWORD:-}" > "$state/password"
    has run-fails && { echo "stub: cannot start the container" >&2; exit 125; }
    option --label "$@" > "$state/containers/$(option --name "$@")"
    echo 0123456789ab; exit 0 ;;
  ps)
    has list-fails && { echo "stub: cannot connect to the Docker daemon" >&2; exit 1; }
    for name in $(labelled containers "$@"); do
      case " $* " in *" --format "*) echo "container $name: Up 3 seconds" ;; *) echo "$name" ;; esac
    done
    exit 0 ;;
  logs)
    # init-unseen: an empty mount, as when the daemon cannot see the checkout
    for f in "$STUB_INIT_DIR"/*.sql; do
      has init-unseen && break
      echo "/usr/local/bin/docker-entrypoint.sh: running /docker-entrypoint-initdb.d/$(basename "$f")"
      if has init-error; then
        echo "psql:/docker-entrypoint-initdb.d/$(basename "$f"):3: ERROR:  syntax error"; exit 0
      fi
    done
    has leaky-logs && echo "server log: the superuser password is $(cat "$state/password")"
    echo "PostgreSQL init process complete; ready for start up."; exit 0 ;;
  exec)
    case " $* " in
      *" pg_isready "*) has never-ready && exit 1; exit 0 ;;
      *" psql "*)
        if has reapply-fails; then
          echo "psql: ERROR:  stub failure" >&2
          has leaky-logs && echo "psql: DETAIL:  password $(cat "$state/password")" >&2
          exit 3
        fi
        exit 0 ;;
    esac
    exit 0 ;;
  port) has public-port && echo "0.0.0.0:$STUB_PORT" || echo "127.0.0.1:$STUB_PORT"; exit 0 ;;
  rm)
    has rm-slow && sleep 1
    has rm-fails && { echo "stub: removal failed" >&2; exit 1; }
    has rm-ineffective && exit 0
    rm -f "$state/containers/$last"; exit 0 ;;
esac
echo "stub: unsupported $*" >&2
exit 2
'''

STUB_TESTS = r'''
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
config = json.loads((here / "stub_tests_config.json").read_text(encoding="utf-8"))
with open(config["events"], "a", encoding="utf-8") as fh:
    fh.write("\x1f".join(["tests", *sys.argv[1:]]) + "\n")
runs = here / "stub_tests_runs.jsonl"
call = len(runs.read_text(encoding="utf-8").splitlines()) if runs.exists() else 0
with runs.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\n")
print("connecting with " + os.environ.get("PEOPLE_PUBS_INTEGRATION_DSN", "(no DSN)"))
report = next(a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--junitxml="))
tests, failures, errors, skipped = config["totals"][call]
Path(report).write_text(
    '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
    f'errors="{errors}" failures="{failures}" skipped="{skipped}" tests="{tests}" />'
    "</testsuites>",
    encoding="utf-8",
)
sys.exit(config["statuses"][call])
'''


# Stands in for rm: refuses to remove anything below STUB_RM_FAIL_UNDER and
# behaves as rm otherwise, so the stub docker keeps its own inventory.
STUB_RM = f'''#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    "$STUB_RM_FAIL_UNDER"/*) echo "rm: cannot remove $arg: Operation not permitted" >&2; exit 1 ;;
  esac
done
exec "{REAL_RM}" "$@"
'''


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Fake:
    """A scratch checkout holding the real script, stubs and a fake init/."""

    def __init__(self, tmp_path: Path, mode: str = "ok", *, statuses=(0, 0),
                 totals=((92, 0, 0, 0), (21, 0, 0, 0)), wait_seconds: int = 5) -> None:
        self.tmp = tmp_path
        root = tmp_path / "checkout"
        self.db = root / "DB"
        (self.db / "init").mkdir(parents=True)
        for script in sorted((DB_DIR / "init").glob("*.sql")):
            (self.db / "init" / script.name).write_text("-- stub\n", encoding="utf-8")
        self.script = self.db / SCRIPT.name
        shutil.copy2(SCRIPT, self.script)
        (self.db / "lib").mkdir()
        shutil.copy2(DB_DIR / "lib" / "disposable_postgres.sh", self.db / "lib")
        self.events = tmp_path / "events.log"
        self.state = tmp_path / "state"
        (self.db / "stub_tests.py").write_text(STUB_TESTS, encoding="utf-8")
        config = {"statuses": list(statuses), "totals": [list(t) for t in totals], "events": str(self.events)}
        (self.db / "stub_tests_config.json").write_text(json.dumps(config), encoding="utf-8")
        _executable(self.db / "run_integration_tests.sh",
                    f'#!/usr/bin/env bash\ncd "$(dirname "$0")"\nexec "{sys.executable}" stub_tests.py "$@"\n')
        _executable(root / ".venv" / "bin" / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        self.bindir = tmp_path / "bin"
        _executable(self.bindir / "docker", STUB_DOCKER)
        # Records how `env` is called, then behaves as env: a launcher of the
        # form `env -i NAME=value ...` would show its values here.
        self.env_log = tmp_path / "env.log"
        _executable(self.bindir / "env",
                    f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.env_log}"\nexec "{REAL_ENV}" "$@"\n')
        # The runner's private directory is created here, so a leftover shows.
        self.tmpdir = tmp_path / "tmp"
        self.tmpdir.mkdir()
        self.env = dict(
            os.environ,
            PATH=f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            TMPDIR=str(self.tmpdir),
            STUB_MODE=mode,
            STUB_EVENTS=str(self.events),
            STUB_STATE=str(self.state),
            STUB_INIT_DIR=str(self.db / "init"),
            STUB_PORT=PORT,
            INTEGRATION_WAIT_SECONDS=str(wait_seconds),
        )

    def run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.script), *args], cwd=self.tmp, env=env or self.env,
                              capture_output=True, text=True, timeout=120)

    def popen(self, command: list | None = None) -> subprocess.Popen:
        # A session of its own, so a signal can go to the whole group as a
        # terminal would send it.
        return subprocess.Popen(command or [BASH, str(self.script)], cwd=self.tmp, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)

    def events_list(self) -> list:
        if not self.events.exists():
            return []
        return [line.split("\x1f") for line in self.events.read_text(encoding="utf-8").splitlines()]

    def argvs(self) -> list:
        return [event[1:] for event in self.events_list() if event[0] == "docker"]

    def test_runs(self) -> list:
        runs = self.db / "stub_tests_runs.jsonl"
        if not runs.exists():
            return []
        return [json.loads(line) for line in runs.read_text(encoding="utf-8").splitlines()]

    def password(self) -> str:
        return (self.state / "password").read_text(encoding="utf-8")

    def inventory(self) -> dict:
        return {kind: sorted(os.listdir(self.state / kind)) if (self.state / kind).exists() else []
                for kind in ("containers", "volumes")}

    def created(self) -> tuple:
        container = volume = None
        for argv in self.argvs():
            if argv[:2] == ["volume", "create"]:
                volume = argv[-1]
            if argv[0] == "run":
                container = argv[argv.index("--name") + 1]
        return container, volume

    def removals(self) -> tuple:
        return ([a[-1] for a in self.argvs() if a[0] == "rm"],
                [a[-1] for a in self.argvs() if a[:2] == ["volume", "rm"]])

    def wait_for(self, predicate, what: str, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        os.killpg(proc.pid, signal.SIGKILL)
        pytest.fail(f"the runner never reached {what}")

    def assert_nothing_left(self) -> None:
        container, volume = self.created()
        assert self.removals() == ([container] if container else [], [volume] if volume else [])
        assert self.inventory() == {"containers": [], "volumes": []}
        assert os.listdir(self.tmpdir) == [], "the private temporary directory is gone too"

    def run_id(self) -> str:
        container, volume = self.created()
        return (container or volume)[len("pubdex-integration-"):]

    def assert_cleanup_reported(self, proc: subprocess.CompletedProcess) -> None:
        run_id = self.run_id()
        assert f"could not confirm that run {run_id} left nothing behind" in proc.stderr
        assert f"docker ps -a --filter label={LABEL_KEY}={run_id}" in proc.stderr
        assert f"docker volume ls --filter label={LABEL_KEY}={run_id}" in proc.stderr
        shown = self.password() in proc.stdout + proc.stderr
        assert not shown, "the warning does not show the password"


def test_success_owns_unique_labelled_resources_and_removes_exactly_them(tmp_path: Path) -> None:
    names = []
    for label in ("one", "two"):
        fake = Fake(tmp_path / label)
        proc = fake.run()
        assert proc.returncode == 0, proc.stderr
        assert "PASS:" in proc.stdout
        container, volume = fake.created()
        assert container and container == volume and container.startswith("pubdex-integration-")
        run_id = container[len("pubdex-integration-"):]
        argvs = fake.argvs()
        create = next(a for a in argvs if a[:2] == ["volume", "create"])
        start = next(a for a in argvs if a[0] == "run")
        assert create[create.index("--label") + 1] == f"{LABEL_KEY}={run_id}"
        assert start[start.index("--label") + 1] == f"{LABEL_KEY}={run_id}"
        # Never a pre-delete: nothing is removed before the resources exist.
        first_create = argvs.index(create)
        assert not any(a[0] == "rm" or a[:2] == ["volume", "rm"] for a in argvs[:first_create])
        assert first_create < argvs.index(start), "the volume exists before the container uses it"
        fake.assert_nothing_left()
        assert "-v" in next(a for a in argvs if a[0] == "rm"), "anonymous volumes go with the container"
        names.append(container)
    assert names[0] != names[1], "two runs never share a resource name"


def test_loopback_port_read_only_init_and_password_kept_out_of_argv_and_output(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run()
    assert proc.returncode == 0, proc.stderr
    start = next(a for a in fake.argvs() if a[0] == "run")
    publish = [start[i + 1] for i, a in enumerate(start) if a in ("-p", "--publish")]
    assert publish == ["127.0.0.1::5432"], "only a loopback port that Docker picks"
    mounts = [start[i + 1] for i, a in enumerate(start) if a == "-v"]
    assert f"{fake.db / 'init'}:/docker-entrypoint-initdb.d:ro" in mounts
    assert start[start.index("-e") + 1] == "POSTGRES_PASSWORD", "handed over by name, not by value"
    # Checks on the password are bare booleans, so a failure never prints it.
    password = fake.password()
    generated = len(password) >= 32
    assert generated
    for argv in fake.argvs():
        assert not any(password in item for item in argv)
    shown = password in proc.stdout + proc.stderr
    assert not shown, "no output shows the password"
    assert "<password>" in proc.stdout, "a test echoing its DSN shows the masked form"


def test_tests_run_in_a_cleared_environment_bound_to_the_disposable_database(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.env.update(
        PGHOST="db.invalid", PGPORT="1", PGSERVICE="production", PGPASSFILE="/nonexistent",
        PEOPLE_DB_DSN="postgresql://db.invalid/people_db",
        PEOPLE_PUBS_INTEGRATION_DSN="postgresql://db.invalid/people_db",
        PEOPLE_DB_TEST_DSN="postgresql://db.invalid/people_db",
        PEOPLE_PUBS_SKIP_DOTENV="0",
        PYTEST_ADDOPTS="-k nothing_at_all",
        UNRELATED_SETTING="must-not-leak",
        **{"BASH_FUNC_leaked%%": "() {  echo leaked\n}"},  # an exported shell function
    )
    proc = fake.run()
    assert proc.returncode == 0, proc.stderr
    password = fake.password()
    runs = fake.test_runs()
    assert len(runs) == 2
    for run in runs:
        env = run["env"]
        assert set(env) - SHELL_VARIABLES == BOUND_ENVIRONMENT
        dsn = env["PEOPLE_PUBS_INTEGRATION_DSN"]
        assert env["PEOPLE_DB_TEST_DSN"] == dsn and env["PEOPLE_DB_DSN"] == dsn
        parts = urlsplit(dsn)
        assert (parts.hostname, parts.port, parts.path) == ("127.0.0.1", int(PORT), "/people_db")
        assert (env["PGHOST"], env["PGHOSTADDR"], env["PGPORT"]) == ("127.0.0.1", "127.0.0.1", PORT)
        assert (env["PGUSER"], env["PGDATABASE"]) == ("postgres", "people_db")
        bound = parts.password == password and env["PGPASSWORD"] == password
        assert bound, "the DSN and PGPASSWORD carry the run's password"
        assert env["PGSERVICEFILE"] == "/dev/null" and env["PGPASSFILE"] == "/dev/null"
        assert env["PEOPLE_PUBS_SKIP_DOTENV"] == "1", "DB/.env is never read"
        assert "PYTEST_ADDOPTS" not in env, "the run cannot be narrowed from outside"


def test_no_command_line_carries_the_password(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run()
    assert proc.returncode == 0, proc.stderr
    password = fake.password()
    launched = [run["argv"] for run in fake.test_runs()]
    launchers = fake.env_log.read_text(encoding="utf-8").splitlines() if fake.env_log.exists() else []
    assert launched, "the test command ran"
    for argv in fake.argvs() + launched:
        assert not any(password in item for item in argv), "no docker or test command line holds it"
    assert not any(password in line for line in launchers), "nor does any env launcher"
    assert all(run["env"]["PGPASSWORD"] == password for run in fake.test_runs()), "the environment does"


def test_suite_then_patch_reapplication_then_view_and_role_rerun(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run("-k", "attribution")
    assert proc.returncode == 0, proc.stderr
    events = fake.events_list()
    suite_at, views_at = [i for i, e in enumerate(events) if e[0] == "tests"]
    reapplications = [i for i, e in enumerate(events) if e[0] == "docker" and "psql" in e]
    assert len(reapplications) == 1
    assert suite_at < reapplications[0] < views_at, "suite, then re-application, then the rerun"
    assert "/docker-entrypoint-initdb.d/010_ingestion_runtime_schema.sql" in events[reapplications[0]]
    suite, views = fake.test_runs()
    assert "-k" in suite["argv"] and not set(RERUN_TESTS) & set(suite["argv"]), "extra arguments go to the main run"
    assert views["argv"][-2:] == RERUN_TESTS and "-k" not in views["argv"]
    for run in (suite, views):
        assert "-p" in run["argv"] and "no:cacheprovider" in run["argv"]
    assert "92 collected: 92 passed" in proc.stdout and "21 collected: 21 passed" in proc.stdout


def test_failing_tests_keep_their_exit_status_and_clean_up(tmp_path: Path) -> None:
    fake = Fake(tmp_path, statuses=(3,), totals=((92, 2, 0, 0),))
    proc = fake.run()
    assert proc.returncode == 3
    assert "the integration suite exited with status 3" in proc.stderr
    assert "--- diagnostics for run" in proc.stderr
    assert len(fake.test_runs()) == 1, "no re-application after a failed suite"
    shown = fake.password() in proc.stdout + proc.stderr
    assert not shown, "no output shows the password"
    fake.assert_nothing_left()


def test_view_rerun_failure_keeps_its_exit_status(tmp_path: Path) -> None:
    fake = Fake(tmp_path, statuses=(0, 4), totals=((92, 0, 0, 0), (21, 1, 0, 0)))
    proc = fake.run()
    assert proc.returncode == 4
    assert "after the re-application" in proc.stderr
    fake.assert_nothing_left()


@pytest.mark.parametrize(
    "totals, message",
    [(((92, 0, 0, 1),), "were skipped"), (((0, 0, 0, 0),), "no integration test ran")],
    ids=["skipped", "empty"],
)
def test_a_skipped_or_empty_run_fails(tmp_path: Path, totals, message: str) -> None:
    fake = Fake(tmp_path, statuses=(0,), totals=totals)
    proc = fake.run()
    assert proc.returncode == 1
    assert message in proc.stderr
    fake.assert_nothing_left()


def test_container_diagnostics_mask_the_password(tmp_path: Path) -> None:
    fake = Fake(tmp_path, mode="never-ready,leaky-logs", wait_seconds=1)
    proc = fake.run()
    assert proc.returncode == 1
    assert "--- diagnostics for run" in proc.stderr
    shown = fake.password() in proc.stdout + proc.stderr
    assert not shown, "no output shows the password"
    assert "the superuser password is <password>" in proc.stderr
    fake.assert_nothing_left()


def test_reapplication_errors_mask_the_password(tmp_path: Path) -> None:
    fake = Fake(tmp_path, mode="reapply-fails,leaky-logs")
    proc = fake.run()
    assert proc.returncode == 1
    shown = fake.password() in proc.stdout + proc.stderr
    assert not shown, "no output shows the password"
    assert "DETAIL:  password <password>" in proc.stderr
    fake.assert_nothing_left()


def test_reapplication_failure_stops_before_the_view_rerun(tmp_path: Path) -> None:
    fake = Fake(tmp_path, mode="reapply-fails")
    proc = fake.run()
    assert proc.returncode == 1
    assert "re-applying 010_ingestion_runtime_schema.sql failed" in proc.stderr
    assert len(fake.test_runs()) == 1
    fake.assert_nothing_left()


def test_readiness_wait_is_bounded(tmp_path: Path) -> None:
    fake = Fake(tmp_path, mode="never-ready", wait_seconds=1)
    started = time.monotonic()
    proc = fake.run()
    assert proc.returncode == 1
    assert "did not bootstrap within 1s" in proc.stderr
    assert time.monotonic() - started < 30
    assert fake.test_runs() == []
    fake.assert_nothing_left()


@pytest.mark.parametrize(
    "mode, message",
    [
        ("container-exits", "stopped during bootstrap"),
        ("init-error", "an init script failed"),
        # A daemon that cannot see the checkout mounts an empty directory.
        ("init-unseen", "the Docker daemon could not see"),
    ],
)
def test_failed_bootstrap_ends_the_wait_at_once(tmp_path: Path, mode: str, message: str) -> None:
    fake = Fake(tmp_path, mode=mode, wait_seconds=120)
    started = time.monotonic()
    proc = fake.run()
    assert proc.returncode == 1
    assert message in proc.stderr
    assert time.monotonic() - started < 30, "a failed bootstrap is not waited out"
    assert fake.test_runs() == []
    fake.assert_nothing_left()


def test_one_variable_sets_the_image_and_the_run_reports_what_it_used(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(env=dict(fake.env, DISPOSABLE_PG_IMAGE="registry.example.org/postgres:16"))
    assert proc.returncode == 0, proc.stderr
    start = next(e for e in fake.events_list() if e[:2] == ["docker", "run"])
    assert start[-1] == "registry.example.org/postgres:16"
    assert "image registry.example.org/postgres:16, digest" in proc.stdout
    # The runner's own variable still wins.
    other = Fake(tmp_path / "other")
    proc = other.run(env=dict(other.env, DISPOSABLE_PG_IMAGE="registry.example.org/postgres:16",
                              INTEGRATION_PG_IMAGE="postgres:16"))
    assert proc.returncode == 0, proc.stderr
    assert next(e for e in other.events_list() if e[:2] == ["docker", "run"])[-1] == "postgres:16"


def test_a_port_published_beyond_loopback_is_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path, mode="public-port")
    proc = fake.run()
    assert proc.returncode == 1
    assert "not published on a single loopback port" in proc.stderr
    assert fake.test_runs() == []
    fake.assert_nothing_left()


@pytest.mark.parametrize("mode", ["collision-container", "collision-volume"])
def test_name_collision_aborts_without_touching_anything(tmp_path: Path, mode: str) -> None:
    fake = Fake(tmp_path, mode=mode)
    proc = fake.run()
    assert proc.returncode == 1
    assert "already exists; not touching it" in proc.stderr
    assert not any(a[0] in ("run", "rm") or a[:2] in (["volume", "create"], ["volume", "rm"])
                   for a in fake.argvs())


def test_failed_start_removes_only_the_created_volume(tmp_path: Path) -> None:
    fake = Fake(tmp_path, mode="run-fails")
    proc = fake.run()
    assert proc.returncode == 125, "docker run's own status"
    _attempted, volume = fake.created()
    assert volume is not None
    assert fake.removals() == ([], [volume]), "no container exists, so none is removed"
    assert fake.inventory() == {"containers": [], "volumes": []}
    assert os.listdir(fake.tmpdir) == []


@pytest.mark.parametrize(
    "mode",
    ["rm-fails", "rm-ineffective", "volume-rm-fails", "list-fails"],
    ids=["container-removal-refused", "container-removal-without-effect", "volume-removal-refused",
         "daemon-not-answering"],
)
def test_a_cleanup_that_cannot_be_confirmed_fails_an_otherwise_successful_run(tmp_path: Path, mode: str) -> None:
    fake = Fake(tmp_path, mode=mode)
    proc = fake.run()
    assert "PASS:" in proc.stdout, "the tests passed"
    assert proc.returncode == 1, "but the run did not clean up"
    fake.assert_cleanup_reported(proc)
    assert os.listdir(fake.tmpdir) == []


def test_a_failure_keeps_its_status_when_the_cleanup_fails_too(tmp_path: Path) -> None:
    fake = Fake(tmp_path, mode="rm-fails", statuses=(3,), totals=((92, 2, 0, 0),))
    proc = fake.run()
    assert proc.returncode == 3, "the tests' own status, not the cleanup's"
    fake.assert_cleanup_reported(proc)


def test_a_signal_keeps_its_status_when_the_cleanup_fails_too(tmp_path: Path) -> None:
    fake = Fake(tmp_path, mode="never-ready,volume-rm-fails", wait_seconds=60)
    proc = fake.popen()
    fake.wait_for(lambda: any("pg_isready" in a for a in fake.argvs()), "its readiness loop", proc)
    os.killpg(proc.pid, signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 143
    fake.assert_cleanup_reported(subprocess.CompletedProcess(proc.args, proc.returncode, out, err))


def test_a_temporary_directory_that_cannot_be_removed_fails_the_run(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    _executable(fake.bindir / "rm", STUB_RM)
    fake.env["STUB_RM_FAIL_UNDER"] = str(fake.tmpdir)
    proc = fake.run()
    assert "PASS:" in proc.stdout, "the tests passed"
    assert proc.returncode == 1
    assert "could not remove the run's temporary directory" in proc.stderr
    assert "could not confirm" not in proc.stderr, "the Docker resources are gone"
    assert fake.inventory() == {"containers": [], "volumes": []}
    shown = fake.password() in proc.stdout + proc.stderr
    assert not shown


@pytest.mark.parametrize(
    "signum, status",
    [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)],
    ids=["SIGINT", "SIGTERM", "SIGHUP"],
)
def test_signal_cleans_up(tmp_path: Path, signum, status: int) -> None:
    fake = Fake(tmp_path, mode="never-ready", wait_seconds=60)
    proc = fake.popen()
    fake.wait_for(lambda: any("pg_isready" in a for a in fake.argvs()), "its readiness loop", proc)
    os.killpg(proc.pid, signum)
    proc.communicate(timeout=30)
    assert proc.returncode == status
    fake.assert_nothing_left()


def test_a_second_signal_cannot_cut_the_cleanup_short(tmp_path: Path) -> None:
    # Two Ctrl-C in a row: the second reaches the runner and every process it
    # has started while the container is being removed.
    fake = Fake(tmp_path, mode="never-ready,rm-slow", wait_seconds=60)
    proc = fake.popen()
    fake.wait_for(lambda: any("pg_isready" in a for a in fake.argvs()), "its readiness loop", proc)
    os.killpg(proc.pid, signal.SIGINT)
    fake.wait_for(lambda: any(a[0] == "rm" for a in fake.argvs()), "its cleanup", proc)
    os.killpg(proc.pid, signal.SIGINT)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    fake.assert_nothing_left()


@pytest.mark.skipif(shutil.which("timeout") is None, reason="needs the timeout utility")
def test_an_external_timeout_cleans_up(tmp_path: Path) -> None:
    # timeout signals the runner and then its whole process group.
    fake = Fake(tmp_path, mode="never-ready", wait_seconds=60)
    proc = fake.popen(["timeout", "-s", "TERM", "2", BASH, str(fake.script)])
    proc.communicate(timeout=60)
    assert proc.returncode == 124
    fake.assert_nothing_left()


def test_prerequisites_are_checked_before_anything_is_created(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(env=dict(fake.env, INTEGRATION_WAIT_SECONDS="soon"))
    assert proc.returncode == 2 and "INTEGRATION_WAIT_SECONDS" in proc.stderr

    proc = fake.run(env=dict(fake.env, STUB_MODE="no-daemon"))
    assert proc.returncode == 1 and "cannot reach the Docker daemon" in proc.stderr

    tools = tmp_path / "tools"
    tools.mkdir()
    os.symlink(shutil.which("dirname"), tools / "dirname")
    proc = fake.run(env=dict(fake.env, PATH=str(tools)))
    assert proc.returncode == 1 and "docker was not found on PATH" in proc.stderr

    shutil.rmtree(fake.db.parent / ".venv")
    proc = fake.run()
    assert proc.returncode == 1 and "python3 -m venv .venv" in proc.stderr
    assert fake.argvs() == [["info"]], "nothing but the daemon probe ever reached Docker"
