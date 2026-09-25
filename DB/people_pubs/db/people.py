"""
people_pubs.db.people

DB helpers for app.people, identities, employments, aliases, and ORCID state.
Used by sync scripts to resolve internal people and maintain profile data.
"""

# people_pubs/db/people.py
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
import difflib
from typing import Any, Dict, List, Optional


import psycopg
from psycopg.rows import dict_row

from ..orcidkit import OrcidClient, ascii_fold, strip_titles
from ..config import NAME_MATCH_MIN_SCORE

# Simple in-memory cache for person_id -> ORCID
_ORCID_CACHE: Dict[int, Optional[str]] = {}


def parse_flexible_date(s: Optional[str]) -> Optional[date]:
    """
    Accept 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD' and convert to a date.

    - 'YYYY'        -> YYYY-01-01
    - 'YYYY-MM'     -> YYYY-MM-01
    - 'YYYY-MM-DD'  -> as-is

    Anything else -> None
    """
    if not s:
        return None
    s = s.strip()
    if not s:
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return date.fromisoformat(s + "-01")
    if re.fullmatch(r"\d{4}", s):
        return date.fromisoformat(s + "-01-01")
    return None


def merge_emails(existing: Optional[Any], new_verified: List[str]) -> List[str]:
    """
    Merge existing PII emails (from citext[] or legacy string) with new ORCID verified emails.

    Behaviour mirrors update_orcid_db.merge_emails:

    - `existing` can be a citext[], a semicolon-separated string, or None
    - de-duplicate, case-insensitive
    - keep original casing of the first occurrence of each email
    """
    out: List[str] = []
    seen = set()

    def add(e: str) -> None:
        if not e:
            return
        norm = e.strip().lower()
        if not norm:
            return
        if norm in seen:
            return
        seen.add(norm)
        out.append(e.strip())

    # tolerate legacy placeholder strings like "emails"
    if isinstance(existing, str):
        low = existing.strip().lower()
        if low not in {"emails", "email", "none", "n/a", "[]"}:
            for part in re.split(r"[;,]", existing):
                add(part)
    else:
        for e in (existing or []):
            add(str(e))

    for e in new_verified:
        add(e)

    return out


def pick_primary_email(
    existing_primary: Optional[str],
    emails: List[str],
) -> Optional[str]:
    """
    Decide which address should be primary.

    Deliberately domain-neutral: no organisation, domain or top-level domain
    is preferred. Which address matters is a deployment question, not one this
    code should answer.

    - If an existing, non-placeholder primary_email is set, keep it.
    - Otherwise take the first address in the given order. `merge_emails`
      normalizes and de-duplicates while preserving input order, so this is
      the first normalized address the caller supplied.

    The same rule is implemented in SQL by pii.merge_people_verified_emails
    and pii.ensure_people_pii; keep the two in step.
    """
    if existing_primary:
        p = existing_primary.strip()
        if p and p.lower() not in {"primary_email", "email", "none", "n/a"}:
            return existing_primary

    return emails[0] if emails else None


def sync_orcid_names_for_person(
    conn: psycopg.Connection,
    oc: OrcidClient,
    person_id: int,
    orcid: str,
    dry_run: bool = False,
) -> None:
    """
    Ensure app.identities_orcid has ORCID-derived name fields filled
    for this person:
      - orcid_first_name
      - orcid_last_name
      - orcid_display_name
    """
    logging.debug("  syncing ORCID names for person_id=%s orcid=%s", person_id, orcid)

    try:
        record = oc.get_record(orcid)
    except Exception as e:
        logging.warning("  could not fetch ORCID record for %s: %s", orcid, e)
        return

    if not record:
        return

    person = record.get("person") or {}
    name_block = person.get("name") or {}

    def _get_name_part(block: Dict[str, Any], key: str) -> Optional[str]:
        v = block.get(key) or {}
        if isinstance(v, dict):
            return (v.get("value") or "").strip() or None
        if isinstance(v, str):
            return v.strip() or None
        return None

    given = _get_name_part(name_block, "given-names")
    family = _get_name_part(name_block, "family-name")
    credit = _get_name_part(name_block, "credit-name")

    display = credit or " ".join(x for x in (given, family) if x)
    if display:
        display = display.strip()

    if dry_run:
        logging.info(
            "  [dry-run] would set ORCID names for person_id=%s: given=%r family=%r display=%r",
            person_id,
            given,
            family,
            display,
        )
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.identities_orcid
               SET orcid_first_name   = COALESCE(NULLIF(orcid_first_name, ''), %s),
                   orcid_last_name    = COALESCE(NULLIF(orcid_last_name, ''), %s),
                   orcid_display_name = COALESCE(NULLIF(orcid_display_name, ''), %s),
                   updated_at         = now()
             WHERE person_id = %s
            """,
            (given, family, display, person_id),
        )


def load_people_due_for_orcid_profile_refresh(
    conn: psycopg.Connection,
    max_age_days: Optional[int],
    max_age_months: Optional[int],
    limit: int,
    only_person_id: Optional[int],
    only_orcid: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Decide which people need an ORCID profile refresh.

    This is the same query as update_orcid_db.load_due_people.
    """
    sql = """
        SELECT
          p.person_id,
          p.display_name,
          io.orcid,
          prs.last_orcid_profile_at
        FROM app.identities_orcid io
        JOIN app.people p
          ON p.person_id = io.person_id
        LEFT JOIN activity.person_refresh_state prs
          ON prs.person_id = p.person_id
        WHERE io.orcid IS NOT NULL
          AND io.orcid <> ''
    """
    params: List[Any] = []

    if only_person_id is not None:
        sql += " AND p.person_id = %s"
        params.append(only_person_id)

    if only_orcid:
        sql += " AND io.orcid = %s"
        params.append(only_orcid)

    if only_person_id is None and not only_orcid:
        if max_age_months is not None:
            sql += """
              AND (
                    prs.last_orcid_profile_at IS NULL
                    OR prs.last_orcid_profile_at < (now() - (%s * interval '1 month'))
                  )
            """
            params.append(max_age_months)
        else:
            effective_days = max_age_days if max_age_days is not None else 180
            sql += """
              AND (
                    prs.last_orcid_profile_at IS NULL
                    OR prs.last_orcid_profile_at < (now() - (%s * interval '1 day'))
                  )
            """
            params.append(effective_days)

    sql += """
        ORDER BY prs.last_orcid_profile_at NULLS FIRST, p.person_id
        LIMIT %s
    """
    params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


def update_people_pii_with_verified_emails(
    conn: psycopg.Connection,
    person_id: int,
    verified_emails: List[str],
) -> None:
    """
    Merge verified ORCID emails into pii.people_pii for this person.

    The write is routed through pii.merge_people_verified_emails(), a narrow
    SECURITY DEFINER function. Routine app_writer jobs should not need direct
    DML grants on pii.people_pii.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT row_found, primary_email, emails
            FROM pii.merge_people_verified_emails(%s, %s::citext[])
            """,
            (person_id, verified_emails),
        )
        row = cur.fetchone()
        if not row:
            logging.warning(
                "  pii.merge_people_verified_emails returned no row for person_id=%s",
                person_id,
            )
            return

        row_found = row.get("row_found") if isinstance(row, dict) else row[0]
        if not row_found:
            logging.warning(
                "  pii.people_pii row missing for person_id=%s; skipping email update",
                person_id,
            )
            return

        new_primary = row.get("primary_email") if isinstance(row, dict) else row[1]
        merged_emails = row.get("emails") if isinstance(row, dict) else row[2]
        logging.debug(
            "  verified emails merged: incoming=%r, after=%r, primary_email=%r",
            verified_emails,
            merged_emails,
            new_primary,
        )


def upsert_employment(
    conn: psycopg.Connection,
    person_id: int,
    emp: Dict[str, Any],
) -> None:
    """
    Upsert a single employment into app.employments.

    Exactly matches the INSERT .. ON CONFLICT block in update_orcid_db.process_person,
    with optional source/raw_json support.
    """
    org_name = emp.get("org_name")
    if not org_name:
        return

    start = parse_flexible_date(emp.get("start_date"))
    end = parse_flexible_date(emp.get("end_date"))
    is_current = bool(emp.get("is_current"))
    dept = emp.get("department")
    role = emp.get("role_title")
    source = emp.get("source") or "orcid"
    raw_json = emp.get("raw_json")

    with conn.cursor() as cur:
        if not start:
            logging.debug(
                "  upserting employment (missing start_date): org=%r, dept=%r, role=%r, end=%r, is_current=%r, source=%r",
                org_name,
                dept,
                role,
                end,
                is_current,
                source,
            )
            cur.execute(
                """
                UPDATE app.employments
                SET department = %s,
                    role_title = %s,
                    end_date   = COALESCE(%s, app.employments.end_date),
                    is_current = %s,
                    source     = COALESCE(app.employments.source, %s),
                    raw_json   = COALESCE(%s, app.employments.raw_json)
                WHERE app.employments.person_id = %s
                  AND app.employments.org_name = %s
                  AND app.employments.start_date IS NULL
                """,
                (dept, role, end, is_current, source, raw_json, person_id, org_name),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO app.employments
                        (person_id, org_name, department, role_title, start_date, end_date, is_current, source, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (person_id, org_name, dept, role, None, end, is_current, source, raw_json),
                )
            return

        logging.debug(
            "  upserting employment: org=%r, dept=%r, role=%r, start=%r, end=%r, is_current=%r, source=%r",
            org_name,
            dept,
            role,
            start,
            end,
            is_current,
            source,
        )

        cur.execute(
            """
            INSERT INTO app.employments
                (person_id, org_name, department, role_title, start_date, end_date, is_current, source, raw_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (person_id, org_name, start_date) DO UPDATE
            SET department = EXCLUDED.department,
                role_title = EXCLUDED.role_title,
                end_date   = COALESCE(EXCLUDED.end_date, app.employments.end_date),
                is_current = EXCLUDED.is_current,
                raw_json   = COALESCE(EXCLUDED.raw_json, app.employments.raw_json)
            """,
            (person_id, org_name, dept, role, start, end, is_current, source, raw_json),
        )


def update_orcid_profile_refresh_state(
    conn: psycopg.Connection,
    person_id: int,
) -> None:
    """
    Set activity.person_refresh_state.last_orcid_profile_at = now() for this person.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO activity.person_refresh_state (
              person_id, last_orcid_profile_at, updated_at
            )
            VALUES (%s, now(), now())
            ON CONFLICT (person_id) DO UPDATE
            SET last_orcid_profile_at = EXCLUDED.last_orcid_profile_at,
                updated_at            = now()
            """,
            (person_id,),
        )

def _normalize_alias_name(name: str) -> str:
    s = (name or "").strip().strip('"').strip("'").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _alias_variants(raw: str) -> List[str]:
    s = _normalize_alias_name(raw)
    if not s:
        return []
    out = [s]
    # Add "First Last" variant for Zotero-style "Last, First"
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) >= 2:
            last, first = parts[0], parts[1]
            swapped = _normalize_alias_name(f"{first} {last}")
            if swapped and swapped.lower() != s.lower():
                out.append(swapped)
    # de-dup case-insensitive
    seen = set()
    deduped: List[str] = []
    for a in out:
        k = a.lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(a)
    return deduped

def upsert_person_name_alias(
    conn: psycopg.Connection,
    person_id: int,
    alias_name: str,
    *,
    source: str = "manual_csv.author_name",
    dry_run: bool = False,
) -> None:
    """
    Insert a name alias for a person into app.person_name_aliases_manual.

    This is intentionally conservative and idempotent:
      - skips empty / placeholder aliases
      - inserts only if the (person_id, alias_name) pair does not already exist,
        using a case-insensitive check on alias_name.

    Note: app.person_name_aliases is a GROUP BY view (not insertable). We write to
    app.person_name_aliases_manual, and the view should UNION it in.
    """
    for candidate in _alias_variants(alias_name):
        cand = _normalize_alias_name(candidate)
        if not cand:
            continue
        low = cand.lower()
        if low in {"none", "n/a", "unknown", "author", "authors"}:
            continue

        if dry_run:
            logging.info(
                "  [dry-run] would add alias for person_id=%s: %r (source=%s)",
                person_id,
                cand,
                source,
            )
            continue

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.person_name_aliases_manual
                  (person_id, alias_name, alias_sources, created_at, updated_at)
                VALUES
                  (%s, %s, ARRAY[%s]::text[], now(), now())
                ON CONFLICT (person_id, lower(alias_name)) DO UPDATE
                SET alias_sources = (
                      SELECT ARRAY(
                        SELECT DISTINCT s
                        FROM unnest(
                          COALESCE(app.person_name_aliases_manual.alias_sources, '{}'::text[])
                          || EXCLUDED.alias_sources
                        ) AS s
                        ORDER BY s
                      )
                    ),
                    updated_at = now()
                """,
                (person_id, cand, source),
            )



# --------------------------------------------------------------------
# Shared name / person matching for ORCID works, Scholar, OpenAlex
# --------------------------------------------------------------------


def names_equivalent(a: str, b: str) -> bool:
    """
    Case/diacritic-insensitive name comparison, tolerant of middle names/initials.

    Heuristics:
      - normalize with ascii_fold + strip_titles
      - compare last names
      - compare first-name token or at least first-letter initial
    """

    def _tokens(x: str) -> List[str]:
        x = ascii_fold(strip_titles(x or "")).lower().strip()
        x = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u00ad]", "-", x)
        x = re.sub(r"[.,;]", " ", x)
        return [t for t in x.split() if t]

    ta = _tokens(a)
    tb = _tokens(b)
    if not ta or not tb:
        return False

    # last-name match
    if ta[-1] != tb[-1]:
        return False

    # first-name or initial match
    if ta[0] == tb[0]:
        return True
    return ta[0][0] == tb[0][0]


_UNICODE_DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u00ad]")


def _normalize_match_name(name: str) -> str:
    s = ascii_fold(strip_titles(name or "")).lower().strip()
    if not s:
        return ""
    s = _UNICODE_DASH_RE.sub("-", s)
    s = re.sub(r"[.,;]", " ", s)
    s = re.sub(r"[-/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def name_similarity_score(a: str, b: str) -> float:
    """
    Return a 0..1 similarity score for two names.

    We require the last token to match to avoid unrelated matches.
    """
    na = _normalize_match_name(a)
    nb = _normalize_match_name(b)
    if not na or not nb:
        return 0.0
    ta = na.split()
    tb = nb.split()
    if not ta or not tb:
        return 0.0
    if ta[-1] != tb[-1]:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _normalize_candidate_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    s = _UNICODE_DASH_RE.sub("-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _name_candidates(full_name: str) -> List[str]:
    raw = (full_name or "").strip()
    if not raw:
        return []

    out: List[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = (s or "").strip()
        if not s:
            return
        key = s.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(s)

    add(raw)
    add(_normalize_candidate_name(raw))

    for base in list(out):
        if "," in base:
            parts = [p.strip() for p in base.split(",") if p.strip()]
            if len(parts) >= 2:
                swapped = f"{parts[1]} {parts[0]}".strip()
                add(swapped)
                add(_normalize_candidate_name(swapped))

    return out


def person_is_internal(
    conn: psycopg.Connection,
    person_id: Optional[int],
) -> bool:
    """Is this person classified internal in app.people?

    `biblio.authorships.is_internal_at_ingest` is documented as a snapshot of
    exactly this classification, so every writer of that flag must ask here
    rather than assume that a linked person is internal. PubDex deliberately
    links external co-authors to person rows too, and an external co-author
    must never be reported as one of an organisation's own authors.
    """
    if person_id is None:
        return False
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT person_kind FROM app.people WHERE person_id = %s",
            (person_id,),
        )
        row = cur.fetchone()
    if not row:
        return False
    return str(row["person_kind"]) == "internal"


def lookup_internal_person_for_author(
    conn: psycopg.Connection,
    full_name: str,
    auth_orcid: Optional[str],
) -> Optional[int]:
    """
    Try to map an arbitrary author (name + optional ORCID) to an internal person.

    Heuristic:
      1) If we have an ORCID, look up app.identities_orcid.
      2) Otherwise, fall back to a unique (case-insensitive) display_name match.
         We only accept a match if it is unique AND names_equivalent() says yes,
         and the name similarity score >= NAME_MATCH_MIN_SCORE.
      3) As a fallback, try app.person_name_aliases.
    """
    if not full_name and not auth_orcid:
        return None

    # 1) ORCID match
    if auth_orcid:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT io.person_id
                FROM app.identities_orcid io
                WHERE io.orcid = %s
                """,
                (auth_orcid,),
            )
            row = cur.fetchone()
        if row:
            return row["person_id"]

    # 2) Unique display_name match
    if full_name:
        for candidate_name in _name_candidates(full_name):
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT person_id, display_name
                    FROM app.people
                    WHERE lower(display_name) = lower(%s)
                    """,
                    (candidate_name,),
                )
                rows = cur.fetchall()

            if len(rows) == 1:
                candidate = rows[0]
                score = name_similarity_score(candidate_name, candidate["display_name"])
                if score >= NAME_MATCH_MIN_SCORE and names_equivalent(
                    candidate_name, candidate["display_name"]
                ):
                    return candidate["person_id"]

        # 3) app.person_name_aliases
        for candidate_name in _name_candidates(full_name):
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT pa.person_id, p.display_name
                    FROM app.person_name_aliases pa
                    JOIN app.people p USING (person_id)
                    WHERE lower(pa.alias_name) = lower(%s)
                    GROUP BY pa.person_id, p.display_name
                    """,
                    (candidate_name,),
                )
                alias_rows = cur.fetchall()

            if len(alias_rows) == 1:
                candidate = alias_rows[0]
                score = name_similarity_score(candidate_name, candidate["display_name"])
                if score >= NAME_MATCH_MIN_SCORE and names_equivalent(
                    candidate_name, candidate["display_name"]
                ):
                    return candidate["person_id"]

    return None


def lookup_orcid_for_person_id(
    conn: psycopg.Connection,
    person_id: int,
) -> Optional[str]:
    """
    Given an internal person_id, return their canonical ORCID from
    app.identities_orcid, if any. Uses a simple in-memory cache to avoid
    hammering the DB when the same person appears on many papers.
    """
    if person_id in _ORCID_CACHE:
        return _ORCID_CACHE[person_id]

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT io.orcid
            FROM app.identities_orcid io
            WHERE io.person_id = %s
            """,
            (person_id,),
        )
        row = cur.fetchone()

    if not row:
        _ORCID_CACHE[person_id] = None
        return None

    val = (row["orcid"] or "").strip()
    _ORCID_CACHE[person_id] = val or None
    return _ORCID_CACHE[person_id]


def load_orcid_people(
    conn: psycopg.Connection,
    only_person_id: Optional[int],
    only_orcid: Optional[str],
    limit_people: Optional[int],
    refreshed_before: Optional[date],
) -> List[Dict[str, Any]]:
    """
    Fetch people with ORCID from app.identities_orcid + app.people.

    Mirrors update_publications_from_orcid.load_orcid_people.
    """
    sql = """
      SELECT
        p.person_id,
        p.display_name,
        io.orcid,
        prs.last_orcid_pub_refresh_at
      FROM app.identities_orcid io
      JOIN app.people p
        ON p.person_id = io.person_id
      LEFT JOIN activity.person_refresh_state prs
        ON prs.person_id = p.person_id
      WHERE io.orcid IS NOT NULL
        AND io.orcid <> ''
    """
    params: List[Any] = []

    if only_person_id is not None:
        sql += " AND p.person_id = %s"
        params.append(only_person_id)

    if only_orcid:
        sql += " AND io.orcid = %s"
        params.append(only_orcid)

    if refreshed_before is not None and only_person_id is None and not only_orcid:
        sql += " AND (prs.last_orcid_pub_refresh_at IS NULL OR prs.last_orcid_pub_refresh_at < %s)"
        params.append(refreshed_before)

    sql += " ORDER BY p.person_id"

    if limit_people and limit_people > 0:
        sql += " LIMIT %s"
        params.append(limit_people)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


def mark_person_orcid_refreshed(conn: psycopg.Connection, person_id: int) -> None:
    """
    Record that we have just refreshed ORCID publications for this person.

    Writes activity.person_refresh_state.last_orcid_pub_refresh_at.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO activity.person_refresh_state (person_id, last_orcid_pub_refresh_at)
            VALUES (%s, now())
            ON CONFLICT (person_id) DO UPDATE
            SET last_orcid_pub_refresh_at = EXCLUDED.last_orcid_pub_refresh_at
            """,
            (person_id,),
        )
