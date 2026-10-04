"""Exact-codepoint Unicode comparison (spec section 12/14) - operates
ONLY on `.raw` text (never `.normalized`/`.semantic`, which deliberately
fold away exactly the differences this module exists to catch). Visual
similarity is NEVER sufficient to call two characters equal - every
comparison is by Unicode code point.

Cross-script substitutions (alpha vs Latin a, Cyrillic a vs Latin a) are
delegated to homoglyph_detector.py, which shares this module's
`char_info()` helper so both report identical Unicode metadata for the
same character."""
import unicodedata

from core.fidelity_compare import homoglyph_detector

# Combining-mark-stripped ASCII fallback for the common accented-letter
# case (spec section 12: 'o-acute' -> 'o', 'e-acute' -> 'e', 'n-tilde' ->
# 'n', 'u-diaeresis' -> 'u') - same script (Latin) on both sides, so this
# is reported as an accent-stripped codepoint change, never a homoglyph.
def _strip_accents(ch: str) -> str:
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def char_info(ch: str) -> dict:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = "<unnamed>"
    return {
        "char": ch,
        "codepoint": f"U+{ord(ch):04X}",
        "name": name,
        "category": unicodedata.category(ch),
        "script": homoglyph_detector.script_of(ch),
    }


def compare_chars(original: str, converted: str) -> dict:
    """Returns a dict describing the relationship between one original
    character and one converted character it aligns to - never mutates
    either. `same` is True only for an exact codepoint match."""
    if original == converted:
        return {"same": True}
    info_o, info_c = char_info(original), char_info(converted)
    is_homoglyph = homoglyph_detector.is_confusable(original, converted)
    script_changed = info_o["script"] != info_c["script"] and info_o["script"] not in ("COMMON", "UNKNOWN") \
        and info_c["script"] not in ("COMMON", "UNKNOWN")
    accent_stripped = (not script_changed) and _strip_accents(original) == converted
    return {
        "same": False,
        "original": info_o,
        "converted": info_c,
        "is_homoglyph": is_homoglyph,
        "script_changed": script_changed,
        "accent_stripped": accent_stripped,
    }
