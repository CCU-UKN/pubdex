"""setup_cluster_tools.sh and its pins in DB/lib/cluster_tools.sh, offline.

The pins are read as the scripts read them, by sourcing the library in Bash,
and checked for form: a release number, an HTTPS download for linux/amd64
from the host each project documents, a SHA-256 and a size limit for every
tool, the one member taken from helm's archive, and the two kind node images
by digest -- the newest supported Kubernetes release and the floor, the
oldest that Chart.yaml's kubeVersion admits -- each within one minor of
kubectl.

The script itself runs from a scratch directory standing in for the
repository root. A stub DB/fetch_verified.py serves synthetic files from a
local directory, as the real one would after checking their hashes, and
records its calls; the pin file is the real one with those files' hashes in
place of the real ones; the synthetic tools print a version and record the
environment they ran in; and a stub uname names the platform. That proves,
without network, that the script installs exactly the three files under
.tools/ and writes nothing anywhere else, checks files already in place
instead of downloading them again and never uses one that changed, refuses a
missing or malformed pin, any platform but Linux on x86_64 and a tool
reporting another version, and takes from helm's archive only the pinned
member, as a regular file under exactly its name, once, with its pinned hash.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "setup_cluster_tools.sh"
LIBRARY = ROOT / "DB" / "lib" / "cluster_tools.sh"
BASH = shutil.which("bash") or "/bin/bash"
TOOLS = ("helm", "kubectl", "kind")


def _read_pins(library: Path = LIBRARY) -> Dict[str, str]:
    proc = subprocess.run(
        [BASH, "-c", 'set -euo pipefail; source "$0"; for n in "${!PINNED_@}"; do printf "%s=%s\\n" "$n" "${!n}"; done',
         str(library)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in proc.stdout.splitlines())


PINS = _read_pins()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version(tool: str) -> str:
    return PINS[f"PINNED_{tool.upper()}_VERSION"]


def _served_name(tool: str) -> str:
    return PINS[f"PINNED_{tool.upper()}_URL"].rsplit("/", 1)[1]


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------

def test_the_pins_name_one_linux_amd64_download_per_tool() -> None:
    assert set(PINS) == {
        *(f"PINNED_{tool.upper()}_{field}" for tool in TOOLS for field in ("VERSION", "URL", "SHA256", "MAX_BYTES")),
        "PINNED_HELM_MEMBER", "PINNED_HELM_MEMBER_SHA256", "PINNED_HELM_MEMBER_MAX_BYTES", "PINNED_KIND_NODE_IMAGE",
        "PINNED_KIND_FLOOR_NODE_IMAGE"}
    for tool in TOOLS:
        assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", _version(tool)), tool
    helm, kubectl, kind = (_version(tool) for tool in TOOLS)
    # The download address each project documents, never a forge's.
    assert PINS["PINNED_HELM_URL"] == f"https://get.helm.sh/helm-v{helm}-linux-amd64.tar.gz"
    assert PINS["PINNED_KUBECTL_URL"] == f"https://dl.k8s.io/release/v{kubectl}/bin/linux/amd64/kubectl"
    assert PINS["PINNED_KIND_URL"] == f"https://kind.sigs.k8s.io/dl/v{kind}/kind-linux-amd64"
    hashes = [PINS[f"PINNED_{tool.upper()}_SHA256"] for tool in TOOLS] + [PINS["PINNED_HELM_MEMBER_SHA256"]]
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes), hashes
    assert len(set(hashes)) == 4
    limits = [PINS[f"PINNED_{tool.upper()}_MAX_BYTES"] for tool in TOOLS] + [PINS["PINNED_HELM_MEMBER_MAX_BYTES"]]
    assert all(re.fullmatch(r"[1-9][0-9]*", value) and 2**20 <= int(value) <= 2**28 for value in limits), limits
    assert PINS["PINNED_HELM_MEMBER"] == "linux-amd64/helm"


def _node_release(pin: str) -> Tuple[int, int]:
    match = re.fullmatch(r"kindest/node:v([0-9]+)\.([0-9]+)\.[0-9]+@sha256:[0-9a-f]{64}", PINS[pin])
    assert match, PINS[pin]
    return int(match.group(1)), int(match.group(2))


@pytest.mark.parametrize("pin", ["PINNED_KIND_NODE_IMAGE", "PINNED_KIND_FLOOR_NODE_IMAGE"])
def test_each_node_image_is_pinned_by_digest_within_kubectls_skew(pin: str) -> None:
    major, minor = _node_release(pin)
    kubectl_major, kubectl_minor = (int(part) for part in _version("kubectl").split(".")[:2])
    # kubectl supports a server within one minor release of its own.
    assert major == kubectl_major and abs(minor - kubectl_minor) <= 1


def test_the_floor_node_image_is_the_oldest_release_the_chart_admits() -> None:
    chart = (ROOT / "charts" / "pubdex" / "Chart.yaml").read_text(encoding="utf-8")
    floor = re.search(r'^kubeVersion: ">=([0-9]+)\.([0-9]+)\.0-0"$', chart, re.M)
    assert floor, "Chart.yaml's kubeVersion is not a plain floor"
    assert _node_release("PINNED_KIND_FLOOR_NODE_IMAGE") == (int(floor.group(1)), int(floor.group(2)))
    assert _node_release("PINNED_KIND_NODE_IMAGE") > _node_release("PINNED_KIND_FLOOR_NODE_IMAGE")
    assert PINS["PINNED_KIND_NODE_IMAGE"].split("@")[1] != PINS["PINNED_KIND_FLOOR_NODE_IMAGE"].split("@")[1]


def test_the_pins_are_read_only_and_name_each_tools_path() -> None:
    script = ('source "$0"; declare -p PINNED_HELM_SHA256 PINNED_KIND_NODE_IMAGE | cut -d" " -f2; '
              'for tool in helm kubectl kind; do cluster_tool_path /checkout "$tool"; done; '
              'status=0; cluster_tool_path /checkout docker || status=$?; echo "other: $status"')
    proc = subprocess.run([BASH, "-c", script, str(LIBRARY)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "-r", "-r",
        f"/checkout/.tools/helm-{_version('helm')}/helm",
        f"/checkout/.tools/kubectl-{_version('kubectl')}/kubectl",
        f"/checkout/.tools/kind-{_version('kind')}/kind",
        "other: 2"]


# ---------------------------------------------------------------------------
# The script, from a scratch checkout
# ---------------------------------------------------------------------------

STUB_FETCH = r'''"""Stands in for DB/fetch_verified.py url: serves STUB_FILES/<the URL's last part>."""
import hashlib
import json
import os
import sys

args = sys.argv[1:]
with open(os.path.join(os.environ["STUB_STATE"], "fetch-calls"), "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
assert args[0] == "url" and args[1] == "--sha256" and args[3] == "--max-bytes" and len(args) == 7, args
sha256, url, target = args[2], args[5], args[6]
if "fetch-fails" in os.environ.get("STUB_MODE", ""):
    print("stub fetch: the download failed", file=sys.stderr)
    sys.exit(1)
with open(os.path.join(os.environ["STUB_FILES"], url.rsplit("/", 1)[1]), "rb") as served:
    data = served.read()
if "fetch-ignores-hash" not in os.environ.get("STUB_MODE", "") and hashlib.sha256(data).hexdigest() != sha256:
    print("stub fetch: another SHA-256", file=sys.stderr)
    sys.exit(1)
if os.path.exists(target):
    sys.exit(1)
with open(target, "wb") as out:
    out.write(data)
print(f"stub fetch: {os.path.basename(target)}")
'''

STUB_UNAME = r'''#!/bin/sh
case "$1" in
  -s) echo "${STUB_OS:-Linux}" ;;
  -m) echo "${STUB_MACHINE:-x86_64}" ;;
  *) echo "stub uname: unexpected $*" >&2; exit 2 ;;
esac
'''


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _tool(state: Path, name: str, *lines: str) -> bytes:
    """A synthetic tool: records its environment and arguments, prints lines."""
    return (f'#!/bin/sh\nenv > {shlex.quote(str(state / ("env-" + name)))}\n'
            f'printf "%s\\n" "$@" > {shlex.quote(str(state / ("args-" + name)))}\n'
            f"printf '%s\\n' {' '.join(shlex.quote(line) for line in lines)}\n").encode()


Entry = Tuple[tarfile.TarInfo, Optional[bytes]]


def _regular(name: str, data: bytes) -> Entry:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), 0o755
    return info, data


def _special(name: str, kind: bytes, target: str = "") -> Entry:
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.mode = kind, target, 0o755
    return info, None


def _archive(entries: Sequence[Entry]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


class Checkout:
    """A scratch repository root holding the script, a pin file naming the
    synthetic files' hashes, and the stub fetch_verified.py; the files it
    serves; the stub uname; and the state the stubs record."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        (self.root / "DB" / "lib").mkdir(parents=True)
        self.script = self.root / SCRIPT.name
        shutil.copy2(SCRIPT, self.script)
        (self.root / "DB" / "fetch_verified.py").write_text(STUB_FETCH, encoding="utf-8")
        self.files, self.bin, self.state = tmp_path / "served", tmp_path / "bin", tmp_path / "state"
        for directory in (self.files, self.bin, self.state):
            directory.mkdir()
        _executable(self.bin / "uname", STUB_UNAME)
        self.pins: Dict[str, Optional[str]] = {}
        self.helm = _tool(self.state, "helm", f"v{_version('helm')}")
        self.contents = {
            "helm": self.helm,
            "kubectl": _tool(self.state, "kubectl", f"Client Version: v{_version('kubectl')}", "Kustomize Version: v5.8.1"),
            "kind": _tool(self.state, "kind", f"kind v{_version('kind')} go1.26.7 linux/amd64"),
        }
        self.serve("kubectl", self.contents["kubectl"])
        self.serve("kind", self.contents["kind"])
        self.serve_helm([_regular("linux-amd64/helm", self.helm), _regular("linux-amd64/LICENSE", b"licence\n"),
                         _regular("linux-amd64/README.md", b"readme\n")])
        self.pins["PINNED_HELM_MEMBER_SHA256"] = _sha256(self.helm)

    def serve(self, tool: str, data: bytes) -> None:
        (self.files / _served_name(tool)).write_bytes(data)
        self.pins[f"PINNED_{tool.upper()}_SHA256"] = _sha256(data)

    def serve_helm(self, entries: Sequence[Entry]) -> None:
        self.serve("helm", _archive(entries))

    def write_pins(self) -> None:
        text = LIBRARY.read_text(encoding="utf-8")
        for name, value in self.pins.items():
            pattern = re.compile(rf"^readonly {name}=.*$", re.M)
            assert len(pattern.findall(text)) == 1, name
            text = pattern.sub(lambda _: "" if value is None else f"readonly {name}={shlex.quote(value)}", text)
        (self.root / "DB" / "lib" / "cluster_tools.sh").write_text(text, encoding="utf-8")

    def run(self, *args: str, mode: str = "", **extra: str) -> subprocess.CompletedProcess:
        self.write_pins()
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": str(self.tmp / "caller-home"),
            "STUB_STATE": str(self.state),
            "STUB_FILES": str(self.files),
            "STUB_MODE": mode,
            # What the caller's environment holds never reaches the tools.
            "KUBECONFIG": str(self.tmp / "caller-kubeconfig"),
            "HELM_CONFIG_HOME": str(self.tmp / "caller-helm"),
            "CALLER_MARK": "1",
        }
        env.update(extra)
        return subprocess.run([str(self.script), *args], cwd=self.tmp, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)

    def fetches(self) -> List[List[str]]:
        log = self.state / "fetch-calls"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def path(self, tool: str) -> Path:
        return self.root / ".tools" / f"{tool}-{_version(tool)}" / tool

    def outside_tools(self) -> List[str]:
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")
                      if path.relative_to(self.root).parts[0] != ".tools")

    def under_tools(self) -> List[str]:
        tools = self.root / ".tools"
        return sorted(str(path.relative_to(tools)) for path in tools.rglob("*")) if tools.exists() else []


@pytest.fixture
def checkout(tmp_path: Path) -> Checkout:
    return Checkout(tmp_path)


def test_it_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_a_first_run_installs_the_three_pinned_tools_under_tools_only(checkout: Checkout) -> None:
    checkout.write_pins()
    before = checkout.outside_tools()
    proc = checkout.run()
    assert proc.returncode == 0, proc.stderr
    assert checkout.outside_tools() == before, "nothing is written outside .tools/, no __pycache__ either"
    assert checkout.under_tools() == sorted(
        f"{tool}-{_version(tool)}{suffix}" for tool in TOOLS for suffix in ("", f"/{tool}")), "the temporary directory is gone"
    for tool in TOOLS:
        assert checkout.path(tool).read_bytes() == checkout.contents[tool]
        assert stat.S_IMODE(checkout.path(tool).stat().st_mode) == 0o755
    # One download per tool: the pinned URL, hash and size limit, into the
    # temporary directory under .tools/.
    fetches = checkout.fetches()
    assert [call[:6] for call in fetches] == [
        ["url", "--sha256", checkout.pins[f"PINNED_{tool.upper()}_SHA256"], "--max-bytes",
         PINS[f"PINNED_{tool.upper()}_MAX_BYTES"], PINS[f"PINNED_{tool.upper()}_URL"]] for tool in TOOLS]
    for tool, call in zip(TOOLS, fetches):
        target = Path(call[6])
        assert target.parent.parent == checkout.root / ".tools" and target.parent.name.startswith(".setup.")
        assert target.name == f"{tool}-download"
    for tool in TOOLS:
        assert f"v{_version(tool)}" in proc.stdout and f".tools/{tool}-{_version(tool)}/{tool}" in proc.stdout
    assert proc.stdout.count("(downloaded, SHA-256 checked)") == 3
    assert f"kind node images: {PINS['PINNED_KIND_NODE_IMAGE']}" in proc.stdout
    assert f"{PINS['PINNED_KIND_FLOOR_NODE_IMAGE']} (the oldest supported release)" in proc.stdout
    assert str(checkout.root) not in proc.stdout, "paths are reported relative to the checkout"


def test_the_tools_report_their_versions_without_the_callers_environment(checkout: Checkout) -> None:
    proc = checkout.run()
    assert proc.returncode == 0, proc.stderr
    assert (checkout.state / "args-helm").read_text().splitlines() == ["version", "--template", "{{.Version}}"]
    assert (checkout.state / "args-kubectl").read_text().splitlines() == ["version", "--client"]
    assert (checkout.state / "args-kind").read_text().splitlines() == ["version"]
    for tool in TOOLS:
        env = dict(line.split("=", 1) for line in (checkout.state / f"env-{tool}").read_text().splitlines() if "=" in line)
        assert not any(name.startswith(("STUB_", "CALLER_")) for name in env), env
        scratch = str(checkout.root / ".tools" / ".setup.")
        for name in ("KUBECONFIG", "HOME", "HELM_CONFIG_HOME", "HELM_CACHE_HOME", "HELM_DATA_HOME"):
            assert env[name].startswith(scratch), (tool, name, env[name])
        assert not Path(env["KUBECONFIG"]).exists()


def test_a_second_run_checks_the_files_instead_of_downloading_them(checkout: Checkout) -> None:
    assert checkout.run().returncode == 0
    checkout.path("kind").chmod(0o644)
    proc = checkout.run()
    assert proc.returncode == 0, proc.stderr
    assert len(checkout.fetches()) == 3, "no download on the second run"
    assert proc.stdout.count("(in place, SHA-256 checked)") == 3
    assert stat.S_IMODE(checkout.path("kind").stat().st_mode) == 0o755


@pytest.mark.parametrize("tool", TOOLS)
def test_a_file_in_place_that_changed_is_neither_used_nor_replaced(checkout: Checkout, tool: str) -> None:
    assert checkout.run().returncode == 0
    changed = checkout.contents[tool] + b"# changed\n"
    checkout.path(tool).write_bytes(changed)
    proc = checkout.run()
    assert proc.returncode == 1
    assert f".tools/{tool}-{_version(tool)}/{tool} has the SHA-256 {_sha256(changed)}, not the pinned" in proc.stderr
    assert f"remove .tools/{tool}-{_version(tool)} and run setup_cluster_tools.sh again" in proc.stderr
    assert checkout.path(tool).read_bytes() == changed
    assert len(checkout.fetches()) == 3


@pytest.mark.parametrize(
    "name, value",
    [
        ("PINNED_KUBECTL_SHA256", None),
        ("PINNED_KUBECTL_SHA256", ""),
        ("PINNED_KUBECTL_SHA256", "C04487523E7AA7D1493F7808174C899DB93D7A9CF57531420CD76B9BD415F8F4"),
        ("PINNED_KUBECTL_SHA256", "c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f"),
        ("PINNED_KUBECTL_SHA256", "g04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4"),
        ("PINNED_HELM_SHA256", None),
        ("PINNED_HELM_MEMBER_SHA256", "sha256:" + "0" * 64),
        ("PINNED_KIND_SHA256", "aee61515 61422756"),
        ("PINNED_HELM_VERSION", "4.3"),
        ("PINNED_KIND_VERSION", None),
        ("PINNED_KIND_URL", "http://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64"),
        ("PINNED_KIND_URL", "https://kind.sigs.k8s.io/dl/latest/kind-linux-amd64"),
        ("PINNED_KUBECTL_MAX_BYTES", "0"),
        ("PINNED_KUBECTL_MAX_BYTES", "64M"),
        ("PINNED_HELM_MEMBER_MAX_BYTES", None),
        ("PINNED_HELM_MEMBER", "/linux-amd64/helm"),
        ("PINNED_HELM_MEMBER", "../linux-amd64/helm"),
        ("PINNED_HELM_MEMBER", "linux-amd64/./helm"),
        ("PINNED_HELM_MEMBER", "linux-amd64//helm"),
        ("PINNED_KIND_NODE_IMAGE", "kindest/node:v1.37.0"),
        ("PINNED_KIND_NODE_IMAGE", None),
        ("PINNED_KIND_FLOOR_NODE_IMAGE", "kindest/node:v1.35.8"),
        ("PINNED_KIND_FLOOR_NODE_IMAGE", "kindest/node:latest@sha256:" + "0" * 64),
        ("PINNED_KIND_FLOOR_NODE_IMAGE", None),
    ],
)
def test_a_missing_or_malformed_pin_is_refused_before_anything_is_written(
        checkout: Checkout, name: str, value: Optional[str]) -> None:
    checkout.pins[name] = value
    proc = checkout.run()
    assert proc.returncode == 1
    assert f"{name} in DB/lib/cluster_tools.sh is missing or malformed" in proc.stderr \
        or (name.endswith("_URL") and f"{name} in DB/lib/cluster_tools.sh does not name version" in proc.stderr), \
        proc.stderr
    assert checkout.fetches() == [] and not (checkout.root / ".tools").exists()


def test_a_pin_from_the_callers_environment_is_ignored(checkout: Checkout) -> None:
    sha256 = checkout.pins["PINNED_KIND_SHA256"]
    assert sha256
    checkout.pins["PINNED_KIND_SHA256"] = None
    proc = checkout.run(PINNED_KIND_SHA256=sha256)
    assert proc.returncode == 1
    assert "PINNED_KIND_SHA256 in DB/lib/cluster_tools.sh is missing or malformed: ''" in proc.stderr
    assert checkout.fetches() == []


@pytest.mark.parametrize("system, machine", [("Darwin", "x86_64"), ("Linux", "aarch64"), ("Linux", "i686"),
                                             ("FreeBSD", "amd64")])
def test_only_linux_on_x86_64_is_accepted(checkout: Checkout, system: str, machine: str) -> None:
    proc = checkout.run(STUB_OS=system, STUB_MACHINE=machine)
    assert proc.returncode == 1
    assert f"only Linux on x86_64 is supported, and this is {system} on {machine}" in proc.stderr
    assert checkout.fetches() == [] and not (checkout.root / ".tools").exists()


HELM_STUB = b"#!/bin/sh\necho v0\n"


@pytest.mark.parametrize(
    "case, entries, message",
    [
        ("missing", lambda helm: [_regular("linux-amd64/LICENSE", b"licence\n")], "it has no member linux-amd64/helm"),
        ("symbolic link", lambda helm: [_special("linux-amd64/helm", tarfile.SYMTYPE, "/bin/sh")],
         "its member linux-amd64/helm is not a regular file"),
        ("hard link", lambda helm: [_regular("linux-amd64/LICENSE", helm),
                                    _special("linux-amd64/helm", tarfile.LNKTYPE, "linux-amd64/LICENSE")],
         "its member linux-amd64/helm is not a regular file"),
        ("directory", lambda helm: [_special("linux-amd64/helm", tarfile.DIRTYPE)],
         "its member linux-amd64/helm is not a regular file"),
        ("absolute name", lambda helm: [_regular("/linux-amd64/helm", helm)],
         "its entry for linux-amd64/helm is named '/linux-amd64/helm'"),
        ("dotted name", lambda helm: [_regular("./linux-amd64/helm", helm)],
         "its entry for linux-amd64/helm is named './linux-amd64/helm'"),
        ("traversing name", lambda helm: [_regular("other/../linux-amd64/helm", helm)],
         "its entry for linux-amd64/helm is named 'other/../linux-amd64/helm'"),
        ("twice", lambda helm: [_regular("linux-amd64/helm", helm), _regular("linux-amd64/helm", HELM_STUB)],
         "it has 2 entries for linux-amd64/helm"),
        ("other content", lambda helm: [_regular("linux-amd64/helm", HELM_STUB)],
         f"its member linux-amd64/helm has the SHA-256 {_sha256(HELM_STUB)}, not the pinned"),
    ],
)
def test_only_the_pinned_member_of_helms_archive_is_taken(checkout: Checkout, case: str, entries, message: str) -> None:
    checkout.serve_helm(entries(checkout.helm))
    proc = checkout.run()
    assert proc.returncode == 1, case
    assert message in proc.stderr, (case, proc.stderr)
    assert "nothing was taken from it" in proc.stderr
    assert f"helm {_version('helm')} could not be taken from its archive" in proc.stderr
    assert checkout.under_tools() == [], "no file, directory or link is left under .tools/"


def test_a_member_larger_than_its_limit_is_refused(checkout: Checkout) -> None:
    checkout.pins["PINNED_HELM_MEMBER_MAX_BYTES"] = str(len(checkout.helm) - 1)
    proc = checkout.run()
    assert proc.returncode == 1
    assert f"its member linux-amd64/helm is larger than {len(checkout.helm) - 1} bytes" in proc.stderr
    assert checkout.under_tools() == []


def test_an_archive_that_is_not_one_is_refused(checkout: Checkout) -> None:
    checkout.serve("helm", b"not a gzip archive\n")
    proc = checkout.run()
    assert proc.returncode == 1
    assert "it cannot be read" in proc.stderr and checkout.under_tools() == []


def test_no_other_member_of_the_archive_is_ever_written(checkout: Checkout) -> None:
    checkout.serve_helm([
        _regular("../../escaped-relative", b"x\n"),
        _regular(str(checkout.tmp / "escaped-absolute"), b"x\n"),
        _special("linux-amd64/escaped-link", tarfile.SYMTYPE, str(checkout.tmp)),
        _regular("linux-amd64/helm", checkout.helm),
        _regular("linux-amd64/LICENSE", b"licence\n"),
    ])
    proc = checkout.run()
    assert proc.returncode == 0, proc.stderr
    assert not [path for path in checkout.tmp.rglob("escaped*")]
    assert checkout.path("helm").read_bytes() == checkout.helm
    assert sorted(path.name for path in checkout.path("helm").parent.iterdir()) == ["helm"]


def test_a_tool_reporting_another_version_is_refused(checkout: Checkout) -> None:
    checkout.serve("kind", _tool(checkout.state, "kind", "kind v0.32.0 go1.26.7 linux/amd64"))
    proc = checkout.run()
    assert proc.returncode == 1
    assert f"kind reports the version 'v0.32.0', not the pinned v{_version('kind')}" in proc.stderr


def test_a_tool_that_does_not_run_is_refused(checkout: Checkout) -> None:
    checkout.serve("kubectl", b"#!/bin/sh\necho 'cannot execute' >&2\nexit 126\n")
    proc = checkout.run()
    assert proc.returncode == 1
    assert "kubectl does not run: cannot execute" in proc.stderr


def test_a_failed_download_leaves_nothing_behind(checkout: Checkout) -> None:
    proc = checkout.run(mode="fetch-fails")
    assert proc.returncode == 1
    assert f"helm {_version('helm')} could not be downloaded" in proc.stderr
    assert checkout.under_tools() == []


def test_a_download_with_another_hash_is_not_used(checkout: Checkout) -> None:
    # Should the download ever keep another file, the script's own check
    # still refuses it.
    pinned = checkout.pins["PINNED_KUBECTL_SHA256"]
    checkout.serve("kubectl", b"#!/bin/sh\necho other\n")
    checkout.pins["PINNED_KUBECTL_SHA256"] = pinned
    proc = checkout.run(mode="fetch-ignores-hash")
    assert proc.returncode == 1
    assert f"the download of kubectl {_version('kubectl')} has the SHA-256" in proc.stderr
    assert not checkout.path("kubectl").exists()


@pytest.mark.parametrize("where", [".tools", "tool directory", "tool"])
def test_a_symbolic_link_in_the_tools_directory_is_not_followed(checkout: Checkout, where: str) -> None:
    elsewhere = checkout.tmp / "elsewhere"
    elsewhere.mkdir()
    if where == ".tools":
        (checkout.root / ".tools").symlink_to(elsewhere)
        named = ".tools"
    else:
        checkout.path("helm").parent.parent.mkdir()
        if where == "tool directory":
            checkout.path("helm").parent.symlink_to(elsewhere)
            named = f".tools/helm-{_version('helm')}"
        else:
            checkout.path("helm").parent.mkdir()
            checkout.path("helm").symlink_to(elsewhere / "helm")
            named = f".tools/helm-{_version('helm')}/helm"
    proc = checkout.run()
    assert proc.returncode == 1
    assert f"{named} exists and is not a" in proc.stderr and "a symbolic link is not followed" in proc.stderr
    assert list(elsewhere.iterdir()) == [] and checkout.fetches() == []


@pytest.mark.parametrize("args, status", [(["--help"], 0), (["-h"], 0), (["--force"], 2), (["--help", "x"], 2),
                                          (["install"], 2)])
def test_usage(checkout: Checkout, args: List[str], status: int) -> None:
    proc = checkout.run(*args)
    assert proc.returncode == status
    assert "usage: setup_cluster_tools.sh" in (proc.stdout if status == 0 else proc.stderr)
    assert checkout.fetches() == [] and not (checkout.root / ".tools").exists()


# ---------------------------------------------------------------------------
# The repository around it
# ---------------------------------------------------------------------------

def _scripts() -> List[Path]:
    """The repository's shell scripts and the Python programs beside them;
    the tests' own stubs are not among them."""
    found = [*ROOT.glob("*.sh"), *(ROOT / "DB").glob("*.sh"), *(ROOT / "DB" / "lib").glob("*.sh"),
             *(ROOT / "DB").glob("*.py")]
    return sorted(path for path in found if path.is_file())


def test_no_script_names_a_github_address() -> None:
    scripts = _scripts()
    assert SCRIPT in scripts and LIBRARY in scripts
    offending = [str(path.relative_to(ROOT)) for path in scripts if "github.com" in path.read_text(encoding="utf-8")]
    assert offending == []


def test_the_tools_directory_is_ignored_and_outside_the_build_context() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/.tools/" in ignored
    rules = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    # An allowlist: everything is excluded, and nothing lets .tools/ back in.
    assert rules[0] == "*" and all(rule.startswith("!") for rule in rules[1:])
    assert not [rule for rule in rules if ".tools" in rule or rule in ("!*", "!**", "!**/*")]


def test_the_setup_is_documented() -> None:
    for document in ("README.md", "DB/TESTING_STRATEGY.md"):
        assert "setup_cluster_tools.sh" in (ROOT / document).read_text(encoding="utf-8"), document
