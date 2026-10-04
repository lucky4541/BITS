"""Central HIGH/MEDIUM/LOW confidence classification (spec section 55) -
every detector converts its own numeric confidence signal (exact-match
strength, geometry certainty, dictionary support, OCR confidence, image
similarity) into a score via `score_of(...)` and a bucket via
`bucket(score)`, so every Difference in the report uses the exact same
thresholds rather than each detector inventing its own cutoffs."""

HIGH_THRESHOLD = 0.90
MEDIUM_THRESHOLD = 0.60


def bucket(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "HIGH"
    if score >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def combine(*scores: float, weights: tuple = None) -> float:
    """Weighted average of independent supporting signals (spec: "geometry
    = primary, dictionary = supporting, grammar = supporting") - callers
    pass the PRIMARY signal first with the highest weight."""
    scores = [s for s in scores if s is not None]
    if not scores:
        return 0.0
    if weights is None:
        weights = tuple(1.0 for _ in scores)
    total_w = sum(weights[:len(scores)])
    if total_w == 0:
        return 0.0
    return sum(s * w for s, w in zip(scores, weights)) / total_w


def from_ocr_confidence(ocr_conf: float) -> float:
    """OCR confidence (0-1 or 0-100) folded into the same 0-1 scale used
    everywhere else - never lets uncertain OCR present as a confirmed
    HIGH-confidence content error (spec section 54)."""
    if ocr_conf is None:
        return 0.5
    value = ocr_conf / 100.0 if ocr_conf > 1.0 else ocr_conf
    return max(0.0, min(1.0, value))
