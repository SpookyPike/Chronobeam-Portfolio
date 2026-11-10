"""``quote_identifier`` — the guard on the two statements SQL cannot parameterize.

``CREATE DATABASE`` and ``DROP DATABASE`` take an identifier, not a value, so the
name has to be interpolated into the statement text. It comes from a configured
database URL, and configuration is not a trust boundary for SQL, so the name is
validated instead of trusted.
"""

import pytest
from app.db import quote_identifier


def test_a_plain_identifier_is_quoted() -> None:
    assert quote_identifier("chronobeam_test") == '"chronobeam_test"'
    assert quote_identifier("_scratch_2$") == '"_scratch_2$"'


@pytest.mark.parametrize(
    "name",
    [
        'chronobeam"; DROP DATABASE chronobeam; --',
        "chronobeam test",
        "chronobeam-test",
        "",
        "1chronobeam",
    ],
)
def test_an_identifier_that_could_escape_the_quotes_is_refused(name: str) -> None:
    """A double quote would end the quoted identifier and inject SQL."""
    with pytest.raises(ValueError, match="unsafe database identifier"):
        quote_identifier(name)
