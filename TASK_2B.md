# Task 2b — Helm chart and cluster tests

**Chart version 0.1.0, in this repository since 2026-11-17. The
clean-clone suite and public CI passed.**

The result is commit
[`a86d5e04a8a41795d476c5d1655bbee5b05150a7`](https://github.com/CCU-UKN/pubdex/commit/a86d5e04a8a41795d476c5d1655bbee5b05150a7),
tree `a51189cfc6877d64731d8f801f31f1488d629e0a`. Its complete suite passed
all nine stages from a clean clone locally on 2026-11-16 (*Local
verification record*) and in
[public CI](https://github.com/CCU-UKN/pubdex/actions/runs/43989083559) on
2026-11-17, after a pull-request run on the same tree had passed
(*Public-CI evidence*). Stage 9 ran the chart in both modes on Kubernetes
1.37, and evaluation mode's lifecycle on Kubernetes 1.35, the oldest
supported release, as a user holding only the rights the guide lists. This
record was updated after those runs; the results belong to the commit named
here. No chart package is published, and no image changed: by default the
chart runs the published 0.1.0 images of task 2a by digest. External
acceptance of task 2b is not asserted.

Project page: [PubDex at NLnet](https://nlnet.nl/project/PubDex/). The
earlier funded results are recorded in [`TASK_1A.md`](TASK_1A.md),
[`TASK_1B.md`](TASK_1B.md) and [`TASK_2A.md`](TASK_2A.md).

## The agreed task

> We'll provide a Helm chart for both a self-contained evaluation
> deployment and an installation with external PostgreSQL. It will include
> documented values, Secret and ConfigMap patterns, ingress and TLS
> examples, and instructions for install, upgrade, rollback and uninstall.
> Public CI will lint and test the templates and run an install-and-upgrade
> smoke test on an ephemeral cluster.

[`TASK_1B.md`](TASK_1B.md) left the Helm tests to this task.

Seven states are kept apart, and each row below says which one it concerns:

- **repository implementation** — the chart, the guide and the checks, in
  this tree;
- **local suite** — the nine stages passed on the commit's tree before it
  was committed, on 2026-11-16, on one Linux x86_64 machine;
- **clean-clone suite** — the nine stages passed from a clean clone of
  commit `a86d5e0` on 2026-11-16, on the same machine;
- **self-hosted Forgejo** — not run for this result;
- **public CI** — the `DB tests` workflow passed all nine stages on
  2026-11-17: in a pull-request run on GitHub's test merge, whose tree is
  the commit's, and in the push run on `main` on commit `a86d5e0` itself;
- **published chart or images** — no chart package is published (it is
  provided in the repository only); the images are the 0.1.0 images
  published for task 2a, unchanged;
- **external acceptance** — not asserted here.

## What task 2b adds

- **The chart** [`charts/pubdex/`](charts/pubdex/), chart version 0.1.0,
  `appVersion` 0.1.0, with two modes. `evaluation` also runs PostgreSQL 16
  as a single-replica StatefulSet with a volume of its own, and can run the
  synthetic demonstration as a `post-install` hook; it is for trying PubDex
  out with synthetic or disposable data. `external` uses a PostgreSQL 16
  server the operator runs, prepared by a `pre-install,pre-upgrade` hook
  Job ([`files/external-setup.sh`](charts/pubdex/files/external-setup.sh))
  with an admin Secret that no other pod mounts, or by the operator running
  the same script once. External mode connects with `sslmode` `verify-full`
  by default and needs the server's CA certificate; the setup lets the
  server authenticate every connection, the admin role's included, by SCRAM
  only. A routine upgrade, one
  that changes no password, turns the setup off and needs no admin Secret.
  Every pod meets Pod Security `restricted`.
- **Secrets and settings.** The chart takes and generates no password. The
  operator creates one Secret from the files of
  [`create_secrets.sh`](create_secrets.sh), and each container mounts only
  its own keys. Non-secret settings are in a ConfigMap whose checksum rolls
  the read service when a setting changes.
- **Scheduling and pod metadata.** `nodeSelector`, `tolerations`,
  `affinity`, `podAnnotations`, `podLabels` and `priorityClassName` apply
  to every pod; under `api` and `postgresql` each replaces the top-level
  value for that component's pods. Labels and annotations the chart sets
  itself are refused.
- **Ingestion** as CronJobs, suspended unless given a schedule and runnable
  on demand; a roster reaches a one-off pod on standard input only.
- **Network.** An Ingress, off by default, with two TLS examples in
  [`charts/pubdex/examples/`](charts/pubdex/examples/); NetworkPolicies, on
  by default; `helm test` against the read service.
- **The operator guide** [`KUBERNETES.md`](KUBERNETES.md). Its table of
  supported platforms says how each row was established, and its appendix
  names, for every block of commands, the stage 9 steps that ran it, or
  says that only stage 8 rendered it, that it ran once outside the suite,
  or that nothing ran it. The guide states the rule by which the supported
  Kubernetes releases move; the table's dates are those of 2026-11-12, and
  the end-of-life and next-release dates were checked against the
  Kubernetes project's release pages on 2026-11-15.
- **Two stages of the complete suite**: stage 8,
  [`run_chart_checks.sh`](run_chart_checks.sh), and stage 9,
  [`run_cluster_acceptance.sh`](run_cluster_acceptance.sh), with the pinned
  tools of [`setup_cluster_tools.sh`](setup_cluster_tools.sh) (helm 4.3.1,
  kubectl 1.36.6 and kind 0.33.0 by SHA-256, the kind node images for
  Kubernetes 1.37.0 and 1.35.8 by digest, in
  [`DB/lib/cluster_tools.sh`](DB/lib/cluster_tools.sh)). Both workflows
  install the tools and call [`run_ci_suite.sh`](run_ci_suite.sh), as
  before, and give the job 40 minutes.

## The short path

To verify, from a clean clone on Linux x86_64 with Docker:

```bash
./setup_venv.sh
./setup_cluster_tools.sh
./run_ci_suite.sh                 # all nine stages; or only the chart's:
./run_chart_checks.sh             # stage 8, offline
./run_cluster_acceptance.sh       # stage 9, kind clusters of its own
```

`./run_ci_suite.sh` ends with `PASS: all 9 stages of the complete
verification suite passed in <n>s.` To install the chart on a cluster, follow
*The short path* in [`KUBERNETES.md`](KUBERNETES.md).

## Acceptance matrix

| Required | Where it lives | How to verify | Expected result | State |
|---|---|---|---|---|
| a Helm chart for … a self-contained evaluation deployment | [`charts/pubdex/`](charts/pubdex/), `mode: evaluation`; [`KUBERNETES.md`](KUBERNETES.md), *The short path*, *Values*, *Health and readiness* | stage 8 renders and checks every values file; stage 9, steps 4–13 on Kubernetes 1.37 and steps 24–29 on 1.35 | PostgreSQL and the read service in one release, restricted pods, the demonstration's publication served over HTTP, `helm test` passing; on 1.35 as a namespace-scoped user, with the kubelet pulling the published images | verified locally, Kubernetes 1.37.0 and 1.35.8 on kind |
| … and an installation with external PostgreSQL | `mode: external`, [`files/external-setup.sh`](charts/pubdex/files/external-setup.sh), [`examples/values-external.yaml`](charts/pubdex/examples/values-external.yaml); [`KUBERNETES.md`](KUBERNETES.md), *Installing with external PostgreSQL* | stage 8, external-mode tests; stage 9, steps 14–20 | no PostgreSQL rendered; the setup Job prepares an empty database with an admin role that has `CREATEROLE` but is no superuser, checks the server over TLS (`verify-full`, the default), answers only its SCRAM requests and is the only pod with the admin Secret; install, roster import, upgrade, password change, then a routine upgrade and a rollback without the admin Secret and without a setup run, uninstall | verified locally, Kubernetes 1.37.0, against a PostgreSQL 16.15 server in the same cluster; the manual path without the setup Job ran once outside the suite, on 2026-11-16, against a disposable PostgreSQL 16.15 server in Docker: on an empty database it applied the schema, and a second run skipped the schema step and left the database's objects unchanged; the setup's refusal of every other authentication than SCRAM ran outside the suite on the same day (*The acceptance tests*) |
| documented values | [`values.yaml`](charts/pubdex/values.yaml), [`values.schema.json`](charts/pubdex/values.schema.json); [`KUBERNETES.md`](KUBERNETES.md), *Values* | stage 8: `helm lint --strict`, refused values; [`DB/tests/test_kubernetes_guide.py`](DB/tests/test_kubernetes_guide.py) checks that the guide's table names every key | every value documented with its default; unknown keys, wrong forms and keys named like a password refused | verified locally |
| Secret … patterns | one operator Secret with per-container keys, an admin Secret for the setup Job; [`KUBERNETES.md`](KUBERNETES.md), *Secrets*, *Changing a password*, *Secrets and TLS* | stage 8, credentials tests; stage 9, steps 4, 10, 15, 17–19 and 25 | no Secret and no password rendered or in Helm's history; each container only its own keys, mode 0440; changed passwords applied and checked by new logins; the admin Secret only where the setup runs | verified locally |
| ConfigMap patterns | the settings ConfigMap and its checksum annotation; `<release>-run` for one-off pods; [`KUBERNETES.md`](KUBERNETES.md), *Settings and the ConfigMap*, *Ingestion* | stage 8, values and ingestion tests; stage 9, steps 8, 15, 16 and 18 | settings from the values, pods rolled on a change, no credential in a ConfigMap | verified locally |
| ingress and TLS examples | [`templates/ingress.yaml`](charts/pubdex/templates/ingress.yaml), [`examples/values-ingress-tls.yaml`](charts/pubdex/examples/values-ingress-tls.yaml), [`examples/values-ingress-cert-manager.yaml`](charts/pubdex/examples/values-ingress-cert-manager.yaml); [`KUBERNETES.md`](KUBERNETES.md), *Ingress and TLS* | stage 8 lints and renders both examples; stage 9, steps 5 and 25, server-side dry runs | every host and path to the read service, TLS as configured, a warning that the service has no authentication | admitted, never served: stage 9 (steps 5 and 25) has the API servers of Kubernetes 1.37 and 1.35 admit every object both examples render but `helm test`'s pod, the Ingress and the NetworkPolicy included, in a server-side dry run, on 1.35 as the namespace-scoped operator; no ingress controller or certificate manager has run them |
| instructions for install | [`KUBERNETES.md`](KUBERNETES.md), *The short path*, *Installing with the setup Job*, *Installing without the setup Job*, and its appendix | stage 9, steps 4–7, 14–15 and 24–26; the appendix's rows | the commands as written install a working release, on 1.35 with only the listed rights | verified locally for the blocks the appendix assigns to stage 9 |
| … upgrade | [`KUBERNETES.md`](KUBERNETES.md), *Upgrade* | stage 9, steps 8, 16–18 and 27 | from the published images to images built from the checkout, with changed values; data and schema kept; a routine upgrade runs no setup and needs no admin Secret; an upgrade that leaves the setup on without the admin Secret fails within seconds and leaves the deployed release as it was | verified locally |
| … rollback | [`KUBERNETES.md`](KUBERNETES.md), *Rollback* | stage 9, steps 9, 19 and 28 | back to revision 1 and the published images, the publication still served; in external mode no setup hook runs | verified locally, in both modes |
| … uninstall | [`KUBERNETES.md`](KUBERNETES.md), *Uninstall* | stage 9, steps 13, 20 and 29 | the release removed; what remains (the volume claim, the server's database) as the guide says | verified locally; an evaluation-mode reinstall onto a kept volume with other passwords ran once outside the suite (*The acceptance tests*) |
| Public CI will lint and test the templates | stage 8, [`run_chart_checks.sh`](run_chart_checks.sh), [`charts/tests/`](charts/tests/); the workflows [`.github/workflows/db-tests.yml`](.github/workflows/db-tests.yml) and [`.forgejo/workflows/db-tests.yml`](.forgejo/workflows/db-tests.yml) | `./run_chart_checks.sh`; public CI of the published commit | `PASS: the Helm chart's offline checks passed.` | **passed 2026-11-17** — in public CI on commit `a86d5e0`, after passing locally and from a clean clone of it (*Public-CI evidence*) |
| … and run an install-and-upgrade smoke test on an ephemeral cluster | stage 9, [`run_cluster_acceptance.sh`](run_cluster_acceptance.sh) | `./run_cluster_acceptance.sh`; public CI of the published commit | `PASS: the cluster acceptance test passed in <n>s.`, with the cleanup confirmed | **passed 2026-11-17** — in public CI on commit `a86d5e0`, on kind clusters on GitHub's hosted runner, after passing locally and from a clean clone of it (*Public-CI evidence*) |
| Helm tests (left to this task by task 1b) | [`templates/tests/test-readiness.yaml`](charts/pubdex/templates/tests/test-readiness.yaml) | stage 9, steps 7, 8, 15, 16, 18, 19, 26 and 27 (`helm test`) | `/readyz` and the first page of publications from a restricted pod | verified locally |

## The acceptance tests

**Stage 8** ([`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md), *The Helm
chart's offline checks*) needs no cluster, Docker or network. It runs
`helm lint --strict` for Kubernetes 1.35.0 and 1.37.0 on the chart's own
values, each test values file and each example. Then pytest renders every
case and checks Pod Security, credentials, object references, values and
pinned images, scheduling and pod metadata, external mode, ingestion and
the roster path, the network objects, and the refused values.

**Stage 9** (*The Helm chart on a disposable cluster*) creates two kind
clusters, one after the other, each on a Docker network of its own and with
its own kubeconfig, which it checks before any kubectl or helm call. It
never reads another kubeconfig, and every step runs on every run. Its 29
steps fall into four phases:

1. **Preparation** (steps 1–3): the images under test are built from the
   checkout; the first cluster, Kubernetes 1.37.0, is created; the images
   under test are loaded into its node, and the published 0.1.0 images are
   pulled there by digest, without credentials.
2. **Evaluation mode on Kubernetes 1.37** (steps 4–13), as the cluster's
   administrator, in a namespace that enforces Pod Security `restricted`:
   server-side dry runs of revision 1, of both ingress examples and of
   values that set the six scheduling keys; the install of the published
   images with the demonstration, the publication read over HTTP and
   `helm test`; an upgrade to the images under test with changed values;
   a rollback to revision 1; a change of every password; readiness while
   PostgreSQL is scaled to 0 and back; the NetworkPolicy, observed with
   three pods; the uninstall.
3. **External mode on Kubernetes 1.37** (steps 14–20), against a
   PostgreSQL 16 server of the run's own with TLS and an admin role that
   is no superuser: the install with the setup Job, the synthetic roster
   on standard input and `helm test`; an upgrade to the images under test;
   the role passwords changed through the setup; without the admin Secret,
   an upgrade that leaves the setup on, which fails, then the routine
   upgrade; a rollback to revision 1; the uninstall. Around the
   routine upgrade, the rollback and the uninstall, the API server's
   replayed watch shows that no setup object was created, while around the
   password change it shows the setup's.
4. **Evaluation mode on Kubernetes 1.35.8, the floor** (steps 21–29),
   after the first cluster is deleted: the administrator creates the
   namespace and a ServiceAccount whose Role grants exactly the rights the
   guide lists; as that user, the dry runs of step 5 again, the install,
   in which the kubelet pulls the published images, `helm test`, the
   upgrade, the rollback and the uninstall. The API server's audit log
   shows no refused request of that user.

Its cleanup deletes both clusters, their networks and volumes, the image
tags it created and its temporary files, and confirms it.

Steps 5 and 25 have the API servers of both clusters admit, in a
server-side dry run and without a warning, every object but `helm test`'s
pod that `charts/tests/values/evaluation.yaml` renders. That file sets the
six scheduling keys and replaces some for the read service and
PostgreSQL. No release has been installed with them.

Step 18 first runs the routine upgrade without the switch that turns the
setup off, and without the admin Secret. The setup's pre-upgrade hook
fails within seconds (5 s against a 300 s timeout), and its log names the
missing password file. `helm status` reports that revision failed while
the previous one stays deployed, and the read service serves on
unchanged. The routine upgrade then succeeds, and the guide's deletion
block removes the failed setup's Job. In external mode, stage 9's release
reaches revision 6: revision 4 is the failed upgrade, 5 the routine
upgrade, 6 the rollback to revision 1.

Stage 9 also runs the guide's short commands in the steps that hold their
state: `helm get notes` (step 7) prints the port forward; `helm get values
--all` (step 9) shows revision 1's values after the rollback, both
published images by digest; `kubectl label pod` (step 12) lets a running
pod that had failed to reach the read service reach it; `kubectl logs
--follow` (step 15) follows each ingestion job's log to its end; and
`export_publications` (step 15) writes the demonstration's publication as
CSV to standard output in the attached pod.

**Outside the suite**, three runs cover what stage 9 does not:

- On 2026-11-16, the guide's manual setup without the setup Job, against a
  disposable PostgreSQL 16.15 server in Docker: on an empty database it
  applied the schema, and a second run skipped the schema step and left
  the database's objects unchanged.
- On 2026-11-16, the setup's SCRAM requirement, with the psql 16.15 of the
  published PostgreSQL image that the setup Job runs. Against a disposable
  PostgreSQL 16.15 server over TLS (`verify-full`) whose `pg_hba.conf`
  asked the admin role for `password`, for no authentication (`trust`), or
  for `md5` with a password stored as MD5, the setup stopped with libpq's
  message and changed nothing; with `scram-sha-256`, or `md5` and a
  password stored as SCRAM, it completed. An inherited `PGREQUIREAUTH`
  of `password` stopped it before it connected. A stand-in for a server
  that asked for the password in clear text, under `sslmode`
  `verify-full`, `require`, `prefer` and `disable`, or as an MD5 hash,
  received neither.
- On 2026-11-15, one evaluation-mode reinstall onto a kept database volume
  with other passwords in the Secret, on kind with Kubernetes 1.37.0 and
  Helm 4.3.1: `helm install --wait` failed at its timeout because the read
  service could not log in, and both ways out the guide names — deleting
  the claim and installing again, or setting the Secret's passwords with
  the image's role-password step and then upgrading — gave a deployed
  release whose `/readyz` answered 200.

## Local verification record

| When, source | Command | Result |
|---|---|---|
| 2026-11-16, the commit's tree, uncommitted | `./setup_venv.sh --python <python3.11> --psycopg python`, `./setup_cluster_tools.sh`, `./run_ci_suite.sh` | **All nine stages passed in 851 seconds.** Stage 1: 1673 offline tests passed, 125 expected skips, in 299 s. Stage 2: the tree, clean; no commit range, since the tree was uncommitted. Stages 3 and 4: 4 s and 5 s. Stage 5: 122 integration tests, then 27, in 22 s. Stages 6 and 7: 42 s and 49 s. Stage 8: 443 chart tests in 24 s. Stage 9: 29 steps in 402 s, 279 s on Kubernetes 1.37.0 and 123 s on 1.35.8. The images built in stages 6, 7 and 9 carry a revision marked `-dirty`, as an uncommitted tree gives. Every cleanup confirmed, and nothing of the run left in Docker |
| 2026-11-16, afterwards, with the commit's `TASK_2B.md`, `CHANGELOG.md` and `DB/TESTING_STRATEGY.md` written | `./run_checks.sh`, `check_markdown_links.py`, `check_secrets_and_local_config.py` | stage 1 passed, with the same tests and skips; the link and secrets checks passed. No other file differs from the tree of the full run |
| 2026-11-16, a clean clone of commit `a86d5e0` | `./setup_venv.sh --python <python3.11> --psycopg python`, `./setup_cluster_tools.sh`, `./run_ci_suite.sh --base b6e9e0b467ea137fb98815ec8609dacb859ad75d --head a86d5e04a8a41795d476c5d1655bbee5b05150a7` | **All nine stages passed in 827 seconds.** Stage 1: 1673 offline tests passed, 125 expected skips, in 298 s. Stage 2: the range `b6e9e0b..a86d5e0`, one commit and 280 files, clean. Stages 3 and 4: 4 s each. Stage 5: 122 integration tests, then 27, in 23 s. Stages 6 and 7: 41 s and 50 s. Stage 8: 443 chart tests in 24 s. Stage 9: 29 steps in 378 s, 268 s on Kubernetes 1.37.0 and 110 s on 1.35.8. The images built in stages 6, 7 and 9 carry the commit's revision, without `-dirty`. Every cleanup confirmed, nothing of the run left in Docker, and the clone clean afterwards. A first attempt in the same clone had stopped before any stage: `setup_cluster_tools.sh` refused the download of kind, whose server presented a certificate for another host name, and installed nothing in its place; the setup was removed and run again |

The host, for both runs: Linux 6.8 on x86_64, Bash 5.2.21, CPython 3.11.17, pip 26.2.2,
psycopg 3.3.6 in its pure-Python implementation with libpq 16.15
(`./setup_venv.sh --psycopg python`), Git 2.43.0, Docker Engine 29.9.3,
Compose 5.7.1, OpenSSL 3.0.10. The clusters: kind 0.33.0 with the network
plugin kindnet, nodes
`kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5`
and
`kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0`,
helm 4.3.1 and kubectl 1.36.6. Revision 1 ran
`ghcr.io/ccu-ukn/pubdex:0.1.0@sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325`
and
`ghcr.io/ccu-ukn/pubdex-postgres:0.1.0@sha256:33988fb179e2065e653ad7813cecf812c21f4381537b8b8db546ad37236b780a`,
pulled by digest in the node: on 1.37 before the install, on 1.35 by the
kubelet during it.

The workflows give the job 40 minutes. From task 2a's public runs and the
run on the uncommitted tree, the nine stages were estimated at about 24
minutes on a hosted runner, and the limit is 1.5 times that, rounded up to
a multiple of 5. The two public runs below took 19 and 18 minutes.

## Public-CI evidence

The `DB tests` workflow,
[`.github/workflows/db-tests.yml`](.github/workflows/db-tests.yml), on
GitHub's hosted `ubuntu-24.04` runner. Times are UTC.

| Run | Commit tested | Result |
|---|---|---|
| [pull-request run](https://github.com/CCU-UKN/pubdex/actions/runs/43983680287), 2026-11-16 23:57 to 2026-11-17 00:17, before `main` moved | `72ea3501bf57bd08b1af0ca3d7f58001ad55a963`, GitHub's test merge of `a86d5e0` into `b6e9e0b`, the tip of `main`; its tree is `a51189cf…`, the commit's | **All nine stages passed in 1082 seconds**, the job in 19 minutes. Stage 1: 1673 offline tests passed, 125 expected skips. Stage 2: from `b6e9e0b`, two commits, `a86d5e0` and the test merge, and 280 files, clean. Stage 5: 122 integration tests, then 27. Stage 8: 443 chart tests. Stage 9: 29 steps in 404 s. The images built carry the test merge's revision |
| [push run on `main`](https://github.com/CCU-UKN/pubdex/actions/runs/43989083559), 2026-11-17 00:37 to 00:55, after `main` was moved to the commit unchanged | `a86d5e04a8a41795d476c5d1655bbee5b05150a7` itself | **All nine stages passed in 1016 seconds**, the job in 18 minutes against its limit of 40. Stage 1: 1673 offline tests passed, 125 expected skips, in 260 s. Stage 2: the range `b6e9e0b..a86d5e0`, one commit and 280 files, clean. Stages 3 and 4: 13 s and 4 s. Stage 5: 122 integration tests, then 27, in 20 s. Stages 6 and 7: 222 s and 56 s. Stage 8: 443 chart tests in 18 s. Stage 9: 29 steps in 419 s, 298 s on Kubernetes 1.37.0 and 121 s on 1.35.8. The images built in stages 6, 7 and 9 carry the commit's revision. Every cleanup confirmed |

Both runs: runner image `ubuntu-24.04` version 20261111.370.1 (Ubuntu
24.04.5), CPython 3.11.18, pip 26.2.2, psycopg 3.3.6 in its C
implementation with libpq 18.6, Docker Engine 28.0.4 and Compose 2.38.2;
helm 4.3.1, kubectl 1.36.6 and kind 0.33.0 installed by
`setup_cluster_tools.sh`, with the node images named above. The installers
reported that the runner permits no network namespace: pip ran with
`--no-index`, without a network sandbox, as in task 2a's public run.

These runs identify commit `a86d5e0`. The later documentation commit that
records them is not a run of that commit and does not change its results.

## Verification limits

- **Two cluster configurations.** Kubernetes 1.37.0 and 1.35.8, on one
  local machine and on GitHub's hosted runner. Every cluster run used kind clusters of one node, with the
  network plugin kindnet and kind's default StorageClass. No other
  Kubernetes distribution, network plugin or storage provisioner has run
  the chart: a volume from another StorageClass with the pods' `fsGroup`,
  the NetworkPolicy under another network plugin and multi-node
  scheduling are covered by the guide's *Supported platforms* table as
  untested. External mode, the password changes, the loss of the
  database and the NetworkPolicy ran on 1.37 only. Kubernetes 1.36 is
  supported by API compatibility, without a cluster run; 1.38 and later
  are not known. Managed services and ingress controllers are untested,
  and the ingress examples and the scheduling values reached the API
  servers only in server-side dry runs. OpenShift and arm64 are not
  supported ([`KUBERNETES.md`](KUBERNETES.md), *Supported platforms*).
- **Access rights.** Only evaluation mode's lifecycle on 1.35 ran as a
  namespace-scoped user; everything else ran as the cluster's
  administrator. That user used the rights over CronJobs and Ingresses
  only in server-side dry runs, and did not use those over `pods/attach`
  and `deployments/scale`.
- **Upgrades from 0.1.0.** Stage 9's upgrades from the published 0.1.0
  images to the images under test (steps 8, 16 and 27) assume that the
  checkout's `DB/init/` is release 0.1.0's, as it is in this tree;
  [`DB/TESTING_STRATEGY.md`](DB/TESTING_STRATEGY.md) states the assumption
  and that a change of `DB/init/` needs a decision about these steps.
- **Helm.** Helm 4.3 or a later 4.x; only 4.3.1 ran. Helm 3 is not
  supported. Argo CD and Flux are untested.
- **Commands no cluster ran.** The guide's appendix lists them. Among them:
  an install without the setup Job, whose manual setup ran only outside
  the suite, against PostgreSQL in Docker; the ingress examples beyond a
  server-side dry run, and `kubectl create secret tls`; the egress policy
  example; scheduled CronJob runs; the roster import in evaluation mode;
  backup and restore on Kubernetes; a PersistentVolume with `Retain`;
  `--reuse-values` after a routine upgrade, which the guide warns against;
  and `sslmode` other than `verify-full` in a cluster.
- **Hosted runners.** The nine stages ran on one hosted runner platform,
  GitHub's `ubuntu-24.04` with Docker Engine 28.0.4, twice, in 19 and 18
  minutes against the job's limit of 40. No other hosted runner has run
  them.
- **No self-hosted Forgejo run** of this result, as stated above.

## Not part of this result

- **Release automation, checksums, SBOM or equivalent provenance, a
  published chart package, and upgrade between releases are task 2c.**
- **The versioned read API is task 3a, and write paths and authentication
  are task 3b.** The read service behind the Ingress is the preliminary one
  of task 2a, without authentication.
- **Scheduled backups, retention, operator tooling and recovery drills are
  task 4a.**
- Schema migrations: there is no migration tool.
- Outside this task altogether: existing clusters, highly available or
  replicated PostgreSQL, Kubernetes operators, autoscaling, OpenShift,
  arm64, a service mesh, and authentication in front of the API.
