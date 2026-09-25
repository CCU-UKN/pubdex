#!/usr/bin/env python3
"""
Batch-run manual ORCID/person/publication refresh commands from a CSV.

Expected CSV columns:
  - pub_id
  - paper_orcid
  - display_name_at_pub

For each unique ORCID, runs (by default):
  1) add_people
  2) orcid_profiles
  3) orcid_works
  4) crossref_search

For each unique pub_id (optional), runs:
  5) crossref_backfill --force --refresh-authorships --only-pub-id <pub_id>
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from people_pubs.utils.identifiers import normalize_orcid




@dataclass
class CsvRow:
    pub_id: Optional[int]
    orcid: Optional[str]
    display_name: Optional[str]
    line_no: int


def _normalize_orcid(raw: str) -> Optional[str]:
    return normalize_orcid(raw)


def _clean(value: object) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _read_csv(path: Path) -> Tuple[List[CsvRow], List[str]]:
    warnings: List[str] = []
    rows: List[CsvRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SystemExit(f"CSV has no header: {path}")
        required = {"paper_orcid"}
        missing = [k for k in required if k not in set(reader.fieldnames)]
        if missing:
            raise SystemExit(f"CSV is missing required columns: {', '.join(missing)}")

        for idx, raw in enumerate(reader, start=2):
            orcid = _normalize_orcid(raw.get("paper_orcid", ""))
            display_name = _clean(raw.get("display_name_at_pub"))

            pub_id_raw = _clean(raw.get("pub_id"))
            pub_id: Optional[int] = None
            if pub_id_raw:
                try:
                    pub_id = int(pub_id_raw)
                except ValueError:
                    warnings.append(f"line {idx}: invalid pub_id={pub_id_raw!r}; ignoring pub_id")

            if not orcid:
                warnings.append(f"line {idx}: missing/invalid paper_orcid; skipping row")
                continue

            rows.append(CsvRow(pub_id=pub_id, orcid=orcid, display_name=display_name, line_no=idx))
    return rows, warnings


def _dedupe_inputs(rows: Sequence[CsvRow]) -> Tuple[List[Tuple[str, Optional[str]]], List[int]]:
    # Keep insertion order.
    by_orcid: Dict[str, Optional[str]] = {}
    pub_ids: List[int] = []
    seen_pub_ids: set[int] = set()

    for row in rows:
        if row.orcid is None:
            continue
        if row.orcid not in by_orcid:
            by_orcid[row.orcid] = row.display_name
        elif (not by_orcid[row.orcid]) and row.display_name:
            by_orcid[row.orcid] = row.display_name

        if row.pub_id is not None and row.pub_id not in seen_pub_ids:
            seen_pub_ids.add(row.pub_id)
            pub_ids.append(row.pub_id)

    orcid_items = [(orcid, name) for orcid, name in by_orcid.items()]
    return orcid_items, pub_ids


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


def _compose_env(dotenv_path: Optional[Path]) -> Dict[str, str]:
    env = os.environ.copy()
    if dotenv_path:
        from_file = _load_dotenv_file(dotenv_path)
        # Keep explicit shell exports as higher priority.
        for k, v in from_file.items():
            if k not in env:
                env[k] = v
    return env


def _run_cmd(
    cmd: List[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    execute: bool,
) -> int:
    printable = " ".join(shlex.quote(c) for c in cmd)
    print(f"\n>>> {printable}")
    if not execute:
        return 0
    proc = subprocess.run(cmd, cwd=str(cwd), env=env)
    return int(proc.returncode)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch-run add_people/orcid_profiles/orcid_works/crossref_search and optional crossref_backfill from CSV."
    )
    p.add_argument("--csv", required=True, help="CSV path with columns: pub_id,paper_orcid,display_name_at_pub")
    p.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to run module commands (default: current interpreter).",
    )
    p.add_argument("--dsn", help="Optional DSN forwarded to all module calls that support --dsn.")
    p.add_argument("--since", default="2019-01-01", help="Since date for orcid_works (default: 2019-01-01).")
    p.add_argument(
        "--person-kind",
        default="internal",
        choices=["internal", "external"],
        help="Person kind used by add_people (default: internal).",
    )
    p.add_argument(
        "--no-update-existing",
        action="store_true",
        help="Do not pass --update-existing to add_people.",
    )
    p.add_argument(
        "--crossref-mailto",
        help="Optional mailto forwarded to orcid_works as --crossref-mailto.",
    )
    p.add_argument("--skip-add-people", action="store_true", help="Skip add_people step.")
    p.add_argument("--skip-orcid-profiles", action="store_true", help="Skip orcid_profiles step.")
    p.add_argument("--skip-orcid-works", action="store_true", help="Skip orcid_works step.")
    p.add_argument("--skip-crossref-search", action="store_true", help="Skip crossref_search step.")
    p.add_argument(
        "--skip-crossref-backfill",
        action="store_true",
        help="Skip crossref_backfill per pub_id step.",
    )
    p.add_argument(
        "--crossref-backfill-source-token",
        help="Optional --source-token value forwarded to crossref_backfill.",
    )
    p.add_argument("--stop-on-error", action="store_true", help="Stop at first failing command.")
    p.add_argument("--dry-run", action="store_true", help="Forward --dry-run to called modules (no DB writes).")
    p.add_argument("--debug", action="store_true", help="Forward --debug to called modules.")
    p.add_argument(
        "--print-only",
        action="store_true",
        help="Only print commands; do not execute subprocesses.",
    )
    p.add_argument(
        "--dotenv",
        default="DB/.env",
        help="Optional dotenv file to load if env vars are missing (default: DB/.env relative to repo root). Use '' to disable.",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)

    script_path = Path(__file__).resolve()
    db_root = script_path.parents[2]  # <repo>/DB
    repo_root = script_path.parents[3]  # <repo>

    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        csv_path = (Path.cwd() / csv_path).resolve()
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    dotenv_path: Optional[Path]
    if args.dotenv == "":
        dotenv_path = None
    else:
        raw = Path(args.dotenv)
        if raw.is_absolute():
            dotenv_path = raw
        else:
            dotenv_path = (repo_root / raw).resolve()
    env = _compose_env(dotenv_path)

    rows, warnings = _read_csv(csv_path)
    for w in warnings:
        print(f"WARN: {w}")
    if not rows:
        raise SystemExit("No valid rows found in CSV.")

    orcids, pub_ids = _dedupe_inputs(rows)
    print(f"Loaded rows={len(rows)} unique_orcids={len(orcids)} unique_pub_ids={len(pub_ids)}")

    run_failed = False

    def run_or_fail(cmd: List[str]) -> None:
        nonlocal run_failed
        rc = _run_cmd(cmd, cwd=db_root, env=env, execute=not args.print_only)
        if rc != 0:
            run_failed = True
            msg = f"Command failed with exit code {rc}: {' '.join(shlex.quote(c) for c in cmd)}"
            print(f"ERROR: {msg}")
            if args.stop_on_error:
                raise SystemExit(rc)

    common_flags: List[str] = []
    if args.dsn:
        common_flags.extend(["--dsn", args.dsn])
    if args.dry_run:
        common_flags.append("--dry-run")
    if args.debug:
        common_flags.append("--debug")

    py = args.python

    for orcid, display_name in orcids:
        print(f"\n=== ORCID {orcid} ===")
        if not args.skip_add_people:
            cmd = [py, "-m", "people_pubs.sync.add_people"]
            cmd.extend(common_flags)
            cmd.extend(["--orcid", orcid, "--person-kind", args.person_kind])
            if display_name:
                cmd.extend(["--display-name", display_name])
            if not args.no_update_existing:
                cmd.append("--update-existing")
            run_or_fail(cmd)

        if not args.skip_orcid_profiles:
            cmd = [py, "-m", "people_pubs.sync.orcid_profiles"]
            cmd.extend(common_flags)
            cmd.extend(["--only-orcid", orcid])
            run_or_fail(cmd)

        if not args.skip_orcid_works:
            cmd = [py, "-m", "people_pubs.sync.orcid_works"]
            cmd.extend(common_flags)
            cmd.extend(["--since", args.since, "--only-orcid", orcid])
            if args.crossref_mailto:
                cmd.extend(["--crossref-mailto", args.crossref_mailto])
            run_or_fail(cmd)

        if not args.skip_crossref_search:
            cmd = [py, "-m", "people_pubs.sync.crossref_search"]
            cmd.extend(common_flags)
            cmd.extend(["--author-orcid", orcid])
            run_or_fail(cmd)

    if not args.skip_crossref_backfill and pub_ids:
        for pub_id in pub_ids:
            print(f"\n=== pub_id {pub_id} backfill ===")
            cmd = [py, "-m", "people_pubs.sync.crossref_backfill"]
            cmd.extend(common_flags)
            cmd.extend(
                [
                    "--only-pub-id",
                    str(pub_id),
                    "--force",
                    "--refresh-authorships",
                ]
            )
            if args.crossref_backfill_source_token:
                cmd.extend(["--source-token", args.crossref_backfill_source_token])
            run_or_fail(cmd)

    if run_failed:
        raise SystemExit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()
