"""TAG NORMALIZATION - final clean-up of inline formatting in a generated tree.

Runs on the generator's intermediate tree (before Mapping.xml). For every
element whose content is text plus inline formatting elements only, the
inline content is re-expressed through the range model
(core.formatting_ranges): parse -> normalize -> rebuild minimal, well-nested
markup. That:

  * removes empty formatting elements;
  * removes duplicate / nested-duplicate formatting (<b><b>x</b></b>);
  * merges identical adjacent ranges (<b>a</b><b>b</b> -> <b>ab</b>,
    <b>A </b><u><b>B</b></u> -> <b>A <u>B</u></b>);
  * keeps separate ranges separate (two underlines with an undecorated
    space between them stay two underlines);
  * preserves the text exactly - the rebuilt plain text is compared with the
    original and the element is left untouched if they differ in any way;
  * never touches structural elements, elements with attributes, or any
    element whose inline children are not pure formatting.
"""
import re

from lxml import etree

from core.formatting_ranges import parse_ranges, normalize_ranges

_EXCLUDED_CONTAINERS = {"table", "tr", "thead", "tbody"}


def inline_vocabulary() -> set:
    """Inline FORMATTING element names of this project: the names the
    extraction pipeline and the verification editor already use
    (core.verification.span_model), never a new list."""
    from core.verification.span_model import _TAG_TO_FIELD
    return set(_TAG_TO_FIELD)


def _local(tag):
    return tag.split("}", 1)[1] if isinstance(tag, str) and "}" in tag else tag


def _is_pure_inline(el, vocab) -> bool:
    for d in el.iterdescendants():
        if not isinstance(d.tag, str):
            return False  # comments / PIs - leave alone
        if _local(d.tag) not in vocab or d.attrib:
            return False
    return True


def _inner_markup(el) -> str:
    text = el.text or ""
    parts = [_escape(text)]
    for child in el:
        parts.append(etree.tostring(child, encoding="unicode", with_tail=False))
        parts.append(_escape(child.tail or ""))
    return "".join(parts)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_nested(plain: str, ranges: list) -> str:
    """Minimal well-nested markup: longer ranges outermost; a range that
    would cross an open one is split (closed and reopened)."""
    events_start = {}
    for r in ranges:
        events_start.setdefault(r["start"], []).append(r)
    out = []
    stack = []   # open ranges
    n = len(plain)
    for i in range(n + 1):
        # close ranges ending here
        ending = [r for r in stack if r["end"] == i]
        if ending:
            reopen = []
            while any(r["end"] == i for r in stack):
                top = stack.pop()
                out.append(f"</{top['style']}>")
                if top["end"] != i:
                    reopen.append(top)
            for r in reversed(reopen):
                out.append(f"<{r['style']}>")
                stack.append(r)
        if i == n:
            break
        for r in sorted(events_start.get(i, []), key=lambda r: (-(r["end"] - r["start"]), r["style"])):
            out.append(f"<{r['style']}>")
            stack.append(r)
        out.append(_escape(plain[i]))
    while stack:
        out.append(f"</{stack.pop()['style']}>")
    return "".join(out)


def normalize_element(el, vocab=None) -> bool:
    """Normalizes one element's inline content in place. Returns True when
    something changed."""
    vocab = vocab or inline_vocabulary()
    if len(el) == 0 or _local(el.tag) in _EXCLUDED_CONTAINERS or not _is_pure_inline(el, vocab):
        return False
    original = _inner_markup(el)
    plain, ranges, issues = parse_ranges(original)
    if issues:
        return False
    norm = normalize_ranges(ranges, len(plain))
    rebuilt = _build_nested(plain, norm)
    if rebuilt == original:
        return False
    try:
        frag = etree.fromstring(f"<x>{rebuilt}</x>".encode("utf-8"))
    except etree.XMLSyntaxError:
        return False
    if "".join(frag.itertext()) != "".join(el.itertext()):
        return False  # text integrity guard - never change a single character
    tail = el.tail
    for child in list(el):
        el.remove(child)
    el.text = frag.text
    for child in frag:
        el.append(child)
    el.tail = tail
    return True


def remove_empty_inline(root, vocab=None) -> int:
    vocab = vocab or inline_vocabulary()
    removed = 0
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or _local(el.tag) not in vocab or el.attrib:
            continue
        if len(el) == 0 and not (el.text or ""):
            parent = el.getparent()
            if parent is None:
                continue
            tail = el.tail or ""
            prev = el.getprevious()
            if prev is not None:
                prev.tail = (prev.tail or "") + tail
            else:
                parent.text = (parent.text or "") + tail
            parent.remove(el)
            removed += 1
    return removed


def normalize_tree(root, vocab=None) -> dict:
    """Normalizes every eligible element of the tree. Returns counts."""
    vocab = vocab or inline_vocabulary()
    stats = {"empty_removed": remove_empty_inline(root, vocab), "elements_normalized": 0}
    before = "".join(root.itertext())
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or _local(el.tag) in vocab:
            continue
        if normalize_element(el, vocab):
            stats["elements_normalized"] += 1
    if "".join(root.itertext()) != before:  # pragma: no cover - guarded per element above
        raise AssertionError("tag normalization changed the document text")
    return stats
