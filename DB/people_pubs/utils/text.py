"""Presentation-side text hygiene for publisher metadata.

Publisher-side providers such as Crossref deliver titles, journal names and
abstracts as JATS/HTML/MathML fragments (`<i>`, `<scp>`, `<sub>`,
`<mml:math>`, entities like `&amp;`) plus the whitespace runs of
pretty-printed XML. Raw payloads keep them (provenance stays faithful by
design), so anything rendered for a human must reduce them to plain text
first.

DUPLICATION NOTE: people_pubs has no cross-imports and must stay importable
with PYTHONPATH=DB alone (DB/run_tests.sh and the db-tests CI job), so this
helper is kept self-contained rather than shared.
"""

from __future__ import annotations

import html
import re

# Real markup tags start with a letter (optionally namespaced, e.g. mml:math);
# a literal "<" in prose (e.g. "x < y") never matches.
_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:._-]*(?:\s[^<>]*)?/?>")
_WS_RE = re.compile(r"\s+")


def clean_publisher_markup(text) -> str:
    """Publisher JATS/HTML/MathML fragment -> plain readable text.

    Tags are dropped and their inner text kept (`<i>Gallus</i>` -> `Gallus`,
    `H<sub>2</sub>O` -> `H2O`, MathML keeps operand text); entities are
    unescaped afterwards so escaped literals stay literal (`&lt;i&gt;` was
    content, not markup); whitespace runs from pretty-printed XML collapse to
    single spaces.
    """
    if not text:
        return ""
    text = _TAG_RE.sub("", str(text))
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()
