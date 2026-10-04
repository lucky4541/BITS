"""Font comparison (spec section 41) - "Do not make font differences
count as missing text": these findings are always reported as their own
FONT_* category, never folded into a content Difference."""
from core.fidelity_compare.layout_analyzer import FONT_SIZE_TOLERANCE_PT
from difflib import SequenceMatcher


def _font_signature(font):
    return (bool(font.bold), bool(font.italic), bool(font.underline),
            bool(font.superscript), bool(font.subscript))


def compare_word_runs(original_words, converted_words):
    """Compare typography at word/run level instead of only block dominant font.

    Words are aligned by their semantic text. A formatting difference is only
    emitted when the same word is present on both sides and the style persists
    across the matched word. This prevents one stray glyph or a whole-block
    dominant-font choice from producing false positives.
    """
    o = [w for w in (original_words or []) if getattr(w, "text", None) and w.text.semantic.strip()]
    c = [w for w in (converted_words or []) if getattr(w, "text", None) and w.text.semantic.strip()]
    if not o or not c:
        return []
    oa = [w.text.semantic for w in o]
    ca = [w.text.semantic for w in c]
    if oa == ca:
        matched_pairs = zip(o, c)
        findings = []
        for ow, cw in matched_pairs:
            if _font_signature(ow.font) == _font_signature(cw.font):
                continue
            for field in ("bold", "italic", "underline", "superscript", "subscript"):
                ov, cv = getattr(ow.font, field), getattr(cw.font, field)
                if ov != cv:
                    score = 0.98 if field in ("italic", "bold") else 0.94
                    findings.append({"type": f"{field.upper()}_CHANGED", "original": ov, "converted": cv,
                                     "original_text": ow.text.raw, "converted_text": cw.text.raw,
                                     "original_bbox": ow.bbox, "converted_bbox": cw.bbox,
                                     "severity": "MEDIUM", "confidence": score,
                                     "evidence": "word-level native PDF typography"})
        return findings
    sm = SequenceMatcher(None, oa, ca, autojunk=False)
    findings = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for oi, ci in zip(range(i1, i2), range(j1, j2)):
            ow, cw = o[oi], c[ci]
            if _font_signature(ow.font) == _font_signature(cw.font):
                continue
            fields = ("bold", "italic", "underline", "superscript", "subscript")
            for field in fields:
                ov, cv = getattr(ow.font, field), getattr(cw.font, field)
                if ov != cv:
                    score = 0.98 if field in ("italic", "bold") else 0.94
                    findings.append({
                        "type": f"{field.upper()}_CHANGED",
                        "original": ov, "converted": cv,
                        "original_text": ow.text.raw, "converted_text": cw.text.raw,
                        "original_bbox": ow.bbox, "converted_bbox": cw.bbox,
                        "severity": "MEDIUM", "confidence": score,
                        "evidence": "word-level native PDF typography",
                    })
    return findings


def compare_run_formatting(original_lines, converted_lines):
    """Fallback for inline runs that do not align cleanly as whole words."""
    findings = []
    for ol, cl in zip(original_lines or [], converted_lines or []):
        ors = getattr(ol, "format_runs", []) or []
        crs = getattr(cl, "format_runs", []) or []
        if not ors or not crs:
            continue
        # Exact/near-exact run text matching. Never classify from one char.
        # Most formatting runs have exact text matches. Index them first so
        # the expensive fuzzy SequenceMatcher fallback is only used for runs
        # that genuinely need fuzzy matching.
        exact = {}
        for q in crs:
            key = q.get("text", "").strip().casefold()
            if len(key) >= 2:
                exact.setdefault(key, q)
        for r in ors:
            key = r.get("text", "").strip().casefold()
            if len(key) < 2:
                continue
            best = exact.get(key)
            best_ratio = 1.0 if best is not None else 0.0
            if best is None:
                for q in crs:
                    ratio = SequenceMatcher(None, key, q.get("text", "").strip().casefold(), autojunk=False).ratio()
                    if ratio > best_ratio:
                        best_ratio, best = ratio, q
            if best is None or best_ratio < 0.92:
                continue
            of, cf = r["font"], best["font"]
            for field in ("bold", "italic", "underline", "superscript", "subscript"):
                if getattr(of, field) != getattr(cf, field):
                    findings.append({
                        "type": f"{field.upper()}_CHANGED",
                        "original": getattr(of, field), "converted": getattr(cf, field),
                        "original_text": r["text"], "converted_text": best["text"],
                        "original_bbox": r["bbox"], "converted_bbox": best["bbox"],
                        "severity": "MEDIUM", "confidence": min(0.96, 0.75 + 0.2 * best_ratio),
                        "evidence": "inline run native PDF typography",
                    })
    return findings


def compare(original_font, converted_font) -> list:
    if original_font is None or converted_font is None:
        return []
    findings = []
    if original_font.family and converted_font.family and original_font.family != converted_font.family:
        findings.append({"type": "FONT_CHANGED", "original": original_font.family,
                          "converted": converted_font.family, "severity": "LOW", "confidence": 0.6})
    if original_font.size and converted_font.size \
            and abs(original_font.size - converted_font.size) > FONT_SIZE_TOLERANCE_PT:
        findings.append({"type": "FONT_SIZE_CHANGED", "original": original_font.size,
                          "converted": converted_font.size, "severity": "MEDIUM", "confidence": 0.85})
    if original_font.bold != converted_font.bold:
        findings.append({"type": "BOLD_CHANGED", "original": original_font.bold,
                          "converted": converted_font.bold, "severity": "MEDIUM", "confidence": 0.8})
    if original_font.italic != converted_font.italic:
        findings.append({"type": "ITALIC_CHANGED", "original": original_font.italic,
                          "converted": converted_font.italic, "severity": "MEDIUM", "confidence": 0.8})
    if original_font.underline != converted_font.underline:
        findings.append({"type": "UNDERLINE_CHANGED", "original": original_font.underline,
                          "converted": converted_font.underline, "severity": "LOW", "confidence": 0.6})
    if original_font.superscript != converted_font.superscript:
        findings.append({"type": "SUPERSCRIPT_CHANGED", "original": original_font.superscript,
                          "converted": converted_font.superscript, "severity": "MEDIUM", "confidence": 0.7})
    if original_font.subscript != converted_font.subscript:
        findings.append({"type": "SUBSCRIPT_CHANGED", "original": original_font.subscript,
                          "converted": converted_font.subscript, "severity": "MEDIUM", "confidence": 0.7})
    return findings
