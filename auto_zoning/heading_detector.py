"""Self-relative heading-level classification for one page's blocks, used
only by Auto Analyse (auto_zoning/page_analyzer.py). Deliberately has NO
absolute point-size thresholds - "18pt = heading" would be wrong for a
document whose body text is itself 14pt. Everything is judged relative to
THIS page's own body font size (pdf_block_detector.page_body_font_size),
and heading LEVELS are assigned by clustering the page's own distinct
larger sizes (reusing core.table_extractor._cluster_1d, the same generic
1D-clustering primitive table column detection already uses - not a second
clustering implementation)."""
from core.table_extractor import _cluster_1d

HEADING_MIN_RATIO = 1.08          # font_size / body_size to qualify purely on size
BOLD_CANDIDATE_MAX_RATIO = 0.98   # a bold, short block still qualifies even at ~body size
BOLD_CANDIDATE_MAX_WORDS = 12
SIZE_CLUSTER_TOLERANCE = 0.6      # pt - distinct heading sizes rarely differ by less than this
MAX_HEADING_LEVEL = 6


def _is_heading_candidate(block, body_size: float) -> bool:
    if body_size <= 0:
        return False
    ratio = block.font_size / body_size
    if ratio >= HEADING_MIN_RATIO:
        return True
    if block.bold and ratio >= BOLD_CANDIDATE_MAX_RATIO:
        word_count = len(block.text.split())
        if word_count <= BOLD_CANDIDATE_MAX_WORDS:
            return True
    return False


def classify_blocks(blocks: list, body_size: float) -> list:
    """Returns [(block, tag), ...] in the same order as `blocks`, tag one of
    "h1".."h6" or "p". Heading levels are assigned CONSISTENTLY across the
    whole set passed in: the largest distinct font-size cluster among
    heading candidates always becomes h1, the next h2, and so on, capped at
    h6 - "if a heading is clearly larger than another, give it a higher-
    level heading", exactly as specified, with no hard-coded sizes."""
    candidates = [b for b in blocks if _is_heading_candidate(b, body_size)]
    if not candidates:
        return [(b, "p") for b in blocks]

    sizes = [c.font_size for c in candidates]
    centers = sorted(_cluster_1d(sizes, SIZE_CLUSTER_TOLERANCE), reverse=True)

    def level_for_size(size: float) -> str:
        rank = min(range(len(centers)), key=lambda i: abs(centers[i] - size))
        return f"h{min(rank + 1, MAX_HEADING_LEVEL)}"

    candidate_ids = {id(c) for c in candidates}
    result = []
    for b in blocks:
        if id(b) in candidate_ids:
            result.append((b, level_for_size(b.font_size)))
        else:
            result.append((b, "p"))
    return result
