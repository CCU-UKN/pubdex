"""Export/import manual curation state.

This is intentionally separate from database backups. It captures the small
manual/auditable tables that protect publication and authorship decisions from
being lost during older-dump restores or publication resets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from people_pubs.config import DEFAULT_DSN
from people_pubs.db.connection import db_conn


LOG = logging.getLogger("people_pubs.curation_backup")
BACKUP_VERSION = 1

TABLES: Dict[str, Dict[str, Any]] = {
    "biblio.curation_review_queue": {
        "order_by": "queue_id",
        "conflict": "queue_id",
        "default": True,
    },
    "biblio.canon_scope_decisions": {
        "order_by": "decision_id",
        "conflict": "decision_id",
        "default": True,
    },
    "biblio.publication_dedup_decisions": {
        "order_by": "decision_id",
        "conflict": "decision_id",
        "default": True,
    },
    "biblio.publication_dedup_overrides": {
        "order_by": "pub_id",
        "conflict": "pub_id",
        "default": True,
    },
    "biblio.authorship_manual_overrides": {
        "order_by": "override_id",
        "conflict": "override_id",
        "default": True,
    },
    "app.person_name_aliases_manual": {
        "order_by": "person_id, lower(alias_name)",
        "conflict": "person_id, lower(alias_name)",
        "default": True,
    },
    "biblio.manual_corrections_log": {
        "order_by": "log_id",
        "conflict": "log_id",
        "default": True,
    },
}


def _json_default(value: Any) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


def _table_exists(conn: psycopg.Connection, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        row = cur.fetchone()
    if isinstance(row, dict):
        return bool(next(iter(row.values())))
    return bool(row and row[0])


def _selected_tables(include_correction_log: bool) -> List[str]:
    return [
        table
        for table, cfg in TABLES.items()
        if cfg["default"] or include_correction_log
    ]


def export_curation(
    conn: psycopg.Connection,
    *,
    include_correction_log: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "version": BACKUP_VERSION,
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tables": {},
    }
    for table in _selected_tables(include_correction_log):
        if not _table_exists(conn, table):
            LOG.warning("skipping missing table: %s", table)
            continue
        order_by = TABLES[table]["order_by"]
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
            rows = [dict(row) for row in cur.fetchall()]
        payload["tables"][table] = rows
        LOG.info("exported %s rows=%d", table, len(rows))
    return payload


def _table_columns(conn: psycopg.Connection, table: str) -> List[str]:
    schema, name = table.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, name),
        )
        rows = cur.fetchall()
    return [r["column_name"] if isinstance(r, dict) else r[0] for r in rows]


def _coerce_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    return value


def _upsert_rows(
    conn: psycopg.Connection,
    table: str,
    rows: Sequence[Dict[str, Any]],
    *,
    dry_run: bool,
) -> int:
    if not rows:
        return 0
    if not _table_exists(conn, table):
        raise SystemExit(f"Target table is missing: {table}")

    target_columns = set(_table_columns(conn, table))
    columns = [c for c in rows[0].keys() if c in target_columns]
    if not columns:
        return 0

    conflict = TABLES[table]["conflict"]
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(columns)
    update_columns = [c for c in columns if c not in {part.strip() for part in conflict.split(",")}]
    if update_columns:
        update_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
        conflict_sql = f"ON CONFLICT ({conflict}) DO UPDATE SET {update_sql}"
    else:
        conflict_sql = f"ON CONFLICT ({conflict}) DO NOTHING"
    sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) {conflict_sql}"

    if dry_run:
        LOG.info("[dry-run] would import %s rows=%d", table, len(rows))
        return len(rows)

    imported = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(sql, tuple(_coerce_value(row.get(c)) for c in columns))
            imported += 1
    return imported


def import_curation(
    conn: psycopg.Connection,
    payload: Dict[str, Any],
    *,
    dry_run: bool,
) -> Dict[str, int]:
    if int(payload.get("version", 0)) != BACKUP_VERSION:
        raise SystemExit(f"Unsupported curation backup version: {payload.get('version')}")
    tables = payload.get("tables") or {}
    if not isinstance(tables, dict):
        raise SystemExit("Invalid curation backup: missing tables object")

    counts: Dict[str, int] = {}
    for table in TABLES:
        rows = tables.get(table)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise SystemExit(f"Invalid rows for {table}")
        counts[table] = _upsert_rows(conn, table, rows, dry_run=dry_run)
        LOG.info("imported %s rows=%d", table, counts[table])
    return counts


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export/import manual curation tables.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN or PG* env vars.")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="Export curation tables to JSON.")
    exp.add_argument("--output", required=True)
    exp.add_argument(
        "--include-correction-log",
        action="store_true",
        help="Deprecated no-op: biblio.manual_corrections_log is always exported.",
    )

    imp = sub.add_parser("import", help="Import curation tables from JSON.")
    imp.add_argument("--input", required=True)
    imp.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")

    with db_conn(args.dsn) as conn:
        if args.command == "export":
            payload = export_curation(conn, include_correction_log=args.include_correction_log)
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
            LOG.info("curation export summary: output=%s tables=%d", path, len(payload["tables"]))
        elif args.command == "import":
            payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
            counts = import_curation(conn, payload, dry_run=args.dry_run)
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()
            LOG.info(
                "curation import summary: tables=%d rows=%d dry_run=%s",
                len(counts),
                sum(counts.values()),
                args.dry_run,
            )
        else:
            raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
