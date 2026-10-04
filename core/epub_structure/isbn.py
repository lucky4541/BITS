"""Step 5/3 (spec: "USER INPUT — ONLY TWO REQUIRED FIELDS" / "ISBN
VALIDATION") - validates the two ISBN values the user manually enters
(Normal ISBN, Printed ISBN) and keeps them completely separate. Real
ISBN-10/ISBN-13 checksum validation, never a superficial "looks like a
number" check.

Normalization is COSMETIC ONLY (stripping hyphens/spaces for the checksum
math and for the value actually written into the OPF) - the two ISBN
values themselves are never merged, swapped, or defaulted from one
another anywhere in this module."""
import re
from dataclasses import dataclass

_STRIP_RE = re.compile(r"[\s-]")


@dataclass
class IsbnValidation:
    raw: str
    normalized: str = ""      # hyphens/spaces removed, uppercase 'X' for ISBN-10 check digit
    kind: str = ""             # "ISBN-10" / "ISBN-13" / "" (invalid/empty)
    valid: bool = False
    error: str = ""


def _clean(raw: str) -> str:
    return _STRIP_RE.sub("", raw or "").upper()


def _validate_isbn10(digits: str) -> bool:
    if len(digits) != 10 or not digits[:9].isdigit() or not (digits[9].isdigit() or digits[9] == "X"):
        return False
    total = 0
    for i, ch in enumerate(digits):
        value = 10 if ch == "X" else int(ch)
        total += value * (10 - i)
    return total % 11 == 0


def _validate_isbn13(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    return total % 10 == 0


def validate_isbn(raw: str, field_name: str = "ISBN") -> IsbnValidation:
    """THE single entry point - used identically for BOTH Normal ISBN and
    Printed ISBN (never two separate validation implementations that could
    disagree on what counts as valid). `field_name` is only used to make
    the error message identify WHICH of the two fields failed."""
    cleaned = _clean(raw)
    if not cleaned:
        return IsbnValidation(raw=raw, error=f"{field_name} is required.")
    if len(cleaned) == 10:
        ok = _validate_isbn10(cleaned)
        return IsbnValidation(raw=raw, normalized=cleaned, kind="ISBN-10", valid=ok,
                               error="" if ok else f"{field_name} '{raw}' fails the ISBN-10 checksum.")
    if len(cleaned) == 13:
        ok = _validate_isbn13(cleaned)
        return IsbnValidation(raw=raw, normalized=cleaned, kind="ISBN-13", valid=ok,
                               error="" if ok else f"{field_name} '{raw}' fails the ISBN-13 checksum.")
    return IsbnValidation(raw=raw, normalized=cleaned,
                           error=f"{field_name} '{raw}' is not a recognizable ISBN-10 or ISBN-13 "
                                 f"(got {len(cleaned)} digit(s), expected 10 or 13).")


def normalized_urn(validation: IsbnValidation) -> str:
    """The exact form the OPF's <dc:identifier> already uses in real
    production EPUBs (confirmed against two real books this session:
    'urn:isbn:9780521554466') - only ever called on an ISBN that already
    validated True."""
    return f"urn:isbn:{validation.normalized}"
