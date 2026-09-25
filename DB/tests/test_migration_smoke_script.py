"""Resource management of DB/run_migration_smoke.sh, offline.

The real script runs against a stub `docker` executable that records every
invocation and keeps a small inventory of the containers and volumes it was
asked to create, so the test can prove — without Docker and without touching
any existing service — that the script creates uniquely named, labelled
resources, registers cleanup before creating them, never pre-deletes, removes
only what it created (after success, a failed start, a bootstrap that never
completes, a container that exits, or an interrupt), bounds its readiness
wait, and aborts on a name collision without touching the colliding resource.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

DB_DIR = Path(__file__).resolve().parents[1]
SCRIPT = DB_DIR / "run_migration_smoke.sh"
LABEL_KEY = "pubdex.migration-smoke.run"

STUB_DOCKER = r'''#!/usr/bin/env python3
"""Stub docker CLI: records argv, keeps an inventory, behaves per SMOKE_STUB_MODE."""
import json, os, sys
args = sys.argv[1:]
mode = os.environ.get("SMOKE_STUB_MODE", "ok")
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

def fmt_value(kind, name):
    fmt = args[args.index("-f") + 1]
    if "State.Running" in fmt:
        return "false" if mode == "container-exits" else "true"
    if mode == "collision" and name not in state[kind]:
        return "someone-elses-run"
    return state[kind][name]["label"]

def inspect(kind):
    name = args[-1]
    exists = name in state[kind] or (mode == "collision")
    if not exists:
        sys.exit(1)
    if "-f" in args:
        print(fmt_value(kind, name))
    else:
        print("[]")
    sys.exit(0)

if args[:2] == ["container", "inspect"]:
    inspect("containers")
if args[:2] == ["volume", "inspect"]:
    inspect("volumes")
if args[:2] == ["volume", "create"]:
    label = args[args.index("--label") + 1].split("=", 1)[1]
    state["volumes"][args[-1]] = {"label": label}
    save(); sys.exit(0)
if args[:2] == ["volume", "rm"]:
    state["volumes"].pop(args[-1], None); save(); sys.exit(0)
if args[0] == "run":
    if mode == "run-fails":
        print("stub: cannot start container", file=sys.stderr); sys.exit(125)
    name = args[args.index("--name") + 1]
    label = args[args.index("--label") + 1].split("=", 1)[1]
    state["containers"][name] = {"label": label}
    save(); print("stub-container-id"); sys.exit(0)
if args[0] == "logs":
    for script in sorted(os.listdir(os.environ["SMOKE_STUB_INIT_DIR"])):
        if script.endswith(".sql"):
            print("docker-entrypoint.sh: running /docker-entrypoint-initdb.d/" + script)
    print("PostgreSQL init process complete; ready for start up.")
    sys.exit(0)
if args[0] == "exec":
    if "pg_isready" in args:
        sys.exit(1 if mode == "never-ready" else 0)
    if "psql" in args:
        sys.stdin.read(); sys.exit(0)
    sys.exit(0)
if args[0] == "rm":
    state["containers"].pop(args[-1], None); save(); sys.exit(0)
print("stub: unsupported " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


class _Run:
    def __init__(self, tmp_path: Path, mode: str, wait_seconds: int = 5) -> None:
        self.dir = tmp_path
        bindir = tmp_path / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        stub = bindir / "docker"
        stub.write_text(STUB_DOCKER, encoding="utf-8")
        stub.chmod(0o755)
        self.log = tmp_path / "docker.log"
        self.state = tmp_path / "state.json"
        self.env = dict(
            os.environ,
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            SMOKE_STUB_MODE=mode,
            SMOKE_STUB_LOG=str(self.log),
            SMOKE_STUB_STATE=str(self.state),
            SMOKE_STUB_INIT_DIR=str(DB_DIR / "init"),
            SMOKE_WAIT_SECONDS=str(wait_seconds),
        )

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True, timeout=120)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def inventory(self) -> dict:
        if not self.state.exists():
            return {"containers": {}, "volumes": {}}
        return json.loads(self.state.read_text(encoding="utf-8"))


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
    assert proc.returncode != 0
    calls = run.calls()
    _container, volume = _created(calls)
    assert volume is not None
    rm_containers, rm_volumes = _removals(calls)
    assert rm_containers == [], "no container was created, so none is removed"
    assert rm_volumes == [volume]
    assert run.inventory() == {"containers": {}, "volumes": {}}


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
    assert "not bootstrapped after 2s" in proc.stderr
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


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_interrupt_during_wait_cleans_up(tmp_path: Path, signum) -> None:
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
    assert proc.returncode != 0
    container, volume = _created(run.calls())
    assert _removals(run.calls()) == ([container], [volume]), "an interrupted run still cleans up its own resources"
    assert run.inventory() == {"containers": {}, "volumes": {}}
