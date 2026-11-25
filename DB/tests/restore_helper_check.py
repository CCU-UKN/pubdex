"""Exercise the development restore helper on the runner's disposable database.

Called only by run_disposable_integration.sh, with its container, private
temporary directory and isolated connection environment. No backup discovery,
globals import, curation import or local configuration is used.
"""
from pathlib import Path
import os
import subprocess
import sys

import psycopg

from people_pubs import schema


def main() -> None:
    container, directory = sys.argv[1:]
    db = Path(__file__).resolve().parents[1]
    dump = Path(directory) / "synthetic.dump"
    dsn = os.environ["PEOPLE_PUBS_INTEGRATION_DSN"]
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO biblio.publications (doi, title, year) "
                     "VALUES ('10.5555/restore-helper', 'Synthetic restore value', 2024)")
        before = conn.execute("SELECT * FROM pubdex.schema_changes ORDER BY number").fetchall()
        assert before
    with dump.open("wb") as output:
        subprocess.run(["docker", "exec", container, "pg_dump", "-U", "postgres", "-d", "people_db", "-Fc"],
                       stdout=output, check=True)
    # A successful comparison must come from the backup, not surviving rows.
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE biblio.publications SET title = 'Changed after backup' "
                     "WHERE doi = '10.5555/restore-helper'")
    subprocess.run(["bash", str(db / "restore_db.sh"), "--dump", str(dump), "--backup-dir", directory,
                    "--container", container, "--db-user", "postgres", "--db-name", "people_db",
                    "--no-start", "--no-globals", "--no-curation", "--yes"], check=True)
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT * FROM pubdex.schema_changes ORDER BY number").fetchall() == before
        assert conn.execute("SELECT title, year FROM biblio.publications "
                            "WHERE doi = '10.5555/restore-helper'").fetchone() == ("Synthetic restore value", 2024)
        owners = conn.execute(
            "SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN ('pubdex', 'app', 'biblio', 'pii', 'activity')"
        ).fetchall()
        assert owners == [("postgres",)], owners
        for role in ("app_readonly", "app_writer"):
            conn.execute(f"SET LOCAL ROLE {role}")
            assert conn.execute("SELECT * FROM pubdex.schema_changes ORDER BY number").fetchall() == before
            schema.check_supported(conn)
            conn.execute("RESET ROLE")
    print("Restored schema record, synthetic values, owner and schema support verified for both application roles.")


if __name__ == "__main__":
    main()
