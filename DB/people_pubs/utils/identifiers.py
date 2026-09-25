from __future__ import annotations

import re
from typing import Optional


_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def orcid_checksum_is_valid(orcid: str) -> bool:
    """ISO 7064 MOD 11-2 check-digit test for a canonical bare ORCID iD.

    Expects the canonical form ``normalize_orcid`` returns. Roster/manual
    entry points use this to reject mistyped-but-format-valid iDs; ingestion
    paths that receive ORCIDs from upstream APIs deliberately do not.
    """
    digits = orcid.replace("-", "")
    if len(digits) != 16:
        return False
    total = 0
    for ch in digits[:-1]:
        if not ch.isdigit():
            return False
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    return digits[-1] == expected


def normalize_orcid(value: Optional[str]) -> Optional[str]:
    """Return canonical bare ORCID form or None."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"^(?:https?://)?(?:www\.)?orcid\.org/", "", cleaned, flags=re.IGNORECASE).strip()
    if not cleaned:
        return None
    cleaned = cleaned.upper()
    if not _ORCID_RE.fullmatch(cleaned):
        return None
    return cleaned
