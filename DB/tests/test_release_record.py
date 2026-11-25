"""DB/release_record.py and releases/, offline.

The committed record holds 0.1.0's entry, which passes every check and names
what the published release is: its commit, both images by digest, its chart
in the repository's history, no schema change and the transform version
1.5.3; FLOOR names a recorded release. The reader is then run on records
written into a temporary directory: it orders versions as Semantic
Versioning does, finds the preceding release below VERSION or a given
version, a test release's version taken without its test part, stands in
VERSION's own entry only while nothing lies below VERSION, and refuses
every malformed entry, a test release in the record, a FLOOR that names no
recorded release, and an unknown release, with status 1 and a message that
says why.

Concrete published image references in the public guides must match the
release record for their version and image role.
"""
from __future__ import annotations

import copy
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import release_record
from release_record import Record, RecordError

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "DB" / "release_record.py"
DIGEST_APP = "sha256:24d570015b20f222c767a8bb89dd18d792abbd25cc730137f7b8bc285df6a325"
DIGEST_PG = "sha256:33988fb179e2065e653ad7813cecf812c21f4381537b8b8db546ad37236b780a"
GUIDES = ("README.md", "DEPLOYMENT.md", "KUBERNETES.md", "VERSIONING.md", "RELEASING.md", "releases/README.md")
PUBLISHED_IMAGE = re.compile(
    r"ghcr\.io/ccu-ukn/(?P<repository>pubdex(?:-postgres)?):(?P<version>[0-9A-Za-z._-]+)"
    r"@(?P<digest>sha256:[0-9a-f]{64})(?![0-9a-f])"
)


def _entry(version: str = "0.1.0", **changes: object) -> dict:
    entry = {
        "version": version,
        "commit": "1" * 40,
        "tag": f"v{version}",
        "published": "2026-11-03",
        "platform": "linux/amd64",
        "images": {"application": f"registry.example.org/pubdex:{version}@sha256:{'a' * 64}",
                   "postgres": f"registry.example.org/pubdex-postgres:{version}@sha256:{'b' * 64}"},
        "schema_changes": [],
        "transform_version": "1.5.3",
        "floor": None,
        "chart": {"version": version, "commit": "2" * 40, "package": None},
    }
    entry.update(changes)
    return entry


def _record(tmp_path: Path, versions: dict, checkout: str = "0.2.0", floor: str = "0.1.0") -> Record:
    for version, entry in versions.items():
        (tmp_path / "releases" / version).mkdir(parents=True)
        (tmp_path / "releases" / version / "release.json").write_text(json.dumps(entry), encoding="utf-8")
    (tmp_path / "releases").mkdir(exist_ok=True)
    (tmp_path / "releases" / "FLOOR").write_text(floor + "\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text(checkout + "\n", encoding="utf-8")
    return Record(tmp_path)


def _main(*args: str) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    status = release_record.main(list(args), out, err)
    return status, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# The committed record
# ---------------------------------------------------------------------------

def test_the_committed_record_names_the_published_0_1_0() -> None:
    entry = Record(ROOT).entry("0.1.0")
    assert entry["commit"] == "57a72dc9f27766007b51c3d16ac7aa8c6ff3798b"
    assert entry["tag"] == "v0.1.0" and entry["published"] == "2026-11-03"
    assert entry["images"] == {"application": f"ghcr.io/ccu-ukn/pubdex:0.1.0@{DIGEST_APP}",
                               "postgres": f"ghcr.io/ccu-ukn/pubdex-postgres:0.1.0@{DIGEST_PG}"}
    assert entry["schema_changes"] == [] and entry["transform_version"] == "1.5.3" and entry["floor"] is None
    assert entry["chart"] == {"version": "0.1.0", "commit": "a86d5e04a8a41795d476c5d1655bbee5b05150a7",
                              "package": None}


def test_every_committed_entry_passes_and_floor_names_one() -> None:
    record = Record(ROOT)
    versions = record.versions()
    assert versions and all(record.entry(version)["version"] == version for version in versions)
    assert record.floor() in versions
    assert sorted(path.name for path in (ROOT / "releases").iterdir()) == sorted([*versions, "FLOOR", "README.md"])


def test_the_preceding_release_of_this_checkout_is_recorded() -> None:
    version, _note = Record(ROOT).preceding()
    assert version in Record(ROOT).versions()


def test_the_command_line_prints_fields_one_a_line() -> None:
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), "field", "0.1.0", "images.application", "chart.commit",
                           "chart.package", "schema_changes"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [f"ghcr.io/ccu-ukn/pubdex:0.1.0@{DIGEST_APP}",
                                        "a86d5e04a8a41795d476c5d1655bbee5b05150a7", "null", "[]"]


def _guide_image_problems(text: str, record: Record) -> list:
    problems = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for match in PUBLISHED_IMAGE.finditer(line):
            version = match["version"]
            role = "postgres" if match["repository"] == "pubdex-postgres" else "application"
            try:
                expected = record.entry(version)["images"][role].rsplit("@", 1)[1]
            except RecordError as exc:
                problems.append(f"line {line_number}: {exc}")
                continue
            if match["digest"] != expected:
                problems.append(f"line {line_number}: {version} {role} digest {match['digest']} != {expected}")
    return problems


@pytest.mark.parametrize("guide", GUIDES)
def test_public_guide_image_digests_match_their_release_record(guide: str) -> None:
    problems = _guide_image_problems((ROOT / guide).read_text(encoding="utf-8"), Record(ROOT))
    assert not problems, guide + ":\n" + "\n".join(problems)


def test_guide_images_are_checked_by_version_and_role(tmp_path: Path) -> None:
    entries = {version: _entry(version) for version in ("0.1.0", "0.2.0")}
    entries["0.2.0"]["images"]["application"] = f"registry.example.org/pubdex:0.2.0@sha256:{'c' * 64}"
    record = _record(tmp_path, entries)
    text = "\n".join(image.replace("registry.example.org", "ghcr.io/ccu-ukn")
                     for entry in entries.values() for image in entry["images"].values())
    assert len(list(PUBLISHED_IMAGE.finditer(text))) == 4
    assert not _guide_image_problems(text, record)
    for wrong in (text.replace("c" * 64, "a" * 64), text.replace("b" * 64, "a" * 64)):
        assert any("digest" in problem for problem in _guide_image_problems(wrong, record))
    unknown = text.replace("pubdex:0.2.0@", "pubdex:0.9.0@")
    assert any("no PubDex release 0.9.0" in problem for problem in _guide_image_problems(unknown, record))
    assert not _guide_image_problems("ghcr.io/ccu-ukn/pubdex:<version>@sha256:<digest>", record)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def test_versions_are_ordered_as_semantic_versioning_orders_them() -> None:
    ordered = ["0.1.0", "0.2.0-alpha", "0.2.0-alpha.1", "0.2.0-alpha.beta", "0.2.0-beta.2", "0.2.0-beta.11",
               "0.2.0-rc.1", "0.2.0-test.a", "0.2.0-test.b1", "0.2.0", "0.10.0", "1.0.0"]
    assert sorted(reversed(ordered), key=release_record.precedence) == ordered


@pytest.mark.parametrize("version, test", [("0.2.0-test.a", True), ("0.2.0-test.b1", True), ("0.2.0-rc.1", False),
                                           ("0.2.0", False)])
def test_a_test_release_is_recognised_by_its_pre_release_part(version: str, test: bool) -> None:
    assert release_record.is_test_version(version) is test
    assert release_record.without_test(version) == ("0.2.0" if test else version)


@pytest.mark.parametrize("version", ["0.1", "v0.1.0", "01.0.0", "0.1.0-", "0.1.0-a..b", "", "0.1.0 "])
def test_what_is_no_version(version: str) -> None:
    assert not release_record.is_version(version)


# ---------------------------------------------------------------------------
# preceding and floor
# ---------------------------------------------------------------------------

def test_the_preceding_release_is_the_newest_below_version(tmp_path: Path) -> None:
    record = _record(tmp_path, {v: _entry(v) for v in ("0.1.0", "0.2.0", "0.2.1", "0.3.0")}, checkout="0.3.0")
    assert record.preceding() == ("0.2.1", None)
    assert record.preceding("0.3.0-test.b1") == ("0.2.1", None)
    assert record.preceding("1.0.0") == ("0.3.0", None)
    assert record.preceding("0.2.1") == ("0.2.0", None)


def test_the_newest_release_is_not_its_own_predecessor(tmp_path: Path) -> None:
    record = _record(tmp_path, {v: _entry(v) for v in ("0.1.0", "0.2.0")}, checkout="0.2.0")
    assert record.preceding() == ("0.1.0", None)


def test_without_a_release_below_the_version_there_is_no_preceding_one(tmp_path: Path) -> None:
    record = _record(tmp_path, {"0.2.0": _entry("0.2.0")}, checkout="0.1.5")
    with pytest.raises(RecordError, match=r"no PubDex release below 0\.1\.5 is recorded \(recorded: 0\.2\.0\)"):
        record.preceding()


def test_floor_names_a_recorded_release(tmp_path: Path) -> None:
    record = _record(tmp_path, {v: _entry(v) for v in ("0.1.0", "0.2.0")}, floor="0.2.0")
    assert record.floor() == "0.2.0"
    (tmp_path / "releases" / "FLOOR").write_text("0.1.5\n", encoding="utf-8")
    with pytest.raises(RecordError, match="no PubDex release 0.1.5 is recorded"):
        record.floor()
    (tmp_path / "releases" / "FLOOR").write_text("0.1.0\n0.2.0\n", encoding="utf-8")
    with pytest.raises(RecordError, match="one line"):
        record.floor()


def test_the_command_line_refuses_when_only_the_same_release_is_recorded(tmp_path: Path) -> None:
    _record(tmp_path, {"0.1.0": _entry()}, checkout="0.1.0")
    for args in ((), ("--version", "0.1.0")):
        status, out, err = _main("--root", str(tmp_path), "preceding", *args)
        assert (status, out) == (1, "") and "no PubDex release below 0.1.0 is recorded" in err


def test_the_command_line_refuses_a_malformed_version() -> None:
    with pytest.raises(SystemExit) as raised:
        _main("preceding", "--version", "latest")
    assert raised.value.code == 2


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

BROKEN = {
    "a missing field": ({"transform_version": None}, "has the fields"),
    "another version": ({"version": "0.1.1"}, "names the version"),
    "a short commit": ({"commit": "57a72dc"}, "commit is not a full commit id"),
    "another tag": ({"tag": "0.1.0"}, "tag is"),
    "no date": ({"published": "3 November 2026"}, "published is not a date"),
    "another platform": ({"platform": "linux/arm64"}, "platform is"),
    "an image without a digest": ({"images": {"application": "registry.example.org/pubdex:0.1.0",
                                              "postgres": f"registry.example.org/pubdex-postgres:0.1.0@sha256:{'b' * 64}"}},
                                  "images.application is not"),
    "an image of another version": ({"images": {"application": f"registry.example.org/pubdex:0.1.1@sha256:{'a' * 64}",
                                                "postgres": f"registry.example.org/pubdex-postgres:0.1.0@sha256:{'b' * 64}"}},
                                    "has the tag 0.1.1"),
    "a gap in the schema changes": ({"schema_changes": [{"number": 2, "name": "0002_x", "sha256": "c" * 64}]},
                                    "the numbers run from 1 without a gap"),
    "a name of another number": ({"schema_changes": [{"number": 1, "name": "0002_x", "sha256": "c" * 64}]},
                                 "not NNNN_<name> with its number"),
    "a change without its hash": ({"schema_changes": [{"number": 1, "name": "0001_x", "sha256": "abc"}]},
                                  "has no SHA-256"),
    "a floor above the release": ({"floor": "0.2.0"}, "floor is"),
    "a chart without its commit": ({"chart": {"version": "0.1.0", "commit": "", "package": None}},
                                   "chart.commit is not"),
    "a chart package of another kind": ({"chart": {"version": "0.1.0", "commit": "2" * 40, "package": "pubdex.zip"}},
                                        "chart.package is neither"),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_a_malformed_entry_is_refused_and_named(tmp_path: Path, case: str) -> None:
    changes, message = BROKEN[case]
    entry = _entry(**copy.deepcopy(changes))
    if case == "a missing field":
        del entry["transform_version"]
    _record(tmp_path, {"0.1.0": entry})
    status, out, err = _main("--root", str(tmp_path), "entry", "0.1.0")
    assert status == 1 and out == ""
    assert err.startswith("release_record.py: releases/0.1.0/release.json: ") and message in err


def test_a_test_release_never_enters_the_record(tmp_path: Path) -> None:
    record = _record(tmp_path, {"0.1.0": _entry(), "0.2.0-test.a": _entry("0.2.0-test.a")})
    with pytest.raises(RecordError, match="a test release is never recorded"):
        record.versions()


def test_an_entry_that_is_no_json_is_refused(tmp_path: Path) -> None:
    _record(tmp_path, {"0.1.0": _entry()})
    (tmp_path / "releases" / "0.1.0" / "release.json").write_text("{", encoding="utf-8")
    status, _out, err = _main("--root", str(tmp_path), "entry", "0.1.0")
    assert status == 1 and "cannot be read as JSON" in err


def test_an_unknown_release_or_field_is_refused(tmp_path: Path) -> None:
    _record(tmp_path, {"0.1.0": _entry()})
    status, _out, err = _main("--root", str(tmp_path), "entry", "0.3.0")
    assert status == 1 and "no PubDex release 0.3.0 is recorded (recorded: 0.1.0)" in err
    status, _out, err = _main("--root", str(tmp_path), "field", "0.1.0", "images.registry")
    assert status == 1 and "has no field images.registry" in err


def test_without_a_record_nothing_is_read(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    status, _out, err = _main("--root", str(tmp_path), "preceding")
    assert status == 1 and "there is no release record" in err
