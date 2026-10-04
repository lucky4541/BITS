"""Step (spec: "NEVER CREATE NESTED <a>" / "DO NOT CREATE INVALID DUPLICATE
LINKS") - detects an <a> whose ENTIRE content is exactly one other <a>
(no other sibling text/elements alongside it) and, when both anchors
carry the SAME href, collapses them into a single <a> - never guessing
when the two hrefs actually differ (that ambiguous case is reported, not
silently resolved, per the same "prefer REVIEW over a wrong repair"
discipline used throughout this module).

Confirmed as a REAL, reproducible bug while building this: a citation
already wrapped in a valid <a> (from an earlier run, or from a prior
linking step in the SAME run) would be re-wrapped in a NEW, nested <a> by
figure_table_links.py's own main loop, which iterated every element in
the tree without excluding an element that is itself already an <a> (or
nested inside one). That root cause is fixed directly in
figure_table_links.py (see _is_inside_anchor); this module is the
separate, general CLEANUP pass for any nested/duplicate anchor that
already exists in a project - from an earlier flawed run, or from the
source document itself - regardless of which linking module (or
upstream conversion step) produced it."""
from dataclasses import dataclass, field

from core.epub_structure.xhtml_parser import local_name


@dataclass
class NormalizeResult:
    duplicates_fixed: int = 0
    review: list = field(default_factory=list)   # (doc_path, outer_href, inner_href, reason)


def normalize_nested_anchors(registry) -> NormalizeResult:
    """THE single entry point. Mutates each document's tree in place and
    marks it dirty; the orchestrator serializes dirty documents exactly
    like every other generation step. Only ever removes a wrapper <a> that
    contributes NO visible text/attributes of its own beyond the
    (identical) href already carried by its sole inner <a> child - visible
    text and the inner anchor's own attributes are otherwise untouched."""
    result = NormalizeResult()
    for doc in registry.documents:
        if doc.tree is None:
            continue
        changed_here = False
        progress = True
        while progress:
            progress = False
            for el in list(doc.tree.iter()):
                if local_name(el.tag) != "a":
                    continue
                children = list(el)
                if len(children) != 1:
                    continue
                inner = children[0]
                if local_name(inner.tag) != "a":
                    continue
                if el.text or inner.tail:
                    continue   # extra text alongside the inner <a> - not a pure nesting, never guess
                outer_href = el.get("href") or ""
                inner_href = inner.get("href") or ""
                if outer_href != inner_href:
                    result.review.append((doc.path, outer_href, inner_href,
                                           "nested <a> tags carry different hrefs - not auto-collapsed"))
                    continue
                parent = el.getparent()
                if parent is None:
                    continue
                idx = list(parent).index(el)
                inner.tail = el.tail
                parent.remove(el)
                parent.insert(idx, inner)
                result.duplicates_fixed += 1
                changed_here = True
                progress = True
                break   # tree structure changed mid-iteration - restart this document's scan safely
        if changed_here:
            doc.dirty = True
    return result
