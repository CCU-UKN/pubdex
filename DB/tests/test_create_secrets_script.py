"""create_secrets.sh, offline: the one-time initialiser of the Compose
deployment's secret files.

The real script runs from a scratch directory standing in for the repository
root, so every file it creates is a throwaway in tmp_path. The tests prove
that it creates the directory owner-only and each missing file readable,
complete and random; that a rerun keeps every existing file byte for byte,
creates only what is missing, and never follows a link; that it refuses a
directory other users can enter and reports unusable files without changing
them; that it finds the directory as Compose does, whatever the working
directory; and that it prints no password. With --check, which
set_passwords.sh runs first, it creates nothing and reports a missing or
unusable file.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict

import pytest

from tests.test_password_files import MARKER, PASSWORD_SAMPLES, UNUSABLE_SAMPLES

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "create_secrets.sh"
NAMES = ("postgres_password", "app_readonly_password", "app_writer_password")
GENERATED = re.compile(r"[0-9a-f]{64}\n")


class Checkout:
    """A scratch repository root holding a copy of the script."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "checkout"
        self.root.mkdir()
        self.script = self.root / SCRIPT.name
        shutil.copy2(SCRIPT, self.script)
        self.secrets = self.root / "secrets"

    def run(self, *args: str, cwd: Path = None, **extra: str) -> subprocess.CompletedProcess:
        env: Dict[str, str] = {"PATH": os.environ["PATH"], "LANG": os.environ.get("LANG", "C.UTF-8")}
        env.update(extra)
        return subprocess.run([str(self.script), *args], cwd=cwd or self.root, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def contents(self, directory: Path = None) -> Dict[str, bytes]:
        directory = directory or self.secrets
        return {name: (directory / name).read_bytes() for name in NAMES if (directory / name).exists()}


@pytest.fixture
def checkout(tmp_path: Path) -> Checkout:
    return Checkout(tmp_path)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_it_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_a_first_run_creates_an_owner_only_directory_and_three_random_files(checkout: Checkout) -> None:
    proc = checkout.run()
    assert proc.returncode == 0, proc.stderr
    assert _mode(checkout.secrets) == 0o700
    contents = checkout.contents()
    assert sorted(contents) == sorted(NAMES)
    for name, content in contents.items():
        assert GENERATED.fullmatch(content.decode()), name
        assert _mode(checkout.secrets / name) == 0o444, name
        assert content.decode().strip() not in proc.stdout + proc.stderr, "no password is printed"
        assert f"created   {name}" in proc.stdout
    assert len(set(contents.values())) == 3, "each file holds a password of its own"
    assert [p.name for p in checkout.secrets.iterdir() if p.name.startswith(".")] == [], "no temporary file is left"
    assert "docker compose up --wait" in proc.stdout


def test_a_rerun_keeps_every_file_and_creates_only_what_is_missing(checkout: Checkout) -> None:
    assert checkout.run().returncode == 0
    before = checkout.contents()
    inodes = {name: (checkout.secrets / name).stat().st_ino for name in NAMES}
    again = checkout.run()
    assert again.returncode == 0, again.stderr
    assert checkout.contents() == before
    assert {name: (checkout.secrets / name).stat().st_ino for name in NAMES} == inodes, "nothing was replaced"
    assert all(f"kept      {name}" in again.stdout for name in NAMES)

    (checkout.secrets / "app_writer_password").unlink()
    partial = checkout.run()
    assert partial.returncode == 0, partial.stderr
    after = checkout.contents()
    assert {n: c for n, c in after.items() if n != "app_writer_password"} == \
        {n: c for n, c in before.items() if n != "app_writer_password"}
    assert after["app_writer_password"] != before["app_writer_password"]
    assert "created   app_writer_password" in partial.stdout


def test_a_directory_other_users_can_enter_is_refused(checkout: Checkout) -> None:
    checkout.secrets.mkdir(mode=0o755)
    checkout.secrets.chmod(0o755)
    proc = checkout.run()
    assert proc.returncode == 1
    assert "open to other users" in proc.stderr and "chmod 700" in proc.stderr
    assert list(checkout.secrets.iterdir()) == []


def test_something_else_in_the_place_of_the_directory_is_refused(checkout: Checkout) -> None:
    checkout.secrets.write_text("not a directory\n", encoding="utf-8")
    proc = checkout.run()
    assert proc.returncode == 1 and "is not a directory" in proc.stderr


@pytest.mark.parametrize("content, problem", UNUSABLE_SAMPLES)
def test_an_unusable_existing_file_is_reported_and_left_alone(
        checkout: Checkout, content: bytes, problem: str) -> None:
    checkout.secrets.mkdir(mode=0o700)
    target = checkout.secrets / "app_readonly_password"
    target.write_bytes(content)
    target.chmod(0o444)
    proc = checkout.run()
    assert proc.returncode == 1
    assert "UNUSABLE  app_readonly_password" in proc.stderr and problem in proc.stderr
    assert target.read_bytes() == content, "an existing file is never changed"
    assert MARKER not in proc.stdout + proc.stderr
    assert sorted(checkout.contents()) == sorted(NAMES), "the missing files are still created"


@pytest.mark.parametrize("content, expected", PASSWORD_SAMPLES)
def test_a_usable_existing_file_is_kept(checkout: Checkout, content: bytes, expected: str) -> None:
    checkout.secrets.mkdir(mode=0o700)
    target = checkout.secrets / "app_writer_password"
    target.write_bytes(content)
    target.chmod(0o444)
    proc = checkout.run()
    assert proc.returncode == 0, proc.stderr
    assert "kept      app_writer_password" in proc.stdout
    assert target.read_bytes() == content
    assert expected not in proc.stdout + proc.stderr


def test_a_role_file_its_container_cannot_read_is_reported(checkout: Checkout) -> None:
    assert checkout.run().returncode == 0
    (checkout.secrets / "app_writer_password").chmod(0o600)
    # The superuser's file is read by root in its container, so 600 is enough.
    (checkout.secrets / "postgres_password").chmod(0o600)
    proc = checkout.run()
    assert proc.returncode == 1
    assert "UNUSABLE  app_writer_password: is not readable for other users" in proc.stderr
    assert "chmod 444" in proc.stderr
    assert "kept      postgres_password" in proc.stdout


def test_two_files_with_the_same_password_are_reported(checkout: Checkout) -> None:
    assert checkout.run().returncode == 0
    reader = checkout.secrets / "app_readonly_password"
    writer = checkout.secrets / "app_writer_password"
    writer.chmod(0o644)
    writer.write_bytes(reader.read_bytes())
    proc = checkout.run()
    assert proc.returncode == 1
    assert "app_readonly_password and app_writer_password hold the same password" in proc.stderr


def test_a_link_in_the_place_of_a_file_is_never_followed(checkout: Checkout, tmp_path: Path) -> None:
    checkout.secrets.mkdir(mode=0o700)
    target = tmp_path / "elsewhere" / "file"
    (checkout.secrets / "postgres_password").symlink_to(target)
    proc = checkout.run()
    assert proc.returncode == 1
    assert "UNUSABLE  postgres_password: is not a file" in proc.stderr
    assert not target.exists() and not target.parent.exists(), "the link's target was not created"


def test_the_directory_is_found_as_compose_finds_it(checkout: Checkout, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # Relative: from the directory of compose.yaml, not from the working directory.
    relative = checkout.run(cwd=elsewhere, PEOPLE_PUBS_SECRETS_DIR="private/secrets")
    assert relative.returncode == 0, relative.stderr
    assert sorted(checkout.contents(checkout.root / "private" / "secrets")) == sorted(NAMES)
    assert list(elsewhere.iterdir()) == []
    absolute_dir = tmp_path / "absolute"
    absolute = checkout.run(cwd=elsewhere, PEOPLE_PUBS_SECRETS_DIR=str(absolute_dir))
    assert absolute.returncode == 0, absolute.stderr
    assert sorted(checkout.contents(absolute_dir)) == sorted(NAMES)
    assert _mode(absolute_dir) == 0o700


def test_check_creates_nothing_and_accepts_usable_files(checkout: Checkout) -> None:
    missing_directory = checkout.run("--check")
    assert missing_directory.returncode == 1 and "does not exist" in missing_directory.stderr
    assert not checkout.secrets.exists(), "--check creates no directory"
    assert checkout.run().returncode == 0
    before = checkout.contents()
    proc = checkout.run("--check")
    assert proc.returncode == 0, proc.stderr
    assert all(f"kept      {name}" in proc.stdout for name in NAMES)
    assert "The three files are usable" in proc.stdout and "docker compose up" not in proc.stdout
    assert checkout.contents() == before
    assert all(content.decode().strip() not in proc.stdout + proc.stderr for content in before.values())


def test_check_reports_a_missing_file_without_creating_it(checkout: Checkout) -> None:
    assert checkout.run().returncode == 0
    (checkout.secrets / "app_readonly_password").unlink()
    proc = checkout.run("--check")
    assert proc.returncode == 1 and "MISSING   app_readonly_password" in proc.stderr
    assert not (checkout.secrets / "app_readonly_password").exists()
    assert [p.name for p in checkout.secrets.iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize("content, problem", UNUSABLE_SAMPLES)
def test_check_reports_an_unusable_file(checkout: Checkout, content: bytes, problem: str) -> None:
    assert checkout.run().returncode == 0
    target = checkout.secrets / "postgres_password"
    target.chmod(0o644)
    target.write_bytes(content)
    proc = checkout.run("--check")
    assert proc.returncode == 1 and "UNUSABLE  postgres_password" in proc.stderr and problem in proc.stderr
    assert MARKER not in proc.stdout + proc.stderr
    assert target.read_bytes() == content


def test_check_refuses_a_directory_other_users_can_enter(checkout: Checkout) -> None:
    assert checkout.run().returncode == 0
    checkout.secrets.chmod(0o711)
    proc = checkout.run("--check")
    assert proc.returncode == 1 and "open to other users" in proc.stderr


@pytest.mark.parametrize("args, status", [(["--help"], 0), (["--force"], 2)])
def test_arguments(checkout: Checkout, args, status: int) -> None:
    proc = checkout.run(*args)
    assert proc.returncode == status
    assert "usage: create_secrets.sh" in (proc.stdout if status == 0 else proc.stderr)
    assert not checkout.secrets.exists()
