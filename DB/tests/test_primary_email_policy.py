"""Primary-email selection must be domain-neutral.

The rule is deliberately dull: keep an explicitly set primary, otherwise take
the first address in stable input order. Nothing about an organisation, a
domain or a top-level domain may influence the choice. The SQL side of the
same rule lives in pii.merge_people_verified_emails and pii.ensure_people_pii
and is covered by tests/integration/test_primary_email_policy_integration.py.
"""

import pytest

from people_pubs.db.people import merge_emails, pick_primary_email


def test_existing_primary_is_kept() -> None:
    assert (
        pick_primary_email("chosen@example.org", ["first@example.org", "b@example.net"])
        == "chosen@example.org"
    )


def test_existing_primary_is_kept_even_when_absent_from_the_list() -> None:
    # The stored primary is authoritative; it is not re-derived from the list.
    assert pick_primary_email("kept@example.org", ["other@example.net"]) == "kept@example.org"


@pytest.mark.parametrize("placeholder", ["primary_email", "email", "none", "n/a", "  ", ""])
def test_placeholder_primary_is_ignored(placeholder: str) -> None:
    assert (
        pick_primary_email(placeholder, ["first@example.org", "second@example.net"])
        == "first@example.org"
    )


def test_first_address_wins_when_no_primary_is_set() -> None:
    assert (
        pick_primary_email(None, ["first@example.org", "second@example.net"])
        == "first@example.org"
    )


def test_no_addresses_means_no_primary() -> None:
    assert pick_primary_email(None, []) is None
    assert pick_primary_email("", []) is None


@pytest.mark.parametrize(
    "emails",
    [
        ["a@example.org", "b@example.net", "c@example.com"],
        ["a@example.net", "b@example.org", "c@example.com"],
        ["a@example.com", "b@example.org", "c@example.net"],
    ],
)
def test_no_domain_or_tld_is_preferred(emails: list) -> None:
    """Whichever address comes first wins, whatever the domains are."""
    assert pick_primary_email(None, emails) == emails[0]


def test_ordering_is_stable_and_not_lexical() -> None:
    """A later-sorting address still wins when it was supplied first."""
    assert (
        pick_primary_email(None, ["zeta@example.org", "alpha@example.org"])
        == "zeta@example.org"
    )


def test_reversing_the_input_reverses_the_choice() -> None:
    """The only thing that decides is position, so the result must flip."""
    pair = ["one@example.org", "two@example.net"]
    assert pick_primary_email(None, pair) == "one@example.org"
    assert pick_primary_email(None, list(reversed(pair))) == "two@example.net"


def test_case_variants_do_not_change_the_result() -> None:
    """merge_emails de-duplicates case-insensitively, keeping the first form."""
    merged = merge_emails(None, ["First@Example.org", "first@example.org", "b@example.net"])
    assert merged == ["First@Example.org", "b@example.net"]
    assert pick_primary_email(None, merged) == "First@Example.org"


def test_merge_emails_preserves_input_order_across_sources() -> None:
    merged = merge_emails(
        ["existing@example.org"], ["verified@example.net", "existing@example.org"]
    )
    assert merged == ["existing@example.org", "verified@example.net"]
    assert pick_primary_email(None, merged) == "existing@example.org"
