"""References/bibliography comparison (spec section 24) - comparison
only, never touches bibliography generation (auto_zoning/bibliography_*
and the Mapping.xml <li class="biblioentry"> convention are read-only
inputs here, never written to)."""
import re

from core.fidelity_compare import block_aligner

TEXT_MATCH_THRESHOLD = 0.85
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_AUTHOR_RE = re.compile(r"^[A-Z][A-Za-z'-]*(,|\s+[A-Z]\.)")


def _extract_year(text: str) -> str:
    m = _YEAR_RE.search(text)
    return m.group(1) if m else ""


def _extract_author(text: str) -> str:
    m = _AUTHOR_RE.match(text.strip())
    return m.group(0).rstrip(", ") if m else ""


def compare(original_doc, converted_doc) -> list:
    orig = original_doc.references
    conv = converted_doc.references
    pairs = block_aligner.align_by_key([r.text.semantic for r in orig], [r.text.semantic for r in conv])
    findings = []
    for p in pairs:
        if p.op == "delete":
            r = orig[p.original_index]
            findings.append({"type": "MISSING_REFERENCE", "text": r.text.raw, "severity": "HIGH",
                              "confidence": 0.85})
        elif p.op == "insert":
            r = conv[p.converted_index]
            findings.append({"type": "ADDED_REFERENCE", "text": r.text.raw, "severity": "HIGH",
                              "confidence": 0.85})
        elif p.op == "replace" and p.original_index is not None and p.converted_index is not None:
            ro, rc = orig[p.original_index], conv[p.converted_index]
            sim = block_aligner.similarity(ro.text.semantic, rc.text.semantic)
            if sim >= TEXT_MATCH_THRESHOLD:
                fields_changed = []
                if _extract_year(ro.text.raw) != _extract_year(rc.text.raw):
                    fields_changed.append("year")
                if _extract_author(ro.text.raw) != _extract_author(rc.text.raw):
                    fields_changed.append("author")
                findings.append({"type": "CHANGED_REFERENCE", "original_text": ro.text.raw,
                                  "converted_text": rc.text.raw, "fields_changed": fields_changed,
                                  "severity": "MEDIUM", "confidence": sim})
            else:
                findings.append({"type": "MISSING_REFERENCE", "text": ro.text.raw, "severity": "HIGH",
                                  "confidence": 0.75})
                findings.append({"type": "ADDED_REFERENCE", "text": rc.text.raw, "severity": "HIGH",
                                  "confidence": 0.75})
    orig_order = [p.original_index for p in pairs if p.op == "equal"]
    if orig_order and orig_order != sorted(orig_order):
        findings.append({"type": "REFERENCE_REORDERED", "severity": "MEDIUM", "confidence": 0.6})
    return findings
