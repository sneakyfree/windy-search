"""Unit tests for app/privacy.py (M1.9).

Covers:
  * Each pattern (email, phone, SSN, coord) redacts.
  * Patterns NOT meant to match leave the query alone (false-positive
    guard).
  * `rewrite_query` returns `(sanitized, n)` with `n` matching the
    actual number of redactions.
  * Idempotent: sanitized → rewrite_query → same sanitized + 0 count.
  * Multiple PII types in one query all get redacted.
"""
from __future__ import annotations

from app.privacy import (
    REDACTED_EMAIL,
    REDACTED_PHONE,
    REDACTED_SSN,
    rewrite_query,
)

# ---------- email ----------


def test_redacts_email():
    out, n = rewrite_query("contact me at grant@example.com please")
    assert REDACTED_EMAIL in out
    assert "grant@example.com" not in out
    assert n == 1


def test_redacts_multiple_emails():
    out, n = rewrite_query("a@b.co and c.d+e@f-g.io")
    assert out.count(REDACTED_EMAIL) == 2
    assert n == 2


def test_keeps_non_email_text_with_at_sign():
    """Standalone `@username` (no domain) shouldn't trip the email regex."""
    out, n = rewrite_query("follow @grantwhitmer on twitter")
    assert n == 0
    assert out == "follow @grantwhitmer on twitter"


# ---------- phone ----------


def test_redacts_phone_us_format():
    out, n = rewrite_query("call me at 555-123-4567 tomorrow")
    assert REDACTED_PHONE in out
    assert "555-123-4567" not in out
    assert n == 1


def test_redacts_phone_with_country_code():
    out, n = rewrite_query("dial +1 555 123 4567")
    assert REDACTED_PHONE in out
    assert n == 1


def test_redacts_phone_with_parens():
    out, n = rewrite_query("call (555) 123-4567")
    assert REDACTED_PHONE in out
    assert n == 1


def test_redacts_phone_with_dots():
    out, n = rewrite_query("text 555.123.4567")
    assert REDACTED_PHONE in out
    assert n == 1


# ---------- SSN ----------


def test_redacts_ssn():
    out, n = rewrite_query("my SSN is 123-45-6789 for the form")
    assert REDACTED_SSN in out
    assert "123-45-6789" not in out
    assert n == 1


def test_does_not_redact_undashed_9_digit_string():
    """Bare 9-digit numbers (zip+4, order IDs) shouldn't be misread as SSN."""
    out, n = rewrite_query("order number 123456789 has shipped")
    assert REDACTED_SSN not in out
    # phone regex MAY trip here (it's 10 digits but pattern allows for
    # missing separators). 123-456-789 has only 9 digits in `\d{3}\d{3}\d{4}`
    # shape — let me check this specifically.
    # The 10-digit phone pattern is \d{3}\d{3}\d{4} = 10 digits.
    # `123456789` is 9 digits — phone regex should NOT match.
    # So both ssn AND phone should not match this case.
    assert REDACTED_PHONE not in out
    assert n == 0


# ---------- coords ----------


def test_rounds_high_precision_coords():
    """Lat 40.71284, long -74.00591 → 40.71, -74.01"""
    out, n = rewrite_query("GPS 40.71284 N, -74.00591 W")
    assert "40.71" in out
    assert "-74.01" in out
    # Neither original survives
    assert "40.71284" not in out
    assert "-74.00591" not in out
    assert n == 2  # two coords rounded


def test_keeps_low_precision_coords():
    """Coords with only 2 decimal places already coarse → leave alone."""
    out, n = rewrite_query("city center is around 40.71, -74.00")
    assert "40.71" in out
    assert "-74.00" in out
    assert n == 0


def test_keeps_versions_and_decimals():
    """Decimals like '3.14' or 'python 3.12' should not trip coord regex."""
    out, n = rewrite_query("install python 3.12 and run pi 3.14")
    assert "3.12" in out
    assert "3.14" in out
    assert n == 0


# ---------- combined ----------


def test_redacts_multiple_types_in_one_query():
    out, n = rewrite_query(
        "Reach me at grant@example.com or 555-123-4567, "
        "my SSN is 123-45-6789, GPS 40.71284, -74.00591"
    )
    assert REDACTED_EMAIL in out
    assert REDACTED_PHONE in out
    assert REDACTED_SSN in out
    assert "40.71" in out
    assert "-74.01" in out
    # 1 email + 1 phone + 1 ssn + 2 coords = 5 redactions
    assert n == 5


def test_no_pii_returns_input_with_zero_count():
    """A clean query should be unmodified and report 0 redactions."""
    out, n = rewrite_query("what are the best restaurants in austin")
    assert out == "what are the best restaurants in austin"
    assert n == 0


def test_empty_query():
    out, n = rewrite_query("")
    assert out == ""
    assert n == 0


def test_idempotent_on_sanitized_output():
    """Calling rewrite_query on already-sanitized output should not
    re-redact (n == 0 on the second call)."""
    once, n1 = rewrite_query("contact grant@example.com or 555-123-4567")
    assert n1 == 2
    twice, n2 = rewrite_query(once)
    assert n2 == 0
    assert twice == once


def test_returns_tuple_shape():
    """Document the signature: returns (str, int)."""
    result = rewrite_query("test")
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], str)
    assert isinstance(result[1], int)
