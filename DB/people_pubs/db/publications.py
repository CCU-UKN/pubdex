"""
people_pubs.db.publications

DB helpers for biblio.publications.
Includes upsert_publication and raw_json merge utilities.
"""

# people_pubs/db/publications.py
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from people_pubs import __version__ as _PEOPLE_PUBS_VERSION
from people_pubs.services.crossref_client import normalize_doi


TRANSFORM_VERSION = f"people_pubs/{_PEOPLE_PUBS_VERSION}"


_RAW_JSON_KEYS = {
    "crossref",
    "orcid",
    "scholar",
    "manual",
    "manual_csv",
    "openalex",
    "dblp",
    "datacite",
    "semantic",
    "legacy",
}

_SOURCE_ORDER = [
    "manual",
    "crossref",
    "datacite",
    "semantic",
    "orcid",
    "scholar",
    "dblp",
    "openalex",
]

_SOURCE_FAMILIES = [
    "crossref",
    "datacite",
    "semantic",
    "orcid",
    "scholar",
    "dblp",
    "openalex",
]

_RAW_SOURCE_FIELDS = {
    "crossref": "raw_crossref_json",
    "orcid": "raw_orcid_json",
    "scholar": "raw_scholar_json",
    "openalex": "raw_openalex_json",
    "dblp": "raw_dblp_json",
    "datacite": "raw_datacite_json",
    "semantic": "raw_semantic_json",
}

_RAW_KEY_SOURCE_TOKENS = {
    "crossref": "crossref",
    "orcid": "orcid",
    "scholar": "scholar",
    "manual": "manual",
    "manual_csv": "manual",
    "openalex": "openalex",
    "dblp": "dblp",
    "datacite": "datacite",
    "semantic": "semantic",
}


def _split_source_tokens(value: str) -> List[str]:
    return [p.strip() for p in (value or "").split("+") if p.strip()]


def _is_family_specific_token(token: str, family: str) -> bool:
    token = (token or "").strip().lower()
    family = (family or "").strip().lower()
    return bool(token and family and token.startswith(f"{family}-"))


def _is_family_funding_token(token: str, family: str) -> bool:
    return _is_family_specific_token(token, family) and "funding" in token.lower()


def merge_source_of_truth(existing: str, add: str) -> str:
    """
    Merge cumulative publication provenance tokens in a stable order.

    Generic family tokens are dropped when a family-specific token is already
    present, so reruns cannot downgrade e.g. `crossref-funding` to plain
    `crossref`.

    Tokens outside the recognised source families are kept, appended in sorted
    order after the known ones, but they get no family-specific handling: a
    generic token is not dropped next to a qualified token of the same
    unrecognised family.
    """
    tokens = _split_source_tokens(existing) + _split_source_tokens(add)
    if not tokens:
        return ""

    non_unknown = [t for t in tokens if t != "unknown"]
    if non_unknown:
        tokens = non_unknown

    seen = set()
    deduped: List[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            deduped.append(token)

    token_set = set(deduped)
    for family in _SOURCE_FAMILIES:
        if family in token_set and any(
            _is_family_specific_token(token, family) for token in token_set
        ):
            token_set.discard(family)

    ordered: List[str] = []
    for token in _SOURCE_ORDER:
        if token in token_set:
            ordered.append(token)
            token_set.remove(token)
    ordered.extend(sorted(token_set))
    return "+".join(ordered) if ordered else "unknown"


def merge_source_of_truth_replace_family(existing: str, add: str, family: str) -> str:
    """
    Merge source_of_truth while intentionally replacing one source family.
    """
    existing = (existing or "").strip()
    family = (family or "").strip()
    add = (add or "").strip()

    tokens = _split_source_tokens(existing)
    family_tokens = [t for t in tokens if t == family or t.startswith(f"{family}-")]

    if add == family:
        family_specific_tokens = [t for t in family_tokens if t != family]
        replacement_tokens = family_specific_tokens or ([add] if add else [])
    elif add.startswith(f"{family}-"):
        add_is_funding = _is_family_funding_token(add, family)
        family_funding_tokens = [
            t for t in family_tokens if _is_family_funding_token(t, family)
        ]
        family_non_funding_tokens = [
            t for t in family_tokens if not _is_family_funding_token(t, family)
        ]
        if add_is_funding:
            replacement_tokens = family_non_funding_tokens + ([add] if add else [])
        else:
            replacement_tokens = family_funding_tokens + ([add] if add else [])
    else:
        replacement_tokens = [add] if add else []

    filtered = [t for t in tokens if not (t == family or t.startswith(f"{family}-"))]
    out = "+".join(filtered)
    for token in replacement_tokens:
        out = merge_source_of_truth(out, token)
    return out


def normalize_raw_json(existing_raw: Any, existing_sot: str = "") -> Dict[str, Any]:
    """
    Normalize legacy raw_json into a dict keyed by source (crossref/orcid/openalex/...).

    - If existing_raw already has known keys, preserve as-is (keys outside the
      recognised source set are kept alongside them).
    - If a single recognised source_of_truth token is present, wrap under that key.
    - Otherwise, stash under "legacy" to avoid losing data. This includes a
      payload keyed only by keys outside the recognised source set, even when
      source_of_truth names that key.
    """
    if not isinstance(existing_raw, dict):
        return {}

    if any(k in existing_raw for k in _RAW_JSON_KEYS):
        return dict(existing_raw)

    tokens = [t.strip() for t in (existing_sot or "").split("+") if t.strip()]
    if len(tokens) == 1 and tokens[0] in _RAW_JSON_KEYS:
        return {tokens[0]: existing_raw}

    return {"legacy": existing_raw}


def merge_raw_json(
    existing_raw: Any,
    existing_sot: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Merge new source payloads into existing raw_json, keyed by source name.
    """
    merged = normalize_raw_json(existing_raw, existing_sot)
    for key, payload in updates.items():
        if payload is None:
            continue
        merged[key] = payload
    return merged


def payload_sha256(payload: Any) -> str:
    """
    SHA-256 of a source payload over its canonical JSON form (sorted keys,
    compact separators), so the hash is stable across dict key order and a
    re-fetch of identical content hashes identically.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_source_provenance(
    raw_updates: Dict[str, Any],
    fetched_at: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Per-source ingest envelope for the payloads written in this call:
    fetch/import timestamp, transform version, payload hash.

    Stored in biblio.publications.source_provenance keyed by the same source
    families as raw_json. Exports and the API may expose this envelope; raw
    payloads themselves never leave the database through those surfaces.
    """
    stamp = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        key: {
            "fetched_at": stamp,
            "transform_version": TRANSFORM_VERSION,
            "payload_sha256": payload_sha256(payload),
        }
        for key, payload in raw_updates.items()
        if payload is not None
    }


def merge_source_provenance(existing: Any, updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge new envelope entries over existing ones, keyed by source family —
    the envelope counterpart of merge_raw_json.
    """
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, entry in updates.items():
        if entry is not None:
            merged[key] = entry
    return merged


def merge_publication_source_provenance(
    winner_raw: Any,
    winner_prov: Any,
    loser_raw: Any,
    loser_prov: Any,
    winner_sot: str = "",
    loser_sot: str = "",
) -> Dict[str, Any]:
    """
    Envelope counterpart of merge_publication_provenance for duplicate merges:
    entries follow the payloads. The merged envelope keeps the winner's entry
    for every source key the winner kept and adopts the loser's entry only for
    the gap keys the loser contributed, so no envelope entry ever describes a
    payload that was dropped in the merge.

    Keys are compared after normalize_raw_json. A payload keyed only by keys
    outside the recognised source set is normalized under "legacy", so its
    envelope entry, which is still keyed by the original key, is not carried
    over; the payload itself survives the merge under "legacy".
    """
    winner_norm = normalize_raw_json(winner_raw, winner_sot)
    loser_norm = normalize_raw_json(loser_raw, loser_sot)
    winner_prov = winner_prov if isinstance(winner_prov, dict) else {}
    loser_prov = loser_prov if isinstance(loser_prov, dict) else {}
    merged: Dict[str, Any] = {}
    for key in winner_norm:
        if key in winner_prov:
            merged[key] = winner_prov[key]
    for key in loser_norm:
        if key not in winner_norm and key in loser_prov:
            merged[key] = loser_prov[key]
    return merged


def merge_publication_provenance(
    winner_sot: Optional[str],
    winner_raw: Any,
    loser_sot: Optional[str],
    loser_raw: Any,
) -> Tuple[str, Dict[str, Any]]:
    """
    Fold a losing duplicate's provenance into the winner on a DOI merge.

    Both duplicate-merge paths call this — the backfill DOI-collision path
    (``crossref_backfill.merge_publications``, shared by every source backfill)
    and the manual ``merge_publication_duplicates`` tool — so a merge never
    silently drops the loser's provenance regardless of which row won:

    - ``source_of_truth`` tokens are unioned via ``merge_source_of_truth``, so
      family-specific tokens such as ``crossref-funding`` / ``openalex-affiliation``
      survive and keep precedence over the generic family token even when the
      winner was chosen purely by trust order.
    - ``raw_json`` is merged per source key with the winner winning conflicts;
      the loser only contributes source keys the winner lacks, so no source
      payload is lost.
    """
    w_sot = winner_sot or ""
    l_sot = loser_sot or ""
    merged_sot = merge_source_of_truth(w_sot, l_sot) or "unknown"

    winner_norm = normalize_raw_json(winner_raw, w_sot)
    loser_norm = normalize_raw_json(loser_raw, l_sot)
    gap_updates = {
        key: payload
        for key, payload in loser_norm.items()
        if key not in winner_norm and payload is not None
    }
    merged_raw = merge_raw_json(winner_raw, w_sot, gap_updates)
    return merged_sot, merged_raw


def _raw_payload_is_source_keyed(payload: Any) -> bool:
    return isinstance(payload, dict) and any(k in payload for k in _RAW_JSON_KEYS)


def _raw_json_updates_from_pub(pub: Dict[str, Any]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}

    explicit_raw = pub.get("raw_json")
    if explicit_raw is not None:
        updates.update(normalize_raw_json(explicit_raw, str(pub.get("source_of_truth") or "")))

    for source_key, field_name in _RAW_SOURCE_FIELDS.items():
        payload = pub.get(field_name)
        if payload is None:
            continue
        if _raw_payload_is_source_keyed(payload):
            updates.update(normalize_raw_json(payload, str(pub.get("source_of_truth") or "")))
        else:
            updates[source_key] = payload

    return updates


def _source_of_truth_from_pub(pub: Dict[str, Any], raw_updates: Dict[str, Any]) -> str:
    explicit = str(pub.get("source_of_truth") or "").strip()
    if explicit:
        return merge_source_of_truth("", explicit) or "unknown"

    source_tokens = []
    for raw_key in raw_updates:
        token = _RAW_KEY_SOURCE_TOKENS.get(raw_key)
        if token:
            source_tokens.append(token)
    return merge_source_of_truth("", "+".join(source_tokens)) or "unknown"


def _prepare_publication_row(
    pub: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    doi_raw = (pub.get("doi") or "").strip().lower()
    doi = normalize_doi(doi_raw)
    if not doi:
        raise ValueError("empty normalized DOI")

    existing = existing or {}
    title = (pub.get("title") or "").strip() or existing.get("title")
    year = pub.get("year") if pub.get("year") is not None else existing.get("year")
    venue = (pub.get("venue") or pub.get("journal_name")) or existing.get("venue")
    raw_updates = _raw_json_updates_from_pub(pub)
    incoming_sot = _source_of_truth_from_pub(pub, raw_updates)

    existing_sot = str(existing.get("source_of_truth") or "")
    existing_raw = existing.get("raw_json")
    replace_sot = bool(
        pub.get("replace_source_of_truth") or pub.get("rewrite_source_of_truth")
    )
    if existing and not replace_sot:
        source_of_truth = merge_source_of_truth(existing_sot, incoming_sot) or "unknown"
    else:
        source_of_truth = incoming_sot or "unknown"

    if existing_raw is not None or raw_updates:
        raw_json = merge_raw_json(existing_raw, existing_sot, raw_updates)
    else:
        raw_json = None

    existing_prov = existing.get("source_provenance")
    prov_updates = build_source_provenance(raw_updates)
    if isinstance(existing_prov, dict) or prov_updates:
        source_provenance: Optional[Dict[str, Any]] = merge_source_provenance(
            existing_prov, prov_updates
        )
    else:
        source_provenance = None

    if "is_preprint" in pub:
        is_preprint: Optional[bool] = bool(pub.get("is_preprint"))
    else:
        is_preprint = existing.get("is_preprint")

    return {
        "doi": doi,
        "title": title,
        "year": year,
        "venue": venue,
        "source_of_truth": source_of_truth,
        "raw_json": raw_json,
        "source_provenance": source_provenance,
        "is_preprint": is_preprint,
    }


def upsert_publication(
    conn: psycopg.Connection,
    pub: Dict[str, Any],
    dry_run: bool = False,
) -> Optional[int]:
    """
    Insert or update biblio.publications row.

    Schema assumed:

        pub_id (PK) | doi | title | year | venue | source_of_truth | raw_json | source_provenance | is_preprint | created_at | updated_at

    Expected keys in `pub`:
      - doi                (str, REQUIRED – we skip rows without it)
      - title              (str)
      - year               (int)
      - venue              (str, optional)
      - journal_name       (str, optional – used as venue fallback)
      - raw_crossref_json  (dict, optional)
      - raw_orcid_json     (dict, optional)
      - raw_scholar_json   (dict, optional)
      - raw_openalex_json  (dict, optional)
      - raw_dblp_json      (dict, optional)
      - raw_datacite_json  (dict, optional)
      - raw_semantic_json  (dict, optional)
      - is_preprint        (bool, optional)
      - source_of_truth    (str, optional override; if absent we infer from
                            which raw_*_json blobs are present)
    """
    try:
        initial_row = _prepare_publication_row(pub)
    except ValueError:
        logging.warning(
            "upsert_publication: skipping publication with empty DOI "
            "(title=%r, source_hint=%r)",
            pub.get("title"),
            pub.get("source_of_truth") or pub.get("source"),
        )
        return None

    if dry_run:
        logging.info(
            "  [dry-run] would upsert publication doi=%s title=%r year=%r venue=%r source_of_truth=%r is_preprint=%r",
            initial_row["doi"],
            initial_row["title"],
            initial_row["year"],
            initial_row["venue"],
            initial_row["source_of_truth"],
            initial_row["is_preprint"],
        )
        return None

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT pub_id, title, year, venue, source_of_truth, raw_json,
                   source_provenance, is_preprint
            FROM biblio.publications
            WHERE doi = %s
            """,
            (initial_row["doi"],),
        )
        existing = cur.fetchone()
        row = _prepare_publication_row(
            pub,
            dict(existing) if existing is not None else None,
        )
        if isinstance(row["raw_json"], dict):
            row["raw_json"] = Json(row["raw_json"])
        if isinstance(row["source_provenance"], dict):
            row["source_provenance"] = Json(row["source_provenance"])
        cur.execute(
            """
            INSERT INTO biblio.publications (
                doi,
                title,
                year,
                venue,
                source_of_truth,
                raw_json,
                source_provenance,
                is_preprint,
                created_at,
                updated_at
            )
            VALUES (
                %(doi)s,
                %(title)s,
                %(year)s,
                %(venue)s,
                %(source_of_truth)s,
                %(raw_json)s,
                %(source_provenance)s,
                %(is_preprint)s,
                now(),
                now()
            )
            ON CONFLICT (doi) DO UPDATE
            SET
                title           = COALESCE(NULLIF(EXCLUDED.title, ''), biblio.publications.title),
                year            = COALESCE(EXCLUDED.year, biblio.publications.year),
                venue           = COALESCE(NULLIF(EXCLUDED.venue, ''), biblio.publications.venue),
                source_of_truth = EXCLUDED.source_of_truth,
                raw_json        = COALESCE(NULLIF(EXCLUDED.raw_json, '{}'::jsonb), biblio.publications.raw_json),
                source_provenance = COALESCE(NULLIF(EXCLUDED.source_provenance, '{}'::jsonb), biblio.publications.source_provenance),
                is_preprint     = COALESCE(EXCLUDED.is_preprint, biblio.publications.is_preprint),
                updated_at      = now()
            RETURNING pub_id
            """,
            row,
        )
        db_row = cur.fetchone()
        publication_id = db_row["pub_id"]

    logging.info("  upserted publication doi=%s (id=%s)", row["doi"], publication_id)
    return publication_id
