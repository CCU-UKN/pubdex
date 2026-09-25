#!/usr/bin/env python3
"""
people_pubs.sync.estimate_search_jobs

Operator preflight: for every broad *discovery* search job configured in
refresh_jobs.json (funding/affiliation queries against Crossref, OpenAlex,
DataCite, Semantic Scholar or DBLP), run the underlying module with --estimate
and report how many records the query would match -- WITHOUT ingesting anything.

Use this before enabling or widening a discovery job to make sure an "exact"
search has not silently become an "any word" search that would ingest hundreds
or thousands of irrelevant papers. See DB/INGEST_SEARCH_SEMANTICS.md.

Only the Crossref searcher implements --estimate and the affiliation
post-filter; a job on any other discovery module is reported as ERROR or WARN,
which is the intended outcome: those modes are not scheduled.

Example (run from DB/):
  PYTHONPATH=. ../.venv/bin/python -m people_pubs.sync.estimate_search_jobs
  PYTHONPATH=. ../.venv/bin/python -m people_pubs.sync.estimate_search_jobs --only <job name>

Reads API keys / mailto the same way the scheduled runner does: DB/.env (next to
refresh_jobs.json) plus the process environment.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# Allow running as a standalone script from DB/ or the repo root without installing.
_PKG_ROOT = Path(__file__).resolve().parents[2]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from people_pubs.sync.refresh_state_runner import (
    StatefulJob,
    load_config,
    _compose_env,
    _load_dotenv_file,
    _normalize_db_env,
)


LOGGER = logging.getLogger("people_pubs.estimate_search_jobs")

# Modules that perform broad discovery (query-scope) searches. Their *_backfill
# siblings only enrich already-known DOIs, so they are intentionally excluded.
_DISCOVERY_MODULES = {
    "people_pubs.sync.crossref_search",
    "people_pubs.sync.datacite_search",
    "people_pubs.sync.openalex_search",
    "people_pubs.sync.semantic_scholar_search",
    "people_pubs.sync.dblp_search",
}

# Any ESTIMATE line printed by a searcher: "... : <N> matching records ...".
_ESTIMATE_RE = re.compile(r"ESTIMATE\s+(\w+).*?:\s*([0-9]+|unknown)\s+matching records", re.IGNORECASE)


@dataclass
class JobEstimate:
    name: str
    source: str
    module: str
    total: Optional[int]
    max_new_records: Optional[int]
    is_affiliation: bool
    has_post_filter: bool
    verdict: str
    error: Optional[str] = None


def _is_discovery_job(job: StatefulJob) -> bool:
    if job.scope_type != "query":
        return False
    if job.module not in _DISCOVERY_MODULES:
        return False
    # A discovery job carries a real query constraint in base_args.
    flags = set(job.base_args)
    return bool(flags & {"--affiliation", "--award", "--funder", "--query"})


def _arg_value(argv: Sequence[str], flag: str) -> Optional[str]:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _short_source(module: str) -> str:
    return module.rsplit(".", 1)[-1].replace("_search", "").replace("_download", "")


def _parse_total(stdout: str) -> Optional[int]:
    """Prefer a 'TOTAL' line (Crossref sums multiple queries), else the max seen."""
    totals: List[int] = []
    total_line_value: Optional[int] = None
    for line in stdout.splitlines():
        m = _ESTIMATE_RE.search(line)
        if not m:
            continue
        raw = m.group(2)
        if raw.lower() == "unknown":
            continue
        val = int(raw)
        if "TOTAL" in line:
            total_line_value = val
        else:
            totals.append(val)
    if total_line_value is not None:
        return total_line_value
    if totals:
        return max(totals)
    return None


def _verdict(total: Optional[int], job: JobEstimate, threshold: int) -> str:
    if job.error:
        return "ERROR"
    reasons: List[str] = []
    if job.max_new_records is None:
        reasons.append("no --max-new-records cap")
    # Every affiliation discovery search is "any word" and must post-filter.
    # See DB/INGEST_SEARCH_SEMANTICS.md.
    if job.is_affiliation and not job.has_post_filter:
        reasons.append("any-word affiliation query without --require-affiliation-match")
    if total is not None and total >= threshold and not reasons:
        # Large raw match is fine *because* it is bounded by the cap/post-filter.
        return "OK (bounded)"
    if reasons:
        return "WARN: " + "; ".join(reasons)
    return "OK"


def _run_estimate(
    job: StatefulJob,
    *,
    python_exec: str,
    env: Dict[str, str],
    cwd: Optional[str],
    timeout: int,
) -> JobEstimate:
    source = _short_source(job.module)
    max_new = _arg_value(job.base_args, "--max-new-records")
    is_aff = "--affiliation" in job.base_args
    has_pf = "--require-affiliation-match" in job.base_args
    est = JobEstimate(
        name=job.name,
        source=source,
        module=job.module,
        total=None,
        max_new_records=int(max_new) if max_new and max_new.isdigit() else None,
        is_affiliation=is_aff,
        has_post_filter=has_pf,
        verdict="",
    )

    cmd = [python_exec, "-m", job.module, *job.base_args, "--estimate"]
    LOGGER.debug("estimate cmd: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        est.error = f"timeout after {timeout}s"
        est.verdict = "ERROR"
        return est

    output = proc.stdout or ""
    est.total = _parse_total(output)
    if proc.returncode != 0 and est.total is None:
        tail = output.strip().splitlines()[-1] if output.strip() else "(no output)"
        est.error = f"exit {proc.returncode}: {tail}"
    return est


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Preflight discovery search jobs: report how many records each would match, no ingestion."
    )
    p.add_argument("--config", default="refresh_jobs.json", help="Path to refresh jobs JSON config.")
    p.add_argument("--only", action="append", default=[], help="Estimate only this job name (repeatable).")
    p.add_argument(
        "--threshold",
        type=int,
        default=500,
        help="Raw-match count above which a bounded job is annotated 'OK (bounded)' (default: 500).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Per-job subprocess timeout in seconds (default: 120).",
    )
    p.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run job modules (default: current interpreter).",
    )
    p.add_argument("--debug", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}")

    _state_table, global_env, jobs = load_config(config_path)

    # Same env resolution as the scheduled runner: DB/.env (next to the config)
    # supplies API keys / mailto; explicit config env overrides it.
    dotenv_env = _load_dotenv_file(config_path.parent / ".env")
    merged_env = dict(dotenv_env)
    merged_env.update(global_env)
    resolved = _normalize_db_env(merged_env)
    child_env = _compose_env(resolved, {})
    # Ensure child `python -m` invocations can import people_pubs.
    pkg_root = str(config_path.parent)
    existing_pp = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = pkg_root + (os.pathsep + existing_pp if existing_pp else "")

    only = set(args.only or [])
    selected = [j for j in jobs if j.enabled and _is_discovery_job(j) and (not only or j.name in only)]
    if not selected:
        LOGGER.info("No discovery jobs selected (nothing to estimate).")
        return

    results: List[JobEstimate] = []
    for job in selected:
        LOGGER.info("Estimating %s (%s) ...", job.name, _short_source(job.module))
        est = _run_estimate(
            job,
            python_exec=args.python,
            env=child_env,
            cwd=str(config_path.parent),
            timeout=args.timeout,
        )
        est.verdict = _verdict(est.total, est, args.threshold)
        results.append(est)

    # Summary table.
    print("")
    print(f"{'JOB':40}  {'SRC':9}  {'RAW MATCH':>10}  {'CAP':>6}  {'PF':>3}  VERDICT")
    print("-" * 100)
    warned = 0
    for r in results:
        raw = "err" if r.error else ("?" if r.total is None else str(r.total))
        cap = "-" if r.max_new_records is None else str(r.max_new_records)
        pf = "y" if r.has_post_filter else ("-" if not r.is_affiliation else "n")
        if r.verdict.startswith("WARN") or r.verdict == "ERROR":
            warned += 1
        print(f"{r.name:40.40}  {r.source:9}  {raw:>10}  {cap:>6}  {pf:>3}  {r.verdict}")
        if r.error:
            print(f"    -> {r.error}")
    print("")
    print(
        "RAW MATCH = raw API match count before the job's post-filter/cap. A large bounded number is fine; "
        "an unbounded or unfiltered affiliation query is the risk. See DB/INGEST_SEARCH_SEMANTICS.md."
    )
    if warned:
        raise SystemExit(f"{warned} job(s) flagged WARN/ERROR.")


if __name__ == "__main__":
    main()
