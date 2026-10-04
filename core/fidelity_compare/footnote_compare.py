"""Footnote comparison (spec section 23) - marker/text/order/backlink."""
from core.fidelity_compare import block_aligner

TEXT_MATCH_THRESHOLD = 0.85


def compare(original_doc, converted_doc) -> list:
    orig = original_doc.footnotes
    conv = converted_doc.footnotes
    pairs = block_aligner.align_by_key([f.text.semantic for f in orig], [f.text.semantic for f in conv])
    findings = []
    for p in pairs:
        if p.op == "equal":
            of, cf = orig[p.original_index], conv[p.converted_index]
            if of.marker and cf.marker and of.marker != cf.marker:
                findings.append({"type": "CHANGED_FOOTNOTE", "original_page": of.page, "converted_page": cf.page,
                                  "field": "marker", "original": of.marker, "converted": cf.marker,
                                  "severity": "MEDIUM", "confidence": 0.85})
            if of.target_id and cf.target_id is None:
                findings.append({"type": "BROKEN_FOOTNOTE_LINK", "original_page": of.page,
                                  "converted_page": cf.page, "severity": "HIGH", "confidence": 0.8})
        elif p.op == "delete":
            of = orig[p.original_index]
            findings.append({"type": "MISSING_FOOTNOTE", "original_page": of.page, "text": of.text.raw,
                              "severity": "HIGH", "confidence": 0.85})
        elif p.op == "insert":
            cf = conv[p.converted_index]
            findings.append({"type": "ADDED_FOOTNOTE", "converted_page": cf.page, "text": cf.text.raw,
                              "severity": "HIGH", "confidence": 0.85})
        elif p.original_index is not None and p.converted_index is not None:
            of, cf = orig[p.original_index], conv[p.converted_index]
            sim = block_aligner.similarity(of.text.semantic, cf.text.semantic)
            if sim >= TEXT_MATCH_THRESHOLD:
                findings.append({"type": "CHANGED_FOOTNOTE", "field": "text", "original_page": of.page,
                                  "converted_page": cf.page, "original_text": of.text.raw,
                                  "converted_text": cf.text.raw, "severity": "MEDIUM", "confidence": sim})
            else:
                findings.append({"type": "MISSING_FOOTNOTE", "original_page": of.page, "text": of.text.raw,
                                  "severity": "HIGH", "confidence": 0.75})
                findings.append({"type": "ADDED_FOOTNOTE", "converted_page": cf.page, "text": cf.text.raw,
                                  "severity": "HIGH", "confidence": 0.75})
    orig_order = [p.original_index for p in pairs if p.op == "equal"]
    if orig_order and orig_order != sorted(orig_order):
        findings.append({"type": "FOOTNOTE_REORDERED", "severity": "MEDIUM", "confidence": 0.6})
    return findings
