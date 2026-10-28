"""The container files, offline: Dockerfile, .dockerignore, compose.yaml,
compose.postgres-port.yaml and the acceptance run's addition to compose.yaml,
DB/tests/compose.acceptance.yaml.

These pin the properties that a later edit could lose without any build or
start failing: inputs fixed by digest, an allowlist build context that holds
what the Dockerfile copies and nothing else, an unprivileged user, files
readable for it, no credential in the image or the Compose file, each
container with its own role's credential only, an image that carries the
schema bootstrap and the Compose files so that compose.yaml needs no
checkout, and the portable defaults -- loopback publication, no host port for
PostgreSQL, no fixed resource name.
Building the image and starting the stack are checked against Docker
itself, not here. compose.yaml is read by indentation, which its layout
keeps regular: no YAML parser is among the locked dependencies.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")
POSTGRES_PORT = (ROOT / "compose.postgres-port.yaml").read_text(encoding="utf-8")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
#: The one step after the last COPY that gives everything under /opt/pubdex
#: its modes, whatever the checkout's were.
MODES = ("RUN find /opt/pubdex -type d ! -perm 755 -exec chmod 755 {} + "
         "&& find /opt/pubdex -type f ! -perm 644 -exec chmod 644 {} +")
INIT_FILES = sorted(f"DB/init/{path.name}" for path in (ROOT / "DB" / "init").iterdir())
COMPOSE_FILES = ["compose.yaml", "compose.postgres-port.yaml"]
#: Next to the Compose files, so that a deployment can be set up and its
#: passwords changed from the image.
SETUP_FILES = [*COMPOSE_FILES, "create_secrets.sh", "set_passwords.sh"]
#: The scripts that fetch and install the locks during the build, mounted for
#: those steps and never copied into the image.
BUILD_SCRIPTS = ["DB/fetch_verified.py", "DB/install_pip.py", "DB/install_locked.py", "DB/locked_files.py"]
ALLOWLIST = {
    "DB/requirements-pip.txt",
    "DB/requirements-build.txt",
    "DB/requirements.txt",
    *BUILD_SCRIPTS,
    "DB/people_pubs/**/*.py",
    "DB/tests/fixtures/golden/README.md",
    "DB/tests/fixtures/golden/demo_roster_minimal.csv",
    "DB/tests/fixtures/golden/orcid_work_minimal.json",
    "DB/tests/fixtures/golden/crossref_work_minimal.json",
    *INIT_FILES,
    *SETUP_FILES,
    "LICENSE",
    "LICENSE-CC-BY-4.0",
}
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
#: The release image compose.yaml names when PEOPLE_PUBS_IMAGE is not set.
RELEASE_IMAGE = f"ghcr.io/ccu-ukn/pubdex:{VERSION}"
#: The release's PostgreSQL image, which compose.yaml names when
#: PEOPLE_PUBS_POSTGRES_IMAGE is not set.
RELEASE_POSTGRES_IMAGE = f"ghcr.io/ccu-ukn/pubdex-postgres:{VERSION}"
#: Where the bootstrap service mounts the volume it fills.
BOOTSTRAP_MOUNT = "/var/lib/pubdex/initdb"


def _instructions() -> List[List[str]]:
    """Each Dockerfile instruction as words, line continuations joined."""
    joined = re.sub(r"\\\n\s*", " ", DOCKERFILE)
    return [line.split() for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _instruction(name: str) -> List[List[str]]:
    return [words for words in _instructions() if words[0] == name]


def _labels() -> Dict[str, str]:
    joined = re.sub(r"\\\n\s*", " ", DOCKERFILE)
    return dict(re.findall(r'(\S+)="([^"]*)"', " ".join(re.findall(r"^LABEL (.*)$", joined, re.M))))


def _stages(dockerfile: str = DOCKERFILE) -> Dict[str, List[str]]:
    """Each stage's instructions, joined, by its name; the last stage, the
    image, as ""."""
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    stages: Dict[str, List[str]] = {}
    current = None
    for line in joined.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        words = line.split()
        if words[0] == "FROM":
            current = words[3] if len(words) > 3 and words[2] == "AS" else ""
            stages[current] = []
        elif current is not None:
            stages[current].append(" ".join(words))
    return stages


def _arg(name: str, dockerfile: str = DOCKERFILE) -> str:
    found = re.findall(rf"^ARG {name}=(\S+)$", dockerfile, re.M)
    assert len(found) == 1, name
    return found[0]


# --- Dockerfile -------------------------------------------------------------------

def test_the_base_image_is_fixed_by_digest_and_named_in_the_labels() -> None:
    # One base for every stage, named once, by digest.
    reference = _arg("PYTHON_BASE")
    assert DIGEST.search(reference), reference
    assert [words[1] for words in _instruction("FROM")] == ["${PYTHON_BASE}"] * 3
    assert list(_stages()) == ["wheels", "psycopg-c", ""]
    name, digest = reference.split("@")
    labels = _labels()
    assert labels["org.opencontainers.image.base.digest"] == digest
    assert labels["org.opencontainers.image.base.name"] == f"docker.io/library/{name}"
    assert name.startswith("python:3.11.") and name.endswith("-slim-trixie")


def test_the_image_runs_as_an_unprivileged_user() -> None:
    users = _instruction("USER")
    assert [words[1] for words in users] == ["10001:10001"]
    order = [words[0] for words in _instructions()]
    assert order.index("USER") < order.index("CMD")
    assert "--uid 10001" in DOCKERFILE and "--gid 10001" in DOCKERFILE
    assert "--shell /usr/sbin/nologin" in DOCKERFILE and "--no-create-home" in DOCKERFILE


def test_that_user_owns_nothing_but_the_empty_mount_point_of_the_bootstrap_volume() -> None:
    # Docker gives a new volume the owner of the directory it is first mounted
    # on: this one, in the bootstrap service of compose.yaml.
    instructions = " ".join(" ".join(words) for words in _instructions())
    grants = re.findall(r"chown|--owner|-o 10001", instructions)
    assert grants == ["-o 10001"], grants
    assert f"install -d -o 10001 -g 10001 -m 755 {BOOTSTRAP_MOUNT}" in instructions


def test_the_image_carries_the_schema_bootstrap_the_compose_files_and_their_scripts() -> None:
    # Every file of DB/init/ -- a new one included, which the allowlist has to
    # name -- next to the package as people_pubs.bootstrap expects it, and the
    # Compose files this image is released with, with the scripts that create
    # their secret files and give the database the files' passwords.
    sources = {}
    for words in _instruction("COPY"):
        for source in words[1:-1]:
            sources[source] = words[-1]
    assert {source: sources.get(source) for source in INIT_FILES} == {source: "/opt/pubdex/init/"
                                                                       for source in INIT_FILES}
    assert {source: sources.get(source) for source in SETUP_FILES} == {source: "/opt/pubdex/"
                                                                        for source in SETUP_FILES}


def test_every_copy_takes_allowlisted_files_from_the_build_context_as_roots() -> None:
    # No --chown, so COPY gives every file and directory to root; no --chmod,
    # whose symbolic modes only the builder of Docker Engine 28.0 and later
    # accepts: the Dockerfile keeps to what the builder of 25.0 understands,
    # although the deployment supports no Engine that old.
    assert _instruction("ADD") == [], "COPY only: ADD fetches and unpacks"
    copies = _instruction("COPY")
    assert copies
    for words in copies:
        assert not any(word.startswith("--") for word in words), words
        for source in words[1:-1]:
            if (ROOT / source).is_dir():
                admitted = f"{source}/**/*.py" in ALLOWLIST
            else:
                admitted = any(fnmatch.fnmatch(source, pattern) for pattern in ALLOWLIST)
            assert admitted, f"{source} is not in the build context"


def test_one_step_after_the_last_copy_gives_every_file_644_and_every_directory_755() -> None:
    # Whatever modes the checkout gives them -- 664 under a umask of 002, 755
    # for every file from Windows -- so that user 10001 reads and enters
    # everything under /opt/pubdex and writes nothing there. The acceptance
    # test checks the result in the built image (run_stack_acceptance.sh).
    instructions = [" ".join(words) for words in _instructions()]
    last_copy = max(i for i, line in enumerate(instructions) if line.startswith("COPY "))
    assert instructions[last_copy + 1] == MODES
    assert instructions.count(MODES) == 1
    assert instructions.index(MODES) < next(i for i, line in enumerate(instructions) if line.startswith("USER "))
    assert all(words[-1].startswith("/opt/pubdex/") for words in _instruction("COPY")), "nothing copied elsewhere"


#: The stage that downloads: every file of the three locks, by hash, with
#: DB/fetch_verified.py, and nothing else.
FETCH = ("RUN --mount=type=bind,source=DB,target=/opt/pubdex-build "
         "python -I /opt/pubdex-build/fetch_verified.py wheels --dest /wheelhouse --allow-source psycopg-c "
         "/opt/pubdex-build/requirements-pip.txt /opt/pubdex-build/requirements-build.txt "
         "/opt/pubdex-build/requirements.txt")
#: How the steps that install begin: no network, the scripts and the
#: verified files mounted, never copied.
OFFLINE = "RUN --network=none --mount=type=bind,source=DB,target=/opt/pubdex-build " \
          "--mount=type=bind,from=wheels,source=/wheelhouse,target=/wheelhouse "


def _install_step() -> str:
    return next(run for run in _stages()[""] if "/opt/pubdex/requirements.txt" in run and run.startswith("RUN"))


def test_the_downloads_are_a_stage_of_their_own_and_no_pip_downloads() -> None:
    stages = _stages()
    assert stages["wheels"] == [FETCH]
    joined = re.sub(r"\\\n\s*", " ", DOCKERFILE)
    instructions = " ".join(" ".join(words) for words in _instructions())
    assert instructions.count("fetch_verified.py") == 1
    for downloader in ("curl", "wget", "pip download", "pip install", "ADD "):
        assert downloader not in joined.replace("--mount", ""), downloader
    assert "requirements-dev" not in joined


def test_psycopg_c_is_compiled_from_the_verified_archive_in_a_stage_of_its_own() -> None:
    # The compiler and libpq's headers, then the build without network; the
    # image's stage installs Debian's libpq alone and takes the wheel by the
    # hash recorded at its build.
    compile_stage = _stages()["psycopg-c"]
    assert compile_stage[0].startswith("RUN apt-get update && apt-get install --yes --no-install-recommends "
                                       "gcc libc6-dev libpq-dev && rm -rf /var/lib/apt/lists/*"), compile_stage[0]
    assert compile_stage[1:] == [OFFLINE + "python -I /opt/pubdex-build/install_locked.py --wheelhouse /wheelhouse "
                                 "--network-isolation required --build-only /psycopg-c "
                                 "/opt/pubdex-build/requirements.txt"]
    image = " ".join(_stages()[""])
    assert "apt-get install --yes --no-install-recommends libpq5 &&" in image
    for build_only in ("gcc", "libc6-dev", "libpq-dev", "requirements-build", "--build-only"):
        assert build_only not in image, build_only
    assert "PSYCOPG_IMPL=c" in image, "psycopg fails rather than falling back to another implementation"


def test_pip_and_the_runtime_lock_are_installed_without_network_from_the_verified_files() -> None:
    # pip first, by DB/install_pip.py, which checks the wheel's hash before it
    # runs anything from it and has that pip install itself from the file;
    # then the runtime lock by DB/install_locked.py, in hash-checking mode,
    # without dependencies and from wheels only -- psycopg-c's by the hash of
    # its build -- all of it without network, which the scripts require.
    joined = re.sub(r"\\\n\s*", " ", DOCKERFILE)
    assert "COPY DB/requirements-pip.txt DB/requirements.txt /opt/pubdex/" in joined
    steps = [step.strip() for step in _install_step().split("&&")]
    assert steps[0] == (OFFLINE + "--mount=type=bind,from=psycopg-c,source=/psycopg-c,target=/psycopg-c "
                        "python /opt/pubdex-build/install_pip.py --wheelhouse /wheelhouse "
                        "--lock /opt/pubdex/requirements-pip.txt --network-isolation required"), steps[0]
    assert steps[1:3] == ["python -m pip uninstall --yes setuptools",
                          "python /opt/pubdex-build/install_locked.py --wheelhouse /wheelhouse --built /psycopg-c "
                          "--network-isolation required /opt/pubdex/requirements.txt"]
    # Every step that runs pip or the scripts has no network.
    for stage in _stages().values():
        for run in stage:
            if run.startswith("RUN") and re.search(r"pip|install_locked|install_pip", run) and run != FETCH:
                assert run.startswith("RUN --network=none "), run
    # The scripts are mounted, never copied, so the image does not hold them.
    copied = " ".join(" ".join(words[1:-1]) for words in _instruction("COPY"))
    assert not any(Path(script).name in copied for script in BUILD_SCRIPTS)
    for workflow in (".github", ".forgejo"):
        text = (ROOT / workflow / "workflows" / "db-tests.yml").read_text(encoding="utf-8")
        assert "./setup_venv.sh --python" in text, "CI fetches and installs with the same scripts"
    assert all(f"$ROOT/{script}" in (ROOT / "setup_venv.sh").read_text(encoding="utf-8")
               for script in BUILD_SCRIPTS if script != "DB/locked_files.py")


def test_the_base_images_setuptools_is_removed_before_the_lock_is_installed() -> None:
    steps = [step.strip() for step in _install_step().split("&&")]
    assert steps.index("python -m pip uninstall --yes setuptools") < next(
        index for index, step in enumerate(steps) if "install_locked.py" in step) < steps.index("python -m pip check")


ENSUREPIP_WHEELS = ("rm /usr/local/lib/python3.11/ensurepip/_bundled/pip-*.whl "
                    "/usr/local/lib/python3.11/ensurepip/_bundled/setuptools-*.whl")


def test_pip_checks_the_installed_packages_and_then_leaves_the_image_in_the_same_step() -> None:
    # The dependency check runs at build time, with the pip that installed
    # the lock; then pip and ensurepip's wheels are removed in the same RUN,
    # so that no layer a container sees holds pip or those wheels. The
    # acceptance test checks the built image (run_stack_acceptance.sh).
    install = _install_step()
    steps = [step.strip() for step in install.split("&&")]
    assert steps[-3:] == ["python -m pip check", "python -m pip uninstall --yes pip", ENSUREPIP_WHEELS]
    image = _stages()[""]
    assert all("pip" not in run for run in image[image.index(install) + 1:]), "no later step runs pip"
    commands = [" ".join(words) for words in _instructions() if words[0] in ("CMD", "ENTRYPOINT")]
    assert commands and not any("pip" in command for command in commands)


# Minimum versions of the runtime lock's dependencies. Regenerating the
# lock may move past them, never back below them. psycopg-c uses the
# system's libpq rather than a bundled client library.
FIXED_RELEASES = {
    "anyio": (4, 14, 2),
    "certifi": (2026, 10, 19),
    "h2": (4, 4, 1),
    "hpack": (4, 2, 0),
    "idna": (3, 15),
    "psycopg": (3, 3, 5),
}
# Minimum version of the pip that installs the hashed locks.
PIP_FIXED_RELEASE = (26, 2, 2)
#: psycopg-c's one file on the package index: its source archive.
PSYCOPG_C_SDIST = "29c568426ad61c1b702c7d73505c43ca2c9fc5d97a8431d8d9049e731e319ff8"


LOCKS = ["DB/requirements-pip.txt", "DB/requirements.txt", "DB/requirements-dev.txt", "DB/requirements-build.txt"]


def _lock_entries(lock: str) -> Dict[str, tuple]:
    """Each requirement of a lock: name -> (version, its hashes)."""
    text = (ROOT / lock).read_text(encoding="utf-8")
    lines = [line for line in re.sub(r"\s*\\\n\s*", " ", text).splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    entries = {}
    for line in lines:
        match = re.fullmatch(r"([a-z0-9-]+)(?:\[[a-z0-9,-]+\])?==(\S+)((?: --hash=sha256:[0-9a-f]{64})+)", line)
        assert match, f"{lock}: not an exact version with hashes: {line!r}"
        entries[match.group(1)] = (match.group(2), re.findall(r"[0-9a-f]{64}", match.group(3)))
    return entries


@pytest.mark.parametrize("lock", LOCKS)
def test_every_requirement_of_the_locks_has_an_exact_version_and_hashes(lock: str) -> None:
    # A lock regenerated without --generate-hashes would still install, by
    # version alone, in CI; the Dockerfile's --require-hashes would refuse it.
    entries = _lock_entries(lock)
    assert entries
    for name, (version, hashes) in entries.items():
        assert hashes and len(set(hashes)) == len(hashes), f"{lock}: {name}"


def test_the_locks_agree_and_the_pip_lock_holds_pip_alone() -> None:
    pip, runtime, development, build = (_lock_entries(lock) for lock in LOCKS)
    assert list(pip) == ["pip"]
    assert {name: development[name] for name in runtime} == runtime, "the same versions and hashes"
    assert "pip" not in runtime and "pip" not in development and "setuptools" not in runtime
    # One wheelhouse holds the files of all of them: a name they share has
    # one version and the same files.
    assert all(development[name] == entry for name, entry in build.items() if name in development)


def test_psycopg_uses_its_c_implementation_from_the_source_archive_with_locked_build_tools() -> None:
    # No psycopg-binary, whose wheels bundle their own client libraries;
    # psycopg-c's one file is its source archive, built against the system's
    # libpq; the build lock holds exactly what psycopg-c 3.3.6 builds with:
    # setuptools 83.0.0, which it pins, and wheel, with wheel's requirement.
    for lock in LOCKS:
        assert "psycopg-binary" not in _lock_entries(lock), lock
        assert not re.search(r"psycopg\[binary\]|psycopg[-_]binary", (ROOT / lock).read_text(encoding="utf-8")), lock
    for lock in ("DB/requirements.txt", "DB/requirements-dev.txt"):
        assert _lock_entries(lock)["psycopg-c"] == ("3.3.6", [PSYCOPG_C_SDIST]), lock
    assert "psycopg[c]==3.3.6" in (ROOT / "DB/requirements.in").read_text(encoding="utf-8")
    build = _lock_entries("DB/requirements-build.txt")
    assert sorted(build) == ["packaging", "setuptools", "wheel"]
    assert build["setuptools"][0] == "83.0.0"


@pytest.mark.parametrize("lock", ["DB/requirements.txt", "DB/requirements-dev.txt"])
def test_the_locks_keep_the_releases_that_fix_known_advisories(lock: str) -> None:
    pins = {name: version for name, (version, _hashes) in _lock_entries(lock).items()}
    for name, fixed in FIXED_RELEASES.items():
        assert tuple(int(part) for part in pins[name].split(".")) >= fixed, f"{lock}: {name}=={pins[name]}"


def test_the_pip_lock_keeps_the_release_whose_certifi_copy_is_fixed() -> None:
    version = _lock_entries("DB/requirements-pip.txt")["pip"][0]
    assert tuple(int(part) for part in version.split(".")) >= PIP_FIXED_RELEASE, f"pip=={version}"


def test_the_github_workflow_tests_with_the_python_release_of_the_image() -> None:
    # The interpreter CI tests with is the one the image ships, so that a
    # Python security release reaches both together.
    base = re.search(r"^ARG PYTHON_BASE=python:(\d+\.\d+\.\d+)-", (ROOT / "Dockerfile").read_text(encoding="utf-8"),
                     re.M)
    workflow = (ROOT / ".github/workflows/db-tests.yml").read_text(encoding="utf-8")
    assert base and re.search(rf'python-version: "{re.escape(base.group(1))}"', workflow), base and base.group(1)


def test_the_image_holds_no_credential_and_reads_no_dotenv_file() -> None:
    for words in _instruction("ARG") + _instruction("ENV"):
        for word in words[1:]:
            assert not re.search(r"PASSWORD|SECRET|TOKEN|API_KEY", word.split("=", 1)[0]), word
    joined = re.sub(r"\\\n\s*", " ", DOCKERFILE)
    env = dict(re.findall(r"(\w+)=(\S+)", " ".join(re.findall(r"^ENV (.*)$", joined, re.M))))
    assert env["PEOPLE_PUBS_SKIP_DOTENV"] == "1"
    assert env["PYTHONSAFEPATH"] == "1" and env["PYTHONPATH"] == "/opt/pubdex"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PEOPLE_PUBS_API_HOST"] == "0.0.0.0"


def test_the_default_command_is_the_read_service_and_the_labels_take_the_build_metadata() -> None:
    assert _instruction("CMD") == [["CMD", '["python",', '"-m",', '"people_pubs.api"]']]
    assert _instruction("ENTRYPOINT") == []
    labels = _labels()
    assert labels["org.opencontainers.image.version"] == "${PEOPLE_PUBS_VERSION}"
    assert labels["org.opencontainers.image.revision"] == "${PEOPLE_PUBS_REVISION}"
    assert labels["pubdex.transform-version"] == "${PEOPLE_PUBS_TRANSFORM_VERSION}"
    assert labels["org.opencontainers.image.licenses"] == "MIT AND CC-BY-4.0"
    assert "database schema bootstrap and Compose files" in labels["org.opencontainers.image.description"]
    assert "ARG PEOPLE_PUBS_REVISION=unknown" in DOCKERFILE, "a plain build claims no revision"


def test_the_release_version_is_one_semantic_version() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8")
    assert re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?\n", version)


# --- .dockerignore ----------------------------------------------------------------

def test_the_build_context_is_an_allowlist() -> None:
    rules = [line.strip() for line in DOCKERIGNORE.splitlines() if line.strip() and not line.startswith("#")]
    assert rules[0] == "*", "everything is excluded first"
    assert all(rule.startswith("!") for rule in rules[1:])
    assert {rule[1:] for rule in rules[1:]} == ALLOWLIST


# --- compose.yaml -----------------------------------------------------------------

def _lines(text: str) -> List[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _block(text: str, name: str, indent: int) -> List[str]:
    """The lines of the mapping entry name at indent, without the key line."""
    lines = _lines(text)
    head = re.compile(" " * indent + re.escape(name) + r":(\s|$)")
    for start, line in enumerate(lines):
        if head.match(line):
            body = []
            for following in lines[start + 1:]:
                if len(following) - len(following.lstrip()) <= indent:
                    break
                body.append(following)
            return body
    raise AssertionError(f"no {name!r} at indentation {indent}")


def _list(block: List[str], key: str) -> Optional[List[str]]:
    """The items of the list under key in a block, or None without the key."""
    for start, line in enumerate(block):
        if line.strip() == f"{key}:":
            indent = len(line) - len(line.lstrip())
            items = []
            for following in block[start + 1:]:
                if len(following) - len(following.lstrip()) <= indent:
                    break
                items.append(following.strip().lstrip("- ").strip('"'))
            return items
    return None


def _mapping(block: List[str], key: str) -> Dict[str, str]:
    items = _list(block, key) or []
    return dict(item.split(": ", 1) for item in items)


SERVICES = ("bootstrap", "postgres", "api", "ingest", "demo")


def postgres_text() -> str:
    return "\n".join(["  postgres:"] + _block(COMPOSE, "postgres", 2))


def test_the_stack_has_its_five_services_and_names_nothing_globally() -> None:
    services = [line.strip()[:-1] for line in _block(COMPOSE, "services", 0) if re.fullmatch(r"  [a-z]+:", line)]
    assert services == list(SERVICES)
    for forbidden in ("container_name", "privileged", "external:", "extra_hosts"):
        assert forbidden not in COMPOSE
    assert [line.strip() for line in _lines(COMPOSE) if "network_mode" in line] == ["network_mode: none"]
    assert not any(re.match(r"\s*name:", line) for line in _lines(COMPOSE)), \
        "no project, volume or network name is fixed"
    assert _block(COMPOSE, "volumes", 0) == ["  postgres-data:", "  initdb:"]


def test_compose_yaml_needs_no_checkout() -> None:
    # No bind mount: only the secret files come from the host, from a
    # directory the operator chooses; the schema comes from the image.
    assert "type: bind" not in COMPOSE and "DB/" not in "\n".join(_lines(COMPOSE))
    for service in SERVICES:
        for item in _list(_block(COMPOSE, service, 2), "volumes") or []:
            assert not item.startswith(("/", ".", "~", "$")), (service, item)
    relative = sorted({value for value in re.findall(r"\$\{[A-Z_]+:-(\.[^}]*)\}", COMPOSE)})
    assert relative == ["./secrets"], "the only relative default is the secrets directory next to compose.yaml"


def test_the_default_image_is_the_release_of_this_file() -> None:
    # The file cannot name the digest of the image built from its own commit,
    # whose labels name that commit: it names the release's version tag, and
    # a release record names the digest (DEPLOYMENT.md, "What identifies a
    # deployment").
    assert "  image: ${PEOPLE_PUBS_IMAGE:-" + RELEASE_IMAGE + "}" in _block(COMPOSE, "x-application-image", 0)
    assert COMPOSE.count("PEOPLE_PUBS_IMAGE:-") == 1


def test_the_bootstrap_service_fills_its_volume_from_the_image_alone() -> None:
    bootstrap = _block(COMPOSE, "bootstrap", 2)
    assert "    <<: *application-image" in bootstrap, "the image's hardening, and no database dependency"
    assert f'    command: ["python", "-m", "people_pubs.bootstrap", "{BOOTSTRAP_MOUNT}"]' in bootstrap
    assert '    restart: "no"' in bootstrap and "    network_mode: none" in bootstrap
    assert _list(bootstrap, "volumes") == [f"initdb:{BOOTSTRAP_MOUNT}"]
    for absent in ("secrets", "environment", "ports", "depends_on", "user"):
        assert not any(line.strip().startswith(f"{absent}:") for line in bootstrap), absent


def test_postgresql_is_pinned_published_nowhere_and_bootstraps_from_the_image() -> None:
    postgres = _block(COMPOSE, "postgres", 2)
    # The PostgreSQL image of the release, by the release's version tag as
    # the application image; postgres.Dockerfile builds it.
    image = next(line.split("image: ", 1)[1] for line in postgres if "image:" in line)
    assert image == "${PEOPLE_PUBS_POSTGRES_IMAGE:-" + RELEASE_POSTGRES_IMAGE + "}"
    assert COMPOSE.count("PEOPLE_PUBS_POSTGRES_IMAGE:-") == 1
    assert "    pull_policy: missing" in postgres
    assert _list(postgres, "ports") is None, "no host port by default"
    # The bootstrap volume read-only, and with nocopy, without which creating
    # this container would give the empty volume root as its owner.
    assert _list(postgres, "volumes") == ["postgres-data:/var/lib/postgresql/data", "type: volume",
                                          "source: initdb", "target: /docker-entrypoint-initdb.d",
                                          "read_only: true", "volume:", "nocopy: true"]
    depends = [line.strip() for line in _block(postgres_text(), "depends_on", 4)]
    assert depends == ["bootstrap:", "condition: service_completed_successfully"]
    assert _list(postgres, "secrets") == ["postgres_password", "app_readonly_password", "app_writer_password"]
    environment = _mapping(postgres, "environment")
    assert environment["POSTGRES_PASSWORD_FILE"] == "/run/secrets/postgres_password"
    assert environment["PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE"] == "/run/secrets/app_readonly_password"
    assert environment["PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE"] == "/run/secrets/app_writer_password"
    assert "POSTGRES_PASSWORD" not in environment
    health = " ".join(_block(postgres_text(), "healthcheck", 4))
    assert "pg_isready -q -h 127.0.0.1" in health and "rolpassword LIKE 'SCRAM-SHA-256$$%'" in health


def test_the_read_service_has_only_the_read_credential_and_a_loopback_port() -> None:
    api = _block(COMPOSE, "api", 2)
    assert "    <<: *application" in api
    assert _list(api, "secrets") == ["app_readonly_password"]
    environment = _mapping(api, "environment")
    assert environment["PEOPLE_PUBS_API_DSN"] == "host=postgres port=5432 dbname=people_db user=app_readonly"
    assert environment["PEOPLE_PUBS_API_DB_PASSWORD_FILE"] == "/run/secrets/app_readonly_password"
    assert _list(api, "ports") == ["${PEOPLE_PUBS_HTTP_ADDRESS:-127.0.0.1}:${PEOPLE_PUBS_HTTP_PORT:-8000}:8000"]
    assert '["CMD", "python", "-m", "people_pubs.api.probe"]' in "\n".join(api)


def test_ingestion_has_only_the_writer_credential_and_starts_only_on_request() -> None:
    ingestion = _block(COMPOSE, "x-ingestion", 0)
    assert _list(ingestion, "secrets") == ["app_writer_password"]
    environment = _mapping(ingestion, "environment")
    assert environment["PGUSER"] == "app_writer" and environment["PGHOST"] == "postgres"
    assert environment["PEOPLE_DB_PASSWORD_FILE"] == "/run/secrets/app_writer_password"
    assert "PGPASSWORD" not in environment
    for service in ("ingest", "demo"):
        block = _block(COMPOSE, service, 2)
        assert "    <<: *ingestion" in block
        assert _list(block, "profiles") == [service], "never started by up"
        assert _list(block, "secrets") is None and _list(block, "environment") is None


def test_the_application_containers_are_locked_down() -> None:
    application = _block(COMPOSE, "x-application-image", 0)
    # Pulled when Docker does not have it -- the one command of a published
    # image -- and never again: a tag moved later does not replace it.
    assert "  pull_policy: missing" in application
    for setting in ("  init: true", "  read_only: true"):
        assert setting in application
    assert _list(application, "cap_drop") == ["ALL"]
    assert _list(application, "security_opt") == ["no-new-privileges:true"]
    assert _list(application, "tmpfs") == ["/tmp"]
    assert _block(COMPOSE, "x-application", 0) == [
        "  <<: *application-image", "  depends_on:", "    postgres:", "      condition: service_healthy"]


def test_the_secret_files_come_from_one_configurable_directory() -> None:
    secrets = _block(COMPOSE, "secrets", 0)
    files = [line.strip().split("file: ", 1)[1] for line in secrets if "file:" in line]
    assert files == [f"${{PEOPLE_PUBS_SECRETS_DIR:-./secrets}}/{name}"
                     for name in ("postgres_password", "app_readonly_password", "app_writer_password")]
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/secrets/" in gitignore


def test_the_demonstration_adds_its_rule_rather_than_replacing_the_rules() -> None:
    demo = "\n".join(_block(COMPOSE, "demo", 2))
    assert "attribution_rules add --affiliation" in demo and "attribution_rules set" not in demo
    assert "--no-orcid-lookup" in demo, "no provider request"


def test_the_postgresql_port_is_published_only_on_request_and_on_loopback() -> None:
    ports = _list(_block(POSTGRES_PORT, "postgres", 2), "ports")
    assert ports == ["${PEOPLE_PUBS_POSTGRES_ADDRESS:-127.0.0.1}:${PEOPLE_PUBS_POSTGRES_PORT:?set "
                     "PEOPLE_PUBS_POSTGRES_PORT to the host port for PostgreSQL}:5432"]


ACCEPTANCE = (ROOT / "DB" / "tests" / "compose.acceptance.yaml").read_text(encoding="utf-8")


def _keys(block: List[str], indent: int) -> List[str]:
    return [line.strip().split(":", 1)[0] for line in block if len(line) - len(line.lstrip()) == indent]


def test_the_acceptance_run_tests_the_deployment_as_it_is_behind_a_network_boundary() -> None:
    # run_stack_acceptance.sh adds DB/tests/compose.acceptance.yaml to
    # compose.yaml: the run's label and the test's network boundary, and
    # nothing that would make it start another stack than the deployment.
    assert _keys(_lines(ACCEPTANCE), 0) == ["x-run-label", "services", "networks", "volumes"]
    assert _keys(_block(ACCEPTANCE, "services", 0), 2) == list(SERVICES)
    for service in SERVICES:
        expected = ["labels", "networks"] if service == "api" else ["labels"]
        assert _keys(_block(ACCEPTANCE, service, 2), 4) == expected, service
    assert _list(_block(ACCEPTANCE, "api", 2), "networks") == ["default", "published"]
    networks = _block(ACCEPTANCE, "networks", 0)
    assert _keys(networks, 2) == ["default", "published"]
    assert "    internal: true" in _block(ACCEPTANCE, "default", 2), "no route off the host for PostgreSQL and ingestion"
    assert "internal" not in "\n".join(_block(ACCEPTANCE, "published", 2))
    assert _keys(_block(ACCEPTANCE, "volumes", 0), 2) == ["postgres-data", "initdb"]
    assert _keys(_block(ACCEPTANCE, "postgres-data", 2), 4) == ["labels"]
    assert _keys(_block(ACCEPTANCE, "initdb", 2), 4) == ["labels"]
    assert "  pubdex.stack-acceptance.run: ${PEOPLE_PUBS_ACCEPTANCE_RUN:?" in ACCEPTANCE
    # The deployment itself keeps Docker's ordinary default network, so that
    # an operator's ingestion can reach the metadata providers.
    assert "networks:" not in COMPOSE and "internal" not in COMPOSE


@pytest.mark.parametrize("path", ["compose.yaml", "compose.postgres-port.yaml"])
def test_no_compose_value_is_a_literal_credential(path: str) -> None:
    for line in _lines((ROOT / path).read_text(encoding="utf-8")):
        key = line.strip().split(":", 1)[0]
        if re.search(r"PASSWORD|SECRET|TOKEN", key) and not key.endswith("_FILE"):
            raise AssertionError(f"{path}: {line.strip()}")


# --- postgres.Dockerfile ----------------------------------------------------------

POSTGRES_DOCKERFILE = (ROOT / "postgres.Dockerfile").read_text(encoding="utf-8")
POSTGRES_DOCKERIGNORE = (ROOT / "postgres.Dockerfile.dockerignore").read_text(encoding="utf-8")


def test_the_postgresql_images_inputs_are_fixed_by_digest_and_hash() -> None:
    for name in ("PYTHON_BASE", "DEBIAN_BASE", "POSTGRES_ENTRYPOINT_IMAGE"):
        assert DIGEST.search(_arg(name, POSTGRES_DOCKERFILE)), name
    assert _arg("PYTHON_BASE", POSTGRES_DOCKERFILE) == _arg("PYTHON_BASE"), "one Python base for both images"
    assert re.fullmatch(r"16\.\d+", _arg("PG_VERSION", POSTGRES_DOCKERFILE))
    assert re.fullmatch(r"[0-9a-f]{64}", _arg("PG_SHA256", POSTGRES_DOCKERFILE))
    froms = [line.split()[1] for line in POSTGRES_DOCKERFILE.splitlines() if line.startswith("FROM ")]
    assert froms == ["${PYTHON_BASE}", "${POSTGRES_ENTRYPOINT_IMAGE}", "${DEBIAN_BASE}", "build", "${DEBIAN_BASE}"]
    # The source by hash, over HTTPS, with the repository's downloader.
    fetch = next(run for run in _stages(POSTGRES_DOCKERFILE)["source"] if run.startswith("RUN"))
    assert 'fetch_verified.py url --sha256 "${PG_SHA256}" "https://ftp.postgresql.org/pub/source/' in fetch
    labels = dict(re.findall(r'(\S+)="([^"]*)"', " ".join(re.findall(
        r"^LABEL (.*)$", re.sub(r"\\\n\s*", " ", POSTGRES_DOCKERFILE), re.M))))
    assert labels["org.opencontainers.image.base.digest"] == _arg("DEBIAN_BASE", POSTGRES_DOCKERFILE).split("@")[1]
    assert labels["pubdex.postgresql-version"] == "${PG_VERSION}"
    assert labels["org.opencontainers.image.revision"] == "${PEOPLE_PUBS_REVISION}"
    assert "ARG PEOPLE_PUBS_REVISION=unknown" in POSTGRES_DOCKERFILE, "a plain build claims no revision"


def test_postgresql_is_compiled_without_xml_and_without_network() -> None:
    build = " ".join(_stages(POSTGRES_DOCKERFILE)["build"])
    compile_step = next(run for run in _stages(POSTGRES_DOCKERFILE)["build"] if "./configure" in run)
    assert compile_step.startswith("RUN --network=none ")
    for option in ("--without-libxml", "--without-libxslt", "--enable-option-checking=fatal", "--with-openssl",
                   "--with-icu", "--with-lz4", "--with-zstd", "--with-system-tzdata=/usr/share/zoneinfo"):
        assert option in compile_step, option
    assert not re.search(r"--with-libx(ml|slt)\b(?!\*)", compile_step)
    assert "libxml2-dev" not in build and "libxslt" not in build.replace("--without-libxslt", "")
    # The extensions DB/init/ creates are built; xml2 is not.
    created = sorted(set(re.findall(r"CREATE EXTENSION IF NOT EXISTS (\w+)", "".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "DB" / "init").glob("*.sql")))))
    assert created == ["citext", "pg_trgm", "pgcrypto", "unaccent"]
    assert f"for extension in {' '.join(['citext', 'pgcrypto', 'unaccent', 'pg_trgm'])}; do" in compile_step
    assert "test ! -e share/extension/xml2.control" in compile_step


def test_the_postgresql_image_checks_that_no_xml_library_is_in_it() -> None:
    image = " ".join(_stages(POSTGRES_DOCKERFILE)[""])
    assert "grep -E '^lib(xml2|xslt1)'" in image and "grep -E 'libxml|libxslt|not found'" in image
    assert "*--with-libxml*|*--with-libxslt*" in image
    assert "| sha256sum --check --strict" in image, "the entrypoint scripts are those of the digest"
    for kept in ("ENTRYPOINT [\"docker-entrypoint.sh\"]", "STOPSIGNAL SIGINT", "VOLUME /var/lib/postgresql/data",
                 "PGDATA=/var/lib/postgresql/data", "--gid 999", "--uid 999", "--mode 3777 /var/run/postgresql"):
        assert kept in image, kept
    assert "apt-get install --yes --no-install-recommends gosu libicu76 liblz4-1 libreadline8t64" in image


ENTRYPOINT_LICENSE = "third_party/docker-library-postgres/LICENSE"


def test_the_postgresql_images_build_context_is_an_allowlist_of_the_downloader() -> None:
    rules = [line.strip() for line in POSTGRES_DOCKERIGNORE.splitlines() if line.strip() and not line.startswith("#")]
    assert rules == ["*", "!DB/fetch_verified.py", "!DB/locked_files.py", f"!{ENTRYPOINT_LICENSE}"]
    # Of the checkout, only the licence notice is copied, into the stage that
    # checks it; the image takes every file from a stage.
    copies = re.findall(r"^COPY (?!--from=).*$", POSTGRES_DOCKERFILE, re.M)
    assert copies == [f"COPY {ENTRYPOINT_LICENSE} /src/docker-library-postgres-LICENSE"]
    assert not any(line.startswith("COPY ") and not line.startswith("COPY --from=")
                   for line in _stages(POSTGRES_DOCKERFILE)[""])


def test_the_postgresql_image_carries_the_notices_its_licences_ask_for() -> None:
    # PostgreSQL's licence and the MIT licence of the entrypoint scripts ask
    # every copy to carry their notices. The latter's text is in the
    # repository, as upstream has it at a fixed commit, so that the build
    # downloads nothing for it, and the build checks it by hash.
    image = " ".join(_stages(POSTGRES_DOCKERFILE)[""])
    assert ("COPY --from=build /usr/src/postgresql/COPYRIGHT /usr/share/doc/pubdex-postgres/PostgreSQL-COPYRIGHT"
            in image)
    assert ("COPY --from=source /src/docker-library-postgres-LICENSE "
            "/usr/share/doc/pubdex-postgres/docker-entrypoint-LICENSE") in image
    expected = _arg("ENTRYPOINT_LICENSE_SHA256", POSTGRES_DOCKERFILE)
    assert hashlib.sha256((ROOT / ENTRYPOINT_LICENSE).read_bytes()).hexdigest() == expected
    source = _stages(POSTGRES_DOCKERFILE)["source"]
    check = next(run for run in source if run.startswith("RUN"))
    assert check.startswith("RUN --mount=type=bind,source=DB,target=/opt/pubdex-build printf '%s %s\\n' "
                            '"${ENTRYPOINT_LICENSE_SHA256}" /src/docker-library-postgres-LICENSE '
                            "| sha256sum --check --strict && chmod 0644 /src/docker-library-postgres-LICENSE && ")
    assert "printf '%s  %s\\n' \"${ENTRYPOINT_LICENSE_SHA256}\"" in POSTGRES_DOCKERFILE, "sha256sum's format"
    assert "ARG ENTRYPOINT_LICENSE_SHA256" in source
    # No step of the build fetches anything from a forge: the only download is
    # PostgreSQL's source, from postgresql.org.
    assert check.count("fetch_verified.py") == 1
    assert "github" not in POSTGRES_DOCKERFILE.lower()
    assert "dcf8ac73e67253ef0c7174418c61850ab4a11b58" in POSTGRES_DOCKERFILE, "the notice's provenance"
