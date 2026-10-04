"""Turns the raw difference list into the measured percentages spec
section 60 requires - VERIFIED MATCH %, CONFIRMED DIFFERENCE %,
UNCERTAIN %, per category and overall. Every percentage here is derived
from an actual count against an actual denominator (`totals`, supplied
by engine.py from the real comparison run) - never a hardcoded "99%"
(spec: "Do NOT simply display 99% accurate without measurement")."""
from dataclasses import dataclass, field

_CATEGORY_MAP = {
    "content": "content", "unicode": "unicode", "hyphenation": "hyphenation",
    "structure": "structure", "layout": "layout", "figure": "figure", "table": "table",
    "footnote": "structure", "reference": "structure", "index": "structure", "link": "link",
    "ocr": "ocr",
}


@dataclass
class CategoryScore:
    category: str
    total_units: int
    confirmed_differences: int
    uncertain: int
    verified_match_pct: float
    confirmed_difference_pct: float
    uncertain_pct: float


@dataclass
class ScoreReport:
    overall_fidelity_pct: float
    categories: dict = field(default_factory=dict)  # category -> CategoryScore

    def to_dict(self) -> dict:
        return {
            "overall_fidelity_pct": round(self.overall_fidelity_pct, 2),
            "categories": {k: {
                "total_units": v.total_units, "confirmed_differences": v.confirmed_differences,
                "uncertain": v.uncertain, "verified_match_pct": round(v.verified_match_pct, 2),
                "confirmed_difference_pct": round(v.confirmed_difference_pct, 2),
                "uncertain_pct": round(v.uncertain_pct, 2),
            } for k, v in self.categories.items()},
        }


# Categories whose HIGH-severity findings represent a genuine, unresolved
# CONTENT error (spec: "ZONETOOL - ADVANCED PDF COMPARISON..." section 53/
# 61 - "missing characters, wrong Unicode, missing words, extra words,
# merged words, split words, missing paragraphs, duplicated content,
# reordered content" - a document with even ONE of these must FAIL
# regardless of its overall percentage score). "layout"/"figure"/"table"/
# "link"/"ocr" are deliberately excluded here - a pure layout/rendering
# difference must never by itself fail the whole document (spec 59/75).
CRITICAL_STATUS_CATEGORIES = {"content", "unicode", "structure"}


def _get_field(d, key):
    return d.get(key) if isinstance(d, dict) else getattr(d, key, None)


def is_production_error(d) -> bool:
    """True for a finding worth an operator's attention as a real content
    problem (spec: "EXACT CONTENT DIFFERENCE ENGINE" section 32/33 -
    "Default: Production Errors Only" / word merges and splits are listed
    among the "428 REAL PRODUCTION ERRORS") - a genuinely different,
    slightly WIDER bar than production_status()'s own HIGH-only PASS/FAIL
    gate below: MERGED_WORD/SPLIT_WORD are deliberately MEDIUM severity
    (spec's own uncertainty about false positives from vocabulary-based
    confidence), so a HIGH-only filter would hide them from the default
    view entirely, contradicting the spec's own worked examples. The ONE
    shared definition both app.comparison.difference_panel's "Production
    Errors Only" filter and summarize_differences()'s "real_errors" count
    below use - never two, potentially-disagreeing copies."""
    category = _get_field(d, "category")
    severity = _get_field(d, "severity")
    return category in CRITICAL_STATUS_CATEGORIES and severity in ("HIGH", "MEDIUM")


def production_status(differences: list) -> str:
    """Returns "FAIL" / "PASS WITH WARNINGS" / "PASS" (spec section 61) -
    a SEPARATE signal from the percentage scores above, computed directly
    from the difference list so a high overall score can never mask an
    unresolved content error. `differences` may be a list of Difference
    objects (engine.py's own return shape) or plain dicts (already-loaded
    report JSON) - both are read the same way via getattr/dict-style
    access below."""
    has_critical = False
    has_warning = False
    for d in differences:
        category = _get_field(d, "category")
        severity = _get_field(d, "severity")
        if category in CRITICAL_STATUS_CATEGORIES and severity == "HIGH":
            has_critical = True
        elif severity in ("MEDIUM", "LOW"):
            has_warning = True
    if has_critical:
        return "FAIL"
    if has_warning:
        return "PASS WITH WARNINGS"
    return "PASS"


# Buckets for the "428 REAL PRODUCTION ERRORS" summary (spec:
# "EPUBForge - EXACT CONTENT DIFFERENCE ENGINE" section 33 - "Do NOT make
# the percentage the primary information... Show: 428 REAL PRODUCTION
# ERRORS, Character errors: 71, Word errors: 84, ..."). Maps each REAL
# type string this engine actually emits into the spec's own named rows -
# never a fabricated category with no detector behind it: a bucket this
# engine has no real detector for (e.g. reading-order/content-reordering -
# confirmed absent by inspection) is simply omitted rather than shown as a
# fake zero that implies a check ran when none did.
SUMMARY_TYPE_BUCKETS = {
    "character_errors": {"TEXT_CHANGED"},
    "unicode_errors": {"HOMOGLYPH_SUBSTITUTION", "UNICODE_CODEPOINT_CHANGED",
                        "SPECIAL_CHARACTER_CHANGED", "LIGATURE_CHANGED"},
    "word_errors": {"MERGED_WORD", "SPLIT_WORD", "MISSING_SPACE", "EXTRA_SPACE"},
    "missing_content": {"MISSING_CONTENT", "MISSING_WORD"},
    "extra_content": {"ADDED_CONTENT", "ADDED_WORD"},
    "paragraph_errors": {"MERGED_PARAGRAPH", "SPLIT_PARAGRAPH"},
    "figure_errors": None,  # matched by category == "figure" below, not by type
    "table_errors": None,   # matched by category == "table" below, not by type
}
_TYPE_TO_BUCKET = {t: bucket for bucket, types in SUMMARY_TYPE_BUCKETS.items()
                    if types for t in types}


def summarize_differences(differences: list) -> dict:
    """Returns {"real_errors": N, "need_review": N, "informational": N,
    "by_type": {bucket_name: count, ...}} - a SEPARATE, additive view from
    calculate_scores() above (percentages), computed directly from actual
    difference records so the headline number is never just a raw
    "8778 differences found" with no explanation (spec section 40)."""
    by_type = {name: 0 for name in SUMMARY_TYPE_BUCKETS}
    real_errors = need_review = informational = 0
    for d in differences:
        category = _get_field(d, "category")
        dtype = _get_field(d, "type")
        confidence = _get_field(d, "confidence")

        bucket = _TYPE_TO_BUCKET.get(dtype)
        if bucket is None:
            if category == "figure":
                bucket = "figure_errors"
            elif category == "table":
                bucket = "table_errors"
        if bucket:
            by_type[bucket] += 1

        if is_production_error(d):
            real_errors += 1
        elif confidence == "LOW" or _get_field(d, "uncertain"):
            need_review += 1
        else:
            informational += 1

    return {
        "real_errors": real_errors, "need_review": need_review, "informational": informational,
        "by_type": {k: v for k, v in by_type.items() if v > 0},
    }


def calculate_scores(differences: list, totals: dict) -> ScoreReport:
    by_category = {}
    for d in differences:
        cat = _CATEGORY_MAP.get(d.get("category", "content"), "content")
        by_category.setdefault(cat, {"confirmed": 0, "uncertain": 0})
        if d.get("confidence") == "LOW" or d.get("uncertain"):
            by_category[cat]["uncertain"] += 1
        else:
            by_category[cat]["confirmed"] += 1

    categories = {}
    weighted_sum, weight_total = 0.0, 0.0
    for cat, total_units in totals.items():
        counts = by_category.get(cat, {"confirmed": 0, "uncertain": 0})
        confirmed = counts["confirmed"]
        uncertain = counts["uncertain"]
        denom = max(total_units, 1)
        match = max(0, denom - confirmed - uncertain)
        verified_pct = 100.0 * match / denom
        confirmed_pct = 100.0 * confirmed / denom
        uncertain_pct = 100.0 * uncertain / denom
        categories[cat] = CategoryScore(cat, total_units, confirmed, uncertain, verified_pct,
                                         confirmed_pct, uncertain_pct)
        if total_units > 0:
            weighted_sum += verified_pct * total_units
            weight_total += total_units

    overall = (weighted_sum / weight_total) if weight_total > 0 else 100.0
    return ScoreReport(overall_fidelity_pct=overall, categories=categories)
