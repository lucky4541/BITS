"""Index comparison (spec section 25) - entry/parent/child hierarchy via
depth, and page references, order preserved."""
from core.fidelity_compare import block_aligner

TEXT_MATCH_THRESHOLD = 0.85


def compare(original_doc, converted_doc) -> list:
    orig = original_doc.index_entries
    conv = converted_doc.index_entries
    pairs = block_aligner.align_by_key([e.text.semantic for e in orig], [e.text.semantic for e in conv])
    findings = []
    for p in pairs:
        if p.op == "equal":
            eo, ec = orig[p.original_index], conv[p.converted_index]
            if eo.depth != ec.depth:
                findings.append({"type": "INDEX_HIERARCHY_CHANGED", "text": eo.text.raw,
                                  "original_depth": eo.depth, "converted_depth": ec.depth,
                                  "severity": "MEDIUM", "confidence": 0.8})
        elif p.op == "delete":
            e = orig[p.original_index]
            findings.append({"type": "MISSING_INDEX_ENTRY", "text": e.text.raw, "severity": "HIGH",
                              "confidence": 0.85})
        elif p.op == "insert":
            e = conv[p.converted_index]
            findings.append({"type": "ADDED_INDEX_ENTRY", "text": e.text.raw, "severity": "HIGH",
                              "confidence": 0.85})
        elif p.op == "replace" and p.original_index is not None and p.converted_index is not None:
            eo, ec = orig[p.original_index], conv[p.converted_index]
            sim = block_aligner.similarity(eo.text.semantic, ec.text.semantic)
            if sim >= TEXT_MATCH_THRESHOLD:
                findings.append({"type": "CHANGED_INDEX_ENTRY", "original_text": eo.text.raw,
                                  "converted_text": ec.text.raw, "severity": "MEDIUM", "confidence": sim})
            else:
                findings.append({"type": "MISSING_INDEX_ENTRY", "text": eo.text.raw, "severity": "HIGH",
                                  "confidence": 0.75})
                findings.append({"type": "ADDED_INDEX_ENTRY", "text": ec.text.raw, "severity": "HIGH",
                                  "confidence": 0.75})
    return findings
