# PubDex Helm chart

This chart installs the PubDex read service and its ingestion commands from
the release images, in one of two modes:

- `evaluation`: the chart also runs PostgreSQL 16, as a single-replica
  StatefulSet with a volume of its own, for trying PubDex out with
  synthetic or disposable data. Data you cannot lose belongs in external
  mode.
- `external`: PubDex uses a database on a PostgreSQL 16 server that you run,
  which the chart's setup Job prepares.

In both modes a Job runs the schema upgrade of the application image,
`python -m people_pubs.schema upgrade`, at the install and at each upgrade
(in external mode, each one that runs the setup).

The default image tags equal the chart's version; both digests default to
empty. Set `image.digest` and `postgresImage.digest` from the selected
PubDex release record before installing. A tag can be moved; a digest
identifies the exact image. An unpublished checkout needs its own built images.

Read `KUBERNETES.md`, the operator guide, before you install: what you
need, the supported platforms, install, upgrade, rollback and uninstall,
Secrets, TLS and settings, scheduling, ingestion, ingress, NetworkPolicy,
backup and troubleshooting. Its appendix says which of its commands the
chart's acceptance test runs.

`KUBERNETES.md` and the other files the chart names but does not hold —
`QUICKSTART.md`, `DB/SCRIPTS.md` and `create_secrets.sh` — are in the
PubDex source of the chart's version: the source archive
`pubdex-<version>-source.tar.gz` of that PubDex release, or the repository
that `home` in `Chart.yaml` names, at the tag `v<version>`. The application
image carries `create_secrets.sh` as well, as `/opt/pubdex/create_secrets.sh`.

| File | What it is |
|---|---|
| [`values.yaml`](values.yaml) | every value, with its default and a comment that explains it |
| [`values.schema.json`](values.schema.json) | refuses unknown keys, values of the wrong form and contradictory settings |
| [`examples/`](examples/) | values for external mode and for an Ingress with TLS |
| [`files/external-setup.sh`](files/external-setup.sh) | the setup of external mode, run by the setup Job or by hand |
| [`templates/`](templates/) | the manifests, with `NOTES.txt` and the `helm test` pod |
| [`LICENSE`](LICENSE) | the MIT licence of the chart's files, the PubDex source's own |

The chart takes no password and generates none: you create the Secrets and
name them in the values. `helm get notes <release>` prints the commands for
a release.
