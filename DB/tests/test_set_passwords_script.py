"""set_passwords.sh, offline: giving the database of a running Compose
deployment the passwords its secret files hold now.

The real script runs from a scratch directory standing in for the directory
of compose.yaml, next to the real create_secrets.sh, against a stub `docker`
that logs every call with its working directory and environment and keeps
what each call received on its standard input. That proves, without Docker,
that the script checks the files with create_secrets.sh --check, that the
project's PostgreSQL runs, and that a new container of the read service and
one of ingestion read exactly the host's bytes, before it sends anything;
that it then hands the three passwords, read from the host's files as the
containers read them, to 020_role_passwords.sh --stdin through `docker compose
exec -T` and nothing else; that every refusal before that point changes
nothing; that a failure or a missing confirmation of that script fails the
run; that docker compose runs in the directory of compose.yaml with the
operator's options and nothing else; and that no password reaches a command
line, an environment or the output. The Compose deployment runs it against
PostgreSQL itself, in run_operator_acceptance.sh.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest

from tests.test_password_files import MARKER, PASSWORD_SAMPLES, UNUSABLE_SAMPLES

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "set_passwords.sh"
NAMES = ("postgres_password", "app_readonly_password", "app_writer_password")
# Each file is named after its role.
ROLES = {name: name.removesuffix("_password") for name in NAMES}
PASSWORDS = {"postgres_password": "example-superuser-61b0", "app_readonly_password": "example-reader-2f9c",
             "app_writer_password": "example-writer-d417"}
CONFIRMATION = ("020_role_passwords.sh: postgres, app_readonly and app_writer have the passwords from standard "
                "input, and each authenticated a new connection.")
INIT_SCRIPT = "/docker-entrypoint-initdb.d/020_role_passwords.sh"

STUB_DOCKER = r'''#!/usr/bin/env bash
# Stand-in for the docker CLI as set_passwords.sh calls it. A new container's
# /run/secrets/<name> is @STATE@/mounted/<name>.
set -u
state="@STATE@"
mode="$(cat "$state/mode" 2>/dev/null || true)"
has() { case ",$mode," in *",$1,"*) return 0 ;; esac; return 1; }
n="$(ls "$state/calls" 2>/dev/null | wc -l | tr -d ' ')"
call="$state/calls/$(printf '%02d' "$n")"
mkdir -p "$call"
printf '%s\n' "$@" > "$call/argv"
pwd -P > "$call/cwd"
env | sort > "$call/env"
[ "$1" = compose ] || { echo "stub: unsupported docker $*" >&2; exit 2; }
shift
while [ $# -gt 0 ]; do
  case "$1" in
    -p|--project-name|-f|--file|--env-file) shift 2 ;;
    *) break ;;
  esac
done
case "$1" in
  ps)
    has ps-fails && { echo "stub: no configuration file provided: not found" >&2; exit 1; }
    has postgres-stopped || echo "4f1c0ffee"
    exit 0 ;;
  run)
    cat > "$call/stdin"
    has run-fails && { echo "stub: no such service: api" >&2; exit 1; }
    name="${!#}"; name="${name#/run/secrets/}"
    service=""
    for a in "$@"; do case "$a" in api|ingest) service="$a" ;; esac; done
    if has "differs-$service"; then echo "comparison status 1"
    elif has "unreadable-$service"; then echo "cmp: $name: Permission denied" >&2; echo "comparison status 2"
    elif cmp -s "$state/mounted/$name" "$call/stdin"; then echo "comparison status 0"
    else echo "comparison status 1"; fi
    exit 0 ;;
  exec)
    cat > "$call/stdin"
    if has exec-fails; then
      echo "020_role_passwords.sh: could not set the password of app_writer: stub, $(sed -n 3p "$call/stdin" | cut -f2-)" >&2
      exit 1
    fi
    has no-confirmation && exit 0
    echo "@CONFIRMATION@"
    exit 0 ;;
esac
echo "stub: unsupported docker compose $*" >&2
exit 2
'''


class Deployment:
    """A directory standing in for that of compose.yaml: the real
    set_passwords.sh and create_secrets.sh, the secret files, a stub docker."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "deployment"
        self.root.mkdir()
        for name in (SCRIPT.name, "create_secrets.sh"):
            shutil.copy2(ROOT / name, self.root / name)
        self.state = tmp_path / "state"
        (self.state / "calls").mkdir(parents=True)
        (self.state / "mounted").mkdir()
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(STUB_DOCKER.replace("@STATE@", str(self.state))
                                       .replace("@CONFIRMATION@", CONFIRMATION), encoding="utf-8")
        (bindir / "docker").chmod(0o755)
        self.env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
                    "LANG": os.environ.get("LANG", "C.UTF-8")}
        self.secrets = self.root / "secrets"
        self.write_secrets(self.secrets, {name: PASSWORDS[name].encode() + b"\n" for name in NAMES})

    def write_secrets(self, directory: Path, contents: Dict[str, bytes], mounted: bool = True) -> None:
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        for name, content in contents.items():
            path = directory / name
            if path.exists():
                path.chmod(0o644)
            path.write_bytes(content)
            path.chmod(0o444)
            if mounted:
                (self.state / "mounted" / name).write_bytes(content)

    def mode(self, mode: str) -> None:
        (self.state / "mode").write_text(mode, encoding="utf-8")

    def run(self, *args: str, cwd: Path = None, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.root / SCRIPT.name), *args], cwd=cwd or self.tmp,
                              env=dict(self.env, **extra), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60)

    def calls(self) -> List[Dict[str, object]]:
        result = []
        for directory in sorted((self.state / "calls").iterdir()):
            stdin = directory / "stdin"
            result.append({
                "argv": (directory / "argv").read_text(encoding="utf-8").splitlines(),
                "cwd": (directory / "cwd").read_text(encoding="utf-8").strip(),
                "env": (directory / "env").read_text(encoding="utf-8"),
                "stdin": stdin.read_bytes() if stdin.exists() else None,
            })
        return result

    def subcommands(self) -> List[str]:
        result = []
        for call in self.calls():
            argv = list(call["argv"])[1:]
            while argv and argv[0] in ("-p", "--project-name", "-f", "--file", "--env-file"):
                argv = argv[2:]
            result.append(argv[0])
        return result


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    return Deployment(tmp_path)


def _sent(passwords: Dict[str, str]) -> bytes:
    return "".join(f"{ROLES[name]}\t{passwords[name]}\n" for name in NAMES).encode("utf-8")


def test_it_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_it_checks_then_sends_the_three_passwords_to_the_database_container(deployment: Deployment) -> None:
    proc = deployment.run()
    assert proc.returncode == 0, proc.stderr
    assert deployment.subcommands() == ["ps", "run", "run", "exec"]
    ps, readonly, writer, exec_ = (list(call["argv"]) for call in deployment.calls())
    assert ps == ["compose", "ps", "--status", "running", "--quiet", "postgres"]
    script = 'cmp -s "$1" -; echo "comparison status $?"'
    assert readonly == ["compose", "run", "--rm", "-T", "--no-deps", "api", "sh", "-c", script, "sh",
                        "/run/secrets/app_readonly_password"]
    assert writer == ["compose", "run", "--rm", "-T", "--no-deps", "ingest", "sh", "-c", script, "sh",
                      "/run/secrets/app_writer_password"]
    assert exec_ == ["compose", "exec", "-T", "-u", "postgres", "postgres", INIT_SCRIPT, "--stdin"]
    calls = deployment.calls()
    # Each new container receives its own role's file, byte for byte, and no other.
    assert calls[1]["stdin"] == (deployment.secrets / "app_readonly_password").read_bytes()
    assert calls[2]["stdin"] == (deployment.secrets / "app_writer_password").read_bytes()
    assert calls[3]["stdin"] == _sent(PASSWORDS)
    for call in calls:
        assert call["cwd"] == str(deployment.root.resolve()), "docker compose runs in the directory of compose.yaml"
    assert CONFIRMATION in proc.stdout
    assert "docker compose restart api" in proc.stdout and "docker compose up --wait" in proc.stdout


def test_no_password_reaches_a_command_line_an_environment_or_the_output(deployment: Deployment) -> None:
    proc = deployment.run()
    assert proc.returncode == 0, proc.stderr
    for call in deployment.calls():
        for password in PASSWORDS.values():
            assert not any(password in arg for arg in call["argv"]), call["argv"]
            assert password not in str(call["env"])
    for password in PASSWORDS.values():
        assert password not in proc.stdout + proc.stderr


@pytest.mark.parametrize("content, expected", PASSWORD_SAMPLES)
def test_each_password_is_sent_as_the_containers_read_its_file(deployment: Deployment, content: bytes,
                                                                expected: str) -> None:
    deployment.write_secrets(deployment.secrets, {"app_writer_password": content})
    proc = deployment.run()
    assert proc.returncode == 0, proc.stderr
    assert deployment.calls()[3]["stdin"] == _sent(dict(PASSWORDS, app_writer_password=expected))
    assert expected not in proc.stdout + proc.stderr


def test_the_options_go_to_every_docker_compose_call(deployment: Deployment, tmp_path: Path) -> None:
    proc = deployment.run("-p", "pubdex-example", "-f", "compose.yaml", "-f", "compose.postgres-port.yaml",
                          "--env-file", "settings.env")
    assert proc.returncode == 0, proc.stderr
    options = ["-p", "pubdex-example", "-f", "compose.yaml", "-f", "compose.postgres-port.yaml",
               "--env-file", "settings.env"]
    for call in deployment.calls():
        assert list(call["argv"])[1:1 + len(options)] == options
    assert "docker compose -p pubdex-example -f compose.yaml -f compose.postgres-port.yaml --env-file " \
           "settings.env restart api" in proc.stdout


def test_the_secrets_directory_is_found_as_create_secrets_finds_it(deployment: Deployment,
                                                                    tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other = {name: f"other-{PASSWORDS[name]}" for name in NAMES}
    deployment.write_secrets(deployment.root / "private", {n: v.encode() + b"\n" for n, v in other.items()})
    proc = deployment.run(cwd=elsewhere, PEOPLE_PUBS_SECRETS_DIR="private")
    assert proc.returncode == 0, proc.stderr
    assert deployment.calls()[3]["stdin"] == _sent(other)
    assert str(deployment.root / "private") in proc.stdout


@pytest.mark.parametrize("content, problem", UNUSABLE_SAMPLES)
def test_an_unusable_file_stops_it_before_docker_is_asked_anything(deployment: Deployment, content: bytes,
                                                                    problem: str) -> None:
    deployment.write_secrets(deployment.secrets, {"app_readonly_password": content})
    proc = deployment.run()
    assert proc.returncode == 1
    assert "UNUSABLE  app_readonly_password" in proc.stderr and problem in proc.stderr
    assert "nothing was changed" in proc.stderr
    assert MARKER not in proc.stdout + proc.stderr
    assert deployment.calls() == []


def test_a_missing_file_a_shared_password_or_an_open_directory_stops_it(deployment: Deployment) -> None:
    (deployment.secrets / "app_writer_password").unlink()
    proc = deployment.run()
    assert proc.returncode == 1 and "MISSING   app_writer_password" in proc.stderr
    assert not (deployment.secrets / "app_writer_password").exists(), "it creates no file"
    deployment.write_secrets(deployment.secrets,
                             {"app_writer_password": PASSWORDS["app_readonly_password"].encode() + b"\n"})
    proc = deployment.run()
    assert proc.returncode == 1 and "hold the same password" in proc.stderr
    deployment.write_secrets(deployment.secrets, {"app_writer_password": PASSWORDS["app_writer_password"].encode()})
    deployment.secrets.chmod(0o750)
    proc = deployment.run()
    assert proc.returncode == 1 and "open to other users" in proc.stderr
    assert deployment.calls() == []


def test_it_needs_create_secrets_next_to_it(deployment: Deployment) -> None:
    (deployment.root / "create_secrets.sh").unlink()
    proc = deployment.run()
    assert proc.returncode == 1 and "create_secrets.sh, which checks the secret files, is not next to" in proc.stderr
    assert deployment.calls() == []


@pytest.mark.parametrize("mode, message", [
    ("postgres-stopped", "PostgreSQL of this Compose project is not running"),
    ("ps-fails", "PostgreSQL of this Compose project is not running"),
])
def test_a_database_that_is_not_running_stops_it(deployment: Deployment, mode: str, message: str) -> None:
    deployment.mode(mode)
    proc = deployment.run()
    assert proc.returncode == 1 and message in proc.stderr and "nothing was changed" in proc.stderr
    assert deployment.subcommands() == ["ps"]


def test_a_file_compose_takes_from_another_directory_stops_it(deployment: Deployment) -> None:
    # As with PEOPLE_PUBS_SECRETS_DIR in .env only: Compose mounts other files
    # than those this script reads.
    deployment.write_secrets(deployment.secrets, {"app_readonly_password": b"example-decoy-0b7e\n"},
                             mounted=False)
    proc = deployment.run()
    assert proc.returncode == 1
    assert "a new api container reads another password at /run/secrets/app_readonly_password" in proc.stderr
    assert "PEOPLE_PUBS_SECRETS_DIR=<directory> bash set_passwords.sh" in proc.stderr
    assert "nothing was changed" in proc.stderr
    assert "exec" not in deployment.subcommands()
    assert "example-decoy-0b7e" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("mode, message", [
    ("differs-ingest", "a new ingest container reads another password at /run/secrets/app_writer_password"),
    ("unreadable-api", "cannot compare /run/secrets/app_readonly_password of a new api container"),
    ("run-fails", "cannot compare /run/secrets/app_readonly_password of a new api container"),
])
def test_a_container_that_does_not_read_the_hosts_file_stops_it(deployment: Deployment, mode: str,
                                                                message: str) -> None:
    deployment.mode(mode)
    proc = deployment.run()
    assert proc.returncode == 1 and message in proc.stderr and "nothing was changed" in proc.stderr
    assert "exec" not in deployment.subcommands()


def test_a_failure_in_the_database_container_fails_it_with_the_output_masked(deployment: Deployment) -> None:
    deployment.mode("exec-fails")
    proc = deployment.run()
    assert proc.returncode == 1
    assert "could not set the password of app_writer: stub, <password>" in proc.stderr
    assert "the database did not confirm the new passwords (exit status 1)" in proc.stderr
    assert "run this script again once the cause is fixed" in proc.stderr
    assert PASSWORDS["app_writer_password"] not in proc.stdout + proc.stderr
    assert "restart api" not in proc.stdout


def test_a_run_without_the_confirmation_fails(deployment: Deployment) -> None:
    deployment.mode("no-confirmation")
    proc = deployment.run()
    assert proc.returncode == 1 and "did not confirm the new passwords (exit status 0)" in proc.stderr


@pytest.mark.parametrize("args, status, message", [
    (["--help"], 0, "usage: set_passwords.sh"),
    (["-p"], 2, "-p needs a value"),
    (["--env-file", ""], 2, "--env-file needs a value"),
    (["--resync"], 2, "unknown argument: --resync"),
    (["down"], 2, "unknown argument: down"),
])
def test_arguments(deployment: Deployment, args: List[str], status: int, message: str) -> None:
    proc = deployment.run(*args)
    assert proc.returncode == status
    assert message in (proc.stdout if status == 0 else proc.stderr)
    assert deployment.calls() == []


def test_the_files_keep_their_modes_and_content(deployment: Deployment) -> None:
    before = {name: ((deployment.secrets / name).read_bytes(), stat.S_IMODE((deployment.secrets / name).stat().st_mode))
              for name in NAMES}
    assert deployment.run().returncode == 0
    assert {name: ((deployment.secrets / name).read_bytes(),
                   stat.S_IMODE((deployment.secrets / name).stat().st_mode)) for name in NAMES} == before
