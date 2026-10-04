"""Heading comparison (spec section 19) - text, level, and (when both
sides have real geometry, i.e. PDF vs PDF) layout via font_analyzer/
alignment_detector/spacing_analyzer, reused rather than reimplemented."""
from core.fidelity_compare import block_aligner, font_analyzer, alignment_detector, spacing_analyzer

TEXT_MATCH_THRESHOLD = 0.85


def collect_headings(document) -> list:
    headings = []
    for page, block in document.all_blocks():
        if block.kind == "heading":
            headings.append((page, block))
    return headings


def compare(original_doc, converted_doc) -> list:
    orig = collect_headings(original_doc)
    conv = collect_headings(converted_doc)
    pairs = block_aligner.align_by_key([b.text.semantic for _, b in orig], [b.text.semantic for _, b in conv])
    findings = []
    for p in pairs:
        if p.op == "equal":
            op, ob = orig[p.original_index]
            cp, cb = conv[p.converted_index]
            if ob.heading_level != cb.heading_level:
                findings.append({"type": "WRONG_HEADING_LEVEL", "original_page": op.number,
                                  "converted_page": cp.number, "text": ob.text.raw,
                                  "original": ob.heading_level, "converted": cb.heading_level,
                                  "severity": "MEDIUM", "confidence": 0.9})
            if ob.layout and cb.layout:
                layout_changes = []
                al = alignment_detector.compare(ob.layout, cb.layout)
                if al.get("changed"):
                    layout_changes.append(al)
                layout_changes.extend(font_analyzer.compare(ob.layout.font, cb.layout.font))
                sp = spacing_analyzer.compare_paragraph_spacing(ob.layout, cb.layout)
                layout_changes.extend(sp)
                if layout_changes:
                    findings.append({"type": "HEADING_LAYOUT_CHANGED", "original_page": op.number,
                                      "converted_page": cp.number, "text": ob.text.raw,
                                      "details": layout_changes, "severity": "MEDIUM", "confidence": 0.8})
        elif p.op == "delete":
            op, ob = orig[p.original_index]
            findings.append({"type": "MISSING_HEADING", "original_page": op.number, "text": ob.text.raw,
                              "severity": "HIGH", "confidence": 0.9})
        elif p.op == "insert":
            cp, cb = conv[p.converted_index]
            findings.append({"type": "ADDED_HEADING", "converted_page": cp.number, "text": cb.text.raw,
                              "severity": "HIGH", "confidence": 0.9})
        elif p.op == "replace":
            if p.original_index is not None and p.converted_index is not None:
                op, ob = orig[p.original_index]
                cp, cb = conv[p.converted_index]
                sim = block_aligner.similarity(ob.text.semantic, cb.text.semantic)
                if sim >= TEXT_MATCH_THRESHOLD:
                    findings.append({"type": "CHANGED_HEADING", "original_page": op.number,
                                      "converted_page": cp.number, "original_text": ob.text.raw,
                                      "converted_text": cb.text.raw, "severity": "MEDIUM", "confidence": sim})
                else:
                    findings.append({"type": "MISSING_HEADING", "original_page": op.number,
                                      "text": ob.text.raw, "severity": "HIGH", "confidence": 0.8})
                    findings.append({"type": "ADDED_HEADING", "converted_page": cp.number,
                                      "text": cb.text.raw, "severity": "HIGH", "confidence": 0.8})
            elif p.original_index is not None:
                op, ob = orig[p.original_index]
                findings.append({"type": "MISSING_HEADING", "original_page": op.number, "text": ob.text.raw,
                                  "severity": "HIGH", "confidence": 0.85})
            elif p.converted_index is not None:
                cp, cb = conv[p.converted_index]
                findings.append({"type": "ADDED_HEADING", "converted_page": cp.number, "text": cb.text.raw,
                                  "severity": "HIGH", "confidence": 0.85})
    return findings
