"""Lifecycle of run_upgrade_acceptance.sh, offline.

The real script runs from a scratch copy of the checkout layout -- the
script, the helpers it sources, VERSION, the release record and its reader,
and the deployment files -- against a stub `release.sh` that records the
test releases it is asked for and keeps their images in a state directory,
and a stub `docker` that keeps an inventory of images and of the
containers, networks and volumes it was asked to create, with their labels,
and logs every call with its environment and working directory. Like
Compose, the stub takes a project's settings from the .env file of its
working directory.

That proves, without Docker, what the real run of stage 11 of
run_ci_suite.sh cannot show by passing: the usage and every missing
prerequisite, each before anything exists; the three test releases built
under names of the run's own, B1 and B2 with their overlays; that a stack's
operator's directory holds the deployment files taken out of the
application image with the documented command, an .env file and the run
label's override, and that the .env file alone configures every Compose
call; that every Docker command uses the run's own client configuration;
that a project which exists already is never touched; that no password
reaches the output unmasked; and that a failure, a signal or a cleanup that
cannot be confirmed ends the run with the right status after removing
exactly what it created. The upgrades and rollbacks themselves run against
real Docker, as stage 11 of run_ci_suite.sh.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "run_upgrade_acceptance.sh"
BASH = shutil.which("bash") or "/bin/bash"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
SYFT_IMAGE = re.search(r"^readonly PINNED_SYFT_IMAGE=(\S+)$",
                       (ROOT / "DB/lib/sbom_tool.sh").read_text(encoding="utf-8"), re.M).group(1)
HELM_VERSION = re.search(r"^readonly PINNED_HELM_VERSION=(\S+)$",
                         (ROOT / "DB/lib/cluster_tools.sh").read_text(encoding="utf-8"), re.M).group(1)
DEPLOYMENT_FILES = ["compose.yaml", "create_secrets.sh", "set_passwords.sh"]
PROJECT_RE = r"pubdex-upgrade-[0-9a-f]{10}-(test|from-[0-9-]+)"
# What isolated() hands a command, plus what a shell adds on its own.
ISOLATED_ENVIRONMENT = {"PATH", "HOME", "LC_ALL", "PWD", "SHLVL", "_", "OLDPWD", "DOCKER_HOST", "DOCKER_CONFIG"}

STUB_DOCKER = r'''#!/usr/bin/env bash
# Stand-in for the docker CLI: images and resources are files in @STATE@,
# each holding its labels, one "key=value" a line.
set -u
state="@STATE@"
events="@EVENTS@"
mode="$(cat "$state/mode" 2>/dev/null || true)"
has() { case ",$mode," in *",$1,"*) return 0 ;; esac; return 1; }
{ printf 'docker'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$events"
{ printf 'docker-env'; env | sort | while IFS= read -r line; do printf '\037%s' "$line"; done; printf '\n'; } >> "$events"
mkdir -p "$state/resources" "$state/images"
option() {  # option <flag> <args...>: the value after the first <flag>
  local flag="$1" previous=""; shift
  for a in "$@"; do [ "$previous" = "$flag" ] && { printf '%s' "$a"; return; }; previous="$a"; done
}
key() { printf '%s' "$1" | tr '/:@' '___'; }
labelled() {  # labelled <directory glob prefix> <label=value>: names of the files holding that line
  local f
  for f in $1*; do [ -e "$f" ] && grep -qxF "$2" "$f" && echo "${f##*/}"; done
}
listing() {  # listing <kind> <args...>
  has list-fails && { echo "stub: cannot connect to the Docker daemon" >&2; exit 1; }
  local kind="$1" wanted; shift
  wanted="$(option --filter "$@")"
  case "$wanted" in
    label=*) wanted="${wanted#label=}" ;;
    ancestor=*) exit 0 ;;
  esac
  if has collision && [[ "$wanted" == com.docker.compose.project=* ]]; then echo "foreign-$kind"; fi
  labelled "$state/resources/$kind." "$wanted" | sed "s/^$kind\.//"
  exit 0
}
remove() {  # remove <kind> <name>...
  local kind="$1"; shift
  has rm-ineffective && exit 0
  for name in "$@"; do rm -f "$state/resources/$kind.$name"; done
  exit 0
}
case "$1" in
  info) exit 0 ;;
  context) printf 'default\tunix:///var/run/stub-docker.sock\t0\n'; exit 0 ;;
  version)
    case "$(option --format "$@")" in
      *Os*) cat "$state/platform" 2>/dev/null || echo linux/amd64 ;;
      *) cat "$state/engine_version" 2>/dev/null || echo 29.10.0 ;;
    esac
    exit 0 ;;
  buildx) has no-buildx && exit 1; echo "github.com/docker/buildx v0.39.0"; exit 0 ;;
  ps) shift; listing container "$@" ;;
  volume) case "$2" in ls) shift 2; listing volume "$@" ;; rm) shift 2; remove volume "$@" ;; esac ;;
  network) case "$2" in ls) shift 2; listing network "$@" ;; rm) shift 2; remove network "$@" ;; esac ;;
  rm) shift; while [ "${1:-}" != "${1#-}" ]; do shift; done; remove container "$@" ;;
  image)
    case "$2" in
      inspect)
        last="${!#}"
        [ -e "$state/images/$(key "$last")" ] || { echo "Error: No such image: $last" >&2; exit 1; }
        case "$(option --format "$@")" in
          *transform-version*) sed -n 's/^pubdex.transform-version=//p' "$state/images/$(key "$last")" ;;
          *) sed -n 's/^id=//p' "$state/images/$(key "$last")" ;;
        esac
        exit 0 ;;
      ls)
        wanted="$(option --filter "$@")"
        labelled "$state/images/" "${wanted#label=}" | while read -r name; do sed -n 's/^id=//p' "$state/images/$name"; done | sort -u
        exit 0 ;;
      rm)
        shift 2
        has image-rm-fails && { echo "stub: image is in use" >&2; exit 1; }
        for reference in "$@"; do
          [ "$reference" = -f ] && continue
          for f in "$state/images/"*; do
            [ -e "$f" ] || continue
            if [ "${f##*/}" = "$(key "$reference")" ] || grep -qxF "id=$reference" "$f"; then rm -f "$f"; fi
          done
        done
        exit 0 ;;
    esac ;;
  run)
    # The deployment files an application image carries, as a tar stream.
    image="$(option --rm "$@")"
    [ -e "$state/images/$(key "$image")" ] || { echo "Unable to find image '$image' locally" >&2; exit 125; }
    tar -cC "$state/deployment" compose.yaml create_secrets.sh set_passwords.sh
    exit 0 ;;
  pull) echo "stub: never pulls" >&2; exit 1 ;;
  port) exit 0 ;;
  compose)
    shift
    if [ "$1" = version ]; then
      cat "$state/compose_version" 2>/dev/null || echo 5.8.0; exit 0
    fi
    printf 'compose-cwd\037%s\n' "$(pwd -P)" >> "$events"
    setting() { sed -n "s/^$1=//p" .env 2>/dev/null; }
    project="$(setting COMPOSE_PROJECT_NAME)"
    run_label="$(sed -n 's/^ *[a-z-]*: {labels: {\(.*\)}}$/\1/p' compose.override.yaml 2>/dev/null | head -n 1 \
      | tr -d ' "' | sed 's/:/=/')"
    while [ "${1:-}" = --profile ]; do shift 2; done
    case "$1" in
      config)
        case "${2:-}" in
          --services) printf '%s\n' bootstrap postgres schema-upgrade api ingest demo ;;
          --volumes) printf '%s\n' postgres-data initdb ;;
        esac
        exit 0 ;;
      up)
        cp compose.override.yaml "$state/override-seen" 2>/dev/null
        has up-fails && { echo "stub: up failed; the superuser password is $(cat secrets/postgres_password)"; exit 1; }
        for resource in container.api container.postgres volume.postgres-data network.default; do
          printf 'com.docker.compose.project=%s\n%s\n' "$project" "$run_label" \
            > "$state/resources/${resource%%.*}.$project-${resource#*.}"
        done
        if has up-hangs; then for _ in $(seq 300); do sleep 0.1; done; fi
        exit 0 ;;
      logs) echo "postgres-1  | stub log"; exit 0 ;;
      ps) exit 0 ;;
      down)
        has down-ineffective && exit 0
        labelled "$state/resources/" "com.docker.compose.project=$project" | while read -r name; do
          rm -f "$state/resources/$name"
        done
        exit 0 ;;
    esac
    exit 0 ;;
esac
echo "stub: unsupported docker $*" >&2
exit 2
'''

STUB_RELEASE = r'''#!/usr/bin/env bash
# release.sh build --test <name> [--overlay b1|b2] --out <dir>: two images of
# the test release in @STATE@, with its version label, and IMAGES.
state="@STATE@"
{ printf 'release'; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "@EVENTS@"
{ printf 'release-env'; env | sort | while IFS= read -r line; do printf '\037%s' "$line"; done; printf '\n'; } >> "@EVENTS@"
name="" overlay="" out=""
shift
while [ $# -gt 0 ]; do
  case "$1" in --test) name="$2" ;; --overlay) overlay="$2" ;; --out) out="$2" ;; esac
  shift 2
done
case ",$(cat "$state/mode" 2>/dev/null)," in *",build-fails:${name:0:2},"*) echo "stub: build failed"; exit 1 ;; esac
version="$(cat "@ROOT@/VERSION")-test.$name"
transform=1.5.4
[ -n "$overlay" ] && transform=1.5.4+test
key() { printf '%s' "$1" | tr '/:@' '___'; }
mkdir -p "$state/images" "$out"
for role in application postgres; do
  tag="pubdex-release:$version"
  [ "$role" = postgres ] && tag="pubdex-postgres-release:$version"
  printf 'id=sha256:%s-%s\norg.opencontainers.image.version=%s\npubdex.transform-version=%s\n' \
    "$name" "$role" "$version" "$transform" > "$state/images/$(key "$tag")"
  printf '%s %s sha256:%s-%s\n' "$role" "$tag" "$name" "$role" >> "$out/IMAGES"
done
echo "PASS: PubDex $version built and verified in $out"
'''


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _finished_lines(path: Path) -> list:
    """The lines the stubs have finished writing; the last may lack its newline."""
    data = path.read_bytes()
    return data[:data.rfind(b"\n") + 1].decode("utf-8").splitlines()


class Fake:
    """A scratch checkout holding the real script, its helpers, the release record and stubs."""

    def __init__(self, tmp_path: Path, mode: str = "") -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        self.script = self.root / SCRIPT.name
        for relative in [SCRIPT.name, "VERSION", "DB/release_record.py", "DB/lib/disposable_postgres.sh",
                         "DB/lib/docker_versions.sh", "DB/lib/compose_acceptance.sh", "DB/lib/cluster_tools.sh",
                         "DB/lib/sbom_tool.sh", "releases/FLOOR", "releases/0.1.0/release.json"]:
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, self.root / relative)
        self.state = tmp_path / "state"
        (self.state / "images").mkdir(parents=True)
        (self.state / "deployment").mkdir()
        for name in DEPLOYMENT_FILES:
            shutil.copy2(ROOT / name, self.state / "deployment" / name)
        self.events = tmp_path / "events.log"
        self.workdirs = tmp_path / "tmp"
        self.workdirs.mkdir()
        bindir = tmp_path / "bin"
        values = {"@STATE@": str(self.state), "@EVENTS@": str(self.events), "@ROOT@": str(self.root)}

        def fill(text: str) -> str:
            for placeholder, value in values.items():
                text = text.replace(placeholder, value)
            return text

        _executable(bindir / "docker", fill(STUB_DOCKER))
        _executable(bindir / "git", '#!/bin/sh\necho true\n')
        _executable(self.root / "release.sh", fill(STUB_RELEASE))
        _executable(self.root / ".tools" / f"helm-{HELM_VERSION}" / "helm", "#!/bin/sh\nexit 0\n")
        self.mode(mode)
        self.add_image(SYFT_IMAGE, "sha256:syft")
        record = json.loads((ROOT / "releases/0.1.0/release.json").read_text(encoding="utf-8"))
        for reference in record["images"].values():
            self.add_image(reference, "sha256:published-" + reference.rsplit(":", 1)[1][:8])
        self.home = tmp_path / "home"
        (self.home / ".docker" / "cli-plugins").mkdir(parents=True)
        (self.home / ".docker" / "config.json").write_text('{"auths": {"ghcr.io": {"auth": "c2VjcmV0"}}}\n')
        self.env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.home),
                    "TMPDIR": str(self.workdirs), "LANG": "C.UTF-8"}

    def mode(self, mode: str) -> None:
        (self.state / "mode").write_text(mode, encoding="utf-8")

    def setting(self, name: str, value: str) -> None:
        (self.state / name).write_text(value, encoding="utf-8")

    def add_image(self, reference: str, image_id: str) -> None:
        (self.state / "images" / re.sub(r"[/:@]", "_", reference)).write_text(f"id={image_id}\n", encoding="utf-8")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.script), *args], cwd=self.tmp, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=120)

    def popen(self) -> subprocess.Popen:
        return subprocess.Popen([BASH, str(self.script)], cwd=self.tmp, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)

    def lines(self) -> list:
        return [line.split("\x1f") for line in _finished_lines(self.events)] if self.events.exists() else []

    def calls(self, kind: str = "docker") -> list:
        return [line[1:] for line in self.lines() if line[0] == kind]

    def environments(self, kind: str = "docker-env") -> list:
        return [dict(item.split("=", 1) for item in line[1:] if "=" in item) for line in self.lines()
                if line[0] == kind]

    def compose_calls(self) -> list:
        return [call[1:] for call in self.calls() if call[:1] == ["compose"] and call[1:2] != ["version"]]

    def resources(self) -> list:
        return sorted(path.name for path in (self.state / "resources").glob("*")) \
            if (self.state / "resources").exists() else []

    def test_images(self) -> list:
        return sorted(path.name for path in (self.state / "images").iterdir() if "test." in path.name)

    def assert_nothing_left(self) -> None:
        assert self.resources() == [], "every container, network and volume of the run is gone"
        assert self.test_images() == [], "every image of the test releases is gone"
        assert list(self.workdirs.iterdir()) == [], "the run's temporary directory is gone"

    def wait_for_up(self, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any(call[:1] == ["up"] for call in self.compose_calls()) and len(self.resources()) == 4:
                return
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        pytest.fail("the run never reached docker compose up")


def _names(fake: Fake) -> list:
    """The test releases' names, from release.sh's calls."""
    return [call[call.index("--test") + 1] for call in fake.calls("release")]


# --- usage and prerequisites ----------------------------------------------------------


def test_it_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize("args, env, message", [
    (["extra"], {}, "unexpected argument: extra"),
    (["--help", "extra"], {}, "unexpected argument: --help"),
    ([], {"STACK_WAIT_SECONDS": "0"}, "STACK_WAIT_SECONDS must be a positive number of seconds"),
])
def test_invalid_arguments_are_usage_errors_and_touch_nothing(tmp_path: Path, args: list, env: dict,
                                                              message: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(*args, **env)
    assert proc.returncode == 2 and message in proc.stderr and "usage: run_upgrade_acceptance.sh" in proc.stderr
    assert fake.calls() == [] and fake.calls("release") == []


def test_help_runs_nothing(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run("--help")
    assert proc.returncode == 0 and "usage: run_upgrade_acceptance.sh" in proc.stdout
    assert "image rollback" in proc.stdout and "restore of the backup" in proc.stdout
    assert fake.calls() == []


@pytest.mark.parametrize("setup, message", [
    (lambda fake: (fake.root / "VERSION").write_text("0.2.0-rc.1\n"), "for a VERSION without a pre-release part"),
    (lambda fake: shutil.rmtree(fake.root / ".tools"), "the pinned helm"),
    (lambda fake: fake.mode("list-fails,info-fails"), None),
    (lambda fake: fake.setting("engine_version", "28.2.1"), "Docker Engine 28.0.1 or later"),
    (lambda fake: fake.setting("platform", "linux/arm64"), "the releases' images are for linux/amd64"),
    (lambda fake: fake.setting("compose_version", "2.23.3"), "2.24.0 or later"),
    (lambda fake: fake.mode("no-buildx"), "the Docker buildx plugin was not found"),
    (lambda fake: (fake.state / "images" / re.sub(r"[/:@]", "_", SYFT_IMAGE)).unlink(), "./release.sh setup"),
    (lambda fake: (fake.root / "releases/FLOOR").unlink(), "the release record names no floor"),
    (lambda fake: (fake.root / "releases/0.1.0/release.json").write_text(
        (ROOT / "releases/0.1.0/release.json").read_text().replace("@sha256:24d5", "@sha256:zz")),
     "the release record"),
], ids=["pre-release", "helm", "daemon", "engine", "platform", "compose", "buildx", "sbom tool", "floor",
        "digest"])
def test_missing_prerequisites_stop_the_run_before_anything_exists(tmp_path: Path, setup, message) -> None:
    fake = Fake(tmp_path)
    setup(fake)
    if message is None:  # docker info fails: an unreachable daemon
        _executable(tmp_path / "bin" / "docker", '#!/bin/sh\n[ "$1" = context ] && printf "d\\tunix:///x\\t0\\n" '
                                                 '&& exit 0\nexit 1\n')
        message = "cannot reach the Docker daemon"
    proc = fake.run()
    assert proc.returncode == 1 and message in proc.stderr, proc.stdout + proc.stderr
    assert fake.calls("release") == [] and fake.compose_calls() == []
    fake.assert_nothing_left()


# --- the test releases and the first stack ----------------------------------------------


def test_the_test_releases_are_built_under_names_of_the_runs_own(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    proc = fake.run()
    assert proc.returncode == 1
    names = _names(fake)
    assert len(names) == 3 and re.fullmatch(r"a[0-9a-f]{10}", names[0]), names
    run = names[0][1:]
    assert names == [f"a{run}", f"b1{run}", f"b2{run}"]
    calls = fake.calls("release")
    assert [call[:3] for call in calls] == [["build", "--test", name] for name in names]
    assert [call[3:5] for call in calls[1:]] == [["--overlay", "b1"], ["--overlay", "b2"]]
    assert "--overlay" not in calls[0]
    # Into the run's temporary directory, with it as release.sh's TMPDIR.
    for call, env in zip(calls, fake.environments("release-env")):
        out = Path(call[call.index("--out") + 1])
        assert out.parent == Path(env["TMPDIR"]).parent and out.parent.parent == fake.workdirs
    assert f"test releases A {VERSION}-test.a{run}, B1 {VERSION}-test.b1{run}, B2 {VERSION}-test.b2{run}" \
        in proc.stdout
    assert "transform versions: A 1.5.4, B1 and B2 1.5.4+test" in proc.stdout


def test_the_first_stack_is_an_operators_directory_configured_by_its_env_file(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    proc = fake.run(COMPOSE_PROJECT_NAME="db", PEOPLE_PUBS_IMAGE="caller:image", COMPOSE_FILE="/elsewhere.yaml")
    assert proc.returncode == 1
    run = _names(fake)[0][1:]
    application = f"pubdex-release:{VERSION}-test.a{run}"
    # The deployment files, taken out of A's application image with the documented command.
    [taken] = [call for call in fake.calls() if call[:1] == ["run"]]
    assert taken == ["run", "--rm", application, "tar", "-cC", "/opt/pubdex", *DEPLOYMENT_FILES]
    # Every Compose call in the operator's directory, with nothing of the caller's environment.
    cwds = {line[1] for line in fake.lines() if line[0] == "compose-cwd"}
    [opdir] = cwds
    assert re.fullmatch(PROJECT_RE, Path(opdir).name) and Path(opdir).name.endswith("-test")
    compose_environments = [env for call, env in zip(fake.calls(), fake.environments())
                            if call[:1] == ["compose"] and call[1:2] != ["version"]]
    assert compose_environments and all(set(env) <= ISOLATED_ENVIRONMENT for env in compose_environments)
    for call in fake.compose_calls():
        assert not {"-p", "--project-name", "-f", "--file", "--env-file"} & set(call), call
    # The run label on every service, volume and network of the compose.yaml in use.
    override = (fake.state / "override-seen").read_text()
    label = f'labels: {{pubdex.upgrade-acceptance.run: "{run}"}}'
    for name in ("bootstrap", "postgres", "schema-upgrade", "api", "ingest", "demo", "postgres-data", "initdb",
                 "default"):
        assert f"\n  {name}: {{{label}}}\n" in override, name
    assert override.count("labels:") == 9


def test_every_docker_command_uses_the_runs_own_client_configuration(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    proc = fake.run(DOCKER_CONTEXT="caller-context")
    assert proc.returncode == 1
    environments = fake.environments()
    configs = {env.get("DOCKER_CONFIG") for env in environments[1:]}
    [config] = configs
    assert Path(config).parent.parent == fake.workdirs and Path(config).name == "docker"
    assert all("DOCKER_CONTEXT" not in env for env in environments[1:])
    assert {env.get("DOCKER_HOST") for env in environments[1:]} == {"unix:///var/run/stub-docker.sock"}
    for env in fake.environments("release-env"):
        assert env["DOCKER_CONFIG"] == config and "DOCKER_CONTEXT" not in env


def test_a_project_that_exists_already_is_never_touched(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "collision")
    proc = fake.run()
    assert proc.returncode == 1 and "exist already; not touching them" in proc.stderr
    assert fake.compose_calls() == [] and not any(call[:1] == ["rm"] for call in fake.calls())


def test_no_password_reaches_the_output_unmasked(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    proc = fake.run()
    assert proc.returncode == 1 and "the superuser password is <password>" in proc.stderr
    assert not re.search(r"password is [0-9a-f]{20,}", proc.stdout + proc.stderr)


# --- failures, signals and the cleanup ----------------------------------------------------


def test_a_failed_build_removes_the_images_built_before_it(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "build-fails:b1")
    proc = fake.run()
    assert proc.returncode == 1 and "release.sh build --test b1" in proc.stderr
    assert len(fake.calls("release")) == 2 and fake.compose_calls() == []
    fake.assert_nothing_left()


def test_a_failure_after_the_start_removes_the_stack_the_images_and_the_directory(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-fails")
    proc = fake.run()
    assert proc.returncode == 1 and "FAIL: docker compose up --wait did not see a healthy stack" in proc.stderr
    downs = [call for call in fake.compose_calls() if "down" in call]
    assert downs and all("--volumes" in call for call in downs)
    assert "ok  no container, volume, network or image of run" in proc.stdout
    fake.assert_nothing_left()


def test_what_compose_leaves_behind_is_removed_by_the_run_label(tmp_path: Path) -> None:
    fake = Fake(tmp_path, "up-hangs")
    proc = fake.popen()
    fake.wait_for_up(proc)
    fake.mode("down-ineffective")
    os.killpg(proc.pid, signal.SIGTERM)
    proc.communicate(timeout=60)
    assert proc.returncode == 143
    fake.assert_nothing_left()


@pytest.mark.parametrize("mode, warning", [
    ("rm-ineffective,down-ineffective", "could not confirm that run"),
    ("image-rm-fails", "docker image ls -a --filter label=org.opencontainers.image.version="),
])
def test_a_cleanup_that_cannot_be_confirmed_names_the_leftovers(tmp_path: Path, mode: str, warning: str) -> None:
    fake = Fake(tmp_path, "up-hangs")
    proc = fake.popen()
    fake.wait_for_up(proc)
    fake.mode(mode)
    os.killpg(proc.pid, signal.SIGINT)
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 130 and warning in err
    assert "docker ps -a --filter label=pubdex.upgrade-acceptance.run=" in err


@pytest.mark.parametrize("signum, status", [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)],
                         ids=["INT", "TERM", "HUP"])
def test_a_signal_during_the_start_cleans_up_with_its_own_status(tmp_path: Path, signum: int, status: int) -> None:
    fake = Fake(tmp_path, "up-hangs")
    proc = fake.popen()
    fake.wait_for_up(proc)
    os.killpg(proc.pid, signum)
    proc.communicate(timeout=60)
    assert proc.returncode == status
    fake.assert_nothing_left()


# --- what the run types --------------------------------------------------------------------


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))


def test_the_backup_restore_and_deployment_files_are_the_documented_commands() -> None:
    guide = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    code = _code(SCRIPT)
    # The backup and the restore of "Backup and restore".
    assert "(umask 077 && docker compose exec -T postgres pg_dump -U postgres -Fc people_db > people_db.dump)" in guide
    assert "umask 077 && bounded 300 isolated docker compose exec -T postgres pg_dump -U postgres -Fc people_db" in code
    assert "> people_db.dump" in code
    restore = [
        ("docker compose up --wait postgres", "dc up --wait --wait-timeout \"$WAIT_SECONDS\" postgres"),
        ("docker compose stop api", "dc stop api"),
        ("docker compose exec -T postgres dropdb -U postgres --force people_db",
         "dc exec -T postgres dropdb -U postgres --force people_db"),
        ("docker compose exec -T postgres createdb -U postgres people_db",
         "dc exec -T postgres createdb -U postgres people_db"),
        ("docker compose exec -T postgres pg_restore -U postgres -d people_db --single-transaction --exit-on-error "
         "< people_db.dump", "docker compose exec -T postgres pg_restore -U postgres -d people_db"),
    ]
    for documented, typed in restore:
        assert documented in guide, documented
        assert typed in code, typed
    assert "--single-transaction --exit-on-error < people_db.dump" in code
    # The deployment files out of the application image, as "Selecting a published image" takes them.
    assert "tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x" in guide
    assert ("docker run --rm \"$1\" tar -cC /opt/pubdex compose.yaml create_secrets.sh set_passwords.sh | tar -x"
            in code)
    assert "bash create_secrets.sh" in guide and "isolated bash create_secrets.sh" in code


def test_nothing_is_published_and_no_shared_name_or_port_is_used() -> None:
    code = _code(SCRIPT)
    for forbidden in ("docker login", "docker push", "docker tag", "release.sh\" publish", "publish ",
                      "5432", "github.com", "gh "):
        assert forbidden not in code, forbidden
    assert re.findall(r'new_stack "([^"]+)"', code) == ["pubdex-upgrade-$RUN_TAG-test",
                                                         "pubdex-upgrade-$RUN_TAG-from-${version//./-}"]
    # The application's own check is shown by starting it without the schema upgrade, in step 4 alone.
    assert re.findall(r"\bdc ([^\n|>]*--no-deps[^\n|>]*)", code) == [
        "run --rm --no-deps -T api python -c '", "up --detach --no-deps api ",
        "run --rm --no-deps -T ingest python -m people_pubs.sync.fixture_demo "]


def test_the_upgrade_acceptance_files_are_in_this_repository() -> None:
    assert SCRIPT.is_file()
    for name in ("compatible.sql", "incompatible.sql", "transform-version-suffix"):
        assert (ROOT / "DB/tests/release_overlay" / name).is_file()
