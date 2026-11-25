"""DB/check_release_notes.py, offline, on the checkout and on fixture trees.

The checkout's CHANGELOG.md has a section for its VERSION whose first three
subsections are Schema changes, Ingestion changes and Upgrade and rollback,
and the check passes on it. Each fixture tree is a repository root of its
own -- VERSION, the release record, DB/schema/, the transform version and
CHANGELOG.md -- in which one rule is broken: the check then fails with
status 1 and a message that names what is wrong. Trees that follow every
rule pass, a patch release without a change and a minor release with a
change that is not backward compatible and a raised floor among them.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

import check_release_notes
from check_release_notes import HEADINGS, check

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "DB" / "check_release_notes.py"

COMPATIBLE = "-- Adds a table.\n-- backward-compatible: yes\n\nCREATE TABLE pubdex.added (id integer);\n"
INCOMPATIBLE = "-- Renames a column.\n-- backward-compatible: no\n\nALTER TABLE pubdex.added RENAME COLUMN id TO key;\n"
FIRST = ("0001_schema_change_record", COMPATIBLE)

SCHEMA = "- `0001`, backward compatible: creates the record of the schema changes."
INGESTION = "The transform version is 1.5.4 (PubDex 0.1.0: 1.5.3). Rows are written as before."
UPGRADE = ("An upgrade to 0.2.0 is supported from 0.1.0, the floor and the preceding PubDex release. "
           "The rollback to 0.1.0 is an image rollback.")


def _entry(version: str, changes: Sequence[Tuple[str, str]] = (), transform: str = "1.5.3",
           floor: Optional[str] = None) -> dict:
    return {
        "version": version,
        "commit": "1" * 40,
        "tag": f"v{version}",
        "published": "2026-11-03",
        "platform": "linux/amd64",
        "images": {"application": f"registry.example.org/pubdex:{version}@sha256:{'a' * 64}",
                   "postgres": f"registry.example.org/pubdex-postgres:{version}@sha256:{'b' * 64}"},
        "schema_changes": [{"number": position, "name": name,
                            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                           for position, (name, text) in enumerate(changes, start=1)],
        "transform_version": transform,
        "floor": floor,
        "chart": {"version": version, "commit": "2" * 40, "package": None},
    }


def section(version: str = "0.2.0", schema: str = SCHEMA, ingestion: str = INGESTION, upgrade: str = UPGRADE,
            headings: Sequence[str] = HEADINGS, heading: Optional[str] = None) -> str:
    bodies = dict(zip(HEADINGS, (schema, ingestion, upgrade)))
    parts = [heading or f"## PubDex {version}", ""]
    for title in headings:
        parts += [f"### {title}", "", bodies.get(title, "None."), ""]
    parts += ["### The result", "", "What else the PubDex release delivers.", ""]
    return "\n".join(parts)


def changelog(*sections: str) -> str:
    older = ("## Container image and Compose deployment — version 0.1.0 published 2026-11-03\n\n"
             "### Schema changes\n\n- `0007`, which a section of another version may name.\n")
    return "# Changelog\n\nIntroduction.\n\n" + "\n".join(sections) + "\n" + older


def tree(root: Path, *, version: str = "0.2.0", changes: Sequence[Tuple[str, str]] = (FIRST,),
         transform: str = "1.5.4", entries: Optional[Dict[str, dict]] = None, floor: str = "0.1.0",
         notes: Optional[str] = None) -> Path:
    entries = entries if entries is not None else {"0.1.0": _entry("0.1.0")}
    for name, entry in entries.items():
        (root / "releases" / name).mkdir(parents=True)
        (root / "releases" / name / "release.json").write_text(json.dumps(entry), encoding="utf-8")
    (root / "releases" / "FLOOR").write_text(floor + "\n", encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "DB" / "schema").mkdir(parents=True)
    for name, text in changes:
        (root / "DB" / "schema" / f"{name}.sql").write_text(text, encoding="utf-8")
    (root / "DB" / "people_pubs").mkdir(parents=True)
    (root / "DB" / "people_pubs" / "__init__.py").write_text(
        f'from __future__ import annotations\n\n__version__ = "{transform}"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog(section(version)) if notes is None else notes, encoding="utf-8")
    return root


def problems(root: Path) -> List[str]:
    return check(root).problems


def fails_with(root: Path, *fragments: str) -> None:
    found = problems(root)
    assert found, "the check passes"
    for fragment in fragments:
        assert any(fragment in problem for problem in found), f"{fragment!r} not in {found}"


# --- The checkout ---------------------------------------------------------------------------------------------


def test_the_checkout_has_its_versions_section_with_the_three_headings_and_passes() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    notes, missing = check_release_notes.release_notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    assert notes is not None, missing
    result = check(ROOT)
    assert result.problems == []
    assert f"PubDex {version} against the preceding PubDex release" in result.summary


def test_the_checkout_passes_on_the_command_line() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("release notes: PubDex ") and "CHANGELOG.md agrees." in proc.stdout


# --- Trees that pass ------------------------------------------------------------------------------------------


def test_a_minor_release_whose_notes_agree_passes(tmp_path: Path) -> None:
    result = check(tree(tmp_path))
    assert result.problems == []
    assert result.summary == ("PubDex 0.2.0 against the preceding PubDex release 0.1.0: new schema changes "
                              "0001_schema_change_record (backward compatible); transform version 1.5.3 -> 1.5.4; "
                              "floor 0.1.0")


def test_a_patch_release_that_changes_nothing_passes_with_none(tmp_path: Path) -> None:
    root = tree(tmp_path, version="0.2.1", transform="1.5.4", floor="0.1.0",
                entries={"0.1.0": _entry("0.1.0"), "0.2.0": _entry("0.2.0", [FIRST], "1.5.4", "0.1.0")},
                notes=changelog(section("0.2.1", schema="None.", ingestion="None.",
                                        upgrade="Supported from 0.1.0, the floor, and from 0.2.0. The rollback "
                                                "is an image rollback.")))
    assert problems(root) == []


def test_an_incompatible_change_and_a_raised_floor_pass_when_the_notes_say_so(tmp_path: Path) -> None:
    root = tree(tmp_path, version="0.3.0", changes=[FIRST, ("0002_rename_key", INCOMPATIBLE)], transform="1.5.4",
                floor="0.2.0",
                entries={"0.1.0": _entry("0.1.0"), "0.2.0": _entry("0.2.0", [FIRST], "1.5.4", "0.1.0")},
                notes=changelog(section("0.3.0", schema="- `0002_rename_key`, not backward compatible: renames.",
                                        ingestion="None.",
                                        upgrade="This PubDex release raised the floor to 0.2.0, the preceding "
                                                "PubDex release. The rollback to 0.2.0 is a restore of the "
                                                "backup taken before the upgrade.")))
    assert problems(root) == []


def test_a_dated_heading_and_later_subsections_are_accepted(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section(heading="## PubDex 0.2.0 — published 2026-12-01")))
    assert problems(root) == []


def test_a_heading_inside_fenced_code_is_not_a_section(tmp_path: Path) -> None:
    fenced = "## Example\n\n```markdown\n## PubDex 0.2.0\n\n### Schema changes\n```\n"
    root = tree(tmp_path, notes=changelog(fenced))
    fails_with(root, "CHANGELOG.md has no section for PubDex 0.2.0")
    root = tree(tmp_path / "second", notes=changelog(section(schema=SCHEMA + "\n\n```\n### Ingestion changes\n```")))
    assert problems(root) == []


# --- The section's form ---------------------------------------------------------------------------------------


def test_a_missing_section_fails(tmp_path: Path) -> None:
    fails_with(tree(tmp_path, notes=changelog(section("0.1.9"))),
               "CHANGELOG.md has no section for PubDex 0.2.0: add one headed '## PubDex 0.2.0'")


def test_two_sections_of_one_version_fail(tmp_path: Path) -> None:
    fails_with(tree(tmp_path, notes=changelog(section(), section())),
               "CHANGELOG.md has 2 sections headed '## PubDex 0.2.0'; it may have one")


@pytest.mark.parametrize("left_out", HEADINGS)
def test_each_missing_heading_fails(tmp_path: Path, left_out: str) -> None:
    headings = [title for title in HEADINGS if title != left_out]
    fails_with(tree(tmp_path, notes=changelog(section(headings=headings))),
               "has to begin with the subsections '### Schema changes', '### Ingestion changes', "
               f"'### Upgrade and rollback', in this order; it lacks '### {left_out}'")


def test_headings_out_of_order_fail(tmp_path: Path) -> None:
    headings = (HEADINGS[1], HEADINGS[0], HEADINGS[2])
    fails_with(tree(tmp_path, notes=changelog(section(headings=headings))),
               "it has them in the order Ingestion changes, Schema changes, Upgrade and rollback")


def test_another_subsection_before_the_three_fails(tmp_path: Path) -> None:
    text = section().replace("### Schema changes", "### Highlights\n\nText.\n\n### Schema changes", 1)
    fails_with(tree(tmp_path, notes=changelog(text)), "it lacks '### Upgrade and rollback'")


# --- Schema changes -------------------------------------------------------------------------------------------


def test_a_new_schema_change_the_notes_do_not_name_fails(tmp_path: Path) -> None:
    fails_with(tree(tmp_path, notes=changelog(section(schema="None."))),
               "Schema changes does not name 0001_schema_change_record, new since PubDex 0.1.0, in a list item "
               "of its own: name it as `0001` and say that it is backward compatible")


def test_a_number_outside_a_code_span_does_not_name_a_change(tmp_path: Path) -> None:
    fails_with(tree(tmp_path, notes=changelog(section(schema="- 0001, backward compatible."))),
               "does not name 0001_schema_change_record")


def test_a_new_change_named_only_together_with_another_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, changes=[FIRST, ("0002_more", COMPATIBLE)],
                notes=changelog(section(schema="- `0001` and `0002`, both backward compatible.")))
    fails_with(root, "does not name 0001_schema_change_record", "does not name 0002_more")


def test_a_name_the_file_contradicts_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section(schema="- `0001_another_name`, backward compatible.")))
    fails_with(root, "Schema changes names `0001_another_name`, but schema change 0001 is 0001_schema_change_record")


def test_a_schema_change_that_does_not_exist_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section(schema=SCHEMA + "\n- `0002`, backward compatible.")))
    fails_with(root, "Schema changes names `0002`, but DB/schema/ holds no schema change 0002")


def test_a_path_names_a_change_too(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section(schema="- `DB/schema/0001_schema_change_record.sql`, backward "
                                                         "compatible.\n- `DB/schema/0003_gone.sql`.")))
    fails_with(root, "Schema changes names `DB/schema/0003_gone.sql`, but DB/schema/ holds no schema change 0003")
    assert not any("does not name 0001" in problem for problem in problems(root))


def test_a_compatible_change_called_not_backward_compatible_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section(schema="- `0001`, not backward compatible.")))
    fails_with(root, "Schema changes does not say that 0001_schema_change_record is backward compatible, as its "
                     "file declares, in the list item that names `0001`")


def test_an_incompatible_change_called_backward_compatible_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, changes=[FIRST, ("0002_rename_key", INCOMPATIBLE)],
                entries={"0.1.0": _entry("0.1.0", [FIRST])},
                notes=changelog(section(schema="- `0002`, backward compatible.",
                                        upgrade=UPGRADE + " The rollback is a restore of the backup.")))
    fails_with(root, "does not say that 0002_rename_key is not backward compatible")


def test_a_change_named_without_its_compatibility_fails(tmp_path: Path) -> None:
    fails_with(tree(tmp_path, notes=changelog(section(schema="- `0001`: creates the record."))),
               "does not say that 0001_schema_change_record is backward compatible")


def test_schema_changes_has_to_be_none_when_nothing_is_new(tmp_path: Path) -> None:
    root = tree(tmp_path, entries={"0.1.0": _entry("0.1.0", [FIRST])})
    fails_with(root, "Schema changes has to be 'None.': no schema change is new since PubDex 0.1.0")


def test_a_released_schema_change_that_is_missing_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, changes=[("0001_other_record", COMPATIBLE)],
                entries={"0.1.0": _entry("0.1.0", [FIRST])},
                notes=changelog(section(schema="None.")))
    fails_with(root, "schema change 0001_schema_change_record of PubDex 0.1.0 is missing from DB/schema/")


def test_a_released_schema_change_that_was_edited_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, changes=[("0001_schema_change_record", COMPATIBLE + "-- edited\n")],
                entries={"0.1.0": _entry("0.1.0", [FIRST])}, notes=changelog(section(schema="None.")))
    fails_with(root, "DB/schema/0001_schema_change_record.sql differs from the file PubDex 0.1.0 shipped")


# --- Ingestion changes ----------------------------------------------------------------------------------------


def test_a_new_transform_version_with_none_fails(tmp_path: Path) -> None:
    fails_with(tree(tmp_path, notes=changelog(section(ingestion="None."))),
               "Ingestion changes says 'None.', but the transform version is 1.5.4, not 1.5.3 as in PubDex 0.1.0")


def test_a_new_transform_version_the_notes_do_not_name_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section(ingestion="The transform version is 1.5.40, and 11.5.4 is not.")))
    fails_with(root, "Ingestion changes does not name the transform version 1.5.4 (PubDex 0.1.0: 1.5.3)")


def test_an_unchanged_transform_version_with_text_fails(tmp_path: Path) -> None:
    fails_with(tree(tmp_path, transform="1.5.3"),
               "Ingestion changes has to be 'None.': the transform version is 1.5.3, as in PubDex 0.1.0")


# --- Upgrade and rollback -------------------------------------------------------------------------------------


def test_notes_that_name_neither_the_floor_nor_the_preceding_release_fail(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section(upgrade="Upgrade by swapping the images; 0.1.00 is no version.")))
    fails_with(root, "Upgrade and rollback does not name the floor, 0.1.0 (releases/FLOOR)",
               "Upgrade and rollback does not name the preceding PubDex release, 0.1.0")


def test_notes_without_the_floor_fail_when_it_is_older_than_the_preceding_release(tmp_path: Path) -> None:
    root = tree(tmp_path, version="0.3.0", changes=[FIRST], transform="1.5.4", floor="0.1.0",
                entries={"0.1.0": _entry("0.1.0"), "0.2.0": _entry("0.2.0", [FIRST], "1.5.4", "0.1.0")},
                notes=changelog(section("0.3.0", schema="None.", ingestion="None.",
                                        upgrade="Supported from 0.2.0, the preceding PubDex release.")))
    assert problems(root) == ["Upgrade and rollback does not name the floor, 0.1.0 (releases/FLOOR)"]


def test_a_raised_floor_the_notes_do_not_mention_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, version="0.3.0", changes=[FIRST], transform="1.5.4", floor="0.2.0",
                entries={"0.1.0": _entry("0.1.0"), "0.2.0": _entry("0.2.0", [FIRST], "1.5.4", "0.1.0")},
                notes=changelog(section("0.3.0", schema="None.", ingestion="None.",
                                        upgrade="Supported from 0.2.0, the floor. The floor is 0.2.0, "
                                                "which was raised.")))
    # "floor" and "raised" stand in one sentence: accepted.
    assert problems(root) == []
    (root / "CHANGELOG.md").write_text(changelog(section("0.3.0", schema="None.", ingestion="None.",
                                                         upgrade="Supported from 0.2.0, the floor. It was raised.")),
                                       encoding="utf-8")
    fails_with(root, "releases/FLOOR raises the floor from 0.1.0, the floor of PubDex 0.2.0, to 0.2.0, but "
                     "Upgrade and rollback does not say so")


def test_a_floor_where_the_preceding_release_had_none_is_no_raise(tmp_path: Path) -> None:
    # 0.1.0, the first PubDex release, has no floor; 0.2.0's floor 0.1.0 raises nothing.
    assert problems(tree(tmp_path)) == []


def test_an_incompatible_change_without_a_restore_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, changes=[FIRST, ("0002_rename_key", INCOMPATIBLE)],
                entries={"0.1.0": _entry("0.1.0", [FIRST])},
                notes=changelog(section(schema="- `0002`, not backward compatible.",
                                        upgrade=UPGRADE + " Restore the backup if needed.")))
    fails_with(root, "0002_rename_key is not backward compatible, but Upgrade and rollback does not say that the "
                     "rollback is a restore: say in one sentence that the rollback to PubDex 0.1.0 is a restore")


# --- Versions -------------------------------------------------------------------------------------------------


def test_a_test_release_version_in_version_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, version="0.2.0-test.a")
    fails_with(root, "VERSION holds 0.2.0-test.a, a test release's version")


def test_a_patch_release_with_a_new_schema_change_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, version="0.2.1", changes=[FIRST, ("0002_more", COMPATIBLE)], transform="1.5.4",
                entries={"0.1.0": _entry("0.1.0"), "0.2.0": _entry("0.2.0", [FIRST], "1.5.4", "0.1.0")},
                notes=changelog(section("0.2.1", schema="- `0002`, backward compatible.", ingestion="None.",
                                        upgrade="Supported from 0.1.0 and 0.2.0.")))
    assert problems(root) == ["0.2.1 is a patch release of 0.2.0, which changes no schema, but 0002_more is new: a "
                              "schema change needs a minor release, such as 0.3.0"]


def test_a_patch_release_with_a_new_transform_version_fails(tmp_path: Path) -> None:
    root = tree(tmp_path, version="0.2.1", changes=[FIRST], transform="1.5.5",
                entries={"0.1.0": _entry("0.1.0"), "0.2.0": _entry("0.2.0", [FIRST], "1.5.4", "0.1.0")},
                notes=changelog(section("0.2.1", schema="None.", ingestion="The transform version is 1.5.5.",
                                        upgrade="Supported from 0.1.0 and 0.2.0.")))
    assert problems(root) == ["0.2.1 is a patch release of 0.2.0, which changes no ingestion, but the transform "
                              "version is 1.5.5, not 1.5.4: an ingestion change needs a minor release, such as "
                              "0.3.0"]


def test_without_a_preceding_release_the_reader_says_why(tmp_path: Path) -> None:
    root = tree(tmp_path, version="0.1.0")
    fails_with(root, "no PubDex release below 0.1.0 is recorded (recorded: 0.1.0)")


# --- The command line -----------------------------------------------------------------------------------------


def test_the_command_line_lists_every_problem_and_exits_1(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section(schema="None.", ingestion="None.", upgrade="Swap the images.")))
    out, err = io.StringIO(), io.StringIO()
    assert check_release_notes.main(["--root", str(root)], out, err) == 1
    assert out.getvalue() == ""
    lines = err.getvalue().splitlines()
    assert lines[0] == "release notes: PubDex 0.2.0: 4 problem(s):"
    assert all(line.startswith("  - ") for line in lines[1:]) and len(lines) == 5


def test_the_script_exits_1_on_a_failing_tree_and_2_on_a_usage_error(tmp_path: Path) -> None:
    root = tree(tmp_path, notes=changelog(section("0.1.9")))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 1 and proc.stdout == ""
    assert "CHANGELOG.md has no section for PubDex 0.2.0" in proc.stderr
    proc = subprocess.run([sys.executable, str(SCRIPT), "--unknown"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
