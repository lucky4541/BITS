"""Comparison-only text normalization. The original text is never changed;
these keys are only used to decide whether two words correspond."""
import re
import unicodedata

SOFT_HYPHEN = "­"
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_KEY_STRIP_RE = re.compile(r"[^\w]+", re.UNICODE)
_WORD_RE = re.compile(r"\S+")


def clean(text: str) -> str:
    """NFC, soft hyphens and zero-width characters removed, whitespace
    collapsed - differences that are never real content changes."""
    text = unicodedata.normalize("NFC", text or "").replace(SOFT_HYPHEN, "").translate(_ZERO_WIDTH)
    return " ".join(text.split())


def key(word: str) -> str:
    """Alignment key: case-folded letters/digits only, compatibility-normalized
    (ligatures, full-width forms). Punctuation and quotes do not prevent two
    words from corresponding; the exact comparison happens afterwards."""
    w = unicodedata.normalize("NFKC", clean(word)).casefold()
    return _KEY_STRIP_RE.sub("", w)


def exact(word: str) -> str:
    """Exact-comparison form (only invisible differences removed)."""
    return clean(word)


def words(text: str) -> list:
    return _WORD_RE.findall(clean(text))
