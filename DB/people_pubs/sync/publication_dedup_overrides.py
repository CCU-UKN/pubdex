"""
people_pubs.sync.publication_dedup_overrides

Manage manual dedup overrides used by biblio.publications_clean.

Override modes:
- merge_to: force pub_id (loser) to map to winner_pub_id
- keep_separate: keep pub_id as its own canonical row, even if title matches
- confirm_winner: lock the currently chosen canonical version for future reingests

The script writes both current pub_id overrides and durable DOI/title decisions.
Durable decisions survive biblio.publications resets and are resolved back to
current pub_ids by biblio.publications_clean.

Examples:
  # Merge loser into winner (manual canonical choice)
  python -m people_pubs.sync.publication_dedup_overrides \
    set-merge --loser-pub-id 12554 --winner-pub-id 5259 --note "Conference -> journal"

  # Keep both entries separate (both can be canonical)
  python -m people_pubs.sync.publication_dedup_overrides \
    set-keep-separate --pub-id 6449 --pub-id 3763 --note "Same title, different venue"

  # List current overrides
  python -m people_pubs.sync.publication_dedup_overrides list

  # Remove an override
  python -m people_pubs.sync.publication_dedup_overrides clear --pub-id 12554

  # Deactivate a durable decision directly
  python -m people_pubs.sync.publication_dedup_overrides clear --decision-id 12
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg
from psycopg.rows import dict_row

from people_pubs.config import DEFAULT_DSN
from people_pubs.db.connection import db_conn
from people_pubs.services.crossref_client import normalize_doi


LOGGER = logging.getLogger("people_pubs.publication_dedup_overrides")
_WS_RE = re.compile(r"\s+")


def _ensure_table(conn: psycopg.Connection, dry_run: bool) -> None:
    if _table_exists(conn, "biblio.publication_dedup_overrides") and _table_exists(
        conn,
        "biblio.publication_dedup_decisions",
    ):
        return

    ddl = """
    CREATE TABLE IF NOT EXISTS biblio.publication_dedup_overrides (
        pub_id int PRIMARY KEY REFERENCES biblio.publications(pub_id) ON DELETE CASCADE,
        mode text NOT NULL CHECK (mode IN ('merge_to', 'keep_separate')),
        winner_pub_id int REFERENCES biblio.publications(pub_id) ON DELETE CASCADE,
        note text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CHECK (
            (mode = 'merge_to' AND winner_pub_id IS NOT NULL AND winner_pub_id <> pub_id)
            OR
            (mode = 'keep_separate' AND winner_pub_id IS NULL)
        )
    );

    CREATE INDEX IF NOT EXISTS idx_publication_dedup_overrides_winner
      ON biblio.publication_dedup_overrides (winner_pub_id);

    CREATE TABLE IF NOT EXISTS biblio.publication_dedup_decisions (
        decision_id bigserial PRIMARY KEY,
        mode text NOT NULL CHECK (mode IN ('merge_to', 'keep_separate', 'confirm_winner')),
        subject_pub_id bigint,
        subject_doi text,
        subject_doi_key text,
        subject_title text,
        subject_title_key text,
        subject_year int,
        subject_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
        winner_pub_id bigint,
        winner_doi text,
        winner_doi_key text,
        winner_title text,
        winner_title_key text,
        winner_year int,
        winner_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
        active boolean NOT NULL DEFAULT true,
        note text,
        evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_by text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CHECK (subject_doi_key IS NOT NULL OR subject_title_key IS NOT NULL),
        CHECK (
            ((mode = 'merge_to' OR mode = 'confirm_winner') AND (winner_doi_key IS NOT NULL OR winner_title_key IS NOT NULL))
            OR
            (mode = 'keep_separate' AND winner_doi_key IS NULL AND winner_title_key IS NULL)
        )
    );

    CREATE INDEX IF NOT EXISTS idx_publication_dedup_decisions_subject_doi
      ON biblio.publication_dedup_decisions (subject_doi_key)
      WHERE active IS TRUE AND subject_doi_key IS NOT NULL;

    CREATE INDEX IF NOT EXISTS idx_publication_dedup_decisions_subject_title
      ON biblio.publication_dedup_decisions (subject_title_key, subject_year)
      WHERE active IS TRUE AND subject_title_key IS NOT NULL;

    CREATE INDEX IF NOT EXISTS idx_publication_dedup_decisions_winner_doi
      ON biblio.publication_dedup_decisions (winner_doi_key)
      WHERE active IS TRUE AND winner_doi_key IS NOT NULL;

    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_publication_dedup_decisions_updated'
          AND tgrelid = 'biblio.publication_dedup_decisions'::regclass
      ) THEN
        CREATE TRIGGER trg_publication_dedup_decisions_updated
        BEFORE UPDATE ON biblio.publication_dedup_decisions
        FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
      END IF;
    END $$;
    """
    if dry_run:
        LOGGER.info("[dry-run] would ensure biblio.publication_dedup_overrides exists")
        return
    with conn.cursor() as cur:
        cur.execute(ddl)


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
        for v in row.values():
            return bool(v)
    return False


def _jsonb(value: Optional[Dict[str, object]]) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _norm_title(value: Optional[str]) -> Optional[str]:
    out = _WS_RE.sub(" ", (value or "").strip()).lower()
    return out or None


def _pub_snapshot(conn: psycopg.Connection, pub_id: int) -> Dict[str, object]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT pub_id, doi, title, year, venue, source_of_truth, is_preprint
            FROM biblio.publications
            WHERE pub_id = %s
            """,
            (pub_id,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"pub_id not found: {pub_id}")

    snap = dict(row)
    snap["doi_key"] = normalize_doi(str(row.get("doi") or "")) if row.get("doi") else None
    snap["title_key"] = _norm_title(row.get("title"))
    if not snap.get("doi_key") and not snap.get("title_key"):
        raise SystemExit(f"pub_id={pub_id} has neither DOI nor title; cannot make durable decision.")
    return snap


def _decision_target(snapshot: Dict[str, object], mode: Optional[str] = None) -> Dict[str, object]:
    target: Dict[str, object] = {
        "pub_id": snapshot.get("pub_id"),
        "doi_key": snapshot.get("doi_key"),
        "title_key": snapshot.get("title_key"),
        "year": snapshot.get("year"),
    }
    if mode:
        target["mode"] = mode
    return target


def _active_decisions_for_snapshot(
    conn: psycopg.Connection,
    snapshot: Dict[str, object],
) -> List[Dict[str, object]]:
    clauses: List[str] = ["active IS TRUE"]
    params: List[object] = []
    doi_key = snapshot.get("doi_key")
    title_key = snapshot.get("title_key")
    year = snapshot.get("year")

    selectors: List[str] = []
    # pub_id is stored for audit only. Durable decisions are intentionally
    # matched by DOI first, and title/year only when no DOI is available, so a
    # reset that reuses pub_id values cannot deactivate an unrelated decision.
    if doi_key:
        selectors.append("subject_doi_key = %s")
        params.append(doi_key)
    elif title_key:
        selectors.append(
            "(subject_title_key = %s AND (subject_year IS NULL OR subject_year = %s))"
        )
        params.extend([title_key, year])
    if not selectors:
        return []

    clauses.append("(" + " OR ".join(selectors) + ")")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT *
            FROM biblio.publication_dedup_decisions
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, decision_id DESC
            """,
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def _deactivate_active_decisions_for_snapshot(
    conn: psycopg.Connection,
    snapshot: Dict[str, object],
) -> int:
    existing = _active_decisions_for_snapshot(conn, snapshot)
    if not existing:
        return 0
    decision_ids = [int(row["decision_id"]) for row in existing]
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE biblio.publication_dedup_decisions
            SET active = FALSE,
                updated_at = now()
            WHERE decision_id = ANY(%s)
            """,
            (decision_ids,),
        )
        return int(cur.rowcount or 0)


def _insert_durable_decision(
    conn: psycopg.Connection,
    *,
    mode: str,
    subject: Dict[str, object],
    winner: Optional[Dict[str, object]],
    note: Optional[str],
    applied_by: Optional[str],
    dry_run: bool,
) -> Optional[Dict[str, object]]:
    if dry_run:
        LOGGER.info(
            "[dry-run] would store durable %s decision for pub_id=%s",
            mode,
            subject.get("pub_id"),
        )
        return None

    _deactivate_active_decisions_for_snapshot(conn, subject)
    winner = winner or {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO biblio.publication_dedup_decisions (
              mode,
              subject_pub_id, subject_doi, subject_doi_key,
              subject_title, subject_title_key, subject_year, subject_snapshot,
              winner_pub_id, winner_doi, winner_doi_key,
              winner_title, winner_title_key, winner_year, winner_snapshot,
              note, created_by
            )
            VALUES (
              %s,
              %s, %s, %s,
              %s, %s, %s, %s::jsonb,
              %s, %s, %s,
              %s, %s, %s, %s::jsonb,
              %s, %s
            )
            RETURNING *
            """,
            (
                mode,
                subject.get("pub_id"),
                subject.get("doi"),
                subject.get("doi_key"),
                subject.get("title"),
                subject.get("title_key"),
                subject.get("year"),
                _jsonb(subject),
                winner.get("pub_id"),
                winner.get("doi"),
                winner.get("doi_key"),
                winner.get("title"),
                winner.get("title_key"),
                winner.get("year"),
                _jsonb(winner),
                note,
                applied_by,
            ),
        )
        return dict(cur.fetchone())


def _fetch_override_state(conn: psycopg.Connection, pub_id: int) -> Optional[Dict[str, object]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT pub_id, mode, winner_pub_id, note, created_at, updated_at
            FROM biblio.publication_dedup_overrides
            WHERE pub_id = %s
            """,
            (pub_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _log_manual_correction(
    conn: psycopg.Connection,
    *,
    has_log_table: bool,
    correction_type: str,
    target: Dict[str, object],
    before_state: Optional[Dict[str, object]],
    after_state: Optional[Dict[str, object]],
    applied_by: Optional[str],
    note: Optional[str],
    dry_run: bool,
) -> None:
    if not has_log_table:
        return
    if dry_run:
        LOGGER.info(
            "[dry-run] would log correction type=%s target=%s",
            correction_type,
            target,
        )
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biblio.manual_corrections_log
              (correction_type, target, before_state, after_state, applied_by, note)
            VALUES
              (%s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
            """,
            (
                correction_type,
                _jsonb(target),
                _jsonb(before_state),
                _jsonb(after_state),
                applied_by,
                note,
            ),
        )


def _pub_exists(conn: psycopg.Connection, pub_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM biblio.publications WHERE pub_id = %s", (pub_id,))
        return cur.fetchone() is not None


def _parse_pub_id_input(value: str) -> List[int]:
    """
    Parse a single --pub-id value that can be:
    - integer literal
    - comma-separated integer list
    - CSV file path (first column contains pub_ids; header allowed)
    """
    raw = (value or "").strip()
    if not raw:
        return []

    path = Path(raw)
    if path.exists() and path.is_file():
        ids: List[int] = []
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                first = str(row[0]).strip().strip('"').strip("'")
                if not first:
                    continue
                try:
                    ids.append(int(first))
                except ValueError:
                    # likely header
                    continue
        if not ids:
            raise SystemExit(f"No pub_id values found in CSV: {path}")
        return ids

    if "," in raw:
        out: List[int] = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            out.append(int(token))
        if not out:
            raise SystemExit("No pub_id values in comma-separated input.")
        return out

    return [int(raw)]


def _flatten_pub_ids(inputs: Sequence[str]) -> List[int]:
    seen: set[int] = set()
    out: List[int] = []
    for val in inputs:
        for pid in _parse_pub_id_input(val):
            if pid in seen:
                continue
            seen.add(pid)
            out.append(pid)
    return out


def _set_merge_one(
    conn: psycopg.Connection,
    loser_pub_id: int,
    winner_pub_id: int,
    note: Optional[str],
    has_log_table: bool,
    applied_by: Optional[str],
    dry_run: bool,
) -> None:
    if loser_pub_id == winner_pub_id:
        raise SystemExit("--loser-pub-id and --winner-pub-id must be different.")
    if not _pub_exists(conn, loser_pub_id):
        raise SystemExit(f"Loser pub_id not found: {loser_pub_id}")
    if not _pub_exists(conn, winner_pub_id):
        raise SystemExit(f"Winner pub_id not found: {winner_pub_id}")
    loser_snapshot = _pub_snapshot(conn, loser_pub_id)
    winner_snapshot = _pub_snapshot(conn, winner_pub_id)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT mode, winner_pub_id
            FROM biblio.publication_dedup_overrides
            WHERE pub_id = %s
            """,
            (winner_pub_id,),
        )
        winner_row = cur.fetchone()
    if winner_row and winner_row.get("mode") == "merge_to":
        LOGGER.warning(
            "winner_pub_id=%s already has mode=merge_to (winner=%s).",
            winner_pub_id,
            winner_row.get("winner_pub_id"),
        )

    before_state = _fetch_override_state(conn, loser_pub_id)
    durable_before = {"active_decisions": _active_decisions_for_snapshot(conn, loser_snapshot)}

    if dry_run:
        LOGGER.info(
            "[dry-run] would set merge_to: loser=%s -> winner=%s note=%r",
            loser_pub_id,
            winner_pub_id,
            note,
        )
        _log_manual_correction(
            conn,
            has_log_table=has_log_table,
            correction_type="dedup_override.set_merge",
            target={"pub_id": loser_pub_id, "mode": "merge_to", "winner_pub_id": winner_pub_id},
            before_state=before_state,
            after_state={
                "pub_id": loser_pub_id,
                "mode": "merge_to",
                "winner_pub_id": winner_pub_id,
                "note": note,
            },
            applied_by=applied_by,
            note=note,
            dry_run=dry_run,
        )
        _log_manual_correction(
            conn,
            has_log_table=has_log_table,
            correction_type="dedup_decision.set_merge",
            target={
                **_decision_target(loser_snapshot, "merge_to"),
                "winner": _decision_target(winner_snapshot),
            },
            before_state=durable_before,
            after_state={
                "mode": "merge_to",
                "subject": loser_snapshot,
                "winner": winner_snapshot,
                "note": note,
            },
            applied_by=applied_by,
            note=note,
            dry_run=dry_run,
        )
        return

    durable_after = _insert_durable_decision(
        conn,
        mode="merge_to",
        subject=loser_snapshot,
        winner=winner_snapshot,
        note=note,
        applied_by=applied_by,
        dry_run=dry_run,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO biblio.publication_dedup_overrides (pub_id, mode, winner_pub_id, note)
            VALUES (%s, 'merge_to', %s, %s)
            ON CONFLICT (pub_id) DO UPDATE
            SET mode = EXCLUDED.mode,
                winner_pub_id = EXCLUDED.winner_pub_id,
                note = EXCLUDED.note,
                updated_at = now()
            """,
            (loser_pub_id, winner_pub_id, note),
        )
    after_state = _fetch_override_state(conn, loser_pub_id)
    _log_manual_correction(
        conn,
        has_log_table=has_log_table,
        correction_type="dedup_override.set_merge",
        target={"pub_id": loser_pub_id, "mode": "merge_to", "winner_pub_id": winner_pub_id},
        before_state=before_state,
        after_state=after_state,
        applied_by=applied_by,
        note=note,
        dry_run=dry_run,
    )
    _log_manual_correction(
        conn,
        has_log_table=has_log_table,
        correction_type="dedup_decision.set_merge",
        target={
            **_decision_target(loser_snapshot, "merge_to"),
            "winner": _decision_target(winner_snapshot),
        },
        before_state=durable_before,
        after_state=durable_after,
        applied_by=applied_by,
        note=note,
        dry_run=dry_run,
    )
    LOGGER.info("set merge_to: loser=%s -> winner=%s", loser_pub_id, winner_pub_id)


def _set_keep_separate(
    conn: psycopg.Connection,
    pub_ids: Sequence[int],
    note: Optional[str],
    has_log_table: bool,
    applied_by: Optional[str],
    dry_run: bool,
) -> None:
    if not pub_ids:
        raise SystemExit("No pub_id values provided.")
    for pub_id in pub_ids:
        if not _pub_exists(conn, pub_id):
            raise SystemExit(f"pub_id not found: {pub_id}")

    for pub_id in pub_ids:
        pub_snapshot = _pub_snapshot(conn, pub_id)
        before_state = _fetch_override_state(conn, pub_id)
        durable_before = {"active_decisions": _active_decisions_for_snapshot(conn, pub_snapshot)}
        if dry_run:
            LOGGER.info("[dry-run] would set keep_separate for pub_id=%s note=%r", pub_id, note)
            _log_manual_correction(
                conn,
                has_log_table=has_log_table,
                correction_type="dedup_override.set_keep_separate",
                target={"pub_id": pub_id, "mode": "keep_separate"},
                before_state=before_state,
                after_state={
                    "pub_id": pub_id,
                    "mode": "keep_separate",
                    "winner_pub_id": None,
                    "note": note,
                },
                applied_by=applied_by,
                note=note,
                dry_run=dry_run,
            )
            _log_manual_correction(
                conn,
                has_log_table=has_log_table,
                correction_type="dedup_decision.set_keep_separate",
                target=_decision_target(pub_snapshot, "keep_separate"),
                before_state=durable_before,
                after_state={
                    "mode": "keep_separate",
                    "subject": pub_snapshot,
                    "note": note,
                },
                applied_by=applied_by,
                note=note,
                dry_run=dry_run,
            )
            continue
        durable_after = _insert_durable_decision(
            conn,
            mode="keep_separate",
            subject=pub_snapshot,
            winner=None,
            note=note,
            applied_by=applied_by,
            dry_run=dry_run,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biblio.publication_dedup_overrides (pub_id, mode, winner_pub_id, note)
                VALUES (%s, 'keep_separate', NULL, %s)
                ON CONFLICT (pub_id) DO UPDATE
                SET mode = EXCLUDED.mode,
                    winner_pub_id = NULL,
                    note = EXCLUDED.note,
                    updated_at = now()
                """,
                (pub_id, note),
            )
        after_state = _fetch_override_state(conn, pub_id)
        _log_manual_correction(
            conn,
            has_log_table=has_log_table,
            correction_type="dedup_override.set_keep_separate",
            target={"pub_id": pub_id, "mode": "keep_separate"},
            before_state=before_state,
            after_state=after_state,
            applied_by=applied_by,
            note=note,
            dry_run=dry_run,
        )
        _log_manual_correction(
            conn,
            has_log_table=has_log_table,
            correction_type="dedup_decision.set_keep_separate",
            target=_decision_target(pub_snapshot, "keep_separate"),
            before_state=durable_before,
            after_state=durable_after,
            applied_by=applied_by,
            note=note,
            dry_run=dry_run,
        )
        LOGGER.info("set keep_separate: pub_id=%s", pub_id)


def _confirm_winner(
    conn: psycopg.Connection,
    winner_pub_id: int,
    note: Optional[str],
    has_log_table: bool,
    applied_by: Optional[str],
    dry_run: bool,
) -> None:
    if not _pub_exists(conn, winner_pub_id):
        raise SystemExit(f"Winner pub_id not found: {winner_pub_id}")

    winner_snapshot = _pub_snapshot(conn, winner_pub_id)
    durable_before = {"active_decisions": _active_decisions_for_snapshot(conn, winner_snapshot)}
    if dry_run:
        LOGGER.info("[dry-run] would confirm current winner for pub_id=%s note=%r", winner_pub_id, note)
        _log_manual_correction(
            conn,
            has_log_table=has_log_table,
            correction_type="dedup_decision.confirm_winner",
            target={
                **_decision_target(winner_snapshot, "confirm_winner"),
                "winner": _decision_target(winner_snapshot),
            },
            before_state=durable_before,
            after_state={
                "mode": "confirm_winner",
                "subject": winner_snapshot,
                "winner": winner_snapshot,
                "note": note,
            },
            applied_by=applied_by,
            note=note,
            dry_run=dry_run,
        )
        return

    after_state = _insert_durable_decision(
        conn,
        mode="confirm_winner",
        subject=winner_snapshot,
        winner=winner_snapshot,
        note=note,
        applied_by=applied_by,
        dry_run=dry_run,
    )
    _log_manual_correction(
        conn,
        has_log_table=has_log_table,
        correction_type="dedup_decision.confirm_winner",
        target={
            **_decision_target(winner_snapshot, "confirm_winner"),
            "winner": _decision_target(winner_snapshot),
        },
        before_state=durable_before,
        after_state=after_state,
        applied_by=applied_by,
        note=note,
        dry_run=dry_run,
    )
    LOGGER.info("confirmed current winner: pub_id=%s", winner_pub_id)


def _clear_overrides(
    conn: psycopg.Connection,
    pub_ids: Sequence[int],
    decision_ids: Sequence[int],
    has_log_table: bool,
    applied_by: Optional[str],
    dry_run: bool,
) -> None:
    if not pub_ids and not decision_ids:
        raise SystemExit("clear requires --pub-id and/or --decision-id.")
    deleted_total = 0
    for pub_id in pub_ids:
        before_state = _fetch_override_state(conn, pub_id)
        pub_snapshot = _pub_snapshot(conn, pub_id) if _pub_exists(conn, pub_id) else None
        durable_before = (
            {"active_decisions": _active_decisions_for_snapshot(conn, pub_snapshot)}
            if pub_snapshot
            else None
        )
        if dry_run:
            LOGGER.info("[dry-run] would delete override for pub_id=%s", pub_id)
            _log_manual_correction(
                conn,
                has_log_table=has_log_table,
                correction_type="dedup_override.clear",
                target={"pub_id": pub_id},
                before_state=before_state,
                after_state=None,
                applied_by=applied_by,
                note=None,
                dry_run=dry_run,
            )
            if pub_snapshot:
                _log_manual_correction(
                    conn,
                    has_log_table=has_log_table,
                    correction_type="dedup_decision.clear_for_pub",
                    target=_decision_target(pub_snapshot),
                    before_state=durable_before,
                    after_state={"active": False},
                    applied_by=applied_by,
                    note=None,
                    dry_run=dry_run,
                )
            continue
        if pub_snapshot:
            deactivated = _deactivate_active_decisions_for_snapshot(conn, pub_snapshot)
            if deactivated:
                LOGGER.info("deactivated durable decisions for pub_id=%s: %s", pub_id, deactivated)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM biblio.publication_dedup_overrides WHERE pub_id = %s",
                (pub_id,),
            )
            deleted = cur.rowcount or 0
            deleted_total += deleted
        _log_manual_correction(
            conn,
            has_log_table=has_log_table,
            correction_type="dedup_override.clear",
            target={"pub_id": pub_id},
            before_state=before_state,
            after_state=None,
            applied_by=applied_by,
            note=None,
            dry_run=dry_run,
        )
        if pub_snapshot:
            _log_manual_correction(
                conn,
                has_log_table=has_log_table,
                correction_type="dedup_decision.clear_for_pub",
                target=_decision_target(pub_snapshot),
                before_state=durable_before,
                after_state={"active": False},
                applied_by=applied_by,
                note=None,
                dry_run=dry_run,
            )

    for decision_id in decision_ids:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT *
                FROM biblio.publication_dedup_decisions
                WHERE decision_id = %s
                """,
                (decision_id,),
            )
            before_row = cur.fetchone()
        before_state = dict(before_row) if before_row else None
        if not before_state:
            LOGGER.warning("durable decision not found: decision_id=%s", decision_id)
            continue
        if dry_run:
            LOGGER.info("[dry-run] would deactivate durable decision_id=%s", decision_id)
            _log_manual_correction(
                conn,
                has_log_table=has_log_table,
                correction_type="dedup_decision.clear",
                target={"decision_id": decision_id},
                before_state=before_state,
                after_state={**before_state, "active": False},
                applied_by=applied_by,
                note=None,
                dry_run=dry_run,
            )
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biblio.publication_dedup_decisions
                SET active = FALSE,
                    updated_at = now()
                WHERE decision_id = %s
                """,
                (decision_id,),
            )
        _log_manual_correction(
            conn,
            has_log_table=has_log_table,
            correction_type="dedup_decision.clear",
            target={"decision_id": decision_id},
            before_state=before_state,
            after_state={**before_state, "active": False},
            applied_by=applied_by,
            note=None,
            dry_run=dry_run,
        )
        LOGGER.info("deactivated durable decision_id=%s", decision_id)
    LOGGER.info("cleared overrides: deleted=%s", deleted_total)


def _backfill_current_overrides(
    conn: psycopg.Connection,
    *,
    has_log_table: bool,
    applied_by: Optional[str],
    dry_run: bool,
) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT pub_id, mode, winner_pub_id, note
            FROM biblio.publication_dedup_overrides
            ORDER BY mode, pub_id
            """
        )
        rows = [dict(row) for row in cur.fetchall()]

    if not rows:
        LOGGER.info("no current pub_id overrides to backfill")
        return 0

    created = 0
    for row in rows:
        mode = str(row["mode"])
        subject = _pub_snapshot(conn, int(row["pub_id"]))
        winner = None
        if mode == "merge_to":
            winner = _pub_snapshot(conn, int(row["winner_pub_id"]))
        before_state = {"active_decisions": _active_decisions_for_snapshot(conn, subject)}
        note = row.get("note") or "Backfilled from current pub_id override"

        if dry_run:
            LOGGER.info(
                "[dry-run] would backfill durable decision from pub_id override: pub_id=%s mode=%s",
                row["pub_id"],
                mode,
            )
            after_state = {
                "mode": mode,
                "subject": subject,
                "winner": winner,
                "note": note,
            }
        else:
            after_state = _insert_durable_decision(
                conn,
                mode=mode,
                subject=subject,
                winner=winner,
                note=str(note) if note is not None else None,
                applied_by=applied_by,
                dry_run=dry_run,
            )
            created += 1

        target = _decision_target(subject, mode)
        if winner:
            target["winner"] = _decision_target(winner)
        _log_manual_correction(
            conn,
            has_log_table=has_log_table,
            correction_type="dedup_decision.backfill_current",
            target=target,
            before_state=before_state,
            after_state=after_state,
            applied_by=applied_by,
            note=str(note) if note is not None else None,
            dry_run=dry_run,
        )

    LOGGER.info("backfilled durable decisions from current overrides: %s", len(rows) if dry_run else created)
    return len(rows) if dry_run else created


def _list_overrides(
    conn: psycopg.Connection,
    mode: Optional[str],
    pub_ids: Optional[Sequence[int]],
    include_inactive: bool,
) -> None:
    where: List[str] = []
    params: List[object] = []
    if mode:
        where.append("o.mode = %s")
        params.append(mode)
    if pub_ids:
        where.append("o.pub_id = ANY(%s)")
        params.append(list(pub_ids))
    sql = """
    SELECT
        o.pub_id,
        o.mode,
        o.winner_pub_id,
        o.note,
        o.created_at,
        o.updated_at,
        p1.doi AS pub_doi,
        p1.title AS pub_title,
        p2.doi AS winner_doi,
        p2.title AS winner_title
    FROM biblio.publication_dedup_overrides o
    JOIN biblio.publications p1 ON p1.pub_id = o.pub_id
    LEFT JOIN biblio.publications p2 ON p2.pub_id = o.winner_pub_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.mode, o.pub_id"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, tuple(params))
        rows = [dict(row) for row in cur.fetchall()]

    durable_where: List[str] = []
    durable_params: List[object] = []
    if mode:
        durable_where.append("d.mode = %s")
        durable_params.append(mode)
    if not include_inactive:
        durable_where.append("d.active IS TRUE")
    if pub_ids:
        durable_where.append("current_subject.pub_id = ANY(%s)")
        durable_params.append(list(pub_ids))

    durable_sql = """
    WITH pub_keys AS (
      SELECT
        p.pub_id,
        NULLIF(lower(
          regexp_replace(
            regexp_replace(btrim(COALESCE(p.doi, '')), '^\\s*doi:\\s*', '', 'i'),
            '^\\s*https?://(?:dx\\.)?doi\\.org/', '', 'i'
          )
        ), '') AS doi_key,
        lower(regexp_replace(btrim(p.title), '\\s+', ' ', 'g')) AS title_key,
        p.year
      FROM biblio.publications p
    )
    SELECT
      d.decision_id,
      d.active,
      d.mode,
      d.subject_pub_id,
      d.subject_doi_key,
      d.subject_title,
      d.subject_year,
      d.winner_pub_id,
      d.winner_doi_key,
      d.winner_title,
      d.winner_year,
      current_subject.pub_id AS current_subject_pub_id,
      current_winner.pub_id AS current_winner_pub_id,
      d.note,
      d.created_by,
      d.created_at,
      d.updated_at
    FROM biblio.publication_dedup_decisions d
    LEFT JOIN LATERAL (
      SELECT p.pub_id
      FROM pub_keys p
      WHERE (d.subject_doi_key IS NOT NULL AND p.doi_key = d.subject_doi_key)
         OR (
           d.subject_doi_key IS NULL
           AND
           d.subject_title_key IS NOT NULL
           AND p.title_key = d.subject_title_key
           AND (d.subject_year IS NULL OR p.year = d.subject_year)
         )
      ORDER BY
        CASE WHEN d.subject_doi_key IS NOT NULL AND p.doi_key = d.subject_doi_key THEN 0 ELSE 1 END,
        p.pub_id
      LIMIT 1
    ) current_subject ON TRUE
    LEFT JOIN LATERAL (
      SELECT p.pub_id
      FROM pub_keys p
      WHERE d.mode = 'merge_to'
        AND (
          (d.winner_doi_key IS NOT NULL AND p.doi_key = d.winner_doi_key)
          OR (
            d.winner_doi_key IS NULL
            AND
            d.winner_title_key IS NOT NULL
            AND p.title_key = d.winner_title_key
            AND (d.winner_year IS NULL OR p.year = d.winner_year)
          )
        )
      ORDER BY
        CASE WHEN d.winner_doi_key IS NOT NULL AND p.doi_key = d.winner_doi_key THEN 0 ELSE 1 END,
        p.pub_id
      LIMIT 1
    ) current_winner ON TRUE
    """
    if durable_where:
        durable_sql += " WHERE " + " AND ".join(durable_where)
    durable_sql += " ORDER BY d.active DESC, d.updated_at DESC, d.decision_id DESC"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(durable_sql, tuple(durable_params))
        durable_rows = [dict(row) for row in cur.fetchall()]

    if not rows and not durable_rows:
        print("No dedup overrides found.")
        return

    if rows:
        print("Current pub_id overrides:")
        print("pub_id\tmode\twinner_pub_id\tpub_doi\twinner_doi\tnote")
        for r in rows:
            print(
                f"{r.get('pub_id')}\t{r.get('mode')}\t{r.get('winner_pub_id')}\t"
                f"{r.get('pub_doi') or ''}\t{r.get('winner_doi') or ''}\t{r.get('note') or ''}"
            )

    if durable_rows:
        if rows:
            print()
        print("Durable decisions:")
        print(
            "decision_id\tactive\tmode\tcurrent_subject_pub_id\tcurrent_winner_pub_id\t"
            "subject_doi_key\twinner_doi_key\tnote"
        )
        for r in durable_rows:
            print(
                f"{r.get('decision_id')}\t{r.get('active')}\t{r.get('mode')}\t"
                f"{r.get('current_subject_pub_id') or ''}\t"
                f"{r.get('current_winner_pub_id') or ''}\t"
                f"{r.get('subject_doi_key') or ''}\t{r.get('winner_doi_key') or ''}\t"
                f"{r.get('note') or ''}"
            )


def _iter_merge_rows_from_csv(path: str) -> Iterable[Tuple[int, int, Optional[str]]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        lower_map: Dict[str, str] = {str(k).lower(): str(k) for k in (reader.fieldnames or [])}

        def col(name: str) -> str:
            key = lower_map.get(name.lower())
            if not key:
                raise SystemExit(
                    f"Missing required CSV column: {name}. "
                    "Expected columns: loser_pub_id,winner_pub_id[,note]"
                )
            return key

        loser_col = col("loser_pub_id")
        winner_col = col("winner_pub_id")
        note_col = lower_map.get("note")
        for i, row in enumerate(reader, start=2):
            loser_raw = str(row.get(loser_col, "")).strip()
            winner_raw = str(row.get(winner_col, "")).strip()
            if not loser_raw or not winner_raw:
                raise SystemExit(f"Invalid empty loser/winner at CSV line {i}.")
            try:
                loser = int(loser_raw)
                winner = int(winner_raw)
            except ValueError as exc:
                raise SystemExit(f"Non-integer loser/winner at CSV line {i}.") from exc
            note_val = None
            if note_col:
                note_val = (row.get(note_col) or "").strip() or None
            yield loser, winner, note_val


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Manage manual publication dedup overrides.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (or use PEOPLE_DB_DSN / PG* env vars).")
    p.add_argument(
        "--applied-by",
        default=os.getenv("USER"),
        help="Provenance user for biblio.manual_corrections_log (default: $USER).",
    )
    p.add_argument("--dry-run", action="store_true", help="Show actions without writing to DB.")
    p.add_argument("--debug", action="store_true", help="Verbose logging.")

    sub = p.add_subparsers(dest="command", required=True)

    s_merge = sub.add_parser("set-merge", help="Set manual loser->winner override.")
    s_merge.add_argument("--loser-pub-id", type=int, help="Loser pub_id (merged into winner).")
    s_merge.add_argument("--winner-pub-id", type=int, help="Winner pub_id (canonical).")
    s_merge.add_argument("--csv", help="Optional CSV with loser_pub_id,winner_pub_id[,note].")
    s_merge.add_argument("--note", default=None, help="Optional note.")

    s_keep = sub.add_parser("set-keep-separate", help="Force pub_id(s) to stay separate canonical rows.")
    s_keep.add_argument(
        "--pub-id",
        action="append",
        required=True,
        help="pub_id, comma list, or CSV path. Repeatable.",
    )
    s_keep.add_argument("--note", default=None, help="Optional note.")

    s_confirm = sub.add_parser(
        "confirm-winner",
        help="Lock the currently chosen canonical version for a dedup family.",
    )
    s_confirm.add_argument("--winner-pub-id", type=int, required=True, help="Current canonical/winner pub_id.")
    s_confirm.add_argument("--note", default=None, help="Optional note.")

    s_clear = sub.add_parser("clear", help="Delete override(s) for pub_id(s).")
    s_clear.add_argument(
        "--pub-id",
        action="append",
        help="pub_id, comma list, or CSV path. Repeatable.",
    )
    s_clear.add_argument(
        "--decision-id",
        type=int,
        action="append",
        help="Durable decision_id to deactivate. Repeatable.",
    )

    s_list = sub.add_parser("list", help="List current overrides.")
    s_list.add_argument("--mode", choices=("merge_to", "keep_separate", "confirm_winner"), default=None)
    s_list.add_argument(
        "--pub-id",
        action="append",
        help="Optional pub_id filter (pub_id, comma list, or CSV path). Repeatable.",
    )
    s_list.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include inactive durable decisions in audit output.",
    )

    sub.add_parser(
        "backfill-current",
        help="Create durable decisions from existing current pub_id overrides.",
    )

    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
    )

    with db_conn(args.dsn) as conn:
        _ensure_table(conn, dry_run=args.dry_run)
        has_log_table = _table_exists(conn, "biblio.manual_corrections_log")
        if not has_log_table:
            LOGGER.debug("biblio.manual_corrections_log not found; skipping correction logging.")

        processed = 0
        if args.command == "set-merge":
            if args.csv:
                rows = list(_iter_merge_rows_from_csv(args.csv))
                for loser_pub_id, winner_pub_id, note in rows:
                    _set_merge_one(
                        conn,
                        loser_pub_id=loser_pub_id,
                        winner_pub_id=winner_pub_id,
                        note=(note if note is not None else args.note),
                        has_log_table=has_log_table,
                        applied_by=args.applied_by,
                        dry_run=args.dry_run,
                    )
                processed = len(rows)
            else:
                if args.loser_pub_id is None or args.winner_pub_id is None:
                    raise SystemExit("set-merge requires --loser-pub-id and --winner-pub-id (or --csv).")
                _set_merge_one(
                    conn,
                    loser_pub_id=args.loser_pub_id,
                    winner_pub_id=args.winner_pub_id,
                    note=args.note,
                    has_log_table=has_log_table,
                    applied_by=args.applied_by,
                    dry_run=args.dry_run,
                )
                processed = 1
        elif args.command == "set-keep-separate":
            pub_ids = _flatten_pub_ids(args.pub_id)
            _set_keep_separate(
                conn,
                pub_ids=pub_ids,
                note=args.note,
                has_log_table=has_log_table,
                applied_by=args.applied_by,
                dry_run=args.dry_run,
            )
            processed = len(pub_ids)
        elif args.command == "confirm-winner":
            _confirm_winner(
                conn,
                winner_pub_id=args.winner_pub_id,
                note=args.note,
                has_log_table=has_log_table,
                applied_by=args.applied_by,
                dry_run=args.dry_run,
            )
            processed = 1
        elif args.command == "clear":
            pub_ids = _flatten_pub_ids(args.pub_id) if args.pub_id else []
            _clear_overrides(
                conn,
                pub_ids=pub_ids,
                decision_ids=args.decision_id or [],
                has_log_table=has_log_table,
                applied_by=args.applied_by,
                dry_run=args.dry_run,
            )
            processed = len(pub_ids) + len(args.decision_id or [])
        elif args.command == "list":
            pub_ids = _flatten_pub_ids(args.pub_id) if args.pub_id else None
            _list_overrides(
                conn,
                mode=args.mode,
                pub_ids=pub_ids,
                include_inactive=args.include_inactive,
            )
        elif args.command == "backfill-current":
            processed = _backfill_current_overrides(
                conn,
                has_log_table=has_log_table,
                applied_by=args.applied_by,
                dry_run=args.dry_run,
            )
        else:
            raise SystemExit(f"Unknown command: {args.command}")

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

        if args.command != "list":
            LOGGER.info(
                "publication_dedup_overrides summary: command=%s processed=%s dry_run=%s errors=0",
                args.command,
                processed,
                args.dry_run,
            )


if __name__ == "__main__":
    main()
