"""Ingestion, the synthetic demonstration and the roster import.

- Each ingestion task is a CronJob whose runs execute exactly the task's
  command, as app_writer, with the settings of the release's ConfigMap and
  app_writer's password file; a task without a schedule is suspended and
  runs only on demand.
- The demonstration is a post-install hook Job that runs the commands of
  compose.yaml's demo service, off by default and refused in external mode
  (test_render_values).
- The attached one-off pod is the pod kubectl run builds with the chart's
  JSON patch: it keeps kubectl's command and standard input and gets the
  release's restricted pod settings (test_render_security checks it with
  every other pod).
- No roster reaches the chart: its contents appear in no rendering, no
  value can hold it, and no task may run add_people.

Pod Security, the read-only roots and the credentials of these pods are
checked with every other pod's in test_render_security.
"""
from __future__ import annotations

import csv
import json

import pytest
import yaml

from chart import (
    ATTACHED_PATCH_KEY, CASES, CHART, COMPOSE, DEMO_ROSTER, EXAMPLES_DIR, NAMESPACE, RELEASE, ROSTER_COMMAND,
    attached_patch, attached_pod, case_values, containers, image_reference, is_external, is_hook, kubectl_run_pod,
    pod_templates, values_section,
)

# The ConfigMap keys every ingestion container takes by name, in both modes.
WRITER_SETTINGS = {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PEOPLE_PUBS_CROSSREF_MAILTO",
                   "PEOPLE_PUBS_ORG_PATTERNS"}
WRITER_PASSWORD_FILE = "/run/secrets/pubdex/app_writer_password"
# The placeholder schedule of a task on demand, which a suspended CronJob
# never uses.
ON_DEMAND_SCHEDULE = "0 0 1 1 *"
DEMO_DOI = "10.5555/pubdex-fixture-001"


def _writer_pods(rendered) -> list:
    return [pod for pod in pod_templates(rendered) if pod.component in ("ingest", "demo")]


def _expected_settings(values: dict, config: dict) -> set:
    keys = set(WRITER_SETTINGS)
    if is_external(values):
        keys.add("PGSSLMODE")
        if "PGSSLROOTCERT" in config["data"]:
            keys.add("PGSSLROOTCERT")
    return keys


@pytest.mark.parametrize("case", CASES)
def test_ingestion_pods_connect_as_app_writer_with_its_password_file_only(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    config = rendered.one("ConfigMap", f"{RELEASE}-config")
    assert config["data"]["PGUSER"] == "app_writer"
    pods = _writer_pods(rendered)
    assert pods, "the attached pod at least"
    for pod in pods:
        assert pod.spec["serviceAccountName"] == f"{RELEASE}-ingest", pod.where
        assert pod.spec["automountServiceAccountToken"] is False and pod.spec["enableServiceLinks"] is False
        assert pod.spec["restartPolicy"] == "Never", pod.where
        context = pod.spec["securityContext"]
        assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (10001, 10001, 10001)
        assert not pod.spec.get("initContainers"), pod.where
        (container,) = pod.spec["containers"]
        settings = {entry["name"]: entry["valueFrom"]["configMapKeyRef"] for entry in container["env"]
                    if "valueFrom" in entry}
        assert set(settings) == _expected_settings(values, config), pod.where
        for name, ref in settings.items():
            assert ref == {"name": f"{RELEASE}-config", "key": name}, f"{pod.where}: {name}"
        literal = {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}
        assert literal == {"PEOPLE_DB_PASSWORD_FILE": WRITER_PASSWORD_FILE}, pod.where
        assert container["image"] == image_reference(values["image"]), pod.where
        assert container.get("resources", {}) == values["ingestion"]["resources"], pod.where


@pytest.mark.parametrize("case", CASES)
def test_each_task_is_a_cronjob_that_runs_its_command(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    tasks = values["ingestion"]["tasks"]
    cronjobs = {obj["metadata"]["name"]: obj for obj in rendered.of_kind("CronJob")}
    assert set(cronjobs) == {f"{RELEASE}-ingest-{task}" for task in tasks}
    for task, spec in tasks.items():
        cronjob = cronjobs[f"{RELEASE}-ingest-{task}"]["spec"]
        if spec.get("schedule"):
            assert cronjob["schedule"] == spec["schedule"], task
            assert cronjob["suspend"] is spec.get("suspend", False), task
        else:
            # On demand: never on a schedule, only kubectl create job --from.
            assert (cronjob["schedule"], cronjob["suspend"]) == (ON_DEMAND_SCHEDULE, True), task
        assert cronjob["concurrencyPolicy"] == "Forbid", task
        assert (cronjob["successfulJobsHistoryLimit"], cronjob["failedJobsHistoryLimit"]) == (3, 3), task
        job = cronjob["jobTemplate"]
        # kubectl create job --from=cronjob copies the job template, labels
        # included, so a run started by hand is labelled like a scheduled one.
        assert job["metadata"]["labels"]["app.kubernetes.io/component"] == "ingest", task
        assert job["spec"]["backoffLimit"] == 0, task
        assert job["spec"].get("activeDeadlineSeconds") == spec.get("activeDeadlineSeconds"), task
        (container,) = job["spec"]["template"]["spec"]["containers"]
        assert container["name"] == "ingest" and container["command"] == spec["command"], task
        assert "args" not in container, task


def test_without_tasks_there_is_no_cronjob_and_on_demand_tasks_never_run_on_their_own(helm) -> None:
    rendered = helm.template("--values", CASES["values/default"])
    assert rendered.of_kind("CronJob") == [] and case_values("values/default")["ingestion"]["tasks"] == {}
    scheduled = [obj for case in CASES for obj in helm.template("--values", CASES[case]).of_kind("CronJob")
                 if not obj["spec"]["suspend"]]
    assert scheduled, "some case schedules a task"
    for obj in scheduled:
        assert obj["spec"]["schedule"] != ON_DEMAND_SCHEDULE, obj["metadata"]["name"]


def _compose_demo_commands() -> list:
    """The commands of compose.yaml's demo service, in order."""
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["demo"]
    shell, flag, script = service["command"]
    assert (shell, flag) == ("sh", "-c")
    return [command.strip() for command in script.split(" && ")]


@pytest.mark.parametrize("case", CASES)
def test_the_demonstration_is_a_post_install_hook_off_by_default(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    demo = [obj for obj in rendered.objects if obj["metadata"]["name"] == f"{RELEASE}-demo"]
    if not values["demo"]["enabled"]:
        assert demo == [], "no demonstration unless demo.enabled"
        assert "demo" not in {pod.component for pod in pod_templates(rendered)}
        assert "fixture_demo" not in rendered.text and DEMO_DOI not in rendered.text
        return
    assert not is_external(values), "external mode refuses the demonstration"
    (job,) = demo
    assert job["kind"] == "Job" and is_hook(job)
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "post-install", "after the install only, never after an upgrade"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    assert job["spec"]["backoffLimit"] == 0 and 0 < job["spec"]["activeDeadlineSeconds"] <= 900
    (pod,) = [pod for pod in pod_templates(rendered) if pod.owner is job]
    (container,) = pod.spec["containers"]
    shell, flag, script = container["command"]
    assert (shell, flag) == ("sh", "-c")
    lines = [line.strip() for line in script.splitlines()]
    # Compose's three commands, in its order, after the wait for the
    # database; nothing else is written.
    commands = [line for line in lines if line.startswith("python -m ")]
    assert commands == _compose_demo_commands()
    assert lines[0] == "set -eu"
    assert any(line.startswith("until python -c 'from people_pubs.db.connection import get_conn;") for line in lines)


def test_the_values_and_notes_mark_the_demonstration_as_synthetic(helm) -> None:
    section = values_section("demo")
    assert "It writes synthetic records into the database" in section
    assert "Evaluation mode only; external mode refuses it. Default: false." in section
    notes = helm.notes("--values", CASES["values/default"], "--set", "demo.enabled=true")
    assert f"The demonstration Job {RELEASE}-demo has written SYNTHETIC records" in notes and DEMO_DOI in notes
    assert "SYNTHETIC" not in helm.notes("--values", CASES["values/default"])


@pytest.mark.parametrize("case", CASES)
def test_the_attached_pod_keeps_kubectls_command_and_input_and_gets_the_release_settings(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    config = rendered.one("ConfigMap", f"{RELEASE}-run")
    assert config["metadata"]["labels"]["app.kubernetes.io/component"] == "ingest"
    patch = attached_patch(config)
    assert {operation["op"] for operation in patch} == {"add"}
    # The command and the standard input stay kubectl's: the patch changes
    # no field of the container that kubectl run sets for them.
    for operation in patch:
        assert operation["path"].split("/")[-1] not in ("command", "args", "stdin", "stdinOnce", "tty", "name"), \
            operation["path"]
        assert operation["path"] not in ("/spec/containers", "/spec/containers/0", "/spec", "/metadata"), \
            operation["path"]
    for args in (ROSTER_COMMAND, ["python", "-m", "people_pubs.sync.export_publications"]):
        pod = attached_pod(config, args)
        (container,) = pod["spec"]["containers"]
        assert container["args"] == args and container["stdin"] is True and container["stdinOnce"] is True
        assert "command" not in container and container["tty"] is False
        assert container["name"] == f"{RELEASE}-run"
        assert container["image"] == image_reference(values["image"])
        assert container["imagePullPolicy"] == values["image"]["pullPolicy"]
        # kubectl's own label stays; the release's labels admit the pod to
        # PostgreSQL (test_render_network).
        labels = pod["metadata"]["labels"]
        assert labels["run"] == f"{RELEASE}-run"
        assert {key: labels[key] for key in ("app.kubernetes.io/name", "app.kubernetes.io/instance",
                                            "app.kubernetes.io/component")} == {
            "app.kubernetes.io/name": "pubdex", "app.kubernetes.io/instance": RELEASE,
            "app.kubernetes.io/component": "ingest"}
        assert pod["spec"]["restartPolicy"] == "Never"
    # Patching the pod kubectl builds changes nothing kubectl set that the
    # patch does not name.
    built = kubectl_run_pod(f"{RELEASE}-run", "placeholder.invalid/image", ROSTER_COMMAND)
    assert attached_pod(config)["spec"]["dnsPolicy"] == built["spec"]["dnsPolicy"]


@pytest.mark.parametrize("case", CASES)
def test_the_notes_give_the_attached_command_and_the_task_runs(helm, case: str) -> None:
    values = case_values(case)
    notes = helm.notes("--values", CASES[case])
    run = f"{RELEASE}-run"
    assert (f"kubectl --namespace {NAMESPACE} run {run} -i --rm --restart=Never --quiet \\\n"
            f"    --image={image_reference(values['image'])} --override-type=json \\\n"
            f"    --overrides=\"$(kubectl --namespace {NAMESPACE} get configmap {run} -o jsonpath="
            f"'{{.data.{ATTACHED_PATCH_KEY}}}')\" \\\n"
            f"    -- python -m people_pubs.sync.add_people --csv - < roster.csv") in notes
    assert "give it on standard input" in notes
    tasks = values["ingestion"]["tasks"]
    if not tasks:
        assert "ingestion.tasks defines no CronJob." in notes and "create job" not in notes
        return
    first = sorted(tasks)[0]
    assert f"create job --from=cronjob/{RELEASE}-ingest-{first} \"$run\"" in notes
    for task, spec in tasks.items():
        listed = f"  {RELEASE}-ingest-{task}  "
        line = next(line for line in notes.splitlines() if line.startswith(listed))
        assert line[len(listed):] == ((spec["schedule"] + (" (suspended)" if spec.get("suspend") else ""))
                                      if spec.get("schedule") else "on demand"), task


def _roster_strings() -> set:
    """The values of the synthetic roster that identify a person: ORCID iD,
    full name and address (a first or last name alone, such as Example,
    also occurs in other settings)."""
    with DEMO_ROSTER.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows
    strings = {row[key] for row in rows for key in ("orcid", "display_name", "primary_email") if row[key]}
    assert len(strings) >= 2 * len(rows)
    return strings


@pytest.mark.parametrize("case", CASES)
def test_no_roster_reaches_a_rendering(helm, case: str) -> None:
    rendered = helm.template("--values", CASES[case])
    strings = _roster_strings()
    found = [value for value in strings if value in rendered.text]
    assert found == [], "roster contents in the rendered manifests"
    assert not rendered.of_kind("Secret")
    # The demonstration names the image's roster file, never its contents.
    for pod in pod_templates(rendered):
        for container in containers(pod):
            text = json.dumps(container)
            assert not [value for value in strings if value in text], f"{pod.where}, {container['name']}"


def test_no_chart_file_holds_roster_contents() -> None:
    strings = _roster_strings()
    for path in sorted(CHART.rglob("*")):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not [value for value in strings if value in text], path.relative_to(CHART)
    assert sorted(path.name for path in EXAMPLES_DIR.glob("*.csv")) == []
