"""Pod Security "restricted" and the credentials, in every rendered case.

Every pod the chart declares has to be admitted to a namespace that enforces
the Pod Security Standard "restricted", and has to run with a read-only root
file system, writing only to volumes of its own. The chart takes no password
and renders no Secret: each container receives exactly the keys of the
operator's Secret that chart.CONTAINERS lists for it, as files readable by
its group, and no credential reaches an environment variable or a setting.
In external mode only the setup Job's container receives the admin
Secret's key.
"""
from __future__ import annotations

import json

import pytest

from chart import (
    ATTACHED_PATCH_KEY, CASES, CONTAINERS, PASSWORD_LIKE, RELEASE, SETUP_SCRIPT, ca_source, case_values, containers, is_external,
    mounted_secret_keys, pod_templates, secret_volumes, setup_enabled,
)

# The volume types "restricted" admits.
RESTRICTED_VOLUMES = {"configMap", "csi", "downwardAPI", "emptyDir", "ephemeral", "persistentVolumeClaim",
                      "projected", "secret"}


def _volume_types(pod) -> dict:
    """Each volume a pod's containers can mount: name -> its type."""
    types = {}
    for volume in pod.spec.get("volumes") or []:
        kinds = set(volume) - {"name"}
        assert len(kinds) == 1, f"{pod.where}: volume {volume['name']} has the types {sorted(kinds)}"
        types[volume["name"]] = kinds.pop()
    if pod.owner["kind"] == "StatefulSet":
        for claim in pod.owner["spec"].get("volumeClaimTemplates") or []:
            types[claim["metadata"]["name"]] = "persistentVolumeClaim"
    return types


@pytest.mark.parametrize("case", CASES)
def test_every_pod_meets_pod_security_restricted(helm, case: str) -> None:
    _assert_restricted(pod_templates(helm.template("--values", CASES[case])))


@pytest.mark.parametrize("case", ["values/evaluation", "values/external-minimal"])
def test_every_pod_meets_pod_security_restricted_with_every_scheduling_value_set(helm, case: str) -> None:
    # All six top-level keys, and the read service's and PostgreSQL's own.
    settings = [arg for key, value in SCHEDULING.items() for arg in ("--set-json", f"{key}={json.dumps(value)}")]
    settings += [arg for component in ("api", "postgresql") for key, value in SCHEDULING.items()
                 for arg in ("--set-json", f"{component}.{key}={json.dumps(value)}")]
    settings += ["--set-json", 'ingestion.tasks={"x":{"command":["python","-m","people_pubs.sync.orcid_works"]}}']
    pods = pod_templates(helm.template("--values", CASES[case], *settings))
    for pod in pods:
        assert pod.spec["priorityClassName"] == SCHEDULING["priorityClassName"], pod.where
    _assert_restricted(pods)


# Every scheduling and metadata value of the pods, set.
SCHEDULING = {
    "nodeSelector": {"example.org/pool": "pubdex"},
    "tolerations": [{"key": "example.org/dedicated", "operator": "Equal", "value": "pubdex", "effect": "NoSchedule"}],
    "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
        {"matchExpressions": [{"key": "example.org/zone", "operator": "In", "values": ["a"]}]}]}}},
    "podAnnotations": {"example.org/owner": "curation"},
    "podLabels": {"example.org/team": "curation"},
    "priorityClassName": "pubdex-normal",
}


def _assert_restricted(pods) -> None:
    """Every pod meets the Pod Security Standard "restricted"."""
    assert pods, "the chart renders pods"
    for pod in pods:
        spec, where = pod.spec, pod.where
        context = spec.get("securityContext") or {}
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            assert not spec.get(field), f"{where}: {field}"
        assert context.get("runAsNonRoot") is True, where
        assert context.get("seccompProfile") == {"type": "RuntimeDefault"}, where
        assert not context.get("sysctls"), where
        for volume, kind in _volume_types(pod).items():
            assert kind in RESTRICTED_VOLUMES, f"{where}: volume {volume} is a {kind}"
        for container in containers(pod):
            name = f"{where}, container {container['name']}"
            own = container.get("securityContext") or {}
            assert own.get("allowPrivilegeEscalation") is False, name
            assert not own.get("privileged"), name
            assert "ALL" in (own.get("capabilities") or {}).get("drop", []), name
            assert not (own.get("capabilities") or {}).get("add"), name
            assert own.get("runAsNonRoot") is not False, name
            assert own.get("seccompProfile", {"type": "RuntimeDefault"}) == {"type": "RuntimeDefault"}, name
            for field in ("runAsUser", "runAsGroup"):
                identity = own.get(field, context.get(field))
                assert isinstance(identity, int) and identity > 0, f"{name}: {field} {identity!r}"
            for port in container.get("ports") or []:
                assert "hostPort" not in port, name


@pytest.mark.parametrize("case", CASES)
def test_every_container_has_a_read_only_root_and_writes_only_to_its_own_volumes(helm, case: str) -> None:
    for pod in pod_templates(helm.template("--values", CASES[case])):
        types = _volume_types(pod)
        for container in containers(pod):
            name = f"{pod.where}, container {container['name']}"
            entitled = CONTAINERS.get((pod.component, container["name"]))
            assert entitled, f"{name} is not listed in chart.CONTAINERS"
            assert (container.get("securityContext") or {}).get("readOnlyRootFilesystem") is True, name
            writable = {mount["mountPath"]: types[mount["name"]] for mount in container.get("volumeMounts") or []
                        if not mount.get("readOnly")}
            assert set(writable) == entitled.writable, name
            for path, kind in writable.items():
                assert kind in ("emptyDir", "persistentVolumeClaim"), f"{name}: {path} is a {kind}"


@pytest.mark.parametrize("case", CASES)
def test_postgresql_runs_as_its_image_user_with_its_data_in_a_subdirectory(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    servers = [pod for pod in pod_templates(rendered) if pod.component == "postgresql"]
    if is_external(values):
        # The operator's server: the chart runs none (test_render_external).
        assert servers == [], "external mode runs no PostgreSQL"
        return
    (pod,) = servers
    context = pod.spec["securityContext"]
    assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (999, 999, 999)
    assert context["fsGroupChangePolicy"] == "OnRootMismatch"
    assert pod.spec["terminationGracePeriodSeconds"] == values["postgresql"]["terminationGracePeriodSeconds"]
    (bootstrap,) = pod.spec["initContainers"]
    assert (bootstrap["securityContext"]["runAsUser"], bootstrap["securityContext"]["runAsGroup"]) == (10001, 10001)
    (server,) = pod.spec["containers"]
    # No lifecycle.stopSignal: it is an alpha field; the image's own stop
    # signal, SIGINT, shuts the server down fast.
    assert "lifecycle" not in server
    env = {entry["name"]: entry.get("value") for entry in server["env"]}
    data = next(m["mountPath"] for m in server["volumeMounts"] if m["name"] == "data")
    assert env["PGDATA"].startswith(data + "/"), "the data directory is a subdirectory of the volume"
    initdb = {m["name"]: m for m in server["volumeMounts"]}["initdb"]
    assert initdb["mountPath"] == "/docker-entrypoint-initdb.d" and initdb["readOnly"] is True
    shm = next(v for v in pod.spec["volumes"] if v["name"] == "shm")["emptyDir"]
    assert shm == {"medium": "Memory", "sizeLimit": values["postgresql"]["shmSize"]}
    retention = pod.owner["spec"]["persistentVolumeClaimRetentionPolicy"]
    assert retention["whenDeleted"] == ("Retain" if values["postgresql"]["persistence"]["retainOnUninstall"]
                                        else "Delete")


@pytest.mark.parametrize("case", CASES)
def test_the_api_runs_as_its_image_user(helm, case: str) -> None:
    (pod,) = [pod for pod in pod_templates(helm.template("--values", CASES[case])) if pod.component == "api"]
    context = pod.spec["securityContext"]
    assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (10001, 10001, 10001)


@pytest.mark.parametrize("case", CASES)
def test_credentials_reach_each_container_only_as_files_of_its_own_keys(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    assert not rendered.of_kind("Secret"), "the chart renders no Secret"
    credentials = values["secrets"]["name"]
    admin = values["externalDatabase"]["setup"]["adminSecret"]
    ca = ca_source(values)
    # The Secrets a pod may name: the operator's, the admin Secret of the
    # setup Job, and a CA certificate kept in a Secret (not a credential).
    allowed = {credentials}
    if setup_enabled(values):
        allowed.add(admin["name"])
    if ca and ca[0] == "secret":
        allowed.add(ca[1])
    for pod in pod_templates(rendered):
        volumes = secret_volumes(pod)
        for volume, secret in volumes.items():
            name = f"{pod.where}, volume {volume}"
            assert secret["secretName"] in allowed, name
            assert secret.get("items"), f"{name}: only the listed keys, never the whole Secret"
            assert secret.get("defaultMode") == 0o440, f"{name}: readable by the pod's group only"
            for item in secret["items"]:
                assert item["path"] == item["key"] and "mode" not in item, name
        for container in containers(pod):
            name = f"{pod.where}, container {container['name']}"
            entitled = CONTAINERS.get((pod.component, container["name"]))
            assert entitled, f"{name} is not listed in chart.CONTAINERS"
            files = mounted_secret_keys(pod, container)
            by_secret: dict = {}
            for secret_name, key in files.values():
                by_secret.setdefault(secret_name, []).append(key)
            assert sorted(by_secret.pop(credentials, [])) == sorted(entitled.secret_keys), name
            if entitled.admin and setup_enabled(values):
                assert by_secret.pop(admin["name"], []) == [admin["key"]], f"{name}: the admin password"
            if entitled.connects and ca and ca[0] == "secret":
                assert by_secret.pop(ca[1], []) == [ca[2]], f"{name}: the CA certificate"
            assert not by_secret, f"{name} receives keys of other Secrets: {by_secret}"
            files = {path: key for path, (_secret, key) in files.items()}
            for mount in container.get("volumeMounts") or []:
                if mount["name"] in volumes:
                    assert mount.get("readOnly") is True, name
            for source in container.get("envFrom") or []:
                assert "secretRef" not in source, name
            for entry in container.get("env") or []:
                assert "secretKeyRef" not in (entry.get("valueFrom") or {}), f"{name}: {entry['name']}"
                if PASSWORD_LIKE.search(entry["name"]):
                    assert entry["name"].endswith("_FILE"), f"{name}: {entry['name']} carries a credential"
                    assert entry.get("value") in files, f"{name}: {entry['name']} names no file it receives"


@pytest.mark.parametrize("case", CASES)
def test_no_setting_carries_a_credential(helm, case: str) -> None:
    rendered = helm.template("--values", CASES[case])
    for config in rendered.of_kind("ConfigMap"):
        if config["metadata"]["name"] == f"{RELEASE}-setup":
            # The setup script, which talks about passwords: exactly the
            # chart's file, so no value can have reached it.
            assert config["data"] == {"external-setup.sh": SETUP_SCRIPT.read_text(encoding="utf-8")}
            continue
        if config["metadata"]["name"] == f"{RELEASE}-run":
            # The attached pod's patch, which names app_writer's password
            # file: that pod is checked as every other pod is, here and in
            # the tests above.
            assert set(config["data"]) == {ATTACHED_PATCH_KEY}
            assert "PGPASSWORD" not in config["data"][ATTACHED_PATCH_KEY]
            continue
        for key, value in (config.get("data") or {}).items():
            assert not PASSWORD_LIKE.search(key), f"ConfigMap {config['metadata']['name']}: {key}"
            assert "password" not in value.lower(), f"ConfigMap {config['metadata']['name']}: {key}"
    for pod in pod_templates(rendered):
        for container in containers(pod):
            for entry in container.get("env") or []:
                assert "password=" not in str(entry.get("value", "")).lower(), f"{pod.where}: {entry['name']}"


def test_every_listed_container_is_rendered_by_some_case(helm) -> None:
    rendered = set()
    for path in CASES.values():
        for pod in pod_templates(helm.template("--values", path)):
            rendered.update((pod.component, container["name"]) for container in containers(pod))
    assert rendered == set(CONTAINERS), "chart.CONTAINERS lists exactly the containers the cases render"
