from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from people_pubs.config import USER_AGENT
from people_pubs.services.crossref_client import normalize_doi


LOGGER = logging.getLogger("people_pubs.semantic_scholar")

SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"
SEMANTIC_SCHOLAR_MAX_RETRIES = 6
SEMANTIC_SCHOLAR_BACKOFF_BASE = 4.0
SEMANTIC_SCHOLAR_BACKOFF_MAX = 20.0
SEMANTIC_SCHOLAR_MIN_INTERVAL = 1.5
SEMANTIC_SCHOLAR_DYNAMIC_MIN_INTERVAL_MAX = 10.0
SEMANTIC_SCHOLAR_DYNAMIC_MIN_INTERVAL_MULTIPLIER = 1.5

DEFAULT_FIELDS = ",".join(
    [
        "title",
        "year",
        "venue",
        "publicationVenue",
        "publicationTypes",
        "publicationDate",
        "externalIds",
        "authors",
        "authors.authorId",
        "authors.name",
        "authors.affiliations",
        "authors.externalIds",
        "isOpenAccess",
    ]
)


def semantic_doi(entry: Dict[str, Any]) -> Optional[str]:
    ext = entry.get("externalIds") if isinstance(entry, dict) else None
    doi = None
    if isinstance(ext, dict):
        doi = ext.get("DOI") or ext.get("doi")
    if not doi:
        doi = entry.get("doi") if isinstance(entry, dict) else None
    return normalize_doi(doi) or (doi or None)


def semantic_title(entry: Dict[str, Any]) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    title = (entry.get("title") or "").strip()
    return title or None


def semantic_year(entry: Dict[str, Any]) -> Optional[int]:
    if not isinstance(entry, dict):
        return None
    year = entry.get("year")
    if isinstance(year, int):
        return year
    date_val = entry.get("publicationDate") or entry.get("publication_date")
    if isinstance(date_val, str) and len(date_val) >= 4 and date_val[:4].isdigit():
        try:
            return int(date_val[:4])
        except Exception:
            return None
    return None


def semantic_venue(entry: Dict[str, Any]) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    venue = (entry.get("venue") or "").strip()
    if venue:
        return venue
    pv = entry.get("publicationVenue")
    if isinstance(pv, dict):
        name = (pv.get("name") or "").strip()
        return name or None
    return None


def semantic_is_preprint(entry: Dict[str, Any]) -> Optional[bool]:
    if not isinstance(entry, dict):
        return None
    types = entry.get("publicationTypes") or []
    if isinstance(types, list):
        for t in types:
            if isinstance(t, str) and "preprint" in t.lower():
                return True
    return None


def _extract_orcid(author: Dict[str, Any]) -> Optional[str]:
    ext = author.get("externalIds")
    if isinstance(ext, dict):
        orcid = ext.get("ORCID") or ext.get("orcid")
        if isinstance(orcid, str) and orcid.strip():
            return orcid.strip()
    orcid = author.get("orcid")
    if isinstance(orcid, str) and orcid.strip():
        return orcid.strip()
    return None


def iter_authors_from_semantic(
    entry: Dict[str, Any],
    *,
    include_department: bool = False,
) -> Iterable[Tuple[str, Optional[str], List[str], bool]]:
    if not isinstance(entry, dict):
        return []
    authors = entry.get("authors") or []
    if not isinstance(authors, list):
        return []
    for a in authors:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").strip()
        if not name:
            continue
        affiliations = a.get("affiliations") or a.get("affiliation") or []
        if isinstance(affiliations, str):
            affiliations = [affiliations]
        if not isinstance(affiliations, list):
            affiliations = []
        affiliations = [str(x).strip() for x in affiliations if str(x).strip()]
        yield (name, _extract_orcid(a), affiliations, False)


def count_authors_with_affiliations(entry: Dict[str, Any]) -> Tuple[int, int]:
    authors = list(iter_authors_from_semantic(entry))
    if not authors:
        return 0, 0
    with_aff = sum(1 for _, _, affs, _ in authors if affs)
    return len(authors), with_aff


@dataclass
class SemanticScholarClient:
    api_key: Optional[str] = None
    min_interval: float = SEMANTIC_SCHOLAR_MIN_INTERVAL
    timeout: float = 30.0
    fields: str = DEFAULT_FIELDS

    def __post_init__(self) -> None:
        if self.min_interval < SEMANTIC_SCHOLAR_MIN_INTERVAL:
            LOGGER.warning(
                "Semantic Scholar min_interval %.2fs is below the %.1fs policy; clamping to %.2fs.",
                self.min_interval,
                SEMANTIC_SCHOLAR_MIN_INTERVAL,
                SEMANTIC_SCHOLAR_MIN_INTERVAL,
            )
            self.min_interval = SEMANTIC_SCHOLAR_MIN_INTERVAL
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        self._client = httpx.Client(headers=headers, timeout=self.timeout, http2=True)
        self._last_request = 0.0
        self._dynamic_min_interval = self.min_interval

    def close(self) -> None:
        self._client.close()

    def _sleep_if_needed(self) -> None:
        effective_min = max(self.min_interval, getattr(self, "_dynamic_min_interval", self.min_interval))
        if effective_min and effective_min > 0:
            elapsed = time.time() - self._last_request
            if elapsed < effective_min:
                time.sleep(effective_min - elapsed)

    def _request(self, url: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        attempt = 0
        while True:
            self._sleep_if_needed()
            resp = self._client.get(url, params=params)
            # Track completion time to enforce spacing between responses.
            self._last_request = time.time()
            if resp.status_code in (429,) or resp.status_code >= 500:
                attempt += 1
                if attempt >= SEMANTIC_SCHOLAR_MAX_RETRIES:
                    resp.raise_for_status()
                retry_after = None
                if resp.status_code == 429:
                    raw_retry = resp.headers.get("retry-after")
                    if raw_retry:
                        try:
                            retry_after = float(raw_retry)
                        except Exception:
                            retry_after = None
                    bumped = max(
                        self._dynamic_min_interval * SEMANTIC_SCHOLAR_DYNAMIC_MIN_INTERVAL_MULTIPLIER,
                        retry_after or 0.0,
                        self.min_interval,
                    )
                    if bumped > self._dynamic_min_interval:
                        self._dynamic_min_interval = min(
                            SEMANTIC_SCHOLAR_DYNAMIC_MIN_INTERVAL_MAX,
                            bumped,
                        )
                        LOGGER.warning(
                            "Semantic Scholar rate limited; bumping min_interval to %.2fs.",
                            self._dynamic_min_interval,
                        )
                backoff = min(
                    SEMANTIC_SCHOLAR_BACKOFF_MAX,
                    SEMANTIC_SCHOLAR_BACKOFF_BASE * (2 ** (attempt - 1)),
                )
                effective_min = max(self.min_interval, self._dynamic_min_interval)
                sleep_for = max(
                    effective_min,
                    retry_after or 0.0,
                    backoff,
                )
                LOGGER.warning(
                    "Semantic Scholar API error %s. Sleeping %.1fs before retry %s/%s.",
                    resp.status_code,
                    sleep_for,
                    attempt,
                    SEMANTIC_SCHOLAR_MAX_RETRIES,
                )
                time.sleep(sleep_for)
                continue
            resp.raise_for_status()
            return resp

    def get_paper_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        doi_norm = normalize_doi(doi) or doi
        if not doi_norm:
            return None
        url = f"{SEMANTIC_SCHOLAR_BASE}/paper/DOI:{doi_norm}"
        params = {"fields": self.fields}
        try:
            resp = self._request(url, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise
        payload = resp.json()
        return payload if isinstance(payload, dict) else None

    def search(self, query: str, *, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        params = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "fields": self.fields,
        }
        resp = self._request(f"{SEMANTIC_SCHOLAR_BASE}/paper/search", params=params)
        return resp.json()
