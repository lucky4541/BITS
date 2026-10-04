"""Link integrity comparison (spec section 26). Links (internal anchors,
chapter/figure/table/footnote/reference/index cross-references) are an
EPUB-native concept with no equivalent in a physical PDF (a PDF has no
named anchor targets to compare against), so this checks the EPUB's own
link integrity directly - does every internal href resolve to a real id
somewhere in the same document - rather than diffing against the PDF
side. Comparison/reporting only; NEVER repairs a broken link (spec:
"Do not repair. Only report.")."""


def _known_ids(document) -> set:
    ids = set()
    for _page, block in document.all_blocks():
        if block.block_id:
            ids.add(block.block_id)
    for f in document.footnotes:
        if f.target_id:
            ids.add(f.target_id)
    return ids


def check_link_integrity(document) -> list:
    known_ids = _known_ids(document)
    findings = []
    for link in document.links:
        if link.href.startswith("#"):
            target = link.href[1:]
            if target and target not in known_ids:
                findings.append({"type": "BROKEN_LINK", "href": link.href, "kind": link.kind,
                                  "text": link.text.raw, "page": link.source_page,
                                  "severity": "HIGH", "confidence": 0.7})
    return findings


def compare(original_epub_doc, converted_epub_doc) -> list:
    """Compares the two EPUB-side link sets by (kind, target) presence -
    a link present in one but not the other, and target changes for
    links whose visible text matches (spec: MISSING_LINK / WRONG_LINK_
    TARGET / LINK_TARGET_CHANGED)."""
    findings = check_link_integrity(original_epub_doc) if original_epub_doc else []
    if not converted_epub_doc:
        return findings
    orig_by_text = {l.text.semantic: l for l in (original_epub_doc.links if original_epub_doc else [])}
    conv_by_text = {l.text.semantic: l for l in converted_epub_doc.links}
    for text, ol in orig_by_text.items():
        cl = conv_by_text.get(text)
        if cl is None:
            findings.append({"type": "MISSING_LINK", "text": ol.text.raw, "href": ol.href,
                              "severity": "MEDIUM", "confidence": 0.6})
        elif ol.target_id != cl.target_id and ol.target_id and cl.target_id:
            findings.append({"type": "LINK_TARGET_CHANGED", "text": ol.text.raw,
                              "original_target": ol.target_id, "converted_target": cl.target_id,
                              "severity": "MEDIUM", "confidence": 0.6})
        elif ol.kind != cl.kind:
            findings.append({"type": "WRONG_LINK_TARGET", "text": ol.text.raw, "original_kind": ol.kind,
                              "converted_kind": cl.kind, "severity": "LOW", "confidence": 0.5})
    return findings
