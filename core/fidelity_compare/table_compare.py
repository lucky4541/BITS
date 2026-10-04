"""Table comparison (spec section 22) - compares cells individually,
never flattening a table into one text string. Tables are matched
across documents by position-in-document-order first (most books have
few enough tables that this is reliable), falling back to caption/
structure similarity when counts differ."""
from core.fidelity_compare import block_aligner

CELL_MATCH_THRESHOLD = 0.85


def _table_key(table) -> str:
    if table.caption:
        return table.caption.semantic
    return " ".join(c.text.semantic for c in table.cells[:20])


def compare(original_doc, converted_doc, progress_cb=None) -> list:
    orig_tables = [(page, t) for page in original_doc.pages for t in page.tables]
    conv_tables = [(page, t) for page in converted_doc.pages for t in page.tables]
    pairs = block_aligner.align_by_key([_table_key(t) for _, t in orig_tables],
                                        [_table_key(t) for _, t in conv_tables])
    findings = []
    total = len(pairs) or 1
    for i, p in enumerate(pairs, start=1):
        if progress_cb:
            progress_cb(i, total)
        if p.op == "delete":
            op, ot = orig_tables[p.original_index]
            findings.append({"type": "MISSING_TABLE", "original_page": op.number, "severity": "HIGH",
                              "confidence": 0.85})
        elif p.op == "insert":
            cp, ct = conv_tables[p.converted_index]
            findings.append({"type": "ADDED_TABLE", "converted_page": cp.number, "severity": "HIGH",
                              "confidence": 0.85})
        elif p.original_index is not None and p.converted_index is not None:
            op, ot = orig_tables[p.original_index]
            cp, ct = conv_tables[p.converted_index]
            findings.extend(_compare_one_table(op, ot, cp, ct))
    return findings


def _compare_one_table(op, ot, cp, ct) -> list:
    findings = []
    if ot.rows != ct.rows:
        findings.append({"type": "TABLE_ROW_CHANGED", "original_page": op.number, "converted_page": cp.number,
                          "original": ot.rows, "converted": ct.rows, "severity": "HIGH", "confidence": 0.9})
    if ot.cols != ct.cols:
        findings.append({"type": "TABLE_COLUMN_CHANGED", "original_page": op.number, "converted_page": cp.number,
                          "original": ot.cols, "converted": ct.cols, "severity": "HIGH", "confidence": 0.9})
    if ot.rows != ct.rows or ot.cols != ct.cols:
        findings.append({"type": "TABLE_STRUCTURE_CHANGED", "original_page": op.number,
                          "converted_page": cp.number, "severity": "HIGH", "confidence": 0.9})

    orig_by_pos = {(c.row, c.col): c for c in ot.cells}
    conv_by_pos = {(c.row, c.col): c for c in ct.cells}
    for pos, oc in orig_by_pos.items():
        cc = conv_by_pos.get(pos)
        if cc is None:
            continue
        if oc.text.semantic != cc.text.semantic:
            sim = block_aligner.similarity(oc.text.semantic, cc.text.semantic)
            findings.append({"type": "TABLE_CELL_CHANGED", "original_page": op.number,
                              "converted_page": cp.number, "row": pos[0], "col": pos[1],
                              "original_text": oc.text.raw, "converted_text": cc.text.raw,
                              "severity": "MEDIUM", "confidence": max(0.5, sim)})
        if oc.rowspan != cc.rowspan or oc.colspan != cc.colspan or oc.is_header != cc.is_header:
            findings.append({"type": "TABLE_STRUCTURE_CHANGED", "original_page": op.number,
                              "converted_page": cp.number, "row": pos[0], "col": pos[1],
                              "severity": "MEDIUM", "confidence": 0.7})

    if ot.caption and ct.caption and ot.caption.semantic != ct.caption.semantic:
        findings.append({"type": "CHANGED_CAPTION", "original_page": op.number, "converted_page": cp.number,
                          "original_text": ot.caption.raw, "converted_text": ct.caption.raw,
                          "severity": "MEDIUM", "confidence": 0.8})
    elif ot.caption and not ct.caption:
        findings.append({"type": "MISSING_CAPTION", "original_page": op.number, "text": ot.caption.raw,
                          "severity": "MEDIUM", "confidence": 0.75})

    if ot.bbox.width and ct.bbox.width and abs(ot.bbox.x0 - ct.bbox.x0) > 8 and op.number != cp.number:
        findings.append({"type": "TABLE_POSITION_CHANGED", "original_page": op.number,
                          "converted_page": cp.number, "severity": "LOW", "confidence": 0.6})
    return findings
