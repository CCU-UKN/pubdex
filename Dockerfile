# The PubDex application image: the read service and the ingestion commands
# of the people_pubs package, in one image, run by an unprivileged user, with
# the database's schema bootstrap (DB/init/) and the Compose files of the
# deployment, so that this image and compose.yaml are the whole deployment.
#
# Build it with ./build_image.sh, which also records the release version and
# the source revision in the labels below (DEPLOYMENT.md, "The application
# image"). Of BuildKit's additions to the Dockerfile syntax it uses
# RUN --mount=type=bind, from the build context and from an earlier stage,
# and RUN --network=none, all part of the Dockerfile syntax that Docker
# Engine's BuildKit supports. Which Engines the deployment supports, to build
# as to run, DB/lib/docker_versions.sh decides. Only linux/amd64 images are
# supported (DEPLOYMENT.md, "Supported platforms").
#
# The command chooses what a container does:
#   python -m people_pubs.api                     the read service (the default)
#   python -m people_pubs.sync.<command> [...]    an ingestion command
#   python -m people_pubs.api.probe [--live]      the read service's health check
#   python -m people_pubs.bootstrap <directory>   copies the schema bootstrap there
#
# The image holds no credential and reads no .env file. The read service takes
# its database password from the file PEOPLE_PUBS_API_DB_PASSWORD_FILE names,
# the ingestion commands theirs from PEOPLE_DB_PASSWORD_FILE.
#
# Inputs: the base image, fixed by digest, less its setuptools and pip; the
# files the hashed locks name -- pip (DB/requirements-pip.txt), the build
# tools of psycopg-c (DB/requirements-build.txt) and the runtime lock
# (DB/requirements.txt) -- each checked against its SHA-256; Debian's packages
# for compiling psycopg-c against libpq and, at run time, Debian's libpq, from
# the base image's signed Debian archive; and exactly the files .dockerignore
# lets into the build context, each copied below, but the scripts in DB/ that
# fetch and install the locks, which the build runs without copying them.
#
# Three stages keep downloading, compiling and installing apart:
#   wheels     DB/fetch_verified.py downloads exactly one file per pin of
#              those locks -- a wheel, but psycopg-c's source archive -- with
#              Python's own HTTPS client, never pip's, and keeps a file only
#              when its SHA-256 is one the lock lists (DB/fetch_verified.py);
#   psycopg-c  a compiler and Debian's libpq headers, then, without network,
#              DB/install_locked.py builds psycopg's C implementation from
#              that verified archive with the hashed build tools, and records
#              the wheel's SHA-256 next to the wheel;
#   the image  Debian's libpq5, then, without network, pip and the runtime
#              lock from those files alone: no compiler, no header and no
#              build tool reaches it.
# No pip runs with network access: the steps that install run under
# RUN --network=none, which leaves them no interface but loopback, and the
# scripts refuse to install if they find any other (--network-isolation
# required).

ARG PYTHON_BASE=python:3.11.18-slim-trixie@sha256:82209229d3fda30d71e603ec95bb4bea0476635ba6df3df2e32284e036654f51

FROM ${PYTHON_BASE} AS wheels
RUN --mount=type=bind,source=DB,target=/opt/pubdex-build \
    python -I /opt/pubdex-build/fetch_verified.py wheels --dest /wheelhouse --allow-source psycopg-c \
      /opt/pubdex-build/requirements-pip.txt /opt/pubdex-build/requirements-build.txt \
      /opt/pubdex-build/requirements.txt

FROM ${PYTHON_BASE} AS psycopg-c
RUN apt-get update \
 && apt-get install --yes --no-install-recommends gcc libc6-dev libpq-dev \
 && rm -rf /var/lib/apt/lists/* \
 && dpkg-query -W gcc libc6-dev libpq-dev libpq5
RUN --network=none \
    --mount=type=bind,source=DB,target=/opt/pubdex-build \
    --mount=type=bind,from=wheels,source=/wheelhouse,target=/wheelhouse \
    python -I /opt/pubdex-build/install_locked.py --wheelhouse /wheelhouse --network-isolation required \
      --build-only /psycopg-c /opt/pubdex-build/requirements.txt

FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Debian's libpq, which psycopg's C implementation calls, with the libraries
# it links -- Debian's OpenSSL, Kerberos, LDAP and SASL among them -- all
# packages Debian maintains and the scanner sees.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && dpkg-query -W libpq5

# pip, then the runtime lock only: no test tool reaches the image. All of it
# without network, from the files the wheels stage verified and the wheel the
# psycopg-c stage built: DB/install_pip.py installs the locked pip from its
# wheel by hash, so the base image's pip 24.0 never runs, and
# DB/install_locked.py installs every package of the lock in hash-checking
# mode, without dependencies of their own and from binary wheels only --
# psycopg-c by the SHA-256 recorded when it was built from the verified
# archive -- and checks that psycopg uses its C implementation. The base
# image's setuptools is removed before the lock: nothing imports it at run
# time, while the copies of other packages it bundles would bring their
# advisories into the image. pip check then confirms that the installed
# packages' requirements are met, and pip is uninstalled in the same step, as
# are the wheels of pip and setuptools that the standard library's ensurepip
# installs from: no command of the image runs pip, so a container's file
# system holds neither pip, nor the packages it vendors, nor those wheels.
# That keeps pip and its advisories out of the image; it does not keep
# software from being installed or run in a container, which still has
# Python, a shell, dpkg and apt. The base image's own layer still holds its
# pip and those wheels, which the later layer hides from every container.
COPY DB/requirements-pip.txt DB/requirements.txt /opt/pubdex/
RUN --network=none \
    --mount=type=bind,source=DB,target=/opt/pubdex-build \
    --mount=type=bind,from=wheels,source=/wheelhouse,target=/wheelhouse \
    --mount=type=bind,from=psycopg-c,source=/psycopg-c,target=/psycopg-c \
    python /opt/pubdex-build/install_pip.py --wheelhouse /wheelhouse --lock /opt/pubdex/requirements-pip.txt \
      --network-isolation required \
 && python -m pip uninstall --yes setuptools \
 && python /opt/pubdex-build/install_locked.py --wheelhouse /wheelhouse --built /psycopg-c \
      --network-isolation required /opt/pubdex/requirements.txt \
 && python -m pip check \
 && python -m pip uninstall --yes pip \
 && rm /usr/local/lib/python3.11/ensurepip/_bundled/pip-*.whl /usr/local/lib/python3.11/ensurepip/_bundled/setuptools-*.whl

# The user every container runs as: no home directory, no login shell, and
# write access to nothing in the image but one empty directory, the mount
# point of the schema bootstrap's volume in compose.yaml: Docker gives a new
# volume the owner of the directory it is first mounted on, so the bootstrap
# service can fill that volume as this user.
RUN groupadd --gid 10001 pubdex \
 && useradd --uid 10001 --gid pubdex --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin pubdex \
 && install -d -o 10001 -g 10001 -m 755 /var/lib/pubdex/initdb

# What COPY adds belongs to root, with the file modes of the checkout; the
# step after the last COPY sets them.
COPY LICENSE LICENSE-CC-BY-4.0 /opt/pubdex/
COPY DB/people_pubs /opt/pubdex/people_pubs
# The demonstration's synthetic roster and payloads, where
# people_pubs.sync.fixture_demo looks for them: tests/fixtures/golden/ next to
# the package.
COPY DB/tests/fixtures/golden/README.md \
     DB/tests/fixtures/golden/demo_roster_minimal.csv \
     DB/tests/fixtures/golden/orcid_work_minimal.json \
     DB/tests/fixtures/golden/crossref_work_minimal.json \
     /opt/pubdex/tests/fixtures/golden/
# The schema bootstrap, file by file, where people_pubs.bootstrap looks for it:
# init/ next to the package. The bootstrap service of compose.yaml copies it
# into the volume from which PostgreSQL initialises a new database, so the
# schema is versioned with this image.
COPY DB/init/000_init.sql \
     DB/init/010_ingestion_runtime_schema.sql \
     DB/init/011_member_info_manual.sql \
     DB/init/020_role_passwords.sh \
     /opt/pubdex/init/
# The Compose files this image is released with, the script that creates
# their secret files and the one that gives the database the files'
# passwords, so that a deployment can be set up and run from the image alone
# (DEPLOYMENT.md, "Selecting a published image"). compose.yaml names the
# release's image by its version tag, and a release is verified with this
# copy (DEPLOYMENT.md, "What identifies a deployment").
COPY compose.yaml compose.postgres-port.yaml create_secrets.sh set_passwords.sh /opt/pubdex/

# Every directory under /opt/pubdex 755 and every file 644, whatever modes the
# checkout gave them -- 664 under a umask of 002, every file executable in a
# checkout on Windows -- so that the unprivileged user can read and enter
# everything there and write nothing. The bootstrap service sets the modes of
# the schema files it copies itself. Only what differs is changed: with a
# checkout in those modes the layer is empty, otherwise it holds a second copy
# of the files, about 1.3 MB.
RUN find /opt/pubdex -type d ! -perm 755 -exec chmod 755 {} + \
 && find /opt/pubdex -type f ! -perm 644 -exec chmod 644 {} +

# The package is found through PYTHONPATH from any working directory, and
# PYTHONSAFEPATH keeps the working directory itself out of the import path,
# so no file there can stand in for a module. Configuration comes from the
# environment and secret files only, never from a .env file. The read
# service listens on all of the container's addresses; publishing its port is
# the deployment's decision (compose.yaml publishes it on loopback).
ENV PYTHONPATH=/opt/pubdex \
    PYTHONSAFEPATH=1 \
    PSYCOPG_IMPL=c \
    PEOPLE_PUBS_SKIP_DOTENV=1 \
    PEOPLE_PUBS_API_HOST=0.0.0.0
WORKDIR /opt/pubdex
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "people_pubs.api"]

# Set by ./build_image.sh. A plain `docker build` leaves them "unknown"
# rather than guessing. The release version is the repository's VERSION
# file; the transform version is people_pubs.__version__, the stamp ingestion
# writes into source provenance; they are independent.
ARG PEOPLE_PUBS_VERSION=unknown
ARG PEOPLE_PUBS_REVISION=unknown
ARG PEOPLE_PUBS_CREATED=unknown
ARG PEOPLE_PUBS_TRANSFORM_VERSION=unknown
ARG PEOPLE_PUBS_SOURCE=
LABEL org.opencontainers.image.title="PubDex" \
      org.opencontainers.image.description="Ingestion commands, read service, database schema bootstrap and Compose files of PubDex, an auditable record of what the people of a research organisation have published" \
      org.opencontainers.image.licenses="MIT AND CC-BY-4.0" \
      org.opencontainers.image.version="${PEOPLE_PUBS_VERSION}" \
      org.opencontainers.image.revision="${PEOPLE_PUBS_REVISION}" \
      org.opencontainers.image.created="${PEOPLE_PUBS_CREATED}" \
      org.opencontainers.image.source="${PEOPLE_PUBS_SOURCE}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.11.18-slim-trixie" \
      org.opencontainers.image.base.digest="sha256:82209229d3fda30d71e603ec95bb4bea0476635ba6df3df2e32284e036654f51" \
      pubdex.transform-version="${PEOPLE_PUBS_TRANSFORM_VERSION}"
