"""Lifecycle of run_operator_acceptance.sh, offline.

The real script runs from a scratch copy of the checkout layout, with the
real create_secrets.sh, set_passwords.sh and compose.yaml, against a stub
`docker` that keeps an inventory of the containers, networks and volumes it
was asked to create and logs every call, with the environment and working
directory of each `docker compose` call. Like Compose, the stub takes the
project's name, image, port and secrets directory from the .env file in that
working directory, and from nothing else.

That proves, without Docker, what the real run of stage 7 of run_ci_suite.sh
cannot show by passing: that the operator's directory holds a copy of
compose.yaml, create_secrets.sh and set_passwords.sh and nothing else of the
checkout -- no DB/init/, which the image carries -- and that its .env file
alone configures every Compose call -- no option names the project, files or
settings, and nothing of a polluted caller's environment or of the
checkout's own .env file reaches them; that a project which exists already
is never touched;
that no password reaches a command line or the output unmasked; and that a
failure, a signal, a second signal during the cleanup or a cleanup that
cannot be confirmed ends the run with the right status after removing
exactly what it created, the operator's directory, secret files and roster
included. The successful path itself runs against real Docker, as stage 7 of
run_ci_suite.sh.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "run_operator_acceptance.sh"
BASH = shutil.which("bash") or "/bin/bash"
SHA = "c0ffee" + "0" * 34
PROJECT_RE = r"pubdex-operator-\d{14}-\d+-[0-9a-f]{8}"
# What isolated() hands a command, plus what a shell adds on its own.
ISOLATED_ENVIRONMENT = {"PATH", "HOME", "LC_ALL", "PWD", "SHLVL", "_", "OLDPWD"}
DEPLOYMENT_FILES = ["compose.yaml", "create_secrets.sh", "set_passwords.sh"]
#: The PostgreSQL image compose.yaml names when PEOPLE_PUBS_POSTGRES_IMAGE is not set.
DEFAULT_POSTGRES = "ghcr.io/ccu-ukn/pubdex-postgres:0.1.0"

STUB_DOCKER = r'''#!/usr/bin/env bash
# Stand-in for the docker CLI: answers from the inventory in @STATE@, and
# reads a Compose project's settings from the .env file of its working
# directory, as Compose does.
set -u
state="@STATE@"
events="@EVENTS@"
mode="$(cat "$state/mode" 2>/dev/null || true)"
has() { case ",$mode," in *",$1,"*) return 0 ;; esac; return 1; }
{ printf 'docker'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$events"
mkdir -p "$state/resources" "$state/images"
option() {  # option <flag> <args...>: the value after the first <flag>
  local flag="$1" previous=""; shift
  for a in "$@"; do [ "$previous" = "$flag" ] && { printf '%s' "$a"; return; }; previous="$a"; done
}
listing() {  # listing <kind> <args...>: ids whose labels include the --filter label
  has list-fails && { echo "stub: cannot connect to the Docker daemon" >&2; exit 1; }
  local kind="$1" wanted f; shift
  wanted="$(option --filter "$@")"; wanted="${wanted#label=}"
  if has collision; then echo "foreign-$kind"; fi
  for f in "$state/resources/$kind".*; do
    [ -e "$f" ] && grep -qxF "$wanted" "$f" && echo "${f##*/$kind.}"
  done
  exit 0
}
key() { printf '%s' "$1" | tr '/:@' '___'; }
field() { sed -n "s/^$2=//p" "$state/images/$(key "$1")"; }
remove() {  # remove <kind> <id>
  has rm-ineffective && exit 0
  rm -f "$state/resources/$1.$2"; exit 0
}
last="${!#}"
case "$1" in
  info) exit 0 ;;
  version) cat "$state/engine_version" 2>/dev/null || echo 29.9.1; exit 0 ;;
  ps) listing container "$@" ;;
  volume) case "$2" in ls) listing volume "$@" ;; rm) remove volume "$last" ;; esac ;;
  network) case "$2" in ls) listing network "$@" ;; rm) remove network "$last" ;; esac ;;
  rm) remove container "$last" ;;
  image)
    case "$2" in
      inspect)
        [ -e "$state/images/$(key "$last")" ] || { echo "Error: No such image: $last" >&2; exit 1; }
        case "$(option --format "$@")" in
          "") echo "[]" ;;
          "{{.Id}}") field "$last" id ;;
          *image.version*) echo "$(field "$last" version), revision $(field "$last" revision)" ;;
        esac
        exit 0 ;;
      rm)
        has image-rm-fails && { echo "stub: image is in use" >&2; exit 1; }
        id="$(field "$last" id)"
        rm -f "$state/images/$(key "$last")" "$state/images/$(key "$id")"; exit 0 ;;
    esac ;;
  pull) echo "stub: never pulls" >&2; exit 1 ;;
  compose)
    shift
    if [ "$1" = version ]; then
      cat "$state/compose_version" 2>/dev/null || echo 5.6.0; exit 0
    fi
    { printf 'compose-env'; env | sort | while IFS= read -r line; do printf '\037%s' "$line"; done; printf '\n'; } >> "$events"
    printf 'compose-cwd\037%s\n' "$(pwd -P)" >> "$events"
    setting() { sed -n "s/^$1=//p" .env 2>/dev/null; }
    project="$(setting COMPOSE_PROJECT_NAME)"; image="$(setting PEOPLE_PUBS_IMAGE)"; port="$(setting PEOPLE_PUBS_HTTP_PORT)"
    postgres_image="$(setting PEOPLE_PUBS_POSTGRES_IMAGE)"
    secrets="$(setting PEOPLE_PUBS_SECRETS_DIR)"
    case "$secrets" in /*) ;; *) secrets="$(cd "$secrets" 2>/dev/null && pwd -P)" ;; esac
    cp .env "$state/last.env" 2>/dev/null
    while [ "${1:-}" = --profile ]; do shift 2; done
    case "$1" in
      config)
        rm -rf "$state/directory-seen"; cp -rp . "$state/directory-seen"
        name="$project"; has config-redirected && name=db
        leak=""; has config-leak && leak="$(cat "$secrets/app_writer_password")"
        files=""
        for secret in postgres_password app_readonly_password app_writer_password; do
          files="$files${files:+, }\"$secret\": {\"file\": \"$secrets/$secret\"}"
        done
        cat <<JSON
{"name": "$name", "services": {
 "bootstrap": {"image": "$image", "network_mode": "none"},
 "postgres": {"image": "$postgres_image", "environment": {"X": "$leak"}},
 "api": {"image": "$image", "ports": [{"host_ip": "127.0.0.1", "published": "$port", "target": 8000}],
         "environment": {"PEOPLE_PUBS_API_LOG_LEVEL": "WARNING"}}},
 "secrets": {$files},
 "networks": {"default": {"name": "${project}_default"}}}
JSON
        exit 0 ;;
      up)
        rm -rf "$state/secrets-seen"; cp -r "$secrets" "$state/secrets-seen"
        has up-fails && { echo "stub: up failed; the writer password is $(cat "$secrets/app_writer_password")"; exit 1; }
        for resource in container.api container.postgres volume.data network.default; do
          printf '%s\n' "com.docker.compose.project=$project" > "$state/resources/${resource%%.*}.$project-${resource#*.}"
        done
        if has up-hangs; then for _ in $(seq 300); do sleep 0.1; done; fi
        exit 0 ;;
      logs)
        has leaky-logs && echo "postgres-1  | the superuser password is $(cat "$secrets/postgres_password")"
        echo "postgres-1  | stub log without an initialisation"; exit 0 ;;
      ps) echo "NAME  STATUS"; exit 0 ;;
      down)
        has down-slow && sleep 2
        has down-fails && exit 1
        has down-ineffective && exit 0
        for f in "$state/resources/"*; do
          [ -e "$f" ] && grep -qxF "com.docker.compose.project=$project" "$f" && rm -f "$f"
        done
        exit 0 ;;
    esac
    exit 0 ;;
esac
echo "stub: unsupported docker $*" >&2
exit 2
'''

STUB_GIT = '#!/bin/sh\ncase "$*" in *rev-parse*) echo "@SHA@" ;; esac\nexit 0\n'

STUB_BUILD = r'''#!/usr/bin/env bash
state="@STATE@"
{ printf 'build'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "@EVENTS@"
case ",$(cat "$state/mode" 2>/dev/null)," in *,build-fails,*) echo "stub: the image build failed"; exit 1 ;; esac
key() { printf '%s' "$1" | tr '/:@' '___'; }
mkdir -p "$state/images"
# build_image.sh --tag <application tag> --postgres-tag <PostgreSQL tag>
[ "$1 $3" = "--tag --postgres-tag" ] || { echo "stub build: unexpected arguments $*" >&2; exit 2; }
printf 'id=sha256:built\nversion=0.1.0\nrevision=@SHA@\n' > "$state/images/$(key "$2")"
cp "$state/images/$(key "$2")" "$state/images/$(key sha256:built)"
printf 'id=sha256:built-postgres\nversion=0.1.0\nrevision=@SHA@\n' > "$state/images/$(key "$4")"
cp "$state/images/$(key "$4")" "$state/images/$(key sha256:built-postgres)"
echo "Built image sha256:built"
'''


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _finished_lines(path: Path) -> list:
    """The lines the stubs have finished writing. The tests poll the log while
    the run goes on, and a stub writes a line in several parts, so the last
    line may still lack its newline."""
    data = path.read_bytes()
    return data[:data.rfind(b"\n") + 1].decode("utf-8").splitlines()


class Fake:
    """A scratch checkout holding the real script, its real helpers and deployment files, and stubs."""

    def __init__(self, tmp_path: Path, mode: str = "") -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        self.script = self.root / SCRIPT.name
        for relative in [SCRIPT.name, "VERSION", "DB/lib/disposable_postgres.sh", "DB/lib/docker_versions.sh",
                         "DB/lib/compose_acceptance.sh", "DB/lib/release_identity.sh", *DEPLOYMENT_FILES]:
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, self.root / relative)
        self.state = tmp_path / "state"
        (self.state / "images").mkdir(parents=True)
        self.events = tmp_path / "events.log"
        self.workdirs = tmp_path / "tmp"
        self.workdirs.mkdir()
        bindir = tmp_path / "bin"
        values = {"@STATE@": str(self.state), "@EVENTS@": str(self.events), "@SHA@": SHA}

        def fill(text: str) -> str:
            for placeholder, value in values.items():
                text = text.replace(placeholder, value)
            return text

        _executable(bindir / "docker", fill(STUB_DOCKER))
        _executable(bindir / "git", fill(STUB_GIT))
        _executable(self.root / "build_image.sh", fill(STUB_BUILD))
        self.mode(mode)
        # Docker has the PostgreSQL image compose.yaml names.
        self.add_image(DEFAULT_POSTGRES, image_id="sha256:postgres-given")
        home = tmp_path / "home"
        home.mkdir()
        self.env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home),
                    "TMPDIR": str(self.workdirs), "LANG": "C.UTF-8"}

    def mode(self, mode: str) -> None:
        (self.state / "mode").write_text(mode, encoding="utf-8")

    def setting(self, name: str, value: str) -> None:
        (self.state / name).write_text(value, encoding="utf-8")

    def add_image(self, reference: str, image_id: str = "sha256:given") -> None:
        key = re.sub(r"[/:@]", "_", reference)
        record = f"id={image_id}\nversion=0.1.0\nrevision={SHA}\n"
        (self.state / "images" / key).write_text(record, encoding="utf-8")
        (self.state / "images" / re.sub(r"[/:@]", "_", image_id)).write_text(record, encoding="utf-8")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.script), *args], cwd=self.tmp, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=120)

    def popen(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen([BASH, str(self.script), *args], cwd=self.tmp, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)

    def calls(self, kind: str = "docker") -> list:
        if not self.events.exists():
            return []
        lines = _finished_lines(self.events)
        return [line.split("\x1f")[1:] for line in lines if line.split("\x1f")[0] == kind]

    def compose_calls(self) -> list:
        return [call[1:] for call in self.calls() if call[:1] == ["compose"] and call[1:2] != ["version"]]

    def subcommands(self) -> list:
        result = []
        for call in self.compose_calls():
            while call[:1] == ["--profile"]:
                call = call[2:]
            result.append(call[0])
        return result

    def resources(self) -> list:
        return sorted(path.name for path in (self.state / "resources").glob("*")) if (
            self.state / "resources").exists() else []

    def images(self) -> list:
        """The stub's images but the PostgreSQL image Docker had before the run."""
        before = {re.sub(r"[/:@]", "_", DEFAULT_POSTGRES), "sha256_postgres-given"}
        return sorted(path.name for path in (self.state / "images").iterdir() if path.name not in before)

    def assert_nothing_left(self) -> None:
        assert self.resources() == [], "every container, network and volume of the run is gone"
        assert list(self.workdirs.iterdir()) == [], \
            "the run's temporary directory, with the operator's directory, secrets and roster, is gone"

    def wait_for(self, subcommand: str, proc: subprocess.Popen) -> None:
        """Until the run has called that Compose subcommand; for up, until the
        call has also created the project's resources."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if subcommand in self.subcommands() and (subcommand != "up" or len(self.resources()) == 4):
                return
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        pytest.fail(f"the run never reached docker compose {subcommand}")


def _project(fake: Fake) -> str:
    settings = dict(line.split("=", 1) for line in (fake.state / "last.env").read_text(encoding="utf-8").splitlines())
    project = settings["COMPOSE_PROJECT_NAME"]
    assert re.fullmatch(PROJECT_RE, project), project
    return project


@pytest.mark.parametrize(
    "args, env, message",
    [
        (["--image"], {}, "--image needs a value"),
        (["--pull"], {}, "unknown argument: --pull"),
        ([], {"STACK_WAIT_SECONDS": "soon"}, "STACK_WAIT_SECONDS must be a positive number"),
    ],
)
def test_invalid_arguments_are_usage_errors_and_touch_nothing(tmp_path: Path, args: list, env: dict, message: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(*args, **env)
    assert proc.returncode == 2 and message in proc.stderr and "usage: run_operator_acceptance.sh" in proc.stderr
    assert fake.calls() == [], "no docker call before the arguments are valid"


def test_help_runs_nothing(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run("--help")
    assert proc.returncode == 0 and proc.stdout.startswith("usage: run_operator_acceptance.sh")
    assert fake.calls() == []


@pytest.mark.parametrize("setup, message", [
    ({"compose_version": "2.23.3"}, "Docker Compose 2.23.3 is too old; the deployment needs 2.24.0 or later"),
    ({"engine_version": "27.5.1"}, "Docker Engine 27.5.1 is too old; the deployment needs Docker Engine 28.0.1 or "
                                   "later, except 28.2.0 to 28.3.2, to build its image as to run it"),
    ({"engine_version": "28.2.0"}, "Docker Engine 28.2.0 is one of the releases in which a firewalld reload opens "
                                   "ports published on 127.0.0.1 to other hosts (CVE-2025-54388)"),
])
def test_missing_prerequisites_stop_the_run_before_anything_exists(tmp_path: Path, setup: dict, message: str) -> None:
    fake = Fake(tmp_path)
    for name, value in setup.items():
        fake.setting(name, value)
    proc = fake.run()
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert fake.calls("build") == [] and fake.compose_calls() == []
    assert list(fake.workdirs.iterdir()) == []


@pytest.mark.parametrize("engine", ["28.0.1", "28.3.3", "29.9.2"])
def test_supported_engines_go_on_to_start_the_stack(tmp_path: Path, engine: str) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.setting("engine_version", engine)
    proc = fake.run()
    assert "Docker Engine" not in proc.stderr, proc.stderr
    assert "up" in fake.subcommands(), "the run goes on to start the stack"


def test_a_project_that_exists_already_is_never_touched(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "collision")
    proc = fake.run()
    assert proc.returncode == 1 and "exist already; not touching them" in proc.stderr
    removals = [call for call in fake.calls() if call[:1] == ["rm"] or call[1:2] == ["rm"]]
    assert removals == [] and fake.compose_calls() == [] and fake.calls("build") == []
    assert list(fake.workdirs.iterdir()) == []


def test_a_given_image_docker_lacks_is_never_built_or_pulled(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run("--image", "registry.example.org/team/pubdex:0.1.0")
    assert proc.returncode == 1
    assert "Docker has no image registry.example.org/team/pubdex:0.1.0; pull it first" in proc.stderr
    assert fake.calls("build") == [] and not any(call[:1] == ["pull"] for call in fake.calls())
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


POLLUTION = {
    "COMPOSE_PROJECT_NAME": "db", "COMPOSE_FILE": "/elsewhere/compose.yaml", "COMPOSE_PROFILES": "demo",
    "COMPOSE_ENV_FILES": "/elsewhere/.env", "PEOPLE_PUBS_IMAGE": "elsewhere/pubdex:latest",
    "PEOPLE_PUBS_SECRETS_DIR": "/elsewhere/secrets", "PEOPLE_PUBS_HTTP_PORT": "80", "PEOPLE_DB_DSN":
    "postgresql://elsewhere/people_db", "PGPASSWORD": "example-inherited-password",
    "BASH_FUNC_docker%%": "() { echo hijacked; }",
}


def test_the_env_file_of_a_copy_of_the_deployment_configures_every_compose_call(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    # The checkout's own .env, override file and secrets, which Compose would
    # read there, and which an operator's directory must not take over.
    (fake.root / ".env").write_text("COMPOSE_PROJECT_NAME=db\n", encoding="utf-8")
    (fake.root / "compose.override.yaml").write_text("services: {}\n", encoding="utf-8")
    (fake.root / "secrets").mkdir()
    proc = fake.run(**POLLUTION, DOCKER_HOST="unix:///run/stub-docker.sock")
    assert proc.returncode == 1 and "docker compose up --wait did not see a healthy stack" in proc.stderr
    assert fake.calls(), "the exported function did not replace docker"
    project = _project(fake)
    assert fake.subcommands()[:2] == ["config", "up"]
    for call in fake.compose_calls():
        assert not {"--project-name", "-p", "--file", "-f", "--env-file", "--project-directory"} & set(call), call
    directories = {Path(call[0]) for call in fake.calls("compose-cwd")}
    assert len(directories) == 1
    directory = directories.pop()
    assert directory.name == project and directory.parent.parent == fake.workdirs.resolve(), directory
    for environment in fake.calls("compose-env"):
        names = {item.split("=", 1)[0] for item in environment}
        assert names <= ISOLATED_ENVIRONMENT | {"DOCKER_HOST"}, sorted(names)
        assert "DOCKER_HOST=unix:///run/stub-docker.sock" in environment, "the daemon selection is kept"
    settings = (fake.state / "last.env").read_text(encoding="utf-8").splitlines()
    assert [line.split("=", 1)[0] for line in settings] == [
        "COMPOSE_PROJECT_NAME", "PEOPLE_PUBS_IMAGE", "PEOPLE_PUBS_POSTGRES_IMAGE", "PEOPLE_PUBS_SECRETS_DIR",
        "PEOPLE_PUBS_HTTP_PORT", "PEOPLE_PUBS_API_LOG_LEVEL"]
    assert settings[1] == f"PEOPLE_PUBS_IMAGE=pubdex-operator:{project.removeprefix('pubdex-operator-')}"
    assert settings[2] == ("PEOPLE_PUBS_POSTGRES_IMAGE=pubdex-operator-postgres:"
                           + project.removeprefix('pubdex-operator-'))
    assert settings[3] == "PEOPLE_PUBS_SECRETS_DIR=../operator-secrets"
    assert re.fullmatch(r"PEOPLE_PUBS_HTTP_PORT=[1-9]\d{0,4}", settings[4]) and settings[4] != "PEOPLE_PUBS_HTTP_PORT=8000"
    assert settings[5] == "PEOPLE_PUBS_API_LOG_LEVEL=WARNING"
    seen = fake.state / "directory-seen"
    assert sorted(str(path.relative_to(seen)) for path in seen.rglob("*") if path.is_file()) == sorted(
        [*DEPLOYMENT_FILES, ".env"]), "the operator's directory: the deployment files and the .env file, nothing else"
    for relative in DEPLOYMENT_FILES:
        assert (seen / relative).read_bytes() == (ROOT / relative).read_bytes(), relative
        assert (seen / relative).stat().st_mode == (ROOT / relative).stat().st_mode, relative
    secrets = fake.state / "secrets-seen"
    assert sorted(path.name for path in secrets.iterdir()) == [
        "app_readonly_password", "app_writer_password", "postgres_password"], \
        "create_secrets.sh wrote the files where the .env file's relative PEOPLE_PUBS_SECRETS_DIR points"
    assert "ok  create_secrets.sh, given the .env file's PEOPLE_PUBS_SECRETS_DIR" in proc.stdout
    assert "ok  Compose renders the .env file's project, images, port 127.0.0.1:" in proc.stdout
    fake.assert_nothing_left()
    assert fake.images() == [], "the run's image tags are removed"


def test_no_password_reaches_a_command_line_or_the_output_unmasked(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    proc = fake.run()
    seen = fake.state / "secrets-seen"
    secrets = [(seen / name).read_text(encoding="utf-8").strip()
               for name in ("postgres_password", "app_readonly_password", "app_writer_password")]
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in secrets) and len(set(secrets)) == 3
    log = fake.events.read_text(encoding="utf-8")
    for value in secrets:
        assert value not in log, "no docker, compose or build command line or environment holds a password"
        assert value not in proc.stdout + proc.stderr
    assert "the writer password is <password>" in proc.stderr, "Docker's output is shown masked"
    fake.assert_nothing_left()


@pytest.mark.parametrize("mode, message", [
    ("config-leak", "a password occurs in the rendered configuration"),
    ("config-redirected", "the rendered configuration is not the one the .env file configures"),
])
def test_a_rendered_configuration_that_is_not_safe_stops_the_run_before_the_start(tmp_path: Path, mode: str,
                                                                                    message: str) -> None:
    fake = Fake(tmp_path, mode)
    proc = fake.run()
    assert proc.returncode == 1 and message in proc.stderr
    assert "up" not in fake.subcommands()
    fake.assert_nothing_left()


def test_a_failure_after_the_start_removes_the_project_and_the_operators_directory(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "leaky-logs")
    proc = fake.run()
    assert proc.returncode == 1
    assert "did not bootstrap the schema the image carries and provision the role passwords" in proc.stderr
    assert "the superuser password is <password>" in proc.stderr
    assert re.search(r"the superuser password is [0-9a-f]{64}", proc.stdout + proc.stderr) is None
    [down] = [call for call in fake.compose_calls() if "down" in call]
    assert down == ["--profile", "ingest", "--profile", "demo", "down", "--volumes", "--remove-orphans",
                    "--timeout", "5"]
    assert "cleanup:" not in proc.stdout, "no success message after a failure"
    fake.assert_nothing_left()
    assert fake.images() == []


def test_what_compose_leaves_behind_is_removed_by_the_project_name(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "leaky-logs,down-fails")
    proc = fake.run()
    assert proc.returncode == 1
    project = _project(fake)
    removed = {call[-1] for call in fake.calls() if call[:1] == ["rm"] or call[1:2] == ["rm"]}
    assert {f"{project}-api", f"{project}-postgres", f"{project}-data", f"{project}-default"} <= removed
    fake.assert_nothing_left()


@pytest.mark.parametrize("mode, warning", [
    ("leaky-logs,down-ineffective,rm-ineffective", "docker ps -a --filter label=com.docker.compose.project="),
    ("leaky-logs,image-rm-fails", "docker image ls pubdex-operator:"),
], ids=["resources", "image-tag"])
def test_a_cleanup_that_cannot_be_confirmed_names_the_leftovers(tmp_path: Path, mode: str, warning: str) -> None:
    fake = Fake(tmp_path, mode)
    proc = fake.run()
    assert proc.returncode == 1, "a failed run keeps its status"
    assert "could not confirm that run" in proc.stderr and warning in proc.stderr
    assert "docker network ls --filter label=com.docker.compose.project=pubdex-operator-" in proc.stderr
    assert list(fake.workdirs.iterdir()) == [], "the secret files and roster go even so"


def test_a_failed_build_stops_the_run(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "build-fails")
    proc = fake.run()
    assert proc.returncode == 1 and "build_image.sh failed" in proc.stderr
    assert "stub: the image build failed" in proc.stderr
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


@pytest.mark.parametrize("signum, status", [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)],
                         ids=["INT", "TERM", "HUP"])
def test_a_signal_during_the_start_cleans_up_with_its_own_status(tmp_path: Path, signum: int, status: int) -> None:
    fake = Fake(tmp_path, "up-hangs")
    proc = fake.popen()
    fake.wait_for("up", proc)
    os.killpg(proc.pid, signum)  # as a terminal or a timeout delivers it: to the whole group
    proc.communicate(timeout=60)
    assert proc.returncode == status
    assert "down" in fake.subcommands()
    fake.assert_nothing_left()
    assert fake.images() == []


def test_a_second_interrupt_during_the_cleanup_does_not_cut_it_short(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-hangs,down-slow")
    proc = fake.popen()
    fake.wait_for("up", proc)
    os.killpg(proc.pid, signal.SIGINT)
    fake.wait_for("down", proc)
    os.killpg(proc.pid, signal.SIGINT)
    proc.communicate(timeout=60)
    assert proc.returncode == 130
    fake.assert_nothing_left()


def test_a_signal_with_a_cleanup_that_fails_keeps_the_signal_status(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-hangs,down-ineffective,rm-ineffective")
    proc = fake.popen()
    fake.wait_for("up", proc)
    os.killpg(proc.pid, signal.SIGTERM)
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 143 and "could not confirm that run" in err


def _backup_and_restore_commands() -> list:
    """The docker compose commands of DEPLOYMENT.md, "Backup and restore", as
    the run types them: without the redirections, which differ in their
    files, and with the verification query by the name the run gives it."""
    text = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    section = text.split("\n### Backup and restore\n", 1)[1].split("\n## ", 1)[0]
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", section, re.S):
        for line in block.splitlines():
            line = re.sub(r"\s+#.*$", "", line).strip()
            line = re.sub(r"^\(umask 077 && (.*)\)$", r"\1", line)
            if line.startswith("docker compose "):
                commands.append(re.split(r" [<>] ", line)[0].removeprefix("docker compose "))
    return commands


def test_the_documented_backup_and_restore_is_what_the_run_types() -> None:
    commands = _backup_and_restore_commands()
    assert len(commands) == 8, commands
    script = SCRIPT.read_text(encoding="utf-8").replace(' --wait-timeout "$WAIT_SECONDS"', "")
    query = re.fullmatch(r"(exec -T postgres psql -U postgres -d people_db -XAtc) '([^']+)'", commands[-1])
    assert query, commands[-1]
    assert f"COUNTS_QUERY='{query.group(2)}'" in script, "the run's verification query is the documented one"
    commands[-1] = f'{query.group(1)} "$COUNTS_QUERY"'
    # wait_healthy() is the documented `docker compose up --wait`.
    script = script.replace("wait_healthy up-restored.log", "dc up --wait")
    step = script.split("# --- 8. backup and restore", 1)[1].split("# --- 9.", 1)[0]
    for command in commands:
        assert f"dc {command}" in step, f"step 8 does not run the documented docker compose {command}"
    assert "( umask 077 && bounded 300 dc exec -T postgres pg_dump" in step, "the dump is written under umask 077"
    assert '< "$DUMP"' in step, "pg_restore reads the dump on its standard input"


def test_the_operator_acceptance_files_are_in_this_repository() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert (ROOT / "DB" / "lib" / "compose_acceptance.sh").is_file()
