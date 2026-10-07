"""Resource management of DB/run_migration_smoke.sh, offline.

The real script runs against a stub `docker` executable that records every
invocation and keeps a small inventory of the containers and volumes it was
asked to create, so the test can prove — without Docker and without touching
any existing service — that the script creates uniquely named, labelled
resources, registers cleanup before creating them, never pre-deletes, removes
only what it created (after success, a failed start, a bootstrap that never
completes, a container that exits, or an interrupt), bounds its readiness
wait, and aborts on a name collision without touching the colliding resource.
DB/run_task_1a_demo.sh shares the same lifecycle; it runs from a scratch
checkout whose interpreter records every Python step instead of connecting
anywhere. Both are checked for a second signal arriving while the cleanup is
removing the container; for a cleanup that fails -- a removal the daemon
refuses, one that reports success but leaves the resource, a daemon that no
longer answers, or (for the demonstration) a temporary directory that cannot
be removed -- which is reported with the commands that find the leftovers and
fails an otherwise successful run, while an earlier failure or a signal keeps
its own exit status; and for their superuser password: random per run, handed
to Docker by name so that it is in no command line, absent from their output,
and masked in a container log that contains it. The demonstration's recorded
Python steps also show that each gets the disposable database only through
its cleared environment, never as an argument.
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
SCRIPT = DB_DIR / "run_migration_smoke.sh"
LABEL_KEY = "pubdex.migration-smoke.run"
REAL_RM = shutil.which("rm") or "/bin/rm"

STUB_DOCKER = r'''#!/usr/bin/env python3
"""Stub docker CLI: records argv, keeps an inventory, behaves per SMOKE_STUB_MODE,
a comma-separated list of modes."""
import json, os, sys, time
args = sys.argv[1:]
modes = set(os.environ.get("SMOKE_STUB_MODE", "ok").split(","))
state_path = os.environ["SMOKE_STUB_STATE"]
with open(os.environ["SMOKE_STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
try:
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)
except FileNotFoundError:
    state = {"containers": {}, "volumes": {}}

def save():
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)

def option(flag):
    return args[args.index(flag) + 1]

def labelled(kind):
    """Lists the resources whose label matches --filter label=KEY=VALUE."""
    if "list-fails" in modes:
        print("stub: cannot connect to the Docker daemon", file=sys.stderr); sys.exit(1)
    wanted = option("--filter").split("=", 1)[1]
    for name, item in sorted(state[kind].items()):
        if item["label"] == wanted:
            print(name)
    sys.exit(0)

def inspect(kind):
    name = args[-1]
    if name not in state[kind] and "collision" not in modes:
        sys.exit(1)
    if "-f" in args:  # only the readiness loop asks for a field: State.Running
        print("false" if "container-exits" in modes else "true")
    else:
        print("[]")
    sys.exit(0)

if args[:2] == ["container", "inspect"]:
    inspect("containers")
if args[:2] == ["volume", "inspect"]:
    inspect("volumes")
if args[:2] == ["volume", "create"]:
    state["volumes"][args[-1]] = {"label": option("--label")}
    save(); print(args[-1]); sys.exit(0)
if args[:2] == ["volume", "ls"]:
    labelled("volumes")
if args[:2] == ["volume", "rm"]:
    if "volume-rm-fails" in modes:
        print("stub: volume is in use", file=sys.stderr); sys.exit(1)
    state["volumes"].pop(args[-1], None); save(); sys.exit(0)
if args[0] == "run":
    # The password the container receives, which never belongs in argv.
    with open(state_path + ".password", "w", encoding="utf-8") as fh:
        fh.write(os.environ.get("POSTGRES_PASSWORD", ""))
    if "run-fails" in modes:
        print("stub: cannot start container", file=sys.stderr); sys.exit(125)
    state["containers"][option("--name")] = {"label": option("--label")}
    save(); print("stub-container-id"); sys.exit(0)
if args[0] == "ps":
    labelled("containers")
if args[0] == "logs":
    # init-unseen: an empty mount, as when the daemon cannot see the checkout
    for script in sorted(os.listdir(os.environ["SMOKE_STUB_INIT_DIR"])):
        if script.endswith(".sql") and "init-unseen" not in modes:
            print("docker-entrypoint.sh: running /docker-entrypoint-initdb.d/" + script)
    if os.environ.get("SMOKE_STUB_LEAK_PASSWORD"):
        with open(state_path + ".password", encoding="utf-8") as fh:
            print("server log: the superuser password is " + fh.read())
    print("PostgreSQL init process complete; ready for start up.")
    sys.exit(0)
if args[0] == "port":
    # Loopback port 1: nothing listens there, and the recording interpreter
    # of the demonstration never tries.
    print("127.0.0.1:1"); sys.exit(0)
if args[0] == "exec":
    if "pg_isready" in args:
        sys.exit(1 if "never-ready" in modes else 0)
    if "psql" in args:
        sys.stdin.read(); sys.exit(0)
    sys.exit(0)
if args[0] == "rm":
    time.sleep(float(os.environ.get("SMOKE_STUB_RM_DELAY", "0")))
    if "rm-fails" in modes:
        print("stub: removal failed", file=sys.stderr); sys.exit(1)
    if "rm-ineffective" not in modes:
        state["containers"].pop(args[-1], None); save()
    sys.exit(0)
print("stub: unsupported " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''

# Stands in for rm: refuses to remove anything below STUB_RM_FAIL_UNDER and
# behaves as rm otherwise.
STUB_RM = f'''#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    "$STUB_RM_FAIL_UNDER"/*) echo "rm: cannot remove $arg: Operation not permitted" >&2; exit 1 ;;
  esac
done
exec "{REAL_RM}" "$@"
'''

# Stands in for .venv/bin/python in a scratch checkout: records each call and
# does just enough for the demonstration to finish -- the export step writes
# the expected CSV, and the demonstration's own check of it ("-") really runs.
DEMO_RECORDER = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(Path(__file__).resolve().parent / "python-calls.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": args, "env": dict(os.environ)}) + "\n")
if args[:2] == ["-m", "people_pubs.sync.export_publications"]:
    Path(args[args.index("--output") + 1]).write_text(
        "pub_id,doi,title,year,venue,source_of_truth,internal_authors\n"
        "1,10.5555/pubdex-fixture-001,Synthetic Collective Behaviour Paper,2024,"
        "Journal of Synthetic Metadata,crossref+orcid,Ada Example\n",
        encoding="utf-8",
    )
elif args[:1] == ["-"]:
    sys.argv = args
    exec(compile(sys.stdin.read(), "<stdin>", "exec"), {"__name__": "__main__"})
'''
DEMO_ENVIRONMENT = {
    "HOME", "PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "PEOPLE_PUBS_SKIP_DOTENV",
    "PEOPLE_DB_DSN", "PEOPLE_PUBS_CROSSREF_MAILTO", "PGHOST", "PGHOSTADDR", "PGPORT", "PGUSER",
    "PGPASSWORD", "PGDATABASE", "PGSSLMODE", "PGSERVICEFILE", "PGSYSCONFDIR", "PGPASSFILE",
}
SHELL_VARIABLES = {"PWD", "OLDPWD", "SHLVL", "_"}

# kind -> (ownership label, readiness-wait variable, resource name prefix)
KINDS = {
    "migration-smoke": ("pubdex.migration-smoke.run", "SMOKE_WAIT_SECONDS", "pubdex-migration-smoke-"),
    "task-1a-demo": ("pubdex.task-1a-demo.run", "TASK_1A_WAIT_SECONDS", "pubdex-task-1a-demo-"),
}
BOTH_SCRIPTS = pytest.mark.parametrize("kind", list(KINDS))


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _demo_checkout(root: Path) -> Path:
    """A scratch checkout holding the real demonstration, stub init scripts and
    a recording interpreter; returns the demonstration's path."""
    for script in sorted((DB_DIR / "init").glob("*.sql")):
        (root / "DB" / "init").mkdir(parents=True, exist_ok=True)
        (root / "DB" / "init" / script.name).write_text("-- stub\n", encoding="utf-8")
    demo = root / "DB" / "run_task_1a_demo.sh"
    shutil.copy2(DB_DIR / "run_task_1a_demo.sh", demo)
    (root / "DB" / "lib").mkdir()
    shutil.copy2(DB_DIR / "lib" / "disposable_postgres.sh", root / "DB" / "lib")
    bindir = root / ".venv" / "bin"
    _executable(bindir / "recorder.py", DEMO_RECORDER)
    _executable(bindir / "python", f'#!/bin/sh\nexec "{sys.executable}" "{bindir / "recorder.py"}" "$@"\n')
    return demo


class _Run:
    def __init__(self, tmp_path: Path, mode: str, wait_seconds: int = 5, kind: str = "migration-smoke") -> None:
        self.dir = tmp_path
        self.kind = kind
        self.label_key, self.wait_variable, self.prefix = KINDS[kind]
        self.bindir = tmp_path / "bin"
        _executable(self.bindir / "docker", STUB_DOCKER)
        self.log = tmp_path / "docker.log"
        self.state = tmp_path / "state.json"
        self.private = tmp_path / "tmp"  # TMPDIR: the demonstration's private directory goes here
        self.private.mkdir()
        self.script = SCRIPT if kind == "migration-smoke" else _demo_checkout(tmp_path / "checkout")
        self.env = dict(
            os.environ,
            PATH=f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            TMPDIR=str(self.private),
            SMOKE_STUB_MODE=mode,
            SMOKE_STUB_LOG=str(self.log),
            SMOKE_STUB_STATE=str(self.state),
            SMOKE_STUB_INIT_DIR=str(DB_DIR / "init"),
            **{self.wait_variable: str(wait_seconds)},
        )

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.script)], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)

    def popen(self) -> subprocess.Popen:
        # A session of its own, so a signal can go to the whole group as a
        # terminal would send it.
        return subprocess.Popen(["bash", str(self.script)], env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def inventory(self) -> dict:
        if not self.state.exists():
            return {"containers": {}, "volumes": {}}
        return json.loads(self.state.read_text(encoding="utf-8"))

    def password(self) -> str:
        return Path(str(self.state) + ".password").read_text(encoding="utf-8")

    def run_id(self) -> str:
        container, volume = _created(self.calls())
        return (container or volume)[len(self.prefix):]

    def wait_for(self, predicate, what: str, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(predicate(c) for c in self.calls()):
                return
            time.sleep(0.02)
        os.killpg(proc.pid, signal.SIGKILL)
        pytest.fail(f"the script never reached {what}")

    def assert_cleanup_reported(self, proc: subprocess.CompletedProcess) -> None:
        run_id = self.run_id()
        assert f"could not confirm that run {run_id} left nothing behind" in proc.stderr
        assert f"docker ps -a --filter label={self.label_key}={run_id}" in proc.stderr
        assert f"docker volume ls --filter label={self.label_key}={run_id}" in proc.stderr
        shown = self.password() in proc.stdout + proc.stderr
        assert not shown, "the warning does not show the password"


def _created(calls: list[list[str]]) -> tuple[str | None, str | None]:
    container = volume = None
    for c in calls:
        if c[:2] == ["volume", "create"]:
            volume = c[-1]
        if c and c[0] == "run":
            container = c[c.index("--name") + 1]
    return container, volume


def _removals(calls: list[list[str]]) -> tuple[list[str], list[str]]:
    containers = [c[-1] for c in calls if c and c[0] == "rm"]
    volumes = [c[-1] for c in calls if c[:2] == ["volume", "rm"]]
    return containers, volumes


def test_success_uses_unique_labelled_resources_and_removes_only_them(tmp_path: Path) -> None:
    first = _Run(tmp_path / "one", mode="ok")
    second = _Run(tmp_path / "two", mode="ok")
    for run in (first, second):
        proc = run.run()
        assert proc.returncode == 0, proc.stderr
        assert "PASS" in proc.stdout
        calls = run.calls()
        container, volume = _created(calls)
        assert container and volume and container.startswith("pubdex-migration-smoke-")
        run_id = container[len("pubdex-migration-smoke-"):]
        assert volume == container, "container and volume share the per-run name"
        create = next(c for c in calls if c[:2] == ["volume", "create"])
        assert create[create.index("--label") + 1] == f"{LABEL_KEY}={run_id}"
        start = next(c for c in calls if c[0] == "run")
        assert start[start.index("--label") + 1] == f"{LABEL_KEY}={run_id}"
        assert "-p" not in start and "--publish" not in start, "no host port is published"
        # Never a pre-delete: no removal of anything before the resources exist.
        first_create = next(i for i, c in enumerate(calls) if c[:2] == ["volume", "create"])
        assert not any(c[0] == "rm" or c[:2] == ["volume", "rm"] for c in calls[:first_create])
        # The cleanup finds what to remove by this run's label alone.
        listings = [c for c in calls if c[0] == "ps" or c[:2] == ["volume", "ls"]]
        assert listings and all(c[c.index("--filter") + 1] == f"label={LABEL_KEY}={run_id}" for c in listings)
        rm_containers, rm_volumes = _removals(calls)
        assert rm_containers == [container] and rm_volumes == [volume], "removes exactly what it created"
        rm_call = next(c for c in calls if c[0] == "rm")
        assert "-v" in rm_call, "the container's anonymous volumes go with it"
        assert run.inventory() == {"containers": {}, "volumes": {}}, "nothing is left behind"
    c1, _ = _created(first.calls())
    c2, _ = _created(second.calls())
    assert c1 != c2, "two runs never share a resource name"


def test_failed_start_removes_only_the_created_volume(tmp_path: Path) -> None:
    run = _Run(tmp_path, mode="run-fails")
    proc = run.run()
    assert proc.returncode == 125, "docker run's own status"
    calls = run.calls()
    _container, volume = _created(calls)
    assert volume is not None
    rm_containers, rm_volumes = _removals(calls)
    assert rm_containers == [], "no container was created, so none is removed"
    assert rm_volumes == [volume]
    assert run.inventory() == {"containers": {}, "volumes": {}}
    assert "could not confirm" not in proc.stderr


def test_name_collision_aborts_without_touching_the_existing_resource(tmp_path: Path) -> None:
    run = _Run(tmp_path, mode="collision")
    proc = run.run()
    assert proc.returncode != 0
    assert "already exists; not touching it" in proc.stderr
    calls = run.calls()
    assert not any(c[0] in ("run", "rm") or c[:2] in (["volume", "create"], ["volume", "rm"]) for c in calls), (
        "a collision must neither create nor remove anything"
    )


def test_bootstrap_wait_is_bounded_and_cleans_up(tmp_path: Path) -> None:
    run = _Run(tmp_path, mode="never-ready", wait_seconds=2)
    started = time.monotonic()
    proc = run.run()
    elapsed = time.monotonic() - started
    assert proc.returncode != 0
    assert "did not bootstrap within 2s" in proc.stderr
    assert elapsed < 30, f"the wait must be bounded by SMOKE_WAIT_SECONDS (took {elapsed:.0f}s)"
    container, volume = _created(run.calls())
    assert _removals(run.calls()) == ([container], [volume])
    assert run.inventory() == {"containers": {}, "volumes": {}}


def test_container_exit_during_bootstrap_fails_fast_and_cleans_up(tmp_path: Path) -> None:
    run = _Run(tmp_path, mode="container-exits", wait_seconds=60)
    started = time.monotonic()
    proc = run.run()
    assert proc.returncode != 0
    assert "stopped during bootstrap" in proc.stderr
    assert time.monotonic() - started < 30, "an exited container must not be waited for"
    container, volume = _created(run.calls())
    assert _removals(run.calls()) == ([container], [volume])
    assert run.inventory() == {"containers": {}, "volumes": {}}


@BOTH_SCRIPTS
def test_an_init_directory_the_daemon_cannot_see_fails_at_once(tmp_path: Path, kind: str) -> None:
    # A daemon on another machine, or in another container, mounts an empty
    # directory in place of DB/init/: the entrypoint then completes without
    # running a single init script, and waiting longer cannot help.
    run = _Run(tmp_path, mode="init-unseen", wait_seconds=60, kind=kind)
    started = time.monotonic()
    proc = run.run()
    assert proc.returncode == 1
    assert "the Docker daemon could not see" in proc.stderr
    assert "Running the suite elsewhere" in proc.stderr
    assert time.monotonic() - started < 30
    assert run.inventory() == {"containers": {}, "volumes": {}}


IMAGE_VARIABLES = {"migration-smoke": "SMOKE_PG_IMAGE", "task-1a-demo": "TASK_1A_PG_IMAGE"}


@BOTH_SCRIPTS
def test_one_variable_sets_the_image_and_the_run_reports_what_it_used(tmp_path: Path, kind: str) -> None:
    run = _Run(tmp_path / "shared", mode="ok", kind=kind)
    run.env["DISPOSABLE_PG_IMAGE"] = "registry.example.org/postgres:16"
    proc = run.run()
    assert proc.returncode == 0, proc.stderr
    assert next(c for c in run.calls() if c[0] == "run")[-1] == "registry.example.org/postgres:16"
    assert "image registry.example.org/postgres:16, digest" in proc.stdout
    own = _Run(tmp_path / "own", mode="ok", kind=kind)
    own.env.update(DISPOSABLE_PG_IMAGE="registry.example.org/postgres:16", **{IMAGE_VARIABLES[kind]: "postgres:16"})
    assert own.run().returncode == 0
    assert next(c for c in own.calls() if c[0] == "run")[-1] == "postgres:16", "the script's own variable wins"


@pytest.mark.parametrize("signum, status", [(signal.SIGINT, 130), (signal.SIGTERM, 143)], ids=["SIGINT", "SIGTERM"])
def test_interrupt_during_wait_cleans_up(tmp_path: Path, signum, status: int) -> None:
    run = _Run(tmp_path, mode="never-ready", wait_seconds=60)
    proc = subprocess.Popen(
        ["bash", str(SCRIPT)], env=run.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any("pg_isready" in c for c in run.calls()):
            break
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("the script never reached its readiness loop")
    proc.send_signal(signum)
    proc.wait(timeout=30)
    assert proc.returncode == status
    container, volume = _created(run.calls())
    assert _removals(run.calls()) == ([container], [volume]), "an interrupted run still cleans up its own resources"
    assert run.inventory() == {"containers": {}, "volumes": {}}


@BOTH_SCRIPTS
def test_a_second_signal_cannot_cut_the_cleanup_short(tmp_path: Path, kind: str) -> None:
    # Two Ctrl-C in a row: each reaches the script and every process it has
    # started, the second one while the container is being removed.
    run = _Run(tmp_path, mode="never-ready", wait_seconds=60, kind=kind)
    run.env["SMOKE_STUB_RM_DELAY"] = "1"
    proc = run.popen()
    run.wait_for(lambda c: "pg_isready" in c, "its readiness loop", proc)
    os.killpg(proc.pid, signal.SIGINT)
    run.wait_for(lambda c: c[0] == "rm", "its cleanup", proc)
    os.killpg(proc.pid, signal.SIGINT)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    container, volume = _created(run.calls())
    assert _removals(run.calls()) == ([container], [volume]), "the cleanup still finished"
    assert run.inventory() == {"containers": {}, "volumes": {}}
    assert os.listdir(run.private) == [], "no temporary directory is left either"


@BOTH_SCRIPTS
def test_password_is_random_handed_over_by_name_and_never_shown(tmp_path: Path, kind: str) -> None:
    passwords = []
    for label in ("one", "two"):
        run = _Run(tmp_path / label, mode="ok", kind=kind)
        proc = run.run()
        assert proc.returncode == 0 and "PASS" in proc.stdout, proc.stderr
        # Every check on the password is a bare boolean, so that a failing
        # assertion reports True or False and never the value itself.
        password = run.password()
        generated = len(password) >= 24
        assert generated, "a password was generated and passed through the environment"
        calls = run.calls()
        start = next(c for c in calls if c[0] == "run")
        assert start[start.index("-e") + 1] == "POSTGRES_PASSWORD", "handed over by name, not by value"
        assert not any(password in item for call in calls for item in call), "no docker argv holds it"
        shown = password in proc.stdout + proc.stderr
        assert not shown, "and no output shows it"
        derived = run.run_id() in password
        assert not derived, "independent of the printed run id"
        assert run.inventory() == {"containers": {}, "volumes": {}}
        assert os.listdir(run.private) == [], "and no temporary directory is left"
        passwords.append(password)
    repeated = passwords[0] == passwords[1]
    assert not repeated, "every run gets its own password"


@BOTH_SCRIPTS
def test_container_log_tail_is_masked(tmp_path: Path, kind: str) -> None:
    run = _Run(tmp_path, mode="container-exits", kind=kind)
    run.env["SMOKE_STUB_LEAK_PASSWORD"] = "1"
    proc = run.run()
    assert proc.returncode == 1
    assert "stopped during bootstrap" in proc.stderr
    password = run.password()
    shown = not password or password in proc.stdout + proc.stderr
    assert not shown, "only the masked form may appear"
    assert "the superuser password is <password>" in proc.stderr
    assert run.inventory() == {"containers": {}, "volumes": {}}


@BOTH_SCRIPTS
@pytest.mark.parametrize(
    "mode",
    ["rm-fails", "rm-ineffective", "volume-rm-fails", "list-fails"],
    ids=["container-removal-refused", "container-removal-without-effect", "volume-removal-refused",
         "daemon-not-answering"],
)
def test_a_cleanup_that_cannot_be_confirmed_fails_an_otherwise_successful_run(
    tmp_path: Path, kind: str, mode: str,
) -> None:
    run = _Run(tmp_path, mode=mode, kind=kind)
    proc = run.run()
    assert "PASS" in proc.stdout, "the run itself succeeded"
    assert proc.returncode == 1, "and still fails, because its cleanup did not"
    run.assert_cleanup_reported(proc)


@BOTH_SCRIPTS
def test_an_earlier_failure_keeps_its_status_when_the_cleanup_fails_too(tmp_path: Path, kind: str) -> None:
    run = _Run(tmp_path, mode="run-fails,volume-rm-fails", kind=kind)
    proc = run.run()
    assert proc.returncode == 125, "docker run's own status, not the cleanup's"
    run.assert_cleanup_reported(proc)


@BOTH_SCRIPTS
def test_a_signal_keeps_its_status_when_the_cleanup_fails_too(tmp_path: Path, kind: str) -> None:
    run = _Run(tmp_path, mode="never-ready,rm-fails", wait_seconds=60, kind=kind)
    proc = run.popen()
    run.wait_for(lambda c: "pg_isready" in c, "its readiness loop", proc)
    os.killpg(proc.pid, signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 143
    run.assert_cleanup_reported(subprocess.CompletedProcess(proc.args, proc.returncode, out, err))


def test_a_temporary_directory_that_cannot_be_removed_fails_the_demonstration(tmp_path: Path) -> None:
    run = _Run(tmp_path, mode="ok", kind="task-1a-demo")
    _executable(run.bindir / "rm", STUB_RM)
    run.env["STUB_RM_FAIL_UNDER"] = str(run.private)
    proc = run.run()
    assert "PASS" in proc.stdout, "the run itself succeeded"
    assert proc.returncode == 1
    assert "could not remove the run's temporary directory" in proc.stderr
    assert run.inventory() == {"containers": {}, "volumes": {}}, "the Docker resources are gone"
    assert "could not confirm" not in proc.stderr
    shown = run.password() in proc.stdout + proc.stderr
    assert not shown


def test_demonstration_steps_get_the_database_only_through_their_environment(tmp_path: Path) -> None:
    run = _Run(tmp_path, mode="ok", kind="task-1a-demo")
    env_log = run.dir / "env.log"  # an `env NAME=value` launcher would show up here
    _executable(
        run.bindir / "env",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{env_log}"\nexec "{shutil.which("env") or "/usr/bin/env"}" "$@"\n',
    )
    elsewhere = "postgresql://db.invalid/people_db"
    run.env.update(
        PEOPLE_DB_DSN=elsewhere, PEOPLE_PUBS_INTEGRATION_DSN=elsewhere,
        PGHOST="db.invalid", PGPORT="1", PGSERVICE="production", PEOPLE_PUBS_SKIP_DOTENV="0",
        PYTEST_ADDOPTS="-k nothing_at_all", UNRELATED_SETTING="must-not-leak",
    )
    proc = run.run()
    assert proc.returncode == 0, proc.stderr
    assert "PASS: clean-clone acceptance run completed" in proc.stdout

    password = run.password()
    log = run.script.parents[1] / ".venv" / "bin" / "python-calls.jsonl"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    modules = [call["argv"][1] for call in calls if call["argv"][:1] == ["-m"]]
    assert modules == [
        "people_pubs.sync.add_people", "people_pubs.sync.fixture_demo",
        "people_pubs.sync.attribution_rules", "people_pubs.sync.export_publications",
    ]
    # Checks on the password are bare booleans, so a failure never prints it.
    for call in calls:
        takes_dsn = "--dsn" in call["argv"]
        assert not takes_dsn, "no step takes a --dsn argument"
        assert not any(password in item for item in call["argv"]), "no Python command line holds it"
        step_env = call["env"]
        assert set(step_env) - SHELL_VARIABLES == DEMO_ENVIRONMENT, "nothing inherited, nothing missing"
        dsn = urlsplit(step_env["PEOPLE_DB_DSN"])
        assert (dsn.hostname, dsn.port, dsn.path) == ("127.0.0.1", 1, "/people_db")
        assert (step_env["PGHOST"], step_env["PGPORT"]) == ("127.0.0.1", "1")
        bound = dsn.password == password and step_env["PGPASSWORD"] == password
        assert bound, "the environment carries the run's password"
        assert step_env["PEOPLE_PUBS_SKIP_DOTENV"] == "1"
    for argv in run.calls():
        assert not any(password in item for item in argv), "no docker command line holds it"
    launchers = env_log.read_text(encoding="utf-8") if env_log.exists() else ""
    launched_with_it = password in launchers
    assert not launched_with_it, "nor does any env launcher"
    shown = password in proc.stdout + proc.stderr
    assert not shown, "and no output shows it"
    assert run.inventory() == {"containers": {}, "volumes": {}}


def test_migration_smoke_masks_the_log_tail_after_a_timeout(tmp_path: Path) -> None:
    run = _Run(tmp_path, mode="never-ready", wait_seconds=1)
    run.env["SMOKE_STUB_LEAK_PASSWORD"] = "1"
    proc = run.run()
    assert proc.returncode == 1 and "did not bootstrap within 1s" in proc.stderr
    shown = run.password() in proc.stdout + proc.stderr
    assert not shown, "only the masked form may appear"
    assert "the superuser password is <password>" in proc.stderr
