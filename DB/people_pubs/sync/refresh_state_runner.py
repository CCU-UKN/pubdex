#!/usr/bin/env python3
"""
people_pubs.sync.refresh_state_runner

Stateful automation runner backed by activity.source_refresh_state.

Runs only stale jobs/entities, based on DB timestamps rather than on a fixed
schedule.

Supports:
- scope_type=person: run a module once per due person_id.
- scope_type=query: run a module once per due scope_key.

Config example (JSON):
{
  "state_table": "activity.source_refresh_state",
  "env": {"PGHOST": "127.0.0.1", "PGPORT": "5432", "PGDATABASE": "people_db", "PGUSER": "postgres"},
  "jobs": [
    {
      "name": "orcid_profiles_person",
      "enabled": true,
      "source": "orcid_profile",
      "scope_type": "person",
      "person_selector_sql": "SELECT io.person_id FROM app.identities_orcid io WHERE io.orcid IS NOT NULL AND io.orcid <> ''",
      "stale_after_months": 3,
      "limit": 20,
      "module": "people_pubs.sync.orcid_profiles",
      "base_args": [],
      "per_person_args": ["--only-person-id", "{person_id}"],
      "dry_run_arg": "--dry-run",
      "timeout_seconds": 7200
    }
  ]
}

Every child invocation is killed after timeout_seconds (default 2h) so one hung
module cannot block the scheduled run forever.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.rows import dict_row

from people_pubs.db.connection import get_conn


LOGGER = logging.getLogger("people_pubs.refresh_state_runner")
RUN_LOG_TABLE = "activity.refresh_run_log"
# Generous ceiling per child invocation; a hung network call inside a module
# must not block the nightly run forever. Override per job via timeout_seconds.
DEFAULT_JOB_TIMEOUT_SECONDS = 7200
TIMEOUT_EXIT_CODE = 124


@dataclass
class StatefulJob:
    name: str
    enabled: bool
    source: str
    scope_type: str
    stale_after_days: Optional[int]
    stale_after_months: Optional[int]
    stale_after_hours: Optional[int]
    limit: int
    module: str
    base_args: List[str]
    per_person_args: List[str]
    scope_key: Optional[str]
    person_selector_sql: Optional[str]
    env: Dict[str, str]
    cwd: Optional[str]
    dry_run_arg: Optional[str]
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS


@dataclass
class RunResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_not_due: int = 0


def _coerce_str_list(value: object, field_name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    out: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{field_name}[{i}] must be a string")
        out.append(item)
    return out


def _coerce_str_dict(value: object, field_name: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object (string->string)")
    out: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"{field_name} keys/values must be strings")
        out[k] = v
    return out


def _coerce_pos_int(value: object, field_name: str, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return value


def _contains_flag(argv: Sequence[str], flag: str) -> bool:
    return any(a == flag for a in argv)


def _compose_env(base: Dict[str, str], extra: Dict[str, str]) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(base)
    env.update(extra)
    return env


def _load_dotenv_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return out
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        out[key] = val
    return out


def _normalize_db_env(env_map: Dict[str, str]) -> Dict[str, str]:
    out = dict(env_map)
    # Compose-style names -> libpq names
    if "PGDATABASE" not in out and out.get("POSTGRES_DB"):
        out["PGDATABASE"] = out["POSTGRES_DB"]
    if "PGUSER" not in out and out.get("POSTGRES_USER"):
        out["PGUSER"] = out["POSTGRES_USER"]
    if "PGPASSWORD" not in out:
        pguser = out.get("PGUSER")
        role_password_vars = {
            "app_readonly": "APP_READONLY_PASSWORD",
            "app_writer": "APP_WRITER_PASSWORD",
            "maintenance": "MAINTENANCE_PASSWORD",
            "postgres": "POSTGRES_PASSWORD",
        }
        password_var = role_password_vars.get(str(pguser or ""))
        if password_var and out.get(password_var):
            out["PGPASSWORD"] = out[password_var]
        elif out.get("POSTGRES_PASSWORD"):
            out["PGPASSWORD"] = out["POSTGRES_PASSWORD"]
    return out


def _dsn_from_env(env_map: Dict[str, str]) -> Optional[str]:
    # Build a libpq-style conninfo string from PG* variables.
    mapping = [
        ("host", "PGHOST"),
        ("port", "PGPORT"),
        ("dbname", "PGDATABASE"),
        ("user", "PGUSER"),
        ("password", "PGPASSWORD"),
        ("sslmode", "PGSSLMODE"),
    ]
    parts: List[str] = []
    for key, env_key in mapping:
        val = env_map.get(env_key) or os.getenv(env_key)
        if val:
            safe = str(val).replace("\\", "\\\\").replace("'", "\\'")
            parts.append(f"{key}='{safe}'")
    return " ".join(parts) if parts else None


def load_config(path: Path) -> Tuple[str, Dict[str, str], List[StatefulJob]]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("Config root must be an object")

    state_table = payload.get("state_table", "activity.source_refresh_state")
    if not isinstance(state_table, str) or not state_table.strip():
        raise ValueError("state_table must be a non-empty string")

    global_env = _coerce_str_dict(payload.get("env"), "env")

    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError("Config must include non-empty 'jobs' list")

    jobs: List[StatefulJob] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw_jobs):
        if not isinstance(item, dict):
            raise ValueError(f"jobs[{idx}] must be an object")

        name = item.get("name")
        source = item.get("source")
        scope_type = item.get("scope_type")
        module = item.get("module")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"jobs[{idx}].name must be a non-empty string")
        if name in seen:
            raise ValueError(f"Duplicate job name: {name}")
        seen.add(name)

        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"jobs[{idx}].source must be a non-empty string")
        if scope_type not in {"person", "query"}:
            raise ValueError(f"jobs[{idx}].scope_type must be one of: person, query")
        if not isinstance(module, str) or not module.strip():
            raise ValueError(f"jobs[{idx}].module must be a non-empty string")

        stale_after_days = _coerce_pos_int(item.get("stale_after_days"), f"jobs[{idx}].stale_after_days")
        stale_after_months = _coerce_pos_int(item.get("stale_after_months"), f"jobs[{idx}].stale_after_months")
        # Hours exist for jobs a daily cron must not skip: with stale_after_days=1
        # a 10:00 cron whose previous run finished 10:05 sees 23h55m elapsed and
        # skips, then drifts. An hours threshold below the cron period avoids that.
        stale_after_hours = _coerce_pos_int(item.get("stale_after_hours"), f"jobs[{idx}].stale_after_hours")
        _set = [v for v in (stale_after_days, stale_after_months, stale_after_hours) if v is not None]
        if len(_set) > 1:
            raise ValueError(
                f"jobs[{idx}] can set only one of stale_after_hours/stale_after_days/stale_after_months"
            )
        if not _set:
            raise ValueError(
                f"jobs[{idx}] must set stale_after_hours, stale_after_days or stale_after_months"
            )

        limit = _coerce_pos_int(item.get("limit"), f"jobs[{idx}].limit", default=20) or 20
        enabled = bool(item.get("enabled", True))
        base_args = _coerce_str_list(item.get("base_args"), f"jobs[{idx}].base_args")
        per_person_args = _coerce_str_list(item.get("per_person_args"), f"jobs[{idx}].per_person_args")
        scope_key = item.get("scope_key")
        if scope_key is not None and not isinstance(scope_key, str):
            raise ValueError(f"jobs[{idx}].scope_key must be a string if set")
        person_selector_sql = item.get("person_selector_sql")
        if person_selector_sql is not None and not isinstance(person_selector_sql, str):
            raise ValueError(f"jobs[{idx}].person_selector_sql must be a string if set")
        env = _coerce_str_dict(item.get("env"), f"jobs[{idx}].env")
        cwd = item.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError(f"jobs[{idx}].cwd must be a string if set")
        dry_run_arg = item.get("dry_run_arg", "--dry-run")
        if dry_run_arg is not None and not isinstance(dry_run_arg, str):
            raise ValueError(f"jobs[{idx}].dry_run_arg must be a string or null")
        timeout_seconds = _coerce_pos_int(
            item.get("timeout_seconds"), f"jobs[{idx}].timeout_seconds"
        ) or DEFAULT_JOB_TIMEOUT_SECONDS

        if scope_type == "query":
            if not scope_key:
                raise ValueError(f"jobs[{idx}] with scope_type=query must set scope_key")
        else:
            if scope_key:
                raise ValueError(f"jobs[{idx}] with scope_type=person must not set scope_key")

        jobs.append(
            StatefulJob(
                name=name,
                enabled=enabled,
                source=source,
                scope_type=scope_type,
                stale_after_days=stale_after_days,
                stale_after_months=stale_after_months,
                stale_after_hours=stale_after_hours,
                limit=limit,
                module=module,
                base_args=base_args,
                per_person_args=per_person_args or ["--only-person-id", "{person_id}"],
                scope_key=scope_key,
                person_selector_sql=person_selector_sql,
                env=env,
                cwd=cwd,
                dry_run_arg=dry_run_arg,
                timeout_seconds=timeout_seconds,
            )
        )

    return state_table, global_env, jobs


def _state_where_clause(job: StatefulJob, person_id: Optional[int] = None) -> Tuple[str, Tuple[object, ...]]:
    if job.scope_type == "person":
        assert person_id is not None
        return "source = %s AND scope_type = 'person' AND person_id = %s", (job.source, person_id)
    assert job.scope_key is not None
    return "source = %s AND scope_type = 'query' AND scope_key = %s", (job.source, job.scope_key)


def _table_exists(conn: psycopg.Connection, qualified_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified_name,))
        row = cur.fetchone()
    if not row:
        return False
    try:
        return bool(row[0])
    except Exception:
        pass
    if isinstance(row, dict):
        for val in row.values():
            return bool(val)
    return False


def _insert_run_log(
    conn: psycopg.Connection,
    *,
    run_log_table: str,
    job: StatefulJob,
    status: str,
    exit_code: int,
    error_msg: Optional[str],
    started_at: datetime,
    finished_at: datetime,
    person_id: Optional[int] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {run_log_table}
              (runner, job_name, source, scope_type, person_id, scope_key, started_at, finished_at, status, exit_code, error_msg)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "refresh_state_runner",
                job.name,
                job.source,
                job.scope_type,
                person_id if job.scope_type == "person" else None,
                job.scope_key if job.scope_type == "query" else None,
                started_at,
                finished_at,
                status,
                exit_code,
                error_msg,
            ),
        )


def _mark_state(
    conn: psycopg.Connection,
    *,
    state_table: str,
    job: StatefulJob,
    success: bool,
    error_msg: Optional[str],
    person_id: Optional[int] = None,
    external_updated_at: Optional[object] = None,
) -> None:
    where_sql, where_params = _state_where_clause(job, person_id=person_id)

    with conn.cursor() as cur:
        if success:
            cur.execute(
                f"""
                UPDATE {state_table}
                   SET last_attempt_at = now(),
                       last_success_at = now(),
                       external_updated_at = COALESCE(%s, external_updated_at),
                       last_error_at = NULL,
                       last_error_msg = NULL,
                       updated_at = now()
                 WHERE {where_sql}
                """,
                (external_updated_at, *where_params),
            )
        else:
            cur.execute(
                f"""
                UPDATE {state_table}
                   SET last_attempt_at = now(),
                       last_error_at = now(),
                       last_error_msg = %s,
                       updated_at = now()
                 WHERE {where_sql}
                """,
                (error_msg or "unknown error", *where_params),
            )
        updated = int(cur.rowcount or 0)

        if updated == 0:
            if job.scope_type == "person":
                assert person_id is not None
                if success:
                    cur.execute(
                        f"""
                        INSERT INTO {state_table}
                          (source, scope_type, person_id, last_attempt_at, last_success_at, external_updated_at, last_error_at, last_error_msg, updated_at)
                        VALUES
                          (%s, 'person', %s, now(), now(), %s, NULL, NULL, now())
                        """,
                        (job.source, person_id, external_updated_at),
                    )
                else:
                    cur.execute(
                        f"""
                        INSERT INTO {state_table}
                          (source, scope_type, person_id, last_attempt_at, last_error_at, last_error_msg, updated_at)
                        VALUES
                          (%s, 'person', %s, now(), now(), %s, now())
                        """,
                        (job.source, person_id, error_msg or "unknown error"),
                    )
            else:
                assert job.scope_key is not None
                if success:
                    cur.execute(
                        f"""
                        INSERT INTO {state_table}
                          (source, scope_type, scope_key, last_attempt_at, last_success_at, external_updated_at, last_error_at, last_error_msg, updated_at)
                        VALUES
                          (%s, 'query', %s, now(), now(), %s, NULL, NULL, now())
                        """,
                        (job.source, job.scope_key, external_updated_at),
                    )
                else:
                    cur.execute(
                        f"""
                        INSERT INTO {state_table}
                          (source, scope_type, scope_key, last_attempt_at, last_error_at, last_error_msg, updated_at)
                        VALUES
                          (%s, 'query', %s, now(), now(), %s, now())
                        """,
                        (job.source, job.scope_key, error_msg or "unknown error"),
                    )


def _lookup_external_updated_at_for_person(
    conn: psycopg.Connection,
    *,
    job: StatefulJob,
    person_id: int,
) -> Optional[object]:
    # Current mapping:
    # - source=orcid_profile -> app.identities_orcid.orcid_last_updated (date).
    if job.source != "orcid_profile":
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT (orcid_last_updated::text || 'T00:00:00+00')::timestamptz AS ext_ts
            FROM app.identities_orcid
            WHERE person_id = %s
              AND orcid_last_updated IS NOT NULL
            """,
            (person_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        return row["ext_ts"]
    except Exception:
        return row[0]


def _stale_interval(job: StatefulJob) -> tuple:
    """Return the (make_interval unit, value) pair for this job's staleness gate.

    The unit name is interpolated into SQL, so it must never come from config
    directly -- only these three literals are reachable.
    """
    if job.stale_after_months is not None:
        return "months", job.stale_after_months
    if job.stale_after_hours is not None:
        return "hours", job.stale_after_hours
    assert job.stale_after_days is not None
    return "days", job.stale_after_days


def _fetch_due_person_ids(conn: psycopg.Connection, *, state_table: str, job: StatefulJob) -> List[int]:
    selector_sql = job.person_selector_sql or "SELECT p.person_id FROM app.people p"

    unit, stale_param = _stale_interval(job)
    stale_sql = (
        "s.last_success_at IS NULL OR s.last_success_at < "
        f"(now() - make_interval({unit} => %s))"
    )

    sql = f"""
        WITH candidates AS (
          {selector_sql}
        )
        SELECT c.person_id
          FROM candidates c
          LEFT JOIN {state_table} s
            ON s.source = %s
           AND s.scope_type = 'person'
           AND s.person_id = c.person_id
         WHERE {stale_sql}
         ORDER BY s.last_success_at NULLS FIRST, c.person_id
         LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (job.source, stale_param, job.limit))
        rows = cur.fetchall()
    out: List[int] = []
    for r in rows:
        try:
            out.append(int(r["person_id"]))  # dict_row
        except Exception:
            out.append(int(r[0]))
    return out


def _is_query_due(conn: psycopg.Connection, *, state_table: str, job: StatefulJob) -> bool:
    assert job.scope_key is not None
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT last_success_at
              FROM {state_table}
             WHERE source = %s
               AND scope_type = 'query'
               AND scope_key = %s
             LIMIT 1
            """,
            (job.source, job.scope_key),
        )
        row = cur.fetchone()

    if row is None:
        return True

    try:
        last_success = row["last_success_at"]
    except Exception:
        last_success = row[0]
    if last_success is None:
        return True

    unit, value = _stale_interval(job)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT (%s::timestamptz < (now() - make_interval({unit} => %s)))",
            (last_success, value),
        )
        due = cur.fetchone()
    try:
        return bool(due[0])
    except Exception:
        return bool(due["?column?"])


def _render_person_args(template_args: Sequence[str], person_id: int) -> List[str]:
    return [a.replace("{person_id}", str(person_id)) for a in template_args]


def _run_subprocess(
    *,
    python_exec: str,
    job: StatefulJob,
    global_env: Dict[str, str],
    force_dry_run: bool,
    extra_args: Sequence[str],
) -> int:
    args = list(job.base_args) + list(extra_args)
    if force_dry_run and job.dry_run_arg and not _contains_flag(args, job.dry_run_arg):
        args.append(job.dry_run_arg)

    cmd = [python_exec, "-m", job.module, *args]
    run_env = _compose_env(global_env, job.env)

    LOGGER.debug("cmd: %s", " ".join(shlex.quote(x) for x in cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=job.cwd or None,
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def _ts() -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

    def _pump(src, dst) -> None:
        assert src is not None
        for raw in src:
            line = raw.rstrip("\n")
            dst.write(f"{_ts()} {line}\n")
            dst.flush()
        src.close()

    t_out = Thread(target=_pump, args=(proc.stdout, sys.stdout), daemon=True)
    t_err = Thread(target=_pump, args=(proc.stderr, sys.stderr), daemon=True)
    t_out.start()
    t_err.start()

    timeout_s = job.timeout_seconds if job.timeout_seconds > 0 else None
    try:
        rc = int(proc.wait(timeout=timeout_s))
    except subprocess.TimeoutExpired:
        LOGGER.error(
            "Job %s exceeded timeout (%ss); terminating pid=%s.",
            job.name, timeout_s, proc.pid,
        )
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            LOGGER.error("Job %s ignored SIGTERM; killing pid=%s.", job.name, proc.pid)
            proc.kill()
            proc.wait()
        rc = TIMEOUT_EXIT_CODE
    t_out.join()
    t_err.join()
    return rc


def _acquire_lock(conn: psycopg.Connection, key: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        row = cur.fetchone()
    try:
        return bool(row[0])
    except Exception:
        return bool(row["pg_try_advisory_lock"])


def _release_lock(conn: psycopg.Connection, key: int) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (key,))


def run_stateful_job(
    *,
    conn: psycopg.Connection,
    state_table: str,
    job: StatefulJob,
    python_exec: str,
    force_dry_run: bool,
    global_env: Dict[str, str],
    stop_on_error: bool,
    run_log_enabled: bool,
    run_log_table: str,
) -> RunResult:
    result = RunResult()
    LOGGER.info("Running stateful job=%s source=%s scope=%s", job.name, job.source, job.scope_type)

    if job.scope_type == "person":
        person_ids = _fetch_due_person_ids(conn, state_table=state_table, job=job)
        if not person_ids:
            LOGGER.info("No due people for job=%s", job.name)
            return result

        for person_id in person_ids:
            result.processed += 1
            extra_args = _render_person_args(job.per_person_args, person_id)
            started_at = datetime.now().astimezone()
            rc = _run_subprocess(
                python_exec=python_exec,
                job=job,
                global_env=global_env,
                force_dry_run=force_dry_run,
                extra_args=extra_args,
            )
            finished_at = datetime.now().astimezone()
            if rc == 0:
                result.succeeded += 1
                if not force_dry_run:
                    if run_log_enabled:
                        _insert_run_log(
                            conn,
                            run_log_table=run_log_table,
                            job=job,
                            status="success",
                            exit_code=rc,
                            error_msg=None,
                            started_at=started_at,
                            finished_at=finished_at,
                            person_id=person_id,
                        )
                    external_updated_at = _lookup_external_updated_at_for_person(
                        conn,
                        job=job,
                        person_id=person_id,
                    )
                    _mark_state(
                        conn,
                        state_table=state_table,
                        job=job,
                        success=True,
                        error_msg=None,
                        person_id=person_id,
                        external_updated_at=external_updated_at,
                    )
                    conn.commit()
            else:
                result.failed += 1
                msg = f"subprocess exited {rc}"
                LOGGER.error("Job=%s person_id=%s failed: %s", job.name, person_id, msg)
                if not force_dry_run:
                    if run_log_enabled:
                        _insert_run_log(
                            conn,
                            run_log_table=run_log_table,
                            job=job,
                            status="failed",
                            exit_code=rc,
                            error_msg=msg,
                            started_at=started_at,
                            finished_at=finished_at,
                            person_id=person_id,
                        )
                    _mark_state(
                        conn,
                        state_table=state_table,
                        job=job,
                        success=False,
                        error_msg=msg,
                        person_id=person_id,
                    )
                    conn.commit()
                if stop_on_error:
                    break
        return result

    due = _is_query_due(conn, state_table=state_table, job=job)
    if not due:
        result.skipped_not_due += 1
        LOGGER.info("Query scope not due for job=%s", job.name)
        return result

    result.processed += 1
    started_at = datetime.now().astimezone()
    rc = _run_subprocess(
        python_exec=python_exec,
        job=job,
        global_env=global_env,
        force_dry_run=force_dry_run,
        extra_args=[],
    )
    finished_at = datetime.now().astimezone()
    if rc == 0:
        result.succeeded += 1
        if not force_dry_run:
            if run_log_enabled:
                _insert_run_log(
                    conn,
                    run_log_table=run_log_table,
                    job=job,
                    status="success",
                    exit_code=rc,
                    error_msg=None,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            _mark_state(
                conn,
                state_table=state_table,
                job=job,
                success=True,
                error_msg=None,
                external_updated_at=None,
            )
            conn.commit()
    else:
        result.failed += 1
        msg = f"subprocess exited {rc}"
        LOGGER.error("Job=%s query scope failed: %s", job.name, msg)
        if not force_dry_run:
            if run_log_enabled:
                _insert_run_log(
                    conn,
                    run_log_table=run_log_table,
                    job=job,
                    status="failed",
                    exit_code=rc,
                    error_msg=msg,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            _mark_state(conn, state_table=state_table, job=job, success=False, error_msg=msg)
            conn.commit()
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run stale-only automation jobs using activity.source_refresh_state.")
    p.add_argument("--config", default="refresh_jobs.json", help="Path to refresh jobs JSON config.")
    p.add_argument("--dsn", default=None, help="Optional PostgreSQL DSN for state table lookups.")
    p.add_argument("--only", action="append", default=[], help="Run only this job name (repeatable).")
    p.add_argument("--skip", action="append", default=[], help="Skip this job name (repeatable).")
    p.add_argument("--list", action="store_true", help="List configured jobs and exit.")
    p.add_argument("--dry-run", action="store_true", help="Do not update state table; forward dry-run flags.")
    p.add_argument("--stop-on-error", action="store_true", help="Stop when first job item fails.")
    p.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run job modules (default: current interpreter).",
    )
    p.add_argument(
        "--lock-key",
        type=int,
        default=94832173,
        help="Postgres advisory lock key to avoid overlapping runs (default: 94832173).",
    )
    p.add_argument("--no-lock", action="store_true", help="Disable advisory lock.")
    p.add_argument(
        "--connect-retries",
        type=int,
        default=5,
        help="Number of DB connect attempts before giving up (default: 5).",
    )
    p.add_argument(
        "--connect-retry-delay",
        type=float,
        default=5.0,
        help="Seconds to wait between DB connect retries (default: 5.0).",
    )
    p.add_argument("--debug", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def _open_conn_with_retry(
    dsn: Optional[str],
    attempts: int,
    delay_seconds: float,
) -> psycopg.Connection:
    attempts = max(int(attempts), 1)
    delay_seconds = max(float(delay_seconds), 0.0)
    last_exc: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            return get_conn(dsn)
        except psycopg.OperationalError as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            LOGGER.warning(
                "DB connect failed (attempt %s/%s): %s; retrying in %.1fs",
                attempt,
                attempts,
                exc,
                delay_seconds,
            )
            time.sleep(delay_seconds)

    assert last_exc is not None
    raise last_exc


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}")

    state_table, global_env, jobs = load_config(config_path)

    # Allow cron runs to work without exporting PG* vars explicitly:
    # read DB/.env (same directory as refresh_jobs.json) and map POSTGRES_* -> PG*.
    dotenv_env = _load_dotenv_file(config_path.parent / ".env")
    merged_env = dict(dotenv_env)
    merged_env.update(global_env)
    global_env = _normalize_db_env(merged_env)

    if args.list:
        for j in jobs:
            status = "enabled" if j.enabled else "disabled"
            print(f"{j.name}\t{status}\t{j.scope_type}\t{j.source}\t{j.module}")
        return

    only = set(args.only or [])
    skip = set(args.skip or [])
    selected = [
        j for j in jobs
        if j.enabled and (not only or j.name in only) and j.name not in skip
    ]
    if not selected:
        LOGGER.info("No jobs selected.")
        return

    state_dsn = args.dsn or _dsn_from_env(global_env)
    conn = _open_conn_with_retry(
        state_dsn,
        attempts=args.connect_retries,
        delay_seconds=args.connect_retry_delay,
    )
    try:
        conn.row_factory = dict_row

        lock_acquired = True
        if not args.no_lock:
            lock_acquired = _acquire_lock(conn, int(args.lock_key))
            if not lock_acquired:
                LOGGER.warning("Another runner holds advisory lock %s; exiting.", args.lock_key)
                return

        run_log_enabled = _table_exists(conn, RUN_LOG_TABLE)
        if not run_log_enabled:
            LOGGER.debug("%s not found; run history logging disabled.", RUN_LOG_TABLE)

        try:
            total = RunResult()
            for job in selected:
                try:
                    stats = run_stateful_job(
                        conn=conn,
                        state_table=state_table,
                        job=job,
                        python_exec=args.python,
                        force_dry_run=bool(args.dry_run),
                        global_env=global_env,
                        stop_on_error=bool(args.stop_on_error),
                        run_log_enabled=run_log_enabled,
                        run_log_table=RUN_LOG_TABLE,
                    )
                except Exception:
                    # One job's unhandled error must not skip the remaining
                    # jobs or leave the shared connection in an aborted
                    # transaction. Roll back, count the failure, move on.
                    LOGGER.exception(
                        "Job %s raised an unhandled error; rolling back and continuing.",
                        job.name,
                    )
                    try:
                        conn.rollback()
                    except Exception:
                        LOGGER.exception(
                            "Rollback after job %s failed; connection unusable, aborting run.",
                            job.name,
                        )
                        raise
                    total.processed += 1
                    total.failed += 1
                    if args.stop_on_error:
                        break
                    continue
                total.processed += stats.processed
                total.succeeded += stats.succeeded
                total.failed += stats.failed
                total.skipped_not_due += stats.skipped_not_due

                if args.stop_on_error and stats.failed > 0:
                    break

            LOGGER.info(
                "refresh_state_runner summary: processed=%s succeeded=%s failed=%s skipped_not_due=%s",
                total.processed,
                total.succeeded,
                total.failed,
                total.skipped_not_due,
            )

            if total.failed > 0:
                raise SystemExit(f"{total.failed} job item(s) failed.")
        finally:
            if not args.no_lock and lock_acquired:
                _release_lock(conn, int(args.lock_key))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
