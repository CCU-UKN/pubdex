# The PostgreSQL image of the PubDex deployment: PostgreSQL 16, built from
# its source without SQL/XML, so that no XML library is in the image. The
# deployment uses no SQL/XML. PostgreSQL documents XML as an optional
# build feature: built --without-libxml, every XML function fails with
# "unsupported XML feature" before any parser is called.
#
# Build it with ./build_image.sh, which records the release version and the
# source revision in the labels below, as for the application image
# (DEPLOYMENT.md, "The application image"); a plain `docker build -f
# postgres.Dockerfile .` leaves them "unknown". Only linux/amd64 is supported.
#
# What it keeps of the official postgres:16 image, so that compose.yaml, its
# health check, DB/init/ and the documented operations work unchanged: the
# entrypoint scripts of postgres:16.16-trixie, copied from that image by
# digest and checked by SHA-256 below -- POSTGRES_PASSWORD_FILE, POSTGRES_DB,
# the scripts of /docker-entrypoint-initdb.d, a pg_hba.conf that
# authenticates remote clients with scram-sha-256, initialisation on an empty
# volume only -- the postgres user and group 999, PGDATA in
# /var/lib/postgresql/data as the volume, the socket in /var/run/postgresql,
# listen_addresses '*', LANG en_US.utf8, SIGINT to stop, port 5432, and the
# programs on PATH: postgres, initdb, pg_ctl, psql, pg_isready, pg_dump,
# pg_restore and the rest of PostgreSQL's own. The server and its data
# directory are compatible with the official image's of the same major
# release on the same Debian release: the same block and segment sizes,
# glibc's collations of Debian 13, and ICU, LZ4 and Zstandard built in.
#
# What it leaves out, and so cannot do: SQL/XML (libxml2) and XSLT (libxslt,
# the contrib module xml2); JIT compilation (LLVM); GSSAPI, LDAP, PAM and
# RADIUS authentication (Kerberos, OpenLDAP and SASL), which compose.yaml's
# deployment never configures; PL/Perl, PL/Python and PL/Tcl; SELinux
# labels, systemd notification, DTrace probes and message translations; and
# from the official image's packages libnss-wrapper (only for a container
# started as a user /etc/passwd does not name), xz-utils and zstd (only for
# init scripts compressed that way; DB/init/ has none), Perl modules and
# postgresql-common's wrappers. Built in: OpenSSL (pgcrypto, and TLS for an
# operator who enables it), ICU, LZ4, Zstandard, zlib, readline and libuuid.
# The contrib modules PubDex's schema creates -- citext, pgcrypto, unaccent
# and pg_trgm -- are checked below, with the server's own plpgsql.
#
# Inputs, each fixed:
#   - PostgreSQL's source, ARG PG_VERSION and PG_SHA256: downloaded by
#     DB/fetch_verified.py, which keeps the file only when its SHA-256 is
#     PG_SHA256 -- the value of postgresql.org's .sha256 file for it, which
#     is also the hash of the orig tarball of PGDG's postgresql-16 package in
#     PGDG's Sources index, signed in its InRelease file by the PostgreSQL
#     Debian repository's key;
#   - Debian 13, ARG DEBIAN_BASE, by digest; the compiler, the headers and the
#     runtime libraries from the base image's signed Debian archive, as listed
#     below and no more (apt-get --no-install-recommends);
#   - the entrypoint scripts, ARG POSTGRES_ENTRYPOINT_IMAGE, by digest and by
#     their own SHA-256, and the MIT licence of the docker-library/postgres
#     project they come from, kept in this repository as
#     third_party/docker-library-postgres/LICENSE: that project's LICENSE
#     file at its commit dcf8ac73e67253ef0c7174418c61850ab4a11b58, byte for
#     byte, checked by ENTRYPOINT_LICENSE_SHA256, so that the build needs no
#     download for it;
#   - the Python image of the application, ARG PYTHON_BASE, by digest, which
#     runs the download and enters nothing else into this image.
# The compilation runs without network (RUN --network=none), with Debian's
# hardening flags (dpkg-buildflags, hardening=+all) and without debug symbols.
#
# Future updates, if provided, need a rebuild and acceptance tests for the
# new version: a PostgreSQL minor release -- PG_VERSION, and PG_SHA256 from postgresql.org's
# .sha256 file, compared with PGDG's signed Sources index -- usually with
# postgres:<version>-trixie's digest in POSTGRES_ENTRYPOINT_IMAGE and the
# scripts' hashes; a Debian update -- DEBIAN_BASE's digest, as the newest
# debian:trixie-slim. The runtime libraries come from the archive at build
# time, so every build takes Debian 13's newest security updates of them;
# the build log and the image's package database record which ones.

ARG PYTHON_BASE=python:3.11.18-slim-trixie@sha256:82209229d3fda30d71e603ec95bb4bea0476635ba6df3df2e32284e036654f51
ARG DEBIAN_BASE=debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
ARG POSTGRES_ENTRYPOINT_IMAGE=postgres:16.16-trixie@sha256:07abc3f43c9395b0bdbbab36bc25ddb0e8d83cd4fcafb5fb7d6abe14ff156dd8
ARG PG_MAJOR=16
ARG PG_VERSION=16.16
ARG PG_SHA256=9d5cd8633043b75740d5d59749c6b7365e6aeb53c47098b1a5e80bb18f87881a
ARG ENTRYPOINT_LICENSE_SHA256=87ffd2c45e3f90cfa3407b5c40ef8333e87c3e875e4895f8b64df758198deafc

# The source archive, by hash, with Python's own HTTPS client, and the
# entrypoint scripts' licence from the build context, by hash.
FROM ${PYTHON_BASE} AS source
ARG PG_VERSION
ARG PG_SHA256
ARG ENTRYPOINT_LICENSE_SHA256
COPY third_party/docker-library-postgres/LICENSE /src/docker-library-postgres-LICENSE
RUN --mount=type=bind,source=DB,target=/opt/pubdex-build \
    printf '%s  %s\n' "${ENTRYPOINT_LICENSE_SHA256}" /src/docker-library-postgres-LICENSE \
    | sha256sum --check --strict \
 && chmod 0644 /src/docker-library-postgres-LICENSE \
 && python -I /opt/pubdex-build/fetch_verified.py url --sha256 "${PG_SHA256}" \
      "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.bz2" \
      "/src/postgresql-${PG_VERSION}.tar.bz2"

# The entrypoint scripts of the official image.
FROM ${POSTGRES_ENTRYPOINT_IMAGE} AS entrypoint

FROM ${DEBIAN_BASE} AS build
ARG PG_MAJOR
ARG PG_VERSION
RUN apt-get update \
 && apt-get install --yes --no-install-recommends \
      bzip2 dpkg-dev gcc libc6-dev libicu-dev liblz4-dev libreadline-dev libssl-dev libzstd-dev make \
      pkg-config uuid-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/* \
 && dpkg-query -W -f '${Package} ${Version}\n' | sort
COPY --from=source /src/postgresql-${PG_VERSION}.tar.bz2 /usr/src/
# The socket directory of Debian's packages, /var/run/postgresql, instead of
# PostgreSQL's /tmp, as the official image has it (the change its own source
# builds make); the check fails the build if the line is not found.
RUN --network=none \
    mkdir /usr/src/postgresql \
 && tar --extract --file "/usr/src/postgresql-${PG_VERSION}.tar.bz2" --directory /usr/src/postgresql \
      --strip-components 1 \
 && cd /usr/src/postgresql \
 && awk '$1 == "#define" && $2 == "DEFAULT_PGSOCKET_DIR" && $3 == "\"/tmp\"" { $3 = "\"/var/run/postgresql\""; print; next } { print }' \
      src/include/pg_config_manual.h > src/include/pg_config_manual.h.new \
 && grep '/var/run/postgresql' src/include/pg_config_manual.h.new \
 && mv src/include/pg_config_manual.h.new src/include/pg_config_manual.h \
 && export DEB_BUILD_MAINT_OPTIONS=hardening=+all DEB_CFLAGS_MAINT_STRIP=-g DEB_LDFLAGS_MAINT_STRIP=-g \
 && ./configure \
      --enable-option-checking=fatal \
      --build="$(dpkg-architecture --query DEB_BUILD_GNU_TYPE)" \
      --prefix="/usr/lib/postgresql/${PG_MAJOR}" \
      --with-pgport=5432 \
      --with-system-tzdata=/usr/share/zoneinfo \
      --with-openssl \
      --with-icu \
      --with-lz4 \
      --with-zstd \
      --with-uuid=e2fs \
      --without-libxml \
      --without-libxslt \
      --without-llvm \
      --without-gssapi \
      --without-ldap \
      --without-pam \
      --without-perl \
      --without-python \
      --without-tcl \
      --without-selinux \
      --without-systemd \
      --disable-nls \
      CFLAGS="$(dpkg-buildflags --get CFLAGS)" \
      CPPFLAGS="$(dpkg-buildflags --get CPPFLAGS)" \
      LDFLAGS="$(dpkg-buildflags --get LDFLAGS)" \
 && make -j "$(nproc)" \
 && make -C contrib -j "$(nproc)" \
 && make install-strip DESTDIR=/staging \
 && make -C contrib install-strip DESTDIR=/staging \
 && cd "/staging/usr/lib/postgresql/${PG_MAJOR}" \
 && for extension in citext pgcrypto unaccent pg_trgm; do test -f "share/extension/${extension}.control"; test -f "lib/${extension}.so"; done \
 && test -f lib/plpgsql.so \
 && test ! -e share/extension/xml2.control \
 && test ! -e lib/pgxml.so

# The regression tests of the build, on request only:
#   docker build -f postgres.Dockerfile --target regress .
# PostgreSQL refuses to run them as root.
FROM build AS regress
RUN useradd --create-home regress && chown -R regress /usr/src/postgresql
USER regress
RUN --network=none make -C /usr/src/postgresql check

# The packages of the build stage -- the compiler, the headers and the tools
# that built PostgreSQL -- one "<package> <version>" a line, sorted. The image
# carries the list in /usr/share/doc/pubdex-postgres/build-packages.txt, from
# which release.sh's provenance record takes it: the build log shows the list
# only when the build stage's step runs, not when it comes from the build
# cache, and the image's own package database names only what the image runs
# with.
FROM build AS build-packages
RUN dpkg-query -W -f '${Package} ${Version}\n' | LC_ALL=C sort > /build-packages.txt

FROM ${DEBIAN_BASE}
ARG PG_MAJOR
ARG PG_VERSION

# The user and group of the official image, with the same ids, so that a
# data volume keeps its owner.
RUN groupadd --system --gid 999 postgres \
 && useradd --system --gid postgres --uid 999 --home-dir /var/lib/postgresql --shell /bin/bash postgres \
 && install --verbose --directory --owner postgres --group postgres --mode 1777 /var/lib/postgresql

# The runtime libraries of the build -- ICU and readline; OpenSSL, LZ4,
# Zstandard, zlib and libuuid are in the base already -- the locale the
# official image initialises databases with, its time zones, and gosu, with
# which the entrypoint drops root. The slim base keeps /usr/share/locale out
# of the image; that exclusion goes first, as in the official image, since
# the locale needs it.
RUN grep -q '/usr/share/locale' /etc/dpkg/dpkg.cfg.d/docker \
 && sed -ri '/\/usr\/share\/locale/d' /etc/dpkg/dpkg.cfg.d/docker \
 && ! grep -q '/usr/share/locale' /etc/dpkg/dpkg.cfg.d/docker \
 && apt-get update \
 && apt-get install --yes --no-install-recommends \
      gosu libicu76 liblz4-1 libreadline8t64 libssl3t64 libuuid1 libzstd1 locales tzdata zlib1g \
 && rm -rf /var/lib/apt/lists/* \
 && echo 'en_US.UTF-8 UTF-8' >> /etc/locale.gen \
 && locale-gen \
 && locale -a | grep 'en_US.utf8' \
 && dpkg-query -W -f '${Package} ${Version}\n' | sort
ENV LANG=en_US.utf8

COPY --from=build /staging/usr/lib/postgresql /usr/lib/postgresql
COPY --from=entrypoint /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-ensure-initdb.sh /usr/local/bin/
# The notices the licences of PostgreSQL and of the entrypoint scripts ask
# every copy to carry; Debian's packages carry theirs in /usr/share/doc.
COPY --from=build /usr/src/postgresql/COPYRIGHT /usr/share/doc/pubdex-postgres/PostgreSQL-COPYRIGHT
COPY --from=source /src/docker-library-postgres-LICENSE /usr/share/doc/pubdex-postgres/docker-entrypoint-LICENSE
# The packages PostgreSQL was compiled with (stage build-packages).
COPY --from=build-packages /build-packages.txt /usr/share/doc/pubdex-postgres/build-packages.txt
ENV PG_MAJOR=${PG_MAJOR} \
    PG_VERSION=${PG_VERSION} \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/${PG_MAJOR}/bin \
    PGDATA=/var/lib/postgresql/data

# The entrypoint scripts as the digest above has them; listen_addresses '*'
# in the template initdb copies, as in the official image; the socket and
# data directories with the official image's modes; and the checks that no
# XML library is installed or linked, that no program or library lacks a
# library it needs, and that the build did not take XML support in.
RUN cd /usr/local/bin \
 && printf '%s  %s\n' \
      48647812e68165e2943abe82a1a7cb8364d6e9f8b6b6aefc179868b66a327c46 docker-entrypoint.sh \
      922ade6b23a312e65023349b36e873752b15ff38ae4c0ae461d74451e104f312 docker-ensure-initdb.sh \
    | sha256sum --check --strict \
 && ln -s docker-ensure-initdb.sh docker-enforce-initdb.sh \
 && mkdir /docker-entrypoint-initdb.d \
 && sample="/usr/lib/postgresql/${PG_MAJOR}/share/postgresql.conf.sample" \
 && sed -ri "s!^#?(listen_addresses)\s*=\s*\S+.*!\1 = '*'!" "$sample" \
 && grep -F "listen_addresses = '*'" "$sample" \
 && install --verbose --directory --owner postgres --group postgres --mode 3777 /var/run/postgresql \
 && install --verbose --directory --owner postgres --group postgres --mode 1777 "$PGDATA" \
 && if dpkg-query -W -f '${Package}\n' | grep -E '^lib(xml2|xslt1)'; then \
      echo "an XML library is installed" >&2; exit 1; fi \
 && linked="$(find /usr/lib/postgresql -type f -exec ldd {} + 2>/dev/null || true)" \
 && if printf '%s\n' "$linked" | grep -E 'libxml|libxslt|not found'; then \
      echo "a file under /usr/lib/postgresql links an XML library or misses a library" >&2; exit 1; fi \
 && configured="$(pg_config --configure)" \
 && case "$configured" in *--with-libxml*|*--with-libxslt*) echo "built with XML" >&2; exit 1 ;; esac \
 && postgres --version

VOLUME /var/lib/postgresql/data
ENTRYPOINT ["docker-entrypoint.sh"]
STOPSIGNAL SIGINT
EXPOSE 5432
CMD ["postgres"]

# Set by ./build_image.sh, as in Dockerfile. A plain `docker build` leaves
# them "unknown" rather than guessing.
ARG PEOPLE_PUBS_VERSION=unknown
ARG PEOPLE_PUBS_REVISION=unknown
ARG PEOPLE_PUBS_CREATED=unknown
ARG PEOPLE_PUBS_SOURCE=
LABEL org.opencontainers.image.title="PubDex PostgreSQL" \
      org.opencontainers.image.description="PostgreSQL ${PG_VERSION} for the PubDex deployment, built from source without SQL/XML" \
      org.opencontainers.image.licenses="PostgreSQL AND MIT" \
      org.opencontainers.image.version="${PEOPLE_PUBS_VERSION}" \
      org.opencontainers.image.revision="${PEOPLE_PUBS_REVISION}" \
      org.opencontainers.image.created="${PEOPLE_PUBS_CREATED}" \
      org.opencontainers.image.source="${PEOPLE_PUBS_SOURCE}" \
      org.opencontainers.image.base.name="docker.io/library/debian:trixie-slim" \
      org.opencontainers.image.base.digest="sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4" \
      pubdex.postgresql-version="${PG_VERSION}"
