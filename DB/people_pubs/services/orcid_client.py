# people_pubs/services/orcid_client.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..orcidkit import (
    OrcidClient as _OrcidClient,
    fetch_orcid_profile_enrichment,
    works_summary_records,
)


from people_pubs.config import DEFAULT_ORCID_ORG_PATTERNS


# Re-export the low-level client in case you still want direct access.
OrcidClient = _OrcidClient


@dataclass
class OrcidProfileService:
    """
    High-level ORCID profile/enrichment wrapper.

    Responsibilities:
      - Own the org_patterns you care about (your institute, its centres, ...)
      - Provide a simple `.fetch_profile_enrichment(orcid, context_tokens=...)`
        that uses orcidkit.fetch_orcid_profile_enrichment under the hood.
    """

    org_patterns: Sequence[str] = tuple(DEFAULT_ORCID_ORG_PATTERNS)
    trace: bool = False

    def fetch_profile_enrichment(
        self,
        orcid: str,
        *,
        context_tokens: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch enriched profile info for one ORCID.

        Returns the dict that orcidkit.fetch_orcid_profile_enrichment() returns,
        i.e. something like::

            {
              "orcid": "...",
              "verified_emails": [...],
              "employments": [...],
              "last_updated": "...",
              ...
            }
        """
        ctx_tokens: List[str] = list(context_tokens or [])
        patterns = list(self.org_patterns)

        with _OrcidClient() as client:
            return fetch_orcid_profile_enrichment(
                client,
                orcid,
                org_patterns=patterns,
                context_tokens=ctx_tokens,
                trace=self.trace,
            )


@dataclass
class OrcidWorksService:
    """
    Helper around ORCID works listing.

    This just wraps the common pattern:

      client.list_works(orcid)  ->  orcidkit.works_summary_records(...)
    """

    trace: bool = False

    def list_flattened_works(self, orcid: str) -> List[Dict[str, Any]]:
        """
        Return a list of flattened ORCID works for the given ORCID.

        Each element is one summary record as produced by
        orcidkit.works_summary_records().
        """
        with _OrcidClient() as client:
            raw = client.list_works(orcid)
            # works_summary_records() takes the raw /{orcid}/works payload.
            return list(works_summary_records(raw))
