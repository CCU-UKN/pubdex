# people_pubs/__init__.py
from __future__ import annotations

# Root package for the people/publications pipeline.
# (Exports kept minimal on purpose.)

# Transform/audit version recorded with every ingested source payload.
# Bump when parsing/transform logic changes
# the rows an importer would write — patch for bugfix-level changes, minor
# for behavior changes, major for output-shape changes. This is the version
# stamped into biblio.publications.source_provenance[*].transform_version.
# 1.5.1 (2026-09-30): configuration loading gains the PEOPLE_PUBS_SKIP_DOTENV
# opt-out, which the offline test suite sets; no importer transform changed,
# so apart from this stamp importers write the same rows.
# 1.5.0 (2026-09-25): organisation-neutrality correction to ORCID employment
# matching. It no longer folds one organisation's names and acronyms into a
# single token, so a configured pattern matches only the forms it lists, plus
# generic spelling variants.
# 1.4.0 (2026-09-23): align metadata ingestion with the supported source
# set; update source selection, the recognised provenance vocabulary,
# authorship reconstruction and the attribution catch-up selector to match.
# 1.3.0 (2026-09-10): two organisation-neutrality corrections to what
# importers write. Primary-email selection is domain-neutral -- the
# institution-specific preference is gone from the Python helper and the SQL
# PII functions, and the first address in stable input order wins when no
# explicit primary is set. And is_internal_at_ingest now reflects the linked
# person's app.people.person_kind instead of merely "a person was matched",
# so a linked external co-author is no longer recorded, or reported, as an
# internal author.
# 1.2.1 (2026-07-30): DOI normalization removes HTML-escaped repository
# landing-page version selectors from the canonical DOI identity.
# 1.2.0 (2026-07-29): attribution catch-up records per-publication provider
# failures as retryable errors instead of completed no-payload lookups.
# 1.1.0 (2026-07-17): new writer surface added; no importer transform
# changed, so existing source_provenance stamps are unaffected.
__version__ = "1.5.1"
