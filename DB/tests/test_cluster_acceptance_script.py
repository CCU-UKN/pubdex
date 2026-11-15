"""run_cluster_acceptance.sh, stage 9 of run_ci_suite.sh, offline.

The real script runs from a scratch copy of the files it uses, with the real
create_secrets.sh, shell libraries and the chart's values.yaml and external
example. docker, uname, build_image.sh and the pinned helm, kubectl and kind
are stubs: one Python program behind a small wrapper each, which logs every
call with its arguments, environment, working directory and the values files
helm is given, and keeps an inventory of the containers, networks, volumes,
image tags and node images it was asked to create. The copy of
DB/lib/cluster_tools.sh pins the wrappers' SHA-256. The stub kind writes a
kubeconfig whose server a control file can point elsewhere; the stub docker
answers crictl in the node.

That proves, without Docker or a cluster: the usage errors; that every
refused prerequisite creates nothing; that the dry run of step 5 admits,
after revision 1's objects, those of the guide's two ingress examples and
of the evaluation values with the six scheduling keys, each file on its
own, without helm test's pod, and installs none of them; that revision 1
runs the chart's default images, which the node pulls by digest, and that
without access to their registry the run fails with a message that says so and installs
nothing; that given images which are the published ones are refused; that a kubeconfig naming another
server, or more than the run's context, stops the run before any kubectl or
helm call that could reach a cluster; that every kubectl and helm call names
the run's kubeconfig and context and receives nothing of a polluted caller's
environment, so no default kubeconfig is read; that the cleanup deletes the
cluster before the network and the image tags, removes what a floor cluster
of the run left, leaves a network named kind and other clusters alone, and
fails with the commands that find what it could not remove, the floor's
included; and that a signal during a long step ends the run with its status
after the cleanup. The successful path itself runs against real Docker and
kind, as stage 9 of run_ci_suite.sh.

The floor -- the second cluster, of the oldest supported Kubernetes
release -- is checked from the script's text: its step numbers and header
agree; it starts only after the first cluster and its network are deleted,
from the pinned floor node image; no published image is pulled or loaded
before its install, which uses the chart's default images; its operator
has the same values admitted as step 5 before the audit log is checked; no
option or environment variable skips a step; and the Role its operator holds grants
exactly the rights KUBERNETES.md lists under "Access rights", read from the
guide's words with a fixed meaning for each.

The end of external mode is checked from the script's text too: after the
password change, whose upgrade alone has the admin Secret, a routine upgrade
with the setup switched off, a rollback to revision 1 and the uninstall each
run without that Secret, and each shows from the API server's replayed watch
that it created nothing, while the same replay around the password change's
upgrade has to find the setup's three objects; the replay reports what was
created and fails, never passes, when the API server cannot replay.

The guide's short commands are checked from the script's text and by
running the Python the script holds: step 7 prints the notes again and
finds the port forward in them; step 9, after the rollback, reads
`helm get values --all` and accepts revision 1's values only; step 12
labels a fourth pod, which tries the read service until it connects, once
that pod has failed, and the pod's program ends on its first connection;
each ingestion job's log is followed to its end and compared with the log
read afterwards; and step 15 runs the export in the attached pod with the
roster import's options, accepting only the export's CSV with the
demonstration's publication on standard output.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from people_pubs.sync.export_publications import write_publications_csv

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "run_cluster_acceptance.sh"
PINS = ROOT / "DB" / "lib" / "cluster_tools.sh"
BASH = shutil.which("bash") or "/bin/bash"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
LIBRARIES = ("disposable_postgres.sh", "docker_versions.sh", "release_identity.sh", "compose_acceptance.sh")
# What steps 5 and 25 admit in a dry run besides revision 1, in this order.
ADMITTED_VALUES = ("charts/pubdex/examples/values-ingress-tls.yaml",
                   "charts/pubdex/examples/values-ingress-cert-manager.yaml", "charts/tests/values/evaluation.yaml")
SCHEDULING_KEYS = ("nodeSelector", "tolerations", "affinity", "podAnnotations", "podLabels", "priorityClassName")


def _pin(name: str) -> str:
    return re.search(rf"^readonly {name}=(\S+)$", PINS.read_text(encoding="utf-8"), re.M).group(1)


TOOLS = {tool: Path(".tools") / f"{tool}-{_pin(f'PINNED_{tool.upper()}_VERSION')}" / tool
         for tool in ("helm", "kubectl", "kind")}
HASH_PINS = {"helm": "PINNED_HELM_MEMBER_SHA256", "kubectl": "PINNED_KUBECTL_SHA256", "kind": "PINNED_KIND_SHA256"}


def _published() -> tuple:
    """The chart's default images, by registry, repository, tag and digest: revision 1's."""
    values = yaml.safe_load((ROOT / "charts" / "pubdex" / "values.yaml").read_text(encoding="utf-8"))
    return tuple(f'{image["registry"]}/{image["repository"]}:{image["tag"]}@{image["digest"]}'
                 for image in (values["image"], values["postgresImage"]))


def _repo_digest(reference: str) -> str:
    name, digest = reference.split("@")
    return name.rsplit(":", 1)[0] + "@" + digest

STUB = r'''
import json, os, re, sys, time, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTROL = HERE / "control"
STATE = HERE / "state.json"
tool, args = sys.argv[1], sys.argv[2:]
files = {value: Path(value).read_text(encoding="utf-8") for flag, value in zip(args, args[1:])
         if tool == "helm" and flag == "--values" and Path(value).is_file()}
with open(HERE / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": tool, "args": args, "env": dict(os.environ), "cwd": os.getcwd(), "files": files}) + "\n")


def control(name, default=""):
    path = CONTROL / name
    return path.read_text(encoding="utf-8").strip() if path.exists() else default


def load():
    return json.loads(STATE.read_text(encoding="utf-8"))


def save(state):
    STATE.write_text(json.dumps(state), encoding="utf-8")


def after(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


def labelled(objects, flag_value):
    key, _, value = flag_value[len("label="):].partition("=")
    return [name for name, obj in objects.items() if obj.get("labels", {}).get(key) == value]


def docker():
    state = load()
    first, second = (args + ["", ""])[:2]
    if first == "info":
        return 0
    if first == "exec":
        return crictl(state, args[2:])
    if first == "version":
        fmt = after("--format")
        print(control("engine", "29.9.3") if "Server.Version" in fmt else control("platform", "linux/amd64"))
        return 0
    if (first, second) == ("compose", "version"):
        print(control("compose", "5.7.0"))
        return 0
    if first == "ps":
        print("\n".join(labelled(state["containers"], after("--filter"))))
        return 0
    if first == "network":
        if second == "ls":
            print("\n".join(labelled(state["networks"], after("--filter"))))
        elif second == "inspect":
            name = args[2]
            if name == "bridge":
                print(control("mtu", ""))
                return 0
            return 0 if name in state["networks"] else 1
        elif second == "create":
            labels = dict(value.split("=", 1) for flag, value in zip(args, args[1:]) if flag == "--label")
            state["networks"][args[-1]] = {"labels": labels}
        elif second == "rm":
            if control("network-rm-fails"):
                return 1
            state["networks"].pop(args[2], None)
        save(state)
        return 0
    if first == "image" and second == "inspect":
        fmt, name = after("--format", ""), args[-1]
        image = state["images"].get(name)
        if image is None:
            return 1
        if ".Id" in fmt:
            print(image["id"])
        elif "RepoDigests" in fmt:
            print("\n".join(image.get("digests", [])))
        elif "Labels" in fmt:
            print(image["labels"].get(re.search(r'Labels "([^"]+)"', fmt).group(1), ""))
        return 0
    if first == "image" and second == "rm":
        state["images"].pop(args[2], None)
    elif first == "tag":
        if args[1] not in state["images"]:
            return 1
        state["images"][args[2]] = dict(state["images"][args[1]])
    elif first == "build":
        base = re.search(r"^FROM (\S+)", sys.stdin.read(), re.M).group(1)
        state["images"][after("--tag")] = {"id": "sha256:" + uuid.uuid4().hex, "labels": dict(state["images"][base]["labels"])}
    elif first == "inspect":
        fmt, container = after("--format"), state["containers"].get(args[-1])
        if container is None:
            return 1
        if "Labels" in fmt:
            print(container["labels"].get("io.x-k8s.kind.cluster", ""))
        elif "Networks" in fmt:
            print(container["network"] + " ")
        elif "Mounts" in fmt:
            print(" ".join(container["volumes"]) + " ")
        else:
            print("kindest/node:stub sha256:node")
        return 0
    elif first == "port":
        print(control("binding", "127.0.0.1:41234"))
        return 0
    elif first == "rm":
        for name in args[3:]:
            state["containers"].pop(name, None)
    elif first == "volume":
        if second == "inspect":
            return 0 if args[2] in state["volumes"] else 1
        state["volumes"].remove(args[2]) if args[2] in state["volumes"] else None
    elif first == "save":
        Path(after("--output")).write_text("archive", encoding="utf-8")
    save(state)
    return 0


def crictl(state, command):
    if command[:2] == ["crictl", "pull"]:
        if control("pull-fails"):
            print(f'E1110 rpc error: failed to resolve reference "{command[2]}": lookup ghcr.io: no such host',
                  file=sys.stderr)
            return 1
        state["pulled"].append(command[2])
        save(state)
        return 0
    if command[:2] == ["crictl", "inspecti"]:
        ref = command[-1]
        if ref.startswith("docker.io/library/"):
            image = state["images"].get(ref[len("docker.io/library/"):])
            if image is None:
                return 1
            print(json.dumps({"status": {"id": image["id"], "repoDigests": None}}))
            return 0
        if ref not in state["pulled"]:
            return 1
        name, digest = ref.split("@")
        image_id = "sha256:" + digest[len("sha256:"):][::-1]
        if control("same-id") and "postgres" not in name:
            image_id = next(image["id"] for tag, image in state["images"].items()
                            if tag.startswith("pubdex-cluster-acceptance:"))
        print(json.dumps({"status": {"id": image_id, "repoDigests": [name.rsplit(":", 1)[0] + "@" + digest]}}))
        return 0
    return 0


def build():
    if control("build-fails"):
        print("build failed")
        return 1
    state = load()
    version = (HERE / "checkout" / "VERSION").read_text(encoding="utf-8").split()[0]
    for tag in (after("--tag"), after("--postgres-tag")):
        state["images"][tag] = {"id": "sha256:" + uuid.uuid4().hex, "labels": {
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.revision": control("revision", "unknown")}}
    save(state)
    return 0


def kind():
    state = load()
    if args[:1] == ["version"]:
        print("kind v" + control("kind-version", "0.33.0") + " go1.25.0 linux/amd64")
        return 0
    name = after("--name")
    if args[:2] == ["create", "cluster"]:
        node, volume = name + "-control-plane", uuid.uuid4().hex
        state["containers"][node] = {"labels": {"io.x-k8s.kind.cluster": name},
                                     "network": os.environ.get("KIND_EXPERIMENTAL_DOCKER_NETWORK", "kind"),
                                     "volumes": [volume]}
        state["volumes"].append(volume)
        if control("floor-leftover"):
            # What a floor cluster of the same run would have left: its node and network.
            run = name.removeprefix("pubdex-cluster-")
            floor = "pubdex-floor-" + run
            state["containers"][floor + "-control-plane"] = {"labels": {"io.x-k8s.kind.cluster": floor},
                                                            "network": floor + "-net", "volumes": []}
            state["networks"][floor + "-net"] = {"labels": {"pubdex.cluster-acceptance.run": run}}
        save(state)
        context = "kind-" + name
        extra = control("extra-context")
        contexts = [context] + ([extra] if extra else [])
        # JSON is YAML too, and what kind writes reads the same.
        Path(after("--kubeconfig")).write_text(json.dumps({
            "apiVersion": "v1", "kind": "Config", "current-context": context,
            "clusters": [{"name": context, "cluster": {"server": control("server", "https://127.0.0.1:41234")}}],
            "contexts": [{"name": c, "context": {"cluster": context, "user": context}} for c in contexts],
            "users": [{"name": context, "user": {}}]}), encoding="utf-8")
        return 0
    if args[:2] == ["delete", "cluster"]:
        if not control("kind-delete-leaves"):
            for node in [n for n, c in state["containers"].items() if c["labels"].get("io.x-k8s.kind.cluster") == name]:
                for volume in state["containers"].pop(node)["volumes"]:
                    state["volumes"].remove(volume)
        save(state)
        return 0
    if args[:2] == ["load", "docker-image"]:
        return 1 if control("load-fails") else 0
    return 0


def kubectl():
    if args[:2] == ["version", "--client"]:
        print("Client Version: v" + control("kubectl-version", "@KUBECTL_VERSION@"))
    elif "version" in args:
        print(json.dumps({"serverVersion": {"gitVersion": "v1.37.0"}}))
    elif "--dry-run=server" in args:
        print("pod/stub")
    return 0


def helm():
    if args[:1] == ["version"]:
        print("v" + control("helm-version", "4.3.1"))
    elif "install" in args:
        mode = control("install")
        if mode == "slow":
            (HERE / "install-started").write_text("", encoding="utf-8")
            for _ in range(6000):
                time.sleep(0.1)
        if mode == "fail":
            print("Error: INSTALLATION FAILED: stub", file=sys.stderr)
            return 1
    return 0


def uname():
    print(control("os", "Linux") if args == ["-s"] else control("arch", "x86_64"))
    return 0


sys.exit({"docker": docker, "build_image": build, "kind": kind, "kubectl": kubectl, "helm": helm,
          "uname": uname}[tool]())
'''


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Fake:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "checkout"
        self.control = tmp_path / "control"
        self.control.mkdir()
        (tmp_path / "stub.py").write_text(STUB.replace("@KUBECTL_VERSION@", _pin("PINNED_KUBECTL_VERSION")),
                                          encoding="utf-8")
        (tmp_path / "state.json").write_text(json.dumps(
            {"containers": {}, "networks": {"kind": {"labels": {}}, "bridge": {"labels": {}}},
             "images": {}, "volumes": [], "pulled": []}), encoding="utf-8")
        self.script = self.root / SCRIPT.name
        self.script.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, self.script)
        shutil.copy2(ROOT / "create_secrets.sh", self.root / "create_secrets.sh")
        shutil.copy2(ROOT / "VERSION", self.root / "VERSION")
        (self.root / "charts" / "pubdex" / "examples").mkdir(parents=True)
        for name in ("values.yaml", "examples/values-external.yaml"):
            shutil.copy2(ROOT / "charts" / "pubdex" / name, self.root / "charts" / "pubdex" / name)
        for name in ADMITTED_VALUES:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / "DB" / "lib").mkdir(parents=True)
        for name in LIBRARIES:
            shutil.copy2(ROOT / "DB" / "lib" / name, self.root / "DB" / "lib" / name)
        self.bindir = tmp_path / "bin"
        pins = PINS.read_text(encoding="utf-8")
        for tool, relative in TOOLS.items():
            _executable(self.root / relative, self._wrapper(tool))
            digest = hashlib.sha256((self.root / relative).read_bytes()).hexdigest()
            pins = re.sub(rf"^readonly {HASH_PINS[tool]}=\S+$", f"readonly {HASH_PINS[tool]}={digest}", pins, flags=re.M)
        (self.root / "DB" / "lib" / "cluster_tools.sh").write_text(pins, encoding="utf-8")
        _executable(self.root / "build_image.sh", self._wrapper("build_image"))
        _executable(self.bindir / "docker", self._wrapper("docker"))
        _executable(self.bindir / "uname", self._wrapper("uname"))
        _executable(self.root / ".venv" / "bin" / "python", f'#!/bin/sh\nexec "{VENV_PYTHON}" "$@"\n')
        self.tmpdir = tmp_path / "tmpdir"
        self.tmpdir.mkdir()
        self.home = tmp_path / "home"
        (self.home / ".kube").mkdir(parents=True)
        (self.home / ".kube" / "config").write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
        self.env = {"PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.home),
                    "TMPDIR": str(self.tmpdir), "LANG": "C.UTF-8"}

    def _wrapper(self, tool: str) -> str:
        return f'#!/bin/sh\nexec "{sys.executable}" -I "{self.tmp / "stub.py"}" {tool} "$@"\n'

    def set(self, name: str, value: str = "1") -> None:
        (self.control / name).write_text(value, encoding="utf-8")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.script), *args], cwd=self.tmp, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=120)

    def calls(self, tool: str | None = None) -> list:
        path = self.tmp / "calls.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [call for call in map(json.loads, lines) if tool is None or call["tool"] == tool]

    def state(self) -> dict:
        return json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))

    def created_anything(self) -> bool:
        state = self.state()
        return bool(state["containers"] or state["images"] or state["volumes"]
                    or set(state["networks"]) != {"kind", "bridge"} or list(self.tmpdir.iterdir()))


def _cluster_calls(fake: Fake) -> list:
    """The kubectl and helm calls that could reach a cluster: all but the version reports."""
    return [call for call in fake.calls("kubectl") + fake.calls("helm") if call["args"][:1] != ["version"]]


def test_help_runs_nothing(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = fake.run("--help")
    assert proc.returncode == 0 and proc.stdout.startswith("usage: run_cluster_acceptance.sh")
    assert "CLUSTER_WAIT_SECONDS" in proc.stdout
    assert fake.calls() == []


@pytest.mark.parametrize(
    "args, env, message",
    [
        (["--keep"], {}, "unknown argument: --keep"),
        (["--image", "example.org/pubdex:1"], {}, "--image needs --postgres-image"),
        (["--postgres-image", "example.org/pg:1"], {}, "--postgres-image needs --image"),
        (["--expect-revision", "a" * 40], {}, "need --image"),
        (["--image", "a", "--postgres-image", "b", "--expect-version", "latest"], {}, "a release version"),
        (["--image", "a", "--postgres-image", "b", "--expect-revision", "main"], {}, "a full commit id"),
        (["--image"], {}, "--image needs a value"),
        ([], {"CLUSTER_WAIT_SECONDS": "0"}, "CLUSTER_WAIT_SECONDS must be a positive number"),
    ],
)
def test_usage_errors(tmp_path: Path, args: list, env: dict, message: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(*args, **env)
    assert proc.returncode == 2 and message in proc.stderr
    assert fake.calls() == []


def _unpin_digest(fake: Fake) -> None:
    values = fake.root / "charts" / "pubdex" / "values.yaml"
    values.write_text(re.sub(r'digest: "sha256:[0-9a-f]+"', 'digest: ""', values.read_text(encoding="utf-8"), count=1),
                      encoding="utf-8")


def _break_tool(fake: Fake, tool: str) -> None:
    (fake.root / TOOLS[tool]).write_text(fake._wrapper(tool) + "# changed\n", encoding="utf-8")


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda fake: fake.set("os", "Darwin"), "Linux on x86_64 only, not Darwin/x86_64"),
        (lambda fake: fake.set("arch", "aarch64"), "Linux on x86_64 only, not Linux/aarch64"),
        (lambda fake: fake.set("engine", "28.2.1"), "Docker Engine 28.0.1 or later, except 28.2.0 to 28.3.2"),
        (lambda fake: fake.set("platform", "linux/arm64"), "runs on 'linux/arm64', not linux/amd64"),
        (lambda fake: fake.set("compose", "2.23.0"), "the deployment needs 2.24.0 or later"),
        (lambda fake: (fake.root / TOOLS["kind"]).unlink(), "the pinned kind 0.33.0 is not installed"),
        (lambda fake: _break_tool(fake, "kubectl"), "not the pinned"),
        (lambda fake: (fake.root / ".venv" / "bin" / "python").unlink(), "./setup_venv.sh"),
        (lambda fake: fake.set("helm-version", "3.22.1"), "helm reports the version 'v3.22.1'"),
        (lambda fake: fake.set("kind-version", "0.31.0"), "not the pinned v0.33.0"),
        (lambda fake: _unpin_digest(fake), "image in values.yaml names no published image"),
    ],
)
def test_a_refused_prerequisite_creates_nothing(tmp_path: Path, setup, message: str) -> None:
    fake = Fake(tmp_path)
    setup(fake)
    proc = fake.run()
    assert proc.returncode == 1 and message in proc.stderr
    assert not fake.created_anything(), "no network, cluster, image tag or temporary directory"
    assert fake.calls("build_image") == [] and _cluster_calls(fake) == []
    assert [call["args"][:2] for call in fake.calls("kind")] in ([], [["version"]])


@pytest.mark.parametrize(
    "control, message",
    [
        ({"server": "https://203.0.113.10:6443"}, "is not this run's cluster at https://127.0.0.1:41234"),
        ({"server": "https://127.0.0.1:6443"}, "is not this run's cluster"),
        ({"extra-context": "shared-cluster"}, "holds more or other than the context"),
        ({"binding": "0.0.0.0:41234"}, "not on one loopback port"),
    ],
)
def test_a_kubeconfig_for_another_server_is_refused_before_any_cluster_call(
        tmp_path: Path, control: dict, message: str) -> None:
    fake = Fake(tmp_path)
    for name, value in control.items():
        fake.set(name, value)
    proc = fake.run()
    assert proc.returncode == 1 and message in proc.stderr
    assert _cluster_calls(fake) == [], "no kubectl or helm call reaches any cluster"
    assert [call["args"][:2] for call in fake.calls("kind")][-1] == ["delete", "cluster"]
    assert not fake.created_anything(), "the run's cluster, network and tags are gone all the same"


def _failed_install(fake: Fake, **env: str) -> subprocess.CompletedProcess:
    fake.set("install", "fail")
    proc = fake.run(**env)
    assert proc.returncode == 1 and "FAIL: helm install failed" in proc.stderr
    return proc


def test_every_cluster_call_uses_the_runs_kubeconfig_and_context_only(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    before = (fake.home / ".kube" / "config").stat()
    _failed_install(fake, KUBECONFIG=str(fake.home / ".kube" / "config"), HELM_KUBECONTEXT="shared",
                    HELM_DRIVER="sql", KUBECACHEDIR="/nonexistent", DOCKER_CONFIG=str(tmp_path / "docker"))
    (create,) = [call for call in fake.calls("kind") if call["args"][:2] == ["create", "cluster"]]
    name = create["args"][create["args"].index("--name") + 1]
    work = Path(create["args"][create["args"].index("--kubeconfig") + 1]).parent
    assert work.parent == fake.tmpdir and not work.exists(), "the run's directory, removed again"
    calls = _cluster_calls(fake)
    assert [call["args"][4:6] for call in calls if call["tool"] == "helm"] == \
        [["template", "pubdex"]] * (1 + len(ADMITTED_VALUES)) + [["install", "pubdex"]]
    for call in calls:
        context = "--context" if call["tool"] == "kubectl" else "--kube-context"
        assert call["args"][:4] == ["--kubeconfig", str(work / "kubeconfig"), context, f"kind-{name}"], call["args"]
    for call in fake.calls("kubectl") + fake.calls("helm"):
        env = call["env"]
        assert env["KUBECONFIG"] == str(work / "kubeconfig") and env["HOME"] == str(work / "home")
        assert env["KUBECACHEDIR"] == str(work / "kube-cache") and env["DOCKER_CONFIG"] == str(work / "docker")
        assert all(env[f"HELM_{part}_HOME"].startswith(str(work)) for part in ("CONFIG", "CACHE", "DATA"))
        assert "HELM_KUBECONTEXT" not in env and "HELM_DRIVER" not in env
        assert not any("/.kube" in value for value in list(env.values()) + call["args"])
    for call in fake.calls("kind"):
        assert "KUBECONFIG" not in call["env"] and call["env"]["HOME"] == str(work / "home")
        assert call["env"]["DOCKER_CONFIG"] == str(tmp_path / "docker"), "the caller's Docker client configuration"
        assert call["env"]["KIND_EXPERIMENTAL_DOCKER_NETWORK"] == f"{name}-net"
    network = [call for call in fake.calls("docker") if call["args"][:2] == ["network", "create"]]
    assert len(network) == 1 and network[0]["args"][-1] == f"{name}-net"
    assert f"pubdex.cluster-acceptance.run={name.removeprefix('pubdex-cluster-')}" in network[0]["args"]
    assert (fake.home / ".kube" / "config").stat() == before


def test_the_cleanup_deletes_the_cluster_then_the_network_then_the_tags(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    state = fake.state()
    state["containers"]["other-control-plane"] = {"labels": {"io.x-k8s.kind.cluster": "other"},
                                                  "network": "kind", "volumes": []}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    proc = _failed_install(fake)
    calls = fake.calls()
    install = max(index for index, call in enumerate(calls) if call["tool"] == "helm" and "install" in call["args"])
    cleanup = [(call["tool"], call["args"][:2]) for call in calls[install + 1:]]
    delete = cleanup.index(("kind", ["delete", "cluster"]))
    network = cleanup.index(("docker", ["network", "rm"]))
    tags = [index for index, call in enumerate(cleanup) if call == ("docker", ["image", "rm"])]
    assert delete < network < tags[0] and len(tags) == 2, cleanup
    removals = {("docker", ("rm",)), ("docker", ("network", "rm")), ("docker", ("image", "rm")),
                ("docker", ("volume", "rm")), ("kind", ("delete", "cluster"))}
    assert not [call for call in cleanup[tags[-1] + 1:]
                if (call[0], tuple(call[1][:1])) in removals or (call[0], tuple(call[1])) in removals], \
        "the confirmation only looks"
    state = fake.state()
    assert set(state["networks"]) == {"kind", "bridge"}, "a network named kind stays"
    assert set(state["containers"]) == {"other-control-plane"}, "another cluster stays"
    assert state["images"] == {} and state["volumes"] == [] and list(fake.tmpdir.iterdir()) == []
    assert "      cleanup: the clusters, their networks and volumes" in proc.stdout, "the confirmation, after a failure too"
    assert "PASS" not in proc.stdout and "the run failed with status 1" in proc.stderr


def test_a_cleanup_that_cannot_be_confirmed_fails_and_names_the_leftovers(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("network-rm-fails")
    fake.set("kind-delete-leaves")
    proc = fake.run()
    assert proc.returncode == 1, "a passing run whose cleanup is not confirmed fails"
    (create,) = [call for call in fake.calls("kind") if call["args"][:2] == ["create", "cluster"]]
    name = create["args"][create["args"].index("--name") + 1]
    floor = name.replace("pubdex-cluster-", "pubdex-floor-")
    assert "WARNING: could not confirm that run" in proc.stderr
    for found in (f"docker ps -a --filter label=io.x-k8s.kind.cluster={name}", f"docker network inspect {name}-net",
                  f"docker ps -a --filter label=io.x-k8s.kind.cluster={floor}", f"docker network inspect {floor}-net"):
        assert found in proc.stderr, found
    assert "PASS" not in proc.stdout and "cleanup:" not in proc.stdout


@pytest.mark.parametrize("sig, status", [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)])
def test_a_signal_during_the_install_cleans_up_with_its_status(tmp_path: Path, sig, status: int) -> None:
    fake = Fake(tmp_path)
    fake.set("install", "slow")
    proc = subprocess.Popen([BASH, str(fake.script)], cwd=tmp_path, env=fake.env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not (tmp_path / "install-started").exists():
        assert time.monotonic() < deadline and proc.poll() is None, "the install never started"
        time.sleep(0.05)
    proc.send_signal(sig)
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == status, err
    assert "cleanup: the clusters, their networks and volumes" in _out
    assert [call["args"][:2] for call in fake.calls("kind")][-1] == ["delete", "cluster"]
    assert not fake.created_anything()


def test_the_images_load_from_an_archive_when_kind_cannot_read_the_store(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("load-fails")
    _failed_install(fake)
    loads = [call["args"][:2] for call in fake.calls("kind") if call["args"][:1] == ["load"]]
    assert loads == [["load", "docker-image"], ["load", "image-archive"]]
    assert [call for call in fake.calls("docker") if call["args"][:1] == ["save"]]


def test_given_images_are_tagged_not_built_and_their_labels_checked(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    state = fake.state()
    for name in ("example.org/pubdex:1", "example.org/pubdex-postgres:1"):
        state["images"][name] = {"id": "sha256:" + name[-1] * 64, "labels": {
            "org.opencontainers.image.version": "0.1.0", "org.opencontainers.image.revision": "a" * 40}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    proc = fake.run("--image", "example.org/pubdex:1", "--postgres-image", "example.org/pubdex-postgres:1",
                    "--expect-revision", "b" * 40)
    assert proc.returncode == 1 and f"expected '{'b' * 40}'" in proc.stderr
    assert fake.calls("build_image") == []
    assert set(fake.state()["images"]) == {"example.org/pubdex:1", "example.org/pubdex-postgres:1"}, \
        "the given images stay, the run's tags go"


def _path_without(tmp_path: Path, program: str) -> str:
    """A directory with every program of this PATH but <program>, as symbolic links."""
    directory = tmp_path / "system-bin"
    directory.mkdir()
    for entry in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(entry):
            continue
        for item in os.scandir(entry):
            target = directory / item.name
            if item.name != program and not os.path.lexists(target) and item.is_file() and os.access(item.path, os.X_OK):
                target.symlink_to(item.path)
    return str(directory)


def test_without_openssl_nothing_is_created(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.env["PATH"] = f"{fake.bindir}{os.pathsep}{_path_without(tmp_path, 'openssl')}"
    proc = fake.run()
    assert proc.returncode == 1 and "openssl was not found on PATH" in proc.stderr
    assert not fake.created_anything() and fake.calls("kind") == []


def test_revision_1_runs_the_published_images_which_the_node_pulls_by_digest(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = _failed_install(fake)
    app, postgres = _published()
    pulls = [call["args"][2:] for call in fake.calls("docker") if call["args"][:1] == ["exec"]
             and call["args"][2:4] == ["crictl", "pull"]]
    assert pulls == [["crictl", "pull", app], ["crictl", "pull", postgres]], "anonymously: no credential option"
    (load,) = [call["args"] for call in fake.calls("kind") if call["args"][:2] == ["load", "docker-image"]]
    assert [tag.split(":")[0] for tag in load[4:]] == ["pubdex-cluster-acceptance", "pubdex-cluster-acceptance-postgres"], \
        "only the images under test are loaded, never a published image under another name"
    (install,) = [call for call in fake.calls("helm") if call["args"][4:5] == ["install"]]
    (values,) = install["files"].values()
    assert yaml.safe_load(values) == {"secrets": {"name": "pubdex-credentials"}, "demo": {"enabled": True}}, \
        "no image value: the chart's defaults, the published images"
    assert f"revision 1 (published): {app}" in proc.stdout and postgres in proc.stdout
    assert "the node pulled the published images by digest, anonymously" in proc.stdout


def test_step_5_admits_the_ingress_examples_and_the_scheduling_values_and_installs_none(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    proc = _failed_install(fake)
    helm = fake.calls("helm")
    install = next(index for index, call in enumerate(helm) if call["args"][4:5] == ["install"])
    templates = [call for call in helm[:install] if call["args"][4:5] == ["template"]]
    assert [list(call["files"]) for call in templates[1:]] == [[str(fake.root / name)] for name in ADMITTED_VALUES], \
        "after revision 1, each file on its own, the chart's defaults under it"
    assert all(call["args"][5:8] == ["pubdex", str(fake.root / "charts" / "pubdex"), "--namespace"]
               for call in templates), "the release's name and namespace, as for revision 1"
    dry_runs = [call for call in fake.calls("kubectl") if "--dry-run=server" in call["args"]]
    assert len(dry_runs) == 1 + len(ADMITTED_VALUES), "revision 1's objects and each file's, before the install"
    assert all(call["args"][call["args"].index("--filename") + 1].endswith("/rendered-objects.yaml")
               for call in dry_runs), "objects only: helm test's pod would be refused before the install"
    step = proc.stdout[proc.stdout.index("[5/29]"):proc.stdout.index("[6/29]")]
    for name in ADMITTED_VALUES:
        assert f"the API server admitted the objects of {name} without a warning" in step
    assert "none of the 3 is installed; helm test's pod of each is left out" in step
    assert "no step creates the PriorityClasses the scheduling values name" in step


def test_the_values_admitted_but_not_installed_are_what_the_guide_says() -> None:
    examples = [yaml.safe_load((ROOT / name).read_text(encoding="utf-8")) for name in ADMITTED_VALUES[:2]]
    assert all(values["ingress"]["enabled"] and values["ingress"]["tls"] and values["networkPolicy"]["enabled"]
               and values["networkPolicy"]["api"]["allowFrom"] for values in examples), \
        "both examples: an Ingress with TLS, and a NetworkPolicy that admits the ingress controller"
    scheduling = yaml.safe_load((ROOT / ADMITTED_VALUES[2]).read_text(encoding="utf-8"))
    assert all(scheduling.get(key) for key in SCHEDULING_KEYS), "all six keys at the top level, none empty"
    assert any(key in scheduling[component] for component in ("api", "postgresql") for key in SCHEDULING_KEYS), \
        "and replaced for a component"
    assert re.search(r"^ADMITTED_VALUES=\((.*?)\)$", _script(), re.M | re.S).group(1).split() == list(ADMITTED_VALUES)
    assert not re.search(r"\bpriorityclass|scheduling\.k8s\.io|kind: *PriorityClass", _script()), \
        "no step creates or reads a PriorityClass"


def test_the_floor_admits_the_same_values_as_the_operator_before_the_install() -> None:
    step = _step(25)
    order = [_at(step, 'dry_run "$WORK/rendered-objects.yaml" "the objects of revision 1"'),
             _at(step, "\nadmit_values\n"), _at(step, '\ncheck_audit "the Secret and the dry runs"')]
    assert order == sorted(order), "revision 1, the three files, then no refused request of the operator"
    assert step.count("\nadmit_values\n") == 1 and _step(5).count("\nadmit_values\n") == 1
    assert "use_kubeconfig" not in step, "still as the operator of step 24"
    assert "helm_run" not in _step(5) + step, "nothing installed in either dry run's step"


def test_without_access_to_the_registry_the_run_fails_and_says_so(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("pull-fails")
    proc = fake.run()
    assert proc.returncode == 1
    assert f"could not pull the published image {_published()[0]} without credentials" in proc.stderr
    assert "needs access to ghcr.io from the kind node; nothing was skipped" in proc.stderr
    assert "no such host" in proc.stderr, "the runtime's own error"
    assert not [call for call in fake.calls("helm") if call["args"][4:5] in (["template"], ["install"])]
    assert "PASS" not in proc.stdout and "cleanup: the clusters" in proc.stdout
    assert not fake.created_anything()


def test_given_images_that_are_the_published_ones_are_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    state = fake.state()
    for name, published in zip(("example.org/pubdex:1", "example.org/pubdex-postgres:1"), _published()):
        state["images"][name] = {"id": "sha256:" + "c" * 64, "digests": [_repo_digest(published)], "labels": {
            "org.opencontainers.image.version": "0.1.0", "org.opencontainers.image.revision": "a" * 40}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    proc = fake.run("--image", "example.org/pubdex:1", "--postgres-image", "example.org/pubdex-postgres:1")
    assert proc.returncode == 1
    assert f"example.org/pubdex:1 is the published image {_published()[0]}, which revision 1 runs" in proc.stderr
    assert fake.calls("kind") == [call for call in fake.calls("kind") if call["args"][:1] == ["version"]]
    assert set(fake.state()["images"]) == {"example.org/pubdex:1", "example.org/pubdex-postgres:1"}
    assert not list(fake.tmpdir.iterdir())


def test_images_under_test_with_the_published_id_are_refused(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("same-id")
    proc = fake.run()
    assert proc.returncode == 1 and "has the id of the published image it should replace" in proc.stderr
    assert not [call for call in fake.calls("helm") if call["args"][4:5] in (["template"], ["install"])]
    assert "cleanup: the clusters" in proc.stdout and not fake.created_anything()


def test_the_cleanup_also_removes_what_a_floor_cluster_left(tmp_path: Path) -> None:
    fake = Fake(tmp_path)
    fake.set("floor-leftover")
    proc = _failed_install(fake)
    state = fake.state()
    assert not [name for name in state["containers"] if name.startswith("pubdex-floor-")], state["containers"]
    assert set(state["networks"]) == {"kind", "bridge"}, "the floor's network goes, by its label and its name"
    assert not fake.created_anything() and "cleanup: the clusters, their networks and volumes" in proc.stdout
    removed = [call["args"][-1] for call in fake.calls("docker") if call["args"][:1] == ["rm"]]
    assert any(name.startswith("pubdex-floor-") for name in removed), removed
    assert not [call for call in fake.calls("kind") if call["args"][:2] == ["delete", "cluster"]
                and call["args"][3].startswith("pubdex-floor-")], "kind deletes only a cluster the run started"


# ---------------------------------------------------------------------------
# The floor: what the script holds, read from its text
# ---------------------------------------------------------------------------

GUIDE = ROOT / "KUBERNETES.md"

# The guide's words for the rights it lists, and the RBAC verbs each means.
RIGHTS = {
    "create, change and delete": {"create", "update", "patch", "delete", "get", "list", "watch"},
    "create and get": {"create", "get"},
    "change": {"update", "patch"},
    "read": {"get", "list", "watch"},
}
# The guide's names for what it grants rights over: (API group, resource).
RESOURCES = {
    "ConfigMaps": ("", "configmaps"), "Secrets": ("", "secrets"), "Services": ("", "services"),
    "ServiceAccounts": ("", "serviceaccounts"), "Pods": ("", "pods"), "pods": ("", "pods"),
    "PersistentVolumeClaims": ("", "persistentvolumeclaims"), "their logs": ("", "pods/log"),
    "events": ("", "events"), "`pods/exec`": ("", "pods/exec"), "`pods/portforward`": ("", "pods/portforward"),
    "`pods/attach`": ("", "pods/attach"), "Deployments": ("apps", "deployments"),
    "StatefulSets": ("apps", "statefulsets"), "ReplicaSets": ("apps", "replicasets"),
    "`deployments/scale`": ("apps", "deployments/scale"), "Jobs": ("batch", "jobs"), "CronJobs": ("batch", "cronjobs"),
    "NetworkPolicies": ("networking.k8s.io", "networkpolicies"),
    "Ingresses if you enable one": ("networking.k8s.io", "ingresses"),
}


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _floor_phase() -> str:
    """The script from step 21 on: the floor's cluster and its lifecycle."""
    text = _script()
    return text[text.index('\nstep 21 "'):]


def _guide_rights(text: str) -> dict:
    """The access rights of KUBERNETES.md, What you need: {(group, resource): verbs}.
    The list is one sentence of clauses separated by semicolons, each "to
    <right> <things>"; parenthetical remarks are not part of it."""
    bullet = text[text.index("**Access rights** in the release's namespace:"):].split("\n- ", 1)[0]
    sentence = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", bullet.split(":", 1)[1]))
    sentence = sentence[:sentence.index(". ")]
    rights: dict = {}
    for clause in sentence.split(";"):
        clause = clause.strip().removeprefix("and ").strip()
        phrase = next((right for right in sorted(RIGHTS, key=len, reverse=True) if clause.startswith(f"to {right} ")),
                      None)
        assert phrase, f"the guide's access rights name no known right: {clause!r}"
        for thing in re.split(r",\s*(?:and\s+)?|\s+and\s+", clause[len(f"to {phrase} "):]):
            assert thing.strip() in RESOURCES, f"the guide grants a right over {thing.strip()!r}, unknown to this test"
            rights.setdefault(RESOURCES[thing.strip()], set()).update(RIGHTS[phrase])
    return rights


def _script_role() -> dict:
    """The Role stage 9 gives the floor's operator: {(group, resource): verbs}."""
    role = yaml.safe_load(re.search(r"<<'ROLE'\n(.*?)^ROLE$", _script(), re.S | re.M).group(1))
    assert role["kind"] == "Role" and role["metadata"] == {"name": "pubdex-operator"}
    rights: dict = {}
    for rule in role["rules"]:
        assert set(rule) == {"apiGroups", "resources", "verbs"}, "no resourceNames and no nonResourceURLs"
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                rights.setdefault((group, resource), set()).update(rule["verbs"])
    return rights


def test_the_operators_role_grants_exactly_the_rights_the_guide_lists() -> None:
    guide, role = _guide_rights(GUIDE.read_text(encoding="utf-8")), _script_role()
    assert len(guide) >= 18
    only_role = {key: sorted(role[key] - guide.get(key, set())) for key in role if role[key] - guide.get(key, set())}
    only_guide = {key: sorted(guide[key] - role.get(key, set())) for key in guide if guide[key] - role.get(key, set())}
    assert not only_role and not only_guide, f"only in the Role: {only_role}; only in KUBERNETES.md: {only_guide}"
    assert not [key for key, verbs in role.items() if "*" in verbs or "*" in key]
    assert re.search(r'^OPERATOR="pubdex-operator"', _script(), re.M), "the Role, its binding and the script agree"


def test_a_change_to_either_list_would_be_found() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="DaemonSets"):
        _guide_rights(text.replace("events and ReplicaSets", "events and DaemonSets", 1))
    narrowed = _guide_rights(text.replace("`pods/portforward` and `pods/attach`", "and `pods/portforward`", 1))
    assert narrowed != _script_role() and ("", "pods/attach") not in narrowed


def test_the_steps_and_the_header_agree() -> None:
    text = _script()
    count = int(re.search(r"^STEPS=([0-9]+)$", text, re.M).group(1))
    assert count == 29
    assert [int(number) for number in re.findall(r'^step ([0-9]+) "', text, re.M)] == list(range(1, count + 1))
    header = text[:text.index("\nset -euo pipefail\n")]
    assert [int(number) for number in re.findall(r"^# +([0-9]+)\. ", header, re.M)] == list(range(1, count + 1))
    assert "On the second, of the oldest" in header and "rights KUBERNETES.md lists" in header


def test_the_floor_replaces_the_first_cluster_with_the_floor_node_image() -> None:
    floor = _floor_phase()
    order = [floor.index('kind_ delete cluster --name "$CLUSTER"'), floor.index('docker network rm "$NETWORK"'),
             floor.index('create_cluster "$FLOOR_CLUSTER" "$FLOOR_NETWORK" "$PINNED_KIND_FLOOR_NODE_IMAGE"')]
    assert order == sorted(order), "the first cluster and its network go before the floor's cluster starts"
    assert "$PINNED_KIND_NODE_IMAGE" not in floor
    assert 'create_cluster "$CLUSTER" "$NETWORK" "$PINNED_KIND_NODE_IMAGE"' in _script()
    assert re.search(r"^readonly PINNED_KIND_FLOOR_NODE_IMAGE=kindest/node:v[0-9.]+@sha256:[0-9a-f]{64}$",
                     PINS.read_text(encoding="utf-8"), re.M)


def test_the_floor_pulls_no_published_image_before_the_install() -> None:
    text, floor = _script(), _floor_phase()
    assert "crictl pull" not in floor and "docker pull" not in floor
    loader = text[text.index("load_images_under_test() {"):]
    loader = loader[:loader.index("\n}\n")]
    assert set(re.findall(r'"\$([A-Z_]+)"', loader)) == {"ACTIVE_CLUSTER", "APP_TAG", "PG_TAG"}, \
        "kind loads only the images under test, never a published image"
    before = floor[:floor.index('\nstep 26 "')]
    assert "the kubelet has to pull it" in before and "helm_run install" not in before
    (install,) = re.findall(r'^helm_run install "\$RELEASE" "\$CHART" (.*)$', floor, re.M)
    assert install.startswith('--values "$WORK/values-1.yaml" --wait'), "revision 1: the chart's default images"
    assert "IfNotPresent" in floor and "Successfully pulled image" in floor


@pytest.mark.parametrize("option", ["--skip-floor", "--no-floor", "--floor=no", "--quick"])
def test_no_option_skips_the_floor(tmp_path: Path, option: str) -> None:
    fake = Fake(tmp_path)
    proc = fake.run(option)
    assert proc.returncode == 2 and f"unknown argument: {option}" in proc.stderr
    assert fake.calls() == []


def test_no_environment_variable_skips_a_step() -> None:
    # Defaults of upper-case names: what the script reads from the environment.
    read = set(re.findall(r"\$\{([A-Z_]+):?[-=+?]", _script()))
    assert read == {"CLUSTER_WAIT_SECONDS", "TMPDIR", "ISOLATED_IN_PLACE"}, read
    assert "Every step runs on every run." in _script()


# ---------------------------------------------------------------------------
# External mode's end: a routine upgrade, a rollback and the uninstall
# ---------------------------------------------------------------------------

def _step(number: int) -> str:
    """Step <number> of the script, from its step line to the next one's,
    with continuation lines joined."""
    text = _script()
    start = text.index(f'\nstep {number} "')
    return re.sub(r"\\\n\s*", " ", text[start:text.index(f'\nstep {number + 1} "', start)])


def _at(text: str, needle: str) -> int:
    assert needle in text, needle
    return text.index(needle)


def _helm(step: str, command: str) -> list:
    """The helm_run <command> lines of a step, continuation lines joined."""
    return re.findall(rf'^helm_run {command} "\$RELEASE" (.*)$', step, re.M)


def test_external_mode_ends_with_a_routine_upgrade_a_rollback_and_the_uninstall() -> None:
    text = _script()
    text = text[_at(text, '\nstep 15 "'):]
    assert re.findall(r'^NAMESPACE="\$([A-Z_]+)"', text, re.M) == ["EXTERNAL_NAMESPACE", "EVAL_NAMESPACE"]
    assert _at(text, 'NAMESPACE="$EXTERNAL_NAMESPACE"') < _at(text, '\nstep 16 "') \
        and _at(text, '\nstep 21 "') < _at(text, 'NAMESPACE="$EVAL_NAMESPACE"'), "steps 16 to 20 act in external mode"
    assert _step(18).startswith('\nstep 18 "External mode: a routine upgrade') and len(_helm(_step(18), "upgrade")) == 1
    assert _step(19).startswith('\nstep 19 "External mode: rolling back to revision 1') \
        and len(_helm(_step(19), "rollback")) == 1
    assert _step(20).startswith('\nstep 20 "External mode: uninstalling') and len(_helm(_step(20), "uninstall")) == 1


def test_the_admin_secret_is_deleted_before_the_routine_upgrade_and_never_created_again() -> None:
    password_change = _step(17)
    calls = re.findall(r"^admin_secret (create|delete)$", password_change, re.M)
    assert calls == ["create", "delete"], "step 17 creates the admin Secret before its upgrade and deletes it after"
    assert _at(password_change, "admin_secret create") < _at(password_change, "helm_run upgrade") \
        < _at(password_change, "admin_secret delete")
    for number, operation in ((18, "helm_run upgrade"), (19, "helm_run rollback"), (20, "helm_run uninstall")):
        step = _step(number)
        assert not re.search(r"^admin_secret (create|delete)$", step, re.M), f"step {number} never touches it"
        assert _at(step, "admin_secret_absent ") < _at(step, operation), \
            f"step {number} first asks the API server whether the admin Secret exists"
    rollback = _step(19)
    assert _at(rollback, "helm_run rollback") < _at(rollback, 'admin_secret_absent "after the rollback"')
    absent = _script()[_at(_script(), "admin_secret_absent() {"):]
    assert "(NotFound)" in absent[:absent.index("\n}\n")], "only the API server's NotFound counts as absent"


def test_only_the_routine_upgrade_switches_the_setup_off() -> None:
    switched = "--set externalDatabase.setup.enabled=false"
    (routine,) = _helm(_step(18), "upgrade")
    assert switched in routine and routine.endswith('--wait --timeout "${WAIT_SECONDS}s"')
    assert '--values "$WORK/values-routine.yaml"' in routine and "--reuse-values" not in routine
    assert re.search(r'^api: \{logLevel: WARNING\}$', _step(18), re.M), "one changed setting"
    assert 'check_revision "routine upgrade" "$APP_TAG" "" 2 WARNING same-image' in _step(18)
    commands = re.findall(r"^helm_run (?:install|upgrade) (.*)$", re.sub(r"\\\n\s*", " ", _script()), re.M)
    assert len(commands) >= 8 and [command for command in commands if "setup.enabled" in command] == [
        f'"$RELEASE" {routine}'], "every other install and upgrade runs the setup, as external mode does by default"


def test_without_the_admin_secret_the_upgrade_with_the_setup_fails_then_the_routine_upgrade_recovers() -> None:
    routine_upgrade = _step(18)
    (failing,) = re.findall(r'^if job "\$\(\(WAIT_SECONDS \+ 60\)\)" helm_ upgrade "\$RELEASE" (.*) '
                            r'> "\$WORK/helm-upgrade-failed\.log" 2>&1; then$', routine_upgrade, re.M)
    (routine,) = _helm(routine_upgrade, "upgrade")
    words = failing.split()
    assert words[1:3] == ["--namespace", '"$NAMESPACE"'] and "--reuse-values" not in words
    assert words[:1] + words[3:] == [word for word in routine.split()
                                     if word not in ("--set", "externalDatabase.setup.enabled=false")], \
        "the same upgrade, without the switch: the setup runs"
    assert re.search(r'^\[ "\$failed_after" -lt \$\(\(WAIT_SECONDS / 2\)\) \]', routine_upgrade, re.M), \
        "it fails well inside the timeout"
    order = [
        'admin_secret_absent "before the routine upgrade"',
        "helm_ upgrade",
        "'pre-upgrade hooks failed'",
        'k logs "job/$RELEASE-setup"',
        "PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE names a file that does not exist",
        '[ "$leftovers" = "job.batch/$RELEASE-setup" ]',
        'helm_ status "$RELEASE"',
        "grep -qx 'STATUS: failed'",
        "'^4[[:space:]].*failed'",
        '[ "$(pods_of api | cut -d\' \' -f1 | sort)" = "$(sort <<<"$API_UIDS")" ]',
        'read_back "after the failed upgrade"',
        "\nmark_hook_kinds\n",
        "helm_run upgrade",
        'nothing_created "the routine upgrade"',
        'check_revision "routine upgrade"',
        'k delete job,configmap,serviceaccount -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=setup"',
        '[ -z "$(setup_hooks_left)" ]',
        "wait_until 120 no_setup_pod",
        "\nno_admin_credential_in_use\n",
        'read_back "routine upgrade"',
        "'^5[[:space:]].*deployed'",
    ]
    positions = [_at(routine_upgrade, needle) for needle in order]
    assert positions == sorted(positions), "Secret absent, failing upgrade, routine upgrade, leftovers deleted"
    # Helm keeps the failed Job and deletes the hooks that succeeded before
    # it; the routine upgrade creates no hook, so the Job is still there.
    job_left = '[ "$leftovers" = "job.batch/$RELEASE-setup" ]'
    second = routine_upgrade.index(job_left, _at(routine_upgrade, job_left) + 1)
    assert _at(routine_upgrade, 'check_revision "routine upgrade"') < second \
        < _at(routine_upgrade, "k delete job,configmap,serviceaccount")
    guide = (ROOT / "KUBERNETES.md").read_text(encoding="utf-8")
    assert ("kubectl --namespace pubdex delete job,configmap,serviceaccount -l "
            "app.kubernetes.io/instance=pubdex,app.kubernetes.io/component=setup") in guide, "the guide's block"


def test_the_password_change_gives_the_values_files_again() -> None:
    # --reuse-values would carry over externalDatabase.setup.enabled=false from
    # a routine upgrade, and no setup would set the new passwords.
    (upgrade,) = _helm(_step(16), "upgrade")
    assert _helm(_step(17), "upgrade") == [upgrade] and "setup.enabled" not in upgrade
    assert "--reuse-values" not in re.sub(r"(?m)^\s*#.*$", "", _script())


def test_the_rollback_returns_to_revision_1_and_its_published_images() -> None:
    rollback = _step(19)
    (command,) = _helm(rollback, "rollback")
    assert command == '1 --wait --timeout "${WAIT_SECONDS}s"'
    assert _at(rollback, "helm_run rollback") < _at(rollback, 'check_revision "external rollback" "$BASE_APP_REF" "" 1 INFO')
    assert "'^6[[:space:]].*deployed.*Rollback to 1'" in rollback, "after revision 4, failed, and 5, the routine upgrade"
    assert 'grep -qx "Job/$RELEASE-setup pre-install,pre-upgrade" "$WORK/hooks-6.txt"' in rollback, \
        "the rollback's revision carries revision 1's setup hooks, which did not run"
    assert re.search(r'^check_revision "external revision 1" "\$BASE_APP_REF" "" 1 INFO$', _step(15), re.M)


def test_each_of_the_three_shows_that_it_created_nothing() -> None:
    for number, operation in ((18, "helm_run upgrade"), (19, "helm_run rollback"), (20, "helm_run uninstall")):
        step = _step(number)
        assert _at(step, "\nmark_hook_kinds\n") < _at(step, operation) < _at(step, "\nnothing_created "), number
    # The same replay around the password change's upgrade, which runs the
    # setup, has to find exactly the setup's three objects: the check sees
    # hooks that Helm deleted again.
    password_change = _step(17)
    assert _at(password_change, "\nmark_hook_kinds\n") < _at(password_change, "helm_run upgrade") \
        < _at(password_change, 'setup_objects="$(created_since)"') < _at(password_change, "admin_secret delete")
    expected = re.search(r'^expected_setup="\$\(printf .*\)"$', password_change, re.M).group(0)
    assert all(f'"{kind}/$RELEASE-setup"' in expected for kind in ("Job", "ConfigMap", "ServiceAccount"))
    assert '[ "$(sort <<<"$setup_objects")" = "$expected_setup" ]' in password_change
    paths = re.search(r"^HOOK_KIND_PATHS=\((.*)\)$", _script(), re.M).group(1).split()
    watched = {path.rsplit("/", 1)[1] for path in paths}
    templates = ROOT / "charts" / "pubdex" / "templates"
    kinds = {kind.lower() + "s" for name in ("external-setup-job.yaml", "external-setup-configmap.yaml")
             for kind in re.findall(r"^kind: (\w+)$", (templates / name).read_text(encoding="utf-8"), re.M)}
    assert watched == kinds == {"jobs", "configmaps", "serviceaccounts"}, "the kinds of the setup's hooks"


def _replay_parser() -> str:
    text = _script()
    function = text[_at(text, "created_since() {"):]
    function = function[:function.index("\n}\n")]
    return re.search(r"<<'PY'\n(.*?)^PY$", function, re.S | re.M).group(1)


@pytest.mark.parametrize(
    ("events", "status", "output"),
    [
        ([], 0, ""),
        ([{"type": "ADDED", "object": {"kind": "Job", "metadata": {"name": "pubdex-setup"}}},
          {"type": "MODIFIED", "object": {"kind": "Job", "metadata": {"name": "pubdex-setup"}}},
          {"type": "DELETED", "object": {"kind": "Job", "metadata": {"name": "pubdex-setup"}}},
          {"type": "ADDED", "object": {"kind": "ConfigMap", "metadata": {"name": "pubdex-setup"}}}],
         0, "Job/pubdex-setup\nConfigMap/pubdex-setup\n"),
        ([{"type": "MODIFIED", "object": {"kind": "ConfigMap", "metadata": {"name": "pubdex-config"}}}], 0, ""),
        ([{"type": "ERROR", "object": {"kind": "Status", "code": 410, "message": "too old resource version: 5 (90)"}}],
         1, "the API server cannot replay the watch: too old resource version: 5 (90)\n"),
    ],
)
def test_the_replayed_watch_names_what_was_created_and_never_passes_an_error(
        tmp_path: Path, events: list, status: int, output: str) -> None:
    stream = tmp_path / "watch.jsonl"
    stream.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", "-", str(stream)], input=_replay_parser(), capture_output=True,
                          text=True, check=False)
    assert (proc.returncode, proc.stdout) == (status, output), proc.stderr


# ---------------------------------------------------------------------------
# The guide's short commands, each in the step that has the state it needs
# ---------------------------------------------------------------------------

NOTES = ROOT / "charts" / "pubdex" / "templates" / "NOTES.txt"
DEMO_DOI = "10.5555/pubdex-fixture-001"


def _raw_step(number: int) -> str:
    """Step <number> of the script as it stands, continuation lines kept, so
    that the Python it holds can run."""
    text = _script()
    start = text.index(f'\nstep {number} "')
    return text[start:text.index(f'\nstep {number + 1} "', start)]


def _heredoc(text: str, after: str) -> str:
    """The first Python heredoc of <text> after <after>."""
    return re.search(r"<<'PY'[^\n]*\n(.*?)^PY$", text[_at(text, after):], re.S | re.M).group(1)


def _run_python(program: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-", *args], input=program, capture_output=True, text=True,
                          check=False)


def test_step_7_reads_the_notes_again_and_finds_the_port_forward() -> None:
    step = _step(7)
    assert _at(step, 'read_back "revision 1"') < _at(step, 'helm_ get notes "$RELEASE" --namespace "$NAMESPACE"') \
        < _at(step, "helm_run test")
    assert 'forward_line="kubectl --namespace $NAMESPACE port-forward service/$RELEASE-api 8000:8000"' in step
    assert 'grep -Fqx "  $forward_line" "$WORK/notes.log"' in step
    assert ("\n  kubectl --namespace {{ $ns }} port-forward service/{{ $api }} "
            "8000:{{ .Values.api.service.port | int64 }}\n") in NOTES.read_text(encoding="utf-8")


def _computed_values(**changes: object) -> dict:
    """Revision 1's values as helm get values --all shows them, with <changes>
    as dotted keys."""
    values = yaml.safe_load((ROOT / "charts" / "pubdex" / "values.yaml").read_text(encoding="utf-8"))
    values["secrets"]["name"], values["demo"]["enabled"] = "pubdex-credentials", True
    for dotted, value in changes.items():
        *parents, leaf = dotted.split(".")
        node = values
        for parent in parents:
            node = node[parent]
        node[leaf] = value
    return values


@pytest.mark.parametrize(
    ("header", "changes", "status"),
    [
        ("COMPUTED VALUES:", {}, 0),
        ("USER-SUPPLIED VALUES:", {}, 1),
        ("COMPUTED VALUES:", {"api.logLevel": "DEBUG"}, 1),
        ("COMPUTED VALUES:", {"api.replicas": 2}, 1),
        ("COMPUTED VALUES:", {"image.digest": "sha256:" + "0" * 64}, 1),
        ("COMPUTED VALUES:", {"postgresImage.tag": "other"}, 1),
        ("COMPUTED VALUES:", {"demo.enabled": False}, 1),
    ],
)
def test_step_9_reads_revision_1s_values_with_all_after_the_rollback(
        tmp_path: Path, header: str, changes: dict, status: int) -> None:
    step = _step(9)
    assert _at(step, "helm_run rollback") < _at(step, 'helm_ history "$RELEASE" --namespace "$NAMESPACE"') \
        < _at(step, 'helm_ get values "$RELEASE" --namespace "$NAMESPACE" --all > "$WORK/values-all.log"')
    output = tmp_path / "values-all.log"
    output.write_text(header + "\n" + yaml.safe_dump(_computed_values(**changes)), encoding="utf-8")
    proc = _run_python(_heredoc(_raw_step(9), "helm_ get values"), str(output), *_published(), "pubdex-credentials")
    assert proc.returncode == status, proc.stderr


def test_step_12_labels_a_pod_that_could_not_reach_the_read_service(tmp_path: Path) -> None:
    step = _step(12)
    assert _at(step, 'wait_until "$WAIT_SECONDS" probes_ended') < _at(step, "wait_until 60 labelled_tried") \
        < _at(step, 'k label pod probe-labelled "$RELEASE-client=true"') < _at(step, "wait_until 60 labelled_ended") \
        < _at(step, 'k delete pods --selector "$PROBE_LABEL"')
    assert '[ "$(head -n 1 "$WORK/labelled.txt")" != connected ]' in step
    assert '[ "$(tail -n 1 "$WORK/labelled.txt")" = connected ]' in step
    assert "'^(outsider|client|ingest) (Succeeded|Failed)$'" in _script(), "the three probes end without the fourth"
    manifests = tmp_path / "probes.yaml"
    proc = _run_python(_heredoc(_raw_step(12), "step 12"), str(manifests), "app@sha256:" + "0" * 64, "pubdex",
                       "probe-label")
    assert proc.returncode == 0, proc.stderr
    pods = {pod["metadata"]["name"]: pod for pod in yaml.safe_load_all(manifests.read_text(encoding="utf-8"))}
    assert sorted(pods) == ["probe-client", "probe-ingest", "probe-labelled", "probe-outsider"]
    commands = {name: pod["spec"]["containers"][0]["command"] for name, pod in pods.items()}
    for name in ("probe-outsider", "probe-client", "probe-ingest"):
        assert commands[name][4:] == ["pubdex-postgresql:5432", "pubdex-api:8000"], name
    assert pods["probe-labelled"]["metadata"]["labels"] == {"probe-label": "labelled"}, \
        "no label the NetworkPolicy admits until the run gives it one"
    assert commands["probe-labelled"][:3] == ["python", "-I", "-c"] and commands["probe-labelled"][4:] == ["pubdex-api:8000"]
    assert pods["probe-labelled"]["spec"]["securityContext"] == pods["probe-outsider"]["spec"]["securityContext"]


@pytest.mark.parametrize(("refusals", "status", "output"), [
    (2, 0, "TimeoutError\nTimeoutError\nconnected\n"),
    (300, 1, "TimeoutError\n" * 300),
])
def test_the_labelled_probe_tries_until_it_connects(monkeypatch, capsys, tmp_path: Path, refusals: int,
                                                    status: int, output: str) -> None:
    manifests = tmp_path / "probes.yaml"
    _run_python(_heredoc(_raw_step(12), "step 12"), str(manifests), "app", "pubdex", "probe-label")
    (program,) = [pod["spec"]["containers"][0]["command"][3]
                  for pod in yaml.safe_load_all(manifests.read_text(encoding="utf-8"))
                  if pod["metadata"]["name"] == "probe-labelled"]
    attempts = []

    class Connection:
        def close(self) -> None:
            pass

    def connect(address: tuple, timeout: float) -> Connection:
        assert (address, timeout) == (("pubdex-api", 8000), 1)
        attempts.append(address)
        if len(attempts) <= refusals:
            raise TimeoutError()
        return Connection()

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["-c", "pubdex-api:8000"])
    with pytest.raises(SystemExit) as end:
        exec(program, {"__name__": "__main__"})
    assert (end.value.code, capsys.readouterr().out) == (status, output)


def test_each_ingestion_job_s_log_is_followed_to_its_end_then_read_again() -> None:
    text = _script()
    body = text[_at(text, "\nrun_task() {"):]
    body = re.sub(r"\\\n\s*", " ", body[:body.index("\n}\n")])
    assert _at(body, 'create job --from="cronjob/$RELEASE-ingest-$1" "$run"') < _at(body, 'job_started "$run"') \
        < _at(body, 'k logs --follow "job/$run" > "$WORK/job-$1.follow"') < _at(body, 'job_ended "$run"') \
        < _at(body, 'k logs "job/$run" > "$WORK/job-$1.log"') \
        < _at(body, 'cmp -s "$WORK/job-$1.follow" "$WORK/job-$1.log"')
    assert re.findall(r"^run_task (\w+)$", _step(15), re.M) == ["fixture", "rules"]
    assert 'logs --follow "job/$run"' in NOTES.read_text(encoding="utf-8"), "the command the notes print"


def test_step_15_exports_through_the_attached_pod_after_the_demonstration() -> None:
    step = _step(15)
    runs = re.findall(r'^job(?:_input)? 300 k run "\$RELEASE-run" (.*?) -- python -m people_pubs\.sync\.(\w+)\s+(.*)$',
                      step, re.M)
    assert [module for _, module, _ in runs] == ["add_people", "export_publications"]
    assert runs[0][0] == runs[1][0], "the export runs with the roster import's options and patch"
    assert runs[1][2].startswith('> "$WORK/publications.csv" 2> "$WORK/export.log" ||'), \
        "standard output alone into the CSV, as in the guide"
    assert _at(step, "\nrun_task rules\n") < _at(step, "people_pubs.sync.export_publications") \
        < _at(step, 'read_back "external revision 1"')
    assert 'grep -Eq "Exported $exported publications to stdout" "$WORK/export.log"' in step


def _export(rows: list, extra: str = "") -> str:
    """What export_publications writes to standard output for <rows>, and <extra>."""
    stream = io.StringIO()
    write_publications_csv(rows, stream)
    return stream.getvalue() + extra


DEMO_ROW = {"pub_id": 7, "doi": DEMO_DOI, "title": "Synthetic Collective Behaviour Paper", "year": 2024,
            "venue": "Journal of Synthetic Metadata", "source_of_truth": "crossref+orcid",
            "internal_authors": "Ada Example"}


@pytest.mark.parametrize(("content", "status", "output"), [
    (lambda: _export([DEMO_ROW]), 0, "1\n"),
    (lambda: _export([dict(DEMO_ROW, pub_id=8, doi="10.5555/other", title="Other"), DEMO_ROW]), 0, "2\n"),
    (lambda: _export([DEMO_ROW], "INFO Exported 1 publications to stdout\n"), 1, ""),
    (lambda: _export([]), 1, ""),
    (lambda: _export([DEMO_ROW, dict(DEMO_ROW, pub_id=8)]), 1, ""),
    (lambda: _export([dict(DEMO_ROW, internal_authors="")]), 1, ""),
    (lambda: "", 1, ""),
], ids=["the-demonstration", "two-rows", "the-log-on-standard-output", "no-row", "the-doi-twice",
        "no-internal-author", "empty"])
def test_the_exports_csv_is_checked_row_by_row(tmp_path: Path, content, status: int, output: str) -> None:
    csv_file = tmp_path / "publications.csv"
    csv_file.write_text(content(), encoding="utf-8")
    proc = _run_python(_heredoc(_raw_step(15), "people_pubs.sync.export_publications"), str(csv_file), DEMO_DOI)
    assert (proc.returncode, proc.stdout) == (status, output), proc.stderr
