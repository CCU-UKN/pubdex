#!/usr/bin/env bash
# The acceptance test of the Helm chart on disposable Kubernetes clusters,
# stage 9 of run_ci_suite.sh: two kind clusters that this run creates, owns
# and deletes again, one after the other. On the first, of the newest
# Kubernetes release the chart supports, the chart of this checkout runs in
# both of its modes -- evaluation mode with its bundled PostgreSQL, and
# external mode against a PostgreSQL server of the run's own in another
# namespace -- through the install of the preceding PubDex release, the
# synthetic demonstration, reads over HTTP, helm test, the upgrade to this
# checkout's chart and the images under test with the schema upgrade,
# rollback, a password change, the loss and return of the database, the
# NetworkPolicy, and uninstall; in evaluation mode also a fresh install of
# this checkout's chart, and in external mode a routine upgrade, without the
# setup Job and the admin role's Secret, and a rollback without that Secret.
# External mode then upgrades test release A to test release B1, each
# installed from its chart package file, and rolls back.
# On the second, of the oldest supported release, a user who holds only the
# rights KUBERNETES.md lists runs evaluation mode's upgrade from the
# preceding PubDex release, and the kubelet pulls the published images.
#
#   ./run_cluster_acceptance.sh                       # builds both images from this checkout
#   ./run_cluster_acceptance.sh --image <reference> --postgres-image <reference> \
#       [--expect-version <version>] [--expect-revision <commit>]   # tests images Docker has
#
# The test releases. release.sh builds two test releases of this checkout,
# each under a version reserved for test releases with a name of the run's
# own, and verifies them: A, the checkout as it is (--test a<run>), and B1,
# the checkout with the test-only overlay b1 -- one backward-compatible
# schema change after this checkout's and a new transform version (--test
# b1<run> --overlay b1). Each gives a chart package file, which the steps
# install, and two images under local tags, which the run loads into the
# node and sets explicitly: in external mode the setup Job runs the
# PostgreSQL image's psql. Both are built with or without --image: they
# test the upgrade's mechanism, whatever images the rest of the run tests.
#
# Revision 1 of each Helm release is the preceding PubDex release as an
# operator has it installed: its own chart and its two published images.
# The release record (releases/, read by DB/release_record.py) names that
# release, the newest one below VERSION, its images by registry, tag and
# digest, and the commit whose charts/pubdex/ is its chart; the run takes
# that chart from the repository's history with git archive, into its
# temporary directory, and the cluster pulls the images without credentials.
# A clone without that commit -- a shallow one -- fails before anything is
# created. Revision 2 is this checkout's chart with the images under test:
# its schema upgrade brings the database to this checkout's schema changes
# (DB/schema/), which every check of the record then compares. What one run
# does, stopping at the first failed check:
#
#   1. selects the images under test: without --image it builds this checkout
#      with build_image.sh under tags of the run's own and checks that their
#      labels are this checkout's VERSION and revision; with --image and
#      --postgres-image it tags those images, which Docker has to have, under
#      the run's tags, and checks the labels --expect-version and
#      --expect-revision require. Images that are the published ones are
#      refused: the upgrade has to change them. Then it extracts the
#      preceding PubDex release's chart from the repository's history and
#      checks its version against the release record. Last, it builds test
#      releases A and B1 with release.sh build, and checks from their source
#      archives that A ships exactly this checkout's schema changes and B1
#      those and one more, and from their images' labels that B1 carries
#      another transform version than A;
#   2. creates a Docker network of the run's own, labelled with the run, and
#      a kind cluster on it from the pinned node image, with a kubeconfig in
#      the run's temporary directory; before any kubectl or helm call it
#      checks that this kubeconfig holds one context, kind-<cluster>, whose
#      server is the loopback port Docker publishes for this run's
#      control-plane container, and refuses to go on otherwise;
#   3. loads the two images under test into the node with kind load, and has
#      the node's container runtime pull the two published images by digest
#      (crictl pull), without credentials. Without access to their registry
#      the run fails here, and says so. Then it loads both images of test
#      releases A and B1 with kind load; A's and B1's application images
#      have different ids;
#
#   Evaluation mode, release "pubdex" in namespace "pubdex":
#
#   4. creates the namespace, which enforces Pod Security "restricted" (and
#      warns and audits by it), the three secret files with create_secrets.sh
#      and from them the Secret pubdex-credentials, with the commands an
#      operator uses;
#   5. renders revision 1, the preceding release's chart, and this checkout's
#      chart with the upgrade's values, with helm template, and has the API
#      server validate every object of each, the demonstration's hook and
#      the schema upgrade's Job included, with a server-side dry run:
#      admission, Pod Security included, without a warning. helm test's pod
#      names a ServiceAccount that only the install creates, so each chart's
#      gets the same dry run before its helm test, in steps 7 and 8. Then the
#      same dry run of this checkout's chart, without helm test's pod, of
#      what no step installs: each of the guide's two ingress examples, and
#      charts/tests/values/evaluation.yaml, which sets the six scheduling
#      keys at the top level and replaces some for the read service and
#      PostgreSQL. The dry run creates no pod, so the PriorityClasses those
#      values name, which no step creates, are never resolved;
#   6. installs revision 1, the preceding release's chart and published
#      images, with the demonstration (demo.enabled) and --wait: the
#      demonstration's post-install Job has to complete;
#   7. checks that every pod runs the published image by its digest and that
#      the database's record of schema changes is the preceding release's,
#      reads GET /v0/publications through kubectl port-forward on 127.0.0.1
#      and a port kubectl picks, field by field, and /readyz; runs helm test;
#   8. upgrades to this checkout's chart and the images under test with
#      changed values -- the log level in the ConfigMap and two API replicas
#      -- with --wait-for-jobs, and checks that the schema upgrade's Job of
#      the revision completed and applied what was outstanding, that the
#      database records exactly this checkout's schema changes, that every
#      pod was replaced and runs the new image, that the data persists, that
#      PostgreSQL started on its volume without a new initialisation, and
#      that app_readonly and app_writer still log in with their passwords;
#      helm test;
#   9. rolls back to revision 1, with the same checks: the schema and its
#      record stay as the upgrade left them, revision 2's schema upgrade Job
#      goes with its revision, and the preceding release's images serve the
#      demonstration's publication on that schema. Then it upgrades to this
#      checkout again, with one replica and the log level of revision 1: the
#      new revision's schema upgrade Job finds nothing outstanding, the
#      record is unchanged, and steps 10 to 13 run this checkout's chart and
#      images;
#  10. changes every password: new files from create_secrets.sh replace the
#      Secret's, the image's role-password step (020_role_passwords.sh
#      --stdin, in the PostgreSQL pod) sets them, and the read service is
#      restarted; the new passwords log in, the old ones are refused;
#  11. scales PostgreSQL to 0: /readyz answers 503 database_unavailable,
#      /livez 200, and the read service's pod becomes not ready but is not
#      restarted; scaled back to 1, the same pod becomes ready again, and
#      PostgreSQL starts on its volume without a new initialisation and with
#      the new passwords;
#  12. observes the NetworkPolicy, which kind's network plugin (kindnet)
#      enforces: of three pods of the namespace, one without labels reaches
#      neither PostgreSQL nor the read service, one with the client label
#      reaches only the read service, and one with the labels of the
#      release's ingestion pods only PostgreSQL;
#  13. uninstalls the release and checks that exactly what the chart's notes
#      say remains -- the database volume's claim and the operator's Secret
#      -- then deletes both as the notes say;
#  14. installs this checkout's chart afresh, with the images under test,
#      the demonstration, --wait and --wait-for-jobs, on a new Secret and an
#      empty volume: PostgreSQL initialises the baseline, the schema upgrade's
#      Job of revision 1 applies this checkout's schema changes, and the
#      demonstration's post-install Job completes after it. The database
#      records exactly this checkout's schema changes; the pods, the
#      publication, helm test; then the uninstall, and the claim and the
#      Secret deleted as the notes say: nothing is left;
#
#   External mode, release "pubdex" in namespace "pubdex-external":
#
#  15. starts the server, in namespace "pubdex-db", which enforces
#      "restricted" too: the published PostgreSQL image, prepared as a
#      managed service would be -- its superuser is clusteradmin, no role is
#      named postgres, and the database people_db belongs to people_db_owner,
#      which has CREATEROLE and is no superuser -- serving TLS with a
#      certificate for its Service's DNS name from a CA the run creates with
#      openssl;
#  16. creates the namespace, the Secrets and the CA's ConfigMap as
#      KUBERNETES.md says, and installs revision 1, the preceding release's
#      chart with its examples/values-external.yaml, its published
#      application image, the server's host and two on-demand ingestion
#      tasks, after server-side dry runs of revision 1 and of this
#      checkout's chart with the upgrade's values: the setup Job, a hook,
#      prepares the database as people_db_owner, and the record of schema
#      changes is the preceding release's. Then it deletes the admin Secret, checks
#      that no remaining pod or workload refers to it and that the read
#      service connects by TLS, verifying the server (verify-full), and runs
#      the synthetic demonstration with the commands the chart's notes
#      print: the roster through kubectl run on standard input, the other two
#      steps as jobs created from the CronJobs; reads the publication and
#      runs helm test;
#  17. upgrades to this checkout's chart and the images under test with
#      changed values, with the admin Secret there for the setup hook and
#      the schema upgrade's
#      hook, which runs after it as people_db_owner: the database records
#      exactly this checkout's schema changes, applied by that role; every
#      read service pod replaced, the schema's objects keep their ids (no
#      second initialisation), the same publication, helm test;
#  18. changes both role passwords: the Secret replaced, helm upgrade with
#      step 17's values files runs the setup again, the read service is
#      restarted; the new passwords log in, the old ones are refused. The
#      admin Secret is created before that upgrade and deleted after it, and
#      stays deleted from here on. The API server's watch of the namespace's Jobs,
#      ConfigMaps and ServiceAccounts, replayed from before that upgrade, has
#      to show the setup's Job, ConfigMap and ServiceAccount and the schema
#      upgrade's Job and ServiceAccount created, although Helm deleted them
#      again: the check steps 19 to 21 use sees a setup. The schema upgrade
#      found nothing outstanding: the record is unchanged;
#  19. a routine upgrade: checks that the admin Secret does not exist, then
#      runs the upgrade first without the switch, so with the setup: it has
#      to fail well inside the timeout, in its pre-upgrade hook, kubectl logs
#      of the setup's Job has to say that the admin Secret is missing, helm
#      status and helm history show revision 4 failed and revision 3
#      deployed, only the setup's Job is left, and the read service runs on
#      unchanged and serves the publication. Then helm upgrade with --set
#      externalDatabase.setup.enabled=false and one changed setting, the log
#      level. The API server's watch of the namespace's Jobs, ConfigMaps and
#      ServiceAccounts, replayed from before that upgrade, has to show that
#      none was created: neither the setup nor the schema upgrade ran. The
#      revision carries no pre-upgrade hook; every read service pod is
#      replaced and runs the same image, the schema keeps its ids and its
#      record. The failed setup's Job is still there; the guide's kubectl
#      delete removes it and its pod. The publication, helm test;
#  20. rolls back to revision 1, the preceding release's chart and image,
#      still without the admin Secret. The rollback's revision carries
#      revision 1's hooks, the setup's pre-install,pre-upgrade hooks among
#      them, yet the replayed watch has to show that nothing was created:
#      Helm ran none of them. Every pod runs the published image by its
#      digest, with the passwords of step 18; the schema keeps its ids and
#      its record, the publication, helm test, and the admin Secret still
#      does not exist;
#  21. uninstalls the release, without the admin Secret, and the replayed
#      watch shows nothing created: the operator's Secret and the CA's
#      ConfigMap remain, and the server keeps the database, its data and the
#      three roles; deletes them. Last, no pod of any namespace was refused
#      by admission (no FailedCreate event).
#
#   External mode, test release A to test release B1, release "pubdex" in
#   namespace "pubdex-external", on the same server:
#
#  22. drops the database and the three roles that step 21 left on the
#      server, as the guide's Uninstall allows, and creates the database
#      again for people_db_owner, so that the server is as step 15 left it;
#      creates the Secrets, of new passwords, and the CA's ConfigMap, and
#      takes the external example out of A's chart package file, and
#      installs A from that package with the setup Job: the setup
#      applies the baseline and the schema upgrade's hook this checkout's
#      schema changes, as people_db_owner, recorded as A's. Then it deletes
#      the admin Secret and runs the demonstration with the commands the
#      chart's notes print, as step 16 does with A's image: the read service
#      serves the publication, and every row carries A's transform version;
#  23. takes the external example out of B1's chart package file, and
#      upgrades to B1 as a routine upgrade, without the admin Secret and with
#      --set externalDatabase.setup.enabled=false, with a timeout of
#      SHORT_WAIT_SECONDS: neither the setup nor the schema upgrade runs, so
#      B1's schema change is outstanding and B1's read service pod stays not
#      ready. helm upgrade has to fail at that timeout; helm status and helm
#      history show revision 2 failed and revision 1 deployed; the pod's
#      events show its readiness probe failing, its /readyz and
#      /v0/publications answer 503 with the reason schema_upgrade_required
#      and the message naming B1's schema change and the command, and its
#      log names them too. A's pod is the same, ready, and serves the
#      publication; the record of schema changes is unchanged;
#  24. takes the external example out of B1's package again, creates the
#      admin Secret and runs the same upgrade with the setup: the
#      replayed watch shows the setup's and the schema upgrade's hooks
#      created, and the database then records B1's schema changes, the new
#      one applied by people_db_owner as B1's. B1's pod of step 23, not
#      replaced, turns ready, and A's pod goes; the admin Secret is deleted
#      again. B1's ingestion task ingests the demonstration's payloads
#      again, every row then carrying B1's transform version, and the
#      publication is served;
#  25. rolls back to revision 1, A, without the admin Secret: an image
#      rollback after a backward-compatible schema change. The replayed watch
#      shows nothing created, the schema and its record stay as B1 left
#      them, A's read service is ready and serves the publication, and A's
#      ingestion task writes on that schema, every row carrying A's transform
#      version again;
#  26. uninstalls the release without the admin Secret: the operator's Secret
#      and the CA's ConfigMap remain, and the run deletes them. Last, no pod
#      of any namespace was refused by admission.
#
#   The floor, the oldest supported release, evaluation mode, release
#   "pubdex" in namespace "pubdex":
#
#  27. deletes the first cluster and its network, and confirms that they are
#      gone: the two clusters never run at the same time;
#  28. creates a Docker network of the run's own and on it a kind cluster from
#      the pinned floor node image (Kubernetes 1.35), whose API server writes
#      an audit log of the operator's requests (below); checks its kubeconfig
#      as in step 2;
#  29. loads the two images under test into the node with kind load, and
#      checks that the node has neither published image: no pull before the
#      install;
#  30. as the cluster's administrator: creates the namespace, which enforces
#      Pod Security "restricted", the operator's ServiceAccount, a Role
#      granting exactly the rights KUBERNETES.md lists under "Access rights"
#      and a RoleBinding, and a token of that ServiceAccount, valid for an
#      hour (kubectl create token); writes a second kubeconfig holding only
#      the operator, with that token, and checks it as in step 2. The
#      operator may not create a namespace;
#
#   As the operator, by the commands KUBERNETES.md gives:
#
#  31. creates the secret files and the Secret, and has the API server
#      validate revision 1, this checkout's chart with the upgrade's values,
#      the two ingress examples and the scheduling values with server-side
#      dry runs, as in step 5: the Role covers every object they render, the
#      Ingress and the schema upgrade's Job included;
#  32. installs revision 1 with the demonstration: the preceding release's
#      chart and its published images by digest, with that chart's default
#      pull policy IfNotPresent. The pods' events have to show the kubelet
#      pulling each published image, and the node's images have to carry
#      their digests. Then the checks of step 7 and helm test;
#  33. upgrades to this checkout's chart and the images under test with
#      changed values, with the checks of step 8 and helm test;
#  34. rolls back to revision 1, with the rollback's checks of step 9;
#  35. uninstalls the release, deletes the database volume's claim and the
#      Secret as the notes say, and, as the administrator, checks that
#      nothing of the release is left, that its volume is gone and that no
#      pod was refused by admission. The audit log has to show no refused
#      request of the operator; the run prints the rights the operator used,
#      and those of the Role it did not use. Last, the administrator deletes
#      the operator's RoleBinding, Role and ServiceAccount, and the token
#      no longer authenticates.
#
#   After each of steps 31 to 34 the audit log, read from the node, has to
#   show no request of the operator the API server refused; a refusal fails
#   the run and names the verb and the resource. The audit log records only
#   the operator's requests, and only their metadata, never a body.
#
# Isolation. The first cluster is pubdex-cluster-<run id>, on the Docker
# network pubdex-cluster-<run id>-net; the floor's is pubdex-floor-<run id>,
# on pubdex-floor-<run id>-net. The run creates each network with the label
# pubdex.cluster-acceptance.run=<run id> and hands it to kind through
# KIND_EXPERIMENTAL_DOCKER_NETWORK (kind's own, experimental, setting); kind
# labels its node container with the cluster's name. The test releases'
# images are pubdex-release:<version> and pubdex-postgres-release:<version>,
# their versions carrying the run id, and their images are labelled with
# those versions. A cluster, network or image tag of those names that exists
# already stops the run before anything is created. release.sh runs with
# the caller's PATH, HOME and Docker selection, and TMPDIR in the run's
# temporary directory. Every helm and kubectl call runs with a cleared environment:
# KUBECONFIG names one of the run's own kubeconfigs and an inherited
# KUBECONFIG is never passed on, HOME, TMPDIR, kubectl's cache and helm's
# configuration, cache and data directories lie in the run's temporary
# directory, and DOCKER_CONFIG names an empty directory there; each call
# names that kubeconfig and its one context explicitly: kind-<cluster>, or
# the operator's on the floor. kind runs with the same HOME and TMPDIR and
# the caller's Docker selection. No kubeconfig of the caller's, no ~/.kube and
# no helm, kubectl or kind but the pinned ones under .tools/ take part; the
# run contacts no other cluster. The only helm and kubectl calls before a
# kubeconfig is checked are `helm version` and `kubectl version --client`,
# which read no kubeconfig and contact no server. The floor's node mounts the
# audit policy from the run's temporary directory, so the Docker daemon has
# to see that directory under the same path.
#
# Cleanup. Registered before anything is created, it runs on every exit and
# after SIGINT, SIGTERM or SIGHUP, ignoring further signals while it works: it
# stops a port forward or a tool still running, deletes each kind cluster the
# run started (kind delete cluster) -- every namespace, image and token in it
# goes with it --, removes any container still labelled with either cluster,
# the nodes' volumes, the run's networks, the image tags the run created,
# every image labelled with a test release's version and every container of
# one -- a probe of release.sh's, or the SBOM tool's reading the run's
# directory -- and the temporary directory with the kubeconfigs, the
# operator's included, the audit log's copy, the secret files, the
# certificates and the test releases' files, in that order. It
# then confirms that none of them is left; if one is, or Docker cannot say, it
# prints the commands that find it and fails -- with status 1 after an
# otherwise successful run, and with the earlier failure's or the signal's
# status otherwise. A network named "kind" and every other cluster, network,
# image and volume are left alone; the node images, the SBOM tool and build
# cache stay, and nothing is pruned. SIGKILL skips the cleanup; the label,
# the names and the versions find what is left.
#
# Network access. Docker needs the two pinned kind node images and, when
# building -- build_image.sh and release.sh -- the base images and the
# Python packages, unless it has them. The clusters pull the preceding
# release's two published images from their registry (ghcr.io for the
# release record's), anonymously, and no other image: the first through its
# container runtime (step 3), the floor's through the kubelet (step 32).
# No metadata provider is contacted: the demonstration uses bundled payloads.
#
# Needs Linux on x86_64, Bash, Docker Engine 28.0.1 or later, except 28.2.0 to
# 28.3.2, with the buildx plugin and the Compose plugin 2.24.0 or later
# (DB/lib/docker_versions.sh; the images are the Compose deployment's), the
# pinned helm, kubectl and kind that ./setup_cluster_tools.sh installs under
# .tools/ (checked against their pinned hashes and versions), the pinned SBOM
# tool that ./release.sh setup pulls, .venv with PyYAML (./setup_venv.sh),
# Python 3, openssl, sha256sum, GNU tar and gzip, and a clone with the commit
# of the preceding release's chart; Git reads that chart and labels a build,
# and release.sh reads the checkout's files with it. Exit status: 0 when every check
# passed and the cleanup was confirmed, 1 for a failed check, prerequisite or
# cleanup, 2 for a usage error, 130, 143 or 129 after a signal.
set -euo pipefail

# A shell function the caller exported, or one a BASH_ENV file defined, would
# stand in for docker or any other command below. None of this script's own
# exists yet, so every function defined now goes.
while read -r _declare _flags _function; do
  if [ -n "$_function" ]; then unset -f "$_function"; fi
done <<<"$(declare -F)"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="run_cluster_acceptance.sh"
CHART="$ROOT/charts/pubdex"
EXTERNAL_EXAMPLE="$CHART/examples/values-external.yaml"
DEMO_ROSTER="$ROOT/DB/tests/fixtures/golden/demo_roster_minimal.csv"
LABEL_KEY="pubdex.cluster-acceptance.run"
PROBE_LABEL="pubdex.cluster-acceptance/probe"
KIND_LABEL="io.x-k8s.kind.cluster"
EVAL_NAMESPACE="pubdex"
EXTERNAL_NAMESPACE="pubdex-external"
SERVER_NAMESPACE="pubdex-db"
SERVER="pubdex-db"             # the external server's StatefulSet and Service
SERVER_POD="$SERVER-0"
SERVER_SUPERUSER="clusteradmin"
SERVER_HOST="$SERVER.$SERVER_NAMESPACE.svc.cluster.local"
NAMESPACE="$EVAL_NAMESPACE"    # the namespace k() works in
RELEASE="pubdex"
EVAL_SECRET_NAME="pubdex-credentials"  # the operator's Secret in evaluation mode
SECRET_NAME="$EVAL_SECRET_NAME"        # the operator's Secret of the current release
DEMO_DOI="10.5555/pubdex-fixture-001"
DEMO_AFFILIATION="Synthetic Institute for Collective Behaviour"
WAIT_SECONDS="${CLUSTER_WAIT_SECONDS:-300}"
OPERATOR="pubdex-operator"     # the floor's operator: its ServiceAccount, Role, RoleBinding and context
OPERATOR_USER="system:serviceaccount:$EVAL_NAMESPACE:$OPERATOR"
AUDIT_POLICY="/etc/kubernetes/policies/pubdex-audit.yaml"  # in the floor's node
AUDIT_LOG="/var/log/kubernetes/pubdex-audit.log"           # in the floor's node
# The routine upgrade to test release B1 cannot complete while B1's schema
# change is outstanding: it is given this timeout, not WAIT_SECONDS.
SHORT_WAIT_SECONDS=30
VERSION_LABEL="org.opencontainers.image.version"
TRANSFORM_LABEL="pubdex.transform-version"
STEPS=35
# shellcheck source=DB/lib/disposable_postgres.sh
source "$ROOT/DB/lib/disposable_postgres.sh"  # for clear_exports
# shellcheck source=DB/lib/docker_versions.sh
source "$ROOT/DB/lib/docker_versions.sh"
# shellcheck source=DB/lib/release_identity.sh
source "$ROOT/DB/lib/release_identity.sh"
# shellcheck source=DB/lib/compose_acceptance.sh
source "$ROOT/DB/lib/compose_acceptance.sh"  # isolated, bounded_wait, wait_until, http
# shellcheck source=DB/lib/cluster_tools.sh
source "$ROOT/DB/lib/cluster_tools.sh"
# shellcheck source=DB/lib/sbom_tool.sh
source "$ROOT/DB/lib/sbom_tool.sh"  # the SBOM tool release.sh builds the test releases with

usage() {
  cat <<'USAGE'
usage: run_cluster_acceptance.sh [--image <reference> --postgres-image <reference>
                                   [--expect-version <version>] [--expect-revision <commit>]]

Creates a kind cluster of its own and tests the Helm chart of this checkout
on it in both modes: evaluation mode with the synthetic demonstration, and
external mode against a PostgreSQL server of the run's own. Each release is
installed as the preceding PubDex release -- its chart from the repository's
history, its published images from the release record --, read over HTTP,
tested with helm test, upgraded to this checkout's chart and the images
under test with other values, which runs the schema upgrade, rolled back
and uninstalled; the evaluation release is also upgraded again after its
rollback, both change their passwords, the evaluation release loses and
regains its database and has its NetworkPolicy observed, and this
checkout's chart is installed afresh with the demonstration. In external
mode the run also builds two test releases of this checkout with
release.sh, A and B1, which adds a backward-compatible schema change, and
installs A from its chart package file, tries B1 as a routine upgrade,
which cannot complete before the schema upgrade, upgrades to B1 with the
schema upgrade and rolls back to A. The run then
replaces that cluster with one of the oldest supported Kubernetes release,
on which a user holding only the rights KUBERNETES.md lists installs the
preceding release, tests, upgrades, rolls back and uninstalls evaluation
mode, the kubelet pulling the published images. Last, it deletes
everything it created. It uses only the pinned tools under .tools/ and
kubeconfigs of its own, and needs anonymous access to the published images'
registry from the clusters. Every step runs on every run.

  (no option)                  build both images from this checkout
                               (build_image.sh) under tags of the run's own
  --image <reference>          test this application image instead; Docker
                               has to have it; only the test releases are
                               built
  --postgres-image <reference> with --image: test this PostgreSQL image
  --expect-version <version>   require this release version in the
                               org.opencontainers.image.version labels
  --expect-revision <commit>   require this source revision in the
                               org.opencontainers.image.revision labels: a
                               full commit id, with -dirty for a local build
  -h, --help                   show this help

CLUSTER_WAIT_SECONDS bounds each wait for the cluster and the release (300).
USAGE
}

die_usage() {
  echo "$PROG: $1" >&2
  usage >&2
  exit 2
}

image_ref=""
postgres_ref=""
expect_version=""
expect_revision=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image|--postgres-image|--expect-version|--expect-revision)
      if [ $# -lt 2 ] || [ -z "$2" ]; then die_usage "$1 needs a value"; fi
      case "$1" in
        --image) image_ref="$2" ;;
        --postgres-image) postgres_ref="$2" ;;
        --expect-version) expect_version="$2" ;;
        --expect-revision) expect_revision="$2" ;;
      esac
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die_usage "unknown argument: $1"
      ;;
  esac
done
if [ -n "$image_ref" ] && [ -z "$postgres_ref" ]; then
  die_usage "--image needs --postgres-image: the cluster runs the images under test only as loaded from this Docker"
fi
if [ -n "$postgres_ref" ] && [ -z "$image_ref" ]; then
  die_usage "--postgres-image needs --image; without --image both images are built from this checkout"
fi
if [ -n "$expect_version$expect_revision" ] && [ -z "$image_ref" ]; then
  die_usage "--expect-version and --expect-revision need --image; a build is checked against this checkout"
fi
if [ -n "$expect_version" ] && ! [[ "$expect_version" =~ $RELEASE_VERSION_RE ]]; then
  die_usage "--expect-version needs a release version such as 0.1.0, not '$expect_version'"
fi
if [ -n "$expect_revision" ] && ! [[ "${expect_revision%-dirty}" =~ $COMMIT_RE ]]; then
  die_usage "--expect-revision needs a full commit id, optionally with -dirty, not '$expect_revision'"
fi
if ! [[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  die_usage "CLUSTER_WAIT_SECONDS must be a positive number of seconds"
fi

# --- prerequisites, before anything is created --------------------------------------------

prerequisite() {
  echo "$PROG: $1" >&2
  shift
  local hint
  for hint in "$@"; do
    echo "  $hint" >&2
  done
  exit 1
}

platform="$(uname -s)/$(uname -m)"
[ "$platform" = Linux/x86_64 ] \
  || prerequisite "this test runs on Linux on x86_64 only, not $platform: the kind node image and the deployment's images are linux/amd64."
command -v docker >/dev/null 2>&1 || prerequisite "docker was not found on PATH."
docker info >/dev/null 2>&1 || prerequisite "cannot reach the Docker daemon ('docker info' shows the reason)."
engine_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" || engine_version=""
engine_arch="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null)" || engine_arch=""
compose_version="$(docker compose version --short 2>/dev/null)" || compose_version=""
problem="$(engine_problem "$engine_version")"
if [ -n "$problem" ]; then
  prerequisite "$problem; the images need Docker Engine $ENGINE_SUPPORTED (DB/lib/docker_versions.sh says why)."
fi
[ "$engine_arch" = linux/amd64 ] || prerequisite "the Docker daemon runs on '$engine_arch', not linux/amd64."
if [ -z "$compose_version" ]; then
  prerequisite "the Docker Compose plugin was not found: 'docker compose version' fails; the deployment's images need it, version $COMPOSE_MINIMUM or later."
fi
problem="$(version_problem "Docker Compose" "$compose_version" "$COMPOSE_MINIMUM")"
[ -z "$problem" ] || prerequisite "$problem; the deployment needs $COMPOSE_MINIMUM or later."
command -v sha256sum >/dev/null 2>&1 || prerequisite "sha256sum was not found on PATH."
command -v openssl >/dev/null 2>&1 \
  || prerequisite "openssl was not found on PATH; it creates the certificates of the external PostgreSQL server's TLS."
# What release.sh needs to build the test releases, which it checks again.
for tool in python3 gzip; do
  command -v "$tool" >/dev/null 2>&1 || prerequisite "$tool was not found on PATH; release.sh builds the test releases with it."
done
tar_version="$(tar --version 2>/dev/null | head -n 1)" || tar_version=""
[[ "$tar_version" == *"GNU tar"* ]] \
  || prerequisite "tar is not GNU tar ('${tar_version:-tar --version fails}'); release.sh writes the test releases' source archives with it."
docker buildx version >/dev/null 2>&1 \
  || prerequisite "the Docker buildx plugin was not found ('docker buildx version' fails); build_image.sh and release.sh need it."
if ! docker image inspect --format '{{.Id}}' "$PINNED_SYFT_IMAGE" >/dev/null 2>&1; then
  prerequisite "the pinned SBOM tool, Syft $PINNED_SYFT_VERSION, is not on this machine; release.sh builds the test releases with it. Pull it, by its pinned digest, from the repository root with:" \
    "./release.sh setup"
fi
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ] || ! "$PY" -I -c 'import yaml' >/dev/null 2>&1; then
  prerequisite "no .venv/bin/python with the development requirements (PyYAML); create it from the repository root with:" \
    "./setup_venv.sh"
fi
HELM="$(cluster_tool_path "$ROOT" helm)"
KUBECTL="$(cluster_tool_path "$ROOT" kubectl)"
KIND="$(cluster_tool_path "$ROOT" kind)"
for tool in helm kubectl kind; do
  path="$(cluster_tool_path "$ROOT" "$tool")"
  version_name="PINNED_${tool^^}_VERSION"
  shown=".tools/$tool-${!version_name}/$tool"
  if [ ! -f "$path" ] || [ -L "$path" ] || [ ! -x "$path" ]; then
    prerequisite "the pinned $tool ${!version_name} is not installed at $shown. Install the pinned tools with:" \
      "./setup_cluster_tools.sh"
  fi
  if [ "$tool" = helm ]; then pinned="$PINNED_HELM_MEMBER_SHA256"; else hash_name="PINNED_${tool^^}_SHA256"; pinned="${!hash_name}"; fi
  have="$(sha256sum < "$path")" || prerequisite "could not read $shown"
  if [ "${have%% *}" != "$pinned" ]; then
    prerequisite "$shown has the SHA-256 ${have%% *}, not the pinned $pinned." \
      "Remove .tools/$tool-${!version_name} and install it again with: ./setup_cluster_tools.sh"
  fi
done

# external_names <example>: the names an external example uses for its
# Secrets and the CA's ConfigMap, one line; the example has to be external
# mode with verify-full, a CA ConfigMap and the setup Job.
external_names() {
  "$PY" -I - "$1" <<'PY'
import sys, yaml
example = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
database, setup = example["externalDatabase"], example["externalDatabase"]["setup"]
ca = database["caCertificate"]
if example["mode"] != "external" or database["sslmode"] != "verify-full" or not ca["configMapName"] or not setup["enabled"]:
    sys.exit(f"{sys.argv[1]} is not external mode with verify-full, a CA ConfigMap and the setup Job")
print(example["secrets"]["name"], example["database"]["name"], setup["adminUser"], setup["adminSecret"]["name"],
      setup["adminSecret"]["key"], ca["configMapName"], ca["key"])
PY
}
external_facts="$(external_names "$EXTERNAL_EXAMPLE" 2>&1)" \
  || prerequisite "cannot read the chart's external example: $external_facts"
read -r EXTERNAL_SECRET_NAME DATABASE ADMIN_USER ADMIN_SECRET_NAME ADMIN_KEY CA_CONFIGMAP CA_KEY <<<"$external_facts"

# Revision 1: the preceding PubDex release, by its entry in the release
# record -- its two published images by registry, tag and digest, its chart's
# version and commit, and the schema changes it ships.
record_answer="$("$PY" -I "$ROOT/DB/release_record.py" preceding 2>&1)" \
  || prerequisite "the release record names no preceding PubDex release: ${record_answer#release_record.py: }"
PRECEDING="$(tail -n 1 <<<"$record_answer")"
record_fields="$("$PY" -I "$ROOT/DB/release_record.py" field "$PRECEDING" images.application images.postgres \
  chart.version chart.commit schema_changes 2>&1)" \
  || prerequisite "cannot read the entry of PubDex $PRECEDING in the release record: ${record_fields#release_record.py: }"
{ read -r BASE_APP_REF; read -r BASE_PG_REF; read -r PRECEDING_CHART_VERSION; read -r PRECEDING_CHART_COMMIT
  read -r preceding_changes; } <<<"$record_fields"

# schema_lines <record's JSON list> | --files <directory>: "<number> <name>
# <SHA-256>" of each schema change, one a line, or "none" for none -- a
# database without the record of schema changes.
schema_lines() {
  "$PY" -I - "$@" <<'PY'
import hashlib, json, pathlib, re, sys
if sys.argv[1] == "--files":
    rows = []
    for path in sorted(pathlib.Path(sys.argv[2]).glob("*.sql")):
        if not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", path.name):
            sys.exit(f"{path.name} is not named as a schema change, NNNN_<name>.sql")
        rows.append((int(path.name[:4]), path.stem, hashlib.sha256(path.read_bytes()).hexdigest()))
else:
    rows = [(change["number"], change["name"], change["sha256"]) for change in json.loads(sys.argv[1])]
print("\n".join(f"{number} {name} {digest}" for number, name, digest in rows) if rows else "none")
PY
}
PRECEDING_RECORD="$(schema_lines "$preceding_changes" 2>&1)" \
  || prerequisite "cannot read the schema changes of PubDex $PRECEDING: $PRECEDING_RECORD"
CHECKOUT_RECORD="$(schema_lines --files "$ROOT/DB/schema" 2>&1)" \
  || prerequisite "cannot read this checkout's schema changes in DB/schema/: $CHECKOUT_RECORD"
[ "$CHECKOUT_RECORD" != none ] || prerequisite "DB/schema/ holds no schema change; the schema upgrade has nothing to show."

save_caller_environment  # PATH, HOME and the Docker selection, for isolated() and kind

# in_checkout <function> [arguments]: a function of DB/lib/release_identity.sh,
# or git itself, run with isolated()'s PATH and HOME and nothing else.
in_checkout() {
  ( clear_exports
    export PATH="$SAVED_PATH" HOME="$SAVED_HOME" LC_ALL=C
    "$@" )
}

# Revision 1's chart is charts/pubdex/ at the commit the release record
# names, read from this clone's history in step 1; a shallow clone lacks it.
in_checkout command -v git >/dev/null 2>&1 \
  || prerequisite "git was not found on PATH; it reads the chart of PubDex $PRECEDING from the repository's history."
if ! in_checkout git -C "$ROOT" cat-file -e "$PRECEDING_CHART_COMMIT^{commit}" 2>/dev/null; then
  prerequisite "this clone does not have the commit $PRECEDING_CHART_COMMIT, whose charts/pubdex/ is the chart of PubDex $PRECEDING, revision 1 of each upgrade. A shallow clone lacks it; fetch the whole history, for example with:" \
    "git fetch --unshallow"
fi
# A test release's version is <VERSION>-test.<name>, for a VERSION without a
# pre-release part.
CHECKOUT_VERSION="$(head -n 1 "$ROOT/VERSION" 2>/dev/null | tr -d '[:space:]')" || CHECKOUT_VERSION=""
if ! [[ "$CHECKOUT_VERSION" =~ $RELEASE_VERSION_RE ]] || [[ "$CHECKOUT_VERSION" == *-* ]]; then
  prerequisite "VERSION holds '$CHECKOUT_VERSION', not a release version without a pre-release part; the test releases' versions are <VERSION>-test.<name>."
fi

# --- the run's identity, cleanup and private directory -------------------------------------

STARTED=$SECONDS
RUN_ID="$(date +%Y%m%d%H%M%S)-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
CLUSTER="pubdex-cluster-$RUN_ID"       # the newest supported release's cluster
NETWORK="$CLUSTER-net"
FLOOR_CLUSTER="pubdex-floor-$RUN_ID"   # the oldest supported release's
FLOOR_NETWORK="$FLOOR_CLUSTER-net"
IMAGE_REPOSITORY="pubdex-cluster-acceptance"
POSTGRES_REPOSITORY="pubdex-cluster-acceptance-postgres"
APP_TAG="$IMAGE_REPOSITORY:$RUN_ID"
PG_TAG="$POSTGRES_REPOSITORY:$RUN_ID"
# Test releases A and B1: names of lower-case letters and digits, release.sh's
# versions <VERSION>-test.<name>, and the local tags release.sh gives them.
A_VERSION="$CHECKOUT_VERSION-test.a${RUN_ID//-/}"
B1_VERSION="$CHECKOUT_VERSION-test.b1${RUN_ID//-/}"
TEST_VERSIONS=("$A_VERSION" "$B1_VERSION")
RUN_TAGS=()          # image tags this run created, removed by the cleanup
NODE_VOLUMES=""      # the node containers' volumes, which kind delete removes with them
CLAIMED=0            # set once nothing of the run's names existed before it
CLUSTER_STARTED=0    # set when kind create cluster starts the first cluster, cleared once it is deleted
FLOOR_STARTED=0      # set when kind create cluster starts the floor's
WORK=""
JOB_PID=""           # a tool running under job()
FORWARD_PID=""       # a kubectl port-forward
# The cluster the helpers act on -- its node, network and the kubeconfig and
# context of every kubectl and helm call -- set by on_cluster and use_kubeconfig.
ACTIVE_CLUSTER="$CLUSTER"
ACTIVE_NETWORK="$NETWORK"
NODE="$CLUSTER-control-plane"
KUBECONFIG_FILE=""
CONTEXT=""
CONTEXT_VERIFIED=0   # set while the kubeconfig in use is known to name this run's cluster only
VERIFIED=""          # the "<kubeconfig> <context>" pairs assert_own_cluster accepted, one a line
FLOOR_AUDIT=0        # set once the floor's audit log exists

stop_process() {  # stop_process <pid>: TERM, then KILL ten seconds later
  local pid="$1" grace
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "$pid" 2>/dev/null || true
  grace=$((SECONDS + 10))
  while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$grace" ]; do sleep 0.2; done
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

owned_containers() {  # the containers of either cluster or of the run; fails when Docker cannot say
  local version ids id sources
  docker ps -aq --filter "label=$KIND_LABEL=$CLUSTER" && docker ps -aq --filter "label=$KIND_LABEL=$FLOOR_CLUSTER" \
    && docker ps -aq --filter "label=$LABEL_KEY=$RUN_ID" || return 1
  # A container of a test release's image -- a probe of release.sh's -- carries
  # its version label; the SBOM tool's mounts the run's directory.
  for version in "${TEST_VERSIONS[@]}"; do
    docker ps -aq --no-trunc --filter "label=$VERSION_LABEL=$version" || return 1
  done
  [ -n "$WORK" ] || return 0
  ids="$(docker ps -aq --no-trunc --filter "ancestor=$PINNED_SYFT_IMAGE")" || return 1
  for id in $ids; do
    sources="$(docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$id" 2>/dev/null)" || continue
    if grep -qF -- "$WORK/" <<<"$sources"; then echo "$id"; fi
  done
}
test_images() {  # the ids of every image labelled with a test release's version, dangling ones included
  local version
  for version in "${TEST_VERSIONS[@]}"; do
    docker image ls -aq --no-trunc --filter "label=$VERSION_LABEL=$version" || return 1
  done
}
owned_networks() {  # the run's networks, by their names or their label
  local name
  docker network ls -q --filter "label=$LABEL_KEY=$RUN_ID" || return 1
  for name in "$NETWORK" "$FLOOR_NETWORK"; do
    if docker network inspect "$name" >/dev/null 2>&1; then echo "$name"; fi
  done
}
left_volumes() {  # the nodes' volumes that still exist
  local volume
  for volume in $NODE_VOLUMES; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then echo "$volume"; fi
  done
}
left_tags() {
  local tag
  for tag in ${RUN_TAGS[@]+"${RUN_TAGS[@]}"}; do
    if docker image inspect "$tag" >/dev/null 2>&1; then echo "$tag"; fi
  done
}

remove_owned() {  # the clusters, then their containers' remains, the networks, the image tags and the test releases' images
  local id
  if [ "$CLUSTER_STARTED" = 1 ]; then
    job 300 kind_ delete cluster --name "$CLUSTER" --kubeconfig "$KIND_KUBECONFIG" > "$WORK/kind-delete.log" 2>&1 || true
  fi
  if [ "$FLOOR_STARTED" = 1 ]; then
    job 300 kind_ delete cluster --name "$FLOOR_CLUSTER" --kubeconfig "$FLOOR_KUBECONFIG" >> "$WORK/kind-delete.log" 2>&1 || true
  fi
  for id in $(owned_containers 2>/dev/null | sort -u); do
    docker rm -f -v "$id" >/dev/null 2>&1 || true
  done
  for id in $NODE_VOLUMES; do
    docker volume rm "$id" >/dev/null 2>&1 || true
  done
  for id in $(owned_networks 2>/dev/null | sort -u); do
    docker network rm "$id" >/dev/null 2>&1 || true
  done
  for id in ${RUN_TAGS[@]+"${RUN_TAGS[@]}"}; do
    docker image rm "$id" >/dev/null 2>&1 || true
  done
  for id in $(test_images 2>/dev/null | sort -u); do
    docker image rm -f "$id" >/dev/null 2>&1 || true
  done
}

confirm_removed() {  # nothing of it left, or a warning with the commands that find it
  local containers networks volumes tags images tag
  if ! containers="$(owned_containers 2>/dev/null)" || ! networks="$(owned_networks 2>/dev/null)" \
     || ! volumes="$(left_volumes)" || ! tags="$(left_tags)" || ! images="$(test_images 2>/dev/null)" \
     || [ -n "$containers$networks$volumes$tags$images" ]; then
    echo "WARNING: could not confirm that run $RUN_ID left nothing behind. Check with" >&2
    echo "  docker ps -a --filter label=$KIND_LABEL=$CLUSTER" >&2
    echo "  docker ps -a --filter label=$KIND_LABEL=$FLOOR_CLUSTER" >&2
    echo "  docker ps -a --filter label=$LABEL_KEY=$RUN_ID" >&2
    echo "  docker network ls --filter label=$LABEL_KEY=$RUN_ID" >&2
    echo "  docker network inspect $NETWORK" >&2
    echo "  docker network inspect $FLOOR_NETWORK" >&2
    for tag in $NODE_VOLUMES; do echo "  docker volume inspect $tag" >&2; done
    for tag in ${RUN_TAGS[@]+"${RUN_TAGS[@]}"}; do echo "  docker image ls $tag" >&2; done
    for tag in "${TEST_VERSIONS[@]}"; do
      echo "  docker image ls -a --filter label=$VERSION_LABEL=$tag" >&2
      echo "  docker ps -a --filter label=$VERSION_LABEL=$tag" >&2
    done
    return 1
  fi
}

cleanup() {
  local rc=$? confirmed=1
  trap '' INT TERM HUP  # a repeated interrupt must not cut the cleanup short
  trap - EXIT
  exec 1>&3 2>&4
  stop_process "$FORWARD_PID"
  stop_process "$JOB_PID"
  JOB_PID=""
  if [ "$CLAIMED" = 1 ]; then
    remove_owned
    if ! confirm_removed; then
      confirmed=""
      [ "$rc" -ne 0 ] || rc=1  # an earlier failure keeps its own status
    fi
  fi
  if [ -n "$WORK" ]; then
    rm -rf -- "$WORK" 2>/dev/null || true
    if [ -e "$WORK" ]; then
      echo "WARNING: could not remove the run's temporary directory $WORK, which holds its kubeconfigs, the operator's token among them, secret files and certificates." >&2
      confirmed=""
      [ "$rc" -ne 0 ] || rc=1
    fi
  fi
  if [ "$CLAIMED" = 1 ] && [ -n "$confirmed" ]; then
    echo "      cleanup: the clusters, their networks and volumes, the run's image tags, the test releases' images and files, kubeconfigs with the operator's token, secret files and certificates are gone"
  fi
  if [ "$rc" -eq 0 ]; then
    echo "PASS: the cluster acceptance test passed in $((SECONDS - STARTED))s."
  else
    echo "      the run failed with status $rc after $((SECONDS - STARTED))s" >&2
  fi
  exit "$rc"
}

exec 3>&1 4>&2  # the run's own output, for the cleanup
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# job <seconds> <command> [arguments]: the command as a background job of
# its own, bounded in time; a signal to this script interrupts the wait at
# once, and the cleanup stops the job. Its standard input is empty.
job() {
  local limit="$1" status=0
  shift
  ( ISOLATED_IN_PLACE=1; "$@" ) &
  JOB_PID=$!
  bounded_wait "$limit" "$JOB_PID" "$1" || status=$?
  JOB_PID=""
  return "$status"
}
# job_input <seconds> <command> [arguments]: as job, with this shell's
# standard input -- the file the caller redirected into it -- as the command's.
job_input() {
  local limit="$1" status=0
  shift
  ( ISOLATED_IN_PLACE=1; "$@" ) <&0 &
  JOB_PID=$!
  bounded_wait "$limit" "$JOB_PID" "$1" || status=$?
  JOB_PID=""
  return "$status"
}

# The tools, each with a cleared environment; under job() each replaces the
# job's process, so that stopping the job stops the tool.
run_cleared() {  # run_cleared <environment array name> <tool> [arguments]
  local -n settings="$1"
  shift
  if [ -n "${ISOLATED_IN_PLACE:-}" ]; then exec env -i "${settings[@]}" "$@"; fi
  ( exec env -i "${settings[@]}" "$@" )
}
kind_() { run_cleared KIND_ENV "$KIND" "$@"; }
kubectl_() {
  [ "$CONTEXT_VERIFIED" = 1 ] || { echo "$PROG: kubectl before the kubeconfig was checked" >&2; return 1; }
  run_cleared KUBE_ENV "$KUBECTL" --kubeconfig "$KUBECONFIG_FILE" --context "$CONTEXT" "$@"
}
helm_() {
  [ "$CONTEXT_VERIFIED" = 1 ] || { echo "$PROG: helm before the kubeconfig was checked" >&2; return 1; }
  run_cleared KUBE_ENV "$HELM" --kubeconfig "$KUBECONFIG_FILE" --kube-context "$CONTEXT" "$@"
}
kn() { local namespace="$1"; shift; kubectl_ --namespace "$namespace" --request-timeout=60s "$@"; }
k() { kn "$NAMESPACE" "$@"; }

# use_kubeconfig <kubeconfig> <context>: every later kubectl and helm call
# names these; they are refused until assert_own_cluster has accepted the pair.
use_kubeconfig() {
  KUBECONFIG_FILE="$1"
  CONTEXT="$2"
  # shellcheck disable=SC2034  # read through run_cleared's name reference
  KUBE_ENV=(PATH="$SAVED_PATH" LC_ALL=C HOME="$WORK/home" TMPDIR="$WORK/tmp" KUBECONFIG="$KUBECONFIG_FILE"
            KUBECACHEDIR="$WORK/kube-cache" DOCKER_CONFIG="$WORK/docker" HELM_CONFIG_HOME="$WORK/helm/config"
            HELM_CACHE_HOME="$WORK/helm/cache" HELM_DATA_HOME="$WORK/helm/data")
  CONTEXT_VERIFIED=0
  if grep -qxF -- "$1 $2" <<<"$VERIFIED"; then CONTEXT_VERIFIED=1; fi
}
# on_cluster <cluster> <network>: the cluster whose node the checks inspect.
on_cluster() { ACTIVE_CLUSTER="$1"; ACTIVE_NETWORK="$2"; NODE="$1-control-plane"; }
# kind runs docker: the caller's Docker client configuration and daemon
# selection, but the run's HOME, so that it finds no kubeconfig of the
# caller's; a new cluster's node joins <network>.
set_kind_env() {
  # shellcheck disable=SC2034
  KIND_ENV=(PATH="$SAVED_PATH" LC_ALL=C HOME="$WORK/home" TMPDIR="$WORK/tmp" DOCKER_CONFIG="$SAVED_HOME/.docker"
            KIND_EXPERIMENTAL_DOCKER_NETWORK="$1" ${DOCKER_SELECTION[@]+"${DOCKER_SELECTION[@]}"})
}

fail() {
  local refused
  echo "FAIL: $*" >&2
  if [ "$FLOOR_AUDIT" = 1 ] && refused="$(audit_refusals 2>/dev/null)" && [ -n "$refused" ]; then
    echo "--- the API server refused the operator (the floor's audit log):" >&2
    echo "$refused" >&2
  fi
  if [ "$CONTEXT_VERIFIED" = 1 ]; then
    echo "--- diagnostics for run $RUN_ID, namespace $NAMESPACE, context $CONTEXT" >&2
    k get pods,jobs,events -o wide 2>&1 | tail -n 40 >&2 || true
  fi
  exit 1
}
ok() { echo "  ok  $*"; }
step() { echo "[$1/$STEPS] $2 (at $((SECONDS - STARTED))s)"; }

# kube <what> <namespace> <kubectl arguments>: kubectl in that namespace; a
# failure or a warning of the API server -- Pod Security's included -- fails
# the run.
kube() {
  local what="$1" namespace="$2"
  shift 2
  if ! kn "$namespace" "$@" > "$WORK/kubectl.out" 2> "$WORK/kubectl.err"; then
    cat "$WORK/kubectl.err" >&2
    fail "$what failed"
  fi
  if grep -q '^Warning: ' "$WORK/kubectl.err"; then cat "$WORK/kubectl.err" >&2; fail "$what: the API server warned"; fi
}

echo "Cluster acceptance run $RUN_ID: cluster $CLUSTER, network $NETWORK"
echo "      the floor: cluster $FLOOR_CLUSTER, network $FLOOR_NETWORK"
echo "      docker $engine_version, compose $compose_version"

# Never take over what exists already, however unlikely a collision.
if ! existing="$(owned_containers && owned_networks)"; then
  echo "ERROR: Docker cannot list the containers and networks of run $RUN_ID; nothing was created." >&2
  exit 1
fi
for tag in "$APP_TAG" "$PG_TAG" "pubdex-release:$A_VERSION" "pubdex-postgres-release:$A_VERSION" \
           "pubdex-release:$B1_VERSION" "pubdex-postgres-release:$B1_VERSION"; do
  if docker image inspect "$tag" >/dev/null 2>&1; then existing+=" image $tag"; fi
done
if ! labelled="$(test_images)"; then
  echo "ERROR: Docker cannot list the images of run $RUN_ID's test releases; nothing was created." >&2
  exit 1
fi
existing+="$labelled"
if [ -n "$existing" ]; then
  echo "ERROR: a cluster, network, image tag or test release of run $RUN_ID exists already; not touching it." >&2
  exit 1
fi
CLAIMED=1
WORK="$(mktemp -d "${TMPDIR:-/tmp}/pubdex-cluster.XXXXXX")"
chmod 700 "$WORK"
mkdir -p "$WORK/home" "$WORK/tmp" "$WORK/kube-cache" "$WORK/docker" "$WORK/helm/config" "$WORK/helm/cache" \
  "$WORK/helm/data" "$WORK/floor"
KIND_KUBECONFIG="$WORK/kubeconfig"                    # kind's, for the first cluster
FLOOR_KUBECONFIG="$WORK/floor/kubeconfig"             # kind's, for the floor's: its administrator
OPERATOR_KUBECONFIG="$WORK/floor/operator-kubeconfig"  # the floor's operator alone, with its token
OPERATOR_CONTEXT="$OPERATOR"
use_kubeconfig "$KIND_KUBECONFIG" "kind-$CLUSTER"
set_kind_env "$NETWORK"

# The versions the pinned tools report, in the run's own environment.
helm_version="$(run_cleared KUBE_ENV "$HELM" version --template '{{.Version}}' 2>/dev/null)" || helm_version=""
kubectl_version="$(run_cleared KUBE_ENV "$KUBECTL" version --client 2>/dev/null | sed -n 's/^Client Version: //p')" || kubectl_version=""
kind_version="$(kind_ version 2>/dev/null)" || kind_version=""
[ "$helm_version" = "v$PINNED_HELM_VERSION" ] || fail "helm reports the version '$helm_version', not the pinned v$PINNED_HELM_VERSION"
[ "$kubectl_version" = "v$PINNED_KUBECTL_VERSION" ] || fail "kubectl reports the version '$kubectl_version', not the pinned v$PINNED_KUBECTL_VERSION"
[[ "$kind_version" == "kind v$PINNED_KIND_VERSION "* ]] || fail "kind reports '$kind_version', not the pinned v$PINNED_KIND_VERSION"
echo "      helm $helm_version, kubectl $kubectl_version, $kind_version, $(isolated openssl version 2>/dev/null)"
echo "      node images $PINNED_KIND_NODE_IMAGE"
echo "                  $PINNED_KIND_FLOOR_NODE_IMAGE (the floor)"
echo "      revision 1: PubDex $PRECEDING, the preceding release (releases/$PRECEDING/release.json):"
echo "        its chart $PRECEDING_CHART_VERSION, charts/pubdex/ at $PRECEDING_CHART_COMMIT"
echo "        its images $BASE_APP_REF"
echo "                   $BASE_PG_REF"
echo "        its schema changes: $(cut -d' ' -f2 <<<"$PRECEDING_RECORD" | paste -sd' ' -)"
echo "      this checkout's schema changes: $(cut -d' ' -f2 <<<"$CHECKOUT_RECORD" | paste -sd' ' -)"

# --- 1. the images ---------------------------------------------------------------------------

label_of() { docker image inspect --format "{{index .Config.Labels \"$2\"}}" "$1"; }

# check_labels <tag> <version> <revision>: the image's release-version and
# revision labels are exactly these (an empty expectation is not checked).
check_labels() {
  local version revision
  version="$(label_of "$1" org.opencontainers.image.version)" \
    && revision="$(label_of "$1" org.opencontainers.image.revision)" || fail "cannot read the labels of $1"
  echo "      $1: id $(docker image inspect --format '{{.Id}}' "$1"), version $version, revision $revision"
  if [ -n "$2" ] && [ "$version" != "$2" ]; then fail "$1 has the release-version label '$version', expected '$2'"; fi
  if [ -n "$3" ] && [ "$revision" != "$3" ]; then fail "$1 has the revision label '$revision', expected '$3'"; fi
}

step 1 "Selecting the images under test and the preceding release's chart, and building test releases A and B1..."
if [ -z "$image_ref" ]; then
  RUN_TAGS+=("$APP_TAG" "$PG_TAG")
  # The labels build_image.sh has to record, by its rules (stage 6 checks the same).
  expected_revision="$(in_checkout checkout_revision "$ROOT" "${IMAGE_CONTEXT_PATHSPECS[@]}")"
  expected_version="$(head -n 1 "$ROOT/VERSION" 2>/dev/null | tr -d '[:space:]')" || expected_version=""
  if ! job 3600 isolated bash "$ROOT/build_image.sh" --tag "$APP_TAG" --postgres-tag "$PG_TAG" > "$WORK/build.log" 2>&1; then
    tail -n 40 "$WORK/build.log" >&2
    fail "build_image.sh failed"
  fi
  ok "built $APP_TAG and $PG_TAG from this checkout with build_image.sh ($((SECONDS - STARTED))s)"
  check_labels "$APP_TAG" "$expected_version" "$expected_revision"
  check_labels "$PG_TAG" "$expected_version" "$expected_revision"
  ok "both carry this checkout's VERSION $expected_version and revision $expected_revision"
else
  for pair in "$image_ref $APP_TAG $BASE_APP_REF" "$postgres_ref $PG_TAG $BASE_PG_REF"; do
    read -r given tag published <<<"$pair"
    docker image inspect "$given" >/dev/null 2>&1 || fail "Docker has no image $given; pull it first. Nothing is built in its place."
    digests="$(docker image inspect --format '{{join .RepoDigests "\n"}}' "$given")" || fail "cannot read the digests of $given"
    if [[ "$given" == *@sha256:* ]] && ! grep -q "@${given##*@}\$" <<<"$digests"; then
      fail "the image Docker resolved for $given does not carry the digest ${given##*@}"
    fi
    if grep -q "@${published##*@}\$" <<<"$digests"; then
      fail "$given is the published image $published, which revision 1 runs; the upgrade to the images under test has to change it"
    fi
    RUN_TAGS+=("$tag")
    docker tag "$given" "$tag" || fail "cannot tag $given as $tag"
    echo "      $given as $tag"
    check_labels "$tag" "$expect_version" "$expect_revision"
  done
  ok "the given images, under the run's tags"
fi
# Revision 1's chart, as the preceding release shipped it: git archive reads
# it from the object store and writes nothing under .git/.
PRECEDING_CHART="$WORK/preceding/charts/pubdex"
PRECEDING_EXAMPLE="$PRECEDING_CHART/examples/values-external.yaml"
mkdir "$WORK/preceding"
in_checkout git -C "$ROOT" archive --format=tar "$PRECEDING_CHART_COMMIT" charts/pubdex > "$WORK/preceding.tar" \
  2> "$WORK/archive.err" || { cat "$WORK/archive.err" >&2; fail "git archive cannot read charts/pubdex at $PRECEDING_CHART_COMMIT"; }
tar -x -f "$WORK/preceding.tar" -C "$WORK/preceding" || fail "cannot unpack the chart of PubDex $PRECEDING"
rm -f -- "$WORK/preceding.tar"
chart_version="$("$PY" -I -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["version"])' \
  "$PRECEDING_CHART/Chart.yaml" 2>&1)" || fail "cannot read the chart of PubDex $PRECEDING: $chart_version"
[ "$chart_version" = "$PRECEDING_CHART_VERSION" ] \
  || fail "charts/pubdex/ at $PRECEDING_CHART_COMMIT is chart $chart_version, not $PRECEDING_CHART_VERSION, which the release record names"
names="$(external_names "$PRECEDING_EXAMPLE" 2>&1)" || fail "cannot read the external example of PubDex $PRECEDING: $names"
[ "$names" = "$external_facts" ] \
  || fail "the external example of PubDex $PRECEDING names other Secrets or ConfigMaps ($names) than this checkout's ($external_facts)"
ok "revision 1's chart: charts/pubdex/ of PubDex $PRECEDING at $PRECEDING_CHART_COMMIT, chart $chart_version, from this clone's history"

# test_release <version> [--overlay b1]: release.sh builds and verifies the
# test release of that version into the run's directory; sets REL_APP and
# REL_PG, the local tags of its images from IMAGES, REL_PACKAGE, its chart
# package file, REL_RECORD, the schema changes its source archive holds, and
# REL_TRANSFORM, its application image's transform version.
test_release() {
  local version="$1" name started=$SECONDS out
  shift
  name="${version##*-test.}"
  out="$WORK/release-$name"
  if ! job 3600 isolated env TMPDIR="$WORK/tmp" bash "$ROOT/release.sh" build --test "$name" "$@" --out "$out" \
       > "$WORK/release-$name.log" 2>&1; then
    tail -n 40 "$WORK/release-$name.log" >&2
    fail "release.sh build --test $name${*:+ $*} failed"
  fi
  REL_APP="$(sed -n 's/^application \([^ ]*\) .*$/\1/p' "$out/IMAGES" 2>/dev/null)" || REL_APP=""
  REL_PG="$(sed -n 's/^postgres \([^ ]*\) .*$/\1/p' "$out/IMAGES" 2>/dev/null)" || REL_PG=""
  REL_PACKAGE="$out/pubdex-$version.tgz"
  [ "$REL_APP" = "pubdex-release:$version" ] && [ "$REL_PG" = "pubdex-postgres-release:$version" ] \
    && [ -f "$REL_PACKAGE" ] && [ -f "$out/pubdex-$version-source.tar.gz" ] \
    || fail "release.sh build --test $name did not write the images, chart package and source archive of PubDex $version: IMAGES names '$REL_APP' and '$REL_PG'"
  mkdir "$WORK/source-$name"
  tar -x -z -f "$out/pubdex-$version-source.tar.gz" -C "$WORK/source-$name" "pubdex-$version/DB/schema" \
    || fail "cannot read DB/schema/ from the source archive of PubDex $version"
  REL_RECORD="$(schema_lines --files "$WORK/source-$name/pubdex-$version/DB/schema" 2>&1)" \
    || fail "cannot read the schema changes of PubDex $version: $REL_RECORD"
  REL_TRANSFORM="$(label_of "$REL_APP" "$TRANSFORM_LABEL")" && [ -n "$REL_TRANSFORM" ] \
    || fail "$REL_APP has no transform-version label"
  check_labels "$REL_APP" "$version" ""
  check_labels "$REL_PG" "$version" ""
  ok "release.sh build --test $name${*:+ $*}: built and verified in $((SECONDS - started))s; chart package ${REL_PACKAGE##*/}; schema changes $(cut -d' ' -f2 <<<"$REL_RECORD" | paste -sd' ' -); transform version $REL_TRANSFORM"
}
RUN_TAGS+=("pubdex-release:$A_VERSION" "pubdex-postgres-release:$A_VERSION")
test_release "$A_VERSION"
A_APP="$REL_APP" A_PG="$REL_PG" A_PACKAGE="$REL_PACKAGE" A_RECORD="$REL_RECORD" A_TRANSFORM="$REL_TRANSFORM"
RUN_TAGS+=("pubdex-release:$B1_VERSION" "pubdex-postgres-release:$B1_VERSION")
test_release "$B1_VERSION" --overlay b1
B1_APP="$REL_APP" B1_PG="$REL_PG" B1_PACKAGE="$REL_PACKAGE" B1_RECORD="$REL_RECORD" B1_TRANSFORM="$REL_TRANSFORM"
[ "$A_RECORD" = "$CHECKOUT_RECORD" ] \
  || fail "test release A ships the schema changes '$(cut -d' ' -f2 <<<"$A_RECORD" | paste -sd' ' -)', not this checkout's"
[ "$(head -n -1 <<<"$B1_RECORD")" = "$CHECKOUT_RECORD" ] \
  && [ "$(grep -c . <<<"$B1_RECORD")" -eq $(($(grep -c . <<<"$CHECKOUT_RECORD") + 1)) ] \
  || fail "test release B1 ships the schema changes '$(cut -d' ' -f2 <<<"$B1_RECORD" | paste -sd' ' -)', not this checkout's and one more"
B1_CHANGE="$(tail -n 1 <<<"$B1_RECORD" | cut -d' ' -f2)"
[ "$B1_TRANSFORM" != "$A_TRANSFORM" ] || fail "test releases A and B1 carry the same transform version, $A_TRANSFORM"
ok "test release B1 adds the schema change $B1_CHANGE to A's and carries the transform version $B1_TRANSFORM, A $A_TRANSFORM"

# --- 2. the cluster ----------------------------------------------------------------------------

# assert_own_cluster: the kubeconfig in use holds its context alone, with
# one cluster and one user, and that cluster's server is the loopback port
# Docker publishes for the active cluster's control-plane container on its
# network. Only then do kubectl and helm accept that kubeconfig.
assert_own_cluster() {
  local server binding labelled networks
  [ -f "$KUBECONFIG_FILE" ] && [ ! -L "$KUBECONFIG_FILE" ] || fail "there is no kubeconfig at $KUBECONFIG_FILE"
  server="$("$PY" -I - "$KUBECONFIG_FILE" "$CONTEXT" <<'PY'
import sys, yaml
config = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
contexts = {item["name"]: item["context"] for item in config.get("contexts") or []}
clusters = {item["name"]: item["cluster"] for item in config.get("clusters") or []}
users = [item["name"] for item in config.get("users") or []]
if list(contexts) != [sys.argv[2]] or config.get("current-context") != sys.argv[2]:
    sys.exit(f"contexts {sorted(contexts)}, current {config.get('current-context')!r}")
if len(clusters) != 1 or contexts[sys.argv[2]]["cluster"] not in clusters:
    sys.exit(f"clusters {sorted(clusters)}")
if users != [contexts[sys.argv[2]]["user"]]:
    sys.exit(f"users {sorted(users)}")
print(clusters[contexts[sys.argv[2]]["cluster"]]["server"])
PY
)" || fail "the kubeconfig $KUBECONFIG_FILE holds more or other than the context $CONTEXT; not using it"
  labelled="$(docker inspect --format "{{index .Config.Labels \"$KIND_LABEL\"}}" "$NODE" 2>/dev/null)" || labelled=""
  networks="$(docker inspect --format '{{range $name, $value := .NetworkSettings.Networks}}{{$name}} {{end}}' "$NODE" 2>/dev/null)" \
    || networks=""
  binding="$(docker port "$NODE" 6443/tcp 2>/dev/null)" || binding=""
  [ "$labelled" = "$ACTIVE_CLUSTER" ] && [ "$networks" = "$ACTIVE_NETWORK " ] \
    || fail "the node container $NODE is not this run's (label '$labelled', networks '$networks')"
  [[ "$binding" =~ ^127\.0\.0\.1:[0-9]+$ ]] || fail "$NODE publishes its API server on '$binding', not on one loopback port"
  [ "$server" = "https://$binding" ] \
    || fail "the kubeconfig's server $server is not this run's cluster at https://$binding; refusing every kubectl and helm call"
  VERIFIED+="$KUBECONFIG_FILE $CONTEXT"$'\n'
  CONTEXT_VERIFIED=1
  ok "the kubeconfig names $CONTEXT alone, at $server, the loopback port of $NODE"
}

# create_cluster <cluster> <network> <node image> <kubeconfig> [kind options]:
# the network, labelled with the run, and on it the kind cluster, whose
# kubeconfig is then checked and used; prints the API server's version.
create_cluster() {
  local cluster="$1" network="$2" image="$3" kubeconfig="$4" mtu started options
  shift 4
  mtu="$(docker network inspect bridge --format '{{index .Options "com.docker.network.driver.mtu"}}' 2>/dev/null)" || mtu=""
  options=(--driver bridge --label "$LABEL_KEY=$RUN_ID" --opt com.docker.network.bridge.enable_ip_masquerade=true)
  if [[ "$mtu" =~ ^[0-9]+$ ]]; then options+=(--opt "com.docker.network.driver.mtu=$mtu"); fi
  docker network create "${options[@]}" "$network" >/dev/null || fail "cannot create the network $network"
  set_kind_env "$network"
  on_cluster "$cluster" "$network"
  use_kubeconfig "$kubeconfig" "kind-$cluster"
  started=$SECONDS
  if [ "$cluster" = "$CLUSTER" ]; then CLUSTER_STARTED=1; else FLOOR_STARTED=1; fi
  if ! job 600 kind_ create cluster --name "$cluster" --image "$image" --kubeconfig "$kubeconfig" "$@" \
         --wait 180s > "$WORK/kind-create.log" 2>&1; then
    tail -n 30 "$WORK/kind-create.log" >&2
    fail "kind create cluster failed"
  fi
  NODE_VOLUMES+="$(docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$NODE" 2>/dev/null) " \
    || true
  ok "kind created $cluster in $((SECONDS - started))s"
  assert_own_cluster
  server_version="$(kubectl_ version --output json 2>/dev/null \
    | "$PY" -I -c 'import json, sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')" \
    || fail "cannot read the API server's version"
  echo "      Kubernetes $server_version; node $(docker inspect --format '{{.Config.Image}} {{.Image}}' "$NODE")"
}

step 2 "Creating the kind cluster on a network of its own..."
create_cluster "$CLUSTER" "$NETWORK" "$PINNED_KIND_NODE_IMAGE" "$KIND_KUBECONFIG"

# --- 3. the images in the node ---------------------------------------------------------------

node_image_id() {  # node_image_id <reference>: the image's id and repository digests in the node
  docker exec "$NODE" crictl inspecti --output json "$1" 2>/dev/null \
    | "$PY" -I -c 'import json, sys; status = json.load(sys.stdin)["status"]; print(status["id"], *(status.get("repoDigests") or []))'
}

load_images_under_test() {  # the two images under test into the active cluster's node, by kind load
  local started=$SECONDS tag
  if ! job 900 kind_ load docker-image --name "$ACTIVE_CLUSTER" "$APP_TAG" "$PG_TAG" > "$WORK/kind-load.log" 2>&1; then
    # kind load docker-image cannot read every image store; an archive it can.
    tail -n 5 "$WORK/kind-load.log" >&2
    echo "      kind load docker-image failed; loading an archive of the images instead"
    docker save --output "$WORK/images.tar" "$APP_TAG" "$PG_TAG" || fail "docker save failed"
    job 900 kind_ load image-archive --name "$ACTIVE_CLUSTER" "$WORK/images.tar" >> "$WORK/kind-load.log" 2>&1 \
      || { tail -n 20 "$WORK/kind-load.log" >&2; fail "kind load image-archive failed"; }
    rm -f -- "$WORK/images.tar"
  fi
  for tag in "$APP_TAG" "$PG_TAG"; do
    node_image_id "docker.io/library/$tag" > "$WORK/node-image.txt" || fail "the node has no image $tag"
    echo "      $tag: $(cut -d' ' -f1 "$WORK/node-image.txt") in the node"
  done
  ok "the node has the two images under test ($((SECONDS - started))s)"
}

step 3 "Loading the images under test into the node, pulling the published ones, and loading the test releases'..."
load_images_under_test
started=$SECONDS
for ref in "$BASE_APP_REF" "$BASE_PG_REF"; do
  if ! job 600 docker exec "$NODE" crictl pull "$ref" > "$WORK/pull.log" 2>&1; then
    tail -n 5 "$WORK/pull.log" >&2
    fail "the cluster could not pull the published image $ref without credentials. Revision 1 of each release runs the published images, so this test needs access to ${ref%%/*} from the kind node; nothing was skipped."
  fi
  node_image_id "$ref" > "$WORK/node-image.txt" || fail "the node has no image $ref after the pull"
  grep -q " ${ref%%:*}@${ref##*@}\( \|\$\)" "$WORK/node-image.txt" \
    || fail "the image the node pulled for $ref does not carry the digest ${ref##*@}: $(cat "$WORK/node-image.txt")"
  echo "      $ref: $(cut -d' ' -f1 "$WORK/node-image.txt") in the node"
done
[ "$(node_image_id "docker.io/library/$APP_TAG" | cut -d' ' -f1)" != "$(node_image_id "$BASE_APP_REF" | cut -d' ' -f1)" ] \
  && [ "$(node_image_id "docker.io/library/$PG_TAG" | cut -d' ' -f1)" != "$(node_image_id "$BASE_PG_REF" | cut -d' ' -f1)" ] \
  || fail "an image under test has the id of the published image it should replace; the upgrade would change nothing"
ok "the node pulled the published images by digest, anonymously ($((SECONDS - started))s); the images under test have other ids"
# The test releases' images, as the images under test are loaded: in
# external mode the read service, the ingestion and the schema upgrade run
# the application image, and the setup Job the PostgreSQL image's psql.
started=$SECONDS
if ! job 900 kind_ load docker-image --name "$ACTIVE_CLUSTER" "$A_APP" "$A_PG" "$B1_APP" "$B1_PG" \
     > "$WORK/kind-load-releases.log" 2>&1; then
  tail -n 5 "$WORK/kind-load-releases.log" >&2
  echo "      kind load docker-image failed; loading an archive of the images instead"
  docker save --output "$WORK/images.tar" "$A_APP" "$A_PG" "$B1_APP" "$B1_PG" || fail "docker save failed"
  job 900 kind_ load image-archive --name "$ACTIVE_CLUSTER" "$WORK/images.tar" >> "$WORK/kind-load-releases.log" 2>&1 \
    || { tail -n 20 "$WORK/kind-load-releases.log" >&2; fail "kind load image-archive failed"; }
  rm -f -- "$WORK/images.tar"
fi
for tag in "$A_APP" "$A_PG" "$B1_APP" "$B1_PG"; do
  node_image_id "docker.io/library/$tag" > "$WORK/node-image.txt" || fail "the node has no image $tag"
  echo "      $tag: $(cut -d' ' -f1 "$WORK/node-image.txt") in the node"
done
[ "$(node_image_id "docker.io/library/$A_APP" | cut -d' ' -f1)" != "$(node_image_id "docker.io/library/$B1_APP" | cut -d' ' -f1)" ] \
  || fail "the application images of test releases A and B1 have the same id in the node"
ok "the node has both images of test releases A and B1, the application images with different ids ($((SECONDS - started))s)"

# --- helpers of both modes --------------------------------------------------------------------

restricted_namespace() {  # restricted_namespace <name>: Pod Security "restricted" enforced, warned and audited
  kubectl_ create namespace "$1" >/dev/null || fail "cannot create namespace $1"
  kubectl_ label namespace "$1" pod-security.kubernetes.io/enforce=restricted \
    pod-security.kubernetes.io/warn=restricted pod-security.kubernetes.io/audit=restricted >/dev/null \
    || fail "cannot label namespace $1"
}

secret_files() {  # secret_files <directory>: the three files of create_secrets.sh in <directory>/secrets
  mkdir -p "$1"
  isolated env PEOPLE_PUBS_SECRETS_DIR="$1/secrets" bash "$ROOT/create_secrets.sh" > "$WORK/create-secrets.log" 2>&1 \
    || { cat "$WORK/create-secrets.log" >&2; fail "create_secrets.sh failed"; }
  chmod 600 "$1/secrets/postgres_password" "$1/secrets/app_readonly_password" "$1/secrets/app_writer_password"
}

# render <chart> <values files...>: helm template of the release in the
# namespace, split into the manifests of all objects but bare pods and those
# of the pods.
render() {
  local chart="$1" file arguments=()
  shift
  for file in "$@"; do arguments+=(--values "$file"); done
  helm_ template "$RELEASE" "$chart" --namespace "$NAMESPACE" "${arguments[@]}" > "$WORK/rendered.yaml" \
    2> "$WORK/template.log" || { cat "$WORK/template.log" >&2; fail "helm template failed"; }
  "$PY" -I - "$WORK/rendered.yaml" "$WORK/rendered-objects.yaml" "$WORK/rendered-pods.yaml" <<'PY' \
    || fail "cannot split the rendered manifests"
import sys, yaml
documents = [document for document in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")) if document]
with open(sys.argv[2], "w", encoding="utf-8") as objects, open(sys.argv[3], "w", encoding="utf-8") as pods:
    yaml.safe_dump_all([document for document in documents if document["kind"] != "Pod"], objects)
    yaml.safe_dump_all([document for document in documents if document["kind"] == "Pod"], pods)
PY
}
# A bare pod -- helm test's -- names a ServiceAccount that only the install
# creates, and the API server refuses such a pod even in a dry run; it is
# validated the same way after the install, before helm test.
dry_run() {  # dry_run <manifests> [<what>]: admitted by the API server, without a warning
  if ! k create --dry-run=server --filename "$1" --output name > "$WORK/dry-run.log" 2> "$WORK/dry-run.err"; then
    cat "$WORK/dry-run.err" >&2
    fail "the API server refused the manifests of ${1##*/}${2:+ ($2)}"
  fi
  if [ -s "$WORK/dry-run.err" ]; then cat "$WORK/dry-run.err" >&2; fail "the API server warned about ${1##*/}${2:+ ($2)}"; fi
  ok "the API server admitted${2:+ $2} without a warning, by kind: $(sed 's|/.*||' "$WORK/dry-run.log" | sort | uniq -c | awk '{ printf "%s%s %s", (NR > 1 ? ", " : ""), $1, $2 }')"
}

# What no step installs, admitted all the same: the guide's two ingress
# examples, and the chart checks' evaluation values, which set the six
# scheduling keys at the top level and replace some of them for the read
# service and PostgreSQL. Each file's objects but helm test's pod, for the
# reason above, go to the API server on their own. A dry run creates no pod,
# and the priority admission resolves a PriorityClass for a pod only, so the
# classes those values name need not exist; no step creates them.
ADMITTED_VALUES=(charts/pubdex/examples/values-ingress-tls.yaml charts/pubdex/examples/values-ingress-cert-manager.yaml
  charts/tests/values/evaluation.yaml)
admit_values() {  # admit_values: each of ADMITTED_VALUES rendered and admitted in a dry run, none installed
  local file
  for file in "${ADMITTED_VALUES[@]}"; do
    render "$CHART" "$ROOT/$file"
    dry_run "$WORK/rendered-objects.yaml" "the objects of $file"
  done
  ok "none of the ${#ADMITTED_VALUES[@]} is installed; helm test's pod of each is left out, since its ServiceAccount exists only after an install"
  ok "no step creates the PriorityClasses the scheduling values name: the API server admitted the pod templates that name them, and the dry run created no pod, the one object whose PriorityClass it resolves"
}

package_example() {  # package_example <package>: the external example of this PubDex release
  ( cd "$WORK" && tar -xzf "$1" pubdex/examples/values-external.yaml ) \
    || fail "cannot extract the external example from ${1##*/}"
  EXTERNAL_EXAMPLE="$WORK/pubdex/examples/values-external.yaml"
}

# helm_run <command> <arguments>: helm <command> in the namespace, bounded,
# its output kept; a warning from the API server or the client fails it. The
# chart's notes, which helm prints, say "WARNING:" about the read service's
# exposure on their own, which is not a warning of either.
helm_run() {
  local log="$WORK/helm-$1.log"
  if ! job "$((WAIT_SECONDS + 60))" helm_ "$@" --namespace "$NAMESPACE" > "$log" 2>&1; then
    tail -n 30 "$log" >&2
    fail "helm $1 failed"
  fi
  if grep -Eq '^W[0-9]{4} |^Warning: |would violate PodSecurity|level=WARN' "$log"; then
    cat "$log" >&2
    fail "helm $1 printed a warning"
  fi
}

# --- the schema upgrade and its record ---------------------------------------------------------

eval_sql() {  # eval_sql <sql>: as postgres, on the local socket of the evaluation release's PostgreSQL pod
  k exec "$RELEASE-postgresql-0" --container postgresql -- \
    psql -X -q -tA -v ON_ERROR_STOP=1 --username postgres --dbname "$DATABASE" -c "$1"
}
# schema_record <sql function>: the database's record of schema changes,
# "<number> <name> <SHA-256> <release> <role> <applied at>" a line, or "none"
# for a database without it.
schema_record() {
  local exists
  exists="$("$1" "SELECT to_regclass('pubdex.schema_changes') IS NOT NULL")" || return 1
  if [ "$exists" = f ]; then echo none; return 0; fi
  [ "$exists" = t ] || return 1
  "$1" "SELECT concat_ws(' ', number, name, sha256, release, applied_by,
    to_char(applied_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')) FROM pubdex.schema_changes ORDER BY number"
}
RECORD=""  # the record as record_is last read it
# record_is <sql function> <"<number> <name> <SHA-256>" lines, or none> <phase>:
# the database records exactly these schema changes.
record_is() {
  RECORD="$(schema_record "$1")" || fail "cannot read the record of schema changes ($3)"
  [ "$(cut -d' ' -f1-3 <<<"$RECORD")" = "$2" ] \
    || fail "the database records the schema changes '$(cut -d' ' -f1-2 <<<"$RECORD" | paste -sd' ' -)', not '$(cut -d' ' -f1-2 <<<"$2" | paste -sd' ' -)' ($3)"
}
applied() {  # what the record says applied its changes: "<release> as <role>", each once
  if [ "$RECORD" = none ]; then echo "no schema upgrade"; else cut -d' ' -f4,5 <<<"$RECORD" | sort -u | sed 's/ / as /' | paste -sd, -; fi
}
record_kept() {  # record_kept <sql function> <phase>: the record is exactly as record_is last read it
  local now
  now="$(schema_record "$1")" || fail "cannot read the record of schema changes ($2)"
  [ "$now" = "$RECORD" ] || fail "the record of schema changes changed ($2): $(tr '\n' ' ' <<<"$now"); it was $(tr '\n' ' ' <<<"$RECORD")"
}
# schema_job <Job> <phase> <pattern>: the schema upgrade's Job completed, and
# the log of its pod that succeeded has a line matching the pattern.
schema_job() {
  local pods pod
  [ "$(k get job "$1" --output 'jsonpath={.status.succeeded}')" = 1 ] \
    || fail "the schema upgrade's Job $1 did not complete ($2): $(k get job "$1" --output 'jsonpath={.status}' 2>&1)"
  pods="$(k get pods --selector "batch.kubernetes.io/job-name=$1" --output name)" || fail "cannot list the pods of Job $1"
  pod="$(k get pods --selector "batch.kubernetes.io/job-name=$1" --field-selector status.phase=Succeeded --output name \
    | head -n 1)"
  [ -n "$pod" ] || fail "no pod of Job $1 succeeded ($2)"
  k logs "$pod" > "$WORK/$1.log" 2>&1 || { cat "$WORK/$1.log" >&2; fail "kubectl logs $pod failed ($2)"; }
  grep -Eq "$3" "$WORK/$1.log" || { cat "$WORK/$1.log" >&2; fail "the log of Job $1 does not say what its schema upgrade had to do ($2)"; }
  ok "Job $1 completed, $(grep -c . <<<"$pods") pod(s): $(grep -E "$3" "$WORK/$1.log" | tail -n 1)"
}
schema_jobs() {  # the schema upgrade's Jobs in the namespace
  k get jobs --selector "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=schema-upgrade" --output name
}
no_schema_job() { local jobs; jobs="$(schema_jobs)" && [ -z "$jobs" ]; }
APPLIED='^schema: done: applied '        # a schema upgrade that applied what was outstanding
NOTHING='^schema: nothing to do: '       # one that found nothing outstanding

PUB_ID=""
publication_check() {  # publication_check <page file>: the demonstration's item, or a failure
  isolated "$PY" -I - "$1" "$DEMO_DOI" <<'PY'
import json, sys
page = json.load(open(sys.argv[1], encoding="utf-8"))
items = [item for item in page["items"] if item["doi"] == sys.argv[2]]
assert len(items) == 1, f"{len(items)} items with the demonstration DOI"
item = items[0]
expected = {
    "title": "Synthetic Collective Behaviour Paper",
    "year": 2024,
    "venue": "Journal of Synthetic Metadata",
    "internal_authors": "Ada Example",
}
assert list(item) == ["pub_id", "doi", "title", "year", "venue", "source_of_truth", "internal_authors"], list(item)
assert {key: item[key] for key in expected} == expected, item
assert set(item["source_of_truth"].split("+")) == {"crossref", "orcid"}, item["source_of_truth"]
assert isinstance(item["pub_id"], int) and page["next_after"] is None, page
print(item["pub_id"])
PY
}

forwarding() { PORT="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9][0-9]*\) -> .*/\1/p' "$WORK/forward.log" | head -n 1)"; [ -n "$PORT" ]; }
live() { [ "$(http /livez "$WORK/livez.json")" = 200 ]; }
ready() { [ "$(http /readyz "$WORK/readyz.json")" = 200 ]; }
not_ready() { [ "$(http /readyz "$WORK/readyz.json")" = 503 ]; }

start_forward() {  # start_forward <resource>: kubectl port-forward on 127.0.0.1 to its port 8000; sets PORT
  : > "$WORK/forward.log"
  ( ISOLATED_IN_PLACE=1; kubectl_ --namespace "$NAMESPACE" port-forward --address 127.0.0.1 "$1" ":8000" ) \
    > "$WORK/forward.log" 2>&1 &
  FORWARD_PID=$!
  wait_until 60 forwarding || fail "kubectl port-forward did not start: $(head -c 300 "$WORK/forward.log")"
  wait_until 60 live || fail "the read service did not answer through the port forward on 127.0.0.1:$PORT"
}
stop_forward() { stop_process "$FORWARD_PID"; FORWARD_PID=""; }

read_back() {  # read_back <phase>: the publication and /readyz through a new port forward
  local pub_id
  start_forward "service/$RELEASE-api"
  [ "$(http /readyz "$WORK/readyz.json")" = 200 ] || fail "GET /readyz did not answer 200 ($1)"
  [ "$(http /v0/publications "$WORK/publications.json")" = 200 ] || fail "GET /v0/publications did not answer 200 ($1)"
  pub_id="$(publication_check "$WORK/publications.json" 2>&1)" || fail "the demonstration's publication ($1): $pub_id"
  stop_forward
  if [ -z "$PUB_ID" ]; then PUB_ID="$pub_id"; fi
  [ "$pub_id" = "$PUB_ID" ] || fail "the publication's pub_id is $pub_id, it was $PUB_ID ($1)"
  ok "GET /v0/publications through 127.0.0.1:$PORT serves $DEMO_DOI field by field (pub_id $pub_id); /readyz 200"
}

pods_of() {  # pods_of <component>: "<uid> <image> <image id> <ready>" for each pod not being deleted
  k get pods --selector "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$1" --output go-template \
    --template '{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.uid}} {{(index .spec.containers 0).image}}{{range .status.containerStatuses}} {{.imageID}} {{.ready}}{{end}}{{"\n"}}{{end}}{{end}}'
}
pods_run() {  # pods_run <component> <count> <image reference>: that many pods, all ready, all on that image
  local lines
  lines="$(pods_of "$1")" || return 1
  [ "$(grep -c . <<<"$lines")" = "$2" ] || return 1
  awk -v image="$3" 'NF != 4 || $2 != image || $4 != "true" { bad = 1 } END { exit bad }' <<<"$lines"
}

API_UIDS=""
PG_UID=""
API_IMAGE_ID=""
# check_revision <phase> <image> <postgres image or ""> <replicas> <log level> [same-image]:
# the pods run these image references; a reference by digest runs exactly that
# digest. After the first call of a release every pod has to be new and the
# read service's image another -- with same-image, the one it ran before.
check_revision() {
  local phase="$1" image="$2" pg_image="$3" replicas="$4" level="$5" kept="${6:-}" api pg="" uid
  wait_until "$WAIT_SECONDS" pods_run api "$replicas" "$image" \
    || fail "the read service does not run $replicas ready pods of $image ($phase): $(pods_of api)"
  api="$(pods_of api)"
  if [ -n "$pg_image" ]; then
    wait_until "$WAIT_SECONDS" pods_run postgresql 1 "$pg_image" \
      || fail "PostgreSQL does not run one ready pod of $pg_image ($phase): $(pods_of postgresql)"
    pg="$(pods_of postgresql)"
  fi
  if [[ "$image" == *@sha256:* ]] && awk -v digest="@${image##*@}" 'substr($3, length($3) - length(digest) + 1) != digest { bad = 1 } END { exit !bad }' <<<"$api"; then
    fail "a read service pod runs another image than ${image##*@} ($phase): $api"
  fi
  if [[ "$pg_image" == *@sha256:* ]] && [[ "$(cut -d' ' -f3 <<<"$pg")" != *"@${pg_image##*@}" ]]; then
    fail "PostgreSQL runs another image than ${pg_image##*@} ($phase): $pg"
  fi
  [ "$(k get configmap "$RELEASE-config" --output 'jsonpath={.data.PEOPLE_PUBS_API_LOG_LEVEL}')" = "$level" ] \
    || fail "the ConfigMap's log level is not $level ($phase)"
  if [ -n "$API_UIDS" ]; then
    for uid in $(cut -d' ' -f1 <<<"$api"); do
      if grep -qx "$uid" <<<"$API_UIDS"; then fail "read service pod $uid was not replaced ($phase)"; fi
    done
    if [ -n "$pg_image" ]; then [ "${pg%% *}" != "$PG_UID" ] || fail "the PostgreSQL pod was not replaced ($phase)"; fi
    if [ "$kept" = same-image ]; then
      [ "$(cut -d' ' -f3 <<<"$api" | sort -u)" = "$API_IMAGE_ID" ] \
        || fail "the read service runs $(cut -d' ' -f3 <<<"$api" | sort -u | tr '\n' ' '), not $API_IMAGE_ID ($phase)"
      ok "every pod was replaced; the read service still runs $API_IMAGE_ID"
    else
      [ "$(cut -d' ' -f3 <<<"$api" | sort -u)" != "$API_IMAGE_ID" ] || fail "the read service still runs image $API_IMAGE_ID ($phase)"
      ok "every pod was replaced; the read service now runs $(cut -d' ' -f3 <<<"$api" | sort -u)"
    fi
  fi
  API_UIDS="$(cut -d' ' -f1 <<<"$api")"
  PG_UID="${pg%% *}"
  API_IMAGE_ID="$(cut -d' ' -f3 <<<"$api" | sort -u)"
  ok "$replicas read service pod(s)${pg_image:+ and PostgreSQL} ready on $image${pg_image:+ and $pg_image}; log level $level"
}

reset_release_state() { PUB_ID=""; API_UIDS=""; PG_UID=""; API_IMAGE_ID=""; }

# login_as <namespace> <pod> <role> <password file>: a SCRAM login as <role>
# to the PostgreSQL pod's own address -- loopback the image's pg_hba.conf
# trusts --, the password on standard input, never on a command line.
# Prints psql's answer, the role's name or the error.
login_as() {
  job_input 90 kn "$1" exec -i "$2" --container postgresql -- sh -c '
    password="$(cat)"
    address="$(getent hosts "$HOSTNAME" | cut -d" " -f1)"
    case "$address" in ""|127.*|::1) echo "no address of its own: $address"; exit 1 ;; esac
    PGPASSWORD="$password" psql -XqtA "host=$address dbname=$2 user=$1 require_auth=scram-sha-256" -c "SELECT current_user"' \
    sh "$3" "$DATABASE" < "$4" 2>&1
}
# logins_changed <namespace> <pod> <new directory> <old directory> <roles...>:
# each role logs in with its new password, and its old one is refused.
logins_changed() {
  local namespace="$1" pod="$2" new="$3" old="$4" role answer
  shift 4
  for role in "$@"; do
    answer="$(login_as "$namespace" "$pod" "$role" "$new/${role}_password")" \
      && [ "$answer" = "$role" ] || fail "$role does not log in with its new password: $answer"
    if answer="$(login_as "$namespace" "$pod" "$role" "$old/${role}_password")"; then
      fail "$role still logs in with its old password"
    fi
    grep -q "password authentication failed for user \"$role\"" <<<"$answer" \
      || fail "the login of $role with its old password failed otherwise: $answer"
  done
  ok "$* log in with their new passwords by SCRAM; their old passwords are refused"
}

# replace_secret <directory> <keys...>: the operator's Secret replaced by one
# from <directory>/secrets, by the command an operator uses.
replace_secret() {
  local directory="$1" key arguments=()
  shift
  for key in "$@"; do arguments+=(--from-file="secrets/$key"); done
  ( cd "$directory" && k create secret generic "$SECRET_NAME" "${arguments[@]}" --dry-run=client --output yaml \
      | k replace --filename - ) > /dev/null 2> "$WORK/replace.err" || { cat "$WORK/replace.err" >&2; fail "cannot replace the Secret $SECRET_NAME"; }
  if grep -q '^Warning: ' "$WORK/replace.err"; then cat "$WORK/replace.err" >&2; fail "replacing the Secret: the API server warned"; fi
}

restart_api() {  # restart_api <replicas> <image>: kubectl rollout restart, as the guide says, until every pod is new
  local before uid
  before="$(pods_of api | cut -d' ' -f1)"
  k rollout restart "deployment/$RELEASE-api" > /dev/null || fail "kubectl rollout restart failed"
  job "$((WAIT_SECONDS + 30))" k rollout status "deployment/$RELEASE-api" --timeout "${WAIT_SECONDS}s" > "$WORK/rollout.log" 2>&1 \
    || { cat "$WORK/rollout.log" >&2; fail "the read service's restart did not complete"; }
  wait_until "$WAIT_SECONDS" pods_run api "$1" "$2" || fail "the restarted read service is not ready: $(pods_of api)"
  for uid in $(pods_of api | cut -d' ' -f1); do
    if grep -qx "$uid" <<<"$before"; then fail "read service pod $uid was not restarted"; fi
  done
  API_UIDS="$(pods_of api | cut -d' ' -f1)"  # the pods the next check_revision has to find replaced
  ok "kubectl rollout restart replaced the read service's pods"
}

remaining() {  # what the namespace holds besides what Kubernetes puts in every namespace
  k get all,configmaps,secrets,persistentvolumeclaims,serviceaccounts,networkpolicies,ingresses,roles,rolebindings \
    --output name | grep -vx -e configmap/kube-root-ca.crt -e serviceaccount/default | sort
}

# --- 4. namespace and Secret ---------------------------------------------------------------

step 4 "Evaluation mode: creating the namespace and the Secret..."
restricted_namespace "$NAMESPACE"
secret_files "$WORK/eval"
( cd "$WORK/eval" && k create secret generic "$SECRET_NAME" --from-file=secrets/postgres_password \
    --from-file=secrets/app_readonly_password --from-file=secrets/app_writer_password ) >/dev/null \
  || fail "cannot create the Secret $SECRET_NAME"
ok "namespace $NAMESPACE enforces Pod Security restricted; Secret $SECRET_NAME from create_secrets.sh's files"

# --- 5. server-side validation ----------------------------------------------------------------

# The preceding release's two published images, by registry, repository, tag
# and digest from the release record: revision 1's in every phase.
"$PY" -I - "$BASE_APP_REF" "$BASE_PG_REF" > "$WORK/values-published.yaml" <<'PY' \
  || fail "cannot write the values of the published images"
import re, sys
for key, reference in (("image", sys.argv[1]), ("postgresImage", sys.argv[2])):
    match = re.fullmatch(r"([^/]+)/([^:@]+):([^@]+)@(sha256:[0-9a-f]{64})", reference)
    if not match:
        sys.exit(f"{reference} is no reference by registry, repository, tag and digest")
    print(f'{key}: {{registry: "{match[1]}", repository: "{match[2]}", tag: "{match[3]}", digest: "{match[4]}"}}')
PY
# Revision 1: the Secret and the demonstration.
cat > "$WORK/values-1.yaml" <<YAML
secrets:
  name: $SECRET_NAME
demo:
  enabled: true
YAML
# Revision 2: the images under test, another log level, two API replicas.
cat > "$WORK/values-2.yaml" <<YAML
image: {registry: "", repository: $IMAGE_REPOSITORY, tag: "$RUN_ID", digest: "", pullPolicy: Never}
postgresImage: {registry: "", repository: $POSTGRES_REPOSITORY, tag: "$RUN_ID", digest: "", pullPolicy: Never}
api: {replicas: 2, logLevel: DEBUG}
YAML
# The images under test alone: the upgrade after the rollback and the fresh install.
sed -n '1,2p' "$WORK/values-2.yaml" > "$WORK/values-images.yaml"

step 5 "Validating revision 1, this checkout's chart, the two ingress examples and the scheduling values with server-side dry runs..."
render "$PRECEDING_CHART" "$WORK/values-published.yaml" "$WORK/values-1.yaml"
dry_run "$WORK/rendered-objects.yaml" "the objects of revision 1, the chart of PubDex $PRECEDING"
cp "$WORK/rendered-pods.yaml" "$WORK/eval-pods-1.yaml"
render "$CHART" "$WORK/values-published.yaml" "$WORK/values-1.yaml" "$WORK/values-2.yaml"
dry_run "$WORK/rendered-objects.yaml" "the objects of this checkout's chart with the upgrade's values"
cp "$WORK/rendered-pods.yaml" "$WORK/eval-pods-2.yaml"
admit_values

# --- 6. install with the demonstration ------------------------------------------------------

step 6 "Installing revision 1, the preceding release's chart and images, with the demonstration..."
started=$SECONDS
helm_run install "$RELEASE" "$PRECEDING_CHART" --values "$WORK/values-published.yaml" --values "$WORK/values-1.yaml" \
  --wait --timeout "${WAIT_SECONDS}s"
ok "helm install --wait of the chart of PubDex $PRECEDING, the demonstration included, in $((SECONDS - started))s"
completed="$(k get events --field-selector "involvedObject.kind=Job,involvedObject.name=$RELEASE-demo,reason=Completed" \
  --output name 2>/dev/null | wc -l)"
[ "$completed" -ge 1 ] || fail "no Completed event of the demonstration's Job $RELEASE-demo"
ok "the demonstration's Job $RELEASE-demo completed (and helm deleted it)"

check_database() {  # check_database <phase>: no new initialisation, both roles log in
  local log logins
  log="$(k logs "$RELEASE-postgresql-0" --container postgresql 2>&1)" || fail "cannot read PostgreSQL's log ($1)"
  grep -q 'Skipping initialization' <<<"$log" && ! grep -q 'init process complete' <<<"$log" \
    || fail "PostgreSQL did not start on its existing data directory ($1): $(tail -n 5 <<<"$log")"
  # The passwords are read inside the pod, from the files of the Secret it
  # mounts; none of them reaches this script or a command line. The login
  # goes to the pod's own address, not to loopback, which the image's
  # pg_hba.conf trusts, and has to use SCRAM.
  logins="$(k exec "$RELEASE-postgresql-0" --container postgresql -- sh -c '
    address="$(getent hosts "$HOSTNAME" | cut -d" " -f1)"
    case "$address" in ""|127.*|::1) echo "no address of its own: $address"; exit 1 ;; esac
    for role in app_readonly app_writer; do
      PGPASSWORD="$(cat "/run/secrets/pubdex/${role}_password")" psql -XqtA \
        "host=$address dbname=people_db user=$role require_auth=scram-sha-256" -c "SELECT current_user" || exit 1
    done' 2>&1)" || fail "a role's password login failed ($1): $logins"
  [ "$(tr '\n' ' ' <<<"$logins")" = "app_readonly app_writer " ] || fail "unexpected logins ($1): $logins"
  ok "PostgreSQL skipped initialisation on its volume; app_readonly and app_writer log in with SCRAM"
}

# --- 7. revision 1 ---------------------------------------------------------------------------

step 7 "Checking revision 1: the published images, the record of schema changes, the read service, helm test..."
check_revision "revision 1" "$BASE_APP_REF" "$BASE_PG_REF" 1 INFO
record_is eval_sql "$PRECEDING_RECORD" "revision 1"
ok "the database records the schema changes of PubDex $PRECEDING: $(cut -d' ' -f2 <<<"$RECORD" | paste -sd' ' -)"
read_back "revision 1"
helm_ get notes "$RELEASE" --namespace "$NAMESPACE" > "$WORK/notes.log" 2>&1 || { cat "$WORK/notes.log" >&2; fail "helm get notes failed"; }
forward_line="kubectl --namespace $NAMESPACE port-forward service/$RELEASE-api 8000:8000"
grep -Fqx "  $forward_line" "$WORK/notes.log" \
  || { cat "$WORK/notes.log" >&2; fail "helm get notes does not print the port forward to the read service"; }
ok "helm get notes prints the release's commands again, the port forward among them: $forward_line"
dry_run "$WORK/eval-pods-1.yaml"
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"

# --- 8. upgrade --------------------------------------------------------------------------------

step 8 "Upgrading to this checkout's chart and the images under test with changed values, with the schema upgrade..."
started=$SECONDS
checksum_before="$(k get deployment "$RELEASE-api" --output 'jsonpath={.spec.template.metadata.annotations.checksum/config}')"
helm_run upgrade "$RELEASE" "$CHART" --values "$WORK/values-published.yaml" --values "$WORK/values-1.yaml" \
  --values "$WORK/values-2.yaml" --wait --wait-for-jobs --timeout "${WAIT_SECONDS}s"
ok "helm upgrade --wait --wait-for-jobs in $((SECONDS - started))s"
schema_job "$RELEASE-schema-upgrade-2" "revision 2" "$APPLIED"
record_is eval_sql "$CHECKOUT_RECORD" "revision 2"
ok "the database records this checkout's schema changes, $(cut -d' ' -f2 <<<"$RECORD" | paste -sd' ' -), applied by $(applied)"
check_revision "revision 2" "$APP_TAG" "$PG_TAG" 2 DEBUG
[ "$(k get deployment "$RELEASE-api" --output 'jsonpath={.spec.template.metadata.annotations.checksum/config}')" \
  != "$checksum_before" ] || fail "the read service's checksum/config did not change with the setting"
ok "the changed setting changed the read service's checksum/config"
check_database "revision 2"
read_back "revision 2"
dry_run "$WORK/eval-pods-2.yaml"
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"

# --- 9. rollback, and the upgrade again ----------------------------------------------------------

step 9 "Rolling back to revision 1, then upgrading to this checkout again..."
started=$SECONDS
helm_run rollback "$RELEASE" 1 --wait --timeout "${WAIT_SECONDS}s"
ok "helm rollback to revision 1 in $((SECONDS - started))s"
check_revision "rollback" "$BASE_APP_REF" "$BASE_PG_REF" 1 INFO
check_database "rollback"
record_kept eval_sql "rollback"
wait_until 60 no_schema_job || fail "a schema upgrade Job outlived the rollback to revision 1, which has none: $(schema_jobs | tr '\n' ' ')"
ok "the schema and its record stay as the upgrade left them; revision 2's schema upgrade Job went with its revision"
read_back "rollback"
ok "PubDex $PRECEDING's images serve the demonstration's publication on this checkout's schema"
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^3[[:space:]].*deployed.*Rollback to 1' "$WORK/history.log" || { cat "$WORK/history.log" >&2; fail "revision 3 is not the rollback to 1"; }
ok "helm history: revision 3 deployed, rollback to 1"
helm_ get values "$RELEASE" --namespace "$NAMESPACE" --all > "$WORK/values-all.log" 2> "$WORK/values-all.err" \
  || { cat "$WORK/values-all.err" >&2; fail "helm get values --all failed"; }
"$PY" -I - "$WORK/values-all.log" "$BASE_APP_REF" "$BASE_PG_REF" "$SECRET_NAME" <<'PY' || fail "helm get values --all: not revision 1's values"
import sys, yaml
path, app, postgres, secret = sys.argv[1:]
head, _, body = open(path, encoding="utf-8").read().partition("\n")
if head != "COMPUTED VALUES:":
    sys.exit(f"the output begins {head!r}")
values = yaml.safe_load(body)
for key, reference in (("image", app), ("postgresImage", postgres)):
    image = values[key]
    shown = f'{image["registry"]}/{image["repository"]}:{image["tag"]}@{image["digest"]}'
    if shown != reference:
        sys.exit(f"{key}: {shown}, not {reference}")
seen = (values["secrets"]["name"], values["demo"]["enabled"], values["api"]["replicas"], values["api"]["logLevel"])
if seen != (secret, True, 1, "INFO"):
    sys.exit(f"secrets.name, demo.enabled, api.replicas, api.logLevel: {seen}")
PY
ok "helm get values --all: revision 1's values, both published images by digest, one replica, log level INFO"
started=$SECONDS
helm_run upgrade "$RELEASE" "$CHART" --values "$WORK/values-published.yaml" --values "$WORK/values-1.yaml" \
  --values "$WORK/values-images.yaml" --wait --wait-for-jobs --timeout "${WAIT_SECONDS}s"
ok "helm upgrade --wait --wait-for-jobs to this checkout again, revision 4, in $((SECONDS - started))s"
schema_job "$RELEASE-schema-upgrade-4" "revision 4" "$NOTHING"
record_kept eval_sql "revision 4"
check_revision "revision 4" "$APP_TAG" "$PG_TAG" 1 INFO
check_database "revision 4"
read_back "revision 4"
ok "the record is unchanged; steps 10 to 13 run this checkout's chart and the images under test"

# --- 10. password change -----------------------------------------------------------------------

step 10 "Changing every password: the Secret, the role-password step, a restart..."
started=$SECONDS
secret_files "$WORK/eval-rotated"
replace_secret "$WORK/eval-rotated" postgres_password app_readonly_password app_writer_password
ok "kubectl replace: Secret $SECRET_NAME holds the new files of create_secrets.sh"
# One line per role, its name, a tab and its password, on 020's standard input.
( umask 077
  for role in postgres app_readonly app_writer; do
    printf '%s\t%s\n' "$role" "$(cat "$WORK/eval-rotated/secrets/${role}_password")"
  done > "$WORK/role-passwords" )
job_input 120 k exec -i "$RELEASE-postgresql-0" --container postgresql -- \
    bash /docker-entrypoint-initdb.d/020_role_passwords.sh --stdin < "$WORK/role-passwords" > "$WORK/role-passwords.log" 2>&1 \
  || { cat "$WORK/role-passwords.log" >&2; fail "020_role_passwords.sh --stdin failed in $RELEASE-postgresql-0"; }
rm -f -- "$WORK/role-passwords"
grep -q 'each authenticated a new connection' "$WORK/role-passwords.log" || { cat "$WORK/role-passwords.log" >&2; fail "020_role_passwords.sh did not confirm the new passwords"; }
ok "020_role_passwords.sh --stdin set the three passwords; each authenticated a new connection"
restart_api 1 "$APP_TAG"
read_back "password change"
logins_changed "$NAMESPACE" "$RELEASE-postgresql-0" "$WORK/eval-rotated/secrets" "$WORK/eval/secrets" postgres app_readonly app_writer
ok "the password change took $((SECONDS - started))s"

# --- 11. readiness -----------------------------------------------------------------------------

api_pod() {  # "<name> <uid> <restarts> <ready>" of the one read service pod not being deleted
  k get pods --selector "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=api" --output go-template \
    --template '{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}} {{.metadata.uid}}{{range .status.containerStatuses}} {{.restartCount}} {{.ready}}{{end}}{{"\n"}}{{end}}{{end}}'
}
api_ready_is() { [ "$(api_pod | cut -d' ' -f4)" = "$1" ]; }
no_postgresql() { [ -z "$(k get pods --selector "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=postgresql" --output name)" ]; }

step 11 "Readiness: PostgreSQL scaled to 0 and back..."
read -r api_name api_uid api_restarts api_ready <<<"$(api_pod)"
[ "$api_ready" = true ] || fail "the read service pod $api_name is not ready before the test"
start_forward "pod/$api_name"
ready || fail "GET /readyz did not answer 200 through the pod's port forward"
started=$SECONDS
k scale statefulset "$RELEASE-postgresql" --replicas=0 > /dev/null || fail "kubectl scale to 0 failed"
wait_until "$WAIT_SECONDS" no_postgresql || fail "the PostgreSQL pod did not end"
wait_until 60 not_ready || fail "GET /readyz did not report the missing database within 60s"
grep -qF '"reason":"database_unavailable"' "$WORK/readyz.json" || fail "GET /readyz gave another reason: $(cat "$WORK/readyz.json")"
live || fail "GET /livez failed while the database was gone"
ok "with PostgreSQL at 0 replicas, /readyz 503 database_unavailable, /livez 200 ($((SECONDS - started))s)"
wait_until 90 api_ready_is false || fail "the read service pod did not become not ready: $(api_pod)"
live || fail "GET /livez failed while the database was gone"
ok "the read service pod $api_name is not ready, and alive ($((SECONDS - started))s after the scale)"
k scale statefulset "$RELEASE-postgresql" --replicas=1 > /dev/null || fail "kubectl scale to 1 failed"
started=$SECONDS
wait_until "$WAIT_SECONDS" pods_run postgresql 1 "$PG_TAG" || fail "PostgreSQL did not come back: $(pods_of postgresql)"
wait_until 60 ready || fail "GET /readyz did not recover within 60s of PostgreSQL's return"
wait_until 60 api_ready_is true || fail "the read service pod did not become ready again: $(api_pod)"
stop_forward
[ "$(api_pod)" = "$api_name $api_uid $api_restarts true" ] \
  || fail "the read service pod was replaced or restarted: before '$api_name $api_uid $api_restarts', now '$(api_pod)'"
ok "PostgreSQL back: the same read service pod ready again $((SECONDS - started))s later, $api_restarts restarts"
check_database "readiness"

# --- 12. NetworkPolicy -----------------------------------------------------------------------

probes_ended() {  # the three probe pods have ended
  local lines
  lines="$(k get pods --selector "$PROBE_LABEL" --output go-template \
    --template '{{range .items}}{{index .metadata.labels "'"$PROBE_LABEL"'"}} {{.status.phase}}{{"\n"}}{{end}}')" || return 1
  [ "$(grep -cE '^(outsider|client|ingest) (Succeeded|Failed)$' <<<"$lines")" = 3 ]
}
# probe-labelled has tried the read service once; it has ended.
labelled_tried() { [ -n "$(k logs probe-labelled 2>/dev/null)" ]; }
labelled_ended() { [[ "$(k get pod probe-labelled --output 'jsonpath={.status.phase}')" =~ ^(Succeeded|Failed)$ ]]; }

step 12 "NetworkPolicy: three pods without the release's labels ask PostgreSQL and the read service..."
plugin="$(kn kube-system get daemonset kindnet --output 'jsonpath={.spec.template.spec.containers[0].image}' 2>/dev/null)" \
  || fail "the cluster runs no kindnet, the network plugin of kind that enforces NetworkPolicy; the observation needs it"
echo "      network plugin: kindnet ($plugin)"
"$PY" -I - "$WORK/probes.yaml" "$BASE_APP_REF" "$RELEASE" "$PROBE_LABEL" <<'PY' || fail "cannot write the probe pods"
import sys, yaml
path, image, release, label = sys.argv[1:]
code = """\
import socket, sys
for target in sys.argv[1:]:
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=5).close()
        print(target, "connected", flush=True)
    except OSError as error:
        print(target, type(error).__name__, flush=True)
"""
# probe-labelled tries the read service until it connects; the run labels it
# once it has failed.
retry = """\
import socket, sys, time
host, port = sys.argv[1].rsplit(":", 1)
for attempt in range(300):
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
    except OSError as error:
        print(type(error).__name__, flush=True)
        time.sleep(0.5)
    else:
        print("connected", flush=True)
        sys.exit(0)
sys.exit(1)
"""
probes = {
    "outsider": ({}, code, [f"{release}-postgresql:5432", f"{release}-api:8000"]),
    "client": ({f"{release}-client": "true"}, code, [f"{release}-postgresql:5432", f"{release}-api:8000"]),
    "ingest": ({"app.kubernetes.io/name": "pubdex", "app.kubernetes.io/instance": release,
                "app.kubernetes.io/component": "ingest"}, code, [f"{release}-postgresql:5432", f"{release}-api:8000"]),
    "labelled": ({}, retry, [f"{release}-api:8000"]),
}
yaml.safe_dump_all([{
    "apiVersion": "v1", "kind": "Pod",
    "metadata": {"name": f"probe-{name}", "labels": dict(labels, **{label: name})},
    "spec": {
        "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "probe", "image": image, "imagePullPolicy": "IfNotPresent",
            "command": ["python", "-I", "-c", program, *targets],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}}}]}}
    for name, (labels, program, targets) in probes.items()], open(path, "w", encoding="utf-8"))
PY
kube "creating the probe pods" "$NAMESPACE" create --filename "$WORK/probes.yaml"
wait_until "$WAIT_SECONDS" probes_ended || fail "the probe pods did not end"
for probe in outsider client ingest; do
  k logs "probe-$probe" | sed "s/^/$probe /" || fail "cannot read the log of probe-$probe"
done > "$WORK/probes.txt"
sed 's/^/      /' "$WORK/probes.txt"
"$PY" -I - "$WORK/probes.txt" "$RELEASE" <<'PY' || fail "the NetworkPolicy did not behave as the chart says"
import sys
release = sys.argv[2]
pg, api = f"{release}-postgresql:5432", f"{release}-api:8000"
seen = {(probe, target): result == "connected" for probe, target, result in
        (line.split() for line in open(sys.argv[1], encoding="utf-8") if line.strip())}
expected = {("outsider", pg): False, ("outsider", api): False, ("client", pg): False, ("client", api): True,
            ("ingest", pg): True, ("ingest", api): False}
if seen != expected:
    sys.exit(f"connected: {sorted(key for key, value in seen.items() if value)}, "
             f"expected {sorted(key for key, value in expected.items() if value)}")
PY
ok "observed on kindnet: no labels -- neither; client label -- the read service only; ingestion labels -- PostgreSQL only"
wait_until 60 labelled_tried || fail "probe-labelled did not try the read service"
k label pod probe-labelled "$RELEASE-client=true" > /dev/null || fail "cannot label pod probe-labelled"
wait_until 60 labelled_ended || fail "probe-labelled did not end within 60s of its label: $(k logs probe-labelled | tail -n 3)"
k logs probe-labelled > "$WORK/labelled.txt" || fail "cannot read the log of probe-labelled"
[ "$(k get pod probe-labelled --output 'jsonpath={.status.phase}')" = Succeeded ] \
  && [ "$(head -n 1 "$WORK/labelled.txt")" != connected ] && [ "$(tail -n 1 "$WORK/labelled.txt")" = connected ] \
  || fail "probe-labelled did not fail first and then reach the read service: $(tr '\n' ' ' < "$WORK/labelled.txt")"
ok "kubectl label pod probe-labelled $RELEASE-client=true: after $(grep -vcx connected "$WORK/labelled.txt") failed attempt(s) ($(head -n 1 "$WORK/labelled.txt")), the pod reached the read service"
k delete pods --selector "$PROBE_LABEL" --wait=true > /dev/null || fail "cannot delete the probe pods"

# --- 13. uninstall -----------------------------------------------------------------------------

step 13 "Uninstalling the evaluation release..."
helm_run uninstall "$RELEASE" --wait --timeout "${WAIT_SECONDS}s"
claim="data-$RELEASE-postgresql-0"
expected="$(printf '%s\n%s\n' "persistentvolumeclaim/$claim" "secret/$SECRET_NAME" | sort)"
only_documented_left() { [ "$(remaining)" = "$expected" ]; }
wait_until 120 only_documented_left \
  || fail "after helm uninstall the namespace holds $(remaining | tr '\n' ' '), not just $(tr '\n' ' ' <<<"$expected")"
ok "helm uninstall left the database volume's claim and the operator's Secret, as the notes say"
volume="$(k get persistentvolumeclaim "$claim" --output 'jsonpath={.spec.volumeName}')"
[ -n "$volume" ] || fail "the claim $claim names no volume"
# The commands of the notes (NOTES.txt) and of the Secret's creation.
k delete pvc "$claim" --wait=true >/dev/null || fail "cannot delete the claim $claim"
k delete secret "$SECRET_NAME" >/dev/null || fail "cannot delete the Secret $SECRET_NAME"
volume_gone() { ! kubectl_ get persistentvolume "$volume" --request-timeout=60s >/dev/null 2>&1; }
wait_until 120 volume_gone || fail "the volume $volume of the deleted claim is still there"
[ -z "$(remaining)" ] || fail "the namespace still holds $(remaining | tr '\n' ' ')"
ok "the claim, its volume $volume and the Secret are deleted; the namespace holds nothing of the release"

# --- 14. a fresh install of this checkout's chart ---------------------------------------------

demo_completions() {  # the Completed events of the demonstration's Jobs in the namespace
  k get events --field-selector "involvedObject.kind=Job,involvedObject.name=$RELEASE-demo,reason=Completed" \
    --output name 2>/dev/null | wc -l
}

# The guide's short path with this checkout's chart: PostgreSQL initialises
# the baseline on a new volume, the schema upgrade's Job of revision 1 applies
# every schema change, and --wait-for-jobs makes Helm wait for that Job
# before it starts the demonstration's post-install hook.
step 14 "Evaluation mode: installing this checkout's chart afresh, with the demonstration..."
started=$SECONDS
reset_release_state
secret_files "$WORK/eval-fresh"
( cd "$WORK/eval-fresh" && k create secret generic "$SECRET_NAME" --from-file=secrets/postgres_password \
    --from-file=secrets/app_readonly_password --from-file=secrets/app_writer_password ) >/dev/null \
  || fail "cannot create the Secret $SECRET_NAME"
completed="$(demo_completions)"
helm_run install "$RELEASE" "$CHART" --values "$WORK/values-1.yaml" --values "$WORK/values-images.yaml" \
  --wait --wait-for-jobs --timeout "${WAIT_SECONDS}s"
ok "helm install --wait --wait-for-jobs of this checkout's chart, the demonstration included, in $((SECONDS - started))s"
[ "$(demo_completions)" -gt "$completed" ] || fail "no new Completed event of the demonstration's Job $RELEASE-demo"
ok "the demonstration's Job $RELEASE-demo completed (and helm deleted it)"
log="$(k logs "$RELEASE-postgresql-0" --container postgresql 2>&1)" || fail "cannot read PostgreSQL's log (fresh install)"
grep -q 'init process complete' <<<"$log" || fail "PostgreSQL did not initialise its new volume: $(tail -n 5 <<<"$log")"
schema_job "$RELEASE-schema-upgrade-1" "fresh install" "$APPLIED"
record_is eval_sql "$CHECKOUT_RECORD" "fresh install"
ok "PostgreSQL initialised the baseline on its new volume; the database records this checkout's schema changes, $(cut -d' ' -f2 <<<"$RECORD" | paste -sd' ' -), applied by $(applied)"
check_revision "fresh install" "$APP_TAG" "$PG_TAG" 1 INFO
read_back "fresh install"
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"
helm_run uninstall "$RELEASE" --wait --timeout "${WAIT_SECONDS}s"
wait_until 120 only_documented_left \
  || fail "after helm uninstall the namespace holds $(remaining | tr '\n' ' '), not just $(tr '\n' ' ' <<<"$expected")"
volume="$(k get persistentvolumeclaim "$claim" --output 'jsonpath={.spec.volumeName}')"
[ -n "$volume" ] || fail "the claim $claim names no volume"
k delete pvc "$claim" --wait=true >/dev/null || fail "cannot delete the claim $claim"
k delete secret "$SECRET_NAME" >/dev/null || fail "cannot delete the Secret $SECRET_NAME"
wait_until 120 volume_gone || fail "the volume $volume of the deleted claim is still there"
[ -z "$(remaining)" ] || fail "the namespace still holds $(remaining | tr '\n' ' ')"
ok "helm uninstall left the claim and the Secret, as the notes say; both deleted, the namespace holds nothing of the release ($((SECONDS - started))s)"

# --- 15. the external server ---------------------------------------------------------------

server_sql() {  # server_sql <sql>: as the server's superuser, on its local socket
  kn "$SERVER_NAMESPACE" exec "$SERVER_POD" --container postgresql -- \
    psql -X -q -tA -v ON_ERROR_STOP=1 --username "$SERVER_SUPERUSER" --dbname "$DATABASE" -c "$1"
}
server_ready() {
  [ "$(kn "$SERVER_NAMESPACE" get pod "$SERVER_POD" --output 'jsonpath={.status.containerStatuses[0].ready}' 2>/dev/null)" = true ]
}
random_file() {  # random_file <path>: 32 random bytes as hex and a line break, readable by this user only
  ( umask 077; od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$1"; printf '\n' >> "$1" )
}

step 15 "External mode: a PostgreSQL server of the run's own, with TLS, in namespace $SERVER_NAMESPACE..."
started=$SECONDS
NAMESPACE="$SERVER_NAMESPACE"
reset_release_state
TLS="$WORK/tls"
mkdir -m 700 "$TLS"
random_file "$TLS/superuser_password"
random_file "$TLS/admin_password"
# A CA of the run's own, and the server's certificate for its Service's DNS name.
{ isolated openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 \
    -subj "/CN=PubDex cluster acceptance CA $RUN_ID" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout "$TLS/ca.key" -out "$TLS/ca.crt" \
  && isolated openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj "/CN=$SERVER_HOST" \
    -keyout "$TLS/server.key" -out "$TLS/server.csr" \
  && printf 'subjectAltName=DNS:%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n' \
    "$SERVER_HOST" > "$TLS/server.ext" \
  && isolated openssl x509 -req -in "$TLS/server.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" \
    -set_serial "0x$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')" -days 2 -extfile "$TLS/server.ext" -out "$TLS/server.crt"
} > "$WORK/openssl.log" 2>&1 || { cat "$WORK/openssl.log" >&2; fail "openssl could not create the CA and the server's certificate"; }
# Run by the image's entrypoint once, on the empty data directory, as a
# managed service provisions a database: its owner may create roles but is
# no superuser. The password comes from the mounted file, through psql's
# variable, not from a command line.
cat > "$WORK/001_managed_server.sh" <<SH
psql -v ON_ERROR_STOP=1 --no-psqlrc --username "\$POSTGRES_USER" --dbname "\$POSTGRES_DB" <<'SQL'
\\set admin_password \`cat /run/pubdex-db/secrets/admin\`
CREATE ROLE $ADMIN_USER LOGIN CREATEROLE PASSWORD :'admin_password';
ALTER DATABASE $DATABASE OWNER TO $ADMIN_USER;
SQL
SH
"$PY" -I - "$WORK/server.yaml" "$SERVER" "$BASE_PG_REF" "$SERVER_SUPERUSER" "$DATABASE" <<'PY' || fail "cannot write the server's manifests"
import sys, yaml
path, name, image, superuser, database = sys.argv[1:]
labels = {"app.kubernetes.io/name": name}
restricted = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "labels": labels},
           "spec": {"selector": labels, "ports": [{"name": "postgresql", "port": 5432, "targetPort": 5432, "protocol": "TCP"}]}}
pod = {
    "automountServiceAccountToken": False, "enableServiceLinks": False, "terminationGracePeriodSeconds": 30,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 999, "runAsGroup": 999, "fsGroup": 999,
                        "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{
        "name": "postgresql", "image": image, "imagePullPolicy": "IfNotPresent",
        "args": ["postgres", "-c", "ssl=on", "-c", "ssl_cert_file=/run/pubdex-db/tls/tls.crt",
                 "-c", "ssl_key_file=/run/pubdex-db/tls/tls.key"],
        "env": [{"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                {"name": "POSTGRES_USER", "value": superuser}, {"name": "POSTGRES_DB", "value": database},
                {"name": "POSTGRES_PASSWORD_FILE", "value": "/run/pubdex-db/secrets/superuser"}],
        "ports": [{"name": "postgresql", "containerPort": 5432, "protocol": "TCP"}],
        "readinessProbe": {"exec": {"command": ["pg_isready", "-q", "-h", "127.0.0.1", "-p", "5432"]},
                           "periodSeconds": 2, "timeoutSeconds": 5},
        "securityContext": restricted,
        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"},
                         {"name": "init", "mountPath": "/docker-entrypoint-initdb.d", "readOnly": True},
                         {"name": "secrets", "mountPath": "/run/pubdex-db/secrets", "readOnly": True},
                         {"name": "tls", "mountPath": "/run/pubdex-db/tls", "readOnly": True},
                         {"name": "run", "mountPath": "/var/run/postgresql"},
                         {"name": "tmp", "mountPath": "/tmp"},
                         {"name": "shm", "mountPath": "/dev/shm"}]}],
    "volumes": [{"name": "data", "emptyDir": {}},
                {"name": "init", "configMap": {"name": f"{name}-init"}},
                {"name": "secrets", "secret": {"secretName": f"{name}-server", "defaultMode": 0o440}},
                {"name": "tls", "secret": {"secretName": f"{name}-tls", "defaultMode": 0o440}},
                {"name": "run", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}},
                {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}}]}
statefulset = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": name, "labels": labels},
               "spec": {"serviceName": name, "replicas": 1, "selector": {"matchLabels": labels},
                        "template": {"metadata": {"labels": labels}, "spec": pod}}}
yaml.safe_dump_all([service, statefulset], open(path, "w", encoding="utf-8"))
PY
restricted_namespace "$SERVER_NAMESPACE"
kube "the server's Secret" "$SERVER_NAMESPACE" create secret generic "$SERVER-server" \
  --from-file="superuser=$TLS/superuser_password" --from-file="admin=$TLS/admin_password"
kube "the server's TLS Secret" "$SERVER_NAMESPACE" create secret tls "$SERVER-tls" --cert "$TLS/server.crt" --key "$TLS/server.key"
kube "the server's initialisation ConfigMap" "$SERVER_NAMESPACE" create configmap "$SERVER-init" \
  --from-file="001_managed_server.sh=$WORK/001_managed_server.sh"
kube "the server's StatefulSet and Service" "$SERVER_NAMESPACE" create --filename "$WORK/server.yaml"
wait_until "$WAIT_SECONDS" server_ready || fail "the external PostgreSQL server did not become ready: $(kn "$SERVER_NAMESPACE" logs "$SERVER_POD" 2>&1 | tail -n 20)"
# managed_server <phase>: PostgreSQL 16 with TLS, whose only roles are the
# superuser and the admin role, which owns the database, has CREATEROLE and is
# no superuser, and whose database holds no schema but public; sets
# SERVER_RELEASE.
managed_server() {
  local facts server_pg server_ssl server_roles server_owner server_schemas expected_roles
  facts="$(server_sql "SELECT current_setting('server_version_num') || ' ' || current_setting('ssl') || ' '
    || (SELECT string_agg(rolname || ':' || rolsuper::text || ':' || rolcreaterole::text, ',' ORDER BY rolname)
        FROM pg_roles WHERE rolname !~ '^pg_') || ' ' || pg_get_userbyid(datdba) || ' '
    || (SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public', 'information_schema'))
    FROM pg_database WHERE datname = current_database()")" || fail "cannot ask the external server ($1): $facts"
  read -r server_pg server_ssl server_roles server_owner server_schemas <<<"$facts"
  # The server sorts the names as the C locale does.
  expected_roles="$(printf '%s\n' "$SERVER_SUPERUSER:true:true" "$ADMIN_USER:false:true" | LC_ALL=C sort | paste -sd, -)"
  [[ "$server_pg" =~ ^16[0-9]{4}$ ]] && [ "$server_ssl" = on ] && [ "$server_owner" = "$ADMIN_USER" ] \
    && [ "$server_roles" = "$expected_roles" ] && [ "$server_schemas" = 0 ] \
    || fail "the external server is not the managed-style server expected ($1): $facts"
  SERVER_RELEASE="${server_pg:0:2}.$((10#${server_pg:2}))"
}
managed_server "its start"
ok "PostgreSQL $SERVER_RELEASE with TLS at $SERVER_HOST; $DATABASE owned by $ADMIN_USER (CREATEROLE, no superuser), without a schema of PubDex; no role postgres ($((SECONDS - started))s)"

# --- 16. external install ----------------------------------------------------------------------

schema_ids() {  # the ids of the schemas and of one table the setup creates; they change only with a new initialisation
  server_sql "SELECT (SELECT string_agg(oid::text, ' ' ORDER BY nspname) FROM pg_namespace
    WHERE nspname IN ('app', 'biblio', 'activity', 'pii', 'staging_raw')) || ' ' || 'biblio.publications'::regclass::oid"
}
setup_hooks_left() {  # the hook objects of the setup and of the schema upgrade still in the namespace
  k get jobs,configmaps,serviceaccounts \
    --selector "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component in (setup,schema-upgrade)" --output name
}
setup_pods() { k get pods --selector "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=setup" --output name; }
no_setup_pod() { local pods; pods="$(setup_pods)" && [ -z "$pods" ]; }
admin_secret() {  # admin_secret create|delete: as KUBERNETES.md says
  if [ "$1" = create ]; then
    kube "creating the admin Secret" "$NAMESPACE" create secret generic "$ADMIN_SECRET_NAME" --from-file="$ADMIN_KEY=$TLS/admin_password"
  else
    kube "deleting the admin Secret" "$NAMESPACE" delete secret "$ADMIN_SECRET_NAME"
  fi
}
admin_secret_absent() {  # admin_secret_absent <phase>: the API server answers that the admin Secret does not exist
  local answer
  if answer="$(k get secret "$ADMIN_SECRET_NAME" --output name 2>&1)"; then
    fail "the admin Secret $ADMIN_SECRET_NAME exists ($1)"
  fi
  grep -q '(NotFound)' <<<"$answer" || fail "cannot tell whether the admin Secret exists ($1): $answer"
}

# What the setup's hooks create: a Job, a ConfigMap and a ServiceAccount, which
# Helm deletes again once the Job succeeded. mark_hook_kinds notes the
# resourceVersion of the namespace's list of each of the three kinds;
# created_since then has the API server replay its watch of each from that
# version and prints "<kind>/<name>" of every object created since -- one
# already deleted again included. A version the API server no longer holds
# makes it fail, never pass.
HOOK_KIND_PATHS=(/apis/batch/v1/namespaces/@/jobs /api/v1/namespaces/@/configmaps /api/v1/namespaces/@/serviceaccounts)
mark_hook_kinds() {
  local path version
  : > "$WORK/marks"
  for path in "${HOOK_KIND_PATHS[@]}"; do
    path="${path/@/$NAMESPACE}"
    version="$(kubectl_ get --raw "$path?limit=1" --request-timeout=60s \
      | "$PY" -I -c 'import json, sys; print(json.load(sys.stdin)["metadata"]["resourceVersion"])')" \
      && [ -n "$version" ] || fail "cannot read the resourceVersion of $path"
    printf '%s %s\n' "$path" "$version" >> "$WORK/marks"
  done
}
created_since() {  # status 1, with the reason, when a watch cannot be replayed
  local path version
  : > "$WORK/watch.jsonl"
  while read -r path version; do
    kubectl_ get --raw "$path?watch=true&resourceVersion=$version&timeoutSeconds=1" --request-timeout=60s \
      >> "$WORK/watch.jsonl" 2> "$WORK/watch.err" || { cat "$WORK/watch.err"; return 1; }
  done < "$WORK/marks"
  "$PY" -I - "$WORK/watch.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    if not line.strip():
        continue
    event = json.loads(line)
    if event["type"] == "ERROR":
        print(f'the API server cannot replay the watch: {event["object"].get("message")}')
        sys.exit(1)
    if event["type"] == "ADDED":
        print(f'{event["object"]["kind"]}/{event["object"]["metadata"]["name"]}')
PY
}
nothing_created() {  # nothing_created <what>: since mark_hook_kinds no Job, ConfigMap or ServiceAccount was created
  local created
  created="$(created_since)" \
    || fail "the API server's watch of the namespace's Jobs, ConfigMaps and ServiceAccounts: $created"
  [ -z "$created" ] || fail "$1 created $(tr '\n' ' ' <<<"$created")"
}
hooks_of() {  # hooks_of <revision>: "<kind>/<name> <hook events>" of each hook that revision of the release carries
  helm_ get hooks "$RELEASE" --namespace "$NAMESPACE" --revision "$1" > "$WORK/hooks-$1.yaml" 2> "$WORK/hooks.err" \
    || { cat "$WORK/hooks.err" >&2; fail "helm get hooks --revision $1 failed"; }
  "$PY" -I - "$WORK/hooks-$1.yaml" <<'PY' || fail "cannot read the hooks of revision $1"
import sys, yaml
for document in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")):
    if document:
        meta = document["metadata"]
        print(f'{document["kind"]}/{meta["name"]} {meta["annotations"]["helm.sh/hook"]}')
PY
}
no_admin_credential_in_use() {  # no pod, and no workload's pod template, refers to the admin Secret
  local found
  k get pods,deployments,statefulsets,daemonsets,replicasets,jobs,cronjobs --output json > "$WORK/workloads.json" \
    || fail "cannot list the namespace's pods and workloads"
  found="$("$PY" -I - "$WORK/workloads.json" "$ADMIN_SECRET_NAME" <<'PY'
import json, sys
secret = sys.argv[2]
def refers(node):
    if isinstance(node, dict):
        if secret in (node.get("secretName"), node.get("name")):
            return True
        return any(refers(value) for value in node.values())
    return isinstance(node, list) and any(refers(value) for value in node)
for item in json.load(open(sys.argv[1], encoding="utf-8"))["items"]:
    if refers(item.get("spec")):
        print(f'{item["kind"]}/{item["metadata"]["name"]}')
PY
)" || fail "cannot check the pods for the admin Secret: $found"
  [ -z "$found" ] || fail "these refer to the admin Secret $ADMIN_SECRET_NAME after the setup: $found"
}
job_ended() {  # job_ended <name>: the job succeeded or failed
  local counts
  counts="$(k get job "$1" --output 'jsonpath={.status.succeeded}/{.status.failed}')" || return 1
  [ "$counts" != / ]
}
job_started() {  # job_started <name>: the job's pod has left Pending, so kubectl logs can read it
  local phases
  phases="$(k get pods --selector "batch.kubernetes.io/job-name=$1" --output 'jsonpath={.items[*].status.phase}')" || return 1
  [ -n "$phases" ] && [[ "$phases" != *Pending* ]]
}
run_task() {  # run_task <task>: a one-off job from the task's CronJob, as the notes say, to its end
  local run
  run="$1-$(date +%Y%m%d%H%M%S)"
  kube "creating job $run" "$NAMESPACE" create job --from="cronjob/$RELEASE-ingest-$1" "$run"
  wait_until "$WAIT_SECONDS" job_started "$run" || fail "the pod of job $run did not start within ${WAIT_SECONDS}s"
  job "$WAIT_SECONDS" k logs --follow "job/$run" > "$WORK/job-$1.follow" 2>&1 \
    || { cat "$WORK/job-$1.follow" >&2; fail "kubectl logs --follow job/$run failed"; }
  wait_until "$WAIT_SECONDS" job_ended "$run" || fail "job $run did not end within ${WAIT_SECONDS}s"
  k logs "job/$run" > "$WORK/job-$1.log" 2>&1 || true
  [ "$(k get job "$run" --output 'jsonpath={.status.succeeded}')" = 1 ] || { cat "$WORK/job-$1.log" >&2; fail "job $run failed"; }
  cmp -s "$WORK/job-$1.follow" "$WORK/job-$1.log" \
    || fail "kubectl logs --follow job/$run printed another log than kubectl logs job/$run afterwards"
  ok "kubectl create job --from=cronjob/$RELEASE-ingest-$1 $run: completed; kubectl logs --follow printed its whole log"
}
# external_demonstration <image>: the synthetic demonstration, by the commands
# the notes print -- the roster on standard input through kubectl run with
# ConfigMap <release>-run's patch, the tasks fixture and rules as jobs from
# their CronJobs -- and the export, as the guide runs it: the CSV on standard
# output, the log on standard error.
external_demonstration() {
  local image="$1" overrides exported
  overrides="$(k get configmap "$RELEASE-run" --output 'jsonpath={.data.overrides}')" || fail "cannot read ConfigMap $RELEASE-run"
  job_input 300 k run "$RELEASE-run" -i --rm --restart=Never --quiet --image="$image" --override-type=json \
      --overrides="$overrides" -- python -m people_pubs.sync.add_people --csv - --no-orcid-lookup \
      < "$DEMO_ROSTER" > "$WORK/roster.log" 2>&1 || { cat "$WORK/roster.log" >&2; fail "the roster import through kubectl run failed"; }
  if grep -q '^Warning: ' "$WORK/roster.log"; then cat "$WORK/roster.log" >&2; fail "kubectl run: the API server warned"; fi
  ok "kubectl run $RELEASE-run -i --rm --image=$image with ConfigMap $RELEASE-run's patch: add_people read the roster from standard input"
  run_task fixture
  run_task rules
  job 300 k run "$RELEASE-run" -i --rm --restart=Never --quiet --image="$image" --override-type=json \
      --overrides="$overrides" -- python -m people_pubs.sync.export_publications \
      > "$WORK/publications.csv" 2> "$WORK/export.log" || { cat "$WORK/export.log" >&2; fail "the export through kubectl run failed"; }
  if grep -q '^Warning: ' "$WORK/export.log"; then cat "$WORK/export.log" >&2; fail "kubectl run: the API server warned"; fi
  exported="$("$PY" -I - "$WORK/publications.csv" "$DEMO_DOI" <<'PY'
import csv, sys
rows = list(csv.reader(open(sys.argv[1], encoding="utf-8", newline="")))
header = ["pub_id", "doi", "title", "year", "venue", "source_of_truth", "internal_authors"]
if not rows or rows[0] != header or any(len(row) != len(header) for row in rows):
    sys.exit(f"not the export's CSV: {rows[:3]}")
items = [dict(zip(header, row)) for row in rows[1:] if row[1] == sys.argv[2]]
expected = {"title": "Synthetic Collective Behaviour Paper", "year": "2024", "internal_authors": "Ada Example"}
if len(items) != 1 or {key: items[0][key] for key in expected} != expected:
    sys.exit(f"{len(items)} rows with the demonstration DOI: {items}")
print(len(rows) - 1)
PY
)" || fail "the export's standard output is not the demonstration's publication list"
  grep -Eq "Exported $exported publications to stdout" "$WORK/export.log" \
    || { cat "$WORK/export.log" >&2; fail "the export's log is not on standard error"; }
  ok "kubectl run $RELEASE-run -i --rm: export_publications wrote $exported row(s) with $DEMO_DOI as CSV to standard output, its log to standard error"
}
# stamped <transform version> <phase>: every row's source provenance carries
# that transform version, as the ingestion that wrote it stamps it.
stamped() {
  local found
  found="$(server_sql "SELECT DISTINCT e.value->>'transform_version' FROM biblio.publications p
    CROSS JOIN LATERAL jsonb_each(p.source_provenance) e ORDER BY 1")" || fail "cannot read the rows' transform versions ($2)"
  [ "$found" = "people_pubs/$1" ] \
    || fail "the rows carry the transform versions '$(tr '\n' ' ' <<<"$found")', not people_pubs/$1 alone ($2)"
}

step 16 "External mode: installing revision 1, the preceding release's chart, against that server, the setup Job as a hook..."
NAMESPACE="$EXTERNAL_NAMESPACE"
restricted_namespace "$NAMESPACE"
SECRET_NAME="$EXTERNAL_SECRET_NAME"
secret_files "$WORK/external"
( cd "$WORK/external" && k create secret generic "$SECRET_NAME" --from-file=secrets/app_readonly_password \
    --from-file=secrets/app_writer_password ) > /dev/null || fail "cannot create the Secret $SECRET_NAME"
admin_secret create
kube "the CA's ConfigMap" "$NAMESPACE" create configmap "$CA_CONFIGMAP" --from-file="$CA_KEY=$TLS/ca.crt"
ok "namespace $NAMESPACE (restricted): Secrets $SECRET_NAME and $ADMIN_SECRET_NAME, ConfigMap $CA_CONFIGMAP"
cat > "$WORK/values-external.yaml" <<YAML
externalDatabase:
  host: $SERVER_HOST
ingestion:
  tasks:
    fixture:
      command: [python, -m, people_pubs.sync.fixture_demo]
    rules:
      command: [python, -m, people_pubs.sync.attribution_rules, add, --affiliation, "$DEMO_AFFILIATION"]
YAML
render "$PRECEDING_CHART" "$PRECEDING_EXAMPLE" "$WORK/values-published.yaml" "$WORK/values-external.yaml"
dry_run "$WORK/rendered-objects.yaml" "the objects of revision 1, the chart of PubDex $PRECEDING"
cp "$WORK/rendered-pods.yaml" "$WORK/external-pods-1.yaml"
# What step 17's upgrade renders, admitted while none of it exists yet.
render "$CHART" "$EXTERNAL_EXAMPLE" "$WORK/values-published.yaml" "$WORK/values-external.yaml" "$WORK/values-2.yaml"
dry_run "$WORK/rendered-objects.yaml" "the objects of this checkout's chart with the upgrade's values"
cp "$WORK/rendered-pods.yaml" "$WORK/external-pods-2.yaml"
started=$SECONDS
helm_run install "$RELEASE" "$PRECEDING_CHART" --values "$PRECEDING_EXAMPLE" --values "$WORK/values-published.yaml" \
  --values "$WORK/values-external.yaml" --wait --timeout "${WAIT_SECONDS}s"
ok "helm install --wait of the chart of PubDex $PRECEDING with its examples/values-external.yaml: the setup hook prepared $DATABASE ($((SECONDS - started))s)"
[ -z "$(setup_hooks_left)" ] || fail "the setup's hook objects are still there: $(setup_hooks_left | tr '\n' ' ')"
admin_secret delete
no_admin_credential_in_use
ok "the setup's Job, ConfigMap and ServiceAccount are gone; the admin Secret is deleted; no pod or workload refers to it"
check_revision "external revision 1" "$BASE_APP_REF" "" 1 INFO
record_is server_sql "$PRECEDING_RECORD" "external revision 1"
ok "the database records the schema changes of PubDex $PRECEDING: $(cut -d' ' -f2 <<<"$RECORD" | paste -sd' ' -)"
external_demonstration "$BASE_APP_REF"
read_back "external revision 1"
tls="$(server_sql "SELECT count(*) || ' ' || bool_and(s.ssl) || ' ' || min(s.version) FROM pg_stat_ssl s
  JOIN pg_stat_activity a USING (pid) WHERE a.usename = 'app_readonly'")" || fail "cannot ask the server for its sessions"
read -r tls_sessions tls_all tls_version <<<"$tls"
[ "$tls_sessions" -ge 1 ] && [ "$tls_all" = true ] || fail "the read service's sessions are not all TLS: $tls"
ok "the read service's $tls_sessions session(s) use $tls_version, the server verified by the run's CA (verify-full)"
dry_run "$WORK/external-pods-1.yaml"
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"

# --- 17. external upgrade ----------------------------------------------------------------------

step 17 "External mode: upgrading to this checkout's chart and the images under test with changed values, with the schema upgrade..."
ids_before="$(schema_ids)" || fail "cannot read the schema's ids"
started=$SECONDS
admin_secret create
helm_run upgrade "$RELEASE" "$CHART" --values "$EXTERNAL_EXAMPLE" --values "$WORK/values-published.yaml" \
  --values "$WORK/values-external.yaml" --values "$WORK/values-2.yaml" --wait --timeout "${WAIT_SECONDS}s"
admin_secret delete
ok "helm upgrade --wait, the setup hook and the schema upgrade's hook, in $((SECONDS - started))s; the admin Secret deleted again"
[ -z "$(setup_hooks_left)" ] || fail "hook objects are still there: $(setup_hooks_left | tr '\n' ' ')"
record_is server_sql "$CHECKOUT_RECORD" "external revision 2"
[ "$(cut -d' ' -f5 <<<"$RECORD" | sort -u)" = "$ADMIN_USER" ] \
  || fail "the schema changes were not applied as $ADMIN_USER: $(tr '\n' ' ' <<<"$RECORD")"
ok "the database records this checkout's schema changes, $(cut -d' ' -f2 <<<"$RECORD" | paste -sd' ' -), applied by $(applied); Helm deleted both hooks"
check_revision "external revision 2" "$APP_TAG" "" 2 DEBUG
[ "$(schema_ids)" = "$ids_before" ] || fail "the schema's objects have new ids: the setup initialised the database again"
ok "the schema's objects keep their ids ($ids_before): no second initialisation"
no_admin_credential_in_use
read_back "external revision 2"
dry_run "$WORK/external-pods-2.yaml"
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"

# --- 18. external password change ---------------------------------------------------------------

step 18 "External mode: changing the role passwords through the setup..."
started=$SECONDS
secret_files "$WORK/external-rotated"
replace_secret "$WORK/external-rotated" app_readonly_password app_writer_password
admin_secret create
mark_hook_kinds
# The same values files as step 17, as the guide says: --reuse-values would
# also carry over a setup that a routine upgrade switched off.
helm_run upgrade "$RELEASE" "$CHART" --values "$EXTERNAL_EXAMPLE" --values "$WORK/values-published.yaml" \
  --values "$WORK/values-external.yaml" --values "$WORK/values-2.yaml" --wait --timeout "${WAIT_SECONDS}s"
setup_objects="$(created_since)" \
  || fail "the API server's watch of the namespace's Jobs, ConfigMaps and ServiceAccounts: $setup_objects"
admin_secret delete
ok "kubectl replace of Secret $SECRET_NAME, then helm upgrade with step 17's values files: the setup set the new passwords"
# The replay that steps 19 to 21 rely on, where a setup did run: it has to
# find the setup's three objects and the schema upgrade's two, which Helm
# deleted again.
expected_setup="$(printf '%s\n' "Job/$RELEASE-setup" "ConfigMap/$RELEASE-setup" "ServiceAccount/$RELEASE-setup" \
  "Job/$RELEASE-schema-upgrade" "ServiceAccount/$RELEASE-schema-upgrade" | sort)"
[ "$(sort <<<"$setup_objects")" = "$expected_setup" ] \
  || fail "the replayed watch shows $(tr '\n' ' ' <<<"$setup_objects")created by the upgrade, not the setup's Job, ConfigMap and ServiceAccount and the schema upgrade's Job and ServiceAccount"
[ -z "$(setup_hooks_left)" ] || fail "hook objects are still there: $(setup_hooks_left | tr '\n' ' ')"
ok "the API server's watch, replayed from before the upgrade, shows $(tr '\n' ' ' <<<"$expected_setup")created; Helm deleted them again"
record_kept server_sql "the password change"
ok "the schema upgrade found nothing outstanding: the record of schema changes is unchanged"
restart_api 2 "$APP_TAG"
read_back "external password change"
logins_changed "$SERVER_NAMESPACE" "$SERVER_POD" "$WORK/external-rotated/secrets" "$WORK/external/secrets" app_readonly app_writer
[ "$(schema_ids)" = "$ids_before" ] || fail "the schema's objects have new ids after the password change"
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^3[[:space:]].*deployed' "$WORK/history.log" || { cat "$WORK/history.log" >&2; fail "revision 3 is not deployed"; }
ok "the password change took $((SECONDS - started))s; helm history: revision 3 deployed"

# --- 19. external routine upgrade --------------------------------------------------------------

# A routine upgrade changes no password, so the setup need not run: it runs
# with the setup switched off for this upgrade, and without the admin Secret,
# which step 18 deleted after its upgrade. The same upgrade without the
# switch runs the setup, which has no admin password: it has to fail at once,
# leave the running release as it was, and leave the failed setup's objects
# for the guide's deletion block.
step 19 "External mode: a routine upgrade without the admin Secret, first with the setup Job, which fails, then without it..."
started=$SECONDS
admin_secret_absent "before the routine upgrade"
ok "the API server finds no admin Secret $ADMIN_SECRET_NAME: step 18 deleted it after its upgrade"
cat > "$WORK/values-routine.yaml" <<YAML
api: {logLevel: WARNING}
YAML
if job "$((WAIT_SECONDS + 60))" helm_ upgrade "$RELEASE" "$CHART" --namespace "$NAMESPACE" --values "$EXTERNAL_EXAMPLE" \
    --values "$WORK/values-published.yaml" --values "$WORK/values-external.yaml" --values "$WORK/values-2.yaml" \
    --values "$WORK/values-routine.yaml" \
    --wait --timeout "${WAIT_SECONDS}s" > "$WORK/helm-upgrade-failed.log" 2>&1; then
  cat "$WORK/helm-upgrade-failed.log" >&2
  fail "helm upgrade with the setup and without the admin Secret succeeded"
fi
failed_after=$((SECONDS - started))
if grep -Eq '^Warning: |would violate PodSecurity' "$WORK/helm-upgrade-failed.log"; then
  cat "$WORK/helm-upgrade-failed.log" >&2
  fail "the failed helm upgrade: the API server warned"
fi
grep -q 'pre-upgrade hooks failed' "$WORK/helm-upgrade-failed.log" \
  || { cat "$WORK/helm-upgrade-failed.log" >&2; fail "the helm upgrade without the admin Secret did not fail in its pre-upgrade hook"; }
[ "$failed_after" -lt $((WAIT_SECONDS / 2)) ] \
  || fail "the helm upgrade without the admin Secret failed only after ${failed_after}s, against a timeout of ${WAIT_SECONDS}s"
ok "helm upgrade with the setup and without the admin Secret failed after ${failed_after}s (timeout ${WAIT_SECONDS}s): $(grep -m 1 '^Error: ' "$WORK/helm-upgrade-failed.log")"
k logs "job/$RELEASE-setup" > "$WORK/setup-failed.log" 2> "$WORK/setup-failed.err" \
  || { cat "$WORK/setup-failed.err" >&2; fail "kubectl logs job/$RELEASE-setup failed"; }
grep -q 'PEOPLE_PUBS_SETUP_ADMIN_PASSWORD_FILE names a file that does not exist: .*the Secret that holds it is missing' \
    "$WORK/setup-failed.log" \
  || { cat "$WORK/setup-failed.log" >&2; fail "the failed setup's log does not say that the admin Secret is missing"; }
ok "kubectl logs job/$RELEASE-setup: $(tail -n 1 "$WORK/setup-failed.log")"
# Helm keeps a failed Job, whose policy lacks hook-failed, and deletes the
# hooks of the same run that succeeded before it, by hook-succeeded.
leftovers="$(setup_hooks_left)" || fail "cannot list the setup's objects"
[ "$leftovers" = "job.batch/$RELEASE-setup" ] \
  || fail "the failed setup left $(tr '\n' ' ' <<<"$leftovers")not its Job alone"
ok "the failed setup left its Job alone: Helm deleted the hooks that ran before it, the schema upgrade's ServiceAccount among them, and the schema upgrade's Job never started"
helm_ status "$RELEASE" --namespace "$NAMESPACE" > "$WORK/status.log" 2>&1 || fail "helm status failed"
grep -qx 'STATUS: failed' "$WORK/status.log" && grep -qx 'REVISION: 4' "$WORK/status.log" \
  || { cat "$WORK/status.log" >&2; fail "helm status does not report revision 4 as failed"; }
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^3[[:space:]].*deployed' "$WORK/history.log" && grep -Eq '^4[[:space:]].*failed' "$WORK/history.log" \
  || { cat "$WORK/history.log" >&2; fail "helm history does not show revision 3 deployed and revision 4 failed"; }
ok "helm status: revision 4, STATUS: failed; helm history: revision 3 still deployed, revision 4 failed"
[ "$(pods_of api | cut -d' ' -f1 | sort)" = "$(sort <<<"$API_UIDS")" ] \
  || fail "the failed upgrade replaced read service pods: $(pods_of api)"
[ "$(k get configmap "$RELEASE-config" --output 'jsonpath={.data.PEOPLE_PUBS_API_LOG_LEVEL}')" = DEBUG ] \
  || fail "the failed upgrade changed the ConfigMap's log level"
read_back "after the failed upgrade"
ok "the running release is as step 18 left it: the same read service pods, log level DEBUG, ready, the same publication"
started=$SECONDS
mark_hook_kinds
helm_run upgrade "$RELEASE" "$CHART" --values "$EXTERNAL_EXAMPLE" --values "$WORK/values-published.yaml" \
  --values "$WORK/values-external.yaml" --values "$WORK/values-2.yaml" --values "$WORK/values-routine.yaml" \
  --set externalDatabase.setup.enabled=false --wait --timeout "${WAIT_SECONDS}s"
nothing_created "the routine upgrade"
ok "helm upgrade --set externalDatabase.setup.enabled=false --wait in $((SECONDS - started))s; the API server's watch, replayed from before it, shows no Job, ConfigMap or ServiceAccount created: neither the setup nor the schema upgrade ran"
hooks_of 5 > "$WORK/hooks-5.txt"
if grep -q 'pre-upgrade' "$WORK/hooks-5.txt" || ! grep -q ' test$' "$WORK/hooks-5.txt"; then
  cat "$WORK/hooks-5.txt" >&2
  fail "revision 5 should carry helm test's hook and no pre-upgrade hook"
fi
ok "revision 5 carries no setup hook, only helm test's: $(tr '\n' ' ' < "$WORK/hooks-5.txt")"
check_revision "routine upgrade" "$APP_TAG" "" 2 WARNING same-image
[ "$(schema_ids)" = "$ids_before" ] || fail "the schema's objects have new ids after the routine upgrade"
record_kept server_sql "the routine upgrade"
# The failed setup's Job outlives the routine upgrade, which creates no hook
# to replace it; the guide's block deletes it, and its pod goes with it.
leftovers="$(setup_hooks_left)" || fail "cannot list the setup's objects"
[ "$leftovers" = "job.batch/$RELEASE-setup" ] \
  || fail "after the routine upgrade the failed setup left $(tr '\n' ' ' <<<"$leftovers")not its Job alone"
k delete job,configmap,serviceaccount -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=setup" \
  > "$WORK/delete-setup.log" 2>&1 || { cat "$WORK/delete-setup.log" >&2; fail "deleting the failed setup's objects failed"; }
[ -z "$(setup_hooks_left)" ] || fail "the setup's objects are still there after the deletion: $(setup_hooks_left | tr '\n' ' ')"
wait_until 120 no_setup_pod || fail "the failed setup's pod is still there: $(setup_pods)"
ok "the routine upgrade left the failed setup's Job; kubectl delete job,configmap,serviceaccount -l ... removed it and its pod"
no_admin_credential_in_use
read_back "routine upgrade"
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^5[[:space:]].*deployed' "$WORK/history.log" || { cat "$WORK/history.log" >&2; fail "revision 5 is not deployed"; }
grep -Eq '^4[[:space:]].*failed' "$WORK/history.log" || { cat "$WORK/history.log" >&2; fail "revision 4 is no longer failed"; }
ok "the routine upgrade took $((SECONDS - started))s; helm history: revision 4 failed, revision 5 deployed"

# --- 20. external rollback ---------------------------------------------------------------------

# Helm runs only pre-rollback and post-rollback hooks on a rollback, of which
# the chart has none; revision 1's setup hooks come back with its revision,
# and the replayed watch shows whether one ran.
step 20 "External mode: rolling back to revision 1, the preceding release's chart and image, without the admin Secret..."
started=$SECONDS
admin_secret_absent "before the rollback"
mark_hook_kinds
helm_run rollback "$RELEASE" 1 --wait --timeout "${WAIT_SECONDS}s"
nothing_created "the rollback"
ok "helm rollback to revision 1 in $((SECONDS - started))s; the API server's watch, replayed from before it, shows no Job, ConfigMap or ServiceAccount created"
hooks_of 6 > "$WORK/hooks-6.txt"
grep -qx "Job/$RELEASE-setup pre-install,pre-upgrade" "$WORK/hooks-6.txt" \
  || { cat "$WORK/hooks-6.txt" >&2; fail "revision 6, the rollback, does not carry revision 1's setup Job as a pre-install,pre-upgrade hook"; }
ok "revision 6 carries revision 1's hooks, $(grep -c ' pre-install,pre-upgrade$' "$WORK/hooks-6.txt") of the setup's pre-install,pre-upgrade among them: the rollback ran none of them ($(tr '\n' ' ' < "$WORK/hooks-6.txt"))"
check_revision "external rollback" "$BASE_APP_REF" "" 1 INFO
[ "$(schema_ids)" = "$ids_before" ] || fail "the schema's objects have new ids after the rollback"
record_kept server_sql "the external rollback"
ok "the schema keeps its ids and its record: PubDex $PRECEDING's image serves on this checkout's schema"
no_admin_credential_in_use
read_back "external rollback"
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^6[[:space:]].*deployed.*Rollback to 1' "$WORK/history.log" || { cat "$WORK/history.log" >&2; fail "revision 6 is not the rollback to 1"; }
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"
admin_secret_absent "after the rollback"
ok "the rollback took $((SECONDS - started))s; helm history: revision 6 deployed, rollback to 1; still no admin Secret"

# --- 21. external uninstall --------------------------------------------------------------------

step 21 "External mode: uninstalling, without the admin Secret..."
admin_secret_absent "before the uninstall"
mark_hook_kinds
helm_run uninstall "$RELEASE" --wait --timeout "${WAIT_SECONDS}s"
nothing_created "helm uninstall"
ok "helm uninstall without the admin Secret; the replayed watch shows no Job, ConfigMap or ServiceAccount created"
expected="$(printf '%s\n%s\n' "configmap/$CA_CONFIGMAP" "secret/$SECRET_NAME" | sort)"
wait_until 120 only_documented_left \
  || fail "after helm uninstall the namespace holds $(remaining | tr '\n' ' '), not just $(tr '\n' ' ' <<<"$expected")"
ok "helm uninstall left the operator's Secret and the CA's ConfigMap"
kept="$(server_sql "SELECT (SELECT count(*) FROM pg_roles WHERE rolname IN ('app_readonly', 'app_writer', 'maintenance'))
  || ' ' || (SELECT count(*) FROM biblio.publications WHERE doi = '$DEMO_DOI')")" || fail "cannot ask the server after the uninstall"
[ "$kept" = "3 1" ] || fail "the server does not keep the three roles and the publication after the uninstall: $kept"
ok "the server keeps $DATABASE with the publication and the roles app_readonly, app_writer and maintenance, as the notes say"
k delete secret "$SECRET_NAME" > /dev/null || fail "cannot delete the Secret $SECRET_NAME"
k delete configmap "$CA_CONFIGMAP" > /dev/null || fail "cannot delete the ConfigMap $CA_CONFIGMAP"
[ -z "$(remaining)" ] || fail "the namespace still holds $(remaining | tr '\n' ' ')"
refused="$(kubectl_ get events --all-namespaces --request-timeout=60s --field-selector reason=FailedCreate \
  --output 'jsonpath={range .items[*]}{.involvedObject.namespace}/{.involvedObject.name}: {.message}{"\n"}{end}')" \
  || fail "cannot list the cluster's events"
[ -z "$refused" ] || fail "admission refused pods: $refused"
ok "the namespace holds nothing of the release; no pod of any namespace was refused by admission"

# --- 22. test release A, from its chart package file -------------------------------------------

step 22 "External mode: test release A from its chart package file, with the setup Job, on the server's database created again..."
started=$SECONDS
# What step 21 left on the server goes, as the guide's Uninstall allows: the
# database and the three roles, dropped as the server's superuser, and the
# database is created again for the admin role, as step 15's server had it.
roles="$(server_sql "SELECT string_agg(rolname, ' ' ORDER BY rolname) FROM pg_roles
  WHERE rolname !~ '^pg_' AND rolname NOT IN ('$SERVER_SUPERUSER', '$ADMIN_USER')")" || fail "cannot list the server's roles"
[ "$roles" = "app_readonly app_writer maintenance" ] \
  || fail "besides $SERVER_SUPERUSER and $ADMIN_USER the server holds the roles '$roles', not app_readonly, app_writer and maintenance"
kn "$SERVER_NAMESPACE" exec "$SERVER_POD" --container postgresql -- psql -X -q -v ON_ERROR_STOP=1 \
    --username "$SERVER_SUPERUSER" --dbname postgres -c "DROP DATABASE $DATABASE WITH (FORCE)" \
    -c "DROP ROLE app_readonly, app_writer, maintenance" -c "CREATE DATABASE $DATABASE OWNER $ADMIN_USER" \
  > "$WORK/server-again.log" 2>&1 || { cat "$WORK/server-again.log" >&2; fail "cannot drop and create the database $DATABASE again"; }
managed_server "with its database created again"
ok "dropped $DATABASE and the roles $roles; $DATABASE created again for $ADMIN_USER, without a schema of PubDex"
reset_release_state
secret_files "$WORK/external-a"
( cd "$WORK/external-a" && k create secret generic "$SECRET_NAME" --from-file=secrets/app_readonly_password \
    --from-file=secrets/app_writer_password ) > /dev/null || fail "cannot create the Secret $SECRET_NAME"
admin_secret create
kube "the CA's ConfigMap" "$NAMESPACE" create configmap "$CA_CONFIGMAP" --from-file="$CA_KEY=$TLS/ca.crt"
ok "namespace $NAMESPACE: Secrets $SECRET_NAME, of new passwords, and $ADMIN_SECRET_NAME, ConfigMap $CA_CONFIGMAP"
# Both images of each test release, set explicitly by the local tags only
# this run gives them; the node has all four and never pulls.
cat > "$WORK/values-a.yaml" <<YAML
image: {registry: "", repository: ${A_APP%:*}, tag: "${A_APP##*:}", digest: "", pullPolicy: Never}
postgresImage: {registry: "", repository: ${A_PG%:*}, tag: "${A_PG##*:}", digest: "", pullPolicy: Never}
YAML
cat > "$WORK/values-b1.yaml" <<YAML
image: {registry: "", repository: ${B1_APP%:*}, tag: "${B1_APP##*:}", digest: "", pullPolicy: Never}
postgresImage: {registry: "", repository: ${B1_PG%:*}, tag: "${B1_PG##*:}", digest: "", pullPolicy: Never}
YAML
started=$SECONDS
package_example "$A_PACKAGE"
helm_run install "$RELEASE" "$A_PACKAGE" --values "$EXTERNAL_EXAMPLE" --values "$WORK/values-external.yaml" \
  --values "$WORK/values-a.yaml" --wait --timeout "${WAIT_SECONDS}s"
ok "helm install --wait of test release A from ${A_PACKAGE##*/}, with the setup hook and the schema upgrade's hook ($((SECONDS - started))s)"
[ -z "$(setup_hooks_left)" ] || fail "hook objects are still there: $(setup_hooks_left | tr '\n' ' ')"
admin_secret delete
no_admin_credential_in_use
ok "Helm deleted both hooks; the admin Secret is deleted; no pod or workload refers to it"
check_revision "test release A" "$A_APP" "" 1 INFO
record_is server_sql "$A_RECORD" "test release A"
[ "$(applied)" = "$A_VERSION as $ADMIN_USER" ] || fail "the record says that $(applied) applied the schema changes, not $A_VERSION as $ADMIN_USER"
RECORD_A="$RECORD"
ok "the database records A's schema changes, $(cut -d' ' -f2 <<<"$RECORD" | paste -sd' ' -), applied by $(applied)"
external_demonstration "$A_APP"
stamped "$A_TRANSFORM" "test release A"
read_back "test release A"
ok "test release A serves the demonstration's publication; every row carries its transform version people_pubs/$A_TRANSFORM"

# --- 23. a routine upgrade to B1, which cannot complete --------------------------------------------

# api_pods: "<name> <uid> <image> <ready> <started>" of each read service pod not being deleted.
api_pods() {
  k get pods --selector "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=api" --output go-template \
    --template '{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}} {{.metadata.uid}} {{(index .spec.containers 0).image}}{{range .status.containerStatuses}} {{.ready}} {{.started}}{{end}}{{"\n"}}{{end}}{{end}}'
}
B1_POD=""
B1_POD_UID=""
b1_pod_started() {  # B1's one read service pod runs, its startup probe passed, and it is not ready
  local line ready started
  line="$(api_pods | awk -v image="$B1_APP" '$3 == image')" || return 1
  [ -n "$line" ] && [ "$(grep -c . <<<"$line")" = 1 ] || return 1
  read -r B1_POD B1_POD_UID _ ready started <<<"$line"
  [ "$ready" = false ] && [ "$started" = true ]
}
b1_probe_failed() {  # the kubelet reported B1's pod not ready, by its readiness probe
  k get events --field-selector "involvedObject.name=$B1_POD,reason=Unhealthy" \
    --output 'jsonpath={range .items[*]}{.message}{"\n"}{end}' > "$WORK/b1-events.txt" 2>/dev/null \
    && grep -q '^Readiness probe failed: HTTP probe failed with statuscode: 503' "$WORK/b1-events.txt"
}

step 23 "External mode: a routine upgrade to test release B1, without the setup, which cannot complete before B1's schema upgrade..."
started=$SECONDS
admin_secret_absent "before the routine upgrade to B1"
package_example "$B1_PACKAGE"
mark_hook_kinds
if job "$((SHORT_WAIT_SECONDS + 60))" helm_ upgrade "$RELEASE" "$B1_PACKAGE" --namespace "$NAMESPACE" \
    --values "$EXTERNAL_EXAMPLE" --values "$WORK/values-external.yaml" --values "$WORK/values-b1.yaml" \
    --set externalDatabase.setup.enabled=false --wait --timeout "${SHORT_WAIT_SECONDS}s" > "$WORK/helm-upgrade-b1.log" 2>&1; then
  cat "$WORK/helm-upgrade-b1.log" >&2
  fail "the routine upgrade to B1 completed, although B1's schema change is outstanding"
fi
failed_after=$((SECONDS - started))
# Helm logs its own failure as level=WARN msg="upgrade failed"; any other
# warning, the API server's or the client's, fails the run.
if grep -Ev '^level=WARN msg="upgrade failed" ' "$WORK/helm-upgrade-b1.log" \
     | grep -Eq '^W[0-9]{4} |^Warning: |would violate PodSecurity|level=WARN'; then
  cat "$WORK/helm-upgrade-b1.log" >&2
  fail "the routine upgrade to B1: helm printed a warning"
fi
grep -q '^Error: UPGRADE FAILED: ' "$WORK/helm-upgrade-b1.log" \
  || { cat "$WORK/helm-upgrade-b1.log" >&2; fail "the routine upgrade to B1 did not fail as an upgrade"; }
[ "$failed_after" -ge "$SHORT_WAIT_SECONDS" ] \
  || { cat "$WORK/helm-upgrade-b1.log" >&2; fail "the routine upgrade to B1 failed after ${failed_after}s, before its timeout of ${SHORT_WAIT_SECONDS}s"; }
ok "helm upgrade --set externalDatabase.setup.enabled=false --wait --timeout ${SHORT_WAIT_SECONDS}s failed after ${failed_after}s:"
grep '^Error: ' "$WORK/helm-upgrade-b1.log" | sed 's/^/        /'
nothing_created "the routine upgrade to B1"
ok "the replayed watch shows no Job, ConfigMap or ServiceAccount created: neither the setup nor the schema upgrade ran"
helm_ status "$RELEASE" --namespace "$NAMESPACE" > "$WORK/status.log" 2>&1 || fail "helm status failed"
grep -qx 'STATUS: failed' "$WORK/status.log" && grep -qx 'REVISION: 2' "$WORK/status.log" \
  || { cat "$WORK/status.log" >&2; fail "helm status does not report revision 2 as failed"; }
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^1[[:space:]].*deployed' "$WORK/history.log" && grep -Eq '^2[[:space:]].*failed' "$WORK/history.log" \
  || { cat "$WORK/history.log" >&2; fail "helm history does not show revision 1 deployed and revision 2 failed"; }
ok "helm status and helm history:"
grep -E '^(REVISION|STATUS|DESCRIPTION): ' "$WORK/status.log" | sed 's/^/        /'
sed 's/^/        /' "$WORK/history.log"
wait_until "$WAIT_SECONDS" b1_pod_started || fail "B1's read service pod did not start, not ready: $(api_pods)"
wait_until 60 b1_probe_failed || fail "no readiness probe of B1's pod $B1_POD failed with 503: $(cat "$WORK/b1-events.txt")"
k get pods --selector "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=api" > "$WORK/pods.txt" 2>&1 \
  || fail "kubectl get pods failed"
ok "kubectl get pods: B1's pod $B1_POD runs and is not ready; its events: $(sort -u "$WORK/b1-events.txt" | paste -sd';' -)"
sed 's/^/        /' "$WORK/pods.txt"
start_forward "pod/$B1_POD"
[ "$(http /readyz "$WORK/readyz-b1.json")" = 503 ] || fail "B1's pod: GET /readyz did not answer 503"
[ "$(http /v0/publications "$WORK/publications-b1.json")" = 503 ] || fail "B1's pod: GET /v0/publications did not answer 503"
stop_forward
message="$("$PY" -I - "$WORK/readyz-b1.json" "$WORK/publications-b1.json" "$B1_CHANGE" <<'PY'
import json, sys
ready = json.load(open(sys.argv[1], encoding="utf-8"))
publications = json.load(open(sys.argv[2], encoding="utf-8"))
assert ready["status"] == "not_ready" and ready["reason"] == "schema_upgrade_required", ready
message = ready["message"]
assert f"missing schema changes: {sys.argv[3]}. " in message, message
assert message.endswith(" Run the schema upgrade: python -m people_pubs.schema upgrade."), message
assert publications == {"error": {"code": "schema_upgrade_required", "message": message}}, publications
print(message)
PY
)" || fail "B1's pod does not answer with the schema upgrade it needs: $message"
ok "B1's pod: GET /readyz and GET /v0/publications answer 503 schema_upgrade_required: $message"
k logs "$B1_POD" > "$WORK/b1-pod.log" 2>&1 || { cat "$WORK/b1-pod.log" >&2; fail "kubectl logs $B1_POD failed"; }
grep -F "schema_upgrade_required: $message" "$WORK/b1-pod.log" > "$WORK/b1-refusal.log" \
  || { tail -n 20 "$WORK/b1-pod.log" >&2; fail "the log of B1's pod does not name the schema upgrade it needs"; }
ok "kubectl logs $B1_POD: $(tail -n 1 "$WORK/b1-refusal.log")"
a_pod="$(pods_of api | awk -v image="$A_APP" '$2 == image')"
[ "$(cut -d' ' -f1 <<<"$a_pod")" = "$API_UIDS" ] && [ "$(cut -d' ' -f4 <<<"$a_pod")" = true ] \
  || fail "A's read service pod is not the one before the upgrade, or not ready: $(pods_of api)"
record_kept server_sql "the routine upgrade to B1"
read_back "while B1's pod is not ready"
ok "A's pod is the same and ready, and serves the publication; the record of schema changes is unchanged ($((SECONDS - started))s)"

# --- 24. the upgrade to B1 with the schema upgrade ------------------------------------------------

step 24 "External mode: upgrading to test release B1 with the setup and the admin Secret, which runs B1's schema upgrade..."
started=$SECONDS
package_example "$B1_PACKAGE"
admin_secret create
mark_hook_kinds
helm_run upgrade "$RELEASE" "$B1_PACKAGE" --values "$EXTERNAL_EXAMPLE" --values "$WORK/values-external.yaml" \
  --values "$WORK/values-b1.yaml" --wait --timeout "${WAIT_SECONDS}s"
setup_objects="$(created_since)" \
  || fail "the API server's watch of the namespace's Jobs, ConfigMaps and ServiceAccounts: $setup_objects"
admin_secret delete
# The same objects as the password change's upgrade of step 18 created.
[ "$(sort <<<"$setup_objects")" = "$expected_setup" ] \
  || fail "the replayed watch shows $(tr '\n' ' ' <<<"$setup_objects")created by the upgrade, not the setup's Job, ConfigMap and ServiceAccount and the schema upgrade's Job and ServiceAccount"
[ -z "$(setup_hooks_left)" ] || fail "hook objects are still there: $(setup_hooks_left | tr '\n' ' ')"
ok "helm upgrade --wait in $((SECONDS - started))s; the replayed watch shows $(tr '\n' ' ' <<<"$expected_setup")created, and Helm deleted them again; the admin Secret deleted again"
record_is server_sql "$B1_RECORD" "test release B1"
[ "$(head -n -1 <<<"$RECORD")" = "$RECORD_A" ] && [ "$(tail -n 1 <<<"$RECORD" | cut -d' ' -f4,5)" = "$B1_VERSION $ADMIN_USER" ] \
  || fail "the record of schema changes is not A's with B1's change applied by $B1_VERSION as $ADMIN_USER: $(tr '\n' ' ' <<<"$RECORD")"
compatible="$(server_sql "SELECT backward_compatible FROM pubdex.schema_changes WHERE name = '$B1_CHANGE'")" \
  || fail "cannot read whether $B1_CHANGE is backward compatible"
[ "$compatible" = t ] || fail "the record says that $B1_CHANGE is not backward compatible: '$compatible'"
ok "the database records B1's schema changes: A's as they were, and $B1_CHANGE, backward compatible, applied by $B1_VERSION as $ADMIN_USER"
check_revision "test release B1" "$B1_APP" "" 1 INFO
[ "$API_UIDS" = "$B1_POD_UID" ] || fail "B1's read service pod of step 23 was replaced by $API_UIDS"
ok "B1's pod $B1_POD of step 23, not replaced, turned ready once the schema upgrade had run; A's pod went"
no_admin_credential_in_use
run_task fixture
stamped "$B1_TRANSFORM" "test release B1"
read_back "test release B1"
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^3[[:space:]].*deployed' "$WORK/history.log" || { cat "$WORK/history.log" >&2; fail "revision 3 is not deployed"; }
ok "B1's ingestion task ingested the demonstration's payloads again: every row carries people_pubs/$B1_TRANSFORM; helm history: revision 3 deployed ($((SECONDS - started))s)"

# --- 25. the image rollback to A -------------------------------------------------------------------

step 25 "External mode: rolling back to test release A without the admin Secret, an image rollback after a backward-compatible schema change..."
started=$SECONDS
admin_secret_absent "before the rollback to A"
mark_hook_kinds
helm_run rollback "$RELEASE" 1 --wait --timeout "${WAIT_SECONDS}s"
nothing_created "the rollback to A"
ok "helm rollback to revision 1 in $((SECONDS - started))s; the replayed watch shows no Job, ConfigMap or ServiceAccount created"
check_revision "rollback to A" "$A_APP" "" 1 INFO
record_kept server_sql "the rollback to A"
ok "the schema and its record stay as B1 left them, $B1_CHANGE included; A's read service is ready on them"
no_admin_credential_in_use
run_task fixture
stamped "$A_TRANSFORM" "the rollback to A"
read_back "rollback to A"
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^4[[:space:]].*deployed.*Rollback to 1' "$WORK/history.log" || { cat "$WORK/history.log" >&2; fail "revision 4 is not the rollback to 1"; }
admin_secret_absent "after the rollback to A"
ok "A's ingestion task writes on B1's schema: every row carries people_pubs/$A_TRANSFORM again; helm history: revision 4 deployed, rollback to 1 ($((SECONDS - started))s)"

# --- 26. uninstall ----------------------------------------------------------------------------------

step 26 "External mode: uninstalling test release A, without the admin Secret..."
started=$SECONDS
admin_secret_absent "before the uninstall of A"
helm_run uninstall "$RELEASE" --wait --timeout "${WAIT_SECONDS}s"
wait_until 120 only_documented_left \
  || fail "after helm uninstall the namespace holds $(remaining | tr '\n' ' '), not just $(tr '\n' ' ' <<<"$expected")"
k delete secret "$SECRET_NAME" > /dev/null || fail "cannot delete the Secret $SECRET_NAME"
k delete configmap "$CA_CONFIGMAP" > /dev/null || fail "cannot delete the ConfigMap $CA_CONFIGMAP"
[ -z "$(remaining)" ] || fail "the namespace still holds $(remaining | tr '\n' ' ')"
refused="$(kubectl_ get events --all-namespaces --request-timeout=60s --field-selector reason=FailedCreate \
  --output 'jsonpath={range .items[*]}{.involvedObject.namespace}/{.involvedObject.name}: {.message}{"\n"}{end}')" \
  || fail "cannot list the cluster's events"
[ -z "$refused" ] || fail "admission refused pods: $refused"
ok "helm uninstall left the operator's Secret and the CA's ConfigMap, both deleted; no pod of any namespace was refused by admission ($((SECONDS - started))s)"

# --- 27. the first cluster goes ------------------------------------------------------------------

step 27 "Deleting the first cluster and its network; the floor's cluster runs on its own..."
started=$SECONDS
CONTEXT_VERIFIED=0
job 300 kind_ delete cluster --name "$CLUSTER" --kubeconfig "$KIND_KUBECONFIG" > "$WORK/kind-delete.log" 2>&1 \
  || { tail -n 20 "$WORK/kind-delete.log" >&2; fail "kind delete cluster $CLUSTER failed"; }
docker network rm "$NETWORK" > /dev/null || fail "cannot remove the network $NETWORK"
[ -z "$(docker ps -aq --filter "label=$KIND_LABEL=$CLUSTER")" ] || fail "containers of $CLUSTER are still there"
! docker network inspect "$NETWORK" > /dev/null 2>&1 || fail "the network $NETWORK is still there"
[ -z "$(left_volumes)" ] || fail "the volumes of $NODE are still there: $(left_volumes | tr '\n' ' ')"
CLUSTER_STARTED=0
ok "kind deleted $CLUSTER; its node, the node's volumes and the network $NETWORK are gone ($((SECONDS - started))s)"

# --- 28. the floor's cluster --------------------------------------------------------------------

FLOOR_NODE="$FLOOR_CLUSTER-control-plane"
floor_release="${PINNED_KIND_FLOOR_NODE_IMAGE#kindest/node:}"
floor_release="${floor_release%%@*}"

# audit_refusals: the operator's requests the floor's API server refused, one
# a line, by verb and resource, from the audit log in its node.
audit_refusals() {
  docker exec "$FLOOR_NODE" cat "$AUDIT_LOG" > "$WORK/floor/audit.jsonl" || return 1
  "$PY" -I - "$WORK/floor/audit.jsonl" "$OPERATOR_USER" <<'PY'
import json, sys
refused = set()
for line in open(sys.argv[1], encoding="utf-8"):
    event = json.loads(line)
    if event["user"]["username"] != sys.argv[2]:
        continue
    if event.get("annotations", {}).get("authorization.k8s.io/decision") != "forbid":
        continue
    ref = event.get("objectRef") or {}
    resource = ref.get("resource", event["requestURI"]) + ("/" + ref["subresource"] if ref.get("subresource") else "")
    refused.add(f'  {event["verb"]} {resource} (API group "{ref.get("apiGroup", "")}")')
print("\n".join(sorted(refused)))
PY
}
check_audit() {  # check_audit <phase>: the API server refused no request of the operator so far
  local refused
  refused="$(audit_refusals)" || fail "cannot read the floor's audit log in its node"
  [ -z "$refused" ] \
    || fail "the API server refused requests of the operator ($1): rights the Role, KUBERNETES.md's list, lacks"
  ok "the audit log shows no refused request of the operator ($1)"
}

step 28 "The floor: creating a Kubernetes $floor_release cluster on a network of its own, with an audit log of the operator's requests..."
NAMESPACE="$EVAL_NAMESPACE"
SECRET_NAME="$EVAL_SECRET_NAME"
reset_release_state
# The API server logs the operator's requests, their metadata only.
( umask 077
  cat > "$WORK/floor/audit-policy.yaml" <<YAML
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: [RequestReceived]
rules:
  - level: Metadata
    users: ["$OPERATOR_USER"]
  - level: None
YAML
)
# kind's configuration: the policy mounted into the node, and the API
# server's audit options (kind's documented way, a kubeadm patch).
"$PY" -I - "$WORK/floor/audit-policy.yaml" "$AUDIT_POLICY" "$AUDIT_LOG" > "$WORK/floor/kind-config.json" <<'PY' \
  || fail "cannot write the floor's kind configuration"
import json, posixpath, sys
policy, policy_in_node, log = sys.argv[1:4]
patch = "\n".join([
    "kind: ClusterConfiguration",
    "apiServer:",
    "  extraArgs:",
    f"    audit-policy-file: {policy_in_node}",
    f"    audit-log-path: {log}",
    "  extraVolumes:",
    f"    - {{name: pubdex-audit-policy, hostPath: {posixpath.dirname(policy_in_node)}, "
    f"mountPath: {posixpath.dirname(policy_in_node)}, readOnly: true, pathType: DirectoryOrCreate}}",
    f"    - {{name: pubdex-audit-log, hostPath: {posixpath.dirname(log)}, "
    f"mountPath: {posixpath.dirname(log)}, readOnly: false, pathType: DirectoryOrCreate}}",
])
print(json.dumps({"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4", "nodes": [{
    "role": "control-plane", "kubeadmConfigPatches": [patch],
    "extraMounts": [{"hostPath": policy, "containerPath": policy_in_node, "readOnly": True}]}]}, indent=1))
PY
create_cluster "$FLOOR_CLUSTER" "$FLOOR_NETWORK" "$PINNED_KIND_FLOOR_NODE_IMAGE" "$FLOOR_KUBECONFIG" \
  --config "$WORK/floor/kind-config.json"
[ "$server_version" = "$floor_release" ] || fail "the floor's API server reports $server_version, not $floor_release"
docker exec "$FLOOR_NODE" grep -q -e "--audit-log-path=$AUDIT_LOG" /etc/kubernetes/manifests/kube-apiserver.yaml \
  || fail "the floor's API server was not started with the audit log $AUDIT_LOG"
FLOOR_AUDIT=1
ok "the API server, Kubernetes $server_version, logs the operator's requests to $AUDIT_LOG in the node"

# --- 29. the images under test, and no published one -------------------------------------------

step 29 "The floor: loading the images under test into the node; the published images are left to the kubelet..."
load_images_under_test
for ref in "$BASE_APP_REF" "$BASE_PG_REF"; do
  if node_image_id "$ref" > /dev/null 2>&1; then
    fail "the floor's node has the published image $ref before the install; the kubelet has to pull it"
  fi
done
ok "the node has neither published image: nothing pulls one before the install"

# --- 30. the operator's access -------------------------------------------------------------------

step 30 "The floor, as the cluster's administrator: the namespace, the operator's ServiceAccount, Role, RoleBinding and token..."
restricted_namespace "$NAMESPACE"
kube "creating the ServiceAccount $OPERATOR" "$NAMESPACE" create serviceaccount "$OPERATOR"
# The operator's rights: exactly those KUBERNETES.md lists under "Access
# rights", in RBAC's verbs; DB/tests/test_cluster_acceptance_script.py holds
# the two to each other. helm --wait reads a Deployment's ReplicaSets, and
# kubectl's WebSocket connections for exec, port-forward and run --stdin need
# get and create on their subresource.
cat > "$WORK/floor/role.yaml" <<'ROLE'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: pubdex-operator
rules:
  - apiGroups: [""]
    resources: [configmaps, secrets, services, serviceaccounts, pods, persistentvolumeclaims]
    verbs: [create, update, patch, delete, get, list, watch]
  - apiGroups: [apps]
    resources: [deployments, statefulsets]
    verbs: [create, update, patch, delete, get, list, watch]
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [create, update, patch, delete, get, list, watch]
  - apiGroups: [networking.k8s.io]
    resources: [networkpolicies, ingresses]
    verbs: [create, update, patch, delete, get, list, watch]
  - apiGroups: [""]
    resources: [pods/log, events]
    verbs: [get, list, watch]
  - apiGroups: [apps]
    resources: [replicasets]
    verbs: [get, list, watch]
  - apiGroups: [apps]
    resources: [deployments/scale]
    verbs: [update, patch]
  - apiGroups: [""]
    resources: [pods/exec, pods/portforward, pods/attach]
    verbs: [create, get]
ROLE
kube "creating the Role $OPERATOR" "$NAMESPACE" create --filename "$WORK/floor/role.yaml"
kube "creating the RoleBinding $OPERATOR" "$NAMESPACE" create rolebinding "$OPERATOR" --role "$OPERATOR" \
  --serviceaccount "$NAMESPACE:$OPERATOR"
k get role "$OPERATOR" --output json > "$WORK/floor/role-applied.json" || fail "cannot read the Role $OPERATOR back"
echo "      the Role $OPERATOR as applied, by API group, resources and verbs:"
"$PY" -I -c '
import json, sys
for rule in json.load(open(sys.argv[1], encoding="utf-8"))["rules"]:
    print("        %s %s: %s" % (rule["apiGroups"][0] or "core", " ".join(rule["resources"]), " ".join(rule["verbs"])))
' "$WORK/floor/role-applied.json"
# A token of one hour, written into the operator's own kubeconfig only.
( umask 077 && k create token "$OPERATOR" --duration 3600s > "$WORK/floor/token" ) 2> "$WORK/kubectl.err" \
  || { cat "$WORK/kubectl.err" >&2; fail "kubectl create token failed"; }
"$PY" -I - "$FLOOR_KUBECONFIG" "$WORK/floor/token" "$OPERATOR_KUBECONFIG" "$OPERATOR_CONTEXT" "$NAMESPACE" <<'PY' \
  || fail "cannot write the operator's kubeconfig"
import os, sys, yaml
admin, token_file, path, name, namespace = sys.argv[1:6]
cluster = yaml.safe_load(open(admin, encoding="utf-8"))["clusters"][0]["cluster"]
with open(token_file, encoding="utf-8") as handle:
    credential = handle.read().strip()
config = {"apiVersion": "v1", "kind": "Config", "current-context": name,
          "clusters": [{"name": name, "cluster": cluster}],
          "users": [{"name": name, "user": {"token": credential}}],
          "contexts": [{"name": name, "context": {"cluster": name, "user": name, "namespace": namespace}}]}
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
    yaml.safe_dump(config, handle)
PY
rm -f -- "$WORK/floor/token"
use_kubeconfig "$OPERATOR_KUBECONFIG" "$OPERATOR_CONTEXT"
assert_own_cluster
whoami="$(kubectl_ auth whoami --output 'jsonpath={.status.userInfo.username}' 2>&1)" \
  || fail "kubectl auth whoami failed with the operator's kubeconfig: $whoami"
[ "$whoami" = "$OPERATOR_USER" ] || fail "the operator's kubeconfig authenticates as '$whoami', not $OPERATOR_USER"
if kubectl_ auth can-i create namespaces > "$WORK/can-i.txt" 2>&1; then fail "the operator may create namespaces"; fi
docker exec "$FLOOR_NODE" grep -q -F "\"username\":\"$OPERATOR_USER\"" "$AUDIT_LOG" \
  || fail "the floor's audit log records no request of the operator"
ok "from here on the operator: the API server knows it as $whoami, which may not create a namespace; the audit log records it"

# --- 31. Secret and server-side validation, as the operator ---------------------------------------

step 31 "The floor, as the operator: the Secret and server-side dry runs of revision 1, this checkout's chart, the two ingress examples and the scheduling values..."
secret_files "$WORK/floor/eval"
( cd "$WORK/floor/eval" && k create secret generic "$SECRET_NAME" --from-file=secrets/postgres_password \
    --from-file=secrets/app_readonly_password --from-file=secrets/app_writer_password ) >/dev/null \
  || fail "cannot create the Secret $SECRET_NAME"
ok "Secret $SECRET_NAME from create_secrets.sh's files"
render "$PRECEDING_CHART" "$WORK/values-published.yaml" "$WORK/values-1.yaml"
dry_run "$WORK/rendered-objects.yaml" "the objects of revision 1, the chart of PubDex $PRECEDING"
cp "$WORK/rendered-pods.yaml" "$WORK/floor/eval-pods-1.yaml"
render "$CHART" "$WORK/values-published.yaml" "$WORK/values-1.yaml" "$WORK/values-2.yaml"
dry_run "$WORK/rendered-objects.yaml" "the objects of this checkout's chart with the upgrade's values"
cp "$WORK/rendered-pods.yaml" "$WORK/floor/eval-pods-2.yaml"
admit_values
check_audit "the Secret and the dry runs"

# --- 32. install: the kubelet pulls the published images ----------------------------------------

step 32 "The floor, as the operator: installing revision 1, the preceding release's chart; the kubelet pulls the published images..."
started=$SECONDS
helm_run install "$RELEASE" "$PRECEDING_CHART" --values "$WORK/values-published.yaml" --values "$WORK/values-1.yaml" \
  --wait --timeout "${WAIT_SECONDS}s"
ok "helm install --wait of the chart of PubDex $PRECEDING, the demonstration included, in $((SECONDS - started))s"
[ "$(demo_completions)" -ge 1 ] || fail "no Completed event of the demonstration's Job $RELEASE-demo"
check_revision "floor revision 1" "$BASE_APP_REF" "$BASE_PG_REF" 1 INFO
record_is eval_sql "$PRECEDING_RECORD" "floor revision 1"
ok "the database records the schema changes of PubDex $PRECEDING: $(cut -d' ' -f2 <<<"$RECORD" | paste -sd' ' -)"
policies="$(k get deployment "$RELEASE-api" --output 'jsonpath={.spec.template.spec.containers[*].imagePullPolicy}') $(
  k get statefulset "$RELEASE-postgresql" --output 'jsonpath={.spec.template.spec.initContainers[*].imagePullPolicy} {.spec.template.spec.containers[*].imagePullPolicy}')"
[ -z "$(tr ' ' '\n' <<<"$policies" | grep -vx -e IfNotPresent -e '')" ] \
  || fail "revision 1's containers do not all pull IfNotPresent, its chart's default: $policies"
k get events --output json > "$WORK/floor/events.json" || fail "cannot read the namespace's events"
pulls="$("$PY" -I - "$WORK/floor/events.json" "$BASE_APP_REF" "$BASE_PG_REF" <<'PY'
import json, sys
events = [event for event in json.load(open(sys.argv[1], encoding="utf-8"))["items"]
          if (event.get("source") or {}).get("component") == "kubelet"]
for ref in sys.argv[2:]:
    pulling = [event for event in events if event["reason"] == "Pulling" and f'"{ref}"' in event["message"]]
    pulled = [event for event in events
              if event["reason"] == "Pulled" and event["message"].startswith(f'Successfully pulled image "{ref}"')]
    if not pulling or not pulled:
        sys.exit(f"no event of the kubelet pulling {ref}")
    print(f'      {pulled[0]["involvedObject"]["kind"]} {pulled[0]["involvedObject"]["name"]}: {pulled[0]["message"]}')
PY
)" || fail "the pods' events do not show the kubelet pulling the published images: $pulls"
echo "$pulls"
for ref in "$BASE_APP_REF" "$BASE_PG_REF"; do
  node_image_id "$ref" > "$WORK/node-image.txt" || fail "the node has no image $ref after the install"
  grep -q " ${ref%%:*}@${ref##*@}\( \|\$\)" "$WORK/node-image.txt" \
    || fail "the image the kubelet pulled for $ref does not carry the digest ${ref##*@}: $(cat "$WORK/node-image.txt")"
done
ok "the kubelet pulled both published images by digest, anonymously, with pull policy IfNotPresent; the node's images carry the digests"
read_back "floor revision 1"
dry_run "$WORK/floor/eval-pods-1.yaml"
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"
check_audit "the install and helm test"

# --- 33. upgrade, as the operator ----------------------------------------------------------------

step 33 "The floor, as the operator: upgrading to this checkout's chart and the images under test with changed values, with the schema upgrade..."
started=$SECONDS
helm_run upgrade "$RELEASE" "$CHART" --values "$WORK/values-published.yaml" --values "$WORK/values-1.yaml" \
  --values "$WORK/values-2.yaml" --wait --wait-for-jobs --timeout "${WAIT_SECONDS}s"
ok "helm upgrade --wait --wait-for-jobs in $((SECONDS - started))s"
schema_job "$RELEASE-schema-upgrade-2" "floor revision 2" "$APPLIED"
record_is eval_sql "$CHECKOUT_RECORD" "floor revision 2"
ok "the database records this checkout's schema changes, $(cut -d' ' -f2 <<<"$RECORD" | paste -sd' ' -), applied by $(applied)"
check_revision "floor revision 2" "$APP_TAG" "$PG_TAG" 2 DEBUG
ok "the kubelet started the images kind loaded, with pull policy Never"
check_database "floor revision 2"
read_back "floor revision 2"
dry_run "$WORK/floor/eval-pods-2.yaml"
helm_run test "$RELEASE" --timeout "${WAIT_SECONDS}s"
ok "helm test passed"
check_audit "the upgrade"

# --- 34. rollback, as the operator ---------------------------------------------------------------

step 34 "The floor, as the operator: rolling back to revision 1..."
started=$SECONDS
helm_run rollback "$RELEASE" 1 --wait --timeout "${WAIT_SECONDS}s"
ok "helm rollback to revision 1 in $((SECONDS - started))s"
check_revision "floor rollback" "$BASE_APP_REF" "$BASE_PG_REF" 1 INFO
check_database "floor rollback"
record_kept eval_sql "floor rollback"
wait_until 60 no_schema_job || fail "a schema upgrade Job outlived the rollback to revision 1, which has none: $(schema_jobs | tr '\n' ' ')"
ok "the schema and its record stay as the upgrade left them; revision 2's schema upgrade Job went with its revision"
read_back "floor rollback"
helm_ history "$RELEASE" --namespace "$NAMESPACE" > "$WORK/history.log" 2>&1 || fail "helm history failed"
grep -Eq '^3[[:space:]].*deployed.*Rollback to 1' "$WORK/history.log" || { cat "$WORK/history.log" >&2; fail "revision 3 is not the rollback to 1"; }
ok "helm history: revision 3 deployed, rollback to 1"
check_audit "the rollback"

# --- 35. uninstall, as the operator; what is left, as the administrator -------------------------

as_administrator() { use_kubeconfig "$FLOOR_KUBECONFIG" "kind-$FLOOR_CLUSTER"; }
as_operator() { use_kubeconfig "$OPERATOR_KUBECONFIG" "$OPERATOR_CONTEXT"; }
token_refused() { ! kubectl_ auth whoami > "$WORK/whoami.txt" 2>&1 && grep -q Unauthorized "$WORK/whoami.txt"; }

step 35 "The floor, as the operator: uninstalling; then, as the administrator, nothing of the release is left..."
helm_run uninstall "$RELEASE" --wait --timeout "${WAIT_SECONDS}s"
as_administrator
claim="data-$RELEASE-postgresql-0"
access="$(printf '%s\n' "role.rbac.authorization.k8s.io/$OPERATOR" "rolebinding.rbac.authorization.k8s.io/$OPERATOR" \
  "serviceaccount/$OPERATOR")"
expected="$(printf '%s\n%s\n%s\n' "persistentvolumeclaim/$claim" "secret/$SECRET_NAME" "$access" | sort)"
wait_until 120 only_documented_left \
  || fail "after helm uninstall the namespace holds $(remaining | tr '\n' ' '), not just $(tr '\n' ' ' <<<"$expected")"
ok "helm uninstall left the database volume's claim and the operator's Secret, as the notes say, besides the operator's access"
volume="$(k get persistentvolumeclaim "$claim" --output 'jsonpath={.spec.volumeName}')"
[ -n "$volume" ] || fail "the claim $claim names no volume"
as_operator
# The commands of the notes (NOTES.txt) and of the Secret's creation.
k delete pvc "$claim" --wait=true >/dev/null || fail "cannot delete the claim $claim"
k delete secret "$SECRET_NAME" >/dev/null || fail "cannot delete the Secret $SECRET_NAME"
check_audit "the whole lifecycle"
as_administrator
wait_until 120 volume_gone || fail "the volume $volume of the deleted claim is still there"
expected="$(sort <<<"$access")"
[ "$(remaining)" = "$expected" ] || fail "the namespace still holds $(remaining | tr '\n' ' ')"
refused="$(kubectl_ get events --all-namespaces --request-timeout=60s --field-selector reason=FailedCreate \
  --output 'jsonpath={range .items[*]}{.involvedObject.namespace}/{.involvedObject.name}: {.message}{"\n"}{end}')" \
  || fail "cannot list the cluster's events"
[ -z "$refused" ] || fail "admission refused pods: $refused"
ok "the namespace holds nothing of the release but the operator's access; the volume is gone; admission refused no pod"
"$PY" -I - "$WORK/floor/audit.jsonl" "$OPERATOR_USER" "$WORK/floor/role.yaml" <<'PY' || fail "cannot summarise the audit log"
import json, sys, yaml
log, user, role = sys.argv[1:4]
used = {}
for line in open(log, encoding="utf-8"):
    event = json.loads(line)
    ref = event.get("objectRef")
    if event["user"]["username"] != user or not ref:
        continue
    resource = ref["resource"] + ("/" + ref["subresource"] if ref.get("subresource") else "")
    used.setdefault((ref.get("apiGroup", ""), resource), set()).add(event["verb"])
granted = {}
for rule in yaml.safe_load(open(role, encoding="utf-8"))["rules"]:
    for group in rule["apiGroups"]:
        for resource in rule["resources"]:
            granted.setdefault((group, resource), set()).update(rule["verbs"])
def name(key):
    return key[1] + (f" ({key[0]})" if key[0] else "")
print("      the operator's requests, by resource and verb, from the audit log:")
for key in sorted(used):
    print(f"        {name(key)}: {' '.join(sorted(used[key]))}")
print("      rights of the Role that no request of this lifecycle used:")
for key in sorted(granted):
    if granted[key] - used.get(key, set()):
        print(f"        {name(key)}: {' '.join(sorted(granted[key] - used.get(key, set())))}")
print("      (kubectl's WebSocket connections are a get on pods/exec, pods/portforward or pods/attach, for which the")
print("      API server also checks create, within the same request; the audit log shows the get alone)")
PY
kube "deleting the operator's RoleBinding" "$NAMESPACE" delete rolebinding "$OPERATOR"
kube "deleting the operator's Role" "$NAMESPACE" delete role "$OPERATOR"
kube "deleting the operator's ServiceAccount" "$NAMESPACE" delete serviceaccount "$OPERATOR"
[ -z "$(remaining)" ] || fail "the namespace still holds $(remaining | tr '\n' ' ')"
as_operator
wait_until 60 token_refused || fail "the operator's token still authenticates without its ServiceAccount: $(cat "$WORK/whoami.txt")"
ok "the operator's RoleBinding, Role and ServiceAccount are deleted; its token no longer authenticates"
