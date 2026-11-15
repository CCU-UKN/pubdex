"""External mode: no database server of the chart's own, the setup Job that
prepares the operator's database, the admin credential confined to that
Job, the CA certificate only where a connection checks it, and the lines of
DB/init/000_init.sql the setup leaves out.

Every test here but those of sslmode takes every case and branches on its
mode, so evaluation mode shows the opposite: no setup Job, no admin Secret,
no CA certificate. The tests of sslmode render external mode with and
without a CA certificate: by default every connection verifies the server
(verify-full), which needs a CA certificate, and without one rendering
fails unless a weaker sslmode is set explicitly.
"""
from __future__ import annotations

import re
import subprocess
from typing import Dict, Set

import pytest

from chart import (
    CA_DIR, CA_VOLUME, CASES, CONTAINERS, INIT_SQL, LEFT_OUT_LINES, NAMESPACE, RELEASE, SETUP_SCRIPT, ca_source,
    case_values, chart_values, containers, env_value, is_external, is_hook, pod_templates, setup_enabled,
)

HOOK_EVENTS = "pre-install,pre-upgrade"
HOOK_DELETE_POLICY = "before-hook-creation,hook-succeeded"

# External mode with the setup Job and an ingestion task, so that every pod
# that connects to the server is rendered, but without a CA certificate.
EXTERNAL_WITHOUT_CA = ("--values", CASES["values/default"], "--set", "mode=external",
                       "--set", "externalDatabase.host=db.example.org",
                       "--set", "externalDatabase.setup.adminUser=dbadmin",
                       "--set", "externalDatabase.setup.adminSecret.name=pubdex-db-admin",
                       "--set-json", 'ingestion.tasks={"x":{"command":["python","-m","people_pubs.sync.orcid_works"]}}')
WITH_CA = ("--set", "externalDatabase.caCertificate.configMapName=db-ca")
WEAKER_SSLMODES = ("require", "prefer", "allow", "disable")


@pytest.mark.parametrize("case", CASES)
def test_external_mode_renders_no_database_server(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    bundled = [(kind, name) for kind, name in rendered.names() if name == f"{RELEASE}-postgresql"]
    components = {pod.component for pod in pod_templates(rendered)}
    if is_external(values):
        assert not rendered.of_kind("StatefulSet") and not rendered.of_kind("PersistentVolumeClaim")
        assert bundled == [], "no PostgreSQL Service or ServiceAccount"
        assert "postgresql" not in components
        # The schema bootstrap runs only in the setup Job, never in a pod
        # that keeps running.
        for pod in pod_templates(rendered):
            if pod.owner["kind"] != "Job":
                assert not pod.spec.get("initContainers"), pod.where
    else:
        policy = {"NetworkPolicy"} if values["networkPolicy"]["enabled"] else set()
        assert {kind for kind, _name in bundled} == {"StatefulSet", "Service", "ServiceAccount"} | policy
        assert "setup" not in components


@pytest.mark.parametrize("case", CASES)
def test_the_setup_job_is_a_hook_that_prepares_the_database_first(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    setup = [obj for obj in rendered.objects if obj["metadata"]["name"] == f"{RELEASE}-setup"]
    # The only other hooks are the demonstration's Job, after the install
    # (test_render_ingestion), and helm test's pod (test_render_network).
    other_hooks = [obj for obj in rendered.objects if is_hook(obj) and obj not in setup]
    assert sorted((obj["kind"], obj["metadata"]["name"]) for obj in other_hooks) == sorted(
        ([("Job", f"{RELEASE}-demo")] if values["demo"]["enabled"] else []) + [("Pod", f"{RELEASE}-test")])
    if not setup_enabled(values):
        assert setup == [], "no setup Job, script or account without externalDatabase.setup.enabled"
        return
    assert sorted(obj["kind"] for obj in setup) == ["ConfigMap", "Job", "ServiceAccount"]
    weights = {}
    for obj in setup:
        annotations = obj["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == HOOK_EVENTS, obj["kind"]
        assert annotations["helm.sh/hook-delete-policy"] == HOOK_DELETE_POLICY, obj["kind"]
        weights[obj["kind"]] = int(annotations["helm.sh/hook-weight"])
    # The script and the account exist before the Job starts.
    assert weights["ConfigMap"] < weights["Job"] and weights["ServiceAccount"] < weights["Job"]
    assert [obj for obj in rendered.objects if is_hook(obj) and obj not in other_hooks] == setup, \
        "the setup is the only hook before the install"
    job = rendered.one("Job", f"{RELEASE}-setup")
    assert job["spec"]["backoffLimit"] == 0
    assert 0 < job["spec"]["activeDeadlineSeconds"] <= 3600
    (pod,) = [pod for pod in pod_templates(rendered) if pod.owner is job]
    assert pod.spec["restartPolicy"] == "Never"
    assert pod.spec["automountServiceAccountToken"] is False
    assert pod.spec["serviceAccountName"] == f"{RELEASE}-setup"
    context = pod.spec["securityContext"]
    assert (context["runAsUser"], context["runAsGroup"], context["fsGroup"]) == (999, 999, 999)
    (bootstrap,) = pod.spec["initContainers"]
    assert bootstrap["command"] == ["python", "-m", "people_pubs.bootstrap", "/var/lib/pubdex/initdb"]
    assert (bootstrap["securityContext"]["runAsUser"], bootstrap["securityContext"]["runAsGroup"]) == (10001, 10001)
    (container,) = pod.spec["containers"]
    assert container["command"] == ["bash", "/opt/pubdex/setup/external-setup.sh"]
    volumes = {volume["name"]: volume for volume in pod.spec["volumes"]}
    assert volumes["script"]["configMap"]["name"] == f"{RELEASE}-setup"
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert mounts["script"]["mountPath"] == "/opt/pubdex/setup" and mounts["initdb"]["readOnly"] is True
    # The admin's libpq password file lies in memory only.
    assert volumes["tmp"]["emptyDir"]["medium"] == "Memory"
    # A missing Secret fails the script with its message, not the hook's timeout.
    assert volumes["secrets"]["secret"]["optional"] is True and volumes["admin"]["secret"]["optional"] is True
    server = values["externalDatabase"]
    expected = {
        "PGHOST": server["host"], "PGPORT": str(server["port"]), "PGDATABASE": values["database"]["name"],
        "PGUSER": server["setup"]["adminUser"], "PGSSLMODE": server["sslmode"],
        "PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE": f"/run/secrets/pubdex-admin/{server['setup']['adminSecret']['key']}",
        "PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE": "/run/secrets/pubdex/app_readonly_password",
        "PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE": "/run/secrets/pubdex/app_writer_password",
        "PEOPLE_PUBS_SETUP_INIT_DIR": "/var/lib/pubdex/initdb",
    }
    ca = ca_source(values)
    if ca:
        expected["PGSSLROOTCERT"] = f"{CA_DIR}/{ca[2]}"
    assert {entry["name"]: entry["value"] for entry in container["env"]} == expected
    script = rendered.one("ConfigMap", f"{RELEASE}-setup")["data"]
    assert script == {"external-setup.sh": SETUP_SCRIPT.read_text(encoding="utf-8")}


@pytest.mark.parametrize("case", CASES)
def test_no_admin_credential_reaches_a_pod_that_keeps_running(helm, case: str) -> None:
    values = case_values(case)
    admin = values["externalDatabase"]["setup"]["adminSecret"]["name"]
    holders = []
    for pod in pod_templates(helm.template("--values", CASES[case])):
        for volume in pod.spec.get("volumes") or []:
            if admin and (volume.get("secret") or {}).get("secretName") == admin:
                holders.append(pod)
                assert pod.owner["kind"] == "Job" and is_hook(pod.owner), f"{pod.where} mounts the admin Secret"
                users = [c["name"] for c in containers(pod)
                         if any(m["name"] == volume["name"] for m in c.get("volumeMounts") or [])]
                assert users == ["setup"], f"{pod.where}: only the setup container reads it"
        for container in containers(pod):
            assert not env_value(container, "PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE") or pod.component == "setup"
    assert len(holders) == (1 if setup_enabled(values) else 0)


@pytest.mark.parametrize("case", CASES)
def test_the_ca_certificate_is_mounted_only_where_a_connection_checks_it(helm, case: str) -> None:
    values = case_values(case)
    rendered = helm.template("--values", CASES[case])
    ca = ca_source(values)
    for pod in pod_templates(rendered):
        volumes = {volume["name"]: volume for volume in pod.spec.get("volumes") or []}
        for container in containers(pod):
            name = f"{pod.where}, container {container['name']}"
            mounts = [m for m in container.get("volumeMounts") or [] if m["name"] == CA_VOLUME]
            wanted = bool(ca) and CONTAINERS[(pod.component, container["name"])].connects
            assert bool(mounts) is wanted, name
            if wanted:
                assert mounts == [{"name": CA_VOLUME, "mountPath": CA_DIR, "readOnly": True}], name
                kind, source, key = ca
                source_field = volumes[CA_VOLUME][kind]
                assert source_field["secretName" if kind == "secret" else "name"] == source, name
                assert source_field["items"] == [{"key": key, "path": key}], name
        if CA_VOLUME in volumes:
            assert any(m["name"] == CA_VOLUME for c in containers(pod) for m in c.get("volumeMounts") or []), \
                f"{pod.where}: a CA volume nobody mounts"
    settings = rendered.one("ConfigMap", f"{RELEASE}-config")["data"]
    named = {settings.get("PGSSLROOTCERT")}
    # Literal values; the ingestion pods take the ConfigMap's key by name.
    named |= {entry["value"] for pod in pod_templates(rendered) for container in containers(pod)
              for entry in container.get("env") or [] if entry["name"] == "PGSSLROOTCERT" and "value" in entry}
    if ca:
        assert settings["PEOPLE_PUBS_API_DSN"].endswith(f" sslrootcert={CA_DIR}/{ca[2]}")
        assert named == {f"{CA_DIR}/{ca[2]}"}
    else:
        assert "sslrootcert" not in settings["PEOPLE_PUBS_API_DSN"]
        assert named == {None}, "no PGSSLROOTCERT without a CA certificate"


def _filter(source: str, target: str) -> subprocess.CompletedProcess:
    """The setup script's own filter, run on source."""
    return subprocess.run(
        ["bash", "-c", 'source "$1" && external_setup_filter_init "$2" "$3"', "filter", SETUP_SCRIPT, source, target],
        capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )


def test_the_setup_leaves_out_exactly_three_lines_of_the_schema(tmp_path) -> None:
    # Pinned to the checkout's file: each line is there once, and the filter
    # removes exactly these lines and nothing else.
    lines = INIT_SQL.read_text(encoding="utf-8").split("\n")
    for line in LEFT_OUT_LINES:
        assert lines.count(line) == 1, line
    script = SETUP_SCRIPT.read_text(encoding="utf-8")
    listed = script.split("EXTERNAL_SETUP_LEFT_OUT=(\n", 1)[1].split("\n)\n", 1)[0]
    assert [entry.strip()[1:-1] for entry in listed.splitlines()] == list(LEFT_OUT_LINES)
    target = tmp_path / "000_init.sql"
    proc = _filter(str(INIT_SQL), str(target))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "3"
    assert target.read_text(encoding="utf-8").split("\n") == [line for line in lines if line not in LEFT_OUT_LINES]


# --- sslmode ----------------------------------------------------------------

def _connection_sslmodes(rendered) -> Dict[str, Set[str]]:
    """The sslmode of every connection a rendering configures, by pod and
    container: the read service's connection string, and each container's
    PGSSLMODE, literal or taken from the release's ConfigMap; and the
    ConfigMap's own PGSSLMODE."""
    settings = rendered.one("ConfigMap", f"{RELEASE}-config")["data"]
    modes: Dict[str, Set[str]] = {}
    for pod in pod_templates(rendered):
        for container in containers(pod):
            found = set()
            for name in ("PGSSLMODE", "PEOPLE_PUBS_API_DSN"):
                entry = env_value(container, name)
                if entry is None:
                    continue
                if "value" in entry:
                    value = entry["value"]
                else:
                    ref = entry["valueFrom"]["configMapKeyRef"]
                    value = rendered.one("ConfigMap", ref["name"])["data"][ref["key"]]
                if name == "PEOPLE_PUBS_API_DSN":
                    match = re.search(r"\bsslmode=(\S+)", value)
                    found.add(match.group(1) if match else "")
                else:
                    found.add(value)
            if found:
                modes.setdefault(f"{pod.where}/{container['name']}", set()).update(found)
    modes["ConfigMap"] = {settings.get("PGSSLMODE", "")}
    return modes


def test_by_default_every_connection_to_an_external_server_verifies_it(helm) -> None:
    assert chart_values()["externalDatabase"]["sslmode"] == "verify-full"
    rendered = helm.template(*EXTERNAL_WITHOUT_CA, *WITH_CA)
    modes = _connection_sslmodes(rendered)
    assert {mode for found in modes.values() for mode in found} == {"verify-full"}, modes
    # The read service, the setup Job, the ingestion runs and the attached pod.
    assert {where.split("/")[0] for where in modes} == {"Deployment", "Job", "CronJob", "ConfigMap"}, modes
    assert f"{RELEASE}-run" in {where.split("/")[1] for where in modes if "/" in where}, modes
    assert "with sslmode verify-full." in helm.notes(*EXTERNAL_WITHOUT_CA, *WITH_CA)
    # Evaluation mode connects to its own server inside the cluster with
    # libpq's defaults, and sets no sslmode at all.
    evaluation = helm.template("--values", CASES["values/default"])
    settings = evaluation.one("ConfigMap", f"{RELEASE}-config")["data"]
    assert "PGSSLMODE" not in settings and "sslmode" not in settings["PEOPLE_PUBS_API_DSN"]


@pytest.mark.parametrize("settings, mode", [
    ([], "verify-full"),
    (["--set", "externalDatabase.sslmode=verify-ca"], "verify-ca"),
    (["--set", "externalDatabase.setup.enabled=false"], "verify-full"),
], ids=["default", "verify-ca", "without-setup"])
def test_without_a_ca_certificate_external_mode_fails_closed_and_names_both_ways_out(
        helm, settings: list, mode: str) -> None:
    refusal = helm.refusal(*EXTERNAL_WITHOUT_CA, *settings)
    assert (f"externalDatabase.sslmode {mode} checks the server's certificate against a CA certificate, "
            "and none is named") in refusal
    # A CA certificate ...
    assert (f"kubectl create configmap pubdex-db-ca --namespace {NAMESPACE} "
            "--from-file=ca.crt=<CA certificate>") in refusal
    assert "externalDatabase.caCertificate.configMapName (or .secretName)" in refusal
    # ... or a weaker sslmode, chosen on purpose.
    assert "or choose a weaker externalDatabase.sslmode on purpose" in refusal


@pytest.mark.parametrize("mode", WEAKER_SSLMODES)
def test_a_weaker_sslmode_set_explicitly_renders_without_a_ca_certificate(helm, mode: str) -> None:
    rendered = helm.template(*EXTERNAL_WITHOUT_CA, "--set", f"externalDatabase.sslmode={mode}")
    modes = _connection_sslmodes(rendered)
    assert {found for values in modes.values() for found in values} == {mode}, modes
    assert "sslrootcert" not in rendered.one("ConfigMap", f"{RELEASE}-config")["data"]["PEOPLE_PUBS_API_DSN"]
    for pod in pod_templates(rendered):
        assert CA_VOLUME not in {volume["name"] for volume in pod.spec.get("volumes") or []}, pod.where


@pytest.mark.parametrize("mode", WEAKER_SSLMODES)
def test_a_ca_certificate_with_a_mode_that_checks_none_is_refused(helm, mode: str) -> None:
    refusal = helm.refusal(*EXTERNAL_WITHOUT_CA, *WITH_CA, "--set", f"externalDatabase.sslmode={mode}")
    assert "at '/externalDatabase/caCertificate/configMapName'" in refusal and "value must be ''" in refusal
