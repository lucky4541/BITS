"""Step 5 (spec: "AUTOMATIC INTERNAL LINKS" - figure/table references) -
wraps every real figure/table/plate/diagram CITATION in body text with a
real <a href="..."> pointing at that item's own id, whenever the target
is confidently resolvable (spec: "Never create a broken link").

Two cases, both text-preserving by construction (never removes or adds a
visible character, only wraps existing text in an <a>):
  1. The citation's keyword+number sit entirely within ONE text run (an
     element's own .text, or one child's own .tail) - handled by
     _split_and_link, which reuses core.epub_structure.figure_table_
     registry.iter_citations() for the same keyword/range/list matching
     the registry itself uses.
  2. The citation is SPLIT ACROSS MARKUP - a real, explicitly required
     case ("Fig. <i>1.1</i>" must still be recognized) - handled by
     _wrap_split_citation, which detects a keyword ending an element's own
     .text immediately followed by an inline child whose ENTIRE flattened
     text is a bare number, and wraps keyword+child together in a new <a>
     by moving the existing child element INTO it (never cloning, never
     touching the child's own internal markup). A citation split any other
     way (number itself split across two runs, or more than one
     intervening child) is left unlinked rather than risking a
     mis-constructed tree - disclosed, not silently guessed at."""
import posixpath
import re
from dataclasses import dataclass, field

from lxml import etree

from core.epub_structure.figure_table_registry import iter_citations, report_kind
from core.epub_structure.xhtml_parser import XHTML_NS, local_name

_A = f"{{{XHTML_NS}}}a"
_INLINE_TAGS = {"i", "b", "sup", "sub", "span", "em", "strong", "u", "small"}
_KEYWORD_TRAILING_RE = re.compile(r"\b(Fig(?:ure|s|\.)?|Table|Plate|Diagram)s?\.?\s*$", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"^\d+(?:[.\-]\d+)*$")


@dataclass
class LinkResult:
    linked: int = 0
    split_markup_linked: int = 0
    details: list = field(default_factory=list)   # (doc_path, match_text, target_doc, target_id)
    detected: int = 0            # every citation-shaped match encountered, resolvable or not
    figures_detected: int = 0
    tables_detected: int = 0
    figures_linked: int = 0
    tables_linked: int = 0
    caption_links_created: int = 0     # a figure/table's OWN caption label, self-linked to its own id
    body_links_created: int = 0         # a genuine body-text citation
    cross_file_links_created: int = 0    # any of the above whose target lives in a different document
    review: list = field(default_factory=list)   # (doc_path, match_text, report_kind, reason)
    already_valid: int = 0        # existing FIGURE anchors, resolved AFTER link_resolver, not from this run
    auto_fixed: int = 0             # ditto, repaired by link_resolver
    broken: int = 0                  # ditto, still unresolvable
    tables_already_valid: int = 0    # same three, but for existing TABLE anchors
    tables_auto_fixed: int = 0
    tables_broken: int = 0
    new_anchors: set = field(default_factory=set, repr=False)   # internal - anchors THIS run created


def _entry_by_key(entries) -> dict:
    by_key = {}
    for entry in entries:
        if entry.number and entry.id and entry.citation_kind:
            by_key.setdefault((entry.citation_kind, entry.number), entry)
    return by_key


def _make_anchor(entry, doc_path: str, text: str):
    anchor = etree.Element(_A)
    rel_href = ("" if entry.doc_path == doc_path else posixpath.basename(entry.doc_path)) + f"#{entry.id}"
    anchor.set("href", rel_href)
    anchor.text = text
    return anchor


def _split_and_link(text: str, by_key: dict, doc_path: str, result: LinkResult, is_caption: bool = False):
    """Returns (leading_text, [anchor_element, ...]) or None if `text` has
    no LINKABLE citation (unresolved citations are still counted/reported,
    just not wrapped in an <a>). Each returned anchor already has its own
    correct .tail set (the text run between it and the next anchor, or the
    end of the original text). `is_caption` only affects reporting (caption
    self-link vs body-reference link counts) - the linking logic itself is
    identical either way."""
    all_citations = iter_citations(text)
    linkable = [(kind, number, start, end) for kind, number, start, end in all_citations
                if (kind, number) in by_key]
    for kind, number, start, end in all_citations:
        result.detected += 1
        rk = report_kind(kind)
        if rk == "table":
            result.tables_detected += 1
        else:
            result.figures_detected += 1
        if (kind, number) not in by_key:
            result.review.append((doc_path, text[start:end], rk, f"no {rk} with this number exists"))
    if not linkable:
        return None

    leading = text[:linkable[0][2]]
    nodes = []
    for i, (kind, number, start, end) in enumerate(linkable):
        entry = by_key[(kind, number)]
        anchor = _make_anchor(entry, doc_path, text[start:end])
        next_start = linkable[i + 1][2] if i + 1 < len(linkable) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
        result.new_anchors.add(anchor)
        result.linked += 1
        if report_kind(kind) == "table":
            result.tables_linked += 1
        else:
            result.figures_linked += 1
        if is_caption:
            result.caption_links_created += 1
        else:
            result.body_links_created += 1
        if entry.doc_path != doc_path:
            result.cross_file_links_created += 1
        result.details.append((doc_path, text[start:end], entry.doc_path, entry.id))
    return leading, nodes


def _is_figure_family(el) -> bool:
    return local_name(el.tag) in ("figure", "figcaption")


def _is_inside_anchor(el) -> bool:
    """True if `el` itself, or any ancestor up to the document root, is an
    <a> - i.e. `el`'s own .text already lives inside an existing link.
    Without this check, a SECOND run over an already-linked citation (or a
    citation body-linked before this module's own figure-caption self-
    link step runs) would re-wrap it in a NEW, nested <a> - the exact
    "duplicate/nested <a> tags" bug confirmed on a real citation that was
    already linked."""
    while el is not None:
        if local_name(el.tag) == "a":
            return True
        el = el.getparent()
    return False


def _is_caption_context(el) -> bool:
    """True if `el` itself is the element carrying a figure/table's OWN
    caption text - a real <figcaption>, or the "figcaption"-classed <p>
    convention confirmed against a real production book
    (<figcaption><p class="figcaption">Fig. 1.3. ...</p></figcaption>)."""
    if local_name(el.tag) == "figcaption":
        return True
    return "figcaption" in (el.get("class") or "").lower()


def _wrap_split_citation(el, by_key: dict, doc_path: str, result: LinkResult) -> bool:
    """Handles "Fig. <i>1.1</i>": el's own .text ends with a bare keyword
    (no number - the number lives entirely inside its first child), and
    that first child's ENTIRE flattened text is a bare number. Wraps
    keyword-text + the existing child (moved, not cloned) into one new
    <a>, and shortens el.text to drop the keyword portion. Returns True if
    a wrap was performed (the caller should not also try the same-run
    split on el.text.text, since the keyword text was just removed)."""
    if not el.text:
        return False
    km = _KEYWORD_TRAILING_RE.search(el.text)
    if not km:
        return False
    children = list(el)
    if not children:
        return False
    first_child = children[0]
    if _is_figure_family(first_child) or local_name(first_child.tag) not in _INLINE_TAGS:
        return False
    child_text = "".join(first_child.itertext())
    if not _BARE_NUMBER_RE.match(child_text):
        return False

    keyword_text = el.text[km.start():]
    combined = keyword_text + child_text
    found = iter_citations(combined)
    if not found:
        return False
    kind, number, _start, end = found[0]
    if end != len(combined):
        return False  # the match doesn't cleanly cover keyword+number together - never guess
    result.detected += 1
    rk = report_kind(kind)
    if rk == "table":
        result.tables_detected += 1
    else:
        result.figures_detected += 1
    entry = by_key.get((kind, number))
    if entry is None:
        result.review.append((doc_path, combined, rk, f"no {rk} with this number exists"))
        return False

    el.text = el.text[:km.start()]
    anchor = etree.Element(_A)
    rel_href = ("" if entry.doc_path == doc_path else posixpath.basename(entry.doc_path)) + f"#{entry.id}"
    anchor.set("href", rel_href)
    anchor.text = keyword_text
    anchor.tail = first_child.tail
    first_child.tail = None
    el.insert(0, anchor)
    anchor.append(first_child)   # lxml moves the REAL node here - never a clone
    result.new_anchors.add(anchor)

    result.linked += 1
    result.split_markup_linked += 1
    if rk == "table":
        result.tables_linked += 1
    else:
        result.figures_linked += 1
    if _is_caption_context(el):
        result.caption_links_created += 1
    else:
        result.body_links_created += 1
    if entry.doc_path != doc_path:
        result.cross_file_links_created += 1
    result.details.append((doc_path, keyword_text + child_text, entry.doc_path, entry.id))
    return True


def apply_figure_table_links(registry, entries: list) -> LinkResult:
    """THE single entry point. Mutates each document's tree in place and
    marks it dirty; the orchestrator serializes dirty documents exactly
    like every other generation step. Never descends into a <figure>/
    <figcaption> - a caption labeling its own figure is never itself
    turned into a link to that same figure."""
    result = LinkResult()
    by_key = _entry_by_key(entries)
    if not by_key:
        return result

    for doc in registry.documents:
        if doc.tree is None:
            continue
        changed_here = False
        for el in list(doc.tree.iter()):
            # <figure> itself is never scannable text (its own .text is
            # just inter-tag whitespace), but <figcaption> IS - spec:
            # "every figure caption with a recognized figure number should
            # contain a link on the figure label", and a <figcaption> that
            # holds its own caption text directly (no wrapping <p>) must
            # get that same self-link. This never reintroduces "the
            # caption cites itself" into first-citation-PLACEMENT data -
            # figure_table_registry.build_figure_table_registry() already
            # separately excludes anything inside <figure>/<figcaption>
            # from being counted as a real citation there.
            if local_name(el.tag) == "figure" or _is_inside_anchor(el):
                continue

            if _wrap_split_citation(el, by_key, doc.path, result):
                changed_here = True

            # Captured AFTER _wrap_split_citation (so ITS OWN newly-created
            # anchor is included - its tail is a genuinely never-before-
            # scanned tail inherited from a real pre-existing child) but
            # BEFORE el.text is split just below (so THOSE newly-created
            # anchors are excluded - their tail is merely the tail end of
            # the SAME el.text string _split_and_link just fully scanned
            # in one pass, review items included; re-scanning it via a
            # freshly-fetched list(el) would report the SAME unresolved
            # citation a second time - a real, confirmed duplicate-review
            # bug, not merely a theoretical one).
            children_to_scan = list(el)

            if el.text:
                split = _split_and_link(el.text, by_key, doc.path, result, is_caption=_is_caption_context(el))
                if split is not None:
                    leading, anchors = split
                    el.text = leading
                    for offset, anchor in enumerate(anchors):
                        el.insert(offset, anchor)
                    changed_here = True

            is_caption = _is_caption_context(el)
            for child in children_to_scan:
                if _is_figure_family(child) or not child.tail:
                    continue
                split = _split_and_link(child.tail, by_key, doc.path, result, is_caption=is_caption)
                if split is not None:
                    leading, anchors = split
                    child.tail = leading
                    insert_at = el.index(child) + 1
                    for offset, anchor in enumerate(anchors):
                        el.insert(insert_at + offset, anchor)
                    changed_here = True

        if changed_here:
            doc.dirty = True
    return result


def classify_existing_figure_links(registry, entries: list, link_resolution, result: LinkResult) -> None:
    """Second pass, run AFTER core.epub_structure.link_resolver has
    already validated/repaired every href in the book. Fills in
    result.already_valid/auto_fixed/broken for every PRE-EXISTING anchor
    that already targets a real figure/table id - cross-referencing the
    SAME live elements link_resolver's own LinkResolution already
    classified, never a second, divergent repair pass. Skips every anchor
    in result.new_anchors - a figure/table link this SAME run just
    created, already counted in result.linked/created (mirrors
    core.epub_structure.index_locator_links.classify_existing_locators,
    which the same double-counting bug was fixed in first)."""
    ids_by_kind = {entry.id: entry.kind for entry in entries if entry.id}
    if not ids_by_kind:
        return
    fixed_elements = {issue.element for issue in link_resolution.fixed}
    review_elements = {issue.element for issue in link_resolution.review}
    for doc in registry.documents:
        if doc.tree is None:
            continue
        for el in doc.tree.iter():
            if el in result.new_anchors or local_name(el.tag) != "a":
                continue
            href = el.get("href") or ""
            fragment = href.split("#", 1)[1] if "#" in href else ""
            kind = ids_by_kind.get(fragment)
            if kind is None:
                continue
            is_table = kind == "table"
            if el in review_elements:
                result.tables_broken += 1 if is_table else 0
                result.broken += 0 if is_table else 1
            elif el in fixed_elements:
                result.tables_auto_fixed += 1 if is_table else 0
                result.auto_fixed += 0 if is_table else 1
            else:
                result.tables_already_valid += 1 if is_table else 0
                result.already_valid += 0 if is_table else 1
