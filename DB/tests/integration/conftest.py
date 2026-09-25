"""Safety net for every disposable-DB integration test in this directory.

When PEOPLE_PUBS_INTEGRATION_DSN (or PEOPLE_DB_TEST_DSN) is set, force the
whole people_pubs configuration onto that disposable DSN BEFORE any test
module imports the package. Without this, a code path that accidentally falls
back to the default connection resolution (env -> DB/.env) would write to the
maintainer's real database — which is exactly what happened once, when a
refactor moved a connection factory and a test kept patching the old
attribute path. Tests must still pass explicit DSNs; this guard only redirects the
default so the failure mode of a missed patch is "wrong table contents in the
disposable DB", never "writes to a real database".
"""
import os

_dsn = os.getenv("PEOPLE_PUBS_INTEGRATION_DSN") or os.getenv("PEOPLE_DB_TEST_DSN")
if _dsn:
    os.environ["PEOPLE_DB_DSN"] = _dsn
