"""Guardrail tests for DB/refresh_jobs.json.

These assert that every *broad discovery* search job (funding/affiliation queries
that can otherwise ingest hundreds/thousands of records) is bounded: it caps new
records, skips existing DOIs, and client-side post-filters affiliation matches so
an "any word" API result set cannot be ingested wholesale. Removing a cap or
post-filter from refresh_jobs.json fails CI.

The shipped configuration schedules no discovery job, so the parametrized
checks below skip; the synthetic probes further down execute the same checks
on constructed jobs, with and without filtering.

Background and the portal-by-portal semantics live in DB/INGEST_SEARCH_SEMANTICS.md.
Pure/offline: this only parses the config and the searchers' argument parsers,
no network or DB access.
"""
from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path
from typing import Optional

import pytest

from people_pubs.sync.estimate_search_jobs import (
    JobEstimate,
    _DISCOVERY_MODULES,
    _is_discovery_job,
    _short_source,
    _verdict,
)
from people_pubs.sync.refresh_state_runner import StatefulJob, load_config

CONFIG_PATH = Path(__file__).resolve().parents[1] / "refresh_jobs.json"

# No discovery job should ever be allowed to ingest more than this many new
# records per run. Current jobs use 10-50; the ceiling is a generous backstop.
MAX_NEW_RECORDS_CEILING = 200

# Affiliation search is an "any word" match on every supported portal (Crossref
# query.affiliation can match over a million records for a short institute
# name), so an affiliation discovery job MUST carry a working client-side
# post-filter. Only these searchers implement one
# (test_post_filter_modules_match_the_searcher_clis keeps this list honest); an
# affiliation discovery job on any other module must not be scheduled.
# See DB/INGEST_SEARCH_SEMANTICS.md.
_POST_FILTER_MODULES = {"people_pubs.sync.crossref_search"}


def _load_jobs() -> list[StatefulJob]:
    _state_table, _env, jobs = load_config(CONFIG_PATH)
    return jobs


ALL_JOBS = _load_jobs()
DISCOVERY_JOBS = [j for j in ALL_JOBS if _is_discovery_job(j)]
BACKFILL_JOBS = [j for j in ALL_JOBS if j.module.endswith("_backfill") or j.module.endswith(".backfill_all")]


def _arg_value(argv, flag):
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _is_affiliation_job(job: StatefulJob) -> bool:
    return "--affiliation" in job.base_args or "affiliation" in (job.scope_key or "")


def _affiliation_post_filter_problem(job: StatefulJob) -> Optional[str]:
    """Why an affiliation discovery job is unsafe to schedule, or None."""
    if job.module not in _POST_FILTER_MODULES:
        return (
            f"{job.name}: {job.module} has no client-side affiliation post-filter, so an "
            "affiliation discovery job on it must not be scheduled"
        )
    if "--require-affiliation-match" not in job.base_args:
        return (
            f"{job.name}: affiliation discovery on {job.module} uses an 'any word' API match and must pass "
            "--require-affiliation-match, else the entire match set is ingested wholesale"
        )
    return None


def _probe(module: str, base_args: list[str]) -> StatefulJob:
    return replace(
        ALL_JOBS[0],
        name="probe",
        scope_type="query",
        module=module,
        base_args=base_args,
    )


def test_config_parses() -> None:
    assert ALL_JOBS, "refresh_jobs.json defines no jobs"


def test_discovery_detection_still_works() -> None:
    # The shipped configuration carries no broad discovery job, so the
    # parametrized guardrails below would be vacuous without this check:
    # prove the detector still recognises one, or a future discovery job
    # could be added and silently skip every cap assertion.
    probe = _probe("people_pubs.sync.crossref_search", ["--affiliation", "Example Institute"])
    assert _is_discovery_job(probe), "discovery-job detection is broken"


@pytest.mark.parametrize("job", DISCOVERY_JOBS, ids=lambda j: j.name)
def test_discovery_job_caps_new_records(job: StatefulJob) -> None:
    raw = _arg_value(job.base_args, "--max-new-records")
    assert raw is not None, f"{job.name}: missing --max-new-records (unbounded discovery ingest)"
    assert raw.isdigit(), f"{job.name}: --max-new-records must be an integer, got {raw!r}"
    assert 0 < int(raw) <= MAX_NEW_RECORDS_CEILING, (
        f"{job.name}: --max-new-records={raw} outside (0, {MAX_NEW_RECORDS_CEILING}]"
    )


@pytest.mark.parametrize("job", DISCOVERY_JOBS, ids=lambda j: j.name)
def test_discovery_job_skips_existing(job: StatefulJob) -> None:
    assert "--skip-existing" in job.base_args, (
        f"{job.name}: missing --skip-existing (would re-touch known DOIs every run)"
    )


@pytest.mark.parametrize("job", DISCOVERY_JOBS, ids=lambda j: j.name)
def test_affiliation_job_has_post_filter(job: StatefulJob) -> None:
    if not _is_affiliation_job(job):
        pytest.skip("not an affiliation job")
    problem = _affiliation_post_filter_problem(job)
    assert problem is None, problem


# --------------------------------------------------------------------------- #
# Synthetic probes: the parametrized checks above run on no shipped job, so
# these execute the same checks on constructed jobs.
# --------------------------------------------------------------------------- #
def test_post_filter_check_accepts_filtered_crossref_affiliation_job() -> None:
    probe = _probe(
        "people_pubs.sync.crossref_search",
        ["--affiliation", "Example Institute", "--require-affiliation-match",
         "--max-new-records", "50", "--skip-existing"],
    )
    assert _is_discovery_job(probe) and _is_affiliation_job(probe)
    assert _affiliation_post_filter_problem(probe) is None


def test_post_filter_check_rejects_unfiltered_crossref_affiliation_job() -> None:
    probe = _probe(
        "people_pubs.sync.crossref_search",
        ["--affiliation", "Example Institute", "--max-new-records", "50", "--skip-existing"],
    )
    assert _is_discovery_job(probe) and _is_affiliation_job(probe)
    problem = _affiliation_post_filter_problem(probe)
    assert problem is not None and "--require-affiliation-match" in problem


@pytest.mark.parametrize("module", sorted(_DISCOVERY_MODULES - _POST_FILTER_MODULES))
def test_post_filter_check_rejects_affiliation_jobs_without_a_filter(module: str) -> None:
    # These searchers accept --affiliation but implement no post-filter, so
    # an affiliation discovery job on them is never acceptable.
    probe = _probe(
        module,
        ["--affiliation", "Example Institute", "--max-new-records", "50", "--skip-existing"],
    )
    assert _is_discovery_job(probe) and _is_affiliation_job(probe)
    problem = _affiliation_post_filter_problem(probe)
    assert problem is not None and "must not be scheduled" in problem


@pytest.mark.parametrize("module_name", sorted(_DISCOVERY_MODULES))
def test_post_filter_modules_match_the_searcher_clis(module_name: str) -> None:
    # _POST_FILTER_MODULES must name exactly the searchers whose CLI accepts
    # --require-affiliation-match: no module is credited with a flag it lacks.
    module = importlib.import_module(module_name)
    base = ["--affiliation", "Example Institute"]
    module.parse_args(base)  # every discovery searcher accepts --affiliation
    if module_name in _POST_FILTER_MODULES:
        assert module.parse_args(base + ["--require-affiliation-match"]).require_affiliation_match is True
    else:
        with pytest.raises(SystemExit):
            module.parse_args(base + ["--require-affiliation-match"])


def _estimate(**overrides) -> JobEstimate:
    fields = dict(
        name="probe",
        source="crossref",
        module="people_pubs.sync.crossref_search",
        total=10,
        max_new_records=50,
        is_affiliation=True,
        has_post_filter=True,
        verdict="",
    )
    fields.update(overrides)
    return JobEstimate(**fields)


def test_estimator_accepts_capped_and_filtered_affiliation_job() -> None:
    assert _verdict(10, _estimate(), threshold=500) == "OK"
    # A large raw match is fine because the cap and post-filter bound it.
    assert _verdict(5000, _estimate(), threshold=500) == "OK (bounded)"


def test_estimator_warns_on_affiliation_job_without_post_filter() -> None:
    verdict = _verdict(10, _estimate(has_post_filter=False), threshold=500)
    assert verdict.startswith("WARN")
    assert "without --require-affiliation-match" in verdict


def test_estimator_exempts_no_affiliation_source() -> None:
    # No discovery source is exempt from the post-filter requirement.
    for module_name in sorted(_DISCOVERY_MODULES):
        verdict = _verdict(
            10,
            _estimate(source=_short_source(module_name), module=module_name, has_post_filter=False),
            threshold=500,
        )
        assert verdict.startswith("WARN"), f"{module_name}: {verdict}"


def test_estimator_needs_no_post_filter_for_non_affiliation_job() -> None:
    assert _verdict(10, _estimate(is_affiliation=False, has_post_filter=False), threshold=500) == "OK"
    verdict = _verdict(10, _estimate(is_affiliation=False, max_new_records=None), threshold=500)
    assert verdict == "WARN: no --max-new-records cap"


@pytest.mark.parametrize("job", BACKFILL_JOBS, ids=lambda j: j.name)
def test_backfill_job_is_bounded(job: StatefulJob) -> None:
    raw = _arg_value(job.base_args, "--max")
    assert raw is not None and raw.isdigit(), f"{job.name}: backfill missing bounded --max"
    assert int(raw) > 0, f"{job.name}: --max must be > 0"
