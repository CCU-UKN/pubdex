"""The schema upgrade: python -m people_pubs.schema upgrade, the command of
compose.yaml's schema-upgrade service, as a Job in both modes.

- Evaluation mode: a resource of each revision, <release>-schema-upgrade-
  <revision>, not a hook, as postgres with that role's key of the operator's
  Secret alone, against the release's own PostgreSQL, retried once.
- External mode: a pre-install,pre-upgrade hook <release>-schema-upgrade
  after the setup Job, as the admin role with the admin Secret's key alone
  and the setup's TLS settings, not retried. It is rendered exactly when the
  setup is: a routine upgrade, with externalDatabase.setup.enabled=false,
  renders neither and needs no admin Secret.

Pod Security "restricted", the read-only root and the credentials of its
container are checked with every other pod's in test_render_security.py
(chart.CONTAINERS lists it), its scheduling values in
test_render_scheduling.py, and PostgreSQL's NetworkPolicy, which admits it,
in test_render_network.py. Whether it brings a database to the schema of
its image is the runner's, which the integration tests of stage 5 run, and
what Helm does with it on a cluster is stage 9's.
"""
from __future__ import annotations

from typing import List

import pytest
import yaml

from chart import (
    CA_DIR, CASES, CHART, COMPOSE, NAMESPACE, RELEASE, ca_source, case_values, containers, image_reference,
    is_external, is_hook, mounted_secret_keys, pod_templates, setup_enabled, values_section,
)

COMMAND = ["python", "-m", "people_pubs.schema", "upgrade", "--wait", "300"]
HOOK_EVENTS = "pre-install,pre-upgrade"
HOOK_DELETE_POLICY = "before-hook-creation,hook-succeeded"
NAME = f"{RELEASE}-schema-upgrade"
# helm template renders revision 1.
EVALUATION_JOB = f"{NAME}-1"

EXTERNAL = ("--values", CASES["values/default"], "--set", "mode=external",
            "--set", "externalDatabase.host=db.example.org", "--set", "externalDatabase.sslmode=require",
            "--set", "externalDatabase.setup.adminUser=dbadmin",
            "--set", "externalDatabase.setup.adminSecret.name=pubdex-db-admin")
# A routine upgrade gives the release's values and switches the setup off;
# no admin role or Secret need be named.
ROUTINE = ("--values", CASES["values/default"], "--set", "mode=external",
           "--set", "externalDatabase.host=db.example.org", "--set", "externalDatabase.sslmode=require",
           "--set", "externalDatabase.setup.enabled=false")


def _objects(rendered) -> List[dict]:
    """Every object of the schema upgrade, by its component label."""
    return [obj for obj in rendered.objects
            if (obj["metadata"].get("labels") or {}).get("app.kubernetes.io/component") == "schema-upgrade"]


def _env(container: dict) -> dict:
    return {entry["name"]: entry["value"] for entry in container["env"]}


@pytest.mark.parametrize("case", CASES)
def test_each_mode_renders_its_schema_upgrade(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    found = sorted((obj["kind"], obj["metadata"]["name"], is_hook(obj)) for obj in _objects(rendered))
    if not is_external(values):
        assert found == [("Job", EVALUATION_JOB, False), ("ServiceAccount", NAME, False)]
    elif setup_enabled(values):
        assert found == [("Job", NAME, True), ("ServiceAccount", NAME, True)]
    else:
        assert found == [], "no setup, no schema upgrade"
    for pod in pod_templates(rendered):
        if pod.component == "schema-upgrade":
            assert pod.owner["kind"] == "Job", pod.where


@pytest.mark.parametrize("case", [case for case in CASES if not is_external(case_values(case))])
def test_evaluation_mode_runs_it_as_postgres_with_each_revision(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    job = rendered.one("Job", EVALUATION_JOB)
    assert not is_hook(job), "a resource of the revision: before helm install there is no database"
    # Retried once, for an upgrade that also replaces PostgreSQL's pod.
    assert job["spec"]["backoffLimit"] == 1
    assert 0 < job["spec"]["activeDeadlineSeconds"] <= 3600
    (pod,) = [pod for pod in pod_templates(rendered) if pod.owner is job]
    assert pod.spec["restartPolicy"] == "Never" and pod.spec["automountServiceAccountToken"] is False
    assert pod.spec["serviceAccountName"] == NAME
    account = rendered.one("ServiceAccount", NAME)
    assert account["automountServiceAccountToken"] is False and not is_hook(account)
    context = pod.spec["securityContext"]
    assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (10001, 10001, 10001)
    assert not pod.spec.get("initContainers")
    (container,) = pod.spec["containers"]
    assert container["name"] == "schema-upgrade"
    assert container["image"] == image_reference(values["image"])
    assert container["imagePullPolicy"] == values["image"]["pullPolicy"]
    assert container["command"] == COMMAND
    assert _env(container) == {
        "PGHOST": f"{RELEASE}-postgresql", "PGPORT": "5432", "PGDATABASE": values["database"]["name"],
        "PGUSER": "postgres", "PEOPLE_DB_PASSWORD_FILE": "/run/secrets/pubdex/postgres_password",
    }
    assert mounted_secret_keys(pod, container) == {
        "/run/secrets/pubdex/postgres_password": (values["secrets"]["name"], "postgres_password")}
    assert container.get("resources") == (values["schemaUpgrade"]["resources"] or None)


@pytest.mark.parametrize("case", [case for case in CASES if setup_enabled(case_values(case))])
def test_external_mode_runs_it_as_the_admin_role_after_the_setup(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    job = rendered.one("Job", NAME)
    account = rendered.one("ServiceAccount", NAME)
    setup = rendered.one("Job", f"{RELEASE}-setup")
    weight = {}
    for obj in (job, account, setup):
        annotations = obj["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == HOOK_EVENTS, obj["kind"]
        assert annotations["helm.sh/hook-delete-policy"] == HOOK_DELETE_POLICY, obj["kind"]
        weight[(obj["kind"], obj["metadata"]["name"])] = int(annotations["helm.sh/hook-weight"])
    # After the setup has applied the baseline and the passwords; its
    # account exists before it starts.
    assert weight[("Job", NAME)] > weight[("Job", f"{RELEASE}-setup")]
    assert weight[("ServiceAccount", NAME)] < weight[("Job", NAME)]
    assert job["spec"]["backoffLimit"] == 0
    assert 0 < job["spec"]["activeDeadlineSeconds"] <= 3600
    (pod,) = [pod for pod in pod_templates(rendered) if pod.owner is job]
    assert pod.spec["serviceAccountName"] == NAME and pod.spec["automountServiceAccountToken"] is False
    context = pod.spec["securityContext"]
    assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (10001, 10001, 10001)
    (container,) = pod.spec["containers"]
    assert container["image"] == image_reference(values["image"]) and container["command"] == COMMAND
    server = values["externalDatabase"]
    admin = server["setup"]["adminSecret"]
    expected = {
        "PGHOST": server["host"], "PGPORT": str(server["port"]), "PGDATABASE": values["database"]["name"],
        "PGUSER": server["setup"]["adminUser"], "PGSSLMODE": server["sslmode"],
        "PEOPLE_DB_PASSWORD_FILE": f"/run/secrets/pubdex-admin/{admin['key']}",
    }
    ca = ca_source(values)
    if ca:
        expected["PGSSLROOTCERT"] = f"{CA_DIR}/{ca[2]}"
    assert _env(container) == expected
    # The admin role's password and nothing of the operator's Secret; a
    # missing admin Secret fails the run with the runner's message.
    assert mounted_secret_keys(pod, container) == {
        f"/run/secrets/pubdex-admin/{admin['key']}": (admin["name"], admin["key"])}
    (volume,) = [volume for volume in pod.spec["volumes"] if "secret" in volume]
    assert volume["secret"]["optional"] is True
    assert container.get("resources") == (values["schemaUpgrade"]["resources"] or None)


def test_a_routine_upgrade_renders_no_schema_upgrade_and_names_no_admin_secret(helm) -> None:
    rendered = helm.template(*ROUTINE)
    assert _objects(rendered) == [] and not [obj for obj in rendered.objects if is_hook(obj)
                                             and obj["metadata"]["name"] != f"{RELEASE}-test"]
    assert "pubdex-admin" not in rendered.text and "people_pubs.schema" not in rendered.text
    # The same values with the setup render both hooks, the setup's first.
    hooks = [(obj["kind"], obj["metadata"]["name"]) for obj in helm.template(*EXTERNAL).objects
             if is_hook(obj) and obj["kind"] == "Job"]
    assert hooks == [("Job", f"{RELEASE}-setup"), ("Job", NAME)]


def test_both_modes_run_the_command_of_the_compose_service(helm) -> None:
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["schema-upgrade"]
    assert service["command"] == COMMAND[:4]
    for arguments in (("--values", CASES["values/default"]), EXTERNAL):
        (container,) = [container for pod in pod_templates(helm.template(*arguments))
                        if pod.component == "schema-upgrade" for container in containers(pod)]
        assert container["command"] == COMMAND, arguments


def test_the_evaluation_job_is_named_after_the_revision(helm) -> None:
    template = (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    assert '{{- printf "%s-%d" (include "pubdex.schemaUpgradeName" .) (.Release.Revision | int) -}}' in template
    # The longest release name leaves room for seven digits of revision in
    # the 63 characters of the job-name label.
    release = "r" * 40
    rendered = helm.template("--values", CASES["values/default"], release=release)
    (job,) = [obj for obj in _objects(rendered) if obj["kind"] == "Job"]
    assert job["metadata"]["name"] == f"{release}-schema-upgrade-1"
    assert len(job["metadata"]["name"]) - 1 + 7 <= 63


def test_the_values_and_notes_describe_it(helm) -> None:
    section = values_section("schemaUpgrade")
    for phrase in ("<release>-schema-upgrade-<revision>", "<release>-schema-upgrade,", "--wait-for-jobs",
                   "externalDatabase.setup.enabled", "Default: requests of 50m CPU and 128Mi memory"):
        assert phrase in section, phrase
    notes = helm.notes("--values", CASES["values/default"])
    assert f"kubectl --namespace {NAMESPACE} logs job/{EVALUATION_JOB}" in notes
    notes = helm.notes(*EXTERNAL)
    assert f"the schema upgrade Job\n{NAME} then applied the schema changes" in notes
    notes = helm.notes(*ROUTINE)
    assert "python -m\npeople_pubs.schema upgrade, as the same role" in notes
