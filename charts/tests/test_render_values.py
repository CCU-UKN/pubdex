"""Values the chart has to refuse, each with the message that says why, and
values it has to accept; and the Kubernetes releases it renders for.

values.schema.json refuses a key it does not list at every level, so a
mistyped key, or any key that could carry a password, fails before anything
is rendered. helm reports a schema error with the path of the value; for a
value checked through a shared definition ($defs) helm can name the wrong
path, so those cases match the key and the rule, not the path.
"""
from __future__ import annotations

import json
import re

import pytest

from chart import CASES, CHART, DEMO_ROSTER, KUBE_NEWEST, KUBE_OLDEST

DEFAULT = ("--values", CASES["values/default"])
# External mode with everything it requires, on top of DEFAULT: with the
# default sslmode, verify-full, that includes a CA certificate.
EXTERNAL = ["--set", "mode=external", "--set", "externalDatabase.host=db.example.org",
            "--set", "externalDatabase.caCertificate.configMapName=db-ca",
            "--set", "externalDatabase.setup.adminUser=dbadmin",
            "--set", "externalDatabase.setup.adminSecret.name=pubdex-db-admin"]
NO_CA = ["--set", "externalDatabase.caCertificate.configMapName="]
CREDENTIAL_KEY = re.compile(r"password|passwd|passphrase|token|credential|private.?key", re.I)
SCHEMA_ERROR = "values don't meet the specifications of the schema"

# An Ingress with one host, on top of DEFAULT.
INGRESS = ["--set", "ingress.enabled=true", "--set", "ingress.hosts[0].host=pubdex.example.org"]


def _task(command: list, **fields) -> list:
    """helm arguments that define the ingestion task x."""
    return ["--set-json", "ingestion.tasks=" + json.dumps({"x": {"command": command, **fields}})]


# id -> (helm arguments on top of DEFAULT, texts helm's message has to contain)
REFUSED = {
    "unknown-top-level-key": (["--set", "replicas=2"], [SCHEMA_ERROR, "additional properties 'replicas' not allowed"]),
    "unknown-nested-key": (["--set", "api.replica=2"], ["at '/api'", "additional properties 'replica' not allowed"]),
    "global-values": (["--set", "global.x=1"], ["additional properties 'global' not allowed"]),
    "password-key-in-api": (["--set", "api.dbPassword=x"], ["additional properties 'dbPassword' not allowed"]),
    "password-key-in-secrets": (["--set", "secrets.postgres_password=x"],
                                ["at '/secrets'", "additional properties 'postgres_password' not allowed"]),
    "unknown-mode": (["--set", "mode=production"], ["at '/mode'", "value must be"]),
    "replicas-not-a-number": (["--set-string", "api.replicas=two"], ["at '/api/replicas'", "got string, want integer"]),
    "wait-not-whole-seconds": (["--set-json", "api.dbWaitSeconds=0.5"],
                               ["at '/api/dbWaitSeconds'", "got number, want integer"]),
    "pool-too-large": (["--set", "api.poolSize=33"], ["at '/api/poolSize'", "maximum: got 33, want 32"]),
    "port-zero": (["--set", "api.service.port=0"], ["at '/api/service/port'", "minimum: got 0, want 1"]),
    "unknown-log-level": (["--set", "api.logLevel=TRACE"], ["at '/api/logLevel'", "value must be one of"]),
    "retain-not-a-boolean": (["--set-string", "postgresql.persistence.retainOnUninstall=yes"],
                             ["retainOnUninstall", "got string, want boolean"]),
    "size-not-a-quantity": (["--set", "postgresql.persistence.size=lots"], ["persistence/size", "does not match pattern"]),
    "unknown-resource": (["--set", "api.resources.requests.gpu=1"], ["invalid propertyName 'gpu'"]),
    "database-name-not-an-identifier": (["--set", "database.name=x;rm"],
                                        ["at '/database/name'", "does not match pattern"]),
    "secret-name-not-an-object-name": (["--set", "secrets.name=Bad_Name"], ["at '/secrets/name'"]),
    "digest-malformed": (["--set", "image.digest=sha256:abc"], ["image/digest", "does not match pattern"]),
    "neither-tag-nor-digest": (["--set", "image.tag=", "--set", "image.digest="], ["/image", "'anyOf' failed"]),
    "unknown-pull-policy": (["--set", "postgresImage.pullPolicy=Sometimes"], ["pullPolicy", "value must be one of"]),
    "pull-secret-unknown-key": (["--set", "imagePullSecrets[0].secret=x"], ["additional properties 'secret' not allowed"]),
    "setting-with-a-line-break": (["--set-json", 'ingestion.crossrefMailto="a\\nb"'],
                                  ["ingestion/crossrefMailto", "does not match pattern"]),
    # External mode: what it needs, and what contradicts it.
    "external-without-host": (["--set", "mode=external"], ["externalDatabase.host is required in external mode"]),
    "evaluation-with-host": (["--set", "externalDatabase.host=db.example.org"],
                             ["at '/externalDatabase/host'", "value must be ''"]),
    "evaluation-with-ca": (["--set", "externalDatabase.caCertificate.configMapName=db-ca"],
                           ["at '/externalDatabase/caCertificate/configMapName'", "value must be ''"]),
    "ca-without-verification": ([*EXTERNAL, "--set", "externalDatabase.sslmode=require"],
                                ["at '/externalDatabase/caCertificate/configMapName'", "value must be ''"]),
    "ca-with-prefer": ([*EXTERNAL, *NO_CA, "--set", "externalDatabase.sslmode=prefer",
                        "--set", "externalDatabase.caCertificate.secretName=db-ca"],
                       ["at '/externalDatabase/caCertificate/secretName'", "value must be ''"]),
    "ca-from-secret-and-configmap": ([*EXTERNAL, "--set", "externalDatabase.sslmode=verify-full",
                                      "--set", "externalDatabase.caCertificate.configMapName=db-ca",
                                      "--set", "externalDatabase.caCertificate.secretName=db-ca"],
                                     ["at '/externalDatabase/caCertificate'", "'not' failed"]),
    "verification-without-ca": ([*EXTERNAL, *NO_CA, "--set", "externalDatabase.sslmode=verify-ca"],
                                ["externalDatabase.sslmode verify-ca checks the server's certificate against a CA",
                                 "externalDatabase.caCertificate.configMapName (or .secretName)",
                                 "or choose a weaker externalDatabase.sslmode on purpose"]),
    "unknown-sslmode": ([*EXTERNAL, "--set", "externalDatabase.sslmode=strict"],
                        ["at '/externalDatabase/sslmode'", "value must be one of"]),
    "host-with-a-space": ([*EXTERNAL, "--set", "externalDatabase.host=db sslmode=disable"],
                          ["at '/externalDatabase/host'", "does not match pattern"]),
    "port-too-large": ([*EXTERNAL, "--set", "externalDatabase.port=70000"],
                       ["at '/externalDatabase/port'", "maximum: got 70,000, want 65,535"]),
    "password-key-in-external": ([*EXTERNAL, "--set", "externalDatabase.setup.adminPassword=x"],
                                 ["at '/externalDatabase/setup'", "additional properties 'adminPassword' not allowed"]),
    "setup-without-admin-user": (["--set", "mode=external", "--set", "externalDatabase.host=db.example.org",
                                  "--set", "externalDatabase.caCertificate.configMapName=db-ca"],
                                 ["externalDatabase.setup.adminUser is required while externalDatabase.setup.enabled",
                                  "externalDatabase.setup.enabled=false"]),
    "setup-without-admin-secret": (["--set", "mode=external", "--set", "externalDatabase.host=db.example.org",
                                    "--set", "externalDatabase.caCertificate.configMapName=db-ca",
                                    "--set", "externalDatabase.setup.adminUser=dbadmin"],
                                   ["externalDatabase.setup.adminSecret.name is required",
                                    "kubectl create secret generic pubdex-db-admin --namespace chart-checks"]),
    "admin-secret-is-the-credentials-secret": ([*EXTERNAL, "--set", "externalDatabase.setup.adminSecret.name=pubdex-credentials"],
                                               ["names the Secret of secrets.name"]),
    "admin-is-an-application-role": ([*EXTERNAL, "--set", "externalDatabase.setup.adminUser=app_writer"],
                                     ["at '/externalDatabase/setup/adminUser'", "'not' failed"]),
    # The schema upgrade: its resources, nothing else, and no password.
    "schema-upgrade-unknown-key": (["--set", "schemaUpgrade.waitSeconds=10"],
                                   ["at '/schemaUpgrade'", "additional properties 'waitSeconds' not allowed"]),
    "schema-upgrade-password-key": (["--set", "schemaUpgrade.adminPassword=x"],
                                    ["at '/schemaUpgrade'", "additional properties 'adminPassword' not allowed"]),
    "schema-upgrade-resources-not-a-map": (["--set", "schemaUpgrade.resources=1"],
                                           ["at '/schemaUpgrade/resources'", "got number, want object"]),
    # The demonstration, ingestion and the roster.
    "demo-in-external-mode": ([*EXTERNAL, "--set", "demo.enabled=true"],
                              ["demo.enabled is for evaluation mode only", "writes synthetic records"]),
    "demo-not-a-boolean": (["--set-string", "demo.enabled=yes"], ["at '/demo/enabled'", "got string, want boolean"]),
    "roster-in-values": (["--set-file", f"ingestion.roster={DEMO_ROSTER}"],
                         ["at '/ingestion'", "additional properties 'roster' not allowed"]),
    "task-runs-add-people": (_task(["python", "-m", "people_pubs.sync.add_people", "--csv", "/data/roster.csv"]),
                             ["ingestion.tasks.x runs people_pubs.sync.add_people",
                              "never goes into the values", "helm get notes checks"]),
    "task-not-python": (_task(["sh", "-c", "true"]), ["at '/ingestion/tasks/x/command/0'", "value must be 'python'"]),
    "task-not-an-ingestion-module": (_task(["python", "-m", "people_pubs.api"]),
                                     ["at '/ingestion/tasks/x/command/2'", "does not match pattern"]),
    "task-argument-not-a-string": (_task(["python", "-m", "people_pubs.sync.crossref_backfill", "--max", 200]),
                                   ["at '/ingestion/tasks/x/command/4'", "got number, want string"]),
    "task-without-command": (["--set-json", 'ingestion.tasks={"x":{"schedule":"@daily"}}'],
                             ["at '/ingestion/tasks/x'", "missing property 'command'"]),
    "task-unknown-key": (_task(["python", "-m", "people_pubs.sync.orcid_works"], image="other"),
                         ["at '/ingestion/tasks/x'", "additional properties 'image' not allowed"]),
    "task-name-not-a-dns-label": (["--set-json", 'ingestion.tasks={"Orcid_Works":{"command":["python","-m","people_pubs.sync.orcid_works"]}}'],
                                  ["invalid propertyName 'Orcid_Works'"]),
    "task-name-too-long-for-a-cronjob": (["--set-json", 'ingestion.tasks={"%s":{"command":["python","-m","people_pubs.sync.orcid_works"]}}' % ("t" * 39)],
                                         [f"the CronJob's name checks-ingest-{'t' * 39} is longer than 52 characters"]),
    "task-schedule-malformed": (_task(["python", "-m", "people_pubs.sync.orcid_works"], schedule="every day"),
                                ["at '/ingestion/tasks/x/schedule'", "does not match pattern"]),
    "task-suspend-not-a-boolean": (_task(["python", "-m", "people_pubs.sync.orcid_works"], schedule="@daily",
                                         suspend="yes"), ["at '/ingestion/tasks/x/suspend'", "got string, want boolean"]),
    # The Ingress.
    "ingress-without-hosts": (["--set", "ingress.enabled=true"], ["at '/ingress/hosts'", "minItems: got 0, want 1"]),
    "ingress-host-is-a-url": ([*INGRESS[:-1], "ingress.hosts[0].host=https://pubdex.example.org"],
                              ["at '/ingress/hosts/0/host'", "does not match pattern"]),
    "ingress-path-not-absolute": ([*INGRESS, "--set", "ingress.hosts[0].paths[0].path=v0",
                                   "--set", "ingress.hosts[0].paths[0].pathType=Prefix"],
                                  ["at '/ingress/hosts/0/paths/0/path'", "does not match pattern"]),
    "ingress-unknown-path-type": ([*INGRESS, "--set", "ingress.hosts[0].paths[0].path=/",
                                   "--set", "ingress.hosts[0].paths[0].pathType=Regex"],
                                  ["at '/ingress/hosts/0/paths/0/pathType'", "value must be one of"]),
    "ingress-tls-without-hosts": ([*INGRESS, "--set", "ingress.tls[0].secretName=pubdex-tls"],
                                  ["at '/ingress/tls/0'", "missing property 'hosts'"]),
    "ingress-annotation-not-a-string": (["--set", "ingress.annotations.example\\.org/flag=true"],
                                        ["got boolean, want string"]),
    "ingress-unknown-key": (["--set", "ingress.host=pubdex.example.org"],
                            ["at '/ingress'", "additional properties 'host' not allowed"]),
    # NetworkPolicy.
    "network-policy-not-a-boolean": (["--set-string", "networkPolicy.enabled=no"],
                                     ["at '/networkPolicy/enabled'", "got string, want boolean"]),
    "network-peer-empty": (["--set-json", "networkPolicy.api.allowFrom=[{}]"],
                           ["at '/networkPolicy/api/allowFrom/0'", "minProperties: got 0, want 1"]),
    "network-peer-unknown-key": (["--set-json", 'networkPolicy.api.allowFrom=[{"pods":{}}]'],
                                 ["additional properties 'pods' not allowed"]),
    "network-peer-address-block-with-selector": (
        ["--set-json", 'networkPolicy.api.allowFrom=[{"ipBlock":{"cidr":"192.0.2.0/24"},"podSelector":{}}]'],
        ["at '/networkPolicy/api/allowFrom/0'", "'not' failed"]),
    "network-peer-unknown-operator": (
        ["--set-json", 'networkPolicy.api.allowFrom=[{"podSelector":{"matchExpressions":[{"key":"a","operator":"Like"}]}}]'],
        ["operator", "value must be one of"]),
    # The pods' scheduling and metadata values: Kubernetes' forms, and no
    # other key beside them.
    "node-selector-a-list": (["--set-json", 'nodeSelector=["pool"]'], ["at '/nodeSelector'", "got array, want object"]),
    "node-selector-value-not-a-string": (["--set-json", 'nodeSelector={"pool":1}'], ["got number, want string"]),
    "tolerations-an-object": (["--set-json", 'tolerations={"key":"a"}'], ["at '/tolerations'", "got object, want array"]),
    "toleration-unknown-operator": (["--set-json", 'tolerations=[{"key":"a","operator":"Maybe"}]'],
                                    ["at '/tolerations/0/operator'", "value must be one of"]),
    "toleration-unknown-effect": (["--set-json", 'tolerations=[{"key":"a","operator":"Exists","effect":"Evict"}]'],
                                  ["at '/tolerations/0/effect'", "value must be one of"]),
    "toleration-unknown-key": (["--set-json", 'tolerations=[{"key":"a","operator":"Exists","seconds":5}]'],
                               ["at '/tolerations/0'", "additional properties 'seconds' not allowed"]),
    "toleration-seconds-not-whole": (["--set-json", 'tolerations=[{"key":"a","operator":"Exists","tolerationSeconds":1.5}]'],
                                     ["at '/tolerations/0/tolerationSeconds'", "got number, want integer"]),
    "affinity-a-list": (["--set-json", "affinity=[]"], ["at '/affinity'", "got array, want object"]),
    "affinity-unknown-kind": (["--set-json", 'affinity={"zoneAffinity":{}}'],
                              ["at '/affinity'", "additional properties 'zoneAffinity' not allowed"]),
    "affinity-unknown-term": (["--set-json", 'affinity={"podAntiAffinity":{"required":[]}}'],
                              ["at '/affinity/podAntiAffinity'", "additional properties 'required' not allowed"]),
    "pod-annotation-not-a-string": (["--set-json", 'podAnnotations={"example.org/flag":true}'],
                                    ["got boolean, want string"]),
    "pod-label-value-too-long": (["--set", "podLabels.team=" + "x" * 64], ["podLabels", "maxLength"]),
    "pod-label-key-malformed": (["--set-json", 'podLabels={"bad key":"x"}'], ["invalid propertyName 'bad key'"]),
    "priority-class-not-an-object-name": (["--set", "priorityClassName=High_Priority"],
                                          ["at '/priorityClassName'", "'anyOf' failed"]),
    "priority-class-a-number": (["--set-json", "priorityClassName=1000"], ["at '/priorityClassName'"]),
    "top-level-key-null": (["--set", "nodeSelector=null"], ["missing property 'nodeSelector'"]),
    "unknown-scheduling-key": (["--set-json", "topologySpreadConstraints=[]"],
                               ["additional properties 'topologySpreadConstraints' not allowed"]),
    "api-unknown-scheduling-key": (["--set-json", "api.topologySpreadConstraints=[]"],
                                   ["at '/api'", "additional properties 'topologySpreadConstraints' not allowed"]),
    "postgresql-unknown-scheduling-key": (["--set-json", "postgresql.runtimeClassName=\"x\""],
                                          ["at '/postgresql'", "additional properties 'runtimeClassName' not allowed"]),
    "api-node-selector-a-list": (["--set-json", "api.nodeSelector=[]"], ["at '/api/nodeSelector'", "'anyOf' failed"]),
    "postgresql-tolerations-an-object": (["--set-json", "postgresql.tolerations={}"],
                                         ["at '/postgresql/tolerations'", "'anyOf' failed"]),
    "api-priority-class-malformed": (["--set", "api.priorityClassName=Low"],
                                     ["at '/api/priorityClassName'", "'anyOf' failed"]),
}

# id -> helm arguments on top of DEFAULT that have to render
ACCEPTED = {
    "external": EXTERNAL,
    "external-without-setup": ["--set", "mode=external", "--set", "externalDatabase.host=db.example.org",
                               "--set", "externalDatabase.caCertificate.secretName=db-ca",
                               "--set", "externalDatabase.setup.enabled=false"],
    "external-ipv6-host": [*EXTERNAL, "--set", "externalDatabase.host=2001:db8::10"],
    "external-tls-disabled": [*EXTERNAL, *NO_CA, "--set", "externalDatabase.sslmode=disable"],
    "external-require-without-ca": [*EXTERNAL, *NO_CA, "--set", "externalDatabase.sslmode=require"],
    "external-verify-ca-from-secret": [*EXTERNAL, *NO_CA, "--set", "externalDatabase.sslmode=verify-ca",
                                       "--set", "externalDatabase.caCertificate.secretName=db-ca"],
    "external-superuser-named-postgres": [*EXTERNAL, "--set", "externalDatabase.setup.adminUser=postgres"],
    "pull-secret": ["--set", "imagePullSecrets[0].name=registry-credentials"],
    "tag-only": ["--set", "image.digest=", "--set", "postgresImage.digest="],
    "digest-only": ["--set", "image.tag=", "--set", "postgresImage.tag=",
                    "--set", "image.digest=sha256:" + "1" * 64, "--set", "postgresImage.digest=sha256:" + "2" * 64],
    "no-registry": ["--set", "image.registry=", "--set", "image.repository=pubdex", "--set", "image.tag=local",
                    "--set", "image.digest=", "--set", "image.pullPolicy=Never"],
    "longest-wait": ["--set", "api.dbWaitSeconds=300"],
    "no-replicas": ["--set", "api.replicas=0"],
    "volume-deleted-on-uninstall": ["--set", "postgresql.persistence.retainOnUninstall=false"],
    "named-storage-class": ["--set", "postgresql.persistence.storageClass=standard"],
    "no-resources": ["--set-json", "api.resources={}", "--set-json", "postgresql.resources={}",
                     "--set-json", "schemaUpgrade.resources={}"],
    "demo": ["--set", "demo.enabled=true"],
    "task-on-demand": _task(["python", "-m", "people_pubs.sync.attribution_rules", "list"]),
    "task-scheduled-by-macro": _task(["python", "-m", "people_pubs.sync.orcid_works"], schedule="@weekly"),
    "task-named-like-the-longest-cronjob": ["--set-json", 'ingestion.tasks={"%s":{"command":["python","-m","people_pubs.sync.orcid_works"]}}' % ("t" * 38)],
    "task-in-external-mode": [*EXTERNAL, *_task(["python", "-m", "people_pubs.sync.orcid_works"])],
    "ingress-one-host": INGRESS,
    "ingress-wildcard-host-with-tls": ["--set", "ingress.enabled=true", "--set", "ingress.hosts[0].host=*.example.org",
                                       "--set", "ingress.tls[0].secretName=wildcard-tls",
                                       "--set", "ingress.tls[0].hosts[0]=*.example.org"],
    "ingress-disabled-with-settings": ["--set", "ingress.hosts[0].host=pubdex.example.org"],
    "network-policy-off": ["--set", "networkPolicy.enabled=false"],
    "network-peer-ipv6-block": ["--set-json", 'networkPolicy.api.allowFrom=[{"ipBlock":{"cidr":"2001:db8::/32"}}]'],
    "network-peer-all-namespaces": ["--set-json", 'networkPolicy.api.allowFrom=[{"namespaceSelector":{}}]'],
    "scheduling-everything": ["--values", CASES["values/evaluation"]],
    "toleration-of-every-taint": ["--set-json", 'tolerations=[{"operator":"Exists"}]'],
    "toleration-with-seconds": ["--set-json", 'tolerations=[{"key":"node.kubernetes.io/unreachable",'
                                '"operator":"Exists","effect":"NoExecute","tolerationSeconds":300}]'],
    "pod-anti-affinity": ["--set-json", 'affinity={"podAntiAffinity":{"preferredDuringSchedulingIgnoredDuringExecution":'
                          '[{"weight":1,"podAffinityTerm":{"topologyKey":"kubernetes.io/hostname"}}]}}'],
    "overrides-null": [arg for key in ("nodeSelector", "tolerations", "affinity", "podAnnotations", "podLabels",
                                       "priorityClassName") for arg in ("--set-json", f"api.{key}=null")],
    "overrides-empty": ["--set-json", "postgresql.nodeSelector={}", "--set-json", "postgresql.tolerations=[]",
                        "--set-json", "postgresql.affinity={}", "--set-json", "postgresql.podAnnotations={}",
                        "--set-json", "postgresql.podLabels={}", "--set", "postgresql.priorityClassName="],
}


@pytest.mark.parametrize("arguments, messages", REFUSED.values(), ids=REFUSED.keys())
def test_the_schema_refuses_invalid_values_with_a_reason(helm, arguments: list, messages: list) -> None:
    refusal = helm.refusal(*DEFAULT, *arguments)
    for message in messages:
        assert message in refusal, refusal


@pytest.mark.parametrize("arguments", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_the_schema_accepts_valid_values(helm, arguments: list) -> None:
    assert helm.template(*DEFAULT, *arguments).objects


def test_rendering_without_the_secret_name_says_how_to_create_the_secret(helm) -> None:
    # helm lint does not apply `required`, so this is checked here.
    refusal = helm.refusal()
    assert "secrets.name is required" in refusal
    # The script is not in the chart: the message says where it is.
    assert ("create_secrets.sh -- in the root of the PubDex source of this chart's version, or "
            "/opt/pubdex/create_secrets.sh in the application image: bash create_secrets.sh") in refusal
    assert "./create_secrets.sh" not in refusal
    assert "kubectl create secret generic pubdex-credentials --namespace chart-checks" in refusal
    for key in ("postgres_password", "app_readonly_password", "app_writer_password"):
        assert f"--from-file=secrets/{key}" in refusal


def test_external_mode_asks_only_for_the_two_role_passwords(helm) -> None:
    refusal = helm.refusal("--set", "mode=external", "--set", "externalDatabase.host=db.example.org",
                           "--set", "externalDatabase.caCertificate.configMapName=db-ca",
                           "--set", "externalDatabase.setup.enabled=false")
    assert "secrets.name is required" in refusal
    assert "with the keys app_readonly_password, app_writer_password." in refusal
    assert ("--from-file=secrets/app_readonly_password --from-file=secrets/app_writer_password\n" in refusal)
    assert "postgres_password" not in refusal


def test_the_supported_kubernetes_releases_render_and_older_ones_are_refused(helm) -> None:
    for version in (KUBE_OLDEST, KUBE_NEWEST):
        assert helm.template(*DEFAULT, kube_version=version).objects, version
    refusal = helm.refusal(*DEFAULT, kube_version="1.34.0")
    assert f"chart requires kubeVersion: >={KUBE_OLDEST}-0" in refusal


def _object_schemas(schema: object, path: str = "#"):
    """Every subschema that describes an object with named properties."""
    if isinstance(schema, dict):
        if "properties" in schema and schema.get("type") == "object":
            yield path, schema
        for key, value in schema.items():
            yield from _object_schemas(value, f"{path}/{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from _object_schemas(value, f"{path}/{index}")


def test_every_object_of_the_schema_refuses_unknown_keys() -> None:
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
    objects = list(_object_schemas(schema))
    assert objects
    for path, subschema in objects:
        assert subschema.get("additionalProperties") is False, path
    # The maps without listed properties limit their keys by name instead:
    # resource quantities, annotations, labels and the ingestion tasks.
    defs = schema["$defs"]
    assert defs["quantities"]["propertyNames"] == {"enum": ["cpu", "memory", "ephemeral-storage"]}
    for name in ("annotations", "labels"):
        assert defs[name]["propertyNames"] == {"$ref": "#/$defs/qualifiedName"}, name
        assert defs[name]["additionalProperties"]["type"] == "string", name
    tasks = schema["properties"]["ingestion"]["properties"]["tasks"]
    assert tasks["propertyNames"]["pattern"] and tasks["additionalProperties"] == {"$ref": "#/$defs/task"}
    # Keys such as secrets.name name a Secret; none may hold a credential.
    keys = {key for _path, subschema in objects for key in subschema["properties"]}
    assert not [key for key in keys if CREDENTIAL_KEY.search(key)], "no value can carry a credential"
