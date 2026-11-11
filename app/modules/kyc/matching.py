"""Comparing the name and date of birth on Aadhaar with those on the PAN.

Names on Indian documents differ in harmless ways: case, punctuation, word order,
titles ("Smt.", "Shri") and initials ("A. Verma" for "Asha Verma"). Anything beyond
that is treated as a mismatch, and an admin reviews every submission anyway.
"""

import re
import unicodedata
from datetime import date

# Not "kumari": it is often part of the name itself.
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "dr", "shri", "sri", "smt"})
_NOT_LETTERS = re.compile(r"[^a-z ]+")


def name_tokens(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    words = _NOT_LETTERS.sub(" ", text).split()
    return [word for word in words if word not in _TITLES]


def _token_matches(a: str, b: str) -> bool:
    if a == b:
        return True
    # An initial matches a full word with the same first letter.
    return (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))


def names_match(first: str, second: str) -> bool:
    """Same words in any order; an initial may stand for a word. At least one full word
    (not an initial) must match, so "A. V." can't match "Asha Verma"."""
    a, b = sorted(name_tokens(first)), sorted(name_tokens(second))
    if not a or len(a) != len(b):
        return False
    remaining = list(b)
    full_word_matched = False
    # Full words first, so an initial doesn't take a word that a full word needs.
    for token in sorted(a, key=len, reverse=True):
        match = next((other for other in remaining if _token_matches(token, other)), None)
        if match is None:
            return False
        remaining.remove(match)
        full_word_matched = full_word_matched or (len(token) > 1 and token == match)
    return full_word_matched


def birth_dates_match(
    aadhaar_date: date | None, aadhaar_year: int, pan_date: date | None
) -> bool | None:
    """None when there's nothing to compare."""
    if pan_date is None:
        return None
    if aadhaar_date is not None:
        return aadhaar_date == pan_date
    # Some Aadhaar records hold only the year of birth.
    return aadhaar_year == pan_date.year
