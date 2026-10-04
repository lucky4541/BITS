"""List comparison (spec section 20) - ordered/unordered/nested, bullets/
numbering via list_kind, indentation via list_depth, order preserved by
comparing against the reordered-detection helper below."""
from core.fidelity_compare import block_aligner

TEXT_MATCH_THRESHOLD = 0.85


def collect_list_items(document) -> list:
    return [(page, block) for page, block in document.all_blocks() if block.kind == "list_item"]


def compare(original_doc, converted_doc) -> list:
    orig = collect_list_items(original_doc)
    conv = collect_list_items(converted_doc)
    pairs = block_aligner.align_by_key([b.text.semantic for _, b in orig], [b.text.semantic for _, b in conv])
    findings = []
    matched_orig, matched_conv = set(), set()
    for p in pairs:
        if p.op == "equal":
            op, ob = orig[p.original_index]
            cp, cb = conv[p.converted_index]
            matched_orig.add(p.original_index)
            matched_conv.add(p.converted_index)
            if ob.list_kind != cb.list_kind or ob.list_depth != cb.list_depth:
                findings.append({"type": "LIST_STRUCTURE_CHANGED", "original_page": op.number,
                                  "converted_page": cp.number, "text": ob.text.raw,
                                  "original_kind": ob.list_kind, "converted_kind": cb.list_kind,
                                  "original_depth": ob.list_depth, "converted_depth": cb.list_depth,
                                  "severity": "MEDIUM", "confidence": 0.85})
        elif p.op == "delete":
            op, ob = orig[p.original_index]
            findings.append({"type": "MISSING_LIST_ITEM", "original_page": op.number, "text": ob.text.raw,
                              "severity": "HIGH", "confidence": 0.85})
        elif p.op == "insert":
            cp, cb = conv[p.converted_index]
            findings.append({"type": "ADDED_LIST_ITEM", "converted_page": cp.number, "text": cb.text.raw,
                              "severity": "HIGH", "confidence": 0.85})
        elif p.op == "replace" and p.original_index is not None and p.converted_index is not None:
            op, ob = orig[p.original_index]
            cp, cb = conv[p.converted_index]
            sim = block_aligner.similarity(ob.text.semantic, cb.text.semantic)
            if sim >= TEXT_MATCH_THRESHOLD:
                findings.append({"type": "CHANGED_LIST_ITEM", "original_page": op.number,
                                  "converted_page": cp.number, "original_text": ob.text.raw,
                                  "converted_text": cb.text.raw, "severity": "MEDIUM", "confidence": sim})
            else:
                findings.append({"type": "MISSING_LIST_ITEM", "original_page": op.number, "text": ob.text.raw,
                                  "severity": "HIGH", "confidence": 0.75})
                findings.append({"type": "ADDED_LIST_ITEM", "converted_page": cp.number, "text": cb.text.raw,
                                  "severity": "HIGH", "confidence": 0.75})

    # Reorder detection: same multiset of matched items, different relative order.
    matched_pairs = [(p.original_index, p.converted_index) for p in pairs if p.op == "equal"]
    if len(matched_pairs) >= 2:
        orig_order = [o for o, _ in matched_pairs]
        conv_order_for_orig = sorted(range(len(matched_pairs)), key=lambda k: matched_pairs[k][1])
        expected = sorted(orig_order)
        if [orig_order[k] for k in conv_order_for_orig] != expected and orig_order != sorted(orig_order):
            findings.append({"type": "LIST_REORDERED", "severity": "MEDIUM", "confidence": 0.6})
    return findings
