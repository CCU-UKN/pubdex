"""NetworkPolicy and the Ingress.

- NetworkPolicy is on by default. PostgreSQL admits TCP 5432 only from the
  release's own pods that connect to the database, the read service TCP
  8000 only from the release's client label and the configured peers; the
  selectors match the release's labels and no other release's pods. Only
  ingress is restricted: what the pods connect to is not.
- The Ingress is off by default. Enabled, it leads every configured host
  and path to the read service's Service, with the configured TLS, class
  and annotations, and the values, the examples and NOTES.txt warn that it
  publishes a service without authentication.
"""
from __future__ import annotations

import pytest
import yaml

from chart import (
    CASES, EXAMPLES_DIR, RELEASE, case_values, containers, env_value, is_external, pod_templates, values_section,
)

DEFAULT = ("--values", CASES["values/default"])
EXPOSURE_WARNING = "WARNING: the read service has no authentication."


def _selects(selector: dict, labels: dict) -> bool:
    """Whether a Kubernetes label selector selects these labels."""
    if not set((selector.get("matchLabels") or {}).items()) <= set(labels.items()):
        return False
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression["key"], expression["operator"], expression.get("values") or []
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and key in labels and labels[key] in values:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
    return True


def _connects_to_the_database(pod) -> bool:
    """Whether a container of the pod opens a database connection: through
    the read service's connection string or libpq's variables."""
    return any(env_value(container, name) for container in containers(pod)
               for name in ("PEOPLE_PUBS_API_DSN", "PGHOST"))


def _policies(rendered) -> dict:
    return {obj["metadata"]["name"]: obj for obj in rendered.of_kind("NetworkPolicy")}


@pytest.mark.parametrize("case", CASES)
def test_network_policies_follow_the_values_and_restrict_ingress_only(helm, case: str) -> None:
    values = case_values(case)
    policies = _policies(helm.template("--values", CASES[case]))
    if not values["networkPolicy"]["enabled"]:
        assert policies == {}, "no NetworkPolicy without networkPolicy.enabled"
        return
    expected = {f"{RELEASE}-api"} | (set() if is_external(values) else {f"{RELEASE}-postgresql"})
    assert set(policies) == expected
    for name, policy in policies.items():
        # Egress stays open: ingestion reaches DNS, the metadata providers
        # and, in external mode, the server.
        assert policy["spec"]["policyTypes"] == ["Ingress"], name
        assert "egress" not in policy["spec"], name
        assert len(policy["spec"]["ingress"]) == 1, name


def test_network_policies_are_on_by_default(helm) -> None:
    assert case_values("values/default")["networkPolicy"] == {"enabled": True, "api": {"allowFrom": []}}
    assert set(_policies(helm.template(*DEFAULT))) == {f"{RELEASE}-api", f"{RELEASE}-postgresql"}


@pytest.mark.parametrize("case", CASES)
def test_postgresql_admits_only_the_release_pods_that_use_the_database(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    policy = _policies(rendered).get(f"{RELEASE}-postgresql")
    if is_external(values) or not values["networkPolicy"]["enabled"]:
        assert policy is None
        return
    pods = pod_templates(rendered)
    selected = [pod for pod in pods if _selects(policy["spec"]["podSelector"], pod.metadata["labels"])]
    assert [pod.component for pod in selected] == ["postgresql"], "the policy selects the PostgreSQL pod alone"
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]
    for peer in rule["from"]:
        assert set(peer) == {"podSelector"}, "pods of the release's namespace only"
    admitted = {pod.where for pod in pods if any(_selects(peer["podSelector"], pod.metadata["labels"])
                                                 for peer in rule["from"])}
    clients = {pod.where for pod in pods if _connects_to_the_database(pod)}
    assert admitted == clients, "exactly the pods that connect to the database"
    assert {pod.component for pod in pods if pod.where in admitted} >= {"api", "ingest"}


@pytest.mark.parametrize("case", CASES)
def test_the_read_service_admits_its_client_label_and_the_configured_peers(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    policy = _policies(rendered).get(f"{RELEASE}-api")
    if not values["networkPolicy"]["enabled"]:
        assert policy is None
        return
    pods = pod_templates(rendered)
    selected = [pod for pod in pods if _selects(policy["spec"]["podSelector"], pod.metadata["labels"])]
    assert {pod.component for pod in selected} == {"api"} and len(selected) == 1
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8000}]
    client, *peers = rule["from"]
    assert client == {"podSelector": {"matchLabels": {f"{RELEASE}-client": "true"}}}
    assert peers == values["networkPolicy"]["api"]["allowFrom"]
    # None of the chart's own pods is a client but helm test's: the label is
    # the operator's.
    for pod in pods:
        if pod.component != "test":
            assert f"{RELEASE}-client" not in pod.metadata["labels"], pod.where


@pytest.mark.parametrize("case", CASES)
def test_helm_test_asks_the_read_service_as_a_client(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    (pod,) = [pod for pod in pod_templates(rendered) if pod.component == "test"]
    assert pod.owner["kind"] == "Pod" and pod.owner["metadata"]["name"] == f"{RELEASE}-test"
    annotations = pod.metadata["annotations"]
    assert annotations["helm.sh/hook"] == "test"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    assert pod.metadata["labels"][f"{RELEASE}-client"] == "true", "admitted by the read service's NetworkPolicy"
    assert pod.spec["restartPolicy"] == "Never" and not _connects_to_the_database(pod)
    (container,) = containers(pod)
    *_, script, url = container["command"]
    assert container["command"][:3] == ["python", "-I", "-c"]
    assert url == f"http://{RELEASE}-api:{values['api']['service']['port']}"
    assert '"/readyz", "/v0/publications?limit=1"' in script
    assert not [obj for obj in rendered.objects if obj["kind"] == "Pod" and obj is not pod.owner], \
        "helm test's pod is the only bare pod"


def test_the_selectors_match_this_release_and_no_other(helm) -> None:
    first = helm.template(*DEFAULT, release="alpha", namespace="one")
    second = helm.template(*DEFAULT, release="beta", namespace="one")
    for rendered, other, name in ((first, second, "alpha"), (second, first, "beta")):
        own_pods = pod_templates(rendered)
        for policy in rendered.of_kind("NetworkPolicy"):
            spec = policy["spec"]
            assert spec["podSelector"]["matchLabels"]["app.kubernetes.io/instance"] == name
            for pod in pod_templates(other):
                assert not _selects(spec["podSelector"], pod.metadata["labels"]), policy["metadata"]["name"]
                for peer in spec["ingress"][0]["from"]:
                    if "podSelector" in peer:
                        assert not _selects(peer["podSelector"], pod.metadata["labels"]), \
                            f"{policy['metadata']['name']} admits {pod.where} of the other release"
            assert any(_selects(spec["podSelector"], pod.metadata["labels"]) for pod in own_pods)


def test_the_policies_select_the_release_pods_whatever_labels_the_values_add(helm) -> None:
    # Labels of every pod, and others of the read service's and PostgreSQL's
    # own, including keys that resemble the chart's selector labels.
    added = ["--set-json", 'podLabels={"example.org/team":"curation","component":"api","instance":"other"}',
             "--set-json", 'api.podLabels={"example.org/tier":"web","app":"pubdex"}',
             "--set-json", 'postgresql.podLabels={"example.org/tier":"database","component":"postgresql"}',
             "--set", "demo.enabled=true",
             "--set-json", 'ingestion.tasks={"x":{"command":["python","-m","people_pubs.sync.orcid_works"]}}']
    first = helm.template(*DEFAULT, *added, release="alpha", namespace="one")
    second = helm.template(*DEFAULT, *added, release="beta", namespace="one")
    for rendered, other, name in ((first, second, "alpha"), (second, first, "beta")):
        pods, policies = pod_templates(rendered), _policies(rendered)
        assert any(pod.metadata["labels"].get("example.org/team") == "curation" for pod in pods)
        database = policies[f"{name}-postgresql"]["spec"]
        assert [pod.component for pod in pods if _selects(database["podSelector"], pod.metadata["labels"])] == \
            ["postgresql"]
        admitted = {pod.where for pod in pods for peer in database["ingress"][0]["from"]
                    if _selects(peer["podSelector"], pod.metadata["labels"])}
        assert admitted == {pod.where for pod in pods if _connects_to_the_database(pod)}
        api = policies[f"{name}-api"]["spec"]
        assert [pod.component for pod in pods if _selects(api["podSelector"], pod.metadata["labels"])] == ["api"]
        for pod in pod_templates(other):
            for policy in policies.values():
                assert not _selects(policy["spec"]["podSelector"], pod.metadata["labels"]), pod.where
                for peer in policy["spec"]["ingress"][0]["from"]:
                    if "podSelector" in peer:
                        assert not _selects(peer["podSelector"], pod.metadata["labels"]), pod.where


def test_the_documentation_says_that_enforcement_depends_on_the_network_plugin(helm) -> None:
    section = values_section("networkPolicy")
    assert "Only a cluster whose network plugin enforces NetworkPolicy applies them" in section
    notes = helm.notes(*DEFAULT)
    assert "provided the cluster's network plugin\nenforces NetworkPolicy" in notes
    assert f"label pod <pod> {RELEASE}-client=true" in notes
    off = helm.notes(*DEFAULT, "--set", "networkPolicy.enabled=false")
    assert "networkPolicy.enabled is false: every pod of the cluster can reach the read\nservice and the database." in off


@pytest.mark.parametrize("case", CASES)
def test_the_ingress_follows_the_values_and_leads_to_the_read_service(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    ingresses = rendered.of_kind("Ingress")
    settings = values["ingress"]
    if not settings["enabled"]:
        assert ingresses == [], "no Ingress unless ingress.enabled"
        return
    (ingress,) = ingresses
    assert ingress["metadata"]["name"] == f"{RELEASE}-api"
    assert ingress["metadata"].get("annotations", {}) == settings["annotations"]
    spec = ingress["spec"]
    assert spec.get("ingressClassName", "") == settings["className"]
    assert spec.get("tls", []) == settings["tls"]
    service = rendered.one("Service", f"{RELEASE}-api")
    port = service["spec"]["ports"][0]["port"]
    expected = []
    for host in settings["hosts"]:
        paths = host.get("paths") or [{"path": "/", "pathType": "Prefix"}]
        expected.append({"host": host["host"], "http": {"paths": [
            {"path": path["path"], "pathType": path["pathType"],
             "backend": {"service": {"name": f"{RELEASE}-api", "port": {"number": port}}}} for path in paths]}})
    assert spec["rules"] == expected


def test_the_ingress_is_off_by_default_and_warned_against_everywhere(helm) -> None:
    assert case_values("values/default")["ingress"]["enabled"] is False
    assert helm.template(*DEFAULT, "--set", "ingress.hosts[0].host=pubdex.example.org").of_kind("Ingress") == []
    assert EXPOSURE_WARNING in values_section("ingress")
    examples = [path for path in sorted(EXAMPLES_DIR.glob("*.yaml"))
                if (yaml.safe_load(path.read_text(encoding="utf-8")).get("ingress") or {}).get("enabled")]
    assert len(examples) >= 2, "an example with a TLS Secret and one with a certificate manager"
    for path in examples:
        text = path.read_text(encoding="utf-8")
        values = yaml.safe_load(text)
        assert EXPOSURE_WARNING in " ".join(line.lstrip("# ") for line in text.splitlines()), path.name
        hosts = {host["host"] for host in values["ingress"]["hosts"]}
        assert all(host.endswith(".example.org") for host in hosts), path.name
        covered = {host for tls in values["ingress"]["tls"] for host in tls["hosts"]}
        assert hosts <= covered, f"{path.name}: TLS for every host"
        assert values["networkPolicy"]["api"]["allowFrom"], f"{path.name}: the controller's pods are admitted"
    assert any("cert-manager.io/cluster-issuer" in (yaml.safe_load(path.read_text(encoding="utf-8"))["ingress"]
                                                    .get("annotations") or {}) for path in examples)
    notes = helm.notes(*DEFAULT, "--set", "ingress.enabled=true", "--set", "ingress.hosts[0].host=pubdex.example.org")
    assert "WARNING: Ingress checks-api publishes the read service, which has no\nauthentication" in notes
    assert "  http://pubdex.example.org/\n" in notes
    assert "No TLS is configured (ingress.tls): these hosts are served by plain HTTP." in notes
    assert "networkPolicy.api.allowFrom is empty, so NetworkPolicy checks-api does not admit" in notes
    assert "WARNING: Ingress" not in helm.notes(*DEFAULT)


@pytest.mark.parametrize("case", [case for case in CASES if case_values(case)["ingress"]["enabled"]])
def test_the_notes_list_each_published_address(helm, case: str) -> None:
    values = case_values(case)
    notes = helm.notes("--values", CASES[case])
    covered = {host for tls in values["ingress"]["tls"] for host in tls["hosts"]}
    for host in values["ingress"]["hosts"]:
        scheme = "https" if host["host"] in covered else "http"
        for path in host.get("paths") or [{"path": "/"}]:
            assert f"\n  {scheme}://{host['host']}{path['path']}\n" in notes
    assert ("No TLS is configured" in notes) is (not values["ingress"]["tls"])
