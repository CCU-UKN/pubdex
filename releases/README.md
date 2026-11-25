# The release record

One directory per published PubDex release, `releases/<version>/`, and the
file `FLOOR`. The record is committed with the repository, so a clone
carries what identifies every published PubDex release: the commit it was
built from and the digests of its images.

## `<version>/release.json`

| Field | What it holds |
|---|---|
| `version` | the release version, the directory's name, as `VERSION` held it at the release |
| `commit` | the full id of the commit the release was built from |
| `tag` | the tag that names that commit, `v<version>` |
| `published` | the date the release was published, `YYYY-MM-DD` |
| `platform` | the one platform its images are built for, `linux/amd64` |
| `images` | `application` and `postgres`: each image's reference by registry, repository, version tag and registry manifest digest, `<registry>/<repository>:<version>@sha256:<digest>` |
| `schema_changes` | the schema changes the release ships (`DB/schema/`), each with its `number`, its `name` (the file name without `.sql`) and the SHA-256 of its file; empty for a release without schema changes |
| `transform_version` | the transform version of its ingestion, `people_pubs.__version__` |
| `floor` | the oldest PubDex release from which an upgrade to this one is supported, or `null` for the first release |
| `chart` | the Helm chart of the release: its `version`, the `commit` whose `charts/pubdex/` it is, and the file name of its chart `package`, or `null` where no package was published |

An image tag can be moved in its registry; the digest cannot. Install a
published release by the digests recorded here.

**0.1.0** predates the release automation: its entry holds `release.json`
only, without a checksum file, SBOM or provenance record. No chart package
of 0.1.0 was published; its chart is `charts/pubdex/` at the commit that
`chart.commit` names, which the Helm chart's tests on a disposable cluster
install from the repository's history as the release an upgrade starts
from.

## `<version>/SHA256SUMS`, `<version>/DIGESTS` and the provenance record

A PubDex release built with [`release.sh`](../release.sh) also has, next to
`release.json`, three files its build and publish steps wrote, unchanged
([`RELEASING.md`](../RELEASING.md), *The record commit*):

- `SHA256SUMS`: the SHA-256 of every file of the artifact set the build
  wrote — the source archive `pubdex-<version>-source.tar.gz`, the Helm
  chart's package `pubdex-<version>.tgz`, `IMAGES`, the SBOM of each image
  (`pubdex-<version>-image.spdx.json`,
  `pubdex-<version>-postgres-image.spdx.json`) and the provenance record
  `pubdex-<version>-provenance.json` — one line `<hash>  <name>` each,
  sorted by name. `sha256sum -c --ignore-missing SHA256SUMS` checks copies
  of those files wherever they were downloaded from; `IMAGES` is listed
  but not published.
- `DIGESTS`: both images, the application image first, each as
  `<registry>/<namespace>/<repository>:<version>@sha256:<digest>`, the
  digest its registry reported when it was pushed; `images` in
  `release.json` names the same references.
- `pubdex-<version>-provenance.json`: the provenance record, what the
  release was built from and with, and both image ids
  ([`DB/release_provenance.py`](../DB/release_provenance.py) describes
  every field).

The source archive, the chart package and the SBOMs are on the forge's
release page of the tag `v<version>`, a copy; the committed `SHA256SUMS`
checks them, with `sha256sum --check --ignore-missing SHA256SUMS` in the
directory of the download.

## `FLOOR`

One line: the oldest PubDex release from which an upgrade to this checkout
is supported. The suite tests the upgrade from the floor and from the
preceding release, the newest one recorded below `VERSION`. A PubDex
release that raises the floor changes this file.

## Reading the record

[`DB/release_record.py`](../DB/release_record.py) reads the record and
checks every entry it reads; it uses no network and no Git:

```sh
python3 DB/release_record.py entry 0.1.0
python3 DB/release_record.py field 0.1.0 images.application chart.commit
python3 DB/release_record.py preceding
python3 DB/release_record.py floor
```
