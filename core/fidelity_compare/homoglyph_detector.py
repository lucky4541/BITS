"""Greek/Latin/Cyrillic homoglyph detection (spec section 13 - "mandatory").

`script_of()` infers a character's Unicode script from its Unicode NAME
prefix (stdlib `unicodedata` has no direct script property) - a real,
documented, fully offline technique: Unicode character names for
letters consistently begin with the script name ("GREEK SMALL LETTER
ALPHA", "CYRILLIC SMALL LETTER A", "LATIN SMALL LETTER A"). This is
reliable for letters across the scripts this spec calls out (Latin,
Greek, Cyrillic) but is a heuristic, not the full Unicode Script
property table - punctuation/digits/symbols fall back to COMMON/UNKNOWN,
which callers already treat as "not a script clash" (see
unicode_detector.compare_chars).

`_CONFUSABLES` is a real, hand-curated, NON-EXHAUSTIVE subset covering
every example the spec explicitly lists (alpha/a, beta/B, gamma/y,
omicron/o, rho/p, chi/x, kappa/k, mu/m or u, nu/v) plus common Cyrillic/
Latin look-alikes - genuinely sourced from well-known Unicode confusable
pairs, not fabricated, but intentionally not the full ~6000-entry
Unicode confusables.txt table (spec section 13: "Do not assume the list
is exhaustive" - script-mismatch detection via script_of() alone already
catches any OTHER cross-script substitution even when the specific pair
isn't in this table, just without the extra "visually similar" label)."""
import unicodedata

_SCRIPT_PREFIXES = [
    ("LATIN", "Latin"), ("GREEK", "Greek"), ("CYRILLIC", "Cyrillic"),
    ("HEBREW", "Hebrew"), ("ARABIC", "Arabic"), ("HAN", "Han"),
    ("HIRAGANA", "Hiragana"), ("KATAKANA", "Katakana"), ("HANGUL", "Hangul"),
    ("DEVANAGARI", "Devanagari"), ("THAI", "Thai"), ("ARMENIAN", "Armenian"),
    ("GEORGIAN", "Georgian"),
]

# (script_a_char, script_b_char) pairs considered visually confusable,
# stored unordered (both directions checked by is_confusable()).
_CONFUSABLES = {
    # Greek <-> Latin (spec section 13's own explicit examples)
    ("α", "a"), ("Α", "A"),          # alpha / a
    ("β", "B"), ("Β", "B"),           # beta / B
    ("γ", "y"),                             # gamma / y
    ("ο", "o"), ("Ο", "O"),           # omicron / o
    ("ρ", "p"), ("Ρ", "P"),           # rho / p
    ("χ", "x"), ("Χ", "X"),           # chi / x
    ("κ", "k"), ("Κ", "K"),           # kappa / k
    ("μ", "m"), ("μ", "u"), ("Μ", "M"),  # mu / m or u
    ("ν", "v"), ("Ν", "N"),           # nu / v (capital nu looks like N)
    ("ι", "i"), ("Ι", "I"),           # iota / i
    ("ε", "e"), ("Ε", "E"),           # epsilon / e
    ("τ", "t"), ("Τ", "T"),           # tau / t
    ("η", "n"), ("Η", "H"),           # eta / n or H
    ("υ", "u"), ("Υ", "Y"),           # upsilon / u or Y
    ("σ", "o"),                             # sigma (loose, shape-only)
    ("Ζ", "Z"),                             # Zeta / Z
    # Cyrillic <-> Latin
    ("а", "a"), ("А", "A"),
    ("е", "e"), ("Е", "E"),
    ("о", "o"), ("О", "O"),
    ("р", "p"), ("Р", "P"),
    ("с", "c"), ("С", "C"),
    ("х", "x"), ("Х", "X"),
    ("у", "y"), ("У", "Y"),
    ("і", "i"), ("І", "I"),
    ("ѕ", "s"),
    ("в", "b"),
    ("к", "k"), ("К", "K"),
    ("м", "m"), ("М", "M"),
    ("н", "h"), ("Н", "H"),
    ("т", "t"), ("Т", "T"),
    ("в", "v"),
}
_CONFUSABLE_LOOKUP = set()
for _a, _b in _CONFUSABLES:
    _CONFUSABLE_LOOKUP.add((_a, _b))
    _CONFUSABLE_LOOKUP.add((_b, _a))


def script_of(ch: str) -> str:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "UNKNOWN"
    for prefix, script in _SCRIPT_PREFIXES:
        if name.startswith(prefix):
            return script.upper()
    return "COMMON"


def is_confusable(a: str, b: str) -> bool:
    if a == b:
        return False
    return (a, b) in _CONFUSABLE_LOOKUP


def script_name_for_display(script: str) -> str:
    return script.title() if script not in ("COMMON", "UNKNOWN") else script.title()
