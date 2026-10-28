# people_pubs/config.py
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, NoReturn, Optional

from people_pubs.password_file import PasswordFileError, read_password_file

#: Names a file holding the database password of the ingestion commands --
#: in the Compose deployment, app_writer's secret file. It takes the place of
#: PGPASSWORD, so libpq's password reaches no command line, and it gives the
#: password to every connection of the process and of the commands it starts.
PASSWORD_FILE_VARIABLE = "PEOPLE_DB_PASSWORD_FILE"
#: Exit status of a configuration error, as for the read service.
CONFIGURATION_ERROR = 2


def _parse_dotenv(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        out[key] = val
    return out


def dotenv_disabled() -> bool:
    """True when PEOPLE_PUBS_SKIP_DOTENV asks not to read any .env file.

    The offline test suite sets it, so that a local DB/.env can never hand the
    tests connection settings or other configuration.
    """
    value = os.environ.get("PEOPLE_PUBS_SKIP_DOTENV", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _configuration_error(message: str) -> NoReturn:
    print(f"people_pubs: configuration error: {message}", file=sys.stderr)
    raise SystemExit(CONFIGURATION_ERROR)


def _load_local_env(candidates: Optional[List[Path]] = None) -> None:
    # Prefer real process env; .env only fills missing values, and not at all
    # when PEOPLE_PUBS_SKIP_DOTENV is set.
    password_in_environment = "PGPASSWORD" in os.environ
    if candidates is None:
        candidates = [
            Path(__file__).resolve().parents[1] / ".env",  # .../DB/.env
            Path.cwd() / ".env",
        ]
    if not dotenv_disabled():
        for candidate in candidates:
            for k, v in _parse_dotenv(candidate).items():
                os.environ.setdefault(k, v)

    # Compose-style variable compatibility for direct script runs.
    if "PGDATABASE" not in os.environ and os.getenv("POSTGRES_DB"):
        os.environ["PGDATABASE"] = str(os.getenv("POSTGRES_DB"))
    if "PGUSER" not in os.environ and os.getenv("POSTGRES_USER"):
        os.environ["PGUSER"] = str(os.getenv("POSTGRES_USER"))
    # A password file takes precedence over any password a .env file would
    # fill in. A different PGPASSWORD in the environment makes the choice
    # ambiguous; the same one is what a command started by another that read
    # the file inherits. Errors name the variable, never the file or a value.
    password_path = os.environ.get(PASSWORD_FILE_VARIABLE, "").strip()
    if password_path:
        try:
            password = read_password_file(password_path, PASSWORD_FILE_VARIABLE)
        except PasswordFileError as exc:
            _configuration_error(str(exc))
        if password_in_environment and os.environ.get("PGPASSWORD") != password:
            _configuration_error(
                f"{PASSWORD_FILE_VARIABLE} and PGPASSWORD are both set, to different passwords; "
                "set only one of them"
            )
        os.environ["PGPASSWORD"] = password
    elif "PGPASSWORD" not in os.environ:
        role_password_vars = {
            "app_readonly": "APP_READONLY_PASSWORD",
            "app_writer": "APP_WRITER_PASSWORD",
            "maintenance": "MAINTENANCE_PASSWORD",
            "postgres": "POSTGRES_PASSWORD",
        }
        password_var = role_password_vars.get(os.getenv("PGUSER", ""))
        if password_var and os.getenv(password_var):
            os.environ["PGPASSWORD"] = str(os.getenv(password_var))
        elif os.getenv("POSTGRES_PASSWORD"):
            os.environ["PGPASSWORD"] = str(os.getenv("POSTGRES_PASSWORD"))


_load_local_env()


class PubSource(str, Enum):
    """Canonical labels for publication sources.

    Keep these aligned with what you write into biblio.publications.source_of_truth.
    """
    MANUAL = "manual"
    CROSSREF = "crossref"
    ORCID = "orcid"
    SCHOLAR = "scholar"
    SEMANTIC = "semantic"
    OPENALEX = "openalex"
    DBLP = "dblp"
    DATACITE = "datacite"
    UNKNOWN = "unknown"


#: Source trust order for publication metadata.
#: Earlier = more trusted. This is the "boot config" you can override per deployment.
TRUST_ORDER_PUBLICATIONS: List[PubSource] = [
    PubSource.MANUAL,
    PubSource.CROSSREF,
    PubSource.DATACITE,
    PubSource.SEMANTIC,
    PubSource.ORCID,
    PubSource.SCHOLAR,
    PubSource.DBLP,
    PubSource.OPENALEX,
    PubSource.UNKNOWN,
]

#: Convenience mapping for fast rank lookups.
SOURCE_PRIORITY_RANK: Dict[str, int] = {
    s.value: i for i, s in enumerate(TRUST_ORDER_PUBLICATIONS)
}


@dataclass(frozen=True)
class DedupePolicy:
    """
    Central place for publication dedupe / merge decisions.

    - treat_equal_doi_as_same:
        If True, two rows with the same normalized DOI are treated as the same work.

    - title_year_window:
        "Same normalized title + year within ±N" counts as "probably same work".

    - prefer_non_preprint:
        When merging, prefer the non-preprint version as canonical if available.

    - canonical_strategy:
        How to pick the surviving row when we need to collapse duplicates.
        Values:
          * "oldest_pub_id"            -> keep the lowest pub_id
          * "newest_pub_id"            -> keep the highest pub_id
          * "non_preprint_then_oldest" -> prefer non-preprint; if tie, lowest pub_id
    """
    treat_equal_doi_as_same: bool = True
    title_year_window: int = 1
    prefer_non_preprint: bool = True
    canonical_strategy: str = "non_preprint_then_oldest"


#: Default dedupe policy; override per script if needed.
DEFAULT_DEDUPE_POLICY = DedupePolicy()


# ---------------------------------------------------------------------------
# External-service configuration / defaults
# ---------------------------------------------------------------------------

#: Optional DSN; if missing, psycopg will use PG* env vars.
PEOPLE_DB_DSN: Optional[str] = os.getenv("PEOPLE_DB_DSN")

# Backwards-compatible aliases used by some sync scripts.
# Keep Optional[str] so psycopg can fall back to PG* env vars when unset.
DEFAULT_DSN: Optional[str] = PEOPLE_DB_DSN
PEOPLE_DB_DS: Optional[str] = PEOPLE_DB_DSN  # alias; do not use in new code

#: Contact mailto; used in User-Agent strings or &mailto= params.
#: Empty by default: set a team address (never a personal one) through
#: PEOPLE_PUBS_CROSSREF_MAILTO, usually via DB/.env. Clients omit the
#: mailto parameter when this is empty.
CROSSREF_MAILTO: str = os.getenv(
    "PEOPLE_PUBS_CROSSREF_MAILTO",
    "",
)

# Polite-pool etiquette: one descriptive User-Agent for every HTTP client in
# the package. USER_AGENT_PRODUCT is the bare product token for clients that
# compose their own contact suffix.
USER_AGENT_PRODUCT: str = "people-pubs/0.1"
USER_AGENT: str = os.getenv(
    "PEOPLE_PUBS_USER_AGENT",
    f"{USER_AGENT_PRODUCT} (+mailto:{CROSSREF_MAILTO})" if CROSSREF_MAILTO else USER_AGENT_PRODUCT,
)

# DataCite API token (optional; used by datacite_backfill).
DATACITE_API_TOKEN: str = os.getenv("DATACITE_API_TOKEN", "")

# Semantic Scholar API key (optional; used by semantic_scholar_* scripts).
SEMANTIC_SCHOLAR_API_KEY: str = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")

# Minimum name match score (0..1) for heuristic author->person matching.
# Set PEOPLE_PUBS_NAME_MATCH_MIN_SCORE to tighten matching globally.
try:
    NAME_MATCH_MIN_SCORE: float = float(
        os.getenv("PEOPLE_PUBS_NAME_MATCH_MIN_SCORE", "0")
    )
except Exception:
    NAME_MATCH_MIN_SCORE = 0.0
if NAME_MATCH_MIN_SCORE < 0:
    NAME_MATCH_MIN_SCORE = 0.0
if NAME_MATCH_MIN_SCORE > 1:
    NAME_MATCH_MIN_SCORE = 1.0

#: ORCID org patterns used by fetch_orcid_profile_enrichment() etc.
#: Empty by default: set PEOPLE_PUBS_ORG_PATTERNS (or pass --org-match) to a
#: semicolon-separated list of your organisation's names and abbreviations,
#: e.g. "Example University;Example Institute;exuni". With no patterns,
#: employment enrichment matches nothing and simply records no employments.
DEFAULT_ORCID_ORG_PATTERNS: List[str] = [
    p.strip()
    for p in os.getenv("PEOPLE_PUBS_ORG_PATTERNS", "").replace(",", ";").split(";")
    if p.strip()
]


def source_rank(source: str) -> int:
    """
    Return a small integer rank for a source label
    (0 = most trusted, higher = less trusted).

    Unknown sources get the worst rank so they never overwrite trusted ones
    unless you explicitly allow it.
    """
    source = (source or "").strip().lower()
    rank = SOURCE_PRIORITY_RANK.get(source)
    if rank is not None:
        return rank
    family = source.split("-", 1)[0] if source else PubSource.UNKNOWN.value
    return SOURCE_PRIORITY_RANK.get(family, len(SOURCE_PRIORITY_RANK))


def source_family(source: str) -> str:
    """Return the canonical source family for generic or family-specific tokens."""
    source = (source or "").strip().lower()
    return source.split("-", 1)[0] if source else PubSource.UNKNOWN.value


def source_tokens(source_of_truth: str) -> List[str]:
    """Split a cumulative source_of_truth value into non-empty source tokens."""
    return [p.strip().lower() for p in (source_of_truth or "").split("+") if p.strip()]


def has_more_trusted_source(source_of_truth: str, candidate_source: str) -> bool:
    """Whether source_of_truth already contains a source ranked above candidate_source."""
    candidate_rank = source_rank(source_family(candidate_source))
    return any(
        source_rank(source_family(token)) < candidate_rank
        for token in source_tokens(source_of_truth)
    )
