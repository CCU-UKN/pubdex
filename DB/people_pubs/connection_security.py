"""
people_pubs.connection_security

The authentication every database connection of PubDex insists on unless its
configuration chooses otherwise: the ingestion commands' connections
(people_pubs.db.connection) and the read service's (people_pubs.api.database).

The deployment uses SCRAM authentication with passwords and disables GSSAPI
encryption. With require_auth set, libpq checks each request of the server
against it and ends the connection before acting on a method it does not
allow; gssencmode=disable keeps it from negotiating GSSAPI encryption. So
every connection gets

  require_auth=scram-sha-256   when it authenticates with a password: the
                               server has to use SCRAM, which also makes it
                               prove that it knows the role's password
                               verifier; a request for a cleartext or MD5
                               password is refused before the password is sent
  require_auth=none            when it has no password (the read service
                               without a password file): the server may not
                               ask for any authentication
  gssencmode=disable

A value the connection string sets for either option is kept, and so, for the
ingestion commands, is one that libpq's environment variables PGREQUIREAUTH
and PGGSSENCMODE set: a server that authenticates otherwise remains usable,
as a deliberate choice of the configuration. An empty value counts as unset.
Both options need libpq 16 or later: the image's psycopg-c uses Debian's libpq
17, and DB/install_locked.py refuses a setup whose psycopg loads an older one.
"""
from __future__ import annotations

from typing import Dict, Mapping

#: What a connection that authenticates with a password requires.
SCRAM = "scram-sha-256"
#: What a connection without a password requires: no authentication at all.
NO_AUTHENTICATION = "none"

# Each option this module sets, and the environment variable libpq reads it from.
_ENVIRONMENT = {"require_auth": "PGREQUIREAUTH", "gssencmode": "PGGSSENCMODE"}


def defaults(params: Mapping[str, object], environ: Mapping[str, str], require_auth: str = SCRAM) -> Dict[str, str]:
    """The options to add to a connection whose connection string sets params,
    with libpq reading environ: require_auth and gssencmode=disable, each one
    unless params or environ sets it already."""
    wanted = {"require_auth": require_auth, "gssencmode": "disable"}
    return {
        option: value
        for option, value in wanted.items()
        if not str(params.get(option) or "").strip() and not environ.get(_ENVIRONMENT[option], "").strip()
    }
