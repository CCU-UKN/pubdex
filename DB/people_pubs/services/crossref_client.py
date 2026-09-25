"""
crossref_client.py
Crossref client and utilities.

This module provides:
- A small Crossref REST API client with retry/backoff.
- DOI normalization helpers.
- Publication-date selection heuristics for Crossref 'message' payloads.
- Preprint heuristics (best-effort) to help downstream sync logic.

Environment variables:

- PEOPLE_PUBS_CROSSREF_MAILTO: contact email used in the User-Agent header (recommended by Crossref).
"""

from __future__ import annotations

import html
import os
import logging
import random
import re
import time
from datetime import date
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple, Dict
import httpx
from urllib.parse import quote

from people_pubs.config import CROSSREF_MAILTO, USER_AGENT_PRODUCT

CROSSREF_MAILTO_ENV = "PEOPLE_PUBS_CROSSREF_MAILTO"
_DOI_RE = re.compile(r"(10\.[0-9]{4,9}/\S+)", re.IGNORECASE)
_DOI_LANDING_PAGE_VERSION_RE = re.compile(
    r"[?&]version=[^\s]*$",
    re.IGNORECASE,
)

log = logging.getLogger(__name__)


def normalize_doi(doi: str) -> Optional[str]:
    """Normalize a DOI string.

    Accepts DOI URLs, 'doi:' prefixes, mixed-case DOIs, strings containing a
    DOI, and HTML-escaped landing-page URLs.  Some metadata feeds incorrectly
    append a repository version selector (for example
    ``&amp;version=2.1``) to the DOI field; that selector belongs to the
    landing-page URL, not the DOI identity, so it is removed.

    Returns a lowercase canonical DOI (without https://doi.org/ prefix), or
    None.
    """
    if not doi:
        return None
    s = html.unescape(doi).strip()

    # Common prefixes / URL forms
    s = re.sub(r"^doi\s*:\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)

    m = _DOI_RE.search(s)
    if not m:
        return None
    candidate = _DOI_LANDING_PAGE_VERSION_RE.sub("", m.group(1))
    return candidate.rstrip(" .).,;\"'").lower()


def _parse_crossref_date_parts(parts: Any) -> Optional[date]:
    """Parse Crossref date-parts structure into a date."""
    try:
        dp = parts.get("date-parts") if isinstance(parts, Mapping) else None
        if not dp or not isinstance(dp, list) or not dp[0] or not isinstance(dp[0], list):
            return None
        y = int(dp[0][0])
        m = int(dp[0][1]) if len(dp[0]) > 1 else 1
        d = int(dp[0][2]) if len(dp[0]) > 2 else 1
        return date(y, m, d)
    except Exception:
        return None


def pick_publication_date(work: Mapping[str, Any], fallback_year: Optional[int] = None) -> Optional[date]:
    """Pick a publication date from a Crossref work 'message' payload.

    Tries common fields in a reasonable priority order. If only a year is available,
    returns Jan 1 of that year. If nothing is available, uses fallback_year if given.
    """
    for key in ("published-print", "published-online", "issued", "created"):
        if key in work:
            dt = _parse_crossref_date_parts(work.get(key))
            if dt:
                return dt

    # Sometimes only 'published' is provided
    if "published" in work:
        dt = _parse_crossref_date_parts(work.get("published"))
        if dt:
            return dt

    # Fallback to 'published' year-like fields
    for key in ("published", "issued"):
        val = work.get(key)
        if isinstance(val, Mapping):
            parts = val.get("date-parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                try:
                    y = int(parts[0][0])
                    return date(y, 1, 1)
                except Exception:
                    pass

    if fallback_year is not None:
        try:
            return date(int(fallback_year), 1, 1)
        except Exception:
            return None
    return None


_PREPRINT_DOI_PREFIXES = (
    "10.1101/",   # bioRxiv / medRxiv
    "10.21203/",  # Research Square
    "10.2139/",   # SSRN
    "10.48550/",  # arXiv (via DOI)
)

_PREPRINT_VENUE_TOKENS = (
    "biorxiv",
    "medrxiv",
    "arxiv",
    "ssrn",
    "research square",
    "preprint",
)


def guess_is_preprint(
    msg: Mapping[str, Any],
    doi: Optional[str] = None,
    venue: Optional[str] = None,
    title: Optional[str] = None,
) -> bool:
    """Best-effort heuristic to flag likely preprints.

    This is intentionally conservative; downstream code should treat it as a hint.
    """
    doi_n = normalize_doi(doi or msg.get("DOI") or "") if (doi or msg.get("DOI")) else None
    if doi_n:
        for pref in _PREPRINT_DOI_PREFIXES:
            if doi_n.startswith(pref):
                return True

    typ = (msg.get("type") or "").lower()
    if typ in {"posted-content", "report"}:
        # Crossref often uses posted-content for preprints.
        return True

    container = venue or ""
    if not container:
        c = msg.get("container-title")
        if isinstance(c, list) and c:
            container = str(c[0] or "")
        elif isinstance(c, str):
            container = c

    t = title or ""
    if not t:
        tl = msg.get("title")
        if isinstance(tl, list) and tl:
            t = str(tl[0] or "")
        elif isinstance(tl, str):
            t = tl

    hay = f"{container} {t}".strip().lower()
    for tok in _PREPRINT_VENUE_TOKENS:
        if tok in hay:
            return True
    return False


@dataclass
class CrossrefClient:
    """Small Crossref REST client (https://api.crossref.org)."""

    # Optional for local/dev, but strongly recommended by Crossref etiquette.
    mailto: Optional[str] = None
    timeout: float = 10.0
    max_retries: int = 5

    base_url: str = "https://api.crossref.org"
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Allow construction without args (e.g., scripts that do CrossrefClient())
        # while still supporting correct etiquette via env vars.
        if not self.mailto:
            self.mailto = (
                os.getenv(CROSSREF_MAILTO_ENV)
                or os.getenv("CROSSREF_MAILTO")
                or os.getenv("PEOPLE_PUBS_MAILTO")
                or CROSSREF_MAILTO
            )

        # Single-source the UA with config: explicit env override wins, else
        # the shared product token plus this client's mailto.
        ua = os.getenv("PEOPLE_PUBS_USER_AGENT") or USER_AGENT_PRODUCT
        if self.mailto and "mailto:" not in ua:
            ua = f"{ua} (+mailto:{self.mailto})"
        elif not self.mailto:
            log.warning(
                "CrossrefClient initialised without a mailto address. "
                "Set PEOPLE_PUBS_CROSSREF_MAILTO (or CROSSREF_MAILTO) to comply with Crossref etiquette."
            )

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "User-Agent": ua,
                "Accept": "application/json",
            },
        )

    @staticmethod
    def normalize_doi(doi: str) -> str:
        out = normalize_doi(doi)
        return out or doi.strip().lower()

    def __enter__(self) -> "CrossrefClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
    def _get_with_status(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> Tuple[Optional[int], Optional[dict[str, Any]]]:
        """HTTP GET with retry/backoff; returns (status_code, parsed_json) or (status_code, None).

        - 404 returns (404, None)
        - retryable statuses raise after retries
        """
        params = dict(params or {})
        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._client.get(path, params=params)
                status = int(r.status_code)
                if status == 404:
                    return status, None
                if status in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                r.raise_for_status()
                try:
                    return status, r.json()
                except Exception:
                    return status, None
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
                last_err = e
                if attempt >= self.max_retries:
                    break
                # Exponential backoff with jitter; honor Retry-After when the
                # server sent one (429/503), capped to keep cron runs bounded.
                sleep_s = min(30.0, (2 ** (attempt - 1))) + random.random()
                retry_after = None
                response = getattr(e, "response", None)
                if response is not None:
                    raw_retry = response.headers.get("retry-after")
                    if raw_retry:
                        try:
                            retry_after = float(raw_retry)
                        except ValueError:
                            retry_after = None
                if retry_after and retry_after > 0:
                    sleep_s = max(sleep_s, min(120.0, retry_after))
                    log.warning(
                        "Crossref sent Retry-After=%.1fs; sleeping %.1fs before retry %d/%d.",
                        retry_after, sleep_s, attempt, self.max_retries,
                    )
                time.sleep(sleep_s)

        if last_err:
            raise last_err
        return None, None

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """HTTP GET with retry/backoff; returns parsed JSON dict or None."""
        _status, data = self._get_with_status(path, params=params)
        return data

    def lookup_by_doi(self, doi: str) -> Optional[dict[str, Any]]:
        msg, _status = self.lookup_by_doi_with_status(doi)
        return msg

    def lookup_by_doi_with_status(self, doi: str) -> Tuple[Optional[dict[str, Any]], Optional[int]]:
        doi_norm = self.normalize_doi(doi)
        # DOIs may contain characters that must be URL-encoded when used as part of a URL path.
        # Encoding the whole DOI (including the slash) is the safest option.
        doi_path = quote(doi_norm, safe="")
        status, data = self._get_with_status(f"/works/{doi_path}")
        if not data:
            return None, status
        msg = data.get("message") if isinstance(data, Mapping) else None
        return (msg if isinstance(msg, dict) else None), status

    def lookup_registration_agency_with_status(
        self, doi: str
    ) -> Tuple[Optional[Dict[str, str]], Optional[int]]:
        """Return DOI registration agency info via /works/{doi}/agency (best-effort)."""
        doi_norm = self.normalize_doi(doi)
        status, data = self._get_with_status(f"/works/{doi_norm}/agency")
        if not data or not isinstance(data, Mapping):
            return None, status
        msg = data.get("message")
        if not isinstance(msg, Mapping):
            return None, status
        agency = msg.get("agency")
        if not isinstance(agency, Mapping):
            return None, status
        out: Dict[str, str] = {}
        if agency.get("id"):
            out["id"] = str(agency.get("id"))
        if agency.get("label"):
            out["label"] = str(agency.get("label"))
        return (out or None), status

    def get_work(self, doi: str) -> Optional[dict[str, Any]]:
        """Backwards-compatible alias for lookup_by_doi()."""
        return self.lookup_by_doi(doi)

    def lookup_by_title(self, title: str, rows: int = 3) -> list[dict[str, Any]]:
        title = (title or "").strip()
        if not title:
            return []
        data = self._get(
            "/works",
            params={
                "query.title": title,
                "rows": rows,
            },
        )
        if not data or not isinstance(data, Mapping):
            return []
        msg = data.get("message")
        if not isinstance(msg, Mapping):
            return []
        items = msg.get("items")
        if not isinstance(items, list):
            return []
        return [it for it in items if isinstance(it, dict)]
