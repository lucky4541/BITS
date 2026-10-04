"""Hyphenation comparison (spec section 17) - a fresh, independent
implementation for the fidelity-compare subsystem (never imports or
calls core/text_extractor.py's own hyphen-join logic, which exists to
DECIDE how the generator should join text; this module only ever
COMPARES two already-produced texts and never writes anything back).

Four outcomes, matching the spec's own four cases exactly:
  NORMALIZED_HYPHENATION - a genuine PDF line-break hyphen was correctly
      joined in the converted text. Status MATCH / EXPECTED_NORMALIZATION.
  HYPHEN_REMOVED - a real, same-line hyphen (e.g. "well-known") is
      missing in the converted text.
  HYPHEN_ADDED - the original had no hyphen at this position at all but
      the converted text introduced one.
  HYPHENATION_ERROR - a line-break hyphen was carried through literally
      (e.g. "govern- ment" instead of "government")."""
import re

_HYPHEN_CHARS = "-­"
_LOWER_RE = re.compile(r"[a-z]")


def is_line_break_hyphen(line_text: str, next_line_text: str) -> bool:
    """Geometry-adjacent signal: does `line_text` end in a hyphen right
    after a lowercase letter, with the following line starting in
    lowercase - the same real-world signature core/text_extractor.py's
    detector uses, reimplemented independently here since this subsystem
    reads output, never the generation pipeline itself."""
    stripped = line_text.rstrip()
    if not stripped or stripped[-1] not in _HYPHEN_CHARS:
        return False
    before = stripped[-2:-1]
    if not before or not _LOWER_RE.match(before):
        return False
    next_stripped = next_line_text.lstrip()
    return bool(next_stripped) and next_stripped[0].islower()


def classify(original_prefix: str, original_suffix: str, converted_text: str, was_line_break: bool) -> dict:
    """original_prefix/original_suffix: the two original fragments either
    side of the hyphen/line-break (e.g. "govern-" and "ment", or
    "well-" and "known" for a same-line hyphen). converted_text: the
    corresponding converted phrase/word."""
    prefix_clean = original_prefix.rstrip()
    has_hyphen = bool(prefix_clean) and prefix_clean[-1] in _HYPHEN_CHARS
    prefix_bare = prefix_clean.rstrip(_HYPHEN_CHARS)
    joined_solid = (prefix_bare + original_suffix.lstrip()).casefold()
    joined_with_space_hyphen = f"{prefix_clean} {original_suffix.lstrip()}".casefold()
    converted_norm = converted_text.strip().casefold()

    if was_line_break and has_hyphen:
        if converted_norm == joined_solid:
            return {"type": "NORMALIZED_HYPHENATION", "status": "MATCH / EXPECTED NORMALIZATION"}
        if converted_norm == joined_with_space_hyphen or converted_norm == prefix_clean.casefold() + " " \
                + original_suffix.strip().casefold():
            return {"type": "HYPHENATION_ERROR",
                    "status": "line-break hyphen carried through literally into reflowed text"}
        return {"type": "HYPHENATION_ERROR", "status": "unexpected result at a line-break hyphen"}

    if has_hyphen and not was_line_break:
        # A genuine mid-word hyphen like "well-known" on a single line.
        if converted_norm == joined_solid:
            return {"type": "HYPHEN_REMOVED", "status": "a real hyphen was dropped"}
        return {"type": None, "status": "hyphen preserved"}

    if not has_hyphen:
        combined_no_hyphen = (prefix_clean + original_suffix.lstrip()).casefold()
        if any(h in converted_norm for h in _HYPHEN_CHARS) and \
                converted_norm.replace("-", "").replace("­", "") == combined_no_hyphen.replace(" ", ""):
            return {"type": "HYPHEN_ADDED", "status": "a hyphen was introduced where none existed"}

    return {"type": None, "status": "no hyphenation change"}
