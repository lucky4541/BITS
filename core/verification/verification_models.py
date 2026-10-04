"""VerificationIssue / VerificationSession - the user-facing data model for
the Verification workflow.

VerificationIssue is deliberately a thin, additive VIEW over core.
fidelity_compare.difference_model.Difference (the existing, reused finding
representation - see core/verification/__init__.py) rather than a
competing model: every issue this package produces is built FROM a real
Difference via VerificationIssue.from_difference(). The two models differ
because they serve different moments in the pipeline - Difference
describes a COMPARISON RESULT (MATCH/MISSING/EXTRA/CHANGED/...); a
VerificationIssue additionally tracks an interactive FIX WORKFLOW state
(OPEN -> AUTO_FIX_AVAILABLE/REVIEW -> FIXED/IGNORED/REJECTED) that has no
equivalent in the comparison-only model, and carries a few zone/PDF-
specific fields (zone_id, ocr_text, formatting_expected/found,
suggested_text) the generic comparison model has no reason to know about.
"""
from dataclasses import dataclass, field
from typing import Optional

from core.fidelity_compare.difference_model import Difference, bbox_dict
from core.fidelity_compare import confidence_engine


# ============================================================
# ISSUE TYPES (spec's own exact vocabulary)
# ============================================================

CONTENT = "CONTENT"
UNICODE = "UNICODE"
SPELLING = "SPELLING"
GRAMMAR = "GRAMMAR"
WORD_JOIN = "WORD_JOIN"
WORD_SPLIT = "WORD_SPLIT"
HYPHENATION = "HYPHENATION"
PUNCTUATION = "PUNCTUATION"
ITALIC = "ITALIC"
BOLD = "BOLD"
UNDERLINE = "UNDERLINE"
SUPERSCRIPT = "SUPERSCRIPT"
SUBSCRIPT = "SUBSCRIPT"
SMALLCAPS = "SMALLCAPS"
FONT = "FONT"
FONT_SIZE = "FONT_SIZE"
PARAGRAPH = "PARAGRAPH"
READING_ORDER = "READING_ORDER"
INLINE_STYLE = "INLINE_STYLE"
STRIKETHROUGH = "STRIKETHROUGH"
ALIGNMENT = "ALIGNMENT"
STRUCTURE = "STRUCTURE"
OTHER = "OTHER"

ALL_ISSUE_TYPES = (
    CONTENT, UNICODE, SPELLING, GRAMMAR, WORD_JOIN, WORD_SPLIT, HYPHENATION,
    PUNCTUATION, ITALIC, BOLD, UNDERLINE, SUPERSCRIPT, SUBSCRIPT, SMALLCAPS,
    FONT, FONT_SIZE, PARAGRAPH, READING_ORDER, INLINE_STYLE, STRIKETHROUGH,
    ALIGNMENT, STRUCTURE, OTHER,
)

# The user's own requested grouping for the issue list panel (spec:
# "Group issues: Content / Formatting / Sup-Sub / Unicode / Spelling /
# Grammar / Word Joining / Paragraph / Structure").
ISSUE_GROUPS = {
    CONTENT: "Content",
    PUNCTUATION: "Content",
    UNICODE: "Unicode",
    SPELLING: "Spelling",
    GRAMMAR: "Grammar",
    WORD_JOIN: "Word Joining",
    WORD_SPLIT: "Word Joining",
    HYPHENATION: "Word Joining",
    ITALIC: "Formatting",
    BOLD: "Formatting",
    UNDERLINE: "Formatting",
    FONT: "Formatting",
    FONT_SIZE: "Formatting",
    SMALLCAPS: "Formatting",
    SUPERSCRIPT: "Sup/Sub",
    SUBSCRIPT: "Sup/Sub",
    PARAGRAPH: "Structure",
    READING_ORDER: "Structure",
    INLINE_STYLE: "Formatting",
    STRIKETHROUGH: "Formatting",
    ALIGNMENT: "Formatting",
    STRUCTURE: "Structure",
    OTHER: "Structure",
}


# ============================================================
# ISSUE STATUS (spec's own exact vocabulary)
# ============================================================

OPEN = "OPEN"
AUTO_FIX_AVAILABLE = "AUTO_FIX_AVAILABLE"
FIXED = "FIXED"
IGNORED = "IGNORED"
REVIEW = "REVIEW"
REJECTED = "REJECTED"
# Added for the Verification window redesign's own exact status
# vocabulary/lifecycle (OPEN -> Apply Fix -> RECHECK_REQUIRED -> Recheck
# Zone -> VERIFIED) - additive; every status above keeps its existing
# meaning and every already-persisted project's issue statuses remain
# valid (none of the original 6 values were renamed or removed).
AUTO_FIXED = "AUTO_FIXED"          # a fix was applied automatically (high-confidence, evidence-backed)
RECHECK_REQUIRED = "RECHECK_REQUIRED"  # a manual/local edit was made; awaiting an explicit recheck
VERIFIED = "VERIFIED"              # rechecked after a fix and confirmed resolved

_UNRESOLVED_STATUSES = (OPEN, AUTO_FIX_AVAILABLE, REVIEW, RECHECK_REQUIRED)

# Types that must NEVER reach AUTO_FIX_AVAILABLE, even with perfect
# corroborating evidence (spec: "Grammar checking must NEVER freely
# rewrite book content" / "Spell checking is advisory unless PDF/OCR
# evidence confirms the correction").
_ADVISORY_ONLY_TYPES = (GRAMMAR,)


@dataclass
class VerificationIssue:
    id: str
    issue_type: str
    severity: str                     # HIGH / MEDIUM / LOW (confidence_engine.bucket vocabulary)
    status: str = OPEN

    page: Optional[int] = None
    zone_id: Optional[str] = None
    pdf_coordinates: Optional[dict] = None   # {x0,y0,x1,y1} in PDF points

    # character/word range within the ZONE's own extracted text (not the
    # whole document) - (start, end) character offsets, half-open.
    char_range: Optional[tuple] = None
    word_index: Optional[int] = None
    word_span_count: int = 1   # how many CURRENTLY-EXTRACTED words suggested_text should replace

    source_text: str = ""       # the zone's own saved/extracted text at this range, as it stands today
    extracted_text: str = ""    # same as source_text today - kept distinct because a FIX changes source_text
    ocr_text: Optional[str] = None       # independent second reading, when one was fetched
    expected_text: Optional[str] = None  # the evidence-backed "what it should say", if determined
    suggested_text: Optional[str] = None # what Apply Fix would write, if status allows it

    confidence: str = "LOW"      # HIGH / MEDIUM / LOW
    confidence_score: float = 0.0
    source_evidence: str = ""    # human-readable: "PDF native + OCR agree", "dictionary only", ...

    formatting_expected: Optional[dict] = None  # {bold,italic,underline,superscript,subscript,smallcaps}
    formatting_found: Optional[dict] = None

    # When set, Apply Fix means "apply this span_model style field to
    # char_range" (e.g. "superscript") via the SAME instant, no-recheck
    # apply_style path a manual toolbar click uses - NOT a text
    # replacement. Mutually exclusive with suggested_text-as-replacement
    # in practice (a style fix's own suggested_text, when present, is
    # just the unchanged text shown for context/detail-panel display).
    style_field: Optional[str] = None

    explanation: str = ""
    difference: Optional[Difference] = None   # the underlying reused comparison result, kept for audit

    def to_dict(self) -> dict:
        return {
            "id": self.id, "issue_type": self.issue_type, "severity": self.severity, "status": self.status,
            "page": self.page, "zone_id": self.zone_id, "pdf_coordinates": self.pdf_coordinates,
            "char_range": list(self.char_range) if self.char_range else None, "word_index": self.word_index,
            "word_span_count": self.word_span_count,
            "source_text": self.source_text, "extracted_text": self.extracted_text, "ocr_text": self.ocr_text,
            "expected_text": self.expected_text, "suggested_text": self.suggested_text,
            "confidence": self.confidence, "confidence_score": self.confidence_score,
            "source_evidence": self.source_evidence,
            "formatting_expected": self.formatting_expected, "formatting_found": self.formatting_found,
            "style_field": self.style_field,
            "explanation": self.explanation,
            "difference": self.difference.to_dict() if self.difference else None,
        }

    @staticmethod
    def from_dict(d: dict) -> "VerificationIssue":
        cr = d.get("char_range")
        return VerificationIssue(
            id=d["id"], issue_type=d["issue_type"], severity=d.get("severity", "LOW"),
            status=d.get("status", OPEN), page=d.get("page"), zone_id=d.get("zone_id"),
            pdf_coordinates=d.get("pdf_coordinates"),
            char_range=tuple(cr) if cr else None, word_index=d.get("word_index"),
            word_span_count=d.get("word_span_count", 1),
            source_text=d.get("source_text", ""), extracted_text=d.get("extracted_text", ""),
            ocr_text=d.get("ocr_text"), expected_text=d.get("expected_text"),
            suggested_text=d.get("suggested_text"),
            confidence=d.get("confidence", "LOW"), confidence_score=d.get("confidence_score", 0.0),
            source_evidence=d.get("source_evidence", ""),
            formatting_expected=d.get("formatting_expected"), formatting_found=d.get("formatting_found"),
            style_field=d.get("style_field"),
            explanation=d.get("explanation", ""),
            # `difference` is audit-only and reconstructible from the fields above; not round-tripped
            # through project JSON to avoid persisting the (larger, comparison-oriented) Difference twice.
            difference=None,
        )

    @staticmethod
    def from_difference(diff: Difference, issue_type: str, *, zone_id: str = None,
                         char_range: tuple = None, word_index: int = None, word_span_count: int = 1,
                         ocr_text: str = None, expected_text: str = None, suggested_text: str = None,
                         source_evidence: str = "", formatting_expected: dict = None,
                         formatting_found: dict = None, auto_fixable: bool = False,
                         style_field: str = None) -> "VerificationIssue":
        """The one, single adapter from a reused Difference to the
        interactive fix-workflow model. `auto_fixable` is the caller's own
        evidence-based judgement (e.g. "OCR and PDF-native agree on the
        correction") - GRAMMAR issues can never be auto-fixable regardless
        (see _ADVISORY_ONLY_TYPES), matching the spec's own hard rule."""
        status = REVIEW
        if issue_type not in _ADVISORY_ONLY_TYPES and auto_fixable and suggested_text is not None:
            status = AUTO_FIX_AVAILABLE
        elif diff.confidence == "LOW" or issue_type in _ADVISORY_ONLY_TYPES:
            status = REVIEW if issue_type in _ADVISORY_ONLY_TYPES else OPEN
        else:
            status = OPEN

        return VerificationIssue(
            id=diff.id, issue_type=issue_type, severity=diff.severity, status=status,
            page=diff.original_page, zone_id=zone_id,
            pdf_coordinates=diff.original_bbox.to_dict() if diff.original_bbox else None,
            char_range=char_range, word_index=word_index if word_index is not None else diff.original_word_index,
            word_span_count=word_span_count,
            source_text=diff.original_text, extracted_text=diff.converted_text,
            ocr_text=ocr_text, expected_text=expected_text, suggested_text=suggested_text,
            confidence=diff.confidence, confidence_score=diff.confidence_score,
            source_evidence=source_evidence,
            formatting_expected=formatting_expected, formatting_found=formatting_found,
            style_field=style_field,
            explanation=diff.explanation, difference=diff,
        )

    def is_unresolved(self) -> bool:
        return self.status in _UNRESOLVED_STATUSES

    def apply(self, new_text: str = None):
        """Marks this issue FIXED. Does not itself mutate any Zone - the
        caller (verification_orchestrator.apply_fix) is responsible for
        writing new_text back to the zone and re-running verification on
        that scope, per the spec's own "verification loop" requirement."""
        if new_text is not None:
            self.source_text = new_text
        self.status = FIXED

    def ignore(self):
        self.status = IGNORED

    def reject(self):
        self.status = REJECTED


def make_issue_id() -> str:
    from core.fidelity_compare.difference_model import next_id
    return next_id("verif")


def build_pdf_coordinates(bbox) -> Optional[dict]:
    bd = bbox_dict(bbox)
    return bd.to_dict() if bd else None


def confidence_from_scores(*scores: float, weights: tuple = None) -> tuple:
    """Returns (bucket_str, combined_score) - the one place this package
    turns multiple independent evidence signals into the confidence
    fields every VerificationIssue/Difference carries, reusing
    core.fidelity_compare.confidence_engine rather than inventing a
    second thresholding scheme."""
    score = confidence_engine.combine(*scores, weights=weights)
    return confidence_engine.bucket(score), score


@dataclass
class VerificationSession:
    """Per-project verification state. Persisted as one new top-level key
    in the existing project JSON (see verification_session.py) - never a
    parallel project file."""
    issues: list = field(default_factory=list)   # list[VerificationIssue]
    dictionary_additions: list = field(default_factory=list)  # spell-check "Add to Dictionary" words
    last_run_at: Optional[str] = None

    def is_complete(self) -> bool:
        return not any(i.is_unresolved() for i in self.issues)

    def unresolved_count(self) -> int:
        return sum(1 for i in self.issues if i.is_unresolved())

    def counts_by_group(self) -> dict:
        counts = {}
        for issue in self.issues:
            group = ISSUE_GROUPS.get(issue.issue_type, "Structure")
            counts[group] = counts.get(group, 0) + 1
        return counts

    def issues_for_zone(self, zone_id: str) -> list:
        return [i for i in self.issues if i.zone_id == zone_id]

    def replace_zone_issues(self, zone_id: str, new_issues: list):
        """Used by recheck() - drops every EXISTING issue for this zone
        (whatever their status) and installs the freshly-computed list,
        which is how a fix -> recheck -> PASS transition actually clears
        a resolved issue from the list rather than leaving a stale FIXED
        entry behind forever."""
        self.issues = [i for i in self.issues if i.zone_id != zone_id] + list(new_issues)

    def to_dict(self) -> dict:
        return {
            "issues": [i.to_dict() for i in self.issues],
            "dictionary_additions": list(self.dictionary_additions),
            "last_run_at": self.last_run_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "VerificationSession":
        if not d:
            return VerificationSession()
        return VerificationSession(
            issues=[VerificationIssue.from_dict(i) for i in d.get("issues", [])],
            dictionary_additions=list(d.get("dictionary_additions", [])),
            last_run_at=d.get("last_run_at"),
        )
