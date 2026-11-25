"""The rendered objects: API versions, names, labels, selectors and references,
images, probes, the settings and their checksum, and NOTES.txt."""
from __future__ import annotations

import re

import pytest
import yaml

from chart import (
    CA_DIR, CASES, CHART, CONTAINERS, NAMESPACE, RELEASE, ROOT, ca_source, case_values, chart_values, containers,
    env_value, image_reference, is_external, pod_templates,
)

DEFAULT = ("--values", CASES["values/default"])
# The API versions the chart may use: only those that are generally
# available in every Kubernetes release it supports.
GA_API_VERSIONS = {"v1", "apps/v1", "batch/v1", "networking.k8s.io/v1", "policy/v1"}
NAMESPACED_KINDS = {"ConfigMap", "Service", "ServiceAccount", "Deployment", "StatefulSet", "Job", "CronJob", "Pod",
                    "Ingress", "NetworkPolicy", "PersistentVolumeClaim", "PodDisruptionBudget"}
SELECTOR_LABELS = {"app.kubernetes.io/name", "app.kubernetes.io/instance", "app.kubernetes.io/component"}


def _chart() -> dict:
    return yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES)
def test_only_generally_available_apis_and_namespaced_objects(helm, case: str) -> None:
    for obj in helm.template("--values", CASES[case]).objects:
        assert obj["apiVersion"] in GA_API_VERSIONS, f"{obj['kind']} {obj['apiVersion']}"
        assert obj["kind"] in NAMESPACED_KINDS, f"{obj['kind']} is not an object of the release's namespace"


@pytest.mark.parametrize("case", CASES)
def test_no_value_is_missing_from_a_rendering(helm, case: str) -> None:
    text = helm.template("--values", CASES[case]).text
    assert "<no value>" not in text and "%!" not in text


@pytest.mark.parametrize("case", CASES)
def test_every_object_is_labelled_named_after_the_release_and_in_its_namespace(helm, case: str) -> None:
    chart = _chart()
    for obj in helm.template("--values", CASES[case]).objects:
        meta, what = obj["metadata"], f"{obj['kind']}/{obj['metadata']['name']}"
        assert meta["name"] == RELEASE or meta["name"].startswith(f"{RELEASE}-"), what
        assert meta["namespace"] == NAMESPACE, what
        labels = meta["labels"]
        assert labels["app.kubernetes.io/name"] == chart["name"], what
        assert labels["app.kubernetes.io/instance"] == RELEASE, what
        assert labels["app.kubernetes.io/managed-by"] == "Helm", what
        assert labels["app.kubernetes.io/part-of"] == "pubdex", what
        assert labels["app.kubernetes.io/version"] == chart["appVersion"], what
        assert labels["helm.sh/chart"] == f"{chart['name']}-{chart['version']}", what


@pytest.mark.parametrize("case", CASES)
def test_selectors_select_their_own_pods_and_references_resolve(helm, case: str) -> None:
    rendered = helm.template("--values", CASES[case])
    names = set(rendered.names())
    pods = pod_templates(rendered)
    for pod in pods:
        owner, where = pod.owner, pod.where
        if owner["kind"] in ("Deployment", "StatefulSet"):
            selector = owner["spec"]["selector"]["matchLabels"]
            assert set(selector) == SELECTOR_LABELS, where
            assert selector["app.kubernetes.io/instance"] == RELEASE, where
            assert selector.items() <= pod.metadata["labels"].items(), f"{where} selects its own pods"
        if owner["kind"] == "StatefulSet":
            service = rendered.one("Service", owner["spec"]["serviceName"])
            assert service["spec"]["clusterIP"] == "None", f"{where}: its governing Service is headless"
        account = pod.spec.get("serviceAccountName")
        assert account and ("ServiceAccount", account) in names, f"{where}: ServiceAccount {account}"
        for container in containers(pod):
            for entry in container.get("env") or []:
                ref = (entry.get("valueFrom") or {}).get("configMapKeyRef")
                if ref:
                    config = rendered.one("ConfigMap", ref["name"])
                    assert ref["key"] in config["data"], f"{where}: {entry['name']}"
    for service in rendered.of_kind("Service"):
        selector, name = service["spec"]["selector"], service["metadata"]["name"]
        selected = [pod for pod in pods if selector.items() <= pod.metadata["labels"].items()]
        assert len(selected) == 1, f"Service {name} selects the pods of one workload"
        assert set(selector) == SELECTOR_LABELS, name
        ports = {port["name"]: port["containerPort"] for c in containers(selected[0]) for port in c.get("ports") or []}
        for port in service["spec"]["ports"]:
            assert port["targetPort"] in ports, f"Service {name}: port {port['targetPort']}"


def test_names_and_namespaces_follow_the_release_without_collisions(helm) -> None:
    first = helm.template(*DEFAULT, release="alpha", namespace="one")
    second = helm.template(*DEFAULT, release="beta", namespace="one")
    elsewhere = helm.template(*DEFAULT, release="alpha", namespace="two")
    assert not set(first.names()) & set(second.names()), "two releases share no object name"
    assert [(kind, name.replace("alpha", "beta", 1)) for kind, name in first.names()] == second.names()
    assert first.names() == elsewhere.names()
    assert {obj["metadata"]["namespace"] for obj in elsewhere.objects} == {"two"}
    # No namespace is written into the chart: every namespace in a rendering
    # is the release's.
    assert set(re.findall(r"\bnamespace: +(\S+)", elsewhere.text)) <= {"two"}
    # Neither release's selectors pick the other's pods.
    for rendered, other in ((first, second), (second, first)):
        for obj in rendered.objects:
            selector = (obj.get("spec") or {}).get("selector") or {}
            selector = selector.get("matchLabels", selector)
            if selector:
                for pod in pod_templates(other):
                    assert not selector.items() <= pod.metadata["labels"].items(), obj["metadata"]["name"]


def test_release_name_length_limit(helm) -> None:
    assert helm.template(*DEFAULT, release="a" * 40).objects
    message = helm.refusal(*DEFAULT, release="a" * 41)
    assert "is longer than 40 characters" in message


@pytest.mark.parametrize("case", CASES)
def test_images_follow_the_values(helm, case: str) -> None:
    values = case_values(case)
    for pod in pod_templates(helm.template("--values", CASES[case])):
        assert pod.spec.get("imagePullSecrets", []) == values["imagePullSecrets"], pod.where
        for container in containers(pod):
            image = values[CONTAINERS[(pod.component, container["name"])].image]
            assert container["image"] == image_reference(image), f"{pod.where}, container {container['name']}"
            assert container["imagePullPolicy"] == image["pullPolicy"], f"{pod.where}, {container['name']}"


def test_the_default_images_name_the_chart_version_without_digests(helm) -> None:
    values, chart = chart_values(), _chart()
    for key in ("image", "postgresImage"):
        assert values[key]["tag"] == chart["appVersion"], key
        assert values[key]["digest"] == "", key
    for pod in pod_templates(helm.template(*DEFAULT)):
        for container in containers(pod):
            assert container["image"].endswith(":" + chart["appVersion"])


@pytest.mark.parametrize(
    "settings, api, postgresql",
    [
        (["image.digest=sha256:" + "1" * 64, "postgresImage.digest=sha256:" + "2" * 64], "ghcr.io/ccu-ukn/pubdex:0.2.0@sha256:{a}", "ghcr.io/ccu-ukn/pubdex-postgres:0.2.0@sha256:{p}"),
        (["image.digest=", "postgresImage.digest="], "ghcr.io/ccu-ukn/pubdex:0.2.0", "ghcr.io/ccu-ukn/pubdex-postgres:0.2.0"),
        (["image.tag=", "postgresImage.tag=", "image.digest=sha256:" + "1" * 64, "postgresImage.digest=sha256:" + "2" * 64], "ghcr.io/ccu-ukn/pubdex@sha256:{a}", "ghcr.io/ccu-ukn/pubdex-postgres@sha256:{p}"),
        (["image.registry=", "image.repository=pubdex", "image.tag=local", "image.digest="], "pubdex:local",
         "ghcr.io/ccu-ukn/pubdex-postgres:0.2.0"),
    ],
    ids=["tag-and-digest", "tag-only", "digest-only", "no-registry"],
)
def test_image_references_are_built_from_their_parts(helm, settings: list, api: str, postgresql: str) -> None:
    digests = {"a": "1" * 64, "p": "2" * 64}
    args = [arg for setting in settings for arg in ("--set", setting)]
    for pod in pod_templates(helm.template(*DEFAULT, *args)):
        for container in containers(pod):
            expected = postgresql if container["name"] == "postgresql" else api
            assert container["image"] == expected.format(**digests), container["name"]


@pytest.mark.parametrize("case", CASES)
def test_the_probes_check_liveness_readiness_and_the_database(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    for pod in pod_templates(rendered):
        for container in containers(pod):
            if (pod.component, container["name"]) == ("api", "api"):
                ports = {port["name"]: port["containerPort"] for port in container["ports"]}
                assert ports == {"http": 8000}
                for probe, path in (("startupProbe", "/livez"), ("livenessProbe", "/livez"),
                                    ("readinessProbe", "/readyz")):
                    assert container[probe]["httpGet"] == {"path": path, "port": "http"}, probe
                # A readiness check waits up to dbWaitSeconds for a connection.
                assert container["readinessProbe"]["timeoutSeconds"] == values["api"]["dbWaitSeconds"] + 2
            if (pod.component, container["name"]) == ("postgresql", "postgresql"):
                for probe in ("startupProbe", "readinessProbe"):
                    command = " ".join(container[probe]["exec"]["command"])
                    assert "pg_isready" in command and "-h 127.0.0.1" in command, probe
                    assert "'app_readonly', 'app_writer'" in command and "SCRAM-SHA-256" in command, probe
                    assert "grep -qx 2" in command, f"{probe}: both roles have SCRAM passwords"
                assert container["livenessProbe"]["exec"]["command"][0] == "pg_isready"
                assert container["startupProbe"]["failureThreshold"] * container["startupProbe"]["periodSeconds"] >= 300


@pytest.mark.parametrize("case", CASES)
def test_the_settings_follow_the_values(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    data = rendered.one("ConfigMap", f"{RELEASE}-config")["data"]
    database = values["database"]["name"]
    if is_external(values):
        server = values["externalDatabase"]
        host, port = server["host"], str(server["port"])
        tls = {"PGSSLMODE": server["sslmode"]}
        ca = ca_source(values)
        if ca:
            tls["PGSSLROOTCERT"] = f"{CA_DIR}/{ca[2]}"
    else:
        host, port, tls = f"{RELEASE}-postgresql", "5432", {}
    # The read service: a libpq string; the ingestion role: libpq's variables.
    tls_options = "".join(f" {option}={tls[variable]}" for option, variable in
                          (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT")) if variable in tls)
    assert data["PEOPLE_PUBS_API_DSN"] == (
        f"host={host} port={port} dbname={database} user=app_readonly{tls_options}")
    libpq = {key: value for key, value in data.items() if key.startswith("PG")}
    assert libpq == {"PGHOST": host, "PGPORT": port, "PGDATABASE": database, "PGUSER": "app_writer", **tls}
    assert data["PEOPLE_PUBS_API_LOG_LEVEL"] == values["api"]["logLevel"]
    assert data["PEOPLE_PUBS_API_POOL_SIZE"] == str(values["api"]["poolSize"])
    assert data["PEOPLE_PUBS_API_DB_WAIT_SECONDS"] == str(values["api"]["dbWaitSeconds"])
    assert data["PEOPLE_PUBS_API_STATEMENT_TIMEOUT_MS"] == str(values["api"]["statementTimeoutMs"])
    assert data["PEOPLE_PUBS_CROSSREF_MAILTO"] == values["ingestion"]["crossrefMailto"]
    assert data["PEOPLE_PUBS_ORG_PATTERNS"] == values["ingestion"]["orgPatterns"]
    servers = [c for pod in pod_templates(rendered) for c in containers(pod) if c["name"] == "postgresql"]
    if is_external(values):
        assert servers == []
    else:
        (server,) = servers
        assert env_value(server, "POSTGRES_DB")["value"] == database
    service = rendered.one("Service", f"{RELEASE}-api")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == values["api"]["service"]["port"]
    assert rendered.one("Deployment", f"{RELEASE}-api")["spec"]["replicas"] == values["api"]["replicas"]


def _checksums(rendered) -> dict:
    """The settings checksum of each long-running pod that reads the
    release's ConfigMap, which every such pod has to carry. A Job's pod reads
    the settings when it starts, so it needs none."""
    sums = {}
    for pod in pod_templates(rendered):
        reads = any((entry.get("valueFrom") or {}).get("configMapKeyRef", {}).get("name") == f"{RELEASE}-config"
                    for container in containers(pod) for entry in container.get("env") or [])
        if reads and pod.owner["kind"] in ("Deployment", "StatefulSet"):
            annotation = (pod.metadata.get("annotations") or {}).get("checksum/config")
            assert annotation and re.fullmatch(r"[0-9a-f]{64}", annotation), pod.where
            sums[pod.where] = annotation
    return sums


def test_a_changed_setting_changes_the_checksum_that_rolls_the_pods(helm) -> None:
    default = _checksums(helm.template(*DEFAULT))
    assert default, "the read service reads the settings"
    assert _checksums(helm.template(*DEFAULT, "--set", "api.replicas=1")) == default, "the same settings, the same sum"
    assert _checksums(helm.template(*DEFAULT, "--set", "api.replicas=3")) == default, "replicas are no setting"
    for setting in ("api.logLevel=DEBUG", "api.poolSize=5", "ingestion.orgPatterns=Example",
                    "database.name=other_db"):
        changed = _checksums(helm.template(*DEFAULT, "--set", setting))
        assert set(changed) == set(default)
        assert all(changed[pod] != default[pod] for pod in default), setting


@pytest.mark.parametrize("case", CASES)
def test_the_notes_render_with_the_release_its_access_and_the_exposure_warning(helm, case: str) -> None:
    values = case_values(case)
    notes = helm.notes("--values", CASES[case])
    assert "<no value>" not in notes and "%!" not in notes
    assert f"release {RELEASE} in namespace {NAMESPACE}" in notes
    assert f"kubectl --namespace {NAMESPACE} port-forward service/{RELEASE}-api 8000:{values['api']['service']['port']}" \
        in notes
    assert "no authentication and no TLS" in notes
    assert f"rollout status deployment/{RELEASE}-api" in notes
    if is_external(values):
        server = values["externalDatabase"]
        assert (f"uses database {values['database']['name']} on the PostgreSQL server "
                f"{server['host']}:{server['port']}, with sslmode {server['sslmode']}") in notes
        assert "postgresql" not in notes.replace("PostgreSQL", ""), "no bundled server, volume or StatefulSet"
        assert "helm uninstall leaves the database" in notes
        if server["setup"]["enabled"]:
            assert f"The setup Job {RELEASE}-setup prepared it as {server['setup']['adminUser']}" in notes
            assert f"delete secret {server['setup']['adminSecret']['name']}" in notes
        else:
            assert "Run files/external-setup.sh of this chart" in notes
        return
    assert f"rollout status statefulset/{RELEASE}-postgresql" in notes
    kept = values["postgresql"]["persistence"]["retainOnUninstall"]
    assert (f"delete pvc data-{RELEASE}-postgresql-0" in notes) is kept
    assert ("is deleted with the release" in notes) is not kept
