from datetime import date

import pytest

from app.modules.kyc.matching import birth_dates_match, name_tokens, names_match


@pytest.mark.parametrize(
    ("aadhaar", "pan"),
    [
        ("Asha Verma", "ASHA VERMA"),
        ("Asha Verma", "Verma Asha"),
        ("Asha  Verma", "asha verma."),
        ("Smt. Asha Verma", "ASHA VERMA"),
        ("Asha Verma", "A. Verma"),
        ("Asha Kumari Verma", "Asha K Verma"),
        ("Ravi Shankar", "Shri Ravi Shankar"),
        ("Rāvi Shankar", "RAVI SHANKAR"),  # accents
    ],
)
def test_same_person(aadhaar: str, pan: str) -> None:
    assert names_match(aadhaar, pan)


@pytest.mark.parametrize(
    ("aadhaar", "pan"),
    [
        ("Asha Verma", "Asha Sharma"),
        ("Asha Verma", "Asha"),
        ("Asha Verma", "Asha Verma Singh"),
        ("Asha Verma", "A. V."),  # initials alone aren't enough
        ("Asha Verma", "B. Verma"),
        ("Asha Verma", ""),
        ("", ""),
        ("Mr", "Mr"),  # titles only
    ],
)
def test_different_person(aadhaar: str, pan: str) -> None:
    assert not names_match(aadhaar, pan)


def test_tokens_drop_titles_and_punctuation() -> None:
    assert name_tokens("Dr. A.K. Verma-Singh") == ["a", "k", "verma", "singh"]


def test_birth_dates() -> None:
    dob = date(1994, 5, 17)
    assert birth_dates_match(dob, 1994, dob) is True
    assert birth_dates_match(dob, 1994, date(1994, 5, 18)) is False
    assert birth_dates_match(None, 1994, dob) is True  # Aadhaar has only the year
    assert birth_dates_match(None, 1990, dob) is False
    assert birth_dates_match(dob, 1994, None) is None
