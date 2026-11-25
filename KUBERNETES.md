# Running PubDex on Kubernetes

The Helm chart in [`charts/pubdex/`](charts/pubdex/) installs the PubDex read
service, its ingestion commands and, if you want, a PostgreSQL 16 database
of its own on a Kubernetes cluster, from the same release images as the
Compose deployment. [`DEPLOYMENT.md`](DEPLOYMENT.md) stays the canonical
way to run PubDex on one host; this guide covers the chart. The chart has
two modes:

- **`evaluation`**, the default: the chart also runs PostgreSQL 16, as a
  single-replica StatefulSet with a volume of its own. It is meant for
  trying PubDex out with synthetic or disposable data; it has no
  replication and no scheduled backup, and backup and restore have not been
  tested on Kubernetes. Data you cannot lose belongs in external mode, on a
  PostgreSQL server whose backups you run.
- **`external`**: PubDex uses a database on a PostgreSQL 16 server that you
  run. A setup Job of the chart prepares the database at the install, and
  runs again at each upgrade unless that upgrade switches it off
  ([Upgrade](#upgrade)).

The chart is not in a chart repository: you install it from a clone, from
the source of a PubDex release, or from the chart package file of a PubDex
release, `pubdex-<version>.tgz`, which every `helm install` and `helm
upgrade` below takes in place of `charts/pubdex`. Its `version` and its
`appVersion` are the PubDex version, one number for the chart and the
images it runs ([`VERSIONING.md`](VERSIONING.md)).

**Tested and untested commands.** [`run_cluster_acceptance.sh`](run_cluster_acceptance.sh),
stage 9 of the complete suite, runs this guide's commands on two disposable
kind clusters, Kubernetes 1.37 and 1.35 ([`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md),
*The Helm chart on a disposable cluster*, which numbers its steps). The
[appendix](#appendix-what-ran-these-commands) lists every block of commands
with the steps of stage 9 that ran it and any difference from the block, or
says that no test ran it. Stage 9 uses its own file names, and the
namespace `pubdex-external` for external mode; such differences are not
repeated there.

## The short path

You need a cluster you may create a namespace in, kubectl, Helm 4.3 or a
later 4.x, Bash, a clone of this repository, and nodes that can pull the
published images from `ghcr.io` ([What you need](#what-you-need)). From the
repository's root, create the namespace and the database credentials:

```sh
./create_secrets.sh                              # the three password files, in secrets/
kubectl create namespace pubdex
kubectl label namespace pubdex pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/warn=restricted pod-security.kubernetes.io/audit=restricted
kubectl --namespace pubdex create secret generic pubdex-credentials \
  --from-file=secrets/postgres_password \
  --from-file=secrets/app_readonly_password \
  --from-file=secrets/app_writer_password
```

Before using an installation or upgrade example below, prepare
`values-images.yaml` with both digests from the selected PubDex release's
[release record](releases/README.md), replacing the two placeholders:

```yaml
image:
  digest: "sha256:<application image digest>"
postgresImage:
  digest: "sha256:<PostgreSQL image digest>"
```

The chart defaults name version tags only: a tag can be moved, so set both
digests explicitly. An unpublished checkout needs its own built images.
Stage 9 supplies its own image values files from the release record or its
local builds; these placeholder substitutions describe the operator's step.

Install evaluation mode with the synthetic demonstration, which writes
synthetic records, so use this release for trying PubDex out only:

```sh
cat > values-demo.yaml <<'YAML'
secrets:
  name: pubdex-credentials
demo:
  enabled: true
YAML
helm install pubdex charts/pubdex --namespace pubdex --values values-demo.yaml --wait --timeout 300s --values values-images.yaml --wait-for-jobs
```

Read the publication the demonstration wrote, through a port forward bound
to 127.0.0.1. In one terminal:

```sh
kubectl --namespace pubdex port-forward --address 127.0.0.1 service/pubdex-api 8000:8000
```

And in another:

```sh
curl -s http://127.0.0.1:8000/readyz
curl -s 'http://127.0.0.1:8000/v0/publications?limit=5'      # the DOI 10.5555/pubdex-fixture-001
helm test pubdex --namespace pubdex --timeout 300s
```

Remove everything, the database included:

```sh
helm uninstall pubdex --namespace pubdex --wait --timeout 300s
kubectl --namespace pubdex delete pvc data-pubdex-postgresql-0 --wait=true
kubectl --namespace pubdex delete secret pubdex-credentials
```

`helm get notes pubdex --namespace pubdex` prints the commands for a
release again, the port forward among them (stage 9, step 7). The rest of
this guide is the detail.

## Contents

- [What you need](#what-you-need)
- [Supported platforms](#supported-platforms)
- [Installing with external PostgreSQL](#installing-with-external-postgresql)
- [Values](#values)
- [Secrets](#secrets), with [Changing a password](#changing-a-password)
- [Settings and the ConfigMap](#settings-and-the-configmap)
- [Ingestion](#ingestion), with [Importing a roster](#importing-a-roster)
- [Ingress and TLS](#ingress-and-tls)
- [NetworkPolicy](#networkpolicy)
- [Health and readiness](#health-and-readiness)
- [Upgrade](#upgrade), [Rollback](#rollback), [Uninstall](#uninstall)
- [Backup and restore](#backup-and-restore)
- [What identifies a deployment](#what-identifies-a-deployment)
- [Troubleshooting](#troubleshooting)
- [Appendix: what ran these commands](#appendix-what-ran-these-commands)

## What you need

- **Kubernetes 1.35, 1.36 or 1.37**, the three minor releases the
  Kubernetes project maintained on 2026-11-24 ([`VERSIONING.md`](VERSIONING.md),
  *The Kubernetes window*), with `linux/amd64` nodes.
  `Chart.yaml` sets `kubeVersion: ">=1.35.0-0"`, so Helm refuses older
  clusters. [Supported platforms](#supported-platforms) says which releases
  ran.
- **Helm 4.3 or a later 4.x**, and **kubectl** within one minor release of
  the cluster. Helm 4.3 supports Kubernetes 1.34 to 1.37 (Helm's version
  skew policy), and a kubectl 1.36 is within one minor release of 1.35,
  1.36 and 1.37 alike (Kubernetes' version skew policy), so one serves all
  three. `./setup_cluster_tools.sh` installs the helm 4.3.1, kubectl 1.36.6
  and kind 0.33.0 that the tests use under `.tools/`, each checked against
  its pinned SHA-256 ([`DB/lib/cluster_tools.sh`](DB/lib/cluster_tools.sh)).
- **A clone of this repository**, or the release source: the chart, its
  examples and `create_secrets.sh` come from there.
- **The images.** The nodes pull the selected PubDex release's images,
  `ghcr.io/ccu-ukn/pubdex:<version>` and
  `ghcr.io/ccu-ukn/pubdex-postgres:<version>`, by the digests you set from
  its release record, without credentials.
  For a registry of your own, copy both images there and set
  `image.registry`, `image.repository`, `image.digest`, the same keys of
  `postgresImage`, and `imagePullSecrets` if it needs credentials.
- **Pod Security.** Every pod of the chart meets the `restricted` profile.
  Label the namespace to enforce it, as in [The short path](#the-short-path).
- **Storage**, in evaluation mode: a default StorageClass, or one named in
  `postgresql.persistence.storageClass`, that provides ReadWriteOnce volumes
  that PostgreSQL, uid and gid 999, can write to, either through the pod's
  `fsGroup` or because the provisioner creates them writable for every
  user. kind's default class did the latter in the tests; others are
  untested. The volume is 8Gi unless `postgresql.persistence.size` says
  otherwise.
- **Access rights** in the release's namespace: to create, change and
  delete ConfigMaps, Secrets (Helm keeps each revision of a release in a
  Secret), Services, ServiceAccounts, Deployments, StatefulSets, Jobs,
  CronJobs, NetworkPolicies, Ingresses if you enable one, Pods (`helm test`
  and `kubectl run`) and PersistentVolumeClaims; to read pods, their logs,
  events and ReplicaSets (`helm --wait` reads a Deployment's ReplicaSets);
  to change `deployments/scale` (`kubectl scale`); and to create and get
  `pods/exec`, `pods/portforward` and `pods/attach` (`kubectl exec`,
  `kubectl port-forward` and `kubectl run -i` connect over WebSocket, which
  needs both). Creating and labelling the namespace needs rights over
  namespaces, which an administrator can use for you instead. The chart
  creates no cluster-scoped object and no RBAC role, and its pods mount no
  ServiceAccount token. On Kubernetes 1.35, stage 9 runs evaluation mode's
  lifecycle as a namespace-scoped user, a ServiceAccount whose Role grants
  exactly these rights and nothing else: the dry run, the install with the
  demonstration, `helm test`, the upgrade, the rollback and the uninstall
  (steps 31 to 35), and the API server's audit log shows no refused
  request. That lifecycle used no right over CronJobs, Ingresses,
  `pods/attach` or `deployments/scale`; these serve the ingestion tasks, an
  Ingress, the roster import and the restore, which ran only as the
  cluster's administrator, or not at all. External mode, too, ran as the
  administrator only.
- **Network.** Ingestion reaches the metadata providers over HTTPS. In
  external mode the namespace reaches your PostgreSQL server.

## Supported platforms

*Supported* means that a configuration meets [What you need](#what-you-need)
and that this guide's tested commands passed on the oldest and on the
newest release a row names. A release between those two rests on API
compatibility, and its row says so. Each row says how its status was
established. The tests ran on one machine, at the date given, on kind
clusters of one node with `linux/amd64`, the network plugin kindnet, kind's
default StorageClass and Pod Security `restricted` enforced, in Docker
Engine 29.9.3 on Linux x86_64.

| Platform | Status | How it was established | Date |
|---|---|---|---|
| Kubernetes 1.37, the newest release | **supported** | Kubernetes 1.37.0 on kind 0.33.0, node image `kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5`, as the cluster's administrator: everything stage 9 runs there (steps 2 to 20), both modes, every block the [appendix](#appendix-what-ran-these-commands) assigns to those steps | 2026-11-12 |
| Kubernetes 1.35, the floor | **supported** | Kubernetes 1.35.8 on kind 0.33.0, node image `kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0`: evaluation mode's lifecycle (steps 31 to 35) as a namespace-scoped user holding only the rights of [What you need](#what-you-need), with the kubelet pulling the published images from `ghcr.io`. External mode, the password change, the loss of the database and the NetworkPolicy ran on 1.37 only | 2026-11-12 |
| Kubernetes 1.36 | **supported**, by API compatibility | no cluster ran 1.36. The chart uses only the stable APIs `v1`, `apps/v1`, `batch/v1` and `networking.k8s.io/v1`, which 1.35, 1.36 and 1.37 all serve | — |
| Kubernetes before 1.35 | **refused** by `kubeVersion` | stage 8 checks the refusal | 2026-11-12 |
| Kubernetes 1.38 and later | **not known** | nothing | — |
| Helm 4.3 or a later 4.x | **supported** | helm 4.3.1: stage 8, and every step of stage 9 on both clusters. No later 4.x release has run | 2026-11-12 |
| kubectl within one minor release of the cluster | **supported** | kubectl 1.36.6 against both clusters, every step of stage 9 | 2026-11-12 |
| Helm 3 | **not supported**: it receives security fixes only, until 2027-02-10 | nothing | — |
| Managed Kubernetes services and other distributions | **untested**: storage classes, network plugins and admission differ, and nothing has run there | nothing | — |
| Network plugins other than kindnet, such as Calico or Cilium | **untested**: whether they enforce the [NetworkPolicy](#networkpolicy), and how | nothing | — |
| An ingress controller, a certificate manager | **untested**: no controller or certificate manager has run with the two [Ingress examples](#ingress-and-tls), and no request has passed through them. Stage 8 lints and renders both; the API servers of Kubernetes 1.37 and 1.35 admit every object they render but `helm test`'s pod, the Ingress and the NetworkPolicy included, in a server-side dry run, on 1.35 as the namespace-scoped user | stage 9, steps 5 and 25: a dry run, which installs and serves nothing | 2026-11-13 |
| OpenShift | **not supported**: the chart sets the fixed user ids 999 and 10001, while OpenShift's default security context constraint admits only ids from a range it assigns to each namespace (inferred, untested) | nothing | — |
| arm64 nodes | **not supported**: the images are `linux/amd64` only | nothing | — |

Argo CD and Flux apply Helm charts with their own handling of hooks (Argo
CD, for one, runs `pre-install` and `pre-upgrade` hooks such as the setup
Job, and `post-install` hooks such as the demonstration, at every sync), and
that handling is untested.

The table states what was established on its dates. Which Kubernetes
releases are supported is established again at each PubDex release, by the
rule of [`VERSIONING.md`](VERSIONING.md), *The Kubernetes window*, which
also gives the Kubernetes project's dates for them.

## Installing with external PostgreSQL

### What the server provides

- **PostgreSQL 16.** The setup refuses other major versions. Only 16.15, on
  a disposable server prepared as a managed one would be, has been tested.
- **A database for PubDex alone**, named `database.name` (default
  `people_db`), owned by an admin role that has `CREATEROLE`, or with a
  superuser as the admin role.
- **The extensions** `citext`, `pgcrypto`, `unaccent` and `pg_trgm`.
  PostgreSQL 16 marks all four as trusted, so the database's owner can
  create them. Where a server does not allow that, someone who may has to
  create them in the database's schema `public` first.
- **TCP connections from the release's namespace**, authenticated by
  `scram-sha-256` in `pg_hba.conf`. `md5` also works, because the server
  then runs SCRAM for a SCRAM password. This holds for the admin role too,
  whose password has to be stored as SCRAM, PostgreSQL 16's default: every
  connection of the setup requires SCRAM, and a server that asks for the
  password in clear text (`password`) or as an MD5 hash, or asks for none
  (`trust`), stops the setup with `authentication method requirement
  "scram-sha-256" failed` before anything is sent in answer.
- **Enough connections.** During a rolling update the read service holds
  up to (`api.replicas` + `api.replicas`/4, rounded up) × `api.poolSize`
  connections, 8 with the defaults, and each running ingestion command adds
  one or two, the setup Job one; the sum has to stay below the server's
  `max_connections` less its reserved connections, which the chart does
  not check ([`values.yaml`](charts/pubdex/values.yaml) derives it at
  `api.replicas`).

| What the setup does | What the admin role needs |
|---|---|
| Create the four extensions | owner of the database (trusted extensions) |
| Create `app_readonly`, `app_writer` and `maintenance` | `CREATEROLE`; PostgreSQL 16 then gives it the ADMIN OPTION on them |
| Create the schemas, tables, views and functions | owner of the database. The admin role then owns them, and has to keep owning them |
| Set the two roles' passwords on later runs | `CREATEROLE` and the ADMIN OPTION on both roles: the same admin role as the first run |
| Revoke `CONNECT` and `TEMPORARY` on the database from `PUBLIC` | owner of the database |
| `ALTER SYSTEM`, `pg_reload_conf()`, a role named `postgres` | not needed: the setup leaves out the three lines of `000_init.sql` that use them |

**One PubDex installation per PostgreSQL server.** A role belongs to the
whole server, not to one database. A second installation on the same server
would share `app_readonly` and `app_writer`, and each installation's read
role could read the other's database. The setup therefore refuses a
database without the PubDex schema while these roles exist on the server.

### Secrets and TLS

Create, in the release's namespace, the Secret with the two application
roles' passwords, a separate Secret with the admin role's password (here
from a file `admin_password` of mode 600 that holds it), and a ConfigMap
with the certificate of the CA that signed the server's certificate (here
from a file `ca.crt`, in PEM):

```sh
./create_secrets.sh
kubectl --namespace pubdex create secret generic pubdex-credentials \
  --from-file=secrets/app_readonly_password \
  --from-file=secrets/app_writer_password
kubectl --namespace pubdex create secret generic pubdex-db-admin --from-file=password=admin_password
kubectl --namespace pubdex create configmap pubdex-db-ca --from-file=ca.crt=ca.crt
```

Only the setup Job mounts the admin Secret. Create it for each run of the
setup, which the install and a password change need, and delete it
afterwards ([Upgrade](#upgrade)).

Every connection to the server, the read service's, the ingestion's and the
setup Job's, uses libpq's `sslmode` from `externalDatabase.sslmode`. The
default, `verify-full`, uses TLS, checks the server's certificate against
the CA certificate that `externalDatabase.caCertificate` names, a key of a
Secret or of a ConfigMap, and checks that the certificate names
`externalDatabase.host`. The CA certificate is therefore required: without
it rendering fails, and the message says how to give one or how to choose
a weaker mode. The chart mounts it read-only at `/run/pubdex/ca/<key>`, only
in the containers that connect to the server.

For a server whose certificate comes from a publicly trusted CA, as with
some managed database services, give the root certificate of that CA, in
PEM, as the ConfigMap above: the chart uses no trust store of the image or
the node. If the server does not send its intermediate certificates,
append them to the same file. This follows from how libpq checks a
certificate and has not been tested.

A weaker mode is your explicit choice, made in your values file. With
`verify-ca` the CA certificate stays required. With a mode that checks no
certificate your values file names none, because the chart refuses one
then:

```yaml
externalDatabase:
  sslmode: require
  caCertificate:
    configMapName: ""
```

Each weaker mode gives something up. `verify-ca` drops the check of the
name: a host with any certificate from that CA can pose as the server.
`require` drops the check of the certificate: any host can pose as the
server, and read and change what the connections carry. `prefer`, `allow`
and `disable` may use no TLS at all. Passwords travel by SCRAM in every
mode, so none is sent as such; the data and the queries are. Stage 9 ran
`verify-full`; stage 8 renders every mode, and no other mode has run in a
cluster.

### Installing with the setup Job

[`examples/values-external.yaml`](charts/pubdex/examples/values-external.yaml)
names the Secrets and the ConfigMap above, `verify-full` and the admin role
`people_db_owner`. Write your server's host, and anything else you change,
into a values file of your own, for example `my-values.yaml`:

```yaml
externalDatabase:
  host: db.example.org
```

Then install, and delete the admin Secret:

```sh
helm install pubdex charts/pubdex --namespace pubdex \
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml --wait --timeout 300s --values values-images.yaml
kubectl --namespace pubdex delete secret pubdex-db-admin
```

The setup Job `<release>-setup` is a `pre-install,pre-upgrade` hook. It runs
[`files/external-setup.sh`](charts/pubdex/files/external-setup.sh) in the
release's PostgreSQL image, as a client, before the release's other objects
are created or changed:

1. It reads the three password files and connects as the admin role.
2. A read-only query checks the server version, the admin role's
   privileges, the extensions, and whether the schema and the roles exist
   already. If a check fails, the Job stops with every reason and changes
   nothing.
3. On a database without the PubDex schema, it applies the schema of the
   application image in one transaction; a failure leaves nothing behind.
   On a database with the schema, it skips this step.
4. It sets both role passwords, as SCRAM verifiers computed in the client,
   and lets only these two roles and the owner connect.
5. Each role has to log in again with its new password, by SCRAM.

Every `helm upgrade` runs it again unless that upgrade switches it off, as
a routine upgrade does ([Upgrade](#upgrade)); a password change reaches the
server this way ([Changing a password](#changing-a-password)). After a success
Helm deletes the Job, its ConfigMap and its ServiceAccount. A failed setup
fails the install or upgrade, and `kubectl logs job/<release>-setup` says
why: Helm deletes the ConfigMap and the ServiceAccount and keeps the failed
Job, with its pod, for that log. The next install or upgrade that runs the
setup replaces the Job; a routine upgrade, a rollback or `helm uninstall`
does not, so delete it by hand once you have read the log. The block also
deletes the ConfigMap and the ServiceAccount, which stay if Helm could not
create the Job at all:

```sh
kubectl --namespace pubdex delete job,configmap,serviceaccount -l app.kubernetes.io/instance=pubdex,app.kubernetes.io/component=setup
```

After the setup, a second hook, the schema upgrade Job
`<release>-schema-upgrade`, runs `python -m people_pubs.schema upgrade` in
the application image as the admin role, with the same admin Secret key and
TLS settings. It applies the schema changes of the image that the
database's record of them, the table `pubdex.schema_changes`, does not hold
yet, each in a transaction of its own; on a first install that follows the
baseline, at an upgrade it is what the new images add, and with nothing
outstanding it changes nothing. It runs exactly when the setup runs, so a
routine upgrade runs neither. Before it changes anything it refuses a
database it cannot bring to the schema of its image; then the install or
upgrade fails before the release's other objects change, as with a failed
setup, and `kubectl logs job/<release>-schema-upgrade` says why. Helm keeps
the failed Job, with its pod, until the next install or upgrade that runs
the setup replaces it; one that succeeded it deletes, with its
ServiceAccount.

### Installing without the setup Job

With `externalDatabase.setup.enabled=false`, the admin password never
enters the cluster. Run the same script yourself, once before the first
install and again after each password change, from the repository's root on
a machine with Docker that reaches the server:

```sh
docker volume create pubdex-initdb
docker run --rm --network none -v pubdex-initdb:/var/lib/pubdex/initdb \
  ghcr.io/ccu-ukn/pubdex:0.1.0@sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325 \
  python -m people_pubs.bootstrap /var/lib/pubdex/initdb
docker run --rm --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -v pubdex-initdb:/var/lib/pubdex/initdb:ro \
  -v "$PWD/charts/pubdex/files/external-setup.sh:/opt/pubdex/setup/external-setup.sh:ro" \
  -v "$PWD/secrets:/run/secrets/pubdex:ro" \
  -v "$PWD/admin:/run/secrets/pubdex-admin:ro" \
  -e PGHOST=db.example.org -e PGPORT=5432 -e PGDATABASE=people_db \
  -e PGUSER=people_db_owner -e PGSSLMODE=verify-full \
  -v "$PWD/ca:/run/pubdex/ca:ro" -e PGSSLROOTCERT=/run/pubdex/ca/ca.crt \
  -e PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE=/run/secrets/pubdex-admin/password \
  -e PEOPLE_PUBS_APP_READONLY_PASSWORD_FILE=/run/secrets/pubdex/app_readonly_password \
  -e PEOPLE_PUBS_APP_WRITER_PASSWORD_FILE=/run/secrets/pubdex/app_writer_password \
  -e PEOPLE_PUBS_SETUP_INIT_DIR=/var/lib/pubdex/initdb \
  ghcr.io/ccu-ukn/pubdex-postgres:0.1.0@sha256:33988fb179e2065e653ad7813cecf812c21f4381537b8b8db546ad37236b780a \
  bash /opt/pubdex/setup/external-setup.sh
docker volume rm pubdex-initdb
```

`secrets/` holds the files of `./create_secrets.sh`, `admin/password` the
admin role's password, and `ca/ca.crt` the CA certificate. For another
`sslmode`, give it in `PGSSLMODE` as well; the script defaults to
`verify-full`. With a mode that checks no certificate, also leave out the
CA certificate's two options. In every mode the script requires SCRAM for
each connection, and it refuses a `PGREQUIREAUTH` other than
`scram-sha-256`.

The script applies the baseline schema only. Then run the schema upgrade
of the application image you install, as the same role, which applies the
schema changes the database does not have yet, each in a transaction of its
own:

```sh
docker run --rm --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$PWD/admin:/run/secrets/pubdex-admin:ro" \
  -e PGHOST=db.example.org -e PGPORT=5432 -e PGDATABASE=people_db \
  -e PGUSER=people_db_owner -e PGSSLMODE=verify-full \
  -v "$PWD/ca:/run/pubdex/ca:ro" -e PGSSLROOTCERT=/run/pubdex/ca/ca.crt \
  -e PEOPLE_DB_PASSWORD_FILE=/run/secrets/pubdex-admin/password \
  ghcr.io/ccu-ukn/pubdex:0.2.0 \
  python -m people_pubs.schema upgrade
```

It requires SCRAM as well, reads the TLS settings from the same variables,
and refuses, before it changes anything, a database it cannot bring to the
schema of its image; its output says why. Run it again with the newer
application image before each upgrade to a newer PubDex release; with
nothing outstanding it changes nothing. Then install with the setup turned
off:

```sh
helm install pubdex charts/pubdex --namespace pubdex \
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml \
  --set externalDatabase.setup.enabled=false --wait --timeout 300s --values values-images.yaml
```

To stay on this path, write `enabled: false` under `externalDatabase.setup`
in your values file instead of the `--set`, so that no later upgrade runs
the setup Job, which would fail without the admin Secret.

### Limits of external mode

- Only PostgreSQL 16.15 on a disposable server has been tested; no managed
  database service has.
- One installation per server, as above.
- `helm uninstall` leaves the database, its schema and data, and the three
  roles on the server.
- The demonstration is refused; [Ingestion](#ingestion) runs its steps.

## Values

[`charts/pubdex/values.yaml`](charts/pubdex/values.yaml) lists every value
with its default and a comment that explains it.
[`values.schema.json`](charts/pubdex/values.schema.json) refuses any other
key, a value of the wrong type or form, and contradictory settings, before
anything is rendered: for example a host in evaluation mode, external mode
without one, or a key whose name contains `password`. The table is an
index; the comments in `values.yaml` are the reference.

| Value | Default | Described in |
|---|---|---|
| `mode` | `evaluation` | the introduction |
| `image.registry`, `image.repository`, `image.tag`, `image.digest`, `image.pullPolicy` | `ghcr.io`, `ccu-ukn/pubdex`, the chart's version, `""`, `IfNotPresent`: the application image by tag; set the digest from the release record | [What identifies a deployment](#what-identifies-a-deployment) |
| `postgresImage.registry`, `postgresImage.repository`, `postgresImage.tag`, `postgresImage.digest`, `postgresImage.pullPolicy` | `ghcr.io`, `ccu-ukn/pubdex-postgres`, the chart's version, `""`, `IfNotPresent`: the PostgreSQL image by tag; set the digest from the release record | the same |
| `imagePullSecrets` | `[]` | [What you need](#what-you-need) |
| `nodeSelector`, `tolerations`, `affinity`, `podAnnotations`, `podLabels`, `priorityClassName` | `{}`, `[]`, `{}`, `{}`, `{}`, `""`: none | [Scheduling and pod metadata](#scheduling-and-pod-metadata) |
| `postgresql.nodeSelector`, `postgresql.tolerations`, `postgresql.affinity`, `postgresql.podAnnotations`, `postgresql.podLabels`, `postgresql.priorityClassName`, `api.nodeSelector`, `api.tolerations`, `api.affinity`, `api.podAnnotations`, `api.podLabels`, `api.priorityClassName` | `null`: the top-level value | [Scheduling and pod metadata](#scheduling-and-pod-metadata) |
| `secrets.name` | `""`, required | [Secrets](#secrets) |
| `database.name` | `people_db` | [Installing with external PostgreSQL](#installing-with-external-postgresql) |
| `postgresql.persistence.size`, `postgresql.persistence.storageClass`, `postgresql.persistence.retainOnUninstall` | `8Gi`, the default class, `true` | [What you need](#what-you-need), [Uninstall](#uninstall) |
| `postgresql.shmSize`, `postgresql.terminationGracePeriodSeconds`, `postgresql.resources` | `256Mi`, `60`, requests of 100m CPU and 256Mi | [Health and readiness](#health-and-readiness) |
| `externalDatabase.host`, `externalDatabase.port`, `externalDatabase.sslmode` | `""`, `5432`, `verify-full` | [Secrets and TLS](#secrets-and-tls) |
| `externalDatabase.caCertificate.secretName`, `externalDatabase.caCertificate.configMapName`, `externalDatabase.caCertificate.key` | `""`, `""`, `ca.crt` | [Secrets and TLS](#secrets-and-tls) |
| `externalDatabase.setup.enabled`, `externalDatabase.setup.adminUser`, `externalDatabase.setup.adminSecret.name`, `externalDatabase.setup.adminSecret.key`, `externalDatabase.setup.resources` | `true`, `""`, `""`, `password`, requests of 50m CPU and 64Mi | [Installing with the setup Job](#installing-with-the-setup-job) |
| `schemaUpgrade.resources` | requests of 50m CPU and 128Mi | [Installing with the setup Job](#installing-with-the-setup-job) |
| `api.replicas`, `api.logLevel`, `api.poolSize`, `api.dbWaitSeconds`, `api.statementTimeoutMs`, `api.resources`, `api.service.port` | `1`, `INFO`, `4`, `5`, `5000`, a memory limit of 256Mi, `8000` | [Settings and the ConfigMap](#settings-and-the-configmap) |
| `ingestion.crossrefMailto`, `ingestion.orgPatterns`, `ingestion.resources`, `ingestion.tasks` | `""`, `""`, requests of 50m CPU and 128Mi, `{}` | [Ingestion](#ingestion) |
| `demo.enabled` | `false` | [The demonstration](#the-demonstration) |
| `ingress.enabled`, `ingress.className`, `ingress.annotations`, `ingress.hosts`, `ingress.tls` | `false`, `""`, `{}`, `[]`, `[]` | [Ingress and TLS](#ingress-and-tls) |
| `networkPolicy.enabled`, `networkPolicy.api.allowFrom` | `true`, `[]` | [NetworkPolicy](#networkpolicy) |

Keep your values in files of your own and give the same files to every
`helm install` and `helm upgrade`, as the tests do. A release name may have
40 characters at most.

### Scheduling and pod metadata

The six top-level keys `nodeSelector`, `tolerations`, `affinity`,
`podAnnotations`, `podLabels` and `priorityClassName` take Kubernetes' own
forms and apply to every pod of the release: the read service, PostgreSQL
with its init container, the setup Job, the ingestion runs, the
demonstration, `helm test`'s pod and the attached pod of `<release>-run`.
The same six keys under `api` and under `postgresql` apply to that
component's pods alone. Such a key **replaces** the top-level value and is
never merged with it: `api.tolerations: []` gives the read service no
toleration at all. Left out or `null`, it takes the top-level value. In
this example every pod runs on the nodes labelled `pool: pubdex`, and the
read service's pods carry both labels, because a component's labels replace
the top-level ones:

```yaml
nodeSelector:
  pool: pubdex
podLabels:
  team: library
api:
  podLabels:
    team: library
    tier: web
```

`podLabels` and `podAnnotations` cannot set what the chart sets itself: the
labels `app.kubernetes.io/name`, `instance`, `component`, `version`,
`part-of` and `managed-by`, `helm.sh/chart` and the client label
`<release>-client` ([NetworkPolicy](#networkpolicy)), and the annotations
`checksum/config`, `helm.sh/hook`, `helm.sh/hook-weight` and
`helm.sh/hook-delete-policy`. Such values fail to render, and the message
names the key. `priorityClassName` has to name a PriorityClass that exists
in the cluster: Kubernetes' priority admission refuses a pod that names
another. The API server admits a Deployment, StatefulSet, Job or CronJob
whose pod template names a missing class; the pods its controller creates
are the ones refused. Stage 8 checks how the six keys and their overrides
render on every pod of both modes, which then still meet Pod Security
`restricted` ([`charts/tests/test_render_scheduling.py`](charts/tests/test_render_scheduling.py)).
Stage 9 (steps 5 and 31) renders [`charts/tests/values/evaluation.yaml`](charts/tests/values/evaluation.yaml),
which sets all six at the top level and replaces some under `api` and
`postgresql`, and the API servers of Kubernetes 1.37 and 1.35 admit every
object it renders but `helm test`'s pod in a server-side dry run, without a
warning, Pod Security's included, although neither cluster has the
PriorityClasses it names. A dry run schedules nothing and creates no pod: no
cluster has installed a release with any of the six set.

## Secrets

The chart takes no password and generates none. Its values name a Secret
you create, so no password reaches the values, the rendered manifests or
Helm's release history, where Helm keeps each revision's values and
manifests. The Secret holds the files of [`create_secrets.sh`](create_secrets.sh)
as its keys, created as in [The short path](#the-short-path):

| Key | Password of | Mounted by |
|---|---|---|
| `postgres_password` | `postgres`, the PostgreSQL superuser; evaluation mode only | the PostgreSQL container |
| `app_readonly_password` | `app_readonly`, the read service's role | PostgreSQL (evaluation), the read service, the setup Job (external) |
| `app_writer_password` | `app_writer`, the ingestion role | PostgreSQL (evaluation), the ingestion and demonstration pods, the attached pod, the setup Job (external) |

Each container mounts only its keys, as files readable by its own group
(mode 0440) under `/run/secrets/pubdex/`. External mode needs only the two
role keys, and its admin Secret is mounted by the setup Job alone. No
password is in a pod's environment or command line. In evaluation mode the
PostgreSQL container reads the role files only when it initialises an empty
volume, so a later change of the Secret alone changes no password.

Anyone who may read Secrets in the namespace, or run a pod there that
mounts one, can read the passwords. Kubernetes stores Secrets unencrypted
unless the cluster encrypts them at rest. The files in `secrets/` are plain
text as well ([`DEPLOYMENT.md`](DEPLOYMENT.md), *Secrets*): delete them or
keep them where only you can read them.

### Changing a password

**The outage.** The read service reads its password when it starts, so in
both modes a password change interrupts it briefly. From the moment the
server takes `app_readonly`'s new password until the restart below has
replaced every read service pod, the old pods keep the connections they
hold, but every new connection they open is refused. A request or readiness
check that needs a new connection then gets 503 `database_unavailable`,
and a pod that is not ready leaves its Service. The outage lasts for the
rest of the role-password step, or of the setup Job and its upgrade, plus
the restart. To keep it short, run the commands right after each other, the
restart at once. On stage 9's clusters a whole password change, with its
checks, took 7 seconds in evaluation mode and 21 in external mode, whose
upgrade also runs the schema upgrade's hook (steps 10 and 18); the outage itself was
not measured, and this description follows from how the read service
connects.

**Evaluation mode.** Create new files, replace the Secret, set the
passwords in the database with the image's role-password step, then restart
the read service, which reads its password when it starts:

```sh
PEOPLE_PUBS_SECRETS_DIR=secrets-new ./create_secrets.sh
kubectl --namespace pubdex create secret generic pubdex-credentials \
  --from-file=secrets-new/postgres_password \
  --from-file=secrets-new/app_readonly_password \
  --from-file=secrets-new/app_writer_password \
  --dry-run=client --output yaml | kubectl --namespace pubdex replace --filename -
for role in postgres app_readonly app_writer; do
  printf '%s\t%s\n' "$role" "$(cat "secrets-new/${role}_password")"
done | kubectl --namespace pubdex exec -i pubdex-postgresql-0 --container postgresql -- \
  bash /docker-entrypoint-initdb.d/020_role_passwords.sh --stdin
kubectl --namespace pubdex rollout restart deployment/pubdex-api
kubectl --namespace pubdex rollout status deployment/pubdex-api --timeout 300s
```

`020_role_passwords.sh --stdin` reads one line per role, its name, a tab and
its password, sets each and confirms it with a new SCRAM-authenticated
connection, so no password reaches a command line. Instead of new random
files, you can write passwords of your own into copies of the files, one
line each, a different one per role. Keep the new files and delete the old
ones afterwards. Ingestion pods read their file when each run starts, so
change `app_writer`'s while none runs.

**External mode.** Replace the Secret with the two role keys as above, then
let the setup set the passwords on the server, with an upgrade that gives
the release's values files and runs the setup:

```sh
kubectl --namespace pubdex create secret generic pubdex-db-admin --from-file=password=admin_password
helm upgrade pubdex charts/pubdex --namespace pubdex \
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml --wait --timeout 300s --values values-images.yaml
kubectl --namespace pubdex delete secret pubdex-db-admin
kubectl --namespace pubdex rollout restart deployment/pubdex-api
kubectl --namespace pubdex rollout status deployment/pubdex-api --timeout 300s
```

Do not use `--reuse-values` here: it keeps every value of the release's
last revision, so after a routine upgrade it keeps
`externalDatabase.setup.enabled=false`, no setup runs, and the restarted
read service cannot log in (from Helm's description of `--reuse-values`,
untested). The admin role's own password is yours to change on the server.

## Settings and the ConfigMap

The ConfigMap `<release>-config` holds the settings that are not secret,
one key per environment variable: the read service's connection string
without a password, its log level, pool size, wait and statement timeout,
the ingestion's libpq settings (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`,
and in external mode `PGSSLMODE` and `PGSSLROOTCERT`), the Crossref contact
and the employers that count as yours. Change them through the values,
never by editing the ConfigMap, which the next upgrade overwrites.

The read service's pods carry a checksum of the ConfigMap, so a
`helm upgrade` that changes a setting replaces them; stage 9 checks that the
checksum and the pods change (step 8). Ingestion pods read the ConfigMap
when each run starts. PostgreSQL in evaluation mode takes no setting from
it. A Secret you replace is not part of the release, so no checksum
follows it: restart the read service yourself, as in [Changing a
password](#changing-a-password).

## Ingestion

The ingestion commands of [`DB/SCRIPTS.md`](DB/SCRIPTS.md),
`python -m people_pubs.sync.<command> [arguments]`, run as `app_writer`.
Each pod mounts only that role's password file, takes its connection
settings from the ConfigMap and runs under Pod Security `restricted`. There
are two ways to run them.

**As Jobs of a CronJob.** Each entry of `ingestion.tasks` becomes a CronJob
`<release>-ingest-<task>`:

```yaml
ingestion:
  tasks:
    orcid-works:
      command: [python, -m, people_pubs.sync.orcid_works]
      schedule: "15 3 * * 0"
    refresh:
      command: [python, -m, people_pubs.sync.attribution_rules, recompute, --refresh-only]
```

A task without a `schedule` is suspended and runs only on demand; a
schedule is in the controller's time zone, usually UTC. A run is not
retried and never overlaps the previous run of its task. The last three
succeeded and the last three failed runs of each task are kept with their
logs. Scheduled runs have not been tested: no test waited for a schedule.
Any task runs at once, without a change to the release:

```sh
run=refresh-$(date +%Y%m%d%H%M%S)
kubectl --namespace pubdex create job --from=cronjob/pubdex-ingest-refresh "$run"
kubectl --namespace pubdex get job "$run" --output 'jsonpath={.status.succeeded}/{.status.failed}'   # 1/ once it succeeded
kubectl --namespace pubdex logs "job/$run"
```

**Attached to your terminal.** For a single command, and for anything that
reads standard input or writes standard output, `kubectl run` starts a
one-off pod that is deleted when the command ends. The ConfigMap
`<release>-run` holds a JSON patch that gives that pod the release's
image, labels, restricted security context, settings and `app_writer`'s
password file; the command stays on kubectl's command line:

```sh
kubectl --namespace pubdex run pubdex-run -i --rm --restart=Never --quiet \
  --image=ghcr.io/ccu-ukn/pubdex:0.1.0@sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325 \
  --override-type=json \
  --overrides="$(kubectl --namespace pubdex get configmap pubdex-run --output 'jsonpath={.data.overrides}')" \
  -- python -m people_pubs.sync.export_publications > publications.csv
```

Name the pod `<release>-run`, as above, one run at a time. kubectl returns
the command's exit status. If kubectl itself is interrupted, the pod may
remain: `kubectl --namespace pubdex delete pod pubdex-run` (untested).

### Importing a roster

A roster holds personal data. It never goes into the values, a ConfigMap,
a Secret or a command line: Helm keeps values and rendered manifests in the
release's history, and Kubernetes keeps a pod's command line in its
specification. Give it on standard input to the attached pod instead:

```sh
kubectl --namespace pubdex run pubdex-run -i --rm --restart=Never --quiet \
  --image=ghcr.io/ccu-ukn/pubdex:0.1.0@sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325 \
  --override-type=json \
  --overrides="$(kubectl --namespace pubdex get configmap pubdex-run --output 'jsonpath={.data.overrides}')" \
  -- python -m people_pubs.sync.add_people --csv - < roster.csv
```

The roster reaches only the container's standard input. The pod is deleted
when the import ends, also after a failed import. What `add_people` prints
names people by ORCID iD and rows by number, never by name or address.
Like any container's output, it is also written to the container's log on
the node, which the kubelet removes after the pod, and a log collector in
the cluster receives it. `--dry-run` prints names and addresses, so do not
use it where such a collector runs. The chart refuses an ingestion task
that runs `add_people`. [`DEPLOYMENT.md`](DEPLOYMENT.md), *Importing a
roster*, describes the roster's format.

### The demonstration

`demo.enabled=true` runs the synthetic demonstration of
[`QUICKSTART.md`](QUICKSTART.md) once, as a `post-install` hook Job
`<release>-demo`, as `app_writer`: the bundled roster, the bundled ORCID
and Crossref payloads with no provider request, and the fictional
institute as an attribution rule, the three commands of `docker compose run
--rm demo`. `GET /v0/publications` then serves the DOI
`10.5555/pubdex-fixture-001`. It writes synthetic records into the
release's database: use it only in a release that you uninstall afterwards
together with its volume. External mode refuses it. The Job waits up to
five minutes for the database; a failure fails `helm install`, and the
failed Job stays for `kubectl logs job/<release>-demo` until you delete it
with `kubectl --namespace pubdex delete job pubdex-demo` (untested), even
after `helm uninstall`. Stage 9 runs it in
[The short path](#the-short-path) (steps 6 and 14).

## Ingress and TLS

> **Warning:** the read service has no authentication. An Ingress
> publishes everything it serves to everyone who can reach the Ingress's
> hosts. Put authentication in front of it, or keep the hosts private.

`ingress.enabled` is `false` by default. Enabled, it needs at least one
host; each host's paths lead to the Service `<release>-api`, and
`ingress.tls` names a Secret of type `kubernetes.io/tls` per group of
hosts. Two examples, each for host `pubdex.example.org`:

- [`examples/values-ingress-tls.yaml`](charts/pubdex/examples/values-ingress-tls.yaml):
  a certificate you create;
- [`examples/values-ingress-cert-manager.yaml`](charts/pubdex/examples/values-ingress-cert-manager.yaml):
  an annotation that lets a certificate manager in the cluster issue the
  certificate into the Secret and renew it.

With the first:

```sh
kubectl --namespace pubdex create secret tls pubdex-tls --cert=fullchain.pem --key=privkey.pem
helm install pubdex charts/pubdex --namespace pubdex --values charts/pubdex/examples/values-ingress-tls.yaml --values values-images.yaml
```

**The examples are admitted, never served.** Stage 8 lints both for
Kubernetes 1.35 and 1.37, renders them, and checks that the Ingress leads
every host and path to the read service and that the warnings are there.
Stage 9 (steps 5 and 31) renders each and has the API servers of
Kubernetes 1.37 and 1.35 admit every object but `helm test`'s pod, the
Ingress and the NetworkPolicy included, in a server-side dry run, without a
warning; on 1.35 as the namespace-scoped user of
[What you need](#what-you-need). No ingress controller or certificate
manager has run with them, and no request has passed through them.

Replace the host, the class (`ingress.className`, empty for the cluster's
default) and the controller's namespace in the example. With NetworkPolicy
on, the read service admits the controller's pods only when
`networkPolicy.api.allowFrom` names them, as both examples do by the
namespace's name label. For a controller whose pods run with
`hostNetwork: true`, the Kubernetes documentation on NetworkPolicy leaves
the behaviour to the network plugin: in its most common implementation the
plugin ignores such pods when it matches a `namespaceSelector`, so the
examples' selector does not admit them, and treats their connections as
the node's, which an `ipBlock` entry in `networkPolicy.api.allowFrom` can
admit.

The chart renders no Gateway API route. An `HTTPRoute` whose `backendRefs`
name the Service `<release>-api` and port `api.service.port` publishes the
read service in the same way; the same warning applies, and the gateway's
pods have to be in `networkPolicy.api.allowFrom`. This is untested.

## NetworkPolicy

`networkPolicy.enabled` is `true` by default. The policies restrict only
connections *to* the release's pods:

- PostgreSQL (evaluation mode) admits TCP 5432 only from the release's
  read service, ingestion, demonstration and attached pods.
- The read service admits TCP 8000 only from pods of the release's
  namespace labelled `<release>-client: "true"`, `helm test`'s pod among
  them, and from the peers in `networkPolicy.api.allowFrom`, such as an
  ingress controller's namespace.

A NetworkPolicy takes effect only where the cluster's network plugin
enforces it; elsewhere the policies are accepted and change nothing. Stage
9 observed kind's plugin, kindnet, enforce them (step 12): a pod without
labels reached neither service, a pod with the client label only the read
service, and a pod with the ingestion pods' labels only PostgreSQL. A
running pod that had failed to reach the read service reached it once the
command below had given it the label. The kubelet's probes and `kubectl
port-forward` are not affected. To let a pod of the namespace read the
service, give it the label in its manifest, or:

```sh
kubectl --namespace pubdex label pod <pod> pubdex-client=true
```

Connections from the pods stay open, because DNS, the metadata providers
and an external server differ per cluster. Where your plugin enforces
egress rules, a policy such as this restricts the release's pods to DNS,
HTTPS and the release's PostgreSQL, for evaluation mode and the release
name `pubdex`. It is untested:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: pubdex-egress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/instance: pubdex
  policyTypes: [Egress]
  egress:
    - ports:
        - {protocol: UDP, port: 53}
        - {protocol: TCP, port: 53}
    - to:
        - podSelector:
            matchLabels:
              app.kubernetes.io/instance: pubdex
              app.kubernetes.io/component: postgresql
      ports:
        - {protocol: TCP, port: 5432}
    - ports:
        - {protocol: TCP, port: 443}
```

## Health and readiness

| Pod | Probes | Ready means |
|---|---|---|
| PostgreSQL, `<release>-postgresql-0` (evaluation) | startup and readiness: the Compose health check, every 5 seconds for up to five minutes, then every 10; liveness: `pg_isready`, restarted after six failures 10 seconds apart | the server accepts TCP connections and both application roles have SCRAM passwords, so a first start that failed part-way never becomes ready |
| read service, `<release>-api` | startup and liveness: `GET /livez`; readiness: `GET /readyz` every 10 seconds, with a timeout of `api.dbWaitSeconds` plus 2 | a database connection within `api.dbWaitSeconds`, a schema the application supports and the snapshot readable |

`helm install --wait` and `helm upgrade --wait` return once every pod is
ready. While the database is away, `/readyz` answers 503
`database_unavailable` and `/livez` 200: the read service's pod becomes not
ready and is not restarted, and it becomes ready again once the database
is back. Stage 9 observed this by scaling PostgreSQL to 0 and back (step
11); the pod became not ready 31 to 32 seconds after the database went.
`helm test` runs a restricted pod with the client label that asks the read
service for `/readyz` and the first page of `/v0/publications`, and fails
unless both answer 200 with JSON; Helm deletes it once it has passed.

PostgreSQL stops on SIGINT, a fast shutdown, which its image declares as
its stop signal, and is killed after `postgresql.terminationGracePeriodSeconds`.
`/dev/shm` is an in-memory volume of `postgresql.shmSize` that counts
against the pod's memory, so a memory limit has to leave room for it.

## Upgrade

`helm upgrade` changes a Helm release to another chart, other values or
other images, the chart and images of a new PubDex release among them
([Upgrading to a new release](#upgrading-to-a-new-release)):

```sh
helm upgrade pubdex charts/pubdex --namespace pubdex --values values-demo.yaml --values my-values.yaml --wait --timeout 300s --values values-images.yaml --wait-for-jobs
```

In evaluation mode every install and upgrade runs the schema upgrade,
`python -m people_pubs.schema upgrade`, as a Job of its revision,
`<release>-schema-upgrade-<revision>`, as `postgres`, beside the release's
other objects: it applies the schema changes of the application image that
the database has not had yet, and with nothing outstanding it changes
nothing. With `--wait-for-jobs` Helm waits until the Job has completed, and
fails the upgrade when it fails; `kubectl --namespace pubdex logs
job/pubdex-schema-upgrade-2`, for revision 2, says what it did. Helm
deletes the Job of the previous revision at the upgrade.

**External mode.** The setup Job runs before an upgrade unless the upgrade
switches it off, and it needs the admin Secret, which you deleted after the
install; the schema upgrade Job runs after it, exactly when it runs
([Installing with the setup Job](#installing-with-the-setup-job)). Three
things need the setup: the first install, a password change, and an upgrade
to images whose schema changes the database has not had yet. Every other
upgrade is a *routine upgrade*: it changes neither a password nor the
schema and runs without the setup, so the admin Secret need not exist. Give
the release's values files and switch the setup off for this upgrade:

```sh
helm upgrade pubdex charts/pubdex --namespace pubdex \
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml \
  --set externalDatabase.setup.enabled=false --wait --timeout 300s --values values-images.yaml
```

Give the switch on the command line, for that upgrade alone. In a values
file that you give to every upgrade, it would also keep the setup of a
password change from running. Without the switch, and without the admin
Secret, the upgrade runs the setup, which fails within seconds, before any
object of the release changes:

- Helm stops with `UPGRADE FAILED: pre-upgrade hooks failed`, naming the
  Job `pubdex-setup` as failed. `kubectl --namespace pubdex logs
  job/pubdex-setup` says that `PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE` names
  a file that does not exist, because the Secret that holds it is missing.
- `helm status` reports the new revision as `failed`, and `helm history`
  shows the previous one still `deployed`. The read service keeps its pods
  and settings and goes on serving.
- The failed Job stays, with its pod
  ([Installing with the setup Job](#installing-with-the-setup-job)).

To go on, either create the admin Secret and run the same upgrade again,
which replaces the failed Job, or run the routine upgrade above, with the
switch, and then delete the failed Job.

An upgrade that runs the setup, as a password change does ([Changing a
password](#changing-a-password)) and an upgrade with schema changes does
([Upgrading to a new release](#upgrading-to-a-new-release)), needs the
admin Secret for that run.
Create it first and delete it afterwards:

```sh
kubectl --namespace pubdex create secret generic pubdex-db-admin --from-file=password=admin_password
helm upgrade pubdex charts/pubdex --namespace pubdex \
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml --wait --timeout 300s --values values-images.yaml
kubectl --namespace pubdex delete secret pubdex-db-admin
```

To run other images, give their registry, repository, tag and digest in
`image` and `postgresImage`. `postgresql.persistence` cannot change after
the install, because Kubernetes refuses changes to a StatefulSet's volume
claim template; this is untested.

### Upgrading to a new release

A PubDex release brings a new chart and new images under one version, and
may bring schema changes, which the schema upgrade applies (what other
tools call a migration). [`VERSIONING.md`](VERSIONING.md) says which
upgrades are supported: from every PubDex release from the floor on, and a
rollback only to the PubDex release you upgraded from. Read the new
PubDex release's section in [`CHANGELOG.md`](CHANGELOG.md) first: *Upgrade and
rollback* names the floor and says whether the way back is an image
rollback or a restore.

**A backup first**, in external mode with your server's own means or
`pg_dump` as the admin role, in evaluation mode as in [Backup and
restore](#backup-and-restore). It is the only way back after a schema change
that is not backward compatible. Restoring a backup on Kubernetes is outside
what the suite runs; PubDex provides no rollback by restore on Kubernetes
([Rollback](#rollback)).

**The chart and the images.** Take the PubDex release's chart package file,
`pubdex-<version>.tgz`, from its release page, and check it against the
checksum file `SHA256SUMS` of its [release record](releases/README.md), in
the directory of the download, with `sha256sum --check --ignore-missing
SHA256SUMS` (GNU coreutils); or use `charts/pubdex` of a clone at the tag
`v<version>`. The package holds the chart's examples, under
`pubdex/examples/`, as the clone does under `charts/pubdex/examples/`. Write
`values-images.yaml` as in [The short path](#the-short-path), with the two
digests the release record names for the new PubDex release: `images` in
`releases/<version>/release.json`, or that PubDex release's file `DIGESTS`. The
chart's defaults name the images by version tag only, and a tag can be
moved.

**The upgrade**, with the values files of the install. In evaluation mode,
where the schema upgrade runs at every upgrade:

```sh
helm upgrade pubdex pubdex-<version>.tgz --namespace pubdex --values values-demo.yaml --values my-values.yaml --wait --timeout 300s --values values-images.yaml --wait-for-jobs
```

In external mode, run the upgrade with the setup and the admin Secret for
this run, since the schema upgrade runs exactly when the setup runs. Take
the example from the new PubDex release's package, not from the previous
PubDex release:

```sh
tar -xzf pubdex-<version>.tgz pubdex/examples/values-external.yaml
kubectl --namespace pubdex create secret generic pubdex-db-admin --from-file=password=admin_password
helm upgrade pubdex pubdex-<version>.tgz --namespace pubdex \
  --values pubdex/examples/values-external.yaml --values my-values.yaml --wait --timeout 300s --values values-images.yaml
kubectl --namespace pubdex delete secret pubdex-db-admin
```

The schema upgrade's hook applies the schema changes the database has not
had yet, as the admin role, after the setup; a failure of either fails the
upgrade before the release's other objects change. Without the setup Job,
run the schema upgrade of the new application image yourself first, as in
[Installing without the setup Job](#installing-without-the-setup-job), and
then the upgrade with the setup switched off. A new installation of a PubDex
release takes the package file in place of `charts/pubdex` in the same way.

**If the schema upgrade was left out.** A routine upgrade, without the
setup, to images whose schema changes the database has not had cannot
complete. Helm waits until its timeout and fails with `UPGRADE FAILED:
resource Deployment/pubdex/pubdex-api not ready`, and `helm status` reports
the new revision as `failed`. The new read service pod runs but stays not
ready: its readiness probe fails with HTTP 503, and `/readyz` answers with
the reason `schema_upgrade_required` and a message that names the missing
schema change and `python -m people_pubs.schema upgrade`; its log has the
same text. The previous pod stays ready and goes on serving. These two
commands show it:

```sh
helm status pubdex --namespace pubdex
kubectl --namespace pubdex get pods --selector app.kubernetes.io/instance=pubdex,app.kubernetes.io/component=api
```

Run the upgrade with the setup and the admin Secret, as above: its schema
upgrade applies what is outstanding, and the pod that stayed not ready
becomes ready without being replaced.

**The way back** is a [Rollback](#rollback): an image rollback after an
upgrade whose schema changes are all backward compatible, otherwise the
restore of the backup taken before the upgrade.

## Rollback

The commands below perform an image rollback, after an upgrade whose
schema changes are all backward compatible. PubDex provides no rollback by
restore on Kubernetes. After a schema change that is not backward
compatible, the way back is the backup taken before the upgrade, restored
by the operator before the older images run; in external mode, with the
server's own means. The new PubDex release's notes say when an upgrade
needs this. `helm rollback` starts the older images; it does not restore
the backup.

```sh
helm history pubdex --namespace pubdex
helm rollback pubdex 1 --namespace pubdex --wait --timeout 300s
```

Both modes roll back in the same way. In external mode a rollback runs no
setup, so it needs no admin Secret and changes no password: stage 9 rolls
back an external release without that Secret, after a password change and a
routine upgrade, and the passwords of the change stay in force.

`helm rollback` restores what Helm manages for that revision: the images,
the settings in the ConfigMap, the replicas, the ingestion tasks, the
Ingress and the NetworkPolicy. It does not restore:

- **the schema**: a rollback never removes a schema change, in either mode.
  After an upgrade whose schema changes are all backward compatible, the
  rollback is an image rollback: the older images accept the schema with
  its later changes and serve on it (stage 9, steps 9, 20, 25 and 34), and
  their ingestion writes on it (step 25). After a schema change that is not backward
  compatible, the older images refuse the database: their read service
  stays not ready with the reason `schema_not_supported`. The operator's
  restore described above is required ([Backup and restore](#backup-and-restore));
  `helm rollback` alone is insufficient. A rollback is supported only to
  the PubDex release you upgraded from;
- **the data**: the database volume, or the external database, keeps every
  change made since;
- **your Secrets**: the operator's Secret is not part of the release, so
  passwords stay as they are;
- **the setup** in external mode: it is a `pre-install,pre-upgrade` hook,
  and Helm runs only `pre-rollback` and `post-rollback` hooks on a
  rollback, of which the chart has none. The rollback's revision carries
  the setup hooks of the revision it returns to, yet stage 9 found that
  none of them ran.

## Uninstall

```sh
helm uninstall pubdex --namespace pubdex --wait --timeout 300s
```

What remains, and how to delete it deliberately:

- **Evaluation mode: the database volume's claim**
  `data-<release>-postgresql-0`, while `postgresql.persistence.retainOnUninstall`
  is `true`, the default. PostgreSQL initialises only an empty volume, so
  a later install with the same release name in the namespace uses the
  claim again, with its database and the passwords it was initialised with,
  whatever the Secret then holds. Install again with the same passwords in
  the Secret, or delete the claim first; [Troubleshooting](#troubleshooting)
  shows what other passwords lead to, and the ways out. Delete the claim,
  and with it the database, with
  `kubectl --namespace pubdex delete pvc data-pubdex-postgresql-0 --wait=true`
  (step 13). With a StorageClass whose reclaim policy is `Delete`, as
  kind's default, the volume goes too; with `Retain`, the PersistentVolume
  and its data stay until you delete it (untested).
- **External mode: the database**, its schema and data, and the roles
  `app_readonly`, `app_writer` and `maintenance` stay on the server (step
  21). Drop them on the server if you want them gone.
- **The Secrets and the ConfigMap you created**: `kubectl --namespace pubdex
  delete secret pubdex-credentials`, and in external mode `kubectl
  --namespace pubdex delete configmap pubdex-db-ca` (steps 13 and 21).
- **Objects of a failed setup or demonstration**, as Helm does not track
  hook objects: see [Installing with the setup Job](#installing-with-the-setup-job)
  and [The demonstration](#the-demonstration).

## Backup and restore

Neither has been tested on Kubernetes, and this section promises nothing
beyond what the commands say. PubDex provides no rollback by restore on
Kubernetes. After a schema change that is not backward compatible, the way
back is the backup taken before the upgrade, restored by the operator
before the older images run; in external mode, with the server's own means.
The new PubDex release's notes say when an upgrade needs this
([Rollback](#rollback)). The restore commands below are for the same
PubDex release only.

A backup is a dump in PostgreSQL's custom
format, into a file only you can read, as in [`DEPLOYMENT.md`](DEPLOYMENT.md),
*Backup and restore*. It holds every record, personal data included, but no
role or password. In evaluation mode:

```sh
(umask 077 && kubectl --namespace pubdex exec pubdex-postgresql-0 --container postgresql -- pg_dump -U postgres -Fc people_db > people_db.dump)
```

To restore it into this release or a new one, which has to run the same
PubDex release and PostgreSQL 16, stop the read service and any ingestion,
replace the database and start the read service again with your
`api.replicas`:

```sh
kubectl --namespace pubdex scale deployment pubdex-api --replicas=0
kubectl --namespace pubdex exec pubdex-postgresql-0 --container postgresql -- dropdb -U postgres --force people_db
kubectl --namespace pubdex exec pubdex-postgresql-0 --container postgresql -- createdb -U postgres people_db
kubectl --namespace pubdex exec -i pubdex-postgresql-0 --container postgresql -- pg_restore -U postgres -d people_db --single-transaction --exit-on-error < people_db.dump
kubectl --namespace pubdex scale deployment pubdex-api --replicas=1
```

Stage 7 runs the same dump and restore on the Compose deployment, where
`DEPLOYMENT.md` gives the conditions: the database has to be empty, the
roles and their passwords are those of the server restored into, and the
query there verifies the restore. While the database is
dropped, PostgreSQL's pod is not ready. In external mode, back up with your
server's own means or `pg_dump` as the admin role; restoring into an
external server is not covered. Scheduled backups and recovery drills are
not part of the chart.

## What identifies a deployment

A deployment is the chart, the values and the two images:

| Part | Identified by |
|---|---|
| the chart | its version, the PubDex version: `helm history` shows its name and version, for example `pubdex-0.2.0`, and its `appVersion`, the same number; installed from a clone, also the clone's commit |
| the values | `helm get values <release> --all` |
| the application and PostgreSQL images | their digests: the chart's defaults name the images by version tag only, so set both digests from the [release record](releases/README.md); each pod's `imageID` names the image that runs |
| the schema | the baseline and the schema changes the database has had, which its record, the table `pubdex.schema_changes`, names ([Upgrading to a new release](#upgrading-to-a-new-release)) |

```sh
helm history pubdex --namespace pubdex
```

```sh
helm get values pubdex --namespace pubdex --all
kubectl --namespace pubdex get pods --output 'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[*].imageID}{"\n"}{end}'
```

A chart from a working tree with changes is not a version anyone else can
reproduce, and an image without a digest can change under its tag. The
chart's version, its `appVersion` and the version of the images it names by
default are one number, that of the PubDex release
([`VERSIONING.md`](VERSIONING.md)).

## Troubleshooting

`kubectl logs` and `kubectl describe` name the cause in most cases. Of the
commands below, only `kubectl logs job/<name>` has run in a test (steps 16
and 19); the ways out of a kept database volume ran once outside the
suite.

- **`secrets.name is required`**, or another message from `helm install`
  that names a value: the schema or the chart refused the values; the
  message says which and why.
- **A pod stays in `CreateContainerConfigError` or `ContainerCreating`**,
  with an event about a Secret: the Secret `secrets.name` names does not
  exist in the namespace or lacks a key. Create it as in [Secrets](#secrets).
- **`ImagePullBackOff`**: the nodes cannot pull an image. Check their access
  to `ghcr.io`, or use a registry of your own ([What you need](#what-you-need)).
- **The PostgreSQL pod's claim stays `Pending`**: the cluster has no default
  StorageClass; name one in `postgresql.persistence.storageClass`.
- **PostgreSQL never becomes ready on its first start**: the initialisation
  failed. `kubectl --namespace pubdex logs pubdex-postgresql-0 --container
  postgresql` says why, and `--container bootstrap` shows the schema files
  copied from the image. A restart does not repeat the initialisation, so
  fix the cause, uninstall, delete the claim and install again.
- **`/readyz` 503 `database_unavailable` with PostgreSQL ready**: the read
  service's password and the database disagree. In evaluation mode, check
  first for a database volume kept from an earlier install of the same
  release name ([Uninstall](#uninstall)): PostgreSQL started on it with the
  passwords it was initialised with, not those the Secret holds. `helm
  install --wait` then fails at its timeout because `pubdex-api` is not
  ready, the release is `failed`, and the demonstration, if enabled, does
  not run. There are two ways out. If the data is disposable, uninstall,
  delete the claim and install again. Otherwise set the Secret's passwords
  in that database with the evaluation-mode block of [Changing a
  password](#changing-a-password) from its `for` loop on, with the files
  the Secret was created from in place of `secrets-new`; the loop's command
  runs in the PostgreSQL pod as `postgres` on the local socket, so it needs
  none of the old passwords. A `helm upgrade` with the same values files
  then records the release as deployed; the demonstration runs only after
  an install. Both ways ran once outside the suite, on kind with Kubernetes
  1.37, on 2026-11-15. In other cases, see [Changing a
  password](#changing-a-password). In external mode, the upgrade of a
  password change may have run without the setup, because it switched the
  setup off or used `--reuse-values` after a routine upgrade: run the
  upgrade with the setup, then restart the read service. With an external
  server, `pg_hba.conf` may also ask for another authentication than SCRAM,
  which the service refuses.
- **`/readyz` 503 `schema_upgrade_required`** after an upgrade: the schema
  upgrade did not run, as after a routine upgrade in external mode to images
  with schema changes; run the upgrade with the setup and the admin Secret
  ([Upgrading to a new release](#upgrading-to-a-new-release)). **`/readyz`
  503 `schema_not_supported`** after a rollback: the database has a schema
  change that is not backward compatible; restore the backup taken before
  the upgrade ([Rollback](#rollback)).
- **`/readyz` 503 `read_surface_unavailable`**: the snapshot is missing or
  unreadable for `app_readonly` ([`DB/README.md`](DB/README.md), section 3.2).
- **An external install or upgrade fails in its setup hook**: `kubectl
  --namespace pubdex logs job/pubdex-setup` names every failed check.
  Without the admin Secret, it says that
  `PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE` names a file that does not exist,
  and Helm fails the install or upgrade within seconds; a running release
  stays as it was. Create the Secret and repeat the command, or, for an
  upgrade that changes no password, run the routine upgrade
  ([Upgrade](#upgrade)) and delete the failed Job ([Installing with the
  setup Job](#installing-with-the-setup-job)). `authentication method
  requirement "scram-sha-256" failed` means that the server asked the
  admin role for another authentication than SCRAM, and the setup sent
  nothing in answer ([What the server provides](#what-the-server-provides)).
- **Another pod, or an ingress controller, gets no answer from the read
  service** while a port forward works: the NetworkPolicy does not admit
  it ([NetworkPolicy](#networkpolicy)).
- **`pods "pubdex-run" already exists`**: an earlier attached run was
  interrupted; delete that pod.
- **An empty publication list**: configure the attribution rules
  ([`DEPLOYMENT.md`](DEPLOYMENT.md), *Ingestion*), and refresh after an
  ingestion.

## Appendix: what ran these commands

One row per block of commands, in the order of this guide. *Ran by* says
what ran the block:

- **stage 9**, with its steps: [`run_cluster_acceptance.sh`](run_cluster_acceptance.sh)
  ran the block's commands. Steps 2 to 26 run on Kubernetes 1.37 as the
  cluster's administrator, steps 28 to 35 on Kubernetes 1.35, the floor:
  step 30 as the administrator, steps 31 to 35 as the namespace-scoped user
  of [What you need](#what-you-need). [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md),
  *The Helm chart on a disposable cluster*, describes each step;
- **stage 8 only**: the chart's offline tests render what the block
  installs, and no cluster has run it;
- **outside the suite**: a run of its own, on the date given;
- **nothing**: no test has run the block.

The last column says where a run differed from the block, and what it left
out. [`DB/tests/test_kubernetes_guide.py`](DB/tests/test_kubernetes_guide.py),
in stage 1, checks each row against its block and against the steps it
names: each kubectl and helm command of the block is one those steps run,
with the same long options.

In the upgrades of steps 6 to 21 and 32 to 34, revision 1 is the preceding
PubDex release, 0.1.0, as an operator has it installed: its chart,
`charts/pubdex/` at the commit its entry in the [release
record](releases/README.md) names, taken from the repository's history, and
its published images by digest. The upgrade goes to this chart and to
images built from this checkout. In steps 22 to 26, revision 1 is test
release A of this checkout, and the upgrade goes to test release B1, each
built by `release.sh` and installed from its chart package file, with its
images set by local tag. Each install or upgrade takes its external-mode
example out of that test release's package file.

| Section | Block | Ran by | Differences and limits |
|---|---|---|---|
| [The short path](#the-short-path) | `kubectl create namespace pubdex` | stage 9, steps 4, 14, 30 and 31 | step 14 created the Secret again, for the fresh install; on 1.35 the administrator created and labelled the namespace (step 30), and the namespace-scoped user created the Secret (step 31) |
| [The short path](#the-short-path) | `helm install pubdex charts/pubdex --namespace pubdex --values values-demo.yaml` | stage 9, steps 6, 14 and 32 | step 14 installed this chart with the images under test, and with `--wait-for-jobs`, so that Helm waited for the schema upgrade's Job before it started the demonstration, in about 15 to 17 seconds. Steps 6 and 32 installed the chart of the preceding PubDex release, 0.1.0, taken from the repository's history, with its published images by digest, as the release the upgrade starts from: in about 15 to 21 seconds on 1.37, with the images on the node, and about 30 on 1.35, where the kubelet pulled both published images first |
| [The short path](#the-short-path) | `kubectl --namespace pubdex port-forward` | stage 9, steps 7 and 32 | with `:8000` in place of `8000:8000`, so that kubectl chose a free local port |
| [The short path](#the-short-path) | `helm test pubdex --namespace pubdex --timeout 300s` | stage 9, steps 7 and 32 | Python's HTTP client read both paths instead of curl |
| [The short path](#the-short-path) | `kubectl --namespace pubdex delete pvc data-pubdex-postgresql-0 --wait=true` | stage 9, steps 13, 14 and 35 | |
| [Secrets and TLS](#secrets-and-tls) | `create configmap pubdex-db-ca` | stage 9, steps 16 and 22 | the CA and the server's certificate came from a CA the run made |
| [Installing with the setup Job](#installing-with-the-setup-job) | `helm install pubdex charts/pubdex --namespace pubdex --values charts/pubdex/examples/values-external.yaml` | stage 9, steps 16 and 22 | in step 16 with the chart of the preceding PubDex release, 0.1.0, taken from the repository's history, its own `examples/values-external.yaml` and its published application image by digest; with a non-superuser admin role on a PostgreSQL 16.15 server of the run's own, connected by TLS with `verify-full`, where that chart's setup applied the baseline. In step 22 this chart, packaged by `release.sh` as a test release of this checkout, installed from its package file, with the test release's two images set by local tag, on an empty database of the same server: its setup applied the baseline and its schema upgrade's hook this chart's schema changes after it, as the admin role, in about 12 seconds |
| [Installing with the setup Job](#installing-with-the-setup-job) | `delete job,configmap,serviceaccount` | stage 9, step 19 | after an upgrade that ran the setup without the admin Secret and failed, and the routine upgrade after it: only the failed Job was left, with its pod, and the block deleted both. Helm had deleted the hooks that ran before the setup's Job, the schema upgrade's ServiceAccount among them, and the schema upgrade's Job never started. No run has left a ConfigMap or ServiceAccount of the setup behind |
| [Installing without the setup Job](#installing-without-the-setup-job) | `docker volume create pubdex-initdb` | outside the suite, once, on 2026-11-16 | against a disposable PostgreSQL 16.15 server in Docker, prepared as [What the server provides](#what-the-server-provides) asks: an empty database owned by an admin role that has `CREATEROLE` and is no superuser, TLS from a CA of the run's own, checked by `verify-full`. The block ran twice: the first time it applied the schema; the second time it skipped the schema step, set the two roles' passwords again and left every object of the database as it was. The server authenticated every connection, the admin role's included, by SCRAM. The run's host name and directories replaced the placeholders, and the second `docker run` also got `--network` with the Docker network of the run, the only one that reached the server |
| [Installing without the setup Job](#installing-without-the-setup-job) | `python -m people_pubs.schema upgrade` | outside the suite, once, on 2026-11-17 | against a disposable PostgreSQL 16.15 server in Docker, prepared as for the block above, which ran first each time. The block ran twice: the first time it applied schema change 0001 to the baseline as the admin role, which is no superuser, and that role owned the new schema `pubdex`; the second time it found nothing outstanding and left every object of the database and the record of the first run as they were. Its connections used TLS, checked by `verify-full`, and SCRAM. A local build of this checkout's application image replaced `ghcr.io/ccu-ukn/pubdex:0.2.0`, the run's host name and directories the placeholders, and `--network` the Docker network of the run, as above |
| [Installing without the setup Job](#installing-without-the-setup-job) | `--values my-values.yaml --set externalDatabase.setup.enabled=false` | stage 8 only, [`test_render_external.py`](charts/tests/test_render_external.py) | no cluster has installed a release without the setup; steps 19 and 23 switch it off for an upgrade only |
| [Changing a password](#changing-a-password), evaluation mode | `020_role_passwords.sh --stdin` | stage 9, step 10 | wrote the three lines to a file of mode 600 and gave that file on standard input instead of a pipe |
| [Changing a password](#changing-a-password), external mode | `create secret generic pubdex-db-admin` | stage 9, step 18 | the API server's record of created objects showed that the setup ran, and the schema upgrade's hook after it, which found nothing outstanding |
| [Ingestion](#ingestion) | `create job --from=cronjob/pubdex-ingest-refresh` | stage 9, steps 16, 22, 24 and 25 | with the tasks `fixture` and `rules`: in step 16 with the chart and image of PubDex 0.1.0, revision 1 of the upgrade, and in step 22 with this chart and the image of a test release of this checkout; in steps 24 and 25 the task `fixture` alone ingested the demonstration again after an upgrade to another test release and after the rollback, and every row then carried the transform version of the image that ran. Once each job's pod had started, `kubectl logs --follow`, which `helm get notes` prints, followed its log to the end and printed what `kubectl logs` printed afterwards |
| [Ingestion](#ingestion) | `people_pubs.sync.export_publications` | stage 9, steps 16 and 22 | in external mode, after the demonstration, with the chart and image of PubDex 0.1.0 (step 16) and with this chart and a test release's image (step 22): standard output held the CSV with the demonstration's publication, and standard error the export's log |
| [Importing a roster](#importing-a-roster) | `people_pubs.sync.add_people --csv -` | stage 9, steps 16 and 22 | in external mode, with the chart and image of PubDex 0.1.0 (step 16) and with this chart and a test release's image (step 22), and the synthetic roster and `--no-orcid-lookup`, which keeps `add_people` from asking ORCID; without that option, and in evaluation mode, it has not run |
| [Ingress and TLS](#ingress-and-tls) | `create secret tls pubdex-tls` | stage 8 only, [`test_render_network.py`](charts/tests/test_render_network.py) | neither command has run. Stage 8 lints both examples for Kubernetes 1.35 and 1.37 and renders them; stage 9, steps 5 and 31, has the API servers of 1.37 and 1.35 admit what each renders, the Ingress included, in a server-side dry run, which installs nothing. No ingress controller or certificate manager has run, and no request has passed through an Ingress |
| [NetworkPolicy](#networkpolicy) | `label pod <pod> pubdex-client=true` | stage 9, step 12 | on a running pod without labels, which had failed to reach the read service; its next attempt after the label reached it |
| [Upgrade](#upgrade) | `--values values-demo.yaml --values my-values.yaml` | stage 9, steps 8, 9 and 33 | from the chart and published images of PubDex 0.1.0, the preceding release, to this chart, with `--wait-for-jobs`, so that Helm waited for the schema upgrade's Job: the second file gave the images under test, a log level and two read service replicas; the Job applied this chart's schema changes, which the database then recorded, every pod was replaced, the data stayed, and PostgreSQL started on its volume without initialising it again. Step 9 ran the upgrade again after the rollback, with the images under test but not the log level and replicas, and its schema upgrade found nothing outstanding |
| [Upgrade](#upgrade), routine | `--set externalDatabase.setup.enabled=false --wait` | stage 9, steps 19 and 23 | without the admin Secret, after the API server had answered that it does not exist. The same upgrade without the switch ran first: its setup failed after 6 seconds, against the timeout of 300, `helm status` reported that revision `failed` and `helm history` the previous one still `deployed`, and the read service served on with its pods unchanged. Then the values changed the log level only, every read service pod was replaced, and the API server's record of created objects showed that neither the setup nor the schema upgrade ran. In step 23 the routine upgrade went to other images, those of a test release whose one schema change was outstanding, with `--timeout 30s`: it failed after 31 seconds with `UPGRADE FAILED: resource Deployment/pubdex-external/pubdex-api not ready. status: InProgress, message: Pending termination: 1`, and `helm status` reported that revision `failed`. Neither the setup nor the schema upgrade ran. The new read service pod ran but stayed not ready: its readiness probe failed with HTTP 503, and its `/readyz` answered with the reason `schema_upgrade_required` and a message naming the missing schema change and `python -m people_pubs.schema upgrade`. The previous pod stayed ready and served the publication |
| [Upgrade](#upgrade), with the setup | `kubectl --namespace pubdex create secret generic pubdex-db-admin` | stage 9, steps 17, 18 and 24 | from the chart and image of PubDex 0.1.0 to this chart and the images under test with other values (step 17), where the schema upgrade's hook applied this chart's schema changes as the admin role after the setup, and for a password change (step 18), where it found nothing outstanding; the schema's objects kept their ids on the server. In step 24 the same upgrade, after the routine upgrade of step 23 had failed, went to that test release: its schema upgrade's hook applied the outstanding schema change, and the read service pod that had stayed not ready turned ready without being replaced, in about 20 seconds |
| [Upgrading to a new release](#upgrading-to-a-new-release) | `helm upgrade pubdex pubdex-<version>.tgz --namespace pubdex --values values-demo.yaml` | stage 9, steps 8, 9 and 33 | with this checkout's chart directory, `charts/pubdex`, in place of a package file, as in the row of [Upgrade](#upgrade) above: from the chart and published images of PubDex 0.1.0, the preceding release, to this chart and the images under test, where the schema upgrade's Job applied this chart's schema changes and every pod was replaced. No evaluation-mode step has installed or upgraded from a package file; external mode did (steps 22 to 24) |
| [Upgrading to a new release](#upgrading-to-a-new-release) | `helm upgrade pubdex pubdex-<version>.tgz --namespace pubdex` | stage 9, steps 17 and 24 | in step 24 from test release A to the package file of test release B1, after the routine upgrade of step 23 had failed: `tar -xzf` took `pubdex/examples/values-external.yaml` out of B1's package before Helm used it; step 22 took A's example out of A's package for the install. The setup ran, then the schema upgrade's hook applied B1's backward-compatible schema change as the admin role, and the read service pod that had stayed not ready turned ready without being replaced. In step 17 from the chart and image of PubDex 0.1.0 to this checkout's chart directory, with its checkout's example, where the hook applied this chart's schema changes. The images were set by local tag in the test releases' own values files |
| [Upgrading to a new release](#upgrading-to-a-new-release) | `helm status pubdex --namespace pubdex` | stage 9, step 23 | in namespace `pubdex-external`, after the routine upgrade to test release B1, with `--timeout 30s`, had failed after 31 seconds with `UPGRADE FAILED: resource Deployment/pubdex-external/pubdex-api not ready. status: InProgress, message: Pending termination: 1`: `helm status` reported revision 2 `failed`, and the list of pods showed the previous pod `1/1 Running` and the new one `0/1 Running`, without restarts. The run also read `helm history`, the new pod's events (`Readiness probe failed: HTTP probe failed with statuscode: 503`), its `/readyz` and `/v0/publications` through a port forward, and its log |
| [Rollback](#rollback) | `helm rollback pubdex 1` | stage 9, steps 9, 20, 25 and 34 | back to the chart and published images of PubDex 0.1.0: in evaluation mode after the upgrade above, where the rollback became revision 3 and the data and passwords stayed (steps 9 and 34); in external mode after a password change and a routine upgrade, without the admin Secret, where no setup hook ran (step 20). Each time this chart's schema changes and their record stayed, and 0.1.0 served the publication on that schema. In step 25 an external release rolled back from one test release of this checkout to another, an image rollback after a backward-compatible schema change, without the admin Secret: the schema and its record stayed, the older images served the publication, and their ingestion wrote on that schema |
| [Uninstall](#uninstall) | `helm uninstall pubdex --namespace pubdex --wait --timeout 300s` | stage 9, steps 13, 14, 21, 26 and 35 | this chart in evaluation mode (steps 13 and 14); the chart of PubDex 0.1.0, after the rollback, in external mode without the admin Secret, where nothing was created (step 21); this chart as a test release in external mode, after a rollback, without the admin Secret (step 26); and on 1.35 (step 35) |
| [Backup and restore](#backup-and-restore) | `pg_dump -U postgres -Fc people_db` | nothing | stage 7 runs the same `pg_dump` on the Compose deployment |
| [Backup and restore](#backup-and-restore) | `pg_restore -U postgres -d people_db` | nothing | stage 7 runs the same dump and restore on the Compose deployment |
| [What identifies a deployment](#what-identifies-a-deployment) | `helm history pubdex --namespace pubdex` | stage 9, steps 9 and 18 | |
| [What identifies a deployment](#what-identifies-a-deployment) | `helm get values pubdex --namespace pubdex --all` | stage 9, step 9 | after the rollback to revision 1: the values showed revision 1's, both published images by digest among them. The pods' image ids were read with a Go template in place of the block's `jsonpath` |
