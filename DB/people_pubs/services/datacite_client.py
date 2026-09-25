from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from people_pubs.config import USER_AGENT
from people_pubs.services.crossref_client import normalize_doi
from people_pubs.utils.identifiers import normalize_orcid


LOGGER = logging.getLogger("people_pubs.datacite_client")
DATACITE_BASE = "https://api.datacite.org"
DATACITE_MAX_RETRIES = 4
DATACITE_BACKOFF_BASE = 1.0
DATACITE_BACKOFF_MAX = 8.0


def _strip_orcid(value: Optional[str]) -> Optional[str]:
    return normalize_orcid(value)


def _extract_orcid(identifiers: Any) -> Optional[str]:
    if not identifiers:
        return None
    if isinstance(identifiers, dict):
        identifiers = [identifiers]
    if not isinstance(identifiers, list):
        return None
    for item in identifiers:
        if not isinstance(item, dict):
            continue
        scheme = (item.get("nameIdentifierScheme") or "").upper()
        if scheme != "ORCID":
            continue
        return _strip_orcid(item.get("nameIdentifier"))
    return None


def _normalize_affiliations(raw: Any, include_department: bool = False) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str):
            val = item.strip()
            if val:
                out.append(val)
            continue
        if isinstance(item, dict):
            val = (item.get("name") or item.get("affiliation") or "").strip()
            if val:
                out.append(val)
            if include_department:
                dept = (item.get("department") or "").strip()
                if dept:
                    out.append(dept)
    return out


def iter_authors_from_datacite(
    entry: Dict[str, Any],
    *,
    include_department: bool = False,
) -> Iterable[Tuple[str, Optional[str], List[str], bool]]:
    attrs = entry.get("attributes") if isinstance(entry, dict) else None
    if not isinstance(attrs, dict):
        return
    creators = attrs.get("creators") or []
    if isinstance(creators, dict):
        creators = [creators]
    if not isinstance(creators, list):
        return
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        full_name = (creator.get("name") or "").strip()
        if not full_name:
            given = (creator.get("givenName") or "").strip()
            family = (creator.get("familyName") or "").strip()
            full_name = " ".join(p for p in (given, family) if p).strip()
        if not full_name:
            continue
        orcid = _extract_orcid(creator.get("nameIdentifiers"))
        affiliations = _normalize_affiliations(creator.get("affiliation"), include_department=include_department)
        yield full_name, orcid, affiliations, False


def count_authors_with_affiliations(entry: Dict[str, Any]) -> Tuple[int, int]:
    total = 0
    with_aff = 0
    for _name, _orcid, affs, _corr in iter_authors_from_datacite(entry):
        total += 1
        if affs:
            with_aff += 1
    return total, with_aff


def datacite_title(entry: Dict[str, Any]) -> Optional[str]:
    attrs = entry.get("attributes") if isinstance(entry, dict) else None
    if not isinstance(attrs, dict):
        return None
    titles = attrs.get("titles") or []
    if isinstance(titles, dict):
        titles = [titles]
    if not isinstance(titles, list):
        return None
    for t in titles:
        if isinstance(t, dict):
            val = (t.get("title") or "").strip()
            if val:
                return val
        elif isinstance(t, str):
            val = t.strip()
            if val:
                return val
    return None


def datacite_year(entry: Dict[str, Any]) -> Optional[int]:
    attrs = entry.get("attributes") if isinstance(entry, dict) else None
    if not isinstance(attrs, dict):
        return None
    year = attrs.get("publicationYear")
    if isinstance(year, int):
        return year
    if isinstance(year, str):
        match = re.search(r"\d{4}", year)
        if match:
            try:
                return int(match.group(0))
            except Exception:
                return None
    return None


def datacite_doi(entry: Dict[str, Any]) -> Optional[str]:
    attrs = entry.get("attributes") if isinstance(entry, dict) else None
    if isinstance(attrs, dict):
        doi = (attrs.get("doi") or "").strip()
        if doi:
            return doi
    if isinstance(entry, dict):
        value = (entry.get("id") or "").strip()
        return value or None
    return None


def datacite_venue(entry: Dict[str, Any]) -> Optional[str]:
    attrs = entry.get("attributes") if isinstance(entry, dict) else None
    if not isinstance(attrs, dict):
        return None
    container = attrs.get("container")
    if isinstance(container, dict):
        val = (container.get("title") or "").strip()
        if val:
            return val
    publisher = (attrs.get("publisher") or "").strip()
    return publisher or None


def datacite_is_preprint(entry: Dict[str, Any]) -> Optional[bool]:
    attrs = entry.get("attributes") if isinstance(entry, dict) else None
    if not isinstance(attrs, dict):
        return None
    types = attrs.get("types") or {}
    if isinstance(types, dict):
        rtg = (types.get("resourceTypeGeneral") or "").lower()
        rt = (types.get("resourceType") or "").lower()
        if "preprint" in rtg or "preprint" in rt:
            return True
    return None


@dataclass
class DataCiteClient:
    token: Optional[str] = None
    min_interval: float = 0.0
    timeout: float = 20.0

    def __post_init__(self) -> None:
        headers = {
            "Accept": "application/vnd.api+json",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(headers=headers, timeout=self.timeout, http2=True)
        self._last_request = 0.0

    def close(self) -> None:
        self._client.close()

    def _sleep_if_needed(self) -> None:
        if self.min_interval and self.min_interval > 0:
            elapsed = time.time() - self._last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

    def _request(self, url: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        attempt = 0
        while True:
            self._sleep_if_needed()
            self._last_request = time.time()
            resp = self._client.get(url, params=params)
            if resp.status_code in (429,) or resp.status_code >= 500:
                attempt += 1
                if attempt >= DATACITE_MAX_RETRIES:
                    resp.raise_for_status()
                backoff = min(DATACITE_BACKOFF_MAX, DATACITE_BACKOFF_BASE * (2 ** (attempt - 1)))
                LOGGER.warning(
                    "DataCite API error %s. Sleeping %.1fs before retry %s/%s.",
                    resp.status_code,
                    backoff,
                    attempt,
                    DATACITE_MAX_RETRIES,
                )
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            return resp

    def get_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        doi_norm = normalize_doi(doi) or doi
        if not doi_norm:
            return None
        url = f"{DATACITE_BASE}/dois/{doi_norm}"
        try:
            resp = self._request(url)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return None

    def search(self, query: str, *, size: int = 5, page: int = 1, filters: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "query": query,
            "page[size]": size,
            "page[number]": page,
        }
        if filters:
            params["filter"] = filters
        resp = self._request(f"{DATACITE_BASE}/dois", params=params)
        return resp.json()
