"""backfill_all runs Crossref, DataCite and Semantic Scholar by default, and
OpenAlex and DBLP only on explicit opt-in.

The tests drive the real command line (parse_args and main) with every
connector replaced by a recorder, so the flag handling and the exact call
sequence are checked end to end, and every scheduled orchestrator job must
still parse. Offline: no network and no database.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from people_pubs.sync import backfill_all

DB_DIR = Path(__file__).resolve().parents[1]

CONNECTORS = (
    "run_crossref_backfill",
    "run_datacite_backfill",
    "run_semantic_backfill",
    "run_openalex_backfill",
    "run_dblp_backfill",
)
STANDARD_SEQUENCE = [
    "run_crossref_backfill",
    "run_datacite_backfill",
    "run_semantic_backfill",
]


class _Stats:
    def __init__(self) -> None:
        self.failed_pub_ids: set[int] = set()


def _stub_connectors(monkeypatch) -> list[str]:
    """Replace every connector entry point with a recorder. No network."""
    called: list[str] = []
    for name in CONNECTORS:
        monkeypatch.setattr(
            backfill_all,
            name,
            lambda *_args, _name=name, **_kwargs: (called.append(_name), _Stats())[1],
        )
    return called


def _run_main(monkeypatch, *extra: str) -> list[str]:
    called = _stub_connectors(monkeypatch)
    backfill_all.main(
        [
            "--max", "1",
            "--only-pub-id", "1",
            "--dry-run",
            "--semantic-api-key", "dummy-value-never-used",
            *extra,
        ]
    )
    return called


def test_defaults_leave_openalex_and_dblp_off() -> None:
    args = backfill_all.parse_args(["--max", "1"])
    assert args.with_openalex is False
    assert args.with_dblp is False
    # Opt-in sources have no --skip-* flag whose absence could enable them.
    assert not hasattr(args, "skip_openalex")
    assert not hasattr(args, "skip_dblp")


def test_default_run_calls_exactly_the_standard_set(monkeypatch) -> None:
    assert _run_main(monkeypatch) == STANDARD_SEQUENCE


def test_with_openalex_adds_openalex_and_not_dblp(monkeypatch) -> None:
    assert _run_main(monkeypatch, "--with-openalex") == STANDARD_SEQUENCE + ["run_openalex_backfill"]


def test_with_dblp_adds_dblp_and_not_openalex(monkeypatch) -> None:
    assert _run_main(monkeypatch, "--with-dblp") == STANDARD_SEQUENCE + ["run_dblp_backfill"]


def test_skip_flags_remove_standard_sources(monkeypatch) -> None:
    called = _run_main(monkeypatch, "--skip-datacite", "--skip-semantic")
    assert called == ["run_crossref_backfill"]


def test_semantic_scholar_without_api_key_fails_loudly(monkeypatch) -> None:
    called = _stub_connectors(monkeypatch)
    with pytest.raises(SystemExit):
        backfill_all.main(
            ["--max", "1", "--only-pub-id", "1", "--dry-run", "--semantic-api-key", ""]
        )
    assert "run_semantic_backfill" not in called


def test_scheduled_backfill_all_jobs_parse() -> None:
    config = json.loads((DB_DIR / "refresh_jobs.json").read_text(encoding="utf-8"))
    jobs = [job for job in config["jobs"] if job["module"] == "people_pubs.sync.backfill_all"]
    assert jobs, "expected at least one scheduled backfill_all job"
    for job in jobs:
        argv = list(job["base_args"])
        if job.get("dry_run_arg"):
            argv.append(job["dry_run_arg"])
        backfill_all.parse_args(argv)  # an unknown option exits with status 2
