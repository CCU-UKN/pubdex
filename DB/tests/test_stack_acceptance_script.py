"""Lifecycle of run_stack_acceptance.sh, offline.

The real script runs from a scratch copy of the checkout layout, with the
real create_secrets.sh and disposable-database library, against a stub
`docker` that keeps an inventory of the containers, networks, volumes and
images it was asked to create and logs every call -- with the environment of
each `docker compose` call -- and against stub `git` and build_image.sh.
`docker compose` runs in a cleared environment, so the stubs take their
paths and modes from files rather than from variables.

That proves, without Docker, what the real stack stage of run_ci_suite.sh
cannot show by passing: that a project or image tag which exists already is
never touched; that a prebuilt image is never rebuilt or replaced, and is
checked for its digest and revision; that the run starts a compose.yaml alone
in a project directory of its own -- this checkout's, or with --release the
one the image carries, whatever the checkout holds -- and refuses an image
that carries none, a build that carries another one and a release whose file
names another version or runs no schema upgrade; that it refuses an image
without schema changes and a build that carries other ones than the
checkout's DB/schema/; that the rendered configuration gives the superuser's
password file to PostgreSQL and the schema upgrade alone and starts nothing
that uses the database before the schema upgrade; that every Compose call is bound to the run's
project, project directory, Compose files and environment file and receives
nothing of a polluted caller's environment; that no password reaches a
command line or the output unmasked; and that a failure, a signal, a second
signal during the cleanup or a cleanup that cannot be confirmed ends the run
with the right status after removing exactly what it created. The successful
path itself runs against real Docker, as stage 6 of run_ci_suite.sh.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "run_stack_acceptance.sh"
BASH = shutil.which("bash") or "/bin/bash"
REAL_RM = shutil.which("rm") or "/bin/rm"
SHA = "c0ffee" + "0" * 34
COMMIT_B = "b" * 40
RUN_LABEL = "pubdex.stack-acceptance.run"
#: The PostgreSQL image compose.yaml names when PEOPLE_PUBS_POSTGRES_IMAGE is not set.
DEFAULT_POSTGRES = "ghcr.io/ccu-ukn/pubdex-postgres:0.1.0"
RELEASE_REF_FOR_USAGE = "registry.example.org/team/pubdex:0.1.0@sha256:" + "a" * 64
PROJECT_RE = r"pubdex-acceptance-\d{14}-\d+-[0-9a-f]{8}"
#: The schema changes of this checkout as the image of a build lists them: number|name|SHA-256.
SCHEMA_FILES = sorted((ROOT / "DB" / "schema").glob("*.sql"))
SCHEMA_LISTING = "".join(f"{int(path.name[:4])}|{path.stem}|{hashlib.sha256(path.read_bytes()).hexdigest()}\n"
                         for path in SCHEMA_FILES)
SCHEMA_CHANGES = f"{len(SCHEMA_FILES)} schema change" + ("" if len(SCHEMA_FILES) == 1 else "s")
# What isolated() hands a command, plus what a shell adds on its own.
ISOLATED_ENVIRONMENT = {"PATH", "HOME", "LC_ALL", "PWD", "SHLVL", "_"}

STUB_DOCKER = r'''#!/usr/bin/env bash
# Stand-in for the docker CLI: answers from the inventory in @STATE@.
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
  if has collision; then case "$wanted" in com.docker.compose.project=*) echo "foreign-$kind" ;; esac; fi
  for f in "$state/resources/$kind".*; do
    [ -e "$f" ] && grep -qxF "$wanted" "$f" && echo "${f##*/$kind.}"
  done
  exit 0
}
key() { printf '%s' "$1" | tr '/:@' '___'; }
field() { sed -n "s/^$2=//p" "$state/images/$(key "$1")"; }
remove() {  # remove <kind> <id>
  has rm-fails && { echo "stub: removal failed" >&2; exit 1; }
  has rm-ineffective && exit 0
  rm -f "$state/resources/$1.$2"; exit 0
}
last="${!#}"
case "$1" in
  info) has no-daemon && exit 1; exit 0 ;;
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
          *image.version*) field "$last" version ;;
          *image.revision*) field "$last" revision ;;
          *transform-version*) echo 1.5.2 ;;
          *postgresql-version*) field "$last" postgresql ;;
          *Architecture*) echo linux/amd64 ;;
          *RepoDigests*'\n'*) field "$last" digests | tr ' ' '\n' ;;
          *RepoDigests*) field "$last" digests ;;
        esac
        exit 0 ;;
      rm)
        has image-rm-fails && { echo "stub: image is in use" >&2; exit 1; }
        id="$(field "$last" id)"
        rm -f "$state/images/$(key "$last")" "$state/images/$(key "$id")"; exit 0 ;;
    esac ;;
  run)
    # To read the compose.yaml the image carries, and to check the image's
    # files as its user finds them, with a program on standard input.
    case "$(option --entrypoint "$@")" in
      cat)
        [ "$last" = /opt/pubdex/compose.yaml ] || { echo "stub: unexpected docker run $*" >&2; exit 2; }
        has image-without-compose && { echo "cat: /opt/pubdex/compose.yaml: No such file or directory" >&2; exit 1; }
        cat "$state/image_compose"; exit 0 ;;
      python)
        [ "$last" = - ] && [ "$(option --user "$@")" = 10001:10001 ] \
          || { echo "stub: unexpected docker run $*" >&2; exit 2; }
        program="$(cat)"
        case "$program" in
          *hashlib*)
            # The schema changes the image carries, number|name|SHA-256.
            printf '%s\n' "$program" > "$state/image-schema-program"
            has image-without-schema && exit 0
            cat "$state/image_schema"; exit 0 ;;
          *importlib.metadata*)
            printf '%s\n' "$program" > "$state/image-packages-program"
            # An image the test laid out as a file system of its own, with a
            # Python whose only packages are that file system's: the check
            # program the script sent runs there, given that root.
            if [ -d "$state/image-root" ]; then
              printf '%s\n' "$program" | "$state/image-root/usr/local/bin/python" -I - "$state/image-root"
              exit $?
            fi
            echo "16 packages of the runtime lock at their locked versions, and from the base image packaging 26.3"
            exit 0 ;;
        esac
        printf '%s\n' "$program" > "$state/image-files-program"
        if has image-files-wrong; then
          echo "/opt/pubdex/init/020_role_passwords.sh: -rwxrwxr-x 0:0"
          echo "12 directories and 75 files under /opt/pubdex"
          exit 1
        fi
        echo "12 directories and 75 files under /opt/pubdex"; exit 0 ;;
    esac
    echo "stub: unexpected docker run $*" >&2; exit 2 ;;
  pull)
    has pull-fails && { echo "stub: pull failed" >&2; exit 1; }
    digests=""
    case "$last" in *@sha256:*) digests="${last%%:*}@${last##*@}" ;; esac
    has wrong-digest && digests="${last%%:*}@sha256:$(printf 'f%.0s' $(seq 64))"
    id=sha256:pulled
    case "$last" in *pubdex-postgres*) id=sha256:pulled-postgres ;; esac
    printf 'id=%s\nversion=%s\nrevision=%s\ndigests=%s\npostgresql=16.15\n' "$id" \
      "$(cat "$state/pull_version" 2>/dev/null || echo 0.1.0)" \
      "$(cat "$state/pull_revision" 2>/dev/null || echo unknown)" \
      "$digests" > "$state/images/$(key "$last")"
    cp "$state/images/$(key "$last")" "$state/images/$(key "$id")"
    exit 0 ;;
  compose)
    shift
    project=""; envfile=""; directory=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --project-name) project="$2"; shift 2 ;;
        --env-file) envfile="$2"; shift 2 ;;
        --project-directory) directory="$2"; shift 2 ;;
        --file|--profile) shift 2 ;;
        version)
          has no-compose && { echo "docker: 'compose' is not a docker command." >&2; exit 1; }
          cat "$state/compose_version" 2>/dev/null || echo 5.6.0; exit 0 ;;
        *) break ;;
      esac
    done
    { printf 'compose-env'; env | sort | while IFS= read -r line; do printf '\037%s' "$line"; done; printf '\n'; } >> "$events"
    setting() { sed -n "s/^$1=//p" "$envfile"; }
    run="$(setting PEOPLE_PUBS_ACCEPTANCE_RUN)"; secrets="$(setting PEOPLE_PUBS_SECRETS_DIR)"; image="$(setting PEOPLE_PUBS_IMAGE)"
    postgres_image="$(setting PEOPLE_PUBS_POSTGRES_IMAGE)"; has config-other-postgres && postgres_image=postgres:16
    cp "$envfile" "$state/last.env" 2>/dev/null
    case "$1" in
      config)
        rm -rf "$state/directory-seen"; cp -rp "$directory" "$state/directory-seen"
        name="$project"; has config-redirected && name=db
        leak=""; has config-leak && leak="$(cat "$secrets/postgres_password")"
        labels="{\"$RUN_LABEL\": \"$run\"}"
        secret() { local s="" n; for n in "$@"; do s="$s${s:+, }{\"source\": \"$n\"}"; done; printf '[%s]' "$s"; }
        api_secrets="$(secret app_readonly_password)"
        has config-extra-credential && api_secrets="$(secret app_readonly_password postgres_password)"
        after='{"postgres": {"condition": "service_healthy"}, "schema-upgrade": {"condition": "service_completed_successfully"}}'
        api_after="$after"
        has config-no-upgrade-dependency && api_after='{"postgres": {"condition": "service_healthy"}}'
        cat <<JSON
{"name": "$name", "services": {
 "bootstrap": {"image": "$image", "labels": $labels, "network_mode": "none"},
 "postgres": {"image": "$postgres_image", "labels": $labels, "networks": {"default": null}, "environment": {"X": "$leak"},
              "secrets": $(secret postgres_password app_readonly_password app_writer_password)},
 "schema-upgrade": {"image": "$image", "labels": $labels, "networks": {"default": null}, "read_only": true,
                    "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "secrets": $(secret postgres_password),
                    "depends_on": {"postgres": {"condition": "service_healthy"}}},
 "api": {"image": "$image", "labels": $labels, "networks": {"default": null, "published": null},
         "secrets": $api_secrets, "depends_on": $api_after},
 "ingest": {"image": "$image", "labels": $labels, "networks": {"default": null}, "secrets": $(secret app_writer_password),
            "depends_on": $after},
 "demo": {"image": "$image", "labels": $labels, "networks": {"default": null}, "secrets": $(secret app_writer_password),
          "depends_on": $after}},
 "networks": {"default": {"internal": true}, "published": {}}}
JSON
        exit 0 ;;
      up)
        cp -r "$secrets" "$state/secrets-seen"
        cp "$directory/compose.yaml" "$state/compose-started.yaml"
        has up-fails && { echo "stub: up failed; the writer password is $(cat "$secrets/app_writer_password")"; exit 1; }
        for resource in container.api container.postgres volume.data network.default network.published; do
          printf '%s\n' "com.docker.compose.project=$project" "$RUN_LABEL=$run" \
            > "$state/resources/${resource%%.*}.$project-${resource#*.}"
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
'''.replace("$RUN_LABEL", RUN_LABEL)

# Stand-in for git: answers the read-only questions a build is checked with
# from @STATE@ -- the checkout's commit, its status and the package modules
# it ignores -- and logs each call with its environment.
STUB_GIT = r'''#!/bin/sh
state="@STATE@"
{ printf 'git'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "@EVENTS@"
{ printf 'git-env'; env | sort | while IFS= read -r line; do printf '\037%s' "$line"; done; printf '\n'; } >> "@EVENTS@"
if [ -e "$state/git_absent" ]; then
  echo "fatal: not a git repository (or any of the parent directories): .git" >&2; exit 128
fi
case "$*" in
  *rev-parse*)
    if [ -e "$state/git_head" ]; then cat "$state/git_head"; else echo "@SHA@"; fi ;;
  *status*)
    if [ -e "$state/git_status_fails" ]; then echo "fatal: index file corrupt" >&2; exit 128; fi
    cat "$state/git_status" 2>/dev/null ;;
  *"ls-files --others --ignored --exclude-standard"*) cat "$state/git_ignored" 2>/dev/null ;;
esac
exit 0
'''

STUB_BUILD = r'''#!/usr/bin/env bash
state="@STATE@"
events="@EVENTS@"
{ printf 'build'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$events"
{ printf 'build-env'; env | sort | while IFS= read -r line; do printf '\037%s' "$line"; done; printf '\n'; } >> "$events"
case ",$(cat "$state/mode" 2>/dev/null)," in *,build-fails,*) echo "stub: the image build failed"; exit 1 ;; esac
key() { printf '%s' "$1" | tr '/:@' '___'; }
mkdir -p "$state/images"
# build_image.sh --tag <application tag> --postgres-tag <PostgreSQL tag>
[ "$1 $3" = "--tag --postgres-tag" ] || { echo "stub build: unexpected arguments $*" >&2; exit 2; }
printf 'id=sha256:built\nversion=%s\nrevision=%s\ndigests=\n' "$(cat "$state/build_version" 2>/dev/null || echo 0.1.0)" \
  "$(cat "$state/build_revision" 2>/dev/null || echo "@SHA@")" > "$state/images/$(key "$2")"
cp "$state/images/$(key "$2")" "$state/images/$(key sha256:built)"
printf 'id=sha256:built-postgres\nversion=%s\nrevision=%s\ndigests=\npostgresql=16.15\n' \
  "$(cat "$state/build_version" 2>/dev/null || echo 0.1.0)" \
  "$(cat "$state/build_postgres_revision" 2>/dev/null || cat "$state/build_revision" 2>/dev/null || echo "@SHA@")" \
  > "$state/images/$(key "$4")"
cp "$state/images/$(key "$4")" "$state/images/$(key sha256:built-postgres)"
echo "Built image sha256:built"
echo "Built PostgreSQL image sha256:built-postgres"
'''

# Stands in for rm: refuses to remove a directory directly below @TMP@ -- the
# run's temporary directory -- once @STATE@/rm_refused exists, and behaves as
# rm otherwise.
STUB_RM = r'''#!/bin/sh
if [ -e "@STATE@/rm_refused" ]; then
  for arg in "$@"; do
    case "$arg" in
      "@TMP@"/*/*) ;;
      "@TMP@"/*) echo "rm: cannot remove $arg: Operation not permitted" >&2; exit 1 ;;
    esac
  done
fi
exec "@REAL_RM@" "$@"
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
    """A scratch checkout holding the real script, its real helpers and stubs."""

    def __init__(self, tmp_path: Path, mode: str = "") -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        self.script = self.root / SCRIPT.name
        for relative in (SCRIPT.name, "create_secrets.sh", "compose.yaml", "VERSION", "DB/lib/disposable_postgres.sh",
                         "DB/lib/docker_versions.sh", "DB/lib/release_identity.sh", "DB/lib/compose_acceptance.sh",
                         "DB/tests/compose.acceptance.yaml"):
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, self.root / relative)
        # The image-label and reference cases below use a fixed synthetic
        # release, independently of the checkout's current version.
        (self.root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (self.root / "compose.yaml").write_text(
            (ROOT / "compose.yaml").read_text(encoding="utf-8").replace(
                (ROOT / "VERSION").read_text(encoding="utf-8").strip(), "0.1.0"), encoding="utf-8")
        self.state = tmp_path / "state"
        (self.state / "images").mkdir(parents=True)
        # The compose.yaml every image of the stub carries: this checkout's,
        # and its schema changes.
        shutil.copy2(self.root / "compose.yaml", self.state / "image_compose")
        shutil.copytree(ROOT / "DB" / "schema", self.root / "DB" / "schema",
                        ignore=lambda directory, names: [name for name in names if not name.endswith(".sql")])
        (self.state / "image_schema").write_text(SCHEMA_LISTING, encoding="utf-8")
        self.events = tmp_path / "events.log"
        self.workdirs = tmp_path / "tmp"
        self.workdirs.mkdir()
        bindir = tmp_path / "bin"
        values = {"@STATE@": str(self.state), "@EVENTS@": str(self.events), "@SHA@": SHA,
                  "@TMP@": str(self.workdirs), "@REAL_RM@": REAL_RM}

        def fill(text: str) -> str:
            for placeholder, value in values.items():
                text = text.replace(placeholder, value)
            return text

        _executable(bindir / "docker", fill(STUB_DOCKER))
        _executable(bindir / "git", fill(STUB_GIT))
        _executable(bindir / "rm", fill(STUB_RM))
        _executable(self.root / "build_image.sh", fill(STUB_BUILD))
        self.mode(mode)
        # Docker has the PostgreSQL image compose.yaml names, as after a
        # release build or a pull of it.
        self.add_postgres_image()
        home = tmp_path / "home"
        home.mkdir()
        self.env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home),
                    "TMPDIR": str(self.workdirs), "LANG": "C.UTF-8"}

    def mode(self, mode: str) -> None:
        (self.state / "mode").write_text(mode, encoding="utf-8")

    def image_compose(self, text: str) -> None:
        """The compose.yaml the images carry, instead of this checkout's."""
        (self.state / "image_compose").write_text(text, encoding="utf-8")

    def started_compose(self) -> str:
        return (self.state / "compose-started.yaml").read_text(encoding="utf-8")

    def setting(self, name: str, value: str) -> None:
        (self.state / name).write_text(value, encoding="utf-8")

    def add_image(self, reference: str, revision: str = SHA, digests: str = "", version: str = "0.1.0",
                  image_id: str = "sha256:given", postgresql: str = "") -> None:
        key = re.sub(r"[/:@]", "_", reference)
        record = f"id={image_id}\nversion={version}\nrevision={revision}\ndigests={digests}\npostgresql={postgresql}\n"
        (self.state / "images" / key).write_text(record, encoding="utf-8")
        (self.state / "images" / re.sub(r"[/:@]", "_", image_id)).write_text(record, encoding="utf-8")

    def add_postgres_image(self, reference: str = DEFAULT_POSTGRES, revision: str = SHA, digests: str = "",
                           version: str = "0.1.0") -> None:
        """A PubDex PostgreSQL image, by default the one compose.yaml names."""
        self.add_image(reference, revision, digests, version, image_id="sha256:postgres-given", postgresql="16.15")

    def image_root(self, lock: dict, installed: dict, files: tuple = ()) -> Path:
        """The file system the package check then runs in, instead of the
        stub's fixed answer: a runtime lock pinning `lock`, a Python without
        pip whose distributions are `installed`, and the empty `files`.
        Returns that Python's site-packages directory. The Python is a
        virtual environment of the host's: it shares the standard library,
        none of the host's packages."""
        root = self.state / "image-root"
        venv.create(root / "usr/local", with_pip=False, symlinks=True)
        [site] = (root / "usr/local/lib").glob("python3*/site-packages")
        (root / "opt/pubdex").mkdir(parents=True)
        (root / "opt/pubdex/requirements.txt").write_text("#\n# A synthetic runtime lock.\n#\n" + "".join(
            f"{name}=={version} \\\n    --hash=sha256:{'0' * 64}\n    # via -r requirements.in\n"
            for name, version in lock.items()), encoding="utf-8")
        for name, version in installed.items():
            (site / f"{name.replace('-', '_')}-{version}.dist-info").mkdir()
            (site / f"{name.replace('-', '_')}-{version}.dist-info" / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
        for relative in files:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).touch()
        return site

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
            rest = call[call.index("--env-file") + 2:]
            while rest[:1] == ["--profile"]:
                rest = rest[2:]
            result.append(rest[0])
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
        assert list(self.workdirs.iterdir()) == [], "the run's temporary directory and its secrets are gone"

    def wait_for(self, subcommand: str, proc: subprocess.Popen) -> None:
        """Until the run has called that Compose subcommand; for up, until the
        call has also created the project's resources."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if subcommand in self.subcommands() and (subcommand != "up" or len(self.resources()) == 5):
                return
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        pytest.fail(f"the run never reached docker compose {subcommand}")


def _project(fake: Fake) -> str:
    projects = {call[call.index("--project-name") + 1] for call in fake.compose_calls()}
    assert len(projects) == 1, projects
    project = projects.pop()
    assert re.fullmatch(PROJECT_RE, project), project
    return project


@pytest.mark.parametrize(
    "args, env, message",
    [
        (["--pull"], {}, "--pull needs --image"),
        (["--expect-revision", SHA], {}, "--expect-revision needs --image"),
        (["--image"], {}, "--image needs a value"),
        (["--bogus"], {}, "unknown argument: --bogus"),
        ([], {"STACK_WAIT_SECONDS": "soon"}, "STACK_WAIT_SECONDS must be a positive number"),
    ],
)
def test_invalid_arguments_are_usage_errors_and_touch_nothing(tmp_path: Path, args: list, env: dict, message: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(*args, **env)
    assert proc.returncode == 2 and message in proc.stderr and "usage: run_stack_acceptance.sh" in proc.stderr
    assert fake.calls() == [], "no docker call before the arguments are valid"


def test_help_runs_nothing(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run("--help")
    assert proc.returncode == 0 and proc.stdout.startswith("usage: run_stack_acceptance.sh")
    assert fake.calls() == []


@pytest.mark.parametrize(
    "setup, args, message",
    [
        ({"mode": "no-daemon"}, [], "cannot reach the Docker daemon"),
        ({"mode": "no-compose"}, [], "the Docker Compose plugin was not found"),
        ({"compose_version": "2.17.3"}, [], "Docker Compose 2.17.3 is too old; the deployment needs 2.24.0 or later"),
        # Rejects the start_interval of compose.yaml.
        ({"compose_version": "2.20.1"}, [], "Docker Compose 2.20.1 is too old; the deployment needs 2.24.0 or later"),
        # Accepts start_interval but leaves it out of the containers.
        ({"compose_version": "2.20.2"}, [], "Docker Compose 2.20.2 is too old"),
        ({"compose_version": "2.23.3"}, [], "Docker Compose 2.23.3 is too old"),
        ({"compose_version": "2.24.0-rc.1"}, [], "Docker Compose 2.24.0-rc.1 is too old"),
        ({"compose_version": "v1.29.2"}, [], "Docker Compose v1.29.2 is too old"),
        ({"compose_version": "dev"}, [], "Docker Compose reports the version 'dev', which is not a release number"),
        # An Engine before 28.0.1 starts the stack but lets other hosts reach
        # its loopback port, for a build or an image alike.
        ({"engine_version": "27.5.1"}, [], "Docker Engine 27.5.1 is too old; the deployment needs Docker Engine "
                                           "28.0.1 or later, except 28.2.0 to 28.3.2, to build its image as to run it"),
        ({"engine_version": "25.0.5"}, ["--image", "pubdex:given"], "Docker Engine 25.0.5 is too old"),
        ({"engine_version": "28.0.0"}, [], "Docker Engine 28.0.0 is too old"),
        # A firewalld reload removes the filter (CVE-2025-54388).
        ({"engine_version": "28.3.2"}, ["--image", "pubdex:given"],
         "Docker Engine 28.3.2 is one of the releases in which a firewalld reload opens ports published on "
         "127.0.0.1 to other hosts (CVE-2025-54388); the deployment needs Docker Engine 28.0.1 or later"),
        ({"engine_version": "dev"}, [], "Docker Engine reports the version 'dev'"),
    ],
    ids=["no-daemon", "no-compose-plugin", "old-compose", "compose-without-start-interval",
         "compose-ignoring-start-interval", "last-compose-ignoring-start-interval", "compose-pre-release",
         "compose-v1", "compose-unknown-version", "old-engine-for-a-build", "old-engine-for-an-image",
         "engine-28.0.0", "engine-losing-the-filter-on-a-firewalld-reload", "engine-unknown-version"],
)
def test_missing_prerequisites_stop_the_run_before_anything_exists(tmp_path: Path, setup: dict, args: list,
                                                                   message: str) -> None:
    fake = Fake(tmp_path, setup.pop("mode", ""))
    for name, value in setup.items():
        fake.setting(name, value)
    proc = fake.run(*args)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    if "plugin" in message:
        assert "Install the Compose plugin, version 2.24.0 or later" in proc.stderr, "the error says what to install"
    elif "Compose" in message:
        assert "Upgrade the Compose plugin" in proc.stderr, "the error says what to do"
    else:
        assert "Upgrade the Engine" in proc.stderr or "Docker daemon" in proc.stderr, "the error says what to do"
    assert fake.calls("build") == [] and fake.compose_calls() == []
    assert not any(call[:1] in (["rm"], ["pull"]) for call in fake.calls())
    assert list(fake.workdirs.iterdir()) == []


@pytest.mark.parametrize("version", ["2.24.0", "v2.24.0", "2.24.6+ds1-0ubuntu1~22.04.1", "2.24.1-rc.1", "5.6.0"])
def test_compose_from_the_first_release_applying_start_interval_is_accepted(tmp_path: Path, version: str) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.setting("compose_version", version)
    fake.add_image("pubdex:given")
    proc = fake.run("--image", "pubdex:given")
    assert "Docker Compose" not in proc.stderr, proc.stderr
    assert "up" in fake.subcommands(), "the run goes on to start the stack"


@pytest.mark.parametrize("args", [[], ["--image", "pubdex:given"]], ids=["build", "image"])
@pytest.mark.parametrize("engine", ["28.0.1", "28.0.4", "28.3.3", "29.9.2"])
def test_supported_engines_build_and_run_the_deployment(tmp_path: Path, args: list, engine: str) -> None:
    # The first supported release, GitHub's ubuntu-24.04 runner, the fix of
    # CVE-2025-54388 and a current release: one requirement for both.
    fake = Fake(tmp_path, "up-fails")
    fake.setting("engine_version", engine)
    fake.add_image("pubdex:given")
    proc = fake.run(*args)
    assert "too old" not in proc.stderr and "up" in fake.subcommands()
    assert len(fake.calls("build")) == (0 if args else 1)


def test_a_project_that_exists_already_is_never_touched(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "collision")
    proc = fake.run()
    assert proc.returncode == 1 and "exist already; not touching them" in proc.stderr
    removals = [call for call in fake.calls() if call[:1] == ["rm"] or call[1:2] == ["rm"]]
    assert removals == [] and fake.compose_calls() == [] and fake.calls("build") == []
    assert list(fake.workdirs.iterdir()) == []


def test_a_daemon_that_cannot_list_resources_stops_the_run(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "list-fails")
    proc = fake.run()
    assert proc.returncode == 1 and "Docker cannot list the resources" in proc.stderr
    assert fake.compose_calls() == [] and fake.calls("build") == []


def test_a_prebuilt_image_is_never_built_or_substituted(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    reference = "registry.example.org/team/pubdex:0.1.0"
    proc = fake.run("--image", reference)
    assert proc.returncode == 1
    assert f"Docker has no image {reference}; pull it first, or pass --pull. Nothing is built" in proc.stderr
    assert fake.calls("build") == [] and not any(call[:1] == ["pull"] for call in fake.calls())
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


def test_a_published_image_is_pulled_by_its_reference_and_checked_for_its_digest(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    digest = "sha256:" + "a" * 64
    reference = f"registry.example.org/team/pubdex:0.1.0@{digest}"
    fake.setting("pull_revision", SHA)
    proc = fake.run("--image", reference, "--pull", "--expect-revision", SHA)
    assert ["pull", reference] in fake.calls(), "the reference is pulled exactly as given"
    assert "ok  the image carries the requested digest" in proc.stdout
    assert f"ok  the image's revision label is {SHA}" in proc.stdout
    assert f"PEOPLE_PUBS_IMAGE={reference}\n" in (fake.state / "last.env").read_text(encoding="utf-8")
    assert fake.calls("build") == []
    assert ["image", "rm", reference] not in fake.calls(), "an image the run was given stays"
    fake.assert_nothing_left()


@pytest.mark.parametrize(
    "mode, args, message",
    [
        ("wrong-digest", ["--pull"], "does not carry the digest"),
        ("", ["--pull", "--expect-revision", "0" * 40], f"the image's revision label is '{SHA}', expected '" + "0" * 40),
        ("", ["--pull", "--expect-version", "0.2.0"], "the image's release-version label is '0.1.0', expected '0.2.0'"),
        ("pull-fails", ["--pull"], "docker pull registry.example.org/team/pubdex:0.1.0@sha256:"),
    ],
    ids=["digest", "revision", "version", "pull"],
)
def test_a_published_image_that_is_not_the_one_asked_for_is_refused(tmp_path: Path, mode: str, args: list,
                                                                    message: str) -> None:
    fake = Fake(tmp_path, mode)
    fake.setting("pull_revision", SHA)
    proc = fake.run("--image", "registry.example.org/team/pubdex:0.1.0@sha256:" + "a" * 64, *args)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert "up" not in fake.subcommands() and fake.calls("build") == []
    fake.assert_nothing_left()


@pytest.mark.parametrize(
    "args, message",
    [
        (["--expect-version", "0.1.0"], "--expect-version needs --image"),
        (["--image", "pubdex:given", "--expect-version", "v0.1.0"], "--expect-version needs a release version such as 0.1.0"),
        (["--image", "pubdex:given", "--expect-version", "0.1"], "--expect-version needs a release version"),
        (["--image", "pubdex:given", "--expect-revision", "c0ffee"], "--expect-revision needs a full commit id"),
        (["--image", "pubdex:given", "--expect-revision", SHA.upper()], "--expect-revision needs a full commit id"),
        (["--image", "pubdex:given", "--expect-revision", "unknown"], "--expect-revision needs a full commit id"),
        (["--image", "pubdex:given", "--expect-revision", SHA + "-dirty-dirty"], "--expect-revision needs a full commit id"),
        (["--release", "--expect-version", "0.1.0", "--expect-revision", SHA], "--release needs --image"),
        (["--release", "--image", "pubdex:given", "--expect-revision", SHA],
         "--release needs --expect-version <version>: a release version is never taken from a tag"),
        (["--release", "--image", "pubdex:given", "--expect-version", "0.1.0"], "--release needs --expect-revision"),
        (["--release", "--image", "pubdex:given", "--expect-version", "0.1.0", "--expect-revision", SHA + "-dirty"],
         "a -dirty revision is that of a local build"),
        (["--release", "--image", "registry.example.org/team/pubdex:0.1.0", "--pull", "--expect-version", "0.1.0",
          "--expect-revision", SHA], "--release with --pull needs a reference with its digest"),
        (["--release", "--image", "pubdex:0.2.0", "--expect-version", "0.1.0", "--expect-revision", SHA],
         "--image names the version tag 0.2.0, but --expect-version is 0.1.0"),
        (["--postgres-image", "pubdex-postgres:local"], "--postgres-image needs --image"),
        (["--image", "pubdex:given", "--postgres-image"], "--postgres-image needs a value"),
        (["--release", "--image", RELEASE_REF_FOR_USAGE, "--pull", "--expect-version", "0.1.0", "--expect-revision", SHA],
         "--release with --pull needs --postgres-image with its digest"),
        (["--release", "--image", RELEASE_REF_FOR_USAGE, "--pull", "--postgres-image",
          "registry.example.org/team/pubdex-postgres:0.1.0", "--expect-version", "0.1.0", "--expect-revision", SHA],
         "--release with --pull needs --postgres-image with its digest"),
        (["--release", "--image", "pubdex:0.1.0", "--postgres-image", "pubdex-postgres:0.2.0",
          "--expect-version", "0.1.0", "--expect-revision", SHA],
         "--postgres-image names the version tag 0.2.0, but --expect-version is 0.1.0"),
    ],
    ids=["version-without-image", "version-v-prefix", "version-two-part", "revision-abbreviated",
         "revision-upper-case", "revision-unknown", "revision-twice-dirty", "release-without-image",
         "release-without-version", "release-without-revision", "release-dirty-revision",
         "release-pull-without-digest", "release-tag-of-another-version", "postgres-without-image",
         "postgres-without-value", "release-pull-without-postgres", "release-pull-postgres-without-digest",
         "release-postgres-tag-of-another-version"],
)
def test_identity_options_that_cannot_be_checked_are_usage_errors(tmp_path: Path, args: list, message: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(*args)
    assert proc.returncode == 2 and message in proc.stderr, proc.stderr
    assert fake.calls() == [] and fake.calls("git") == [], "nothing is asked of Docker or Git"


@pytest.mark.parametrize(
    "version, revision, args, message",
    [
        ("", SHA, ["--expect-version", "0.1.0"],
         "the image has no release-version label (org.opencontainers.image.version)"),
        ("unknown", SHA, ["--expect-version", "0.1.0"],
         "the image's release-version label is 'unknown': it was not built with build_image.sh"),
        ("0.1", SHA, ["--expect-version", "0.1.0"], "the image's release-version label '0.1' is not a release version"),
        ("0.2.0", SHA, ["--expect-version", "0.1.0"], "the image's release-version label is '0.2.0', expected '0.1.0'"),
        ("0.1.0", "", ["--expect-revision", SHA], "the image has no revision label (org.opencontainers.image.revision)"),
        ("0.1.0", "unknown", ["--expect-revision", SHA],
         "the image's revision label is 'unknown': it was built outside a Git checkout or without build_image.sh"),
        ("0.1.0", "main", ["--expect-revision", SHA], "the image's revision label 'main' is not a commit id"),
        ("0.1.0", SHA + "-dirty", ["--expect-revision", SHA],
         f"the image was built from a working tree of {SHA} with changes (revision label {SHA}-dirty)"),
        ("0.1.0", COMMIT_B, ["--expect-revision", SHA], f"the image's revision label is '{COMMIT_B}', expected '{SHA}'"),
    ],
    ids=["version-missing", "version-unknown", "version-malformed", "version-other", "revision-missing",
         "revision-unknown", "revision-malformed", "revision-local-build", "revision-other"],
)
def test_a_given_image_without_the_required_labels_is_refused_before_anything_starts(
        tmp_path: Path, version: str, revision: str, args: list, message: str) -> None:
    fake = Fake(tmp_path)
    fake.add_image("pubdex:given", revision=revision, version=version)
    proc = fake.run("--image", "pubdex:given", *args)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert fake.compose_calls() == [] and fake.calls("build") == []
    assert ["image", "rm", "pubdex:given"] not in fake.calls(), "an image the run was given stays"
    fake.assert_nothing_left()


def test_a_given_image_with_the_required_version_and_revision_goes_on_to_start(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.add_image("pubdex:given")
    proc = fake.run("--image", "pubdex:given", "--expect-version", "0.1.0", "--expect-revision", SHA)
    assert "ok  the image's release-version label is 0.1.0" in proc.stdout
    assert f"ok  the image's revision label is {SHA}" in proc.stdout
    assert "ok  the run starts this checkout's compose.yaml, which the image carries unchanged" in proc.stdout
    assert "note:" not in proc.stdout
    assert fake.subcommands()[:2] == ["config", "up"]


def test_a_local_build_is_tested_under_its_honest_dirty_revision(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.add_image("pubdex:local", revision=SHA + "-dirty")
    fake.add_postgres_image("pubdex-postgres:local", revision=SHA + "-dirty")
    proc = fake.run("--image", "pubdex:local", "--postgres-image", "pubdex-postgres:local",
                    "--expect-version", "0.1.0", "--expect-revision", SHA + "-dirty")
    assert f"ok  the image's revision label is {SHA}-dirty" in proc.stdout
    assert f"ok  the PostgreSQL image's revision label is {SHA}-dirty" in proc.stdout
    assert "up" in fake.subcommands(), "a development run tests the images it is given"
    assert "PEOPLE_PUBS_POSTGRES_IMAGE=pubdex-postgres:local\n" in (fake.state / "last.env").read_text()


def test_a_postgresql_image_of_another_revision_is_refused_before_the_start(tmp_path: Path) -> None:
    # The images of one deployment are of one commit: a PostgreSQL image of
    # another, or a dirty build of the one required, is not tested with it.
    for revision, message in ((COMMIT_B, f"the PostgreSQL image's revision label is '{COMMIT_B}', expected '{SHA}'"),
                              (SHA + "-dirty", f"the PostgreSQL image was built from a working tree of {SHA}")):
        fake = Fake(tmp_path / revision[-6:], "up-fails")
        fake.add_image("pubdex:given")
        fake.add_postgres_image(revision=revision)
        proc = fake.run("--image", "pubdex:given", "--expect-revision", SHA)
        assert proc.returncode == 1 and message in proc.stderr, proc.stderr
        assert fake.compose_calls() == []
        fake.assert_nothing_left()


def test_a_postgresql_image_that_is_not_pubdexs_is_refused_before_the_start(tmp_path: Path) -> None:
    # The official postgres image, say, which parses XML: it carries no
    # pubdex.postgresql-version label.
    fake = Fake(tmp_path, "up-fails")
    fake.add_image("pubdex:given")
    fake.add_image("postgres:16", image_id="sha256:official")
    proc = fake.run("--image", "pubdex:given", "--postgres-image", "postgres:16")
    assert proc.returncode == 1 and "it is not a PubDex PostgreSQL image" in proc.stderr, proc.stderr
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


def test_a_missing_postgresql_image_is_never_built_or_pulled_in_its_place(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.add_image("pubdex:given")
    proc = fake.run("--image", "pubdex:given", "--postgres-image", "pubdex-postgres:absent")
    assert proc.returncode == 1 and "Docker has no image pubdex-postgres:absent" in proc.stderr, proc.stderr
    assert fake.calls("build") == [] and not any(call[:1] == ["pull"] for call in fake.calls())
    fake.assert_nothing_left()


def test_a_configuration_that_starts_another_postgresql_image_stops_the_run(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "config-other-postgres")
    fake.add_image("pubdex:given")
    proc = fake.run("--image", "pubdex:given")
    assert proc.returncode == 1 and "the rendered configuration is not the run's own" in proc.stderr, proc.stderr
    assert "up" not in fake.subcommands()
    fake.assert_nothing_left()


def test_a_development_run_starts_this_checkouts_compose_file_and_says_when_the_image_carries_another(
        tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.image_compose("# the compose.yaml of another commit\n")
    fake.add_image("pubdex:given")
    proc = fake.run("--image", "pubdex:given", "--expect-revision", SHA)
    assert "note: the run starts this checkout's compose.yaml, which differs from the one the image carries" in proc.stdout
    assert "(--release starts the image's)" in proc.stdout
    assert fake.started_compose() == (fake.root / "compose.yaml").read_text(encoding="utf-8")
    assert fake.calls("git") == [], "only a build is checked against the checkout's commit"


def test_an_image_that_carries_no_compose_file_is_refused_before_the_start(tmp_path: Path) -> None:
    # An image of an earlier model, which took the schema and compose.yaml
    # from a checkout: the bootstrap service could not run from it.
    fake = Fake(tmp_path, "image-without-compose")
    fake.add_image("pubdex:old")
    proc = fake.run("--image", "pubdex:old")
    assert proc.returncode == 1
    assert "the image carries no /opt/pubdex/compose.yaml: it predates this deployment" in proc.stderr
    assert "No such file or directory" in proc.stderr
    assert fake.compose_calls() == []
    [run] = [call for call in fake.calls() if call[:1] == ["run"]]
    assert run[1:8] == ["--rm", "--network", "none", "--label", run[5], "--entrypoint", "cat"]
    assert run[5].startswith("pubdex.stack-acceptance.run=") and run[-2:] == ["sha256:given", "/opt/pubdex/compose.yaml"]
    fake.assert_nothing_left()


def test_an_image_without_schema_changes_is_refused_before_the_start(tmp_path: Path) -> None:
    # Such as 0.1.0's: compose.yaml would run a schema upgrade the image lacks.
    fake = Fake(tmp_path, "image-without-schema")
    fake.add_image("pubdex:candidate")
    proc = fake.run("--image", "pubdex:candidate")
    assert proc.returncode == 1
    assert ("the image carries no schema change in /opt/pubdex/schema: it predates the schema upgrade that "
            "compose.yaml runs") in proc.stderr
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


def test_the_images_schema_changes_are_listed_in_a_container_of_the_run_without_network(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.add_image("pubdex:candidate")
    proc = fake.run("--image", "pubdex:candidate")
    assert f"ok  the image carries {SCHEMA_CHANGES} in /opt/pubdex/schema, the last {SCHEMA_FILES[-1].stem}\n" in proc.stdout
    assert ", exactly this checkout's DB/schema/" not in proc.stdout, "a given image is not compared with the checkout"
    assert '"/opt/pubdex/schema"' in (fake.state / "image-schema-program").read_text(encoding="utf-8")
    runs = [call for call in fake.calls() if call[:1] == ["run"] and call[-1] == "-"]
    assert len(runs) == 3, "the files, the packages and the schema changes, each in a container of its own"
    for call in runs:
        assert call[call.index("--network") + 1] == "none" and call[call.index("--user") + 1] == "10001:10001"
        assert call[call.index("--label") + 1].startswith(f"{RUN_LABEL}=") and "--rm" in call
    fake.assert_nothing_left()


def test_a_build_that_carries_other_schema_changes_than_this_checkouts_is_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.setting("image_schema", SCHEMA_LISTING.replace(SCHEMA_FILES[-1].stem, SCHEMA_FILES[-1].stem + "_edited"))
    proc = fake.run()
    assert proc.returncode == 1
    assert "the image built from this checkout does not carry exactly the schema changes of its DB/schema/" in proc.stderr
    assert fake.compose_calls() == []
    fake.assert_nothing_left()
    assert fake.images() == []


def test_the_images_files_are_checked_as_its_user_finds_them(tmp_path: Path) -> None:
    # Whichever builder made the image: user 10001, in a container without
    # network, reads and enters everything under /opt/pubdex, root's, 755 and
    # 644, and may write nothing else but the bootstrap volume's mount point
    # and the temporary directories.
    fake = Fake(tmp_path, "up-fails")
    fake.add_image("pubdex:given")
    proc = fake.run("--image", "pubdex:given")
    assert ("ok  the image's 12 directories and 75 files under /opt/pubdex are root's, 755 and 644: user 10001 "
            "reads and enters them and writes none") in proc.stdout
    check = next(call for call in fake.calls() if call[:1] == ["run"] and "python" in call)
    assert check[1:5] == ["--rm", "-i", "--network", "none"]
    assert check[check.index("--user") + 1] == "10001:10001" and check[-4:] == ["python", "sha256:given", "-I", "-"]
    program = (fake.state / "image-files-program").read_text(encoding="utf-8")
    assert 'os.walk("/opt/pubdex")' in program and "(info.st_uid, info.st_gid) != (0, 0)" in program
    assert '["/run/lock", "/tmp", "/var/lib/pubdex/initdb", "/var/tmp"]' in program
    assert fake.subcommands()[:2] == ["config", "up"], "the check comes before the start"


def test_an_image_whose_files_are_not_as_the_dockerfile_sets_them_is_refused_before_the_start(
        tmp_path: Path) -> None:
    fake = Fake(tmp_path, "image-files-wrong")
    fake.add_image("pubdex:given")
    proc = fake.run("--image", "pubdex:given")
    assert proc.returncode == 1
    assert "/opt/pubdex/init/020_role_passwords.sh: -rwxrwxr-x 0:0" in proc.stderr
    assert "FAIL: the image's files are not root's with modes 755 and 644" in proc.stderr
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


#: The runtime lock of the package check's synthetic images.
RUNTIME_LOCK = {"anyio": "4.14.2", "psycopg-c": "3.3.6", "starlette": "1.7.0"}
PACKAGES_REFUSED = "FAIL: the image's Python packages are not those of its runtime lock, or it holds pip or a wheel"


def test_the_images_packages_are_its_runtime_lock_without_pip(tmp_path: Path) -> None:
    # The image has no pip, so the standard library reads the installed
    # distributions' metadata, as user 10001 and without network, before the
    # stack starts. The program the script sends runs here on a synthetic
    # image's file system, whose Python holds the lock's packages at their
    # versions and two of the base image's.
    fake = Fake(tmp_path, "up-fails")
    fake.add_image("pubdex:given")
    fake.image_root(RUNTIME_LOCK, dict(RUNTIME_LOCK, packaging="26.3", wheel="0.46.3"),
                    ("usr/local/bin/python3.11-config", "usr/bin/pipe", "usr/lib/pipewire/client.conf"))
    proc = fake.run("--image", "pubdex:given")
    assert ("ok  the image holds the 3 packages of the runtime lock at their locked versions, and from the base "
            "image packaging 26.3, wheel 0.46.3; its Python cannot import pip, and no file in it is named as pip, "
            "its scripts, its distribution or a wheel") in proc.stdout
    checks = [call for call in fake.calls() if call[:1] == ["run"] and "python" in call]
    assert len(checks) == 3, "the files, then the packages, then the schema changes"
    assert checks[1][1:5] == ["--rm", "-i", "--network", "none"]
    assert checks[1][checks[1].index("--user") + 1] == "10001:10001"
    assert checks[1][-4:] == ["python", "sha256:given", "-I", "-"], "in the container the program checks its root"
    program = (fake.state / "image-packages-program").read_text(encoding="utf-8")
    assert "pip check" not in program and "-m pip" not in program, "nothing in the check runs pip"
    assert fake.subcommands()[:2] == ["config", "up"], "the check comes before the start"


@pytest.mark.parametrize(
    "installed, problem",
    [
        ({"anyio": "4.14.2", "starlette": "1.7.0"}, "psycopg-c: not installed, locked 3.3.6"),
        (dict(RUNTIME_LOCK, anyio="4.14.1"), "anyio: 4.14.1, locked 4.14.2"),
    ],
    ids=["missing", "other-version"],
)
def test_a_package_missing_or_at_another_version_is_refused_before_the_start(
        tmp_path: Path, installed: dict, problem: str) -> None:
    fake = Fake(tmp_path)
    fake.add_image("pubdex:given")
    fake.image_root(RUNTIME_LOCK, installed)
    proc = fake.run("--image", "pubdex:given")
    assert proc.returncode == 1
    assert problem in proc.stderr.splitlines()
    assert "1 of the 3 packages of the runtime lock missing or at another version" in proc.stderr.splitlines()
    assert "at their locked versions" not in proc.stdout + proc.stderr
    assert PACKAGES_REFUSED in proc.stderr
    assert fake.compose_calls() == [], "nothing started"
    fake.assert_nothing_left()


def test_a_runtime_lock_that_pins_nothing_is_refused_before_the_start(tmp_path: Path) -> None:
    # A lock the check cannot read a package from would otherwise pass it.
    fake = Fake(tmp_path)
    fake.add_image("pubdex:given")
    fake.image_root({}, {"packaging": "26.3"})
    proc = fake.run("--image", "pubdex:given")
    assert proc.returncode == 1
    assert f"{fake.state}/image-root/opt/pubdex/requirements.txt pins no package" in proc.stderr.splitlines()
    assert PACKAGES_REFUSED in proc.stderr
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


def test_an_image_that_holds_pip_or_a_wheel_is_refused_before_the_start(tmp_path: Path) -> None:
    # pip importable, its distribution, its scripts and the wheels ensurepip
    # installs from, and any other wheel: each is named; files whose names
    # only resemble them, and anything under /proc, /sys and /dev, are not.
    fake = Fake(tmp_path)
    fake.add_image("pubdex:given")
    named = ("usr/local/bin/pip", "usr/local/bin/pip3", "usr/local/bin/pip3.11",
             "usr/local/lib/python3.11/ensurepip/_bundled/pip-24.0-py3-none-any.whl",
             "usr/local/lib/python3.11/ensurepip/_bundled/setuptools-79.0.1-py3-none-any.whl",
             "srv/any-1.0-py3-none-any.whl")
    unnamed = ("usr/bin/pipe", "usr/lib/pipewire/client.conf", "usr/local/lib/python3.11/pipes.py",
               "usr/local/bin/pydoc3.11", "usr/local/bin/wheel", "opt/pip_audit/README",
               "proc/1/root/usr/bin/pip3", "sys/pip", "dev/pip-1.0-py3-none-any.whl")
    site = fake.image_root(RUNTIME_LOCK, dict(RUNTIME_LOCK, pip="26.2.2", wheel="0.46.3"), named + unnamed)
    (site / "pip").mkdir()
    (site / "pip" / "__init__.py").touch()
    root = fake.state / "image-root"
    proc = fake.run("--image", "pubdex:given")
    assert proc.returncode == 1
    lines = proc.stderr.splitlines()
    assert f"pip can be imported: {site}/pip/__init__.py" in lines
    reported = {Path(line) for line in lines if line.startswith(str(root))}
    assert reported == {root / path for path in named} | {site / "pip", site / "pip-26.2.2.dist-info"}
    assert ("3 packages of the runtime lock at their locked versions, and from the base image pip 26.2.2, "
            "wheel 0.46.3") in lines
    assert PACKAGES_REFUSED in proc.stderr
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


RELEASE_REF = "registry.example.org/team/pubdex:0.1.0@sha256:" + "a" * 64
RELEASE_POSTGRES_REF = "registry.example.org/team/pubdex-postgres:0.1.0@sha256:" + "b" * 64
RELEASE = ["--expect-version", "0.1.0", "--expect-revision", SHA]
#: A release pulled by digest: both images of it.
RELEASE_PULL = ["--pull", "--postgres-image", RELEASE_POSTGRES_REF]
RELEASE_COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8").replace(
    (ROOT / "VERSION").read_text(encoding="utf-8").strip(), "0.1.0").replace(
    "${PEOPLE_PUBS_IMAGE:-ghcr.io/ccu-ukn/pubdex:0.1.0}",
    "${PEOPLE_PUBS_IMAGE:-registry.example.org/team/pubdex:0.1.0}")


@pytest.mark.parametrize("reference, extra", [
    (RELEASE_REF, RELEASE_PULL),
    # Before the push: the image as build_image.sh tagged it, no digest yet.
    ("registry.example.org/team/pubdex:0.1.0", []),
    ("registry.example.org/team/pubdex:candidate", []),
], ids=["published", "before-the-push", "tag-without-version"])
def test_a_release_is_the_image_labelled_with_it_and_the_compose_file_it_carries(tmp_path: Path, reference: str,
                                                                                 extra: list) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.setting("pull_revision", SHA)
    fake.image_compose(RELEASE_COMPOSE)
    # Whatever the checkout holds and whatever commit it is: neither takes part.
    (fake.root / "compose.yaml").write_text("# a modified checkout\n", encoding="utf-8")
    fake.setting("git_absent", "")
    if not extra:
        fake.add_image(reference)
    proc = fake.run("--release", "--image", reference, *extra, *RELEASE)
    out = proc.stdout
    assert "ok  the image's release-version label is 0.1.0" in out and f"ok  the image's revision label is {SHA}" in out
    assert ("ok  the image carries the requested digest" in out) == ("@sha256:" in reference)
    assert ("ok  the run starts the compose.yaml the image carries, which names the release's image "
            "registry.example.org/team/pubdex:0.1.0") in out
    assert f"ok  the image carries {SCHEMA_CHANGES} in /opt/pubdex/schema, the last {SCHEMA_FILES[-1].stem}\n" in out
    assert f"ok  the PostgreSQL image's revision label is {SHA}" in out
    assert "ok  the compose.yaml the image carries names the release's PostgreSQL image " + DEFAULT_POSTGRES in out
    assert ("ok  the PostgreSQL image carries the requested digest" in out) == bool(extra)
    assert "note:" not in out
    assert fake.started_compose() == RELEASE_COMPOSE
    assert (["pull", reference] in fake.calls()) == bool(extra)
    assert (["pull", RELEASE_POSTGRES_REF] in fake.calls()) == bool(extra)
    postgres = RELEASE_POSTGRES_REF if extra else DEFAULT_POSTGRES
    assert f"PEOPLE_PUBS_POSTGRES_IMAGE={postgres}\n" in (fake.state / "last.env").read_text()
    assert fake.subcommands()[:2] == ["config", "up"] and fake.calls("build") == []
    assert fake.calls("git") == [], "a release is verified without Git"
    fake.assert_nothing_left()


@pytest.mark.parametrize("compose, message", [
    (RELEASE_COMPOSE.replace("pubdex:0.1.0}", "pubdex:0.0.9}"),
     "the compose.yaml the image carries names the image registry.example.org/team/pubdex:0.0.9, not one of release 0.1.0"),
    (RELEASE_COMPOSE.replace("${PEOPLE_PUBS_IMAGE:-registry.example.org/team/pubdex:0.1.0}", "pubdex:local"),
     "the compose.yaml the image carries names no default application image"),
    # As 0.1.0's: nothing brings the database to the schema of the image.
    (re.sub(r"\n  # Brings the database to the schema.*?\n\n", "\n\n", RELEASE_COMPOSE, flags=re.S),
     "the compose.yaml the image carries has no schema-upgrade service, which brings the database to the schema of "
     "the image"),
], ids=["other-version", "no-default", "no-schema-upgrade"])
def test_a_release_whose_compose_file_names_another_image_stops_before_the_start(tmp_path: Path, compose: str,
                                                                                message: str) -> None:
    fake = Fake(tmp_path)
    fake.setting("pull_revision", SHA)
    fake.image_compose(compose)
    proc = fake.run("--release", "--image", RELEASE_REF, *RELEASE_PULL, *RELEASE)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


@pytest.mark.parametrize(
    "version, revision, message",
    [
        ("0.1.0", SHA + "-dirty", f"the image was built from a working tree of {SHA} with changes"),
        ("0.1.0", "unknown", "the image's revision label is 'unknown'"),
        ("0.1.0", COMMIT_B, f"the image's revision label is '{COMMIT_B}', expected '{SHA}'"),
        ("", SHA, "the image has no release-version label"),
        # Its tag says 0.1.0; the label is what counts.
        ("0.1.1", SHA, "the image's release-version label is '0.1.1', expected '0.1.0'"),
    ],
    ids=["local-build", "revision-unknown", "other-commit", "version-missing", "version-not-the-tag"],
)
def test_a_release_image_whose_labels_are_not_the_release_is_refused_before_the_start(
        tmp_path: Path, version: str, revision: str, message: str) -> None:
    fake = Fake(tmp_path)
    fake.setting("pull_version", version)
    fake.setting("pull_revision", revision)
    proc = fake.run("--release", "--image", RELEASE_REF, *RELEASE_PULL, *RELEASE)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert ["pull", RELEASE_REF] in fake.calls() and fake.compose_calls() == []
    assert not any(call[:1] == ["run"] for call in fake.calls()), "nothing runs from an image that is not the release"
    assert ["image", "rm", RELEASE_REF] not in fake.calls()
    fake.assert_nothing_left()


GIT_POLLUTION = {"GIT_DIR": "/elsewhere/.git", "GIT_WORK_TREE": "/elsewhere", "GIT_INDEX_FILE": "/elsewhere/index",
                 "GIT_CONFIG_PARAMETERS": "'core.bare'='true'", "GIT_OPTIONAL_LOCKS": "1"}


def test_the_checkout_is_only_asked_and_in_a_cleared_environment(tmp_path: Path) -> None:
    # A build is checked against the revision of this checkout, by the rules
    # build_image.sh labels it with.
    fake = Fake(tmp_path, "up-fails")
    proc = fake.run(**GIT_POLLUTION)
    assert f"ok  the image's revision label is {SHA}" in proc.stdout, proc.stderr
    calls = fake.calls("git")
    assert calls
    for call in calls:
        assert call[:2] == ["-C", str(fake.root)], call
        question = call[2:]
        if question[0] == "--no-optional-locks":
            question = question[1:]
        else:
            assert question[0] != "status", "git status without --no-optional-locks may write the index"
        assert question[0] in {"rev-parse", "status", "ls-files"}, call
    for environment in fake.calls("git-env"):
        names = {item.split("=", 1)[0] for item in environment}
        assert names <= ISOLATED_ENVIRONMENT, sorted(names)


@pytest.mark.parametrize("git_status, git_ignored, revision", [
    ("", "", SHA),
    (" M compose.yaml\n", "", SHA + "-dirty"),
    ("", "DB/people_pubs/__pycache__/local_settings.py\n", SHA + "-dirty"),
], ids=["clean", "modified", "ignored-module"])
def test_a_build_is_this_checkout_under_a_tag_of_the_run_which_goes_with_it(tmp_path: Path, git_status: str,
                                                                            git_ignored: str, revision: str) -> None:
    fake = Fake(tmp_path, "up-fails")
    fake.setting("git_status", git_status)
    fake.setting("git_ignored", git_ignored)
    fake.setting("build_revision", revision)
    proc = fake.run()
    [build] = fake.calls("build")
    run = _project(fake).removeprefix('pubdex-acceptance-')
    assert build == ["--tag", f"pubdex-acceptance:{run}", "--postgres-tag", f"pubdex-acceptance-postgres:{run}"], build
    assert f"ok  the image's revision label is {revision}" in proc.stdout
    assert f"ok  the PostgreSQL image's labels are this checkout's: version 0.1.0, revision {revision}" in proc.stdout
    assert "ok  the run starts this checkout's compose.yaml, which the image carries unchanged" in proc.stdout
    assert (f"ok  the image carries {SCHEMA_CHANGES} in /opt/pubdex/schema, the last {SCHEMA_FILES[-1].stem}, "
            "exactly this checkout's DB/schema/") in proc.stdout
    assert f"PEOPLE_PUBS_POSTGRES_IMAGE={build[3]}\n" in (fake.state / "last.env").read_text()
    assert ["image", "rm", build[1]] in fake.calls(), "the cleanup removes the run's tags"
    assert ["image", "rm", build[3]] in fake.calls()
    assert fake.images() == [], "and nothing else of the image is left in the stub"
    fake.assert_nothing_left()


def test_a_build_that_carries_another_compose_file_than_this_checkouts_is_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.image_compose("# not this checkout's compose.yaml\n")
    proc = fake.run()
    assert proc.returncode == 1
    assert "the image built from this checkout carries another compose.yaml than this checkout's" in proc.stderr
    assert fake.compose_calls() == [] and fake.images() == []
    fake.assert_nothing_left()


def test_a_build_that_claims_another_revision_is_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.setting("build_revision", "0" * 40)
    proc = fake.run()
    assert proc.returncode == 1 and f"expected '{SHA}'" in proc.stderr
    assert fake.compose_calls() == [] and fake.images() == []
    fake.assert_nothing_left()


def test_a_build_whose_postgresql_image_claims_another_revision_is_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.setting("build_postgres_revision", COMMIT_B)
    proc = fake.run()
    assert proc.returncode == 1 and f"the PostgreSQL image's labels are version '0.1.0', revision '{COMMIT_B}'" \
        in proc.stderr, proc.stderr
    assert fake.compose_calls() == [] and fake.images() == []
    fake.assert_nothing_left()


def test_a_build_labelled_with_another_version_than_version_is_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.setting("build_version", "0.2.0")
    proc = fake.run()
    assert proc.returncode == 1
    assert "the image's release-version label is '0.2.0', expected '0.1.0' from VERSION" in proc.stderr
    assert fake.compose_calls() == [] and fake.images() == []
    fake.assert_nothing_left()


def test_a_failed_build_stops_the_run(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "build-fails")
    proc = fake.run()
    assert proc.returncode == 1 and "build_image.sh failed" in proc.stderr
    assert "stub: the image build failed" in proc.stderr, "the build's own error is shown"
    assert fake.compose_calls() == []
    fake.assert_nothing_left()


POLLUTION = {
    "COMPOSE_PROJECT_NAME": "db", "COMPOSE_FILE": "/elsewhere/compose.yaml", "COMPOSE_PROFILES": "demo",
    "COMPOSE_ENV_FILES": "/elsewhere/.env", "COMPOSE_PATH_SEPARATOR": ";",
    "PEOPLE_PUBS_IMAGE": "elsewhere/pubdex:latest", "PEOPLE_PUBS_SECRETS_DIR": "/elsewhere/secrets",
    "PEOPLE_PUBS_HTTP_ADDRESS": "0.0.0.0", "PEOPLE_DB_DSN": "postgresql://elsewhere/people_db",
    "PGHOST": "elsewhere", "PGPASSWORD": "example-inherited-password", "POSTGRES_PASSWORD": "example-inherited",
    "BUILDX_GIT_INFO": "1", "DOCKER_BUILDKIT": "0",
    "BASH_FUNC_docker%%": "() { echo hijacked; }",
}


def test_every_compose_call_is_bound_to_the_run_whatever_the_caller_exports(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    # A caller's .env and override file next to compose.yaml, which Compose
    # would read without explicit options.
    (fake.root / ".env").write_text("PEOPLE_PUBS_IMAGE=elsewhere/pubdex:latest\n", encoding="utf-8")
    (fake.root / "compose.override.yaml").write_text("services: {}\n", encoding="utf-8")
    proc = fake.run(**POLLUTION, DOCKER_HOST="unix:///run/stub-docker.sock")
    assert proc.returncode == 1 and "docker compose up --wait failed" in proc.stderr
    assert fake.calls(), "the exported function did not replace docker"
    project = _project(fake)
    calls = fake.compose_calls()
    assert fake.subcommands()[:2] == ["config", "up"]
    work = Path(calls[0][9]).parent
    assert work.parent == fake.workdirs and calls[0][9].endswith("/stack.env")
    for call in calls:
        # A project directory of the run's own, with this checkout's
        # compose.yaml, not the checkout itself.
        assert call[:10] == ["--project-name", project, "--project-directory", str(work / "project"),
                             "--file", str(work / "project" / "compose.yaml"),
                             "--file", str(fake.root / "DB" / "tests" / "compose.acceptance.yaml"),
                             "--env-file", str(work / "stack.env")], call
    seen = fake.state / "directory-seen"
    assert sorted(path.name for path in seen.iterdir()) == ["compose.yaml"], "compose.yaml and nothing else"
    assert (seen / "compose.yaml").read_bytes() == (fake.root / "compose.yaml").read_bytes()
    environments = [dict(item.split("=", 1) for item in call) for call in fake.calls("compose-env")]
    assert environments
    for environment in environments:
        assert set(environment) <= ISOLATED_ENVIRONMENT | {"DOCKER_HOST"}, sorted(environment)
        assert environment["DOCKER_HOST"] == "unix:///run/stub-docker.sock", "the daemon selection is kept"
    settings = (fake.state / "last.env").read_text(encoding="utf-8").splitlines()
    assert [line.split("=", 1)[0] for line in settings] == [
        "PEOPLE_PUBS_IMAGE", "PEOPLE_PUBS_POSTGRES_IMAGE", "PEOPLE_PUBS_SECRETS_DIR", "PEOPLE_PUBS_HTTP_ADDRESS",
        "PEOPLE_PUBS_HTTP_PORT", "PEOPLE_PUBS_API_LOG_LEVEL", "PEOPLE_PUBS_CROSSREF_MAILTO",
        "PEOPLE_PUBS_ORG_PATTERNS", "PEOPLE_PUBS_ACCEPTANCE_RUN"]
    assert settings[0] == f"PEOPLE_PUBS_IMAGE=pubdex-acceptance:{project.removeprefix('pubdex-acceptance-')}"
    assert settings[1] == ("PEOPLE_PUBS_POSTGRES_IMAGE=pubdex-acceptance-postgres:"
                           + project.removeprefix('pubdex-acceptance-'))
    assert settings[3:5] == ["PEOPLE_PUBS_HTTP_ADDRESS=127.0.0.1", "PEOPLE_PUBS_HTTP_PORT=0"]
    assert settings[8] == f"PEOPLE_PUBS_ACCEPTANCE_RUN={project.removeprefix('pubdex-acceptance-')}"
    [build_environment] = [dict(item.split("=", 1) for item in call) for call in fake.calls("build-env")]
    assert set(build_environment) <= ISOLATED_ENVIRONMENT | {"DOCKER_HOST"}, sorted(build_environment)
    fake.assert_nothing_left()


def test_no_password_reaches_a_command_line_or_the_output_unmasked(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    proc = fake.run()
    seen = fake.state / "secrets-seen"
    secrets = {name: (seen / name).read_text(encoding="utf-8").strip()
               for name in ("postgres_password", "app_readonly_password", "app_writer_password")}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in secrets.values()), "three random passwords"
    assert len(set(secrets.values())) == 3
    log = fake.events.read_text(encoding="utf-8")
    for value in secrets.values():
        assert value not in log, "no docker, compose or build command line or environment holds a password"
        assert value not in proc.stdout + proc.stderr
    assert "the writer password is <password>" in proc.stderr, "Docker's output is shown masked"
    fake.assert_nothing_left()


@pytest.mark.parametrize(
    "mode, message",
    [("config-leak", "a password occurs in the rendered configuration"),
     ("config-redirected", "the rendered configuration is not the run's own"),
     # The read service holding the superuser's password file, or starting
     # before the schema upgrade has succeeded.
     ("config-extra-credential", "the rendered configuration is not the run's own"),
     ("config-no-upgrade-dependency", "the rendered configuration is not the run's own")],
)
def test_a_rendered_configuration_that_is_not_safe_stops_the_run_before_the_start(tmp_path: Path, mode: str,
                                                                                    message: str) -> None:
    fake = Fake(tmp_path, mode)
    proc = fake.run()
    assert proc.returncode == 1 and message in proc.stderr
    assert "up" not in fake.subcommands()
    fake.assert_nothing_left()


def test_a_failure_after_the_start_removes_the_project_and_shows_masked_diagnostics(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "leaky-logs")
    proc = fake.run()
    assert proc.returncode == 1
    assert "the read service is not published on a single loopback port" in proc.stderr
    assert "the superuser password is <password>" in proc.stderr
    assert re.search(r"the superuser password is [0-9a-f]{64}", proc.stdout + proc.stderr) is None
    [down] = [call for call in fake.compose_calls() if "down" in call]
    assert down[down.index("down"):] == ["down", "--volumes", "--remove-orphans", "--timeout", "5"]
    assert down[down.index("--env-file") + 2:down.index("down")] == ["--profile", "ingest", "--profile", "demo"]
    assert "cleanup:" not in proc.stdout, "no success message after a failure"
    fake.assert_nothing_left()
    assert fake.images() == []


def test_what_compose_leaves_behind_is_removed_by_the_run_label(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "leaky-logs,down-fails")
    proc = fake.run()
    assert proc.returncode == 1
    project = _project(fake)
    removed = {call[-1] for call in fake.calls() if call[:1] == ["rm"] or call[1:2] == ["rm"]}
    assert {f"{project}-api", f"{project}-postgres", f"{project}-data", f"{project}-default",
            f"{project}-published"} <= removed
    fake.assert_nothing_left()


@pytest.mark.parametrize(
    "mode, warning",
    [
        ("leaky-logs,down-ineffective,rm-ineffective", "docker ps -a --filter label=pubdex.stack-acceptance.run="),
        ("leaky-logs,image-rm-fails", "docker image ls pubdex-acceptance:"),
    ],
    ids=["resources", "image-tag"],
)
def test_a_cleanup_that_cannot_be_confirmed_names_the_leftovers(tmp_path: Path, mode: str, warning: str) -> None:
    fake = Fake(tmp_path, mode)
    proc = fake.run()
    assert proc.returncode == 1, "a failed run keeps its status"
    assert "could not confirm that run" in proc.stderr and warning in proc.stderr
    assert "docker network ls --filter label=pubdex.stack-acceptance.run=" in proc.stderr


def test_a_temporary_directory_that_cannot_be_removed_is_reported(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "leaky-logs")
    fake.setting("rm_refused", "")
    proc = fake.run()
    assert proc.returncode == 1
    assert "could not remove the run's temporary directory" in proc.stderr and "secret files" in proc.stderr
    (fake.state / "rm_refused").unlink()


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
    # Ignored by the cleanup, and by the commands it starts: the slow `down`
    # and the removal by label both run to the end.
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


STUB_INSPECT = r'''#!/bin/sh
# Stand-in for docker ps and inspect: the mounts of two containers, in the
# format foreign_mounts asks for -- type, source and name, tab-separated.
case "$1" in
  ps) [ -e "@STATE@/ps_fails" ] && exit 1; printf 'c1\nc2\n' ;;
  inspect)
    for last; do :; done
    case "$last" in
      c1) printf 'volume\t/var/lib/docker/volumes/proj_postgres-data/_data\tproj_postgres-data\n'
          printf 'volume\t/var/lib/docker/volumes/proj_initdb/_data\tproj_initdb\n'
          printf 'bind\t@SECRETS@/postgres_password\t\n'
          printf 'bind\t@SECRETS@/app_readonly_password\t\n' ;;
      c2) printf 'bind\t@SECRETS@/app_writer_password\t\n'
          printf 'bind\t/srv/checkout/DB/init\t\n'
          printf 'volume\t/var/lib/docker/volumes/other_initdb/_data\tother_initdb\n'
          printf 'bind\t@SECRETS@/../elsewhere/app_writer_password\t\n' ;;
    esac ;;
esac
'''


def test_foreign_mounts_names_every_mount_but_the_projects_volumes_and_secret_files(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    (tmp_path / "elsewhere").mkdir()
    secrets.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    _executable(tmp_path / "bin" / "docker",
                STUB_INSPECT.replace("@SECRETS@", str(secrets)).replace("@STATE@", str(state)))
    command = ['bash', '-c', 'source "$1"; foreign_mounts proj "$2"', "bash",
               str(ROOT / "DB" / "lib" / "compose_acceptance.sh"), str(secrets)]
    env = {"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}", "LANG": "C.UTF-8"}
    proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    # A bind has no name: the empty field must not shift its source.
    assert proc.stdout.splitlines() == ["c2: bind /srv/checkout/DB/init", "c2: volume other_initdb",
                                        f"c2: bind {secrets}/../elsewhere/app_writer_password"]
    (state / "ps_fails").write_text("", encoding="utf-8")
    assert subprocess.run(command, env=env, capture_output=True, text=True, timeout=60).returncode == 1


def test_the_acceptance_files_are_in_this_repository() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert (ROOT / "DB" / "tests" / "compose.acceptance.yaml").is_file()
