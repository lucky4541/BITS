"""Unicode/homoglyph verification - a thin wrapper around core.
fidelity_compare's unicode_detector/homoglyph_detector (pure functions,
reused as-is), plus surfacing core.text_extractor's OWN structural-
Unicode-repair diagnostics (get_unicode_repair_log - see core/
text_extractor.py's _repair_structural_unicode_anomalies, added earlier
this project for the "Franc{X}ois"/"cliche{X}" class of PDF ToUnicode
defects) as REVIEW-status issues instead of letting a low-confidence,
correctly-rejected repair disappear silently."""
import difflib

from core.fidelity_compare import unicode_detector


def find_unicode_char_issues(extracted_text: str, ocr_text: str) -> list:
    """Character-aligned comparison of the zone's own extracted text
    against an independent OCR reading. Only single-character 'replace'
    opcodes are inspected (a multi-character replace is a content/word
    issue, not a Unicode-mapping issue - content_verifier.py's own
    alignment already covers that case) - returns raw finding dicts:
        {"index": int, "original_char": str, "converted_char": str, **compare_chars result}
    `original_char`/`converted_char` follow unicode_detector.compare_chars'
    own original/converted naming (original = OCR evidence here)."""
    if not ocr_text or not extracted_text:
        return []
    matcher = difflib.SequenceMatcher(a=ocr_text, b=extracted_text, autojunk=False)
    findings = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace" or (i2 - i1) != 1 or (j2 - j1) != 1:
            continue
        original_char, converted_char = ocr_text[i1], extracted_text[j1]
        result = unicode_detector.compare_chars(original_char, converted_char)
        if not result.get("same"):
            findings.append({"index": j1, "original_char": original_char,
                              "converted_char": converted_char, **result})
    return findings


def repair_log_issues_for_pdf(pdf_path: str) -> list:
    """Every structural-Unicode-anomaly record text_extractor.py's own
    repair pass logged for this specific PDF, whether accepted or
    rejected - a REJECTED entry (insufficient OCR evidence at extraction
    time) is exactly the kind of low-confidence case the spec says must
    surface for human REVIEW rather than vanish."""
    from core.text_extractor import get_unicode_repair_log
    return [r for r in get_unicode_repair_log() if r.get("pdf") == pdf_path]
