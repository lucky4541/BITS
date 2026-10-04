"""Space-only differences (spec section 8) - MISSING_SPACE/EXTRA_SPACE
always accompany a MERGED_WORD/SPLIT_WORD finding (spec section 9's own
example reports both tags together), and this module also catches the
narrower case of a run of multiple original spaces collapsing to one (or
vice versa) inside otherwise-unchanged text, which merged_word_detector
does not cover since no word boundary actually moved."""
import re

_MULTI_SPACE_RE = re.compile(r" {2,}")


def companion_tag_for_merge() -> str:
    return "MISSING_SPACE"


def companion_tag_for_split() -> str:
    return "EXTRA_SPACE"


def detect_space_run_change(original_raw: str, converted_raw: str) -> dict:
    """Same words, same order, only whitespace RUN LENGTH differs - e.g.
    an original double-space that collapsed to one, or vice versa. Only
    meaningful when the word sequence is otherwise identical (callers
    check that first); never confused with a real MERGED_WORD/SPLIT_WORD,
    which changes the number of tokens rather than just the number of
    spaces between two tokens."""
    orig_words = original_raw.split()
    conv_words = converted_raw.split()
    if orig_words != conv_words:
        return {"changed": False}
    orig_runs = _MULTI_SPACE_RE.findall(original_raw)
    conv_runs = _MULTI_SPACE_RE.findall(converted_raw)
    if len(orig_runs) != len(conv_runs) or orig_runs != conv_runs:
        tag = "EXTRA_SPACE" if len(conv_runs) > len(orig_runs) else "MISSING_SPACE"
        return {"changed": True, "type": tag}
    return {"changed": False}
