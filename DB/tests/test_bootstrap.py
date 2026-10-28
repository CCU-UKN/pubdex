"""people_pubs.bootstrap, offline: the schema bootstrap the image carries.

The command fills a directory standing in for the bootstrap volume of the
Compose deployment, in tmp_path, from the real DB/init/ or a scratch source.
It proves that the directory ends up holding exactly the source's files, byte
for byte, with the modes PostgreSQL's entrypoint and set_passwords.sh need;
that files of an earlier image and the remains of an interrupted copy
go; that it fails, naming the path and never a file's content, where it
cannot do that; and that it finds DB/init/ where the image places it, next
to the package.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path

import pytest

from people_pubs import bootstrap

ROOT = Path(__file__).resolve().parents[2]
INIT = ROOT / "DB" / "init"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _source(tmp_path: Path, files: dict) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    for name, text in files.items():
        (source / name).write_text(text, encoding="utf-8")
    return source


def test_the_source_is_db_init_next_to_the_package() -> None:
    # In the image the package is /opt/pubdex/people_pubs and the files are
    # /opt/pubdex/init, as DB/people_pubs and DB/init are in the repository.
    assert bootstrap.SOURCE == INIT
    assert [path.name for path in bootstrap.source_files(INIT)] == sorted(path.name for path in INIT.iterdir())


def test_it_fills_the_directory_with_db_init_byte_for_byte(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    target = tmp_path / "initdb"
    target.mkdir()
    assert bootstrap.main([str(target)]) == 0
    names = sorted(path.name for path in INIT.iterdir())
    assert sorted(path.name for path in target.iterdir()) == names
    for name in names:
        assert (target / name).read_bytes() == (INIT / name).read_bytes(), name
        assert _mode(target / name) == (0o755 if name.endswith(".sh") else 0o644), name
    out = capsys.readouterr().out.splitlines()
    assert out[:-1] == [f"bootstrap: {name} sha256:{hashlib.sha256((INIT / name).read_bytes()).hexdigest()}"
                        for name in names]
    assert out[-1] == f"bootstrap: {target} holds the {len(names)} files of this image's schema bootstrap"


def test_modes_do_not_depend_on_the_source_or_the_umask(tmp_path: Path) -> None:
    source = _source(tmp_path, {"000_schema.sql": "SELECT 1;\n", "020_roles.sh": "#!/bin/sh\n"})
    (source / "000_schema.sql").chmod(0o600)
    (source / "020_roles.sh").chmod(0o644)  # a checkout without file modes
    target = tmp_path / "initdb"
    target.mkdir()
    previous = os.umask(0o077)
    try:
        bootstrap.install(source, target)
    finally:
        os.umask(previous)
    assert _mode(target / "000_schema.sql") == 0o644 and _mode(target / "020_roles.sh") == 0o755


def test_a_second_run_replaces_what_an_earlier_image_left(tmp_path: Path) -> None:
    target = tmp_path / "initdb"
    target.mkdir()
    (target / "000_schema.sql").write_text("-- an earlier schema\n", encoding="utf-8")
    (target / "030_dropped_in_this_release.sql").write_text("SELECT 2;\n", encoding="utf-8")
    (target / ".000_schema.sql.12.tmp").write_text("-- an interrupted copy\n", encoding="utf-8")
    (target / "stale-link").symlink_to(tmp_path / "nowhere")
    source = _source(tmp_path, {"000_schema.sql": "SELECT 1;\n"})
    assert bootstrap.install(source, target) == [("000_schema.sql", hashlib.sha256(b"SELECT 1;\n").hexdigest())]
    assert sorted(path.name for path in target.iterdir()) == ["000_schema.sql"]
    assert (target / "000_schema.sql").read_text(encoding="utf-8") == "SELECT 1;\n"
    assert bootstrap.install(source, target) == bootstrap.install(source, target), "the same on every start"


@pytest.mark.parametrize("arrange, message", [
    (lambda source, target: (target / "nested").mkdir(), "is a directory, which the schema bootstrap never holds"),
    (lambda source, target: (source / "999_extra.sql").mkdir(), "999_extra.sql is not a regular file"),
    (lambda source, target: (source / "000_schema.sql").unlink(), "holds no .sql file to bootstrap from"),
    (lambda source, target: target.rmdir(), "initdb is not a directory"),
], ids=["directory-in-target", "directory-in-source", "no-sql", "no-target"])
def test_what_it_cannot_make_right_fails_naming_the_path(tmp_path: Path, capsys: pytest.CaptureFixture,
                                                         monkeypatch: pytest.MonkeyPatch, arrange, message: str) -> None:
    source = _source(tmp_path, {"000_schema.sql": "SELECT 'not shown';\n", "020_roles.sh": "#!/bin/sh\n"})
    target = tmp_path / "initdb"
    target.mkdir()
    arrange(source, target)
    monkeypatch.setattr(bootstrap, "SOURCE", source)
    assert bootstrap.main([str(target)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("bootstrap: ") and message in err, err
    assert "not shown" not in err


def test_a_directory_it_may_not_write_names_the_owner_it_needs(tmp_path: Path, capsys: pytest.CaptureFixture,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # The volume as Docker creates it for a container of another image: owned
    # by root, so the image's user cannot write it.
    def refused(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    target = tmp_path / "initdb"
    target.mkdir()
    monkeypatch.setattr(bootstrap.os, "open", refused)
    assert bootstrap.main([str(target)]) == 1
    err = capsys.readouterr().err
    assert f"cannot write {target}/000_init.sql: Permission denied" in err
    assert "uid 10001" in err and "first mounted on" in err
    assert list(target.iterdir()) == [], "no temporary file is left"


def test_a_directory_is_required(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exit_info:
        bootstrap.main([])
    assert exit_info.value.code == 2
    assert "usage: python -m people_pubs.bootstrap" in capsys.readouterr().err
