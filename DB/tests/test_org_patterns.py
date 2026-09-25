"""Employment org-pattern matching: empty by default, configured per install.

`PEOPLE_PUBS_ORG_PATTERNS` (or `--org-match`) tells the ORCID profile
enrichment which employers count as "ours". It ships EMPTY, which matches no
employment at all rather than every employment.

This is a *different* setting from institutional publication attribution
(`app.institution_attribution_rules`, see
tests/integration/test_institution_attribution_integration.py): one decides
which employments are recorded for a person, the other decides which
publications enter the canonical list. Configuring one must not configure the
other, and this file pins that separation. It also pins that matching folds
only generic spelling variants and gives no organisation built-in aliases.
Offline: pure functions, no network and no DB.
"""
from __future__ import annotations

import importlib
import itertools
import sys

from people_pubs.orcidkit import _norm_org_tokens, _org_match

INSTITUTE = "Institute for Example Studies"


def _reload_config(monkeypatch, value: str | None):
    if value is None:
        monkeypatch.delenv("PEOPLE_PUBS_ORG_PATTERNS", raising=False)
    else:
        monkeypatch.setenv("PEOPLE_PUBS_ORG_PATTERNS", value)
    for name in [m for m in list(sys.modules) if m.startswith("people_pubs.config")]:
        del sys.modules[name]
    return importlib.import_module("people_pubs.config")


def test_default_org_patterns_are_empty(monkeypatch) -> None:
    config = _reload_config(monkeypatch, None)
    assert config.DEFAULT_ORCID_ORG_PATTERNS == [], (
        "the baseline must ship no institution-specific employment patterns"
    )


def test_empty_patterns_match_no_employment() -> None:
    for org in (INSTITUTE, "Example University", "Anything At All", ""):
        assert _org_match(org, []) is False, (
            "an empty pattern list must match nothing, not everything"
        )


def test_blank_patterns_are_ignored_rather_than_matching_everything() -> None:
    assert _org_match(INSTITUTE, ["", "   "]) is False


def test_configured_patterns_match_their_organisation() -> None:
    patterns = [INSTITUTE, "exinst"]
    assert _org_match(INSTITUTE, patterns) is True
    assert _org_match("Institute for Example Studies (EXINST)", patterns) is True
    assert _org_match("Unrelated Department, Elsewhere", patterns) is False


def test_generic_spelling_variants_still_normalise() -> None:
    assert (
        _norm_org_tokens("University")
        == _norm_org_tokens("Universität")
        == _norm_org_tokens("Universitaet")
        == _norm_org_tokens("Uni")
        == {"uni"}
    )
    assert (
        _norm_org_tokens("Centre")
        == _norm_org_tokens("Center")
        == _norm_org_tokens("Ctr")
        == {"center"}
    )
    assert _norm_org_tokens("Behaviour") == _norm_org_tokens("Behavior") == {"behavior"}
    assert _norm_org_tokens("Institut") == _norm_org_tokens("Institute") == {"inst"}
    # The example documented in SCRIPTS.md, and several variants in one name.
    assert _org_match("Universität Example", ["Example University"]) is True
    assert _org_match(
        "Example Institut, Centre for Behaviour Studies",
        ["Example Institute Center for Behavior Studies"],
    ) is True


def test_configured_patterns_match_on_whole_tokens() -> None:
    patterns = ["Example Studies"]
    # Every pattern token present, in any order and among other tokens.
    assert _org_match("Institute for Example Studies", patterns) is True
    assert _org_match("Studies of Example, Department of Methods", patterns) is True
    # A missing token, or a token that merely contains the pattern's, is not.
    assert _org_match("Institute for Example Research", patterns) is False
    assert _org_match("Institute for Examples Studies", patterns) is False


def test_organisation_names_and_acronyms_are_not_folded_together() -> None:
    forms = ["Max Planck", "MPI", "MPIAB"]
    assert [_norm_org_tokens(form) for form in forms] == [
        {"max", "planck"},
        {"mpi"},
        {"mpiab"},
    ]
    for org, pattern in itertools.permutations(forms, 2):
        assert _org_match(org, [pattern]) is False, (
            f"{pattern!r} must not match {org!r} without being configured"
        )
    assert _org_match("Max Planck Institute", ["MPI"]) is False
    assert _org_match("Max-Planck-Institut", ["MPIAB"]) is False


def test_an_install_matches_every_form_it_lists(monkeypatch) -> None:
    config = _reload_config(monkeypatch, "Max Planck Institute; MPIAB")
    patterns = config.DEFAULT_ORCID_ORG_PATTERNS
    assert _org_match("Max Planck Institute", patterns) is True
    assert _org_match("Max-Planck-Institut", patterns) is True
    assert _org_match("MPIAB", patterns) is True
    # A form the install did not list stays unmatched.
    assert _org_match("MPI", patterns) is False
    _reload_config(monkeypatch, None)


def test_configured_patterns_come_from_the_environment(monkeypatch) -> None:
    config = _reload_config(monkeypatch, f"{INSTITUTE}; exinst")
    assert config.DEFAULT_ORCID_ORG_PATTERNS == [INSTITUTE, "exinst"]
    assert _org_match(INSTITUTE, config.DEFAULT_ORCID_ORG_PATTERNS) is True
    # Comma separation is accepted too, and blanks are dropped.
    config = _reload_config(monkeypatch, f"{INSTITUTE},, exinst ,")
    assert config.DEFAULT_ORCID_ORG_PATTERNS == [INSTITUTE, "exinst"]
    _reload_config(monkeypatch, None)


def test_employment_patterns_do_not_configure_publication_attribution(monkeypatch) -> None:
    """The two settings are independent by construction.

    Employment matching reads PEOPLE_PUBS_ORG_PATTERNS in the process
    environment; publication attribution reads app.institution_attribution_rules
    in the database. Neither module consults the other's source.
    """
    config = _reload_config(monkeypatch, INSTITUTE)
    assert config.DEFAULT_ORCID_ORG_PATTERNS == [INSTITUTE]

    import people_pubs.orcidkit as orcidkit

    source = "".join(
        open(m.__file__, encoding="utf-8").read() for m in (orcidkit, config)
    )
    assert "institution_attribution_rules" not in source, (
        "employment matching must not read the publication-attribution rules"
    )
    _reload_config(monkeypatch, None)


def test_cli_org_match_default_is_empty() -> None:
    from people_pubs.cli import people_pubs_cli

    parser_src = open(people_pubs_cli.__file__, encoding="utf-8").read()
    assert '"--org-match",\n        default="",' in parser_src, (
        "--org-match must default to empty, not to any institution's name"
    )
