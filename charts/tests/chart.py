"""What the chart checks share: the chart, the cases they render, and how to
read the manifests helm renders.

A case is one values file, given to helm with --values:

- every file under values/ in this directory, which the checks define:
  default.yaml is the chart's defaults with only the required Secret name
  added, evaluation.yaml changes every setting evaluation mode has (the
  demonstration, ingestion tasks, an Ingress, NetworkPolicy peers and the
  pods' scheduling and metadata values included), external-minimal.yaml is
  external mode with only what it requires, without a CA certificate and so
  with an explicitly weaker sslmode, and external-manual.yaml external mode
  without the setup Job, with an ingestion task and without
  NetworkPolicies;
- every file under the chart's examples/, which the documentation offers to
  operators, as it is. An example therefore names its Secret itself.

run_chart_checks.sh lints the chart with the same files. A new values file or
example is a new case of every check that takes CASES, without any change
here.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Tuple

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHART = ROOT / "charts" / "pubdex"
VALUES_DIR = HERE / "values"
EXAMPLES_DIR = CHART / "examples"

# The release and namespace the cases are rendered as; other tests pick
# other names to show that nothing depends on these.
RELEASE = "checks"
NAMESPACE = "chart-checks"

# The oldest and the newest Kubernetes release the chart supports, as
# run_chart_checks.sh lints them; the cases are rendered for the newest.
KUBE_OLDEST = "1.35.0"
KUBE_NEWEST = "1.37.0"


def _case_files() -> Dict[str, Path]:
    files = {f"values/{path.stem}": path for path in sorted(VALUES_DIR.glob("*.yaml"))}
    files.update({f"examples/{path.stem}": path for path in sorted(EXAMPLES_DIR.glob("*.yaml"))})
    return files


CASES: Dict[str, Path] = _case_files()


@dataclass(frozen=True)
class Container:
    """What one container of the chart is entitled to."""

    image: str  # the values key of its image: image or postgresImage
    secret_keys: FrozenSet[str]  # the keys of the operator's Secret it mounts, exactly
    writable: FrozenSet[str]  # where it may write, exactly; its root file system is read-only
    admin: bool = False  # mounts the external server's admin Secret (externalDatabase.setup.adminSecret)
    connects: bool = False  # connects to an external server, so mounts its CA certificate when one is set
    # The keys of the operator's Secret it mounts in external mode, where
    # they differ from secret_keys; None when they do not.
    external_secret_keys: Optional[FrozenSet[str]] = None

    def keys_in(self, values: dict) -> FrozenSet[str]:
        """The keys of the operator's Secret it mounts with these values."""
        if values["mode"] == "external" and self.external_secret_keys is not None:
            return self.external_secret_keys
        return self.secret_keys


# Every container the chart renders, by (component label, container name).
# A container that is not listed fails the checks, so a new one has to say
# here which image it runs, which Secret keys it receives and where it writes.
CONTAINERS: Dict[Tuple[str, str], Container] = {
    ("postgresql", "bootstrap"): Container("image", frozenset(), frozenset({"/var/lib/pubdex/initdb", "/tmp"})),
    ("postgresql", "postgresql"): Container(
        "postgresImage",
        frozenset({"postgres_password", "app_readonly_password", "app_writer_password"}),
        frozenset({"/var/lib/postgresql/data", "/var/run/postgresql", "/tmp", "/dev/shm"}),
    ),
    ("api", "api"): Container("image", frozenset({"app_readonly_password"}), frozenset({"/tmp"}), connects=True),
    ("setup", "bootstrap"): Container("image", frozenset(), frozenset({"/var/lib/pubdex/initdb", "/tmp"})),
    ("setup", "setup"): Container(
        "postgresImage", frozenset({"app_readonly_password", "app_writer_password"}), frozenset({"/tmp"}),
        admin=True, connects=True,
    ),
    # The ingestion runs of the CronJobs, the demonstration, and the attached
    # one-off pod, whose container kubectl run names after the pod.
    ("ingest", "ingest"): Container("image", frozenset({"app_writer_password"}), frozenset({"/tmp"}), connects=True),
    ("demo", "demo"): Container("image", frozenset({"app_writer_password"}), frozenset({"/tmp"}), connects=True),
    ("ingest", f"{RELEASE}-run"): Container("image", frozenset({"app_writer_password"}), frozenset({"/tmp"}),
                                            connects=True),
    # helm test's pod, a client of the read service without any credential.
    ("test", "test"): Container("image", frozenset(), frozenset({"/tmp"})),
    # The schema upgrade: as postgres with that role's key in evaluation
    # mode, as the admin role with the admin Secret's key alone in external
    # mode.
    ("schema-upgrade", "schema-upgrade"): Container(
        "image", frozenset({"postgres_password"}), frozenset({"/tmp"}), admin=True, connects=True,
        external_secret_keys=frozenset(),
    ),
}

# The synthetic roster of the demonstration, which the image carries; the
# checks give it to the attached pod on standard input only.
DEMO_ROSTER = ROOT / "DB" / "tests" / "fixtures" / "golden" / "demo_roster_minimal.csv"
COMPOSE = ROOT / "compose.yaml"

# Where a container that connects to an external server finds its CA
# certificate, and the name of that volume.
CA_DIR = "/run/pubdex/ca"
CA_VOLUME = "database-ca"
# The script of external mode's setup Job, and the three lines of
# DB/init/000_init.sql it leaves out (design: no image change).
SETUP_SCRIPT = CHART / "files" / "external-setup.sh"
INIT_SQL = ROOT / "DB" / "init" / "000_init.sql"
LEFT_OUT_LINES = (
    "ALTER SYSTEM SET password_encryption = 'scram-sha-256';",
    "SELECT pg_reload_conf();",
    "GRANT USAGE ON SCHEMA public TO postgres;",
)


# --- values -----------------------------------------------------------------

def chart_values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


def values_section(key: str) -> str:
    """The paragraph of values.yaml that documents a top-level key: its
    comment and its values, up to the next blank line, as one line of text
    without the comment marks."""
    text = (CHART / "values.yaml").read_text(encoding="utf-8")
    at = text.index(f"\n{key}:\n")
    start = text.rfind("\n\n", 0, at)
    end = text.find("\n\n", at)
    block = text[start:] if end == -1 else text[start:end]
    return " ".join(line.strip().lstrip("#").strip() for line in block.splitlines() if line.strip())


def merge(base: dict, override: dict) -> dict:
    """Values as Helm combines them: maps merge key by key, anything else --
    lists included -- replaces, and null removes the key. (Helm keeps a null
    for a key the chart gives no value, such as an override of the pods'
    scheduling values; the chart reads that null as absent, as here.)"""
    result = dict(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def case_values(case: str) -> dict:
    """The values a case renders with: the chart's, then the case's file."""
    return merge(chart_values(), yaml.safe_load(CASES[case].read_text(encoding="utf-8")) or {})


# The scheduling and metadata values of the pods (values.yaml): set at the
# top level for every pod, and overridden by api and postgresql for theirs.
POD_KEYS = ("nodeSelector", "tolerations", "affinity", "podAnnotations", "podLabels", "priorityClassName")
OVERRIDING_COMPONENTS = ("api", "postgresql")


def pod_values(values: dict, component: str) -> dict:
    """The six values a pod of the component gets: api and postgresql their
    own where it is set, neither left out nor null, the top-level ones
    otherwise; every other component the top-level ones."""
    own = values.get(component) or {} if component in OVERRIDING_COMPONENTS else {}
    return {key: own[key] if own.get(key) is not None else values[key] for key in POD_KEYS}


def is_external(values: dict) -> bool:
    return values["mode"] == "external"


def setup_enabled(values: dict) -> bool:
    """Whether a case renders external mode's setup Job."""
    return is_external(values) and values["externalDatabase"]["setup"]["enabled"]


def ca_source(values: dict) -> Optional[Tuple[str, str, str]]:
    """The CA certificate a case configures, as (secret or configMap, name,
    key); None when it configures none."""
    ca = values["externalDatabase"]["caCertificate"]
    if not is_external(values):
        return None
    if ca["secretName"]:
        return ("secret", ca["secretName"], ca["key"])
    if ca["configMapName"]:
        return ("configMap", ca["configMapName"], ca["key"])
    return None


def image_reference(image: dict) -> str:
    """The reference the chart builds from an image value:
    <registry>/<repository>:<tag>@<digest>, without the parts left empty."""
    reference = f"{image['registry']}/{image['repository']}" if image["registry"] else image["repository"]
    if image["tag"]:
        reference += f":{image['tag']}"
    if image["digest"]:
        reference += f"@{image['digest']}"
    return reference


# --- rendered manifests -------------------------------------------------------

@dataclass(frozen=True)
class Rendered:
    """The output of one helm template: its text and the parsed objects."""

    text: str
    objects: Tuple[dict, ...]

    def of_kind(self, kind: str) -> List[dict]:
        return [obj for obj in self.objects if obj["kind"] == kind]

    def one(self, kind: str, name: str) -> dict:
        found = [obj for obj in self.of_kind(kind) if obj["metadata"]["name"] == name]
        assert len(found) == 1, f"{kind}/{name}: {len(found)} rendered"
        return found[0]

    def names(self) -> List[Tuple[str, str]]:
        return [(obj["kind"], obj["metadata"]["name"]) for obj in self.objects]


def parse(text: str) -> Rendered:
    return Rendered(text, tuple(doc for doc in yaml.safe_load_all(text) if doc is not None))


@dataclass(frozen=True)
class PodTemplate:
    """A pod the chart makes Kubernetes run, wherever it is declared."""

    owner: dict  # the object that declares it
    metadata: dict
    spec: dict

    @property
    def where(self) -> str:
        return f"{self.owner['kind']}/{self.owner['metadata']['name']}"

    @property
    def component(self) -> str:
        return self.metadata.get("labels", {}).get("app.kubernetes.io/component", "")


def pod_templates(rendered: Rendered) -> List[PodTemplate]:
    """Every pod template: those of Pods, of the workload controllers, of
    the Jobs that CronJobs create, and the attached one-off pod that
    kubectl run builds with the patch of a ConfigMap (attached_pod)."""
    pods = []
    for obj in rendered.objects:
        kind, spec = obj["kind"], obj.get("spec", {})
        if kind == "Pod":
            template = {"metadata": obj["metadata"], "spec": spec}
        elif kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
            template = spec["template"]
        elif kind == "CronJob":
            template = spec["jobTemplate"]["spec"]["template"]
        elif kind == "ConfigMap" and ATTACHED_PATCH_KEY in (obj.get("data") or {}):
            template = attached_pod(obj)
        else:
            continue
        pods.append(PodTemplate(obj, template.get("metadata", {}), template["spec"]))
    return pods


# --- the attached one-off pod -------------------------------------------------

# The key of the ConfigMap <release>-run that holds the JSON patch for
# kubectl run --override-type=json.
ATTACHED_PATCH_KEY = "overrides"
# The command the documentation gives that pod for a roster.
ROSTER_COMMAND = ["python", "-m", "people_pubs.sync.add_people", "--csv", "-"]


def kubectl_run_pod(name: str, image: str, args: List[str]) -> dict:
    """The pod kubectl run <name> -i --restart=Never --image=<image> --
    <args> builds before it applies an override: one container named after
    the pod that runs args with its standard input open (kubectl's generator
    for a basic pod). The cluster run of the chart's acceptance checks the
    same fields on the pod a real kubectl creates."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"run": name}},
        "spec": {
            "containers": [{"name": name, "image": image, "args": list(args), "stdin": True, "stdinOnce": True,
                            "tty": False, "resources": {}}],
            "dnsPolicy": "ClusterFirst",
            "restartPolicy": "Never",
        },
    }


def _pointer(path: str) -> List[str]:
    assert path.startswith("/"), path
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def apply_json_patch(document: dict, operations: List[dict]) -> dict:
    """A JSON patch (RFC 6902) of "add" operations, as kubectl applies it:
    a member is set whether or not it exists, and an array element is
    inserted. The chart's patch uses no other operation."""
    result = copy.deepcopy(document)
    for operation in operations:
        assert set(operation) == {"op", "path", "value"} and operation["op"] == "add", operation
        *parents, last = _pointer(operation["path"])
        target = result
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]
        if isinstance(target, list):
            target.insert(len(target) if last == "-" else int(last), copy.deepcopy(operation["value"]))
        else:
            target[last] = copy.deepcopy(operation["value"])
    return result


def attached_patch(config: dict) -> List[dict]:
    return json.loads(config["data"][ATTACHED_PATCH_KEY])


def attached_pod(config: dict, args: Optional[List[str]] = None) -> dict:
    """The attached pod as the documented kubectl run command creates it
    from the ConfigMap config: named like it, with a placeholder image that
    the patch replaces, running args (the roster import by default)."""
    name = config["metadata"]["name"]
    pod = apply_json_patch(kubectl_run_pod(name, "placeholder.invalid/image", args or ROSTER_COMMAND),
                           attached_patch(config))
    return {"metadata": pod["metadata"], "spec": pod["spec"]}


def containers(pod: PodTemplate) -> Iterator[dict]:
    """Every container of a pod: init, ordinary and ephemeral."""
    for field in ("initContainers", "containers", "ephemeralContainers"):
        yield from pod.spec.get(field) or []


def secret_volumes(pod: PodTemplate) -> Dict[str, dict]:
    """The pod's Secret volumes, by volume name."""
    return {volume["name"]: volume["secret"] for volume in pod.spec.get("volumes") or [] if "secret" in volume}


def mounted_secret_keys(pod: PodTemplate, container: dict) -> Dict[str, Tuple[str, str]]:
    """The Secret keys a container receives as files: path -> (Secret, key)."""
    volumes = secret_volumes(pod)
    files = {}
    for mount in container.get("volumeMounts") or []:
        secret = volumes.get(mount["name"])
        if secret is None:
            continue
        for item in secret.get("items") or []:
            files[f"{mount['mountPath'].rstrip('/')}/{item['path']}"] = (secret["secretName"], item["key"])
    return files


def is_hook(obj: dict) -> bool:
    """Whether helm runs the object as a hook rather than keeping it with the release."""
    return "helm.sh/hook" in (obj["metadata"].get("annotations") or {})


def env_value(container: dict, name: str) -> Optional[dict]:
    """A container's environment entry of that name, or None."""
    return next((entry for entry in container.get("env") or [] if entry["name"] == name), None)


PASSWORD_LIKE = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL|API_?KEY|PRIVATE_?KEY", re.I)
