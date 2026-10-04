"""Word merge/split verification - a thin orchestration wrapper around
core.fidelity_compare.merged_word_detector's own pure evaluate_merge/
evaluate_split functions (reused as-is, never reimplemented).

Compares the zone's own CURRENTLY EXTRACTED text (what would go into
XHTML today) against an independent second reading (OCR evidence, when
one is available for this zone) - a genuine merge means the extracted
text wrongly joined two OCR-confirmed separate words ("NewYork" where OCR
saw "New York"); a genuine split means the extracted text wrongly broke
one OCR-confirmed word into pieces ("Uni versity" where OCR saw
"University"). Per merged_word_detector's own design, the vocabulary is
only ever SUPPORTING evidence - it can raise/lower confidence, never
invent a merge/split that the two texts' own word alignment doesn't
already show."""
import difflib

from core.fidelity_compare import merged_word_detector


def _words(text: str) -> list:
    return (text or "").split()


def build_vocabulary_from_zones(zones) -> set:
    """merged_word_detector.build_vocabulary expects core.fidelity_compare.
    document_model.Document objects (.all_blocks()) - this project's own
    Zone objects aren't that, so this builds the same KIND of self-
    referential vocabulary (every word already seen elsewhere in this
    project's own zones is real, honest evidence it's a genuine word -
    the exact rationale merged_word_detector's own docstring gives)
    directly from Zone.text, without needing the heavier Document
    wrapper. `zones` is any iterable of objects with a `.text` attribute
    (core.zone_manager.ZoneManager.zones.values(), typically)."""
    vocab = set()
    for zone in zones:
        for word in (zone.text or "").split():
            cleaned = word.strip(".,;:!?\"'()[]{}‘’“”")
            if cleaned:
                vocab.add(cleaned)
    return vocab


def find_word_join_issues(extracted_text: str, ocr_text: str, vocabulary: set) -> list:
    """Returns a list of raw finding dicts (not yet wrapped as Difference/
    VerificationIssue - see verification_engine.py for that step):
        {"kind": "merge", "original_words": [...], "converted_word": str, **evaluate_merge result}
        {"kind": "split", "original_word": str, "converted_words": [...], **evaluate_split result}
    `original_words`/`original_word` here refer to the OCR (evidence) side
    - merged_word_detector's own naming convention, kept consistent with
    it rather than renamed, since its evaluate_merge/evaluate_split
    signatures use exactly these parameter names."""
    if not ocr_text or not ocr_text.strip():
        return []
    ocr_words = _words(ocr_text)
    extracted_words = _words(extracted_text)
    if not ocr_words or not extracted_words:
        return []

    matcher = difflib.SequenceMatcher(
        a=[w.casefold() for w in ocr_words], b=[w.casefold() for w in extracted_words], autojunk=False)
    findings = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        ocr_chunk = ocr_words[i1:i2]
        extracted_chunk = extracted_words[j1:j2]
        if len(ocr_chunk) >= 2 and len(extracted_chunk) == 1:
            result = merged_word_detector.evaluate_merge(ocr_chunk, extracted_chunk[0], vocabulary)
            if result.get("is_merge"):
                findings.append({"kind": "merge", "original_words": ocr_chunk,
                                  "converted_word": extracted_chunk[0], "word_index": j1, **result})
        elif len(ocr_chunk) == 1 and len(extracted_chunk) >= 2:
            result = merged_word_detector.evaluate_split(ocr_chunk[0], extracted_chunk, vocabulary)
            if result.get("is_split"):
                findings.append({"kind": "split", "original_word": ocr_chunk[0],
                                  "converted_words": extracted_chunk, "word_index": j1, **result})
    return findings
