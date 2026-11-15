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

The chart lives in this repository only: you install it from a clone or
from the release source, not from a chart repository. Its version is 0.1.0,
and its `appVersion`, 0.1.0, is the application release whose published
images it uses by default.

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

Install evaluation mode with the synthetic demonstration, which writes
synthetic records, so use this release for trying PubDex out only:

```sh
cat > values-demo.yaml <<'YAML'
secrets:
  name: pubdex-credentials
demo:
  enabled: true
YAML
helm install pubdex charts/pubdex --namespace pubdex --values values-demo.yaml --wait --timeout 300s
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
  Kubernetes project maintained on 2026-11-10, with `linux/amd64` nodes.
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
- **The images.** The nodes pull `ghcr.io/ccu-ukn/pubdex:0.1.0` and
  `ghcr.io/ccu-ukn/pubdex-postgres:0.1.0` by digest, without credentials.
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
  (steps 25 to 29), and the API server's audit log shows no refused
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
| Kubernetes 1.35, the floor | **supported** | Kubernetes 1.35.8 on kind 0.33.0, node image `kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0`: evaluation mode's lifecycle (steps 22 to 29) as a namespace-scoped user holding only the rights of [What you need](#what-you-need), with the kubelet pulling the published images from `ghcr.io`. External mode, the password change, the loss of the database and the NetworkPolicy ran on 1.37 only | 2026-11-12 |
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

The table states what was established on its dates. The supported
Kubernetes releases are established again at each PubDex release: the kind
node images of the newest Kubernetes release and of the oldest one the
Kubernetes project maintains on that date, the floor, are pinned again in
[`DB/lib/cluster_tools.sh`](DB/lib/cluster_tools.sh), and stage 9 runs on
both. Between two PubDex releases the table stands as dated. `kubeVersion`
has no upper bound, so Helm installs the chart on a newer Kubernetes, whose
status is *not known*. It refuses, rather than warns about, a release older
than the floor: stage 9 of this PubDex release did not run on it, and the
Kubernetes project no longer maintains it.

Kubernetes 1.35 reaches its end of life on 2027-02-28, and 1.38 is planned
for 2026-12-16, by the Kubernetes project's release information as read on
2026-11-10.

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
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml --wait --timeout 300s
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
`scram-sha-256`. Then install with the setup turned off:

```sh
helm install pubdex charts/pubdex --namespace pubdex \
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml \
  --set externalDatabase.setup.enabled=false --wait --timeout 300s
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
| `image.registry`, `image.repository`, `image.tag`, `image.digest`, `image.pullPolicy` | the published 0.1.0 application image by digest, `IfNotPresent` | [What identifies a deployment](#what-identifies-a-deployment) |
| `postgresImage.registry`, `postgresImage.repository`, `postgresImage.tag`, `postgresImage.digest`, `postgresImage.pullPolicy` | the published 0.1.0 PostgreSQL image by digest | the same |
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
Stage 9 (steps 5 and 25) renders [`charts/tests/values/evaluation.yaml`](charts/tests/values/evaluation.yaml),
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
checks, took less than 20 seconds (steps 10 and 17); the outage itself was
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
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml --wait --timeout 300s
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
[The short path](#the-short-path) (step 6).

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
helm install pubdex charts/pubdex --namespace pubdex --values charts/pubdex/examples/values-ingress-tls.yaml
```

**The examples are admitted, never served.** Stage 8 lints both for
Kubernetes 1.35 and 1.37, renders them, and checks that the Ingress leads
every host and path to the read service and that the warnings are there.
Stage 9 (steps 5 and 25) renders each and has the API servers of
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
| read service, `<release>-api` | startup and liveness: `GET /livez`; readiness: `GET /readyz` every 10 seconds, with a timeout of `api.dbWaitSeconds` plus 2 | a database connection within `api.dbWaitSeconds` and the snapshot readable |

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

`helm upgrade` changes a release to another chart, other values or other
images:

```sh
helm upgrade pubdex charts/pubdex --namespace pubdex --values values-demo.yaml --values my-values.yaml --wait --timeout 300s
```

**External mode.** The setup Job runs before an upgrade unless the upgrade
switches it off, and it needs the admin Secret, which you deleted after the
install. Only two things need the setup: the first install and a password
change. Every other upgrade is a *routine upgrade*: it changes no password
and runs without the setup, so the admin Secret need not exist. Give the
release's values files and switch the setup off for this upgrade:

```sh
helm upgrade pubdex charts/pubdex --namespace pubdex \
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml \
  --set externalDatabase.setup.enabled=false --wait --timeout 300s
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
password](#changing-a-password)), needs the admin Secret for that run.
Create it first and delete it afterwards:

```sh
kubectl --namespace pubdex create secret generic pubdex-db-admin --from-file=password=admin_password
helm upgrade pubdex charts/pubdex --namespace pubdex \
  --values charts/pubdex/examples/values-external.yaml --values my-values.yaml --wait --timeout 300s
kubectl --namespace pubdex delete secret pubdex-db-admin
```

To run other images, give their registry, repository, tag and digest in
`image` and `postgresImage`. **The schema does not change on upgrade.**
PostgreSQL in evaluation mode runs the image's `DB/init/` only on an empty
volume, and the setup of external mode applies it only to a database
without the PubDex schema; the chart has no migrations. An upgrade is
therefore safe only to images of the same schema: the same application
release, rebuilt from its source or copied to another registry. **An
upgrade from one PubDex release to the next is not supported yet**; it
needs a migration path that this chart does not provide.
`postgresql.persistence` cannot change after the install, because
Kubernetes refuses changes to a StatefulSet's volume claim template; this
is untested.

## Rollback

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

- **the schema**: a rollback never changes the database's schema, in either
  mode. It stays the schema of the image that initialised the database
  ([Upgrade](#upgrade)), so roll back only to images of that schema;
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
  20). Drop them on the server if you want them gone.
- **The Secrets and the ConfigMap you created**: `kubectl --namespace pubdex
  delete secret pubdex-credentials`, and in external mode `kubectl
  --namespace pubdex delete configmap pubdex-db-ca` (steps 13 and 20).
- **Objects of a failed setup or demonstration**, as Helm does not track
  hook objects: see [Installing with the setup Job](#installing-with-the-setup-job)
  and [The demonstration](#the-demonstration).

## Backup and restore

Neither has been tested on Kubernetes, and this section promises nothing
beyond what the commands say. A backup is a dump in PostgreSQL's custom
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
| the chart | the commit of the clone it was installed from; `helm history` shows its name and version, `pubdex-0.1.0`, and its `appVersion` |
| the values | `helm get values <release> --all` |
| the application and PostgreSQL images | their digests: the chart's defaults name the published 0.1.0 images by digest, and each pod's `imageID` names the image that runs |
| the schema | the image that initialised the database ([Upgrade](#upgrade)) |

```sh
helm history pubdex --namespace pubdex
```

```sh
helm get values pubdex --namespace pubdex --all
kubectl --namespace pubdex get pods --output 'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[*].imageID}{"\n"}{end}'
```

A chart from a working tree with changes is not a version anyone else can
reproduce, and an image without a digest can change under its tag. The
chart's own version and its `appVersion` change independently: the chart
when it changes, `appVersion` when it defaults to another application
release.

## Troubleshooting

`kubectl logs` and `kubectl describe` name the cause in most cases. Of the
commands below, only `kubectl logs job/<name>` has run in a test (steps 15
and 18); the ways out of a kept database volume ran once outside the
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
  ran the block's commands. Steps 2 to 20 run on Kubernetes 1.37 as the
  cluster's administrator, steps 22 to 29 on Kubernetes 1.35, the floor:
  step 24 as the administrator, steps 25 to 29 as the namespace-scoped user
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

| Section | Block | Ran by | Differences and limits |
|---|---|---|---|
| [The short path](#the-short-path) | `kubectl create namespace pubdex` | stage 9, steps 4, 24 and 25 | on 1.35 the administrator created and labelled the namespace (step 24), and the namespace-scoped user created the Secret (step 25) |
| [The short path](#the-short-path) | `helm install pubdex charts/pubdex --namespace pubdex --values values-demo.yaml` | stage 9, steps 6 and 26 | the install took about 20 seconds on 1.37, with the images on the node, and about 30 on 1.35, where the kubelet pulled both published images first |
| [The short path](#the-short-path) | `kubectl --namespace pubdex port-forward` | stage 9, steps 7 and 26 | with `:8000` in place of `8000:8000`, so that kubectl chose a free local port |
| [The short path](#the-short-path) | `helm test pubdex --namespace pubdex --timeout 300s` | stage 9, steps 7 and 26 | Python's HTTP client read both paths instead of curl |
| [The short path](#the-short-path) | `kubectl --namespace pubdex delete pvc data-pubdex-postgresql-0 --wait=true` | stage 9, steps 13 and 29 | |
| [Secrets and TLS](#secrets-and-tls) | `create configmap pubdex-db-ca` | stage 9, step 15 | the CA and the server's certificate came from a CA the run made |
| [Installing with the setup Job](#installing-with-the-setup-job) | `helm install pubdex charts/pubdex --namespace pubdex --values charts/pubdex/examples/values-external.yaml` | stage 9, step 15 | with a non-superuser admin role on a PostgreSQL 16.15 server of the run's own, connected by TLS with `verify-full`; the setup applied the schema |
| [Installing with the setup Job](#installing-with-the-setup-job) | `delete job,configmap,serviceaccount` | stage 9, step 18 | after an upgrade that ran the setup without the admin Secret and failed, and the routine upgrade after it: only the failed Job was left, with its pod, and the block deleted both. No run has left a ConfigMap or ServiceAccount of the setup behind |
| [Installing without the setup Job](#installing-without-the-setup-job) | `docker volume create pubdex-initdb` | outside the suite, once, on 2026-11-16 | against a disposable PostgreSQL 16.15 server in Docker, prepared as [What the server provides](#what-the-server-provides) asks: an empty database owned by an admin role that has `CREATEROLE` and is no superuser, TLS from a CA of the run's own, checked by `verify-full`. The block ran twice: the first time it applied the schema; the second time it skipped the schema step, set the two roles' passwords again and left every object of the database as it was. The server authenticated every connection, the admin role's included, by SCRAM. The run's host name and directories replaced the placeholders, and the second `docker run` also got `--network` with the Docker network of the run, the only one that reached the server |
| [Installing without the setup Job](#installing-without-the-setup-job) | `--values my-values.yaml --set externalDatabase.setup.enabled=false` | stage 8 only, [`test_render_external.py`](charts/tests/test_render_external.py) | no cluster has installed a release without the setup; step 18 switches it off for an upgrade only |
| [Changing a password](#changing-a-password), evaluation mode | `020_role_passwords.sh --stdin` | stage 9, step 10 | wrote the three lines to a file of mode 600 and gave that file on standard input instead of a pipe |
| [Changing a password](#changing-a-password), external mode | `create secret generic pubdex-db-admin` | stage 9, step 17 | the API server's record of created objects showed that the setup ran |
| [Ingestion](#ingestion) | `create job --from=cronjob/pubdex-ingest-refresh` | stage 9, step 15 | with the tasks `fixture` and `rules`. Once each job's pod had started, `kubectl logs --follow`, which `helm get notes` prints, followed its log to the end and printed what `kubectl logs` printed afterwards |
| [Ingestion](#ingestion) | `people_pubs.sync.export_publications` | stage 9, step 15 | in external mode, after the demonstration: standard output held the CSV with the demonstration's publication, and standard error the export's log |
| [Importing a roster](#importing-a-roster) | `people_pubs.sync.add_people --csv -` | stage 9, step 15 | in external mode, with the synthetic roster and `--no-orcid-lookup`, which keeps `add_people` from asking ORCID; without that option, and in evaluation mode, it has not run |
| [Ingress and TLS](#ingress-and-tls) | `create secret tls pubdex-tls` | stage 8 only, [`test_render_network.py`](charts/tests/test_render_network.py) | neither command has run. Stage 8 lints both examples for Kubernetes 1.35 and 1.37 and renders them; stage 9, steps 5 and 25, has the API servers of 1.37 and 1.35 admit what each renders, the Ingress included, in a server-side dry run, which installs nothing. No ingress controller or certificate manager has run, and no request has passed through an Ingress |
| [NetworkPolicy](#networkpolicy) | `label pod <pod> pubdex-client=true` | stage 9, step 12 | on a running pod without labels, which had failed to reach the read service; its next attempt after the label reached it |
| [Upgrade](#upgrade) | `--values values-demo.yaml --values my-values.yaml` | stage 9, steps 8 and 27 | the second file gave other images, a log level and two read service replicas; every pod was replaced, the data stayed, and PostgreSQL started on its volume without initialising it again |
| [Upgrade](#upgrade), routine | `--set externalDatabase.setup.enabled=false --wait` | stage 9, step 18 | without the admin Secret, after the API server had answered that it does not exist. The same upgrade without the switch ran first: its setup failed after 5 seconds, against the timeout of 300, `helm status` reported that revision `failed` and `helm history` the previous one still `deployed`, and the read service served on with its pods unchanged. Then the values changed the log level only, every read service pod was replaced, and the API server's record of created objects showed that no setup ran. A routine upgrade to other images has not run; the rollback of step 19 changed the images without the setup |
| [Upgrade](#upgrade), with the setup | `kubectl --namespace pubdex create secret generic pubdex-db-admin` | stage 9, steps 16 and 17 | to the images under test with other values (step 16), and for a password change (step 17); the schema's objects kept their ids on the server |
| [Rollback](#rollback) | `helm rollback pubdex 1` | stage 9, steps 9, 19 and 28 | in evaluation mode after the upgrade above, where the rollback became revision 3, the published images ran again, and the data and passwords stayed (steps 9 and 28); in external mode after a password change and a routine upgrade, without the admin Secret, where no setup hook ran (step 19) |
| [Uninstall](#uninstall) | `helm uninstall pubdex --namespace pubdex --wait --timeout 300s` | stage 9, steps 13, 20 and 29 | in external mode without the admin Secret, and nothing was created (step 20) |
| [Backup and restore](#backup-and-restore) | `pg_dump -U postgres -Fc people_db` | nothing | stage 7 runs the same `pg_dump` on the Compose deployment |
| [Backup and restore](#backup-and-restore) | `pg_restore -U postgres -d people_db` | nothing | stage 7 runs the same dump and restore on the Compose deployment |
| [What identifies a deployment](#what-identifies-a-deployment) | `helm history pubdex --namespace pubdex` | stage 9, steps 9 and 17 | |
| [What identifies a deployment](#what-identifies-a-deployment) | `helm get values pubdex --namespace pubdex --all` | stage 9, step 9 | after the rollback to revision 1: the values showed revision 1's, both published images by digest among them. The pods' image ids were read with a Go template in place of the block's `jsonpath` |
