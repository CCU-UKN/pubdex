"""
people_pubs.sync.fixture_demo

Offline acceptance ingestion: drive the real ORCID/Crossref ingestion path
with the bundled golden fixtures instead of a metadata provider.

Why this exists: the clean-clone acceptance walkthrough has to be
reproducible without provider credentials and without an existing database.
Every other ingestion command talks to a live service, so its result depends
on what that service happens to return today. This one performs no
metadata-provider request at all: its input and its expected publication data
come entirely from repository fixtures.

What it is NOT: a provider client. The two source classes below open no
connection, hold no session, base URL, credential or retry policy, and can
only ever return the fixture documents they were constructed with. They exist
solely to stand in for `OrcidClient` and `CrossrefClient` at the two lookup
points `people_pubs.sync.orcid_works.process_person` uses.

Everything after that lookup is the production path, unmodified: the same
work-summary normalization, publication dict, DOI handling, provenance merge,
`upsert_publication` and authorship writing that runs against live providers.
Ingestion happens in two stages against the same work, so the stored record
ends up with cumulative provenance:

  1. the ORCID payload alone           -> source_of_truth "orcid"
  2. the same work plus the Crossref payload -> "crossref+orcid", venue filled

Example:
  python -m people_pubs.sync.fixture_demo --dsn postgresql://...
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from people_pubs.db.connection import db_conn
from people_pubs.db.people import load_orcid_people
from people_pubs.sync.orcid_works import OrcidWorksStats, process_person

#: Bundled synthetic payloads: tiny, invented records that exist only here.
#: The identifiers in them are synthetic examples used by these fixtures, and
#: nothing in the demonstration resolves them over the network.
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "golden"
ORCID_FIXTURE = FIXTURE_DIR / "orcid_work_minimal.json"
CROSSREF_FIXTURE = FIXTURE_DIR / "crossref_work_minimal.json"

#: The fictional researcher the bundled roster and both payloads share.
#: Synthetic identifier: it is never looked up against the live registry.
DEMO_ORCID = "0000-0000-0000-0001"
DEMO_DOI = "10.5555/pubdex-fixture-001"
DEMO_TITLE = "Synthetic Collective Behaviour Paper"
#: Affiliation carried by the Crossref payload; the acceptance run configures
#: it as the institutional attribution rule.
DEMO_AFFILIATION = "Synthetic Institute for Collective Behaviour"

#: No freshness filter: the fixtures must ingest whatever year they carry.
_NO_DATE_FILTER = date(1900, 1, 1)

LOGGER = logging.getLogger(__name__)


def _full_work_shape(work: Dict[str, Any]) -> Dict[str, Any]:
    """Return the work in the `/work/{put-code}` shape the ingestion expects.

    A work *summary* nests contributors under `contributor-group`; the full
    work record lists them directly under `contributor`. The bundled fixture
    is stored in the summary shape, so translate it here rather than keeping
    two copies of the same payload.
    """
    group = (work.get("contributors") or {}).get("contributor-group") or {}
    contributors = group.get("contributor")
    if contributors is None:
        return work
    full = dict(work)
    full["contributors"] = {"contributor": contributors}
    return full


class FixtureOrcidWorks:
    """Fixture stand-in for the ORCID client used during ingestion.

    Answers only for the iD it was built for, only from the payload it was
    given. It performs no I/O of any kind.
    """

    def __init__(self, orcid: str, work: Dict[str, Any]) -> None:
        self._orcid = orcid
        self._work = work

    def list_works(self, orcid: str) -> Dict[str, Any]:
        if orcid != self._orcid:
            return {"group": []}
        return {"group": [{"work-summary": [self._work]}]}

    def get_work(self, orcid: str, put_code: Any) -> Optional[Dict[str, Any]]:
        if orcid != self._orcid or put_code != self._work.get("put-code"):
            return None
        return _full_work_shape(self._work)


class FixtureCrossrefWorks:
    """Fixture stand-in for the Crossref client used during ingestion.

    Holds a DOI -> payload mapping and nothing else; an unknown DOI simply has
    no metadata, which is the same thing the ingestion sees when Crossref does
    not know a work. It performs no I/O of any kind.
    """

    def __init__(self, works_by_doi: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._by_doi = {
            (doi or "").strip().lower(): payload
            for doi, payload in (works_by_doi or {}).items()
        }

    def get_work(self, doi: str) -> Optional[Dict[str, Any]]:
        return self._by_doi.get((doi or "").strip().lower())


@dataclass
class StageResult:
    """One ingestion stage of the acceptance run."""

    stage: str
    processed: int
    inserted: int
    updated: int


def load_fixture(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def ingest_fixtures(
    conn,
    *,
    orcid_work: Dict[str, Any],
    crossref_work: Dict[str, Any],
    demo_orcid: str = DEMO_ORCID,
) -> List[StageResult]:
    """Ingest the bundled payloads through the production ingestion path.

    Returns one `StageResult` per stage. Raises `SystemExit` when the person
    the fixtures belong to has not been imported yet, because that is a setup
    mistake rather than an ingestion failure.
    """
    people = load_orcid_people(
        conn=conn,
        only_person_id=None,
        only_orcid=demo_orcid,
        limit_people=None,
        refreshed_before=None,
    )
    if not people:
        raise SystemExit(
            f"fixture demo: no person with ORCID {demo_orcid} in the database. "
            "Import the bundled roster first "
            "(people_pubs.sync.add_people --csv tests/fixtures/golden/"
            "demo_roster_minimal.csv --no-orcid-lookup)."
        )

    stages = (
        ("orcid", FixtureCrossrefWorks()),
        ("crossref", FixtureCrossrefWorks({DEMO_DOI: crossref_work})),
    )

    results: List[StageResult] = []
    for stage, crossref_source in stages:
        orcid_source = FixtureOrcidWorks(demo_orcid, orcid_work)
        stats = OrcidWorksStats(people_total=len(people))
        for person_row in people:
            process_person(
                conn=conn,
                oc=orcid_source,
                cr=crossref_source,
                person_row=person_row,
                since=_NO_DATE_FILTER,
                dry_run=False,
                stats=stats,
            )
        LOGGER.info(
            "fixture stage %s: processed=%s inserted=%s updated=%s",
            stage,
            stats.processed,
            stats.inserted,
            stats.updated,
        )
        results.append(
            StageResult(
                stage=stage,
                processed=stats.processed,
                inserted=stats.inserted,
                updated=stats.updated,
            )
        )
    return results


def run_fixture_demo(
    *,
    dsn: Optional[str],
    orcid_fixture: Path = ORCID_FIXTURE,
    crossref_fixture: Path = CROSSREF_FIXTURE,
) -> List[StageResult]:
    orcid_work = load_fixture(orcid_fixture)
    crossref_work = load_fixture(crossref_fixture)
    with db_conn(dsn) as conn:
        return ingest_fixtures(
            conn, orcid_work=orcid_work, crossref_work=crossref_work
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest the bundled synthetic ORCID and Crossref payloads through "
            "the production ingestion path. Makes no metadata-provider request."
        )
    )
    parser.add_argument(
        "--dsn", help="Postgres DSN (else PEOPLE_DB_DSN / PG* env vars)."
    )
    parser.add_argument(
        "--orcid-fixture",
        type=Path,
        default=ORCID_FIXTURE,
        help="ORCID payload to ingest (default: the bundled golden fixture).",
    )
    parser.add_argument(
        "--crossref-fixture",
        type=Path,
        default=CROSSREF_FIXTURE,
        help="Crossref payload to enrich with (default: the bundled golden fixture).",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose logging.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    results = run_fixture_demo(
        dsn=args.dsn,
        orcid_fixture=args.orcid_fixture,
        crossref_fixture=args.crossref_fixture,
    )
    for result in results:
        print(
            f"fixture stage {result.stage}: processed={result.processed} "
            f"inserted={result.inserted} updated={result.updated}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
