"""The pods' scheduling and metadata values: nodeSelector, tolerations,
affinity, podAnnotations, podLabels and priorityClassName.

- Set at the top level, each reaches every pod the chart makes Kubernetes
  run: the read service, PostgreSQL with its init container, the setup Job,
  the schema upgrade's Job, the ingestion CronJobs' runs, the
  demonstration, helm test's pod and the attached one-off pod of the
  ConfigMap <release>-run.
- api and postgresql may set each key for their own pods. Their value
  replaces the top-level one, never merged with it, even when it is {}, []
  or ""; left out or null, the top-level one applies.
- podLabels and podAnnotations cannot set what the chart sets on its pods
  itself; such values fail with the key.

values.schema.json's refusals of malformed values are in
test_render_values.py, the NetworkPolicies' selection with added labels in
test_render_network.py, and Pod Security "restricted" with every value set
in test_render_security.py.
"""
from __future__ import annotations

import json
from typing import Dict, List

import pytest

from chart import (
    CASES, POD_KEYS, RELEASE, PodTemplate, attached_pod, case_values, pod_templates, pod_values,
)

DEFAULT = ("--values", CASES["values/default"])
TASK = ("--set-json", 'ingestion.tasks={"x":{"command":["python","-m","people_pubs.sync.orcid_works"]}}')
# Each mode with every pod it has: evaluation mode with the demonstration,
# external mode with the setup Job; both with an ingestion task.
EVALUATION = (*DEFAULT, "--set", "demo.enabled=true", *TASK)
EXTERNAL = (*DEFAULT, "--set", "mode=external", "--set", "externalDatabase.host=db.example.org",
            "--set", "externalDatabase.sslmode=require", "--set", "externalDatabase.setup.adminUser=dbadmin",
            "--set", "externalDatabase.setup.adminSecret.name=pubdex-db-admin", *TASK)
# The pods each mode renders, as (kind of the declaring object, component).
EVALUATION_PODS = {("Deployment", "api"), ("StatefulSet", "postgresql"), ("CronJob", "ingest"), ("Job", "demo"),
                   ("Job", "schema-upgrade"), ("Pod", "test"), ("ConfigMap", "ingest")}
EXTERNAL_PODS = {("Deployment", "api"), ("Job", "setup"), ("Job", "schema-upgrade"), ("CronJob", "ingest"),
                 ("Pod", "test"), ("ConfigMap", "ingest")}

# A value of each key, as an operator sets it at the top level ...
TOP = {
    "nodeSelector": {"example.org/pool": "pubdex"},
    "tolerations": [{"key": "example.org/dedicated", "operator": "Equal", "value": "pubdex", "effect": "NoSchedule"}],
    "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
        {"matchExpressions": [{"key": "example.org/zone", "operator": "In", "values": ["a"]}]}]}}},
    "podAnnotations": {"example.org/owner": "curation"},
    "podLabels": {"example.org/team": "curation"},
    "priorityClassName": "pubdex-normal",
}
# ... another, with keys of its own, so that a merge with TOP would show ...
OTHER = {
    "nodeSelector": {"example.org/disk": "ssd"},
    "tolerations": [{"key": "example.org/database", "operator": "Exists", "effect": "NoSchedule"}],
    "affinity": {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
        {"weight": 1, "podAffinityTerm": {"topologyKey": "kubernetes.io/hostname"}}]}},
    "podAnnotations": {"example.org/scrape": "true"},
    "podLabels": {"example.org/tier": "database"},
    "priorityClassName": "pubdex-database",
}
# ... and none.
EMPTY = {"nodeSelector": {}, "tolerations": [], "affinity": {}, "podAnnotations": {}, "podLabels": {},
         "priorityClassName": ""}

# What the chart sets on its pods itself, which the values cannot set.
CHART_LABELS = ("helm.sh/chart", "app.kubernetes.io/name", "app.kubernetes.io/instance",
                "app.kubernetes.io/component", "app.kubernetes.io/version", "app.kubernetes.io/part-of",
                "app.kubernetes.io/managed-by", f"{RELEASE}-client")
CHART_ANNOTATIONS = ("checksum/config", "helm.sh/hook", "helm.sh/hook-weight", "helm.sh/hook-delete-policy")
# The label kubectl run gives the attached pod.
KUBECTL_RUN_LABEL = "run"


def _set(values: Dict[str, object], prefix: str = "") -> List[str]:
    """helm arguments that set these values, under prefix."""
    return [arg for key, value in values.items() for arg in ("--set-json", f"{prefix}{key}={json.dumps(value)}")]


def _applied(pod: PodTemplate) -> Dict[str, object]:
    """The six values as a pod carries them: the pod specification's fields,
    and the labels and annotations the chart does not set itself."""
    labels = pod.metadata.get("labels") or {}
    annotations = pod.metadata.get("annotations") or {}
    return {
        "nodeSelector": pod.spec.get("nodeSelector", {}),
        "tolerations": pod.spec.get("tolerations", []),
        "affinity": pod.spec.get("affinity", {}),
        "podAnnotations": {key: value for key, value in annotations.items() if key not in CHART_ANNOTATIONS},
        "podLabels": {key: value for key, value in labels.items()
                      if key not in CHART_LABELS and key != KUBECTL_RUN_LABEL},
        "priorityClassName": pod.spec.get("priorityClassName", ""),
    }


def _kinds(pods: List[PodTemplate]) -> set:
    return {(pod.owner["kind"], pod.component) for pod in pods}


@pytest.mark.parametrize("case", CASES)
def test_each_pod_carries_the_values_of_its_component(helm, case: str) -> None:
    values = case_values(case)
    for pod in pod_templates(helm.template("--values", CASES[case])):
        assert _applied(pod) == pod_values(values, pod.component), pod.where


@pytest.mark.parametrize("settings, kinds", [(EVALUATION, EVALUATION_PODS), (EXTERNAL, EXTERNAL_PODS)],
                         ids=["evaluation", "external"])
def test_the_top_level_values_reach_every_pod(helm, settings: tuple, kinds: set) -> None:
    pods = pod_templates(helm.template(*settings, *_set(TOP)))
    assert _kinds(pods) == kinds
    for pod in pods:
        assert _applied(pod) == TOP, pod.where
    # PostgreSQL's init container runs in the same pod, so with the same
    # placement and priority.
    for pod in pods:
        if pod.component in ("postgresql", "setup"):
            assert [container["name"] for container in pod.spec["initContainers"]] == ["bootstrap"], pod.where


def test_the_attached_pod_gets_them_through_its_patch(helm) -> None:
    rendered = helm.template(*EVALUATION, *_set(TOP))
    patch = json.loads(rendered.one("ConfigMap", f"{RELEASE}-run")["data"]["overrides"])
    paths = {operation["path"] for operation in patch}
    assert {"/spec/nodeSelector", "/spec/tolerations", "/spec/affinity", "/spec/priorityClassName",
            "/metadata/annotations", "/metadata/labels/example.org~1team"} <= paths
    # kubectl run's own label stays.
    pod = attached_pod(rendered.one("ConfigMap", f"{RELEASE}-run"))
    assert pod["metadata"]["labels"][KUBECTL_RUN_LABEL] == f"{RELEASE}-run"
    # Without the values the patch sets none of these fields.
    plain = json.loads(helm.template(*EVALUATION).one("ConfigMap", f"{RELEASE}-run")["data"]["overrides"])
    assert not {operation["path"] for operation in plain} & {"/spec/nodeSelector", "/spec/tolerations",
                                                              "/spec/affinity", "/spec/priorityClassName",
                                                              "/metadata/annotations"}


@pytest.mark.parametrize("override", ["other", "empty", "null"])
@pytest.mark.parametrize("key", POD_KEYS)
@pytest.mark.parametrize("component", ["api", "postgresql"])
def test_an_override_replaces_the_top_level_value_and_null_inherits_it(
        helm, component: str, key: str, override: str) -> None:
    given = {"other": OTHER[key], "empty": EMPTY[key], "null": None}[override]
    pods = pod_templates(helm.template(*EVALUATION, *_set(TOP), *_set({key: given}, f"{component}.")))
    assert _kinds(pods) == EVALUATION_PODS
    for pod in pods:
        applied = _applied(pod)
        wanted = given if pod.component == component and given is not None else TOP[key]
        assert applied[key] == wanted, f"{pod.where}: {key}"
        # The other keys keep the top-level values.
        assert {other: applied[other] for other in POD_KEYS if other != key} == \
            {other: TOP[other] for other in POD_KEYS if other != key}, pod.where


def test_a_null_in_a_values_file_inherits_as_well(helm, tmp_path) -> None:
    values = tmp_path / "overrides.yaml"
    values.write_text("".join(f"{component}:\n" + "".join(f"  {key}: null\n" for key in POD_KEYS)
                              for component in ("api", "postgresql")), encoding="utf-8")
    pods = pod_templates(helm.template(*EVALUATION, *_set(TOP), "--values", values))
    for pod in pods:
        assert _applied(pod) == TOP, pod.where


def test_the_chart_sets_exactly_the_labels_and_annotations_it_keeps_for_itself(helm) -> None:
    # CHART_LABELS and CHART_ANNOTATIONS are what the chart's pods carry
    # without any value, so the refusals below cover all of it.
    pods = pod_templates(helm.template(*EVALUATION)) + pod_templates(helm.template(*EXTERNAL))
    labels = {key for pod in pods for key in pod.metadata.get("labels") or {}} - {KUBECTL_RUN_LABEL}
    annotations = {key for pod in pods for key in pod.metadata.get("annotations") or {}}
    assert labels == set(CHART_LABELS)
    assert annotations <= set(CHART_ANNOTATIONS)


@pytest.mark.parametrize("label", CHART_LABELS)
@pytest.mark.parametrize("prefix", ["", "api.", "postgresql."], ids=["top-level", "api", "postgresql"])
def test_a_label_the_chart_sets_itself_is_refused(helm, prefix: str, label: str) -> None:
    refusal = helm.refusal(*EVALUATION, "--set-json", f"{prefix}podLabels={json.dumps({label: 'x'})}")
    assert f"{prefix}podLabels sets {label}, a label the chart sets on the release's pods itself" in refusal


@pytest.mark.parametrize("annotation", CHART_ANNOTATIONS)
@pytest.mark.parametrize("prefix", ["", "api.", "postgresql."], ids=["top-level", "api", "postgresql"])
def test_an_annotation_the_chart_sets_itself_is_refused(helm, prefix: str, annotation: str) -> None:
    refusal = helm.refusal(*EVALUATION, "--set-json", f"{prefix}podAnnotations={json.dumps({annotation: 'x'})}")
    assert f"{prefix}podAnnotations sets {annotation}, an annotation the chart sets on the release's pods itself" \
        in refusal


def test_the_refusals_apply_in_external_mode_as_well(helm) -> None:
    refusal = helm.refusal(*EXTERNAL, "--set-json", 'podLabels={"app.kubernetes.io/component":"x"}')
    assert "podLabels sets app.kubernetes.io/component" in refusal
