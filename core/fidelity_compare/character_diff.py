"""Character-level diff between two aligned word/token strings (the
finest level of spec section 53's hierarchical alignment). Used whenever
word_aligner has already decided two words correspond to each other but
their RAW text differs, to classify exactly WHAT changed - a plain typo,
a Unicode substitution, a homoglyph, a ligature difference, or a special
character change (spec sections 12/13/15/16)."""
import difflib

from core.fidelity_compare import homoglyph_detector, text_forms, unicode_detector

_SPECIAL_CHARS = set("©®™°±≤≥≠×÷−αβγΔΩμπ§¶†‡•")


def diff_words(original: str, converted: str) -> dict:
    """Returns a structured breakdown: per-character-pair unicode/homoglyph
    findings for equal-length aligned spans, plus an overall classification
    ('LIGATURE_CHANGED' / 'HOMOGLYPH_SUBSTITUTION' / 'UNICODE_CODEPOINT_
    CHANGED' / 'SPECIAL_CHARACTER_CHANGED' / 'TEXT_CHANGED') so callers can
    pick the most specific true label rather than a generic diff."""
    if original == converted:
        return {"changed": False}

    ligature_involved = text_forms.contains_ligature(original) or text_forms.contains_ligature(converted)
    if ligature_involved and text_forms.expand_ligatures(original) == text_forms.expand_ligatures(converted):
        return {"changed": True, "classification": "LIGATURE_CHANGED",
                "raw_different": True, "semantic_equivalent": True,
                "original": original, "converted": converted}

    sm = difflib.SequenceMatcher(a=original, b=converted, autojunk=False)
    char_findings = []
    homoglyph_found = False
    unicode_found = False
    special_found = False
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        seg_a = original[i1:i2]
        seg_b = converted[j1:j2]
        if len(seg_a) == 1 and len(seg_b) == 1:
            cmp = unicode_detector.compare_chars(seg_a, seg_b)
            if not cmp.get("same"):
                char_findings.append({"position": i1, **cmp})
                if cmp.get("is_homoglyph"):
                    homoglyph_found = True
                elif seg_a in _SPECIAL_CHARS or seg_b in _SPECIAL_CHARS:
                    special_found = True
                else:
                    unicode_found = True
        else:
            char_findings.append({"position": i1, "same": False, "original_text": seg_a, "converted_text": seg_b})

    if homoglyph_found:
        classification = "HOMOGLYPH_SUBSTITUTION"
    elif special_found:
        classification = "SPECIAL_CHARACTER_CHANGED"
    elif unicode_found:
        classification = "UNICODE_CODEPOINT_CHANGED"
    else:
        classification = "TEXT_CHANGED"

    return {
        "changed": True, "classification": classification, "raw_different": True,
        "semantic_equivalent": text_forms.semantic(original) == text_forms.semantic(converted),
        "original": original, "converted": converted, "char_findings": char_findings,
    }
