"""Generic block-sequence alignment shared by every higher-level aligner
(paragraph_aligner, heading_compare, list_compare, reference_compare,
index_compare, footnote_compare) - one real LCS-based alignment
implementation (spec section 53: "sequence matching, LCS, edit distance
... never use fuzzy matching alone"), never duplicated per comparator.

Uses difflib.SequenceMatcher (stdlib, fully offline) over each block's
SEMANTIC text as the primary key. `autojunk=False` is required - with
autojunk left on, SequenceMatcher silently treats any element appearing
in >1% of a long sequence as "popular" and ignores it entirely for
matching, which would corrupt alignment of a book where many short
paragraphs/list items legitimately repeat similar wording."""
import difflib
from dataclasses import dataclass


@dataclass
class AlignedPair:
    original_index: int  # or None if this is a pure insertion
    converted_index: int  # or None if this is a pure deletion
    op: str               # "equal" / "replace" / "delete" / "insert"


def align_by_key(original_keys: list, converted_keys: list) -> list:
    """Returns a list of AlignedPair covering every element of both
    sequences at least once, using longest-matching-block alignment.

    Fast path: equality is common in EPUB/PDF production runs. Avoid
    constructing a SequenceMatcher at all when the two sequences are already
    identical; this also makes repeated comparisons effectively linear in the
    output size instead of paying SequenceMatcher setup/matching costs.
    """
    if len(original_keys) == len(converted_keys) and original_keys == converted_keys:
        return [AlignedPair(i, i, "equal") for i in range(len(original_keys))]
    sm = difflib.SequenceMatcher(a=original_keys, b=converted_keys, autojunk=False)
    pairs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pairs.append(AlignedPair(i1 + k, j1 + k, "equal"))
        elif tag == "replace":
            span_o = list(range(i1, i2))
            span_c = list(range(j1, j2))
            for k in range(max(len(span_o), len(span_c))):
                oi = span_o[k] if k < len(span_o) else None
                ci = span_c[k] if k < len(span_c) else None
                pairs.append(AlignedPair(oi, ci, "replace"))
        elif tag == "delete":
            for k in range(i1, i2):
                pairs.append(AlignedPair(k, None, "delete"))
        elif tag == "insert":
            for k in range(j1, j2):
                pairs.append(AlignedPair(None, k, "insert"))
    return pairs


def align_blocks(original_blocks: list, converted_blocks: list, key_attr: str = "semantic") -> list:
    original_keys = [getattr(b.text, key_attr) for b in original_blocks]
    converted_keys = [getattr(b.text, key_attr) for b in converted_blocks]
    return align_by_key(original_keys, converted_keys)


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()
