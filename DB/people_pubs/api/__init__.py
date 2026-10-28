"""
people_pubs.api

The read service: a small HTTP service over the canonical publication list,
run with `python -m people_pubs.api`.

It serves three things, all read-only:

  GET /livez             the process is up (no database access)
  GET /readyz            the database and the read surface the service needs
                         are usable
  GET /v0/publications   a bounded page of the canonical list, read from the
                         serving snapshot biblio.publications_canon_web

Everything under /v0/ is preliminary: the format may change without notice,
and no compatibility is promised for it. It is the foundation the versioned
read API will be built on, not that API. `python -m people_pubs.api.probe`
asks a running service for /readyz or /livez: the health check of its
container.

The service connects as a read-only database role, normally app_readonly,
with its own settings (people_pubs.api.settings). It never reads the
ingestion configuration -- DB/.env, PEOPLE_DB_DSN or libpq's PG* variables --
and never imports the ingestion or provider-client modules, so running it
cannot trigger a database write or a metadata-provider request.
"""
