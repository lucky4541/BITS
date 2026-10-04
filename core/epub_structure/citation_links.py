"""Author-year bibliography citation linking (spec: "FAST IMPLEMENTATION -
ADVANCED AUTOMATIC EPUB REFERENCE LINKER", sections 14-21) - the one
citation category confirmed to be missing from this pipeline's existing
reference_links.py, which only recognizes NUMBERED citations ("(1.1)",
"[3]") keyed off a bibliography entry's own leading number. This module
adds "Smith (2020)", "(Smith, 2020)", "Smith and Jones (2019)", "Smith et
al. (2020)", "World Health Organization (2020)", and semicolon-separated
multiple citations ("(Smith, 2020; Jones, 2021)") - reusing the exact same
architecture, span-insertion helpers, and "never guess, skip existing
links" philosophy as reference_links.py/chapter_section_links.py so it
composes safely with them rather than fighting them (both are wired into
orchestrator.run_full_analysis's own Level 3 linking phase, side by side).

DICTIONARY-DRIVEN, not free-form name matching: every author surname or
institutional phrase this module will ever try to match comes ONLY from
what build_citation_registry() actually found in this book's own
bibliography entries - it never invents a candidate name from an
arbitrary capitalized word in body text. This is a deliberate, confirmed
precision choice: a real production book (reviewed directly before
writing this module) already showed the opposite failure mode from an
unrelated, prior linking pass - the ordinary word "contents" repeatedly
linked to the book's own Table of Contents page purely because it
matched that page's title text, with no grounding in an actual target
registry entry. Matching only against real, known bibliography names
makes that entire failure class structurally impossible here.

A citation naming MULTIPLE years for the same author ("Kim 1992, 1993")
is deliberately left unlinked (reported for review) rather than
attempting a fine-grained per-year sub-split of the shared author name -
spec 65: "prefer NO LINK over WRONG LINK" - this one narrow case is where
that tradeoff is made explicitly; every other listed citation shape
(single year, et al., multiple authors, institutional, multiple
semicolon-separated citations) is fully handled below."""
import re
from dataclasses import dataclass, field

from lxml import etree

from core.epub_structure.id_assigner import derive_content_prefix
from core.epub_structure.xhtml_parser import XHTML_NS, epub_type, local_name

_YEAR = r"(?:1[5-9]\d{2}|20\d{2})[a-z]?"
_YEAR_RE = re.compile(_YEAR)
_YEAR_LIST_RE = re.compile(rf"{_YEAR}(?:\s*,\s*{_YEAR})*")
_ET_AL_RE = re.compile(r"\bet\s+al\.?\b", re.IGNORECASE)
_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'’\-]*$")
_LEADING_LABEL_RE = re.compile(r"^\s*[\[(]?\d+(?:\.\d+)*[\])]?\.?\s*")
_OUTER_PAREN_RE = re.compile(r"\(([^()]*)\)")
# One "author name unit," found by matching its own self-contained shape
# rather than by SPLITTING the surrounding text on commas/and/& - a bare
# comma is genuinely ambiguous on its own ("Smith, J." is ONE author;
# "Smith, Jones" is TWO), but "Surname, Initial(s)" and "Initial(s)
# Surname" are each unambiguous as a single unit, so scanning for
# consecutive unit-matches (finditer, not split) correctly walks past
# ", " / ", and " / " & " between them without ever needing to classify
# what kind of separator a given comma is. Confirmed necessary directly:
# an earlier split-based version of this parser mis-split "Smith, J."
# into two separate "authors" ("Smith" and "J.").
_NAME_UNIT_RE = re.compile(
    r"(?:(?P<surname1>[A-Z][A-Za-z'’\-]+)\s*,\s*(?P<initials1>(?:[A-Z]\.\s*)+))"
    r"|(?:(?P<initials2>(?:[A-Z]\.\s*)+)(?P<surname2>[A-Z][A-Za-z'’\-]+))"
    r"|(?P<surname3>[A-Z][A-Za-z'’\-]+)"
)


def _extract_surnames(segment: str) -> list:
    return [m.group("surname1") or m.group("surname2") or m.group("surname3")
            for m in _NAME_UNIT_RE.finditer(segment)]


@dataclass
class CitationEntry:
    id: str
    doc_path: str
    element: object
    surnames: tuple          # lowercase, one entry per author; a single institutional phrase for that case
    years: tuple              # normalized year strings, e.g. ("1988a", "1988b")
    is_institutional: bool = False
    needs_id: bool = False


@dataclass
class LinkResult:
    detected: int = 0
    created: int = 0
    review: list = field(default_factory=list)     # (doc_path, text, reason)
    ids_assigned: int = 0
    cross_file_created: int = 0


def _is_biblioentry(el) -> bool:
    if local_name(el.tag) != "li":
        return False
    return epub_type(el) in ("biblioentry", "reference") or "biblioentry" in (el.get("class") or "").lower()


def _parse_biblio_entry(text: str):
    """Returns (surnames_tuple, years_tuple, is_institutional) or None when
    no year (so no usable citation anchor) can be found at all. Handles
    both common bibliography styles - "Surname, F. (Year)" and "F.
    Surname (Year)" - by checking whether every token but the last in a
    name group looks like a bare initial."""
    years = tuple(m.group(0) for m in _YEAR_RE.finditer(text))
    if not years:
        return None
    author_segment = text[:_YEAR_RE.search(text).start()]
    author_segment = _LEADING_LABEL_RE.sub("", author_segment).strip(" ,.()")
    if not author_segment:
        return None

    parts = author_segment.split()
    has_comma = "," in author_segment
    all_name_like = all(_WORD_RE.match(p.rstrip(".")) or p.lower() in ("of", "for", "the", "and")
                         for p in parts)
    if not has_comma and len(parts) >= 2 and all_name_like and parts[0][:1].isupper():
        looks_like_initials_form = all(len(p.rstrip(".")) <= 1 for p in parts[:-1])
        if not looks_like_initials_form:
            # e.g. "World Health Organization" - no single personal surname to extract.
            return ((author_segment,), years, True)
        return ((parts[-1],), years, False)

    surnames = _extract_surnames(_ET_AL_RE.sub("", author_segment))
    if not surnames:
        return None
    return (tuple(surnames), years, False)


def build_citation_registry(registry) -> list:
    """Every bibliography entry, book-wide, that has at least a year (spec
    14: "scan the entire bibliography/reference section... before
    searching for author-year citations"). A citation candidate found in
    body text is later resolved against this list, never the reverse -
    the bibliography is always the source of truth for what a valid
    author/year combination even IS."""
    entries = []
    for doc in registry.documents:
        if doc.tree is None:
            continue
        for el in doc.tree.iter():
            if not _is_biblioentry(el):
                continue
            parsed = _parse_biblio_entry("".join(el.itertext()))
            if parsed is None:
                continue
            surnames, years, institutional = parsed
            # ORIGINAL case preserved (never lowercased here) - _known_names
            # below needs the real capitalization to build a body-text match
            # regex against actual proper nouns; lookup keys are lowercased
            # separately, only at the point they're actually used as dict keys.
            entries.append(CitationEntry(
                id=el.get("id") or "", doc_path=doc.path, element=el,
                surnames=tuple(surnames), years=years,
                is_institutional=institutional, needs_id=not el.get("id")))
    return entries


def _known_names(entries) -> list:
    """Every distinct surname/institutional phrase actually seen in the
    bibliography - the ONLY vocabulary the body-text scanner is ever
    allowed to treat as a candidate author (see module docstring)."""
    names = set()
    for entry in entries:
        names.update(entry.surnames)
    return sorted(names, key=len, reverse=True)   # longest-first so e.g. "Smithson" never matches as "Smith"


def _resolve(surnames, years, et_al: bool, by_key: dict):
    """(surnames, years) as parsed from body text -> a single CitationEntry,
    or None when no confident, unambiguous match exists. Only ever
    attempts resolution for a SINGLE cited year (spec's own deliberate
    "Kim 1992, 1993" scope limit - see module docstring); the caller
    filters multi-year candidates out before ever reaching here."""
    if len(years) != 1:
        return None
    year = years[0]
    key = (tuple(sorted(s.lower() for s in surnames)), year, et_al)
    candidates = by_key.get(key)
    if candidates and len(candidates) == 1:
        return candidates[0]
    if et_al:
        # "Smith et al. (2020)" - match any entry LED by "Smith" for that
        # year, regardless of exactly how many co-authors it actually has
        # (the citing text never spells the rest out, by definition).
        lead_key = (surnames[0].lower(), year)
        candidates = by_key.get(("lead", lead_key))
        if candidates and len(candidates) == 1:
            return candidates[0]
    return None


def _build_key_index(entries) -> dict:
    by_key = {}
    for entry in entries:
        lower_surnames = tuple(sorted(s.lower() for s in entry.surnames))
        is_multi = len(entry.surnames) > 1
        for year in entry.years:
            exact_key = (lower_surnames, year, is_multi)
            by_key.setdefault(exact_key, []).append(entry)
            if is_multi:
                # A query naming ALL co-authors explicitly ("Smith and Jones
                # 2020") has et_al=False even though the entry itself has
                # more than one author - a SEPARATE key, distinct from
                # exact_key above, is needed to match that. For a single-
                # author entry, exact_key already has et_al/is_multi=False,
                # so adding this again would double-list the same entry
                # under the identical key, breaking _resolve's own
                # len(candidates) == 1 unambiguous-match check.
                single_key = (lower_surnames, year, False)
                by_key.setdefault(single_key, []).append(entry)
            if entry.surnames:
                lead_key = ("lead", (entry.surnames[0].lower(), year))
                by_key.setdefault(lead_key, []).append(entry)
    return by_key


def _author_group_pattern(alt: str) -> str:
    joined = rf"(?:{alt})(?:\s*,\s*(?:{alt}))*\s*(?:,\s*)?(?:and|&)\s*(?:{alt})"
    etal = rf"(?:{alt})\s+et\s+al\.?"
    single = rf"(?:{alt})"
    return rf"(?:{joined}|{etal}|{single})"


def _parse_matched_authors(matched_text: str, alt: str):
    """(surnames, et_al) from an already-matched author-group substring.
    Since the substring was matched USING the known-name alternation in
    the first place, re-matching that same alternation against it
    directly recovers exactly which known name(s) it contains, in order -
    no separate comma/and/& splitting logic needed (and none of that
    logic's own ambiguity - see _NAME_UNIT_RE's own docstring - applies
    here, since body-text citations name bare surnames, never
    "Surname, Initial" bibliography-style entries)."""
    et_al = bool(_ET_AL_RE.search(matched_text))
    names = tuple(re.findall(alt, matched_text))
    return names, et_al


def _find_citations(text: str, alt: str, by_key: dict):
    """Yields (start, end) spans, each already resolved to exactly one
    CitationEntry, for every confident citation in `text` - narrative form
    ("Smith (2020)") and parenthetical form ("(Smith, 2020)",
    "(Smith, 2020; Jones, 2021)") scanned separately since they can never
    overlap by construction (the character immediately after the author
    group is either "(" - narrative - or "," - parenthetical -, never
    both for the same match)."""
    group = _author_group_pattern(alt)
    spans = []

    narrative_re = re.compile(rf"\b({group})\s*\(\s*({_YEAR_LIST_RE.pattern})\s*\)")
    for m in narrative_re.finditer(text):
        surnames, et_al = _parse_matched_authors(m.group(1), alt)
        years = tuple(y.strip() for y in re.split(r"\s*,\s*", m.group(2)))
        entry = _resolve(surnames, years, et_al, by_key)
        if entry is not None:
            spans.append((m.start(), m.end(), entry))
        else:
            spans.append((m.start(), m.end(), None))

    segment_re = re.compile(rf"^\s*({group})[,\s]+({_YEAR_LIST_RE.pattern})\s*$")
    for pm in _OUTER_PAREN_RE.finditer(text):
        inner = pm.group(1)
        inner_start = pm.start(1)
        cursor = 0
        for piece in inner.split(";"):
            piece_start = inner_start + cursor
            cursor += len(piece) + 1   # +1 for the ";" consumed by split
            sm = segment_re.match(piece)
            if not sm:
                continue
            # Trim the segment's own span down to sm's own matched group
            # extents (never the surrounding whitespace split() left in).
            seg_start = piece_start + sm.start(1)
            seg_end = piece_start + sm.end(2)
            surnames, et_al = _parse_matched_authors(sm.group(1), alt)
            years = tuple(y.strip() for y in re.split(r"\s*,\s*", sm.group(2)))
            entry = _resolve(surnames, years, et_al, by_key)
            if entry is not None:
                spans.append((seg_start, seg_end, entry))
            else:
                spans.append((seg_start, seg_end, None))

    spans.sort(key=lambda s: s[0])
    # Narrative and parenthetical scans cannot overlap (see docstring), but
    # guard anyway - drop any span that starts before the previous one ended.
    out, last_end = [], -1
    for start, end, entry in spans:
        if start < last_end:
            continue
        out.append((start, end, entry))
        last_end = end
    return out


def apply_citation_links(registry, entries: list) -> LinkResult:
    """THE single entry point - mirrors reference_links.apply_reference_links
    exactly (same ancestor-skip rules, same el.text/child.tail two-pass
    scan, same deterministic id-on-demand assignment), just for author-year
    citations instead of numbered ones."""
    result = LinkResult()
    if not entries:
        return result
    names = _known_names(entries)
    if not names:
        return result
    alt = "|".join(re.escape(n) for n in names)
    by_key = _build_key_index(entries)

    def _ensure_id(entry) -> str:
        if entry.needs_id:
            prefix = derive_content_prefix(entry.doc_path)
            entry.id = f"{prefix}_cite_{'_'.join(s.lower().replace(' ', '') for s in entry.surnames)}_{entry.years[0]}"
            entry.element.set("id", entry.id)
            entry.needs_id = False
            result.ids_assigned += 1
            target_doc_record = registry.by_path.get(entry.doc_path)
            if target_doc_record is not None:
                target_doc_record.dirty = True
        return entry.id

    def _resolve_spans(text: str, doc_path: str):
        found = _find_citations(text, alt, by_key)
        spans = []
        for start, end, entry in found:
            result.detected += 1
            if entry is None:
                result.review.append((doc_path, text[start:end],
                                       "no unambiguous bibliography entry matches this author/year"))
                continue
            target_id = _ensure_id(entry)
            if entry.doc_path != doc_path:
                result.cross_file_created += 1
            spans.append((start, end, entry.doc_path, target_id, text[start:end]))
        return spans

    for doc in registry.documents:
        if doc.tree is None:
            continue
        changed_here = False
        for el in list(doc.tree.iter()):
            ln = local_name(el.tag)
            if ln not in ("p", "li"):
                continue
            if _is_biblioentry(el):
                continue   # a bibliography entry's own leading author/year is its OWN label, never a citation of itself
            ancestor = el.getparent()
            skip = False
            while ancestor is not None:
                if local_name(ancestor.tag) in ("figure", "figcaption", "a"):
                    skip = True
                    break
                ancestor = ancestor.getparent()
            if skip:
                continue

            original_children = list(el)

            if el.text:
                spans = _resolve_spans(el.text, doc.path)
                if spans:
                    _apply_spans(el, doc.path, spans)
                    result.created += len(spans)
                    changed_here = True

            for child in original_children:
                if local_name(child.tag) in ("figure", "figcaption") or not child.tail:
                    continue
                spans = _resolve_spans(child.tail, doc.path)
                if spans:
                    _apply_tail_spans(el, child, doc.path, spans)
                    result.created += len(spans)
                    changed_here = True

        if changed_here:
            doc.dirty = True
    return result


def _apply_spans(el, doc_path: str, spans):
    text = el.text
    leading = text[:spans[0][0]]
    nodes = []
    for i, (start, end, target_doc_path, target_id, span_text) in enumerate(spans):
        anchor = etree.Element(f"{{{XHTML_NS}}}a")
        anchor.set("href", ("" if target_doc_path == doc_path else _basename(target_doc_path)) + f"#{target_id}")
        anchor.text = span_text
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
    el.text = leading
    for offset, anchor in enumerate(nodes):
        el.insert(offset, anchor)


def _apply_tail_spans(el, child, doc_path: str, spans):
    text = child.tail
    leading = text[:spans[0][0]]
    nodes = []
    for i, (start, end, target_doc_path, target_id, span_text) in enumerate(spans):
        anchor = etree.Element(f"{{{XHTML_NS}}}a")
        anchor.set("href", ("" if target_doc_path == doc_path else _basename(target_doc_path)) + f"#{target_id}")
        anchor.text = span_text
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        anchor.tail = text[end:next_start]
        nodes.append(anchor)
    child.tail = leading
    insert_at = el.index(child) + 1
    for offset, anchor in enumerate(nodes):
        el.insert(insert_at + offset, anchor)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]
