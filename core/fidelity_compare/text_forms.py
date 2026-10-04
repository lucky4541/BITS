"""Builds the three text representations every comparator relies on
(spec section 7). Pure functions, no state - `raw` is passed through
completely untouched (the ONE hard rule: "NEVER destroy RAW text").

NORMALIZED is for safe alignment only: collapses whitespace runs
(including non-breaking/thin/em space variants and the soft hyphen) and
normalizes Unicode canonical-equivalent forms (NFC) so e.g. a precomposed
'e-acute' and a decomposed 'e' + combining acute compare equal for
ALIGNMENT purposes - this never affects what unicode_detector.py reports,
which always reads `.raw` (see document_model.TextForms and
unicode_detector.py's own docstring on why NFC-for-alignment and
exact-codepoint-for-Unicode-reporting are deliberately different
concerns).

SEMANTIC additionally case-folds and expands common ligatures (from the
RAW string, never from an already-lossy form) so content matching treats
"OFFICE" / "office" / the ligature spelling as the same word -
ligature-vs-expansion is separately reported as a RAW difference by
homoglyph_detector.py's sibling checks, so this folding never hides it."""
import re
import unicodedata

_SOFT_HYPHEN = "­"
# NBSP, en/em/thin/hair/figure/narrow-NBSP/medium-math spaces, ideographic space.
_SPACE_CODEPOINTS = (0x00A0, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
                     0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000)
_SPACE_TRANSLATION = {cp: " " for cp in _SPACE_CODEPOINTS}
_WHITESPACE_RE = re.compile(r"\s+")

_LIGATURE_MAP = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}


def normalize(raw: str) -> str:
    text = unicodedata.normalize("NFC", raw)
    text = text.replace(_SOFT_HYPHEN, "")
    text = text.translate(_SPACE_TRANSLATION)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def semantic(raw: str) -> str:
    text = normalize(raw)
    for lig, expansion in _LIGATURE_MAP.items():
        text = text.replace(lig, expansion)
    return text.casefold()


def build(raw: str):
    from core.fidelity_compare.document_model import TextForms
    return TextForms(raw=raw, normalized=normalize(raw), semantic=semantic(raw))


def expand_ligatures(text: str) -> str:
    for lig, expansion in _LIGATURE_MAP.items():
        text = text.replace(lig, expansion)
    return text


def contains_ligature(text: str) -> bool:
    return any(lig in text for lig in _LIGATURE_MAP)
