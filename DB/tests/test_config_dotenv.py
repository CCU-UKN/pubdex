"""people_pubs.config and its PEOPLE_PUBS_SKIP_DOTENV opt-out, offline.

The loader only ever sees .env files in a temporary directory: the unit tests
hand it that file explicitly, and the import test sets the opt-out, so no test
reads the repository's own DB/.env.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from people_pubs import config

DB_DIR = Path(__file__).resolve().parents[1]
PROBES = ("DOTENV_PROBE", "PGHOST")


@pytest.fixture
def environ(monkeypatch):
    """A private copy of os.environ for the loader to fill."""
    copy = {key: value for key, value in os.environ.items()
            if key not in PROBES and key != "PEOPLE_PUBS_SKIP_DOTENV"}
    monkeypatch.setattr(os, "environ", copy)
    return copy


def _dotenv(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("DOTENV_PROBE=from-file\nPGHOST=from-file\n", encoding="utf-8")
    return path


def test_a_dotenv_file_fills_only_missing_values(tmp_path: Path, environ: dict) -> None:
    environ["PGHOST"] = "from-environment"
    config._load_local_env([_dotenv(tmp_path)])
    assert environ["DOTENV_PROBE"] == "from-file"
    assert environ["PGHOST"] == "from-environment", "the real environment wins"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_the_opt_out_reads_no_dotenv_file(tmp_path: Path, environ: dict, value: str) -> None:
    environ["PEOPLE_PUBS_SKIP_DOTENV"] = value
    assert config.dotenv_disabled()
    config._load_local_env([_dotenv(tmp_path)])
    assert "DOTENV_PROBE" not in environ and "PGHOST" not in environ


@pytest.mark.parametrize("value", ["0", "", "no", "off"])
def test_other_values_leave_the_dotenv_file_in_use(tmp_path: Path, environ: dict, value: str) -> None:
    environ["PEOPLE_PUBS_SKIP_DOTENV"] = value
    assert not config.dotenv_disabled()
    config._load_local_env([_dotenv(tmp_path)])
    assert environ["DOTENV_PROBE"] == "from-file"


def test_importing_with_the_opt_out_reads_no_dotenv_file(tmp_path: Path) -> None:
    # A .env in the working directory is one of the loader's two candidates;
    # the opt-out also keeps it away from DB/.env, the other one.
    _dotenv(tmp_path)
    code = (
        "import json, os, people_pubs.config; "
        "print(json.dumps({key: os.environ.get(key) for key in ('DOTENV_PROBE', 'PGHOST')}))"
    )
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(DB_DIR), "PEOPLE_PUBS_SKIP_DOTENV": "1"}
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"DOTENV_PROBE": None, "PGHOST": None}
