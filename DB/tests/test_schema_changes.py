"""The baseline and the schema changes of this checkout, offline.

DB/init/ is the baseline: the schema of PubDex 0.1.0, which every database
starts from and which never changes again; a later release changes the
schema only through a schema change in DB/schema/. The baseline test holds
the SHA-256 of each file of /opt/pubdex/init in the published 0.1.0
application image, ghcr.io/ccu-ukn/pubdex:0.1.0 by its digest
sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325,
and fails on any difference: a file edited, added or removed.

Each file of DB/schema/ has to follow the rules people_pubs.schema applies
before it connects -- its name, its opening comment and its declaration,
statements that run in a transaction -- and every change of 0.2.0 only adds:
it declares itself backward compatible and holds none of the statements the
compatibility heuristic refuses.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import List

import pytest

from people_pubs import schema

ROOT = Path(__file__).resolve().parents[2]
INIT = ROOT / "DB" / "init"
SCHEMA = ROOT / "DB" / "schema"

#: /opt/pubdex/init of the published 0.1.0 application image.
BASELINE_0_1_0 = {
    "000_init.sql": "609c601a0ff88d6d6df54bf9fcb9c783d6aec90512d2b06820db79c6017c44f4",
    "010_ingestion_runtime_schema.sql": "8a739c9972041a171b3d632c35be6478ce041b5d60b3f1974df86c8bb9ec0cc1",
    "011_member_info_manual.sql": "d2bb2d1a39625c6cae1a65713fa84b8dd940ebe2a9e7c840b8da01a3af2f6fe1",
    "020_role_passwords.sh": "fc1bfea180f5cd8342b00721f4c10062058dc8eb6c4e7386a58846c80d95228b",
}
#: The schema changes PubDex 0.2.0 ships, all of them backward compatible
#: with 0.1.0.
CHANGES_OF_0_2_0 = ["0001_schema_change_record"]


def baseline_differences(directory: Path) -> List[str]:
    """How directory differs from the baseline, one line per file."""
    present = {path.name: path for path in directory.iterdir()}
    found = []
    for name in sorted(set(present) | set(BASELINE_0_1_0)):
        if name not in BASELINE_0_1_0:
            found.append(f"{name}: not part of the baseline")
        elif name not in present:
            found.append(f"{name}: missing")
        elif hashlib.sha256(present[name].read_bytes()).hexdigest() != BASELINE_0_1_0[name]:
            found.append(f"{name}: differs from 0.1.0's")
    return found


def test_db_init_is_the_baseline_of_0_1_0() -> None:
    assert baseline_differences(INIT) == [], (
        "DB/init/ is the baseline and never changes: change the schema with a schema change in DB/schema/")


@pytest.mark.parametrize("edit, expected", [
    (lambda d: (d / "010_ingestion_runtime_schema.sql").write_bytes(
        (d / "010_ingestion_runtime_schema.sql").read_bytes() + b"\n-- one more line\n"),
     ["010_ingestion_runtime_schema.sql: differs from 0.1.0's"]),
    (lambda d: (d / "012_more.sql").write_text("SELECT 1;\n", encoding="utf-8"),
     ["012_more.sql: not part of the baseline"]),
    (lambda d: (d / "011_member_info_manual.sql").unlink(), ["011_member_info_manual.sql: missing"]),
], ids=["edited", "added", "removed"])
def test_the_baseline_test_fails_on_a_changed_copy(tmp_path: Path, edit, expected: List[str]) -> None:
    copy = tmp_path / "init"
    shutil.copytree(INIT, copy)
    assert baseline_differences(copy) == []
    edit(copy)
    assert baseline_differences(copy) == expected


def test_the_runner_finds_db_schema_next_to_the_package() -> None:
    # In the image the package is /opt/pubdex/people_pubs and the changes
    # /opt/pubdex/schema, as DB/people_pubs and DB/schema are here.
    assert schema.SCHEMA_DIR == SCHEMA


def test_every_file_of_db_schema_is_a_usable_schema_change() -> None:
    changes = schema.load_changes(SCHEMA)
    assert [change.number for change in changes] == list(range(1, len(changes) + 1))
    assert sorted(path.name for path in SCHEMA.iterdir()) == [f"{change.name}.sql" for change in changes]
    for change in changes:
        assert change.sha256 == hashlib.sha256((SCHEMA / f"{change.name}.sql").read_bytes()).hexdigest()


def test_the_changes_of_0_2_0_only_add() -> None:
    changes = {change.name: change for change in schema.load_changes(SCHEMA)}
    assert list(changes)[:len(CHANGES_OF_0_2_0)] == CHANGES_OF_0_2_0
    for name in CHANGES_OF_0_2_0:
        assert changes[name].backward_compatible, name
        assert schema.compatibility_findings(changes[name].text) == [], name


def test_every_change_that_declares_itself_backward_compatible_passes_the_heuristic() -> None:
    for change in schema.load_changes(SCHEMA):
        if change.backward_compatible:
            assert schema.compatibility_findings(change.text) == [], change.name


def test_the_first_change_creates_the_record_and_grants_only_reading_it() -> None:
    first = schema.load_changes(SCHEMA)[0]
    words = [statement.words for statement in schema.statements(first.text)]
    assert words[0] == "CREATE SCHEMA PUBDEX"
    assert any(w.startswith("CREATE TABLE PUBDEX.SCHEMA_CHANGES (") for w in words)
    grants = [w for w in words if w.startswith(("GRANT", "REVOKE", "ALTER DEFAULT PRIVILEGES"))]
    assert grants == ["GRANT USAGE ON SCHEMA PUBDEX TO APP_READONLY, APP_WRITER",
                      "GRANT SELECT ON PUBDEX.SCHEMA_CHANGES TO APP_READONLY, APP_WRITER"]
