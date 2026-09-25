"""Shared publication canonical-choice policy.

Automatic merge paths should use this module instead of carrying local source
or preprint scoring. Manual durable decisions still live in the database and
take precedence in views; callers can also pass a known durable winner when
they have resolved one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from people_pubs.config import source_rank
from people_pubs.services.crossref_client import normalize_doi


@dataclass(frozen=True)
class PublicationCandidate:
    pub_id: int
    doi: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    source_of_truth: str = "unknown"
    raw_json: Any = None
    is_preprint: Optional[bool] = None


@dataclass(frozen=True)
class CanonicalChoice:
    winner_pub_id: int
    loser_pub_ids: tuple[int, ...]
    reason: str
    winner_score: tuple[int, ...]


def _get_value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def publication_candidate_from(row: Any) -> PublicationCandidate:
    return PublicationCandidate(
        pub_id=int(_get_value(row, "pub_id")),
        doi=_get_value(row, "doi"),
        title=_get_value(row, "title"),
        year=_get_value(row, "year"),
        venue=_get_value(row, "venue"),
        source_of_truth=_get_value(row, "source_of_truth", "unknown") or "unknown",
        raw_json=_get_value(row, "raw_json"),
        is_preprint=_get_value(row, "is_preprint"),
    )


def source_tokens(source_of_truth: Optional[str]) -> list[str]:
    return [p.strip().lower() for p in (source_of_truth or "").split("+") if p.strip()]


def source_family(token: str) -> str:
    token = (token or "").strip().lower()
    return token.split("-", 1)[0] if token else "unknown"


def _source_policy_score(source_of_truth: Optional[str]) -> tuple[int, int, int]:
    tokens = source_tokens(source_of_truth)
    if not tokens:
        tokens = ["unknown"]
    best_rank = min(source_rank(source_family(t)) for t in tokens)
    specialized = sum(1 for t in tokens if "-" in t)
    return (-best_rank, len(set(tokens)), specialized)


def _candidate_score(candidate: PublicationCandidate) -> tuple[int, ...]:
    # Explicit non-preprint should beat unknown, and unknown should beat a known
    # preprint for automatic merges. Durable manual choices can override this.
    if candidate.is_preprint is False:
        preprint_score = 2
    elif candidate.is_preprint is None:
        preprint_score = 1
    else:
        preprint_score = 0

    doi_score = 1 if normalize_doi(candidate.doi) else 0
    year_score = 1 if candidate.year is not None else 0
    venue_score = 1 if candidate.venue else 0
    source_score = _source_policy_score(candidate.source_of_truth)
    stable_tiebreaker = -int(candidate.pub_id)
    return (
        preprint_score,
        doi_score,
        *source_score,
        year_score,
        venue_score,
        stable_tiebreaker,
    )


def choose_canonical_publication(
    candidates: Sequence[PublicationCandidate] | Iterable[PublicationCandidate],
    *,
    durable_winner_pub_id: Optional[int] = None,
) -> CanonicalChoice:
    rows = list(candidates)
    if not rows:
        raise ValueError("choose_canonical_publication() requires at least one candidate")

    pub_ids = {int(r.pub_id) for r in rows}
    if durable_winner_pub_id is not None and int(durable_winner_pub_id) in pub_ids:
        winner = next(r for r in rows if int(r.pub_id) == int(durable_winner_pub_id))
        losers = tuple(sorted(pid for pid in pub_ids if pid != winner.pub_id))
        return CanonicalChoice(
            winner_pub_id=winner.pub_id,
            loser_pub_ids=losers,
            reason="durable_manual_decision",
            winner_score=_candidate_score(winner),
        )

    winner = max(rows, key=_candidate_score)
    losers = tuple(sorted(pid for pid in pub_ids if pid != winner.pub_id))
    return CanonicalChoice(
        winner_pub_id=winner.pub_id,
        loser_pub_ids=losers,
        reason="non_preprint_then_source_trust_then_metadata_then_oldest_pub_id",
        winner_score=_candidate_score(winner),
    )
